"""Stripe invoice webhooks delivered out of order, on real Postgres against the REAL billing schema.

Stripe does not order events. A failed first charge (invoice.payment_failed) can be delivered after
the retry that paid (invoice.paid). 'paid' must survive that: agent share accrual (ADR-025 D5) and
partner activation both read status='paid', and nothing later would restore it.

Events go through the signed /webhooks/stripe/billing route, so the stripe_events claim and the
dispatcher run too.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_invoice_event_order_test \\
        .venv/bin/python -m pytest tests/test_billing_invoice_event_order_postgres.py
"""

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_TABLES = ("gmv_invoice_credits", "agent_share_ledger", "agent_share_rates", "partner_settlement_completions", "settlement_snapshots",
           "billing_run_items",
           "invoice_disputes", "invoices",
           "billing_runs", "gmv_attribution_daily", "stripe_events")
_MIG = Path(__file__).resolve().parent.parent / "db/migrations"
_MIGRATIONS = ("100_stripe_events.sql", "113_billing_core.sql", "118_invoice_payment_failed_status.sql",
               "119_invoice_finalizing_status.sql", "120_invoices_billing_period_to_date.sql",
               "121_billing_runs_period_to_date.sql", "122_billing_runs_partial_failed_status.sql",
               "235_agent_share_accrual.sql", "236_agent_share_after_partner.sql",
               "238_gmv_invoice_credits.sql")

WEBHOOK_SECRET = "whsec_invoice_event_order_test"
MERCHANT = "merch_event_order"
INVOICE = "in_event_order_1"
AMOUNT = 450
PAID_AT = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)  # pinned, so "intact" means byte-equal


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _drop_all(database):
    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


async def _build_schema(database):
    from db.sql_migrations import split_statements

    await _drop_all(database)
    ddl = (_MIG / "110_gmv_attribution_daily.sql").read_text().replace(
        "REFERENCES channel_partners(id) ON DELETE SET NULL", "")
    for stmt in split_statements(ddl):
        await database.execute(stmt)
    for name in _MIGRATIONS:
        for stmt in split_statements((_MIG / name).read_text()):
            await database.execute(stmt)
    # Agent share accrual reads a completed run's settlement snapshots (#2275). channel_partners
    # itself is not under test.
    ddl = (_MIG / "114_settlement_snapshots.sql").read_text().replace(
        "REFERENCES channel_partners(id) ON DELETE RESTRICT", "")
    for stmt in split_statements(ddl):
        await database.execute(stmt)


@pytest.fixture(autouse=True)
async def db(monkeypatch):
    from db.database import database
    from routes import billing_routes

    _assert_throwaway_database()

    async def _no_activation(*, merchant_id):
        return []

    # Partner activation is best-effort and reads tables this file does not build.
    monkeypatch.setattr(billing_routes, "activate_brands_for_merchant", _no_activation)
    monkeypatch.setattr(billing_routes.settings, "stripe_billing_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(billing_routes, "_billing_livemode_gate_active", lambda: False)
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
async def client():
    import httpx
    from fastapi import FastAPI

    from routes.billing_routes import router

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _invoice_event(event_id, event_type, *, invoice=INVOICE, amount=AMOUNT):
    paid = event_type == "invoice.paid"
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": int(time.time()),
        "data": {"object": {
            "id": invoice,
            "object": "invoice",
            "customer": "cus_event_order",
            "metadata": {"merchant_id": MERCHANT},
            "amount_paid": amount if paid else 0,
            "amount_due": amount,
            "period_start": int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()),
            "period_end": int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()),
            "status_transitions": {"finalized_at": int(datetime(2026, 9, 1, 1, tzinfo=timezone.utc).timestamp())},
        }},
    }


async def _deliver(client, event):
    payload = json.dumps(event)
    ts = int(time.time())
    sig = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    resp = await client.post("/webhooks/stripe/billing", content=payload,
                             headers={"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"})
    assert resp.status_code == 200, resp.text
    row = await _event_row(event["id"])
    assert row["status"] == "processed", row["error"]


async def _event_row(event_id):
    from db.database import database

    return await database.fetch_one("SELECT status, error FROM stripe_events WHERE event_id = :e", {"e": event_id})


async def _invoice(db, invoice=INVOICE):
    rows = await db.fetch_all(
        "SELECT status, paid_at, total_cents FROM invoices WHERE stripe_invoice_id = :i", {"i": invoice})
    assert len(rows) == 1
    return dict(rows[0])


async def _billed_invoice(db, *, status="finalized", invoice=INVOICE):
    """The local mirror row generate_merchant_invoice writes, with one billed agent line on it."""
    rollup_id = await db.fetch_val(
        "INSERT INTO gmv_attribution_daily (date, merchant_id, agent_id, gross_attributed_gmv_cents, "
        "net_attributed_gmv_cents, take_rate_bp, take_amount_cents) "
        "VALUES (:d, :m, 'agent_minds', 4500, 4500, 1000, :amt) RETURNING id",
        {"d": date(2026, 9, 10), "m": MERCHANT, "amt": AMOUNT})
    run_id = await db.fetch_val(
        "INSERT INTO billing_runs (period_start, period_end, idempotency_key, status) "
        "VALUES (:s, :e, 'run-event-order', 'completed') RETURNING id",
        {"s": date(2026, 9, 1), "e": date(2026, 9, 30)})
    await db.execute(
        "INSERT INTO invoices (merchant_id, billing_period_start, billing_period_end, stripe_invoice_id, "
        "total_cents, status, billing_run_id) VALUES (:m, :s, :e, :inv, :amt, :st, :r)",
        {"m": MERCHANT, "s": date(2026, 9, 1), "e": date(2026, 9, 30), "inv": invoice, "amt": AMOUNT,
         "st": status, "r": run_id})
    return await db.fetch_val(
        "INSERT INTO billing_run_items (billing_run_id, merchant_id, source_type, source_id, "
        "stripe_invoice_item_id, stripe_invoice_id, amount_cents) "
        "VALUES (:r, :m, 'gmv_rollup', :g, 'ii_event_order', :inv, :amt) RETURNING id",
        {"r": run_id, "m": MERCHANT, "g": rollup_id, "inv": invoice, "amt": AMOUNT})


async def _pin_paid_at(db, invoice=INVOICE):
    await db.execute("UPDATE invoices SET paid_at = :t WHERE stripe_invoice_id = :i", {"t": PAID_AT, "i": invoice})


async def test_a_late_payment_failed_does_not_downgrade_a_paid_invoice(db, client):
    await _billed_invoice(db)
    await _deliver(client, _invoice_event("evt_paid", "invoice.paid"))
    await _pin_paid_at(db)

    await _deliver(client, _invoice_event("evt_failed_late", "invoice.payment_failed"))

    assert await _invoice(db) == {"status": "paid", "paid_at": PAID_AT, "total_cents": AMOUNT}


async def test_a_late_payment_failed_does_not_downgrade_a_paid_invoice_the_webhook_inserted(db, client):
    """No billing-run row: invoice.paid inserts through _insert_minimal_invoice, and the late failure
    reaches the same upsert and conflicts on it. The ON CONFLICT branch must carry the guard too."""
    await _deliver(client, _invoice_event("evt_paid", "invoice.paid"))
    await _pin_paid_at(db)

    await _deliver(client, _invoice_event("evt_failed_late", "invoice.payment_failed"))

    assert await _invoice(db) == {"status": "paid", "paid_at": PAID_AT, "total_cents": AMOUNT}


async def test_in_order_a_failed_attempt_then_the_paying_retry_ends_paid(db, client):
    """The guard is on 'paid' only: a failure still lands on an unpaid invoice, and paid still wins."""
    await _billed_invoice(db)
    await _deliver(client, _invoice_event("evt_failed", "invoice.payment_failed"))
    # The failure records what was billed, not amount_paid (0 on an unpaid invoice).
    assert await _invoice(db) == {"status": "payment_failed", "paid_at": None, "total_cents": AMOUNT}

    await _deliver(client, _invoice_event("evt_paid", "invoice.paid"))
    after_paid = await _invoice(db)
    assert (after_paid["status"], after_paid["total_cents"]) == ("paid", AMOUNT)
    assert after_paid["paid_at"] is not None


async def test_a_payment_failed_on_an_unknown_invoice_still_inserts_it(db, client):
    await _deliver(client, _invoice_event("evt_failed", "invoice.payment_failed"))
    assert await _invoice(db) == {"status": "payment_failed", "paid_at": None, "total_cents": AMOUNT}


async def test_a_replayed_invoice_paid_does_not_move_paid_at(db, client):
    await _billed_invoice(db)
    await _deliver(client, _invoice_event("evt_paid", "invoice.paid"))
    await _pin_paid_at(db)

    await _deliver(client, _invoice_event("evt_paid_again", "invoice.paid"))

    assert await _invoice(db) == {"status": "paid", "paid_at": PAID_AT, "total_cents": AMOUNT}


async def test_a_late_payment_failed_does_not_reverse_an_earned_agent_share(db, client):
    from services.agent_share_accrual import accrue_for_line, set_agent_share_rate

    line = await _billed_invoice(db)
    # An agent's share for a run accrues once that run's partner settlement has completed (#2275);
    # this run settled with no partner paid.
    await db.execute(
        "INSERT INTO partner_settlement_completions (billing_run_id, partner_ids, engine) "
        "SELECT billing_run_id, '[]'::jsonb, 'v2' FROM billing_run_items WHERE id = :l", {"l": line})
    await set_agent_share_rate(agent_id="agent_minds", share_bp=2500, created_by="test",
                               effective_from=datetime(2026, 8, 1, tzinfo=timezone.utc),
                               now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    await _deliver(client, _invoice_event("evt_paid", "invoice.paid"))
    assert (await accrue_for_line(line)).written_minor == 112

    await _deliver(client, _invoice_event("evt_failed_late", "invoice.payment_failed"))

    r = await accrue_for_line(line)
    assert (r.status, r.basis) == ("unchanged", "billed_line")
    total = await db.fetch_val(
        "SELECT COALESCE(SUM(amount_minor), 0) FROM agent_share_ledger WHERE billing_run_item_id = :l", {"l": line})
    assert total == 112


@pytest.mark.parametrize(("status", "expected"), [
    ("paid", "paid"), ("payment_failed", "payment_failed"), ("void", "void"),
    ("draft", "finalizing"), ("finalizing", "finalizing"),
])
async def test_finalize_invoice_only_moves_a_not_yet_finalized_invoice(db, monkeypatch, status, expected):
    """auto_advance can finalize and charge the invoice before finalize_invoice runs, so its local
    'finalizing' write can land after a webhook. It must only move draft (or a re-run's finalizing)."""
    from services import invoice_generation_service as svc

    await _billed_invoice(db, status=status)
    if status == "paid":
        await _pin_paid_at(db)
    finalize_calls = []
    fake_invoices = SimpleNamespace(finalize_invoice=lambda inv, params: finalize_calls.append(inv))
    monkeypatch.setattr(svc, "stripe_client", SimpleNamespace(v1=SimpleNamespace(invoices=fake_invoices)))
    monkeypatch.setattr(svc, "_SCHEMA_GUARD_ATTEMPTED", True)

    await svc.finalize_invoice(INVOICE)

    row = await _invoice(db)
    assert row["status"] == expected
    assert row["paid_at"] == (PAID_AT if status == "paid" else None)
    assert finalize_calls == [INVOICE]
