from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services import crawled_inci_ingest as csi


def test_merchant_id_from_product_key():
    # Pure string helper only — it returns the HISTORICAL/bucket token and must
    # NEVER be used to write a seller subject (ADR-009 D2). See its docstring.
    assert csi.merchant_id_from_product_key("prod::external_seed::external_seed::x") == "external_seed"
    assert csi.merchant_id_from_product_key("prod::merch_abc::shopify::1") == "merch_abc"
    assert csi.merchant_id_from_product_key("") == "external_seed"


class _RoutedDB:
    """fetch_one routes by table: catalog_products -> authoritative seller;
    beauty_sku_ingredients -> existing INCI source (precedence)."""
    is_connected = True

    def __init__(self, *, catalog_merchant, existing_source=None):
        self._catalog_merchant = catalog_merchant  # None => no catalog row (unresolved)
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


def test_is_skippable_rejects_prose_and_blob():
    # Regression (claim-quality audit): only a real delimited INCI may mint claims.
    assert not csi._is_skippable_inci(
        "Water, Snail Secretion Filtrate, Sodium Hyaluronate, Panthenol, Allantoin")
    # crawler keyword-blob (no delimiters)
    assert csi._is_skippable_inci("PDRNNiacinamideAzelaic AcidHeartleafCentellaCeramide")
    # marketing/benefit prose, not an ingredient list
    assert csi._is_skippable_inci("Promotes collagen creation, hydrates deeply, improves elasticity")
    # legacy checks still hold
    assert csi._is_skippable_inci("")
    assert csi._is_skippable_inci("NO INGREDIENT INFO AVAILABLE")


def test_ingest_upserts_with_source_system_and_enriches(monkeypatch):
    executed = []
    enriched = []

    async def _fake_persist(pk, *, db=None, dry_run=False):
        enriched.append(pk)
        return {"derived": {"active_source": "inci", "substantiated_claims": ["Contains Niacinamide"]},
                "written": {"actives_skus": [pk + "::s"], "evidence_claims": True}}

    monkeypatch.setattr(csi, "enrich_and_persist_product", _fake_persist)

    # The catalog row was re-subjected by A9-4 to an observed seller; its
    # product_key is still historical-format `prod::external_seed::...`.
    db = _RoutedDB(catalog_merchant="merch_obs_deadbeefdeadbeef")
    items = [
        {"product_key": "prod::external_seed::external_seed::a", "sku_key": "a::canonical", "raw_inci": "Water, Niacinamide"},
        {"product_key": "prod::external_seed::external_seed::b", "sku_key": "b::canonical", "raw_inci": ""},  # skipped
        {"product_key": "prod::external_seed::external_seed::c", "sku_key": "c::canonical", "raw_inci": "NO INGREDIENT LIST"},  # skipped
    ]
    report = asyncio.run(csi.ingest_crawled_inci_items(
        items, source_system="external_brand_crawl", dry_run=False, db=db))
    executed.extend(db.executed)

    assert report["n"] == 3
    assert report["inci_written"] == 1
    assert report["skipped"] == 2
    assert report["actives_filled"] == 1
    assert report["claims_written"] == 1
    assert enriched == ["prod::external_seed::external_seed::a"]
    # the source_system param flows into the UPSERT bind (default is 'pdp_crawl')
    assert executed[0]["src"] == "external_brand_crawl"
    # LEAK CLOSED: the seller written is the AUTHORITATIVE catalog seller, NOT the
    # bucket token parsed from the historical-format product_key.
    assert executed[0]["mid"] == "merch_obs_deadbeefdeadbeef"


def test_ingest_writes_observed_seller_not_bucket(monkeypatch):
    """A product whose catalog row is a re-subjected observed seller writes THAT
    seller into beauty_sku_ingredients — never 'external_seed' (ADR-009 D2)."""
    async def _fake_persist(pk, *, db=None, dry_run=False):
        return {"derived": {}, "written": {}}

    monkeypatch.setattr(csi, "enrich_and_persist_product", _fake_persist)
    db = _RoutedDB(catalog_merchant="merch_obs_anuko00000000")
    item = [{"product_key": "prod::external_seed::external_seed::a", "sku_key": "a::canonical",
             "raw_inci": "Water, Snail Secretion Filtrate, Niacinamide"}]
    report = asyncio.run(csi.ingest_crawled_inci_items(item, dry_run=False, db=db))
    assert report["inci_written"] == 1
    assert report["skipped_unresolved_seller"] == 0
    assert db.executed[0]["mid"] == "merch_obs_anuko00000000"
    assert db.executed[0]["mid"] != "external_seed"


def test_ingest_skips_loudly_when_no_catalog_row(monkeypatch, caplog):
    """No catalog_products row => seller unknown => SKIP the write loudly, never
    bucket under 'external_seed' and never enrich (honest failure, ADR-009 D2)."""
    enriched = []

    async def _fake_persist(pk, *, db=None, dry_run=False):
        enriched.append(pk)
        return {"derived": {}, "written": {}}

    monkeypatch.setattr(csi, "enrich_and_persist_product", _fake_persist)
    db = _RoutedDB(catalog_merchant=None)  # no catalog row
    item = [{"product_key": "prod::external_seed::external_seed::orphan", "sku_key": "o::canonical",
             "raw_inci": "Water, Niacinamide, Panthenol"}]
    import logging
    with caplog.at_level(logging.WARNING, logger="services.crawled_inci_ingest"):
        report = asyncio.run(csi.ingest_crawled_inci_items(item, dry_run=False, db=db))
    assert report["skipped_unresolved_seller"] == 1
    assert report["inci_written"] == 0
    assert db.executed == []          # nothing written
    assert enriched == []             # not enriched under an unknown subject
    assert any("seller-of-record unknown" in r.message for r in caplog.records)


def test_ingest_dry_run_writes_nothing(monkeypatch):
    async def _fake_persist(pk, *, db=None, dry_run=False):
        assert dry_run is True
        return {"derived": {}, "written": {}}

    monkeypatch.setattr(csi, "enrich_and_persist_product", _fake_persist)

    db = _RoutedDB(catalog_merchant="merch_obs_deadbeefdeadbeef")
    items = [{"product_key": "prod::external_seed::external_seed::a", "sku_key": "a::canonical",
              "raw_inci": "Water, Niacinamide"}]
    report = asyncio.run(csi.ingest_crawled_inci_items(items, dry_run=True, db=db))
    assert db.executed == []
    assert report["inci_written"] == 0


def test_ingest_respects_source_precedence(monkeypatch):
    """A pdp_crawl item must not overwrite a higher-authority (brand_official) row,
    and must write over an equal/lower one."""
    executed = []
    enriched = []

    async def _fake_persist(pk, *, db=None, dry_run=False):
        enriched.append(pk)
        return {"derived": {}, "written": {}}

    monkeypatch.setattr(csi, "enrich_and_persist_product", _fake_persist)

    item = [{"product_key": "prod::external_seed::external_seed::a", "sku_key": "a::canonical", "raw_inci": "Water, Niacinamide"}]

    # brand_official (rank 3) outranks pdp_crawl (rank 1) -> skipped, no write, no enrich
    db = _RoutedDB(catalog_merchant="merch_obs_deadbeefdeadbeef", existing_source="brand_official")
    report = asyncio.run(csi.ingest_crawled_inci_items(item, dry_run=False, db=db))
    assert report["skipped_outranked"] == 1
    assert report["inci_written"] == 0
    assert db.executed == [] and enriched == []

    # reseller_listing (rank 1) == pdp_crawl (rank 1) -> writes (same-rank refresh)
    db = _RoutedDB(catalog_merchant="merch_obs_deadbeefdeadbeef", existing_source="reseller_listing")
    report = asyncio.run(csi.ingest_crawled_inci_items(item, dry_run=False, db=db))
    executed.extend(db.executed)
    assert report["skipped_outranked"] == 0
    assert report["inci_written"] == 1
    assert executed and enriched == ["prod::external_seed::external_seed::a"]
