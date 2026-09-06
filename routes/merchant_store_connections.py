"""
Merchant Store Connections
Allow merchants to connect their own stores (Shopify, Wix, etc.)
"""
from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from services.shopify_access_token_service import (
    exchange_shopify_client_credentials_token,
    resolve_shopify_admin_access_token,
)
from services.wix_connection import (
    WixConnectionValidationError,
    find_wix_stores_by_instance_id,
    is_wix_instance_id,
    merge_wix_credential,
    validate_wix_catalog_access,
)
from services.store_lifecycle_service import sync_catalog_merchant_status
from services.shopify_domain import canonicalize_shop_domain, normalize_myshopify_domain
from services.bigcommerce_event_adapter import SUPPORTED_BIGCOMMERCE_SCOPES
from fastapi import APIRouter, Depends, HTTPException, Body, BackgroundTasks, Request, Query, Header
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field, TypeAdapter, ValidationError
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from contextlib import asynccontextmanager
import asyncio
import logging
import httpx
import json
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import re
import secrets
from urllib.parse import quote, urlparse, urlencode

from db.database import IS_POSTGRES, database
from db.startup_ddl import _asyncpg_dsn, _connect_kwargs
from utils.auth import (
    MERCHANT_OR_ADMIN_ROLES,
    MERCHANT_OR_EMPLOYEE_STAFF_ROLES,
    can_access_merchant,
    get_current_user,
    hash_password,
    verify_password as verify_bcrypt_password,
)
from config.settings import settings
from config.settings import resolve_public_api_base_url
from services.merchant_web_collector_service import (
    MAX_ALLOWED_ORIGINS,
    MAX_TOKEN_TTL_DAYS,
    WebCollectorError,
    issue_web_collector_token,
    normalize_allowed_origins,
    normalize_collector_origin,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["Merchant Integrations"])

_SHOPIFY_OAUTH_STATE_TTL_SECONDS = 30 * 60
_SHOPIFY_OAUTH_REQUIRED_WEBHOOK_TOPICS = [
    "orders/create",
    "orders/updated",
    "orders/paid",
    "orders/cancelled",
    # Refunds. services/shopify_commerce_event_adapter.py maps refunds/create
    # into the canonical ledger (refund.created, and refund.succeeded per
    # successful refund transaction), and routes/webhook_routes.py reconciles
    # it operationally. Until 2026-09-04 no install path registered it, so
    # Shopify refunds reached the ledger only for merchants who had run the
    # verify flow. Every topic the adapter maps must be registered here;
    # tests/test_shopify_refund_webhook_subscription.py pins that.
    "refunds/create",
    "fulfillments/create",
    "fulfillments/update",
    "orders/fulfilled",
    "app/uninstalled",
]

_STOREFRONT_AUTO_CREATE_DENIED_UNTIL: Dict[str, float] = {}
_STOREFRONT_AUTO_CREATE_DENIED_TTL_SECONDS = 24 * 3600
_MARKETPLACE_INSTALL_SUCCESS_PATH = "/app/install/success"
_MARKETPLACE_INSTALL_ERROR_PATH = "/app/install/error"


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

        url = f"https://{shop_domain}/admin/api/2025-10/storefront_access_tokens.json"
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


def _validate_myshopify_domain(value: str) -> str:
    # The one place a 400 is the right answer: the caller's own input. Everywhere else the value is
    # re-checked with normalize_myshopify_domain and FALLS BACK, because failing a merchant's
    # install over an unexpected upstream response would be worse than the thing being prevented.
    if not (canonicalize_shop_domain(value) or "").strip():
        raise HTTPException(status_code=400, detail="shop is required")
    shop = normalize_myshopify_domain(value)
    if not shop:
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


_SHOPIFY_CLAIM_TOKEN_TTL_SECONDS = 60 * 60


def _claim_signing_key() -> str:
    from config.settings import require_jwt_secret

    return (require_jwt_secret() or "").strip()


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    import base64

    s = (data or "").strip()
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign_claim_token(payload: Dict[str, Any]) -> str:
    """base64url(json) + "." + base64url(hmac_sha256(msg, key))."""
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    msg = _b64url(raw)
    sig = hmac.new(_claim_signing_key().encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return f"{msg}.{_b64url(sig)}"


def _verify_claim_token(token: str) -> Dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Invalid claim token")
    msg, sig = parts
    try:
        expected = hmac.new(
            _claim_signing_key().encode("utf-8"), msg.encode("utf-8"), hashlib.sha256
        ).digest()
        got = _b64url_decode(sig)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid claim token")
    if not hmac.compare_digest(expected, got):
        raise HTTPException(status_code=401, detail="Invalid claim token signature")
    try:
        payload = json.loads(_b64url_decode(msg).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid claim token")
    if not isinstance(payload, dict) or payload.get("typ") != "pivota_shopify_claim":
        raise HTTPException(status_code=400, detail="Invalid claim token")
    return payload


async def _reassign_store_to_merchant(
    *, from_merchant_id: str, to_merchant_id: str, shop_domain: str
) -> None:
    """Move a freshly-installed Shopify store off the shell merchant onto a real one.

    Used when the person claiming the install already runs a Pivota merchant: they
    keep their own merchant_id and simply gain the store.
    """
    # If they somehow already have this shop, drop the shell duplicate instead of
    # violating the (merchant_id, platform, domain) uniqueness.
    already = await database.fetch_one(
        """
        SELECT store_id FROM merchant_stores
        WHERE merchant_id = :to_merchant_id AND platform = 'shopify' AND domain = :domain
        """,
        {"to_merchant_id": to_merchant_id, "domain": shop_domain},
    )
    if already:
        await database.execute(
            """
            DELETE FROM merchant_stores
            WHERE merchant_id = :from_merchant_id AND platform = 'shopify' AND domain = :domain
            """,
            {"from_merchant_id": from_merchant_id, "domain": shop_domain},
        )
        return

    await database.execute(
        """
        UPDATE merchant_stores
        SET merchant_id = :to_merchant_id
        WHERE merchant_id = :from_merchant_id AND platform = 'shopify' AND domain = :domain
        """,
        {
            "to_merchant_id": to_merchant_id,
            "from_merchant_id": from_merchant_id,
            "domain": shop_domain,
        },
    )


async def _merchant_has_owner(merchant_id: str) -> bool:
    """True once a real user account is bound to this merchant."""
    try:
        row = await database.fetch_one(
            "SELECT 1 AS ok FROM users WHERE merchant_id = :merchant_id LIMIT 1",
            {"merchant_id": merchant_id},
        )
        return bool(row)
    except Exception:
        # Fail closed: if we cannot tell, do not hand out a claim token.
        logger.warning("merchant owner lookup failed merchant=%s", merchant_id, exc_info=True)
        return True


async def _mint_store_claim_token(*, merchant_id: str, shop_domain: str) -> Optional[str]:
    """Best-effort: an install must still succeed even if claim-token minting fails."""
    try:
        jti = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=_SHOPIFY_CLAIM_TOKEN_TTL_SECONDS)
        token = _sign_claim_token(
            {
                "typ": "pivota_shopify_claim",
                "jti": jti,
                "merchant_id": merchant_id,
                "shop_domain": shop_domain,
                "iat": int(now.timestamp()),
                "exp": int(exp.timestamp()),
            }
        )
        await database.execute(
            """
            INSERT INTO shopify_store_claim_tokens (jti_sha256, merchant_id, shop_domain, expires_at)
            VALUES (:jti_sha256, :merchant_id, :shop_domain, :expires_at)
            ON CONFLICT (jti_sha256) DO NOTHING
            """,
            {
                "jti_sha256": hashlib.sha256(jti.encode("utf-8")).hexdigest(),
                "merchant_id": merchant_id,
                "shop_domain": shop_domain,
                "expires_at": exp,
            },
        )
        return token
    except Exception:
        logger.warning("shopify claim-token mint failed merchant=%s", merchant_id, exc_info=True)
        return None


_INSTALL_ERROR_REASON_RE = re.compile(r"reason=([a-z0-9_]+)")


def _install_error_reason(detail: Any) -> str:
    """Map an internal OAuth failure into a short, non-leaky reason slug."""
    text = str(detail or "").strip()
    m = _INSTALL_ERROR_REASON_RE.search(text)
    if m:
        return m.group(1)
    lowered = text.lower()
    if "signature" in lowered:
        return "invalid_signature"
    if "not configured" in lowered:
        return "not_configured"
    if "access token" in lowered or "access_token" in lowered:
        return "token_exchange_failed"
    if "verification failed" in lowered or "shop response" in lowered:
        return "shop_verification_failed"
    if "myshopify.com" in lowered and "domain" in lowered:
        return "invalid_shop"
    if "shop" in lowered and "required" in lowered:
        return "missing_shop"
    if "missing required oauth params" in lowered:
        return "missing_params"
    return "install_failed"


def _marketplace_install_error_url(reason: str) -> str:
    base = settings.merchant_portal_base_url.rstrip("/")
    return f"{base}{_MARKETPLACE_INSTALL_ERROR_PATH}?{urlencode({'reason': reason})}"


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
        # One-time tokens that let whoever completed an App Store install bind the
        # freshly-created shell merchant to a real Pivota account (see /shopify/claim).
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS shopify_store_claim_tokens (
                jti_sha256 VARCHAR(64) PRIMARY KEY,
                merchant_id VARCHAR(50) NOT NULL,
                shop_domain VARCHAR(255) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                used_at TIMESTAMP WITH TIME ZONE
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
        # A reconnect must re-open the public door a disconnect closed:
        # catalog_merchants.status is derived from merchant_stores now (#1648).
        # The hourly reconciliation sweep would converge this within the hour
        # anyway; the call here just makes it immediate.
        await sync_catalog_merchant_status(merchant_id, reason="shopify_store_reconnected")
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
    await sync_catalog_merchant_status(merchant_id, reason="shopify_store_connected")
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
    if current_user.get("role") not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = (merchant_id or "").strip() or (current_user.get("merchant_id") or "").strip()
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    if not can_access_merchant(current_user, target_merchant_id):
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


def _wants_shopify_oauth_redirect(raw: Any) -> bool:
    """Lenient parse of the ?redirect= debug flag — a malformed value must not
    surface a raw validation error on this browser-loaded route."""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no"}


@router.get("/shopify/app")
@router.get("/shopify/install")
async def shopify_app_store_install(
    request: Request,
    shop: Optional[str] = Query(None, description="Shop domain provided by Shopify, e.g. your-shop.myshopify.com"),
    host: Optional[str] = Query(None),
    embedded: Optional[str] = Query(None),
    redirect: Optional[str] = Query(None, description="Set to 'false' to return JSON instead of a 302 to Shopify OAuth"),
):
    """
    Public Shopify App Store entrypoint.
    Shopify calls this without a Pivota JWT, so we bind OAuth state to an
    existing merchant for that shop when present or create a shell merchant.

    Like the OAuth callback below, this URL is loaded by a BROWSER, so it must
    NEVER render a JSON error body (Shopify 2.1.1). `shop` is validated inside
    the impl rather than by FastAPI so a missing/malformed value 302s to the
    portal install-error page instead of raising a raw validation envelope.
    """
    try:
        return await _shopify_app_store_install_impl(
            shop=shop,
            host=host,
            embedded=embedded,
            redirect=_wants_shopify_oauth_redirect(redirect),
        )
    except HTTPException as exc:
        reason = _install_error_reason(exc.detail)
        logger.warning(
            "shopify_app_store_install failed status=%s reason=%s detail=%s shop=%s",
            exc.status_code, reason, exc.detail, shop,
        )
        return RedirectResponse(url=_marketplace_install_error_url(reason), status_code=302)
    except Exception:
        logger.exception("shopify_app_store_install crashed shop=%s", shop)
        return RedirectResponse(
            url=_marketplace_install_error_url("install_failed"), status_code=302
        )


async def _shopify_app_store_install_impl(
    *,
    shop: Optional[str],
    host: Optional[str],
    embedded: Optional[str],
    redirect: bool,
):
    shop_domain = _validate_myshopify_domain(shop or "")
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
    Shopify OAuth callback. This endpoint is loaded by a BROWSER (Shopify's OAuth
    redirect), so it must NEVER render a JSON body — Shopify review flags a raw /
    pretty-printed JSON page as a display error (2.1.1), and every failure path
    below used to raise HTTPException. Any failure is therefore converted into a
    302 to a clean portal error page; the real reason is kept in the logs and
    reduced to a short slug in the query string.
    """
    try:
        return await _shopify_oauth_callback_impl(request)
    except HTTPException as exc:
        reason = _install_error_reason(exc.detail)
        logger.warning(
            "shopify_oauth_callback failed status=%s reason=%s detail=%s shop=%s",
            exc.status_code, reason, exc.detail, request.query_params.get("shop"),
        )
        return RedirectResponse(url=_marketplace_install_error_url(reason), status_code=302)
    except Exception:
        logger.exception(
            "shopify_oauth_callback crashed shop=%s", request.query_params.get("shop")
        )
        return RedirectResponse(
            url=_marketplace_install_error_url("install_failed"), status_code=302
        )


async def _shopify_oauth_callback_impl(request: Request):
    """
    Shopify OAuth callback (unauthenticated; validated via HMAC + state anti-replay).

    Raises HTTPException on any failure; the route wrapper above turns that into a
    browser redirect. Never return raw JSON from here.
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
            f"https://{shop_domain}/admin/api/2025-10/shop.json",
            headers={"X-Shopify-Access-Token": access_token},
        )
    if shop_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Shopify token verification failed")
    shop_json = shop_resp.json() or {}
    shop_info = shop_json.get("shop") if isinstance(shop_json, dict) else None
    if not isinstance(shop_info, dict):
        raise HTTPException(status_code=400, detail="Invalid Shopify shop response")

    # `shop_domain` is already validated; `myshopify_domain` is whatever the upstream response
    # carried, and it — not the validated input — is what gets PERSISTED and later turned back into
    # an Admin API URL by this repo and by the gateway. Re-check it, and fall back to the validated
    # input rather than failing the install.
    canonical_myshopify_domain = (
        normalize_myshopify_domain(shop_info.get("myshopify_domain")) or shop_domain
    )
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
                    api_version="2025-10",
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

    # An App Store install mints a SHELL merchant with a placeholder contact_email
    # and no user row, so nobody can actually sign in to it. Hand the installer a
    # one-time claim token so they can bind this store to a real Pivota account
    # (sign in or create one) instead of dead-ending on the landing page.
    claim_params: Dict[str, str] = {}
    if not await _merchant_has_owner(merchant_id):
        claim_token = await _mint_store_claim_token(
            merchant_id=merchant_id, shop_domain=canonical_myshopify_domain
        )
        if claim_token:
            claim_params["claim_token"] = claim_token

    return RedirectResponse(
        url=_append_query_params(
            redirect_target,
            {
                "installed": "shopify",
                "merchant_id": merchant_id,
                "shop": canonical_myshopify_domain,
                "store_id": store_id,
                "status": "success",
                **claim_params,
            },
        ),
        status_code=302,
    )


class ShopifyStoreClaimRequest(BaseModel):
    claim_token: str
    email: EmailStr
    password: str
    full_name: Optional[str] = None


@router.post("/shopify/claim")
async def claim_shopify_store(body: ShopifyStoreClaimRequest):
    """
    Bind an App Store-installed Shopify store to a real Pivota account.

    An App Store install creates a SHELL merchant (placeholder contact_email, no
    user row), so without this the installer lands on the success page and has no
    way to sign in. The one-time claim token issued by the OAuth callback proves
    they completed the install for this shop; they may either sign in to an
    existing account or create one, and the store binds to it.
    """
    # Imported lazily: auth_routes owns the portal's JWT shape and email normalisation.
    from routes.auth_routes import create_jwt_token, normalize_email

    payload = _verify_claim_token(body.claim_token)

    shell_merchant_id = str(payload.get("merchant_id") or "").strip()
    shop_domain = str(payload.get("shop_domain") or "").strip().lower()
    jti = str(payload.get("jti") or "").strip()
    if not shell_merchant_id or not shop_domain or not jti:
        raise HTTPException(status_code=400, detail="Invalid claim token")
    try:
        if int(datetime.now(timezone.utc).timestamp()) > int(payload.get("exp") or 0):
            raise HTTPException(status_code=400, detail="This link has expired. Reinstall the app to get a new one.")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid claim token")

    password = body.password or ""
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    email = normalize_email(str(body.email))
    await _ensure_shopify_oauth_tables()

    # Consume the token FIRST (atomic, one-time) so a replay cannot re-bind the store.
    consumed = await database.fetch_one(
        """
        UPDATE shopify_store_claim_tokens
        SET used_at = NOW()
        WHERE jti_sha256 = :jti_sha256
          AND used_at IS NULL
          AND expires_at > NOW()
          AND merchant_id = :merchant_id
          AND shop_domain = :shop_domain
        RETURNING merchant_id
        """,
        {
            "jti_sha256": hashlib.sha256(jti.encode("utf-8")).hexdigest(),
            "merchant_id": shell_merchant_id,
            "shop_domain": shop_domain,
        },
    )
    if not consumed:
        raise HTTPException(
            status_code=400,
            detail="This link has already been used or expired. Reinstall the app to get a new one.",
        )

    existing = await database.fetch_one(
        "SELECT id, email, password_hash, full_name, role, active, merchant_id FROM users WHERE email = :email",
        {"email": email},
    )
    user = dict(existing) if existing else None

    if user:
        # Existing account: authenticate before binding anything to it.
        if not user.get("password_hash") or not verify_bcrypt_password(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Incorrect password for this account")
        if user.get("active") is False:
            raise HTTPException(status_code=403, detail="Account has been deactivated")

        target_merchant_id = (user.get("merchant_id") or "").strip() or shell_merchant_id
        if target_merchant_id != shell_merchant_id:
            # They already run a merchant: move the store onto it rather than
            # repointing their account at the throwaway shell merchant.
            await _reassign_store_to_merchant(
                from_merchant_id=shell_merchant_id,
                to_merchant_id=target_merchant_id,
                shop_domain=shop_domain,
            )
        else:
            await database.execute(
                "UPDATE users SET merchant_id = :merchant_id WHERE id = :id",
                {"merchant_id": shell_merchant_id, "id": user["id"]},
            )
        role = user.get("role") or "merchant"
    else:
        # New account: it owns the shell merchant outright.
        target_merchant_id = shell_merchant_id
        role = "merchant"
        await database.execute(
            """
            INSERT INTO users (email, password_hash, full_name, role, active, merchant_id)
            VALUES (:email, :password_hash, :full_name, 'merchant', TRUE, :merchant_id)
            """,
            {
                "email": email,
                "password_hash": hash_password(password),
                "full_name": (body.full_name or "").strip() or shop_domain,
                "merchant_id": shell_merchant_id,
            },
        )

    # Replace the placeholder shopify-install+...@pivota.invalid contact with the real one.
    if target_merchant_id == shell_merchant_id:
        await database.execute(
            """
            UPDATE merchant_onboarding
            SET contact_email = :email, updated_at = CURRENT_TIMESTAMP
            WHERE merchant_id = :merchant_id
            """,
            {"email": email, "merchant_id": shell_merchant_id},
        )

    logger.info(
        "shopify_store_claimed merchant=%s shop=%s email=%s new_account=%s",
        target_merchant_id, shop_domain, email, user is None,
    )

    token = create_jwt_token(email, role, email, {"merchant_id": target_merchant_id})
    return {
        "status": "success",
        "token": token,
        "user": {
            "email": email,
            "role": role,
            "merchant_id": target_merchant_id,
            "full_name": (body.full_name or "").strip() or (user or {}).get("full_name") or shop_domain,
        },
        "shop_domain": shop_domain,
    }


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
    if current_user.get("role") not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = (merchant_id or "").strip() or (current_user.get("merchant_id") or "").strip()
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    if not can_access_merchant(current_user, target_merchant_id):
        raise HTTPException(status_code=403, detail="Can only access your own merchant")

    store = await get_primary_store(target_merchant_id)
    if not store or (store.get("platform") or "").lower() != "shopify":
        raise HTTPException(status_code=400, detail="No Shopify store connected")

    # Same stored column, same Admin API URLs, same guard as verify_shopify_integration. This route
    # is the sharper of the two: it returns the upstream status codes in a 200 body, so an unpinned
    # host here is a read-back SSRF oracle rather than a blind one.
    raw_domain = (store.get("domain") or store.get("shop_domain") or "")
    shop_domain = normalize_myshopify_domain(raw_domain) or ""
    if not shop_domain:
        raise HTTPException(
            status_code=400,
            detail="Stored Shopify domain is not a *.myshopify.com host",
        )
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
            f"https://{shop_domain}/admin/api/2025-10/shop.json",
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
    # The id of the Pivota app INSTANCE on this Wix site. Wix webhooks are an
    # app-level extension delivered to one static URL for every site that
    # installed the app, and the delivery names its site only by `instanceId`
    # (https://dev.wix.com/docs/build-apps/develop-your-app/api-integrations/events-and-webhooks/about-webhooks.md),
    # so routes/wix_webhooks.py cannot resolve a store without it. Nothing
    # persisted it before this: the API-key connect below never knew it, and
    # the OAuth path is still a 501 stub. Optional, because catalog sync works
    # without it — a store that omits it simply receives no telemetry.
    instance_id: Optional[str] = None


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
    # The one site in this module that had no role gate at all -- only the
    # merchant comparison -- so an agent or a buyer reached the body. The body
    # is an unconditional 501, so nothing was exposed; it is gated here for the
    # same reason the rest of the file is spelled one way: the next person to
    # implement this stub inherits the guard instead of having to notice its
    # absence. Non-merchant, non-staff callers now get 403 rather than 501.
    if current_user.get("role") not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    if not can_access_merchant(current_user, merchant_id):
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
    webhook_secret: Optional[str] = None


def _woocommerce_webhook_callback_url(store_id: str) -> str:
    base = str(
        os.getenv("WOOCOMMERCE_WEBHOOK_BASE_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or os.getenv("PIVOTA_BACKEND_BASE_URL")
        or ""
    ).strip().rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "Configure WOOCOMMERCE_WEBHOOK_BASE_URL or PUBLIC_BASE_URL "
                "as an HTTPS origin"
            ),
        )
    callback_url = f"{base}/webhooks/woocommerce/{quote(store_id, safe='')}"
    if len(callback_url) > 2048:
        raise HTTPException(status_code=503, detail="Webhook callback URL is too long")
    return callback_url


def _bigcommerce_webhook_callback_url(store_id: str) -> str:
    base = str(
        os.getenv("BIGCOMMERCE_WEBHOOK_BASE_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or os.getenv("PIVOTA_BACKEND_BASE_URL")
        or ""
    ).strip().rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "Configure BIGCOMMERCE_WEBHOOK_BASE_URL or PUBLIC_BASE_URL "
                "as an HTTPS origin"
            ),
        )
    callback_url = f"{base}/webhooks/bigcommerce/{quote(store_id, safe='')}"
    if len(callback_url) > 2048:
        raise HTTPException(status_code=503, detail="Webhook callback URL is too long")
    return callback_url


def _squarespace_webhook_callback_url(store_id: str) -> str:
    base = str(
        os.getenv("SQUARESPACE_WEBHOOK_BASE_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or os.getenv("PIVOTA_BACKEND_BASE_URL")
        or ""
    ).strip().rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "Configure SQUARESPACE_WEBHOOK_BASE_URL or PUBLIC_BASE_URL "
                "as an HTTPS origin"
            ),
        )
    callback_url = f"{base}/webhooks/squarespace/{quote(store_id, safe='')}"
    if len(callback_url) > 2048:
        raise HTTPException(status_code=503, detail="Webhook callback URL is too long")
    return callback_url


def _bigcommerce_credentials(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    value = str(raw or "").strip()
    if not value or not value.startswith("{"):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _woocommerce_credentials(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    value = str(raw or "").strip()
    if not value:
        return {}
    if ":" in value and not value.startswith("{"):
        consumer_key, consumer_secret = value.split(":", 1)
        return {
            "consumer_key": consumer_key.strip(),
            "consumer_secret": consumer_secret.strip(),
        }
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


@asynccontextmanager
async def _woocommerce_webhook_install_lock(store_id: str):
    """Serialize one store without consuming the serving database pool."""

    if not IS_POSTGRES:
        yield
        return
    dsn = _asyncpg_dsn()
    if not dsn:
        raise HTTPException(
            status_code=503,
            detail="WooCommerce webhook installation lock is unavailable",
        )
    lock_name = f"woocommerce:webhook-install:{store_id}"
    connection = None
    try:
        import asyncpg

        connection = await asyncio.wait_for(
            asyncpg.connect(dsn, **_connect_kwargs()),
            timeout=5.0,
        )
        acquired = bool(
            await asyncio.wait_for(
                connection.fetchval(
                    "SELECT pg_try_advisory_lock(hashtext($1))",
                    lock_name,
                ),
                timeout=5.0,
            )
        )
    except BaseException as exc:
        if connection is not None:
            try:
                connection.terminate()
            except Exception:
                pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise HTTPException(
            status_code=503,
            detail="WooCommerce webhook installation lock is unavailable",
        ) from exc
    if not acquired:
        try:
            await asyncio.wait_for(connection.close(), timeout=5.0)
        except Exception:
            connection.terminate()
        raise HTTPException(
            status_code=409,
            detail="WooCommerce webhook installation is already in progress",
        )
    try:
        yield
    finally:
        try:
            await asyncio.wait_for(
                connection.execute(
                    "SELECT pg_advisory_unlock(hashtext($1))",
                    lock_name,
                ),
                timeout=5.0,
            )
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                connection.terminate()
                raise
            logger.warning(
                "WooCommerce webhook advisory unlock failed store_id=%s error=%s",
                store_id,
                str(exc)[:200],
            )
        try:
            await asyncio.wait_for(connection.close(), timeout=5.0)
        except BaseException as exc:
            connection.terminate()
            if isinstance(exc, asyncio.CancelledError):
                raise


_WOOCOMMERCE_WEBHOOK_INSTALL_CONCURRENCY = asyncio.Semaphore(4)
_BIGCOMMERCE_WEBHOOK_INSTALL_CONCURRENCY = asyncio.Semaphore(4)
_SQUARESPACE_WEBHOOK_INSTALL_CONCURRENCY = asyncio.Semaphore(4)

# One in-flight sweep per store. Two sweeps of the SAME store race on one
# `reconciliation` cell: whichever merge lands last decides the cursor, so the
# other run's pages are either re-read or, if it advanced further, skipped.
# This is per-process, which is the honest bound — it stops the operator
# double-click and the retry-on-timeout, not two replicas. The sweep's own
# correctness under a genuinely concurrent merge is the transaction in
# `merge_squarespace_credentials`, not this set.
_SQUARESPACE_SWEEPS_IN_FLIGHT: set[str] = set()
# A full sweep is `max_pages` (<=200) sequential upstream calls at a 15s
# timeout each. The request must not outlive the proxy in front of it.
SQUARESPACE_RECONCILE_TIMEOUT_SECONDS = 240.0


class ConnectBigCommerceRequest(BaseModel):
    merchant_id: str
    store_hash: str
    access_token: str
    client_id: Optional[str] = None


class ConnectPrestaShopRequest(BaseModel):
    merchant_id: str
    store_url: str
    api_key: str


class ConnectSquarespaceRequest(BaseModel):
    """A Squarespace connection is a credential plus, optionally, an OAuth token.

    `api_key` is the per-site Developer API key (Settings -> Developer API
    Keys), which reaches the Orders API and nothing else. `oauth_access_token`
    is accepted so a site connected through a Squarespace Developer Platform app
    can also provision webhooks; without it the store is sweep-only and
    `/webhooks/ensure` answers 409 `oauth_required`.

    `oauth_refresh_token` and `oauth_expires_at` are PERSISTED BUT NOT YET USED:
    a Developer-Platform access token is short-lived (assumed ~30 minutes) and
    this repo has no refresh path, so reads fall back to the API key on 401
    instead. Storing them now means the refresh, when it lands, does not need
    every OAuth store to reconnect first. See the Residual gaps section of
    docs/SQUARESPACE_TELEMETRY.md.
    """

    merchant_id: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1, max_length=512)
    oauth_access_token: Optional[str] = Field(default=None, max_length=2048)
    oauth_refresh_token: Optional[str] = Field(default=None, max_length=2048)
    oauth_expires_at: Optional[str] = Field(default=None, max_length=64)
    store_name: Optional[str] = Field(default=None, max_length=255)
    domain: Optional[str] = Field(default=None, max_length=2048)


class ConnectCustomStoreRequest(BaseModel):
    merchant_id: str = Field(min_length=1, max_length=128)
    store_url: str = Field(min_length=1, max_length=2048)
    store_name: Optional[str] = Field(default=None, max_length=255)
    allowed_origins: List[str] = Field(default_factory=list, max_length=MAX_ALLOWED_ORIGINS)
    collector_token_ttl_days: int = Field(default=90, ge=1, le=MAX_TOKEN_TTL_DAYS)


class UpdateStoreSupportEmailRequest(BaseModel):
    merchant_id: Optional[str] = None
    support_email: Optional[str] = None


class GetStoreSupportEmailResponse(BaseModel):
    status: str
    merchant_id: str
    store_id: str
    support_email: Optional[str] = None
    effective_support_email: Optional[str] = None


@router.post("/custom/connect")
async def merchant_connect_custom_store(
    request: ConnectCustomStoreRequest,
    current_user: dict = Depends(get_current_user),
):
    """Create a credential-free store scope for custom/headless telemetry."""
    if current_user.get("role") not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    if not can_access_merchant(current_user, request.merchant_id):
        raise HTTPException(status_code=403, detail="Can only connect your own store")

    try:
        storefront_origin = normalize_collector_origin(request.store_url)
        origins = normalize_allowed_origins(
            request.allowed_origins or [storefront_origin]
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if storefront_origin not in origins:
        try:
            origins = normalize_allowed_origins([storefront_origin, *origins])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    merchant_id = str(request.merchant_id).strip()
    store_id = "store_custom_" + hashlib.sha256(
        f"{merchant_id}\n{storefront_origin}".encode("utf-8")
    ).hexdigest()[:24]
    store_name = str(request.store_name or "").strip()[:255] or (
        urlparse(storefront_origin).hostname or "Custom storefront"
    )

    # Provision before mutating the store so a missing signing secret fails
    # closed without leaving a half-connected record.
    try:
        collector = issue_web_collector_token(
            merchant_id=merchant_id,
            store_id=store_id,
            platform="custom",
            allowed_origins=origins,
            ttl_days=request.collector_token_ttl_days,
        )
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    existing = await database.fetch_one(
        """
        SELECT store_id
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND platform = 'custom'
          AND domain = :domain
        """,
        {"merchant_id": merchant_id, "domain": storefront_origin},
    )
    if existing:
        store_id = str(existing["store_id"])
        # Reissue with the persisted ID in case this row predates deterministic IDs.
        try:
            collector = issue_web_collector_token(
                merchant_id=merchant_id,
                store_id=store_id,
                platform="custom",
                allowed_origins=origins,
                ttl_days=request.collector_token_ttl_days,
            )
        except WebCollectorError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        await database.execute(
            """
            UPDATE merchant_stores
            SET name = :name,
                status = 'active',
                connected_at = CURRENT_TIMESTAMP
            WHERE store_id = :store_id
              AND merchant_id = :merchant_id
            """,
            {
                "store_id": store_id,
                "merchant_id": merchant_id,
                "name": store_name,
            },
        )
        reused_existing = True
    else:
        await database.execute(
            """
            INSERT INTO merchant_stores
                (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
            VALUES
                (:store_id, :merchant_id, 'custom', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)
            """,
            {
                "store_id": store_id,
                "merchant_id": merchant_id,
                "domain": storefront_origin,
                "name": store_name,
                "api_key": json.dumps(
                    {"collector_only": True, "credential_version": 1},
                    separators=(",", ":"),
                ),
            },
        )
        try:
            await database.execute(
                """
                UPDATE merchant_stores
                SET is_primary = TRUE
                WHERE store_id = :store_id
                  AND merchant_id = :merchant_id
                  AND NOT EXISTS (
                    SELECT 1 FROM merchant_stores
                    WHERE merchant_id = :merchant_id
                      AND store_id != :store_id
                      AND is_primary = TRUE
                      AND lower(COALESCE(status, '')) IN ('active', 'connected')
                  )
                """,
                {"store_id": store_id, "merchant_id": merchant_id},
            )
        except Exception:
            # Older local schemas can lack is_primary; connection remains valid.
            pass
        reused_existing = False

    base_url = resolve_public_api_base_url().rstrip("/")
    script_src = f"{base_url}/merchant-events/v1/collector.js"
    install_snippet = (
        f'<script async src="{script_src}" '
        f'data-pivota-token="{collector["token"]}" '
        'data-pivota-consent="pending"></script>'
    )
    return {
        "status": "success",
        "platform": "custom",
        "merchant_id": merchant_id,
        "store_id": store_id,
        "storefront_origin": storefront_origin,
        "allowed_origins": origins,
        "reused_existing": reused_existing,
        "collector_token": collector["token"],
        "collector_token_expires_at": collector["expires_at"],
        "collector_script_src": script_src,
        "install_snippet": install_snippet,
        "server_collector_path": "/merchant-events/v1/batch",
    }


@router.post("/shopify/connect")
async def merchant_connect_shopify(
    request: ConnectShopifyRequest,
    http_request: Request,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their Shopify store"""
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if not can_access_merchant(current_user, request.merchant_id):
        raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        # Validate shop domain and credentials.
        #
        # This used to check only that the string was non-empty, while the comment claimed to
        # validate it. Everything below builds a URL from it and sends CREDENTIALS there: the
        # client-credentials token exchange, and `GET https://<host>/admin/api/.../shop.json` with
        # an Admin token. An authenticated merchant could therefore point this repo's egress at any
        # host or port and read the outcome from the status echoed back in the 400 detail. The
        # shape check is also a PRECONDITION for the myshopify_domain re-check further down, whose
        # fallback is this value — falling back to an unvalidated host would defeat it.
        shop_domain = _validate_myshopify_domain(request.shop_domain)

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
                shop_domain=shop_domain,
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
        test_url = f"https://{shop_domain}/admin/api/2025-10/shop.json"
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
        canonical_myshopify_domain = (
            normalize_myshopify_domain(shop_info.get("myshopify_domain")) or shop_domain
        )
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
                "domain_input": shop_domain,
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
                sf_url = f"https://{canonical_myshopify_domain}/api/2025-10/graphql.json"
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

        # catalog_merchants.status (the public-recall gate) is derived from
        # merchant_stores now — connecting must flip it back to 'active' if a
        # prior disconnect had flipped it (#1648).
        await sync_catalog_merchant_status(request.merchant_id, reason="shopify_custom_app_connect")

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
                api_version="2025-10",
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
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    if not can_access_merchant(current_user, request.merchant_id):
        raise HTTPException(status_code=403, detail="Can only verify your own store")

    if not request.callback_base_url or not request.callback_base_url.strip():
        raise HTTPException(status_code=400, detail="callback_base_url is required")

    try:
        from services.shopify_integration_verify import verify_shopify_integration

        report = await verify_shopify_integration(
            merchant_id=request.merchant_id,
            callback_base_url=request.callback_base_url,
            api_version=request.api_version or "2025-10",
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
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = merchant_id or current_user.get("merchant_id")
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    if not can_access_merchant(current_user, target_merchant_id):
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
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = request.merchant_id or current_user.get("merchant_id")
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    if not can_access_merchant(current_user, target_merchant_id):
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

    # Pinned here -- before the credential lookup and outside the try below, so the refusal costs no
    # further work and is not reshaped by that block's broad handler. Both uses downstream build a
    # URL from this: the token refresh POSTs client credentials to {domain}/admin/oauth/access_token,
    # and fetch_products reads from the same host.
    sync_shop_domain = normalize_myshopify_domain(store.get("domain")) or ""
    if not sync_shop_domain:
        raise HTTPException(
            status_code=400,
            detail="Stored Shopify domain is not a *.myshopify.com host",
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
            shop_domain=sync_shop_domain,
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
                shop_domain=sync_shop_domain,
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
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if not can_access_merchant(current_user, request.merchant_id):
        raise HTTPException(status_code=403, detail="Can only connect your own store")
    
    try:
        instance_id = str(request.instance_id or "").strip()
        if instance_id:
            # Shape-checked with the SAME predicate the receiver uses, so an
            # id the receiver could never resolve is refused at the door
            # rather than persisted and silently ignored — and so nothing
            # carrying a SQL LIKE wildcard reaches the lookup below.
            if not is_wix_instance_id(instance_id):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "WIX_INSTANCE_ID_INVALID",
                        "message": "instance_id must be a Wix app instance id (8-64 chars, letters, digits and '-').",
                    },
                )
            # `merchant_stores` has no uniqueness on the instance id, and
            # `POST /webhooks/wix` resolves a store by nothing else. Without
            # this check merchant B could type merchant A's instance id and
            # start receiving A's signed order and refund events. First claim
            # wins; the second is a 409.
            claimed = await find_wix_stores_by_instance_id(database, instance_id)
            foreign = [
                row
                for row in claimed
                if str(row.get("merchant_id") or "") != str(request.merchant_id)
            ]
            if foreign:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "WIX_INSTANCE_ID_ALREADY_CLAIMED",
                        "message": "That Wix app instance is already connected to another merchant.",
                    },
                )

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
            """SELECT store_id, api_key FROM merchant_stores
               WHERE merchant_id = :merchant_id AND platform = 'wix' AND domain = :site_id""",
            {"merchant_id": request.merchant_id, "site_id": site_id}
        )

        if existing_store:
            # `merge_wix_credential`, not a bare write: a reconnect that does
            # not re-supply `instance_id` must not erase the one already
            # stored, or the merchant's telemetry goes dark at their next
            # credential rotation with no error anywhere. A store that never
            # had a blob and is not opting into telemetry still gets the bare
            # key, byte-identical to before.
            stored_credential = merge_wix_credential(
                dict(existing_store).get("api_key"),
                api_key=api_key,
                site_id=site_id,
                instance_id=instance_id,
            )
            # Update existing store
            await database.execute(
                """UPDATE merchant_stores
                   SET api_key = :token, status = 'active', connected_at = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"store_id": existing_store["store_id"], "token": stored_credential}
            )
            store_id = existing_store["store_id"]
        else:
            # Nothing stored yet: an instance id makes it the JSON blob every
            # Wix reader in this repo already understands
            # (`normalize_wix_api_key`, `extract_wix_site_id`,
            # `adapters/wix_adapter.py::extract_wix_order_credentials` all read
            # `api_key`/`site_id`/`instance_id` out of one); without one it is
            # the bare key, exactly as before.
            stored_credential = merge_wix_credential(
                None, api_key=api_key, site_id=site_id, instance_id=instance_id
            )
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
                    "token": stored_credential
                }
            )
        
        # See the Shopify connect path: catalog_merchants.status is derived from
        # merchant_stores now, so connecting has to re-open the door (#1648).
        await sync_catalog_merchant_status(request.merchant_id, reason="wix_connect")

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
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if not can_access_merchant(current_user, request.merchant_id):
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
                "webhook_secret": request.webhook_secret or request.consumer_secret,
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
                   SET api_key = :api_key, status = 'active', last_sync = CURRENT_TIMESTAMP,
                       connected_at = CURRENT_TIMESTAMP
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
            "store_id": store_id,
            "webhook_path": f"/webhooks/woocommerce/{store_id}",
            "webhook_subscription_path": (
                f"/integrations/woocommerce/{store_id}/webhooks/ensure"
            ),
            "required_webhook_topics": ["order.created", "order.updated"],
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting WooCommerce: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect WooCommerce: {str(e)}")


@router.post("/woocommerce/{store_id}/webhooks/ensure")
async def ensure_woocommerce_webhooks(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'woocommerce'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Connected WooCommerce store not found")
    store = dict(store)
    # The owning merchant is a property of the ROW here: `store_id` is
    # caller-supplied and the SELECT above keys on it alone.
    if not can_access_merchant(current_user, str(store.get("merchant_id") or "")):
        raise HTTPException(status_code=403, detail="Can only manage your own store")

    credentials = _woocommerce_credentials(store.get("api_key"))
    consumer_key = str(credentials.get("consumer_key") or "").strip()
    consumer_secret = str(credentials.get("consumer_secret") or "").strip()
    webhook_secret = str(
        credentials.get("webhook_secret") or consumer_secret
    ).strip()
    if not consumer_key or not consumer_secret or not webhook_secret:
        raise HTTPException(status_code=409, detail="WooCommerce API credentials are incomplete")

    from services.woocommerce_webhook_subscriptions import (
        WooCommerceWebhookSubscriptionError,
        ensure_woocommerce_subscriptions,
    )

    try:
        async with asyncio.timeout(90.0):
            async with _WOOCOMMERCE_WEBHOOK_INSTALL_CONCURRENCY:
                async with _woocommerce_webhook_install_lock(store_id):
                    result = await ensure_woocommerce_subscriptions(
                        store_url=str(store.get("domain") or ""),
                        consumer_key=consumer_key,
                        consumer_secret=consumer_secret,
                        webhook_secret=webhook_secret,
                        callback_url=_woocommerce_webhook_callback_url(store_id),
                        topics=("order.created", "order.updated"),
                    )
    except WooCommerceWebhookSubscriptionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="WooCommerce webhook management request failed",
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="WooCommerce webhook installation timed out",
        ) from exc
    return {"status": "success", "store_id": store_id, **result}


@router.post("/bigcommerce/connect")
async def merchant_connect_bigcommerce(
    request: ConnectBigCommerceRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their BigCommerce store"""
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if not can_access_merchant(current_user, request.merchant_id):
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
                   SET api_key = :api_key, status = 'active', last_sync = CURRENT_TIMESTAMP,
                       connected_at = CURRENT_TIMESTAMP
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
            "store_id": store_id,
            "webhook_path": f"/webhooks/bigcommerce/{store_id}",
            "webhook_subscription_path": (
                f"/integrations/bigcommerce/{store_id}/webhooks/ensure"
            ),
            "required_webhook_scopes": list(SUPPORTED_BIGCOMMERCE_SCOPES),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting BigCommerce: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to connect BigCommerce: {str(e)}")


@router.post("/bigcommerce/{store_id}/webhooks/ensure")
async def ensure_bigcommerce_webhooks(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Register (or re-sync) the BigCommerce hooks that feed the ledger.

    BigCommerce does not sign deliveries, so the receiver's credential is a
    per-store random secret carried in a header the hook itself declares. It is
    minted here on first use, persisted into the store's credential JSON, and
    NEVER returned: the response says which scopes and callback were installed,
    nothing more.
    """
    if current_user.get("role") not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'bigcommerce'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Connected BigCommerce store not found")
    store = dict(store)
    # The owning merchant is a property of the ROW: `store_id` is
    # caller-supplied and the SELECT above keys on it alone.
    if not can_access_merchant(current_user, str(store.get("merchant_id") or "")):
        raise HTTPException(status_code=403, detail="Can only manage your own store")

    credentials = _bigcommerce_credentials(store.get("api_key"))
    store_hash = str(credentials.get("store_hash") or "").strip()
    access_token = str(credentials.get("access_token") or "").strip()
    if not store_hash or not access_token:
        raise HTTPException(status_code=409, detail="BigCommerce API credentials are incomplete")

    webhook_secret = str(credentials.get("webhook_secret") or "").strip()
    minted_secret = not webhook_secret
    if minted_secret:
        webhook_secret = secrets.token_urlsafe(32)

    from services.bigcommerce_webhook_subscriptions import (
        BigCommerceWebhookSubscriptionError,
        ensure_bigcommerce_subscriptions,
    )

    callback_url = _bigcommerce_webhook_callback_url(store_id)
    if minted_secret:
        # Persist BEFORE registering: a hook that carries a secret the
        # receiver does not know would 401 every delivery.
        await database.execute(
            """
            UPDATE merchant_stores
            SET api_key = :api_key
            WHERE store_id = :store_id
            """,
            {
                "store_id": store_id,
                "api_key": json.dumps(
                    {**credentials, "webhook_secret": webhook_secret},
                    separators=(",", ":"),
                ),
            },
        )

        # Two first-time calls can race: each mints its own secret and the
        # last UPDATE wins. Hooks must carry the secret the RECEIVER holds, so
        # re-read the row and register with whatever actually persisted; the
        # loser's minted value is discarded, never registered.
        persisted = await database.fetch_one(
            "SELECT api_key FROM merchant_stores WHERE store_id = :store_id",
            {"store_id": store_id},
        )
        persisted_secret = str(
            _bigcommerce_credentials(dict(persisted).get("api_key") if persisted else None)
            .get("webhook_secret")
            or ""
        ).strip()
        if not persisted_secret:
            raise HTTPException(
                status_code=503, detail="BigCommerce webhook secret could not be persisted"
            )
        webhook_secret = persisted_secret
    try:
        async with asyncio.timeout(90.0):
            async with _BIGCOMMERCE_WEBHOOK_INSTALL_CONCURRENCY:
                result = await ensure_bigcommerce_subscriptions(
                    store_hash=store_hash,
                    access_token=access_token,
                    client_id=credentials.get("client_id"),
                    callback_url=callback_url,
                    secret=webhook_secret,
                    scopes=SUPPORTED_BIGCOMMERCE_SCOPES,
                )
    except BigCommerceWebhookSubscriptionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="BigCommerce webhook management request failed",
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="BigCommerce webhook installation timed out",
        ) from exc
    return {"status": "success", "store_id": store_id, **result}



# ---------------------------------------------------------------------------
# Squarespace. Two credential models that do NOT reach the same APIs: a per-site
# Developer API key reads the Orders API, and only an OAuth app token can create
# a webhook subscription. See docs/SQUARESPACE_TELEMETRY.md.
# ---------------------------------------------------------------------------


@router.post("/squarespace/connect")
async def merchant_connect_squarespace(
    request: ConnectSquarespaceRequest,
    current_user: dict = Depends(get_current_user),
):
    """Connect a Squarespace site for commerce telemetry.

    The API key is validated by calling `GET /1.0/authorization/website`, which
    is also the BINDING step: the website id it returns is persisted and every
    webhook delivery must name it. Without that binding a notification signed
    with some other Squarespace site's subscription secret could not be
    distinguished from this store's own.

    The key is never logged, and a reconnect READ-MODIFY-WRITEs the credential
    blob: the same cell holds the webhook secret (Pivota's only copy) and the
    reconciliation cursor, and overwriting it is exactly the PrestaShop P1 where
    re-entering a key silently disarmed a shop's telemetry.
    """
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    if not can_access_merchant(current_user, request.merchant_id):
        raise HTTPException(status_code=403, detail="Can only connect your own store")

    from services import squarespace_connection as squarespace
    from services.squarespace_connection import (
        SquarespaceConnectionError,
        serialize_squarespace_credentials,
    )

    api_key = request.api_key.strip()
    oauth_access_token = (request.oauth_access_token or "").strip() or None
    oauth_refresh_token = (request.oauth_refresh_token or "").strip() or None
    oauth_expires_at = (request.oauth_expires_at or "").strip() or None
    if not api_key:
        raise HTTPException(status_code=400, detail="A Squarespace API key is required")

    try:
        # Validated with the credential that will be used for reads: when an
        # OAuth token is supplied it is the one that also carries webhook
        # subscriptions, so it is the identity worth proving.
        website = await squarespace.fetch_squarespace_website(
            oauth_access_token or api_key
        )
    except SquarespaceConnectionError as exc:
        # The upstream status is part of the detail. That `GET
        # /1.0/authorization/website` answers a per-site API key at all is an
        # ASSUMED claim (docs/SQUARESPACE_TELEMETRY.md row 4); if it is wrong,
        # a 404 here says so immediately, while a bare "connection failed"
        # would look exactly like a mistyped key.
        raise HTTPException(
            status_code=400,
            detail=(
                f"Squarespace connection failed: {exc}"
                + (
                    f" (upstream HTTP {exc.status_code})"
                    if getattr(exc, "status_code", None)
                    else ""
                )
            ),
        ) from exc

    website_id = str(website.get("id") or "").strip()
    store_domain = (
        (request.domain or "").strip()
        or str(website.get("identifier") or "").strip()
        or f"squarespace:{website_id}"
    )
    store_name = (
        (request.store_name or "").strip()
        or str(website.get("title") or "").strip()
        or "Squarespace Store"
    )

    existing_store = await database.fetch_one(
        """SELECT store_id, api_key FROM merchant_stores
           WHERE merchant_id = :merchant_id AND platform = 'squarespace' AND domain = :domain""",
        {"merchant_id": request.merchant_id, "domain": store_domain},
    )

    def _reconnect(preserved: dict) -> dict:
        """The blob to persist, computed INSIDE the merge's critical section.

        This runs under the same row lock as the read and the write, which is
        the whole reason connect no longer hand-rolls its own read-modify-write:
        two of them meant two critical sections over one cell, and a sweep's
        cursor write landing between a connect's read and its write reverted
        the merchant's new credential.
        """
        previous_website_id = str(preserved.get("website_id") or "").strip()
        if previous_website_id and previous_website_id != website_id:
            # The credential now belongs to a DIFFERENT site. Everything issued
            # for or derived from the OLD site goes, and that INCLUDES the old
            # OAuth token: it is preferred over the API key on every read, so
            # leaving it behind makes the sweep keep listing the old site's
            # orders and record them under the store that now represents the
            # new one. The secret would authenticate deliveries the `websiteId`
            # bind then rejects, and the cursor is a high-water mark over
            # another site's orders. Everything else in the blob is preserved.
            for stale in (
                "webhook_secret",
                "webhook_subscription_id",
                "reconciliation",
                "oauth_access_token",
                "oauth_refresh_token",
                "oauth_expires_at",
            ):
                preserved.pop(stale, None)
        preserved.update({"api_key": api_key, "website_id": website_id})
        if oauth_access_token:
            preserved["oauth_access_token"] = oauth_access_token
            if oauth_refresh_token:
                preserved["oauth_refresh_token"] = oauth_refresh_token
            if oauth_expires_at:
                preserved["oauth_expires_at"] = oauth_expires_at
        return preserved

    if existing_store:
        existing_store = dict(existing_store)
        store_id = existing_store["store_id"]
        persisted = await squarespace.merge_squarespace_credentials(
            store_id=store_id,
            mutate=_reconnect,
            mark_connected=True,
            db=database,
        )
    else:
        store_id = f"store_{request.merchant_id[:8]}_{int(datetime.now().timestamp())}"
        blob = {"api_key": api_key, "website_id": website_id}
        if oauth_access_token:
            blob["oauth_access_token"] = oauth_access_token
            if oauth_refresh_token:
                blob["oauth_refresh_token"] = oauth_refresh_token
            if oauth_expires_at:
                blob["oauth_expires_at"] = oauth_expires_at
        persisted = dict(blob)
        await database.execute(
            """INSERT INTO merchant_stores
               (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
               VALUES (:store_id, :merchant_id, 'squarespace', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)""",
            {
                "store_id": store_id,
                "merchant_id": request.merchant_id,
                "domain": store_domain,
                "name": store_name,
                "api_key": serialize_squarespace_credentials(blob),
            },
        )

    logger.info(
        "squarespace_connect store_id=%s merchant_id=%s website_id=%s oauth=%s",
        store_id,
        request.merchant_id,
        website_id,
        bool(oauth_access_token),
    )
    return {
        "status": "success",
        "message": "Squarespace store connected successfully",
        "store_id": store_id,
        "website_id": website_id,
        "domain": store_domain,
        # Read off the BLOB THAT PERSISTED, not off the request field. A
        # reconnect that supplies no OAuth token still has webhooks if the
        # stored one survived, and answering `sweep_only` there would tell the
        # merchant their armed subscription is not armed.
        "telemetry_mode": (
            "webhook_and_sweep"
            if str(persisted.get("oauth_access_token") or "").strip()
            else "sweep_only"
        ),
        "webhook_path": f"/webhooks/squarespace/{store_id}",
        "webhook_subscription_path": (
            f"/integrations/squarespace/{store_id}/webhooks/ensure"
        ),
        "reconcile_path": f"/integrations/squarespace/{store_id}/reconcile",
    }


async def _squarespace_store_for_caller(store_id: str, current_user: dict) -> dict:
    """The store row, with the role gate and the ROW's ownership check applied.

    `store_id` is caller-supplied and the SELECT keys on it alone, so the owning
    merchant is a property of the ROW and nothing in the request says who it is.
    """
    if current_user.get("role") not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'squarespace'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Connected Squarespace store not found")
    store = dict(store)
    if not can_access_merchant(current_user, str(store.get("merchant_id") or "")):
        raise HTTPException(status_code=403, detail="Can only manage your own store")
    return store


@router.post("/squarespace/{store_id}/webhooks/ensure")
async def ensure_squarespace_webhooks(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Create the Squarespace subscription that feeds `/webhooks/squarespace/{id}`.

    Requires an OAuth access token on the store. The Webhook Subscriptions API
    is a Developer-Platform surface and a per-site Developer API key cannot
    create a subscription, so an API-key-only store is answered 409
    `oauth_required` and pointed at the reconciliation sweep. Faking a
    provisioning here would leave a store that reports telemetry as armed and
    receives nothing.

    The secret comes FROM Squarespace and is shown exactly once, which inverts
    the BigCommerce lifecycle: it is persisted after the create, re-read to
    learn whether this request's write won, and never returned or logged.
    """
    store = await _squarespace_store_for_caller(store_id, current_user)

    from services.squarespace_connection import (
        merge_squarespace_credentials,
        parse_squarespace_credentials,
    )
    from services.squarespace_event_adapter import (
        SQUARESPACE_ORDER_TOPICS,
        SQUARESPACE_UNINSTALL_TOPIC,
    )
    from services.squarespace_webhook_subscriptions import (
        SquarespaceWebhookSubscriptionError,
        delete_squarespace_subscription,
        ensure_squarespace_subscription,
    )

    credentials = parse_squarespace_credentials(store.get("api_key"))
    oauth_access_token = str(credentials.get("oauth_access_token") or "").strip()
    website_id = str(credentials.get("website_id") or "").strip()
    if not oauth_access_token:
        logger.info(
            "squarespace_webhooks_ensure action=oauth_required store_id=%s "
            "store_merchant_id=%s actor_role=%s actor_user_id=%s",
            store_id,
            store.get("merchant_id") or "-",
            current_user.get("role") or "-",
            current_user.get("sub") or "-",
        )
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "oauth_required",
                "message": (
                    "Squarespace webhook subscriptions require an OAuth access "
                    "token from a Squarespace Developer Platform app; a per-site "
                    "Developer API key cannot create one. This store's telemetry "
                    "runs through the reconciliation sweep instead: "
                    f"POST /integrations/squarespace/{store_id}/reconcile."
                ),
                "store_id": store_id,
                "reconcile_path": f"/integrations/squarespace/{store_id}/reconcile",
            },
        )
    if not website_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "This Squarespace store has no website_id binding; reconnect it "
                "so deliveries can be bound to the site they came from"
            ),
        )

    callback_url = _squarespace_webhook_callback_url(store_id)
    topics = list(SQUARESPACE_ORDER_TOPICS) + [SQUARESPACE_UNINSTALL_TOPIC]
    try:
        async with asyncio.timeout(90.0):
            async with _SQUARESPACE_WEBHOOK_INSTALL_CONCURRENCY:
                result = await ensure_squarespace_subscription(
                    access_token=oauth_access_token,
                    callback_url=callback_url,
                    topics=topics,
                )
    except SquarespaceWebhookSubscriptionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="Squarespace webhook management request failed"
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504, detail="Squarespace webhook installation timed out"
        ) from exc

    persisted = await merge_squarespace_credentials(
        store_id=store_id,
        updates={
            "webhook_secret": result.secret,
            "webhook_subscription_id": result.subscription_id,
        },
    )
    persisted_secret = str(persisted.get("webhook_secret") or "").strip()
    if not persisted_secret:
        # The subscription is live and the receiver holds no secret for it:
        # every delivery would 401. Undo the create rather than leave that.
        await _discard_squarespace_subscription(
            oauth_access_token, result.subscription_id, delete_squarespace_subscription
        )
        raise HTTPException(
            status_code=503, detail="Squarespace webhook secret could not be persisted"
        )
    if not hmac.compare_digest(persisted_secret, result.secret):
        # A concurrent ensure won the write. The receiver holds THAT secret, so
        # this request's subscription can never authenticate; discard it and
        # report the state that actually exists.
        await _discard_squarespace_subscription(
            oauth_access_token, result.subscription_id, delete_squarespace_subscription
        )
        # The same principal fields as `oauth_required` and `provisioned`. A
        # lost race is the one outcome where a subscription was created and
        # then deleted against Squarespace, so the audit trail needs to name
        # who caused that as much as it does for the other two.
        logger.info(
            "squarespace_webhooks_ensure action=lost_race store_id=%s "
            "store_merchant_id=%s actor_role=%s actor_user_id=%s "
            "discarded_subscription_id=%s",
            store_id,
            store.get("merchant_id") or "-",
            current_user.get("role") or "-",
            current_user.get("sub") or "-",
            result.subscription_id or "-",
        )
        return {
            "status": "success",
            "store_id": store_id,
            "endpoint": callback_url,
            "topics": topics,
            "secret_provisioned": True,
            "subscription_id": str(persisted.get("webhook_subscription_id") or ""),
        }

    logger.info(
        "squarespace_webhooks_ensure action=provisioned store_id=%s "
        "store_merchant_id=%s actor_role=%s actor_user_id=%s "
        "subscription_id=%s replaced=%s",
        store_id,
        store.get("merchant_id") or "-",
        current_user.get("role") or "-",
        current_user.get("sub") or "-",
        result.subscription_id or "-",
        len(result.replaced_subscription_ids),
    )
    return {
        "status": "success",
        "store_id": store_id,
        "endpoint": callback_url,
        "topics": result.topics,
        # The value itself is never returned and never logged: Pivota installs
        # the subscription, so no human needs to see it.
        "secret_provisioned": True,
        "subscription_id": result.subscription_id,
        "replaced_subscriptions": len(result.replaced_subscription_ids),
    }


async def _discard_squarespace_subscription(access_token, subscription_id, delete) -> None:
    """Best-effort removal of a subscription whose secret we cannot use.

    A failure here is logged and swallowed: the caller is already answering an
    error (or reporting the winner's state), and raising a second failure over
    the first would hide what actually happened.
    """
    try:
        await delete(access_token=access_token, subscription_id=subscription_id)
    except Exception:
        logger.warning(
            "squarespace_webhooks_ensure could not discard subscription_id=%s",
            subscription_id or "-",
        )


@router.post("/squarespace/{store_id}/reconcile")
async def run_squarespace_reconciliation(
    store_id: str,
    apply: bool = Query(default=True),
    overlap_minutes: int = Query(default=30, ge=0, le=1440),
    initial_lookback_days: int = Query(default=7, ge=1, le=90),
    max_pages: int = Query(default=20, ge=1, le=200),
    modified_before: Optional[str] = Query(default=None, max_length=64),
    current_user: dict = Depends(get_current_user),
):
    """Run the Orders-API reconciliation sweep for one store, now.

    This is the authenticated face of `scripts/sweep_squarespace_orders.py`.
    Both exist because CI deploys no Cloud Run job for this lane and the
    APScheduler lane runs on a service that is not deployed on merge — see the
    scheduling section of docs/SQUARESPACE_TELEMETRY.md.

    `modified_before` pins the window's end for ONE run: the operator escape
    hatch over the automatic bisect, for digging a store out of a range the
    page cap cannot read in one pass.

    Two guards wrap the sweep, both because it is an unbounded outbound loop
    over somebody else's API. A per-store lock answers 409 rather than letting
    two sweeps of the same store interleave — they would race on the same
    cursor cell, and the loser's pages would be re-read or skipped depending on
    which merge landed last. The timeout bounds the request itself: a sweep
    that pages 200 times against a slow upstream would otherwise hold the
    connection until the proxy gives up, with no cursor written either way.
    """
    store = await _squarespace_store_for_caller(store_id, current_user)

    from services.squarespace_order_sweep import (
        SquarespaceSweepError,
        sweep_squarespace_store,
    )

    resolved_store_id = str(store["store_id"])
    # Check-and-add with no `await` between them, so this is atomic on the
    # event loop without a lock of its own.
    if resolved_store_id in _SQUARESPACE_SWEEPS_IN_FLIGHT:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "sweep_already_running",
                "message": (
                    "A Squarespace reconciliation sweep is already running for "
                    "this store; wait for it to finish rather than racing it on "
                    "the same cursor."
                ),
                "store_id": resolved_store_id,
            },
        )
    _SQUARESPACE_SWEEPS_IN_FLIGHT.add(resolved_store_id)
    try:
        async with asyncio.timeout(SQUARESPACE_RECONCILE_TIMEOUT_SECONDS):
            return await sweep_squarespace_store(
                store_id=resolved_store_id,
                apply=apply,
                overlap_minutes=overlap_minutes,
                initial_lookback_days=initial_lookback_days,
                max_pages=max_pages,
                modified_before=modified_before,
            )
    except SquarespaceSweepError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TimeoutError as exc:
        # The cursor is untouched: the sweep persists it only after the whole
        # page loop completes, so a timeout re-reads rather than skips.
        raise HTTPException(
            status_code=504, detail="Squarespace reconciliation sweep timed out"
        ) from exc
    finally:
        _SQUARESPACE_SWEEPS_IN_FLIGHT.discard(resolved_store_id)

@router.post("/prestashop/connect")
async def merchant_connect_prestashop(
    request: ConnectPrestaShopRequest,
    current_user: dict = Depends(get_current_user)
):
    """Allow merchant to connect their PrestaShop store"""
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if not can_access_merchant(current_user, request.merchant_id):
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
        
        # Check if store already exists. `api_key` is SELECTed too, not just
        # `store_id`: that cell is a credential BLOB once telemetry has been
        # provisioned -- `{"api_key": ..., "webhook_secret": ...}` -- and the
        # telemetry secret is the only copy Pivota has (the merchant holds the
        # other, pasted into the module).
        existing_store = await database.fetch_one(
            """SELECT store_id, api_key FROM merchant_stores
               WHERE merchant_id = :merchant_id AND platform = 'prestashop' AND domain = :domain""",
            {"merchant_id": request.merchant_id, "domain": request.store_url}
        )

        if existing_store:
            # Update existing store. READ-MODIFY-WRITE, not an overwrite: this
            # UPDATE used to put the bare Webservice key over the whole cell,
            # which destroyed the `webhook_secret`
            # `ensure_prestashop_telemetry` had minted. After that the receiver
            # answered every signed module delivery with 401, and the module --
            # which cannot tell a rotated secret from an outage -- burned its
            # 20-attempt budget and dropped the events. Reconnecting a shop is
            # a routine thing to do; silently disarming its telemetry is not.
            existing_store = dict(existing_store)
            preserved = _prestashop_credentials(existing_store.get("api_key"))
            preserved.pop("api_key", None)
            stored_api_key = (
                json.dumps({"api_key": request.api_key, **preserved}, separators=(",", ":"))
                if preserved
                # Nothing to preserve: keep the plain-string shape this column
                # has always had for a store with no telemetry, so the generic
                # `parse_api_key` readers see exactly what they see today.
                else request.api_key
            )
            await database.execute(
                """UPDATE merchant_stores
                   SET api_key = :api_key, status = 'active', last_sync = CURRENT_TIMESTAMP,
                       connected_at = CURRENT_TIMESTAMP
                   WHERE store_id = :store_id""",
                {"store_id": existing_store["store_id"], "api_key": stored_api_key}
            )
            store_id = existing_store["store_id"]
        else:
            # Insert new store. No telemetry secret can exist for a store row
            # that does not exist yet, so the bare key is written as-is;
            # `ensure_prestashop_telemetry` migrates the cell to the blob shape
            # the first time it mints.
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


class EnsurePrestaShopTelemetryRequest(BaseModel):
    """Body of the PrestaShop telemetry provisioning call.

    `rotate` is the only knob: it mints a NEW secret and returns it once,
    which is what a merchant does after the old one leaked or after moving
    the shop.
    """

    rotate: bool = False


def _prestashop_credentials(raw: object) -> dict:
    """The credential JSON in `merchant_stores.api_key`.

    `merchant_connect_prestashop` persists the BARE Webservice key (a plain
    string, not JSON), so a store connected before telemetry existed is
    migrated here to `{"api_key": "<the bare key>"}` rather than having its
    key destroyed by the secret write.
    """
    if isinstance(raw, dict):
        return dict(raw)
    value = str(raw or "").strip()
    if not value:
        return {}
    if not value.startswith("{"):
        return {"api_key": value}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"api_key": value}
    return dict(parsed) if isinstance(parsed, dict) else {"api_key": value}


def _prestashop_telemetry_endpoint(store_id: str) -> str:
    return (
        f"{resolve_public_api_base_url().rstrip('/')}"
        f"/webhooks/prestashop/{quote(store_id, safe='')}"
    )


@router.post("/prestashop/{store_id}/telemetry/ensure")
async def ensure_prestashop_telemetry(
    store_id: str,
    request: EnsurePrestaShopTelemetryRequest = Body(default=None),
    current_user: dict = Depends(get_current_user),
):
    """Mint the shared secret the PrestaShop module signs its batches with.

    PrestaShop is the one platform where the secret has to reach a HUMAN:
    there is no OAuth handshake and no webhook API to register a callback
    through, so the merchant pastes this value into the module's configuration
    page. That forces a different lifecycle from `ensure_bigcommerce_webhooks`,
    which never returns its secret because the server installs the hooks
    itself:

    * the call that MINTS the secret returns it, exactly once;
    * every later call returns `secret_provisioned: true` and no secret;
    * `{"rotate": true}` mints a replacement and returns that one once.

    A merchant who loses the value rotates; there is no read-back path, and
    the secret is never logged.

    Because the secret LEAVES the system here, the two paths that return one
    (the first mint and a rotation) are held to `MERCHANT_OR_ADMIN_ROLES`, not
    to the whole staff tier `can_access_merchant` waves through: every employee
    role could otherwise mint a per-store signing credential for any merchant,
    or rotate a live one and silently sever a shop's telemetry until a human
    re-pastes it. The wider staff tier still reaches the read-only answer
    (`secret_provisioned: true/false`). Every call is logged with the actor,
    the target and what it did -- never the value.
    """
    if current_user.get("role") not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'prestashop'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Connected PrestaShop store not found")
    store = dict(store)
    # The owning merchant is a property of the ROW: `store_id` is
    # caller-supplied and the SELECT above keys on it alone.
    if not can_access_merchant(current_user, str(store.get("merchant_id") or "")):
        raise HTTPException(status_code=403, detail="Can only manage your own store")

    rotate = bool(request.rotate) if request is not None else False
    credentials = _prestashop_credentials(store.get("api_key"))
    existing_secret = str(credentials.get("webhook_secret") or "").strip()
    endpoint = _prestashop_telemetry_endpoint(store_id)

    def _audit(action: str) -> None:
        """One line per call: who, which store, what happened.

        This route mints, rotates and hands out a per-store SIGNING credential
        and staff roles can reach any merchant's store, so a call that leaves
        no trace is an untraceable credential event. The secret itself is
        never an argument here -- `secret_provisioned` is a boolean.
        """
        logger.info(
            "prestashop_telemetry_ensure action=%s store_id=%s store_merchant_id=%s "
            "actor_role=%s actor_user_id=%s actor_merchant_id=%s rotate_requested=%s "
            "secret_was_provisioned=%s",
            action,
            store_id,
            store.get("merchant_id") or "-",
            current_user.get("role") or "-",
            current_user.get("sub") or "-",
            current_user.get("merchant_id") or "-",
            rotate,
            bool(existing_secret),
        )

    # The secret-RETURNING paths are owner-or-admin only. Reading the state is
    # not: an employee troubleshooting a shop may still ask whether it is
    # provisioned, they just cannot cause a value to be handed out, and cannot
    # rotate one -- a rotation is a silent telemetry outage for the shop until
    # a human re-pastes the new value into the module.
    if (rotate or not existing_secret) and current_user.get("role") not in MERCHANT_OR_ADMIN_ROLES:
        _audit("denied_rotate" if rotate else "denied_mint")
        raise HTTPException(
            status_code=403,
            detail=(
                "Only the merchant or an admin may mint or rotate the "
                "PrestaShop telemetry secret"
            ),
        )

    if existing_secret and not rotate:
        # The secret exists and this call did not mint it, so it is not ours to
        # hand out again. Rotation is the only way back to a readable value.
        _audit("noop")
        return {
            "status": "success",
            "store_id": store_id,
            "endpoint": endpoint,
            "shop_url": store.get("domain"),
            "secret_provisioned": True,
        }

    minted = secrets.token_urlsafe(32)
    await database.execute(
        """
        UPDATE merchant_stores
        SET api_key = :api_key
        WHERE store_id = :store_id
        """,
        {
            "store_id": store_id,
            "api_key": json.dumps(
                {**credentials, "webhook_secret": minted}, separators=(",", ":")
            ),
        },
    )
    # Two first-time calls (or two rotations) can race and the last UPDATE
    # wins. The merchant must paste the secret the RECEIVER will hold, so
    # re-read the row and return whatever actually persisted; the loser's
    # minted value is discarded, never shown. `databases`+asyncpg reports no
    # rowcount from an UPDATE, so the re-read is also the only proof the write
    # landed at all.
    persisted = await database.fetch_one(
        "SELECT api_key FROM merchant_stores WHERE store_id = :store_id",
        {"store_id": store_id},
    )
    persisted_secret = str(
        _prestashop_credentials(dict(persisted).get("api_key") if persisted else None)
        .get("webhook_secret")
        or ""
    ).strip()
    if not persisted_secret:
        _audit("mint_failed")
        raise HTTPException(
            status_code=503, detail="PrestaShop telemetry secret could not be persisted"
        )
    _audit("rotated" if existing_secret else "minted")
    return {
        "status": "success",
        "store_id": store_id,
        "endpoint": endpoint,
        "shop_url": store.get("domain"),
        # The ONE response that carries it. Nothing logs this value.
        "secret": persisted_secret,
        "secret_provisioned": True,
        "rotated": bool(existing_secret),
    }



@router.post("/stores/support-email")
async def merchant_update_store_support_email(
    request: UpdateStoreSupportEmailRequest,
    current_user: dict = Depends(get_current_user),
):
    """Allow merchant to set a support email for review invitations."""
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = request.merchant_id or current_user.get("merchant_id")
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    if not can_access_merchant(current_user, target_merchant_id):
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
    if current_user["role"] not in MERCHANT_OR_EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    target_merchant_id = (merchant_id or "").strip() or (current_user.get("merchant_id") or "").strip()
    if not target_merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    if not can_access_merchant(current_user, target_merchant_id):
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
