"""db/agent_purchase_events.py on Postgres: schema parity with migration 265 (columns, index defs,
CHECK names and definitions), and the hand-over claim under real concurrency.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_agent_purchase_events_postgres.py
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter
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


@pytest.fixture(autouse=True)
def heal_clock(monkeypatch):
    """A clock the test moves, and an empty heal retry memory (it is per process)."""
    import db.agent_purchase_ledger as purchases

    now = [1000.0]
    monkeypatch.setattr(purchases, "_clock", lambda: now[0])
    monkeypatch.setattr(purchases, "_heal_deferred_until", {})
    return now


async def _a2a_child(agent_id: str, n: int) -> str:
    import db.a2a_rail_ledger as a2a_ledger

    return (await a2a_ledger.create_purchase(
        agent_id=agent_id, agent_user_ref_hash="r" * 64, merchant_domain="brand.example",
        checkout_url=f"https://brand.example/cart/{n}:1?attributes[pivota_click_id]=c{n}",
        currency="USD", our_price_minor=4650, consent_version="v1",
    ))["id"]


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


async def test_the_a2a_candidate_read_and_the_parent_heal_plan_and_behave():
    from db.database import database
    import db.a2a_rail_ledger as a2a_ledger
    import db.agent_purchase_events as events_db
    import db.agent_purchase_ledger as purchases

    made = []

    async def purchase(state, set_sql="", parent=True):
        ap = (await a2a_ledger.create_purchase(
            agent_id="agent_pg_a2a", agent_user_ref_hash="h" * 64, merchant_domain="brand.example",
            checkout_url="https://brand.example/cart/1:1?attributes[pivota_click_id]=c1",
            currency="USD", our_price_minor=4650, consent_version="v1",
        ))["id"]
        await database.execute(f"UPDATE a2a_rail_purchases SET state = :s{set_sql} WHERE id = :i",
                               {"s": state, "i": ap})
        made.append(await purchases.ensure_a2a_parent(ap) if parent else ap)
        return made[-1]

    page = ", buyer_action_url = 'https://pay.rail.test/a'"
    live = await purchase("awaiting_authorization", page + ", buyer_action_kind = 'approval'"
                          ", buyer_action_expires_at = now() + interval '1 hour'")
    await purchase("awaiting_authorization", page + ", buyer_action_kind = 'approval'"
                   ", buyer_action_expires_at = now() - interval '1 minute'")
    await purchase("needs_payment_method", page + ", buyer_action_kind = 'approval'"
                   ", buyer_action_expires_at = now() + interval '1 hour'")
    await purchase("quoting")
    await purchase("completed", ", state_entered_at = now() - interval '3 days', updated_at = now()")
    fresh = await purchase("failed", ", updated_at = now() - interval '3 days'")
    orphan = await purchase("completed", parent=False)
    old_born = await purchase("completed", ", created_at = now() - interval '30 hours'", parent=False)
    settled = await purchase("completed", ", state_entered_at = now() - interval '3 days'", parent=False)
    inside = await purchase("completed", ", state_entered_at = now() - interval '23 hours'", parent=False)
    outside = await purchase("completed", ", state_entered_at = now() - interval '25 hours'", parent=False)
    # Only this test's rows are judged: the dialect gate shares one database across test files.
    got = {row["id"] for row in await events_db.a2a_candidates(lookback_hours=24, limit=1000)}
    assert got & set(made) == {live, fresh}
    assert await purchases.backfill_a2a_parents(limit=5000, lookback_hours=24) >= 1
    healed = await database.fetch_one(
        "SELECT id FROM agent_purchases WHERE rail = 'a2a' AND rail_purchase_id = :i", {"i": orphan})
    assert healed is not None
    got = {row["id"] for row in await events_db.a2a_candidates(lookback_hours=24, limit=1000)}
    assert healed["id"] in got
    # The heal is bounded by the detector's clock (state_entered_at), not by created_at.
    parents = {row["rail_purchase_id"] for row in await database.fetch_all(
        "SELECT rail_purchase_id FROM agent_purchases WHERE rail = 'a2a' AND rail_purchase_id IN (:a, :b, :c, :d)",
        {"a": old_born, "b": settled, "c": inside, "d": outside})}
    assert parents == {old_born, inside}  # the window is the lookback exactly


async def test_a_database_error_on_one_heal_row_does_not_abort_the_rest(monkeypatch):
    """A real Postgres error, not a Python raise: the heal runs outside any transaction (the job, the
    scheduler and detect open none), so each statement commits on its own and a failed one does not
    leave the connection in an aborted transaction for the rows after it."""
    from db.database import database
    import db.a2a_rail_ledger as a2a_ledger
    import db.agent_purchase_ledger as purchases

    async def child(n):
        ap = (await a2a_ledger.create_purchase(
            agent_id="agent_pg_heal_rows", agent_user_ref_hash="r" * 64, merchant_domain="brand.example",
            checkout_url=f"https://brand.example/cart/{n}:1?attributes[pivota_click_id]=c{n}",
            currency="USD", our_price_minor=4650, consent_version="v1",
        ))["id"]
        return ap

    bad, good = await child(1), await child(2)
    await database.execute("UPDATE a2a_rail_purchases SET state_entered_at = now() - interval '2 hours' WHERE id = :i",
                           {"i": bad})  # oldest first: the bad row is healed first
    real = purchases.ensure_a2a_parent

    async def failing_in_the_database(rail_purchase_id, **kw):
        if rail_purchase_id == bad:
            await database.execute("SELECT 1 / 0")  # division_by_zero, raised by the server
        return await real(rail_purchase_id, **kw)

    monkeypatch.setattr(purchases, "ensure_a2a_parent", failing_in_the_database)
    assert await purchases.backfill_a2a_parents(limit=5000, lookback_hours=24) >= 1
    rows = await database.fetch_all(
        "SELECT rail_purchase_id FROM agent_purchases WHERE rail = 'a2a' AND rail_purchase_id IN (:a, :b)",
        {"a": bad, "b": good})
    assert {r["rail_purchase_id"] for r in rows} == {good}
    assert await database.fetch_val("SELECT 1") == 1  # the connection is usable afterwards


async def test_the_heal_takes_the_purchase_that_entered_its_state_first():
    """The Postgres statement's own ORDER BY (the SQLite one is a separate literal). The two rows sit
    near the far edge of a 30-day window, so another test file's leftover parentless rows (the dialect
    gate shares one database) are newer and cannot take the single slot."""
    from db.database import database
    import db.agent_purchase_ledger as purchases

    born_first, entered_first = await _a2a_child("agent_pg_heal_order", 1), await _a2a_child("agent_pg_heal_order", 2)
    for ap, created, entered in ((born_first, 710, 690), (entered_first, 705, 700)):
        await database.execute(
            "UPDATE a2a_rail_purchases SET created_at = now() - (:c * interval '1 hour'), "
            "state_entered_at = now() - (:e * interval '1 hour') WHERE id = :i",
            {"c": created, "e": entered, "i": ap})
    assert await purchases.backfill_a2a_parents(limit=1, lookback_hours=24 * 30) == 1
    rows = await database.fetch_all(
        "SELECT rail_purchase_id FROM agent_purchases WHERE rail = 'a2a' AND rail_purchase_id IN (:a, :b)",
        {"a": born_first, "b": entered_first})
    assert [r["rail_purchase_id"] for r in rows] == [entered_first]


async def test_inside_a_callers_transaction_a_database_error_on_one_row_rolls_back_only_that_row(monkeypatch):
    """Without a SAVEPOINT per row, the failed statement aborts the caller's transaction: every later row
    fails with InFailedSQLTransaction, and the caller's COMMIT silently rolls back."""
    from db.database import database
    import db.agent_purchase_ledger as purchases

    bad, good = await _a2a_child("agent_pg_heal_txn", 1), await _a2a_child("agent_pg_heal_txn", 2)
    await database.execute("UPDATE a2a_rail_purchases SET state_entered_at = now() - interval '2 hours' WHERE id = :i",
                           {"i": bad})  # oldest first: the bad row is healed first
    real = purchases.ensure_a2a_parent

    async def failing_in_the_database(rail_purchase_id, **kw):
        if rail_purchase_id == bad:
            await database.execute("SELECT 1 / 0")
        return await real(rail_purchase_id, **kw)

    monkeypatch.setattr(purchases, "ensure_a2a_parent", failing_in_the_database)
    tally = Counter()
    async with database.transaction():
        assert await purchases.backfill_a2a_parents(limit=5000, lookback_hours=24, tally=tally) >= 1
        assert await database.fetch_val("SELECT 1") == 1  # the caller's transaction is still usable
    assert tally["failed"] == 1
    rows = await database.fetch_all(
        "SELECT rail_purchase_id FROM agent_purchases WHERE rail = 'a2a' AND rail_purchase_id IN (:a, :b)",
        {"a": bad, "b": good})
    assert {r["rail_purchase_id"] for r in rows} == {good}  # committed with the caller's transaction


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



async def test_the_heal_takes_no_lock_when_the_column_is_already_there():
    """A boot must not queue behind (or ahead of) the worker's transactions on this table: with
    another connection holding a lock that ACCESS EXCLUSIVE would wait on, the heal still returns
    at once, because it reads the catalog and finds nothing to do."""
    import asyncpg
    import db.agent_purchase_events as events_db

    other = await asyncpg.connect(DATABASE_URL)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute("LOCK TABLE agent_purchase_events IN ACCESS SHARE MODE")
        await asyncio.wait_for(events_db.ensure_agent_purchase_events_schema(), timeout=5)
        await tx.rollback()
    finally:
        await other.close()
