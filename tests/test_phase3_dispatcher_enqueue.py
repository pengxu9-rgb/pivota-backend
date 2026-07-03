"""Phase 3.3 — dispatcher enqueue-pattern tests.

Validates the migration from fire-and-forget execution to durable
enqueueing. The dispatcher now calls enqueue_executor_run for each
agent that passes should_run, rather than running the agent inline.

Strategy: monkey-patch the registry with fake agents, monkey-patch
the db.executor_runs accessors, assert what got enqueued.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# =====================================================================
# Stubs
# =====================================================================


class _FakeAgent:
    """Stand-in BaseExecutorAgent. should_run controllable per
    instance; execute is never called by the new dispatcher (the
    worker calls it later)."""
    def __init__(self, name: str, should_run_returns: bool = True,
                 should_run_raises: Optional[Exception] = None):
        self.name = name
        self.should_run_returns = should_run_returns
        self.should_run_raises = should_run_raises
        self.execute_called = False

    async def should_run(self, context):
        if self.should_run_raises is not None:
            raise self.should_run_raises
        return self.should_run_returns

    async def execute(self, context):
        # New dispatcher MUST NOT call this — the worker does.
        self.execute_called = True
        raise AssertionError("dispatcher should not call execute()")


class _EnqueueStub:
    def __init__(self):
        self.enqueued: List[Dict[str, Any]] = []
        self.idem_lookups: List[str] = []
        # Configurable: simulate dedupe hit
        self.idem_returns: Dict[str, Optional[str]] = {}
        # Configurable: simulate enqueue failure
        self.enqueue_returns_none_for: set = set()

    async def enqueue(self, **kwargs):
        if kwargs["agent_name"] in self.enqueue_returns_none_for:
            return None
        self.enqueued.append(kwargs)
        return f"run-{kwargs['agent_name']}-{len(self.enqueued)}"

    async def find_idem(self, *, idempotency_key):
        self.idem_lookups.append(idempotency_key)
        return self.idem_returns.get(idempotency_key)


def _patch_dispatcher(
    monkeypatch, *, agents: List[_FakeAgent], stub: _EnqueueStub,
):
    from services.executor_agents import dispatcher
    from db import executor_runs as er

    monkeypatch.setattr(dispatcher, "_registry", lambda: agents)
    monkeypatch.setattr(er, "enqueue_executor_run", stub.enqueue)
    monkeypatch.setattr(
        er, "find_in_flight_executor_run_by_idempotency",
        stub.find_idem,
    )
    return stub


# =====================================================================
# Tests
# =====================================================================


@pytest.mark.asyncio
async def test_enqueues_one_run_per_should_run_true_agent(monkeypatch):
    agents = [
        _FakeAgent("agent_a", should_run_returns=True),
        _FakeAgent("agent_b", should_run_returns=False),
        _FakeAgent("agent_c", should_run_returns=True),
    ]
    stub = _EnqueueStub()
    _patch_dispatcher(monkeypatch, agents=agents, stub=stub)

    from services.executor_agents.base import ExecutorContext
    from services.executor_agents.dispatcher import dispatch_agents
    ctx = ExecutorContext(
        merchant_id="m-1", parent_audit_run_id="audit-1",
        audit_report={"k": "v"},
    )
    result = await dispatch_agents(ctx)

    assert result["agents_evaluated"] == 3
    assert result["agents_enqueued"] == 2
    assert result["dispatched_count"] == 2
    enqueued_names = sorted(e["agent_name"] for e in stub.enqueued)
    assert enqueued_names == ["agent_a", "agent_c"]
    # No agent's execute() was called by the dispatcher
    assert not any(a.execute_called for a in agents)


@pytest.mark.asyncio
async def test_kill_switch_disables_all_dispatch(monkeypatch):
    """R3: AUDIT_EXECUTOR_DISPATCH_ENABLED=false must enqueue NOTHING —
    no agent evaluated, no Gemini spend, no merchant_tasks, at every call site."""
    from config.settings import settings
    monkeypatch.setattr(settings, "audit_executor_dispatch_enabled", False)

    agents = [
        _FakeAgent("agent_a", should_run_returns=True),
        _FakeAgent("agent_b", should_run_returns=True),
    ]
    stub = _EnqueueStub()
    _patch_dispatcher(monkeypatch, agents=agents, stub=stub)

    from services.executor_agents.base import ExecutorContext
    from services.executor_agents.dispatcher import dispatch_agents
    ctx = ExecutorContext(
        merchant_id="m-1", parent_audit_run_id="audit-1", audit_report={"k": "v"},
    )
    result = await dispatch_agents(ctx)

    assert result["disabled"] is True
    assert result["agents_evaluated"] == 0
    assert result["dispatched_count"] == 0
    assert stub.enqueued == []
    assert not any(a.execute_called for a in agents)


@pytest.mark.asyncio
async def test_dispatcher_does_not_call_execute(monkeypatch):
    """The defining behavior of P3.3 — dispatcher MUST NOT run
    agents inline. The worker is responsible for execution."""
    agents = [_FakeAgent("agent_a")]
    stub = _EnqueueStub()
    _patch_dispatcher(monkeypatch, agents=agents, stub=stub)

    from services.executor_agents.base import ExecutorContext
    from services.executor_agents.dispatcher import dispatch_agents
    await dispatch_agents(ExecutorContext(merchant_id="m"))
    assert agents[0].execute_called is False


@pytest.mark.asyncio
async def test_should_run_failure_skips_enqueue(monkeypatch):
    """If should_run raises, the dispatcher should log + continue
    rather than crash the whole dispatch."""
    agents = [
        _FakeAgent("agent_a",
                   should_run_raises=ValueError("should_run boom")),
        _FakeAgent("agent_b", should_run_returns=True),
    ]
    stub = _EnqueueStub()
    _patch_dispatcher(monkeypatch, agents=agents, stub=stub)

    from services.executor_agents.base import ExecutorContext
    from services.executor_agents.dispatcher import dispatch_agents
    result = await dispatch_agents(
        ExecutorContext(merchant_id="m-1"),
    )
    # agent_a's should_run raised — not enqueued. agent_b proceeded.
    assert result["agents_evaluated"] == 2
    assert result["agents_enqueued"] == 1
    assert stub.enqueued[0]["agent_name"] == "agent_b"


@pytest.mark.asyncio
async def test_idempotent_dedupe_reuses_existing_run(monkeypatch):
    """If a previous dispatch enqueued the same (agent, merchant,
    audit) tuple and it's still in-flight, the new dispatch should
    NOT enqueue a duplicate."""
    agents = [_FakeAgent("agent_a")]
    stub = _EnqueueStub()
    # Force idempotency lookup to return an existing run_id
    from db.executor_runs import compute_executor_idempotency_key
    idem_key = compute_executor_idempotency_key(
        agent_name="agent_a",
        merchant_id="m-1", parent_audit_run_id="audit-1",
    )
    stub.idem_returns[idem_key] = "existing-run-id"
    _patch_dispatcher(monkeypatch, agents=agents, stub=stub)

    from services.executor_agents.base import ExecutorContext
    from services.executor_agents.dispatcher import dispatch_agents
    result = await dispatch_agents(
        ExecutorContext(merchant_id="m-1",
                        parent_audit_run_id="audit-1"),
    )
    # No new enqueue happened — dedupe path fired
    assert stub.enqueued == []
    assert result["agents_enqueued"] == 0
    # Result still surfaces the existing run for traceability
    runs = result["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == "existing-run-id"
    assert runs[0]["state"] == "deduped"


@pytest.mark.asyncio
async def test_enqueue_failure_recorded_in_summary(monkeypatch):
    """If enqueue_executor_run returns None (persistence failed),
    the dispatcher records the failure in the summary so the audit
    pipeline knows this agent didn't get scheduled."""
    agents = [
        _FakeAgent("agent_ok"),
        _FakeAgent("agent_fails"),
    ]
    stub = _EnqueueStub()
    stub.enqueue_returns_none_for = {"agent_fails"}
    _patch_dispatcher(monkeypatch, agents=agents, stub=stub)

    from services.executor_agents.base import ExecutorContext
    from services.executor_agents.dispatcher import dispatch_agents
    result = await dispatch_agents(ExecutorContext(merchant_id="m-1"))

    assert result["agents_enqueued"] == 1
    runs = {r["agent_name"]: r for r in result["runs"]}
    assert runs["agent_ok"]["state"] == "enqueued"
    assert runs["agent_ok"]["run_id"] is not None
    assert runs["agent_fails"]["state"] == "enqueue_failed"
    assert runs["agent_fails"]["run_id"] is None


@pytest.mark.asyncio
async def test_per_agent_max_retries_passed_to_enqueue(monkeypatch):
    """Per-agent retry budgets defined in _AGENT_MAX_RETRIES must
    be passed to enqueue_executor_run so the worker enforces them."""
    from services.executor_agents import dispatcher
    # Use the real registry's agent names so the retry-budget map fires
    agents = [
        _FakeAgent("gsc_url_submission_loop"),
        _FakeAgent("sitemap_freshness_monitor"),
        _FakeAgent("content_brief_generator"),
        _FakeAgent("unknown_agent"),  # falls back to default 3
    ]
    stub = _EnqueueStub()
    _patch_dispatcher(monkeypatch, agents=agents, stub=stub)

    from services.executor_agents.base import ExecutorContext
    from services.executor_agents.dispatcher import dispatch_agents
    await dispatch_agents(ExecutorContext(merchant_id="m-1"))

    by_name = {e["agent_name"]: e for e in stub.enqueued}
    # GSC: 2 (per-agent override — quota-sensitive)
    assert by_name["gsc_url_submission_loop"]["max_retries"] == 2
    # Sitemap: 3 (default)
    assert by_name["sitemap_freshness_monitor"]["max_retries"] == 3
    # Content brief: 3 (default)
    assert by_name["content_brief_generator"]["max_retries"] == 3
    # Unknown: falls back to 3
    assert by_name["unknown_agent"]["max_retries"] == 3


@pytest.mark.asyncio
async def test_payload_carries_extra_but_not_audit_report(monkeypatch):
    """The payload_jsonb stored with the queue row carries the
    dispatcher's `extra` context. It should NOT carry audit_report
    (too big — worker re-reads it from merchant_audit_runs)."""
    agents = [_FakeAgent("agent_a")]
    stub = _EnqueueStub()
    _patch_dispatcher(monkeypatch, agents=agents, stub=stub)

    from services.executor_agents.base import ExecutorContext
    from services.executor_agents.dispatcher import dispatch_agents
    huge_report = {"per_product": ["x" * 1000 for _ in range(100)]}
    await dispatch_agents(ExecutorContext(
        merchant_id="m-1",
        audit_report=huge_report,
        extra={"trigger": "manual"},
    ))

    payload = stub.enqueued[0]["payload_jsonb"]
    assert "audit_report" not in payload
    assert payload.get("extra") == {"trigger": "manual"}
