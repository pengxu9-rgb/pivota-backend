"""Phase B unit tests: services.brand_alias.

derive_brand_aliases + text_mentions_brand — the alias normalization that fixes
the "BB Lab Global" != "BB Lab" under-count, WITHOUT the generic-word false
positives ("The Ordinary" -> "ordinary", "Magnesium Co" -> "magnesium").
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.brand_alias import derive_brand_aliases, text_mentions_brand


# --------------------------------------------------------------------------
# derive_brand_aliases
# --------------------------------------------------------------------------
def test_strips_trailing_marketing_suffix_to_core():
    a = derive_brand_aliases("BB Lab Global")
    assert "bb lab global" in a   # full form kept
    assert "bb lab" in a          # trailing "global" stripped
    assert "bblab" in a           # de-spaced core


def test_does_not_strip_leading_token_no_ordinary_landmine():
    a = derive_brand_aliases("The Ordinary")
    assert "the ordinary" in a
    assert "ordinary" not in a    # leading "the" is NOT stripped


def test_single_token_core_is_rejected_as_too_generic():
    a = derive_brand_aliases("Magnesium Co")
    assert "magnesium" not in a   # 1-token core would over-match supplement copy
    assert "magnesium co" in a


def test_multi_suffix_strip():
    a = derive_brand_aliases("Glow Recipe Inc")
    assert "glow recipe" in a
    assert "glowrecipe" in a


def test_host_registrable_name():
    assert "bblab" in derive_brand_aliases(None, "https://www.bblab.shop/x")
    assert "mybrand" in derive_brand_aliases(None, "shop.mybrand.com")
    assert "mybrand" in derive_brand_aliases(None, "mybrand.co.uk")  # ccTLD


def test_vendors_contribute_aliases():
    a = derive_brand_aliases("BB Lab Global", None, ("Glow Recipe Inc",))
    assert "bb lab" in a
    assert "glow recipe" in a


def test_min_length_and_dedup():
    a = derive_brand_aliases("AB")  # 2 chars -> below min
    assert "ab" not in a
    # dedup: no duplicates regardless of how many forms collapse together
    a2 = derive_brand_aliases("BB Lab")
    assert len(a2) == len(set(a2))


def test_diacritics_fold_to_ascii_base_letters():
    """'Kérastase' must alias to 'kerastase' — the form ASCII answer copy,
    source titles, and registrable host labels ('kerastase-usa') actually use.
    Pre-fix, the accented letter became a token BREAK ('k rastase' → de-spaced
    'krastase'), so the alias could never match, and a rival's own storefront
    slipped past the competitor flag into 'Get cited on' outreach (prod run
    83e8fcb4, competitor 'Kérastase' vs kerastase-usa.com)."""
    assert derive_brand_aliases("Kérastase") == ("kerastase",)
    creme = derive_brand_aliases("Crème de la Mer")
    assert "creme de la mer" in creme
    assert "cremedelamer" in creme
    assert derive_brand_aliases("Shiseidō") == ("shiseido",)


def test_trademark_marks_leave_no_letter_residue():
    """A trademark mark must never leave letter residue in the alias, in any
    spelling. The literal '™' is safe under either ordering (NFKD maps it to
    UPPERCASE 'TM', which the lowercase-only char class scrubs) — the case that
    actually discriminates is the fullwidth/compatibility spelling, which only
    normalizes into the strippable ASCII '(tm)' when the fold runs FIRST."""
    for raw in ("Brand™", "Brand®", "Brand℠", "Brand（ｔｍ）", "Brand (TM)"):
        assert derive_brand_aliases(raw) == ("brand",), raw


def test_folded_alias_matches_ascii_text():
    aliases = derive_brand_aliases("Kérastase")
    assert text_mentions_brand("best kerastase dupes for bond repair", aliases)


# --------------------------------------------------------------------------
# text_mentions_brand
# --------------------------------------------------------------------------
def test_matches_stripped_core_in_source_title():
    aliases = derive_brand_aliases("BB Lab Global", "bblab.shop")
    assert text_mentions_brand("bb lab collagen tangle up | iherb", aliases)
    assert text_mentions_brand("shop bblab.com today", aliases)


def test_boundary_prevents_midword_false_positive():
    aliases = derive_brand_aliases("BB Lab Global", "bblab.shop")
    assert not text_mentions_brand("the rubblabar lounge", aliases)  # bblab inside word
    assert not text_mentions_brand("bblabs incorporated", aliases)   # trailing s


def test_no_match_for_unrelated_or_generic_text():
    ordinary = derive_brand_aliases("The Ordinary")
    assert not text_mentions_brand("just an ordinary tuesday", ordinary)
    magnesium = derive_brand_aliases("Magnesium Co")
    assert not text_mentions_brand("take a magnesium supplement nightly", magnesium)


def test_hyphen_and_spacing_variants_match():
    aliases = derive_brand_aliases("BB Lab Global")
    assert text_mentions_brand("the bb-lab serum", aliases)   # hyphen
    assert text_mentions_brand("bblab launch", aliases)       # no space


def test_empty_inputs_are_safe():
    assert not text_mentions_brand("", derive_brand_aliases("BB Lab"))
    assert not text_mentions_brand("bb lab", tuple())
    assert derive_brand_aliases(None) == tuple()
    assert derive_brand_aliases("") == tuple()
