from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import ingest_crawled_inci as ing
from services import crawled_inci_ingest as csi


def test_merchant_id_from_product_key():
    # Pure string helper only (re-exported for back-compat/tests); it returns the
    # HISTORICAL/bucket token and must NOT be used to write a seller (ADR-009 D2).
    assert ing._merchant_id("prod::external_seed::external_seed::ext_x") == "external_seed"
    assert ing._merchant_id("prod::merch_abc::shopify::123") == "merch_abc"


class _RoutedDB:
    """fetch_one routes by table: catalog_products -> authoritative seller;
    beauty_sku_ingredients -> existing INCI source."""
    is_connected = True

    def __init__(self, *, catalog_merchant, existing_source=None):
        self._catalog_merchant = catalog_merchant
        self._existing_source = existing_source
        self.executed = []

    async def execute(self, q, p=None):
        self.executed.append(p)

    async def fetch_one(self, q, p=None):
        if "FROM catalog_products" in q:
            return {"merchant_id": self._catalog_merchant} if self._catalog_merchant else None
        if "FROM beauty_sku_ingredients" in q:
            return {"source_system": self._existing_source} if self._existing_source else None
        return None


def test_drive_upserts_and_enriches_each_item(monkeypatch):
    executed = []
    enriched = []

    async def _fake_persist(pk, *, db=None, dry_run=False):
        enriched.append(pk)
        return {"derived": {"active_source": "inci", "substantiated_claims": ["Contains Niacinamide"]},
                "written": {"actives_skus": [pk + "::s"], "evidence_claims": True}}

    # _drive delegates to the shared service; patch where enrich is actually called.
    monkeypatch.setattr(csi, "enrich_and_persist_product", _fake_persist)

    db = _RoutedDB(catalog_merchant="merch_obs_deadbeefdeadbeef")
    items = [
        {"product_key": "prod::external_seed::external_seed::ext_1", "sku_key": "sk1", "raw_inci": "Water, Niacinamide"},
        {"product_key": "prod::external_seed::external_seed::ext_2", "sku_key": "sk2", "raw_inci": ""},  # skipped
        {"product_key": "prod::external_seed::external_seed::ext_3", "sku_key": "sk3", "raw_inci": "NO INGREDIENT LIST FOUND"},  # skipped
    ]
    report = asyncio.run(ing._drive(items, dry_run=False, db=db))
    executed.extend(db.executed)

    assert report["n"] == 3
    assert report["inci_written"] == 1
    assert report["actives_filled"] == 1
    assert report["claims_written"] == 1
    assert report["skipped"] == 2
    assert enriched == ["prod::external_seed::external_seed::ext_1"]  # only the real-INCI item
    # LEAK CLOSED: the authoritative catalog seller is written, not the bucket token.
    assert executed and executed[0]["mid"] == "merch_obs_deadbeefdeadbeef"


def test_dry_run_writes_nothing(monkeypatch):
    async def _fake_persist(pk, *, db=None, dry_run=False):
        assert dry_run is True
        return {"derived": {"active_source": "inci", "substantiated_claims": []},
                "written": {"actives_skus": [], "evidence_claims": False}}

    # _drive delegates to the shared service; patch where enrich is actually called.
    monkeypatch.setattr(csi, "enrich_and_persist_product", _fake_persist)

    db = _RoutedDB(catalog_merchant="merch_obs_deadbeefdeadbeef")
    items = [{"product_key": "prod::external_seed::external_seed::ext_1", "sku_key": "sk1", "raw_inci": "Water, Niacinamide"}]
    report = asyncio.run(ing._drive(items, dry_run=True, db=db))
    assert db.executed == []  # no UPSERT under dry_run
    assert report["inci_written"] == 0
