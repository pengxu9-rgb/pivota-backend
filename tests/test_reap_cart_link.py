"""services/reap_cart_link.py — the one cart-permalink shape Reap may be asked to quote. Pure.

EVERY EXAMPLE IN THE BRIEF IS A ROW BELOW, AND EVERY REFUSAL ASSERTS ITS REASON CODE, not merely
"refused". The rules overlap on purpose (a `checkout[...]` key is also an extra query key), so a
test that only asserted None could not tell a deleted rule from a live one: the next rule would
refuse the same URL and the test would stay green. Asserting the code is what makes each rule's
deletion a failing test — the mutation table in the PR body is built on that.

No database, no network. Runs identically under either dialect's DATABASE_URL.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import services.reap_cart_link as cl  # noqa: E402

SHOP = "judydoll.com"
CLICK = "clk_9b1c3dcd4a854e26a5c8a295"
VARIANT = "49922977038613"
MARKET = "US"
GOOD = f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&country=US"
CANONICAL = GOOD

#: A full prefill link of the shape Reap's example used: the buyer's email and address as
#: `checkout[...]` keys, AND our click attribute, AND a legitimate line. Everything about it is
#: right except that it carries PII in the URL — which is the one thing that must never be stored.
PREFILL_LINK = (
    f"https://{SHOP}/cart/{VARIANT}:1"
    "?checkout[email]=adrian%40example.com"
    "&checkout[shipping_address][first_name]=Adrian"
    "&checkout[shipping_address][last_name]=Tan"
    "&checkout[shipping_address][address1]=1%20Raffles%20Place"
    "&checkout[shipping_address][city]=Singapore"
    "&checkout[shipping_address][zip]=048616"
    "&checkout[shipping_address][country]=SG"
    f"&attributes[pivota_click_id]={CLICK}"
    "&country=SG"
)


def _check(url, click_id=CLICK, shop_domain=SHOP, market=MARKET):
    return cl.cart_link_refusal(
        url, click_id=click_id, shop_domain=shop_domain, market=market
    )


def _valid(url, click_id=CLICK, shop_domain=SHOP, market=MARKET):
    return cl.validate_cart_link(
        url, click_id=click_id, shop_domain=shop_domain, market=market
    )


# ── ACCEPT ───────────────────────────────────────────────────────────────────────────────────

ACCEPT = [
    ("the brief's example, with the market pin", GOOD, CANONICAL),
    (
        "country first, click id second",
        f"https://{SHOP}/cart/{VARIANT}:1?country=US&attributes[pivota_click_id]={CLICK}",
        CANONICAL,
    ),
    (
        "a lower-case country is folded to the canonical upper case",
        f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&country=us",
        CANONICAL,
    ),
    (
        "percent-encoded brackets",
        f"https://{SHOP}/cart/{VARIANT}:1?attributes%5Bpivota_click_id%5D={CLICK}&country=US",
        CANONICAL,
    ),
    (
        "lowercase percent-encoding",
        f"https://{SHOP}/cart/{VARIANT}:1?attributes%5bpivota_click_id%5d={CLICK}&country=US",
        CANONICAL,
    ),
    (
        "qty 2",
        f"https://{SHOP}/cart/{VARIANT}:2?attributes[pivota_click_id]={CLICK}&country=US",
        f"https://{SHOP}/cart/{VARIANT}:2?attributes[pivota_click_id]={CLICK}&country=US",
    ),
    (
        "the www. host of the shop",
        f"https://www.{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&country=US",
        f"https://www.{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&country=US",
    ),
    (
        "qty 100, the ceiling",
        f"https://{SHOP}/cart/{VARIANT}:100?attributes[pivota_click_id]={CLICK}&country=US",
        f"https://{SHOP}/cart/{VARIANT}:100?attributes[pivota_click_id]={CLICK}&country=US",
    ),
    (
        "an uppercase host is folded, not refused",
        f"https://JudyDoll.COM/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&country=US",
        CANONICAL,
    ),
]


@pytest.mark.parametrize("label,url,canonical", ACCEPT, ids=[a[0] for a in ACCEPT])
def test_accepted_links_return_their_canonical_form(label, url, canonical):
    assert _check(url) is None, label
    assert _valid(url) == canonical


def test_the_shop_domain_is_compared_case_insensitively():
    assert _valid(GOOD, shop_domain="  JudyDoll.com ") == GOOD


# ── REFUSE ───────────────────────────────────────────────────────────────────────────────────

_TAIL = f"?attributes[pivota_click_id]={CLICK}&country=US"

REFUSE = [
    # (label, url, expected code)
    ("Adrian-shaped full prefill link", PREFILL_LINK, "checkout_prefill"),
    (
        "a single checkout[email] key",
        f"https://{SHOP}/cart/{VARIANT}:1?checkout[email]=a%40b.co&attributes[pivota_click_id]={CLICK}",
        "checkout_prefill",
    ),
    (
        "checkout[...] percent-encoded",
        f"https://{SHOP}/cart/{VARIANT}:1?checkout%5Bemail%5D=a%40b.co&attributes[pivota_click_id]={CLICK}",
        "checkout_prefill",
    ),
    (
        "checkout double-percent-encoded",
        f"https://{SHOP}/cart/{VARIANT}:1?%2563heckout%255Bemail%255D=x&attributes[pivota_click_id]={CLICK}",
        "checkout_prefill",
    ),
    (
        "CHECKOUT in upper case",
        f"https://{SHOP}/cart/{VARIANT}:1?CHECKOUT[email]=x&attributes[pivota_click_id]={CLICK}",
        "checkout_prefill",
    ),
    ("http://", f"http://{SHOP}/cart/{VARIANT}:1{_TAIL}", "not_https"),
    ("HTTPS in capitals is not our spelling", f"HTTPS://{SHOP}/cart/{VARIANT}:1{_TAIL}", "not_https"),
    ("no scheme", f"//{SHOP}/cart/{VARIANT}:1{_TAIL}", "not_https"),
    ("userinfo", f"https://u:p@{SHOP}/cart/{VARIANT}:1{_TAIL}", "userinfo"),
    ("bare user", f"https://u@{SHOP}/cart/{VARIANT}:1{_TAIL}", "userinfo"),
    ("IPv4 literal", f"https://192.0.2.10/cart/{VARIANT}:1{_TAIL}", "ip_literal"),
    ("IPv6 literal", f"https://[::1]/cart/{VARIANT}:1{_TAIL}", "ip_literal"),
    ("localhost", f"https://localhost/cart/{VARIANT}:1{_TAIL}", "host_not_a_hostname"),
    ("trailing-dot host", f"https://{SHOP}./cart/{VARIANT}:1{_TAIL}", "host_not_a_hostname"),
    ("a port", f"https://{SHOP}:8443/cart/{VARIANT}:1{_TAIL}", "port"),
    ("even :443", f"https://{SHOP}:443/cart/{VARIANT}:1{_TAIL}", "port"),
    ("another shop", f"https://evil.example/cart/{VARIANT}:1{_TAIL}", "host_mismatch"),
    ("a lookalike suffix", f"https://{SHOP}.evil.example/cart/{VARIANT}:1{_TAIL}", "host_mismatch"),
    ("a subdomain that is not www", f"https://shop.{SHOP}/cart/{VARIANT}:1{_TAIL}", "host_mismatch"),
    ("a prefix-glued host", f"https://evil{SHOP}/cart/{VARIANT}:1{_TAIL}", "host_mismatch"),
    ("opaque cart token", f"https://{SHOP}/cart/c/Z2NwLXVzLWVhc3QxOjAxSjk{_TAIL}", "opaque_cart_token"),
    ("two lines", f"https://{SHOP}/cart/111:1,222:1{_TAIL}", "multiple_lines"),
    ("the brief's a:1,b:1", f"https://{SHOP}/cart/{VARIANT}:1,49922977038614:1{_TAIL}", "multiple_lines"),
    ("no quantity", f"https://{SHOP}/cart/{VARIANT}{_TAIL}", "path_shape"),
    ("a non-numeric variant", f"https://{SHOP}/cart/abc:1{_TAIL}", "path_shape"),
    ("/cart/add", f"https://{SHOP}/cart/add{_TAIL}", "path_shape"),
    ("trailing slash", f"https://{SHOP}/cart/{VARIANT}:1/{_TAIL}", "path_shape"),
    ("a path prefix", f"https://{SHOP}/en/cart/{VARIANT}:1{_TAIL}", "path_shape"),
    ("a checkout path", f"https://{SHOP}/checkouts/abc{_TAIL}", "path_shape"),
    ("a leading-zero variant", f"https://{SHOP}/cart/0{VARIANT}:1{_TAIL}", "path_shape"),
    ("a leading-zero quantity", f"https://{SHOP}/cart/{VARIANT}:01{_TAIL}", "path_shape"),
    ("percent-encoded path digits", f"https://{SHOP}/cart/%34{VARIANT}:1{_TAIL}", "path_shape"),
    ("qty 0", f"https://{SHOP}/cart/{VARIANT}:0{_TAIL}", "quantity_out_of_range"),
    ("qty 101", f"https://{SHOP}/cart/{VARIANT}:101{_TAIL}", "quantity_out_of_range"),
    ("qty 1000", f"https://{SHOP}/cart/{VARIANT}:1000{_TAIL}", "quantity_out_of_range"),
    ("no query at all", f"https://{SHOP}/cart/{VARIANT}:1", "click_id_missing"),
    ("an empty query", f"https://{SHOP}/cart/{VARIANT}:1?", "click_id_missing"),
    (
        "the key with no value",
        f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]&country=US",
        "click_id_missing",
    ),
    (
        "an empty click id",
        f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]=&country=US",
        "click_id_malformed",
    ),
    (
        "a percent-encoded click id",
        f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]=clk%5F9b1c&country=US",
        "click_id_malformed",
    ),
    (
        "someone else's click id",
        f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]=clk_000000000000000000000000&country=US",
        "click_id_mismatch",
    ),
    ("discount=", f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}&discount=SAVE50", "extra_query_key"),
    ("payment=shop_pay", f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}&payment=shop_pay", "extra_query_key"),
    ("note=", f"https://{SHOP}/cart/{VARIANT}:1?note=hi&attributes[pivota_click_id]={CLICK}&country=US", "extra_query_key"),
    ("ref=", f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}&ref=abc", "extra_query_key"),
    ("ref= alone, no click id", f"https://{SHOP}/cart/{VARIANT}:1?ref=abc", "extra_query_key"),
    (
        "our own recovery-key attribute",
        f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}&attributes[pivota_recovery_key]=rk_1",
        "extra_query_key",
    ),
    ("the click key twice", f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}&attributes[pivota_click_id]={CLICK}", "extra_query_key"),
    ("an empty segment", f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}&", "extra_query_key"),
    # THE MARKET PIN (coordinator amendment, 2026-09-18).
    ("country missing", f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}", "country_missing"),
    ("country=JP for a US row", f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&country=JP", "country_mismatch"),
    ("country repeated", f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}&country=US", "country_repeated"),
    ("country repeated, disagreeing", f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}&country=JP", "country_repeated"),
    ("country=USA", f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&country=USA", "country_malformed"),
    ("country=", f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&country=", "country_malformed"),
    ("a bare country key", f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&country", "country_malformed"),
    ("a percent-encoded country", f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&country=%55S", "country_malformed"),
    ("Country= in another case is another key", f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]={CLICK}&Country=US", "extra_query_key"),
    ("a fragment", f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}#frag", "fragment"),
    ("an empty fragment", f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}#", "fragment"),
    ("CR", f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}\r", "control_character"),
    ("LF", f"https://{SHOP}/cart/{VARIANT}:1{_TAIL}\nX-Injected: 1", "control_character"),
    ("CRLF in the host", f"https://{SHOP}\r\n/cart/{VARIANT}:1{_TAIL}", "control_character"),
    ("NUL", f"https://{SHOP}/cart/{VARIANT}:1\x00{_TAIL}", "control_character"),
    ("TAB", f"https://{SHOP}/cart/\t{VARIANT}:1{_TAIL}", "control_character"),
    ("a space", f"https://{SHOP}/cart/{VARIANT}:1 {_TAIL}", "control_character"),
    ("a backslash", f"https://{SHOP}\\@evil.example/cart/{VARIANT}:1{_TAIL}", "disallowed_character"),
    ("a non-ASCII host", f"https://judydöll.com/cart/{VARIANT}:1{_TAIL}", "disallowed_character"),
    ("a zero-width space", f"https://{SHOP}/cart/{VARIANT}:1​{_TAIL}", "disallowed_character"),
    ("length 2049", GOOD + "&" + "a" * (2048 - len(GOOD)), "too_long"),
]


@pytest.mark.parametrize("label,url,code", REFUSE, ids=[r[0] for r in REFUSE])
def test_refused_links_name_their_reason(label, url, code):
    assert _check(url) == code, label
    assert _valid(url) is None


def test_the_length_rule_is_exactly_2048():
    at_limit = f"https://{SHOP}/cart/{VARIANT}:1?attributes[pivota_click_id]=" + "a" * 128
    assert len(at_limit) < 2048  # control: an ordinary long link is fine
    padded = GOOD + "&x=" + "a" * (2048 - len(GOOD) - 3)
    assert len(padded) == 2048
    # Exactly at the limit, the length rule passes and the NEXT rule (one extra key) is the one
    # that refuses — which is how this test tells `>` from `>=`.
    assert _check(padded) == "extra_query_key"
    assert _check(padded + "a") == "too_long"


@pytest.mark.parametrize(
    "click_id,code",
    [
        (None, "expected_click_id_invalid"),
        ("", "expected_click_id_invalid"),
        ("clk 1", "expected_click_id_invalid"),
        ("clk_OTHER", "click_id_mismatch"),
    ],
)
def test_the_expected_click_id_must_be_a_real_one(click_id, code):
    assert _check(GOOD, click_id=click_id) == code


@pytest.mark.parametrize(
    "shop_domain",
    [None, "", "localhost", "192.0.2.10", "https://judydoll.com", "judydoll.com/", 42],
)
def test_a_shop_domain_that_is_not_a_hostname_refuses(shop_domain):
    assert _check(GOOD, shop_domain=shop_domain) == "shop_domain_invalid"


@pytest.mark.parametrize("market", [None, "", "USA", "U", "1S", 840])
def test_the_expected_market_must_be_two_letters(market):
    assert _check(GOOD, market=market) == "expected_market_invalid"


@pytest.mark.parametrize("market", ["US", "us", " Us "])
def test_the_expected_market_is_compared_case_insensitively(market):
    assert _valid(GOOD, market=market) == GOOD


def test_the_same_link_for_another_market_is_refused_not_rewritten():
    """The validator is a GATE, not a builder: a link pinned to US handed in for an SG row is
    refused, never re-pinned. Re-pinning would be us choosing the buyer's market."""
    assert _check(GOOD, market="SG") == "country_mismatch"


def test_a_www_shop_domain_does_not_admit_the_apex():
    """The rule is "the shop's host or its www." — not "anything sharing a registrable domain"."""
    assert _check(GOOD, shop_domain=f"www.{SHOP}") == "host_mismatch"


@pytest.mark.parametrize("bad", [None, 42, b"https://judydoll.com/cart/1:1", ["x"]])
def test_a_non_string_refuses_without_raising(bad):
    assert _check(bad) == "not_a_string"
    assert _valid(bad) is None


def test_no_refusal_code_ever_contains_the_url_or_its_pii():
    """The code goes into exceptions and log lines; the URL may be carrying the PII it was
    refused for. Every code is a fixed vocabulary word."""
    for _label, url, _code in REFUSE:
        code = _check(url)
        assert code is not None and code.replace("_", "").isalpha(), code
        assert "adrian" not in code and SHOP not in code


# ── the helpers the service uses on a STORED url ─────────────────────────────────────────────


def test_line_and_click_id_are_read_from_a_valid_link_only():
    assert cl.cart_link_line(GOOD) == (VARIANT, 1)
    assert cl.cart_link_click_id(GOOD) == CLICK
    assert cl.cart_link_line(PREFILL_LINK) is None
    assert cl.cart_link_click_id(PREFILL_LINK) is None
    assert cl.cart_link_line(None) is None and cl.cart_link_click_id(42) is None


# ── pinned to the builder this repo already has ──────────────────────────────────────────────


def test_the_attribute_name_is_the_outbound_builders_own():
    from services.outbound_links_service import SHOPIFY_CART_CLICK_ATTRIBUTE

    assert cl.CART_CLICK_ATTRIBUTE == SHOPIFY_CART_CLICK_ATTRIBUTE


@pytest.mark.parametrize("qty", [1, 2, 7])
def test_what_build_shopify_cart_permalink_builds_is_accepted_verbatim(qty):
    """The link our own outbound builder produces must validate, and must already BE canonical —
    otherwise the ledger would store a different string from the one the agent was handed."""
    from services.outbound_links_service import build_shopify_cart_permalink

    built = build_shopify_cart_permalink(
        shop_domain=SHOP, variant_id=VARIANT, click_id=CLICK, quantity=qty
    )
    assert built is not None
    assert _valid(built + "&country=US") == built + "&country=US"
