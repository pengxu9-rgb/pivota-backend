"""Tests for services.beauty_field_authoring.

Mocks the global `database` so tests pin the SQL shape + source-precedence
guard + transaction lifecycle without a real PG instance.
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
    WRITE_OUTCOME_PRODUCT_NOT_FOUND,
    WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED,
    WRITE_OUTCOME_UNCHANGED,
    WRITE_OUTCOME_WRITTEN,
    write_merchant_authored_beauty_fields,
)


def test_skin_concerns_enum_stable():
    """If the enum changes the UI multi-select needs a coordinated
    update. Pin the strings so a future refactor surfaces here."""
    assert "oily" in ALLOWED_SKIN_CONCERNS
    assert "dry" in ALLOWED_SKIN_CONCERNS
    assert "acne-prone" in ALLOWED_SKIN_CONCERNS
    assert len(ALLOWED_SKIN_CONCERNS) >= 8


def test_source_enums_stable():
    assert SOURCE_MERCHANT_AUTHORED == "merchant_authored"
    assert SOURCE_MERCHANT_PAYLOAD == "merchant_payload"


class _FakeDatabase:
    """Programmable fetch_one queue + records all execute calls."""

    def __init__(self, fetch_one_responses: List[Optional[Dict[str, Any]]],
                 fetch_all_responses: List[List[Dict[str, Any]]]):
        self._fetch_one = list(fetch_one_responses)
        self._fetch_all = list(fetch_all_responses)
        self.fetched_one: List[Dict[str, Any]] = []
        self.fetched_all: List[Dict[str, Any]] = []
        self.executed: List[Dict[str, Any]] = []
        self.transaction_count = 0

    async def fetch_one(self, sql: str, params: Optional[Dict[str, Any]] = None):
        self.fetched_one.append({"sql": sql, "params": dict(params or {})})
        if not self._fetch_one:
            return None
        return self._fetch_one.pop(0)

    async def fetch_all(self, sql: str, params: Optional[Dict[str, Any]] = None):
        self.fetched_all.append({"sql": sql, "params": dict(params or {})})
        if not self._fetch_all:
            return []
        return self._fetch_all.pop(0)

    async def execute(self, sql: str, params: Optional[Dict[str, Any]] = None):
        self.executed.append({"sql": sql, "params": dict(params or {})})

    def transaction(self):
        outer = self

        @asynccontextmanager
        async def _tx():
            outer.transaction_count += 1
            yield

        return _tx()


def _install_db(monkeypatch, fetch_one=None, fetch_all=None):
    fake = _FakeDatabase(fetch_one or [], fetch_all or [])
    monkeypatch.setattr(ba, "database", fake)
    return fake


# ---------- happy path: write all three fields ----------

@pytest.mark.asyncio
async def test_writes_all_three_fields_when_product_exists(monkeypatch):
    db = _install_db(
        monkeypatch,
        fetch_one=[
            {"content_key": "ck_abc"},        # _ensure_product_exists
            # raw_inci path: per-SKU lookups (one row per SKU)
            None,                              # sku_a — existing row absent
            None,                              # sku_b — existing row absent
        ],
        fetch_all=[
            # _list_sku_keys returns two SKUs
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
    # 2 ingredient UPSERTs + 1 usage guide UPSERT + 1 profile UPSERT
    assert len(db.executed) == 4
    # The ingredient writes carry merchant_authored source.
    ingr_writes = [e for e in db.executed if "beauty_sku_ingredients" in e["sql"]]
    assert len(ingr_writes) == 2
    for e in ingr_writes:
        assert e["params"]["src"] == "merchant_authored"


# ---------- payload-owns guard ----------

@pytest.mark.asyncio
async def test_skips_raw_inci_when_all_skus_payload_owned(monkeypatch):
    db = _install_db(
        monkeypatch,
        fetch_one=[
            {"content_key": "ck"},
            {"source_system": "merchant_payload"},  # sku_a — payload-owned
            {"source_system": "merchant_payload"},  # sku_b — payload-owned
        ],
        fetch_all=[
            [{"sku_key": "sku_a"}, {"sku_key": "sku_b"}],
        ],
    )
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        raw_inci="should be blocked",
    )
    assert out == {"raw_inci": WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED}
    # No execute calls for ingredients — all SKUs skipped.
    assert not any("beauty_sku_ingredients" in e["sql"] and "INSERT" in e["sql"] for e in db.executed)


@pytest.mark.asyncio
async def test_writes_inci_to_unguarded_skus_when_some_payload_owned(monkeypatch):
    """Partial — one SKU payload-locked, one open: write happens to the
    open SKU; outcome is `written` overall because at least one wrote."""
    db = _install_db(
        monkeypatch,
        fetch_one=[
            {"content_key": "ck"},
            {"source_system": "merchant_payload"},  # sku_a — locked
            None,                                    # sku_b — fresh
        ],
        fetch_all=[
            [{"sku_key": "sku_a"}, {"sku_key": "sku_b"}],
        ],
    )
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        raw_inci="Aqua, Glycerin",
    )
    assert out == {"raw_inci": WRITE_OUTCOME_WRITTEN}
    ingr_writes = [e for e in db.executed if "beauty_sku_ingredients" in e["sql"] and "INSERT" in e["sql"]]
    assert len(ingr_writes) == 1


# ---------- raw_inci with no SKUs ingested ----------

@pytest.mark.asyncio
async def test_inci_unchanged_when_no_skus_exist(monkeypatch):
    db = _install_db(
        monkeypatch,
        fetch_one=[{"content_key": "ck"}],
        fetch_all=[[]],  # no SKUs
    )
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        raw_inci="Aqua",
    )
    assert out == {"raw_inci": WRITE_OUTCOME_UNCHANGED}
    # No ingredient writes.
    assert not any("beauty_sku_ingredients" in e["sql"] and "INSERT" in e["sql"] for e in db.executed)


# ---------- skin_concerns enum validation ----------

@pytest.mark.asyncio
async def test_skin_concerns_invalid_values_filtered_out(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck"}])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        skin_concerns=["oily", "not-a-real-concern", "acne-prone"],
    )
    assert out == {"skin_concerns": WRITE_OUTCOME_WRITTEN}
    profile_writes = [e for e in db.executed if "beauty_product_profiles" in e["sql"]]
    assert len(profile_writes) == 1
    persisted = json.loads(profile_writes[0]["params"]["concerns"])
    # invalid value dropped, valid ones sorted
    assert persisted == ["acne-prone", "oily"]


@pytest.mark.asyncio
async def test_skin_concerns_all_invalid_treated_as_unchanged(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck"}])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        skin_concerns=["nonsense", "made-up"],
    )
    assert out == {"skin_concerns": WRITE_OUTCOME_UNCHANGED}
    assert not any("beauty_product_profiles" in e["sql"] for e in db.executed)


@pytest.mark.asyncio
async def test_skin_concerns_empty_list_treated_as_unchanged(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck"}])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        skin_concerns=[],
    )
    assert out == {"skin_concerns": WRITE_OUTCOME_UNCHANGED}


@pytest.mark.asyncio
async def test_skin_concerns_dedupes(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck"}])
    await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        skin_concerns=["oily", "oily", "oily"],
    )
    profile_write = next(e for e in db.executed if "beauty_product_profiles" in e["sql"])
    persisted = json.loads(profile_write["params"]["concerns"])
    assert persisted == ["oily"]


# ---------- how_to_use_text normalization ----------

@pytest.mark.asyncio
async def test_how_to_use_whitespace_treated_as_unchanged(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck"}])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        how_to_use_text="   \n  ",
    )
    assert out == {"how_to_use_text": WRITE_OUTCOME_UNCHANGED}
    assert not any("beauty_usage_guides" in e["sql"] for e in db.executed)


@pytest.mark.asyncio
async def test_how_to_use_strips_whitespace(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck"}])
    await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        how_to_use_text="  Apply at night  \n",
    )
    usage_write = next(e for e in db.executed if "beauty_usage_guides" in e["sql"])
    assert usage_write["params"]["txt"] == "Apply at night"


# ---------- product not found ----------

@pytest.mark.asyncio
async def test_product_not_found_short_circuits(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[None])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="missing",
        raw_inci="Aqua",
        how_to_use_text="apply once",
        skin_concerns=["oily"],
    )
    assert out == {
        "raw_inci": WRITE_OUTCOME_PRODUCT_NOT_FOUND,
        "how_to_use_text": WRITE_OUTCOME_PRODUCT_NOT_FOUND,
        "skin_concerns": WRITE_OUTCOME_PRODUCT_NOT_FOUND,
    }
    # No execute calls; we short-circuited inside the transaction.
    assert db.executed == []


# ---------- input handling ----------

@pytest.mark.asyncio
async def test_none_inputs_dont_appear_in_output(monkeypatch):
    """Caller didn't ask about a field → it shouldn't appear in the
    outcomes dict. Mirrors fashion's contract."""
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck"}])
    out = await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        how_to_use_text="apply morning",
        # raw_inci and skin_concerns omitted
    )
    assert out == {"how_to_use_text": WRITE_OUTCOME_WRITTEN}
    assert "raw_inci" not in out
    assert "skin_concerns" not in out


# ---------- product_key shape ----------

@pytest.mark.asyncio
async def test_product_key_construction(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck"}])
    await write_merchant_authored_beauty_fields(
        merchant_id="merch_x", platform="wix", source_product_id="abc",
        how_to_use_text="apply",
    )
    assert db.fetched_one[0]["params"]["pk"] == "prod::merch_x::wix::abc"


# ---------- guide_id determinism ----------

def test_usage_guide_id_deterministic():
    """Same product_key → same guide_id (so re-authoring lands in the
    same row Aurora ingest would update). Different products → different
    guide_ids."""
    assert ba._usage_guide_id("prod::m1::shopify::p1") == ba._usage_guide_id("prod::m1::shopify::p1")
    assert ba._usage_guide_id("prod::m1::shopify::p1") != ba._usage_guide_id("prod::m1::shopify::p2")


# ---------- transaction wraps all writes ----------

@pytest.mark.asyncio
async def test_writes_happen_inside_transaction(monkeypatch):
    db = _install_db(
        monkeypatch,
        fetch_one=[{"content_key": "ck"}],
    )
    await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        skin_concerns=["dry"],
    )
    assert db.transaction_count == 1


# ---------- product-row lock pinned ----------

@pytest.mark.asyncio
async def test_product_exists_query_uses_for_update(monkeypatch):
    db = _install_db(monkeypatch, fetch_one=[{"content_key": "ck"}])
    await write_merchant_authored_beauty_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        how_to_use_text="apply",
    )
    assert "FOR UPDATE" in db.fetched_one[0]["sql"]
