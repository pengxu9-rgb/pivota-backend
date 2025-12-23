import asyncio
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient
import httpx

from main import app
from routes import agent_shop_gateway
from services.agent_task_manager import AgentTaskManager


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _base_multi_body() -> Dict[str, Any]:
    return {
        "operation": "find_products_multi",
        "payload": {
            "search": {
                "query": "test",
                "page": 1,
                "limit": 10,
                "in_stock_only": False,
            },
        },
        "metadata": {},
    }


@pytest.mark.asyncio
async def test_invoke_find_products_multi_uses_queue_and_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    # Use a small, but non-restrictive manager for this test.
    agent_shop_gateway.agent_task_manager = AgentTaskManager(
        max_workers=4,
        max_queue_size=16,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=10,
    )

    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        return {
            "products": [],
            "total": 0,
            "page": 1,
            "page_size": 0,
            "reply": "ok",
        }

    monkeypatch.setattr(agent_shop_gateway, "_handle_find_products_multi", fake_handler)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        resp = await async_client.post("/agent/shop/v1/invoke", json=_base_multi_body())

    assert resp.status_code == 200
    body = resp.json()
    assert body["products"] == []
    assert body["reply"] == "ok"


@pytest.mark.asyncio
async def test_queue_backpressure_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only 1 worker and no queue capacity: second concurrent request must see 429.
    agent_shop_gateway.agent_task_manager = AgentTaskManager(
        max_workers=1,
        max_queue_size=0,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=100,
    )

    async def slow_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        await asyncio.sleep(0.3)
        return {
            "products": [],
            "total": 0,
            "page": 1,
            "page_size": 0,
        }

    monkeypatch.setattr(agent_shop_gateway, "_handle_find_products_multi", slow_handler)

    body = _base_multi_body()
    # Ensure no session id is derived so global queue backpressure (not single-flight) is exercised.
    body["metadata"] = {}
    body["payload"].pop("user", None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        first = async_client.post("/agent/shop/v1/invoke", json=body)
        second = async_client.post("/agent/shop/v1/invoke", json=body)
        resp1, resp2 = await asyncio.gather(first, second)

    # One request should succeed, the other should be rejected due to queue capacity.
    codes = {resp1.status_code, resp2.status_code}
    assert codes == {200, 429}


@pytest.mark.asyncio
async def test_loop_detection_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_shop_gateway.agent_task_manager = AgentTaskManager(
        max_workers=2,
        max_queue_size=4,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=2,
    )

    async def immediate_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        return {
            "products": [],
            "total": 0,
            "page": 1,
            "page_size": 0,
        }

    monkeypatch.setattr(agent_shop_gateway, "_handle_find_products_multi", immediate_handler)

    body = _base_multi_body()
    body["metadata"] = {
        "creator_id": "creator-loop",
        "source": "creator-agent-ui",
    }
    body["payload"]["user"] = {"id": "user-loop", "recent_queries": ["test"]}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        resp1 = await async_client.post("/agent/shop/v1/invoke", json=body)
        resp2 = await async_client.post("/agent/shop/v1/invoke", json=body)
        resp3 = await async_client.post("/agent/shop/v1/invoke", json=body)

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp3.status_code == 429
    assert resp3.json()["detail"] == "TOOL_LOOP_DETECTED"
