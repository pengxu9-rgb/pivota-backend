"""Build the tables `db/schema_guard.ensure_required_schema_light` owns, then take away only what it built.

Some tables have no SQLAlchemy model, so `tests/model_schema.ensure_model_tables` cannot
build them. `reap_agentic_purchases` and `conversion_click_claims` are two of them: their
only definition is the production heal in `db/schema_guard.py` (plus `db/migrations`).
A test file that reaches one of them through real code used to pass only when an earlier
file in the same process had run that heal, either directly (e.g.
`tests/test_a2a_rail_ledger.py`) or through a full-app startup, and had left the tables
behind. Alone, or in a sweep shard where no such file came first, it failed.

`schema_guard_tables()` runs the same heal production runs: no DDL is written here, and no
row is inserted (the SQLite branch of the heal is DDL only). It records `sqlite_master`
before and right after the heal, and when the block ends it drops exactly the objects the
heal added: tables, plus indexes, triggers and views. Anything that was already there is
left alone, never dropped or emptied, so this file does not become the next hidden provider.

What it cannot undo: a column the heal ADDED to a table it found stays. That is the same
patch-not-drop rule `ensure_model_tables` follows, and dropping a column from a table some
other file built would be worse.

It also puts the shared `db.database.database` back the way it found it. The heal connects
it, and a later file may assume it starts disconnected (be#2790).

SQLite only. The sweep that runs these files is SQLite, and a Postgres test DB is a real
migrated schema, so on Postgres the block builds and drops nothing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Iterable, Set, Tuple

# Children before parents. An index or trigger on a table this block created goes with its
# table, so `IF EXISTS` covers the ones that are already gone by the time we reach them.
_DROP_ORDER = ("trigger", "view", "index", "table")

_Object = Tuple[str, str]  # (type, name) in sqlite_master


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _schema_objects(engine) -> Set[_Object]:
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        )
        return {(str(kind), str(name)) for kind, name in rows}


def _drop(engine, created: Set[_Object]) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        for kind in _DROP_ORDER:
            for obj_kind, name in sorted(created):
                if obj_kind == kind:
                    conn.execute(text(f"DROP {kind.upper()} IF EXISTS {_quote_ident(name)}"))


@asynccontextmanager
async def schema_guard_tables(required: Iterable[str] = ()) -> AsyncIterator[None]:
    """Run `ensure_required_schema_light` for the block; drop what it created afterwards.

    `required` names tables the caller reads. The block fails right away if the heal did
    not leave one of them, which is clearer than the caller's own query failing later.
    """
    from sqlalchemy import create_engine

    from db.database import IS_SQLITE, database, sync_url
    from db.schema_guard import ensure_required_schema_light

    if not IS_SQLITE:
        yield
        return

    was_connected = bool(getattr(database, "is_connected", False))
    engine = create_engine(sync_url)
    created: Set[_Object] = set()
    try:
        before = _schema_objects(engine)
        try:
            await ensure_required_schema_light()
        finally:
            created = _schema_objects(engine) - before
        tables = {name for kind, name in before | created if kind == "table"}
        missing = sorted(set(required) - tables)
        assert not missing, f"ensure_required_schema_light did not build {missing}"
        yield
    finally:
        try:
            _drop(engine, created)
        finally:
            engine.dispose()
            if not was_connected and getattr(database, "is_connected", False):
                await database.disconnect()
