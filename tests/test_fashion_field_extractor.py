"""Tests for services.fashion_field_extractor v2 (LLM-backed).

Covers: feature-flag gate, category gate, substring grounding, confidence
calibration, transport/parse failure modes. The actual HTTP call is mocked
via monkeypatch so tests stay deterministic and free.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import fashion_field_extractor as fx  # noqa: E402
from services.fashion_field_extractor import (  # noqa: E402
    EXTRACTION_SOURCE_LLM,
    EXTRACTION_SOURCE_REGEX,
    ExtractionResult,
    extract_care,
    extract_material,
    extract_size_guide,
)


# ---------- contract: source enums stable across v1 → v2 ----------

def test_source_enums_stable():
    assert EXTRACTION_SOURCE_REGEX == "regex_extraction_v1"
    assert EXTRACTION_SOURCE_LLM == "llm_extraction_v1"


# ---------- helper: mock Deepseek with a canned response ----------

def _install_llm_mock(monkeypatch, response: Optional[Dict[str, Any]]):
    async def _fake_call(*, field: str, user_message: str, timeout_s: float = 15.0):
        return response
    monkeypatch.setattr(fx, "_call_deepseek_extract", _fake_call)


def _enable_flag(monkeypatch):
    monkeypatch.setenv("FASHION_EXTRACT_ENABLED", "true")


# ---------- gate: feature flag (default OFF) ----------

@pytest.mark.asyncio
async def test_flag_off_returns_noop(monkeypatch):
    monkeypatch.delenv("FASHION_EXTRACT_ENABLED", raising=False)
    _install_llm_mock(monkeypatch, {"value": "100% cotton", "confidence": 0.9})
    r = await extract_material(
        title="Linen Dress",
        description="Material: 100% organic cotton",
        category_path="fashion/dresses",
    )
    assert r.value is None
    assert r.confidence == 0.0
    assert r.source == EXTRACTION_SOURCE_LLM


# ---------- gate: category prefix ----------

@pytest.mark.asyncio
async def test_non_fashion_category_short_circuits(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm_mock(monkeypatch, {"value": "100% cotton", "confidence": 0.9})
    # Even though flag is on AND mock would return a value, beauty category
    # short-circuits before the LLM call.
    r = await extract_material(
        description="Material: 100% organic cotton",
        category_path="beauty/skincare/treat/serum",
    )
    assert r.value is None
    assert r.confidence == 0.0


@pytest.mark.asyncio
async def test_null_category_short_circuits(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm_mock(monkeypatch, {"value": "100% cotton", "confidence": 0.9})
    r = await extract_material(
        description="Material: 100% organic cotton",
        category_path=None,
    )
    assert r.value is None


@pytest.mark.asyncio
async def test_fashion_category_apparel_passes(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm_mock(monkeypatch, {"value": "100% cotton", "confidence": 0.9})
    r = await extract_material(
        description="Material: 100% cotton, premium grade",
        category_path="apparel/tops/tshirts",
    )
    assert r.value == "100% cotton"
    assert r.confidence > 0


# ---------- substring grounding ----------

@pytest.mark.asyncio
async def test_verbatim_match_full_confidence(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm_mock(monkeypatch, {"value": "100% organic cotton", "confidence": 0.8})
    r = await extract_material(
        description="A breezy dress. Material: 100% organic cotton; OEKO-TEX certified.",
        category_path="fashion/dresses",
    )
    assert r.value == "100% organic cotton"
    # 0.8 self-report × 1.0 substring × 1.0 category = 0.8
    assert r.confidence == 0.8


@pytest.mark.asyncio
async def test_absent_value_dropped(monkeypatch):
    _enable_flag(monkeypatch)
    # LLM hallucinates a value that's not in the source text.
    _install_llm_mock(monkeypatch, {"value": "silk taffeta", "confidence": 0.9})
    r = await extract_material(
        description="A breezy dress made for warm days.",
        category_path="fashion/dresses",
    )
    # Substring grounding score = 0.0 → final confidence = 0 → dropped
    assert r.value is None
    assert r.confidence == 0.0


@pytest.mark.asyncio
async def test_partial_match_downgraded(monkeypatch):
    _enable_flag(monkeypatch)
    # Source has "100% cotton blend with extra fabric", LLM hallucinates
    # extra trailing detail "100% cotton blend with elastane".
    # First 20 chars ("100% cotton blend wi") appear in source verbatim,
    # but the full string doesn't.
    _install_llm_mock(monkeypatch, {"value": "100% cotton blend with elastane", "confidence": 0.9})
    r = await extract_material(
        description="Material: 100% cotton blend with extra fabric layers.",
        category_path="fashion/dresses",
    )
    # 0.9 self-report × 0.5 partial × 1.0 category = 0.45
    assert r.value == "100% cotton blend with elastane"
    assert r.confidence == 0.45


# ---------- self-report confidence handling ----------

@pytest.mark.asyncio
async def test_missing_self_report_defaults_to_half(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm_mock(monkeypatch, {"value": "100% cotton", "reason": "stated"})  # no confidence
    r = await extract_material(
        description="Material: 100% cotton, premium grade",
        category_path="fashion/tops",
    )
    # default 0.5 × 1.0 verbatim × 1.0 category = 0.5
    assert r.value == "100% cotton"
    assert r.confidence == 0.5


@pytest.mark.asyncio
async def test_out_of_range_self_report_defaults_to_half(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm_mock(monkeypatch, {"value": "100% cotton", "confidence": 1.5})
    r = await extract_material(
        description="Material: 100% cotton, premium grade",
        category_path="fashion/tops",
    )
    assert r.confidence == 0.5  # treated as invalid → default 0.5


# ---------- LLM declines / failures ----------

@pytest.mark.asyncio
async def test_llm_returns_null_value(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm_mock(monkeypatch, {"value": None, "confidence": 1.0, "reason": "not_stated"})
    r = await extract_material(
        description="A great everyday shirt.",
        category_path="fashion/tops",
    )
    assert r.value is None


@pytest.mark.asyncio
async def test_llm_transport_failure_returns_noop(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm_mock(monkeypatch, None)  # transport/parse failure
    r = await extract_material(
        description="Material: 100% cotton",
        category_path="fashion/tops",
    )
    assert r.value is None
    assert r.confidence == 0.0


@pytest.mark.asyncio
async def test_empty_haystack_short_circuits(monkeypatch):
    _enable_flag(monkeypatch)
    _install_llm_mock(monkeypatch, {"value": "anything", "confidence": 1.0})
    r = await extract_material(
        title=None, description=None, category_path="fashion/dresses",
    )
    assert r.value is None


# ---------- all three field extractors ----------

@pytest.mark.asyncio
async def test_all_three_fields_route_through_same_core(monkeypatch):
    _enable_flag(monkeypatch)
    # Same mock for all three — proves the field-specific extractors all
    # invoke the same core gate + grounding pipeline.
    _install_llm_mock(monkeypatch, {"value": "cotton", "confidence": 0.7})
    desc = "Material: cotton. Care: cotton. Sizing: cotton."
    cat = "fashion/dresses"
    m = await extract_material(description=desc, category_path=cat)
    c = await extract_care(description=desc, category_path=cat)
    s = await extract_size_guide(description=desc, category_path=cat)
    for r in (m, c, s):
        assert r.value == "cotton"
        assert r.source == EXTRACTION_SOURCE_LLM


# ---------- ExtractionResult dataclass invariants ----------

def test_extraction_result_has_required_fields():
    r = ExtractionResult(value="x", confidence=0.5, source=EXTRACTION_SOURCE_LLM)
    assert r.value == "x"
    assert r.confidence == 0.5
    assert r.source == EXTRACTION_SOURCE_LLM


# ---------- batched extractor (one LLM call for all three fields) ----------

from services.fashion_field_extractor import (  # noqa: E402
    batch_extract_fashion_fields,
)


def _install_batch_mock(monkeypatch, response: Optional[Dict[str, Any]]):
    async def _fake_batch(*, user_message: str, timeout_s: float = 20.0):
        return response
    monkeypatch.setattr(fx, "_call_deepseek_batch", _fake_batch)


@pytest.mark.asyncio
async def test_batch_flag_off_returns_three_noops(monkeypatch):
    monkeypatch.delenv("FASHION_EXTRACT_ENABLED", raising=False)
    _install_batch_mock(monkeypatch, {
        "material": {"value": "100% cotton", "confidence": 0.9},
        "care": {"value": "hand wash", "confidence": 0.9},
        "size_guide": {"value": "see chart", "confidence": 0.9},
    })
    out = await batch_extract_fashion_fields(
        description="Material: 100% cotton. Care: hand wash. Sizing: see chart.",
        category_path="fashion/dresses",
    )
    assert set(out.keys()) == {"material", "care", "size_guide"}
    for r in out.values():
        assert r.value is None
        assert r.confidence == 0.0
        assert r.source == EXTRACTION_SOURCE_LLM


@pytest.mark.asyncio
async def test_batch_non_fashion_returns_three_noops(monkeypatch):
    _enable_flag(monkeypatch)
    _install_batch_mock(monkeypatch, {
        "material": {"value": "100% cotton", "confidence": 0.9},
        "care": {"value": "hand wash", "confidence": 0.9},
        "size_guide": {"value": "see chart", "confidence": 0.9},
    })
    out = await batch_extract_fashion_fields(
        description="Material: 100% cotton. Care: hand wash.",
        category_path="beauty/skincare/serum",
    )
    for r in out.values():
        assert r.value is None


@pytest.mark.asyncio
async def test_batch_happy_path_all_three(monkeypatch):
    _enable_flag(monkeypatch)
    _install_batch_mock(monkeypatch, {
        "material": {"value": "100% organic cotton", "confidence": 0.8},
        "care": {"value": "Hand wash cold", "confidence": 0.7},
        "size_guide": {"value": "See size chart below", "confidence": 0.9},
    })
    out = await batch_extract_fashion_fields(
        description=(
            "A breezy dress. Material: 100% organic cotton. "
            "Care: Hand wash cold; lay flat to dry. "
            "Sizing: See size chart below for measurements."
        ),
        category_path="fashion/dresses",
    )
    assert out["material"].value == "100% organic cotton"
    assert out["material"].confidence == 0.8  # 0.8 × 1.0 verbatim × 1.0 category
    assert out["care"].value == "Hand wash cold"
    assert out["care"].confidence == 0.7
    assert out["size_guide"].value == "See size chart below"
    assert out["size_guide"].confidence == 0.9


@pytest.mark.asyncio
async def test_batch_substring_grounding_drops_per_field(monkeypatch):
    _enable_flag(monkeypatch)
    # material is verbatim, care is hallucinated, size_guide is null.
    _install_batch_mock(monkeypatch, {
        "material": {"value": "100% cotton", "confidence": 0.9},
        "care": {"value": "machine wash hot only", "confidence": 0.9},  # not in source
        "size_guide": {"value": None, "confidence": 1.0, "reason": "not_stated"},
    })
    out = await batch_extract_fashion_fields(
        description="Material: 100% cotton. Hand wash cold.",
        category_path="fashion/tops",
    )
    assert out["material"].value == "100% cotton"
    assert out["material"].confidence > 0
    assert out["care"].value is None  # grounding rejected
    assert out["care"].confidence == 0.0
    assert out["size_guide"].value is None  # LLM declined
    assert out["size_guide"].confidence == 0.0


@pytest.mark.asyncio
async def test_batch_transport_failure_returns_three_noops(monkeypatch):
    _enable_flag(monkeypatch)
    _install_batch_mock(monkeypatch, None)
    out = await batch_extract_fashion_fields(
        description="Material: 100% cotton",
        category_path="fashion/tops",
    )
    for r in out.values():
        assert r.value is None
        assert r.confidence == 0.0


@pytest.mark.asyncio
async def test_batch_empty_haystack_returns_three_noops(monkeypatch):
    _enable_flag(monkeypatch)
    _install_batch_mock(monkeypatch, {
        "material": {"value": "anything", "confidence": 1.0},
        "care": {"value": "anything", "confidence": 1.0},
        "size_guide": {"value": "anything", "confidence": 1.0},
    })
    out = await batch_extract_fashion_fields(
        title=None, description=None, category_path="fashion/dresses",
    )
    for r in out.values():
        assert r.value is None


@pytest.mark.asyncio
async def test_batch_partial_response_safe(monkeypatch):
    """LLM returns malformed shape (missing keys / wrong types) — every
    field still surfaces as a clean None ExtractionResult, never raises."""
    _enable_flag(monkeypatch)
    _install_batch_mock(monkeypatch, {"material": "not-a-dict", "care": None})
    out = await batch_extract_fashion_fields(
        description="Material: 100% cotton",
        category_path="fashion/tops",
    )
    assert set(out.keys()) == {"material", "care", "size_guide"}
    for r in out.values():
        assert r.value is None
