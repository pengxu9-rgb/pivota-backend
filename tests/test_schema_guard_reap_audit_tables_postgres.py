"""Postgres twin of the behaviour half of tests/test_schema_guard_reap_audit_tables.py.

The real Postgres self-heal branch, with a neighbouring statement made to fail: the Reap operator
audit tables (migrations 253, 255, 257) must still be created, and a failing audit table must not
take the purchases table with it. Needs a disposable Postgres (`DATABASE_URL`).
"""
from __future__ import annotations

import pytest

from db.database import IS_POSTGRES

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="requires disposable Postgres test database")

AUDIT_TABLES = {
    253: "reap_checkout_manual_resolution_audit",
    255: "reap_unopened_attempt_retirements",
    257: "reap_checkout_dispatch_resolution_audit",
}


@pytest.fixture
async def heal(monkeypatch):
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    if not database.is_connected:
        await database.connect()
    real = database.execute

    async def run(*failing: str, drop=()):
        for table in drop:
            await real(f"DROP TABLE IF EXISTS {table}")

        async def execute(query, *args, **kwargs):
            if any(marker in str(query) for marker in failing):
                raise RuntimeError("synthetic DDL failure")
            return await real(query, *args, **kwargs)

        monkeypatch.setattr(database, "execute", execute)
        try:
            await ensure_required_schema_light()
        finally:
            monkeypatch.setattr(database, "execute", real)
        rows = await database.fetch_all(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        return {r["tablename"] for r in rows}

    yield run
    await ensure_required_schema_light()
    await database.disconnect()


async def test_a_failing_enrollment_index_no_longer_skips_the_audit_tables(heal):
    tables = await heal("uq_reap_agentic_enrollments_one_active", drop=AUDIT_TABLES.values())
    assert set(AUDIT_TABLES.values()) <= tables


@pytest.mark.parametrize("number", [253, 255])
async def test_a_failing_audit_table_no_longer_skips_the_purchases_table(heal, number):
    tables = await heal(f"CREATE TABLE IF NOT EXISTS {AUDIT_TABLES[number]}",
                        drop=["reap_agentic_purchases", AUDIT_TABLES[number]])
    assert "reap_agentic_purchases" in tables and AUDIT_TABLES[number] not in tables
    assert set(AUDIT_TABLES.values()) - {AUDIT_TABLES[number]} <= tables


async def test_a_failing_continuation_heal_no_longer_skips_the_257_table(heal):
    tables = await heal("reap_dispatch_events_immutable", drop=["reap_checkout_dispatch_resolution_audit"])
    assert "reap_checkout_dispatch_resolution_audit" in tables
