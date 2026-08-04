"""P-T2.3.3 — isolated off-session Stripe capture (mocked Stripe).

NOTE: verifies the helper's logic/normalization only. It does NOT prove behavior
against real Stripe — that requires the deployed test-mode canary.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
        _FakeStripeClient.pm_lookups.append(pm_id)
        if _FakeStripeClient.pm_lookup_raises:
            raise RuntimeError("pm lookup boom")
        return SimpleNamespace(id=pm_id, customer=_FakeStripeClient.pm_customer)


class _FakeStripeClient:
    last_api_key = None
    intents = None
    pm_customer = None          # what payment_methods.retrieve reports as .customer
    pm_lookup_raises = False
    pm_lookups = []             # every payment_methods.retrieve arg, in order

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
    _FakeStripeClient.pm_lookups = []
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
    # A Stripe-style pm_/tok_/vt_/spt_ token is not an Adyen storedPaymentMethodId.
    # `spt_` matters especially (review N3): forwarding it produced an
    # adyen_http_*/adyen_refused failure, both AMBIGUOUS, which wedges the
    # session in `completing` until TTL. Refusing PRE-DISPATCH releases a fresh
    # claim and holds a resumed one — correct on both.
    _install_merchant_key(monkeypatch, api_key="test_ADYEN", psp_provider="adyen", provider_config=_ADYEN_CFG)
    for tok in ("pm_card_visa", "tok_test", "vt_abc", "spt_abc123", ""):
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


# =============================================================================
# P1 PR-B — Stripe SharedPaymentToken (`spt_`) capture lane
# =============================================================================
# The flag gates ONE adapter branch. With it OFF an `spt_` token must behave
# exactly as any other non-`pm_` token behaves today; with it ON the intent
# carries `payment_method_data[shared_payment_granted_token]` under the preview
# API version and NOTHING else about the money path moves.

SPT = "spt_test_granted_token_123"


def _arm_spt(monkeypatch, enabled=True):
    from config.settings import settings

    monkeypatch.setattr(settings, "acp_spt_capture_enabled", enabled, raising=False)


def _ok(**kw):
    base = dict(id="pi_spt", status="succeeded", amount=169, currency="usd")
    base.update(kw)
    return SimpleNamespace(**base)


# --- flag OFF: byte-identical to today ---------------------------------------


@pytest.mark.asyncio
async def test_spt_flag_off_live_lane_is_byte_identical_to_any_other_token(monkeypatch):
    # ⚠️ This pins what the live lane ACTUALLY does today, which is NOT what the
    # PR-B spec assumed. The live-lane guard is `not pm or pm ==
    # DEFAULT_TEST_PAYMENT_METHOD` — it refuses an EMPTY or TEST payment method,
    # it does NOT require a `pm_` prefix. So today an `spt_` (like any other
    # non-empty, non-test token) is forwarded to Stripe as `payment_method`, and
    # Stripe rejects it there. `live_pm_required` is NOT today's answer.
    #
    # Flag OFF must reproduce that exactly, byte for byte, however unattractive
    # it is — a flag that is off may not change behavior. Making the live lane
    # demand a `pm_` prefix would be a real (and arguably correct) tightening,
    # but it is a SEPARATE change with its own blast radius on the proven live
    # path, not something to smuggle in under an off-by-default flag.
    intents = _install_stripe(monkeypatch, _ok())
    _install_merchant_key(monkeypatch, "sk_live_merch")
    _arm_spt(monkeypatch, enabled=False)
    await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method=SPT, allow_live=True, max_cents=500,
    )
    await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method="vt_0123456789abcd", allow_live=True, max_cents=500,
    )
    spt_call, control_call = intents.calls[0], intents.calls[1]
    assert spt_call["payload"]["payment_method"] == SPT   # forwarded, as today
    assert "payment_method_data" not in spt_call["payload"]
    assert spt_call["payload"]["off_session"] is True
    assert "stripe_version" not in spt_call["opts"]
    # …and identical in every respect except the token itself.
    assert {k: v for k, v in spt_call["payload"].items() if k != "payment_method"} == {
        k: v for k, v in control_call["payload"].items() if k != "payment_method"
    }
    assert spt_call["opts"] == control_call["opts"]


@pytest.mark.asyncio
async def test_spt_flag_off_live_lane_still_refuses_an_empty_or_test_pm(monkeypatch):
    # The guard that DOES exist on the live lane is untouched by the SPT work.
    _install_stripe(monkeypatch, _ok())
    _install_merchant_key(monkeypatch, "sk_live_merch")
    _arm_spt(monkeypatch, enabled=False)
    for pm in (None, "", "pm_card_visa"):
        r = await cap.capture_offsession(
            merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
            payment_method=pm, allow_live=True, max_cents=500,
        )
        assert r.success is False and r.error_code == "live_pm_required"


@pytest.mark.asyncio
async def test_spt_flag_off_test_lane_is_byte_identical_to_any_other_token(monkeypatch):
    # The test lane substitutes the test PM for a non-pm token. With the flag
    # off an `spt_` must produce the IDENTICAL request an unrelated placeholder
    # token produces — same params, same request options.
    intents = _install_stripe(monkeypatch, _ok())
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _arm_spt(monkeypatch, enabled=False)
    await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method=SPT, metadata={"order_id": "o1"},
    )
    await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method="tok_test", metadata={"order_id": "o1"},
    )
    spt_call, control_call = intents.calls[0], intents.calls[1]
    assert spt_call["payload"] == control_call["payload"]
    assert spt_call["opts"] == control_call["opts"]
    assert spt_call["payload"]["payment_method"] == "pm_card_visa"
    assert spt_call["payload"]["off_session"] is True
    assert "payment_method_data" not in spt_call["payload"]
    assert "stripe_version" not in spt_call["opts"]


# --- flag ON: the SPT request shape ------------------------------------------


@pytest.mark.asyncio
async def test_spt_flag_on_test_lane_charges_the_spt(monkeypatch):
    # Test-mode SPTs exist, so the test canary charges a REAL test SPT instead
    # of substituting the test PM — substituting would prove nothing about this
    # rail.
    intents = _install_stripe(monkeypatch, _ok())
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _arm_spt(monkeypatch)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="USD", idempotency_key="k_a1",
        payment_method=SPT, metadata={"order_id": "o1"},
    )
    assert r.success is True and r.payment_intent_id == "pi_spt"
    p = intents.calls[0]["payload"]
    # The exact Stripe seller-flow shape, verified against the docs 2026-08-04.
    assert p["payment_method_data"] == {"shared_payment_granted_token": SPT}
    assert "payment_method" not in p          # NOT payment_method=<spt>
    assert "request_delegated_payment" not in p  # no such seller-flow param
    assert "off_session" not in p             # undocumented for SPT → not sent
    assert p["confirm"] is True
    assert p["amount"] == 169 and p["currency"] == "usd"
    assert p["metadata"]["pivota_acp_test_capture"] == "true"
    assert p["metadata"]["order_id"] == "o1"
    assert "customer" not in p
    # The token is not a PaymentMethod — no PM object exists to retrieve, and
    # the lookup must be SKIPPED, not attempted-and-swallowed.
    assert _FakeStripeClient.pm_lookups == []


@pytest.mark.asyncio
async def test_spt_flag_on_live_lane_charges_the_spt(monkeypatch):
    intents = _install_stripe(monkeypatch, _ok(id="pi_spt_live"))
    _install_merchant_key(monkeypatch, "sk_live_merch")
    _arm_spt(monkeypatch)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k_live",
        payment_method=SPT, allow_live=True, max_cents=500,
    )
    assert r.success is True
    assert _FakeStripeClient.last_api_key == "sk_live_merch"
    p = intents.calls[0]["payload"]
    assert p["payment_method_data"] == {"shared_payment_granted_token": SPT}
    assert "payment_method" not in p and "off_session" not in p
    assert p["metadata"]["pivota_acp_live_capture"] == "true"
    assert "pivota_acp_test_capture" not in p["metadata"]
    assert _FakeStripeClient.pm_lookups == []


@pytest.mark.asyncio
async def test_spt_sends_the_preview_version_per_request_only(monkeypatch):
    # The preview version rides in the SDK's PER-REQUEST options (stripe-python
    # 15.1.0 turns `stripe_version` into the Stripe-Version header). The global
    # API version is never touched, so the proven pm_ path keeps charging on
    # exactly the version it charges on today.
    intents = _install_stripe(monkeypatch, _ok())
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _arm_spt(monkeypatch)
    await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k_spt",
        payment_method=SPT,
    )
    await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k_pm",
        payment_method="pm_card_mastercard",
    )
    assert intents.calls[0]["opts"]["stripe_version"] == "2026-04-22.preview"
    assert cap.STRIPE_SPT_API_VERSION == "2026-04-22.preview"
    assert "stripe_version" not in intents.calls[1]["opts"]


@pytest.mark.asyncio
async def test_spt_forwards_the_stored_attempt_scoped_idempotency_key(monkeypatch):
    # Doctrine: the session layer's stored, attempt-scoped key is what a capture
    # charges under — first try or resume — so a replay is parameter-identical.
    intents = _install_stripe(monkeypatch, _ok())
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _arm_spt(monkeypatch)
    key = "acp_complete:csn_abc:a2"
    await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key=key,
        payment_method=SPT,
    )
    assert intents.calls[0]["opts"]["idempotency_key"] == key
    # A resume replays the SAME key with the SAME params — byte-identical.
    await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key=key,
        payment_method=SPT,
    )
    assert intents.calls[1] == intents.calls[0]


@pytest.mark.asyncio
async def test_spt_flag_on_does_not_disturb_the_pm_path(monkeypatch):
    # The flag gates ONE branch. A `pm_` token with the flag ON is unchanged:
    # customer lookup, off_session, payment_method, no preview version.
    intents = _install_stripe(monkeypatch, _ok())
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _FakeStripeClient.pm_customer = "cus_X"
    _arm_spt(monkeypatch)
    await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method="pm_real",
    )
    p = intents.calls[0]["payload"]
    assert p["payment_method"] == "pm_real"
    assert p["off_session"] is True
    assert p["customer"] == "cus_X"
    assert "payment_method_data" not in p
    assert _FakeStripeClient.pm_lookups == ["pm_real"]
    assert "stripe_version" not in intents.calls[0]["opts"]


# --- the lane guards are NOT weakened by an SPT ------------------------------


@pytest.mark.asyncio
async def test_spt_flag_on_test_lane_still_hard_refuses_a_live_key(monkeypatch):
    intents = _install_stripe(monkeypatch, _ok())
    _install_merchant_key(monkeypatch, "sk_live_merch")
    _arm_spt(monkeypatch)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method=SPT,
    )
    assert r.success is False and r.error_code == "live_key_refused"
    assert intents.calls == []


@pytest.mark.asyncio
async def test_spt_flag_on_live_lane_still_refuses_a_test_key(monkeypatch):
    intents = _install_stripe(monkeypatch, _ok())
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _arm_spt(monkeypatch)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method=SPT, allow_live=True, max_cents=500,
    )
    assert r.success is False and r.error_code == "live_key_required"
    assert intents.calls == []


@pytest.mark.asyncio
async def test_spt_still_respects_the_amount_cap(monkeypatch):
    _install_stripe(monkeypatch, _ok())
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _arm_spt(monkeypatch)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=100_000, currency="usd", idempotency_key="k",
        payment_method=SPT, max_cents=500,
    )
    assert r.success is False and r.error_code == "over_cap"


@pytest.mark.asyncio
async def test_spt_on_a_non_stripe_provider_is_unaffected(monkeypatch):
    # The SPT branch belongs to the Stripe adapter alone: no
    # shared_payment_granted_token and no preview version ever reach Adyen.
    # Adyen REFUSES an spt_ pre-dispatch (review N3) rather than forwarding it
    # into its storedPaymentMethodId slot — forwarding produced an ambiguous
    # adyen_http_*/adyen_refused failure, which wedged the session until TTL,
    # whereas a pre-dispatch refusal releases a fresh claim and holds a resumed
    # one. Nothing is sent to Adyen at all, so no Stripe parameter can leak.
    _install_merchant_key(
        monkeypatch, api_key="test_ADYEN", psp_provider="adyen", provider_config=_ADYEN_CFG,
    )
    _install_adyen_http(
        monkeypatch, payload={"resultCode": "Refused", "refusalReason": "Invalid stored PM"},
    )
    _FakeAdyenClient.captured = {}
    _arm_spt(monkeypatch)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="USD", idempotency_key="k",
        payment_method=SPT, metadata=dict(_ADYEN_MD), max_cents=5000,
    )
    assert r.success is False and r.provider == "adyen"
    assert r.error_code == "adyen_pm_required"
    # No HTTP call was made at all — the refusal is local and pre-dispatch.
    assert not _FakeAdyenClient.captured


# --- failure normalization stays conservative --------------------------------


@pytest.mark.asyncio
async def test_spt_unknown_stripe_error_is_normalized_untouched(monkeypatch):
    # Stripe's usage-limit violation codes are UNDOCUMENTED. The adapter must
    # surface whatever code arrives verbatim — no invented mapping — so the
    # session layer's classifier can hold it as ambiguous (claim kept).
    exc = Exception("shared payment token allowance exceeded")
    exc.code = "some_undocumented_spt_code"
    _install_stripe(monkeypatch, exc)
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _arm_spt(monkeypatch)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method=SPT,
    )
    assert r.success is False
    assert r.error_code == "some_undocumented_spt_code"
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_spt_codeless_stripe_error_falls_back_to_stripe_error(monkeypatch):
    _install_stripe(monkeypatch, Exception("boom"))
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _arm_spt(monkeypatch)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method=SPT,
    )
    assert r.success is False and r.error_code == "stripe_error"


@pytest.mark.asyncio
async def test_spt_requires_action_is_still_a_failure(monkeypatch):
    _install_stripe(monkeypatch, _ok(status="requires_action"))
    _install_merchant_key(monkeypatch, "sk_test_merch")
    _arm_spt(monkeypatch)
    r = await cap.capture_offsession(
        merchant_id="m", amount_cents=169, currency="usd", idempotency_key="k",
        payment_method=SPT,
    )
    assert r.success is False and r.error_code == "requires_action"


# --- the token gate ----------------------------------------------------------


def test_is_shared_payment_token_gate():
    assert cap.is_shared_payment_token("spt_abc")
    assert cap.is_shared_payment_token("  spt_abc  ")
    for other in ("pm_card_visa", "vt_abc", "tok_test", "", None, "SPT_ABC"):
        assert not cap.is_shared_payment_token(other)


@pytest.mark.asyncio
async def test_adyen_refuses_spt_in_both_flag_states(monkeypatch):
    # The SPT flag governs the STRIPE adapter only. An Adyen merchant must refuse
    # a SharedPaymentToken pre-dispatch whether the flag is on or off — Stripe's
    # token means nothing to Adyen, and the refusal must not depend on a Stripe
    # feature gate.
    _install_merchant_key(monkeypatch, api_key="test_ADYEN", psp_provider="adyen", provider_config=_ADYEN_CFG)
    for flag in (False, True):
        monkeypatch.setattr(cap.settings, "acp_spt_capture_enabled", flag, raising=False)
        r = await cap.capture_offsession(
            merchant_id="m", amount_cents=169, currency="USD", idempotency_key="k",
            payment_method="spt_1AbcDef", metadata=dict(_ADYEN_MD), max_cents=5000,
        )
        assert r.success is False
        assert r.error_code == "adyen_pm_required"
        # Pre-dispatch by construction: it is in the pre-dispatch set, so a FRESH
        # claim may release on it while a RESUMED claim holds (money-path doctrine).
        from services import acp_checkout_session_service as svc

        assert "adyen_pm_required" in svc._PRE_DISPATCH_ERROR_CODES
