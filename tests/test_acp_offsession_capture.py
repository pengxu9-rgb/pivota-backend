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


class _FakePaymentMethods:
    def retrieve(self, pm_id):
        if _FakeStripeClient.pm_lookup_raises:
            raise RuntimeError("pm lookup boom")
        return SimpleNamespace(id=pm_id, customer=_FakeStripeClient.pm_customer)


class _FakeStripeClient:
    last_api_key = None
    intents = None
    pm_customer = None          # what payment_methods.retrieve reports as .customer
    pm_lookup_raises = False

    def __init__(self, api_key, *a, **k):
        _FakeStripeClient.last_api_key = api_key
        self.v1 = SimpleNamespace(
            payment_intents=_FakeStripeClient.intents,
            payment_methods=_FakePaymentMethods(),
        )


def _install_stripe(monkeypatch, outcome):
    import stripe

    _FakeStripeClient.intents = _FakeIntents(outcome)
    _FakeStripeClient.last_api_key = None
    _FakeStripeClient.pm_customer = None
    _FakeStripeClient.pm_lookup_raises = False
    monkeypatch.setattr(stripe, "StripeClient", _FakeStripeClient)
    return _FakeStripeClient.intents


def _install_merchant_key(monkeypatch, api_key="sk_test_merch", psp_provider="stripe", environment=None, provider_config=None):
    async def fake_row(*, merchant_id, provider=None, psp_id=None, database_override=None):
        # P-T2.3.7: capture resolves the active PSP with NO provider hint and
        # dispatches by the row's `provider`.
        return {
            "api_key": api_key, "provider": psp_provider, "environment": environment,
            "provider_config": provider_config,
        }

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
    async def no_row(*, merchant_id, provider=None, psp_id=None, database_override=None):
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


# --- #1303: customer + mandate for off-session SCA cards ---
@pytest.mark.asyncio
async def test_pm_customer_is_passed_to_payment_intent(monkeypatch):
    # A pm attached to a customer (via SetupIntent{customer, off_session}) must
    # ride into the PaymentIntent so Stripe applies the mandate (SCA card succeeds).
    intents = _install_stripe(monkeypatch, SimpleNamespace(id="pi_c", status="succeeded", amount=169, currency="usd"))
    _install_merchant_key(monkeypatch, "sk_live_merch")
    _FakeStripeClient.pm_customer = "cus_ABC"
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method="pm_real", allow_live=True, max_cents=500,
    )
    assert r.success is True
    assert intents.calls[0]["payload"]["customer"] == "cus_ABC"


@pytest.mark.asyncio
async def test_no_customer_key_when_pm_unattached(monkeypatch):
    # A bare pm with no customer (US/non-SCA card, or test PM) → no customer key.
    intents = _install_stripe(monkeypatch, SimpleNamespace(id="pi_n", status="succeeded", amount=100, currency="usd"))
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _FakeStripeClient.pm_customer = None
    await cap.capture_offsession(
        merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k",
    )
    assert "customer" not in intents.calls[0]["payload"]


# --- P-T2.3.7: PSP-agnostic dispatch ---
@pytest.mark.asyncio
async def test_unsupported_provider_fails_closed(monkeypatch):
    # A resolved key on a PSP with no capture adapter (e.g. Mollie — not yet built)
    # must fail closed — never charge, never fall back to Stripe.
    _install_merchant_key(monkeypatch, api_key="test_MOLLIE_KEY", psp_provider="mollie")
    r = await cap.capture_offsession(merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k")
    assert r.success is False
    assert r.error_code == "unsupported_provider"


@pytest.mark.asyncio
async def test_missing_provider_fails_closed(monkeypatch):
    # A row with no provider slug can't be dispatched → fail closed.
    _install_merchant_key(monkeypatch, api_key="sk_test_merch", psp_provider="")
    r = await cap.capture_offsession(merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k")
    assert r.success is False
    assert r.error_code == "unsupported_provider"


@pytest.mark.asyncio
async def test_live_key_guard_generalizes_to_non_stripe(monkeypatch):
    # The test-lane live-key refusal must hold for ANY provider, not just Stripe:
    # an Adyen `live_` key on the test lane is refused BEFORE any adapter runs
    # (so the missing Adyen adapter is never even reached).
    _install_merchant_key(monkeypatch, api_key="live_ADYEN_KEY", psp_provider="adyen")
    r = await cap.capture_offsession(merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k")
    assert r.success is False
    assert r.error_code == "live_key_refused"


@pytest.mark.asyncio
async def test_pm_lookup_failure_still_charges_without_customer(monkeypatch):
    # A payment_method.retrieve failure must not block the charge (best-effort).
    intents = _install_stripe(monkeypatch, SimpleNamespace(id="pi_f", status="succeeded", amount=100, currency="usd"))
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _FakeStripeClient.pm_lookup_raises = True
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=100, currency="usd", idempotency_key="k",
    )
    assert r.success is True
    assert "customer" not in intents.calls[0]["payload"]


# --- P-T2.3.7: AdyenCaptureAdapter (MIT /payments, mocked httpx) ---

import json as _json  # noqa: E402


class _FakeAdyenResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload


class _FakeAdyenClient:
    captured = None
    response = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAdyenClient.captured = {"url": url, "headers": headers or {}, "json": json or {}}
        return _FakeAdyenClient.response


def _install_adyen_http(monkeypatch, *, status_code=200, payload):
    import httpx

    _FakeAdyenClient.captured = None
    _FakeAdyenClient.response = _FakeAdyenResp(status_code, payload)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAdyenClient)


_ADYEN_CFG = {"merchant_account": "PivotaTestECOM"}
_ADYEN_MD = {"adyen_shopper_reference": "shopper_efbc", "order_id": "ORD_WIX_1"}


@pytest.mark.asyncio
async def test_adyen_authorised_success(monkeypatch):
    _install_merchant_key(monkeypatch, api_key="test_ADYEN", psp_provider="adyen", provider_config=_ADYEN_CFG)
    _install_adyen_http(monkeypatch, payload={"resultCode": "Authorised", "pspReference": "ADY_PSP_1"})
    r = await cap.capture_offsession(
        merchant_id="merch_efbc", amount_cents=169, currency="USD", idempotency_key="idem_a",
        payment_method="8416891234567890", metadata=dict(_ADYEN_MD), max_cents=5000,
    )
    assert r.success is True and r.status == "succeeded"
    assert r.payment_intent_id == "ADY_PSP_1"
    body = _FakeAdyenClient.captured["json"]
    assert _FakeAdyenClient.captured["url"] == "https://checkout-test.adyen.com/v71/payments"
    assert body["merchantAccount"] == "PivotaTestECOM"
    assert body["shopperInteraction"] == "ContAuth"
    assert body["recurringProcessingModel"] == "CardOnFile"
    assert body["paymentMethod"] == {"type": "scheme", "storedPaymentMethodId": "8416891234567890"}
    assert body["shopperReference"] == "shopper_efbc"
    assert body["amount"] == {"value": 169, "currency": "USD"}
    assert body["reference"] == "ORD_WIX_1"
    assert _FakeAdyenClient.captured["headers"]["Idempotency-Key"] == "idem_a"
    assert _FakeAdyenClient.captured["headers"]["X-API-Key"] == "test_ADYEN"
    assert body["metadata"]["pivota_acp_test_capture"] == "true"


@pytest.mark.asyncio
async def test_adyen_refused_fails(monkeypatch):
    _install_merchant_key(monkeypatch, api_key="test_ADYEN", psp_provider="adyen", provider_config=_ADYEN_CFG)
    _install_adyen_http(monkeypatch, payload={"resultCode": "Refused", "refusalReason": "Insufficient funds", "pspReference": "ADY_PSP_2"})
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="USD", idempotency_key="k",
        payment_method="8416891234567890", metadata=dict(_ADYEN_MD), max_cents=5000,
    )
    assert r.success is False and r.error_code == "adyen_refused"
    assert "Insufficient funds" in r.error


@pytest.mark.asyncio
async def test_adyen_sca_is_requires_action(monkeypatch):
    _install_merchant_key(monkeypatch, api_key="test_ADYEN", psp_provider="adyen", provider_config=_ADYEN_CFG)
    _install_adyen_http(monkeypatch, payload={"resultCode": "RedirectShopper", "pspReference": "ADY_PSP_3"})
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="USD", idempotency_key="k",
        payment_method="8416891234567890", metadata=dict(_ADYEN_MD), max_cents=5000,
    )
    assert r.success is False and r.error_code == "requires_action"


@pytest.mark.asyncio
async def test_adyen_missing_merchant_account(monkeypatch):
    _install_merchant_key(monkeypatch, api_key="test_ADYEN", psp_provider="adyen", provider_config={})
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="USD", idempotency_key="k",
        payment_method="8416891234567890", metadata=dict(_ADYEN_MD), max_cents=5000,
    )
    assert r.success is False and r.error_code == "adyen_config"


@pytest.mark.asyncio
async def test_adyen_requires_shopper_reference(monkeypatch):
    _install_merchant_key(monkeypatch, api_key="test_ADYEN", psp_provider="adyen", provider_config=_ADYEN_CFG)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="USD", idempotency_key="k",
        payment_method="8416891234567890", metadata={"order_id": "ORD_1"}, max_cents=5000,
    )
    assert r.success is False and r.error_code == "adyen_shopper_ref_required"


@pytest.mark.asyncio
async def test_adyen_rejects_stripe_style_token(monkeypatch):
    # A Stripe-style pm_/tok_/vt_ token is not an Adyen storedPaymentMethodId.
    _install_merchant_key(monkeypatch, api_key="test_ADYEN", psp_provider="adyen", provider_config=_ADYEN_CFG)
    for tok in ("pm_card_visa", "tok_test", "vt_abc", ""):
        r = await cap.capture_offsession(
            merchant_id="m", amount_cents=169, currency="USD", idempotency_key="k",
            payment_method=tok, metadata=dict(_ADYEN_MD), max_cents=5000,
        )
        assert r.success is False and r.error_code == "adyen_pm_required"


@pytest.mark.asyncio
async def test_adyen_live_requires_url_prefix(monkeypatch):
    # Live lane with a live key but no live_url_prefix in provider_config → config fail.
    _install_merchant_key(monkeypatch, api_key="live_ADYEN", psp_provider="adyen", provider_config=_ADYEN_CFG)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="USD", idempotency_key="k",
        payment_method="8416891234567890", metadata=dict(_ADYEN_MD),
        allow_live=True, max_cents=5000,
    )
    assert r.success is False and r.error_code == "adyen_config"


@pytest.mark.asyncio
async def test_adyen_live_uses_prefixed_endpoint(monkeypatch):
    _install_merchant_key(
        monkeypatch, api_key="live_ADYEN", psp_provider="adyen",
        provider_config={"merchant_account": "PivotaLiveECOM", "live_url_prefix": "1797a-Pivota"},
    )
    _install_adyen_http(monkeypatch, payload={"resultCode": "Authorised", "pspReference": "ADY_LIVE_1"})
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="USD", idempotency_key="k",
        payment_method="8416891234567890", metadata=dict(_ADYEN_MD),
        allow_live=True, max_cents=5000,
    )
    assert r.success is True
    assert _FakeAdyenClient.captured["url"] == "https://1797a-Pivota-checkout-live.adyenpayments.com/checkout/v71/payments"
    assert _FakeAdyenClient.captured["json"]["metadata"]["pivota_acp_live_capture"] == "true"


@pytest.mark.asyncio
async def test_adyen_http_error_fails_closed(monkeypatch):
    _install_merchant_key(monkeypatch, api_key="test_ADYEN", psp_provider="adyen", provider_config=_ADYEN_CFG)
    _install_adyen_http(monkeypatch, status_code=401, payload={"message": "Unauthorized"})
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="USD", idempotency_key="k",
        payment_method="8416891234567890", metadata=dict(_ADYEN_MD), max_cents=5000,
    )
    assert r.success is False and r.error_code == "adyen_http_401"
