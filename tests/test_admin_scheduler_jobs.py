from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

import routes.admin_scheduler_jobs as module
import services.audit_scheduler as audit_scheduler


class _FakeJob:
    def __init__(self, job_id: str, next_run_time: datetime | None) -> None:
        self.id = job_id
        self.next_run_time = next_run_time
        self.trigger = "cron[day=3,hour=4]"


class _FakeScheduler:
    def __init__(self, jobs: list[_FakeJob], *, running: bool = True) -> None:
        self.running = running
        self._jobs = {j.id: j for j in jobs}

    def get_job(self, job_id: str) -> _FakeJob | None:
        return self._jobs.get(job_id)

    def get_jobs(self) -> list[_FakeJob]:
        return list(self._jobs.values())

    def resume_job(self, job_id: str) -> None:
        self._jobs[job_id].next_run_time = datetime(2026, 8, 3, 4, tzinfo=timezone.utc)

    def pause_job(self, job_id: str) -> None:
        self._jobs[job_id].next_run_time = None


def _install_scheduler(
    monkeypatch: pytest.MonkeyPatch, scheduler: _FakeScheduler | None
) -> None:
    monkeypatch.setattr(audit_scheduler, "get_scheduler", lambda: scheduler)


def _build_client(*, authenticated: bool = True) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(module.router)
    if authenticated:
        app.dependency_overrides[module.require_admin] = lambda: {
            "email": "admin@example.com",
            "role": "admin",
        }
    return TestClient(app), app


def test_resume_paused_settlement_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_scheduler(
        monkeypatch,
        _FakeScheduler([_FakeJob("partner_settlement_monthly", None)]),
    )
    client, app = _build_client()
    try:
        response = client.post(
            "/admin/scheduler/jobs/partner_settlement_monthly/resume"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "resume"
    assert body["paused"] is False
    assert body["next_run_time"] is not None


def test_pause_running_settlement_job(monkeypatch: pytest.MonkeyPatch) -> None:
    running_at = datetime(2026, 8, 3, 4, tzinfo=timezone.utc)
    _install_scheduler(
        monkeypatch,
        _FakeScheduler([_FakeJob("partner_settlement_monthly", running_at)]),
    )
    client, app = _build_client()
    try:
        response = client.post(
            "/admin/scheduler/jobs/partner_settlement_monthly/pause"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "pause"
    assert body["paused"] is True
    assert body["next_run_time"] is None


def test_invalid_action_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_scheduler(
        monkeypatch,
        _FakeScheduler([_FakeJob("partner_settlement_monthly", None)]),
    )
    client, app = _build_client()
    try:
        response = client.post(
            "/admin/scheduler/jobs/partner_settlement_monthly/restart"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_action"


def test_non_allowlisted_job_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_scheduler(
        monkeypatch,
        _FakeScheduler([_FakeJob("gmv_aggregation_daily", None)]),
    )
    client, app = _build_client()
    try:
        response = client.post("/admin/scheduler/jobs/gmv_aggregation_daily/resume")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "job_not_manageable"
    assert "partner_settlement_monthly" in body["allowed_values"]


def test_allowlisted_but_unregistered_job_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Allowlisted id, but the scheduler has no such job registered.
    _install_scheduler(monkeypatch, _FakeScheduler([]))
    client, app = _build_client()
    try:
        response = client.post(
            "/admin/scheduler/jobs/settlement_file_transfer/resume"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["error"] == "job_not_found"


def test_scheduler_not_running_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_scheduler(monkeypatch, None)
    client, app = _build_client()
    try:
        response = client.post(
            "/admin/scheduler/jobs/partner_settlement_monthly/resume"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["error"] == "scheduler_not_running"


def test_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_scheduler(
        monkeypatch,
        _FakeScheduler([_FakeJob("partner_settlement_monthly", None)]),
    )
    client, app = _build_client(authenticated=False)
    try:
        response = client.post(
            "/admin/scheduler/jobs/partner_settlement_monthly/resume"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code in {401, 403}


def test_list_managed_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_scheduler(
        monkeypatch,
        _FakeScheduler(
            [
                _FakeJob("partner_settlement_monthly", None),
                _FakeJob(
                    "settlement_file_transfer",
                    datetime(2026, 8, 10, 2, tzinfo=timezone.utc),
                ),
            ]
        ),
    )
    client, app = _build_client()
    try:
        response = client.get("/admin/scheduler/jobs")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    jobs = {j["id"]: j for j in response.json()["jobs"]}
    # All four allowlisted ids are reported, even the unregistered ones.
    assert set(jobs) == module._MANAGEABLE_JOB_IDS
    assert jobs["partner_settlement_monthly"]["paused"] is True
    assert jobs["settlement_file_transfer"]["paused"] is False
    assert jobs["invoice_generation_monthly"]["registered"] is False
