"""Tests for the beauty merchant endpoints:
  - PUT /merchant/products/{platform}/{platform_product_id}/beauty_fields
  - GET /merchant/products/beauty_completeness

Calls the handlers directly via monkeypatched apiClient mocks (matches
the pattern in test_merchant_products_pagination / fashion endpoint tests).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _put_body(**fields):
    from routes.merchant_products import BeautyFieldsBody
    return BeautyFieldsBody(**fields)


# ---------- PUT auth ----------

@pytest.mark.asyncio
async def test_put_non_merchant_role_403(monkeypatch):
    import routes.merchant_products as module
    with pytest.raises(HTTPException) as exc:
        await module.update_product_beauty_fields(
            platform="shopify",
            platform_product_id="p1",
            body=_put_body(raw_inci="Aqua"),
            current_user={"role": "buyer", "merchant_id": "m1"},
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_put_missing_merchant_id_400(monkeypatch):
    import routes.merchant_products as module
    with pytest.raises(HTTPException) as exc:
        await module.update_product_beauty_fields(
            platform="shopify",
            platform_product_id="p1",
            body=_put_body(raw_inci="Aqua"),
            current_user={"role": "merchant"},
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_put_product_not_in_cache_404(monkeypatch):
    import routes.merchant_products as module
    monkeypatch.setattr(module.database, "fetch_one", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await module.update_product_beauty_fields(
            platform="shopify",
            platform_product_id="missing",
            body=_put_body(raw_inci="Aqua"),
            current_user={"role": "merchant", "merchant_id": "m1"},
        )
    assert exc.value.status_code == 404


# ---------- PUT happy path ----------

@pytest.mark.asyncio
async def test_put_happy_path_returns_outcomes_and_allowed_concerns(monkeypatch):
    import routes.merchant_products as module
    monkeypatch.setattr(
        module.database, "fetch_one",
        AsyncMock(return_value={"platform_product_id": "p1"}),
    )
    write_mock = AsyncMock(return_value={
        "raw_inci": "written",
        "how_to_use_text": "written",
    })
    monkeypatch.setattr(
        module, "write_merchant_authored_beauty_fields", write_mock,
    )
    resp = await module.update_product_beauty_fields(
        platform="shopify",
        platform_product_id="p1",
        body=_put_body(raw_inci="Aqua", how_to_use_text="apply"),
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    assert resp["status"] == "success"
    assert resp["outcomes"]["raw_inci"] == "written"
    # Allowed enum is surfaced so a thin UI can render the multi-select
    # without hardcoding the values.
    assert "oily" in resp["allowed_skin_concerns"]
    assert "acne-prone" in resp["allowed_skin_concerns"]


@pytest.mark.asyncio
async def test_put_all_outcomes_product_not_found_returns_404(monkeypatch):
    import routes.merchant_products as module
    monkeypatch.setattr(
        module.database, "fetch_one",
        AsyncMock(return_value={"platform_product_id": "p1"}),
    )
    monkeypatch.setattr(
        module, "write_merchant_authored_beauty_fields",
        AsyncMock(return_value={
            "raw_inci": "product_not_found",
            "how_to_use_text": "product_not_found",
        }),
    )
    with pytest.raises(HTTPException) as exc:
        await module.update_product_beauty_fields(
            platform="shopify",
            platform_product_id="p1",
            body=_put_body(raw_inci="x", how_to_use_text="y"),
            current_user={"role": "merchant", "merchant_id": "m1"},
        )
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "catalog_product_not_found"


# ---------- GET auth ----------

@pytest.mark.asyncio
async def test_get_non_merchant_role_403(monkeypatch):
    import routes.merchant_products as module
    with pytest.raises(HTTPException) as exc:
        await module.get_beauty_completeness(
            page=1, page_size=50,
            current_user={"role": "buyer", "merchant_id": "m1"},
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_missing_merchant_id_400(monkeypatch):
    import routes.merchant_products as module
    with pytest.raises(HTTPException) as exc:
        await module.get_beauty_completeness(
            page=1, page_size=50,
            current_user={"role": "merchant"},
        )
    assert exc.value.status_code == 400


# ---------- GET happy path ----------

@pytest.mark.asyncio
async def test_get_happy_path_returns_queue_with_per_field_status(monkeypatch):
    import routes.merchant_products as module
    totals = {
        "beauty_total": 36,
        "missing_inci": 30,
        "missing_how_to_use": 20,
        "missing_concerns": 18,
        "total_incomplete": 32,
    }
    rows = [
        {
            "platform": "shopify",
            "platform_product_id": "p1",
            "title": "Niacinamide Serum",
            "image_url": "https://example.com/img.jpg",
            "category_path": "beauty/skincare/treat/serum",
            "has_inci": True,
            "inci_payload_owned": True,
            "sample_inci": "Aqua, Niacinamide, Glycerin",
            "how_to_use_text": None,
            "concerns_json": None,
        },
        {
            "platform": "shopify",
            "platform_product_id": "p2",
            "title": "Vitamin C Toner",
            "image_url": None,
            "category_path": "beauty/skincare/treat/toner",
            "has_inci": False,
            "inci_payload_owned": False,
            "sample_inci": None,
            "how_to_use_text": "Apply morning and evening",
            "concerns_json": '["oily", "acne-prone"]',
        },
    ]
    monkeypatch.setattr(
        module.database, "fetch_one", AsyncMock(return_value=totals),
    )
    monkeypatch.setattr(
        module.database, "fetch_all", AsyncMock(return_value=rows),
    )
    resp = await module.get_beauty_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    assert resp["status"] == "success"
    assert resp["data"]["totals"]["beauty_total"] == 36
    assert resp["data"]["totals"]["missing_inci"] == 30
    queue = resp["data"]["queue"]
    assert len(queue) == 2

    # First product: INCI is payload-locked (Shopify metafield)
    p1 = queue[0]
    assert p1["category_kind"] == "beauty"
    assert p1["fields"]["raw_inci"]["status"] == "merchant-payload-locked"
    assert p1["fields"]["raw_inci"]["value"] == "Aqua, Niacinamide, Glycerin"
    assert p1["fields"]["how_to_use_text"]["status"] == "missing"
    assert p1["fields"]["skin_concerns"]["status"] == "missing"

    # Second product: missing INCI but has the other two
    p2 = queue[1]
    assert p2["fields"]["raw_inci"]["status"] == "missing"
    assert p2["fields"]["how_to_use_text"]["status"] == "merchant-authored"
    assert p2["fields"]["how_to_use_text"]["value"] == "Apply morning and evening"
    # concerns_json arrived as a JSON-encoded string from the DB;
    # the route must parse it into a list.
    assert p2["fields"]["skin_concerns"]["status"] == "merchant-authored"
    assert p2["fields"]["skin_concerns"]["value"] == ["oily", "acne-prone"]


@pytest.mark.asyncio
async def test_get_empty_queue_returns_zero_totals(monkeypatch):
    import routes.merchant_products as module
    monkeypatch.setattr(
        module.database, "fetch_one",
        AsyncMock(return_value={
            "beauty_total": 0, "missing_inci": 0,
            "missing_how_to_use": 0, "missing_concerns": 0,
            "total_incomplete": 0,
        }),
    )
    monkeypatch.setattr(
        module.database, "fetch_all", AsyncMock(return_value=[]),
    )
    resp = await module.get_beauty_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    assert resp["data"]["queue"] == []
    assert resp["data"]["totals"]["has_more"] is False


@pytest.mark.asyncio
async def test_get_scoped_to_merchant_id(monkeypatch):
    """merchant_id binds from auth context, not query string."""
    import routes.merchant_products as module
    fetch_one = AsyncMock(return_value={
        "beauty_total": 0, "missing_inci": 0,
        "missing_how_to_use": 0, "missing_concerns": 0,
        "total_incomplete": 0,
    })
    fetch_all = AsyncMock(return_value=[])
    monkeypatch.setattr(module.database, "fetch_one", fetch_one)
    monkeypatch.setattr(module.database, "fetch_all", fetch_all)
    await module.get_beauty_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "merch_legit"},
    )
    assert fetch_one.call_args.args[1]["merchant_id"] == "merch_legit"
    assert fetch_all.call_args.args[1]["merchant_id"] == "merch_legit"


@pytest.mark.asyncio
async def test_get_response_includes_allowed_skin_concerns_enum(monkeypatch):
    """UI uses this to render the multi-select without hard-coding the enum."""
    import routes.merchant_products as module
    monkeypatch.setattr(
        module.database, "fetch_one",
        AsyncMock(return_value={
            "beauty_total": 0, "missing_inci": 0,
            "missing_how_to_use": 0, "missing_concerns": 0,
            "total_incomplete": 0,
        }),
    )
    monkeypatch.setattr(module.database, "fetch_all", AsyncMock(return_value=[]))
    resp = await module.get_beauty_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    assert "oily" in resp["data"]["allowed_skin_concerns"]
    assert "acne-prone" in resp["data"]["allowed_skin_concerns"]
