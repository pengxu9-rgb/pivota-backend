"""scripts/ops/refresh_tierb_seed_hints.py: the handle resolver, against a mocked storefront.

No database, no real network (anything not on a MockTransport fails the test). The properties
that matter: it reads the handle off Shopify's /variants/<id> redirect, confirms it on
/products/<handle>.js for the row's market, reports a 404 as GONE, picks a replacement only
when asked and never a test/sample/gift product, prefers a MAC lipstick then a lip product, and
cannot create a cart or a checkout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SPEC = importlib.util.spec_from_file_location(
    "refresh_tierb_seed_hints",
    Path(__file__).resolve().parent.parent / "scripts" / "ops" / "refresh_tierb_seed_hints.py",
)
hints = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hints)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    async def refuse(self, request):
        raise AssertionError(f"un-mocked network request: {request.method} {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def product(pid, title, handle, *, vendor="Brand", product_type="", variants):
    return {"id": pid, "title": title, "handle": handle, "vendor": vendor, "product_type": product_type,
            "variants": variants}


def var(vid, *, available=True, price="24.00", requires_shipping=True):
    return {"id": vid, "available": available, "price": price, "requires_shipping": requires_shipping}


class Shop:
    def __init__(self, *, variants=None, product_js=None, catalog=None):
        self.variants = variants or {}      # vid -> location | None (404)
        self.product_js = product_js or {}  # handle -> payload
        self.catalog = catalog or []        # list of pages
        self.requests = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/variants/"):
            vid = path.rsplit("/", 1)[-1]
            location = self.variants.get(vid)
            if location is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(302, headers={"location": location})
        if path.startswith("/products/") and path.endswith(".js"):
            handle = path[len("/products/"):-3]
            if handle not in self.product_js:
                return httpx.Response(404)
            return httpx.Response(200, json=self.product_js[handle])
        if path == "/products.json":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json={"products": self.catalog[page - 1] if page <= len(self.catalog) else []})
        raise AssertionError(f"unrouted {path}")

    def client(self, *, read_only=True):
        transport: httpx.AsyncBaseTransport = httpx.MockTransport(self.handle)
        if read_only:
            transport = hints.ReadOnlyTransport(transport)
        return httpx.AsyncClient(transport=transport, follow_redirects=False)


def test_the_handle_is_read_off_the_products_path_and_decoded():
    assert hints.handle_from_url("https://metro.com.sg/products/mac-m-a-cximal-matte-silky-lipstick?variant=1") == \
        "mac-m-a-cximal-matte-silky-lipstick"
    assert hints.handle_from_url("https://x.com/en-sg/products/%EB%B0%A4?variant=1") == "밤"
    assert hints.handle_from_url("https://x.com/variants/1") is None


async def test_a_variant_redirect_through_a_host_hop_yields_the_handle():
    shop = Shop(variants={"50755485991233": "/products/mac-lipstick?variant=50755485991233"})

    # First hop is a host redirect with no handle (robinsons -> www.robinsons), then the product.
    def routed(request):
        if request.url.host == "metro.com.sg" and request.url.path.startswith("/variants/"):
            return httpx.Response(301, headers={"location": f"https://www.metro.com.sg{request.url.path}"})
        return shop.handle(request)

    async with httpx.AsyncClient(transport=hints.ReadOnlyTransport(httpx.MockTransport(routed))) as client:
        status, handle = await hints.variant_redirect(client, "metro.com.sg", "50755485991233")
    assert (status, handle) == ("found", "mac-lipstick")


async def test_a_404_variant_is_gone():
    shop = Shop()
    async with shop.client() as client:
        assert await hints.variant_redirect(client, "robinsons.com.sg", "40975353675861") == ("gone", None)


async def test_confirm_reads_availability_for_the_rows_market():
    shop = Shop(product_js={"balm": {"title": "Balm", "variants": [{"id": 1, "available": False}, {"id": 2, "available": True}]}})
    async with shop.client() as client:
        assert await hints.confirm_handle(client, "podl.us", "balm", "2", "US") == (True, True, "Balm")
        assert await hints.confirm_handle(client, "podl.us", "balm", "1", "US") == (True, False, "Balm")
        assert await hints.confirm_handle(client, "podl.us", "balm", "3", "US") == (False, None, "Balm")
        assert await hints.confirm_handle(client, "podl.us", "missing", "3", "US") == (False, None, None)
    assert all(r.url.params.get("country") == "US" for r in shop.requests)


async def test_the_replacement_prefers_a_mac_lipstick_and_never_a_test_sample_or_gift():
    catalog = [[
        product(1, "TEST product do not buy", "test-product", variants=[var(11)]),
        product(2, "Lip Sample Mini", "lip-sample", variants=[var(21)]),
        product(3, "Holiday Gift Set Lipstick", "gift-lip", vendor="M·A·C", variants=[var(31)]),
        product(4, "Hydrating Cream", "cream", variants=[var(41)]),
        product(5, "Tinted Lip Balm", "lip-balm", variants=[var(51, available=False), var(52)]),
        product(6, "Macximal Matte Lipstick", "mac-macximal", vendor="M·A·C", product_type="Lipstick",
                variants=[var(61, price="2.00"), var(62)]),
    ]]
    shop = Shop(catalog=catalog)
    async with shop.client() as client:
        pick = await hints.pick_replacement(client, "robinsons.com.sg", "SG", max_pages=3)
    assert pick["variant_id"] == "62" and pick["product_handle"] == "mac-macximal" and pick["rank"] == 0


async def test_without_a_mac_lipstick_a_lip_product_beats_anything_else():
    catalog = [[product(4, "Hydrating Cream", "cream", variants=[var(41)]),
                product(5, "Tinted Lip Balm", "lip-balm", variants=[var(51, available=False), var(52)])]]
    shop = Shop(catalog=catalog)
    async with shop.client() as client:
        pick = await hints.pick_replacement(client, "x.com", "US", max_pages=3)
    assert (pick["variant_id"], pick["rank"]) == ("52", 1)


async def test_nothing_qualifying_is_no_replacement():
    catalog = [[product(1, "Sample", "s", variants=[var(1)]), product(2, "Cream", "c", variants=[var(2, available=False)])]]
    shop = Shop(catalog=catalog)
    async with shop.client() as client:
        assert await hints.pick_replacement(client, "x.com", "US", max_pages=3) is None


@pytest.mark.parametrize("method, path", [("GET", "/cart/1:1"), ("GET", "/checkouts/cn/T"), ("GET", "/cart"),
                                          ("POST", "/products.json"), ("GET", "/checkout")])
async def test_the_transport_refuses_anything_that_could_create_a_cart_or_checkout(method, path):
    shop = Shop()
    async with shop.client() as client:
        with pytest.raises(RuntimeError, match="refused"):
            await client.request(method, f"https://x.com{path}")
    assert shop.requests == []


async def test_a_redirect_into_the_cart_is_refused_too():
    async def routed(request):
        return httpx.Response(302, headers={"location": "/cart/1:1"})

    async with httpx.AsyncClient(transport=hints.ReadOnlyTransport(httpx.MockTransport(routed))) as client:
        with pytest.raises(RuntimeError, match="refused"):
            await hints.variant_redirect(client, "x.com", "1")


def test_the_file_is_written_one_row_per_line_in_key_order():
    rows = [{"market": "US", "domain": "a.com", "product_handle": "h", "variant_id": "1"},
            {"domain": "b.com", "market": "SG"}]
    text = hints.dump_rows(rows)
    assert text.splitlines()[1] == '  {"domain": "a.com", "market": "US", "variant_id": "1", "product_handle": "h"},'
    assert json.loads(text) == [{"domain": "a.com", "market": "US", "variant_id": "1", "product_handle": "h"},
                                {"domain": "b.com", "market": "SG"}]


async def test_refresh_replaces_a_gone_variant_only_when_asked(monkeypatch):
    catalog = [[product(6, "Macximal Matte Lipstick", "mac-macximal", vendor="MAC", product_type="Lipstick",
                        variants=[var(62)])]]
    shop = Shop(catalog=catalog, product_js={"mac-macximal": {"title": "Macximal", "variants": [{"id": 62, "available": True}]}})
    monkeypatch.setattr(hints, "_default_inner_transport", lambda: httpx.MockTransport(shop.handle))

    async def no_wait(self):  # pacing is the job's tested limiter; here only its wiring matters
        return None

    monkeypatch.setattr(hints.RequestPacer, "acquire", no_wait)
    rows = [{"domain": "robinsons.com.sg", "market": "SG", "variant_id": "40975353675861"}]
    report = await hints.refresh(rows, only=None, replace_gone=False, max_pages=2)
    assert report[0]["lookup"] == "gone" and rows[0] == {"domain": "robinsons.com.sg", "market": "SG",
                                                        "variant_id": "40975353675861"}
    report = await hints.refresh(rows, only=None, replace_gone=True, max_pages=2)
    assert report[0]["lookup"] == "replaced" and report[0]["listed"] is True
    assert rows[0] == {"domain": "robinsons.com.sg", "market": "SG", "variant_id": "62", "product_handle": "mac-macximal"}


async def test_a_handle_that_does_not_list_the_variant_is_not_written(monkeypatch):
    shop = Shop(variants={"1": "/products/other?variant=1"},
                product_js={"other": {"title": "Other", "variants": [{"id": 2, "available": True}]}})
    monkeypatch.setattr(hints, "_default_inner_transport", lambda: httpx.MockTransport(shop.handle))

    async def no_wait(self):
        return None

    monkeypatch.setattr(hints.RequestPacer, "acquire", no_wait)
    rows = [{"domain": "a.com", "market": "US", "variant_id": "1"}]
    report = await hints.refresh(rows, only=None, replace_gone=False, max_pages=1)
    assert report[0]["lookup"] == "found:handle_does_not_list_variant"
    assert "product_handle" not in rows[0]


async def test_refresh_goes_through_the_global_pacer(monkeypatch):
    shop = Shop(variants={"1": "/products/h?variant=1"}, product_js={"h": {"title": "H", "variants": [{"id": 1, "available": True}]}})
    monkeypatch.setattr(hints, "_default_inner_transport", lambda: httpx.MockTransport(shop.handle))
    calls = []

    async def counting(self):
        calls.append(1)

    monkeypatch.setattr(hints.RequestPacer, "acquire", counting)
    await hints.refresh([{"domain": "a.com", "market": "US", "variant_id": "1"}], only=None, replace_gone=False, max_pages=1)
    assert len(calls) == len(shop.requests) == 2
