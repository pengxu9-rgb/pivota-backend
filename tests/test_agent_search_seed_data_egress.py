"""The local search lanes serve an external seed's seed_data without its internal state, and nothing else
changes.

GET /agent/v1/products/search (agent_sdk_fixed, which delegates to agent_api.agent_search_products) and
GET /agent/v1/beauty/products/search (agent_api) served external-seed products with the stored seed_data
attached whole. Each of agent_search_products' three returns (fast mode, browse standard, search
standard) now strips the internal-state keys at every depth (services/seed_data_egress_projection.py,
DENYLIST). EVERYTHING ELSE IS KEPT AS STORED, URLs included (the retinoid net reads them as text): the gateway gates on this
response before its own egress projection (category paths, hold contracts, every variant string, the
retinoid safety text), so dropping anything else could turn a gated product into a served one. The
gateway's own gates are run on full vs stripped products by scripts/ops/seed_data_gateway_gate_equivalence.py
(tests/test_seed_data_gateway_gate_equivalence.py).

All of these run with the gateway search proxy OFF (its default), which is when every caller is served by
the local lane.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from main import app
from tests.test_agent_product_detail_seed_data_egress import _seed_row

# Stated here independently of the module, so a key silently removed from the module's list fails.
_INTERNAL_KEYS = {
    "price_read",
    "price_read_source",
    "price_currency_source",
    "price_currency_proof",
    "price_writeback",
    "variant_refresh",
    "variant_refresh_status",
    "variant_refresh_last_write",
    "variant_census",
    "canonical_reset",
    "read_provenance",
    "read_source",
    "refresh_status",
    "refreshed_at",
    "extracted_at",
    "fetched_at",
    "description_source",
    "description_refused_source",
    "image_urls_source",
    "storefront_platform_source",
    "review_summary",
    "review_status",
    "audit",
    "audit_quarantine",
    "manual_overrides",
    "utm_template",
    "seller_ref",
    "shopify_cart_proof",
    "shopify_cart_variant_proofs",
    "variant_id_provenance",
    "url_source",
    "pdp_matcher",
}
# Values that exist only under internal keys: none may appear anywhere in a response's seed_data.
_INTERNAL_MARKERS = (
    "INTERNAL-price-read",
    "INTERNAL-refresh",
    "INTERNAL-provenance",
    "INTERNAL-audit",
    "INTERNAL-review",
    "INTERNAL-override",
    "INTERNAL-seller",
    "INTERNAL-purchase",
    "INTERNAL-cart-proof",
    "utm_medium={tool}",
    "pvt_click_id",
)


def _search_seed_data() -> Dict[str, Any]:
    """A refreshed row's blob: what the gateway's gates read, next to internal state at every depth."""
    return {
        # --- product state the gateway gates on (be#2727 review P1-1 / P1-2 / P2-1) ---
        "title": "Night Renewal Serum",
        "brand": "Example Brand",
        "category": "Skincare",
        "product_type": "Serum",
        "category_path": "beauty/skincare/treat/serum",
        "catalog_category_path": "beauty/skincare/treat/serum",
        "description": "A night serum.",
        "inci_list": "Aqua, Glycerin, Retinol",
        "key_ingredients": [{"name": "Retinal"}],
        "tags": ["night", "tretinoin"],
        "image_url": "https://example.com/img_0.jpg",
        "image_urls": ["https://example.com/img_1.jpg"],
        "price_amount": 12.34,
        "price_currency": "USD",
        "source_unavailable_v1": {"status": "available", "contract_version": "external_seed.source_unavailable.v1"},
        "commerce_facts_v1": {"spf_rating": "50", "texture": "gel"},
        "destination_url": "https://user:pw@shop.example/products/night-serum?utm_source=pivota&aff=PUB1#x",
        "snapshot": {
            "description": "A light night serum.",
            "category_path": "beauty/skincare/treat/serum",
            "inci_list": "Aqua, Retinyl Palmitate",
            "tags": ["Adapalene"],
            "canonical_url": "HTTPS://shop.example:8443/p/night-serum?variant=1",
            "transaction_readiness_blocker_v1": {"status": "clear"},
            "variants": [
                {
                    "variant_id": "v1",
                    "title": "Retinol 0.5% / 30ml",
                    "visible_option_labels": ["Retinol 0.5%"],
                    "options": [["Strength", "0.5%"]],
                    "price_amount": 12.34,
                    "price_read": {"status": "INTERNAL-price-read"},
                }
            ],
            # internal
            "price_read_source": "INTERNAL-price-read",
            "read_source": "INTERNAL-provenance",
            "refreshed_at": "INTERNAL-refresh",
            "extracted_at": "INTERNAL-refresh",
            "variant_refresh": {"status": "INTERNAL-refresh"},
            "variant_refresh_last_write": {"at": "INTERNAL-refresh"},
            "description_refused_source": "INTERNAL-provenance",
            "price_writeback": {"purchase_id": "INTERNAL-purchase"},
            "shopify_cart_proof": {"checked_at": "INTERNAL-cart-proof"},
            "shopify_cart_variant_proofs": [{"live": "INTERNAL-cart-proof"}],
        },
        "variants": [
            {
                "variant_id": "v1",
                "title": "50ml",
                "price_amount": 12.34,
                "price_currency": "USD",
                "availability": "in_stock",
                "url": "https://shop.example/products/night-serum?variant=1",
                "shade": "Clear",
                "variant_refresh_status": "INTERNAL-refresh",
                "variant_id_provenance": "INTERNAL-provenance",
                "url_source": "INTERNAL-provenance",
            },
            {"variant_id": "v2", "title": "100ml", "price_amount": 19.99, "price_currency": "USD"},
        ],
        "ingredient_intel": {
            "actives": [{"name": "Hydroxypinacolone Retinoate", "audit": {"by": "INTERNAL-audit"}}],
            "review_status": "INTERNAL-review",
        },
        # --- internal ---
        "price_read": {"amount": 12.34, "status": "INTERNAL-price-read"},
        "price_read_source": "INTERNAL-price-read",
        "variant_refresh_status": "INTERNAL-refresh",
        "canonical_reset": {"reason": "INTERNAL-refresh"},
        "read_provenance": {"via": "INTERNAL-provenance"},
        "refresh_status": "INTERNAL-refresh",
        "review_summary": {"rating": 4.5, "status": "INTERNAL-review"},
        "audit": {"result": "INTERNAL-audit"},
        "audit_quarantine": {"reason": "INTERNAL-audit"},
        "manual_overrides": {"price": "INTERNAL-override"},
        "utm_template": "utm_source=pivota&utm_medium={tool}",
        "seller_ref": "INTERNAL-seller",
        "description_source": "INTERNAL-provenance",
        "image_urls_source": "INTERNAL-provenance",
        "pdp_matcher": {"decision": "INTERNAL-provenance"},
    }


def _leaves(value: Any, path: Tuple[Any, ...] = ()) -> Iterator[Tuple[Tuple[Any, ...], Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, path + (index,))
    else:
        yield path, value


def _at(value: Any, path: Tuple[Any, ...]) -> Any:
    for step in path:
        value = value[step]
    return value


def _assert_stripped_from(full: Dict[str, Any], served: Dict[str, Any]) -> None:
    """served == full minus the internal keys, everything else (URLs included) unchanged: leaf by leaf."""
    served_wire = json.dumps(served)
    for marker in _INTERNAL_MARKERS:
        assert marker not in served_wire, marker
    for path, _ in _leaves(served):
        assert not (set(k for k in path if isinstance(k, str)) & _INTERNAL_KEYS), path
    for path, value in _leaves(full):
        keys = [k for k in path if isinstance(k, str)]
        if set(keys) & _INTERNAL_KEYS:
            continue
        assert _at(served, path) == value, path


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _proxy_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.agent_search_gateway_proxy as proxy

    monkeypatch.delenv(proxy.FLAG, raising=False)


def _patch_search(
    monkeypatch: pytest.MonkeyPatch, rows: List[Dict[str, Any]], *, cache: bool = False
) -> Dict[str, Any]:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", cache)
    calls: Dict[str, Any] = {"seed_fetches": 0, "paths": []}

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            calls["seed_fetches"] += 1
            return rows
        return []

    async def fake_fetch_one(query: str, values=None):
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

    def spy_record(*args, **kwargs):
        calls["paths"].append(kwargs.get("path"))

    monkeypatch.setattr(agent_api_module, "record_catalog_search", spy_record)
    return calls


def _external_products(res) -> List[Dict[str, Any]]:
    assert res.status_code == 200, res.text
    products = res.json().get("products") or []
    external = [p for p in products if p.get("merchant_id") == "external_seed"]
    assert external, products
    return external


# One URL per return of agent_search_products. The path each lands on is asserted, so a test cannot
# silently drift onto another return.
_CASES = [
    pytest.param(
        "/agent/v1/products/search?query=&search_all_merchants=true&limit=20&offset=0&in_stock_only=false",
        "cross_merchant_search_fast_mode",
        id="v1-fast-mode",
    ),
    pytest.param(
        "/agent/v1/products/search?merchant_id=external_seed&query=&limit=20&offset=0&in_stock_only=false",
        "cross_merchant_browse_standard",
        id="v1-merchant-scoped-browse",
    ),
    pytest.param(
        "/agent/v1/products/search?query=example.com&limit=20&offset=0&in_stock_only=false",
        "cross_merchant_search_standard",
        id="v1-search-standard",
    ),
    pytest.param(
        "/agent/v1/products/search?merchant_id=external_seed&query=example.com&limit=20&offset=0&in_stock_only=false",
        "cross_merchant_search_standard",
        id="v1-merchant-scoped-search",
    ),
    pytest.param(
        "/agent/v1/beauty/products/search?query=example.com&limit=20&offset=0&in_stock_only=false",
        "cross_merchant_search_standard",
        id="beauty-search-standard",
    ),
    pytest.param(
        "/agent/v1/beauty/products/search?merchant_id=external_seed&query=&limit=20&offset=0&in_stock_only=false",
        "cross_merchant_browse_standard",
        id="beauty-merchant-scoped-browse",
    ),
]


@pytest.mark.parametrize("url,expected_path", _CASES)
def test_search_strips_only_the_internal_state(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, url: str, expected_path: str
) -> None:
    row = _seed_row(_search_seed_data())
    before = copy.deepcopy(row)
    calls = _patch_search(monkeypatch, [row])

    (product,) = _external_products(client.get(url, headers={"X-API-Key": "test-api-key"}))

    assert calls["paths"] and calls["paths"][-1] == expected_path, calls["paths"]
    _assert_stripped_from(_search_seed_data(), product["seed_data"])
    assert row == before  # the stored row is untouched


@pytest.mark.parametrize("url,expected_path", _CASES)
def test_search_keeps_every_input_the_gateway_gates_read(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, url: str, expected_path: str
) -> None:
    """The be#2727 review's P1-1 / P1-2 / P2-1 inputs, by name, on all three returns."""
    _patch_search(monkeypatch, [_seed_row(_search_seed_data())])
    (product,) = _external_products(client.get(url, headers={"X-API-Key": "test-api-key"}))
    seed = product["seed_data"]
    snapshot = seed["snapshot"]
    variant = snapshot["variants"][0]

    assert seed["category_path"] == "beauty/skincare/treat/serum"  # retinoid net, fragrance-free gate
    assert seed["catalog_category_path"] == "beauty/skincare/treat/serum"
    assert snapshot["category_path"] == "beauty/skincare/treat/serum"
    assert seed["source_unavailable_v1"]["status"] == "available"  # hold contracts
    assert snapshot["transaction_readiness_blocker_v1"] == {"status": "clear"}
    assert variant["visible_option_labels"] == ["Retinol 0.5%"]  # every variant string
    assert variant["options"] == [["Strength", "0.5%"]]
    assert variant["title"] == "Retinol 0.5% / 30ml"
    assert seed["variants"][0]["shade"] == "Clear"
    assert seed["variants"][0]["url"] == "https://shop.example/products/night-serum?variant=1"
    assert seed["ingredient_intel"]["actives"] == [{"name": "Hydroxypinacolone Retinoate"}]
    assert seed["inci_list"] == "Aqua, Glycerin, Retinol"
    assert snapshot["inci_list"] == "Aqua, Retinyl Palmitate"
    assert seed["tags"] == ["night", "tretinoin"] and snapshot["tags"] == ["Adapalene"]
    assert seed["price_amount"] == 12.34 and seed["commerce_facts_v1"] == {"spf_rating": "50", "texture": "gel"}


def test_search_keeps_seed_data_urls_as_stored(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """be#2730 review P1-1: the retinoid net reads these URLs as text (a relative path, a query or a
    non-http slug can name the retinoid), and they publish nothing the product's own top-level
    destination_url does not. So they are served exactly as stored."""
    seed_data = _search_seed_data()
    stored_urls = {
        ("canonical_url",): "/products/retinol-night-serum",
        ("external_url",): "ftp://shop.example/retinol-serum",
        ("snapshot", "destination_url"): "https://shop.example\\products\\retinol-serum",
        ("variants", 1, "destination_url"): "https://shop.example/p?variant=1&strength=Retinol%200.5#x",
    }
    for path, url in stored_urls.items():
        _at(seed_data, path[:-1])[path[-1]] = url
    _patch_search(monkeypatch, [_seed_row(seed_data)])
    url = "/agent/v1/products/search?query=example.com&limit=20&offset=0&in_stock_only=false"
    (product,) = _external_products(client.get(url, headers={"X-API-Key": "test-api-key"}))
    seed = product["seed_data"]

    for path, stored in stored_urls.items():
        assert _at(seed, path) == stored, path
    assert seed["destination_url"] == _search_seed_data()["destination_url"]
    assert "utm_template" not in seed  # the one attribution secret stays denied


def test_search_strips_inside_a_snapshot_stored_as_json_text(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    seed_data = _search_seed_data()
    full_snapshot = seed_data["snapshot"]
    seed_data["snapshot"] = json.dumps(full_snapshot)
    _patch_search(monkeypatch, [_seed_row(seed_data)])
    url = "/agent/v1/products/search?query=example.com&limit=20&offset=0&in_stock_only=false"
    (product,) = _external_products(client.get(url, headers={"X-API-Key": "test-api-key"}))

    served = product["seed_data"]["snapshot"]
    assert isinstance(served, str)  # the gateway reads it exactly as it read the stored text
    _assert_stripped_from(full_snapshot, json.loads(served))


@pytest.mark.parametrize("url,expected_path", _CASES)
def test_search_for_an_acl_restricted_caller_is_stripped_too(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, url: str, expected_path: str
) -> None:
    from routes.agent_auth import AgentContext, get_agent_context

    async def _override(request: Request) -> AgentContext:
        return AgentContext(
            {
                "agent_id": "agent_restricted",
                "agent_name": "Restricted",
                "allowed_merchants": ["external_seed"],
                "is_active": True,
            },
            request,
        )

    calls = _patch_search(monkeypatch, [_seed_row(_search_seed_data())])
    app.dependency_overrides[get_agent_context] = _override
    try:
        res = client.get(url, headers={"X-API-Key": "test-api-key"})
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    (product,) = _external_products(res)
    _assert_stripped_from(_search_seed_data(), product["seed_data"])
    assert calls["paths"] and calls["paths"][-1] == expected_path, calls["paths"]


def test_search_never_mutates_the_cached_products_across_two_searches(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module

    monkeypatch.setenv("EXTERNAL_SEED_THIN_CACHE_REFRESH_FLOOR", "0")
    monkeypatch.setattr(agent_api_module, "_EXTERNAL_SEED_SEARCH_CACHE", type(agent_api_module._EXTERNAL_SEED_SEARCH_CACHE)())
    monkeypatch.setattr(agent_api_module, "_EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT", {})
    calls = _patch_search(monkeypatch, [_seed_row(_search_seed_data())], cache=True)
    url = "/agent/v1/products/search?query=example.com&limit=20&offset=0&in_stock_only=false"

    first = _external_products(client.get(url, headers={"X-API-Key": "test-api-key"}))
    fetches_after_first = calls["seed_fetches"]
    second = _external_products(client.get(url, headers={"X-API-Key": "test-api-key"}))

    _assert_stripped_from(_search_seed_data(), first[0]["seed_data"])
    _assert_stripped_from(_search_seed_data(), second[0]["seed_data"])
    assert calls["seed_fetches"] == fetches_after_first  # the second came from the cache

    cached: List[Dict[str, Any]] = []
    for entry in agent_api_module._EXTERNAL_SEED_SEARCH_CACHE.values():
        cached.extend(p for p in (entry.get("products") or []) if isinstance(p, dict))
    assert cached, "the search did not populate the external-seed cache"
    for product in cached:
        assert product["seed_data"] == _search_seed_data()  # the full blob, still


@pytest.mark.parametrize(
    "url",
    [
        "/agent/v1/products/search?query=example.com&limit=20&offset=0&in_stock_only=false",
        "/agent/v1/products/search?merchant_id=external_seed&query=example.com&limit=20&offset=0&in_stock_only=false",
        "/agent/v1/beauty/products/search?query=example.com&limit=20&offset=0&in_stock_only=false",
    ],
)
def test_search_fails_safe_when_the_projection_raises(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, url: str
) -> None:
    """A raise never serves the unstripped products: a 500 (unscoped v1 and beauty), or, on a
    merchant-scoped SDK call, the legacy scoped fallback, which carries no seed_data."""
    import routes.agent_api as agent_api_module

    def boom(_products: Any) -> Any:
        raise RuntimeError("projection failed")

    monkeypatch.setattr(agent_api_module, "serve_search_products", boom)
    _patch_search(monkeypatch, [_seed_row(_search_seed_data())])
    res = client.get(url, headers={"X-API-Key": "test-api-key"})
    if "merchant_id=external_seed" in url:
        assert res.status_code == 200
        assert not [p for p in res.json().get("products") or [] if p.get("merchant_id") == "external_seed"]
    else:
        assert res.status_code == 500
    for word in ("seed_data", "INTERNAL-", "utm_template"):
        assert word not in res.text, word


# --- the strip itself ---------------------------------------------------------------------------------

from services.seed_data_egress_projection import (  # noqa: E402
    INTERNAL_STATE_KEYS,
    strip_internal_seed_data,
    strip_products_internal_seed_data,
)


def test_the_module_denies_exactly_the_keys_this_test_names() -> None:
    assert set(INTERNAL_STATE_KEYS) == _INTERNAL_KEYS


@pytest.mark.parametrize("key", sorted(_INTERNAL_KEYS))
def test_every_internal_key_is_removed_at_every_depth(key: str) -> None:
    marker = f"INTERNAL-{key}"
    stored = {
        key: marker,
        "title": "kept",
        "snapshot": {key: marker, "variants": [{key: marker, "title": "kept"}]},
        "variants": [{key: marker, "options": [{key: marker, "name": "kept"}]}],
        "ingredient_intel": {"actives": [{key: marker, "name": "kept"}]},
        "attributes": {"nested": {"deeper": {key: marker, "value": "kept"}}},
    }
    stripped = strip_internal_seed_data(stored)
    assert marker not in json.dumps(stripped)
    assert json.dumps(stripped).count('"kept"') == 5


def test_strip_never_mutates_and_shares_no_containers() -> None:
    stored = _search_seed_data()
    before = copy.deepcopy(stored)
    stripped = strip_internal_seed_data(stored)
    assert stored == before
    assert stripped["snapshot"] is not stored["snapshot"]
    assert stripped["variants"][0] is not stored["variants"][0]
    assert stripped["tags"] is not stored["tags"]
    stripped["snapshot"]["variants"][0]["title"] = "changed"
    stripped["tags"].append("changed")
    assert stored == before


def test_strip_products_never_mutates_the_list_or_its_products() -> None:
    with_blob = {"id": "a", "seed_data": _search_seed_data()}
    only_internal = {"id": "b", "seed_data": {"price_read": {"amount": 1}}}
    no_blob = {"id": "c"}
    products = [with_blob, only_internal, no_blob]
    before = copy.deepcopy(products)

    out = strip_products_internal_seed_data(products)

    assert products == before
    assert out is not products
    assert out[0] is not with_blob and out[1] is not only_internal
    assert out[2] is no_blob
    assert "seed_data" not in out[1]
    assert strip_products_internal_seed_data(None) is None


@pytest.mark.parametrize("stored", [None, "text", ["a"], 7, {}, {"price_read": {"a": 1}}])
def test_strip_of_nothing_left_is_none(stored: Any) -> None:
    assert strip_internal_seed_data(stored) is None


def test_strip_bounds_its_depth_and_never_raises() -> None:
    deep: Dict[str, Any] = {"leaf": "deepest"}
    for _ in range(5000):
        deep = {"n": deep}
    stripped = strip_internal_seed_data({"title": "x", "deep": deep})
    assert stripped["title"] == "x"
    assert "deepest" not in json.dumps(stripped)  # below the bound: dropped, nothing reads that deep

    text = '{"a":' + "[" * 5000 + "]" * 5000 + "}"
    assert strip_internal_seed_data({"title": "x", "snapshot": text}) == {"title": "x"}


def test_a_text_snapshot_that_is_not_a_json_object_is_dropped() -> None:
    for text in ("not json", "[1, 2]", '"s"', ""):
        assert strip_internal_seed_data({"title": "x", "snapshot": text}) == {"title": "x"}, text


# --- lone surrogates (be#2730 review P2-1) ------------------------------------------------------------

_LONE_SURROGATE_SNAPSHOT = json.dumps({"description": "Glow \ud83d", "title": "Night \udc00 Serum", "price_read": 1})


@pytest.mark.parametrize("url,expected_path", _CASES)
def test_search_with_a_lone_surrogate_in_a_text_snapshot_still_renders(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, url: str, expected_path: str
) -> None:
    """jsonb holds the ESCAPE (what JS writes for a split emoji); parsing the text yields a raw lone
    surrogate, which would fail the response's encode after the route returned: a 500 for the page."""
    seed_data = _search_seed_data()
    seed_data["snapshot"] = _LONE_SURROGATE_SNAPSHOT
    _patch_search(monkeypatch, [_seed_row(seed_data)])
    res = client.get(url, headers={"X-API-Key": "test-api-key"})
    assert res.status_code == 200, res.text[:300]
    (product,) = _external_products(res)
    served = json.loads(product["seed_data"]["snapshot"])
    assert served == {"description": "Glow \ufffd", "title": "Night \ufffd Serum"}


def test_strip_replaces_lone_surrogates_in_every_string_and_key() -> None:
    stripped = strip_internal_seed_data({"a\ud83d": ["x\udc00", {"k": "ok \U0001F600"}]})
    assert stripped == {"a\ufffd": ["x\ufffd", {"k": "ok \U0001F600"}]}
    json.dumps(stripped, ensure_ascii=False).encode("utf-8")  # encodes


def test_strip_keeps_everything_down_to_its_bound() -> None:
    """Mutant D2 (bound 64 -> 20): a real but deep structure at depth 40 survives whole."""
    deep: Dict[str, Any] = {"leaf": "kept-at-40"}
    for _ in range(40):
        deep = {"n": deep}
    stripped = strip_internal_seed_data({"title": "x", "deep": deep})
    assert "kept-at-40" in json.dumps(stripped)


def test_a_reserialised_text_snapshot_is_ascii_escaped() -> None:
    """Defence in depth behind the surrogate replacement: the text form never carries a raw non-ASCII
    character either (the gateway never parses a string snapshot, so the escaping changes no reader)."""
    stripped = strip_internal_seed_data({"snapshot": json.dumps({"title": "Crème \U0001F600", "audit": {"a": 1}})})
    assert stripped["snapshot"].isascii()
    assert json.loads(stripped["snapshot"]) == {"title": "Crème \U0001F600"}
