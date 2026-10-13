"""One page, one offer, whichever way the crawl spells the store's host (ingestion.respell_listing_host).

A content-keyed row is the same row for `www.<host>/products/x` and `<host>/products/x` (content_listing drops
`www.`), but derive_offer_id and derive_seed_id hash the URL as spelled. Measured on prod 2026-10-13: a brand store
crawled as `www.` in September and as the apex in October kept two live canonical offers per row, one per
spelling, both refreshed nightly. Every record here is built by the real producer
(curated_brand_feed.shopify_product_to_record).
"""
import asyncio
import json
from urllib.parse import urlsplit

from services.catalog_enrichment_agent.apply import current_listings, plan_with_current_listings
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl, respell_listing_host
from tests.services.test_same_title_listing_collision import _Catalog, official

APEX, WWW = "store.example", "www.store.example"
ITEM = ("Base Duo", "set", "base-duo", "64.00", 42_000_000_000_001)


def _record(domain):
    return official(*ITEM, domain=domain, vendor="Store Brand")


def _ids(plan, key="offers", field="offer_id"):
    return sorted(row[field] for row in plan[key])


def _hosts(plan):
    return {urlsplit(json.loads(o["offer_payload"])["destination_url"]).hostname for o in plan["offers"]}


def _stored(plan):
    """What apply.current_listings returns for the rows `plan` wrote."""
    catalog = _Catalog({p["product_key"]: p["canonical_url"] for p in plan["pdps"]})
    return asyncio.run(current_listings([p["product_key"] for p in plan["pdps"]], db=catalog))


def test_the_two_spellings_mint_two_offers_without_the_row_s_url():
    """The defect: without the row's stored URL a re-crawl under the other spelling mints a second set."""
    www, apex = ingest_validated_jsonl([_record(WWW)]), ingest_validated_jsonl([_record(APEX)])
    assert _ids(www) and set(_ids(www)).isdisjoint(_ids(apex))
    assert [p["product_key"] for p in www["pdps"]] == [p["product_key"] for p in apex["pdps"]]


def test_a_recrawl_under_the_other_spelling_refreshes_the_row_s_own_offers_and_seeds():
    for first, then in ((WWW, APEX), (APEX, WWW)):
        before = ingest_validated_jsonl([_record(first)])
        stored = _stored(before)
        assert [len(v) for v in stored.values()] == [3]
        plan = ingest_validated_jsonl([_record(then)], current_listings=stored)
        assert _ids(plan) == _ids(before)
        assert _ids(plan, "seeds", "id") == _ids(before, "seeds", "id")
        assert _hosts(plan) == {first}
        assert [p["canonical_url"] for p in plan["pdps"]] == [p["canonical_url"] for p in before["pdps"]]
        [named] = plan["listing_respellings"]
        assert named["stored"] == before["pdps"][0]["canonical_url"] and urlsplit(named["crawled"]).hostname == then
        assert plan["listing_moves"] == [] and plan["listing_collisions"] == []


def test_the_same_spelling_is_untouched():
    before = ingest_validated_jsonl([_record(WWW)])
    plan = ingest_validated_jsonl([_record(WWW)], current_listings=_stored(before))
    assert _ids(plan) == _ids(before) and plan["listing_respellings"] == []


def test_the_plan_reads_the_spelling_through_plan_with_current_listings():
    before = ingest_validated_jsonl([_record(WWW)])
    catalog = _Catalog({p["product_key"]: p["canonical_url"] for p in before["pdps"]})
    plan = asyncio.run(plan_with_current_listings([_record(APEX)], db=catalog))
    assert _ids(plan) == _ids(before) and _hosts(plan) == {WWW}
    assert plan["current_listings"] == {"status": "read", "rows": 1}


def test_only_www_is_the_same_host():
    record = _record("us.store.example")
    assert respell_listing_host(record, f"https://{APEX}/products/base-duo") is record
    for stored in (f"https://{APEX}:8443/products/base-duo", "not a url", "https://[bad/x", "", None):
        www = _record(WWW)
        assert respell_listing_host(www, stored) is www
    crawled = _record(WWW)
    crawled["offers"][0]["destination_url"] = f"https://user:pw@{WWW}/products/base-duo"
    out = respell_listing_host(crawled, f"https://{APEX}/products/base-duo")
    assert out["offers"][0]["destination_url"] == f"https://user:pw@{WWW}/products/base-duo"
    assert out["offers"][0]["canonical_url"] == f"https://{APEX}/products/base-duo"


def test_a_folded_variant_s_own_listing_is_respelled_with_its_offer():
    record = _record(WWW)
    record["pdp"]["variants"][0]["own_listing_url"] = f"https://{WWW}/products/base-duo-light"
    out = respell_listing_host(record, f"https://{APEX}/products/base-duo")
    assert out["pdp"]["variants"][0]["own_listing_url"] == f"https://{APEX}/products/base-duo-light"
    assert record["pdp"]["variants"][0]["own_listing_url"] == f"https://{WWW}/products/base-duo-light"  # a copy
    assert {o["destination_url"] for o in out["offers"]} == {f"https://{APEX}/products/base-duo"}
