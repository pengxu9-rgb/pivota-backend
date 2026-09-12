"""Redirect provenance is part of the feed/price/currency contract."""
from unittest.mock import AsyncMock
import httpx
import pytest
from services import curated_brand_feed as feed


def product(number):
    return {"id": number, "title": "Honey Milk Lip Oil", "product_type": "Lip Oil",
            "handle": f"oil-{number}", "vendor": "A'PIEU",
            "images": [{"src":"https://cdn.example/i.jpg"}],
            "variants": [{"id":45000000000000+number,"price":"5.00","available":True}]}


def install(monkeypatch, handler):
    factory = httpx.AsyncClient
    monkeypatch.setattr(feed.httpx, "AsyncClient", lambda **kw: factory(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(feed.crawl_politeness, "before_request", AsyncMock())
    monkeypatch.setattr(feed.crawl_politeness, "note_response", lambda *a, **kw:None)
    monkeypatch.setattr(feed, "_PER_PAGE", 2)
    feed.storefront_currency.clear_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_host", ["uk.store.com","shop.store.com","evilstore.com","other.com"])
async def test_regional_and_other_product_hosts_are_refused_before_currency_or_plan(monkeypatch, other_host):
    def handler(request):
        if request.url.host == "store.com":
            return httpx.Response(302,headers={"location":f"https://{other_host}/products.json"})
        return httpx.Response(200,json={"products":[product(1)]})
    install(monkeypatch,handler)
    locale = AsyncMock(return_value={"currency":"USD"})
    monkeypatch.setattr(feed,"fetch_shopify_shop_locale",locale)
    with pytest.raises(feed.CrawlIncomplete,match="storefront host changed") as err:
        await feed.records_for_brand(domain="store.com",category_path="beauty",source_role="retailer",only_vendors=["A'PIEU"])
    assert err.value.scanned_products == 0 and err.value.status == "failed"
    locale.assert_not_called()


@pytest.mark.asyncio
async def test_second_page_redirect_does_not_expose_the_accepted_prefix(monkeypatch):
    def handler(request):
        if request.url.host == "store.com" and request.url.params.get("page") == "1":
            return httpx.Response(200,json={"products":[product(1),product(2)]})
        if request.url.host == "store.com":
            return httpx.Response(302,headers={"location":"https://uk.store.com/products.json"})
        return httpx.Response(200,json={"products":[product(3)]})
    install(monkeypatch,handler)
    with pytest.raises(feed.CrawlIncomplete) as err:
        await feed.fetch_shopify_products("store.com",max_products=10)
    assert err.value.next_page == 2 and err.value.scanned_products == 2
    assert err.value.selected_products == 2 and not hasattr(err.value,"products")


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,actual", [("store.com","www.store.com"),("www.store.com","store.com")])
async def test_apex_www_redirects_keep_one_storefront_for_feed_and_currency(monkeypatch,requested,actual):
    def handler(request):
        # storefront_currency normalizes requested www away before fetching meta;
        # therefore redirect products only, and serve meta from either allowed host.
        if request.url.path == "/products.json" and request.url.host == requested:
            return httpx.Response(302,headers={"location":f"https://{actual}/products.json"})
        if request.url.path == "/meta.json":
            return httpx.Response(200,json={"currency":"USD"})
        return httpx.Response(200,json={"products":[product(1)]})
    install(monkeypatch,handler)
    records=await feed.records_for_brand(domain=requested,category_path="beauty",source_role="retailer",only_vendors=["A'PIEU"])
    assert records.crawl_report["status"] == "complete"
    assert records[0]["pdp"]["currency"] == "USD"


@pytest.mark.asyncio
async def test_regional_currency_redirect_cannot_relabel_same_host_prices(monkeypatch):
    def handler(request):
        if request.url.path == "/products.json":
            return httpx.Response(200,json={"products":[product(1)]})
        if request.url.host == "store.com":
            return httpx.Response(302,headers={"location":"https://uk.store.com/meta.json"})
        return httpx.Response(200,json={"currency":"GBP"})
    install(monkeypatch,handler)
    with pytest.raises(feed.CurrencyNotProven):
        await feed.records_for_brand(domain="store.com",category_path="beauty",source_role="retailer",only_vendors=["A'PIEU"])
