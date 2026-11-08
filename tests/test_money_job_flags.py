"""O-05: the scheduler's money jobs run only when their flag is literally 'true'.

Driven through the REAL start_scheduler registration and the runner's run-now path (the same
wrapped callable the cron fires), with the service functions the jobs call replaced by
recorders: flag unset, the job must not reach them and must log that it was skipped through
the "pivota" logger (the one that reaches prod logs).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import services.scheduler_job_allowlist as allowlist_mod
import services.scheduler_job_runner as runner
from pivota_log_capture import capture_pivota_stdout, pivota_lines, root_as_in_prod

_ENV_KEYS = (
    "AUDIT_WORKER_ENABLED", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_NAME", "PIVOTA_ENV", "PIVOTA_SERVICE_NAME", "K_SERVICE", "K_REVISION",
    "K_CONFIGURATION", allowlist_mod.ENV_VAR,
    "SETTLEMENT_FILE_TRANSFER_ENABLED", "PARTNER_SETTLEMENT_MONTHLY_ENABLED",
    "INVOICE_GENERATION_MONTHLY_ENABLED",
)


class _Job:
    def __init__(self, job_id):
        self.id = job_id
        self.next_run_time = datetime(2026, 9, 29, tzinfo=timezone.utc)
        self.trigger = "cron"


class _RecordingScheduler:
    state = 1
    running = True

    def __init__(self, *a, **k):
        self.ids = []
        self.kwargs = {}

    def add_job(self, func, *a, **k):
        self.ids.append(k.get("id"))
        self.kwargs[k.get("id")] = k

    def start(self):
        pass

    def get_jobs(self):
        return [_Job(j) for j in self.ids]


@pytest.fixture
def sched(monkeypatch):
    import services.audit_scheduler as sched_mod

    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sched_mod, "_SCHEDULER", None)
    runner._reset_for_tests()
    allowlist_mod._reset_for_tests()
    yield sched_mod
    runner._reset_for_tests()
    allowlist_mod._reset_for_tests()


@pytest.fixture
def money_calls(monkeypatch):
    """Record every call the three money jobs would make into their services."""
    from services import invoice_generation_service, partner_settlement_service
    from services import settlement_file_service

    calls: list = []

    async def _transfer_all(month):
        calls.append(("transfer_all_pending_for_month", month))
        return {}

    async def _billing(start, end):
        calls.append(("run_billing_cycle", start, end))
        return 1

    async def _settle(run_id):
        calls.append(("run_settlement", run_id))
        return 0

    monkeypatch.setattr(settlement_file_service, "transfer_all_pending_for_month", _transfer_all)
    monkeypatch.setattr(invoice_generation_service, "run_billing_cycle", _billing)
    monkeypatch.setattr(partner_settlement_service, "run_settlement", _settle)

    async def _fetch_one(query, params=None):
        calls.append(("fetch_one", str(query)))
        return {"id": 7, "period_start": None, "period_end": None}

    import db.database as dbmod

    # Patch the METHOD, not the module attribute: modules start_scheduler imports for the first
    # time bind `database` by name, and must keep the real object after this test.
    monkeypatch.setattr(dbmod.database, "fetch_one", _fetch_one)
    return calls


async def _boot(monkeypatch, sched_mod, **env):
    # The production worker's posture (a non-production worker with no allowlist runs nothing).
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("AUDIT_WORKER_ENABLED", "true")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    rec = _RecordingScheduler()
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", lambda *a, **k: rec)
    await sched_mod.start_scheduler()
    assert sched_mod._BOOT_ERROR is None, sched_mod._BOOT_ERROR
    return rec


_JOBS = [
    ("settlement_file_transfer", "SETTLEMENT_FILE_TRANSFER_ENABLED", "transfer_all_pending_for_month"),
    ("partner_settlement_monthly", "PARTNER_SETTLEMENT_MONTHLY_ENABLED", "run_settlement"),
    ("invoice_generation_monthly", "INVOICE_GENERATION_MONTHLY_ENABLED", "run_billing_cycle"),
]


@pytest.mark.parametrize("job_id,flag,service_call", _JOBS)
async def test_flag_unset_the_job_calls_nothing_and_logs_skipped(
    monkeypatch, sched, money_calls, job_id, flag, service_call,
):
    rec = await _boot(monkeypatch, sched)
    # Still registered (and settlement_file_transfer still scheduled): the gate is in the body.
    assert job_id in rec.ids
    with root_as_in_prod(), capture_pivota_stdout() as buf:
        outcome = await runner.run_job_now(job_id)
    assert outcome["outcome"] == "ok", outcome
    assert money_calls == []
    lines = [ln for ln in pivota_lines(buf) if job_id in ln]
    assert any("SKIPPED" in ln and flag in ln for ln in lines), pivota_lines(buf)


@pytest.mark.parametrize("value", ["", "1", "yes", "on", "enabled", "truthy", "false"])
async def test_only_literal_true_arms_the_transfer(monkeypatch, sched, money_calls, value):
    await _boot(monkeypatch, sched, SETTLEMENT_FILE_TRANSFER_ENABLED=value)
    await runner.run_job_now("settlement_file_transfer")
    assert money_calls == []


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
async def test_literal_true_runs_the_transfer(monkeypatch, sched, money_calls, value):
    await _boot(monkeypatch, sched, SETTLEMENT_FILE_TRANSFER_ENABLED=value)
    outcome = await runner.run_job_now("settlement_file_transfer")
    assert outcome["outcome"] == "ok", outcome
    assert [c[0] for c in money_calls] == ["transfer_all_pending_for_month"]


async def test_flag_is_read_at_run_time_not_boot(monkeypatch, sched, money_calls):
    await _boot(monkeypatch, sched)
    await runner.run_job_now("settlement_file_transfer")
    assert money_calls == []
    monkeypatch.setenv("SETTLEMENT_FILE_TRANSFER_ENABLED", "true")
    await runner.run_job_now("settlement_file_transfer")
    assert [c[0] for c in money_calls] == ["transfer_all_pending_for_month"]


@pytest.mark.parametrize("job_id,flag,service_call", _JOBS[1:])
async def test_t7_t8_armed_reach_their_service(
    monkeypatch, sched, money_calls, job_id, flag, service_call,
):
    await _boot(monkeypatch, sched, **{flag: "true"})
    outcome = await runner.run_job_now(job_id)
    assert outcome["outcome"] == "ok", outcome
    assert service_call in [c[0] for c in money_calls]


async def test_registration_shape_is_unchanged(monkeypatch, sched, money_calls):
    """Same trigger kwargs as before: T7/T8 still paused, the transfer still scheduled."""
    rec = await _boot(monkeypatch, sched)
    assert rec.kwargs["invoice_generation_monthly"]["next_run_time"] is None
    assert rec.kwargs["partner_settlement_monthly"]["next_run_time"] is None
    assert "next_run_time" not in rec.kwargs["settlement_file_transfer"]
    assert rec.kwargs["settlement_file_transfer"]["day"] == 10
