"""Regression tests for the completed-run readers in db/merchant_audit_runs.py.

These four readers (score_history_for_merchant, recent_completed_reports,
recent_completed_reports_for_merchant, completed_runs_in_window) filtered on
`status = 'completed'` — a value NO writer ever produces. Successful runs get
status='succeeded' (transition_stage stamps it when stage reaches 'completed';
every record_audit_run_completed caller passes it too), so all four readers
returned [] forever and GET /api/merchant-center/audit/tracking always plotted
an empty series. Prod at fix time: 168 succeeded / 13 failed / 0 'completed'.

Also pins the trap that rules out filtering on stage instead: the legacy
insert path (record_audit_run_started) doesn't set `stage`, so its server
default 'completed' stamps RUNNING rows — a stage-based filter would count
in-flight and legacy-failed runs as done.

Same hermetic temp-file-SQLite pattern as test_audit_run_abandoned_reaper.py.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
async def sqlite_db(monkeypatch):
    from databases import Database
    # A temp FILE (not :memory:) — in-memory SQLite isolates per connection,
    # so the fixture's CREATE TABLE wouldn't be visible to the readers' query.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = Database(f"sqlite:///{tmp.name}")
    await db.connect()
    await db.execute("""
        CREATE TABLE merchant_audit_runs (
          run_id TEXT PRIMARY KEY,
          merchant_id TEXT NOT NULL,
          subject_type TEXT,
          requested_at TIMESTAMP NOT NULL,
          completed_at TIMESTAMP,
          status TEXT NOT NULL,
          stage TEXT NOT NULL DEFAULT 'completed',
          visibility_score_avg INTEGER,
          attribution_score_avg INTEGER,
          category_visibility_score_avg INTEGER,
          report_jsonb TEXT
        )
    """)
    monkeypatch.setattr("db.merchant_audit_runs.database", db)
    import db.merchant_audit_runs as mar
    mar._DDL_READY = True
    yield db
    await db.disconnect()
    mar._DDL_READY = False
    os.unlink(tmp.name)


async def _insert(db, **row):
    row.setdefault("subject_type", "merchant")
    row.setdefault("requested_at", _now())
    row.setdefault("report_jsonb", json.dumps({"basis_id": "b1"}))
    cols = ", ".join(row)
    params = ", ".join(f":{k}" for k in row)
    await db.execute(
        f"INSERT INTO merchant_audit_runs ({cols}) VALUES ({params})", row
    )


@pytest.mark.anyio
async def test_score_history_returns_succeeded_runs_only(sqlite_db):
    """THE bug: succeeded rows must come back; running/failed must not —
    even though all three carry stage='completed' via the legacy default."""
    from db.merchant_audit_runs import score_history_for_merchant

    t0 = _now() - timedelta(days=2)
    await _insert(
        sqlite_db, run_id="r1", merchant_id="m1", status="succeeded",
        requested_at=t0, visibility_score_avg=6, attribution_score_avg=55,
    )
    await _insert(
        sqlite_db, run_id="r2", merchant_id="m1", status="succeeded",
        requested_at=t0 + timedelta(days=1), visibility_score_avg=8,
    )
    await _insert(sqlite_db, run_id="r3", merchant_id="m1", status="running")
    await _insert(sqlite_db, run_id="r4", merchant_id="m1", status="failed")

    rows = await score_history_for_merchant(merchant_id="m1")
    assert [r["run_id"] for r in rows] == ["r1", "r2"]  # oldest-first
    assert rows[0]["visibility"] == 6
    assert rows[0]["attribution"] == 55
    assert rows[0]["report_jsonb"] == {"basis_id": "b1"}


@pytest.mark.anyio
async def test_score_history_separates_subject_types(sqlite_db):
    """merchant vs merchant_url are disjoint series — the url-audit page
    queries subject_type='merchant_url' and must not see catalog runs."""
    from db.merchant_audit_runs import score_history_for_merchant

    await _insert(sqlite_db, run_id="cat", merchant_id="m1", status="succeeded")
    await _insert(
        sqlite_db, run_id="url", merchant_id="m1", status="succeeded",
        subject_type="merchant_url",
    )

    catalog = await score_history_for_merchant(merchant_id="m1")
    url = await score_history_for_merchant(
        merchant_id="m1", subject_type="merchant_url"
    )
    assert [r["run_id"] for r in catalog] == ["cat"]
    assert [r["run_id"] for r in url] == ["url"]


@pytest.mark.anyio
async def test_w7_readers_see_succeeded_runs(sqlite_db):
    """The three W7 canary readers shared the same dead filter."""
    from db.merchant_audit_runs import (
        completed_runs_in_window,
        recent_completed_reports,
        recent_completed_reports_for_merchant,
    )

    await _insert(sqlite_db, run_id="ok", merchant_id="m1", status="succeeded")
    await _insert(sqlite_db, run_id="bad", merchant_id="m1", status="failed")
    await _insert(
        sqlite_db, run_id="noreport", merchant_id="m1", status="succeeded",
        report_jsonb=None,
    )

    recent = await recent_completed_reports()
    assert [r["run_id"] for r in recent] == ["ok"]

    per_merchant = await recent_completed_reports_for_merchant(merchant_id="m1")
    assert [r["run_id"] for r in per_merchant] == ["ok"]

    windowed = await completed_runs_in_window(window_seconds=3600)
    assert [r["run_id"] for r in windowed] == ["ok"]
