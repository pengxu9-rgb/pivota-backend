import json
from decimal import Decimal

import pytest

from services.quote_service import QuoteService
from services.shopify_pricing_service import ShopifyPricingResult


@pytest.mark.asyncio
async def test_preview_quote_returns_nested_discount_evidence_json_safe(monkeypatch):
    svc = QuoteService()
    captured_quote = {}

    async def fake_preview_cart_quote(**_kwargs):
        return ShopifyPricingResult(
            engine="shopify_storefront_cart",
            engine_ref="cart_1",
            currency="USD",
            pricing={
                "subtotal": Decimal("10.00"),
                "discount_total": Decimal("1.00"),
                "shipping_fee": Decimal("0.00"),
                "tax": Decimal("0.00"),
                "total": Decimal("9.00"),
            },
            promotion_lines=[
                {
                    "id": "shopify:code:SAVE10",
                    "source": "shopify_storefront",
                    "source_ref": "SAVE10",
                    "discount_class": "product",
                    "method": "code",
                    "label": "SAVE10",
                    "code": "SAVE10",
                    "amount": Decimal("-1.00"),
                    "allocations": [
                        {
                            "target_type": "line_item",
                            "target_id": "var_1",
                            "amount": Decimal("-1.00"),
                        }
                    ],
                }
            ],
            line_items=[
                {
                    "product_id": "prod_1",
                    "variant_id": "var_1",
                    "quantity": 1,
                    "unit_price_original": Decimal("10.00"),
                    "unit_price_effective": Decimal("9.00"),
                    "line_discount_total": Decimal("1.00"),
                    "compare_at_savings": Decimal("0.00"),
                }
            ],
            delivery_options=[{"handle": "free", "amount": Decimal("0.00"), "title": "Free"}],
            debug={"debug_id": "debug_1", "discount_evidence": {"debug_amount": Decimal("1.00")}},
            discount_evidence={
                "source": "shopify_storefront_cart",
                "pricing_confidence": "authoritative",
                "codes": [{"code": "SAVE10", "applicable": True, "source": "shopify_storefront"}],
                "applications": [
                    {
                        "source": "shopify",
                        "discount_class": "product",
                        "amount": Decimal("1.00"),
                        "allocations": [{"target_id": "var_1", "amount": Decimal("-1.00")}],
                    }
                ],
                "decisions": [],
            },
        )

    async def fake_insert_quote(row):
        captured_quote.update(row)

    async def fake_list_promotions(**_kwargs):
        return ([], 0)

    async def fake_store_discount_evidence(**_kwargs):
        return {
            "var_1": {
                "pricing_confidence": "metadata_available",
                "offers": [{"discount_value": Decimal("10.00")}],
                "decisions": [],
            }
        }

    async def fake_payment_offer_evidence(**_kwargs):
        return {
            "pricing_confidence": "display_estimate",
            "offers": [{"payment_offer_id": "pay_1", "estimated_savings": Decimal("0.50")}],
            "decisions": [{"weight": Decimal("1.00")}],
        }

    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "0")
    monkeypatch.setattr(svc.pricing_storefront, "preview_cart_quote", fake_preview_cart_quote)
    monkeypatch.setattr("services.quote_service.insert_quote", fake_insert_quote)
    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setattr(
        "services.quote_service.resolve_store_discount_evidence_for_targets",
        fake_store_discount_evidence,
    )
    monkeypatch.setattr(
        "services.quote_service.resolve_payment_offer_evidence",
        fake_payment_offer_evidence,
    )
    monkeypatch.setattr(
        "services.quote_service.emit_payment_offer_analytics_event",
        lambda **_kwargs: None,
    )

    result = await svc.preview_quote(
        merchant_id="merch_1",
        agent_id="agent_1",
        items=[{"product_id": "prod_1", "variant_id": "var_1", "quantity": 1}],
        discount_codes=["SAVE10"],
        customer_email=None,
        shipping_address={"country": "US", "postal_code": "10001", "city": "New York", "state": "NY"},
        selected_delivery_option=None,
    )

    assert result["discount_evidence"]["applications"][0]["amount"] == "1.00"
    assert result["discount_evidence"]["applications"][0]["allocations"][0]["amount"] == "-1.00"
    assert result["promotion_lines"][0]["amount"] == "-1.00"
    assert result["promotion_lines"][0]["allocations"][0]["amount"] == "-1.00"
    assert result["store_discount_evidence"]["offers"][0]["discount_value"] == "10.00"
    assert result["payment_offer_evidence"]["offers"][0]["estimated_savings"] == "0.50"
    assert result["line_items"][0]["line_discount_total"] == "1.00"
    assert result["delivery_options"][0]["amount"] == "0.00"
    assert result["availability_status"] == "available_confirmed"
    assert result["is_final"] is True
    assert result["source_updated_at"] is not None
    assert result["warnings"] == []

    snapshot = captured_quote["snapshot_json"]
    assert snapshot["availability_status"] == "available_confirmed"
    assert snapshot["is_final"] is True
    assert snapshot["discount_evidence"]["applications"][0]["amount"] == "1.00"
    assert snapshot["payment_offer_evidence"]["offers"][0]["estimated_savings"] == "0.50"
    assert snapshot["delivery_options"][0]["amount"] == "0.00"
    assert snapshot["metadata"]["discount_evidence"]["debug_amount"] == "1.00"
    json.dumps(snapshot)
    json.dumps(
        {
            "promotion_lines": result["promotion_lines"],
            "discount_evidence": result["discount_evidence"],
            "store_discount_evidence": result["store_discount_evidence"],
            "payment_offer_evidence": result["payment_offer_evidence"],
            "line_items": result["line_items"],
            "delivery_options": result["delivery_options"],
        }
    )


@pytest.mark.asyncio
async def test_preview_quote_dedupes_store_discount_evidence_across_targets(monkeypatch):
    svc = QuoteService()

    async def fake_preview_cart_quote(**_kwargs):
        return ShopifyPricingResult(
            engine="shopify_storefront_cart",
            engine_ref="cart_2",
            currency="USD",
            pricing={
                "subtotal": Decimal("20.00"),
                "discount_total": Decimal("0.00"),
                "shipping_fee": Decimal("0.00"),
                "tax": Decimal("0.00"),
                "total": Decimal("20.00"),
            },
            promotion_lines=[],
            line_items=[
                {
                    "product_id": "prod_1",
                    "variant_id": "var_1",
                    "quantity": 1,
                    "unit_price_original": Decimal("10.00"),
                    "unit_price_effective": Decimal("10.00"),
                    "line_discount_total": Decimal("0.00"),
                    "compare_at_savings": Decimal("0.00"),
                },
                {
                    "product_id": "prod_2",
                    "variant_id": "var_2",
                    "quantity": 1,
                    "unit_price_original": Decimal("10.00"),
                    "unit_price_effective": Decimal("10.00"),
                    "line_discount_total": Decimal("0.00"),
                    "compare_at_savings": Decimal("0.00"),
                },
            ],
            delivery_options=[],
            debug={},
            discount_evidence={
                "source": "shopify_storefront_cart",
                "pricing_confidence": "authoritative",
                "codes": [],
                "applications": [],
                "decisions": [],
                "shipping_evidence": {"status": "authoritative"},
            },
        )

    async def fake_insert_quote(_row):
        return None

    async def fake_list_promotions(**_kwargs):
        return ([], 0)

    async def fake_store_discount_evidence(**_kwargs):
        offer = {"store_discount_id": "store_1", "label": "PIVOTA_TEST_BXGY"}
        decision = {"type": "store_discount_skipped", "store_discount_id": "expired_1", "reason": "expired"}
        return {
            "var_1": {"pricing_confidence": "metadata_available", "offers": [offer], "decisions": [decision]},
            "var_2": {"pricing_confidence": "metadata_available", "offers": [dict(offer)], "decisions": [dict(decision)]},
        }

    async def fake_payment_offer_evidence(**_kwargs):
        return {"pricing_confidence": "not_applicable", "offers": [], "decisions": []}

    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "0")
    monkeypatch.setattr(svc.pricing_storefront, "preview_cart_quote", fake_preview_cart_quote)
    monkeypatch.setattr("services.quote_service.insert_quote", fake_insert_quote)
    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setattr("services.quote_service.resolve_store_discount_evidence_for_targets", fake_store_discount_evidence)
    monkeypatch.setattr("services.quote_service.resolve_payment_offer_evidence", fake_payment_offer_evidence)
    monkeypatch.setattr("services.quote_service.emit_payment_offer_analytics_event", lambda **_kwargs: None)

    result = await svc.preview_quote(
        merchant_id="merch_1",
        agent_id="agent_1",
        items=[
            {"product_id": "prod_1", "variant_id": "var_1", "quantity": 1},
            {"product_id": "prod_2", "variant_id": "var_2", "quantity": 1},
        ],
        discount_codes=[],
        customer_email=None,
        shipping_address={"country": "US", "postal_code": "10001", "city": "New York", "state": "NY"},
        selected_delivery_option=None,
    )

    assert result["store_discount_evidence"]["offers"] == [{"store_discount_id": "store_1", "label": "PIVOTA_TEST_BXGY"}]
    assert result["store_discount_evidence"]["decisions"] == [
        {"type": "store_discount_skipped", "store_discount_id": "expired_1", "reason": "expired"}
    ]
