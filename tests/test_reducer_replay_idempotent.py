from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from services.pcs_reducer import reduce_merchant


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class InMemoryReducerStore:
    facts: List[Dict[str, Any]] = field(default_factory=list)
    checkpoints: Dict[Tuple[str, str, str], Dict[str, Any]] = field(default_factory=dict)
    orders_current: Dict[Tuple[str, str], Dict[str, Any]] = field(default_factory=dict)

    async def get_checkpoint(self, *, merchant_id: str, stream_id: str, reducer_name: str) -> Optional[Dict[str, Any]]:
        return self.checkpoints.get((merchant_id, stream_id, reducer_name))

    async def upsert_checkpoint(self, *, merchant_id: str, stream_id: str, reducer_name: str, checkpoint: Dict[str, Any]) -> None:
        self.checkpoints[(merchant_id, stream_id, reducer_name)] = dict(checkpoint)

    async def list_new_facts(
        self,
        *,
        merchant_id: str,
        stream_id: str,
        since_received_at: Optional[datetime],
        since_row_id: Optional[int],
        limit: int,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for f in self.facts:
            if f.get("merchant_id") != merchant_id or f.get("stream_id") != stream_id:
                continue
            rcv = f.get("received_at")
            if since_received_at is not None:
                if since_row_id is None:
                    if rcv < since_received_at:
                        continue
                else:
                    if rcv < since_received_at:
                        continue
                    if rcv == since_received_at and int(f.get("id")) <= int(since_row_id):
                        continue
            out.append(dict(f))
        out.sort(key=lambda x: (x.get("received_at"), x.get("id")))
        return out[:limit]

    async def list_all_facts_for_order(self, *, merchant_id: str, stream_id: str, order_id: str) -> List[Dict[str, Any]]:
        out = [
            dict(f)
            for f in self.facts
            if f.get("merchant_id") == merchant_id and f.get("stream_id") == stream_id and str(f.get("order_id")) == str(order_id)
        ]
        out.sort(key=lambda x: (x.get("occurred_at") or datetime(1970, 1, 1, tzinfo=timezone.utc), x.get("received_at"), str(x.get("fact_id"))))
        return out

    async def upsert_orders_current(
        self,
        *,
        merchant_id: str,
        order_id: str,
        current_state_json: Dict[str, Any],
        last_fact_occurred_at: Optional[datetime],
        last_dedupe_key: Optional[str],
    ) -> None:
        self.orders_current[(merchant_id, str(order_id))] = {
            "current_state_json": dict(current_state_json),
            "last_fact_occurred_at": last_fact_occurred_at,
            "last_dedupe_key": last_dedupe_key,
        }


@pytest.mark.asyncio
async def test_reducer_replay_twice_is_idempotent():
    merchant_id = "m_1"
    order_id = "1001"

    store = InMemoryReducerStore(
        facts=[
            {
                "id": 1,
                "merchant_id": merchant_id,
                "stream_id": "orders",
                "order_id": order_id,
                "fact_id": "f1",
                "fact_type": "shopify.orders.updated",
                "occurred_at": _ts("2025-01-01T00:00:00Z"),
                "received_at": _ts("2025-01-01T00:00:05Z"),
                "dedupe_key": "shopify:1",
                "payload_json": {"id": int(order_id), "financial_status": "paid", "fulfillment_status": None, "currency": "USD", "total_price": "10.00"},
            }
        ]
    )

    r1 = await reduce_merchant(merchant_id=merchant_id, store=store)
    cur1 = store.orders_current[(merchant_id, order_id)]["current_state_json"]

    r2 = await reduce_merchant(merchant_id=merchant_id, store=store)
    cur2 = store.orders_current[(merchant_id, order_id)]["current_state_json"]

    assert cur1 == cur2
    assert r1.orders_updated == 1
    assert r2.orders_updated == 0

