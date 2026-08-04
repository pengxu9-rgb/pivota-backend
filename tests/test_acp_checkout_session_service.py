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
    # The runtime self-heal owns the additive columns (capture_attempt,
    # psp_idempotency_key): create_all is checkfirst-only and will not add them
    # to a pivota_test.db an older build already created.
    await svc._ensure_acp_checkout_sessions_table()
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
