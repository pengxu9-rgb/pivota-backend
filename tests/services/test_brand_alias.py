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


def test_accented_text_matches_brand_either_spelling():
    """The symmetric half: these brands SPELL THEMSELVES WITH THE ACCENTS in the
    copy we match against (grounded answer text, cited source titles, competitor
    names). Folding only the alias side left this direction silently failing, so
    own-brand visibility + attribution under-counted — the same class of bug the
    module was written to fix."""
    for brand in ("Kérastase", "Kerastase"):
        aliases = derive_brand_aliases(brand)
        assert text_mentions_brand("kérastase elixir ultime hair oil review", aliases), brand
    estee = derive_brand_aliases("Estée Lauder")
    assert text_mentions_brand("estée lauder advanced night repair", estee)
    assert text_mentions_brand("estee lauder advanced night repair", estee)
    creme = derive_brand_aliases("Crème de la Mer")
    assert text_mentions_brand("crème de la mer moisturizing cream | sephora", creme)


def test_text_fold_still_only_adds_matches():
    """Design invariant: folding the text side must never REMOVE a match the
    literal compare already made. An accented letter is NOT in the pattern's
    [a-z0-9] boundary class but its folded base letter IS, so the fold can
    destroy a boundary the raw text had — 'bb labé' matches 'bb lab', 'bb labe'
    does not. The raw text is searched too, so the pre-fold match survives."""
    aliases = derive_brand_aliases("BB Lab Global")
    assert text_mentions_brand("bb labé collagen", aliases)   # boundary only in raw text
    assert text_mentions_brand("bb lab collagen", aliases)    # plain ASCII, unchanged


def test_text_fold_does_not_invent_matches():
    """The fold must not manufacture a brand mention out of unrelated copy."""
    aliases = derive_brand_aliases("Kérastase")
    assert not text_mentions_brand("kerastasey conditioner", aliases)  # boundary holds
    assert not text_mentions_brand("kérastasey conditioner", aliases)
    assert not text_mentions_brand("a totally unrelated sentence", aliases)


def test_text_fold_is_canonical_only_no_symbol_to_alnum_false_positives():
    """The TEXT side folds NFD, not NFKD. NFKD maps non-alphanumeric symbols
    INTO [a-z0-9] ('º'→'o', '¹'→'1', 'ﬁ'→'fi'), which would invent brand
    mentions in unrelated copy. That over-credits own-brand visibility AND, where
    a merchant's own aliases filter a rival list, silently drops a real
    competitor — so it must not happen."""
    no7 = derive_brand_aliases("No7")
    assert not text_mentions_brand("el mejor serum nº7 de la lista", no7)  # 'nº7' is "number 7"
    fifty = derive_brand_aliases("Brand 12")
    assert not text_mentions_brand("brand ½ off this week", fifty)  # NFKD would give '1', '2'

    # The fold's job is to make ACCENTED copy behave exactly like the ASCII
    # spelling of the same words — no better, no worse. "The Creme Shop" strips
    # to the generic core 'the creme', which already over-matches dessert copy in
    # plain ASCII on main today; folding must not change that verdict either way.
    creme_shop = derive_brand_aliases("The Creme Shop")
    assert "the creme" in creme_shop
    ascii_verdict = text_mentions_brand("i ordered the creme brulee for dessert", creme_shop)
    accent_verdict = text_mentions_brand("i ordered the crème brûlée for dessert", creme_shop)
    assert accent_verdict == ascii_verdict


def test_accented_brand_matches_next_to_a_footnote_marker():
    """Grounded, citation-bearing answer copy puts superscripts and footnote
    markers right up against a brand name. The marker must stay a word boundary
    (it does under NFD; NFKD would fold '¹'→'1' and glue it into the name,
    killing the match this whole fix exists to make)."""
    kerastase = derive_brand_aliases("Kérastase")
    assert text_mentions_brand("kérastase¹ ranked first for bond repair", kerastase)
    estee = derive_brand_aliases("Estée Lauder")
    assert text_mentions_brand("estée lauder² night repair", estee)


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
