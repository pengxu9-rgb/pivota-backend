import pytest
from fastapi import HTTPException

from routes.universal_product_sync import UniversalSyncResponse


@pytest.mark.asyncio
async def test_sync_platform_products_endpoint_passes_platform_hint(monkeypatch):
    from routes import platform_products_sync_api as module

    captured = {}

    async def fake_universal_product_sync(request, background_tasks, current_user):
        captured["request"] = request
        captured["current_user"] = current_user
        return UniversalSyncResponse(
            status="success",
            message="Successfully synced 2 products from Wix",
            merchant_id=request.merchant_id,
            platform=request.platform,
            products_synced=2,
            sync_time="2026-05-18T00:00:00",
        )

    monkeypatch.setattr(module, "universal_product_sync", fake_universal_product_sync)

    result = await module.sync_platform_products_endpoint(
        merchant_id="merch_1",
        platform="wix",
        limit=25,
        _=None,
    )

    assert result["ok"] is True
    assert result["summary"]["platform"] == "wix"
    assert captured["request"].platform == "wix"
    assert captured["request"].limit == 25
    assert captured["current_user"] == {"role": "admin"}


@pytest.mark.asyncio
async def test_sync_platform_products_endpoint_maps_warning_to_400(monkeypatch):
    from routes import platform_products_sync_api as module

    async def fake_universal_product_sync(request, background_tasks, current_user):
        return UniversalSyncResponse(
            status="warning",
            message="Wix API credentials are missing or incomplete",
            merchant_id=request.merchant_id,
            platform=request.platform or "wix",
            products_synced=0,
            sync_time="2026-05-18T00:00:00",
        )

    monkeypatch.setattr(module, "universal_product_sync", fake_universal_product_sync)

    with pytest.raises(HTTPException) as exc_info:
        await module.sync_platform_products_endpoint(
            merchant_id="merch_1",
            platform="wix",
            limit=25,
            _=None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "PLATFORM_PRODUCTS_WARNING"
