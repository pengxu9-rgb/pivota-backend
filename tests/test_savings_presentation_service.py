from __future__ import annotations

from decimal import Decimal

from services.savings_presentation_service import build_savings_presentation


def test_savings_presentation_separates_applied_store_discount_and_payment_benefit() -> None:
    presentation = build_savings_presentation(
        pricing={
            "subtotal": Decimal("100.00"),
            "discount_total": Decimal("10.00"),
            "shipping_fee": Decimal("5.00"),
            "tax": Decimal("0.00"),
            "total": Decimal("95.00"),
        },
        currency="USD",
        promotion_lines=[
            {
                "id": "promo_1",
                "source": "woocommerce",
                "discount_class": "product",
                "method": "code",
                "label": "10% off",
                "code": "PIVOTA_TEST_AMOUNT10",
                "amount": Decimal("-10.00"),
            }
        ],
        discount_evidence={"source": "woocommerce_quote", "pricing_confidence": "authoritative"},
        payment_offer_evidence={
            "pricing_confidence": "display_estimate",
            "offers": [
                {
                    "payment_offer_id": "mc_5",
                    "label": "Mastercard 5% Off",
                    "benefit_kind": "percentage_off",
                    "estimated_savings": "4.75",
                    "estimated_total_after_payment_offer": "90.25",
                    "eligibility": {"status": "potential", "reason_codes": []},
                    "display": {"badge": "Mastercard offer available"},
                    "application_policy": {"affects_psp_amount_v1": False},
                }
            ],
        },
        payment_pricing={
            "checkout_total": "95.00",
            "currency": "USD",
            "estimated_payment_benefit": "4.75",
            "display_only": True,
            "affects_psp_amount_v1": False,
        },
    )

    assert presentation["contract_version"] == "savings.v1"
    assert presentation["agentFacing"]["externalAgentsCanRender"] is True
    assert presentation["agentFacing"]["priceAuthority"] == "pivota_quote_psp_charge"
    assert presentation["agentFacing"]["payButtonUses"] == "pricing.total"
    assert presentation["appliedStoreDiscounts"][0]["application_policy"]["affects_checkout_total"] is True
    assert presentation["appliedStoreDiscounts"][0]["application_policy"]["source_of_truth"] == "woocommerce_quote"
    assert presentation["applicationPolicy"]["storePlatformQuoteIsStoreDiscountAuthority"] is True
    assert presentation["applicationPolicy"]["appliedStoreDiscountSourceOfTruth"] == "woocommerce_quote"
    assert presentation["paymentBenefits"][0]["application_policy"]["affects_psp_amount_v1"] is False
    rows = {row["kind"]: row for row in presentation["checkoutRows"]}
    assert rows["total_charged_now"]["amount"] == "95.00"
    assert rows["estimated_payment_benefit"]["affects_total"] is False


def test_savings_presentation_routes_bxgy_metadata_to_cart_unlocks() -> None:
    presentation = build_savings_presentation(
        pricing={"subtotal": "30.00", "discount_total": "0.00", "shipping_fee": "0.00", "tax": "0.00", "total": "30.00"},
        currency="USD",
        store_discount_evidence={
            "pricing_confidence": "metadata_unlockable",
            "offers": [
                {
                    "store_discount_id": "promo_bxgy",
                    "source": "store_discount_metadata",
                    "source_system": "shopify_discount_node",
                    "platform": "shopify",
                    "discount_method": "code",
                    "discount_type": "bxgy",
                    "status": "unlockable",
                    "codes": ["PIVOTA_TEST_BXGY"],
                    "minimum_requirement": {
                        "quantity_required": 3,
                        "current_quantity": 1,
                        "remaining_quantity": 2,
                    },
                    "display": {
                        "badge": "Buy 3, get 1",
                        "detail_copy": "Add 2 more to unlock this offer.",
                    },
                }
            ],
        },
    )

    assert presentation["availableStoreOffers"] == []
    assert presentation["cartUnlocks"][0]["minimum_requirement"]["remaining_quantity"] == 2
    assert presentation["cartUnlocks"][0]["source"] == "store_discount_metadata"
    assert presentation["cartUnlocks"][0]["platform"] == "shopify"
    assert presentation["summaryBadges"][0]["group"] == "cart_unlocks"
