"""Phase 5.1 — verification_runs worker-pull accessors + state machine.

Mirrors the P2.1 / P3.1 pattern: pure-logic tests for the state
machine + idempotency key, with a documented skip for the DB
round-trip surface (Postgres FOR UPDATE SKIP LOCKED + partial
indexes don't round-trip cleanly on SQLite).
"""

from __future__ import annotations

import pytest


# =====================================================================
# Verification state machine
# =====================================================================


def test_verification_active_and_terminal_disjoint():
    from db.audit_evidence import (
        VERIFICATION_ACTIVE, VERIFICATION_TERMINAL,
        VALID_VERIFICATION_TRANSITIONS,
    )
    assert VERIFICATION_ACTIVE & VERIFICATION_TERMINAL == set()
    assert (
        VERIFICATION_ACTIVE | VERIFICATION_TERMINAL
        == set(VALID_VERIFICATION_TRANSITIONS.keys())
    )


def test_happy_path_pending_through_succeeded():
    from db.audit_evidence import (
        is_valid_verification_transition,
        VERIFICATION_STATUS_PENDING, VERIFICATION_STATUS_CLAIMED,
        VERIFICATION_STATUS_SUCCEEDED,
    )
    assert is_valid_verification_transition(
        VERIFICATION_STATUS_PENDING, VERIFICATION_STATUS_CLAIMED,
    )
    assert is_valid_verification_transition(
        VERIFICATION_STATUS_CLAIMED, VERIFICATION_STATUS_SUCCEEDED,
    )


def test_claimed_can_blocked_terminal():
    """Blocked is its own terminal — distinct from failed. Used
    when the upstream system (GSC, agent.pivota.cc) is genuinely
    unavailable so retrying won't help."""
    from db.audit_evidence import (
        is_valid_verification_transition,
        VERIFICATION_STATUS_CLAIMED, VERIFICATION_STATUS_BLOCKED,
    )
    assert is_valid_verification_transition(
        VERIFICATION_STATUS_CLAIMED, VERIFICATION_STATUS_BLOCKED,
    )


def test_claimed_can_retry_back_to_pending():
    from db.audit_evidence import (
        is_valid_verification_transition,
        VERIFICATION_STATUS_CLAIMED, VERIFICATION_STATUS_PENDING,
    )
    assert is_valid_verification_transition(
        VERIFICATION_STATUS_CLAIMED, VERIFICATION_STATUS_PENDING,
    )


def test_claimed_can_exhaust_retries():
    from db.audit_evidence import (
        is_valid_verification_transition,
        VERIFICATION_STATUS_CLAIMED,
        VERIFICATION_STATUS_EXHAUSTED_RETRIES,
    )
    assert is_valid_verification_transition(
        VERIFICATION_STATUS_CLAIMED,
        VERIFICATION_STATUS_EXHAUSTED_RETRIES,
    )


def test_terminal_states_have_no_outbound():
    from db.audit_evidence import (
        is_valid_verification_transition,
        VERIFICATION_TERMINAL, VALID_VERIFICATION_TRANSITIONS,
    )
    for terminal in VERIFICATION_TERMINAL:
        for any_state in VALID_VERIFICATION_TRANSITIONS:
            assert not is_valid_verification_transition(
                terminal, any_state,
            ), (
                f"terminal {terminal!r} must not transition to "
                f"{any_state!r}"
            )


def test_cannot_skip_claimed_stage():
    """Pending can't jump straight to succeeded/failed — must
    transition through claimed first. Prevents bypassing the
    worker-claim safety."""
    from db.audit_evidence import (
        is_valid_verification_transition,
        VERIFICATION_STATUS_PENDING,
        VERIFICATION_STATUS_SUCCEEDED,
        VERIFICATION_STATUS_FAILED,
        VERIFICATION_STATUS_BLOCKED,
    )
    assert not is_valid_verification_transition(
        VERIFICATION_STATUS_PENDING, VERIFICATION_STATUS_SUCCEEDED,
    )
    assert not is_valid_verification_transition(
        VERIFICATION_STATUS_PENDING, VERIFICATION_STATUS_FAILED,
    )
    assert not is_valid_verification_transition(
        VERIFICATION_STATUS_PENDING, VERIFICATION_STATUS_BLOCKED,
    )


# =====================================================================
# Idempotency key
# =====================================================================


def test_idempotency_key_stable():
    from db.audit_evidence import compute_verification_idempotency_key
    a = compute_verification_idempotency_key(
        audit_run_id="r-1", verifier_id="pdp_renders", product_key="p-1",
    )
    b = compute_verification_idempotency_key(
        audit_run_id="r-1", verifier_id="pdp_renders", product_key="p-1",
    )
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_idempotency_key_distinguishes_verifier():
    from db.audit_evidence import compute_verification_idempotency_key
    a = compute_verification_idempotency_key(
        audit_run_id="r-1", verifier_id="pdp_renders",
        product_key="p-1",
    )
    b = compute_verification_idempotency_key(
        audit_run_id="r-1", verifier_id="pdp_in_sitemap",
        product_key="p-1",
    )
    assert a != b


def test_idempotency_key_distinguishes_audit_run():
    """Re-auditing a merchant produces a new audit_run_id, which
    must result in a fresh verification — same brand, new probes."""
    from db.audit_evidence import compute_verification_idempotency_key
    a = compute_verification_idempotency_key(
        audit_run_id="r-1", verifier_id="pdp_renders",
    )
    b = compute_verification_idempotency_key(
        audit_run_id="r-2", verifier_id="pdp_renders",
    )
    assert a != b


def test_idempotency_key_handles_brand_level_no_product():
    """Brand-level verifications (no product_key) still produce
    stable keys — product_key=None is treated as the empty string
    in the dedupe tuple."""
    from db.audit_evidence import compute_verification_idempotency_key
    a = compute_verification_idempotency_key(
        audit_run_id="r-1", verifier_id="frontend_agent_cite",
        product_key=None,
    )
    b = compute_verification_idempotency_key(
        audit_run_id="r-1", verifier_id="frontend_agent_cite",
    )
    assert a == b


# =====================================================================
# DB round-trip — skipped (same rationale as P2.1/P3.1)
# =====================================================================


@pytest.mark.skip(
    reason="Round-trip integration via in-memory SQLite is fragile "
    "for the FOR UPDATE SKIP LOCKED + partial-index surface this "
    "module uses (matches the existing skip rationale in "
    "test_phase2_audit_runs_lifecycle.py). The accessors are "
    "exercised against real Postgres in the verification_run_worker "
    "integration flow that lands in P5.2."
)
async def test_round_trip_postgres_only():
    pass
