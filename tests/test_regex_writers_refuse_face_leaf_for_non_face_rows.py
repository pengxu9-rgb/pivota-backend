"""The regex writers never file a nail, lash, body or hand product under a FACE skincare leaf.

`resolve_path_from_row` feeds the regex backfill (scripts/backfill_pdp_category_path.py), the
seed mirror's at-mirror classification, the LLM backfill's regex step and merchant sync. Its
patterns keep only the noun they recognise: a merchant type "Nail Polish" matched the exfoliant
pattern through "polish", so 52 live nail polishes sat on beauty/skincare/treat/exfoliant.

On 2026-09-23 the 188 rows with an honest leaf were moved (body care / hair care). The other 60
-- nail polishes, lash and brow serums -- had NO leaf; clearing them would have been undone by
the next backfill run, which re-derived the same face leaf. The rule now lives in
`non_face_leaf`, shared with the curated resolver, and returns NO answer for them.

2026-09-26: nail products got leaves (beauty/makeup/nails/*, Peng). A nail polish now resolves to
`beauty/makeup/nails/nail-polish` -- still never to a face leaf, which is what these tests guard.
Lash and brow serums still have no leaf and still get no answer.

Measured 2026-09-23 over all 11,531 live prod rows: 415 classify differently, every one names a
non-face area; 0 changes elsewhere, 0 in merchant sync.
"""
import pytest

from services.pdp_category_classifier import classify, non_face_leaf, resolve_path_from_row

BODY = ("Body Care", "beauty/body/care")
POLISH = ("Nail Polish", "beauty/makeup/nails/nail-polish")
HAIR = ("Hair Care", "beauty/haircare/general")


def rp(title, product_type=None, category=None):
    return resolve_path_from_row(category=category, product_type=product_type, title=title)


# Real live rows that were HELD on 2026-09-23 (title, merchant product_type, stored face leaf).
@pytest.mark.parametrize("title,ptype,was", [
    ("Large Lash Serum", None, "beauty/skincare/treat/serum"),
    ("Perfect Lash Serum 8ml", "Lash Serum", "beauty/skincare/treat/serum"),
    ("Pink Enriched Eyelash Serum", "Eyelash Serum", "beauty/skincare/treat/serum"),
    ("Dailism Peptide Enhancing Lash Serum", "Eye serum", "beauty/skincare/treat/serum"),
    ("Moon Boost Eyebrow and Lash Serum", "Eye Treatment", None),
])
def test_the_held_rows_get_no_answer(title, ptype, was):
    old = classify(ptype) or classify(title)
    if was:
        assert old[1] == was  # the defect, reproduced
    assert rp(title, ptype) is None


@pytest.mark.parametrize("title,ptype", [
    ("Add Blueberry Smoothie - Deep Lilac", "Nail Polish"),
    ("MN36 Cosmic Cutie: Chrome Bronze Peel Off Nail Polish", "Nail Polish"),
    ("Rainbow Mani Magic Set", "Nail Polish Set"),
])
def test_the_held_nail_polishes_get_the_nail_leaf(title, ptype):
    """Held 2026-09-23 on beauty/skincare/treat/exfoliant; since 2026-09-26 the nail leaf."""
    assert rp(title, ptype) == POLISH
    assert "exfoliant" not in rp(title, ptype)[1]


@pytest.mark.parametrize("title,ptype,category,want", [
    ("Rose Hand Wash", None, None, BODY),
    ("Sunshine Lemon Hand & Body Wash", None, None, BODY),
    ("Lavender Body Oil", None, None, BODY),
    ("Urea 5% Body Serum", None, None, BODY),
    ("Bifi Blanche Microbiome Foot Cream", "Foot Cream", None, BODY),
    ("Collagen Cream", "Body Strategist", None, BODY),          # the area is in the merchant type
    ("Collagen Cream", None, "Body", BODY),                     # ...or in the category
    ("Hair Serum with Fermented Rice Water + Baobab", "Serum", None, HAIR),
])
def test_an_area_with_a_leaf_gets_that_leaf(title, ptype, category, want):
    assert rp(title, ptype, category) == want


# REFUSING EXAMPLES: face products, and non-face answers, resolve exactly as the first regex hit.
@pytest.mark.parametrize("title,ptype", [
    ("Gentle Face Wash", None),
    ("Handmade Cream", "Moisturizer"),
    ("Splash Serum", None),
    ("Brown Rice Toner", None),
    ("The Body Shop Vitamin C Glow Cream", None),
    ("Face & Body Lotion", None),
    ("Eye Cream for Crow’s Feet", None),
    ("Oil Cleanser (safe for eyelash extensions)", None),
    ("Micellar Cleansing Water - safe for lashes", None),
    ("Cleansing Oil, gentle on the eyelashes", None),
    ("Hand Poured Cleansing Balm", None),
    ("Second hand Toner", None),
    ("Nailed It Cleansing Balm", None),
    ("Body Sunscreen SPF 50", None),        # sun/sunscreen is not a face leaf
    ("Scalp Shampoo", None),                # already a hair leaf
    ("Lip & Body Balm", None),              # a makeup leaf
    ("Retro Matte Lipstick", None),
])
def test_everything_else_is_the_first_regex_hit(title, ptype):
    assert rp(title, ptype) == (classify(ptype) or classify(title))


def test_the_curated_resolver_and_the_regex_writers_share_one_rule():
    # Identity, not behaviour: a copied rule would pass a behavioural check and then drift.
    import services.curated_brand_feed as feed
    import services.pdp_category_classifier as pcc
    assert feed.non_face_leaf is pcc.non_face_leaf
    assert feed._FACE_SKINCARE_LEAF is pcc.FACE_SKINCARE_LEAF
    assert feed._non_face_leaf("beauty/skincare/treat/serum", title="My Lash Serum", product_type=None) == ""


def test_the_variant_fold_is_guarded_too():
    from services.pdp_category_classifier import fold_category_from_variants
    assert fold_category_from_variants(category=None, product_type="Lash Serum",
                                       title="Perfect Lash Serum", variants=None) is None
    (hit, _source, _conf) = fold_category_from_variants(category=None, product_type="Nail Polish",
                                                        title="Deep Lilac", variants=None)
    assert hit == POLISH


@pytest.mark.parametrize("ptype,title,variants", [
    # A refused product-level answer ends the fold: the variant's own words lack the area.
    ("Lash Serum", "Perfect Lash Serum", [{"title": "Serum 8ml"}]),
    ("Lash Serum", "Perfect Lash Serum", [{"title": "8ml", "platform_metadata": {"product_type": "Serum"}}]),
    # No product-level hit, but the variant's face-leaf hit is judged on the product's words too.
    (None, "Perfect Lash Booster", [{"title": "Serum"}]),
    # ...and a refused variant ends the fold too, rather than trying the next variant's noun.
    (None, "Perfect Lash Booster", [{"title": "Serum"}, {"title": "Top Coat"}]),
])
def test_a_variant_cannot_bring_the_refused_face_leaf_back(ptype, title, variants):
    from services.pdp_category_classifier import fold_category_from_variants
    assert fold_category_from_variants(category=None, product_type=ptype, title=title, variants=variants) is None


@pytest.mark.parametrize("ptype,title,variants", [
    ("Nail Polish", "Deep Lilac", [{"title": "Polish"}]),
    ("Nail Polish", "Chrome Nail Polish", [{"title": "Top Coat + Cuticle Oil"}]),   # not a fashion coat
    (None, "Deep Lilac", [{"title": "Nail Polish"}]),
])
def test_a_nail_polish_folds_to_the_nail_leaf_never_a_face_or_fashion_one(ptype, title, variants):
    from services.pdp_category_classifier import fold_category_from_variants
    (hit, _source, _conf) = fold_category_from_variants(category=None, product_type=ptype, title=title,
                                                        variants=variants)
    assert hit == POLISH


def test_a_variant_face_answer_for_a_face_product_is_unchanged():
    from services.pdp_category_classifier import fold_category_from_variants
    (hit, source, _conf) = fold_category_from_variants(category=None, product_type=None, title="Glow No. 3",
                                                       variants=[{"title": "Serum 30ml"}])
    assert hit == ("Serum", "beauty/skincare/treat/serum") and source == "variant_aggregate"


@pytest.mark.asyncio
@pytest.mark.parametrize("title,ptype,llm_path,want", [
    ("Perfect Lash Serum", "Lash Serum", "beauty/skincare/treat/serum", None),
    # The regex answers first now (the nail leaf), so the LLM's face answer is never consulted.
    ("Deep Lilac", "Nail Polish", "beauty/skincare/treat/exfoliant", "beauty/makeup/nails/nail-polish"),
    ("Silky Body Butter", None, "beauty/skincare/moisturize/cream", "beauty/body/care"),
    ("Glow No. 3", None, "beauty/skincare/treat/serum", "beauty/skincare/treat/serum"),
])
async def test_the_llm_answer_is_guarded_too(monkeypatch, title, ptype, llm_path, want):
    """The LLM backfill selects category_path IS NULL -- exactly where a refused row lands."""
    import services.category_classifier_llm as cc
    from services.pdp_category_classifier import fold_category_with_llm_fallback
    monkeypatch.setenv("LLM_CATEGORY_CLASSIFIER_ENABLED", "true")
    cc._invalidate_cache_for_tests()

    async def fake(*, user_message, timeout_s=15.0):
        return {"label": "X", "path": llm_path, "confidence": 0.9}

    monkeypatch.setattr(cc, "_call_deepseek_classify", fake)
    result = await fold_category_with_llm_fallback(title=title, product_type=ptype)
    assert (result[0][1] if result else None) == want


@pytest.mark.asyncio
async def test_the_backfill_files_a_nail_polish_on_the_nail_leaf_not_exfoliant(monkeypatch):
    """The point of the 2026-09-23 change: a cleared nail polish is not re-filed under exfoliant.
    Since 2026-09-26 it has a leaf of its own, so the backfill files it THERE."""
    import scripts.backfill_pdp_category_path as bf
    row = {"product_key": "k-nail", "category": None, "category_path": None, "brand": "Miss Nella",
           "product_type": "Nail Polish", "title": "Chrome Bronze Peel Off Nail Polish"}
    batches = [[row], []]
    writes = []

    async def fetch(*args, **kwargs):
        return batches.pop(0) if batches else []

    async def fetch_val(query, values=None):
        writes.append(values)
        return "k-nail"

    monkeypatch.setattr(bf.database, "fetch_all", fetch)
    monkeypatch.setattr(bf.database, "fetch_val", fetch_val)
    monkeypatch.setattr(bf.database, "is_connected", True, raising=False)
    report = await bf.run_category_path_backfill(dry_run=False)
    assert report["matched"] == 1 and report["unmatched"] == 0, report
    assert report["matched_by_path"] == {"beauty/makeup/nails/nail-polish": 1}, report
    assert not any("exfoliant" in str(w) for w in writes), writes


@pytest.mark.asyncio
async def test_the_backfill_files_a_body_wash_as_body_care(monkeypatch):
    import scripts.backfill_pdp_category_path as bf
    row = {"product_key": "k-body", "category": None, "category_path": None, "brand": "Naturium",
           "product_type": None, "title": "The Calmer Ceramide Body Wash"}
    batches = [[row], []]

    async def fetch(*args, **kwargs):
        return batches.pop(0) if batches else []

    monkeypatch.setattr(bf.database, "fetch_all", fetch)
    monkeypatch.setattr(bf.database, "is_connected", True, raising=False)
    report = await bf.run_category_path_backfill(dry_run=True)
    assert report["matched_by_path"] == {"beauty/body/care": 1}, report


def test_the_mirror_files_a_new_nail_polish_on_the_nail_leaf():
    from scripts.mirror_external_seeds_to_catalog_products import resolve_mirror_category_metadata
    meta = resolve_mirror_category_metadata(category=None, product_type="Nail Polish",
                                            title="Add Blueberry Smoothie - Deep Lilac")
    assert meta["category_path"] == "beauty/makeup/nails/nail-polish"


# 2026-09-26: the 9 live curl creams filed on a face leaf (title verbatim, stored path). Every one
# came through the bare "cream" arm of the Moisturizer pattern.
CURL_CREAMS_ON_A_FACE_LEAF = [
    ("Curl Cream 100ml", "beauty/skincare/moisturize/cream"),
    ("Curl Cream 100ml - Barber", "beauty/skincare/moisturize/cream"),
    ("Curl Defining Cream", "beauty/skincare/moisturize/cream"),
    ("Parkjun Beautilab LPP Ceramide Hard Curl Cream", "beauty/skincare/moisturize/cream"),
    ("Parkjun Beautilab LPP Ceramide Soft Curl Cream", "beauty/skincare/moisturize/cream"),
    ("The Homecurl Curl-Defining Cream", "beauty/skincare/moisturize/cream"),
    ("The Homecurl Curl-Defining Cream Deluxe Sample", "beauty/skincare/moisturize/cream"),
    ("Wave Boost Curl Cream 150ml", "beauty/skincare/moisturize/cream"),
    # Stored on the bare skincare root by the enrichment agent; the regex answer was the same face leaf.
    ("Multi Texture Curl Cream", "beauty/skincare"),
]


@pytest.mark.parametrize("title,stored", CURL_CREAMS_ON_A_FACE_LEAF)
def test_a_curl_cream_is_hair_care(title, stored):
    assert classify(title)[1] == "beauty/skincare/moisturize/cream"  # the defect, reproduced
    assert rp(title) == HAIR


def test_the_mirror_fold_files_a_curl_cream_under_hair_care():
    # fold_category_from_variants is what the seed mirror calls on INSERT.
    from services.pdp_category_classifier import fold_category_from_variants

    hit = fold_category_from_variants(category=None, product_type=None, title="Curl Cream 100ml")
    assert hit is not None and hit[0] == HAIR


@pytest.mark.parametrize("title,want", [
    # Merchant-filed siblings already on haircare/general keep the same answer from the title alone.
    ("Moroccanoil Curl Defining Cream 250 mL", HAIR),
    ("Moroccanoil Intense Curl Cream 10.2 OZ", HAIR),
    ("Curly Hair Cream", HAIR),
    # No curl word: a face cream stays a face cream.
    ("Radian-C Cream", ("Moisturizer", "beauty/skincare/moisturize/cream")),
    # The face is named too: the face answer stands, as for "Face & Body Lotion".
    ("Curl Friendly Face Cream", ("Moisturizer", "beauty/skincare/moisturize/cream")),
])
def test_curl_rule_boundaries(title, want):
    assert rp(title) == want


def test_curling_mascara_is_not_a_face_skincare_leaf_so_the_curl_rule_never_runs():
    hit = rp("Lash Curling Mascara")
    assert hit is not None and "haircare" not in hit[1] and not hit[1].startswith("beauty/skincare/")
