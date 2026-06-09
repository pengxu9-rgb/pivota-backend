from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import stripe

from config.settings import settings
from db.database import database
from services.billing import monthly_brand_statements_service


stripe_client = stripe.StripeClient(api_key=settings.stripe_secret_key or "")


class CreditOverageBillingError(Exception):
    """Raised when a credit-overage invoice cannot be created."""


async def create_overage_invoice(statement_id: int) -> int:
    """Given a frozen statement with overage_credits > 0, create a Stripe invoice line for the overage charge.

    Flow:
    1. SELECT FOR UPDATE the statement, assert status='frozen' and overage_credits > 0 and overage_invoice_id IS NULL.
    2. Compose the customer-facing line description EXACTLY:
         f"{overage_credits:,} credits overage — ${overage_revenue_usd:.2f}"
       That string is the only credit→dollar adjacency permitted in any merchant-facing surface.
       No "at $X per credit", no rate disclosure, no premium-multiplier mention.
    3. Create an invoice item via Stripe (use existing Stripe client patterns in routes/billing_routes.py).
    4. INSERT a row into `invoices` mirror table with:
         merchant_id, billing_period_start = statement.calendar_month, billing_period_end = calendar_month + 1 month,
         total_cents = overage_revenue_usd_cents, status='draft' (will become finalized/paid via existing Stripe webhook handler).
    5. INSERT a row into `billing_run_items` with source_type='credit_overage', source_id=statement_id.
    6. Call monthly_brand_statements_service.mark_invoiced(statement_id, invoices.id).

    Returns: the new invoices.id.
    """

    async with database.transaction():
        statement = await _locked_statement(statement_id)
        overage_credits = int(_row_get(statement, "overage_credits") or 0)
        if _row_text(statement, "status") != "frozen":
            raise CreditOverageBillingError("Statement must be frozen before overage invoicing")
        if overage_credits <= 0:
            raise CreditOverageBillingError("Statement has no credit overage to invoice")
        if _row_get(statement, "overage_invoice_id") is not None:
            raise CreditOverageBillingError("Statement already has an overage invoice")

        merchant_id = _row_text(statement, "merchant_id")
        calendar_month = _row_get(statement, "calendar_month")
        if not isinstance(calendar_month, date):
            raise CreditOverageBillingError("Statement calendar_month is invalid")
        period_end = _next_month(calendar_month)
        overage_revenue_cents = int(_row_get(statement, "overage_revenue_usd_cents") or 0)
        stripe_customer_id = await _stripe_customer_id_for_merchant(merchant_id)
        billing_run_id = await _ensure_overage_billing_run(
            merchant_id=merchant_id,
            period_start=calendar_month,
            period_end=period_end,
        )

        description = _overage_line_description(
            overage_credits=overage_credits,
            overage_revenue_usd_cents=overage_revenue_cents,
        )
        stripe_invoice_id = await _create_stripe_invoice(
            statement_id=statement_id,
            merchant_id=merchant_id,
            stripe_customer_id=stripe_customer_id,
            calendar_month=calendar_month,
            period_end=period_end,
        )
        stripe_invoice_item_id = await _create_stripe_invoice_item(
            statement_id=statement_id,
            merchant_id=merchant_id,
            stripe_customer_id=stripe_customer_id,
            stripe_invoice_id=stripe_invoice_id,
            amount_cents=overage_revenue_cents,
            description=description,
        )
        invoice_id = await _insert_invoice_mirror(
            merchant_id=merchant_id,
            calendar_month=calendar_month,
            period_end=period_end,
            billing_run_id=billing_run_id,
            stripe_invoice_id=stripe_invoice_id,
            stripe_customer_id=stripe_customer_id,
            total_cents=overage_revenue_cents,
        )
        await _insert_billing_run_item(
            billing_run_id=billing_run_id,
            merchant_id=merchant_id,
            statement_id=statement_id,
            stripe_invoice_item_id=stripe_invoice_item_id,
            stripe_invoice_id=stripe_invoice_id,
            amount_cents=overage_revenue_cents,
            description=description,
        )
        await monthly_brand_statements_service.mark_invoiced(statement_id, invoice_id)
        return invoice_id


async def finalize_statement_no_overage(statement_id: int) -> None:
    """For statements with overage_credits = 0, advance frozen → invoiced with overage_invoice_id=NULL."""

    row = await database.fetch_one(
        """
        UPDATE monthly_brand_statements
        SET status = 'invoiced',
            invoiced_at = NOW(),
            overage_invoice_id = NULL,
            updated_at = NOW()
        WHERE id = :statement_id
          AND status = 'frozen'
          AND overage_credits = 0
          AND overage_invoice_id IS NULL
        RETURNING id
        """,
        {"statement_id": statement_id},
    )
    if not row:
        raise CreditOverageBillingError(
            f"Statement {statement_id} must be frozen with no overage and no invoice"
        )


async def _locked_statement(statement_id: int) -> Any:
    row = await database.fetch_one(
        """
        SELECT
          id,
          merchant_id,
          calendar_month,
          status,
          overage_credits,
          overage_revenue_usd_cents,
          overage_invoice_id
        FROM monthly_brand_statements
        WHERE id = :statement_id
        FOR UPDATE
        """,
        {"statement_id": statement_id},
    )
    if not row:
        raise CreditOverageBillingError(f"Statement not found: {statement_id}")
    return row


async def _stripe_customer_id_for_merchant(merchant_id: str) -> str:
    # Shared resolver: merchant_id-first, onboarding contact_email fallback.
    # See billing_routes.resolve_merchant_stripe_customer_id for why the
    # merchants/merchant_onboarding link is unreliable.
    from routes.billing_routes import resolve_merchant_stripe_customer_id

    stripe_customer_id = await resolve_merchant_stripe_customer_id(database, merchant_id)
    if not stripe_customer_id:
        raise CreditOverageBillingError(
            f"Merchant {merchant_id} has no Stripe customer for overage invoicing"
        )
    return stripe_customer_id


async def _ensure_overage_billing_run(
    *,
    merchant_id: str,
    period_start: date,
    period_end: date,
) -> int:
    # billing_run_items requires a billing_run_id. Overage invoicing is
    # statement-scoped in this PR, so create a narrow completed run for audit
    # lineage rather than piggybacking on the GMV billing cycle.
    idempotency_key = f"{period_start.isoformat()}-credit-overage-{merchant_id}"
    row = await database.fetch_one(
        """
        INSERT INTO billing_runs (
          period_start,
          period_end,
          idempotency_key,
          status,
          started_at,
          completed_at,
          created_at
        ) VALUES (
          :period_start,
          :period_end,
          :idempotency_key,
          'completed',
          NOW(),
          NOW(),
          NOW()
        )
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        {
            "period_start": period_start,
            "period_end": period_end,
            "idempotency_key": idempotency_key,
        },
    )
    if row:
        return int(_row_get(row, "id"))

    existing = await database.fetch_one(
        """
        SELECT id
        FROM billing_runs
        WHERE idempotency_key = :idempotency_key
        LIMIT 1
        """,
        {"idempotency_key": idempotency_key},
    )
    if not existing:
        raise CreditOverageBillingError("Unable to resolve overage billing run")
    return int(_row_get(existing, "id"))


async def _create_stripe_invoice(
    *,
    statement_id: int,
    merchant_id: str,
    stripe_customer_id: str,
    calendar_month: date,
    period_end: date,
) -> str:
    invoice = await asyncio.to_thread(
        stripe_client.v1.invoices.create,
        params={
            "customer": stripe_customer_id,
            "collection_method": "charge_automatically",
            "currency": "usd",
            "auto_advance": True,
            "description": f"Pivota credit overage {calendar_month:%B %Y}",
            "metadata": {
                "merchant_id": merchant_id,
                "monthly_brand_statement_id": str(statement_id),
                "period_start": calendar_month.isoformat(),
                "period_end": period_end.isoformat(),
                "source_type": "credit_overage",
            },
        },
        options={"idempotency_key": f"credit_overage_invoice:{statement_id}"},
    )
    stripe_invoice_id = _stripe_id(invoice)
    if not stripe_invoice_id:
        raise CreditOverageBillingError("Stripe Invoice.create returned no invoice id")
    return stripe_invoice_id


async def _create_stripe_invoice_item(
    *,
    statement_id: int,
    merchant_id: str,
    stripe_customer_id: str,
    stripe_invoice_id: str,
    amount_cents: int,
    description: str,
) -> str:
    item = await asyncio.to_thread(
        stripe_client.v1.invoice_items.create,
        params={
            "customer": stripe_customer_id,
            "invoice": stripe_invoice_id,
            "amount": amount_cents,
            "currency": "usd",
            "description": description,
            "metadata": {
                "merchant_id": merchant_id,
                "monthly_brand_statement_id": str(statement_id),
                "source_type": "credit_overage",
            },
        },
        options={"idempotency_key": f"credit_overage_invoice_item:{statement_id}"},
    )
    stripe_invoice_item_id = _stripe_id(item)
    if not stripe_invoice_item_id:
        raise CreditOverageBillingError("Stripe InvoiceItem.create returned no invoice item id")
    return stripe_invoice_item_id


async def _insert_invoice_mirror(
    *,
    merchant_id: str,
    calendar_month: date,
    period_end: date,
    billing_run_id: int,
    stripe_invoice_id: str,
    stripe_customer_id: str,
    total_cents: int,
) -> int:
    row = await database.fetch_one(
        """
        INSERT INTO invoices (
          merchant_id,
          billing_period_start,
          billing_period_end,
          billing_run_id,
          stripe_invoice_id,
          stripe_customer_id,
          total_cents,
          status,
          due_date,
          created_at
        ) VALUES (
          :merchant_id,
          :calendar_month,
          :period_end,
          :billing_run_id,
          :stripe_invoice_id,
          :stripe_customer_id,
          :total_cents,
          'draft',
          :due_date,
          NOW()
        )
        ON CONFLICT (stripe_invoice_id) DO NOTHING
        RETURNING id
        """,
        {
            "merchant_id": merchant_id,
            "calendar_month": calendar_month,
            "period_end": period_end,
            "billing_run_id": billing_run_id,
            "stripe_invoice_id": stripe_invoice_id,
            "stripe_customer_id": stripe_customer_id,
            "total_cents": total_cents,
            "due_date": period_end + timedelta(days=5),
        },
    )
    if row:
        return int(_row_get(row, "id"))

    existing = await database.fetch_one(
        "SELECT id FROM invoices WHERE stripe_invoice_id = :stripe_invoice_id LIMIT 1",
        {"stripe_invoice_id": stripe_invoice_id},
    )
    if not existing:
        raise CreditOverageBillingError("Unable to resolve local invoice mirror")
    return int(_row_get(existing, "id"))


async def _insert_billing_run_item(
    *,
    billing_run_id: int,
    merchant_id: str,
    statement_id: int,
    stripe_invoice_item_id: str,
    stripe_invoice_id: str,
    amount_cents: int,
    description: str,
) -> None:
    await database.execute(
        """
        INSERT INTO billing_run_items (
          billing_run_id,
          merchant_id,
          source_type,
          source_id,
          stripe_invoice_item_id,
          stripe_invoice_id,
          amount_cents,
          description
        ) VALUES (
          :billing_run_id,
          :merchant_id,
          'credit_overage',
          :statement_id,
          :stripe_invoice_item_id,
          :stripe_invoice_id,
          :amount_cents,
          :description
        )
        ON CONFLICT (billing_run_id, source_type, source_id, stripe_invoice_item_id) DO NOTHING
        """,
        {
            "billing_run_id": billing_run_id,
            "merchant_id": merchant_id,
            "statement_id": statement_id,
            "stripe_invoice_item_id": stripe_invoice_item_id,
            "stripe_invoice_id": stripe_invoice_id,
            "amount_cents": amount_cents,
            "description": description,
        },
    )


def _overage_line_description(
    *,
    overage_credits: int,
    overage_revenue_usd_cents: int,
) -> str:
    overage_revenue_usd = overage_revenue_usd_cents / 100
    return f"{overage_credits:,} credits overage — ${overage_revenue_usd:.2f}"


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _stripe_id(value: Any) -> str:
    return str(getattr(value, "id", None) or _row_get(value, "id") or "").strip()


def _row_get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except Exception:
        return getattr(row, key, None)


def _row_text(row: Any, key: str) -> str:
    value = _row_get(row, key)
    return str(value or "").strip()
