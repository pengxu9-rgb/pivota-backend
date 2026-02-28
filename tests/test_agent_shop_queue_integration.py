import asyncio
from typing import Any, Dict
from unittest.mock import AsyncMock

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
async def test_invoke_find_products_multi_shopping_bypasses_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_shop_gateway.agent_task_manager = AgentTaskManager(
        max_workers=1,
        max_queue_size=0,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=1,
    )
    monkeypatch.setattr(agent_shop_gateway, "INVOKE_MULTI_BYPASS_QUEUE_SHOPPING", True)

    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        return {
            "products": [{"id": "p_1"}],
            "total": 1,
            "page": 1,
            "page_size": 1,
            "reply": "bypassed",
        }

    monkeypatch.setattr(agent_shop_gateway, "_handle_find_products_multi", fake_handler)
    enqueue_mock = AsyncMock(side_effect=AssertionError("queue should not be used for shopping source"))
    monkeypatch.setattr(agent_shop_gateway.agent_task_manager, "enqueue", enqueue_mock)

    body = _base_multi_body()
    body["metadata"] = {"source": "shopping_agent"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        resp = await async_client.post("/agent/shop/v1/invoke", json=body)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["reply"] == "bypassed"
    enqueue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_invoke_find_products_multi_clamps_limit_and_includes_route_health_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_shop_gateway.agent_task_manager = AgentTaskManager(
        max_workers=1,
        max_queue_size=0,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=1,
    )
    monkeypatch.setattr(agent_shop_gateway, "INVOKE_MULTI_BYPASS_QUEUE_SHOPPING", True)

    observed: Dict[str, Any] = {}

    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        observed["limit"] = payload.search.limit
        return {
            "products": [],
            "total": 0,
            "page": 1,
            "page_size": 0,
            "reply": "ok",
            "metadata": {"query_source": "contract_probe"},
        }

    monkeypatch.setattr(agent_shop_gateway, "_handle_find_products_multi", fake_handler)

    body = _base_multi_body()
    body["payload"]["search"]["limit"] = 201
    body["metadata"] = {"source": "shopping_agent"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        resp = await async_client.post("/agent/shop/v1/invoke", json=body)

    assert resp.status_code == 200
    assert observed.get("limit") == 200
    payload = resp.json()
    meta = payload.get("metadata", {})
    route_health = meta.get("route_health", {})
    assert route_health.get("orchestrator_path") == "shop_gateway.find_products_multi"
    assert route_health.get("decision_node") == "contract_probe"
    for key in (
        "domain_filter_dropped_external",
        "semantic_retry_applied",
        "semantic_retry_hits",
        "external_seed_brand_strict_rows",
        "external_seed_brand_relevant_rows",
        "external_seed_broad_fallback_used",
        "external_seed_broad_scope_rows",
    ):
        assert key in route_health


@pytest.mark.asyncio
async def test_invoke_find_products_multi_shopping_alias_source_bypasses_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_shop_gateway.agent_task_manager = AgentTaskManager(
        max_workers=1,
        max_queue_size=0,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=1,
    )
    monkeypatch.setattr(agent_shop_gateway, "INVOKE_MULTI_BYPASS_QUEUE_SHOPPING", True)

    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        return {
            "products": [{"id": "p_1"}],
            "total": 1,
            "page": 1,
            "page_size": 1,
            "reply": "bypassed",
            "metadata": {},
        }

    monkeypatch.setattr(agent_shop_gateway, "_handle_find_products_multi", fake_handler)
    enqueue_mock = AsyncMock(side_effect=AssertionError("queue should not be used for shopping source aliases"))
    monkeypatch.setattr(agent_shop_gateway.agent_task_manager, "enqueue", enqueue_mock)

    body = _base_multi_body()
    body["metadata"] = {"source": "shopping_agent_web"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        resp = await async_client.post("/agent/shop/v1/invoke", json=body)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["reply"] == "bypassed"
    assert payload.get("metadata", {}).get("shopping_surface_detected") is True
    enqueue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_invoke_find_products_multi_payload_metadata_source_bypasses_queue_and_echoes_page_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_shop_gateway.agent_task_manager = AgentTaskManager(
        max_workers=1,
        max_queue_size=0,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=1,
    )
    monkeypatch.setattr(agent_shop_gateway, "INVOKE_MULTI_BYPASS_QUEUE_SHOPPING", True)

    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        return {
            "products": [{"id": "p_1"}],
            "total": 1,
            "page": 1,
            "page_size": 1,
            "reply": "bypassed",
            "metadata": {},
        }

    monkeypatch.setattr(agent_shop_gateway, "_handle_find_products_multi", fake_handler)
    enqueue_mock = AsyncMock(side_effect=AssertionError("queue should not be used for payload metadata shopping source"))
    monkeypatch.setattr(agent_shop_gateway.agent_task_manager, "enqueue", enqueue_mock)

    body = _base_multi_body()
    body["payload"]["metadata"] = {
        "source": "shopping_agent_web",
        "page_request_id": "pr_demo_001",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        resp = await async_client.post("/agent/shop/v1/invoke", json=body)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["reply"] == "bypassed"
    assert payload.get("metadata", {}).get("shopping_surface_detected") is True
    assert payload.get("metadata", {}).get("source_normalized") == "shopping-agent-web"
    assert payload.get("metadata", {}).get("page_request_id") == "pr_demo_001"
    enqueue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_invoke_find_products_multi_shopping_page_request_dedup_collapses_concurrent_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_shop_gateway.agent_task_manager = AgentTaskManager(
        max_workers=1,
        max_queue_size=0,
        task_timeout_seconds=5.0,
        max_calls_per_session=100,
        max_duplicate_payloads=1,
    )
    monkeypatch.setattr(agent_shop_gateway, "INVOKE_MULTI_BYPASS_QUEUE_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway, "MULTI_SEARCH_PAGE_REQUEST_DEDUP_ENABLED", True)
    monkeypatch.setattr(agent_shop_gateway, "MULTI_SEARCH_PAGE_REQUEST_DEDUP_TTL_SECONDS", 5.0)
    monkeypatch.setattr(agent_shop_gateway, "_MULTI_SEARCH_PAGE_REQUEST_CACHE", {})
    monkeypatch.setattr(agent_shop_gateway, "_MULTI_SEARCH_PAGE_REQUEST_INFLIGHT", {})

    calls = {"count": 0}

    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        calls["count"] += 1
        await asyncio.sleep(0.05)
        return {
            "products": [{"id": "p_1"}],
            "total": 1,
            "page": 1,
            "page_size": 1,
            "reply": "deduped",
            "metadata": {},
        }

    monkeypatch.setattr(agent_shop_gateway, "_handle_find_products_multi", fake_handler)

    body = _base_multi_body()
    body["metadata"] = {"source": "shopping_agent", "page_request_id": "pr_same_001"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        r1, r2 = await asyncio.gather(
            async_client.post("/agent/shop/v1/invoke", json=body),
            async_client.post("/agent/shop/v1/invoke", json=body),
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    p1 = r1.json()
    p2 = r2.json()
    assert p1["reply"] == "deduped"
    assert p2["reply"] == "deduped"
    assert calls["count"] == 1
    md1 = p1.get("metadata", {})
    md2 = p2.get("metadata", {})
    assert md1.get("page_request_dedup_enabled") is True
    assert md2.get("page_request_dedup_enabled") is True
    assert bool(md1.get("page_request_dedup_inflight_joined")) or bool(md2.get("page_request_dedup_inflight_joined"))


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
