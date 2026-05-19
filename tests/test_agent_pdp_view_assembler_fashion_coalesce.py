"""Tests for the cross-PDP fashion-field coalesce added to
services.agent_pdp_view_assembler.

The unit under test is pure-function: `coalesce_fashion_fields(products,
external_seed)` returns the winning value + source + confidence for each
of material / care / size_guide. No DB.

Source-priority ordering (lower = better):
    merchant_payload > merchant_authored > llm_extraction_v1 > external_seed > (unknown)
Ties broken by confidence DESC.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.agent_pdp_view_assembler import (  # noqa: E402
    _source_rank,
    coalesce_fashion_fields,
)


def _p(**fields):
    """Tiny helper: build one product-member row with the fashion-field
    columns the assembler SELECT projects. Missing keys default to None."""
    out = {
        "material": None, "material_source": None, "material_confidence": None,
        "care": None, "care_source": None, "care_confidence": None,
        "size_guide": None, "size_guide_source": None, "size_guide_confidence": None,
    }
    out.update(fields)
    return out


# ---------- source-priority ordering ----------

def test_source_rank_ordering():
    assert _source_rank("merchant_payload") < _source_rank("merchant_authored")
    assert _source_rank("merchant_authored") < _source_rank("llm_extraction_v1")
    assert _source_rank("llm_extraction_v1") < _source_rank("external_seed")
    assert _source_rank("external_seed") < _source_rank("unknown_source")
    assert _source_rank("unknown_source") < _source_rank(None)


# ---------- single member, no seed ----------

def test_single_member_passes_through():
    products = [_p(material="cotton", material_source="merchant_authored", material_confidence=1.0)]
    out = coalesce_fashion_fields(products, None)
    assert out["material"] == "cotton"
    assert out["material_source"] == "merchant_authored"
    assert out["material_confidence"] == 1.0
    # Other fields stay null
    assert out["care"] is None
    assert out["size_guide"] is None


def test_empty_member_set_returns_nulls():
    out = coalesce_fashion_fields([], None)
    assert out == {
        "material": None, "material_source": None, "material_confidence": None,
        "care": None, "care_source": None, "care_confidence": None,
        "size_guide": None, "size_guide_source": None, "size_guide_confidence": None,
    }


# ---------- source priority across merchants ----------

def test_merchant_payload_beats_merchant_authored():
    products = [
        _p(material="from_payload", material_source="merchant_payload", material_confidence=1.0),
        _p(material="from_authored", material_source="merchant_authored", material_confidence=1.0),
    ]
    out = coalesce_fashion_fields(products, None)
    assert out["material"] == "from_payload"
    assert out["material_source"] == "merchant_payload"


def test_merchant_authored_beats_llm():
    products = [
        _p(material="from_authored", material_source="merchant_authored", material_confidence=1.0),
        _p(material="from_llm", material_source="llm_extraction_v1", material_confidence=0.9),
    ]
    out = coalesce_fashion_fields(products, None)
    assert out["material"] == "from_authored"


def test_llm_beats_external_seed():
    products = [
        _p(material="from_llm", material_source="llm_extraction_v1", material_confidence=0.7),
    ]
    seed = {"seed_data": {"material": "from_seed"}}
    out = coalesce_fashion_fields(products, seed)
    assert out["material"] == "from_llm"
    assert out["material_source"] == "llm_extraction_v1"


def test_external_seed_fills_when_no_merchant_data():
    products = [_p()]  # no fashion fields populated
    seed = {"seed_data": {"material": "from_seed", "care": "hand wash from seed"}}
    out = coalesce_fashion_fields(products, seed)
    assert out["material"] == "from_seed"
    assert out["material_source"] == "external_seed"
    assert out["material_confidence"] == 1.0
    assert out["care"] == "hand wash from seed"
    assert out["care_source"] == "external_seed"


# ---------- per-field independence ----------

def test_winning_member_can_differ_per_field():
    """Merchant A has material; merchant B has care; merchant C has size_guide.
    Coalesce picks the right source per field — no single 'canonical' row."""
    products = [
        _p(material="cotton", material_source="merchant_authored", material_confidence=1.0),
        _p(care="hand wash", care_source="merchant_payload", care_confidence=1.0),
        _p(size_guide={"raw": "see chart"}, size_guide_source="llm_extraction_v1",
           size_guide_confidence=0.8),
    ]
    out = coalesce_fashion_fields(products, None)
    assert out["material"] == "cotton"
    assert out["material_source"] == "merchant_authored"
    assert out["care"] == "hand wash"
    assert out["care_source"] == "merchant_payload"
    assert out["size_guide"] == {"raw": "see chart"}
    assert out["size_guide_source"] == "llm_extraction_v1"


# ---------- tie-breaking by confidence ----------

def test_ties_broken_by_confidence_desc():
    """Two LLM-sourced material values from co-merchants — higher confidence wins."""
    products = [
        _p(material="low_conf", material_source="llm_extraction_v1", material_confidence=0.6),
        _p(material="high_conf", material_source="llm_extraction_v1", material_confidence=0.9),
    ]
    out = coalesce_fashion_fields(products, None)
    assert out["material"] == "high_conf"
    assert out["material_confidence"] == 0.9


def test_missing_confidence_ranks_lowest_within_same_source():
    products = [
        _p(material="with_conf", material_source="llm_extraction_v1", material_confidence=0.5),
        _p(material="no_conf", material_source="llm_extraction_v1", material_confidence=None),
    ]
    out = coalesce_fashion_fields(products, None)
    assert out["material"] == "with_conf"


# ---------- ignores empty values ----------

def test_empty_string_treated_as_missing():
    products = [
        _p(material="", material_source="merchant_payload"),  # empty — skip
        _p(material="cotton", material_source="llm_extraction_v1", material_confidence=0.5),
    ]
    out = coalesce_fashion_fields(products, None)
    assert out["material"] == "cotton"  # the LLM one wins because the payload was empty


# ---------- external seed shape ----------

def test_external_seed_with_json_string_seed_data():
    """seed_data can land as a JSON-encoded string instead of a dict; handle both."""
    products = [_p()]
    seed = {"seed_data": '{"material": "from_json_string_seed"}'}
    out = coalesce_fashion_fields(products, seed)
    assert out["material"] == "from_json_string_seed"


def test_external_seed_with_malformed_string_seed_data():
    """If seed_data is unparseable, ignore — never raise."""
    products = [_p()]
    seed = {"seed_data": "not valid json {{"}
    out = coalesce_fashion_fields(products, seed)
    assert out["material"] is None  # no candidate


def test_external_seed_without_fashion_keys():
    products = [_p()]
    seed = {"seed_data": {"title": "x", "description": "y"}}  # no material/care/size_guide
    out = coalesce_fashion_fields(products, seed)
    assert out["material"] is None
    assert out["care"] is None
    assert out["size_guide"] is None


# ---------- unknown source handling ----------

def test_unknown_source_ranks_lowest():
    products = [
        _p(material="from_unknown", material_source="some_legacy_source", material_confidence=1.0),
        _p(material="from_llm", material_source="llm_extraction_v1", material_confidence=0.5),
    ]
    out = coalesce_fashion_fields(products, None)
    assert out["material"] == "from_llm"  # known source beats unknown


def test_null_source_with_value_still_considered():
    """Defensive: a value with source=None still beats no candidate at all."""
    products = [_p(material="orphan_value", material_source=None, material_confidence=None)]
    out = coalesce_fashion_fields(products, None)
    assert out["material"] == "orphan_value"
