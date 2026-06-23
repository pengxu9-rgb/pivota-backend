"""Multi-active-subscription handling: cancel-prior-on-checkout + the guard
that one subscription ending must not downgrade a merchant who still holds
another active plan.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from routes import billing_routes


class FakeDB:
    def __init__(self, *, remaining=None, prior_rows=None) -> None:
        self._remaining = remaining
        self._prior_rows = prior_rows or []
        self.executes: List[tuple[str, Dict[str, Any]]] = []

    async def fetch_one(self, query: str, values: Optional[Dict[str, Any]] = None):
        sql = str(query)
        if "IS DISTINCT FROM :ended" in sql:
            return self._remaining
        raise AssertionError(f"unexpected fetch_one: {sql}")

    async def fetch_all(self, query: str, values: Optional[Dict[str, Any]] = None):
        sql = str(query)
        if "IS DISTINCT FROM :keep" in sql:
            return self._prior_rows
        raise AssertionError(f"unexpected fetch_all: {sql}")

    async def execute(self, query: str, values: Optional[Dict[str, Any]] = None):
        self.executes.append((str(query), dict(values or {})))


@pytest.mark.asyncio
async def test_reconcile_keeps_merchant_on_remaining_active_plan(monkeypatch):
    # Cancelling one sub while a higher plan is still active must keep the
    # merchant on that plan — never drop to free.
    calls: Dict[str, Any] = {}

    async def fake_update_tier(db, *, merchant_id, contact_email, tier_name):
        calls["tier"] = tier_name

    async def fake_downgrade(db, *, merchant_id, contact_email):
        calls["downgraded"] = True

    monkeypatch.setattr(billing_routes, "_update_merchant_tier", fake_update_tier)
    monkeypatch.setattr(billing_routes, "_downgrade_merchant_to_free", fake_downgrade)
    import services.merchant_credit_balance_service as mcb

    async def fake_apply(merchant_id):
        calls["applied"] = merchant_id

    monkeypatch.setattr(mcb, "apply_subscription_allowance", fake_apply)

    db = FakeDB(remaining={"tier_name": "growth"})
    await billing_routes._reconcile_after_subscription_ended(
        db, merchant_id="merch-A", contact_email=None,
        ended_stripe_subscription_id="sub_old",
    )
    assert calls.get("tier") == "growth"
    assert calls.get("applied") == "merch-A"
    assert "downgraded" not in calls


@pytest.mark.asyncio
async def test_reconcile_downgrades_to_free_when_no_active_remains(monkeypatch):
    calls: Dict[str, Any] = {}

    async def fake_update_tier(db, *, merchant_id, contact_email, tier_name):
        calls["tier"] = tier_name

    async def fake_downgrade(db, *, merchant_id, contact_email):
        calls["downgraded"] = True

    monkeypatch.setattr(billing_routes, "_update_merchant_tier", fake_update_tier)
    monkeypatch.setattr(billing_routes, "_downgrade_merchant_to_free", fake_downgrade)

    db = FakeDB(remaining=None)
    await billing_routes._reconcile_after_subscription_ended(
        db, merchant_id="merch-A", contact_email=None,
        ended_stripe_subscription_id="sub_old",
    )
    assert calls.get("downgraded") is True
    assert "tier" not in calls


def _fake_stripe(monkeypatch, *, fail=False):
    cancelled: List[str] = []

    class FakeSubs:
        def cancel(self, sub_id):
            if fail:
                raise RuntimeError("stripe unavailable")
            cancelled.append(sub_id)
            return {"id": sub_id, "status": "canceled"}

    class FakeV1:
        subscriptions = FakeSubs()

    class FakeStripe:
        v1 = FakeV1()

    monkeypatch.setattr(billing_routes, "stripe_client", FakeStripe())
    return cancelled


@pytest.mark.asyncio
async def test_cancel_prior_subs_cancels_in_stripe_and_locally(monkeypatch):
    cancelled = _fake_stripe(monkeypatch)
    db = FakeDB(prior_rows=[
        {"stripe_subscription_id": "sub_old1"},
        {"stripe_subscription_id": "sub_old2"},
    ])
    await billing_routes._cancel_prior_active_subscriptions(
        db, merchant_id="merch-A", keep_stripe_subscription_id="sub_new",
    )
    assert cancelled == ["sub_old1", "sub_old2"]
    marked = [v["sid"] for (q, v) in db.executes if "UPDATE user_subscriptions" in q]
    assert marked == ["sub_old1", "sub_old2"]


@pytest.mark.asyncio
async def test_cancel_prior_subs_marks_local_even_if_stripe_fails(monkeypatch):
    _fake_stripe(monkeypatch, fail=True)
    db = FakeDB(prior_rows=[{"stripe_subscription_id": "sub_old1"}])
    await billing_routes._cancel_prior_active_subscriptions(
        db, merchant_id="merch-A", keep_stripe_subscription_id="sub_new",
    )
    marked = [v["sid"] for (q, v) in db.executes if "UPDATE user_subscriptions" in q]
    assert marked == ["sub_old1"]
