"""Retention of crawled codes (services/deal_code_retention.py) and the writer's opt-out predicate, executed on real
Postgres. Retention runs on EVERY refresh run, so its SQL must hold on the production dialect: TIMESTAMPTZ comparisons,
the correlated MAX() and EXISTS subqueries, `LIKE ... ESCAPE` for the deal_id prefix, and `'www.' || lower(...)` with a
bound host. The rules themselves: tests/test_deal_code_retention.py (SQLite).

    DATABASE_URL=postgresql://pgtest@127.0.0.1:55433/pivota_retention_dialect_check \\
        .venv/bin/python -m pytest tests/test_deal_code_retention_postgres.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")]

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_MIG_275 = Path(__file__).resolve().parent.parent / "db" / "migrations" / "275_deal_lane_tables.sql"
_TABLES = ("observed_store_offer_verifications", "observed_store_offers", "deal_lane_stops", "deal_lane_optout")
_PREFIX = "dcr-gate-"
# The gate shares ONE database across files; another file may have created catalog_products with a narrower schema.
_CATALOG_DDL = "ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS source_domain text"


@pytest.fixture
async def db(monkeypatch):
    from sqlalchemy import create_engine

    from db.catalog import catalog_products
    from db.database import database, metadata
    from db.sql_migrations import split_statements

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write deal rows in {dbname!r}; throwaway only")
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, tables=[catalog_products], checkfirst=True)
    engine.dispose()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    for stmt in split_statements(_MIG_275.read_text()):  # IF NOT EXISTS throughout
        await database.execute(stmt)
    await database.execute(_CATALOG_DDL)

    async def _clean():
        for table in _TABLES:
            await database.execute(f"DELETE FROM {table}")
        await database.execute("DELETE FROM catalog_products WHERE product_key LIKE :p", {"p": _PREFIX + "%"})

    await _clean()
    monkeypatch.delenv("DEAL_CODE_RETENTION_PAUSED", raising=False)
    try:
        yield database
    finally:
        await _clean()
        if not was_connected:
            await database.disconnect()


async def _catalog_row(db, i, *, merchant, source_domain):
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, source_domain)"
        " VALUES (:k, :m, 'shopify', :s, 't', :d)",
        {"k": f"{_PREFIX}{i}", "m": merchant, "s": str(i), "d": source_domain},
    )


async def test_every_rule_runs_on_postgres_and_deletes_exactly_what_it_names(db):
    from services import deal_code_retention as r
    from tests.test_deal_code_retention import NOW, ids, offer, verification

    await offer(db, "fresh", observed_days=13)
    await offer(db, "stale", observed_days=15)
    await offer(db, "oldfail", observed_days=1)
    await verification(db, "oldfail", "failed", updated_days=8)
    await offer(db, "recentfail", observed_days=1)
    await verification(db, "recentfail", "failed", updated_days=6)
    await offer(db, "lately", observed_days=40)
    await verification(db, "lately", "checkout_verified", expires_days=-29)
    await offer(db, "longgone", observed_days=40)
    await verification(db, "longgone", "checkout_verified", expires_days=-31)
    await offer(db, "handentered", observed_days=60, created_days=60, crawl=False)
    out = await r.run(now=NOW)
    assert "errors" not in out, out
    assert out["deleted_unverified"] == 1 and out["deleted_refused"] == 1 and out["deleted_applied_expired"] == 1
    assert await ids(db) == ["fresh", "handentered", "lately", "recentfail"]
    assert (await r.run(now=NOW)).get("errors") is None  # idempotent, still no error


async def test_bar_text_clearing_and_the_literal_prefix_on_postgres(db):
    from db.deal_lane import observed_store_offer_key, observed_store_offers as o
    from services import deal_code_retention as r
    from tests.test_deal_code_retention import NOW, offer, verification
    from datetime import timedelta

    await offer(db, "young")
    await offer(db, "checked")
    await verification(db, "checked", "unverified")
    key = observed_store_offer_key(seller_id="glow.example", market="US", code="LOOKALIKE", platform_rule_ref=None, program_id=None)
    await db.execute(o.insert().values(deal_id="dlcXlookalike", deal_key=key, seller_id="glow.example", market="US",
                                       code="LOOKALIKE", kind="percent", value=10, source="merchant_published",
                                       terms_text="bar text", observed_at=NOW - timedelta(days=60),
                                       created_at=NOW - timedelta(days=60)))
    out = await r.run(now=NOW)
    assert "errors" not in out, out
    texts = {row["deal_id"]: row["terms_text"] for row in await db.fetch_all(o.select())}
    assert texts == {"dlc_young": "Get 15% off with code X", "dlc_checked": None, "dlcXlookalike": "bar text"}


@pytest.mark.parametrize("optout_id", ["glow.example", "WWW.Glow.Example", "MERCH_OBS_GLOW"])
async def test_the_opt_out_deletes_one_sellers_rows_on_postgres(db, optout_id):
    from db.deal_lane import deal_lane_optout
    from services import deal_code_retention as r
    from tests.test_deal_code_retention import NOW, ids, offer

    await _catalog_row(db, 1, merchant="merch_obs_glow", source_domain="www.Glow.example")
    await _catalog_row(db, 2, merchant="merch_obs_other", source_domain="other.example")
    await offer(db, "glow1")
    await offer(db, "glow2")
    await offer(db, "other", seller="other.example")
    await db.execute(deal_lane_optout.insert().values(seller_id=optout_id, requested_at=NOW, recorded_by="test"))
    out = await r.run(now=NOW)
    assert "errors" not in out, out
    assert out["deleted_optout"] == 2
    assert await ids(db) == ["other"]


@pytest.mark.parametrize("optout_id,hit", [
    ("glow.example", True), ("www.glow.example", True), ("Merch_Obs_Glow", True), ("merch_obs_other", False),
    ("glow.exampl", False),
])
async def test_the_writers_opt_out_check_binds_its_host_on_postgres(db, optout_id, hit):
    from db.deal_lane import deal_lane_optout
    from services import deal_code_capture_writer as w
    from tests.test_deal_code_retention import NOW

    await _catalog_row(db, 1, merchant="merch_obs_glow", source_domain="WWW.glow.example")
    await _catalog_row(db, 2, merchant="merch_obs_other", source_domain="other.example")
    await db.execute(deal_lane_optout.insert().values(seller_id=optout_id, requested_at=NOW, recorded_by="test"))
    assert await w._opted_out("glow.example") is hit
