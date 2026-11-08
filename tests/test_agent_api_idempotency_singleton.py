from __future__ import annotations

from typing import Any

import pytest


class _ObservedAgentOrderIdempotencyStore:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, Any]] = []

    async def put(self, *, scope: str, key: str, value: dict[str, Any]) -> None:
        self.put_calls.append(
            {
                "store_id": id(self),
                "scope": scope,
                "key": key,
                "value": value,
            }
        )


@pytest.mark.asyncio
async def test_agent_order_idempotency_store_singleton_reused_across_cache_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as module

    store = _ObservedAgentOrderIdempotencyStore()
    monkeypatch.setattr(module, "_AGENT_ORDER_IDEMPOTENCY_STORE", store)

    await module._cache_agent_order_create_response_best_effort(
        "idem_1", {"order_id": "ord_1"}, agent_id="agent_1", intent_id=None
    )
    await module._cache_agent_order_create_response_best_effort(
        "idem_2", {"order_id": "ord_2"}, agent_id="agent_1", intent_id=None
    )

    assert [call["store_id"] for call in store.put_calls] == [id(store), id(store)]
    assert [call["key"] for call in store.put_calls] == ["agent:agent_1:idem_1", "agent:agent_1:idem_2"]
