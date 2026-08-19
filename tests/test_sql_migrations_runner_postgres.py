"""
Gates for db/sql_migrations.py — the db/migrations/*.sql runner.

The bug it replaces, measured on a clean Postgres 2026-08-19 with the pinned
SQLAlchemy 1.4.52: main.startup() ran each file as
``with engine.connect() as conn: conn.execute(text(sql)); conn.commit()``.
A legacy 1.4 Connection has no ``.commit()``, so EVERY file raised
AttributeError (logged as "may be already applied"). Files whose text starts
with a DDL keyword still landed through 1.4's legacy autocommit; files that
start with a ``--`` comment — 003_create_users_table, 024_webhook_events,
031_create_employees_table and ~40 more — were executed and then ROLLED BACK.
On a fresh database those tables never existed, which is where
``relation "webhook_events" does not exist`` at boot came from.

Mutants these kill:
* revert to ``engine.connect()`` + ``conn.commit()``
  → test_comment_led_migration_is_committed fails (AttributeError / no table).
* drop the AUTOCOMMIT + per-statement path
  → test_concurrently_migration_applies fails (ActiveSqlTransaction).
* drop the ledger
  → test_applied_file_is_not_rerun fails.
* make the splitter naive (``body.split(";")``)
  → test_split_statements_is_quote_aware fails.

Postgres-backed cases need a Postgres DATABASE_URL (see tests/test_startup_ddl_race_postgres.py).
"""
from __future__ import annotations

import os
import uuid

import pytest

from db.sql_migrations import (
    LEDGER_TABLE,
    list_migration_files,
    needs_autocommit,
    run_sql_migrations,
    split_statements,
)

# See tests/test_startup_ddl_race_postgres.py for why DATABASE_URL is honoured.
PG_TEST_URL = (os.getenv("PIVOTA_PG_TEST_URL") or os.getenv("DATABASE_URL") or "").strip()
requires_pg = pytest.mark.skipif(
    not PG_TEST_URL.startswith("postgres"),
    reason="needs a Postgres DATABASE_URL (or PIVOTA_PG_TEST_URL) to run the migration-runner gates",
)


def test_split_statements_is_quote_aware() -> None:
    body = """
    -- a comment with a ; semicolon
    CREATE TABLE a (v TEXT DEFAULT 'x;y');
    /* block ; comment */
    CREATE FUNCTION f() RETURNS void AS $$
    BEGIN
        PERFORM 1; PERFORM 2;
    END $$ LANGUAGE plpgsql;
    CREATE INDEX CONCURRENTLY IF NOT EXISTS "idx;weird" ON a (v);
    """
    parts = split_statements(body)
    assert len(parts) == 3, parts
    assert parts[0].lstrip().startswith("--"), (
        "the line comment was dropped from the statement: %r" % parts[0]
    )
    assert parts[0].endswith("'x;y')")
    assert "$$" in parts[1] and "PERFORM 1; PERFORM 2;" in parts[1]
    assert parts[2].startswith("CREATE INDEX CONCURRENTLY")
    # Comment-only tail produces no empty statement.
    assert all(p.strip() for p in parts)


def test_needs_autocommit_detects_concurrently() -> None:
    assert needs_autocommit("CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (c);")
    assert needs_autocommit("drop index concurrently i;")
    assert not needs_autocommit("CREATE INDEX IF NOT EXISTS i ON t (c);")
    # A column merely named like it must not flip the whole file to autocommit.
    assert not needs_autocommit("SELECT concurrently_flag FROM t;")


def test_repo_migrations_are_discovered_and_disabled_ones_excluded() -> None:
    names = {os.path.basename(p) for p in list_migration_files()}
    assert names, "no migrations discovered"
    assert all(n.endswith(".sql") for n in names)
    assert "002_production_tables.sql" not in names
    assert "007_agents_management_upgrade.sql" not in names


@pytest.fixture
def fresh_pg_engine(tmp_path):
    """A SQLAlchemy engine on a brand-new empty database, plus a migrations dir."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine.url import make_url

    base = make_url(PG_TEST_URL)
    name = f"migrun_{uuid.uuid4().hex[:12]}"
    admin = create_engine(str(base), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(str(base.set(database=name)))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _write(migdir, name, body):
    d = os.path.join(migdir, "db", "migrations")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        fh.write(body)


@requires_pg
def test_comment_led_migration_is_committed(fresh_pg_engine, tmp_path) -> None:
    """The exact shape that silently rolled back before: a file starting with
    a `--` comment."""
    from sqlalchemy import text

    base = str(tmp_path)
    _write(base, "001_comment_led.sql", "-- leading comment\nCREATE TABLE IF NOT EXISTS t_comment (id int);\n")
    _write(base, "002_stmt_led.sql", "CREATE TABLE IF NOT EXISTS t_stmt (id int);\n")

    result = run_sql_migrations(fresh_pg_engine, base_dir=base)
    assert result["failed"] == [], result["failed"]
    assert sorted(result["applied"]) == ["001_comment_led.sql", "002_stmt_led.sql"]

    with fresh_pg_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('t_comment')")).scalar() == "t_comment"
        assert conn.execute(text("SELECT to_regclass('t_stmt')")).scalar() == "t_stmt"


@requires_pg
def test_applied_file_is_not_rerun(fresh_pg_engine, tmp_path) -> None:
    """The ledger makes each file run once per database: a second pass must
    not re-execute the (non-idempotent) INSERT."""
    from sqlalchemy import text

    base = str(tmp_path)
    _write(
        base,
        "001_seed.sql",
        "-- seeds a row\nCREATE TABLE IF NOT EXISTS t_seed (v TEXT);\nINSERT INTO t_seed (v) VALUES ('once');\n",
    )
    first = run_sql_migrations(fresh_pg_engine, base_dir=base)
    second = run_sql_migrations(fresh_pg_engine, base_dir=base)

    assert first["applied"] == ["001_seed.sql"]
    assert second["applied"] == [] and second["skipped"] == ["001_seed.sql"]
    with fresh_pg_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM t_seed")).scalar() == 1
        assert conn.execute(text(f"SELECT count(*) FROM {LEDGER_TABLE}")).scalar() == 1


@requires_pg
def test_failed_file_rolls_back_whole_file_and_is_retried(fresh_pg_engine, tmp_path) -> None:
    from sqlalchemy import text

    base = str(tmp_path)
    _write(
        base,
        "001_partial.sql",
        "-- second statement is bad\nCREATE TABLE IF NOT EXISTS t_partial (id int);\nSELECT * FROM does_not_exist;\n",
    )
    result = run_sql_migrations(fresh_pg_engine, base_dir=base)
    assert [n for n, _ in result["failed"]] == ["001_partial.sql"]
    with fresh_pg_engine.connect() as conn:
        # whole file rolled back — no half-applied schema
        assert conn.execute(text("SELECT to_regclass('t_partial')")).scalar() is None
        assert conn.execute(text(f"SELECT count(*) FROM {LEDGER_TABLE}")).scalar() == 0

    # Fix the file; the next boot retries it (no ledger row was written).
    _write(base, "001_partial.sql", "-- fixed\nCREATE TABLE IF NOT EXISTS t_partial (id int);\n")
    again = run_sql_migrations(fresh_pg_engine, base_dir=base)
    assert again["applied"] == ["001_partial.sql"]


@requires_pg
def test_concurrently_migration_applies(fresh_pg_engine, tmp_path) -> None:
    """CREATE INDEX CONCURRENTLY cannot run in a transaction block, and
    psycopg2 sends a multi-statement string as one implicit transaction even
    under AUTOCOMMIT — so the runner must also split the file."""
    from sqlalchemy import text

    base = str(tmp_path)
    _write(
        base,
        "001_conc.sql",
        "-- concurrent index\n"
        "CREATE TABLE IF NOT EXISTS t_conc (id int, v text);\n"
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_t_conc_v ON t_conc (v);\n",
    )
    result = run_sql_migrations(fresh_pg_engine, base_dir=base)
    assert result["failed"] == [], result["failed"]
    with fresh_pg_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('idx_t_conc_v')")).scalar() == "idx_t_conc_v"


@requires_pg
def test_checksum_change_after_apply_is_reported_not_rerun(fresh_pg_engine, tmp_path, caplog) -> None:
    base = str(tmp_path)
    _write(base, "001_x.sql", "-- v1\nCREATE TABLE IF NOT EXISTS t_x (id int);\n")
    run_sql_migrations(fresh_pg_engine, base_dir=base)
    _write(base, "001_x.sql", "-- v2 edited after being applied\nCREATE TABLE IF NOT EXISTS t_x2 (id int);\n")
    with caplog.at_level("WARNING"):
        result = run_sql_migrations(fresh_pg_engine, base_dir=base)
    assert result["applied"] == [] and result["skipped"] == ["001_x.sql"]
    assert any("checksum changed" in r.getMessage() for r in caplog.records)
    from sqlalchemy import text

    with fresh_pg_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('t_x2')")).scalar() is None


@pytest.mark.asyncio
async def test_recheck_webhook_events_schema_clears_the_latch(monkeypatch) -> None:
    """The probe latches _CHECKED=True on failure; recheck must clear it so a
    table created by a later migration is picked up without a restart."""
    import services.webhook_service as ws

    calls = {"n": 0}

    class _Db:
        async def fetch_one(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError('relation "webhook_events" does not exist')
            return {"event_id": "x"}

    monkeypatch.setattr(ws, "database", _Db())
    monkeypatch.setattr(ws, "_WEBHOOK_EVENTS_SCHEMA_READY", False)
    monkeypatch.setattr(ws, "_WEBHOOK_EVENTS_SCHEMA_CHECKED", False)

    await ws.WebhookService.ensure_webhook_events_table()
    assert ws._WEBHOOK_EVENTS_SCHEMA_READY is False
    # Without the recheck the latch keeps persistence off forever:
    await ws.WebhookService.ensure_webhook_events_table()
    assert calls["n"] == 1

    assert await ws.WebhookService.recheck_webhook_events_schema() is True
    assert calls["n"] == 2
    assert ws._WEBHOOK_EVENTS_SCHEMA_READY is True


@requires_pg
def test_colon_token_in_a_comment_is_not_a_bind_parameter(fresh_pg_engine, tmp_path) -> None:
    """SQLAlchemy text() reads `:a` as a bind parameter even inside a `--`
    comment. 191_acp_checkout_sessions.sql and 192_acp_delegate_allowances.sql
    both die that way ("A value is required for bind parameter 'a'") because
    a comment describes a cache key `acp_complete:<session_id>:a<attempt>`.
    Migration bodies are static SQL — they must go to the driver unparsed."""
    from sqlalchemy import text

    base = str(tmp_path)
    _write(
        base,
        "001_colon.sql",
        "-- cache key shape: acp_complete:<session_id>:a<capture_attempt>\n"
        "CREATE TABLE IF NOT EXISTS t_colon (id int);\n",
    )
    result = run_sql_migrations(fresh_pg_engine, base_dir=base)
    assert result["failed"] == [], result["failed"]
    with fresh_pg_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('t_colon')")).scalar() == "t_colon"


@requires_pg
def test_colon_token_in_a_concurrently_file_is_not_a_bind_parameter(fresh_pg_engine, tmp_path) -> None:
    """Same for the AUTOCOMMIT/split path."""
    from sqlalchemy import text

    base = str(tmp_path)
    _write(
        base,
        "001_colon_conc.sql",
        "-- key: foo:<id>:a<n>\n"
        "CREATE TABLE IF NOT EXISTS t_colon_conc (id int, v text);\n"
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_t_colon_conc ON t_colon_conc (v);\n",
    )
    result = run_sql_migrations(fresh_pg_engine, base_dir=base)
    assert result["failed"] == [], result["failed"]
    with fresh_pg_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('idx_t_colon_conc')")).scalar() == "idx_t_colon_conc"


@requires_pg
def test_literal_percent_is_not_interpolated(fresh_pg_engine, tmp_path) -> None:
    """exec_driver_sql() hands psycopg2 an empty parameter mapping, so any
    migration containing a literal `%` dies with "dict is not a sequence"
    (measured: 30+ files). The raw cursor path must not interpolate."""
    from sqlalchemy import text

    base = str(tmp_path)
    _write(
        base,
        "001_percent.sql",
        "-- 100% of rows, LIKE '%x%'\n"
        "CREATE TABLE IF NOT EXISTS t_pct (v TEXT);\n"
        "CREATE INDEX IF NOT EXISTS idx_t_pct ON t_pct (v) WHERE v LIKE '%pivota%';\n",
    )
    result = run_sql_migrations(fresh_pg_engine, base_dir=base)
    assert result["failed"] == [], result["failed"]
    with fresh_pg_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('idx_t_pct')")).scalar() == "idx_t_pct"


@requires_pg
def test_literal_percent_in_a_concurrently_file(fresh_pg_engine, tmp_path) -> None:
    from sqlalchemy import text

    base = str(tmp_path)
    _write(
        base,
        "001_pct_conc.sql",
        "-- 50% done\n"
        "CREATE TABLE IF NOT EXISTS t_pct_c (v TEXT);\n"
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_t_pct_c ON t_pct_c (v) WHERE v LIKE '%p%';\n",
    )
    result = run_sql_migrations(fresh_pg_engine, base_dir=base)
    assert result["failed"] == [], result["failed"]
    with fresh_pg_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('idx_t_pct_c')")).scalar() == "idx_t_pct_c"


@requires_pg
def test_migration_commits_without_the_ledger(fresh_pg_engine, tmp_path, monkeypatch) -> None:
    """_ensure_ledger() is allowed to fail ("running without it"), and on that
    path nothing else commits for us.

    This is the gate that pins `engine.begin()`. With `engine.connect()` the
    suite still passes on the normal path — but only by accident: the ledger
    INSERT is DML, so SQLAlchemy 1.4 legacy autocommit fires and incidentally
    commits the raw-cursor DDL on the same DBAPI connection. Remove the ledger
    and that accident is gone, and a comment-led migration silently rolls back
    again — the original defect, verbatim."""
    from sqlalchemy import text

    import db.sql_migrations as module

    base = str(tmp_path)
    _write(base, "001_no_ledger.sql", "-- leading comment\nCREATE TABLE IF NOT EXISTS t_no_ledger (id int);\n")
    monkeypatch.setattr(module, "_ensure_ledger", lambda engine: False)

    result = module.run_sql_migrations(fresh_pg_engine, base_dir=base)
    assert result["failed"] == [], result["failed"]
    assert result["applied"] == ["001_no_ledger.sql"]

    with fresh_pg_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('t_no_ledger')")).scalar() == "t_no_ledger", (
            "the migration was rolled back when no ledger row was written"
        )
        # And no ledger table was created behind our back.
        assert conn.execute(text(f"SELECT to_regclass('{LEDGER_TABLE}')")).scalar() is None


@requires_pg
def test_concurrently_migration_commits_without_the_ledger(fresh_pg_engine, tmp_path, monkeypatch) -> None:
    """Same for the AUTOCOMMIT path, which records the ledger row separately."""
    from sqlalchemy import text

    import db.sql_migrations as module

    base = str(tmp_path)
    _write(
        base,
        "001_conc_no_ledger.sql",
        "-- concurrent\nCREATE TABLE IF NOT EXISTS t_cnl (v text);\n"
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_t_cnl ON t_cnl (v);\n",
    )
    monkeypatch.setattr(module, "_ensure_ledger", lambda engine: False)

    result = module.run_sql_migrations(fresh_pg_engine, base_dir=base)
    assert result["failed"] == [], result["failed"]
    with fresh_pg_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('idx_t_cnl')")).scalar() == "idx_t_cnl"


def test_split_statements_drops_comment_only_trailers() -> None:
    """_is_only_comments(): a trailing comment after the last semicolon must not
    become an empty statement handed to the driver."""
    parts = split_statements(
        "CREATE TABLE a (id int);\n-- trailing note\n/* and a block one */\n"
    )
    assert len(parts) == 1, parts
    assert parts[0].startswith("CREATE TABLE a")

    # A comment BETWEEN statements stays attached to the statement it precedes,
    # rather than being emitted on its own.
    parts = split_statements("CREATE TABLE a (id int);\n-- note\nCREATE TABLE b (id int);\n")
    assert len(parts) == 2, parts
    assert parts[1].lstrip().startswith("--")
