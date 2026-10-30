"""Each search writer maps a card's product_key to its content_key before it records the slate.

2026-10-29: the writers passed product_key as content_key (VARCHAR(40)); a long one lost the whole
slate. These drive the real routes with a card that carries only a long product_key and assert the
recorded exposure carries the resolved content_key, so a writer that skips resolve_card_identities
(or puts product_key back into content_key) fails here.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from main import app  # noqa: E402
from services import agent_decision_event_store as store  # noqa: E402

CK = "ck_" + "cd" * 16
LONG_PRODUCT_KEY = "ext:" + "w" * 60


class _AgentContext:
    agent_id = "agent_wiring"
    agent_name = "Wiring"
    allowed_merchants = ["m_wiring"]
    session_id = "session_wiring"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return merchant_id in self.allowed_merchants


def _product() -> Dict[str, Any]:
    return {
        "id": "prod_1", "product_id": "prod_1", "merchant_id": "m_wiring", "title": "Serum",
        "price": "10.00", "currency": "USD", "platform": "shopify", "source": "products_cache",
        "product_key": LONG_PRODUCT_KEY,
    }


@pytest.fixture()
def recorded(monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
    enqueued: List[tuple] = []

    async def fake_enqueue(op: str, payload: Dict[str, Any]) -> None:
        enqueued.append((op, payload))

    async def fake_fetch_all(query: str, values: Dict[str, Any]) -> List[Dict[str, Any]]:
        if "catalog_products" in query:
            return [{"product_key": k, "content_key": CK} for k in values["keys"] if k == LONG_PRODUCT_KEY]
        return []

    monkeypatch.setattr(store, "_enqueue", fake_enqueue)
    monkeypatch.setattr(store.database, "fetch_all", fake_fetch_all)
    return enqueued


async def _exposures(enqueued: List[tuple]) -> List[Dict[str, Any]]:
    for _ in range(50):
        if any(op == "exposures" for op, _ in enqueued):
            break
        await asyncio.sleep(0.01)
    return next(payload["rows"] for op, payload in enqueued if op == "exposures")


@pytest.mark.asyncio
async def test_agent_v2_search_records_the_resolved_content_key(
    monkeypatch: pytest.MonkeyPatch, recorded: List[tuple]
) -> None:
    import routes.agent_v2 as agent_v2
    from routes.agent_auth import get_agent_context

    async def fake_v1_search(**kwargs: Any) -> Dict[str, Any]:
        return {"status": "success", "products": [_product()],
                "pagination": {"total": 1, "limit": 10, "offset": 0, "has_more": False},
                "metadata": {"reason_code": "ok"}}

    async def _ctx() -> _AgentContext:
        return _AgentContext()

    app.dependency_overrides[get_agent_context] = _ctx
    monkeypatch.setattr(agent_v2, "agent_v1_search_products", fake_v1_search)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/agent/v2/products/search",
                                     json={"query": "serum", "merchant_id": "m_wiring", "limit": 10})
        assert resp.status_code == 200
        rows = await _exposures(recorded)
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
    assert [r["content_key"] for r in rows] == [CK]


@pytest.mark.asyncio
async def test_agent_sdk_fixed_search_records_the_resolved_content_key(
    monkeypatch: pytest.MonkeyPatch, recorded: List[tuple]
) -> None:
    import routes.agent_api as agent_api_module

    async def fake_agent_search_products(**kwargs: Any) -> Dict[str, Any]:
        return {"status": "success", "products": [_product()],
                "pagination": {"total_count": 1, "limit": kwargs.get("limit"), "offset": 0, "has_more": False},
                "metadata": {"reason_code": "ok", "route_health": {"primary_path_used": "test_delegate"}}}

    monkeypatch.setattr(agent_api_module, "agent_search_products", fake_agent_search_products)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/agent/v1/products/search",
            params={"query": "serum", "limit": 10, "offset": 0, "search_all_merchants": "true",
                    "allow_external_seed": "false", "allow_stale_cache": "false"},
            headers={"X-API-Key": "test-api-key"},
        )
    assert resp.status_code == 200
    rows = await _exposures(recorded)
    assert [r["content_key"] for r in rows] == [CK]
