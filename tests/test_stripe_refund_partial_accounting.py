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
    def refund_rows(self) -> Dict[str, str]:
        """The REFUND-LEVEL rows of psp_refund_records, as {refund_id: amount}.

        This is the state the cumulative is derived from — maintained by
        finalize_refund_success through update_order_status (an additive merge
        against a fresh read), and pruned by the failure path.
        """
        records = (self.row.get("metadata") or {}).get("psp_refund_records") or {}
        return {
            str(r.get("refund_reference") or ""): str(r.get("amount_minor") or "")
            for r in records.values()
            if isinstance(r, dict) and r.get("source_event") == "refund.updated"
        }


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
    # These flows must never 5xx; a deferral here would silently look like a
    # no-op to every caller below.
    assert resp.status_code == 200, (resp.status_code, resp.text)
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
    assert order.refund_rows == {"re_first": "30000", "re_second": "20000"}


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

    body = await _send(monkeypatch, "refund.updated", _refund_updated("re_same_money", 30000))
    # Positive evidence the refund.updated was actually HANDLED — without this,
    # "still 300" passes just as well if the event was refused or never ran.
    assert body == {"status": "success", "event": "refund.updated"}
    assert order.refund_rows == {"re_same_money": "30000"}
    assert order.total_refunded == Decimal("300")


@pytest.mark.asyncio
async def test_redelivered_refund_updated_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stripe re-delivers; the same refund id must not add twice.

    The amount is deliberately SMALL relative to the order total. At $300 a
    double-count would sum to $600, trip the over-refund bound, and be refused —
    so the total would read correctly for the wrong reason and the test would
    pass against a broken exclusion. At $100 the double-count is visible as $200.
    """
    order = _StatefulOrder()
    _install(monkeypatch, order)

    first = await _send(monkeypatch, "refund.updated", _refund_updated("re_dupe", 10000))
    second = await _send(monkeypatch, "refund.updated", _refund_updated("re_dupe", 10000))

    assert first == {"status": "success", "event": "refund.updated"}
    assert second == {"status": "success", "event": "refund.updated"}
    assert order.total_refunded == Decimal("100")
    assert order.refund_rows == {"re_dupe": "10000"}


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
    assert "re_will_fail" not in order.refund_rows

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
async def test_a_corrupt_refund_row_cannot_shrink_the_refunded_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed stored amount is skipped rather than wedging the refund, and
    the amount just received is still applied in full."""
    order = _StatefulOrder()
    order.row["metadata"] = {
        "psp_refund_records": {
            "stripe:re_old": {
                "refund_reference": "re_old",
                "amount_minor": "not-a-number",
                "currency": "usd",
                "source_event": "refund.updated",
            }
        }
    }
    _install(monkeypatch, order)

    await _send(monkeypatch, "refund.updated", _refund_updated("re_new", 20000))

    assert order.total_refunded == Decimal("200")


@pytest.mark.asyncio
async def test_a_refund_without_an_id_still_applies_its_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refund with no id cannot be matched against a prior row, so it must
    still contribute its own amount rather than resolving to a stale total."""
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


@pytest.mark.asyncio
async def test_a_redelivery_does_not_erase_that_refund_from_the_cumulative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression on the derivation itself.

    `psp_refund_records[...]["amount"]` is the DELTA applied, which the
    cumulative branch recomputes as `next_total_refunded - current_total_refunded`
    and so zeroes on a redelivery. Summing that field made a redelivered refund
    contribute 0, and the NEXT refund then under-counted by the whole redelivered
    amount. The face amount (`amount_minor`) is what stays stable."""
    order = _StatefulOrder()
    _install(monkeypatch, order)

    await _send(monkeypatch, "refund.updated", _refund_updated("re_a", 30000))
    await _send(monkeypatch, "refund.updated", _refund_updated("re_a", 30000))  # redelivered
    assert order.total_refunded == Decimal("300")

    await _send(monkeypatch, "refund.updated", _refund_updated("re_b", 20000))
    assert order.total_refunded == Decimal("500")


@pytest.mark.asyncio
async def test_refunds_summing_past_the_order_total_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The amount guard bounded ONE refund by the order total, which was correct
    only while refund_total was a monotonic ceiling. Once refund-level events
    contribute a SUM, two $400 refunds on a $500 order each pass individually and
    would write total_refunded=800 — a number that feeds the attribution edge and
    the merchant's statement."""
    order = _StatefulOrder()
    _install(monkeypatch, order)

    first = await _send(monkeypatch, "refund.updated", _refund_updated("re_big_1", 40000))
    assert first == {"status": "success", "event": "refund.updated"}
    assert order.total_refunded == Decimal("400")

    second = await _send(monkeypatch, "refund.updated", _refund_updated("re_big_2", 40000))
    assert second["status"] == "unmatched"
    assert second["reason"].startswith("refund_exceeds_order_total:")
    # Refused before any write: the order keeps the total it legitimately had.
    assert order.total_refunded == Decimal("400")


@pytest.mark.asyncio
async def test_partials_that_exactly_reach_the_order_total_are_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is inclusive — a fully refunded order must not be refused."""
    order = _StatefulOrder()
    _install(monkeypatch, order)

    await _send(monkeypatch, "refund.updated", _refund_updated("re_x", 30000))
    await _send(monkeypatch, "refund.updated", _refund_updated("re_y", 20000))

    assert order.total_refunded == Decimal("500")
    assert order.statuses[-1] == "refunded"


@pytest.mark.asyncio
async def test_an_id_less_refund_redelivery_does_not_double_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id-less refund writes an id-less row. Self-exclusion was gated on the
    incoming id being non-empty, so on redelivery the row was summed on top of
    itself: $200 became $400."""
    order = _StatefulOrder()
    _install(monkeypatch, order)

    idless = {
        "status": "succeeded",
        "payment_intent": "pi_partial",
        "amount": 20000,
        "currency": "usd",
        "metadata": {},
    }
    await _send(monkeypatch, "refund.updated", idless)
    assert order.total_refunded == Decimal("200")

    await _send(monkeypatch, "refund.updated", dict(idless))
    assert order.total_refunded == Decimal("200")
