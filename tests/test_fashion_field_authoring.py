"""Tests for services.fashion_field_authoring.

Mocks the global `database` object since the unit under test is the SQL
shape + the source-precedence guard, not the DB driver.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import fashion_field_authoring as fa  # noqa: E402
from services.fashion_field_authoring import (  # noqa: E402
    EXTRACTION_SOURCE_LLM,
    EXTRACTION_SOURCE_MERCHANT_AUTHORED,
    EXTRACTION_SOURCE_MERCHANT_PAYLOAD,
    WRITE_OUTCOME_PRODUCT_NOT_FOUND,
    WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED,
    WRITE_OUTCOME_UNCHANGED,
    WRITE_OUTCOME_WRITTEN,
    write_merchant_authored_fashion_fields,
)


# ---------- source enum stability ----------

def test_source_enums_stable():
    assert EXTRACTION_SOURCE_MERCHANT_PAYLOAD == "merchant_payload"
    assert EXTRACTION_SOURCE_MERCHANT_AUTHORED == "merchant_authored"
    assert EXTRACTION_SOURCE_LLM == "llm_extraction_v1"


# ---------- helper: programmable database mock ----------

class _FakeDatabase:
    """Records every fetch_one + execute call. fetch_one returns whatever
    the test queued for that call ordinal."""

    def __init__(self, fetch_responses: List[Optional[Dict[str, Any]]]):
        self._fetch_queue = list(fetch_responses)
        self.fetched: List[Dict[str, Any]] = []
        self.executed: List[Dict[str, Any]] = []

    async def fetch_one(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        self.fetched.append({"sql": sql, "params": dict(params or {})})
        if not self._fetch_queue:
            return None
        return self._fetch_queue.pop(0)

    async def execute(self, sql: str, params: Optional[Dict[str, Any]] = None) -> None:
        self.executed.append({"sql": sql, "params": dict(params or {})})


def _install_db(monkeypatch, fetch_responses):
    fake = _FakeDatabase(fetch_responses)
    monkeypatch.setattr(fa, "database", fake)
    return fake


# ---------- happy path: write across all three fields ----------

@pytest.mark.asyncio
async def test_writes_all_three_when_no_prior_source(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": None, "was_null": True},   # material
        {"src": None, "was_null": True},   # care
        {"src": None, "was_null": True},   # size_guide
    ])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="satin",
        care="hand wash cold",
        size_guide="see chart below",
    )
    assert out == {
        "material": WRITE_OUTCOME_WRITTEN,
        "care": WRITE_OUTCOME_WRITTEN,
        "size_guide": WRITE_OUTCOME_WRITTEN,
    }
    assert len(db.executed) == 3
    # Each UPDATE carries source=merchant_authored, confidence=1.0
    for e in db.executed:
        assert e["params"]["src"] == "merchant_authored"
        assert e["params"]["conf"] == 1.0
        assert e["params"]["pk"] == "prod::m1::shopify::p1"


# ---------- payload-owned guard ----------

@pytest.mark.asyncio
async def test_skips_field_when_source_is_merchant_payload(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": "merchant_payload", "was_null": False},
    ])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="other_material",
    )
    assert out == {"material": WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED}
    assert db.executed == []  # nothing written


@pytest.mark.asyncio
async def test_mixed_outcomes_when_some_payload_owned(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": "merchant_payload", "was_null": False},   # material — payload-owned
        {"src": "llm_extraction_v1", "was_null": False},  # care — overwritable
        {"src": None, "was_null": True},                  # size_guide — empty
    ])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="cotton",
        care="hand wash",
        size_guide="see chart",
    )
    assert out == {
        "material": WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED,
        "care": WRITE_OUTCOME_WRITTEN,
        "size_guide": WRITE_OUTCOME_WRITTEN,
    }
    assert len(db.executed) == 2


# ---------- overwrites LLM values ----------

@pytest.mark.asyncio
async def test_overwrites_llm_extraction(monkeypatch):
    _install_db(monkeypatch, [
        {"src": "llm_extraction_v1", "was_null": False},
    ])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="genuine merchant input",
    )
    assert out == {"material": WRITE_OUTCOME_WRITTEN}


# ---------- size_guide JSONB wrapping ----------

@pytest.mark.asyncio
async def test_size_guide_string_wrapped_in_raw_envelope(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": None, "was_null": True},
    ])
    await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        size_guide="See chart below",
    )
    assert len(db.executed) == 1
    persisted = json.loads(db.executed[0]["params"]["v"])
    assert persisted == {"raw": "See chart below"}


@pytest.mark.asyncio
async def test_size_guide_dict_passed_through_verbatim(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": None, "was_null": True},
    ])
    structured = {"columns": ["S", "M", "L"], "rows": [{"chest": "32-34"}]}
    await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        size_guide=structured,
    )
    persisted = json.loads(db.executed[0]["params"]["v"])
    assert persisted == structured


# ---------- input handling ----------

@pytest.mark.asyncio
async def test_none_inputs_dont_appear_in_output(monkeypatch):
    _install_db(monkeypatch, [
        {"src": None, "was_null": True},
    ])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="cotton",
        # care and size_guide both None
    )
    assert out == {"material": WRITE_OUTCOME_WRITTEN}
    assert "care" not in out
    assert "size_guide" not in out


@pytest.mark.asyncio
async def test_empty_string_is_unchanged_not_clear(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": "llm_extraction_v1", "was_null": False},  # material existing
    ])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="   ",  # whitespace-only
    )
    assert out == {"material": WRITE_OUTCOME_UNCHANGED}
    assert db.executed == []  # no UPDATE for empty input


@pytest.mark.asyncio
async def test_whitespace_stripped_from_value(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": None, "was_null": True},
    ])
    await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="  100% cotton  \n",
    )
    assert db.executed[0]["params"]["v"] == "100% cotton"


# ---------- product not found ----------

@pytest.mark.asyncio
async def test_product_not_found_short_circuits(monkeypatch):
    db = _install_db(monkeypatch, [None])  # first fetch returns no row
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="missing",
        material="cotton",
        care="hand wash",
        size_guide="see chart",
    )
    assert out == {
        "material": WRITE_OUTCOME_PRODUCT_NOT_FOUND,
        "care": WRITE_OUTCOME_PRODUCT_NOT_FOUND,
        "size_guide": WRITE_OUTCOME_PRODUCT_NOT_FOUND,
    }
    assert db.executed == []  # nothing written
    assert len(db.fetched) == 1  # short-circuited after first miss


# ---------- product_key shape ----------

@pytest.mark.asyncio
async def test_product_key_construction(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": None, "was_null": True},
    ])
    await write_merchant_authored_fashion_fields(
        merchant_id="merch_xyz", platform="wix", source_product_id="abc123",
        material="silk",
    )
    assert db.fetched[0]["params"]["pk"] == "prod::merch_xyz::wix::abc123"
    assert db.executed[0]["params"]["pk"] == "prod::merch_xyz::wix::abc123"


# ---------- internal validation ----------

@pytest.mark.asyncio
async def test_unknown_field_raises(monkeypatch):
    _install_db(monkeypatch, [])
    with pytest.raises(ValueError, match="unknown fashion field"):
        await fa._write_one_field(product_key="prod::x", field="not_a_field", value="x")
