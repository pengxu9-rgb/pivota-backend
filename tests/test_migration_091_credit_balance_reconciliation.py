from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MIGRATION = (
    REPO
    / "db"
    / "migrations"
    / "091_reconcile_merchant_credit_balance_single_credit.sql"
)


def _sql() -> str:
    assert MIGRATION.exists(), f"missing migration: {MIGRATION}"
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_091_adds_single_credit_columns() -> None:
    sql = _sql().upper()
    assert "ADD COLUMN IF NOT EXISTS CREDITS BIGINT NOT NULL DEFAULT 0" in sql
    assert "CHECK (CREDITS >= 0)" in sql
    assert (
        "ADD COLUMN IF NOT EXISTS USD_COGS_INTERNAL NUMERIC(14,4) "
        "NOT NULL DEFAULT 0"
    ) in sql
    assert "ADD COLUMN IF NOT EXISTS ALLOWANCE_CREDITS BIGINT" in sql
    assert "ADD COLUMN IF NOT EXISTS ALLOWANCE_PERIOD_START TIMESTAMPTZ" in sql


def test_migration_091_guards_populated_wallet_drops() -> None:
    sql = _sql()
    assert "DO $$" in sql
    assert "RAISE EXCEPTION" in sql
    assert "feedback_db_access_destructive_ops" in sql
    assert (
        "COALESCE(audit_credits, 0)\n"
        "                 + COALESCE(prompt_credits, 0)\n"
        "                 + COALESCE(execution_credits, 0) > 0"
    ) in sql


def test_migration_091_drops_only_legacy_wallet_columns() -> None:
    sql = _sql().upper()
    assert "DROP COLUMN IF EXISTS AUDIT_CREDITS" in sql
    assert "DROP COLUMN IF EXISTS PROMPT_CREDITS" in sql
    assert "DROP COLUMN IF EXISTS EXECUTION_CREDITS" in sql
    assert "DROP TABLE" not in sql
    assert "TRUNCATE" not in sql
    assert "DELETE FROM" not in sql
