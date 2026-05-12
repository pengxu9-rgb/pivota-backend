"""Tests for scripts/sweep_stale_catalog_products.py — Stage 2a sweep.

The sweep's correctness directly affects what shows up in agent recall.
A wrong sweep could tombstone live products (cost: missed sales) or
fail to tombstone deleted ones (cost: stale PDPs in chat). Tests pin:

  - Dry-run never calls execute
  - Apply only writes the two UPDATE statements (no other side effects)
  - Stale detection: rows whose last_seen_in_sync_at is older than
    (last_full_sync_at - grace) get marked
  - Legacy rows (last_seen NULL) tombstoned only when created_at is
    older than the stale threshold (no false positives on new rows)
  - Archive transition only when sync_status='stale' AND updated_at
    older than archive cutoff
  - external_seed merchant excluded by the SELECT (last_full_sync IS NULL
    naturally; double-checked by != 'external_seed' filter)
  - --merchant-id scopes the sweep
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import sweep_stale_catalog_products as sweep  # noqa: E402


# ---------------------------------------------------------------------------
# SQL shape pins — same-cost guard against drift
# ---------------------------------------------------------------------------


def test_select_merchants_excludes_external_seed_and_nulls() -> None:
    """Two filters: last_full_sync_at IS NOT NULL (naturally excludes
    external_seed) + merchant_id != 'external_seed' (defense in
    depth)."""
    sql = sweep.SELECT_MERCHANTS_SQL
    assert "last_full_sync_at IS NOT NULL" in sql
    assert "merchant_id != 'external_seed'" in sql


def test_find_stale_sql_only_targets_live_rows() -> None:
    """The sweep must never touch rows that are already stale or
    archived — those have their own transition rules."""
    sql = sweep.FIND_STALE_SQL
    assert "sync_status = 'live'" in sql
    # Legacy fallback (last_seen NULL): use created_at as activity
    # signal so we don't immediately tombstone fresh writes that
    # predate the column.
    assert "last_seen_in_sync_at IS NULL" in sql
    assert "created_at < :stale_before" in sql


def test_find_archive_sql_only_targets_stale_rows() -> None:
    """Archive transition is stale → archived. Never live → archived."""
    sql = sweep.FIND_ARCHIVE_SQL
    assert "sync_status = 'stale'" in sql
    assert "updated_at < :archive_before" in sql


def test_update_sql_idempotency_guards() -> None:
    """UPDATE statements include guard predicates so a re-run is a
    no-op (status only flips from live→stale and stale→archived
    once)."""
    assert "sync_status = 'live'" in sweep.UPDATE_TO_STALE_SQL
    assert "sync_status = 'stale'" in sweep.UPDATE_TO_ARCHIVED_SQL


# ---------------------------------------------------------------------------
# _sweep_merchant — per-merchant orchestration
# ---------------------------------------------------------------------------


def _ns(**kw) -> SimpleNamespace:
    base = {
        "apply": False, "merchant_id": None,
        "grace_hours": 24, "archive_days": 7,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_now() -> dt.datetime:
    return dt.datetime(2026, 5, 12, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.mark.asyncio
async def test_sweep_merchant_skipped_on_null_sync_timestamp(monkeypatch) -> None:
    """If last_full_sync_at is None, the merchant hasn't synced yet
    (e.g., a freshly-onboarded Shopify store mid-OAuth). Skip without
    error."""
    async def fail_fetch_all(*args, **kwargs):
        raise AssertionError("should not query")

    monkeypatch.setattr(sweep.database, "fetch_all", fail_fetch_all)
    out = await sweep._sweep_merchant(
        merchant_id="merch_x", last_full_sync_at=None,
        grace_hours=24, archive_days=7, apply=True,
    )
    assert out["skipped_reason"] == "no_sync_yet"


@pytest.mark.asyncio
async def test_sweep_merchant_dry_run_does_not_execute_updates(monkeypatch) -> None:
    """Dry-run reports counts only — no writes."""
    last_sync = _fake_now()
    stale_rows = [
        {"product_key": "p1", "last_seen_in_sync_at": last_sync - dt.timedelta(hours=72),
         "sync_status": "live", "created_at": last_sync - dt.timedelta(days=30)},
        {"product_key": "p2", "last_seen_in_sync_at": None,
         "sync_status": "live", "created_at": last_sync - dt.timedelta(days=30)},
    ]
    archive_rows = [
        {"product_key": "p3", "updated_at": last_sync - dt.timedelta(days=10)},
    ]
    execute_calls: List[Any] = []

    async def fake_fetch_all(sql, params):
        if "FROM catalog_products" in sql and "sync_status = 'live'" in sql:
            return stale_rows
        if "sync_status = 'stale'" in sql:
            return archive_rows
        return []

    async def fake_execute(*args, **kwargs):
        execute_calls.append(args)

    monkeypatch.setattr(sweep.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(sweep.database, "execute", fake_execute)

    out = await sweep._sweep_merchant(
        merchant_id="merch_moyu", last_full_sync_at=last_sync,
        grace_hours=24, archive_days=7, apply=False,
    )
    assert execute_calls == []
    assert out["marked_stale"] == 2
    assert out["marked_archived"] == 1
    assert len(out["samples_stale"]) == 2
    assert len(out["samples_archive"]) == 1


@pytest.mark.asyncio
async def test_sweep_merchant_apply_writes_two_update_statements(monkeypatch) -> None:
    """Apply path: one UPDATE per stale candidate (live→stale) and one
    per archive candidate (stale→archived). No other DDL/DML."""
    last_sync = _fake_now()
    stale_rows = [
        {"product_key": "p_stale", "last_seen_in_sync_at": last_sync - dt.timedelta(hours=72),
         "sync_status": "live", "created_at": last_sync - dt.timedelta(days=30)},
    ]
    archive_rows = [
        {"product_key": "p_archive", "updated_at": last_sync - dt.timedelta(days=10)},
    ]
    executed: List[Dict[str, Any]] = []

    async def fake_fetch_all(sql, params):
        if "sync_status = 'live'" in sql:
            return stale_rows
        if "sync_status = 'stale'" in sql:
            return archive_rows
        return []

    async def fake_execute(sql, params):
        executed.append({"sql": str(sql), "params": params})
        return 1

    monkeypatch.setattr(sweep.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(sweep.database, "execute", fake_execute)

    out = await sweep._sweep_merchant(
        merchant_id="merch_moyu", last_full_sync_at=last_sync,
        grace_hours=24, archive_days=7, apply=True,
    )
    assert out["marked_stale"] == 1
    assert out["marked_archived"] == 1
    assert len(executed) == 2
    sqls = "\n".join(e["sql"] for e in executed)
    assert "SET sync_status = 'stale'" in sqls
    assert "SET sync_status = 'archived'" in sqls
    # No INSERT, no DELETE, no DROP, no touching other tables
    assert "INSERT" not in sqls
    assert "DELETE" not in sqls
    assert "DROP" not in sqls
    assert "catalog_offers" not in sqls
    assert "external_product_seeds" not in sqls
    assert "seed_data" not in sqls


@pytest.mark.asyncio
async def test_sweep_merchant_grace_hours_changes_stale_threshold(monkeypatch) -> None:
    """grace_hours=48 → stale cutoff is 48h before last_full_sync_at,
    not 24. Confirms the threshold is wired through."""
    last_sync = _fake_now()
    received_params: List[Dict[str, Any]] = []

    async def fake_fetch_all(sql, params):
        received_params.append(dict(params))
        return []

    async def fake_execute(*args, **kwargs):
        return None

    monkeypatch.setattr(sweep.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(sweep.database, "execute", fake_execute)

    await sweep._sweep_merchant(
        merchant_id="m", last_full_sync_at=last_sync,
        grace_hours=48, archive_days=7, apply=False,
    )
    stale_before = received_params[0]["stale_before"]
    assert stale_before == last_sync - dt.timedelta(hours=48)


# ---------------------------------------------------------------------------
# _drive — full orchestration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drive_iterates_all_eligible_merchants(monkeypatch) -> None:
    """No --merchant-id: sweep every merchant the SELECT returns."""
    last_sync = _fake_now()
    merchants = [
        {"merchant_id": "merch_a", "last_full_sync_at": last_sync},
        {"merchant_id": "merch_b", "last_full_sync_at": last_sync},
    ]

    async def fake_fetch_all(sql, params):
        if "FROM catalog_merchants" in sql:
            return merchants
        return []  # no rows to tombstone

    async def fake_execute(*args, **kwargs):
        return None

    monkeypatch.setattr(sweep.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(sweep.database, "execute", fake_execute)
    monkeypatch.setattr(sweep.database, "is_connected", True)

    out = await sweep._drive(_ns(apply=False))
    assert out["totals"]["merchants_swept"] == 2
    merchant_ids = {p["merchant_id"] for p in out["per_merchant"]}
    assert merchant_ids == {"merch_a", "merch_b"}


@pytest.mark.asyncio
async def test_drive_scopes_to_single_merchant_when_specified(monkeypatch) -> None:
    """--merchant-id targets one merchant. Confirms the scoped SELECT
    is used instead of the all-merchants one."""
    last_sync = _fake_now()
    captured: List[Dict[str, Any]] = []

    async def fake_fetch_all(sql, params):
        captured.append({"sql": str(sql), "params": dict(params)})
        if "WHERE merchant_id = :m" in sql:
            return [{"merchant_id": "merch_moyu", "last_full_sync_at": last_sync}]
        return []

    async def fake_execute(*args, **kwargs):
        return None

    monkeypatch.setattr(sweep.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(sweep.database, "execute", fake_execute)
    monkeypatch.setattr(sweep.database, "is_connected", True)

    out = await sweep._drive(_ns(merchant_id="merch_moyu"))
    assert out["totals"]["merchants_swept"] == 1
    # First SELECT was the scoped one
    assert any("WHERE merchant_id = :m" in c["sql"] for c in captured)
    # All-merchants SELECT (no WHERE merchant_id = :m) was NOT used
    assert not any(
        "WHERE last_full_sync_at IS NOT NULL" in c["sql"]
        and "AND merchant_id != 'external_seed'" in c["sql"]
        for c in captured
    )
