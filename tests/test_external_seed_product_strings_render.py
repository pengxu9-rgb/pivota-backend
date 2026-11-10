"""A lone surrogate in a field the external-seed product builder COPIES onto the product must not fail the
response.

seed_data is jsonb, which cannot hold a raw lone surrogate, but a snapshot stored as JSON TEXT can hold the
escape (what JS writes for a split emoji). `_build_external_seed_product` parses that text and copies
title, brand, vendor, product_type, category, the canonical / destination URL, image_url and
external_product_id from it onto the product. A raw lone surrogate there made the JSON response fail to
ENCODE after the route returned: a 500 for the whole search page (and the product detail), past every
handler and fallback. The search and detail routes now replace lone surrogates in every string of the
products they return (services/seed_data_egress_projection.serve_search_products / serve_detail_product).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app
from services.seed_data_egress_projection import serve_product_strings
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


def _row() -> Dict[str, Any]:
    # No title / brand / ... on seed_data itself, so the builder takes them from the text snapshot.
    row = _seed_row({"snapshot": json.dumps(_SURROGATE_SNAPSHOT)})
    row["title"] = None
    return row


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _patched(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module
    import services.agent_search_gateway_proxy as proxy

    monkeypatch.delenv(proxy.FLAG, raising=False)
    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", False)
    row = _row()

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


# --- the helper ------------------------------------------------------------------------------------------


def test_serve_product_strings_is_copy_on_write() -> None:
    clean = {"title": "ok", "variants": [{"title": "30ml"}], "tags": ["a"], "n": 1}
    assert serve_product_strings(clean) is clean  # nothing to replace: the same object

    dirty = {"title": "ok", "variants": [{"title": "30ml \ud83d"}, {"title": "50ml"}], "tags": ["a"], "k\udc00": 2}
    out = serve_product_strings(dirty)
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
