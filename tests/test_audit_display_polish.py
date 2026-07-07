"""Display-polish guards for merchant-facing audit copy:

1. `sanitize_display_name` — a dirty identity/product name (e.g. a leaked
   "...30 sticks's page") must not render verbatim, while legitimate names
   (apostrophes, parens, ampersands, accents, brackets) pass through UNCHANGED.
2. INVISIBLE verdict label coherence — the header must not say "Invisible..."
   atop a nonzero cited-count body; it softens to "Rarely cited...".
"""
import pytest

from services.text_normalization import sanitize_display_name
from services.agent_center_bd_report_service import (
    _verdict_display_label,
    VERDICT_INVISIBLE,
    VERDICT_STRONG,
    VERDICT_MISATTRIBUTED,
)
from services.next_best_action import _sku_title


# --- #1 identity sanitizer -------------------------------------------------

# Real product/brand names observed in prod audit artifacts (DamDam / Anuko).
# These MUST pass through untouched — over-sanitizing a real name is a worse
# trust failure than the rare dirty one.
_LEGIT_NAMES = [
    "Paula's Choice",
    "DAMDAM NOMAD'S CREAM - Purifying Cleanser",
    "ANUKO Bond & Repair Hair Oil (75ml)",
    "ANUKO Nourishing Hair Butter Treatment - Shea Butter & Green Tea (200ml)",
    "Kérastase",
    "K-Beauty [Set]",
    "SK-II",
]


@pytest.mark.parametrize("name", _LEGIT_NAMES)
def test_legit_names_pass_through_unchanged(name):
    assert sanitize_display_name(name) == name


@pytest.mark.parametrize(
    "dirty,expected",
    [
        ("NUTRIONE CO., LTD [Bundle]...30 sticks's page", "NUTRIONE CO., LTD [Bundle]...30 sticks"),
        ("ANUKO Hair Oil (75ml)'s page", "ANUKO Hair Oil (75ml)"),
        ("  spaced   \n  name  ", "spaced name"),
        ('"Quoted Name"', "Quoted Name"),
        ("Truncated title...", "Truncated title"),
        ("Ends in ellipsis…", "Ends in ellipsis"),
        (None, ""),
        ("", ""),
        ("   ", ""),
    ],
)
def test_dirty_names_are_cleaned(dirty, expected):
    assert sanitize_display_name(dirty) == expected


def test_paragraph_length_name_is_capped():
    long = "A really long marketing sentence masquerading as a product name " * 5
    out = sanitize_display_name(long)
    assert len(out) <= 100
    assert out.endswith("…")


def test_sanitize_is_idempotent():
    dirty = "NUTRIONE CO., LTD [Bundle]...30 sticks's page"
    once = sanitize_display_name(dirty)
    assert sanitize_display_name(once) == once


def test_sku_title_sanitizes_identity_name():
    assert _sku_title(identity={"name": "Foo 30 sticks's page"}, sku_title=None) == "Foo 30 sticks"
    # legit name untouched
    assert _sku_title(identity={"name": "Paula's Choice"}, sku_title=None) == "Paula's Choice"
    # empty identity -> falls back through sku_title, then default
    assert _sku_title(identity={}, sku_title="  Clean Title  ") == "Clean Title"
    assert _sku_title(identity={}, sku_title=None) == "this SKU"


# --- #2 INVISIBLE label coherence ------------------------------------------

def test_invisible_label_softens_only_when_cited():
    # Zero (or unknown) citations -> the honest flat "Invisible" label.
    assert _verdict_display_label(VERDICT_INVISIBLE, cited_runs=0) == "Invisible in grounded LLM citations"
    assert _verdict_display_label(VERDICT_INVISIBLE) == "Invisible in grounded LLM citations"
    # Nonzero citations -> softened, so the header no longer contradicts a
    # "cited in N of M" body.
    softened = _verdict_display_label(VERDICT_INVISIBLE, cited_runs=3)
    assert softened == "Rarely cited in grounded LLM answers"
    assert "invisible" not in softened.lower()


def test_cited_runs_does_not_affect_other_verdicts():
    assert _verdict_display_label(VERDICT_STRONG, cited_runs=0) == "Strong AI-channel attribution"
    assert _verdict_display_label(VERDICT_MISATTRIBUTED, cited_runs=9) == "Visible but misattributed"
