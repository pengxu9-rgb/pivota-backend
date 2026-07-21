"""W5.4 — URL-audit runs deliver the full URL-tier executor set.

The worker's materializing stage used to skip executor dispatch entirely for
synthetic (URL-audit) runs (url_audit_no_executors). Now it dispatches the
URL-tier set (content_brief + competitor_insights + canonical_pdp_enrichment +
gsc_url_submission_loop) via dispatch_only, so a pasted-URL merchant gets real
executor work — including seed-aware enrichment/indexing now that P3 mints the
seed's Pivota canonical identity — while sitemap_freshness (connected-store-only)
stays excluded.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest


@pytest.mark.asyncio
async def test_dispatch_only_skips_task_materialization_and_forwards_allowlist(monkeypatch):
    import services.audit_run_worker as worker
    import services.task_queue_service as tq
    import services.executor_agents.dispatcher as disp

    tasks_called: List[str] = []
    dispatch_calls: List[Dict[str, Any]] = []

    async def fake_materialize_tasks(**kwargs):
        tasks_called.append("materialized")
        return {"created_count": 3}

    async def fake_reverify(**kwargs):
        tasks_called.append("reverified")
        return {}

    async def fake_dispatch(context, *, agent_names=None):
        dispatch_calls.append({"agent_names": agent_names})
        return {"dispatched_count": 2, "runs": [{"a": 1}, {"a": 2}]}

    monkeypatch.setattr(tq, "materialize_tasks_from_audit", fake_materialize_tasks)
    monkeypatch.setattr(tq, "reverify_outreach_records", fake_reverify)
    monkeypatch.setattr(disp, "dispatch_agents", fake_dispatch)

    summary = await worker._materialize_tasks_and_executors(
        merchant_id="m-1", run_id="r-1", brand_report={"k": "v"},
        integration_state=None,
        agent_names=disp.URL_AUDIT_EXECUTORS,
        dispatch_only=True,
    )

    # dispatch_only: NO task-queue materialization, NO outreach reverify
    assert tasks_called == []
    assert summary["tasks_materialized"] == 0
    # executors DID dispatch, restricted to the URL-tier allowlist (the fake
    # dispatch returns a fixed count; the point here is the allowlist forwarding)
    assert summary["executors_dispatched"] == 2
    assert dispatch_calls == [{"agent_names": disp.URL_AUDIT_EXECUTORS}]


@pytest.mark.asyncio
async def test_connected_store_run_still_materializes_tasks_and_full_registry(monkeypatch):
    import services.audit_run_worker as worker
    import services.task_queue_service as tq
    import services.executor_agents.dispatcher as disp

    tasks_called: List[str] = []
    dispatch_calls: List[Dict[str, Any]] = []

    async def fake_materialize_tasks(**kwargs):
        tasks_called.append("materialized")
        return {"created_count": 5}

    async def fake_reverify(**kwargs):
        return {}

    async def fake_dispatch(context, *, agent_names=None):
        dispatch_calls.append({"agent_names": agent_names})
        return {"dispatched_count": 5, "runs": []}

    monkeypatch.setattr(tq, "materialize_tasks_from_audit", fake_materialize_tasks)
    monkeypatch.setattr(tq, "reverify_outreach_records", fake_reverify)
    monkeypatch.setattr(disp, "dispatch_agents", fake_dispatch)

    summary = await worker._materialize_tasks_and_executors(
        merchant_id="m-2", run_id="r-2", brand_report={"k": "v"},
        integration_state=None,
    )
    # default (connected store): tasks materialize + FULL registry (no filter)
    assert tasks_called == ["materialized"]
    assert summary["tasks_materialized"] == 5
    assert dispatch_calls == [{"agent_names": None}]
