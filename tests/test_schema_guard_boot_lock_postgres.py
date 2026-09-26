"""A boot must not queue behind a reader of a hot table.

    TZ=UTC DATABASE_URL=postgresql://postgres@127.0.0.1:5432/pivota_bootlock_test \\
        .venv/bin/python -m pytest tests/test_schema_guard_boot_lock_postgres.py

WHAT WAS WRONG. ensure_required_schema_light() ran its column heals bare, on every boot.
`ADD COLUMN IF NOT EXISTS` takes the table's ACCESS EXCLUSIVE lock BEFORE it finds the
column already there, and nothing set a lock_timeout. So a boot that arrived while any
transaction held even ACCESS SHARE on `orders` waited for it, and every later reader and
writer of `orders` (every checkout) queued behind the boot. Measured 2026-09-25 on PG15:
more than 20s stalled, pg_stat_activity showing the orders ALTER waiting on Lock.

WHAT THIS PINS, on the production dialect, because only Postgres has the lock:
  * columns present + a reader holding the table -> the boot finishes WHILE the reader
    still holds it, and no backend is ever blocked by the reader;
  * a column missing + a reader holding the table -> the boot gives up on that heal after
    the lock_timeout instead of waiting, and the next boot heals it;
  * a column missing, no reader -> the heal builds exactly what the bare ALTER built.

EVERY TEST RUNS THE GUARD IN A SCRATCH SCHEMA. The dialect gate runs every
test_*_postgres.py against ONE shared database, and these tests drop columns and rebuild
tables. So each test creates `bootlock_<hex>`, and gives the guard (and the tierb helper it
calls) a pool whose search_path is that schema ALONE: every unqualified name the guard
creates, alters or resolves through to_regclass lands there, none can fall through to
`public`, and the schema is dropped afterwards. It also means the guard sees the same empty
schema locally and in CI.

THIS MODULE MUST NOT IMPORT `main` (the gate runs every test_*_postgres.py in one process;
see tests/test_merchant_purchasability_postgres.py).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from typing import List, Optional, Tuple

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

#: How long the reader holds its lock before this harness lets go on its own. Far above the
#: heal's lock_timeout, so a boot that finishes only after this was waiting on the reader.
_READER_HOLDS_S = 6.0

#: merchant_psps as main.py's heavy init creates it, before the heal's six columns.
_MERCHANT_PSPS_BASE = """
    CREATE TABLE IF NOT EXISTS merchant_psps (
        psp_id VARCHAR(50) PRIMARY KEY,
        merchant_id VARCHAR(50) NOT NULL,
        provider VARCHAR(50) NOT NULL,
        name VARCHAR(255) NOT NULL,
        api_key TEXT,
        account_id VARCHAR(255),
        capabilities TEXT,
        status VARCHAR(50) DEFAULT 'active',
        connected_at TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    )
"""
_MERCHANT_PSPS_HEALED = (
    "secret_key", "environment", "provider_config",
    "validation_status", "validation_error", "last_validated_at",
)
_ORDERS_HEALED = ("buyer_id", "intent_id", "agent_user_ref", "agent_scoped_buyer_ref")


_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _asyncpg_dsn() -> str:
    from db.database import DATABASE_URL as normalized

    return normalized.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture
async def _db(monkeypatch):
    """A pool pinned to a fresh scratch schema, installed as the guard's `database`."""
    import uuid

    import asyncpg
    from databases import Database
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import db.schema_guard as sg
    import db.tierb_cart_link_eligibility_schema as tierb
    from db.commerce_interactions import commerce_interaction_events, commerce_interactions
    from db.database import DATABASE_URL as normalized
    from db.orders import orders

    if not any(m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip("throwaway databases only (see _SAFE_DB_MARKERS)")

    schema = f"bootlock_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_asyncpg_dsn())
    await admin.execute(f"CREATE SCHEMA {schema}")
    scratch = Database(normalized, server_settings={"search_path": schema})
    try:
        await scratch.connect()
        monkeypatch.setattr(sg, "database", scratch)
        monkeypatch.setattr(tierb, "database", scratch)
        assert await scratch.fetch_val("SELECT current_schema()") == schema
        # The guard's commerce_interactions block is not in a try of its own: without the
        # table its CREATE INDEX raises and abandons every heal after it, orders and
        # merchant_psps included. Production has both; build them, and orders, from the
        # repo's own models.
        for table in (orders, commerce_interactions, commerce_interaction_events):
            await scratch.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
        await scratch.execute(_MERCHANT_PSPS_BASE)
        yield scratch
    finally:
        if scratch.is_connected:
            await scratch.disconnect()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


async def _columns(table: str) -> List[Tuple]:
    import db.schema_guard as sg

    rows = await sg.database.fetch_all(
        """
        SELECT column_name, data_type, character_maximum_length, column_default, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = :t
        ORDER BY column_name
        """,
        {"t": table},
    )
    keys = ("column_name", "data_type", "character_maximum_length", "column_default", "is_nullable")
    return [tuple(r[k] for k in keys) for r in rows]


class _Recording:
    """The guard's `database`, recording every statement it runs and how it ended. The
    positive control: a boot that never reaches a table's heal cannot queue behind that
    table's reader either, so "nothing was blocked" means nothing without it."""

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

    def heal_of(self, table: str) -> List[Optional[str]]:
        """How each run of `table`'s column heal ended: None, or the error."""
        pattern = re.compile(rf"ALTER TABLE IF EXISTS {table}\b ADD COLUMN")
        return [error for statement, error in self.ran if pattern.search(statement)]


async def _boot_while_a_reader_holds(
    table: str, monkeypatch
) -> Tuple[bool, List[str], _Recording]:
    """Run the boot guard while another session holds ACCESS SHARE on `table`.

    Returns (finished while the reader still held the lock, the queries the reader blocked,
    what the boot ran). The reader lets go after _READER_HOLDS_S whatever happens, so a
    regressed guard fails this test instead of hanging it.
    """
    import asyncpg

    import db.schema_guard as sg

    schema = await sg.database.fetch_val("SELECT current_schema()")
    recording = _Recording(sg.database)
    monkeypatch.setattr(sg, "database", recording)
    reader = await asyncpg.connect(_asyncpg_dsn())
    watcher = await asyncpg.connect(_asyncpg_dsn())
    held = reader.transaction()
    await held.start()
    blocked: List[str] = []
    boot = None
    try:
        await reader.execute(f"LOCK TABLE {schema}.{table} IN ACCESS SHARE MODE")
        reader_pid = await reader.fetchval("SELECT pg_backend_pid()")
        started = time.monotonic()
        boot = asyncio.ensure_future(sg.ensure_required_schema_light())
        while not boot.done() and time.monotonic() - started < _READER_HOLDS_S:
            rows = await watcher.fetch(
                "SELECT query FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))",
                reader_pid,
            )
            blocked.extend(" ".join(r["query"].split()) for r in rows)
            await asyncio.wait({boot}, timeout=0.02)
        finished_while_held = boot.done()
    finally:
        await held.rollback()
        await reader.close()
        await watcher.close()
        if boot is not None:
            await boot
    return finished_while_held, blocked, recording


@pytest.mark.parametrize("table", ["orders", "merchant_psps"])
async def test_a_boot_does_not_queue_behind_a_reader_when_the_columns_exist(
    _db, table, monkeypatch
):
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()  # the first boot heals; every boot after is this one
    healed = _ORDERS_HEALED if table == "orders" else _MERCHANT_PSPS_HEALED
    present = {c[0] for c in await _columns(table)}
    assert set(healed) <= present, "precondition: the first boot healed the columns"

    finished_while_held, blocked, ran = await _boot_while_a_reader_holds(table, monkeypatch)

    assert ran.heal_of(table) == [None], f"positive control: the boot ran the {table} heal once"
    assert blocked == [], f"the boot queued behind a reader of {table}: {blocked}"
    assert finished_while_held, f"the boot finished only once the reader let go of {table}"


async def test_a_boot_gives_up_on_a_missing_column_instead_of_waiting(_db, monkeypatch):
    from db.schema_guard import HEAL_LOCK_TIMEOUT, ensure_required_schema_light

    assert HEAL_LOCK_TIMEOUT == "500ms"
    await ensure_required_schema_light()
    await _db.execute("ALTER TABLE merchant_psps DROP COLUMN last_validated_at")

    finished_while_held, blocked, ran = await _boot_while_a_reader_holds("merchant_psps", monkeypatch)

    # The heal DID try (the column is missing), so it waited — for its lock_timeout only.
    assert any("merchant_psps" in q for q in blocked), "precondition: the heal attempted the lock"
    [error] = ran.heal_of("merchant_psps")
    assert error is not None and "lock timeout" in error, error
    assert finished_while_held, "the heal waited for the reader instead of its lock_timeout"
    assert "last_validated_at" not in {c[0] for c in await _columns("merchant_psps")}

    await ensure_required_schema_light()  # the next boot, no reader: heals it
    assert "last_validated_at" in {c[0] for c in await _columns("merchant_psps")}


async def test_a_missing_column_is_healed_exactly_as_the_bare_alter_healed_it(_db, monkeypatch):
    """"Keep behaviour identical when a column really is missing": the guarded heal and the
    ALTER it wraps, run bare as before, must leave the same catalog."""
    import db.schema_guard as sg

    heal_sql: List[str] = []
    original = sg.guarded_add_columns

    def spy(sql: str) -> List[str]:
        if "merchant_psps" in sql:
            heal_sql.append(sql)
        return original(sql)

    monkeypatch.setattr(sg, "guarded_add_columns", spy)
    await sg.ensure_required_schema_light()
    guarded = await _columns("merchant_psps")
    assert set(_MERCHANT_PSPS_HEALED) <= {c[0] for c in guarded}
    assert len(heal_sql) == 1

    await _db.execute("DROP TABLE merchant_psps")
    await _db.execute(_MERCHANT_PSPS_BASE)
    await _db.execute(heal_sql[0])
    assert await _columns("merchant_psps") == guarded
