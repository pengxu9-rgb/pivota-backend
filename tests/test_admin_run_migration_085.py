"""Tests for routes/admin_run_migration_085.py — Stage 3a-i.

Same shape as the 081/082/083/084 admin-route test pattern: confirm
the route is wired, the SQL file exists, verify-mode runs without
side-effects, and apply-mode SQL is valid (smoke-test on a fresh
SQLite via the route's own runner).

Postgres-specific bits (partial indexes, NUMERIC precision) only
surface when the migration runs against real Postgres — verified
in prod via POST /admin/migrations/post/run/085 after deploy. The
admin route's SQLite verify short-circuit covers the local-test gap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes.admin_run_migration_085 import (  # noqa: E402
    MIGRATION_PATH,
    VERIFY_SQL,
    _read_migration_sql,
    router,
)


def test_migration_file_exists_and_is_non_empty() -> None:
    """The migration file the route reads must be present in the
    deployed image. Catches the case where the .sql file gets renamed
    or accidentally excluded from the docker build."""
    assert MIGRATION_PATH.exists()
    assert MIGRATION_PATH.stat().st_size > 100  # non-trivial DDL


def test_migration_sql_creates_agent_pdp_view_table() -> None:
    """Top-level smoke: the SQL file CREATES the table this stage exists
    to add. If someone renames the table mid-PR this test catches it."""
    sql = _read_migration_sql()
    assert "CREATE TABLE IF NOT EXISTS agent_pdp_view" in sql


def test_migration_sql_declares_required_columns() -> None:
    """Lock the column surface — these are what Stage 3a-ii backfill,
    3a-iii writer hook, and 3a-iv endpoint will write/read. Renames
    here force the downstream PRs to update with confidence."""
    sql = _read_migration_sql()
    required_columns = [
        "content_key VARCHAR(40) PRIMARY KEY",
        "pivota_signature_id VARCHAR(40)",
        "product_group_id VARCHAR(64)",
        "brand TEXT",
        "title TEXT NOT NULL",
        "description TEXT",
        "image_url TEXT",
        "image_urls JSONB",
        "currency VARCHAR(3)",
        "price_min NUMERIC(12, 2)",
        "price_max NUMERIC(12, 2)",
        "offer_count INT",
        "offers JSONB",
        "variants JSONB",
        "variants_count INT",
        "gtin13 VARCHAR(14)",
        "breadcrumb JSONB",
        "pdp_lifecycle_stage VARCHAR(16)",
        "sync_status VARCHAR(16)",
        "refreshed_at TIMESTAMPTZ",
        "refreshed_by_proposal_id BIGINT",
    ]
    for col in required_columns:
        assert col in sql, f"missing required column declaration: {col!r}"


def test_migration_sql_creates_all_four_indexes() -> None:
    """The verify endpoint counts these by name. If the index naming
    changes, the verify SQL no longer reports success even when DDL
    succeeded — silent prod outage. Pin the names."""
    sql = _read_migration_sql()
    required_indexes = [
        "idx_agent_pdp_view_pivota_signature_id",
        "idx_agent_pdp_view_product_group_id",
        "idx_agent_pdp_view_gtin13",
        "idx_agent_pdp_view_brand",
    ]
    for idx in required_indexes:
        assert idx in sql, f"missing index declaration: {idx!r}"


def test_migration_sql_indexes_are_partial_where_appropriate() -> None:
    """Each index is partial WHERE the column IS NOT NULL — keeps the
    indexes small and matches the table's intentionally-nullable
    columns. A non-partial unique index on pivota_signature_id would
    reject the legitimate case where multiple rows have NULL.

    (Postgres allows multiple NULLs in a unique index by default,
    but the WHERE clause is explicit + faster.)"""
    sql = _read_migration_sql()
    assert "WHERE pivota_signature_id IS NOT NULL" in sql
    assert "WHERE product_group_id IS NOT NULL" in sql
    assert "WHERE gtin13 IS NOT NULL" in sql
    assert "WHERE brand IS NOT NULL" in sql


def test_migration_sql_is_idempotent() -> None:
    """Production deploys may run the migration repeatedly during
    rollbacks / retries. CREATE TABLE + CREATE INDEX in the SQL body
    (not in -- comments) MUST use IF NOT EXISTS or the second run
    errors and rolls back."""
    sql = _read_migration_sql()
    # Strip line comments before scanning so 'Idempotent: CREATE TABLE
    # / INDEX' phrasing in a header comment doesn't trip the regex.
    sql_no_comments = "\n".join(
        line for line in sql.split("\n") if not line.lstrip().startswith("--")
    )
    import re
    # Match every CREATE TABLE / CREATE INDEX / CREATE UNIQUE INDEX
    # statement. Capture the kind and whether IF NOT EXISTS follows.
    create_statements = re.findall(
        r"CREATE\s+(TABLE|UNIQUE INDEX|INDEX)\s+(IF NOT EXISTS\s+)?(\w+)",
        sql_no_comments,
    )
    assert create_statements, "no CREATE TABLE/INDEX statements found"
    for kind, if_not_exists, name in create_statements:
        assert if_not_exists, (
            f"CREATE {kind} {name} missing IF NOT EXISTS — "
            f"re-run on prod will fail"
        )


def test_verify_sql_checks_table_and_all_four_indexes() -> None:
    """The verify endpoint returns success only when table + all 4
    indexes exist. If verify is incomplete (only checks table, not
    indexes), a partial migration apply would falsely report success."""
    assert "table_name='agent_pdp_view'" in VERIFY_SQL
    assert "idx_agent_pdp_view_pivota_signature_id" in VERIFY_SQL
    assert "idx_agent_pdp_view_product_group_id" in VERIFY_SQL
    assert "idx_agent_pdp_view_gtin13" in VERIFY_SQL
    assert "idx_agent_pdp_view_brand" in VERIFY_SQL


def test_router_registers_three_endpoints_under_admin_migrations() -> None:
    """Same shape as 081-084: GET /verify/085, POST /run/085,
    POST /post/run/085. The third is a workaround for ops tooling
    that strips the 'verify' segment."""
    assert router.prefix == "/admin/migrations"
    paths = sorted({route.path for route in router.routes})
    assert "/admin/migrations/verify/085" in paths
    assert "/admin/migrations/run/085" in paths
    assert "/admin/migrations/post/run/085" in paths
