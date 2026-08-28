"""Livemode gate on the Stripe PSP webhook: test-psp probe exemption.

In production the webhook drops livemode=false events unconditionally — EXCEPT
for the controlled test-processor probe: an event whose order resolves to a
merchant in TEST_PSP_PROBE_MERCHANTS, while ALLOW_TEST_PSP_PROBE is on, and
whose order metadata itself requested the test-psp bypass (the same conjuncts
order_routes._resolve_order_live_readiness_requirement used to allow the
test-mode charge). These tests pin every conjunct:

1. probe merchant + env armed + order requested bypass → processed (marks paid)
2. non-allowlisted merchant, env armed → dropped
3. probe merchant, env off → dropped
4. probe merchant + env armed, order never requested the bypass → dropped
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app


PROBE_MERCHANT = "merch_efbc46b4619cfbdf"


def _test_mode_success_event(order_metadata_hint: str = "") -> Dict[str, Any]:
    obj: Dict[str, Any] = {
        "id": "pi_probe_test_mode",
        "amount": 4520,
        "currency": "usd",
    }
    if order_metadata_hint:
        obj["metadata"] = {"order_id": order_metadata_hint}
    return {
        "type": "payment_intent.succeeded",
        "livemode": False,
        "id": "evt_probe_test_mode",
        "data": {"object": obj},
    }


def _order_row(merchant_id: str, *, requested_bypass: bool) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {"agent_v2": {"hosted_checkout": True}}
    if requested_bypass:
        metadata["allow_test_psp_surfaces"] = True
    return {
        "order_id": "ORD_PROBE_TEST_MODE",
        "merchant_id": merchant_id,
        "payment_intent_id": "pi_probe_test_mode",
        "payment_status": "awaiting_payment",
        "psp_used": "stripe",
        "total": "45.20",
        "currency": "usd",
        "metadata": metadata,
    }


def _install_webhook_plumbing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    event: Dict[str, Any],
    order_row: Dict[str, Any],
) -> Dict[str, list]:
    """Wire signature verification + DB resolution; record downstream calls."""
    import db.database as database_module
    import routes.merchant_onboarding_routes as merchant_onboarding_module
    import routes.order_routes as order_routes_module
    import routes.webhook_routes as webhook_routes_module

    calls: Dict[str, list] = {"paid": [], "fetch_one": []}

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        calls["fetch_one"].append(dict(values))
        if values.get("payment_intent_id") == order_row["payment_intent_id"]:
            return dict(order_row)
        return None

    async def fake_mark_order_paid(order_id: str) -> bool:
        calls["paid"].append(order_id)
        return True

    async def fake_log_order_event(**kwargs: Any) -> None:
        return None

    async def fake_create_order_snapshot_evidence_pack(order_id: str, triggered_by: str) -> None:
        return None

    async def fake_get_primary_store(merchant_id: str) -> Dict[str, Any]:
        return {"platform": "shopify"}

    async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": merchant_id, "status": "active"}

    async def fake_create_shopify_order(order_id: str) -> bool:
        return True

    async def fake_emit_merchant_webhook_event(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "delivered"}

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(
        webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False
    )
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(
        webhook_routes_module,
        "create_order_snapshot_evidence_pack",
        fake_create_order_snapshot_evidence_pack,
    )
    monkeypatch.setattr(webhook_routes_module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(
        merchant_onboarding_module,
        "get_merchant_onboarding",
        fake_get_merchant_onboarding,
    )
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fake_create_shopify_order)
    monkeypatch.setattr(
        webhook_routes_module,
        "emit_merchant_webhook_event",
        fake_emit_merchant_webhook_event,
    )
    return calls


async def _post_webhook() -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_probe_test_mode"}',
            headers={"stripe-signature": "sig_probe"},
        )


@pytest.mark.asyncio
async def test_prod_processes_test_mode_event_for_armed_probe_merchant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _test_mode_success_event()
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        order_row=_order_row(PROBE_MERCHANT, requested_bypass=True),
    )
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", PROBE_MERCHANT)

    resp = await _post_webhook()

    assert resp.status_code == 200
    body = resp.json()
    # The event must be PROCESSED — not dropped as test_mode_event_in_production —
    # and the finalizer must actually run (webhook-driven mark_order_paid fires).
    assert body.get("reason") != "test_mode_event_in_production"
    assert body["status"] == "success"
    assert body["event"] == "payment_intent.succeeded"
    assert calls["paid"] == ["ORD_PROBE_TEST_MODE"]
    # The exemption resolved the order through the real resolver (DB lookup ran).
    assert any(
        v.get("payment_intent_id") == "pi_probe_test_mode" for v in calls["fetch_one"]
    )


@pytest.mark.asyncio
async def test_prod_drops_test_mode_event_for_non_allowlisted_merchant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _test_mode_success_event()
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        order_row=_order_row("merch_someone_else", requested_bypass=True),
    )
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", PROBE_MERCHANT)

    resp = await _post_webhook()

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ignored",
        "event": "payment_intent.succeeded",
        "reason": "test_mode_event_in_production",
    }
    assert calls["paid"] == []
    # The gate consulted the resolver (allowlist decision was made on the real
    # order row, not short-circuited before resolution).
    assert any(
        v.get("payment_intent_id") == "pi_probe_test_mode" for v in calls["fetch_one"]
    )


@pytest.mark.asyncio
async def test_prod_drops_test_mode_event_for_probe_merchant_when_env_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _test_mode_success_event()
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        order_row=_order_row(PROBE_MERCHANT, requested_bypass=True),
    )
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("ALLOW_TEST_PSP_PROBE", raising=False)
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", PROBE_MERCHANT)

    resp = await _post_webhook()

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ignored",
        "event": "payment_intent.succeeded",
        "reason": "test_mode_event_in_production",
    }
    assert calls["paid"] == []


@pytest.mark.asyncio
async def test_prod_drops_test_mode_event_when_order_never_requested_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _test_mode_success_event()
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        order_row=_order_row(PROBE_MERCHANT, requested_bypass=False),
    )
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", PROBE_MERCHANT)

    resp = await _post_webhook()

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ignored",
        "event": "payment_intent.succeeded",
        "reason": "test_mode_event_in_production",
    }
    assert calls["paid"] == []
