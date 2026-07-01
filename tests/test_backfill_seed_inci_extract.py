"""extract_inci pulls the real INCI list out of seed_data text that carries
marketing preamble + an 'Ingredients:' label. Pure, no DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backfill_seed_inci import extract_inci, looks_like_inci  # noqa: E402


def test_looks_like_inci_accepts_real_list():
    assert looks_like_inci("Water, Glycerin, Butylene Glycol, Niacinamide, Panthenol") is True


def test_looks_like_inci_rejects_cross_reference():
    assert looks_like_inci("see Natural Blur Powder Foundation for list of ingredients.") is False


def test_looks_like_inci_rejects_marketing_bullets():
    assert looks_like_inci("• Avocado conditions & replenishes • Jojoba Oil hydrates") is False


def test_strips_preamble_after_ingredients_marker():
    raw = (
        "Key Ingredients\n\n- Hyaluronic Acid\n\nThe formula glides on smoothly.\n\n"
        "Ingredients: Water, Glycerin, Butylene Glycol, Niacinamide, Sodium Hyaluronate"
    )
    out = extract_inci(raw)
    assert out.startswith("Water, Glycerin")
    assert "Hyaluronic Acid" not in out  # the prose block is dropped
    assert "formula glides" not in out


def test_takes_last_marker_when_multiple():
    raw = "Ingredients description blah. Full Ingredients: Aqua, Dimethicone, Tocopherol"
    out = extract_inci(raw)
    assert out == "Aqua, Dimethicone, Tocopherol"


def test_clean_list_without_marker_passes_through():
    raw = "Water, Glycerin, Butylene Glycol, Panthenol"
    assert extract_inci(raw) == "Water, Glycerin, Butylene Glycol, Panthenol"


def test_collapses_whitespace_and_newlines():
    raw = "Ingredients:\n  Water,\n  Glycerin,\n\tNiacinamide"
    assert extract_inci(raw) == "Water, Glycerin, Niacinamide"


def test_empty_and_none():
    assert extract_inci("") == ""
    assert extract_inci(None) == ""


def test_dash_marker_variant():
    assert extract_inci("Ingredients - Aqua, Glycerin") == "Aqua, Glycerin"
