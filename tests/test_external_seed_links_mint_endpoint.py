"""POST /agent/shop/v1/attribution/external-seed-links — the mint the gateway calls for cards it built.

The gateway's search stack builds external-seed cards in JS straight from Postgres; they reach
agents with a raw merchant URL and no `/r` link. The gateway holds no signing secret, so it asks
this backend to mint. These tests pin that a minted entry is byte-for-byte as attributed as a
card this backend builds itself: one click id across the signed token, `destination_url`,
`cart_url` and `tracking`; a candidate we cannot mint is absent, not wrong; and the route is
credentialed.
"""

from __future__ import annotations

from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import routes.agent_shop_gateway as gw
from main import app
from routes.agent_auth import AgentContext, get_agent_context
from services.commerce_attribution_service import PVT_CLICK_ID
from services.outbound_links_service import (
    REFERRAL_CLICK_PARAM,
    SHOPIFY_CART_CLICK_ATTRIBUTE,
    parse_and_verify_redirect_token,
)

PATH = "/agent/shop/v1/attribution/external-seed-links"


def _token_payload(redirect_url: str) -> Dict[str, Any]:
    token = parse_qs(urlparse(redirect_url).query)["token"][0]
    return parse_and_verify_redirect_token(token)


def _query(url: str) -> Dict[str, list]:
    return parse_qs(urlparse(url).query)


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch: pytest.MonkeyPatch):
    async def allowed(*, market: str):
        return ["example.com", "teststore.com"]

    monkeypatch.setattr(gw, "get_allowed_domains_for_market", allowed)


@pytest.fixture()
def client():
    async def fake_context() -> AgentContext:
        req = Request(
            {
                "type": "http",
                "method": "POST",
                "path": PATH,
                "headers": [],
                "query_string": b"",
                "client": ("127.0.0.1", 0),
                "scheme": "https",
                "server": ("api.pivota.cc", 443),
            }
        )
        return AgentContext({"agent_id": "agent_gateway", "agent_name": "Gateway", "allowed_merchants": None}, req)

    app.dependency_overrides[get_agent_context] = fake_context
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


_SHOPIFY_SNAPSHOT = {
    "snapshot": {
        "storefront_platform": "shopify",
        "variants": [{"shopify_variant_id": "46123456789", "title": "Default"}],
    }
}


def _strip(url: str) -> Dict[str, Any]:
    q = _query(url)
    q.pop(REFERRAL_CLICK_PARAM, None)
    q.pop("utm_content", None)
    q.pop(SHOPIFY_CART_CLICK_ATTRIBUTE, None)
    return {"path": urlparse(url).path, "host": urlparse(url).netloc, "q": q}


def _candidate(**over: Any) -> Dict[str, Any]:
    base = {
        "external_seed_id": "seed_1",
        "external_product_id": "ext_1",
        "destination_url": "https://example.com/products/serum",
        "canonical_url": "https://example.com/products/serum",
        "market": "US",
        "tool": "*",
    }
    base.update(over)
    return base


# --- what a minted entry carries ------------------------------------------------------------------


def test_minted_entry_shares_one_click_id_across_token_url_and_tracking(client: TestClient):
    res = client.post(PATH, json={"market": "US", "tool": "find_products_multi", "candidates": [_candidate()]})
    assert res.status_code == 200, res.text
    links = res.json()["links"]
    assert len(links) == 1
    link = links[0]
    assert link["external_seed_id"] == "seed_1"
    assert link["external_product_id"] == "ext_1"

    payload = _token_payload(link["external_redirect_url"])
    click_id = payload["ctx"][PVT_CLICK_ID]
    assert click_id.startswith("clk_")
    assert payload["ctx"]["seedId"] == "seed_1"

    q = _query(link["destination_url"])
    assert q[REFERRAL_CLICK_PARAM] == [click_id]
    assert q["utm_content"] == [click_id]
    assert q["utm_source"] == ["pivota"]
    assert urlparse(link["destination_url"]).netloc == "example.com"
    assert link["destination_url"] == payload["dest"]  # exactly what the redirect resolves to
    assert link["cart_url"] is None
    assert link["tracking"] == {"click_id": click_id, "param": REFERRAL_CLICK_PARAM, "join_mode": "referral_only"}


def test_minted_entry_is_identical_in_shape_to_a_card_this_backend_builds(client: TestClient):
    """Same mint, same stamp: a gateway-built card must not be attributed differently from one
    `_build_prefetched_external_seed_wrappers` builds from the same seed."""
    import asyncio

    # WITH the seed's snapshot: a standalone Shopify seed on a custom domain can only build a
    # cart permalink from `seed_data.snapshot`, so parity is only meaningful when it is present.
    candidate = _candidate(
        destination_url="https://teststore.com/products/widget",
        canonical_url="https://teststore.com/products/widget",
        seed_data=_SHOPIFY_SNAPSHOT,
    )
    res = client.post(PATH, json={"candidates": [candidate]})
    link = res.json()["links"][0]
    wrappers = asyncio.run(gw._build_prefetched_external_seed_wrappers({"external_seed_candidates": [candidate]}))
    product = wrappers[0]["product"]
    assert link["cart_url"] is not None and product["cart_url"] is not None
    assert _strip(link["cart_url"]) == _strip(product["cart_url"])

    assert _strip(link["destination_url"]) == _strip(product["destination_url"])
    assert link["tracking"]["param"] == product["tracking"]["param"]
    assert link["tracking"]["join_mode"] == product["tracking"]["join_mode"]


def test_shopify_cart_join_when_the_seed_identity_justifies_one(client: TestClient):
    res = client.post(
        PATH,
        json={
            "candidates": [
                _candidate(
                    external_seed_id="seed_cart",
                    destination_url="https://teststore.com/products/widget",
                    canonical_url="https://teststore.com/products/widget",
                    domain="teststore.com",
                    attached_product_key="prod::merch_x::shopify::999",
                    attached_variant_id="46123456789",
                )
            ]
        },
    )
    assert res.status_code == 200, res.text
    link = res.json()["links"][0]
    click_id = _token_payload(link["external_redirect_url"])["ctx"][PVT_CLICK_ID]
    assert link["cart_url"] is not None
    assert link["cart_url"].startswith("https://teststore.com/cart/46123456789:1?")
    assert _query(link["cart_url"])[SHOPIFY_CART_CLICK_ATTRIBUTE] == [click_id]
    assert urlparse(link["destination_url"]).path == "/products/widget"
    assert _query(link["destination_url"])[REFERRAL_CLICK_PARAM] == [click_id]
    assert link["tracking"] == {"click_id": click_id, "param": SHOPIFY_CART_CLICK_ATTRIBUTE, "join_mode": "cart_permalink"}


# --- what is absent, and why ----------------------------------------------------------------------


def test_a_candidate_off_the_allowlist_is_absent_not_wrong(client: TestClient):
    res = client.post(
        PATH,
        json={
            "candidates": [
                _candidate(external_seed_id="seed_ok"),
                _candidate(external_seed_id="seed_no", destination_url="https://not-allowed.example/p/1", canonical_url=None),
            ]
        },
    )
    assert res.status_code == 200, res.text
    ids = [l["external_seed_id"] for l in res.json()["links"]]
    assert ids == ["seed_ok"]


def test_a_candidate_with_an_unusable_url_is_absent(client: TestClient):
    # Held by the mint itself (`_make_external_redirect_url` answers None for a non-http URL,
    # and the allowlist refuses an empty host), not by a check in this endpoint.
    res = client.post(PATH, json={"candidates": [_candidate(destination_url="javascript:alert(1)")]})
    assert res.status_code == 200, res.text
    assert res.json()["links"] == []


def test_two_cards_on_one_destination_get_two_links_each_naming_its_own_seed(client: TestClient):
    # The signed token carries per-seed context. Review of the first cut caught a
    # destination-keyed cache handing card b a token whose ctx named seed a — clicks from b
    # would have been logged under a. One mint per candidate, and the ctx proves it.
    res = client.post(
        PATH,
        json={
            "candidates": [
                _candidate(external_seed_id="a", attached_product_key="prod::merch_A::woocommerce::1"),
                _candidate(external_seed_id="b", attached_product_key="prod::merch_B::woocommerce::2"),
            ]
        },
    )
    links = {l["external_seed_id"]: l for l in res.json()["links"]}
    assert set(links) == {"a", "b"}
    assert links["a"]["external_redirect_url"] != links["b"]["external_redirect_url"]
    for seed_id, link in links.items():
        ctx = _token_payload(link["external_redirect_url"])["ctx"]
        assert ctx["seedId"] == seed_id
        assert ctx["merchant_id"] == ("merch_A" if seed_id == "a" else "merch_B")
    assert links["a"]["tracking"]["click_id"] != links["b"]["tracking"]["click_id"]


def test_same_destination_two_shop_domains_never_share_a_cart(client: TestClient):
    # The worst shape from review: same product page, two shop domains with a numeric
    # variant. Each card's signed destination must be a cart on ITS OWN domain.
    base = dict(
        destination_url="https://example.com/products/widget",
        canonical_url="https://example.com/products/widget",
        attached_product_key="prod::merch_x::shopify::999",
        attached_variant_id="46123456789",
    )
    res = client.post(
        PATH,
        json={
            "candidates": [
                _candidate(external_seed_id="shop_a", domain="teststore.com", **base),
                _candidate(external_seed_id="shop_b", domain="example.com", **base),
            ]
        },
    )
    links = {l["external_seed_id"]: l for l in res.json()["links"]}
    assert set(links) == {"shop_a", "shop_b"}
    assert _token_payload(links["shop_a"]["external_redirect_url"])["dest"].startswith("https://teststore.com/cart/")
    assert _token_payload(links["shop_b"]["external_redirect_url"])["dest"].startswith("https://example.com/cart/")
    for link in links.values():
        assert link["tracking"] is not None


def test_snapshot_evidence_builds_the_cart_join_for_a_standalone_seed(client: TestClient):
    # No attached key, custom domain: the ONLY route to a Shopify cart permalink is the
    # seed's snapshot (storefront_platform + one stamped variant). Dropping `seed_data`
    # silently turns this into a referral — the join this endpoint exists to recover.
    res = client.post(
        PATH,
        json={
            "candidates": [
                _candidate(
                    external_seed_id="standalone",
                    destination_url="https://teststore.com/products/widget",
                    canonical_url="https://teststore.com/products/widget",
                    seed_data=_SHOPIFY_SNAPSHOT,
                )
            ]
        },
    )
    assert res.status_code == 200, res.text
    link = res.json()["links"][0]
    assert link["cart_url"] is not None and link["cart_url"].startswith("https://teststore.com/cart/46123456789:1?")
    assert link["tracking"]["join_mode"] == "cart_permalink"


def test_a_multi_variant_snapshot_refuses_the_cart_join(client: TestClient):
    two = {"snapshot": {"storefront_platform": "shopify", "variants": [
        {"shopify_variant_id": "1", "title": "S"}, {"shopify_variant_id": "2", "title": "M"}]}}
    res = client.post(PATH, json={"candidates": [_candidate(
        destination_url="https://teststore.com/products/widget", canonical_url=None, seed_data=two)]})
    link = res.json()["links"][0]
    assert link["cart_url"] is None
    assert link["tracking"]["join_mode"] == "referral_only"


def test_same_destination_with_and_without_a_cart_variant_are_different_links(client: TestClient):
    # The per-request mint cache must key on the cart variant too: two candidates on one product
    # page, one of which can build a cart permalink, are two different destinations, and reusing
    # the first link for the second would hand the cart-less card a cart it cannot justify (or
    # vice versa).
    base = dict(
        destination_url="https://teststore.com/products/widget",
        canonical_url="https://teststore.com/products/widget",
        domain="teststore.com",
        attached_product_key="prod::merch_x::shopify::999",
    )
    res = client.post(
        PATH,
        json={
            "candidates": [
                _candidate(external_seed_id="with_cart", attached_variant_id="46123456789", **base),
                _candidate(external_seed_id="no_cart", **base),
            ]
        },
    )
    assert res.status_code == 200, res.text
    by_id = {l["external_seed_id"]: l for l in res.json()["links"]}
    assert set(by_id) == {"with_cart", "no_cart"}
    assert by_id["with_cart"]["cart_url"] is not None
    assert by_id["no_cart"]["cart_url"] is None
    assert by_id["with_cart"]["external_redirect_url"] != by_id["no_cart"]["external_redirect_url"]
    assert _token_payload(by_id["no_cart"]["external_redirect_url"])["dest"].startswith(
        "https://teststore.com/products/widget?"
    )


def test_market_and_tool_default_from_the_body(client: TestClient):
    res = client.post(
        PATH,
        json={"market": "GB", "tool": "find_products_multi", "candidates": [_candidate(market=None, tool=None)]},
    )
    link = res.json()["links"][0]
    payload = _token_payload(link["external_redirect_url"])
    assert payload["market"] == "GB"  # not the hardcoded fallback, so the body default is proven
    assert payload["tool"] == "find_products_multi"
    assert _query(link["destination_url"])["utm_medium"] == ["find_products_multi"]


# --- the request contract -------------------------------------------------------------------------


def test_more_than_fifty_candidates_is_refused(client: TestClient):
    res = client.post(PATH, json={"candidates": [_candidate(external_seed_id=f"s{i}") for i in range(51)]})
    # The app maps request-validation errors to 400 (see main.py's exception handlers).
    assert res.status_code == 400


def test_a_candidate_without_a_seed_id_is_refused(client: TestClient):
    res = client.post(PATH, json={"candidates": [{"destination_url": "https://example.com/p"}]})
    assert res.status_code == 400


def test_unknown_fields_are_ignored_not_refused(client: TestClient):
    res = client.post(PATH, json={"candidates": [_candidate(title="Serum", price=12, seed_data={"x": 1})]})
    assert res.status_code == 200, res.text
    assert len(res.json()["links"]) == 1


def test_anonymous_callers_are_refused():
    # No dependency override here: the real auth dependency runs, and a request with no
    # credential at all must be refused before any minting happens.
    res = TestClient(app).post(PATH, json={"candidates": [_candidate()]})
    assert res.status_code in (401, 403), res.text
