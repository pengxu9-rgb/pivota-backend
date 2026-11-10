"""Production-dialect gate: an agent's cancel closes the hosted payment page and cancels only an unpaid order.

    createdb getorder_cancel_test_dialect_check
    DATABASE_URL=postgresql://localhost/getorder_cancel_test_dialect_check \\
        .venv/bin/python -m pytest tests/test_checkout_cancel_upstream_postgres.py

FIX_PLAN C-11, CHECKOUT_CANCEL_UPSTREAM_ENABLED (services/checkout_cancel_upstream.py).

HOW IT IS DRIVEN. The real routes (`POST /agent/v2/orders/{id}/cancel`, `POST /webhooks/stripe`,
`POST /agent/v1/payments`) over httpx.ASGITransport, real `orders` and `after_sales_cases` rows, and the real
`StripeAdapter` with its Stripe client replaced by a recording fake (the pattern of
tests/test_stripe_payment_element_runtime.py): no live PSP is ever called. Only the agent context, the webhook's
signature check and its best-effort side ledgers are doubled.

WHY POSTGRES. The cancel and the paid transition are each ONE conditional UPDATE ... RETURNING, and the claim that
they serialise (exactly one wins) is a claim about the real dialect's row semantics.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

OWNER = "agent_cancel_owner"
OTHER = "agent_cancel_other"
MERCHANT_ID = "m_cancel_upstream"
ORDER_ID = "ORD_CANCEL_UPSTREAM"
ORDER_TOTAL = Decimal("42.00")
SECRET = "whsec_cancel_upstream_fake"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write orders in database {dbname!r}; use a throwaway *_dialect_check database")


async def _delete_own_rows() -> None:
    from db.database import database

    await database.execute("DELETE FROM orders WHERE order_id = :o", {"o": ORDER_ID})
    await database.execute("DELETE FROM after_sales_cases WHERE order_id = :o", {"o": ORDER_ID})


async def _insert_order(
    *,
    status: str = "pending",
    payment_status: str = "awaiting_payment",
    payment_intent_id: Optional[str] = None,
    agent_id: str = OWNER,
) -> None:
    from db.database import database
    from db.orders import orders

    await database.execute(
        orders.insert().values(
            order_id=ORDER_ID,
            merchant_id=MERCHANT_ID,
            customer_email="buyer@example.com",
            shipping_address={},
            items=[],
            subtotal=ORDER_TOTAL,
            total=ORDER_TOTAL,
            total_refunded=Decimal("0"),
            currency="USD",
            status=status,
            payment_status=payment_status,
            payment_intent_id=payment_intent_id,
            psp_used="stripe" if payment_intent_id else None,
            agent_id=agent_id,
            metadata={"agent_v2": {"hosted_checkout": True}},
            is_deleted=False,
            created_at=datetime.now(timezone.utc),
        )
    )


async def _stored() -> Dict[str, Any]:
    from db.database import database

    row = await database.fetch_one(
        "SELECT status, payment_status, payment_intent_id, cancelled_at, metadata FROM orders WHERE order_id = :o",
        {"o": ORDER_ID},
    )
    assert row is not None
    raw = row["metadata"]
    return {
        "status": row["status"],
        "payment_status": row["payment_status"],
        "payment_intent_id": row["payment_intent_id"],
        "cancelled_at": row["cancelled_at"],
        "metadata": json.loads(raw) if isinstance(raw, str) else raw,
    }


async def _cases() -> List[Dict[str, Any]]:
    from db.database import database

    rows = await database.fetch_all(
        "SELECT case_id, agent_id, status, reason_code, requested_refund_amount, currency_order, idempotency_key "
        "FROM after_sales_cases WHERE order_id = :o",
        {"o": ORDER_ID},
    )
    return [dict(r) for r in rows]


@pytest.fixture(autouse=True)
async def _db():
    from sqlalchemy import create_engine

    from db.database import database, metadata
    from db.orders import orders

    _assert_throwaway_database()
    engine = create_engine(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        metadata.create_all(engine, tables=[orders], checkfirst=True)
    finally:
        engine.dispose()

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    from routes.after_sales_cases import _ensure_after_sales_cases_table

    await _ensure_after_sales_cases_table()
    await _delete_own_rows()
    yield
    await _delete_own_rows()
    if not was_connected and database.is_connected:
        await database.disconnect()


# ---- the PSP: the real StripeAdapter over a recording fake client ---------------------------------------------


class _FakeStripe:
    """What the Stripe API would do, per object id. Records every call; holds no secret."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.pi_status: Dict[str, str] = {}
        self.session: Dict[str, Dict[str, str]] = {}

    # payment_intents
    def pi_cancel(self, intent: str, params: Any = None, options: Any = None):
        self.calls.append({"method": "payment_intents.cancel", "id": intent, "options": options})
        if self.pi_status.get(intent) in {"succeeded", "processing", "canceled"}:
            raise RuntimeError(f"PaymentIntent {intent} cannot be canceled from {self.pi_status[intent]}")
        self.pi_status[intent] = "canceled"
        return SimpleNamespace(id=intent, status="canceled")

    def pi_retrieve(self, intent: str, params: Any = None, options: Any = None):
        self.calls.append({"method": "payment_intents.retrieve", "id": intent})
        status = self.pi_status.get(intent, "requires_payment_method")
        return {"id": intent, "status": status, "amount": 4200, "amount_received": 4200, "currency": "usd"}

    # checkout.sessions
    def cs_expire(self, session: str, params: Any = None, options: Any = None):
        self.calls.append({"method": "checkout.sessions.expire", "id": session})
        state = self.session.setdefault(session, {"status": "open", "payment_status": "unpaid"})
        if state["status"] != "open":
            raise RuntimeError(f"Checkout Session {session} is {state['status']}")
        state["status"] = "expired"
        return SimpleNamespace(id=session, status="expired")

    def cs_retrieve(self, session: str, params: Any = None, options: Any = None):
        self.calls.append({"method": "checkout.sessions.retrieve", "id": session})
        state = self.session.get(session, {"status": "open", "payment_status": "unpaid"})
        return SimpleNamespace(id=session, **state)

    # refunds: present only so a refund attempt would be RECORDED, never silently absent
    def refund_create(self, params: Any = None, options: Any = None):
        self.calls.append({"method": "refunds.create", "params": params})
        raise AssertionError("a refund must never be executed by the cancel or late-payment path")

    def methods(self) -> List[str]:
        return [c["method"] for c in self.calls]


@pytest.fixture()
def stripe_fake(monkeypatch: pytest.MonkeyPatch) -> _FakeStripe:
    from adapters import psp_adapter as module
    import routes.order_routes as order_routes

    fake = _FakeStripe()

    class _FakeStripeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.v1 = SimpleNamespace(
                payment_intents=SimpleNamespace(cancel=fake.pi_cancel, retrieve=fake.pi_retrieve),
                checkout=SimpleNamespace(sessions=SimpleNamespace(expire=fake.cs_expire, retrieve=fake.cs_retrieve)),
                refunds=SimpleNamespace(create=fake.refund_create),
            )

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(module.stripe, "StripeClient", _FakeStripeClient)
    monkeypatch.setattr(module.stripe, "RequestsClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(module.asyncio, "to_thread", _run_inline)

    async def resolve(order: Dict[str, Any]):
        assert order["merchant_id"] == MERCHANT_ID
        return "stripe", module.StripeAdapter(api_key="sk_test_fake_cancel_upstream")

    monkeypatch.setattr(order_routes, "_resolve_order_psp_adapter", resolve)
    return fake


# ---- the app: the real routers, the caller's agent context doubled -----------------------------------------


class _Ctx:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.agent_name = agent_id
        self.allowed_merchants = [MERCHANT_ID]
        self.session_id = "s"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return merchant_id in self.allowed_merchants


@pytest.fixture()
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", "1")


def _app(agent_id: str):
    from fastapi import FastAPI

    import routes.agent_auth as agent_auth
    import routes.agent_payment_sdk as agent_payment_sdk
    import routes.agent_v2 as agent_v2
    import routes.webhook_routes as webhook_routes

    app = FastAPI()
    app.include_router(agent_v2.router)
    app.include_router(agent_payment_sdk.router)
    app.include_router(webhook_routes.router)

    async def ctx():
        return _Ctx(agent_id)

    app.dependency_overrides[agent_auth.get_agent_context] = ctx
    return app


async def _post(app, path: str, **kwargs: Any):
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, **kwargs)


async def _cancel(agent_id: str = OWNER):
    return await _post(_app(agent_id), f"/agent/v2/orders/{ORDER_ID}/cancel", json={"idempotency_key": "idem_cancel_1"})


@pytest.fixture()
def webhook(monkeypatch: pytest.MonkeyPatch):
    import routes.webhook_routes as webhook_routes

    async def fake_record_event(**kwargs: Any) -> bool:
        return False

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(webhook_routes.settings, "stripe_webhook_secret", SECRET, raising=False)
    monkeypatch.setattr(webhook_routes, "_record_stripe_webhook_event_best_effort", fake_record_event)
    monkeypatch.setattr(webhook_routes, "_mark_stripe_webhook_event_status_best_effort", noop)
    monkeypatch.setattr(webhook_routes, "_record_stripe_canonical_event_best_effort", noop)
    monkeypatch.setattr(webhook_routes, "_emit_stripe_merchant_webhook_best_effort", noop)
    monkeypatch.setattr(webhook_routes, "create_order_snapshot_evidence_pack", noop)
    monkeypatch.setattr(webhook_routes, "log_order_event", noop)
    monkeypatch.setattr(webhook_routes, "enqueue_merchant_order_create", noop)
    seq = [0]

    async def send(payment_intent: str) -> Dict[str, Any]:
        seq[0] += 1
        event = {
            "id": f"evt_cancel_upstream_{seq[0]}",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": payment_intent,
                    "object": "payment_intent",
                    "amount": 4200,
                    "amount_received": 4200,
                    "currency": "usd",
                    "metadata": {"order_id": ORDER_ID},
                }
            },
        }

        def construct(payload: bytes, signature: Optional[str], secret: str) -> Dict[str, Any]:
            assert secret == SECRET
            return event

        monkeypatch.setattr(webhook_routes.stripe.Webhook, "construct_event", staticmethod(construct))
        resp = await _post(_app(OWNER), "/webhooks/stripe", content=b"{}", headers={"stripe-signature": "sig_fake"})
        assert resp.status_code == 200, resp.text
        return resp.json()

    return send


@pytest.fixture()
def alerts():
    lines: List[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            if message.startswith("CHECKOUT_LATE_PAYMENT_AFTER_CANCEL_ALERT"):
                lines.append(message)

    handler = _Capture(level=logging.ERROR)
    logger = logging.getLogger("pivota")
    logger.addHandler(handler)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)


# ---- cancel before payment ---------------------------------------------------------------------------------


async def test_cancel_before_payment_expires_the_checkout_session_and_cancels_the_order(flag_on, stripe_fake) -> None:
    await _insert_order(payment_intent_id="cs_cancel_open")

    resp = await _cancel()

    assert resp.status_code == 200, resp.text
    cancel = resp.json()["cancel"]
    assert cancel["canceled"] is True and cancel["outcome"] == "canceled"
    assert cancel["order_state"] == "cancelled"
    assert cancel["payment_surface"] == {"action": "expired", "reference_type": "checkout_session"}
    assert stripe_fake.methods() == ["checkout.sessions.expire"]
    assert stripe_fake.session["cs_cancel_open"]["status"] == "expired"
    stored = await _stored()
    assert (stored["status"], stored["payment_status"]) == ("cancelled", "cancelled")
    assert stored["cancelled_at"] is not None
    assert stored["metadata"]["checkout_cancel"]["agent_id"] == OWNER
    assert stored["metadata"]["agent_v2"] == {"hosted_checkout": True}, "other writers' metadata is kept"


async def test_cancel_before_payment_voids_the_payment_intent(flag_on, stripe_fake) -> None:
    await _insert_order(payment_intent_id="pi_cancel_open")

    resp = await _cancel()

    assert resp.status_code == 200, resp.text
    assert resp.json()["cancel"]["payment_surface"] == {"action": "canceled", "reference_type": "payment_intent"}
    assert stripe_fake.calls[0]["method"] == "payment_intents.cancel"
    assert stripe_fake.calls[0]["options"] == {"idempotency_key": f"checkout_cancel:{ORDER_ID}:pi_cancel_open"}
    assert (await _stored())["status"] == "cancelled"


async def test_a_cancelled_order_gets_no_payment_surface_and_its_hosted_token_cannot_start_one(
    flag_on, stripe_fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hosted page itself: no PSP payment existed yet, so closing the page is refusing the next one."""
    import routes.agent_auth as agent_auth
    from routes.agent_checkout_intents import mint_checkout_token

    await _insert_order()
    assert (await _cancel()).status_code == 200
    assert stripe_fake.calls == [], "no PSP payment existed: nothing to call"

    refused = await _post(_app(OWNER), "/agent/v1/payments", json={"order_id": ORDER_ID, "payment_method": {"type": "card"}, "idempotency_key": "k1"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == "ORDER_CANCELED"

    # The buyer's own link: the real checkout-token auth path, with a token bound to this order.
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", "test-checkout-token-secret-not-real")

    async def get_agent(agent_id: str) -> Dict[str, Any]:
        return {"agent_id": agent_id, "name": agent_id, "status": "active", "is_active": True, "allowed_merchants": None}

    monkeypatch.setattr(agent_auth, "get_agent", get_agent)
    token = mint_checkout_token(
        {"agent_id": OWNER, "merchant_ids": [MERCHANT_ID], "order_id": ORDER_ID, "intent_id": "ci_cancel_upstream"},
        ttl_seconds=600,
    )
    from fastapi import FastAPI

    import routes.agent_payment_sdk as agent_payment_sdk

    page = FastAPI()
    page.include_router(agent_payment_sdk.router)
    blocked = await _post(page, "/agent/v1/payments", json={"order_id": ORDER_ID, "payment_method": {"type": "card"}}, headers={"X-Checkout-Token": token})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["error"] == "CHECKOUT_CANCELED"


# ---- cancel after payment: an honest answer, no refund -------------------------------------------------------


async def test_cancel_after_payment_says_paid_and_executes_no_refund(flag_on, stripe_fake) -> None:
    await _insert_order(status="paid", payment_status="paid", payment_intent_id="pi_cancel_paid")

    resp = await _cancel()

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "ORDER_ALREADY_PAID"
    assert detail["canceled"] is False and detail["outcome"] == "not_canceled_paid"
    assert detail["refund"]["executed"] is False
    assert detail["refund"]["path"] == "/agent/v1/after-sales/cases"
    assert detail["refund"]["code"] == "REFUND_HANDLED_BY_MERCHANT"
    assert stripe_fake.calls == [], "a paid order: the PSP is not touched, and nothing is refunded"
    stored = await _stored()
    assert (stored["status"], stored["payment_status"]) == ("paid", "paid")
    assert await _cases() == []


async def test_a_buyer_who_paid_before_the_webhook_arrived_is_still_reported_paid(flag_on, stripe_fake) -> None:
    """The row still says unpaid (the webhook lags); Stripe refuses the void because the PaymentIntent succeeded."""
    await _insert_order(payment_intent_id="pi_cancel_race")
    stripe_fake.pi_status["pi_cancel_race"] = "succeeded"

    resp = await _cancel()

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "ORDER_ALREADY_PAID"
    assert "refunds.create" not in stripe_fake.methods()
    assert (await _stored())["status"] == "pending", "a paid order is never cancelled"


async def test_a_completed_checkout_session_is_reported_paid(flag_on, stripe_fake) -> None:
    await _insert_order(payment_intent_id="cs_cancel_done")
    stripe_fake.session["cs_cancel_done"] = {"status": "complete", "payment_status": "paid"}

    resp = await _cancel()

    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "ORDER_ALREADY_PAID"
    assert (await _stored())["status"] == "pending"


async def test_a_payment_still_settling_is_not_canceled(flag_on, stripe_fake) -> None:
    await _insert_order(payment_intent_id="pi_cancel_settling")
    stripe_fake.pi_status["pi_cancel_settling"] = "processing"

    resp = await _cancel()

    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "PAYMENT_PROCESSING"
    assert (await _stored())["status"] == "pending"


async def test_a_payment_landing_between_the_void_and_the_cancel_wins_and_is_reported(
    flag_on, stripe_fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another PaymentIntent settles after the void but before the cancel UPDATE. The two conditional UPDATEs
    serialise on the row: the real db.orders.mark_order_paid wins, the cancel refuses, and the answer says paid."""
    import services.checkout_cancel_upstream as cancel_module
    from db.orders import mark_order_paid

    await _insert_order(payment_intent_id="pi_cancel_window")
    real_close = cancel_module.close_payment_surface
    paid: List[bool] = []

    async def close_then_a_payment_lands(order: Dict[str, Any]) -> Dict[str, Any]:
        surface = await real_close(order)
        paid.append(await mark_order_paid(ORDER_ID))
        return surface

    monkeypatch.setattr(cancel_module, "close_payment_surface", close_then_a_payment_lands)

    resp = await _cancel()

    assert paid == [True], "the payment won the row"
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "ORDER_ALREADY_PAID"
    stored = await _stored()
    assert (stored["status"], stored["payment_status"]) == ("paid", "paid")


@pytest.mark.parametrize(
    "paid_columns",
    [{"payment_status": "paid"}, {"status": "paid"}, {"payment_status": "succeeded"}, {"status": "fulfilled"}],
    ids=["payment_status-paid", "status-paid", "payment_status-succeeded", "status-fulfilled"],
)
async def test_either_paid_column_alone_stops_the_cancel_update(
    flag_on, stripe_fake, monkeypatch: pytest.MonkeyPatch, paid_columns: Dict[str, str]
) -> None:
    """Some writers move one column only (update_payment_info sets payment_status; others set status). The cancel
    UPDATE refuses a row that either column says is paid."""
    import services.checkout_cancel_upstream as cancel_module
    from db.database import database

    await _insert_order(payment_intent_id="pi_cancel_one_column")
    real_close = cancel_module.close_payment_surface

    async def close_then_one_column_moves(order: Dict[str, Any]) -> Dict[str, Any]:
        surface = await real_close(order)
        sets = ", ".join(f"{column} = :{column}" for column in paid_columns)
        await database.execute(f"UPDATE orders SET {sets} WHERE order_id = :o", {**paid_columns, "o": ORDER_ID})
        return surface

    monkeypatch.setattr(cancel_module, "close_payment_surface", close_then_one_column_moves)

    resp = await _cancel()

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "ORDER_ALREADY_PAID"
    stored = await _stored()
    assert stored["status"] != "cancelled" and stored["payment_status"] != "cancelled"


async def test_a_cancel_that_won_makes_the_late_paid_transition_refuse(flag_on, stripe_fake) -> None:
    """The other order of the same race: the cancel committed first, so mark_order_paid refuses the row."""
    from db.orders import mark_order_paid

    await _insert_order(payment_intent_id="pi_cancel_first")
    assert (await _cancel()).status_code == 200
    assert await mark_order_paid(ORDER_ID) is False
    assert (await _stored())["status"] == "cancelled"


async def test_a_psp_failure_is_not_confirmed_and_cancels_nothing(flag_on, stripe_fake, monkeypatch) -> None:
    await _insert_order(payment_intent_id="pi_cancel_down")

    def down(*args: Any, **kwargs: Any):
        stripe_fake.calls.append({"method": "payment_intents.cancel"})
        raise RuntimeError("connection reset")

    def unknown(*args: Any, **kwargs: Any):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(stripe_fake, "pi_cancel", down)
    monkeypatch.setattr(stripe_fake, "pi_retrieve", unknown)

    resp = await _cancel()

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "CANCEL_NOT_CONFIRMED" and detail["canceled"] is False and detail["retryable"] is True
    assert (await _stored())["status"] == "pending"


# ---- late payment after a cancel -----------------------------------------------------------------------------


async def test_a_payment_after_cancel_is_recorded_and_raised_as_a_refund_request(
    flag_on, stripe_fake, webhook, alerts
) -> None:
    """A stale tab confirms an OLDER PaymentIntent nobody holds any more, after the cancel succeeded."""
    await _insert_order(payment_intent_id="pi_cancel_current")
    assert (await _cancel()).status_code == 200

    body = await webhook("pi_cancel_stale_tab")

    assert body == {"status": "success", "event": "payment_intent.succeeded"}
    stored = await _stored()
    assert stored["status"] == "cancelled", "the order stays cancelled"
    assert stored["payment_status"] == "paid_after_cancel", "the money is recorded on the order"
    late = stored["metadata"]["late_payments"]
    assert [(p["psp"], p["payment_reference"], p["amount_minor"]) for p in late] == [("stripe", "pi_cancel_stale_tab", "4200")]
    cases = await _cases()
    assert len(cases) == 1
    case = cases[0]
    assert case["status"] == "refund_requested_from_merchant"
    assert case["reason_code"] == "payment_after_cancel"
    assert case["agent_id"] == OWNER
    assert Decimal(str(case["requested_refund_amount"])) == Decimal("42.00")
    assert case["currency_order"] == "USD"
    assert len(alerts) == 1
    alert = json.loads(alerts[0].split(" ", 1)[1])
    assert alert["order_id"] == ORDER_ID and alert["case_id"] == case["case_id"] and alert["recorded"] is True
    assert "refunds.create" not in stripe_fake.methods(), "never auto-refunded"

    # Stripe redelivers: one case, one alert.
    await webhook("pi_cancel_stale_tab")
    assert len(await _cases()) == 1
    assert len(alerts) == 1
    assert len((await _stored())["metadata"]["late_payments"]) == 1


# ---- ownership -------------------------------------------------------------------------------------------------


async def test_a_foreign_agent_is_refused_as_not_found_and_nothing_moves(flag_on, stripe_fake) -> None:
    await _insert_order(payment_intent_id="cs_cancel_foreign")

    resp = await _cancel(agent_id=OTHER)

    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": "Order not found"}
    assert stripe_fake.calls == []
    assert (await _stored())["status"] == "pending"


async def test_an_unknown_order_is_not_found(flag_on, stripe_fake) -> None:
    resp = await _cancel()
    assert resp.status_code == 404 and resp.json() == {"detail": "Order not found"}


# ---- flag off ----------------------------------------------------------------------------------------------------


async def test_flag_off_the_route_does_not_exist_and_a_late_payment_keeps_todays_answer(
    stripe_fake, webhook, alerts, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHECKOUT_CANCEL_UPSTREAM_ENABLED", raising=False)
    await _insert_order(status="cancelled", payment_status="cancelled", payment_intent_id="pi_cancel_off")

    resp = await _cancel()
    absent = await _post(_app(OWNER), f"/agent/v2/orders/{ORDER_ID}/no-such-route", json={})

    assert (resp.status_code, resp.content) == (absent.status_code, absent.content) == (404, b'{"detail":"Not Found"}')
    body = await webhook("pi_cancel_off")
    assert body == {"status": "success", "event": "payment_intent.succeeded"}
    stored = await _stored()
    assert (stored["status"], stored["payment_status"]) == ("cancelled", "cancelled")
    assert "late_payments" not in (stored["metadata"] or {})
    assert await _cases() == []
    assert alerts == []
    assert stripe_fake.calls == []
