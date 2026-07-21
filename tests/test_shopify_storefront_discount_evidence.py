from decimal import Decimal

import pytest

from services.shopify_storefront_pricing_service import (
    ShopifyStorefrontPricingService,
    StorefrontCartResult,
    _infer_shipping_fee_from_totals,
    _mark_shipping_evidence,
    _original_subtotal_from_line_items,
    _parse_storefront_cart_discounts,
    _shipping_unverified_reason,
    _shopify_cart_buyer_identity_input,
    _shopify_cart_selectable_address_input,
)
from services.shopify_pricing_service import ShopifyPricingError


def test_storefront_discount_parser_uses_line_allocations_not_total_delta():
    cart = {
        "discountCodes": [{"code": "save10", "applicable": True}],
        "lines": {
            "edges": [
                {
                    "node": {
                        "id": "line_1",
                        "quantity": 2,
                        "attributes": [{"key": "pivota_variant_id", "value": "111"}],
                        "discountAllocations": [
                            {
                                "__typename": "CartCodeDiscountAllocation",
                                "targetType": "LINE_ITEM",
                                "code": "save10",
                                "discountedAmount": {"amount": "5.00", "currencyCode": "USD"},
                            }
                        ],
                        "cost": {
                            "amountPerQuantity": {"amount": "10.00", "currencyCode": "USD"},
                            "totalAmount": {"amount": "15.00", "currencyCode": "USD"},
                        },
                    }
                }
            ]
        },
    }

    parsed = _parse_storefront_cart_discounts(cart=cart, submitted_codes=["save10"])

    assert parsed["discount_total"] == Decimal("5.00")
    assert parsed["discount_codes"] == [{"code": "SAVE10", "applicable": True, "source": "shopify_storefront_cart"}]
    assert parsed["line_pricing_by_variant_id"]["111"]["unit_price_original"] == Decimal("10.00")
    assert parsed["line_pricing_by_variant_id"]["111"]["unit_price_effective"] == Decimal("7.50")
    assert parsed["line_pricing_by_variant_id"]["111"]["line_discount_total"] == Decimal("5.00")
    assert parsed["promotion_lines"][0]["amount"] == Decimal("-5.00")
    assert parsed["discount_evidence"]["pricing_confidence"] == "authoritative"


def test_shopify_cart_buyer_identity_input_includes_email_without_address():
    buyer_identity = _shopify_cart_buyer_identity_input(
        customer_email="buyer@example.com",
        country=None,
        postal=None,
        city=None,
        province=None,
        address1=None,
        address2=None,
        use_buyer_country_for_pricing=True,
    )

    assert buyer_identity == {"email": "buyer@example.com"}


def test_storefront_discount_parser_records_cart_level_shipping_discount_allocations():
    cart = {
        "id": "gid://shopify/Cart/test",
        "discountCodes": [{"code": "freeship", "applicable": True}],
        "discountAllocations": [
            {
                "__typename": "CartCodeDiscountAllocation",
                "targetType": "SHIPPING_LINE",
                "code": "freeship",
                "discountedAmount": {"amount": "29.00", "currencyCode": "EUR"},
            }
        ],
        "lines": {
            "edges": [
                {
                    "node": {
                        "id": "line_1",
                        "quantity": 1,
                        "attributes": [{"key": "pivota_variant_id", "value": "111"}],
                        "discountAllocations": [],
                        "cost": {
                            "amountPerQuantity": {"amount": "29.00", "currencyCode": "EUR"},
                            "totalAmount": {"amount": "29.00", "currencyCode": "EUR"},
                        },
                    }
                }
            ]
        },
    }

    parsed = _parse_storefront_cart_discounts(cart=cart, submitted_codes=["freeship"])

    assert parsed["discount_total"] == Decimal("0.00")
    assert parsed["shipping_discount_total"] == Decimal("29.00")
    assert parsed["promotion_lines"][0]["discount_class"] == "shipping"
    assert parsed["promotion_lines"][0]["amount"] == Decimal("-29.00")
    assert parsed["promotion_lines"][0]["allocations"][0]["target_type"] == "shipping"
    assert parsed["discount_evidence"]["shipping_evidence"]["discount_total"] == "29.00"
    assert parsed["discount_evidence"]["pricing_confidence"] == "authoritative"


def test_storefront_discount_parser_treats_cart_level_code_allocations_as_order_discounts():
    cart = {
        "id": "gid://shopify/Cart/test",
        "discountCodes": [{"code": "combo_b", "applicable": True}],
        "discountAllocations": [
            {
                "__typename": "CartCodeDiscountAllocation",
                "targetType": "LINE_ITEM",
                "code": "combo_b",
                "discountedAmount": {"amount": "1.00", "currencyCode": "USD"},
            }
        ],
        "lines": {
            "edges": [
                {
                    "node": {
                        "id": "line_1",
                        "quantity": 1,
                        "attributes": [{"key": "pivota_variant_id", "value": "111"}],
                        "discountAllocations": [],
                        "cost": {
                            "amountPerQuantity": {"amount": "10.00", "currencyCode": "USD"},
                            "totalAmount": {"amount": "10.00", "currencyCode": "USD"},
                        },
                    }
                }
            ]
        },
    }

    parsed = _parse_storefront_cart_discounts(cart=cart, submitted_codes=["combo_b"])

    assert parsed["discount_total"] == Decimal("1.00")
    assert parsed["promotion_lines"][0]["discount_class"] == "order"
    assert parsed["promotion_lines"][0]["source_ref"].startswith("cart|code|COMBO_B|COMBO_B|order|")
    assert parsed["promotion_lines"][0]["allocations"][0]["target_type"] == "order"
    assert parsed["promotion_lines"][0]["amount"] == Decimal("-1.00")
    assert parsed["promotion_lines"][0]["metadata"]["allocation_scope"] == "cart"


def test_storefront_discount_parser_records_invalid_code_without_discount_amount():
    cart = {
        "discountCodes": [{"code": "badcode", "applicable": False}],
        "lines": {
            "edges": [
                {
                    "node": {
                        "id": "line_1",
                        "quantity": 1,
                        "attributes": [{"key": "pivota_variant_id", "value": "222"}],
                        "discountAllocations": [],
                        "cost": {
                            "amountPerQuantity": {"amount": "20.00", "currencyCode": "USD"},
                            "totalAmount": {"amount": "20.00", "currencyCode": "USD"},
                        },
                    }
                }
            ]
        },
    }

    parsed = _parse_storefront_cart_discounts(cart=cart, submitted_codes=["badcode"])

    assert parsed["discount_total"] == Decimal("0.00")
    assert parsed["promotion_lines"] == []
    assert parsed["discount_codes"] == [{"code": "BADCODE", "applicable": False, "source": "shopify_storefront_cart"}]
    assert parsed["discount_evidence"]["pricing_confidence"] == "partial"


def test_storefront_discount_parser_records_line_shipping_requirements():
    cart = {
        "discountCodes": [],
        "lines": {
            "edges": [
                {
                    "node": {
                        "id": "line_1",
                        "quantity": 1,
                        "attributes": [{"key": "pivota_variant_id", "value": "333"}],
                        "merchandise": {
                            "id": "gid://shopify/ProductVariant/333",
                            "availableForSale": True,
                            "requiresShipping": False,
                            "weight": 0.0,
                            "weightUnit": "KILOGRAMS",
                        },
                        "discountAllocations": [],
                        "cost": {
                            "amountPerQuantity": {"amount": "20.00", "currencyCode": "USD"},
                            "totalAmount": {"amount": "20.00", "currencyCode": "USD"},
                        },
                    }
                }
            ]
        },
    }

    parsed = _parse_storefront_cart_discounts(cart=cart, submitted_codes=[])

    assert parsed["discount_evidence"]["shipping_evidence"]["line_shipping_requirements"] == [
        {
            "variant_id": "333",
            "storefront_variant_id": "gid://shopify/ProductVariant/333",
            "requires_shipping": False,
            "available_for_sale": True,
            "weight": 0.0,
            "weight_unit": "KILOGRAMS",
        }
    ]
    assert _shipping_unverified_reason(parsed["discount_evidence"]) == "cart_lines_do_not_require_shipping"


def test_original_subtotal_preserves_pre_discount_line_amount_for_quote_display():
    line_items = [
        {
            "quantity": 3,
            "unit_price_original": Decimal("29.00"),
            "unit_price_effective": Decimal("19.33"),
            "line_discount_total": Decimal("29.00"),
        }
    ]

    assert _original_subtotal_from_line_items(line_items) == Decimal("87.00")


def test_shipping_inference_does_not_treat_product_discount_as_shipping():
    shipping_fee = _infer_shipping_fee_from_totals(
        subtotal=Decimal("29.00"),
        total=Decimal("26.10"),
        tax=Decimal("0.00"),
        discount_total=Decimal("2.90"),
    )

    assert shipping_fee is None


def test_shipping_inference_can_recover_selected_shipping_from_authoritative_totals():
    shipping_fee = _infer_shipping_fee_from_totals(
        subtotal=Decimal("29.00"),
        total=Decimal("33.10"),
        tax=Decimal("0.00"),
        discount_total=Decimal("2.90"),
    )

    assert shipping_fee == Decimal("7.00")


def test_derive_shipping_fee_uses_net_shipping_after_shipping_discount():
    service = ShopifyStorefrontPricingService()

    shipping_fee = service._derive_shipping_fee(
        StorefrontCartResult(
            cart_id="cart_1",
            checkout_url=None,
            currency="EUR",
            subtotal=Decimal("29.00"),
            total=Decimal("29.00"),
            tax=Decimal("0.00"),
            delivery_options=[
                {
                    "handle": "standard",
                    "estimatedCost": {"amount": "29.00", "currencyCode": "EUR"},
                }
            ],
            selected_delivery_option=None,
            unit_price_by_variant_id={},
            line_pricing_by_variant_id={},
            promotion_lines=[],
            discount_codes=[],
            discount_total=Decimal("0.00"),
            shipping_discount_total=Decimal("29.00"),
            discount_evidence={},
        )
    )

    assert shipping_fee == Decimal("0.00")


@pytest.mark.asyncio
async def test_preview_cart_quote_fails_closed_when_cart_create_returns_no_cart(monkeypatch):
    service = ShopifyStorefrontPricingService()

    async def fake_primary_store(_merchant_id):
        return {
            "platform": "shopify",
            "domain": "example.myshopify.com",
            "api_credentials": {"storefront_access_token": "sf_token"},
        }

    async def fake_admin_token(**_kwargs):
        return None, None

    async def fake_create_cart(**_kwargs):
        return None

    monkeypatch.setattr(
        "services.shopify_storefront_pricing_service.get_primary_store",
        fake_primary_store,
    )
    monkeypatch.setattr(
        "services.shopify_storefront_pricing_service.resolve_shopify_admin_access_token",
        fake_admin_token,
    )
    monkeypatch.setattr(service, "_create_cart", fake_create_cart)

    with pytest.raises(ShopifyPricingError) as excinfo:
        await service.preview_cart_quote(
            merchant_id="merch_test",
            items=[{"product_id": "prod_1", "variant_id": "123", "quantity": 1}],
            discount_codes=[],
            customer_email=None,
            shipping_address={"country": "US", "postal_code": "94102"},
            selected_delivery_option=None,
        )

    err = excinfo.value
    assert err.code == "SHOPIFY_PRICING_UNAVAILABLE"
    assert err.message == "Storefront cartCreate returned no cart"
    assert err.details["variant_id"] == "123"
    assert err.details["item_count"] == 1


def test_unverified_shipping_evidence_downgrades_pricing_confidence():
    evidence = {
        "pricing_confidence": "authoritative",
        "shipping_evidence": {
            "line_shipping_requirements": [
                {
                    "variant_id": "333",
                    "requires_shipping": True,
                }
            ]
        },
    }

    _mark_shipping_evidence(evidence, status="unverified", reason=_shipping_unverified_reason(evidence))

    assert evidence["pricing_confidence"] == "partial"
    assert evidence["shipping_evidence"] == {
        "status": "unverified",
        "source": "shopify_storefront_cart",
        "reason": "shipping_rates_unavailable_for_shippable_lines",
        "line_shipping_requirements": [
            {
                "variant_id": "333",
                "requires_shipping": True,
            }
        ],
    }


def test_shopify_selectable_address_input_matches_cart_delivery_shape():
    address = _shopify_cart_selectable_address_input(
        country="US",
        postal="10118",
        city="New York",
        province="NY",
        address1="350 5th Ave",
        address2=None,
    )

    assert address == {
        "selected": True,
        "oneTimeUse": True,
        "address": {
            "deliveryAddress": {
                "countryCode": "US",
                "zip": "10118",
                "city": "New York",
                "provinceCode": "NY",
                "address1": "350 5th Ave",
            }
        },
    }


@pytest.mark.asyncio
async def test_delivery_address_add_uses_shopify_current_selectable_address_shape(monkeypatch):
    monkeypatch.setenv("SHOPIFY_STOREFRONT_DELIVERY_OPTIONS_ATTEMPTS", "1")
    service = ShopifyStorefrontPricingService()
    calls = []

    async def fake_storefront_graphql(**kwargs):
        calls.append(kwargs)
        query = kwargs["query"]
        if "cartDeliveryAddressesAdd" in query:
            return {"cartDeliveryAddressesAdd": {"cart": {"id": "cart_1"}, "userErrors": []}}
        if "cartBuyerIdentityUpdate" in query:
            return {"cartBuyerIdentityUpdate": {"cart": {"id": "cart_1"}, "userErrors": []}}
        return {"cart": {"deliveryGroups": {"edges": []}}}

    monkeypatch.setattr(service, "_storefront_graphql", fake_storefront_graphql)

    options, selected, diagnostics = await service._attach_address_and_select_delivery_best_effort(
        shop_domain="example.myshopify.com",
        storefront_token="sf_token",
        cart_id="gid://shopify/Cart/test",
        customer_email="buyer@example.com",
        country="US",
        postal="10118",
        city="New York",
        province="NY",
        address1="350 5th Ave",
        address2="",
        selected_delivery_option=None,
        use_buyer_country_for_pricing=True,
        debug_id="dbg",
    )

    assert options is None
    assert selected is None
    assert diagnostics["storefront_api_version"] == "2026-04"
    assert diagnostics["address_add_succeeded"] is True
    assert diagnostics["buyer_identity_update_succeeded"] is True
    assert diagnostics["delivery_options_count"] == 0
    assert calls[0]["variables"]["addresses"] == [
        {
            "selected": True,
            "oneTimeUse": True,
            "address": {
                "deliveryAddress": {
                    "countryCode": "US",
                    "zip": "10118",
                    "city": "New York",
                    "provinceCode": "NY",
                    "address1": "350 5th Ave",
                }
            },
        }
    ]
    assert calls[1]["variables"]["buyerIdentity"]["email"] == "buyer@example.com"


@pytest.mark.asyncio
async def test_delivery_options_query_retries_until_rates_are_available(monkeypatch):
    monkeypatch.setenv("SHOPIFY_STOREFRONT_DELIVERY_OPTIONS_ATTEMPTS", "3")
    monkeypatch.setenv("SHOPIFY_STOREFRONT_DELIVERY_OPTIONS_RETRY_DELAY_SECONDS", "0")
    service = ShopifyStorefrontPricingService()
    delivery_query_count = 0

    async def fake_storefront_graphql(**kwargs):
        nonlocal delivery_query_count
        query = kwargs["query"]
        if "cartDeliveryAddressesAdd" in query:
            return {"cartDeliveryAddressesAdd": {"cart": {"id": "cart_1"}, "userErrors": []}}
        if "cartBuyerIdentityUpdate" in query:
            return {"cartBuyerIdentityUpdate": {"cart": {"id": "cart_1"}, "userErrors": []}}
        if "cartSelectedDeliveryOptionsUpdate" in query:
            return {"cartSelectedDeliveryOptionsUpdate": {"cart": {"id": "cart_1"}, "userErrors": []}}
        delivery_query_count += 1
        if delivery_query_count < 2:
            return {"cart": {"deliveryGroups": {"edges": []}}}
        return {
            "cart": {
                "deliveryGroups": {
                    "edges": [
                        {
                            "node": {
                                "id": "group_1",
                                "deliveryOptions": [
                                    {
                                        "handle": "standard",
                                        "title": "Standard",
                                        "estimatedCost": {"amount": "7.00", "currencyCode": "USD"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            },
            "cartSelectedDeliveryOptionsUpdate": {"cart": {"id": "cart_1"}, "userErrors": []},
        }

    monkeypatch.setattr(service, "_storefront_graphql", fake_storefront_graphql)

    options, selected, diagnostics = await service._attach_address_and_select_delivery_best_effort(
        shop_domain="example.myshopify.com",
        storefront_token="sf_token",
        cart_id="gid://shopify/Cart/test",
        customer_email="buyer@example.com",
        country="US",
        postal="10118",
        city="New York",
        province="NY",
        address1="350 5th Ave",
        address2="",
        selected_delivery_option=None,
        use_buyer_country_for_pricing=True,
        debug_id="dbg",
    )

    assert delivery_query_count == 2
    assert options and options[0]["handle"] == "standard"
    assert selected and selected["estimatedCost"]["amount"] == "7.00"
    assert diagnostics["address_add_succeeded"] is True
    assert diagnostics["buyer_identity_update_succeeded"] is True
    assert diagnostics["delivery_groups_count"] == 1
    assert diagnostics["delivery_options_count"] == 1
    assert diagnostics["selected_delivery_option_handle"] == "standard"


@pytest.mark.asyncio
async def test_cart_create_includes_delivery_address_in_cart_input(monkeypatch):
    service = ShopifyStorefrontPricingService()
    calls = []

    async def fake_storefront_graphql(**kwargs):
        calls.append(kwargs)
        query = kwargs["query"]
        if "cartCreate" in query:
            return {
                "cartCreate": {
                    "cart": {
                        "id": "gid://shopify/Cart/test",
                        "checkoutUrl": "https://example.test/cart",
                        "discountCodes": [],
                        "lines": {"edges": []},
                        "cost": {
                            "subtotalAmount": {"amount": "0.00", "currencyCode": "USD"},
                            "totalTaxAmount": {"amount": "0.00", "currencyCode": "USD"},
                            "totalAmount": {"amount": "0.00", "currencyCode": "USD"},
                        },
                    },
                    "userErrors": [],
                }
            }
        return {"cart": {"deliveryGroups": {"edges": []}}}

    monkeypatch.setattr(service, "_storefront_graphql", fake_storefront_graphql)

    await service._create_cart(
        shop_domain="example.myshopify.com",
        storefront_token="sf_token",
        items=[{"variant_id": "123", "quantity": 1}],
        discount_codes=[],
        customer_email="buyer@example.com",
        shipping_address={
            "country": "US",
            "postal_code": "10118",
            "city": "New York",
            "state": "NY",
            "address_line1": "350 5th Ave",
        },
        selected_delivery_option=None,
        use_buyer_country_for_pricing=True,
        debug_id="dbg",
    )

    cart_create_variables = calls[0]["variables"]
    assert cart_create_variables["input"]["lines"][0]["merchandiseId"] == "gid://shopify/ProductVariant/123"
    assert cart_create_variables["input"]["buyerIdentity"] == {
        "email": "buyer@example.com",
        "countryCode": "US",
        "preferences": {"delivery": {"deliveryMethod": ["SHIPPING"]}},
        "deliveryAddressPreferences": [
            {
                "oneTimeUse": True,
                "deliveryAddress": {
                    "country": "US",
                    "zip": "10118",
                    "city": "New York",
                    "province": "NY",
                    "address1": "350 5th Ave",
                },
            }
        ],
    }
    assert cart_create_variables["input"]["delivery"] == {
        "addresses": [
            {
                "selected": True,
                "oneTimeUse": True,
                "address": {
                    "deliveryAddress": {
                        "countryCode": "US",
                        "zip": "10118",
                        "city": "New York",
                        "provinceCode": "NY",
                        "address1": "350 5th Ave",
                    }
                },
            }
        ]
    }
