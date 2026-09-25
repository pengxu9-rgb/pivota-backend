"""`guarded_statements()`: the request-path `ensure_*` DDL lists go out guarded, never bare.

The lock behaviour itself is proved on Postgres by
tests/test_request_path_self_heal_lock_postgres.py. This file pins what needs no database: which
statements are guarded, which pass through, which are refused, that the SQLite path is left
alone, and that every converted module's list builds.
"""

from __future__ import annotations

import importlib
import re

import pytest

from db import schema_guard as sg

_BARE = re.compile(r"^\s*(ALTER TABLE|CREATE (UNIQUE )?INDEX)\b", re.IGNORECASE)


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def test_column_adds_and_index_builds_are_guarded_and_create_table_passes_through():
    create = "CREATE TABLE IF NOT EXISTS t (id INT PRIMARY KEY);"
    by_hand = sg.guarded_ddl("t", sg.column_is_not_null("t", "a"), "ALTER TABLE t ALTER COLUMN a DROP NOT NULL;")
    out = sg.guarded_statements(
        [
            create,
            "ALTER TABLE t ADD COLUMN IF NOT EXISTS a TEXT;",
            "ALTER TABLE IF EXISTS t ADD COLUMN IF NOT EXISTS b TEXT",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_t_a ON t (a) WHERE a IS NOT NULL;",
            by_hand,
        ],
        postgres=True,
    )
    assert out[0] == create and out[4] == by_hand
    assert _flat(out[1]).startswith("DO $heal$") and "ALTER TABLE t ADD COLUMN IF NOT EXISTS a TEXT;" in _flat(out[1])
    # A statement given without its semicolon still gets one inside the block.
    assert "ALTER TABLE IF EXISTS t ADD COLUMN IF NOT EXISTS b TEXT;" in _flat(out[2])
    assert _flat(out[3]).startswith("DO $index$") and "relname = 'ux_t_a'" in out[3]
    assert not any(_BARE.match(s) for s in out)


def test_a_bare_alter_table_keeps_its_raise_on_a_missing_table():
    """`ALTER TABLE t` (no IF EXISTS) must still run, and raise UndefinedTable, when t is
    missing: an apply_ddl_statements pass counts that as a failure to retry. Only the IF EXISTS
    form skips a missing table, as its bare statement did."""
    bare = _flat(sg.guarded_add_columns("ALTER TABLE t ADD COLUMN IF NOT EXISTS a TEXT;")[0])
    if_exists = _flat(sg.guarded_add_columns("ALTER TABLE IF EXISTS t ADD COLUMN IF NOT EXISTS a TEXT;")[0])
    assert "to_regclass('t') IS NOT NULL" not in bare
    assert "WHERE to_regclass('t') IS NOT NULL AND NOT EXISTS" in if_exists


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE t ALTER COLUMN a DROP NOT NULL;",
        "ALTER TABLE t DROP CONSTRAINT IF EXISTS c;",
        "CREATE INDEX i ON t (a);",  # no IF NOT EXISTS: not a heal
        "UPDATE t SET a = 1;",
        "DO $$ BEGIN END $$;",  # an unguarded block
    ],
)
def test_anything_without_a_guarded_form_is_refused(sql):
    with pytest.raises(ValueError):
        sg.guarded_statements([sql], postgres=True)


def test_off_postgres_the_list_is_left_alone():
    statements = ["CREATE INDEX IF NOT EXISTS i ON t (a);", "ALTER TABLE t ADD COLUMN IF NOT EXISTS a TEXT;"]
    assert sg.guarded_statements(statements, postgres=False) == statements


class _Driver(Exception):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Wrapped(Exception):
    def __init__(self, orig):
        super().__init__(str(orig))
        self.orig = orig


def test_a_lock_timeout_is_recognised_bare_or_wrapped_and_nothing_else_is():
    assert sg.is_lock_timeout(_Driver("55P03"))
    assert sg.is_lock_timeout(_Wrapped(_Driver("55P03")))
    assert not sg.is_lock_timeout(_Driver("42P01"))
    assert not sg.is_lock_timeout(_Wrapped(_Driver("57014")))
    assert not sg.is_lock_timeout(RuntimeError("lock timeout"))


# (module, the module-level list it runs on the request path)
_MODULE_LISTS = [
    ("db.audit_evidence", "_DDL_STATEMENTS"),
    ("db.executor_runs", "_DDL_STATEMENTS"),
    ("db.merchant_audit_runs", "_DDL_STATEMENTS"),
    ("db.merchant_tasks", "_DDL_STATEMENTS"),
    ("routes.employee_products", "_EXTERNAL_SEED_IMPORT_TASKS_DDL_STATEMENTS"),
    ("services.agent_webhook_service", "_AGENT_WEBHOOK_DDL_STATEMENTS"),
    ("services.merchant_webhook_service", "_MERCHANT_WEBHOOK_DDL_STATEMENTS"),
]


@pytest.mark.parametrize("module_name, attr", _MODULE_LISTS)
def test_every_converted_list_has_a_guarded_form_for_each_statement(module_name, attr):
    """On Postgres these lists are built through guarded_statements at import; this builds the
    same guarded form here (SQLite or not) so a statement added later without one fails in
    the ordinary suite, not only in the Postgres gate."""
    statements = getattr(importlib.import_module(module_name), attr)
    guarded = sg.guarded_statements(statements, postgres=True)
    assert len(guarded) == len(statements)
    assert not any(_BARE.match(s) for s in guarded)
