"""Tests for scripts/resnapshot_quality_bulk.py (Fix Plan G — T2 full cohort).

Pins: live-non-demo cohort keyset scan, append-only set-based INSERT tagged
model_version=structural_depth.g1, dry-run inserts nothing, distribution report.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from scripts import resnapshot_quality_bulk as rb


def _ns(**kw) -> SimpleNamespace:
    base = {"dry_run": False, "batch_size": 500, "max_batches": 0,
            "start_after": "", "max_retries": 3, "retry_base_delay": 0.0}
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeDB:
    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = sorted(rows, key=lambda r: r["product_key"])
        self.is_connected = True
        self.inserts: List[Dict[str, Any]] = []

    async def fetch_all(self, sql, params):
        cursor = params["cursor"]
        return [r for r in self.rows if r["product_key"] > cursor][: params["batch_size"]]

    async def execute(self, sql, params):
        self.inserts.append({"sql": str(sql), "params": dict(params)})
        return 1


def _row(pk: str, **over) -> Dict[str, Any]:
    base = {
        "product_key": pk, "merchant_id": "m", "platform": "external_seed",
        "source_product_id": f"pid_{pk}", "title": "Snail Essence 100ml",
        "description": "x" * 80, "product_type": "Essence", "category": "Skincare",
        "category_path": "beauty/skincare/essence", "brand": "B",
        "image_url": "https://img/1.jpg", "product_payload": {"price": 20.0},
        "resolved_vertical": "beauty",
        "llm_attributes": {"schema_version": "structural_depth.beauty.v1",
                           "attributes": {"volume": "100 ml", "texture": "watery"}},
    }
    base.update(over)
    return base


def test_cohort_sql_live_nondemo_keyset_no_llm_guard():
    sql = rb._SELECT_SQL
    assert "suppression_reason IS NULL" in sql
    assert "pivota-review-demo%" in sql
    assert "cp.merchant_id <> ALL(:demo_merchants)" in sql
    assert "cp.product_key > :cursor" in sql
    # full-cohort re-snapshot: NOT restricted to un-enriched rows
    assert "llm_attributes IS NULL" not in sql


def test_insert_sql_append_only_and_tagged():
    sql = rb._INSERT_BATCH_SQL
    assert "INSERT INTO product_quality_snapshot" in sql
    assert "structural_depth.g1" in sql
    assert "unnest(" in sql.lower()
    assert "UPDATE" not in sql.upper().replace("INSERT", "")
    assert "DELETE" not in sql.upper()


@pytest.mark.asyncio
async def test_apply_inserts_batchwise_and_reports_distribution():
    db = _FakeDB([_row(f"p{i}") for i in range(5)])
    report = await rb._drive(_ns(batch_size=2), db=db)
    assert report["scanned"] == 5
    assert report["inserted"] == 5
    assert len(db.inserts) == 3  # 2+2+1 set-based statements
    assert report["readiness_distribution"]["n"] == 5
    assert report["readiness_distribution"]["avg"] > 50.0  # structured rows
    assert report["readiness_gt0"] == 5


@pytest.mark.asyncio
async def test_dry_run_inserts_nothing():
    db = _FakeDB([_row("p1")])
    report = await rb._drive(_ns(dry_run=True), db=db)
    assert db.inserts == []
    assert report["inserted"] == 0
    assert report["readiness_distribution"]["n"] == 1


@pytest.mark.asyncio
async def test_start_after_resumes_keyset():
    db = _FakeDB([_row("p1"), _row("p2"), _row("p3")])
    report = await rb._drive(_ns(start_after="p1"), db=db)
    assert report["scanned"] == 2
