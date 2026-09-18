"""The Tier B cart-permalink preflight, against a mocked storefront (httpx.MockTransport).

Every fixture is a minimal fragment of what the live probe saw on 2026-09-18
(reports/tierb_cart_permalink_2026_09_18/results_*.json): the redirect chains are the real
shapes with made-up tokens, and the bodies carry only the distinguishing text — an
HTML-escaped `ProductVariant/<id>` inside `&quot;`, "Link no longer exists", "isn't set up to
receive orders yet".
"""

import asyncio
import html
import importlib.util
import json
import logging
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx
import pytest

import services.shopify_cart_link_preflight as pf
from services.outbound_links_service import CartPrefill, cart_prefill_refusal
from services.shopify_cart_link_preflight import PreflightResult, Verdict, classify_landing, preflight


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """No test here may reach the network: anything not on a MockTransport fails loudly."""

    async def refuse_async(self, request):
        raise AssertionError(f"un-mocked network request: {request.method} {request.url.host}")

    def refuse_sync(self, request):
        raise AssertionError(f"un-mocked network request: {request.method} {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse_sync)


VID = "50041364447509"
CLICK = "clk_test_judydoll"
EMAIL = "ucp-probe@pivota.cc"
ADDR1 = "1209 Orange Street"
BUYER = CartPrefill(
    email=EMAIL, first_name="Pivota", last_name="Probe", address1=ADDR1, city="Wilmington",
    province="DE", zip="19801", country="US", phone="+12025550142",
)
TOKEN = "hWNGxZONBs9DRwvyX0myoSvM"


# --- fixtures modelled on the live bodies ----------------------------------------------------


def merchandise_line(variant, *, typename="ProductVariantMerchandise", variant_id=None):
    """One `MerchandiseLine`, shaped like judydoll's live checkout (2026-09-18)."""
    return {
        "stableId": "dd930d1d-6cc3-45cd-a188-11258a924e5e",
        "merchandise": {
            "__typename": typename,
            "id": f"gid://shopify/ProductVariantMerchandise/{variant}",
            "digest": "43f27e47c6ed0a6b3e8ead7c4ae62038",
            "variantId": f"gid://shopify/ProductVariant/{variant_id or variant}",
            "title": "Single Eyeshadow",
        },
        "lineComponents": [],
        "legacyFee": False,
        "__typename": "MerchandiseLine",
    }


def checkout_state(*, variant=VID, click=CLICK, total=("9.99", "USD"), country="US", extra=None,
                   lines=None, attributes=None):
    """The checkout's serialized state, keyed the way the live page keys it: the policy set's
    EMPTY `buyerIdentity: []`, a `queryString` that ECHOES the click id (it must never count),
    `merchandiseLines`, `note.customAttributes`, `buyerIdentity.customer.countryCode`."""
    state = {
        "session": {"policies": {"payment": [], "buyerIdentity": [], "merchandise": [],
                                 "__typename": "PolicyFactSet"}},
        "shopConfig": {"queryString": f"_r=AQAB&attributes%5Bpivota_click_id%5D={CLICK}&cart_link_id=NM6Naf3v"},
        "delivery": {"deliveryLines": []},
        "merchandise": {"taxesIncluded": False, "merchandiseLines": (
            lines if lines is not None else ([merchandise_line(variant)] if variant else []))},
        "note": {"customAttributes": (
            attributes if attributes is not None
            else ([{"key": "pivota_click_id", "value": click, "__typename": "NoteAttribute"}] if click else [])),
            "message": "", "__typename": "Note"},
    }
    if country is not None:
        state["buyerIdentity"] = {"shopUser": None, "customer": {
            "presentmentCurrency": (total or ("", "USD"))[1], "countryCode": country,
            "market": {"handle": country.lower(), "__typename": "Market"}, "__typename": "GuestProfile"}}
    if total:
        state["totalAmount"] = {"value": {"amount": total[0], "currencyCode": total[1]}}
    state.update(extra or {})
    return state


def checkout_body(*, variant=VID, click=CLICK, email=EMAIL, address1=ADDR1, total=("9.99", "USD"),
                  country="US", state=None):
    """A /checkouts/cn/ page: the state JSON HTML-escaped (`&quot;`) inside an attribute, as the
    live page serializes it, plus rendered inputs. `deliveryLines: []` because shipping rates load
    later via JS (the reason shipping is unverifiable here)."""
    if state is None:
        state = checkout_state(variant=variant, click=click, total=total, country=country)
    serialized = html.escape(json.dumps(state, separators=(",", ":")), quote=True)
    inputs = ""
    if email:
        inputs += f'<input name="email" value="{html.escape(email)}">'
    if address1:
        inputs += f'<input name="address1" value="{html.escape(address1)}">'
    return (
        "<!DOCTYPE html><html><head><title>Checkout - Judydoll</title>"
        f'<meta name="serialized-session" content="{serialized}"></head><body>'
        f"{inputs}</body></html>"
    )


LINK_GONE_BODY = "<html><head><title>Link no longer exists</title></head><body>Link no longer exists</body></html>"
NOT_ACCEPTING_BODY = (
    "<html><head><title>Checkout</title></head><body><h1>This store isn&rsquo;t set up to receive "
    "orders yet</h1></body></html>"
)
PLAIN_403_BODY = "<html><body>Access denied</body></html>"


def catalog(*variants, product_title="Single Eyeshadow"):
    return {"products": [{"title": product_title, "handle": "single-eyeshadow", "variants": list(variants)}]}


def cvar(vid=VID, *, available=True, price="9.99", title="#D02 Static Pulse", requires_shipping=True):
    return {"id": int(vid), "title": title, "available": available, "price": price,
            "requires_shipping": requires_shipping}


class Store:
    """A routed fake storefront. Routes are keyed by (host, path); a value is an httpx.Response
    or a callable(request) -> Response. An unrouted request fails the test."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        route = self.routes.get((request.url.host, request.url.path))
        if route is None:
            raise AssertionError(f"unrouted request {request.url.host}{request.url.path}")
        return route(request) if callable(route) else route

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def paths(self):
        return [(r.url.host, r.url.path) for r in self.requests]

    def hit(self, path_prefix):
        return [r for r in self.requests if r.url.path.startswith(path_prefix)]


def redirect(status, location):
    return lambda request: httpx.Response(status, headers={"location": location})


def page(status, body):
    return lambda request: httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


def products_json(first_page, *later_pages):
    pages = [first_page, *later_pages]

    def respond(request):
        n = int(request.url.params.get("page", "1"))
        body = pages[n - 1] if n <= len(pages) else {"products": []}
        return httpx.Response(200, json=body)

    return respond


def cart_to_checkout(host="judydoll.com", vid=VID, body=None, status=200):
    """The judydoll chain: /cart/<id>:1 302 -> /checkouts/cn/<token>/en-us -> 200."""
    checkout_path = f"/checkouts/cn/{TOKEN}/en-us"
    return {
        (host, f"/cart/{vid}:1"): redirect(
            302, f"https://{host}{checkout_path}?_r=AQAB&attributes%5Bpivota_click_id%5D={CLICK}&cart_link_id=NM6Naf3v"
        ),
        (host, checkout_path): page(status, checkout_body() if body is None else body),
    }


async def run(store, host="judydoll.com", **kwargs):
    kwargs.setdefault("click_id", CLICK)
    kwargs.setdefault("market", kwargs["buyer"].country if kwargs.get("buyer") else "US")
    async with store.client() as client:
        return await preflight(host, client=client, **kwargs)


# --- ELIGIBLE: accepting -------------------------------------------------------------------


async def test_judydoll_shape_is_eligible_with_buyer():
    store = Store({("judydoll.com", "/products.json"): products_json(catalog(cvar())), **cart_to_checkout()})
    result = await run(store, variant_id=VID, buyer=BUYER)
    assert result.verdict is Verdict.ELIGIBLE
    assert result.retryable is False
    assert result.shipping_verified is False
    assert result.variant_id == VID and result.variant_source == "caller"
    assert result.variant_title == "#D02 Static Pulse" and result.product_title == "Single Eyeshadow"
    assert result.price == "9.99"
    assert (result.checkout_total, result.checkout_currency) == ("9.99", "USD")
    assert result.final_status == 200 and result.final_host == "judydoll.com"
    assert [s for s, _ in result.chain] == [302, 200]
    assert result.chain[0][1].startswith(f"https://judydoll.com/cart/{VID}:1?attributes[pivota_click_id]={CLICK}")
    # the permalink that went out really carried the prefill
    sent = str(store.hit("/cart/")[0].url)
    assert "checkout[email]=ucp-probe%40pivota.cc" in sent or "checkout%5Bemail%5D=ucp-probe%40pivota.cc" in sent


def test_without_a_buyer_eligible_never_asks_for_email_or_address():
    """The Reap path: no buyer in the link, so none can be expected on the page."""
    body = checkout_body(email=None, address1=None)
    assert EMAIL not in body and ADDR1 not in body
    verdict, missing = _classify(f"https://s.com/checkouts/cn/{TOKEN}", body=body, buyer=None)
    assert verdict is Verdict.ELIGIBLE and missing == ()


async def test_eligible_without_a_buyer_needs_no_email_on_the_page():
    store = Store({("judydoll.com", "/products.json"): products_json(catalog(cvar())),
                   **cart_to_checkout(body=checkout_body(email=None, address1=None))})
    result = await run(store, variant_id=VID)
    assert result.verdict is Verdict.ELIGIBLE
    assert "checkout" not in str(store.hit("/cart/")[0].url.query)


async def test_heartpercent_shape_is_eligible_and_shipping_is_still_unverified():
    """heartpercent has no US rate for the item, but the rate loads by JS: the HTTP page is a
    normal prefilled checkout with deliveryLines: []. This preflight CANNOT tell, and says so."""
    host, vid = "heartpercent.us", "53109000536373"
    body = checkout_body(variant=vid)
    assert "deliveryLines&quot;:[]" in body
    store = Store({(host, "/products.json"): products_json(catalog(cvar(vid))),
                   **cart_to_checkout(host=host, vid=vid, body=body)})
    result = await run(store, host=host, variant_id=vid, buyer=BUYER)
    assert result.verdict is Verdict.ELIGIBLE
    assert result.shipping_verified is False
    assert result.to_dict()["shipping_verified"] is False


def test_shipping_verified_cannot_be_set_by_anyone():
    with pytest.raises(TypeError):
        PreflightResult(host="x.com", verdict=Verdict.ELIGIBLE, shipping_verified=True)  # type: ignore[call-arg]
    assert PreflightResult(host="x.com", verdict=Verdict.ELIGIBLE).shipping_verified is False


def test_the_docstrings_carry_the_shipping_and_exposure_caveats():
    doc = pf.__doc__
    assert "ABANDONED SHOPIFY CHECKOUT" in doc
    assert "NEVER EXPOSE THIS TO A USER-SUPPLIED OR ANONYMOUS DOMAIN" in doc
    rdoc = PreflightResult.__doc__
    for needle in ("JavaScript", "heartpercent", "anua", "skin1004", "downstream"):
        assert needle in rdoc


async def test_a_cross_host_redirect_through_shop_app_still_lands_eligible():
    """poopourri.com -> pourri.com -> shop.app -> pourri.com/checkouts/cn/..."""
    vid = "45064135868513"
    cpath = f"/checkouts/cn/{TOKEN}/en-us"
    store = Store({
        ("poopourri.com", "/products.json"): products_json(catalog(cvar(vid))),
        ("poopourri.com", f"/cart/{vid}:1"): redirect(301, f"https://pourri.com/cart/{vid}:1?checkout%5Bemail%5D=ucp-probe%40pivota.cc"),
        ("pourri.com", f"/cart/{vid}:1"): redirect(302, f"https://shop.app/checkout/10201629/cn/{TOKEN}/en-us/shoppay?shop_pay_token=eyJ.PAYLOAD.sig"),
        ("shop.app", f"/checkout/10201629/cn/{TOKEN}/en-us/shoppay"): redirect(302, f"https://pourri.com{cpath}?skip_shop_pay=true"),
        ("pourri.com", cpath): page(200, checkout_body(variant=vid)),
    })
    result = await run(store, host="poopourri.com", variant_id=vid, buyer=BUYER)
    assert result.verdict is Verdict.ELIGIBLE
    assert result.final_host == "pourri.com"
    assert [s for s, _ in result.chain] == [301, 302, 302, 200]
    assert "PAYLOAD" not in json.dumps(result.to_dict())


# --- ELIGIBLE: refusing --------------------------------------------------------------------


def _classify(final_url, status=200, body=None, *, chain=None, buyer=BUYER, variant=VID):
    chain = chain or [(302, f"https://judydoll.com/cart/{VID}:1?attributes[pivota_click_id]={CLICK}"), (status, final_url)]
    return classify_landing(chain=chain, final_status=status, body=checkout_body() if body is None else body,
                            variant_id=variant, click_id=CLICK, buyer=buyer)


def test_a_200_at_the_cart_or_the_home_page_is_not_eligible():
    assert _classify("https://judydoll.com/cart")[0] is Verdict.UNCLASSIFIED
    assert _classify("https://judydoll.com/")[0] is Verdict.UNCLASSIFIED


def test_a_checkout_page_missing_the_variant_gid_is_not_eligible():
    verdict, missing = _classify(f"https://judydoll.com/checkouts/cn/{TOKEN}/en-us", body=checkout_body(variant=None))
    assert verdict is Verdict.UNCLASSIFIED and "variant" in missing


def test_a_different_variant_whose_id_starts_with_ours_is_not_our_variant():
    verdict, missing = _classify(
        f"https://judydoll.com/checkouts/cn/{TOKEN}/en-us", body=checkout_body(variant=VID + "1"))
    assert verdict is Verdict.UNCLASSIFIED and "variant" in missing


def test_a_checkout_page_missing_the_click_id_is_not_eligible():
    verdict, missing = _classify(f"https://judydoll.com/checkouts/cn/{TOKEN}/en-us", body=checkout_body(click=None))
    assert verdict is Verdict.UNCLASSIFIED and missing == ("click_id",)


def test_a_checkout_with_the_variant_but_without_the_email_is_prefill_missing():
    verdict, missing = _classify(f"https://judydoll.com/checkouts/cn/{TOKEN}/en-us", body=checkout_body(email=None))
    assert verdict is Verdict.CHECKOUT_PREFILL_MISSING and missing == ("email",)


def test_a_checkout_with_the_variant_but_without_address1_is_prefill_missing():
    verdict, missing = _classify(f"https://judydoll.com/checkouts/cn/{TOKEN}/en-us", body=checkout_body(address1=None))
    assert verdict is Verdict.CHECKOUT_PREFILL_MISSING and missing == ("address1",)


def test_a_checkout_path_with_a_non_200_status_is_not_eligible():
    verdict, _ = _classify(f"https://judydoll.com/checkouts/cn/{TOKEN}/en-us", status=404)
    assert verdict is Verdict.UNCLASSIFIED


def test_checkout_path_variants_c_co_are_accepted_and_others_are_not():
    for kind in ("cn", "c", "co"):
        assert _classify(f"https://s.com/checkouts/{kind}/{TOKEN}")[0] is Verdict.ELIGIBLE
    assert _classify(f"https://s.com/checkouts/xx/{TOKEN}")[0] is Verdict.UNCLASSIFIED
    assert _classify(f"https://shop.app/checkout/1/cn/{TOKEN}")[0] is Verdict.UNCLASSIFIED


async def test_values_the_page_html_escapes_are_found_after_unescaping():
    """The rendered input escapes `&` and `'`; the check reads the HTML-unescaped page."""
    tricky = CartPrefill(email=EMAIL, first_name="Pivota", last_name="O'Probe",
                         address1="12 O'Range & Lemon St", city="Wilmington", province="DE",
                         zip="19801", country="US")
    body = checkout_body(address1=tricky.address1)
    assert "O&#x27;Range &amp; Lemon" in body
    store = Store({("judydoll.com", "/products.json"): products_json(catalog(cvar())),
                   **cart_to_checkout(body=body)})
    result = await run(store, variant_id=VID, buyer=tricky)
    assert result.verdict is Verdict.ELIGIBLE


async def test_prefill_missing_end_to_end():
    store = Store({("judydoll.com", "/products.json"): products_json(catalog(cvar())),
                   **cart_to_checkout(body=checkout_body(email=None))})
    result = await run(store, variant_id=VID, buyer=BUYER)
    assert result.verdict is Verdict.CHECKOUT_PREFILL_MISSING
    assert result.missing == ("email",)


# --- LOGIN_REQUIRED ------------------------------------------------------------------------


def _login_routes(host, vid, *, via_host=None):
    shop = via_host or host
    ret = f"%2Fcheckouts%2Fcn%2F{TOKEN}%2Fen-us"
    routes = {
        (shop, f"/cart/{vid}:1"): redirect(
            302, f"https://{shop}/customer_authentication/login?locale=en-US&region_country=US&return_to={ret}"),
        (shop, "/customer_authentication/login"): redirect(
            302, "https://shopify.com/authentication/73087058091/oauth/authorize?buyer_flags=eyJ.x.y"),
        ("shopify.com", "/authentication/73087058091/oauth/authorize"): page(406, "Not Acceptable"),
    }
    if via_host:
        routes[(host, f"/cart/{vid}:1")] = redirect(301, f"https://{via_host}/cart/{vid}:1")
    return routes


async def test_forbeaut_login_wall_is_login_required_whatever_the_final_status():
    vid = "46060205703339"
    store = Store({("forbeaut.us", "/products.json"): products_json(catalog(cvar(vid))),
                   **_login_routes("forbeaut.us", vid)})
    result = await run(store, host="forbeaut.us", variant_id=vid, buyer=BUYER)
    assert result.verdict is Verdict.LOGIN_REQUIRED
    assert result.final_status == 406 and result.final_host == "shopify.com"
    assert result.retryable is False


async def test_podl_cross_host_login_wall_is_login_required():
    vid = "43311538634826"
    store = Store({("podl.us", "/products.json"): redirect(301, "https://www.podl.global/products.json?limit=250&page=1"),
                   ("www.podl.global", "/products.json"): products_json(catalog(cvar(vid))),
                   **_login_routes("podl.us", vid, via_host="www.podl.global")})
    result = await run(store, host="podl.us", variant_id=vid, buyer=BUYER)
    assert result.verdict is Verdict.LOGIN_REQUIRED


def test_either_login_marker_alone_is_enough_even_ahead_of_a_good_checkout():
    good = f"https://s.com/checkouts/cn/{TOKEN}"
    via_customer_auth = [(302, "https://s.com/customer_authentication/login?return_to=x"), (200, good)]
    assert _classify(good, chain=via_customer_auth)[0] is Verdict.LOGIN_REQUIRED
    via_shopify_auth = [(302, "https://shopify.com/authentication/1/oauth/authorize"), (200, good)]
    assert _classify(good, chain=via_shopify_auth)[0] is Verdict.LOGIN_REQUIRED
    via_account_sub = [(302, "https://account.shopify.com/authentication/1/login"), (200, good)]
    assert _classify(good, chain=via_account_sub)[0] is Verdict.LOGIN_REQUIRED


def test_authentication_on_a_non_shopify_host_or_another_shopify_path_is_not_a_login_wall():
    good = f"https://s.com/checkouts/cn/{TOKEN}"
    assert _classify(good, chain=[(302, "https://s.com/authentication/1"), (200, good)])[0] is Verdict.ELIGIBLE
    assert _classify(good, chain=[(302, "https://notshopify.com/authentication/1"), (200, good)])[0] is Verdict.ELIGIBLE
    assert _classify(good, chain=[(302, "https://shopify.com/checkouts/1"), (200, good)])[0] is Verdict.ELIGIBLE


# --- NOT_ACCEPTING_ORDERS / BLOCKED_UNKNOWN -------------------------------------------------


async def test_luafee_not_accepting_orders():
    vid = "52912527868213"
    cpath = f"/checkouts/cn/{TOKEN}/en-jp"
    store = Store({
        ("luafee.jp", "/products.json"): products_json(catalog(cvar(vid))),
        ("luafee.jp", f"/cart/{vid}:1"): redirect(301, f"https://luafeejp.com/cart/{vid}:1?checkout%5Bemail%5D=ucp-probe%40pivota.cc"),
        ("luafeejp.com", f"/cart/{vid}:1"): redirect(302, f"https://luafeejp.com{cpath}?_r=AQAB"),
        ("luafeejp.com", cpath): page(403, NOT_ACCEPTING_BODY),
    })
    jp = CartPrefill(email=EMAIL, first_name="Pivota", last_name="Probe", address1="1-1 Marunouchi",
                     city="Chiyoda-ku", province="JP-13", zip="100-0005", country="JP", phone="+81312345678")
    result = await run(store, host="luafee.jp", variant_id=vid, buyer=jp)
    assert result.verdict is Verdict.NOT_ACCEPTING_ORDERS
    assert result.final_status == 403 and result.final_host == "luafeejp.com"


def test_a_403_without_the_text_is_blocked_unknown_not_not_accepting():
    assert _classify(f"https://s.com/checkouts/cn/{TOKEN}", status=403, body=PLAIN_403_BODY)[0] is Verdict.BLOCKED_UNKNOWN


def test_the_not_accepting_text_off_a_checkout_path_is_blocked_unknown():
    assert _classify("https://s.com/cart/1:1", status=403, body=NOT_ACCEPTING_BODY)[0] is Verdict.BLOCKED_UNKNOWN


def test_the_not_accepting_text_with_a_200_is_not_not_accepting():
    verdict, _ = _classify(f"https://s.com/checkouts/cn/{TOKEN}", status=200, body=NOT_ACCEPTING_BODY)
    assert verdict is Verdict.UNCLASSIFIED


# --- VARIANT_GONE / VARIANT_UNAVAILABLE / substitution ---------------------------------------


async def test_a_410_on_the_cart_is_variant_gone():
    """robinsons with its 09-07 id: 301 -> www -> 410 "Link no longer exists". Here the catalog
    still lists the id (a stale listing), so it is the 410 that decides."""
    vid = "40975353675861"
    store = Store({
        ("robinsons.com.sg", "/products.json"): redirect(301, "https://www.robinsons.com.sg/products.json?limit=250&page=1"),
        ("www.robinsons.com.sg", "/products.json"): products_json(catalog(cvar(vid))),
        ("robinsons.com.sg", f"/cart/{vid}:1"): redirect(301, f"https://www.robinsons.com.sg/cart/{vid}:1"),
        ("www.robinsons.com.sg", f"/cart/{vid}:1"): page(410, LINK_GONE_BODY),
    })
    result = await run(store, host="robinsons.com.sg", variant_id=vid)
    assert result.verdict is Verdict.VARIANT_GONE
    assert result.final_status == 410


def test_a_410_off_the_cart_or_a_404_on_the_cart_is_not_variant_gone():
    assert _classify("https://s.com/products/x", status=410, body=LINK_GONE_BODY)[0] is Verdict.UNCLASSIFIED
    assert _classify("https://s.com/cart/1:1", status=404, body=LINK_GONE_BODY)[0] is Verdict.UNCLASSIFIED


async def test_a_named_variant_absent_from_the_catalog_is_gone_and_never_substituted():
    other = "11111111111111"
    store = Store({("s.com", "/products.json"): products_json(catalog(cvar(other)))})
    result = await run(store, host="s.com", variant_id=VID, buyer=BUYER)
    assert result.verdict is Verdict.VARIANT_GONE
    assert result.detail == "variant_not_in_catalog"
    assert result.variant_id == VID
    assert store.hit("/cart/") == [], "no checkout may be created for a substitute"


async def test_a_named_variant_that_is_unavailable_is_reported_and_not_substituted():
    store = Store({("s.com", "/products.json"): products_json(
        catalog(cvar(VID, available=False), cvar("22222222222222")))})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.VARIANT_UNAVAILABLE
    assert store.hit("/cart/") == []


async def test_a_named_variant_is_found_on_a_later_catalog_page():
    full_page = {"products": [{"title": f"P{i}", "variants": [cvar(str(10_000 + i))]} for i in range(250)]}
    store = Store({("s.com", "/products.json"): products_json(full_page, catalog(cvar())),
                   **cart_to_checkout(host="s.com")})
    result = await run(store, host="s.com", variant_id=VID, buyer=BUYER)
    assert result.verdict is Verdict.ELIGIBLE
    assert len(store.hit("/products.json")) == 2


async def test_a_catalog_scan_that_hits_its_cap_is_unverified_not_gone(monkeypatch):
    monkeypatch.setattr(pf, "MAX_CATALOG_PAGES", 2)
    full_page = {"products": [{"title": f"P{i}", "variants": [cvar(str(10_000 + i))]} for i in range(250)]}
    store = Store({("s.com", "/products.json"): products_json(full_page, full_page, catalog(cvar()))})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.VARIANT_UNVERIFIED
    assert result.detail == "catalog_scan_cap"
    assert store.hit("/cart/") == []


async def test_a_catalog_that_will_not_answer_is_unverified():
    store = Store({("s.com", "/products.json"): page(430, "Too many requests")})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.VARIANT_UNVERIFIED
    assert store.hit("/cart/") == []


async def test_a_handle_confines_the_pick_to_that_product():
    """No variant named + a handle: the first AVAILABLE variant of that product, and nothing else."""
    payload = {"title": "Single Eyeshadow", "variants": [
        {"id": 1, "title": "Sold out shade", "available": False, "price": 999},
        {"id": int(VID), "title": "#D02 Static Pulse", "available": True, "price": 999},
    ]}
    store = Store({("judydoll.com", "/products/single-eyeshadow.js"): lambda r: httpx.Response(200, json=payload),
                   **cart_to_checkout()})
    result = await run(store, product_handle="single-eyeshadow", buyer=BUYER)
    assert result.verdict is Verdict.ELIGIBLE
    assert result.variant_id == VID and result.variant_source == "product"
    assert result.price == "9.99"
    assert store.hit("/products.json") == []


async def test_a_handle_with_nothing_available_is_unavailable_not_substituted_from_the_catalog():
    payload = {"title": "X", "variants": [{"id": 1, "title": "A", "available": False, "price": 999}]}
    store = Store({("s.com", "/products/x.js"): lambda r: httpx.Response(200, json=payload)})
    result = await run(store, host="s.com", product_handle="x")
    assert result.verdict is Verdict.VARIANT_UNAVAILABLE
    assert store.hit("/products.json") == [] and store.hit("/cart/") == []


async def test_a_named_variant_not_on_the_named_product_is_gone():
    payload = {"title": "X", "variants": [{"id": 1, "title": "A", "available": True, "price": 999}]}
    store = Store({("s.com", "/products/x.js"): lambda r: httpx.Response(200, json=payload)})
    result = await run(store, host="s.com", product_handle="x", variant_id=VID)
    assert result.verdict is Verdict.VARIANT_GONE and result.detail == "variant_not_on_product"
    assert store.hit("/cart/") == []


async def test_a_named_variant_unavailable_on_the_named_product_is_unavailable():
    payload = {"title": "X", "variants": [
        {"id": int(VID), "title": "A", "available": False, "price": 999},
        {"id": 2, "title": "B", "available": True, "price": 999},
    ]}
    store = Store({("s.com", "/products/x.js"): lambda r: httpx.Response(200, json=payload)})
    result = await run(store, host="s.com", product_handle="x", variant_id=VID)
    assert result.verdict is Verdict.VARIANT_UNAVAILABLE
    assert store.hit("/cart/") == []


async def test_a_catalog_behind_a_login_wall_is_login_required():
    store = Store({
        ("s.com", "/products.json"): redirect(302, "https://s.com/customer_authentication/login?return_to=%2F"),
        ("s.com", "/customer_authentication/login"): redirect(302, "https://shopify.com/authentication/1/oauth/authorize"),
        ("shopify.com", "/authentication/1/oauth/authorize"): page(406, "Not Acceptable"),
    })
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.LOGIN_REQUIRED
    assert store.hit("/cart/") == []


async def test_a_missing_handle_without_a_named_variant_is_gone_not_a_catalog_pick():
    store = Store({("s.com", "/products/x.js"): page(404, "Not found")})
    result = await run(store, host="s.com", product_handle="x")
    assert result.verdict is Verdict.VARIANT_GONE and result.detail == "product_handle_not_found"
    assert store.hit("/products.json") == []


async def test_a_missing_handle_with_a_named_variant_falls_back_to_the_catalog():
    store = Store({("judydoll.com", "/products/renamed.js"): page(404, "Not found"),
                   ("judydoll.com", "/products.json"): products_json(catalog(cvar())),
                   **cart_to_checkout()})
    result = await run(store, product_handle="renamed", variant_id=VID)
    assert result.verdict is Verdict.ELIGIBLE and result.variant_source == "caller"


async def test_with_nothing_named_the_catalog_pick_skips_gift_cards_samples_and_cheap_or_sold_out_items():
    products = {"products": [
        {"title": "Gift Card", "variants": [cvar("1", price="50.00", requires_shipping=False)]},
        {"title": "Free Sample", "variants": [cvar("2", price="0.00")]},
        {"title": "Cheap", "variants": [cvar("3", price="1.00")]},
        {"title": "Sold Out", "variants": [cvar("4", available=False)]},
        {"title": "No Ship", "variants": [cvar("5", requires_shipping=False)]},
        {"title": "Real Product", "variants": [cvar(VID, price="23.00")]},
    ]}
    store = Store({("s.com", "/products.json"): products_json(products), **cart_to_checkout(host="s.com")})
    result = await run(store, host="s.com", buyer=BUYER)
    assert result.verdict is Verdict.ELIGIBLE
    assert result.variant_id == VID and result.variant_source == "catalog"
    assert result.product_title == "Real Product"


async def test_with_nothing_named_and_nothing_representative_it_is_unavailable():
    store = Store({("s.com", "/products.json"): products_json(
        {"products": [{"title": "Gift Card", "variants": [cvar("1")]}]})})
    result = await run(store, host="s.com")
    assert result.verdict is Verdict.VARIANT_UNAVAILABLE
    assert store.hit("/cart/") == []


# --- availability is read in the BUYER's market ------------------------------------------------
#
# Shopify scopes `available` to the market it resolves for the REQUEST. Measured on podl.us from a
# JP IP (2026-09-18): any Accept-Language -> 0 available; no language header, or `&country=US`,
# -> 2; `&country=JP` -> 0. These mocks reproduce that: `country=` decides when present, and only
# without it does the request's own language header decide.

PODL_VID = "43311735799882"


def _market_available(request):
    country = request.url.params.get("country")
    if country is not None:
        return country == "US"
    return "accept-language" not in request.headers


def podl_catalog(request):
    available = _market_available(request)
    return httpx.Response(200, json={"products": [{
        "title": "Chestnut Balm-to-Foam Cleanser", "handle": "chestnut-balm-to-foam-cleanser",
        "variants": [cvar(PODL_VID, available=available, price="20.00", title="1-Pack")],
    }]} if request.url.params.get("page", "1") == "1" else {"products": []})


def podl_product_js(request):
    return httpx.Response(200, json={"title": "Chestnut Balm-to-Foam Cleanser", "variants": [
        {"id": int(PODL_VID), "title": "1-Pack", "available": _market_available(request), "price": 2000},
    ]})


def _podl_store(**extra):
    return Store({
        ("podl.us", "/products.json"): podl_catalog,
        ("podl.us", "/products/chestnut-balm-to-foam-cleanser.js"): podl_product_js,
        **_login_routes("podl.us", PODL_VID, via_host="www.podl.global"),
        **extra,
    })


@pytest.mark.parametrize("variant_id", [PODL_VID, None])
async def test_catalog_availability_is_read_for_the_us_market(variant_id):
    store = _podl_store()
    result = await run(store, host="podl.us", market="US", variant_id=variant_id)
    assert result.verdict is Verdict.LOGIN_REQUIRED, "available in US -> the permalink is followed"
    assert result.variant_id == PODL_VID and result.market == "US"
    assert [r.url.params.get("country") for r in store.hit("/products.json")] == ["US"]


@pytest.mark.parametrize("variant_id", [PODL_VID, None])
async def test_the_same_catalog_read_for_jp_is_unavailable(variant_id):
    store = _podl_store()
    result = await run(store, host="podl.us", market="JP", variant_id=variant_id)
    assert result.verdict is Verdict.VARIANT_UNAVAILABLE
    assert result.market == "JP"
    assert store.hit("/cart/") == [], "no checkout for a variant the buyer's market cannot buy"
    assert [r.url.params.get("country") for r in store.hit("/products.json")] == ["JP"]


@pytest.mark.parametrize("variant_id", [PODL_VID, None])
async def test_handle_availability_is_read_for_the_us_market(variant_id):
    store = _podl_store()
    result = await run(store, host="podl.us", market="US", variant_id=variant_id,
                       product_handle="chestnut-balm-to-foam-cleanser")
    assert result.verdict is Verdict.LOGIN_REQUIRED
    assert [r.url.params.get("country") for r in store.hit("/products/")] == ["US"]
    assert store.hit("/products.json") == []


@pytest.mark.parametrize("variant_id", [PODL_VID, None])
async def test_the_same_handle_read_for_jp_is_unavailable(variant_id):
    store = _podl_store()
    result = await run(store, host="podl.us", market="JP", variant_id=variant_id,
                       product_handle="chestnut-balm-to-foam-cleanser")
    assert result.verdict is Verdict.VARIANT_UNAVAILABLE
    assert store.hit("/cart/") == []


@pytest.mark.parametrize("accept_language", ["en-US,en;q=0.9", "ja-JP", "ko-KR", None])
@pytest.mark.parametrize("path", ["catalog", "handle"])
async def test_accept_language_no_longer_decides_availability(monkeypatch, accept_language, path):
    headers = {"User-Agent": pf.USER_AGENT}
    if accept_language is not None:
        headers["Accept-Language"] = accept_language
    monkeypatch.setattr(pf, "_HEADERS", headers)
    store = _podl_store()
    kwargs = dict(product_handle="chestnut-balm-to-foam-cleanser") if path == "handle" else {}
    result = await run(store, host="podl.us", market="US", variant_id=PODL_VID, **kwargs)
    assert result.verdict is Verdict.LOGIN_REQUIRED
    # control: the header really varied on the wire
    seen = {r.headers.get("accept-language") for r in store.requests}
    assert seen == {accept_language}


async def test_country_is_sent_on_every_catalog_page():
    full_page = {"products": [{"title": f"P{i}", "variants": [cvar(str(10_000 + i))]} for i in range(250)]}
    store = Store({("s.com", "/products.json"): products_json(full_page, full_page, catalog(cvar())),
                   **cart_to_checkout(host="s.com", body=checkout_body(country="SG", total=("40.0", "SGD")))})
    result = await run(store, host="s.com", market="SG", variant_id=VID)
    assert result.verdict is Verdict.ELIGIBLE
    pages = store.hit("/products.json")
    assert [r.url.params.get("page") for r in pages] == ["1", "2", "3"]
    assert [r.url.params.get("country") for r in pages] == ["SG", "SG", "SG"]


async def test_country_survives_a_redirect_that_drops_or_rewrites_the_query():
    """robinsons 301s products.json to www; a redirect may lose the query or carry another
    market. Every hop is re-pinned to the buyer's market, exactly once."""
    store = Store({
        ("s.com", "/products.json"): redirect(301, "https://www.s.com/products.json?country=JP&limit=250&page=1"),
        ("www.s.com", "/products.json"): products_json(catalog(cvar())),
        ("s.com", "/products/x.js"): redirect(301, "https://www.s.com/products/x.js"),
        ("www.s.com", "/products/x.js"): lambda r: httpx.Response(200, json={
            "title": "X", "variants": [{"id": int(VID), "title": "A", "available": True, "price": 999}]}),
        **cart_to_checkout(host="s.com"),
    })
    await run(store, host="s.com", market="US", variant_id=VID)
    await run(store, host="s.com", market="US", variant_id=VID, product_handle="x")
    reads = store.hit("/products")
    assert len(reads) == 4
    for request in reads:
        assert request.url.params.get_list("country") == ["US"], str(request.url)


def test_with_query_param_merges_and_replaces():
    assert pf._with_query_param("https://s.com/p.json", "country", "US") == "https://s.com/p.json?country=US"
    assert pf._with_query_param("https://s.com/p.json?limit=250&page=2", "country", "US") == (
        "https://s.com/p.json?limit=250&page=2&country=US")
    assert pf._with_query_param("https://s.com/p.json?country=JP&page=2", "country", "US") == (
        "https://s.com/p.json?page=2&country=US")


# --- PASSWORD_PAGE -------------------------------------------------------------------------


async def test_a_cart_that_lands_on_the_password_page():
    store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                   ("s.com", f"/cart/{VID}:1"): redirect(302, "https://s.com/password"),
                   ("s.com", "/password"): page(200, "<html>Opening soon</html>")})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.PASSWORD_PAGE


async def test_a_catalog_behind_the_password_page_is_password_page():
    store = Store({("s.com", "/products.json"): redirect(302, "https://s.com/password"),
                   ("s.com", "/password"): page(200, "<html>Opening soon</html>")})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.PASSWORD_PAGE
    assert store.hit("/cart/") == []


def test_a_passwordless_path_that_merely_contains_password_is_not_password_page():
    assert _classify("https://s.com/pages/password-help", status=200, body="x")[0] is Verdict.UNCLASSIFIED


# --- TRANSPORT_ERROR -----------------------------------------------------------------------


def _raise(exc_type):
    def respond(request):
        raise exc_type("simulated", request=request)
    return respond


async def test_a_connect_error_on_the_permalink_is_retryable_and_not_ineligible():
    store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                   ("s.com", f"/cart/{VID}:1"): _raise(httpx.ConnectError)})
    result = await run(store, host="s.com", variant_id=VID, buyer=BUYER)
    assert result.verdict is Verdict.TRANSPORT_ERROR
    assert result.retryable is True
    assert result.detail == "permalink:ConnectError"


async def test_a_timeout_while_resolving_the_variant_is_retryable():
    store = Store({("s.com", "/products.json"): _raise(httpx.ReadTimeout)})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.TRANSPORT_ERROR and result.retryable is True
    assert result.detail == "resolve:ReadTimeout"


async def test_a_proxy_error_mid_chain_is_retryable():
    store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                   ("s.com", f"/cart/{VID}:1"): redirect(301, f"https://www.s.com/cart/{VID}:1"),
                   ("www.s.com", f"/cart/{VID}:1"): _raise(httpx.ProxyError)})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.TRANSPORT_ERROR and result.retryable is True
    assert [s for s, _ in result.chain] == [301]


@pytest.mark.parametrize("verdict", [v for v in Verdict if v is not Verdict.TRANSPORT_ERROR])
async def test_only_transport_errors_are_retryable(verdict):
    assert PreflightResult(host="x.com", verdict=verdict).retryable is False


# --- redirect handling ----------------------------------------------------------------------


async def test_redirects_stop_after_ten_requests():
    routes = {("s.com", "/products.json"): products_json(catalog(cvar()))}
    routes[("s.com", f"/cart/{VID}:1")] = redirect(302, "https://s.com/loop/1")
    for i in range(1, 30):
        routes[("s.com", f"/loop/{i}")] = redirect(302, f"https://s.com/loop/{i + 1}")
    store = Store(routes)
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.UNCLASSIFIED and result.detail == "too_many_redirects"
    assert len(result.chain) == 10
    assert len([r for r in store.requests if r.url.path != "/products.json"]) == 10


async def test_a_redirect_to_a_private_address_or_plain_http_is_not_followed():
    for target in ("http://s.com/checkouts/cn/x", "https://169.254.169.254/latest/meta-data",
                   "https://localhost/x", "https://[::1]/x"):
        store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                       ("s.com", f"/cart/{VID}:1"): redirect(302, target)})
        result = await run(store, host="s.com", variant_id=VID)
        assert result.verdict is Verdict.UNCLASSIFIED, target
        assert result.detail.startswith("permalink:host_") or result.detail == "permalink:hop_not_https", target
        assert len(store.requests) == 2, target  # products.json + the cart; the target is never fetched


async def test_a_relative_location_is_resolved_against_the_current_hop():
    cpath = f"/checkouts/cn/{TOKEN}/en-us"
    store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                   ("s.com", f"/cart/{VID}:1"): redirect(302, cpath),
                   ("s.com", cpath): page(200, checkout_body())})
    result = await run(store, host="s.com", variant_id=VID, buyer=BUYER)
    assert result.verdict is Verdict.ELIGIBLE


# --- INVALID_INPUT: refused before any request ----------------------------------------------


@pytest.mark.parametrize("kwargs,detail", [
    (dict(host="10.0.0.1"), "host_is_ip_literal"),
    (dict(host="localhost"), "host_local"),
    (dict(host="shop.internal"), "host_local"),
    (dict(variant_id="SKU-1"), "variant_id_not_numeric"),
    (dict(product_handle="a/b"), "product_handle_malformed"),
    (dict(quantity=0), "quantity_out_of_range"),
    (dict(quantity=True), "quantity_out_of_range"),
    (dict(click_id=""), "click_id_invalid"),
    (dict(click_id="clk\r\nx"), "click_id_invalid"),
    (dict(buyer=CartPrefill(email="a@b", first_name="P", last_name="Q", address1="1", city="c", country="US")),
     "email_implausible"),
    (dict(market="US", buyer=CartPrefill(email=EMAIL, first_name="P", last_name="Q", address1="1", city="c",
                                         country="USA")),
     "country_not_iso2"),
    (dict(market="USA"), "market_not_iso2"),
    (dict(market="U1"), "market_not_iso2"),
    (dict(market=""), "market_not_iso2"),
    (dict(market=None), "market_not_iso2"),
    (dict(market="US", buyer=CartPrefill(email=EMAIL, first_name="P", last_name="Q", address1="1", city="c",
                                         country="JP")),
     "buyer_country_not_market"),
    (dict(market="jp", buyer=CartPrefill(email=EMAIL, first_name="P", last_name="Q", address1="1", city="c",
                                         country="US")),
     "buyer_country_not_market"),
])
async def test_bad_input_is_refused_before_any_request(kwargs, detail):
    store = Store({})
    host = kwargs.pop("host", "s.com")
    result = await run(store, host=host, **kwargs)
    assert result.verdict is Verdict.INVALID_INPUT and result.detail == detail
    assert result.market is None
    assert store.requests == []


def test_market_is_a_required_keyword():
    with pytest.raises(TypeError):
        asyncio.run(preflight("s.com", click_id=CLICK))  # type: ignore[call-arg]


async def test_a_lowercase_market_matching_the_buyer_is_accepted():
    store = Store({("judydoll.com", "/products.json"): products_json(catalog(cvar())), **cart_to_checkout()})
    result = await run(store, variant_id=VID, buyer=BUYER, market="us")
    assert result.verdict is Verdict.ELIGIBLE and result.market == "US"
    assert all(r.url.params.get("country") == "US" for r in store.hit("/products.json"))


async def test_without_an_injected_client_the_suite_guard_blocks_the_real_network():
    """Control for the autouse guard: a preflight that builds its own client must be stopped."""
    with pytest.raises(AssertionError, match="un-mocked network request"):
        await preflight("judydoll.com", market="US", variant_id=VID, click_id=CLICK)


# --- PII: nothing emitted during a run carries the buyer's email or address ------------------


def _pii_forms(value):
    return {value, quote(value, safe=""), quote_plus(value), quote(value, safe="@")}


class _HttpcoreLoggingStore(Store):
    """MockTransport never reaches httpcore, but the real transport does, and httpcore's DEBUG
    trace prints each response's headers — `Location` included. A live DEBUG run on 2026-09-18
    caught exactly that line carrying `checkout%5Bemail%5D=ucp-probe%40pivota.cc`. Emit the same
    line, in httpcore's own format and on its own loggers, for every response."""

    def handler(self, request):
        response = super().handler(request)
        headers = [(k.encode(), v.encode()) for k, v in response.headers.items()]
        for name in ("httpcore.http11", "httpcore.http2"):
            logging.getLogger(name).debug(
                f"receive_response_headers.complete return_value="
                f"{(b'HTTP/1.1', response.status_code, b'Status', headers)!r}"
            )
        return response


def _pii_chain_store():
    """Shopify re-emits the buyer values on the next hop with %5B-encoded keys; model that."""
    cpath = f"/checkouts/cn/{TOKEN}/en-us"
    return _HttpcoreLoggingStore({
        ("judydoll.com", "/products.json"): products_json(catalog(cvar())),
        ("judydoll.com", f"/cart/{VID}:1"): redirect(
            301, f"https://www.judydoll.com/cart/{VID}:1?attributes%5Bpivota_click_id%5D={CLICK}"
                 f"&checkout%5Bemail%5D=ucp-probe%40pivota.cc"
                 f"&checkout%5Bshipping_address%5D%5Baddress1%5D=1209+Orange+Street"),
        ("www.judydoll.com", f"/cart/{VID}:1"): redirect(302, f"https://www.judydoll.com{cpath}?_r=AQAB"),
        ("www.judydoll.com", cpath): page(200, checkout_body()),
    })


async def test_no_log_record_during_a_preflight_contains_the_email_or_address(caplog):
    caplog.set_level(logging.DEBUG)
    store = _pii_chain_store()
    result = await run(store, variant_id=VID, buyer=BUYER)
    assert result.verdict is Verdict.ELIGIBLE
    messages = [rec.getMessage() for rec in caplog.records]
    # Control: the mechanism under test really ran — httpx logged each request and this module
    # logged its start/hops/verdict. Without these, "no PII found" would pass vacuously.
    httpx_lines = [m for m in messages if m.startswith("HTTP Request:")]
    assert len(httpx_lines) == len(store.requests) >= 4
    assert any("REDACTED" in m for m in httpx_lines)
    for name in ("httpcore.http11", "httpcore.http2"):
        core = [r.getMessage() for r in caplog.records if r.name == name]
        assert len(core) == len(store.requests)
        assert any("checkout%5Bemail%5D=REDACTED" in m for m in core), name
    assert any("cart-link preflight start" in m for m in messages)
    assert any("cart-link preflight hop" in m for m in messages)
    assert any("verdict=ELIGIBLE" in m for m in messages)
    for value in (EMAIL, ADDR1):
        for form in _pii_forms(value):
            leaked = [m for m in messages if form in m]
            assert not leaked, (form, leaked)


async def test_the_result_and_its_chain_carry_no_buyer_values():
    result = await run(_pii_chain_store(), variant_id=VID, buyer=BUYER)
    dumped = json.dumps(result.to_dict())
    for value in (EMAIL, ADDR1, "Wilmington", "19801", "+12025550142"):
        for form in _pii_forms(value):
            assert form not in dumped, form
    assert CLICK in dumped  # the click id is kept


async def test_the_httpx_log_filter_is_scoped_to_a_running_preflight(caplog):
    """Outside a preflight, other callers' httpx lines are left exactly as httpx wrote them."""
    caplog.set_level(logging.INFO)
    store = Store({("s.com", "/x"): page(200, "ok")})
    async with store.client() as client:
        await client.get("https://s.com/x?checkout[email]=someone%40example.com")
    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert lines and "someone%40example.com" in lines[0]


# --- the operator script ---------------------------------------------------------------------


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "tierb_cart_link_preflight.py"
    spec = importlib.util.spec_from_file_location("tierb_cart_link_preflight_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_reads_population_rows_and_its_probe_buyers_are_valid():
    script = _load_script()
    row = script.normalize_row({"domain": "judydoll.com", "market": "us", "variant": "50041364447509"})
    assert row == {"domain": "judydoll.com", "market": "US", "variant_id": "50041364447509", "product_handle": None}
    assert script.normalize_row({"domain": "b.com", "market": "US", "variant": None})["variant_id"] is None
    for market, buyer in script.PROBE_BUYERS.items():
        assert cart_prefill_refusal(buyer) is None, market
        assert buyer.country == market


def _fake(verdicts, calls, inflight=None):
    seq = list(verdicts)

    async def fake_preflight(host, **kwargs):
        calls.append((host, kwargs))
        if inflight is not None:
            inflight["now"] += 1
            inflight["max"] = max(inflight["max"], inflight["now"])
            await asyncio.sleep(0.01)
            inflight["now"] -= 1
        verdict = seq.pop(0) if seq else Verdict.ELIGIBLE
        return PreflightResult(host=host, verdict=verdict, retryable=verdict is Verdict.TRANSPORT_ERROR)

    return fake_preflight


@pytest.mark.parametrize("sequence,attempts,final", [
    ([Verdict.TRANSPORT_ERROR, Verdict.ELIGIBLE], 2, "ELIGIBLE"),
    ([Verdict.TRANSPORT_ERROR, Verdict.TRANSPORT_ERROR, Verdict.ELIGIBLE], 2, "TRANSPORT_ERROR"),
    ([Verdict.LOGIN_REQUIRED], 1, "LOGIN_REQUIRED"),
    ([Verdict.UNCLASSIFIED], 1, "UNCLASSIFIED"),
])
async def test_script_retries_a_transport_error_exactly_once(sequence, attempts, final):
    script = _load_script()
    calls = []
    rows = [script.normalize_row({"domain": "a.com", "market": "US", "variant": "1"})]
    out = await script.run_rows(rows, preflight_fn=_fake(sequence, calls), retry_delay_s=0)
    assert out[0]["attempts"] == attempts and len(calls) == attempts
    assert out[0]["result"]["verdict"] == final


async def test_script_defaults_to_no_buyer_the_reap_path():
    script = _load_script()
    calls = []
    rows = [script.normalize_row({"domain": "a.com", "market": "US"}),
            script.normalize_row({"domain": "b.com", "market": "FR"})]
    await script.run_rows(rows, preflight_fn=_fake([], calls), retry_delay_s=0)
    assert [c[1]["buyer"] for c in calls] == [None, None]


async def test_script_probe_buyer_is_opt_in_and_never_foreign():
    script = _load_script()
    calls = []
    rows = [script.normalize_row({"domain": "a.com", "market": "US"}),
            script.normalize_row({"domain": "b.com", "market": "FR"})]
    out = await script.run_rows(rows, use_buyer=True, preflight_fn=_fake([], calls), retry_delay_s=0)
    assert calls[0][1]["buyer"] is script.PROBE_BUYERS["US"]
    assert len(calls) == 1, "a market with no probe buyer is refused, not sent a foreign address"
    assert out[1]["result"]["verdict"] == "INVALID_INPUT"


@pytest.mark.parametrize("argv_extra,use_buyer", [([], False), (["--with-probe-buyer"], True)])
def test_script_cli_buyer_is_opt_in(tmp_path, monkeypatch, argv_extra, use_buyer):
    script = _load_script()
    seen = {}

    async def fake_run_rows(rows, **kwargs):
        seen.update(kwargs, rows=rows)
        return []

    monkeypatch.setattr(script, "run_rows", fake_run_rows)
    src = tmp_path / "in.json"
    src.write_text(json.dumps([{"domain": "podl.us", "market": "US"}]))
    out = tmp_path / "out.json"
    assert script.main([str(src), "--out", str(out), *argv_extra]) == 0
    assert seen["use_buyer"] is use_buyer
    report = json.loads(out.read_text())
    assert report["buyer"] == ("synthetic_probe_per_market" if use_buyer else "none")
    assert report["shipping_verified"] is False


def test_script_cli_no_longer_accepts_no_buyer(tmp_path):
    script = _load_script()
    with pytest.raises(SystemExit):
        script.main([str(tmp_path / "in.json"), "--out", str(tmp_path / "o.json"), "--no-buyer"])


async def test_script_passes_each_rows_market():
    script = _load_script()
    calls = []
    rows = [script.normalize_row({"domain": "a.com", "market": "us"}),
            script.normalize_row({"domain": "b.jp", "market": "JP"}),
            script.normalize_row({"domain": "c.sg", "market": "SG"})]
    await script.run_rows(rows, preflight_fn=_fake([], calls), retry_delay_s=0)
    assert sorted((host, kw["market"]) for host, kw in calls) == [("a.com", "US"), ("b.jp", "JP"), ("c.sg", "SG")]


async def test_script_concurrency_never_exceeds_six():
    script = _load_script()
    calls, inflight = [], {"now": 0, "max": 0}
    rows = [script.normalize_row({"domain": f"s{i}.com", "market": "US"}) for i in range(20)]
    await script.run_rows(rows, concurrency=50, preflight_fn=_fake([], calls, inflight), retry_delay_s=0)
    assert len(calls) == 20
    assert inflight["max"] == 6



# =============================================================================================
# Review of #2209 @71675390 — every finding has an accepting and a refusing test below.
# =============================================================================================

CHECKOUT_URL = f"https://s.com/checkouts/cn/{TOKEN}/en-us"


def _classify_state(state, *, market=None, buyer=None, click=CLICK, variant=VID):
    body = checkout_body(state=state, email=None, address1=None)
    return classify_landing(chain=[(200, CHECKOUT_URL)], final_status=200, body=body,
                            variant_id=variant, click_id=click, buyer=buyer, market=market)


# --- P1: the variant must be a merchandise LINE, the click id the cart ATTRIBUTE --------------


def test_review_repro_wrong_line_removed_line_and_prefix_click_is_not_eligible():
    """r1.py verbatim: line 9999, removedLines 5004, attribute clk_12; want 5004 / clk_1."""
    chain = [(302, "https://s.com/cart/5004:1?attributes[pivota_click_id]=REDACTED"),
             (200, "https://s.com/checkouts/cn/TOK/en-us")]
    body = ('{"lines":[{"merchandise":{"id":"gid://shopify/ProductVariant/9999"}}],'
            '"removedLines":[{"id":"gid://shopify/ProductVariant/5004"}],'
            '"attributes":[{"key":"pivota_click_id","value":"clk_12"}]}')
    verdict, missing = classify_landing(chain=chain, final_status=200, body=body, variant_id="5004",
                                        click_id="clk_1", buyer=None)
    assert verdict is Verdict.UNCLASSIFIED and missing == ("variant", "click_id")


def test_review_repro_click_only_in_an_echoed_url_is_not_eligible():
    body = ('<link rel="canonical" href="https://s.com/cart/5004:1?attributes[pivota_click_id]=clk_1">'
            ' ProductVariant/5004')
    verdict, missing = classify_landing(chain=[(200, "https://s.com/checkouts/cn/TOK/en-us")], final_status=200,
                                        body=body, variant_id="5004", click_id="clk_1", buyer=None)
    assert verdict is Verdict.UNCLASSIFIED and "click_id" in missing


def test_the_live_shaped_checkout_is_eligible():
    assert _classify_state(checkout_state()) == (Verdict.ELIGIBLE, ())


def test_key_order_and_whitespace_inside_the_line_do_not_matter():
    line = merchandise_line(VID)
    shuffled = {k: line[k] for k in reversed(list(line))}
    shuffled["merchandise"] = {k: line["merchandise"][k] for k in reversed(list(line["merchandise"]))}
    state = checkout_state(lines=[shuffled])
    body = checkout_body(state=state, email=None, address1=None).replace("&quot;:", "&quot; :  ")
    verdict, _ = classify_landing(chain=[(200, CHECKOUT_URL)], final_status=200, body=body,
                                  variant_id=VID, click_id=CLICK, buyer=None)
    assert verdict is Verdict.ELIGIBLE


def test_the_contextualized_merchandise_typename_is_accepted_and_other_lines_may_coexist():
    lines = [merchandise_line("11111111111111"),
             merchandise_line(VID, typename="ContextualizedProductVariantMerchandise")]
    assert _classify_state(checkout_state(lines=lines))[0] is Verdict.ELIGIBLE


def test_the_click_id_is_compared_exactly():
    assert _classify_state(checkout_state(click="clk_12"), click="clk_1")[1] == ("click_id",)
    assert _classify_state(checkout_state(click="clk_1"), click="clk_12")[1] == ("click_id",)
    assert _classify_state(checkout_state(click="xclk_1"), click="clk_1")[1] == ("click_id",)
    assert _classify_state(checkout_state(click="clk_1"), click="clk_1")[0] is Verdict.ELIGIBLE


def test_a_click_id_present_only_in_the_query_string_echo_does_not_count():
    state = checkout_state(attributes=[])
    body = checkout_body(state=state, email=None, address1=None)
    assert CLICK in html.unescape(body), "control: the echo really carries the click id"
    assert _classify_state(state)[1] == ("click_id",)


def test_a_click_attribute_under_another_key_does_not_count():
    state = checkout_state(attributes=[{"key": "utm_content", "value": CLICK}])
    assert _classify_state(state)[1] == ("click_id",)


def test_the_variant_only_in_removed_lines_does_not_count():
    state = checkout_state(lines=[], extra={"removedMerchandiseLines": [merchandise_line(VID)],
                                            "removedLines": [merchandise_line(VID)]})
    assert _classify_state(state)[1] == ("variant",)


def test_the_variant_only_in_a_recommendation_block_does_not_count():
    rec = {"productRecommendations": [{"merchandise": merchandise_line(VID)["merchandise"],
                                       "__typename": "ProductRecommendation"}]}
    state = checkout_state(lines=[merchandise_line("11111111111111")], extra=rec)
    assert _classify_state(state)[1] == ("variant",)


def test_a_line_that_is_not_a_merchandise_line_or_not_a_variant_does_not_count():
    not_a_line = dict(merchandise_line(VID), __typename="RecommendedLine")
    assert _classify_state(checkout_state(lines=[not_a_line]))[1] == ("variant",)
    gift = merchandise_line(VID, typename="GiftCardMerchandise")
    assert _classify_state(checkout_state(lines=[gift]))[1] == ("variant",)


def test_a_line_whose_variant_id_disagrees_does_not_count():
    assert _classify_state(checkout_state(lines=[merchandise_line(VID, variant_id="999")]))[1] == ("variant",)


def test_a_longer_variant_gid_starting_with_ours_does_not_count():
    assert _classify_state(checkout_state(lines=[merchandise_line(VID + "1")]))[1] == ("variant",)
    # the same with no `variantId` to fall back on: the `id` comparison alone must refuse it
    bare = merchandise_line(VID + "1")
    del bare["merchandise"]["variantId"]
    assert _classify_state(checkout_state(lines=[bare]))[1] == ("variant",)


def test_variant_id_is_optional_when_the_merchandise_id_matches():
    bare = merchandise_line(VID)
    del bare["merchandise"]["variantId"]
    assert _classify_state(checkout_state(lines=[bare]))[0] is Verdict.ELIGIBLE


def test_a_key_that_merely_ends_in_merchandise_lines_is_not_the_line_list():
    state = checkout_state(lines=[], extra={"deletedmerchandiseLines": [merchandise_line(VID)]})
    assert _classify_state(state)[1] == ("variant",)


async def test_an_input_host_that_is_not_valid_idna_is_unclassified_not_an_exception():
    result = await run(Store({}), host="xn--zz.com", variant_id=VID)
    assert result.verdict is Verdict.UNCLASSIFIED and result.detail == "resolve:hop_invalid_url"


# --- P2: the checkout market is pinned and read back ------------------------------------------


async def test_the_permalink_carries_country_equal_to_the_market():
    for market, country in (("US", "US"), ("SG", "SG")):
        store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                       **cart_to_checkout(host="s.com", body=checkout_body(country=country))})
        result = await run(store, host="s.com", market=market, variant_id=VID)
        assert result.verdict is Verdict.ELIGIBLE and result.checkout_country == country
        (cart,) = store.hit("/cart/")
        assert cart.url.params.get_list("country") == [market]
        assert f"country={market}" in result.chain[0][1], "country stays visible in the redacted chain"


async def test_a_checkout_in_another_market_is_a_market_mismatch():
    store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                   **cart_to_checkout(host="s.com", body=checkout_body(country="JP", total=("1545", "JPY")))})
    result = await run(store, host="s.com", market="US", variant_id=VID)
    assert result.verdict is Verdict.CHECKOUT_MARKET_MISMATCH
    assert result.checkout_country == "JP" and result.detail == "checkout_country_JP"
    assert result.retryable is False


async def test_a_checkout_whose_market_cannot_be_read_is_a_market_mismatch():
    """country=None leaves only the policy set's EMPTY `buyerIdentity: []`, which must not match."""
    body = checkout_body(country=None)
    assert "buyerIdentity&quot;:[]" in body
    store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                   **cart_to_checkout(host="s.com", body=body)})
    result = await run(store, host="s.com", market="US", variant_id=VID)
    assert result.verdict is Verdict.CHECKOUT_MARKET_MISMATCH
    assert result.checkout_country is None and result.detail == "checkout_country_unreadable"


def test_checkout_buyer_country_reads_nested_top_level_and_refuses_ambiguity():
    assert pf.checkout_buyer_country(checkout_body(country="SG")) == "SG"
    top = checkout_state(country=None, extra={"buyerIdentity": {"countryCode": "GB", "email": None}})
    assert pf.checkout_buyer_country(checkout_body(state=top)) == "GB"
    two = checkout_state(country="US", extra={"buyerProposal": {"buyerIdentity": {"countryCode": "JP"}}})
    assert pf.checkout_buyer_country(checkout_body(state=two)) is None
    same = checkout_state(country="US", extra={"buyerProposal": {"buyerIdentity": {"countryCode": "US"}}})
    assert pf.checkout_buyer_country(checkout_body(state=same)) == "US"
    assert pf.checkout_buyer_country(checkout_body(country=None)) is None
    assert pf.checkout_buyer_country('"buyerIdentity":{"countryCode":"usa"}') is None


def test_market_mismatch_outranks_a_missing_prefill():
    state = checkout_state(country="JP")
    body = checkout_body(state=state, email=None)
    verdict, _ = classify_landing(chain=[(200, CHECKOUT_URL)], final_status=200, body=body, variant_id=VID,
                                  click_id=CLICK, buyer=BUYER, market="US")
    assert verdict is Verdict.CHECKOUT_MARKET_MISMATCH


# --- P3: no network-type failure escapes; one merchant never sinks the report -----------------


class _StreamedBody(httpx.AsyncByteStream):
    """A body httpx only decodes when it is READ (as on the real network). `content=` bytes are
    decoded eagerly inside the mock, which would exercise a different except clause."""

    async def __aiter__(self):
        yield b"not gzip at all"


@pytest.mark.parametrize("eager", [True, False], ids=["decoded-at-send", "decoded-at-read"])
async def test_a_body_that_fails_to_decode_is_a_retryable_transport_error(eager):
    def bad_gzip(request):
        if eager:
            return httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"not gzip at all")
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_StreamedBody())

    store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                   ("s.com", f"/cart/{VID}:1"): redirect(302, f"https://s.com/checkouts/cn/{TOKEN}/en-us"),
                   ("s.com", f"/checkouts/cn/{TOKEN}/en-us"): bad_gzip})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.TRANSPORT_ERROR and result.retryable is True
    assert result.detail == "permalink:DecodingError"


@pytest.mark.parametrize("location", [
    "https://s.com:abc/checkouts/cn/T?checkout%5Bemail%5D=alice%40example.com",
    "https://xn--zz.com/checkouts/cn/T",
    "https://xn--a.com/x",
])
async def test_an_unparseable_location_is_unclassified_not_an_exception(location):
    store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                   ("s.com", f"/cart/{VID}:1"): redirect(302, location)})
    result = await run(store, host="s.com", variant_id=VID, buyer=BUYER)
    assert result.verdict is Verdict.UNCLASSIFIED and result.detail == "permalink:hop_invalid_url"
    assert "alice" not in json.dumps(result.to_dict())


async def test_any_other_protocol_error_stays_a_retryable_transport_error():
    store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                   ("s.com", f"/cart/{VID}:1"): _raise(httpx.RemoteProtocolError)})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.TRANSPORT_ERROR and result.retryable is True
    assert result.detail == "permalink:RemoteProtocolError"


async def test_an_unparseable_location_while_resolving_is_unclassified():
    store = Store({("s.com", "/products.json"): redirect(301, "https://xn--zz.com/products.json")})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.UNCLASSIFIED and result.detail == "resolve:hop_invalid_url"


async def test_script_one_raising_merchant_does_not_sink_the_report():
    script = _load_script()

    async def flaky(host, **kwargs):
        if host == "bad.com":
            raise RuntimeError("https://bad.com/cart/1:1?checkout[email]=alice@example.com")
        return PreflightResult(host=host, verdict=Verdict.ELIGIBLE)

    rows = [script.normalize_row({"domain": d, "market": "US"}) for d in ("a.com", "bad.com", "c.com")]
    out = await script.run_rows(rows, preflight_fn=flaky, retry_delay_s=0)
    assert [e["result"]["verdict"] for e in out] == ["ELIGIBLE", "UNCLASSIFIED", "ELIGIBLE"]
    assert out[1]["result"]["detail"] == "script_exception:RuntimeError"
    assert "alice" not in json.dumps(out)


# --- P4: relative and scheme-relative Locations in httpcore's trace are redacted --------------


@pytest.mark.parametrize("location", [
    "/checkouts/cn/T?checkout%5Bemail%5D=alice%40example.com&attributes%5Bpivota_click_id%5D=clk_keep&country=US",
    "//s.com/checkouts/cn/T?checkout%5Bemail%5D=alice%40example.com&attributes%5Bpivota_click_id%5D=clk_keep&country=US",
    "https://s.com/checkouts/cn/T?checkout%5Bemail%5D=alice%40example.com&attributes%5Bpivota_click_id%5D=clk_keep&country=US",
])
def test_httpcore_location_lines_are_redacted_in_every_url_form(caplog, location):
    caplog.set_level(logging.DEBUG)
    line = ("receive_response_headers.complete return_value=(b'HTTP/1.1', 302, b'Found', "
            f"[(b'Location', b'{location}')])")
    token = pf._PREFLIGHT_ACTIVE.set(True)
    try:
        logging.getLogger("httpcore.http11").debug(line)
    finally:
        pf._PREFLIGHT_ACTIVE.reset(token)
    (message,) = [r.getMessage() for r in caplog.records if r.name == "httpcore.http11"]
    assert "alice" not in message
    assert "checkout%5Bemail%5D=REDACTED" in message
    assert "attributes%5Bpivota_click_id%5D=clk_keep" in message and "country=US" in message


def test_text_without_a_query_is_left_alone():
    assert pf._redact_text("why? because") == "why? because"
    assert pf._redact_text("GET /cart/1:1 200") == "GET /cart/1:1 200"


# --- P5: the host guard ----------------------------------------------------------------------


@pytest.mark.parametrize("host,reason", [
    ("127.1", "host_is_numeric"), ("0x7f.1", "host_is_numeric"), ("10.1", "host_is_numeric"),
    ("0177.0.0.1", "host_is_numeric"), ("127.0.0.1.", "host_is_ip_literal"),
    ("localhost.", "host_local"), ("LOCALHOST", "host_local"), ("foo.localhost.", "host_local"),
    ("foo.localhost", "host_local"), ("[::ffff:127.0.0.1]", "host_is_ip_literal"),
])
def test_numeric_hex_and_localhost_hosts_are_refused(host, reason):
    assert pf._hop_refusal(f"https://{host}/x") == reason


@pytest.mark.parametrize("host", ["judydoll.com", "www.robinsons.com.sg", "face.cafe", "1password.com", "123.example.org"])
def test_real_dns_names_pass_the_host_guard(host):
    assert pf._hop_refusal(f"https://{host}/x") is None


async def test_a_redirect_to_a_numeric_host_is_never_fetched():
    store = Store({("s.com", "/products.json"): products_json(catalog(cvar())),
                   ("s.com", f"/cart/{VID}:1"): redirect(302, "https://127.1/latest/meta-data")})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.UNCLASSIFIED and result.detail == "permalink:host_is_numeric"
    assert len(store.requests) == 2


# --- P6: more than 100 variants ----------------------------------------------------------------


def _big_product(n, available=True):
    return {"title": "Big", "variants": [{"id": 1000 + i, "title": str(i), "available": available, "price": 1000}
                                         for i in range(n)]}


async def test_a_named_variant_past_the_parse_cap_is_unverified_not_gone():
    store = Store({("s.com", "/products/big.js"): lambda r: httpx.Response(200, json=_big_product(150))})
    result = await run(store, host="s.com", variant_id="1120", product_handle="big")
    assert result.verdict is Verdict.VARIANT_UNVERIFIED and result.detail == "product_js_variants_truncated"
    assert store.hit("/cart/") == []


async def test_a_named_variant_within_a_big_product_is_still_found():
    store = Store({("s.com", "/products/big.js"): lambda r: httpx.Response(200, json=_big_product(150)),
                   **cart_to_checkout(host="s.com", vid="1050", body=checkout_body(variant="1050"))})
    result = await run(store, host="s.com", variant_id="1050", product_handle="big")
    assert result.verdict is Verdict.ELIGIBLE


async def test_a_small_product_without_the_named_variant_is_still_gone():
    store = Store({("s.com", "/products/big.js"): lambda r: httpx.Response(200, json=_big_product(99))})
    result = await run(store, host="s.com", variant_id="5555", product_handle="big")
    assert result.verdict is Verdict.VARIANT_GONE and result.detail == "variant_not_on_product"


async def test_a_catalog_product_at_the_variant_cap_makes_absence_unverified():
    many = {"title": "Many", "variants": [cvar(str(2000 + i)) for i in range(100)]}
    store = Store({("s.com", "/products.json"): products_json({"products": [many]})})
    result = await run(store, host="s.com", variant_id=VID)
    assert result.verdict is Verdict.VARIANT_UNVERIFIED and result.detail == "catalog_variants_possibly_truncated"
    few = {"title": "Few", "variants": [cvar(str(2000 + i)) for i in range(99)]}
    store = Store({("s.com", "/products.json"): products_json({"products": [few]})})
    assert (await run(store, host="s.com", variant_id=VID)).verdict is Verdict.VARIANT_GONE


# --- P7: limit/page survive a redirect that drops the query -----------------------------------


async def test_limit_and_page_are_re_pinned_after_a_query_dropping_redirect():
    """r2.py (d): the www host serves 30 per page when `limit` is missing, so a lost query reads
    page 1 of 30 and "proves" product #50 absent."""
    products = [{"title": f"P{i}", "variants": [cvar(str(7000 + i))]} for i in range(100)]

    def www(request):
        n = int(request.url.params.get("page", "1"))
        size = int(request.url.params.get("limit", "30"))
        return httpx.Response(200, json={"products": products[(n - 1) * size: n * size]})

    store = Store({("s.com", "/products.json"): redirect(301, "https://www.s.com/products.json"),
                   ("www.s.com", "/products.json"): www,
                   **cart_to_checkout(host="s.com", vid="7050", body=checkout_body(variant="7050"))})
    result = await run(store, host="s.com", variant_id="7050")
    assert result.verdict is Verdict.ELIGIBLE
    (www_read,) = [r for r in store.requests if r.url.host == "www.s.com"]
    assert (www_read.url.params.get("limit"), www_read.url.params.get("page"),
            www_read.url.params.get("country")) == ("250", "1", "US")


# --- P10: smaller gaps ---------------------------------------------------------------------------


def test_the_checkout_path_is_anchored():
    body = checkout_body()
    assert _classify("https://s.com/pages/checkouts/c/x", body=body)[0] is Verdict.UNCLASSIFIED
    assert _classify("https://s.com/en/checkouts/cn/x", body=body)[0] is Verdict.UNCLASSIFIED
    assert _classify("https://s.com/checkouts/c/x", body=body)[0] is Verdict.ELIGIBLE
    assert _classify("https://s.com/pages/checkouts/x", status=403, body=NOT_ACCEPTING_BODY)[0] \
        is Verdict.BLOCKED_UNKNOWN
    assert _classify("https://s.com/checkouts/cn/x", status=403, body=NOT_ACCEPTING_BODY)[0] \
        is Verdict.NOT_ACCEPTING_ORDERS


def test_classic_account_login_is_login_required():
    good = f"https://s.com/checkouts/cn/{TOKEN}"
    assert _classify(good, chain=[(302, "https://s.com/cart/1:1"), (200, "https://s.com/account/login")],
                     body="<form>")[0] is Verdict.LOGIN_REQUIRED
    assert _classify(good, chain=[(302, "https://s.com/account/login?return_url=x"), (200, good)])[0] \
        is Verdict.LOGIN_REQUIRED
    assert _classify(good, chain=[(302, "https://s.com/account"), (200, good)])[0] is Verdict.ELIGIBLE
