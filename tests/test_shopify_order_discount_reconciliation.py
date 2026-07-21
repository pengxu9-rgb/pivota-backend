from decimal import Decimal


from routes import order_routes


def _pricing_quote_meta():
    return {
        "quote_id": "q_123",
        "pricing": {"total": "95.00", "discount_total": "5.00"},
        "line_items": [
            {
                "product_id": "prod_1",
                "variant_id": "var_1",
                "quantity": 1,
                "unit_price_original": "100.00",
                "unit_price_effective": "95.00",
                "line_discount_total": "5.00",
            }
        ],
        "promotion_lines": [
            {"source": "shopify", "method": "code", "code": "SAVE5", "amount": "-5.00"},
        ],
        "discount_evidence": {
            "pricing_confidence": "authoritative",
            "codes": [{"code": "SAVE5", "applicable": True, "source": "shopify_storefront_cart"}],
            "applications": [
                {"source": "shopify", "method": "code", "discount_class": "product", "code": "SAVE5", "amount": "-5.00"}
            ],
        },
    }


def test_shopify_order_discount_codes_and_annotations_from_quote_evidence():
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["line_items"] = []
    pricing_quote_meta["discount_evidence"]["applications"][0]["discount_class"] = "order"
    pricing_quote_meta["promotion_lines"][0]["discount_class"] = "order"

    assert order_routes._build_shopify_order_discount_codes(pricing_quote_meta) == [
        {"code": "SAVE5", "amount": "5.00", "type": "fixed_amount"}
    ]

    tags, note_attributes = order_routes._build_shopify_discount_order_annotations(
        order_id="ord_123",
        pricing_quote_meta=pricing_quote_meta,
    )

    assert "pivota-quote-id-q-123" in tags
    assert any(tag.startswith("pivota-discount-evidence-") for tag in tags)
    assert all(":" not in tag and "_" not in tag and len(tag) <= 40 for tag in tags)
    assert {"name": "pivota_quote_id", "value": "q_123"} in note_attributes
    assert {"name": "pivota_order_id", "value": "ord_123"} in note_attributes


def test_shopify_order_discount_codes_skip_product_level_allocations():
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["discount_evidence"]["applications"][0]["discount_class"] = "product"
    pricing_quote_meta["promotion_lines"][0]["discount_class"] = "product"

    assert order_routes._build_shopify_order_discount_codes(pricing_quote_meta) == []


def test_shopify_receipt_representation_blockers_flag_product_and_automatic_discounts():
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["pricing"]["shipping_fee"] = "0.00"
    pricing_quote_meta["promotion_lines"].append(
        {
            "source": "shopify",
            "method": "automatic",
            "discount_class": "shipping",
            "code": None,
            "amount": "-8.00",
        }
    )
    pricing_quote_meta["discount_evidence"]["applications"].append(
        {
            "source": "shopify",
            "method": "automatic",
            "discount_class": "shipping",
            "code": None,
            "amount": "-8.00",
        }
    )

    blockers = order_routes._shopify_receipt_representation_blockers(pricing_quote_meta)

    assert "product_level_discount" in blockers
    assert "automatic_discount" in blockers
    assert "code_less_shipping_discount" in blockers
    assert (
        "discount_not_encodable_as_rest_order_discount_code" in blockers
    )


def test_shopify_receipt_can_be_auto_sent_when_quote_uses_single_order_discount_code():
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["line_items"] = []
    pricing_quote_meta["discount_evidence"]["applications"][0]["discount_class"] = "order"
    pricing_quote_meta["promotion_lines"][0]["discount_class"] = "order"

    assert order_routes._shopify_receipt_can_be_auto_sent(
        customer_email="buyer@example.com",
        pricing_quote_meta=pricing_quote_meta,
    ) is True


def test_shopify_receipt_is_suppressed_when_quote_has_unrenderable_discount_shape():
    pricing_quote_meta = _pricing_quote_meta()

    assert order_routes._shopify_receipt_can_be_auto_sent(
        customer_email="buyer@example.com",
        pricing_quote_meta=pricing_quote_meta,
    ) is False


def test_select_shopify_write_policy_uses_rest_simple_for_quote_snapshot_without_blockers():
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["line_items"] = []
    pricing_quote_meta["discount_evidence"]["applications"][0]["discount_class"] = "order"
    pricing_quote_meta["promotion_lines"][0]["discount_class"] = "order"

    result = order_routes._select_shopify_write_policy(
        order={"merchant_id": "merch_1", "metadata": {"amounts_source": "quote_snapshot"}},
        pricing_quote_meta=pricing_quote_meta,
    )

    assert result["shopify_write_strategy"] == order_routes.SHOPIFY_WRITE_STRATEGY_REST_SIMPLE
    assert result["receipt_policy"] == order_routes.SHOPIFY_RECEIPT_POLICY_SEND
    assert result["representation_status"] == "rest_simple_representable"


def test_select_shopify_write_policy_requires_draft_order_for_complex_quote_when_enabled(monkeypatch):
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["pricing"]["shipping_fee"] = "0.00"
    pricing_quote_meta["discount_evidence"]["applications"].append(
        {
            "source": "shopify",
            "method": "automatic",
            "discount_class": "shipping",
            "code": None,
            "amount": "-8.00",
        }
    )

    monkeypatch.setenv("SHOPIFY_DRAFT_ORDER_QUOTE_SYNC_ENABLED", "1")

    result = order_routes._select_shopify_write_policy(
        order={"merchant_id": "merch_1", "metadata": {"amounts_source": "quote_snapshot"}},
        pricing_quote_meta=pricing_quote_meta,
    )

    assert result["shopify_write_strategy"] == order_routes.SHOPIFY_WRITE_STRATEGY_DRAFT_ORDER_QUOTE
    assert result["write_path"] == "draft_order"
    assert result["receipt_policy"] == order_routes.SHOPIFY_RECEIPT_POLICY_DRAFT_SUPPRESSED


def test_select_shopify_write_policy_suppresses_non_quote_snapshot_rows():
    result = order_routes._select_shopify_write_policy(
        order={"merchant_id": "merch_1", "metadata": {"amounts_source": "legacy_incomplete"}},
        pricing_quote_meta={},
    )

    assert result["shopify_write_strategy"] == order_routes.SHOPIFY_WRITE_STRATEGY_REST_LEGACY_SUPPRESSED
    assert result["receipt_policy"] == order_routes.SHOPIFY_RECEIPT_POLICY_SUPPRESSED
    assert result["representation_status"] == "legacy_not_authoritative"


def test_pricing_quote_disables_custom_line_item_rest_encoding_even_when_discount_is_fully_line_allocated():
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["pricing"]["shipping_fee"] = "0.00"
    pricing_quote_meta["discount_evidence"]["applications"].append(
        {
            "source": "shopify",
            "method": "automatic",
            "discount_class": "shipping",
            "code": None,
            "amount": "-8.00",
        }
    )

    assert order_routes._pricing_quote_supports_custom_line_item_rest_encoding(pricing_quote_meta) is False


def test_pricing_quote_rejects_custom_line_item_rest_encoding_when_discount_has_unallocated_remainder():
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["pricing"]["discount_total"] = "13.00"
    pricing_quote_meta["discount_evidence"]["applications"].append(
        {
            "source": "shopify",
            "method": "automatic",
            "discount_class": "shipping",
            "code": None,
            "amount": "-8.00",
        }
    )

    assert order_routes._pricing_quote_supports_custom_line_item_rest_encoding(pricing_quote_meta) is False


def test_apply_pricing_quote_line_item_overrides_sets_price_and_total_discount():
    line_item = {"variant_id": 123, "quantity": 1}
    order_item = {"product_id": "prod_1", "variant_id": "var_1", "quantity": 1}

    out = order_routes._apply_pricing_quote_line_item_overrides(
        line_item=line_item,
        order_item=order_item,
        pricing_quote_meta=_pricing_quote_meta(),
    )

    assert out["price"] == "100.00"
    assert out["total_discount"] == "5.00"


def test_build_shopify_draft_order_input_maps_variant_discounts_and_shipping():
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["pricing"].update(
        {
            "subtotal": "100.00",
            "discount_total": "5.00",
            "shipping_fee": "0.00",
            "tax": "0.00",
            "total": "95.00",
        }
    )
    pricing_quote_meta["discount_evidence"]["applications"].append(
        {
            "source": "shopify",
            "method": "automatic",
            "discount_class": "shipping",
            "code": None,
            "amount": "-8.00",
        }
    )
    pricing_quote_meta["discount_evidence"]["shipping_evidence"] = {
        "status": "authoritative",
        "selected_delivery_option_title": "Free Shipping",
    }

    draft_input = order_routes._build_shopify_draft_order_input(
        order_id="ORD_1",
        order={
            "items": [
                {
                    "product_id": "prod_1",
                    "variant_id": "123",
                    "product_title": "Test Product",
                    "quantity": 1,
                    "unit_price": "95.00",
                }
            ],
            "shipping_fee": "0.00",
        },
        pricing_quote_meta=pricing_quote_meta,
        customer_email="buyer@example.com",
        shopify_shipping={
            "first_name": "Buyer",
            "last_name": "Test",
            "address1": "1 Main St",
            "city": "San Francisco",
            "province": "CA",
            "zip": "94105",
            "country": "US",
        },
        currency_code="USD",
        shopify_tags=["pivota", "agent-order"],
        discount_note_attributes=[{"name": "pivota_quote_id", "value": "q_123"}],
    )

    assert draft_input["presentmentCurrencyCode"] == "USD"
    assert draft_input["shippingLine"]["title"] == "Free Shipping"
    assert draft_input["shippingLine"]["price"] == "0.00"
    assert draft_input["lineItems"][0]["variantId"] == "gid://shopify/ProductVariant/123"
    assert draft_input["lineItems"][0]["priceOverride"]["amount"] == "100.00"
    assert draft_input["lineItems"][0]["appliedDiscount"]["amountWithCurrency"]["amount"] == "5.00"


def test_shopify_order_tag_sanitizes_and_bounds_values():
    tag = order_routes._shopify_order_tag(
        "pivota_order_id",
        "ORD_508D4460ACA8DE11/unsafe:value-that-is-way-too-long",
    )

    assert ":" not in tag
    assert "_" not in tag
    assert "/" not in tag
    assert len(tag) <= 40


def test_pricing_quote_detects_unverified_shipping_evidence():
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["discount_evidence"]["shipping_evidence"] = {
        "status": "unverified",
        "reason": "delivery_options_unavailable",
        "source": "shopify_storefront_cart",
    }

    assert order_routes._pricing_quote_has_unverified_shipping(pricing_quote_meta) is True


def test_pricing_quote_accepts_authoritative_shipping_evidence():
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["discount_evidence"]["shipping_evidence"] = {
        "status": "authoritative",
        "amount": "7.00",
        "source": "shopify_storefront_cart",
    }

    assert order_routes._pricing_quote_has_unverified_shipping(pricing_quote_meta) is False


def test_shopify_order_reconciliation_passes_when_totals_match(monkeypatch):
    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "fail_closed")
    result = order_routes._reconcile_shopify_discount_order(
        order={"total": "95.00"},
        pricing_quote_meta=_pricing_quote_meta(),
        shopify_order={
            "total_price": "95.00",
            "total_discounts": "5.00",
            "transactions": [{"kind": "sale", "status": "success", "amount": "95.00"}],
        },
        transaction_amount=Decimal("95.00"),
    )

    assert result["passed"] is True
    assert result["status"] == "passed"
    assert result["mismatches"] == []


def test_shopify_order_reconciliation_does_not_count_shipping_discount_as_order_discount(monkeypatch):
    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "fail_closed")
    pricing_quote_meta = _pricing_quote_meta()
    pricing_quote_meta["pricing"].update(
        {
            "subtotal": "1.69",
            "discount_total": "0.00",
            "shipping_fee": "0.00",
            "tax": "0.00",
            "total": "1.69",
        }
    )
    pricing_quote_meta["line_items"] = [
        {
            "product_id": "prod_1",
            "variant_id": "var_1",
            "quantity": 1,
            "unit_price_original": "1.69",
            "unit_price_effective": "1.69",
            "line_discount_total": "0.00",
        }
    ]
    pricing_quote_meta["promotion_lines"] = [
        {
            "source": "shopify",
            "method": "automatic",
            "discount_class": "shipping",
            "code": None,
            "label": "Free Shipping",
            "amount": "-8.00",
        }
    ]
    pricing_quote_meta["discount_evidence"]["applications"] = [
        {
            "source": "shopify",
            "method": "automatic",
            "discount_class": "shipping",
            "code": None,
            "label": "Free Shipping",
            "amount": "-8.00",
        }
    ]

    result = order_routes._reconcile_shopify_discount_order(
        order={"total": "1.69"},
        pricing_quote_meta=pricing_quote_meta,
        shopify_order={
            "total_price": "1.69",
            "total_discounts": "0.00",
            "transactions": [{"kind": "sale", "status": "success", "amount": "1.69"}],
        },
        transaction_amount=Decimal("1.69"),
    )

    assert order_routes._pricing_quote_discount_total(pricing_quote_meta) == Decimal("0.00")
    assert result["passed"] is True
    assert result["status"] == "passed"
    assert result["mismatches"] == []
    assert result["expected"]["pivota_discount_total"] == "0.00"


def test_shopify_order_reconciliation_fails_closed_on_discount_mismatch(monkeypatch):
    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "fail_closed")
    result = order_routes._reconcile_shopify_discount_order(
        order={"total": "95.00"},
        pricing_quote_meta=_pricing_quote_meta(),
        shopify_order={
            "total_price": "95.00",
            "total_discounts": "0.00",
            "transactions": [{"kind": "sale", "status": "success", "amount": "95.00"}],
        },
        transaction_amount=Decimal("95.00"),
    )

    assert result["passed"] is False
    assert result["status"] == "failed"
    assert result["mismatches"] == ["shopify_discount_total"]


def test_shopify_order_reconciliation_observe_logs_mismatch_without_blocking(monkeypatch):
    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "observe")
    result = order_routes._reconcile_shopify_discount_order(
        order={"total": "95.00"},
        pricing_quote_meta=_pricing_quote_meta(),
        shopify_order={
            "total_price": "95.00",
            "total_discounts": "0.00",
            "transactions": [{"kind": "sale", "status": "success", "amount": "95.00"}],
        },
        transaction_amount=Decimal("95.00"),
    )

    assert result["passed"] is True
    assert result["status"] == "failed"
    assert result["mismatches"] == ["shopify_discount_total"]
