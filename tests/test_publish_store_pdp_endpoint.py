"""POST /merchant/products/{platform}/{id}/store_pdp/publish — the gated
content-writeback endpoint. Direct-coroutine pattern (no TestClient)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

MERCHANT = {"role": "merchant", "merchant_id": "m1"}


@pytest.mark.asyncio
async def test_publish_non_merchant_403():
    import routes.merchant_products as module
    with pytest.raises(HTTPException) as exc:
        await module.publish_store_pdp(
            platform="shopify", platform_product_id="p1",
            current_user={"role": "buyer", "merchant_id": "m1"})
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_publish_no_copy_when_no_enrichment(monkeypatch):
    import routes.merchant_products as module
    monkeypatch.setattr(module, "get_enrichment", AsyncMock(return_value=None))
    res = await module.publish_store_pdp(
        platform="shopify", platform_product_id="p1", current_user=MERCHANT)
    assert res["status"] == "no_copy"


@pytest.mark.asyncio
async def test_publish_delegates_to_writeback_service(monkeypatch):
    import routes.merchant_products as module
    monkeypatch.setattr(module, "get_enrichment",
                        AsyncMock(return_value={"description_markdown": "D"}))
    pub = AsyncMock(return_value={"status": "blocked", "blocker": "content_writeback_disabled"})
    # endpoint lazy-imports from services.shopify_content_writeback
    monkeypatch.setattr("services.shopify_content_writeback.publish_content_to_store", pub)
    res = await module.publish_store_pdp(
        platform="shopify", platform_product_id="p1", current_user=MERCHANT)
    assert res["status"] == "blocked"
    pub.assert_awaited_once()
    assert pub.await_args.kwargs["merchant_id"] == "m1"
    assert pub.await_args.kwargs["platform_product_id"] == "p1"
