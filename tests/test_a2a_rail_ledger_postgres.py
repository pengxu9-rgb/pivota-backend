"""db/a2a_rail_ledger.py on Postgres: schema parity with migration 268, the parent's widened rail
CHECK, every statement with NULL and timestamp parameters (Postgres types them; SQLite does not),
and the claim under real concurrency.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_a2a_rail_ledger_postgres.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL; production-dialect gate for migration 268")

_REPO = Path(__file__).resolve().parent.parent
_MIGRATION = _REPO / "db/migrations/268_a2a_rail_ledger.sql"
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_TABLES = ("a2a_rail_purchases", "a2a_rail_buyer_refs")
AGENT = "agent_a2a_pg"
PAGE = "https://pay.rail.test/approve/1"


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop the a2a rail tables in {dbname!r}: throwaway only")
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    # This file is the only one that writes a2a parents; clear them so a reused database is clean.
    await database.execute("DELETE FROM agent_purchases WHERE rail = 'a2a'")
    try:
        yield
    finally:
        for statement in ("DELETE FROM agent_purchases WHERE rail = 'a2a'",
                          *(f"DELETE FROM {table}" for table in _TABLES)):
            try:
                await database.execute(statement)
            except Exception:  # noqa: BLE001
                pass
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _fingerprint(table):
    from db.database import database

    columns = [tuple(r) for r in await database.fetch_all(
        "SELECT column_name, data_type, character_maximum_length, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t "
        "ORDER BY column_name", {"t": table})]
    indexes = {r["indexname"]: " ".join(r["indexdef"].split()) for r in await database.fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t", {"t": table})}
    checks = sorted((r["conname"], r["def"]) for r in await database.fetch_all(
        "SELECT c.conname, pg_get_constraintdef(c.oid) AS def FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname = :t AND c.contype = 'c'", {"t": table}))
    return columns, indexes, checks


async def _rail_check():
    from db.database import database

    row = await database.fetch_one(
        "SELECT pg_get_constraintdef(c.oid) AS def FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname = 'agent_purchases' AND c.conname = 'ck_agent_purchases_rail'")
    return row["def"] if row else None


async def test_the_self_heal_builds_the_same_schema_as_migration_268():
    from db.database import database
    from db.sql_migrations import split_statements
    import db.a2a_rail_ledger as ledger

    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    for statement in split_statements(_MIGRATION.read_text(encoding="utf-8")):
        await database.execute(statement)
    from_migration = [await _fingerprint(t) for t in _TABLES]
    assert [name for name, _ in from_migration[0][2]] == [
        "ck_a2a_rail_purchases_action", "ck_a2a_rail_purchases_price",
        "ck_a2a_rail_purchases_quantity", "ck_a2a_rail_purchases_state"]
    migrated_check = await _rail_check()
    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ledger.ensure_a2a_rail_schema()
    assert [await _fingerprint(t) for t in _TABLES] == from_migration
    assert await _rail_check() == migrated_check


async def test_the_parent_heal_widens_an_old_rail_check_and_keeps_rows():
    from db.database import database
    import db.agent_purchase_ledger as purchases

    await database.execute("DELETE FROM agent_purchases WHERE id IN ('pp_pre268', 'pp_post268')")
    await database.execute("ALTER TABLE agent_purchases DROP CONSTRAINT IF EXISTS ck_agent_purchases_rail")
    await database.execute(
        "ALTER TABLE agent_purchases ADD CONSTRAINT ck_agent_purchases_rail CHECK (rail IN ('reap'))")
    await database.execute(
        "INSERT INTO agent_purchases (id, rail, executor, rail_purchase_id) VALUES ('pp_pre268', 'reap', 'rail_managed', 'rp_pre268')")
    await purchases.ensure_agent_purchase_schema()
    assert await _rail_check() == purchases._RAIL_CHECK_PG_DEF
    await database.execute(
        "INSERT INTO agent_purchases (id, rail, executor, rail_purchase_id) VALUES ('pp_post268', 'a2a', 'rail_managed', 'ap_post268')")
    ids = {r["id"] for r in await database.fetch_all("SELECT id FROM agent_purchases WHERE id IN ('pp_pre268', 'pp_post268')")}
    assert ids == {"pp_pre268", "pp_post268"}
    await database.execute("DELETE FROM agent_purchases WHERE id IN ('pp_pre268', 'pp_post268')")


async def test_the_parent_heal_takes_no_lock_once_the_check_is_wide():
    import asyncpg
    import db.agent_purchase_ledger as purchases

    other = await asyncpg.connect(DATABASE_URL)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute("LOCK TABLE agent_purchases IN ACCESS SHARE MODE")
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(purchases.ensure_agent_purchase_schema(), timeout=5)
        # well under the heal's 500 ms lock_timeout: no lock was even attempted (a DROP that waited
        # and gave up would also "return", which is why the bound matters)
        assert asyncio.get_running_loop().time() - started < 0.3
        await tx.rollback()
    finally:
        await other.close()


async def test_every_statement_runs_with_nulls_and_timestamps_on_postgres():
    from db.database import database
    import db.a2a_rail_ledger as ledger
    import db.agent_purchase_ledger as purchases

    row = await ledger.create_purchase(
        agent_id=AGENT, agent_user_ref_hash="h" * 64, merchant_domain="brand.example",
        checkout_url="https://brand.example/cart/1:1", currency="USD", our_price_minor=4650,
        shipping_address={"addressLine1": "900 Brannan St"}, buyer_email="ada@example.test")
    assert row["consented_at"] is None and row["shipping_address"] == {"addressLine1": "900 Brannan St"}
    with_consent = await ledger.create_purchase(
        agent_id=AGENT, agent_user_ref_hash="h" * 64, merchant_domain="brand.example",
        checkout_url="https://brand.example/cart/1:1", currency="USD", our_price_minor=4650, consent_version="v1")
    assert with_consent["consented_at"] is not None
    await database.execute("UPDATE a2a_rail_purchases SET next_poll_at = now() + interval '1 hour' WHERE id = :i",
                           {"i": with_consent["id"]})
    [claimed] = await ledger.claim_due_purchases("w1")
    assert claimed["id"] == row["id"]
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    asked = await ledger.transition_as_holder(
        row["id"], "w1", from_states=["opened"], to_state="needs_payment_method",
        buyer_action_kind="card_binding", buyer_action_url=PAGE, buyer_action_expires_at=future)
    assert asked["buyer_action_expires_at"] is not None
    moved = await ledger.transition_as_holder(row["id"], "w1", from_states=["needs_payment_method"], to_state="quoting")
    assert moved["buyer_action_url"] is None and moved["buyer_action_expires_at"] is None
    assert (await ledger.mark_pending_write(row["id"], "w1", "shop.cart#submit"))["pending_write"]
    assert (await ledger.clear_pending_write(row["id"], "w1", "shop.cart#submit"))["pending_write"] is None
    assert await ledger.release_claim(row["id"], "w1", next_poll_at=future, last_error_code="waiting")
    assert await ledger.requeue_stale_claims(lease_seconds=300) == 0
    await database.execute("UPDATE a2a_rail_purchases SET state_entered_at = now() - interval '3 days' WHERE id = :i",
                           {"i": row["id"]})
    assert await ledger.expire_overdue_purchases(max_age_seconds=86400) == 1
    gone = await ledger.get_purchase_internal(row["id"])
    assert gone["state"] == "expired" and gone["shipping_address"] is None and gone["buyer_email"] is None
    parent = await purchases.ensure_a2a_parent(row["id"])
    assert parent and parent == await purchases.ensure_a2a_parent(row["id"])


async def test_concurrent_workers_claim_disjoint_purchases():
    import db.a2a_rail_ledger as ledger

    made = set()
    for i in range(12):
        row = await ledger.create_purchase(
            agent_id=AGENT, agent_user_ref_hash=f"{i:064d}", merchant_domain="brand.example",
            checkout_url="https://brand.example/cart/1:1", currency="USD", our_price_minor=100)
        made.add(row["id"])
    batches = await asyncio.gather(*(ledger.claim_due_purchases(f"w{k}", limit=12) for k in range(4)))
    claimed = [r["id"] for batch in batches for r in batch if r["id"] in made]
    assert len(claimed) == len(set(claimed)) == 12


async def _stale_awaiting_authorization():
    from db.database import database
    import db.a2a_rail_ledger as ledger

    row = await ledger.create_purchase(
        agent_id=AGENT, agent_user_ref_hash="r" * 64, merchant_domain="brand.example",
        checkout_url="https://brand.example/cart/1:1", currency="USD", our_price_minor=4650,
        buyer_email="ada@example.test")
    [claimed] = await ledger.claim_due_purchases("w1")
    assert claimed["id"] == row["id"]
    await ledger.transition_as_holder(row["id"], "w1", from_states=["opened"], to_state="quoting")
    await ledger.transition_as_holder(row["id"], "w1", from_states=["quoting"], to_state="awaiting_authorization",
                                      buyer_action_kind="approval", buyer_action_url=PAGE)
    await database.execute("UPDATE a2a_rail_purchases SET state_entered_at = now() - interval '3 days' WHERE id = :i",
                           {"i": row["id"]})
    return row["id"]


async def test_the_sweep_does_not_expire_a_row_whose_pay_write_was_recorded_while_it_waited():
    """The race the outer predicate guards (reproduced by review): the sweep's subquery saw the row
    with no pending write, then blocked on its lock while the worker recorded `pay`. Under READ
    COMMITTED the woken UPDATE re-checks only its own WHERE."""
    import asyncpg
    from db.database import database
    import db.a2a_rail_ledger as ledger

    pid = await _stale_awaiting_authorization()
    worker = await asyncpg.connect(DATABASE_URL)
    try:
        tx = worker.transaction()
        await tx.start()
        await worker.execute(
            "UPDATE a2a_rail_purchases SET pending_write = 'shop.order-detail#pay', pending_write_at = now() "
            "WHERE id = $1", pid)
        sweep = asyncio.ensure_future(ledger.expire_overdue_purchases(max_age_seconds=86400))
        await asyncio.sleep(0.5)
        assert not sweep.done()  # blocked on the worker's row lock
        await tx.commit()
        assert await asyncio.wait_for(sweep, timeout=10) == 0
    finally:
        await worker.close()
    row = await database.fetch_one("SELECT state, pending_write, buyer_email FROM a2a_rail_purchases WHERE id = :i",
                                   {"i": pid})
    assert (row["state"], row["pending_write"], row["buyer_email"]) == (
        "awaiting_authorization", "shop.order-detail#pay", "ada@example.test")


async def test_the_requeue_does_not_strip_a_lease_taken_while_it_waited():
    import asyncpg
    from db.database import database
    import db.a2a_rail_ledger as ledger

    pid = await _stale_awaiting_authorization()
    await database.execute("UPDATE a2a_rail_purchases SET claimed_at = now() - interval '10 minutes' WHERE id = :i",
                           {"i": pid})
    other = await asyncpg.connect(DATABASE_URL)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute("UPDATE a2a_rail_purchases SET claimed_by = 'w2', claimed_at = now() WHERE id = $1", pid)
        requeue = asyncio.ensure_future(ledger.requeue_stale_claims(lease_seconds=300))
        await asyncio.sleep(0.5)
        assert not requeue.done()
        await tx.commit()
        assert await asyncio.wait_for(requeue, timeout=10) == 0
    finally:
        await other.close()
    row = await database.fetch_one("SELECT claimed_by FROM a2a_rail_purchases WHERE id = :i", {"i": pid})
    assert row["claimed_by"] == "w2"


async def test_the_new_statements_run_with_real_parameters_on_postgres():
    from db.database import database
    import db.a2a_rail_ledger as ledger

    pid = await _stale_awaiting_authorization()
    await ledger.mark_pending_write(pid, "w1", "payment.token-setup#submit")
    assert await ledger.transition_as_holder(pid, "w1", from_states=["awaiting_authorization"],
                                             to_state="paying") is None  # unresolved write
    paying = await ledger.transition_as_holder(pid, "w1", from_states=["awaiting_authorization"], to_state="paying",
                                               resolves_write="payment.token-setup#submit", quote_id="q_1")
    assert paying["state"] == "paying" and paying["pending_write"] is None
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    challenged = await ledger.set_payment_challenge(pid, "w1", url=PAGE, expires_at=future)
    assert challenged["buyer_action_kind"] == "authenticate" and challenged["buyer_action_expires_at"] is not None
    cleared = await ledger.set_payment_challenge(pid, "w1", url=None)
    assert cleared["buyer_action_url"] is None and cleared["buyer_action_expires_at"] is None
    await database.execute("UPDATE a2a_rail_purchases SET created_at = now() - interval '4 days' WHERE id = :i",
                           {"i": pid})
    assert await ledger.purge_stale_contact(max_age_seconds=3 * 86400) >= 1
    row = await ledger.get_purchase_internal(pid)
    assert row["buyer_email"] is None and row["contact_purged_at"] is not None and row["state"] == "paying"
    # the re-quote edge with typed NULLs on Postgres, after a voided token is forgotten
    other = await _stale_awaiting_authorization()
    await database.execute("UPDATE a2a_rail_purchases SET payment_token_id = 'ptk_1' WHERE id = :i", {"i": other})
    assert await ledger.transition_as_holder(other, "w1", from_states=["awaiting_authorization"],
                                             to_state="quoting") is None
    assert (await ledger.forget_voided_token(other, "w1", "ptk_1"))["payment_token_id"] is None
    requoted = await ledger.transition_as_holder(other, "w1", from_states=["awaiting_authorization"], to_state="quoting")
    assert requoted["quote_id"] is None and requoted["quoted_total_minor"] is None
    # the lifetime ceiling on Postgres
    await database.execute("UPDATE a2a_rail_purchases SET created_at = now() - interval '3 days', "
                           "state_entered_at = now() WHERE id = :i", {"i": other})
    await ledger.release_claim(other, "w1")
    assert await ledger.expire_overdue_purchases(max_age_seconds=86400, max_lifetime_seconds=2 * 86400) >= 1
    assert (await ledger.get_purchase_internal(other))["state"] == "expired"


async def _rail_check_state():
    from db.database import database

    row = await database.fetch_one(
        "SELECT pg_get_constraintdef(c.oid) AS def, c.convalidated AS ok FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = 'agent_purchases' AND c.conname = 'ck_agent_purchases_rail'")
    return (row["def"], row["ok"]) if row else (None, None)


async def _narrow_rail_check():
    from db.database import database

    await database.execute("ALTER TABLE agent_purchases DROP CONSTRAINT IF EXISTS ck_agent_purchases_rail")
    await database.execute("ALTER TABLE agent_purchases ADD CONSTRAINT ck_agent_purchases_rail CHECK (rail IN ('reap'))")


async def test_the_parent_heal_finishes_a_widening_interrupted_before_validation():
    from db.database import database
    import db.agent_purchase_ledger as purchases

    await database.execute("ALTER TABLE agent_purchases DROP CONSTRAINT IF EXISTS ck_agent_purchases_rail")
    await database.execute(
        "ALTER TABLE agent_purchases ADD CONSTRAINT ck_agent_purchases_rail CHECK (rail IN ('reap', 'a2a')) NOT VALID")
    assert (await _rail_check_state())[1] is False
    await purchases.ensure_agent_purchase_schema()
    assert await _rail_check_state() == (purchases._RAIL_CHECK_PG_DEF, True)


async def test_the_parent_heal_defers_rather_than_waits_when_the_table_is_busy():
    """A boot must not queue behind live traffic: with a reader holding the table, step 1's ACCESS
    EXCLUSIVE gives up at the heal lock_timeout and the heal is deferred, not stuck."""
    import asyncpg
    import db.agent_purchase_ledger as purchases

    await _narrow_rail_check()
    reader = await asyncpg.connect(DATABASE_URL)
    try:
        tx = reader.transaction()
        await tx.start()
        await reader.execute("LOCK TABLE agent_purchases IN ACCESS SHARE MODE")
        await asyncio.wait_for(purchases.ensure_agent_purchase_schema(), timeout=5)
        assert "'a2a'" not in (await _rail_check_state())[0]  # deferred to the next boot
        await tx.rollback()
    finally:
        await reader.close()
    await purchases.ensure_agent_purchase_schema()
    assert await _rail_check_state() == (purchases._RAIL_CHECK_PG_DEF, True)


async def test_validation_does_not_block_writers_and_writers_do_not_block_it():
    """Step 2 runs under SHARE UPDATE EXCLUSIVE: an open writer transaction on the table neither
    waits for it nor stops it."""
    import asyncpg
    from db.database import database
    import db.agent_purchase_ledger as purchases

    await database.execute("ALTER TABLE agent_purchases DROP CONSTRAINT IF EXISTS ck_agent_purchases_rail")
    await database.execute(
        "ALTER TABLE agent_purchases ADD CONSTRAINT ck_agent_purchases_rail CHECK (rail IN ('reap', 'a2a')) NOT VALID")
    writer = await asyncpg.connect(DATABASE_URL)
    try:
        tx = writer.transaction()
        await tx.start()
        await writer.execute(
            "INSERT INTO agent_purchases (id, rail, executor, rail_purchase_id) "
            "VALUES ('pp_writer_open', 'reap', 'rail_managed', 'rp_writer_open')")
        await asyncio.wait_for(purchases.ensure_agent_purchase_schema(), timeout=5)
        assert await _rail_check_state() == (purchases._RAIL_CHECK_PG_DEF, True)
        await tx.rollback()
    finally:
        await writer.close()


async def test_a_healthy_boot_takes_no_lock_that_waits_for_writers():
    """Once wide and indexed, both ledgers' heals are catalog reads: an open writer transaction on
    each table (ROW EXCLUSIVE, which a bare CREATE INDEX IF NOT EXISTS's SHARE lock would wait on)
    does not hold the boot up."""
    import asyncpg
    import db.a2a_rail_ledger as ledger
    import db.agent_purchase_ledger as purchases

    await purchases.ensure_agent_purchase_schema()
    await ledger.ensure_a2a_rail_schema()
    writer = await asyncpg.connect(DATABASE_URL)
    try:
        tx = writer.transaction()
        await tx.start()
        await writer.execute("LOCK TABLE agent_purchases IN ROW EXCLUSIVE MODE")
        await writer.execute("LOCK TABLE a2a_rail_purchases IN ROW EXCLUSIVE MODE")
        await writer.execute("LOCK TABLE a2a_rail_buyer_refs IN ROW EXCLUSIVE MODE")
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(purchases.ensure_agent_purchase_schema(), timeout=5)
        await asyncio.wait_for(ledger.ensure_a2a_rail_schema(), timeout=5)
        assert asyncio.get_running_loop().time() - started < 0.3  # nothing waited on a lock
        await tx.rollback()
    finally:
        await writer.close()



async def test_the_sweep_does_not_expire_a_row_requoted_while_it_waited():
    """The time predicate is part of the race guard too: the sweep saw a stale row, then blocked
    while the worker re-quoted it (fresh state_entered_at). Woken, it must re-check the clocks."""
    import asyncpg
    from db.database import database
    import db.a2a_rail_ledger as ledger

    pid = await _stale_awaiting_authorization()
    worker = await asyncpg.connect(DATABASE_URL)
    try:
        tx = worker.transaction()
        await tx.start()
        await worker.execute(
            "UPDATE a2a_rail_purchases SET state = 'quoting', state_entered_at = now(), "
            "buyer_action_kind = NULL, buyer_action_url = NULL WHERE id = $1", pid)
        sweep = asyncio.ensure_future(ledger.expire_overdue_purchases(max_age_seconds=86400))
        await asyncio.sleep(0.5)
        assert not sweep.done()
        await tx.commit()
        assert await asyncio.wait_for(sweep, timeout=10) == 0
    finally:
        await worker.close()
    assert (await ledger.get_purchase_internal(pid))["state"] == "quoting"
