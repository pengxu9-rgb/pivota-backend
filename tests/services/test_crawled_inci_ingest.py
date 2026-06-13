from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services import crawled_inci_ingest as csi


def test_merchant_id_from_product_key():
    assert csi.merchant_id_from_product_key("prod::external_seed::external_seed::x") == "external_seed"
    assert csi.merchant_id_from_product_key("prod::merch_abc::shopify::1") == "merch_abc"
    assert csi.merchant_id_from_product_key("") == "external_seed"


def test_ingest_upserts_with_source_system_and_enriches(monkeypatch):
    executed = []
    enriched = []

    async def _fake_persist(pk, *, db=None, dry_run=False):
        enriched.append(pk)
        return {"derived": {"active_source": "inci", "substantiated_claims": ["Contains Niacinamide"]},
                "written": {"actives_skus": [pk + "::s"], "evidence_claims": True}}

    monkeypatch.setattr(csi, "enrich_and_persist_product", _fake_persist)

    class FakeDB:
        is_connected = True
        async def execute(self, q, p=None):
            executed.append(p)

    items = [
        {"product_key": "prod::external_seed::external_seed::a", "sku_key": "a::canonical", "raw_inci": "Water, Niacinamide"},
        {"product_key": "prod::external_seed::external_seed::b", "sku_key": "b::canonical", "raw_inci": ""},  # skipped
        {"product_key": "prod::external_seed::external_seed::c", "sku_key": "c::canonical", "raw_inci": "NO INGREDIENT LIST"},  # skipped
    ]
    report = asyncio.run(csi.ingest_crawled_inci_items(
        items, source_system="external_brand_crawl", dry_run=False, db=FakeDB()))

    assert report["n"] == 3
    assert report["inci_written"] == 1
    assert report["skipped"] == 2
    assert report["actives_filled"] == 1
    assert report["claims_written"] == 1
    assert enriched == ["prod::external_seed::external_seed::a"]
    # the source_system param flows into the UPSERT bind (default is 'pdp_crawl')
    assert executed[0]["src"] == "external_brand_crawl"
    assert executed[0]["mid"] == "external_seed"


def test_ingest_dry_run_writes_nothing(monkeypatch):
    async def _fake_persist(pk, *, db=None, dry_run=False):
        assert dry_run is True
        return {"derived": {}, "written": {}}

    monkeypatch.setattr(csi, "enrich_and_persist_product", _fake_persist)
    executed = []

    class FakeDB:
        is_connected = True
        async def execute(self, q, p=None):
            executed.append(p)

    items = [{"product_key": "prod::external_seed::external_seed::a", "sku_key": "a::canonical", "raw_inci": "Water"}]
    report = asyncio.run(csi.ingest_crawled_inci_items(items, dry_run=True, db=FakeDB()))
    assert executed == []
    assert report["inci_written"] == 0
