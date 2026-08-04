"""F1 regression gate: complete_session drives the REAL agent_create_order.

The in-process call in services/acp_checkout_session_service._create_pivota_order
must pass `agent_user=None, x_buyer_ref=None` explicitly — those parameters'
FastAPI Depends/Header defaults are TRUTHY sentinel objects, and the route body
dereferences `agent_user.agent_user_ref` (routes/agent_api.py, metadata
hydration), so omitting them turns every completion into
AttributeError → 500 → acp_order_create_failed. This test intentionally does
NOT monkeypatch _create_pivota_order or agent_create_order: it stubs the deep
seams instead (order_routes.create_new_order, governance, telemetry — the same
seams tests/test_quote_first_replay_idempotency.py stubs), so the real
route-function wiring executes and a sentinel regression can never pass again.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict

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
    agent_name = "Test Agent"
    allowed_merchants = None

    def can_access_merchant(self, merchant_id):
        return True


@pytest.fixture(autouse=True)
async def _db():
    metadata.create_all(engine, tables=[acp_checkout_sessions])
    if not database.is_connected:
        await database.connect()
    await database.execute(acp_checkout_sessions.delete())
    yield


class _FakeQuoteService:
    async def preview_quote(self, **kwargs):
        return {
            "quote_id": "q_wiring",
            "currency": "USD",
            "pricing": {"subtotal": "1.00", "discount_total": "0.00",
                        "shipping_fee": "0.50", "tax": "0.19", "total": "1.69"},
            "line_items": [{"product_id": "p1", "variant_id": "v1", "quantity": 1,
                            "unit_price_effective": "1.00"}],
            "delivery_options": [],
        }


def _stub_order_create_deep_seams(monkeypatch, created_orders):
    """Stub BENEATH agent_create_order (never the route function itself), the
    way tests/test_quote_first_replay_idempotency.py does."""
    import mvp.events as mvp_events
    import mvp.ledger_events as ledger_events

    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
    monkeypatch.setattr(ledger_events, "emit_best_effort", lambda **_: None)

    import services.agent_governance as governance_module

    async def noop_validate_request(agent_id):
        return None

    async def noop_record_response(agent_id, latency_ms, success):
        return None

    monkeypatch.setattr(governance_module.agent_governance, "validate_request", noop_validate_request)
    monkeypatch.setattr(governance_module.agent_governance, "record_response", noop_record_response)

    import services.agent_webhook_service as agent_webhook_service

    async def noop_emit_agent_webhook_event(*_, **__):
        return None

    monkeypatch.setattr(agent_webhook_service, "emit_agent_webhook_event", noop_emit_agent_webhook_event)

    import routes.agent_auth as agent_auth_module
    import routes.agent_api as agent_api_module

    async def noop_log_agent_request(*_, **__):
        return None

    monkeypatch.setattr(agent_auth_module, "log_agent_request", noop_log_agent_request)
    monkeypatch.setattr(agent_api_module, "log_agent_request", noop_log_agent_request)

    async def fake_get_primary_store(_merchant_id):
        return {"platform": "shopify", "store_id": "store_test"}

    monkeypatch.setattr(agent_api_module, "get_primary_store", fake_get_primary_store)

    from mvp.idempotency import InMemoryIdempotencyStore

    shared_store = InMemoryIdempotencyStore()

    class FakePostgresIdempotencyStore:
        async def get(self, *, scope, key):
            return await shared_store.get(scope=scope, key=key)

        async def put(self, *, scope, key, value):
            return await shared_store.put(scope=scope, key=key, value=value)

    monkeypatch.setattr(agent_api_module, "_AGENT_ORDER_IDEMPOTENCY_STORE", FakePostgresIdempotencyStore())

    import routes.order_routes as order_routes_module
    from models.order import OrderResponse, PaymentAction

    async def fake_create_new_order(order_request, background_tasks):
        created_orders.append(order_request)
        now = datetime.now(timezone.utc)
        return OrderResponse(
            order_id="ord_wiring_1",
            merchant_id=order_request.merchant_id,
            customer_email=order_request.customer_email,
            items=order_request.items,
            shipping_address=order_request.shipping_address,
            subtotal=Decimal("1.00"),
            shipping_fee=Decimal("0.50"),
            tax=Decimal("0.19"),
            total=Decimal("1.69"),
            currency=order_request.currency or "USD",
            status="pending",
            payment_status="unpaid",
            fulfillment_status=None,
            payment_intent_id=None,
            client_secret=None,
            psp=None,
            payment_action=None,
            shopify_order_id=None,
            tracking_number=None,
            created_at=now,
            updated_at=now,
            agent_session_id=order_request.agent_session_id,
            metadata=order_request.metadata,
        )

    monkeypatch.setattr(order_routes_module, "create_new_order", fake_create_new_order)


async def test_complete_session_drives_real_agent_create_order(monkeypatch):
    monkeypatch.setattr(settings, "agent_checkout_strict", True, raising=False)
    monkeypatch.setattr(settings, "agent_submit_payment_enabled", True, raising=False)
    monkeypatch.setattr(settings, "agent_submit_payment_merchants", frozenset(), raising=False)
    monkeypatch.setattr(settings, "agent_acp_test_capture", True, raising=False)
    monkeypatch.setattr(settings, "agent_acp_test_max_cents", 500, raising=False)
    monkeypatch.setattr(settings, "agent_acp_allow_live_capture", False, raising=False)

    monkeypatch.setattr(svc, "QuoteService", _FakeQuoteService)

    created_orders: list = []
    _stub_order_create_deep_seams(monkeypatch, created_orders)

    import db.orders as orders_mod

    async def fake_get_order(oid):
        return {
            "order_id": oid, "merchant_id": "merch_x", "total_amount": 1.69,
            "currency": "USD", "status": "created", "payment_status": "pending",
            "metadata": {"protocol_name": "acp"},
        }

    monkeypatch.setattr(orders_mod, "get_order", fake_get_order)

    async def fake_execute(**kwargs):
        return offpay.AcpOffsessionPaymentOutcome(
            success=True, status="succeeded", payment_intent_id="pi_wiring",
            psp_used="stripe", error=None, error_code=None,
            payment_intent=SimpleNamespace(id="pi_wiring", status="succeeded", client_secret=None),
        )

    async def fake_settle(**kwargs):
        return {"reconciliation_needed": False}

    monkeypatch.setattr(offpay, "execute_acp_offsession_payment", fake_execute)
    monkeypatch.setattr(offpay, "settle_acp_offsession_success", fake_settle)

    created = await svc.create_session(
        merchant_id="merch_x",
        platform="shopify",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 1}],
        metadata={"pvt_click_id": "clk_wiring"},
        # No defaults exist any more: a session without a real address and a
        # real buyer email is refused at /complete, so the wiring fixture must
        # carry both (and the order below is asserted to receive THEM).
        buyer={"email": "wiring-buyer@example.com"},
        fulfillment_address={
            "name": "Wiring Buyer",
            "address_line1": "742 Evergreen Terrace",
            "city": "Springfield",
            "postal_code": "97477",
            "country": "US",
        },
        agent_id="agent_test",
    )

    # NOTE: _create_pivota_order and agent_create_order run REAL here. With the
    # F1 sentinel bug (agent_user/x_buyer_ref left to their Depends/Header
    # defaults) this raises AttributeError inside the route body and surfaces
    # as acp_order_create_failed instead of completing.
    out = await svc.complete_session(
        session_id=created.session_id,
        agent_context=_Ctx(),
        payment_token="pm_card_visa",
    )

    assert out["status"] == "completed"
    assert out["order_id"] == "ord_wiring_1"
    assert len(created_orders) == 1
    order_request = created_orders[0]
    assert order_request.metadata["protocol_name"] == "acp"
    assert order_request.metadata["checkout_session_id"] == created.session_id
    assert order_request.metadata["pvt_click_id"] == "clk_wiring"
    # The sentinel bug's exact symptom: metadata polluted by the Depends object.
    assert "agent_user_ref" not in order_request.metadata
    # The REAL order carries the session's REAL buyer identity — no placeholder
    # address, no placeholder mailbox (this runs the real _create_pivota_order).
    assert order_request.customer_email == "wiring-buyer@example.com"
    assert order_request.shipping_address.address_line1 == "742 Evergreen Terrace"
    assert order_request.shipping_address.city == "Springfield"
    assert order_request.shipping_address.postal_code == "97477"
    assert order_request.shipping_address.country == "US"


async def test_order_create_refuses_a_session_without_buyer_identity(monkeypatch):
    # The fail-closed half, through the REAL _create_pivota_order: a session
    # with no address / no buyer email can never reach agent_create_order, so no
    # order is ever minted against invented buyer data.
    monkeypatch.setattr(svc, "QuoteService", _FakeQuoteService)
    created_orders: list = []
    _stub_order_create_deep_seams(monkeypatch, created_orders)

    session = {
        "id": "csn_wiring_nofill",
        "merchant_id": "merch_x",
        "items": [{"product_id": "p1", "variant_id": "v1", "quantity": 1}],
        "metadata": {"protocol_name": "acp"},
        "currency": "USD",
    }
    quote = await _FakeQuoteService().preview_quote()

    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc._create_pivota_order(
            session=session, quote=quote, agent_context=_Ctx(),
            protocol_name="acp", idempotency_key=None,
        )
    assert ei.value.code == "acp_address_required"
    assert ei.value.status_code == 400

    session["fulfillment_address"] = {
        "name": "Wiring Buyer", "address_line1": "742 Evergreen Terrace",
        "city": "Springfield", "postal_code": "97477", "country": "US",
    }
    with pytest.raises(svc.AcpCheckoutSessionError) as ei:
        await svc._create_pivota_order(
            session=session, quote=quote, agent_context=_Ctx(),
            protocol_name="acp", idempotency_key=None,
        )
    assert ei.value.code == "acp_email_required"
    assert ei.value.status_code == 400
    assert created_orders == []  # nothing was ever created
