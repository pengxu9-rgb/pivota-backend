"""The orphan revocation sweep, and the row state that makes it reachable at all.

WHAT AN ORPHAN IS. `POST /agent/v1/cards` refuses a mint when the issuer will not confirm the
constraints — REAP_CONSTRAINTS_MISMATCH or REAP_CONSTRAINTS_UNCONFIRMED. Those refusals happen
AFTER a 2xx, so a real card exists: possibly uncapped, possibly not merchant-locked. Before this
sweep, that card was recorded only in a log line and nothing ever touched it again.

THE LOAD-BEARING PART IS THE ROW, NOT THE JOB. If `issuer_card_ref` is not persisted on the
failed row, the sweep runs, finds nothing, reports success, and the orphan lives on — a green
run that means the opposite of what it looks like. So the first tests here assert the ref
survives the refusal, and one asserts the sweep is NOT fooled by an empty queue.
"""

from __future__ import annotations

import pytest

import jobs.agent_card_revocation_sweep as sweep
from services.card_issuers import CardIssuerError


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("AGENT_CARD_REVOCATION_SWEEP_ENABLED", "1")


class _Issuer:
    """Records what it was asked to kill, and can refuse on demand."""

    name = "reap"

    def __init__(self, refuse_refs=()):
        self.revoked = []
        self.refuse_refs = set(refuse_refs)

    async def revoke(self, issuer_card_ref: str) -> None:
        if issuer_card_ref in self.refuse_refs:
            raise CardIssuerError("REAP_REVOKE_UNCONFIRMED", "issuer did not confirm")
        self.revoked.append(issuer_card_ref)


def _orphan(card_id="card_1", ref="reap_1", issuer="reap", **kw):
    row = {
        "card_id": card_id,
        "agent_id": "agent_1",
        "issuer": issuer,
        "issuer_card_ref": ref,
        "merchant_domain": "cosrx.com",
        "amount_cap_minor": 2317,
        "currency": "USD",
        "failure_reason": "REAP_CONSTRAINTS_UNCONFIRMED",
        "created_at": None,
    }
    row.update(kw)
    return row


def _install(monkeypatch, rows, issuer):
    marked = []

    async def _list(limit=100):
        return list(rows)

    async def _mark(card_id):
        marked.append(card_id)
        return True

    monkeypatch.setattr(sweep, "list_orphaned_cards", _list)
    monkeypatch.setattr(sweep, "mark_revoked", _mark)
    monkeypatch.setattr(sweep, "resolve_issuer", lambda: issuer)
    return marked


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_confirmed_revocation_advances_the_row(monkeypatch):
    issuer = _Issuer()
    marked = _install(monkeypatch, [_orphan()], issuer)

    out = await sweep.run_agent_card_revocation_sweep()

    assert issuer.revoked == ["reap_1"], "the issuer must actually be called"
    assert marked == ["card_1"]
    assert out["orphans"] == 1 and out["revoked"] == 1 and out["unconfirmed"] == 0


@pytest.mark.asyncio
async def test_an_empty_queue_is_reported_as_empty_not_as_work_done(monkeypatch):
    issuer = _Issuer()
    _install(monkeypatch, [], issuer)

    out = await sweep.run_agent_card_revocation_sweep()

    assert out["orphans"] == 0 and out["revoked"] == 0
    assert issuer.revoked == []


# --------------------------------------------------------------------------------------
# Unconfirmed revocation must NOT advance the row
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unconfirmed_revocation_leaves_the_row_for_the_next_run(monkeypatch):
    """Rule 1. Marking it revoked on an unconfirmed call is how a live card gets forgotten."""
    issuer = _Issuer(refuse_refs={"reap_1"})
    marked = _install(monkeypatch, [_orphan()], issuer)

    out = await sweep.run_agent_card_revocation_sweep()

    assert marked == [], "an unconfirmed revocation must not advance the row"
    assert out["revoked"] == 0 and out["unconfirmed"] == 1
    assert out["orphans"] == 1, "it is still counted as found, so it stays visible"


@pytest.mark.asyncio
async def test_one_failing_orphan_does_not_block_the_others(monkeypatch):
    """Rule 3, with oldest-first ordering: a stuck head must not starve the queue."""
    issuer = _Issuer(refuse_refs={"reap_1"})
    rows = [_orphan("card_1", "reap_1"), _orphan("card_2", "reap_2"), _orphan("card_3", "reap_3")]
    marked = _install(monkeypatch, rows, issuer)

    out = await sweep.run_agent_card_revocation_sweep()

    assert issuer.revoked == ["reap_2", "reap_3"]
    assert marked == ["card_2", "card_3"]
    assert out["revoked"] == 2 and out["unconfirmed"] == 1


@pytest.mark.asyncio
async def test_an_unexpected_exception_does_not_end_the_sweep(monkeypatch):
    class _Exploding(_Issuer):
        async def revoke(self, issuer_card_ref):
            if issuer_card_ref == "reap_1":
                raise RuntimeError("boom")
            self.revoked.append(issuer_card_ref)

    issuer = _Exploding()
    marked = _install(monkeypatch, [_orphan("card_1", "reap_1"), _orphan("card_2", "reap_2")], issuer)

    out = await sweep.run_agent_card_revocation_sweep()

    assert marked == ["card_2"]
    assert out["unconfirmed"] == 1 and out["revoked"] == 1


@pytest.mark.asyncio
async def test_a_row_that_will_not_advance_is_counted_unconfirmed(monkeypatch):
    """Confirmed dead upstream but our UPDATE matched nothing — the row is not what we thought."""
    issuer = _Issuer()

    async def _list(limit=100):
        return [_orphan()]

    async def _mark(card_id):
        return False

    monkeypatch.setattr(sweep, "list_orphaned_cards", _list)
    monkeypatch.setattr(sweep, "mark_revoked", _mark)
    monkeypatch.setattr(sweep, "resolve_issuer", lambda: issuer)

    out = await sweep.run_agent_card_revocation_sweep()

    assert out["revoked"] == 0 and out["unconfirmed"] == 1


# --------------------------------------------------------------------------------------
# Fail-closed conditions
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_sweep_touches_nothing(monkeypatch):
    monkeypatch.delenv("AGENT_CARD_REVOCATION_SWEEP_ENABLED", raising=False)
    issuer = _Issuer()
    marked = _install(monkeypatch, [_orphan()], issuer)

    out = await sweep.run_agent_card_revocation_sweep()

    assert out["enabled"] is False
    assert issuer.revoked == [] and marked == []


@pytest.mark.asyncio
async def test_no_issuer_configured_is_an_error_not_an_empty_run(monkeypatch):
    """A misconfigured issuer must not be indistinguishable from 'nothing to do'."""
    _install(monkeypatch, [_orphan()], None)
    monkeypatch.setattr(sweep, "resolve_issuer", lambda: None)

    out = await sweep.run_agent_card_revocation_sweep()

    assert out.get("error") == "no_issuer"
    assert out["revoked"] == 0


@pytest.mark.asyncio
async def test_an_orphan_from_a_DIFFERENT_issuer_is_never_revoked(monkeypatch):
    """The ref belongs to another provider's namespace; sending it names someone else's card."""
    issuer = _Issuer()  # name = "reap"
    marked = _install(monkeypatch, [_orphan(issuer="mock", ref="mockcard_9")], issuer)

    out = await sweep.run_agent_card_revocation_sweep()

    assert issuer.revoked == [], "a ref minted elsewhere must not be sent to this adapter"
    assert marked == []
    assert out["skipped_no_issuer"] == 1


# --------------------------------------------------------------------------------------
# The row state the sweep depends on
# --------------------------------------------------------------------------------------

def test_the_orphan_predicate_is_structural_not_a_code_list():
    """`failed` + a ref cannot occur on the success path, so it needs no failure_reason filter.

    Pinned because the tempting 'fix' — filtering on the two known codes — silently stops
    sweeping any future failure path that also gets past a 2xx.
    """
    import inspect

    import db.agent_issued_cards as mod

    sql = inspect.getsource(mod.list_orphaned_cards)
    assert "status = 'failed'" in sql
    assert "issuer_card_ref IS NOT NULL" in sql
    assert "failure_reason =" not in sql, "must not filter on a hardcoded code list"


def test_mark_revoked_cannot_touch_a_live_issued_card():
    """Guarded on status='failed': a sweep bug must not be able to cancel a card in use."""
    import inspect

    import db.agent_issued_cards as mod

    sql = inspect.getsource(mod.mark_revoked)
    assert "status = 'failed'" in sql
    assert "'issued'" not in sql


def test_the_orphan_write_keeps_the_issuer_ref():
    """Without this the sweep has nothing to find and every run is a vacuous green."""
    import inspect

    import db.agent_issued_cards as mod

    sql = inspect.getsource(mod.mark_failed_with_orphan)
    assert "issuer_card_ref = :ref" in sql
    assert "status = 'failed'" in sql


def test_plain_mark_failed_does_NOT_write_a_ref():
    """The two events are kept separate on purpose: a failure that minted nothing must never
    leave a stray ref for the sweep to chase."""
    import inspect

    import db.agent_issued_cards as mod

    sql = inspect.getsource(mod.mark_failed)
    assert "issuer_card_ref" not in sql


@pytest.mark.asyncio
async def test_a_DB_error_marking_one_row_does_not_end_the_batch(monkeypatch):
    """Rule 3 applied to the OTHER half of the loop body.

    The per-row try used to wrap only `issuer.revoke`, so a database error in `mark_revoked` —
    a pool blip, a lost connection — escaped and ended the sweep. The damage is specific: it
    happens AFTER the issuer has already killed that card, so the run stops at the one row we
    know is dead and every orphan behind it goes untouched for the rest of the run. Those are
    the cards that may still be spendable, which is the entire reason this job exists.
    """
    issuer = _Issuer()
    rows = [_orphan("card_1", "reap_1"), _orphan("card_2", "reap_2"), _orphan("card_3", "reap_3")]
    marked = []

    async def _list(limit=100):
        return list(rows)

    async def _mark(card_id):
        if card_id == "card_2":
            raise RuntimeError("connection reset by peer")
        marked.append(card_id)
        return True

    monkeypatch.setattr(sweep, "list_orphaned_cards", _list)
    monkeypatch.setattr(sweep, "mark_revoked", _mark)
    monkeypatch.setattr(sweep, "resolve_issuer", lambda: issuer)

    out = await sweep.run_agent_card_revocation_sweep()

    assert issuer.revoked == ["reap_1", "reap_2", "reap_3"], "every orphan is still attempted"
    assert marked == ["card_1", "card_3"], "rows either side of the failure still advance"
    assert out["orphans"] == 3 and out["revoked"] == 2
    # Counted, and counted DISTINCTLY: this is not "we could not confirm the card is dead" — we
    # confirmed it is, and only our row is behind. An operator reading the summary needs that
    # difference, because the two have opposite urgencies.
    assert out["unconfirmed"] == 1
    assert out["revoked_row_not_advanced"] == 1


@pytest.mark.asyncio
async def test_the_row_write_failure_is_reported_distinctly_from_an_unconfirmed_revoke(monkeypatch):
    """The POSITIVE counterpart to the count above: a plain unconfirmed revocation must NOT
    carry the new key, or the two failures are indistinguishable again."""
    issuer = _Issuer(refuse_refs={"reap_1"})
    _install(monkeypatch, [_orphan()], issuer)

    out = await sweep.run_agent_card_revocation_sweep()

    assert out["unconfirmed"] == 1
    assert "revoked_row_not_advanced" not in out
