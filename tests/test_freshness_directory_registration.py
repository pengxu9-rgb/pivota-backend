"""R-03 over the protocol directory: the job is registered ONLY while FRESHNESS_DIRECTORY_CHECK_ENABLED
is on, and with it off the worker registers exactly main's job list.

Drives the real `services.audit_scheduler.start_scheduler()` through the allowlist suite's recording
scheduler. Needs no database, so it is not a Postgres twin and lives apart from
tests/test_freshness_directory_check.py, whose every case runs on both engines.

    .venv/bin/python -m pytest tests/test_freshness_directory_registration.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import freshness_clock_check as fresh  # noqa: E402

from test_scheduler_job_allowlist import clean_state  # noqa: E402,F401


@pytest.mark.parametrize("value,expected", [
    (None, []),
    ("", []),
    ("0", []),
    ("false", []),
    ("off", []),
    ("1", [fresh.DIRECTORY_JOB_ID]),
    ("true", [fresh.DIRECTORY_JOB_ID]),
], ids=["unset", "empty", "0", "false", "off", "1", "true"])
async def test_registered_only_while_its_own_flag_is_on(monkeypatch, clean_state, value, expected):
    """Flag off: EXACTLY main's job list (the allowlist suite's own constant)."""
    from test_scheduler_job_allowlist import MAIN_WORKER_JOB_IDS, _start as start_scheduler

    monkeypatch.delenv(fresh.DIRECTORY_FLAG_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, value)
    rec = await start_scheduler(monkeypatch, clean_state, PIVOTA_ENV="production", AUDIT_WORKER_ENABLED="true")
    assert [j for j in rec.ids if j not in MAIN_WORKER_JOB_IDS] == expected
    assert [j for j in rec.ids if j in MAIN_WORKER_JOB_IDS] == MAIN_WORKER_JOB_IDS
    for job_id in expected:
        assert clean_state.run_deadline_for(job_id) == 300


async def test_the_job_is_an_hourly_cron_not_an_interval(monkeypatch, clean_state):
    """An `interval` clock restarts on every worker deploy (which is every merge) and starves; the
    went-silent policy would then page with the job healthy. Pinned: cron at a fixed minute, with
    a misfire grace and coalescing, like the module's other hourly crons."""
    import test_scheduler_job_allowlist as allowlist_suite

    calls = []

    class _Recording(allowlist_suite._RecordingScheduler):
        def add_job(self, func, *a, **k):
            super().add_job(func, *a, **k)
            calls.append((a, k))

    rec = _Recording()
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", lambda *a, **k: rec)
    for key, value in {"PIVOTA_ENV": "production", "AUDIT_WORKER_ENABLED": "true",
                       fresh.DIRECTORY_FLAG_ENV: "1"}.items():
        monkeypatch.setenv(key, value)
    await clean_state.start_scheduler()
    assert clean_state._BOOT_ERROR is None, clean_state._BOOT_ERROR
    [(args, kwargs)] = [(a, k) for a, k in calls if k.get("id") == fresh.DIRECTORY_JOB_ID]
    assert args == ("cron",)
    assert {k: v for k, v in kwargs.items() if k != "id"} == {
        "minute": 34, "replace_existing": True, "misfire_grace_time": 1800, "coalesce": True, "max_instances": 1}
    assert fresh.DIRECTORY_CRON_MINUTE == 34 and not 10 <= fresh.DIRECTORY_CRON_MINUTE <= 25
