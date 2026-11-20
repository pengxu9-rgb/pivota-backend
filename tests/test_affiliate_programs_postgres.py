"""The affiliate program registry's SQL on Postgres (db/affiliate_programs.py, migration 291).

What only Postgres can show: the module and the migration build the same two tables (columns, CHECKs, the
foreign key, both indexes); the boot heal is idempotent; the service's statements run on asyncpg (create,
update, mark-live, auto un-live, history order, import idempotency); the live CHECK and the partial unique
index hold; two admins writing one program on two connections queue on the row lock, so neither write is
lost and the history matches the row; and a real import that fails part-way leaves nothing behind.

The routes, the dial and the roles are tested on SQLite (tests/test_employee_affiliate_programs.py).

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_affiliate_programs_postgres.py

THIS MODULE MUST NOT IMPORT `main` (the gate runs every test_*_postgres.py in one process).
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_REPO = Path(__file__).resolve().parents[1]
#: Every row this file writes carries this merchant-name prefix; cleanup deletes by it, never drops tables.
PREFIX = "PG Gate Merchant"

pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL; the affiliate registry's dialect gate")

assert "main" not in sys.modules or not _IS_PG, (
    "main is imported: it registers every model on the shared metadata at collection time "
    "and poisons later create_all calls in the Postgres gate"
)


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to touch affiliate_programs in database {dbname!r}: throwaway only")


async def _cleanup(database) -> None:
    ids = "SELECT id FROM affiliate_programs WHERE merchant_name LIKE :p"
    for statement in (f"DELETE FROM affiliate_program_events WHERE program_id IN ({ids})",
                      "DELETE FROM affiliate_programs WHERE merchant_name LIKE :p"):
        try:
            await database.execute(statement, {"p": PREFIX + "%"})
        except Exception:  # noqa: BLE001 - a table this run never built is not a leak
            pass


@pytest.fixture(autouse=True)
async def _db():
    from db.affiliate_programs import ensure_affiliate_programs_schema
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await ensure_affiliate_programs_schema()
    await _cleanup(database)
    try:
        yield database
    finally:
        await _cleanup(database)
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _columns(database, table: str):
    rows = await database.fetch_all(
        "SELECT column_name, data_type, is_nullable, column_default, character_maximum_length "
        "FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = :t "
        "ORDER BY ordinal_position", {"t": table})
    return [tuple(r) for r in rows]


async def _constraints(database, table: str, renames):
    rows = await database.fetch_all(
        "SELECT replace(c.conname, :t, 'T') AS name, pg_get_constraintdef(c.oid) AS def FROM pg_constraint c "
        "JOIN pg_class r ON r.oid = c.conrelid WHERE r.relname = :t AND r.relnamespace = current_schema()::regnamespace "
        "ORDER BY 1", {"t": table})
    out = []
    for name, definition in rows:
        for twin, base in renames:
            definition = definition.replace(twin, base)
        out.append((name, definition))
    return out


async def _indexdef(database, name: str):
    row = await database.fetch_one(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :i", {"i": name})
    return row["indexdef"] if row else None


async def test_the_migration_and_the_module_build_the_same_tables(_db):
    database = _db
    twin_programs, twin_events = "affiliate_programs_mig291", "affiliate_program_events_mig291"
    sql = (_REPO / "db/migrations/291_affiliate_programs.sql").read_text("utf-8")
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = sql.replace("affiliate_program_events", twin_events).replace("affiliate_programs", twin_programs)
    await database.execute(f"DROP TABLE IF EXISTS {twin_events}")
    await database.execute(f"DROP TABLE IF EXISTS {twin_programs}")
    try:
        for statement in [s.strip() for s in sql.split(";") if s.strip()]:
            await database.execute(statement)
        renames = [(twin_events, "affiliate_program_events"), (twin_programs, "affiliate_programs")]
        for twin, base in ((twin_programs, "affiliate_programs"), (twin_events, "affiliate_program_events")):
            assert await _columns(database, twin) == await _columns(database, base), base
            assert await _constraints(database, twin, renames) == await _constraints(database, base, renames), base
        unique = await _indexdef(database, "uq_affiliate_programs_network_advertiser")
        assert unique and "UNIQUE" in unique and "(network, advertiser_id)" in unique and "advertiser_id IS NOT NULL" in unique
        twin_unique = await _indexdef(database, f"uq_{twin_programs}_network_advertiser")
        assert twin_unique and twin_unique.replace(twin_programs, "affiliate_programs").replace(
            f"uq_affiliate_programs_network_advertiser", "uq_affiliate_programs_network_advertiser") == unique
        events_index = await _indexdef(database, "idx_affiliate_program_events_program")
        assert events_index and "(program_id, created_at)" in events_index
    finally:
        await database.execute(f"DROP TABLE IF EXISTS {twin_events}")
        await database.execute(f"DROP TABLE IF EXISTS {twin_programs}")


async def test_the_heal_is_idempotent(_db):
    from db.affiliate_programs import ensure_affiliate_programs_schema

    await ensure_affiliate_programs_schema()
    await ensure_affiliate_programs_schema()


async def test_the_service_lifecycle_runs_on_postgres(_db):
    from services import affiliate_programs as programs

    created = await programs.create_program({
        "network": "other", "advertiser_id": "PGTEST-1", "merchant_name": PREFIX + " A",
        "merchant_domains": ["example-pg-a.test"], "commission_rate_bp": 400}, "pg@example.com")
    pid = created["id"]
    assert created["merchant_domains"] == ["example-pg-a.test"] and created["publisher_id_links_live"] is False
    with pytest.raises(programs.NotLiveReady):
        await programs.mark_live(pid, "pg@example.com")
    await programs.update_program(pid, {"status": "approved", "software_publishers_allowed": "allowed",
                                        "revenue_share_allowed": "allowed", "publisher_id_links_live": True}, "pg@example.com")
    live = await programs.mark_live(pid, "pg@example.com")
    assert live["live_at"] is not None and live["decided_at"] is not None
    unlived = await programs.update_program(pid, {"revenue_share_allowed": "not_allowed"}, "pg@example.com")
    assert unlived["live_at"] is None
    detail = await programs.detail(pid)
    assert [e["action"] for e in detail["events"]] == ["auto_unlive", "update", "mark_live", "update", "create"]
    assert detail["events"][0]["reason"] == "revenue_share_not_allowed"


async def test_the_import_is_idempotent_on_postgres(_db):
    from services import affiliate_programs as programs

    csv_text = ("network,advertiser_id,merchant_name,commission_rate_percent,applied_at\n"
                f"cj,PGTEST-2,{PREFIX} B,3.5,2026-09-25\n"
                f"cj,,{PREFIX} C,,\n")
    first = await programs.import_csv(csv_text, dry_run=False, actor="pg@example.com")
    assert first["summary"] == {"create": 2, "update": 0, "unchanged": 0, "error": 0}
    second = await programs.import_csv(csv_text, dry_run=False, actor="pg@example.com")
    assert second["summary"] == {"create": 0, "update": 0, "unchanged": 2, "error": 0}


async def test_the_live_check_and_the_unique_index_hold(_db):
    import db.affiliate_programs as store
    from services import affiliate_programs as programs

    database = _db
    created = await programs.create_program({"network": "other", "advertiser_id": "PGTEST-3",
                                             "merchant_name": PREFIX + " D"}, "pg@example.com")
    with pytest.raises(Exception):
        await database.execute("UPDATE affiliate_programs SET live_at = now() WHERE id = :id", {"id": created["id"]})
    with pytest.raises(programs.DuplicateProgram):
        await programs.create_program({"network": "other", "advertiser_id": "PGTEST-3",
                                       "merchant_name": PREFIX + " D again"}, "pg@example.com")
    for name in (" E", " F"):
        await programs.create_program({"network": "cj", "merchant_name": PREFIX + name}, "pg@example.com")
    assert len([p for p in await store.list_programs() if str(p["merchant_name"]).startswith(PREFIX)]) == 3


def test_this_module_does_not_import_main():
    assert "main" not in sys.modules


# ── two writers, two connections ─────────────────────────────────────────────────────────────
#
# Each writer runs in a task with an EMPTY context, so `databases` gives it its own connection (a task that
# copies a context holding a connection would share it). The first writer is held between its read and its
# write; the second must then wait on the row lock instead of reading the stale row.


async def _live_program(name: str):
    from services import affiliate_programs as programs

    created = await programs.create_program({"network": "other", "merchant_name": PREFIX + name}, "setup@example.com")
    await programs.update_program(created["id"], {"status": "approved", "software_publishers_allowed": "allowed",
                                                  "revenue_share_allowed": "allowed", "publisher_id_links_live": True},
                                  "setup@example.com")
    await programs.mark_live(created["id"], "setup@example.com")
    return created["id"]


def _hold_writes_by(monkeypatch, actor: str):
    """Hold `actor`'s row write (after its locked read) until the returned release event is set."""
    import db.affiliate_programs as store

    reached, release = asyncio.Event(), asyncio.Event()
    real_update = store.update_program

    async def held(program_id, values, *, actor: str, now):  # noqa: ANN001
        if actor == held_actor:
            reached.set()
            await release.wait()
        await real_update(program_id, values, actor=actor, now=now)

    held_actor = actor
    monkeypatch.setattr(store, "update_program", held)
    return reached, release


def _own_connection(coro):
    return asyncio.get_running_loop().create_task(coro, context=contextvars.Context())


async def _race(monkeypatch, first, second, first_actor: str):
    reached, release = _hold_writes_by(monkeypatch, first_actor)
    task_first = _own_connection(first)
    await asyncio.wait_for(reached.wait(), 10)
    task_second = _own_connection(second)
    try:
        await asyncio.sleep(0.5)
        second_finished_early = task_second.done()
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(task_first, task_second, return_exceptions=True), 15)
    assert not second_finished_early, "the second writer read the row while the first held it: no row lock"
    return results


async def test_a_notes_edit_cannot_undo_a_not_allowed_term_held_before_it(_db, monkeypatch):
    from services import affiliate_programs as programs

    pid = await _live_program(" race 1")
    results = await _race(
        monkeypatch,
        programs.update_program(pid, {"software_publishers_allowed": "not_allowed"}, "b@example.com"),
        programs.update_program(pid, {"notes": "checked the payout page"}, "a@example.com"),
        "b@example.com")
    assert not [r for r in results if isinstance(r, BaseException)], results
    row = (await programs.detail(pid))
    assert row["program"]["software_publishers_allowed"] == "not_allowed"
    assert row["program"]["notes"] == "checked the payout page"
    assert row["program"]["live_at"] is None
    events = row["events"]
    assert [(e["action"], e["actor"]) for e in events[:3]] == [
        ("update", "a@example.com"), ("auto_unlive", "b@example.com"), ("update", "b@example.com")]
    assert events[0]["before_state"]["software_publishers_allowed"] == "not_allowed"
    assert events[0]["after_state"]["software_publishers_allowed"] == "not_allowed"


async def test_a_not_allowed_term_after_a_held_notes_edit_still_unlives(_db, monkeypatch):
    from services import affiliate_programs as programs

    pid = await _live_program(" race 2")
    results = await _race(
        monkeypatch,
        programs.update_program(pid, {"notes": "checked the payout page"}, "a@example.com"),
        programs.update_program(pid, {"software_publishers_allowed": "not_allowed"}, "b@example.com"),
        "a@example.com")
    assert not [r for r in results if isinstance(r, BaseException)], results
    row = (await programs.detail(pid))["program"]
    assert (row["software_publishers_allowed"], row["notes"], row["live_at"]) == (
        "not_allowed", "checked the payout page", None)


async def test_mark_live_waits_for_a_held_pause_and_then_refuses(_db, monkeypatch):
    from services import affiliate_programs as programs

    pid = await _live_program(" race 3")
    await programs.unlive(pid, "setup@example.com", "re-check terms")
    results = await _race(
        monkeypatch,
        programs.update_program(pid, {"status": "paused"}, "b@example.com"),
        programs.mark_live(pid, "a@example.com"),
        "b@example.com")
    assert isinstance(results[1], programs.NotLiveReady), results
    assert "status_not_approved" in results[1].reasons
    row = (await programs.detail(pid))["program"]
    assert (row["status"], row["live_at"]) == ("paused", None)


async def test_unlive_and_an_edit_both_land(_db, monkeypatch):
    from services import affiliate_programs as programs

    pid = await _live_program(" race 4")
    results = await _race(
        monkeypatch,
        programs.unlive(pid, "b@example.com", "terms under review"),
        programs.update_program(pid, {"notes": "asked the network"}, "a@example.com"),
        "b@example.com")
    assert not [r for r in results if isinstance(r, BaseException)], results
    row = (await programs.detail(pid))["program"]
    assert (row["live_at"], row["notes"]) == (None, "asked the network")


# ── the import is all-or-nothing on Postgres ─────────────────────────────────────────────────


async def test_a_real_import_that_fails_part_way_leaves_nothing_on_postgres(_db, monkeypatch):
    import db.affiliate_programs as store
    from services import affiliate_programs as programs

    csv_text = ("network,advertiser_id,merchant_name\n"
                f"other,PGTEST-6,{PREFIX} G\n"
                f"other,PGTEST-7,{PREFIX} H\n")
    real_insert = store.insert_program
    calls = {"n": 0}

    async def failing_insert(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure on the second row")
        await real_insert(*args, **kwargs)

    monkeypatch.setattr(store, "insert_program", failing_insert)
    with pytest.raises(RuntimeError):
        await programs.import_csv(csv_text, dry_run=False, actor="pg@example.com")
    assert [p for p in await store.list_programs() if str(p["merchant_name"]).startswith(PREFIX)] == []


async def test_a_real_import_with_an_invalid_row_writes_nothing_on_postgres(_db):
    import db.affiliate_programs as store
    from services import affiliate_programs as programs

    csv_text = ("network,advertiser_id,merchant_name\n"
                f"other,PGTEST-8,{PREFIX} I\n"
                f"myspace,PGTEST-9,{PREFIX} J\n")
    with pytest.raises(programs.ImportRefused) as refused:
        await programs.import_csv(csv_text, dry_run=False, actor="pg@example.com")
    assert refused.value.report["summary"]["error"] == 1
    assert [p for p in await store.list_programs() if str(p["merchant_name"]).startswith(PREFIX)] == []
