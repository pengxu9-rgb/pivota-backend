"""Phase 3.2 — executor_run_worker tests.

Mirrors the P2.2 test pattern: monkey-patch the DB accessors + agent
registry with fakes, validate the worker's claim → execute → mark
flow including the retry path.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest


# =====================================================================
# Stubs — accessor + agent fakes
# =====================================================================


class _ExecAccessors:
    """Records every accessor call so tests can assert behavior +
    configure return values per call."""

    def __init__(self, claim_payload: Optional[Dict[str, Any]]):
        self.claim_payload = claim_payload
        self.succeeded: List[Dict[str, Any]] = []
        self.failed_with_retry: List[Dict[str, Any]] = []

        # Configurable: what mark_executor_run_failed_with_retry returns
        # ("queued" for retry, "exhausted_retries" for terminal). Default
        # "queued" — caller can override per-test.
        self.failed_returns: str = "queued"

    async def claim_next_pending_executor_run(self, *, worker_id):
        return self.claim_payload

    async def mark_executor_run_succeeded(
        self, *, run_id, worker_id, evidence_jsonb=None,
    ):
        self.succeeded.append({
            "run_id": run_id, "worker_id": worker_id,
            "evidence_jsonb": evidence_jsonb,
        })
        return True

    async def mark_executor_run_failed_with_retry(
        self, *, run_id, worker_id, error_message, error_jsonb=None,
    ):
        self.failed_with_retry.append({
            "run_id": run_id, "worker_id": worker_id,
            "error_message": error_message,
            "error_jsonb": error_jsonb,
        })
        return self.failed_returns


class _FakeAgent:
    """Stand-in for a real BaseExecutorAgent. should_run isn't
    invoked by the worker (the dispatcher already filtered before
    enqueueing); the worker just calls execute()."""
    name = "fake_agent"

    def __init__(self, *, return_status="succeeded",
                 evidence=None, error_message=None,
                 raise_exception: Optional[Exception] = None,
                 result_type=None, task_title=None):
        self.return_status = return_status
        self.evidence = evidence or {"ran": True}
        self.error_message = error_message
        self.raise_exception = raise_exception
        self.result_type = result_type
        self.task_title = task_title
        self.execute_called_with: Optional[Any] = None

    async def should_run(self, context):
        return True

    async def execute(self, context):
        self.execute_called_with = context
        if self.raise_exception is not None:
            raise self.raise_exception
        from services.executor_agents.base import (
            ExecutorResult, RESULT_TYPE_DIRECT_ACTION_COMPLETED,
            RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
        )
        return ExecutorResult(
            status=self.return_status,
            evidence=self.evidence,
            error_message=self.error_message,
            result_type=(
                self.result_type or RESULT_TYPE_DIRECT_ACTION_COMPLETED
            ),
            task_title=self.task_title,
        )


def _patch_worker_deps(
    monkeypatch,
    *,
    claim_payload: Optional[Dict[str, Any]],
    fake_agent: Optional[_FakeAgent] = None,
    materialize_call: Optional[List[Dict[str, Any]]] = None,
):
    """Patch every external dependency of process_one_executor_run."""
    from db import executor_runs as er
    from services import executor_run_worker as worker

    accessors = _ExecAccessors(claim_payload=claim_payload)
    monkeypatch.setattr(
        er, "claim_next_pending_executor_run",
        accessors.claim_next_pending_executor_run,
    )
    monkeypatch.setattr(
        er, "mark_executor_run_succeeded",
        accessors.mark_executor_run_succeeded,
    )
    monkeypatch.setattr(
        er, "mark_executor_run_failed_with_retry",
        accessors.mark_executor_run_failed_with_retry,
    )

    # Replace the agent registry with a single fake (or empty if
    # fake_agent is None — exercises the unknown-agent path).
    def fake_registry():
        if fake_agent is not None:
            return {fake_agent.name: fake_agent}
        return {}
    monkeypatch.setattr(
        worker, "_agent_registry_by_name", fake_registry,
    )

    # Stub out the audit-report hydration so we don't need a real
    # merchant_audit_runs row.
    async def fake_hydrate(**kwargs):
        from services.executor_agents.base import ExecutorContext
        return ExecutorContext(
            merchant_id=kwargs.get("merchant_id"),
            parent_audit_run_id=kwargs.get("parent_audit_run_id"),
            audit_report=None,
            extra=(kwargs.get("payload_jsonb") or {}).get("extra") or {},
        )
    monkeypatch.setattr(
        worker, "_hydrate_executor_context", fake_hydrate,
    )

    # Stub task materialization.
    async def fake_materialize(**kwargs):
        if materialize_call is not None:
            materialize_call.append(kwargs)
        return "fake-task-id"
    import services.task_queue_service as tqs
    monkeypatch.setattr(
        tqs, "materialize_task_from_executor", fake_materialize,
    )

    return accessors


# =====================================================================
# Tests
# =====================================================================


@pytest.mark.asyncio
async def test_no_op_when_queue_empty(monkeypatch):
    accessors = _patch_worker_deps(
        monkeypatch, claim_payload=None, fake_agent=_FakeAgent(),
    )
    from services.executor_run_worker import process_one_executor_run
    processed = await process_one_executor_run()
    assert processed is False
    assert accessors.succeeded == []
    assert accessors.failed_with_retry == []


@pytest.mark.asyncio
async def test_happy_path_succeeded_marks_terminal(monkeypatch):
    """A claimed run that returns status='succeeded' should call
    mark_executor_run_succeeded once and not retry."""
    fake = _FakeAgent(return_status="succeeded",
                      evidence={"submitted_count": 12})
    fake.name = "fake_agent"
    accessors = _patch_worker_deps(
        monkeypatch,
        claim_payload={
            "run_id": "r-1", "agent_name": "fake_agent",
            "merchant_id": "m-1",
            "parent_audit_run_id": "audit-1",
            "payload_jsonb": None,
            "retry_count": 0, "max_retries": 3,
        },
        fake_agent=fake,
    )
    from services.executor_run_worker import process_one_executor_run
    processed = await process_one_executor_run()
    assert processed is True
    assert len(accessors.succeeded) == 1
    assert accessors.succeeded[0]["evidence_jsonb"] == {"submitted_count": 12}
    assert accessors.failed_with_retry == []
    # Agent received the merchant_id/parent_audit_run_id
    assert fake.execute_called_with.merchant_id == "m-1"
    assert fake.execute_called_with.parent_audit_run_id == "audit-1"


@pytest.mark.asyncio
async def test_skipped_status_treated_as_succeeded(monkeypatch):
    """Some agents return status='skipped' when their precondition
    fails (e.g. content_brief skips when no missing brief). Worker
    should treat that as a clean terminal — don't retry."""
    fake = _FakeAgent(return_status="skipped")
    fake.name = "fake_agent"
    accessors = _patch_worker_deps(
        monkeypatch,
        claim_payload={
            "run_id": "r-2", "agent_name": "fake_agent",
            "merchant_id": "m-1", "parent_audit_run_id": None,
            "payload_jsonb": None,
            "retry_count": 0, "max_retries": 3,
        },
        fake_agent=fake,
    )
    from services.executor_run_worker import process_one_executor_run
    await process_one_executor_run()
    assert len(accessors.succeeded) == 1
    assert accessors.failed_with_retry == []


@pytest.mark.asyncio
async def test_failed_status_routes_to_retry(monkeypatch):
    """Agent returning status='failed' should call
    mark_executor_run_failed_with_retry, NOT mark_succeeded."""
    fake = _FakeAgent(return_status="failed",
                      error_message="upstream rate limit")
    fake.name = "fake_agent"
    accessors = _patch_worker_deps(
        monkeypatch,
        claim_payload={
            "run_id": "r-3", "agent_name": "fake_agent",
            "merchant_id": "m-1", "parent_audit_run_id": None,
            "payload_jsonb": None,
            "retry_count": 1, "max_retries": 3,
        },
        fake_agent=fake,
    )
    from services.executor_run_worker import process_one_executor_run
    await process_one_executor_run()
    assert accessors.succeeded == []
    assert len(accessors.failed_with_retry) == 1
    assert accessors.failed_with_retry[0]["error_message"] == (
        "upstream rate limit"
    )
    # error_jsonb captures the attempt number for diagnostics
    err = accessors.failed_with_retry[0]["error_jsonb"]
    assert err["attempt"] == 2  # retry_count + 1 = 1+1


@pytest.mark.asyncio
async def test_uncaught_exception_routes_to_retry_with_traceback(
    monkeypatch,
):
    """If the agent raises an unexpected exception, the worker
    catches it, captures the traceback, and routes to retry."""
    fake = _FakeAgent(raise_exception=ValueError("boom"))
    fake.name = "fake_agent"
    accessors = _patch_worker_deps(
        monkeypatch,
        claim_payload={
            "run_id": "r-4", "agent_name": "fake_agent",
            "merchant_id": "m-1", "parent_audit_run_id": None,
            "payload_jsonb": None,
            "retry_count": 0, "max_retries": 3,
        },
        fake_agent=fake,
    )
    from services.executor_run_worker import process_one_executor_run
    await process_one_executor_run()
    assert accessors.succeeded == []
    assert len(accessors.failed_with_retry) == 1
    err = accessors.failed_with_retry[0]["error_jsonb"]
    assert err["stage"] == "agent_execute"
    assert "boom" in err["message"]
    assert "ValueError" in err["traceback_truncated"]


@pytest.mark.asyncio
async def test_unknown_agent_marks_failed_immediately(monkeypatch):
    """If the queue references an agent_name that's not in the
    registry (deployment skew — agent removed but queued runs
    remain), the worker marks failed immediately rather than
    looping."""
    accessors = _patch_worker_deps(
        monkeypatch,
        claim_payload={
            "run_id": "r-5", "agent_name": "removed_agent",
            "merchant_id": "m-1", "parent_audit_run_id": None,
            "payload_jsonb": None,
            "retry_count": 0, "max_retries": 3,
        },
        fake_agent=None,  # empty registry
    )
    from services.executor_run_worker import process_one_executor_run
    await process_one_executor_run()
    assert accessors.succeeded == []
    assert len(accessors.failed_with_retry) == 1
    assert "removed_agent" in (
        accessors.failed_with_retry[0]["error_message"]
    )


@pytest.mark.asyncio
async def test_human_task_recommended_materializes(monkeypatch):
    """Successful run with result_type=HUMAN_TASK_RECOMMENDED should
    invoke materialize_task_from_executor (mirrors the dispatcher's
    existing branch for backwards-compat parity)."""
    from services.executor_agents.base import (
        RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
    )
    fake = _FakeAgent(
        return_status="succeeded",
        result_type=RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
        task_title="Fix sitemap drift",
    )
    fake.name = "fake_agent"
    materialized: List[Dict[str, Any]] = []
    accessors = _patch_worker_deps(
        monkeypatch,
        claim_payload={
            "run_id": "r-6", "agent_name": "fake_agent",
            "merchant_id": "m-1",
            "parent_audit_run_id": "audit-1",
            "payload_jsonb": None,
            "retry_count": 0, "max_retries": 3,
        },
        fake_agent=fake,
        materialize_call=materialized,
    )
    from services.executor_run_worker import process_one_executor_run
    await process_one_executor_run()
    assert len(accessors.succeeded) == 1
    assert len(materialized) == 1
    assert materialized[0]["title"] == "Fix sitemap drift"
    assert materialized[0]["merchant_id"] == "m-1"
    assert materialized[0]["executor_run_id"] == "r-6"
