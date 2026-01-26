from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from routes.agent_auth import AgentContext, get_agent_context
from db.checkout_intents import checkout_intents
from db.database import IS_POSTGRES, database, engine, metadata
import uuid
from utils.transient_errors import db_busy_http_exception, is_asyncpg_busy_error
from db.buyer_vault import buyer_addresses
from routes.accounts_orders_api import get_accounts_principal

from sqlalchemy.sql import func


router = APIRouter(prefix="/agent/v1/checkout", tags=["agent-checkout"])

async def _ensure_checkout_intents_table() -> None:
    """
    Best-effort self-healing for environments where migrations cannot be run manually.
    Safe to call multiple times (IF NOT EXISTS).
    """
    # Local dev/test: create via SQLAlchemy metadata so SQLite works without Postgres DDL.
    try:
        metadata.create_all(engine, tables=[checkout_intents])
    except Exception:
        pass

    # Production Postgres: best-effort schema drift healing (older table missing columns).
    if not IS_POSTGRES:
        return
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS agent_user_ref TEXT")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS requested_scopes JSONB")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS linked_buyer_id TEXT")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS used_at TIMESTAMPTZ")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS checkout_token_hash TEXT")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS prefill_read_count INTEGER NOT NULL DEFAULT 0")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS prefill_last_read_at TIMESTAMPTZ")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_checkout_intents_agent_buyer ON checkout_intents(agent_id, buyer_ref)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_checkout_intents_expires_at ON checkout_intents(expires_at)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_checkout_intents_expires_used ON checkout_intents(expires_at, used_at)")


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _checkout_token_secret() -> str:
    return (os.getenv("CHECKOUT_TOKEN_SECRET") or os.getenv("AGENT_CHECKOUT_TOKEN_SECRET") or "").strip()


def mint_checkout_token(payload: Dict[str, Any], ttl_seconds: int = 60 * 60) -> str:
    secret = _checkout_token_secret()
    if not secret:
        raise HTTPException(status_code=500, detail="Checkout token secret is not configured")

    now = int(time.time())
    exp = now + int(ttl_seconds)
    body = {
        "v": 1,
        "iat": now,
        "exp": exp,
        **payload,
    }

    payload_b64 = _base64url_encode(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = _base64url_encode(hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest())
    return f"v1.{payload_b64}.{sig}"


def _checkout_ui_base() -> str:
    return (os.getenv("CHECKOUT_UI_BASE_URL") or "https://agent.pivota.cc").rstrip("/")


def _constant_time_equals(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest(str(a or ""), str(b or ""))
    except Exception:
        return False


def _checkout_ui_key_expected() -> str:
    return (os.getenv("CHECKOUT_UI_KEY") or os.getenv("PIVOTA_CHECKOUT_UI_KEY") or "").strip()


def _require_checkout_ui_key(x_checkout_ui_key: Optional[str]) -> None:
    expected = _checkout_ui_key_expected()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "SERVER_ERROR", "message": "CHECKOUT_UI_KEY is not configured"},
        )
    if not x_checkout_ui_key or not _constant_time_equals(x_checkout_ui_key, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Not authorized for checkout UI"},
        )


def _sha256_hex(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()

def _coerce_datetime_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None

def _checkout_intent_ttl_seconds() -> int:
    raw = (os.getenv("CHECKOUT_INTENT_TTL_SECONDS") or "").strip()
    try:
        val = int(raw or 1800)
    except Exception:
        val = 1800
    # Clamp to a sane range: 5min .. 6h
    return max(300, min(val, 6 * 60 * 60))


def _checkout_token_ttl_seconds() -> int:
    raw = (os.getenv("CHECKOUT_TOKEN_TTL_SECONDS") or "").strip()
    try:
        val = int(raw or 0)
    except Exception:
        val = 0
    return val if val > 0 else _checkout_intent_ttl_seconds()


def _prefill_max_reads() -> int:
    raw = (os.getenv("CHECKOUT_PREFILL_MAX_READS") or "").strip()
    try:
        val = int(raw or 3)
    except Exception:
        val = 3
    return max(1, min(val, 20))


def _prefill_include_phone() -> bool:
    return (os.getenv("CHECKOUT_PREFILL_INCLUDE_PHONE") or "false").strip().lower() == "true"


def _normalize_intent_shipping_address(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or raw.get("recipient_name") or raw.get("recipientName") or "").strip() or None
    line1 = str(raw.get("address_line1") or raw.get("addressLine1") or raw.get("line1") or "").strip() or None
    line2 = str(raw.get("address_line2") or raw.get("addressLine2") or raw.get("line2") or "").strip() or None
    city = str(raw.get("city") or "").strip() or None
    state = str(raw.get("state") or raw.get("region") or raw.get("province") or "").strip() or None
    postal_code = str(raw.get("postal_code") or raw.get("postalCode") or raw.get("zip") or "").strip() or None
    country = str(raw.get("country") or "").strip().upper() or None
    phone = str(raw.get("phone") or "").strip() or None
    return {
        **({"name": name} if name else {}),
        **({"phone": phone} if phone else {}),
        **({"address_line1": line1} if line1 else {}),
        **({"address_line2": line2} if line2 else {}),
        **({"city": city} if city else {}),
        **({"state": state} if state else {}),
        **({"postal_code": postal_code} if postal_code else {}),
        **({"country": country} if country else {}),
    } or None


def _minimize_prefill_response(prefill: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(prefill, dict):
        return None
    email = str(prefill.get("customer_email") or "").strip() or None
    ship = prefill.get("shipping_address")
    ship_obj = ship if isinstance(ship, dict) else None

    minimized: Dict[str, Any] = {}
    if email:
        minimized["customer_email"] = email
    if ship_obj:
        allowed_keys = [
            "name",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
        ]
        if _prefill_include_phone():
            allowed_keys.append("phone")
        out_ship: Dict[str, Any] = {}
        for key in allowed_keys:
            value = ship_obj.get(key)
            if value is None:
                continue
            s = str(value).strip()
            if s:
                out_ship[key] = s
        if out_ship:
            minimized["shipping_address"] = out_ship
    return minimized or None


def _verify_checkout_ui_auth(
    token: str,
    *,
    aud: str,
    checkout_token: Optional[str] = None,
) -> None:
    """
    Verify short-lived UI-signed auth token to avoid sending static UI keys over the wire.

    Format:
      v1.<payload_b64url>.<sig_b64url>
    Payload:
      { v:1, typ:"checkout_ui_auth", aud:"prefill", iat, exp, cth:"<sha256hex(checkout_token)>" }
    """
    raw = (token or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Missing checkout UI auth"},
        )

    secret = _checkout_ui_key_expected()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "SERVER_ERROR", "message": "CHECKOUT_UI_KEY is not configured"},
        )

    parts = raw.split(".")
    if len(parts) == 3 and parts[0] == "v1":
        payload_b64, sig = parts[1], parts[2]
    elif len(parts) == 2:
        payload_b64, sig = parts
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Invalid checkout UI auth format"},
        )

    expected_sig = _base64url_encode(
        hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    )
    if not _constant_time_equals(sig, expected_sig):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Invalid checkout UI auth signature"},
        )

    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Invalid checkout UI auth payload"},
        )

    if not isinstance(payload, dict) or payload.get("typ") != "checkout_ui_auth":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Invalid checkout UI auth"},
        )
    if str(payload.get("aud") or "") != str(aud):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Invalid checkout UI auth audience"},
        )

    now = int(time.time())
    exp = int(payload.get("exp") or 0)
    if exp and now > exp:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Checkout UI auth expired"},
        )

    cth = str(payload.get("cth") or "").strip() or None
    if cth:
        if not checkout_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "UNAUTHENTICATED", "message": "X-Checkout-Token is required"},
            )
        if not _constant_time_equals(cth, _sha256_hex(checkout_token)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "FORBIDDEN", "message": "Checkout UI auth token mismatch"},
            )


def _require_checkout_ui_access(
    *,
    aud: str,
    x_checkout_ui_key: Optional[str],
    x_checkout_ui_auth: Optional[str],
    checkout_token: Optional[str],
) -> None:
    # Backward compatibility: accept static key, but prefer short-lived signed auth.
    if x_checkout_ui_auth:
        _verify_checkout_ui_auth(x_checkout_ui_auth, aud=aud, checkout_token=checkout_token)
        return
    _require_checkout_ui_key(x_checkout_ui_key)


def _address_row_to_checkout_shipping(addr: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": addr.get("recipient_name") or None,
        "address_line1": addr.get("line1") or None,
        "address_line2": addr.get("line2") or None,
        "city": addr.get("city") or None,
        "state": addr.get("region") or None,
        "postal_code": addr.get("postal_code") or None,
        "country": addr.get("country") or None,
        "phone": addr.get("phone") or None,
    }


class CheckoutIntentItem(BaseModel):
    product_id: str = Field(..., description="Platform product id")
    variant_id: Optional[str] = Field(None, description="Platform variant id (preferred when available)")
    sku: Optional[str] = None
    merchant_id: str = Field(..., description="Merchant id")
    title: Optional[str] = None
    quantity: int = Field(1, ge=1)
    unit_price: Optional[float] = None
    currency: Optional[str] = None
    image_url: Optional[str] = None


class CreateCheckoutIntentRequest(BaseModel):
    items: List[CheckoutIntentItem]
    return_url: Optional[str] = None
    buyer_ref: Optional[str] = None
    agent_user_ref: Optional[str] = None
    requested_scopes: Optional[List[str]] = None
    job_id: Optional[str] = None
    market: Optional[str] = None
    locale: Optional[str] = None
    source: Optional[str] = None
    customer_email: Optional[str] = None
    shipping_address: Optional[Dict[str, Any]] = None


@router.post("/intents")
async def create_checkout_intent(
    req: CreateCheckoutIntentRequest,
    context: AgentContext = Depends(get_agent_context),
):
    if not req.items:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "message": "items[] is required"})

    merchant_ids = sorted({str(it.merchant_id).strip() for it in req.items if str(it.merchant_id or "").strip()})
    if not merchant_ids:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "message": "items[] must include merchant_id"})
    if len(merchant_ids) > 1:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "MULTI_MERCHANT_NOT_SUPPORTED",
                "message": "Create one checkout intent per merchant_id (split the cart by merchant).",
                "merchant_ids": merchant_ids,
            },
        )

    for mid in merchant_ids:
        if not context.can_access_merchant(mid):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    buyer_ref = (req.buyer_ref or "").strip() or None
    agent_user_ref = (req.agent_user_ref or "").strip() or None
    job_id = (req.job_id or "").strip() or None
    market = (req.market or "").strip().upper() or None
    locale = (req.locale or "").strip().lower() or None
    source = (req.source or "").strip().lower() or None
    requested_scopes: Optional[List[str]] = None
    if isinstance(req.requested_scopes, list):
        requested_scopes = sorted({str(s).strip().lower() for s in req.requested_scopes if str(s or "").strip()}) or None

    intent_id = f"ci_{uuid.uuid4().hex}"
    expires_at_sec = int(time.time()) + _checkout_intent_ttl_seconds()
    expires_at_dt = datetime.fromtimestamp(expires_at_sec, tz=timezone.utc)

    prefill: Optional[Dict[str, Any]] = None
    if req.customer_email or req.shipping_address:
        prefill = {
            "customer_email": (req.customer_email or "").strip() or None,
            "shipping_address": _normalize_intent_shipping_address(req.shipping_address),
        }

    token_payload: Dict[str, Any] = {
        "agent_id": context.agent_id,
        "buyer_ref": buyer_ref,
        "job_id": job_id,
        "market": market,
        "locale": locale,
        "merchant_ids": merchant_ids,
        "scopes": ["checkout"],
        "intent_id": intent_id,
        # Bind items to the token (merchant-scoped enforcement is applied at auth;
        # item-level enforcement can be added later if needed).
        "items": [it.model_dump() for it in req.items],
    }
    if agent_user_ref:
        token_payload["agent_user_ref"] = agent_user_ref
    if requested_scopes:
        token_payload["requested_scopes"] = requested_scopes

    token = mint_checkout_token(token_payload, ttl_seconds=_checkout_token_ttl_seconds())

    # Best-effort: store intent server-side (prefill never goes into URLs/tokens).
    # If the table isn't migrated yet, we still return a usable checkout_url without prefill.
    try:
        await database.execute(
            checkout_intents.insert().values(
                intent_id=intent_id,
                agent_id=context.agent_id,
                buyer_ref=buyer_ref,
                agent_user_ref=agent_user_ref,
                expires_at=expires_at_dt,
                prefill=prefill,
                requested_scopes=requested_scopes,
                checkout_token_hash=_sha256_hex(token),
                prefill_read_count=0,
                prefill_last_read_at=None,
                linked_buyer_id=None,
                used_at=None,
            )
        )
    except Exception as e:
        if is_asyncpg_busy_error(e):
            raise db_busy_http_exception()
        try:
            await _ensure_checkout_intents_table()
            await database.execute(
                checkout_intents.insert().values(
                    intent_id=intent_id,
                    agent_id=context.agent_id,
                    buyer_ref=buyer_ref,
                    agent_user_ref=agent_user_ref,
                    expires_at=expires_at_dt,
                    prefill=prefill,
                    requested_scopes=requested_scopes,
                    checkout_token_hash=_sha256_hex(token),
                    prefill_read_count=0,
                    prefill_last_read_at=None,
                    linked_buyer_id=None,
                    used_at=None,
                )
            )
        except Exception as e2:
            if is_asyncpg_busy_error(e2):
                raise db_busy_http_exception()

    checkout_ui = _checkout_ui_base()
    # Keep items in query for backward compatibility (UI can still parse without decoding the token).
    items_param = json.dumps([it.model_dump() for it in req.items], ensure_ascii=False)
    query = {
        "checkout_token": token,
        "items": items_param,
    }
    if req.return_url:
        query["return"] = str(req.return_url)
    if market:
        query["market"] = market
    if locale:
        query["locale"] = locale
    if source:
        query["source"] = source
    if buyer_ref:
        query["buyer_ref"] = buyer_ref
    if job_id:
        query["job_id"] = job_id

    checkout_url = f"{checkout_ui}/order?{urlencode(query)}"

    return {
        "checkout_token": token,
        "checkout_url": checkout_url,
        "expires_at": expires_at_sec,
    }


@router.get("/prefill")
async def get_checkout_prefill(
    request: Request,
    response: Response,
    context: AgentContext = Depends(get_agent_context),
    x_checkout_ui_key: Optional[str] = Header(None, alias="X-Checkout-UI-Key"),
    x_checkout_ui_auth: Optional[str] = Header(None, alias="X-Checkout-UI-Auth"),
):
    """
    Return checkout prefill data for the current checkout token (if available).

    Auth:
    - X-Checkout-Token (preferred)
    """
    # User-specific; never cache.
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"

    raw_checkout_token = (request.headers.get("x-checkout-token") or "").strip() or None
    _require_checkout_ui_access(
        aud="prefill",
        x_checkout_ui_key=x_checkout_ui_key,
        x_checkout_ui_auth=x_checkout_ui_auth,
        checkout_token=raw_checkout_token,
    )

    payload = getattr(context, "checkout_token_payload", None)
    if not isinstance(payload, dict):
        # Disallow API-key access; this endpoint is checkout-UI-only.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "UNAUTHENTICATED", "message": "X-Checkout-Token is required"},
        )

    intent_id = str(payload.get("intent_id") or "").strip()
    if not intent_id:
        return {"prefill": None}

    try:
        row = await database.fetch_one(
            """
            SELECT
              prefill,
              expires_at,
              used_at,
              checkout_token_hash,
              prefill_read_count
            FROM checkout_intents
            WHERE intent_id = :intent_id AND agent_id = :agent_id
            LIMIT 1
            """,
            {"intent_id": intent_id, "agent_id": context.agent_id},
        )
    except Exception as e:
        if is_asyncpg_busy_error(e):
            raise db_busy_http_exception()
        # Best-effort self-heal for schema drift, then retry once.
        try:
            await _ensure_checkout_intents_table()
            row = await database.fetch_one(
                """
                SELECT
                  prefill,
                  expires_at,
                  used_at,
                  checkout_token_hash,
                  prefill_read_count
                FROM checkout_intents
                WHERE intent_id = :intent_id AND agent_id = :agent_id
                LIMIT 1
                """,
                {"intent_id": intent_id, "agent_id": context.agent_id},
            )
        except Exception:
            return {"prefill": None}

    if not row:
        return {"prefill": None}
    row = dict(row)

    # Validate intent lifetime and token binding.
    try:
        expires_at = _coerce_datetime_utc(row.get("expires_at"))
        if expires_at and expires_at.timestamp() < time.time():
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"error": "INTENT_EXPIRED", "message": "Checkout intent expired"},
            )
    except HTTPException:
        raise
    except Exception:
        pass

    if row.get("used_at"):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"error": "INTENT_USED", "message": "Checkout intent already used"},
        )

    stored_cth = str(row.get("checkout_token_hash") or "").strip() or None
    if stored_cth and raw_checkout_token and not _constant_time_equals(stored_cth, _sha256_hex(raw_checkout_token)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Checkout token mismatch"},
        )

    max_reads = _prefill_max_reads()
    try:
        reads = int(row.get("prefill_read_count") or 0)
    except Exception:
        reads = 0
    if reads >= max_reads:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"error": "PREFILL_EXHAUSTED", "message": "Checkout prefill is no longer available"},
        )
    # Best-effort read accounting (do not fail prefill if this update fails).
    try:
        await database.execute(
            checkout_intents.update()
            .where((checkout_intents.c.intent_id == intent_id) & (checkout_intents.c.agent_id == context.agent_id))
            .values(
                prefill_read_count=func.coalesce(checkout_intents.c.prefill_read_count, 0) + 1,
                prefill_last_read_at=func.now(),
                updated_at=func.now(),
            ),
        )
    except Exception:
        pass

    prefill_value = row.get("prefill")
    if isinstance(prefill_value, str):
        try:
            prefill_value = json.loads(prefill_value)
        except Exception:
            prefill_value = None
    prefill = prefill_value if isinstance(prefill_value, dict) else None

    # Buyer Vault merge: if buyer is logged in, prefer vault values over intent prefill.
    buyer_prefill: Optional[Dict[str, Any]] = None
    try:
        principal = await get_accounts_principal(request)
    except Exception:
        principal = None

    if principal:
        buyer_prefill = {"customer_email": principal.email, "shipping_address": None}
        try:
            addr_row = await database.fetch_one(
                buyer_addresses.select()
                .where((buyer_addresses.c.buyer_id == principal.user_id) & (buyer_addresses.c.is_default.is_(True)))
                .limit(1)
            )
            if addr_row:
                buyer_prefill["shipping_address"] = _address_row_to_checkout_shipping(dict(addr_row))
        except Exception:
            pass

    merged: Optional[Dict[str, Any]] = None
    if isinstance(prefill, dict):
        merged = dict(prefill)
    if buyer_prefill:
        if merged is None:
            merged = {}
        if buyer_prefill.get("customer_email"):
            merged["customer_email"] = buyer_prefill["customer_email"]
        if isinstance(buyer_prefill.get("shipping_address"), dict):
            merged["shipping_address"] = buyer_prefill["shipping_address"]

    return {"prefill": _minimize_prefill_response(merged)}
