"""Tests for services.beauty_field_authoring v2.1.

Covers per-subcategory schema dispatch, the new tool fields, JSONB
profile_payload writes, and the existing raw_inci/how_to_use/concerns
paths. Mocks the global `database` so tests pin SQL shape + transaction
lifecycle without a real PG instance.
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import beauty_field_authoring as ba  # noqa: E402
from services.beauty_field_authoring import (  # noqa: E402
    ALLOWED_SKIN_CONCERNS,
    SOURCE_MERCHANT_AUTHORED,
    SOURCE_MERCHANT_PAYLOAD,
    SUBCATEGORY_SCHEMAS,
    WRITE_OUTCOME_PRODUCT_NOT_FOUND,
    WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED,
    WRITE_OUTCOME_UNCHANGED,
    WRITE_OUTCOME_WRITTEN,
    field_names_for_subcategory,
    subcategory_for_path,
    write_merchant_authored_beauty_fields,
)


# ---------- schema invariants ----------

def test_schemas_cover_expected_subcategories():
    """Pin the v2.1 subcategory set so a future refactor surfaces here
    if someone drops a category by accident."""
    kinds = {s["subcategory_kind"] for s in SUBCATEGORY_SCHEMAS.values()}
    assert "skincare" in kinds
    assert "haircare" in kinds
    assert "bath" in kinds
    assert "body" in kinds
    assert "makeup" in kinds
    assert "tools" in kinds
    # Fragrance + accessories deferred to v2.2 — should NOT be in v2.1.
    assert "fragrance" not in kinds
    assert "accessories" not in kinds


def test_subcategory_for_path_matches_prefix():
    assert subcategory_for_path("beauty/skincare/treat/serum")["subcategory_kind"] == "skincare"
    assert subcategory_for_path("beauty/tools/brush")["subcategory_kind"] == "tools"
    assert subcategory_for_path("beauty/makeup/face/foundation")["subcategory_kind"] == "makeup"
    # Out-of-scope subcategories → None (excluded from queue).
    assert subcategory_for_path("beauty/fragrance/perfume") is None
    assert subcategory_for_path("beauty/accessories/clip") is None
    # Bad input → None, not a crash.
    assert subcategory_for_path(None) is None
    assert subcategory_for_path("") is None
    assert subcategory_for_path("fashion/apparel/tops") is None


def test_skincare_field_set():
    fields = field_names_for_subcategory("skincare")
    assert set(fields) == {"raw_inci", "how_to_use_text", "skin_concerns"}


def test_tools_field_set():
    """The v2.1 fix: brushes get tool_material/use_with/care_instructions,
    NOT raw_inci/skin_concerns. Pin so the schema can't silently
    regress to asking for ingredients on a brush."""
    fields = field_names_for_subcategory("tools")
    assert set(fields) == {"tool_material", "use_with", "care_instructions"}
    assert "raw_inci" not in fields
    assert "skin_concerns" not in fields


def test_makeup_field_set():
    """Makeup uses ingredients + how-to-use but NOT skin_concerns —
    a lipstick doesn't 'target oily skin'."""
    fields = field_names_for_subcategory("makeup")
    assert set(fields) == {"raw_inci", "how_to_use_text"}
    assert "skin_concerns" not in fields


def test_source_enums_stable():
    assert SOURCE_MERCHANT_AUTHORED == "merchant_authored"
    assert SOURCE_MERCHANT_PAYLOAD == "merchant_payload"


# ---------- database mock plumbing ----------

class _FakeDatabase:
    def __init__(self, fetch_one_responses=None, fetch_all_responses=None):
        self._fetch_one = list(fetch_one_responses or [])
        self._fetch_all = list(fetch_all_responses or [])
        self.fetched_one: List[Dict[str, Any]] = []
        self.fetched_all: List[Dict[str, Any]] = []
        self.executed: List[Dict[str, Any]] = []
        self.transaction_count = 0

    async def fetch_one(self, sql, params=None):
        self.fetched_one.append({"sql": sql, "params": dict(params or {})})
        if not self._fetch_one:
            return None
        return self._fetch_one.pop(0)

    async def fetch_all(self, sql, params=None):
        self.fetched_all.append({"sql": sql, "params": dict(params or {})})
        if not self._fetch_all:
            return []
        return self._fetch_all.pop(0)

    async def execute(self, sql, params=None):
        self.executed.append({"sql": sql, "params": dict(params or {})})

    def transaction(self):
        outer = self

        @asynccontextmanager
        async def _tx():
            outer.transaction_count += 1
            yield

        return _tx()


def _install_db(monkeypatch, fetch_one=None, fetch_all=None):
    fake = _FakeDatabase(fetch_one, fetch_all)
    monkeypatch.setattr(ba, "database", fake)
    return fake


# ---------- skincare-shape writes (raw_inci / how_to_use / concerns) ----------

@pytest.mark.asyncio
async def test_writes_all_three_skincare_fields(monkeypatch):
    db = _install_db(
        monkeypatch,
        fetch_one=[
            {"content_key": "ck_abc", "category_path": "beauty/skincare/treat/serum"},
            None,  # sku_a — no existing row
            None,  # sku_b
        ],
        fetch_all=[
            [{"sku_key": "sku_a"}, {"sku_key": "sku_b"}],
        ],
    )
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        raw_inci="Water, Glycerin, Niacinamide",
        how_to_use_text="Apply morning + evening",
        skin_concerns=["oily", "acne-prone"],
    )
    assert out == {
        "raw_inci": WRITE_OUTCOME_WRITTEN,
        "how_to_use_text": WRITE_OUTCOME_WRITTEN,
        "skin_concerns": WRITE_OUTCOME_WRITTEN,
    }
    assert db.transaction_count == 1


@pytest.mark.asyncio
async def test_payload_owns_guards_raw_inci(monkeypatch):
    db = _install_db(
        monkeypatch,
        fetch_one=[
            {"content_key": "ck", "category_path": "beauty/skincare/serum"},
            {"source_system": "merchant_payload"},
            {"source_system": "merchant_payload"},
        ],
        fetch_all=[
            [{"sku_key": "sku_a"}, {"sku_key": "sku_b"}],
        ],
    )
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        raw_inci="blocked",
    )
    assert out == {"raw_inci": WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED}


# ---------- tools-shape writes (the v2.1 case) ----------

@pytest.mark.asyncio
async def test_writes_tools_fields_to_profile_payload(monkeypatch):
    db = _install_db(
        monkeypatch,
        fetch_one=[{"content_key": "ck", "category_path": "beauty/tools/brush"}],
    )
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        tool_material="Synthetic fibers",
        use_with="Eyeshadow",
        care_instructions="Wash weekly with mild soap and water; dry flat.",
    )
    assert out == {
        "tool_material": WRITE_OUTCOME_WRITTEN,
        "use_with": WRITE_OUTCOME_WRITTEN,
        "care_instructions": WRITE_OUTCOME_WRITTEN,
    }
    # All three should write into beauty_product_profiles.profile_payload
    # via jsonb_set / INSERT ... ON CONFLICT.
    payload_writes = [
        e for e in db.executed
        if "profile_payload" in e["sql"] and "beauty_product_profiles" in e["sql"]
    ]
    assert len(payload_writes) == 3
    field_names_written = sorted(e["params"]["field"] for e in payload_writes)
    assert field_names_written == ["care_instructions", "tool_material", "use_with"]


@pytest.mark.asyncio
async def test_tools_fields_dont_touch_skincare_tables(monkeypatch):
    """tool_material must NOT write into beauty_sku_ingredients or
    beauty_usage_guides. v2.0 had no opinion on subcategory dispatch;
    v2.1 enforces it via the schema table."""
    db = _install_db(
        monkeypatch,
        fetch_one=[{"content_key": "ck", "category_path": "beauty/tools/brush"}],
    )
    await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        tool_material="Horse hair",
    )
    skincare_writes = [
        e for e in db.executed
        if "beauty_sku_ingredients" in e["sql"]
        or "beauty_usage_guides" in e["sql"]
        or "concerns_json" in e["sql"]
    ]
    assert skincare_writes == []


@pytest.mark.asyncio
async def test_unknown_fields_silently_ignored(monkeypatch):
    """Posting a field the schema doesn't know about must not crash and
    must not write anything. Keeps forward-compat safe when the client
    sends a stale field name."""
    _install_db(monkeypatch, fetch_one=[{"content_key": "ck", "category_path": "beauty/skincare/"}])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        future_field_we_havent_built="ignored",  # type: ignore[arg-type]
    )
    assert out == {}  # nothing reported on


@pytest.mark.asyncio
async def test_whitespace_tool_material_unchanged(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck", "category_path": "beauty/tools/"}])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        tool_material="   \n  ",
    )
    assert out == {"tool_material": WRITE_OUTCOME_UNCHANGED}
    payload_writes = [
        e for e in db.executed
        if "profile_payload" in e["sql"]
    ]
    assert payload_writes == []


# ---------- skin_concerns validation ----------

@pytest.mark.asyncio
async def test_skin_concerns_filtered_and_sorted(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck", "category_path": "beauty/skincare/"}])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        skin_concerns=["oily", "not-a-real-concern", "acne-prone"],
    )
    assert out == {"skin_concerns": WRITE_OUTCOME_WRITTEN}
    profile_writes = [e for e in db.executed if "concerns_json" in e["sql"]]
    persisted = json.loads(profile_writes[0]["params"]["concerns"])
    assert persisted == ["acne-prone", "oily"]


@pytest.mark.asyncio
async def test_skin_concerns_all_invalid_unchanged(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck", "category_path": "beauty/skincare/"}])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        skin_concerns=["nonsense"],
    )
    assert out == {"skin_concerns": WRITE_OUTCOME_UNCHANGED}


# ---------- product not found ----------

@pytest.mark.asyncio
async def test_product_not_found_short_circuits(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[None])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="missing",
        raw_inci="X",
        tool_material="Y",
    )
    assert out == {
        "raw_inci": WRITE_OUTCOME_PRODUCT_NOT_FOUND,
        "tool_material": WRITE_OUTCOME_PRODUCT_NOT_FOUND,
    }
    assert db.executed == []


@pytest.mark.asyncio
async def test_none_inputs_dont_appear_in_output(monkeypatch):
    _install_db(monkeypatch, fetch_one=[{"content_key": "ck", "category_path": "beauty/skincare/"}])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        how_to_use_text="apply morning",
    )
    assert out == {"how_to_use_text": WRITE_OUTCOME_WRITTEN}
    assert "raw_inci" not in out
    assert "tool_material" not in out


# ---------- product-row lock ----------

@pytest.mark.asyncio
async def test_product_lookup_uses_for_update(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck", "category_path": "beauty/skincare/"}])
    await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        how_to_use_text="apply",
    )
    assert "FOR UPDATE" in db.fetched_one[0]["sql"]
