"""Q-P1-7: mark_executor_run_succeeded must clear stale error_message.

Pre-fix, a run that bounced through STAGE_FAILED → retry →
STAGE_SUCCEEDED kept the error_message from the failed attempt. The
operator/BD view showed "succeeded" rows that carried stale failure
text, making it look like the run had a problem when it actually
recovered cleanly on retry.

Post-fix, mark_executor_run_succeeded sets error_message = NULL
alongside the status / completed_at update.

Strategy: monkeypatch `database.execute` to capture the SQLAlchemy
update statement and inspect its compiled `values` dict. We assert
that `error_message` is present in the SET clause with value None
regardless of what the row previously held — clearing it is the
contract.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


# ---------------------------------------------------------------------------
# Stub harness
# ---------------------------------------------------------------------------


class _CapturingDatabase:
    """Captures every executed statement. Returns 1 row affected to
    let `mark_executor_run_succeeded` return True."""

    def __init__(self) -> None:
        self.executed: List[Any] = []

    async def execute(self, stmt: Any) -> int:
        self.executed.append(stmt)
        return 1

    @staticmethod
    def _compile_values(stmt: Any) -> Dict[str, Any]:
        """Pull the SET-clause values out of a SQLAlchemy update.
        Each entry in `_values` is a BindParameter — pull its `.value`
        so tests can assert on the actual Python value."""
        out: Dict[str, Any] = {}
        for col, bind in stmt._values.items():
            name = getattr(col, "name", str(col))
            if hasattr(bind, "value"):
                out[name] = bind.value
            else:
                out[name] = bind
        return out


@pytest.fixture
def captured_db(monkeypatch):
    from db import executor_runs as er
    fake = _CapturingDatabase()
    monkeypatch.setattr(er, "database", fake)

    async def _noop_ensure():
        return None
    monkeypatch.setattr(er, "ensure_executor_runs_table", _noop_ensure)
    return fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_succeeded_sets_error_message_to_null(captured_db):
    """Whether or not the prior row had an error_message, the
    succeeded UPDATE includes `error_message = NULL` in the SET clause.
    The retry-recovery case is the regression target — pre-fix the
    error_message column survived the success transition."""
    from db.executor_runs import mark_executor_run_succeeded

    ok = await mark_executor_run_succeeded(
        run_id="run-1",
        worker_id="worker-A",
        evidence_jsonb={"briefs": [{"target_query": "x"}]},
    )
    assert ok is True
    assert len(captured_db.executed) == 1

    values = _CapturingDatabase._compile_values(captured_db.executed[0])
    # status transitions to succeeded
    assert values.get("status") == "succeeded"
    # AND error_message is cleared — the regression target
    assert "error_message" in values, (
        "mark_executor_run_succeeded must set error_message in SET clause "
        "(was missing → stale text would survive the success transition)"
    )
    assert values["error_message"] is None


@pytest.mark.asyncio
async def test_succeeded_clears_error_even_without_evidence(captured_db):
    """The error_message clear must fire regardless of whether
    evidence_jsonb is supplied. Pre-fix the absence of evidence
    didn't matter (error_message survived in both code paths) —
    confirm the new behavior is unconditional."""
    from db.executor_runs import mark_executor_run_succeeded

    ok = await mark_executor_run_succeeded(
        run_id="run-2",
        worker_id="worker-B",
        evidence_jsonb=None,
    )
    assert ok is True

    values = _CapturingDatabase._compile_values(captured_db.executed[0])
    assert values.get("status") == "succeeded"
    assert "error_message" in values
    assert values["error_message"] is None
    # evidence_jsonb was None → MUST NOT be in the SET clause (don't
    # blow away an existing evidence_jsonb when the caller didn't
    # supply a replacement).
    assert "evidence_jsonb" not in values
