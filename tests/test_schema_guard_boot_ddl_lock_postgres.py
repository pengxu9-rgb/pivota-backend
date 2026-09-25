"""A boot must not queue behind a reader, or a writer, of a table it has nothing to change on.

    TZ=UTC DATABASE_URL=postgresql://postgres@127.0.0.1:5432/pivota_bootlock_ddl_test \\
        .venv/bin/python -m pytest tests/test_schema_guard_boot_ddl_lock_postgres.py

WHAT WAS WRONG. tests/test_schema_guard_boot_lock_postgres.py pins the column heals. The heals
that are NOT column adds ran bare on every boot too, each taking ACCESS EXCLUSIVE with no
lock_timeout although it changes nothing after its first run:

  * commerce_interactions  ALTER COLUMN checkout_id/order_id/refund_id/return_id TYPE VARCHAR(128)
  * merchant_audit_runs    ALTER COLUMN merchant_id DROP NOT NULL
  * merchant_onboarding    ALTER COLUMN store_url DROP NOT NULL
  * partner_send_log       DROP + ADD CONSTRAINT ck_partner_send_log_template (two locks, a
                           validating scan, and a window with no CHECK: each committed alone)
  * tierb_cart_link_eligibility  the same DROP + ADD pair for verdict / previous_verdict

and every `CREATE INDEX IF NOT EXISTS` took the table's SHARE lock before it looked for the
name, which queues behind any writer and blocks every later writer.

WHAT THIS PINS, on the production dialect, because only Postgres has the lock:
  * healed + a reader holding the table -> the boot finishes WHILE the reader holds it, no
    backend is blocked by the reader, and (positive control) the boot did run that table's
    guarded heal, which ended cleanly;
  * the indexes present + a WRITER holding the table -> the same, for the index guards;
  * a heal still needed + a reader -> the boot gives up after the lock_timeout instead of
    waiting, and the next boot runs it;
  * a heal still needed, no reader -> the guarded heal leaves exactly the catalog the bare
    statement left.

THIS MODULE MUST NOT IMPORT `main` (the gate runs every test_*_postgres.py in one process). It
never DROPs a table another gate file relies on finding: it builds `orders`, the two
commerce tables and `merchant_stores` only when missing, `merchant_onboarding` from its model only when missing, and
DROPs only the tables every other user of them DROPs and rebuilds first (merchant_audit_runs,
tierb_cart_link_eligibility) or that nothing else in the gate touches (partner_send_log).
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — a lock queue exists only on the production dialect",
)

assert "main" not in sys.modules or not _IS_PG, (
    "main is imported: it registers every model on the shared metadata at collection time "
    "and poisons later create_all calls in the Postgres gate"
)

_MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"

#: How long the lock holder keeps its lock before this harness lets go on its own. Far above
#: the guard's lock_timeout, so a boot that finishes only after this was waiting on the holder.
_HOLDER_HOLDS_S = 6.0

#: merchant_audit_runs as production had it before mig 210 (merchant_id NOT NULL), plus the
#: columns the guard's own index heals on it name, so every one of them can build.
_MERCHANT_AUDIT_RUNS_PRE_210 = """
    CREATE TABLE merchant_audit_runs (
        run_id UUID PRIMARY KEY,
        merchant_id TEXT NOT NULL,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        status TEXT NOT NULL,
        stage TEXT,
        subject_type TEXT,
        idempotency_key TEXT,
        partial_result_jsonb JSONB
    )
"""

#: partner_send_log's template column and CHECK as migration 137 built them, before 172 widened
#: the CHECK with 'partner_invite'. Nothing else in the gate uses the table, so this file owns it.
_PARTNER_SEND_LOG_PRE_172 = """
    CREATE TABLE partner_send_log (
        id BIGSERIAL PRIMARY KEY,
        template_id TEXT NOT NULL,
        CONSTRAINT ck_partner_send_log_template CHECK (template_id IN (
            'settlement_monthly',
            'settlement_skipped',
            'settlement_failed_notice'
        ))
    )
"""

#: How many guarded heals the boot runs per table (tierb: verdict and previous_verdict).
_HEALS = {
    "commerce_interactions": 1,
    "merchant_audit_runs": 1,
    "merchant_onboarding": 1,
    "partner_send_log": 1,
    "tierb_cart_link_eligibility": 2,
}


def _asyncpg_dsn() -> str:
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


def _startup_ddl_for(table: str) -> str:
    """main.py's own `CREATE TABLE IF NOT EXISTS <table>` literal, by AST — the technique
    tests/test_store_sync_columns_postgres.py uses: `merchant_stores` is created by application
    bootstrap, not by a migration or by `metadata`."""
    tree = ast.parse((_MIGRATIONS.parents[1] / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and f"CREATE TABLE IF NOT EXISTS {table}" in node.value
        ):
            return node.value
    raise AssertionError(f"main.py no longer creates {table} — this fixture is stale")


async def _apply(path: Path) -> None:
    from db.database import database
    from db.sql_migrations import split_statements

    for statement in split_statements(path.read_text(encoding="utf-8")):
        await database.execute(statement)


async def _make_needed(table: str) -> None:
    """Put `table` back in the shape its heal exists for (production's pre-migration shape)."""
    from db.database import database

    if table == "merchant_audit_runs":
        await database.execute("DROP TABLE IF EXISTS merchant_audit_runs")
        await database.execute(_MERCHANT_AUDIT_RUNS_PRE_210)
    elif table == "partner_send_log":
        await database.execute("DROP TABLE IF EXISTS partner_send_log")
        await database.execute(_PARTNER_SEND_LOG_PRE_172)
    elif table == "tierb_cart_link_eligibility":
        await database.execute("DROP TABLE IF EXISTS tierb_cart_link_eligibility")
        await _apply(_MIGRATIONS / "228_tierb_cart_link_eligibility.sql")
    elif table == "commerce_interactions":
        # Not the pre-204 type (unknown), but a type the heal must change, reached without a
        # rewrite and without depending on the rows other gate files left in the table.
        await database.execute(
            "ALTER TABLE commerce_interactions "
            "ALTER COLUMN checkout_id TYPE TEXT, ALTER COLUMN return_id TYPE TEXT"
        )
    else:
        raise AssertionError(f"no needed shape for {table}")


@pytest.fixture
async def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.commerce_interactions import commerce_interaction_events, commerce_interactions
    from db.database import database, metadata
    from db.merchant_onboarding import merchant_onboarding
    from db.orders import orders

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not dbname.endswith("_test"):
        pytest.skip(f"refusing to rebuild tables in {dbname!r} — throwaway *_test only")

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    try:
        await database.execute(str(CreateTable(orders).compile(dialect=postgresql.dialect())))
    except Exception:
        # Another gate file already built it from the same model. Never DROP it.
        pass
    engine = create_engine(_asyncpg_dsn())
    try:
        metadata.create_all(
            engine,
            tables=[commerce_interactions, commerce_interaction_events, merchant_onboarding],
            checkfirst=True,
        )
    finally:
        engine.dispose()
    # The guard's `UPDATE merchant_stores` is not in a try of its own: on a database without
    # the table it raises and abandons every heal after it, merchant_onboarding's included.
    # Production has the table (main.py's bootstrap builds it); so does this fixture.
    await database.execute(_startup_ddl_for("merchant_stores"))
    for table in ("merchant_audit_runs", "partner_send_log", "tierb_cart_link_eligibility"):
        await _make_needed(table)
    yield database
    await database.execute("DROP TABLE IF EXISTS partner_send_log")
    await database.execute("DROP TABLE IF EXISTS merchant_audit_runs")
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _catalog(table: str) -> Dict[str, list]:
    """Everything a heal on `table` could change: columns, constraints, indexes."""
    from db.database import database

    columns = await database.fetch_all(
        """
        SELECT attname, format_type(atttypid, atttypmod) AS type, attnotnull
        FROM pg_attribute
        WHERE attrelid = to_regclass(:t) AND attnum > 0 AND NOT attisdropped
        ORDER BY attname
        """,
        {"t": table},
    )
    constraints = await database.fetch_all(
        """
        SELECT conname, pg_get_constraintdef(oid) AS def, convalidated
        FROM pg_constraint WHERE conrelid = to_regclass(:t) ORDER BY conname
        """,
        {"t": table},
    )
    indexes = await database.fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = current_schema() AND tablename = :t ORDER BY indexname",
        {"t": table},
    )
    return {
        "columns": [tuple(r.values()) for r in columns],
        "constraints": [tuple(r.values()) for r in constraints],
        "indexes": [tuple(r.values()) for r in indexes],
    }


class _Recording:
    """The guard's `database`, recording every statement it runs and how it ended. The
    positive control: a boot that never reaches a table's heal cannot queue behind that
    table's lock holder either, so "nothing was blocked" means nothing without it."""

    def __init__(self, database) -> None:
        self._database = database
        self.ran: List[Tuple[str, Optional[str]]] = []

    def __getattr__(self, name):
        return getattr(self._database, name)

    async def execute(self, query, *args, **kwargs):
        statement = " ".join(str(query).split())
        try:
            result = await self._database.execute(query, *args, **kwargs)
        except Exception as exc:
            self.ran.append((statement, str(exc)))
            raise
        self.ran.append((statement, None))
        return result

    def guard_of(self, table: str) -> List[Optional[str]]:
        """How each run of `table`'s guarded (non column-add) heal ended: None, or the error."""
        return [
            error
            for statement, error in self.ran
            if statement.startswith("DO $guard$") and f"to_regclass('{table}') IS NOT NULL" in statement
        ]

    def index_guards_of(self, table: str) -> List[Tuple[str, Optional[str]]]:
        """(index name, how it ended) for each guarded index build on `table`."""
        out = []
        for statement, error in self.ran:
            if statement.startswith("DO $index$") and f"to_regclass('{table}')" in statement:
                out.append((re.search(r"relname = '(\w+)'", statement).group(1), error))
        return out


async def _boot_while_held(table: str, mode: str, monkeypatch) -> Tuple[bool, List[str], _Recording]:
    """Run the boot guard while another session holds `mode` on `table`.

    Returns (finished while the holder still held the lock, the queries the holder blocked,
    what the boot ran). The holder lets go after _HOLDER_HOLDS_S whatever happens, so a
    regressed guard fails this test instead of hanging it.
    """
    import asyncpg

    import db.schema_guard as sg

    recording = _Recording(sg.database)
    monkeypatch.setattr(sg, "database", recording)
    holder = await asyncpg.connect(_asyncpg_dsn())
    watcher = await asyncpg.connect(_asyncpg_dsn())
    held = holder.transaction()
    await held.start()
    blocked: List[str] = []
    boot = None
    try:
        await holder.execute(f"LOCK TABLE {table} IN {mode} MODE")
        holder_pid = await holder.fetchval("SELECT pg_backend_pid()")
        started = time.monotonic()
        boot = asyncio.ensure_future(sg.ensure_required_schema_light())
        while not boot.done() and time.monotonic() - started < _HOLDER_HOLDS_S:
            rows = await watcher.fetch(
                "SELECT query FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))",
                holder_pid,
            )
            blocked.extend(" ".join(r["query"].split()) for r in rows)
            await asyncio.wait({boot}, timeout=0.02)
        finished_while_held = boot.done()
    finally:
        await held.rollback()
        await holder.close()
        await watcher.close()
        if boot is not None:
            await boot
    monkeypatch.setattr(sg, "database", recording._database)
    return finished_while_held, blocked, recording


@pytest.mark.parametrize("table", sorted(_HEALS))
async def test_a_boot_does_not_queue_behind_a_reader_once_the_table_is_healed(
    _db, table, monkeypatch
):
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()  # the first boot heals; every boot after is this one

    finished_while_held, blocked, ran = await _boot_while_held(table, "ACCESS SHARE", monkeypatch)

    assert ran.guard_of(table) == [None] * _HEALS[table], (
        f"positive control: the boot ran the {table} guarded heal {_HEALS[table]}x, cleanly"
    )
    assert blocked == [], f"the boot queued behind a reader of {table}: {blocked}"
    assert finished_while_held, f"the boot finished only once the reader let go of {table}"


@pytest.mark.parametrize(
    "table", ["commerce_interactions", "merchant_audit_runs", "merchant_onboarding"]
)
async def test_a_boot_does_not_queue_behind_a_writer_once_the_indexes_exist(
    _db, table, monkeypatch
):
    """CREATE INDEX takes SHARE, which a reader does not conflict with and a writer does."""
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()

    finished_while_held, blocked, ran = await _boot_while_held(table, "ROW EXCLUSIVE", monkeypatch)

    guards = ran.index_guards_of(table)
    assert guards, f"positive control: the boot ran {table}'s index guards: {guards}"
    assert [error for _, error in guards] == [None] * len(guards), guards
    assert blocked == [], f"the boot queued behind a writer of {table}: {blocked}"
    assert finished_while_held, f"the boot finished only once the writer let go of {table}"


@pytest.mark.parametrize(
    "table",
    ["commerce_interactions", "merchant_audit_runs", "partner_send_log", "tierb_cart_link_eligibility"],
)
async def test_a_needed_heal_gives_up_after_the_lock_timeout_and_the_next_boot_runs_it(
    _db, table, monkeypatch
):
    from db.schema_guard import HEAL_LOCK_TIMEOUT, ensure_required_schema_light

    assert HEAL_LOCK_TIMEOUT == "500ms"
    await ensure_required_schema_light()
    healed = await _catalog(table)
    await _make_needed(table)
    needed = await _catalog(table)
    assert needed != healed, f"precondition: {table} really is back in its pre-heal shape"

    finished_while_held, blocked, ran = await _boot_while_held(table, "ACCESS SHARE", monkeypatch)

    # The heal DID try (it is needed), so it waited — for its lock_timeout only.
    assert any(table in q for q in blocked), "precondition: the heal attempted the lock"
    errors = ran.guard_of(table)
    assert errors and all(e is not None and "lock timeout" in e for e in errors), errors
    assert finished_while_held, "the heal waited for the reader instead of its lock_timeout"
    # One deferred heal does not cost the rest of the boot (merchant_onboarding's comes after
    # every table parametrized here).
    assert ran.guard_of("merchant_onboarding") == [None], "the heals after it did not run"
    after_timeout = await _catalog(table)
    assert after_timeout["columns"] == needed["columns"]
    assert after_timeout["constraints"] == needed["constraints"], (
        "a timed-out DROP + ADD must not leave the table without its CHECK"
    )

    await ensure_required_schema_light()  # the next boot, no reader: heals it
    assert await _catalog(table) == healed


@pytest.mark.parametrize(
    "table",
    ["commerce_interactions", "merchant_audit_runs", "partner_send_log", "tierb_cart_link_eligibility"],
)
async def test_a_needed_heal_leaves_the_catalog_the_bare_statement_left(_db, table, monkeypatch):
    """"Keep behaviour identical when the change is actually needed": from the same pre-heal
    shape, a boot running the guarded heal and a boot running the same DDL unconditionally (as
    it ran before) leave the same catalog."""
    import db.schema_guard as sg

    ddl: List[str] = []
    original = sg.guarded_ddl

    def spy(guarded_table: str, needed: str, statement: str) -> str:
        if guarded_table == table:
            ddl.append(statement)
        return original(guarded_table, needed, statement)

    def bare(guarded_table: str, needed: str, statement: str) -> str:
        if guarded_table == table:
            return f"DO $bare$ BEGIN {statement} END $bare$;"
        return original(guarded_table, needed, statement)

    await _make_needed(table)
    before = await _catalog(table)
    monkeypatch.setattr(sg, "guarded_ddl", spy)
    await sg.ensure_required_schema_light()
    guarded = await _catalog(table)
    assert len(ddl) == _HEALS[table], "positive control: the boot built the guarded heal"
    assert guarded != before, f"the boot did not heal {table}"

    await _make_needed(table)
    assert await _catalog(table) == before
    monkeypatch.setattr(sg, "guarded_ddl", bare)
    await sg.ensure_required_schema_light()
    assert await _catalog(table) == guarded

    # And the next boot finds nothing to do: the guard reads the healed catalog as healed.
    monkeypatch.setattr(sg, "guarded_ddl", original)
    finished_while_held, blocked, ran = await _boot_while_held(table, "ACCESS SHARE", monkeypatch)
    assert finished_while_held and blocked == [] and ran.guard_of(table) == [None] * _HEALS[table]


async def test_a_needed_index_waits_for_the_next_boot_instead_of_for_a_writer(_db, monkeypatch):
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()
    [[bare]] = [
        r.values()
        for r in await _db.fetch_all(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_commerce_interactions_store'"
        )
    ]
    await _db.execute("DROP INDEX idx_commerce_interactions_store")

    finished_while_held, blocked, ran = await _boot_while_held(
        "commerce_interactions", "ROW EXCLUSIVE", monkeypatch
    )

    assert any("commerce_interactions" in q for q in blocked), "precondition: the build tried"
    outcome = dict(ran.index_guards_of("commerce_interactions"))
    assert "lock timeout" in (outcome["idx_commerce_interactions_store"] or ""), outcome
    # One deferred build does not cost the rest of the boot.
    assert outcome["idx_commerce_interactions_store_session"] is None, outcome
    assert ran.guard_of("merchant_onboarding") == [None], "the heals after it still ran"
    assert finished_while_held, "the build waited for the writer instead of its lock_timeout"

    await ensure_required_schema_light()  # the next boot, no writer: builds it, as it was
    [[rebuilt]] = [
        r.values()
        for r in await _db.fetch_all(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_commerce_interactions_store'"
        )
    ]
    assert rebuilt == bare


async def test_the_store_url_heal_the_boot_sends_runs_on_a_not_null_table(_db, monkeypatch):
    """merchant_onboarding cannot be put back in its NOT NULL shape in this database: other gate
    files leave rows with no store_url. So the exact statement the boot sends is replayed on an
    empty copy of the table in a scratch schema that search_path resolves first — the same name
    resolution to_regclass() and the ALTER make in production."""
    import asyncpg

    import db.schema_guard as sg

    sent: List[str] = []
    original = sg.guarded_ddl

    def spy(guarded_table: str, needed: str, statement: str) -> str:
        built = original(guarded_table, needed, statement)
        if guarded_table == "merchant_onboarding":
            sent.append(built)
        return built

    monkeypatch.setattr(sg, "guarded_ddl", spy)
    await sg.ensure_required_schema_light()
    [statement] = sent

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute("DROP SCHEMA IF EXISTS bootlock_scratch CASCADE")
        await conn.execute("CREATE SCHEMA bootlock_scratch")
        await conn.execute(
            "CREATE TABLE bootlock_scratch.merchant_onboarding "
            "(LIKE public.merchant_onboarding INCLUDING ALL)"
        )
        await conn.execute(
            "ALTER TABLE bootlock_scratch.merchant_onboarding ALTER COLUMN store_url SET NOT NULL"
        )
        not_null = (
            "SELECT attnotnull FROM pg_attribute WHERE attname = 'store_url' "
            "AND attrelid = 'bootlock_scratch.merchant_onboarding'::regclass"
        )
        assert await conn.fetchval(not_null) is True, "precondition"
        await conn.execute("SET search_path = bootlock_scratch, public")
        await conn.execute(statement)
        assert await conn.fetchval(not_null) is False, "the boot's heal did not DROP NOT NULL"
    finally:
        await conn.execute("RESET search_path")
        await conn.execute("DROP SCHEMA IF EXISTS bootlock_scratch CASCADE")
        await conn.close()


async def test_an_index_of_the_same_name_in_another_schema_does_not_stop_the_build(_db):
    """IF NOT EXISTS looks for the name in the TABLE's schema, not along search_path; so must
    the guard, or a same-named relation anywhere else would stop a build the bare statement
    ran."""
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()
    await _db.execute("DROP INDEX idx_commerce_interactions_store")
    await _db.execute("DROP SCHEMA IF EXISTS bootlock_elsewhere CASCADE")
    await _db.execute("CREATE SCHEMA bootlock_elsewhere")
    try:
        await _db.execute("CREATE TABLE bootlock_elsewhere.t (a INT)")
        await _db.execute("CREATE INDEX idx_commerce_interactions_store ON bootlock_elsewhere.t (a)")

        await ensure_required_schema_light()

        rows = await _db.fetch_all(
            "SELECT schemaname FROM pg_indexes WHERE indexname = 'idx_commerce_interactions_store' "
            "AND tablename = 'commerce_interactions'"
        )
        assert [r["schemaname"] for r in rows] == ["public"], "the boot did not rebuild it"
    finally:
        await _db.execute("DROP SCHEMA IF EXISTS bootlock_elsewhere CASCADE")


async def test_a_not_valid_check_with_the_right_text_is_still_re_added(_db):
    """The bare DROP + ADD left a VALIDATED check every boot. A NOT VALID one with the same text
    (a hand repair, a half-finished migration) is not yet what it left, so the heal still runs."""
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()
    await _db.execute("ALTER TABLE partner_send_log DROP CONSTRAINT ck_partner_send_log_template")
    await _db.execute(
        "ALTER TABLE partner_send_log ADD CONSTRAINT ck_partner_send_log_template CHECK ("
        "template_id IN ('settlement_monthly', 'settlement_skipped', 'settlement_failed_notice', "
        "'partner_invite')) NOT VALID"
    )
    validated = (
        "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_partner_send_log_template'"
    )
    assert await _db.fetch_val(validated) is False, "precondition"

    await ensure_required_schema_light()

    assert await _db.fetch_val(validated) is True
