"""Guard test for migration 167 — external-conversion representation on
commerce_attribution_edges (T2-2, gap #4).

Same house style as the 090 migration-file guard: assert the DDL the .sql
ships (columns + idempotency guard + honesty CHECK) AND cross-check it against
the SQLAlchemy Table def in db/commerce_attribution.py so the two never drift.
No real DB needed — prod applies the migration manually (see migration 159/166
headers); the DDL behavior is a property of the SQL text + the ORM model.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATION = REPO / "db" / "migrations" / "167_commerce_attribution_edges_external_conversion.sql"


def _sql() -> str:
    assert MIGRATION.exists(), f"missing migration file: {MIGRATION}"
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_is_additive_and_non_destructive() -> None:
    sql = _sql().upper()
    assert len(sql) > 200
    for forbidden in ("DROP TABLE", "TRUNCATE", "DELETE FROM", "DROP COLUMN"):
        # (DROP COLUMN appears only inside the commented-out DOWN block)
        active = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
        assert forbidden not in active, f"destructive DDL in active migration: {forbidden}"


def test_migration_adds_external_conversion_columns() -> None:
    sql = _sql()
    for col in (
        "state",
        "converted_at",
        "currency",
        "external_order_id",
        "source",
        "click_id",
        "gross_attributed_gmv_cents",
    ):
        assert re.search(
            rf"ADD COLUMN IF NOT EXISTS\s+{col}\b", sql, re.IGNORECASE
        ), f"migration missing additive column: {col}"


def test_migration_ships_idempotency_guard() -> None:
    sql = _sql()
    # UNIQUE index on (merchant_id, external_order_id) is the money-safe replay guard.
    m = re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS\s+\w+\s+ON\s+commerce_attribution_edges\s*\(([^)]*)\)",
        sql,
        re.IGNORECASE,
    )
    assert m, "no unique index shipped"
    cols = {c.strip() for c in m.group(1).split(",")}
    assert cols == {"merchant_id", "external_order_id"}


def test_migration_state_check_is_honest() -> None:
    sql = _sql()
    m = re.search(r"CHECK\s*\(\s*state[^)]*IN\s*\(([^)]*)\)", sql, re.IGNORECASE | re.DOTALL)
    assert m, "no state CHECK constraint"
    states = set(re.findall(r"'([^']+)'", m.group(1)))
    assert states == {"referred", "converted"}


def test_orm_table_carries_new_columns_and_guard() -> None:
    # Cross-check: the SQLAlchemy model must expose the same additive columns +
    # the unique guard, or reads/writes through the ORM would drift from the DB.
    from db.commerce_attribution import commerce_attribution_edges

    cols = set(commerce_attribution_edges.c.keys())
    for col in ("state", "converted_at", "currency", "external_order_id", "source",
                "click_id", "gross_attributed_gmv_cents"):
        assert col in cols, f"ORM Table missing column: {col}"

    unique_pairs = {
        tuple(sorted(c.name for c in idx.columns))
        for idx in commerce_attribution_edges.indexes
        if idx.unique
    }
    assert tuple(sorted(("merchant_id", "external_order_id"))) in unique_pairs
