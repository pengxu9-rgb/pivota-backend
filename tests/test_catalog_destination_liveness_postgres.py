"""Production-dialect gate for catalog destination liveness (migration 261) and its writer.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_catalog_destination_liveness_postgres.py

What only real Postgres proves: the migration applies twice (the job applies it on every run);
its CHECK refuses an unknown verdict; the upsert's CASE guards keep a verdict when "we could not
look"; the candidate query's regex, NULLS FIRST and dead-first ordering; and the full
hide -> lift -> hide -> withdraw cycle on catalog_products / catalog_offers.

THE GATE SHARES ONE DATABASE ACROSS FILES. catalog_products and catalog_offers come from the
models via tests/model_schema.ensure_model_tables (never a hand-written narrow table), are never
dropped, and this file deletes only its own rows, by prefix.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — this is the production-dialect gate",
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
NOW = datetime(2026, 10, 10, 4, 10, tzinfo=timezone.utc)
P = "cdl::"  # every row this file writes starts with it


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write test rows into {dbname!r}; use a throwaway such as pivota_dialect_check")


async def _clean(db) -> None:
    await db.execute("DELETE FROM catalog_offers WHERE offer_id LIKE :p", {"p": P + "%"})
    await db.execute("DELETE FROM catalog_products WHERE product_key LIKE :p", {"p": P + "%"})
    await db.execute("DELETE FROM catalog_destination_liveness WHERE product_key LIKE :p", {"p": P + "%"})


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from services import catalog_destination_liveness as catalog

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # The catalog core from the MODELS (tests/model_schema): a hand-written narrow table here would
    # poison every later file in the shared gate database.
    from db.catalog import catalog_offers, catalog_products
    from tests.model_schema import ensure_model_tables

    await ensure_model_tables([catalog_products, catalog_offers])
    # The DDL UNDER TEST, applied the way the job applies it — twice, because it runs every night.
    await catalog.ensure_table(db=database)
    await catalog.ensure_table(db=database)
    await _clean(database)
    yield database
    await _clean(database)
    if not was_connected:
        await database.disconnect()


async def _product(db, key, *, url=None, system="catalog_enrichment_agent_v1", reason=None, offer=True):
    url = url or f"https://shop.example/products/{key.split('::')[-1]}"
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, "
        "source_system, catalog_track, canonical_url, suppressed_at, suppression_reason) VALUES "
        "(:k, 'merch_obs_cdl', 'external_seed', :k, 'T', :sys, 'external_referral', :url, :at, :reason)",
        {"k": P + key, "sys": system, "url": url, "at": NOW if reason else None, "reason": reason},
    )
    if offer:
        await db.execute(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, suppressed_at, suppression_reason) "
            "VALUES (:id, :sku, :k, 'merch_obs_cdl', :at, :reason)",
            {"id": P + "offer::" + key, "sku": P + key + "::sku", "k": P + key,
             "at": NOW if reason else None, "reason": reason},
        )


async def _row(db, table, key_col, key):
    row = await db.fetch_one(f"SELECT * FROM {table} WHERE {key_col} = :k", {"k": key})
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_the_check_refuses_an_unknown_verdict(_db):
    import asyncpg

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _db.execute(
            "INSERT INTO catalog_destination_liveness (product_key, canonical_url, destination_verdict) "
            "VALUES (:k, 'https://x/products/y', 'dead')", {"k": P + "bad"})


@pytest.mark.asyncio
async def test_the_queue_takes_only_this_lanes_live_or_pending_rows_dead_first(_db):
    from services import catalog_destination_liveness as catalog

    await _product(_db, "never")
    await _product(_db, "old_live")
    await _product(_db, "dead_recent")
    await _product(_db, "pending", reason=catalog.PENDING_SUPPRESSION_REASON)
    await _product(_db, "withdrawn", reason=catalog.SUPPRESSION_REASON)
    await _product(_db, "other_lane", system="external_product_seeds_mirror_v1")
    await _product(_db, "no_offer", offer=False)
    await _product(_db, "collection", url="https://shop.example/collections/all")
    for key, verdict, attempted in (
        ("old_live", "live", NOW - timedelta(days=9)),
        ("dead_recent", "dead_404", NOW - timedelta(hours=1)),
        ("pending", "dead_404", NOW - timedelta(hours=2)),
    ):
        await _db.execute(
            "INSERT INTO catalog_destination_liveness (product_key, canonical_url, destination_verdict, last_attempt_at) "
            "VALUES (:k, 'https://shop.example/products/x', :v, :t)", {"k": P + key, "v": verdict, "t": attempted})

    got = [r["product_key"] for r in await catalog.get_candidates(50) if r["product_key"].startswith(P)]
    assert got == [P + "pending", P + "dead_recent", P + "never", P + "old_live"]


@pytest.mark.asyncio
async def test_hide_lift_hide_withdraw_on_real_postgres(_db):
    from services import catalog_destination_liveness as catalog
    from services import external_seed_destination_liveness as liveness

    key = P + "cycle"
    url = "https://shop.example/products/cycle"
    await _product(_db, "cycle", url=url)
    dead = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, url, corroborated=True)
    live = liveness.DestinationObservation(liveness.VERDICT_LIVE, None, None, "listed")
    blind = liveness.DestinationObservation(liveness.VERDICT_UNVERIFIABLE, None, None, "catalogue bot_challenge")

    first = await catalog.record_catalog_observation(key, url, dead, now=NOW, suppress=True)
    assert first["pending_suppressed"] == 1
    assert (await _row(_db, "catalog_products", "product_key", key))["suppression_reason"] == catalog.PENDING_SUPPRESSION_REASON
    assert (await _row(_db, "catalog_offers", "offer_id", P + "offer::cycle"))["suppression_reason"] == catalog.PENDING_SUPPRESSION_REASON

    # the host goes dark: only the attempt clock moves, the verdict and the hiding stand
    await catalog.record_catalog_observation(key, url, blind, now=NOW + timedelta(hours=6), suppress=True)
    state = await _row(_db, "catalog_destination_liveness", "product_key", key)
    assert state["destination_verdict"] == "dead_404" and state["destination_failure_streak"] == 1
    assert state["last_attempt_at"] == NOW + timedelta(hours=6) and state["destination_checked_at"] == NOW

    lifted = await catalog.record_catalog_observation(key, url, live, now=NOW + timedelta(hours=12), suppress=False)
    assert lifted["pending_lifted"] == 1
    assert (await _row(_db, "catalog_products", "product_key", key))["suppressed_at"] is None
    assert (await _row(_db, "catalog_offers", "offer_id", P + "offer::cycle"))["suppressed_at"] is None

    await catalog.record_catalog_observation(key, url, dead, now=NOW + timedelta(days=1), suppress=True)
    final = await catalog.record_catalog_observation(key, url, dead, now=NOW + timedelta(days=2), suppress=True)
    assert final["retire"] is True and final["withdrawn"] == 1
    product = await _row(_db, "catalog_products", "product_key", key)
    assert product["suppression_reason"] == catalog.SUPPRESSION_REASON
    offer = await _row(_db, "catalog_offers", "offer_id", P + "offer::cycle")
    assert offer["suppressed_at"] is not None and offer["suppression_reason"] == "product_suppressed"
    # withdrawn rows leave the queue
    assert key not in [r["product_key"] for r in await catalog.get_candidates(50)]


@pytest.mark.asyncio
async def test_a_lift_never_clears_another_lanes_suppression(_db):
    from services import catalog_destination_liveness as catalog
    from services import external_seed_destination_liveness as liveness

    key = P + "theirs"
    url = "https://shop.example/products/theirs"
    await _product(_db, "theirs", url=url, reason="identity_merge_loser")
    await _db.execute(
        "INSERT INTO catalog_destination_liveness (product_key, canonical_url, destination_verdict, "
        "destination_failure_streak, destination_corroborated_dead_at) VALUES (:k, :u, 'dead_404', 1, :t)",
        {"k": key, "u": url, "t": NOW - timedelta(hours=5)})
    await catalog.record_catalog_observation(
        key, url, liveness.DestinationObservation(liveness.VERDICT_LIVE, None, None, "listed"), now=NOW)
    assert (await _row(_db, "catalog_products", "product_key", key))["suppression_reason"] == "identity_merge_loser"
