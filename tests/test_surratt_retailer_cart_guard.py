"""The reviewed retailer keys stay referral-only across checkout configurations."""

from unittest.mock import AsyncMock

import pytest

from routes import agent_commerce_reap as reap
from services.surratt_retailer_pilot import EXACT_RETAILER_LISTINGS, KIOKII_FLAG, PILOT_FLAG, PROTECTED_RETAILER_LISTING_KEYS


@pytest.mark.asyncio
@pytest.mark.parametrize("product_key", sorted(PROTECTED_RETAILER_LISTING_KEYS))
async def test_reviewed_retailer_cannot_read_purchase_proof(monkeypatch, product_key):
    monkeypatch.delenv(PILOT_FLAG, raising=False)
    monkeypatch.delenv(KIOKII_FLAG, raising=False)
    db_load = AsyncMock(side_effect=AssertionError("referral must be refused before a purchase lookup"))
    monkeypatch.setattr(reap.database, "fetch_one", db_load)
    listing = EXACT_RETAILER_LISTINGS.get(product_key)
    with pytest.raises(reap.svc.PurchaseRefused, match="reviewed retailer listing is referral-only"):
        await reap._load_cart_link_item(
            product_key=product_key, merchant_domain=listing.host if listing else "kiokii.com",
            variant_key=None, market_country="US",
        )
    db_load.assert_not_awaited()
