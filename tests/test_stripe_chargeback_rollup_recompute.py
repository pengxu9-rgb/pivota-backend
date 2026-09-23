"""A Stripe chargeback reaches the refunded edge's billed day, through the real route.

The charge.dispute.* branch calls attach_dispute_to_attribution_edge with its own minor-unit
conversion, then logs chargeback_received. A chargeback stays additive per dispute id and is
kept apart from the refund ceiling (tests/test_stripe_refund_attribution_edge_postgres.py). This drives the real ASGI route and the real attach,
stubbing only its three writes (the edge UPDATE, the event, the rollup recompute), so the wiring,
the units and the recompute call are all checked. The recompute itself is covered on Postgres in
tests/test_refund_rollup_recompute_postgres.py.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app


EDGE = {
    "edge_id": "cae_cb",
    "merchant_id": "m_stripe",
    "created_at": datetime(2026, 9, 10, 12, tzinfo=timezone.utc),
    "metadata": None,
}


def _install(monkeypatch: pytest.MonkeyPatch, calls: Dict[str, list], *, event_raises: bool = False) -> None:
    import routes.webhook_routes as webhook_routes_module
    import services.commerce_attribution_service as attribution_module
    import services.dispute_records_service as dispute_records_module
    import services.pcs_evidence_pack_service as pcs_module

    event = {
        "type": "charge.dispute.created",
        "data": {
            "object": {
                "id": "dp_cb_1",
                "status": "needs_response",
                "charge": "ch_cb_1",
                "amount": 1500,
                "currency": "usd",
                "metadata": {"merchant_id": "m_stripe", "order_id": "ORD_CB"},
            }
        },
    }

    async def fake_apply(**kwargs: Any):
        calls["apply"].append(kwargs)
        return [dict(EDGE)]

    async def fake_emit(rows, **kwargs: Any) -> None:
        calls["emit"].append(kwargs)
        if event_raises:
            raise TypeError("'str' object is not a mapping")

    async def fake_recompute(rows) -> Dict[Any, str]:
        calls["recompute"].append([r["edge_id"] for r in rows])
        return {}

    async def fake_log_order_event(**kwargs: Any) -> None:
        calls["order_events"].append(kwargs)

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def fail_refund_writer(**kwargs: Any):
        raise AssertionError("a chargeback must stay out of the refund writers")

    monkeypatch.delenv("ATTRIBUTION_REVERSE_ON_CHARGEBACK", raising=False)
    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook, "construct_event", staticmethod(lambda *a, **k: event)
    )
    monkeypatch.setattr(dispute_records_module, "upsert_stripe_dispute_record_best_effort", noop)
    monkeypatch.setattr(pcs_module, "create_dispute_evidence_pack", noop)
    monkeypatch.setattr(attribution_module, "apply_attribution_dispute_rows", fake_apply)
    monkeypatch.setattr(attribution_module, "apply_attribution_refund_rows", fail_refund_writer)
    monkeypatch.setattr(attribution_module, "apply_refund_total_rows", fail_refund_writer)
    monkeypatch.setattr(attribution_module, "emit_attribution_refund_event", fake_emit)
    monkeypatch.setattr(attribution_module, "recompute_days_for_edges", fake_recompute)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)


async def _post() -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/webhooks/stripe", content=b'{"id":"evt_cb"}', headers={"stripe-signature": "sig_cb"}
        )


@pytest.mark.asyncio
async def test_a_chargeback_applies_major_units_and_recomputes_the_edges_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: Dict[str, list] = {"apply": [], "emit": [], "recompute": [], "order_events": []}
    _install(monkeypatch, calls)

    resp = await _post()

    assert resp.status_code == 200
    assert calls["apply"] == [{"order_id": "ORD_CB", "dispute_id": "dp_cb_1", "amount": Decimal("15")}]
    assert calls["recompute"] == [["cae_cb"]]
    assert [e["event_type"] for e in calls["order_events"]] == ["chargeback_received"]


@pytest.mark.asyncio
async def test_a_chargeback_whose_event_raises_still_recomputes_the_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refund event raises for any edge with metadata today (JSONB comes back as a str).
    The webhook still answers 200, and the day is still re-rolled."""
    calls: Dict[str, list] = {"apply": [], "emit": [], "recompute": [], "order_events": []}
    _install(monkeypatch, calls, event_raises=True)

    resp = await _post()

    assert resp.status_code == 200
    assert calls["recompute"] == [["cae_cb"]]
