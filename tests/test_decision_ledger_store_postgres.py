"""The decision-ledger store's batch writer against REAL Postgres.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml` globs
`tests/test_*_postgres.py`).

What only a real server shows: which asyncpg error class a bad row, a foreign-key miss and a killed
backend actually raise, and that `databases` 0.7 checks out a fresh connection after one dies.
A fake cannot tell these apart, and the order of the except clauses in `_write_many` depends on them.

Runs in its own scratch schema (migration 140's three slate tables, plus the protocol column and
minimal catalog tables), so the shared dialect-gate database is never touched:

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_decision_ledger_store_postgres.py
"""

from __future__ import annotations

import asyncio
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
_SCHEMA = f"decision_ledger_store_test_{os.getpid()}"
_APP = f"decision_ledger_test_{os.getpid()}"
CK = "ck_" + "0123456789abcdef" * 2


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
    db = databases.Database(_async_url(), server_settings={"search_path": _SCHEMA, "application_name": _APP})
    await db.connect()
    try:
        sql = _MIGRATION.read_text()
        for name in ("agent_decision_events", "agent_decision_candidates", "agent_exposure_events"):
            await db.execute(re.search(r"CREATE TABLE IF NOT EXISTS " + name + r" \(.*?\);", sql, re.S).group(0))
        await db.execute(
            "ALTER TABLE agent_decision_events ADD COLUMN IF NOT EXISTS protocol VARCHAR(32) NOT NULL DEFAULT 'pdp_direct'"
        )
        await db.execute("INSERT INTO agent_decision_events (decision_id, surface) VALUES ('dec', 's')")
        monkeypatch.setattr(store, "database", db)
        monkeypatch.setattr(store, "_report", lambda exc, **kw: None)
        yield db
    finally:
        await db.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


def _row(i: int, **over):
    row = {"exposure_id": str(uuid.uuid4()), "decision_id": "dec", "content_key": CK,
           "catalog_offer_id": None, "exposure_position": i, "slot": "search_result"}
    row.update(over)
    return row


async def _landed(db) -> int:
    return await db.fetch_val("SELECT count(*) FROM agent_exposure_events")


@pytest.mark.asyncio
async def test_an_overflowing_row_is_a_data_error_and_the_other_rows_land(scoped) -> None:
    import services.agent_decision_event_store as store

    rows = [_row(i, content_key=("x" * 64 if i == 3 else CK)) for i in range(10)]
    assert await store._write_many("exposures", {"decision_id": "dec", "rows": rows}) == 1
    assert await _landed(scoped) == 9


@pytest.mark.asyncio
async def test_a_foreign_key_miss_is_row_level_and_the_other_rows_land(scoped) -> None:
    import services.agent_decision_event_store as store

    rows = [_row(i, decision_id=("no-such-decision" if i == 1 else "dec")) for i in range(5)]
    assert await store._write_many("exposures", {"decision_id": "dec", "rows": rows}) == 1
    assert await _landed(scoped) == 4


@pytest.mark.asyncio
async def test_a_backend_killed_mid_batch_is_resent_on_a_fresh_connection(scoped) -> None:
    # Without the one resend, the 5 or so rows inserted before the kill were all that landed.
    import databases

    import services.agent_decision_event_store as store

    await scoped.execute(
        "CREATE OR REPLACE FUNCTION slow_insert() RETURNS trigger AS $$ "
        "BEGIN PERFORM pg_sleep(0.05); RETURN NEW; END $$ LANGUAGE plpgsql"
    )
    await scoped.execute(
        "CREATE TRIGGER slow_insert BEFORE INSERT ON agent_exposure_events FOR EACH ROW EXECUTE FUNCTION slow_insert()"
    )
    rows = [_row(i) for i in range(20)]
    writer = asyncio.ensure_future(store._write_many("exposures", {"decision_id": "dec", "rows": rows}))

    killer = databases.Database(_async_url())
    await killer.connect()
    try:
        killed = 0
        for _ in range(100):  # wait until the batch is part-way in, then kill its backend
            await asyncio.sleep(0.05)
            # poll on the killer's own connection: the writer task may share the test's
            if await killer.fetch_val(f"SELECT count(*) FROM {_SCHEMA}.agent_exposure_events") >= 3:
                killed = await killer.fetch_val(
                    "SELECT count(*) FROM (SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE application_name = :app AND query LIKE 'INSERT INTO agent_exposure_events%' "
                    "AND pid <> pg_backend_pid()) t",
                    {"app": _APP},
                )
                break
    finally:
        await killer.disconnect()

    lost = await asyncio.wait_for(writer, 30)
    assert killed == 1
    assert lost == 0
    assert await _landed(scoped) == 20
