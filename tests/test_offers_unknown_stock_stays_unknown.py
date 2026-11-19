"""Unknown or missing availability is never served as in stock (flag OFFERS_UNKNOWN_STOCK_STAYS_UNKNOWN_ENABLED)."""
from __future__ import annotations

import pytest

import routes.agent_shop_gateway as gateway

FLAG = gateway.UNKNOWN_STOCK_STAYS_UNKNOWN_FLAG
CASES = [  # (availability, flag off, flag on)
    ("in_stock", True, True),
    ("InStock", True, True),
    ("https://schema.org/InStock", True, True),
    ("out_of_stock", False, False),
    ("sold out", False, False),
    ("unknown", True, None),
    ("", True, None),
    (None, True, None),
    ("check_at_store", True, None),
]


@pytest.fixture(params=["off", "on"])
def flag(request, monkeypatch):
    if request.param == "on":
        monkeypatch.setenv(FLAG, "true")
    else:
        monkeypatch.delenv(FLAG, raising=False)
    return request.param


def test_the_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    assert gateway._unknown_stock_stays_unknown() is False
    for value in ("1", "yes", "TRUE ", "on"):
        monkeypatch.setenv(FLAG, value)
        assert gateway._unknown_stock_stays_unknown() is (value.strip().lower() == "true")


@pytest.mark.parametrize(("availability", "off", "on"), CASES)
def test_catalog_arm_offer(flag, availability, off, on):
    row = {"offer_id": "o1", "merchant_id": "m1", "price_amount": 10, "currency": "USD", "availability": availability,
           "destination_url": "https://shop.example/products/x"}
    for group_mode in (True, False):
        offer = gateway._catalog_offer_entry(row, group_mode=group_mode, redirect_url=None)
        expected = off if flag == "off" else on
        if group_mode is False and availability == "sold out" and flag == "off":
            expected = True      # today, outside a group, the arm's own vocabulary misses "sold out" (flag on fixes it)
        assert offer["in_stock"] is expected, (availability, group_mode)
        assert offer["currency"] == "USD"            # currency untouched


@pytest.mark.parametrize("currency", ["USD", "SGD", "GBP", "EUR", None])
def test_currency_is_passed_through_unchanged_with_the_flag_on(monkeypatch, currency):
    monkeypatch.setenv(FLAG, "true")
    row = {"offer_id": "o1", "availability": "unknown", "price_amount": 9, "currency": currency}
    for group_mode in (True, False):
        assert gateway._catalog_offer_entry(row, group_mode=group_mode, redirect_url=None)["currency"] == currency


@pytest.mark.parametrize(("availability", "off", "on"), CASES)
def test_external_seed_product(flag, availability, off, on):
    row = {"id": "s1", "title": "Tinted Balm", "canonical_url": "https://shop.example/products/x",
           "destination_url": "https://shop.example/products/x", "price_amount": 12, "price_currency": "USD"}
    if availability is not None:
        row["availability"] = availability
    product = gateway._external_seed_to_shop_product(row=row, seed_data={}, redirect_url=None)
    in_stock = off if flag == "off" else on
    if flag == "off" and availability in ("sold out", "https://schema.org/InStock", "InStock"):
        in_stock = {"sold out": True, "https://schema.org/InStock": True, "InStock": True}[availability]
    assert product["in_stock"] is in_stock, availability
    assert product["inventory_quantity"] == ({True: 999, False: 0, None: None}[in_stock])
    assert product["currency"] == "USD"


@pytest.mark.parametrize(("candidate", "off", "on"), [
    ({"availability": "in_stock"}, "in_stock", "in_stock"),
    ({"availability": "out_of_stock"}, "out_of_stock", "out_of_stock"),
    ({"in_stock": True}, "in_stock", "in_stock"),
    ({"in_stock": False}, "out_of_stock", "out_of_stock"),
    ({}, "in_stock", None),
    ({"in_stock": None}, "in_stock", None),
])
def test_prefetched_candidate_availability(flag, candidate, off, on):
    assert gateway._prefetched_candidate_availability(candidate) == (off if flag == "off" else on)


def test_an_unknown_offer_is_never_best_priced_as_sold_out_and_never_counted_in_stock(monkeypatch):
    """Readers already treat a non-bool in_stock by its availability text: unknown is not out of stock (it can
    still rank), and the in-stock-only pivot (bool(in_stock)) leaves it out."""
    monkeypatch.setenv(FLAG, "true")
    offer = gateway._catalog_offer_entry({"offer_id": "o1", "availability": None, "price_amount": 5, "currency": "USD"},
                                         group_mode=True, redirect_url=None)
    assert offer["in_stock"] is None
    assert not bool(offer["in_stock"])
    assert not (offer["in_stock"] is False or gateway._availability_says_out_of_stock(offer["availability"]))
