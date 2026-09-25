"""An external seed with no stock claim is published on /agent/v2 as in_stock null.

THE DEFECT. The external-seed builders (services/external_seed_stock.seed_stock_fields)
serve a seed whose own availability is unknown, or contradicted by its variants, as
`availability: "unknown"` with NO `in_stock` key. `_canonicalize_search_product`
filled the gap with `bool(product.get("in_stock", True))`, so every offer claimed
`availability.in_stock: true`. (Live-verification rows were already nulled by #2321.)

UNKNOWN IS NEITHER TRUE NOR FALSE. A real boolean passes through; None is never
folded into False, which the gateway reads as sold out and drops.

CONSUMERS (audited 2026-09-25). The backend has no reader of v2 offers. The gateway
(PIVOTA-Agent src/server.js normalizeCanonicalSearchProductCompat) takes the offer's
`in_stock` only when it is a boolean, so a null leaves the flattened product with no
`in_stock` -- the shape live-verification rows have served since #2321. Its
availability contract drops only a known out_of_stock; `in_stock_only` filters
(backend `_passes_explicit_commerce_filters`, gateway policy `hasPositiveInStockSignal`)
already exclude unknown. Run against gateway origin/main with a priced, in_stock-null
seed on the MCP and shopping-agent doors: kept, same position, in_stock absent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from routes.agent_v2 import _canonicalize_search_product  # noqa: E402
from tests.test_external_seed_product_stock_follows_availability import (  # noqa: E402
    _MODULES,
    _build,
    _seed_row,
)


def _offer_stock(product: Dict[str, Any]):
    return [o["availability"]["in_stock"] for o in _canonicalize_search_product(product)["offers"]]


def _seed_like(**overrides: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "product_id": "round-lab:85dd4c56da58a259",
        "merchant_id": "external_seed",
        "title": "Round Lab Sheet Mask Sampler 9pc",
        "price": 18.0,
        "currency": "USD",
        "source": "external_seed",
        "availability": "unknown",
        "commerce_verification": {"required": False, "status": "catalog_facts_accepted"},
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.asyncio
async def test_a_builder_unknown_seed_is_published_as_null(module_path, monkeypatch):
    product = await _build(module_path, _seed_row(availability=None), monkeypatch)
    # Premise: the builder served the unknown shape, not a boolean.
    assert product["availability"] == "unknown"
    assert "in_stock" not in product
    stock = _offer_stock(product)
    assert stock and all(value is None for value in stock), stock


def test_a_present_none_with_unknown_availability_is_null_not_false():
    assert _offer_stock(_seed_like(in_stock=None)) == [None]


@pytest.mark.parametrize("availability", ["Unknown", " UNKNOWN "])
def test_the_unknown_marker_is_read_case_insensitively(availability):
    assert _offer_stock(_seed_like(availability=availability)) == [None]


@pytest.mark.parametrize("in_stock", [True, False])
def test_a_real_boolean_wins_over_the_unknown_marker(in_stock):
    assert _offer_stock(_seed_like(in_stock=in_stock)) == [in_stock]


@pytest.mark.parametrize(
    "overrides, expected",
    [
        # Scope guard: only the unknown marker changes the default.
        pytest.param({"availability": None}, [True], id="no_marker_keeps_legacy_true"),
        pytest.param({"availability": "in_stock"}, [True], id="in_stock_marker"),
        pytest.param({"availability": "out_of_stock", "in_stock": False}, [False], id="out_of_stock"),
    ],
)
def test_rows_without_the_unknown_marker_keep_the_legacy_default(overrides, expected):
    assert _offer_stock(_seed_like(**overrides)) == expected


def _seed_variant(vid: str, *, in_stock: Any = None, availability: Any = None) -> Dict[str, Any]:
    # The builders' served-variant shape: a bool comes with 999/0, availability only if set.
    v: Dict[str, Any] = {"variant_id": vid, "title": f"Variant {vid}", "price": 18.0, "currency": "USD"}
    if in_stock is not None:
        v["in_stock"] = in_stock
        v["inventory_quantity"] = 999 if in_stock else 0
    if availability is not None:
        v["availability"] = availability
    return v


def _offers(product: Dict[str, Any]):
    return [
        (o["availability"]["in_stock"], o["availability"]["inventory_quantity"])
        for o in _canonicalize_search_product(product)["offers"]
    ]


@pytest.mark.parametrize(
    "variant, expected",
    [
        # An inherited bool (no availability of its own) under an unknown product.
        pytest.param(_seed_variant("1", in_stock=True), [(None, None)], id="bool_without_availability"),
        pytest.param(
            _seed_variant("1", in_stock=True, availability="in_stock"), [(True, 999)], id="own_in_stock"
        ),
        pytest.param(
            _seed_variant("1", in_stock=False, availability="out_of_stock"), [(False, 0)], id="own_out_of_stock"
        ),
        # The builders copy any non-None availability, so presence alone is not a signal.
        pytest.param(_seed_variant("1", in_stock=True, availability=""), [(None, None)], id="empty_string"),
        pytest.param(
            _seed_variant("1", in_stock=True, availability="pre-order"), [(None, None)], id="unrecognised"
        ),
        # #2333's shape for an unknown product's variant: no boolean at all.
        pytest.param(_seed_variant("1", availability="unknown"), [(None, None)], id="variant_unknown"),
    ],
)
def test_under_an_unknown_product_only_a_known_variant_availability_speaks(variant, expected):
    # inventory_quantity falls back with in_stock, so null is never shown next to 999.
    assert _offers(_seed_like(variants=[variant])) == expected


def test_an_unknown_product_with_mixed_variants_speaks_only_for_the_known_ones():
    product = _seed_like(
        variants=[
            _seed_variant("1", in_stock=False, availability="out_of_stock"),
            _seed_variant("2", in_stock=True),
            _seed_variant("3", in_stock=True, availability="In Stock"),
        ]
    )
    assert _offers(product) == [(False, 0), (None, None), (True, 999)]


@pytest.mark.parametrize("product_in_stock", [True, False])
def test_under_a_known_product_a_variant_bool_is_still_its_own_fact(product_in_stock):
    # Scope guard: #2323's per-variant stock is unchanged when the product has a claim.
    product = _seed_like(
        availability=None,
        in_stock=product_in_stock,
        inventory_quantity=999 if product_in_stock else 0,
        variants=[_seed_variant("1", in_stock=not product_in_stock), _seed_variant("2")],
    )
    qty = 999 if product_in_stock else 0
    assert _offers(product) == [(not product_in_stock, 0 if product_in_stock else 999), (product_in_stock, qty)]


@pytest.mark.asyncio
async def test_the_route_serialises_unknown_stock_as_json_null(monkeypatch):
    import routes.agent_v2 as agent_v2
    from main import app
    from routes.agent_auth import get_agent_context

    class _Ctx:
        agent_id = "agent_v2_unknown_stock"
        agent_name = "Agent V2 Unknown Stock"
        allowed_merchants = ["external_seed"]
        session_id = "session_v2_unknown_stock"

        def can_access_merchant(self, merchant_id: str) -> bool:
            return True

    async def _ctx() -> _Ctx:
        return _Ctx()

    unknown = await _build("routes.agent_api", _seed_row(availability=None), monkeypatch)
    in_stock = await _build("routes.agent_api", _seed_row(availability="in_stock"), monkeypatch)
    in_stock["product_id"] = in_stock["id"] = "control:in_stock"

    async def fake_v1_search(**kwargs: Any) -> Dict[str, Any]:
        return {"status": "success", "products": [unknown, in_stock], "metadata": {}}

    app.dependency_overrides[get_agent_context] = _ctx
    monkeypatch.setattr(agent_v2, "agent_v1_search_products", fake_v1_search)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/agent/v2/products/search", json={"query": "round lab"})
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 200
    assert '"in_stock":null' in resp.text.replace(" ", "")
    by_id = {p["product_id"]: p for p in resp.json()["products"]}
    assert [o["availability"]["in_stock"] for o in by_id[unknown["product_id"]]["offers"]] == [None]
    # CONTROL: a seed with a real claim keeps it on the same response.
    assert [o["availability"]["in_stock"] for o in by_id["control:in_stock"]["offers"]] == [True]
