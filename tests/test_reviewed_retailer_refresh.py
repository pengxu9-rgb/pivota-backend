"""Production refresh only accepts its two exact Shopify shades and US currency."""

from datetime import datetime, timezone
import importlib
from unittest.mock import AsyncMock

import httpx
import pytest

from services.catalog_enrichment_agent.apply import apply_ingest_plan
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
from services.catalog_enrichment_agent.reviewed_retailer_offers import scope_reviewed_plan
from services.exact_retailer_cohorts import COHORT_A_CONFIG, COHORT_B_CONFIG, PROOF_KEY, RETAILERS, RETAILER_COHORT_B

# The refresh job entrypoint keeps its deployed module path; the config names it.
refresh = importlib.import_module(COHORT_A_CONFIG["refresh"]["module"])


REFRESH = refresh.REFRESH
(COHORT_B_LISTING,) = RETAILER_COHORT_B.listings.values()


def payload(listing):
    return {
        "id": int(listing.native_product_id), "handle": REFRESH["handle"], "vendor": REFRESH["vendor_names"][0],
        "title": REFRESH["title_token"].capitalize(), "type": "Lipstick", "description": "Fixture lip color",
        "variants": [
            {"id": int(vid), "title": title, "option1": title, "sku": title,
             "barcode": gtin.lstrip("0"), "price": minor, "available": available,
             "featured_image": {"src": "https://cdn.example.test/lip-color.jpg"}}
            for (vid, (_, gtin)), (title, minor, available) in zip(
                listing.variants.items(), (("Nu De Soleil", 3400, True), ("Paramour", 3525, False)), strict=True)
        ],
        "options": [{"name": "Color", "position": 1}],
    }


@pytest.mark.parametrize("listing", RETAILERS.values())
def test_production_observation_uses_stock_projection_and_distinct_native_facts(listing):
    import json

    now = datetime.now(timezone.utc)
    record = refresh.storefront_record(listing, payload(listing), now)
    shaped = scope_reviewed_plan(ingest_validated_jsonl([record], market="US", include_reviewed_cohort_a=True))
    assert shaped["pdps"][0]["pivota_signature_id"] is None
    assert shaped["pdps"][0]["pivota_canonical_url"] is None
    offers = [json.loads(row["offer_payload"])[PROOF_KEY] for row in shaped["offers"]]
    assert {row["merchant_id"] for row in shaped["offers"]} == {listing.merchant_id}
    assert {(row["price_minor"], row["availability"]) for row in offers} == {
        (3400, "in_stock"), (3525, "out_of_stock"),
    }
    assert {row["observed_at"] for row in offers} == {now.isoformat()}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,cookie", [(200, ""), (200, "cart_currency=CAD"), (302, "cart_currency=USD")])
async def test_missing_market_or_redirect_cannot_refresh(monkeypatch, status, cookie):
    listing = next(iter(RETAILERS.values()))
    remote = f"{listing.family_url}.js?country=US"

    def shop(request):
        assert str(request.url) == remote
        assert request.headers["cache-control"] == "no-cache"
        return httpx.Response(status, json=payload(listing), headers={"Set-Cookie": cookie})

    http_client = httpx.AsyncClient
    monkeypatch.setattr(refresh.httpx, "AsyncClient", lambda **kw: http_client(transport=httpx.MockTransport(shop), **kw))
    monkeypatch.setattr(refresh.crawl_politeness, "before_request", AsyncMock())
    with pytest.raises(ValueError, match="did not confirm US storefront currency"):
        await refresh.fetch_storefront(listing)


def test_wrong_or_missing_stock_cannot_replace_existing_observation():
    listing = next(iter(RETAILERS.values()))
    changed = payload(listing)
    changed["variants"][0]["available"] = None
    with pytest.raises(ValueError, match="price or stock is unproven"):
        refresh.storefront_record(listing, changed, datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_stock_writer_cannot_publish_family_offer_or_active_seed_for_scope():
    listing = next(iter(RETAILERS.values()))
    held = refresh.storefront_record(listing, payload(listing), datetime.now(timezone.utc))
    generic = ingest_validated_jsonl([held], market="US", include_reviewed_cohort_a=True)
    assert generic["seeds"]
    with pytest.raises(ValueError, match="require the scoped retailer writer"):
        await apply_ingest_plan(generic, batch_label="unreviewed-refresh", db=object(), market="US")


def test_mixed_generic_plan_holds_only_reviewed_listing():
    import json

    listing = next(iter(RETAILERS.values()))
    held = refresh.storefront_record(listing, payload(listing), datetime.now(timezone.utc))
    raw = {**payload(listing), "handle": "another-lipstick", "id": 123456789}
    from services.curated_brand_feed import shopify_product_to_record

    sibling = shopify_product_to_record(
        raw, domain=listing.host, source_role="retailer", retailer_name=refresh.STORE_NAMES[listing.host],
        category_path="beauty/makeup/lip/lipstick", currency="USD",
    )
    plan = ingest_validated_jsonl([held, sibling], market="US")
    assert len(plan["pdps"]) == 1 and plan["pdps"][0]["canonical_url"].endswith("/another-lipstick")
    assert plan["seeds"] and all(row["attached_product_key"] == plan["pdps"][0]["product_key"] for row in plan["seeds"])
    assert all(json.loads(row["offer_payload"])["destination_url"].endswith("/another-lipstick") for row in plan["offers"])


@pytest.mark.asyncio
async def test_cohort_b_bare_listing_cannot_enter_the_generic_or_reviewed_writer():
    from services.catalog_enrichment_agent.ingestion import retailer_product_key
    from services.curated_brand_feed import shopify_product_to_record
    from services.exact_retailer_cohorts import COHORT_B_BARE_FAMILY_URL, COHORT_B_BARE_LISTING_KEY, RETAILER_COHORT_B

    raw = {
        "id": int(COHORT_B_LISTING.native_product_id), "handle": COHORT_B_CONFIG["refresh"]["handle"],
        "title": "Fixture Lip Oil", "vendor": COHORT_B_CONFIG["refresh"]["vendor"], "product_type": "Makeup", "images": [{"src": "https://cdn.example/lip.jpg"}],
        "variants": [{"id": 51629126123840, "barcode": "8800299133494", "price": "18.99", "available": True}],
    }
    record = shopify_product_to_record(raw, domain=COHORT_B_LISTING.host, source_role="retailer", retailer_name="Fixture Retailer",
                                       category_path="beauty/makeup/lip/lip_oil", currency="USD")
    assert record["offers"][0]["destination_url"] == COHORT_B_BARE_FAMILY_URL
    assert retailer_product_key(COHORT_B_LISTING.host, COHORT_B_BARE_FAMILY_URL) == COHORT_B_BARE_LISTING_KEY
    plan = ingest_validated_jsonl([record], market="US", reviewed_retailer_keys=(COHORT_B_BARE_LISTING_KEY,))
    assert plan["skipped_reasons"] == {COHORT_B_CONFIG["ingest_skip_reason"]: 1}
    assert not plan["pdps"] and not plan["seeds"]
    with pytest.raises(ValueError, match="unlocalized cohort B listing is reserved"):
        await apply_ingest_plan({"pdps": [{"product_key": COHORT_B_BARE_LISTING_KEY}],
                                 "reviewed_secondary_group": RETAILER_COHORT_B.canonical_group_id},
                                batch_label="unlocalized-test", db=object(), market="US")
