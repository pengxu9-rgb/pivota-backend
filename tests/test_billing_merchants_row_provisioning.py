"""Regression + concurrency coverage for merchants-billing-row self-provisioning.

Portal signup + approval writes only `merchant_onboarding`; nothing promotes an
approved merchant into the `merchants` monetization table. The checkout flow
`UPDATE`s that row, so a first-time subscriber used to 500 with
"Merchant subscription provisioning incomplete: ... local merchants row missing"
and orphan a Stripe customer.

`_ensure_merchant_billing_row` closes that gap: `create_billing_checkout_session`
provisions the row (idempotently, from the approved onboarding record) before
creating the Stripe customer. Under a double-submit race the losing INSERT hits
the `uq_merchants_lower_contact_email` unique index (migration 180) and raises a
unique violation, which is swallowed. These tests pin that behaviour.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest


class _FakeDB:
    """Records executed statements; can be told to raise on execute."""

    def __init__(self, raise_on_execute: Optional[Exception] = None) -> None:
        self.executed: List[Dict[str, Any]] = []
        self._raise = raise_on_execute

    async def execute(self, query: str, values: Optional[dict] = None) -> None:
        self.executed.append({"query": query, "values": dict(values or {})})
        if self._raise is not None:
            raise self._raise
        return None


class _FakeUniqueViolation(Exception):
    """Mimics asyncpg's UniqueViolationError surface (SQLSTATE 23505)."""

    sqlstate = "23505"


def test_is_unique_violation_detection() -> None:
    from routes import billing_routes

    assert billing_routes._is_unique_violation(_FakeUniqueViolation()) is True
    # class-name fallback (driver error surfaced without a sqlstate attr)
    named = type("UniqueViolationError", (Exception,), {})()
    assert billing_routes._is_unique_violation(named) is True
    # unrelated errors must NOT be treated as unique violations
    assert billing_routes._is_unique_violation(ValueError("nope")) is False
    assert billing_routes._is_unique_violation(RuntimeError("conn lost")) is False


@pytest.mark.asyncio
async def test_ensure_merchant_billing_row_inserts_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routes import billing_routes

    async def fake_columns(_db, _table):
        return {"contact_email", "business_name", "legal_name", "platform",
                "store_url", "status", "verification_status", "current_tier"}

    async def fake_billing_row(*_a, **_k):
        return None  # no existing merchants row

    async def fake_onboarding(_merchant_id):
        return {"business_name": "My Yak", "store_url": None,
                "contact_email": "soojung@my-yak.com"}

    monkeypatch.setattr(billing_routes, "_table_columns", fake_columns)
    monkeypatch.setattr(billing_routes, "_fetch_merchant_billing_row", fake_billing_row)
    monkeypatch.setattr(billing_routes, "get_merchant_onboarding", fake_onboarding)

    db = _FakeDB()
    await billing_routes._ensure_merchant_billing_row(
        db, merchant_id="merch_680f9f566e9e70bb", contact_email="soojung@my-yak.com"
    )

    assert len(db.executed) == 1, "expected exactly one INSERT"
    call = db.executed[0]
    assert "INSERT INTO merchants" in call["query"]
    assert "WHERE NOT EXISTS" in call["query"], "insert must be idempotent under a race"
    assert call["values"]["contact_email"] == "soojung@my-yak.com"
    assert call["values"]["business_name"] == "My Yak"  # carried from onboarding


@pytest.mark.asyncio
async def test_ensure_merchant_billing_row_noop_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routes import billing_routes

    async def fake_columns(_db, _table):
        return {"contact_email"}

    async def fake_billing_row(*_a, **_k):
        return {"id": 21, "contact_email": "soojung@my-yak.com"}  # already exists

    monkeypatch.setattr(billing_routes, "_table_columns", fake_columns)
    monkeypatch.setattr(billing_routes, "_fetch_merchant_billing_row", fake_billing_row)

    db = _FakeDB()
    await billing_routes._ensure_merchant_billing_row(
        db, merchant_id="merch_1", contact_email="soojung@my-yak.com"
    )

    assert db.executed == [], "must not INSERT when a merchants row already exists"


@pytest.mark.asyncio
async def test_ensure_merchant_billing_row_swallows_concurrent_unique_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Double-submit race: our WHERE-NOT-EXISTS check passed, but a concurrent
    request inserted first, so the unique index makes our INSERT raise 23505.
    Provisioning must treat that as success (row exists), not propagate a 500."""
    from routes import billing_routes

    async def fake_columns(_db, _table):
        return {"contact_email", "business_name"}

    async def fake_billing_row(*_a, **_k):
        return None  # check passed; the concurrent txn then won the race

    async def fake_onboarding(_merchant_id):
        return {"business_name": "My Yak", "contact_email": "soojung@my-yak.com"}

    monkeypatch.setattr(billing_routes, "_table_columns", fake_columns)
    monkeypatch.setattr(billing_routes, "_fetch_merchant_billing_row", fake_billing_row)
    monkeypatch.setattr(billing_routes, "get_merchant_onboarding", fake_onboarding)

    db = _FakeDB(raise_on_execute=_FakeUniqueViolation())
    # Must NOT raise — the violation is swallowed.
    await billing_routes._ensure_merchant_billing_row(
        db, merchant_id="merch_1", contact_email="soojung@my-yak.com"
    )
    assert len(db.executed) == 1  # it attempted the insert, then swallowed 23505


@pytest.mark.asyncio
async def test_ensure_merchant_billing_row_reraises_non_unique_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only unique violations are swallowed; real DB errors must propagate."""
    from routes import billing_routes

    async def fake_columns(_db, _table):
        return {"contact_email"}

    async def fake_billing_row(*_a, **_k):
        return None

    async def fake_onboarding(_merchant_id):
        return {"business_name": "X", "contact_email": "e@x.com"}

    monkeypatch.setattr(billing_routes, "_table_columns", fake_columns)
    monkeypatch.setattr(billing_routes, "_fetch_merchant_billing_row", fake_billing_row)
    monkeypatch.setattr(billing_routes, "get_merchant_onboarding", fake_onboarding)

    db = _FakeDB(raise_on_execute=RuntimeError("connection lost"))
    with pytest.raises(RuntimeError):
        await billing_routes._ensure_merchant_billing_row(
            db, merchant_id="m", contact_email="e@x.com"
        )


class _RecordingResource:
    def __init__(self, recorder: List[Dict[str, Any]], make_id: str) -> None:
        self.recorder = recorder
        self.make_id = make_id

    def create(self, *args, **kwargs):
        self.recorder.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(id=self.make_id, url="https://stripe.test/checkout")


class _RecordingStripeClient:
    def __init__(self) -> None:
        self.customers_calls: List[Dict[str, Any]] = []
        self.sessions_calls: List[Dict[str, Any]] = []
        self.v1 = SimpleNamespace(
            customers=_RecordingResource(self.customers_calls, "cus_test"),
            checkout=SimpleNamespace(
                sessions=_RecordingResource(self.sessions_calls, "cs_test"),
            ),
        )


@pytest.mark.asyncio
async def test_checkout_provisions_missing_billing_row_instead_of_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a merchant with no `merchants` row must not 500 the checkout.

    This exercises the REAL `_ensure_merchant_billing_row` (it is intentionally
    NOT mocked) so the guarantee actually flows through provisioning: the fake
    database records the INSERT, and checkout completes.
    """
    from routes import billing_routes

    recorder = _RecordingStripeClient()
    fake_db = _FakeDB()
    monkeypatch.setattr(billing_routes, "stripe_client", recorder)
    monkeypatch.setattr(billing_routes, "database", fake_db)

    fetch_calls = {"n": 0}

    def fake_require_platform_key():
        return None

    async def fake_billing_free(_merchant_id):
        return False

    async def fake_lookup_plan(*_a, **_k):
        return {"id": 1}

    async def fake_columns(_db, _table):
        return {"contact_email", "business_name"}

    async def fake_fetch_billing_row(*_a, **_k):
        # 1: checkout pre-read → None; 2: _ensure existing-check → None;
        # 3: checkout re-read after provisioning → row (no Stripe customer yet).
        fetch_calls["n"] += 1
        if fetch_calls["n"] < 3:
            return None
        return {"contact_email": "soojung@my-yak.com", "stripe_customer_id": None}

    async def fake_onboarding(_merchant_id):
        return {"business_name": "My Yak", "contact_email": "soojung@my-yak.com"}

    async def fake_update_customer_id(*_a, **_k):
        return True  # the provisioned row exists now, so the UPDATE matches

    monkeypatch.setattr(billing_routes, "_require_platform_stripe_key", fake_require_platform_key)
    monkeypatch.setattr(billing_routes, "_merchant_is_billing_free", fake_billing_free)
    monkeypatch.setattr(billing_routes, "_lookup_subscription_plan", fake_lookup_plan)
    monkeypatch.setattr(billing_routes, "_table_columns", fake_columns)
    monkeypatch.setattr(billing_routes, "_fetch_merchant_billing_row", fake_fetch_billing_row)
    monkeypatch.setattr(billing_routes, "get_merchant_onboarding", fake_onboarding)
    monkeypatch.setattr(billing_routes, "_update_merchant_stripe_customer_id", fake_update_customer_id)
    # NOTE: _ensure_merchant_billing_row is intentionally NOT mocked — real provisioning runs.

    body = billing_routes.CheckoutSessionRequest(
        price_id="price_test",
        success_url="https://x.test/ok",
        cancel_url="https://x.test/no",
    )
    merchant = {"merchant_id": "merch_680f9f566e9e70bb", "contact_email": "soojung@my-yak.com"}

    result = await billing_routes.create_billing_checkout_session(body, merchant=merchant)

    # Real provisioning issued the INSERT, and checkout completed without a 500.
    assert any("INSERT INTO merchants" in e["query"] for e in fake_db.executed), \
        "real _ensure_merchant_billing_row should have issued the provisioning INSERT"
    assert len(recorder.customers_calls) == 1  # customer created after provisioning
    assert len(recorder.sessions_calls) == 1
    assert result["session_url"] == "https://stripe.test/checkout"
