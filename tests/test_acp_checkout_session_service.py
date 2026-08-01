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
from db.database import database, engine, metadata  # noqa: E402
from services import acp_checkout_session_service as svc  # noqa: E402
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
    await database.execute(acp_checkout_sessions.delete())
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


async def _create(**overrides):
    kwargs = dict(
        merchant_id="merch_x",
        platform="shopify",
        items=_items(),
        metadata={"pvt_click_id": "clk_abc", "pvt_surface": "chatgpt"},
        buyer={"email": "buyer@example.com"},
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
    calls = {"order_create": 0, "capture": 0, "settle": 0}

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
    assert calls == {"order_create": 1, "capture": 1, "settle": 1}
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
    assert calls == {"order_create": 0, "capture": 0, "settle": 0}
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


def test_no_simulation_fallback_exists():
    # The old service's simulated capture (fake `payment_captured` webhook) must
    # NOT be ported: a failed capture is an error, never a pretend success.
    # Skip the module docstring (which documents WHY the fallback is absent) —
    # the assertion is about executable code paths.
    src = inspect.getsource(svc).split('"""', 2)[2]
    assert "payment_captured" not in src
    assert "def simulate" not in src and "simulate_" not in src
