"""Tests for the /__scheduler_health endpoint.

This endpoint exists because Railway prod filters app-level INFO logs,
so the audit_scheduler success log line never surfaced in log fetches —
making it impossible to confirm worker boot without code spelunking.
The endpoint reports state directly.

These tests exercise the endpoint's response shape across the three
states it must report:
  1. scheduler not running (get_scheduler returns None)
  2. scheduler running with jobs registered
  3. scheduler module import fails (defensive)
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest


@pytest.fixture
def client():
    """FastAPI TestClient bound to the route under test only.

    Mounting only the scheduler_health router (not the full main app)
    keeps the test fast + avoids startup side-effects (DB connects,
    actual scheduler boot, etc.)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes.scheduler_health import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_scheduler_health_reports_not_running_when_get_scheduler_returns_none(
    client, monkeypatch,
):
    """If start_scheduler() never ran (or its boot was swallowed),
    get_scheduler() returns None — endpoint must report running=False
    with a diagnostic reason, NOT 500."""
    import services.audit_scheduler as audit_scheduler

    monkeypatch.setattr(audit_scheduler, "_SCHEDULER", None)

    response = client.get("/__scheduler_health")
    assert response.status_code == 200
    body = response.json()
    assert body["running"] is False
    assert body["job_count"] == 0
    assert body["jobs"] == []
    assert "reason" in body
    assert "None" in body["reason"]


def test_scheduler_health_reports_running_with_jobs(client, monkeypatch):
    """When the scheduler is running, the endpoint must enumerate
    registered jobs with their id + next_run_time + trigger summary."""
    import services.audit_scheduler as audit_scheduler

    class _FakeJob:
        def __init__(self, job_id: str, trigger_str: str, next_run):
            self.id = job_id
            self._trigger_str = trigger_str
            self.next_run_time = next_run

        @property
        def trigger(self):
            return self._trigger_str

    class _FakeScheduler:
        running = True
        def get_jobs(self):
            return [
                _FakeJob(
                    "audit_run_worker_tick",
                    "interval[0:00:10]",
                    datetime(2026, 5, 12, 18, 0, 0, tzinfo=timezone.utc),
                ),
                _FakeJob(
                    "executor_run_worker_tick",
                    "interval[0:00:05]",
                    datetime(2026, 5, 12, 18, 0, 5, tzinfo=timezone.utc),
                ),
            ]

    monkeypatch.setattr(audit_scheduler, "_SCHEDULER", _FakeScheduler())

    response = client.get("/__scheduler_health")
    assert response.status_code == 200
    body = response.json()
    assert body["running"] is True
    assert body["job_count"] == 2
    job_ids = [j["id"] for j in body["jobs"]]
    assert "audit_run_worker_tick" in job_ids
    assert "executor_run_worker_tick" in job_ids
    # next_run_time must surface as ISO 8601 string (not datetime obj),
    # so the JSON response is parseable without custom decoders.
    for job in body["jobs"]:
        assert isinstance(job["next_run_time"], str)
        assert "T" in job["next_run_time"]
        assert "trigger" in job


def test_scheduler_health_does_not_500_when_get_jobs_raises(
    client, monkeypatch,
):
    """If the scheduler is in a weird intermediate state where the
    instance exists but get_jobs() raises, the endpoint must still
    return 200 with a diagnostic reason. 5xx here would page oncall
    on a transient race."""
    import services.audit_scheduler as audit_scheduler

    class _BrokenScheduler:
        running = True
        def get_jobs(self):
            raise RuntimeError("scheduler in unstable state")

    monkeypatch.setattr(audit_scheduler, "_SCHEDULER", _BrokenScheduler())

    response = client.get("/__scheduler_health")
    assert response.status_code == 200
    body = response.json()
    assert "reason" in body
    assert "unstable state" in body["reason"]


def test_scheduler_health_surfaces_paused_silent_stall(client, monkeypatch):
    """THE silent-stall class: a PAUSED scheduler whose every job has
    next_run_time=None fires NOTHING, yet `running` (state != STOPPED) is
    still True. The diagnostic must distinguish it: state_name='PAUSED' and
    fireable_job_count=0, so operators aren't fooled by running:True."""
    import services.audit_scheduler as audit_scheduler

    class _Job:
        def __init__(self, jid):
            self.id = jid
            self.next_run_time = None  # paused → no next fire
            self.trigger = "cron[hour=4]"

    class _PausedScheduler:
        running = True          # state != STOPPED → property is True even paused
        state = 2               # STATE_PAUSED
        def get_jobs(self):
            return [_Job("nightly_index_health"), _Job("audit_run_worker_tick")]

    monkeypatch.setattr(audit_scheduler, "_SCHEDULER", _PausedScheduler())

    body = client.get("/__scheduler_health").json()
    assert body["running"] is True            # the misleading legacy flag
    assert body["state_name"] == "PAUSED"     # the truth the diagnostic adds
    assert body["fireable_job_count"] == 0    # nothing will fire
    assert body["job_count"] == 2
