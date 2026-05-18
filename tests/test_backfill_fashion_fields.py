"""Tests for scripts/backfill_fashion_fields.py — pure helpers only.

The DB I/O is exercised in staging; here we lock the extractor wiring
+ haystack assembly + update-builder semantics so a future refactor
can't silently regress them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backfill_fashion_fields import (  # noqa: E402
    _apply_update,
    _description_haystack,
)
from services.fashion_field_extractor import (  # noqa: E402
    ExtractionResult,
    extract_care,
    extract_material,
    extract_size_guide,
)


# ---------- haystack assembly ----------

def test_haystack_uses_description_column_first():
    row = {"description": "Material: 100% cotton", "title": "Tee"}
    haystack = _description_haystack(row)
    assert "Material: 100% cotton" in haystack
    # Title is included too (least specific, but still present).
    assert "Tee" in haystack


def test_haystack_pulls_from_product_payload_dict():
    row = {
        "title": "Item",
        "product_payload": {
            "description_text": "Care: Hand wash cold.",
            "body_html": "<p>Made of wool</p>",
        },
    }
    haystack = _description_haystack(row)
    assert "Hand wash cold" in haystack
    assert "Made of wool" in haystack


def test_haystack_parses_product_payload_string():
    # Some DB drivers return JSONB as a string; ensure we don't break.
    row = {
        "title": "Item",
        "product_payload": json.dumps({"description": "Material: silk"}),
    }
    haystack = _description_haystack(row)
    assert "Material: silk" in haystack


def test_haystack_pulls_from_external_seed_snapshot():
    row = {
        "title": "Item",
        "seed_data": {
            "snapshot": {
                "description": "Fabric: 60% linen, 40% cotton",
            }
        },
    }
    haystack = _description_haystack(row)
    assert "linen" in haystack


def test_haystack_handles_all_null_inputs_gracefully():
    haystack = _description_haystack({"title": None, "description": None})
    assert haystack == ""


# ---------- end-to-end: extractors on a synthetic Shopify-style row ----------

@pytest.mark.asyncio
async def test_extractors_are_async_and_default_off(monkeypatch):
    # Phase O-5b v2: the extractors are async, category-gated, and gated
    # by FASHION_EXTRACT_ENABLED (default off). The backfill script loops
    # over every row + invokes them, but they short-circuit cheaply when
    # the flag is off OR the row isn't fashion-categorized — so a
    # whole-catalog --apply against the current 99%-beauty catalog
    # produces zero extractions.
    monkeypatch.delenv("FASHION_EXTRACT_ENABLED", raising=False)
    row = {
        "title": "Linen Summer Dress",
        "description": "Material: 100% European linen. Care: Machine wash cold.",
    }
    haystack = _description_haystack(row)
    m = await extract_material(description=haystack, category_path="fashion/dresses")
    c = await extract_care(description=haystack, category_path="fashion/dresses")
    s = await extract_size_guide(description=haystack, category_path="fashion/dresses")
    assert m.value is None
    assert c.value is None
    assert s.value is None


# ---------- _apply_update behavior (SQL string + params) ----------

class _RecordingDb:
    """Minimal database stub that captures execute() calls."""
    def __init__(self):
        self.calls = []

    async def execute(self, sql, params):
        self.calls.append((sql, params))


@pytest.mark.asyncio
async def test_apply_update_skips_when_no_extractions(monkeypatch):
    from scripts import backfill_fashion_fields as module

    recorder = _RecordingDb()
    monkeypatch.setattr(module, "database", recorder)
    await _apply_update(
        product_key="prod_x",
        material=ExtractionResult(value=None, confidence=0.0, source="regex_extraction_v1"),
        care=ExtractionResult(value=None, confidence=0.0, source="regex_extraction_v1"),
        size_guide=ExtractionResult(value=None, confidence=0.0, source="regex_extraction_v1"),
    )
    assert recorder.calls == []  # nothing to update → no SQL emitted


@pytest.mark.asyncio
async def test_apply_update_only_sets_extracted_fields(monkeypatch):
    from scripts import backfill_fashion_fields as module

    recorder = _RecordingDb()
    monkeypatch.setattr(module, "database", recorder)
    await _apply_update(
        product_key="prod_x",
        material=ExtractionResult(value="100% cotton", confidence=0.75, source="regex_extraction_v1"),
        care=ExtractionResult(value=None, confidence=0.0, source="regex_extraction_v1"),
        size_guide=ExtractionResult(value=None, confidence=0.0, source="regex_extraction_v1"),
    )
    assert len(recorder.calls) == 1
    sql, params = recorder.calls[0]
    # Only material columns should be in the SET clause.
    assert "material = :material" in sql
    assert "material_source = :material_source" in sql
    assert "material_confidence = :material_confidence" in sql
    assert "care = :care" not in sql
    assert "size_guide" not in sql
    # The WHERE clause adds the per-column IS NULL guard so concurrent runs
    # don't clobber a value another path already set.
    assert "material IS NULL" in sql
    assert params["material"] == "100% cotton"
    assert params["material_confidence"] == 0.75


@pytest.mark.asyncio
async def test_apply_update_wraps_size_guide_in_jsonb_envelope(monkeypatch):
    from scripts import backfill_fashion_fields as module

    recorder = _RecordingDb()
    monkeypatch.setattr(module, "database", recorder)
    await _apply_update(
        product_key="prod_x",
        material=ExtractionResult(value=None, confidence=0.0, source="regex_extraction_v1"),
        care=ExtractionResult(value=None, confidence=0.0, source="regex_extraction_v1"),
        size_guide=ExtractionResult(value="see chart below", confidence=0.7, source="regex_extraction_v1"),
    )
    sql, params = recorder.calls[0]
    assert "size_guide = :size_guide" in sql
    decoded = json.loads(params["size_guide"])
    assert decoded == {"raw": "see chart below"}
