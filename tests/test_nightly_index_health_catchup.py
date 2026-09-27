"""A worker deploy that cuts the nightly index-health run must not lose the night.

The worker is rolled on every merge. A shutdown cancels the run in flight
(services/scheduler_job_runner logs "ABANDONED as a zombie"), and APScheduler's
in-memory store on the new revision schedules the NEXT 04:00, so the slot that was
cut never re-runs: 2026-09-23 and 09-27 on prod, and on 09-27 63 rows stayed on a
cleared extractor_regression blocker until they were recomputed by hand.

What this pins:
  * the run records its slot in scheduled_job_completions only after every pass,
    so a run cancelled mid-batch leaves the slot unrecorded (end to end, through
    the same wrap_job path the scheduler uses);
  * the catch-up re-runs an unrecorded slot, and only one: it leaves a recorded
    slot alone, waits CATCHUP_MIN_DELAY after the slot fires, stands aside while a
    scheduled run is in flight in this process, does not count a lock-held skip,
    refuses to guess when the marker cannot be read, and caps real re-runs;
  * the marker never moves backwards.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import db.scheduled_job_completions as completions
import jobs.nightly_index_health_job as job
import services.scheduler_job_runner as runner
from db.database import database

SLOT = datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc)
YESTERDAY = SLOT - timedelta(days=1)


@pytest.fixture()
async def marker_db():
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await completions.ensure_scheduled_job_completions_table()
    await database.execute("DELETE FROM scheduled_job_completions")
    runner._reset_for_tests()
    job._CATCHUP_RUNS.clear()
    try:
        yield database
    finally:
        await database.execute("DELETE FROM scheduled_job_completions")
        runner._reset_for_tests()
        job._CATCHUP_RUNS.clear()
        if not was_connected and database.is_connected:
            await database.disconnect()


class FakeRun:
    """Stands in for run_nightly_index_health. Like the real one, a run that
    finishes records its slot (unless `records=False`)."""

    def __init__(self, *, skipped: bool = False, records: bool = True):
        self.calls = 0
        self.skipped = skipped
        self.records = records

    async def __call__(self):
        self.calls += 1
        summary = {"errors": [], "total_processed": 7, "eligible_count": 3}
        if self.skipped:
            summary["skipped"] = True
            return summary
        if self.records:
            await completions.record_completion(job.JOB_ID, SLOT)
        return summary


# ---------------------------------------------------------------------------
# slot arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2026, 9, 27, 3, 59, 59, tzinfo=timezone.utc), YESTERDAY),
        (datetime(2026, 9, 27, 4, 0, 0, tzinfo=timezone.utc), SLOT),
        (datetime(2026, 9, 27, 4, 4, 15, tzinfo=timezone.utc), SLOT),
        (datetime(2026, 9, 28, 3, 0, tzinfo=timezone.utc), SLOT),
        # a non-UTC clock is normalised, not read as local 04:00
        (datetime(2026, 9, 27, 5, 30, tzinfo=timezone(timedelta(hours=2))), YESTERDAY),
    ],
)
def test_latest_slot(now, expected):
    assert job.latest_slot(now) == expected


# ---------------------------------------------------------------------------
# the marker
# ---------------------------------------------------------------------------

async def test_marker_only_moves_forward(marker_db):
    assert await completions.last_completed_slot(job.JOB_ID) is None
    await completions.record_completion(job.JOB_ID, SLOT)
    await completions.record_completion(job.JOB_ID, YESTERDAY)  # a late, older writer
    assert await completions.last_completed_slot(job.JOB_ID) == SLOT
    tomorrow = SLOT + timedelta(days=1)
    await completions.record_completion(job.JOB_ID, tomorrow)
    assert await completions.last_completed_slot(job.JOB_ID) == tomorrow


# ---------------------------------------------------------------------------
# catch-up decisions
# ---------------------------------------------------------------------------

async def test_the_2026_09_27_case_a_slot_cut_by_a_deploy_is_re_run(marker_db, monkeypatch):
    await completions.record_completion(job.JOB_ID, YESTERDAY)
    fake = FakeRun()
    monkeypatch.setattr(job, "run_nightly_index_health", fake)

    out = await job.run_nightly_index_health_catchup(now=SLOT + timedelta(minutes=24))
    assert out["action"] == "ran" and fake.calls == 1
    assert await completions.last_completed_slot(job.JOB_ID) == SLOT

    # ...and exactly once: the next tick sees the slot recorded.
    out = await job.run_nightly_index_health_catchup(now=SLOT + timedelta(minutes=34))
    assert out["action"] == "up_to_date" and fake.calls == 1


async def test_a_slot_that_completed_is_left_alone(marker_db, monkeypatch):
    await completions.record_completion(job.JOB_ID, SLOT)
    fake = FakeRun()
    monkeypatch.setattr(job, "run_nightly_index_health", fake)
    for minutes in (10, 60, 23 * 60):
        out = await job.run_nightly_index_health_catchup(now=SLOT + timedelta(minutes=minutes))
        assert out["action"] == "up_to_date"
    assert fake.calls == 0


async def test_a_never_recorded_job_is_run_once(marker_db, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(job, "run_nightly_index_health", fake)
    out = await job.run_nightly_index_health_catchup(now=SLOT + timedelta(hours=9))
    assert out["action"] == "ran" and out["last_completed_slot"] is None
    out = await job.run_nightly_index_health_catchup(now=SLOT + timedelta(hours=9, minutes=10))
    assert out["action"] == "up_to_date" and fake.calls == 1


async def test_it_does_not_race_the_scheduled_run_at_the_top_of_the_slot(marker_db, monkeypatch):
    await completions.record_completion(job.JOB_ID, YESTERDAY)
    fake = FakeRun()
    monkeypatch.setattr(job, "run_nightly_index_health", fake)
    for now in (SLOT, SLOT + timedelta(minutes=5), SLOT + job.CATCHUP_MIN_DELAY - timedelta(seconds=1)):
        out = await job.run_nightly_index_health_catchup(now=now)
        assert out["action"] == "waiting_for_scheduled_run"
    assert fake.calls == 0
    out = await job.run_nightly_index_health_catchup(now=SLOT + job.CATCHUP_MIN_DELAY)
    assert out["action"] == "ran" and fake.calls == 1


async def test_it_stands_aside_while_the_scheduled_run_is_in_flight_here(marker_db, monkeypatch):
    await completions.record_completion(job.JOB_ID, YESTERDAY)
    fake = FakeRun()
    monkeypatch.setattr(job, "run_nightly_index_health", fake)

    release = asyncio.Event()

    async def slow_nightly():
        await release.wait()

    wrapped = runner.wrap_job(job.JOB_ID, slow_nightly, deadline_seconds=60)
    in_flight = asyncio.create_task(wrapped())
    await asyncio.sleep(0)
    try:
        out = await job.run_nightly_index_health_catchup(now=SLOT + timedelta(minutes=30))
        assert out["action"] == "scheduled_run_in_flight" and fake.calls == 0
    finally:
        release.set()
        await in_flight


async def test_a_lock_held_skip_is_not_counted_and_does_not_record(marker_db, monkeypatch):
    await completions.record_completion(job.JOB_ID, YESTERDAY)
    fake = FakeRun(skipped=True)
    monkeypatch.setattr(job, "run_nightly_index_health", fake)
    for i in range(job.CATCHUP_MAX_RUNS_PER_SLOT + 2):
        out = await job.run_nightly_index_health_catchup(now=SLOT + timedelta(minutes=20 + 10 * i))
        assert out["action"] == "skipped_lock_held"
    assert fake.calls == job.CATCHUP_MAX_RUNS_PER_SLOT + 2
    assert await completions.last_completed_slot(job.JOB_ID) == YESTERDAY


async def test_real_re_runs_are_capped_when_the_marker_never_lands(marker_db, monkeypatch):
    fake = FakeRun(records=False)
    monkeypatch.setattr(job, "run_nightly_index_health", fake)
    actions = [
        (await job.run_nightly_index_health_catchup(now=SLOT + timedelta(minutes=20 + 10 * i)))["action"]
        for i in range(job.CATCHUP_MAX_RUNS_PER_SLOT + 2)
    ]
    assert actions == ["ran"] * job.CATCHUP_MAX_RUNS_PER_SLOT + ["run_cap_reached"] * 2
    assert fake.calls == job.CATCHUP_MAX_RUNS_PER_SLOT
    # the cap is per slot: the next night's slot gets its own budget
    out = await job.run_nightly_index_health_catchup(now=SLOT + timedelta(days=1, minutes=20))
    assert out["action"] == "ran"


async def test_an_unreadable_marker_runs_nothing(marker_db, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(job, "run_nightly_index_health", fake)

    async def boom(job_id):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(completions, "last_completed_slot", boom)
    out = await job.run_nightly_index_health_catchup(now=SLOT + timedelta(minutes=30))
    assert out["action"] == "marker_unreadable" and fake.calls == 0


# ---------------------------------------------------------------------------
# end to end through the real job and the scheduler's wrapper
# ---------------------------------------------------------------------------

async def test_a_run_cut_mid_batch_by_a_shutdown_is_recovered_by_the_catch_up(marker_db, monkeypatch):
    """The real run_nightly_index_health, heavy SQL stubbed at its seams. The run
    is parked where the 09-27 run was cut (after scoring, before the batch has
    finished), its scheduler wrapper is cancelled the way a revision shutdown
    cancels it, and the catch-up then runs the slot to completion."""
    await completions.record_completion(job.JOB_ID, YESTERDAY)

    async def no_scorecards():
        return {}

    async def no_indexnow(before, summary):
        return None

    parked = asyncio.Event()
    park = True

    async def snapshot():
        if park:
            parked.set()
            await asyncio.Event().wait()  # never returns: mid-run
        return {}

    monkeypatch.setattr(job, "_compute_domain_extractor_scorecards", no_scorecards)
    monkeypatch.setattr(job, "_submit_indexnow_transitions", no_indexnow)
    monkeypatch.setattr(job, "_snapshot_serving_eligibility", snapshot)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return SLOT + timedelta(seconds=1)

    monkeypatch.setattr(job, "datetime", FrozenDatetime)

    scheduled = runner.wrap_job(job.JOB_ID, job.run_nightly_index_health, deadline_seconds=60)
    catchup = runner.wrap_job(
        "nightly_index_health_catchup", job.run_nightly_index_health_catchup, deadline_seconds=60,
    )

    tick = asyncio.create_task(scheduled())
    await asyncio.wait_for(parked.wait(), timeout=5)
    tick.cancel()  # the revision shuts down
    with pytest.raises(asyncio.CancelledError):
        await tick
    assert await completions.last_completed_slot(job.JOB_ID) == YESTERDAY, (
        "a run that never finished its passes recorded the slot as done"
    )

    park = False
    out = await catchup(now=SLOT + timedelta(minutes=24))
    assert out["action"] == "ran"
    assert await completions.last_completed_slot(job.JOB_ID) == SLOT
    out = await catchup(now=SLOT + timedelta(minutes=34))
    assert out["action"] == "up_to_date"


async def test_the_scheduler_registers_the_catch_up_on_an_interval(monkeypatch):
    import services.audit_scheduler as sched

    added = {}

    class Recording:
        def __init__(self, *a, **k):
            pass

        def add_job(self, func, *args, **kwargs):
            added[kwargs.get("id")] = (func, args, kwargs)

        def start(self):
            pass

        def get_jobs(self):
            return []

    for k in ("AUDIT_WORKER_ENABLED", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "web")
    monkeypatch.setattr(sched, "_SCHEDULER", None)
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", Recording)
    runner._reset_for_tests()

    await sched.start_scheduler()
    assert sched._BOOT_ERROR is None, sched._BOOT_ERROR
    func, args, kwargs = added["nightly_index_health_catchup"]
    assert args == ("interval",) and kwargs["minutes"] == 10 and kwargs["max_instances"] == 1
    assert func.__wrapped_job_id__ == "nightly_index_health_catchup"
    assert func.__wrapped__ is job.run_nightly_index_health_catchup
    assert "nightly_index_health" in added
    runner._reset_for_tests()
