"""utils/db_retry.with_asyncpg_busy_retry — now on two charge paths
(routes/agent_payment_sdk and services/acp_offsession_payment), so its retry
posture is pinned directly: non-busy errors pass through untouched, a busy
error is retried exactly once, and exhaustion surfaces db_busy_http_exception.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from utils import db_retry  # noqa: E402


class _BusyError(Exception):
    pass


def _force_busy(monkeypatch, matcher):
    monkeypatch.setattr(db_retry, "is_asyncpg_busy_error", matcher)


@pytest.mark.asyncio
async def test_success_passes_through():
    async def op():
        return "ok"

    assert await db_retry.with_asyncpg_busy_retry("t", op) == "ok"


@pytest.mark.asyncio
async def test_non_busy_error_is_not_retried(monkeypatch):
    _force_busy(monkeypatch, lambda exc: False)
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise ValueError("not busy")

    with pytest.raises(ValueError):
        await db_retry.with_asyncpg_busy_retry("t", op)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_busy_error_retries_once_then_succeeds(monkeypatch):
    _force_busy(monkeypatch, lambda exc: isinstance(exc, _BusyError))
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _BusyError("busy")
        return "recovered"

    out = await db_retry.with_asyncpg_busy_retry("t", op, base_delay_seconds=0.0)
    assert out == "recovered"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_busy_exhaustion_raises_db_busy_http_exception(monkeypatch):
    _force_busy(monkeypatch, lambda exc: isinstance(exc, _BusyError))
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise _BusyError("busy forever")

    with pytest.raises(HTTPException) as ei:
        await db_retry.with_asyncpg_busy_retry("t", op, base_delay_seconds=0.0)
    assert calls["n"] == 2  # default attempts
    assert ei.value.status_code == 503


def test_route_re_export_is_the_same_object():
    from routes import agent_payment_sdk

    assert agent_payment_sdk._with_asyncpg_busy_retry is db_retry.with_asyncpg_busy_retry
