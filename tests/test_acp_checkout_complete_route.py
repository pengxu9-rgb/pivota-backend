"""POST /agent/v1/checkout/acp/{session_id}/complete — the in-process ACP
completion door.

Money-path route tests: the dark kill-switch 403 is THE positive control that
dark stays dark end-to-end through the route; the happy path runs with a mocked
capture layer; over-cap refuses in the gate's own shape; token precedence
mirrors pivota-acp's extract_payment_token (payment_data.token wins).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from db.acp_checkout_sessions import acp_checkout_sessions  # noqa: E402
from db.database import database, engine, metadata  # noqa: E402
from config.settings import settings  # noqa: E402
from services import acp_checkout_session_service as svc  # noqa: E402
from services import acp_offsession_payment as offpay  # noqa: E402


class _Ctx:
    agent_id = "agent_test"

    def __init__(self, allowed=True):
        self._allowed = allowed

    def can_access_merchant(self, merchant_id):
        return self._allowed


@pytest.fixture(autouse=True)
async def _db():
    metadata.create_all(engine, tables=[acp_checkout_sessions])
    if not database.is_connected:
        await database.connect()
    await database.execute(acp_checkout_sessions.delete())
    yield


async def _seed_session(session_id="csn_route_test01", merchant_id="merch_x"):
    now = datetime.now(timezone.utc)
    await database.execute(
        acp_checkout_sessions.insert().values(
            id=session_id,
            merchant_id=merchant_id,
            platform="shopify",
            status="ready_for_payment",
            items=[{"product_id": "p1", "variant_id": "v1", "quantity": 1}],
            metadata={"pvt_click_id": "clk_abc", "protocol_name": "acp"},
            currency="USD",
            total_cents=169,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=3600),
        )
    )
    return session_id


def _arm(monkeypatch, *, submit=True, test_capture=True):
    monkeypatch.setattr(settings, "agent_checkout_strict", True, raising=False)
    monkeypatch.setattr(settings, "agent_submit_payment_enabled", submit, raising=False)
    monkeypatch.setattr(settings, "agent_submit_payment_merchants", frozenset(), raising=False)
    monkeypatch.setattr(settings, "agent_acp_test_capture", test_capture, raising=False)
    monkeypatch.setattr(settings, "agent_acp_test_max_cents", 500, raising=False)
    monkeypatch.setattr(settings, "agent_acp_allow_live_capture", False, raising=False)


class _FakeQuoteService:
    async def preview_quote(self, **kwargs):
        return {
            "quote_id": "q_route",
            "currency": "USD",
            "pricing": {"subtotal": "1.00", "discount_total": "0.00",
                        "shipping_fee": "0.50", "tax": "0.19", "total": "1.69"},
            "line_items": [{"product_id": "p1", "variant_id": "v1", "quantity": 1,
                            "unit_price_effective": "1.00"}],
            "delivery_options": [],
        }


def _mock_layers(monkeypatch, *, order_total=1.69, capture_success=True):
    calls = {"order_create": 0, "capture": 0, "settle": 0, "capture_kwargs": None}

    monkeypatch.setattr(svc, "QuoteService", _FakeQuoteService)

    async def fake_create_order(**kwargs):
        calls["order_create"] += 1
        return "ord_route_1"

    monkeypatch.setattr(svc, "_create_pivota_order", fake_create_order)

    import db.orders as orders_mod

    async def fake_get_order(oid):
        return {
            "order_id": oid, "merchant_id": "merch_x", "total_amount": order_total,
            "currency": "USD", "status": "created", "payment_status": "pending",
            "metadata": {"protocol_name": "acp", "checkout_session_id": "csn_route_test01"},
        }

    monkeypatch.setattr(orders_mod, "get_order", fake_get_order)

    async def fake_execute(**kwargs):
        calls["capture"] += 1
        calls["capture_kwargs"] = kwargs
        return offpay.AcpOffsessionPaymentOutcome(
            success=capture_success,
            status=("succeeded" if capture_success else "failed"),
            payment_intent_id="pi_route",
            psp_used="stripe",
            error=(None if capture_success else "card_declined"),
            error_code=(None if capture_success else "card_declined"),
            payment_intent=SimpleNamespace(
                id="pi_route",
                status=("succeeded" if capture_success else "failed"),
                client_secret=None,
            ),
        )

    async def fake_settle(**kwargs):
        calls["settle"] += 1

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", fake_execute)
    monkeypatch.setattr(offpay, "settle_acp_offsession_success", fake_settle)
    return calls


async def test_dark_kill_switch_is_403_and_side_effect_free(monkeypatch):
    # Default fail-closed posture (strict ON, submit OFF): the completion door
    # must refuse in the kill-switch's own error shape and touch NOTHING.
    from fastapi import HTTPException
    from routes import agent_checkout_intents as m

    _arm(monkeypatch, submit=False)
    calls = _mock_layers(monkeypatch)
    sid = await _seed_session()

    with pytest.raises(HTTPException) as ei:
        await m.complete_acp_checkout(sid, m.CompleteAcpCheckoutRequest(), _Ctx())
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "TIER2_CHARGE_DISABLED"
    assert calls["order_create"] == 0 and calls["capture"] == 0

    row = await svc.peek_session(sid)
    assert row["status"] == "ready_for_payment"


async def test_happy_path_with_test_capture_enabled(monkeypatch):
    from routes import agent_checkout_intents as m

    _arm(monkeypatch)
    calls = _mock_layers(monkeypatch)
    sid = await _seed_session()

    out = await m.complete_acp_checkout(
        sid,
        m.CompleteAcpCheckoutRequest(payment_data={"token": "pm_card_visa"}),
        _Ctx(),
    )
    assert out["status"] == "completed"
    assert out["payment_status"] == "succeeded"
    assert out["order"]["id"] == "ord_route_1"
    assert calls == {**calls, "order_create": 1, "capture": 1, "settle": 1}
    # payment_data.token wins and is forwarded to the capture layer.
    assert calls["capture_kwargs"]["payment_method_token"] == "pm_card_visa"


async def test_flat_payment_token_is_accepted(monkeypatch):
    from routes import agent_checkout_intents as m

    _arm(monkeypatch)
    calls = _mock_layers(monkeypatch)
    sid = await _seed_session()
    out = await m.complete_acp_checkout(
        sid, m.CompleteAcpCheckoutRequest(payment_token="pm_flat"), _Ctx()
    )
    assert out["status"] == "completed"
    assert calls["capture_kwargs"]["payment_method_token"] == "pm_flat"


async def test_payment_data_token_precedence(monkeypatch):
    from routes import agent_checkout_intents as m

    _arm(monkeypatch)
    calls = _mock_layers(monkeypatch)
    sid = await _seed_session()
    await m.complete_acp_checkout(
        sid,
        m.CompleteAcpCheckoutRequest(payment_data={"token": "pm_wins"}, payment_token="pm_loses"),
        _Ctx(),
    )
    assert calls["capture_kwargs"]["payment_method_token"] == "pm_wins"


async def test_over_cap_refusal(monkeypatch):
    from fastapi import HTTPException
    from routes import agent_checkout_intents as m

    _arm(monkeypatch)  # cap 500 cents
    calls = _mock_layers(monkeypatch, order_total=9.99)  # 999 cents
    sid = await _seed_session()
    with pytest.raises(HTTPException) as ei:
        await m.complete_acp_checkout(sid, m.CompleteAcpCheckoutRequest(), _Ctx())
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "TIER2_TEST_CAPTURE_OVER_CAP"
    assert calls["capture"] == 0


async def test_capture_failure_is_502_named_error(monkeypatch):
    from fastapi import HTTPException
    from routes import agent_checkout_intents as m

    _arm(monkeypatch)
    calls = _mock_layers(monkeypatch, capture_success=False)
    sid = await _seed_session()
    with pytest.raises(HTTPException) as ei:
        await m.complete_acp_checkout(sid, m.CompleteAcpCheckoutRequest(), _Ctx())
    assert ei.value.status_code == 502
    assert ei.value.detail["code"] == "acp_capture_failed"
    assert calls["settle"] == 0
    row = await svc.peek_session(sid)
    assert row["status"] == "ready_for_payment"  # never marked completed


async def test_unknown_session_is_404(monkeypatch):
    from fastapi import HTTPException
    from routes import agent_checkout_intents as m

    with pytest.raises(HTTPException) as ei:
        await m.complete_acp_checkout("csn_missing", m.CompleteAcpCheckoutRequest(), _Ctx())
    assert ei.value.status_code == 404
    assert ei.value.detail["code"] == "acp_session_not_found"


async def test_foreign_merchant_is_403(monkeypatch):
    from fastapi import HTTPException
    from routes import agent_checkout_intents as m

    sid = await _seed_session()
    with pytest.raises(HTTPException) as ei:
        await m.complete_acp_checkout(sid, m.CompleteAcpCheckoutRequest(), _Ctx(allowed=False))
    assert ei.value.status_code == 403
