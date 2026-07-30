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
from services.crawled_inci_ingest import _PROSE_RE

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
    out = compose_description(_row(raw_inci="Water, Glycerin, Aqua"))
    # parse_inci dedupes Water/Aqua; under MIN_INCI_TOKENS -> refused
    assert out["refused"] in ("too_few_inci_tokens", "too_thin_to_compose")
    assert MIN_INCI_TOKENS == 3


def test_refuses_missing_title():
    assert compose_description(_row(title=""))["refused"] == "missing_title"


def test_refuses_marketing_prose_stamped_as_inci():
    """THE review-blocking case: the merchant-sync lane stores raw_inci with
    no validation, so a benefits block can arrive labelled as ingredients.
    The shared _looks_like_inci gate must refuse it before composition."""
    prose = ("Aloe soothes and calms irritated skin, Vitamin C brightens dull "
             "skin, Peptides visibly reduce fine lines and wrinkles")
    assert compose_description(_row(raw_inci=prose))["refused"] == "not_inci_like"


def test_refuses_below_compose_min_length():
    out = compose_description(_row(title="Gel", brand="", product_type="",
                                   raw_inci="Water, Glycerin, Panthenol"))
    assert out["refused"] == "too_thin_to_compose"
    assert COMPOSE_MIN_LENGTH == 120


def test_never_emits_efficacy_language():
    """Identity claims only — the INCI source cannot substantiate efficacy.

    Two layers: the repo's OWN prose detector must find nothing (so this test
    tracks the shared vocabulary, not a private weaker copy), and the output
    must be EXACTLY the 3-part template — any added sentence fails regardless
    of vocabulary."""
    out = compose_description(_row())
    d = out["description"]
    assert not _PROSE_RE.search(d)
    import re as _re
    assert _re.fullmatch(
        r"[^.]+\. (Formulated with [^.]+\. )?Full ingredients \(INCI\): .+\.",
        d,
    ), d


def test_brand_not_duplicated_when_already_in_title():
    out = compose_description(_row(title="Arolinne Revive Barrier Serum"))
    assert out["description"].count("Arolinne") == 1
