"""scheduled_job_completions on the production dialect: the self-heal builds it, and
the marker is forward-only and round-trips a UTC slot through TIMESTAMPTZ.

Runs the module's real ensure + accessors against a scratch schema (the module's
`database` swapped for one whose search_path is that schema alone), so the shared
dialect-gate database is never touched.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_scheduled_job_completions_postgres.py

THIS MODULE MUST NOT IMPORT `main` (the gate runs every test_*_postgres.py in one process).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"sched_job_completions_test_{os.getpid()}"

SLOT = datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc)


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r}; throwaway only")


def _async_url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@pytest.fixture(autouse=True)
async def completions(monkeypatch):
    import databases

    import db.scheduled_job_completions as mod

    _assert_throwaway_database()
    admin = databases.Database(_async_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    # A non-UTC session zone, so a slot that is only right because the session
    # happens to be UTC would show up as off by hours.
    scoped = databases.Database(
        _async_url(),
        server_settings={"search_path": _SCHEMA, "timezone": "America/Los_Angeles"},
    )
    await scoped.connect()
    monkeypatch.setattr(mod, "database", scoped)
    monkeypatch.setattr(mod, "_DDL_READY", False)
    try:
        yield mod
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


async def test_the_self_heal_builds_the_table_and_the_marker_moves_forward_only(completions):
    assert await completions.last_completed_slot("nightly_index_health") is None
    assert completions._DDL_READY is True

    await completions.record_completion("nightly_index_health", SLOT)
    got = await completions.last_completed_slot("nightly_index_health")
    assert got == SLOT and got.utcoffset() == timedelta(0)

    await completions.record_completion("nightly_index_health", SLOT - timedelta(days=1))
    assert await completions.last_completed_slot("nightly_index_health") == SLOT

    await completions.record_completion("nightly_index_health", SLOT + timedelta(days=1))
    assert await completions.last_completed_slot("nightly_index_health") == SLOT + timedelta(days=1)

    # rows are per job
    assert await completions.last_completed_slot("some_other_job") is None


async def test_the_self_heal_is_rerunnable(completions):
    await completions.ensure_scheduled_job_completions_table()
    completions._DDL_READY = False
    await completions.ensure_scheduled_job_completions_table()
    assert completions._DDL_READY is True
