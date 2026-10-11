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
    assert again["event_id"] == held["event_id"] == event_id and again["attempts"] == 0
    # (the database is shared with other test files: only the rows this file made are asserted)


async def test_release_backs_off_on_a_failure_and_returns_an_untried_event_at_once():
    from db.database import database
    import db.agent_purchase_events as events_db

    event_id = await events_db.record_event(purchase_id="pp_" + "b" * 24, agent_id="agent_pg", rail="reap",
                                            rail_state="completed", unified_state="completed",
                                            event_type="purchase.completed", payload={})
    await events_db.claim_pending(worker="w1")
    assert await events_db.release(event_id, worker="w1", burn=False) == "pending"
    [again] = await events_db.claim_pending(worker="w2")  # untried: claimable at once, no attempt
    assert again["attempts"] == 0
    assert await events_db.release(event_id, worker="w2", max_attempts=2) == "pending"
    assert await events_db.claim_pending(worker="w3") == []  # failed: backed off for a lease
    await database.execute("UPDATE agent_purchase_events SET claimed_at = now() - interval '10 minutes'")
    await events_db.claim_pending(worker="w3")
    assert await events_db.release(event_id, worker="w3", max_attempts=2) == "abandoned"


async def test_the_candidate_read_skips_dead_pages_and_keys_the_window_on_state_entry():
    from db.database import database
    import db.agent_purchase_events as events_db
    import db.agent_purchase_ledger as purchases
    import db.reap_agentic_ledger as reap_ledger

    made = []

    async def purchase(state, set_sql=""):
        rp = (await reap_ledger.create_purchase(
            buyer_ref="bref_pg", agent_id="agent_pg", agent_user_ref_hash="h" * 64,
            merchant_domain="brand.example", product_key="pk_1", product_name="Serum", quantity=1,
            currency="USD", our_price_minor=4250,
        ))["id"]
        await database.execute(f"UPDATE reap_agentic_purchases SET state = :s{set_sql} WHERE id = :i",
                               {"s": state, "i": rp})
        made.append(await purchases.ensure_reap_parent(rp))
        return made[-1]

    url = ", hosted_url = 'https://pay.prava.space/a'"
    live = await purchase("awaiting_approval", url + ", hosted_url_expires_at = now() + interval '1 hour'")
    await purchase("needs_enrollment")
    await purchase("needs_enrollment", url)
    await purchase("awaiting_approval", url + ", hosted_url_expires_at = now() - interval '1 minute'")
    await purchase("awaiting_approval", url + ", hosted_url_expires_at = now() + interval '1 hour'"
                                              ", reap_quote_expires_at = now() - interval '1 minute'")
    await purchase("completed", ", state_entered_at = now() - interval '3 days', updated_at = now()")
    fresh = await purchase("completed", ", updated_at = now() - interval '3 days'")
    # Only this test's rows are judged: the dialect gate shares one database across test files.
    got = {row["id"] for row in await events_db.reap_candidates(lookback_hours=24, limit=1000)}
    assert got & set(made) == {live, fresh}


async def test_a_no_action_skip_is_rechecked_and_upgraded_in_place_and_others_are_final():
    from db.database import database
    import db.agent_purchase_events as events_db

    pp = "pp_" + "c" * 24
    kw = dict(purchase_id=pp, agent_id="agent_pg", rail="reap", rail_state="awaiting_approval",
              unified_state="awaiting_buyer_authorization", event_type="purchase.action_required")
    first = await events_db.record_event(**kw, payload={}, skip_reason="no_action")
    second = await events_db.record_event(**kw, payload={"next_action": {"url": "u"}})
    row = await database.fetch_one("SELECT * FROM agent_purchase_events WHERE purchase_id = :p", {"p": pp})
    assert first == second == row["event_id"]
    assert (row["status"], row["skip_reason"]) == ("pending", None)
    # a final skip is never overwritten
    other = "pp_" + "d" * 24
    kw2 = {**kw, "purchase_id": other}
    await events_db.record_event(**kw2, payload={}, skip_reason="not_found")
    await events_db.record_event(**kw2, payload={"x": 1})
    row = await database.fetch_one("SELECT * FROM agent_purchase_events WHERE purchase_id = :p", {"p": other})
    assert (row["status"], row["skip_reason"]) == ("skipped", "not_found")
    # the candidate read's recheck window compiles and runs on Postgres
    assert isinstance(await events_db.reap_candidates(lookback_hours=24), list)



async def test_the_heal_adds_checked_at_to_a_table_built_before_it():
    from db.database import database
    import db.agent_purchase_events as events_db

    await database.execute("ALTER TABLE agent_purchase_events DROP COLUMN IF EXISTS checked_at")
    await events_db.ensure_agent_purchase_events_schema()
    await events_db.ensure_agent_purchase_events_schema()  # idempotent
    cols = {r["column_name"] for r in await database.fetch_all(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_purchase_events'")}
    assert "checked_at" in cols
    assert isinstance(await events_db.reap_candidates(lookback_hours=24), list)
