"""Tests for services.fashion_field_authoring.

Mocks the global `database` object since the unit under test is the SQL
shape, the source-precedence guard, and the transaction lifecycle —
not the DB driver. agent_pdp_view refresh is also stubbed out (covered
in test_agent_pdp_view_assembler_fashion_coalesce / dedicated
integration tests).
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
    the test queued for that call ordinal. transaction() returns an
    async no-op context manager so the unit-under-test's `async with`
    block runs end-to-end without a real PG connection."""

    def __init__(self, fetch_responses: List[Optional[Dict[str, Any]]]):
        self._fetch_queue = list(fetch_responses)
        self.fetched: List[Dict[str, Any]] = []
        self.executed: List[Dict[str, Any]] = []
        self.transaction_count = 0

    async def fetch_one(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        self.fetched.append({"sql": sql, "params": dict(params or {})})
        if not self._fetch_queue:
            return None
        return self._fetch_queue.pop(0)

    async def execute(self, sql: str, params: Optional[Dict[str, Any]] = None) -> None:
        self.executed.append({"sql": sql, "params": dict(params or {})})

    def transaction(self):
        outer = self

        @asynccontextmanager
        async def _tx():
            outer.transaction_count += 1
            yield

        return _tx()


def _install_db(monkeypatch, fetch_responses):
    """Install the fake DB and stub the view-refresh hook so tests don't
    need to mock the assembler module. Refresh is exercised in the
    integration tests under test_agent_pdp_view_assembler_*."""
    fake = _FakeDatabase(fetch_responses)
    monkeypatch.setattr(fa, "database", fake)
    monkeypatch.setattr(
        fa, "_refresh_view_for_content_key", AsyncMock(return_value=None),
    )
    return fake


# ---------- happy path: write across all three fields ----------

@pytest.mark.asyncio
async def test_writes_all_three_when_no_prior_source(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": None, "content_key": "ck_abc"},  # material
        {"src": None, "content_key": "ck_abc"},  # care
        {"src": None, "content_key": "ck_abc"},  # size_guide
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
    assert db.transaction_count == 1  # one transaction per call
    for e in db.executed:
        assert e["params"]["src"] == "merchant_authored"
        assert e["params"]["conf"] == 1.0
        assert e["params"]["pk"] == "prod::m1::shopify::p1"
    # view refresh called once (one content_key)
    fa._refresh_view_for_content_key.assert_awaited_once_with("ck_abc")  # type: ignore[attr-defined]


# ---------- race-safety: SELECT uses FOR UPDATE ----------

@pytest.mark.asyncio
async def test_select_locks_row_for_update(monkeypatch):
    db = _install_db(monkeypatch, [{"src": None, "content_key": "ck_xyz"}])
    await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="cotton",
    )
    # The SELECT must include FOR UPDATE to serialize against concurrent
    # merchant_payload writes. Codex flagged the race as a ship-blocker;
    # this pins the fix so a future refactor can't drop the clause.
    select_sql = db.fetched[0]["sql"]
    assert "FOR UPDATE" in select_sql
    assert db.transaction_count == 1


# ---------- payload-owned guard ----------

@pytest.mark.asyncio
async def test_skips_field_when_source_is_merchant_payload(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": "merchant_payload", "content_key": "ck"},
    ])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="other_material",
    )
    assert out == {"material": WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED}
    assert db.executed == []  # nothing written
    # No write means no view refresh required.
    fa._refresh_view_for_content_key.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_mixed_outcomes_when_some_payload_owned(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": "merchant_payload", "content_key": "ck"},   # material — payload-owned
        {"src": "llm_extraction_v1", "content_key": "ck"},  # care — overwritable
        {"src": None, "content_key": "ck"},                  # size_guide — empty
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
    # Two writes share one content_key — view refreshed once, not twice.
    fa._refresh_view_for_content_key.assert_awaited_once_with("ck")  # type: ignore[attr-defined]


# ---------- overwrites LLM values ----------

@pytest.mark.asyncio
async def test_overwrites_llm_extraction(monkeypatch):
    db = _install_db(monkeypatch, [
        {"src": "llm_extraction_v1", "content_key": "ck"},
    ])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="genuine merchant input",
    )
    assert out == {"material": WRITE_OUTCOME_WRITTEN}
    fa._refresh_view_for_content_key.assert_awaited_once_with("ck")  # type: ignore[attr-defined]


# ---------- size_guide handling ----------

@pytest.mark.asyncio
async def test_size_guide_string_wrapped_in_raw_envelope(monkeypatch):
    db = _install_db(monkeypatch, [{"src": None, "content_key": "ck"}])
    await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        size_guide="See chart below",
    )
    assert len(db.executed) == 1
    persisted = json.loads(db.executed[0]["params"]["v"])
    assert persisted == {"raw": "See chart below"}


@pytest.mark.asyncio
async def test_size_guide_dict_passed_through_verbatim(monkeypatch):
    db = _install_db(monkeypatch, [{"src": None, "content_key": "ck"}])
    structured = {"columns": ["S", "M", "L"], "rows": [{"chest": "32-34"}]}
    await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        size_guide=structured,
    )
    persisted = json.loads(db.executed[0]["params"]["v"])
    assert persisted == structured


@pytest.mark.asyncio
async def test_size_guide_whitespace_string_treated_as_unchanged(monkeypatch):
    """Codex review flagged: empty/whitespace size_guide was writing
    {"raw": ""} with confidence 1.0. Now it's a no-op."""
    db = _install_db(monkeypatch, [{"src": None, "content_key": "ck"}])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        size_guide="   \n  ",
    )
    assert out == {"size_guide": WRITE_OUTCOME_UNCHANGED}
    assert db.executed == []
    fa._refresh_view_for_content_key.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_size_guide_empty_dict_treated_as_unchanged(monkeypatch):
    db = _install_db(monkeypatch, [{"src": None, "content_key": "ck"}])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        size_guide={},
    )
    assert out == {"size_guide": WRITE_OUTCOME_UNCHANGED}
    assert db.executed == []


@pytest.mark.asyncio
async def test_size_guide_wrong_type_rejected(monkeypatch):
    """Lists / numbers / bools are not valid size_guide values; defending
    in depth because the column gates merchant-facing prose."""
    db = _install_db(monkeypatch, [{"src": None, "content_key": "ck"}])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        size_guide=[1, 2, 3],  # type: ignore[arg-type]  — deliberate bad input
    )
    assert out == {"size_guide": WRITE_OUTCOME_UNCHANGED}
    assert db.executed == []


# ---------- input handling ----------

@pytest.mark.asyncio
async def test_none_inputs_dont_appear_in_output(monkeypatch):
    _install_db(monkeypatch, [{"src": None, "content_key": "ck"}])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="cotton",
    )
    assert out == {"material": WRITE_OUTCOME_WRITTEN}
    assert "care" not in out
    assert "size_guide" not in out


@pytest.mark.asyncio
async def test_empty_string_is_unchanged_not_clear(monkeypatch):
    db = _install_db(monkeypatch, [{"src": "llm_extraction_v1", "content_key": "ck"}])
    out = await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="   ",  # whitespace-only
    )
    assert out == {"material": WRITE_OUTCOME_UNCHANGED}
    assert db.executed == []


@pytest.mark.asyncio
async def test_whitespace_stripped_from_value(monkeypatch):
    db = _install_db(monkeypatch, [{"src": None, "content_key": "ck"}])
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
    fa._refresh_view_for_content_key.assert_not_awaited()  # type: ignore[attr-defined]


# ---------- product_key shape ----------

@pytest.mark.asyncio
async def test_product_key_construction(monkeypatch):
    db = _install_db(monkeypatch, [{"src": None, "content_key": "ck"}])
    await write_merchant_authored_fashion_fields(
        merchant_id="merch_xyz", platform="wix", source_product_id="abc123",
        material="silk",
    )
    assert db.fetched[0]["params"]["pk"] == "prod::merch_xyz::wix::abc123"
    assert db.executed[0]["params"]["pk"] == "prod::merch_xyz::wix::abc123"


# ---------- view refresh trigger ----------

@pytest.mark.asyncio
async def test_view_refresh_skipped_when_no_writes(monkeypatch):
    """All payload-owned or all unchanged → no refresh."""
    _install_db(monkeypatch, [
        {"src": "merchant_payload", "content_key": "ck"},
    ])
    await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="cotton",
    )
    fa._refresh_view_for_content_key.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_view_refresh_skipped_when_content_key_is_null(monkeypatch):
    """If catalog_products row has no content_key yet (rare — pre-Stage-1
    onboarding), skip the refresh; nothing to refresh."""
    _install_db(monkeypatch, [
        {"src": None, "content_key": None},
    ])
    await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="cotton",
    )
    fa._refresh_view_for_content_key.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_view_refresh_deduped_across_fields(monkeypatch):
    """Three fields on the same product share one content_key; refresh
    fires once, not three times."""
    _install_db(monkeypatch, [
        {"src": None, "content_key": "ck"},
        {"src": None, "content_key": "ck"},
        {"src": None, "content_key": "ck"},
    ])
    await write_merchant_authored_fashion_fields(
        merchant_id="m1", platform="shopify", source_product_id="p1",
        material="cotton",
        care="hand wash",
        size_guide="see chart",
    )
    fa._refresh_view_for_content_key.assert_awaited_once_with("ck")  # type: ignore[attr-defined]


# ---------- internal validation ----------

@pytest.mark.asyncio
async def test_unknown_field_raises(monkeypatch):
    _install_db(monkeypatch, [])
    with pytest.raises(ValueError, match="unknown fashion field"):
        await fa._write_one_field_in_tx(product_key="prod::x", field="not_a_field", value="x")
