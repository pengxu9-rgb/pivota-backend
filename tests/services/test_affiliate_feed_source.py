"""The affiliate datafeed source, and the listing-identity rule it needs.

The feed below is SYNTHETIC (no real Olive Young feed exists yet): column names are invented and
declared through `fields`, exactly as a real job must declare the network's actual header.
"""
from urllib.parse import urlsplit

import pytest

from services.catalog_enrichment_agent import ingestion
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl, retailer_listing_identity
from services.retailer_ingest import affiliate_feed as af

OY = "global.oliveyoung.com"
PAGE = "https://global.oliveyoung.com/product/detail?prdtNo={}"
LINK = "https://invl.example-network.com/c/{}"

FEED = {
    "network": "example_network", "url_env": "AFFILIATE_FEED_OLIVE_YOUNG", "format": "csv",
    "retailer_host": OY, "link_hosts": ["invl.example-network.com"],
    "fields": {"id": "sku_id", "parent_id": "group_id", "title": "name", "brand": "brand_name",
               "product_url": "page", "link": "click", "price": "sale_price", "currency": "ccy",
               "gtin": "ean", "category": "cat", "availability": "stock", "variant_title": "shade",
               "description": "desc", "image": "img"},
}
HEADER = "sku_id,group_id,name,brand_name,page,click,sale_price,ccy,ean,cat,stock,shade,desc,img"


def _row(sku, group, name, brand="3CE", price="18.00", ccy="USD", page=None, click=None, cat="Lip Tint",
         shade="Rose", ean="", stock="in stock"):
    page = page or PAGE.format(group)
    click = click or LINK.format(group)
    return ",".join([sku, group, name, brand, page, click, price, ccy, ean, cat, stock, shade,
                     "A soft velvet colour for lips.", "https://img.example.com/x.jpg"])


def _csv(*rows):
    return "\n".join([HEADER, *rows]) + "\n"


def _records(text, vendors=("3CE",), feed=FEED, currency="USD"):
    rows = af.parse_feed(text, fmt=feed["format"])
    return af.feed_rows_to_records(rows, feed, vendors=list(vendors), category_path="beauty", currency=currency)


# --- listing identity -------------------------------------------------------------------------

def test_two_olive_young_products_are_two_listings():
    a = retailer_listing_identity(OY, PAGE.format("GA230418579"))
    b = retailer_listing_identity(OY, PAGE.format("GA999999999"))
    assert a != b and a == "global.oliveyoung.com/product/detail?prdtNo=GA230418579"


def test_tracking_parameters_and_their_order_do_not_change_the_listing():
    base = retailer_listing_identity(OY, PAGE.format("GA1"))
    assert retailer_listing_identity(OY, "https://global.oliveyoung.com/product/detail?utm_source=x&prdtNo=GA1&dataSource=y") == base


@pytest.mark.parametrize("url", ["https://global.oliveyoung.com/product/detail",
                                 "https://global.oliveyoung.com/product/detail?prdtNo=",
                                 "https://global.oliveyoung.com/product/detail?prdtNo=A&prdtNo=B"])
def test_an_olive_young_url_without_exactly_one_product_id_is_refused(url):
    with pytest.raises(ValueError, match="retailer_listing_identity_unproven"):
        retailer_listing_identity(OY, url)


@pytest.mark.parametrize("host,url", [
    ("k-touch.us", "https://k-touch.us/products/3ce-velvet-lip-tint-4g"),
    ("k-touch.us", "https://k-touch.us/products/3ce-velvet-lip-tint-4g/?variant=123&utm=x"),
    ("sokoglam.com", "https://www.sokoglam.com/products/abc"),
    ("oliveyoung.com", "https://oliveyoung.com/product/detail?prdtNo=GA1"),  # not the listed host
])
def test_every_other_host_is_byte_identical_to_host_plus_path(host, url):
    parsed = urlsplit(url)
    old_rule = host.lower().removeprefix("www.") + parsed.path.rstrip("/")
    assert retailer_listing_identity(host, url) == old_rule


def test_the_plan_gives_each_olive_young_product_its_own_key():
    """The collapse this rule prevents: without it, every product on the store is ONE listing."""
    records = _records(_csv(_row("1", "GA1", "3CE Velvet Lip Tint"), _row("2", "GA2", "3CE Blur Water Tint")))
    plan = ingest_validated_jsonl(records)
    assert len({p["product_key"] for p in plan["pdps"]}) == 2


# --- the feed -----------------------------------------------------------------------------------

def test_rows_become_retailer_records_with_the_page_as_listing_and_the_link_as_click():
    [rec] = _records(_csv(_row("1", "GA1", "3CE Velvet Lip Tint", ean="8809000000017")))
    assert rec["pdp"]["brand"] == "3CE" and rec["pdp"]["source_role"] == "retailer"
    assert rec["pdp"]["category_path"] == "beauty/makeup/lip/tint"
    offer = rec["offers"][0]
    assert offer["canonical_url"] == PAGE.format("GA1") and offer["destination_url"] == LINK.format("GA1")


def test_an_all_digit_network_sku_never_becomes_a_storefront_variant_id():
    """variant_identity classes 8+ digits as merchant-issued; a feed SKU must never be read that way."""
    from services.variant_identity import MERCHANT_ISSUED
    records = _records(_csv(_row("44012345678901", "GA1", "3CE Velvet Lip Tint", shade="Rose"),
                            _row("44012345678902", "GA1", "3CE Velvet Lip Tint", shade="Taupe")))
    assert len(records) == 1 and records[0]["pdp"].get("variants") in (None, [])
    plan = ingest_validated_jsonl(records)
    assert not any(MERCHANT_ISSUED in str(s.get("sku_payload")) for s in plan["skus"])


def test_alphanumeric_variant_ids_still_land_the_product_as_one_listing():
    """services.variant_identity only trusts 8+ digit or Shopify GID variant ids as merchant-issued.
    An Olive Young-style id ("GA230418579-01") is UNVERIFIABLE, so the product lands as ONE canonical
    listing without shade SKUs -- enough for a referral offer; variant identity only matters for
    agent checkout, which a store with no UCP door cannot do anyway."""
    records = _records(_csv(_row("GA1-01", "GA1", "3CE Velvet Lip Tint", shade="Rose"),
                            _row("GA1-02", "GA1", "3CE Velvet Lip Tint", shade="Taupe")))
    assert len(records) == 1 and records[0]["offers"][0]["destination_url"] == LINK.format("GA1")


def test_only_the_cohort_brands_are_kept():
    records = _records(_csv(_row("1", "GA1", "3CE Velvet Lip Tint"),
                            _row("2", "GA2", "Rom&nd Juicy Tint", brand="rom&nd")))
    assert [r["pdp"]["brand"] for r in records] == ["3CE"]


def test_json_feeds_parse_at_a_path():
    feed = {**FEED, "format": "json", "json_path": "data.products"}
    text = ('{"data": {"products": [{"sku_id": "1", "group_id": "GA1", "name": "3CE Velvet Lip Tint", '
            '"brand_name": "3CE", "page": "' + PAGE.format("GA1") + '", "click": "' + LINK.format("GA1") + '", '
            '"sale_price": 18, "ccy": "USD", "ean": null, "cat": "Lip Tint", "stock": "in stock", "shade": "Rose", '
            '"desc": "Soft lips.", "img": ""}]}}')
    rows = af.parse_feed(text, fmt="json", json_path="data.products")
    assert len(af.feed_rows_to_records(rows, feed, vendors=["3CE"], category_path="beauty", currency="USD")) == 1


@pytest.mark.parametrize("kw,match", [
    (dict(ccy="KRW"), "priced in KRW"),
    (dict(click="https://evil.example.org/redirect?to=x"), "link host"),
    (dict(click="http://invl.example-network.com/c/1"), "link host"),
    (dict(page="https://global.oliveyoung.com.evil.io/product/detail?prdtNo=GA1"), "product_url"),
])
def test_untrusted_rows_refuse_the_feed(kw, match):
    with pytest.raises(af.FeedError, match=match):
        _records(_csv(_row("1", "GA1", "3CE Velvet Lip Tint", **kw)))


def test_a_mapped_column_missing_from_the_header_refuses():
    feed = {**FEED, "fields": {**FEED["fields"], "gtin": "barcode_upc"}}
    with pytest.raises(af.FeedError, match="missing from the feed header"):
        _records(_csv(_row("1", "GA1", "3CE Velvet Lip Tint")), feed=feed)


@pytest.mark.parametrize("bad", [
    {**FEED, "url_env": "https://feed.example/with-token"},            # a URL, not an env var name
    {**FEED, "fields": {k: v for k, v in FEED["fields"].items() if k != "link"}},
    {**FEED, "fields": {**FEED["fields"], "made_up": "x"}},
    {**FEED, "format": "xml"},
    {**FEED, "link_hosts": ["https://invl.example-network.com"]},
    {**FEED, "surprise": 1},
    {**FEED, "url_env": "DATABASE_URL"},                               # another secret the job holds
])
def test_feed_options_are_validated(bad):
    with pytest.raises(af.FeedError):
        af.validate_feed_options(bad)


async def test_the_feed_url_comes_from_the_environment_never_the_job():
    with pytest.raises(af.FeedError, match="AFFILIATE_FEED_OLIVE_YOUNG is not set"):
        await af.fetch_feed_text(FEED, env={})


# --- primary readiness: the native-variant rule and its one excuse ----------------------------------

def test_the_merchant_is_the_retailer_and_the_provenance_is_the_feed():
    [rec] = _records(_csv(_row("GA1-01", "GA1", "3CE Velvet Lip Tint")))
    offer = rec["offers"][0]
    assert offer["merchant_inferred"] == OY          # never the network: it sells nothing
    assert offer["validated_at"] == "affiliate_feed:example_network"


def test_a_feed_only_product_is_ready_without_a_storefront_variant_id():
    from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan
    plan = ingest_validated_jsonl(_records(_csv(_row("GA1-01", "GA1", "3CE Velvet Lip Tint"))))
    report = inspect_primary_plan(plan)
    assert report["status"] == "ready_to_apply", report["reasons"]


def test_one_unstamped_offer_brings_the_native_variant_rule_back():
    import json
    from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan
    plan = ingest_validated_jsonl(_records(_csv(_row("GA1-01", "GA1", "3CE Velvet Lip Tint"))))
    extra = dict(plan["offers"][0])
    payload = json.loads(extra["offer_payload"]) if isinstance(extra["offer_payload"], str) else dict(extra["offer_payload"])
    extra["offer_id"] += ":storefront"
    extra["offer_payload"] = json.dumps({**payload, "validated_at": "shopify_products_json"})
    plan["offers"].append(extra)
    assert "no_native_retailer_commerce_chain" in inspect_primary_plan(plan)["reasons"]


def test_a_storefront_product_with_an_unplaceable_variant_id_is_still_blocked():
    from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan
    from services.curated_brand_feed import shopify_product_to_record
    rec = shopify_product_to_record(
        {"id": 1, "vendor": "3CE", "title": "3CE Velvet Lip Tint", "handle": "velvet-lip-tint",
         "product_type": "Lip Tint", "body_html": "<p>For lips.</p>", "images": [],
         "variants": [{"id": "abc", "price": "18.00", "available": True}]},
        domain="k-touch.us", category_path="beauty", brand_override="3CE", currency="USD",
        source_role="retailer", retailer_name="k-touch.us", emit_native_variants=True)
    assert "no_native_retailer_commerce_chain" in inspect_primary_plan(ingest_validated_jsonl([rec]))["reasons"]


# --- hardening (review of #2272) ------------------------------------------------------------------

@pytest.mark.parametrize("kw,match", [
    (dict(click="https://evil.example\\.invl.example-network.com/c/1"), "link host"),   # backslash
    (dict(click="https://invl.example-network.com:8443/c/1"), "link host"),
    (dict(click="https://user@invl.example-network.com/c/1"), "link host"),
    (dict(page="https://shop.global.oliveyoung.com/product/detail?prdtNo=GA1"), "product_url"),
    (dict(page="https://global.oliveyoung.com/product/detail"), "retailer_listing_identity_unproven"),
])
def test_lookalike_and_unlistable_urls_refuse_the_feed(kw, match):
    with pytest.raises(af.FeedError, match=match):
        _records(_csv(_row("1", "GA1", "3CE Velvet Lip Tint", **kw)))


@pytest.mark.parametrize("second", [
    dict(page=PAGE.format("GA9")),                       # same parent, another listing
    dict(click=LINK.format("other")),                    # same parent, another click
])
def test_rows_of_one_product_that_disagree_refuse_the_feed(second):
    with pytest.raises(af.FeedError, match="disagree"):
        _records(_csv(_row("1", "GA1", "3CE Velvet Lip Tint"), _row("2", "GA1", "3CE Velvet Lip Tint", **second)))


def test_a_row_id_equal_to_another_rows_parent_does_not_merge_into_it():
    text = _csv(_row("GA1", "", "3CE Glow Lip Tint", page=PAGE.format("GA1")),
                _row("7", "GA1", "3CE Velvet Lip Tint", page=PAGE.format("GA2"), click=LINK.format("GA2")))
    assert len(_records(text)) == 2


def test_a_repeated_row_id_refuses_the_feed():
    with pytest.raises(af.FeedError, match="repeated"):
        _records(_csv(_row("1", "GA1", "3CE Velvet Lip Tint"), _row("1", "GA2", "3CE Blur Water Tint")))


def test_a_bom_and_a_long_html_description_parse():
    text = "\ufeff" + _csv(_row("1", "GA1", "3CE Velvet Lip Tint")).replace(
        "A soft velvet colour for lips.", "<p>" + "lips " * 40_000 + "</p>")
    assert len(_records(text)) == 1


def test_an_olive_young_listing_has_a_handle_approvals_can_name():
    from scripts.onboard_curated_brands import _exclude_by_handle
    from services.retailer_ingest.detectors import _handle
    records = _records(_csv(_row("1", "GA1", "3CE Velvet Lip Tint"), _row("2", "GA2", "3CE Blur Water Tint")))
    assert {_handle(r) for r in records} == {"ga1", "ga2"}
    kept, matched = _exclude_by_handle(records, {"ga2"}, domain=OY)
    assert matched == {"ga2"} and [r["pdp"]["product_name"] for r in kept] == ["3CE Velvet Lip Tint"]


def test_storefront_handles_are_unchanged():
    from services.catalog_enrichment_agent.ingestion import listing_handle
    assert listing_handle("https://k-touch.us/products/3CE-Velvet/?variant=1#x") == "3ce-velvet"
    assert listing_handle("https://k-touch.us/collections/lip") is None


def _transport(monkeypatch, handler):
    import httpx
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


FEED_ENV = {"AFFILIATE_FEED_OLIVE_YOUNG": "https://feeds.example-network.com/p/1?token=SEKRET"}


async def test_a_redirect_off_https_is_refused(monkeypatch):
    import httpx
    _transport(monkeypatch, lambda req: httpx.Response(302, headers={"location": "http://feeds.example-network.com/f"}))
    with pytest.raises(af.FeedError, match="off https"):
        await af.fetch_feed_text(FEED, env=FEED_ENV)


async def test_an_https_redirect_is_followed_and_a_bom_is_dropped(monkeypatch):
    import httpx
    def handler(req):
        if req.url.path == "/p/1":
            return httpx.Response(302, headers={"location": "/final.csv"})
        return httpx.Response(200, content="\ufeffa,b\n1,2\n".encode("utf-8"))
    _transport(monkeypatch, handler)
    assert await af.fetch_feed_text(FEED, env=FEED_ENV) == "a,b\n1,2\n"


async def test_an_oversized_feed_is_refused_while_streaming(monkeypatch):
    import httpx
    monkeypatch.setattr(af, "MAX_FEED_BYTES", 1000)
    _transport(monkeypatch, lambda req: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(af.FeedError, match="size cap"):
        await af.fetch_feed_text(FEED, env=FEED_ENV)


def test_a_link_hiding_a_tab_is_refused():
    """urlsplit silently DROPS tabs and newlines, so only the raw-character check sees this."""
    with pytest.raises(af.FeedError, match="link host"):
        _records(_csv(_row("1", "GA1", "3CE Velvet Lip Tint", click="https://invl.example-network.com/c/\t1")))


async def test_an_oversized_feed_without_a_declared_length_is_refused_while_streaming(monkeypatch):
    import httpx
    monkeypatch.setattr(af, "MAX_FEED_BYTES", 1000)

    async def body():
        for _ in range(10):
            yield b"x" * 600
    _transport(monkeypatch, lambda req: httpx.Response(200, content=body()))
    with pytest.raises(af.FeedError, match="size cap"):
        await af.fetch_feed_text(FEED, env=FEED_ENV)
