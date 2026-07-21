from services.shopify_promotions_sync import _map_discount_node_to_promotion


def test_discount_node_mapper_preserves_open_ended_code_basic_metadata():
    promo = _map_discount_node_to_promotion(
        {
            "id": "gid://shopify/DiscountNode/1",
            "discount": {
                "__typename": "DiscountCodeBasic",
                "title": "SAVE10",
                "status": "ACTIVE",
                "summary": "10 off products",
                "startsAt": "2026-04-01T00:00:00Z",
                "endsAt": None,
                "discountClasses": ["PRODUCT"],
                "combinesWith": {"productDiscounts": True, "orderDiscounts": False, "shippingDiscounts": False},
                "customerSelection": {"__typename": "DiscountCustomerAll"},
                "customerGets": {"__typename": "DiscountCustomerGets", "items": {"__typename": "AllDiscountItems"}},
                "minimumRequirement": {"__typename": "DiscountMinimumSubtotal"},
                "usageLimit": 100,
                "appliesOncePerCustomer": True,
                "asyncUsageCount": 5,
                "codes": {"nodes": [{"code": "SAVE10"}]},
            },
        },
        merchant_id="merch_1",
    )

    assert promo is not None
    assert promo.endAt is None
    assert promo.type == "MULTI_BUY_DISCOUNT"
    assert promo.config["source"] == "shopify_discount_node"
    assert promo.config["shopifyDiscountNodeId"] == "gid://shopify/DiscountNode/1"
    assert promo.config["discountMethod"] == "code"
    assert promo.config["discountType"] == "basic"
    assert promo.config["codes"] == ["SAVE10"]
    assert promo.config["usageLimit"] == 100
    assert promo.config["appliesOncePerCustomer"] is True
    assert promo.config["asyncUsageCount"] == 5


def test_discount_node_mapper_handles_bxgy_and_free_shipping_primitives():
    bxgy = _map_discount_node_to_promotion(
        {
            "id": "gid://shopify/DiscountNode/2",
            "discount": {
                "__typename": "DiscountAutomaticBxgy",
                "title": "Auto BXGY",
                "startsAt": "2026-04-01T00:00:00Z",
                "endsAt": "2026-05-01T00:00:00Z",
                "discountClasses": ["PRODUCT"],
                "combinesWith": {"productDiscounts": False, "orderDiscounts": False, "shippingDiscounts": False},
                "customerBuys": {
                    "__typename": "DiscountCustomerBuys",
                    "items": {
                        "__typename": "DiscountProducts",
                        "products": {"nodes": [{"id": "gid://shopify/Product/10064558129449"}]},
                        "productVariants": {"nodes": [{"id": "gid://shopify/ProductVariant/53012602618153"}]},
                    },
                    "value": {"__typename": "DiscountQuantity", "quantity": 2},
                },
                "customerGets": {
                    "__typename": "DiscountCustomerGets",
                    "items": {
                        "__typename": "DiscountProducts",
                        "products": {"nodes": [{"id": "gid://shopify/Product/10064558129449"}]},
                        "productVariants": {"nodes": [{"id": "gid://shopify/ProductVariant/53012602618153"}]},
                    },
                    "value": {
                        "__typename": "DiscountOnQuantity",
                        "quantity": {"quantity": 1},
                        "effect": {"__typename": "DiscountPercentage", "percentage": 80.0},
                    },
                },
            },
        },
        merchant_id="merch_1",
    )
    free_shipping = _map_discount_node_to_promotion(
        {
            "id": "gid://shopify/DiscountNode/3",
            "discount": {
                "__typename": "DiscountCodeFreeShipping",
                "title": "SHIPFREE",
                "startsAt": "2026-04-01T00:00:00Z",
                "endsAt": None,
                "discountClasses": ["SHIPPING"],
                "combinesWith": {"productDiscounts": False, "orderDiscounts": False, "shippingDiscounts": True},
                "codes": {"nodes": [{"code": "SHIPFREE"}]},
            },
        },
        merchant_id="merch_1",
    )

    assert bxgy is not None
    assert bxgy.config["discountType"] == "bxgy"
    assert bxgy.config["discountMethod"] == "automatic"
    assert bxgy.scope["shopifyItems"]["__typename"] == "DiscountProducts"
    assert bxgy.scope["shopifyItems"]["productIds"] == ["gid://shopify/Product/10064558129449"]
    assert bxgy.scope["shopifyItems"]["variantIds"] == ["gid://shopify/ProductVariant/53012602618153"]
    assert bxgy.config["customerBuys"]["value"]["quantity"] == 2
    assert bxgy.config["customerGets"]["value"]["quantity"]["quantity"] == 1
    assert bxgy.config["customerGets"]["value"]["effect"]["percentage"] == 80.0
    assert free_shipping is not None
    assert free_shipping.type == "FREE_SHIPPING"
    assert free_shipping.config["discountType"] == "free_shipping"
    assert free_shipping.config["freeShipping"] is True
