from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import stripe

from config.settings import settings
from db.database import database

logger = logging.getLogger(__name__)

stripe_client = stripe.StripeClient(api_key=settings.stripe_secret_key or "")

_DASH = "\u2013"
_SCHEMA_GUARD_ATTEMPTED = False


class InvoiceGenerationError(Exception):
    """Base error for GMV-take invoice generation failures."""


class DisputeOnFinalizedInvoiceError(InvoiceGenerationError):
    """Raised when a dispute is applied to an invoice that is no longer draft."""


class MerchantNotEnrolledError(InvoiceGenerationError):
    """Raised when a merchant has no Stripe customer for monetization billing."""


_INSERT_BILLING_RUN_QUERY = """
INSERT INTO billing_runs (period_start, period_end, idempotency_key, status, created_at)
VALUES (:period_start, :period_end, :idempotency_key, 'running', NOW())
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING id
"""

_SELECT_BILLING_RUN_BY_KEY_QUERY = """
SELECT id
FROM billing_runs
WHERE idempotency_key = :idempotency_key
LIMIT 1
"""

_DISTINCT_MERCHANTS_QUERY = """
SELECT DISTINCT merchant_id
FROM gmv_attribution_daily
WHERE date BETWEEN :period_start AND :period_end
  AND take_amount_cents > 0
ORDER BY merchant_id
"""

_COMPLETE_BILLING_RUN_QUERY = """
UPDATE billing_runs
SET status = 'completed',
    completed_at = NOW()
WHERE id = :billing_run_id
"""

# `merchants.id` is an integer PK, while GMV rollups carry the operational
# merchant id. Match the monetization bridge used by gmv_aggregation_service.
_MERCHANT_STRIPE_CUSTOMER_QUERY = """
SELECT m.stripe_customer_id
FROM merchants m
JOIN user_subscriptions us ON us.id = m.subscription_id
WHERE us.merchant_id = :merchant_id
ORDER BY us.updated_at DESC NULLS LAST, us.created_at DESC NULLS LAST
LIMIT 1
"""

_GMV_ROWS_QUERY = """
SELECT
    id,
    date,
    merchant_id,
    agent_id,
    channel_partner_id,
    gross_attributed_gmv_cents,
    refund_amount_cents,
    net_attributed_gmv_cents,
    take_rate_bp,
    take_amount_cents,
    protocol_name
FROM gmv_attribution_daily
WHERE merchant_id = :merchant_id
  AND date BETWEEN :period_start AND :period_end
  AND take_amount_cents > 0
ORDER BY date ASC, id ASC
"""

_INSERT_BILLING_RUN_ITEM_QUERY = """
INSERT INTO billing_run_items (
  billing_run_id, merchant_id, source_type, source_id,
  stripe_invoice_item_id, stripe_invoice_id, amount_cents, description
) VALUES (
  :billing_run_id, :merchant_id, :source_type, :source_id,
  :stripe_invoice_item_id, :stripe_invoice_id, :amount_cents, :description
)
"""

_INSERT_INVOICE_QUERY = """
INSERT INTO invoices (
  merchant_id, billing_period_start, billing_period_end, billing_run_id,
  stripe_invoice_id, stripe_customer_id, total_cents, status, due_date, created_at
) VALUES (
  :merchant_id, :period_start, :period_end, :billing_run_id,
  :stripe_invoice_id, :stripe_customer_id, :total_cents, 'draft', :due_date, NOW()
)
"""

_MARK_INVOICE_FINALIZING_QUERY = """
UPDATE invoices
SET status = 'finalizing'
WHERE stripe_invoice_id = :stripe_invoice_id
"""

_SELECT_INVOICE_DISPUTE_QUERY = """
SELECT
    id,
    invoice_id,
    merchant_id,
    disputed_line_items_jsonb,
    reason,
    status
FROM invoice_disputes
WHERE id = :invoice_dispute_id
LIMIT 1
"""

_SELECT_INVOICE_BY_ID_QUERY = """
SELECT
    id,
    merchant_id,
    billing_run_id,
    stripe_invoice_id,
    stripe_customer_id,
    status
FROM invoices
WHERE id = :invoice_id
LIMIT 1
"""

_SELECT_BILLING_RUN_ITEM_QUERY = """
SELECT
    id,
    billing_run_id,
    merchant_id,
    source_type,
    source_id,
    stripe_invoice_item_id,
    stripe_invoice_id,
    amount_cents,
    description
FROM billing_run_items
WHERE id = :billing_run_item_id
LIMIT 1
"""

_VOID_BILLING_RUN_ITEM_QUERY = """
UPDATE billing_run_items
SET voided_at = NOW()
WHERE id = :billing_run_item_id
"""

_APPLY_INVOICE_DISPUTE_QUERY = """
UPDATE invoice_disputes
SET status = 'applied',
    resolved_at = NOW()
WHERE id = :invoice_dispute_id
"""

_SCHEMA_GUARD_STATEMENTS = (
    "ALTER TABLE IF EXISTS invoices ADD COLUMN IF NOT EXISTS billing_run_id BIGINT",
    "ALTER TABLE IF EXISTS invoices ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT",
    "ALTER TABLE IF EXISTS billing_run_items ADD COLUMN IF NOT EXISTS voided_at TIMESTAMPTZ",
    (
        "ALTER TABLE IF EXISTS invoice_disputes "
        "ADD COLUMN IF NOT EXISTS disputed_line_items_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb"
    ),
    "ALTER TABLE IF EXISTS invoices DROP CONSTRAINT IF EXISTS ck_invoices_status",
    (
        "ALTER TABLE IF EXISTS invoices ADD CONSTRAINT ck_invoices_status CHECK ("
        "status IN ('draft', 'finalizing', 'finalized', 'paid', 'failed', "
        "'payment_failed', 'void', 'uncollectible'))"
    ),
    "ALTER TABLE IF EXISTS invoice_disputes DROP CONSTRAINT IF EXISTS ck_invoice_disputes_status",
    (
        "ALTER TABLE IF EXISTS invoice_disputes ADD CONSTRAINT ck_invoice_disputes_status CHECK ("
        "status IN ('open', 'under_review', 'applied', 'resolved', 'rejected', 'cancelled'))"
    ),
    "ALTER TABLE IF EXISTS invoice_disputes DROP CONSTRAINT IF EXISTS ck_invoice_disputes_resolved_status",
    (
        "ALTER TABLE IF EXISTS invoice_disputes ADD CONSTRAINT ck_invoice_disputes_resolved_status CHECK ("
        "resolved_at IS NULL OR status IN ('applied', 'resolved', 'rejected', 'cancelled'))"
    ),
)


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _as_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _stripe_id(value: Any) -> str:
    return _as_text(getattr(value, "id", None) or _get(value, "id"))


def _line_description(row: Any) -> str:
    agent_id = _as_text(_get(row, "agent_id")) or "direct"
    return f"GMV Take Rate {_DASH} Agent {agent_id} {_DASH} {_get(row, 'date')}"


def _normalize_disputed_items(raw_value: Any) -> list[dict[str, int]]:
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        raw_value = json.loads(raw_value)
    if isinstance(raw_value, dict):
        raw_value = [raw_value]
    if not isinstance(raw_value, Iterable):
        raise InvoiceGenerationError("disputed_line_items_jsonb must be a JSON array")

    normalized: list[dict[str, int]] = []
    for item in raw_value:
        if not isinstance(item, dict):
            raise InvoiceGenerationError("disputed line item entries must be JSON objects")

        raw_id = (
            item.get("billing_run_item_id")
            or item.get("billing_run_items_id")
            or item.get("billing_run_item")
            or item.get("id")
        )
        if raw_id is None:
            raise InvoiceGenerationError("disputed line item missing billing_run_item_id")

        raw_adjusted_amount = item.get("adjusted_amount_cents", item.get("adjusted_amount"))
        if raw_adjusted_amount is None:
            raise InvoiceGenerationError("disputed line item missing adjusted_amount_cents")

        adjusted_amount_cents = int(raw_adjusted_amount)
        if adjusted_amount_cents < 0:
            raise InvoiceGenerationError("adjusted_amount_cents must be non-negative")

        normalized.append(
            {
                "billing_run_item_id": int(raw_id),
                "adjusted_amount_cents": adjusted_amount_cents,
            }
        )

    return normalized


async def _ensure_invoice_generation_schema() -> None:
    global _SCHEMA_GUARD_ATTEMPTED

    if _SCHEMA_GUARD_ATTEMPTED:
        return
    _SCHEMA_GUARD_ATTEMPTED = True

    for statement in _SCHEMA_GUARD_STATEMENTS:
        try:
            await database.execute(statement)
        except Exception:
            logger.debug("Invoice generation schema guard skipped statement: %s", statement, exc_info=True)


async def run_billing_cycle(period_start: date, period_end: date) -> int:
    """Run an idempotent GMV-take billing cycle and return billing_runs.id."""

    idempotency_key = f"{period_start.isoformat()}-billing"
    row = await database.fetch_one(
        _INSERT_BILLING_RUN_QUERY,
        {
            "period_start": period_start,
            "period_end": period_end,
            "idempotency_key": idempotency_key,
        },
    )

    if not row:
        existing = await database.fetch_one(
            _SELECT_BILLING_RUN_BY_KEY_QUERY,
            {"idempotency_key": idempotency_key},
        )
        if not existing:
            raise InvoiceGenerationError(f"Unable to resolve billing run for {idempotency_key}")
        return int(_get(existing, "id"))

    billing_run_id = int(_get(row, "id"))
    merchant_rows = await database.fetch_all(
        _DISTINCT_MERCHANTS_QUERY,
        {
            "period_start": period_start,
            "period_end": period_end,
        },
    )

    for merchant_row in merchant_rows:
        merchant_id = _as_text(_get(merchant_row, "merchant_id"))
        if not merchant_id:
            continue
        try:
            await generate_merchant_invoice(billing_run_id, merchant_id, period_start, period_end)
        except Exception:
            logger.exception(
                "Merchant invoice generation failed billing_run_id=%s merchant_id=%s",
                billing_run_id,
                merchant_id,
            )

    await database.execute(_COMPLETE_BILLING_RUN_QUERY, {"billing_run_id": billing_run_id})
    return billing_run_id


async def generate_merchant_invoice(
    billing_run_id: int,
    merchant_id: str,
    period_start: date,
    period_end: date,
) -> str | None:
    """Create one draft Stripe invoice and attached GMV invoice items for a merchant."""

    merchant_row = await database.fetch_one(
        _MERCHANT_STRIPE_CUSTOMER_QUERY,
        {"merchant_id": merchant_id},
    )
    stripe_customer_id = _as_text(_get(merchant_row, "stripe_customer_id"))
    if not stripe_customer_id:
        logger.warning(
            "Skipping invoice generation for merchant_id=%s because stripe_customer_id is missing",
            merchant_id,
        )
        return None

    rows = await database.fetch_all(
        _GMV_ROWS_QUERY,
        {
            "merchant_id": merchant_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    )
    if not rows:
        return None

    await _ensure_invoice_generation_schema()

    draft_invoice_id = ""
    created_item_ids: list[str] = []
    total_cents = sum(_as_int(_get(row, "take_amount_cents")) for row in rows)

    try:
        async with database.transaction():
            invoice = await asyncio.to_thread(
                stripe_client.v1.invoices.create,
                params={
                    "customer": stripe_customer_id,
                    "collection_method": "charge_automatically",
                    # auto_advance=True: Stripe auto-finalizes the draft after its
                    # internal timer (~1h) if our explicit finalize_invoice() never
                    # fires. In the happy path we attach InvoiceItems and finalize
                    # immediately, so Stripe's timer never elapses. The risk is a
                    # crash between Invoice.create and the InvoiceItem.create loop:
                    # Stripe could auto-finalize a $0 empty invoice. Recovery is
                    # stripe.Invoice.void() — documented in runbook 03.
                    "auto_advance": True,
                    "description": f"Pivota {_DASH} {period_start.strftime('%B %Y')}",
                    "metadata": {
                        "merchant_id": merchant_id,
                        "billing_run_id": str(billing_run_id),
                        "period_start": period_start.isoformat(),
                        "period_end": period_end.isoformat(),
                    },
                },
            )
            draft_invoice_id = _stripe_id(invoice)
            if not draft_invoice_id:
                raise InvoiceGenerationError("Stripe Invoice.create returned no invoice id")

            for row in rows:
                description = _line_description(row)
                item = await asyncio.to_thread(
                    stripe_client.v1.invoice_items.create,
                    params={
                        "customer": stripe_customer_id,
                        "invoice": draft_invoice_id,
                        "amount": _as_int(_get(row, "take_amount_cents")),
                        "currency": "usd",
                        "description": description,
                        "metadata": {
                            "merchant_id": merchant_id,
                            "gmv_rollup_id": str(_get(row, "id")),
                            "billing_run_id": str(billing_run_id),
                        },
                    },
                )
                stripe_invoice_item_id = _stripe_id(item)
                if not stripe_invoice_item_id:
                    raise InvoiceGenerationError("Stripe InvoiceItem.create returned no invoice item id")
                created_item_ids.append(stripe_invoice_item_id)

                await database.execute(
                    _INSERT_BILLING_RUN_ITEM_QUERY,
                    {
                        "billing_run_id": billing_run_id,
                        "merchant_id": merchant_id,
                        "source_type": "gmv_rollup",
                        "source_id": int(_get(row, "id")),
                        "stripe_invoice_item_id": stripe_invoice_item_id,
                        "stripe_invoice_id": draft_invoice_id,
                        "amount_cents": _as_int(_get(row, "take_amount_cents")),
                        "description": description,
                    },
                )

            await database.execute(
                _INSERT_INVOICE_QUERY,
                {
                    "merchant_id": merchant_id,
                    "period_start": period_start,
                    "period_end": period_end,
                    "billing_run_id": billing_run_id,
                    "stripe_invoice_id": draft_invoice_id,
                    "stripe_customer_id": stripe_customer_id,
                    "total_cents": total_cents,
                    "due_date": period_end + timedelta(days=5),
                },
            )
    except Exception:
        logger.exception(
            "Stripe invoice generation failed billing_run_id=%s merchant_id=%s "
            "stripe_invoice_id=%s created_invoice_item_ids=%s",
            billing_run_id,
            merchant_id,
            draft_invoice_id or None,
            created_item_ids,
        )
        raise

    return draft_invoice_id


async def finalize_invoice(stripe_invoice_id: str) -> None:
    """Mark a local invoice finalizing, then ask Stripe to finalize and collect it."""

    await _ensure_invoice_generation_schema()
    await database.execute(
        _MARK_INVOICE_FINALIZING_QUERY,
        {"stripe_invoice_id": stripe_invoice_id},
    )
    await asyncio.to_thread(
        stripe_client.v1.invoices.finalize_invoice,
        stripe_invoice_id,
        params={"auto_advance": True},
    )


async def handle_dispute(invoice_dispute_id: int) -> None:
    """Apply a merchant invoice dispute by replacing draft Stripe invoice items."""

    await _ensure_invoice_generation_schema()
    dispute = await database.fetch_one(
        _SELECT_INVOICE_DISPUTE_QUERY,
        {"invoice_dispute_id": invoice_dispute_id},
    )
    if not dispute:
        raise InvoiceGenerationError(f"Invoice dispute not found: {invoice_dispute_id}")

    invoice_id = int(_get(dispute, "invoice_id"))
    invoice = await database.fetch_one(
        _SELECT_INVOICE_BY_ID_QUERY,
        {"invoice_id": invoice_id},
    )
    if not invoice:
        raise InvoiceGenerationError(f"Invoice not found for dispute: {invoice_id}")

    invoice_status = _as_text(_get(invoice, "status"))
    if invoice_status != "draft":
        raise DisputeOnFinalizedInvoiceError(
            f"Invoice {invoice_id} is {invoice_status}; disputes can only modify draft invoices"
        )

    draft_invoice_id = _as_text(_get(invoice, "stripe_invoice_id"))
    stripe_customer_id = _as_text(_get(invoice, "stripe_customer_id"))
    merchant_id = _as_text(_get(invoice, "merchant_id") or _get(dispute, "merchant_id"))
    if not draft_invoice_id:
        raise InvoiceGenerationError(f"Invoice {invoice_id} has no stripe_invoice_id")
    if not stripe_customer_id:
        raise InvoiceGenerationError(f"Invoice {invoice_id} has no stripe_customer_id")

    disputed_items = _normalize_disputed_items(_get(dispute, "disputed_line_items_jsonb"))

    async with database.transaction():
        for disputed_item in disputed_items:
            billing_run_item_id = disputed_item["billing_run_item_id"]
            adjusted_amount_cents = disputed_item["adjusted_amount_cents"]
            original_item = await database.fetch_one(
                _SELECT_BILLING_RUN_ITEM_QUERY,
                {"billing_run_item_id": billing_run_item_id},
            )
            if not original_item:
                raise InvoiceGenerationError(f"Billing run item not found: {billing_run_item_id}")

            stripe_invoice_item_id = _as_text(_get(original_item, "stripe_invoice_item_id"))
            if not stripe_invoice_item_id:
                raise InvoiceGenerationError(f"Billing run item {billing_run_item_id} has no Stripe item id")

            await asyncio.to_thread(
                stripe_client.v1.invoice_items.delete,
                stripe_invoice_item_id,
            )

            if adjusted_amount_cents > 0:
                description = (
                    _as_text(_get(original_item, "description"))
                    or f"GMV Take Rate {_DASH} adjusted dispute {invoice_dispute_id}"
                )
                replacement = await asyncio.to_thread(
                    stripe_client.v1.invoice_items.create,
                    params={
                        "customer": stripe_customer_id,
                        "invoice": draft_invoice_id,
                        "amount": adjusted_amount_cents,
                        "currency": "usd",
                        "description": description,
                        "metadata": {
                            "merchant_id": merchant_id,
                            "invoice_dispute_id": str(invoice_dispute_id),
                            "original_billing_run_item_id": str(billing_run_item_id),
                            "billing_run_id": str(_get(original_item, "billing_run_id")),
                        },
                    },
                )
                replacement_item_id = _stripe_id(replacement)
                if not replacement_item_id:
                    raise InvoiceGenerationError("Stripe replacement InvoiceItem.create returned no id")

                await database.execute(
                    _INSERT_BILLING_RUN_ITEM_QUERY,
                    {
                        "billing_run_id": int(_get(original_item, "billing_run_id")),
                        "merchant_id": merchant_id,
                        "source_type": "dispute_adj",
                        "source_id": invoice_dispute_id,
                        "stripe_invoice_item_id": replacement_item_id,
                        "stripe_invoice_id": draft_invoice_id,
                        "amount_cents": adjusted_amount_cents,
                        "description": description,
                    },
                )

            await database.execute(
                _VOID_BILLING_RUN_ITEM_QUERY,
                {"billing_run_item_id": billing_run_item_id},
            )

        await database.execute(
            _APPLY_INVOICE_DISPUTE_QUERY,
            {"invoice_dispute_id": invoice_dispute_id},
        )
