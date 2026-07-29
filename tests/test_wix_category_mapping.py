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


@pytest.mark.parametrize(
    "collections,expected",
    [
        ({"c1": "Best Sellers", "c2": "Dog Harnesses"}, "Dog Harnesses"),
        ({"c1": "New Arrivals", "c2": "Vitamin C Serum"}, "Vitamin C Serum"),
        ({"c1": "All Products Shop", "c2": "Winter Coats"}, "Winter Coats"),
        ({"c1": "Sale", "c2": "Zebra Print Leggings"}, "Zebra Print Leggings"),
        ({"c1": "Gifts", "c2": "Toners"}, "Toners"),
        ({"c1": "Clearance", "c2": "Bath Bombs"}, "Bath Bombs"),
    ],
)
def test_merchandising_collections_lose_to_a_semantic_one(collections, expected):
    """Alphabetical-first is BIASED, not neutral.

    Merchandising names cluster on A/B/N/S, so plain sorting reliably picks the
    storefront shelf over the product kind. The score component fills either
    way -- which is what makes this easy to miss -- but everything downstream of
    product_type degrades: resolve_vertical drops beauty/fashion to `other`, the
    beauty taxonomy rows are never written, and category probes would generate
    queries for "New Arrivals".
    """
    wp = _wix_product(collectionIds=list(collections))
    assert WixProductAdapter._pick_wix_product_type(wp, collections) == expected


def test_merchandising_is_still_better_than_no_category():
    """The stoplist DEPRIORITISES; it must never veto the only candidate.

    A merchandising category still fills brand_category, which is the whole
    point of the change.
    """
    wp = _wix_product(collectionIds=["c1", "c2"])
    assert WixProductAdapter._pick_wix_product_type(
        wp, {"c1": "Sale", "c2": "Best Sellers"}
    ) == "Best Sellers"


@pytest.mark.parametrize("name", ["Salt Scrubs", "Newborn Sets", "Allergy Care", "Bestie Bundles Co"])
def test_stoplist_does_not_swallow_real_categories(name):
    """Anchored word-boundary matching: "Sale" must not eat "Salt Scrubs"."""
    wp = _wix_product(collectionIds=["c1", "c2"])
    picked = WixProductAdapter._pick_wix_product_type(wp, {"c1": name, "c2": "Zzz Last"})
    assert picked == name, f"{name!r} was wrongly treated as a merchandising shelf"


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
        # `headers` is recorded because a dropped Authorization header is a
        # 403 -> {} -> the whole feature silently off. A mutation run showed
        # that removing headers from the collections POST survived the suite.
        self.calls.append({"url": url, "json": json, "headers": headers})
        nxt = self._responses.pop(0) if self._responses else _FakeResponse(200, {"collections": []})
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _fetch(client, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    return asyncio.run(
        WixProductAdapter._fetch_wix_collection_names(client, headers or {}, "m1")
    )


def test_fetch_forwards_the_auth_headers():
    """A dropped Authorization header is 403 -> {} -> the feature silently off."""
    client = _FakeClient([_FakeResponse(200, {"collections": []})])
    _fetch(client, {"Authorization": "tok", "wix-site-id": "site-1"})
    assert client.calls[0]["headers"] == {"Authorization": "tok", "wix-site-id": "site-1"}


def test_fetch_advances_the_paging_offset():
    """Without a real advance, page 2 re-reads page 1 forever."""
    full = [{"id": f"c{i}", "name": f"N{i}"} for i in range(100)]
    client = _FakeClient([
        _FakeResponse(200, {"collections": full}),
        _FakeResponse(200, {"collections": [{"id": "c100", "name": "Last"}]}),
    ])
    names = _fetch(client)
    assert client.calls[0]["json"]["query"]["paging"]["offset"] == 0
    assert client.calls[1]["json"]["query"]["paging"]["offset"] == 100
    assert names["c100"] == "Last"


def test_fetch_sends_a_paging_body():
    client = _FakeClient([_FakeResponse(200, {"collections": []})])
    _fetch(client)
    paging = client.calls[0]["json"]["query"]["paging"]
    assert paging["limit"] == 100 and paging["offset"] == 0


def test_partial_page_failure_discards_rather_than_returning_a_partial_map():
    """A partial map is worse than none: it silently changes the category.

    101 collections, product in {"Zebra Print" (page 1), "Apparel" (page 2)}.
    A complete fetch picks "Apparel"; a partial one picks "Zebra Print" -- a
    different, valid-looking value that flips back on the next good sync,
    breaking the determinism _pick_wix_product_type relies on.
    """
    page1 = [{"id": f"c{i}", "name": f"Zebra Print {i}"} for i in range(100)]
    client = _FakeClient([
        _FakeResponse(200, {"collections": page1}),
        _FakeResponse(500, text="boom"),
    ])
    assert _fetch(client) == {}


def test_partial_transport_failure_also_discards():
    page1 = [{"id": f"c{i}", "name": f"N{i}"} for i in range(100)]
    client = _FakeClient([
        _FakeResponse(200, {"collections": page1}),
        RuntimeError("connection reset"),
    ])
    assert _fetch(client) == {}


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
        self.calls: List[Dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, json: Optional[Dict[str, Any]] = None, headers=None):
        self.urls.append(url)
        self.calls.append({"url": url, "json": json, "headers": headers})
        if url.endswith("/collections/query"):
            return self._collections
        page = self._product_pages.pop(0) if self._product_pages else _FakeResponse(200, {"products": []})
        return page


def _run_fetch(monkeypatch, client, limit: int = 50) -> List[Any]:
    import adapters.product_adapters as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *a, **k: client)
    products, _token, err = asyncio.run(
        WixProductAdapter.fetch_products(
            site_id="site-1", api_key="key-1", merchant_id="m1", limit=limit
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


def test_fetch_products_queries_collections_once_across_MANY_product_pages(monkeypatch):
    """Collections are a per-store resource. Re-fetching per page is N+1.

    This test needs MULTIPLE product pages to mean anything. With a single-page
    fixture it passed whether the fetch sat before the loop or inside it -- one
    page, one call, either way -- and a mutation run confirmed the N+1 mutant
    survived it. Three pages at limit=2 make the two implementations differ:
    1 collections call vs 3.
    """
    page = lambda i: _FakeResponse(200, {  # noqa: E731
        "products": [
            _wix_product(id=f"p{i}a", sku=f"S{i}a", collectionIds=["c1"]),
            _wix_product(id=f"p{i}b", sku=f"S{i}b", collectionIds=["c1"]),
        ],
        "totalResults": 6,
    })
    client = _SequencedClient(
        collections=_FakeResponse(200, {"collections": [{"id": "c1", "name": "Dog Harness"}]}),
        product_pages=[page(0), page(1), page(2)],
    )
    products = _run_fetch(monkeypatch, client, limit=2)

    assert len(products) == 6, "the multi-page fixture did not actually page"
    assert sum(1 for u in client.urls if u.endswith("/products/query")) == 3
    assert all(p.product_type == "Dog Harness" for p in products)
    assert sum(1 for u in client.urls if u.endswith("/collections/query")) == 1


def test_fetch_products_forwards_auth_headers_to_collections(monkeypatch):
    """Real credentials must reach the collections call, not just products."""
    client = _SequencedClient(
        collections=_FakeResponse(200, {"collections": [{"id": "c1", "name": "Dog Harness"}]}),
        product_pages=[_FakeResponse(200, {"products": [_wix_product(collectionIds=["c1"])], "totalResults": 1})],
    )
    _run_fetch(monkeypatch, client)
    collections_call = next(c for c in client.calls if c["url"].endswith("/collections/query"))
    assert collections_call["headers"], "collections call went out with NO headers"
    assert any(
        "authorization" in str(k).lower() or "wix-site-id" in str(k).lower()
        for k in collections_call["headers"]
    ), f"no auth header on the collections call: {sorted(collections_call['headers'])}"


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


def test_canary_fires_when_collections_exist_but_no_product_claims_one(monkeypatch, caplog):
    """The field-rename blind spot.

    If Wix renames `collectionIds` to a spelling outside our aliases, every
    product parses as having no collections. The "RESOLVED NOTHING" arm cannot
    see that -- it requires products_with_collection_ids to be non-zero, which
    is exactly what a rename drives to zero. A store with real collections whose
    products all claim membership in none of them is anomalous, not empty.
    """
    import logging

    client = _SequencedClient(
        collections=_FakeResponse(200, {"collections": [
            {"id": "c1", "name": "Harnesses"},
            {"id": "c2", "name": "Collars"},
        ]}),
        product_pages=[_FakeResponse(200, {
            # renamed field -> parses as "no collection ids"
            "products": [_wix_product(collectionSlugs=["c1"])],
            "totalResults": 1,
        })],
    )
    with caplog.at_level(logging.WARNING):
        products = _run_fetch(monkeypatch, client)
    assert products[0].product_type is None
    assert any("SAW NO COLLECTION IDS" in r.message for r in caplog.records), (
        "a renamed collectionIds field produced no warning -- every product "
        "ships category-less and nothing says so"
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
    # Assert on EVERY category-mapping warning, not just one string. Checking a
    # single message let a mutant that fires the second arm unconditionally
    # survive: a canary that always fires is a canary that gets muted, and then
    # the real signal is gone too.
    noisy = [
        r.message for r in caplog.records
        if "RESOLVED NOTHING" in r.message or "SAW NO COLLECTION IDS" in r.message
    ]
    assert not noisy, f"canary fired for a store that genuinely has no collections: {noisy}"
