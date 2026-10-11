"""db/agent_purchase_events.py on Postgres: schema parity with migration 265 (columns, index defs,
CHECK names and definitions), and the hand-over claim under real concurrency.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_agent_purchase_events_postgres.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL; production-dialect gate for migration 265")

_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/265_agent_purchase_events.sql"
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop agent_purchase_events in {dbname!r}: throwaway only")
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS agent_purchase_events")
    await ensure_required_schema_light()
    try:
        yield
    finally:
        try:
            await database.execute("DELETE FROM agent_purchase_events")
        except Exception:  # noqa: BLE001
            pass
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _fingerprint():
    from db.database import database

    columns = [tuple(r) for r in await database.fetch_all(
        "SELECT column_name, data_type, character_maximum_length, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'agent_purchase_events' "
        "ORDER BY column_name")]
    indexes = {r["indexname"]: " ".join(r["indexdef"].split()) for r in await database.fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'agent_purchase_events'")}
    checks = sorted((r["conname"], r["def"]) for r in await database.fetch_all(
        "SELECT c.conname, pg_get_constraintdef(c.oid) AS def FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname = 'agent_purchase_events' AND c.contype = 'c'"))
    return columns, indexes, checks


async def test_the_self_heal_builds_the_same_schema_as_migration_265():
    from db.database import database
    from db.sql_migrations import split_statements
    import db.agent_purchase_events as events_db

    await database.execute("DROP TABLE IF EXISTS agent_purchase_events")
    for statement in split_statements(_MIGRATION.read_text(encoding="utf-8")):
        await database.execute(statement)
    from_migration = await _fingerprint()
    assert [name for name, _ in from_migration[2]] == ["ck_agent_purchase_events_status", "ck_agent_purchase_events_type"]
    assert from_migration[1]["uq_agent_purchase_events_purchase_state"].startswith("CREATE UNIQUE INDEX")
    await database.execute("DROP TABLE IF EXISTS agent_purchase_events")
    await events_db.ensure_agent_purchase_events_schema()
    assert await _fingerprint() == from_migration


async def test_concurrent_workers_claim_disjoint_events_and_the_payload_round_trips():
    import db.agent_purchase_events as events_db

    for i in range(20):
        await events_db.record_event(purchase_id=f"pp_{i:024x}", agent_id="agent_pg", rail="reap",
                                     rail_state="completed", unified_state="completed",
                                     event_type="purchase.completed", payload={"n": i, "nested": {"ok": True}})
    batches = await asyncio.gather(*(events_db.claim_pending(worker=f"w{k}", limit=20) for k in range(4)))
    ids = [e["event_id"] for batch in batches for e in batch]
    assert len(ids) == 20 and len(set(ids)) == 20
    payloads = sorted((e["payload"]["n"], e["payload"]["nested"]["ok"]) for batch in batches for e in batch)
    assert payloads == [(i, True) for i in range(20)]


async def test_the_lease_and_lookback_intervals_plan_and_behave():
    from db.database import database
    import db.agent_purchase_events as events_db

    event_id = await events_db.record_event(purchase_id="pp_" + "a" * 24, agent_id="agent_pg", rail="reap",
                                            rail_state="failed", unified_state="failed",
                                            event_type="purchase.failed", payload={})
    [held] = await events_db.claim_pending(worker="w1", lease_seconds=300)
    assert await events_db.claim_pending(worker="w2", lease_seconds=300) == []
    await database.execute("UPDATE agent_purchase_events SET claimed_at = now() - interval '10 minutes'")
    [again] = await events_db.claim_pending(worker="w2", lease_seconds=300)
    assert again["event_id"] == held["event_id"] == event_id and again["attempts"] == 2
    assert await events_db.reap_candidates(lookback_hours=24) == []
