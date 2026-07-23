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
from db.buyer_vault import buyer_addresses, buyer_identity_links, hash_agent_user_ref
from routes.accounts_orders_api import get_accounts_principal
from routes.agent_user_auth import AgentUserContext, get_agent_user_context
from services.commerce_execution_policy import (
    COMMERCE_PATH_PIVOTA_DIRECT_QUOTE_FIRST,
    SURFACE_EXTERNAL_PLATFORM_CHECKOUT,
    SURFACE_PUBLIC_AGENT_PURCHASE,
    resolve_commerce_execution_policy,
)
from services.quote_service import QuoteError, QuoteService
from services.traffic_taxonomy_service import attach_traffic_taxonomy, build_traffic_taxonomy
from services.tier2_acp_lane import resolve_acp_lane_decision
from services.pivota_acp_client import AcpClientError, create_checkout_session
from config.settings import settings

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
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS order_id TEXT")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS requested_scopes JSONB")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS linked_buyer_id TEXT")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS used_at TIMESTAMPTZ")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS checkout_token_hash TEXT")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS prefill_read_count INTEGER NOT NULL DEFAULT 0")
    await database.execute("ALTER TABLE checkout_intents ADD COLUMN IF NOT EXISTS prefill_last_read_at TIMESTAMPTZ")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_checkout_intents_agent_buyer ON checkout_intents(agent_id, buyer_ref)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_checkout_intents_order_id ON checkout_intents(order_id)")
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


async def _buyer_prefill_from_identity_link(*, agent_id: str, agent_user_ref: str) -> Optional[Dict[str, Any]]:
    ref = str(agent_user_ref or "").strip()
    if not ref:
        return None
    ref_hash = hash_agent_user_ref(ref)
    if not ref_hash:
        return None

    try:
        link_row = await database.fetch_one(
            """
            SELECT buyer_id
            FROM buyer_identity_links
            WHERE agent_id = :agent_id AND agent_user_ref_hash = :agent_user_ref_hash
            LIMIT 1
            """,
            {"agent_id": agent_id, "agent_user_ref_hash": ref_hash},
        )
    except Exception:
        link_row = None
    if not link_row:
        return None

    buyer_id = str(dict(link_row).get("buyer_id") or "").strip() or None
    if not buyer_id:
        return None

    try:
        await database.execute(
            buyer_identity_links.update()
            .where(
                (buyer_identity_links.c.agent_id == agent_id)
                & (buyer_identity_links.c.agent_user_ref_hash == ref_hash)
            )
            .values(last_seen_at=func.now(), updated_at=func.now())
        )
    except Exception:
        pass

    email: Optional[str] = None
    try:
        user_row = await database.fetch_one(
            "SELECT email FROM shop_users WHERE id = :id LIMIT 1",
            {"id": buyer_id},
        )
        if user_row:
            email = str(dict(user_row).get("email") or "").strip() or None
    except Exception:
        email = None

    shipping_address: Optional[Dict[str, Any]] = None
    try:
        addr_row = await database.fetch_one(
            buyer_addresses.select()
            .where((buyer_addresses.c.buyer_id == buyer_id) & (buyer_addresses.c.is_default.is_(True)))
            .limit(1)
        )
        if addr_row:
            shipping_address = _address_row_to_checkout_shipping(dict(addr_row))
    except Exception:
        shipping_address = None

    if not email and not shipping_address:
        return None
    return {
        "customer_email": email,
        "shipping_address": shipping_address,
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
    order_id: Optional[str] = None
    quote_id: Optional[str] = None
    buyer_ref: Optional[str] = None
    agent_user_ref: Optional[str] = None
    brief_id: Optional[str] = None
    brief_schema_version: Optional[str] = None
    requested_scopes: Optional[List[str]] = None
    job_id: Optional[str] = None
    market: Optional[str] = None
    locale: Optional[str] = None
    source: Optional[str] = None
    customer_email: Optional[str] = None
    shipping_address: Optional[Dict[str, Any]] = None


def _order_items_from_checkout_intent_items(items: List[CheckoutIntentItem]):
    from models.order import OrderItem

    converted = []
    for item in items or []:
        converted.append(
            OrderItem(
                product_id=str(item.product_id),
                product_title=item.title,
                variant_id=item.variant_id,
                sku=item.sku,
                quantity=int(item.quantity),
                unit_price=item.unit_price,
            )
        )
    return converted


def _single_checkout_merchant_or_raise(items: List[CheckoutIntentItem]) -> str:
    merchant_ids = sorted({str(it.merchant_id).strip() for it in items or [] if str(it.merchant_id or "").strip()})
    if not merchant_ids:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "message": "items[] must include merchant_id"})
    if len(merchant_ids) > 1:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "MULTI_MERCHANT_NOT_SUPPORTED",
                "message": "Create one checkout request per merchant_id (split the cart by merchant).",
                "merchant_ids": merchant_ids,
            },
        )
    return merchant_ids[0]


@router.post("/external-platform")
async def create_external_platform_checkout(
    req: CreateCheckoutIntentRequest,
    context: AgentContext = Depends(get_agent_context),
):
    """
    Create a merchant/platform-hosted checkout redirect without creating a Pivota order or PSP payment.

    This is the safe non-Shopify fallback when Pivota does not have a live quote adapter:
    the platform checkout remains responsible for final price, tax, shipping, payment,
    inventory availability, and inventory decrement.
    """
    if not req.items:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "message": "items[] is required"})
    merchant_id = _single_checkout_merchant_or_raise(req.items)
    if not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    from routes import order_routes as order_routes_module
    from services.merchant_store_service import get_primary_store

    store = await get_primary_store(merchant_id)
    platform = str((store or {}).get("platform") or "").strip().lower()
    policy = resolve_commerce_execution_policy(
        platform=platform,
        surface=SURFACE_EXTERNAL_PLATFORM_CHECKOUT,
    )

    if not policy.allows_external_redirect:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "EXTERNAL_PLATFORM_CHECKOUT_UNSUPPORTED",
                "message": "This store platform does not have a verified external checkout redirect path.",
                "merchant_id": merchant_id,
                "platform": platform or None,
                **policy.response_fields(),
            },
        )

    checkout_payload = await order_routes_module._get_platform_checkout_fallback_url_best_effort(
        merchant_id=merchant_id,
        items=_order_items_from_checkout_intent_items(req.items),
    )
    if not checkout_payload or not checkout_payload.get("url"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "EXTERNAL_PLATFORM_CHECKOUT_UNAVAILABLE",
                "message": "Pivota could not build a safe platform checkout URL for this cart shape. Use merchant checkout directly or add a platform live quote/checkout adapter.",
                "merchant_id": merchant_id,
                "platform": platform or None,
                "supported_semantics": "single_simple_product_redirect_only",
                **policy.response_fields(),
            },
        )

    return {
        "status": "requires_external_platform_checkout",
        "checkout_source": "external_platform_checkout",
        "checkout_url": str(checkout_payload["url"]),
        "platform": checkout_payload.get("platform") or platform,
        "method": checkout_payload.get("method"),
        "merchant_id": merchant_id,
        "creates_pivota_order": False,
        "creates_psp_payment": False,
        "requires_platform_checkout_validation": True,
        "quote_required_before_pivota_purchase": True,
        "inventory_guarantee": "not_guaranteed_by_pivota",
        "availability_status": "unknown_requires_validation",
        **policy.response_fields(),
        "pricing": {
            "price_source": "merchant_platform_checkout",
            "is_final": False,
            "finalized_by": "merchant_platform_checkout",
        },
        "warnings": [
            "Pivota did not create an order or PSP payment for this response.",
            "Final price, tax, shipping, payment, and inventory availability are validated by the merchant platform checkout.",
        ],
    }


class CreateAcpCheckoutRequest(BaseModel):
    items: List[CheckoutIntentItem]
    shipping_address: Optional[Dict[str, Any]] = None
    customer_email: Optional[str] = None
    pvt_click_id: Optional[str] = Field(None, description="Attribution click id to thread through the ACP session")
    pvt_surface: Optional[str] = Field(None, description="Origin surface (e.g. chatgpt)")
    source: Optional[str] = None
    # Store/platform override — selects WHICH of the merchant's connected stores
    # the lane resolves capability against, instead of the default primary store.
    # Only ever narrows to a store the merchant actually has connected (never
    # fabricates capability); a selector that matches no active store resolves to
    # the redirect floor. Chief use: exercise a specific platform's connector on a
    # multi-store merchant (e.g. the Wix store when a Shopify store is primary).
    store_id: Optional[str] = Field(None, description="Connected store_id to check out against (overrides primary store)")
    platform: Optional[str] = Field(None, description="Connected store platform to check out against, e.g. 'wix' (overrides primary store; store_id wins if both given)")


def _redirect_fallback(merchant_id: str, *, reason: str, platform: Optional[str] = None) -> Dict[str, Any]:
    """Uniform 'not this lane — use the redirect floor' response. Never an error:
    routing to the floor is a valid outcome (fail-open, never a dead end)."""
    return {
        "status": "requires_redirect_floor",
        "checkout_source": "redirect_floor",
        "lane": "redirect_floor",
        "reason": reason,
        "merchant_id": merchant_id,
        "platform": platform,
        "message": (
            "In-chat ACP checkout is not available for this merchant; fall back to "
            "the redirect floor (POST /agent/v1/checkout/external-platform or a "
            "merchant redirect)."
        ),
    }


@router.post("/acp")
async def create_acp_checkout(
    req: CreateAcpCheckoutRequest,
    context: AgentContext = Depends(get_agent_context),
):
    """P-T2.3.1b — initiate an in-chat ACP checkout SCREEN for an ACP-capable,
    charge-permitted merchant, else fall back to the redirect floor.

    DARK: this creates a pivota-acp session (screen/quote) only. It does NOT
    complete or charge — real capture is gated behind the kill-switch + P-T2.3.2+.
    By default (SUBMIT_PAYMENT off) the lane decision routes every merchant to the
    redirect floor, so this endpoint is inert until a merchant is canary-enabled.
    """
    if not req.items:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "message": "items[] is required"})
    merchant_id = _single_checkout_merchant_or_raise(req.items)
    if not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    # Capability + kill-switch gate (P-T2.3.1). Fail-open to redirect for anything
    # short of "ACP-capable AND charge-permitted for this merchant". An optional
    # store/platform override selects which connected store to resolve against
    # (else the primary store) — narrows only, never fabricates capability.
    decision = await resolve_acp_lane_decision(
        merchant_id,
        store_id=req.store_id,
        platform_override=req.platform,
    )
    if decision.is_native_handoff:
        # Mid-man three-tier routing (flag-dark, TIER2_NATIVE_HANDOFF_ENABLED):
        # Pivota cannot/may not charge, but the merchant's OWN checkout is a
        # verified settlement rail — a strictly better destination than the cold
        # redirect. Same response shape as the redirect fallback (agents that
        # only know that shape keep working) plus the rail so a rail-aware agent
        # can route the buyer to the merchant's native checkout.
        return {
            **_redirect_fallback(merchant_id, reason=decision.reason, platform=decision.platform),
            "status": "requires_native_checkout_handoff",
            "checkout_source": "native_handoff",
            "lane": "native_handoff",
            "settlement_rail": decision.settlement_rail,
            "message": (
                "In-chat charge is not available, but this merchant settles on "
                "their own checkout. Route the buyer there (the transaction "
                "completes merchant-side; Pivota passes it through)."
            ),
        }
    if not decision.is_acp:
        return _redirect_fallback(merchant_id, reason=decision.reason, platform=decision.platform)

    # Defense-in-depth: even when the kill-switch permits, do not touch the
    # pivota-acp integration unless it is explicitly enabled.
    if not settings.enable_platform_orders_acp:
        return _redirect_fallback(merchant_id, reason="acp_integration_disabled", platform=decision.platform)

    metadata: Dict[str, Any] = {
        "source": req.source or "agent_api",
        "commerce_surface": req.pvt_surface or "acp",
    }
    if req.pvt_click_id:
        metadata["pvt_click_id"] = req.pvt_click_id
    if req.pvt_surface:
        metadata["pvt_surface"] = req.pvt_surface

    try:
        session = await create_checkout_session(
            merchant_id=merchant_id,
            platform=decision.platform or "shopify",
            items=[it.model_dump() for it in req.items],
            fulfillment_address=req.shipping_address,
            buyer={"email": req.customer_email} if req.customer_email else None,
            metadata=metadata,
        )
    except AcpClientError as exc:
        # Fail-open to the redirect floor on any ACP error — never a dead end.
        return _redirect_fallback(
            merchant_id, reason=f"acp_unavailable:{exc.code}", platform=decision.platform
        )

    return {
        "status": "requires_in_chat_acp_checkout",
        "checkout_source": "acp_in_chat",
        "lane": "acp_in_chat",
        "protocol": "acp",
        "merchant_id": merchant_id,
        "platform": decision.platform,
        "acp_session_id": session.session_id,
        "checkout_url": session.checkout_url,
        "currency": session.currency,
        "totals": session.totals,
        "pvt_click_id": req.pvt_click_id,
        # DARK lane invariants — honest about what this does NOT do yet.
        "creates_pivota_order": False,
        "creates_psp_payment": False,
        "capture_enabled": False,
        "warnings": [
            "In-chat ACP checkout screen only: no order or charge is created here.",
            "Real capture is gated behind the kill-switch and is not yet enabled (P-T2.3.2+).",
        ],
    }


@router.post("/intents")
async def create_checkout_intent(
    req: CreateCheckoutIntentRequest,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
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
    requested_agent_user_ref = (req.agent_user_ref or "").strip() or None
    verified_agent_user_ref = str(agent_user.agent_user_ref or "").strip() if agent_user else ""
    if requested_agent_user_ref and verified_agent_user_ref and requested_agent_user_ref != verified_agent_user_ref:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "FORBIDDEN",
                "message": "agent_user_ref mismatch with verified identity",
            },
        )
    agent_user_ref = verified_agent_user_ref or requested_agent_user_ref or None
    brief_id = (req.brief_id or "").strip() or None
    brief_schema_version = (req.brief_schema_version or "").strip() or None
    job_id = (req.job_id or "").strip() or None
    market = (req.market or "").strip().upper() or None
    locale = (req.locale or "").strip().lower() or None
    source = (req.source or "").strip().lower() or None
    quote_id = (req.quote_id or "").strip() or None
    requested_scopes: Optional[List[str]] = None
    if isinstance(req.requested_scopes, list):
        requested_scopes = sorted({str(s).strip().lower() for s in req.requested_scopes if str(s or "").strip()}) or None

    # This endpoint mints a buyer-facing checkout surface. For raw item payloads,
    # require an active live quote so cached product price/inventory never becomes
    # purchase-authoritative. Existing-order checkout resume is allowed because the
    # order path already performed quote validation before creating any PSP surface.
    if not req.order_id and not quote_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "QUOTE_REQUIRED_BEFORE_PURCHASE",
                "message": "quote_id is required before creating a checkout intent from cart items",
            },
        )
    if quote_id:
        try:
            quote = await QuoteService().load_active_quote_or_raise(quote_id=quote_id)
        except QuoteError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": exc.code,
                    "message": exc.message,
                    **({"details": exc.details} if getattr(exc, "details", None) else {}),
                },
            )
        if str(quote.merchant_id or "").strip() != merchant_ids[0]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "QUOTE_MERCHANT_MISMATCH",
                    "message": "quote_id does not belong to checkout intent merchant_id",
                },
            )

    commerce_policy = None
    if not req.order_id:
        from services.merchant_store_service import get_primary_store

        store = await get_primary_store(merchant_ids[0])
        platform = str((store or {}).get("platform") or "").strip().lower()
        commerce_policy = resolve_commerce_execution_policy(
            platform=platform,
            surface=SURFACE_PUBLIC_AGENT_PURCHASE,
        )
        if commerce_policy.commerce_path != COMMERCE_PATH_PIVOTA_DIRECT_QUOTE_FIRST:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "UNSUPPORTED_COMMERCE_PATH",
                    "message": "This platform is not enabled for Pivota-direct checkout; use external platform checkout when available.",
                    "merchant_id": merchant_ids[0],
                    "platform": platform or None,
                    **commerce_policy.response_fields(),
                },
            )

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
        "quote_id": quote_id,
        **({"commerce_path": commerce_policy.commerce_path} if commerce_policy else {}),
        **({"execution_policy_version": commerce_policy.execution_policy_version} if commerce_policy else {}),
        # Bind items to the token (merchant-scoped enforcement is applied at auth;
        # item-level enforcement can be added later if needed).
        "items": [it.model_dump() for it in req.items],
    }
    if agent_user_ref:
        token_payload["agent_user_ref"] = agent_user_ref
    if brief_id:
        token_payload["brief_id"] = brief_id
        token_payload["brief_schema_version"] = brief_schema_version
    if requested_scopes:
        token_payload["requested_scopes"] = requested_scopes
    token_payload = attach_traffic_taxonomy(
        token_payload,
        build_traffic_taxonomy(
            token_payload,
            authenticated_agent_id=context.agent_id,
            caller_id=context.agent_id,
            default_source_channel=source,
            default_protocol_name="rest",
            default_commerce_surface="agent_api",
        ),
    )

    token = mint_checkout_token(token_payload, ttl_seconds=_checkout_token_ttl_seconds())

    # Best-effort: store intent server-side (prefill never goes into URLs/tokens).
    # If the table isn't migrated yet, we still return a usable checkout_url without prefill.
    try:
        await database.execute(
            checkout_intents.insert().values(
                intent_id=intent_id,
                agent_id=context.agent_id,
                order_id=(str(req.order_id or "").strip() or None),
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
                    order_id=(str(req.order_id or "").strip() or None),
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
    if quote_id:
        query["quote_id"] = quote_id
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
        "intent_id": intent_id,
        "checkout_session_id": intent_id,
        "checkout_token": token,
        "checkout_url": checkout_url,
        "expires_at": expires_at_sec,
        **(commerce_policy.response_fields() if commerce_policy else {}),
        **({"order_id": req.order_id} if req.order_id else {}),
        **({"quote_id": quote_id} if quote_id else {}),
        **({"brief_id": brief_id} if brief_id else {}),
        **({"brief_schema_version": brief_schema_version} if brief_schema_version else {}),
    }


async def _resolve_merchant_stripe_config(merchant_id: str) -> Optional[Dict[str, Any]]:
    """Resolve the merchant's Stripe PUBLISHABLE config for the checkout UI.

    Returns {publishable_key, stripe_account, environment} for the merchant's active Stripe row (prefer a
    live-classified row). Only the PUBLISHABLE key is returned — it is browser-safe by design (it is the key
    the checkout page initializes Stripe.js with). Surfaced on /prefill so the card form can mount on page
    load, before the quote/order, instead of waiting for the order-create response to carry the key. NEVER
    returns a secret key. Best-effort: any failure returns None and the page falls back to the order-create
    response for the key.
    """
    mid = str(merchant_id or "").strip()
    if not mid:
        return None
    try:
        from services.merchant_psp_config_service import (
            fetch_active_merchant_psps,
            normalize_psp_environment,
            _extract_public_key,
        )

        rows = await fetch_active_merchant_psps(merchant_id=mid, provider="stripe")
    except Exception:
        return None
    if not rows:
        return None

    def _env(r: Dict[str, Any]) -> str:
        key_for_env = r.get("runtime_secret_key") or r.get("secret_key") or r.get("api_key")
        return normalize_psp_environment("stripe", key_for_env, r.get("environment"))

    chosen = next((r for r in rows if _env(r) == "live"), None) or rows[0]
    pk = _extract_public_key(chosen.get("provider_config"))
    if not pk:
        return None
    return {
        "publishable_key": pk,
        "stripe_account": (str(chosen.get("account_id") or "").strip() or None),
        "environment": _env(chosen),
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
    else:
        token_agent_user_ref = str(payload.get("agent_user_ref") or "").strip() or None
        if token_agent_user_ref:
            buyer_prefill = await _buyer_prefill_from_identity_link(
                agent_id=context.agent_id,
                agent_user_ref=token_agent_user_ref,
            )

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

    # Surface the merchant's Stripe PUBLISHABLE config so the checkout page can mount the card form on load
    # (before the quote/order), instead of waiting for the order-create response to carry the key. Publishable
    # key only — browser-safe. Best-effort: never blocks prefill.
    stripe_config: Optional[Dict[str, Any]] = None
    try:
        merchant_ids = payload.get("merchant_ids")
        first_merchant = (
            str(merchant_ids[0]).strip()
            if isinstance(merchant_ids, list) and merchant_ids
            else ""
        )
        if first_merchant:
            stripe_config = await _resolve_merchant_stripe_config(first_merchant)
    except Exception:
        stripe_config = None

    return {
        "prefill": _minimize_prefill_response(merged),
        **({"stripe_config": stripe_config} if stripe_config else {}),
    }
