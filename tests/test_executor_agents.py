"""PR-4a: executor agent dispatcher + GscUrlSubmissionAgent tests.

Pure-logic coverage:
  - BaseExecutorAgent contract
  - dispatcher iterates registry, gates on should_run, persists via
    record_executor_run_started/completed
  - GscUrlSubmissionAgent: should_run gating, success/failure result
    shapes (mocked submit_url_to_gsc to avoid real Google API calls)

DB-touching paths (executor_runs persistence) tested end-to-end on
staging — same rationale as scheduled_audit_job.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from services.executor_agents.base import (
    BaseExecutorAgent,
    ExecutorContext,
    ExecutorResult,
)


# ---------------------------------------------------------------------------
# BaseExecutorAgent contract
# ---------------------------------------------------------------------------


class _NoopAgent(BaseExecutorAgent):
    name = "noop"

    def __init__(self, *, run_decision: bool = True, exec_status: str = "succeeded"):
        self._run = run_decision
        self._status = exec_status

    async def should_run(self, context):
        return self._run

    async def execute(self, context):
        return ExecutorResult(status=self._status, evidence={"agent": "noop"})


def test_base_agent_subclass_must_override_methods():
    """BaseExecutorAgent is abstract — direct instantiation fails."""
    with pytest.raises(TypeError):
        BaseExecutorAgent()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_executor_result_default_evidence():
    """ExecutorResult.evidence defaults to {} (dataclass default_factory)."""
    r = ExecutorResult(status="succeeded")
    assert r.evidence == {}
    assert r.error_message is None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_skips_agent_when_should_run_false():
    """Agent with should_run=False is evaluated but not executed.
    Persistence should NOT be called for it."""
    from services.executor_agents import dispatcher
    started = AsyncMock(return_value="run-1")
    completed = AsyncMock()
    with patch.object(dispatcher, "_registry", lambda: [_NoopAgent(run_decision=False)]), \
         patch("db.executor_runs.record_executor_run_started", started), \
         patch("db.executor_runs.record_executor_run_completed", completed):
        result = await dispatcher.dispatch_agents(
            ExecutorContext(merchant_id="m1"),
        )
    assert result["agents_evaluated"] == 1
    assert result["agents_executed"] == 0
    assert result["results"] == []
    started.assert_not_called()
    completed.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_executes_agent_when_should_run_true():
    """Agent with should_run=True gets started + completed via DB."""
    from services.executor_agents import dispatcher
    started = AsyncMock(return_value="run-2")
    completed = AsyncMock()
    with patch.object(dispatcher, "_registry", lambda: [_NoopAgent(run_decision=True, exec_status="succeeded")]), \
         patch("db.executor_runs.record_executor_run_started", started), \
         patch("db.executor_runs.record_executor_run_completed", completed):
        result = await dispatcher.dispatch_agents(
            ExecutorContext(merchant_id="m2", parent_audit_run_id="audit-x"),
        )
    assert result["agents_evaluated"] == 1
    assert result["agents_executed"] == 1
    assert result["results"][0]["status"] == "succeeded"
    started.assert_called_once_with(
        agent_name="noop",
        merchant_id="m2",
        parent_audit_run_id="audit-x",
    )
    completed.assert_called_once()
    completed_kwargs = completed.call_args.kwargs
    assert completed_kwargs["status"] == "succeeded"
    assert completed_kwargs["evidence_jsonb"] == {"agent": "noop"}


@pytest.mark.asyncio
async def test_dispatcher_records_failure_when_execute_raises():
    """Uncaught exception in execute() → status='failed' with diagnostic
    error_message instead of propagating."""
    from services.executor_agents import dispatcher

    class _BoomAgent(BaseExecutorAgent):
        name = "boom"
        async def should_run(self, context): return True
        async def execute(self, context): raise RuntimeError("kaboom")

    started = AsyncMock(return_value="run-3")
    completed = AsyncMock()
    with patch.object(dispatcher, "_registry", lambda: [_BoomAgent()]), \
         patch("db.executor_runs.record_executor_run_started", started), \
         patch("db.executor_runs.record_executor_run_completed", completed):
        result = await dispatcher.dispatch_agents(
            ExecutorContext(merchant_id="m3"),
        )
    assert result["results"][0]["status"] == "failed"
    assert "kaboom" in (result["results"][0]["error_message"] or "")
    completed_kwargs = completed.call_args.kwargs
    assert completed_kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_dispatcher_continues_when_should_run_raises():
    """should_run is meant to be cheap; if it raises (e.g. transient
    DB error), skip that agent silently and continue with the next.
    Lifecycle persistence does NOT run for the skipped agent."""
    from services.executor_agents import dispatcher

    class _BoomShouldRun(BaseExecutorAgent):
        name = "boom_should_run"
        async def should_run(self, context): raise ValueError("cant-decide")
        async def execute(self, context): return ExecutorResult(status="succeeded")

    started = AsyncMock(return_value="run-4")
    completed = AsyncMock()
    with patch.object(dispatcher, "_registry", lambda: [_BoomShouldRun(), _NoopAgent(run_decision=True)]), \
         patch("db.executor_runs.record_executor_run_started", started), \
         patch("db.executor_runs.record_executor_run_completed", completed):
        result = await dispatcher.dispatch_agents(
            ExecutorContext(merchant_id="m4"),
        )
    # 2 evaluated, 1 actually executed (the noop one)
    assert result["agents_evaluated"] == 2
    assert result["agents_executed"] == 1
    assert result["results"][0]["agent_name"] == "noop"


# ---------------------------------------------------------------------------
# GscUrlSubmissionAgent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gsc_agent_should_run_false_without_merchant_id():
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    assert await agent.should_run(ExecutorContext(merchant_id=None)) is False


@pytest.mark.asyncio
async def test_gsc_agent_should_run_false_when_gsc_disabled():
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    with patch("services.executor_agents.gsc_url_submission._gsc_enabled", return_value=False):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is False


@pytest.mark.asyncio
async def test_gsc_agent_should_run_false_when_no_stale_urls():
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    with patch("services.executor_agents.gsc_url_submission._gsc_enabled", return_value=True), \
         patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=[])):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is False


@pytest.mark.asyncio
async def test_gsc_agent_should_run_true_when_candidates_exist():
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    candidates = [{"url": "https://acme.co/p/1", "last_status": "pending", "last_status_at": "2026-04-01T00:00:00+00:00"}]
    with patch("services.executor_agents.gsc_url_submission._gsc_enabled", return_value=True), \
         patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=candidates)):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is True


@pytest.mark.asyncio
async def test_gsc_agent_execute_no_candidates_returns_skipped():
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    with patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=[])):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "skipped"
    assert result.evidence["reason"] == "no stale unindexed URLs"


@pytest.mark.asyncio
async def test_gsc_agent_execute_succeeds_with_partial_failures():
    """Some URLs submit OK, some return error responses → status=succeeded
    with both counts surfaced in evidence (caller can show '8 of 12
    submitted; 4 failed Google quota')."""
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    agent = GscUrlSubmissionAgent()
    candidates = [
        {"url": "https://acme.co/p/1", "last_status": "pending", "last_status_at": "2026-04-01"},
        {"url": "https://acme.co/p/2", "last_status": "pending", "last_status_at": "2026-04-01"},
        {"url": "https://acme.co/p/3", "last_status": "pending", "last_status_at": "2026-04-01"},
    ]

    async def _fake_submit(merchant_id, url, *, audit_run_id=None):
        if "p/2" in url:
            return {"status": "error", "message": "quota exceeded"}
        return {"status": "submitted", "message": "ok"}

    with patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=candidates)), \
         patch("services.gsc_integration.submit_url_to_gsc", new=AsyncMock(side_effect=_fake_submit)):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "succeeded"
    assert result.evidence["candidates_total"] == 3
    assert result.evidence["succeeded_count"] == 2
    assert result.evidence["failed_count"] == 1


@pytest.mark.asyncio
async def test_gsc_agent_execute_aborts_on_oauth_missing():
    """GscNotConfiguredError on the first URL aborts the loop —
    subsequent URLs would all hit the same error."""
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    from services.gsc_integration import GscNotConfiguredError
    agent = GscUrlSubmissionAgent()
    candidates = [{"url": "https://acme.co/p/1", "last_status": "pending", "last_status_at": "x"}]

    async def _fake_submit(merchant_id, url, *, audit_run_id=None):
        raise GscNotConfiguredError("no oauth")

    with patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=candidates)), \
         patch("services.gsc_integration.submit_url_to_gsc", new=AsyncMock(side_effect=_fake_submit)):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "failed"
    assert "gsc_oauth_missing" in (result.error_message or "")


@pytest.mark.asyncio
async def test_gsc_agent_caps_submits_per_run():
    """Even with 100 candidates, agent caps per-run submits at
    _MAX_SUBMITS_PER_RUN (25). Skipped count surfaced in evidence."""
    from services.executor_agents.gsc_url_submission import (
        GscUrlSubmissionAgent,
        _MAX_SUBMITS_PER_RUN,
    )
    agent = GscUrlSubmissionAgent()
    candidates = [
        {"url": f"https://acme.co/p/{i}", "last_status": "pending", "last_status_at": "x"}
        for i in range(100)
    ]
    async def _fake_submit(merchant_id, url, *, audit_run_id=None):
        return {"status": "submitted", "message": "ok"}
    with patch.object(agent, "_fetch_stale_unindexed", new=AsyncMock(return_value=candidates)), \
         patch("services.gsc_integration.submit_url_to_gsc", new=AsyncMock(side_effect=_fake_submit)):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "succeeded"
    assert result.evidence["submits_attempted"] == _MAX_SUBMITS_PER_RUN
    assert result.evidence["skipped_for_throttle"] == 100 - _MAX_SUBMITS_PER_RUN
