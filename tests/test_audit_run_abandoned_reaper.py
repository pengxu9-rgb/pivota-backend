"""Regression tests for fail_abandoned_runs() — the terminal backstop reaper.

Covers the gap that left 10- and 40-day-old runs stuck status='running' in
prod: release_stale_leases() only re-queues rows with a non-NULL expired
lease, so abandoned URL-audit wedge runs (bare asyncio, never leased) and
pre-lease-era orphans (claimed_until IS NULL) sat 'running' forever.

fail_abandoned_runs() is pure raw SQL, so it round-trips against an in-memory
SQLite table built with just the columns it touches. Timestamps are inserted
through the same parameterized execute() path the reaper uses, so the
datetime->TEXT adapter formats stay symmetric and the `< :cutoff` comparison
is apples-to-apples.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
async def sqlite_db(monkeypatch):
    from databases import Database
    # A temp FILE (not :memory:) — in-memory SQLite isolates per connection,
    # so the fixture's CREATE TABLE wouldn't be visible to the reaper's query.
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
          stage_updated_at TIMESTAMP,
          status TEXT NOT NULL,
          stage TEXT,
          claimed_by_worker TEXT,
          claimed_until TIMESTAMP,
          error_message TEXT,
          partial_result_jsonb TEXT
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
    cols = ", ".join(row.keys())
    binds = ", ".join(f":{k}" for k in row)
    await db.execute(
        f"INSERT INTO merchant_audit_runs ({cols}) VALUES ({binds})", row,
    )


@pytest.mark.asyncio
async def test_fails_abandoned_runs_both_orphan_classes(sqlite_db):
    from db.merchant_audit_runs import fail_abandoned_runs

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=2)

    # 1. Abandoned wedge run: never leased (claimed_until NULL), old. Carries
    #    launch debit metadata (a metered wedge run) so the reaper can refund.
    await _insert(
        sqlite_db, run_id="wedge_orphan", merchant_id="m1",
        subject_type="merchant_url", requested_at=old, status="running",
        stage="completed", claimed_until=None,
        partial_result_jsonb=(
            '{"launch": {"billing_mode": "credits", '
            '"estimated_audit_credits": 7}}'
        ),
    )
    # 2. Abandoned durable run: died mid-probe, lease NULL, old.
    await _insert(
        sqlite_db, run_id="durable_orphan", merchant_id="m1",
        subject_type="merchant", requested_at=old, status="running",
        stage="probing", claimed_until=None,
    )

    reaped = await fail_abandoned_runs(ttl_seconds=3600)
    assert len(reaped) == 2
    # The reaped rows carry what the refund path needs: identity + decoded
    # launch options (asyncpg returns JSONB as a JSON string; the db layer
    # decodes so callers never re-implement that).
    by_id = {r["run_id"]: r for r in reaped}
    assert by_id["wedge_orphan"]["merchant_id"] == "m1"
    assert by_id["wedge_orphan"]["launch_options"] == {
        "billing_mode": "credits", "estimated_audit_credits": 7,
    }
    assert by_id["durable_orphan"]["launch_options"] == {}

    for rid in ("wedge_orphan", "durable_orphan"):
        row = await sqlite_db.fetch_one(
            "SELECT status, stage, error_message, completed_at "
            "FROM merchant_audit_runs WHERE run_id = :r", {"r": rid},
        )
        assert row["status"] == "failed"
        assert row["stage"] == "failed"
        assert row["error_message"] == "audit_abandoned_reaped"
        assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_spares_live_lease_and_young_and_terminal_runs(sqlite_db):
    from db.merchant_audit_runs import fail_abandoned_runs

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=2)

    # Live lease: old but a worker holds the lease into the future — actively
    # running, must NOT be reaped.
    await _insert(
        sqlite_db, run_id="live_lease", merchant_id="m1",
        subject_type="merchant", requested_at=old, status="running",
        stage="probing", claimed_by_worker="w1",
        claimed_until=now + timedelta(minutes=10),
    )
    # Young: just started, no lease yet — must NOT be reaped.
    await _insert(
        sqlite_db, run_id="young", merchant_id="m1",
        subject_type="merchant_url", requested_at=now, status="running",
        stage="queued", claimed_until=None,
    )
    # Already terminal: old + succeeded — must NOT be touched.
    await _insert(
        sqlite_db, run_id="done", merchant_id="m1",
        subject_type="merchant", requested_at=old, status="succeeded",
        stage="completed", claimed_until=None, error_message=None,
    )

    assert await fail_abandoned_runs(ttl_seconds=3600) == []

    statuses = {
        r["run_id"]: r["status"]
        for r in await sqlite_db.fetch_all(
            "SELECT run_id, status FROM merchant_audit_runs"
        )
    }
    assert statuses == {
        "live_lease": "running", "young": "running", "done": "succeeded",
    }


@pytest.mark.asyncio
async def test_preserves_existing_error_message(sqlite_db):
    from db.merchant_audit_runs import fail_abandoned_runs

    old = datetime.now(timezone.utc) - timedelta(days=2)
    await _insert(
        sqlite_db, run_id="had_error", merchant_id="m1",
        subject_type="merchant", requested_at=old, status="running",
        stage="probing", claimed_until=None, error_message="upstream_5xx",
    )

    assert len(await fail_abandoned_runs(ttl_seconds=3600)) == 1
    row = await sqlite_db.fetch_one(
        "SELECT status, error_message FROM merchant_audit_runs "
        "WHERE run_id = 'had_error'"
    )
    assert row["status"] == "failed"
    # COALESCE keeps the original reason rather than clobbering it.
    assert row["error_message"] == "upstream_5xx"
