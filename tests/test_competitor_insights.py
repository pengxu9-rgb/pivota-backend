"""C4a — CompetitorInsightsAgent: extract WHY the competitors AI names win from the
captured answer excerpts, and surface it as a merchant task. Mocks the LLM call;
the evidence-collection + result shaping run for real.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from services.executor_agents.base import (
    ExecutorContext,
    RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
)
from services.executor_agents.competitor_insights import (
    CompetitorInsightsAgent,
    _collect_competitor_evidence,
)

_MOD = "services.executor_agents.competitor_insights"


def _report(per_prompt) -> Dict[str, Any]:
    return {"per_sku_reports": [{"opportunity": {"per_prompt": per_prompt}}]}


_LOSING_LANE = {
    "normalized_query": "best collagen for before bed",
    "competitors": ["MDhair Marine Collagen", "Vida Glow"],
    "cited_evidence": {
        "excerpt": (
            "MDhair Marine Collagen and Vida Glow are recommended for nighttime "
            "use, highlighting glycine and melatonin for sleep support."
        )
    },
}


# ---------------------------------------------------------------------------
# evidence collection
# ---------------------------------------------------------------------------


def test_collect_evidence_keeps_only_lanes_with_excerpt_and_competitors():
    report = _report([
        _LOSING_LANE,
        {"normalized_query": "no-competitors", "competitors": [],
         "cited_evidence": {"excerpt": "generic answer"}},                 # no competitors
        {"normalized_query": "no-excerpt", "competitors": ["X"],
         "cited_evidence": {}},                                            # no excerpt
        {"normalized_query": "no-query", "competitors": ["Y"],
         "cited_evidence": {"excerpt": "ans"}},                            # has query though
    ])
    rows = _collect_competitor_evidence(report)
    queries = [r["query"] for r in rows]
    assert "best collagen for before bed" in queries
    assert "no-competitors" not in queries
    assert "no-excerpt" not in queries
    row = rows[0]
    assert row["competitors"] == ["MDhair Marine Collagen", "Vida Glow"]
    assert "glycine" in row["excerpt"]


def test_collect_evidence_dedups_by_query_and_is_defensive():
    report = _report([_LOSING_LANE, dict(_LOSING_LANE)])  # dup query
    assert len(_collect_competitor_evidence(report)) == 1
    assert _collect_competitor_evidence(None) == []
    assert _collect_competitor_evidence("{bad json") == []


# ---------------------------------------------------------------------------
# should_run / execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_run_true_with_key_and_evidence():
    agent = CompetitorInsightsAgent()
    ctx = ExecutorContext(merchant_id="m1", audit_report=_report([_LOSING_LANE]))
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"):
        assert await agent.should_run(ctx) is True


@pytest.mark.asyncio
async def test_should_run_false_without_key_or_evidence():
    agent = CompetitorInsightsAgent()
    ctx = ExecutorContext(merchant_id="m1", audit_report=_report([_LOSING_LANE]))
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value=None):
        assert await agent.should_run(ctx) is False
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"):
        assert await agent.should_run(
            ExecutorContext(merchant_id="m1", audit_report=_report([]))
        ) is False


@pytest.mark.asyncio
async def test_execute_emits_competitive_intel_task():
    agent = CompetitorInsightsAgent()
    ctx = ExecutorContext(merchant_id="m1", audit_report=_report([_LOSING_LANE]))
    insights = [{
        "competitor": "MDhair Marine Collagen",
        "query": "best collagen for before bed",
        "why_wins": "marine collagen + glycine/melatonin for sleep",
        "how_to_compete": "lead with your sleep actives",
    }]
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._extract_win_reasons", new=AsyncMock(return_value=insights)):
        result = await agent.execute(ctx)
    assert result.status == "succeeded"
    assert result.result_type == RESULT_TYPE_HUMAN_TASK_RECOMMENDED
    assert result.task_title
    assert "MDhair" in (result.task_body or "")
    assert result.evidence["insights"] == insights
    assert result.evidence["queries_analyzed"] == 1


@pytest.mark.asyncio
async def test_execute_skipped_no_evidence():
    agent = CompetitorInsightsAgent()
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"):
        result = await agent.execute(
            ExecutorContext(merchant_id="m1", audit_report=_report([]))
        )
    assert result.status == "skipped"


@pytest.mark.asyncio
async def test_execute_failed_when_extraction_empty():
    agent = CompetitorInsightsAgent()
    ctx = ExecutorContext(merchant_id="m1", audit_report=_report([_LOSING_LANE]))
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._extract_win_reasons", new=AsyncMock(return_value=[])):
        result = await agent.execute(ctx)
    assert result.status == "failed"


def test_agent_registered_in_dispatcher_and_worker():
    from services.executor_agents.dispatcher import _registry
    from services.executor_run_worker import _agent_registry_by_name

    name = CompetitorInsightsAgent().name
    assert name in {a.name for a in _registry()}
    assert name in _agent_registry_by_name()
