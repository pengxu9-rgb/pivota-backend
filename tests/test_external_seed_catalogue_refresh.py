"""The seed refresh's catalogue price lane (`services.external_seed_catalogue_refresh`).

A Shopify store's paged `/products.json` prices every variant of 250 products a request, where the
refresh's page read costs ~1.3 requests a listing on an egress held to 0.5 requests/s. The lane swaps
the READ and nothing after it, so these tests drive the real code on both sides of the swap:

  * the stored row is the retailer-ingest drain's own output for a products.json product
    (`curated_brand_feed.shopify_product_to_record` -> `ingest_validated_jsonl`), and the prior page read
    goes through the real JSON-LD extractor;
  * the catalogue is served over an `httpx.MockTransport` to the real `read_host_catalogue`, with the
    headers a storefront sends (`cart_currency` Set-Cookie), and handed to the real
    `_refresh_external_seed_by_id` and the real batch.
Only the network, the DB and the projection's own writer are stubbed.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock

import httpx
import pytest

from services import crawl_global_pacer
from services import crawl_politeness as cp
from services import external_seed_catalogue_refresh as lane
from services import external_seed_destination_liveness as liveness

HOST = "brand.example"
HANDLE = "example-eau-de-toilette"
DEST = f"https://{HOST}/products/{HANDLE}"
V50, V15 = "37169662034071", "41430446375063"  # real-shaped Shopify variant ids
TITLE = "Example Eau De Toilette"


#: The pacing layer's own response hook, before `_no_pacing` stubs it (the breaker test puts it back).
_REAL_NOTE_RESPONSE = cp.note_response


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch: pytest.MonkeyPatch):
    """The gate is tested in tests/test_crawl_politeness.py; here it records and never waits."""
    cp.reset_for_tests()
    asked: List[str] = []

    async def _allow(url, *, user_agent, max_wait=None):
        asked.append(url)

    async def _no_sleep(_seconds, result=None):
        return result

    monkeypatch.setattr(cp, "before_request", _allow)
    monkeypatch.setattr(cp, "note_response", lambda *a, **k: None)
    monkeypatch.setattr(liveness.asyncio, "sleep", _no_sleep)
    monkeypatch.delenv(lane.FLAG, raising=False)
    monkeypatch.delenv(lane.MIN_SEEDS_ENV, raising=False)
    monkeypatch.delenv(lane.PAGE_MAX_AGE_ENV, raising=False)
    yield asked
    cp.reset_for_tests()


# ----------------------------------------------------------------- the store, as it serves itself


def _product(handle: str = HANDLE, *, p50: Any = "80.00", p15: Any = "32.00", a50: Any = True, a15: Any = True,
             compare50: Any = None, **extra: Any) -> Dict[str, Any]:
    """One `/products.json` product: the dict the drain itself ingests (tests/test_seed_refresh_keeps_merchant_variants)."""
    return {
        "id": 6644068500001, "vendor": "Example", "title": TITLE, "handle": handle,
        "product_type": "Fragrance", "body_html": "<p>A fragrance.</p>",
        "images": [{"src": "https://cdn.brand.example/a.jpg"}], "options": [{"name": "Size"}],
        "variants": [
            {"id": int(V50), "title": "1.7 fl. oz. / 50 mL", "price": p50, "compare_at_price": compare50,
             "available": a50, "sku": "EX50", "option1": "1.7 fl. oz. / 50 mL"},
            {"id": int(V15), "title": "0.5 fl. oz. / 15 mL", "price": p15, "compare_at_price": None,
             "available": a15, "sku": "EX15", "option1": "0.5 fl. oz. / 15 mL"},
        ],
        **extra,
    }


def _other(i: int) -> Dict[str, Any]:
    return {"id": 9000 + i, "title": f"Other {i}", "handle": f"other-{i}",
            "variants": [{"id": 8000000000 + i, "title": "Default Title", "price": "10.00", "available": True}]}


def _store(
    pages: List[List[Dict[str, Any]]],
    *,
    base: Optional[str] = "USD",
    cookie: Optional[str] = "USD",
    cart: Optional[str] = None,
    count: Any = None,
    seen: Optional[List[httpx.Request]] = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A storefront: /meta.json, paged /products.json (each page sets `cart_currency` when `cookie`),
    /cart.js. An empty page ends the catalogue."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == "/meta.json":
            if base is None:
                return httpx.Response(404, text="not found")
            body = {"name": "Example", "currency": base, "domain": HOST}
            if count is not None:
                body["published_products_count"] = count
            return httpx.Response(200, json=body)
        if path == "/products.json":
            page = int(request.url.params.get("page") or 1)
            products = pages[page - 1] if page <= len(pages) else []
            headers = [("set-cookie", f"cart_currency={cookie}; path=/")] if cookie else []
            headers.append(("set-cookie", "localization=US; path=/"))
            return httpx.Response(200, json={"products": products}, headers=headers)
        if path == "/cart.js":
            if cart is None:
                return httpx.Response(404, text="")
            return httpx.Response(200, json={"token": "t", "items": [], "currency": cart})
        return httpx.Response(404, text="")

    return handler


def _read_host(pages, wanted=(HANDLE,), **store_kw) -> lane.HostCatalogue:
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_store(pages, **store_kw))) as client:
            return await lane.read_host_catalogue(HOST, set(wanted), client=client)

    return asyncio.run(go())


def _seed_read(product: Optional[Dict[str, Any]] = None, url: str = DEST, **store_kw) -> lane.SeedCatalogueRead:
    catalogue = _read_host([[_other(1), product or _product(), _other(2)]], wanted=(HANDLE, "x", "y"), **store_kw)
    read, why = lane.seed_catalogue_read(catalogue, url)
    assert read is not None, why
    return read


# ----------------------------------------------------------------- pure rules


@pytest.mark.parametrize("url,handle", [
    (DEST, HANDLE),
    (DEST + "/", HANDLE),
    (DEST + "?variant=" + V15, HANDLE),
    (f"https://{HOST}/collections/fragrance/products/{HANDLE}", HANDLE),
    (f"https://{HOST}/products/Example-EAU", "example-eau"),
    (f"https://{HOST}/products/%E3%82%AA%E3%82%A4%E3%83%AB", "オイル"),
    (f"https://{HOST}/en-gb/products/{HANDLE}", None),          # a Markets subfolder: another currency
    (f"https://{HOST}/products/{HANDLE}/extra", None),
    (f"https://{HOST}/products/", None),
    (f"https://{HOST}/p/{HANDLE}", None),
    ("", None),
])
def test_only_a_root_product_url_is_one_the_catalogue_prices(url, handle):
    assert lane.catalogue_handle(url) == handle


@pytest.mark.parametrize("url,selected", [
    (DEST, ""),
    (DEST + "?variant=" + V15, V15),
    (DEST + f"?variant={V15}&variant={V15}", V15),
    (DEST + f"?variant={V15}&variant={V50}", None),
    (DEST + "?variant=abc", None),
    (DEST + "?variant=", None),
])
def test_the_url_selects_at_most_one_variant(url, selected):
    assert lane.url_variant_id(url) == selected


@pytest.mark.parametrize("raw,amount", [
    ("28.00", 28.0), ("28", 28.0), ("28.5", 28.5), ("0.00", 0.0), (" 28.00 ", 28.0),
    ("28,00", None), ("1.234", None), ("-1.00", None), ("1e3", None), ("", None),
    (28.0, None), (28, None), (None, None), (True, None),
])
def test_a_catalogue_price_is_read_strictly(raw, amount):
    assert lane.catalogue_price(raw) == amount


@pytest.mark.parametrize("base,cookies,cart,expected", [
    ("USD", ["USD", "USD"], None, ("USD", lane.PROOF_BASE_AND_PAGE_COOKIE)),
    ("USD", ["USD", None], "USD", ("USD", lane.PROOF_BASE_AND_CART)),
    ("USD", [None], "USD", ("USD", lane.PROOF_BASE_AND_CART)),
    ("USD", [None], None, (None, lane.STATUS_CURRENCY_UNPROVEN)),
    ("USD", ["USD", None], None, (None, lane.STATUS_CURRENCY_UNPROVEN)),
    # base and presentment differ: the numbers could be in either, so neither is taken.
    ("KRW", ["USD"], None, (None, lane.STATUS_CURRENCY_CONFLICT)),
    ("KRW", [None], "USD", (None, lane.STATUS_CURRENCY_CONFLICT)),
    ("USD", ["USD", "CAD"], None, (None, lane.STATUS_CURRENCY_CONFLICT)),
    ("USD", ["USD"], "CAD", (None, lane.STATUS_CURRENCY_CONFLICT)),
    (None, ["USD"], "USD", (None, lane.STATUS_BASE_UNREAD)),
    (None, [None], None, (None, lane.STATUS_BASE_UNREAD)),
])
def test_a_host_is_priced_only_when_its_base_currency_is_the_one_it_presented(base, cookies, cart, expected):
    assert lane.decide_host_currency(base=base, page_cookies=cookies, cart=cart) == expected


@pytest.mark.parametrize("wanted,count,pages", [
    (0, None, None),
    (3, None, 3),
    (3, 250, 3),            # one page needed
    (3, 750, 3),            # three pages needed, three wanted
    (3, 751, None),         # four pages needed for three seeds: page reads are cheaper
    (500, None, liveness.MAX_CATALOGUE_PAGES),
    (5, True, 5),           # a boolean is not a count
    (5, "2000", 5),         # nor is a string
    (5, 0, 5),
])
def test_a_host_is_read_only_when_its_pages_pay_for_themselves(wanted, count, pages):
    assert lane.pages_worth_reading(wanted, count) == pages


def test_the_variants_read_like_the_page_extractors_and_the_census_is_complete():
    variants, census = lane.read_variants(_product(compare50="95.00"), "USD")
    assert variants == [
        {"variant_id": V50, "title": "1.7 fl. oz. / 50 mL", "price_amount": 80.0, "price_currency": "USD",
         "availability": "in_stock", "price_exact": True, "sku": "EX50", "compare_at_amount": 95.0},
        {"variant_id": V15, "title": "0.5 fl. oz. / 15 mL", "price_amount": 32.0, "price_currency": "USD",
         "availability": "in_stock", "price_exact": True, "sku": "EX15"},
    ]
    assert census["offers"] == 2 and census["exact_prices"] == [32.0, 80.0]
    assert census["availabilities"] == ["in_stock"]
    assert not any(census[k] for k in ("inexact", "aggregate", "truncated", "plans_only", "data_attr_duplicate_ids"))


def test_an_unread_price_or_stock_is_never_invented():
    variants, census = lane.read_variants(_product(p15="32,00", a15=None), "USD")
    assert variants[1]["price_amount"] is None and variants[1]["price_exact"] is False
    assert variants[1]["availability"] == "unknown"
    # An unread sibling price: the single-variant fallback must not take the product price off this list.
    assert census["inexact"] and census["price_refused"] == 1
    assert census["exact_prices"] == [80.0]


def test_a_duplicate_id_is_marked_and_a_long_list_is_truncated(monkeypatch):
    product = _product()
    product["variants"].append(dict(product["variants"][0], price="81.00"))
    variants, census = lane.read_variants(product, "USD")
    assert [v["variant_id"] for v in variants] == [V50, V15]
    assert variants[0]["id_collided"] is True and variants[0]["price_amount"] == 80.0
    assert census["duplicate_ids"] == 1 and census["offers"] == 3
    monkeypatch.setattr(lane, "CATALOGUE_MAX_VARIANTS", 1)
    variants, census = lane.read_variants(_product(), "USD")
    assert len(variants) == 1 and census["truncated"]


def test_default_title_is_not_a_variant_label():
    variants, _ = lane.read_variants(_other(1), "USD")
    assert variants[0]["title"] is None


@pytest.mark.parametrize("url,a50,a15,price,availability", [
    (DEST, True, True, 80.0, "in_stock"),                       # first available
    (DEST, False, True, 32.0, "in_stock"),                      # first AVAILABLE, not first
    (DEST, False, False, 80.0, "out_of_stock"),                 # none available: the first
    (DEST + "?variant=" + V15, True, True, 32.0, "in_stock"),   # the URL's variant
    (DEST + "?variant=" + V50, False, True, 80.0, "out_of_stock"),
])
def test_the_product_price_is_the_pages_selected_or_first_available_variant(url, a50, a15, price, availability):
    snap = lane.catalogue_snapshot(_product(a50=a50, a15=a15), url=url, host=HOST, currency="USD",
                                   proof=lane.PROOF_BASE_AND_PAGE_COOKIE)
    assert (snap.price_amount, snap.price_currency, snap.availability) == (price, "USD", availability)
    assert snap.evidence["price_read"]["status"] == "parsed"
    assert snap.evidence["variant_census"]["product_price_exact"] is True


def test_a_url_variant_the_product_does_not_list_is_not_priced():
    assert lane.catalogue_snapshot(_product(), url=DEST + "?variant=123456789", host=HOST, currency="USD",
                                   proof=lane.PROOF_BASE_AND_CART) is None
    assert lane.catalogue_snapshot({"handle": HANDLE, "variants": []}, url=DEST, host=HOST, currency="USD",
                                   proof=lane.PROOF_BASE_AND_CART) is None


def test_the_snapshot_reads_no_content_and_names_its_currency_proof():
    snap = lane.catalogue_snapshot(_product(), url=DEST, host=HOST, currency="USD", proof=lane.PROOF_BASE_AND_CART)
    assert (snap.title, snap.image_url, snap.canonical_url, snap.brand) == (None, None, None, None)
    ev = snap.evidence
    assert not {"description", "image_urls", "image_urls_scope", "meta_description"} & set(ev)
    assert ev["price_currency_source"] == "page" and ev["price_currency_proof"] == lane.PROOF_BASE_AND_CART
    assert ev["read_source"] == lane.READ_SOURCE


@pytest.mark.parametrize("p50,status", [("80,00", "catalogue_price_unread"), (None, "empty"), ("", "empty")])
def test_an_unread_product_price_reaches_the_refresh_as_unreadable(p50, status):
    snap = lane.catalogue_snapshot(_product(p50=p50), url=DEST, host=HOST, currency="USD", proof=lane.PROOF_BASE_AND_CART)
    assert snap.price_amount is None and snap.evidence["price_read"]["status"] == status
    assert snap.evidence["variant_census"]["product_price_exact"] is False


# ----------------------------------------------------------------- one host's read


def test_a_host_read_costs_meta_and_the_pages_up_to_the_last_wanted_handle(_no_pacing):
    seen: List[httpx.Request] = []
    pages = [[_other(1), _product()], [_other(2)], [_other(3)]]
    catalogue = _read_host(pages, wanted=(HANDLE, "other-2"), seen=seen)
    assert catalogue.status == lane.STATUS_OK and catalogue.currency == "USD"
    assert catalogue.proof == lane.PROOF_BASE_AND_PAGE_COOKIE
    assert set(catalogue.products) == {HANDLE, "other-2"}
    assert [str(r.url) for r in seen] == [
        f"https://{HOST}/meta.json", f"https://{HOST}/products.json?limit=250&page=1",
        f"https://{HOST}/products.json?limit=250&page=2"]
    assert catalogue.catalogue_status == liveness.CATALOGUE_WANTED_FOUND and catalogue.pages_read == 2
    # Every request went through the politeness gate (robots.txt, per-host pacing).
    assert _no_pacing == [str(r.url) for r in seen]


def test_pages_without_the_cookie_ask_cart_js_once():
    seen: List[httpx.Request] = []
    catalogue = _read_host([[_product()]], cookie=None, cart="USD", seen=seen)
    assert (catalogue.status, catalogue.proof) == (lane.STATUS_OK, lane.PROOF_BASE_AND_CART)
    assert [r.url.path for r in seen].count("/cart.js") == 1
    seen.clear()
    _read_host([[_product()]], seen=seen)
    assert "/cart.js" not in [r.url.path for r in seen], "the page cookie proved it: no extra request"


@pytest.mark.parametrize("store_kw,status", [
    ({"base": None}, lane.STATUS_BASE_UNREAD),
    ({"base": "KRW"}, lane.STATUS_CURRENCY_CONFLICT),
    ({"cookie": None}, lane.STATUS_CURRENCY_UNPROVEN),           # no cookie, /cart.js 404
    ({"cookie": None, "cart": "CAD"}, lane.STATUS_CURRENCY_CONFLICT),
    ({"cookie": "usd"}, lane.STATUS_CURRENCY_UNPROVEN),          # a malformed cookie proves nothing
])
def test_a_host_whose_currency_is_not_proven_is_not_priced(store_kw, status):
    catalogue = _read_host([[_product()]], **store_kw)
    assert catalogue.status == status and catalogue.products == {} and catalogue.currency is None
    assert lane.seed_catalogue_read(catalogue, DEST) == (None, status)


def test_meta_unread_costs_one_request_and_reads_no_page():
    seen: List[httpx.Request] = []
    catalogue = _read_host([[_product()]], base=None, seen=seen)
    assert [r.url.path for r in seen] == ["/meta.json"] and catalogue.status == lane.STATUS_BASE_UNREAD


def test_a_catalogue_too_big_for_its_seeds_is_not_paged():
    seen: List[httpx.Request] = []
    catalogue = _read_host([[_product()]], wanted=(HANDLE,), count=251, seen=seen)
    assert catalogue.status == lane.STATUS_NOT_WORTH_IT and [r.url.path for r in seen] == ["/meta.json"]


def test_the_page_cap_stops_paging_and_keeps_what_it_read():
    seen: List[httpx.Request] = []
    pages = [[_product()], [_other(1)], [_other(2)], [_other(3)]]
    catalogue = _read_host(pages, wanted=(HANDLE, "missing-a", "missing-b"), seen=seen)
    assert [r.url.path for r in seen].count("/products.json") == 3, "one page per wanted seed"
    assert catalogue.status == lane.STATUS_OK and set(catalogue.products) == {HANDLE}
    assert catalogue.catalogue_status == liveness.CATALOGUE_INCOMPLETE
    assert lane.seed_catalogue_read(catalogue, f"https://{HOST}/products/missing-a") == (None, "handle_not_read")


def test_no_wanted_handle_listed_asks_nothing_more():
    seen: List[httpx.Request] = []
    catalogue = _read_host([[_other(1)]], wanted=("nope", "nada", "zip"), cookie=None, seen=seen)
    assert catalogue.status == lane.STATUS_NONE_LISTED and "/cart.js" not in [r.url.path for r in seen]


def test_a_refused_first_page_is_named_and_prices_nothing():
    def handler(request):
        if request.url.path == "/meta.json":
            return httpx.Response(200, json={"currency": "USD"})
        return httpx.Response(429, headers={"cf-mitigated": "challenge"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lane.read_host_catalogue(HOST, {HANDLE}, client=client)

    catalogue = asyncio.run(go())
    assert catalogue.status == "catalogue_" + liveness.CATALOGUE_BOT_CHALLENGE and not catalogue.products


def test_a_stopped_run_propagates_and_anything_else_is_a_refusal(monkeypatch):
    async def stopped(*a, **k):
        raise crawl_global_pacer.GlobalPaceStopped("budget")

    monkeypatch.setattr(cp, "before_request", stopped)
    with pytest.raises(crawl_global_pacer.GlobalPaceStopped):
        _read_host([[_product()]])

    async def broken(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(lane, "_get_json", broken)
    assert _read_host([[_product()]]).status == lane.STATUS_ERROR


def test_the_lane_carries_no_cookie_from_one_response_to_the_next(monkeypatch):
    """The presentment rule: a cookie one response set must not steer the next one's currency."""
    from services import crawl_identity

    seen: List[httpx.Request] = []
    transport = httpx.MockTransport(_store([[_other(1)], [_product()]], seen=seen))
    monkeypatch.setattr(crawl_identity, "transport_kwargs", lambda inner=None: {"transport": transport})
    catalogue = asyncio.run(lane.read_host_catalogue(HOST, {HANDLE, "other-1"}))
    assert catalogue.status == lane.STATUS_OK and len(seen) == 3
    assert not any("cookie" in r.headers for r in seen)


def test_a_client_the_read_opened_is_closed_whatever_the_outcome(monkeypatch):
    closed: List[str] = []

    class _Tracked(httpx.AsyncClient):
        async def aclose(self) -> None:
            closed.append("closed")
            await super().aclose()

    for handler in (_store([[_product()]]), _store([[_product()]], base=None)):
        monkeypatch.setattr(lane, "_client", lambda h=handler: _Tracked(transport=httpx.MockTransport(h)))
        asyncio.run(lane.read_host_catalogue(HOST, {HANDLE}))
    assert closed == ["closed", "closed"]


# ----------------------------------------------------------------- review of #2685: one storefront, early stop


OTHER_HOST = "sister-store.example"


def _read_following(handler, wanted=(HANDLE, "x", "y"), host: str = HOST) -> lane.HostCatalogue:
    """`read_host_catalogue` over a client that follows redirects, as the lane's own `_client()` does."""
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await lane.read_host_catalogue(host, set(wanted), client=client)

    return asyncio.run(go())


def _redirecting(inner, *, to: str, paths=("/meta.json", "/products.json", "/cart.js"), seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.host == HOST and request.url.path in paths:
            return httpx.Response(301, headers={"location": str(request.url.copy_with(host=to))})
        return inner(request)

    return handler


def test_a_host_redirected_onto_another_storefront_is_not_priced_from_it():
    """Review P2-1 (the reviewer's F1): every answer came from the other store, a consistent USD one."""
    seen: List[httpx.Request] = []
    store = _store([[_product(p50="99.00")]], base="USD", cookie="USD")
    catalogue = _read_following(_redirecting(store, to=OTHER_HOST, seen=seen))
    assert catalogue.status == lane.STATUS_REDIRECTED_OFF_HOST and not catalogue.products
    assert [r.url.path for r in seen] == ["/meta.json", "/meta.json"], "the hop, then nothing more"
    assert lane.seed_catalogue_read(catalogue, DEST) == (None, lane.STATUS_REDIRECTED_OFF_HOST)


def test_a_catalogue_redirected_onto_another_storefront_is_not_priced_from_it():
    store = _store([[_product(p50="99.00")]], base="USD", cookie="USD")
    catalogue = _read_following(_redirecting(store, to=OTHER_HOST, paths=("/products.json",)))
    assert catalogue.status == "catalogue_" + liveness.CATALOGUE_REDIRECTED_OFF_HOST
    assert lane.seed_catalogue_read(catalogue, DEST)[0] is None


def test_a_page_redirected_mid_catalogue_ends_the_read_with_what_this_host_listed():
    store = _store([[_product(), _other(1)], [_other(2)]], base="USD", cookie="USD")

    def handler(request):
        if request.url.host == HOST and request.url.params.get("page") == "2":
            return httpx.Response(301, headers={"location": str(request.url.copy_with(host=OTHER_HOST))})
        return store(request)

    catalogue = _read_following(handler)
    assert catalogue.status == lane.STATUS_OK and catalogue.catalogue_status == liveness.CATALOGUE_INCOMPLETE
    assert set(catalogue.products) == {HANDLE} and "final host" in catalogue.note


def test_a_redirect_to_the_www_storefront_is_the_same_storefront():
    catalogue = _read_following(_redirecting(_store([[_product(p50="85.00")]]), to="www." + HOST))
    assert catalogue.status == lane.STATUS_OK
    read, why = lane.seed_catalogue_read(catalogue, DEST)
    assert why == "read" and read.snapshot.price_amount == 85.0


def test_a_cart_js_from_another_storefront_proves_nothing():
    store = _store([[_product()]], base="USD", cookie=None, cart="USD")
    catalogue = _read_following(_redirecting(store, to=OTHER_HOST, paths=("/cart.js",)))
    assert (catalogue.status, catalogue.cart_currency) == (lane.STATUS_CURRENCY_UNPROVEN, None)


@pytest.mark.parametrize("host,other,same", [
    ("brand.example", "brand.example", True), ("brand.example", "WWW.Brand.Example.", True),
    ("www.brand.example", "brand.example", True), ("brand.example", "shop.brand.example", False),
    ("brand.example", "brand.example.evil", False), ("", "", False),
])
def test_one_storefront_is_one_host_but_for_www(host, other, same):
    assert liveness.same_storefront(host, other) is same


def test_a_first_page_presented_in_another_currency_ends_the_read_there():
    """Review P2-3 (the reviewer's F2): it read meta and six pages before refusing."""
    pages = [[_other(i) for i in range(p * 10, p * 10 + 3)] for p in range(4)] + [[_product()]]
    seen: List[httpx.Request] = []
    catalogue = _read_host(pages, wanted=(HANDLE, "a", "b", "c", "d", "e"), base="GBP", cookie="USD", cart="GBP", seen=seen)
    assert catalogue.status == lane.STATUS_CURRENCY_CONFLICT
    assert [r.url.path for r in seen] == ["/meta.json", "/products.json"], "no more pages, no /cart.js"
    assert catalogue.catalogue_status == liveness.CATALOGUE_STOPPED and catalogue.pages_read == 1


def test_a_later_page_presented_in_another_currency_ends_the_read_there():
    store = _store([[_other(1)], [_other(2)], [_product()]], base="USD", cookie="USD")
    seen: List[httpx.Request] = []

    def handler(request):
        seen.append(request)
        resp = store(request)
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=resp.json(), headers=[("set-cookie", "cart_currency=CAD; path=/")])
        return resp

    catalogue = _read_following(handler, wanted=(HANDLE, "x", "y"))
    assert catalogue.status == lane.STATUS_CURRENCY_CONFLICT
    assert [r.url.params.get("page") for r in seen if r.url.path == "/products.json"] == ["1", "2"]


def test_the_catalogues_answers_feed_the_host_breakers(monkeypatch):
    """The batch's 429 and challenge breakers read `crawl_politeness`'s per-host streaks, which every lane
    response is noted into, so a refused catalogue counts toward them like a refused page."""
    monkeypatch.setattr(cp, "note_response", _REAL_NOTE_RESPONSE)
    store = _store([[_product()]])

    def handler(request):
        if request.url.path == "/meta.json":
            return httpx.Response(429, headers={"retry-after": "1"}, text="")
        return store(request)

    assert _read_host_with(handler).status == lane.STATUS_BASE_UNREAD
    assert cp.consecutive_blocks(HOST) == 1

    def challenged(request):
        if request.url.path == "/products.json":
            return httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="")
        return store(request)

    cp.reset_for_tests()
    assert _read_host_with(challenged).status == "catalogue_" + liveness.CATALOGUE_BOT_CHALLENGE
    assert cp.consecutive_challenges(HOST) == 1


def _read_host_with(handler) -> lane.HostCatalogue:
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lane.read_host_catalogue(HOST, {HANDLE, "x", "y"}, client=client)

    return asyncio.run(go())


def test_every_snapshot_is_stamped_when_the_catalogue_was_fetched():
    catalogue = _read_host([[_product(), _other(1), _other(2)]], wanted=(HANDLE, "x", "y"))
    fetched = datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)
    catalogue.fetched_at = fetched
    read, _ = lane.seed_catalogue_read(catalogue, DEST)
    assert read.snapshot.last_checked_at == fetched and read.snapshot.evidence["fetchedAt"] == fetched.isoformat()


NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("seed,due", [
    ({"last_crawled_at": NOW - timedelta(days=29)}, None),
    ({"last_crawled_at": NOW - timedelta(days=31)}, lane.DUE_TOO_OLD),
    ({"last_crawled_at": (NOW - timedelta(days=29)).replace(tzinfo=None)}, None),   # a naive UTC column
    ({"last_crawled_at": (NOW - timedelta(days=2)).isoformat()}, None),
    ({}, lane.DUE_UNKNOWN),
    ({"last_crawled_at": "not a time"}, lane.DUE_UNKNOWN),
    # After a catalogue read (last_crawled_at = NOW()) the column holds the page read it carried forward.
    ({"last_crawled_at": NOW, "last_page_read_at": NOW - timedelta(days=3)}, None),
    ({"last_crawled_at": NOW, "last_page_read_at": NOW - timedelta(days=31)}, lane.DUE_TOO_OLD),
    ({"last_crawled_at": NOW, "last_page_read_at": "2026-09-01T00:00:00Z"}, lane.DUE_TOO_OLD),
    # Review round 2: a drain rewrite of seed_data leaves the column, so the snapshot never decides.
    ({"last_crawled_at": NOW, "last_page_read_at": NOW - timedelta(days=31), "read_source": None, "page_read_at": None},
     lane.DUE_TOO_OLD),
    ({"last_crawled_at": NOW, "last_page_read_at": NOW - timedelta(days=31), "page_read_at": NOW.isoformat()},
     lane.DUE_TOO_OLD),
    ({"last_crawled_at": NOW, "destination_verdict": liveness.VERDICT_LIVE}, None),
    ({"last_crawled_at": NOW, "destination_verdict": "dead"}, lane.DUE_NOT_LIVE),
    ({"last_crawled_at": NOW, "destination_verdict": "unverifiable"}, lane.DUE_NOT_LIVE),
    ({"last_crawled_at": NOW, "destination_failure_streak": 1}, lane.DUE_FAILURE_STREAK),
    ({"last_crawled_at": NOW, "destination_failure_streak": None}, None),
])
def test_a_page_read_is_due_by_age_and_by_liveness(seed, due):
    assert lane.page_read_due(seed, now=NOW, max_age_days=30) == due


@pytest.mark.parametrize("raw,days", [("", 30), ("7", 7), ("0", 1), ("-3", 1), ("x", 30)])
def test_the_page_read_max_age_is_at_least_a_day(monkeypatch, raw, days):
    monkeypatch.setenv(lane.PAGE_MAX_AGE_ENV, raw)
    assert lane.page_max_age_days() == days


def test_the_sweeps_read_is_unchanged_by_the_price_lane():
    """`read_brand_catalogue` without `want_products` keeps no products and stops only at the end."""
    async def go():
        handler = _store([[_other(1), _product()], [_other(2)]])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await liveness.read_brand_catalogue(client, HOST)

    read = asyncio.run(go())
    assert read.status == liveness.CATALOGUE_OK and read.handles == {"other-1", HANDLE, "other-2"}
    assert read.products == {} and read.page_set_cookies == []


@pytest.mark.parametrize("url,why", [
    (f"https://www.{HOST}/products/{HANDLE}", "other_host"),
    (f"https://{HOST}/en-gb/products/{HANDLE}", "path_not_catalogue_shaped"),
    (f"https://{HOST}/products/not-listed", "handle_not_read"),
    (DEST + "?variant=abc", "url_variant_unreadable"),
    (DEST + "?variant=123456789", "url_variant_not_listed"),
])
def test_a_seed_the_catalogue_cannot_speak_for_keeps_its_page_read(url, why):
    catalogue = _read_host([[_product()]], wanted=(HANDLE, "not-listed"))
    assert lane.seed_catalogue_read(catalogue, url) == (None, why)
    assert lane.seed_catalogue_read(None, DEST) == (None, "host_not_read")


def test_a_listed_handle_is_the_sweeps_observation():
    read = _seed_read()
    assert (read.host, read.handle) == (HOST, HANDLE)
    obs = read.observation
    assert (obs.verdict, obs.http_status, obs.final_url, obs.corroborated) == (liveness.VERDICT_LIVE, None, None, False)


@pytest.mark.parametrize("raw,on", [("true", True), ("1", True), ("ON", True), ("", False), ("false", False), ("0", False)])
def test_the_lane_is_off_unless_switched_on(monkeypatch, raw, on):
    monkeypatch.setenv(lane.FLAG, raw)
    assert lane.catalogue_refresh_enabled() is on


@pytest.mark.parametrize("raw,n", [("", 3), ("5", 5), ("0", 1), ("-2", 1), ("x", 3)])
def test_min_seeds_is_at_least_one(monkeypatch, raw, n):
    monkeypatch.setenv(lane.MIN_SEEDS_ENV, raw)
    assert lane.catalogue_min_seeds() == n


# ----------------------------------------------------------------- the real refresh


def _drain_row(product: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The seed row the retailer-ingest drain writes for this product, as `SELECT *` returns it."""
    from services import curated_brand_feed as feed
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

    record = feed.shopify_product_to_record(
        product or _product(), domain=HOST, category_path="beauty", brand_override="Example", currency="USD",
        source_role="brand_official", retailer_name=None, emit_native_variants=True)
    seed = dict(ingest_validated_jsonl([record])["seeds"][0])
    seed["seed_data"] = json.loads(seed["seed_data"]) if isinstance(seed["seed_data"], str) else seed["seed_data"]
    for col in ("utm_template", "partner_type", "disclosure_text", "attached_variant_id", "seller_ref", "seed_kind",
                "destination_checked_at", "destination_http_status", "destination_verdict", "last_crawled_at",
                "last_crawl_attempt_at", "last_page_read_at"):
        seed.setdefault(col, None)
    seed.setdefault("destination_failure_streak", 0)
    return seed


def _page_html(p50: float, p15: float) -> str:
    offers = [
        {"@type": "Offer", "price": str(p50), "priceCurrency": "USD", "availability": "https://schema.org/InStock",
         "name": f"{TITLE} - 1.7 fl. oz. / 50 mL", "@id": f"/products/{HANDLE}?variant={V50}#offer"},
        {"@type": "Offer", "price": str(p15), "priceCurrency": "USD", "availability": "https://schema.org/InStock",
         "name": f"{TITLE} - 0.5 fl. oz. / 15 mL", "@id": f"/products/{HANDLE}?variant={V15}#offer"},
    ]
    product = {"@context": "https://schema.org", "@type": "Product", "name": TITLE,
               "image": "https://cdn.brand.example/page-hero.jpg",
               "description": "A woody eau de toilette with a long, soft dry-down.", "offers": offers}
    return ("<html><head><title>" + TITLE + "</title>"
            '<link rel="canonical" href="' + DEST + '">'
            '<script type="application/ld+json">' + json.dumps(product) + "</script></head><body></body></html>")


def _page_snapshot(p50: float, p15: float):
    """What `resolve_external_offer` hands the refresh for the product page (its own evidence builders)."""
    from datetime import datetime, timezone

    from services.external_offers_service import (
        _extract_from_html, evidence_content_fields, evidence_variant_fields, snapshot_price_fields,
    )

    extracted = _extract_from_html(DEST, _page_html(p50, p15))
    amount, currency, price_read = snapshot_price_fields(extracted)
    return SimpleNamespace(
        canonical_url=extracted["canonical_url"], domain=HOST, title=extracted.get("title"),
        image_url=extracted.get("image_url"), price_amount=amount, price_currency=currency,
        availability=extracted.get("availability") or "unknown", last_checked_at=datetime.now(timezone.utc),
        evidence={"provider": extracted.get("evidence_provider"), "price_currency_source": "page" if currency else "unread",
                  **evidence_content_fields(extracted), **evidence_variant_fields(extracted), "price_read": price_read},
    )


class _Refresh:
    """The real `_refresh_external_seed_by_id` over one stored row; DB, fetch, liveness writer and
    projection stubbed and recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, row: Dict[str, Any], *, page_read_days_ago: Optional[float] = 1) -> None:
        import routes.employee_products as mod

        self.mod, self.stored = mod, copy.deepcopy(row)
        if page_read_days_ago is not None and self.stored.get("last_crawled_at") is None:
            # A row the refresh last read from its page that long ago (every refresh before the lane).
            self.stored["last_crawled_at"] = datetime.now(timezone.utc) - timedelta(days=page_read_days_ago)
        self.page = None
        self.fetched: List[str] = []
        self.observations: List[Any] = []
        self.projections: List[Dict[str, Any]] = []
        self.sql: List[Any] = []

        async def fetch_one(_q, values=None):
            return copy.deepcopy(self.stored) if values and values.get("id") == self.stored["id"] else None

        async def execute(query: str, values):
            # The clocks, as Postgres sets them: SET reads the row as it was before the statement.
            if "last_crawled_at = CASE" in query and values.get("read_the_served_product"):
                now = datetime.now(timezone.utc)
                if "last_page_read_at = NOW()" in query:
                    self.stored["last_page_read_at"] = now
                elif "last_page_read_at = COALESCE(last_page_read_at, last_crawled_at)" in query:
                    self.stored["last_page_read_at"] = self.stored.get("last_page_read_at") or self.stored.get("last_crawled_at")
                else:
                    assert "last_page_read_at" not in query
                self.stored["last_crawled_at"] = now
            persisted = json.loads(json.dumps(values, default=str))
            if isinstance(persisted.get("seed_data"), str):
                persisted["seed_data"] = json.loads(persisted["seed_data"])
            self.sql.append((query, persisted))
            # `title = COALESCE(title, :title)` and the same for image_url: a NULL never clears one.
            coalesced = {k for k in ("title", "image_url") if f"{k} = COALESCE({k}, :{k})" in query}
            self.stored.update({k: v for k, v in persisted.items()
                                if k not in ("id", "read_the_served_product", "read_canonical_url")
                                and not (k in coalesced and v is None)})

        async def resolve(*, url, observed=None, **_kw):
            self.fetched.append(url)
            assert self.page is not None, "the catalogue lane must not fetch the page"
            observed.update(status_code=200, final_url=url)
            return self.page

        async def record(seed_id, observation, *, now=None):
            self.observations.append(observation)
            return {"seed_id": seed_id, "verdict": observation.verdict, "failure_streak": 0}

        async def project(seed_id, **kwargs):
            self.projections.append(kwargs)
            return {"attempted": 1, "projected": 1}

        monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
        monkeypatch.setattr(mod.database, "fetch_one", fetch_one)
        monkeypatch.setattr(mod, "_execute_seed_data_stmt", execute)
        monkeypatch.setattr(mod, "resolve_external_offer", resolve)
        monkeypatch.setattr(mod.destination_liveness, "record_destination_observation", record)
        monkeypatch.setattr(mod, "_project_refreshed_seed_to_serving_surfaces", project)

    def page_read(self, p50: float, p15: float, *, page_read_column: bool = False) -> Dict[str, Any]:
        self.page = _page_snapshot(p50, p15)
        kwargs = {"page_read_column": True} if page_read_column else {}
        try:
            return asyncio.run(self.mod._refresh_external_seed_by_id(self.stored["id"], max_wait=0, **kwargs))
        finally:
            self.page = None

    def catalogue(self, read: lane.SeedCatalogueRead, *, page_read_column: bool = True) -> Dict[str, Any]:
        return asyncio.run(self.mod._refresh_external_seed_by_id(
            self.stored["id"], max_wait=0, catalogue_read=read, page_read_column=page_read_column))

    @property
    def variants(self) -> Dict[str, Dict[str, Any]]:
        return {v["variant_id"]: v for v in self.stored["seed_data"]["variants"]}

    def last_update(self) -> Dict[str, Any]:
        return [v for q, v in self.sql if "last_crawled_at" in q][-1]


def test_premise_the_drain_row_and_a_page_read(monkeypatch):
    run = _Refresh(monkeypatch, _drain_row())
    assert [v["variant_id"] for v in run.stored["seed_data"]["variants"]] == [V50, V15]
    result = run.page_read(80, 32)
    assert result["read_source"] == "page" and run.fetched == [DEST]
    assert run.stored["seed_data"]["snapshot"]["variant_refresh"]["status"] == "all_re_read"
    assert run.stored["seed_data"]["snapshot"]["description"].startswith("A woody")


def test_a_catalogue_read_re_prices_the_drains_variants_like_a_page_read(monkeypatch):
    run = _Refresh(monkeypatch, _drain_row())
    run.page_read(80, 32)
    result = run.catalogue(_seed_read(_product(p50="85.00", p15="34.00", a15=False)))

    assert run.fetched == [DEST], "no page fetch for the catalogue read"
    assert result["status"] == "success" and result["read_source"] == lane.READ_SOURCE
    assert result["snapshot_from_cache"] is False
    assert result["price_refresh"]["status"] == "applied"
    assert (run.stored["price_amount"], run.stored["price_currency"]) == (85.0, "USD")
    by_id = run.variants
    assert (by_id[V50]["price_amount"], by_id[V50]["price"], by_id[V50]["availability"]) == (85.0, 85.0, "in_stock")
    assert (by_id[V15]["price_amount"], by_id[V15]["availability"], by_id[V15]["in_stock"]) == (34.0, "out_of_stock", False)
    snapshot = run.stored["seed_data"]["snapshot"]
    assert snapshot["variant_refresh"]["status"] == "all_re_read"
    assert snapshot["read_source"] == lane.READ_SOURCE
    assert snapshot["price_amount"] == 85.0


def test_it_stamps_freshness_and_attempt_and_projects_with_the_currency_read(monkeypatch):
    run = _Refresh(monkeypatch, _drain_row())
    before = run.page_read(80, 32)
    stamp_before = run.stored["seed_data"]["snapshot"]["extracted_at"]
    run.catalogue(_seed_read(_product(p50="85.00")))
    update = run.last_update()
    assert update["read_the_served_product"] is True, "last_crawled_at = NOW()"
    query = [q for q, _ in run.sql if "last_crawled_at" in q][-1]
    assert "last_crawl_attempt_at = NOW()" in query
    assert run.stored["seed_data"]["snapshot"]["extracted_at"] >= stamp_before
    assert run.projections[-1] == {"price_source": "refresh", "currency_read": True}
    assert before["projection"] and len(run.projections) == 2


def test_it_records_the_sweeps_listed_observation(monkeypatch):
    run = _Refresh(monkeypatch, _drain_row())
    result = run.catalogue(_seed_read())
    (obs,) = run.observations
    assert (obs.verdict, obs.http_status, obs.note) == (liveness.VERDICT_LIVE, None, "listed in products.json")
    assert result["destination_refresh"]["verdict"] == liveness.VERDICT_LIVE


def test_it_rewrites_no_copy_image_gallery_title_or_canonical(monkeypatch):
    run = _Refresh(monkeypatch, _drain_row())
    run.page_read(80, 32)
    stored = copy.deepcopy(run.stored)
    run.catalogue(_seed_read(_product(p50="85.00")))
    after = run.stored
    for col in ("title", "image_url", "canonical_url", "domain", "destination_url"):
        assert after[col] == stored[col], col
    content_keys = ("title", "description", "image_url", "image_urls", "product_name", "brand",
                    "description_source", "image_urls_source")
    for key in content_keys:
        assert after["seed_data"].get(key) == stored["seed_data"].get(key), key
    for key in ("title", "description", "image_url", "image_urls", "canonical_url", "domain", "description_refused_source"):
        assert after["seed_data"]["snapshot"].get(key) == stored["seed_data"]["snapshot"].get(key), key
    # The list is re-priced, never replaced: same ids, titles, images, provenance.
    keep = ("variant_id", "sku", "title", "image_url", "options", "variant_id_provenance")
    assert [{k: v.get(k) for k in keep} for v in after["seed_data"]["variants"]] == \
        [{k: v.get(k) for k in keep} for v in stored["seed_data"]["variants"]]


def test_a_catalogue_read_never_replaces_a_page_read_variant_list(monkeypatch):
    """A seed whose variants an earlier PAGE read adopted (no merchant provenance) would have its list
    replaced by a richer page read. A catalogue read only re-prices: same list, same entries."""
    row = _drain_row()
    page_variant = {"variant_id": V50, "title": TITLE, "price_amount": 80.0, "price_currency": "USD",
                    "availability": "in_stock"}
    row["seed_data"]["variants"] = [page_variant]
    run = _Refresh(monkeypatch, row)
    result = run.catalogue(_seed_read(_product(p50="85.00", p15="34.00")))
    assert result["read_source"] == lane.READ_SOURCE and result["catalogue_fallback"] is None
    assert [v["variant_id"] for v in run.stored["seed_data"]["variants"]] == [V50]
    assert run.variants[V50]["price_amount"] == 85.0
    assert run.stored["seed_data"]["snapshot"]["variant_refresh"]["status"] == "all_re_read"


def test_a_catalogue_read_that_cannot_re_read_every_variant_is_dropped_for_the_page(monkeypatch):
    """Review P2-2(b): stored keys the catalogue cannot match (here a page read's own id) would have the lane
    remove `extracted_at` every night. The read is discarded unwritten and the page read in the same call."""
    row = _drain_row()
    row["seed_data"]["variants"] = [{"variant_id": "EXBASE", "title": TITLE, "price_amount": 80.0,
                                     "price_currency": "USD", "availability": "in_stock"}]
    run = _Refresh(monkeypatch, row)
    run.page = _page_snapshot(85, 34)
    result = run.catalogue(_seed_read(_product(p50="85.00", p15="34.00")))
    assert result["catalogue_fallback"] == "not_all_re_read" and result["read_source"] == "page"
    assert run.fetched == [DEST]
    assert len([q for q, _ in run.sql if "last_crawled_at" in q]) == 1, "the catalogue read wrote nothing"
    assert "read_source" not in run.stored["seed_data"]["snapshot"]
    assert len(run.observations) == 1 and run.observations[0].http_status == 200


def test_a_page_read_is_due_after_the_max_age_and_on_a_destination_not_known_live(monkeypatch):
    """Review P2-2(a): a listed handle can still 404, and only the page read sees it."""
    run = _Refresh(monkeypatch, _drain_row(), page_read_days_ago=31)
    run.page = _page_snapshot(80, 32)
    result = run.catalogue(_seed_read())
    assert result["catalogue_fallback"] == "page_read_due:" + lane.DUE_TOO_OLD and run.fetched == [DEST]
    assert datetime.now(timezone.utc) - run.stored["last_page_read_at"] < timedelta(minutes=1), "the page read is stamped"
    # Now a page read is a day old ... but the destination has a failure streak: still the page.
    run.stored["destination_failure_streak"] = 1
    result = run.catalogue(_seed_read())
    assert result["catalogue_fallback"] == "page_read_due:" + lane.DUE_FAILURE_STREAK and len(run.fetched) == 2
    run.stored["destination_failure_streak"] = 0
    run.page = None
    assert run.catalogue(_seed_read())["read_source"] == lane.READ_SOURCE


def test_a_catalogue_read_carries_the_last_page_read_forward(monkeypatch):
    """Review round 2: a catalogue read moves last_crawled_at, so the page read it replaces goes to
    last_page_read_at, which the drain's whole-seed_data upsert never names."""
    run = _Refresh(monkeypatch, _drain_row(), page_read_days_ago=20)
    page_read = run.stored["last_crawled_at"]
    run.catalogue(_seed_read(_product(p50="85.00")))
    assert run.stored["last_page_read_at"] == page_read
    assert datetime.now(timezone.utc) - run.stored["last_crawled_at"] < timedelta(minutes=1)
    # The next catalogue read keeps the same page read, not its predecessor's NOW().
    run.catalogue(_seed_read(_product(p50="86.00")))
    assert run.stored["last_page_read_at"] == page_read
    # The drain rewrites seed_data whole: the column is not in it, and still decides.
    run.stored["seed_data"] = _drain_row()["seed_data"]
    run.stored["last_page_read_at"] = page_read - timedelta(days=11)
    run.page = _page_snapshot(85, 32)
    result = run.catalogue(_seed_read())
    assert result["catalogue_fallback"] == "page_read_due:" + lane.DUE_TOO_OLD and run.fetched == [DEST]
    assert datetime.now(timezone.utc) - run.stored["last_page_read_at"] < timedelta(minutes=1)


def test_a_page_read_in_a_lane_run_stamps_the_column_and_one_outside_it_does_not(monkeypatch):
    run = _Refresh(monkeypatch, _drain_row())
    run.page_read(80, 32)
    assert run.stored["last_page_read_at"] is None, "the employee route never names the column"
    assert all("last_page_read_at" not in q for q, _ in run.sql)
    run.page_read(80, 32, page_read_column=True)
    assert datetime.now(timezone.utc) - run.stored["last_page_read_at"] < timedelta(minutes=1)


def test_a_catalogue_read_without_the_column_is_not_taken(monkeypatch):
    """It could not carry the page read forward, and would leave last_crawled_at looking like one."""
    run = _Refresh(monkeypatch, _drain_row())
    run.page = _page_snapshot(80, 32)
    result = run.catalogue(_seed_read(), page_read_column=False)
    assert result["catalogue_fallback"] == "no_page_read_column" and result["read_source"] == "page"
    assert run.fetched == [DEST] and all("last_page_read_at" not in q for q, _ in run.sql)


def test_freshness_is_stamped_at_the_catalogue_fetch_not_the_rows_turn(monkeypatch):
    run = _Refresh(monkeypatch, _drain_row())
    read = _seed_read(_product(p50="85.00"))
    fetched = datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)
    read = dataclasses.replace(read, snapshot=dataclasses.replace(read.snapshot, last_checked_at=fetched))
    run.catalogue(read)
    snapshot = run.stored["seed_data"]["snapshot"]
    assert snapshot["extracted_at"].startswith("2026-11-01T07:00:00") and snapshot["refreshed_at"].startswith("2026-11-01T07:00:00")


def test_a_catalogue_read_of_another_host_is_not_this_destinations(monkeypatch):
    run = _Refresh(monkeypatch, _drain_row())
    run.page = _page_snapshot(80, 32)
    result = run.catalogue(dataclasses.replace(_seed_read(), host="sister-store.example"))
    assert result["catalogue_fallback"] == "not_this_destination" and run.fetched == [DEST]


def test_a_page_read_after_a_catalogue_read_takes_its_own_source_back(monkeypatch):
    run = _Refresh(monkeypatch, _drain_row())
    run.catalogue(_seed_read())
    assert run.stored["seed_data"]["snapshot"]["read_source"] == lane.READ_SOURCE
    run.page_read(80, 32)
    assert "read_source" not in run.stored["seed_data"]["snapshot"]


def test_the_urls_variant_prices_the_column(monkeypatch):
    row = _drain_row()
    row["destination_url"] = row["canonical_url"] = DEST + "?variant=" + V15
    run = _Refresh(monkeypatch, row)
    run.catalogue(_seed_read(_product(p50="85.00", p15="34.00"), url=row["destination_url"]))
    assert run.stored["price_amount"] == 34.0


def _writes(run: "_Refresh") -> List[Dict[str, Any]]:
    return [v for q, v in run.sql if "last_crawled_at" in q]


def test_the_currency_guard_still_refuses_and_the_page_decides(monkeypatch):
    run = _Refresh(monkeypatch, _drain_row())
    run.page_read(80, 32)
    run.page = _page_snapshot(80, 32)
    result = run.catalogue(_seed_read(_product(p50="99.00"), base="CAD", cookie="CAD"))
    assert result["catalogue_discarded"]["price_refresh"]["status"] == "skipped_currency_mismatch"
    assert result["catalogue_fallback"] == "not_all_re_read" and result["read_source"] == "page"
    assert all(w["price_amount"] == 80.0 and w["price_currency"] == "USD" for w in _writes(run)), \
        "the CAD read is never written"
    assert run.variants[V50]["price_amount"] == 80.0, "a CAD read never prices a USD variant"
    assert len(_writes(run)) == 2 and len(run.projections) == 2, "two page reads, no catalogue write"


def test_an_unread_catalogue_price_is_counted_and_the_page_decides(monkeypatch):
    run = _Refresh(monkeypatch, _drain_row())
    run.page = _page_snapshot(80, 32)
    result = run.catalogue(_seed_read(_product(p50="80,00")))
    assert result["catalogue_discarded"]["price_refresh"] == {
        **result["catalogue_discarded"]["price_refresh"], "status": "skipped_unreadable", "reason": "catalogue_price_unread"}
    assert result["catalogue_fallback"] == "not_all_re_read" and run.fetched == [DEST]
    assert len(_writes(run)) == 1 and run.stored["price_amount"] == 80.0


def test_a_read_planned_for_another_destination_falls_back_to_the_page(monkeypatch):
    row = _drain_row()
    row["destination_url"] = f"https://{HOST}/products/renamed-handle"
    run = _Refresh(monkeypatch, row)
    run.page = _page_snapshot(80, 32)
    result = run.catalogue(_seed_read())
    assert run.fetched == [row["destination_url"]] and result["read_source"] == "page"


def test_a_served_url_that_is_not_the_destination_is_not_called_read(monkeypatch):
    row = _drain_row()
    row["canonical_url"] = f"https://{HOST}/products/another-product"
    run = _Refresh(monkeypatch, row)
    crawled = run.stored["last_crawled_at"]
    result = run.catalogue(_seed_read(_product(p50="85.00")))
    assert run.last_update()["read_the_served_product"] is False
    # Not a read of the served product: neither clock moves, and the column is not named.
    assert run.stored["last_crawled_at"] == crawled and run.stored["last_page_read_at"] is None
    assert all("last_page_read_at" not in q for q, _ in run.sql)
    assert run.observations == [] and result["destination_refresh"]["reason"] == "not_the_served_url"
    assert not run.projections and "extracted_at" not in run.stored["seed_data"]["snapshot"]


def _id_less_single_variant_row() -> Dict[str, Any]:
    row = _drain_row()
    only = dict(row["seed_data"]["variants"][0])
    # No id left to match a read variant by (not even its SKU): only the product-level fallback can re-read it.
    only.pop("variant_id"), only.pop("id"), only.pop("sku")
    row["seed_data"]["variants"] = [only]
    return row


def test_a_single_variant_seed_takes_the_product_price_only_off_a_one_value_list(monkeypatch):
    run = _Refresh(monkeypatch, _id_less_single_variant_row())
    result = run.catalogue(_seed_read(_product(p50="85.00", p15="85.00")))
    assert result["read_source"] == lane.READ_SOURCE
    assert run.stored["seed_data"]["snapshot"]["variant_refresh"]["status"] == "all_re_read"
    assert run.stored["seed_data"]["variants"][0]["price_amount"] == 85.0


def test_a_single_variant_seed_off_a_two_value_list_is_page_read(monkeypatch):
    run = _Refresh(monkeypatch, _id_less_single_variant_row())
    run.page = _page_snapshot(85, 34)
    # Two variants at two prices: the product price is not this variant's, so it is not re-read.
    assert run.catalogue(_seed_read(_product(p50="85.00", p15="34.00")))["catalogue_fallback"] == "not_all_re_read"
    assert run.fetched == [DEST]


# ----------------------------------------------------------------- the real batch


def _batch(monkeypatch, dests: Dict[str, str], *, on: bool = True, catalogues=None, refresh_result=None, plan=None,
           column: bool = True, kwargs_seen: Optional[List[Dict[str, Any]]] = None):
    """Run the real batch over `dests` (seed id -> destination URL) with an injected refresher. Every seed
    was page-read yesterday unless `plan` (seed id -> plan row fields) says otherwise."""
    import services.external_referral_readiness as err

    if on:
        monkeypatch.setenv(lane.FLAG, "true")
    seed_ids = list(dests)
    monkeypatch.setattr(err, "get_external_referral_refresh_candidate_seed_ids",
                        lambda *a, **k: asyncio.sleep(0, result=seed_ids))
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts",
                        lambda ids: asyncio.sleep(0, result={s: cp.host_of(d) for s, d in dests.items()}))
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    rows = {s: {"id": s, "destination_url": d, "last_crawled_at": yesterday, **((plan or {}).get(s) or {})}
            for s, d in dests.items()}
    monkeypatch.setattr(err, "_fetch_refresh_candidate_dests", lambda ids: asyncio.sleep(0, result=rows))
    monkeypatch.setattr(err, "_page_read_column_ready", lambda: asyncio.sleep(0, result=column))
    reads: List[Any] = []

    async def read_host(host, wanted, **kw):
        reads.append((host, set(wanted)))
        if isinstance(catalogues, Exception):
            raise catalogues
        return (catalogues or {})[host]

    monkeypatch.setattr(lane, "read_host_catalogue", read_host)
    calls: List[Any] = []

    async def refresh(seed_id: str, **kwargs):
        calls.append((seed_id, kwargs.get("catalogue_read")))
        if kwargs_seen is not None:
            kwargs_seen.append(kwargs)
        out = {"status": "success", "price_refresh": {"status": "unchanged"}, "projection": {}}
        out["read_source"] = lane.READ_SOURCE if kwargs.get("catalogue_read") else "page"
        return refresh_result(seed_id, kwargs) if refresh_result else out

    summary = asyncio.run(err.run_external_referral_refresh_batch(refresh_seed_by_id=refresh, limit=len(dests)))
    return summary, reads, calls


def _ok_catalogue(*handles: str) -> lane.HostCatalogue:
    return lane.HostCatalogue(host=HOST, status=lane.STATUS_OK, currency="USD", proof=lane.PROOF_BASE_AND_PAGE_COOKIE,
                              products={h: _product(h) for h in handles}, pages_read=1, wanted=len(handles))


SEEDS = {
    "s1": f"https://{HOST}/products/a",
    "s2": f"https://{HOST}/products/b",
    "s3": f"https://{HOST}/products/c",
    "s4": f"https://{HOST}/en-gb/products/d",   # not catalogue-shaped: page read
    "s5": "https://small.example/products/x",    # 2 seeds: below the minimum
    "s6": "https://small.example/products/y",
}


def test_off_by_default_the_batch_is_unchanged(monkeypatch):
    seen: List[Dict[str, Any]] = []
    summary, reads, calls = _batch(monkeypatch, SEEDS, on=False, column=False, kwargs_seen=seen)
    assert reads == [] and all(read is None for _, read in calls)
    assert seen == [{}] * len(SEEDS), "the refresh is called exactly as before the lane"
    assert summary["catalogue"]["enabled"] is False and summary["catalogue"]["hosts_planned"] == 0
    assert summary["catalogue"]["page_read_column_missing"] is False


def test_on_without_the_page_read_column_the_lane_stays_off(monkeypatch):
    seen: List[Dict[str, Any]] = []
    summary, reads, calls = _batch(monkeypatch, SEEDS, catalogues={HOST: _ok_catalogue("a", "b", "c")},
                                   column=False, kwargs_seen=seen)
    assert reads == [] and seen == [{}] * len(SEEDS)
    assert summary["catalogue"]["enabled"] is False and summary["catalogue"]["page_read_column_missing"] is True


@pytest.mark.parametrize("answer,ready", [({"?column?": 1}, True), (None, False), (RuntimeError("boom"), False)])
def test_the_page_read_column_check_fails_closed(monkeypatch, answer, ready):
    import services.external_referral_readiness as err

    async def fetch_one(query, values):
        assert values == {"column": "last_page_read_at"} and "current_schemas(false)" in query
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(err.database, "fetch_one", fetch_one)
    assert asyncio.run(err._page_read_column_ready()) is ready


def test_a_lane_run_tells_every_refresh_the_column_is_there(monkeypatch):
    seen: List[Dict[str, Any]] = []
    _batch(monkeypatch, SEEDS, catalogues={HOST: _ok_catalogue("a", "b", "c")}, kwargs_seen=seen)
    assert len(seen) == len(SEEDS) and all(kw.get("page_read_column") is True for kw in seen)
    assert sum(1 for kw in seen if kw.get("catalogue_read") is not None) == 3


def test_on_a_host_is_read_once_and_its_rows_priced_from_memory(monkeypatch):
    summary, reads, calls = _batch(monkeypatch, SEEDS, catalogues={HOST: _ok_catalogue("a", "c")})
    assert reads == [(HOST, {"a", "b", "c"})], "one read per host, only hosts with >= 3 catalogue-shaped seeds"
    by_seed = dict(calls)
    assert {s for s, r in by_seed.items() if r is not None} == {"s1", "s3"}
    assert by_seed["s1"].handle == "a" and by_seed["s1"].snapshot.price_amount == 80.0
    assert by_seed["s2"] is None and by_seed["s4"] is None and by_seed["s5"] is None
    cat = summary["catalogue"]
    assert (cat["enabled"], cat["hosts_planned"], cat["hosts_read"], cat["rows_read"]) == (True, 1, 1, 2)
    assert cat["rows"] == {"read": 2, "handle_not_read": 1, "path_not_catalogue_shaped": 1}
    assert cat["host_status"] == {lane.STATUS_OK: 1}
    assert cat["pages_read"] == 1 and cat["hosts"][0]["host"] == HOST
    assert summary["refreshed"] == 6 and summary["origin_reads"] == 6


def test_a_host_that_cannot_be_priced_keeps_every_page_read(monkeypatch):
    refused = lane.HostCatalogue(host=HOST, status=lane.STATUS_CURRENCY_CONFLICT)
    summary, reads, calls = _batch(monkeypatch, SEEDS, catalogues={HOST: refused})
    assert len(reads) == 1 and all(read is None for _, read in calls)
    assert summary["catalogue"]["rows"] == {lane.STATUS_CURRENCY_CONFLICT: 4}, "every row on the host, s4 included"


def test_the_minimum_is_configurable(monkeypatch):
    monkeypatch.setenv(lane.MIN_SEEDS_ENV, "2")
    _, reads, _ = _batch(monkeypatch, SEEDS, catalogues={HOST: _ok_catalogue("a"), "small.example": _ok_catalogue()})
    assert {h for h, _ in reads} == {HOST, "small.example"}


def test_a_seed_due_its_page_read_is_never_asked_of_the_catalogue(monkeypatch):
    plan = {"s2": {"last_crawled_at": datetime.now(timezone.utc) - timedelta(days=40)},
            "s7": {"destination_verdict": "dead"}}
    dests = {**SEEDS, "s7": f"https://{HOST}/products/e", "s8": f"https://{HOST}/products/f"}
    summary, reads, calls = _batch(monkeypatch, dests, catalogues={HOST: _ok_catalogue("a", "c", "f")}, plan=plan)
    assert reads == [(HOST, {"a", "c", "f"})], "a due seed costs the catalogue no page"
    by_seed = dict(calls)
    assert by_seed["s2"] is None and by_seed["s7"] is None and by_seed["s8"] is not None
    rows = summary["catalogue"]["rows"]
    assert rows["page_read_due:" + lane.DUE_TOO_OLD] == 1 and rows["page_read_due:" + lane.DUE_NOT_LIVE] == 1
    assert summary["catalogue"]["page_max_age_days"] == lane.PAGE_MAX_AGE_DEFAULT_DAYS


def test_due_seeds_do_not_count_toward_the_minimum(monkeypatch):
    plan = {"s1": {"last_crawled_at": None}}
    _, reads, _ = _batch(monkeypatch, SEEDS, catalogues={HOST: _ok_catalogue("b", "c")}, plan=plan)
    assert reads == [], "two eligible seeds are below the minimum of three"


def test_the_routes_fallbacks_are_counted(monkeypatch):
    def result(seed_id, kwargs):
        fallback = "not_all_re_read" if kwargs.get("catalogue_read") and seed_id == "s3" else None
        return {"status": "success", "price_refresh": {"status": "unchanged"}, "projection": {},
                "read_source": "page" if fallback or not kwargs.get("catalogue_read") else lane.READ_SOURCE,
                "catalogue_fallback": fallback}

    summary, _, _ = _batch(monkeypatch, SEEDS, catalogues={HOST: _ok_catalogue("a", "c")}, refresh_result=result)
    assert summary["catalogue"]["fallbacks"] == {"not_all_re_read": 1}
    assert summary["catalogue"]["rows_read"] == 1


def test_a_stopped_run_during_the_catalogue_read_defers_the_row_unstamped(monkeypatch):
    summary, _, calls = _batch(monkeypatch, {"s1": SEEDS["s1"], "s2": SEEDS["s2"], "s3": SEEDS["s3"]},
                               catalogues=crawl_global_pacer.GlobalPaceStopped("budget"))
    assert calls == [], "no row is refreshed (or stamped) off a read the pacer refused"
    assert summary["failed"] == 0, "a refused request is a deferral, never a failure"
    assert summary["skipped_for_budget"] == 3 and summary["stopped_early"] is True


# ----------------------------------------------------------------- the job


def test_the_job_hands_the_catalogue_read_through_and_never_invents_one(monkeypatch):
    import jobs.external_referral_refresh as job

    seen: List[Dict[str, Any]] = []

    async def fake(seed_id, **kwargs):
        seen.append(kwargs)
        return {}

    monkeypatch.setattr(job, "_refresh_external_seed_by_id", fake)
    marker = object()
    asyncio.run(job._refresh_unbounded("s1"))
    asyncio.run(job._refresh_unbounded("s1", catalogue_read=marker))
    asyncio.run(job._refresh_unbounded("s1", catalogue_read=marker, page_read_column=True))
    asyncio.run(job._refresh_unbounded("s1", page_read_column=True))
    assert seen == [{"max_wait": 0}, {"max_wait": 0, "catalogue_read": marker},
                    {"max_wait": 0, "catalogue_read": marker, "page_read_column": True},
                    {"max_wait": 0, "page_read_column": True}]


def test_the_job_prints_one_catalogue_line(monkeypatch, capsys):
    import sys

    import jobs.external_referral_refresh as job
    from db.database import database as db_singleton

    async def noop() -> None:
        return None

    async def fake_batch(**_kwargs):
        return {"status": "success", "catalogue": {"enabled": True, "rows_read": 7}}

    monkeypatch.setattr(db_singleton, "connect", noop)
    monkeypatch.setattr(db_singleton, "disconnect", noop)
    monkeypatch.setattr(job, "run_external_referral_refresh_batch", fake_batch)
    monkeypatch.setattr(sys, "argv", ["external_referral_refresh", "--limit", "3"])
    assert job.main() == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("CATALOGUE_REFRESH ")]
    assert lines == ['CATALOGUE_REFRESH {"enabled":true,"rows_read":7}']
