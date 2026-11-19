"""The affiliate program registry's SQL on Postgres (db/affiliate_programs.py, migration 291).

What only Postgres can show: the module and the migration build the same two tables (columns, CHECKs, the
foreign key, both indexes); the boot heal is idempotent; the service's statements run on asyncpg (create,
update, mark-live, auto un-live, history order, import idempotency); the live CHECK and the partial unique
index hold.

The routes, the dial and the roles are tested on SQLite (tests/test_employee_affiliate_programs.py).

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_affiliate_programs_postgres.py

THIS MODULE MUST NOT IMPORT `main` (the gate runs every test_*_postgres.py in one process).
"""

from __future__ import annotations

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
        "network": "rakuten", "advertiser_id": "PGTEST-1", "merchant_name": PREFIX + " A",
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
                f"awin,PGTEST-2,{PREFIX} B,3.5,2026-09-25\n"
                f"impact,,{PREFIX} C,,\n")
    first = await programs.import_csv(csv_text, dry_run=False, actor="pg@example.com")
    assert first["summary"] == {"create": 2, "update": 0, "unchanged": 0, "error": 0}
    second = await programs.import_csv(csv_text, dry_run=False, actor="pg@example.com")
    assert second["summary"] == {"create": 0, "update": 0, "unchanged": 2, "error": 0}


async def test_the_live_check_and_the_unique_index_hold(_db):
    import db.affiliate_programs as store
    from services import affiliate_programs as programs

    database = _db
    created = await programs.create_program({"network": "rakuten", "advertiser_id": "PGTEST-3",
                                             "merchant_name": PREFIX + " D"}, "pg@example.com")
    with pytest.raises(Exception):
        await database.execute("UPDATE affiliate_programs SET live_at = now() WHERE id = :id", {"id": created["id"]})
    with pytest.raises(programs.DuplicateProgram):
        await programs.create_program({"network": "rakuten", "advertiser_id": "PGTEST-3",
                                       "merchant_name": PREFIX + " D again"}, "pg@example.com")
    for name in (" E", " F"):
        await programs.create_program({"network": "cj", "merchant_name": PREFIX + name}, "pg@example.com")
    assert len([p for p in await store.list_programs() if str(p["merchant_name"]).startswith(PREFIX)]) == 3


def test_this_module_does_not_import_main():
    assert "main" not in sys.modules
