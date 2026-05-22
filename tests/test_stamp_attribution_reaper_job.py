from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest

import jobs.stamp_attribution_reaper_job as reaper


class FakeDB:
    def __init__(self, *, rows: List[Dict[str, Any]]) -> None:
        self.rows = rows
        self.fetch_all_calls: List[tuple[str, Dict[str, Any]]] = []

    async def fetch_all(self, query: str, values: Optional[Dict[str, Any]] = None):
        self.fetch_all_calls.append((str(query), dict(values or {})))
        return list(self.rows)


def _install_stamp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_value: Optional[int] = 1,
    raise_for: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    raise_for_set = set(raise_for or [])
    calls: List[Dict[str, Any]] = []

    async def fake_stamp(order_id: str, *, subtotal: Any, discount_total: Any = None):
        calls.append(
            {"order_id": order_id, "subtotal": subtotal, "discount_total": discount_total}
        )
        if order_id in raise_for_set:
            raise RuntimeError("simulated stamp failure")
        return return_value

    monkeypatch.setattr(reaper, "stamp_gross_attributed_gmv", fake_stamp)
    return calls


@pytest.mark.asyncio
async def test_reaper_stamps_unstamped_paid_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"order_id": "ord_1", "subtotal": Decimal("100.00"), "discount_total": Decimal("0.00")},
        {"order_id": "ord_2", "subtotal": Decimal("50.00"), "discount_total": Decimal("5.00")},
    ]
    fake_db = FakeDB(rows=rows)
    monkeypatch.setattr(reaper, "database", fake_db)
    stamp_calls = _install_stamp(monkeypatch, return_value=1)

    result = await reaper.run_stamp_attribution_reaper_tick()

    assert result == {"scanned": 2, "stamped": 2, "failed": 0}
    assert [c["order_id"] for c in stamp_calls] == ["ord_1", "ord_2"]
    assert stamp_calls[0]["subtotal"] == Decimal("100.00")
    assert stamp_calls[1]["discount_total"] == Decimal("5.00")


@pytest.mark.asyncio
async def test_reaper_continues_past_per_order_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    # One order's stamp raises; the reaper must keep going and tally `failed`
    # without surfacing the exception to the scheduler tick.
    rows = [
        {"order_id": "ord_ok", "subtotal": Decimal("10.00"), "discount_total": Decimal("0.00")},
        {"order_id": "ord_bad", "subtotal": Decimal("20.00"), "discount_total": Decimal("0.00")},
        {"order_id": "ord_ok2", "subtotal": Decimal("30.00"), "discount_total": Decimal("0.00")},
    ]
    fake_db = FakeDB(rows=rows)
    monkeypatch.setattr(reaper, "database", fake_db)
    stamp_calls = _install_stamp(monkeypatch, return_value=1, raise_for=["ord_bad"])

    result = await reaper.run_stamp_attribution_reaper_tick()

    assert result == {"scanned": 3, "stamped": 2, "failed": 1}
    # All three orders were attempted (failure didn't abort the loop).
    assert [c["order_id"] for c in stamp_calls] == ["ord_ok", "ord_bad", "ord_ok2"]


@pytest.mark.asyncio
async def test_reaper_treats_zero_updated_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # stamp_gross_attributed_gmv returning 0 means the edges were already
    # stamped by a parallel path between SELECT and UPDATE. Not a failure.
    fake_db = FakeDB(rows=[
        {"order_id": "ord_x", "subtotal": Decimal("10.00"), "discount_total": Decimal("0.00")},
    ])
    monkeypatch.setattr(reaper, "database", fake_db)
    _install_stamp(monkeypatch, return_value=0)

    result = await reaper.run_stamp_attribution_reaper_tick()

    # Neither stamped (updated=0) nor failed — counted only as scanned.
    assert result == {"scanned": 1, "stamped": 0, "failed": 0}


@pytest.mark.asyncio
async def test_reaper_resilient_to_scan_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenDB:
        async def fetch_all(self, *args: Any, **kwargs: Any):
            raise RuntimeError("simulated db scan failure")

    monkeypatch.setattr(reaper, "database", BrokenDB())
    stamp_calls = _install_stamp(monkeypatch, return_value=1)

    # Must not raise — the scheduler depends on tick functions being
    # resilient so one bad tick doesn't kill the job.
    result = await reaper.run_stamp_attribution_reaper_tick()

    assert result == {"scanned": 0, "stamped": 0, "failed": 0}
    assert stamp_calls == []


@pytest.mark.asyncio
async def test_reaper_passes_batch_limit_to_query(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = FakeDB(rows=[])
    monkeypatch.setattr(reaper, "database", fake_db)
    _install_stamp(monkeypatch, return_value=1)

    await reaper.run_stamp_attribution_reaper_tick(batch_limit=25)

    assert len(fake_db.fetch_all_calls) == 1
    _, params = fake_db.fetch_all_calls[0]
    assert params == {"batch_limit": 25}


def test_reaper_query_uses_paid_at_window_and_payment_status_predicate() -> None:
    """Regression: the candidate-orders query must (a) only pick PAID orders,
    (b) bound the look-back window so the scan stays cheap, and (c) skip
    orders that paid in the last ~2 minutes (give the synchronous T9 path a
    chance to fire normally). All three are encoded in the SQL — guard them.
    """
    sql = reaper._UNSTAMPED_PAID_ORDERS_QUERY
    assert "gross_attributed_gmv_cents IS NULL" in sql
    assert "paid_at" in sql
    assert "INTERVAL '24 hours'" in sql, "look-back window must be bounded"
    assert "INTERVAL '2 minutes'" in sql, "fresh paid orders must be skipped"
    assert "payment_status" in sql
    assert "'paid'" in sql or "paid" in sql.lower()
