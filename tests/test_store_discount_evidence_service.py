from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List

import pytest

import services.store_discount_evidence_service as module
from services.promotions_service import PromotionOut, PromotionStatus


def _promo(**overrides: Any) -> PromotionOut:
    now = datetime.utcnow()
    base: Dict[str, Any] = {
        "id": "promo_basic",
        "merchantId": "merch_1",
        "name": "PIVOTA_TEST_AMOUNT10",
        "type": "MULTI_BUY_DISCOUNT",
        "description": "10% off products",
        "startAt": now - timedelta(days=1),
        "endAt": None,
        "channels": ["agent"],
        "scope": {"shopifyItems": {"__typename": "AllDiscountItems"}},
        "config": {
            "source": "shopify_discount_node",
            "shopifyDiscountNodeId": "gid://shopify/DiscountNode/1",
            "discountMethod": "code",
            "discountType": "basic",
            "discountClasses": ["PRODUCT"],
            "combinesWith": {"productDiscounts": True, "orderDiscounts": False, "shippingDiscounts": False},
            "summary": "10% off products",
            "minimumRequirement": None,
            "codes": ["PIVOTA_TEST_AMOUNT10"],
            "status": "ACTIVE",
        },
        "exposeToCreators": True,
        "allowedCreatorIds": None,
        "humanReadableRule": "10% off products",
        "status": PromotionStatus.ACTIVE,
        "createdAt": now,
        "updatedAt": now,
        "deletedAt": None,
    }
    base.update(overrides)
    return PromotionOut(**base)


def test_store_discount_card_target_prefers_nested_variant_id() -> None:
    target = module._target_from_product_card(
        {
            "merchant_id": "merch_1",
            "product_id": "prod_1",
            "id": "prod_1",
            "price": "10.00",
            "currency": "USD",
            "variants": [{"variant_id": "var_real"}],
        }
    )

    assert target is not None
    assert target.product_id == "prod_1"
    assert target.variant_id == "var_real"
    assert target.target_id == "merch_1:var_real"


@pytest.mark.asyncio
async def test_store_discount_evidence_basic_code_is_metadata_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list_promotions(**_kwargs: Any) -> tuple[List[PromotionOut], int]:
        return [_promo()], 1

    monkeypatch.setattr(module, "list_promotions", fake_list_promotions)

    result = await module.resolve_store_discount_evidence_for_targets(
        merchant_id="merch_1",
        targets=[
            module.StoreDiscountTarget(
                target_id="var_1",
                merchant_id="merch_1",
                product_id="prod_1",
                variant_id="var_1",
                subtotal=Decimal("25.00"),
                currency="USD",
            )
        ],
    )

    evidence = result["var_1"]
    assert evidence["pricing_confidence"] == "metadata_available"
    assert evidence["presentation_contract_version"] == "savings.v1"
    offer = evidence["offers"][0]
    assert offer["discount_type"] == "basic"
    assert offer["discount_method"] == "code"
    assert offer["status"] == "available"
    assert offer["source"] == "store_discount_metadata"
    assert offer["source_system"] == "shopify_discount_node"
    assert offer["platform"] == "shopify"
    assert offer["codes"] == ["PIVOTA_TEST_AMOUNT10"]
    assert offer["application_policy"]["affects_checkout_total_before_quote"] is False
    assert offer["application_policy"]["final_authority"] == "store_platform_quote"


@pytest.mark.asyncio
async def test_store_discount_evidence_bxgy_minimum_quantity_is_unlockable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bxgy = _promo(
        id="promo_bxgy",
        name="PIVOTA_TEST_BXGY",
        config={
            "source": "shopify_discount_node",
            "shopifyDiscountNodeId": "gid://shopify/DiscountNode/2",
            "discountMethod": "code",
            "discountType": "bxgy",
            "discountClasses": ["PRODUCT"],
            "combinesWith": {"productDiscounts": False, "orderDiscounts": False, "shippingDiscounts": False},
            "summary": "Buy 3, get 1 free",
            "minimumRequirement": {"__typename": "DiscountMinimumQuantity", "greaterThanOrEqualToQuantity": 3},
            "codes": ["PIVOTA_TEST_BXGY"],
            "status": "ACTIVE",
        },
    )

    async def fake_list_promotions(**_kwargs: Any) -> tuple[List[PromotionOut], int]:
        return [bxgy], 1

    monkeypatch.setattr(module, "list_promotions", fake_list_promotions)

    result = await module.resolve_store_discount_evidence_for_targets(
        merchant_id="merch_1",
        targets=[
            module.StoreDiscountTarget(
                target_id="var_1",
                merchant_id="merch_1",
                product_id="prod_1",
                variant_id="var_1",
                quantity=1,
                subtotal=Decimal("15.00"),
                currency="USD",
            )
        ],
    )

    offer = result["var_1"]["offers"][0]
    assert offer["discount_type"] == "bxgy"
    assert offer["status"] == "unlockable"
    assert offer["minimum_requirement"]["quantity_required"] == 3
    assert offer["minimum_requirement"]["remaining_quantity"] == 2
    assert offer["display"]["detail_copy"] == "Add 2 more to unlock this offer."


@pytest.mark.asyncio
async def test_store_discount_evidence_free_shipping_and_unknown_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    free_ship = _promo(
        id="promo_ship",
        name="PIVOTA_TEST_FREESHIP",
        type="FREE_SHIPPING",
        scope={"shopifyItems": {"__typename": "DiscountCollections"}},
        config={
            "source": "shopify_discount_node",
            "shopifyDiscountNodeId": "gid://shopify/DiscountNode/3",
            "discountMethod": "code",
            "discountType": "free_shipping",
            "discountClasses": ["SHIPPING"],
            "combinesWith": {"shippingDiscounts": True},
            "summary": "Free US shipping",
            "minimumRequirement": None,
            "codes": ["PIVOTA_TEST_FREESHIP"],
            "status": "ACTIVE",
        },
    )

    async def fake_list_promotions(**_kwargs: Any) -> tuple[List[PromotionOut], int]:
        return [free_ship], 1

    monkeypatch.setattr(module, "list_promotions", fake_list_promotions)

    result = await module.resolve_store_discount_evidence_for_targets(
        merchant_id="merch_1",
        targets=[module.StoreDiscountTarget(target_id="var_1", merchant_id="merch_1", variant_id="var_1")],
    )

    offer = result["var_1"]["offers"][0]
    assert offer["discount_type"] == "free_shipping"
    assert offer["status"] == "unverified"
    assert offer["display"]["badge"] == "Free shipping code"
    assert offer["application_policy"]["requires_storefront_allocation_for_applied_amount"] is True


@pytest.mark.asyncio
async def test_store_discount_evidence_skips_expired_and_non_shopify_promos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expired = _promo(id="promo_expired", status=PromotionStatus.ENDED)
    manual = _promo(id="manual", config={"source": "manual"})

    async def fake_list_promotions(**_kwargs: Any) -> tuple[List[PromotionOut], int]:
        return [expired, manual], 2

    monkeypatch.setattr(module, "list_promotions", fake_list_promotions)

    result = await module.resolve_store_discount_evidence_for_targets(
        merchant_id="merch_1",
        targets=[module.StoreDiscountTarget(target_id="var_1", merchant_id="merch_1", variant_id="var_1")],
    )

    evidence = result["var_1"]
    assert evidence["offers"] == []
    reasons = {decision["reason"] for decision in evidence["decisions"]}
    assert {"expired", "unsupported_store_discount_source"}.issubset(reasons)
