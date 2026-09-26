from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import httpx

from db.database import database
from services.shopify_transactions_service import extract_shopify_access_token
from services.shopify_domain import normalize_myshopify_domain

logger = logging.getLogger(__name__)

_TOKEN_REFRESH_SKEW_SECONDS = 5 * 60
_DEFAULT_EXPIRES_IN_SECONDS = 24 * 3600


def _normalize_shop_domain(value: Optional[str]) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    return raw.replace("https://", "").replace("http://", "").strip("/").lower()


def _parse_credentials_blob(api_key_raw: Any) -> Dict[str, Any]:
    if isinstance(api_key_raw, dict):
        return dict(api_key_raw)
    if not isinstance(api_key_raw, str):
        return {}
    raw = api_key_raw.strip()
    if not raw.startswith("{"):
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _token_needs_refresh(creds: Dict[str, Any], current_token: Optional[str]) -> bool:
    client_id = str(creds.get("client_id") or "").strip()
    client_secret = str(creds.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return False
    if not current_token:
        return True

    now = datetime.now(timezone.utc)
    expires_at = _parse_iso_dt(creds.get("access_token_expires_at"))
    if expires_at is not None:
        skew = timedelta(seconds=_TOKEN_REFRESH_SKEW_SECONDS)
        return now >= (expires_at - skew)

    issued_at = _parse_iso_dt(creds.get("access_token_issued_at"))
    if issued_at is not None:
        return now >= (issued_at + timedelta(hours=23))

    # No metadata means we can't reason about expiry. Refresh once and persist metadata.
    return True


async def exchange_shopify_client_credentials_token(
    *,
    shop_domain: str,
    client_id: str,
    client_secret: str,
    timeout_s: float = 12.0,
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    normalized_domain = _normalize_shop_domain(shop_domain)
    cid = (client_id or "").strip()
    csec = (client_secret or "").strip()

    if not normalized_domain:
        return None, None, "shop_domain_missing"

    # THE CHOKEPOINT. This is the only place in the repo that POSTs client_id + client_secret, and
    # those credentials mint tokens for EVERY shop the app is installed on -- strictly worse than
    # the per-shop token the rest of this sweep protects. `_normalize_shop_domain` above only strips
    # a scheme; it lets any host through, so every caller of resolve_shopify_admin_access_token that
    # passes a raw merchant_stores.domain sent the app's client secret wherever that row pointed.
    #
    # Refusing HERE rather than at ~27 call sites, and refusing by RETURNING because that is this
    # function's existing failure channel -- callers read the reason out of `meta["refresh_error"]`
    # and carry on with whatever token they already had.
    pinned = normalize_myshopify_domain(normalized_domain)
    if not pinned:
        return None, None, "shop_domain_not_myshopify"
    normalized_domain = pinned
    if not cid or not csec:
        return None, None, "client_credentials_missing"

    url = f"https://{normalized_domain}/admin/oauth/access_token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": csec,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as e:
        return None, None, f"request_error:{type(e).__name__}"

    if resp.status_code != 200:
        # Keep error compact; never include secrets.
        detail = (resp.text or "")[:300].replace("\n", " ").strip()
        return None, None, f"http_{resp.status_code}:{detail}"

    try:
        data = resp.json() or {}
    except Exception:
        return None, None, "invalid_json"

    token = str(data.get("access_token") or "").strip()
    if not token:
        return None, None, "missing_access_token"

    expires_in_raw = data.get("expires_in")
    expires_in: Optional[int] = None
    if expires_in_raw is not None:
        try:
            parsed = int(expires_in_raw)
            if parsed > 0:
                expires_in = parsed
        except Exception:
            expires_in = None

    return token, expires_in, None


async def resolve_shopify_admin_access_token(
    *,
    shop_domain: Optional[str],
    api_key_raw: Any,
    store_id: Optional[str] = None,
    persist_refresh: bool = True,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Return a valid Shopify Admin API token.

    Behavior:
    - If only a static `access_token` is stored, returns it unchanged.
    - If `client_id/client_secret` are present and token metadata indicates expiry,
      refreshes token via client_credentials grant and persists updated credentials.
    """
    creds = _parse_credentials_blob(api_key_raw)
    current_token = extract_shopify_access_token(api_key_raw)
    normalized_domain = _normalize_shop_domain(shop_domain)

    meta: Dict[str, Any] = {
        "has_client_credentials": bool(str(creds.get("client_id") or "").strip() and str(creds.get("client_secret") or "").strip()),
        "refreshed": False,
    }

    if not _token_needs_refresh(creds, current_token):
        return current_token, meta

    client_id = str(creds.get("client_id") or "").strip()
    client_secret = str(creds.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return current_token, meta
    if not normalized_domain:
        meta["refresh_error"] = "shop_domain_missing"
        # Every caller discards meta (access_token, _ = ...), so this resolver is
        # the single observability point for a dead token refresh — otherwise it
        # surfaces only as opaque downstream Shopify 401s (audit fix #9).
        logger.warning(
            "Shopify token refresh skipped: shop_domain missing shop=%s store_id=%s",
            shop_domain,
            store_id,
        )
        return current_token, meta

    new_token, expires_in, refresh_error = await exchange_shopify_client_credentials_token(
        shop_domain=normalized_domain,
        client_id=client_id,
        client_secret=client_secret,
    )
    if not new_token:
        meta["refresh_error"] = refresh_error or "unknown_refresh_error"
        logger.warning(
            "Shopify token refresh failed shop=%s store_id=%s refresh_error=%s",
            shop_domain,
            store_id,
            meta["refresh_error"],
        )
        return current_token, meta

    now = datetime.now(timezone.utc)
    effective_expires_in = expires_in if expires_in and expires_in > 0 else _DEFAULT_EXPIRES_IN_SECONDS

    updated_creds = dict(creds)
    updated_creds["access_token"] = new_token
    updated_creds["access_token_issued_at"] = now.isoformat()
    updated_creds["access_token_expires_at"] = (now + timedelta(seconds=effective_expires_in)).isoformat()
    if expires_in:
        updated_creds["access_token_expires_in"] = int(expires_in)

    if persist_refresh and store_id:
        try:
            await database.execute(
                """
                UPDATE merchant_stores
                SET api_key = :api_key
                WHERE store_id = :store_id
                """,
                {"store_id": store_id, "api_key": json.dumps(updated_creds, ensure_ascii=False)},
            )
        except Exception as e:
            # Token is still usable for this request; persistence failure should not block flow.
            logger.warning(
                "Failed to persist refreshed Shopify token store_id=%s err=%s",
                store_id,
                str(e)[:200],
            )

    meta["refreshed"] = True
    meta["effective_expires_in"] = int(effective_expires_in)
    return new_token, meta
