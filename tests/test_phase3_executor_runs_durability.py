"""Phase 3.1 — executor_runs durability schema + state machine tests.

Mirrors the P2.1 pattern: pure-logic tests for the state machine +
idempotency key, with a documented skip for the DB round-trip
surface (Postgres FOR UPDATE SKIP LOCKED + partial indexes don't
round-trip cleanly under SQLite — verified against real Postgres
in the executor_run_worker integration flow that lands in P3.2).
"""

from __future__ import annotations

import pytest


# =====================================================================
# State machine — pure logic
# =====================================================================


def test_active_and_terminal_stages_are_disjoint():
    from db import executor_runs as er
    assert er.ACTIVE_STAGES & er.TERMINAL_STAGES == set()
    # W5 P7: pending_approval is a "gate" stage — neither active
    # (worker-claimable) nor terminal. It sits between dispatch and
    # queue, waiting on merchant consent.
    gate_stages = {er.STAGE_PENDING_APPROVAL}
    assert gate_stages & er.ACTIVE_STAGES == set()
    assert gate_stages & er.TERMINAL_STAGES == set()
    assert (
        er.ACTIVE_STAGES | er.TERMINAL_STAGES | gate_stages
        == set(er.VALID_STAGE_TRANSITIONS.keys())
    )


def test_happy_path_queued_to_succeeded():
    from db import executor_runs as er
    assert er.is_valid_stage_transition(
        er.STAGE_QUEUED, er.STAGE_CLAIMED,
    )
    assert er.is_valid_stage_transition(
        er.STAGE_CLAIMED, er.STAGE_SUCCEEDED,
    )


def test_failed_attempt_can_retry_back_to_queued():
    """The retry path is what makes this a durable queue —
    a failed attempt re-enqueues for another worker to claim."""
    from db import executor_runs as er
    assert er.is_valid_stage_transition(
        er.STAGE_CLAIMED, er.STAGE_FAILED,
    )
    assert er.is_valid_stage_transition(
        er.STAGE_CLAIMED, er.STAGE_QUEUED,
    )


def test_claimed_can_terminate_via_exhausted_retries():
    from db import executor_runs as er
    assert er.is_valid_stage_transition(
        er.STAGE_CLAIMED, er.STAGE_EXHAUSTED_RETRIES,
    )


def test_terminal_stages_have_no_outbound_transitions():
    from db import executor_runs as er
    for terminal in er.TERMINAL_STAGES:
        for any_stage in er.VALID_STAGE_TRANSITIONS:
            assert not er.is_valid_stage_transition(terminal, any_stage), (
                f"terminal {terminal!r} must not transition to {any_stage!r}"
            )


def test_cannot_skip_claimed_stage():
    """A queued row cannot jump straight to a terminal — must
    transition through claimed first. Prevents accidental "drop the
    work" via wrong UPDATE."""
    from db import executor_runs as er
    assert not er.is_valid_stage_transition(
        er.STAGE_QUEUED, er.STAGE_SUCCEEDED,
    )
    assert not er.is_valid_stage_transition(
        er.STAGE_QUEUED, er.STAGE_FAILED,
    )


# =====================================================================
# Idempotency key — pure helper
# =====================================================================


def test_idempotency_key_stable_for_identical_inputs():
    from db.executor_runs import compute_executor_idempotency_key
    a = compute_executor_idempotency_key(
        agent_name="GscUrlSubmissionAgent",
        merchant_id="m-1",
        parent_audit_run_id="run-1",
    )
    b = compute_executor_idempotency_key(
        agent_name="GscUrlSubmissionAgent",
        merchant_id="m-1",
        parent_audit_run_id="run-1",
    )
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_idempotency_key_distinguishes_agent():
    from db.executor_runs import compute_executor_idempotency_key
    a = compute_executor_idempotency_key(
        agent_name="GscUrlSubmissionAgent",
        merchant_id="m-1", parent_audit_run_id="run-1",
    )
    b = compute_executor_idempotency_key(
        agent_name="SitemapFreshnessMonitor",
        merchant_id="m-1", parent_audit_run_id="run-1",
    )
    assert a != b


def test_idempotency_key_distinguishes_audit_run():
    """The same agent on the same merchant from a DIFFERENT audit
    should enqueue separately. Re-audits produce a NEW
    parent_audit_run_id, so re-running an audit always re-fires
    the executor."""
    from db.executor_runs import compute_executor_idempotency_key
    a = compute_executor_idempotency_key(
        agent_name="GscUrlSubmissionAgent",
        merchant_id="m-1", parent_audit_run_id="run-1",
    )
    b = compute_executor_idempotency_key(
        agent_name="GscUrlSubmissionAgent",
        merchant_id="m-1", parent_audit_run_id="run-2",
    )
    assert a != b


def test_idempotency_key_handles_none_components():
    """Standalone executor runs (no parent audit) get an
    idempotency key built from what's available — agent_name is
    always present, the rest may be None."""
    from db.executor_runs import compute_executor_idempotency_key
    a = compute_executor_idempotency_key(
        agent_name="ManualBackfillAgent",
        merchant_id=None, parent_audit_run_id=None,
    )
    assert isinstance(a, str)
    assert len(a) == 64


# =====================================================================
# DB round-trip — skipped (FOR UPDATE SKIP LOCKED is Postgres-only)
# =====================================================================


@pytest.mark.skip(
    reason="Round-trip integration via in-memory SQLite is fragile "
    "for the FOR UPDATE SKIP LOCKED + partial-index surface this "
    "module uses (matches the existing skip rationale in "
    "test_phase2_audit_runs_lifecycle.py). The accessors are "
    "exercised against real Postgres in the executor_run_worker "
    "integration flow that lands in P3.2."
)
async def test_round_trip_postgres_only():
    pass
