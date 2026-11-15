"""Production refresh only accepts its two exact Shopify shades and US currency."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from scripts.ops import surratt_retailer_offers as refresh
from services.catalog_enrichment_agent.apply import apply_ingest_plan
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
from services.catalog_enrichment_agent.surratt_retailer_offers import scope_reviewed_plan
from services.surratt_retailer_pilot import PROOF_KEY, RETAILERS


def payload(listing):
    return {
        "id": int(listing.native_product_id), "handle": "surratt-lipslique", "vendor": "Surratt",
        "title": "Lipslique", "type": "Lipstick", "description": "Surratt Lipslique",
        "variants": [
            {"id": int(vid), "title": title, "option1": title, "sku": title,
             "barcode": gtin.lstrip("0"), "price": minor, "available": available,
             "featured_image": {"src": "https://cdn.example.test/surratt.jpg"}}
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
    shaped = scope_reviewed_plan(ingest_validated_jsonl([record], market="US", include_reviewed_surratt=True))
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
    generic = ingest_validated_jsonl([held], market="US", include_reviewed_surratt=True)
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
        raw, domain=listing.host, source_role="retailer", retailer_name="Bluemercury",
        category_path="beauty/makeup/lip/lipstick", currency="USD",
    )
    plan = ingest_validated_jsonl([held, sibling], market="US")
    assert len(plan["pdps"]) == 1 and plan["pdps"][0]["canonical_url"].endswith("/another-lipstick")
    assert plan["seeds"] and all(row["attached_product_key"] == plan["pdps"][0]["product_key"] for row in plan["seeds"])
    assert all(json.loads(row["offer_payload"])["destination_url"].endswith("/another-lipstick") for row in plan["offers"])
