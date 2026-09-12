"""Real feed→mapper→ingest seam with bounded HTTP fixtures; no DB/network writes."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from services import curated_brand_feed as feed
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl


def product(number=1, vendor="A'PIEU", **updates):
    row = {
        "id": 9000000 + number, "vendor": vendor, "title": "Honey Milk Lip Oil",
        "handle": f"honey-milk-lip-oil-{number}", "product_type": "Lip Oil",
        "body_html": "<p>Ingredients: Water, Glycerin, Butylene Glycol, Panthenol, Adenosine</p>",
        "images": [{"src": "https://cdn.example/item.jpg"}],
        "variants": [{"id": 45000000000000 + number, "price": "19.00", "available": True,
                      "option1": "Peach", "sku": f"LIP{number}"}],
    }
    row.update(updates)
    return row


def install_http(monkeypatch, replies):
    requests = []
    def handle(request):
        requests.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": "temporary"})
        return httpx.Response(200, json=reply)
    factory = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(feed.httpx, "AsyncClient", lambda **kw: factory(transport=transport, **kw))
    monkeypatch.setattr(feed.crawl_politeness, "before_request", AsyncMock())
    monkeypatch.setattr(feed.crawl_politeness, "note_response", lambda *a, **kw: None)
    monkeypatch.setattr(feed.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(feed, "_PER_PAGE", 2)
    return requests


def map_record(host, *, role="retailer", name="Same Display Name", **kwargs):
    return feed.shopify_product_to_record(
        product(**kwargs), domain=host, category_path="beauty/skincare",
        brand_override="A'PIEU", currency="SGD", source_role=role, retailer_name=name,
    )


def test_retailer_sellers_and_ingredient_authority_survive_ingestion():
    records = [map_record("sukoshi.com"), map_record("sentisenti.com")]
    plan = ingest_validated_jsonl(records)
    assert len(plan["offers"]) == 2
    assert {o["merchant_id"] for o in plan["offers"]} == {
        "agent_seed::retailer::sukoshi.com", "agent_seed::retailer::sentisenti.com",
    }
    assert {r["pdp"]["inci_source"] for r in records} == {"reseller_listing"}
    assert plan["incis"] and {i["source"] for i in plan["incis"]} == {"reseller_listing"}
    assert {json.loads(o["offer_payload"])["merchant_inferred"] for o in plan["offers"]} == {"Same Display Name"}
    # Idempotency remains deterministic for retries and display-name corrections.
    again = ingest_validated_jsonl([map_record("sukoshi.com", name="New Name"), records[1]])
    assert {o["offer_id"] for o in plan["offers"]} == {o["offer_id"] for o in again["offers"]}
    assert {o["merchant_id"] for o in plan["offers"]} == {o["merchant_id"] for o in again["offers"]}


def test_retailer_host_names_do_not_collide_under_punctuation_normalization():
    plan = ingest_validated_jsonl([map_record("a-b.com"), map_record("a.b.com")])
    assert len({o["merchant_id"] for o in plan["offers"]}) == 2


def test_official_merchant_identity_and_authority_are_unchanged():
    rec = map_record("apieu.com", role="brand_official")
    plan = ingest_validated_jsonl([rec])
    assert plan["offers"][0]["merchant_id"] == "agent_seed::a-pieu"
    assert plan["incis"][0]["source"] == "brand_official"


@pytest.mark.parametrize("vendor", ["3CE", "3M", "3INA", "珂润", "설화수", "悦诗风吟"])
def test_actual_short_and_non_latin_vendors_are_preserved(vendor):
    assert feed.resolve_record_brand(vendor, "MISSHA", "retailer.com") == (vendor, "vendor_disagrees")


@pytest.mark.asyncio
async def test_non_latin_brands_do_not_collapse_in_spelling_fold(monkeypatch):
    monkeypatch.setattr(feed, "fetch_shopify_products", AsyncMock(return_value=[product(vendor="珂润"), product(2, "悦诗风吟")]))
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", AsyncMock(return_value={"currency": "USD"}))
    records = await feed.records_for_brand(domain="retailer.com", brand="MISSHA", category_path="beauty", source_role="retailer")
    assert {r["pdp"]["brand"] for r in records} == {"珂润", "悦诗风吟"}


@pytest.mark.parametrize("title,product_type,want", [
    ("MISSHA Artistool Foundation Brush #101", "", "beauty/tools/brush"),
    ("Honey Milk Lip Oil", "Lip Oil", "beauty/makeup/lip/oil"),
    ("Gentle Cleanser", "Cleanser", "beauty/skincare/cleanse/cleanser"),
    ("Mystery Item", "", "beauty/skincare"),
])
def test_category_comes_from_product_not_store(title, product_type, want):
    assert map_record("misshaus.com", title=title, product_type=product_type)["pdp"]["category_path"] == want


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", [None, "", "usd", "not money"])
async def test_controlled_retailer_refuses_unknown_currency_without_opt_in(monkeypatch, currency):
    monkeypatch.setattr(feed, "fetch_shopify_products", AsyncMock(return_value=[product()]))
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", AsyncMock(return_value={"currency": currency}))
    with pytest.raises(feed.CurrencyNotProven):
        await feed.records_for_brand(domain="cocomo.sg", brand="A'PIEU", category_path="beauty", source_role="retailer")


@pytest.mark.asyncio
async def test_vendor_selection_scans_past_selected_product_budget(monkeypatch):
    reqs = install_http(monkeypatch, [
        {"products": [product(1, "Other"), product(2, "Other")]},
        {"products": [product(3, "Other"), product(4, "Other")]},
        {"products": [product(5)]},
    ])
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", AsyncMock(return_value={"currency": "USD"}))
    records = await feed.records_for_brand(domain="retailer.com", category_path="beauty", source_role="retailer",
                                          only_vendors=["A'PIEU"], max_products=1, max_scan_products=10)
    assert len(records) == 1 and len(reqs) == 3
    assert records.crawl_report == {
        "status": "complete", "pages": 3, "scanned_products": 5, "selected_products": 1, "emitted_records": 1,
    }


@pytest.mark.asyncio
async def test_scan_cap_is_not_vendor_absence(monkeypatch):
    install_http(monkeypatch, [{"products": [product(1, "Other"), product(2, "Other")]}, {"products": [product(3)]}])
    with pytest.raises(feed.CrawlIncomplete) as err:
        await feed.fetch_shopify_products("retailer.com", only_vendors=["A'PIEU"], max_products=1, max_scan_products=2)
    assert err.value.status == "capped" and err.value.next_page == 2
    assert err.value.scanned_products == 2 and err.value.selected_products == 0


@pytest.mark.asyncio
async def test_exact_cap_must_establish_exhaustion_with_lookahead(monkeypatch):
    reqs = install_http(monkeypatch, [{"products": [product(1), product(2)]}, {"products": []}])
    result = await feed.fetch_shopify_products("retailer.com", max_products=2)
    assert len(result) == 2 and len(reqs) == 2
    assert result.crawl_report["status"] == "complete"


@pytest.mark.asyncio
async def test_selected_budget_overflow_never_yields_partial_success(monkeypatch):
    install_http(monkeypatch, [{"products": [product(1), product(2)]}])
    with pytest.raises(feed.CrawlIncomplete, match="selected-product budget") as err:
        await feed.fetch_shopify_products("retailer.com", max_products=1, max_scan_products=20)
    assert err.value.status == "capped"


@pytest.mark.asyncio
async def test_page_two_throttle_has_bounded_retries_and_no_partial_return(monkeypatch):
    reqs = install_http(monkeypatch, [{"products": [product(1), product(2)]}, 429, 429, 429])
    with pytest.raises(feed.CrawlIncomplete, match="HTTP 429") as err:
        await feed.fetch_shopify_products("retailer.com", max_products=10)
    assert len(reqs) == 4
    assert err.value.status == "failed" and err.value.next_page == 2
    assert err.value.scanned_products == 2


@pytest.mark.asyncio
async def test_transient_page_failure_can_recover(monkeypatch):
    reqs = install_http(monkeypatch, [503, {"products": [product(1)]}])
    result = await feed.fetch_shopify_products("retailer.com", max_products=10)
    assert len(reqs) == 2 and len(result) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{}, {"products": "oops"}, {"products": [None]}])
async def test_invalid_envelope_is_not_empty_catalog(monkeypatch, invalid):
    install_http(monkeypatch, [invalid])
    with pytest.raises(feed.CrawlIncomplete):
        await feed.fetch_shopify_products("retailer.com")


@pytest.mark.asyncio
async def test_repeated_page_cannot_consume_budget_as_new_products(monkeypatch):
    page = {"products": [product(1), product(2)]}
    install_http(monkeypatch, [page, page])
    with pytest.raises(feed.CrawlIncomplete, match="did not advance"):
        await feed.fetch_shopify_products("retailer.com", max_products=10)


def test_category_repair_plan_is_guarded_and_matches_fresh_ingest():
    from scripts.plan_curated_category_repair import plan_category_repair
    row = {"product_key": "existing-brush", "title": "Artistool Foundation Brush #101",
           "product_type": "", "category_path": "beauty/skincare"}
    plan = plan_category_repair([row])
    fresh = map_record("misshaus.com", title=row["title"], product_type="")
    assert plan["mode"] == "review_only" and plan["proposed_changes"] == 1
    assert plan["changes"][0]["expected_category_path"] == "beauty/skincare"
    assert plan["changes"][0]["proposed_category_path"] == fresh["pdp"]["category_path"]
    assert row["category_path"] == "beauty/skincare"  # input snapshot remains untouched
    with pytest.raises(ValueError, match="duplicate product_key"):
        plan_category_repair([row, row])


def test_cli_prints_crawl_failure_and_never_applies_prefix(monkeypatch, capsys):
    from scripts import onboard_curated_brands as cli
    failed = feed.CrawlIncomplete("throttled", status="failed", next_page=2, scanned_products=250, selected_products=10)
    monkeypatch.setattr(cli, "records_for_brand", AsyncMock(side_effect=failed))
    apply = AsyncMock()
    monkeypatch.setattr(cli, "apply_ingest_plan", apply)
    assert cli.main(["--domain", "retailer.com", "--category", "beauty", "--source-role", "retailer", "--apply"]) == 2
    assert json.loads(capsys.readouterr().err)["crawl"]["next_page"] == 2
    apply.assert_not_called()


def test_native_variant_ids_are_scoped_to_their_actual_retailer():
    records = [feed.shopify_product_to_record(
        product(), domain=host, category_path="beauty", source_role="retailer", currency="USD",
        brand_override="A'PIEU", emit_native_variants=True,
    ) for host in ["sukoshi.com", "sentisenti.com"]]
    plan = ingest_validated_jsonl(records)
    native = [s for s in plan["skus"] if s["source_variant_id"] == "45000000000001"]
    assert len(native) == 2 and len({s["sku_key"] for s in native}) == 2
    assert {s["source_domain"] for s in native} == {"sukoshi.com", "sentisenti.com"}
    native_offers = [o for o in plan["offers"] if o["sku_key"] in {s["sku_key"] for s in native}]
    assert len(native_offers) == 2
    assert {o["merchant_id"] for o in native_offers} == {
        "agent_seed::retailer::sukoshi.com", "agent_seed::retailer::sentisenti.com",
    }


def test_filtered_cli_requires_an_explicit_source_role(monkeypatch, capsys):
    from scripts import onboard_curated_brands as cli
    fetch = AsyncMock()
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    assert cli.main(["--domain", "retailer.com", "--category", "beauty", "--only-vendor", "3CE"]) == 2
    assert "explicit --source-role" in capsys.readouterr().err
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_record_batches_retain_their_own_scan_evidence(monkeypatch):
    import asyncio
    a_report = {"status": "complete", "scanned_products": 10, "selected_products": 1, "pages": 2}
    b_report = {"status": "complete", "scanned_products": 20, "selected_products": 1, "pages": 3}
    async def fetch(domain, **kw):
        return feed.CuratedRecordBatch([product()], crawl_report=a_report if domain == "a.com" else b_report)
    async def locale(domain):
        await asyncio.sleep(0)
        return {"currency": "USD"}
    monkeypatch.setattr(feed, "fetch_shopify_products", fetch)
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", locale)
    first, second = await asyncio.gather(*[
        feed.records_for_brand(domain=host, category_path="beauty", source_role="retailer")
        for host in ["a.com", "b.com"]
    ])
    assert first.crawl_report["scanned_products"] == 10
    assert second.crawl_report["scanned_products"] == 20
    assert first.crawl_report is not second.crawl_report
