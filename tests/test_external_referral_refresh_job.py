"""`jobs/external_referral_refresh.main()` must OPEN THE POOL before it queries.

The job is a CLI entrypoint: nothing runs the API's lifespan hook for it, so the
`databases.Database` singleton it shares with `services/external_referral_readiness` and
`routes/employee_products` is still unconnected when `asyncio.run` starts. Under Postgres the
first query then raises `AssertionError("DatabaseBackend is not running")` from the patched
`PostgresConnection.acquire` in `db/database.py` — the job could never have completed a run.

WHY THESE ASSERT ON THE CALL AND NOT ON A RUN SUCCEEDING. The sqlite backend has no such guard:
a sqlite-backed end-to-end test passes against the BROKEN code and proves nothing. That is
exactly why this defect survived — the batch itself is well covered
(`tests/test_external_referral_readiness.py`), just never through `main()` against a backend
that cares. So every row here pins the connect/disconnect calls themselves, on the SAME object
the query path resolves, with the backend never involved.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jobs.external_referral_refresh as job_module  # noqa: E402
import routes.employee_products as employee_products  # noqa: E402
import services.external_referral_readiness as readiness  # noqa: E402
from db.database import database as db_singleton  # noqa: E402


_SUMMARY: Dict[str, Any] = {"status": "success", "candidate_count": 0, "refreshed": 0}


def _instrument(monkeypatch, *, batch_raises: bool = False) -> List[str]:
    """Record the ORDER of connect / batch / disconnect on the real singleton.

    Patched onto `db.database.database` ITSELF, not onto `job_module.database`. Going through
    the job's own alias would make a job that never imports `database` at all fail with a bare
    AttributeError on the patch line — a green-to-red transition that says nothing about whether
    the connect HAPPENS. Patching the singleton means the mutant "imports it but never calls
    connect" is killed by the recorded order, which is the assertion that matters.
    """
    events: List[str] = []

    async def fake_connect() -> None:
        events.append("connect")

    async def fake_disconnect() -> None:
        events.append("disconnect")

    async def fake_batch(*, refresh_seed_by_id, limit) -> Dict[str, Any]:
        events.append("batch")
        if batch_raises:
            raise RuntimeError("boom")
        return dict(_SUMMARY)

    monkeypatch.setattr(db_singleton, "connect", fake_connect)
    monkeypatch.setattr(db_singleton, "disconnect", fake_disconnect)
    monkeypatch.setattr(job_module, "run_external_referral_refresh_batch", fake_batch)
    monkeypatch.setattr(sys, "argv", ["external_referral_refresh", "--limit", "3"])
    return events


def test_main_connects_the_pool_before_it_runs_the_batch(monkeypatch, capsys) -> None:
    """The whole defect in one row: without `await database.connect()` this reads
    `['batch']` and the first real query would have raised
    AssertionError("DatabaseBackend is not running")."""
    events = _instrument(monkeypatch)

    assert job_module.main() == 0

    assert events == ["connect", "batch", "disconnect"], (
        "expected the pool to be opened before the batch and closed after it, got "
        f"{events!r}"
    )
    # The summary still reaches stdout — that is what Cloud Logging captures for this job.
    assert '"status": "success"' in capsys.readouterr().out


def test_main_disconnects_even_when_the_batch_raises(monkeypatch) -> None:
    """`disconnect` belongs in a `finally`, matching `jobs/external_seed_destination_sweep.py`.
    A Cloud Run Job that leaks its pool on the error path holds Cloud SQL connections until the
    task times out, and this batch's whole job is to touch hosts that fail."""
    events = _instrument(monkeypatch, batch_raises=True)

    with pytest.raises(RuntimeError, match="boom"):
        job_module.main()

    assert events == ["connect", "batch", "disconnect"], (
        f"expected the pool to be closed on the failure path too, got {events!r}"
    )


def test_main_passes_the_parsed_limit_through(monkeypatch, capsys) -> None:
    """Guards the refactor rather than the bug: wrapping the body in an inner coroutine is
    where a CLI argument silently stops being forwarded."""
    seen: Dict[str, Any] = {}

    async def fake_connect() -> None:
        return None

    async def fake_batch(*, refresh_seed_by_id, limit) -> Dict[str, Any]:
        seen["limit"] = limit
        seen["refresh_seed_by_id"] = refresh_seed_by_id
        return dict(_SUMMARY)

    monkeypatch.setattr(db_singleton, "connect", fake_connect)
    monkeypatch.setattr(db_singleton, "disconnect", fake_connect)
    monkeypatch.setattr(job_module, "run_external_referral_refresh_batch", fake_batch)
    monkeypatch.setattr(sys, "argv", ["external_referral_refresh", "--limit", "7"])

    assert job_module.main() == 0
    assert seen["limit"] == 7
    capsys.readouterr()


def test_the_job_connects_the_same_singleton_the_query_path_uses() -> None:
    """Connecting SOME `Database` would satisfy a naive call-count assertion while leaving the
    pool the queries actually resolve unopened. `db.database.database` is a module-level
    singleton shared by all three modules, and the tests above only mean anything because of
    that; assert it rather than assume it."""
    assert job_module.database is db_singleton
    assert readiness.database is db_singleton
    assert employee_products.database is db_singleton
