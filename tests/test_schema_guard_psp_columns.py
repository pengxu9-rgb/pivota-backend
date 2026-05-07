import os

import pytest


os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")


@pytest.mark.asyncio
async def test_ensure_required_schema_light_adds_merchant_psp_columns_in_fast_mode(monkeypatch):
    from db import schema_guard as sg

    executed = []

    class DummyDB:
        async def execute(self, query):
            executed.append(str(query))

    async def noop_connect():
        return None

    monkeypatch.setattr(sg, "IS_POSTGRES", True)
    monkeypatch.setattr(sg, "IS_SQLITE", False)
    monkeypatch.setattr(sg, "database", DummyDB())
    monkeypatch.setattr(sg, "_ensure_database_connected", noop_connect)

    await sg.ensure_required_schema_light()

    assert any("ALTER TABLE IF EXISTS orders" in stmt for stmt in executed)
    merchant_stmt = next(stmt for stmt in executed if "ALTER TABLE IF EXISTS merchant_psps" in stmt)
    assert "ADD COLUMN IF NOT EXISTS environment" in merchant_stmt
    assert "ADD COLUMN IF NOT EXISTS provider_config" in merchant_stmt
    assert "ADD COLUMN IF NOT EXISTS validation_status" in merchant_stmt

    # Pivota canonical PDP columns (migration 071) must be ensured by
    # the same fast-mode startup guard, otherwise the canonical
    # resolver + audit URL fallback both 500 with UndefinedColumnError
    # in production (deploy-skipped-migrations failure mode).
    catalog_stmt = next(
        stmt for stmt in executed if "ALTER TABLE IF EXISTS catalog_products" in stmt
    )
    assert "ADD COLUMN IF NOT EXISTS pivota_signature_id" in catalog_stmt
    assert "ADD COLUMN IF NOT EXISTS pivota_canonical_url" in catalog_stmt
    assert any(
        "idx_catalog_products_pivota_signature" in stmt for stmt in executed
    ), "partial unique index for pivota_signature_id not created"
