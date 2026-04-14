import os
from decimal import Decimal


os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/test")

from services.shopify_storefront_pricing_service import _parse_storefront_cart_discounts


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
