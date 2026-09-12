"""Concrete adversarial failures from the pre-rollout review; no network or DB."""
import copy
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import onboard_curated_brands as cli
from services import catalog_onboard_worker as worker, curated_brand_feed as feed
from services.catalog_enrichment_agent import apply as writer, ingestion as ing
from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan


def product():
    return {
        "id": 9000001, "handle": "honey-milk-lip-oil", "title": "Honey Milk Lip Oil",
        "vendor": "A'PIEU", "product_type": "Lip Oil", "images": [{"src": "https://cdn.example/oil.jpg"}],
        "variants": [{"id": 45000000000001, "barcode": "8809530070499", "price": "10.00", "available": True}],
    }


def record(host="first.example", raw=None, *, native=True):
    return feed.shopify_product_to_record(
        raw or product(), domain=host, category_path="beauty", currency="USD",
        source_role="retailer", emit_native_variants=native,
    )


def batch(records):
    return feed.CuratedRecordBatch(records, crawl_report={"status": "complete", "pages": 1})


@pytest.mark.asyncio
async def test_same_title_native_product_and_variant_ids_keep_two_listing_chains_on_replay():
    records = [record(host) for host in ("first.example", "second.example")]
    plan = ing.ingest_validated_jsonl(records)
    assert {k: len(plan[k]) for k in ("pdps", "skus", "offers", "seeds")} == {
        "pdps": 2, "skus": 4, "offers": 4, "seeds": 2,
    }
    assert not plan["skipped_reasons"]
    assert len({p["pivota_signature_id"] for p in plan["pdps"]}) == 2
    assert inspect_primary_plan(plan)["status"] == "ready_to_apply"
    existing = copy.deepcopy(plan["pdps"])

    class DB:
        async def fetch_all(self, *args, **kwargs):
            return existing
        async def fetch_one(self, *args, **kwargs):
            return None
        async def execute(self, *args, **kwargs):
            return None

    before = [(s["sku_key"], s["merchant_id"], s["source_variant_id"]) for s in plan["skus"]]
    await writer._prepare_seller_of_record(plan, DB())
    assert [(s["sku_key"], s["merchant_id"], s["source_variant_id"]) for s in plan["skus"]] == before
    assert len({s["merchant_id"] for s in plan["skus"]}) == 2
    for row in records:
        row["pdp"]["product_name"] = "Revised Display Title"
    replay = ing.ingest_validated_jsonl(records)
    assert {p["product_key"] for p in replay["pdps"]} == {p["product_key"] for p in plan["pdps"]}
    assert {p["pivota_signature_id"] for p in replay["pdps"]} == {p["pivota_signature_id"] for p in plan["pdps"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
async def test_legacy_same_storefront_listing_blocks_before_any_write(batch_mode):
    plan = ing.ingest_validated_jsonl([record()])
    legacy = dict(plan["pdps"][0], product_key="ext:legacy-title::12345678")
    database = AsyncMock()
    database.is_connected = True
    database.fetch_all.return_value = [legacy]
    with pytest.raises(ValueError, match="retailer_listing_migration_required"):
        await writer.apply_ingest_plan(plan, batch_label="review", db=database, batch=batch_mode)
    database.execute.assert_not_awaited()


def test_apex_www_listing_identity_is_equal_and_other_store_is_refused():
    assert ing.retailer_listing_identity("www.first.example", "https://first.example/products/oil/") == (
        ing.retailer_listing_identity("first.example", "http://www.first.example/products/oil?tracking=1")
    )
    with pytest.raises(ValueError, match="identity_unproven"):
        ing.retailer_listing_identity("first.example", "https://second.example/products/oil")


@pytest.mark.parametrize("other_barcode", ["4006381333931", "8809530070499", None])
def test_native_variant_order_or_price_never_promotes_one_barcode_to_whole_line(other_barcode):
    raw = product()
    raw["variants"].append(dict(raw["variants"][0], id=45000000000002, barcode=other_barcode))
    rows = []
    for _ in range(2):
        mapped = record(raw=raw)
        plan = ing.ingest_validated_jsonl([mapped])
        assert mapped["pdp"]["barcode"] is None
        assert plan["pdps"][0]["gtin"] is None
        assert {s["barcode"] for s in plan["skus"] if "::v:" in s["sku_key"]} == {"8809530070499", other_barcode}
        rows.append((plan["pdps"][0]["product_key"], plan["pdps"][0]["content_key"]))
        raw["variants"].reverse()
    assert rows[0] == rows[1]
    raw["variants"][0]["price"] = "0.00"
    assert record(raw=raw)["pdp"]["barcode"] is None


@pytest.mark.parametrize("vendor,host", [(None, "store.example"), ("VC-B004", "sukoshi.com"),
    ("Metro Singapore Departmental Store - Celebrating 69 Years in SG", "metro.com.sg")])
def test_store_or_supplier_vendor_cannot_become_operator_brand(vendor, host):
    raw = product()
    raw["vendor"] = vendor
    with pytest.raises(ValueError, match="retailer_maker_unproven"):
        feed.shopify_product_to_record(raw, domain=host, category_path="beauty", currency="USD",
                                      source_role="retailer", brand_override="Elizabeth Arden")


@pytest.mark.parametrize("vendor", ["珂润", "설화수", "3CE", "A'PIEU Plus"])
def test_real_distinct_vendor_cannot_be_overridden(vendor):
    raw = product()
    raw["vendor"] = vendor
    mapped = feed.shopify_product_to_record(raw, domain="store.example", category_path="beauty", currency="USD",
                                          source_role="retailer", brand_override="A'PIEU")
    assert mapped["pdp"]["brand"] == vendor


def test_empty_and_cross_product_only_chains_are_not_primary_ready():
    assert inspect_primary_plan({})["status"] == "blocked"
    plan = ing.ingest_validated_jsonl([record("first.example"), record("second.example")])
    missing = plan["pdps"][1]["product_key"]
    plan["offers"] = [o for o in plan["offers"] if o["product_key"] != missing]
    report = inspect_primary_plan(plan)
    assert report["status"] == "blocked"
    assert report["missing_commerce_product_keys"] == [missing]
    assert inspect_primary_plan(ing.ingest_validated_jsonl([record(native=False)]))["status"] == "blocked"


def test_cli_rejects_unsupported_market_before_fetching_any_roster_row(monkeypatch, tmp_path):
    roster = tmp_path / "roster.jsonl"
    rows = [{"domain": "first.example", "category_path": "beauty", "source_role": "retailer",
             "only_vendors": ["A'PIEU"], "emit_real_variants": True},
            {"domain": "second.example", "category_path": "beauty", "market": "SG"}]
    roster.write_text("\n".join(map(json.dumps, rows)))
    fetch = AsyncMock()
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    assert cli.main(["--file", str(roster), "--apply"]) == 2
    fetch.assert_not_awaited()


def test_cli_row_controls_reach_fetch_and_real_native_plan(monkeypatch, tmp_path):
    roster = tmp_path / "roster.jsonl"
    row = {"domain": "first.example", "category_path": "beauty", "market": "US", "source_role": "retailer",
           "only_vendors": ["A'PIEU"], "emit_real_variants": True, "max_products": 7,
           "base_listings_only": True, "max_scan_products": 12, "require_currency": "USD"}
    roster.write_text(json.dumps(row))
    calls = []
    async def fetch(**kwargs):
        calls.append(kwargs)
        return batch([record(native=kwargs["emit_real_variants"])])
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    assert cli.main(["--file", str(roster)]) == 0
    assert calls[0]["emit_real_variants"] is True
    assert calls[0]["base_listings_only"] is True
    assert calls[0]["max_products"] == 7
    assert calls[0]["max_scan_products"] == 12


@pytest.mark.asyncio
async def test_empty_complete_crawl_cannot_finish_queue_apply(monkeypatch):
    monkeypatch.setattr(worker, "records_for_brand", AsyncMock(return_value=batch([])))
    apply = AsyncMock()
    monkeypatch.setattr(worker, "apply_ingest_plan", apply)
    with pytest.raises(ValueError, match="no products enumerated"):
        await worker._process_curated_brand({"domain": "first.example"}, apply=True, db=None)
    apply.assert_not_awaited()


def test_retailer_cli_requires_exact_maker_subset_before_fetch(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    assert cli.main(["--domain", "store.example", "--category", "beauty", "--source-role", "retailer"]) == 2
    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
@pytest.mark.parametrize("outcome", [None, {}, {"action": "MINT"},
    {"action": "MINT", "content_key": "ck_substitute", "evidence": {"evidence": {"reason": "error", "error": "db unavailable"}}}])
async def test_primary_identity_failure_never_writes_a_minted_substitute(monkeypatch, batch_mode, outcome):
    from services import intake_identity as identity
    plan = ing.ingest_validated_jsonl([record()])
    monkeypatch.setattr(identity, "intake_identity_enabled", lambda door: True)
    monkeypatch.setattr(identity, "resolve_or_attach_content_identity", AsyncMock(return_value=outcome))
    monkeypatch.setattr(writer, "write_writer_audit_log", AsyncMock())
    database = AsyncMock()
    database.is_connected = True
    database.fetch_all.return_value = []
    database.fetch_one.return_value = None
    database.transaction = MagicMock(return_value=AsyncMock())
    counts = await writer.apply_ingest_plan(plan, batch_label="review", db=database, batch=batch_mode)
    assert counts["pdps"] == counts["skus"] == counts["offers"] == counts["seeds"] == 0
    assert counts["pdps_skipped_identity"] == 1
    for call in database.execute.await_args_list:
        sql = str(call.args[0] if call.args else call.kwargs.get("query", ""))
        assert not any("INSERT INTO " + table in sql for table in ("catalog_products", "catalog_skus", "catalog_offers", "external_product_seeds"))


@pytest.mark.asyncio
async def test_legacy_guard_ignores_unrelated_missing_source_metadata():
    plan = ing.ingest_validated_jsonl([record()])
    database = AsyncMock()
    database.fetch_all.return_value = [{"product_key": "legacy", "source_domain": None,
                                       "canonical_url": "https://first.example/products/unrelated"}]
    await writer._refuse_parallel_retailer_listings(plan, database)
    database.execute.assert_not_awaited()


@pytest.mark.parametrize("stock,expected,quantity", [(True, "in_stock", None), (False, "out_of_stock", 0), (None, "unknown", None)])
def test_observed_stock_reaches_native_and_canonical_offers_without_invented_quantity(stock, expected, quantity):
    raw = product()
    if stock is None:
        raw["variants"][0].pop("available")
    else:
        raw["variants"][0]["available"] = stock
    mapped = record(raw=raw)
    assert mapped["pdp"]["variants"][0]["in_stock"] is stock
    plan = ing.ingest_validated_jsonl([mapped])
    assert len(plan["offers"]) == 2
    assert {offer["availability"] for offer in plan["offers"]} == {expected}
    assert {offer["inventory_quantity"] for offer in plan["offers"]} == {quantity}
    assert {seed["availability"] for seed in plan["seeds"]} == {expected}
