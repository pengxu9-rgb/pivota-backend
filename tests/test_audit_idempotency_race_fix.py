"""Regression tests for the P0-3 audit idempotency race.

Before this fix, POST /api/audits did:

  1. find_in_flight_by_idempotency_key — return existing run_id if hit
  2. if miss → enqueue_audit_run — INSERT a new row

The check-then-insert window (1→2) is non-atomic. Two concurrent
POSTs with the same payload could BOTH see no in-flight row at
step 1, then BOTH INSERT at step 2. The idempotency_key index from
migration 083 was non-unique, so both inserts succeeded. Result: 2
audits enqueued, 2× LLM cost for one customer ask.

Fix:
  - Migration 144 adds `uniq_merchant_audit_runs_active_idempotency_key`
    — a partial UNIQUE index on (merchant_id, idempotency_key) WHERE active-stage.
    db/schema_guard.py self-heals it on startup so deploys that
    skip db/migrations/ still get the protection.
  - New `enqueue_audit_run_with_replay` uses
    INSERT ... ON CONFLICT (...) WHERE ... DO NOTHING RETURNING.
    On conflict (rowcount=0 returned), it fetches the existing
    run_id and returns (run_id, was_existing=True).
  - POST /api/audits route uses the new function and surfaces
    was_existing as `idempotent_replay`.

These tests cover the SQL shape (ON CONFLICT used + scoped to the
right constraint), the tuple-return semantics, the back-compat
single-return shim, and the schema_guard self-heal.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest


@pytest.mark.asyncio
async def test_enqueue_with_replay_uses_on_conflict_when_idempotency_key_set(monkeypatch):
    """When idempotency_key is set, the enqueue must hit the
    ON CONFLICT path against the active-stage partial unique index —
    not a plain INSERT."""
    from db import merchant_audit_runs as mar

    seen_sql: List[str] = []

    class DummyDB:
        async def execute(self, query):
            # Plain insert path captures the SQLAlchemy core query.
            seen_sql.append(str(query.compile(
                compile_kwargs={"literal_binds": True},
            )))
            return 1

        async def fetch_one(self, query, params=None):
            # Raw SQL path captures the string directly.
            seen_sql.append(str(query))
            # Simulate fresh insert (returned row with run_id).
            return {"run_id": params["run_id"]}

    async def noop():
        return None

    monkeypatch.setattr(mar, "database", DummyDB())
    monkeypatch.setattr(mar, "ensure_merchant_audit_runs_table", noop)

    run_id, was_existing = await mar.enqueue_audit_run_with_replay(
        merchant_id="m1",
        product_keys=["k1"],
        subject_type="merchant",
        idempotency_key="idemp-1",
        requested_by_user_id="m1",
    )

    assert run_id is not None
    assert was_existing is False, "Fresh insert → was_existing False"
    assert len(seen_sql) == 1
    sql = seen_sql[0].lower()
    assert "on conflict" in sql, (
        "Idempotency-key INSERT must use ON CONFLICT — without it the "
        "race that motivated this fix is still open"
    )
    assert "on conflict (merchant_id, idempotency_key)" in sql, (
        "ON CONFLICT must infer the same columns as the partial "
        "unique index"
    )
    assert "on constraint" not in sql, (
        "Partial unique indexes are not valid ON CONSTRAINT targets"
    )
    assert "stage = any(array[" in sql, (
        "ON CONFLICT inference must include the active-stage predicate"
    )
    assert "do nothing" in sql, (
        "ON CONFLICT must be DO NOTHING (not DO UPDATE) so we don't "
        "mutate the existing in-flight row"
    )


@pytest.mark.asyncio
async def test_enqueue_with_replay_returns_existing_on_conflict(monkeypatch):
    """Simulated race: INSERT returns no row (conflict), enqueue
    falls back to find_in_flight and signals replay."""
    from db import merchant_audit_runs as mar

    class DummyDB:
        async def fetch_one(self, query, params=None):
            # On the INSERT ... RETURNING attempt, return None to
            # signal the ON CONFLICT DO NOTHING fired (no row inserted).
            # On the subsequent find_in_flight query, return the
            # winning run_id.
            sql = str(query).lower()
            if "insert into merchant_audit_runs" in sql:
                return None
            return {0: "existing-run-id-123"} if hasattr(query, "limit") \
                else {"run_id": "existing-run-id-123"}

        async def execute(self, query):
            return 1

    async def noop():
        return None

    async def fake_find_in_flight(*, idempotency_key):
        return "existing-run-id-123" if idempotency_key else None

    monkeypatch.setattr(mar, "database", DummyDB())
    monkeypatch.setattr(mar, "ensure_merchant_audit_runs_table", noop)
    monkeypatch.setattr(
        mar, "find_in_flight_by_idempotency_key", fake_find_in_flight,
    )

    run_id, was_existing = await mar.enqueue_audit_run_with_replay(
        merchant_id="m1",
        product_keys=["k1"],
        subject_type="merchant",
        idempotency_key="idemp-race",
        requested_by_user_id="m1",
    )

    assert run_id == "existing-run-id-123"
    assert was_existing is True, (
        "ON CONFLICT path must signal was_existing=True so the route "
        "can surface idempotent_replay correctly"
    )


@pytest.mark.asyncio
async def test_enqueue_with_replay_no_idempotency_key_skips_on_conflict(monkeypatch):
    """idempotency_key=None means no dedupe possible — use plain
    insert (preserves behavior for force=True path + legacy callers)."""
    from db import merchant_audit_runs as mar

    captured: List[str] = []

    class DummyDB:
        async def execute(self, query):
            captured.append(str(query))
            return 1

        async def fetch_one(self, query, params=None):
            captured.append(str(query))
            return None

    async def noop():
        return None

    monkeypatch.setattr(mar, "database", DummyDB())
    monkeypatch.setattr(mar, "ensure_merchant_audit_runs_table", noop)

    run_id, was_existing = await mar.enqueue_audit_run_with_replay(
        merchant_id="m1", product_keys=["k1"],
        idempotency_key=None,
    )
    assert run_id is not None
    assert was_existing is False
    assert len(captured) == 1
    assert "on conflict" not in captured[0].lower(), (
        "No idempotency_key → no ON CONFLICT clause needed"
    )


@pytest.mark.asyncio
async def test_enqueue_audit_run_backcompat_shim_returns_str(monkeypatch):
    """The historical enqueue_audit_run signature returns Optional[str]
    only — legacy callers (the sync fallback in
    /ai-commerce-readiness) must keep working unchanged."""
    from db import merchant_audit_runs as mar

    async def fake_with_replay(**kwargs):
        return ("fresh-run-id", False)

    monkeypatch.setattr(
        mar, "enqueue_audit_run_with_replay", fake_with_replay,
    )

    out = await mar.enqueue_audit_run(
        merchant_id="m1", product_keys=["k1"],
    )
    assert out == "fresh-run-id"
    assert isinstance(out, str), (
        "enqueue_audit_run back-compat shim must return Optional[str]"
    )


def test_schema_guard_self_heals_unique_idempotency_index():
    """Sentinel: schema_guard must self-heal the partial unique
    idempotency index so the P0-3 protection survives a deploy
    that skipped migration 144."""
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[1]
    guard_text = (repo_root / "db" / "schema_guard.py").read_text()
    assert "uniq_merchant_audit_runs_active_idempotency_key" in guard_text, (
        "schema_guard must declare the partial unique idempotency "
        "constraint inline so it self-heals on startup"
    )
    assert "CREATE UNIQUE INDEX" in guard_text, (
        "self-heal must use CREATE UNIQUE INDEX (not just INDEX) so "
        "the constraint is actually enforced"
    )


def test_migration_144_exists_and_matches_guard():
    """Sentinel: migration 144 sql and the schema_guard inline block
    must reference the same constraint name + same active-stage
    predicate. A drift between the two re-introduces the gap."""
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[1]
    migration = (
        repo_root / "db" / "migrations"
        / "144_audit_runs_unique_idempotency.sql"
    )
    assert migration.exists(), (
        "Migration 144 must exist as the canonical SQL — schema_guard "
        "is the runtime safety net, not the source of truth"
    )
    sql = migration.read_text()
    for token in (
        "uniq_merchant_audit_runs_active_idempotency_key",
        "CREATE UNIQUE INDEX",
        "merchant_id, idempotency_key",
        "WHERE idempotency_key IS NOT NULL",
        "queued",
        "verifying",
    ):
        assert token in sql, f"Migration 144 missing token {token!r}"
