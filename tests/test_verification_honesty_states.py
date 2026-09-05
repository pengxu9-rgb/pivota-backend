"""The four states that make "we have no verdict" storable.

`verification_runs.status` was a work-queue lifecycle standing in for an honesty
model: pending/claimed/succeeded/failed/exhausted_retries/blocked says what the
RUNNER did, never what we KNOW. So "we never checked" and "there is no row" were
the same thing, and a projection reading absence as a pass is the failure the
migration judgment rates Critical.

These pin the distinction the four states exist to make, and the invariants that
stop them becoming a new way to claim a pass.
"""
from __future__ import annotations

import pytest

import db.audit_evidence as ae


NO_VERDICT = (
    ae.VERIFICATION_STATUS_UNVERIFIED,
    ae.VERIFICATION_STATUS_SKIPPED,
    ae.VERIFICATION_STATUS_PROVIDER_FAILED,
    ae.VERIFICATION_STATUS_UNPARSEABLE,
)


def test_the_four_states_exist_with_the_spelled_values():
    """The P0 item names them exactly; a projection and an out-of-repo worker
    both key on these strings, so a rename is a contract break."""
    assert ae.VERIFICATION_STATUS_UNVERIFIED == "unverified"
    assert ae.VERIFICATION_STATUS_SKIPPED == "skipped"
    assert ae.VERIFICATION_STATUS_PROVIDER_FAILED == "provider_failed"
    assert ae.VERIFICATION_STATUS_UNPARSEABLE == "unparseable"


@pytest.mark.parametrize("status", NO_VERDICT)
def test_no_verdict_states_are_terminal(status):
    """A check that could not produce a verdict is finished, not retryable —
    otherwise the worker re-claims it forever and the state means nothing."""
    assert status in ae.VERIFICATION_TERMINAL
    assert ae.VALID_VERIFICATION_TRANSITIONS[status] == set()


@pytest.mark.parametrize("status", NO_VERDICT)
def test_no_verdict_states_are_never_active(status):
    """VERIFICATION_ACTIVE is what the claim query and the in-flight checks read.
    A no-verdict row appearing there would be re-claimed by a worker and would
    also read as 'still running' in the teaser, which is the opposite of what it
    records."""
    assert status not in ae.VERIFICATION_ACTIVE


@pytest.mark.parametrize("status", NO_VERDICT)
def test_reachable_only_from_claimed(status):
    """Only a worker that TOOK the row can conclude it has no verdict.

    Reachable from `pending` would let an enqueue write its own conclusion
    without anything ever running — the exact "never ran, looks decided" shape
    these states exist to prevent.
    """
    assert status in ae.VALID_VERIFICATION_TRANSITIONS[ae.VERIFICATION_STATUS_CLAIMED]
    assert status not in ae.VALID_VERIFICATION_TRANSITIONS[ae.VERIFICATION_STATUS_PENDING]
    assert ae.is_valid_verification_transition(ae.VERIFICATION_STATUS_CLAIMED, status)
    assert not ae.is_valid_verification_transition(ae.VERIFICATION_STATUS_PENDING, status)


def test_no_verdict_is_disjoint_from_succeeded():
    """`succeeded` is a verdict; these are the absence of one. If any of them
    ever joined the success set, a projection would count a check that never
    ran as a pass — which is the whole defect."""
    assert ae.VERIFICATION_STATUS_SUCCEEDED not in ae.VERIFICATION_NO_VERDICT
    assert not (ae.VERIFICATION_NO_VERDICT & ae.VERIFICATION_ACTIVE)
    assert ae.VERIFICATION_NO_VERDICT <= ae.VERIFICATION_TERMINAL


def test_provider_failed_is_distinct_from_blocked():
    """Different fixes. `blocked` means the upstream is unavailable and a retry
    is pointless; `provider_failed` means it answered uselessly. Collapsing them
    sends the operator to the wrong system."""
    assert ae.VERIFICATION_STATUS_PROVIDER_FAILED != ae.VERIFICATION_STATUS_BLOCKED
    assert ae.VERIFICATION_STATUS_BLOCKED not in ae.VERIFICATION_NO_VERDICT


def test_unparseable_is_distinct_from_provider_failed():
    """Also different fixes, and different owners: unparseable is ours to fix,
    provider_failed is theirs."""
    assert ae.VERIFICATION_STATUS_UNPARSEABLE != ae.VERIFICATION_STATUS_PROVIDER_FAILED


def test_the_public_teaser_reports_them_as_inconclusive_not_ready():
    """The one live consumer. Its fallthrough must treat a no-verdict row as
    inconclusive — reporting `ready` would publish 'we checked and it's fine'
    for a check that produced nothing."""
    import routes.store_audit_public_intake as intake

    for status in NO_VERDICT:
        assert status not in intake._ACTIVE_RUN_STATUSES
        assert status != "succeeded"
