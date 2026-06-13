from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services import canonical_inci_intake as intake


class FakeDB:
    def __init__(self, cp=None, sku_rows=None, catalog_skus=None):
        self.cp = cp
        self.sku_rows = sku_rows or []
        self.catalog_skus = catalog_skus or []
        self.executed = []

    async def fetch_one(self, query, params=None):
        if "FROM catalog_products" in query:
            return dict(self.cp) if self.cp else None
        return None

    async def fetch_all(self, query, params=None):
        if "FROM beauty_sku_ingredients" in query:
            return [dict(r) for r in self.sku_rows]
        if "FROM catalog_skus" in query:
            return [dict(r) for r in self.catalog_skus]
        return []

    async def execute(self, query, params=None):
        self.executed.append((query, params))


def _patch_recompute(monkeypatch, calls):
    async def _fake(content_key, *, reason=None):
        calls.append((content_key, reason))
        return True
    monkeypatch.setattr(intake, "recompute_serving_eligibility", _fake)


_CP = {"content_key": "ck1", "merchant_id": "merch_test"}
_INCI = "Aqua, Niacinamide, Sodium Hyaluronate, Centella Asiatica Extract, Butylene Glycol"


# --- pure precedence / validation ---------------------------------------------

def test_source_rank_ladder():
    assert intake.source_rank("brand_official") == 3
    assert intake.source_rank("supplier_input") == 2
    assert intake.source_rank("merchant_authored") == 2
    assert intake.source_rank("reseller_listing") == 1
    assert intake.source_rank("shopify_products_sync") == 1
    assert intake.source_rank(None) == 0


def test_may_write_never_downgrades():
    assert intake.may_write("brand_official", "shopify_products_sync") is True
    assert intake.may_write("supplier_input", "reseller_listing") is True
    assert intake.may_write("supplier_input", "supplier_input") is True  # refresh
    assert intake.may_write("reseller_listing", "brand_official") is False  # downgrade blocked
    assert intake.may_write("reseller_listing", "merchant_authored") is False


def test_is_valid_inci():
    assert intake.is_valid_inci("Aqua, Niacinamide") is True
    assert intake.is_valid_inci("Niacinamide") is False  # single token
    assert intake.is_valid_inci(None) is False


# --- ingest -------------------------------------------------------------------

def test_ingest_writes_verified_actives_and_recomputes(monkeypatch):
    calls = []
    _patch_recompute(monkeypatch, calls)
    db = FakeDB(cp=_CP, sku_rows=[{"sku_key": "s1", "source_system": "shopify_products_sync"}])
    res = asyncio.run(intake.ingest_canonical_inci("pk1", _INCI, intake.INCI_SOURCE_BRAND_OFFICIAL, db=db))

    assert res["status"] == "ok"
    assert res["written_skus"] == ["s1"]
    assert "Niacinamide" in res["verified_actives"]
    assert "Hyaluronic Acid" in res["verified_actives"]  # from Sodium Hyaluronate
    assert res["serving_eligible"] is True
    assert calls == [("ck1", "canonical_inci_intake")]
    # The written active_ingredients carry source="inci" (verified).
    q, p = next((q, p) for q, p in db.executed if "INSERT INTO beauty_sku_ingredients" in q)
    assert p["mid"] == "merch_test"
    assert all(a["source"] == "inci" for a in json.loads(p["act"]))


def test_ingest_does_not_downgrade_brand_official(monkeypatch):
    calls = []
    _patch_recompute(monkeypatch, calls)
    db = FakeDB(cp=_CP, sku_rows=[{"sku_key": "s1", "source_system": "brand_official"}])
    res = asyncio.run(intake.ingest_canonical_inci("pk1", _INCI, intake.INCI_SOURCE_RESELLER, db=db))

    assert res["written_skus"] == []
    assert res["skipped_outranked_skus"] == ["s1"]
    assert res["serving_eligible"] is None  # nothing written
    assert db.executed == []


def test_ingest_attaches_to_catalog_sku_when_no_ingredient_row(monkeypatch):
    _patch_recompute(monkeypatch, [])
    db = FakeDB(cp=_CP, sku_rows=[], catalog_skus=[{"sku_key": "cs1"}])
    res = asyncio.run(intake.ingest_canonical_inci("pk1", _INCI, intake.INCI_SOURCE_SUPPLIER, db=db))
    assert res["written_skus"] == ["cs1"]
    assert any("INSERT INTO beauty_sku_ingredients" in q for q, _ in db.executed)


def test_ingest_rejects_non_inci_and_missing_product(monkeypatch):
    _patch_recompute(monkeypatch, [])
    assert asyncio.run(
        intake.ingest_canonical_inci("pk1", "just a sentence", "brand_official", db=FakeDB(cp=_CP))
    )["status"] == "rejected_not_inci"
    assert asyncio.run(
        intake.ingest_canonical_inci("ghost", _INCI, "brand_official", db=FakeDB(cp=None))
    )["status"] == "not_found"


def test_ingest_dry_run_writes_nothing(monkeypatch):
    calls = []
    _patch_recompute(monkeypatch, calls)
    db = FakeDB(cp=_CP, sku_rows=[{"sku_key": "s1", "source_system": "reseller_listing"}])
    res = asyncio.run(
        intake.ingest_canonical_inci("pk1", _INCI, "brand_official", db=db, dry_run=True)
    )
    assert res["dry_run"] is True
    assert res["written_skus"] == ["s1"]  # would write
    assert db.executed == [] and calls == []  # but wrote nothing
