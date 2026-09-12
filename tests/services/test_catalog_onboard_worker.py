"""Catalog-onboard queue worker tests (mocked queue + feeds; no DB/network)."""

import asyncio
import copy

import pytest

import services.catalog_onboard_worker as w


def test_enqueue_curated_dedup_key_and_priority(monkeypatch):
    calls = []

    async def fake_enqueue(*, kind, dedup_key, payload, priority, source, db=None):
        calls.append((kind, dedup_key, priority))
        return "id"

    monkeypatch.setattr(w.q, "enqueue", fake_enqueue)
    brands = [{"domain": "cosrx.com", "brand": "COSRX"}, {"domain": "", "brand": "skip"}]
    n = asyncio.run(w.enqueue_curated_brands(brands, priority_rank={"cosrx": 7}))
    assert n == 1  # blank domain skipped
    assert len(calls) == 1
    assert calls[0][0] == "curated_brand" and calls[0][2] == 7
    assert calls[0][1].startswith("curated:v2:cosrx.com:")


def test_enqueue_audit_candidates_normalizes_key(monkeypatch):
    calls = []

    async def fake_enqueue(*, kind, dedup_key, payload, priority, source, db=None):
        calls.append((kind, dedup_key, priority))
        return "id"

    monkeypatch.setattr(w.q, "enqueue", fake_enqueue)
    cands = [{"product_name": "OLLY Collagen"}, {"product_name": ""}]
    n = asyncio.run(w.enqueue_audit_candidates(cands, priority_rank={"olly collagen": 5}))
    assert n == 1
    assert calls == [("audit_candidate", "olly collagen", 5)]


def test_enqueue_defaults_to_recurrence_rank(monkeypatch):
    # No priority_rank supplied → falls back to live cross-audit demand.
    calls = []

    async def fake_enqueue(*, kind, dedup_key, payload, priority, source, db=None):
        calls.append((dedup_key, priority))
        return "id"

    async def fake_rank(*, db=None):
        return {"cosrx": 9}

    monkeypatch.setattr(w.q, "enqueue", fake_enqueue)
    monkeypatch.setattr(w, "recurrence_rank", fake_rank)
    n = asyncio.run(w.enqueue_curated_brands([{"domain": "cosrx.com", "brand": "COSRX"}]))
    assert n == 1
    assert len(calls) == 1 and calls[0][1] == 9
    assert calls[0][0].startswith("curated:v2:cosrx.com:")


def _job(**overrides):
    payload = {"domain": "retailer.example", "brand": "COSRX", "source_role": "retailer",
               "only_vendors": ["COSRX"], "require_currency": "USD",
               "category_path": "beauty/skincare"}
    return dict(payload, **overrides)


def _complete(records):
    from services.curated_brand_feed import CuratedRecordBatch
    return CuratedRecordBatch(records, crawl_report={"status": "complete", "pages": 1,
        "scanned_products": len(records), "selected_products": len(records), "emitted_records": len(records)})


def test_work_identity_retains_subsets_and_normalizes_equivalent_requests():
    first = _job()
    equivalent = _job(domain="https://RETAILER.example/", brand=" cosrx ",
                      only_vendors=[" COSRX ", "cosrx"], require_currency="usd", market="us",
                      retailer_name="retailer.example")
    assert w.curated_brand_work_key(first) == w.curated_brand_work_key(equivalent)
    for other in [
        _job(brand="APIEU", only_vendors=["APIEU"]), _job(only_vendors=["COSRX", "APIEU"]),
        _job(require_currency="SGD"), _job(category_path="beauty/makeup"),
        _job(source_role="brand_official"), _job(emit_real_variants=False),
        _job(max_products=1000),
    ]:
        assert w.curated_brand_work_key(first) != w.curated_brand_work_key(other)
    assert w.curated_brand_work_key(first, source="meitu") != w.curated_brand_work_key(first, source="audit")


@pytest.mark.parametrize("overrides", [
    {"only_vendors": "COSRX"}, {"only_vendors": []}, {"only_vendors": [""]},
    {"source_role": "unknown"}, {"source_role": None}, {"require_currency": ""},
    {"require_currency": "US"}, {"market": "SG"}, {"emit_real_variants": "false"},
    {"max_products": True}, {"max_products": 0}, {"max_scan_products": -1},
    {"domain": "https://retailer.example/products"}, {"domain": "not a host"},
])
def test_invalid_controls_refused_before_enqueue(monkeypatch, overrides):
    calls = []

    async def enqueue(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(w.q, "enqueue", enqueue)
    with pytest.raises(ValueError):
        asyncio.run(w.enqueue_curated_brands([_job(), _job(**overrides)], priority_rank={}))
    assert not calls


def test_vendor_subset_requires_explicit_source_role():
    payload = _job()
    del payload["source_role"]
    with pytest.raises(ValueError, match="explicit source_role"):
        w.normalize_curated_brand_payload(payload)


def test_retailer_name_cannot_silently_take_brand_official_authority():
    with pytest.raises(ValueError, match="explicit source_role"):
        w.normalize_curated_brand_payload({"domain": "retailer.example", "retailer_name": "Eyurs"})
    with pytest.raises(ValueError, match="only valid"):
        w.normalize_curated_brand_payload(_job(source_role="brand_official", retailer_name="Eyurs"))


def test_worker_forwards_all_supported_controls(monkeypatch):
    calls = []

    async def records(**kwargs):
        calls.append(kwargs)
        return _complete([])

    monkeypatch.setattr(w, "records_for_brand", records)
    payload = _job(max_products=700, max_scan_products=12000, base_listings_only=True,
                   emit_real_variants=True, enrich_missing_inci=False, max_pdp_inci_fetches=0)
    result = asyncio.run(w._process_curated_brand(payload, apply=False, db=None))
    assert result["records"] == 0
    assert calls == [{
        "domain": "retailer.example", "brand": "COSRX", "category_path": "beauty/skincare",
        "source_role": "retailer", "retailer_name": "retailer.example", "only_vendors": ["cosrx"],
        "require_currency": "USD", "emit_real_variants": True, "base_listings_only": True,
        "enrich_missing_inci": False, "max_pdp_inci_fetches": 0, "max_products": 700,
        "max_scan_products": 12000,
    }]


@pytest.mark.parametrize("currency", [None, "", "usd", "SGD"])
def test_worker_refuses_unproven_or_mismatched_currency_before_planning(monkeypatch, currency):
    async def records(**kwargs):
        return _complete([{"pdp": {"currency": currency}}])

    monkeypatch.setattr(w, "records_for_brand", records)
    monkeypatch.setattr(w, "ingest_validated_jsonl", lambda _: pytest.fail("must not plan unsafe currency"))
    with pytest.raises(ValueError, match="currency"):
        asyncio.run(w._process_curated_brand(_job(), apply=True, db=None))


def test_worker_requires_currency_even_without_expected_currency(monkeypatch):
    async def records(**kwargs):
        return _complete([{"pdp": {"currency": None}}])

    monkeypatch.setattr(w, "records_for_brand", records)
    with pytest.raises(ValueError, match="currency was not proven"):
        asyncio.run(w._process_curated_brand(_job(require_currency=None), apply=False, db=None))


def _merchant_product(brand, number):
    return {
        "id": 9000000 + number, "vendor": brand, "title": f"{brand} Facial Cleanser",
        "handle": f"cleanser-{number}", "product_type": "Cleanser",
        "body_html": "<p>A gentle facial cleanser for daily use with hydrating ingredients.</p>",
        "images": [{"src": "https://cdn.example/cleanser.jpg"}], "options": [{"name": "Size"}],
        "variants": [
            {"id": 45000000000000 + number, "price": "19.00", "available": True, "option1": "30ml"},
            {"id": 46000000000000 + number, "price": "29.00", "available": True, "option1": "50ml"},
        ],
    }


def test_real_worker_feed_and_plan_keep_only_selected_vendor_and_real_variants(monkeypatch):
    from unittest.mock import AsyncMock
    from services import curated_brand_feed as feed
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

    products = [_merchant_product("COSRX", 1), _merchant_product("APIEU", 2)]
    monkeypatch.setattr(feed, "fetch_shopify_products", AsyncMock(
        return_value=feed.ShopifyProductBatch(products, scanned_products=2, pages=1)))
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", AsyncMock(return_value={"currency": "USD"}))
    plans = []

    def observe(records):
        plan = ingest_validated_jsonl(records)
        plans.append(copy.deepcopy(plan))
        return plan

    monkeypatch.setattr(w, "ingest_validated_jsonl", observe)
    result = asyncio.run(w._process_curated_brand(_job(enrich_missing_inci=False), apply=False, db=None))
    assert result["records"] == 1 and result["plan_pdps"] == 1
    assert result["crawl"]["status"] == "complete" and result["crawl"]["scanned_products"] == 2
    plan = plans[0]
    assert {p["brand"] for p in plan["pdps"]} == {"COSRX"}
    # Ingestion also retains its canonical aggregate SKU. Both merchant-issued
    # variants must be present beside that anchor; an opt-in lost at the worker
    # leaves ONLY the synthetic anchor and fails this assertion.
    real_ids = {str(s["source_variant_id"]) for s in plan["skus"] if str(s["source_variant_id"]).isdigit()}
    assert real_ids == {"45000000000001", "46000000000001"}
    assert {o["currency"] for o in plan["offers"]} == {"USD"}
    assert {o["merchant_id"] for o in plan["offers"]} == {"agent_seed::retailer::retailer.example"}


def test_real_worker_missing_required_locale_never_builds_plan(monkeypatch):
    from unittest.mock import AsyncMock
    from services import curated_brand_feed as feed

    monkeypatch.setattr(feed, "fetch_shopify_products", AsyncMock(return_value=[_merchant_product("COSRX", 1)]))
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", AsyncMock(return_value={"currency": None}))
    monkeypatch.setattr(w, "ingest_validated_jsonl", lambda _: pytest.fail("unproven currency reached plan"))
    with pytest.raises(feed.CurrencyNotProven):
        asyncio.run(w._process_curated_brand(_job(require_currency="SGD"), apply=True, db=None))


def test_real_partial_feed_cannot_reach_worker_plan(monkeypatch):
    from tests.services.test_curated_retailer_contract import install_http, product
    from services import curated_brand_feed as feed

    requests = install_http(monkeypatch, [{"products": [product(1, "COSRX"), product(2, "COSRX")]}, 429, 429, 429])
    monkeypatch.setattr(w, "ingest_validated_jsonl", lambda _: pytest.fail("partial crawl reached plan"))
    with pytest.raises(feed.CrawlIncomplete) as failure:
        asyncio.run(w._process_curated_brand(_job(), apply=True, db=None))
    assert failure.value.next_page == 2 and len(requests) == 4


@pytest.mark.parametrize("status", [None, "partial", "failed", "capped"])
def test_worker_refuses_missing_or_incomplete_batch_metadata(monkeypatch, status):
    from services.curated_brand_feed import CuratedRecordBatch

    async def records(**kwargs):
        report = {"status": status} if status else None
        return CuratedRecordBatch([{"pdp": {"currency": "USD"}}], crawl_report=report)

    monkeypatch.setattr(w, "records_for_brand", records)
    monkeypatch.setattr(w, "ingest_validated_jsonl", lambda _: pytest.fail("unproven completeness reached plan"))
    with pytest.raises(ValueError, match="completeness was not proven"):
        asyncio.run(w._process_curated_brand(_job(), apply=True, db=None))


def _patch_drain(monkeypatch, items, *, raise_on=None, done=None, failed=None):
    async def claim_batch(limit, db=None):
        return items

    async def mark_done(item_id, *, result=None, db=None):
        done.append(item_id)

    async def mark_failed(item_id, *, error, attempts, max_attempts, db=None):
        failed.append((item_id, attempts >= max_attempts))

    async def fake_curated(payload, *, apply, db):
        if raise_on == "curated_brand":
            raise RuntimeError("boom")
        return {"records": 1, "applied": {"pdps": 1} if apply else None}

    async def fake_audit(payload, *, apply, db):
        return {"plan_pdps": 1, "applied": {"pdps": 1} if apply else None}

    monkeypatch.setattr(w.q, "claim_batch", claim_batch)
    monkeypatch.setattr(w.q, "mark_done", mark_done)
    monkeypatch.setattr(w.q, "mark_failed", mark_failed)
    monkeypatch.setattr(w, "_process_curated_brand", fake_curated)
    monkeypatch.setattr(w, "_process_audit_candidate", fake_audit)


def test_process_queue_dispatch_and_done(monkeypatch):
    done, failed = [], []
    items = [
        {"id": "a", "kind": "curated_brand", "payload": {"domain": "x.com"}, "attempts": 1, "max_attempts": 3},
        {"id": "b", "kind": "audit_candidate", "payload": {"product_name": "Y"}, "attempts": 1, "max_attempts": 3},
    ]
    _patch_drain(monkeypatch, items, done=done, failed=failed)
    summary = asyncio.run(w.process_queue(limit=10, apply=True))
    assert summary == {"claimed": 2, "done": 2, "failed": 0, "skipped": 0}
    assert set(done) == {"a", "b"} and failed == []


def test_process_queue_failure_marks_failed(monkeypatch):
    done, failed = [], []
    items = [{"id": "a", "kind": "curated_brand", "payload": {}, "attempts": 3, "max_attempts": 3}]
    _patch_drain(monkeypatch, items, raise_on="curated_brand", done=done, failed=failed)
    summary = asyncio.run(w.process_queue(limit=10, apply=True))
    assert summary["failed"] == 1 and summary["done"] == 0
    assert failed == [("a", True)]  # attempts==max → terminal failure


def test_process_queue_unknown_kind_skipped(monkeypatch):
    done, failed = [], []
    items = [{"id": "z", "kind": "weird", "payload": {}, "attempts": 1, "max_attempts": 3}]
    skipped = []

    async def mark_skipped(item_id, *, reason="", db=None):
        skipped.append(item_id)

    _patch_drain(monkeypatch, items, done=done, failed=failed)
    monkeypatch.setattr(w.q, "mark_skipped", mark_skipped)
    summary = asyncio.run(w.process_queue(limit=10, apply=True))
    assert summary["skipped"] == 1 and skipped == ["z"]
