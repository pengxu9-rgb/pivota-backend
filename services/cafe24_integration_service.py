from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

from adapters.cafe24_adapter import (
    build_cafe24_api_base,
    build_cafe24_headers,
    normalize_cafe24_mall_id,
)
from db.database import database


logger = logging.getLogger(__name__)


DEFAULT_CAFE24_SCOPES = (
    "mall.read_product,mall.read_order,mall.read_application,mall.write_application"
)


@dataclass(frozen=True)
class Cafe24OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str
    api_version: str
    webhook_api_key: str
    state_secret: str


def get_cafe24_oauth_config() -> Cafe24OAuthConfig:
    from config.settings import require_jwt_secret as _require_jwt_secret, settings

    return Cafe24OAuthConfig(
        client_id=os.getenv("CAFE24_CLIENT_ID", "").strip(),
        client_secret=os.getenv("CAFE24_CLIENT_SECRET", "").strip(),
        redirect_uri=os.getenv("CAFE24_REDIRECT_URI", "").strip(),
        scopes=os.getenv("CAFE24_SCOPES", DEFAULT_CAFE24_SCOPES).strip(),
        api_version=os.getenv("CAFE24_API_VERSION", "2026-03-01").strip(),
        webhook_api_key=os.getenv("CAFE24_WEBHOOK_API_KEY", "").strip(),
        state_secret=(
            os.getenv("CAFE24_OAUTH_STATE_SECRET", "").strip()
            or str(_require_jwt_secret() or "").strip()
        ),
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_cafe24_oauth_state(
    *,
    merchant_id: str,
    mall_id: str,
    secret: str,
    ttl_seconds: int = 10 * 60,
) -> str:
    if not secret:
        raise ValueError("Cafe24 OAuth state secret is not configured")
    payload = {
        "typ": "pivota_cafe24_oauth",
        "merchant_id": merchant_id,
        "mall_id": normalize_cafe24_mall_id(mall_id),
        "exp": int(time.time()) + max(60, int(ttl_seconds)),
        "nonce": secrets.token_urlsafe(18),
    }
    encoded = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64url(signature)}"


def verify_cafe24_oauth_state(state: str, *, secret: str) -> Dict[str, Any]:
    if not secret:
        raise ValueError("Cafe24 OAuth state secret is not configured")
    parts = str(state or "").split(".")
    if len(parts) != 2:
        raise ValueError("Invalid Cafe24 OAuth state")
    encoded, supplied_signature = parts
    expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    try:
        supplied = _b64url_decode(supplied_signature)
    except Exception as exc:
        raise ValueError("Invalid Cafe24 OAuth state") from exc
    if not hmac.compare_digest(expected, supplied):
        raise ValueError("Invalid Cafe24 OAuth state signature")
    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid Cafe24 OAuth state") from exc
    if not isinstance(payload, dict) or payload.get("typ") != "pivota_cafe24_oauth":
        raise ValueError("Invalid Cafe24 OAuth state")
    if int(payload.get("exp") or 0) < int(time.time()):
        raise ValueError("Cafe24 OAuth state has expired")
    if not payload.get("merchant_id") or not normalize_cafe24_mall_id(payload.get("mall_id")):
        raise ValueError("Cafe24 OAuth state is incomplete")
    return payload


def build_cafe24_authorization_url(*, mall_id: str, state: str, config: Cafe24OAuthConfig) -> str:
    normalized = normalize_cafe24_mall_id(mall_id)
    if not normalized or not config.client_id or not config.redirect_uri or not config.scopes:
        raise ValueError("Cafe24 OAuth is not configured")
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "state": state,
        "redirect_uri": config.redirect_uri,
        "scope": config.scopes,
    }
    return f"{build_cafe24_api_base(normalized)}/oauth/authorize?{urlencode(params)}"


async def exchange_cafe24_token(
    *,
    mall_id: str,
    config: Cafe24OAuthConfig,
    code: Optional[str] = None,
    refresh_token: Optional[str] = None,
) -> Dict[str, Any]:
    if not config.client_id or not config.client_secret:
        raise ValueError("Cafe24 OAuth client credentials are not configured")
    if bool(code) == bool(refresh_token):
        raise ValueError("Provide exactly one of code or refresh_token")
    basic = base64.b64encode(
        f"{config.client_id}:{config.client_secret}".encode("utf-8")
    ).decode("ascii")
    data: Dict[str, str]
    if code:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirect_uri,
        }
    else:
        data = {"grant_type": "refresh_token", "refresh_token": str(refresh_token)}
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{build_cafe24_api_base(mall_id)}/oauth/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=data,
        )
    if response.status_code != 200:
        raise ValueError(f"Cafe24 token exchange failed (HTTP {response.status_code})")
    payload = response.json() or {}
    if not isinstance(payload, dict) or not str(payload.get("access_token") or "").strip():
        raise ValueError("Cafe24 token response is missing access_token")
    return payload


def parse_cafe24_credentials(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


async def upsert_cafe24_store(
    *,
    merchant_id: str,
    mall_id: str,
    access_token: str,
    refresh_token: Optional[str],
    expires_at: Optional[str],
    refresh_token_expires_at: Optional[str],
    webhook_api_key: str,
    api_version: str,
    shop_no: int = 1,
    currency: str = "KRW",
) -> str:
    normalized = normalize_cafe24_mall_id(mall_id)
    if not normalized:
        raise ValueError("Cafe24 mall_id is required")
    domain = f"{normalized}.cafe24api.com"
    new_credentials = {
        "mall_id": normalized,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "refresh_token_expires_at": refresh_token_expires_at,
        "webhook_api_key": webhook_api_key,
        "api_version": api_version,
        "shop_no": max(1, int(shop_no or 1)),
        "currency": str(currency or "KRW").upper(),
    }
    existing = await database.fetch_one(
        """
        SELECT store_id, api_key FROM merchant_stores
        WHERE merchant_id = :merchant_id AND platform = 'cafe24' AND lower(domain) = :domain
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "domain": domain},
    )
    if existing:
        store_id = str(existing["store_id"])
        credential_blob = json.dumps(
            {
                **parse_cafe24_credentials(existing["api_key"]),
                **new_credentials,
            },
            separators=(",", ":"),
        )
        await database.execute(
            """
            UPDATE merchant_stores
            SET api_key = :api_key, status = 'active', connected_at = CURRENT_TIMESTAMP
            WHERE store_id = :store_id
            """,
            {"api_key": credential_blob, "store_id": store_id},
        )
        return store_id

    credential_blob = json.dumps(new_credentials, separators=(",", ":"))
    digest = hashlib.sha256(f"{merchant_id}:{normalized}".encode("utf-8")).hexdigest()[:18]
    store_id = f"store_c24_{digest}"
    await database.execute(
        """
        INSERT INTO merchant_stores
          (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
        VALUES
          (:store_id, :merchant_id, 'cafe24', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)
        """,
        {
            "store_id": store_id,
            "merchant_id": merchant_id,
            "domain": domain,
            "name": normalized,
            "api_key": credential_blob,
        },
    )
    return store_id


async def find_cafe24_store(mall_id: str) -> Optional[Dict[str, Any]]:
    normalized = normalize_cafe24_mall_id(mall_id)
    if not normalized:
        return None
    row = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key, status
        FROM merchant_stores
        WHERE platform = 'cafe24'
          AND lower(domain) = :domain
          AND lower(COALESCE(status, 'connected')) IN ('active', 'connected')
        ORDER BY connected_at DESC NULLS LAST
        LIMIT 1
        """,
        {"domain": f"{normalized}.cafe24api.com"},
    )
    if not row:
        return None
    result = dict(row)
    result["credentials"] = parse_cafe24_credentials(result.pop("api_key", None))
    return result


async def find_cafe24_store_by_id(store_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key, status
        FROM merchant_stores
        WHERE platform = 'cafe24'
          AND store_id = :store_id
          AND lower(COALESCE(status, 'connected')) IN ('active', 'connected')
        LIMIT 1
        """,
        {"store_id": str(store_id or "").strip()},
    )
    if not row:
        return None
    result = dict(row)
    result["credentials"] = parse_cafe24_credentials(result.pop("api_key", None))
    result["credentials"]["store_id"] = str(result["store_id"])
    return result


async def merge_cafe24_store_credentials(
    *,
    store_id: str,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge operational state without clobbering rotating OAuth credentials."""
    row = await database.fetch_one(
        "SELECT api_key FROM merchant_stores WHERE platform = 'cafe24' AND store_id = :store_id",
        {"store_id": store_id},
    )
    if not row:
        raise ValueError("Cafe24 store was not found")
    merged = {**parse_cafe24_credentials(row["api_key"]), **dict(updates or {})}
    merged.pop("store_id", None)
    await database.execute(
        "UPDATE merchant_stores SET api_key = :api_key WHERE platform = 'cafe24' AND store_id = :store_id",
        {"store_id": store_id, "api_key": json.dumps(merged, separators=(",", ":"))},
    )
    return merged


def _parse_expiry(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def resolve_cafe24_access_token(credentials: Dict[str, Any]) -> str:
    """Return a live access token, rotating Cafe24's single-use refresh token.

    A failed concurrent refresh re-reads the store row so the loser can adopt
    the winner's newly persisted token instead of disconnecting the store.
    """
    current = str(credentials.get("access_token") or "").strip()
    expiry = _parse_expiry(credentials.get("expires_at"))
    now = datetime.now(timezone.utc)
    if current and (expiry is None or (expiry - now).total_seconds() > 5 * 60):
        return current

    refresh_token = str(credentials.get("refresh_token") or "").strip()
    store_id = str(credentials.get("store_id") or "").strip()
    mall_id = normalize_cafe24_mall_id(credentials.get("mall_id"))
    if not refresh_token or not mall_id:
        return current

    stored_credentials: Dict[str, Any] = {}
    if store_id:
        stored_row = await database.fetch_one(
            "SELECT api_key FROM merchant_stores WHERE store_id = :store_id",
            {"store_id": store_id},
        )
        stored_credentials = parse_cafe24_credentials(
            stored_row["api_key"] if stored_row else None
        )

    config = get_cafe24_oauth_config()
    try:
        refreshed = await exchange_cafe24_token(
            mall_id=mall_id,
            config=config,
            refresh_token=refresh_token,
        )
    except Exception:
        if store_id:
            row = await database.fetch_one(
                "SELECT api_key FROM merchant_stores WHERE store_id = :store_id",
                {"store_id": store_id},
            )
            latest = parse_cafe24_credentials(row["api_key"] if row else None)
            latest_token = str(latest.get("access_token") or "").strip()
            if latest_token and latest_token != current:
                return latest_token
        raise

    merged = {
        **stored_credentials,
        **credentials,
        **refreshed,
        "mall_id": mall_id,
        "api_version": credentials.get("api_version") or config.api_version,
    }
    merged.pop("store_id", None)
    if store_id:
        await database.execute(
            "UPDATE merchant_stores SET api_key = :api_key WHERE store_id = :store_id",
            {
                "store_id": store_id,
                "api_key": json.dumps(merged, separators=(",", ":")),
            },
        )
    return str(refreshed["access_token"])


async def get_cafe24_webhook_reception_status(credentials: Dict[str, Any]) -> Dict[str, Any]:
    access_token = await resolve_cafe24_access_token(credentials)
    mall_id = normalize_cafe24_mall_id(credentials.get("mall_id"))
    api_version = str(credentials.get("api_version") or "2026-03-01")
    if not access_token or not mall_id:
        raise ValueError("Cafe24 credentials are incomplete")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{build_cafe24_api_base(mall_id)}/admin/webhooks/setting",
            headers=build_cafe24_headers(access_token, api_version),
        )
    if response.status_code != 200:
        raise ValueError(f"Cafe24 webhook setting lookup failed (HTTP {response.status_code})")
    payload = response.json() or {}
    setting = payload.get("webhook") or payload.get("setting") or payload
    return dict(setting) if isinstance(setting, dict) else {"raw_status": setting}


async def enable_cafe24_webhook_reception(credentials: Dict[str, Any]) -> Dict[str, Any]:
    """Activate reception for subscriptions already configured in App Setup.

    Cafe24's Admin API toggles reception but does not create event/URL
    subscriptions. Those remain app-level Developer Center configuration.
    """
    access_token = await resolve_cafe24_access_token(credentials)
    mall_id = normalize_cafe24_mall_id(credentials.get("mall_id"))
    api_version = str(credentials.get("api_version") or "2026-03-01")
    store_id = str(credentials.get("store_id") or "").strip()
    if not access_token or not mall_id:
        raise ValueError("Cafe24 credentials are incomplete")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.put(
            f"{build_cafe24_api_base(mall_id)}/admin/webhooks/setting",
            headers=build_cafe24_headers(access_token, api_version),
            json={"request": {"reception_status": "T"}},
        )
    if response.status_code not in {200, 201, 202}:
        raise ValueError(f"Cafe24 webhook activation failed (HTTP {response.status_code})")
    result = {
        "reception_status": "T",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "event_subscription_configuration": "developer_center_required",
    }
    if store_id:
        try:
            await merge_cafe24_store_credentials(
                store_id=store_id,
                updates={"webhook_reception": result},
            )
        except Exception:
            logger.exception("Failed to persist Cafe24 webhook reception state store_id=%s", store_id)
    return result
