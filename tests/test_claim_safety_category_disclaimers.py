"""ensure_category_disclaimers — floor-safety merge of category-mandatory
disclaimers (the FDA/DSHEA supplement statement) with authored ones.
Pure, no DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.catalog import RequiredDisclaimer  # noqa: E402
from services.claim_safety import (  # noqa: E402
    FDA_DSHEA_DISCLAIMER_CODE,
    FDA_DSHEA_DISCLAIMER_TEXT,
    ensure_category_disclaimers,
)


def _codes(disclaimers):
    return [d["code"] for d in (disclaimers or [])]


def test_supplement_with_no_authored_gets_fda_disclaimer():
    out = ensure_category_disclaimers(None, "supplement")
    assert _codes(out) == [FDA_DSHEA_DISCLAIMER_CODE]
    assert out[0]["text"] == FDA_DSHEA_DISCLAIMER_TEXT


def test_supplement_with_empty_list_gets_fda_disclaimer():
    out = ensure_category_disclaimers([], "supplement")
    assert _codes(out) == [FDA_DSHEA_DISCLAIMER_CODE]


def test_skincare_is_a_noop_and_preserves_none():
    assert ensure_category_disclaimers(None, "skincare") is None
    authored = [{"code": "eu_cosmetics", "text": "..."}]
    assert ensure_category_disclaimers(authored, "skincare") == authored


def test_unclassified_category_is_a_noop():
    assert ensure_category_disclaimers(None, None) is None


def test_authored_fda_disclaimer_is_not_duplicated_and_wins():
    authored = [
        {"code": FDA_DSHEA_DISCLAIMER_CODE, "text": "Merchant-worded FDA statement."}
    ]
    out = ensure_category_disclaimers(authored, "supplement")
    # deduped by code -> single entry, and the authored text is preserved.
    assert _codes(out) == [FDA_DSHEA_DISCLAIMER_CODE]
    assert out[0]["text"] == "Merchant-worded FDA statement."


def test_authored_other_disclaimer_keeps_both():
    authored = [{"code": "prop65", "text": "California Prop 65 warning."}]
    out = ensure_category_disclaimers(authored, "supplement")
    assert _codes(out) == ["prop65", FDA_DSHEA_DISCLAIMER_CODE]


def test_accepts_required_disclaimer_objects_in_authored():
    authored = [RequiredDisclaimer(code="prop65", text="Prop 65", applies_to=None)]
    out = ensure_category_disclaimers(authored, "supplement")
    assert _codes(out) == ["prop65", FDA_DSHEA_DISCLAIMER_CODE]
