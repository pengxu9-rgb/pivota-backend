"""record_funnel_link against REAL Postgres foreign keys.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml` globs
`tests/test_*_postgres.py`).

agent_decision_funnel_links (migration 140) is the edge from a paid order's funnel event back to the
search decision that produced it, and five of its optional columns are foreign keys. Order metadata
can carry a catalog_offer_id that never joins catalog_offers (the "of:catalog_offer:" prefix, a
synthetic "of:internal_checkout:..." id, an offer since deleted) or a content_key that is not in
agent_pdp_view. One such value used to fail the INSERT, and the flush worker dropped the whole link.
Only a real server enforces the FKs, so a fake cannot show it.

Runs in its own scratch schema (migration 140's tables plus minimal referenced tables), so the
shared dialect-gate database is never touched:

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_decision_funnel_link_postgres.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/140_decision_layer_event_store.sql"
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"decision_funnel_link_test_{os.getpid()}"
CK = "ck_" + "0123456789abcdef" * 2
OFFER = "offer_known_1"
EDGE = "edge_known_1"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _async_url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@pytest.fixture()
async def scoped(monkeypatch):
    import databases

    import services.agent_decision_event_store as store

    _assert_throwaway_database()
    admin = databases.Database(_async_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    db = databases.Database(_async_url(), server_settings={"search_path": _SCHEMA})
    await db.connect()
    reports: list = []
    try:
        # The tables the funnel-link FKs point at, reduced to their keys.
        await db.execute("CREATE TABLE funnel_events (event_id UUID PRIMARY KEY)")
        await db.execute("CREATE TABLE agent_pdp_view (content_key VARCHAR(40) PRIMARY KEY)")
        await db.execute("CREATE TABLE catalog_offers (offer_id VARCHAR(255) PRIMARY KEY)")
        await db.execute("CREATE TABLE commerce_attribution_edges (edge_id VARCHAR(64) PRIMARY KEY)")
        sql = _MIGRATION.read_text()
        for name in ("agent_decision_events", "agent_exposure_events", "checkout_decisions",
                     "agent_decision_funnel_links"):
            await db.execute(re.search(r"CREATE TABLE IF NOT EXISTS " + name + r" \(.*?\);", sql, re.S).group(0))
        # the funnel-link insert's ON CONFLICT (funnel_event_id) target
        await db.execute(re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_decision_funnel_links_event.*?;", sql, re.S).group(0))
        for name in ("agent_decision_events", "checkout_decisions", "agent_decision_funnel_links"):
            # migration 145
            await db.execute(f"ALTER TABLE {name} ADD COLUMN protocol VARCHAR(32) NOT NULL DEFAULT 'pdp_direct'")
        await db.execute("INSERT INTO agent_decision_events (decision_id, surface) VALUES ('dec', 's')")
        await db.execute("INSERT INTO agent_pdp_view (content_key) VALUES (:ck)", {"ck": CK})
        await db.execute("INSERT INTO catalog_offers (offer_id) VALUES (:o)", {"o": OFFER})
        await db.execute("INSERT INTO commerce_attribution_edges (edge_id) VALUES (:e)", {"e": EDGE})
        monkeypatch.setattr(store, "database", db)
        monkeypatch.setattr(store, "_report", lambda exc, **kw: reports.append((exc, kw)))
        # a fresh queue + worker bound to this test's loop and database
        monkeypatch.setattr(store, "_QUEUE", None)
        monkeypatch.setattr(store, "_WORKER", None)
        monkeypatch.setattr(store, "_QUEUE_LOOP", None)
        db.reports = reports
        yield db
    finally:
        if store._WORKER is not None:
            store._WORKER.cancel()
        await db.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


async def _funnel_event(db) -> str:
    event_id = str(uuid.uuid4())
    await db.execute("INSERT INTO funnel_events (event_id) VALUES (:e)", {"e": event_id})
    return event_id


async def _read_link(db, event_id: str):
    return await db.fetch_one(
        "SELECT decision_id, content_key, catalog_offer_id, commerce_attribution_edge_id, exposure_id, "
        "checkout_decision_id FROM agent_decision_funnel_links WHERE funnel_event_id = :e", {"e": event_id},
    )


async def _link_through_the_worker(db, *, event_id: str = "", decision_id="dec", **kwargs):
    """record_funnel_link -> the store's queue -> its flush worker, then read the row back."""
    import services.agent_decision_event_store as store

    event_id = event_id or await _funnel_event(db)
    await store.record_funnel_link(funnel_event_id=event_id, decision_id=decision_id, merchant_id="m1", **kwargs)
    if store._QUEUE is not None:
        await store._QUEUE.join()
    return await _read_link(db, event_id)


def _write_stage_nulls(caplog) -> list:
    return [json.loads(r.getMessage()) for r in caplog.records
            if "funnel_link_refs_nulled" in r.getMessage() and '"stage": "write"' in r.getMessage()]


@pytest.mark.asyncio
async def test_a_link_with_known_refs_keeps_them(scoped) -> None:
    row = await _link_through_the_worker(
        scoped, content_key=CK, catalog_offer_id=OFFER, commerce_attribution_edge_id=EDGE,
    )
    assert row is not None
    assert (row["decision_id"], row["content_key"], row["catalog_offer_id"], row["commerce_attribution_edge_id"]) == (
        "dec", CK, OFFER, EDGE,
    )
    assert scoped.reports == []


@pytest.mark.asyncio
async def test_a_prefixed_catalog_offer_id_is_stripped_and_joins(scoped) -> None:
    row = await _link_through_the_worker(scoped, catalog_offer_id=f"of:catalog_offer:{OFFER}", content_key=CK)
    assert row is not None
    assert (row["catalog_offer_id"], row["content_key"]) == (OFFER, CK)


@pytest.mark.parametrize("offer_id", ["of:internal_checkout:merch_1:sku_9", "offer_deleted_since", "of:catalog_offer:gone"])
@pytest.mark.asyncio
async def test_an_offer_id_that_is_not_in_catalog_offers_is_nulled_and_the_link_lands(scoped, offer_id, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="services.agent_decision_event_store"):
        row = await _link_through_the_worker(scoped, catalog_offer_id=offer_id, content_key=CK,
                                             commerce_attribution_edge_id=EDGE)
    assert row is not None, "the whole funnel link was dropped"
    assert row["catalog_offer_id"] is None
    assert (row["decision_id"], row["content_key"], row["commerce_attribution_edge_id"]) == ("dec", CK, EDGE)
    nulls = _write_stage_nulls(caplog)
    if offer_id.startswith("of:internal_checkout:"):
        assert nulls == [], "a synthetic offer id is nulled before the insert, not by a failed one"
    else:
        [line] = nulls
        assert line["columns"] == ["catalog_offer_id"] and line["link_id"]


@pytest.mark.asyncio
async def test_an_over_long_content_key_is_nulled_before_the_insert_and_the_link_lands(scoped) -> None:
    # content_key is VARCHAR(40): sent through, it is a DataError (not an FK miss) and drops the link
    row = await _link_through_the_worker(scoped, content_key="x" * 64, catalog_offer_id=OFFER)
    assert row is not None, "the whole funnel link was dropped"
    assert (row["content_key"], row["catalog_offer_id"]) == (None, OFFER)
    assert scoped.reports == []


@pytest.mark.parametrize("content_key", ["prod_key_that_is_not_a_content_key", "ck_" + "f" * 32])
@pytest.mark.asyncio
async def test_a_content_key_that_is_not_in_the_view_is_nulled_and_the_link_lands(scoped, content_key) -> None:
    row = await _link_through_the_worker(scoped, content_key=content_key, catalog_offer_id=OFFER)
    assert row is not None, "the whole funnel link was dropped"
    assert row["content_key"] is None
    assert row["catalog_offer_id"] == OFFER


@pytest.mark.asyncio
async def test_every_unknown_never_joining_ref_at_once_is_nulled_and_the_link_lands(scoped) -> None:
    row = await _link_through_the_worker(
        scoped, content_key="ck_" + "e" * 32, catalog_offer_id="offer_deleted_since",
        commerce_attribution_edge_id="edge_unknown",
    )
    assert row is not None, "the whole funnel link was dropped"
    assert row["decision_id"] == "dec"
    assert [row[c] for c in ("content_key", "catalog_offer_id", "commerce_attribution_edge_id")] == [None] * 3
    assert scoped.reports == []


@pytest.mark.parametrize("column", ["checkout_decision_id", "exposure_id"])
@pytest.mark.asyncio
async def test_a_miss_on_a_queued_parent_drops_the_link_and_reports_it(scoped, column) -> None:
    row = await _link_through_the_worker(scoped, catalog_offer_id=OFFER, **{column: str(uuid.uuid4())})
    assert row is None, f"a link with {column} nulled for good was written"
    assert len(scoped.reports) == 1


@pytest.mark.asyncio
async def test_a_checkout_decision_written_after_the_link_still_lands_whole_on_the_resend(scoped) -> None:
    # The checkout row is still in another instance's queue when confirm-payment flushes the link.
    # The link is dropped (not written with checkout_decision_id nulled, which ON CONFLICT DO NOTHING
    # would make permanent); once the checkout row exists, the re-sent link lands with it.
    checkout_id = str(uuid.uuid4())
    event_id = await _funnel_event(scoped)
    assert await _link_through_the_worker(scoped, event_id=event_id, checkout_decision_id=checkout_id,
                                          catalog_offer_id=OFFER) is None
    await scoped.execute(
        "INSERT INTO checkout_decisions (checkout_decision_id, decision_id) VALUES (:c, 'dec')", {"c": checkout_id},
    )
    row = await _link_through_the_worker(scoped, event_id=event_id, checkout_decision_id=checkout_id,
                                         catalog_offer_id=OFFER)
    assert row is not None
    assert (str(row["checkout_decision_id"]), row["decision_id"], row["catalog_offer_id"]) == (checkout_id, "dec", OFFER)


@pytest.mark.asyncio
async def test_no_decision_and_a_checkout_miss_writes_no_row(scoped) -> None:
    # the confirm-payment caller has no linkage gate: an all-NULL link would count as an outcome
    row = await _link_through_the_worker(scoped, decision_id=None, checkout_decision_id=str(uuid.uuid4()),
                                         catalog_offer_id="of:internal_checkout:x")
    assert row is None
    assert await scoped.fetch_val("SELECT count(*) FROM agent_decision_funnel_links") == 0


@pytest.mark.asyncio
async def test_no_decision_and_no_checkout_decision_writes_no_row_and_reports_nothing(scoped) -> None:
    row = await _link_through_the_worker(scoped, decision_id=None, catalog_offer_id=OFFER, content_key=CK)
    assert row is None
    assert scoped.reports == []


@pytest.mark.asyncio
async def test_an_unknown_decision_or_funnel_event_still_fails_the_link(scoped) -> None:
    # decision_id is ON DELETE RESTRICT and funnel_event_id is NOT NULL: the link is about them,
    # so a link that cannot name them is not written with them blanked out.
    import services.agent_decision_event_store as store

    event_id = await _funnel_event(scoped)
    await store.record_funnel_link(funnel_event_id=event_id, decision_id="no-such-decision", catalog_offer_id=OFFER)
    await store.record_funnel_link(funnel_event_id=str(uuid.uuid4()), decision_id="dec", catalog_offer_id=OFFER)
    await store._QUEUE.join()
    assert await scoped.fetch_val("SELECT count(*) FROM agent_decision_funnel_links") == 0
    assert len(scoped.reports) == 2
