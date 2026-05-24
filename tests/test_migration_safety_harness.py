"""Migration safety harness for the channel-program migrations.

OPT-IN via environment variables:
  MIGRATION_HARNESS_DB_URL=postgresql://user:pass@host/dbname
  MIGRATION_HARNESS_DROP_SCHEMA_OK=true

Without both vars set, all tests in this module are skipped. This keeps the
regular test suite hermetic; CI can enable the harness against a disposable
Postgres instance.

Run:
  MIGRATION_HARNESS_DB_URL=postgresql://... MIGRATION_HARNESS_DROP_SCHEMA_OK=true \
    pytest tests/test_migration_safety_harness.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest


_HARNESS_DB_URL = os.getenv("MIGRATION_HARNESS_DB_URL")
_DROP_OK = os.getenv("MIGRATION_HARNESS_DROP_SCHEMA_OK", "").lower() == "true"

pytestmark = pytest.mark.skipif(
    not (_HARNESS_DB_URL and _DROP_OK),
    reason=(
        "Set MIGRATION_HARNESS_DB_URL + "
        "MIGRATION_HARNESS_DROP_SCHEMA_OK=true to run"
    ),
)


_PR1_CHANNEL_PARTNER_COLUMNS = {
    "term_start_date",
    "term_months",
    "per_brand_tail_months",
    "churn_clawback_days",
    "nonpayment_clawback_days",
    "per_brand_subsidy_cap_cents",
    "gmv_take_rate_bp",
    "active_rate_scope",
    "gmv_take_definition",
    "prepaid_credits_supported",
    "monthly_overage_supported",
}


@pytest.fixture(scope="module")
def migrated_conn():
    with _connect() as conn:
        _reset_public_schema(conn)
        _apply_migrations(conn)
        yield conn


def test_full_migration_sequence_applies_cleanly(migrated_conn) -> None:
    """Apply db/migrations/*.sql files with numeric prefixes 100..131."""

    assert _table_exists(migrated_conn, "stripe_events")
    assert _table_exists(migrated_conn, "settlement_snapshots")


def test_full_migration_sequence_is_idempotent_on_reapply(migrated_conn) -> None:
    """Re-apply the same sequence; every migration script should succeed."""

    _apply_migrations(migrated_conn)


def test_channel_partners_has_pr1_contract_columns(migrated_conn) -> None:
    columns = _columns(migrated_conn, "channel_partners")
    missing = sorted(_PR1_CHANNEL_PARTNER_COLUMNS - columns)
    assert missing == []


def test_partner_rate_schedules_table_exists(migrated_conn) -> None:
    assert _table_exists(migrated_conn, "partner_rate_schedules")
    index_defs = _index_defs(migrated_conn, "partner_rate_schedules")
    assert any(
        "unique" in index_def
        and "(channel_partner_id, scope, stream, brand_year, effective_from)"
        in index_def
        for index_def in index_defs
    )


def test_partner_cohort_targets_table_exists(migrated_conn) -> None:
    assert _table_exists(migrated_conn, "partner_cohort_targets")
    row = _fetchone(
        migrated_conn,
        """
        SELECT column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'partner_cohort_targets'
          AND column_name = 'status'
        """,
    )
    assert row and "open" in str(row["column_default"])


def test_monthly_brand_statements_has_freeze_trigger(migrated_conn) -> None:
    assert _table_exists(migrated_conn, "monthly_brand_statements")
    assert _function_exists(
        migrated_conn,
        "prevent_monthly_brand_statement_frozen_mutation",
    )
    assert _trigger_exists(
        migrated_conn,
        "monthly_brand_statements",
        "trg_monthly_brand_statements_freeze_guard",
    )


def test_invoices_has_refunded_cents_column(migrated_conn) -> None:
    assert "refunded_cents" in _columns(migrated_conn, "invoices")


def test_partner_subsidy_ledger_table_exists(migrated_conn) -> None:
    assert _table_exists(migrated_conn, "partner_subsidy_ledger")
    assert _trigger_exists(
        migrated_conn,
        "partner_subsidy_ledger",
        "trg_partner_subsidy_ledger_append_only",
    )
    trigger = _fetchone(
        migrated_conn,
        """
        SELECT pg_get_triggerdef(t.oid) AS triggerdef
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        WHERE c.relname = 'partner_subsidy_ledger'
          AND t.tgname = 'trg_partner_subsidy_ledger_append_only'
          AND NOT t.tgisinternal
        """,
    )
    assert trigger and "prevent_monetization_append_only_mutation" in trigger["triggerdef"]


def test_commerce_attribution_edges_has_gmv_classification_columns(migrated_conn) -> None:
    columns = _columns(migrated_conn, "commerce_attribution_edges")
    assert {
        "gmv_channel",
        "third_party_platform",
        "third_party_platform_fee_pct",
    }.issubset(columns)


def test_settlement_snapshots_trigger_swap(migrated_conn) -> None:
    assert _function_exists(
        migrated_conn,
        "prevent_settlement_snapshot_payload_mutation",
    )
    assert _trigger_exists(
        migrated_conn,
        "settlement_snapshots",
        "trg_settlement_snapshots_settle_only",
    )
    assert not _trigger_exists(
        migrated_conn,
        "settlement_snapshots",
        "trg_settlement_snapshots_append_only",
    )

    partner_id = _insert_harness_partner(migrated_conn)
    billing_run_id = _insert_harness_billing_run(migrated_conn)
    file_id = _insert_harness_settlement_file(migrated_conn, partner_id)
    snapshot_id = _insert_harness_settlement_snapshot(
        migrated_conn,
        partner_id,
        billing_run_id,
    )

    _execute(
        migrated_conn,
        """
        UPDATE settlement_snapshots
        SET settled_at = NOW(), settled_via_file_id = %s
        WHERE id = %s
        """,
        (file_id, snapshot_id),
    )
    with pytest.raises(Exception):
        _execute(
            migrated_conn,
            """
            UPDATE settlement_snapshots
            SET computed_comp_cents = computed_comp_cents + 1
            WHERE id = %s
            """,
            (snapshot_id,),
        )
    migrated_conn.rollback()


def test_settlement_files_table_exists(migrated_conn) -> None:
    assert _table_exists(migrated_conn, "settlement_files")
    constraints = _constraint_names(migrated_conn, "settlement_files")
    assert "uq_settlement_files_partner_month" in constraints
    assert "ck_settlement_files_carryover_forward_nonpos" in constraints
    assert "ck_settlement_files_carryover_applied_nonpos" in constraints


def test_markato_seed_partner_exists(migrated_conn) -> None:
    partner = _markato_partner_or_skip(migrated_conn)
    assert partner["archetype"] == "curated_marketplace"
    assert partner["term_months"] == 12
    assert partner["per_brand_tail_months"] == 36
    assert partner["churn_clawback_days"] == 90
    assert partner["nonpayment_clawback_days"] == 60
    assert partner["per_brand_subsidy_cap_cents"] == 500000
    assert partner["gmv_take_rate_bp"] == 1000
    assert partner["active_rate_scope"] == "B"
    assert partner["gmv_take_definition"] == "net"
    assert partner["prepaid_credits_supported"] is True
    assert partner["monthly_overage_supported"] is True


def test_markato_seed_rate_schedules_exist(migrated_conn) -> None:
    partner = _markato_partner_or_skip(migrated_conn)
    rows = _fetchall(
        migrated_conn,
        """
        SELECT stream, brand_year, rate_bp
        FROM partner_rate_schedules
        WHERE channel_partner_id = %s
          AND scope = 'B'
        ORDER BY stream, brand_year
        """,
        (partner["id"],),
    )
    actual = {(row["stream"], row["brand_year"]): row["rate_bp"] for row in rows}
    expected = {
        ("subscription", 1): 2700,
        ("subscription", 2): 1700,
        ("subscription", 3): 700,
        ("credit_overage", 1): 1700,
        ("credit_overage", 2): 1200,
        ("credit_overage", 3): 700,
        ("gmv_take", 1): 3000,
        ("gmv_take", 2): 2200,
        ("gmv_take", 3): 1200,
    }
    assert actual == expected


def _connect():
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(_HARNESS_DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn


def _reset_public_schema(conn) -> None:
    if not _DROP_OK:
        pytest.fail("Refusing to drop schema without MIGRATION_HARNESS_DROP_SCHEMA_OK=true")
    _execute(
        conn,
        """
        DROP SCHEMA IF EXISTS public CASCADE;
        CREATE SCHEMA public;
        GRANT ALL ON SCHEMA public TO public;
        """,
    )


def _apply_migrations(conn) -> None:
    for path in _migration_files():
        _apply_migration_file(conn, path)


def _apply_migration_file(conn, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    if not sql.strip():
        return
    try:
        _execute(conn, sql)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        pytest.fail(
            f"{path.name} failed while applying migration sequence:\n"
            f"{exc}\n\nSQL excerpt:\n{_script_excerpt(sql)}"
        )


def _migration_files() -> list[Path]:
    migrations_dir = Path(__file__).resolve().parents[1] / "db" / "migrations"
    files: list[Path] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        prefix = path.name.split("_", 1)[0]
        if prefix.isdigit() and 100 <= int(prefix) <= 131:
            files.append(path)
    return files


def _execute(conn, sql: str, params: tuple[Any, ...] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _fetchone(
    conn,
    sql: str,
    params: tuple[Any, ...] | None = None,
) -> Any | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _fetchall(
    conn,
    sql: str,
    params: tuple[Any, ...] | None = None,
) -> list[Any]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _table_exists(conn, table_name: str) -> bool:
    row = _fetchone(
        conn,
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        (table_name,),
    )
    return row is not None


def _columns(conn, table_name: str) -> set[str]:
    rows = _fetchall(
        conn,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        (table_name,),
    )
    return {str(row[0] if not isinstance(row, dict) else row["column_name"]) for row in rows}


def _function_exists(conn, function_name: str) -> bool:
    row = _fetchone(
        conn,
        """
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname = %s
        """,
        (function_name,),
    )
    return row is not None


def _trigger_exists(conn, table_name: str, trigger_name: str) -> bool:
    row = _fetchone(
        conn,
        """
        SELECT 1
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = %s
          AND t.tgname = %s
          AND NOT t.tgisinternal
        """,
        (table_name, trigger_name),
    )
    return row is not None


def _index_defs(conn, table_name: str) -> list[str]:
    rows = _fetchall(
        conn,
        """
        SELECT LOWER(indexdef) AS indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = %s
        """,
        (table_name,),
    )
    return [str(row["indexdef"] if isinstance(row, dict) else row[0]) for row in rows]


def _constraint_names(conn, table_name: str) -> set[str]:
    rows = _fetchall(
        conn,
        """
        SELECT constraint_name
        FROM information_schema.table_constraints
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        (table_name,),
    )
    return {
        str(row["constraint_name"] if isinstance(row, dict) else row[0])
        for row in rows
    }


def _insert_harness_partner(conn) -> int:
    row = _fetchone(
        conn,
        """
        INSERT INTO channel_partners (
          legal_name,
          archetype,
          status,
          active_rate_scope,
          gmv_take_definition
        ) VALUES (
          %s,
          'agency',
          'active',
          'B',
          'net'
        )
        RETURNING id
        """,
        (f"Harness Partner {uuid4()}",),
    )
    return int(row["id"] if isinstance(row, dict) else row[0])


def _insert_harness_billing_run(conn) -> int:
    row = _fetchone(
        conn,
        """
        INSERT INTO billing_runs (
          period_start,
          period_end,
          idempotency_key,
          status,
          completed_at
        ) VALUES (
          DATE '2025-06-01',
          DATE '2025-06-30',
          %s,
          'completed',
          NOW()
        )
        RETURNING id
        """,
        (f"harness_trigger_swap_{uuid4()}",),
    )
    return int(row["id"] if isinstance(row, dict) else row[0])


def _insert_harness_settlement_file(conn, partner_id: int) -> int:
    row = _fetchone(
        conn,
        """
        INSERT INTO settlement_files (
          channel_partner_id,
          calendar_month,
          transfer_amount_cents,
          source_snapshot_ids_jsonb
        ) VALUES (
          %s,
          DATE '2025-06-01',
          100,
          '[]'::jsonb
        )
        RETURNING id
        """,
        (partner_id,),
    )
    return int(row["id"] if isinstance(row, dict) else row[0])


def _insert_harness_settlement_snapshot(
    conn,
    partner_id: int,
    billing_run_id: int,
) -> int:
    row = _fetchone(
        conn,
        """
        INSERT INTO settlement_snapshots (
          billing_run_id,
          channel_partner_id,
          snapshot_payload_jsonb,
          computed_comp_cents
        ) VALUES (
          %s,
          %s,
          '{"net_comp_cents": 100}'::jsonb,
          100
        )
        RETURNING id
        """,
        (billing_run_id, partner_id),
    )
    return int(row["id"] if isinstance(row, dict) else row[0])


def _markato_partner_or_skip(conn) -> dict[str, Any]:
    row = _fetchone(
        conn,
        """
        SELECT *
        FROM channel_partners
        WHERE archetype = 'curated_marketplace'
          AND legal_name ILIKE 'markato%%'
        ORDER BY id
        LIMIT 1
        """,
    )
    if not row:
        pytest.skip(
            "No pre-existing Markato channel_partners row was present before "
            "migration 125; seed assertions are not applicable on an empty DB."
        )
    return dict(row)


def _script_excerpt(sql: str) -> str:
    compact_lines = [line.rstrip() for line in sql.strip().splitlines() if line.strip()]
    return "\n".join(compact_lines[:30])[:1600]
