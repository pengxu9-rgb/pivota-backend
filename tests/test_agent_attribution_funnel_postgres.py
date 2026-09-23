"""The funnel's SQL against the real Postgres schema.

The unit tests prove the fold; this proves the queries PLAN and JOIN on the production dialect:
JSONB path operators on commerce_attribution_edges.metadata, the purchase-id join, FILTER
clauses, and the column probe that adapts to a prod missing a numbered migration.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_funnel_test \\
        .venv/bin/python -m pytest tests/test_agent_attribution_funnel_postgres.py
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

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db/migrations"
_PURCHASE_MIGRATIONS = (
    "224_reap_agentic_ledger.sql",
    "225_reap_agentic_purchase_hints.sql",
    "229_reap_agentic_purchase_item_source.sql",
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_TABLES = ("reap_agentic_purchases", "reap_agentic_enrollments",
           "commerce_attribution_edges", "surface_click_events")


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _build_schema(with_purchases=True):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from db.commerce_attribution import commerce_attribution_edges, surface_click_events
    from db.database import database
    from db.sql_migrations import split_statements

    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for table in (surface_click_events, commerce_attribution_edges):
        await database.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
    if with_purchases:
        for name in _PURCHASE_MIGRATIONS:
            for stmt in split_statements((_MIGRATIONS_DIR / name).read_text(encoding="utf-8")):
                await database.execute(stmt)


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    try:
        yield database
    finally:
        # DROP, not DELETE. This file rebuilds these tables in a reduced shape (no migration-only
        # indexes, a test that drops item_source). Leaving that behind would hand later gate files
        # a table that `metadata.create_all(checkfirst=True)` skips, so their ON CONFLICT targets
        # would be missing (review of #2268). Absent tables are rebuilt whole by whoever needs them.
        for table in _TABLES:
            try:
                await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            except Exception:  # noqa: BLE001 -- best-effort cleanup must not mask the test result
                continue
        if not was_connected and database.is_connected:
            await database.disconnect()


NOW = datetime.now(timezone.utc)


async def _purchase(db, pid, agent, state, *, order=None, final=None, currency="USD", source="reap_variant"):
    await db.execute(
        "INSERT INTO reap_agentic_purchases (id, buyer_ref, agent_id, state, merchant_domain, "
        "currency, reap_order_id, final_total_minor, item_source, cart_url, created_at, terminal_at) "
        "VALUES (:id, 'buyer_x', :agent, :state, 'brand.example', :cur, :ord, :fin, :src, :cart, :now, :now)",
        {"id": pid, "agent": agent, "state": state, "cur": currency, "ord": order,
         "fin": final, "src": source, "now": NOW,
         # migration 229 pairs a cart_link purchase with its cart URL
         "cart": "https://brand.example/cart/1:1" if source == "cart_link" else None},
    )


async def _edge(db, edge_id, agent, *, purchase_id=None, partner=True, state="converted", gmv=4500,
                refunds=0, created=None):
    meta = {"agent_source": "partner_purchase" if agent else ""}
    if purchase_id:
        meta["partner_provenance"] = {"partner_reported": partner, "purchase_id": purchase_id}
    await db.execute(
        "INSERT INTO commerce_attribution_edges (edge_id, merchant_id, order_id, agent_id, state, "
        "gross_attributed_gmv_cents, currency, refund_count, refunded_amount, metadata, created_at, "
        "updated_at, source) VALUES (:eid, 'brand.example', :oid, :agent, :state, :gmv, 'USD', :rc, 0, "
        "CAST(:meta AS JSONB), :created, :created, 'external_redirect')",
        {"eid": edge_id, "oid": "ext_" + edge_id, "agent": agent, "state": state, "gmv": gmv,
         "rc": refunds, "meta": json.dumps(meta), "created": created or NOW},
    )


async def test_the_real_queries_fold_a_realistic_ledger(_db):
    from scripts import agent_attribution_funnel as f

    await _build_schema()
    db = _db
    await db.execute(
        "INSERT INTO surface_click_events (click_id, surface, agent_id, impression_count, click_count, "
        "created_at, updated_at) "
        "VALUES ('clk1', 'reap_cart_link', 'agent_minds', 0, 1, :now, :now), "
        "('clk2', 'offers.resolve', NULL, 1, 0, :now, :now)",
        {"now": NOW},
    )
    await _purchase(db, "rp_ok", "agent_minds", "completed", order="o1", final=4500)
    await _purchase(db, "rp_lost", "agent_minds", "completed", order="o2", final=3000)
    await _purchase(db, "rp_noord", "agent_minds", "completed", order=None, final=3000)
    await _purchase(db, "rp_wait", "agent_minds", "awaiting_approval", source="cart_link")
    await _purchase(db, "rp_other", "agent_b", "completed", order="o5", final=1000)
    # Closed by another path first: an edge for the order exists, but not with our purchase id.
    await _purchase(db, "rp_foreign", "agent_minds", "completed", order="o7", final=2000)
    await db.execute(
        "INSERT INTO commerce_attribution_edges (edge_id, merchant_id, order_id, external_order_id, "
        "state, refund_count, refunded_amount, metadata, created_at, updated_at, source) "
        "VALUES ('e_foreign', 'brand.example', 'ext_foreign', 'o7', 'converted', 0, 0, "
        "'{}'::jsonb, :now, :now, 'external_redirect')",
        {"now": NOW},
    )
    await _edge(db, "e_ok", "agent_minds", purchase_id="rp_ok", refunds=1)
    await _edge(db, "e_noagent", None, purchase_id="rp_other", gmv=1000)
    # Outside the window: must not count.
    await _edge(db, "e_old", "agent_minds", purchase_id="rp_ok_old",
                created=NOW - timedelta(days=90))

    rows = await f.collect(30)
    assert rows["_errors"] == {}
    fn = f.build_funnel(rows, 30)
    by = {a["agent"]: a for a in fn["agents"]}

    m = by["agent_minds"]
    assert (m["issued"], m["clicked"]) == (1, 1)
    assert (m["opened"], m["completed"], m["in_flight"]) == (5, 4, 1)
    assert m["lanes"] == {"reap_variant": 4, "cart_link": 1}
    assert (m["credited"], m["credited_partner"], m["refunded_edges"]) == (1, 1, 1)
    assert m["credited_minor"] == {"USD": 4500}
    assert by[f.NO_AGENT]["issued"] == 1

    ex = fn["exceptions"]
    assert ex["completed_without_edge"]["by_reason"] == {
        "missing_edge": 1, "no_order_id": 1, "edge_without_provenance": 1}
    assert {r["purchase_id"] for r in ex["completed_without_edge"]["rows"]} == {
        "rp_lost", "rp_noord", "rp_foreign"}
    assert [r["edge_id"] for r in ex["partner_edge_without_agent"]["rows"]] == ["e_noagent"]
    assert ex["partner_edge_without_agent"]["rows"][0]["purchase_agent"] == "agent_b"
    assert ex["agent_mismatch"]["count"] == 0
    assert ex["orphan_edge"]["count"] == 0
    assert fn["totals"]["credited_partner_to_agent"] == 1


async def test_an_agent_mismatch_is_caught_by_the_join(_db):
    from scripts import agent_attribution_funnel as f

    await _build_schema()
    await _purchase(_db, "rp_1", "agent_minds", "completed", order="o1", final=4500)
    await _edge(_db, "e_1", "agent_x", purchase_id="rp_1")
    fn = f.build_funnel(await f.collect(30), 30)
    assert [(r["edge_agent"], r["purchase_agent"]) for r in fn["exceptions"]["agent_mismatch"]["rows"]] == [
        ("agent_x", "agent_minds")
    ]


async def test_a_prod_without_the_purchases_table_still_reports(_db):
    from scripts import agent_attribution_funnel as f

    await _build_schema(with_purchases=False)
    rows = await f.collect(30)
    assert rows["_errors"] == {}
    assert "purchases" not in rows
    assert "partner lane is not measured" in f.render(f.build_funnel(rows, 30))


async def test_a_prod_missing_item_source_still_reports_the_partner_lane(_db):
    from scripts import agent_attribution_funnel as f

    await _build_schema()
    await _db.execute("ALTER TABLE reap_agentic_purchases DROP COLUMN item_source")
    await _purchase_without_source(_db)
    rows = await f.collect(30)
    assert rows["_errors"] == {}
    assert rows["purchases"][0]["item_source"] == "reap_variant"


async def _purchase_without_source(db):
    await db.execute(
        "INSERT INTO reap_agentic_purchases (id, buyer_ref, agent_id, state, created_at) "
        "VALUES ('rp_s', 'buyer_x', 'agent_minds', 'resolving', :now)",
        {"now": NOW},
    )


async def test_the_unknown_sentinel_is_reported_as_no_agent(_db):
    """traffic_taxonomy_service writes agent_id='unknown' when there is no agent. It names nobody,
    so it must count as no agent in every lane (review of #2269)."""
    from scripts import agent_attribution_funnel as f

    await _build_schema()
    await _db.execute(
        "INSERT INTO surface_click_events (click_id, surface, agent_id, impression_count, click_count, "
        "created_at, updated_at) VALUES ('clk_u', 'offers.resolve', 'unknown', 0, 1, :now, :now)",
        {"now": NOW},
    )
    await _purchase(_db, "rp_u", "agent_minds", "completed", order="o1", final=4500)
    await _edge(_db, "e_u", "unknown", purchase_id="rp_u")
    fn = f.build_funnel(await f.collect(30), 30)
    agents = {a["agent"] for a in fn["agents"]}
    assert "unknown" not in agents
    assert next(a for a in fn["agents"] if a["agent"] == f.NO_AGENT)["issued"] == 1
    assert fn["totals"]["credited_partner_to_agent"] == 0
    assert [r["edge_id"] for r in fn["exceptions"]["partner_edge_without_agent"]["rows"]] == ["e_u"]
