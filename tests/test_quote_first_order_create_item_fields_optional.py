from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from models.order import CreateOrderRequest


def _base_payload():
    return {
        "merchant_id": "merch_1",
        "customer_email": "test@example.com",
        "items": [{"product_id": "p1", "variant_id": "v1", "quantity": 1}],
        "shipping_address": {
            "name": "Test Buyer",
            "address_line1": "Test Street 1",
            "address_line2": "",
            "city": "Berlin",
            "state": "BE",
            "postal_code": "10115",
            "country": "DE",
            "phone": "",
        },
        "currency": "EUR",
    }


def test_quote_first_allows_minimal_items_without_prices():
    payload = _base_payload()
    payload["quote_id"] = "q_test"
    req = CreateOrderRequest(**payload)
    assert req.quote_id == "q_test"
    assert req.items[0].product_title is None
    assert req.items[0].unit_price is None
    assert req.items[0].subtotal is None


def test_non_quote_requires_product_title_and_unit_price():
    payload = _base_payload()
    with pytest.raises(Exception) as e:
        CreateOrderRequest(**payload)
    assert "product_title" in str(e.value) or "unit_price" in str(e.value)

