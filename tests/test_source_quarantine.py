from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.source_quarantine import (
    Quarantine,
    build_quarantine_anti_join_sql,
    create_quarantine,
    is_source_quarantined,
    quarantine_matches_source,
    revoke_quarantine,
)


class FakeDb:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.inserted = []
        self.revoked = []

    async def fetch_all(self, _query, _values=None):
        return list(self.rows)

    async def fetch_one(self, query, values=None):
        values = values or {}
        if "INSERT INTO catalog_source_quarantine" in query:
            row = {
                "quarantine_id": 99,
                "match_type": values["match_type"],
                "match_value": values["match_value"],
                "state": "active",
                "reason": values.get("reason"),
                "expires_at": values.get("expires_at"),
                "created_by": values["created_by"],
                "created_at": datetime(2026, 5, 25, tzinfo=timezone.utc),
                "revoked_at": None,
                "revoked_by": None,
                "metadata": values.get("metadata"),
            }
            self.inserted.append(row)
            return row
        if "UPDATE catalog_source_quarantine" in query:
            row = {
                "quarantine_id": values["quarantine_id"],
                "match_type": "domain",
                "match_value": "example.com",
                "state": "revoked",
                "reason": "test",
                "expires_at": None,
                "created_by": "codex",
                "created_at": datetime(2026, 5, 25, tzinfo=timezone.utc),
                "revoked_at": datetime(2026, 5, 25, tzinfo=timezone.utc),
                "revoked_by": values["revoked_by"],
                "metadata": None,
            }
            self.revoked.append(row)
            return row
        return None


def q(match_type, match_value, *, state="active", expires_at=None):
    return Quarantine(
        quarantine_id=1,
        match_type=match_type,
        match_value=match_value,
        state=state,
        reason=None,
        expires_at=expires_at,
        created_by="test",
        created_at=None,
        revoked_at=None,
        revoked_by=None,
        metadata=None,
    )


def test_build_quarantine_anti_join_sql_contains_all_match_types():
    sql = build_quarantine_anti_join_sql(
        "p.source_domain",
        "p.merchant_id",
        "p.platform",
        "p.source_system",
        "p.source_ref",
    )

    assert "catalog_source_quarantine q" in sql
    assert "q.state = 'active'" in sql
    # CURRENT_TIMESTAMP, not now(): identical on Postgres, but `now()` does not
    # exist on SQLite — and this fragment is now embedded in
    # external_seed_search, whose suite runs on SQLite. See
    # test_anti_join_executes_on_sqlite below, which is the assertion that
    # actually protects this; a string match alone would not have caught it.
    assert "q.expires_at IS NULL OR q.expires_at > CURRENT_TIMESTAMP" in sql
    assert "now()" not in sql
    assert "q.match_type = 'domain'" in sql
    assert "lower(q.match_value) = lower(p.source_domain)" in sql
    assert "q.match_type = 'merchant_platform'" in sql
    assert "q.match_type = 'source_system_ref'" in sql


def test_anti_join_executes_on_sqlite():
    """The fragment must RUN on the engine the suite uses, not merely read well.

    Every other assertion in this file is a substring match, which is exactly
    the kind of test that stays green while the SQL is unrunnable. `now()`
    passed all of them and failed with `no such function: now` the first time
    anything executed it.
    """
    import sqlite3

    sql = build_quarantine_anti_join_sql(
        "s.source_domain", "s.merchant_id", "s.platform", "s.source_system", "s.source_ref"
    )
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE rows_under_test (id INTEGER, source_domain TEXT, merchant_id TEXT,"
        " platform TEXT, source_system TEXT, source_ref TEXT)"
    )
    conn.execute(
        "CREATE TABLE catalog_source_quarantine (quarantine_id INTEGER, match_type TEXT,"
        " match_value TEXT, state TEXT, expires_at TEXT)"
    )
    conn.execute(
        "INSERT INTO rows_under_test VALUES (1,'mintree.us',NULL,NULL,NULL,NULL),"
        " (2,'beplain.com',NULL,NULL,NULL,NULL)"
    )
    conn.execute(
        "INSERT INTO catalog_source_quarantine VALUES (1,'domain','mintree.us','active',NULL)"
    )
    got = [r[0] for r in conn.execute(f"SELECT id FROM rows_under_test s WHERE 1=1 {sql}")]
    assert got == [2], f"quarantined row survived, or the SQL did not run: {got}"


def test_domain_match_is_case_insensitive():
    assert quarantine_matches_source(
        q("domain", "JWX893-FZ.MyShopify.com"),
        domain="jwx893-fz.myshopify.com",
        merchant_id=None,
        platform=None,
        source_system=None,
        source_ref=None,
    )


def test_merchant_platform_match_is_exact():
    quarantine = q("merchant_platform", "merch_efbc46b4619cfbdf:shopify")

    assert quarantine_matches_source(
        quarantine,
        domain=None,
        merchant_id="merch_efbc46b4619cfbdf",
        platform="shopify",
        source_system=None,
        source_ref=None,
    )
    assert not quarantine_matches_source(
        quarantine,
        domain=None,
        merchant_id="MERCH_EFBC46B4619CFBDF",
        platform="shopify",
        source_system=None,
        source_ref=None,
    )


def test_source_system_ref_match_is_exact():
    assert quarantine_matches_source(
        q("source_system_ref", "shopify_products_sync:run_12345"),
        domain=None,
        merchant_id=None,
        platform=None,
        source_system="shopify_products_sync",
        source_ref="run_12345",
    )


def test_revoked_and_expired_do_not_match():
    now = datetime(2026, 5, 25, tzinfo=timezone.utc)

    assert not quarantine_matches_source(
        q("domain", "example.com", state="revoked"),
        domain="example.com",
        merchant_id=None,
        platform=None,
        source_system=None,
        source_ref=None,
        now=now,
    )
    assert not quarantine_matches_source(
        q("domain", "example.com", expires_at=now - timedelta(seconds=1)),
        domain="example.com",
        merchant_id=None,
        platform=None,
        source_system=None,
        source_ref=None,
        now=now,
    )


@pytest.mark.asyncio
async def test_is_source_quarantined_uses_active_rows():
    db = FakeDb(
        [
            {
                "quarantine_id": 1,
                "match_type": "domain",
                "match_value": "example.com",
                "state": "active",
                "reason": None,
                "expires_at": None,
                "created_by": "test",
                "created_at": None,
                "revoked_at": None,
                "revoked_by": None,
                "metadata": None,
            }
        ]
    )

    assert await is_source_quarantined(
        "EXAMPLE.com",
        None,
        None,
        None,
        None,
        db=db,
    )


@pytest.mark.asyncio
async def test_create_and_revoke_quarantine():
    db = FakeDb()

    created = await create_quarantine(
        match_type="domain",
        match_value="example.com",
        reason="test",
        created_by="codex",
        db=db,
    )
    revoked = await revoke_quarantine(quarantine_id=created.quarantine_id, revoked_by="codex", db=db)

    assert created.quarantine_id == 99
    assert db.inserted[0]["match_value"] == "example.com"
    assert revoked.state == "revoked"
    assert db.revoked[0]["revoked_by"] == "codex"
