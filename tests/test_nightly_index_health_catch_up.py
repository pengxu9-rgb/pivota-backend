"""A nightly_index_health run killed by a redeploy is completed the same day.

THE INCIDENT (2026-09-27). The 04:00 UTC run started on worker-00247-fdw; a deploy replaced
the worker at 04:03:42; `stop_scheduler` drained for 3s, then APScheduler cancelled the
wrapper, `run_isolated` cancelled the run and adopted it as a zombie. The IPS reclassify had
reached ~ck_e4..., the orphan delete / stale invalidation / regression pass never ran, and the
new instance's in-memory cron was already past 04:00, so nothing re-ran it until the next day.
Same on 2026-09-23 (killed 53s in).

These tests kill the run the way production does: a REAL AsyncIOScheduler fires the job
through the REAL `wrap_job` wrapper, and the REAL `audit_scheduler.stop_scheduler` shuts it
down while the run is parked mid-batch. No flag is set by hand; the only thing that tells
catch-up the run did not finish is what the killed run left in the slot ledger.

The job's own SQL (batch fetch, upserts, orphan/stale/regression passes) is answered by a
scripted fake, because it needs the whole catalog schema; the slot ledger, its DDL and the
`connection()` pin go to a REAL SQLite database. The Postgres-only half (the advisory lock
really held for the run, and really released by the kill) is in
tests/test_nightly_index_health_catch_up_postgres.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import jobs.nightly_index_health_job as job
from db import scheduler_job_slots as slots
from test_nightly_index_health_job import _full_row

CRON_FIRES_AT = datetime(2026, 9, 27, 4, 0, 1, tzinfo=timezone.utc)
KEYS = ["ck_a", "ck_b", "ck_c", "ck_d", "ck_e"]


class _HybridDb:
    """The nightly job's own SQL is scripted; everything else — the slot ledger, its DDL,
    `connection()` — goes to a REAL `databases.Database`.

    Batches are pages of `batch_size` content keys. When `park_at_cursor` is set, the batch
    fetch for that cursor parks until `release` is set: that is where the redeploy lands."""

    def __init__(self, real, *, batch_size: int = 2, park_at_cursor=None, park_on_execute=None,
                 fail_batch_at_cursor=None):
        self.real = real
        self.batch_size = batch_size
        self.park_at_cursor = park_at_cursor
        self.park_on_execute = park_on_execute
        self.fail_batch_at_cursor = fail_batch_at_cursor
        self.parked = asyncio.Event()
        self.release = asyncio.Event()
        self.upserted: list = []
        self.executed: list = []
        self.batch_fetches = 0

    def __getattr__(self, name):  # url, _connection_context, ... -> the real database
        return getattr(self.real, name)

    def connection(self):
        return self.real.connection()

    @staticmethod
    def _job_sql(query: str) -> bool:
        return query in {
            job._BATCH_QUERY, job._ORPHAN_COUNT_QUERY, job._IPS_TOTAL_COUNT_QUERY,
            job._ORPHAN_DELETE, job._STALE_INVALIDATION_UPDATE,
            job._REGRESSION_BLOCKER_UPDATE, job._UPSERT_QUERY,
        } or "domain_extractor_baselines" in query

    async def fetch_all(self, query, values=None):
        if query == job._BATCH_QUERY:
            self.batch_fetches += 1
            cursor = (values or {}).get("cursor")
            if cursor == self.park_at_cursor and not self.release.is_set():
                self.parked.set()
                await self.release.wait()
            if cursor == self.fail_batch_at_cursor:
                self.fail_batch_at_cursor = None  # a transient failure: once
                raise TimeoutError("canceling statement due to statement timeout")
            after = [k for k in KEYS if k > cursor][: self.batch_size]
            return [_full_row(content_key=k) for k in after]
        if self._job_sql(query):
            return []
        return await self.real.fetch_all(query, values)

    async def fetch_one(self, query, values=None):
        if query == job._ORPHAN_COUNT_QUERY:
            return {"n": 1}  # the ck_088ae30b... orphan 09-27 left behind
        if query == job._IPS_TOTAL_COUNT_QUERY:
            return {"n": 100}
        if self._job_sql(query):
            return {"n": 0}
        return await self.real.fetch_one(query, values)

    async def execute(self, query, values=None):
        if self._job_sql(query):
            if query == self.park_on_execute and not self.release.is_set():
                self.parked.set()
                await self.release.wait()
            self.executed.append(query)
            return None
        return await self.real.execute(query, values)

    async def execute_many(self, query, values):
        assert query == job._UPSERT_QUERY, query[:80]
        self.upserted.extend(v["content_key"] for v in values)


async def _no_scorecards():
    return {"domains_scored": 0, "domains_regressed": 0, "domains_recovered": 0, "errors": []}


async def _no_snapshot():
    return {}


async def _no_indexnow(before, summary):
    return None


@pytest.fixture()
async def real_db(tmp_path):
    from databases import Database

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'slots.db'}")
    await db.connect()
    try:
        yield db
    finally:
        await db.disconnect()


@pytest.fixture()
def runner():
    import services.scheduler_job_runner as mod

    mod._reset_for_tests()
    yield mod
    mod._reset_for_tests()


@pytest.fixture()
def clock(monkeypatch):
    now = {"t": CRON_FIRES_AT}
    monkeypatch.setattr(job, "_utcnow", lambda: now["t"])
    return now


def _install(monkeypatch, hybrid):
    import db._ddl_guard as ddl_guard
    import db.database

    monkeypatch.setattr(db.database, "database", hybrid)
    monkeypatch.setattr(ddl_guard, "_state", {})
    monkeypatch.setattr(slots, "_DDL_READY", False)
    monkeypatch.setattr(job, "_EXHAUSTED_SLOTS_LOGGED", set())
    monkeypatch.setattr(job, "BATCH_SIZE", hybrid.batch_size)
    monkeypatch.setattr(job, "_compute_domain_extractor_scorecards", _no_scorecards)
    monkeypatch.setattr(job, "_snapshot_serving_eligibility", _no_snapshot)
    monkeypatch.setattr(job, "_submit_indexnow_transitions", _no_indexnow)


async def _run_now(runner, job_id):
    """run_job_now, bounded: a regression that lets the run park forever must FAIL here,
    not hang the suite."""
    return await asyncio.wait_for(runner.run_job_now(job_id), timeout=15)


async def _ledger(real_db, slot="2026-09-27"):
    row = await real_db.fetch_one(
        "SELECT attempts, completed_at FROM scheduler_job_slots "
        "WHERE job_id = 'nightly_index_health' AND slot_date = :s", {"s": slot},
    )
    return dict(row) if row is not None else None


async def _kill_mid_batch_by_redeploy(monkeypatch, runner, hybrid):
    """Fire the 04:00 job on a real AsyncIOScheduler through the real wrapper, wait until it
    is parked mid-batch, then shut the scheduler down exactly as a redeploy does."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    import services.audit_scheduler as audit_scheduler

    sched = AsyncIOScheduler(timezone="UTC")
    wrapped = runner.wrap_job(
        "nightly_index_health", job.run_nightly_index_health,
        deadline_seconds=audit_scheduler.run_deadline_for("nightly_index_health"),
    )
    sched.add_job(wrapped, "date", run_date=datetime.now(timezone.utc), id="nightly_index_health")
    sched.start()
    await asyncio.wait_for(hybrid.parked.wait(), timeout=10)

    monkeypatch.setattr(audit_scheduler, "_SCHEDULER", sched)
    monkeypatch.setattr(audit_scheduler, "_DRAIN_SECONDS", 0.2)
    await audit_scheduler.stop_scheduler()
    # APScheduler only cancels the wrapper's future; the wrapper sees it on a later loop
    # turn, cancels the run and adopts it as a zombie, and the run then unwinds through its
    # own `finally`. Wait for all three.
    st = runner._REGISTRY["nightly_index_health"]
    for _ in range(250):
        if st.runs_cancelled and all(t.done() for t in st.zombies):
            break
        await asyncio.sleep(0.02)
    assert st.runs_cancelled == 1, "the redeploy never cancelled the run"


# ── the incident, end to end ─────────────────────────────────────────────────────────


async def test_a_run_killed_by_a_redeploy_is_completed_by_the_next_catch_up_tick(
    monkeypatch, runner, real_db, clock,
):
    hybrid = _HybridDb(real_db, park_at_cursor="ck_b")
    _install(monkeypatch, hybrid)

    await _kill_mid_batch_by_redeploy(monkeypatch, runner, hybrid)

    # The kill went down the production path: the wrapper was cancelled, not a deadline.
    snap = runner.registry_snapshot()["nightly_index_health"]
    assert snap["runs_cancelled"] == 1 and snap["last_outcome"] == "cancelled", snap
    # ...half-done, exactly like 09-27: first batch written, the passes after it never ran.
    assert hybrid.upserted == ["ck_a", "ck_b"]
    assert job._ORPHAN_DELETE not in hybrid.executed
    assert job._STALE_INVALIDATION_UPDATE not in hybrid.executed
    # What the killed run left behind is the only signal catch-up gets.
    assert (await _ledger(real_db)) == {"attempts": 1, "completed_at": None}

    # The replacement instance: its catch-up tick, ~90s after boot, through the same wrapper.
    hybrid.release.set()
    clock["t"] = CRON_FIRES_AT + timedelta(minutes=5)
    runner.wrap_job(
        "nightly_index_health_catch_up", job.run_nightly_index_health_catch_up,
        deadline_seconds=7200,
    )
    out = await _run_now(runner, "nightly_index_health_catch_up")

    assert out["outcome"] == "ok", out
    assert out["result"]["catch_up"] is True and not out["result"].get("skipped"), out["result"]
    assert out["result"]["slot_date"] == "2026-09-27"
    assert out["result"]["slot_completed"] is True
    # The whole job re-ran, from the first key, including every pass after the batch.
    assert hybrid.upserted[2:] == KEYS
    assert job._ORPHAN_DELETE in hybrid.executed and out["result"]["orphans_deleted"] == 1
    assert job._STALE_INVALIDATION_UPDATE in hybrid.executed
    ledger = await _ledger(real_db)
    assert ledger["attempts"] == 2 and ledger["completed_at"] is not None, ledger

    # Every later tick that day is a no-op: it reads the ledger and stops.
    upserts_before = list(hybrid.upserted)
    clock["t"] = CRON_FIRES_AT + timedelta(hours=9)
    again = await _run_now(runner, "nightly_index_health_catch_up")
    assert again["result"]["skip_reason"] == "already_completed", again
    assert hybrid.upserted == upserts_before
    assert (await _ledger(real_db))["attempts"] == 2


async def test_the_killed_run_is_not_recorded_as_complete_even_after_it_unwinds(
    monkeypatch, runner, real_db, clock,
):
    """The completion write must be the last step, not a `finally`: a run whose unwind
    reaches the end of the function must still look unfinished."""
    hybrid = _HybridDb(real_db, park_at_cursor="ck_b")
    _install(monkeypatch, hybrid)
    await _kill_mid_batch_by_redeploy(monkeypatch, runner, hybrid)
    hybrid.release.set()
    await asyncio.sleep(0.1)
    assert (await _ledger(real_db))["completed_at"] is None


async def test_a_run_killed_during_the_passes_after_the_batch_is_also_caught_up(
    monkeypatch, runner, real_db, clock,
):
    """Every batch done, killed in the orphan delete: the passes after the batch are part of
    the run, so the slot must still read as unfinished (the 09-27 run lost exactly these)."""
    hybrid = _HybridDb(real_db, park_on_execute=job._ORPHAN_DELETE)
    _install(monkeypatch, hybrid)
    await _kill_mid_batch_by_redeploy(monkeypatch, runner, hybrid)

    assert hybrid.upserted == KEYS  # the whole batch landed...
    assert job._STALE_INVALIDATION_UPDATE not in hybrid.executed  # ...the passes did not
    assert (await _ledger(real_db)) == {"attempts": 1, "completed_at": None}

    hybrid.release.set()
    clock["t"] = CRON_FIRES_AT + timedelta(minutes=12)
    runner.wrap_job("nightly_index_health_catch_up", job.run_nightly_index_health_catch_up,
                    deadline_seconds=7200)
    out = await _run_now(runner, "nightly_index_health_catch_up")
    assert out["result"]["slot_completed"] is True, out
    assert job._STALE_INVALIDATION_UPDATE in hybrid.executed
    assert (await _ledger(real_db))["attempts"] == 2


async def test_a_run_cut_short_by_a_batch_error_is_not_recorded_as_done(monkeypatch, runner, real_db, clock):
    """A statement timeout on one page ends the reclassify loop early; every later key is
    left unclassified, which is the same damage as a kill."""
    hybrid = _HybridDb(real_db, fail_batch_at_cursor="ck_b")
    _install(monkeypatch, hybrid)

    first = await job.run_nightly_index_health()
    assert first["batch_errors"] == 1 and not first.get("slot_completed")
    assert hybrid.upserted == ["ck_a", "ck_b"]
    assert (await _ledger(real_db))["completed_at"] is None

    clock["t"] = CRON_FIRES_AT + timedelta(minutes=10)
    out = await job.run_nightly_index_health_catch_up()
    assert out["slot_completed"] is True and hybrid.upserted[2:] == KEYS


async def test_the_lock_fails_closed(monkeypatch, runner, real_db, clock):
    """A lock query that errors must skip the run, not run it unlocked: catch-up retries
    within 10 minutes, so failing open would only buy a chance of two runs at once."""

    class _LockErrors(_HybridDb):
        url = "postgresql://prod.invalid/pivota"  # take the real lock branch

        async def fetch_one(self, query, values=None):
            if "pg_try_advisory_lock" in query:
                raise ConnectionError("lock query failed")
            return await super().fetch_one(query, values)

    hybrid = _LockErrors(real_db)
    _install(monkeypatch, hybrid)
    for entry in (job.run_nightly_index_health, job.run_nightly_index_health_catch_up):
        out = await entry()
        assert out["skip_reason"] == "lock_held", out
    assert hybrid.batch_fetches == 0 and hybrid.upserted == []


# ── the decision catch-up makes ──────────────────────────────────────────────────────


async def test_catch_up_is_a_no_op_after_a_04_00_run_that_finished(monkeypatch, runner, real_db, clock):
    hybrid = _HybridDb(real_db)
    _install(monkeypatch, hybrid)

    first = await job.run_nightly_index_health()
    assert first["slot_completed"] is True and hybrid.upserted == KEYS

    clock["t"] = CRON_FIRES_AT + timedelta(minutes=10)
    out = await job.run_nightly_index_health_catch_up()
    assert out["skip_reason"] == "already_completed"
    assert hybrid.upserted == KEYS  # nothing re-ran
    assert (await _ledger(real_db))["attempts"] == 1


async def test_catch_up_runs_a_slot_no_instance_ever_started(monkeypatch, runner, real_db, clock):
    """No worker was up at 04:00 (or it lost the lock to one that was then killed): the
    ledger has no row for the slot at all."""
    hybrid = _HybridDb(real_db)
    _install(monkeypatch, hybrid)
    clock["t"] = CRON_FIRES_AT + timedelta(minutes=3)

    out = await job.run_nightly_index_health_catch_up()
    assert out.get("skipped") is None and out["slot_completed"] is True
    assert hybrid.upserted == KEYS
    assert (await _ledger(real_db))["attempts"] == 1


async def test_before_04_00_catch_up_looks_at_yesterdays_slot(monkeypatch, runner, real_db, clock):
    hybrid = _HybridDb(real_db)
    _install(monkeypatch, hybrid)
    await job.run_nightly_index_health()  # 2026-09-27 04:00 run, completed

    clock["t"] = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
    out = await job.run_nightly_index_health_catch_up()
    assert out["slot_date"] == "2026-09-27" and out["skip_reason"] == "already_completed"

    clock["t"] = datetime(2026, 9, 28, 4, 10, tzinfo=timezone.utc)  # 09-28's 04:00 run was lost
    out = await job.run_nightly_index_health_catch_up()
    assert out["slot_date"] == "2026-09-28" and out["slot_completed"] is True


async def test_catch_up_stops_after_the_attempt_cap(monkeypatch, runner, real_db, clock, caplog):
    hybrid = _HybridDb(real_db)
    _install(monkeypatch, hybrid)
    assert await slots.ensure_scheduler_job_slots_table()
    for _ in range(job.CATCH_UP_MAX_ATTEMPTS):
        await slots.record_attempt("nightly_index_health", "2026-09-27", CRON_FIRES_AT)

    clock["t"] = CRON_FIRES_AT + timedelta(hours=2)
    out = await job.run_nightly_index_health_catch_up()
    assert out["skip_reason"] == "attempts_exhausted"
    assert hybrid.upserted == [] and hybrid.batch_fetches == 0
    assert any("catch-up has stopped" in r.getMessage() for r in caplog.records)
    # ...once per slot, not on every 10-minute tick for the rest of the day.
    await job.run_nightly_index_health_catch_up()
    assert sum("catch-up has stopped" in r.getMessage() for r in caplog.records) == 1

    # One below the cap still runs.
    await real_db.execute("UPDATE scheduler_job_slots SET attempts = attempts - 1")
    out = await job.run_nightly_index_health_catch_up()
    assert out["slot_completed"] is True


async def test_catch_up_fails_closed_when_the_ledger_cannot_be_read(monkeypatch, runner, real_db, clock):
    """An unreadable ledger can never show a finished run: failing open would re-run the
    whole job every 10 minutes."""
    hybrid = _HybridDb(real_db)
    _install(monkeypatch, hybrid)

    async def _broken(*a, **k):
        raise RuntimeError("relation scheduler_job_slots is unreadable")

    monkeypatch.setattr(slots, "read_slot", _broken)
    clock["t"] = CRON_FIRES_AT + timedelta(minutes=10)
    out = await job.run_nightly_index_health_catch_up()
    assert out["skip_reason"] == "ledger_unavailable"
    assert hybrid.batch_fetches == 0


async def test_the_04_00_run_still_runs_when_the_ledger_is_broken(monkeypatch, runner, real_db, clock):
    """The ledger may never cost the nightly run itself."""
    hybrid = _HybridDb(real_db)
    _install(monkeypatch, hybrid)

    async def _broken(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(slots, "record_attempt", _broken)
    monkeypatch.setattr(slots, "record_completion", _broken)
    out = await job.run_nightly_index_health()
    assert hybrid.upserted == KEYS
    assert job._STALE_INVALIDATION_UPDATE in hybrid.executed
    assert any("slot_ledger_attempt" in e for e in out["errors"])
    assert any("slot_ledger_completion" in e for e in out["errors"])


def test_slot_for_boundaries():
    assert slots.slot_for(datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc), hour=4) == "2026-09-27"
    assert slots.slot_for(datetime(2026, 9, 27, 3, 59, 59, 999999, tzinfo=timezone.utc), hour=4) == "2026-09-26"
    assert slots.slot_for(datetime(2026, 9, 27, 23, 59, tzinfo=timezone.utc), hour=4) == "2026-09-27"
    # an aware non-UTC time is converted, not read as wall-clock
    tz8 = timezone(timedelta(hours=8))
    assert slots.slot_for(datetime(2026, 9, 27, 11, 0, tzinfo=tz8), hour=4) == "2026-09-26"


# ── the registration ─────────────────────────────────────────────────────────────────


async def test_catch_up_is_registered_to_tick_soon_after_boot_then_every_10_minutes(monkeypatch, runner):
    import services.audit_scheduler as sched

    class _Rec:
        def __init__(self):
            self.added = []

        def add_job(self, func, *a, **k):
            self.added.append((func, a, k))

        def start(self):
            pass

        def get_jobs(self):
            return []

    for k in ("AUDIT_WORKER_ENABLED", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "web")
    monkeypatch.setattr(sched, "_SCHEDULER", None)
    rec = _Rec()
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", lambda *a, **k: rec)

    before = datetime.now(timezone.utc)
    await sched.start_scheduler()
    by_id = {k.get("id"): (func, a, k) for func, a, k in rec.added}

    func, args, kw = by_id["nightly_index_health_catch_up"]
    assert func.__wrapped__ is job.run_nightly_index_health_catch_up
    assert args == ("interval",) and kw["minutes"] == 10
    assert timedelta(seconds=30) <= kw["next_run_time"] - before <= timedelta(seconds=150)

    # The slot the ledger keys on is the slot the cron fires at.
    func, args, kw = by_id["nightly_index_health"]
    assert func.__wrapped__ is job.run_nightly_index_health
    assert args == ("cron",) and kw["hour"] == job.SLOT_HOUR_UTC and kw["minute"] == 0
