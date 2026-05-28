"""Guard test for migration 090 — widen agent_center_usage_events billing
CHECK constraints so the credit ledger (Brief 3) can write debit/credit rows.

Same shape as the 085 migration-file guard: parse the .sql text and assert
the DDL it ships. The high-value assertion is the cross-check — that the
exact billing values merchant_credit_balance_service writes are all inside
the migration's widened allow-lists. If either side drifts (someone adds a
'refund' billing_mode in the service, or trims a value from the migration),
this test fails before the constraint rejects rows in production.

No real DB needed: the constraint behavior is a property of the SQL text +
the service's literals, both of which we can read statically. The actual
INSERT-against-Postgres path is exercised in prod after the migration is
applied via the Railway public proxy (prod skips the startup migration
runner — see reference_railway_prod_startup_skip).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATION = REPO / "db" / "migrations" / "090_widen_usage_events_billing_for_credits.sql"
SERVICE = REPO / "services" / "merchant_credit_balance_service.py"

# The original (migration 067) allow-lists. The widened constraints must
# remain a strict superset so every existing row still passes validation.
ORIGINAL_BILLING_MODE = {"preview_only", "metered"}
ORIGINAL_BILLING_STATUS = {"not_invoiced", "invoiced", "voided"}

# What the credit ledger writes (merchant_credit_balance_service).
CREDIT_BILLING_MODES = {"debit", "credit"}
CREDIT_BILLING_STATUS = {"applied"}


def _read(p: Path) -> str:
    assert p.exists(), f"missing file: {p}"
    return p.read_text(encoding="utf-8")


def _check_values(sql: str, constraint_name: str) -> set[str]:
    """Extract the IN (...) allow-list for a named CHECK constraint."""
    # Find: ADD CONSTRAINT <name> CHECK (<col> IN ('a', 'b', ...))
    m = re.search(
        re.escape(constraint_name) + r"\s+CHECK\s*\([^)]*IN\s*\(([^)]*)\)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert m, f"no widened CHECK found for {constraint_name}"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def test_migration_file_exists_and_is_non_trivial() -> None:
    sql = _read(MIGRATION)
    assert len(sql) > 200, "migration looks empty / stubbed"
    # Non-destructive: only constraint predicates change, nothing dropped/rewritten.
    assert "DROP TABLE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()


def test_migration_is_idempotent() -> None:
    sql = _read(MIGRATION).upper()
    # DROP CONSTRAINT IF EXISTS makes re-running a no-op.
    assert sql.count("DROP CONSTRAINT IF EXISTS") >= 2


def test_billing_mode_constraint_widened_as_superset() -> None:
    sql = _read(MIGRATION)
    allowed = _check_values(sql, "chk_agent_center_usage_events_billing_mode")
    # Superset: existing rows (preview_only/metered) must still pass.
    assert ORIGINAL_BILLING_MODE <= allowed, (
        f"widened billing_mode dropped original values: "
        f"{ORIGINAL_BILLING_MODE - allowed}"
    )
    # New credit-ledger values present.
    assert CREDIT_BILLING_MODES <= allowed, (
        f"billing_mode missing credit values: {CREDIT_BILLING_MODES - allowed}"
    )


def test_billing_status_constraint_widened_as_superset() -> None:
    sql = _read(MIGRATION)
    allowed = _check_values(sql, "chk_agent_center_usage_events_billing_status")
    assert ORIGINAL_BILLING_STATUS <= allowed, (
        f"widened billing_status dropped original values: "
        f"{ORIGINAL_BILLING_STATUS - allowed}"
    )
    assert CREDIT_BILLING_STATUS <= allowed, (
        f"billing_status missing credit values: {CREDIT_BILLING_STATUS - allowed}"
    )


def test_service_billing_literals_are_within_migration_allowlists() -> None:
    """Cross-check: every billing value the credit service emits must be
    permitted by the migration. Catches drift on either side before it
    becomes a production CHECK violation."""
    sql = _read(MIGRATION)
    service_src = _read(SERVICE)

    mode_allowed = _check_values(sql, "chk_agent_center_usage_events_billing_mode")
    status_allowed = _check_values(sql, "chk_agent_center_usage_events_billing_status")

    # The service declares: operation: Literal["debit", "credit"] and writes
    # billing_mode=operation, billing_status="applied". Assert the literals
    # the source actually contains are covered.
    assert 'Literal["debit", "credit"]' in service_src, (
        "credit service operation literal changed — update this guard + the "
        "migration allow-list together"
    )
    assert '"billing_status": "applied"' in service_src, (
        "credit service billing_status literal changed — update this guard + "
        "the migration allow-list together"
    )
    assert CREDIT_BILLING_MODES <= mode_allowed
    assert CREDIT_BILLING_STATUS <= status_allowed
