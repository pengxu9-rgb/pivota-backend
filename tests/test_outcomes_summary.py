"""Phase 4 v2: transaction-outcomes summary (the moat) — honest surfacing."""

from __future__ import annotations

import pytest

from services.agent_center_bd_report_service import build_outcomes_summary


@pytest.mark.asyncio
async def test_none_when_no_transactions(monkeypatch):
    async def fake(mid, *, window_key="all_time"):
        return {"transacted_count": 0, "min_sample_met": False}
    monkeypatch.setattr("services.outcome_aggregation_service.get_merchant_outcomes", fake)
    assert await build_outcomes_summary("m1") is None


@pytest.mark.asyncio
async def test_transacted_only_until_min_sample(monkeypatch):
    async def fake(mid, *, window_key="all_time"):
        return {"transacted_count": 7, "min_sample_met": False,
                "refund_rate": None, "gmv_cents": 12345}
    monkeypatch.setattr("services.outcome_aggregation_service.get_merchant_outcomes", fake)
    out = await build_outcomes_summary("m1")
    assert out["transacted_count"] == 7
    assert out["min_sample_met"] is False
    # honesty: no refund_rate / gmv before min_sample_met
    assert "refund_rate" not in out
    assert "gmv_cents" not in out


@pytest.mark.asyncio
async def test_full_outcomes_once_min_sample(monkeypatch):
    async def fake(mid, *, window_key="all_time"):
        return {"transacted_count": 120, "min_sample_met": True,
                "refund_rate": 0.0250, "gmv_cents": 999900}
    monkeypatch.setattr("services.outcome_aggregation_service.get_merchant_outcomes", fake)
    out = await build_outcomes_summary("m1")
    assert out["transacted_count"] == 120
    assert out["min_sample_met"] is True
    assert out["refund_rate"] == pytest.approx(0.025)
    assert out["gmv_cents"] == 999900


@pytest.mark.asyncio
async def test_best_effort_on_read_failure(monkeypatch):
    async def boom(mid, *, window_key="all_time"):
        raise RuntimeError("no table")
    monkeypatch.setattr("services.outcome_aggregation_service.get_merchant_outcomes", boom)
    assert await build_outcomes_summary("m1") is None
