"""
Stripe Billing routes for monetization v1.3.

This module is intentionally separate from routes.webhook_routes, which handles
merchant commerce payment webhooks.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

import stripe
from databases import Database
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config.settings import settings
from db.database import IS_POSTGRES, database
from routes.payment_execution_routes import verify_merchant_api_key
from utils.logger import logger


router = APIRouter(tags=["billing"])
stripe_client = stripe.StripeClient(api_key=settings.stripe_secret_key or "")

_TABLE_COLUMN_CACHE: Dict[str, set[str]] = {}


class CheckoutSessionRequest(BaseModel):
    """Request body for creating a Stripe Billing Checkout Session."""

    price_id: str
    success_url: str
    cancel_url: str


async def require_approved_merchant(
    x_merchant_api_key: Optional[str] = Header(None, alias="X-Merchant-API-Key"),
) -> Dict[str, Any]:
    """Authenticate a merchant API key using the existing payment-route dependency."""

    return await verify_merchant_api_key(x_merchant_api_key or "")


@router.post("/webhooks/stripe/billing")
async def handle_stripe_billing_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None),
) -> JSONResponse:
    """Receive Stripe Billing webhooks and update monetization mirror state."""

    webhook_secret = str(settings.stripe_billing_webhook_secret or "").strip()
    if not webhook_secret:
        raise HTTPException(status_code=400, detail="Billing webhook secret not configured")

    payload = await request.body()
    try:
        event_obj = stripe.Webhook.construct_event(payload, stripe_signature, webhook_secret)
    except ValueError:
        logger.error("Invalid Stripe billing webhook payload")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except Exception as exc:
        logger.error("Invalid Stripe billing webhook signature: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid signature")

    event = _stripe_object_to_dict(event_obj)
    event_id = _as_text(event.get("id"))
    event_type = _as_text(event.get("type"))
    if not event_id or not event_type:
        raise HTTPException(status_code=400, detail="Invalid Stripe event")

    inserted = await _insert_stripe_event(event_id, event_type, event, database)
    if not inserted:
        return JSONResponse({"status": "duplicate"}, status_code=200)

    logger.info("Received Stripe billing webhook: %s event_id=%s", event_type, event_id)

    if event_type == "checkout.session.completed":
        await _handle_checkout_session_completed(event, database)
    elif event_type == "customer.subscription.updated":
        await _handle_subscription_updated(event, database)
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(event, database)
    elif event_type == "customer.subscription.created":
        await _mark_event_ignored(event_id, database)
        return JSONResponse({"status": "ignored"}, status_code=200)
    elif event_type == "invoice.paid":
        await _handle_invoice_paid(event, database)
    elif event_type == "invoice.payment_failed":
        await _handle_invoice_payment_failed(event, database)
    else:
        await _mark_event_ignored(event_id, database)
        return JSONResponse({"status": "ignored"}, status_code=200)

    return JSONResponse({"status": "processed"}, status_code=200)


@router.post("/api/billing/checkout-session")
async def create_billing_checkout_session(
    body: CheckoutSessionRequest,
    merchant: Dict[str, Any] = Depends(require_approved_merchant),
) -> Dict[str, str]:
    """Create a Stripe Checkout Session for a merchant subscription plan."""

    _require_platform_stripe_key()

    merchant_id = _as_text(merchant.get("merchant_id"))
    price_id = _as_text(body.price_id)
    success_url = _as_text(body.success_url)
    cancel_url = _as_text(body.cancel_url)
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Authenticated merchant is missing merchant_id")
    if not price_id or not success_url or not cancel_url:
        raise HTTPException(status_code=400, detail="price_id, success_url, and cancel_url are required")

    plan = await _lookup_subscription_plan(
        database,
        stripe_price_id=price_id,
        stripe_product_id=None,
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Subscription plan not found")

    contact_email = _as_text(merchant.get("contact_email"))
    billing_row = await _fetch_merchant_billing_row(
        database,
        merchant_id=merchant_id,
        contact_email=contact_email,
        stripe_customer_id=None,
    )
    if billing_row and _as_text(billing_row.get("contact_email")):
        contact_email = _as_text(billing_row.get("contact_email"))
    if not contact_email:
        raise HTTPException(status_code=400, detail="Merchant contact email is required")

    stripe_customer_id = _as_text((billing_row or {}).get("stripe_customer_id"))
    if not stripe_customer_id:
        customer = await asyncio.to_thread(
            stripe_client.v1.customers.create,
            {
                "email": contact_email,
                "metadata": {"merchant_id": merchant_id},
            },
            None,
        )
        stripe_customer_id = _as_text(getattr(customer, "id", None) or _stripe_object_to_dict(customer).get("id"))
        if not stripe_customer_id:
            raise HTTPException(status_code=502, detail="Stripe customer creation returned no customer id")

        updated = await _update_merchant_stripe_customer_id(
            database,
            merchant_id=merchant_id,
            contact_email=contact_email,
            stripe_customer_id=stripe_customer_id,
        )
        if not updated:
            logger.warning(
                "Created Stripe customer %s for merchant_id=%s but no merchants row was updated",
                stripe_customer_id,
                merchant_id,
            )

    metadata = {"merchant_id": merchant_id, "price_id": price_id}
    session = await asyncio.to_thread(
        stripe_client.v1.checkout.sessions.create,
        {
            "mode": "subscription",
            "customer": stripe_customer_id,
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": metadata,
        },
        None,
    )

    session_id = _as_text(getattr(session, "id", None) or _stripe_object_to_dict(session).get("id"))
    session_url = _as_text(getattr(session, "url", None) or _stripe_object_to_dict(session).get("url"))
    if not session_id or not session_url:
        raise HTTPException(status_code=502, detail="Stripe Checkout Session returned no URL")

    return {"session_url": session_url, "session_id": session_id}


async def _handle_checkout_session_completed(event: Dict[str, Any], db: Database) -> None:
    event_id = _event_id(event)
    try:
        session = _event_object(event)
        metadata = _coerce_dict(session.get("metadata"))
        stripe_subscription_id = _as_text(session.get("subscription"))
        stripe_customer_id = _as_text(session.get("customer"))
        merchant_id = _as_text(metadata.get("merchant_id"))
        price_id = _as_text(metadata.get("price_id"))
        product_id = _as_text(metadata.get("stripe_product_id") or metadata.get("product_id"))

        if not stripe_subscription_id:
            raise ValueError("checkout.session.completed missing subscription id")
        if not stripe_customer_id:
            raise ValueError("checkout.session.completed missing customer id")
        if not merchant_id:
            merchant_id = await _resolve_merchant_id_from_customer_id(db, stripe_customer_id)
        if not merchant_id:
            raise ValueError("checkout.session.completed missing merchant_id")

        plan = await _lookup_subscription_plan(
            db,
            stripe_price_id=price_id or None,
            stripe_product_id=product_id or None,
        )
        if not plan:
            raise ValueError("No active subscription plan found for checkout session")

        merchant_contact_email = await _lookup_merchant_contact_email(db, merchant_id)
        async with db.transaction():
            subscription_row = await db.fetch_one(
                """
                INSERT INTO user_subscriptions (
                  merchant_id,
                  plan_id,
                  stripe_subscription_id,
                  status,
                  started_at
                )
                VALUES (
                  :merchant_id,
                  :plan_id,
                  :stripe_subscription_id,
                  'active',
                  NOW()
                )
                ON CONFLICT (stripe_subscription_id) DO NOTHING
                RETURNING id
                """,
                {
                    "merchant_id": merchant_id,
                    "plan_id": plan["id"],
                    "stripe_subscription_id": stripe_subscription_id,
                },
            )
            if not subscription_row:
                subscription_row = await db.fetch_one(
                    """
                    SELECT id
                    FROM user_subscriptions
                    WHERE stripe_subscription_id = :stripe_subscription_id
                    LIMIT 1
                    """,
                    {"stripe_subscription_id": stripe_subscription_id},
                )
            if not subscription_row:
                raise ValueError("Unable to resolve local subscription row after insert")

            local_subscription_id = int(subscription_row["id"])
            await _update_merchant_subscription_after_checkout(
                db,
                merchant_id=merchant_id,
                contact_email=merchant_contact_email,
                local_subscription_id=local_subscription_id,
                tier_name=_as_text(plan["name"]),
                monthly_credit_allowance=int(plan["monthly_credit_allowance"] or 0),
            )

            await db.execute(
                """
                INSERT INTO credit_ledger (
                  merchant_id,
                  operation_type,
                  operation_id,
                  credits_delta,
                  balance_after,
                  metadata
                )
                VALUES (
                  :merchant_id,
                  'subscription_initial_grant',
                  :operation_id,
                  :credits_delta,
                  :balance_after,
                  CAST(:metadata AS jsonb)
                )
                """,
                {
                    "merchant_id": merchant_id,
                    "operation_id": stripe_subscription_id,
                    "credits_delta": int(plan["monthly_credit_allowance"] or 0),
                    "balance_after": int(plan["monthly_credit_allowance"] or 0),
                    "metadata": json.dumps(
                        {
                            "stripe_event_id": event_id,
                            "stripe_customer_id": stripe_customer_id,
                            "stripe_price_id": price_id,
                        }
                    ),
                },
            )

        await _mark_event_processed(event_id, db)
    except Exception as exc:
        await _handle_billing_handler_error(event_id, exc, db)


async def _handle_subscription_updated(event: Dict[str, Any], db: Database) -> None:
    event_id = _event_id(event)
    try:
        subscription = _event_object(event)
        stripe_subscription_id = _as_text(subscription.get("id"))
        if not stripe_subscription_id:
            raise ValueError("customer.subscription.updated missing subscription id")

        status_value = _normalize_subscription_status(subscription.get("status"))
        price_id, product_id = _extract_subscription_price_and_product(subscription)
        plan = await _lookup_subscription_plan(
            db,
            stripe_price_id=price_id,
            stripe_product_id=product_id,
        )

        existing = await db.fetch_one(
            """
            SELECT id, merchant_id, plan_id
            FROM user_subscriptions
            WHERE stripe_subscription_id = :stripe_subscription_id
            LIMIT 1
            """,
            {"stripe_subscription_id": stripe_subscription_id},
        )

        await db.execute(
            """
            UPDATE user_subscriptions
            SET
              status = :status,
              plan_id = COALESCE(CAST(:plan_id AS BIGINT), plan_id),
              current_period_start = :current_period_start,
              current_period_end = :current_period_end,
              cancel_at_period_end = :cancel_at_period_end,
              canceled_at = :canceled_at
            WHERE stripe_subscription_id = :stripe_subscription_id
            """,
            {
                "stripe_subscription_id": stripe_subscription_id,
                "status": status_value,
                "plan_id": int(plan["id"]) if plan else None,
                "current_period_start": _stripe_timestamp(subscription.get("current_period_start")),
                "current_period_end": _stripe_timestamp(subscription.get("current_period_end")),
                "cancel_at_period_end": bool(subscription.get("cancel_at_period_end") or False),
                "canceled_at": _stripe_timestamp(subscription.get("canceled_at")),
            },
        )

        if not existing:
            logger.warning(
                "Subscription update received before local subscription exists: %s",
                stripe_subscription_id,
            )
            await _mark_event_processed(event_id, db)
            return

        merchant_id = _as_text(existing["merchant_id"])
        contact_email = await _lookup_merchant_contact_email(db, merchant_id)
        if status_value in {"canceled", "unpaid"}:
            await _downgrade_merchant_to_free(db, merchant_id=merchant_id, contact_email=contact_email)
        elif plan and int(existing["plan_id"]) != int(plan["id"]):
            await _update_merchant_tier(
                db,
                merchant_id=merchant_id,
                contact_email=contact_email,
                tier_name=_as_text(plan["name"]),
            )

        await _mark_event_processed(event_id, db)
    except Exception as exc:
        await _handle_billing_handler_error(event_id, exc, db)


async def _handle_subscription_deleted(event: Dict[str, Any], db: Database) -> None:
    event_id = _event_id(event)
    try:
        subscription = _event_object(event)
        stripe_subscription_id = _as_text(subscription.get("id"))
        if not stripe_subscription_id:
            raise ValueError("customer.subscription.deleted missing subscription id")

        row = await db.fetch_one(
            """
            UPDATE user_subscriptions
            SET status = 'canceled',
                canceled_at = COALESCE(canceled_at, NOW())
            WHERE stripe_subscription_id = :stripe_subscription_id
            RETURNING merchant_id
            """,
            {"stripe_subscription_id": stripe_subscription_id},
        )
        if row:
            merchant_id = _as_text(row["merchant_id"])
            contact_email = await _lookup_merchant_contact_email(db, merchant_id)
            await _downgrade_merchant_to_free(db, merchant_id=merchant_id, contact_email=contact_email)
        else:
            logger.warning("Subscription delete received for unknown subscription: %s", stripe_subscription_id)

        await _mark_event_processed(event_id, db)
    except Exception as exc:
        await _handle_billing_handler_error(event_id, exc, db)


async def _handle_invoice_paid(event: Dict[str, Any], db: Database) -> None:
    event_id = _event_id(event)
    try:
        invoice = _event_object(event)
        stripe_invoice_id = _as_text(invoice.get("id"))
        if not stripe_invoice_id:
            raise ValueError("invoice.paid missing invoice id")

        merchant_id = await _resolve_invoice_merchant_id(db, invoice)
        invoice_values = _invoice_values(invoice, merchant_id=merchant_id, status_value="paid")
        updated = await db.fetch_one(
            """
            UPDATE invoices
            SET status = 'paid',
                paid_at = NOW(),
                total_cents = :total_cents,
                due_date = :due_date,
                finalized_at = COALESCE(finalized_at, :finalized_at)
            WHERE stripe_invoice_id = :stripe_invoice_id
            RETURNING id
            """,
            invoice_values,
        )
        if not updated:
            await _insert_minimal_invoice(db, invoice_values)

        await _mark_event_processed(event_id, db)
    except Exception as exc:
        await _handle_billing_handler_error(event_id, exc, db)


async def _handle_invoice_payment_failed(event: Dict[str, Any], db: Database) -> None:
    event_id = _event_id(event)
    try:
        invoice = _event_object(event)
        stripe_invoice_id = _as_text(invoice.get("id"))
        if not stripe_invoice_id:
            raise ValueError("invoice.payment_failed missing invoice id")

        merchant_id = await _resolve_invoice_merchant_id(db, invoice)
        invoice_values = _invoice_values(invoice, merchant_id=merchant_id, status_value="payment_failed")
        updated = await db.fetch_one(
            """
            UPDATE invoices
            SET status = 'payment_failed',
                total_cents = :total_cents,
                due_date = :due_date,
                finalized_at = COALESCE(finalized_at, :finalized_at)
            WHERE stripe_invoice_id = :stripe_invoice_id
            RETURNING id
            """,
            invoice_values,
        )
        if not updated:
            await _insert_minimal_invoice(db, invoice_values)

        logger.warning(
            "Stripe invoice payment failed merchant_id=%s stripe_invoice_id=%s amount_cents=%s",
            merchant_id,
            stripe_invoice_id,
            invoice_values["total_cents"],
        )
        await _mark_event_processed(event_id, db)
    except Exception as exc:
        await _handle_billing_handler_error(event_id, exc, db)


async def _insert_stripe_event(
    event_id: str,
    event_type: str,
    event: Dict[str, Any],
    db: Database,
) -> bool:
    payload = json.dumps(event, default=str)
    if IS_POSTGRES:
        row = await db.fetch_one(
            """
            INSERT INTO stripe_events (
              event_id,
              event_type,
              payload_jsonb,
              received_at,
              status
            )
            VALUES (
              :event_id,
              :event_type,
              CAST(:payload_json AS jsonb),
              NOW(),
              'pending'
            )
            ON CONFLICT (event_id) DO NOTHING
            RETURNING id
            """,
            {
                "event_id": event_id,
                "event_type": event_type,
                "payload_json": payload,
            },
        )
    else:
        row = await db.fetch_one(
            """
            INSERT INTO stripe_events (
              event_id,
              event_type,
              payload_jsonb,
              received_at,
              status
            )
            VALUES (
              :event_id,
              :event_type,
              :payload_json,
              CURRENT_TIMESTAMP,
              'pending'
            )
            ON CONFLICT (event_id) DO NOTHING
            RETURNING id
            """,
            {
                "event_id": event_id,
                "event_type": event_type,
                "payload_json": payload,
            },
        )
    return row is not None


async def _mark_event_processed(event_id: str, db: Database) -> None:
    await db.execute(
        """
        UPDATE stripe_events
        SET status = 'processed',
            processed_at = NOW(),
            error = NULL
        WHERE event_id = :event_id
        """,
        {"event_id": event_id},
    )


async def _mark_event_ignored(event_id: str, db: Database) -> None:
    await db.execute(
        """
        UPDATE stripe_events
        SET status = 'ignored',
            processed_at = NOW(),
            error = NULL
        WHERE event_id = :event_id
        """,
        {"event_id": event_id},
    )


async def _mark_event_failed(event_id: str, db: Database, error: str) -> None:
    await db.execute(
        """
        UPDATE stripe_events
        SET status = 'failed',
            error = :error
        WHERE event_id = :event_id
        """,
        {"event_id": event_id, "error": error[:2000]},
    )


async def _handle_billing_handler_error(event_id: str, exc: Exception, db: Database) -> None:
    await _mark_event_failed(event_id, db, str(exc))
    logger.exception("Stripe billing webhook handler failed event_id=%s", event_id)
    raise HTTPException(status_code=500, detail="Stripe billing webhook handler failed")


async def _lookup_subscription_plan(
    db: Database,
    *,
    stripe_price_id: Optional[str],
    stripe_product_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    if stripe_price_id:
        row = await db.fetch_one(
            """
            SELECT id, name, stripe_price_id, monthly_credit_allowance
            FROM subscription_plans
            WHERE stripe_price_id = :stripe_price_id
              AND status = 'active'
            LIMIT 1
            """,
            {"stripe_price_id": stripe_price_id},
        )
        if row:
            return dict(row)

    if stripe_product_id and await _table_has_column(db, "subscription_plans", "stripe_product_id"):
        row = await db.fetch_one(
            """
            SELECT id, name, stripe_price_id, monthly_credit_allowance
            FROM subscription_plans
            WHERE stripe_product_id = :stripe_product_id
              AND status = 'active'
            LIMIT 1
            """,
            {"stripe_product_id": stripe_product_id},
        )
        if row:
            return dict(row)

    return None


async def _update_merchant_subscription_after_checkout(
    db: Database,
    *,
    merchant_id: str,
    contact_email: Optional[str],
    local_subscription_id: int,
    tier_name: str,
    monthly_credit_allowance: int,
) -> None:
    where_clause, params = await _merchant_where_clause(
        db,
        merchant_id=merchant_id,
        contact_email=contact_email,
        stripe_customer_id=None,
    )
    if not where_clause:
        raise ValueError("Unable to build merchants lookup for subscription fulfillment")

    row = await db.fetch_one(
        f"""
        UPDATE merchants
        SET subscription_id = :subscription_id,
            current_tier = :current_tier,
            credits_balance = :credits_balance,
            promo_period_until = NOW() + INTERVAL '6 months',
            billing_anchor_day = 1
        WHERE {where_clause}
        RETURNING id
        """,
        {
            **params,
            "subscription_id": local_subscription_id,
            "current_tier": tier_name,
            "credits_balance": monthly_credit_allowance,
        },
    )
    if not row:
        raise ValueError(f"No merchants row found for merchant_id={merchant_id}")


async def _update_merchant_tier(
    db: Database,
    *,
    merchant_id: str,
    contact_email: Optional[str],
    tier_name: str,
) -> None:
    where_clause, params = await _merchant_where_clause(
        db,
        merchant_id=merchant_id,
        contact_email=contact_email,
        stripe_customer_id=None,
    )
    if not where_clause:
        return

    await db.execute(
        f"""
        UPDATE merchants
        SET current_tier = :current_tier
        WHERE {where_clause}
        """,
        {**params, "current_tier": tier_name},
    )


async def _downgrade_merchant_to_free(
    db: Database,
    *,
    merchant_id: str,
    contact_email: Optional[str],
) -> None:
    where_clause, params = await _merchant_where_clause(
        db,
        merchant_id=merchant_id,
        contact_email=contact_email,
        stripe_customer_id=None,
    )
    if not where_clause:
        return

    await db.execute(
        f"""
        UPDATE merchants
        SET current_tier = 'free',
            subscription_id = NULL
        WHERE {where_clause}
        """,
        params,
    )


async def _fetch_merchant_billing_row(
    db: Database,
    *,
    merchant_id: Optional[str],
    contact_email: Optional[str],
    stripe_customer_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    columns = await _table_columns(db, "merchants")
    select_columns = _select_columns(
        columns,
        ("id", "merchant_id", "contact_email", "stripe_customer_id", "subscription_id", "current_tier"),
    )
    if not select_columns:
        return None

    where_clause, params = await _merchant_where_clause(
        db,
        merchant_id=merchant_id,
        contact_email=contact_email,
        stripe_customer_id=stripe_customer_id,
    )
    if not where_clause:
        return None

    row = await db.fetch_one(
        f"""
        SELECT {", ".join(select_columns)}
        FROM merchants
        WHERE {where_clause}
        LIMIT 1
        """,
        params,
    )
    return dict(row) if row else None


async def _update_merchant_stripe_customer_id(
    db: Database,
    *,
    merchant_id: str,
    contact_email: Optional[str],
    stripe_customer_id: str,
) -> bool:
    where_clause, params = await _merchant_where_clause(
        db,
        merchant_id=merchant_id,
        contact_email=contact_email,
        stripe_customer_id=None,
    )
    if not where_clause:
        return False

    row = await db.fetch_one(
        f"""
        UPDATE merchants
        SET stripe_customer_id = :stripe_customer_id
        WHERE {where_clause}
        RETURNING id
        """,
        {**params, "stripe_customer_id": stripe_customer_id},
    )
    return row is not None


async def _merchant_where_clause(
    db: Database,
    *,
    merchant_id: Optional[str],
    contact_email: Optional[str],
    stripe_customer_id: Optional[str],
) -> Tuple[str, Dict[str, Any]]:
    columns = await _table_columns(db, "merchants")
    clauses = []
    params: Dict[str, Any] = {}

    if stripe_customer_id and "stripe_customer_id" in columns:
        clauses.append("stripe_customer_id = :stripe_customer_id")
        params["stripe_customer_id"] = stripe_customer_id
    if merchant_id and "merchant_id" in columns:
        clauses.append("merchant_id = :merchant_id")
        params["merchant_id"] = merchant_id
    if contact_email and "contact_email" in columns:
        clauses.append("LOWER(contact_email) = LOWER(:contact_email)")
        params["contact_email"] = contact_email
    if merchant_id and merchant_id.isdigit() and "id" in columns:
        clauses.append("id = :merchant_pk_id")
        params["merchant_pk_id"] = int(merchant_id)

    return " OR ".join(f"({clause})" for clause in clauses), params


async def _resolve_merchant_id_from_customer_id(db: Database, stripe_customer_id: str) -> str:
    columns = await _table_columns(db, "merchants")
    if "stripe_customer_id" not in columns:
        return ""

    if "merchant_id" in columns:
        row = await db.fetch_one(
            """
            SELECT merchant_id
            FROM merchants
            WHERE stripe_customer_id = :stripe_customer_id
            LIMIT 1
            """,
            {"stripe_customer_id": stripe_customer_id},
        )
        if row:
            return _as_text(row["merchant_id"])

    if "contact_email" in columns:
        row = await db.fetch_one(
            """
            SELECT mo.merchant_id
            FROM merchants m
            JOIN merchant_onboarding mo
              ON LOWER(m.contact_email) = LOWER(mo.contact_email)
            WHERE m.stripe_customer_id = :stripe_customer_id
            LIMIT 1
            """,
            {"stripe_customer_id": stripe_customer_id},
        )
        if row:
            return _as_text(row["merchant_id"])

    return ""


async def _lookup_merchant_contact_email(db: Database, merchant_id: str) -> Optional[str]:
    row = await db.fetch_one(
        """
        SELECT contact_email
        FROM merchant_onboarding
        WHERE merchant_id = :merchant_id
        LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )
    if row and _as_text(row["contact_email"]):
        return _as_text(row["contact_email"])

    billing_row = await _fetch_merchant_billing_row(
        db,
        merchant_id=merchant_id,
        contact_email=None,
        stripe_customer_id=None,
    )
    return _as_text((billing_row or {}).get("contact_email")) or None


async def _resolve_invoice_merchant_id(db: Database, invoice: Dict[str, Any]) -> str:
    metadata = _coerce_dict(invoice.get("metadata"))
    merchant_id = _as_text(metadata.get("merchant_id"))
    if merchant_id:
        return merchant_id

    subscription_details = _coerce_dict(invoice.get("subscription_details"))
    subscription_metadata = _coerce_dict(subscription_details.get("metadata"))
    merchant_id = _as_text(subscription_metadata.get("merchant_id"))
    if merchant_id:
        return merchant_id

    stripe_subscription_id = _as_text(
        invoice.get("subscription")
        or subscription_details.get("subscription")
        or _coerce_dict(invoice.get("parent")).get("subscription")
    )
    if stripe_subscription_id:
        row = await db.fetch_one(
            """
            SELECT merchant_id
            FROM user_subscriptions
            WHERE stripe_subscription_id = :stripe_subscription_id
            LIMIT 1
            """,
            {"stripe_subscription_id": stripe_subscription_id},
        )
        if row:
            return _as_text(row["merchant_id"])

    stripe_customer_id = _as_text(invoice.get("customer"))
    if stripe_customer_id:
        merchant_id = await _resolve_merchant_id_from_customer_id(db, stripe_customer_id)
        if merchant_id:
            return merchant_id

    raise ValueError("Unable to resolve merchant_id for Stripe invoice")


async def _insert_minimal_invoice(db: Database, values: Dict[str, Any]) -> None:
    await db.execute(
        """
        INSERT INTO invoices (
          merchant_id,
          billing_period_start,
          billing_period_end,
          stripe_invoice_id,
          total_cents,
          status,
          due_date,
          finalized_at,
          paid_at
        )
        VALUES (
          :merchant_id,
          :billing_period_start,
          :billing_period_end,
          :stripe_invoice_id,
          :total_cents,
          :status,
          :due_date,
          :finalized_at,
          CASE WHEN :status = 'paid' THEN NOW() ELSE NULL END
        )
        ON CONFLICT (stripe_invoice_id) DO UPDATE
        SET status = EXCLUDED.status,
            total_cents = EXCLUDED.total_cents,
            due_date = EXCLUDED.due_date,
            finalized_at = COALESCE(invoices.finalized_at, EXCLUDED.finalized_at),
            paid_at = COALESCE(invoices.paid_at, EXCLUDED.paid_at)
        """,
        values,
    )


async def _table_has_column(db: Database, table_name: str, column_name: str) -> bool:
    return column_name in await _table_columns(db, table_name)


async def _table_columns(db: Database, table_name: str) -> set[str]:
    if table_name in _TABLE_COLUMN_CACHE:
        return _TABLE_COLUMN_CACHE[table_name]

    if table_name not in {"merchants", "subscription_plans"}:
        raise ValueError(f"Unsupported table introspection target: {table_name}")

    if IS_POSTGRES:
        rows = await db.fetch_all(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table_name
            """,
            {"table_name": table_name},
        )
        columns = {_as_text(row["column_name"]) for row in rows}
    else:
        rows = await db.fetch_all(f"PRAGMA table_info({table_name})")
        columns = {_as_text(row["name"]) for row in rows}

    _TABLE_COLUMN_CACHE[table_name] = {column for column in columns if column}
    return _TABLE_COLUMN_CACHE[table_name]


def _event_id(event: Dict[str, Any]) -> str:
    return _as_text(event.get("id"))


def _event_object(event: Dict[str, Any]) -> Dict[str, Any]:
    return _coerce_dict(_coerce_dict(event.get("data")).get("object"))


def _stripe_object_to_dict(value: Any) -> Any:
    if hasattr(value, "to_dict_recursive"):
        return value.to_dict_recursive()
    if isinstance(value, dict):
        return {str(k): _stripe_object_to_dict(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stripe_object_to_dict(item) for item in value]
    return value


def _coerce_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict_recursive"):
        data = value.to_dict_recursive()
        return dict(data) if isinstance(data, dict) else {}
    return {}


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _select_columns(columns: Iterable[str], candidates: Iterable[str]) -> list[str]:
    return [column for column in candidates if column in columns]


def _normalize_subscription_status(value: Any) -> str:
    status_value = _as_text(value).lower()
    allowed = {
        "incomplete",
        "incomplete_expired",
        "trialing",
        "active",
        "past_due",
        "canceled",
        "unpaid",
        "paused",
    }
    return status_value if status_value in allowed else "incomplete"


def _extract_subscription_price_and_product(subscription: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    items = _coerce_dict(subscription.get("items"))
    data = items.get("data") if isinstance(items.get("data"), list) else []
    if not data:
        return None, None

    first_item = _coerce_dict(data[0])
    price = _coerce_dict(first_item.get("price"))
    return _as_text(price.get("id")) or None, _as_text(price.get("product")) or None


def _stripe_timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except Exception:
        return None


def _invoice_values(
    invoice: Dict[str, Any],
    *,
    merchant_id: str,
    status_value: str,
) -> Dict[str, Any]:
    period_start = _stripe_timestamp(invoice.get("period_start"))
    period_end = _stripe_timestamp(invoice.get("period_end"))
    lines = _coerce_dict(invoice.get("lines"))
    line_data = lines.get("data") if isinstance(lines.get("data"), list) else []
    if line_data:
        first_period = _coerce_dict(_coerce_dict(line_data[0]).get("period"))
        period_start = period_start or _stripe_timestamp(first_period.get("start"))
        period_end = period_end or _stripe_timestamp(first_period.get("end"))

    now = datetime.now(timezone.utc)
    period_start = period_start or _stripe_timestamp(invoice.get("created")) or now
    period_end = period_end or period_start + timedelta(seconds=1)
    if period_end <= period_start:
        period_end = period_start + timedelta(seconds=1)

    status_transitions = _coerce_dict(invoice.get("status_transitions"))
    return {
        "merchant_id": merchant_id,
        "billing_period_start": period_start,
        "billing_period_end": period_end,
        "stripe_invoice_id": _as_text(invoice.get("id")),
        "total_cents": _invoice_amount_cents(invoice),
        "status": status_value,
        "due_date": _stripe_timestamp(invoice.get("due_date")),
        "finalized_at": _stripe_timestamp(status_transitions.get("finalized_at")),
    }


def _invoice_amount_cents(invoice: Dict[str, Any]) -> int:
    for key in ("amount_paid", "amount_due", "total"):
        raw = invoice.get(key)
        if raw is not None:
            try:
                return max(0, int(raw))
            except Exception:
                continue
    return 0


def _require_platform_stripe_key() -> None:
    if not str(settings.stripe_secret_key or "").strip():
        raise HTTPException(status_code=500, detail="Stripe secret key not configured")
