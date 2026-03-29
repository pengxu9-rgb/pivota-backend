from __future__ import annotations

import re
import sqlite3
from pathlib import Path


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "db" / "migrations" / "058_catalog_core.sql"


def _sqlite_compatible_sql(raw_sql: str) -> str:
    sql = raw_sql.replace("JSONB", "JSON").replace("CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP")
    return re.sub(r"DO \$\$.*?END \$\$;", "", sql, flags=re.DOTALL)


def test_catalog_migration_applies_to_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog_migration_empty.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_sqlite_compatible_sql(MIGRATION_PATH.read_text(encoding="utf-8")))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "catalog_products" in tables
    assert "catalog_offers" in tables
    assert "catalog_quote_snapshots" in tables
    assert "beauty_product_profiles" in tables
    assert "catalog_payment_incentives" in tables


def test_catalog_migration_is_idempotent_on_existing_db(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog_migration_existing.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS catalog_merchants (
                merchant_id TEXT PRIMARY KEY,
                merchant_name TEXT
            )
            """
        )
        sql = _sqlite_compatible_sql(MIGRATION_PATH.read_text(encoding="utf-8"))
        conn.executescript(sql)
        conn.executescript(sql)
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "idx_catalog_products_source_identity" in indexes
    assert "idx_catalog_skus_source_identity" in indexes
    assert "idx_catalog_quote_snapshots_quote_id" in indexes
