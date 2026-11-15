"""The reviewed retailer keys stay referral-only across checkout configurations."""

from unittest.mock import AsyncMock

import pytest

from routes import agent_commerce_reap as reap
from services.surratt_retailer_pilot import PILOT_FLAG, RETAILERS


@pytest.mark.asyncio
@pytest.mark.parametrize("product_key", RETAILERS)
async def test_reviewed_retailer_cannot_read_purchase_proof(monkeypatch, product_key):
    monkeypatch.delenv(PILOT_FLAG, raising=False)
    db_load = AsyncMock(side_effect=AssertionError("referral must be refused before a purchase lookup"))
    monkeypatch.setattr(reap.database, "fetch_one", db_load)
    with pytest.raises(reap.svc.PurchaseRefused, match="reviewed retailer listing is referral-only"):
        await reap._load_cart_link_item(
            product_key=product_key, merchant_domain=RETAILERS[product_key].host,
            variant_key=None, market_country="US",
        )
    db_load.assert_not_awaited()
