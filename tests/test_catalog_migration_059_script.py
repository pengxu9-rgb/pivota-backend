from __future__ import annotations

from pathlib import Path

import scripts.catalog_migration_059 as module


def test_catalog_migration_059_verify_sqlite_is_safe_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog059.sqlite3"
    db_path.touch()
    database_url = f"sqlite:///{db_path}"

    report = module._run("verify", database_url)

    assert report["success"] is True
    assert report["database_kind"] == "sqlite"
    assert report["verification"]["missing_indexes_count"] == 0
    assert report["verification"]["skipped"] is True


def test_catalog_migration_059_sql_uses_concurrent_indexes() -> None:
    sql_blob = module.MIGRATION_PATH.read_text(encoding="utf-8")

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_merchants_merchant_name_trgm" in sql_blob
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_skus_source_variant_id_lookup" in sql_blob


def test_catalog_migration_059_apply_postgres_uses_autocommit_and_statement_loop(monkeypatch) -> None:
    executed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, statement):
            executed.append(statement)

    class FakeConnection:
        def __init__(self):
            self.autocommit = False
            self.closed = False

        def cursor(self):
            return FakeCursor()

        def close(self):
            self.closed = True

    fake_connection = FakeConnection()

    class FakePsycopg2:
        @staticmethod
        def connect(_database_url):
            return fake_connection

    monkeypatch.setitem(__import__("sys").modules, "psycopg2", FakePsycopg2)

    result = module._apply_postgres("postgresql://example")

    assert result["applied"] is True
    assert result["statement_count"] == len(module.POSTGRES_STATEMENTS)
    assert fake_connection.autocommit is True
    assert executed == module.POSTGRES_STATEMENTS
    assert fake_connection.closed is True
