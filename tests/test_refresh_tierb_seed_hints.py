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


@pytest.mark.parametrize("prod, rank", [
    (product(1, "Advanced Liposomal NMN", "liposomal-nmn", vendor="Haroutine", variants=[]), 2),  # not a lip product
    (product(2, "PDRN Lip Sleeping Mask", "pdrn-lip-sleeping-mask", variants=[]), 1),
    (product(3, "Kids' Moisture Lip Balm, 0.1oz.", "kids-moisture-lip-balm", variants=[]), 1),
    (product(4, "Velvet Lipstick", "velvet-lipstick", vendor="M·A·C", variants=[]), 0),
    (product(5, "Eclipse Palette", "eclipse-palette", vendor="MAC", variants=[]), 2),  # "eclipse" is not "lip"
])
def test_the_preference_reads_whole_words(prod, rank):
    prod["variants"] = [var(1)]
    assert hints._preference(prod) == rank


async def test_nothing_qualifying_is_no_replacement():
    catalog = [[product(1, "Sample", "s", variants=[var(1)]), product(2, "Cream", "c", variants=[var(2, available=False)])]]
    shop = Shop(catalog=catalog)
    async with shop.client() as client:
        assert await hints.pick_replacement(client, "x.com", "US", max_pages=3) is None


REFUSED_PATHS = [
    "/cart/1:1", "/cart", "/checkouts/cn/T", "/checkout",
    # the two bypasses the denylist let through (review of #2213)
    "/en-us/cart/123:1", "/12345678/checkouts/cn/T/information",
    # anything else that is not one of the four reads
    "/", "/account/login", "/products/a/b", "/variants/abc", "/collections/all/products.json",
    "/en-us/fr/products.json", "/cart/add.js", "/products", "/variants/1/extra",
]
ALLOWED_PATHS = [
    "/variants/40975353675861", "/products/mac-lipstick", "/products/mac-lipstick.js", "/products.json",
    "/en-us/products.json", "/ja/products/%EB%B0%A4.js", "/en-sg/variants/1", "/products/acv-shampoo-530ml-사본",
]


@pytest.mark.parametrize("path", REFUSED_PATHS)
async def test_the_transport_refuses_every_path_off_the_allowlist(path):
    shop = Shop()
    async with shop.client() as client:
        with pytest.raises(RuntimeError, match="refused"):
            await client.get(f"https://x.com{path}")
    assert shop.requests == []
    assert hints.path_allowed(path) is False


@pytest.mark.parametrize("path", ALLOWED_PATHS)
def test_the_allowlist_admits_the_four_reads_with_an_optional_locale(path):
    assert hints.path_allowed(httpx.URL(f"https://x.com{path}").path) is True


async def test_a_post_to_an_allowed_path_is_refused():
    shop = Shop()
    async with shop.client() as client:
        with pytest.raises(RuntimeError, match="refused"):
            await client.post("https://x.com/products.json")
    assert shop.requests == []


@pytest.mark.parametrize("location", ["/cart/1:1", "/en-us/cart/123:1", "/12345678/checkouts/cn/T",
                                      "https://shop.app/checkouts/x"])
async def test_a_redirect_off_the_allowlist_is_refused_too(location):
    sent = []

    async def routed(request):
        sent.append(request.url.path)
        return httpx.Response(302, headers={"location": location})

    async with httpx.AsyncClient(transport=hints.ReadOnlyTransport(httpx.MockTransport(routed))) as client:
        with pytest.raises(RuntimeError, match="refused"):
            await hints.variant_redirect(client, "x.com", "1")
    assert sent == ["/variants/1"]  # the redirect target never reached the network


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


@pytest.mark.parametrize("title, handle, unfit", [
    ("Pro Filt'r Fluid Flex Foundation Shade Sample", "pro-filtr-fluid-flex-foundation-shade-sample", True),
    ("Glow Starter Sample", "glow-starter-sample-pack", True),
    ("[GIFT] Collagen Glow Sunscreen 10ml", "collagen-glow-sunscreen-10ml", True),
    ("Kids Trial Travel Kit", "kids-trial-travel-kit-shampoo-body-wash", True),
    ("Gloss Bomb Universal Lip Luminizer", "gloss-bomb-universal-lip-luminizer", False),
    ("Single Eyeshadow", "single-eyeshadow", False),
    ("The Latest Glow Serum", "latest-glow-serum", False),   # whole words: "latest" is not "test"
    ("Serum", "serum-testers", True),
])
def test_unfit_seeds_are_recognised_by_title_or_handle(title, handle, unfit):
    assert hints.is_unfit(title, handle) is unfit


def _unfit_shop():
    catalog = [[
        product(1, "Shade Sample", "shade-sample", variants=[var(11)]),
        product(2, "Lip Mini", "lip-mini", variants=[var(21)]),
        product(3, "Travel Size Lip Oil", "lip-oil-travel", variants=[var(31)]),
        product(4, "Lip Care Kit", "lip-care-kit", variants=[var(41)]),
        product(5, "Gloss Bomb Lip Luminizer", "gloss-bomb", variants=[var(51)]),
        product(6, "Body Cream", "body-cream", variants=[var(61)]),
    ]]
    return Shop(
        variants={"11": "/products/shade-sample?variant=11"},
        catalog=catalog,
        product_js={"shade-sample": {"title": "Shade Sample", "variants": [{"id": 11, "available": True}]},
                    "gloss-bomb": {"title": "Gloss Bomb Lip Luminizer", "variants": [{"id": 51, "available": True}]}},
    )


@pytest.mark.parametrize("replace_unfit", [False, True])
async def test_an_unfit_seed_is_swapped_for_a_full_size_product_only_when_asked(monkeypatch, replace_unfit):
    shop = _unfit_shop()
    monkeypatch.setattr(hints, "_default_inner_transport", lambda: httpx.MockTransport(shop.handle))

    async def no_wait(self):
        return None

    monkeypatch.setattr(hints.RequestPacer, "acquire", no_wait)
    rows = [{"domain": "fentybeauty.com", "market": "US", "variant_id": "11"}]
    report = await hints.refresh(rows, only=None, replace_gone=False, max_pages=1, replace_unfit=replace_unfit)
    if not replace_unfit:
        assert report[0]["lookup"] == "found"
        assert rows[0] == {"domain": "fentybeauty.com", "market": "US", "variant_id": "11",
                           "product_handle": "shade-sample"}
        return
    # a lip product that is not a mini, a travel size or a kit; confirmed on /products/<h>.js
    assert report[0]["lookup"] == "replaced" and report[0]["replaced_because"] == "unfit"
    assert report[0]["replaced_variant_id"] == "11" and report[0]["listed"] is True
    assert rows[0] == {"domain": "fentybeauty.com", "market": "US", "variant_id": "51", "product_handle": "gloss-bomb"}


async def test_refresh_goes_through_the_global_pacer(monkeypatch):
    shop = Shop(variants={"1": "/products/h?variant=1"}, product_js={"h": {"title": "H", "variants": [{"id": 1, "available": True}]}})
    monkeypatch.setattr(hints, "_default_inner_transport", lambda: httpx.MockTransport(shop.handle))
    calls = []

    async def counting(self):
        calls.append(1)

    monkeypatch.setattr(hints.RequestPacer, "acquire", counting)
    await hints.refresh([{"domain": "a.com", "market": "US", "variant_id": "1"}], only=None, replace_gone=False, max_pages=1)
    assert len(calls) == len(shop.requests) == 2


def test_no_seed_in_the_repo_list_is_a_sample_gift_or_trial_by_its_handle():
    """The eligibility probe should exercise a product a buyer would actually purchase. Titles
    are not in the file, but the handle usually carries the tell (…-sample-pack, kids-trial-…)."""
    from services.tierb_cart_link_merchants import load_merchants

    unfit = [(m.domain, m.product_handle) for m in load_merchants() if hints.is_unfit(None, m.product_handle)]
    assert unfit == []
