"""W5 P7 — per-merchant executor consent (auto-execute toggle +
pending_approval stage).

Covers the DB-layer consent surface added in db/executor_runs.py:
  - the pending_approval gate stage + its transitions
  - the 30-day TTL on pending rows
  - approve (idempotent → queued exactly once) + ownership guard
  - decline (idempotent → terminal 'declined')
  - the worker's claim scope EXCLUDES pending_approval

Mirrors the established P3 test pattern: pure-logic for the state
machine + monkey-patched DB accessors for the branch logic (the raw
Postgres UPDATE round-trip is exercised against real Postgres in the
integration flow, per test_phase3_executor_runs_durability.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest


# =====================================================================
# State machine — pure logic
# =====================================================================


def test_pending_approval_can_be_approved_to_queued():
    from db import executor_runs as er
    assert er.is_valid_stage_transition(
        er.STAGE_PENDING_APPROVAL, er.STAGE_QUEUED,
    )


def test_pending_approval_can_be_declined_to_terminal():
    from db import executor_runs as er
    assert er.is_valid_stage_transition(
        er.STAGE_PENDING_APPROVAL, er.STAGE_DECLINED,
    )


def test_declined_is_terminal():
    from db import executor_runs as er
    assert er.STAGE_DECLINED in er.TERMINAL_STAGES
    for any_stage in er.VALID_STAGE_TRANSITIONS:
        assert not er.is_valid_stage_transition(er.STAGE_DECLINED, any_stage)


def test_pending_approval_cannot_jump_straight_to_a_terminal():
    """A pending row must go through queued → claimed before it can
    succeed — approval never skips the worker."""
    from db import executor_runs as er
    assert not er.is_valid_stage_transition(
        er.STAGE_PENDING_APPROVAL, er.STAGE_SUCCEEDED,
    )
    assert not er.is_valid_stage_transition(
        er.STAGE_PENDING_APPROVAL, er.STAGE_CLAIMED,
    )


def test_worker_claim_scope_excludes_pending_approval():
    """(b) The worker only claims 'queued'/'claimed'. pending_approval
    is neither active nor named in the claim SQL, so pending rows are
    inert until approved."""
    from db import executor_runs as er
    import inspect

    assert er.STAGE_PENDING_APPROVAL not in er.ACTIVE_STAGES
    # The raw claim query filters on stage IN ('queued', 'claimed').
    src = inspect.getsource(er.claim_next_pending_executor_run)
    assert "pending_approval" not in src
    assert "stage IN ('queued', 'claimed')" in src


def test_pending_approval_dedupes_a_redispatch():
    """A pending row must count as in-flight so re-dispatching the same
    (agent, merchant, audit) tuple doesn't create a duplicate."""
    from db import executor_runs as er
    assert er.STAGE_PENDING_APPROVAL in er.IN_FLIGHT_STAGES


# =====================================================================
# TTL helper — pure logic
# =====================================================================


def test_pending_ttl_fresh_row_not_expired():
    from db import executor_runs as er
    recent = (er._now_utc() - timedelta(days=1)).isoformat()
    assert er._pending_is_expired(recent) is False


def test_pending_ttl_old_row_expired():
    from db import executor_runs as er
    old = (
        er._now_utc()
        - timedelta(days=er.PENDING_APPROVAL_TTL_DAYS + 1)
    ).isoformat()
    assert er._pending_is_expired(old) is True


def test_pending_ttl_none_is_not_expired():
    from db import executor_runs as er
    assert er._pending_is_expired(None) is False


# =====================================================================
# approve / decline — monkey-patched DB branch logic
# =====================================================================


class _FakeDB:
    """Records UPDATE calls; returns a configurable row for the guarded
    UPDATE ... RETURNING (truthy = matched, None = no-op)."""

    def __init__(self, update_returns: Optional[Any]):
        self.update_returns = update_returns
        self.fetch_one_calls: List[Any] = []

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls.append({"query": query, "values": values})
        return self.update_returns


def _fresh_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _old_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()


def _patch_fetch(monkeypatch, rows: List[Optional[Dict[str, Any]]]):
    """Serve a scripted sequence of fetch_executor_run_by_id results."""
    from db import executor_runs as er
    seq = list(rows)

    async def _fake_fetch(*, run_id):
        return seq.pop(0) if seq else None

    monkeypatch.setattr(er, "fetch_executor_run_by_id", _fake_fetch)


@pytest.mark.asyncio
async def test_approve_pending_transitions_to_queued(monkeypatch):
    from db import executor_runs as er
    _patch_fetch(monkeypatch, [{
        "run_id": "r1", "merchant_id": "m-1",
        "stage": er.STAGE_PENDING_APPROVAL,
        "stage_updated_at": _fresh_iso(),
    }])
    fake_db = _FakeDB(update_returns={"run_id": "r1"})
    monkeypatch.setattr(er, "database", fake_db)

    res = await er.approve_executor_run(run_id="r1", merchant_id="m-1")
    assert res["status"] == "success"
    assert res["stage"] == er.STAGE_QUEUED
    assert len(fake_db.fetch_one_calls) == 1  # one UPDATE


@pytest.mark.asyncio
async def test_approve_is_idempotent_no_double_enqueue(monkeypatch):
    """(c) Approving an already-queued run is a no-op success and does
    NOT issue a second UPDATE — approve happens exactly once."""
    from db import executor_runs as er
    _patch_fetch(monkeypatch, [{
        "run_id": "r1", "merchant_id": "m-1",
        "stage": er.STAGE_QUEUED,  # already approved
        "stage_updated_at": _fresh_iso(),
    }])
    fake_db = _FakeDB(update_returns={"run_id": "r1"})
    monkeypatch.setattr(er, "database", fake_db)

    res = await er.approve_executor_run(run_id="r1", merchant_id="m-1")
    assert res["status"] == "success"
    assert res.get("noop") is True
    assert fake_db.fetch_one_calls == []  # no UPDATE issued


@pytest.mark.asyncio
async def test_approve_rejects_wrong_merchant(monkeypatch):
    from db import executor_runs as er
    _patch_fetch(monkeypatch, [{
        "run_id": "r1", "merchant_id": "OWNER",
        "stage": er.STAGE_PENDING_APPROVAL,
        "stage_updated_at": _fresh_iso(),
    }])
    fake_db = _FakeDB(update_returns={"run_id": "r1"})
    monkeypatch.setattr(er, "database", fake_db)

    res = await er.approve_executor_run(run_id="r1", merchant_id="ATTACKER")
    assert res["status"] == "forbidden"
    assert fake_db.fetch_one_calls == []  # never touched the row


@pytest.mark.asyncio
async def test_approve_expired_pending_is_rejected(monkeypatch):
    """(f) A pending row past the 30-day TTL can't be approved."""
    from db import executor_runs as er
    _patch_fetch(monkeypatch, [{
        "run_id": "r1", "merchant_id": "m-1",
        "stage": er.STAGE_PENDING_APPROVAL,
        "stage_updated_at": _old_iso(),
    }])
    fake_db = _FakeDB(update_returns={"run_id": "r1"})
    monkeypatch.setattr(er, "database", fake_db)

    res = await er.approve_executor_run(run_id="r1", merchant_id="m-1")
    assert res["status"] == "expired"
    assert fake_db.fetch_one_calls == []  # short-circuits before UPDATE


@pytest.mark.asyncio
async def test_approve_missing_run(monkeypatch):
    from db import executor_runs as er
    _patch_fetch(monkeypatch, [None])
    monkeypatch.setattr(er, "database", _FakeDB(update_returns=None))
    res = await er.approve_executor_run(run_id="nope", merchant_id="m-1")
    assert res["status"] == "not_found"


@pytest.mark.asyncio
async def test_decline_pending_transitions_to_declined(monkeypatch):
    """(d) Decline moves a pending row to the terminal 'declined'."""
    from db import executor_runs as er
    _patch_fetch(monkeypatch, [{
        "run_id": "r1", "merchant_id": "m-1",
        "stage": er.STAGE_PENDING_APPROVAL,
        "stage_updated_at": _fresh_iso(),
    }])
    fake_db = _FakeDB(update_returns={"run_id": "r1"})
    monkeypatch.setattr(er, "database", fake_db)

    res = await er.decline_executor_run(run_id="r1", merchant_id="m-1")
    assert res["status"] == "success"
    assert res["stage"] == er.STAGE_DECLINED
    assert len(fake_db.fetch_one_calls) == 1


@pytest.mark.asyncio
async def test_decline_is_idempotent(monkeypatch):
    from db import executor_runs as er
    _patch_fetch(monkeypatch, [{
        "run_id": "r1", "merchant_id": "m-1",
        "stage": er.STAGE_DECLINED,  # already declined
        "stage_updated_at": _fresh_iso(),
    }])
    fake_db = _FakeDB(update_returns={"run_id": "r1"})
    monkeypatch.setattr(er, "database", fake_db)

    res = await er.decline_executor_run(run_id="r1", merchant_id="m-1")
    assert res["status"] == "success"
    assert res.get("noop") is True
    assert fake_db.fetch_one_calls == []  # no second UPDATE


@pytest.mark.asyncio
async def test_decline_rejects_wrong_merchant(monkeypatch):
    from db import executor_runs as er
    _patch_fetch(monkeypatch, [{
        "run_id": "r1", "merchant_id": "OWNER",
        "stage": er.STAGE_PENDING_APPROVAL,
        "stage_updated_at": _fresh_iso(),
    }])
    monkeypatch.setattr(er, "database", _FakeDB(update_returns=None))
    res = await er.decline_executor_run(run_id="r1", merchant_id="ATTACKER")
    assert res["status"] == "forbidden"


# =====================================================================
# enqueue stage guard
# =====================================================================


@pytest.mark.asyncio
async def test_enqueue_rejects_unknown_stage():
    """enqueue_executor_run only accepts queued / pending_approval — a
    typo'd stage must fail loud, not silently create an orphan row."""
    from db import executor_runs as er

    with pytest.raises(ValueError):
        await er.enqueue_executor_run(agent_name="a", stage="succeeded")
