"""In-process ACP checkout-session layer (services/acp_checkout_session_service).

This replaces the retired pivota-acp service, so the tests pin the WIRE-shape
survivors (csn_ ids, minor-unit totals with a type=="total" entry, pvt_*
attribution metadata) and — because /complete is a money path — the fail-closed
posture: dark kill-switch refuses BEFORE any order exists, over-cap refuses, a
failed capture is an error (never a simulated success), and a completed session
replays idempotently without re-charging.
"""

from __future__ import annotations

import inspect
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from db.acp_checkout_sessions import acp_checkout_sessions  # noqa: E402
from db.acp_delegate_allowances import acp_delegate_allowances  # noqa: E402
from db.database import database, engine, metadata  # noqa: E402
from services import acp_checkout_session_service as svc  # noqa: E402
from services import acp_delegate_allowance_service as reg  # noqa: E402
from services import acp_offsession_payment as offpay  # noqa: E402
from config.settings import settings  # noqa: E402


class _Ctx:
    agent_id = "agent_test"

    def can_access_merchant(self, merchant_id):
        return True


_FAKE_QUOTE = {
    "quote_id": "q_fake123",
    "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
    "engine": "shopify_storefront_cart",
    "currency": "USD",
    "pricing": {
        "subtotal": "40.00",
        "discount_total": "0.00",
        "shipping_fee": "5.00",
        "tax": "0.99",
        "total": "45.99",
    },
    "line_items": [
        {
            "product_id": "p1",
            "variant_id": "v1",
            "quantity": 1,
            "unit_price_original": "40.00",
            "unit_price_effective": "40.00",
        }
    ],
    "delivery_options": [],
}


class _FakeQuoteService:
    calls: List[Dict[str, Any]] = []
    quote: Dict[str, Any] = _FAKE_QUOTE
    raises: Optional[Exception] = None

    async def preview_quote(self, **kwargs):
        _FakeQuoteService.calls.append(kwargs)
        if _FakeQuoteService.raises is not None:
            raise _FakeQuoteService.raises
        return dict(_FakeQuoteService.quote)


@pytest.fixture(autouse=True)
async def _db():
    metadata.create_all(engine, tables=[acp_checkout_sessions])
    if not database.is_connected:
        await database.connect()
    # The runtime self-heal owns the additive columns (capture_attempt,
    # psp_idempotency_key): create_all is checkfirst-only and will not add them
    # to a pivota_test.db an older build already created.
    await svc._ensure_acp_checkout_sessions_table()
    await reg._ensure_acp_delegate_allowances_table()
    await database.execute(acp_checkout_sessions.delete())
    await database.execute(acp_delegate_allowances.delete())
    _FakeQuoteService.calls = []
    _FakeQuoteService.quote = _FAKE_QUOTE
    _FakeQuoteService.raises = None
    yield


@pytest.fixture
def fake_quote(monkeypatch):
    monkeypatch.setattr(svc, "QuoteService", _FakeQuoteService)
    return _FakeQuoteService


def _items():
    return [{"product_id": "p1", "variant_id": "v1", "sku": "SKU1", "quantity": 1}]


def _address():
    """A REAL, complete fulfillment address. There is no default any more: a
    session without one cannot be completed (`acp_address_required`)."""
    return {
        "name": "Real Buyer",
        "address_line1": "742 Evergreen Terrace",
        "city": "Springfield",
        "state": "OR",
        "postal_code": "97477",
        "country": "US",
    }


async def _create(**overrides):
    kwargs = dict(
        merchant_id="merch_x",
        platform="shopify",
        items=_items(),
        metadata={"pvt_click_id": "clk_abc", "pvt_surface": "chatgpt"},
        buyer={"email": "buyer@example.com"},
        fulfillment_address=_address(),
        agent_id="agent_test",
    )
    kwargs.update(overrides)
    return await svc.create_session(**kwargs)


# --- create ------------------------------------------------------------------


async def test_create_session_totals_shape_and_id(fake_quote):
    result = await _create()
    assert result.session_id.startswith("csn_")
    assert len(result.session_id) == len("csn_") + 14
    assert result.status == "ready_for_payment"
    assert result.currency == "USD"
    # Old wire shape: minor-unit entries including exactly one type=="total".
    totals = [t for t in result.totals if t.get("type") == "total"]
    assert len(totals) == 1
    assert totals[0]["amount"] == 4599
    assert result.total_cents == 4599
    assert all(isinstance(t.get("amount"), int) for t in result.totals)
    assert result.checkout_url.endswith(f"/{result.session_id}")
    assert result.checkout_url.startswith(settings.agent_acp_checkout_url_base)
    assert result.raw["id"] == result.session_id


async def test_create_session_persists_attribution_and_expiry(fake_quote):
    result = await _create()
    session = await svc.get_session(result.session_id)
    assert session is not None
    md = session["metadata"]
    # pvt_* materialized into the session metadata (attribution parity), and the
    # buyer email preserved even though it also rides the buyer object.
    assert md["pvt_click_id"] == "clk_abc"
    assert md["pvt_surface"] == "chatgpt"
    assert md["customer_email"] == "buyer@example.com"
    assert md["protocol_name"] == "acp"
    expires_at = svc._coerce_datetime_utc(session["expires_at"])
    delta = (expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 3500 < delta <= 3601


async def test_create_session_requires_items(fake_quote):
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await _create(items=[])
    assert ei.value.code == "acp_items_required"


async def test_create_session_quote_failure_is_named(fake_quote):
    from services.quote_service import QuoteError

    _FakeQuoteService.raises = QuoteError("SHOPIFY_PRICING_UNAVAILABLE", "down")
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await _create()
    assert ei.value.code == "acp_quote_failed"


# --- create: payment_provider comes from the merchant's REAL PSP row ---------


def _fake_psp_lookup(monkeypatch, *, row=None, raises=None):
    """Patch the PSP lookup the create response derives `payment_provider` from.
    (The service imports it lazily, so patching the module attribute is what a
    real call would see.) The service uses the provider-ONLY helper — no key
    material rides the create path — so the fake returns just the provider."""
    import services.merchant_psp_config_service as psp_mod

    seen = {"merchant_ids": []}

    async def fake_fetch(**kwargs):
        seen["merchant_ids"].append(kwargs.get("merchant_id"))
        if raises is not None:
            raise raises
        provider = str(((row or {}).get("provider")) or "").strip().lower()
        return provider or None

    monkeypatch.setattr(psp_mod, "fetch_active_merchant_psp_provider", fake_fetch)
    return seen


async def test_create_payment_provider_reflects_a_stripe_psp_row(fake_quote, monkeypatch):
    seen = _fake_psp_lookup(monkeypatch, row={"psp_id": "psp_1", "provider": "stripe"})
    result = await _create()
    assert result.raw["payment_provider"] == {
        "provider": "stripe",
        "supported_payment_methods": ["card"],
    }
    assert seen["merchant_ids"] == ["merch_x"]


async def test_create_payment_provider_reflects_an_adyen_psp_row(fake_quote, monkeypatch):
    # THE point of the change: the old hardcode claimed "stripe" for every
    # merchant, including the ones who settle through Adyen.
    _fake_psp_lookup(monkeypatch, row={"psp_id": "psp_2", "provider": "adyen"})
    result = await _create()
    assert result.raw["payment_provider"]["provider"] == "adyen"


async def test_create_omits_payment_provider_when_the_merchant_has_no_psp_row(
    fake_quote, monkeypatch
):
    # Honest absence, not a guess: the redirect-floor merchants have no PSP at
    # all, and telling an agent "stripe" would be a lie about who can charge.
    _fake_psp_lookup(monkeypatch, row=None)
    result = await _create()
    assert "payment_provider" not in result.raw


async def test_create_omits_payment_provider_when_the_lookup_fails(fake_quote, monkeypatch):
    # And the lookup is NON-FATAL — a session that charges nothing must still be
    # creatable when the PSP table is unreachable.
    _fake_psp_lookup(monkeypatch, raises=RuntimeError("merchant_psps unavailable"))
    result = await _create()
    assert "payment_provider" not in result.raw
    assert result.session_id.startswith("csn_")
    assert await svc.get_session(result.session_id) is not None


async def test_create_omits_payment_provider_for_a_blank_provider_row(fake_quote, monkeypatch):
    _fake_psp_lookup(monkeypatch, row={"psp_id": "psp_3", "provider": "  "})
    result = await _create()
    assert "payment_provider" not in result.raw


async def test_create_payment_provider_lookup_times_out_instead_of_stalling(
    fake_quote, monkeypatch
):
    # Review nit 2: a hung merchant_psps read may only OMIT the field — it must
    # never stall session creation (create charges nothing; latency there is
    # pure UX damage). The 1.5s wait_for is the ceiling; the fake hangs forever.
    import asyncio as _asyncio

    import services.merchant_psp_config_service as psp_mod

    async def hanging_fetch(**kwargs):
        await _asyncio.sleep(3600)

    monkeypatch.setattr(psp_mod, "fetch_active_merchant_psp_provider", hanging_fetch)
    monkeypatch.setattr(svc, "_PAYMENT_PROVIDER_LOOKUP_TIMEOUT_S", 0.05, raising=False)
    result = await _asyncio.wait_for(_create(), timeout=5)
    assert "payment_provider" not in result.raw
    assert result.session_id.startswith("csn_")


def test_no_fabricated_buyer_defaults_exist():
    # The three ported parity hardcodes are GONE, not merely unused.
    assert not hasattr(svc, "DEFAULT_SHIPPING_ADDRESS")
    assert not hasattr(svc, "DEFAULT_BUYER_EMAIL")
    src = inspect.getsource(svc).split('"""', 2)[2]
    assert "1 ACP Street" not in src
    assert "acp-buyer@pivota.cc" not in src
    assert '"provider": "stripe"' not in src
    # Round 2: the recipient-name invention is gone too (name comes from the
    # address or the buyer's own first/last -- never a made-up "Customer").
    assert "'Customer'" not in src and '"Customer"' not in src


# --- get ---------------------------------------------------------------------


async def test_get_session_expired_is_absent(fake_quote):
    result = await _create()
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == result.session_id)
        .values(expires_at=past)
    )
    assert await svc.get_session(result.session_id) is None
    # peek still sees the row (route needs it for the merchant check).
    assert await svc.peek_session(result.session_id) is not None


async def test_get_session_unknown_is_none():
    assert await svc.get_session("csn_nope") is None


# --- complete ----------------------------------------------------------------


def _arm_capture_lane(monkeypatch, *, submit=True, test_capture=True):
    monkeypatch.setattr(settings, "agent_checkout_strict", True, raising=False)
    monkeypatch.setattr(settings, "agent_submit_payment_enabled", submit, raising=False)
    monkeypatch.setattr(settings, "agent_submit_payment_merchants", frozenset(), raising=False)
    monkeypatch.setattr(settings, "agent_acp_test_capture", test_capture, raising=False)
    monkeypatch.setattr(settings, "agent_acp_test_max_cents", 500, raising=False)
    monkeypatch.setattr(settings, "agent_acp_allow_live_capture", False, raising=False)


def _mock_order_layer(monkeypatch, *, order_total=1.69, order_id="ord_acp_1"):
    calls = {"order_create": 0, "capture": 0, "settle": 0, "capture_kwargs": None}

    async def fake_create_order(**kwargs):
        calls["order_create"] += 1
        return order_id

    monkeypatch.setattr(svc, "_create_pivota_order", fake_create_order)

    import db.orders as orders_mod

    async def fake_get_order(oid):
        return {
            "order_id": oid,
            "merchant_id": "merch_x",
            "total_amount": order_total,
            "currency": "USD",
            "status": "created",
            "payment_status": "pending",
            "metadata": {"protocol_name": "acp", "checkout_session_id": "whatever"},
        }

    monkeypatch.setattr(orders_mod, "get_order", fake_get_order)

    async def fake_execute(**kwargs):
        calls["capture"] += 1
        calls["capture_kwargs"] = kwargs
        from types import SimpleNamespace

        return offpay.AcpOffsessionPaymentOutcome(
            success=True, status="succeeded", payment_intent_id="pi_fake",
            psp_used="stripe", error=None, error_code=None,
            payment_intent=SimpleNamespace(id="pi_fake", status="succeeded", client_secret=None),
        )

    async def fake_settle(**kwargs):
        calls["settle"] += 1

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", fake_execute)
    monkeypatch.setattr(offpay, "settle_acp_offsession_success", fake_settle)
    return calls


async def test_complete_happy_path(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()

    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="idem_1",
    )
    assert out["status"] == "completed"
    assert out["order_id"] == "ord_acp_1"
    assert out["payment_status"] == "succeeded"
    assert out["order"]["id"] == "ord_acp_1"
    assert out["order"]["permalink_url"].endswith("/agent/v1/orders/ord_acp_1")
    assert (calls["order_create"], calls["capture"], calls["settle"]) == (1, 1, 1)
    # No private bookkeeping leaks into the returned contract.
    assert not any(str(k).startswith("_") for k in out)

    session = await svc.peek_session(created.session_id)
    assert session["status"] == "completed"
    assert session["order_id"] == "ord_acp_1"
    assert session["completed_at"] is not None


async def test_complete_idempotent_replay_never_recharges(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    first = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="idem_1",
    )
    replay = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="idem_1",
    )
    assert replay == first
    assert calls["capture"] == 1  # the charge ran exactly once
    assert calls["order_create"] == 1


async def test_complete_idempotency_conflict_on_different_payload(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="idem_1",
    )
    # Same Idempotency-Key, different payment token → ACP wire 409 semantics.
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="pm_other", idempotency_key="idem_1",
        )
    assert ei.value.code == "idempotency_conflict"
    assert ei.value.status_code == 409


async def test_complete_missing_session_404(fake_quote):
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(session_id="csn_missing", agent_context=_Ctx())
    assert ei.value.code == "acp_session_not_found"
    assert ei.value.status_code == 404


async def test_complete_expired_session_is_refused(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == created.session_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(session_id=created.session_id, agent_context=_Ctx())
    assert ei.value.code == "acp_session_expired"


async def test_complete_dark_kill_switch_refuses_before_any_order(fake_quote, monkeypatch):
    # THE positive control that dark stays dark: default posture (strict ON,
    # submit OFF) → 403 in the kill-switch's own shape, and no order/charge
    # side effects at all.
    from fastapi import HTTPException

    _arm_capture_lane(monkeypatch, submit=False)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    with pytest.raises(HTTPException) as ei:
        await svc.complete_session(session_id=created.session_id, agent_context=_Ctx())
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "TIER2_CHARGE_DISABLED"
    assert (calls["order_create"], calls["capture"], calls["settle"]) == (0, 0, 0)
    session = await svc.peek_session(created.session_id)
    assert session["status"] == "ready_for_payment"


async def test_complete_over_cap_refuses(fake_quote, monkeypatch):
    from fastapi import HTTPException

    _arm_capture_lane(monkeypatch)  # cap = 500 cents
    calls = _mock_order_layer(monkeypatch, order_total=9.99)  # 999 cents
    created = await _create()
    with pytest.raises(HTTPException) as ei:
        await svc.complete_session(session_id=created.session_id, agent_context=_Ctx())
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "TIER2_TEST_CAPTURE_OVER_CAP"
    assert calls["capture"] == 0
    session = await svc.peek_session(created.session_id)
    assert session["status"] == "ready_for_payment"  # never marked completed


async def test_complete_no_capture_lane_fails_closed(fake_quote, monkeypatch):
    # Kill-switch permits but no off-session lane is armed → refuse (an
    # off-session completion has no client-confirm fallback).
    _arm_capture_lane(monkeypatch, test_capture=False)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(session_id=created.session_id, agent_context=_Ctx())
    assert ei.value.code == "acp_capture_lane_disabled"
    assert ei.value.status_code == 403
    assert calls["capture"] == 0


async def test_complete_capture_failure_is_error_not_success(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()

    from types import SimpleNamespace

    async def failing_capture(**kwargs):
        calls["capture"] += 1
        return offpay.AcpOffsessionPaymentOutcome(
            success=False, status="failed", payment_intent_id="pi_fail",
            psp_used="stripe", error="card_declined", error_code="card_declined",
            payment_intent=SimpleNamespace(id="pi_fail", status="failed", client_secret=None),
        )

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", failing_capture)
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(session_id=created.session_id, agent_context=_Ctx())
    assert ei.value.code == "acp_capture_failed"
    assert ei.value.status_code == 502
    assert calls["settle"] == 0  # nothing finalized
    session = await svc.peek_session(created.session_id)
    assert session["status"] == "ready_for_payment"  # NOT completed


# --- fail-closed fulfillment identity (no fabricated address / buyer email) --


async def _strip(session_id, **values):
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == session_id)
        .values(**values)
    )


async def test_complete_without_fulfillment_address_fails_closed(fake_quote, monkeypatch):
    # THE removed "1 ACP Street" default: a session that never named a
    # destination must refuse, pre-claim, with zero side effects — never charge
    # a card for goods sent to an address we made up.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create(fulfillment_address=None)

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="pm_card_visa", idempotency_key="idem_addr",
        )
    assert ei.value.code == "acp_address_required"
    assert ei.value.status_code == 400
    # No update path exists, so the error has to tell the caller where the
    # address must come from.
    assert "created" in ei.value.message
    assert (calls["order_create"], calls["capture"], calls["settle"]) == (0, 0, 0)

    # Session UNTOUCHED: still claimable, no order, no claim, no attempt burned.
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "ready_for_payment"
    assert row["order_id"] is None
    assert row["completion"] is None
    assert (row["capture_attempt"] or 0) == 0
    assert row["idempotency_key"] is None


async def test_complete_with_an_incomplete_address_fails_closed(fake_quote, monkeypatch):
    # A street line alone is not a usable address — ShippingAddress needs
    # city/postal_code/country, and the old code backfilled exactly those from
    # the placeholder default (silently relocating the order).
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create(fulfillment_address={"address_line1": "742 Evergreen Terrace"})

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
        )
    assert ei.value.code == "acp_address_required"
    assert ei.value.status_code == 400
    for field in ("city", "postal_code", "country"):
        assert field in ei.value.message
    assert calls["capture"] == 0
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "ready_for_payment"


async def test_complete_with_a_real_address_proceeds(fake_quote, monkeypatch):
    # The other way: a complete address completes, and the REAL address (never a
    # default) is what the completion quote is built against.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
    )
    assert out["status"] == "completed"
    assert calls["capture"] == 1
    completion_quote = _FakeQuoteService.calls[-1]
    assert completion_quote["shipping_address"]["address_line1"] == "742 Evergreen Terrace"
    assert completion_quote["shipping_address"]["postal_code"] == "97477"


async def test_complete_accepts_the_acp_wire_address_shape(fake_quote, monkeypatch):
    # ACP sends line_one/postal_code; the normalizer maps it, so this is a
    # usable address and must NOT be refused.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create(
        fulfillment_address={
            "name": "Wire Buyer", "line_one": "9 Protocol Rd", "city": "Portland",
            "postal_code": "97201", "country": "US",
        }
    )
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
    )
    assert out["status"] == "completed"
    assert calls["capture"] == 1


async def test_complete_without_any_buyer_email_fails_closed(fake_quote, monkeypatch):
    # THE removed acp-buyer@pivota.cc default: no buyer email anywhere → refuse
    # pre-claim, never attribute a real charge to a mailbox we invented.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create(buyer=None, metadata={"pvt_click_id": "clk_abc"})
    # create copies buyer.email into metadata.customer_email; with no buyer there
    # is nothing to copy — assert the row really carries neither.
    row = await svc.peek_session(created.session_id)
    assert not (row["buyer"] or {})
    assert "customer_email" not in (row["metadata"] or {})

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="pm_card_visa", idempotency_key="idem_email",
        )
    assert ei.value.code == "acp_email_required"
    assert ei.value.status_code == 400
    assert (calls["order_create"], calls["capture"], calls["settle"]) == (0, 0, 0)

    row = await svc.peek_session(created.session_id)
    assert row["status"] == "ready_for_payment"
    assert row["order_id"] is None
    assert (row["capture_attempt"] or 0) == 0


async def test_complete_with_only_the_buyer_email_proceeds(fake_quote, monkeypatch):
    # Buyer email present, metadata.customer_email absent → completes.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    await _strip(created.session_id, metadata={"pvt_click_id": "clk_abc", "protocol_name": "acp"})
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
    )
    assert out["status"] == "completed"
    assert calls["capture"] == 1
    assert _FakeQuoteService.calls[-1]["customer_email"] == "buyer@example.com"


async def test_complete_with_only_metadata_customer_email_proceeds(fake_quote, monkeypatch):
    # No buyer object at all, but metadata.customer_email → completes.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create(
        buyer=None, metadata={"pvt_click_id": "clk_abc", "customer_email": "meta@example.com"}
    )
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
    )
    assert out["status"] == "completed"
    assert calls["capture"] == 1
    assert _FakeQuoteService.calls[-1]["customer_email"] == "meta@example.com"


async def test_blank_buyer_email_is_not_an_email(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create(buyer={"email": "   "}, metadata={"pvt_click_id": "clk_abc"})
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
        )
    assert ei.value.code == "acp_email_required"
    assert calls["capture"] == 0


async def test_fulfillment_refusals_do_not_leak_past_a_dark_kill_switch(fake_quote, monkeypatch):
    # Ordering guard: a dark merchant still answers 403 TIER2_CHARGE_DISABLED —
    # the new 400s must not become an oracle for a lane that is switched off.
    from fastapi import HTTPException

    _arm_capture_lane(monkeypatch, submit=False)
    _mock_order_layer(monkeypatch)
    created = await _create(fulfillment_address=None, buyer=None, metadata={})
    with pytest.raises(HTTPException) as ei:
        await svc.complete_session(session_id=created.session_id, agent_context=_Ctx())
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "TIER2_CHARGE_DISABLED"


async def test_wedged_pre_change_session_can_still_converge(fake_quote, monkeypatch):
    # Migration safety, the money-critical half: a row left `completing` by the
    # OLD code charged under its stored PSP key with the old defaults. The new
    # pre-claim refusal must NOT strand that charge — a stale resume still
    # converges (the resume creates no fulfillment data; it replays the key).
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    await _strip(
        created.session_id,
        fulfillment_address=None,
        buyer=None,
        metadata={"protocol_name": "acp"},
        status="completing",
        order_id="ord_acp_1",
        capture_attempt=1,
        psp_idempotency_key=f"acp_complete:{created.session_id}:a1",
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    keys: list = []
    _spy_capture(monkeypatch, keys)

    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
    )
    assert out["status"] == "completed"
    assert out["order_id"] == "ord_acp_1"
    assert keys == [f"acp_complete:{created.session_id}:a1"]  # same key → replay
    assert calls["order_create"] == 0  # order reused; no fabricated order created


async def test_completed_session_without_an_address_still_replays(fake_quote, monkeypatch):
    # Migration safety: sessions completed BEFORE this change were charged with
    # the old defaults. Their idempotent replay must not start 400-ing (that
    # would invite a re-charge attempt), so the replay wins over the new checks.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    first = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="idem_legacy",
    )
    await _strip(created.session_id, fulfillment_address=None, buyer=None)
    replay = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="idem_legacy",
    )
    assert replay == first
    assert calls["capture"] == 1  # still exactly one charge


# --- review findings: charge-sequencing safety (F2-F4, F6, F7) ---------------


async def test_concurrent_double_complete_charges_exactly_once(fake_quote, monkeypatch):
    # F2: two simultaneous /complete calls — the claim admits exactly ONE
    # charge; the loser gets 409 completion_in_progress (or the stored replay
    # if it arrives after the winner finished).
    import asyncio

    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)

    # Hold the winner mid-capture so the loser deterministically overlaps.
    orig_capture = offpay.execute_acp_offsession_payment

    async def slow_capture(**kwargs):
        await asyncio.sleep(0.05)
        return await orig_capture(**kwargs)

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", slow_capture)

    created = await _create()

    async def attempt():
        try:
            return await svc.complete_session(
                session_id=created.session_id, agent_context=_Ctx(),
                payment_token="pm_card_visa",
            )
        except svc.AcpCheckoutSessionError as exc:
            return exc

    async def staggered_attempt():
        await asyncio.sleep(0.01)
        return await attempt()

    first, second = await asyncio.gather(attempt(), staggered_attempt())
    results = [first, second]
    successes = [r for r in results if isinstance(r, dict) and r.get("status") == "completed"]
    losers = [r for r in results if isinstance(r, svc.AcpCheckoutSessionError)]
    assert len(successes) >= 1
    if losers:
        assert losers[0].code == "completion_in_progress"
        assert losers[0].status_code == 409
    else:
        # Both returned: the second must be the idempotent replay of the first.
        assert successes[0]["order_id"] == successes[1]["order_id"]
    # THE money assertions: one order, one capture.
    assert calls["order_create"] == 1
    assert calls["capture"] == 1


async def test_psp_idempotency_key_is_session_derived(fake_quote, monkeypatch):
    # F3 + N1: the PSP key must NOT be the caller's Idempotency-Key — it is
    # minted by the CLAIM and STORED on the row, attempt-scoped.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    captured_keys = []

    orig_execute = offpay.execute_acp_offsession_payment

    async def spy_execute(**kwargs):
        captured_keys.append(kwargs["idempotency_key"])
        return await orig_execute(**kwargs)

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", spy_execute)
    created = await _create()
    await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="caller_key_xyz",
    )
    assert captured_keys == [f"acp_complete:{created.session_id}:a1"]
    assert calls["capture"] == 1
    # The key the charge ran under is the key PERSISTED before any order work.
    row = await svc.peek_session(created.session_id)
    assert row["capture_attempt"] == 1
    assert row["psp_idempotency_key"] == captured_keys[0]


async def test_retry_after_persist_failure_converges_without_second_charge(fake_quote, monkeypatch):
    # F3+F4: capture succeeds, the completion persist fails → 502 with
    # reconciliation_required; the row stays completing+order_id. A LATER retry
    # (different caller key!) resumes: same order, same derived PSP key, and the
    # persist converges.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    captured_keys = []

    orig_execute = offpay.execute_acp_offsession_payment

    async def spy_execute(**kwargs):
        captured_keys.append(kwargs["idempotency_key"])
        return await orig_execute(**kwargs)

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", spy_execute)

    real_persist = svc._persist_completion
    fail_once = {"armed": True}

    async def flaky_persist(**kwargs):
        if fail_once["armed"]:
            fail_once["armed"] = False
            raise RuntimeError("db blip")
        return await real_persist(**kwargs)

    monkeypatch.setattr(svc, "_persist_completion", flaky_persist)

    created = await _create()
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="pm_card_visa", idempotency_key="key_attempt_1",
        )
    assert ei.value.code == "acp_completion_persist_failed"
    assert ei.value.status_code == 502
    assert ei.value.extra.get("reconciliation_required") is True

    row = await svc.peek_session(created.session_id)
    assert row["status"] == "completing"  # NOT completed, NOT reverted
    assert row["order_id"] == "ord_acp_1"  # kept for reuse

    # Age the wedged claim past the resume window, then retry with a DIFFERENT key.
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == created.session_id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="key_attempt_2_fresh",
    )
    assert out["status"] == "completed"
    assert out["order_id"] == "ord_acp_1"
    assert calls["order_create"] == 1  # the order was REUSED, never re-minted
    # Both capture attempts used the SAME STORED PSP key → PSP-side replay, no
    # second charge. The resume must NOT have incremented the attempt counter
    # (that is what would have minted a new key and re-charged).
    derived = f"acp_complete:{created.session_id}:a1"
    assert captured_keys == [derived, derived]
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "completed"
    assert row["capture_attempt"] == 1


async def test_fresh_concurrent_completing_claim_is_409_not_resumed(fake_quote, monkeypatch):
    # A row in `completing` that is NOT stale is an in-flight completion — a
    # second caller must get 409, never race the charge.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == created.session_id)
        .values(status="completing", order_id="ord_inflight", updated_at=datetime.now(timezone.utc))
    )
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(session_id=created.session_id, agent_context=_Ctx())
    assert ei.value.code == "completion_in_progress"
    assert ei.value.status_code == 409
    assert calls["capture"] == 0


async def test_cross_session_idempotency_key_collision_is_409_before_any_charge(fake_quote, monkeypatch):
    # F2 item 2 + item 5: reusing an Idempotency-Key from ANOTHER session is
    # refused up front — no charge, no unbounded retry-recharge loop (the old
    # unique index aborted the completion write only AFTER the charge).
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    session_a = await _create()
    await svc.complete_session(
        session_id=session_a.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="shared_key",
    )
    assert calls["capture"] == 1

    session_b = await _create()
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=session_b.session_id, agent_context=_Ctx(),
            payment_token="pm_card_visa", idempotency_key="shared_key",
        )
    assert ei.value.code == "idempotency_conflict"
    assert ei.value.status_code == 409
    assert calls["capture"] == 1  # session B never charged
    row_b = await svc.peek_session(session_b.session_id)
    assert row_b["status"] == "ready_for_payment"  # not wedged — retryable with its own key


async def test_zero_decimal_currency_charges_major_units(fake_quote, monkeypatch):
    # F7: JPY 300 must charge amount=300, not 30000.
    _FakeQuoteService.quote = {
        **_FAKE_QUOTE,
        "currency": "JPY",
        "pricing": {"subtotal": "250", "discount_total": "0",
                    "shipping_fee": "30", "tax": "20", "total": "300"},
    }
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch, order_total=300)

    import db.orders as orders_mod

    async def fake_get_order_jpy(oid):
        return {
            "order_id": oid, "merchant_id": "merch_x", "total_amount": 300,
            "currency": "JPY", "status": "created", "payment_status": "pending",
            "metadata": {"protocol_name": "acp"},
        }

    monkeypatch.setattr(orders_mod, "get_order", fake_get_order_jpy)

    created = await _create()
    assert created.total_cents == 300  # display totals are currency-aware too
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
    )
    assert out["amount_cents"] == 300
    assert out["currency"] == "JPY"
    assert calls["capture_kwargs"]["gates"].amount_cents == 300


async def test_agent_mismatch_is_403(fake_quote, monkeypatch):
    # F6: agent A cannot complete agent B's session, even with merchant access.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create(agent_id="agent_b")

    class _CtxA(_Ctx):
        agent_id = "agent_a"

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(session_id=created.session_id, agent_context=_CtxA())
    assert ei.value.code == "acp_agent_mismatch"
    assert ei.value.status_code == 403
    assert calls["order_create"] == 0 and calls["capture"] == 0


# --- review findings: attempt-scoped stored PSP keys (N1, N2, N4, N5) --------


async def _wedge_completing(session_id, *, order_id, attempt=1, key=None, age_seconds=120):
    """Force the row into the `completing` state a crashed attempt would leave,
    optionally FRESH (age_seconds=0) instead of stale."""
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == session_id)
        .values(
            status="completing",
            order_id=order_id,
            capture_attempt=attempt,
            psp_idempotency_key=(key if key is not None else f"acp_complete:{session_id}:a{attempt}"),
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        )
    )


def _spy_capture(monkeypatch, keys, *, outcomes=None):
    """Record every PSP idempotency key a capture is dispatched with. `outcomes`
    is a list of pre-scripted failures consumed in order; anything past it falls
    through to the (successful) mocked capture."""
    orig_execute = offpay.execute_acp_offsession_payment
    scripted = list(outcomes or [])

    async def spy(**kwargs):
        keys.append(kwargs["idempotency_key"])
        if scripted:
            return scripted.pop(0)
        return await orig_execute(**kwargs)

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", spy)


def _failed_outcome(error_code):
    from types import SimpleNamespace

    return offpay.AcpOffsessionPaymentOutcome(
        success=False, status="failed", payment_intent_id="pi_failed",
        psp_used="stripe", error=error_code, error_code=error_code,
        payment_intent=SimpleNamespace(id="pi_failed", status="failed", client_secret=None),
    )


async def test_definitive_decline_releases_claim_and_next_attempt_mints_a_new_key(
    fake_quote, monkeypatch
):
    # N1: a PSP-confirmed decline is the ONE capture failure that may release
    # the claim — the PSP has cached that answer against the key we used, so the
    # retry has to charge under a NEW key or the new card would just replay the
    # decline forever.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    keys: list = []
    _spy_capture(monkeypatch, keys, outcomes=[_failed_outcome("card_declined")])

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="pm_bad_card", idempotency_key="key_try_1",
        )
    assert ei.value.code == "acp_capture_failed"
    assert ei.value.status_code == 502
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "ready_for_payment"  # claim RELEASED — retryable
    assert row["capture_attempt"] == 1
    assert row["order_id"] == "ord_acp_1"  # order kept for reuse

    # Retry with a different card: a NEW attempt, a NEW PSP key, a real second
    # capture dispatch (the first one can never be replayed).
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_good_card", idempotency_key="key_try_2",
    )
    assert out["status"] == "completed"
    assert keys == [
        f"acp_complete:{created.session_id}:a1",
        f"acp_complete:{created.session_id}:a2",
    ]
    assert calls["order_create"] == 1  # order reused, never re-minted
    assert calls["capture"] == 1  # the mocked (successful) capture ran once
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "completed"
    assert row["capture_attempt"] == 2
    assert row["psp_idempotency_key"] == f"acp_complete:{created.session_id}:a2"


async def test_ambiguous_capture_failure_holds_claim_and_resume_replays_same_key(
    fake_quote, monkeypatch
):
    # N1: an UNRESOLVED capture failure may have charged. Releasing the claim
    # would mint a new key on retry — a second charge. So the claim is held, the
    # caller gets a DISTINCT code, and the resume replays the stored key.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    keys: list = []
    _spy_capture(monkeypatch, keys, outcomes=[_failed_outcome("stripe_error")])

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="pm_card_visa", idempotency_key="key_try_1",
        )
    assert ei.value.code == "acp_capture_pending_retry"
    assert ei.value.status_code == 502
    assert ei.value.extra["order_id"] == "ord_acp_1"
    assert calls["settle"] == 0
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "completing"  # claim HELD
    assert row["capture_attempt"] == 1

    # Age it past the resume window and retry with a FRESH caller key.
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == created.session_id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="key_try_2_fresh",
    )
    assert out["status"] == "completed"
    replayed = f"acp_complete:{created.session_id}:a1"
    assert keys == [replayed, replayed]  # SAME key → PSP-side replay
    row = await svc.peek_session(created.session_id)
    assert row["capture_attempt"] == 1  # the resume did NOT mint a new attempt


async def test_exception_during_capture_is_ambiguous_and_holds_the_claim(
    fake_quote, monkeypatch
):
    # A timeout/transport blow-up is the canonical ambiguous case: the PSP may
    # have taken the charge and we simply never heard back.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()

    async def exploding_capture(**kwargs):
        raise TimeoutError("psp did not answer")

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", exploding_capture)
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
        )
    assert ei.value.code == "acp_capture_pending_retry"
    assert calls["settle"] == 0
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "completing"
    assert row["psp_idempotency_key"] == f"acp_complete:{created.session_id}:a1"


async def test_two_concurrent_stale_resumers_exactly_one_proceeds(fake_quote, monkeypatch):
    # N4: the stale takeover is itself a conditional UPDATE, so a wedged session
    # cannot be resumed by two callers at once (which would race the same PSP
    # key through two captures).
    import asyncio

    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    await _wedge_completing(created.session_id, order_id="ord_acp_1")

    orig_execute = offpay.execute_acp_offsession_payment

    async def slow_capture(**kwargs):
        await asyncio.sleep(0.05)  # hold the winner in-flight
        return await orig_execute(**kwargs)

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", slow_capture)

    async def attempt(delay):
        await asyncio.sleep(delay)
        try:
            return await svc.complete_session(
                session_id=created.session_id, agent_context=_Ctx(),
                payment_token="pm_card_visa",
            )
        except svc.AcpCheckoutSessionError as exc:
            return exc

    first, second = await asyncio.gather(attempt(0), attempt(0.01))
    results = [first, second]
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, svc.AcpCheckoutSessionError)]
    assert len(winners) == 1, results
    assert len(losers) == 1
    assert losers[0].code == "completion_in_progress"
    assert losers[0].status_code == 409
    assert calls["capture"] == 1  # THE money assertion: one resume, one capture


async def test_stale_completing_without_order_id_is_recovered(fake_quote, monkeypatch):
    # N2: the crash window BETWEEN the claim and order creation leaves
    # completing + order_id NULL. The resume must re-enter the order-create path
    # and still capture under the STORED key.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    await _wedge_completing(created.session_id, order_id=None)
    keys: list = []
    _spy_capture(monkeypatch, keys)

    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
    )
    assert out["status"] == "completed"
    assert out["order_id"] == "ord_acp_1"
    assert calls["order_create"] == 1  # the order missing from the crash window
    assert keys == [f"acp_complete:{created.session_id}:a1"]  # stored key, not a2
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "completed"
    assert row["capture_attempt"] == 1


async def test_fresh_completing_without_order_id_is_409(fake_quote, monkeypatch):
    # The other half of N2: a FRESH completing+NULL-order row is an in-flight
    # attempt that simply has not created its order yet — never take it over.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    await _wedge_completing(created.session_id, order_id=None, age_seconds=0)

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
        )
    assert ei.value.code == "completion_in_progress"
    assert ei.value.status_code == 409
    assert (calls["order_create"], calls["capture"]) == (0, 0)


async def test_resume_of_pre_attempt_scoping_row_replays_the_legacy_key(fake_quote, monkeypatch):
    # Migration safety: a row wedged by a build that predates attempt scoping
    # charged under the UN-suffixed key. Its resume must replay THAT key, not a
    # freshly minted one.
    _arm_capture_lane(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    # Exactly what such a row looks like: claimed, order created, no attempt
    # counter and no stored key.
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == created.session_id)
        .values(
            status="completing",
            order_id="ord_acp_1",
            capture_attempt=0,
            psp_idempotency_key=None,
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=120),
        )
    )
    keys: list = []
    _spy_capture(monkeypatch, keys)

    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
    )
    assert out["status"] == "completed"
    assert keys == [f"acp_complete:{created.session_id}"]


async def test_create_session_refuses_a_blank_agent_id(fake_quote):
    # N5 (create side): an explicit None/blank would mint a session that
    # complete_session can never accept — refused at the door.
    for bad in (None, "", "   "):
        with pytest.raises(svc.AcpCheckoutSessionError) as ei:
            await _create(agent_id=bad)
        assert ei.value.code == "acp_agent_required"
        assert ei.value.status_code == 400


async def test_unbound_agent_session_cannot_be_completed(fake_quote, monkeypatch):
    # N5 (complete side): create refuses NULL agents now, so an unbound session
    # can only come from an out-of-contract writer — seed one directly and prove
    # the money path still refuses it.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == created.session_id)
        .values(agent_id=None)
    )
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
        )
    assert ei.value.code == "acp_agent_unbound"
    assert ei.value.status_code == 403
    assert (calls["order_create"], calls["capture"]) == (0, 0)


def test_create_session_requires_an_agent_id_argument():
    # N5: agent_id has NO default — a caller cannot silently mint a session that
    # can never be completed.
    param = inspect.signature(svc.create_session).parameters["agent_id"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_capture_failure_classification_is_conservative():
    # The classifier decides whether a failure may RELEASE the claim (and thus
    # let the next attempt mint a new PSP key). Only proven refusals qualify.
    from types import SimpleNamespace

    def definitive(status, code):
        return svc._capture_failure_is_definitive(
            SimpleNamespace(status=status, error_code=code)
        )

    # PSP said no.
    assert definitive("failed", "card_declined")
    assert definitive("failed", "expired_card")
    # Review B2: adyen_refused covers resultCodes where money may have moved
    # (Received/Pending/PartiallyAuthorised) — ambiguous until the adapter
    # narrows to resultCode=="Refused" and an Adyen canary proves it.
    assert not definitive("failed", "adyen_refused")
    # Nothing was ever dispatched to a PSP.
    assert definitive("failed", "no_merchant_psp")
    assert definitive("failed", "live_key_refused")
    assert definitive("failed", "adyen_pm_required")
    # Ambiguous — the charge may exist.
    assert not definitive("failed", "stripe_error")
    assert not definitive("failed", "adyen_network_error")
    assert not definitive("failed", "adyen_http_502")
    assert not definitive("failed", "adyen_bad_response")
    assert not definitive("failed", "not_succeeded")
    assert not definitive("requires_action", "requires_action")
    assert not definitive("requires_action", "card_declined")
    assert not definitive("failed", None)
    assert not definitive(None, "card_declined")


async def test_payments_record_amount_is_currency_aware(monkeypatch):
    # N3: JPY 300 minor units ARE 300 major units. The old `amount_cents / 100.0`
    # wrote 3.0 into payments.amount — a 100x understated money record on every
    # zero-decimal currency.
    from types import SimpleNamespace

    import db.orders as orders_mod
    from db.database import database as db

    recorded: Dict[str, Any] = {}

    async def fake_execute(query, values=None):
        if "INSERT INTO payments" in str(query):
            recorded.update(values or {})
        return None

    async def fake_update_payment_info(**kwargs):
        return True

    async def fake_finalize(**kwargs):
        return None

    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(orders_mod, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(offpay, "_finalize_paid_transition", fake_finalize)

    gates = offpay.AcpOffsessionGates(
        kill_switch=SimpleNamespace(),
        test_capture=SimpleNamespace(bypass_live_readiness=True),
        live_capture=SimpleNamespace(allow_live=False),
        amount_cents=300,
    )
    outcome = offpay.AcpOffsessionPaymentOutcome(
        success=True, status="succeeded", payment_intent_id="pi_jpy",
        psp_used="stripe", error=None, error_code=None,
        payment_intent=SimpleNamespace(id="pi_jpy", status="succeeded", client_secret=None),
    )
    flags = await offpay.settle_acp_offsession_success(
        gates=gates, outcome=outcome,
        order={"order_id": "ord_jpy", "merchant_id": "merch_x", "currency": "JPY", "metadata": {}},
        order_id="ord_jpy", merchant_id="merch_x", currency="JPY",
        idempotency_key="idem_jpy", agent_id="agent_test", order_metadata={},
    )
    assert flags["reconciliation_needed"] is False
    assert recorded["currency"] == "JPY"
    assert recorded["amount"] == 300  # NOT 3.0


def test_to_minor_units_helper():
    from utils.money import to_minor_units

    assert to_minor_units("1.69", "USD") == 169
    assert to_minor_units("1.005", "USD") == 101  # Decimal ROUND_HALF_UP, no float drift
    assert to_minor_units("300", "JPY") == 300
    assert to_minor_units(300, "jpy") == 300
    assert to_minor_units("1000", "KRW") == 1000
    assert to_minor_units("2500", "VND") == 2500
    assert to_minor_units(None, "USD") == 0


def test_from_minor_units_is_the_exact_inverse():
    from utils.money import from_minor_units, to_minor_units

    assert from_minor_units(169, "USD") == 1.69
    assert from_minor_units(300, "JPY") == 300  # zero-decimal: minor IS major
    assert from_minor_units(1000, "krw") == 1000
    assert from_minor_units(None, "USD") == 0
    for amount, currency in (("1.69", "USD"), ("0.01", "USD"), ("300", "JPY"), ("2500", "VND")):
        assert from_minor_units(to_minor_units(amount, currency), currency) == float(amount)


def test_no_simulation_fallback_exists():
    # The old service's simulated capture (fake `payment_captured` webhook) must
    # NOT be ported: a failed capture is an error, never a pretend success.
    # Skip the module docstring (which documents WHY the fallback is absent) —
    # the assertion is about executable code paths.
    src = inspect.getsource(svc).split('"""', 2)[2]
    assert "payment_captured" not in src
    assert "def simulate" not in src and "simulate_" not in src


async def test_resumed_claim_is_held_when_pre_dispatch_work_fails(fake_quote, monkeypatch):
    # Review B1: a RESUMED claim exists precisely because a prior attempt may
    # have left a charge in flight under the stored key. A pre-dispatch failure
    # during the resume (order lookup, quote, gates) must therefore HOLD the
    # claim — releasing it would let the next attempt mint :a2 and double-charge.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    keys: list = []
    _spy_capture(monkeypatch, keys, outcomes=[_failed_outcome("stripe_error")])

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="pm_card_visa", idempotency_key="key_try_1",
        )
    assert ei.value.code == "acp_capture_pending_retry"  # ambiguous → claim held

    # Age past the resume window, then make the RESUME fail before dispatch.
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == created.session_id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    import db.orders as orders_mod

    async def exploding_get_order(oid):
        raise RuntimeError("orders table unavailable")

    monkeypatch.setattr(orders_mod, "get_order", exploding_get_order)
    with pytest.raises(Exception):
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="pm_card_visa", idempotency_key="key_try_2",
        )
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "completing"  # HELD — not reverted to ready
    assert row["capture_attempt"] == 1
    assert row["psp_idempotency_key"] == f"acp_complete:{created.session_id}:a1"

    # Restore the order layer; the next stale resume converges on the SAME key.
    async def good_get_order(oid):
        return {
            "order_id": oid, "merchant_id": "merch_x", "total_amount": 1.69,
            "currency": "USD", "status": "created", "payment_status": "pending",
            "metadata": {"protocol_name": "acp", "checkout_session_id": "whatever"},
        }

    monkeypatch.setattr(orders_mod, "get_order", good_get_order)
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == created.session_id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="key_try_3",
    )
    assert out["status"] == "completed"
    derived = f"acp_complete:{created.session_id}:a1"
    assert keys == [derived, derived]  # never :a2 — no re-keyed second charge
    row = await svc.peek_session(created.session_id)
    assert row["capture_attempt"] == 1


async def test_resumed_claim_is_held_on_pre_dispatch_failure_after_persist_failure(
    fake_quote, monkeypatch
):
    # Review B1, severe variant: attempt 1 CHARGED (only the completion persist
    # failed). A failing resume must not release the claim — the charge exists.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    keys: list = []
    _spy_capture(monkeypatch, keys)

    real_persist = svc._persist_completion
    fail_once = {"armed": True}

    async def flaky_persist(**kwargs):
        if fail_once["armed"]:
            fail_once["armed"] = False
            raise RuntimeError("db blip")
        return await real_persist(**kwargs)

    monkeypatch.setattr(svc, "_persist_completion", flaky_persist)
    created = await _create()
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="pm_card_visa", idempotency_key="key_try_1",
        )
    assert ei.value.code == "acp_completion_persist_failed"

    # Stale resume whose pre-dispatch work fails (gates refuse: lane switched
    # off mid-incident — the exact operator action review B1 called out).
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == created.session_id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    monkeypatch.setattr(settings, "agent_acp_test_capture", False, raising=False)
    with pytest.raises(svc.AcpCheckoutSessionError):
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="pm_card_visa", idempotency_key="key_try_2",
        )
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "completing"  # charge exists — claim must be HELD

    # Lane back on → stale resume replays the SAME key and converges.
    monkeypatch.setattr(settings, "agent_acp_test_capture", True, raising=False)
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == created.session_id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    )
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa", idempotency_key="key_try_3",
    )
    assert out["status"] == "completed"
    derived = f"acp_complete:{created.session_id}:a1"
    assert keys == [derived, derived]
    row = await svc.peek_session(created.session_id)
    assert row["capture_attempt"] == 1


async def test_adyen_refused_is_not_classified_definitive():
    # Review B2: the Adyen adapter emits adyen_refused for resultCodes that
    # include Received/Pending/PartiallyAuthorised — outcomes where money may
    # have moved. Until the adapter narrows to resultCode=="Refused" (proven by
    # a canary), an adyen_refused failure must HOLD the claim (ambiguous).
    assert "adyen_refused" not in svc._DEFINITIVE_DECLINE_ERROR_CODES
    assert not svc._capture_failure_is_definitive(_failed_outcome("adyen_refused"))


async def test_address_without_recipient_name_is_refused_without_buyer_name(
    fake_quote, monkeypatch
):
    # Round 2 of the fabrication sweep: no more invented "Customer" recipient.
    # An address with no name and no buyer first/last to backfill from -> 400.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    addr = _address()
    del addr["name"]
    created = await _create(fulfillment_address=addr)
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
        )
    assert ei.value.code == "acp_address_required"
    assert "name" in str(ei.value)
    assert (calls["order_create"], calls["capture"]) == (0, 0)
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "ready_for_payment"  # pre-claim, zero side effects


async def test_address_name_backfills_from_the_buyers_real_name(fake_quote, monkeypatch):
    # A missing address-level name backfilled from buyer first+last is the
    # buyer's REAL name, not an invention -- allowed.
    _arm_capture_lane(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    addr = _address()
    del addr["name"]
    created = await _create(
        fulfillment_address=addr,
        buyer={"email": "buyer@example.com", "first_name": "Ada", "last_name": "Lovelace"},
    )
    seen = {}

    real_require = svc._require_fulfillment_address

    def spy_require(session):
        out = real_require(session)
        seen["name"] = out.get("name")
        return out

    monkeypatch.setattr(svc, "_require_fulfillment_address", spy_require)
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
    )
    assert out["status"] == "completed"
    assert seen["name"] == "Ada Lovelace"


async def test_explicit_address_name_is_preserved_over_buyer_name(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create(
        buyer={"email": "buyer@example.com", "first_name": "Ada", "last_name": "Lovelace"},
    )
    seen = {}
    real_require = svc._require_fulfillment_address

    def spy_require(session):
        out = real_require(session)
        seen["name"] = out.get("name")
        return out

    monkeypatch.setattr(svc, "_require_fulfillment_address", spy_require)
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(), payment_token="pm_card_visa",
    )
    assert out["status"] == "completed"
    assert seen["name"] == "Real Buyer"  # the address's own name wins


# =============================================================================
# Delegated-token (`vt_`) allowance enforcement — PR-A of the P1 design.
#
# The gate is EXACTLY `startswith("vt_")` and the whole lane sits behind
# ACP_DELEGATE_ALLOWANCE_REGISTRY_ENABLED (default OFF). The six pre-claim
# checks run in a CONTRACT order (existence → session → merchant → currency →
# amount → expiry) and every refusal is a 422 with a `param`, decided BEFORE the
# claim so nothing is ordered or charged. Consumption is a single-use CAS taken
# at claim time.
# =============================================================================


def _arm_allowance_registry(monkeypatch, enabled=True):
    monkeypatch.setattr(
        settings, "acp_delegate_allowance_registry_enabled", enabled, raising=False
    )


async def _mint_allowance(**overrides):
    """An allowance that MATCHES the session the tests create: 4599 USD (the
    fake quote's total), merchant merch_x."""
    kwargs = dict(
        merchant_id="merch_x",
        max_amount=4599,
        currency="USD",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=900),
    )
    kwargs.update(overrides)
    return await reg.mint_allowance(**kwargs)


async def _force_expiry(token_id, when):
    await database.execute(
        acp_delegate_allowances.update()
        .where(acp_delegate_allowances.c.token_id == token_id)
        .values(expires_at=when)
    )


def _spy_get_allowance(monkeypatch):
    """Record every registry lookup, so a lane that must not consult the
    registry at all can be pinned as such."""
    seen: List[Any] = []
    real = reg.get_allowance

    async def spy(token_id):
        seen.append(token_id)
        return await real(token_id)

    monkeypatch.setattr(reg, "get_allowance", spy)
    return seen


# --- flag OFF preserves today's behavior byte-for-byte -----------------------


async def test_flag_off_never_touches_the_registry_for_a_vt_token(fake_quote, monkeypatch):
    # Default posture: a vt_ token is not looked up at all. It behaves exactly as
    # it does today — it reaches capture, where the test lane substitutes the
    # test PM (and the live lane refuses it). PR-B changes what happens there;
    # this pins what happens BEFORE that change.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch, enabled=False)
    _mock_order_layer(monkeypatch)
    seen = _spy_get_allowance(monkeypatch)
    created = await _create()

    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(), payment_token="vt_unknown0000",
    )
    assert out["status"] == "completed"
    assert seen == []  # no registry lookup happened at all


async def test_flag_off_does_not_consume_an_allowance(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch, enabled=False)
    _mock_order_layer(monkeypatch)
    minted = await _mint_allowance(checkout_session_id="csn_placeholder")
    created = await _create()
    await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token=minted["token_id"],
    )
    stored = await reg.get_allowance(minted["token_id"])
    assert stored["used"] is False  # the CAS never ran


# --- flag ON: the six pre-claim checks, in contract order --------------------


async def test_unknown_vt_token_is_invalid_token_422(fake_quote, monkeypatch):
    # Founder-confirmed (design Q2): a vt_ we have no record of REFUSES. We
    # never charge a delegated token whose allowance we cannot verify.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="vt_00000000000000",
        )
    assert ei.value.code == "invalid_token"
    assert ei.value.status_code == 422
    assert ei.value.message == "delegate token not found"  # verbatim wire parity
    assert ei.value.extra["param"] == "payment_data.token"
    # Pre-claim: zero side effects.
    assert (calls["order_create"], calls["capture"], calls["settle"]) == (0, 0, 0)
    assert (await svc.peek_session(created.session_id))["status"] == "ready_for_payment"


async def test_allowance_for_another_session_is_session_mismatch_422(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(checkout_session_id="csn_some_other_one")

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        )
    assert ei.value.code == "allowance_session_mismatch"
    assert ei.value.status_code == 422
    assert ei.value.message == "delegate token not authorized for this session"
    assert ei.value.extra["param"] == "allowance.checkout_session_id"
    assert (calls["order_create"], calls["capture"]) == (0, 0)
    # A refused allowance is NOT consumed.
    assert (await reg.get_allowance(minted["token_id"]))["used"] is False


async def test_allowance_for_another_merchant_is_merchant_mismatch_422(fake_quote, monkeypatch):
    # NEW tightening: the retired service never checked merchant scope, so a
    # token minted for merchant A was spendable at merchant B.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(
        checkout_session_id=created.session_id, merchant_id="merch_someone_else"
    )

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        )
    assert ei.value.code == "allowance_merchant_mismatch"
    assert ei.value.status_code == 422
    assert ei.value.extra["param"] == "allowance.merchant_id"
    assert (calls["order_create"], calls["capture"]) == (0, 0)


async def test_currency_mismatch_is_422(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(
        checkout_session_id=created.session_id, currency="EUR"
    )

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        )
    assert ei.value.code == "allowance_currency_mismatch"
    assert ei.value.status_code == 422
    assert ei.value.message == "currency mismatch"
    assert ei.value.extra["param"] == "allowance.currency"
    assert (calls["order_create"], calls["capture"]) == (0, 0)


async def test_currency_comparison_is_case_insensitive_on_both_sides(fake_quote, monkeypatch):
    # The session records "USD"; a lowercase allowance currency is the SAME
    # currency, not a mismatch.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(
        checkout_session_id=created.session_id, currency="usd"
    )
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token=minted["token_id"],
    )
    assert out["status"] == "completed"


async def test_total_over_allowance_is_allowance_exceeded_422(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()  # session total is 4599 cents
    minted = await _mint_allowance(
        checkout_session_id=created.session_id, max_amount=4598
    )

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        )
    assert ei.value.code == "allowance_exceeded"
    assert ei.value.status_code == 422
    assert ei.value.message == "total exceeds allowance"
    assert ei.value.extra["param"] == "allowance.max_amount"
    assert (calls["order_create"], calls["capture"]) == (0, 0)


async def test_total_exactly_equal_to_the_allowance_passes(fake_quote, monkeypatch):
    # Wire parity: the refusal is `total > max_amount`, so equality PASSES.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(
        checkout_session_id=created.session_id, max_amount=4599
    )
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token=minted["token_id"],
    )
    assert out["status"] == "completed"


async def test_expired_allowance_is_422(fake_quote, monkeypatch):
    # NEW tightening: the retired service stored expires_at and never read it.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(checkout_session_id=created.session_id)
    await _force_expiry(
        minted["token_id"], datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        )
    assert ei.value.code == "allowance_expired"
    assert ei.value.status_code == 422
    assert ei.value.extra["param"] == "allowance.expires_at"
    assert (calls["order_create"], calls["capture"]) == (0, 0)
    # Still unconsumed — an expired token was never spent.
    assert (await reg.get_allowance(minted["token_id"]))["used"] is False


# --- the check ORDER is the contract -----------------------------------------


async def test_check_order_session_beats_merchant_currency_amount_and_expiry(
    fake_quote, monkeypatch
):
    # An allowance wrong in EVERY dimension reports the FIRST failing check in
    # contract order, not the most severe one.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(
        checkout_session_id="csn_other",
        merchant_id="merch_other",
        currency="EUR",
        max_amount=1,
    )
    await _force_expiry(
        minted["token_id"], datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        )
    assert ei.value.code == "allowance_session_mismatch"


async def test_check_order_merchant_beats_currency_amount_and_expiry(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(
        checkout_session_id=created.session_id,
        merchant_id="merch_other",
        currency="EUR",
        max_amount=1,
    )
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        )
    assert ei.value.code == "allowance_merchant_mismatch"


async def test_check_order_currency_beats_amount_and_expiry(fake_quote, monkeypatch):
    # The retired service's relative order (currency BEFORE amount) is preserved.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(
        checkout_session_id=created.session_id, currency="EUR", max_amount=1
    )
    await _force_expiry(
        minted["token_id"], datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        )
    assert ei.value.code == "allowance_currency_mismatch"


async def test_check_order_amount_beats_expiry(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(
        checkout_session_id=created.session_id, max_amount=1
    )
    await _force_expiry(
        minted["token_id"], datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        )
    assert ei.value.code == "allowance_exceeded"


# --- no-oracle ordering: an allowance error never precedes a lane refusal ----


async def test_dark_kill_switch_beats_invalid_token(fake_quote, monkeypatch):
    # A dark lane must stay dark: an unknown-token 422 would tell a caller the
    # completion path is live and merely mis-tokened.
    from fastapi import HTTPException

    _arm_capture_lane(monkeypatch, submit=False)
    _arm_allowance_registry(monkeypatch)
    seen = _spy_get_allowance(monkeypatch)
    created = await _create()
    with pytest.raises(HTTPException) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="vt_00000000000000",
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "TIER2_CHARGE_DISABLED"
    assert seen == []  # the registry was never even consulted


async def test_agent_mismatch_beats_invalid_token(fake_quote, monkeypatch):
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    seen = _spy_get_allowance(monkeypatch)
    created = await _create()

    class _CtxA:
        agent_id = "agent_a"

        def can_access_merchant(self, merchant_id):
            return True

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_CtxA(),
            payment_token="vt_00000000000000",
        )
    assert ei.value.code == "acp_agent_mismatch"
    assert seen == []


async def test_address_refusal_beats_invalid_token(fake_quote, monkeypatch):
    # Fulfillment identity is checked before the allowance — both are pre-claim,
    # so the ordering is only about which refusal a caller sees first, and the
    # established one wins.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    created = await _create()
    await _strip(created.session_id, fulfillment_address=None)
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="vt_00000000000000",
        )
    assert ei.value.code == "acp_address_required"


# --- single-use consumption at claim time ------------------------------------


async def test_a_valid_vt_token_is_consumed_by_the_completing_session(
    fake_quote, monkeypatch
):
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(checkout_session_id=created.session_id)

    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token=minted["token_id"],
    )
    assert out["status"] == "completed"
    stored = await reg.get_allowance(minted["token_id"])
    assert stored["used"] is True
    assert stored["used_by_session"] == created.session_id
    assert stored["used_at"] is not None


async def test_a_token_bound_to_another_session_refuses_and_releases_the_fresh_claim(
    fake_quote, monkeypatch
):
    # The bind happens on a FRESH claim, pre-dispatch: refusing must hand the
    # session back (status ready_for_payment) rather than wedge it.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(checkout_session_id=created.session_id)
    # Someone else already spent it.
    assert await reg.bind_allowance_to_session(
        token_id=minted["token_id"], session_id="csn_thief"
    ) is True

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        )
    assert ei.value.code == "allowance_already_used"
    assert ei.value.status_code == 422
    assert ei.value.extra["param"] == "payment_data.token"
    # Bound at claim time, BEFORE any order/charge work.
    assert (calls["order_create"], calls["capture"], calls["settle"]) == (0, 0, 0)
    # The fresh claim was released.
    assert (await svc.peek_session(created.session_id))["status"] == "ready_for_payment"
    # ...and the thief still holds the token.
    assert (await reg.get_allowance(minted["token_id"]))["used_by_session"] == "csn_thief"


async def test_a_resumed_claim_cannot_be_refused_by_its_own_bind(fake_quote, monkeypatch):
    # The wedge test (review B1): a session that already bound its token and
    # then hit an ambiguous failure MUST be able to resume. If the CAS refused a
    # re-bind by the same session, the resume would 422 forever while a charge
    # sat in flight under the stored PSP key.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(checkout_session_id=created.session_id)
    assert await reg.bind_allowance_to_session(
        token_id=minted["token_id"], session_id=created.session_id
    ) is True
    # A stale `completing` row is exactly what an ambiguous failure leaves.
    await _wedge_completing(created.session_id, order_id="ord_acp_1")

    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token=minted["token_id"],
    )
    assert out["status"] == "completed"
    assert (await reg.get_allowance(minted["token_id"]))["used_by_session"] == (
        created.session_id
    )


async def test_two_concurrent_sessions_presenting_one_token_only_its_own_completes(
    fake_quote, monkeypatch
):
    # Layered defense, end to end: the SESSION-scope check separates the two
    # racers pre-claim (the outer layer), and the CAS is the backstop for
    # anything that ever gets past it — its own single-flight behavior is pinned
    # in tests/test_acp_delegate_allowance_service.py (asyncio.gather) and on the
    # production dialect in tests/test_acp_delegate_allowances_postgres.py.
    import asyncio

    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    a = await _create()
    b = await _create()
    minted = await _mint_allowance(checkout_session_id=a.session_id)

    ok, refused = await asyncio.gather(
        svc.complete_session(
            session_id=a.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        ),
        svc.complete_session(
            session_id=b.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        ),
        return_exceptions=True,
    )
    assert isinstance(ok, dict) and ok["status"] == "completed"
    assert isinstance(refused, svc.AcpCheckoutSessionError)
    # B is refused by the SESSION scope check (pre-claim) — the earlier gate.
    assert refused.code == "allowance_session_mismatch"


# --- pm_ tokens are untouched in both flag states ----------------------------


async def test_pm_token_never_consults_the_registry_with_the_flag_on(
    fake_quote, monkeypatch
):
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    seen = _spy_get_allowance(monkeypatch)
    created = await _create()
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa",
    )
    assert out["status"] == "completed"
    assert seen == []


async def test_a_future_spt_token_is_not_a_delegate_token(fake_quote, monkeypatch):
    # The gate is EXACTLY startswith("vt_"): `spt_` gets its own lane in PR-B and
    # must not be swept into the registry now.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    seen = _spy_get_allowance(monkeypatch)
    created = await _create()
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="spt_something",
    )
    assert out["status"] == "completed"
    assert seen == []


# --- what a VALID vt_ does today (the line PR-B moves) -----------------------


async def test_valid_vt_token_completes_via_the_test_pm_today(fake_quote, monkeypatch):
    # A vt_ that passes every allowance check still reaches capture, where TODAY
    # the Stripe adapter substitutes the test PM on the test lane (and would
    # refuse it on the live lane). That is correct for PR-A: the registry decides
    # whether the token MAY be spent, not how it is charged. PR-B replaces this
    # substitution with a real spt_ charge — pinned here so that change is
    # visible in the diff instead of silent.
    from services.acp_offsession_capture import DEFAULT_TEST_PAYMENT_METHOD

    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    minted = await _mint_allowance(checkout_session_id=created.session_id)

    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token=minted["token_id"],
    )
    assert out["status"] == "completed"
    # The vt_ token is what the capture layer was handed...
    assert calls["capture_kwargs"]["payment_method_token"] == minted["token_id"]
    # ...and the test-lane adapter is what turns a non-pm_ into the test PM.
    assert DEFAULT_TEST_PAYMENT_METHOD == "pm_card_visa"


# --- resume-path enforcement (Opus review: the claim decides which case) ------
#
# Consumption is CLAIM-SCOPED. A `completing` row is reachable with no crash at
# all — one ambiguous capture does it — so "resumed" must never mean "skip the
# checks". The two variants below are the exploitable shapes the review found.


async def _age_row(session_id, *, seconds=120):
    """Push updated_at back out of the stale window so the row is resumable
    again (a takeover moves it forward, so a second resume needs re-aging)."""
    await database.execute(
        acp_checkout_sessions.update()
        .where(acp_checkout_sessions.c.id == session_id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=seconds))
    )


# Variant A — RESUMED claim WITH an order: a foreign token is INERT.


async def test_resume_with_an_order_never_consumes_a_foreign_allowance(
    fake_quote, monkeypatch
):
    # THE variant-A regression. An ambiguous capture leaves completing+order_id.
    # A resume presenting a wholly-invalid vt_ (minted for another session, wrong
    # merchant, over the cap, expired) must NOT bind it: the capture replays the
    # STORED PSP key, so the caller's token cannot affect what is charged, and
    # consuming it would bind a foreign allowance to a session it was never
    # minted for.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    foreign = await _mint_allowance(
        checkout_session_id="csn_not_this_one",
        merchant_id="merch_someone_else",
        max_amount=1,
    )
    await _force_expiry(
        foreign["token_id"], datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    await _wedge_completing(created.session_id, order_id="ord_acp_1")

    keys: list = []
    _spy_capture(monkeypatch, keys)
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token=foreign["token_id"],
    )
    # The resume converges (refusing would strand a charge that may be in
    # flight) on the STORED key, and reuses the existing order.
    assert out["status"] == "completed"
    assert keys == [f"acp_complete:{created.session_id}:a1"]
    assert calls["order_create"] == 0
    # ...and the foreign allowance is untouched: not used, not bound, no
    # used_at watermark.
    stored = await reg.get_allowance(foreign["token_id"])
    assert stored["used"] is False
    assert stored["used_by_session"] is None
    assert stored["used_at"] is None


async def test_resume_with_an_order_ignores_an_unknown_token_rather_than_refusing(
    fake_quote, monkeypatch
):
    # The documented decision for the inert-token case: IGNORED, not refused.
    # An unknown vt_ on a replay must not 422 a session whose charge may already
    # be in flight.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    await _wedge_completing(created.session_id, order_id="ord_acp_1")

    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="vt_00000000000000",
    )
    assert out["status"] == "completed"


async def test_resume_with_an_order_rebinds_only_its_own_token(fake_quote, monkeypatch):
    # The positive half: this session's OWN bound token is re-bound (idempotent),
    # and a token bound by ANOTHER session is neither stolen nor fatal.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    mine = await _mint_allowance(checkout_session_id=created.session_id)
    theirs = await _mint_allowance(checkout_session_id=created.session_id)
    assert await reg.bind_allowance_to_session(
        token_id=mine["token_id"], session_id=created.session_id
    ) is True
    assert await reg.bind_allowance_to_session(
        token_id=theirs["token_id"], session_id="csn_someone_else"
    ) is True
    await _wedge_completing(created.session_id, order_id="ord_acp_1")

    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token=theirs["token_id"],
    )
    assert out["status"] == "completed"
    # The other session still holds its token — nothing was taken from it.
    assert (await reg.get_allowance(theirs["token_id"]))["used_by_session"] == (
        "csn_someone_else"
    )
    # ...and ours is untouched too (it stays bound to us).
    assert (await reg.get_allowance(mine["token_id"]))["used_by_session"] == (
        created.session_id
    )


# Variant B — RESUMED claim with order_id NULL: full 1–6 enforcement.


async def test_resume_without_an_order_refuses_an_unknown_token_and_holds_the_claim(
    fake_quote, monkeypatch
):
    # THE variant-B regression. order_id IS NULL is the claim→order-create crash
    # window: capture is only reachable after _persist_order_id succeeds, so
    # NOTHING was ever dispatched. This resume does genuinely FRESH order-create
    # and charge work, and gets the full check set. Refusing strands nothing.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    await _wedge_completing(created.session_id, order_id=None)

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="vt_00000000000000",
        )
    assert ei.value.code == "invalid_token"
    assert ei.value.status_code == 422
    assert ei.value.extra["param"] == "payment_data.token"
    # No fresh order, no charge...
    assert (calls["order_create"], calls["capture"], calls["settle"]) == (0, 0, 0)
    # ...and the RESUMED claim is HELD (review B1): the row stays `completing`
    # with its stored key, so nothing is released for a new attempt to re-key.
    row = await svc.peek_session(created.session_id)
    assert row["status"] == "completing"
    assert row["psp_idempotency_key"] == f"acp_complete:{created.session_id}:a1"
    assert row["capture_attempt"] == 1


async def test_resume_without_an_order_refuses_an_over_cap_allowance(
    fake_quote, monkeypatch
):
    # A second of the six codes reachable on this path — the checks are the
    # SAME set as a fresh completion, not a subset.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()  # session total 4599
    minted = await _mint_allowance(
        checkout_session_id=created.session_id, max_amount=4598
    )
    await _wedge_completing(created.session_id, order_id=None)

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=minted["token_id"],
        )
    assert ei.value.code == "allowance_exceeded"
    assert ei.value.status_code == 422
    assert (calls["order_create"], calls["capture"]) == (0, 0)
    assert (await svc.peek_session(created.session_id))["status"] == "completing"
    # Refused → never consumed.
    assert (await reg.get_allowance(minted["token_id"]))["used"] is False


async def test_resume_without_an_order_refuses_a_foreign_session_allowance(
    fake_quote, monkeypatch
):
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    foreign = await _mint_allowance(checkout_session_id="csn_not_this_one")
    await _wedge_completing(created.session_id, order_id=None)

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token=foreign["token_id"],
        )
    assert ei.value.code == "allowance_session_mismatch"
    assert (calls["order_create"], calls["capture"]) == (0, 0)
    assert (await reg.get_allowance(foreign["token_id"]))["used"] is False


async def test_a_held_claim_still_converges_on_a_later_valid_token(
    fake_quote, monkeypatch
):
    # The other direction of B1: HOLDING the claim on a 422 must not wedge the
    # session. A later resume presenting a VALID token converges the same row —
    # same attempt, same stored PSP key, and the allowance is consumed then.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    calls = _mock_order_layer(monkeypatch)
    created = await _create()
    await _wedge_completing(created.session_id, order_id=None)

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc.complete_session(
            session_id=created.session_id, agent_context=_Ctx(),
            payment_token="vt_00000000000000",
        )
    assert ei.value.code == "invalid_token"

    # A takeover moved updated_at forward; age it back so the row is resumable.
    await _age_row(created.session_id)
    minted = await _mint_allowance(checkout_session_id=created.session_id)
    keys: list = []
    _spy_capture(monkeypatch, keys)
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token=minted["token_id"],
    )
    assert out["status"] == "completed"
    assert calls["order_create"] == 1  # the fresh order this resume owed
    # The attempt was never re-keyed by the refusal.
    assert keys == [f"acp_complete:{created.session_id}:a1"]
    stored = await reg.get_allowance(minted["token_id"])
    assert stored["used"] is True
    assert stored["used_by_session"] == created.session_id


async def test_resume_without_an_order_is_unaffected_when_the_flag_is_off(
    fake_quote, monkeypatch
):
    # Flag OFF stays byte-for-byte today's behavior on the resume path too.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch, enabled=False)
    calls = _mock_order_layer(monkeypatch)
    seen = _spy_get_allowance(monkeypatch)
    created = await _create()
    await _wedge_completing(created.session_id, order_id=None)
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token="vt_00000000000000",
    )
    assert out["status"] == "completed"
    assert calls["order_create"] == 1
    assert seen == []


async def test_pm_token_resume_paths_never_consult_the_registry(fake_quote, monkeypatch):
    # Neither resume case may drag a pm_ token into the registry.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    seen = _spy_get_allowance(monkeypatch)

    with_order = await _create()
    await _wedge_completing(with_order.session_id, order_id="ord_acp_1")
    assert (await svc.complete_session(
        session_id=with_order.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa",
    ))["status"] == "completed"

    without_order = await _create()
    await _wedge_completing(without_order.session_id, order_id=None)
    assert (await svc.complete_session(
        session_id=without_order.session_id, agent_context=_Ctx(),
        payment_token="pm_card_visa",
    ))["status"] == "completed"

    assert seen == []


async def test_resume_replays_with_this_sessions_own_token_not_the_callers(
    fake_quote, monkeypatch
):
    # Review follow-up: the caller's token is inert for BINDING but NOT for the
    # PSP call — a PSP keys idempotency on the whole parameter set, so replaying
    # the stored key with a DIFFERENT token turns a clean retry into an
    # idempotency error (which classifies AMBIGUOUS and re-holds the claim; a
    # caller repeating that every <60s would wedge the row until its TTL).
    # So the replay is made parameter-identical: the session's OWN bound token.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    own = await _mint_allowance(
        checkout_session_id=created.session_id, merchant_id="merch_x", max_amount=100000
    )
    # The session legitimately consumed its own token on the first attempt.
    assert await reg.bind_allowance_to_session(
        token_id=own["token_id"], session_id=created.session_id
    )
    foreign = await _mint_allowance(
        checkout_session_id="csn_elsewhere", merchant_id="merch_someone_else"
    )
    await _wedge_completing(created.session_id, order_id="ord_acp_1")

    seen: list = []
    orig_execute = offpay.execute_acp_offsession_payment

    async def spy(**kwargs):
        seen.append((kwargs["idempotency_key"], kwargs.get("payment_method_token")))
        return await orig_execute(**kwargs)

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", spy)

    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token=foreign["token_id"],  # junk/foreign token on the replay
    )
    assert out["status"] == "completed"
    # The capture replayed the stored key WITH THIS SESSION'S OWN TOKEN — never
    # the caller's, so the retry is parameter-identical to the real dispatch.
    assert seen == [(f"acp_complete:{created.session_id}:a1", own["token_id"])]
    # ...and the foreign allowance is still untouched.
    stored = await reg.get_allowance(foreign["token_id"])
    assert stored["used"] is False and stored["used_by_session"] is None


async def test_resume_without_a_bound_token_forwards_no_token(fake_quote, monkeypatch):
    # A session that has an order but never bound a delegate token (e.g. its
    # first attempt used a pm_): a junk vt_ on the replay must not be forwarded
    # either — None keeps the replay closest to a parameter-identical retry.
    _arm_capture_lane(monkeypatch)
    _arm_allowance_registry(monkeypatch)
    _mock_order_layer(monkeypatch)
    created = await _create()
    foreign = await _mint_allowance(
        checkout_session_id="csn_elsewhere", merchant_id="merch_someone_else"
    )
    await _wedge_completing(created.session_id, order_id="ord_acp_1")

    seen: list = []
    orig_execute = offpay.execute_acp_offsession_payment

    async def spy(**kwargs):
        seen.append(kwargs.get("payment_method_token"))
        return await orig_execute(**kwargs)

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", spy)
    out = await svc.complete_session(
        session_id=created.session_id, agent_context=_Ctx(),
        payment_token=foreign["token_id"],
    )
    assert out["status"] == "completed"
    assert seen == [None]
    stored = await reg.get_allowance(foreign["token_id"])
    assert stored["used"] is False
