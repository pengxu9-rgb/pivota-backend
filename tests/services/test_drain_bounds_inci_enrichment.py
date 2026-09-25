"""PDP INCI enrichment is bounded in the unattended drain (luxiface.com timed out twice, 2026-09-24/25).

Parsing a PDP for INCI is CPU-bound and scales with the page (2.2 s per ~2 MB luxiface page), so the worker's
default -- up to 300 fetches, no time limit -- ran a drain stage past its 3600 s task timeout with nothing logged.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from services import curated_brand_feed as cbf
from services.catalog_onboard_worker import curated_brand_work_key, normalize_curated_brand_payload
from services.retailer_ingest import pipeline


def _p(i):
    return {"id": i, "title": f"Serum {i}", "handle": f"serum-{i}", "vendor": "Brand", "product_type": "Serum",
            "body_html": "<p>A serum.</p>", "tags": [], "images": [{"src": "https://cdn.example/i.jpg"}],
            "variants": [{"id": 10000000 + i, "price": "20.00", "available": True}]}


@pytest.mark.asyncio
async def test_inci_enrichment_stops_at_its_time_budget_and_says_so(monkeypatch):
    async def fake_fetch(domain, **kw):
        return cbf.ShopifyProductBatch([_p(i) for i in range(20)], scanned_products=20, pages=1)

    calls = []

    async def slow_pdp(domain, handle, *, client):
        calls.append(handle)
        await asyncio.sleep(0.05)
        return None

    monkeypatch.setattr(cbf, "fetch_shopify_products", fake_fetch)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", AsyncMock(return_value={"currency": "USD"}))
    monkeypatch.setattr(cbf, "fetch_pdp_inci", slow_pdp)
    records = await cbf.records_for_brand(domain="luxiface.com", category_path="beauty", enrich_missing_inci=True,
                                          max_pdp_inci_fetches=20, pdp_inci_budget_s=0.12)
    report = records.crawl_report["inci_enrichment"]
    assert report["stopped_on_budget"] is True and 1 <= report["attempted"] < 20
    assert report["attempted"] == len(calls) and report["candidates"] == 20 and report["cap"] == 20


@pytest.mark.asyncio
async def test_without_a_budget_the_count_cap_alone_applies_as_before(monkeypatch):
    async def fake_fetch(domain, **kw):
        return cbf.ShopifyProductBatch([_p(i) for i in range(5)], scanned_products=5, pages=1)

    async def pdp(domain, handle, *, client):
        return "Water, Glycerin" if handle.endswith("-0") else None

    monkeypatch.setattr(cbf, "fetch_shopify_products", fake_fetch)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", AsyncMock(return_value={"currency": "USD"}))
    monkeypatch.setattr(cbf, "fetch_pdp_inci", pdp)
    records = await cbf.records_for_brand(domain="x.com", category_path="beauty", enrich_missing_inci=True,
                                          max_pdp_inci_fetches=3)
    assert records.crawl_report["inci_enrichment"] == {"candidates": 5, "cap": 3, "attempted": 3, "found": 1,
                                                       "stopped_on_budget": False,
                                                       "seconds": records.crawl_report["inci_enrichment"]["seconds"]}


def _job(**options):
    return {"id": "rij_x", "domain": "luxiface.com", "brand": "multi:luxiface.com", "status": "queued",
            "options": {"vendors": ["A"], "require_currency": "USD", **options}}


def test_the_drain_bounds_inci_by_default():
    payload = pipeline._feed_payload(_job())
    assert payload["max_pdp_inci_fetches"] == pipeline.DRAIN_PDP_INCI_FETCHES == 60
    assert payload["pdp_inci_budget_s"] == pipeline.DRAIN_PDP_INCI_BUDGET_S == 900


def test_a_job_can_turn_inci_off_or_raise_it_within_the_ceiling():
    assert pipeline._feed_payload(_job(max_pdp_inci_fetches=0))["max_pdp_inci_fetches"] == 0
    assert pipeline._feed_payload(_job(max_pdp_inci_fetches=300))["max_pdp_inci_fetches"] == 300
    with pytest.raises(ValueError, match="max_pdp_inci_fetches must be at most 300"):
        pipeline.validate_options({"vendors": ["A"], "max_pdp_inci_fetches": 301})
    with pytest.raises(ValueError, match="max_pdp_inci_fetches must be a nonnegative integer"):
        pipeline.validate_options({"vendors": ["A"], "max_pdp_inci_fetches": -1})
    with pytest.raises(ValueError, match="max_pdp_identity_fetches must be a positive integer"):
        pipeline.validate_options({"vendors": ["A"], "max_pdp_identity_fetches": 0})


def test_the_drain_payload_normalises_and_reaches_records_for_brand():
    normalized = normalize_curated_brand_payload(pipeline._feed_payload(_job()))
    assert normalized["max_pdp_inci_fetches"] == 60 and normalized["pdp_inci_budget_s"] == 900


@pytest.mark.parametrize("bad", [0, -1, "900", True, float("nan"), float("inf")])
def test_a_bad_budget_is_refused(bad):
    with pytest.raises(ValueError, match="pdp_inci_budget_s"):
        normalize_curated_brand_payload({"domain": "a.com", "brand": "X", "pdp_inci_budget_s": bad})


def test_work_keys_are_unchanged_without_a_budget_and_distinct_with_one():
    base = {"domain": "a.com", "brand": "X"}
    assert curated_brand_work_key(base) == curated_brand_work_key({**base, "pdp_inci_budget_s": None})
    assert curated_brand_work_key(base) != curated_brand_work_key({**base, "pdp_inci_budget_s": 900})
