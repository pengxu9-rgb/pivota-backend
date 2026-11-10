"""FIX_PLAN C-11 without a database: the cancel route's flag-off identity through the real app, the PSP-surface
classification for PSPs other than the Stripe paths the Postgres gate drives, and the payment finalizer's
flag-off answer for a payment on a cancelled order. The SQL itself is pinned by
tests/test_checkout_cancel_upstream_postgres.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from main import app  # noqa: E402


async def _post(path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, **kwargs)


# ---- the route, flag off: it does not exist ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": "ak_test_not_a_real_key"}, {"X-Checkout-Token": "v1.not.a-token"}],
    ids=["no-credential", "api-key", "checkout-token"],
)
async def test_flag_off_the_cancel_route_answers_exactly_like_an_unregistered_path(monkeypatch, headers) -> None:
    monkeypatch.delenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", raising=False)
    cancel = await _post("/agent/v2/orders/ORD_X/cancel", json={}, headers=headers)
    absent = await _post("/agent/v2/orders/ORD_X/cancel-nothing-here", json={}, headers=headers)
    assert cancel.status_code == absent.status_code == 404
    # The app's error envelope stamps a timestamp and a request id per response; everything else is identical.
    assert _without_per_request_fields(cancel) == _without_per_request_fields(absent)
    assert cancel.json()["detail"] == "Not Found"


def _without_per_request_fields(resp: httpx.Response) -> Dict[str, Any]:
    body = resp.json()
    body.pop("metadata", None)
    headers = {k: v for k, v in resp.headers.items() if k not in {"x-request-id", "content-length"}}
    return {"body": body, "headers": headers}


# ---- the answers the gateway classifies (PIVOTA-Agent src/services/checkoutCancelUpstream.js) ----------------


@pytest.fixture()
def staged_outcome(monkeypatch):
    """The real route and the real app's error envelope, with the cancel's OUTCOME staged (the outcome producer
    itself is driven against Postgres in tests/test_checkout_cancel_upstream_postgres.py)."""
    import routes.agent_auth as agent_auth
    import routes.agent_v2 as agent_v2
    import services.checkout_cancel_upstream as cancel_module

    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")
    orders = {
        "ORD_MINE": {"order_id": "ORD_MINE", "agent_id": "agent_a", "status": "pending", "payment_status": "awaiting_payment"},
        "ORD_THEIRS": {"order_id": "ORD_THEIRS", "agent_id": "agent_b", "status": "pending"},
    }

    async def get_order(order_id: str):
        return orders.get(order_id)

    monkeypatch.setattr(agent_v2, "get_order", get_order)
    state = {"outcome": "canceled"}

    async def cancel(order: Dict[str, Any], *, agent_id: str) -> Dict[str, Any]:
        after = {**order, "status": "cancelled", "payment_status": "cancelled"} if state["outcome"] == "canceled" else order
        return {"outcome": state["outcome"], "order": after, "payment_surface": {"action": "none", "reference_type": None}}

    monkeypatch.setattr(cancel_module, "cancel_order_upstream", cancel)

    class _Ctx:
        agent_id = "agent_a"
        agent_name = "agent_a"
        allowed_merchants = None
        session_id = "s"

        def can_access_merchant(self, merchant_id: str) -> bool:
            return True

    async def ctx():
        return _Ctx()

    app.dependency_overrides[agent_auth.get_agent_context] = ctx
    try:
        yield state
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome, status, read",
    [
        ("canceled", 200, lambda b: (b["cancel"]["canceled"], b["cancel"]["outcome"])),
        ("already_canceled", 200, lambda b: (b["cancel"]["canceled"], b["cancel"]["outcome"])),
        ("not_canceled_paid", 409, lambda b: (b["detail"]["canceled"], b["detail"]["error"])),
        ("not_canceled_processing", 409, lambda b: (b["detail"]["canceled"], b["detail"]["error"])),
        ("not_confirmed", 503, lambda b: (b["detail"]["canceled"], b["detail"]["error"])),
    ],
)
async def test_each_outcome_reaches_the_gateway_in_the_keys_it_reads(staged_outcome, outcome, status, read) -> None:
    staged_outcome["outcome"] = outcome
    resp = await _post("/agent/v2/orders/ORD_MINE/cancel", json={})
    assert resp.status_code == status, resp.text
    expected = {
        "canceled": (True, "canceled"),
        "already_canceled": (True, "already_canceled"),
        "not_canceled_paid": (False, "ORDER_ALREADY_PAID"),
        "not_canceled_processing": (False, "PAYMENT_PROCESSING"),
        "not_confirmed": (False, "CANCEL_NOT_CONFIRMED"),
    }[outcome]
    assert read(resp.json()) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id", ["ORD_THEIRS", "ORD_NOBODY"])
async def test_another_agents_order_and_an_unknown_one_are_the_same_not_found(staged_outcome, order_id) -> None:
    resp = await _post(f"/agent/v2/orders/{order_id}/cancel", json={})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Order not found"


@pytest.mark.asyncio
async def test_flag_on_the_cancel_route_needs_an_agent_credential(monkeypatch) -> None:
    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")
    resp = await _post("/agent/v2/orders/ORD_X/cancel", json={})
    assert resp.status_code in (401, 403), resp.text


@pytest.mark.asyncio
async def test_a_checkout_token_may_not_call_the_cancel_route(monkeypatch) -> None:
    """The hosted page's own token is not an agent: the cancel route is not one of CHECKOUT_TOKEN_ROUTES."""
    import routes.agent_auth as agent_auth
    from routes.agent_checkout_intents import mint_checkout_token

    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", "test-checkout-token-secret-not-real")

    async def get_agent(agent_id: str) -> Dict[str, Any]:
        return {"agent_id": agent_id, "name": agent_id, "status": "active", "allowed_merchants": None}

    monkeypatch.setattr(agent_auth, "get_agent", get_agent)
    token = mint_checkout_token({"agent_id": "agent_a", "merchant_ids": ["m_a"], "order_id": "ORD_X"}, ttl_seconds=600)
    resp = await _post("/agent/v2/orders/ORD_X/cancel", json={}, headers={"X-Checkout-Token": token})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "Checkout token not authorized for this endpoint"


# ---- closing the payment surface on PSPs without the Stripe paths -------------------------------------------


class _NoVoidAdapter:
    """A PSP adapter on the base class's defaults: no void, no hosted-session expiry."""

    def __init__(self, status: str = "requires_action") -> None:
        self.status = status
        self.calls: List[str] = []

    async def cancel_payment_authorization(self, payment_intent_id: str, reason=None, idempotency_key=None):
        self.calls.append("cancel_payment_authorization")
        from adapters.psp_adapter import PSPAdapter

        return await PSPAdapter.cancel_payment_authorization(self, payment_intent_id, reason, idempotency_key)

    async def get_payment_status(self, payment_intent_id: str):
        self.calls.append("get_payment_status")
        return True, self.status, None


@pytest.fixture()
def adapter(monkeypatch):
    import routes.order_routes as order_routes

    holder: Dict[str, Any] = {}

    async def resolve(order: Dict[str, Any]):
        return "other_psp", holder["adapter"]

    monkeypatch.setattr(order_routes, "_resolve_order_psp_adapter", resolve)
    return holder


def _order(reference: Optional[str]) -> Dict[str, Any]:
    return {"order_id": "ORD_X", "merchant_id": "m_x", "payment_intent_id": reference, "status": "pending"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "psp_status, action",
    [
        ("requires_action", "failed"),  # could not void, not paid: the page may still be payable
        ("succeeded", "paid"),
        ("processing", "processing"),
        ("canceled", "already_closed"),
    ],
)
async def test_a_psp_that_cannot_void_is_never_reported_closed_unless_it_is(adapter, psp_status, action) -> None:
    from services.checkout_cancel_upstream import close_payment_surface

    adapter["adapter"] = _NoVoidAdapter(status=psp_status)
    surface = await close_payment_surface(_order("pay_other_1"))
    assert surface == {"action": action, "reference_type": "payment_intent"}
    assert adapter["adapter"].calls == ["cancel_payment_authorization", "get_payment_status"]


@pytest.mark.asyncio
async def test_a_checkout_session_on_a_psp_without_expiry_is_not_confirmed(adapter) -> None:
    from services.checkout_cancel_upstream import close_payment_surface

    adapter["adapter"] = _NoVoidAdapter()
    assert (await close_payment_surface(_order("cs_other_1")))["action"] == "failed"


@pytest.mark.asyncio
async def test_an_unresolvable_psp_is_not_confirmed(monkeypatch) -> None:
    import routes.order_routes as order_routes
    from services.checkout_cancel_upstream import close_payment_surface

    async def resolve(order: Dict[str, Any]):
        raise ValueError("Canonical merchant_psps configuration is missing for this order")

    monkeypatch.setattr(order_routes, "_resolve_order_psp_adapter", resolve)
    assert (await close_payment_surface(_order("pi_x")))["action"] == "failed"


@pytest.mark.asyncio
async def test_no_payment_reference_means_nothing_to_close() -> None:
    from services.checkout_cancel_upstream import close_payment_surface

    assert await close_payment_surface(_order(None)) == {"action": "none", "reference_type": None}


# ---- the finalizer: a payment on a cancelled order --------------------------------------------------------------


async def _finalize(order: Dict[str, Any], recorded: List[Dict[str, Any]], monkeypatch) -> Dict[str, Any]:
    import services.checkout_cancel_upstream as cancel_module
    from services.psp_payment_finalizer import finalize_payment_success

    async def record(order: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        recorded.append({"order_id": order["order_id"], **kwargs})
        return {"applied": False, "transitioned": False, "reason": "late_payment_after_cancel", "order_id": order["order_id"]}

    monkeypatch.setattr(cancel_module, "record_late_payment_after_cancel", record)

    async def never(*args: Any, **kwargs: Any):
        raise AssertionError("a settled order is never marked paid again")

    return await finalize_payment_success(
        order,
        psp="stripe",
        payment_reference="pi_late",
        amount_minor=4200,
        currency="usd",
        mark_order_paid_fn=never,
        log_order_event_fn=never,
    )


@pytest.mark.asyncio
async def test_flag_off_a_payment_on_a_cancelled_order_keeps_todays_answer(monkeypatch) -> None:
    monkeypatch.delenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", raising=False)
    recorded: List[Dict[str, Any]] = []
    out = await _finalize({"order_id": "ORD_X", "status": "cancelled", "payment_status": "cancelled"}, recorded, monkeypatch)
    assert out == {"applied": False, "reason": "already_settled", "order_id": "ORD_X"}
    assert recorded == []


@pytest.mark.asyncio
async def test_flag_on_a_payment_on_a_cancelled_order_is_a_late_payment(monkeypatch) -> None:
    from decimal import Decimal

    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")
    recorded: List[Dict[str, Any]] = []
    out = await _finalize(
        {"order_id": "ORD_X", "status": "cancelled", "payment_status": "cancelled", "currency": "USD"}, recorded, monkeypatch
    )
    assert out["reason"] == "late_payment_after_cancel"
    assert recorded == [
        {
            "order_id": "ORD_X",
            "psp": "stripe",
            "payment_reference": "pi_late",
            "amount_major": Decimal("42"),
            "amount_minor": 4200,
            "currency": "usd",
            "source_event": "payment_confirmed_webhook",
        }
    ]


@pytest.mark.asyncio
async def test_flag_on_a_refunded_order_is_not_a_late_payment(monkeypatch) -> None:
    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")
    recorded: List[Dict[str, Any]] = []
    out = await _finalize({"order_id": "ORD_X", "status": "refunded", "payment_status": "refunded"}, recorded, monkeypatch)
    assert out == {"applied": False, "reason": "already_settled", "order_id": "ORD_X"}
    assert recorded == []
