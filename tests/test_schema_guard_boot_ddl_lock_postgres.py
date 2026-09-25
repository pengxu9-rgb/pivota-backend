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

ISOLATION. The dialect gate runs every test_*_postgres.py file against ONE shared database, so
this file never touches a real table. Each test gets its own scratch schema holding its own
copies of the tables under test, and the guard runs on a pool whose search_path is that schema
ALONE: to_regclass() and every unqualified statement resolve there, exactly as they resolve to
`public` in production, and nothing falls through to the shared tables. The boot's own CREATE
TABLE IF NOT EXISTS statements land in the scratch schema too. It is dropped after the test.

THIS MODULE MUST NOT IMPORT `main` (the gate runs every test_*_postgres.py in one process).
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
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

# Same convention as the sibling gates: this creates and drops schemas, so it must be
# incapable of running anywhere but a throwaway database.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

_REPO = Path(__file__).resolve().parents[1]
_MIGRATIONS = _REPO / "db" / "migrations"

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
#: the CHECK with 'partner_invite'.
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
    tree = ast.parse((_REPO / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and f"CREATE TABLE IF NOT EXISTS {table}" in node.value
        ):
            return node.value
    raise AssertionError(f"main.py no longer creates {table} — this fixture is stale")


@dataclass
class _Scratch:
    """The scratch schema of one test, and the pool whose search_path resolves into it."""

    db: object
    schema: str

    def qualified(self, table: str) -> str:
        return f'"{self.schema}"."{table}"'


async def _create_model_tables(db, *tables) -> None:
    """Build model tables (and their indexes) with unqualified DDL, i.e. in the scratch schema."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    dialect = postgresql.dialect()
    for table in tables:
        await db.execute(str(CreateTable(table).compile(dialect=dialect)))
        for index in table.indexes:
            await db.execute(str(CreateIndex(index).compile(dialect=dialect)))


async def _apply(db, path: Path) -> None:
    from db.sql_migrations import split_statements

    for statement in split_statements(path.read_text(encoding="utf-8")):
        await db.execute(statement)


async def _make_needed(db, table: str) -> None:
    """Put the scratch copy of `table` in the shape its heal exists for."""
    if table == "merchant_audit_runs":
        await db.execute("DROP TABLE IF EXISTS merchant_audit_runs")
        await db.execute(_MERCHANT_AUDIT_RUNS_PRE_210)
    elif table == "partner_send_log":
        await db.execute("DROP TABLE IF EXISTS partner_send_log")
        await db.execute(_PARTNER_SEND_LOG_PRE_172)
    elif table == "tierb_cart_link_eligibility":
        await db.execute("DROP TABLE IF EXISTS tierb_cart_link_eligibility")
        await _apply(db, _MIGRATIONS / "228_tierb_cart_link_eligibility.sql")
    elif table == "merchant_onboarding":
        # Production before mig 164. The scratch copy is empty, so this cannot fail on rows.
        await db.execute("ALTER TABLE merchant_onboarding ALTER COLUMN store_url SET NOT NULL")
    elif table == "commerce_interactions":
        # Not the pre-204 type (unknown), but a type the heal must change.
        await db.execute(
            "ALTER TABLE commerce_interactions "
            "ALTER COLUMN checkout_id TYPE TEXT, ALTER COLUMN return_id TYPE TEXT"
        )
    else:
        raise AssertionError(f"no needed shape for {table}")


@pytest.fixture
async def _db(monkeypatch):
    from databases import Database

    import db.schema_guard as sg
    import db.tierb_cart_link_eligibility_schema as tierb_schema
    from db.commerce_interactions import commerce_interaction_events, commerce_interactions
    from db.database import database_kwargs
    from db.merchant_onboarding import merchant_onboarding
    from db.orders import orders

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in {dbname!r} — throwaway only")

    schema = f"bootlock_{uuid.uuid4().hex[:10]}"
    kwargs = dict(database_kwargs)
    kwargs["server_settings"] = {
        **dict(kwargs.get("server_settings") or {}),
        # The scratch schema ALONE: with `public` on the path, anything the boot touches that
        # this file did not copy would resolve to the gate's shared tables.
        "search_path": f'"{schema}"',
    }
    db = Database(DATABASE_URL, **kwargs)
    await db.connect()
    try:
        await db.execute(f'CREATE SCHEMA "{schema}"')
        assert await db.fetch_val("SELECT current_schema()") == schema, "search_path did not take"
        await _create_model_tables(
            db, orders, commerce_interactions, commerce_interaction_events, merchant_onboarding
        )
        # The guard's `UPDATE merchant_stores` is not in a try of its own: without main.py's
        # table (its `connected_at`) it raises and abandons every heal after it.
        await db.execute(_startup_ddl_for("merchant_stores"))
        for table in ("merchant_audit_runs", "partner_send_log", "tierb_cart_link_eligibility"):
            await _make_needed(db, table)
        monkeypatch.setattr(sg, "database", db)
        # The boot calls this helper for the tierb table; it has its own module-level pool.
        monkeypatch.setattr(tierb_schema, "database", db)
        yield _Scratch(db, schema)
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.disconnect()


async def _catalog(db, table: str) -> Dict[str, list]:
    """Everything a heal on `table` could change: columns, constraints, indexes."""
    columns = await db.fetch_all(
        """
        SELECT attname, format_type(atttypid, atttypmod) AS type, attnotnull
        FROM pg_attribute
        WHERE attrelid = to_regclass(:t) AND attnum > 0 AND NOT attisdropped
        ORDER BY attname
        """,
        {"t": table},
    )
    constraints = await db.fetch_all(
        """
        SELECT conname, pg_get_constraintdef(oid) AS def, convalidated
        FROM pg_constraint WHERE conrelid = to_regclass(:t) ORDER BY conname
        """,
        {"t": table},
    )
    indexes = await db.fetch_all(
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


async def _call_while_held(
    scratch: _Scratch, table: str, mode: str, call
) -> Tuple[bool, List[str]]:
    """Run `call()` while another session holds `mode` on the scratch copy of `table`.

    Returns (finished while the holder still held the lock, the queries the holder blocked).
    The holder lets go after _HOLDER_HOLDS_S whatever happens, so a regressed guard fails the
    test instead of hanging it.
    """
    import asyncpg

    holder = await asyncpg.connect(_asyncpg_dsn())
    watcher = await asyncpg.connect(_asyncpg_dsn())
    held = holder.transaction()
    await held.start()
    blocked: List[str] = []
    task = None
    try:
        await holder.execute(f"LOCK TABLE {scratch.qualified(table)} IN {mode} MODE")
        holder_pid = await holder.fetchval("SELECT pg_backend_pid()")
        started = time.monotonic()
        task = asyncio.ensure_future(call())
        while not task.done() and time.monotonic() - started < _HOLDER_HOLDS_S:
            rows = await watcher.fetch(
                "SELECT query FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))",
                holder_pid,
            )
            blocked.extend(" ".join(r["query"].split()) for r in rows)
            await asyncio.wait({task}, timeout=0.02)
        finished_while_held = task.done()
    finally:
        await held.rollback()
        await holder.close()
        await watcher.close()
        if task is not None:
            await task
    return finished_while_held, blocked


async def _boot_while_held(
    scratch: _Scratch, table: str, mode: str, monkeypatch
) -> Tuple[bool, List[str], _Recording]:
    """The boot guard, recorded, while another session holds `mode` on `table`."""
    import db.schema_guard as sg

    recording = _Recording(scratch.db)
    monkeypatch.setattr(sg, "database", recording)
    try:
        finished_while_held, blocked = await _call_while_held(
            scratch, table, mode, sg.ensure_required_schema_light
        )
    finally:
        monkeypatch.setattr(sg, "database", scratch.db)
    return finished_while_held, blocked, recording


_NEEDED = sorted(_HEALS)


@pytest.mark.parametrize("table", _NEEDED)
async def test_a_boot_does_not_queue_behind_a_reader_once_the_table_is_healed(
    _db, table, monkeypatch
):
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()  # the first boot heals; every boot after is this one

    finished_while_held, blocked, ran = await _boot_while_held(
        _db, table, "ACCESS SHARE", monkeypatch
    )

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

    finished_while_held, blocked, ran = await _boot_while_held(
        _db, table, "ROW EXCLUSIVE", monkeypatch
    )

    guards = ran.index_guards_of(table)
    assert guards, f"positive control: the boot ran {table}'s index guards: {guards}"
    assert [error for _, error in guards] == [None] * len(guards), guards
    assert blocked == [], f"the boot queued behind a writer of {table}: {blocked}"
    assert finished_while_held, f"the boot finished only once the writer let go of {table}"


@pytest.mark.parametrize("table", _NEEDED)
async def test_a_needed_heal_gives_up_after_the_lock_timeout_and_the_next_boot_runs_it(
    _db, table, monkeypatch
):
    from db.schema_guard import HEAL_LOCK_TIMEOUT, ensure_required_schema_light

    assert HEAL_LOCK_TIMEOUT == "500ms"
    await ensure_required_schema_light()
    healed = await _catalog(_db.db, table)
    await _make_needed(_db.db, table)
    needed = await _catalog(_db.db, table)
    assert needed != healed, f"precondition: {table} really is back in its pre-heal shape"

    finished_while_held, blocked, ran = await _boot_while_held(
        _db, table, "ACCESS SHARE", monkeypatch
    )

    # The heal DID try (it is needed), so it waited — for its lock_timeout only.
    assert any(table in q for q in blocked), "precondition: the heal attempted the lock"
    errors = ran.guard_of(table)
    assert errors and all(e is not None and "lock timeout" in e for e in errors), errors
    assert finished_while_held, "the heal waited for the reader instead of its lock_timeout"
    # One deferred heal does not cost the rest of the boot: the boot's last guarded heal
    # (merchant_onboarding's) still ran, cleanly, unless it is the one deferred here.
    if table != "merchant_onboarding":
        assert ran.guard_of("merchant_onboarding") == [None], "the heals after it did not run"
    after_timeout = await _catalog(_db.db, table)
    assert after_timeout["columns"] == needed["columns"]
    assert after_timeout["constraints"] == needed["constraints"], (
        "a timed-out DROP + ADD must not leave the table without its CHECK"
    )

    await ensure_required_schema_light()  # the next boot, no reader: heals it
    assert await _catalog(_db.db, table) == healed


@pytest.mark.parametrize("table", _NEEDED)
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

    await sg.ensure_required_schema_light()  # every other table healed, so only `table` moves
    await _make_needed(_db.db, table)
    before = await _catalog(_db.db, table)
    monkeypatch.setattr(sg, "guarded_ddl", spy)
    await sg.ensure_required_schema_light()
    guarded = await _catalog(_db.db, table)
    assert len(ddl) == _HEALS[table], "positive control: the boot built the guarded heal"
    assert guarded != before, f"the boot did not heal {table}"

    await _make_needed(_db.db, table)
    assert await _catalog(_db.db, table) == before
    monkeypatch.setattr(sg, "guarded_ddl", bare)
    await sg.ensure_required_schema_light()
    assert await _catalog(_db.db, table) == guarded

    # And the next boot finds nothing to do: the guard reads the healed catalog as healed.
    monkeypatch.setattr(sg, "guarded_ddl", original)
    finished_while_held, blocked, ran = await _boot_while_held(
        _db, table, "ACCESS SHARE", monkeypatch
    )
    assert finished_while_held and blocked == [] and ran.guard_of(table) == [None] * _HEALS[table]


async def _indexdef(db, name: str) -> List[str]:
    rows = await db.fetch_all(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :n",
        {"n": name},
    )
    return [r["indexdef"] for r in rows]


async def test_a_needed_index_waits_for_the_next_boot_instead_of_for_a_writer(_db, monkeypatch):
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()
    [bare] = await _indexdef(_db.db, "idx_commerce_interactions_store")
    await _db.db.execute("DROP INDEX idx_commerce_interactions_store")

    finished_while_held, blocked, ran = await _boot_while_held(
        _db, "commerce_interactions", "ROW EXCLUSIVE", monkeypatch
    )

    assert any("commerce_interactions" in q for q in blocked), "precondition: the build tried"
    outcome = dict(ran.index_guards_of("commerce_interactions"))
    assert "lock timeout" in (outcome["idx_commerce_interactions_store"] or ""), outcome
    # One deferred build does not cost the rest of the boot.
    assert outcome["idx_commerce_interactions_store_session"] is None, outcome
    assert ran.guard_of("merchant_onboarding") == [None], "the heals after it still ran"
    assert finished_while_held, "the build waited for the writer instead of its lock_timeout"

    await ensure_required_schema_light()  # the next boot, no writer: builds it, as it was
    assert await _indexdef(_db.db, "idx_commerce_interactions_store") == [bare]


async def test_an_index_of_the_same_name_in_another_schema_does_not_stop_the_build(_db):
    """IF NOT EXISTS looks for the name in the TABLE's schema, not along search_path; so must
    the guard, or a same-named relation anywhere else would stop a build the bare statement
    ran."""
    from db.schema_guard import ensure_required_schema_light

    elsewhere = f"{_db.schema}_elsewhere"
    await ensure_required_schema_light()
    await _db.db.execute("DROP INDEX idx_commerce_interactions_store")
    await _db.db.execute(f'CREATE SCHEMA "{elsewhere}"')
    try:
        await _db.db.execute(f'CREATE TABLE "{elsewhere}".t (a INT)')
        await _db.db.execute(f'CREATE INDEX idx_commerce_interactions_store ON "{elsewhere}".t (a)')

        await ensure_required_schema_light()

        assert len(await _indexdef(_db.db, "idx_commerce_interactions_store")) == 1, (
            "the boot did not rebuild it in the table's own schema"
        )
    finally:
        await _db.db.execute(f'DROP SCHEMA IF EXISTS "{elsewhere}" CASCADE')


async def test_a_not_valid_check_with_the_right_text_is_still_re_added(_db):
    """The bare DROP + ADD left a VALIDATED check every boot. A NOT VALID one with the same text
    (a hand repair, a half-finished migration) is not yet what it left, so the heal still runs."""
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()
    await _db.db.execute("ALTER TABLE partner_send_log DROP CONSTRAINT ck_partner_send_log_template")
    await _db.db.execute(
        "ALTER TABLE partner_send_log ADD CONSTRAINT ck_partner_send_log_template CHECK ("
        "template_id IN ('settlement_monthly', 'settlement_skipped', 'settlement_failed_notice', "
        "'partner_invite')) NOT VALID"
    )
    validated = (
        "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_partner_send_log_template' "
        "AND conrelid = to_regclass('partner_send_log')"
    )
    assert await _db.db.fetch_val(validated) is False, "precondition"

    await ensure_required_schema_light()

    assert await _db.db.fetch_val(validated) is True


# The same hazard off the boot path: self-heals a request runs, lazily once per process
# (orders, quotes) or after any failed write (checkout_intents, acp_checkout_sessions).
# (table, module, ensure function, the module's per-process "done" flag or None)
_REQUEST_PATH_HEALS = [
    ("orders", "db.orders", "_ensure_client_secret_storage_allows_long_values",
     "_client_secret_storage_ready"),
    ("quotes", "db.quotes", "ensure_quotes_table", "_QUOTES_DDL_READY"),
    ("checkout_intents", "routes.agent_checkout_intents", "_ensure_checkout_intents_table", None),
    ("acp_checkout_sessions", "services.acp_checkout_session_service",
     "_ensure_acp_checkout_sessions_table", None),
]


# ACCESS SHARE (a reader) conflicts with the column heals' ACCESS EXCLUSIVE; ROW EXCLUSIVE (a
# writer) also conflicts with the index builds' SHARE, which a reader does not.
@pytest.mark.parametrize("mode", ["ACCESS SHARE", "ROW EXCLUSIVE"])
@pytest.mark.parametrize("table, module_name, ensure, flag", _REQUEST_PATH_HEALS)
async def test_a_request_path_self_heal_does_not_queue_behind_a_reader_or_writer(
    _db, table, module_name, ensure, flag, mode, monkeypatch
):
    import importlib

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "database", _db.db)
    if hasattr(module, "metadata"):
        # The SQLite path (create_all on the module's sync engine) would build the table in
        # `public` of the shared gate database; the Postgres DDL under test is what follows it.
        monkeypatch.setattr(module.metadata, "create_all", lambda *a, **k: None)
    if table == "checkout_intents":
        from db.checkout_intents import checkout_intents

        await _create_model_tables(_db.db, checkout_intents)
    if flag:
        monkeypatch.setattr(module, flag, False)
    await getattr(module, ensure)()  # the first call heals; every later call is this one
    if flag:
        assert getattr(module, flag) is True, "precondition: the first call healed"
        monkeypatch.setattr(module, flag, False)
    assert await _db.db.fetch_val(
        "SELECT relnamespace::regnamespace::text FROM pg_class WHERE oid = to_regclass(:t)",
        {"t": table},
    ) == _db.schema, "precondition: the heal resolves to the scratch copy"

    recording = _Recording(_db.db)
    monkeypatch.setattr(module, "database", recording)
    finished_while_held, blocked = await _call_while_held(
        _db, table, mode, getattr(module, ensure)
    )

    assert blocked == [], f"the self-heal queued behind a {mode} holder of {table}: {blocked}"
    assert finished_while_held, f"the self-heal finished only once the holder let go of {table}"
    heals = [(s, e) for s, e in recording.ran if s.startswith("DO $") and f"'{table}'" in s]
    assert heals, f"positive control: the call ran its guarded {table} heals"
    assert [e for _, e in heals] == [None] * len(heals), heals
    if flag:
        assert getattr(module, flag) is True
