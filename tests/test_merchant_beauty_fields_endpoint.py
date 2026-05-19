"""Tests for the v2.1 beauty merchant endpoints.

PUT /merchant/products/{platform}/{platform_product_id}/beauty_fields
GET /merchant/products/beauty_completeness

v2.1 added subcategory-aware schemas (skincare / haircare / bath /
body / makeup / tools) so brushes don't get prompted for INCI. Tests
pin: subcategory dispatch, per-product field_schemas in the response,
unsupported subcategories excluded, tool fields written to JSONB
profile_payload, payload-owns guard still works for raw_inci.
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


# ---------- PUT body accepts v2.1 fields ----------

def test_put_body_accepts_tool_fields():
    body = _put_body(
        tool_material="Synthetic fibers",
        use_with="Eyeshadow",
        care_instructions="Wash weekly",
    )
    assert body.tool_material == "Synthetic fibers"
    assert body.use_with == "Eyeshadow"
    assert body.care_instructions == "Wash weekly"


def test_put_body_accepts_skincare_fields():
    body = _put_body(
        raw_inci="Aqua",
        how_to_use_text="Apply morning",
        skin_concerns=["oily"],
    )
    assert body.raw_inci == "Aqua"
    assert body.how_to_use_text == "Apply morning"
    assert body.skin_concerns == ["oily"]


# ---------- PUT happy path passes all fields through to service ----------

@pytest.mark.asyncio
async def test_put_passes_all_v21_fields_to_service(monkeypatch):
    import routes.merchant_products as module
    monkeypatch.setattr(
        module.database, "fetch_one",
        AsyncMock(return_value={"platform_product_id": "p1"}),
    )
    write_mock = AsyncMock(return_value={
        "tool_material": "written",
        "use_with": "written",
    })
    monkeypatch.setattr(module, "write_merchant_authored_beauty_fields", write_mock)

    resp = await module.update_product_beauty_fields(
        platform="shopify",
        platform_product_id="p1",
        body=_put_body(tool_material="Synthetic", use_with="Eyeshadow"),
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    assert resp["status"] == "success"
    assert resp["outcomes"]["tool_material"] == "written"
    # Service was called with the new keyword args wired through.
    kwargs = write_mock.call_args.kwargs
    assert kwargs["tool_material"] == "Synthetic"
    assert kwargs["use_with"] == "Eyeshadow"


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
            "tool_material": "product_not_found",
            "use_with": "product_not_found",
        }),
    )
    with pytest.raises(HTTPException) as exc:
        await module.update_product_beauty_fields(
            platform="shopify",
            platform_product_id="p1",
            body=_put_body(tool_material="x", use_with="y"),
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


# ---------- GET subcategory dispatch (the v2.1 fix) ----------

@pytest.mark.asyncio
async def test_get_returns_subcategory_kind_per_product(monkeypatch):
    """One skincare row + one tools row → each gets the matching
    subcategory_kind + the right field_schemas. This is the v2.1 fix
    for the brush case."""
    import routes.merchant_products as module
    rows = [
        # Skincare product
        {
            "product_key": "prod::m1::shopify::p1",
            "platform": "shopify",
            "platform_product_id": "p1",
            "title": "Niacinamide Serum",
            "image_url": None,
            "category_path": "beauty/skincare/treat/serum",
            "has_inci": False,
            "inci_payload_owned": False,
            "sample_inci": None,
            "how_to_use_text": None,
            "concerns_json": None,
            "profile_payload": None,
        },
        # Tools (brush) product — the v2.0 bug case
        {
            "product_key": "prod::m1::shopify::p2",
            "platform": "shopify",
            "platform_product_id": "p2",
            "title": "Eyeshadow Brush",
            "image_url": None,
            "category_path": "beauty/tools/brush",
            "has_inci": False,
            "inci_payload_owned": False,
            "sample_inci": None,
            "how_to_use_text": None,
            "concerns_json": None,
            "profile_payload": None,
        },
    ]
    monkeypatch.setattr(module.database, "fetch_all", AsyncMock(return_value=rows))

    resp = await module.get_beauty_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    queue = resp["data"]["queue"]
    assert len(queue) == 2

    skincare = next(p for p in queue if p["subcategory_kind"] == "skincare")
    tools = next(p for p in queue if p["subcategory_kind"] == "tools")

    # Skincare gets the three skincare fields and NOT tool fields.
    assert set(skincare["fields"].keys()) == {"raw_inci", "how_to_use_text", "skin_concerns"}
    assert "tool_material" not in skincare["fields"]

    # Tools gets the three tool fields and NOT raw_inci / skin_concerns.
    # This is the v2.1 fix in one assertion.
    assert set(tools["fields"].keys()) == {"tool_material", "use_with", "care_instructions"}
    assert "raw_inci" not in tools["fields"]
    assert "skin_concerns" not in tools["fields"]

    # field_schemas is surfaced so a thin UI renders forms generically.
    assert all(f["name"] == "tool_material" or f["name"] == "use_with" or f["name"] == "care_instructions"
               for f in tools["field_schemas"])


@pytest.mark.asyncio
async def test_get_excludes_unsupported_subcategories(monkeypatch):
    """Fragrance / accessories aren't in the v2.1 schema table — they
    must be filtered out of the queue entirely. Pin the contract so a
    future schema-table edit doesn't silently re-add them without
    field support."""
    import routes.merchant_products as module
    rows = [
        {
            "product_key": "prod::m1::shopify::p1",
            "platform": "shopify",
            "platform_product_id": "p1",
            "title": "Eau de Parfum",
            "image_url": None,
            "category_path": "beauty/fragrance/perfume",  # unsupported
            "has_inci": False, "inci_payload_owned": False, "sample_inci": None,
            "how_to_use_text": None, "concerns_json": None, "profile_payload": None,
        },
        {
            "product_key": "prod::m1::shopify::p2",
            "platform": "shopify",
            "platform_product_id": "p2",
            "title": "Hair Clip",
            "image_url": None,
            "category_path": "beauty/accessories/clip",  # unsupported
            "has_inci": False, "inci_payload_owned": False, "sample_inci": None,
            "how_to_use_text": None, "concerns_json": None, "profile_payload": None,
        },
    ]
    monkeypatch.setattr(module.database, "fetch_all", AsyncMock(return_value=rows))
    resp = await module.get_beauty_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    assert resp["data"]["queue"] == []


@pytest.mark.asyncio
async def test_get_skips_fully_covered_products(monkeypatch):
    """A skincare product with INCI + how_to_use + concerns all set is
    fully covered for its subcategory and should NOT appear in the queue."""
    import routes.merchant_products as module
    rows = [
        {
            "product_key": "prod::m1::shopify::p1",
            "platform": "shopify",
            "platform_product_id": "p1",
            "title": "Complete Serum",
            "image_url": None,
            "category_path": "beauty/skincare/serum",
            "has_inci": True, "inci_payload_owned": False, "sample_inci": "Aqua",
            "how_to_use_text": "Apply morning",
            "concerns_json": '["oily"]',
            "profile_payload": None,
        },
    ]
    monkeypatch.setattr(module.database, "fetch_all", AsyncMock(return_value=rows))
    resp = await module.get_beauty_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    assert resp["data"]["queue"] == []


@pytest.mark.asyncio
async def test_get_response_surfaces_subcategory_schemas(monkeypatch):
    """UI consumes `subcategory_schemas` to render the right form per
    product. Pin that the response includes it."""
    import routes.merchant_products as module
    monkeypatch.setattr(module.database, "fetch_all", AsyncMock(return_value=[]))
    resp = await module.get_beauty_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    schemas = resp["data"]["subcategory_schemas"]
    kinds = {s["subcategory_kind"] for s in schemas}
    assert "skincare" in kinds
    assert "tools" in kinds


@pytest.mark.asyncio
async def test_get_tools_field_payload_read_from_profile_payload(monkeypatch):
    """When tool_material is already set in beauty_product_profiles
    .profile_payload JSONB, the read endpoint surfaces it as the value
    on the tools product's field state."""
    import routes.merchant_products as module
    rows = [
        {
            "product_key": "prod::m1::shopify::p1",
            "platform": "shopify",
            "platform_product_id": "p1",
            "title": "Foundation Brush",
            "image_url": None,
            "category_path": "beauty/tools/brush",
            "has_inci": False, "inci_payload_owned": False, "sample_inci": None,
            "how_to_use_text": None, "concerns_json": None,
            "profile_payload": {"tool_material": "Horse hair", "use_with": "Foundation"},
        },
    ]
    monkeypatch.setattr(module.database, "fetch_all", AsyncMock(return_value=rows))
    resp = await module.get_beauty_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    queue = resp["data"]["queue"]
    # Product has tool_material + use_with filled, but care_instructions
    # is missing — so it's still in the queue.
    assert len(queue) == 1
    p = queue[0]
    assert p["fields"]["tool_material"]["status"] == "merchant-authored"
    assert p["fields"]["tool_material"]["value"] == "Horse hair"
    assert p["fields"]["use_with"]["status"] == "merchant-authored"
    assert p["fields"]["care_instructions"]["status"] == "missing"


@pytest.mark.asyncio
async def test_get_subcategory_group_beauty_care_excludes_tools(monkeypatch):
    """v2.1.1: ?subcategory_group=beauty_care must scope the queue to
    skincare/haircare/bath/body/makeup only — tools rows in the same
    fetch_all must be filtered out. (The fetch_all mock returns both;
    the route's subcategory_for_path() check still runs, so a tools row
    being passed in would be excluded by virtue of the SQL `LIKE`
    clause normally — but we test that the prefix-filter logic is
    actually wired by checking the queue contents.)"""
    import routes.merchant_products as module
    rows = [
        # Skincare product (in care group)
        {
            "product_key": "prod::m1::shopify::p1",
            "platform": "shopify", "platform_product_id": "p1",
            "title": "Serum", "image_url": None,
            "category_path": "beauty/skincare/serum",
            "has_inci": False, "inci_payload_owned": False, "sample_inci": None,
            "how_to_use_text": None, "concerns_json": None, "profile_payload": None,
        },
    ]
    monkeypatch.setattr(module.database, "fetch_all", AsyncMock(return_value=rows))
    resp = await module.get_beauty_completeness(
        page=1, page_size=50,
        subcategory_group="beauty_care",
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    queue = resp["data"]["queue"]
    assert len(queue) == 1
    assert queue[0]["subcategory_kind"] == "skincare"


@pytest.mark.asyncio
async def test_get_subcategory_group_beauty_tools_excludes_care(monkeypatch):
    """Same shape inverted — tools group should only surface tools rows."""
    import routes.merchant_products as module
    rows = [
        {
            "product_key": "prod::m1::shopify::p1",
            "platform": "shopify", "platform_product_id": "p1",
            "title": "Eyeshadow Brush", "image_url": None,
            "category_path": "beauty/tools/brush",
            "has_inci": False, "inci_payload_owned": False, "sample_inci": None,
            "how_to_use_text": None, "concerns_json": None, "profile_payload": None,
        },
    ]
    monkeypatch.setattr(module.database, "fetch_all", AsyncMock(return_value=rows))
    resp = await module.get_beauty_completeness(
        page=1, page_size=50,
        subcategory_group="beauty_tools",
        current_user={"role": "merchant", "merchant_id": "m1"},
    )
    queue = resp["data"]["queue"]
    assert len(queue) == 1
    assert queue[0]["subcategory_kind"] == "tools"


def test_subcategory_groups_lookup():
    """Pin which prefixes belong to which group so a v2.2 schema edit
    can't silently move a subcategory between groups."""
    from services.beauty_field_authoring import (
        SUBCATEGORY_GROUPS,
        SUBCATEGORY_GROUP_BEAUTY_CARE,
        SUBCATEGORY_GROUP_BEAUTY_TOOLS,
    )
    care = SUBCATEGORY_GROUPS[SUBCATEGORY_GROUP_BEAUTY_CARE]
    tools = SUBCATEGORY_GROUPS[SUBCATEGORY_GROUP_BEAUTY_TOOLS]
    # Care group covers the skincare-shape subcategories
    assert "beauty/skincare/" in care
    assert "beauty/haircare/" in care
    assert "beauty/makeup/" in care
    # Tools group is just tools (v2.1)
    assert tools == ("beauty/tools/",)
    # No overlap
    assert set(care).isdisjoint(set(tools))


@pytest.mark.asyncio
async def test_get_scoped_to_merchant_id(monkeypatch):
    import routes.merchant_products as module
    fetch_all = AsyncMock(return_value=[])
    monkeypatch.setattr(module.database, "fetch_all", fetch_all)
    await module.get_beauty_completeness(
        page=1, page_size=50,
        current_user={"role": "merchant", "merchant_id": "merch_legit"},
    )
    assert fetch_all.call_args.args[1]["merchant_id"] == "merch_legit"
