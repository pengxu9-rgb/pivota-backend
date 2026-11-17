"""The reviewed retailer keys stay referral-only across checkout configurations."""

from unittest.mock import AsyncMock

import pytest

from routes import agent_commerce_reap as reap
from services.exact_retailer_cohorts import (
    COHORT_B_FLAG, EXACT_RETAILER_LISTINGS, PILOT_FLAG, PROTECTED_RETAILER_LISTING_KEYS, RETAILER_COHORT_B,
)

# The reserved bare listing key has no listing of its own; it belongs to the cohort B retailer host.
COHORT_B_HOST = next(iter(RETAILER_COHORT_B.listings.values())).host


@pytest.mark.asyncio
@pytest.mark.parametrize("product_key", sorted(PROTECTED_RETAILER_LISTING_KEYS))
async def test_reviewed_retailer_cannot_read_purchase_proof(monkeypatch, product_key):
    monkeypatch.delenv(PILOT_FLAG, raising=False)
    monkeypatch.delenv(COHORT_B_FLAG, raising=False)
    db_load = AsyncMock(side_effect=AssertionError("referral must be refused before a purchase lookup"))
    monkeypatch.setattr(reap.database, "fetch_one", db_load)
    listing = EXACT_RETAILER_LISTINGS.get(product_key)
    with pytest.raises(reap.svc.PurchaseRefused, match="reviewed retailer listing is referral-only"):
        await reap._load_cart_link_item(
            product_key=product_key, merchant_domain=listing.host if listing else COHORT_B_HOST,
            variant_key=None, market_country="US",
        )
    db_load.assert_not_awaited()
