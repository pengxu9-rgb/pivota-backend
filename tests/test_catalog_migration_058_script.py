from __future__ import annotations

import sqlite3
from pathlib import Path

import scripts.catalog_migration_058 as module


def test_catalog_migration_058_apply_verify_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog058.sqlite3"
    database_url = f"sqlite:///{db_path}"

    report = module._run("apply-verify", database_url)

    assert report["success"] is True
    assert report["verification"]["missing_tables_count"] == 0
    assert report["verification"]["missing_indexes_count"] == 0
    assert report["verification"]["missing_column_defaults_count"] == 0

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "catalog_products" in tables
    assert "catalog_quote_snapshots" in tables


def test_catalog_migration_058_verify_reports_missing_tables_on_empty_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog058-empty.sqlite3"
    db_path.touch()
    database_url = f"sqlite:///{db_path}"

    report = module._run("verify", database_url)

    assert report["success"] is False
    assert report["verification"]["missing_tables_count"] > 0
    assert "catalog_products" in report["verification"]["missing_tables"]
    assert report["verification"]["missing_column_defaults_count"] == 0
