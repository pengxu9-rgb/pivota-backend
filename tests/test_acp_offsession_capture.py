"""P-T2.3.3 — isolated off-session Stripe capture (mocked Stripe).

NOTE: verifies the helper's logic/normalization only. It does NOT prove behavior
against real Stripe — that requires the deployed test-mode canary.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")

from services import acp_offsession_capture as cap  # noqa: E402


class _FakeIntents:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def create(self, payload, opts=None):
        self.calls.append({"payload": payload, "opts": opts})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _FakeStripeClient:
    last_api_key = None
    intents = None

    def __init__(self, api_key, *a, **k):
        _FakeStripeClient.last_api_key = api_key
        self.v1 = SimpleNamespace(payment_intents=_FakeStripeClient.intents)


def _install_stripe(monkeypatch, outcome):
    import stripe

    _FakeStripeClient.intents = _FakeIntents(outcome)
    _FakeStripeClient.last_api_key = None
    monkeypatch.setattr(stripe, "StripeClient", _FakeStripeClient)
    return _FakeStripeClient.intents


def _install_merchant_key(monkeypatch, api_key="sk_test_merch"):
    async def fake_row(*, merchant_id, provider):
        assert provider == "stripe"
        return {"api_key": api_key}

    monkeypatch.setattr(cap, "fetch_active_runtime_merchant_psp", fake_row)


@pytest.mark.asyncio
async def test_successful_capture(monkeypatch):
    intents = _install_stripe(monkeypatch, SimpleNamespace(id="pi_1", status="succeeded", amount=100, currency="usd"))
    _install_merchant_key(monkeypatch, "sk_test_merch")

    r = await cap.capture_offsession(
        merchant_id="merch_efbc", amount_cents=100, currency="USD",
        idempotency_key="idem_1", metadata={"order_id": "o1"},
    )
    assert r.success is True
    assert r.status == "succeeded"
    assert r.payment_intent_id == "pi_1"
    # charged the MERCHANT's key, off_session + confirm, idempotency + test PM.
    assert _FakeStripeClient.last_api_key == "sk_test_merch"
    p = intents.calls[0]["payload"]
    assert p["off_session"] is True and p["confirm"] is True
    assert p["payment_method"] == "pm_card_visa"
    assert p["amount"] == 100 and p["currency"] == "usd"
    assert intents.calls[0]["opts"]["idempotency_key"] == "idem_1"
    assert p["metadata"]["pivota_acp_test_capture"] == "true"


@pytest.mark.asyncio
async def test_uses_provided_payment_method(monkeypatch):
    intents = _install_stripe(monkeypatch, SimpleNamespace(id="pi_2", status="succeeded", amount=50, currency="usd"))
    _install_merchant_key(monkeypatch)
    await cap.capture_offsession(
        merchant_id="m", amount_cents=50, currency="usd", idempotency_key="k",
        payment_method="pm_card_mastercard",
    )
    assert intents.calls[0]["payload"]["payment_method"] == "pm_card_mastercard"


@pytest.mark.asyncio
async def test_requires_action_is_failure(monkeypatch):
    _install_stripe(monkeypatch, SimpleNamespace(id="pi_3", status="requires_action", amount=100, currency="usd"))
    _install_merchant_key(monkeypatch)
    r = await cap.capture_offsession(merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k")
    assert r.success is False
    assert r.error_code == "requires_action"


@pytest.mark.asyncio
async def test_card_declined_is_normalized(monkeypatch):
    exc = Exception("card declined")
    exc.code = "card_declined"
    _install_stripe(monkeypatch, exc)
    _install_merchant_key(monkeypatch)
    r = await cap.capture_offsession(merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k")
    assert r.success is False
    assert r.error_code == "card_declined"


@pytest.mark.asyncio
async def test_unresolved_merchant_key(monkeypatch):
    async def no_row(*, merchant_id, provider):
        return None

    monkeypatch.setattr(cap, "fetch_active_runtime_merchant_psp", no_row)
    r = await cap.capture_offsession(merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k")
    assert r.success is False
    assert r.error_code == "no_merchant_psp"


@pytest.mark.asyncio
async def test_live_key_is_refused(monkeypatch):
    # A live key must never be charged from the test-only lane.
    _install_merchant_key(monkeypatch, "sk_live_danger")
    r = await cap.capture_offsession(merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k")
    assert r.success is False
    assert r.error_code == "live_key_refused"


@pytest.mark.asyncio
async def test_over_cap_refused(monkeypatch):
    _install_merchant_key(monkeypatch)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=100_000, currency="usd", idempotency_key="k", max_cents=500,
    )
    assert r.success is False
    assert r.error_code == "over_cap"


@pytest.mark.asyncio
async def test_non_positive_amount_refused(monkeypatch):
    _install_merchant_key(monkeypatch)
    r = await cap.capture_offsession(merchant_id="m", amount_cents=0, currency="usd", idempotency_key="k")
    assert r.success is False
    assert r.error_code == "invalid_amount"


# --- P-T2.3.5: LIVE-money lane (allow_live=True) ---
@pytest.mark.asyncio
async def test_live_lane_permits_live_key_with_real_pm(monkeypatch):
    intents = _install_stripe(monkeypatch, SimpleNamespace(id="pi_live", status="succeeded", amount=150, currency="usd"))
    _install_merchant_key(monkeypatch, "sk_live_merch")
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=150, currency="usd", idempotency_key="k",
        payment_method="pm_real_buyer", allow_live=True, max_cents=200,
    )
    assert r.success is True
    assert _FakeStripeClient.last_api_key == "sk_live_merch"
    p = intents.calls[0]["payload"]
    assert p["payment_method"] == "pm_real_buyer"
    assert p["metadata"]["pivota_acp_live_capture"] == "true"
    assert "pivota_acp_test_capture" not in p["metadata"]


@pytest.mark.asyncio
async def test_live_lane_refuses_missing_or_test_pm(monkeypatch):
    _install_stripe(monkeypatch, SimpleNamespace(id="x", status="succeeded", amount=1, currency="usd"))
    _install_merchant_key(monkeypatch, "sk_live_merch")
    for pm in (None, "pm_card_visa"):
        r = await cap.capture_offsession(
            merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k",
            payment_method=pm, allow_live=True, max_cents=200,
        )
        assert r.success is False and r.error_code == "live_pm_required"


@pytest.mark.asyncio
async def test_live_lane_refuses_test_merchant_key(monkeypatch):
    _install_stripe(monkeypatch, SimpleNamespace(id="x", status="succeeded", amount=1, currency="usd"))
    _install_merchant_key(monkeypatch, "sk_test_merch")
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k",
        payment_method="pm_real", allow_live=True, max_cents=200,
    )
    assert r.success is False and r.error_code == "live_key_required"


@pytest.mark.asyncio
async def test_test_lane_still_hard_refuses_live_key(monkeypatch):
    # Regression: the default (test) lane must still refuse a live merchant key.
    _install_stripe(monkeypatch, SimpleNamespace(id="x", status="succeeded", amount=1, currency="usd"))
    _install_merchant_key(monkeypatch, "sk_live_merch")
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k",
    )
    assert r.success is False and r.error_code == "live_key_refused"


@pytest.mark.asyncio
async def test_test_lane_ignores_non_pm_token_forwarded_by_surface(monkeypatch):
    # P-T2.3.5 pairing: the connector now forwards payment_data.token; a placeholder
    # like "tok_test" must NOT become the PM on the test lane — fall back to the
    # test PM so the proven test canary keeps working.
    intents = _install_stripe(monkeypatch, SimpleNamespace(id="pi_t", status="succeeded", amount=169, currency="usd"))
    _install_merchant_key(monkeypatch, "sk_test_merch")
    await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method="tok_test",
    )
    assert intents.calls[0]["payload"]["payment_method"] == "pm_card_visa"


@pytest.mark.asyncio
async def test_test_lane_still_honors_real_pm_token(monkeypatch):
    intents = _install_stripe(monkeypatch, SimpleNamespace(id="pi_u", status="succeeded", amount=50, currency="usd"))
    _install_merchant_key(monkeypatch, "sk_test_merch")
    await cap.capture_offsession(
        merchant_id="m", amount_cents=50, currency="usd", idempotency_key="k",
        payment_method="pm_card_mastercard",
    )
    assert intents.calls[0]["payload"]["payment_method"] == "pm_card_mastercard"
