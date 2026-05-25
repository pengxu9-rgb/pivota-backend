from __future__ import annotations

from pathlib import Path


def test_source_quarantine_migration_shape():
    repo_root = Path(__file__).resolve().parents[1]
    up_sql = (repo_root / "db" / "migrations" / "134_catalog_source_quarantine.sql").read_text()
    down_sql = (
        repo_root / "db" / "migrations" / "down" / "134_catalog_source_quarantine_down.sql"
    ).read_text()

    assert "CREATE TABLE IF NOT EXISTS catalog_source_quarantine" in up_sql
    assert "quarantine_id BIGSERIAL PRIMARY KEY" in up_sql
    assert "match_type TEXT NOT NULL CHECK" in up_sql
    assert "'domain','merchant_platform','source_system_ref'" in up_sql
    assert "state TEXT NOT NULL DEFAULT 'active'" in up_sql
    assert "'active','revoked','expired'" in up_sql
    assert "metadata JSONB" in up_sql
    assert "idx_csq_active_lookup" in up_sql
    assert "ON catalog_source_quarantine (match_type, lower(match_value))" in up_sql
    assert "WHERE state = 'active'" in up_sql
    assert "idx_csq_match_value_lower" in up_sql

    assert "DROP INDEX IF EXISTS idx_csq_active_lookup" in down_sql
    assert "DROP TABLE IF EXISTS catalog_source_quarantine" in down_sql
