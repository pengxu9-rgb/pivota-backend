import os

# Ensure db/database.py doesn't fail import-time validation in unit tests.
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/test")

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
        config={"thresholdQuantity": 3, "discountPercent": 10},
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
        config={"thresholdQuantity": 3, "discountPercent": 10},
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
