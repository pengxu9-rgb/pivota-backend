"""A lone surrogate in a field the external-seed product builder COPIES onto the product must not fail the
response.

seed_data is jsonb, which cannot hold a raw lone surrogate, but a snapshot stored as JSON TEXT can hold the
escape (what JS writes for a split emoji). `_build_external_seed_product` parses that text and copies
title, brand, vendor, product_type, category, the canonical / destination URL, image_url and
external_product_id from it onto the product. A raw lone surrogate there made the JSON response fail to
ENCODE after the route returned: a 500 for the whole search page (and the product detail), past every
handler and fallback. The search and detail routes now replace lone surrogates in every string of the
products they return (services/seed_data_egress_projection.serve_search_products / serve_detail_product).

The URLs and the id also go into the redirect token, which is UTF-8 encoded INSIDE the builder, before
any response boundary: a raw lone surrogate there raised and the product was silently dropped (a missing
search hit, a 404 detail). The builder now cleans a text snapshot as it parses it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app
import services.seed_data_egress_projection as egress
from services.outbound_links_service import parse_redirect_token_verified
from services.seed_data_egress_projection import (
    replace_lone_surrogates,
    serve_detail_product,
    serve_search_products,
)
from tests.test_agent_product_detail_seed_data_egress import _seed_row

REPLACEMENT = "�"

_SURROGATE_SNAPSHOT = {
    "title": "Night \ud83d Serum",
    "brand": "Brand \udc00",
    "vendor": "Vendor \ud83d",
    "product_type": "Serum \ud83d",
    "category": "Skincare \udc00",
    "image_url": "https://example.com/\ud83d.jpg",
}


_LINK_SNAPSHOT = {
    "title": "Night Serum",
    "category": "Skincare",
    "canonical_url": "https://example.com/p/night-\ud83d-serum",
    "destination_url": "https://example.com/p/night-\ud83d-serum?ref=\udc00",
    "external_product_id": "ext_\ud83d_1",
}


def _row(snapshot: Dict[str, Any] = _SURROGATE_SNAPSHOT) -> Dict[str, Any]:
    # No title / brand / ... on seed_data itself, so the builder takes them from the text snapshot.
    row = _seed_row({"snapshot": json.dumps(snapshot)})
    row["title"] = None
    return row


def _link_row() -> Dict[str, Any]:
    # No URL or id on the row or seed_data: the builder takes them from the text snapshot, and they go
    # into the redirect token.
    row = _row(_LINK_SNAPSHOT)
    row["destination_url"] = None
    row["canonical_url"] = None
    row["external_product_id"] = None
    return row



@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _patched(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module
    import services.agent_search_gateway_proxy as proxy

    monkeypatch.delenv(proxy.FLAG, raising=False)
    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", False)
    # The tests named "..._link_fields_..." serve the row whose URLs and id come from the text snapshot.
    row = _link_row() if "_link_fields_" in request.node.name else _row()

    async def fake_fetch_all(query: str, values=None):
        return [row] if "FROM external_product_seeds" in str(query) else []

    async def fake_fetch_one(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return row
        if "COUNT" in str(query):
            return {"total": 0}
        return None

    for module in (agent_api_module, agent_sdk_fixed_module):
        monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
        monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(module, "is_destination_domain_allowed", lambda *_a, **_k: True)
        monkeypatch.setattr(
            module, "should_block_external_referral_runtime", AsyncMock(return_value=(False, None))
        )


_SEARCH_URLS = [
    "/agent/v1/products/search?query=&search_all_merchants=true&limit=20&offset=0&in_stock_only=false",
    "/agent/v1/products/search?merchant_id=external_seed&query=&limit=20&offset=0&in_stock_only=false",
    "/agent/v1/products/search?query=example.com&limit=20&offset=0&in_stock_only=false",
    "/agent/v1/beauty/products/search?query=example.com&limit=20&offset=0&in_stock_only=false",
]


@pytest.mark.parametrize("url", _SEARCH_URLS)
def test_search_renders_a_product_whose_copied_fields_carry_a_lone_surrogate(client: TestClient, url: str) -> None:
    res = client.get(url, headers={"X-API-Key": "test-api-key"})
    assert res.status_code == 200, res.text[:300]
    products: List[Dict[str, Any]] = [p for p in res.json()["products"] if p.get("merchant_id") == "external_seed"]
    assert products and products[0]["title"] == f"Night {REPLACEMENT} Serum"


def test_detail_renders_a_product_whose_copied_fields_carry_a_lone_surrogate(client: TestClient) -> None:
    res = client.get("/agent/v1/products/external_seed/ext_test_1", headers={"X-API-Key": "test-api-key"})
    assert res.status_code == 200, res.text[:300]
    product = res.json()["product"]
    assert product["title"] == f"Night {REPLACEMENT} Serum"
    assert product["brand"] == f"Brand {REPLACEMENT}"
    assert product["vendor"] == f"Vendor {REPLACEMENT}"
    assert product["product_type"] == f"Serum {REPLACEMENT}"
    assert product["category"] == f"Skincare {REPLACEMENT}"
    assert product["image_url"] == f"https://example.com/{REPLACEMENT}.jpg"


# --- URLs and the id: the redirect token -----------------------------------------------------------------


def _token_dest(product: Dict[str, Any]) -> str:
    token = product["external_redirect_url"].split("token=", 1)[1]
    payload, _expired = parse_redirect_token_verified(token)
    return payload["dest"]


# The browse URLs: this row has no URL columns, so the query URLs' text match does not select it. The
# token is built by the same builder on every search path.
_LINK_SEARCH_URLS = [u for u in _SEARCH_URLS if "query=&" in u]
_DEST = f"https://example.com/p/night-{REPLACEMENT}-serum?ref=%EF%BF%BD"  # the query is re-encoded


@pytest.mark.parametrize("url", _LINK_SEARCH_URLS)
def test_search_keeps_a_product_whose_link_fields_carry_a_lone_surrogate(client: TestClient, url: str) -> None:
    res = client.get(url, headers={"X-API-Key": "test-api-key"})
    assert res.status_code == 200, res.text[:300]
    products = [p for p in res.json()["products"] if p.get("merchant_id") == "external_seed"]
    assert products, "the product was dropped: its redirect token could not be encoded"
    assert products[0]["canonical_url"] == f"https://example.com/p/night-{REPLACEMENT}-serum"
    assert _token_dest(products[0]).startswith(_DEST)


def test_detail_serves_a_product_whose_link_fields_carry_a_lone_surrogate(client: TestClient) -> None:
    res = client.get("/agent/v1/products/external_seed/ext_test_1", headers={"X-API-Key": "test-api-key"})
    assert res.status_code == 200, res.text[:300]
    product = res.json()["product"]
    assert product["canonical_url"] == f"https://example.com/p/night-{REPLACEMENT}-serum"
    assert _token_dest(product).startswith(_DEST)


# --- the helpers -----------------------------------------------------------------------------------------


def test_replace_lone_surrogates_is_copy_on_write() -> None:
    clean = {"title": "ok", "variants": [{"title": "30ml"}], "tags": ["a"], "n": 1}
    assert replace_lone_surrogates(clean) is clean  # nothing to replace: the same object

    dirty = {"title": "ok", "variants": [{"title": "30ml \ud83d"}, {"title": "50ml"}], "tags": ["a"], "k\udc00": 2}
    out = replace_lone_surrogates(dirty)
    assert out == {
        "title": "ok",
        "variants": [{"title": f"30ml {REPLACEMENT}"}, {"title": "50ml"}],
        "tags": ["a"],
        f"k{REPLACEMENT}": 2,
    }
    assert dirty["variants"][0]["title"] == "30ml \ud83d"  # the input is untouched
    assert out["tags"] is dirty["tags"]  # untouched subtrees are shared, not copied
    assert out["variants"][1] is dirty["variants"][1]
    assert list(out) == ["title", "variants", "tags", f"k{REPLACEMENT}"]  # key order kept
    json.dumps(out, ensure_ascii=False).encode("utf-8")  # encodes


def test_replace_lone_surrogates_keeps_the_clean_items_before_the_first_dirty_one() -> None:
    first = {"title": "15ml"}
    items = [first, "ok", {"title": "30ml \ud83d"}, "50ml \udc00", "70ml"]
    out = replace_lone_surrogates(items)
    assert out == [first, "ok", {"title": f"30ml {REPLACEMENT}"}, f"50ml {REPLACEMENT}", "70ml"]
    assert out[0] is first and items[2] == {"title": "30ml \ud83d"}

    nested = {"a": first, "b": 1, "c": "x \ud83d", "d": ["y"]}
    out = replace_lone_surrogates(nested)
    assert out == {"a": first, "b": 1, "c": f"x {REPLACEMENT}", "d": ["y"]}
    assert list(out) == ["a", "b", "c", "d"] and out["a"] is first and out["d"] is nested["d"]


def test_search_serving_does_not_walk_the_stripped_seed_data_again(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Any] = []
    real = egress._safe_tree

    def spy(value: Any, depth: int) -> Any:
        seen.append(value)
        return real(value, depth)

    monkeypatch.setattr(egress, "_safe_tree", spy)
    product = {"title": "t \ud83d", "seed_data": {"note": "seed only \ud83d", "price_read": {"x": 1}}, "n": 1}
    out = serve_search_products([product])[0]
    assert out == {"title": f"t {REPLACEMENT}", "seed_data": {"note": f"seed only {REPLACEMENT}"}, "n": 1}
    assert not any(isinstance(v, (dict, str)) and "seed only" in str(v) for v in seen), "seed_data walked twice"
    assert seen, "the rest of the product is walked"


def test_both_routes_replace_lone_surrogates_outside_seed_data_as_a_backstop() -> None:
    # The builder cleans what it copies from a text snapshot; any other producer of a product field is
    # still covered at the response.
    product = {"title": "t \ud83d", "variants": [{"title": "ok"}, {"title": "v \udc00"}]}
    for served in (serve_search_products([product])[0], serve_detail_product(product)):
        assert served == {"title": f"t {REPLACEMENT}", "variants": [{"title": "ok"}, {"title": f"v {REPLACEMENT}"}]}
