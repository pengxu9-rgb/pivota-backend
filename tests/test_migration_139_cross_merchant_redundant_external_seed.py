"""Shape tests for migration 139 (tombstone cross-merchant redundant external_seed).

Verifies the predicate and the up/down pair without requiring a DB. A live
smoke test belongs alongside the existing MIGRATION_PR1_DB_URL pattern (see
test_catalog_offer_suppression_migration.py) but is deferred — the migration
is a SELECT-then-UPDATE on a small (~50-row) cohort and is exercised by the
6h catalog_row_trust backfill cron once applied.
"""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
UP_SQL = REPO / "db" / "migrations" / "139_tombstone_cross_merchant_redundant_external_seed.sql"
DOWN_SQL = (
    REPO
    / "db"
    / "migrations"
    / "down"
    / "139_tombstone_cross_merchant_redundant_external_seed_down.sql"
)


def test_migration_139_up_shape() -> None:
    sql = UP_SQL.read_text(encoding="utf-8")

    # Wrapped in an explicit transaction.
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql

    # Single UPDATE on catalog_products.
    assert "UPDATE catalog_products" in sql
    assert "SET suppression_reason = 'cross_merchant_redundant_external_seed'" in sql

    # Predicate matches the audit cohort exactly.
    assert "WHERE merchant_id = 'external_seed'" in sql
    assert "AND sync_status = 'live'" in sql
    assert "AND suppression_reason IS NULL" in sql
    assert "AND content_key IS NOT NULL" in sql

    # First-party sibling check.
    assert "EXISTS (" in sql
    assert "sibling.content_key = catalog_products.content_key" in sql
    assert "sibling.merchant_id <> 'external_seed'" in sql
    assert "sibling.sync_status = 'live'" in sql
    assert "sibling.suppression_reason IS NULL" in sql


def test_migration_139_is_idempotent() -> None:
    # The WHERE clause includes `suppression_reason IS NULL`, so a second
    # apply cannot re-touch rows already marked. Belt-and-suspenders check:
    # the literal string we set must not also be the sentinel that the
    # WHERE clause would re-include.
    sql = UP_SQL.read_text(encoding="utf-8")

    assert "suppression_reason IS NULL" in sql
    assert "SET suppression_reason = NULL" not in sql


def _sql_only(text: str) -> str:
    """Drop `--` comments before asserting on statement shape.

    Asserting against the raw file lets a COMMENT satisfy the assertion. That
    bit here: the explanatory comment added alongside the guard contains the
    literal `suppression_metadata IS NULL`, so deleting the guard from the
    actual WHERE clause left this test green (mutation-verified). An assertion
    a docstring can satisfy is not an assertion.
    """
    import re as _re

    return _re.sub(r"--[^\n]*", "", text)


def test_migration_139_down_shape() -> None:
    sql = _sql_only(DOWN_SQL.read_text(encoding="utf-8"))

    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert "UPDATE catalog_products" in sql
    # P1a (#1648): the down migration must clear BOTH columns. Clearing the
    # label alone leaves every row gated by `suppressed_at` — a revert that
    # silently does not revert. This assertion previously pinned that defect.
    assert "suppression_reason = NULL" in sql
    assert "suppressed_at = NULL" in sql
    # ...and must not sweep step5_lane4's rows, which carry the same reason
    # string but stamp suppression_metadata.
    assert "suppression_metadata IS NULL" in sql
    assert "WHERE suppression_reason = 'cross_merchant_redundant_external_seed'" in sql
