"""cross_merchant_same_size (M4): `slug_generation_mismatch` -- a pair whose titles match but whose
canonical_url slugs name two generations of the product is HELD (never vetoed, never proposed).

Fixtures are real pairs from the 2026-10-17 US dry run (brands ohui + etude, 80 audit pairs, all T0c)
that a Gemini side-by-side review judged: titles, prices and URLs as the audit recorded them."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Tuple
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.identity_cross_merchant_match import (  # noqa: E402
    HELD_REASONS,
    RULE_VERSION,
    VETOES_CHECKED,
    Planner,
    slug_generation,
    slug_generation_differs,
    slug_segment_words,
    url_slug_segments,
)
from test_identity_cross_merchant_match import Cat, brand_store, outcome, proposed_keys, retailer  # noqa: E402

Side = Tuple[str, str, float, str]  # brand, title, price, canonical_url


def _pair(listing: Side, anchor: Side) -> Tuple[Cat, str, str]:
    """The anchor is the OLDER retailer listing (anchor_reason oldest_listing, as in the dry run)."""
    cat = Cat()
    keys = []
    for n, (brand, title, price, url), created in ((1, anchor, "2026-01-01"), (2, listing, "2026-06-01")):
        key = retailer(cat, n, brand, title, price=price, host=urlsplit(url).hostname, created=created)
        next(p for p in cat.products if p["product_key"] == key)["canonical_url"] = url
        keys.append(key)
    return cat, keys[1], keys[0]


def _verdict(listing: Side, anchor: Side):
    cat, li_key, an_key = _pair(listing, anchor)
    planner = Planner(cat.snapshot(), "US")
    return planner.judge(planner.by_pk[li_key], planner.by_pk[an_key]), cat, li_key


# --- the generations Gemini found, and the slugs that carry them: HELD -------------------------------------

HELD_PAIRS = {
    # Gemini "different": the 2019 "Etude House" jar vs the 2021 ETUDE renewal.
    "etude_collagen_eye_cream_2019_brand_new_version": (
        ("ETUDE", "[EtudeHouse] Moistfull Collagen Eye Cream 28ml", 17.6,
         "https://luxiface.com/products/etudehouse-moistfull-collagen-eye-cream-28ml"),
        ("ETUDE", "ETUDE Moistfull Collagen Eye Cream 28ml", 22.76,
         "https://dodoskin.com/products/etudehouse-moistfull-collagen-eye-cream-28ml-2019-brand-new-version")),
    # Gemini "different": the same renewal on the toner.
    "etude_collagen_toner_2019_new_version": (
        ("ETUDE", "[EtudeHouse] Moistfull Collagen Facial Toner 200ml", 17.6,
         "https://luxiface.com/products/etudehouse-moistfull-collagen-facial-toner-200ml"),
        ("ETUDE", "ETUDE Moistfull Collagen Facial Toner 200ml", 22.76,
         "https://dodoskin.com/products/etudehouse-moistfull-collagen-facial-toner-200ml-2019-new-version")),
    # Gemini "different": the title was renamed "De-aging", the slug (and image) are still the older line.
    "ohui_prime_advancer_ampoule_serum_slug_without_de_aging": (
        ("O HUI", "[Ohui] Prime Advancer De-aging Ampoule Serum 50ml", 106.7,
         "https://luxiface.com/products/ohui-prime-advancer-ampoule-serum-50ml"),
        ("O HUI", "O HUI Prime Advancer De-aging Ampoule Serum 50ml", 101.4,
         "https://nanasbeautyholic.com/products/o-hui-prime-advancer-de-aging-ampoule-serum-50ml")),
    # Gemini "same", concern "Listing B URL slug contains 'renewal'": a slug renewal is held for a human.
    "ohui_cleansing_foam_renewal_slug": (
        ("O HUI", "O HUI Miracle Moisture Cleansing Foam 200ml", 32.2,
         "https://kbeautymakeup.com/products/o-hui-miracle-moisture-cleansing-foam-200ml"),
        ("O HUI", "OHUI Miracle Moisture Cleansing Foam 200ml", 25.97,
         "https://dodoskin.com/products/ohui-miracle-moisture-cleansing-foam-200ml-renewal")),
    # A "-2" de-dup suffix is noise; the other slug's "2019-new" is not.
    "etude_collagen_cream_dedup_suffix_vs_2019_new": (
        ("ETUDE", "[EtudeHouse] Moistfull Collagen Cream 75ml", 19.99,
         "https://luxiface.com/products/etudehouse-moistfull-collagen-cream-75ml-2"),
        ("ETUDE", "ETUDE Moistfull Collagen Cream 75ml", 15.24,
         "https://dodoskin.com/products/etudehouse-moistfull-collagen-cream-75ml-2019-new")),
}


@pytest.mark.parametrize("name", sorted(HELD_PAIRS))
def test_a_pair_whose_slugs_name_two_generations_is_held(name):
    listing, anchor = HELD_PAIRS[name]
    v, cat, li_key = _verdict(listing, anchor)
    assert (v.status, v.reasons, v.tiers_matched[-1:]) == ("held", ["slug_generation_mismatch"], ["T0c"])
    plan = cat.plan()
    assert plan.proposals == []
    assert outcome(plan, li_key)["status"] == "held"
    assert outcome(plan, li_key)["reason"] == "slug_generation_mismatch"


def test_the_hold_is_symmetric_whichever_side_is_the_anchor():
    listing, anchor = HELD_PAIRS["etude_collagen_eye_cream_2019_brand_new_version"]
    v, _cat, _k = _verdict(anchor, listing)
    assert (v.status, v.reasons) == ("held", ["slug_generation_mismatch"])


# --- the clean pairs of the same audit: slugs agree, or carry no generation word -> still PROPOSE --------

PROPOSED_PAIRS = {
    "etude_sebum_soak_pact_brand_prefix_noise": (
        ("ETUDE", "[Etudehouse] Sebum soak Pact 9.5g", 10.8,
         "https://luxiface.com/products/etudehouse-sebum-soak-pact-9-5g"),
        ("ETUDE", "ETUDE Sebum Soak Pact 9.5g", 10.81,
         "https://dodoskin.com/products/etude-sebum-soak-pact-9-5g")),
    "ohui_prime_advancer_skin_softener_size_suffix_noise": (
        ("O HUI", "O HUI PRIME ADVANCER skin softener 150ml", 76,
         "https://kbeautymakeup.com/products/o-hui-prime-advancer-skin-softener-150ml"),
        ("O HUI", "[OHUI] Prime Advancer Skin Softener (150ml/5.07fl.oz)", 67,
         "https://shoppalacebeauty.com/products/ohui-prime-advancer-skin-softener-150ml-5-07fl-oz")),
    "ohui_cleansing_oil_marketing_words_in_slug": (
        ("O HUI", "O HUI Miracle Moisture Cleansing Oil 150ml", 34.6,
         "https://kbeautymakeup.com/products/o-hui-miracle-moisture-cleansing-oil-150ml"),
        ("O HUI", "OHUI Miracle Moisture Cleansing Oil 150ml", 24.74,
         "https://dodoskin.com/products/ohui-miracle-moisture-cleansing-oil-removes-makeup-without-irritation-150ml")),
    "ohui_age_recovery_skin_softener_dash_1_dedup": (
        ("O HUI", "O HUI Age Recovery Skin Softener 150ml", 49.4,
         "https://nanasbeautyholic.com/products/o-hui-age-recovery-skin-softener-150ml-1"),
        ("O HUI", "[OHUI] Age Recovery Skin Softener (150Ml/5.0fl.oz)", 57,
         "https://shoppalacebeauty.com/products/ohui-age-recovery-skin-softener-150ml-5-0fl-oz")),
    "ohui_geniture_on_both_slugs_korean_copy_suffix": (
        ("O HUI", "O Hui The First Geniture Sym Micro Essence 90ml", 255,
         "https://coscorea.com/products/o-hui-the-first-geniture-sym-micro-essence-90ml-%EC%82%AC%EB%B3%B8"),
        ("O HUI", "[OHUI] The First Geniture Sym-Micro Essence (90ml/3.0fl.oz)", 215,
         "https://shoppalacebeauty.com/products/ohui-the-first-geniture-sym-micro-essence-90ml-3-0fl-oz")),
    "ohui_ceramide_cream_two_sizes_in_one_slug": (
        ("O HUI", "Ohui Miracle Moisture Ceramide Boosting Cream 50ml", 56,
         "https://koolseoul.com/products/ohui-miracle-moisture-ceramide-boosting-cream-50ml-60ml"),
        ("O HUI", "[O HUI] Miracle Moisture Ceramide Boosting Cream 50ml", 54.5,
         "https://holiholic.com/products/o-hui-miracle-moisture-ceramide-boosting-cream-50ml")),
    # NOT caught -- Gemini "different" from the IMAGES only (renamed "Pink Barrier Essence" bottle); both
    # slugs and titles agree, so text cannot see it. Pinned so the limit is explicit.
    "ohui_miracle_moisture_essence_image_only_miss": (
        ("O HUI", "[Ohui] Miracle Moisture Essence 50ml", 48.6,
         "https://luxiface.com/products/ohui-miracle-moisture-essence-50ml"),
        ("O HUI", "O HUI MIRACLE MOISTURE ESSENCE 50ml", 75,
         "https://kbeautymakeup.com/products/o-hui-miracle-moisture-essence-50ml")),
    # NOT caught -- Gemini "different" (De-aging vs Phyto Vital) from the images; no slug names either line.
    "ohui_prime_advancer_skin_softener_image_only_miss": (
        ("O HUI", "OHUI Prime Advancer Skin Softener 150ml", 50.48,
         "https://dodoskin.com/products/ohui-prime-advancer-skin-softener-high-moistureizing-150ml"),
        ("O HUI", "[OHUI] Prime Advancer Skin Softener (150ml/5.07fl.oz)", 67,
         "https://shoppalacebeauty.com/products/ohui-prime-advancer-skin-softener-150ml-5-07fl-oz")),
}


@pytest.mark.parametrize("name", sorted(PROPOSED_PAIRS))
def test_a_pair_whose_slugs_agree_or_carry_no_generation_word_still_proposes(name):
    listing, anchor = PROPOSED_PAIRS[name]
    v, cat, li_key = _verdict(listing, anchor)
    assert v.status == "pass", v.reasons
    assert proposed_keys(cat.plan()) == [li_key]


# --- the slug reader -------------------------------------------------------------------------------------

@pytest.mark.parametrize("url, segments", [
    ("https://dodoskin.com/products/ohui-miracle-moisture-cleansing-foam-200ml-renewal",
     ["ohui miracle moisture cleansing foam 200ml renewal"]),
    ("https://x.example/products/etude-soon-jung-cream/?variant=123#reviews", ["etude soon jung cream"]),
    ("https://x.example/shop/etude_soon_jung_cream.html", ["shop", "etude soon jung cream"]),
    ("https://x.example/p/o-hui-essence-%EB%A6%AC%EB%89%B4%EC%96%BC", ["o hui essence 리뉴얼"]),
    # Every segment that can name the product, not only the last: Amazon, an id after the name, YesStyle.
    ("https://www.amazon.com/ETUDE-Moistfull-Collagen-Eye-Cream-2019-New-Version/dp/B07ABCDEF1",
     ["ETUDE Moistfull Collagen Eye Cream 2019 New Version"]),
    ("https://s.example/product/ohui-foam-2019-new/12345/", ["ohui foam 2019 new"]),
    ("https://www.yesstyle.com/en/etude-collagen-cream-2019-new/info.html/pid.1234",
     ["en", "etude collagen cream 2019 new"]),
    ("https://s.example/goods/item-77/sku_9/id-12/detail.jsp", []),
    ("https://ohui.co.kr/product/view.do?prdCd=123", []),
    # A listing page's handle is no edition: "/collections/new-arrivals/..." is not "new".
    ("https://s.example/collections/new-arrivals/products/etude-collagen-cream", ["etude collagen cream"]),
    ("https://s.example/category/2019-new/etude-collagen-cream", ["etude collagen cream"]),
    ("https://s.example/products/리뉴얼-오휘-에센스", ["리뉴얼 오휘 에센스"]),
    ("", []), (None, []), ("https://x.example/", []),
])
def test_url_slug_segments_reads_every_segment_that_can_name_the_product(url, segments):
    assert url_slug_segments(url) == segments


def test_a_segment_that_does_not_name_the_product_is_not_read():
    title = "ETUDE Moistfull Collagen Cream 75ml"
    gen = slug_generation("https://s.example/new/2019-renewal/etude-moistfull-collagen-cream-75ml", "ETUDE", title)
    assert gen.words == frozenset() and gen.readable


OHUI_TITLE = "O HUI Prime Advancer De-aging Ampoule Serum 50ml"


def _gen(slug: str, brand: str = "O HUI", title: str = OHUI_TITLE):
    return slug_generation(f"https://shop.example/products/{slug}", brand, title)


@pytest.mark.parametrize("slug", [
    "o-hui-prime-advancer-de-aging-ampoule-serum-50ml",            # the plain slug
    "ohui-prime-advancer-de-aging-ampoule-serum-50ml-1-69fl-oz",    # size suffix
    "o-hui-prime-advancer-de-aging-ampoule-serum-50ml-1",           # de-dup -1
    "o-hui-prime-advancer-de-aging-ampoule-serum-50ml-2",           # de-dup -2
    "o-hui-prime-advancer-de-aging-ampoule-serum-50ml-copy",        # copy
    "o-hui-prime-advancer-de-aging-ampoule-serum-50ml-사본",         # Korean "copy"
    "prime-advancer-de-aging-ampoule-serum-50ml-8012345678",        # product id, no brand prefix
    "o-hui-prime-advancer-de-aging-ampoule-serum-50ml-20",          # a short number is no year
    "o-hui-prime-advancer-de-aging-ampoule-serum-50ml-korean-cosmetics-beauty",
])
def test_retailer_slug_noise_is_never_a_generation(slug):
    base = _gen("o-hui-prime-advancer-de-aging-ampoule-serum-50ml")
    assert base.words == frozenset() and base.lines == {"de_aging"} and base.readable
    assert not slug_generation_differs(_gen(slug), base)


@pytest.mark.parametrize("slug, words", [
    ("ohui-cleansing-foam-200ml-2019-new", {"year_2019", "new"}),
    ("o-hui-cleansing-foam-200ml-2019-brand-new-version", {"year_2019", "new"}),
    ("ohui-cleansing-foam-200ml-renewal", {"renewal"}),
    ("ohui-cleansing-foam-200ml-renewed", {"renewal"}),
    ("ohui-cleansing-foam-200ml-%EB%A6%AC%EB%89%B4%EC%96%BC", {"renewal"}),
    ("ohui-cleansing-foam-new-formula-200ml", {"new_formula"}),
    ("ohui-cleansing-foam-reformulated-200ml", {"reformulated"}),
    ("ohui-cleansing-foam-version-2-200ml", {"v2"}),
    ("ohui-cleansing-foam-2nd-gen-200ml", {"gen2"}),
    ("ohui-cleansing-foam-limited-edition-200ml", {"limited_edition"}),
    ("ohui-cleansing-foam-holiday-edition-200ml", {"holiday_edition"}),
    ("ohui-cleansing-foam-2023-200ml", {"year_2023"}),
    # round 2: slug spellings the title lexicon lacks, and glued forms
    ("ohui-cleansing-foam-200ml-2021ver", {"year_2021"}),
    ("ohui-cleansing-foam-200ml-21ver", {"year_2021"}),
    ("ohui-cleansing-foam-200ml-ver2021", {"year_2021"}),
    ("ohui-cleansing-foam-200ml-ver-2021", {"year_2021"}),
    ("ohui-cleansing-foam-200ml-2021-ver", {"year_2021"}),
    ("ohui-cleansing-foam-200ml-newver", {"new"}),
    ("ohui-cleansing-foam-200ml-new2021", {"new", "year_2021"}),
    ("ohui-cleansing-foam-200ml-2021new", {"new", "year_2021"}),
    ("ohui-cleansing-foam-200ml-renewal2021", {"renewal", "year_2021"}),
    ("ohui-cleansing-foam-200ml-renewed2023", {"renewal", "year_2023"}),
    ("ohui-cleansing-foam-200ml-newest2021", {"new", "year_2021"}),
    ("ohui-cleansing-foam-200ml-renew", {"renewal"}),
    ("ohui-cleansing-foam-200ml-relaunch", {"renewal"}),
    ("ohui-cleansing-foam-200ml-relaunched", {"renewal"}),
    ("ohui-cleansing-foam-200ml-upgraded", {"upgraded"}),
    ("ohui-cleansing-foam-200ml-upgrade", {"upgraded"}),
    ("ohui-cleansing-foam-200ml-newest-version", {"new"}),
    ("ohui-cleansing-foam-200ml-2nd-edition", {"edition_2"}),
    ("ohui-cleansing-foam-200ml-3rd-edition", {"edition_3"}),
    ("ohui-cleansing-foam-200ml-%EC%8B%A0%ED%98%95", {"new"}),
])
def test_generation_words_read_from_a_slug(slug, words):
    assert _gen(slug, title="OHUI Miracle Moisture Cleansing Foam 200ml").words == words


@pytest.mark.parametrize("slug", [
    "ohui-cleansing-foam-plus-200ml", "ohui-cleansing-foam-advanced-200ml", "ohui-cleansing-foam-ex-200ml",
    "ohui-cleansing-foam-intense-200ml", "ohui-cleansing-foam-rx-200ml", "ohui-cleansing-foam-refill-200ml",
    "ohui-cleansing-foam-2009-200ml", "ohui-cleansing-foam-2040-200ml", "ohui-cleansing-foam-20190-200ml",
    "ohui-cleansing-foam-12019-200ml", "ohui-cleansing-foam-sku2019-200ml",
    "ohui-cleansing-foam-new-york-200ml",
    # no word inside a word, glued or not
    "ohui-cleansing-foam-renewalist-200ml", "ohui-cleansing-foam-newborn-200ml", "ohui-cleansing-foam-renewing-200ml",
    "ohui-cleansing-foam-newyork2021x-200ml", "ohui-cleansing-foam-50ver-200ml", "ohui-cleansing-foam-121ver-200ml",
    "ohui-cleansing-foam-2021verb-200ml", "ohui-cleansing-foam-newsletter-200ml", "ohui-cleansing-foam-upgrades-200ml",
    "ohui-cleansing-foam-2nd-200ml", "ohui-cleansing-foam-sinew2021-200ml", "ohui-cleansing-foam-xrenewal2021-200ml",
    "ohui-cleansing-foam-x21ver-200ml",
])
def test_line_words_flags_and_non_year_numbers_are_not_slug_generation_words(slug):
    assert _gen(slug, title="OHUI Miracle Moisture Cleansing Foam 200ml").words == frozenset()


def test_ver_year_and_year_ver_read_as_the_same_words():
    assert slug_segment_words("foam ver 2021") == slug_segment_words("foam 2021 ver") == {"year_2021"}
    assert slug_segment_words("foam ver 2") == {"v2"}
    a = _gen("ohui-miracle-moisture-cleansing-foam-200ml-ver-2021", title="OHUI Miracle Moisture Cleansing Foam 200ml")
    b = _gen("ohui-miracle-moisture-cleansing-foam-200ml-2021-ver", title="OHUI Miracle Moisture Cleansing Foam 200ml")
    assert not slug_generation_differs(a, b)


def test_two_different_years_disagree_and_the_same_renewal_agrees():
    title = "ETUDE Moistfull Collagen Cream 75ml"
    y2019 = _gen("etude-moistfull-collagen-cream-75ml-2019", "ETUDE", title)
    y2021 = _gen("etude-moistfull-collagen-cream-75ml-2021", "ETUDE", title)
    assert slug_generation_differs(y2019, y2021)
    a = _gen("ohui-foam-200ml-renewal", title="OHUI Miracle Moisture Cleansing Foam 200ml")
    b = _gen("o-hui-miracle-moisture-cleansing-foam-renewal-200ml", title="O HUI Miracle Moisture Cleansing Foam 200ml")
    assert not slug_generation_differs(a, b)


def test_a_row_with_no_url_does_not_prove_the_same_generation_as_a_renewal_slug():
    none = slug_generation(None, "O HUI", "OHUI Miracle Moisture Cleansing Foam 200ml")
    renewal = _gen("ohui-miracle-moisture-cleansing-foam-200ml-renewal",
                   title="OHUI Miracle Moisture Cleansing Foam 200ml")
    assert slug_generation_differs(none, renewal) and slug_generation_differs(renewal, none)
    assert not slug_generation_differs(none, slug_generation("", "O HUI", "x"))


def test_named_lines_compare_only_between_two_readable_slugs():
    named = _gen("o-hui-prime-advancer-de-aging-ampoule-serum-50ml")
    id_only = _gen("8012345678")
    assert not id_only.readable and id_only.lines == frozenset()
    assert not slug_generation_differs(named, id_only) and not slug_generation_differs(id_only, named)
    stale = _gen("ohui-prime-advancer-ampoule-serum-50ml")
    assert stale.readable and slug_generation_differs(stale, named)


@pytest.mark.parametrize("slug, line", [
    ("ohui-phyto-vital-prime-advancer-ampoule-serum-50ml", "phyto_vital"),
    ("ohui-phytovital-prime-advancer-ampoule-serum-50ml", "phyto_vital"),
    ("ohui-miracle-moisture-pink-barrier-essence-50ml", "pink_barrier"),
    ("ohui-the-first-geniture-eye-cream-25ml", "geniture"),
])
def test_ohui_named_lines(slug, line):
    title = "O HUI Prime Advancer Ampoule Serum Miracle Moisture Essence The First Eye Cream 50ml"
    assert _gen(slug, title=title).lines == {line}


@pytest.mark.parametrize("brand", ["오휘", "LG O HUI", "LG생활건강 오휘"])
def test_named_lines_run_for_every_spelling_of_the_brand(brand):
    title = "Prime Advancer De-aging Ampoule Serum 50ml"
    stale = _gen("ohui-prime-advancer-ampoule-serum-50ml", brand, title)
    named = _gen("o-hui-prime-advancer-de-aging-ampoule-serum-50ml", brand, title)
    assert named.lines == {"de_aging"} and slug_generation_differs(stale, named)


def test_named_lines_are_scoped_to_their_brand():
    title = "Brand X De-aging Ampoule Serum 50ml"
    with_line = slug_generation("https://s.example/p/brandx-de-aging-ampoule-serum-50ml", "Brand X", title)
    without = slug_generation("https://s.example/p/brandx-ampoule-serum-50ml", "Brand X", title)
    assert with_line.lines == frozenset() and not slug_generation_differs(with_line, without)


def test_a_brand_word_in_the_slug_is_read_with_the_brand_removed():
    # A brand whose own name is a generation word: "New Avon" -- its "new" is the brand, not an edition.
    gen = slug_generation("https://s.example/p/new-avon-skin-so-soft-oil-150ml", "New Avon", "New Avon Skin So Soft Oil 150ml")
    assert gen.words == frozenset()


# --- registered like every other check ---------------------------------------------------------------------

def test_the_reason_is_a_held_reason_and_the_check_is_in_the_evidence():
    assert "slug_generation_mismatch" in HELD_REASONS and "slug_generation" in VETOES_CHECKED
    listing, anchor = PROPOSED_PAIRS["etude_sebum_soak_pact_brand_prefix_noise"]
    cat, _li, _an = _pair(listing, anchor)
    (p,) = cat.plan().proposals
    assert "slug_generation" in p["evidence"]["vetoes_checked"]


def _set_url(cat: Cat, key: str, url: str) -> None:
    next(p for p in cat.products if p["product_key"] == key)["canonical_url"] = url


def test_a_title_veto_still_wins_over_the_slug_hold():
    cat = Cat()
    brand_store(cat, "ext:etude-collagen::a1", "ETUDE", "Moistfull Collagen Cream 75ml")
    li = retailer(cat, 3, "ETUDE", "Moistfull Collagen Cream 75ml Refill")
    _set_url(cat, li, "https://dodoskin.com/products/etude-moistfull-collagen-cream-75ml-refill-2019-new")
    o = outcome(cat.plan(), li)
    assert o["status"] == "vetoed" and "conflict:refill" in o["reasons"]


def test_a_veto_inside_the_judgement_still_wins_over_the_slug_hold():
    """Differing valid GTINs are vetoed in judge() itself: a vetoed pair is a different item, never
    downgraded to a hold by its slug."""
    cat = Cat()
    an = brand_store(cat, "ext:etude-collagen::a1", "ETUDE", "Moistfull Collagen Cream 75ml", gtin="8809643066389")
    li = retailer(cat, 3, "ETUDE", "Moistfull Collagen Cream 75ml", gtin="8801042593473")
    _set_url(cat, li, "https://dodoskin.com/products/etude-moistfull-collagen-cream-75ml-2019-new")
    planner = Planner(cat.snapshot(), "US")
    v = planner.judge(planner.by_pk[li], planner.by_pk[an])
    assert v.status == "vetoed" and v.reasons == ["gtin_differs"]


def test_the_rule_version_is_v4_and_the_evidence_shows_each_rows_url():
    assert RULE_VERSION == "cmss.v4"
    listing, anchor = PROPOSED_PAIRS["etude_sebum_soak_pact_brand_prefix_noise"]
    cat, _li, _an = _pair(listing, anchor)
    (p,) = cat.plan().proposals
    ev = p["evidence"]
    assert ev["rule_version"] == ev["matcher_verdict"]["rule_version"] == "cmss.v4"
    assert ev["anchor"]["canonical_url"] == anchor[3]
    assert [d["canonical_url"] for d in ev["listings"]] == [listing[3]]


def test_a_generation_word_outside_the_last_segment_holds():
    """An Amazon URL keeps the product name before /dp/<ASIN>: its "2019-New-Version" still holds."""
    listing = ("ETUDE", "ETUDE Moistfull Collagen Eye Cream 28ml", 21.5,
               "https://www.amazon.com/ETUDE-Moistfull-Collagen-Eye-Cream-2019-New-Version/dp/B07ABCDEF1")
    anchor = ("ETUDE", "[EtudeHouse] Moistfull Collagen Eye Cream 28ml", 17.6,
              "https://luxiface.com/products/etudehouse-moistfull-collagen-eye-cream-28ml")
    v, _cat, _li = _verdict(listing, anchor)
    assert (v.status, v.reasons) == ("held", ["slug_generation_mismatch"])


def _merge_catalog(renewal_url: str) -> Tuple[Cat, str, str]:
    cat = Cat()
    an = retailer(cat, 1, "O HUI", "OHUI Miracle Moisture Cleansing Foam 200ml", price=26, created="2026-01-01")
    _set_url(cat, an, "https://dodoskin.com/products/ohui-miracle-moisture-cleansing-foam-200ml")
    a = retailer(cat, 2, "O HUI", "O HUI Miracle Moisture Cleansing Foam 200ml", price=28, ck="ck_foam_shared")
    _set_url(cat, a, "https://kbeautymakeup.com/products/o-hui-miracle-moisture-cleansing-foam-200ml")
    b = retailer(cat, 3, "O HUI", "O HUI Miracle Moisture Cleansing Foam 200ml", price=27, ck="ck_foam_shared")
    _set_url(cat, b, renewal_url)
    return cat, a, b


def test_one_renewal_row_holds_the_whole_merge_key():
    clean, a, b = _merge_catalog("https://nanasbeautyholic.com/products/o-hui-miracle-moisture-cleansing-foam-200ml")
    (p,) = clean.plan().proposals
    assert p["kind"] == "merge_content_key" and sorted(p["evidence"]["from_product_keys"]) == sorted([a, b])
    cat, a, b = _merge_catalog("https://nanasbeautyholic.com/products/o-hui-miracle-moisture-cleansing-foam-200ml-renewal")
    plan = cat.plan()
    assert plan.proposals == []
    assert (outcome(plan, b)["status"], outcome(plan, b)["reason"]) == ("held", "slug_generation_mismatch")
    assert (outcome(plan, a)["status"], outcome(plan, a)["reason"]) == ("held", "key_shared_with_unmatched")
