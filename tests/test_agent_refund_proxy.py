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


class _TestAgentContext:
    agent_id = "agent_refund_proxy"
    agent_name = "Agent Refund Proxy"
    allowed_merchants = ["merchant_refund_proxy"]
    session_id = "session_refund_proxy"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return merchant_id in self.allowed_merchants


async def _override_get_agent_context() -> _TestAgentContext:
    return _TestAgentContext()


async def _override_get_agent_user_context():
    return None


@pytest.mark.asyncio
async def test_agent_refund_proxy_passes_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_api as agent_api

    captured: Dict[str, Any] = {}

    async def fake_get_order(order_id: str) -> Dict[str, Any]:
        return {
            "order_id": order_id,
            "merchant_id": "merchant_refund_proxy",
            "agent_id": "agent_refund_proxy",
            "total": "4.07",
            "currency": "EUR",
            "payment_status": "paid",
        }

    async def fake_process_refund(order_id, refund_request, background_tasks, current_user):
        captured["order_id"] = order_id
        captured["refund_request"] = refund_request
        captured["current_user"] = current_user
        return {
            "status": "success",
            "order_id": order_id,
            "refund_amount": "4.07",
        }

    async def fake_emit_agent_webhook_event(*_args, **_kwargs):
        return None

    app.dependency_overrides[agent_api.get_agent_context] = _override_get_agent_context
    app.dependency_overrides[agent_api.get_agent_user_context] = _override_get_agent_user_context
    monkeypatch.setattr(agent_api, "get_order", fake_get_order)
    monkeypatch.setattr(agent_api, "process_refund", fake_process_refund)
    monkeypatch.setattr(
        "services.agent_webhook_service.emit_agent_webhook_event",
        fake_emit_agent_webhook_event,
    )

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/agent/v1/orders/ORD_REFUND_PROXY/refund")
    finally:
        app.dependency_overrides.pop(agent_api.get_agent_context, None)
        app.dependency_overrides.pop(agent_api.get_agent_user_context, None)

    assert response.status_code == 200
    assert captured["current_user"]["role"] == "admin"
    refund_request = captured["refund_request"]
    assert refund_request.idempotency_key == "agent_refund:agent_refund_proxy:ORD_REFUND_PROXY"
    assert refund_request.amount is None
    assert refund_request.restore_inventory is True
