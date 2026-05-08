"""Tests for scripts/run_pdp_label_agent.py (Phase O-3b worker).

The runner orchestrates fetch → classify → merge → write. We mock
the DB layer + classify_pdp so the test exercises the actual
decision logic (skip below confidence, preserve merchant data,
drop_reason histogram, no-DB-write in dry-run) without hitting
prod or making real Gemini calls.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.run_pdp_label_agent as runner  # noqa: E402


def _row(
    *,
    product_key: str = "prod::demo::a",
    demographic: Any = None,
    category_path: Any = None,
    use_case_tags: Any = None,
    lifestyle_tags: Any = None,
    pdp_lifecycle_stage: Any = None,
    **extras: Any,
) -> Dict[str, Any]:
    return {
        "product_key": product_key,
        "merchant_id": extras.get("merchant_id", "external_seed"),
        "platform": extras.get("platform", "external_seed"),
        "source_system": extras.get("source_system", "shopify"),
        "title": extras.get("title", "Sample Product"),
        "description": extras.get("description", "desc"),
        "brand": extras.get("brand", "Brand"),
        "product_type": extras.get("product_type", "Cream"),
        "category_path": category_path,
        "image_url": extras.get("image_url", None),
        "tags": extras.get("tags", []),
        "demographic": demographic,
        "use_case_tags": use_case_tags,
        "lifestyle_tags": lifestyle_tags,
        "pdp_scope": extras.get("pdp_scope", "merchant_owned"),
        "pdp_lifecycle_stage": pdp_lifecycle_stage,
    }


@pytest.mark.asyncio
async def test_process_one_skips_when_confidence_below_threshold(monkeypatch):
    async def fake_classify(row, **_kwargs):
        return {
            "demographic": "women",
            "use_case_tags": ["daily"],
            "lifestyle_tags": [],
            "category_path": None,
            "confidence": 0.4,
            "drop_reason": None,
            "model": "gemini-2.5-flash",
        }

    monkeypatch.setattr(runner, "classify_pdp", fake_classify)

    result = await runner._process_one(
        _row(),
        apply=True,
        no_gemini=False,
        min_confidence=0.5,
        api_key="fake",
    )
    assert result["applied"] is False
    assert result["skip_reason"].startswith("confidence_below_threshold_")


@pytest.mark.asyncio
async def test_process_one_skips_when_agent_fills_nothing_useful(monkeypatch):
    async def fake_classify(row, **_kwargs):
        # Agent returned high confidence but every field None / empty.
        return {
            "demographic": None,
            "use_case_tags": [],
            "lifestyle_tags": [],
            "category_path": None,
            "confidence": 0.95,
            "drop_reason": None,
            "model": "gemini-2.5-flash",
        }

    monkeypatch.setattr(runner, "classify_pdp", fake_classify)

    result = await runner._process_one(
        _row(),
        apply=True,
        no_gemini=False,
        min_confidence=0.5,
        api_key="fake",
    )
    assert result["applied"] is False
    assert result["skip_reason"] == "agent_filled_nothing"


@pytest.mark.asyncio
async def test_process_one_dry_run_does_not_call_db(monkeypatch):
    async def fake_classify(row, **_kwargs):
        return {
            "demographic": "women",
            "use_case_tags": ["daily"],
            "lifestyle_tags": ["vegan"],
            "category_path": "beauty/x/y",
            "confidence": 0.9,
            "drop_reason": None,
            "model": "gemini-2.5-flash",
        }

    db_calls: List[Any] = []

    async def fake_execute(*args, **kwargs):
        db_calls.append((args, kwargs))

    monkeypatch.setattr(runner, "classify_pdp", fake_classify)
    monkeypatch.setattr(runner.database, "execute", fake_execute)

    result = await runner._process_one(
        _row(),
        apply=False,  # dry-run
        no_gemini=False,
        min_confidence=0.5,
        api_key="fake",
    )
    # Reports what it would have filled
    assert set(result["fields_filled"]) == {"demographic", "use_case_tags", "lifestyle_tags", "category_path"}
    # But did NOT touch the DB
    assert db_calls == []
    assert result["applied"] is False


@pytest.mark.asyncio
async def test_process_one_apply_writes_to_db(monkeypatch):
    async def fake_classify(row, **_kwargs):
        return {
            "demographic": "women",
            "use_case_tags": ["daily"],
            "lifestyle_tags": ["vegan"],
            "category_path": "beauty/skincare/treat/serum",
            "confidence": 0.9,
            "drop_reason": None,
            "model": "gemini-2.5-flash",
        }

    db_calls: List[Dict[str, Any]] = []

    async def fake_execute(sql, params):
        db_calls.append({"sql": str(sql), "params": dict(params)})

    monkeypatch.setattr(runner, "classify_pdp", fake_classify)
    monkeypatch.setattr(runner.database, "execute", fake_execute)

    result = await runner._process_one(
        _row(product_key="prod::write_me"),
        apply=True,
        no_gemini=False,
        min_confidence=0.5,
        api_key="fake",
    )
    assert result["applied"] is True
    assert len(db_calls) == 1
    params = db_calls[0]["params"]
    assert params["product_key"] == "prod::write_me"
    assert params["demographic"] == "women"
    assert params["category_path"] == "beauty/skincare/treat/serum"
    # JSONB lists serialized as JSON strings (CAST AS jsonb in SQL)
    assert "daily" in params["use_case_tags"]
    assert "vegan" in params["lifestyle_tags"]


@pytest.mark.asyncio
async def test_process_one_preserves_merchant_demographic_via_coalesce(monkeypatch):
    """SQL uses COALESCE(demographic, :demographic) so even if the
    runner accidentally passes a value for a field that's already
    set, the COALESCE keeps the merchant value. Validates wiring."""

    async def fake_classify(row, **_kwargs):
        return {
            "demographic": "men",  # agent disagrees with merchant
            "use_case_tags": [],
            "lifestyle_tags": [],
            "category_path": None,
            "confidence": 0.95,
            "drop_reason": None,
            "model": "gemini-2.5-flash",
        }

    db_calls: List[Dict[str, Any]] = []

    async def fake_execute(sql, params):
        db_calls.append({"sql": str(sql), "params": dict(params)})

    monkeypatch.setattr(runner, "classify_pdp", fake_classify)
    monkeypatch.setattr(runner.database, "execute", fake_execute)

    # Row already has demographic='women' — agent shouldn't overwrite
    row_with_demo = _row(demographic="women", category_path=None)
    result = await runner._process_one(
        row_with_demo,
        apply=True,
        no_gemini=False,
        min_confidence=0.5,
        api_key="fake",
    )
    # merge_classification_into_row preserves merchant demographic
    # → no field gets filled → applied=False
    assert result["applied"] is False
    assert result["skip_reason"] == "agent_filled_nothing"
    assert db_calls == []


@pytest.mark.asyncio
async def test_process_one_propagates_drop_reason(monkeypatch):
    async def fake_classify(row, **_kwargs):
        return {
            "demographic": None,
            "use_case_tags": [],
            "lifestyle_tags": [],
            "category_path": None,
            "confidence": 0.0,
            "drop_reason": "gemini_no_text_parts",
            "model": "gemini-2.5-flash",
        }

    monkeypatch.setattr(runner, "classify_pdp", fake_classify)

    result = await runner._process_one(
        _row(),
        apply=True,
        no_gemini=False,
        min_confidence=0.5,
        api_key="fake",
    )
    assert result["drop_reason"] == "gemini_no_text_parts"
    assert result["applied"] is False


@pytest.mark.asyncio
async def test_process_one_no_gemini_flag_skips_classification(monkeypatch):
    """--no-gemini lets us audit candidate counts + needed_fields
    without burning Gemini quota."""
    called = []

    async def fake_classify(row, **_kwargs):
        called.append(row)
        return {}

    monkeypatch.setattr(runner, "classify_pdp", fake_classify)

    result = await runner._process_one(
        _row(),
        apply=False,
        no_gemini=True,
        min_confidence=0.5,
        api_key=None,
    )
    assert called == []  # never called
    assert result["drop_reason"] == "no_gemini_flag"
    assert "needed_fields" in result
    assert "demographic" in result["needed_fields"]


# ---------------------------------------------------------------------------
# Phase O-4b — lifecycle stage recompute on UPDATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_recomputes_and_writes_new_lifecycle_stage(monkeypatch):
    """Phase O-4b: when LabelAgent fills the missing taxonomy fields
    on a row that's at 'candidate', the merged row qualifies for
    'validated' — the UPDATE must include the new stage. Without
    this, O-5's recall live-stage filter would never surface the
    promoted row."""

    async def fake_classify(row, **_kwargs):
        return {
            "demographic": "women",
            "use_case_tags": ["daily"],
            "lifestyle_tags": ["vegan"],
            "category_path": "beauty/skincare/treat/serum",
            "confidence": 0.9,
            "drop_reason": None,
            "model": "gemini-2.5-flash",
        }

    db_calls: List[Dict[str, Any]] = []

    async def fake_execute(sql, params):
        db_calls.append({"sql": str(sql), "params": dict(params)})

    monkeypatch.setattr(runner, "classify_pdp", fake_classify)
    monkeypatch.setattr(runner.database, "execute", fake_execute)

    # Row was written by Path A as 'candidate' (title + image + long
    # description, no category_path / taxonomy yet).
    row = _row(
        product_key="prod::promote_me",
        title="Vitamin C Serum",
        description="A long enough product description to clear the candidate gate by a comfortable margin.",
        image_url="https://example.com/serum.jpg",
        pdp_lifecycle_stage="candidate",
    )

    result = await runner._process_one(
        row, apply=True, no_gemini=False, min_confidence=0.5, api_key="fake",
    )

    assert result["applied"] is True
    assert result["lifecycle_stage_before"] == "candidate"
    # After the agent fills demographic + tags + category_path, the
    # merged row qualifies for validated (merchant_owned scope, so
    # never published from this path).
    assert result["lifecycle_stage_after"] == "validated"
    assert result["lifecycle_promoted"] is True
    # The UPDATE must carry the new stage so the column tracks reality.
    assert len(db_calls) == 1
    assert db_calls[0]["params"]["new_stage"] == "validated"


@pytest.mark.asyncio
async def test_process_one_no_promotion_when_stage_unchanged(monkeypatch):
    """If the agent fills only ONE of the missing fields and the row
    still doesn't qualify for the next stage, the UPDATE still writes
    the (unchanged) stage but lifecycle_promoted=False so the report
    summary is honest."""

    async def fake_classify(row, **_kwargs):
        # Agent only fills demographic; category_path stays NULL,
        # so the validated gate (which requires category_path) doesn't
        # fire.
        return {
            "demographic": "women",
            "use_case_tags": [],
            "lifestyle_tags": [],
            "category_path": None,
            "confidence": 0.9,
            "drop_reason": None,
            "model": "gemini-2.5-flash",
        }

    db_calls: List[Dict[str, Any]] = []

    async def fake_execute(sql, params):
        db_calls.append({"sql": str(sql), "params": dict(params)})

    monkeypatch.setattr(runner, "classify_pdp", fake_classify)
    monkeypatch.setattr(runner.database, "execute", fake_execute)

    row = _row(
        product_key="prod::stays_candidate",
        title="Mystery Product",
        description="A long enough description that the candidate gate is satisfied for this row.",
        image_url="https://example.com/x.jpg",
        pdp_lifecycle_stage="candidate",
    )

    result = await runner._process_one(
        row, apply=True, no_gemini=False, min_confidence=0.5, api_key="fake",
    )

    assert result["applied"] is True
    assert result["lifecycle_stage_before"] == "candidate"
    assert result["lifecycle_stage_after"] == "candidate"
    assert result["lifecycle_promoted"] is False
    assert db_calls[0]["params"]["new_stage"] == "candidate"


@pytest.mark.asyncio
async def test_process_one_promotes_null_stage_row(monkeypatch):
    """Pre-O-6b-backfill rows have pdp_lifecycle_stage=NULL. When the
    LabelAgent UPDATE fires on one, it sets the stage from scratch.
    Reporting shows 'null->validated' so the operator can see the
    backfill side-effect of running LabelAgent on legacy rows."""

    async def fake_classify(row, **_kwargs):
        return {
            "demographic": "women",
            "use_case_tags": ["daily"],
            "lifestyle_tags": [],
            "category_path": "beauty/skincare/treat/serum",
            "confidence": 0.9,
            "drop_reason": None,
            "model": "gemini-2.5-flash",
        }

    db_calls: List[Dict[str, Any]] = []

    async def fake_execute(sql, params):
        db_calls.append({"sql": str(sql), "params": dict(params)})

    monkeypatch.setattr(runner, "classify_pdp", fake_classify)
    monkeypatch.setattr(runner.database, "execute", fake_execute)

    row = _row(
        product_key="prod::legacy_null",
        title="Legacy Serum",
        description="A long enough product description to satisfy the candidate length gate for this row.",
        image_url="https://example.com/legacy.jpg",
        pdp_lifecycle_stage=None,  # legacy row
    )

    result = await runner._process_one(
        row, apply=True, no_gemini=False, min_confidence=0.5, api_key="fake",
    )

    assert result["lifecycle_stage_before"] is None
    assert result["lifecycle_stage_after"] == "validated"
    assert result["lifecycle_promoted"] is True
    assert db_calls[0]["params"]["new_stage"] == "validated"


def test_update_sql_writes_lifecycle_stage():
    """Pin the UPDATE statement so a future refactor can't accidentally
    drop the lifecycle write — that was the entire point of O-4b."""
    assert "pdp_lifecycle_stage = :new_stage" in runner.UPDATE_SQL


def test_scope_queries_select_image_url_and_lifecycle_stage():
    """compute_lifecycle_stage needs image_url (candidate gate) and
    pdp_lifecycle_stage (for the before/after delta in the report).
    Pin the SELECT shape so a future column refactor doesn't silently
    break the recompute."""
    for scope_name, sql in runner.SCOPE_QUERIES.items():
        assert "image_url" in sql, f"{scope_name} SELECT missing image_url"
        assert "pdp_lifecycle_stage" in sql, f"{scope_name} SELECT missing pdp_lifecycle_stage"
