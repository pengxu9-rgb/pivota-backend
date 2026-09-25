"""The ALTER COLUMN / constraint heals and the index builds in ensure_required_schema_light()
run guarded, never bare.

The lock behaviour itself is proved on Postgres by
tests/test_schema_guard_boot_ddl_lock_postgres.py. This file pins what needs no database: the
shape of the guarded statements, which statements the index guard accepts, how a failed index
build is handled, and that nothing the boot sends is a bare ALTER TABLE or CREATE INDEX.
"""

from __future__ import annotations

import re

import pytest

from db import schema_guard as sg

_BARE = re.compile(r"^(ALTER TABLE|CREATE (UNIQUE )?INDEX)\b", re.IGNORECASE)


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def test_the_ddl_runs_verbatim_only_while_needed_and_under_the_lock_timeout():
    ddl = """
        ALTER TABLE IF EXISTS t DROP CONSTRAINT IF EXISTS c;
        ALTER TABLE IF EXISTS t ADD CONSTRAINT c CHECK (a IN ('x', 'y'));
    """
    statement = _flat(sg.guarded_ddl("t", sg.column_is_not_null("t", "a"), ddl))

    assert statement.startswith("DO $guard$ BEGIN IF to_regclass('t') IS NOT NULL AND (")
    assert _flat(ddl) in statement
    assert "AND attname = 'a'" in statement and "AND attnotnull" in statement
    # The lock_timeout is set before, and only inside, the branch that runs the DDL.
    assert "THEN PERFORM set_config('lock_timeout', '500ms', true); ALTER TABLE" in statement


def test_the_catalog_conditions_name_what_they_check():
    types = _flat(sg.column_type_differs("t", ("a", "b"), "character varying(128)"))
    assert "attname = ANY(ARRAY['a', 'b']::text[])" in types
    assert "format_type(atttypid, atttypmod) <> 'character varying(128)'" in types

    differs = _flat(sg.constraint_differs("t", "c", "CHECK ((a = 'x'::text))"))
    assert differs.startswith("NOT EXISTS (")
    assert "conname = 'c'" in differs and "AND convalidated" in differs
    assert "pg_get_constraintdef(oid) = $def$CHECK ((a = 'x'::text))$def$" in differs


def test_the_expected_definitions_are_built_from_the_one_verdict_list():
    assert sg._tierb_verdict_def("verdict").startswith(
        "CHECK (((verdict IS NULL) OR ((verdict)::text = ANY ((ARRAY['ELIGIBLE'::character varying, "
    )
    assert "'PRICE_DRIFT'::character varying])::text[]))))" in sg._tierb_verdict_def("verdict")


def test_a_ddl_carrying_the_blocks_own_quote_tag_is_refused():
    with pytest.raises(ValueError):
        sg.guarded_ddl("t", "true", "ALTER TABLE t ADD CONSTRAINT c CHECK (a <> $guard$x$guard$);")


def test_an_index_is_built_only_while_its_name_is_free_in_its_tables_schema():
    statement = _flat(
        sg.guarded_index(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
              uq_t_a
              ON t(a) WHERE a IS NOT NULL;
            """
        )
    )
    assert statement.startswith("DO $index$ BEGIN IF NOT EXISTS (")
    assert "WHERE relname = 'uq_t_a'" in statement
    assert "SELECT relnamespace FROM pg_class WHERE oid = to_regclass('t')" in statement
    assert (
        "THEN PERFORM set_config('lock_timeout', '500ms', true); "
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_t_a ON t(a) WHERE a IS NOT NULL; END IF;"
    ) in statement


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX i ON t (a);",  # no IF NOT EXISTS: fails on the second boot anyway
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (a);",  # illegal inside a DO block
        "CREATE INDEX IF NOT EXISTS i ON t (a); DROP TABLE t;",  # a statement it would hide
        "CREATE INDEX IF NOT EXISTS i ON t (a) WHERE b = $x$y$x$;",
        "ALTER TABLE t ADD COLUMN IF NOT EXISTS a TEXT;",
        "",
    ],
)
def test_anything_but_one_create_index_if_not_exists_is_refused(sql):
    with pytest.raises(ValueError):
        sg.guarded_index(sql)


class _Error(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _RecordingDB:
    def __init__(self, raise_on: str = "", sqlstate: str = "") -> None:
        self.statements: list[str] = []
        self._raise_on = raise_on
        self._sqlstate = sqlstate

    async def execute(self, query, *args, **kwargs):
        statement = _flat(str(query))
        self.statements.append(statement)
        if self._raise_on and self._raise_on in statement:
            raise _Error(self._sqlstate)

    async def fetch_all(self, *args, **kwargs):
        return []

    async def fetch_one(self, *args, **kwargs):
        return None

    async def fetch_val(self, *args, **kwargs):
        return None


async def test_an_index_build_that_timed_out_on_its_lock_waits_for_the_next_boot(monkeypatch):
    db = _RecordingDB(raise_on="CREATE INDEX", sqlstate="55P03")
    monkeypatch.setattr(sg, "database", db)
    await sg._ensure_index("CREATE INDEX IF NOT EXISTS i ON t (a);")
    assert len(db.statements) == 1


async def test_any_other_index_build_failure_raises_as_the_bare_statement_did(monkeypatch):
    # The boot's shared try relies on it: a missing table raised UndefinedTable (42P01).
    monkeypatch.setattr(sg, "database", _RecordingDB(raise_on="CREATE INDEX", sqlstate="42P01"))
    with pytest.raises(_Error):
        await sg._ensure_index("CREATE INDEX IF NOT EXISTS i ON t (a);")


@pytest.fixture
def _postgres_guard(monkeypatch):
    async def connected() -> None:
        return None

    monkeypatch.setattr(sg, "IS_POSTGRES", True)
    monkeypatch.setattr(sg, "IS_SQLITE", False)
    monkeypatch.setattr(sg, "_ensure_database_connected", connected)

    def install(db: _RecordingDB) -> _RecordingDB:
        monkeypatch.setattr(sg, "database", db)
        return db

    return install


async def test_nothing_the_boot_sends_is_a_bare_alter_table_or_create_index(_postgres_guard):
    db = _postgres_guard(_RecordingDB())
    await sg.ensure_required_schema_light()

    bare = [s[:120] for s in db.statements if _BARE.match(s)]
    assert bare == [], (
        "the boot sends a bare ALTER TABLE / CREATE INDEX; route it through _heal_add_columns, "
        "_heal_guarded or _ensure_index: " + repr(bare)
    )
    # Positive control: the boot really sent the guarded forms (60 index guards and these six
    # heals when this was written). One added later raises the count; one dropped lowers it.
    assert sum(s.startswith("DO $index$") for s in db.statements) >= 60
    guarded = [
        m.group(1)
        for s in db.statements
        for m in [re.match(r"DO \$guard\$ BEGIN IF to_regclass\('(\w+)'\)", s)]
        if m
    ]
    assert sorted(guarded) == [
        "commerce_interactions",
        "merchant_audit_runs",
        "merchant_onboarding",
        "partner_send_log",
        "tierb_cart_link_eligibility",
        "tierb_cart_link_eligibility",
    ]
