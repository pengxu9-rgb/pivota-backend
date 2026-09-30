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
