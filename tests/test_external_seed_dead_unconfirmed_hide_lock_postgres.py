"""The unconfirmed-dead hide's re-check and hide, against a writer on ANOTHER connection, on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_external_seed_dead_unconfirmed_hide_lock_postgres.py

The re-check reads the seed row FOR UPDATE in the same transaction as the hide (be#2725 review). So:

* a live answer that arrives DURING the hide waits for the hide's commit, then lifts it;
* a live answer committed BEFORE the re-check is seen, and nothing is hidden;
* a seed row another connection holds longer than the apply step's LOCAL lock_timeout is an `errors` row, not a
  stalled run.

Another connection stands for another process (the web route, the sweep). A writer gathered on the SAME `databases`
Database would share the apply step's connection and not be blocked at all; that is why the batch runs the apply
step alone, after its workers (tests/test_external_seed_dead_unconfirmed_hide.py pins that).

Runs in its own scratch schema, alone on the search_path (the dialect gate shares one database).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL (the production-dialect gate)")

_MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"
_TABLE_MIGRATIONS = (
    "044_external_product_seeds.sql", "169_external_product_seeds_seller_ref.sql",
    "200_external_seed_destination_liveness.sql", "202_external_seed_content_freshness.sql",
    "266_external_seed_destination_corroborated_dead_at.sql", "279_external_seed_destination_dead_404_since.sql",
    "058_catalog_core.sql", "132_catalog_offer_suppression_writer_audit.sql",
    "135_catalog_product_sku_stale_suppression.sql", "134_catalog_source_quarantine.sql",
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _url() -> str:
    return "postgresql://" + DATABASE_URL[len("postgres://"):] if DATABASE_URL.startswith("postgres://") else DATABASE_URL


@pytest.fixture()
async def scoped_db():
    import databases

    from db.sql_migrations import split_statements

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")
    schema = f"unconfirmed_lock_{uuid.uuid4().hex[:8]}"
    admin = databases.Database(_url())
    await admin.connect()
    await admin.execute(f"CREATE SCHEMA {schema}")
    scoped = databases.Database(_url(), server_settings={"search_path": schema}, min_size=2, max_size=4)
    await scoped.connect()
    try:
        for name in _TABLE_MIGRATIONS:
            for statement in split_statements((_MIGRATIONS / name).read_text()):
                if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
                    continue
                await scoped.execute(statement)
        yield scoped, schema
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.disconnect()


async def _other(schema: str):
    import databases

    other = databases.Database(_url(), server_settings={"search_path": schema})
    await other.connect()
    return other


async def _setup(db, liveness):
    from services.external_offer_dual_write import MIRROR_SOURCE_SYSTEM

    await db.execute("INSERT INTO external_product_seeds (id, market, destination_url, attached_product_key,"
                     " price_currency) VALUES ('eps_x', 'US', 'https://b.example/shop/x.html', 'k|m|x', 'USD')")
    await db.execute("INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title,"
                     " source_system, source_ref) VALUES ('k|m|x', 'm', 'external_seed', 'x', 't', :s, 'eps_x')",
                     {"s": MIRROR_SOURCE_SYSTEM})
    h404 = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, None, "http_404")
    await liveness.record_destination_observation("eps_x", h404, now=NOW)
    second = await liveness.record_destination_observation("eps_x", h404, now=NOW + timedelta(hours=25))
    by_host: dict = {}
    liveness.count_unconfirmed_observation(by_host, "b.example", "eps_x", second)
    return by_host


async def _product(db):
    return await db.fetch_one("SELECT suppressed_at, suppression_reason FROM catalog_products WHERE product_key='k|m|x'")


@pytest.mark.asyncio
async def test_a_live_answer_during_the_hide_waits_for_it_then_lifts_it(scoped_db, monkeypatch):
    from services import destination_dead_suppression
    from services import external_seed_destination_liveness as liveness

    db, schema = scoped_db
    monkeypatch.setattr(liveness, "database", db)
    monkeypatch.setenv(liveness.UNCONFIRMED_HIDE_ENV, "on")
    by_host = await _setup(db, liveness)

    real_keys = liveness._mirror_product_keys
    locked = asyncio.Event()
    calls = {"n": 0}

    async def slow_keys(seed_id):
        calls["n"] += 1
        if calls["n"] == 1:  # the apply step, inside its transaction, after the FOR UPDATE re-check
            locked.set()
            await asyncio.sleep(1.5)
        return await real_keys(seed_id)

    monkeypatch.setattr(liveness, "_mirror_product_keys", slow_keys)

    async def writer():
        # The writer's statement order on its own connection: the seed UPDATE (autocommit), then the lift.
        other = await _other(schema)
        try:
            await locked.wait()
            t = time.monotonic()
            await other.execute("UPDATE external_product_seeds SET destination_verdict = 'live',"
                                " destination_http_status = 200, destination_dead_404_since = NULL WHERE id = 'eps_x'")
            waited = time.monotonic() - t
            moved = await destination_dead_suppression.lift_pending(
                ["k|m|x"], reason=liveness.UNCONFIRMED_DEAD_REASON, db=other)
        finally:
            await other.disconnect()
        return waited, len(moved["products"])

    summary, (waited, lifted) = await asyncio.gather(
        liveness.apply_unconfirmed_hides(by_host, mode="on", now=NOW + timedelta(hours=26)), writer())
    final = await _product(db)
    assert summary["totals"].get("hidden_rows") == 1
    assert waited >= 1.0, "the writer's UPDATE must wait for the hide's transaction"
    assert lifted == 1 and final["suppressed_at"] is None


@pytest.mark.asyncio
async def test_a_live_answer_committed_before_the_re_check_hides_nothing(scoped_db, monkeypatch):
    from services import external_seed_destination_liveness as liveness

    db, _schema = scoped_db
    monkeypatch.setattr(liveness, "database", db)
    monkeypatch.setenv(liveness.UNCONFIRMED_HIDE_ENV, "on")
    by_host = await _setup(db, liveness)
    await liveness.record_destination_observation(
        "eps_x", liveness.DestinationObservation(liveness.VERDICT_LIVE, 200, None),
        now=NOW + timedelta(hours=25, minutes=30))
    summary = await liveness.apply_unconfirmed_hides(by_host, mode="on", now=NOW + timedelta(hours=26))
    assert summary["totals"].get("changed") == 1 and (await _product(db))["suppressed_at"] is None


@pytest.mark.asyncio
async def test_a_seed_row_held_elsewhere_is_an_error_after_the_lock_timeout_not_a_stall(scoped_db, monkeypatch):
    from services import external_seed_destination_liveness as liveness

    db, schema = scoped_db
    monkeypatch.setattr(liveness, "database", db)
    monkeypatch.setenv(liveness.UNCONFIRMED_HIDE_ENV, "on")
    by_host = await _setup(db, liveness)
    other = await _other(schema)
    try:
        async with other.transaction():
            await other.fetch_one("SELECT id FROM external_product_seeds WHERE id = 'eps_x' FOR UPDATE")
            t = time.monotonic()
            # Bounded: without the lock timeout the re-check would wait on this very transaction forever.
            summary = await asyncio.wait_for(
                liveness.apply_unconfirmed_hides(by_host, mode="on", now=NOW + timedelta(hours=26)), timeout=10)
            took = time.monotonic() - t
    finally:
        await other.disconnect()
    assert summary["totals"].get("errors") == 1 and "hidden_rows" not in summary["totals"]
    assert 1.5 <= took < 6.0, took
    assert (await _product(db))["suppressed_at"] is None
