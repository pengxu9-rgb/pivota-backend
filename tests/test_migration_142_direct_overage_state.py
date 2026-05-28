from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MIGRATION = REPO / "db" / "migrations" / "142_direct_merchant_overage_state.sql"
SCHEMA_GUARD = REPO / "db" / "schema_guard.py"


def test_migration_142_adds_direct_overage_columns_only() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "ALTER TABLE merchant_credit_balance" in sql
    for column in (
        "overage_pending_credits",
        "overage_charged_credits",
        "overage_blocked_until_payment",
        "overage_last_payment_intent_id",
        "overage_last_failed_at",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql

    # Direct path only: no channel-partner ledger/table writes in this migration.
    assert "merchant_credits" not in sql
    assert "credit_ledger" not in sql
    assert "partner_rev_share" not in sql


def test_schema_guard_covers_direct_overage_runtime_columns() -> None:
    guard = SCHEMA_GUARD.read_text(encoding="utf-8")

    assert "ALTER TABLE IF EXISTS merchant_credit_balance" in guard
    for column in (
        "purchased_credits",
        "overage_pending_credits",
        "overage_charged_credits",
        "overage_blocked_until_payment",
        "overage_last_payment_intent_id",
        "overage_last_failed_at",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in guard
