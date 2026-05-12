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


# P3.3 architecture note: the dispatcher no longer runs agents inline.
# The 3 original tests here (P1.1) asserted the old inline-execution
# behavior — they were removed when P3.3 migrated the dispatcher to
# enqueue-only. The behaviors they covered are now tested in:
#   - tests/test_phase3_dispatcher_enqueue.py — dispatcher contract
#     (enqueue per should_run=True agent, idempotency dedupe, etc.)
#   - tests/test_phase3_executor_run_worker.py::test_human_task_recommended_materializes
#     — worker materializes the task after agent execute()
#
# This stub remains so the section header above ("Dispatcher
# materializes human tasks") stays grep-able to the architectural
# move.


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
