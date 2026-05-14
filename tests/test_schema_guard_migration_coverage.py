"""CI gate: every NEW migration's ADD COLUMN must have a matching
schema_guard.py self-heal entry.

## Why this exists

Railway production deploys do NOT auto-run `db/migrations/*.sql`.
The only thing that brings a new column into prod on deploy is the
`db/schema_guard.py` self-heal block (`ALTER TABLE IF EXISTS ...
ADD COLUMN IF NOT EXISTS ...` run at startup).

When a PR ships a migration + the SQLAlchemy `Table()` model update
but forgets the schema_guard entry, the column never lands in prod —
and the next runtime `Table.select()` materializes the missing
column into the SQL and crashes. That's the PR #494 → #501
`merchant_onboarding.apm_enabled` outage: 4 whole audits died at
`stage=discovering` on "column merchant_onboarding.apm_enabled does
not exist" before #501 added the self-heal block reactively.

This test makes that footgun loud at PR time instead of in prod.

## What it checks

For every migration numbered above `_GRANDFATHERED_THROUGH`, every
`ADD COLUMN` must have a matching `ADD COLUMN IF NOT EXISTS` for the
same (table, column) somewhere in `db/schema_guard.py`.

Existing migrations (<= the grandfather baseline) are exempt — they
predate the gate and are either already applied in every
environment or deliberately not self-healed. The gate activates for
the next migration that lands.

## Escape hatch

If a migration genuinely doesn't need runtime self-heal (e.g. it
only adds an index, or adds a column to a table never SELECT-*'d at
runtime), put the marker `-- schema-guard-exempt: <reason>` anywhere
in the migration file and the gate skips it. Make the reason
specific — a reviewer should be able to tell why it's safe.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Set, Tuple

_REPO = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _REPO / "db" / "migrations"
_SCHEMA_GUARD = _REPO / "db" / "schema_guard.py"

# Migrations at or below this number predate the schema-guard-coverage
# gate. The current max migration when the gate shipped was 093; the
# gate activates for 094+. Bumping this baseline retroactively exempts
# migrations — don't, unless you've confirmed the columns are in prod.
_GRANDFATHERED_THROUGH = 93

_EXEMPT_MARKER = "schema-guard-exempt"

# `ALTER TABLE [IF EXISTS] <table> ... ;` — non-greedy body capture up
# to the statement terminator. DOTALL so multi-line ALTERs (one
# ADD COLUMN per line, comma-separated) are captured whole.
_ALTER_TABLE_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?[\"']?(\w+)[\"']?(.*?);",
    re.IGNORECASE | re.DOTALL,
)
# `ADD COLUMN [IF NOT EXISTS] <col>` — only matches column adds, so
# `ADD INDEX` / `ADD CONSTRAINT` in the same ALTER body are ignored.
_ADD_COLUMN_RE = re.compile(
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?(\w+)[\"']?",
    re.IGNORECASE,
)


def _extract_added_columns(sql: str) -> Set[Tuple[str, str]]:
    """Return the set of (table, column) pairs an SQL blob adds via
    `ALTER TABLE ... ADD COLUMN`. Works on both migration .sql files
    and the schema_guard.py source (the ALTER blocks there live
    inside Python triple-quoted strings — the regex doesn't care)."""
    out: Set[Tuple[str, str]] = set()
    for alter in _ALTER_TABLE_RE.finditer(sql):
        table = alter.group(1).lower()
        body = alter.group(2)
        for col in _ADD_COLUMN_RE.finditer(body):
            out.add((table, col.group(1).lower()))
    return out


def _migration_number(path: Path) -> Optional[int]:
    m = re.match(r"(\d+)_", path.name)
    return int(m.group(1)) if m else None


def _schema_guard_covered() -> Set[Tuple[str, str]]:
    return _extract_added_columns(_SCHEMA_GUARD.read_text())


# =========================================================================
# The gate
# =========================================================================


def test_new_migrations_have_schema_guard_coverage():
    """Every migration above the grandfather baseline must have each
    of its ADD COLUMN targets mirrored into db/schema_guard.py."""
    covered = _schema_guard_covered()
    missing = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        num = _migration_number(path)
        if num is None or num <= _GRANDFATHERED_THROUGH:
            continue
        sql = path.read_text()
        if _EXEMPT_MARKER in sql:
            continue  # explicit, documented opt-out
        for table, col in sorted(_extract_added_columns(sql)):
            if (table, col) not in covered:
                missing.append(f"{path.name}: {table}.{col}")

    assert not missing, (
        "Migration(s) add a column with no matching schema_guard.py "
        "self-heal entry. Railway deploys skip db/migrations/, so the "
        "column won't exist in prod and the next runtime Table.select() "
        "will crash — this is the PR #494/#501 `apm_enabled` outage.\n\n"
        "Fix: add an `ALTER TABLE IF EXISTS <table> ADD COLUMN IF NOT "
        "EXISTS <col> ...` block to db/schema_guard.py "
        "(ensure_required_schema_light). OR, if the column genuinely "
        f"doesn't need runtime self-heal, mark the migration file "
        f"`-- {_EXEMPT_MARKER}: <specific reason>`.\n\n"
        "Uncovered:\n  " + "\n  ".join(missing)
    )


# =========================================================================
# Self-tests — prove the gate's machinery works against real data, so a
# silently-broken parser can't make the gate vacuously pass.
# =========================================================================


def test_migration_parser_extracts_real_columns():
    """Parser self-test against a real migration — migration 093 adds
    merchant_tasks.parent_task_id."""
    cols = _extract_added_columns(
        (_MIGRATIONS_DIR / "093_merchant_tasks_parent_task.sql").read_text()
    )
    assert ("merchant_tasks", "parent_task_id") in cols


def test_schema_guard_parser_finds_known_entries():
    """schema_guard.py parser self-test — apm_enabled (the column the
    whole gate exists to protect) and the merchant_tasks columns are
    all self-healed."""
    covered = _schema_guard_covered()
    assert ("merchant_onboarding", "apm_enabled") in covered
    assert ("merchant_tasks", "superseded_by_task_id") in covered
    assert ("merchant_tasks", "parent_task_id") in covered


def test_index_and_constraint_adds_are_not_treated_as_columns():
    """ADD INDEX / ADD CONSTRAINT must not be mistaken for ADD COLUMN —
    otherwise the gate would demand schema_guard entries for indexes."""
    sql = (
        "ALTER TABLE orders ADD INDEX idx_foo (foo);\n"
        "ALTER TABLE orders ADD CONSTRAINT chk_bar CHECK (bar > 0);\n"
        "CREATE INDEX idx_baz ON orders (baz);\n"
    )
    assert _extract_added_columns(sql) == set()


def test_gate_would_flag_an_uncovered_column():
    """Negative self-test: a synthetic migration adding a column that
    is NOT in schema_guard must be both (a) extracted by the parser
    and (b) absent from the covered set — i.e. the gate WOULD fire."""
    synthetic = (
        "ALTER TABLE some_runtime_table\n"
        "    ADD COLUMN IF NOT EXISTS a_brand_new_uncovered_col TEXT;"
    )
    extracted = _extract_added_columns(synthetic)
    assert ("some_runtime_table", "a_brand_new_uncovered_col") in extracted
    assert ("some_runtime_table", "a_brand_new_uncovered_col") not in (
        _schema_guard_covered()
    )


def test_exempt_marker_is_recognized():
    """A migration carrying the exempt marker is skipped by the gate.
    Verifies the marker string the gate looks for matches what the
    docstring tells authors to write."""
    assert _EXEMPT_MARKER == "schema-guard-exempt"
    sample = "-- schema-guard-exempt: index-only migration, no runtime column\n"
    assert _EXEMPT_MARKER in sample


def test_multi_column_alter_extracts_all_columns():
    """A single ALTER TABLE with several comma-separated ADD COLUMN
    clauses (the migration 089 shape) must yield every column."""
    sql = (
        "ALTER TABLE merchant_onboarding\n"
        "  ADD COLUMN IF NOT EXISTS apm_enabled BOOLEAN NOT NULL DEFAULT FALSE,\n"
        "  ADD COLUMN IF NOT EXISTS apm_cadence_days INTEGER NULL,\n"
        "  ADD COLUMN IF NOT EXISTS apm_scope_jsonb JSONB NULL;\n"
    )
    cols = _extract_added_columns(sql)
    assert cols == {
        ("merchant_onboarding", "apm_enabled"),
        ("merchant_onboarding", "apm_cadence_days"),
        ("merchant_onboarding", "apm_scope_jsonb"),
    }
