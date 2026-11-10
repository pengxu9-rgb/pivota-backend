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


# ---- the finalizer: a lost paid transition whose re-read fails is not silent (review P2-4) ----------------------


@pytest.fixture()
def money_alert_lines():
    """Lines on the "pivota" logger (utils/logger.py), the one logger whose lines survive in production."""
    import logging

    lines: List[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            if message.startswith("money_alert: "):
                lines.append(message)

    handler = _Capture(level=logging.ERROR)
    pivota = logging.getLogger("pivota")
    pivota.addHandler(handler)
    try:
        yield lines
    finally:
        pivota.removeHandler(handler)


async def _finalize_losing_the_paid_transition(monkeypatch, reread) -> Dict[str, Any]:
    """The real finalizer on an order still unpaid in memory whose atomic paid transition refuses (someone else
    changed the row), with the re-read staged."""
    import db.orders as db_orders
    from services.psp_payment_finalizer import finalize_payment_success

    monkeypatch.setattr(db_orders, "get_order", reread)

    async def lost(order_id: str) -> bool:
        return False

    async def never(*args: Any, **kwargs: Any):
        raise AssertionError("no side effect after a lost paid transition")

    return await finalize_payment_success(
        {"order_id": "ORD_LOST", "merchant_id": "m_x", "status": "pending", "payment_status": "processing", "currency": "USD"},
        psp="stripe",
        payment_reference="pi_lost",
        amount_minor=4200,
        currency="usd",
        source_event="payment_intent.succeeded",
        mark_order_paid_fn=lost,
        log_order_event_fn=never,
    )


_LOST = {"applied": False, "transitioned": False, "reason": "already_settled_concurrent", "order_id": "ORD_LOST"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read-raises", "row-missing"])
async def test_flag_on_a_failed_reread_after_a_lost_paid_transition_raises_the_money_alert(
    monkeypatch, money_alert_lines, failure
) -> None:
    import json

    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")

    async def reread(order_id: str):
        if failure == "read-raises":
            raise ConnectionError("connection reset")
        return None

    out = await _finalize_losing_the_paid_transition(monkeypatch, reread)

    assert out == _LOST, "the outcome stays fail-safe: nothing applied, nothing re-run"
    assert len(money_alert_lines) == 1
    body = json.loads(money_alert_lines[0][len("money_alert: "):])
    assert body == {
        "event": "late_payment_check_failed",
        "order_id": "ORD_LOST",
        "psp": "stripe",
        "payment_reference": "pi_lost",
        "amount_minor": "4200",
        "currency": "USD",
        "source_event": "payment_intent.succeeded",
        "reason": "order_read_failed" if failure == "read-raises" else "order_missing",
        "error_type": "ConnectionError" if failure == "read-raises" else None,
    }
    assert money_alert_lines[0] == "money_alert: " + json.dumps(body, sort_keys=True, separators=(",", ":"))


@pytest.mark.asyncio
async def test_flag_on_a_reread_that_finds_a_settled_row_stays_quiet(monkeypatch, money_alert_lines) -> None:
    """Another finalizer won the paid transition: today's answer, and no alert (the alert is for a read that failed)."""
    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")

    async def reread(order_id: str):
        return {"order_id": order_id, "status": "paid", "payment_status": "paid"}

    assert await _finalize_losing_the_paid_transition(monkeypatch, reread) == _LOST
    assert money_alert_lines == []


@pytest.mark.asyncio
async def test_flag_off_a_lost_paid_transition_reads_nothing_and_alerts_nothing(monkeypatch, money_alert_lines) -> None:
    monkeypatch.delenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", raising=False)

    async def reread(order_id: str):
        raise AssertionError("flag off: no re-read")

    assert await _finalize_losing_the_paid_transition(monkeypatch, reread) == _LOST
    assert money_alert_lines == []


# ---- the v1 cancel door (POST /agent/v1/orders/{id}/cancel, review P1-2) -----------------------------------------


@pytest.fixture()
def v1_door(monkeypatch):
    """The real v1 and v2 cancel routes in the real app, over an in-memory order store. The UPDATE the flag-off path
    issues, its order_cancelled event and its webhook are recorded; `cancel_order_upstream` is a recording spy whose
    outcome is staged (the producer itself is driven against Postgres)."""
    import db.database as db_database
    import db.products as db_products
    import routes.agent_api as agent_api
    import routes.agent_auth as agent_auth
    import routes.agent_v2 as agent_v2
    import services.agent_webhook_service as webhooks
    import services.checkout_cancel_upstream as cancel_module

    orders = {
        "ORD_HOSTED": {
            "order_id": "ORD_HOSTED",
            "agent_id": "agent_a",
            "merchant_id": "m_a",
            "status": "pending",
            "payment_status": "processing",
            "payment_intent_id": "cs_test_hosted_page",
            "metadata": {},
        },
        "ORD_PAID": {"order_id": "ORD_PAID", "agent_id": "agent_a", "merchant_id": "m_a", "status": "paid", "payment_status": "paid", "metadata": {}},
        "ORD_DONE": {"order_id": "ORD_DONE", "agent_id": "agent_a", "merchant_id": "m_a", "status": "cancelled", "payment_status": "cancelled", "metadata": {}},
        "ORD_THEIRS": {"order_id": "ORD_THEIRS", "agent_id": "agent_b", "merchant_id": "m_a", "status": "pending", "metadata": {}},
    }
    state: Dict[str, Any] = {"outcome": "canceled", "upstream": [], "writes": [], "events": []}

    async def get_order(order_id: str):
        row = orders.get(order_id)
        return dict(row) if row else None

    async def execute(query: Any, values: Optional[Dict[str, Any]] = None):
        state["writes"].append(" ".join(str(query).split()))
        if (values or {}).get("order_id") in orders:
            orders[values["order_id"]]["status"] = "cancelled"
        return 1

    async def log_order_event(**kwargs: Any) -> None:
        state["events"].append(("log_order_event", kwargs["event_type"]))

    async def webhook(agent_id: str, **kwargs: Any) -> None:
        state["events"].append(("webhook", kwargs["event_type"]))

    async def cancel(order: Dict[str, Any], *, agent_id: str) -> Dict[str, Any]:
        state["upstream"].append((order["order_id"], agent_id))
        after = {**order, "status": "cancelled", "payment_status": "cancelled"} if state["outcome"] == "canceled" else order
        return {"outcome": state["outcome"], "order": after, "payment_surface": {"action": "expired", "reference_type": "checkout_session"}}

    monkeypatch.setattr(agent_api, "get_order", get_order)
    monkeypatch.setattr(agent_v2, "get_order", get_order)
    monkeypatch.setattr(db_database.database, "execute", execute)
    monkeypatch.setattr(db_products, "log_order_event", log_order_event)
    monkeypatch.setattr(webhooks, "emit_agent_webhook_event", webhook)
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
    "order_id, status, body, writes",
    [
        # Today's answers (the scratch differential against the branch base pins 12 cases byte for byte). A 200 is
        # compared as bytes; an error by its detail, since the app's error envelope stamps a per-request id.
        ("ORD_HOSTED", 200, b'{"status":"success","order_id":"ORD_HOSTED"}', 1),
        ("ORD_PAID", 400, "Cannot cancel a paid/completed order. Please refund instead.", 0),
        ("ORD_DONE", 200, b'{"status":"success","order_id":"ORD_DONE","message":"Order already cancelled"}', 0),
        ("ORD_THEIRS", 403, "Not authorized for this order", 0),
    ],
    ids=["hosted-session", "paid", "already-cancelled", "foreign"],
)
async def test_flag_off_the_v1_cancel_is_todays_route(v1_door, monkeypatch, order_id, status, body, writes) -> None:
    monkeypatch.delenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", raising=False)
    resp = await _post(f"/agent/v1/orders/{order_id}/cancel")
    assert resp.status_code == status, resp.text
    assert (resp.content if status == 200 else resp.json()["detail"]) == body
    assert v1_door["upstream"] == [], "flag off: the upstream cancel is never called"
    assert v1_door["writes"] == ["UPDATE orders SET status = 'cancelled' WHERE order_id = :order_id"] * writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["not_canceled_paid", "not_canceled_processing", "not_confirmed"]
)
async def test_flag_on_the_v1_cancel_refuses_exactly_like_v2(v1_door, monkeypatch, outcome) -> None:
    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")
    v1_door["outcome"] = outcome
    v1 = await _post("/agent/v1/orders/ORD_HOSTED/cancel")
    v2 = await _post("/agent/v2/orders/ORD_HOSTED/cancel", json={})
    assert v1.status_code == v2.status_code == {"not_confirmed": 503}.get(outcome, 409)
    assert v1.json()["detail"] == v2.json()["detail"]
    assert v1.json()["detail"]["canceled"] is False
    assert v1_door["upstream"] == [("ORD_HOSTED", "agent_a")] * 2
    assert v1_door["writes"] == [], "the v1 door no longer flips the row on its own"
    assert v1_door["events"] == []


@pytest.mark.asyncio
async def test_flag_on_the_v1_cancel_goes_upstream_and_keeps_its_own_keys(v1_door, monkeypatch) -> None:
    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")
    resp = await _post("/agent/v1/orders/ORD_HOSTED/cancel")
    v2 = await _post("/agent/v2/orders/ORD_HOSTED/cancel", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"status": "success", "order_id": "ORD_HOSTED", "cancel": v2.json()["cancel"]}
    assert body["cancel"]["canceled"] is True and body["cancel"]["payment_surface"]["action"] == "expired"
    assert v1_door["writes"] == []
    assert v1_door["events"] == [("log_order_event", "order_cancelled"), ("webhook", "order.cancelled")]


@pytest.mark.asyncio
async def test_flag_on_the_v1_cancel_of_a_cancelled_order_is_idempotent_and_emits_nothing(v1_door, monkeypatch) -> None:
    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")
    v1_door["outcome"] = "already_canceled"
    resp = await _post("/agent/v1/orders/ORD_HOSTED/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["status"], body["order_id"], body["message"]) == ("success", "ORD_HOSTED", "Order already cancelled")
    assert body["cancel"]["outcome"] == "already_canceled" and body["cancel"]["canceled"] is True
    assert v1_door["events"] == []


@pytest.mark.asyncio
async def test_flag_on_the_v1_cancel_keeps_its_ownership_answers(v1_door, monkeypatch) -> None:
    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")
    foreign = await _post("/agent/v1/orders/ORD_THEIRS/cancel")
    unknown = await _post("/agent/v1/orders/ORD_NOBODY/cancel")
    assert (foreign.status_code, foreign.json()["detail"]) == (403, "Not authorized for this order")
    assert (unknown.status_code, unknown.json()["detail"]) == (404, "Order not found")
    assert v1_door["upstream"] == []


# ---- a PSP object made for an order cancelled meanwhile (create_payment's lost write) --------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "psp_status, alerted",
    [("canceled", False), ("requires_action", True), ("succeeded", True)],
    ids=["closed", "not-closed", "already-paid"],
)
async def test_closing_a_surface_made_for_a_cancelled_order_alerts_unless_it_closed(
    adapter, money_alert_lines, psp_status, alerted
) -> None:
    import json

    from services.checkout_cancel_upstream import close_surface_created_for_cancelled_order

    adapter["adapter"] = _NoVoidAdapter(status=psp_status)
    surface = await close_surface_created_for_cancelled_order(_order(None), reference="pay_other_new", psp="other_psp")

    assert adapter["adapter"].calls == ["cancel_payment_authorization", "get_payment_status"], "the NEW reference is closed"
    assert [json.loads(line[len("money_alert: "):])["action"] for line in money_alert_lines] == (
        [surface["action"]] if alerted else []
    )


@pytest.mark.asyncio
async def test_a_close_that_raises_is_alerted_and_never_propagates(monkeypatch, money_alert_lines) -> None:
    import json

    import services.checkout_cancel_upstream as cancel_module

    async def boom(order: Dict[str, Any]) -> Dict[str, Any]:
        raise TimeoutError("psp timeout")

    monkeypatch.setattr(cancel_module, "close_payment_surface", boom)
    surface = await cancel_module.close_surface_created_for_cancelled_order(_order(None), reference="cs_new", psp="stripe")

    assert surface["action"] == "failed"
    assert len(money_alert_lines) == 1
    body = json.loads(money_alert_lines[0][len("money_alert: "):])
    assert (body["event"], body["order_id"], body["payment_reference"], body["error_type"]) == (
        "cancelled_order_surface_not_closed",
        "ORD_X",
        "cs_new",
        "TimeoutError",
    )


@pytest.mark.asyncio
async def test_flag_on_the_off_session_capture_lane_keeps_its_unconditional_order_write(monkeypatch) -> None:
    """The synchronous off-session capture lane is left exactly as it is: its charge may already be captured, which
    closing a hosted surface would not undo. Even with the order cancelled meanwhile it writes as today (the late
    payment path owns that money)."""
    from types import SimpleNamespace

    import db.orders as db_orders
    import mvp.events as mvp_events
    import mvp.governance as mvp_governance
    import routes.agent_payment_sdk as pay
    import services.acp_offsession_payment as acp
    import services.commerce_attribution_service as attribution
    import services.psp_payment_finalizer as finalizer
    from fastapi import BackgroundTasks

    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")
    row = {
        "order_id": "ORD_OFF", "merchant_id": "m_off", "agent_id": "agent_off", "status": "pending",
        "payment_status": "awaiting_payment", "total": 42.0, "currency": "USD", "items": [],
        "metadata": {"pricing_quote": {"quote_id": "q_off", "live_validation": {"status": "validated"}, "expires_at": "2099-01-01T00:00:00Z"}},
    }
    writes: List[str] = []

    async def get_order(order_id: str):
        return dict(row)

    async def offsession(**kwargs: Any):
        row["status"] = "cancelled"  # the cancel commits while the capture runs
        return SimpleNamespace(success=True, error=None, psp_used="stripe", payment_intent=SimpleNamespace(id="pi_off_new", status="succeeded", client_secret=None))

    async def unconditional(**kwargs: Any) -> bool:
        writes.append("update_payment_info")
        return True

    async def conditional(**kwargs: Any) -> bool:
        writes.append("update_payment_info_unless_cancelled")
        return False

    class _Routing:
        def __init__(self, _db: Any) -> None:
            pass

        async def select_psp(self, **_kwargs: Any):
            return "stripe", {}

    class _Decision:
        decision = "allow"
        reason_codes: list = []
        required_scopes: list = []
        risk_tier = "low"

    async def none(*_a: Any, **_k: Any):
        return None

    async def stop_after_the_write(*_a: Any, **_k: Any):
        raise RuntimeError("stopped by the test after the order write")

    monkeypatch.setattr(pay, "get_order", get_order)
    monkeypatch.setattr(pay, "update_payment_info", unconditional)
    monkeypatch.setattr(db_orders, "update_payment_info_unless_cancelled", conditional)
    async def store(merchant_id: str) -> Dict[str, Any]:
        return {"platform": "shopify", "store_id": "store_off"}

    async def onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": merchant_id}

    monkeypatch.setattr(pay, "get_primary_store", store)
    monkeypatch.setattr(pay, "PaymentRoutingService", _Routing)
    monkeypatch.setattr(pay, "get_merchant_onboarding", onboarding)
    monkeypatch.setattr(pay.database, "fetch_one", none)
    monkeypatch.setattr(pay.database, "execute", none)
    monkeypatch.setattr(attribution, "upsert_order_attribution_edge", none)
    monkeypatch.setattr(finalizer, "finalize_payment_success", stop_after_the_write)
    monkeypatch.setattr(
        acp,
        "evaluate_acp_offsession_gates",
        lambda **_k: SimpleNamespace(
            kill_switch=SimpleNamespace(),
            test_capture=SimpleNamespace(bypass_live_readiness=True),
            live_capture=SimpleNamespace(allow_live=False, max_cents=None),
        ),
    )
    monkeypatch.setattr(acp, "execute_acp_offsession_payment", offsession)
    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
    monkeypatch.setattr(mvp_governance.governance, "evaluate", lambda *_a, **_k: _Decision())
    monkeypatch.setattr(mvp_governance.governance, "record_audit_event", lambda **_: None)

    class _Ctx:
        agent_id = "agent_off"
        session_id = "s"

        def can_access_merchant(self, merchant_id: str) -> bool:
            return True

    outcome: Any = None
    try:
        outcome = await pay.create_payment(
            pay.PaymentRequest(order_id="ORD_OFF", payment_method=pay.PaymentMethod(type="card")),
            BackgroundTasks(),
            context=_Ctx(),
        )
    except Exception as exc:  # noqa: BLE001 - what follows the write is not under test
        outcome = exc

    assert writes == ["update_payment_info"], repr(outcome)
    assert "ORDER_CANCELED" not in repr(outcome)
