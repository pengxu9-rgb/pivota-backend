"""Tests for services.fashion_field_extractor — DEPRECATED no-op stage.

The regex extractor v1 produced false positives on beauty catalog text
(see services/fashion_field_extractor.py module docstring). It's been
neutered until LLM extractor v2 lands; these tests pin that semantics
so a future refactor can't silently restore the regex behavior.

When v2 ships, expand this file with LLM-mocked tests covering
category-gated extraction + calibrated confidence + substring grounding.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.fashion_field_extractor import (  # noqa: E402
    EXTRACTION_SOURCE_LLM,
    EXTRACTION_SOURCE_REGEX,
    ExtractionResult,
    extract_care,
    extract_material,
    extract_size_guide,
)


# ---------- contract: stable source enum values ----------

def test_source_enums_stable_across_v1_v2():
    # Downstream consumers (PIVOTA-Agent pdpBuilder trust gate) depend on
    # these exact string values. Don't rename without coordinating.
    assert EXTRACTION_SOURCE_REGEX == "regex_extraction_v1"
    assert EXTRACTION_SOURCE_LLM == "llm_extraction_v1"


# ---------- contract: no-op for all extract_* functions ----------

def test_extract_material_is_noop():
    result = extract_material(
        title="Linen Summer Dress",
        description="Material: 100% organic cotton; OEKO-TEX certified.",
    )
    assert result.value is None
    assert result.confidence == 0.0
    assert result.source == EXTRACTION_SOURCE_REGEX  # source stays for telemetry


def test_extract_care_is_noop():
    result = extract_care(
        description="Care: Hand wash cold; lay flat to dry.",
    )
    assert result.value is None
    assert result.confidence == 0.0


def test_extract_size_guide_is_noop():
    result = extract_size_guide(
        description="Size guide: see chart below for measurements per size.",
    )
    assert result.value is None
    assert result.confidence == 0.0


def test_extract_with_no_inputs_is_noop():
    assert extract_material().value is None
    assert extract_care().value is None
    assert extract_size_guide().value is None


# ---------- contract: ExtractionResult shape stays stable ----------

def test_extraction_result_has_required_fields():
    r = ExtractionResult(value="x", confidence=0.5, source=EXTRACTION_SOURCE_REGEX)
    assert r.value == "x"
    assert r.confidence == 0.5
    assert r.source == EXTRACTION_SOURCE_REGEX
