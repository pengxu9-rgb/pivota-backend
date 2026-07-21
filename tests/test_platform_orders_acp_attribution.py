"""P-T2.0 attribution parity on the ACP (Tier-2 in-chat protocol) path.

Verifies the two halves of the round-trip:
  1. create-ACP-checkout threads the pvt_click_id/surface into the provider
     checkout metadata (so it can come back on the completed webhook) and
     persists it onto the platform order.
  2. the order-completed webhook deposits a commerce_attribution_edge with that
     same pvt_click_id — exactly like the redirect floor deposits one.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


class _DummyResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _DummyAsyncClient:
    """Captures the outbound ACP checkout_sessions POST body."""

    captured: List[Dict[str, Any]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None, **_kwargs):
        _DummyAsyncClient.captured.append({"url": url, "json": json, "headers": headers})
        return _DummyResponse(200, {"id": "acp_sess_1", "totals": [{"type": "total", "amount": 4599}]})


class _FakeRequest:
    def __init__(self, body: bytes):
        self._body = body
        self.headers: Dict[str, str] = {}

    async def body(self) -> bytes:
        return self._body


@pytest.mark.asyncio
async def test_create_acp_checkout_threads_click_id_into_metadata(monkeypatch):
    import httpx
    from fastapi import BackgroundTasks
    from routes import platform_orders_acp as acp

    _DummyAsyncClient.captured = []
    persisted: List[Dict[str, Any]] = []

    platform_order = {
        "order_id": "PLAT_ORD_1",
        "merchant_id": "merch_acp",
        "platform": "amazon",
        "data": {
            "items": [{"sku": "SKU1", "quantity": 1, "currency": "USD"}],
            "buyer_name": "ACP Buyer",
            # The Tier-2 click id, stamped upstream at order intake.
            "pvt_click_id": "clk_tier2_abc",
            "pvt_surface": "chatgpt",
        },
    }

    async def fake_get_platform_order_by_id(order_id: int):
        assert order_id == 1
        return dict(platform_order)

    async def fake_update_platform_order_data(order_id: int, updates: Dict[str, Any]):
        persisted.append({"order_id": order_id, "updates": dict(updates)})
        return True

    monkeypatch.setattr(acp, "get_platform_order_by_id", fake_get_platform_order_by_id)
    monkeypatch.setattr(acp, "update_platform_order_data", fake_update_platform_order_data)
    monkeypatch.setattr(acp, "can_access_merchant", lambda user, mid: True)
    monkeypatch.setattr(acp, "_resolve_acp_bearer_token", lambda: "test")
    monkeypatch.setattr(httpx, "AsyncClient", _DummyAsyncClient)

    bg = BackgroundTasks()
    resp = await acp.create_acp_checkout_for_platform_order(
        1, bg, current_user={"user_id": "u1"}
    )
    assert resp["status"] == "success"
    assert resp["session_id"] == "acp_sess_1"

    # The click id + surface rode into the provider checkout metadata.
    sent_meta = _DummyAsyncClient.captured[0]["json"]["metadata"]
    assert sent_meta["pvt_click_id"] == "clk_tier2_abc"
    assert sent_meta["pvt_surface"] == "chatgpt"
    assert sent_meta["platform_order_id"] == "PLAT_ORD_1"

    # And it was persisted onto the platform order (fallback carrier). Run the
    # background task that writes the ACP session back.
    await bg()
    assert persisted, "expected a platform order writeback"
    assert persisted[0]["updates"]["pvt_click_id"] == "clk_tier2_abc"


@pytest.mark.asyncio
async def test_acp_completed_webhook_deposits_attribution_edge(monkeypatch):
    from fastapi import BackgroundTasks
    from routes import platform_orders_acp as acp

    edge_calls: List[Dict[str, Any]] = []

    async def fake_get_platform_order_by_id(order_id: int):
        assert order_id == 1
        return {
            "order_id": "PLAT_ORD_1",
            "merchant_id": "merch_acp",
            "data": {"pvt_click_id": "clk_tier2_abc", "pvt_surface": "chatgpt"},
        }

    async def fake_update_platform_order_data(order_id: int, updates: Dict[str, Any]):
        return True

    async def fake_upsert_order_attribution_edge(*, order_id, merchant_id, metadata):
        edge_calls.append(
            {"order_id": order_id, "merchant_id": merchant_id, "metadata": dict(metadata)}
        )
        return {"edge_id": "cae_test", "order_id": order_id}

    # No webhook secret configured + non-prod → signature check is skipped.
    monkeypatch.setattr(acp.settings, "platform_orders_acp_webhook_secret", None, raising=False)
    monkeypatch.setattr(acp, "get_platform_order_by_id", fake_get_platform_order_by_id)
    monkeypatch.setattr(acp, "update_platform_order_data", fake_update_platform_order_data)
    monkeypatch.setattr(acp, "upsert_order_attribution_edge", fake_upsert_order_attribution_edge)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "test")

    import json as _json

    body = _json.dumps(
        {
            "id": "evt_1",
            "order_id": "acp_order_9",
            "checkout_session_id": "acp_sess_1",
            "status": "completed",
            # Provider echoes the metadata we stamped at checkout create.
            "metadata": {
                "platform_order_db_id": "1",
                "platform_order_id": "PLAT_ORD_1",
                "pvt_click_id": "clk_tier2_abc",
                "pvt_surface": "chatgpt",
            },
        }
    ).encode("utf-8")

    resp = await acp.handle_acp_order_completed_webhook(
        _FakeRequest(body), BackgroundTasks(), Merchant_Signature=None
    )
    assert resp["payment_status"] == "paid"

    assert len(edge_calls) == 1, "expected exactly one attribution edge deposit"
    call = edge_calls[0]
    assert call["order_id"] == "acp_order_9"
    assert call["merchant_id"] == "merch_acp"
    assert call["metadata"]["pvt_click_id"] == "clk_tier2_abc"
    assert call["metadata"]["protocol_name"] == "acp"


@pytest.mark.asyncio
async def test_acp_completed_webhook_skips_edge_when_no_signal(monkeypatch):
    """An order with no attribution signal must NOT fabricate an edge."""
    from fastapi import BackgroundTasks
    from routes import platform_orders_acp as acp

    edge_calls: List[Dict[str, Any]] = []

    async def fake_get_platform_order_by_id(order_id: int):
        return {"order_id": "PLAT_ORD_2", "merchant_id": "merch_acp", "data": {}}

    async def fake_update_platform_order_data(order_id: int, updates: Dict[str, Any]):
        return True

    async def fake_upsert_order_attribution_edge(*, order_id, merchant_id, metadata):
        edge_calls.append({"order_id": order_id})
        return None

    monkeypatch.setattr(acp.settings, "platform_orders_acp_webhook_secret", None, raising=False)
    monkeypatch.setattr(acp, "get_platform_order_by_id", fake_get_platform_order_by_id)
    monkeypatch.setattr(acp, "update_platform_order_data", fake_update_platform_order_data)
    monkeypatch.setattr(acp, "upsert_order_attribution_edge", fake_upsert_order_attribution_edge)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "test")

    import json as _json

    body = _json.dumps(
        {
            "id": "evt_2",
            "order_id": "acp_order_10",
            "status": "completed",
            "metadata": {"platform_order_db_id": "2", "platform_order_id": "PLAT_ORD_2"},
        }
    ).encode("utf-8")

    resp = await acp.handle_acp_order_completed_webhook(
        _FakeRequest(body), BackgroundTasks(), Merchant_Signature=None
    )
    assert resp["payment_status"] == "paid"
    assert edge_calls == [], "no attribution signal → no edge deposit"
