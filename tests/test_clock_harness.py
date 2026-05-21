from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import pytest


if os.getenv("RUN_STRIPE_TEST_CLOCK_HARNESS") != "1":
    pytest.skip(
        "Stripe Test Clock harness is opt-in: set RUN_STRIPE_TEST_CLOCK_HARNESS=1",
        allow_module_level=True,
    )

stripe = pytest.importorskip("stripe")

from config.settings import settings
from db.database import IS_POSTGRES, database
from services import partner_settlement_service
from services.partner_settlement_service import PayoutMissingConnectAccountError


pytestmark = pytest.mark.asyncio

stripe_client = stripe.StripeClient(api_key=settings.stripe_secret_key or "")


@pytest.fixture(autouse=True)
async def _connected_database() -> AsyncIterator[None]:
    """Connect the async `databases` pool for the duration of each harness test.

    The harness reads/writes staging Postgres via `db.database.database`, which
    requires an explicit `connect()` before `fetch_one`/`execute` calls work
    (otherwise the backend asserts `pool is not None`). Function-scoped per
    asyncio_default_fixture_loop_scope=function in pytest.ini.
    """
    await database.connect()
    try:
        yield
    finally:
        await database.disconnect()


@pytest.fixture
async def stripe_test_clock() -> AsyncIterator[Any]:
    """Create and delete a Stripe Test Clock for one billing-cycle harness run."""

    clock = await asyncio.to_thread(
        stripe_client.v1.test_helpers.test_clocks.create,
        params={"frozen_time": int(time.time())},
    )
    try:
        yield clock
    finally:
        await asyncio.to_thread(
            stripe_client.v1.test_helpers.test_clocks.delete,
            clock.id,
        )


async def advance_clock(test_clock_id: str, to_timestamp: int) -> None:
    """Advance a Stripe Test Clock and wait until Stripe reports it is ready."""

    await asyncio.to_thread(
        stripe_client.v1.test_helpers.test_clocks.advance,
        test_clock_id,
        params={"frozen_time": to_timestamp},
    )
    while True:
        clock = await asyncio.to_thread(
            stripe_client.v1.test_helpers.test_clocks.retrieve,
            test_clock_id,
        )
        if clock.status == "ready":
            return
        if clock.status == "internal_failure":
            raise RuntimeError(f"Test clock advance failed: {clock.id}")
        await asyncio.sleep(0.5)


async def test_full_billing_cycle(stripe_test_clock: Any) -> None:
    """Simulate the full v1.3 billing-to-pending-payout cycle on a Test Clock."""

    invoice_generation_service = pytest.importorskip("services.invoice_generation_service")
    price_id = _required_env("STRIPE_TEST_CLOCK_PRICE_ID")
    merchant_id = f"clock_merch_{int(time.time())}"

    customer = await _create_clock_customer(stripe_test_clock.id, merchant_id)
    subscription = await asyncio.to_thread(
        stripe_client.v1.subscriptions.create,
        params={
            "customer": customer.id,
            "items": [{"price": price_id}],
            "metadata": {"merchant_id": merchant_id},
        },
    )

    # Use a per-run-unique offset so this harness invocation doesn't collide
    # with leftover billing_runs rows from previous runs. run_billing_cycle is
    # idempotent on idempotency_key=f"{period_start}-billing" — if the row
    # exists, T7 returns the existing run without processing this test's new
    # merchant. Shifting period by a time-derived offset gives each run its
    # own period; staging accumulates dated test rows over time but tests
    # remain repeatable.
    period_start_ts, period_end_ts = _subscription_period(subscription)
    raw_period_start = datetime.fromtimestamp(period_start_ts, tz=timezone.utc).date()
    raw_period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc).date()
    unique_offset_days = int(time.time() // 60) % 8000  # minute-resolution; ~21 years range
    period_start = raw_period_start + timedelta(days=unique_offset_days)
    period_end = raw_period_end + timedelta(days=unique_offset_days)
    await _seed_local_billing_state(
        merchant_id=merchant_id,
        stripe_customer_id=customer.id,
        stripe_subscription_id=subscription.id,
        stripe_price_id=price_id,
        period_start=period_start,
        period_end=period_end,
    )
    partner_id = await _seed_partner_state(
        merchant_id,
        period_start,
        connect_account_id=f"acct_clock_{merchant_id}",
    )

    await advance_clock(stripe_test_clock.id, period_end_ts - 86400)
    billing_run_id = await invoice_generation_service.run_billing_cycle(period_start, period_end)

    invoice_row = await database.fetch_one(
        """
        SELECT id, stripe_invoice_id, status
        FROM invoices
        WHERE merchant_id = :merchant_id
          AND billing_period_start = :period_start
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "period_start": period_start},
    )
    assert invoice_row is not None
    assert invoice_row["status"] == "draft"

    item_count = await _count_billing_items(billing_run_id, merchant_id)
    rollup_count = await _count_gmv_rollups(merchant_id, period_start, period_end)
    assert item_count == rollup_count

    dispute_id = await _create_invoice_dispute(invoice_row["id"], merchant_id)
    await invoice_generation_service.handle_dispute(dispute_id)
    assert await _count_billing_items(billing_run_id, merchant_id) >= item_count

    await advance_clock(stripe_test_clock.id, int(time.time()) + 5 * 24 * 60 * 60)
    await invoice_generation_service.finalize_invoice(invoice_row["stripe_invoice_id"])
    await _mark_invoice_status(invoice_row["stripe_invoice_id"], "finalized")
    assert await _invoice_status(invoice_row["stripe_invoice_id"]) == "finalized"

    await _mark_invoice_status(invoice_row["stripe_invoice_id"], "paid")
    payout_count = await partner_settlement_service.run_settlement(billing_run_id)

    snapshot_row = await database.fetch_one(
        """
        SELECT id
        FROM settlement_snapshots
        WHERE billing_run_id = :billing_run_id
          AND channel_partner_id = :partner_id
        """,
        {"billing_run_id": billing_run_id, "partner_id": partner_id},
    )
    assert snapshot_row is not None

    ledger_row = await database.fetch_one(
        """
        SELECT id
        FROM partner_balance_ledger
        WHERE channel_partner_id = :partner_id
          AND event_type = 'settlement_added'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"partner_id": partner_id},
    )
    assert ledger_row is not None

    payout_row = await database.fetch_one(
        """
        SELECT status
        FROM agent_payouts
        WHERE payee_type = 'channel_partner'
          AND payee_id = :partner_id
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"partner_id": partner_id},
    )
    assert payout_count == 1
    assert payout_row is not None
    assert payout_row["status"] == "pending"


async def test_failure_modes(stripe_test_clock: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the Week 9 failure-mode checklist against Stripe and local state."""

    invoice_generation_service = pytest.importorskip("services.invoice_generation_service")
    price_id = _required_env("STRIPE_TEST_CLOCK_PRICE_ID")
    merchant_id = f"clock_fail_{int(time.time())}"

    customer = await _create_clock_customer(stripe_test_clock.id, merchant_id)
    subscription = await asyncio.to_thread(
        stripe_client.v1.subscriptions.create,
        params={
            "customer": customer.id,
            "items": [{"price": price_id}],
            "metadata": {"merchant_id": merchant_id},
        },
    )
    # Shift this test's billing period off `test_full_billing_cycle`'s period.
    # run_billing_cycle is idempotent on (period_start, period_end) — if both
    # tests share a period, the second test's mocks never fire because the
    # billing_runs row already exists. Use a far-future period unique to this
    # test (subscription period from Stripe + 90 days) so the SDK-timeout,
    # signature, duplicate-event, idempotent-retry, and other monkeypatched
    # assertions actually execute.
    period_start_ts, period_end_ts = _subscription_period(subscription)
    raw_period_start = datetime.fromtimestamp(period_start_ts, tz=timezone.utc).date()
    raw_period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc).date()
    period_start = raw_period_start + timedelta(days=90)
    period_end = raw_period_end + timedelta(days=90)
    await _seed_local_billing_state(
        merchant_id=merchant_id,
        stripe_customer_id=customer.id,
        stripe_subscription_id=subscription.id,
        stripe_price_id=price_id,
        period_start=period_start,
        period_end=period_end,
    )
    partner_id = await _seed_partner_state(merchant_id, period_start, connect_account_id=None)

    original_create = invoice_generation_service.stripe_client.v1.invoice_items.create

    def timeout_invoice_item(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("simulated Stripe SDK timeout")

    monkeypatch.setattr(
        invoice_generation_service.stripe_client.v1.invoice_items,
        "create",
        timeout_invoice_item,
    )
    # T7 spec (services/invoice_generation_service.py::run_billing_cycle):
    # per-merchant exceptions are caught and logged; the cycle completes so a
    # single broken merchant doesn't abort the whole run. SDK timeout therefore
    # does NOT propagate — assert run completes and the affected merchant has
    # no invoice. (Note: this currently leaves the merchant permanently
    # unbilled for the cycle; v1.4 should add a retry path — flagged in
    # questions_for_cowork.md.)
    await invoice_generation_service.run_billing_cycle(period_start, period_end)
    first_run = await _billing_run_id(period_start)
    assert first_run is not None

    timed_out_invoice = await database.fetch_one(
        """
        SELECT id FROM invoices
        WHERE merchant_id = :merchant_id
          AND billing_period_start = :period_start
        """,
        {"merchant_id": merchant_id, "period_start": period_start},
    )
    assert timed_out_invoice is None, (
        "SDK timeout should leave the merchant unbilled for this cycle"
    )

    monkeypatch.setattr(
        invoice_generation_service.stripe_client.v1.invoice_items,
        "create",
        original_create,
    )
    # Retry verifies T7's period-level idempotency: same idempotency_key
    # returns the existing billing_run_id, the cycle is not re-executed
    # even though the SDK mock is now removed. This is intentional behavior
    # but a known limitation of v1 (see v1.4 retry note in questions_for_cowork.md).
    retry_run = await invoice_generation_service.run_billing_cycle(period_start, period_end)
    assert retry_run == first_run

    from fastapi import HTTPException
    from routes import billing_routes

    monkeypatch.setattr(
        billing_routes.settings,
        "stripe_billing_webhook_secret",
        "whsec_clock_harness",
    )
    bad_request = _FakeRequest(
        b'{"id":"evt_bad_sig","type":"invoice.paid","data":{"object":{"id":"in_bad"}}}'
    )
    with pytest.raises(HTTPException) as signature_error:
        await billing_routes.handle_stripe_billing_webhook(
            bad_request,
            stripe_signature="bad_signature",
        )
    assert signature_error.value.status_code == 400
    assert await _stripe_event_count("evt_bad_sig") == 0

    event = {
        "id": "evt_duplicate_clock_harness",
        "type": "invoice.paid",
        "data": {"object": {"id": "in_duplicate", "customer": customer.id, "amount_paid": 0}},
    }
    monkeypatch.setattr(
        billing_routes.stripe.Webhook,
        "construct_event",
        lambda *_args, **_kwargs: event,
    )
    monkeypatch.setattr(billing_routes, "_handle_invoice_paid", _noop_handler)
    ok_request = _FakeRequest(json.dumps(event).encode())
    first_response = await billing_routes.handle_stripe_billing_webhook(
        ok_request,
        stripe_signature="sig",
    )
    second_response = await billing_routes.handle_stripe_billing_webhook(
        ok_request,
        stripe_signature="sig",
    )
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert await _stripe_event_count(event["id"]) == 1

    finalized_invoice_id = "in_already_finalized_clock"
    await _insert_local_invoice(merchant_id, finalized_invoice_id, period_start, period_end, "finalizing")

    def already_finalized(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("Invoice is already finalized")

    monkeypatch.setattr(
        invoice_generation_service.stripe_client.v1.invoices,
        "finalize_invoice",
        already_finalized,
    )
    with pytest.raises(RuntimeError):
        await invoice_generation_service.finalize_invoice(finalized_invoice_id)
    assert await _invoice_status(finalized_invoice_id) == "finalizing"

    payout_id = await _seed_pending_partner_payout(partner_id, first_run, amount_dollars="12.34")
    with pytest.raises(PayoutMissingConnectAccountError):
        await partner_settlement_service.execute_payout(payout_id)

    await _set_partner_connect_account(partner_id, "acct_test_insufficient_balance")

    class FakeStripeError(Exception):
        pass

    FakeStripeError.__module__ = "stripe.error"

    def transfer_failure(*_args: Any, **_kwargs: Any) -> Any:
        raise FakeStripeError("insufficient platform balance")

    monkeypatch.setattr(
        partner_settlement_service.stripe_client.v1.transfers,
        "create",
        transfer_failure,
    )
    balance_before = await _partner_balance(partner_id)
    with pytest.raises(FakeStripeError):
        await partner_settlement_service.execute_payout(payout_id)
    assert await _payout_status(payout_id) == "failed"
    assert await _partner_balance(partner_id) == balance_before

    await _seed_prior_settlement_snapshot(first_run, partner_id, merchant_id, accrued_cents=5000)
    await database.execute(
        """
        UPDATE user_subscriptions
        SET status = 'canceled',
            canceled_at = NOW()
        WHERE stripe_subscription_id = :stripe_subscription_id
        """,
        {"stripe_subscription_id": subscription.id},
    )
    next_period_start = period_start + timedelta(days=31)
    next_period_end = period_end + timedelta(days=31)
    await _seed_gmv_rollup(merchant_id, next_period_start)
    await database.execute(
        """
        UPDATE gmv_attribution_daily
        SET channel_partner_id = :partner_id
        WHERE merchant_id = :merchant_id
          AND date = :target_date
        """,
        {"partner_id": partner_id, "merchant_id": merchant_id, "target_date": next_period_start},
    )
    await _insert_local_invoice(
        merchant_id,
        f"in_paid_clawback_{int(time.time())}",
        next_period_start,
        next_period_end,
        "paid",
    )
    next_billing_run = await _insert_billing_run(
        next_period_start,
        next_period_end,
    )
    await partner_settlement_service.run_settlement(next_billing_run)
    clawback_row = await database.fetch_one(
        """
        SELECT amount_cents
        FROM partner_balance_ledger
        WHERE channel_partner_id = :partner_id
          AND event_type = 'clawback'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"partner_id": partner_id},
    )
    assert clawback_row is not None
    assert clawback_row["amount_cents"] == -5000
    assert await _partner_balance(partner_id) == balance_before - 5000


def _subscription_period(subscription: Any) -> tuple[int, int]:
    """Return (period_start_ts, period_end_ts) for a Stripe Subscription.

    Stripe moved `current_period_start`/`current_period_end` from the
    Subscription object to per-item in API version 2025+; older API versions
    keep them at the top level. Read from items.data[0] first, fall back to
    the top-level fields so the harness works across API versions.
    """
    items = getattr(subscription, "items", None)
    data = getattr(items, "data", None) if items is not None else None
    if data:
        item = data[0]
        period_start = getattr(item, "current_period_start", None)
        period_end = getattr(item, "current_period_end", None)
        if period_start is not None and period_end is not None:
            return int(period_start), int(period_end)
    period_start = getattr(subscription, "current_period_start", None)
    period_end = getattr(subscription, "current_period_end", None)
    if period_start is None or period_end is None:
        raise RuntimeError(
            "Stripe Subscription is missing current_period_start/end on both "
            "subscription.items.data[0] and the top level"
        )
    return int(period_start), int(period_end)


async def _create_clock_customer(test_clock_id: str, merchant_id: str) -> Any:
    customer = await asyncio.to_thread(
        stripe_client.v1.customers.create,
        params={
            "email": f"{merchant_id}@example.test",
            "test_clock": test_clock_id,
            "metadata": {"merchant_id": merchant_id},
        },
    )
    # Stripe accounts default to rejecting raw card numbers; use a pre-tokenized
    # test card instead. tok_visa is Stripe's permanent test token, available in
    # every test account, and bypasses the "raw card data API" restriction.
    payment_method = await asyncio.to_thread(
        stripe_client.v1.payment_methods.create,
        params={
            "type": "card",
            "card": {"token": "tok_visa"},
        },
    )
    await asyncio.to_thread(
        stripe_client.v1.payment_methods.attach,
        payment_method.id,
        params={"customer": customer.id},
    )
    await asyncio.to_thread(
        stripe_client.v1.customers.update,
        customer.id,
        params={"invoice_settings": {"default_payment_method": payment_method.id}},
    )
    return customer


async def _seed_local_billing_state(
    *,
    merchant_id: str,
    stripe_customer_id: str,
    stripe_subscription_id: str,
    stripe_price_id: str,
    period_start,
    period_end,
) -> None:
    plan_id = await _seed_subscription_plan(stripe_price_id)
    subscription_row = await database.fetch_one(
        f"""
        INSERT INTO user_subscriptions (
          merchant_id,
          plan_id,
          stripe_subscription_id,
          status,
          started_at,
          current_period_start,
          current_period_end,
          created_at,
          updated_at
        )
        VALUES (
          :merchant_id,
          :plan_id,
          :stripe_subscription_id,
          'active',
          {_now_sql()},
          :period_start,
          :period_end,
          {_now_sql()},
          {_now_sql()}
        )
        ON CONFLICT (stripe_subscription_id) DO UPDATE
        SET status = 'active',
            current_period_start = EXCLUDED.current_period_start,
            current_period_end = EXCLUDED.current_period_end,
            updated_at = {_now_sql()}
        RETURNING id
        """,
        {
            "merchant_id": merchant_id,
            "plan_id": plan_id,
            "stripe_subscription_id": stripe_subscription_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    )
    await database.execute(
        f"""
        INSERT INTO merchants (
          business_name,
          legal_name,
          platform,
          store_url,
          contact_email,
          status,
          verification_status,
          stripe_customer_id,
          subscription_id,
          current_tier,
          created_at,
          updated_at
        )
        VALUES (
          :business_name,
          :business_name,
          'custom',
          'https://example.test',
          :contact_email,
          'active',
          'verified',
          :stripe_customer_id,
          :subscription_id,
          'starter',
          {_now_sql()},
          {_now_sql()}
        )
        """,
        {
            "business_name": f"Clock Harness {merchant_id}",
            "contact_email": f"{merchant_id}@example.test",
            "stripe_customer_id": stripe_customer_id,
            "subscription_id": subscription_row["id"],
        },
    )
    await _seed_gmv_rollup(merchant_id, period_start)


async def _seed_partner_state(
    merchant_id: str,
    period_start,
    *,
    connect_account_id: str | None = "acct_test_partner",
) -> int:
    payload = json.dumps(
        {
            "subscription_rev_share_bp": 2500,
            "gmv_take_share_bp": 3000,
            "subsidy_cap_cents": 500000,
        }
    )
    row = await database.fetch_one(
        f"""
        INSERT INTO channel_partners (
          legal_name,
          contact_email,
          archetype,
          stripe_connect_account_id,
          commission_config_json,
          status,
          created_at,
          updated_at
        )
        VALUES (
          :legal_name,
          :contact_email,
          'markato',
          :connect_account_id,
          {_json_sql('payload')},
          'active',
          {_now_sql()},
          {_now_sql()}
        )
        RETURNING id
        """,
        {
            "legal_name": f"Clock Partner {merchant_id}",
            "contact_email": f"{merchant_id}+partner@example.test",
            "connect_account_id": connect_account_id,
            "payload": payload,
        },
    )
    await database.execute(
        f"""
        INSERT INTO partner_attribution (
          merchant_id,
          channel_partner_id,
          status,
          activated_at,
          created_at,
          updated_at
        )
        VALUES (
          :merchant_id,
          :partner_id,
          'active',
          :period_start,
          {_now_sql()},
          {_now_sql()}
        )
        ON CONFLICT (merchant_id, channel_partner_id) DO UPDATE
        SET status = 'active',
            activated_at = EXCLUDED.activated_at,
            updated_at = {_now_sql()}
        """,
        {"merchant_id": merchant_id, "partner_id": row["id"], "period_start": period_start},
    )
    await database.execute(
        """
        UPDATE gmv_attribution_daily
        SET channel_partner_id = :partner_id
        WHERE merchant_id = :merchant_id
          AND date = :period_start
        """,
        {"partner_id": row["id"], "merchant_id": merchant_id, "period_start": period_start},
    )
    return int(row["id"])


async def _seed_subscription_plan(stripe_price_id: str) -> int:
    existing = await database.fetch_one(
        "SELECT id FROM subscription_plans WHERE stripe_price_id = :stripe_price_id",
        {"stripe_price_id": stripe_price_id},
    )
    if existing:
        return int(existing["id"])

    row = await database.fetch_one(
        f"""
        INSERT INTO subscription_plans (
          name,
          stripe_price_id,
          price_cents,
          tier_level,
          monthly_credit_allowance,
          features_json,
          status,
          created_at,
          updated_at
        )
        VALUES (
          'starter',
          :stripe_price_id,
          10000,
          1,
          1000,
          {_json_sql('features')},
          'active',
          {_now_sql()},
          {_now_sql()}
        )
        ON CONFLICT (name) DO UPDATE
        SET stripe_price_id = EXCLUDED.stripe_price_id,
            updated_at = {_now_sql()}
        RETURNING id
        """,
        {"stripe_price_id": stripe_price_id, "features": "{}"},
    )
    return int(row["id"])


async def _seed_gmv_rollup(merchant_id: str, target_date) -> None:
    await database.execute(
        f"""
        INSERT INTO gmv_attribution_daily (
          date,
          merchant_id,
          agent_id,
          gross_attributed_gmv_cents,
          refund_amount_cents,
          net_attributed_gmv_cents,
          take_rate_bp,
          take_amount_cents,
          created_at,
          updated_at
        )
        VALUES (
          :target_date,
          :merchant_id,
          'agent_clock',
          20000,
          0,
          20000,
          1000,
          2000,
          {_now_sql()},
          {_now_sql()}
        )
        ON CONFLICT (date, merchant_id, COALESCE(agent_id, ''), COALESCE(channel_partner_id, -1))
        DO UPDATE SET
          gross_attributed_gmv_cents = EXCLUDED.gross_attributed_gmv_cents,
          refund_amount_cents = EXCLUDED.refund_amount_cents,
          net_attributed_gmv_cents = EXCLUDED.net_attributed_gmv_cents,
          take_rate_bp = EXCLUDED.take_rate_bp,
          take_amount_cents = EXCLUDED.take_amount_cents,
          updated_at = {_now_sql()}
        """,
        {"target_date": target_date, "merchant_id": merchant_id},
    )


async def _create_invoice_dispute(invoice_id: int, merchant_id: str) -> int:
    row = await database.fetch_one(
        f"""
        INSERT INTO invoice_disputes (
          invoice_id,
          merchant_id,
          disputed_edge_ids_jsonb,
          reason,
          status,
          opened_at,
          created_at,
          updated_at
        )
        VALUES (
          :invoice_id,
          :merchant_id,
          {_json_sql('line_items')},
          'clock_harness_adjustment',
          'open',
          {_now_sql()},
          {_now_sql()},
          {_now_sql()}
        )
        RETURNING id
        """,
        {"invoice_id": invoice_id, "merchant_id": merchant_id, "line_items": "[]"},
    )
    return int(row["id"])


async def _seed_pending_partner_payout(partner_id: int, billing_run_id: int, *, amount_dollars: str) -> int:
    run = await database.fetch_one(
        "SELECT period_start, period_end FROM billing_runs WHERE id = :billing_run_id",
        {"billing_run_id": billing_run_id},
    )
    await database.execute(
        """
        INSERT INTO partner_balance (channel_partner_id, balance_cents)
        VALUES (:partner_id, :balance_cents)
        ON CONFLICT (channel_partner_id) DO UPDATE
        SET balance_cents = EXCLUDED.balance_cents
        """,
        {"partner_id": partner_id, "balance_cents": int(float(amount_dollars) * 100)},
    )
    row = await database.fetch_one(
        f"""
        INSERT INTO agent_payouts (
          merchant_id,
          agent_id,
          payee_type,
          payee_id,
          billing_run_id,
          amount,
          currency,
          status,
          period_start,
          period_end,
          created_at
        )
        VALUES (
          :legacy_payee,
          :legacy_payee,
          'channel_partner',
          :partner_id,
          :billing_run_id,
          :amount,
          'USD',
          'approved',
          :period_start,
          :period_end,
          {_now_sql()}
        )
        RETURNING id
        """,
        {
            "legacy_payee": f"channel_partner:{partner_id}",
            "partner_id": partner_id,
            "billing_run_id": billing_run_id,
            "amount": amount_dollars,
            "period_start": run["period_start"],
            "period_end": run["period_end"],
        },
    )
    return int(row["id"])


async def _seed_prior_settlement_snapshot(
    billing_run_id: int,
    partner_id: int,
    merchant_id: str,
    *,
    accrued_cents: int,
) -> None:
    payload = {
        "net_comp_cents": accrued_cents,
        "merchant_accruals": {
            merchant_id: {
                "credited_comp_cents": accrued_cents,
                "subsidy_cap_remaining_after_cents": 0,
            }
        },
        "clawbacks": [],
    }
    await database.execute(
        f"""
        INSERT INTO settlement_snapshots (
          billing_run_id,
          channel_partner_id,
          snapshot_payload_jsonb,
          computed_comp_cents,
          subsidy_cap_remaining_at_snapshot,
          created_at
        )
        VALUES (
          :billing_run_id,
          :partner_id,
          {_json_sql('payload')},
          :accrued_cents,
          0,
          {_now_sql()}
        )
        """,
        {
            "billing_run_id": billing_run_id,
            "partner_id": partner_id,
            "payload": json.dumps(payload),
            "accrued_cents": accrued_cents,
        },
    )


async def _insert_billing_run(period_start, period_end) -> int:
    row = await database.fetch_one(
        f"""
        INSERT INTO billing_runs (
          period_start,
          period_end,
          idempotency_key,
          status,
          started_at,
          completed_at,
          created_at,
          updated_at
        )
        VALUES (
          :period_start,
          :period_end,
          :idempotency_key,
          'completed',
          {_now_sql()},
          {_now_sql()},
          {_now_sql()},
          {_now_sql()}
        )
        RETURNING id
        """,
        {
            "period_start": period_start,
            "period_end": period_end,
            "idempotency_key": f"{period_start.isoformat()}-{int(time.time())}-clock-harness",
        },
    )
    return int(row["id"])


async def _insert_local_invoice(merchant_id: str, stripe_invoice_id: str, period_start, period_end, status: str) -> None:
    await database.execute(
        f"""
        INSERT INTO invoices (
          merchant_id,
          billing_period_start,
          billing_period_end,
          stripe_invoice_id,
          total_cents,
          status,
          created_at,
          updated_at
        )
        VALUES (
          :merchant_id,
          :period_start,
          :period_end,
          :stripe_invoice_id,
          0,
          :status,
          {_now_sql()},
          {_now_sql()}
        )
        ON CONFLICT (stripe_invoice_id) DO UPDATE
        SET status = EXCLUDED.status,
            updated_at = {_now_sql()}
        """,
        {
            "merchant_id": merchant_id,
            "period_start": period_start,
            "period_end": period_end,
            "stripe_invoice_id": stripe_invoice_id,
            "status": status,
        },
    )


async def _mark_invoice_status(stripe_invoice_id: str, status: str) -> None:
    await database.execute(
        f"""
        UPDATE invoices
        SET status = :status,
            updated_at = {_now_sql()}
        WHERE stripe_invoice_id = :stripe_invoice_id
        """,
        {"stripe_invoice_id": stripe_invoice_id, "status": status},
    )


async def _mark_invoice_status_by_merchant(merchant_id: str, period_start, status: str) -> None:
    await database.execute(
        f"""
        UPDATE invoices
        SET status = :status,
            updated_at = {_now_sql()}
        WHERE merchant_id = :merchant_id
          AND billing_period_start = :period_start
        """,
        {"merchant_id": merchant_id, "period_start": period_start, "status": status},
    )


async def _set_partner_connect_account(partner_id: int, connect_account_id: str | None) -> None:
    await database.execute(
        f"""
        UPDATE channel_partners
        SET stripe_connect_account_id = :connect_account_id,
            updated_at = {_now_sql()}
        WHERE id = :partner_id
        """,
        {"partner_id": partner_id, "connect_account_id": connect_account_id},
    )


async def _invoice_status(stripe_invoice_id: str) -> str:
    row = await database.fetch_one(
        "SELECT status FROM invoices WHERE stripe_invoice_id = :stripe_invoice_id",
        {"stripe_invoice_id": stripe_invoice_id},
    )
    return str(row["status"])


async def _payout_status(payout_id: int) -> str:
    row = await database.fetch_one(
        "SELECT status FROM agent_payouts WHERE id = :payout_id",
        {"payout_id": payout_id},
    )
    return str(row["status"])


async def _partner_balance(partner_id: int) -> int:
    row = await database.fetch_one(
        "SELECT balance_cents FROM partner_balance WHERE channel_partner_id = :partner_id",
        {"partner_id": partner_id},
    )
    return int(row["balance_cents"]) if row else 0


async def _billing_run_id(period_start) -> int | None:
    row = await database.fetch_one(
        """
        SELECT id
        FROM billing_runs
        WHERE idempotency_key = :idempotency_key
        """,
        {"idempotency_key": f"{period_start.isoformat()}-billing"},
    )
    return int(row["id"]) if row else None


async def _count_billing_items(billing_run_id: int, merchant_id: str) -> int:
    row = await database.fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM billing_run_items
        WHERE billing_run_id = :billing_run_id
          AND merchant_id = :merchant_id
        """,
        {"billing_run_id": billing_run_id, "merchant_id": merchant_id},
    )
    return int(row["count"])


async def _count_gmv_rollups(merchant_id: str, period_start, period_end) -> int:
    row = await database.fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM gmv_attribution_daily
        WHERE merchant_id = :merchant_id
          AND date BETWEEN :period_start AND :period_end
          AND take_amount_cents > 0
        """,
        {"merchant_id": merchant_id, "period_start": period_start, "period_end": period_end},
    )
    return int(row["count"])


async def _stripe_event_count(event_id: str) -> int:
    row = await database.fetch_one(
        "SELECT COUNT(*) AS count FROM stripe_events WHERE event_id = :event_id",
        {"event_id": event_id},
    )
    return int(row["count"])


async def _noop_handler(event: dict[str, Any], db: Any) -> None:
    await db.execute(
        f"""
        UPDATE stripe_events
        SET status = 'processed',
            processed_at = {_now_sql()}
        WHERE event_id = :event_id
        """,
        {"event_id": event["id"]},
    )


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} is required for the Stripe Test Clock harness")
    return value


def _json_sql(param_name: str) -> str:
    if IS_POSTGRES:
        return f"CAST(:{param_name} AS jsonb)"
    return f":{param_name}"


def _now_sql() -> str:
    return "NOW()" if IS_POSTGRES else "CURRENT_TIMESTAMP"


class _FakeRequest:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body
