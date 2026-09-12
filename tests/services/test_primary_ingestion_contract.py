"""Primary entry points cannot report substituted or partial data as success."""

import json
from unittest.mock import AsyncMock

import pytest

from services import curated_brand_feed as feed, catalog_onboard_worker as worker
from services.catalog_enrichment_agent import ingestion
from services.catalog_enrichment_agent.primary_ingestion import (
    PrimaryIngestionIncomplete,
    inspect_primary_plan,
    require_primary_apply,
)
from scripts import onboard_curated_brands as cli
from tests.services.test_curated_gtin_handoff import captured_records


def batch(records):
    return feed.CuratedRecordBatch(records, crawl_report={"status": "complete", "pages": 1})


@pytest.mark.parametrize("currency", [None, "", "not currency", {"code": "USD"}])
def test_direct_planner_and_offer_builder_never_supply_usd(currency):
    record = captured_records()[0]
    record["pdp"]["currency"] = currency
    with pytest.raises(ValueError, match="currency_unproven"):
        ingestion.ingest_validated_jsonl([record])
    with pytest.raises(ValueError, match="currency_unproven"):
        ingestion._build_offer_inserts(product_key="p", sku_key="s", offers=[], currency=currency)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["retailer", "brand_official"])
async def test_unknown_currency_stops_main_enumerator_without_expected_currency(monkeypatch, role):
    monkeypatch.setattr(feed, "fetch_shopify_products", AsyncMock(return_value=[]))
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", AsyncMock(return_value={"currency": None}))
    with pytest.raises(feed.CurrencyNotProven):
        await feed.records_for_brand(domain="eyurs.com", category_path="beauty", source_role=role)


def unresolved_record():
    product = {
        "id": 9000001,
        "title": "Mystery Item",
        "handle": "mystery",
        "vendor": "A'PIEU",
        "product_type": "",
        "tags": ["daily"],
        "body_html": "<p>" + ("Observed descriptive copy. " * 8) + "</p>",
        "images": [{"src": "https://cdn.example/item.jpg"}],
        "variants": [{"id": 45000000000001, "price": "10.00", "available": True}],
    }
    return feed.shopify_product_to_record(
        product,
        domain="eyurs.com",
        category_path="beauty/skincare",
        currency="USD",
        source_role="retailer",
        emit_native_variants=True,
    )


def test_missing_leaf_stays_explicitly_unresolved_and_draft_even_with_publishable_content():
    record = unresolved_record()
    assert record["pdp"]["category_path"] is None
    assert record["pdp"]["category_input_path"] == "beauty/skincare"
    assert record["pdp"]["category_resolution_status"] == "unresolved"
    plan = ingestion.ingest_validated_jsonl([record])
    row = plan["pdps"][0]
    assert row["category_path"] is None and row["pdp_lifecycle_stage"] == "draft"
    assert json.loads(row["product_payload"])["enrichment_meta"]["category_resolution_status"] == "unresolved"
    assert inspect_primary_plan(plan)["status"] == "blocked"


@pytest.mark.asyncio
async def test_worker_never_applies_unresolved_category(monkeypatch):
    monkeypatch.setattr(worker, "records_for_brand", AsyncMock(return_value=batch([unresolved_record()])))
    apply = AsyncMock()
    monkeypatch.setattr(worker, "apply_ingest_plan", apply)
    with pytest.raises(PrimaryIngestionIncomplete) as error:
        await worker._process_curated_brand({"domain": "eyurs.com", "source_role": "retailer"}, apply=True, db=None)
    assert error.value.report["unresolved_category_count"] == 1
    apply.assert_not_awaited()
    dry = await worker._process_curated_brand({"domain": "eyurs.com", "source_role": "retailer"}, apply=False, db=None)
    assert dry["primary_ingestion"]["status"] == "blocked"


def test_cli_refuses_unresolved_before_database_connect(monkeypatch):
    fake = AsyncMock(return_value=batch([unresolved_record()]))
    fake.last_vendor_filter_report = None
    fake.last_brand_census = None
    monkeypatch.setattr(cli, "records_for_brand", fake)
    apply = AsyncMock()
    monkeypatch.setattr(cli, "apply_ingest_plan", apply)
    assert cli.main(["--domain", "eyurs.com", "--category", "beauty", "--apply"]) == 2
    apply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "counts",
    [
        {"pdps": 2, "skus": 0, "offers": 0},
        {"pdps": 2, "skus": 4, "offers": 0},
        {"pdps": 2, "skus": 4, "offers": 3},
        {"pdps": 1, "skus": 4, "offers": 4},
    ],
)
async def test_worker_reports_partial_writes_as_failure(monkeypatch, counts):
    monkeypatch.setattr(worker, "records_for_brand", AsyncMock(return_value=batch(captured_records())))
    monkeypatch.setattr(worker, "apply_ingest_plan", AsyncMock(return_value=counts))
    with pytest.raises(PrimaryIngestionIncomplete) as error:
        await worker._process_curated_brand({"domain": "eyurs.com"}, apply=True, db=None)
    assert error.value.report["status"] == "partial"
    assert error.value.report["applied"] == counts


@pytest.mark.asyncio
async def test_absent_gtin_is_valid_but_success_never_claims_search_or_shared_identity_proof(monkeypatch):
    records = captured_records()
    for record in records:
        record["pdp"]["barcode"] = None
        for variant in record["pdp"]["variants"]:
            variant["barcode"] = None
    monkeypatch.setattr(worker, "records_for_brand", AsyncMock(return_value=batch(records)))
    plan = ingestion.ingest_validated_jsonl(records)
    counts = {name: len(plan[name]) for name in ("pdps", "skus", "offers")}
    monkeypatch.setattr(worker, "apply_ingest_plan", AsyncMock(return_value=counts))
    outcome = await worker._process_curated_brand({"domain": "eyurs.com"}, apply=True, db=None)
    assert outcome["primary_ingestion"]["status"] == "applied"
    assert outcome["primary_ingestion"]["primary_search_verified"] is False
    assert outcome["primary_ingestion"]["shared_identity_verified"] is False
    assert all(p["gtin"] is None for p in plan["pdps"])


def test_natural_key_sku_adoption_can_reduce_rows_without_losing_offers():
    report = {"planned": {"pdps": 1, "skus": 3, "offers": 2}, "reasons": []}
    assert (
        require_primary_apply(report, {"pdps": 1, "skus": 2, "offers": 2, "skus_deduped_same_identity": 1})["status"]
        == "applied"
    )


def test_missing_native_sku_cannot_be_hidden_by_a_surviving_canonical_sku():
    with pytest.raises(PrimaryIngestionIncomplete):
        require_primary_apply({"planned": {"pdps": 1, "skus": 3, "offers": 2}}, {"pdps": 1, "skus": 2, "offers": 2})


@pytest.mark.parametrize(
    "product_type,expected",
    [
        ("Lip Scrub", "beauty/makeup/lip/balm"),
        ("Sun Protection", "beauty/skincare/sun/sunscreen"),
        ("Foot Care", "beauty/body/care"),
        ("Footwear", None),
        ("Foot Care Shoes", None),
        ("Sun Protection Accessories", None),
        ("Lip Scrub & Cleanser", None),
    ],
)
def test_primary_merchant_types_resolve_only_supported_product_evidence(product_type, expected):
    product = {
        "id": 9000001,
        "title": "Observed Product",
        "handle": "item",
        "vendor": "A'PIEU",
        "product_type": product_type,
        "variants": [{"id": 45000000000001, "price": "10.00", "available": True}],
    }
    rec = feed.shopify_product_to_record(
        product, domain="eyurs.com", category_path="beauty", currency="USD", source_role="retailer"
    )
    assert rec["pdp"]["category_path"] == expected
    assert rec["pdp"]["category_source_product_type"] == product_type
    planned = ingestion.ingest_validated_jsonl([rec])
    metadata = json.loads(planned["pdps"][0]["product_payload"])["enrichment_meta"]
    assert metadata["category_source_product_type"] == product_type
    assert rec["pdp"]["category_resolution_status"] == ("resolved" if expected else "unresolved")


def test_direct_jsonl_missing_category_cannot_claim_resolved_or_published():
    record = captured_records()[0]
    record["pdp"]["category_path"] = None
    record["pdp"]["category_resolution_status"] = "resolved"
    record["pdp"]["attribute_summary"] = "Observed descriptive copy. " * 8
    record["pdp"]["tags"] = ["daily"]
    row = ingestion.ingest_validated_jsonl([record])["pdps"][0]
    assert row["pdp_lifecycle_stage"] == "draft"
    assert json.loads(row["product_payload"])["enrichment_meta"]["category_resolution_status"] == "unresolved"
