"""
Stripe Billing routes for monetization v1.3.

This module is intentionally separate from routes.webhook_routes, which handles
merchant commerce payment webhooks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

import stripe
from databases import Database
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from config.settings import settings
from db.database import IS_POSTGRES, database
from db.merchant_onboarding import get_merchant_onboarding
from routes.payment_execution_routes import verify_merchant_api_key
from services.activation_event_service import activate_brands_for_merchant
from services.billing import monthly_brand_statements_service
from services.merchant_store_service import (
    get_merchant_active_stores,
    parse_api_credentials,
)
from utils.auth import decode_token, get_current_merchant, optional_security
from utils.logger import logger


router = APIRouter(tags=["billing"])
stripe_client = stripe.StripeClient(api_key=settings.stripe_secret_key or "")

_TABLE_COLUMN_CACHE: Dict[str, set[str]] = {}


class CheckoutSessionRequest(BaseModel):
    """Request body for creating a Stripe Billing Checkout Session."""

    price_id: str
    success_url: str
    cancel_url: str


class PortalSessionRequest(BaseModel):
    """Request body for creating a Stripe Billing Portal Session.

    ``return_url`` is where Stripe sends the merchant back after they finish
    managing their subscription (cancel plan, update/remove card, view invoices).
    """

    return_url: str


class CreditTopUpRequest(BaseModel):
    """Manual direct/self-serve credit top-up request."""

    merchant_id: str
    pack_credits: int
    idempotency_key: Optional[str] = None


async def require_approved_merchant(
    x_merchant_api_key: Optional[str] = Header(None, alias="X-Merchant-API-Key"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
) -> Dict[str, Any]:
    """Authenticate a merchant for self-serve billing.

    Accepts EITHER:
      - ``X-Merchant-API-Key`` — the merchant payment-execution key. Takes
        precedence when present, preserving any server-to-server callers.
      - ``Authorization: Bearer <jwt>`` — a merchant-role portal session JWT.

    The merchant portal browser only ever holds the JWT (the API key is a
    high-privilege secret that must never reach the client), so the self-serve
    billing pages ride the JWT path — identical to commission/payouts. Both
    paths enforce the same approval gate and return a dict carrying
    ``merchant_id``, ``status`` ("approved"), and ``contact_email``.
    """

    if x_merchant_api_key:
        # An explicit API key means API-key auth was intended; a bad key fails
        # here rather than silently falling back to JWT.
        return await verify_merchant_api_key(x_merchant_api_key)

    if credentials and credentials.credentials:
        payload = decode_token(credentials.credentials)
        if payload.get("role") != "merchant":
            raise HTTPException(status_code=403, detail="Merchant access required")
        merchant_id = payload.get("merchant_id")
        if not merchant_id:
            raise HTTPException(
                status_code=401,
                detail=(
                    "Merchant ID missing from token. Log out and log back in "
                    "to refresh your session."
                ),
            )
        # Enforce the same approval gate as the API-key path: only approved
        # merchants may view billing or open a checkout session. Without this,
        # a pending/rejected/suspended merchant's portal JWT would regress the
        # gate that verify_merchant_api_key applies.
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        merchant_status = str(merchant.get("status") or "").lower()
        if merchant_status != "approved":
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Merchant account is {merchant_status or 'unverified'}. "
                    "Only approved merchants can manage billing."
                ),
            )
        return {
            "merchant_id": merchant_id,
            "status": "approved",
            "contact_email": merchant.get("contact_email"),
        }

    raise HTTPException(
        status_code=401,
        detail="Missing credentials. Provide an X-Merchant-API-Key header or a Bearer token.",
    )


def _billing_exempt_merchant_ids() -> set[str]:
    """Merchant ids explicitly exempt from off-platform (Stripe) billing.

    Used for the Shopify App Store review account and any merchant we must not
    charge off-platform. Set ``BILLING_EXEMPT_MERCHANT_IDS`` to a comma-separated
    list of merchant ids.
    """
    raw = (os.getenv("BILLING_EXEMPT_MERCHANT_IDS") or "").strip()
    return {x.strip() for x in raw.split(",") if x.strip()}


async def _merchant_is_billing_free(merchant_id: str) -> bool:
    """True when a merchant must never see / use off-platform (Stripe) billing.

    Shopify App Store distribution must bill only through Shopify, and App A is
    free, so ``app_store``-origin installs and explicitly-exempted merchants get
    no paid plans and cannot open a Stripe checkout session. The headless / BYO
    tier (custom-token connects) keeps Stripe billing unchanged.
    """
    if not merchant_id:
        return False
    if merchant_id in _billing_exempt_merchant_ids():
        return True
    try:
        stores = await get_merchant_active_stores(merchant_id)
    except Exception:
        stores = []
    # Exempt when ANY connected store was installed via the App Store, not just the
    # "primary" one — a merchant who already had a store and then installs App A
    # must still be billed only through Shopify.
    #
    # NOTE: get_merchant_active_stores() rewrites each row's `api_key` to the bare
    # access token and keeps the original JSON blob (which is where install_source
    # lives) in `api_key_raw`. Parse the raw blob, not the token, or the source is
    # always lost and no App Store merchant is ever exempt.
    for store in stores or []:
        raw = store.get("api_key_raw")
        if raw is None:
            raw = store.get("api_key")
        creds = parse_api_credentials(str(raw or ""))
        if isinstance(creds, dict) and str(creds.get("install_source") or "").strip().lower() == "app_store":
            return True
    return False


@router.get("/api/billing/me/current-period")
async def get_my_current_billing_period(
    merchant: Dict[str, Any] = Depends(require_approved_merchant),
) -> Dict[str, Any]:
    """Merchant-safe current-period credit usage snapshot."""

    merchant_id = _as_text(merchant.get("merchant_id"))
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Authenticated merchant is missing merchant_id")
    return await monthly_brand_statements_service.current_period_usage_snapshot(merchant_id)


@router.get("/api/billing/me/statements")
async def list_my_billing_statements(
    limit: int = Query(12, ge=1),
    merchant: Dict[str, Any] = Depends(require_approved_merchant),
) -> Dict[str, Any]:
    """Merchant-safe frozen/invoiced monthly statement history."""

    merchant_id = _as_text(merchant.get("merchant_id"))
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Authenticated merchant is missing merchant_id")
    capped_limit = min(int(limit), 36)
    statements = await monthly_brand_statements_service.list_merchant_statement_rows(
        merchant_id=merchant_id,
        limit=capped_limit,
    )
    return {"statements": statements}


@router.get("/api/billing/plans")
async def list_billing_plans(
    merchant: Dict[str, Any] = Depends(require_approved_merchant),
) -> Dict[str, Any]:
    """Merchant-safe catalogue of active, checkout-able subscription plans.

    Scoped to the platform's current Stripe mode (live/test), so the portal
    never needs to know or ship price_ids — the upgrade UI renders whatever
    this returns and posts the chosen price_id straight back to checkout.
    """

    # App Store (free) and exempt merchants must never be offered off-platform
    # billing — return no plans and a flag so the portal hides the entire
    # billing UI (no Stripe references anywhere) for them.
    merchant_id = _as_text(merchant.get("merchant_id"))
    if await _merchant_is_billing_free(merchant_id):
        return {"plans": [], "off_platform_billing": False}

    plans = await _list_active_paid_plans(database, mode=_platform_stripe_mode())
    return {"plans": plans, "off_platform_billing": True}


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

    # Stripe retries deliver the same event_id on transient failures. Cases:
    #   - status='processed'/'ignored' → genuine duplicate; ack and exit.
    #   - status='failed' → previous handler errored; reprocess this retry.
    #   - status='pending' stale (> STALE_PENDING_SECONDS) → previous handler
    #     crashed before writing terminal state. Reclaim same as 'failed'.
    #   - status='pending' fresh → another worker is currently processing this
    #     event; respond 409 so Stripe retries later.
    inserted = await _insert_stripe_event(event_id, event_type, event, database)
    if not inserted:
        claim = await _claim_retryable_event(event_id, event_type, event, database)
        if claim == "ack_duplicate":
            return JSONResponse({"status": "duplicate"}, status_code=200)
        if claim == "concurrent_in_flight":
            return JSONResponse({"status": "in_progress"}, status_code=409)
        logger.info(
            "Reclaiming retryable stripe event event_id=%s type=%s prior_status=%s",
            event_id,
            event_type,
            claim,
        )

    logger.info("Received Stripe billing webhook: %s event_id=%s", event_type, event_id)

    # Livemode gate: in production, refuse test-mode events. A test-mode endpoint
    # secret that happens to verify (or a test-clock / smoke-test harness) must
    # not be able to mutate live billing state. `livemode` is part of the signed
    # event. Mirrors the order/PSP webhook guard in webhook_routes.py.
    is_prod_env = (
        os.getenv("ENVIRONMENT", "").lower() == "production"
        or os.getenv("RAILWAY_ENVIRONMENT", "").lower() == "production"
    )
    if is_prod_env and event.get("livemode") is False:
        logger.warning(
            "Ignoring test-mode Stripe billing webhook (livemode=false) in production: type=%s event_id=%s",
            event_type,
            event_id,
        )
        await _mark_event_ignored(event_id, database)
        return JSONResponse(
            {"status": "ignored", "reason": "test_mode_event_in_production"},
            status_code=200,
        )

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
    elif event_type == "payment_intent.succeeded":
        payment_intent_result = await _handle_payment_intent_succeeded(event, database)
        if payment_intent_result == "ignored":
            return JSONResponse({"status": "ignored"}, status_code=200)
    else:
        await _mark_event_ignored(event_id, database)
        return JSONResponse({"status": "ignored"}, status_code=200)

    return JSONResponse({"status": "processed"}, status_code=200)


def _is_missing_customer_error(exc: Exception) -> bool:
    """True when a Stripe error indicates the customer no longer exists."""

    if getattr(exc, "code", None) == "resource_missing":
        return True
    return "no such customer" in str(getattr(exc, "user_message", None) or exc).lower()


def _is_unique_violation(exc: Exception) -> bool:
    """True when ``exc`` is a Postgres unique-violation (SQLSTATE 23505).

    The databases/asyncpg stack surfaces the driver error; match on its
    ``sqlstate``/``pgcode`` (or class name as a fallback) without importing the
    driver here.
    """
    code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if code == "23505":
        return True
    return "uniqueviolation" in type(exc).__name__.lower()


async def _ensure_merchant_billing_row(
    db: Database, *, merchant_id: str, contact_email: str
) -> None:
    """Ensure a ``merchants`` (billing account) row exists for this merchant.

    Portal signup + approval only writes ``merchant_onboarding``; no code path
    promotes an approved merchant into the ``merchants`` monetization table. The
    checkout flow assumes that row exists and ``UPDATE``s it, so a first-time
    subscriber otherwise 500s ("local merchants row missing") and orphans a
    Stripe customer. Provision the row here from the approved onboarding record.

    Idempotent and concurrency-safe. Two guards cover a double-submit race:
    ``WHERE NOT EXISTS`` skips the insert when a row already exists, and — if two
    requests both pass that check before either commits — the
    ``uq_merchants_lower_contact_email`` unique index (migration 180) makes the
    losing insert raise a unique violation, which we swallow (the row we wanted
    now exists; the caller re-reads it). The ``merchants`` table has no
    ``merchant_id`` column — it is keyed by ``contact_email``, which is also how
    the Stripe webhook maps a completed checkout back to the merchant.
    """
    if not contact_email:
        return
    columns = await _table_columns(db, "merchants")
    if "contact_email" not in columns:
        return

    existing = await _fetch_merchant_billing_row(
        db, merchant_id=merchant_id, contact_email=contact_email, stripe_customer_id=None
    )
    if existing:
        return

    onboarding = await get_merchant_onboarding(merchant_id) or {}
    business_name = _as_text(onboarding.get("business_name")) or contact_email
    store_url = _as_text(onboarding.get("store_url")) or None

    try:
        await db.execute(
            """
            INSERT INTO merchants (
                business_name, legal_name, platform, store_url,
                contact_email, status, verification_status,
                current_tier, created_at, updated_at
            )
            SELECT
                :business_name, :business_name, 'custom', :store_url,
                :contact_email, 'active', 'verified',
                'free', NOW(), NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM merchants WHERE LOWER(contact_email) = LOWER(:contact_email)
            )
            """,
            {
                "business_name": business_name,
                "store_url": store_url,
                "contact_email": contact_email,
            },
        )
    except Exception as exc:  # noqa: BLE001 — narrowed via _is_unique_violation
        # A concurrent checkout (double-submit) can win the race between the
        # WHERE NOT EXISTS check above and this INSERT; with the unique index the
        # losing insert raises 23505. The row now exists, so provisioning is
        # satisfied — the caller re-reads billing_row. Re-raise anything else.
        if not _is_unique_violation(exc):
            raise
        logger.info(
            "merchants billing row for contact_email=%s created concurrently; "
            "treating provisioning as satisfied.",
            contact_email,
        )
        return
    logger.info(
        "Provisioned merchants billing row for merchant_id=%s from approved "
        "onboarding record (contact_email=%s).",
        merchant_id,
        contact_email,
    )


async def _create_and_persist_stripe_customer(
    *, merchant_id: str, contact_email: str, idempotency_key: str
) -> str:
    """Create a Stripe customer and persist its id on the merchants row.

    Shared by first-time checkout and the recreate-on-invalid-customer recovery
    path. Keeps the orphan-customer guard: if no merchants row matches the
    persist, fail loud rather than leave a Stripe customer our DB can't map.
    """

    customer = await asyncio.to_thread(
        stripe_client.v1.customers.create,
        {"email": contact_email, "metadata": {"merchant_id": merchant_id}},
        {"idempotency_key": idempotency_key},
    )
    stripe_customer_id = _as_text(
        getattr(customer, "id", None) or _stripe_object_to_dict(customer).get("id")
    )
    if not stripe_customer_id:
        raise HTTPException(status_code=502, detail="Stripe customer creation returned no customer id")

    updated = await _update_merchant_stripe_customer_id(
        database,
        merchant_id=merchant_id,
        contact_email=contact_email,
        stripe_customer_id=stripe_customer_id,
    )
    if not updated:
        logger.error(
            "Orphan Stripe customer: created %s for merchant_id=%s but no "
            "merchants row matched the UPDATE. Provision the merchants + "
            "user_subscriptions row, then retry.",
            stripe_customer_id,
            merchant_id,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Merchant subscription provisioning incomplete: Stripe customer "
                "created but local merchants row missing. Retry after provisioning completes."
            ),
        )
    return stripe_customer_id


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
    # Block the off-platform charge path for App Store / exempt merchants, even if
    # the UI is bypassed — Shopify App Store apps must not bill off-platform.
    if await _merchant_is_billing_free(merchant_id):
        raise HTTPException(status_code=403, detail="This merchant is not eligible for off-platform billing.")
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

    if not billing_row:
        # Portal signup + approval writes only `merchant_onboarding`; nothing
        # promotes an approved merchant into the `merchants` monetization table,
        # so a first-time subscriber would otherwise 500 in
        # `_create_and_persist_stripe_customer` (its UPDATE matches zero rows).
        # Provision the billing row now, idempotently, from the approved
        # onboarding record, then re-read it.
        await _ensure_merchant_billing_row(
            database, merchant_id=merchant_id, contact_email=contact_email
        )
        billing_row = await _fetch_merchant_billing_row(
            database,
            merchant_id=merchant_id,
            contact_email=contact_email,
            stripe_customer_id=None,
        )

    stripe_customer_id = _as_text((billing_row or {}).get("stripe_customer_id"))
    if not stripe_customer_id:
        # Deterministic idempotency: at most one Stripe customer per merchant
        # in the typical 24h signup/retry window.
        stripe_customer_id = await _create_and_persist_stripe_customer(
            merchant_id=merchant_id,
            contact_email=contact_email,
            idempotency_key=f"merchant_customer:{merchant_id}",
        )

    metadata = {"merchant_id": merchant_id, "price_id": price_id}
    idempotency_bucket = datetime.now(timezone.utc).date().isoformat()

    def _session_payload(customer_id: str) -> Dict[str, Any]:
        return {
            "mode": "subscription",
            "customer": customer_id,
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": metadata,
            # Show the "Add promotion code" field on hosted Checkout. Additive:
            # nothing is discounted unless the buyer enters a valid code.
            "allow_promotion_codes": True,
        }

    # Derive the idempotency key from the exact payload. Identical "subscribe"
    # requests dedupe to one session; ANY difference (success/cancel_url,
    # customer after a recreate, price) yields a distinct key — so we never hit
    # Stripe's "keys for idempotent requests can only be used with the same
    # parameters" error (IdempotencyError) that a coarse per-day key caused.
    def _session_idempotency_key(payload: Dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, default=str)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:40]
        return f"checkout_session:{merchant_id}:{digest}"

    payload = _session_payload(stripe_customer_id)
    try:
        session = await asyncio.to_thread(
            stripe_client.v1.checkout.sessions.create,
            payload,
            {"idempotency_key": _session_idempotency_key(payload)},
        )
    except Exception as exc:
        # The stored Stripe customer can be invalid for the current account —
        # created under a different key before a Stripe key rotation, or deleted.
        # Recreate it (fresh idempotency keys so Stripe doesn't replay the stale
        # objects) and retry the session once. The recreate persists the new id,
        # so this self-heals: subsequent checkouts take the normal path.
        if type(exc).__name__ != "InvalidRequestError" or not _is_missing_customer_error(exc):
            raise
        logger.warning(
            "Stored Stripe customer %s invalid for merchant_id=%s (%s); "
            "recreating and retrying checkout.",
            stripe_customer_id,
            merchant_id,
            getattr(exc, "code", None),
        )
        stripe_customer_id = await _create_and_persist_stripe_customer(
            merchant_id=merchant_id,
            contact_email=contact_email,
            idempotency_key=f"merchant_customer:{merchant_id}:{idempotency_bucket}",
        )
        payload = _session_payload(stripe_customer_id)
        session = await asyncio.to_thread(
            stripe_client.v1.checkout.sessions.create,
            payload,
            {"idempotency_key": _session_idempotency_key(payload)},
        )

    session_id = _as_text(getattr(session, "id", None) or _stripe_object_to_dict(session).get("id"))
    session_url = _as_text(getattr(session, "url", None) or _stripe_object_to_dict(session).get("url"))
    if not session_id or not session_url:
        raise HTTPException(status_code=502, detail="Stripe Checkout Session returned no URL")

    return {"session_url": session_url, "session_id": session_id}


@router.post("/api/billing/portal-session")
async def create_billing_portal_session(
    body: PortalSessionRequest,
    merchant: Dict[str, Any] = Depends(require_approved_merchant),
) -> Dict[str, str]:
    """Open a Stripe Billing Portal so a merchant can self-serve their plan.

    The portal is the merchant's off-ramp: from it they can cancel their
    subscription (Stripe schedules it to stop at period end — no more surprise
    monthly charges), update or remove their payment card, and view invoices.
    It is Stripe-hosted, so we never handle raw card data.

    Nothing new is needed on the receiving side: whatever the merchant does in
    the portal arrives back through the existing ``customer.subscription.updated``
    (which reads ``cancel_at_period_end`` / ``canceled_at``) and
    ``customer.subscription.deleted`` webhook handlers, so the local
    subscription row stays in sync automatically.
    """

    _require_platform_stripe_key()

    merchant_id = _as_text(merchant.get("merchant_id"))
    return_url = _as_text(body.return_url)
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Authenticated merchant is missing merchant_id")
    if not return_url:
        raise HTTPException(status_code=400, detail="return_url is required")

    contact_email = _as_text(merchant.get("contact_email"))
    billing_row = await _fetch_merchant_billing_row(
        database,
        merchant_id=merchant_id,
        contact_email=contact_email,
        stripe_customer_id=None,
    )
    stripe_customer_id = _as_text((billing_row or {}).get("stripe_customer_id"))
    if not stripe_customer_id:
        # No Stripe customer => the merchant has never subscribed off-platform
        # (free tier, App Store install, or never completed checkout). There is
        # nothing to manage; 404 lets the portal render a "no subscription"
        # state rather than surfacing a server error.
        raise HTTPException(status_code=404, detail="No subscription to manage for this merchant")

    try:
        session = await asyncio.to_thread(
            stripe_client.v1.billing_portal.sessions.create,
            {"customer": stripe_customer_id, "return_url": return_url},
        )
    except Exception as exc:
        # The Billing Portal requires a one-time configuration in the Stripe
        # Dashboard (Settings -> Billing -> Customer portal). Until that exists,
        # Stripe raises an InvalidRequestError mentioning "configuration" — call
        # it out explicitly so the operator knows the fix is a dashboard toggle,
        # not a code bug.
        if "configuration" in str(exc).lower():
            logger.error(
                "Stripe Billing Portal is not configured for merchant_id=%s: %s",
                merchant_id,
                exc,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    "The billing portal is not configured yet. An administrator "
                    "must enable it in the Stripe Dashboard (Settings -> Billing "
                    "-> Customer portal)."
                ),
            )
        logger.warning(
            "Stripe billing portal session failed for merchant_id=%s: %s",
            merchant_id,
            exc,
        )
        raise HTTPException(status_code=502, detail="Could not open the billing portal. Please try again.")

    portal_url = _as_text(getattr(session, "url", None) or _stripe_object_to_dict(session).get("url"))
    if not portal_url:
        raise HTTPException(status_code=502, detail="Stripe Billing Portal returned no URL")

    return {"portal_url": portal_url}


@router.post("/api/credits/topup")
async def create_credit_topup(
    body: CreditTopUpRequest,
    auth_merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Create a manual direct-credit top-up PaymentIntent on the verified card."""

    _require_platform_stripe_key()
    merchant_id = _as_text(body.merchant_id)
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    if merchant_id != auth_merchant_id:
        raise HTTPException(
            status_code=403,
            detail="merchant_id in body must match the authenticated merchant.",
        )
    # App Store / exempt merchants must not be charged off-platform.
    if await _merchant_is_billing_free(merchant_id):
        raise HTTPException(status_code=403, detail="This merchant is not eligible for off-platform billing.")
    if int(body.pack_credits or 0) <= 0:
        raise HTTPException(status_code=422, detail="pack_credits must be > 0")

    from services.merchant_credit_balance_service import (
        MissingVerifiedPaymentMethodError,
        create_credit_topup_payment_intent,
    )

    try:
        return await create_credit_topup_payment_intent(
            merchant_id=merchant_id,
            pack_credits=int(body.pack_credits),
            idempotency_key=body.idempotency_key,
        )
    except MissingVerifiedPaymentMethodError as exc:
        raise HTTPException(
            status_code=402,
            detail={
                "error": exc.code,
                "message": (
                    "Manual credit top-ups require a verified Stripe card."
                ),
                "reason": exc.reason,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
                  source_type,
                  metadata
                )
                VALUES (
                  :merchant_id,
                  'subscription_initial_grant',
                  :operation_id,
                  :credits_delta,
                  :balance_after,
                  'subscription_grant',
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

            # Create / refresh the merchant_credits row T5 metering operates on.
            # Without this, the first reserve() call fails because there's no
            # row to SELECT FOR UPDATE. Idempotent: ON CONFLICT updates allowance
            # if the merchant re-subscribes after cancellation.
            tier_allowance = int(plan["monthly_credit_allowance"] or 0)
            await db.execute(
                """
                INSERT INTO merchant_credits (
                  merchant_id, balance, tier_allowance, period_start, created_at, last_updated
                )
                VALUES (
                  :merchant_id, :balance, :tier_allowance, NOW(), NOW(), NOW()
                )
                ON CONFLICT (merchant_id) DO UPDATE SET
                  balance = EXCLUDED.balance,
                  tier_allowance = EXCLUDED.tier_allowance,
                  last_updated = NOW()
                """,
                {
                    "merchant_id": merchant_id,
                    "balance": tier_allowance,
                    "tier_allowance": tier_allowance,
                },
            )

        # Stripe sends customer.subscription.updated soon after checkout, which
        # populates current_period_start/end on the user_subscriptions row. But
        # the .updated event isn't guaranteed to fire before downstream code
        # (e.g. test-mode flows, programmatic fulfillment, immediate metering
        # reservations) reads the row. Fetch the subscription now and stamp the
        # period fields directly so the row is immediately consistent.
        try:
            sub_obj = await asyncio.to_thread(
                stripe_client.v1.subscriptions.retrieve, stripe_subscription_id
            )
            sub_dict = _stripe_object_to_dict(sub_obj)
            period_start_raw, period_end_raw = _extract_subscription_period(sub_dict)
            if period_start_raw and period_end_raw:
                await db.execute(
                    """
                    UPDATE user_subscriptions
                    SET current_period_start = :ps, current_period_end = :pe
                    WHERE id = :id
                    """,
                    {
                        "ps": _stripe_timestamp(period_start_raw),
                        "pe": _stripe_timestamp(period_end_raw),
                        "id": local_subscription_id,
                    },
                )
        except Exception as period_exc:
            # Don't fail fulfillment over period stamping — customer.subscription.updated
            # will converge it later. Just log so it's noticeable.
            logger.warning(
                "Could not stamp subscription period on checkout fulfillment for %s: %s; "
                "customer.subscription.updated webhook will populate later",
                stripe_subscription_id,
                period_exc,
            )

        # A new paid subscription supersedes any prior active subscription the
        # merchant still holds. Without this, an upgrade where the old plan was
        # never cancelled double-bills the merchant and leaves duplicate active
        # rows that make tier resolution ambiguous (the lower plan can outrank
        # the upgrade on renewal). Cancel the prior subscription(s) in Stripe
        # and locally. Runs after the new subscription is committed and outside
        # the fulfillment transaction; best-effort so cleanup never blocks the
        # new subscription.
        await _cancel_prior_active_subscriptions(
            db,
            merchant_id=merchant_id,
            keep_stripe_subscription_id=stripe_subscription_id,
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
                "current_period_start": _stripe_timestamp(_extract_subscription_period(subscription)[0]),
                "current_period_end": _stripe_timestamp(_extract_subscription_period(subscription)[1]),
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
            await _reconcile_after_subscription_ended(
                db,
                merchant_id=merchant_id,
                contact_email=contact_email,
                ended_stripe_subscription_id=stripe_subscription_id,
            )
        elif plan and int(existing["plan_id"]) != int(plan["id"]):
            await _update_merchant_tier(
                db,
                merchant_id=merchant_id,
                contact_email=contact_email,
                tier_name=_as_text(plan["name"]),
            )
            # A mid-cycle plan change (e.g. upgrade) must refresh the credit
            # wallet too — the merchants.current_tier UPDATE above doesn't touch
            # it, and the wallet's plan_tier/allowance feed the AI-readiness /
            # audit credit preview. Mirrors the downgrade path's
            # expire_plan_allowance call. Best-effort: a wallet hiccup must not
            # fail the webhook — the next balance read self-heals via
            # apply_subscription_allowance.
            try:
                from services.merchant_credit_balance_service import (
                    apply_subscription_allowance,
                )

                await apply_subscription_allowance(merchant_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "apply_subscription_allowance failed on plan change for "
                    "merchant_id=%s",
                    merchant_id,
                    exc_info=True,
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
            await _reconcile_after_subscription_ended(
                db,
                merchant_id=merchant_id,
                contact_email=contact_email,
                ended_stripe_subscription_id=stripe_subscription_id,
            )
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
        # The UPDATE only references a subset of invoice_values; the databases
        # async wrapper around SQLAlchemy text() rejects extra bound parameters
        # ("This text() construct doesn't define a bound parameter named X").
        # Project only the keys the UPDATE actually binds.
        update_params = {
            k: invoice_values[k]
            for k in ("total_cents", "due_date", "finalized_at", "stripe_invoice_id")
        }
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
            update_params,
        )
        if not updated:
            await _insert_minimal_invoice(db, invoice_values)

        # Partner lifecycle: a paid, positive-net invoice is the Track-B
        # activation event. Stamp partner_attribution.activated_at so the
        # merchant's brand starts accruing rev-share. Best-effort — activation
        # is write-once/idempotent and a miss is re-covered by any later
        # invoice.paid, so it must never fail the webhook.
        if merchant_id:
            try:
                outcomes = await activate_brands_for_merchant(merchant_id=merchant_id)
                activated = [o for o in outcomes if o.get("activated")]
                if activated:
                    logger.info(
                        "invoice.paid activated %d partner attribution(s) for "
                        "merchant_id=%s partners=%s",
                        len(activated),
                        merchant_id,
                        ",".join(str(o["channel_partner_id"]) for o in activated),
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "partner activation hook failed for merchant_id=%s "
                    "stripe_invoice_id=%s",
                    merchant_id,
                    stripe_invoice_id,
                    exc_info=True,
                )

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
        # See note in _handle_invoice_paid above — UPDATE references a subset
        # of the values; project to avoid the databases-wrapper bound-param
        # rejection.
        update_params = {
            k: invoice_values[k]
            for k in ("total_cents", "due_date", "finalized_at", "stripe_invoice_id")
        }
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
            update_params,
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


async def _handle_payment_intent_succeeded(event: Dict[str, Any], db: Database) -> str:
    event_id = _event_id(event)
    try:
        payment_intent = _event_object(event)
        metadata = _coerce_dict(payment_intent.get("metadata"))
        if _as_text(metadata.get("pivota_purpose")) != "direct_credit_topup":
            await _mark_event_ignored(event_id, db)
            return "ignored"

        from services.merchant_credit_balance_service import (
            apply_credit_topup_payment_intent_succeeded,
        )

        result = await apply_credit_topup_payment_intent_succeeded(
            payment_intent,
            conn=db,
        )
        if result.get("ignored"):
            await _mark_event_ignored(event_id, db)
            return "ignored"
        await _mark_event_processed(event_id, db)
        return "processed"
    except Exception as exc:
        await _handle_billing_handler_error(event_id, exc, db)
        return "failed"


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


# Pending rows older than this are treated as orphaned (handler crashed mid-
# flight). Tuned so a legitimate slow handler doesn't get reclaimed under it,
# while a crashed-then-retried Stripe event still gets reprocessed within ~1
# delivery cycle of Stripe's exponential backoff (~ couple of minutes).
STALE_PENDING_SECONDS = 300


async def _claim_retryable_event(
    event_id: str,
    event_type: str,
    event: Dict[str, Any],
    db: Database,
) -> str:
    """Decide what to do with a Stripe event whose row already exists.

    Returns one of:
      - "ack_duplicate" → already processed/ignored; caller should 200 OK.
      - "concurrent_in_flight" → another worker is processing; 409.
      - "reclaimed_failed" / "reclaimed_stale_pending" → caller should run the
        handler. The row has been transitioned back to 'pending' with the
        latest payload under a row lock.
    """
    if IS_POSTGRES:
        # SKIP LOCKED so a concurrent retry sees no row to claim and surfaces
        # "concurrent_in_flight" instead of blocking on the lock.
        row = await db.fetch_one(
            """
            SELECT id, status,
                   EXTRACT(EPOCH FROM (NOW() - received_at)) AS age_seconds
            FROM stripe_events
            WHERE event_id = :event_id
            FOR UPDATE SKIP LOCKED
            """,
            {"event_id": event_id},
        )
    else:
        row = await db.fetch_one(
            "SELECT id, status, "
            "(strftime('%s','now') - strftime('%s', received_at)) AS age_seconds "
            "FROM stripe_events WHERE event_id = :event_id",
            {"event_id": event_id},
        )

    if row is None:
        # Lost the race for the lock — another worker has the row open.
        return "concurrent_in_flight"

    status = str(row["status"] or "").lower()
    if status in ("processed", "ignored"):
        return "ack_duplicate"

    age_seconds = float(row["age_seconds"] or 0)
    if status == "pending" and age_seconds < STALE_PENDING_SECONDS:
        return "concurrent_in_flight"

    # status == 'failed' OR status == 'pending' stale → reclaim. Refresh the
    # payload (Stripe sometimes enriches on retry) and reset status to
    # 'pending' under the same lock we already hold.
    payload = json.dumps(event, default=str)
    if IS_POSTGRES:
        await db.execute(
            """
            UPDATE stripe_events
            SET status = 'pending',
                error = NULL,
                payload_jsonb = CAST(:payload_json AS jsonb),
                event_type = :event_type,
                received_at = NOW(),
                processed_at = NULL,
                updated_at = NOW()
            WHERE event_id = :event_id
            """,
            {"event_id": event_id, "event_type": event_type, "payload_json": payload},
        )
    else:
        await db.execute(
            """
            UPDATE stripe_events
            SET status = 'pending',
                error = NULL,
                payload_jsonb = :payload_json,
                event_type = :event_type,
                received_at = CURRENT_TIMESTAMP,
                processed_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE event_id = :event_id
            """,
            {"event_id": event_id, "event_type": event_type, "payload_json": payload},
        )
    if status == "failed":
        return "reclaimed_failed"
    return "reclaimed_stale_pending"


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


def _platform_stripe_mode() -> str:
    """Return the Stripe mode ('live'/'test') implied by the platform secret key."""

    key = str(settings.stripe_secret_key or "").strip().lower()
    return "live" if key.startswith("sk_live") or key.startswith("rk_live") else "test"


async def _list_active_paid_plans(db: Database, *, mode: str) -> list[Dict[str, Any]]:
    """Active, checkout-able plans for the given Stripe mode, cheapest first.

    Only merchant-safe fields are returned (no tier_level/features_json). Plans
    without a stripe_price_id or with price_cents=0 (e.g. 'free') are excluded —
    they can't be the target of a Checkout session.
    """

    columns = await _table_columns(db, "subscription_plans")
    if not columns:
        return []
    where = ["status = 'active'", "stripe_price_id IS NOT NULL", "price_cents > 0"]
    params: Dict[str, Any] = {}
    if "stripe_mode" in columns:
        where.append("stripe_mode = :mode")
        params["mode"] = mode
    order_by = "tier_level ASC" if "tier_level" in columns else "price_cents ASC"
    rows = await db.fetch_all(
        f"""
        SELECT name, stripe_price_id, price_cents, monthly_credit_allowance
        FROM subscription_plans
        WHERE {" AND ".join(where)}
        ORDER BY {order_by}
        """,
        params,
    )
    plans: list[Dict[str, Any]] = []
    for raw in rows or []:
        row = dict(raw)
        price_id = _as_text(row.get("stripe_price_id"))
        plans.append(
            {
                "key": price_id,
                "name": _as_text(row.get("name")),
                "price_id": price_id,
                "price_cents": int(row.get("price_cents") or 0),
                "monthly_credit_allowance": int(row.get("monthly_credit_allowance") or 0),
                "currency": "usd",
            }
        )
    return plans


async def _lookup_subscription_plan(
    db: Database,
    *,
    stripe_price_id: Optional[str],
    stripe_product_id: Optional[str],
    mode: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    # Resolve plans in the platform's current Stripe mode (live in prod). Test
    # and live plan rows share a tier but carry distinct price ids; filtering by
    # stripe_mode guards against ever resolving a live checkout to a test-mode
    # plan row (mirrors _list_active_paid_plans). Column-guarded for old schemas.
    mode = mode or _platform_stripe_mode()
    has_mode = await _table_has_column(db, "subscription_plans", "stripe_mode")
    mode_clause = "AND stripe_mode = :mode" if has_mode else ""

    if stripe_price_id:
        params: Dict[str, Any] = {"stripe_price_id": stripe_price_id}
        if has_mode:
            params["mode"] = mode
        row = await db.fetch_one(
            f"""
            SELECT id, name, stripe_price_id, monthly_credit_allowance
            FROM subscription_plans
            WHERE stripe_price_id = :stripe_price_id
              AND status = 'active'
              {mode_clause}
            LIMIT 1
            """,
            params,
        )
        if row:
            return dict(row)

    if stripe_product_id and await _table_has_column(db, "subscription_plans", "stripe_product_id"):
        params = {"stripe_product_id": stripe_product_id}
        if has_mode:
            params["mode"] = mode
        row = await db.fetch_one(
            f"""
            SELECT id, name, stripe_price_id, monthly_credit_allowance
            FROM subscription_plans
            WHERE stripe_product_id = :stripe_product_id
              AND status = 'active'
              {mode_clause}
            LIMIT 1
            """,
            params,
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

    # ADR-005 §2: a downgrade wipes the monthly plan allowance (top-up credits
    # persist). The merchants tier update above doesn't touch the credit wallet,
    # so do it here. Best-effort — a balance hiccup must not fail the webhook's
    # tier downgrade (the allowance would just linger until the next reset).
    if merchant_id:
        try:
            from services.merchant_credit_balance_service import expire_plan_allowance
            await expire_plan_allowance(merchant_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "expire_plan_allowance failed on downgrade for merchant_id=%s",
                merchant_id,
                exc_info=True,
            )


async def _reconcile_after_subscription_ended(
    db: Database,
    *,
    merchant_id: str,
    contact_email: Optional[str],
    ended_stripe_subscription_id: Optional[str],
) -> None:
    """Re-resolve a merchant's tier after one of their subscriptions ends.

    A merchant may hold more than one active subscription (e.g. a superseded
    plan during an upgrade window, or a coupon'd secondary plan). Cancelling or
    ending one of them must NOT drop the merchant to free while a higher plan is
    still active. Prefer the richest remaining active subscription; only fall
    back to the free downgrade when none remain. Mirrors the
    richest-plan-wins ordering used by the tier resolvers.
    """
    remaining = await db.fetch_one(
        """
        SELECT sp.name AS tier_name
          FROM user_subscriptions us
          JOIN subscription_plans sp ON sp.id = us.plan_id
         WHERE us.merchant_id = :merchant_id
           AND us.status IN ('active', 'trialing')
           AND sp.status = 'active'
           AND us.stripe_subscription_id IS DISTINCT FROM :ended
         ORDER BY sp.monthly_credit_allowance DESC NULLS LAST,
                  us.current_period_start DESC NULLS LAST,
                  us.id DESC
         LIMIT 1
        """,
        {"merchant_id": merchant_id, "ended": ended_stripe_subscription_id},
    )
    if not remaining:
        await _downgrade_merchant_to_free(
            db, merchant_id=merchant_id, contact_email=contact_email
        )
        return

    # Another active plan remains — keep the merchant on the richest one and
    # refresh the credit wallet to that plan rather than wiping to free.
    await _update_merchant_tier(
        db,
        merchant_id=merchant_id,
        contact_email=contact_email,
        tier_name=_as_text(remaining["tier_name"]),
    )
    if merchant_id:
        try:
            from services.merchant_credit_balance_service import (
                apply_subscription_allowance,
            )

            await apply_subscription_allowance(merchant_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "apply_subscription_allowance failed reconciling remaining "
                "subscription for merchant_id=%s",
                merchant_id,
                exc_info=True,
            )


async def _cancel_prior_active_subscriptions(
    db: Database,
    *,
    merchant_id: str,
    keep_stripe_subscription_id: str,
) -> None:
    """Cancel every active/trialing subscription for the merchant other than
    keep_stripe_subscription_id — in Stripe (so billing stops) and locally.

    Called after a new subscription checkout completes so the merchant ends up
    with a single active plan. Best-effort and isolated per subscription: a
    failure to cancel one prior subscription is logged and never interrupts the
    others or the new subscription's fulfillment. The resulting
    customer.subscription.deleted webhook is reconciled by
    _reconcile_after_subscription_ended, which keeps the merchant on the
    just-activated plan rather than downgrading to free.
    """
    if not merchant_id or not keep_stripe_subscription_id:
        return
    try:
        rows = await db.fetch_all(
            """
            SELECT stripe_subscription_id
              FROM user_subscriptions
             WHERE merchant_id = :merchant_id
               AND status IN ('active', 'trialing')
               AND stripe_subscription_id IS DISTINCT FROM :keep
            """,
            {"merchant_id": merchant_id, "keep": keep_stripe_subscription_id},
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not list prior subscriptions to cancel for merchant_id=%s",
            merchant_id,
            exc_info=True,
        )
        return

    for row in rows or []:
        prior_id = _as_text(dict(row).get("stripe_subscription_id"))
        if not prior_id:
            continue
        try:
            await asyncio.to_thread(
                stripe_client.v1.subscriptions.cancel, prior_id
            )
        except Exception:  # noqa: BLE001
            # Already cancelled in Stripe, or a transient API failure — mark the
            # local row cancelled regardless so tier resolution stops counting
            # it; the deleted webhook (if any) is idempotent.
            logger.warning(
                "Stripe cancel failed for prior subscription %s (merchant_id=%s); "
                "marking local row cancelled anyway",
                prior_id,
                merchant_id,
                exc_info=True,
            )
        try:
            await db.execute(
                """
                UPDATE user_subscriptions
                   SET status = 'canceled',
                       canceled_at = COALESCE(canceled_at, NOW())
                 WHERE stripe_subscription_id = :sid
                """,
                {"sid": prior_id},
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not mark prior subscription %s cancelled locally "
                "(merchant_id=%s)",
                prior_id,
                merchant_id,
                exc_info=True,
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


async def resolve_merchant_stripe_customer_id(
    db: Database,
    merchant_id: str,
) -> str:
    """Single source of truth: resolve a `merch_` id to its Stripe customer id.

    `merchants.merchant_id` is NOT a code-maintained link — no path populates it,
    and the row is provisioned separately from `merchant_onboarding` (which owns
    the `merch_` identity + contact_email). Checkout persists
    `stripe_customer_id` via a permissive `(merchant_id OR contact_email)` match,
    so a strict `merchant_id`-only read can miss the row the id landed on,
    surfacing as `missing_stripe_customer` at the paid-audit gate and breaking
    overage billing. Resolve `merchant_id` first, then fall back to the
    onboarding `contact_email` — the only reliable cross-link — targeting a row
    that actually carries a customer id.

    Integrated merchants only: crawled `agent_seed::` catalog merchants have no
    `merchant_onboarding` row, so this returns '' for them (correct — they are
    not billing entities).
    """
    billing_row = await _fetch_merchant_billing_row(
        db,
        merchant_id=merchant_id,
        contact_email=None,
        stripe_customer_id=None,
    )
    customer_id = _as_text((billing_row or {}).get("stripe_customer_id"))
    if customer_id:
        return customer_id

    onboarding = await get_merchant_onboarding(merchant_id)
    contact_email = _as_text((onboarding or {}).get("contact_email")) if onboarding else ""
    if not contact_email:
        return ""
    row = await db.fetch_one(
        """
        SELECT stripe_customer_id
        FROM merchants
        WHERE LOWER(contact_email) = LOWER(:contact_email)
          AND stripe_customer_id IS NOT NULL
          AND stripe_customer_id <> ''
        LIMIT 1
        """,
        {"contact_email": contact_email},
    )
    return _as_text(dict(row).get("stripe_customer_id")) if row else ""


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
    # Stripe SDK 15.x (pinned in requirements.txt) renamed `to_dict_recursive`
    # to `to_dict()` and removed the dict-subclass shim, so the old check fell
    # through and returned the StripeObject as-is. Downstream `.get(...)` then
    # raised AttributeError on every real Stripe webhook (the clock harness
    # masked this by monkeypatching construct_event to return a plain dict;
    # surfaced 2026-05-23 by the §B webhook mirror shakeout).
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        # to_dict() in 15.x already returns a fully-recursive plain dict, but
        # walk it anyway so older SDKs (with shallow to_dict) work too.
        if isinstance(result, dict):
            return {str(k): _stripe_object_to_dict(v) for k, v in result.items()}
        return result
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


def _extract_subscription_period(subscription: Dict[str, Any]) -> Tuple[Optional[Any], Optional[Any]]:
    """Return (current_period_start, current_period_end) Stripe timestamps.

    Stripe moved these from the Subscription object to per-item in API version
    2025+. Read from `subscription.items.data[0]` first, fall back to top-level
    for older API versions / test clients that still return the legacy shape.
    """
    items = _coerce_dict(subscription.get("items"))
    data = items.get("data") if isinstance(items.get("data"), list) else []
    if data:
        first_item = _coerce_dict(data[0])
        item_start = first_item.get("current_period_start")
        item_end = first_item.get("current_period_end")
        if item_start is not None and item_end is not None:
            return item_start, item_end
    return subscription.get("current_period_start"), subscription.get("current_period_end")


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
    # billing_period_{start,end} are DATE columns (migration 120 converted
    # them from TIMESTAMPTZ). A timedelta(seconds=1) round-trips to the same
    # DATE, violating ck_invoices_billing_period_order. Use days to guarantee
    # period_end > period_start at the DATE level. Surfaced by §B shakeout
    # 2026-05-23.
    period_end = period_end or period_start + timedelta(days=1)
    if period_end.date() <= period_start.date():
        period_end = period_start + timedelta(days=1)

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
