"""ADR-027 D7, the deal lane's link: a verified deal is resolved at mint time against the link the
mint composes, its REFERENCE is signed into the `/r` token (never the code), `/r` re-checks it and
appends `discount=` only to the verified variant's pinned cart, and the click row records the code
(issue time) and whether `/r` carried it (click time). The real mint, token and `/r` route run on
the suite's SQLite file; only the merchant-store lookup is faked."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import routes.agent_shop_gateway as gw  # noqa: E402
import services.merchant_store_service as mss  # noqa: E402
from services import deal_lane_decision as dld  # noqa: E402
from services.outbound_links_service import (  # noqa: E402
    make_redirect_token,
    parse_and_verify_redirect_token,
)

SHOP = "conn-store.myshopify.com"
VARIANT = "46000000001"
CART = f"https://{SHOP}/cart/{VARIANT}:1"
REF = {"deal_id": "deal_a", "variant_id": VARIANT, "market": "US"}


def _payload(redirect_url: str) -> Dict[str, Any]:
    return parse_and_verify_redirect_token(parse_qs(urlparse(redirect_url).query)["token"][0])


# ---- the destination check -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dest,ok",
    [
        (f"{CART}?country=US", True),
        (f"{CART}?attributes[pivota_click_id]=clk_1&utm_source=pivota&country=US", True),
        (f"{CART}?country=us", False),  # the pin is spelled upper-case by every cart lane
        (f"{CART}?country=SG", False),
        (CART, False),  # not pinned
        (f"{CART}?country=US&country=SG", False),
        (f"{CART}?country=US&discount=OTHER", False),  # never a second code
        (f"https://{SHOP}/cart/{VARIANT}:2?country=US", False),  # one unit only
        (f"https://{SHOP}/cart/46000000002:1?country=US", False),  # another variant
        (f"https://{SHOP}/cart/{VARIANT}:1,46000000002:1?country=US", False),  # another line
        (f"https://{SHOP}/products/serum?country=US", False),  # a product page (referral)
        (f"https://{SHOP}/cart/c/abc123?country=US", False),  # a warm-handoff continue_url
        (f"http://{SHOP}/cart/{VARIANT}:1?country=US", False),
        ("not a url", False),
    ],
)
def test_only_the_verified_variants_pinned_one_unit_cart_may_carry_a_code(dest, ok):
    assert dld.cart_dest_matches(dest, variant_id=VARIANT, market="US") is ok


def test_the_code_is_percent_encoded():
    assert dld.append_discount(f"{CART}?country=US", "GLOW 10&x=1") == f"{CART}?country=US&discount=GLOW%2010%26x%3D1"
    assert dld.append_discount(CART, " GLOW10 ") == f"{CART}?discount=GLOW10"


def test_the_token_key_for_the_pin_is_the_one_the_mint_writes():
    assert gw.TOKEN_CART_COUNTRY_KEY == "cart_country"  # read by deal_destination_at_click


# ---- deals on the suite's SQLite file --------------------------------------------------------------


@pytest.fixture
async def db(monkeypatch):
    from db import deal_lane
    from db.commerce_attribution import surface_click_events
    from db.database import database, engine, metadata

    tables = [
        deal_lane.observed_store_offers,
        deal_lane.observed_store_offer_verifications,
        deal_lane.deal_lane_stops,
        deal_lane.deal_lane_optout,
    ]
    metadata.create_all(engine, tables=[*tables, surface_click_events])
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    async def _clean():
        for table in reversed(tables):
            await database.execute(table.delete())
        await database.execute(surface_click_events.delete().where(surface_click_events.c.click_id.like("dlr_%")))

    await _clean()
    monkeypatch.setenv("DEAL_LANE_EMIT_ENABLED", "true")
    monkeypatch.setenv("DEAL_LANE_DISCOUNT_PARAM_ENABLED", "true")
    try:
        yield database
    finally:
        await _clean()
        if not was_connected:
            await database.disconnect()


async def _seed(database, *, deal_id="deal_a", code="GLOW10", seller_id="merch_conn"):
    from db.deal_lane import observed_store_offer_key, observed_store_offer_verifications, observed_store_offers

    now = datetime.now(timezone.utc)
    await database.execute(
        observed_store_offers.insert().values(
            deal_id=deal_id,
            deal_key=observed_store_offer_key(
                seller_id=seller_id, market="US", code=code, platform_rule_ref=None, program_id=None
            ),
            seller_id=seller_id, market="US", code=code, kind="percent", value=10, currency="USD",
            source="merchant_published", eligibility_known=True, observed_at=now,
        )
    )
    await database.execute(
        observed_store_offer_verifications.insert().values(
            deal_id=deal_id, variant_id=VARIANT, state="checkout_verified",
            verified_at=now - timedelta(hours=1), expires_at=now + timedelta(hours=23),
            verified_via="shopify_storefront_connected", authorized_by="connected",
            ref_unit_price=Decimal("40"), ref_subtotal=Decimal("40"), ref_discount_amount=Decimal("4"),
            ref_currency="USD", ref_automatic_discounts=[],
        )
    )


# ---- the mint ------------------------------------------------------------------------------------


async def _mint(*, deal_offer=None, cart_country=None, issued=None):
    return await gw._make_external_redirect_url(
        market="US",
        market_observed=False,
        tool="find_products",
        destination_url=f"https://{SHOP}/products/serum",
        utm_template=None,
        ctx={"source": "connected_catalog"},
        allowed_domains=[SHOP],
        merchant_id="merch_conn",
        product_id="9990001",
        variant_id=VARIANT,
        cart_variant_id=VARIANT,
        shop_domain=SHOP,
        platform="shopify",
        click_id="dlr_clk_1",
        issued_clicks=issued,
        cart_country=cart_country,
        deal_offer=deal_offer,
    )


def _offer(**over):
    base = dict(seller_id="merch_conn", buyer_market="US", observed_unit_price="40.00", observed_currency="USD")
    base.update(over)
    return dld.DealOffer(**base)


async def test_a_verified_deal_pins_the_cart_and_signs_its_reference_never_the_code(db):
    await _seed(db)
    issued: List[Any] = []
    link = await _mint(deal_offer=_offer(), issued=issued)
    payload = _payload(link)
    assert payload[dld.TOKEN_DEAL_REF_KEY] == REF
    assert payload["cart_country"] == "US"
    assert dld.cart_dest_matches(payload["dest"], variant_id=VARIANT, market="US")
    assert "GLOW10" not in link and "GLOW10" not in str(payload)
    assert "discount" not in payload["dest"]
    assert [c.discount_code for c in issued] == ["GLOW10"]


async def test_the_published_cart_url_never_carries_the_code(db):
    await _seed(db)
    link = await _mint(deal_offer=_offer())
    attribution = gw._seed_attribution_from_redirect(
        link, destination_url=f"https://{SHOP}/products/serum", utm_template=None, market="US",
        tool="find_products", shop_domain=SHOP, platform="shopify", cart_variant_id=VARIANT,
    )
    # The direct cart_url (no `/r` hop) matches the signed dest, pinned, and has no code (lane matrix).
    assert attribution["cart_url"] == _payload(link)["dest"]
    assert "discount" not in attribution["cart_url"]


@pytest.mark.parametrize(
    "offer,flags_off",
    [
        (None, False),
        (_offer(buyer_market=None), False),  # a defaulted market is not the buyer's
        (_offer(observed_unit_price="41.00"), False),  # price moved since the reference cart
        (_offer(seller_id="merch_other"), False),
        (_offer(), True),
    ],
)
async def test_without_a_decision_the_link_is_byte_identical(db, monkeypatch, offer, flags_off):
    import services.outbound_links_service as ols

    monkeypatch.setattr(ols, "_now_ts", lambda: 1_800_000_000)  # the token carries iat/exp
    await _seed(db)
    if flags_off:
        monkeypatch.setenv("DEAL_LANE_EMIT_ENABLED", "false")
    baseline_issued: List[Any] = []
    baseline = await _mint(issued=baseline_issued)
    issued: List[Any] = []
    assert await _mint(deal_offer=offer, issued=issued) == baseline
    assert [c.discount_code for c in issued] == [None]


async def test_a_mint_already_pinned_to_another_market_gets_no_deal(db):
    await _seed(db)
    sg_pinned = await gw._make_external_redirect_url(
        market="SG", market_observed=True, tool="find_products", destination_url=f"https://{SHOP}/products/serum",
        utm_template=None, ctx={}, allowed_domains=[SHOP], merchant_id="merch_conn", variant_id=VARIANT,
        cart_variant_id=VARIANT, shop_domain=SHOP, platform="shopify", click_id="dlr_clk_2",
        cart_country="SG", deal_offer=_offer(),
    )
    assert dld.TOKEN_DEAL_REF_KEY not in _payload(sg_pinned)


async def test_a_failing_decision_never_costs_the_link(db, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("decision broke")

    monkeypatch.setattr(gw, "resolve_deal_decision", _boom)
    link = await _mint(deal_offer=_offer())
    assert link and dld.TOKEN_DEAL_REF_KEY not in _payload(link)


async def test_connected_shopify_cards_offer_their_variant_at_the_buyers_market(db, monkeypatch):
    async def _stores(merchant_id):
        return [{"platform": "shopify", "domain": SHOP, "status": "active"}]

    monkeypatch.setattr(mss, "get_merchant_active_stores", _stores)
    await _seed(db)
    card = {
        "id": "9990001", "product_id": "9990001", "merchant_id": "merch_conn", "platform": "shopify",
        "handle": "serum", "currency": "USD",
        "variants": [{"variant_id": VARIANT, "id": VARIANT, "price": 40.0}],
    }
    await gw._attach_connected_product_redirects([card], tool="find_products", request_market="US")
    payload = _payload(card["external_redirect_url"])
    assert payload[dld.TOKEN_DEAL_REF_KEY] == REF
    # The card's direct cart_url is the pinned signed dest, without the code.
    assert card["cart_url"] == payload["dest"] and "discount" not in card["cart_url"]

    undeclared = dict(card, external_redirect_url=None, cart_url=None)
    undeclared.pop("external_redirect_url")
    await gw._attach_connected_product_redirects([undeclared], tool="find_products", request_market=None)
    assert dld.TOKEN_DEAL_REF_KEY not in _payload(undeclared["external_redirect_url"])


async def test_non_shopify_or_cartless_cards_offer_nothing(monkeypatch):
    offers = []

    async def _spy(**kwargs):
        offers.append(kwargs.get("deal_offer"))
        return None

    async def _stores(merchant_id):
        return [{"platform": "woocommerce", "domain": "woo.example", "status": "active"}]

    monkeypatch.setattr(gw, "_make_external_redirect_url", _spy)
    monkeypatch.setattr(mss, "get_merchant_active_stores", _stores)
    woo = {"product_id": "1", "merchant_id": "merch_woo", "platform": "woocommerce", "handle": "x",
           "variants": [{"variant_id": "7", "price": 5}]}
    await gw._attach_connected_product_redirects([woo], tool="find_products", request_market="US")
    assert offers == [None]


# ---- `/r` ----------------------------------------------------------------------------------------


def _token(*, dest=None, ref=REF, cart_country="US", ttl=3600, extra=None) -> str:
    body = {
        "market": "US",
        "tool": "find_products",
        "dest": dest or f"{CART}?attributes[pivota_click_id]=dlr_clk_9&country=US",
        "ctx": {"pvt_click_id": "dlr_clk_9", "merchant_id": "merch_conn"},
        **({"cart_country": cart_country} if cart_country else {}),
        **({dld.TOKEN_DEAL_REF_KEY: ref} if ref is not None else {}),
        **(extra or {}),
    }
    return make_redirect_token(body, ttl_seconds=ttl)


@pytest.fixture
def client_and_log(monkeypatch):
    from fastapi.testclient import TestClient

    import routes.outbound_links as outbound_routes
    from main import app

    logged: List[Dict[str, Any]] = []

    async def _log(**kwargs):
        logged.append(kwargs["token_payload"])

    monkeypatch.setattr(outbound_routes, "log_outbound_click", _log)
    return TestClient(app), logged


def _follow(client, token):
    return client.get("/r", params={"token": token}, follow_redirects=False)


async def test_r_appends_the_code_when_the_recheck_passes(db, client_and_log):
    await _seed(db)
    client, logged = client_and_log
    resp = _follow(client, _token())
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("&country=US&discount=GLOW10")
    assert logged[-1]["discount_carried"] is True
    assert logged[-1]["ctx"]["deal_reason"] == "ok"
    # The signed dest the click row records stays the code-free one.
    assert "discount" not in logged[-1]["dest"]


@pytest.mark.parametrize(
    "env,mutate,reason",
    [
        ({"DEAL_LANE_DISCOUNT_PARAM_ENABLED": "false"}, None, "discount_param_off"),
        ({"DEAL_LANE_EMIT_ENABLED": "false"}, None, "emit_off"),
        ({}, "stop", "market_stopped"),
        ({}, "unverify", "no_deal"),
    ],
)
async def test_r_drops_the_code_on_links_already_issued(db, client_and_log, monkeypatch, env, mutate, reason):
    from db.deal_lane import deal_lane_stops, observed_store_offer_verifications

    await _seed(db)
    client, logged = client_and_log
    token = _token()  # issued while everything passed
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if mutate == "stop":
        await db.execute(deal_lane_stops.insert().values(stop_id="s1", market="US", stop_trigger="manual"))
    if mutate == "unverify":
        await db.execute(observed_store_offer_verifications.update().values(state="unverified"))
    resp = _follow(client, token)
    assert "discount" not in resp.headers["location"]
    assert (logged[-1]["discount_carried"], logged[-1]["ctx"]["deal_reason"]) == (False, reason)


@pytest.mark.parametrize(
    "token_kwargs,reason",
    [
        (dict(dest=f"https://{SHOP}/products/serum?country=US"), "dest_not_the_verified_cart"),
        (dict(cart_country=None), "dest_not_the_verified_cart"),
        (dict(ref={"deal_id": "deal_a", "variant_id": VARIANT, "market": "SG"}), "reference_other_market"),
        (dict(ref={"deal_id": "deal_a"}), "bad_reference"),
        (dict(ref={"deal_id": "deal_gone", "variant_id": VARIANT, "market": "US"}), "no_deal"),
    ],
)
async def test_r_never_appends_off_the_verified_cart(db, client_and_log, token_kwargs, reason):
    await _seed(db)
    client, logged = client_and_log
    resp = _follow(client, _token(**token_kwargs))
    assert "discount" not in resp.headers["location"]
    assert (logged[-1]["discount_carried"], logged[-1]["ctx"]["deal_reason"]) == (False, reason)


async def test_r_leaves_a_token_without_a_deal_exactly_as_before(db, client_and_log):
    await _seed(db)
    client, logged = client_and_log
    dest = f"{CART}?attributes[pivota_click_id]=dlr_clk_9&country=US"
    resp = _follow(client, _token(ref=None))
    assert resp.headers["location"] == dest
    assert "discount_carried" not in logged[-1] and "deal_reason" not in logged[-1]["ctx"]


async def test_expired_and_synthetic_tokens_never_carry_a_code(db, client_and_log, monkeypatch):
    await _seed(db)
    client, logged = client_and_log
    expired = _token(ttl=-10)
    assert "discount" not in _follow(client, expired).headers["location"]
    monkeypatch.setattr("routes.outbound_links.token_is_synthetic", lambda payload: True)
    assert "discount" not in _follow(client, _token()).headers["location"]
    assert logged == []


async def test_r_never_warms_a_carried_code(db, client_and_log, monkeypatch):
    import routes.outbound_links as outbound_routes
    from config.settings import settings

    await _seed(db)
    client, _ = client_and_log
    calls = []

    def _eligible(**kwargs):
        calls.append(kwargs)
        return True, "ok"

    monkeypatch.setattr(settings, "outbound_warm_handoff_enabled", True)
    monkeypatch.setattr(outbound_routes, "evaluate_warm_eligibility", _eligible)
    resp = _follow(client, _token())
    assert resp.headers["location"].endswith("discount=GLOW10")
    assert calls == []


async def test_a_recheck_that_raises_still_redirects_to_the_signed_dest(db, client_and_log, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(dld, "recheck_deal_reference", _boom)
    client, logged = client_and_log
    resp = _follow(client, _token())
    assert resp.status_code == 302 and "discount" not in resp.headers["location"]
    assert (logged[-1]["discount_carried"], logged[-1]["ctx"]["deal_reason"]) == (False, "recheck_failed")


# ---- the click row -------------------------------------------------------------------------------


async def _row(database, click_id):
    from db.commerce_attribution import surface_click_events

    return await database.fetch_one(
        surface_click_events.select().where(surface_click_events.c.click_id == click_id)
    )


async def test_the_issued_row_records_the_code_and_r_records_whether_it_carried_it(db):
    from services.commerce_attribution_service import IssuedClick, issue_clicks, record_surface_event

    await issue_clicks([IssuedClick(click_id="dlr_row_1", surface="find_products", discount_code="GLOW10")])
    row = await _row(db, "dlr_row_1")
    assert (row["discount_code"], row["discount_carried"]) == ("GLOW10", None)

    async def click(carried=None, event="click"):
        payload = {"tool": "find_products", "dest": CART, "ctx": {"pvt_click_id": "dlr_row_1"}}
        if carried is not None:
            payload["discount_carried"] = carried
        await record_surface_event(token_payload=payload, request_meta={}, event_type=event)
        return (await _row(db, "dlr_row_1"))["discount_carried"]

    assert await click(event="impression", carried=True) is None  # only clicks say anything
    assert await click(carried=None) is None  # a token without a deal says nothing
    assert await click(carried=False) in (False, 0)
    assert await click(carried=True) in (True, 1)
    # Once a click carried the code, an order may hold it: a later stripped click cannot undo that.
    assert await click(carried=False) in (True, 1)


async def test_a_too_long_code_is_not_written(db):
    from services.commerce_attribution_service import IssuedClick, issue_clicks

    await issue_clicks([IssuedClick(click_id="dlr_row_2", surface="find_products", discount_code="X" * 256)])
    assert (await _row(db, "dlr_row_2"))["discount_code"] is None
