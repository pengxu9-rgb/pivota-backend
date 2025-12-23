from __future__ import annotations

import pytest

from mvp.idempotency import InMemoryIdempotencyStore


@pytest.mark.asyncio
async def test_inmemory_idempotency_roundtrip():
    store = InMemoryIdempotencyStore()
    assert await store.get(scope="refund", key="k") is None
    rec = await store.put(scope="refund", key="k", value={"ok": True})
    assert rec.value["ok"] is True
    rec2 = await store.get(scope="refund", key="k")
    assert rec2 is not None
    assert rec2.value["ok"] is True

