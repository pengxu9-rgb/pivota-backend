"""Regression: the find_products_multi cache prefetch must parse `databases`
Record rows, not just dicts.

databases.backends.postgres.Record is a Sequence whose __getattr__ delegates
to self._mapping.get(name), so `row.get` resolves to None (no such column)
and `row.get("merchant_id")` raises TypeError: 'NoneType' object is not
callable. The cross-merchant fast-path prefetch did exactly that inside a
broad try/except, killing the fast path on the first row of every query.
"""
from __future__ import annotations

import json

import pytest

from routes.agent_shop_gateway import (
    _order_row_merchant_items,
    _products_cache_row_candidate,
)


class FakeRecord:
    """Mimics databases.backends.postgres.Record attribute/key semantics."""

    def __init__(self, mapping: dict):
        object.__setattr__(self, "_mapping_dict", mapping)

    @property
    def _mapping(self):
        return self._mapping_dict

    def keys(self):
        return self._mapping_dict.keys()

    def __getitem__(self, key):
        return self._mapping_dict[key]

    def __iter__(self):
        return iter(self._mapping_dict.keys())

    def __len__(self):
        return len(self._mapping_dict)

    def __getattr__(self, name):
        return self._mapping_dict.get(name)


PRODUCT = {"product_id": "p1", "title": "AeroFlex joggers", "price": 42.0}


def test_fake_record_reproduces_the_record_get_trap():
    row = FakeRecord({"merchant_id": "merch_efbc46b4619cfbdf", "product_data": PRODUCT})
    with pytest.raises(TypeError):
        row.get("merchant_id")


def test_candidate_parsed_from_record_row():
    row = FakeRecord({"merchant_id": "merch_efbc46b4619cfbdf", "product_data": PRODUCT})
    mid, data = _products_cache_row_candidate(row)
    assert mid == "merch_efbc46b4619cfbdf"
    assert data == PRODUCT


def test_candidate_parsed_from_record_row_with_json_string_payload():
    row = FakeRecord(
        {"merchant_id": "merch_efbc46b4619cfbdf", "product_data": json.dumps(PRODUCT)}
    )
    mid, data = _products_cache_row_candidate(row)
    assert mid == "merch_efbc46b4619cfbdf"
    assert data == PRODUCT


def test_candidate_parsed_from_plain_dict_row():
    mid, data = _products_cache_row_candidate(
        {"merchant_id": "merch_1", "product_data": PRODUCT}
    )
    assert mid == "merch_1"
    assert data == PRODUCT


def test_unusable_rows_are_rejected_not_raised():
    assert _products_cache_row_candidate({}) == ("", None)
    assert _products_cache_row_candidate({"merchant_id": "m", "product_data": "{bad"}) == ("m", None)
    assert _products_cache_row_candidate({"merchant_id": "m", "product_data": 7}) == ("m", None)
    assert _products_cache_row_candidate(None) == ("", None)


ORDER_ITEMS = [{"product_id": "p1", "product_title": "AeroFlex joggers", "quantity": 2}]


def test_order_items_parsed_from_record_row():
    row = FakeRecord({"merchant_id": "merch_efbc46b4619cfbdf", "items": ORDER_ITEMS})
    mid, items = _order_row_merchant_items(row)
    assert mid == "merch_efbc46b4619cfbdf"
    assert items == ORDER_ITEMS


def test_order_items_parsed_from_record_row_with_json_string_payload():
    row = FakeRecord(
        {"merchant_id": "merch_efbc46b4619cfbdf", "items": json.dumps(ORDER_ITEMS)}
    )
    mid, items = _order_row_merchant_items(row)
    assert mid == "merch_efbc46b4619cfbdf"
    assert items == ORDER_ITEMS


def test_order_items_parsed_from_plain_dict_row():
    mid, items = _order_row_merchant_items({"merchant_id": "m1", "items": ORDER_ITEMS})
    assert mid == "m1"
    assert items == ORDER_ITEMS


def test_unusable_order_rows_are_rejected_not_raised():
    assert _order_row_merchant_items({}) == (None, None)
    assert _order_row_merchant_items({"merchant_id": "m", "items": "{bad"}) == ("m", None)
    assert _order_row_merchant_items({"merchant_id": "m", "items": {"not": "a list"}}) == ("m", None)
    assert _order_row_merchant_items(None) == (None, None)
    row = FakeRecord({"merchant_id": "m", "items": None})
    assert _order_row_merchant_items(row) == ("m", None)


def test_no_dead_record_get_idiom_left_in_gateway():
    """Guard: `row.get(...) if isinstance(row, dict) else None` silently skips
    every Record row database.fetch_all/fetch_one returns in production. Six
    personalization/fallback lanes were dead because of it; keep it out."""
    import inspect

    import routes.agent_shop_gateway as gateway

    assert "if isinstance(row, dict) else None" not in inspect.getsource(gateway)
