"""The composer is the only new logic — pin its honesty gates.

Everything else in scripts/author_descriptions_tier_g.py is imported from the
reviewed source_pdp_content_repair write path; what needs testing here is that
the deterministic composer (a) only assembles source-truth fragments, (b)
refuses thin input rather than padding, and (c) never emits efficacy language.
"""

from __future__ import annotations

from scripts.author_descriptions_tier_g import (
    COMPOSE_MIN_LENGTH,
    MIN_INCI_TOKENS,
    compose_description,
)

_INCI = (
    "Water, Niacinamide, Glycerin, Butylene Glycol, Sodium Hyaluronate, "
    "Panthenol, Adenosine, Madecassoside, Phenoxyethanol"
)


def _row(**over):
    row = {
        "title": "Revive Barrier Serum",
        "brand": "Arolinne",
        "product_type": "Serum",
        "category": "skincare",
        "category_path": "skincare/serum",
        "cp_description": "",
        "raw_inci": _INCI,
    }
    row.update(over)
    return row


def test_composes_identity_actives_and_full_inci():
    out = compose_description(_row())
    assert out is not None
    d = out["description"]
    assert "Revive Barrier Serum by Arolinne" in d
    assert "Full ingredients (INCI):" in d and "Niacinamide" in d
    assert len(d) >= COMPOSE_MIN_LENGTH


def test_refuses_thin_inci_rather_than_padding():
    assert compose_description(_row(raw_inci="Water, Glycerin")) is None
    assert MIN_INCI_TOKENS == 3  # the gate the line above exercises


def test_refuses_missing_title():
    assert compose_description(_row(title="")) is None


def test_never_emits_efficacy_language():
    """Identity claims only — the INCI source cannot substantiate efficacy.

    Guard the words that would turn 'contains X' into a benefit claim."""
    out = compose_description(_row())
    lowered = out["description"].lower()
    for banned in ("reduces", "improves", "treats", "anti-aging", "brighten",
                   "repairs", "clinically", "proven"):
        assert banned not in lowered, banned


def test_brand_not_duplicated_when_already_in_title():
    out = compose_description(_row(title="Arolinne Revive Barrier Serum"))
    assert out["description"].count("Arolinne") == 1
