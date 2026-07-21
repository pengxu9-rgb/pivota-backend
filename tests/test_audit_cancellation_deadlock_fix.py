"""Regression tests for the P0-1 cancellation finalization deadlock.

Before this fix:
  - cancel_audit_run only set cancelled_at; never transitioned stage.
  - claim_next_pending_run filtered out cancelled rows, so queued
    cancelled runs were never claimed → never finalized → forever
    stuck at stage=queued.
  - transition_stage's WHERE clause required cancelled_at IS NULL,
    so even if a worker DID hold a lease on an active run that got
    cancelled, the worker's "finalize to cancelled" transition would
    no-op → stuck forever at the active stage.
  - The worker had no cancellation polling at all.

The fix has three parts:
  1. cancel_audit_run atomically finalizes queued runs.
  2. transition_stage drops the cancelled_at IS NULL filter ONLY when
     to_stage == STAGE_CANCELLED (so cancellation can finalize but
     forward progress is still blocked after cancellation).
  3. The worker checks cancelled_at at the entry of every stage and
     calls transition_stage(... to_stage=STAGE_CANCELLED) to bail.

These tests assert the SQL shapes + worker behavior so the bug can't
silently reintroduce.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest


# =====================================================================
# Part 1: cancel_audit_run shape
# =====================================================================


@pytest.mark.asyncio
async def test_cancel_audit_run_finalizes_queued_atomically(monkeypatch):
    """A queued run with no worker lease must transition to
    stage=cancelled in one UPDATE — not just have cancelled_at set."""
    from db import merchant_audit_runs as mar

    executed: List[Dict[str, Any]] = []

    class DummyDB:
        async def execute(self, query):
            # databases-style: returns rowcount on UPDATE.
            executed.append({
                "compiled": str(query.compile(
                    compile_kwargs={"literal_binds": True},
                )),
                "values": getattr(query, "_values", None),
            })
            # First UPDATE (queued path) returns 1 row matched.
            return 1 if len(executed) == 1 else 0

    async def noop():
        return None

    monkeypatch.setattr(mar, "database", DummyDB())
    monkeypatch.setattr(mar, "ensure_merchant_audit_runs_table", noop)

    ok = await mar.cancel_audit_run(run_id="run-1")
    assert ok is True
    assert len(executed) == 1, (
        "Queued-path finalize should NOT fall through to the active "
        "flag-only UPDATE when it matched a row"
    )
    sql = executed[0]["compiled"]
    # The UPDATE must set stage to cancelled, not just cancelled_at.
    assert "stage" in sql.lower() and "'cancelled'" in sql.lower(), (
        f"Queued-path finalize must atomically set stage=cancelled. "
        f"Got SQL: {sql}"
    )
    assert "completed_at" in sql.lower(), (
        "Queued-path finalize should also set completed_at so the "
        "run is properly terminal"
    )


@pytest.mark.asyncio
async def test_cancel_audit_run_falls_back_to_flag_for_active(monkeypatch):
    """When the row is active (not queued), cancel_audit_run does the
    second UPDATE that just sets cancelled_at and lets the worker
    finalize."""
    from db import merchant_audit_runs as mar

    executed: List[str] = []

    class DummyDB:
        async def execute(self, query):
            executed.append(str(query.compile(
                compile_kwargs={"literal_binds": True},
            )))
            # First UPDATE matches 0 rows (no queued row); second
            # matches 1 row (active row gets cancelled_at).
            return 0 if len(executed) == 1 else 1

    async def noop():
        return None

    monkeypatch.setattr(mar, "database", DummyDB())
    monkeypatch.setattr(mar, "ensure_merchant_audit_runs_table", noop)

    ok = await mar.cancel_audit_run(run_id="run-active")
    assert ok is True
    assert len(executed) == 2, (
        "When the queued-path didn't match, cancel_audit_run must "
        "fall through to the active-flag UPDATE"
    )
    second = executed[1].lower()
    assert "cancelled_at" in second
    # The active-flag UPDATE must NOT change stage (only the worker
    # writes terminal stage for active runs, since it holds the lease).
    assert "set cancelled_at" in second.replace("\n", " ") or \
        "cancelled_at =" in second.replace("\n", " "), \
        f"Active flag-only update missing cancelled_at SET clause: {executed[1]}"


# =====================================================================
# Part 2: transition_stage gating
# =====================================================================


@pytest.mark.asyncio
async def test_transition_stage_blocks_forward_progress_when_cancelled(monkeypatch):
    """Forward transitions (e.g. discovering→probing) must keep the
    cancelled_at IS NULL guard so a cancellation that landed
    mid-stage prevents further forward progress."""
    from db import merchant_audit_runs as mar

    seen_sql: List[str] = []

    class DummyDB:
        async def execute(self, query):
            sql = str(query.compile(compile_kwargs={"literal_binds": True}))
            seen_sql.append(sql)
            return 1

    async def noop():
        return None

    monkeypatch.setattr(mar, "database", DummyDB())
    monkeypatch.setattr(mar, "ensure_merchant_audit_runs_table", noop)

    await mar.transition_stage(
        run_id="run-1",
        from_stage=mar.STAGE_DISCOVERING,
        to_stage=mar.STAGE_PROBING,
        worker_id="w-1",
    )
    assert len(seen_sql) == 1
    sql = seen_sql[0].lower()
    assert "cancelled_at is null" in sql, (
        "Forward transitions must include cancelled_at IS NULL so "
        "post-cancellation forward progress is blocked"
    )


@pytest.mark.asyncio
async def test_transition_stage_to_cancelled_allowed_when_cancelled_at_set(monkeypatch):
    """The finalize-to-cancelled transition MUST work even when
    cancelled_at is already set — that's the whole point of the
    fix. Asserts the WHERE clause does NOT include cancelled_at IS NULL
    for this transition."""
    from db import merchant_audit_runs as mar

    seen_sql: List[str] = []

    class DummyDB:
        async def execute(self, query):
            sql = str(query.compile(compile_kwargs={"literal_binds": True}))
            seen_sql.append(sql)
            return 1

    async def noop():
        return None

    monkeypatch.setattr(mar, "database", DummyDB())
    monkeypatch.setattr(mar, "ensure_merchant_audit_runs_table", noop)

    await mar.transition_stage(
        run_id="run-1",
        from_stage=mar.STAGE_PROBING,
        to_stage=mar.STAGE_CANCELLED,
        worker_id="w-1",
    )
    assert len(seen_sql) == 1
    sql = seen_sql[0].lower()
    assert "cancelled_at is null" not in sql, (
        "Finalize-to-cancelled must NOT include cancelled_at IS NULL "
        "in WHERE — that's the bug that deadlocked the lifecycle"
    )
    # Sanity: it should still be guarded by worker_id and from_stage.
    assert "claimed_by_worker" in sql
    assert "stage" in sql


# =====================================================================
# Part 3: Worker checks cancellation at every stage
# =====================================================================


@pytest.mark.asyncio
async def test_worker_finalizes_when_cancellation_detected_at_queued(monkeypatch):
    """Worker enters with current_stage=queued. cancel_audit_run set
    cancelled_at on the row (active-flag path). Worker must call
    transition_stage(queued→cancelled) and bail without doing the
    discovering work."""
    import services.audit_run_worker as worker
    from db import merchant_audit_runs as mar

    transitions: List[Dict[str, Any]] = []
    fetch_calls: List[str] = []

    async def fake_fetch(*, run_id):
        fetch_calls.append(run_id)
        # cancelled_at IS NOT NULL — cancellation has been requested.
        return {
            "run_id": run_id, "merchant_id": "m1",
            "stage": "queued", "cancelled_at": "2026-05-13T17:00:00+00:00",
        }

    async def fake_transition(**kwargs):
        transitions.append(kwargs)
        return True

    async def fake_resolve(**kwargs):
        raise AssertionError(
            "_resolve_merchant_and_products must NOT be called when "
            "a cancellation is detected at stage=queued"
        )

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)
    monkeypatch.setattr(mar, "transition_stage", fake_transition)
    monkeypatch.setattr(
        worker, "_resolve_merchant_and_products", fake_resolve,
    )

    result = await worker._process_one_audit_run_inner(
        run_id="run-1",
        merchant_id="m1",
        product_keys=["k1"],
        current_stage=mar.STAGE_QUEUED,
        brand_report=None,
        products=[],
        pivota_url_used=[],
        merchant_name="m1",
        merchant_domain=None,
        integration_state=None,
    )
    assert result is True
    assert len(fetch_calls) == 1
    assert len(transitions) == 1, (
        "Worker should issue exactly one transition (queued→cancelled)"
    )
    assert transitions[0]["from_stage"] == mar.STAGE_QUEUED
    assert transitions[0]["to_stage"] == mar.STAGE_CANCELLED


@pytest.mark.asyncio
async def test_worker_finalizes_when_cancellation_detected_at_discovering(monkeypatch):
    """Worker resumes from STAGE_DISCOVERING (stale lease replay). The
    cancellation check at the discovering-stage entry must finalize
    without invoking resolve."""
    import services.audit_run_worker as worker
    from db import merchant_audit_runs as mar

    transitions: List[Dict[str, Any]] = []

    async def fake_fetch(*, run_id):
        return {
            "run_id": run_id, "stage": "discovering",
            "cancelled_at": "2026-05-13T17:00:00+00:00",
        }

    async def fake_transition(**kwargs):
        transitions.append(kwargs)
        return True

    async def fake_resolve(**kwargs):
        raise AssertionError(
            "resolve must NOT be called when cancellation is detected"
        )

    async def fake_extend_lease(**kwargs):
        return True

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)
    monkeypatch.setattr(mar, "transition_stage", fake_transition)
    monkeypatch.setattr(mar, "extend_lease", fake_extend_lease)
    monkeypatch.setattr(
        worker, "_resolve_merchant_and_products", fake_resolve,
    )

    result = await worker._process_one_audit_run_inner(
        run_id="run-1",
        merchant_id="m1",
        product_keys=["k1"],
        current_stage=mar.STAGE_DISCOVERING,
        brand_report=None,
        products=[],
        pivota_url_used=[],
        merchant_name="m1",
        merchant_domain=None,
        integration_state=None,
    )
    assert result is True
    assert len(transitions) == 1
    assert transitions[0]["from_stage"] == mar.STAGE_DISCOVERING
    assert transitions[0]["to_stage"] == mar.STAGE_CANCELLED


@pytest.mark.asyncio
async def test_worker_no_cancellation_path_does_not_finalize(monkeypatch):
    """Sanity guard: when cancelled_at is None, the worker proceeds
    normally and does NOT issue a finalize-to-cancelled transition."""
    import services.audit_run_worker as worker
    from db import merchant_audit_runs as mar

    transitions: List[Dict[str, Any]] = []

    async def fake_fetch(*, run_id):
        return {"run_id": run_id, "stage": "queued", "cancelled_at": None}

    async def fake_transition(**kwargs):
        transitions.append(kwargs)
        # Simulate forward transition succeeding; the test only cares
        # that no cancellation transition fires.
        return True

    async def fake_resolve(**kwargs):
        # Return a benign tuple so the discovering stage completes
        # — keeps the test focused on cancellation behavior only.
        return ("m1", None, [], [], None)

    async def fake_extend_lease(**kwargs):
        return True

    async def fake_record_partial(**kwargs):
        return True

    async def fake_record_final(**kwargs):
        return True

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)
    monkeypatch.setattr(mar, "transition_stage", fake_transition)
    monkeypatch.setattr(mar, "extend_lease", fake_extend_lease)
    monkeypatch.setattr(mar, "record_partial_result", fake_record_partial)
    monkeypatch.setattr(
        worker, "_resolve_merchant_and_products", fake_resolve,
    )

    # Run with starting stage queued; we expect the queued→discovering
    # transition to fire (not a finalize-to-cancelled).
    await worker._process_one_audit_run_inner(
        run_id="run-1",
        merchant_id="m1",
        product_keys=["k1"],
        current_stage=mar.STAGE_QUEUED,
        brand_report=None,
        products=[],
        pivota_url_used=[],
        merchant_name="m1",
        merchant_domain=None,
        integration_state=None,
    )
    # At least one transition fired (forward), and none of them was
    # finalize-to-cancelled.
    assert any(
        t.get("to_stage") == mar.STAGE_DISCOVERING for t in transitions
    ), "Expected at least the queued→discovering forward transition"
    assert not any(
        t.get("to_stage") == mar.STAGE_CANCELLED for t in transitions
    ), (
        "Worker must NOT finalize to cancelled when cancelled_at is "
        "None — regression on the cancellation check"
    )
