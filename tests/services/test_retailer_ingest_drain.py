"""The drain entrypoint: disabled by default, one stage per execution, errors fail the execution."""
import json

import jobs.retailer_ingest_drain as drain


def test_creating_the_job_is_not_arming_it(monkeypatch, capsys):
    monkeypatch.delenv("RETAILER_INGEST_DRAIN_ENABLED", raising=False)
    monkeypatch.setattr(drain, "drain_once", lambda **kw: (_ for _ in ()).throw(AssertionError("ran")))
    monkeypatch.setattr("sys.argv", ["drain"])
    assert drain.main() == 0
    assert json.loads(capsys.readouterr().out.split(drain.SUMMARY_MARKER, 1)[1]) == {"outcome": "disabled"}


async def _counts(**kw):
    return {"held": 2, "queued": 5}


async def test_nothing_due_is_idle_and_still_reports_held_jobs(monkeypatch):
    async def no_job(**kw):
        return None
    monkeypatch.setattr(drain.ledger, "claim_due_job", no_job)
    monkeypatch.setattr(drain.ledger, "status_counts", _counts)
    assert await drain.drain_once(lease_seconds=60, db=object()) == {"outcome": "idle",
                                                                     "jobs": {"held": 2, "queued": 5}}


async def test_one_claimed_job_runs_exactly_one_stage(monkeypatch):
    calls = []

    async def one_job(**kw):
        return {"id": "rij_1", "status": "queued"}

    async def stage(job, *, db):
        calls.append(job["id"])
        return {"job_id": job["id"], "outcome": "clean", "status": "apply_due"}
    monkeypatch.setattr(drain.ledger, "claim_due_job", one_job)
    monkeypatch.setattr(drain.ledger, "status_counts", _counts)
    monkeypatch.setattr(drain, "run_stage", stage)
    assert (await drain.drain_once(lease_seconds=60, db=object()))["outcome"] == "clean"
    assert calls == ["rij_1"]


def test_an_unexpected_error_fails_the_execution(monkeypatch, capsys):
    monkeypatch.setenv("RETAILER_INGEST_DRAIN_ENABLED", "1")
    monkeypatch.setattr("sys.argv", ["drain"])

    class DB:
        async def connect(self):
            return None

        async def disconnect(self):
            return None
    monkeypatch.setattr(drain, "database", DB())

    async def boom(**kw):
        raise RuntimeError("ledger unreachable")
    monkeypatch.setattr(drain, "drain_once", boom)
    assert drain.main() == 1
    assert '"outcome": "error"' in capsys.readouterr().out


# ------------------------------------------------------------------ back to back within a budget

def _loop_env(monkeypatch, outcomes):
    """drain_once stand-in: one outcome per call, then idle."""
    seen = []

    async def once(**kw):
        outcome = outcomes.pop(0) if outcomes else "idle"
        seen.append(outcome)
        return {"outcome": outcome, "jobs": {}}
    monkeypatch.setattr(drain, "drain_once", once)
    return seen


class Clock:
    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


async def test_a_zero_budget_is_exactly_one_stage(monkeypatch):
    seen = _loop_env(monkeypatch, ["clean", "clean", "clean"])
    await drain.drain_loop(lease_seconds=60, budget_seconds=0, db=object())
    assert seen == ["clean"]


async def test_stages_run_back_to_back_until_nothing_is_due(monkeypatch):
    seen = _loop_env(monkeypatch, ["clean", "held", "applied"])
    reported = []
    await drain.drain_loop(lease_seconds=60, budget_seconds=3000, db=object(), clock=Clock(1),
                           report=reported.append)
    assert seen == ["clean", "held", "applied", "idle"]
    assert [r["outcome"] for r in reported] == seen  # one summary line per stage


class Script:
    """A clock that reads the given times in order (start, then began/ended for each stage)."""
    def __init__(self, times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0)


async def test_a_stage_starts_only_if_the_longest_stage_so_far_still_fits(monkeypatch):
    seen = _loop_env(monkeypatch, ["clean"] * 10)
    # budget 1000s. stage 1: 0->300 (longest 300; 300+300 < 1000 go on); stage 2: 300->500
    # (500+300 = 800 < 1000 go on); stage 3: 500->750 (750+300 >= 1000 stop).
    await drain.drain_loop(lease_seconds=60, budget_seconds=1000, db=object(),
                           clock=Script([0, 0, 300, 300, 500, 500, 750]))
    assert seen == ["clean", "clean", "clean"]


async def test_each_stage_reports_its_duration(monkeypatch):
    _loop_env(monkeypatch, ["clean"])
    reported = []
    await drain.drain_loop(lease_seconds=60, budget_seconds=0, db=object(), clock=Script([0, 5, 47.25]),
                           report=reported.append)
    assert reported[0]["duration_s"] == 42.2 or reported[0]["duration_s"] == 42.3


async def test_a_throttled_crawl_ends_the_loop(monkeypatch):
    seen = _loop_env(monkeypatch, ["clean", "crawl_throttled", "clean"])
    await drain.drain_loop(lease_seconds=60, budget_seconds=3000, db=object(), clock=Clock(1))
    assert seen == ["clean", "crawl_throttled"]


def test_an_error_mid_loop_fails_the_execution_after_reporting_earlier_stages(monkeypatch, capsys):
    monkeypatch.setenv("RETAILER_INGEST_DRAIN_ENABLED", "1")
    monkeypatch.setenv("RETAILER_INGEST_DRAIN_BUDGET_SECONDS", "3000")
    monkeypatch.setattr("sys.argv", ["drain"])

    class DB:
        async def connect(self):
            return None

        async def disconnect(self):
            return None
    monkeypatch.setattr(drain, "database", DB())
    calls = []

    async def once(**kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("stage blew up")
        return {"outcome": "clean", "jobs": {}}
    monkeypatch.setattr(drain, "drain_once", once)
    assert drain.main() == 1
    out = capsys.readouterr().out
    assert out.count(drain.SUMMARY_MARKER) == 2 and '"outcome": "clean"' in out and '"outcome": "error"' in out


async def test_each_claim_leases_only_what_is_left_of_the_task(monkeypatch):
    leases = []

    async def once(**kw):
        leases.append(kw["lease_seconds"])
        return {"outcome": "clean", "jobs": {}}
    monkeypatch.setattr(drain, "drain_once", once)
    # stage 1 claims at t=0 (3600 left), stage 2 at t=1000 (2600 left); budget stops after stage 2.
    await drain.drain_loop(lease_seconds=4200, budget_seconds=1800, db=object(), task_timeout_seconds=3600,
                           clock=Script([0, 0, 500, 1000, 1500]))
    assert leases == [4200, 2600 + drain.LEASE_SLACK_SECONDS]
