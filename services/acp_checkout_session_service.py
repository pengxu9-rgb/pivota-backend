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

  Charge sequencing (double-charge safety):
  1. CLAIM before charge — a conditional UPDATE flips the session
     ready_for_payment → completing; only the caller whose UPDATE lands
     proceeds. A concurrent second caller gets 409 `completion_in_progress`
     (or the stored completion if already completed).
  2. The PSP idempotency key is STORED on the row, never taken from the
     caller's Idempotency-Key, and it is ATTEMPT-SCOPED: the same claim UPDATE
     that wins the row increments `capture_attempt` and writes
     `acp_complete:{session_id}:a{attempt}` into `psp_idempotency_key`, BEFORE
     any order or capture work. Every capture — first try or resume — charges
     under the key READ BACK from the row. So:
       - a retry with a fresh caller key cannot mint a second charge, and
       - a RESUMED attempt replays the SAME PSP request (no double charge on a
         lost response), while
       - a definitively DECLINED attempt releases the claim, so the next claim
         mints the NEXT key and the buyer can retry with a different card
         instead of replaying a cached decline forever.
  3. order_id is persisted on the row IMMEDIATELY after order creation,
     BEFORE the charge; a later retry reuses that order, never mints another.
  4. A capture failure is CLASSIFIED, because releasing the claim is what lets
     the next attempt mint a new PSP key:
       - DEFINITIVE (the PSP refused the card, or the capture layer refused
         before dispatching anything): no charge can be in flight → revert to
         ready_for_payment (order_id kept for reuse), 502 `acp_capture_failed`.
       - AMBIGUOUS (exception, timeout, transport error, SCA/unresolved
         status — anything not provably a refusal): the charge may have landed
         → do NOT revert. The row stays `completing` with its stored key and
         the call returns 502 `acp_capture_pending_retry`; the stale-resume
         path replays the same key, which the PSP answers idempotently. When in
         doubt, ambiguous.
     Capture success persists the completion; if THAT write fails the call
     returns 502 `acp_completion_persist_failed` with
     reconciliation_required=true and leaves the row in `completing` +
     order_id, so a (stale) retry resumes with the same stored PSP key and
     re-attempts the persist. No path returns success without the completed
     row durably written.
  5. A stale `completing` row is taken over by a CONDITIONAL UPDATE (status +
     staleness in the WHERE clause), so exactly one resumer proceeds and any
     other concurrent resumer gets 409. Takeover covers the crash window
     BETWEEN claim and order-create too (order_id still NULL): the resumer
     re-enters the order-create path and captures under the stored key.

- `protocol_name` is a parameter (default "acp"): UCP/AP2 flows reuse this
  execution layer through the same GUARDED_PROTOCOLS gate chain.

Error vocabulary keeps the ACP public wire codes where the concept exists
(`acp_items_required`, `idempotency_conflict` on key reuse with a different
payload, 409 semantics) plus named in-process codes
(`acp_session_not_found`, `acp_session_expired`, `acp_quote_failed`,
`acp_address_required`, `acp_email_required`, ...).

NO FABRICATED BUYER DATA. Three parity hardcodes ported from the retired
service are gone: the "1 ACP Street" shipping default, the
acp-buyer@pivota.cc email default, and the hardcoded `payment_provider:
stripe` in the create response. A session that cannot name where the goods go
or who is buying them is refused at /complete (400, pre-claim, zero side
effects), and the create response reports the merchant's REAL PSP provider or
omits the field entirely.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from config.settings import settings
from db.acp_checkout_sessions import acp_checkout_sessions
from db.database import IS_POSTGRES, database, engine, metadata
from utils.money import to_minor_units
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
STATUS_COMPLETING = "completing"
STATUS_COMPLETED = "completed"

# A row stuck in `completing` (a prior attempt failed to persist its completion,
# crashed before creating the order, or hit an ambiguous capture failure) may be
# RESUMED — but only once it is stale, so a genuinely concurrent in-flight
# completion is answered 409 completion_in_progress instead of racing it.
# Resumption is convergent by construction: it replays the PSP idempotency key
# STORED on the row, so a resumed capture replays, never re-charges.
COMPLETING_RESUME_AFTER_SECONDS = 60

# Ceiling on the (non-fatal, provider-name-only) merchant_psps read that fills
# the create response's payment_provider — a slow row omits the field, never
# stalls session creation.
_PAYMENT_PROVIDER_LOOKUP_TIMEOUT_S = 1.5

# The fields models.order.ShippingAddress requires (beyond `name`, which
# _normalize_address always supplies). A session missing any of them cannot
# produce a real order, so completion REFUSES instead of inventing one: the
# retired pivota-acp service shipped a hardcoded placeholder street address
# here, which meant a chargeable order could ship somewhere nobody asked for.
# Fabricating buyer data on a money path is never the safe default.
_REQUIRED_ADDRESS_FIELDS = ("name", "address_line1", "city", "postal_code", "country")

# The pvt_* keys threaded into the session metadata for attribution parity.
# (Formerly kept in lockstep with routes/platform_orders_acp._ACP_ATTRIBUTION_KEYS;
# that router went away with the retired pivota-acp service — ADR-021. This is
# now the single definition for the ACP lane.)
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
    route layer should translate to when it chooses to surface the error.
    `extra` carries additional machine-readable detail fields (e.g.
    reconciliation_required) the route merges into its error payload."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.extra = dict(extra or {})


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
    191_acp_checkout_sessions.sql. The raw DDL runs FIRST so on Postgres the
    canonical TEXT/JSONB shape always wins; the SQLAlchemy metadata create is
    the SQLite dev/test path (pattern:
    routes/agent_checkout_intents._ensure_checkout_intents_table)."""
    if IS_POSTGRES:
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS acp_checkout_sessions (
                id TEXT PRIMARY KEY,
                merchant_id TEXT NOT NULL,
                agent_id TEXT,
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
                capture_attempt INTEGER NOT NULL DEFAULT 0,
                psp_idempotency_key TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                expires_at TIMESTAMPTZ
            )
            """
        )
        await database.execute("ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS agent_id TEXT")
        await database.execute("ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS completion JSONB")
        await database.execute("ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS idempotency_key TEXT")
        await database.execute("ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS order_id TEXT")
        await database.execute("ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ")
        await database.execute("ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
        await database.execute(
            "ALTER TABLE acp_checkout_sessions "
            "ADD COLUMN IF NOT EXISTS capture_attempt INTEGER NOT NULL DEFAULT 0"
        )
        await database.execute(
            "ALTER TABLE acp_checkout_sessions ADD COLUMN IF NOT EXISTS psp_idempotency_key TEXT"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_acp_checkout_sessions_merchant_created "
            "ON acp_checkout_sessions (merchant_id, created_at)"
        )
        # Plain lookup index, deliberately NOT unique — cross-session key reuse
        # is answered 409 BEFORE any charge; a unique index would abort the
        # completion write only AFTER the charge (retry-recharge wedge).
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_acp_checkout_sessions_merchant_idem "
            "ON acp_checkout_sessions (merchant_id, idempotency_key)"
        )

    try:
        metadata.create_all(engine, tables=[acp_checkout_sessions])
    except Exception:
        pass

    if not IS_POSTGRES:
        # SQLite dev/test: create_all() is checkfirst-only, so it will NOT add a
        # new column to a table an older build already created (the local
        # pivota_test.db outlives a schema change). Mirror the Postgres
        # ADD COLUMN IF NOT EXISTS healing — SQLite has no IF NOT EXISTS for
        # columns, so a duplicate-column error is the expected no-op.
        for ddl in (
            "ALTER TABLE acp_checkout_sessions ADD COLUMN capture_attempt INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE acp_checkout_sessions ADD COLUMN psp_idempotency_key TEXT",
        ):
            try:
                await database.execute(ddl)
            except Exception:
                pass


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
        # No placeholder recipient default — the last invented-fulfillment
        # field on this path. The name must be real buyer data: from the
        # address itself, or backfilled from the session's buyer in
        # _require_fulfillment_address.
        "name": a.get("name"),
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


def _require_fulfillment_address(session: Dict[str, Any]) -> Dict[str, Any]:
    """The session's fulfillment address, or a fail-closed refusal.

    There is NO session-update endpoint in this layer (the ACP protocol allows
    an address-less create followed by an update, but nothing in this codebase
    implements that update yet), so the address can only arrive at creation via
    `fulfillment_address`. An absent or incomplete one is a 400 the caller can
    fix by re-creating the session — never a default we make up.

    The recipient `name` is required too, but a missing address-level name may
    be backfilled from the session's OWN buyer (first+last) — that is the
    buyer's real name, not an invention. No buyer name either → refuse."""
    address = _normalize_address(session.get("fulfillment_address"))
    if address is not None and not str(address.get("name") or "").strip():
        buyer = session.get("buyer") if isinstance(session.get("buyer"), dict) else {}
        composed = " ".join(
            part
            for part in (
                str((buyer or {}).get("first_name") or "").strip(),
                str((buyer or {}).get("last_name") or "").strip(),
            )
            if part
        )
        if composed:
            address["name"] = composed
    missing = [
        key
        for key in _REQUIRED_ADDRESS_FIELDS
        if not str((address or {}).get(key) or "").strip()
    ]
    if address is None or missing:
        raise AcpCheckoutSessionError(
            "fulfillment address required: this checkout session has no usable "
            "fulfillment address ("
            + (
                "none was supplied"
                if address is None
                else "missing " + ", ".join(missing)
            )
            + "). There is no session-update endpoint — supply the full address "
            "(name, address_line1, city, postal_code, country) as "
            "`fulfillment_address` when the checkout session is created (a "
            "missing recipient name may also come from buyer.first_name/"
            "last_name).",
            code="acp_address_required",
            status_code=400,
        )
    return address


def _require_customer_email(session: Dict[str, Any]) -> str:
    """The session's buyer email, or a fail-closed refusal. The retired service
    charged under a hardcoded placeholder mailbox when the session carried none,
    which produced real orders no real buyer could be notified about."""
    buyer = session.get("buyer") if isinstance(session.get("buyer"), dict) else {}
    session_metadata = (
        session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    )
    email = str(
        (buyer or {}).get("email") or (session_metadata or {}).get("customer_email") or ""
    ).strip()
    if not email:
        raise AcpCheckoutSessionError(
            "buyer email required: this checkout session carries neither a buyer "
            "email nor metadata.customer_email. There is no session-update "
            "endpoint — supply `buyer.email` (or metadata.customer_email) when "
            "the checkout session is created.",
            code="acp_email_required",
            status_code=400,
        )
    return email


async def _payment_provider_descriptor(merchant_id: str) -> Optional[Dict[str, Any]]:
    """The merchant's ACTUAL payment provider, from their active PSP row.

    Returns None — and the create response then OMITS `payment_provider`
    entirely — when the merchant has no active PSP row or the lookup fails.
    Honest absence beats the old hardcoded provider=stripe descriptor, which told
    every redirect-floor merchant (who have no PSP at all) that Pivota would
    charge their buyer's card through Stripe. Deliberately NON-FATAL: session
    creation does not charge anything, so a missing PSP must not break it.

    Review nits 2+3: provider-ONLY query (no key material rides a path that
    charges nothing) with a hard timeout so a slow merchant_psps can only omit
    the field, never stall session creation."""
    try:
        from services.merchant_psp_config_service import fetch_active_merchant_psp_provider

        provider = await asyncio.wait_for(
            fetch_active_merchant_psp_provider(merchant_id=str(merchant_id or "")),
            timeout=_PAYMENT_PROVIDER_LOOKUP_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — never fatal to session creation
        logger.warning(
            "[AcpCheckoutSession] payment-provider lookup failed merchant=%s: %s "
            "(payment_provider omitted from the session response)",
            merchant_id, str(exc)[:200],
        )
        return None
    if not provider:
        return None
    return {"provider": provider, "supported_payment_methods": ["card"]}


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


def _totals_from_pricing(pricing: Dict[str, Any], currency: Any) -> List[Dict[str, Any]]:
    """Old wire shape: a list of `{"type", "display_text", "amount"}` entries in
    minor units, including one entry with type == "total" — consumers
    (agent_checkout_intents' callers, the old client's parser) rely on exactly
    that. Uses the SAME currency-aware minor-unit conversion as the charge path
    (utils.money.to_minor_units) so display and charge can never round apart."""
    subtotal = to_minor_units((pricing or {}).get("subtotal"), currency)
    discount = to_minor_units((pricing or {}).get("discount_total"), currency)
    shipping = to_minor_units((pricing or {}).get("shipping_fee"), currency)
    tax = to_minor_units((pricing or {}).get("tax"), currency)
    total = to_minor_units((pricing or {}).get("total"), currency)
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
    agent_id: Optional[str],
    fulfillment_address: Optional[Dict[str, Any]] = None,
    buyer: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    protocol_name: str = "acp",
) -> AcpSessionResult:
    """Create an in-process ACP checkout session: quote the cart with the REAL
    quote engine, persist the session, and return the old wire-shape descriptor.
    Does NOT complete or charge. Raises AcpCheckoutSessionError on failure.

    `agent_id` is REQUIRED (no default): the completing agent must match the
    minting agent, so a session created without one can never be completed
    (complete_session refuses it with `acp_agent_unbound`). Making it a
    positional-by-name requirement stops a caller from silently minting an
    uncompletable session by omitting it.

    `fulfillment_address` and `buyer` stay OPTIONAL here (the ACP protocol
    allows an address-less session that a later update fills in, and a session
    is only a screen/quote — it charges nothing). But this layer has no update
    path, so in practice a session created without a full address and a buyer
    email cannot be completed: /complete refuses it with `acp_address_required`
    / `acp_email_required` rather than inventing either."""
    if not str(agent_id or "").strip():
        # An explicit None/blank would mint a session complete_session can never
        # accept (acp_agent_unbound) — refuse at the door instead.
        raise AcpCheckoutSessionError(
            "agent_id_required", code="acp_agent_required", status_code=400
        )
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
    totals = _totals_from_pricing(quote.get("pricing") or {}, currency)
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
        "agent_id": (str(agent_id or "").strip() or None),
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
    # Derived from the merchant's real PSP row, and simply ABSENT when there is
    # none (or the lookup fails) — never a guessed provider.
    payment_provider = await _payment_provider_descriptor(merchant_id)
    if payment_provider:
        raw["payment_provider"] = payment_provider
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


def _request_hash(session_id: str, payment_token: Optional[str]) -> str:
    # Covers the session id AND the payment token: reusing an Idempotency-Key
    # for a different session (or a different card) is a different request.
    body = json.dumps(
        {"session_id": str(session_id or ""), "payment_token": payment_token or None},
        sort_keys=True,
    )
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

    # Fail-closed, no fabrication: complete_session already refused a session
    # without a usable address/email BEFORE claiming, so these are belt-and-
    # suspenders for any other caller — they never invent a default.
    address = _require_fulfillment_address(session)
    customer_email = _require_customer_email(session)

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
        # agent_user/x_buyer_ref MUST be passed explicitly on an in-process
        # call: their FastAPI Depends/Header defaults are truthy sentinel
        # objects that crash the route body (repo precedent:
        # routes/agent_commerce.py, routes/agent_v2.py).
        created = await agent_create_order(
            order_request=order_request,
            background_tasks=BackgroundTasks(),
            context=agent_context,
            agent_user=None,
            x_buyer_ref=None,
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


PSP_IDEMPOTENCY_KEY_PREFIX = "acp_complete:"


def _psp_idempotency_key(session_id: str, capture_attempt: int) -> str:
    """The PSP idempotency key for ONE completion attempt — always derived from
    the session id + attempt number, NEVER from the caller's Idempotency-Key.

    Session-scoped is the double-charge kill (a retry with a fresh caller key
    replays the same PSP request); ATTEMPT-scoped is what keeps a definitively
    declined session retryable (a new card charges under a new key instead of
    replaying the PSP's cached decline). The key is minted by the claim UPDATE
    and STORED; captures always use the stored value, never a recomputed one."""
    return f"{PSP_IDEMPOTENCY_KEY_PREFIX}{session_id}:a{int(capture_attempt)}"


def _psp_idempotency_key_prefix(session_id: str) -> str:
    """The stored key minus its attempt suffix — the claim UPDATE appends the
    attempt number it just incremented to, so key and attempt are written
    atomically with the claim."""
    return f"{PSP_IDEMPOTENCY_KEY_PREFIX}{session_id}:a"


def _legacy_psp_idempotency_key(session_id: str) -> str:
    """The un-suffixed key written by builds BEFORE attempt scoping existed. A
    row those builds wedged in `completing` charged under THIS key, so a resume
    of such a row must replay it (never a freshly minted one)."""
    return f"{PSP_IDEMPOTENCY_KEY_PREFIX}{session_id}"


def _replay_completed(
    session: Dict[str, Any],
    *,
    idempotency_key: Optional[str],
    req_hash: str,
) -> Dict[str, Any]:
    """Idempotent replay of an already-completed session (never re-charges).
    A matching stored Idempotency-Key with a DIFFERENT request hash is the ACP
    wire 409 `idempotency_conflict`."""
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


@dataclass(frozen=True)
class _ClaimOutcome:
    """What a winning claim/takeover UPDATE returned. `psp_idempotency_key` is
    the key the capture MUST charge under — read back from the row, never
    recomputed by the caller."""

    capture_attempt: int
    psp_idempotency_key: str
    order_id: Optional[str] = None


async def _with_table_self_heal(label: str, run) -> Any:
    """Run a DB coroutine factory, healing a missing/drifted table once and
    retrying. A DDL race must surface as an AcpCheckoutSessionError (the shape
    the route maps), never a raw 500 — the same posture peek_session/create use.
    """
    try:
        return await run()
    except Exception:
        try:
            await _ensure_acp_checkout_sessions_table()
            return await run()
        except Exception as exc:  # noqa: BLE001 — a claim we cannot run is a named 503
            raise AcpCheckoutSessionError(
                f"{label}: {str(exc)[:200]}",
                code="acp_session_persist_failed",
                status_code=503,
            ) from exc


# The claim mints the attempt's PSP key IN THE SAME STATEMENT that wins the row,
# so there is no window in which a claim holder has no stored key. CAST(...) is
# spelled explicitly: it is the one concat form both Postgres (which would
# otherwise see an unknown-typed parameter) and SQLite accept.
_CLAIM_SQL = """
UPDATE acp_checkout_sessions
SET status = :completing,
    updated_at = :now,
    idempotency_key = COALESCE(:key, idempotency_key),
    capture_attempt = COALESCE(capture_attempt, 0) + 1,
    psp_idempotency_key = CAST(:key_prefix AS TEXT)
        || CAST(COALESCE(capture_attempt, 0) + 1 AS TEXT)
WHERE id = :id AND status = :ready
RETURNING capture_attempt, psp_idempotency_key, order_id
"""

# N4: taking over a STALE `completing` row is itself a conditional UPDATE — the
# status AND the staleness live in the WHERE clause, so exactly one of N
# concurrent resumers can proceed (it moves updated_at out of the stale window)
# and the rest fall through to 409. It deliberately does NOT touch
# capture_attempt or psp_idempotency_key: a resume must replay the STORED key.
_STALE_TAKEOVER_SQL = """
UPDATE acp_checkout_sessions
SET updated_at = :now
WHERE id = :id AND status = :completing AND updated_at <= :stale_cutoff
RETURNING capture_attempt, psp_idempotency_key, order_id
"""


async def _claim_session(
    session_id: str, idempotency_key: Optional[str]
) -> Optional[_ClaimOutcome]:
    """Atomically flip ready_for_payment → completing; exactly one caller wins
    (conditional UPDATE + RETURNING — works on both Postgres and SQLite). The
    winning UPDATE also increments capture_attempt and stores that attempt's PSP
    idempotency key, so the key exists on the row BEFORE any order/capture work.
    The caller's Idempotency-Key is recorded for validation only.

    Returns the claim outcome, or None when another attempt holds the row."""

    async def _run():
        return await database.fetch_one(
            _CLAIM_SQL,
            {
                "completing": STATUS_COMPLETING,
                "now": _now_utc(),
                "key": (idempotency_key or None),
                "key_prefix": _psp_idempotency_key_prefix(session_id),
                "id": session_id,
                "ready": STATUS_READY_FOR_PAYMENT,
            },
        )

    row = await _with_table_self_heal("session_claim_failed", _run)
    if row is None:
        return None
    data = dict(row)
    attempt = int(data.get("capture_attempt") or 1)
    return _ClaimOutcome(
        capture_attempt=attempt,
        # Fall back to the computed key only if the dialect returned nothing for
        # the column it just wrote (never observed; belt-and-suspenders).
        psp_idempotency_key=(
            str(data.get("psp_idempotency_key") or "").strip()
            or _psp_idempotency_key(session_id, attempt)
        ),
        order_id=(str(data.get("order_id") or "").strip() or None),
    )


async def _take_over_stale_completing(
    session_id: str, stale_cutoff: datetime
) -> Optional[_ClaimOutcome]:
    """Take over a STALE `completing` row (single-flight, N4). Returns the row's
    STORED attempt/key — the resume replays that key, so a charge whose response
    was lost cannot become a second charge. Returns None when the row is not
    completing, not stale, or another resumer just took it."""

    async def _run():
        return await database.fetch_one(
            _STALE_TAKEOVER_SQL,
            {
                "now": _now_utc(),
                "id": session_id,
                "completing": STATUS_COMPLETING,
                "stale_cutoff": stale_cutoff,
            },
        )

    row = await _with_table_self_heal("session_resume_failed", _run)
    if row is None:
        return None
    data = dict(row)
    attempt = int(data.get("capture_attempt") or 0)
    stored_key = str(data.get("psp_idempotency_key") or "").strip()
    return _ClaimOutcome(
        capture_attempt=attempt,
        # NEVER recompute a resume key from the attempt counter: a row wedged by
        # a pre-attempt-scoping build has no stored key and charged under the
        # legacy un-suffixed one, which is what must be replayed.
        psp_idempotency_key=(stored_key or _legacy_psp_idempotency_key(session_id)),
        order_id=(str(data.get("order_id") or "").strip() or None),
    )


# --- capture-failure classification (which failures may release the claim) ----
#
# Releasing the claim is not a cosmetic state change: the NEXT claim mints a NEW
# PSP idempotency key, so a release is only sound when we KNOW no charge can be
# in flight under the current one. Exactly two provably-safe classes, both drawn
# from codes services/acp_offsession_capture actually produces:
#
#  1. PRE-DISPATCH refusals — the capture layer refused before any PSP request
#     left this process (orchestrator guards + each adapter's pre-request
#     validation). No PSP has ever seen the charge.
#  2. DEFINITIVE PSP declines — the PSP answered and refused. No money moved,
#     and the PSP has bound that answer to the key we used, so replaying it
#     would return the same decline forever; a new card needs a new key.
#
# EVERYTHING ELSE is ambiguous and must NOT release the claim: transport errors
# (`adyen_network_error`), HTTP failures (`adyen_http_*`), unresolved provider
# errors (`stripe_error`), SCA (`requires_action`), and any non-succeeded intent
# that could still settle (`not_succeeded`, `adyen_bad_response`). Those replay
# the stored key via the stale-resume path. When in doubt → ambiguous.
_PRE_DISPATCH_ERROR_CODES = frozenset(
    {
        # capture_offsession orchestrator, before adapter dispatch
        "invalid_amount",
        "over_cap",
        "no_merchant_psp",
        "live_key_refused",
        "live_key_required",
        "unsupported_provider",
        # adapter pre-request validation (nothing was sent to the PSP)
        "live_pm_required",
        "adyen_config",
        "adyen_pm_required",
        "adyen_shopper_ref_required",
    }
)
_DEFINITIVE_DECLINE_ERROR_CODES = frozenset(
    {
        # Stripe card_error class — the PSP refused this card outright.
        "card_declined",
        "expired_card",
        "incorrect_cvc",
        "invalid_cvc",
        "incorrect_number",
        "invalid_number",
        "incorrect_zip",
        "invalid_expiry_month",
        "invalid_expiry_year",
        "insufficient_funds",
        "card_not_supported",
        "currency_not_supported",
        "pickup_card",
        "lost_card",
        "stolen_card",
        "fraudulent",
        # NOTE (review B2): `adyen_refused` is deliberately NOT in this set. The
        # Adyen adapter emits it for EVERY resultCode that is neither Authorised
        # nor an SCA action — a bucket that includes Received/Pending (payment in
        # flight) and PartiallyAuthorised (money moved). Until the adapter is
        # narrowed to resultCode == "Refused" and proven by an Adyen canary,
        # Adyen failures are held as AMBIGUOUS (claim kept, same-key replay).
    }
)


def _capture_failure_is_definitive(outcome: Any) -> bool:
    """True only when the failed capture provably left no charge in flight (see
    the code-set comments above). Conservative: an unknown or empty error code,
    or any status other than `failed`, is ambiguous."""
    if str(getattr(outcome, "status", "") or "").strip().lower() != "failed":
        return False
    code = str(getattr(outcome, "error_code", "") or "").strip().lower()
    if not code:
        return False
    return code in _PRE_DISPATCH_ERROR_CODES or code in _DEFINITIVE_DECLINE_ERROR_CODES


async def _revert_claim_to_ready(session_id: str) -> None:
    """Release the completion claim. Callable ONLY when no charge can be in
    flight (see the classification above): the next claim increments the attempt
    and mints a NEW PSP idempotency key, so releasing a possibly-charged session
    is exactly how a double charge would happen. order_id is kept so a retry
    reuses the already-created order instead of minting another."""
    try:
        await database.execute(
            acp_checkout_sessions.update()
            .where(
                (acp_checkout_sessions.c.id == session_id)
                & (acp_checkout_sessions.c.status == STATUS_COMPLETING)
            )
            .values(status=STATUS_READY_FOR_PAYMENT, updated_at=_now_utc())
        )
    except Exception as exc:  # noqa: BLE001 — a stuck claim is resumable after the stale window
        logger.error(
            "[AcpCheckoutSession] failed to release completion claim session=%s: %s "
            "(session resumes via the stale-completing path)",
            session_id, str(exc)[:200],
        )


# --- delegated-token (`vt_`) allowance enforcement ---------------------------
#
# ACP delegated payment hands the business a single-use, amount-capped,
# merchant-scoped token instead of a PSP token. We keep our OWN recorded view of
# that allowance (services/acp_delegate_allowance_service — no card material of
# any kind is stored) and enforce it here, fail-closed, IN ADDITION to whatever
# the PSP enforces authoritatively.
#
# THE CHECK ORDER BELOW IS THE WIRE CONTRACT — DO NOT REORDER.
#   1. invalid_token               — no allowance recorded for this token
#   2. allowance_session_mismatch  — recorded for a different checkout session
#   3. allowance_merchant_mismatch — recorded for a different merchant   (NEW)
#   4. allowance_currency_mismatch — currency differs from the session's
#   5. allowance_exceeded          — session total > max_amount
#   6. allowance_expired           — the allowance's expires_at has passed (NEW)
# The retired pivota-acp service's four 422s (1, 2, 4, 5) keep their RELATIVE
# order and their message strings verbatim, because external platforms may have
# coded against them: existence → session → currency → amount. The two
# tightenings are inserted where they belong semantically — the merchant scope
# right after the session scope (both are "who is this allowance for"), the
# expiry appended last (it is the newest gap closed). All six are 422s in the
# `{type, code, message, param}` envelope the route builds from `extra`.
#
# The gate is EXACTLY `startswith("vt_")`, wire parity: `pm_` (a real PSP
# payment method) and a future `spt_` (Stripe SharedPaymentToken) never reach
# the registry, in either flag state.
#
# WHERE THE CHECKS RUN, AND WHY IT DEPENDS ON THE CLAIM (review: resume path).
# Consumption is CLAIM-SCOPED, and there are exactly three cases:
#
#   FRESH claim
#       Checks 1–6 run PRE-CLAIM (zero side effects on refusal), then the CAS
#       bind runs at claim time before any order/charge work.
#
#   RESUMED claim, order_id IS NULL
#       This is the claim→order-create crash window. A capture is only
#       reachable AFTER `_persist_order_id` succeeds, so a NULL order_id
#       PROVABLY means nothing was ever dispatched to a PSP — this resume does
#       genuinely FRESH order-create and charge work, and it gets the FULL 1–6
#       enforcement plus the bind. Refusing here strands nothing (there is no
#       charge to strand). Per review B1 the refusal HOLDS the resumed claim, so
#       a later resume presenting a VALID token still converges.
#
#   RESUMED claim, order_id SET
#       A capture may already be in flight under the STORED PSP idempotency key,
#       and — decisively — the caller's token is INERT on this path: the capture
#       replays that stored key regardless of which token rides in. So a
#       caller-supplied token is NEVER consumed here. Only the token this
#       session ALREADY bound may be re-bound (idempotently). A foreign,
#       expired or unknown `vt_` on such a replay is IGNORED for binding
#       purposes, not refused: refusing would strand a real charge in order to
#       punish a token that cannot affect it, and consuming it would bind an
#       allowance to a session it was never minted for. Ignoring is the only
#       option that neither burns a foreign allowance nor wedges live money.
_ALLOWANCE_TOKEN_PARAM = "payment_data.token"


def _allowance_error(message: str, *, code: str, param: str) -> "AcpCheckoutSessionError":
    return AcpCheckoutSessionError(
        message, code=code, status_code=422, extra={"param": param}
    )


async def _lookup_allowance(payment_token: Optional[str]) -> Optional[Dict[str, Any]]:
    """The recorded allowance for a token, with the registry's own error mapped
    into this module's vocabulary. A registry we cannot read is a named 503,
    never a silent pass: an unverifiable allowance must not become a charge."""
    from services import acp_delegate_allowance_service as allowances

    try:
        return await allowances.get_allowance(str(payment_token))
    except allowances.AcpDelegateAllowanceError as exc:
        raise AcpCheckoutSessionError(
            exc.message, code=exc.code, status_code=exc.status_code or 503
        ) from exc


async def _lookup_allowance_bound_to_session(
    session_id: str,
) -> Optional[Dict[str, Any]]:
    """The allowance this session already consumed, same error mapping."""
    from services import acp_delegate_allowance_service as allowances

    try:
        return await allowances.get_allowance_bound_to_session(str(session_id))
    except allowances.AcpDelegateAllowanceError as exc:
        raise AcpCheckoutSessionError(
            exc.message, code=exc.code, status_code=exc.status_code or 503
        ) from exc


async def _enforce_delegate_allowance(
    session: Dict[str, Any], payment_token: Optional[str]
) -> None:
    """Allowance enforcement for a `vt_` delegate token (checks 1–6 above).
    Raises a 422 AcpCheckoutSessionError on any refusal.

    Runs on a FRESH completion PRE-CLAIM (zero side effects), and on a RESUMED
    claim whose order_id is still NULL — the one resume that provably has no
    charge behind it and therefore must be validated exactly like a fresh one
    (see the case analysis above).

    Amount and currency are compared against the SERVER-SIDE session values
    only; a caller-supplied amount is never an input to a cap.
    """
    allowance = await _lookup_allowance(payment_token)

    # 1. existence — an unknown token REFUSES. We do not charge a delegated
    #    token whose allowance we cannot verify (design Q2, founder-confirmed).
    if not allowance:
        raise _allowance_error(
            "delegate token not found",
            code="invalid_token",
            param=_ALLOWANCE_TOKEN_PARAM,
        )

    sid = str(session.get("id") or "")
    # 2. session scope
    if str(allowance.get("checkout_session_id") or "") != sid:
        raise _allowance_error(
            "delegate token not authorized for this session",
            code="allowance_session_mismatch",
            param="allowance.checkout_session_id",
        )

    # 3. merchant scope (NEW — the retired service never checked this, so a
    #    token minted for merchant A was spendable at merchant B).
    if str(allowance.get("merchant_id") or "") != str(session.get("merchant_id") or ""):
        raise _allowance_error(
            "delegate token not authorized for this merchant",
            code="allowance_merchant_mismatch",
            param="allowance.merchant_id",
        )

    # 4. currency — case-insensitive on BOTH sides. A session with no recorded
    #    currency cannot be verified against the allowance, so it refuses too
    #    (fail-closed: an unverifiable comparison is not a passing one).
    session_currency = str(session.get("currency") or "").strip().lower()
    allowance_currency = str(allowance.get("currency") or "").strip().lower()
    if not session_currency or session_currency != allowance_currency:
        raise _allowance_error(
            "currency mismatch",
            code="allowance_currency_mismatch",
            param="allowance.currency",
        )

    # 5. amount — SERVER-SIDE session total vs the cap, in minor units.
    #    Equality PASSES (wire parity: the refusal is `total > max_amount`). A
    #    session with no recorded total is unverifiable → refuse.
    max_amount = allowance.get("max_amount")
    total_cents = session.get("total_cents")
    try:
        total_cents = int(total_cents) if total_cents is not None else None
    except (TypeError, ValueError):
        total_cents = None
    if max_amount is None or total_cents is None or total_cents > int(max_amount):
        raise _allowance_error(
            "total exceeds allowance",
            code="allowance_exceeded",
            param="allowance.max_amount",
        )

    # 6. expiry (NEW — the retired service stored expires_at and never read it).
    expires_at = allowance.get("expires_at")
    if expires_at is None or expires_at <= _now_utc():
        raise _allowance_error(
            "delegate token allowance expired",
            code="allowance_expired",
            param="allowance.expires_at",
        )


async def _bind_delegate_allowance(session_id: str, payment_token: Optional[str]) -> None:
    """Consume the allowance for THIS session (the single-use CAS), at claim
    time and before any order/charge work.

    Idempotent for the same session by construction, so a retry or a stale
    resume of this session's own attempt can never be refused by its own earlier
    bind. A token already bound to a DIFFERENT session refuses."""
    from services import acp_delegate_allowance_service as allowances

    try:
        bound = await allowances.bind_allowance_to_session(
            token_id=str(payment_token), session_id=session_id
        )
    except allowances.AcpDelegateAllowanceError as exc:
        raise AcpCheckoutSessionError(
            exc.message, code=exc.code, status_code=exc.status_code or 503
        ) from exc
    if not bound:
        raise _allowance_error(
            "delegate token has already been used",
            code="allowance_already_used",
            param=_ALLOWANCE_TOKEN_PARAM,
        )


async def _rebind_own_delegate_allowance(
    session_id: str, payment_token: Optional[str]
) -> Optional[str]:
    """The RESUME-with-an-order case: re-bind ONLY the token this session has
    already bound, consume NOTHING else, and return the token the capture must
    REPLAY WITH.

    A token that is not this session's own bound token is neither consumed
    (that would bind a foreign allowance to a session it was never minted for)
    nor refused (that would strand a charge which may already be in flight) —
    it is ignored, and the session's OWN bound token is returned in its place.

    Returning the own token matters (review): the capture replays the STORED PSP
    idempotency key, and a PSP keys idempotency on the whole parameter set — so
    replaying that key with a DIFFERENT token turns a clean retry into an
    idempotency error, which classifies as AMBIGUOUS and re-holds the claim.
    Repeated often enough that wedges the row until its TTL. The caller's token
    is inert for BINDING; it is not inert for the PSP call. So the replay is
    made parameter-identical to the dispatch that actually happened."""
    own = await _lookup_allowance_bound_to_session(session_id)
    own_token = str((own or {}).get("token_id") or "") or None
    presented = str(payment_token or "").strip() or None
    if own_token and presented == own_token:
        await _bind_delegate_allowance(session_id, payment_token)
        return own_token
    logger.info(
        "[AcpCheckoutSession] ignoring a delegate token on a resumed completion "
        "that already has an order session=%s (no allowance consumed; the "
        "capture replays this session's own token so the retry stays "
        "parameter-identical) own_token_present=%s",
        session_id,
        bool(own_token),
    )
    return own_token


def _delegate_allowance_applies(payment_token: Optional[str]) -> bool:
    """True only when the registry lane is armed AND the token is a `vt_`
    delegate token. Flag OFF ⇒ a `vt_` token behaves exactly as it does today
    (no registry lookup at all; it dies at capture, where the Stripe adapter
    refuses a non-`pm_` on the live lane and substitutes the test PM on the test
    lane). `pm_`/`spt_` never take this branch in either flag state."""
    if not getattr(settings, "acp_delegate_allowance_registry_enabled", False):
        return False
    from services.acp_delegate_allowance_service import is_delegate_token

    return is_delegate_token(payment_token)


async def _persist_order_id(session_id: str, order_id: str) -> None:
    """Record the created order on the row BEFORE the charge. NOT best-effort:
    charging an order we could not durably record would let a retry mint a
    second order."""
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == session_id)
        .values(order_id=order_id, updated_at=_now_utc())
    )


async def _persist_completion(
    *,
    session_id: str,
    order_id: str,
    completion: Dict[str, Any],
    idempotency_key: Optional[str],
) -> None:
    """Durably record the completion. Callers treat a failure as 502 (never a
    silent success) — separated into its own function so tests can fault it."""
    now = _now_utc()
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == session_id)
        .values(
            status=STATUS_COMPLETED,
            order_id=order_id,
            completion=completion,
            idempotency_key=(idempotency_key or None),
            completed_at=now,
            updated_at=now,
        )
    )


async def complete_session(
    *,
    session_id: str,
    agent_context: Any,
    payment_token: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    protocol_name: str = "acp",
) -> Dict[str, Any]:
    """Complete a checkout session: claim → quote → order(protocol) →
    off-session charge through the SAME Tier-2 gate chain as
    POST /agent/v1/payments (see the module docstring for the full
    double-charge-safety sequencing).

    Fail-closed, never silent:
    - absent session → `acp_session_not_found` (404); expired → `acp_session_expired`.
    - a different agent than the session's creator → `acp_agent_mismatch` (403).
    - a session with NO bound agent identity → `acp_agent_unbound` (403): the
      money path's identity check cannot be satisfied by merchant scope alone,
      so an unbound session is refused rather than completed by anyone.
    - already completed → the stored completion result (idempotent replay; a
      matching idempotency_key with a DIFFERENT payload → `idempotency_conflict`, 409).
    - an Idempotency-Key already used by ANOTHER session → `idempotency_conflict`
      (409), decided BEFORE any charge.
    - no usable fulfillment address → `acp_address_required` (400) and no buyer
      email anywhere → `acp_email_required` (400), both decided BEFORE the claim
      (zero side effects). Neither is defaulted: an order shipped to an invented
      address, or attributed to an invented buyer, is worse than a refusal.
    - a `vt_` delegated token (and only then, and only while
      ACP_DELEGATE_ALLOWANCE_REGISTRY_ENABLED is on) is checked against the
      recorded allowance BEFORE the claim, in the contract order
      `invalid_token` → `allowance_session_mismatch` →
      `allowance_merchant_mismatch` → `allowance_currency_mismatch` →
      `allowance_exceeded` → `allowance_expired` (all 422, zero side effects),
      then consumed single-use at claim time → `allowance_already_used` (422).
    - a concurrent in-flight completion → `completion_in_progress` (409).
    - kill-switch block / over-cap → the gate chain's own HTTPException(403)
      propagates untouched (as_error_detail shape — dark stays dark).
    - capture lane not engaged → `acp_capture_lane_disabled` (403).
    - DEFINITIVE capture failure (PSP refusal / pre-dispatch refusal) →
      `acp_capture_failed` (502); the claim is released (so a retry mints a new
      PSP key and can use a different card) and the created order is kept for
      reuse.
    - AMBIGUOUS capture failure (exception, timeout, transport, unresolved
      status) → `acp_capture_pending_retry` (502); the claim is NOT released and
      the row keeps its stored PSP key, so a retry after the stale window
      REPLAYS the same PSP request instead of charging again.
    - completion-persist failure AFTER a successful capture →
      `acp_completion_persist_failed` (502, reconciliation_required=true); the
      row stays `completing`+order_id so a stale retry converges on the same
      stored PSP key. There is NO simulated capture fallback — that pivota-acp
      path is deliberately not ported.
    """
    session = await peek_session(session_id)
    if not session:
        raise AcpCheckoutSessionError(
            "checkout session not found", code="acp_session_not_found", status_code=404
        )

    # The completing agent must be the agent that minted the session — merchant
    # scoping alone is not enough on a money path. A session with NO recorded
    # agent cannot satisfy that check at all, so it is refused outright instead
    # of silently degrading to merchant-only authorization (create_session
    # requires agent_id, so this only guards rows from older/foreign writers).
    session_agent = str(session.get("agent_id") or "") or None
    caller_agent = str(getattr(agent_context, "agent_id", "") or "") or None
    if not session_agent:
        raise AcpCheckoutSessionError(
            "checkout session has no bound agent identity and cannot be completed",
            code="acp_agent_unbound",
            status_code=403,
        )
    if caller_agent != session_agent:
        raise AcpCheckoutSessionError(
            "checkout session belongs to a different agent",
            code="acp_agent_mismatch",
            status_code=403,
        )

    sid = str(session.get("id"))
    req_hash = _request_hash(sid, payment_token)

    if str(session.get("status") or "") == STATUS_COMPLETED:
        return _replay_completed(session, idempotency_key=idempotency_key, req_hash=req_hash)

    if _is_expired(session):
        raise AcpCheckoutSessionError(
            "checkout session expired", code="acp_session_expired", status_code=404
        )

    # Cross-session Idempotency-Key reuse is refused BEFORE any claim/charge
    # (this replaces the earlier unique index, which would only have aborted the
    # completion write AFTER the charge).
    if idempotency_key:
        try:
            other = await database.fetch_one(
                "SELECT id FROM acp_checkout_sessions "
                "WHERE merchant_id = :merchant_id AND idempotency_key = :key AND id != :sid "
                "LIMIT 1",
                {
                    "merchant_id": str(session.get("merchant_id")),
                    "key": idempotency_key,
                    "sid": sid,
                },
            )
        except Exception:  # noqa: BLE001 — lookup failure must not block an honest completion
            other = None
        if other:
            raise AcpCheckoutSessionError(
                "Idempotency-Key already used by a different checkout session",
                code="idempotency_conflict",
                status_code=409,
            )

    # Fail-fast kill-switch pre-check BEFORE claiming or creating any order: a
    # dark lane must refuse without side effects. The full gate chain (incl.
    # amount caps) runs again on the charge itself below.
    from fastapi import HTTPException

    from services.agent_checkout_kill_switch import evaluate_tier2_charge

    pre_gate = evaluate_tier2_charge(protocol_name, merchant_id=str(session.get("merchant_id")))
    if not pre_gate.allowed:
        raise HTTPException(status_code=403, detail=pre_gate.as_error_detail())

    # Fulfillment identity, checked BEFORE the claim so a session that cannot
    # produce a real order refuses with ZERO side effects (same posture as the
    # validations above). These used to be silently defaulted to a placeholder
    # address and mailbox — a charge whose order shipped somewhere nobody gave
    # us. Refusing loudly is the safe answer.
    #
    # Scoped to a FRESH completion (the row is ready_for_payment): the only other
    # status reachable here is `completing`, i.e. a prior attempt that may ALREADY
    # have charged under the stored PSP key. Refusing that one would strand real
    # money — a resume creates no new fulfillment data, it only converges the
    # attempt that exists (and if it still has to create the order, the same
    # requirement is re-applied there, holding the claim instead of charging).
    if str(session.get("status") or "") == STATUS_READY_FOR_PAYMENT:
        _require_fulfillment_address(session)
        _require_customer_email(session)

        # Delegated-token allowance, checked here for the same two reasons the
        # fulfillment identity is: it belongs AFTER the kill-switch pre-gate and
        # the identity checks (a dark lane and a wrong agent must still refuse
        # first — an allowance error must never become an oracle for a lane that
        # is supposed to be dark), and BEFORE the claim so every refusal has
        # zero side effects. Scoped to a FRESH completion for the same reason as
        # the address/email checks: the only other status reachable here is
        # `completing`, an attempt that may ALREADY have charged under the
        # stored PSP key — refusing that one would strand real money without
        # preventing anything, since a resume replays the stored key rather than
        # minting a new charge. The single-use CAS bind below still runs on a
        # resume (idempotently for its own session).
        if _delegate_allowance_applies(payment_token):
            await _enforce_delegate_allowance(session, payment_token)

    # CLAIM BEFORE CHARGE: exactly one completion attempt may proceed. The
    # winning UPDATE also mints and STORES this attempt's PSP idempotency key.
    claim = await _claim_session(sid, idempotency_key)
    resumed = False
    if claim is None:
        current = await peek_session(sid)
        if not current:
            raise AcpCheckoutSessionError(
                "checkout session not found", code="acp_session_not_found", status_code=404
            )
        status_now = str(current.get("status") or "")
        if status_now == STATUS_COMPLETED:
            return _replay_completed(current, idempotency_key=idempotency_key, req_hash=req_hash)

        # A prior attempt crashed, charged-without-persisting, or hit an
        # ambiguous capture failure. Resuming is safe ONLY once the row is
        # stale (a fresh `completing` row is a live in-flight completion) and
        # ONLY for the single resumer whose conditional takeover lands. The
        # takeover covers an order_id-NULL row too: that is the crash window
        # between the claim and order creation, and the resumer simply re-enters
        # the order-create path — its capture still replays the STORED key.
        claim = None
        if status_now == STATUS_COMPLETING:
            claim = await _take_over_stale_completing(
                sid, _now_utc() - timedelta(seconds=COMPLETING_RESUME_AFTER_SECONDS)
            )
        if claim is None:
            raise AcpCheckoutSessionError(
                "a completion for this checkout session is already in progress",
                code="completion_in_progress",
                status_code=409,
            )
        resumed = True

    # Re-read post-claim: the claim/takeover UPDATE is the source of truth for
    # the attempt + key, but the rest of the row (order_id, items, metadata) is
    # read back fresh — a prior failed attempt may have left an order to reuse.
    session = await peek_session(sid) or session
    # THE key every capture below charges under: read back from the row, never
    # recomputed. A fresh claim → the new attempt's key; a resume → the stored
    # key of the attempt being resumed (replay, never a second charge).
    psp_idempotency_key = claim.psp_idempotency_key
    if resumed:
        logger.warning(
            "[AcpCheckoutSession] resuming stale completing session=%s attempt=%s "
            "order=%s psp_key=%s",
            sid, claim.capture_attempt, claim.order_id or "<none>", psp_idempotency_key,
        )

    # From here the claim is held. Any failure BEFORE the capture is dispatched
    # releases it (order_id kept — no charge can exist yet). Once the capture is
    # dispatched, only a DEFINITIVE failure releases it (see the classification
    # below); nothing after a successful capture does.
    from db.orders import get_order
    from services.acp_offsession_payment import (
        evaluate_acp_offsession_gates,
        execute_acp_offsession_payment,
        settle_acp_offsession_success,
    )

    order_id = str(session.get("order_id") or "").strip()
    # The token the CAPTURE is dispatched with. Identical to the caller's token
    # everywhere except a resume that already has an order, where it becomes
    # this session's own bound token so the replay stays parameter-identical to
    # the dispatch that actually happened (see _rebind_own_delegate_allowance).
    capture_payment_token = payment_token

    try:
        # SINGLE-USE CONSUMPTION, at claim time and before ANY order or charge
        # work: the claim we hold is the thing a bound token is bound to. Which
        # of the three cases applies is decided by the claim, not by the caller
        # (see the case analysis above the enforcement helpers).
        #
        # Every refusal below is still pre-dispatch, so the `except
        # BaseException` handler releases a FRESH claim and HOLDS a RESUMED one
        # (review B1). Holding is what makes the order_id-NULL refusal safe to
        # repeat: a later resume presenting a valid token converges the same row.
        if _delegate_allowance_applies(payment_token):
            if not resumed:
                # FRESH claim — checks 1–6 already passed pre-claim.
                await _bind_delegate_allowance(sid, payment_token)
            elif not order_id:
                # RESUMED claim, NO order: provably nothing was ever dispatched
                # (capture is only reachable after _persist_order_id succeeds),
                # so this resume does genuinely fresh work and is validated
                # exactly like a fresh completion before it may consume anything.
                await _enforce_delegate_allowance(session, payment_token)
                await _bind_delegate_allowance(sid, payment_token)
            else:
                # RESUMED claim WITH an order: the caller's token is inert (the
                # capture replays the stored PSP key). Re-bind only our OWN
                # token; never consume a new allowance here.
                capture_payment_token = await _rebind_own_delegate_allowance(
                    sid, payment_token
                )

        quote_currency: Optional[str] = None
        if not order_id:
            # Fresh quote at completion time (mirrors pivota-acp's real capture:
            # the shipping address must match the order for the quote fingerprint).
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

            # Both already validated pre-claim; re-derived here from the row
            # re-read after the claim (never defaulted — see the helpers).
            customer_email = _require_customer_email(session)
            address = _require_fulfillment_address(session)

            quote = await _build_quote(
                merchant_id=str(session.get("merchant_id")),
                items=items,
                customer_email=str(customer_email),
                shipping_address=address,
                agent_id=caller_agent,
            )
            quote_currency = str(quote.get("currency") or "") or None

            order_id = await _create_pivota_order(
                session=session,
                quote=quote,
                agent_context=agent_context,
                protocol_name=str(protocol_name or "acp"),
                idempotency_key=idempotency_key,
            )
            # PERSIST the order on the row BEFORE the charge — a retry must
            # reuse it, never mint another. Not best-effort.
            try:
                await _persist_order_id(sid, order_id)
            except Exception as persist_exc:  # noqa: BLE001
                raise AcpCheckoutSessionError(
                    f"order_record_failed: {str(persist_exc)[:200]}",
                    code="acp_session_persist_failed",
                    status_code=503,
                ) from persist_exc

        # Charge through the shared gate chain + off-session capture (the exact
        # code POST /agent/v1/payments runs — services/acp_offsession_payment).
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
        order_total = order.get("total_amount")
        if order_total is None:
            order_total = order.get("total")
        if order_total is None:
            raise AcpCheckoutSessionError(
                "order total not found", code="acp_order_create_failed", status_code=502
            )
        currency = str(order.get("currency") or quote_currency or session.get("currency") or "USD")
        amount_cents = to_minor_units(order_total, currency)

        gates = evaluate_acp_offsession_gates(
            protocol_name=order_metadata.get("protocol_name") or protocol_name,
            merchant_id=str(session.get("merchant_id")),
            amount_cents=amount_cents,
            order_id=order_id,
        )
        if not gates.engaged:
            # Kill-switch permits, but neither the test- nor live-capture lane is
            # armed. An off-session completion has no buyer present to
            # client-confirm a payment surface — fail closed instead of pretending.
            raise AcpCheckoutSessionError(
                "no off-session capture lane is enabled for this charge",
                code="acp_capture_lane_disabled",
                status_code=403,
            )

    except BaseException:
        # Nothing has been dispatched to a PSP under THIS claim (gate refusal,
        # quote/order failure, lane disabled) — but that reasoning only holds for
        # a FRESH claim. A RESUMED claim exists precisely because a prior attempt
        # may have left a charge in flight under the stored key; releasing it
        # here would let the next attempt mint a NEW key and double-charge
        # (review B1). So: fresh claim → release for retry (order_id kept);
        # resumed claim → hold, the stale-takeover path converges it later.
        if not resumed:
            await _revert_claim_to_ready(sid)
        raise

    # ---- the charge itself: a failure here is CLASSIFIED, never blanket-reverted
    try:
        outcome = await execute_acp_offsession_payment(
            gates=gates,
            merchant_id=str(session.get("merchant_id")),
            order_id=order_id,
            currency=currency,
            # The STORED, attempt-scoped session key — never the caller's key
            # and never recomputed here (see the module docstring).
            idempotency_key=psp_idempotency_key,
            payment_method_token=(capture_payment_token or None),
            agent_id=str(caller_agent or ""),
            order_metadata=order_metadata,
        )
    except BaseException as capture_exc:
        # The dispatch itself blew up (timeout, transport, cancellation, bug):
        # we do NOT know whether the PSP took the charge. Releasing the claim
        # would let the next attempt mint a NEW key and charge a second time, so
        # the claim and its stored key STAY — a retry after the stale window
        # replays this exact PSP request.
        logger.error(
            "[AcpCheckoutSession] capture dispatch failed AMBIGUOUSLY session=%s "
            "order=%s psp_key=%s: %s — claim held for replay",
            sid, order_id, psp_idempotency_key, str(capture_exc)[:200],
        )
        raise AcpCheckoutSessionError(
            "the capture could not be confirmed; retry to converge "
            "(the same PSP request is replayed, it will not charge twice)",
            code="acp_capture_pending_retry",
            status_code=502,
            extra={
                "order_id": order_id,
                "retry_after_seconds": COMPLETING_RESUME_AFTER_SECONDS,
            },
        ) from capture_exc

    if not outcome.success:
        if _capture_failure_is_definitive(outcome):
            # The PSP refused (or nothing was ever dispatched): no charge is in
            # flight, so release the claim. The next attempt mints the NEXT
            # attempt key, which is what lets the buyer retry with another card
            # instead of replaying a decline the PSP has already cached.
            await _revert_claim_to_ready(sid)
            raise AcpCheckoutSessionError(
                f"capture_failed:{outcome.error_code or 'unknown'}: {str(outcome.error or '')[:200]}",
                code="acp_capture_failed",
                status_code=502,
            )
        # Not provably a refusal (SCA/requires_action, unresolved provider
        # error, HTTP/transport failure): treat as possibly-charged.
        logger.error(
            "[AcpCheckoutSession] capture failed AMBIGUOUSLY session=%s order=%s "
            "psp_key=%s status=%s code=%s — claim held for replay",
            sid, order_id, psp_idempotency_key, outcome.status, outcome.error_code,
        )
        raise AcpCheckoutSessionError(
            f"capture_unconfirmed:{outcome.error_code or 'unknown'}: "
            f"{str(outcome.error or '')[:200]}",
            code="acp_capture_pending_retry",
            status_code=502,
            extra={
                "order_id": order_id,
                "retry_after_seconds": COMPLETING_RESUME_AFTER_SECONDS,
            },
        )

    # Capture SUCCEEDED beyond this point — the claim is never released now.
    settle_flags = await settle_acp_offsession_success(
        gates=gates,
        outcome=outcome,
        order=order,
        order_id=order_id,
        merchant_id=str(session.get("merchant_id")),
        currency=currency,
        idempotency_key=idempotency_key,
        agent_id=str(caller_agent or ""),
        order_metadata=order_metadata,
    )

    completion: Dict[str, Any] = {
        "status": STATUS_COMPLETED,
        "checkout_session_id": sid,
        "order_id": order_id,
        "payment_status": "succeeded",
        "psp_used": outcome.psp_used,
        "payment_intent_id": outcome.payment_intent_id,
        "amount_cents": amount_cents,
        "currency": currency,
        "order": {
            "id": order_id,
            "checkout_session_id": sid,
            "permalink_url": _order_permalink_url(order_id),
        },
        _COMPLETION_REQUEST_HASH_KEY: req_hash,
    }
    if isinstance(settle_flags, dict) and settle_flags.get("reconciliation_needed"):
        # The charge stands but a post-capture settlement write kept failing —
        # durably queryable marker for ops reconciliation.
        completion["reconciliation_needed"] = True

    try:
        await _persist_completion(
            session_id=sid,
            order_id=order_id,
            completion=completion,
            idempotency_key=idempotency_key,
        )
    except Exception as persist_exc:  # noqa: BLE001
        # A capture happened that we could NOT durably record. Never report
        # success. The row stays `completing`+order_id, so a (stale) retry
        # resumes with the same STORED PSP key and re-attempts this persist.
        logger.error(
            "[AcpCheckoutSession] completion persist FAILED after successful capture "
            "session=%s order=%s intent=%s: %s",
            sid, order_id, outcome.payment_intent_id, str(persist_exc)[:200],
        )
        raise AcpCheckoutSessionError(
            "capture succeeded but the completion could not be durably recorded; "
            "retry to converge (the charge will not repeat)",
            code="acp_completion_persist_failed",
            status_code=502,
            extra={"reconciliation_required": True, "order_id": order_id},
        ) from persist_exc

    return _public_completion(completion)
