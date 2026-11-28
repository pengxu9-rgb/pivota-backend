"""db/merchant_agent_checkout_request_facts on the PRODUCTION dialect.

Picked up by .github/workflows/postgres-dialect-gate.yml through the `tests/test_*_postgres.py`
glob. What only Postgres can show:

  * MIGRATION 302 IS THE ONLY BUILDER. The table has no startup heal; the shared fixture builds it
    from the migration, split the way the migration runner splits it.
  * PREPARE. The module's Postgres statements are planned against that schema (the conditional
    upsert's CAST(:x AS BOOLEAN) / INTERVAL arithmetic included).
  * The CHECK refuses an outcome outside the three, and the timestamps are the server's clock.
  * EVERY CASE of the dialect-agnostic suite, star-imported below: each write rule and boundary,
    the concurrent first write, the missing table, and the text checks.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_request_facts_test \
        .venv/bin/python -m pytest tests/test_merchant_agent_checkout_request_facts_postgres.py

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
    from tests.test_merchant_agent_checkout_request_facts import *  # noqa: F401,F403,E402
    # Underscore names a star-import skips. `_request_facts_db` is the fixture the shared cases resolve
    # by name in THIS module's namespace.
    from tests.test_merchant_agent_checkout_request_facts import (  # noqa: E402,F401
        NEGATIVE,
        POSITIVE_UCP,
        TABLE,
        _MIGRATION,
        _age,
        _request_facts_db,
    )

_BIND = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")
#: The held-domain reads plan against tables other migrations own; they are prepared when those
#: tables exist here (tests/test_repo_sql_prepare_postgres.py plans them against the full schema).
_OTHER_TABLES = {
    "_HELD_TIERB_SQL": "tierb_cart_link_eligibility",
    "_HELD_PURCHASABILITY_SQL": "merchant_purchasability",
    "_HELD_CENSUS_FACT_SQL": "merchant_agent_checkout_facts",
}


def _postgres_statements():
    from db import merchant_agent_checkout_request_facts as request_facts

    return {
        name: value for name, value in vars(request_facts).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }


async def test_every_postgres_statement_prepares(_request_facts_db):
    import asyncpg

    statements = _postgres_statements()
    assert set(statements) == {"_UPSERT_SQL", "_SELECT_REQUEST_FACT_SQL", *_OTHER_TABLES}, sorted(statements)
    conn = await asyncpg.connect(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    failures = []
    try:
        for name, sql in sorted(statements.items()):
            other = _OTHER_TABLES.get(name)
            if other and not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", other):
                continue
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


async def test_the_migration_builds_the_declared_columns_and_applies_twice(_request_facts_db):
    from db.database import database
    from db.sql_migrations import split_statements

    for statement in split_statements(_MIGRATION.read_text("utf-8")):
        await database.execute(statement)  # a second apply is a no-op, not an error
    rows = await database.fetch_all(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = :t ORDER BY ordinal_position", {"t": TABLE})
    assert [(r["column_name"], r["data_type"], r["is_nullable"]) for r in rows] == [
        ("merchant_domain", "character varying", "NO"),
        ("checked_at", "timestamp with time zone", "NO"),
        ("last_outcome", "character varying", "NO"),
        ("last_code", "character varying", "NO"),
        ("ucp_checkout_at", "timestamp with time zone", "YES"),
        ("acp_checkout_at", "timestamp with time zone", "YES"),
        ("negative_at", "timestamp with time zone", "YES"),
        ("ucp_version", "character varying", "YES"),
        ("source", "character varying", "NO"),
    ]
    fks = await database.fetch_val(
        "SELECT COUNT(*) FROM information_schema.table_constraints "
        "WHERE table_name = :t AND constraint_type = 'FOREIGN KEY'", {"t": TABLE})
    assert int(fks) == 0


async def test_the_check_refuses_an_outcome_outside_the_three(_request_facts_db):
    from db.database import database

    with pytest.raises(Exception) as caught:
        await database.execute(
            f"INSERT INTO {TABLE} (merchant_domain, checked_at, last_outcome, last_code, source) "
            "VALUES ('brand-a.example', CURRENT_TIMESTAMP, 'maybe', 'ok', 'request')")
    assert "ck_merchant_agent_checkout_request_facts_outcome" in str(caught.value)


async def test_the_timestamps_are_the_servers_clock(_request_facts_db):
    from db import merchant_agent_checkout_request_facts as request_facts
    from db.database import database

    await request_facts.record_request_check("brand-a.example", POSITIVE_UCP)
    drift = await database.fetch_val(
        f"SELECT ABS(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - checked_at))) FROM {TABLE} "
        "WHERE merchant_domain = 'brand-a.example'")
    assert float(drift) < 60
