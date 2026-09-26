"""Each v2 search offer is priced from its own variant.

_canonicalize_search_product built one offer per variant but priced every offer
from the product-level price (for external seeds, the FIRST variant's price via
_seed_primary_price). Measured 2026-09-24: eyurs.com Round Lab mask variants
[2.5, 18.0] were served on POST /agent/v2/products/search -- and through the
gateway to MCP search_catalog -- as two offers both priced "2.5".

The withheld-price rules from tests/test_agent_v2_withheld_price.py still hold
per offer: a marked row is null everywhere, absent is null, a real 0 is "0".
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from routes.agent_v2 import _canonicalize_search_product  # noqa: E402

WITHHELD_MARK = {
    "required": True,
    "status": "live_quote_required",
    "reasons": ["stale_commerce_facts"],
    "price_trusted": False,
    "availability_trusted": False,
}


def _variant(variant_id, **fields):
    # The shape agent_api's external-seed builder emits per variant.
    return {
        "id": f"round-lab:mask:{variant_id}",
        "variant_id": variant_id,
        "title": f"Variant {variant_id}",
        **fields,
    }


def _two_variant_row(**overrides):
    row = {
        "id": "round-lab:mask",
        "product_id": "round-lab:mask",
        "merchant_id": "external_seed",
        "platform": "external",
        "title": "Round Lab Mask",
        "source": "external_seed",
        # Product-level price = first variant's, as _seed_primary_price sets it.
        "price": 2.5,
        "currency": "USD",
        "in_stock": True,
        "inventory_quantity": 999,
        "variants": [
            _variant("single", price=2.5, currency="USD", in_stock=True, inventory_quantity=999),
            _variant("pack10", price=18.0, currency="USD", in_stock=False, inventory_quantity=0),
        ],
    }
    row.update(overrides)
    return row


def _offers_by_variant(out):
    return {offer["variant_id"]: offer for offer in out["offers"]}


def test_two_variants_get_two_distinct_offer_prices():
    offers = _offers_by_variant(_canonicalize_search_product(_two_variant_row()))
    assert offers["single"]["price"] == "2.5"
    assert offers["pack10"]["price"] == "18.0"


def test_each_offer_carries_its_own_variant_stock():
    # The external builder hardcodes product-level in_stock True; the variant's
    # own availability is the fact.
    offers = _offers_by_variant(_canonicalize_search_product(_two_variant_row()))
    assert offers["single"]["availability"] == {"in_stock": True, "inventory_quantity": 999}
    assert offers["pack10"]["availability"] == {"in_stock": False, "inventory_quantity": 0}


def test_each_offer_carries_its_own_variant_currency():
    row = _two_variant_row(
        price=1000,
        currency="JPY",
        variants=[
            _variant("a", price=1000, currency="JPY"),
            _variant("b", price=12.0, currency="USD"),
        ],
    )
    offers = _offers_by_variant(_canonicalize_search_product(row))
    assert (offers["a"]["price"], offers["a"]["currency"]) == ("1000", "JPY")
    assert (offers["b"]["price"], offers["b"]["currency"]) == ("12.0", "USD")


def test_a_marked_row_is_null_on_every_offer_even_with_variant_prices():
    out = _canonicalize_search_product(_two_variant_row(commerce_verification=dict(WITHHELD_MARK)))
    assert len(out["offers"]) == 2
    for offer in out["offers"]:
        assert offer["price"] is None
        assert offer["availability"]["in_stock"] is None


def test_a_variant_without_a_price_falls_back_to_the_product_price_and_currency():
    row = _two_variant_row(
        price=9.5,
        currency="GBP",
        variants=[
            _variant("priced", price=18.0, currency="USD"),
            _variant("unpriced"),
            _variant("null_priced", price=None, currency="EUR"),
        ],
    )
    offers = _offers_by_variant(_canonicalize_search_product(row))
    assert (offers["priced"]["price"], offers["priced"]["currency"]) == ("18.0", "USD")
    assert (offers["unpriced"]["price"], offers["unpriced"]["currency"]) == ("9.5", "GBP")
    # Currency follows the price it was used with, not a price-less variant.
    assert (offers["null_priced"]["price"], offers["null_priced"]["currency"]) == ("9.5", "GBP")


def test_a_variant_without_a_price_on_a_row_without_one_is_null_not_zero():
    row = _two_variant_row(variants=[_variant("unpriced")])
    del row["price"]
    out = _canonicalize_search_product(row)
    assert out["offers"][0]["price"] is None


def test_a_real_zero_variant_price_stays_zero():
    row = _two_variant_row(variants=[_variant("free", price=0, currency="USD"), _variant("paid", price=5.0)])
    offers = _offers_by_variant(_canonicalize_search_product(row))
    assert offers["free"]["price"] == "0"
    assert offers["paid"]["price"] == "5.0"


def test_a_variant_without_in_stock_falls_back_to_the_product_stock():
    # An internal StandardProductVariant carries inventory_quantity (default 0,
    # even when untracked) but no in_stock bool: that is not a stock claim.
    row = _two_variant_row(
        in_stock=True,
        inventory_quantity=None,
        variants=[_variant("internal", price=20.0, inventory_quantity=0, available=True)],
    )
    offer = _canonicalize_search_product(row)["offers"][0]
    assert offer["availability"] == {"in_stock": True, "inventory_quantity": None}


def test_the_public_variants_shape_is_unchanged():
    out = _canonicalize_search_product(_two_variant_row())
    for variant in out["variants"]:
        assert set(variant) == {"variant_id", "variant_attributes"}
