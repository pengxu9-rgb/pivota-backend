"""The column heals in ensure_required_schema_light() run guarded, never bare.

The lock behaviour itself is proved on Postgres by tests/test_schema_guard_boot_lock_postgres.py.
This file pins what needs no database: which statements the guard accepts, that each heal
the boot runs goes out in its guarded form, and that one failed heal does not abandon the rest.
"""

from __future__ import annotations

import re

import pytest

from db import schema_guard as sg

_BARE_HEAL = re.compile(r"^ALTER TABLE IF EXISTS \w+ ADD COLUMN", re.IGNORECASE)


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def test_each_alter_is_wrapped_verbatim_behind_a_presence_check_and_a_lock_timeout():
    sql = """
        ALTER TABLE IF EXISTS execution_routes
          ADD COLUMN IF NOT EXISTS last_audit_run_id UUID;
        ALTER TABLE IF EXISTS evidence_items
          ADD COLUMN IF NOT EXISTS execution_route_id UUID,
          ADD COLUMN IF NOT EXISTS evidence_level TEXT;
    """
    first, second = (_flat(s) for s in sg.guarded_add_columns(sql))

    assert "ALTER TABLE IF EXISTS execution_routes ADD COLUMN IF NOT EXISTS last_audit_run_id UUID;" in first
    assert "ARRAY['last_audit_run_id']::text[]" in first
    assert "to_regclass('execution_routes')" in first
    assert "ARRAY['execution_route_id', 'evidence_level']::text[]" in second
    assert "to_regclass('evidence_items')" in second
    for statement in (first, second):
        assert statement.startswith("DO $heal$")
        assert "PERFORM set_config('lock_timeout', '500ms', true);" in statement
        # The lock_timeout is set before, and only inside, the branch that runs the ALTER.
        assert statement.index("THEN PERFORM set_config") < statement.index("ALTER TABLE")


def test_commas_inside_types_literals_expressions_and_comments_are_not_clause_breaks():
    sql = """
        ALTER TABLE IF EXISTS t
          -- a comment, with commas, and a quote's apostrophe
          ADD COLUMN IF NOT EXISTS fee NUMERIC(5,4),
          ADD COLUMN IF NOT EXISTS label TEXT DEFAULT 'a,b',
          ADD COLUMN IF NOT EXISTS net BIGINT GENERATED ALWAYS AS (GREATEST(a - b, 0)) STORED;
    """
    [statement] = sg.guarded_add_columns(sql)
    assert "ARRAY['fee', 'label', 'net']::text[]" in _flat(statement)


@pytest.mark.parametrize(
    "sql",
    [
        # Must still run on a boot where every column exists: a presence guard would stop it.
        "ALTER TABLE IF EXISTS t ADD COLUMN IF NOT EXISTS a TEXT, ALTER COLUMN b DROP NOT NULL;",
        "ALTER TABLE IF EXISTS t ADD COLUMN IF NOT EXISTS a TEXT, ADD CONSTRAINT c CHECK (a <> '');",
        "ALTER TABLE IF EXISTS t ALTER COLUMN b TYPE VARCHAR(128);",
        "ALTER TABLE IF EXISTS t DROP COLUMN a;",
        # Would fail on the second boot, guarded or not.
        "ALTER TABLE IF EXISTS t ADD COLUMN a TEXT;",
        # A statement the guard would silently drop.
        "ALTER TABLE IF EXISTS t ADD COLUMN IF NOT EXISTS a TEXT; CREATE INDEX i ON t (a);",
        # Would end the DO block's dollar quote early.
        "ALTER TABLE IF EXISTS t ADD COLUMN IF NOT EXISTS a TEXT DEFAULT $heal$x$heal$;",
        "CREATE INDEX IF NOT EXISTS i ON t (a);",
        "",
    ],
)
def test_anything_but_pure_column_adds_is_refused(sql):
    with pytest.raises(ValueError):
        sg.guarded_add_columns(sql)


class _RecordingDB:
    def __init__(self, fail_on: str = "") -> None:
        self.statements: list[str] = []
        self._fail_on = fail_on

    async def execute(self, query, *args, **kwargs):
        statement = _flat(str(query))
        self.statements.append(statement)
        if self._fail_on and self._fail_on in statement:
            raise RuntimeError("canceling statement due to lock timeout")

    async def fetch_all(self, *args, **kwargs):
        return []

    async def fetch_one(self, *args, **kwargs):
        return None

    async def fetch_val(self, *args, **kwargs):
        return None


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


def _heal_tables(statements):
    return [
        m.group(1)
        for s in statements
        for m in re.finditer(r"THEN PERFORM set_config\('lock_timeout', '500ms', true\); "
                             r"ALTER TABLE IF EXISTS (\w+) ADD COLUMN", s)
    ]


async def test_no_column_heal_the_boot_runs_goes_out_bare(_postgres_guard):
    db = _postgres_guard(_RecordingDB())
    await sg.ensure_required_schema_light()

    bare = [s[:120] for s in db.statements if _BARE_HEAL.match(s)]
    assert bare == [], "a column heal runs bare; wrap it in _heal_add_columns: " + repr(bare)
    healed = _heal_tables(db.statements)
    # Positive control: the boot really ran the heals (83 ALTERs over 49 tables when this was
    # written). A heal added later raises the count; one that stops being run lowers it.
    assert len(healed) >= 83, len(healed)
    assert {"orders", "merchant_psps"} <= set(healed)


async def test_a_failed_heal_does_not_abandon_the_heals_after_it(_postgres_guard):
    db = _postgres_guard(_RecordingDB(fail_on="ALTER TABLE IF EXISTS orders ADD COLUMN"))
    await sg.ensure_required_schema_light()

    healed = _heal_tables(db.statements)
    assert "orders" in healed, "precondition: the orders heal ran (and raised)"
    assert healed.index("merchant_psps") > healed.index("orders")
    # And the boot went on to its very last heal.
    assert healed[-1] == "commerce_attribution_edges"
