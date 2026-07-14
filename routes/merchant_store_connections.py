"""
Merchant Store Connections
Allow merchants to connect their own stores (Shopify, Wix, etc.)
"""
from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from services.shopify_access_token_service import (
    exchange_shopify_client_credentials_token,
    resolve_shopify_admin_access_token,
)
from services.wix_connection import WixConnectionValidationError, validate_wix_catalog_access
from fastapi import APIRouter, Depends, HTTPException, Body, BackgroundTasks, Request, Query, Header
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, TypeAdapter, ValidationError
from typing import Dict, Any, Optional
from dataclasses import dataclass
import logging
import httpx
import json
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import re
import secrets
from urllib.parse import urlparse, urlencode

from db.database import database
from utils.auth import get_current_user
from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["Merchant Integrations"])

_SHOPIFY_OAUTH_STATE_TTL_SECONDS = 30 * 60
_SHOPIFY_OAUTH_REQUIRED_WEBHOOK_TOPICS = [
    "orders/create",
    "orders/updated",
    "orders/paid",
    "orders/cancelled",
    "fulfillments/create",
    "fulfillments/update",
    "orders/fulfilled",
    "app/uninstalled",
]

_STOREFRONT_AUTO_CREATE_DENIED_UNTIL: Dict[str, float] = {}
_STOREFRONT_AUTO_CREATE_DENIED_TTL_SECONDS = 24 * 3600
_MARKETPLACE_INSTALL_SUCCESS_PATH = "/app/install/success"


class ConnectShopifyRequest(BaseModel):
    merchant_id: str
    shop_domain: str
    access_token: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    # Optional: Shopify webhook HMAC secret (API secret key for the app that owns the webhooks).
    # If omitted, we fall back to the global SHOPIFY_CLIENT_SECRET (official app). For custom apps,
    # this should be provided so we can verify webhooks.
    webhook_secret: Optional[str] = None
    # Optional: Storefront token for quote/checkout pricing (Storefront Cart API).
    # If omitted, backend will try to auto-create one using the Admin token.
    storefront_access_token: Optional[str] = None
    storefront_token: Optional[str] = None


async def _create_storefront_access_token_best_effort(*, shop_domain: str, access_token: str) -> Optional[str]:
    """
    Best-effort create a Shopify Storefront API token using the Admin token.
    Requires the custom app to have Storefront API enabled on Shopify side.
    """
    try:
        shop_domain_lc = (shop_domain or "").strip().lower()
        if shop_domain_lc:
            import time

            denied_until = _STOREFRONT_AUTO_CREATE_DENIED_UNTIL.get(shop_domain_lc, 0.0)
            if denied_until and denied_until > time.time():
                return None

        url = f"https://{shop_domain}/admin/api/2024-07/storefront_access_tokens.json"
        headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
        payload = {"storefront_access_token": {"title": "Pivota Pricing"}}
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            # Common "expected" failures:
            # - 401/403: missing scope/permission for Storefront token creation
            # - 404: Storefront API not enabled on the app / endpoint unavailable for this shop
            # We silently skip and avoid spamming Shopify on every reconnect.
            if shop_domain_lc and resp.status_code in (401, 403, 404):
                _STOREFRONT_AUTO_CREATE_DENIED_UNTIL[shop_domain_lc] = time.time() + float(
                    _STOREFRONT_AUTO_CREATE_DENIED_TTL_SECONDS
                )
            return None
        data = resp.json() or {}
        storefront = data.get("storefront_access_token") if isinstance(data, dict) else None
        token = storefront.get("access_token") if isinstance(storefront, dict) else None
        return token.strip() if isinstance(token, str) and token.strip() else None
    except Exception:
        return None


def _canonicalize_shop_domain(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").strip().lower()
        return host or None
    except Exception:
        return raw.lower()


def _validate_myshopify_domain(value: str) -> str:
    shop = (_canonicalize_shop_domain(value) or "").strip().lower()
    if not shop:
        raise HTTPException(status_code=400, detail="shop is required")
    # Basic guard: allow only the canonical myshopify domain during OAuth.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", shop):
        raise HTTPException(status_code=400, detail="shop must be a *.myshopify.com domain")
    return shop


@dataclass(frozen=True)
class ShopifyAppCreds:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str
    label: str  # "appstore" | "headless"


# install_source values routed to the PUBLIC App Store app (App A = "Pivota").
# ALL third-party OAuth connects use the public App A: a merchant can install
# Pivota with Shopify's own OAuth (no custom app). App B ("Pivota Merchant") is
# a custom app locked to a single org and CANNOT install on third-party stores,
# so it is never used for OAuth — the write/BYO tier uses the custom-token
# /connect path (merchant-supplied client_id+secret), not resolve_shopify_app.
_APPSTORE_INSTALL_SOURCES = {"app_store", "merchant_portal"}


def resolve_shopify_app(install_source: Optional[str]) -> ShopifyAppCreds:
    """Select Shopify app credentials by install source.

    All OAuth install sources -> App A (public, read-only merchant tool), so
    merchants connect through Pivota's own Shopify OAuth without creating a
    custom app. The headless/write path is the custom-token /connect flow and
    does not go through here. Defaults fall back to the single SHOPIFY_CLIENT_*
    env, so this is a no-op until the SHOPIFY_APPSTORE_* envs are configured.
    """
    src = (install_source or "").strip().lower()
    if src in _APPSTORE_INSTALL_SOURCES:
        return ShopifyAppCreds(
            client_id=(settings.shopify_appstore_client_id or settings.shopify_client_id or "").strip(),
            client_secret=(settings.shopify_appstore_client_secret or settings.shopify_client_secret or "").strip(),
            redirect_uri=(settings.shopify_appstore_redirect_uri or settings.shopify_redirect_uri or "").strip(),
            scopes=(settings.shopify_appstore_scopes or settings.shopify_scopes or "").strip(),
            label="appstore",
        )
    return ShopifyAppCreds(
        client_id=(settings.shopify_headless_client_id or settings.shopify_client_id or "").strip(),
        client_secret=(settings.shopify_headless_client_secret or settings.shopify_client_secret or "").strip(),
        redirect_uri=(settings.shopify_headless_redirect_uri or settings.shopify_redirect_uri or "").strip(),
        scopes=(settings.shopify_headless_scopes or settings.shopify_scopes or "").strip(),
        label="headless",
    )


def _shopify_oauth_authorize_url(*, shop_domain: str, state: str, app: ShopifyAppCreds) -> str:
    if not app.client_id or not app.redirect_uri or not app.scopes:
        raise HTTPException(status_code=500, detail="Shopify OAuth is not configured")
    params = {
        "client_id": app.client_id,
        "scope": app.scopes,
        "redirect_uri": app.redirect_uri,
        "state": state,
    }
    return f"https://{shop_domain}/admin/oauth/authorize?{urlencode(params)}"


def _shopify_oauth_verify_hmac(*, request: Request, secret: str) -> bool:
    secret = (secret or "").strip()
    if not secret:
        return False
    qp = request.query_params
    received = qp.get("hmac") or ""
    if not received:
        return False
    items = [(k, v) for (k, v) in qp.multi_items() if k not in ("hmac", "signature")]
    items.sort(key=lambda kv: kv[0])
    message = "&".join([f"{k}={v}" for (k, v) in items])
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, received)


def _shopify_webhook_callback_base_url(request: Request) -> str:
    env = (os.getenv("SHOPIFY_WEBHOOK_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").strip()
    if env:
        return env.rstrip("/")
    redirect_uri = (settings.shopify_redirect_uri or "").strip()
    if redirect_uri:
        parsed = urlparse(redirect_uri)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return str(request.base_url).rstrip("/")


def _marketplace_merchant_id(platform: str, identity: str) -> str:
    digest = hashlib.sha256(f"{platform}:{identity}".encode("utf-8")).hexdigest()[:20]
    return f"merch_{platform}_{digest}"


def _append_query_params(url: str, params: Dict[str, Any]) -> str:
    clean_params = {
        key: value
        for key, value in params.items()
        if value is not None and str(value).strip() != ""
    }
    if not clean_params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(clean_params)}"


def _marketplace_install_success_url(platform: str) -> str:
    platform_key = platform.strip().upper()
    env_url = (
        os.getenv(f"{platform_key}_POST_INSTALL_REDIRECT_URL")
        or os.getenv("MARKETPLACE_POST_INSTALL_REDIRECT_URL")
        or ""
    ).strip()
    if env_url:
        return env_url
    return f"{settings.merchant_portal_base_url.rstrip('/')}{_MARKETPLACE_INSTALL_SUCCESS_PATH}"


async def _ensure_shopify_oauth_tables() -> None:
    """
    Best-effort DDL so app-store install routes work in environments where
    lightweight startup tasks have not created the OAuth state tables yet.
    """
    try:
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS shopify_oauth_states (
                state_sha256 VARCHAR(64) PRIMARY KEY,
                merchant_id VARCHAR(50) NOT NULL,
                shop_domain VARCHAR(255) NOT NULL,
                install_source VARCHAR(50),
                return_to TEXT,
                host TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                used_at TIMESTAMP WITH TIME ZONE
            )
            """
        )
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS shopify_install_tokens (
                jti_sha256 VARCHAR(64) PRIMARY KEY,
                merchant_id VARCHAR(50) NOT NULL,
                shop_domain VARCHAR(255) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                used_at TIMESTAMP WITH TIME ZONE,
                used_request_id TEXT
            )
            """
        )
    except Exception:
        logger.warning("Shopify OAuth table bootstrap failed", exc_info=True)
        return

    for ddl in (
        "ALTER TABLE shopify_oauth_states ADD COLUMN IF NOT EXISTS install_source VARCHAR(50)",
        "ALTER TABLE shopify_oauth_states ADD COLUMN IF NOT EXISTS return_to TEXT",
        "ALTER TABLE shopify_oauth_states ADD COLUMN IF NOT EXISTS host TEXT",
    ):
        try:
            await database.execute(ddl)
        except Exception:
            # Some local SQLite versions do not support ADD COLUMN IF NOT EXISTS.
            # Existing deployments with the old schema can still use the legacy JSON callback.
            logger.debug("Shopify OAuth state schema extension skipped: %s", ddl, exc_info=True)


async def _insert_shopify_oauth_state(
    *,
    state_sha256: str,
    merchant_id: str,
    shop_domain: str,
    expires_at: datetime,
    install_source: Optional[str] = None,
    return_to: Optional[str] = None,
    host: Optional[str] = None,
) -> None:
    await _ensure_shopify_oauth_tables()
    try:
        await database.execute(
            """
            INSERT INTO shopify_oauth_states
              (state_sha256, merchant_id, shop_domain, expires_at, install_source, return_to, host)
            VALUES
              (:state_sha256, :merchant_id, :shop_domain, :expires_at, :install_source, :return_to, :host)
            """,
            {
                "state_sha256": state_sha256,
                "merchant_id": merchant_id,
                "shop_domain": shop_domain,
                "expires_at": expires_at,
                "install_source": install_source,
                "return_to": return_to,
                "host": host,
            },
        )
    except Exception:
        logger.warning("Shopify OAuth state insert fell back to legacy schema", exc_info=True)
        await database.execute(
            """
            INSERT INTO shopify_oauth_states (state_sha256, merchant_id, shop_domain, expires_at)
            VALUES (:state_sha256, :merchant_id, :shop_domain, :expires_at)
            """,
            {
                "state_sha256": state_sha256,
                "merchant_id": merchant_id,
                "shop_domain": shop_domain,
                "expires_at": expires_at,
            },
        )


async def _lookup_shopify_marketplace_merchant(domain: str) -> Optional[str]:
    try:
        store_row = await database.fetch_one(
            """
            SELECT merchant_id
            FROM merchant_stores
            WHERE platform = 'shopify'
              AND lower(domain) = :domain
            ORDER BY connected_at DESC NULLS LAST
            LIMIT 1
            """,
            {"domain": domain.lower()},
        )
        if store_row and store_row.get("merchant_id"):
            return str(store_row["merchant_id"])
    except Exception:
        logger.debug("Shopify marketplace merchant store lookup failed domain=%s", domain, exc_info=True)

    try:
        onboarding_row = await database.fetch_one(
            """
            SELECT merchant_id
            FROM merchant_onboarding
            WHERE lower(coalesce(mcp_shop_domain, '')) = :domain
               OR lower(coalesce(store_url, '')) IN (:domain, :https_domain)
               OR lower(coalesce(website, '')) IN (:domain, :https_domain)
            ORDER BY created_at DESC NULLS LAST
            LIMIT 1
            """,
            {
                "domain": domain.lower(),
                "https_domain": f"https://{domain.lower()}",
            },
        )
        if onboarding_row and onboarding_row.get("merchant_id"):
            return str(onboarding_row["merchant_id"])
    except Exception:
        logger.debug("Shopify marketplace onboarding lookup failed domain=%s", domain, exc_info=True)

    return None


async def _ensure_shopify_marketplace_shell_merchant(domain: str) -> str:
    existing_merchant_id = await _lookup_shopify_marketplace_merchant(domain)
    if existing_merchant_id:
        return existing_merchant_id

    merchant_id = _marketplace_merchant_id("shopify", domain)
    digest = hashlib.sha256(f"shopify:{domain}".encode("utf-8")).hexdigest()[:12]
    await database.execute(
        """
        INSERT INTO merchant_onboarding (
            merchant_id,
            business_name,
            store_url,
            website,
            region,
            contact_email,
            auto_approved,
            approval_confidence,
            status,
            mcp_connected,
            mcp_platform,
            mcp_shop_domain,
            apm_enabled,
            created_at,
            updated_at
        )
        VALUES (
            :merchant_id,
            :business_name,
            :store_url,
            :website,
            'shopify',
            :contact_email,
            FALSE,
            0.0,
            'pending_verification',
            TRUE,
            'shopify',
            :domain,
            FALSE,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (merchant_id) DO UPDATE
        SET business_name = COALESCE(merchant_onboarding.business_name, EXCLUDED.business_name),
            store_url = EXCLUDED.store_url,
            website = EXCLUDED.website,
            mcp_connected = TRUE,
            mcp_platform = EXCLUDED.mcp_platform,
            mcp_shop_domain = EXCLUDED.mcp_shop_domain,
            updated_at = CURRENT_TIMESTAMP
        """,
        {
            "merchant_id": merchant_id,
            "business_name": f"Shopify Store {domain}",
            "store_url": f"https://{domain}",
            "website": f"https://{domain}",
            "contact_email": f"shopify-install+{digest}@pivota.invalid",
            "domain": domain,
        },
    )
    return merchant_id


async def _upsert_shopify_store_credentials(
    *,
    merchant_id: str,
    myshopify_domain: str,
    shop_name: str,
    access_token: str,
    storefront_token: Optional[str],
    webhook_secret: Optional[str] = None,
    install_source: Optional[str] = None,
) -> str:
    existing = await database.fetch_one(
        """
        SELECT store_id, api_key
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND platform = 'shopify'
          AND domain = :domain
        """,
        {"merchant_id": merchant_id, "domain": myshopify_domain},
    )

    existing_row = dict(existing) if existing else None

    existing_creds: Dict[str, Any] = {}
    if existing_row and (existing_row.get("api_key") or ""):
        try:
            parsed = json.loads(existing_row.get("api_key") or "")
            if isinstance(parsed, dict):
                existing_creds = parsed
        except Exception:
            existing_creds = {}

    token_blob: Dict[str, Any] = {"access_token": access_token}
    if webhook_secret:
        token_blob["webhook_secret"] = webhook_secret
    if storefront_token:
        token_blob["storefront_access_token"] = storefront_token
    if install_source:
        token_blob["install_source"] = install_source
    # Preserve prior storefront token if we didn't re-create one.
    if not token_blob.get("storefront_access_token"):
        stored = (
            existing_creds.get("storefront_access_token")
            if isinstance(existing_creds.get("storefront_access_token"), str)
            else None
        ) or (existing_creds.get("storefront_token") if isinstance(existing_creds.get("storefront_token"), str) else None)
        if stored and str(stored).strip():
            token_blob["storefront_access_token"] = str(stored).strip()

    token_blob["installed_at"] = datetime.now(timezone.utc).isoformat()
    token_json = json.dumps(token_blob, ensure_ascii=False)

    if existing_row:
        await database.execute(
            """
            UPDATE merchant_stores
            SET name = :name,
                domain = :domain,
                api_key = :api_key,
                status = 'active',
                connected_at = CURRENT_TIMESTAMP,
                last_sync = CURRENT_TIMESTAMP
            WHERE store_id = :store_id
            """,
            {
                "store_id": existing_row["store_id"],
                "name": shop_name,
                "domain": myshopify_domain,
                "api_key": token_json,
            },
        )
        return str(existing_row["store_id"])

    store_id = f"store_{merchant_id[:8]}_{int(datetime.now().timestamp())}"
    await database.execute(
        """
        INSERT INTO merchant_stores
          (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
        VALUES
          (:store_id, :merchant_id, 'shopify', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)
        """,
        {
            "store_id": store_id,
            "merchant_id": merchant_id,
            "domain": myshopify_domain,
            "name": shop_name,
            "api_key": token_json,
        },
    )
    return store_id


@router.get("/shopify/oauth/start")
async def shopify_oauth_start(
    shop: str = Query(..., description="Shop domain, e.g. your-shop.myshopify.com"),
    merchant_id: Optional[str] = Query(None, description="Merchant id (employee/admin only)"),
    redirect: bool = Query(False, description="If true, 302 redirect to Shopify"),
    current_user: dict = Depends(get_current_user),
):
    """
    Start Shopify OAuth install.
    Requires a Pivota JWT (merchant/employee/admin).
    """
    if current_user.get("role") not in ["merchant", "employee", "admin", "super_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = (merchant_id or "").strip() or (current_user.get("merchant_id") or "").strip()
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    if current_user.get("role") == "merchant" and current_user.get("merchant_id") != target_merchant_id:
        raise HTTPException(status_code=403, detail="Can only connect your own store")

    shop_domain = _validate_myshopify_domain(shop)

    state = secrets.token_urlsafe(32)
    state_sha = hashlib.sha256(state.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_SHOPIFY_OAUTH_STATE_TTL_SECONDS)

    await _insert_shopify_oauth_state(
        state_sha256=state_sha,
        merchant_id=target_merchant_id,
        shop_domain=shop_domain,
        expires_at=expires_at,
        install_source="merchant_portal",
    )

    url = _shopify_oauth_authorize_url(
        shop_domain=shop_domain, state=state, app=resolve_shopify_app("merchant_portal")
    )
    if redirect:
        return RedirectResponse(url=url, status_code=302)
    return {
        "status": "success",
        "merchant_id": target_merchant_id,
        "shop_domain": shop_domain,
        "authorization_url": url,
        "state_sha256_prefix": state_sha[:10],
        "expires_in_seconds": _SHOPIFY_OAUTH_STATE_TTL_SECONDS,
    }


@router.get("/shopify/app")
@router.get("/shopify/install")
async def shopify_app_store_install(
    request: Request,
    shop: str = Query(..., description="Shop domain provided by Shopify, e.g. your-shop.myshopify.com"),
    host: Optional[str] = Query(None),
    embedded: Optional[str] = Query(None),
    redirect: bool = Query(True, description="If true, 302 redirect to Shopify OAuth"),
):
    """
    Public Shopify App Store entrypoint.
    Shopify calls this without a Pivota JWT, so we bind OAuth state to an
    existing merchant for that shop when present or create a shell merchant.
    """
    shop_domain = _validate_myshopify_domain(shop)
    merchant_id = await _ensure_shopify_marketplace_shell_merchant(shop_domain)

    state = secrets.token_urlsafe(32)
    state_sha = hashlib.sha256(state.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_SHOPIFY_OAUTH_STATE_TTL_SECONDS)
    return_to = _marketplace_install_success_url("shopify")

    await _insert_shopify_oauth_state(
        state_sha256=state_sha,
        merchant_id=merchant_id,
        shop_domain=shop_domain,
        expires_at=expires_at,
        install_source="app_store",
        return_to=return_to,
        host=host,
    )

    # App Store distribution: request the reduced, read-only / merchant-tool scope
    # set. write_orders (PSP -> Shopify order creation) is intentionally excluded
    # here and kept only for non-App-Store (custom/headless) installs.
    url = _shopify_oauth_authorize_url(
        shop_domain=shop_domain, state=state, app=resolve_shopify_app("app_store")
    )
    if redirect:
        return RedirectResponse(url=url, status_code=302)
    return {
        "status": "success",
        "merchant_id": merchant_id,
        "shop_domain": shop_domain,
        "install_source": "app_store",
        "authorization_url": url,
        "state_sha256_prefix": state_sha[:10],
        "expires_in_seconds": _SHOPIFY_OAUTH_STATE_TTL_SECONDS,
        "host_present": bool(host),
        "embedded": embedded,
    }


@router.get("/shopify/oauth/callback")
async def shopify_oauth_callback(request: Request):
    """
    Shopify OAuth callback (unauthenticated; validated via HMAC + state anti-replay).
    """
    shop_domain = _validate_myshopify_domain(request.query_params.get("shop") or "")
    code = (request.query_params.get("code") or "").strip()
    state = (request.query_params.get("state") or "").strip()
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing required OAuth params")

    # Look up the OAuth state FIRST (read-only, by state hash) so we know which
    # Shopify app this install belongs to, then verify HMAC + exchange the token
    # with that app's credentials (App A = public/app_store, App B = headless).
    await _ensure_shopify_oauth_tables()
    state_sha = hashlib.sha256(state.encode("utf-8")).hexdigest()
    try:
        state_row = await database.fetch_one(
            """
            SELECT merchant_id, shop_domain, expires_at, used_at, install_source, return_to, host
            FROM shopify_oauth_states
            WHERE state_sha256 = :state_sha256
            """,
            {"state_sha256": state_sha},
        )
    except Exception:
        logger.warning("Shopify OAuth state lookup fell back to legacy schema", exc_info=True)
        state_row = await database.fetch_one(
            """
            SELECT merchant_id, shop_domain, expires_at, used_at
            FROM shopify_oauth_states
            WHERE state_sha256 = :state_sha256
            """,
            {"state_sha256": state_sha},
        )
    if not state_row:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state (reason=state_not_found)")
    state_row = dict(state_row)

    install_source = (state_row.get("install_source") or "").strip()
    app = resolve_shopify_app(install_source)
    # Fall back to the legacy single secret if the resolved app isn't configured
    # (keeps existing installs working before SHOPIFY_HEADLESS_* is set).
    app_secret = app.client_secret or (settings.shopify_client_secret or "").strip()

    if not _shopify_oauth_verify_hmac(request=request, secret=app_secret):
        raise HTTPException(status_code=401, detail="Invalid Shopify OAuth signature")

    if state_row.get("used_at"):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state (reason=state_already_used)")
    expires_at = state_row.get("expires_at")
    if expires_at and expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state (reason=state_expired)")

    merchant_id = str(state_row["merchant_id"])
    stored_shop_domain = (state_row.get("shop_domain") or "").strip().lower()
    return_to = (state_row.get("return_to") or "").strip()

    token_url = f"https://{shop_domain}/admin/oauth/access_token"
    token_payload = {
        "client_id": app.client_id or (settings.shopify_client_id or "").strip(),
        "client_secret": app_secret,
        "code": code,
    }
    if not token_payload["client_id"] or not token_payload["client_secret"]:
        raise HTTPException(status_code=500, detail="Shopify OAuth is not configured")

    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(token_url, json=token_payload)
    if token_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to exchange Shopify access token")
    token_data = token_resp.json() or {}
    access_token = (token_data.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="Shopify token response missing access_token")

    # Fetch canonical shop info (myshopify_domain + name).
    async with httpx.AsyncClient(timeout=12.0) as client:
        shop_resp = await client.get(
            f"https://{shop_domain}/admin/api/2024-07/shop.json",
            headers={"X-Shopify-Access-Token": access_token},
        )
    if shop_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Shopify token verification failed")
    shop_json = shop_resp.json() or {}
    shop_info = shop_json.get("shop") if isinstance(shop_json, dict) else None
    if not isinstance(shop_info, dict):
        raise HTTPException(status_code=400, detail="Invalid Shopify shop response")

    canonical_myshopify_domain = (shop_info.get("myshopify_domain") or shop_domain).strip().lower()
    shop_name = (shop_info.get("name") or canonical_myshopify_domain).strip()
    if stored_shop_domain and stored_shop_domain not in {canonical_myshopify_domain, shop_domain}:
        raise HTTPException(status_code=400, detail="OAuth shop mismatch (reason=shop_domain_mismatch)")

    consumed = await database.fetch_one(
        """
        UPDATE shopify_oauth_states
        SET used_at = NOW()
        WHERE state_sha256 = :state_sha256
          AND used_at IS NULL
          AND expires_at > NOW()
        RETURNING merchant_id
        """,
        {"state_sha256": state_sha},
    )
    if not consumed:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state (reason=state_consumption_failed)")

    # Optional Storefront token: best-effort create so quotes/checkout can work.
    storefront_token = await _create_storefront_access_token_best_effort(
        shop_domain=canonical_myshopify_domain,
        access_token=access_token,
    )

    store_id = await _upsert_shopify_store_credentials(
        merchant_id=merchant_id,
        myshopify_domain=canonical_myshopify_domain,
        shop_name=shop_name,
        access_token=access_token,
        storefront_token=storefront_token,
        # Persist the OWNING app's secret so webhook HMAC verification uses the
        # right app's secret per store (App A vs App B in the dual-app setup).
        webhook_secret=(app.client_secret or None),
        install_source=install_source or None,
    )

    # Register required webhooks right after OAuth.
    webhooks_report: Dict[str, Any] = {"attempted": False}
    try:
        from services.shopify_integration_verify import register_webhooks_best_effort

        callback_base_url = _shopify_webhook_callback_base_url(request)
        webhooks_report = {
            "attempted": True,
            "callback_base_url": callback_base_url,
            **(
                await register_webhooks_best_effort(
                    shop_domain=canonical_myshopify_domain,
                    access_token=access_token,
                    merchant_id=merchant_id,
                    callback_base_url=callback_base_url,
                    topics=list(_SHOPIFY_OAUTH_REQUIRED_WEBHOOK_TOPICS),
                    api_version="2024-07",
                )
            ),
        }
    except Exception as e:
        logger.warning("Shopify webhook registration failed merchant=%s shop=%s err=%s", merchant_id, canonical_myshopify_domain, str(e)[:200])
        webhooks_report = {"attempted": True, "error": "webhook_registration_failed"}

    access_token_fp = hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:10]
    payload = {
        "status": "success",
        "merchant_id": merchant_id,
        "shop_domain": canonical_myshopify_domain,
        "store_id": store_id,
        "access_token_sha256_prefix": access_token_fp,
        "storefront_token_present": bool(storefront_token),
        "webhooks": webhooks_report,
    }
    # ALWAYS redirect the browser to a real UI after OAuth — never return raw
    # JSON. The callback is hit by Shopify's OAuth redirect (a browser), so a
    # JSON body renders as a raw/pretty-printed page, which Shopify review flags
    # as a display error (2.1.1). This applies to every install source: App
    # Store installs (app_store) and portal "Connect with Shopify"
    # (merchant_portal) alike. The JSON payload is kept only for structured
    # logging below.
    logger.info(
        "shopify_oauth_callback success merchant=%s shop=%s store=%s source=%s payload=%s",
        merchant_id, canonical_myshopify_domain, store_id, install_source or "?", payload,
    )
    redirect_target = return_to or _marketplace_install_success_url("shopify")
    return RedirectResponse(
        url=_append_query_params(
            redirect_target,
            {
                "installed": "shopify",
                "merchant_id": merchant_id,
                "shop": canonical_myshopify_domain,
                "store_id": store_id,
                "status": "success",
            },
        ),
        status_code=302,
    )


class ShopifySyncRequest(BaseModel):
    merchant_id: Optional[str] = None


class VerifyShopifyIntegrationRequest(BaseModel):
    merchant_id: str
    callback_base_url: str
    api_version: Optional[str] = None


@router.get("/shopify/token/diagnostic")
async def shopify_token_diagnostic(
    merchant_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """
    Diagnostic: verify Shopify token validity and required scopes without leaking secrets.
    """
    if current_user.get("role") not in ["merchant", "employee", "admin", "super_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = (merchant_id or "").strip() or (current_user.get("merchant_id") or "").strip()
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    if current_user.get("role") == "merchant" and current_user.get("merchant_id") != target_merchant_id:
        raise HTTPException(status_code=403, detail="Can only access your own merchant")

    store = await get_primary_store(target_merchant_id)
    if not store or (store.get("platform") or "").lower() != "shopify":
        raise HTTPException(status_code=400, detail="No Shopify store connected")

    shop_domain = (store.get("domain") or store.get("shop_domain") or "").strip().lower()
    access_token, token_meta = await resolve_shopify_admin_access_token(
        shop_domain=shop_domain,
        api_key_raw=store.get("api_key_raw") or store.get("api_key"),
        store_id=str(store.get("store_id") or "").strip() or None,
    )
    if not access_token:
        creds = store.get("api_credentials") if isinstance(store.get("api_credentials"), dict) else {}
        access_token = str(creds.get("access_token") or "").strip() or None
        token_meta = {"has_client_credentials": bool(creds.get("client_id") and creds.get("client_secret")), "refreshed": False}
    if not shop_domain or not access_token:
        raise HTTPException(status_code=400, detail="Missing Shopify credentials")

    scopes_status: Optional[int] = None
    shop_status: Optional[int] = None
    scopes: list[str] = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        scopes_resp = await client.get(
            f"https://{shop_domain}/admin/oauth/access_scopes.json",
            headers={"X-Shopify-Access-Token": access_token},
        )
        scopes_status = scopes_resp.status_code
        if scopes_status == 200:
            data = scopes_resp.json() or {}
            scopes = [str(s.get("handle")) for s in (data.get("access_scopes") or []) if s.get("handle")]

        shop_resp = await client.get(
            f"https://{shop_domain}/admin/api/2024-07/shop.json",
            headers={"X-Shopify-Access-Token": access_token},
        )
        shop_status = shop_resp.status_code

    scope_set = set(scopes)
    required_scopes = [s.strip() for s in (settings.shopify_scopes or "").split(",") if s.strip()]
    required_scope_set = set(required_scopes)
    missing_required_scopes: list[str] = []
    if required_scope_set:
        if scopes_status == 200:
            missing_required_scopes = sorted(required_scope_set.difference(scope_set))
        else:
            # If we can't read scopes (auth failed, etc.), treat as unknown and surface the requirement list.
            missing_required_scopes = sorted(required_scope_set)
    return {
        "status": "success",
        "merchant_id": target_merchant_id,
        "shop_domain": shop_domain,
        "auth_ok": shop_status == 200,
        "shop_status_code": shop_status,
        "scopes_status_code": scopes_status,
        "required_scopes": sorted(required_scope_set),
        "missing_required_scopes": missing_required_scopes,
        "scope_summary": {
            "read_products": "read_products" in scope_set,
            "read_orders": "read_orders" in scope_set,
            "read_discounts": "read_discounts" in scope_set,
            "read_fulfillments": "read_fulfillments" in scope_set,
            "read_customers": "read_customers" in scope_set,
            "write_orders": "write_orders" in scope_set,
            "write_webhooks": "write_webhooks" in scope_set,
        },
        "scope_count": len(scope_set),
        "token_refresh": {
            "has_client_credentials": bool((token_meta or {}).get("has_client_credentials")),
            "refreshed": bool((token_meta or {}).get("refreshed")),
            "refresh_error": (token_meta or {}).get("refresh_error"),
        },
    }


class ShopifyWebhookEventOut(BaseModel):
    id: int
    merchant_id: str
    shop_domain: Optional[str] = None
    topic: str
    webhook_id: Optional[str] = None
    idempotency_key: str
    signature_verified: bool
    received_at: Optional[str] = None
    occurred_at: Optional[str] = None
    payload_sha256: str
    prev_chain_hash: Optional[str] = None
    chain_hash: str


class ConnectWixRequest(BaseModel):
    merchant_id: str
    site_id: str
    api_key: str
    store_name: Optional[str] = None


@router.get("/wix/oauth/start")
async def wix_oauth_start_stub(
    merchant_id: str = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """Stub for Wix App OAuth onboarding.

    TODO(PR-10b follow-up): register the Wix app, request Manage Orders
    permission, exchange the app instance for an access token using
    WIX_APP_CLIENT_ID / WIX_APP_CLIENT_SECRET, and persist:
      {"access_token": "<bearer token>", "site_id": "<wix site id>"}
    in merchant_stores.api_key.
    """
    if current_user["role"] == "merchant" and current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Can only connect your own store")
    raise HTTPException(
        status_code=501,
        detail={
            "error": "wix_oauth_not_configured",
            "message": "Wix App OAuth requires registered Wix app credentials before live onboarding can run.",
            "required_env": ["WIX_APP_CLIENT_ID", "WIX_APP_CLIENT_SECRET"],
            "expected_store_credentials": {
                "access_token": "stored Wix OAuth bearer token",
                "site_id": "Wix site id",
            },
            "env_configured": {
                "WIX_APP_CLIENT_ID": bool(os.getenv("WIX_APP_CLIENT_ID")),
                "WIX_APP_CLIENT_SECRET": bool(os.getenv("WIX_APP_CLIENT_SECRET")),
            },
        },
    )


@router.get("/wix/oauth/callback")
async def wix_oauth_callback_stub():
    """Stub callback for the future Wix App OAuth handshake."""
    raise HTTPException(
        status_code=501,
        detail={
            "error": "wix_oauth_not_configured",
            "message": "Wix OAuth callback is stubbed until Wix developer app credentials are available.",
            "required_env": ["WIX_APP_CLIENT_ID", "WIX_APP_CLIENT_SECRET"],
        },
    )


class ConnectWooCommerceRequest(BaseModel):
    merchant_id: str
    store_url: str
    consumer_key: str
    consumer_secret: str


class ConnectBigCommerceRequest(BaseModel):
    merchant_id: str
    store_hash: str
    access_token: str
    client_id: Optional[str] = None


class ConnectPrestaShopRequest(BaseModel):
    merchant_id: str
    store_url: str
    api_key: str


class UpdateStoreSupportEmailRequest(BaseModel):
    merchant_id: Optional[str] = None
    support_email: Optional[str] = None


class GetStoreSupportEmailResponse(BaseModel):
    status: str
    merchant_id: str
    store_id: str
    support_email: Optional[str] = None
    effective_support_email: Optional[str] = None


@router.post("/shopify/connect")
async def merchant_connect_shopify(
    request: ConnectShopifyRequest,
    http_request: Request,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their Shopify store"""
    # Allow merchant, employee, or admin
    if current_user["role"] not in ["merchant", "employee", "admin", "super_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # If merchant role, verify they can only connect their own store
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        # Validate shop domain and credentials
        if not request.shop_domain or not request.shop_domain.strip():
            raise HTTPException(status_code=400, detail="Shop domain is required")

        provided_access_token = (request.access_token or "").strip()
        provided_client_id = (request.client_id or "").strip()
        provided_client_secret = (request.client_secret or "").strip()

        if not provided_access_token and not (provided_client_id and provided_client_secret):
            raise HTTPException(
                status_code=400,
                detail="Either access_token or client_id+client_secret is required",
            )
        if bool(provided_client_id) != bool(provided_client_secret):
            raise HTTPException(
                status_code=400,
                detail="client_id and client_secret must be provided together",
            )

        effective_access_token = provided_access_token
        exchanged_expires_in: Optional[int] = None
        if not effective_access_token:
            exchanged_token, exchanged_expires_in, exchange_error = await exchange_shopify_client_credentials_token(
                shop_domain=request.shop_domain,
                client_id=provided_client_id,
                client_secret=provided_client_secret,
            )
            if not exchanged_token:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to obtain Shopify access token via client credentials ({exchange_error})",
                )
            effective_access_token = exchanged_token

        # Test Shopify API connection
        test_url = f"https://{request.shop_domain}/admin/api/2024-07/shop.json"
        headers = {"X-Shopify-Access-Token": effective_access_token}
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            test_response = await client.get(test_url, headers=headers)
        
        if test_response.status_code != 200:
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid Shopify credentials. API returned: {test_response.status_code}"
            )
        
        # Verify shop data
        shop_data = test_response.json()
        if not shop_data.get("shop"):
            raise HTTPException(status_code=400, detail="Invalid Shopify response")
        
        shop_info = shop_data["shop"]
        canonical_myshopify_domain = (shop_info.get("myshopify_domain") or request.shop_domain or "").strip().lower()
        logger.info(f"✅ Shopify credentials verified for {canonical_myshopify_domain}")

        # Storefront token strategy:
        # 1) accept from request (optional)
        # 2) preserve prior stored token (so merchants can reconnect without re-entering)
        # 3) best-effort auto-create using Admin token (to simplify merchant UX)
        storefront_token_raw = (request.storefront_access_token or request.storefront_token or "").strip()
        storefront_token_from_request = bool(storefront_token_raw)
        storefront_token = storefront_token_raw or None
        storefront_token_created = False
        storefront_token_verified = None

        existing = await database.fetch_one(
            """SELECT store_id, api_key FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'shopify' 
               AND (domain = :domain_input OR domain = :domain_canonical)""",
            {
                "merchant_id": request.merchant_id,
                "domain_input": request.shop_domain,
                "domain_canonical": canonical_myshopify_domain,
            },
        )

        existing_creds: Dict[str, Any] = {}
        if existing:
            # databases.fetch_one returns a Record; normalize to dict for .get access
            existing = dict(existing)
        if existing and (existing.get("api_key") or ""):
            try:
                parsed = json.loads(existing.get("api_key") or "")
                if isinstance(parsed, dict):
                    existing_creds = parsed
            except Exception:
                existing_creds = {}

        # Preserve client credentials for 24h token refresh flows.
        effective_client_id = provided_client_id or (
            str(existing_creds.get("client_id") or "").strip() if isinstance(existing_creds, dict) else ""
        )
        effective_client_secret = provided_client_secret or (
            str(existing_creds.get("client_secret") or "").strip() if isinstance(existing_creds, dict) else ""
        )
        if not (effective_client_id and effective_client_secret):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Shopify now requires client_id+client_secret for automatic token refresh. "
                    "Please reconnect with client credentials."
                ),
            )

        if not storefront_token:
            stored = (
                (
                    existing_creds.get("storefront_access_token")
                    if isinstance(existing_creds.get("storefront_access_token"), str)
                    else None
                )
                or (existing_creds.get("storefront_token") if isinstance(existing_creds.get("storefront_token"), str) else None)
                or (
                    existing_creds.get("storefrontAccessToken")
                    if isinstance(existing_creds.get("storefrontAccessToken"), str)
                    else None
                )
            )
            storefront_token = stored.strip() if isinstance(stored, str) and stored.strip() else None

        if not storefront_token:
            auto = await _create_storefront_access_token_best_effort(
                shop_domain=canonical_myshopify_domain,
                access_token=effective_access_token,
            )
            if auto:
                storefront_token = auto
                storefront_token_created = True

        if storefront_token:
            try:
                sf_url = f"https://{canonical_myshopify_domain}/api/2024-07/graphql.json"
                sf_payload = {"query": "query { shop { name } }"}
                async with httpx.AsyncClient(timeout=8.0) as client:
                    sf_resp = await client.post(
                        sf_url,
                        headers={
                            "X-Shopify-Storefront-Access-Token": storefront_token,
                            "Content-Type": "application/json",
                        },
                        json=sf_payload,
                    )
                if sf_resp.status_code != 200:
                    if storefront_token_from_request:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Invalid Shopify Storefront token. API returned: {sf_resp.status_code}",
                        )

                    storefront_token_verified = False
                    storefront_token = None

                    # If stored token is invalid, try to auto-create a fresh one (best-effort).
                    if not storefront_token_created:
                        auto2 = await _create_storefront_access_token_best_effort(
                            shop_domain=canonical_myshopify_domain,
                            access_token=effective_access_token,
                        )
                        if auto2:
                            try:
                                async with httpx.AsyncClient(timeout=8.0) as client:
                                    sf_resp2 = await client.post(
                                        sf_url,
                                        headers={
                                            "X-Shopify-Storefront-Access-Token": auto2,
                                            "Content-Type": "application/json",
                                        },
                                        json=sf_payload,
                                    )
                                if sf_resp2.status_code == 200:
                                    storefront_token = auto2
                                    storefront_token_created = True
                                    storefront_token_verified = True
                            except Exception:
                                pass
                else:
                    storefront_token_verified = True
            except HTTPException:
                raise
            except Exception:
                storefront_token_verified = None

        token_blob: Dict[str, Any] = {"access_token": effective_access_token}
        if effective_client_id and effective_client_secret:
            token_blob["client_id"] = effective_client_id
            token_blob["client_secret"] = effective_client_secret
            token_blob["access_token_issued_at"] = datetime.now(timezone.utc).isoformat()
            if exchanged_expires_in and exchanged_expires_in > 0:
                token_blob["access_token_expires_in"] = int(exchanged_expires_in)
                token_blob["access_token_expires_at"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=int(exchanged_expires_in))
                ).isoformat()
        webhook_secret = (request.webhook_secret or "").strip()
        if webhook_secret:
            token_blob["webhook_secret"] = webhook_secret
        if storefront_token:
            token_blob["storefront_access_token"] = storefront_token
        token_json = json.dumps(token_blob, ensure_ascii=False)
        
        if existing:
            # Update existing store - store token as JSON for consistency
            await database.execute(
                """UPDATE merchant_stores 
                   SET domain = :domain,
                       api_key = :token,
                       status = 'active',
                       connected_at = CURRENT_TIMESTAMP,
                       last_sync = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {
                    "domain": canonical_myshopify_domain,
                    "token": token_json,
                    "store_id": existing["store_id"],
                }
            )
            store_id = existing["store_id"]
        else:
            # Create new store record
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'shopify', :domain, :name, :token, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "domain": canonical_myshopify_domain,
                    "name": shop_info.get("name", canonical_myshopify_domain),
                    "token": token_json,
                }
            )

        try:
            await database.execute(
                """
                UPDATE merchant_stores
                SET is_primary = TRUE
                WHERE store_id = :store_id
                  AND merchant_id = :merchant_id
                  AND NOT EXISTS (
                    SELECT 1
                    FROM merchant_stores
                    WHERE merchant_id = :merchant_id
                      AND store_id != :store_id
                      AND is_primary = TRUE
                      AND lower(COALESCE(status, '')) IN ('active', 'connected')
                  )
                """,
                {"store_id": store_id, "merchant_id": request.merchant_id},
            )
        except Exception:
            pass

        # Prevent stale recommendations/checkout attempts after reconnecting a Shopify store:
        # cached product IDs from a previous shop can linger until TTL, which breaks pricing/checkout.
        try:
            await database.execute(
                """
                UPDATE products_cache
                SET expires_at = NOW()
                WHERE merchant_id = :merchant_id
                  AND platform = 'shopify'
                  AND (expires_at IS NULL OR expires_at > NOW())
                """,
                {"merchant_id": request.merchant_id},
            )
        except Exception:
            pass
        
        # Legacy MCP fields have been migrated to merchant_stores table
        # No need to update merchant_onboarding anymore

        # Register the required order/uninstall webhooks now that credentials are
        # stored. The custom-token connect path (App B / write-tier) is the ONLY
        # path that holds write_webhooks, so this is the mainline for conversion
        # closure — historically it was NEVER registered here, so orders/paid was
        # left unsubscribed for every custom-token store (audit fix #1). We do NOT
        # fail the connect if registration fails (credential storage already
        # succeeded and is useful), but the gap MUST be impossible to miss: the
        # full report is returned and, when orders/paid is not subscribed, we log
        # at ERROR with the failure bodies. This is deliberately NOT silent
        # best-effort — no fallback-as-pass.
        webhooks_report: Dict[str, Any] = {"attempted": False}
        orders_paid_subscribed = False
        try:
            from services.shopify_integration_verify import register_webhooks_best_effort

            callback_base_url = _shopify_webhook_callback_base_url(http_request)
            report = await register_webhooks_best_effort(
                shop_domain=canonical_myshopify_domain,
                access_token=effective_access_token,
                merchant_id=request.merchant_id,
                callback_base_url=callback_base_url,
                topics=list(_SHOPIFY_OAUTH_REQUIRED_WEBHOOK_TOPICS),
                api_version="2024-07",
            )
            webhooks_report = {"attempted": True, "callback_base_url": callback_base_url, **report}
            # `created` is a list of {"topic", "webhook_id"}; `already_exists` is a list of topic strings.
            created_topics = {(c or {}).get("topic") for c in (report.get("created") or [])}
            already_topics = set(report.get("already_exists") or [])
            orders_paid_subscribed = ("orders/paid" in created_topics) or ("orders/paid" in already_topics)
        except Exception as e:
            logger.error(
                "Shopify webhook registration raised during connect merchant=%s shop=%s err=%s",
                request.merchant_id,
                canonical_myshopify_domain,
                str(e)[:300],
            )
            webhooks_report = {"attempted": True, "error": "webhook_registration_failed", "detail": str(e)[:300]}

        if not orders_paid_subscribed:
            logger.error(
                "Shopify connect: orders/paid NOT subscribed merchant=%s shop=%s report=%s",
                request.merchant_id,
                canonical_myshopify_domain,
                json.dumps(webhooks_report, ensure_ascii=False, default=str)[:1500],
            )

        return {
            "status": "success",
            "message": "Shopify store connected successfully",
            "store_id": store_id,
            "shop_name": shop_info.get("name"),
            "shop_domain": canonical_myshopify_domain,
            "storefront_token_present": bool(storefront_token),
            "storefront_token_verified": storefront_token_verified,
            "storefront_token_created": storefront_token_created,
            "webhooks_ok": orders_paid_subscribed,
            "webhooks": webhooks_report,
            "warning": None
            if storefront_token
            else "Storefront token missing: enable Storefront API for the Shopify custom app so Pivota can auto-generate it, or have support add it later.",
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting Shopify: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect Shopify: {str(e)}")


@router.post("/shopify/verify")
async def merchant_verify_shopify_integration(
    request: VerifyShopifyIntegrationRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Verify Shopify integration at onboarding time:
    - token validity + canonical myshopify domain
    - access scopes (REST access_scopes)
    - webhook registration (best-effort)
    - policies snapshot (best-effort)
    - capability probes (Shopify Payments / Returns)
    Persists a snapshot to pcs_merchant_capabilities when available.
    """
    if current_user["role"] not in ["merchant", "employee", "admin", "super_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    if current_user["role"] == "merchant" and current_user.get("merchant_id") != request.merchant_id:
        raise HTTPException(status_code=403, detail="Can only verify your own store")

    if not request.callback_base_url or not request.callback_base_url.strip():
        raise HTTPException(status_code=400, detail="callback_base_url is required")

    try:
        from services.shopify_integration_verify import verify_shopify_integration

        report = await verify_shopify_integration(
            merchant_id=request.merchant_id,
            callback_base_url=request.callback_base_url,
            api_version=request.api_version or "2024-07",
        )
        return {"status": "success", "report": report}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Shopify integration verify failed merchant={request.merchant_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to verify Shopify integration")


@router.get("/shopify/webhooks/events")
async def list_shopify_webhook_events(
    merchant_id: Optional[str] = None,
    limit: int = 20,
    current_user: dict = Depends(get_current_user),
):
    """
    Read-only debug: list latest ingested Shopify webhook events for a merchant.
    Does NOT return payload_json (to avoid leaking PII).
    """
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = merchant_id or current_user.get("merchant_id")
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    if current_user["role"] == "merchant" and current_user.get("merchant_id") != target_merchant_id:
        raise HTTPException(status_code=403, detail="Can only access your own merchant")

    safe_limit = max(1, min(int(limit or 20), 200))

    try:
        rows = await database.fetch_all(
            """
            SELECT
              id,
              merchant_id,
              shop_domain,
              topic,
              webhook_id,
              idempotency_key,
              signature_verified,
              received_at,
              occurred_at,
              payload_sha256,
              prev_chain_hash,
              chain_hash
            FROM pcs_shopify_webhook_events
            WHERE merchant_id = :merchant_id
            ORDER BY received_at DESC
            LIMIT :limit
            """,
            {"merchant_id": target_merchant_id, "limit": safe_limit},
        )
    except Exception as e:
        logger.error(f"Failed to query pcs_shopify_webhook_events merchant={target_merchant_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to load webhook events")

    events = []
    for row in rows:
        d = dict(row)
        # ISO stringify datetimes for JSON stability (FastAPI will also handle, but be explicit)
        for k in ("received_at", "occurred_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        events.append(d)

    return {"status": "success", "merchant_id": target_merchant_id, "events": events}


@router.post("/shopify/products/sync")
async def merchant_sync_shopify_products(
    request: ShopifySyncRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    """
    Sync Shopify products for a merchant.
    Mirrors /merchant/integrations/shopify/sync so legacy front-ends keep working.
    """
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = request.merchant_id or current_user.get("merchant_id")
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    if current_user["role"] == "merchant" and current_user.get("merchant_id") != target_merchant_id:
        raise HTTPException(status_code=403, detail="Can only sync your own store")

    store_row = await database.fetch_one(
        """
        SELECT store_id, platform, domain, status
        FROM merchant_stores
        WHERE merchant_id = :merchant_id AND platform = 'shopify' AND status IN ('active', 'connected')
        ORDER BY connected_at DESC NULLS LAST
        LIMIT 1
        """,
        {"merchant_id": target_merchant_id}
    )

    if not store_row:
        raise HTTPException(
            status_code=400,
            detail="No Shopify store connected. Please connect your store first in Integrations."
        )

    store = dict(store_row)
    if (store.get("status") or "").lower() != "active":
        raise HTTPException(
            status_code=400,
            detail=f"Store is {store.get('status')}. Please reconnect your store."
        )

    # Call Shopify adapter directly
    try:
        import json
        from adapters.product_adapters import ShopifyProductAdapter
        from db.products import upsert_product_cache
        
        # Get credentials from merchant_stores
        cred_row = await database.fetch_one(
            "SELECT api_key FROM merchant_stores WHERE store_id = :store_id",
            {"store_id": store["store_id"]}
        )
        api_key_raw = cred_row["api_key"] if cred_row else None
        access_token, token_meta = await resolve_shopify_admin_access_token(
            shop_domain=store.get("domain"),
            api_key_raw=api_key_raw,
            store_id=str(store.get("store_id") or "").strip() or None,
        )
        
        if not access_token:
            raise HTTPException(status_code=400, detail="Shopify access token not found")
        if (token_meta or {}).get("refreshed"):
            logger.info(
                "Shopify admin token refreshed before legacy sync endpoint",
                extra={
                    "merchant_id": target_merchant_id,
                    "store_id": store.get("store_id"),
                    "shop_domain": store.get("domain"),
                },
            )
        
        # Fetch products from Shopify API (paginated).
        synced_count = 0
        page_info = None
        pages_fetched = 0
        max_pages = 40  # Safety cap (40 * 250 = 10,000 products)

        while pages_fetched < max_pages:
            products, next_page, error = await ShopifyProductAdapter.fetch_products(
                shop_domain=store["domain"],
                access_token=access_token,
                merchant_id=target_merchant_id,
                limit=250,
                page_info=page_info,
            )

            if error:
                raise HTTPException(status_code=400, detail=f"Shopify API error: {error}")

            if not products:
                break

            pages_fetched += 1

            # Cache products
            for product in products:
                try:
                    product_data = json.loads(product.json())
                    await upsert_product_cache(
                        merchant_id=target_merchant_id,
                        platform="shopify",
                        platform_product_id=product.id,
                        product_data=product_data,
                        ttl_seconds=604800  # 7 days
                    )
                    synced_count += 1
                except Exception as cache_err:
                    logger.error(f"Failed to cache product {product.id}: {cache_err}")
                    continue

            # Stop if there's no next page token.
            if not next_page:
                break

            # Defensive: adapter sometimes returns a sentinel token when Link parsing fails.
            if next_page == "has_next":
                logger.warning(
                    "Shopify pagination indicated next page but page_info token could not be parsed; stopping early",
                    extra={"merchant_id": target_merchant_id, "domain": store.get("domain")},
                )
                break

            # Continue pagination.
            page_info = next_page

        # Update store product_count
        await database.execute(
            """UPDATE merchant_stores
               SET product_count = :count, last_sync = CURRENT_TIMESTAMP
               WHERE store_id = :store_id""",
            {"count": synced_count, "store_id": store["store_id"]}
        )

        # Onboarding→audit readiness (WS-A.2): the sync above populated
        # products_cache. Ingest it into the catalog so the merchant's OWN sync
        # action produces an auditable catalog — run_catalog_sync_job then
        # enqueues the quality backfill (WS-A.1), so the merchant becomes
        # v3-audit-ready without any admin/webhook step. Run as a BACKGROUND task
        # to keep this response fast; best-effort so it never breaks the sync.
        catalog_ingest_queued = False
        try:
            from services.catalog_sync_service import (
                create_catalog_sync_job,
                run_catalog_sync_job,
            )
            cjob = await create_catalog_sync_job(
                merchant_id=target_merchant_id,
                connector="shopify",
                mode="reconcile",
                scope={"platform": "shopify"},
                requested_by="merchant_products_sync",
            )
            background_tasks.add_task(run_catalog_sync_job, cjob["job_id"])
            catalog_ingest_queued = True
        except Exception as exc:  # noqa: BLE001 - readiness hook is best-effort
            logger.warning(
                "merchant sync: catalog ingest enqueue failed merchant=%s: %s",
                target_merchant_id, exc,
            )

        return {
            "status": "success",
            "message": f"Successfully synced {synced_count} products from {store['domain']}",
            "data": {
                "product_count": synced_count,
                "store_domain": store["domain"],
                "pages_fetched": pages_fetched,
                "synced_at": datetime.now().isoformat(),
                "catalog_ingest_queued": catalog_ingest_queued
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing Shopify products via legacy endpoint: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")


@router.post("/wix/connect")
async def merchant_connect_wix(
    request: ConnectWixRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their Wix store"""
    # Allow merchant, employee, or admin
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # If merchant role, verify they can only connect their own store
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        try:
            validation = await validate_wix_catalog_access(request.site_id, request.api_key)
        except WixConnectionValidationError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            )

        site_id = validation["site_id"]
        api_key = validation["api_key"]

        logger.info("Wix credentials verified for merchant=%s", request.merchant_id)
        
        # Check if store already exists
        existing_store = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'wix' AND domain = :site_id""",
            {"merchant_id": request.merchant_id, "site_id": site_id}
        )
        
        if existing_store:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :token, status = 'active', connected_at = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"store_id": existing_store["store_id"], "token": api_key}
            )
            store_id = existing_store["store_id"]
        else:
            # Insert new store
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'wix', :site_id, :name, :token, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "site_id": site_id,
                    "name": request.store_name or f"Wix Store {site_id[:8]}",
                    "token": api_key
                }
            )
        
        return {
            "status": "success",
            "message": "Wix store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting Wix: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect Wix: {str(e)}")


@router.post("/woocommerce/connect")
async def merchant_connect_woocommerce(
    request: ConnectWooCommerceRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their WooCommerce store"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        if not request.store_url or not request.consumer_key or not request.consumer_secret:
            raise HTTPException(status_code=400, detail="Store URL, Consumer Key and Consumer Secret are required")
        
        # Test WooCommerce API connection using adapter
        from adapters.woocommerce_adapter import WooCommerceAdapter
        
        adapter = WooCommerceAdapter({
            'store_url': request.store_url,
            'consumer_key': request.consumer_key,
            'consumer_secret': request.consumer_secret
        })
        
        # Validate config
        is_valid, error_msg = adapter.validate_config()
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Test connection
        test_result = await adapter.test_connection()
        if not test_result.get('success'):
            raise HTTPException(status_code=400, detail=f"WooCommerce connection failed: {test_result.get('error')}")
        
        normalized_store_url = adapter.store_url
        credential_blob = json.dumps(
            {
                "consumer_key": request.consumer_key,
                "consumer_secret": request.consumer_secret,
            },
            separators=(",", ":"),
        )

        logger.info(f"✅ WooCommerce credentials verified for {normalized_store_url}")
        
        # Check if store already exists
        existing_store = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'woocommerce' AND domain = :domain""",
            {"merchant_id": request.merchant_id, "domain": normalized_store_url}
        )
        
        if existing_store:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :api_key, status = 'active', last_sync = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"store_id": existing_store["store_id"], "api_key": credential_blob}
            )
            store_id = existing_store["store_id"]
        else:
            # Insert new store
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'woocommerce', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "domain": normalized_store_url,
                    "name": test_result.get('store_name', f"WooCommerce Store"),
                    "api_key": credential_blob
                }
            )
        
        return {
            "status": "success",
            "message": "WooCommerce store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting WooCommerce: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect WooCommerce: {str(e)}")


@router.post("/bigcommerce/connect")
async def merchant_connect_bigcommerce(
    request: ConnectBigCommerceRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their BigCommerce store"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        if not request.store_hash or not request.access_token:
            raise HTTPException(status_code=400, detail="Store Hash and Access Token are required")
        
        # Test BigCommerce API connection using adapter
        from adapters.bigcommerce_adapter import BigCommerceAdapter, build_bigcommerce_domain
        
        adapter = BigCommerceAdapter({
            'store_hash': request.store_hash,
            'access_token': request.access_token,
            'client_id': request.client_id
        })
        
        # Validate config
        is_valid, error_msg = adapter.validate_config()
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Test connection
        test_result = await adapter.test_connection()
        if not test_result.get('success'):
            raise HTTPException(status_code=400, detail=f"BigCommerce connection failed: {test_result.get('error')}")
        
        normalized_store_hash = adapter.store_hash
        store_domain = build_bigcommerce_domain(normalized_store_hash)
        credential_blob = json.dumps(
            {
                "access_token": request.access_token,
                "client_id": request.client_id,
                "store_hash": normalized_store_hash,
            },
            separators=(",", ":"),
        )

        logger.info(f"✅ BigCommerce credentials verified for {normalized_store_hash}")
        
        # Check if store already exists
        existing_store = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'bigcommerce' AND domain = :domain""",
            {"merchant_id": request.merchant_id, "domain": store_domain}
        )
        
        if existing_store:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :api_key, status = 'active', last_sync = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"store_id": existing_store["store_id"], "api_key": credential_blob}
            )
            store_id = existing_store["store_id"]
        else:
            # Insert new store
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'bigcommerce', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "domain": store_domain,
                    "name": test_result.get('store_name', f"BigCommerce Store"),
                    "api_key": credential_blob
                }
            )
        
        return {
            "status": "success",
            "message": "BigCommerce store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting BigCommerce: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect BigCommerce: {str(e)}")


@router.post("/prestashop/connect")
async def merchant_connect_prestashop(
    request: ConnectPrestaShopRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their PrestaShop store"""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if current_user["role"] == "merchant":
        if current_user.get("merchant_id") != request.merchant_id:
            raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        if not request.store_url or not request.api_key:
            raise HTTPException(status_code=400, detail="Store URL and API Key are required")
        
        # Test PrestaShop API connection using adapter
        from adapters.prestashop_adapter import PrestaShopAdapter
        
        adapter = PrestaShopAdapter({
            'store_url': request.store_url,
            'api_key': request.api_key
        })
        
        # Validate config
        is_valid, error_msg = adapter.validate_config()
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Test connection
        test_result = await adapter.test_connection()
        if not test_result.get('success'):
            raise HTTPException(status_code=400, detail=f"PrestaShop connection failed: {test_result.get('error')}")
        
        logger.info(f"✅ PrestaShop credentials verified for {request.store_url}")
        
        # Check if store already exists
        existing_store = await database.fetch_one(
            """SELECT store_id FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND platform = 'prestashop' AND domain = :domain""",
            {"merchant_id": request.merchant_id, "domain": request.store_url}
        )
        
        if existing_store:
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores 
                   SET api_key = :api_key, status = 'active', last_sync = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"store_id": existing_store["store_id"], "api_key": request.api_key}
            )
            store_id = existing_store["store_id"]
        else:
            # Insert new store
            store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
            await database.execute(
                """INSERT INTO merchant_stores 
                   (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
                   VALUES (:store_id, :merchant_id, 'prestashop', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)""",
                {
                    "store_id": store_id,
                    "merchant_id": request.merchant_id,
                    "domain": request.store_url,
                    "name": test_result.get('store_name', f"PrestaShop Store"),
                    "api_key": request.api_key
                }
            )
        
        return {
            "status": "success",
            "message": "PrestaShop store connected successfully",
            "store_id": store_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting PrestaShop: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect PrestaShop: {str(e)}")


@router.post("/stores/support-email")
async def merchant_update_store_support_email(
    request: UpdateStoreSupportEmailRequest,
    current_user: dict = Depends(get_current_user),
):
    """Allow merchant to set a support email for review invitations."""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = request.merchant_id or current_user.get("merchant_id")
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    if current_user["role"] == "merchant" and current_user.get("merchant_id") != target_merchant_id:
        raise HTTPException(status_code=403, detail="Can only update your own store")

    support_email = (request.support_email or "").strip() or None
    if support_email is not None:
        try:
            support_email = TypeAdapter(EmailStr).validate_python(support_email)
        except ValidationError:
            raise HTTPException(status_code=400, detail="Invalid support_email")

    # Backward compatibility: column may not exist on some deployments.
    try:
        await database.execute("ALTER TABLE merchant_stores ADD COLUMN IF NOT EXISTS support_email TEXT")
    except Exception:
        pass

    store_row = await database.fetch_one(
        """
        SELECT store_id
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND status IN ('active','connected')
        ORDER BY connected_at DESC
        LIMIT 1
        """,
        {"merchant_id": target_merchant_id},
    )
    if not store_row:
        raise HTTPException(status_code=404, detail="No active store found")

    await database.execute(
        "UPDATE merchant_stores SET support_email = :support_email WHERE store_id = :store_id",
        {"support_email": support_email, "store_id": store_row["store_id"]},
    )

    return {
        "status": "success",
        "merchant_id": target_merchant_id,
        "store_id": store_row["store_id"],
        "support_email": support_email,
    }


@router.get("/stores/support-email", response_model=GetStoreSupportEmailResponse)
async def merchant_get_store_support_email(
    merchant_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """Get the current (and effective) support email used for review invitations."""
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = (merchant_id or "").strip() or (current_user.get("merchant_id") or "").strip()
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    if current_user["role"] == "merchant" and current_user.get("merchant_id") != target_merchant_id:
        raise HTTPException(status_code=403, detail="Can only view your own store")

    # Backward compatibility: column may not exist on some deployments.
    try:
        await database.execute("ALTER TABLE merchant_stores ADD COLUMN IF NOT EXISTS support_email TEXT")
    except Exception:
        pass

    store_row = await database.fetch_one(
        """
        SELECT store_id, support_email
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND status IN ('active','connected')
        ORDER BY connected_at DESC
        LIMIT 1
        """,
        {"merchant_id": target_merchant_id},
    )
    if not store_row:
        raise HTTPException(status_code=404, detail="No active store found")

    support_email_raw = None
    try:
        support_email_raw = store_row["support_email"]
    except Exception:
        try:
            support_email_raw = dict(store_row).get("support_email")
        except Exception:
            support_email_raw = None
    support_email = (str(support_email_raw or "").strip() or None)
    effective = support_email
    if not effective:
        effective = (os.getenv("REVIEWS_INVITATION_SUPPORT_EMAIL") or "").strip() or None
    if not effective:
        effective = (os.getenv("FROM_EMAIL") or "").strip() or None

    return {
        "status": "success",
        "merchant_id": target_merchant_id,
        "store_id": store_row["store_id"],
        "support_email": support_email,
        "effective_support_email": effective,
    }
