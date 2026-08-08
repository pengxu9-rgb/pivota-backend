"""Tests for services/catalog_identity.py.

These are the contract tests for Stage 1's identity layer. Every Stage
2+ assumption (auto-grouping by content_key, agent_pdp_view keyed on
it, recall layer preferring primary by content_key) depends on these
properties holding stable:

  - same brand + title + gtin → same key, always
  - same brand + title + missing gtin → same key
  - missing brand or title → None (don't create a deceptive key)
  - normalization absorbs cosmetic variation (case, whitespace,
    Inc/LLC, registered marks, diacritics)
  - GTIN is normalized (strip non-digits) so "0773602443796" and
    "773602443796" and "773-602-443796" all collide

Failures here break the architecture roadmap. Be paranoid.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.catalog_identity import (  # noqa: E402
    is_content_key,
    make_content_key,
    normalize_brand,
    normalize_gtin,
    normalize_title,
)


# ---------------------------------------------------------------------------
# normalize_brand
# ---------------------------------------------------------------------------


def test_normalize_brand_lowercases_and_trims() -> None:
    assert normalize_brand("  Glow Recipe  ") == "glow recipe"


def test_normalize_brand_strips_inc_llc_corp_suffixes() -> None:
    """'Glow Recipe', 'Glow Recipe Inc.', 'Glow Recipe LLC' all
    represent the same brand — must produce the same key."""
    assert normalize_brand("Glow Recipe Inc.") == "glow recipe"
    assert normalize_brand("Glow Recipe LLC") == "glow recipe"
    assert normalize_brand("Glow Recipe Corp") == "glow recipe"
    assert normalize_brand("Glow Recipe Co.") == "glow recipe"
    assert normalize_brand("Glow Recipe Company") == "glow recipe"


def test_normalize_brand_strips_registered_and_trademark_marks() -> None:
    """Marks anywhere in the string get removed — both unicode glyphs
    and ASCII forms."""
    assert normalize_brand("Tylenol®") == "tylenol"
    assert normalize_brand("Tide™") == "tide"
    assert normalize_brand("Brand (R)") == "brand"
    assert normalize_brand("Brand (TM)") == "brand"


def test_normalize_brand_empty_or_none_is_empty_string() -> None:
    """Empty inputs return '' — caller decides whether to use the key
    at all. make_content_key returns None in this case."""
    assert normalize_brand(None) == ""
    assert normalize_brand("") == ""
    assert normalize_brand("   ") == ""


def test_normalize_brand_handles_non_string() -> None:
    """Defensive — don't crash on int/dict."""
    assert normalize_brand(123) == ""
    assert normalize_brand({"x": 1}) == ""


# ---------------------------------------------------------------------------
# normalize_title
# ---------------------------------------------------------------------------


def test_normalize_title_lowercases() -> None:
    assert normalize_title("Plum Plump Hyaluronic Serum") == "plum plump hyaluronic serum"


def test_normalize_title_strips_diacritics() -> None:
    """'Crème' and 'Creme' are the same product surfaced with different
    encoding. NFKD normalize + drop combining marks unifies them."""
    assert normalize_title("Crème de la Mer") == normalize_title("Creme de la Mer")


def test_normalize_title_keeps_hyphens_drops_other_punctuation() -> None:
    """Hyphens are identity-bearing ('Anti-Aging' is a distinct phrase).
    Apostrophes / commas / parens are cosmetic — strip."""
    out = normalize_title("Anti-Aging Cream, Original (NEW!)")
    assert "anti-aging" in out  # hyphen kept
    assert "," not in out
    assert "(" not in out
    assert "!" not in out


def test_normalize_title_keeps_digits_and_units() -> None:
    """Size IS identity — 30ml vs 50ml are different products. Don't
    strip numbers or unit letters."""
    out = normalize_title("Lipstick 3.5g")
    assert "3" in out and "5" in out and "g" in out


def test_normalize_title_collapses_internal_whitespace() -> None:
    assert normalize_title("foo   bar  baz") == "foo bar baz"


def test_normalize_title_empty_or_none_is_empty() -> None:
    assert normalize_title(None) == ""
    assert normalize_title("") == ""
    assert normalize_title("   ") == ""


# ---------------------------------------------------------------------------
# normalize_gtin
# ---------------------------------------------------------------------------


def test_normalize_gtin_canonicalizes_to_14_digit_form() -> None:
    """GS1 canonical form is GTIN-14 — UPC-A (12), EAN-13 (13), and
    GTIN-14 all left-pad to 14 digits with leading zeros. Separators
    stripped. After normalize, the same physical product's GTIN
    matches regardless of which form the source data uses."""
    # UPC-A (12-digit) → GTIN-14
    assert normalize_gtin("773602443796") == "00773602443796"
    # GTIN-13 (13-digit) → GTIN-14
    assert normalize_gtin("0773602443796") == "00773602443796"
    # GTIN-14 (14-digit) → identity
    assert normalize_gtin("00773602443796") == "00773602443796"
    # Separators stripped, then padded
    assert normalize_gtin("773-602-443796") == "00773602443796"
    assert normalize_gtin("  773 602 443796  ") == "00773602443796"


def test_normalize_gtin_passes_through_malformed_long_codes() -> None:
    """15+ digit input is malformed (no standard GTIN is that long).
    Pass through rather than truncate so we don't silently merge
    unrelated codes."""
    assert normalize_gtin("1" * 16) == "1" * 16


def test_normalize_gtin_empty_or_none_is_empty() -> None:
    assert normalize_gtin(None) == ""
    assert normalize_gtin("") == ""


# ---------------------------------------------------------------------------
# make_content_key — the load-bearing function
# ---------------------------------------------------------------------------


def test_make_content_key_returns_none_on_empty_brand() -> None:
    """Empty brand → None. We don't want every brand-less seed to
    collide on the same key — that would defeat the whole purpose."""
    assert make_content_key("", "title", "gtin") is None
    assert make_content_key(None, "title", "gtin") is None
    assert make_content_key("   ", "title", "gtin") is None


def test_make_content_key_returns_none_on_empty_title() -> None:
    """Same logic — empty title means no identity signal."""
    assert make_content_key("brand", "", "gtin") is None
    assert make_content_key("brand", None, "gtin") is None


def test_make_content_key_returns_none_when_only_gtin_present() -> None:
    """Brand + title are both required. GTIN alone isn't enough
    because the same GTIN under different brand/title would be a data
    quality bug we'd want to surface, not silently dedup."""
    assert make_content_key("", "", "0773602443796") is None


def test_make_content_key_format_is_ck_prefix_plus_32_hex() -> None:
    """Pin the wire format — Stage 3 routes will dispatch on this
    prefix shape."""
    key = make_content_key("Brand", "Title")
    assert key is not None
    assert key.startswith("ck_")
    assert len(key) == 35  # 'ck_' + 32 hex
    assert all(c in "0123456789abcdef" for c in key[3:])


def test_make_content_key_is_deterministic() -> None:
    """Same inputs → same key, always. The whole architecture depends
    on this."""
    k1 = make_content_key("Glow Recipe", "Plum Plump Serum", "0123456789")
    k2 = make_content_key("Glow Recipe", "Plum Plump Serum", "0123456789")
    assert k1 == k2


def test_make_content_key_normalizes_inputs() -> None:
    """Cosmetic variation in brand/title doesn't change the key. This
    is what makes Path A (Shopify) and Path B (external_seed) produce
    the same key for the same physical product."""
    a = make_content_key("Glow Recipe", "Plum Plump Hyaluronic Serum")
    b = make_content_key("Glow Recipe Inc.", "PLUM PLUMP HYALURONIC SERUM")
    c = make_content_key("  glow recipe  ", "Plum  Plump  Hyaluronic  Serum")
    assert a == b == c


def test_make_content_key_with_and_without_gtin_differ() -> None:
    """The presence of a GTIN tightens identity. Same brand + title
    with GTIN should be a DIFFERENT key from without GTIN — otherwise
    we'd silently dedup pre-GTIN catalog rows with post-GTIN ones,
    which is a real-world data integrity risk."""
    without = make_content_key("Brand", "Title")
    with_gtin = make_content_key("Brand", "Title", "0123456789")
    assert without != with_gtin


def test_make_content_key_different_gtins_produce_different_keys() -> None:
    """Two products with the same brand + title but different GTINs
    are different products (e.g. shade variants with their own GTINs)."""
    k1 = make_content_key("MAC", "Lipstick", "0000000001")
    k2 = make_content_key("MAC", "Lipstick", "0000000002")
    assert k1 != k2


def test_make_content_key_gtin_normalization_collapses_separators() -> None:
    """Hyphens / spaces in GTIN shouldn't break dedup."""
    k1 = make_content_key("MAC", "Lipstick", "0773602443796")
    k2 = make_content_key("MAC", "Lipstick", "773-602-443796")
    k3 = make_content_key("MAC", "Lipstick", "0773 602 443796")
    assert k1 == k2 == k3


def test_make_content_key_different_brand_same_title_differs() -> None:
    """A product titled the same way under different brands isn't the
    same physical product."""
    a = make_content_key("Brand A", "Lipstick")
    b = make_content_key("Brand B", "Lipstick")
    assert a != b


def test_make_content_key_different_title_same_brand_differs() -> None:
    """Shade / size variants distinguished by title MUST get different
    keys. Otherwise auto-grouping would over-merge."""
    a = make_content_key("MAC", "Lipstick Russian Red")
    b = make_content_key("MAC", "Lipstick Velvet Teddy")
    assert a != b


# ---------------------------------------------------------------------------
# is_content_key
# ---------------------------------------------------------------------------


def test_is_content_key_recognizes_valid_keys() -> None:
    """The Stage 3 endpoint needs to dispatch on id format. Pin the
    recognition predicate so it can't drift."""
    key = make_content_key("Brand", "Title")
    assert is_content_key(key) is True


def test_is_content_key_rejects_other_id_formats() -> None:
    """sig_*, ext_*, raw IDs, and garbage all return False."""
    assert is_content_key("sig_abc123") is False
    assert is_content_key("ext_abc123") is False
    assert is_content_key("12345678") is False
    assert is_content_key("") is False
    assert is_content_key(None) is False
    assert is_content_key(123) is False
    # Right prefix, wrong length
    assert is_content_key("ck_short") is False
    # Right prefix + length, non-hex
    assert is_content_key("ck_" + "Z" * 32) is False


# ---------------------------------------------------------------------------
# Cross-language conformance corpus (issue #1694)
#
# The tests above pin behaviour one property at a time. These pin the exact
# OUTPUT, case by case, and are mirrored byte-for-byte into PIVOTA-Agent, whose
# Node port of this module reads the same file
# (tests/fixtures/content_key_v1_cases.json there,
#  tests/content_key_authority.test.js asserts it).
#
# The point is that neither side can move alone. This module is the authority,
# so the corpus is generated here — scripts/generate_content_key_conformance_corpus.py
# computes every key by calling make_content_key, never by hardcoding one. A
# change to the formula therefore fails HERE first, at the authority, instead of
# downstream or, as in 2026-05, nowhere at all: PIVOTA-Agent minted under three
# rival formulas for two months without a single test going red, because nothing
# connected the two repos. See pengxu9-rgb/PIVOTA-Agent#1916.
#
# If one of these fails, do NOT edit the JSON. Either the change was unintended,
# or it was intended and the corpus needs regenerating — in which case the diff
# is the deliverable, because it names exactly which keys move and therefore
# which stored rows stop being reproducible.
# ---------------------------------------------------------------------------

import json  # noqa: E402

import pytest  # noqa: E402

_CORPUS_PATH = Path(__file__).parent / "fixtures" / "content_key_v1_cases.json"
_CORPUS = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


def test_conformance_corpus_covers_rules_and_production_rows() -> None:
    """Both halves must stay populated. Synthetic cases test the rules we
    thought of; production rows test the ones we did not."""
    rules = [c for c in _CORPUS if c["source"] == "python_authority"]
    prod = [c for c in _CORPUS if c["source"] == "prod_catalog_products"]
    assert len(rules) >= 20
    assert len(prod) >= 8
    assert len({c["name"] for c in _CORPUS}) == len(_CORPUS), "duplicate case names"


@pytest.mark.parametrize("case", _CORPUS, ids=[c["name"] for c in _CORPUS])
def test_conformance_corpus_case(case: dict) -> None:
    """Every case recomputed from the authority. A hand-edited key fails here."""
    assert make_content_key(case["brand"], case["title"], case["gtin"]) == case["content_key"]


def _load_generator():
    """Load the generator by file path.

    Deliberately NOT `sys.path.insert(0, "scripts")`: scripts/ holds ~200 modules
    with short, generic names, and putting it at the front of sys.path lets any
    of them shadow a stdlib or first-party import for every test that runs after
    this one in the same process. That is an order-dependent failure in an
    unrelated suite — exactly the kind of thing a conformance test has no
    business causing.
    """
    import importlib.util

    generator_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "generate_content_key_conformance_corpus.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_content_key_corpus_generator", generator_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_conformance_corpus_is_not_stale() -> None:
    """The file on disk is exactly what the generator produces today. Catches a
    formula change that was made without regenerating, and a corpus edited by
    hand — both of which would otherwise leave a weakened gate looking green."""
    generator = _load_generator()
    build_corpus, render = generator.build_corpus, generator.render

    assert _CORPUS_PATH.read_text(encoding="utf-8") == render(build_corpus()), (
        "content_key_v1_cases.json is stale — run "
        "python3 scripts/generate_content_key_conformance_corpus.py, read the diff, "
        "then mirror the file into PIVOTA-Agent."
    )


def test_production_rows_reproduce_their_stored_keys() -> None:
    """The 8 prod cases carry keys minted by this module and read back off
    catalog_products on 2026-08-07. Recomputing them here is what makes the
    corpus evidence about the live corpus rather than about itself."""
    prod = [c for c in _CORPUS if c["source"] == "prod_catalog_products"]
    for case in prod:
        recomputed = make_content_key(case["brand"], case["title"], case["gtin"])
        assert recomputed == case["content_key"], case["name"]
        assert is_content_key(recomputed), case["name"]


# ---------------------------------------------------------------------------
# The mirror, enforced (issue #1694 review)
#
# Both repos run their own suite against their own copy of this corpus, and each
# validates it against its own implementation. Nothing compared the two FILES —
# the coupling was a bullet in a docstring saying "mirror it into PIVOTA-Agent".
# A regeneration here that nobody mirrors therefore left that repo green on a
# stale corpus indefinitely: both sides passing, silently forked, exactly the
# 2026-05 incident this pair exists to prevent.
#
# The digest below is committed in BOTH repos. Regenerating the corpus breaks
# this assertion here and the matching one in
# PIVOTA-Agent/tests/content_key_authority.test.js, so the mirror step stops
# being something to remember and becomes something CI requires. It also
# subsumes the name-by-name checks over there: a corpus that drops cases fails
# the digest whether or not the surviving names look right.
#
# When you legitimately change the corpus: regenerate, read the diff, update
# this constant AND the one in PIVOTA-Agent, and land both together.
# ---------------------------------------------------------------------------

CORPUS_SHA256 = "3ba657415f44d3d2b2a87300d198a9510c9eff0881eba5d27406f853061dae30"


def test_corpus_digest_is_pinned_in_both_repos() -> None:
    import hashlib

    digest = hashlib.sha256(_CORPUS_PATH.read_bytes()).hexdigest()
    assert digest == CORPUS_SHA256, (
        "the conformance corpus changed. Update CORPUS_SHA256 here AND the matching "
        "constant in PIVOTA-Agent/tests/content_key_authority.test.js, mirror the file, "
        "and land both together."
    )
