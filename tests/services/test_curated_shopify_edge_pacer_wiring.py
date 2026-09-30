"""The curated brand feed (and so the retailer-ingest drain) paces its Shopify requests on the crawl IP's
shared Shopify-edge budget (#2474), and feeds response headers to the IP-throttle breaker (#2473).

WHY. From ~00:00Z 2026-09-30 Shopify's shared edge throttled the single crawl NAT IP across every shop.
The /products.json crawl is the heaviest Shopify crawler left on that IP, and it went through
crawl_politeness.await_slot without ever marking or learning a host as Shopify-served -- so the shared
budget never applied to it. A /products.json host is Shopify by construction.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from services import crawl_politeness, shopify_edge_pacer
from services import curated_brand_feed as cbf

HOST = "shop.example-brand.com"
PRODUCT = {"id": 1, "handle": "lip-oil", "title": "Lip Oil", "vendor": "Brand",
           "product_type": "Lip Oil", "variants": [], "images": []}
SHOPIFY_HEADERS = {"content-type": "application/json", "x-shopid": "123"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    shopify_edge_pacer.reset_for_tests()
    monkeypatch.delenv(shopify_edge_pacer.ENABLED_ENV, raising=False)
    monkeypatch.setattr(cbf.asyncio, "sleep", _noop)
    yield
    shopify_edge_pacer.reset_for_tests()


async def _noop(*a, **kw):
    return None


def _serve(monkeypatch, handler):
    """Every httpx.AsyncClient the feed opens answers from `handler`; the politeness gate records,
    per request, whether the shared Shopify-edge budget already applies to that URL."""
    seen = []
    real = httpx.AsyncClient

    def factory(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)

    async def gate(url, *, user_agent, max_wait=None):
        seen.append((url, shopify_edge_pacer.applies(url)))

    notes = []

    def note(url, status_code, *, retry_after=None, headers=None):
        notes.append((url, status_code, headers))

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(crawl_politeness, "before_request", gate)
    monkeypatch.setattr(crawl_politeness, "note_response", note)
    return seen, notes


def _products_json(request):
    page = int(request.url.params.get("page", "1"))
    body = {"products": [PRODUCT] if page == 1 else []}
    return httpx.Response(200, json=body, headers=SHOPIFY_HEADERS)


def test_with_the_pacer_on_every_products_json_page_is_paced_from_the_first_request(monkeypatch):
    monkeypatch.setenv(shopify_edge_pacer.ENABLED_ENV, "1")
    seen, _ = _serve(monkeypatch, _products_json)
    asyncio.run(cbf.fetch_shopify_products(HOST, max_products=50))
    assert seen and all(applies for _url, applies in seen), seen  # page 1 included
    assert shopify_edge_pacer.is_shopify_host(HOST)


def test_with_the_pacer_off_nothing_is_marked_or_learned(monkeypatch):
    seen, _ = _serve(monkeypatch, _products_json)
    asyncio.run(cbf.fetch_shopify_products(HOST, max_products=50))
    assert seen and not any(applies for _url, applies in seen)
    assert not shopify_edge_pacer.is_shopify_host(HOST)
    assert shopify_edge_pacer.stats()["hosts_marked"] == 0 and shopify_edge_pacer.stats()["hosts_learned"] == 0


def test_every_response_hands_its_headers_to_the_crawl_gate(monkeypatch):
    """#2473's breaker (and the 429 diagnostics line) only see what note_response is given."""
    _, notes = _serve(monkeypatch, _products_json)
    asyncio.run(cbf.fetch_shopify_products(HOST, max_products=50))
    assert notes and all(headers is not None and headers.get("x-shopid") == "123" for _u, _s, headers in notes)


def test_a_pdp_fetch_learns_a_shopify_host_from_its_response_headers(monkeypatch):
    """Not Shopify by construction (a brand PDP), so learned from the host that ANSWERED."""
    monkeypatch.setenv(shopify_edge_pacer.ENABLED_ENV, "1")

    def pdp(request):
        return httpx.Response(200, text="<html></html>", headers={"x-shopify-stage": "production"})

    _serve(monkeypatch, pdp)
    asyncio.run(cbf.fetch_pdp_description("brand-pdp.example.com", "lip-oil"))
    assert shopify_edge_pacer.is_shopify_host("brand-pdp.example.com")


def test_a_pdp_fetch_from_a_non_shopify_host_learns_nothing(monkeypatch):
    monkeypatch.setenv(shopify_edge_pacer.ENABLED_ENV, "1")
    _serve(monkeypatch, lambda request: httpx.Response(200, text="<html></html>", headers={"server": "nginx"}))
    asyncio.run(cbf.fetch_pdp_description("plain.example.com", "lip-oil"))
    assert not shopify_edge_pacer.is_shopify_host("plain.example.com")


def test_the_meta_json_fetch_is_paced_as_shopify(monkeypatch):
    monkeypatch.setenv(shopify_edge_pacer.ENABLED_ENV, "1")
    seen, _ = _serve(monkeypatch, lambda request: httpx.Response(200, json={}, headers={"content-type": "application/json"}))
    asyncio.run(cbf.fetch_shop_description_from_meta(HOST))
    assert seen and all(applies for _url, applies in seen)


def test_the_markets_capture_paces_its_requests_as_shopify(monkeypatch):
    from services.retailer_ingest import shopify_markets

    monkeypatch.setenv(shopify_edge_pacer.ENABLED_ENV, "1")
    seen = []

    class _Polite:
        async def before_request(self, url, *, user_agent, max_wait=None):
            seen.append(shopify_edge_pacer.applies(url))

        def note_response(self, url, status_code, *, retry_after=None, headers=None):
            seen.append(("headers", headers is not None))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={}, headers={"content-type": "application/json"}))) as client:
            session = shopify_markets._Session(client, HOST, _Polite(), {})
            await session.request("GET", "/cart.js", expect_json=True)

    asyncio.run(run())
    assert seen[0] is True and ("headers", True) in seen


# -- review of #2477 ---------------------------------------------------------------------------------

def _gtin_product(n):
    return {"id": 9000000 + n, "handle": f"oil-{n}", "vendor": "Brand", "title": "Oil",
            "variants": [{"id": 45000000000000 + n, "price": "10.00"}]}


def test_gtin_recovery_is_paced_as_shopify_and_waits_for_its_turn(monkeypatch):
    """P1: the .js fetch is marked AND waits (max_wait=0); a bounded wait refused every product."""
    monkeypatch.setenv(shopify_edge_pacer.ENABLED_ENV, "1")
    seen = []

    async def gate(url, *, user_agent, max_wait=None):
        seen.append((shopify_edge_pacer.applies(url), max_wait))

    def reply(request):
        n = int(request.url.path.split("oil-")[1].split(".")[0])
        detail = _gtin_product(n)
        detail["variants"][0]["barcode"] = "8809530070499"
        return httpx.Response(200, json=detail)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(reply), **kw))
    monkeypatch.setattr(crawl_politeness, "before_request", gate)
    monkeypatch.setattr(crawl_politeness, "note_response", lambda *a, **kw: None)
    _rows, report = asyncio.run(cbf.recover_missing_variant_gtins(
        [_gtin_product(1), _gtin_product(2)], domain=HOST))
    assert [applies for applies, _w in seen] == [True, True]
    assert all(0 < wait <= cbf.GTIN_RECOVERY_BUDGET_S for _a, wait in seen)  # bounded by the budget, not 10s
    assert report["recovered"] == 2 and report["failed"] == 0 and report["paced"] == 0


def test_a_pacing_refusal_is_counted_paced_not_failed_and_stops_the_batch(monkeypatch):
    """A host asking for a Crawl-delay longer than we honour: not the product's failure, and every
    later product would be refused the same way."""
    calls = []

    async def gate(url, *, user_agent, max_wait=None):
        calls.append(url)
        raise crawl_politeness.CrawlDelayTooLong("Crawl-delay 120s")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)), **kw))
    monkeypatch.setattr(crawl_politeness, "before_request", gate)
    _rows, report = asyncio.run(cbf.recover_missing_variant_gtins(
        [_gtin_product(n) for n in range(1, 6)], domain=HOST))
    assert report["failed"] == 0 and report["paced"] == 5 and report["attempted"] == 0
    assert report["http_requests"] == 0 and len(calls) == 1


def test_the_shop_locale_fetch_is_paced_as_shopify(monkeypatch):
    """/meta.json via storefront_currency: marked before the first request."""
    monkeypatch.setenv(shopify_edge_pacer.ENABLED_ENV, "1")
    seen, _ = _serve(monkeypatch, lambda request: httpx.Response(
        200, json={"currency": "USD"}, headers={"content-type": "application/json"}))
    asyncio.run(cbf.fetch_shopify_shop_locale(HOST))
    assert seen and all(applies for _url, applies in seen)


@pytest.mark.parametrize("fetch", [
    lambda: cbf.fetch_pdp_inci("inci.example.com", "lip-oil"),
    lambda: cbf.fetch_shop_description("inci.example.com"),
])
def test_the_inci_and_shop_blurb_pages_feed_headers_and_learn(monkeypatch, fetch):
    monkeypatch.setenv(shopify_edge_pacer.ENABLED_ENV, "1")
    _, notes = _serve(monkeypatch, lambda request: httpx.Response(
        200, text="<html></html>", headers={"x-shopid": "9"}))
    asyncio.run(fetch())
    assert notes and all(headers is not None for _u, _s, headers in notes)
    assert shopify_edge_pacer.is_shopify_host("inci.example.com")



def test_a_host_answering_429_ends_the_recovery_after_one_request_like_main(monkeypatch):
    """Review #2477 P0: flag OFF, the drain's pacing env, every .js answering 429 Retry-After 60.
    main stopped after 1 request in ~0s; an unbounded wait slept out the growing backoff for every
    product (~16h for 200). Run through the REAL politeness gate."""
    import time as _time

    crawl_politeness.reset_for_tests()
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "4")
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "15")
    requests = []

    def reply(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        requests.append(request.url.path)
        return httpx.Response(429, headers={"retry-after": "60"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(reply), **kw))
    started = _time.monotonic()
    _rows, report = asyncio.run(cbf.recover_missing_variant_gtins(
        [_gtin_product(n) for n in range(1, 21)], domain=HOST))
    assert len(requests) == 1 and report["http_requests"] == 1
    assert report["failed"] == 1 and report["blocked_stopped"] == 19
    assert _time.monotonic() - started < 5
    crawl_politeness.reset_for_tests()


def test_a_backed_up_schedule_stops_at_the_budget_not_after_thousands_of_seconds(monkeypatch):
    """Review #2477 P0: a steady 15s shared backlog (flag on) cost ~3,000s for 200 products unbounded.
    On a fake clock: each turn takes 15s; the recovery stops when its budget cannot cover the next."""
    clock = [1000.0]
    monkeypatch.setattr(cbf.time, "monotonic", lambda: clock[0])
    waits = []

    async def gate(url, *, user_agent, max_wait=None):
        waits.append(max_wait)
        if max_wait < 15:
            raise shopify_edge_pacer.EdgePaced("next shared slot is 15s out")
        clock[0] += 15

    def reply(request):
        n = int(request.url.path.split("oil-")[1].split(".")[0])
        detail = _gtin_product(n)
        detail["variants"][0]["barcode"] = "8809530070499"
        return httpx.Response(200, json=detail)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(reply), **kw))
    monkeypatch.setattr(crawl_politeness, "before_request", gate)
    monkeypatch.setattr(crawl_politeness, "note_response", lambda *a, **kw: None)
    _rows, report = asyncio.run(cbf.recover_missing_variant_gtins(
        [_gtin_product(n) for n in range(1, 11)], domain=HOST, budget_s=40))
    assert report["recovered"] == 2 and report["budget_stopped"] == 8 and report["failed"] == 0
    assert clock[0] - 1000.0 == 30  # never ran past its budget
