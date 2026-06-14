"""Phase 2 v2: cross-merchant niche-query recurrence demand proxy."""

from __future__ import annotations

import pytest

from services import niche_recurrence as nr
from services.agent_center_bd_report_service import (
    attach_niche_recurrence,
    build_where_you_can_win,
)


class _FakeDB:
    def __init__(self, rows=None):
        self.executes = []
        self._rows = rows or []

    async def execute(self, sql, params):
        self.executes.append(params)

    async def fetch_all(self, sql, params):
        self.last_params = params
        return self._rows


def _report_with_open_lane(query, score=80):
    return {
        "sku_key": "s1",
        "identity": {"name": "Brand A"},
        "opportunity": {"per_prompt": [
            {"query": query, "normalized_query": query.strip().lower(),
             "open_lane": True, "opportunity_score": score, "attribute_basis": ["x"]},
        ]},
    }


@pytest.mark.asyncio
async def test_record_dedupes_and_normalizes():
    db = _FakeDB()
    n = await nr.record_niche_queries(
        queries=["Vegan Collagen", "vegan collagen", "  ", "Collagen Stick"],
        merchant_id="m1", db=db,
    )
    assert n == 2  # "vegan collagen" deduped, blank skipped
    assert {e["q"] for e in db.executes} == {"vegan collagen", "collagen stick"}
    assert all(e["mid"] == "m1" for e in db.executes)


@pytest.mark.asyncio
async def test_record_noop_without_merchant_or_queries():
    db = _FakeDB()
    assert await nr.record_niche_queries(queries=[], merchant_id="m1", db=db) == 0
    assert await nr.record_niche_queries(queries=["q"], merchant_id="", db=db) == 0
    assert db.executes == []


@pytest.mark.asyncio
async def test_recurrence_for_queries_maps_counts():
    db = _FakeDB(rows=[
        {"normalized_query": "vegan collagen", "distinct_merchants": 3, "total_runs": 7},
    ])
    out = await nr.recurrence_for_queries(["Vegan Collagen", "other"], db=db)
    assert out == {"vegan collagen": {"distinct_merchants": 3, "total_runs": 7}}


@pytest.mark.asyncio
async def test_attach_recurrence_adds_signal_and_exposes_proxy():
    wycw = build_where_you_can_win([_report_with_open_lane("vegan collagen")])
    assert wycw["demand_proxies"] == ["probe"]  # default before recurrence data
    db = _FakeDB(rows=[
        {"normalized_query": "vegan collagen", "distinct_merchants": 4, "total_runs": 9},
    ])
    out = await attach_niche_recurrence(wycw, db=db)
    assert out["targets"][0]["recurrence"] == {"distinct_merchants": 4, "total_runs": 9}
    assert "recurrence" in out["demand_proxies"]


@pytest.mark.asyncio
async def test_attach_recurrence_noop_when_no_history():
    wycw = build_where_you_can_win([_report_with_open_lane("niche q")])
    db = _FakeDB(rows=[])
    out = await attach_niche_recurrence(wycw, db=db)
    assert "recurrence" not in out["targets"][0]
    assert out["demand_proxies"] == ["probe"]  # recurrence not offered without data


@pytest.mark.asyncio
async def test_recurrence_read_failure_is_best_effort():
    class _BoomDB:
        async def fetch_all(self, *a, **k):
            raise RuntimeError("no table")
    assert await nr.recurrence_for_queries(["q"], db=_BoomDB()) == {}
