from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import ingest_crawled_inci as ing


def test_merchant_id_from_product_key():
    assert ing._merchant_id("prod::external_seed::external_seed::ext_x") == "external_seed"
    assert ing._merchant_id("prod::merch_abc::shopify::123") == "merch_abc"


def test_drive_upserts_and_enriches_each_item(monkeypatch):
    executed = []
    enriched = []

    async def _fake_persist(pk, *, db=None, dry_run=False):
        enriched.append(pk)
        return {"derived": {"active_source": "inci", "substantiated_claims": ["Contains Niacinamide"]},
                "written": {"actives_skus": [pk + "::s"], "evidence_claims": True}}

    monkeypatch.setattr(ing, "enrich_and_persist_product", _fake_persist)

    class FakeDB:
        is_connected = True
        async def execute(self, q, p=None): executed.append(p)

    items = [
        {"product_key": "prod::external_seed::external_seed::ext_1", "sku_key": "sk1", "raw_inci": "Water, Niacinamide"},
        {"product_key": "prod::external_seed::external_seed::ext_2", "sku_key": "sk2", "raw_inci": ""},  # skipped
        {"product_key": "prod::external_seed::external_seed::ext_3", "sku_key": "sk3", "raw_inci": "NO INGREDIENT LIST FOUND"},  # skipped
    ]
    report = asyncio.run(ing._drive(items, dry_run=False, db=FakeDB()))

    assert report["n"] == 3
    assert report["inci_written"] == 1
    assert report["actives_filled"] == 1
    assert report["claims_written"] == 1
    assert report["skipped"] == 2
    assert enriched == ["prod::external_seed::external_seed::ext_1"]  # only the real-INCI item
    assert executed and executed[0]["mid"] == "external_seed"


def test_dry_run_writes_nothing(monkeypatch):
    async def _fake_persist(pk, *, db=None, dry_run=False):
        assert dry_run is True
        return {"derived": {"active_source": "inci", "substantiated_claims": []},
                "written": {"actives_skus": [], "evidence_claims": False}}

    monkeypatch.setattr(ing, "enrich_and_persist_product", _fake_persist)

    executed = []

    class FakeDB:
        is_connected = True
        async def execute(self, q, p=None): executed.append(p)

    items = [{"product_key": "prod::external_seed::external_seed::ext_1", "sku_key": "sk1", "raw_inci": "Water, Niacinamide"}]
    report = asyncio.run(ing._drive(items, dry_run=True, db=FakeDB()))
    assert executed == []  # no UPSERT under dry_run
    assert report["inci_written"] == 0
