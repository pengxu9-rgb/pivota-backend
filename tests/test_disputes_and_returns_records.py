from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from services.dispute_records_service import _normalize_dispute_status, upsert_stripe_dispute_record_best_effort
from services.return_records_service import _normalize_return_status


def test_normalize_stripe_dispute_status() -> None:
    assert _normalize_dispute_status(source="stripe", raw=None) == "open"
    assert _normalize_dispute_status(source="stripe", raw="needs_response") == "needs_response"
    assert _normalize_dispute_status(source="stripe", raw="under_review") == "under_review"
    assert _normalize_dispute_status(source="stripe", raw="won") == "won"
    assert _normalize_dispute_status(source="stripe", raw="lost") == "lost"
    assert _normalize_dispute_status(source="stripe", raw="warning_closed") == "closed"


def test_normalize_shopify_return_status() -> None:
    assert _normalize_return_status(None) == "open"
    assert _normalize_return_status("OPEN") == "open"
    assert _normalize_return_status("closed") == "closed"
    assert _normalize_return_status("cancelled") == "cancelled"


class _FakeDb:
    def __init__(self) -> None:
        self.execute_calls = 0

    async def fetch_one(self, _query: str, _values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return None

    async def execute(self, _query: str, _values: Dict[str, Any]) -> None:
        self.execute_calls += 1


@pytest.mark.asyncio
async def test_upsert_stripe_dispute_skips_when_merchant_unknown() -> None:
    db = _FakeDb()
    await upsert_stripe_dispute_record_best_effort(
        {"id": "dp_test", "status": "needs_response"},
        event_type="charge.dispute.created",
        db=db,
    )
    assert db.execute_calls == 0


@pytest.mark.asyncio
async def test_upsert_stripe_dispute_upserts_when_merchant_hint_present() -> None:
    db = _FakeDb()
    await upsert_stripe_dispute_record_best_effort(
        {"id": "dp_test", "status": "needs_response", "metadata": {"merchant_id": "merch_x", "order_id": "ORD_X"}},
        event_type="charge.dispute.created",
        db=db,
    )
    assert db.execute_calls == 1
