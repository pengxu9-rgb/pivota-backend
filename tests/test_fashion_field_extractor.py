"""Tests for services.fashion_field_extractor (Phase O-5b)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.fashion_field_extractor import (  # noqa: E402
    EXTRACTION_SOURCE_REGEX,
    extract_care,
    extract_material,
    extract_size_guide,
)


# ---------- material ----------

def test_extract_material_clean_match():
    result = extract_material(
        title="Summer Linen Dress",
        description="Material: 100% organic cotton; OEKO-TEX certified.",
    )
    assert result.value is not None
    assert "100% organic cotton" in result.value
    assert result.confidence >= 0.6
    assert result.source == EXTRACTION_SOURCE_REGEX


def test_extract_material_via_fabric_alias():
    result = extract_material(
        description="Fabric: 90% nylon, 10% spandex",
    )
    assert result.value is not None
    assert "nylon" in result.value.lower()


def test_extract_material_via_composition_alias():
    result = extract_material(
        description="Composition: Pure merino wool, knitted in Italy.",
    )
    assert result.value is not None
    assert "merino wool" in result.value.lower()


def test_extract_material_returns_none_when_absent():
    result = extract_material(
        title="Plain T-Shirt",
        description="A great everyday shirt with a soft feel.",
    )
    assert result.value is None
    assert result.confidence == 0.0


def test_extract_material_handles_empty_inputs():
    result = extract_material()
    assert result.value is None
    assert result.confidence == 0.0


# ---------- care ----------

def test_extract_care_clean_match():
    result = extract_care(
        description="Care: Hand wash cold; lay flat to dry.",
    )
    assert result.value is not None
    assert "hand wash cold" in result.value.lower()
    assert result.source == EXTRACTION_SOURCE_REGEX


def test_extract_care_via_washing_instructions_alias():
    result = extract_care(
        description="Washing instructions: Machine wash cold on delicate; tumble dry low.",
    )
    assert result.value is not None
    assert "machine wash" in result.value.lower()


def test_extract_care_returns_none_when_absent():
    result = extract_care(
        title="Cotton Tee",
        description="Made for everyday wear.",
    )
    assert result.value is None


# ---------- size guide ----------

def test_extract_size_guide_present():
    result = extract_size_guide(
        description="Size guide: see chart below for measurements per size.",
    )
    assert result.value is not None
    assert result.confidence > 0


def test_extract_size_guide_via_sizing_alias():
    result = extract_size_guide(
        description="Sizing: We recommend ordering your usual size.",
    )
    assert result.value is not None


def test_extract_size_guide_absent_returns_none():
    result = extract_size_guide(
        description="Soft material that feels great against skin.",
    )
    assert result.value is None


# ---------- confidence behaviour ----------

def test_short_match_gets_downgraded_confidence():
    # 4-char value triggers the < 5 downgrade.
    result = extract_material(description="Material: 4ply")
    assert result.value is not None
    assert result.confidence == 0.55


def test_long_match_gets_downgraded_confidence():
    long_value = "x" * 150
    result = extract_material(description=f"Material: {long_value}")
    assert result.value is not None
    assert result.confidence == 0.65


def test_clean_short_match_gets_top_confidence():
    result = extract_material(description="Material: 100% cotton, sourced ethically.")
    assert result.value is not None
    assert result.confidence == 0.75


# ---------- source-text grounding (substring validator) ----------

def test_extracted_value_must_appear_in_source_text():
    # All current regex extractors capture FROM the haystack so the
    # substring validator is essentially always true. This test pins
    # the invariant so a future LLM extractor cannot bypass grounding.
    result = extract_material(description="Material: 100% cashmere.")
    assert result.value in "Material: 100% cashmere."
