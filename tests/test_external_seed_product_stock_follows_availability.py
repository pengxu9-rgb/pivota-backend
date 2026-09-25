"""
An external seed's product-level stock claim is the seed's own, not a constant.

THE DEFECT. Both routed seed builders hard-coded `in_stock: True,
inventory_quantity: 999` for every seed whose catalog facts the referral gate
accepts, whatever `external_product_seeds.availability` said. agent_v2 publishes
that boolean as `offers[].availability.in_stock`, and the gateway lets a boolean
`in_stock` outrank every other stock signal, so a seed stored `out_of_stock` was
advertised in stock.

UNKNOWN IS NOT IN STOCK. `_availability_to_in_stock` (the variant loop's parser)
reads an absent value as in stock. The product level uses the explicit-signal
parser instead and serves an unknown claim as `availability: "unknown"` with NO
boolean — the shape the live-verification branch already serves.

Parametrised over both builders: `routes/agent_sdk_fixed.py` holds a second
copy and both are routed.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional

import pytest


class _FakeReq:
    base_url = "https://api.pivota.cc/"


_MODULES = ["routes.agent_api", "routes.agent_sdk_fixed"]


def _seed_row(
    *,
    availability: Any,
    variants: Optional[List[Dict[str, Any]]] = None,
    seed_data_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    seed_data: Dict[str, Any] = {
        "brand": "Round Lab",
        "title": "Round Lab Sheet Mask Sampler 9pc",
        "variants": variants or [],
    }
    seed_data.update(seed_data_extra or {})
    return {
        "id": "seed:catalog_enrichment_agent_v1:85dd4c56da58a259",
        "external_product_id": "round-lab:85dd4c56da58a259",
        "market": "US",
        "title": "Round Lab Sheet Mask Sampler 9pc",
        "price_amount": 18.0,
        "price_currency": "USD",
        "availability": availability,
        "destination_url": "https://roundlab.com/products/sheet-mask-sampler",
        "canonical_url": "https://roundlab.com/products/sheet-mask-sampler",
        "domain": "roundlab.com",
        "status": "active",
        "seed_data": seed_data,
    }


def _variant(vid: str, availability: Any) -> Dict[str, Any]:
    return {
        "variant_id": vid,
        "title": f"Variant {vid}",
        "price_amount": 18.0,
        "price_currency": "USD",
        "availability": availability,
    }


async def _build(
    module_path: str,
    seed_row: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    live_verification: bool = False,
) -> Dict[str, Any]:
    module = importlib.import_module(module_path)
    status = None
    if live_verification:
        status = type(
            "GateStatus",
            (),
            {
                "status": "blocked",
                "gating_policy_version": "external_referral_v1",
                # What the round-lab seed actually carried in prod on 2026-09-25.
                "blocker_anomaly_types": ["destination_never_verified", "stale_snapshot"],
                "review_anomaly_types": [],
            },
        )()

    async def _gate(row, **kwargs):
        return (False, status)

    monkeypatch.setattr(module, "should_block_external_referral_runtime", _gate)
    product = await module._build_external_seed_product(
        req=_FakeReq(), seed_row=seed_row, allowed_domains=["roundlab.com"],
    )
    assert product is not None, f"{module_path} builder returned None"
    assert product["commerce_verification"]["required"] is live_verification
    return product


def _stock(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: d[k] for k in ("in_stock", "inventory_quantity", "availability") if k in d}


OUT = {"in_stock": False, "inventory_quantity": 0, "availability": "out_of_stock"}
IN = {"in_stock": True, "inventory_quantity": 999}
UNKNOWN = {"availability": "unknown"}


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.asyncio
async def test_an_out_of_stock_seed_is_not_advertised_in_stock(module_path, monkeypatch):
    seed = _seed_row(
        availability="out_of_stock",
        variants=[_variant("1", "out_of_stock"), _variant("2", "out_of_stock")],
    )
    product = await _build(module_path, seed, monkeypatch)
    assert _stock(product) == OUT
    assert [v["in_stock"] for v in product["variants"]] == [False, False]


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.asyncio
async def test_the_synthesised_default_variant_carries_the_same_claim(module_path, monkeypatch):
    product = await _build(module_path, _seed_row(availability="out_of_stock"), monkeypatch)
    assert _stock(product) == OUT
    assert len(product["variants"]) == 1
    assert _stock(product["variants"][0]) == OUT


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.asyncio
async def test_an_in_stock_seed_keeps_todays_shape(module_path, monkeypatch):
    seed = _seed_row(availability="in_stock", variants=[_variant("1", "in_stock")])
    product = await _build(module_path, seed, monkeypatch)
    # No `availability` key added: the 10,636 in-stock seeds serve byte-identical stock fields.
    assert _stock(product) == IN


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.asyncio
async def test_a_seed_with_no_stock_signal_is_unknown_not_in_stock(module_path, monkeypatch):
    product = await _build(module_path, _seed_row(availability=None), monkeypatch)
    assert _stock(product) == UNKNOWN
    assert "in_stock" not in product
    assert _stock(product["variants"][0]) == UNKNOWN


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.parametrize(
    "variant_availability, expected",
    [
        (["out of stock", "Sold Out"], OUT),
        (["in stock", "out_of_stock"], IN),
        # One variant unclassifiable: not every variant is known out, so no claim.
        (["out_of_stock", None], UNKNOWN),
    ],
)
@pytest.mark.asyncio
async def test_without_a_column_the_explicit_variants_decide(
    module_path, variant_availability, expected, monkeypatch
):
    seed = _seed_row(
        availability="",
        variants=[_variant(str(i), a) for i, a in enumerate(variant_availability)],
    )
    product = await _build(module_path, seed, monkeypatch)
    assert _stock(product) == expected


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.parametrize(
    "column, variant_availability",
    [
        ("out_of_stock", ["out_of_stock", "in_stock"]),
        ("in_stock", ["out_of_stock", "out_of_stock"]),
    ],
)
@pytest.mark.asyncio
async def test_a_column_the_variants_contradict_is_unknown(
    module_path, column, variant_availability, monkeypatch
):
    seed = _seed_row(
        availability=column,
        variants=[_variant(str(i), a) for i, a in enumerate(variant_availability)],
    )
    product = await _build(module_path, seed, monkeypatch)
    assert _stock(product) == UNKNOWN


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.parametrize(
    "extra",
    [
        {"availability": "sold out"},
        {"snapshot": {"availability": "out_of_stock"}},
    ],
)
@pytest.mark.asyncio
async def test_seed_data_availability_is_the_last_fallback(module_path, extra, monkeypatch):
    product = await _build(
        module_path, _seed_row(availability=None, seed_data_extra=extra), monkeypatch
    )
    assert _stock(product) == OUT


@pytest.mark.parametrize("module_path", _MODULES)
@pytest.mark.asyncio
async def test_the_live_verification_branch_still_withholds_stock(module_path, monkeypatch):
    seed = _seed_row(
        availability="in_stock", variants=[_variant("1", "in_stock")]
    )
    product = await _build(module_path, seed, monkeypatch, live_verification=True)
    assert _stock(product) == UNKNOWN
    assert product["buyable"] is False
    assert product["checkout_ready"] is False
    assert product["variants"] == []


@pytest.mark.asyncio
async def test_in_stock_only_and_agent_v2_read_the_seeds_own_claim(monkeypatch):
    import routes.agent_api as agent_api
    from routes.agent_v2 import _canonicalize_search_product

    def _passes(product):
        return agent_api._passes_explicit_commerce_filters(
            product, in_stock_only=True, min_price=None, max_price=None
        )

    out = await _build(
        "routes.agent_api", _seed_row(availability="out_of_stock"), monkeypatch
    )
    in_ = await _build("routes.agent_api", _seed_row(availability="in_stock"), monkeypatch)
    unknown = await _build("routes.agent_api", _seed_row(availability=None), monkeypatch)

    assert _passes(out) is False
    assert _passes(in_) is True
    # Unknown stays discoverable without in_stock_only, but never claims a strict match.
    assert _passes(unknown) is False

    assert [o["availability"]["in_stock"] for o in _canonicalize_search_product(out)["offers"]] == [False]
    assert [o["availability"]["in_stock"] for o in _canonicalize_search_product(in_)["offers"]] == [True]
