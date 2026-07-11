"""Regression: self-provision the `merchants` billing row at checkout time.

Portal signup + approval writes only `merchant_onboarding`; nothing promotes an
approved merchant into the `merchants` monetization table. The checkout flow
`UPDATE`s that row, so a first-time subscriber used to 500 with
"Merchant subscription provisioning incomplete: ... local merchants row missing"
and orphan a Stripe customer.

`_ensure_merchant_billing_row` closes that gap: `create_billing_checkout_session`
provisions the row (idempotently, from the approved onboarding record) before
creating the Stripe customer. These tests pin that behaviour without a live DB.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest


class _FakeDB:
    def __init__(self) -> None:
        self.executed: List[Dict[str, Any]] = []

    async def execute(self, query: str, values: Optional[dict] = None) -> None:
        self.executed.append({"query": query, "values": dict(values or {})})
        return None


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
    """The regression: no merchants row must no longer 500 the checkout."""
    from routes import billing_routes

    recorder = _RecordingStripeClient()
    monkeypatch.setattr(billing_routes, "stripe_client", recorder)

    ensure_calls: List[Dict[str, Any]] = []
    fetch_calls = {"n": 0}

    def fake_require_platform_key():
        return None

    async def fake_billing_free(_merchant_id):
        return False

    async def fake_lookup_plan(*_a, **_k):
        return {"id": 1}

    async def fake_fetch_billing_row(*_a, **_k):
        # First read (before provisioning) → missing; after provisioning → present.
        fetch_calls["n"] += 1
        if fetch_calls["n"] == 1:
            return None
        return {"contact_email": "soojung@my-yak.com", "stripe_customer_id": None}

    async def fake_ensure(_db, *, merchant_id, contact_email):
        ensure_calls.append({"merchant_id": merchant_id, "contact_email": contact_email})

    async def fake_update_customer_id(*_a, **_k):
        return True  # the row exists now, so the UPDATE matches

    monkeypatch.setattr(billing_routes, "_require_platform_stripe_key", fake_require_platform_key)
    monkeypatch.setattr(billing_routes, "_merchant_is_billing_free", fake_billing_free)
    monkeypatch.setattr(billing_routes, "_lookup_subscription_plan", fake_lookup_plan)
    monkeypatch.setattr(billing_routes, "_fetch_merchant_billing_row", fake_fetch_billing_row)
    monkeypatch.setattr(billing_routes, "_ensure_merchant_billing_row", fake_ensure)
    monkeypatch.setattr(billing_routes, "_update_merchant_stripe_customer_id", fake_update_customer_id)

    body = billing_routes.CheckoutSessionRequest(
        price_id="price_test",
        success_url="https://x.test/ok",
        cancel_url="https://x.test/no",
    )
    merchant = {"merchant_id": "merch_680f9f566e9e70bb", "contact_email": "soojung@my-yak.com"}

    result = await billing_routes.create_billing_checkout_session(body, merchant=merchant)

    # Provisioning ran once for the missing row, and checkout completed.
    assert ensure_calls == [
        {"merchant_id": "merch_680f9f566e9e70bb", "contact_email": "soojung@my-yak.com"}
    ]
    assert len(recorder.customers_calls) == 1  # customer created after provisioning
    assert len(recorder.sessions_calls) == 1
    assert result["session_url"] == "https://stripe.test/checkout"
