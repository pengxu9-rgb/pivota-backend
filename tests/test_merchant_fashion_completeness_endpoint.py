"""Tests for GET /merchant/products/fashion_completeness.

Powers the merchant agent surface (/dashboard/agent-chat). Verifies the
per-field status mapping + auth + ownership + the response shape the UI
binds to.

Calls the handler directly (not via TestClient) to mirror the pattern
in test_merchant_products_pagination.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------- field-status mapping helper ----------

def test_fashion_field_status_mapping():
    from routes.merchant_products import _fashion_field_status

    # Missing variants
    assert _fashion_field_status(None, None) == "missing"
    assert _fashion_field_status("", None) == "missing"
    assert _fashion_field_status(None, "merchant_payload") == "missing"

    # Sourced variants
    assert _fashion_field_status("cotton", "merchant_payload") == "merchant-payload-locked"
    assert _fashion_field_status("cotton", "merchant_authored") == "merchant-authored"
    assert _fashion_field_status("cotton", "llm_extraction_v1") == "filled-by-llm"
    assert _fashion_field_status("cotton", "external_seed") == "inherited"

    # Legacy / unknown source with a value falls back to merchant-authored
    # (we don't want to surface a value with no provenance to the
    # merchant; treating as authored is the safe default).
    assert _fashion_field_status("cotton", "legacy_source") == "merchant-authored"
    assert _fashion_field_status("cotton", None) == "merchant-authored"


# ---------- auth ----------

@pytest.mark.asyncio
async def test_non_merchant_role_403(monkeypatch):
    import routes.merchant_products as module
    with pytest.raises(HTTPException) as exc:
        await module.get_fashion_completeness(
            page=1, page_size=50,
            current_user={"role": "buyer", "merchant_id": "m1"},
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_missing_merchant_id_400(monkeypatch):
    import routes.merchant_products as module
    with pytest.raises(HTTPException) as exc:
        await module.get_fashion_completeness(
            page=1, page_size=50,
            current_user={"role": "merchant"},  # no merchant_id
        )
    assert exc.value.status_code == 400


# ---------- happy path ----------

@pytest.mark.asyncio
async def test_happy_path_returns_queue_with_per_field_status(monkeypatch):
    import routes.merchant_products as module

    totals = {
        "fashion_total": 148,
        "missing_material": 146,
        "missing_care": 99,
        "missing_size_guide": 54,
        "total_incomplete": 147,
    }
    rows = [
        {
            "platform": "shopify",
            "platform_product_id": "p1",
            "title": "Velvet Lace Lingerie",
            "image_url": "https://example.com/img.jpg",
            "material": None,
            "material_source": None,
            "material_confidence": None,
            "care": "Hand wash cold",
            "care_source": "llm_extraction_v1",
            "care_confidence": 0.72,
            "size_guide": {"raw": "See chart below"},
            "size_guide_source": "merchant_payload",
            "size_guide_confidence": 1.0,
            "category_path": "fashion/apparel/intimates/lingerie",
        },
    ]
    monkeypatch.setattr(
        module.database, "fetch_one",
        AsyncMock(return_value=totals),
    )
    monkeypatch.setattr(
        module.database, "fetch_all",
        AsyncMock(return_value=rows),
    )

    resp = await module.get_fashion_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )

    assert resp["status"] == "success"
    assert resp["data"]["totals"]["fashion_total"] == 148
    assert resp["data"]["totals"]["missing_material"] == 146
    assert resp["data"]["totals"]["has_more"] is True  # 147 incomplete, page_size 50

    queue = resp["data"]["queue"]
    assert len(queue) == 1
    p = queue[0]
    assert p["platform"] == "shopify"
    assert p["platform_product_id"] == "p1"
    assert p["title"] == "Velvet Lace Lingerie"
    assert p["image_url"] == "https://example.com/img.jpg"

    # Per-field status mapping
    assert p["fields"]["material"]["status"] == "missing"
    assert p["fields"]["material"]["value"] is None

    assert p["fields"]["care"]["status"] == "filled-by-llm"
    assert p["fields"]["care"]["value"] == "Hand wash cold"
    assert p["fields"]["care"]["confidence"] == 0.72

    assert p["fields"]["size_guide"]["status"] == "merchant-payload-locked"
    assert p["fields"]["size_guide"]["value"] == {"raw": "See chart below"}


@pytest.mark.asyncio
async def test_empty_queue_returns_zero_totals(monkeypatch):
    import routes.merchant_products as module
    monkeypatch.setattr(
        module.database, "fetch_one",
        AsyncMock(return_value={
            "fashion_total": 0, "missing_material": 0,
            "missing_care": 0, "missing_size_guide": 0,
            "total_incomplete": 0,
        }),
    )
    monkeypatch.setattr(
        module.database, "fetch_all", AsyncMock(return_value=[]),
    )
    resp = await module.get_fashion_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    assert resp["status"] == "success"
    assert resp["data"]["queue"] == []
    assert resp["data"]["totals"]["fashion_total"] == 0
    assert resp["data"]["totals"]["has_more"] is False


@pytest.mark.asyncio
async def test_page_2_has_more_logic(monkeypatch):
    """Page 2 of 200 incomplete with page_size 50 → has_more True if
    (page * page_size) < total. (2*50=100) < 200, so has_more=True."""
    import routes.merchant_products as module
    monkeypatch.setattr(
        module.database, "fetch_one",
        AsyncMock(return_value={
            "fashion_total": 250, "missing_material": 250,
            "missing_care": 100, "missing_size_guide": 80,
            "total_incomplete": 200,
        }),
    )
    monkeypatch.setattr(
        module.database, "fetch_all", AsyncMock(return_value=[]),
    )
    resp = await module.get_fashion_completeness(
        page=2, page_size=50,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    assert resp["data"]["totals"]["has_more"] is True
    assert resp["data"]["totals"]["page"] == 2


@pytest.mark.asyncio
async def test_query_scoped_to_merchant_id(monkeypatch):
    """The SQL binds :merchant_id from current_user, NOT from a query
    param. Verifies the auth scoping can't be bypassed by trying to
    pass a different merchant_id via query string."""
    import routes.merchant_products as module
    fetch_one = AsyncMock(return_value={
        "fashion_total": 0, "missing_material": 0,
        "missing_care": 0, "missing_size_guide": 0,
        "total_incomplete": 0,
    })
    fetch_all = AsyncMock(return_value=[])
    monkeypatch.setattr(module.database, "fetch_one", fetch_one)
    monkeypatch.setattr(module.database, "fetch_all", fetch_all)

    await module.get_fashion_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "merch_legit"},
    )
    # Both calls should bind merchant_id from the auth context.
    assert fetch_one.call_args.args[1]["merchant_id"] == "merch_legit"
    assert fetch_all.call_args.args[1]["merchant_id"] == "merch_legit"
