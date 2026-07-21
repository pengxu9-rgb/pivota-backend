"""ADR-011 step 4 — catalog_products.gtin backfill from catalog_skus.barcode.

Verifies the backfill: modal GTIN-14 pick (via the serving view's pick_gtin13),
canonicalization, the gtin-IS-NULL skip, dry-run vs apply, and honest absence
when there's no usable barcode.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List

import pytest

import scripts.backfill_catalog_products_gtin as bf  # noqa: E402


class FakeDatabase:
    is_connected = True

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self.updates: List[Dict[str, Any]] = []

    async def fetch_all(self, sql: Any, params: Any = None) -> List[Dict[str, Any]]:
        return self._rows

    async def execute(self, sql: Any, params: Any = None) -> int:
        self.updates.append(params)
        return 1

    async def connect(self) -> None:
        return None


def _args(apply: bool) -> argparse.Namespace:
    return argparse.Namespace(apply=apply, limit=200, offset=0)


def _install(monkeypatch: pytest.MonkeyPatch, rows: List[Dict[str, Any]]) -> FakeDatabase:
    # Swap the NAME the script binds (scripts.backfill_catalog_products_gtin.
    # database) rather than the shared db.database.database singleton — this
    # module is the only consumer of that name, so no collaborator binding is
    # poisoned (the db-singleton gotcha applies only to the shared object).
    fake = FakeDatabase(rows)
    monkeypatch.setattr(bf, "database", fake)
    return fake


@pytest.mark.asyncio
async def test_apply_writes_modal_canonical_gtin(monkeypatch):
    rows = [
        # Two SKUs, one barcode repeated → modal wins; 13-digit → padded to 14.
        {"product_key": "prod::m::p::1", "gtin": None,
         "barcodes": ["8809640733458", "8809640733458", "0000000000017"]},
    ]
    fake = _install(monkeypatch, rows)
    report = await bf._drive(_args(apply=True))
    assert report["outcome_counts"]["gtin_computed"] == 1
    assert report["outcome_counts"]["updated"] == 1
    assert fake.updates == [{"product_key": "prod::m::p::1", "gtin": "08809640733458"}]


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(monkeypatch):
    rows = [{"product_key": "prod::m::p::1", "gtin": None, "barcodes": ["8809640733458"]}]
    fake = _install(monkeypatch, rows)
    report = await bf._drive(_args(apply=False))
    assert report["outcome_counts"]["gtin_computed"] == 1
    assert report["outcome_counts"]["skipped_no_op_in_dry_run"] == 1
    assert report["outcome_counts"]["updated"] == 0
    assert fake.updates == []


@pytest.mark.asyncio
async def test_skips_rows_already_carrying_gtin(monkeypatch):
    rows = [{"product_key": "prod::m::p::1", "gtin": "08809640733458",
             "barcodes": ["1111111111116"]}]
    fake = _install(monkeypatch, rows)
    report = await bf._drive(_args(apply=True))
    assert report["outcome_counts"]["skipped_already_gtin"] == 1
    assert fake.updates == []  # never overwrite a door-written value


@pytest.mark.asyncio
async def test_no_barcode_stays_null(monkeypatch):
    rows = [
        {"product_key": "prod::m::p::1", "gtin": None, "barcodes": []},
        # malformed 16-digit → pick_gtin13 drops it → honest absence
        {"product_key": "prod::m::p::2", "gtin": None, "barcodes": ["1234567890123456"]},
    ]
    fake = _install(monkeypatch, rows)
    report = await bf._drive(_args(apply=True))
    assert report["outcome_counts"]["no_barcode_or_malformed"] == 2
    assert report["outcome_counts"]["updated"] == 0
    assert fake.updates == []
