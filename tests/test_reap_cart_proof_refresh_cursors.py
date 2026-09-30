"""db/reap_cart_proof_refresh_cursors.py -- where each cart-proof refresh left off (migration 250).

Dialect-agnostic: runs on the suite's SQLite here, and on Postgres through
tests/test_reap_cart_proof_refresh_cursors_postgres.py, which star-imports this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import db.reap_cart_proof_refresh_cursors as cursors
from db.database import database

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
_MIGRATION = _MIGRATIONS_DIR / "250_reap_cart_proof_refresh_cursors.sql"
_DOWN = _MIGRATIONS_DIR / "down" / "250_reap_cart_proof_refresh_cursors_down.sql"
T1 = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


def _utc(value: datetime) -> datetime:
    """SQLite hands a TIMESTAMPTZ back naive; Postgres, aware."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _statement_body(sql: str) -> str:
    lines = [line.split("--", 1)[0] for line in sql.splitlines()]
    return " ".join(" ".join(lines).split()).rstrip(";").strip()


def test_the_self_heal_statement_is_the_migrations_statement():
    migration = _statement_body(_MIGRATION.read_text(encoding="utf-8"))
    assert migration.startswith("CREATE TABLE IF NOT EXISTS reap_cart_proof_refresh_cursors (")
    assert _statement_body(cursors._CREATE) == migration


def test_the_down_migration_drops_only_this_table():
    body = _statement_body(_DOWN.read_text(encoding="utf-8"))
    assert body == "DROP TABLE IF EXISTS reap_cart_proof_refresh_cursors"


@pytest.fixture
async def cursor_db():
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute(f"DROP TABLE IF EXISTS {cursors.TABLE}")
    assert await cursors.ensure_table(database)
    yield database
    await database.execute(f"DROP TABLE IF EXISTS {cursors.TABLE}")
    if not was_connected and database.is_connected:
        await database.disconnect()


async def test_ensure_table_is_idempotent(cursor_db):
    assert await cursors.ensure_table(cursor_db)


async def test_a_cursor_round_trips_per_lane(cursor_db):
    await cursors.save(cursor_db, lane="mirror", domain="anua.us", next_cursor="epsv_040",
                       last_status="in_progress", completed_at=None, now=T1)
    await cursors.save(cursor_db, lane="enrichment", domain="anua.us", next_cursor=None,
                       last_status="done", completed_at=T1, now=T1)
    mirror = await cursors.load(cursor_db, "mirror", table_must_exist=True)
    assert set(mirror) == {"anua.us"}
    assert mirror["anua.us"].next_cursor == "epsv_040" and mirror["anua.us"].last_status == "in_progress"
    assert mirror["anua.us"].last_completed_at is None
    enrichment = await cursors.load(cursor_db, "enrichment", table_must_exist=True)
    assert enrichment["anua.us"].next_cursor is None
    assert _utc(enrichment["anua.us"].last_completed_at) == T1


async def test_a_later_incomplete_walk_keeps_the_last_completed_time(cursor_db):
    await cursors.save(cursor_db, lane="mirror", domain="a.com", next_cursor=None, last_status="done",
                       completed_at=T1, now=T1)
    await cursors.save(cursor_db, lane="mirror", domain="a.com", next_cursor="epsv_9",
                       last_status="budget_stopped", completed_at=None, now=T2)
    row = (await cursors.load(cursor_db, "mirror", table_must_exist=True))["a.com"]
    assert row.next_cursor == "epsv_9" and row.last_status == "budget_stopped"
    assert row.last_completed_at is not None and _utc(row.last_completed_at) == T1
    await cursors.save(cursor_db, lane="mirror", domain="a.com", next_cursor=None, last_status="done",
                       completed_at=T2, now=T2)
    row = (await cursors.load(cursor_db, "mirror", table_must_exist=True))["a.com"]
    assert _utc(row.last_completed_at) == T2


async def test_a_missing_table_reads_as_no_cursors_only_for_a_dry_run():
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    try:
        await database.execute(f"DROP TABLE IF EXISTS {cursors.TABLE}")
        assert await cursors.load(database, "mirror", table_must_exist=False) == {}
        with pytest.raises(Exception):
            await cursors.load(database, "mirror", table_must_exist=True)
    finally:
        if not was_connected and database.is_connected:
            await database.disconnect()
