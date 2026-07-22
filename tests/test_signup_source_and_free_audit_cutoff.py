"""
Audit-growth-funnel WS-1 backend surface:

  (a) `signup_source` acquisition attribution — accepted on
      /merchant/onboarding/register, sanitized to a lowercase slug, persisted
      on the merchant_onboarding row, and never able to block a registration
      (junk input coerces to None rather than 422).
  (b) FREE_AUDIT_COUNT_SINCE cutoff (founder decision D3, 2026-07-22) — the
      free URL-audit allowance counter only counts runs requested at/after
      the cutoff, so flipping FREE_URL_AUDITS_PER_MERCHANT on does not
      retroactively consume the allowance with runs from the unmetered era.

Register-path style mirrors tests/test_merchant_declared_mode.py (route
coroutine called directly with monkeypatched DB + helpers); the counter tests
reuse the sqlite fixture shape from tests/test_billing_charged_iff_delivered.py.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

import routes.merchant_onboarding_routes as module
from routes.merchant_onboarding_routes import MerchantRegisterRequest


def _patch_common(monkeypatch, captured):
    """Stub the shared onboarding side-effects so register_merchant runs DB-free."""

    async def fake_execute(*args, **kwargs):
        return None

    async def fake_fetch_one(*args, **kwargs):
        return None

    async def fake_get_active_onboarding_by_email(email):
        return None

    async def fake_get_user_auth_binding(email):
        return None

    async def fake_resolve_identity_merge(**kwargs):
        return None, None

    async def fake_get_merchant_onboarding(merchant_id):
        return None

    async def fake_create(merchant_dict):
        captured["create_dict"] = dict(merchant_dict)
        return "merch_test_attribution"

    async def fake_update_kyc_status(merchant_id, status, *a, **k):
        return True

    async def fake_sync_user(**kwargs):
        return None

    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module, "get_active_onboarding_by_email", fake_get_active_onboarding_by_email)
    monkeypatch.setattr(module, "get_user_auth_binding", fake_get_user_auth_binding)
    monkeypatch.setattr(module, "resolve_public_merchant_identity_merge", fake_resolve_identity_merge)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "create_merchant_onboarding", fake_create)
    monkeypatch.setattr(module, "update_kyc_status", fake_update_kyc_status)
    monkeypatch.setattr(module, "sync_merchant_auth_user", fake_sync_user)


class _DummyBackgroundTasks:
    def add_task(self, *args, **kwargs):
        pass


# ---- (a) signup_source: accepted, sanitized, persisted ----------------------

@pytest.mark.asyncio
async def test_register_persists_signup_source(monkeypatch):
    captured = {}
    _patch_common(monkeypatch, captured)

    req = MerchantRegisterRequest(
        business_name="Funnel Brand Co",
        region="US",
        contact_email="funnel@example.com",
        password="hunter2hunter2",
        operating_mode="store_less",
        signup_source="ai-readiness-audit",
    )

    resp = await module.register_merchant(req, _DummyBackgroundTasks())

    assert resp["status"] == "success"
    assert captured["create_dict"]["signup_source"] == "ai-readiness-audit"


@pytest.mark.asyncio
async def test_register_without_signup_source_persists_none(monkeypatch):
    captured = {}
    _patch_common(monkeypatch, captured)

    req = MerchantRegisterRequest(
        business_name="No Source Co",
        region="US",
        contact_email="nosource@example.com",
        password="hunter2hunter2",
        operating_mode="store_less",
    )

    await module.register_merchant(req, _DummyBackgroundTasks())

    assert captured["create_dict"]["signup_source"] is None


def test_signup_source_sanitization():
    def source_of(value):
        return MerchantRegisterRequest(
            business_name="X",
            region="US",
            contact_email="x@example.com",
            operating_mode="store_less",
            signup_source=value,
        ).signup_source

    # Case/whitespace normalize to the slug form.
    assert source_of("  AI-Readiness-Audit ") == "ai-readiness-audit"
    # Junk must coerce to None, never 422 — attribution can't block signup.
    assert source_of("<script>alert(1)</script>") is None
    assert source_of("spaces in source") is None
    assert source_of("") is None
    assert source_of(None) is None
    assert source_of(123) is None
    # Over-length input truncates to the 64-char column cap.
    assert source_of("a" * 80) == "a" * 64


# ---- (b) FREE_AUDIT_COUNT_SINCE cutoff --------------------------------------

@pytest.fixture
async def sqlite_db(monkeypatch):
    from databases import Database
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = Database(f"sqlite:///{tmp.name}")
    await db.connect()
    await db.execute("""
        CREATE TABLE merchant_audit_runs (
          run_id TEXT PRIMARY KEY,
          merchant_id TEXT NOT NULL,
          subject_type TEXT,
          requested_at TIMESTAMP NOT NULL,
          status TEXT NOT NULL,
          stage TEXT
        )
    """)
    monkeypatch.setattr("db.merchant_audit_runs.database", db)
    import db.merchant_audit_runs as mar
    mar._DDL_READY = True
    yield db
    await db.disconnect()
    mar._DDL_READY = False
    os.unlink(tmp.name)


async def _seed_run(db, run_id, requested_at):
    await db.execute(
        "INSERT INTO merchant_audit_runs "
        "(run_id, merchant_id, subject_type, requested_at, status, stage) "
        "VALUES (:r, 'm1', 'merchant_url', :t, 'succeeded', 'completed')",
        {"r": run_id, "t": requested_at},
    )


@pytest.mark.asyncio
async def test_count_with_since_excludes_pre_cutoff_runs(sqlite_db):
    from db.merchant_audit_runs import count_runs_for_merchant_by_subject

    cutoff = datetime(2026, 7, 22, tzinfo=timezone.utc)
    # SQLite stores naive timestamps; write naive UTC to match the comparison.
    await _seed_run(sqlite_db, "old1", cutoff.replace(tzinfo=None) - timedelta(days=30))
    await _seed_run(sqlite_db, "old2", cutoff.replace(tzinfo=None) - timedelta(seconds=1))
    await _seed_run(sqlite_db, "new1", cutoff.replace(tzinfo=None))
    await _seed_run(sqlite_db, "new2", cutoff.replace(tzinfo=None) + timedelta(days=1))

    used_all = await count_runs_for_merchant_by_subject(
        merchant_id="m1", subject_type="merchant_url",
    )
    assert used_all == 4

    # Pass the tz-aware shape prod actually uses (_FREE_AUDIT_COUNT_SINCE is
    # always tz-aware UTC); SQLAlchemy's SQLite processor strips tzinfo, so
    # this exercises the same wall-clock comparison the route performs.
    used_since = await count_runs_for_merchant_by_subject(
        merchant_id="m1", subject_type="merchant_url",
        since=cutoff,
    )
    assert used_since == 2


def test_parse_free_audit_count_since():
    import routes.merchant_audit_routes as mar_routes

    parse = mar_routes._parse_free_audit_count_since

    # Bare date -> midnight UTC, tz-aware.
    parsed = parse("2026-07-22")
    assert parsed == datetime(2026, 7, 22, tzinfo=timezone.utc)

    # Full datetime with offset preserved.
    parsed = parse("2026-07-22T09:30:00+00:00")
    assert parsed == datetime(2026, 7, 22, 9, 30, tzinfo=timezone.utc)

    # Naive datetimes are treated as UTC.
    parsed = parse("2026-07-22T09:30:00")
    assert parsed.tzinfo is not None

    # Unset / blank / garbage all disable the cutoff (count everything)
    # rather than raising at import time.
    assert parse(None) is None
    assert parse("") is None
    assert parse("   ") is None
    assert parse("not-a-date") is None
