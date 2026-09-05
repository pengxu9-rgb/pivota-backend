"""
The axis a seed variant varies on must survive every builder that reads it.

WHAT THIS BUYS. `catalog_enrichment_agent.ingestion` writes each seed variant
with `options: [{"name": "Shade", "value": "Ruby Woo"}]`. The renderer already
exposed the variant LIST without it — a non-placeholder title was enough — but
it would not render the SELECTOR: `variantHasDisplayableChoice` reads `options`
first and an explicit `display_label` second, so #2073's real shades arrived as
an unlabelled list with no way to pick one.

TWO STORED SHAPES. The column is written by two lanes that never agreed:
`ingestion` writes a list of pairs, `routes/employee_products.py` writes a
`{name: value}` mapping and always has. A list-only reader does not merely miss
the mapping — it silently discards an axis the CSV lane already carried.

Parametrised over both modules on purpose. `routes/agent_sdk_fixed.py` holds a
second copy of this builder and both are routed, so a passthrough added to one
of them is a coin flip on which one serves a given request.

SCOPE. These two builders serve the search and checkout-rewrite lanes. The
deployed gateway reads `external_product_seeds` with its own SQL for the PDP
itself, so this file pins the passthrough, not the page.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional

import pytest


class _FakeReq:
    base_url = "https://api.pivota.cc/"


def _seed_row(variants: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": "seed:catalog_enrichment_agent_v1:b9fbf06a66abf0ed",
        "external_product_id": "mac-cosmetics:b9fbf06a66abf0ed",
        "market": "US",
        "title": "Retro Matte Lipstick",
        "image_url": "https://cdn.x/base.jpg",
        "price_amount": 24.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "destination_url": "https://brand.example/products/retro-matte-lipstick",
        "canonical_url": "https://brand.example/products/retro-matte-lipstick",
        "domain": "brand.example",
        "attached_product_key": "ext:mac-cosmetics-retro-matte-lipstick::a234aae1",
        "status": "active",
        "seed_data": {
            "brand": "MAC Cosmetics",
            "product_name": "Retro Matte Lipstick",
            "title": "Retro Matte Lipstick",
            "variants": variants,
        },
    }


def _variant(vid: str, shade: str, **over: Any) -> Dict[str, Any]:
    v = {
        "variant_id": vid,
        "id": vid,
        "sku": f"M0N{vid}",
        "title": shade,
        "currency": "USD",
        "price_currency": "USD",
        "price_amount": 24.0,
        "price": 24.0,
        "availability": "in_stock",
        "in_stock": True,
        "image_url": f"https://cdn.x/{shade}.jpg",
        "options": [{"name": "Shade", "value": shade}],
    }
    v.update(over)
    return v


async def _build(module_path: str, seed_row: Dict[str, Any],
                 monkeypatch: pytest.MonkeyPatch) -> Optional[Dict[str, Any]]:
    module = importlib.import_module(module_path)

    async def _gate_open(row, **kwargs):
        return (False, None)

    monkeypatch.setattr(module, "should_block_external_referral_runtime", _gate_open)
    return await module._build_external_seed_product(
        req=_FakeReq(), seed_row=seed_row, allowed_domains=["brand.example"],
    )


_MODULES = ["routes.agent_api", "routes.agent_sdk_fixed"]


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.asyncio
async def test_the_shade_axis_reaches_the_served_variant(module_path, monkeypatch):
    seed = _seed_row([_variant("1", "RubyWoo"), _variant("2", "Bronx")])
    product = await _build(module_path, seed, monkeypatch)
    assert product is not None, f"{module_path} builder returned None"
    variants = product["variants"]
    assert [v["options"] for v in variants] == [
        [{"name": "Shade", "value": "RubyWoo"}],
        [{"name": "Shade", "value": "Bronx"}],
    ]
    # the shade axis is displayable only WITH visual evidence
    assert all(v["image_url"] for v in variants)


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.asyncio
async def test_a_variant_with_no_axis_carries_no_options_key(module_path, monkeypatch):
    """Every lane that does not fold stays byte-identical — no empty `options`
    key appearing on variants that never had one.

    The length assertion is load-bearing: without it this passes vacuously
    against a builder that serves NO seed variants at all and falls back to its
    fabricated "Default" one, which a mutation check caught it doing."""
    seed = _seed_row([_variant("1", "RubyWoo", options=[]),
                      _variant("2", "Bronx", options=[])])
    product = await _build(module_path, seed, monkeypatch)
    assert [v["title"] for v in product["variants"]] == ["RubyWoo", "Bronx"]
    assert all("options" not in v for v in product["variants"])


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.asyncio
async def test_the_employee_csv_lanes_mapping_shape_is_read_too(module_path, monkeypatch):
    """`routes/employee_products.py` writes `options` as `{name: value}` and has
    since before the list form existed. A list-only reader silently discarded an
    axis that lane already had, leaving it unlabelled on the page while the
    answer sat in the row."""
    seed = _seed_row([_variant("1", "RubyWoo", options={"Shade": "Ruby Woo"}),
                      _variant("2", "Bronx", options={"Shade": "Bronx"})])
    product = await _build(module_path, seed, monkeypatch)
    assert [v["options"] for v in product["variants"]] == [
        [{"name": "Shade", "value": "Ruby Woo"}],
        [{"name": "Shade", "value": "Bronx"}],
    ]


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.asyncio
async def test_a_malformed_option_is_dropped_not_forwarded(module_path, monkeypatch):
    """A half-formed pair rendered as a selector reads as a broken choice. Only
    well-formed name/value pairs go to the page."""
    seed = _seed_row([
        _variant("1", "RubyWoo", options=[{"name": "Shade", "value": ""},
                                          {"value": "Bronx"},
                                          "Dangerous",
                                          {"name": "  Shade  ", "value": "  Ruby Woo  "}]),
    ])
    product = await _build(module_path, seed, monkeypatch)
    assert product["variants"][0]["options"] == [{"name": "Shade", "value": "Ruby Woo"}]


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.asyncio
async def test_a_variant_with_no_image_still_carries_its_axis(module_path, monkeypatch):
    """The renderer demands visual evidence on a SHADE axis, so a variant with no
    image of its own is not selectable there. That is the renderer's call to
    make on the data it is given — this builder must still hand over the axis
    rather than deciding for it, or the shade is unlabelled AND unselectable."""
    seed = _seed_row([_variant("1", "RubyWoo", image_url=None),
                      _variant("2", "Bronx")])
    product = await _build(module_path, seed, monkeypatch)
    variants = product["variants"]
    assert "image_url" not in variants[0]
    assert variants[0]["options"] == [{"name": "Shade", "value": "RubyWoo"}]
