"""Tests for PUT /merchant/products/{platform}/{platform_product_id}/fashion_fields.

Mirrors the calling-handler-direct pattern in
test_merchant_products_pagination.py — no TestClient setup needed, the
route handler is invoked as a plain coroutine.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _body(**fields):
    """Build a FashionFieldsBody pydantic model from kwargs."""
    from routes.merchant_products import FashionFieldsBody
    return FashionFieldsBody(**fields)


# ---------- auth ----------

@pytest.mark.asyncio
async def test_non_merchant_role_403(monkeypatch):
    import routes.merchant_products as module
    with pytest.raises(HTTPException) as exc:
        await module.update_product_fashion_fields(
            platform="shopify",
            platform_product_id="p1",
            body=_body(material="cotton"),
            current_user={"role": "buyer", "merchant_id": "m1"},
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_missing_merchant_id_400(monkeypatch):
    import routes.merchant_products as module
    with pytest.raises(HTTPException) as exc:
        await module.update_product_fashion_fields(
            platform="shopify",
            platform_product_id="p1",
            body=_body(material="cotton"),
            current_user={"role": "merchant"},  # no merchant_id
        )
    assert exc.value.status_code == 400


# ---------- ownership ----------

@pytest.mark.asyncio
async def test_product_not_in_cache_returns_404(monkeypatch):
    import routes.merchant_products as module
    monkeypatch.setattr(module.database, "fetch_one", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await module.update_product_fashion_fields(
            platform="shopify",
            platform_product_id="missing",
            body=_body(material="cotton"),
            current_user={"role": "merchant", "merchant_id": "m1"},
        )
    assert exc.value.status_code == 404


# ---------- happy path ----------

@pytest.mark.asyncio
async def test_happy_path_calls_service_and_returns_outcomes(monkeypatch):
    import routes.merchant_products as module
    monkeypatch.setattr(
        module.database, "fetch_one",
        AsyncMock(return_value={"platform_product_id": "p1"}),
    )
    write_mock = AsyncMock(return_value={
        "material": "written",
        "care": "skipped_payload_owned",
    })
    monkeypatch.setattr(module, "write_merchant_authored_fashion_fields", write_mock)

    resp = await module.update_product_fashion_fields(
        platform="shopify",
        platform_product_id="p1",
        body=_body(material="cotton", care="hand wash"),
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    assert resp == {
        "status": "success",
        "outcomes": {
            "material": "written",
            "care": "skipped_payload_owned",
        },
    }
    write_mock.assert_awaited_once_with(
        merchant_id="m1",
        platform="shopify",
        source_product_id="p1",
        material="cotton",
        care="hand wash",
        size_guide=None,
    )


@pytest.mark.asyncio
async def test_size_guide_dict_passes_through(monkeypatch):
    """JSONB size_guide accepts structured chart dicts as well as strings."""
    import routes.merchant_products as module
    monkeypatch.setattr(
        module.database, "fetch_one",
        AsyncMock(return_value={"platform_product_id": "p1"}),
    )
    write_mock = AsyncMock(return_value={"size_guide": "written"})
    monkeypatch.setattr(module, "write_merchant_authored_fashion_fields", write_mock)

    chart = {"columns": ["S", "M"], "rows": [{"chest": "32-34"}]}
    await module.update_product_fashion_fields(
        platform="shopify",
        platform_product_id="p1",
        body=_body(size_guide=chart),
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    write_mock.assert_awaited_once_with(
        merchant_id="m1",
        platform="shopify",
        source_product_id="p1",
        material=None,
        care=None,
        size_guide=chart,
    )


@pytest.mark.asyncio
async def test_all_null_body_still_calls_service(monkeypatch):
    """A no-op call is fine — the service returns an empty outcomes dict
    and the response shape stays consistent."""
    import routes.merchant_products as module
    monkeypatch.setattr(
        module.database, "fetch_one",
        AsyncMock(return_value={"platform_product_id": "p1"}),
    )
    write_mock = AsyncMock(return_value={})
    monkeypatch.setattr(module, "write_merchant_authored_fashion_fields", write_mock)

    resp = await module.update_product_fashion_fields(
        platform="shopify",
        platform_product_id="p1",
        body=_body(),  # all fields None
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    assert resp == {"status": "success", "outcomes": {}}
    write_mock.assert_awaited_once()
