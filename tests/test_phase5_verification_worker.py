"""Phase 5.2 — verification_run_worker tests.

Mirrors the P3.2 executor_run_worker test pattern: monkey-patch
the DB accessors + verifier registry with fakes, validate the
worker's claim → execute → mark flow including retry / blocked /
unknown-verifier paths.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest


# =====================================================================
# Stubs
# =====================================================================


class _VerifAccessors:
    """Records each accessor call so tests can assert behavior."""

    def __init__(self, claim_payload: Optional[Dict[str, Any]]):
        self.claim_payload = claim_payload
        self.succeeded: List[Dict[str, Any]] = []
        self.blocked: List[Dict[str, Any]] = []
        self.failed_with_retry: List[Dict[str, Any]] = []
        # mark_verification_failed_with_retry's return: "pending"
        # (re-enqueue) or "exhausted_retries" (terminal).
        self.failed_returns: str = "pending"

    async def claim_next_pending_verification(self, *, worker_id):
        return self.claim_payload

    async def mark_verification_succeeded(
        self, *, verify_id, worker_id, evidence_jsonb=None,
    ):
        self.succeeded.append({
            "verify_id": verify_id, "worker_id": worker_id,
            "evidence_jsonb": evidence_jsonb,
        })
        return True

    async def mark_verification_blocked(
        self, *, verify_id, worker_id, error_message,
        evidence_jsonb=None,
    ):
        self.blocked.append({
            "verify_id": verify_id, "worker_id": worker_id,
            "error_message": error_message,
            "evidence_jsonb": evidence_jsonb,
        })
        return True

    async def mark_verification_failed_with_retry(
        self, *, verify_id, worker_id, error_message,
        evidence_jsonb=None,
    ):
        self.failed_with_retry.append({
            "verify_id": verify_id, "worker_id": worker_id,
            "error_message": error_message,
            "evidence_jsonb": evidence_jsonb,
        })
        return self.failed_returns


def _patch_worker_deps(
    monkeypatch, *, claim_payload, registry_overrides=None,
):
    """Patch the worker's DB accessors + verifier lookup. We
    monkey-patch _lookup_verifier rather than touching
    _verifier_registry directly so the global registry (populated
    by services.verifiers at import time) isn't polluted across
    tests — a real production verifier registered by import
    side-effect must remain available to other tests."""
    from db import audit_evidence as ae
    from services import verification_run_worker as worker

    accessors = _VerifAccessors(claim_payload=claim_payload)
    monkeypatch.setattr(
        ae, "claim_next_pending_verification",
        accessors.claim_next_pending_verification,
    )
    monkeypatch.setattr(
        ae, "mark_verification_succeeded",
        accessors.mark_verification_succeeded,
    )
    monkeypatch.setattr(
        ae, "mark_verification_blocked",
        accessors.mark_verification_blocked,
    )
    monkeypatch.setattr(
        ae, "mark_verification_failed_with_retry",
        accessors.mark_verification_failed_with_retry,
    )

    # Test-local registry. _lookup_verifier reads from this
    # controlled dict; the real _verifier_registry is left
    # untouched.
    test_registry = dict(registry_overrides or {})

    def fake_lookup(verifier_id):
        return test_registry.get(verifier_id)

    monkeypatch.setattr(worker, "_lookup_verifier", fake_lookup)
    return accessors


# =====================================================================
# Tests
# =====================================================================


@pytest.mark.asyncio
async def test_no_op_when_queue_empty(monkeypatch):
    accessors = _patch_worker_deps(monkeypatch, claim_payload=None)
    from services.verification_run_worker import (
        process_one_verification_run,
    )
    processed = await process_one_verification_run()
    assert processed is False
    assert accessors.succeeded == []
    assert accessors.blocked == []
    assert accessors.failed_with_retry == []


@pytest.mark.asyncio
async def test_happy_path_succeeded_calls_mark_succeeded(monkeypatch):
    from services.verification_run_worker import (
        process_one_verification_run, VerifierResult,
    )

    async def fake_verifier(ctx):
        return VerifierResult(
            status="succeeded",
            evidence_jsonb={"pdp_status_code": 200},
        )

    accessors = _patch_worker_deps(
        monkeypatch,
        claim_payload={
            "verify_id": "v-1",
            "audit_run_id": "audit-1",
            "verifier_id": "pdp_renders",
            "product_key": "p-1",
            "retry_count": 0,
            "max_retries": 2,
        },
        registry_overrides={"pdp_renders": fake_verifier},
    )

    processed = await process_one_verification_run()
    assert processed is True
    assert len(accessors.succeeded) == 1
    assert accessors.succeeded[0]["evidence_jsonb"] == {
        "pdp_status_code": 200,
    }
    assert accessors.blocked == []
    assert accessors.failed_with_retry == []


@pytest.mark.asyncio
async def test_blocked_routes_to_terminal_blocked_not_retry(
    monkeypatch,
):
    """Verifier explicitly returning status='blocked' goes to the
    terminal blocked state, NOT through retry. This is the
    distinguishing P5.1 design."""
    from services.verification_run_worker import (
        process_one_verification_run, VerifierResult,
    )

    async def gsc_consent_revoked(ctx):
        return VerifierResult(
            status="blocked",
            error_message="merchant revoked GSC consent",
            evidence_jsonb={"gsc_state": "consent_revoked"},
        )

    accessors = _patch_worker_deps(
        monkeypatch,
        claim_payload={
            "verify_id": "v-2",
            "audit_run_id": "audit-1",
            "verifier_id": "gsc_url_submitted",
            "product_key": "p-1",
            "retry_count": 0,
            "max_retries": 2,
        },
        registry_overrides={"gsc_url_submitted": gsc_consent_revoked},
    )

    await process_one_verification_run()
    assert accessors.succeeded == []
    assert accessors.failed_with_retry == []
    assert len(accessors.blocked) == 1
    assert (
        accessors.blocked[0]["error_message"]
        == "merchant revoked GSC consent"
    )


@pytest.mark.asyncio
async def test_failed_routes_to_retry(monkeypatch):
    """Verifier returning status='failed' (transient) goes through
    the retry path, NOT terminal blocked."""
    from services.verification_run_worker import (
        process_one_verification_run, VerifierResult,
    )

    async def transient_fail(ctx):
        return VerifierResult(
            status="failed",
            error_message="http 503 upstream",
        )

    accessors = _patch_worker_deps(
        monkeypatch,
        claim_payload={
            "verify_id": "v-3",
            "audit_run_id": "audit-1",
            "verifier_id": "pdp_renders",
            "retry_count": 0,
            "max_retries": 2,
        },
        registry_overrides={"pdp_renders": transient_fail},
    )

    await process_one_verification_run()
    assert accessors.succeeded == []
    assert accessors.blocked == []
    assert len(accessors.failed_with_retry) == 1
    assert "503" in accessors.failed_with_retry[0]["error_message"]


@pytest.mark.asyncio
async def test_uncaught_exception_routes_to_retry_with_traceback(
    monkeypatch,
):
    """Verifier raising unexpectedly → caught + retry routing
    with traceback captured in evidence_jsonb."""
    from services.verification_run_worker import (
        process_one_verification_run, VerifierResult,
    )

    async def boom(ctx):
        raise ValueError("verifier exploded")

    accessors = _patch_worker_deps(
        monkeypatch,
        claim_payload={
            "verify_id": "v-4",
            "audit_run_id": "audit-1",
            "verifier_id": "pdp_renders",
            "retry_count": 0,
            "max_retries": 2,
        },
        registry_overrides={"pdp_renders": boom},
    )

    await process_one_verification_run()
    assert accessors.succeeded == []
    assert len(accessors.failed_with_retry) == 1
    err = accessors.failed_with_retry[0]["evidence_jsonb"]
    assert err["stage"] == "verifier_execute"
    assert "verifier exploded" in err["message"]
    assert "ValueError" in err["traceback_truncated"]


@pytest.mark.asyncio
async def test_unknown_verifier_marks_blocked_to_avoid_loop(
    monkeypatch,
):
    """A claimed row with a verifier_id not in the registry
    (deployment skew — verifier removed but queue still has rows)
    is marked blocked rather than failed-with-retry. Prevents the
    row from looping forever when no worker has the verifier."""
    accessors = _patch_worker_deps(
        monkeypatch,
        claim_payload={
            "verify_id": "v-5",
            "audit_run_id": "audit-1",
            "verifier_id": "removed_verifier",
            "retry_count": 0,
            "max_retries": 2,
        },
        registry_overrides={},  # empty
    )
    # The worker first calls failed_with_retry (which returns
    # "pending" per failed_returns default), then upgrades to
    # blocked when the retry path tries to re-enqueue.
    accessors.failed_returns = "pending"

    from services.verification_run_worker import (
        process_one_verification_run,
    )
    await process_one_verification_run()
    # Should have invoked both failed_with_retry AND blocked
    assert len(accessors.failed_with_retry) == 1
    assert len(accessors.blocked) == 1
    assert (
        "removed_verifier" in accessors.blocked[0]["error_message"]
    )


@pytest.mark.asyncio
async def test_registry_replace_does_not_raise(monkeypatch):
    """register_verifier must tolerate replacement (tests do this
    via monkey-patch). The replacement is logged at debug, not
    warning, to avoid noise.

    Cleans up the test registration at teardown so the global
    registry isn't polluted for sibling tests (P5.7's drift test
    asserts the registered-set matches VERIFIER_SPECS exactly)."""
    from services.verification_run_worker import (
        _verifier_registry, register_verifier, VerifierResult,
    )

    async def v1(ctx):
        return VerifierResult(status="succeeded")

    async def v2(ctx):
        return VerifierResult(status="failed")

    key = "test_verifier_xyz_for_replacement_check"
    register_verifier(key, v1)
    register_verifier(key, v2)  # replacement; OK

    # Clean up so the drift test stays clean
    _verifier_registry.pop(key, None)


def test_verifier_result_default_evidence_is_none():
    """VerifierResult is a dataclass; default evidence_jsonb is
    None (not an empty dict). Important for the accessors which
    treat None as 'don't write' but {} as 'write empty object'."""
    from services.verification_run_worker import VerifierResult
    r = VerifierResult(status="succeeded")
    assert r.evidence_jsonb is None
    assert r.error_message is None
