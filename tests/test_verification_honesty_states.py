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


async def _noop(*a, **k):
    return None


# ---------------------------------------------------------------------
# The writer. Constants nothing can write are not "storable" — the item's own
# title is "make 'we have no verdict' STORABLE", and until this existed the
# vocabulary had no producer at all.
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_writer_refuses_a_status_that_is_not_a_no_verdict_state(
    monkeypatch,
):
    """Coercion is the failure this whole item removes. A caller that means
    `unparseable` and gets `succeeded` written has produced the pass-by-absence
    the states exist to prevent.

    ASSERTED ON THE DATABASE CALL, not the return value. `False` is what this
    function returns for a refused status AND for a status that reached the
    UPDATE and matched no row — so `is False` cannot tell a working guard from a
    deleted one. A mutant replacing the guard with `if False:` passed the
    earlier version of this test. What distinguishes them is whether the write
    was ATTEMPTED at all.
    """
    import db.audit_evidence as ae

    calls = []

    async def _spy(*a, **k):
        calls.append(a)
        return 0

    monkeypatch.setattr(ae.database, "execute", _spy)
    monkeypatch.setattr(ae, "ensure_audit_evidence_tables", _noop)

    for bad in (ae.VERIFICATION_STATUS_SUCCEEDED, ae.VERIFICATION_STATUS_PENDING,
                ae.VERIFICATION_STATUS_CLAIMED, "nonsense"):
        assert await ae.mark_verification_no_verdict(
            verify_id="v1", worker_id="w1", status=bad, reason="because",
        ) is False, f"{bad!r} was accepted as a no-verdict state"
    assert not calls, (
        f"a non-no-verdict status reached the database ({len(calls)} write(s)); "
        f"the guard is not refusing, it is only failing to match a row"
    )

    # The positive counterpart, same spy: a VALID state must get through to the
    # write. Without this the guard could reject everything and still pass.
    assert await ae.mark_verification_no_verdict(
        verify_id="v1", worker_id="w1",
        status=ae.VERIFICATION_STATUS_UNVERIFIED, reason="never attempted",
    ) is False  # 0 rows matched by the spy
    assert len(calls) == 1, "a valid no-verdict status did not reach the write"


@pytest.mark.asyncio
async def test_the_writer_refuses_an_empty_reason():
    """A row in one of these states must explain itself. One written without a
    reason reproduces the silence the states were added to remove, one column
    over."""
    import db.audit_evidence as ae

    for blank in ("", "   ", None):
        assert await ae.mark_verification_no_verdict(
            verify_id="v1", worker_id="w1",
            status=ae.VERIFICATION_STATUS_UNVERIFIED, reason=blank,
        ) is False, f"reason={blank!r} was accepted"


def _params(call) -> dict:
    """The bound parameters of the UPDATE the writer actually issued."""
    stmt = call[0]
    return dict(stmt.compile().params)


@pytest.mark.asyncio
async def test_every_no_verdict_state_reaches_the_write(monkeypatch):
    """The positive counterpart: the guard must not reject the whole vocabulary.

    ASSERTS THE WRITE WAS ISSUED, not the return value. The earlier version
    asserted `result in (True, False)` — which `bool` satisfies on every path,
    so it could not fail at all, and its docstring claimed it caught a
    `return False` mutant. It did not: inserting `return False` as the first
    statement left this test green.
    """
    import db.audit_evidence as ae

    calls = []

    async def _spy(*a, **k):
        calls.append(a)
        return 1

    monkeypatch.setattr(ae.database, "execute", _spy)
    monkeypatch.setattr(ae, "ensure_audit_evidence_tables", _noop)

    for status in sorted(ae.VERIFICATION_NO_VERDICT):
        assert await ae.mark_verification_no_verdict(
            verify_id="v1", worker_id="w1", status=status,
            reason="the browser commerce lane has never run",
        ) is True, f"{status!r} did not reach the write"
    assert len(calls) == len(ae.VERIFICATION_NO_VERDICT)


@pytest.mark.asyncio
async def test_the_reason_is_actually_stored(monkeypatch):
    """The feature is "make the reason STORABLE", and nothing asserted the
    reason was written.

    Proven vacuous: replacing the whole VALUES clause with `{"status": status}`
    — dropping the reason, both timestamps and evidence_jsonb — left all 21
    tests green. A test suite for "we store the reason" must fail when the
    reason is not stored.
    """
    import db.audit_evidence as ae

    calls = []

    async def _spy(*a, **k):
        calls.append(a)
        return 1

    monkeypatch.setattr(ae.database, "execute", _spy)
    monkeypatch.setattr(ae, "ensure_audit_evidence_tables", _noop)

    await ae.mark_verification_no_verdict(
        verify_id="v1", worker_id="w1",
        status=ae.VERIFICATION_STATUS_UNVERIFIED,
        reason="the browser commerce lane has never run",
    )

    params = _params(calls[0])
    assert params.get("status") == ae.VERIFICATION_STATUS_UNVERIFIED
    # In error_message, where the live ops rollup groups on it — a reason
    # hidden only in JSONB shows there as NULL.
    assert params.get("error_message") == (
        "the browser commerce lane has never run"
    )
    assert params.get("completed_at") is not None
    assert params.get("last_checked_at") is not None


@pytest.mark.asyncio
async def test_a_caller_supplied_evidence_payload_is_kept_and_annotated(
    monkeypatch,
):
    """And when the caller HAS evidence, the reason joins it rather than
    replacing it."""
    import db.audit_evidence as ae

    calls = []

    async def _spy(*a, **k):
        calls.append(a)
        return 1

    monkeypatch.setattr(ae.database, "execute", _spy)
    monkeypatch.setattr(ae, "ensure_audit_evidence_tables", _noop)

    await ae.mark_verification_no_verdict(
        verify_id="v1", worker_id="w1",
        status=ae.VERIFICATION_STATUS_UNPARSEABLE,
        reason="could not parse the answer",
        evidence_jsonb={"raw_prefix": "<<<not json>>>"},
    )

    payload = _params(calls[0]).get("evidence_jsonb") or {}
    assert payload.get("raw_prefix") == "<<<not json>>>", (
        "the caller's evidence was dropped"
    )
    assert payload.get("no_verdict_reason") == "could not parse the answer"


@pytest.mark.asyncio
async def test_no_payload_means_the_column_is_left_alone(monkeypatch):
    """The divergence a review caught: every sibling terminal transition OMITS
    evidence_jsonb when the caller passes none. Writing it unconditionally
    REPLACES the column, so a row carrying partial verifier output would lose
    it. Latent today (nothing writes it pre-terminal) and one line to prevent.
    """
    import db.audit_evidence as ae

    calls = []

    async def _spy(*a, **k):
        calls.append(a)
        return 1

    monkeypatch.setattr(ae.database, "execute", _spy)
    monkeypatch.setattr(ae, "ensure_audit_evidence_tables", _noop)

    await ae.mark_verification_no_verdict(
        verify_id="v1", worker_id="w1",
        status=ae.VERIFICATION_STATUS_SKIPPED, reason="out of scope",
    )

    assert "evidence_jsonb" not in _params(calls[0]), (
        "the column was written with no caller payload, which REPLACES "
        "whatever the row already had"
    )


def test_the_writer_is_reachable_only_from_claimed():
    """Same ownership guard as every other terminal transition: a row still
    `pending` has not been attempted, so nothing yet knows it has no verdict."""
    import inspect

    import db.audit_evidence as ae

    src = inspect.getsource(ae.mark_verification_no_verdict)
    assert "VERIFICATION_STATUS_CLAIMED" in src
    assert "claimed_by_worker" in src
    assert "claimed_until" in src
