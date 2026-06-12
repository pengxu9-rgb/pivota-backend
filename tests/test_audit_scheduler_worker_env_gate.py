"""The shared-queue worker ticks must only run on the production worker.

Prod + staging share one Postgres, and claim_next_pending_run has no env filter,
so a staging service draining the queue poaches prod-enqueued runs it can't
complete. The gate disables drainers on staging while staying fail-safe toward
ENABLED so a detection miss never stops the prod worker.
"""
from __future__ import annotations

import pytest

from services.audit_scheduler import _queue_worker_enabled

_ENV_KEYS = ("AUDIT_WORKER_ENABLED", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _set(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_default_unknown_is_enabled_fail_safe(monkeypatch):
    # No markers (can't prove staging) -> stay ON so prod is never disabled.
    assert _queue_worker_enabled() is True


def test_prod_web_service_enabled(monkeypatch):
    _set(monkeypatch, RAILWAY_SERVICE_NAME="web")
    assert _queue_worker_enabled() is True


def test_staging_service_disabled(monkeypatch):
    _set(monkeypatch, RAILWAY_SERVICE_NAME="web-staging")
    assert _queue_worker_enabled() is False


def test_staging_environment_disabled(monkeypatch):
    _set(monkeypatch, RAILWAY_ENVIRONMENT="staging")
    assert _queue_worker_enabled() is False


def test_explicit_override_enables_on_staging(monkeypatch):
    _set(monkeypatch, RAILWAY_SERVICE_NAME="web-staging", AUDIT_WORKER_ENABLED="true")
    assert _queue_worker_enabled() is True


def test_explicit_override_disables_on_prod(monkeypatch):
    _set(monkeypatch, RAILWAY_SERVICE_NAME="web", AUDIT_WORKER_ENABLED="false")
    assert _queue_worker_enabled() is False


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
])
def test_override_truthiness(monkeypatch, val, expected):
    _set(monkeypatch, AUDIT_WORKER_ENABLED=val)
    assert _queue_worker_enabled() is expected


# --- integration: start_scheduler registers nothing on staging ---------------
import pytest as _pytest


class _RecordingScheduler:
    def __init__(self, *a, **k):
        self.jobs = []

    def add_job(self, func, *a, **k):
        self.jobs.append(k.get("id"))

    def start(self):
        pass

    def get_jobs(self):
        return list(self.jobs)


async def _run_start(monkeypatch, **env):
    import services.audit_scheduler as sched
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sched, "_SCHEDULER", None)
    rec = _RecordingScheduler()
    monkeypatch.setattr(
        "apscheduler.schedulers.asyncio.AsyncIOScheduler",
        lambda *a, **k: rec,
    )
    await sched.start_scheduler()
    return rec


@_pytest.mark.asyncio
async def test_start_scheduler_registers_no_jobs_on_staging(monkeypatch):
    rec = await _run_start(monkeypatch, RAILWAY_SERVICE_NAME="web-staging")
    assert rec.jobs == [], f"staging must register NO jobs, got {rec.jobs}"


@_pytest.mark.asyncio
async def test_start_scheduler_registers_jobs_on_prod(monkeypatch):
    rec = await _run_start(monkeypatch, RAILWAY_SERVICE_NAME="web")
    # crons AND drainers register on the prod worker.
    assert "daily_audit_check" in rec.jobs
    assert "audit_run_worker_tick" in rec.jobs
    assert "partner_settlement_monthly" in rec.jobs
    assert len(rec.jobs) > 8
