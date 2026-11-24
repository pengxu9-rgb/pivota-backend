"""db/partner_agent_merchant_blocks on the PRODUCTION dialect.

Picked up by .github/workflows/postgres-dialect-gate.yml through the `tests/test_*_postgres.py`
glob. What only Postgres can show:

  * MIGRATION 295 IS THE ONLY BUILDER. The table has no startup heal, so the schema production
    gets is exactly what this file applies (the shared fixture builds the table from the
    migration, split the way the migration runner splits it).
  * PREPARE. Every statement in the module is planned against that schema.
  * The catalog: the columns, the two partial indexes as Postgres built them, and the five
    CHECK constraints by name.
  * EVERY CASE of the dialect-agnostic suite, star-imported below.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_partner_agent_blocks_test \
        .venv/bin/python -m pytest tests/test_partner_agent_merchant_blocks_postgres.py

THIS MODULE MUST NOT IMPORT `main` (the gate runs every `*_postgres.py` in one process).
"""

from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL: this is the production-dialect gate; see the module docstring",
)

if _IS_PG:
    from tests.test_partner_agent_merchant_blocks import *  # noqa: F401,F403,E402
    # Underscore names a star-import skips. `_blocks_db` is the fixture the shared cases resolve
    # by name in THIS module's namespace.
    from tests.test_partner_agent_merchant_blocks import (  # noqa: E402,F401
        SHOP,
        TABLE,
        _blocks_db,
        _clear,
        _worker_event,
    )

_BIND = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")


def _statements():
    from db import partner_agent_merchant_blocks as blocks

    return {name: value for name, value in vars(blocks).items() if name.endswith("_SQL") and isinstance(value, str)}


async def test_every_statement_prepares(_blocks_db):
    import asyncpg

    statements = _statements()
    assert set(statements) == {
        "_INSERT_SQL", "_BY_PURCHASE_SQL", "_CLEAR_SQL", "_OPEN_SQL", "_LIST_SQL", "_TABLE_READABLE_SQL",
    }, sorted(statements)
    conn = await asyncpg.connect(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    failures = []
    try:
        for name, sql in sorted(statements.items()):
            order: list = []

            def positional(match):
                if match.group(1) not in order:
                    order.append(match.group(1))
                return f"${order.index(match.group(1)) + 1}"

            try:
                await conn.prepare(_BIND.sub(positional, sql))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
    finally:
        await conn.close()
    assert not failures, "\n".join(failures)


async def test_the_migration_builds_the_declared_schema_and_applies_twice(_blocks_db):
    from db.database import database
    from db.sql_migrations import split_statements
    from tests.test_partner_agent_merchant_blocks import _MIGRATION

    for statement in split_statements(_MIGRATION.read_text("utf-8")):
        await database.execute(statement)  # a second apply is a no-op, not an error
    columns = await database.fetch_all(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = :t ORDER BY ordinal_position", {"t": TABLE})
    assert [(r["column_name"], r["data_type"], r["is_nullable"]) for r in columns] == [
        ("id", "character varying", "NO"),
        ("rail", "character varying", "NO"),
        ("merchant_domain", "character varying", "NO"),
        ("kind", "character varying", "NO"),
        ("source", "character varying", "NO"),
        ("purchase_id", "character varying", "YES"),
        ("recorded_by", "character varying", "YES"),
        ("observed_at", "timestamp with time zone", "NO"),
        ("cleared_at", "timestamp with time zone", "YES"),
        ("cleared_basis", "character varying", "YES"),
        ("cleared_by", "character varying", "YES"),
        ("cleared_evidence", "character varying", "YES"),
    ]
    indexes = await database.fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t ORDER BY indexname", {"t": TABLE})
    built = {r["indexname"]: " ".join(r["indexdef"].split()) for r in indexes}
    assert "UNIQUE INDEX" in built["uq_partner_agent_merchant_blocks_purchase"]
    assert "(purchase_id IS NOT NULL)" in built["uq_partner_agent_merchant_blocks_purchase"]
    assert "UNIQUE" not in built["idx_partner_agent_merchant_blocks_open"]
    assert "(cleared_at IS NULL)" in built["idx_partner_agent_merchant_blocks_open"]
    checks = await database.fetch_all(
        "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND contype = 'c' ORDER BY conname",
        {"t": TABLE})
    assert [r["conname"] for r in checks] == [
        "ck_partner_agent_merchant_blocks_basis",
        "ck_partner_agent_merchant_blocks_cleared_together",
        "ck_partner_agent_merchant_blocks_kind",
        "ck_partner_agent_merchant_blocks_rail",
        "ck_partner_agent_merchant_blocks_source",
    ]


async def test_each_check_refuses_a_value_outside_its_vocabulary(_blocks_db):
    from db.database import database

    base = "INSERT INTO {t} (id, rail, merchant_domain, kind, source, observed_at) VALUES ('pab_x', {v}, CURRENT_TIMESTAMP)"
    for values, constraint in [
        ("'rail_x', 'brand-a.example', 'captcha', 'worker'", "ck_partner_agent_merchant_blocks_rail"),
        ("'a2a', 'brand-a.example', 'issuer_step_up', 'worker'", "ck_partner_agent_merchant_blocks_kind"),
        ("'a2a', 'brand-a.example', 'captcha', 'request'", "ck_partner_agent_merchant_blocks_source"),
    ]:
        with pytest.raises(Exception) as caught:
            await database.execute(base.format(t=TABLE, v=values))
        assert constraint in str(caught.value)
    await _worker_event()
    with pytest.raises(Exception) as caught:
        await database.execute(
            f"UPDATE {TABLE} SET cleared_at = CURRENT_TIMESTAMP, cleared_basis = 'later_order', "
            "cleared_by = 'x', cleared_evidence = 'y'")
    assert "ck_partner_agent_merchant_blocks_basis" in str(caught.value)


async def test_the_clearance_time_is_the_servers_clock(_blocks_db):
    from db import partner_agent_merchant_blocks as blocks
    from db.database import database

    await _worker_event()
    assert await _clear() == 1
    drift = await database.fetch_val(
        f"SELECT ABS(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - cleared_at))) FROM {TABLE}")
    assert float(drift) < 60 and await blocks.is_clear("a2a", SHOP) is True
