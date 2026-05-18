"""Tests for services.fashion_field_payload_extractor (Phase O-5b #3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.fashion_field_payload_extractor import (  # noqa: E402
    extract_care_from_payload,
    extract_material_from_payload,
    extract_size_guide_from_payload,
)


# ---------- shopify standard metafield namespace ----------

def test_material_via_shopify_namespace():
    md = {
        "metafields": [
            {"namespace": "shopify", "key": "material", "value": "100% organic cotton",
             "type": "single_line_text_field"},
        ],
    }
    assert extract_material_from_payload(md) == "100% organic cotton"


def test_material_via_custom_namespace():
    md = {
        "metafields": [
            {"namespace": "custom", "key": "material", "value": "90% nylon, 10% spandex"},
        ],
    }
    assert extract_material_from_payload(md) == "90% nylon, 10% spandex"


def test_material_via_custom_fabric_alias():
    md = {"metafields": [{"namespace": "custom", "key": "fabric", "value": "linen"}]}
    assert extract_material_from_payload(md) == "linen"


def test_priority_shopify_namespace_wins_over_custom():
    # Both present — shopify wins (first in priority list).
    md = {
        "metafields": [
            {"namespace": "custom", "key": "material", "value": "WRONG"},
            {"namespace": "shopify", "key": "material", "value": "RIGHT"},
        ],
    }
    assert extract_material_from_payload(md) == "RIGHT"


# ---------- legacy / admin top-level injection ----------

def test_material_via_toplevel_legacy():
    md = {"material": "denim"}
    assert extract_material_from_payload(md) == "denim"


def test_toplevel_is_fallback_when_metafields_empty():
    md = {"metafields": [], "material": "fallback wool"}
    assert extract_material_from_payload(md) == "fallback wool"


def test_metafields_win_over_toplevel():
    md = {
        "material": "TOPLEVEL_WRONG",
        "metafields": [{"namespace": "custom", "key": "material", "value": "METAFIELD_RIGHT"}],
    }
    assert extract_material_from_payload(md) == "METAFIELD_RIGHT"


# ---------- care ----------

def test_care_via_shopify_namespace():
    md = {"metafields": [{"namespace": "shopify", "key": "care_instructions",
                          "value": "Machine wash cold; tumble dry low."}]}
    assert extract_care_from_payload(md) == "Machine wash cold; tumble dry low."


def test_care_via_custom_washing_instructions_alias():
    md = {"metafields": [{"namespace": "custom", "key": "washing_instructions",
                          "value": "Hand wash only."}]}
    assert extract_care_from_payload(md) == "Hand wash only."


# ---------- size_guide ----------

def test_size_guide_passthrough_dict():
    md = {"metafields": [{"namespace": "custom", "key": "size_guide",
                          "value": {"columns": ["Size", "Bust"], "rows": []}}]}
    result = extract_size_guide_from_payload(md)
    assert result == {"columns": ["Size", "Bust"], "rows": []}


def test_size_guide_json_string_parsed():
    serialized = json.dumps({"columns": ["S", "M"], "rows": [{"label": "S", "values": []}]})
    md = {"metafields": [{"namespace": "shopify", "key": "size_chart",
                          "value": serialized, "type": "json_string"}]}
    result = extract_size_guide_from_payload(md)
    assert isinstance(result, dict)
    assert result["columns"] == ["S", "M"]


def test_size_guide_plain_string_wrapped():
    md = {"metafields": [{"namespace": "custom", "key": "size_guide",
                          "value": "See chart below"}]}
    result = extract_size_guide_from_payload(md)
    assert result == {"raw": "See chart below"}


# ---------- absence / robustness ----------

def test_returns_none_when_no_metadata():
    assert extract_material_from_payload(None) is None
    assert extract_care_from_payload(None) is None
    assert extract_size_guide_from_payload(None) is None


def test_returns_none_when_metafields_empty_and_no_toplevel():
    assert extract_material_from_payload({"metafields": []}) is None


def test_returns_none_when_metafield_value_empty():
    md = {"metafields": [{"namespace": "custom", "key": "material", "value": ""}]}
    assert extract_material_from_payload(md) is None


def test_ignores_non_dict_metafields_entries():
    md = {"metafields": ["not-a-dict", {"namespace": "custom", "key": "material", "value": "wool"}]}
    assert extract_material_from_payload(md) == "wool"


def test_size_guide_unparseable_json_falls_back_to_raw():
    md = {"metafields": [{"namespace": "shopify", "key": "size_chart",
                          "value": "not valid json {"}]}
    result = extract_size_guide_from_payload(md)
    assert result == {"raw": "not valid json {"}
