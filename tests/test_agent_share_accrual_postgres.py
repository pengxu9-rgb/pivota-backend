"""Agent share accrual on real Postgres: row lock, advisory lock, JSONB, the append-only ledger,
the billed-rate lookup, and migration 235's DDL.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_share_test \\
        .venv/bin/python -m pytest tests/test_agent_share_accrual_postgres.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_TABLES = ("agent_share_ledger", "agent_share_rates", "gmv_attribution_daily", "commerce_attribution_edges")
_MIG = Path(__file__).resolve().parent.parent / "db/migrations"

T_CONV = datetime(2026, 9, 10, 15, tzinfo=timezone.utc)


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _build_schema(database):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.commerce_attribution import commerce_attribution_edges
    from db.sql_migrations import split_statements

    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await database.execute(str(CreateTable(commerce_attribution_edges).compile(dialect=postgresql.dialect())))
    await database.execute(
        "ALTER TABLE commerce_attribution_edges ADD COLUMN channel_partner_id BIGINT, "
        "ADD COLUMN refund_amount_cents BIGINT NOT NULL DEFAULT 0, ADD COLUMN refunded_at TIMESTAMPTZ"
    )
    ddl = (_MIG / "110_gmv_attribution_daily.sql").read_text().replace(
        "REFERENCES channel_partners(id) ON DELETE SET NULL", "")
    for stmt in split_statements(ddl):
        await database.execute(stmt)
    # The reviewed migration itself, not the model: this is what proves 235 is valid DDL.
    for stmt in split_statements((_MIG / "235_agent_share_accrual.sql").read_text()):
        await database.execute(stmt)


@pytest.fixture(autouse=True)
async def db(monkeypatch):
    from db.database import database
    from services import agent_share_accrual as svc

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    billable = {"brand.example"}

    async def _billable(merchant_id):
        # The real check is the invoice run's own Stripe-customer query; the merchants and
        # user_subscriptions tables it reads are not rebuilt here.
        return merchant_id in billable

    monkeypatch.setattr(svc, "_merchant_is_billable", _billable)
    await _build_schema(database)
    database.billable = billable
    try:
        yield database
    finally:
        for table in _TABLES:
            try:
                await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            except Exception:  # noqa: BLE001
                continue
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _edge(db, edge_id="e1", agent="agent_minds", gross=4500, refund=0, state="converted",
                merchant="brand.example", metadata=None, created=T_CONV):
    await db.execute(
        "INSERT INTO commerce_attribution_edges (edge_id, merchant_id, order_id, agent_id, state, "
        "gross_attributed_gmv_cents, currency, refund_count, refunded_amount, refund_amount_cents, "
        "metadata, created_at, updated_at, converted_at, source) VALUES (:e, :m, :o, :a, :s, :g, 'USD', "
        "0, 0, :r, CAST(:meta AS JSONB), :c, :c, :c, 'external_redirect')",
        {"e": edge_id, "m": merchant, "o": "ext_" + edge_id, "a": agent, "s": state, "g": gross,
         "r": refund, "meta": json.dumps(metadata or {}), "c": created},
    )


async def _rollup(db, agent="agent_minds", bp=1000, merchant="brand.example", day=T_CONV.date()):
    await db.execute(
        "INSERT INTO gmv_attribution_daily (date, merchant_id, agent_id, take_rate_bp) "
        "VALUES (:d, :m, :a, :bp)", {"d": day, "m": merchant, "a": agent, "bp": bp})


async def _rate(agent="agent_minds", bp=2500, start=T_CONV - timedelta(days=30)):
    from services.agent_share_accrual import set_agent_share_rate

    return await set_agent_share_rate(agent_id=agent, share_bp=bp, effective_from=start, created_by="test")


async def _ledger(db, edge_id="e1"):
    return [dict(r) for r in await db.fetch_all(
        "SELECT * FROM agent_share_ledger WHERE edge_id = :e ORDER BY id", {"e": edge_id})]


async def test_an_order_accrues_the_agent_s_share_of_pivota_s_commission_once(db):
    from services.agent_share_accrual import accrue_for_edge

    await _edge(db); await _rollup(db); rate_id = await _rate()
    r = await accrue_for_edge("e1")
    assert (r.status, r.target_minor, r.commission_source) == ("written", 112, "billed_take")
    (row,) = await _ledger(db)
    assert (row["entry_kind"], row["amount_minor"], row["target_minor"]) == ("accrual", 112, 112)
    assert (row["net_gmv_minor"], row["take_rate_bp"], row["commission_minor"], row["share_bp"]) == (4500, 1000, 450, 2500)
    assert row["rate_id"] == rate_id and row["agent_id"] == "agent_minds" and row["currency"] == "USD"
    assert (await accrue_for_edge("e1")).status == "unchanged"
    assert len(await _ledger(db)) == 1


async def test_a_refund_writes_a_negative_adjustment(db):
    from services.agent_share_accrual import accrue_for_edge

    await _edge(db); await _rollup(db); await _rate()
    await accrue_for_edge("e1")
    await db.execute("UPDATE commerce_attribution_edges SET refund_amount_cents = 1500")
    r = await accrue_for_edge("e1")
    assert (r.target_minor, r.written_minor) == (75, -37)
    rows = await _ledger(db)
    assert [(x["entry_kind"], x["amount_minor"]) for x in rows] == [("accrual", 112), ("adjustment", -37)]
    assert sum(x["amount_minor"] for x in rows) == 75


async def test_no_rate_means_no_share(db):
    from services.agent_share_accrual import accrue_for_edge

    await _edge(db); await _rollup(db)
    assert (await accrue_for_edge("e1")).status == "unchanged"
    assert await _ledger(db) == []


async def test_the_rate_in_force_when_the_order_converted_is_the_one_used(db):
    from services.agent_share_accrual import accrue_for_edge

    await _edge(db); await _rollup(db)
    await _rate(bp=2500, start=T_CONV - timedelta(days=30))
    await _rate(bp=5000, start=T_CONV + timedelta(days=1))  # a later raise does not re-price this order
    assert (await accrue_for_edge("e1")).target_minor == 112


async def test_a_rate_that_starts_after_the_order_gives_nothing(db):
    from services.agent_share_accrual import accrue_for_edge

    await _edge(db); await _rollup(db)
    await _rate(start=T_CONV + timedelta(seconds=1))
    assert (await accrue_for_edge("e1")).status == "unchanged"


async def test_an_unbillable_merchant_earns_pivota_nothing_so_the_agent_gets_nothing(db):
    from services.agent_share_accrual import accrue_for_edge

    await _edge(db); await _rollup(db); await _rate()
    await accrue_for_edge("e1")
    db.billable.clear()  # e.g. the merchant's billing link was removed
    r = await accrue_for_edge("e1")
    assert (r.commission_source, r.target_minor, r.written_minor) == ("merchant_not_billable", 0, -112)
    assert sum(x["amount_minor"] for x in await _ledger(db)) == 0


async def test_an_unrolled_day_waits(db):
    from services.agent_share_accrual import accrue_for_edge

    await _edge(db); await _rate()
    assert (await accrue_for_edge("e1")).status == "rollup_pending"
    assert await _ledger(db) == []


@pytest.mark.parametrize("over", [dict(agent="unknown"), dict(agent=None), dict(state="started"),
                                  dict(metadata={"inferred": True}), dict(gross=0)])
async def test_ineligible_edges_accrue_nothing(db, over):
    from services.agent_share_accrual import accrue_for_edge

    await _edge(db, **over); await _rollup(db, agent=over.get("agent", "agent_minds")); await _rate()
    r = await accrue_for_edge("e1")
    assert r.status == "unchanged" and await _ledger(db) == []


async def test_a_reassigned_edge_moves_the_share_to_the_new_agent(db):
    from services.agent_share_accrual import accrue_for_edge

    await _edge(db); await _rollup(db); await _rate()
    await accrue_for_edge("e1")
    await db.execute("UPDATE commerce_attribution_edges SET agent_id = 'agent_b'")
    await _rollup(db, agent="agent_b"); await _rate(agent="agent_b", bp=5000)
    await accrue_for_edge("e1")
    totals = {r["agent_id"]: r["t"] for r in (dict(x) for x in await db.fetch_all(
        "SELECT agent_id, SUM(amount_minor) t FROM agent_share_ledger GROUP BY agent_id"))}
    assert totals == {"agent_minds": 0, "agent_b": 225}


async def test_a_dry_run_writes_nothing(db):
    from services.agent_share_accrual import accrue_for_edge

    await _edge(db); await _rollup(db); await _rate()
    r = await accrue_for_edge("e1", apply=False)
    assert (r.status, r.target_minor) == ("would_write", 112)
    assert await _ledger(db) == []


async def test_accrue_recent_covers_touched_edges_and_isolates_a_failure(db, monkeypatch):
    from services import agent_share_accrual as svc

    now = datetime.now(timezone.utc)
    await _edge(db, edge_id="e1", created=now); await _rollup(db, day=now.date())
    await _edge(db, edge_id="e_old", created=now - timedelta(days=200))
    await _rate(start=now - timedelta(days=1))
    real = svc.accrue_for_edge

    async def flaky(edge_id, **kw):
        if edge_id == "e_boom":
            raise RuntimeError("boom")
        return await real(edge_id, **kw)

    await _edge(db, edge_id="e_boom", created=now)
    monkeypatch.setattr(svc, "accrue_for_edge", flaky)
    summary = await svc.accrue_recent(30)
    assert summary["edges"] == 2 and summary["failed"] == 1 and summary.get("written") == 1


async def test_rates_are_forward_only_and_close_the_open_window(db):
    from services.agent_share_accrual import RateRefused, set_agent_share_rate

    first = await _rate(start=T_CONV)
    second = await _rate(bp=3000, start=T_CONV + timedelta(days=5))
    rows = {r["id"]: dict(r) for r in await db.fetch_all("SELECT * FROM agent_share_rates")}
    assert rows[first]["effective_to"] == T_CONV + timedelta(days=5)
    assert rows[second]["effective_to"] is None
    with pytest.raises(RateRefused) as e:
        await _rate(start=T_CONV + timedelta(days=2))
    assert e.value.code == "not_forward"
    for kw, code in [(dict(agent_id="unknown"), "invalid_agent_id"), (dict(share_bp=10001), "invalid_share_bp"),
                     (dict(share_bp=True), "invalid_share_bp"),
                     (dict(effective_from=datetime(2027, 1, 1)), "invalid_effective_from"),
                     (dict(created_by=" "), "created_by_required")]:
        args = dict(agent_id="agent_z", share_bp=100, effective_from=T_CONV, created_by="t")
        args.update(kw)
        with pytest.raises(RateRefused) as e:
            await set_agent_share_rate(**args)
        assert e.value.code == code


async def test_the_ledger_refuses_a_zero_entry(db):
    import asyncpg

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db.execute(
            "INSERT INTO agent_share_ledger (edge_id, agent_id, merchant_id, currency, entry_kind, amount_minor, "
            "net_gmv_minor, take_rate_bp, commission_minor, share_bp, target_minor, commission_source) "
            "VALUES ('e', 'a', 'm', 'USD', 'accrual', 0, 0, 0, 0, 0, 0, 'x')")


async def test_two_accruals_of_one_edge_cannot_both_write(db):
    """A second connection holds the edge's row lock and writes the full target. The accrual must
    wait and then find nothing left to write."""
    import asyncio

    import asyncpg

    from services.agent_share_accrual import accrue_for_edge

    await _edge(db); await _rollup(db); await _rate()
    other = await asyncpg.connect(DATABASE_URL)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute("SELECT 1 FROM commerce_attribution_edges WHERE edge_id = 'e1' FOR UPDATE")
        task = asyncio.ensure_future(accrue_for_edge("e1"))
        await asyncio.sleep(0.5)
        assert not task.done(), "the accrual did not wait for the edge's row lock"
        await other.execute(
            "INSERT INTO agent_share_ledger (edge_id, agent_id, merchant_id, currency, entry_kind, amount_minor, "
            "net_gmv_minor, take_rate_bp, commission_minor, share_bp, target_minor, commission_source) "
            "VALUES ('e1', 'agent_minds', 'brand.example', 'USD', 'accrual', 112, 4500, 1000, 450, 2500, 112, 'billed_take')")
        await tx.commit()
        r = await asyncio.wait_for(task, timeout=10)
    finally:
        await other.close()
    assert r.status == "unchanged"
    assert sum(x["amount_minor"] for x in await _ledger(db)) == 112
