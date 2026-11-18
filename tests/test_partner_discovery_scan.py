from email.message import Message
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from scripts.ops.partner_discovery import manifest, scan


HOST = "brand.example"
ORIGIN = f"https://{HOST}"
PDP = f"{ORIGIN}/products/exact-lipstick"


@pytest.fixture(autouse=True)
def placeholder_vendor_aliases(monkeypatch):
    """A brand and its storefront vendor spelling differ; a placeholder alias family stands in."""
    monkeypatch.setattr(scan, "BRAND_VENDOR_ALIASES", ({"A.B.C", "ABC Cosmetics"},))


def query(**changes):
    return {
        "line": 1, "meitu_id": "fixture-lipstick", "brand": "A.B.C", "name": "Exact Lipstick",
        "search_names": [], "platform": "shopify", "host": HOST, "anchor_url": PDP, **changes,
    }


def payload(row, batch="fixture-round-one"):
    return {"schema_version": 1, "market": "US", "batch_id": batch, "queries": [row]}


def native_product(handle="exact-lipstick", vendor="ABC Cosmetics", country="US", variants=None):
    headers = Message()
    headers.add_header("Set-Cookie", "cart_currency=USD; Path=/")
    headers.add_header("Set-Cookie", f"localization={country}; Path=/")
    return headers, json.dumps({
        "id": 70, "handle": handle, "title": "Exact Lipstick", "vendor": vendor,
        "variants": [variant(101, "KNOWN-SKU")] if variants is None else variants,
    }).encode()


def variant(native_id, sku=None, price=2400):
    return {"id": native_id, "title": f"Shade {native_id}", "barcode": "617037660635", "sku": sku,
            "price": price, "available": True}


def suggestions(*handles):
    return {}, json.dumps({"resources": {"results": {"products": [
        {"handle": handle, "title": "Exact Lipstick"} for handle in handles
    ]}}}).encode()


def observe(data, fetch):
    rows, summary = scan.run(data, fetch)
    assert summary["query_count"] == len(rows) == 1
    assert rows[0]["candidate_count"] == len(rows[0]["candidates"])
    return rows[0]


def test_anchor_precedes_search_and_qualification_precedes_variant_sampling():
    calls = []

    def fetch(url, _limit):
        calls.append(url)
        path = urlsplit(url).path
        if path == "/products/exact-lipstick.js":
            variants = [variant(native_id) for native_id in range(201, 225)]
            return native_product(variants=[*variants, variant(999, "ONLY-VALID-SKU")])
        if path == "/search/suggest.json":
            return suggestions(*(f"broken-{i}" for i in range(5)))
        if path == "/sitemap.xml":
            return {}, b"<urlset/>"
        raise HTTPError(url, 404, "Missing fixture", {}, None)

    first = payload(query(anchor_url=f"{ORIGIN}/collections/lips/products/exact-lipstick"))
    receipt = observe(first, fetch)
    assert calls[0] == PDP + ".js?country=US"
    assert receipt["coverage"]["anchor_outcome"]["url"] == PDP
    candidate = receipt["candidates"][0]
    assert candidate["status"] == "original_title_candidate"
    evidence = candidate["evidence"]
    assert evidence["variants"][0]["sku"] == "ONLY-VALID-SKU"
    assert evidence["in_stock_gtin_sku_variant_count"] == 1
    assert receipt["coverage"]["deferred_candidate_count"] == 3

    second = manifest.resume(first, [receipt], "fixture-round-two")
    assert second["queries"][0]["variant_cursors"][PDP]["offset"] == 24
    receipt = observe(second, fetch)
    assert [v["variant_id"] for v in receipt["candidates"][0]["evidence"]["variants"]] == ["224"]
    third = manifest.resume(second, [receipt], "fixture-round-three")
    assert third["queries"][0]["cursor"]["candidate_offset"] == 2


@pytest.mark.parametrize(("changed", "candidate_status", "detail"), [
    ({"handle": "other-lipstick"}, "shopify_identity_needs_review", "handle_mismatch"),
    ({"vendor": "Another Maker"}, "shopify_identity_needs_review", "vendor_mismatch"),
    ({"country": "CA"}, "product_evidence_unavailable", "market_conflict"),
])
def test_native_review_holds_conflicting_handle_vendor_or_market(changed, candidate_status, detail):
    def fetch(url, _limit):
        path = urlsplit(url).path
        if path.endswith(".js"):
            return native_product(**changed)
        if path == "/search/suggest.json":
            return suggestions()
        assert path == "/sitemap.xml"
        return {}, b"<urlset/>"

    candidate, = observe(payload(query()), fetch)["candidates"]
    assert candidate["status"] == candidate_status
    evidence = candidate["evidence"]
    assert evidence.get("identity_status", evidence["status"]) == detail


def test_sitemap_continuation_keeps_blocked_prefix_and_reaches_an_ordinary_child(monkeypatch):
    monkeypatch.setattr(scan, "MAX_SITEMAPS", 2)

    def fetch(url, _limit):
        path = urlsplit(url).path
        if path == "/sitemap.xml":
            return {}, (f"<sitemapindex><sitemap><loc>{ORIGIN}/sitemap-a.xml</loc></sitemap>"
                        f"<sitemap><loc>{ORIGIN}/sitemap-b.xml</loc></sitemap></sitemapindex>").encode()
        if path == "/sitemap-a.xml":
            raise HTTPError(url, 403, "Held fixture", {}, None)
        if path == "/sitemap-b.xml":
            return {}, f"<urlset><url><loc>{PDP}</loc></url></urlset>".encode()
        assert path == "/products/exact-lipstick.js"
        return native_product()

    first = payload(query(platform="other", anchor_url=None))
    initial = observe(first, fetch)
    assert initial["coverage"]["sitemap"]["deferred_sitemap_sample"] == [f"{ORIGIN}/sitemap-b.xml"]
    second = manifest.resume(first, [initial], "fixture-round-two")
    receipt = observe(second, fetch)
    assert receipt["candidates"][0]["url"] == PDP
    assert receipt["coverage"]["sitemap"]["prefix_failed_source_count"] == 1
    assert receipt["coverage"]["status"] == "blocked"
    assert manifest.resume(second, [receipt], "fixture-round-three")["queries"]
