"""Sequential partial refunds must land on the correct total_refunded.

`finalize_refund_success` applies `refund_total` as a MONOTONIC CEILING
(`max(current_total_refunded, refund_total)`), not an accumulator. That suits
`charge.refunded`, whose `amount_refunded` is the charge's cumulative total, but
`refund.updated` carries a SINGLE refund's amount. Feeding that single amount to
the ceiling under-counts; feeding a naive running sum double-counts whenever
`charge.refunded` already contributed the same money under a different refund key
(`stripe:ch_…` vs `stripe:re_…`, which the psp_refund_refs guard does not catch).

These drive the real ASGI route against a STATEFUL order, so each event sees the
state the previous one wrote — the only way the ordering bugs are observable.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app


SECRET = "whsec_partial_accounting"


class _StatefulOrder:
    """An order whose total_refunded and metadata persist across events."""

    def __init__(self, total: str = "500.00") -> None:
        self.row: Dict[str, Any] = {
            "order_id": "ORD_PARTIAL",
            "merchant_id": "m_partial",
            "payment_intent_id": "pi_partial",
            "total": total,
            "total_refunded": "0.00",
            "currency": "USD",
            "metadata": {},
        }
        self.statuses: List[str] = []

    @property
    def total_refunded(self) -> Decimal:
        return Decimal(str(self.row["total_refunded"]))

    @property
    def ledger(self) -> Dict[str, str]:
        return dict((self.row.get("metadata") or {}).get("stripe_refund_ledger") or {})


def _install(monkeypatch: pytest.MonkeyPatch, order: _StatefulOrder) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module
    import services.commerce_attribution_service as attribution_module

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Any:
        if "FROM orders" in query:
            return dict(order.row)
        raise AssertionError(f"Unexpected query: {query}")

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        order.statuses.append(status)
        if "total_refunded" in kwargs:
            order.row["total_refunded"] = str(kwargs["total_refunded"])
        if "metadata" in kwargs:
            order.row["metadata"] = kwargs["metadata"]

    async def fake_update_order(order_id: str, fields: Dict[str, Any]) -> None:
        if "metadata" in fields:
            order.row["metadata"] = fields["metadata"]

    async def fake_log_order_event(**kwargs: Any) -> None:
        return None

    async def fake_attach(**kwargs: Any) -> None:
        return None

    async def fake_record_event(**kwargs: Any) -> bool:
        return False  # never a duplicate: each event is distinct

    async def fake_mark_status(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_get_order(order_id: str) -> Optional[Dict[str, Any]]:
        return dict(order.row) if order_id == order.row["order_id"] else None

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", SECRET, raising=False)
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(webhook_routes_module, "update_order", fake_update_order)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(
        webhook_routes_module, "_record_stripe_webhook_event_best_effort", fake_record_event
    )
    monkeypatch.setattr(
        webhook_routes_module, "_mark_stripe_webhook_event_status_best_effort", fake_mark_status
    )
    monkeypatch.setattr(attribution_module, "attach_refund_to_attribution_edge", fake_attach)


async def _send(monkeypatch: pytest.MonkeyPatch, event_type: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    import routes.webhook_routes as webhook_routes_module

    def fake_construct_event(payload: bytes, signature: Optional[str], secret: str) -> Dict[str, Any]:
        return {"type": event_type, "data": {"object": obj}}

    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook, "construct_event", staticmethod(fake_construct_event)
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_partial"}',
            headers={"stripe-signature": "sig_partial"},
        )
    return resp.json()


def _refund_updated(refund_id: str, amount_minor: int) -> Dict[str, Any]:
    return {
        "id": refund_id,
        "status": "succeeded",
        "payment_intent": "pi_partial",
        "amount": amount_minor,
        "currency": "usd",
        "metadata": {},
    }


@pytest.mark.asyncio
async def test_two_sequential_partial_refunds_reach_the_full_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """$300 then $200 on a $500 order. The ceiling used to leave this at 300 —
    max(300, 200) — under-reporting the refund and leaving the order in
    `partially_refunded`."""
    order = _StatefulOrder()
    _install(monkeypatch, order)

    await _send(monkeypatch, "refund.updated", _refund_updated("re_first", 30000))
    assert order.total_refunded == Decimal("300")
    assert order.statuses[-1] == "partially_refunded"

    await _send(monkeypatch, "refund.updated", _refund_updated("re_second", 20000))
    assert order.total_refunded == Decimal("500")
    assert order.statuses[-1] == "refunded"
    assert order.ledger == {"re_first": "300", "re_second": "200"}


@pytest.mark.asyncio
async def test_charge_refunded_first_does_not_double_count_the_same_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression the naive fix would introduce. `charge.refunded` carries
    the CUMULATIVE amount under key `stripe:ch_…`; the matching `refund.updated`
    carries the same money under `stripe:re_…`, so psp_refund_refs does not
    dedupe it. Summing blindly lands at 600 on a $500 order."""
    order = _StatefulOrder()
    _install(monkeypatch, order)

    await _send(
        monkeypatch,
        "charge.refunded",
        {
            "id": "ch_partial",
            "payment_intent": "pi_partial",
            "amount_refunded": 30000,
            "currency": "usd",
        },
    )
    assert order.total_refunded == Decimal("300")

    await _send(monkeypatch, "refund.updated", _refund_updated("re_same_money", 30000))
    assert order.total_refunded == Decimal("300")


@pytest.mark.asyncio
async def test_redelivered_refund_updated_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stripe re-delivers; the same refund id must not add twice."""
    order = _StatefulOrder()
    _install(monkeypatch, order)

    await _send(monkeypatch, "refund.updated", _refund_updated("re_dupe", 30000))
    await _send(monkeypatch, "refund.updated", _refund_updated("re_dupe", 30000))

    assert order.total_refunded == Decimal("300")
    assert order.ledger == {"re_dupe": "300"}


@pytest.mark.asyncio
async def test_a_rolled_back_refund_is_not_resurrected_by_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finalizer drops a rolled-back refund from psp_refund_records. If the
    ledger kept it, the NEXT refund's cumulative would silently add it back."""
    order = _StatefulOrder()
    _install(monkeypatch, order)

    await _send(monkeypatch, "refund.updated", _refund_updated("re_will_fail", 30000))
    assert order.total_refunded == Decimal("300")

    await _send(
        monkeypatch,
        "refund.failed",
        {
            "id": "re_will_fail",
            "status": "failed",
            "payment_intent": "pi_partial",
            "amount": 30000,
            "currency": "usd",
            "failure_reason": "expired_or_canceled_card",
            "metadata": {},
        },
    )
    assert order.total_refunded == Decimal("0")
    assert "re_will_fail" not in order.ledger

    await _send(monkeypatch, "refund.updated", _refund_updated("re_retry", 20000))
    assert order.total_refunded == Decimal("200")


@pytest.mark.asyncio
async def test_single_full_refund_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression control: the common case must behave exactly as before."""
    order = _StatefulOrder()
    _install(monkeypatch, order)

    await _send(monkeypatch, "refund.updated", _refund_updated("re_full", 50000))

    assert order.total_refunded == Decimal("500")
    assert order.statuses[-1] == "refunded"


@pytest.mark.asyncio
async def test_a_corrupt_ledger_entry_cannot_shrink_the_refunded_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed stored value is skipped, and the amount just received still
    forms a floor — a bad row must never quietly reduce what was refunded."""
    order = _StatefulOrder()
    order.row["metadata"] = {"stripe_refund_ledger": {"re_old": "not-a-number"}}
    _install(monkeypatch, order)

    await _send(monkeypatch, "refund.updated", _refund_updated("re_new", 20000))

    assert order.total_refunded == Decimal("200")


@pytest.mark.asyncio
async def test_a_refund_without_an_id_still_applies_its_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refund with no id cannot be keyed into the ledger, so the ledger sum
    does not include it. Without the floor, the event would apply a STALE
    cumulative — here 0 — and silently drop a real refund. The floor is what
    keeps the amount just received from being lost."""
    order = _StatefulOrder()
    _install(monkeypatch, order)

    await _send(
        monkeypatch,
        "refund.updated",
        {
            "status": "succeeded",
            "payment_intent": "pi_partial",
            "amount": 20000,
            "currency": "usd",
            "metadata": {},
        },
    )

    assert order.total_refunded == Decimal("200")
    assert order.ledger == {}
