from __future__ import annotations

import os
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
UP_SQL = REPO / "db" / "migrations" / "132_catalog_offer_suppression_writer_audit.sql"
DOWN_SQL = REPO / "db" / "migrations" / "down" / "132_catalog_offer_suppression_writer_audit_down.sql"


def test_catalog_offer_suppression_migration_shape() -> None:
    sql = UP_SQL.read_text(encoding="utf-8")

    assert "ALTER TABLE IF EXISTS catalog_offers" in sql
    assert "ADD COLUMN IF NOT EXISTS suppression_reason TEXT NULL" in sql
    assert "ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ NULL" in sql
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_offers_suppressed" in sql
    assert "WHERE suppressed_at IS NOT NULL" in sql
    assert "CREATE TABLE IF NOT EXISTS writer_audit_log" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_writer_audit_writer_time" in sql


def test_catalog_offer_suppression_down_migration_shape() -> None:
    sql = DOWN_SQL.read_text(encoding="utf-8")

    assert "DROP INDEX CONCURRENTLY IF EXISTS idx_catalog_offers_suppressed" in sql
    assert "DROP TABLE IF EXISTS writer_audit_log" in sql
    assert "DROP COLUMN IF EXISTS suppressed_at" in sql
    assert "DROP COLUMN IF EXISTS suppression_reason" in sql


def test_agent_pdp_view_refresh_filters_suppressed_offers() -> None:
    source = (REPO / "services" / "agent_pdp_view_assembler.py").read_text(encoding="utf-8")

    assert "FROM catalog_offers o" in source
    assert "o.suppressed_at IS NULL" in source


@pytest.mark.skipif(
    not os.getenv("MIGRATION_PR1_DB_URL"),
    reason="set MIGRATION_PR1_DB_URL to run transactional migration smoke",
)
def test_catalog_offer_suppression_columns_rollback_in_transaction() -> None:
    import psycopg2

    conn = psycopg2.connect(os.environ["MIGRATION_PR1_DB_URL"])
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_offers (
                  offer_id TEXT PRIMARY KEY,
                  sku_key TEXT NOT NULL,
                  product_key TEXT NOT NULL,
                  merchant_id TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                ALTER TABLE catalog_offers
                  ADD COLUMN IF NOT EXISTS suppression_reason TEXT NULL,
                  ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ NULL
                """
            )
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'catalog_offers'
                  AND column_name IN ('suppression_reason', 'suppressed_at')
                """
            )
            assert {row[0] for row in cur.fetchall()} == {"suppression_reason", "suppressed_at"}
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'catalog_offers'
                  AND column_name IN ('suppression_reason', 'suppressed_at')
                """
            )
            assert {row[0] for row in cur.fetchall()} == set()
    finally:
        conn.close()
