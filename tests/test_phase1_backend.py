"""Phase 1 backend tests:
  - Task row dual-key shim (evidence + evidence_jsonb)
  - Executor row dual-key shim (requested_at + started_at)
  - Dispatcher materializes human tasks for HUMAN_TASK_RECOMMENDED
  - Idempotency key formula stable + window-floored
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------
# Dual-key shims (db row → API dict)
# ---------------------------------------------------------------------


def test_merchant_task_row_emits_both_evidence_keys():
    from db.merchant_tasks import _row_to_dict
    fake_row = {
        "task_id": "t-1", "merchant_id": "m-1",
        "parent_audit_run_id": None,
        "source_executor_run_id": None,
        "lever": None, "severity": "medium",
        "title": "X", "body": "y",
        "status": "pending",
        "assigned_to_agent": None, "assigned_to_human": None,
        "evidence_jsonb": {"foo": "bar"},
        "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "completed_at": None,
        "dismissed_reason": None,
    }
    out = _row_to_dict(fake_row)
    assert out["evidence"] == {"foo": "bar"}
    assert out["evidence_jsonb"] == {"foo": "bar"}
    assert out["evidence"] == out["evidence_jsonb"]


def test_executor_run_row_emits_both_timestamp_keys():
    from db.executor_runs import _row_to_dict
    ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    fake_row = {
        "run_id": "r-1", "agent_name": "gsc_agent",
        "merchant_id": "m-1", "parent_audit_run_id": None,
        "requested_at": ts,
        "completed_at": None,
        "status": "succeeded",
        "evidence_jsonb": {"foo": "bar"},
        "error_message": None,
    }
    out = _row_to_dict(fake_row)
    assert out["requested_at"] == ts.isoformat()
    assert out["started_at"] == ts.isoformat()
    assert out["requested_at"] == out["started_at"]
    # evidence shim too
    assert out["evidence"] == {"foo": "bar"}
    assert out["evidence_jsonb"] == {"foo": "bar"}


# ---------------------------------------------------------------------
# Dispatcher materializes human tasks
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_materializes_task_for_human_task_recommended(
    monkeypatch,
):
    """Agent emits HUMAN_TASK_RECOMMENDED → dispatcher calls
    materialize_task_from_executor with the agent's task_* fields."""
    from services.executor_agents import dispatcher as dispatcher_mod
    from services.executor_agents.base import (
        BaseExecutorAgent, ExecutorContext, ExecutorResult,
        RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
    )

    class TestAgent(BaseExecutorAgent):
        name = "test_agent_emits_human_task"

        async def should_run(self, context):
            return True

        async def execute(self, context):
            return ExecutorResult(
                status="succeeded",
                evidence={"foo": "bar"},
                result_type=RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
                task_title="Custom task title",
                task_body="Custom task body",
                task_severity="high",
                task_owner="merchant_brand_team",
                task_lever="content_creation",
            )

    captured: Dict[str, Any] = {}

    async def fake_materialize(**kwargs):
        captured.update(kwargs)
        return "task-id-1"

    async def fake_record_started(**kwargs):
        return "run-id-1"

    async def fake_record_completed(**kwargs):
        return None

    monkeypatch.setattr(
        dispatcher_mod, "_registry", lambda: [TestAgent()],
    )
    monkeypatch.setattr(
        "db.executor_runs.record_executor_run_started",
        fake_record_started,
    )
    monkeypatch.setattr(
        "db.executor_runs.record_executor_run_completed",
        fake_record_completed,
    )
    monkeypatch.setattr(
        "services.task_queue_service.materialize_task_from_executor",
        fake_materialize,
    )

    ctx = ExecutorContext(merchant_id="m-1", parent_audit_run_id="audit-1")
    summary = await dispatcher_mod.dispatch_agents(ctx)

    assert summary["agents_executed"] == 1
    assert captured["merchant_id"] == "m-1"
    assert captured["agent_name"] == "test_agent_emits_human_task"
    assert captured["title"] == "Custom task title"
    assert captured["body"] == "Custom task body"
    assert captured["severity"] == "high"
    assert captured["lever"] == "content_creation"
    assert captured["evidence"] == {"foo": "bar"}
    # Result summary surfaces materialized_task_id
    assert summary["results"][0]["materialized_task_id"] == "task-id-1"
    assert summary["results"][0]["result_type"] == (
        RESULT_TYPE_HUMAN_TASK_RECOMMENDED
    )


@pytest.mark.asyncio
async def test_dispatcher_does_not_materialize_for_direct_action_completed(
    monkeypatch,
):
    """Pre-P1.1 default behavior preserved: no task created when
    result_type is direct_action_completed (the default)."""
    from services.executor_agents import dispatcher as dispatcher_mod
    from services.executor_agents.base import (
        BaseExecutorAgent, ExecutorContext, ExecutorResult,
    )

    class LegacyAgent(BaseExecutorAgent):
        name = "legacy_agent"

        async def should_run(self, context):
            return True

        async def execute(self, context):
            return ExecutorResult(
                status="succeeded", evidence={"x": 1},
            )  # default result_type = direct_action_completed

    materialize_called = {"yes": False}

    async def fake_materialize(**kwargs):
        materialize_called["yes"] = True
        return "should-not-be-called"

    async def noop(**kwargs):
        return "run-id-1"

    monkeypatch.setattr(dispatcher_mod, "_registry", lambda: [LegacyAgent()])
    monkeypatch.setattr(
        "db.executor_runs.record_executor_run_started", noop,
    )
    monkeypatch.setattr(
        "db.executor_runs.record_executor_run_completed",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "services.task_queue_service.materialize_task_from_executor",
        fake_materialize,
    )

    summary = await dispatcher_mod.dispatch_agents(
        ExecutorContext(merchant_id="m-1"),
    )
    assert materialize_called["yes"] is False
    assert summary["results"][0]["materialized_task_id"] is None


@pytest.mark.asyncio
async def test_dispatcher_swallows_materialize_failure(monkeypatch):
    """Materialization failure does NOT lose the executor result.
    The run + evidence are already persisted; ops can retry the
    materialize manually."""
    from services.executor_agents import dispatcher as dispatcher_mod
    from services.executor_agents.base import (
        BaseExecutorAgent, ExecutorContext, ExecutorResult,
        RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
    )

    class CrashyMaterializeAgent(BaseExecutorAgent):
        name = "crashy_test"

        async def should_run(self, context):
            return True

        async def execute(self, context):
            return ExecutorResult(
                status="succeeded", evidence={"x": 1},
                result_type=RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
                task_title="X", task_body="Y", task_severity="medium",
            )

    async def crash_materialize(**kwargs):
        raise RuntimeError("DB down")

    async def noop(**kwargs):
        return "run-id-1"

    monkeypatch.setattr(
        dispatcher_mod, "_registry", lambda: [CrashyMaterializeAgent()],
    )
    monkeypatch.setattr(
        "db.executor_runs.record_executor_run_started", noop,
    )
    monkeypatch.setattr(
        "db.executor_runs.record_executor_run_completed",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "services.task_queue_service.materialize_task_from_executor",
        crash_materialize,
    )

    # Should NOT raise — materialize failures are swallowed
    summary = await dispatcher_mod.dispatch_agents(
        ExecutorContext(merchant_id="m-1"),
    )
    # Run still recorded; materialized_task_id is None
    assert summary["results"][0]["status"] == "succeeded"
    assert summary["results"][0]["materialized_task_id"] is None


# ---------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------


def test_idempotency_key_stable_for_identical_inputs():
    from services.idempotency import compute_audit_idempotency_key
    ts = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    k1 = compute_audit_idempotency_key(
        merchant_id="m-1", product_keys=["a", "b"], submitted_at=ts,
    )
    k2 = compute_audit_idempotency_key(
        merchant_id="m-1", product_keys=["a", "b"], submitted_at=ts,
    )
    assert k1 == k2
    assert len(k1) == 64  # sha256 hex


def test_idempotency_key_floors_to_window():
    """Submissions within the same 5-minute window produce the same
    key. Across the window boundary they differ."""
    from services.idempotency import compute_audit_idempotency_key
    inside_1 = datetime(2026, 5, 12, 12, 0, 30, tzinfo=timezone.utc)
    inside_2 = datetime(2026, 5, 12, 12, 4, 59, tzinfo=timezone.utc)
    outside = datetime(2026, 5, 12, 12, 5, 0, tzinfo=timezone.utc)

    k_inside_1 = compute_audit_idempotency_key(
        merchant_id="m-1", product_keys=["a"], submitted_at=inside_1,
    )
    k_inside_2 = compute_audit_idempotency_key(
        merchant_id="m-1", product_keys=["a"], submitted_at=inside_2,
    )
    k_outside = compute_audit_idempotency_key(
        merchant_id="m-1", product_keys=["a"], submitted_at=outside,
    )
    assert k_inside_1 == k_inside_2
    assert k_inside_1 != k_outside


def test_idempotency_key_product_order_irrelevant():
    """['a', 'b'] and ['b', 'a'] dedupe the same."""
    from services.idempotency import compute_audit_idempotency_key
    ts = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    k1 = compute_audit_idempotency_key(
        merchant_id="m-1", product_keys=["a", "b"], submitted_at=ts,
    )
    k2 = compute_audit_idempotency_key(
        merchant_id="m-1", product_keys=["b", "a"], submitted_at=ts,
    )
    assert k1 == k2


def test_idempotency_key_distinguishes_subject_type():
    """A merchant audit and a cold-start audit with the same merchant
    id (when a synthetic prospect id collides with a real merchant id)
    don't dedupe each other."""
    from services.idempotency import compute_audit_idempotency_key
    ts = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    k_merchant = compute_audit_idempotency_key(
        merchant_id="m-1", subject_type="merchant", submitted_at=ts,
    )
    k_cold_start = compute_audit_idempotency_key(
        merchant_id="m-1", subject_type="cold_start", submitted_at=ts,
    )
    assert k_merchant != k_cold_start


def test_idempotency_key_requires_merchant_id():
    from services.idempotency import compute_audit_idempotency_key
    with pytest.raises(ValueError):
        compute_audit_idempotency_key(merchant_id="")
    with pytest.raises(ValueError):
        compute_audit_idempotency_key(merchant_id="   ")


def test_idempotency_key_naive_timestamp_coerced_to_utc():
    """Defensive: callers passing naive datetime get UTC interpretation
    instead of crashing."""
    from services.idempotency import compute_audit_idempotency_key
    naive = datetime(2026, 5, 12, 12, 0, 0)  # no tzinfo
    aware = naive.replace(tzinfo=timezone.utc)
    k_naive = compute_audit_idempotency_key(
        merchant_id="m-1", submitted_at=naive,
    )
    k_aware = compute_audit_idempotency_key(
        merchant_id="m-1", submitted_at=aware,
    )
    assert k_naive == k_aware
