"""Agent share accrual on real Postgres, against the REAL billing schema (migrations 113, 118-122)
and migration 235: the line's row lock, the advisory lock on rates, the append-only ledger.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_share_test \\
        .venv/bin/python -m pytest tests/test_agent_share_accrual_postgres.py
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_TABLES = ("agent_share_ledger", "agent_share_rates", "billing_run_items", "invoice_disputes", "invoices",
           "billing_runs", "gmv_attribution_daily")
_MIG = Path(__file__).resolve().parent.parent / "db/migrations"
_BILLING_MIGRATIONS = ("113_billing_core.sql", "118_invoice_payment_failed_status.sql",
                       "119_invoice_finalizing_status.sql", "120_invoices_billing_period_to_date.sql",
                       "121_billing_runs_period_to_date.sql", "122_billing_runs_partial_failed_status.sql")

DAY = date(2026, 9, 10)
RATE_SET_AT = datetime(2026, 8, 1, tzinfo=timezone.utc)  # "now" when the test rates are set


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
    for name in _BILLING_MIGRATIONS + ("235_agent_share_accrual.sql",):
        for stmt in split_statements((_MIG / name).read_text()):
            await database.execute(stmt)


@pytest.fixture(autouse=True)
async def db():
    from db.database import database

    _assert_throwaway_database()
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


async def _billed_line(db, *, agent="agent_minds", amount=450, invoice_status="paid", day=DAY,
                       channel_partner=None, merchant="brand.example", tag="1"):
    """One rollup row billed on one invoice line, as generate_merchant_invoice writes them."""
    rollup_id = await db.fetch_val(
        "INSERT INTO gmv_attribution_daily (date, merchant_id, agent_id, channel_partner_id, "
        "gross_attributed_gmv_cents, net_attributed_gmv_cents, take_rate_bp, take_amount_cents) "
        "VALUES (:d, :m, :a, :cp, 4500, 4500, 1000, :amt) RETURNING id",
        {"d": day, "m": merchant, "a": agent, "cp": channel_partner, "amt": amount})
    run_id = await db.fetch_val(
        "INSERT INTO billing_runs (period_start, period_end, idempotency_key, status) "
        "VALUES (:s, :e, :k, 'completed') RETURNING id",
        {"s": date(2026, 9, 1), "e": date(2026, 9, 30), "k": "run-" + tag})
    await db.execute(
        "INSERT INTO invoices (merchant_id, billing_period_start, billing_period_end, stripe_invoice_id, "
        "total_cents, status, billing_run_id, paid_at) VALUES (:m, :s, :e, :inv, :amt, :st, :r, :paid)",
        {"m": merchant, "s": date(2026, 9, 1), "e": date(2026, 9, 30), "inv": "in_" + tag, "amt": amount,
         "st": invoice_status, "r": run_id,
         "paid": datetime.now(timezone.utc) if invoice_status == "paid" else None})
    return await db.fetch_val(
        "INSERT INTO billing_run_items (billing_run_id, merchant_id, source_type, source_id, "
        "stripe_invoice_item_id, stripe_invoice_id, amount_cents) "
        "VALUES (:r, :m, 'gmv_rollup', :g, :ii, :inv, :amt) RETURNING id",
        {"r": run_id, "m": merchant, "g": rollup_id, "ii": "ii_" + tag, "inv": "in_" + tag, "amt": amount})


async def _rate(agent="agent_minds", bp=2500, start=datetime(2026, 8, 1, tzinfo=timezone.utc), now=RATE_SET_AT):
    from services.agent_share_accrual import set_agent_share_rate

    return await set_agent_share_rate(agent_id=agent, share_bp=bp, effective_from=start, created_by="test", now=now)


async def _ledger(db, line_id):
    return [dict(r) for r in await db.fetch_all(
        "SELECT * FROM agent_share_ledger WHERE billing_run_item_id = :l ORDER BY id", {"l": line_id})]


async def _total(db, line_id):
    return sum(r["amount_minor"] for r in await _ledger(db, line_id))


async def test_a_billed_line_accrues_the_share_of_what_was_invoiced_once(db):
    from services.agent_share_accrual import accrue_for_line

    line = await _billed_line(db); rate_id = await _rate()
    r = await accrue_for_line(line)
    assert (r.status, r.basis, r.target_minor) == ("written", "billed_line", 112)
    (row,) = await _ledger(db, line)
    assert (row["entry_kind"], row["amount_minor"], row["billed_minor"], row["share_bp"]) == ("accrual", 112, 450, 2500)
    assert (row["rate_id"], row["currency"], row["agent_id"]) == (rate_id, "USD", "agent_minds")
    assert (await accrue_for_line(line)).status == "unchanged"
    assert len(await _ledger(db, line)) == 1


async def test_the_share_can_never_exceed_what_was_billed(db):
    """Review of #2273 (P1): the share is taken from the invoiced amount, which already nets the
    group's refunds and clamps at zero, never re-derived per order."""
    from services.agent_share_accrual import accrue_for_line

    line = await _billed_line(db, amount=50); await _rate(bp=10000)
    assert (await accrue_for_line(line)).target_minor == 50


async def test_a_disputed_line_is_voided_and_the_share_reverses(db):
    from services.agent_share_accrual import accrue_for_line

    line = await _billed_line(db); await _rate()
    await accrue_for_line(line)
    await db.execute("UPDATE billing_run_items SET voided_at = NOW() WHERE id = :l", {"l": line})
    r = await accrue_for_line(line)
    assert (r.basis, r.written_minor) == ("line_voided", -112)
    assert [x["entry_kind"] for x in await _ledger(db, line)] == ["accrual", "adjustment"]
    assert await _total(db, line) == 0


@pytest.mark.parametrize("status", ["void", "uncollectible", "payment_failed", "draft"])
async def test_an_invoice_that_stops_being_paid_reverses_the_share(db, status):
    from services.agent_share_accrual import accrue_for_line

    line = await _billed_line(db); await _rate()
    await accrue_for_line(line)
    await db.execute("UPDATE invoices SET status = :s", {"s": status})
    assert (await accrue_for_line(line)).basis == f"invoice_{status}"
    assert await _total(db, line) == 0


@pytest.mark.parametrize("status", ["draft", "finalizing", "finalized", "payment_failed", "failed"])
async def test_only_a_paid_invoice_carries_a_share(db, status):
    """Re-review of #2273 (P1): nothing locally marks an invoice void or uncollectible, so an unpaid
    invoice must never carry a share; it may never be collected."""
    from services.agent_share_accrual import accrue_for_line

    line = await _billed_line(db, invoice_status=status); await _rate()
    r = await accrue_for_line(line)
    assert (r.basis, r.target_minor, r.status) == (f"invoice_{status}", 0, "unchanged")


async def test_an_invoice_paid_long_after_it_was_billed_is_still_picked_up(db):
    """The sweep keys on paid_at, not only on the line's creation or invoices.updated_at (whose
    trigger prod may lack): a line billed 200 days ago and paid today must accrue."""
    from services import agent_share_accrual as svc

    line = await _billed_line(db, invoice_status="payment_failed"); await _rate()
    long_ago = datetime.now(timezone.utc) - timedelta(days=200)
    await db.execute("UPDATE billing_run_items SET created_at = :t", {"t": long_ago})
    await db.execute("ALTER TABLE invoices DISABLE TRIGGER USER")
    await db.execute("UPDATE invoices SET updated_at = :t", {"t": long_ago})
    assert (await svc.accrue_recent(120))["lines"] == 0
    await db.execute("UPDATE invoices SET status = 'paid', paid_at = NOW()")
    summary = await svc.accrue_recent(120)
    assert summary["lines"] == 1 and summary.get("written") == 1
    assert await _total(db, line) == 112


async def test_no_agent_or_the_unknown_sentinel_accrues_nothing(db):
    from services.agent_share_accrual import accrue_for_line

    a = await _billed_line(db, agent=None, tag="a"); b = await _billed_line(db, agent="unknown", tag="b")
    await _rate()
    for line in (a, b):
        r = await accrue_for_line(line)
        assert (r.status, r.basis) == ("unchanged", "no_agent")


async def test_a_channel_partner_line_accrues_nothing_until_the_split_is_decided(db):
    """Review of #2273 (P2): partner settlement already pays a share of the same take."""
    from services.agent_share_accrual import accrue_for_line

    line = await _billed_line(db, channel_partner=7); await _rate()
    r = await accrue_for_line(line)
    assert (r.basis, r.status) == ("channel_partner_row", "unchanged")


async def test_no_rate_means_no_share(db):
    from services.agent_share_accrual import accrue_for_line

    line = await _billed_line(db)
    assert (await accrue_for_line(line)).basis == "no_rate"
    assert await _ledger(db, line) == []


async def test_the_rate_in_force_at_the_start_of_the_billed_day_is_used(db):
    from services.agent_share_accrual import accrue_for_line

    line = await _billed_line(db)
    await _rate(bp=2500, start=datetime(2026, 8, 1, tzinfo=timezone.utc))
    await _rate(bp=5000, start=datetime(2026, 9, 10, 12, tzinfo=timezone.utc),
                now=datetime(2026, 9, 10, 12, tzinfo=timezone.utc))  # mid-day: applies from the next day
    assert (await accrue_for_line(line)).detail["share_bp"] == 2500


async def test_rates_are_strictly_forward(db):
    """Review of #2273 (P1): a start in the past used to be accepted if it was after the latest
    window, and the next run re-priced orders already accrued."""
    from services.agent_share_accrual import RateRefused, set_agent_share_rate

    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    first = await _rate(start=now, now=now)
    with pytest.raises(RateRefused) as e:  # AFTER the latest window, but already in the past
        await _rate(bp=5000, start=now + timedelta(hours=1), now=now + timedelta(days=1))
    assert e.value.code == "not_forward" and "in the past" in str(e.value)
    with pytest.raises(RateRefused):  # not after the latest window
        await _rate(bp=5000, start=now, now=now)
    second = await _rate(bp=5000, start=now + timedelta(days=2), now=now + timedelta(days=1))
    rows = {r["id"]: dict(r) for r in await db.fetch_all("SELECT * FROM agent_share_rates")}
    assert rows[first]["effective_to"] == now + timedelta(days=2) and rows[second]["effective_to"] is None
    for kw, code in [(dict(agent_id="unknown"), "invalid_agent_id"), (dict(share_bp=10001), "invalid_share_bp"),
                     (dict(share_bp=True), "invalid_share_bp"),
                     (dict(effective_from=datetime(2027, 1, 1)), "invalid_effective_from"),
                     (dict(created_by=" "), "created_by_required")]:
        args = dict(agent_id="agent_z", share_bp=100, effective_from=now + timedelta(days=9), created_by="t",
                    now=now)
        args.update(kw)
        with pytest.raises(RateRefused) as e:
            await set_agent_share_rate(**args)
        assert e.value.code == code


async def test_a_dry_run_writes_nothing(db):
    from services.agent_share_accrual import accrue_for_line

    line = await _billed_line(db); await _rate()
    r = await accrue_for_line(line, apply=False)
    assert (r.status, r.target_minor) == ("would_write", 112)
    assert await _ledger(db, line) == []


async def test_accrue_recent_covers_new_voided_and_credited_lines_and_isolates_failures(db, monkeypatch):
    from services import agent_share_accrual as svc

    ok = await _billed_line(db, tag="ok", merchant="m_ok")
    boom = await _billed_line(db, tag="boom", merchant="m_boom")
    unknown = await _billed_line(db, agent="unknown", tag="u", merchant="m_u")  # never picked up: no real agent
    await _rate()
    real = svc.accrue_for_line

    async def flaky(line_id, **kw):
        if line_id == boom:
            raise RuntimeError("boom")
        return await real(line_id, **kw)

    monkeypatch.setattr(svc, "accrue_for_line", flaky)
    summary = await svc.accrue_recent(30)
    assert summary["lines"] == 2 and summary["failed"] == 1 and summary.get("written") == 1
    assert await _total(db, ok) == 112 and await _ledger(db, unknown) == []


async def test_the_ledger_refuses_a_zero_entry(db):
    import asyncpg

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db.execute(
            "INSERT INTO agent_share_ledger (billing_run_item_id, rollup_id, agent_id, merchant_id, currency, "
            "entry_kind, amount_minor, billed_minor, share_bp, target_minor, basis) "
            "VALUES (1, 1, 'a', 'm', 'USD', 'accrual', 0, 0, 0, 0, 'x')")


async def test_two_accruals_of_one_line_cannot_both_write(db):
    import asyncio

    import asyncpg

    from services.agent_share_accrual import accrue_for_line

    line = await _billed_line(db); await _rate()
    other = await asyncpg.connect(DATABASE_URL)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute("SELECT 1 FROM billing_run_items WHERE id = $1 FOR UPDATE", line)
        task = asyncio.ensure_future(accrue_for_line(line))
        await asyncio.sleep(0.5)
        assert not task.done(), "the accrual did not wait for the line's row lock"
        await other.execute(
            "INSERT INTO agent_share_ledger (billing_run_item_id, rollup_id, agent_id, merchant_id, currency, "
            "entry_kind, amount_minor, billed_minor, share_bp, target_minor, basis) "
            "VALUES ($1, 1, 'agent_minds', 'brand.example', 'USD', 'accrual', 112, 450, 2500, 112, 'billed_line')",
            line)
        await tx.commit()
        r = await asyncio.wait_for(task, timeout=10)
    finally:
        await other.close()
    assert r.status == "unchanged" and await _total(db, line) == 112



async def test_a_line_without_its_invoice_row_accrues_nothing(db):
    """Items and their invoice row are written in one transaction, so this is a manual delete or a
    broken join. Unbilled either way, so not shared."""
    from services.agent_share_accrual import accrue_for_line

    line = await _billed_line(db); await _rate()
    await db.execute("DELETE FROM invoices")
    r = await accrue_for_line(line)
    assert (r.basis, r.status) == ("no_invoice", "unchanged")
