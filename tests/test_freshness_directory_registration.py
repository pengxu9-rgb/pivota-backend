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
