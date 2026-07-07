"""Unit tests for the Shopify webhook payload PII sanitizer (audit fix #6)."""


def _realistic_orders_paid_payload():
    # Trimmed but representative orders/paid payload shape from Shopify.
    return {
        "id": 820982911946154508,
        "order_number": 1001,
        "financial_status": "paid",
        "currency": "USD",
        "total_price": "49.99",
        "total_price_set": {"shop_money": {"amount": "49.99", "currency_code": "USD"}},
        "created_at": "2026-07-07T10:00:00-04:00",
        "note_attributes": [
            {"name": "pivota_click_id", "value": "clk_abc123"},
        ],
        "customer": {
            "id": 115310627314723954,
            "email": "jane@example.com",
            "first_name": "Jane",
            "last_name": "Doe",
        },
        "email": "jane@example.com",
        "contact_email": "jane@example.com",
        "phone": "+15551234567",
        "customer_locale": "en-US",
        "browser_ip": "203.0.113.5",
        "customer_url": "https://shop.example.com/account",
        "billing_address": {"address1": "1 Main St", "city": "Ottawa", "zip": "K1A0B1"},
        "shipping_address": {"address1": "1 Main St", "city": "Ottawa", "zip": "K1A0B1"},
        "line_items": [
            {
                "id": 466157049,
                "quantity": 1,
                "price": "49.99",
                "sku": "SKU-1",
                "destination_location": {"address1": "1 Main St", "zip": "K1A0B1"},
                "origin_location": {"address1": "Warehouse", "zip": "10001"},
            }
        ],
    }


def test_sanitizer_strips_pii_keeps_financial_and_note_attributes():
    from services.shopify_webhook_ingest import _strip_order_payload_pii

    out = _strip_order_payload_pii(_realistic_orders_paid_payload())

    # PII removed
    for key in (
        "customer",
        "email",
        "contact_email",
        "phone",
        "customer_locale",
        "browser_ip",
        "customer_url",
        "billing_address",
        "shipping_address",
    ):
        assert key not in out, f"{key} should have been stripped"

    # Nested line-item location blobs removed
    li = out["line_items"][0]
    assert "destination_location" not in li
    assert "origin_location" not in li

    # Non-PII preserved
    assert out["id"] == 820982911946154508
    assert out["order_number"] == 1001
    assert out["financial_status"] == "paid"
    assert out["currency"] == "USD"
    assert out["total_price"] == "49.99"
    assert out["total_price_set"]["shop_money"]["amount"] == "49.99"
    assert out["created_at"] == "2026-07-07T10:00:00-04:00"
    assert out["line_items"][0]["sku"] == "SKU-1"
    assert out["line_items"][0]["price"] == "49.99"

    # note_attributes MUST survive — attribution depends on pivota_click_id
    assert out["note_attributes"] == [{"name": "pivota_click_id", "value": "clk_abc123"}]

    # Marker stamped
    assert out["pii_stripped"] is True


def test_sanitizer_ignores_non_dict():
    from services.shopify_webhook_ingest import _strip_order_payload_pii

    assert _strip_order_payload_pii(["not", "a", "dict"]) == ["not", "a", "dict"]
    assert _strip_order_payload_pii(None) is None


def test_topic_gate_only_order_family():
    from services.shopify_webhook_ingest import _topic_carries_pii

    assert _topic_carries_pii("orders/paid")
    assert _topic_carries_pii("orders/create")
    assert _topic_carries_pii("refunds/create")
    assert _topic_carries_pii("returns/create")
    assert not _topic_carries_pii("app/uninstalled")
    assert not _topic_carries_pii("products/update")
    assert not _topic_carries_pii("fulfillments/create")
    assert not _topic_carries_pii(None)
