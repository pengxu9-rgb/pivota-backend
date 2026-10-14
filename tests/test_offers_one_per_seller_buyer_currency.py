"""offers.resolve compares SELLERS, in the buyer's currency, and hands out carts pinned to the buyer.

Live 2026-10-14 (prod agent door, get_offers market=US): Rare Beauty Soft Pinch Liquid Blush came
back as five "offers" -- five shades of rarebeauty.com -- and `best_offer` was the first shade.
The seed lane fanned out `variants[:min(12, limit)]`, one offer per shade.

Three rules, each driven through the REAL route (only the DB read is faked; the real redirect
minter runs so a cart link can be compared with the destination its token signs):

1. ONE OFFER PER SELLER LISTING when no variant is named. Its cart is the seed's own variant
   (attached, else first in stock, else first); every variant rides in `variants` without links.
   A named variant (`product.sku_id`) still gets exactly that variant.
2. THE BUYER'S CURRENCY BEFORE THE CUT. With a declared market, offers in another currency are
   dropped before `limit`, and counted in `metadata.buyer_currency_filtered`.
3. THE BUYER'S COUNTRY ON THE CART. With a declared market, the cart permalink carries
   `country=<CC>`, identically in the published `cart_url` and the signed `/r` destination.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from main import app
from tests.test_offers_resolve import _CATALOG_OFFER_ROW, _VERIFIED_CONTENT, _VERIFIED_DESTINATION

HOST = "rarebeauty.com"
SHADES = ["Bliss", "Hope", "Adore", "Happy", "Worth"]


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _shade(i: int, name: str, *, availability: str = "in_stock", currency: str = "USD") -> Dict[str, Any]:
    return {
        "variant_id": str(43734829564000 + i),
        "title": name,
        "price_amount": 25.0,
        "price_currency": currency,
        "availability": availability,
        "variant_id_provenance": "merchant_issued",
        # The cart-proof stamp a cart permalink is built from (see _seed_row_for_exec_spec).
        "shopify_variant_id": str(43734829564000 + i),
    }


def _seed(
    *,
    seed_id: str = "eps_rare_blush",
    host: str = HOST,
    variants: Optional[List[Dict[str, Any]]] = None,
    currency: str = "USD",
    attached_variant_id: Optional[str] = None,
) -> Dict[str, Any]:
    url = f"https://{host}/products/soft-pinch-liquid-blush"
    variants = variants if variants is not None else [_shade(i, n) for i, n in enumerate(SHADES)]
    return {
        "id": seed_id,
        "external_product_id": f"ext_{seed_id}",
        "market": "US",
        "tool": "*",
        "destination_url": url,
        "canonical_url": url,
        "domain": host,
        "title": "Soft Pinch Liquid Blush",
        "price_amount": 25.0,
        "price_currency": currency,
        "availability": "in_stock",
        "utm_template": None,
        "attached_variant_id": attached_variant_id,
        "seed_data": {
            "snapshot": {
                **_VERIFIED_CONTENT,
                "storefront_platform": "shopify",
                "storefront_platform_source": "products_js_v1",
                # The page read the cart identity is resolved from (as in _seed_row_for_exec_spec).
                "variants": [dict(v) for v in variants],
            },
            "brand": "Rare Beauty",
            "variants": variants,
        },
        "status": "active",
        **_VERIFIED_DESTINATION,
    }


def _resolve(
    monkeypatch, client, seeds: List[Dict[str, Any]], *, product: Dict[str, Any], market: Optional[str] = "US",
    limit: int = 5,
) -> Dict[str, Any]:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query, values=None):
        if "FROM external_product_seeds" in str(query):
            return list(seeds)
        return []

    async def open_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")
    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", open_gate)
    monkeypatch.setattr(
        gateway, "get_allowed_domains_for_market",
        AsyncMock(return_value=[HOST, "beautyretailer.example"]),
    )
    payload: Dict[str, Any] = {"product": product, "limit": limit, "tool": "*"}
    if market:
        payload["market"] = market
    res = client.post(
        "/agent/shop/v1/invoke",
        json={"operation": "offers.resolve", "payload": payload, "metadata": {"source": "creator-agent-ui"}},
    )
    assert res.status_code == 200, res.text
    return res.json()


def _seed_offers(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [o for o in (body.get("offers") or []) if str(o.get("offer_id") or "").startswith("of:external_seed:")]


def _signed_dest(offer: Dict[str, Any]) -> str:
    tok = parse_qs(urlparse(offer["affiliate_url"]).query)["token"][0].split(".")[0]
    return json.loads(base64.urlsafe_b64decode(tok + "=" * ((4 - len(tok) % 4) % 4)))["dest"]


# ----------------------------------------------------------------- 1. one offer per seller


def test_a_five_shade_listing_is_one_offer_with_its_shades_listed(monkeypatch, client):
    body = _resolve(monkeypatch, client, [_seed()], product={"product_id": "sig_rare_blush"})
    (offer,) = _seed_offers(body)
    assert offer["variant_count"] == 5
    assert [v["title"] for v in offer["variants"]] == SHADES
    assert all(v["in_stock"] is True and v["currency"] == "USD" for v in offer["variants"])
    assert all("url" not in v and "affiliate_url" not in v for v in offer["variants"]), (
        "the option list mints no links; a link is minted only for a named variant")
    # The offer is the first in-stock shade. (Whether that shade gets a cart is the hand-over's
    # rule -- a stamped id builds one only for a single-variant product -- and is unchanged.)
    assert offer["source"]["variant_id"] == offer["variants"][0]["variant_id"]


def test_the_cart_is_the_shade_the_seed_is_attached_to(monkeypatch, client):
    shades = [_shade(i, n) for i, n in enumerate(SHADES)]
    attached = shades[3]["variant_id"]
    body = _resolve(
        monkeypatch, client, [_seed(variants=shades, attached_variant_id=attached)],
        product={"product_id": "sig_rare_blush"},
    )
    (offer,) = _seed_offers(body)
    assert offer["source"]["variant_id"] == attached


def test_a_sold_out_first_shade_does_not_take_the_cart(monkeypatch, client):
    shades = [_shade(0, "Bliss", availability="out_of_stock"), *[_shade(i, n) for i, n in enumerate(SHADES[1:], 1)]]
    body = _resolve(monkeypatch, client, [_seed(variants=shades)], product={"product_id": "sig_rare_blush"})
    (offer,) = _seed_offers(body)
    assert offer["source"]["variant_id"] == shades[1]["variant_id"]
    assert offer["variants"][0]["in_stock"] is False


def test_a_named_shade_gets_that_shade_and_no_option_list(monkeypatch, client):
    shades = [_shade(i, n) for i, n in enumerate(SHADES)]
    body = _resolve(
        monkeypatch, client, [_seed(variants=shades)], product={"sku_id": shades[2]["variant_id"]},
    )
    (offer,) = _seed_offers(body)
    assert offer["source"]["variant_id"] == shades[2]["variant_id"]
    assert "variants" not in offer and "variant_count" not in offer


def test_a_single_variant_listing_carries_no_option_list(monkeypatch, client):
    body = _resolve(
        monkeypatch, client, [_seed(variants=[_shade(0, "Bliss")])], product={"product_id": "sig_rare_blush"},
    )
    (offer,) = _seed_offers(body)
    assert "variants" not in offer


def test_two_sellers_are_two_offers(monkeypatch, client):
    body = _resolve(
        monkeypatch, client,
        [_seed(), _seed(seed_id="eps_retailer_blush", host="beautyretailer.example")],
        product={"product_id": "sig_rare_blush"},
    )
    hosts = sorted(o["execution_spec"]["merchant_domain"] for o in _seed_offers(body))
    assert hosts == ["beautyretailer.example", HOST]


# ----------------------------------------------------------------- 2. the buyer's currency


def test_a_declared_market_drops_foreign_currency_offers_before_the_cut(monkeypatch, client):
    gbp = _seed(
        seed_id="eps_uk_blush", host="beautyretailer.example", currency="GBP",
        variants=[_shade(i, n, currency="GBP") for i, n in enumerate(SHADES)],
    )
    # limit=1, GBP seed first: a filter after the cut would have left the buyer nothing.
    body = _resolve(monkeypatch, client, [gbp, _seed()], product={"product_id": "sig_rare_blush"}, limit=1)
    offers = _seed_offers(body)
    assert [o["currency"] for o in offers] == ["USD"]
    assert body["metadata"]["buyer_currency_filtered"] == {"GBP": 1}


def test_no_declared_market_filters_nothing(monkeypatch, client):
    gbp = _seed(
        seed_id="eps_uk_blush", host="beautyretailer.example", currency="GBP",
        variants=[_shade(i, n, currency="GBP") for i, n in enumerate(SHADES)],
    )
    body = _resolve(monkeypatch, client, [gbp, _seed()], product={"product_id": "sig_rare_blush"}, market=None)
    assert sorted(o["currency"] for o in _seed_offers(body)) == ["GBP", "USD"]
    assert "buyer_currency_filtered" not in body["metadata"]


def test_a_catalog_offer_in_another_currency_is_dropped_too(monkeypatch, client):
    """The final filter, for the lanes that do not filter at the source (catalog offers)."""
    import routes.agent_shop_gateway as gateway

    gbp = {
        **_CATALOG_OFFER_ROW, "offer_id": "of_uk_1", "currency": "GBP", "merchant_id": "uk_retailer",
        # Its own host: the catalog arm keeps one offer per host.
        "destination_url": "https://www.ukbeauty.example/product/cosrx-snail-essence",
        "merchant_name": "UK Beauty",
    }

    async def fake_fetch_all(query, values=None):
        q = str(query)
        if "FROM catalog_products" in q and "JOIN catalog_offers" in q:
            return [gbp, dict(_CATALOG_OFFER_ROW)]
        return []

    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")
    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=c"))
    res = client.post(
        "/agent/shop/v1/invoke",
        json={"operation": "offers.resolve",
              "payload": {"product": {"product_id": "sig_catalog_1"}, "limit": 10, "market": "US", "tool": "*"},
              "metadata": {"source": "creator-agent-ui"}},
    )
    assert res.status_code == 200
    body = res.json()
    assert [o["currency"] for o in body["offers"]] == ["USD"]
    assert body["metadata"]["buyer_currency_filtered"] == {"GBP": 1}


# ----------------------------------------------------------------- 3. the buyer's country on the cart


def test_a_declared_market_pins_the_cart_and_the_signed_link_alike(monkeypatch, client):
    # Single variant: the stamped id builds the cart (see `_external_seed_redirect_identity`).
    body = _resolve(
        monkeypatch, client, [_seed(variants=[_shade(0, "Bliss")])], product={"product_id": "sig_rare_blush"},
    )
    (offer,) = _seed_offers(body)
    cart = offer["execution_spec"]["cart_url"]
    assert cart.endswith("&country=US")
    assert cart == _signed_dest(offer), "the published cart must be the destination the token signs"
    assert "country=" not in (offer["execution_spec"]["pdp_url"] or ""), "a product page is not pinned"


def test_no_declared_market_pins_nothing(monkeypatch, client):
    body = _resolve(
        monkeypatch, client, [_seed(variants=[_shade(0, "Bliss")])], product={"product_id": "sig_rare_blush"},
        market=None,
    )
    offers = _seed_offers(body)
    assert offers and all(o["execution_spec"]["cart_url"] for o in offers), "the test needs a cart"
    for offer in offers:
        assert "country=" not in (offer["execution_spec"]["cart_url"] or "")
        assert "country=" not in _signed_dest(offer)


@pytest.mark.parametrize(
    "market, observed, expected",
    [
        ("US", True, "US"),
        ("sg", True, "SG"),
        ("US", False, None),  # a defaulted "US" is not a buyer's market
        ("US", None, None),
        ("*", True, None),  # a seed listed for every market names no country
        ("ZZ", True, None),  # not in the market registry
        ("", True, None),
    ],
)
def test_the_cart_country_pin_rule(market, observed, expected):
    from routes.agent_shop_gateway import _cart_country_pin

    assert _cart_country_pin(market, observed) == expected


def test_a_listing_for_every_market_is_pinned_to_the_buyer(monkeypatch, client):
    """A '*' row is served under the request's market; the pin is the BUYER's, not the row's."""
    row = _seed(variants=[_shade(0, "Bliss")])
    row["market"] = "*"
    body = _resolve(monkeypatch, client, [row], product={"product_id": "sig_rare_blush"})
    (offer,) = _seed_offers(body)
    assert offer["execution_spec"]["cart_url"].endswith("&country=US")
    assert offer["execution_spec"]["cart_url"] == _signed_dest(offer)


# ----------------------------------------------------------------- the minter, directly (review of #2622)


def _token_payload(link: str) -> Dict[str, Any]:
    tok = parse_qs(urlparse(link).query)["token"][0].split(".")[0]
    return json.loads(base64.urlsafe_b64decode(tok + "=" * ((4 - len(tok) % 4) % 4)))


async def _mint(*, market: str, observed: bool, cart_country: Optional[str] = None) -> str:
    import routes.agent_shop_gateway as gw

    return await gw._make_external_redirect_url(
        market=market,
        market_observed=observed,
        tool="*",
        destination_url="https://teststore.com/products/widget",
        utm_template=None,
        ctx={"seedId": "seed_cart"},
        allowed_domains=["teststore.com"],
        shop_domain="teststore.com",
        platform="shopify",
        cart_variant_id="44556677889900",
        cart_country=cart_country,
    )


@pytest.mark.asyncio
async def test_a_buyer_on_a_row_listed_elsewhere_is_pinned_to_the_buyer_not_the_row():
    """THE REVIEW REPRO: an SG buyer on a US-listed row. Pinning from the served market forced
    `country=US`; the pin is the buyer's, passed explicitly."""
    from services.outbound_links_service import request_market_observed

    observed = request_market_observed("SG", listing_market="US")
    unpinned = _token_payload(await _mint(market="US", observed=observed))
    assert "country=" not in unpinned["dest"], "a lane that names no buyer country pins nothing"
    assert "cart_country" not in unpinned

    pinned = _token_payload(await _mint(market="US", observed=observed, cart_country="SG"))
    assert pinned["dest"].endswith("&country=SG")
    assert pinned["cart_country"] == "SG"


@pytest.mark.asyncio
async def test_an_undeclared_market_pins_nothing_even_with_a_country():
    payload = _token_payload(await _mint(market="US", observed=False, cart_country="US"))
    assert "country=" not in payload["dest"] and "cart_country" not in payload


@pytest.mark.asyncio
async def test_the_card_rederive_applies_the_pin_the_token_records():
    import routes.agent_shop_gateway as gw

    link = await _mint(market="US", observed=True, cart_country="SG")
    attribution = gw._seed_attribution_from_redirect(
        link,
        destination_url="https://teststore.com/products/widget",
        utm_template=None,
        market="US",
        tool="*",
        shop_domain="teststore.com",
        platform="shopify",
        cart_variant_id="44556677889900",
    )
    assert attribution is not None, "the re-derive must reproduce the signed destination"
    assert attribution["cart_url"] == _token_payload(link)["dest"]
    assert attribution["cart_url"].endswith("&country=SG")


def test_host_diverse_head_fills_with_same_host_offers_after_one_per_host():
    """Coverage the fan-out test used to give: one per host first, then fill in rank order."""
    from routes.agent_shop_gateway import _host_diverse_head

    def offer(i: int, host: str) -> Dict[str, Any]:
        return {"offer_id": f"o{i}", "url": f"https://{host}/p", "source": {}}

    ranked = [offer(0, "a.com"), offer(1, "a.com"), offer(2, "a.com"), offer(3, "b.com")]
    assert [o["offer_id"] for o in _host_diverse_head(ranked, 3)] == ["o0", "o3", "o1"]


def test_the_attached_variant_is_read_from_seed_data_too(monkeypatch, client):
    """`_external_seed_redirect_identity` reads the attachment from the row OR seed_data; the
    offer's variant must follow the same read, or cart and token name different variants."""
    shades = [_shade(i, n) for i, n in enumerate(SHADES)]
    row = _seed(variants=shades)
    row["seed_data"]["attached_variant_id"] = shades[4]["variant_id"]
    body = _resolve(monkeypatch, client, [row], product={"product_id": "sig_rare_blush"})
    (offer,) = _seed_offers(body)
    assert offer["source"]["variant_id"] == shades[4]["variant_id"]


def test_a_long_shade_range_is_capped_and_counted_whole(monkeypatch, client):
    from routes.agent_shop_gateway import _MAX_VARIANT_OPTIONS

    shades = [_shade(i, f"Shade {i}") for i in range(_MAX_VARIANT_OPTIONS + 5)]
    body = _resolve(monkeypatch, client, [_seed(variants=shades)], product={"product_id": "sig_rare_blush"})
    (offer,) = _seed_offers(body)
    assert len(offer["variants"]) == _MAX_VARIANT_OPTIONS
    assert offer["variant_count"] == _MAX_VARIANT_OPTIONS + 5


def test_a_named_variant_says_whether_it_matched_a_stored_variant(monkeypatch, client):
    """`source.variant_match`: exact when sku_id is one of the listing's variants, fallback when the offer is
    the listing at its row price for a variant it does not have (the gateway keeps only exact)."""
    shades = [_shade(i, n) for i, n in enumerate(SHADES)]
    body = _resolve(monkeypatch, client, [_seed(variants=shades)], product={"sku_id": shades[1]["variant_id"]})
    (offer,) = _seed_offers(body)
    assert offer["source"]["variant_match"] == "exact"
    body = _resolve(monkeypatch, client, [_seed(variants=shades)], product={"sku_id": "999999999999"})
    (offer,) = _seed_offers(body)
    assert offer["source"]["variant_match"] == "fallback"
    body = _resolve(monkeypatch, client, [_seed(variants=shades)], product={"product_id": "sig_rare_blush"})
    (offer,) = _seed_offers(body)
    assert "variant_match" not in offer["source"]
