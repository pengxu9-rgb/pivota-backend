"""In-process ACP checkout-session layer (replaces the retired pivota-acp service).

The external `pivota-acp` Railway service is being retired; this module is the
DURABLE protocol execution layer the gateway's ACP door (and the Tier-2 routing
in routes/agent_checkout_intents) calls instead of an HTTP hop. No HTTP client
anywhere on this path — everything runs in-process:

- `create_session` builds a REAL Pivota quote (services.quote_service — the old
  service faked prices) and persists a `csn_*` session to `acp_checkout_sessions`
  with the old wire-shape `totals` (minor-unit entries incl. type=="total").
- `complete_session` is the money path. It ports the semantics of pivota-acp's
  `_real_capture.py` (quote → order(protocol) → pay) with in-process calls: the
  order is created through the existing order-create flow with
  `protocol_name`/`checkout_session_id` metadata (which engages the Tier-2
  kill-switch chain), and the charge runs through the SAME shared gate chain +
  off-session capture as POST /agent/v1/payments
  (services.acp_offsession_payment). Fail-closed with named errors; the old
  service's simulated-capture fallback (fake `payment_captured` webhook) is
  deliberately NOT ported — a failed capture is an error, never a fake success.
- `protocol_name` is a parameter (default "acp"): UCP/AP2 flows reuse this
  execution layer through the same GUARDED_PROTOCOLS gate chain.

Error vocabulary keeps the ACP public wire codes where the concept exists
(`acp_items_required`, `idempotency_conflict` on key reuse with a different
payload, 409 semantics) plus named in-process codes
(`acp_session_not_found`, `acp_session_expired`, `acp_quote_failed`, ...).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from config.settings import settings
from db.acp_checkout_sessions import acp_checkout_sessions
from db.database import IS_POSTGRES, database, engine, metadata
from services.commerce_attribution_service import (
    PVT_CLICK_ID,
    PVT_PRODUCT_ID,
    PVT_PROMPT_CLUSTER,
    PVT_SURFACE,
    PVT_VARIANT_ID,
    has_attribution_signal,
    materialize_attribution_context,
)
from services.quote_service import QuoteError, QuoteService
from utils.logger import logger

SESSION_ID_PREFIX = "csn_"
SESSION_TTL_SECONDS = 3600
STATUS_READY_FOR_PAYMENT = "ready_for_payment"
STATUS_COMPLETED = "completed"

# Old-service parity: the buyer email used when the session carries none
# (pivota-acp's real-capture hardcoded this on /orders/create).
DEFAULT_BUYER_EMAIL = "acp-buyer@pivota.cc"
# Old-service parity: the fallback shipping address used when the session has
# none (order create requires one; the retired service shipped this default).
DEFAULT_SHIPPING_ADDRESS: Dict[str, Any] = {
    "name": "Customer",
    "address_line1": "1 ACP Street",
    "city": "San Francisco",
    "postal_code": "94102",
    "country": "US",
}

# Keep in lockstep with routes/platform_orders_acp._ACP_ATTRIBUTION_KEYS: the
# pvt_* keys threaded into the session metadata for attribution parity.
_ACP_ATTRIBUTION_KEYS = (
    PVT_SURFACE,
    PVT_CLICK_ID,
    PVT_PRODUCT_ID,
    PVT_VARIANT_ID,
    PVT_PROMPT_CLUSTER,
)

# Private key inside the stored completion JSON: the request-body hash the
# completion was produced for (idempotency-conflict detection). Stripped from
# every replayed/returned result.
_COMPLETION_REQUEST_HASH_KEY = "_request_hash"


class AcpCheckoutSessionError(Exception):
    """A checkout-session operation failed. `status_code` is the HTTP status the
    route layer should translate to when it chooses to surface the error."""

    def __init__(self, message: str, *, code: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class AcpSessionResult:
    session_id: str
    checkout_url: str
    status: Optional[str]
    currency: Optional[str]
    total_cents: Optional[int]
    totals: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


def _checkout_url_base() -> str:
    return str(settings.agent_acp_checkout_url_base or "").rstrip("/")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_datetime_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _parse_json_field(value: Any) -> Any:
    """JSON columns come back as dict/list on Postgres but may be raw JSON
    strings via the `databases` driver on SQLite — parse defensively (same
    defensiveness as checkout_intents' prefill handling)."""
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


async def _ensure_acp_checkout_sessions_table() -> None:
    """Best-effort self-healing for environments where migrations cannot be run
    manually. Safe to call multiple times (IF NOT EXISTS). Mirrors migration
    191_acp_checkout_sessions.sql; local SQLite dev/tests create via SQLAlchemy
    metadata (pattern: routes/agent_checkout_intents._ensure_checkout_intents_table)."""
    try:
        metadata.create_all(engine, tables=[acp_checkout_sessions])
    except Exception:
        pass

    if not IS_POSTGRES:
        return
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS acp_checkout_sessions (
            id TEXT PRIMARY KEY,
            merchant_id TEXT NOT NULL,
            platform TEXT,
            status TEXT NOT NULL DEFAULT 'ready_for_payment',
            buyer JSONB,
            items JSONB NOT NULL,
            fulfillment_address JSONB,
            quote JSONB,
            metadata JSONB,
            currency TEXT,
            total_cents INTEGER,
            order_id TEXT,
            completion JSONB,
            idempotency_key TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ
        )
        """
    )
    await database.execute("ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS completion JSONB")
    await database.execute("ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS idempotency_key TEXT")
    await database.execute("ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS order_id TEXT")
    await database.execute("ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ")
    await database.execute("ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_acp_checkout_sessions_merchant_created "
        "ON acp_checkout_sessions (merchant_id, created_at)"
    )
    await database.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acp_checkout_sessions_merchant_idem "
        "ON acp_checkout_sessions (merchant_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )


def _normalize_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize cart items to the native in-process shape. Unlike the old HTTP
    client there is no lossy `{id, quantity}` mapping — product_id/variant_id/sku
    ride natively so /complete can build a real Pivota quote. A bare ACP `id`
    (the legacy acp-session door shape) is treated as a product_id."""
    out: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "").strip()
        variant_id = str(item.get("variant_id") or "").strip()
        sku = str(item.get("sku") or "").strip()
        legacy_id = str(item.get("id") or "").strip()
        if not product_id and legacy_id:
            product_id = legacy_id
        if not (product_id or variant_id or sku):
            continue
        qty = item.get("quantity")
        try:
            qty = int(qty) if qty is not None else 1
        except (TypeError, ValueError):
            qty = 1
        normalized: Dict[str, Any] = {"quantity": max(1, qty)}
        if product_id:
            normalized["product_id"] = product_id
        if variant_id:
            normalized["variant_id"] = variant_id
        if sku:
            normalized["sku"] = sku
        title = str(item.get("title") or item.get("product_title") or "").strip()
        if title:
            normalized["title"] = title
        out.append(normalized)
    return out


def _normalize_address(addr: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Accept either the ACP Address shape (line_one/line_two) or the Pivota
    checkout shape (address_line1/address_line2) and normalize to the Pivota
    shape. Returns None when there is no usable street line."""
    if not isinstance(addr, dict):
        return None
    a = dict(addr)
    line1 = (
        a.get("address_line1") or a.get("line_one") or a.get("line1") or a.get("addressLine1")
    )
    if not line1:
        return None
    line2 = a.get("address_line2") or a.get("line_two") or a.get("line2") or a.get("addressLine2")
    out: Dict[str, Any] = {
        "name": a.get("name") or "Customer",
        "address_line1": str(line1),
        "city": a.get("city"),
        "state": a.get("state") or a.get("region") or a.get("province"),
        "postal_code": a.get("postal_code") or a.get("postalCode") or a.get("zip"),
        "country": a.get("country"),
        "phone": a.get("phone"),
    }
    if line2:
        out["address_line2"] = str(line2)
    return {k: v for k, v in out.items() if v is not None}


def _build_session_metadata(
    *,
    merchant_id: str,
    platform: str,
    metadata: Optional[Dict[str, Any]],
    buyer: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Attribution semantics moved from services/pivota_acp_client
    .create_checkout_session: materialize pvt_* into the session metadata (so
    /complete deposits an attributed edge), pass caller metadata through
    non-destructively, and preserve the buyer email even when no buyer object is
    stored."""
    acp_metadata: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "platform": platform,
    }
    src = dict(metadata or {})
    if has_attribution_signal(src):
        attribution = materialize_attribution_context(
            src, default_surface=str(src.get(PVT_SURFACE) or "acp"), merchant_id=merchant_id
        )
        for key in _ACP_ATTRIBUTION_KEYS:
            value = attribution.get(key)
            if value:
                acp_metadata[key] = str(value)
    for k, v in src.items():
        acp_metadata.setdefault(k, v)

    if isinstance(buyer, dict) and buyer.get("email"):
        acp_metadata.setdefault("customer_email", buyer["email"])
    return acp_metadata


def _money_minor(value: Any) -> int:
    try:
        return int(
            (Decimal(str(value if value is not None else "0")) * 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    except Exception:
        return 0


def _totals_from_pricing(pricing: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Old wire shape: a list of `{"type", "display_text", "amount"}` entries in
    minor units, including one entry with type == "total" — consumers
    (agent_checkout_intents' callers, the old client's parser) rely on exactly
    that."""
    subtotal = _money_minor((pricing or {}).get("subtotal"))
    discount = _money_minor((pricing or {}).get("discount_total"))
    shipping = _money_minor((pricing or {}).get("shipping_fee"))
    tax = _money_minor((pricing or {}).get("tax"))
    total = _money_minor((pricing or {}).get("total"))
    totals: List[Dict[str, Any]] = [
        {"type": "items_base_amount", "display_text": "Item(s) total", "amount": subtotal + discount},
        {"type": "subtotal", "display_text": "Subtotal", "amount": subtotal},
    ]
    if discount:
        totals.append({"type": "discount", "display_text": "Discount", "amount": -discount})
    totals.append({"type": "fulfillment", "display_text": "Fulfillment", "amount": shipping})
    totals.append({"type": "tax", "display_text": "Tax", "amount": tax})
    totals.append({"type": "total", "display_text": "Total", "amount": total})
    return totals


async def _build_quote(
    *,
    merchant_id: str,
    items: List[Dict[str, Any]],
    customer_email: Optional[str],
    shipping_address: Optional[Dict[str, Any]],
    agent_id: Optional[str],
) -> Dict[str, Any]:
    quote_items = [
        {
            "product_id": it.get("product_id"),
            "variant_id": it.get("variant_id"),
            "quantity": it.get("quantity", 1),
        }
        for it in items
    ]
    try:
        return await QuoteService().preview_quote(
            merchant_id=merchant_id,
            agent_id=agent_id,
            items=quote_items,
            discount_codes=None,
            customer_email=customer_email,
            shipping_address=shipping_address,
            selected_delivery_option=None,
        )
    except QuoteError as exc:
        raise AcpCheckoutSessionError(
            f"quote_failed:{exc.code}: {exc.message}",
            code="acp_quote_failed",
            status_code=502,
        ) from exc
    except AcpCheckoutSessionError:
        raise
    except Exception as exc:  # noqa: BLE001 — any pricing failure is a named quote failure
        raise AcpCheckoutSessionError(
            f"quote_failed: {str(exc)[:200]}",
            code="acp_quote_failed",
            status_code=502,
        ) from exc


async def create_session(
    *,
    merchant_id: str,
    platform: str,
    items: List[Dict[str, Any]],
    fulfillment_address: Optional[Dict[str, Any]] = None,
    buyer: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    agent_id: Optional[str] = None,
    protocol_name: str = "acp",
) -> AcpSessionResult:
    """Create an in-process ACP checkout session: quote the cart with the REAL
    quote engine, persist the session, and return the old wire-shape descriptor.
    Does NOT complete or charge. Raises AcpCheckoutSessionError on failure."""
    normalized_items = _normalize_items(items)
    if not normalized_items:
        raise AcpCheckoutSessionError(
            "items_required", code="acp_items_required", status_code=400
        )

    acp_metadata = _build_session_metadata(
        merchant_id=merchant_id, platform=platform, metadata=metadata, buyer=buyer
    )
    acp_metadata.setdefault("protocol_name", str(protocol_name or "acp"))
    address = _normalize_address(fulfillment_address)
    customer_email = (
        (buyer or {}).get("email")
        or acp_metadata.get("customer_email")
        or None
    )

    quote = await _build_quote(
        merchant_id=merchant_id,
        items=normalized_items,
        customer_email=customer_email,
        shipping_address=address,
        agent_id=agent_id,
    )

    currency = str(quote.get("currency") or "USD")
    totals = _totals_from_pricing(quote.get("pricing") or {})
    total_obj = next((t for t in totals if t.get("type") == "total"), None)
    total_cents = int(total_obj["amount"]) if total_obj else None

    session_id = f"{SESSION_ID_PREFIX}{uuid.uuid4().hex[:14]}"
    now = _now_utc()
    expires_at = now + timedelta(seconds=SESSION_TTL_SECONDS)

    quote_snapshot = {
        "quote_id": quote.get("quote_id"),
        "expires_at": str(quote.get("expires_at") or ""),
        "engine": quote.get("engine"),
        "currency": currency,
        "pricing": {
            k: str(v)
            for k, v in (quote.get("pricing") or {}).items()
        },
        "line_items": quote.get("line_items") or [],
        "totals": totals,
    }

    values = {
        "id": session_id,
        "merchant_id": merchant_id,
        "platform": str(platform or "") or None,
        "status": STATUS_READY_FOR_PAYMENT,
        "buyer": buyer or None,
        "items": normalized_items,
        "fulfillment_address": address,
        "quote": quote_snapshot,
        "metadata": acp_metadata,
        "currency": currency,
        "total_cents": total_cents,
        "created_at": now,
        "updated_at": now,
        "expires_at": expires_at,
    }
    try:
        await database.execute(acp_checkout_sessions.insert().values(**values))
    except Exception:
        try:
            await _ensure_acp_checkout_sessions_table()
            await database.execute(acp_checkout_sessions.insert().values(**values))
        except Exception as exc:  # noqa: BLE001 — a session we cannot persist must not be handed out
            raise AcpCheckoutSessionError(
                f"session_persist_failed: {str(exc)[:200]}",
                code="acp_session_persist_failed",
                status_code=503,
            ) from exc

    raw = {
        "id": session_id,
        "buyer": buyer or None,
        "payment_provider": {"provider": "stripe", "supported_payment_methods": ["card"]},
        "status": STATUS_READY_FOR_PAYMENT,
        "currency": currency,
        "line_items": quote.get("line_items") or [],
        "fulfillment_address": address,
        "fulfillment_options": quote.get("delivery_options") or [],
        "fulfillment_option_id": None,
        "totals": totals,
        "messages": [],
        "links": [],
    }
    return AcpSessionResult(
        session_id=session_id,
        checkout_url=f"{_checkout_url_base()}/{session_id}",
        status=STATUS_READY_FOR_PAYMENT,
        currency=currency,
        total_cents=total_cents,
        totals=totals,
        raw=raw,
    )


def _row_to_session(row: Any) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    session = dict(row)
    for key in ("buyer", "items", "fulfillment_address", "quote", "metadata", "completion"):
        session[key] = _parse_json_field(session.get(key))
    return session


async def peek_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the raw session row WITHOUT expiry filtering (route layer uses it
    for the merchant-access check before completion; complete_session applies
    the expiry semantics itself)."""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    try:
        row = await database.fetch_one(
            acp_checkout_sessions.select().where(acp_checkout_sessions.c.id == sid).limit(1)
        )
    except Exception:
        try:
            await _ensure_acp_checkout_sessions_table()
            row = await database.fetch_one(
                acp_checkout_sessions.select().where(acp_checkout_sessions.c.id == sid).limit(1)
            )
        except Exception:
            return None
    return _row_to_session(row)


def _is_expired(session: Dict[str, Any]) -> bool:
    expires_at = _coerce_datetime_utc(session.get("expires_at"))
    return bool(expires_at and expires_at <= _now_utc())


async def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a session; an expired session is treated as absent."""
    session = await peek_session(session_id)
    if not session:
        return None
    if _is_expired(session):
        return None
    return session


def _request_hash(payment_token: Optional[str]) -> str:
    body = json.dumps({"payment_token": payment_token or None}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _public_completion(completion: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in (completion or {}).items() if not str(k).startswith("_")}


def _order_permalink_url(order_id: str) -> str:
    base = str(os.getenv("PIVOTA_BACKEND_BASE_URL") or "").rstrip("/")
    return f"{base}/agent/v1/orders/{order_id}"


async def _create_pivota_order(
    *,
    session: Dict[str, Any],
    quote: Dict[str, Any],
    agent_context: Any,
    protocol_name: str,
    idempotency_key: Optional[str],
) -> str:
    """Create the Pivota order through the existing order-create flow (the same
    core the retired service drove over HTTP), in-process. Order metadata
    carries protocol_name + checkout_session_id (this is what engages the
    Tier-2 kill-switch chain on the charge) plus the session's pvt_* keys."""
    from fastapi import BackgroundTasks, HTTPException

    from models.order import CreateOrderRequest, OrderItem, ShippingAddress
    from routes.agent_api import agent_create_order

    session_metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    order_metadata: Dict[str, Any] = {
        "protocol_name": protocol_name,
        "checkout_session_id": str(session.get("id")),
    }
    for key in _ACP_ATTRIBUTION_KEYS:
        value = (session_metadata or {}).get(key)
        if value:
            order_metadata[key] = str(value)

    line_items = quote.get("line_items") or []

    def _line_for(product_id: str, variant_id: str, idx: int) -> Dict[str, Any]:
        for li in line_items:
            if not isinstance(li, dict):
                continue
            if str(li.get("product_id") or "") == product_id and str(li.get("variant_id") or "") == variant_id:
                return li
        if idx < len(line_items) and isinstance(line_items[idx], dict):
            return line_items[idx]
        return {}

    order_items: List[OrderItem] = []
    for idx, item in enumerate(session.get("items") or []):
        product_id = str((item or {}).get("product_id") or "").strip()
        variant_id = str((item or {}).get("variant_id") or "").strip()
        qty = int((item or {}).get("quantity") or 1)
        li = _line_for(product_id, variant_id, idx)
        unit_price = li.get("unit_price_effective") or li.get("unit_price_original")
        order_items.append(
            OrderItem(
                product_id=product_id,
                product_title=str(item.get("title") or item.get("sku") or product_id),
                variant_id=variant_id or None,
                sku=(str(item.get("sku") or "").strip() or None),
                quantity=qty,
                unit_price=(Decimal(str(unit_price)) if unit_price is not None else None),
            )
        )

    address = _normalize_address(session.get("fulfillment_address")) or dict(DEFAULT_SHIPPING_ADDRESS)
    # ShippingAddress requires city/postal_code/country — backfill from the
    # old-service default rather than failing an otherwise-chargeable session.
    for key, fallback in DEFAULT_SHIPPING_ADDRESS.items():
        address.setdefault(key, fallback)

    buyer = session.get("buyer") if isinstance(session.get("buyer"), dict) else {}
    customer_email = (
        (buyer or {}).get("email")
        or (session_metadata or {}).get("customer_email")
        or DEFAULT_BUYER_EMAIL
    )

    order_request = CreateOrderRequest(
        merchant_id=str(session.get("merchant_id")),
        customer_email=str(customer_email),
        quote_id=str(quote.get("quote_id")),
        items=order_items,
        shipping_address=ShippingAddress(**address),
        currency=str(quote.get("currency") or session.get("currency") or "USD"),
        metadata=order_metadata,
        idempotency_key=(idempotency_key or None),
    )

    try:
        created = await agent_create_order(
            order_request=order_request,
            background_tasks=BackgroundTasks(),
            context=agent_context,
        )
    except HTTPException as exc:
        raise AcpCheckoutSessionError(
            f"order_create_failed: {str(exc.detail)[:300]}",
            code="acp_order_create_failed",
            status_code=502,
        ) from exc
    except AcpCheckoutSessionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AcpCheckoutSessionError(
            f"order_create_failed: {str(exc)[:300]}",
            code="acp_order_create_failed",
            status_code=502,
        ) from exc

    payload = created.model_dump() if hasattr(created, "model_dump") else dict(created or {})
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        raise AcpCheckoutSessionError(
            "order_create_returned_no_order_id",
            code="acp_order_create_failed",
            status_code=502,
        )
    return order_id


async def complete_session(
    *,
    session_id: str,
    agent_context: Any,
    payment_token: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    protocol_name: str = "acp",
) -> Dict[str, Any]:
    """Complete a checkout session: quote → order(protocol) → off-session charge
    through the SAME Tier-2 gate chain as POST /agent/v1/payments.

    Fail-closed, never silent:
    - absent session → `acp_session_not_found` (404); expired → `acp_session_expired`.
    - already completed → the stored completion result (idempotent replay; a
      matching idempotency_key with a DIFFERENT payload → `idempotency_conflict`, 409).
    - kill-switch block / over-cap → the gate chain's own HTTPException(403)
      propagates untouched (as_error_detail shape — dark stays dark).
    - capture lane not engaged → `acp_capture_lane_disabled` (403): an
      off-session charge has no client-confirm fallback, so it refuses.
    - capture failure → `acp_capture_failed` (502). There is NO simulated
      capture fallback — that pivota-acp path is deliberately not ported.
    """
    session = await peek_session(session_id)
    if not session:
        raise AcpCheckoutSessionError(
            "checkout session not found", code="acp_session_not_found", status_code=404
        )

    req_hash = _request_hash(payment_token)

    if str(session.get("status") or "") == STATUS_COMPLETED:
        completion = session.get("completion") if isinstance(session.get("completion"), dict) else {}
        stored_key = str(session.get("idempotency_key") or "") or None
        stored_hash = str((completion or {}).get(_COMPLETION_REQUEST_HASH_KEY) or "") or None
        if (
            idempotency_key
            and stored_key
            and idempotency_key == stored_key
            and stored_hash
            and stored_hash != req_hash
        ):
            raise AcpCheckoutSessionError(
                "Same Idempotency-Key used with different parameters",
                code="idempotency_conflict",
                status_code=409,
            )
        if completion:
            return _public_completion(completion)
        # Completed by an older writer without a stored result: reconstruct the
        # minimal contract from the row (never re-charge).
        order_id = str(session.get("order_id") or "") or None
        return {
            "status": STATUS_COMPLETED,
            "checkout_session_id": str(session.get("id")),
            "order_id": order_id,
            "payment_status": "succeeded",
            "order": (
                {
                    "id": order_id,
                    "checkout_session_id": str(session.get("id")),
                    "permalink_url": _order_permalink_url(order_id),
                }
                if order_id
                else None
            ),
        }

    if _is_expired(session):
        raise AcpCheckoutSessionError(
            "checkout session expired", code="acp_session_expired", status_code=404
        )

    # Fail-fast kill-switch pre-check BEFORE creating any order: a dark lane
    # must refuse without side effects. The full gate chain (incl. amount caps)
    # runs again on the charge itself below.
    from fastapi import HTTPException

    from services.agent_checkout_kill_switch import evaluate_tier2_charge

    pre_gate = evaluate_tier2_charge(protocol_name, merchant_id=str(session.get("merchant_id")))
    if not pre_gate.allowed:
        raise HTTPException(status_code=403, detail=pre_gate.as_error_detail())

    # Fresh quote at completion time (mirrors pivota-acp's real capture: the
    # shipping address must match the order for the quote fingerprint).
    items = _normalize_items(session.get("items") or [])
    if not items:
        raise AcpCheckoutSessionError(
            "session has no completable items", code="acp_items_required", status_code=422
        )
    for idx, item in enumerate(items):
        if not (item.get("product_id") and item.get("variant_id")):
            raise AcpCheckoutSessionError(
                f"item {idx} missing product_id/variant_id — a Pivota quote requires both",
                code="acp_item_identity_required",
                status_code=422,
            )

    session_metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    buyer = session.get("buyer") if isinstance(session.get("buyer"), dict) else {}
    customer_email = (
        (buyer or {}).get("email")
        or (session_metadata or {}).get("customer_email")
        or DEFAULT_BUYER_EMAIL
    )
    address = _normalize_address(session.get("fulfillment_address")) or dict(DEFAULT_SHIPPING_ADDRESS)

    quote = await _build_quote(
        merchant_id=str(session.get("merchant_id")),
        items=items,
        customer_email=str(customer_email),
        shipping_address=address,
        agent_id=str(getattr(agent_context, "agent_id", "") or "") or None,
    )

    order_id = await _create_pivota_order(
        session=session,
        quote=quote,
        agent_context=agent_context,
        protocol_name=str(protocol_name or "acp"),
        idempotency_key=idempotency_key,
    )

    # Charge through the shared gate chain + off-session capture (the exact code
    # POST /agent/v1/payments runs — services/acp_offsession_payment).
    from db.orders import get_order
    from services.acp_offsession_payment import (
        evaluate_acp_offsession_gates,
        execute_acp_offsession_payment,
        settle_acp_offsession_success,
    )

    order = await get_order(order_id)
    if not order:
        raise AcpCheckoutSessionError(
            f"order {order_id} not found after create",
            code="acp_order_create_failed",
            status_code=502,
        )
    order_metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}
    if isinstance(order.get("metadata"), str):
        order_metadata = _parse_json_field(order.get("metadata")) or {}
    order_total = order.get("total_amount") or order.get("total")
    if order_total is None:
        raise AcpCheckoutSessionError(
            "order total not found", code="acp_order_create_failed", status_code=502
        )
    amount_cents = int(round(float(order_total) * 100))
    currency = str(order.get("currency") or quote.get("currency") or "USD")

    gates = evaluate_acp_offsession_gates(
        protocol_name=order_metadata.get("protocol_name") or protocol_name,
        merchant_id=str(session.get("merchant_id")),
        amount_cents=amount_cents,
        order_id=order_id,
    )
    if not gates.engaged:
        # Kill-switch permits, but neither the test- nor live-capture lane is
        # armed. An off-session completion has no buyer present to client-confirm
        # a payment surface — fail closed instead of pretending.
        raise AcpCheckoutSessionError(
            "no off-session capture lane is enabled for this charge",
            code="acp_capture_lane_disabled",
            status_code=403,
        )

    outcome = await execute_acp_offsession_payment(
        gates=gates,
        merchant_id=str(session.get("merchant_id")),
        order_id=order_id,
        currency=currency,
        idempotency_key=(idempotency_key or f"acp_complete:{session.get('id')}"),
        payment_method_token=(payment_token or None),
        agent_id=str(getattr(agent_context, "agent_id", "") or ""),
        order_metadata=order_metadata,
    )
    if not outcome.success:
        raise AcpCheckoutSessionError(
            f"capture_failed:{outcome.error_code or 'unknown'}: {str(outcome.error or '')[:200]}",
            code="acp_capture_failed",
            status_code=502,
        )

    await settle_acp_offsession_success(
        gates=gates,
        outcome=outcome,
        order=order,
        order_id=order_id,
        merchant_id=str(session.get("merchant_id")),
        currency=currency,
        idempotency_key=idempotency_key,
        agent_id=str(getattr(agent_context, "agent_id", "") or ""),
        order_metadata=order_metadata,
    )

    completion: Dict[str, Any] = {
        "status": STATUS_COMPLETED,
        "checkout_session_id": str(session.get("id")),
        "order_id": order_id,
        "payment_status": "succeeded",
        "psp_used": outcome.psp_used,
        "payment_intent_id": outcome.payment_intent_id,
        "amount_cents": amount_cents,
        "currency": currency,
        "order": {
            "id": order_id,
            "checkout_session_id": str(session.get("id")),
            "permalink_url": _order_permalink_url(order_id),
        },
        _COMPLETION_REQUEST_HASH_KEY: req_hash,
    }

    now = _now_utc()
    try:
        await database.execute(
            acp_checkout_sessions.update()
            .where(acp_checkout_sessions.c.id == str(session.get("id")))
            .values(
                status=STATUS_COMPLETED,
                order_id=order_id,
                completion=completion,
                idempotency_key=(idempotency_key or None),
                completed_at=now,
                updated_at=now,
            )
        )
    except Exception as persist_exc:  # noqa: BLE001 — the charge succeeded; log, don't refuse
        logger.warning(
            "[AcpCheckoutSession] completion persist failed session=%s order=%s: %s",
            session.get("id"), order_id, str(persist_exc)[:200],
        )

    return _public_completion(completion)
