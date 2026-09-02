"""platform_import_tasks: the claim must be atomic, and `running` must be recoverable.

Context. `jobs/catalog_import_worker.process_next_import_task` — the drainer that
claims "the oldest pending/retry_scheduled task" — had ZERO callers anywhere in
the repo. The only production runner was the request-scoped BackgroundTask at
routes/merchant_api_extensions.py:1564/1591, which is never retried and dies with
the process on a Cloud Run revision swap, stranding the row `pending` forever.

Registering a scheduler drain tick fixes the stranding but makes a second,
latent bug reachable: the claim was `get_next_scheduled_task()` (a plain SELECT)
followed by `mark_import_task_running()` (an unconditional UPDATE). Two runners
reading the same row both proceeded — a full duplicate Shopify catalog import.

These tests run against the REAL platform_import_tasks table built from the
`db/` model (tests/model_schema.py), not a stubbed claim: the whole property
under test is what the DATABASE does when two conditional UPDATEs race, which a
stub cannot exhibit.

Mutation-checked — each of these reverts turns a test below RED:
  * claim_import_task: drop `AND status IN ('pending','retry_scheduled')`
    -> test_only_one_of_two_concurrent_claims_by_id_wins
  * claim_import_task: drop `attempt = attempt + 1`
    -> test_claim_increments_attempt_exactly_once
  * claim_next_import_task: use get_next_scheduled_task + unconditional UPDATE
    -> test_only_one_of_two_concurrent_next_claims_wins
  * process_import_task_by_id: process on a failed claim instead of reporting
    task_not_ready -> test_concurrent_by_id_processing_runs_the_import_once
  * claim_import_task: return dict(row) instead of _normalize_claimed_row
    -> test_a_claimed_row_carries_counts_as_a_dict_not_a_json_string
  * requeue_stale_import_tasks: drop `AND status = 'running'`
    -> test_requeue_skips_a_task_that_finished_between_select_and_update
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from db.database import database
from db.platform_import_tasks import (
    claim_import_task,
    claim_next_import_task,
    get_import_task,
    platform_import_tasks,
    requeue_stale_import_tasks,
)
from tests.model_schema import ensure_model_tables


@pytest.fixture(autouse=True)
async def _schema():
    if not database.is_connected:
        await database.connect()
    await ensure_model_tables((platform_import_tasks,))
    # Every test owns the whole queue: the claim helpers are global (they take
    # no merchant filter), so a row left by a neighbour would be claimable here.
    await database.execute(platform_import_tasks.delete())
    yield
    await database.execute(platform_import_tasks.delete())


async def _insert(
    *,
    merchant_id: str = "m_claim",
    status: str = "pending",
    attempt: int = 0,
    next_run_at=None,
    updated_at=None,
    created_at=None,
    source_type: str = "connector",
    connector=  "shopify",
) -> int:
    values = {
        "merchant_id": merchant_id,
        "source_type": source_type,
        "connector": connector,
        "status": status,
        "attempt": attempt,
        "next_run_at": next_run_at,
    }
    if updated_at is not None:
        values["updated_at"] = updated_at
    if created_at is not None:
        values["created_at"] = created_at
    return int(await database.execute(platform_import_tasks.insert().values(**values)))


# ---------------------------------------------------------------------------
# 1. the claim is atomic
# ---------------------------------------------------------------------------

async def test_only_one_of_two_concurrent_claims_by_id_wins():
    """The exact double-run shape: the endpoint's BackgroundTask and the new
    drain tick reaching for the same task_id at the same moment."""
    task_id = await _insert()

    winners = await asyncio.gather(claim_import_task(task_id), claim_import_task(task_id))

    claimed = [w for w in winners if w is not None]
    assert len(claimed) == 1, f"both runners claimed the same task: {winners}"
    assert (claimed[0].get("status") or "").lower() == "running"


async def test_only_one_of_two_concurrent_next_claims_wins():
    """Two drain ticks (or a tick and a manual worker) racing for the queue
    head. Only one may come away with the row."""
    await _insert()

    winners = await asyncio.gather(claim_next_import_task(), claim_next_import_task())

    claimed = [w for w in winners if w is not None]
    assert len(claimed) == 1, f"both runners claimed the queue head: {winners}"


async def test_claim_increments_attempt_exactly_once():
    """`_process_import_task_record` no longer does `attempt + 1` itself — the
    claim owns it. If the claim stops incrementing, retry backoff (which is
    computed from `attempt`) silently flattens to a constant."""
    task_id = await _insert(attempt=2)

    claimed = await claim_import_task(task_id)

    assert claimed is not None
    assert int(claimed["attempt"]) == 3
    stored = await get_import_task(task_id)
    assert int(stored["attempt"]) == 3


@pytest.mark.parametrize("status", ["running", "succeeded", "failed"])
async def test_claim_refuses_a_task_that_is_not_claimable(status):
    task_id = await _insert(status=status)

    assert await claim_import_task(task_id) is None
    stored = await get_import_task(task_id)
    assert (stored["status"] or "").lower() == status
    assert int(stored["attempt"]) == 0, "a refused claim must not touch the row"


async def test_claim_next_skips_a_retry_whose_backoff_has_not_elapsed():
    """`retry_scheduled` rows carry exponential backoff in `next_run_at`. A
    drain tick that ignored it would hammer a failing Shopify store every 30s."""
    await _insert(status="retry_scheduled", next_run_at=datetime.utcnow() + timedelta(hours=1))

    assert await claim_next_import_task() is None


async def test_claim_next_takes_a_retry_whose_backoff_has_elapsed():
    """Positive counterpart to the test above: the skip must be the backoff,
    not the claim refusing `retry_scheduled` rows outright."""
    task_id = await _insert(
        status="retry_scheduled", next_run_at=datetime.utcnow() - timedelta(minutes=1)
    )

    claimed = await claim_next_import_task()

    assert claimed is not None and int(claimed["id"]) == task_id


async def test_claim_next_returns_none_on_an_empty_queue():
    assert await claim_next_import_task() is None


# ---------------------------------------------------------------------------
# 2. the worker entry points ride the claim
# ---------------------------------------------------------------------------

async def test_concurrent_by_id_processing_runs_the_import_once(monkeypatch):
    """The delivering assertion: with both runners live, the actual IMPORT body
    executes once. Asserting only on the claim would leave open the possibility
    that `process_import_task_by_id` ignores a failed claim and processes
    anyway."""
    import jobs.catalog_import_worker as worker

    ran = []

    async def _record(task):
        ran.append(int(task["id"]))
        return {"processed": True, "task_id": int(task["id"]), "status": "succeeded"}

    monkeypatch.setattr(worker, "_process_import_task_record", _record)

    task_id = await _insert()
    results = await asyncio.gather(
        worker.process_import_task_by_id(task_id),
        worker.process_import_task_by_id(task_id),
    )

    assert ran == [task_id], f"the import body ran {len(ran)} times: {ran}"
    losers = [r for r in results if not r.get("processed")]
    assert len(losers) == 1
    assert losers[0]["reason"] == "task_not_ready"


async def test_process_by_id_reports_task_not_found_for_a_missing_row():
    import jobs.catalog_import_worker as worker

    result = await worker.process_import_task_by_id(987654321)

    assert result == {
        "processed": False,
        "reason": "task_not_found",
        "task_id": 987654321,
    }


async def test_drain_tick_processes_a_pending_task(monkeypatch):
    """The registered job actually reaches the import body — the point of the
    whole change. A green claim test would still pass if the tick were wired to
    the wrong function."""
    import jobs.catalog_import_worker as worker

    ran = []

    async def _record(task):
        ran.append(int(task["id"]))
        return {"processed": True, "task_id": int(task["id"])}

    monkeypatch.setattr(worker, "_process_import_task_record", _record)
    monkeypatch.setenv("CATALOG_IMPORT_DRAIN_ENABLED", "true")

    task_id = await _insert()
    result = await worker.run_catalog_import_drain_tick()

    assert ran == [task_id]
    assert result["processed"] is True


async def test_the_drain_is_on_by_default(monkeypatch):
    """The default IS the fix. Rows strand `pending` on every revision swap
    until something drains them, and an env-var-armed fix can be silently
    disarmed by the next deploy (a 2026-08-30 deploy wiped five). So the default
    lives in code, and it is ON. The kill switch is tested separately below.

    Inverts the earlier test that pinned dormant-by-default: dormant was right
    while the credential resolver could hand a disconnected merchant the
    platform's store (#1964), and wrong once #1989 closed that.
    """
    import jobs.catalog_import_worker as worker

    ran = []

    async def _record(task):
        ran.append(int(task["id"]))
        return {"processed": True, "task_id": int(task["id"])}

    monkeypatch.setattr(worker, "_process_import_task_record", _record)
    monkeypatch.delenv("CATALOG_IMPORT_DRAIN_ENABLED", raising=False)

    task_id = await _insert()
    result = await worker.run_catalog_import_drain_tick()

    assert ran == [task_id], "the drain must run with NO env var set"
    assert result["processed"] is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "False", " off "])
async def test_the_kill_switch_stops_both_ticks(monkeypatch, value):
    """The env var is a kill switch, so every disabling spelling must work —
    an operator reaching for it mid-incident should not have to guess."""
    import jobs.catalog_import_worker as worker

    async def _record(task):  # pragma: no cover - must not run
        raise AssertionError(f"the drain tick ran with CATALOG_IMPORT_DRAIN_ENABLED={value!r}")

    monkeypatch.setattr(worker, "_process_import_task_record", _record)
    monkeypatch.setenv("CATALOG_IMPORT_DRAIN_ENABLED", value)

    await _insert()
    await _insert(status="running", updated_at=datetime.utcnow() - timedelta(hours=1))

    assert await worker.run_catalog_import_drain_tick() == {
        "processed": False,
        "reason": "disabled",
    }
    assert await worker.run_catalog_import_stale_reaper_tick() == {
        "requeued": 0,
        "reason": "disabled",
    }


@pytest.mark.parametrize(
    "source_type,connector",
    [
        ("amazon_orders", "amazon_sp_api"),
        ("orders_report", "amazon_orders"),
        ("report", "temu_report"),
        ("unknown", None),
    ],
)
async def test_the_drain_lane_ignores_non_shopify_rows(source_type, connector):
    """`platform_import_tasks` is shared by four other producers. The
    amazon/report branches write platform_orders, have no runtime cap and no
    heartbeat — the one shape this reaper could requeue mid-run. They are out of
    the lane until each is understood on its own terms."""
    await _insert(source_type=source_type, connector=connector)

    assert await claim_next_import_task() is None


async def test_the_reaper_ignores_non_shopify_rows():
    """Requeueing a row the drain tick will never claim is pure churn."""
    task_id = await _insert(
        source_type="amazon_orders",
        connector="amazon_sp_api",
        status="running",
        updated_at=datetime.utcnow() - timedelta(hours=1),
    )

    assert await requeue_stale_import_tasks(stale_after_seconds=900) == 0

    stored = await get_import_task(task_id)
    assert (stored["status"] or "").lower() == "running"


async def test_the_reaper_stops_requeueing_a_poison_pill():
    """A task that KILLS its process never reaches the worker's attempt cutoffs
    — they all sit in `except` handlers, and an OOM raises none. Without a bound
    here, a row that reliably OOMs the instance is requeued, claimed, and OOMs
    again every five minutes forever."""
    task_id = await _insert(
        status="running", attempt=5, updated_at=datetime.utcnow() - timedelta(hours=1)
    )

    assert await requeue_stale_import_tasks(stale_after_seconds=900, max_attempt=5) == 0

    stored = await get_import_task(task_id)
    assert (stored["status"] or "").lower() == "running", (
        "a poison pill must be left for an operator, not cycled forever"
    )
    # and a task still under the bound IS recovered, so the bound is not a
    # blanket off-switch
    fresh = await _insert(
        status="running", attempt=1, updated_at=datetime.utcnow() - timedelta(hours=1)
    )
    assert await requeue_stale_import_tasks(stale_after_seconds=900, max_attempt=5) == 1
    assert (await get_import_task(fresh))["status"] == "retry_scheduled"


async def test_drain_tick_is_a_no_op_when_the_kill_switch_is_off(monkeypatch):
    import jobs.catalog_import_worker as worker

    async def _record(task):  # pragma: no cover - must not run
        raise AssertionError("drain tick ran with CATALOG_IMPORT_DRAIN_ENABLED=false")

    monkeypatch.setattr(worker, "_process_import_task_record", _record)
    monkeypatch.setenv("CATALOG_IMPORT_DRAIN_ENABLED", "false")

    await _insert()
    result = await worker.run_catalog_import_drain_tick()

    assert result == {"processed": False, "reason": "disabled"}


# ---------------------------------------------------------------------------
# 3. the claimed row keeps the shape the Core select produced
# ---------------------------------------------------------------------------

async def test_a_claimed_row_carries_counts_as_a_dict_not_a_json_string():
    """`RETURNING *` runs through NO SQLAlchemy result processor.

    On databases 0.7.0 a raw statement is wrapped in text(), whose compiled
    _result_columns is empty, so the backend short-circuits and the JSON
    `counts` column comes back as a STR — on both backends. The Core select
    this claim replaced returned a dict.

    _process_import_task_record reads the resume cursor out of `counts` behind
    an `isinstance(..., dict)` test, so an unconverted str silently discards
    pagination progress. Asserting the TYPE alone would be weak; this asserts
    the cursor a real resumed run depends on actually survives the claim.
    """
    task_id = await _insert(status="retry_scheduled", attempt=1)
    await database.execute(
        platform_import_tasks.update()
        .where(platform_import_tasks.c.id == task_id)
        .values(counts={"total": 5000, "shopify_next_page_info": "CURSOR_ABC"})
    )

    claimed = await claim_import_task(task_id)

    assert claimed is not None
    assert isinstance(claimed["counts"], dict), (
        f"counts came back as {type(claimed['counts']).__name__}; "
        "resume progress would be discarded"
    )
    assert claimed["counts"]["shopify_next_page_info"] == "CURSOR_ABC"


async def test_a_claimed_row_with_unusable_counts_degrades_to_an_empty_dict():
    """A row whose counts is not a JSON object must not raise inside the claim
    — the queue head would be unclaimable and every merchant behind it starved.
    `{}` is what the isinstance test downstream would have produced anyway."""
    task_id = await _insert()
    await database.execute(
        platform_import_tasks.update()
        .where(platform_import_tasks.c.id == task_id)
        .values(counts="not json at all")
    )

    claimed = await claim_import_task(task_id)

    assert claimed is not None and claimed["counts"] == {}


# ---------------------------------------------------------------------------
# 4. stale `running` recovery
# ---------------------------------------------------------------------------

async def test_requeue_returns_an_abandoned_running_task_to_the_queue():
    """A revision swap mid-import leaves `running`, which
    get_next_scheduled_task never reconsiders — the row is invisible to every
    runner forever."""
    task_id = await _insert(
        status="running", updated_at=datetime.utcnow() - timedelta(hours=1)
    )

    assert await requeue_stale_import_tasks(stale_after_seconds=900) == 1

    stored = await get_import_task(task_id)
    assert (stored["status"] or "").lower() == "retry_scheduled"
    assert stored["error"] == "stale_running_recovered"
    # and it is genuinely reachable again, not just relabelled
    claimed = await claim_next_import_task()
    assert claimed is not None and int(claimed["id"]) == task_id


async def test_requeue_leaves_a_still_working_import_alone():
    """A healthy import must never be requeued — that produces the exact
    double-run the claim exists to prevent.

    What keeps it safe is the per-page heartbeat in _process_import_task_record,
    which refreshes `updated_at`, so the window is measured from the last page
    rather than from the claim. The fresh `updated_at` below is what a
    heartbeating run looks like."""
    task_id = await _insert(status="running", updated_at=datetime.utcnow())

    assert await requeue_stale_import_tasks(stale_after_seconds=900) == 0

    stored = await get_import_task(task_id)
    assert (stored["status"] or "").lower() == "running"


async def test_requeue_never_sweeps_a_task_that_is_not_running():
    """Cheap outer guard: the candidate SELECT is status-scoped."""
    task_id = await _insert(
        status="succeeded", updated_at=datetime.utcnow() - timedelta(hours=1)
    )

    assert await requeue_stale_import_tasks(stale_after_seconds=900) == 0

    stored = await get_import_task(task_id)
    assert (stored["status"] or "").lower() == "succeeded"


async def test_requeue_skips_a_task_that_finished_between_select_and_update(monkeypatch):
    """The per-row UPDATE re-asserts `status = 'running'`, and THAT is what this
    test drives — the candidate SELECT cannot cover it, because the row is still
    `running` when the sweep picks it up.

    Forcing the interleaving is the whole point: an import that completes while
    the sweep holds its candidate list would otherwise be dragged back onto the
    queue and imported a second time. Asserting on a row that was already
    `succeeded` before the sweep started (as an earlier version of this test did)
    is vacuous — the SELECT filters it out and the guard is never reached.
    """
    import db.platform_import_tasks as mod

    task_id = await _insert(
        status="running", updated_at=datetime.utcnow() - timedelta(hours=1)
    )

    real_fetch_all = mod.database.fetch_all

    async def racing_fetch_all(*args, **kwargs):
        rows = await real_fetch_all(*args, **kwargs)
        # The real import finishes right here, after the sweep has chosen its
        # candidates but before it writes.
        await mod.database.execute(
            mod.platform_import_tasks.update()
            .where(mod.platform_import_tasks.c.id == task_id)
            .values(status="succeeded")
        )
        return rows

    monkeypatch.setattr(mod.database, "fetch_all", racing_fetch_all)

    assert await mod.requeue_stale_import_tasks(stale_after_seconds=900) == 0

    monkeypatch.undo()
    stored = await get_import_task(task_id)
    assert (stored["status"] or "").lower() == "succeeded"
    assert stored["error"] != "stale_running_recovered"


async def test_stale_reaper_tick_requeues_and_reports(monkeypatch):
    import jobs.catalog_import_worker as worker

    monkeypatch.setenv("CATALOG_IMPORT_DRAIN_ENABLED", "true")
    monkeypatch.setenv("CATALOG_IMPORT_STALE_AFTER_SECONDS", "60")
    await _insert(status="running", updated_at=datetime.utcnow() - timedelta(hours=1))

    assert await worker.run_catalog_import_stale_reaper_tick() == {"requeued": 1}
