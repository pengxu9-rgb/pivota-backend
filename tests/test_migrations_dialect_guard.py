"""
db/migrations/*.sql is executed by main.startup() as one transaction per file
with every error swallowed. Two files never applied anywhere and were disabled
on 2026-08-19 (see the header comment inside each *.sql.disabled):

* 002_production_tables.sql — MySQL dialect (inline `INDEX idx (col)`,
  `ON UPDATE CURRENT_TIMESTAMP`); fixing it would create an `agent_metrics`
  shape that conflicts with 009_agent_observability.sql (the live one).
* 007_agents_management_upgrade.sql — fails on `agents(status)` (the
  SQLAlchemy `agents` table has `is_active`), and its Part 2 would ALSO create
  the wrong `agent_metrics` before 009 runs.

These guards keep them disabled and keep MySQL syntax out of the active set.
"""
from __future__ import annotations

import glob
import os
import re

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "migrations")

# Must stay disabled; re-enabling either breaks agent_metrics on a clean DB.
DISABLED_FOR_CAUSE = {
    "002_production_tables.sql",
    "007_agents_management_upgrade.sql",
}

_MYSQL_INLINE_INDEX = re.compile(r"^\s*(?:UNIQUE\s+)?(?:INDEX|KEY)\s+\w+\s*\(", re.IGNORECASE | re.MULTILINE)
_MYSQL_ON_UPDATE = re.compile(r"ON\s+UPDATE\s+CURRENT_TIMESTAMP", re.IGNORECASE)


def _active_sql_files():
    return sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))


def test_known_bad_migrations_stay_disabled() -> None:
    active = {os.path.basename(p) for p in _active_sql_files()}
    leaked = DISABLED_FOR_CAUSE & active
    assert not leaked, f"re-enabled migrations that must stay disabled: {sorted(leaked)}"
    for name in DISABLED_FOR_CAUSE:
        assert os.path.exists(os.path.join(MIGRATIONS_DIR, name + ".disabled")), name


def test_active_migrations_are_postgres_dialect() -> None:
    active = _active_sql_files()
    # Floor: `assert not offenders` is vacuously true if the glob finds nothing
    # (a moved directory, a renamed constant), which is exactly how a guard
    # like this stops guarding without anyone noticing.
    assert len(active) > 100, f"only {len(active)} migrations discovered — the glob is wrong"
    offenders = []
    for path in active:
        with open(path, "r", encoding="utf-8") as fh:
            body = fh.read()
        if _MYSQL_INLINE_INDEX.search(body) or _MYSQL_ON_UPDATE.search(body):
            offenders.append(os.path.basename(path))
    assert not offenders, f"MySQL-dialect syntax in active Postgres migrations: {offenders}"
