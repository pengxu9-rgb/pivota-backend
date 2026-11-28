"""db/merchant_agent_checkout_request_facts: the request-time door-read fact (migration 302).

The store on the process database (SQLite here; the same cases run on Postgres through
tests/test_merchant_agent_checkout_request_facts_postgres.py, which star-imports this module), and the
text checks that keep the run condition and the census away from the new table.

Every row a test ages is aged with the DATABASE's clock (a relative UPDATE, in minutes so each
boundary is tested one minute either side of it), never with a pinned Python datetime.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import os
from pathlib import Path
from typing import List

import pytest

from db import merchant_agent_checkout_facts as facts
from db import merchant_agent_checkout_request_facts as request_facts
from db.database import IS_POSTGRES, database

_REPO = Path(__file__).resolve().parents[1]
_MIGRATION = _REPO / "db/migrations/302_merchant_agent_checkout_request_facts.sql"
_DOWN = _REPO / "db/migrations/down/302_merchant_agent_checkout_request_facts_down.sql"
_CENSUS_MIGRATION = _REPO / "db/migrations/294_merchant_agent_checkout_facts.sql"
TABLE = request_facts.TABLE

POSITIVE_UCP = facts.Check(facts.PUBLISHES, "ok", True, None, "2026-08-25")
POSITIVE_ACP = facts.Check(facts.PUBLISHES, "ok", None, True, None)
NEGATIVE = facts.Check(facts.DOES_NOT_PUBLISH, "ok", False, False)
UNREADABLE = facts.Check(facts.UNREADABLE, "ucp_timeout")

DAY = 24 * 60  # minutes

#: The SQLite copy of migration 302's table, for this suite only. Nothing in the application builds one.
_CREATE_SQL_SQLITE = """
CREATE TABLE IF NOT EXISTS merchant_agent_checkout_request_facts (
    merchant_domain VARCHAR(255) PRIMARY KEY,
    checked_at TIMESTAMP NOT NULL,
    last_outcome VARCHAR(20) NOT NULL
        CONSTRAINT ck_merchant_agent_checkout_request_facts_outcome
        CHECK (last_outcome IN ('publishes', 'does_not_publish', 'unreadable')),
    last_code VARCHAR(64) NOT NULL,
    ucp_checkout_at TIMESTAMP,
    acp_checkout_at TIMESTAMP,
    negative_at TIMESTAMP,
    ucp_version VARCHAR(10),
    source VARCHAR(32) NOT NULL
)
"""

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_")


def _assert_throwaway_database() -> None:
    if not IS_POSTGRES:
        return
    url = (os.getenv("DATABASE_URL") or "").strip()
    dbname = url.rsplit("/", 1)[-1].split("?")[0]
    if not any(marker in dbname for marker in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop {TABLE} in database {dbname!r}: throwaway databases only")


async def _create_table() -> None:
    """Postgres: migration 302 itself, split the way the migration runner splits it. SQLite: the copy."""
    if IS_POSTGRES:
        from db.sql_migrations import split_statements

        for statement in split_statements(_MIGRATION.read_text("utf-8")):
            await database.execute(statement)
    else:
        await database.execute(_CREATE_SQL_SQLITE)


@pytest.fixture
async def _request_facts_db(monkeypatch):
    """The request fact table, empty, on the process database, and a fresh warning limit."""
    _assert_throwaway_database()
    monkeypatch.delenv(facts.MAX_AGE_ENV, raising=False)
    request_facts._reset_warning_limit()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _create_table()
    await database.execute(f"DELETE FROM {TABLE}")
    yield
    await _create_table()
    await database.execute(f"DELETE FROM {TABLE}")
    request_facts._reset_warning_limit()
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _age(domain: str, column: str, minutes: int) -> None:
    """Move one timestamp `minutes` into the past, by the database's own clock."""
    assert column in {"ucp_checkout_at", "acp_checkout_at", "negative_at", "checked_at"}
    if IS_POSTGRES:
        past = "CURRENT_TIMESTAMP - (CAST(:m AS INTEGER) * INTERVAL '1 minute')"
    else:
        past = "datetime('now', '-' || CAST(:m AS INTEGER) || ' minutes')"
    await database.execute(
        f"UPDATE {TABLE} SET {column} = {past} WHERE merchant_domain = :d", {"m": minutes, "d": domain})


async def _past_the_rate_limit(domain: str) -> None:
    await _age(domain, "checked_at", DAY + 1)


async def _count() -> int:
    return int(await database.fetch_val(f"SELECT COUNT(*) FROM {TABLE}"))


class _StatementSpy:
    """Stands in for the module's `database`: records every statement, then runs it."""

    def __init__(self) -> None:
        self.statements: List[str] = []

    async def fetch_one(self, sql, values=None):
        self.statements.append(sql)
        return await database.fetch_one(sql, values)


# ── the store ────────────────────────────────────────────────────────────────────────────────


async def test_a_first_positive_is_written_with_source_request(_request_facts_db):
    assert await request_facts.record_request_check("www.Brand-A.example", POSITIVE_UCP) is True
    row = await request_facts.get_request_fact("brand-a.example")
    assert row["merchant_domain"] == "brand-a.example", "the census key: lower case, no www."
    assert (row["last_outcome"], row["last_code"], row["ucp_version"], row["source"]) == (
        "publishes", "ok", "2026-08-25", "request")
    assert row["ucp_checkout_at"] is not None and row["acp_checkout_at"] is None and row["negative_at"] is None


async def test_a_first_negative_is_written(_request_facts_db):
    assert await request_facts.record_request_check("brand-a.example", NEGATIVE) is True
    row = await request_facts.get_request_fact("brand-a.example")
    assert (row["last_outcome"], row["negative_at"] is not None, row["source"]) == ("does_not_publish", True, "request")


async def test_an_unreadable_check_writes_nothing_and_sends_no_statement(_request_facts_db, monkeypatch):
    spy = _StatementSpy()
    monkeypatch.setattr(request_facts, "database", spy)
    assert await request_facts.record_request_check("brand-a.example", UNREADABLE) is False
    walled = facts.Check(facts.UNREADABLE, "acp_403", False, None, "2026-11-01")  # census rule 6 shape
    assert await request_facts.record_request_check("brand-a.example", walled) is False
    assert spy.statements == []
    monkeypatch.undo()
    assert await _count() == 0


async def test_an_unreadable_check_never_touches_an_existing_positive(_request_facts_db):
    await request_facts.record_request_check("brand-a.example", POSITIVE_UCP)
    await _past_the_rate_limit("brand-a.example")
    before = await request_facts.get_request_fact("brand-a.example")
    walled = facts.Check(facts.UNREADABLE, "acp_403", False, None, "2026-11-01")
    assert await request_facts.record_request_check("brand-a.example", walled) is False
    assert await request_facts.get_request_fact("brand-a.example") == before


@pytest.mark.parametrize("domain", [None, "", "   ", 7])
async def test_an_unusable_domain_is_refused(_request_facts_db, domain):
    assert await request_facts.record_request_check(domain, POSITIVE_UCP) is False
    assert await _count() == 0


@pytest.mark.parametrize("check", [
    facts.Check(facts.PUBLISHES, "ok", False, False),        # outcome disagrees with the answers
    facts.Check(facts.DOES_NOT_PUBLISH, "ok", True, None),
    facts.Check("maybe", "ok", True, None),
    ("publishes", "ok", True, None, None),                  # not a Check
    None,
])
async def test_a_check_that_disagrees_with_itself_is_refused(_request_facts_db, check):
    assert await request_facts.record_request_check("brand-a.example", check) is False
    assert await _count() == 0


# ── at most one write per domain per 24 hours ────────────────────────────────────────────────


async def test_a_second_write_within_24_hours_writes_nothing(_request_facts_db):
    assert await request_facts.record_request_check("brand-a.example", POSITIVE_UCP) is True
    first = await request_facts.get_request_fact("brand-a.example")
    assert await request_facts.record_request_check("brand-a.example", POSITIVE_ACP) is False
    assert await request_facts.record_request_check("brand-a.example", NEGATIVE) is False
    assert await request_facts.get_request_fact("brand-a.example") == first


async def test_the_24_hour_limit_boundary(_request_facts_db):
    await request_facts.record_request_check("brand-a.example", POSITIVE_UCP)
    await _age("brand-a.example", "checked_at", DAY - 1)
    assert await request_facts.record_request_check("brand-a.example", POSITIVE_ACP) is False, "23h59m: too soon"
    await _age("brand-a.example", "checked_at", DAY + 1)
    assert await request_facts.record_request_check("brand-a.example", POSITIVE_ACP) is True, "24h01m: allowed"
    row = await request_facts.get_request_fact("brand-a.example")
    assert row["acp_checkout_at"] is not None and row["ucp_checkout_at"] is not None, "cannot-tell UCP kept"


async def test_the_limit_is_per_domain(_request_facts_db):
    assert await request_facts.record_request_check("brand-a.example", POSITIVE_UCP) is True
    assert await request_facts.record_request_check("brand-b.example", POSITIVE_UCP) is True
    assert await _count() == 2


async def test_two_concurrent_first_writes_for_one_domain_leave_one_row(_request_facts_db):
    """On Postgres the second INSERT meets the first's row under ON CONFLICT and its WHERE reads the
    fresh checked_at; on SQLite the two statements serialize on the one connection, with the same
    result. Either way: one row, exactly one True."""
    results = await asyncio.gather(
        request_facts.record_request_check("brand-a.example", POSITIVE_UCP),
        request_facts.record_request_check("www.brand-a.example", NEGATIVE),
    )
    assert sorted(results) == [False, True]
    assert await _count() == 1


# ── a positive over a negative: only once the negative is 14 days old ───────────────────────


async def test_a_positive_renews_a_positive(_request_facts_db):
    await request_facts.record_request_check("brand-a.example", POSITIVE_UCP)
    await _past_the_rate_limit("brand-a.example")
    await _age("brand-a.example", "ucp_checkout_at", 10 * DAY)
    assert await request_facts.record_request_check("brand-a.example", POSITIVE_UCP) is True
    fresh = await database.fetch_val(
        f"SELECT COUNT(*) FROM {TABLE} WHERE merchant_domain = 'brand-a.example' AND ucp_checkout_at "
        + ("> CURRENT_TIMESTAMP - INTERVAL '1 hour'" if IS_POSTGRES else "> datetime('now', '-1 hours')"))
    assert int(fresh) == 1, "the positive was renewed to the database's now"


async def test_a_positive_over_a_fresh_negative_writes_nothing(_request_facts_db):
    await request_facts.record_request_check("brand-a.example", NEGATIVE)
    await _past_the_rate_limit("brand-a.example")
    await _age("brand-a.example", "negative_at", 14 * DAY - 1)
    assert await request_facts.record_request_check("brand-a.example", POSITIVE_UCP) is False
    row = await request_facts.get_request_fact("brand-a.example")
    assert row["last_outcome"] == "does_not_publish" and row["negative_at"] is not None


async def test_a_positive_replaces_a_negative_older_than_14_days(_request_facts_db):
    await request_facts.record_request_check("brand-a.example", NEGATIVE)
    await _past_the_rate_limit("brand-a.example")
    await _age("brand-a.example", "negative_at", 14 * DAY + 1)
    assert await request_facts.record_request_check("brand-a.example", POSITIVE_UCP) is True
    row = await request_facts.get_request_fact("brand-a.example")
    assert (row["last_outcome"], row["negative_at"], row["ucp_version"]) == ("publishes", None, "2026-08-25")
    assert row["ucp_checkout_at"] is not None


# ── a negative never overwrites a fresh positive ─────────────────────────────────────────────


@pytest.mark.parametrize("positive,column", [(POSITIVE_UCP, "ucp_checkout_at"), (POSITIVE_ACP, "acp_checkout_at")])
async def test_a_negative_never_overwrites_a_positive_younger_than_the_max_age(_request_facts_db, positive, column):
    await request_facts.record_request_check("brand-a.example", positive)
    await _past_the_rate_limit("brand-a.example")
    await _age("brand-a.example", column, 30 * DAY - 1)
    assert await request_facts.record_request_check("brand-a.example", NEGATIVE) is False
    row = await request_facts.get_request_fact("brand-a.example")
    assert (row["last_outcome"], row["negative_at"]) == ("publishes", None) and row[column] is not None


@pytest.mark.parametrize("positive,column", [(POSITIVE_UCP, "ucp_checkout_at"), (POSITIVE_ACP, "acp_checkout_at")])
async def test_a_negative_replaces_a_positive_older_than_the_max_age(_request_facts_db, positive, column):
    await request_facts.record_request_check("brand-a.example", positive)
    await _past_the_rate_limit("brand-a.example")
    await _age("brand-a.example", column, 30 * DAY + 1)
    assert await request_facts.record_request_check("brand-a.example", NEGATIVE) is True
    row = await request_facts.get_request_fact("brand-a.example")
    assert row["last_outcome"] == "does_not_publish" and row["negative_at"] is not None
    assert (row["ucp_checkout_at"], row["acp_checkout_at"], row["ucp_version"]) == (None, None, None)


async def test_one_fresh_path_is_enough_to_keep_the_positive(_request_facts_db):
    await request_facts.record_request_check("brand-a.example", facts.Check(facts.PUBLISHES, "ok", True, True, "2026-08-25"))
    await _past_the_rate_limit("brand-a.example")
    await _age("brand-a.example", "ucp_checkout_at", 31 * DAY)
    await _age("brand-a.example", "acp_checkout_at", 2 * DAY)
    assert await request_facts.record_request_check("brand-a.example", NEGATIVE) is False


async def test_the_max_age_is_the_census_modules_own(_request_facts_db, monkeypatch):
    monkeypatch.setenv(facts.MAX_AGE_ENV, "10")
    assert facts.max_age_days() == 10
    await request_facts.record_request_check("brand-a.example", POSITIVE_UCP)
    await _past_the_rate_limit("brand-a.example")
    await _age("brand-a.example", "ucp_checkout_at", 10 * DAY - 1)
    assert await request_facts.record_request_check("brand-a.example", NEGATIVE) is False
    await _age("brand-a.example", "ucp_checkout_at", 10 * DAY + 1)
    assert await request_facts.record_request_check("brand-a.example", NEGATIVE) is True


async def test_a_negative_renews_a_negative(_request_facts_db):
    await request_facts.record_request_check("brand-a.example", NEGATIVE)
    await _past_the_rate_limit("brand-a.example")
    await _age("brand-a.example", "negative_at", 3 * DAY)
    assert await request_facts.record_request_check("brand-a.example", NEGATIVE) is True
    recent = await database.fetch_val(
        f"SELECT COUNT(*) FROM {TABLE} WHERE merchant_domain = 'brand-a.example' AND negative_at "
        + ("> CURRENT_TIMESTAMP - INTERVAL '1 hour'" if IS_POSTGRES else "> datetime('now', '-1 hours')"))
    assert int(recent) == 1, "renewed as record_check renews it"


async def test_the_columns_move_as_the_census_record_check_moves_them(_request_facts_db):
    """A new row of each kind: the request row and the census row agree on every column but source
    (and on which timestamps are set). The census table is built for this test and emptied after."""
    from tests.test_merchant_agent_checkout_facts import _create_table as create_census_table

    await create_census_table()
    domains = {"brand-a.example": POSITIVE_UCP, "brand-b.example": POSITIVE_ACP, "brand-c.example": NEGATIVE}
    try:
        for domain, check in domains.items():
            await facts.record_check(domain, check)
            await request_facts.record_request_check(domain, check)

        def shape(row):
            return tuple(
                (value is not None) if column.endswith("_at") else value
                for column, value in row.items() if column != "source")

        for domain in domains:
            census_row = await facts.get_fact(domain)
            request_row = await request_facts.get_request_fact(domain)
            assert shape(request_row) == shape(census_row), domain
            assert (census_row["source"], request_row["source"]) == ("census", "request")
    finally:
        for domain in domains:
            await database.execute(
                "DELETE FROM merchant_agent_checkout_facts WHERE merchant_domain = :d", {"d": domain})


# ── a missing table ──────────────────────────────────────────────────────────────────────────


async def test_a_missing_table_is_a_no_op_with_one_warning_across_many_calls(_request_facts_db, caplog, monkeypatch):
    await database.execute(f"DROP TABLE {TABLE}")
    clock = [1000.0]
    monkeypatch.setattr(request_facts, "_monotonic", lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger=request_facts.__name__):
        results = [await request_facts.record_request_check(f"brand-{i}.example", POSITIVE_UCP) for i in range(50)]
    assert results == [False] * 50
    warnings = [r for r in caplog.records if r.name == request_facts.__name__ and r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "brand-" not in warnings[0].getMessage(), "no domain, value or statement in the warning"

    caplog.clear()
    clock[0] += request_facts.WARNING_INTERVAL_SECONDS
    with caplog.at_level(logging.WARNING, logger=request_facts.__name__):
        assert await request_facts.record_request_check("brand-a.example", POSITIVE_UCP) is False
    warnings = [r for r in caplog.records if r.name == request_facts.__name__]
    assert len(warnings) == 1 and "suppressed_since_last=49" in warnings[0].getMessage()


async def test_a_missing_table_reads_as_no_fact(_request_facts_db):
    await database.execute(f"DROP TABLE {TABLE}")
    assert await request_facts.get_request_fact("brand-a.example") is None


# ── the migration ────────────────────────────────────────────────────────────────────────────


def _columns(sql_path: Path, table: str) -> List[str]:
    """The column lines of a migration's CREATE TABLE, with the table's own name taken out."""
    text = "\n".join(l for l in sql_path.read_text("utf-8").splitlines() if not l.lstrip().startswith("--"))
    body = text.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1].rsplit(");", 1)[0]
    return [" ".join(line.split()).replace(table, "<t>") for line in body.splitlines() if line.strip()]


def test_the_migration_has_the_census_tables_columns_and_types():
    assert _columns(_MIGRATION, TABLE) == _columns(_CENSUS_MIGRATION, facts.TABLE)


def test_the_migration_names_no_foreign_key_and_the_down_drops_only_its_table():
    text = _MIGRATION.read_text("utf-8")
    assert "REFERENCES" not in text.upper()
    statements = [l for l in _DOWN.read_text("utf-8").splitlines() if l.strip() and not l.lstrip().startswith("--")]
    assert statements == [f"DROP TABLE IF EXISTS {TABLE};"]


def test_nothing_at_startup_builds_the_table():
    """No startup heal, as 294: the table is applied in production as its own gated one-off."""
    for path in [_REPO / "db/schema_guard.py", _REPO / "main.py", *sorted((_REPO / "db").glob("*schema*.py"))]:
        assert TABLE not in path.read_text("utf-8"), path


# ── the text checks: the run condition and the census never read the new table ──────────────


def _module_sql(module) -> dict:
    return {name: value for name, value in vars(module).items() if name.endswith("_SQL") and isinstance(value, str)}


def test_the_run_condition_reader_sql_never_names_the_request_table():
    statements = _module_sql(facts)
    assert {"_RUN_CONDITION_SQL", "_UPSERT_SQL"} <= set(statements)
    statements["_RUN_CONDITION_SQL_SQLITE"] = facts._RUN_CONDITION_SQL_SQLITE
    for name, sql in statements.items():
        assert TABLE not in sql, name
    assert TABLE not in inspect.getsource(facts.run_condition_answer)
    assert TABLE not in inspect.getsource(facts.record_check)


def test_the_run_condition_never_imports_the_request_module():
    path = _REPO / "services/payment_orchestration/agent_run_condition.py"
    source = path.read_text("utf-8")
    assert "merchant_agent_checkout_request_facts" not in source
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not any("request_facts" in name for name in imported), sorted(imported)
    # ...and neither does the census fact module it reads.
    assert "merchant_agent_checkout_request_facts" not in (_REPO / "db/merchant_agent_checkout_facts.py").read_text("utf-8")


def test_the_census_job_and_its_population_never_read_the_request_table():
    from jobs import merchant_protocol_profile_census as census

    source = (_REPO / "jobs/merchant_protocol_profile_census.py").read_text("utf-8")
    assert TABLE not in source and "merchant_agent_checkout_request_facts" not in source
    for sql in (census._DIRECTORY_TIERB_DOMAINS_SQL, census._DIRECTORY_PURCHASABILITY_DOMAINS_SQL):
        assert TABLE not in sql
    assert TABLE not in inspect.getsource(census.read_directory_domains)


def test_the_upsert_compares_time_only_in_sql_and_always_writes_source_request():
    for name in ("_UPSERT_SQL", "_UPSERT_SQL_SQLITE"):
        sql = getattr(request_facts, name)
        assert "'request'" in sql and ":source" not in sql
        assert ":now" not in sql and ":checked_at" not in sql, "no Python clock is bound"
        assert "INTERVAL '24 hours'" in sql or "'-24 hours'" in sql
        assert "INTERVAL '14 days'" in sql or "'-14 days'" in sql
        assert ":max_age_days" in sql
    assert "CURRENT_TIMESTAMP" in request_facts._UPSERT_SQL and "datetime(" not in request_facts._UPSERT_SQL
    assert "datetime('now')" in request_facts._UPSERT_SQL_SQLITE
