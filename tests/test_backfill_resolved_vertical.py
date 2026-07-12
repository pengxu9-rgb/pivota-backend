"""Tests for scripts/backfill_resolved_vertical.py (Fix Plan B — T2).

The backfill heals catalog_products rows whose resolved_vertical is NULL (the
~83% external-seed-mirror cohort that never passed through the resolver). Tests
pin:
  - SELECT filters NULL rows + keyset-paginates on product_key (resumable)
  - UPDATE writes ONLY resolved_vertical, guarded IS NULL (idempotent, single-col)
  - --dry-run performs zero writes
  - --apply writes one UPDATE per row with the resolver's value
  - batching stops when a batch returns no rows; keyset cursor advances
  - NULL-signal rows still get a non-NULL value ('other') — drives NULL -> 0
  - report carries distribution + other_share + samples
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import backfill_resolved_vertical as backfill  # noqa: E402


def _ns(**kw) -> SimpleNamespace:
    base = {
        "dry_run": False,
        "batch_size": 500,
        "max_batches": 0,
        "sample_size": 25,
        "max_retries": 5,
        "retry_base_delay": 0.0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeDB:
    """Minimal fake honoring the keyset SELECT + guarded UPDATE contract.

    Holds a list of catalog_products-shaped rows; fetch_all applies the
    `resolved_vertical IS NULL AND product_key > cursor` window and the LIMIT;
    execute applies the single-column guarded UPDATE in place. Records every SQL
    for source-contract assertions.
    """

    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = {r["product_key"]: dict(r) for r in rows}
        self.is_connected = True
        self.selects: List[Dict[str, Any]] = []
        self.updates: List[Dict[str, Any]] = []

    async def connect(self):  # pragma: no cover - not exercised (is_connected True)
        return None

    async def disconnect(self):  # pragma: no cover
        return None

    async def fetch_all(self, sql, params):
        self.selects.append({"sql": str(sql), "params": dict(params)})
        cursor = params["cursor"]
        batch_size = params["batch_size"]
        eligible = [
            r for r in self.rows.values()
            if r.get("resolved_vertical") is None and r["product_key"] > cursor
        ]
        eligible.sort(key=lambda r: r["product_key"])
        return eligible[:batch_size]

    async def execute(self, sql, params):
        self.updates.append({"sql": str(sql), "params": dict(params)})
        if "keys" in params and "vals" in params:
            # Set-based batch UPDATE via unnest(keys, vals).
            for pk, rv in zip(params["keys"], params["vals"]):
                self._apply_update(pk, rv)
        else:
            self._apply_update(params["product_key"], params["resolved_vertical"])
        return 1

    def _apply_update(self, pk, rv):
        row = self.rows.get(pk)
        # Guarded IS NULL: only writes when currently NULL.
        if row is not None and row.get("resolved_vertical") is None:
            row["resolved_vertical"] = rv


# ---------------------------------------------------------------------------
# SQL contract (source-level) — resumable + single-column + guarded
# ---------------------------------------------------------------------------


def test_select_sql_is_null_filtered_and_keyset_paginated() -> None:
    assert "resolved_vertical IS NULL" in backfill._SELECT_SQL
    assert "product_key > :cursor" in backfill._SELECT_SQL
    assert "ORDER BY product_key ASC" in backfill._SELECT_SQL
    assert "LIMIT :batch_size" in backfill._SELECT_SQL


def test_update_sql_writes_only_resolved_vertical_and_guards_null() -> None:
    sql = backfill._UPDATE_SQL
    assert "UPDATE catalog_products" in sql
    assert "SET resolved_vertical = :resolved_vertical" in sql
    assert "resolved_vertical IS NULL" in sql
    assert "WHERE product_key = :product_key" in sql
    _assert_single_column(sql)


def test_batch_update_sql_writes_only_resolved_vertical_and_guards_null() -> None:
    """The set-based batch UPDATE (the fast path actually used) must also touch
    ONLY resolved_vertical and keep the IS NULL idempotency guard."""
    sql = backfill._UPDATE_BATCH_SQL
    assert "UPDATE catalog_products" in sql
    assert "SET resolved_vertical = v.rv" in sql
    assert "unnest(" in sql.lower()
    assert "c.resolved_vertical IS NULL" in sql
    _assert_single_column(sql)


def _assert_single_column(sql: str) -> None:
    """Single-column backfill hard constraint: no other column is written."""
    for forbidden in ("category", "seed_data", "product_payload", "product_type", "tags"):
        assert f"SET {forbidden}" not in sql
        assert f", {forbidden} =" not in sql


# ---------------------------------------------------------------------------
# dry-run vs apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_performs_no_writes() -> None:
    db = _FakeDB([
        {"product_key": "p1", "resolved_vertical": None, "category": "Skincare",
         "product_type": None, "category_path": None, "title": "Snail Essence",
         "description": None, "tags": None},
        {"product_key": "p2", "resolved_vertical": None, "category": None,
         "product_type": "Wireless Earbuds", "category_path": None,
         "title": "ANC Buds", "description": None, "tags": None},
    ])
    report = await backfill._drive(_ns(dry_run=True), db=db)
    assert db.updates == []
    assert report["written"] == 0
    assert report["scanned_null_vertical"] == 2
    assert report["distribution"]["beauty"] == 1
    assert report["distribution"]["electronics"] == 1
    # rows unchanged in the fake store
    assert all(r["resolved_vertical"] is None for r in db.rows.values())


@pytest.mark.asyncio
async def test_apply_writes_one_update_per_row_with_resolver_value() -> None:
    db = _FakeDB([
        {"product_key": "p1", "resolved_vertical": None, "category": "Skincare",
         "product_type": None, "category_path": None, "title": "Snail Essence",
         "description": None, "tags": None},
        {"product_key": "p2", "resolved_vertical": None, "category": None,
         "product_type": "Wireless Earbuds", "category_path": None,
         "title": "ANC Buds", "description": None, "tags": None},
    ])
    report = await backfill._drive(_ns(), db=db)
    assert report["written"] == 2
    assert db.rows["p1"]["resolved_vertical"] == "beauty"
    assert db.rows["p2"]["resolved_vertical"] == "electronics"
    # One set-based batch execute covers both rows (not one round-trip per row).
    assert len(db.updates) == 1
    assert set(db.updates[0]["params"]["keys"]) == {"p1", "p2"}


@pytest.mark.asyncio
async def test_null_signal_rows_still_get_non_null_other() -> None:
    """The whole point: a row with NO category/product_type/category_path/title
    still receives a non-NULL value ('other'), so a full pass drives the NULL
    count to zero rather than leaving structureless rows NULL."""
    db = _FakeDB([
        {"product_key": "p1", "resolved_vertical": None, "category": None,
         "product_type": None, "category_path": None, "title": None,
         "description": None, "tags": None},
    ])
    report = await backfill._drive(_ns(), db=db)
    assert db.rows["p1"]["resolved_vertical"] == "other"
    assert report["distribution"]["other"] == 1
    assert report["written"] == 1
    # No NULLs remain.
    assert all(r["resolved_vertical"] is not None for r in db.rows.values())


# ---------------------------------------------------------------------------
# batching + keyset resumability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batches_until_no_rows_remain() -> None:
    rows = [
        {"product_key": f"p{i:03d}", "resolved_vertical": None, "category": "Skincare",
         "product_type": None, "category_path": None, "title": None,
         "description": None, "tags": None}
        for i in range(5)
    ]
    db = _FakeDB(rows)
    report = await backfill._drive(_ns(batch_size=2), db=db)
    assert report["scanned_null_vertical"] == 5
    assert report["written"] == 5
    assert report["batches_processed"] == 3  # 2 + 2 + 1
    # Keyset cursor advanced monotonically across the selects.
    cursors = [s["params"]["cursor"] for s in db.selects]
    assert cursors == sorted(cursors)
    assert cursors[0] == ""  # first batch starts at the empty sentinel


@pytest.mark.asyncio
async def test_max_batches_stops_early_and_is_resumable() -> None:
    """--max-batches caps a run; re-invoking resumes because processed rows are
    now non-NULL and drop out of the IS NULL filter."""
    rows = [
        {"product_key": f"p{i:03d}", "resolved_vertical": None, "category": "Skincare",
         "product_type": None, "category_path": None, "title": None,
         "description": None, "tags": None}
        for i in range(6)
    ]
    db = _FakeDB(rows)
    first = await backfill._drive(_ns(batch_size=2, max_batches=1), db=db)
    assert first["written"] == 2
    remaining_null = [r for r in db.rows.values() if r["resolved_vertical"] is None]
    assert len(remaining_null) == 4
    # Resume: a fresh drive picks up exactly the remaining NULL rows.
    second = await backfill._drive(_ns(batch_size=2), db=db)
    assert second["written"] == 4
    assert all(r["resolved_vertical"] is not None for r in db.rows.values())


# ---------------------------------------------------------------------------
# report content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_carries_distribution_other_share_and_samples() -> None:
    rows = [
        {"product_key": "p1", "resolved_vertical": None, "category": "Skincare",
         "product_type": None, "category_path": None, "title": None,
         "description": None, "tags": None},
        {"product_key": "p2", "resolved_vertical": None, "category": None,
         "product_type": None, "category_path": None, "title": None,
         "description": None, "tags": None},  # -> other
    ]
    db = _FakeDB(rows)
    report = await backfill._drive(_ns(sample_size=1), db=db)
    assert report["distribution"] == {"beauty": 1, "fashion": 0, "electronics": 0, "other": 1}
    assert abs(report["other_share"] - 0.5) < 1e-9
    assert len(report["samples"]) == 1
    assert set(report["samples"][0]) >= {
        "product_key", "title", "resolved_vertical", "category", "category_path",
    }


@pytest.mark.asyncio
async def test_tags_jsonb_string_folds_into_title_signal() -> None:
    """tags may arrive as a JSON string; it must still fold into the title blob
    so a SKU whose only vertical signal is in its tags resolves."""
    db = _FakeDB([
        {"product_key": "p1", "resolved_vertical": None, "category": None,
         "product_type": None, "category_path": None, "title": "Mystery Box",
         "description": None, "tags": '["collagen", "supplement"]'},
    ])
    await backfill._drive(_ns(), db=db)
    assert db.rows["p1"]["resolved_vertical"] == "beauty"


# ---------------------------------------------------------------------------
# resolver integration (source-level) — must reuse the shared resolver
# ---------------------------------------------------------------------------


def test_backfill_uses_shared_resolve_vertical() -> None:
    src = (Path(__file__).resolve().parents[1] / "scripts" / "backfill_resolved_vertical.py").read_text()
    assert "from services.vertical_profiles import resolve_vertical" in src
    assert "resolve_vertical(" in src
