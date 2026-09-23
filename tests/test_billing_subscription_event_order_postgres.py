"""Stripe subscription webhooks delivered out of order, on real Postgres against the REAL billing schema.

Stripe does not order events, and a canceled subscription can never be reactivated. A
customer.subscription.updated describing an earlier state (active, past_due, a plan change) can be
delivered after customer.subscription.deleted. Applied blindly it puts the local row back to a live
status, and nothing later sets it back to canceled: the credit wallet (apply_subscription_allowance,
run on every balance read) and the tier reconcile both count an 'active' row as a paid plan.

checkout.session.completed can be late the same way: after the subscription it fulfils was deleted.
Fulfilling it then activates a subscription Stripe already canceled, and cancels the merchant's other
live plan in Stripe as "superseded".

Events go through the signed /webhooks/stripe/billing route, so the stripe_events claim and the
dispatcher run too.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_subscription_event_order_test \\
        .venv/bin/python -m pytest tests/test_billing_subscription_event_order_postgres.py
"""

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_TABLES = ("merchant_credit_adjustments", "merchant_credit_balance", "merchants", "user_subscriptions",
           "subscription_plans", "merchant_onboarding", "stripe_events", "credit_ledger", "credit_reservations",
           "merchant_credits")
_MIG = Path(__file__).resolve().parent.parent / "db/migrations"
_MIGRATIONS = ("100_stripe_events.sql", "101_subscription_plans.sql", "102_user_subscriptions.sql",
               "103_extend_merchants_monetization.sql", "124_subscription_plans_test_live_modes.sql",
               "089_create_merchant_credit_balance.sql",
               "091_reconcile_merchant_credit_balance_single_credit.sql",
               "141_track_direct_merchant_purchased_credits.sql", "142_direct_merchant_overage_state.sql",
               "104_merchant_credits.sql", "105_credit_reservations.sql", "106_credit_ledger.sql",
               "117_metering_service_columns.sql")

WEBHOOK_SECRET = "whsec_subscription_event_order_test"
CUSTOMER = "cus_sub_event_order"
MERCHANT = "merch_sub_event_order"
EMAIL = "owner@sub-event-order.test"
SUB = "sub_event_order_1"
SUB_OTHER = "sub_event_order_2"
PRICES = {"starter": "price_test_event_order_starter", "growth": "price_test_event_order_growth"}
ALLOWANCE = {"starter": 1000, "growth": 5000}
PURCHASED = 100  # top-up credits: survive a downgrade, so "no plan allowance" reads as credits == 100
NOW = datetime.now(timezone.utc).replace(microsecond=0)
PERIOD = (NOW - timedelta(days=1), NOW + timedelta(days=29))


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _drop_all(database):
    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


async def _build_schema(database):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from db.merchant_onboarding import merchant_onboarding
    from db.merchants import merchants
    from db.sql_migrations import split_statements

    await _drop_all(database)
    # merchants / merchant_onboarding come from the SQLAlchemy models (create_all builds them in prod).
    for table in (merchant_onboarding, merchants):
        await database.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
        for index in table.indexes:
            await database.execute(str(CreateIndex(index).compile(dialect=postgresql.dialect())))
    for name in _MIGRATIONS:
        for stmt in split_statements((_MIG / name).read_text()):
            await database.execute(stmt)
    # 222 also alters usage/statement tables this file does not build; take only its wallet statements.
    wanted = ("ALTER TABLE merchant_credit_balance", "CREATE TABLE IF NOT EXISTS merchant_credit_adjustments")
    for stmt in split_statements((_MIG / "222_fractional_merchant_credits.sql").read_text()):
        code = "\n".join(line for line in stmt.splitlines() if not line.lstrip().startswith("--")).strip()
        if code.startswith(wanted):
            await database.execute(stmt)


@pytest.fixture(autouse=True)
async def db(monkeypatch):
    from db.database import database
    from routes import billing_routes

    _assert_throwaway_database()
    monkeypatch.setattr(billing_routes.settings, "stripe_billing_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(billing_routes.settings, "stripe_secret_key", "sk_test_event_order")
    monkeypatch.setattr(billing_routes, "_billing_livemode_gate_active", lambda: False)
    monkeypatch.setattr(billing_routes, "_TABLE_COLUMN_CACHE", {})
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _build_schema(database)
    try:
        yield database
    finally:
        try:
            await _drop_all(database)  # leave nothing reduced behind for later gate files
        except Exception:  # noqa: BLE001
            pass
        if not was_connected and database.is_connected:
            await database.disconnect()


@pytest.fixture
def stripe_calls(monkeypatch):
    """Checkout fulfilment calls Stripe: it retrieves the new subscription for its period, and cancels
    the merchant's other live subscriptions as superseded. Record both instead."""
    from types import SimpleNamespace

    from routes import billing_routes

    calls = []

    def retrieve(sid):
        calls.append(("retrieve", sid))
        return {"id": sid, "items": {"data": [{"current_period_start": int(PERIOD[0].timestamp()),
                                                "current_period_end": int(PERIOD[1].timestamp())}]}}

    def cancel(sid):
        calls.append(("cancel", sid))
        return {"id": sid, "status": "canceled"}

    subscriptions = SimpleNamespace(retrieve=retrieve, cancel=cancel)
    monkeypatch.setattr(billing_routes, "stripe_client", SimpleNamespace(v1=SimpleNamespace(subscriptions=subscriptions)))
    return calls


@pytest.fixture
async def client():
    import httpx
    from fastapi import FastAPI

    from routes.billing_routes import router

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _subscription_event(event_id, event_type, *, status, plan="starter", sid=SUB, canceled_at=None):
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": int(time.time()),
        "data": {"object": {
            "id": sid,
            "object": "subscription",
            "customer": CUSTOMER,
            "status": status,
            "cancel_at_period_end": False,
            "canceled_at": int(canceled_at.timestamp()) if canceled_at else None,
            "items": {"data": [{
                "price": {"id": PRICES[plan], "product": "prod_event_order"},
                "current_period_start": int(PERIOD[0].timestamp()),
                "current_period_end": int(PERIOD[1].timestamp()),
            }]},
        }},
    }


def _checkout_completed(event_id, *, sid=SUB, plan="starter"):
    return {
        "id": event_id,
        "object": "event",
        "type": "checkout.session.completed",
        "created": int(time.time()),
        "data": {"object": {
            "id": f"cs_{event_id}",
            "object": "checkout.session",
            "mode": "subscription",
            "subscription": sid,
            "customer": CUSTOMER,
            "metadata": {"merchant_id": MERCHANT, "price_id": PRICES[plan]},
        }},
    }


def _deleted(event_id, sid=SUB, plan="starter"):
    return _subscription_event(event_id, "customer.subscription.deleted", status="canceled", plan=plan, sid=sid,
                               canceled_at=NOW)


async def _deliver(client, event, *, expect="processed"):
    payload = json.dumps(event)
    ts = int(time.time())
    sig = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    resp = await client.post("/webhooks/stripe/billing", content=payload,
                             headers={"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"})
    assert resp.status_code == 200, resp.text
    from db.database import database

    row = await database.fetch_one("SELECT status, error FROM stripe_events WHERE event_id = :e", {"e": event["id"]})
    assert row["status"] == expect, row["error"]


async def _plan_id(db, plan):
    return await db.fetch_val("SELECT id FROM subscription_plans WHERE stripe_price_id = :p", {"p": PRICES[plan]})


async def _subscribed_merchant(db, subs=((SUB, "starter"),)):
    """The state checkout fulfillment leaves: live subscription rows, a paid tier, a funded wallet.
    With subs=() it is a merchant on free that has not subscribed yet."""
    from services.merchant_credit_balance_service import apply_subscription_allowance

    for level, plan in enumerate(("starter", "growth"), start=1):
        await db.execute(
            "INSERT INTO subscription_plans (name, stripe_price_id, price_cents, tier_level, "
            "monthly_credit_allowance, status, stripe_mode) VALUES (:n, :p, 100, :l, :a, 'active', 'test')",
            {"n": plan, "p": PRICES[plan], "l": level, "a": ALLOWANCE[plan]})
    await db.execute("INSERT INTO merchant_onboarding (merchant_id, business_name, contact_email) "
                     "VALUES (:m, 'Event Order Co', :e)", {"m": MERCHANT, "e": EMAIL})
    for sid, plan in subs:
        await db.execute(
            "INSERT INTO user_subscriptions (merchant_id, plan_id, stripe_subscription_id, status, started_at, "
            "current_period_start, current_period_end) VALUES (:m, :p, :s, 'active', :ps, :ps, :pe)",
            {"m": MERCHANT, "p": await _plan_id(db, plan), "s": sid, "ps": PERIOD[0], "pe": PERIOD[1]})
    richest = max((plan for _, plan in subs), key=ALLOWANCE.__getitem__, default="free")
    local_id = await db.fetch_val("SELECT id FROM user_subscriptions ORDER BY id LIMIT 1")
    await db.execute(
        "INSERT INTO merchants (business_name, legal_name, platform, contact_email, current_tier, subscription_id, "
        "stripe_customer_id) VALUES ('Event Order Co', 'Event Order Co LLC', 'custom', :e, :t, :s, :c)",
        {"e": EMAIL, "t": richest, "s": local_id, "c": CUSTOMER})
    await db.execute("INSERT INTO merchant_credit_balance (merchant_id, credits, purchased_credits) "
                     "VALUES (:m, :c, :c)", {"m": MERCHANT, "c": PURCHASED})
    wallet = await apply_subscription_allowance(MERCHANT)
    assert (wallet["credits"], wallet["plan_tier"]) == (PURCHASED + ALLOWANCE.get(richest, 0), richest)


async def _subscription(db, sid=SUB):
    row = await db.fetch_one(
        "SELECT us.status, us.canceled_at, sp.name AS plan FROM user_subscriptions us "
        "JOIN subscription_plans sp ON sp.id = us.plan_id WHERE us.stripe_subscription_id = :s", {"s": sid})
    return dict(row)


async def _merchant(db):
    return dict(await db.fetch_one("SELECT current_tier, subscription_id FROM merchants WHERE contact_email = :e",
                                   {"e": EMAIL}))


async def _local_id(db, sid=SUB):
    return await db.fetch_val("SELECT id FROM user_subscriptions WHERE stripe_subscription_id = :s", {"s": sid})


async def _invoiced_customer(db):
    """The Stripe customer monthly GMV invoicing would bill. It joins merchants.subscription_id to
    user_subscriptions, and skips the merchant when that finds nothing."""
    from services.invoice_generation_service import _MERCHANT_STRIPE_CUSTOMER_QUERY

    row = await db.fetch_one(_MERCHANT_STRIPE_CUSTOMER_QUERY, {"merchant_id": MERCHANT})
    return row["stripe_customer_id"] if row else None


async def _fulfilment(db):
    """What checkout fulfilment writes besides the subscription row and the tier."""
    return {
        "ledger_grants": await db.fetch_val(
            "SELECT COUNT(*) FROM credit_ledger WHERE merchant_id = :m AND operation_type = 'subscription_initial_grant'",
            {"m": MERCHANT}),
        "merchant_credits": await db.fetch_val("SELECT balance FROM merchant_credits WHERE merchant_id = :m",
                                               {"m": MERCHANT}),
        "promo_period_until": await db.fetch_val("SELECT promo_period_until FROM merchants WHERE contact_email = :e",
                                                 {"e": EMAIL}),
    }


async def _wallet_on_next_read():
    """A balance read re-derives the allowance from user_subscriptions (get_balance ->
    apply_subscription_allowance), so this is what the merchant can spend after the events land."""
    from services.merchant_credit_balance_service import get_balance

    balance = await get_balance(MERCHANT)
    return balance["credits"], balance["plan_tier"]


@pytest.mark.parametrize("stale_status", ["active", "past_due", "trialing"])
async def test_a_late_subscription_updated_does_not_revive_a_deleted_subscription(db, client, stale_status):
    await _subscribed_merchant(db)
    await _deliver(client, _deleted("evt_deleted"))
    canceled = await _subscription(db)
    assert canceled["status"] == "canceled" and canceled["canceled_at"] is not None
    assert await _merchant(db) == {"current_tier": "free", "subscription_id": None}

    await _deliver(client, _subscription_event("evt_updated_late", "customer.subscription.updated",
                                               status=stale_status))

    assert await _subscription(db) == canceled
    assert await _merchant(db) == {"current_tier": "free", "subscription_id": None}
    assert await _wallet_on_next_read() == (PURCHASED, "free")


async def test_a_late_plan_change_does_not_upgrade_a_merchant_whose_subscription_was_deleted(db, client):
    """The stale event carries a different plan (an upgrade issued before the cancel). Its tier write
    and allowance refresh must not run either."""
    await _subscribed_merchant(db)
    await _deliver(client, _deleted("evt_deleted"))

    await _deliver(client, _subscription_event("evt_upgrade_late", "customer.subscription.updated",
                                               status="active", plan="growth"))

    assert (await _subscription(db))["plan"] == "starter"
    assert await _merchant(db) == {"current_tier": "free", "subscription_id": None}
    assert await _wallet_on_next_read() == (PURCHASED, "free")


async def test_a_revived_row_is_not_the_plan_a_later_cancellation_falls_back_to(db, client):
    """Two live plans. The growth one is deleted, then its stale update lands. When the starter one
    ends too, the reconcile must find nothing live and drop to free, not keep a growth plan that
    Stripe canceled."""
    await _subscribed_merchant(db, subs=((SUB, "growth"), (SUB_OTHER, "starter")))
    await _deliver(client, _deleted("evt_deleted_growth", sid=SUB, plan="growth"))
    assert (await _merchant(db))["current_tier"] == "starter"
    await _deliver(client, _subscription_event("evt_growth_updated_late", "customer.subscription.updated",
                                               status="active", plan="growth", sid=SUB))

    await _deliver(client, _deleted("evt_deleted_starter", sid=SUB_OTHER, plan="starter"))

    assert (await _subscription(db, SUB))["status"] == "canceled"
    assert await _merchant(db) == {"current_tier": "free", "subscription_id": None}
    assert await _wallet_on_next_read() == (PURCHASED, "free")


async def test_in_order_updates_still_move_a_live_subscription(db, client):
    """The guard is on 'canceled' only: past_due and back to active both land on a live row."""
    await _subscribed_merchant(db)

    await _deliver(client, _subscription_event("evt_past_due", "customer.subscription.updated",
                                               status="past_due"))
    assert (await _subscription(db))["status"] == "past_due"

    await _deliver(client, _subscription_event("evt_recovered", "customer.subscription.updated",
                                               status="active", plan="growth"))
    assert (await _subscription(db))["status"] == "active"
    assert (await _subscription(db))["plan"] == "growth"
    assert await _merchant(db) == {"current_tier": "growth", "subscription_id": await _local_id(db)}
    assert await _wallet_on_next_read() == (PURCHASED + ALLOWANCE["growth"], "growth")


async def test_an_unpaid_subscription_that_pays_goes_back_to_active(db, client):
    """'unpaid' is not terminal in Stripe: paying the open invoice reactivates the subscription."""
    await _subscribed_merchant(db)
    await _deliver(client, _subscription_event("evt_unpaid", "customer.subscription.updated", status="unpaid"))
    assert (await _subscription(db))["status"] == "unpaid"
    assert await _merchant(db) == {"current_tier": "free", "subscription_id": None}
    assert await _invoiced_customer(db) is None
    assert await _wallet_on_next_read() == (PURCHASED, "free")

    await _deliver(client, _subscription_event("evt_paid_up", "customer.subscription.updated", status="active"))

    assert (await _subscription(db))["status"] == "active"
    assert await _merchant(db) == {"current_tier": "starter", "subscription_id": await _local_id(db)}
    assert await _invoiced_customer(db) == CUSTOMER
    assert await _wallet_on_next_read() == (PURCHASED + ALLOWANCE["starter"], "starter")


async def test_an_unpaid_subscription_that_pays_on_a_new_plan_lands_on_that_plan(db, client):
    await _subscribed_merchant(db)
    await _deliver(client, _subscription_event("evt_unpaid", "customer.subscription.updated", status="unpaid"))

    await _deliver(client, _subscription_event("evt_paid_up", "customer.subscription.updated", status="trialing",
                                               plan="growth"))

    assert await _merchant(db) == {"current_tier": "growth", "subscription_id": await _local_id(db)}
    assert await _invoiced_customer(db) == CUSTOMER
    assert await _wallet_on_next_read() == (PURCHASED + ALLOWANCE["growth"], "growth")


async def test_a_recovered_plan_outranks_the_one_that_carried_the_merchant_meanwhile(db, client):
    """Growth goes unpaid while starter stays live: the merchant rides on starter, then goes back to
    growth when it is paid. Each time merchants.subscription_id follows the plan it is on."""
    await _subscribed_merchant(db, subs=((SUB, "growth"), (SUB_OTHER, "starter")))
    await _deliver(client, _subscription_event("evt_unpaid", "customer.subscription.updated", status="unpaid",
                                               plan="growth", sid=SUB))
    assert await _merchant(db) == {"current_tier": "starter", "subscription_id": await _local_id(db, SUB_OTHER)}

    await _deliver(client, _subscription_event("evt_paid_up", "customer.subscription.updated", status="active",
                                               plan="growth", sid=SUB))

    assert await _merchant(db) == {"current_tier": "growth", "subscription_id": await _local_id(db, SUB)}
    assert await _wallet_on_next_read() == (PURCHASED + ALLOWANCE["growth"], "growth")


async def test_past_due_recovering_is_not_a_tier_change(db, client):
    """Only unpaid downgraded the merchant, so only unpaid re-resolves on recovery: past_due -> active on
    the same plan leaves merchants alone (a manual tier on the row survives it)."""
    await _subscribed_merchant(db)
    await _deliver(client, _subscription_event("evt_past_due", "customer.subscription.updated", status="past_due"))
    await db.execute("UPDATE merchants SET current_tier = 'scale' WHERE contact_email = :e", {"e": EMAIL})

    await _deliver(client, _subscription_event("evt_recovered", "customer.subscription.updated", status="active"))

    assert await _merchant(db) == {"current_tier": "scale", "subscription_id": await _local_id(db)}


async def test_a_subscription_updated_to_canceled_still_ends_a_live_subscription(db, client):
    await _subscribed_merchant(db)

    await _deliver(client, _subscription_event("evt_canceled", "customer.subscription.updated",
                                               status="canceled", canceled_at=NOW))

    assert await _subscription(db) == {"status": "canceled", "canceled_at": NOW, "plan": "starter"}
    assert await _merchant(db) == {"current_tier": "free", "subscription_id": None}
    assert await _wallet_on_next_read() == (PURCHASED, "free")


async def test_a_checkout_completed_after_its_subscription_was_deleted_does_not_activate_it(db, client, stripe_calls):
    """The deletion arrives while there is no local row, so it has nothing to cancel. The checkout that
    lands afterwards must not create a live one."""
    await _subscribed_merchant(db, subs=())
    await _deliver(client, _deleted("evt_deleted"))

    await _deliver(client, _checkout_completed("evt_checkout_late"))

    subscription = await _subscription(db)
    assert (subscription["status"], subscription["plan"]) == ("canceled", "starter")
    assert subscription["canceled_at"] is not None
    assert await _merchant(db) == {"current_tier": "free", "subscription_id": None}
    assert await _fulfilment(db) == {"ledger_grants": 0, "merchant_credits": None, "promo_period_until": None}
    assert await _wallet_on_next_read() == (PURCHASED, "free")
    assert stripe_calls == []


async def test_a_late_checkout_does_not_cancel_the_plan_the_merchant_still_holds(db, client, stripe_calls):
    """Fulfilment cancels every other live subscription as superseded. For a subscription that is already
    gone that would end the merchant's real plan in Stripe."""
    await _subscribed_merchant(db, subs=((SUB_OTHER, "starter"),))
    await _deliver(client, _deleted("evt_deleted", sid=SUB, plan="growth"))

    await _deliver(client, _checkout_completed("evt_checkout_late", sid=SUB, plan="growth"))

    assert stripe_calls == []
    assert (await _subscription(db, SUB_OTHER))["status"] == "active"
    assert (await _subscription(db, SUB))["status"] == "canceled"
    assert await _merchant(db) == {"current_tier": "starter", "subscription_id": await _local_id(db, SUB_OTHER)}
    assert await _wallet_on_next_read() == (PURCHASED + ALLOWANCE["starter"], "starter")


async def test_a_checkout_retried_after_the_subscription_was_deleted_does_not_fulfil_it_again(
        db, client, stripe_calls):
    """The first delivery committed its fulfilment but failed before it was marked processed, so Stripe
    retries it; the subscription is deleted in between. The retry reaches a canceled row."""
    await _subscribed_merchant(db, subs=())
    await _deliver(client, _checkout_completed("evt_checkout"))
    await _deliver(client, _deleted("evt_deleted"))
    after_delete = await _fulfilment(db)
    await db.execute("UPDATE stripe_events SET status = 'failed' WHERE event_id = 'evt_checkout'")
    stripe_calls.clear()

    await _deliver(client, _checkout_completed("evt_checkout"))

    assert (await _subscription(db))["status"] == "canceled"
    assert await _merchant(db) == {"current_tier": "free", "subscription_id": None}
    assert await _fulfilment(db) == after_delete
    assert after_delete["ledger_grants"] == 1
    assert await _wallet_on_next_read() == (PURCHASED, "free")
    assert stripe_calls == []


async def test_a_checkout_in_order_activates_the_subscription(db, client, stripe_calls):
    await _subscribed_merchant(db, subs=())

    await _deliver(client, _checkout_completed("evt_checkout"))

    assert await _subscription(db) == {"status": "active", "canceled_at": None, "plan": "starter"}
    assert await _merchant(db) == {"current_tier": "starter", "subscription_id": await _local_id(db)}
    fulfilment = await _fulfilment(db)
    assert (fulfilment["ledger_grants"], fulfilment["merchant_credits"]) == (1, ALLOWANCE["starter"])
    assert fulfilment["promo_period_until"] is not None
    assert await _wallet_on_next_read() == (PURCHASED + ALLOWANCE["starter"], "starter")
    assert stripe_calls == [("retrieve", SUB)]


async def test_resubscribing_after_a_cancellation_activates_the_new_subscription(db, client, stripe_calls):
    """Only the deletion of THIS subscription blocks its checkout: a merchant that canceled one plan and
    bought another gets the new one."""
    await _subscribed_merchant(db, subs=((SUB_OTHER, "starter"),))
    await _deliver(client, _deleted("evt_deleted_old", sid=SUB_OTHER))

    await _deliver(client, _checkout_completed("evt_checkout_new", sid=SUB, plan="growth"))

    assert (await _subscription(db, SUB))["status"] == "active"
    assert await _merchant(db) == {"current_tier": "growth", "subscription_id": await _local_id(db, SUB)}
    assert await _wallet_on_next_read() == (PURCHASED + ALLOWANCE["growth"], "growth")
    assert stripe_calls == [("retrieve", SUB)]


async def test_the_subscription_events_around_a_checkout_do_not_block_it(db, client, stripe_calls):
    """Stripe sends customer.subscription.created and .updated around every checkout, often first. They
    are about the same subscription, and only its deletion may stop the fulfilment."""
    await _subscribed_merchant(db, subs=())
    await _deliver(client, _subscription_event("evt_created", "customer.subscription.created", status="active"),
                   expect="ignored")
    await _deliver(client, _subscription_event("evt_updated_first", "customer.subscription.updated",
                                               status="active"))

    await _deliver(client, _checkout_completed("evt_checkout"))

    assert (await _subscription(db))["status"] == "active"
    assert await _merchant(db) == {"current_tier": "starter", "subscription_id": await _local_id(db)}
