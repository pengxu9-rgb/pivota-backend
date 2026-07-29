"""Wix category mapping: collections -> StandardProduct.product_type.

Why this file exists at all
---------------------------
Wix keeps a product's category in COLLECTIONS, a resource the adapter never
called. `product_type` was therefore always None, and
`product_quality_service` scores

    ("brand_category", 1.0 if brand_present and category_present else 0.0)

as one of six equal-weight components. A category-less Wix row forfeits 16.7
points outright. Measured on the 2026-07-29 Wix pilot: scoring capped at
4-of-6 = 66.7 against a 71.4 floor, so NO Wix product could serve. Writing
`product_type` by hand onto those 20 rows moved them to 83.3 and 14/14
serving_eligible.

Every failure mode of this feature is SILENT — wrong endpoint, renamed field,
revoked scope, no collections — all yield `product_type=None`, which is
byte-identical to the bug. So these tests assert on BEHAVIOUR (what value comes
out) and never on source text; a source-match test here would die when a string
changed and live when behaviour changed, which is exactly backwards.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from adapters.product_adapters import WixProductAdapter
from services.wix_connection import WIX_ALL_PRODUCTS_COLLECTION_ID


def _wix_product(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "id": "prod-1",
        "name": "Reflective Dog Harness",
        "description": "A harness.",
        "brand": "Arolinne",
        "sku": "SKU-1",
        "priceData": {"price": 42.0, "currency": "USD"},
        "visible": True,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# collection id extraction
# --------------------------------------------------------------------------

def test_extracts_collection_ids():
    wp = _wix_product(collectionIds=["c1", "c2"])
    assert WixProductAdapter._extract_wix_collection_ids(wp) == ["c1", "c2"]


def test_all_products_collection_is_excluded():
    """The implicit "All Products" collection must never become a category.

    Every product in every Wix store belongs to it. Mapping it would give the
    whole catalog one identical, meaningless product_type that scores exactly
    as well as a real one -- a valid-but-wrong value, which is worse than None
    because it is invisible to the canary AND blocks the real category.
    """
    wp = _wix_product(collectionIds=[WIX_ALL_PRODUCTS_COLLECTION_ID, "c1"])
    assert WixProductAdapter._extract_wix_collection_ids(wp) == ["c1"]


def test_all_products_only_yields_no_category():
    wp = _wix_product(collectionIds=[WIX_ALL_PRODUCTS_COLLECTION_ID])
    assert WixProductAdapter._extract_wix_collection_ids(wp) == []
    assert WixProductAdapter._pick_wix_product_type(
        wp, {WIX_ALL_PRODUCTS_COLLECTION_ID: "All Products"}
    ) is None


@pytest.mark.parametrize("field", ["collectionIds", "collection_ids", "collectionIDs"])
def test_accepts_known_field_spellings(field: str):
    """Wix has moved this field's spelling across catalog versions."""
    assert WixProductAdapter._extract_wix_collection_ids(_wix_product(**{field: ["c1"]})) == ["c1"]


def test_missing_or_malformed_collection_ids_is_empty_not_an_error():
    assert WixProductAdapter._extract_wix_collection_ids(_wix_product()) == []
    assert WixProductAdapter._extract_wix_collection_ids(_wix_product(collectionIds="c1")) == []
    assert WixProductAdapter._extract_wix_collection_ids(_wix_product(collectionIds=[None, "", "  "])) == []


# --------------------------------------------------------------------------
# picking the category
# --------------------------------------------------------------------------

def test_picks_the_collection_name():
    wp = _wix_product(collectionIds=["c1"])
    assert WixProductAdapter._pick_wix_product_type(wp, {"c1": "Dog Harness"}) == "Dog Harness"


def test_pick_is_deterministic_across_id_order():
    """An unstable product_type churns the quality score on every re-sync.

    The same product, same collections, different id ORDER (which the API does
    not promise to preserve) must yield the same category.
    """
    names = {"c1": "Harnesses", "c2": "Collars", "c3": "Leashes"}
    a = WixProductAdapter._pick_wix_product_type(_wix_product(collectionIds=["c1", "c2", "c3"]), names)
    b = WixProductAdapter._pick_wix_product_type(_wix_product(collectionIds=["c3", "c1", "c2"]), names)
    assert a == b == "Collars"  # alphabetically first


def test_unresolvable_ids_yield_none_not_the_id():
    """A raw id is not a category name.

    Falling back to the id would emit a valid-looking string that scores a full
    component while telling a buyer or a crawler nothing -- and would hide the
    breakage from the canary.
    """
    wp = _wix_product(collectionIds=["c-unknown"])
    assert WixProductAdapter._pick_wix_product_type(wp, {"c1": "Harnesses"}) is None


def test_no_collection_map_yields_none():
    wp = _wix_product(collectionIds=["c1"])
    assert WixProductAdapter._pick_wix_product_type(wp, None) is None
    assert WixProductAdapter._pick_wix_product_type(wp, {}) is None


def test_blank_collection_name_is_not_a_category():
    wp = _wix_product(collectionIds=["c1"])
    assert WixProductAdapter._pick_wix_product_type(wp, {"c1": "   "}) is None


# --------------------------------------------------------------------------
# end-to-end through _convert_product -- the assertion that actually matters
# --------------------------------------------------------------------------

def test_convert_product_sets_product_type():
    product = WixProductAdapter._convert_product(
        _wix_product(collectionIds=["c1"]),
        merchant_id="m1",
        collection_names={"c1": "Dog Harness"},
    )
    assert product is not None
    assert product.product_type == "Dog Harness"


def test_convert_product_without_collections_is_unchanged():
    """The degraded path must reproduce the old behaviour exactly.

    Losing categories costs one score component; letting it break the sync
    would cost the whole catalog.
    """
    product = WixProductAdapter._convert_product(_wix_product(), merchant_id="m1")
    assert product is not None
    assert product.product_type is None
    assert product.title == "Reflective Dog Harness"
    assert product.vendor == "Arolinne"


def test_category_is_what_moves_the_score_component():
    """Pin the ACTUAL reason this feature exists, not a proxy for it.

    Guards against a future refactor that keeps product_type populated but
    stops it reaching the scorer's category input.
    """
    from services.product_quality_service import _first_non_empty  # noqa: F401 - import guard

    with_cat = WixProductAdapter._convert_product(
        _wix_product(collectionIds=["c1"]), merchant_id="m1", collection_names={"c1": "Dog Harness"}
    )
    without = WixProductAdapter._convert_product(_wix_product(), merchant_id="m1")

    # `product_quality_service` reads `product.product_type` as its category.
    assert bool(with_cat.product_type) is True
    assert bool(without.product_type) is False


# --------------------------------------------------------------------------
# the collections fetch -- degradation is the contract
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    """Records calls so paging is assertable, not assumed."""

    def __init__(self, responses: List[Any]):
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def post(self, url: str, json: Optional[Dict[str, Any]] = None, headers=None):
        self.calls.append({"url": url, "json": json})
        nxt = self._responses.pop(0) if self._responses else _FakeResponse(200, {"collections": []})
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _fetch(client) -> Dict[str, str]:
    return asyncio.run(WixProductAdapter._fetch_wix_collection_names(client, {}, "m1"))


def test_fetch_builds_the_id_to_name_map():
    client = _FakeClient([
        _FakeResponse(200, {"collections": [
            {"id": "c1", "name": "Harnesses"},
            {"id": "c2", "name": "Collars"},
        ]}),
    ])
    assert _fetch(client) == {"c1": "Harnesses", "c2": "Collars"}


def test_fetch_drops_the_all_products_collection():
    client = _FakeClient([
        _FakeResponse(200, {"collections": [
            {"id": WIX_ALL_PRODUCTS_COLLECTION_ID, "name": "All Products"},
            {"id": "c1", "name": "Harnesses"},
        ]}),
    ])
    assert _fetch(client) == {"c1": "Harnesses"}


def test_fetch_returns_empty_on_http_error_and_does_not_raise():
    client = _FakeClient([_FakeResponse(403, text="forbidden")])
    assert _fetch(client) == {}


def test_fetch_returns_empty_on_transport_error_and_does_not_raise():
    client = _FakeClient([RuntimeError("connection reset")])
    assert _fetch(client) == {}


def test_fetch_returns_empty_on_malformed_body():
    client = _FakeClient([_FakeResponse(200, {"unexpected": "shape"})])
    assert _fetch(client) == {}


def test_fetch_stops_on_a_short_page():
    """A short page means the end. Without this the loop burns all 10 pages."""
    client = _FakeClient([
        _FakeResponse(200, {"collections": [{"id": "c1", "name": "Harnesses"}]}),
        _FakeResponse(200, {"collections": [{"id": "c2", "name": "Never Fetched"}]}),
    ])
    assert _fetch(client) == {"c1": "Harnesses"}
    assert len(client.calls) == 1


def test_fetch_is_bounded_when_every_page_is_full():
    """A pathological response must not spin the sync forever."""
    full = [{"id": f"c{i}", "name": f"N{i}"} for i in range(100)]
    client = _FakeClient([_FakeResponse(200, {"collections": full}) for _ in range(50)])
    _fetch(client)
    assert len(client.calls) == 10  # _WIX_COLLECTIONS_MAX_PAGES


def test_fetch_targets_the_collections_endpoint_not_products():
    client = _FakeClient([_FakeResponse(200, {"collections": []})])
    _fetch(client)
    assert client.calls[0]["url"].endswith("/collections/query")


# --------------------------------------------------------------------------
# THE WIRING -- the only tests that prove the feature is connected
# --------------------------------------------------------------------------
#
# Everything above tests the two halves in ISOLATION: _convert_product with a
# map handed to it, and _fetch_wix_collection_names on its own. A mutation run
# proved that is not enough. Deleting `collection_names=collection_names` from
# the fetch loop's call to _convert_product -- i.e. disconnecting the feature
# completely, so every product in production ships category-less exactly as
# before -- left all 23 isolation tests GREEN.
#
# That is the defect class this repo keeps hitting: the parts work, the seam
# does not, and the suite says everything is fine. These tests drive
# fetch_products end to end and assert on the products that come OUT.

class _SequencedClient:
    """One fake client for the whole sync: collections call, then product pages."""

    def __init__(self, collections: Any, product_pages: List[Any]):
        self._collections = collections
        self._product_pages = list(product_pages)
        self.urls: List[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, json: Optional[Dict[str, Any]] = None, headers=None):
        self.urls.append(url)
        if url.endswith("/collections/query"):
            return self._collections
        page = self._product_pages.pop(0) if self._product_pages else _FakeResponse(200, {"products": []})
        return page


def _run_fetch(monkeypatch, client) -> List[Any]:
    import adapters.product_adapters as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *a, **k: client)
    products, _token, err = asyncio.run(
        WixProductAdapter.fetch_products(
            site_id="site-1", api_key="key-1", merchant_id="m1", limit=50
        )
    )
    assert err is None
    return products


def test_fetch_products_puts_the_category_on_the_product(monkeypatch):
    """The end-to-end assertion. Kills the disconnect mutant."""
    client = _SequencedClient(
        collections=_FakeResponse(200, {"collections": [{"id": "c1", "name": "Dog Harness"}]}),
        product_pages=[_FakeResponse(200, {
            "products": [_wix_product(collectionIds=["c1"])],
            "totalResults": 1,
        })],
    )
    products = _run_fetch(monkeypatch, client)
    assert len(products) == 1
    assert products[0].product_type == "Dog Harness", (
        "the collections map was fetched but never reached _convert_product -- "
        "the feature is disconnected"
    )


def test_fetch_products_queries_collections_before_products(monkeypatch):
    """Order is load-bearing: every converted row reads the map."""
    client = _SequencedClient(
        collections=_FakeResponse(200, {"collections": [{"id": "c1", "name": "Dog Harness"}]}),
        product_pages=[_FakeResponse(200, {"products": [_wix_product(collectionIds=["c1"])], "totalResults": 1})],
    )
    _run_fetch(monkeypatch, client)
    assert client.urls[0].endswith("/collections/query")
    assert client.urls[1].endswith("/products/query")


def test_fetch_products_queries_collections_once_per_sync_not_per_product(monkeypatch):
    """Collections are a per-store resource. Per-product would be N+1."""
    client = _SequencedClient(
        collections=_FakeResponse(200, {"collections": [{"id": "c1", "name": "Dog Harness"}]}),
        product_pages=[_FakeResponse(200, {
            "products": [
                _wix_product(id=f"p{i}", sku=f"S{i}", collectionIds=["c1"]) for i in range(5)
            ],
            "totalResults": 5,
        })],
    )
    products = _run_fetch(monkeypatch, client)
    assert len(products) == 5
    assert all(p.product_type == "Dog Harness" for p in products)
    assert sum(1 for u in client.urls if u.endswith("/collections/query")) == 1


def test_fetch_products_survives_a_dead_collections_endpoint(monkeypatch):
    """Degradation is the contract: no categories, but the catalog still syncs."""
    client = _SequencedClient(
        collections=_FakeResponse(403, text="forbidden"),
        product_pages=[_FakeResponse(200, {"products": [_wix_product(collectionIds=["c1"])], "totalResults": 1})],
    )
    products = _run_fetch(monkeypatch, client)
    assert len(products) == 1
    assert products[0].product_type is None
    assert products[0].title == "Reflective Dog Harness"


def test_fetch_products_warns_when_mapping_resolves_nothing(monkeypatch, caplog):
    """The canary. Every failure mode of this feature is otherwise silent.

    Products carrying collection ids while none resolve to a name is not a
    store without categories -- it is a lookup that did not work, and it must
    be loud.
    """
    import logging

    client = _SequencedClient(
        collections=_FakeResponse(200, {"collections": [{"id": "other", "name": "Unrelated"}]}),
        product_pages=[_FakeResponse(200, {"products": [_wix_product(collectionIds=["c1"])], "totalResults": 1})],
    )
    with caplog.at_level(logging.WARNING):
        products = _run_fetch(monkeypatch, client)
    assert products[0].product_type is None
    assert any("RESOLVED NOTHING" in r.message for r in caplog.records), (
        "a broken lookup produced no warning -- it is indistinguishable from a "
        "store that simply has no collections"
    )


def test_no_warning_when_the_store_genuinely_has_no_collections(monkeypatch, caplog):
    """The canary must answer BOTH ways, or it is noise that gets muted."""
    import logging

    client = _SequencedClient(
        collections=_FakeResponse(200, {"collections": []}),
        product_pages=[_FakeResponse(200, {"products": [_wix_product()], "totalResults": 1})],
    )
    with caplog.at_level(logging.WARNING):
        _run_fetch(monkeypatch, client)
    assert not any("RESOLVED NOTHING" in r.message for r in caplog.records)
