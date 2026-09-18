"""agent_pdp_view.offers: a seller that can sell ranks ahead of one that says it cannot.

`aggregate_offers` sorted by (is_primary, price, merchant_id) and then cut to OFFER_TOP_N, so a
cheaper or primary out-of-stock offer led the stored list and the cut could drop every in-stock
one. Rebuilt read-only from prod on 2026-09-18: 39 of 12,643 rows reorder, 26 led with an
out-of-stock offer ahead of a sellable one, 13 change which offers survive the cut, and on one
(Kylie Matte Liquid Lipstick, 39 shade offers) the five stored were all sold out, so the served
`is_buy_pick` was a sold-out shade.

The order is (known-unavailable, is_primary, price, merchant_id), all applied before the cut.
"Known-unavailable" is the ONE vocabulary offers.resolve ranks by (#2218), and unknown
availability ranks with in-stock, by price. Pure functions only; no SQL changed.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import agent_pdp_view_assembler as assembler  # noqa: E402
from services import offer_buyability  # noqa: E402
from services.offer_buyability import (  # noqa: E402
    OFFER_UNAVAILABLE_AVAILABILITIES,
    annotate_offer_buyability,
    availability_is_known_unavailable,
)


def _offer(merchant_id: str, price: str, availability: Optional[str]) -> Dict[str, Any]:
    """A catalog_offers row as fetch_offers_for_keys returns it."""
    return {
        "merchant_id": merchant_id,
        "merchant_name": merchant_id.upper(),
        "availability": availability,
        "currency": "USD",
        "list_price": Decimal(price),
        "market": "US",
    }


def _top(offers: List[Dict[str, Any]], primary: Optional[str] = None) -> List[Dict[str, Any]]:
    _, _, _, _, top = assembler.aggregate_offers(
        offers, primary_merchant_id=primary, merchant_url_by_id={}
    )
    return top


def _ids(top: List[Dict[str, Any]]) -> List[str]:
    return [o["merchant_id"] for o in top]


def test_a_cheaper_sold_out_seller_ranks_behind_an_in_stock_one() -> None:
    top = _top([_offer("eyurs", "13.00", "out_of_stock"), _offer("sokoglam", "19.50", "in_stock")])
    assert _ids(top) == ["sokoglam", "eyurs"]


def test_a_sold_out_primary_ranks_behind_an_in_stock_retailer() -> None:
    """The stock tier sits ABOVE is_primary. Measured shapes: The Ordinary (brand, primary) out
    of stock over ulta.com in stock at the same $19; COSRX (primary) out of stock at $15 over
    stylekorean in stock at $6.30. The primary is kept, only moved."""
    top = _top(
        [_offer("the_ordinary", "19.00", "out_of_stock"), _offer("ulta", "19.00", "in_stock")],
        primary="the_ordinary",
    )
    assert _ids(top) == ["ulta", "the_ordinary"]
    assert [o["is_primary"] for o in top] == [False, True]


def test_the_primary_still_leads_within_its_stock_group_even_when_pricier() -> None:
    top = _top(
        [
            _offer("cheap_retailer", "5.00", "in_stock"),
            _offer("brand", "30.00", "in_stock"),
            _offer("cheap_sold_out", "1.00", "out_of_stock"),
            _offer("brand", "2.00", "out_of_stock"),
        ],
        primary="brand",
    )
    assert [(o["merchant_id"], o["price"]) for o in top] == [
        ("brand", 30.0),
        ("cheap_retailer", 5.0),
        ("brand", 2.0),
        ("cheap_sold_out", 1.0),
    ]


def test_price_ascends_within_each_stock_group() -> None:
    top = _top(
        [
            _offer("c", "30.00", "out_of_stock"),
            _offer("b", "20.00", "in_stock"),
            _offer("a", "10.00", "out_of_stock"),
            _offer("d", "15.00", "in_stock"),
        ]
    )
    assert [(o["merchant_id"], o["price"]) for o in top] == [
        ("d", 15.0),
        ("b", 20.0),
        ("a", 10.0),
        ("c", 30.0),
    ]


def test_the_cut_keeps_an_in_stock_offer_that_five_cheaper_sold_out_ones_used_to_push_out() -> None:
    """The Kylie shape: the sort must run BEFORE the cut. The aggregates still cover every
    offer — count, and a price range that includes the sold-out prices."""
    offers = [_offer(f"sold_out_{i}", f"{10 + i}.00", "out_of_stock") for i in range(5)]
    offers.append(_offer("in_stock", "99.00", "in_stock"))
    currency, price_min, price_max, count, top = assembler.aggregate_offers(
        offers, primary_merchant_id=None, merchant_url_by_id={}
    )
    assert len(top) == assembler.OFFER_TOP_N == 5
    assert _ids(top) == ["in_stock", "sold_out_0", "sold_out_1", "sold_out_2", "sold_out_3"]
    assert count == 6
    assert (currency, price_min, price_max) == ("USD", Decimal("10.00"), Decimal("99.00"))


def test_the_served_buy_pick_is_in_stock_once_the_cut_keeps_an_in_stock_offer() -> None:
    """End to end through the serve-time annotation (routes/agent_pdp_v1 -> annotate_offer_buyability):
    before this change the pick could only be one of the five sold-out offers."""
    offers = [_offer("brand", "21.00", "out_of_stock") for _ in range(9)]
    offers.append(_offer("brand", "21.00", "in_stock"))
    served = annotate_offer_buyability(_top(offers, primary="brand"), "US")
    picks = [o for o in served if o["is_buy_pick"]]
    assert len(picks) == 1 and picks[0]["availability"] == "in_stock"


@pytest.mark.parametrize("availability", ["unknown", None, "", "   ", "preorder", "limited"])
def test_unknown_availability_ranks_with_in_stock_by_price(availability: Optional[str]) -> None:
    """No stock statement is not a statement against the seller (#2218's decision): the
    unstated offer is neither demoted below an in-stock one nor promoted above a cheaper one."""
    top = _top(
        [
            _offer("in_stock_dear", "20.00", "in_stock"),
            _offer("sold_out_cheapest", "1.00", "out_of_stock"),
            _offer("unstated", "15.00", availability),
            _offer("in_stock_cheap", "10.00", "in_stock"),
        ]
    )
    assert _ids(top) == ["in_stock_cheap", "unstated", "in_stock_dear", "sold_out_cheapest"]


@pytest.mark.parametrize(
    "spelling",
    sorted(OFFER_UNAVAILABLE_AVAILABILITIES)
    + [s.upper() for s in sorted(OFFER_UNAVAILABLE_AVAILABILITIES)]
    + [" Out_Of_Stock ", "\tSOLD_OUT\n"],
)
def test_every_unavailable_spelling_is_demoted(spelling: str) -> None:
    top = _top([_offer("cannot_sell", "1.00", spelling), _offer("can_sell", "50.00", "in_stock")])
    assert _ids(top) == ["can_sell", "cannot_sell"]


def test_the_vocabulary_is_exactly_the_one_offers_resolve_ranks_by() -> None:
    """Pinned by value so a widening is a reviewed change. Adding a spelling here also changes
    offers.resolve's SQL ORDER BY and `in_stock` flag, which is the point of there being one."""
    assert OFFER_UNAVAILABLE_AVAILABILITIES == frozenset(
        {"out_of_stock", "outofstock", "sold_out", "soldout", "unavailable"}
    )
    assert (
        assembler.availability_is_known_unavailable
        is offer_buyability.availability_is_known_unavailable
    )


def test_offers_resolve_binds_the_same_set() -> None:
    import routes.agent_shop_gateway as gateway

    assert gateway.OFFER_UNAVAILABLE_AVAILABILITIES is OFFER_UNAVAILABLE_AVAILABILITIES


@pytest.mark.parametrize(
    "availability, expected",
    [
        ("out_of_stock", True),
        (" SOLDOUT ", True),
        ("\tUnavailable\n", True),
        ("in_stock", False),
        ("unknown", False),
        ("preorder", False),
        ("", False),
        (None, False),
    ],
)
def test_known_unavailable_needs_an_explicit_statement(
    availability: Optional[str], expected: bool
) -> None:
    assert availability_is_known_unavailable(availability) is expected


def test_an_internal_offer_is_not_exempt_here() -> None:
    """offers.resolve never demotes an internal offer on its flag, because that lane's internal
    offers passed a variant eligibility gate first. This view has no such gate: an internal
    offer's `availability` is the quantity-only string catalog_sync wrote, and it is the only
    stock statement here, so it ranks like any other."""
    internal = {**_offer("internal_store", "5.00", "out_of_stock"), "is_first_party": True}
    top = _top([internal, _offer("retailer", "9.00", "in_stock")], primary="internal_store")
    assert _ids(top) == ["retailer", "internal_store"]
