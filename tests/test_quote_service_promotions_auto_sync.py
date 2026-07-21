from decimal import Decimal
from types import SimpleNamespace

import pytest

from services.quote_service import QuoteService


@pytest.mark.asyncio
async def test_quote_service_promotion_load_falls_back_to_any_channel(monkeypatch):
    svc = QuoteService()

    calls = []

    promo = SimpleNamespace(
        id="promo_1",
        type="MULTI_BUY_DISCOUNT",
        scope={"global": True},
        config={"source": "pivota_manual", "thresholdQuantity": 3, "discountPercent": 10},
        humanReadableRule="Buy 3, get 10% off",
        name="Buy 3 deal",
    )

    async def fake_list_promotions(*, merchant_id, status, channel=None, creator_id=None, search=None, limit=50, offset=0):
        calls.append({"channel": channel, "creator_id": creator_id})
        if channel is None:
            return ([promo], 1)
        return ([], 0)

    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "0")

    pricing = {
        "subtotal": Decimal("100.00"),
        "discount_total": Decimal("0.00"),
        "shipping_fee": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "total": Decimal("100.00"),
    }
    line_items = [
        {"product_id": "p1", "quantity": 3, "unit_price_effective": Decimal("10.00")},
    ]
    promotion_lines = []

    await svc._apply_infra_promotions_best_effort(
        merchant_id="merch_1",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 3}],
        pricing=pricing,
        line_items=line_items,
        promotion_lines=promotion_lines,
        creator_id="agent_1",
        channel="creator_agents",
    )

    assert calls[0]["channel"] == "creator_agents"
    assert calls[1]["channel"] is None
    assert pricing["discount_total"] == Decimal("3.00")
    assert pricing["total"] == Decimal("97.00")
    assert len(promotion_lines) == 1


@pytest.mark.asyncio
async def test_quote_service_auto_sync_shopify_promotions_when_missing(monkeypatch):
    svc = QuoteService()

    promo = SimpleNamespace(
        id="promo_2",
        type="MULTI_BUY_DISCOUNT",
        scope={"global": True},
        config={"source": "pivota_manual", "thresholdQuantity": 3, "discountPercent": 10},
        humanReadableRule="Buy 3, get 10% off",
        name="Buy 3 deal",
    )

    calls = {"list": 0, "sync": 0}

    async def fake_list_promotions(*, merchant_id, status, channel=None, creator_id=None, search=None, limit=50, offset=0):
        calls["list"] += 1
        # 1) channel scoped -> empty
        # 2) any-channel -> empty
        # 3) after sync -> returns promo
        if calls["list"] >= 3 and channel == "creator_agents":
            return ([promo], 1)
        return ([], 0)

    async def fake_sync_shopify_promotions_for_merchant(*, merchant_id, channel="creator_agents"):
        calls["sync"] += 1
        return {"merchantId": merchant_id, "rulesFetched": 1, "created": 1, "updated": 0, "skipped": 0}

    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setattr("services.quote_service.sync_shopify_promotions_for_merchant", fake_sync_shopify_promotions_for_merchant)
    monkeypatch.setattr("services.quote_service._should_attempt_shopify_promotions_sync", lambda _mid: True)
    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "1")
    # Allow the quote path a tiny sync budget in tests so we can assert the promo applies.
    monkeypatch.setenv("PROMOTIONS_SYNC_QUOTE_WAIT_SECONDS", "1")

    pricing = {
        "subtotal": Decimal("100.00"),
        "discount_total": Decimal("0.00"),
        "shipping_fee": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "total": Decimal("100.00"),
    }
    line_items = [
        {"product_id": "p1", "quantity": 3, "unit_price_effective": Decimal("10.00")},
    ]
    promotion_lines = []

    await svc._apply_infra_promotions_best_effort(
        merchant_id="merch_1",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 3}],
        pricing=pricing,
        line_items=line_items,
        promotion_lines=promotion_lines,
        creator_id="agent_1",
        channel="creator_agents",
    )

    assert calls["sync"] == 1
    assert pricing["discount_total"] == Decimal("3.00")
    assert len(promotion_lines) == 1


@pytest.mark.asyncio
async def test_quote_service_auto_syncs_shopify_metadata_when_only_manual_promo_exists(monkeypatch):
    svc = QuoteService()

    promo = SimpleNamespace(
        id="promo_manual",
        type="MULTI_BUY_DISCOUNT",
        scope={"global": True},
        config={"source": "pivota_manual", "thresholdQuantity": 3, "discountPercent": 10},
        humanReadableRule="Buy 3, get 10% off",
        name="Buy 3 deal",
    )

    calls = {"sync": 0}

    async def fake_list_promotions(*, merchant_id, status, channel=None, creator_id=None, search=None, limit=50, offset=0):
        return ([promo], 1)

    async def fake_sync_shopify_promotions_for_merchant(*, merchant_id, channel="creator_agents"):
        calls["sync"] += 1
        return {"merchantId": merchant_id, "discountNodesFetched": 1, "created": 1, "updated": 0, "skipped": 0}

    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setattr("services.quote_service.sync_shopify_promotions_for_merchant", fake_sync_shopify_promotions_for_merchant)
    monkeypatch.setattr("services.quote_service._should_attempt_shopify_promotions_sync", lambda _mid: True)
    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "1")
    monkeypatch.setenv("PROMOTIONS_SYNC_QUOTE_WAIT_SECONDS", "1")

    pricing = {
        "subtotal": Decimal("100.00"),
        "discount_total": Decimal("0.00"),
        "shipping_fee": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "total": Decimal("100.00"),
    }
    promotion_lines = []
    evidence = {"codes": [], "applications": [], "decisions": [], "pricing_confidence": "authoritative"}

    await svc._apply_infra_promotions_best_effort(
        merchant_id="merch_1",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 3}],
        pricing=pricing,
        line_items=[{"product_id": "p1", "quantity": 3, "unit_price_effective": Decimal("10.00")}],
        promotion_lines=promotion_lines,
        discount_evidence=evidence,
        creator_id="agent_1",
        channel="creator_agents",
    )

    assert calls["sync"] == 1
    assert pricing["discount_total"] == Decimal("3.00")
    assert pricing["total"] == Decimal("97.00")
    assert len(promotion_lines) == 1
    assert evidence["pricing_confidence"] == "partial"
    assert evidence["decisions"][0]["decision"] == "applied"
    assert evidence["decisions"][0]["reason"] == "pivota_manual_adjustment_not_shopify_allocation"


@pytest.mark.asyncio
async def test_quote_service_skips_unscoped_legacy_manual_promo(monkeypatch):
    svc = QuoteService()

    promo = SimpleNamespace(
        id="promo_legacy_global",
        type="MULTI_BUY_DISCOUNT",
        scope={"global": True, "brandIds": [], "productIds": [], "categoryIds": []},
        config={"thresholdQuantity": 3, "discountPercent": 20},
        humanReadableRule="Buy 3, get 20% off",
        name="Legacy global deal",
    )

    async def fake_list_promotions(*, merchant_id, status, channel=None, creator_id=None, search=None, limit=50, offset=0):
        return ([promo], 1)

    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "0")

    pricing = {
        "subtotal": Decimal("100.00"),
        "discount_total": Decimal("0.00"),
        "shipping_fee": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "total": Decimal("100.00"),
    }
    evidence = {"codes": [], "applications": [], "decisions": []}
    promotion_lines = []

    await svc._apply_infra_promotions_best_effort(
        merchant_id="merch_1",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 3}],
        pricing=pricing,
        line_items=[{"product_id": "p1", "variant_id": "v1", "quantity": 3, "unit_price_effective": Decimal("10.00")}],
        promotion_lines=promotion_lines,
        discount_evidence=evidence,
        creator_id="agent_1",
        channel="creator_agents",
    )

    assert pricing["discount_total"] == Decimal("0.00")
    assert pricing["total"] == Decimal("100.00")
    assert promotion_lines == []
    assert evidence["decisions"][0]["decision"] == "skipped"
    assert evidence["decisions"][0]["reason"] == "legacy_unscoped_manual_promotion"


@pytest.mark.asyncio
async def test_quote_service_allows_scoped_manual_promo_without_source(monkeypatch):
    svc = QuoteService()

    promo = SimpleNamespace(
        id="promo_scoped_manual",
        type="MULTI_BUY_DISCOUNT",
        scope={"global": False, "productIds": ["p1"]},
        config={"thresholdQuantity": 3, "discountPercent": 20},
        humanReadableRule="Buy 3, get 20% off",
        name="Scoped deal",
    )

    async def fake_list_promotions(*, merchant_id, status, channel=None, creator_id=None, search=None, limit=50, offset=0):
        return ([promo], 1)

    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "0")

    pricing = {
        "subtotal": Decimal("100.00"),
        "discount_total": Decimal("0.00"),
        "shipping_fee": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "total": Decimal("100.00"),
    }
    evidence = {"codes": [], "applications": [], "decisions": []}
    promotion_lines = []

    await svc._apply_infra_promotions_best_effort(
        merchant_id="merch_1",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 3}],
        pricing=pricing,
        line_items=[{"product_id": "p1", "variant_id": "v1", "quantity": 3, "unit_price_effective": Decimal("10.00")}],
        promotion_lines=promotion_lines,
        discount_evidence=evidence,
        creator_id="agent_1",
        channel="creator_agents",
    )

    assert pricing["discount_total"] == Decimal("6.00")
    assert pricing["total"] == Decimal("94.00")
    assert len(promotion_lines) == 1
    assert evidence["decisions"][0]["decision"] == "applied"
    assert evidence["decisions"][0]["reason"] == "pivota_manual_adjustment_not_shopify_allocation"


@pytest.mark.asyncio
async def test_quote_service_skips_manual_promo_when_shopify_discount_present(monkeypatch):
    svc = QuoteService()

    promo = SimpleNamespace(
        id="promo_skip",
        type="MULTI_BUY_DISCOUNT",
        scope={"global": True},
        config={"source": "pivota_manual", "thresholdQuantity": 3, "discountPercent": 10},
        humanReadableRule="Buy 3, get 10% off",
        name="Buy 3 deal",
    )

    async def fake_list_promotions(*, merchant_id, status, channel=None, creator_id=None, search=None, limit=50, offset=0):
        return ([promo], 1)

    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "0")

    pricing = {
        "subtotal": Decimal("100.00"),
        "discount_total": Decimal("5.00"),
        "shipping_fee": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "total": Decimal("95.00"),
    }
    evidence = {
        "codes": [{"code": "SAVE5", "applicable": True, "source": "shopify_storefront_cart"}],
        "applications": [
            {
                "source": "shopify",
                "discount_class": "product",
                "method": "code",
                "code": "SAVE5",
                "amount": "-5.00",
            }
        ],
        "decisions": [],
    }

    await svc._apply_infra_promotions_best_effort(
        merchant_id="merch_1",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 3}],
        pricing=pricing,
        line_items=[{"product_id": "p1", "quantity": 3, "unit_price_effective": Decimal("10.00")}],
        promotion_lines=[],
        discount_evidence=evidence,
        creator_id="agent_1",
        channel="creator_agents",
    )

    assert pricing["discount_total"] == Decimal("5.00")
    assert pricing["total"] == Decimal("95.00")
    assert evidence["decisions"][0]["decision"] == "skipped"
    assert evidence["decisions"][0]["reason"] == "shopify_discount_present"


@pytest.mark.asyncio
async def test_quote_service_skips_manual_promo_when_shopify_code_rejected(monkeypatch):
    svc = QuoteService()

    promo = SimpleNamespace(
        id="promo_invalid_code_mask",
        type="MULTI_BUY_DISCOUNT",
        scope={"global": True},
        config={"source": "pivota_manual", "thresholdQuantity": 3, "discountPercent": 10},
        humanReadableRule="Buy 3, get 10% off",
        name="Buy 3 deal",
    )

    async def fake_list_promotions(*, merchant_id, status, channel=None, creator_id=None, search=None, limit=50, offset=0):
        return ([promo], 1)

    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "0")

    pricing = {
        "subtotal": Decimal("100.00"),
        "discount_total": Decimal("0.00"),
        "shipping_fee": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "total": Decimal("100.00"),
    }
    evidence = {
        "codes": [{"code": "BADCODE", "applicable": False, "source": "shopify_storefront_cart"}],
        "applications": [],
        "decisions": [],
        "pricing_confidence": "partial",
    }
    promotion_lines = []

    await svc._apply_infra_promotions_best_effort(
        merchant_id="merch_1",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 3}],
        pricing=pricing,
        line_items=[{"product_id": "p1", "quantity": 3, "unit_price_effective": Decimal("10.00")}],
        promotion_lines=promotion_lines,
        discount_evidence=evidence,
        creator_id="agent_1",
        channel="creator_agents",
    )

    assert pricing["discount_total"] == Decimal("0.00")
    assert pricing["total"] == Decimal("100.00")
    assert promotion_lines == []
    assert evidence["decisions"][0]["decision"] == "skipped"
    assert evidence["decisions"][0]["reason"] == "shopify_code_rejected"


@pytest.mark.asyncio
async def test_quote_service_allows_manual_promo_after_rejected_code_when_explicitly_configured(monkeypatch):
    svc = QuoteService()

    promo = SimpleNamespace(
        id="promo_invalid_code_fallback_allowed",
        type="MULTI_BUY_DISCOUNT",
        scope={"global": True},
        config={
            "source": "pivota_manual",
            "thresholdQuantity": 3,
            "discountPercent": 10,
            "canApplyWhenShopifyCodeRejected": True,
        },
        humanReadableRule="Buy 3, get 10% off",
        name="Buy 3 deal",
    )

    async def fake_list_promotions(*, merchant_id, status, channel=None, creator_id=None, search=None, limit=50, offset=0):
        return ([promo], 1)

    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "0")

    pricing = {
        "subtotal": Decimal("100.00"),
        "discount_total": Decimal("0.00"),
        "shipping_fee": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "total": Decimal("100.00"),
    }
    evidence = {
        "codes": [{"code": "BADCODE", "applicable": False, "source": "shopify_storefront_cart"}],
        "applications": [],
        "decisions": [],
        "pricing_confidence": "partial",
    }
    promotion_lines = []

    await svc._apply_infra_promotions_best_effort(
        merchant_id="merch_1",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 3}],
        pricing=pricing,
        line_items=[{"product_id": "p1", "quantity": 3, "unit_price_effective": Decimal("10.00")}],
        promotion_lines=promotion_lines,
        discount_evidence=evidence,
        creator_id="agent_1",
        channel="creator_agents",
    )

    assert pricing["discount_total"] == Decimal("3.00")
    assert pricing["total"] == Decimal("97.00")
    assert len(promotion_lines) == 1
    assert evidence["decisions"][0]["decision"] == "applied"


@pytest.mark.asyncio
async def test_quote_service_allows_manual_stack_when_same_discount_class_allowed(monkeypatch):
    svc = QuoteService()

    promo = SimpleNamespace(
        id="promo_stack",
        type="MULTI_BUY_DISCOUNT",
        scope={"global": True},
        config={
            "source": "pivota_manual",
            "thresholdQuantity": 3,
            "discountPercent": 10,
            "canStackWithShopify": True,
            "combinesWith": {"productDiscounts": True},
        },
        humanReadableRule="Buy 3, get 10% off",
        name="Buy 3 deal",
    )

    async def fake_list_promotions(*, merchant_id, status, channel=None, creator_id=None, search=None, limit=50, offset=0):
        return ([promo], 1)

    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "0")

    pricing = {
        "subtotal": Decimal("100.00"),
        "discount_total": Decimal("5.00"),
        "shipping_fee": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "total": Decimal("95.00"),
    }
    promotion_lines = []
    evidence = {
        "codes": [{"code": "SAVE5", "applicable": True, "source": "shopify_storefront_cart"}],
        "applications": [{"source": "shopify", "discount_class": "product", "amount": "-5.00"}],
        "decisions": [],
    }

    await svc._apply_infra_promotions_best_effort(
        merchant_id="merch_1",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 3}],
        pricing=pricing,
        line_items=[{"product_id": "p1", "quantity": 3, "unit_price_effective": Decimal("10.00")}],
        promotion_lines=promotion_lines,
        discount_evidence=evidence,
        creator_id="agent_1",
        channel="creator_agents",
    )

    assert pricing["discount_total"] == Decimal("8.00")
    assert pricing["total"] == Decimal("92.00")
    assert len(promotion_lines) == 1


@pytest.mark.asyncio
async def test_quote_service_blocks_manual_new_customer_promo_without_shopify_evidence(monkeypatch):
    svc = QuoteService()

    promo = SimpleNamespace(
        id="promo_new_customer",
        type="MULTI_BUY_DISCOUNT",
        scope={"global": True},
        config={"source": "pivota_manual", "thresholdQuantity": 1, "discountPercent": 10, "newCustomerOnly": True},
        humanReadableRule="New customer 10% off",
        name="New customer deal",
    )

    async def fake_list_promotions(*, merchant_id, status, channel=None, creator_id=None, search=None, limit=50, offset=0):
        return ([promo], 1)

    monkeypatch.setattr("services.quote_service.list_promotions", fake_list_promotions)
    monkeypatch.setenv("AUTO_SYNC_SHOPIFY_PROMOTIONS_ON_QUOTE_PREVIEW", "0")

    pricing = {
        "subtotal": Decimal("100.00"),
        "discount_total": Decimal("0.00"),
        "shipping_fee": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "total": Decimal("100.00"),
    }
    evidence = {"codes": [], "applications": [], "decisions": []}

    await svc._apply_infra_promotions_best_effort(
        merchant_id="merch_1",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 1}],
        pricing=pricing,
        line_items=[{"product_id": "p1", "quantity": 1, "unit_price_effective": Decimal("10.00")}],
        promotion_lines=[],
        discount_evidence=evidence,
        creator_id="agent_1",
        channel="creator_agents",
    )

    assert pricing["discount_total"] == Decimal("0.00")
    assert evidence["decisions"][0]["reason"] == "shopify_new_customer_unverified_or_ineligible"
