"""CI gate: a SQLite self-heal's DEFAULT must match its migration's DEFAULT.

`tests/test_schema_guard_migration_coverage.py` already checks that every new
migration HAS a self-heal entry. It does not check that the entry says the same
thing, and that gap shipped a real divergence: the `catalog_merchants.indexable`
heal read `BOOLEAN DEFAULT FALSE` while migration 139 says
`BOOLEAN NOT NULL DEFAULT TRUE` — copy-pasted from the `merchant_stores.is_primary`
line above it in #1536. On a self-healed SQLite DB that marked EVERY existing
merchant non-indexable, and `COALESCE(m.indexable, TRUE) IS TRUE` — the gate on
both cross-merchant recall lanes — then dropped all of them from search.

A heal that disagrees with prod is worse than no heal: /health goes green, the
column exists, and the behaviour is silently inverted.

Scope is deliberately the DEFAULT VALUE only. Type NAMES legitimately differ
where SQLite has no equivalent (TIMESTAMPTZ -> TIMESTAMP, BIGINT -> NUMERIC),
and SQLite's dynamic typing makes that harmless; NOT NULL is likewise only
laxer, since the default backfills either way. A wrong default value is neither.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional, Tuple

_REPO = Path(__file__).resolve().parents[1]
_SCHEMA_GUARD = _REPO / "db" / "schema_guard.py"
_MIGRATIONS_DIR = _REPO / "db" / "migrations"

# The `sqlite_type` map inside `db/schema_guard.py`. Parsed from source rather
# than imported because it is a local inside the heal function — the same
# approach test_schema_guard_migration_coverage.py takes.
_HEAL_MAP_RE = re.compile(r"sqlite_type = \{(.*?)\n\s{12}\}", re.DOTALL)
_HEAL_ENTRY_RE = re.compile(r'\("(\w+)",\s*"(\w+)"\):\s*"([^"]+)"')

_ALTER_TABLE_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?[\"']?(\w+)[\"']?(.*?);",
    re.IGNORECASE | re.DOTALL,
)
_ADD_COLUMN_RE = re.compile(
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?(\w+)[\"']?\s+(.*?)[,;]?\s*$",
    re.IGNORECASE,
)
_DEFAULT_RE = re.compile(r"\bDEFAULT\s+('(?:[^']|'')*'|[^\s,;]+)", re.IGNORECASE)

# Columns with no migration to compare against. `merchant_stores.created_at`
# predates migration 190 — the REQUIRED_SCHEMA spec says so explicitly — so the
# heal is the only definition of it and there is nothing to agree with.
_NO_MIGRATION = {("merchant_stores", "created_at")}


def _normalize_default(clause: str) -> Optional[str]:
    match = _DEFAULT_RE.search(clause)
    if match is None:
        return None
    literal = match.group(1).strip()
    # TRUE/true/1 are the same value; compare case-insensitively and map the
    # boolean keywords onto the digits SQLite actually stores.
    lowered = literal.lower()
    return {"true": "1", "false": "0"}.get(lowered, lowered)


def _heal_entries() -> Dict[Tuple[str, str], str]:
    source = _SCHEMA_GUARD.read_text(encoding="utf-8")
    block = _HEAL_MAP_RE.search(source)
    assert block is not None, (
        "could not locate the `sqlite_type = {...}` heal map in db/schema_guard.py "
        "— if it was renamed or reindented, update this gate rather than deleting it"
    )
    entries = {(t, c): ddl for t, c, ddl in _HEAL_ENTRY_RE.findall(block.group(1))}
    assert entries, "parsed the heal map but found no entries — the regex has rotted"
    return entries


def _migration_defaults() -> Dict[Tuple[str, str], str]:
    defaults: Dict[Tuple[str, str], str] = {}
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        for alter in _ALTER_TABLE_RE.finditer(sql):
            table = alter.group(1).lower()
            for line in alter.group(2).splitlines():
                add = _ADD_COLUMN_RE.search(line)
                if add is None:
                    continue
                # Later migrations win — a column can be re-defaulted.
                defaults[(table, add.group(1).lower())] = add.group(2).strip()
    return defaults


def test_sqlite_heal_defaults_match_their_migrations() -> None:
    migrations = _migration_defaults()
    mismatches = []
    compared = 0

    for (table, column), heal_ddl in sorted(_heal_entries().items()):
        if (table, column) in _NO_MIGRATION:
            continue
        migration_ddl = migrations.get((table.lower(), column.lower()))
        if migration_ddl is None:
            mismatches.append(
                f"{table}.{column}: heal says {heal_ddl!r} but NO migration adds "
                f"this column — either the migration is missing or the pair "
                f"belongs in _NO_MIGRATION with a reason"
            )
            continue
        compared += 1
        heal_default = _normalize_default(heal_ddl)
        migration_default = _normalize_default(migration_ddl)
        if heal_default != migration_default:
            mismatches.append(
                f"{table}.{column}: heal DEFAULT {heal_default!r} != migration "
                f"DEFAULT {migration_default!r}  (heal={heal_ddl!r}, "
                f"migration={migration_ddl!r})"
            )

    assert not mismatches, (
        "SQLite self-heal defaults disagree with their migrations. A self-healed "
        "DB will behave differently from prod while /health reports green:\n  "
        + "\n  ".join(mismatches)
    )
    # Guard the guard: if the parsing silently stops matching, the loop above
    # compares nothing and passes vacuously.
    assert compared >= 8, (
        f"only {compared} heal entries were compared against a migration — the "
        "migration or heal-map parsing has rotted and this gate is now vacuous"
    )
