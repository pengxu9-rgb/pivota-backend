"""The regex writers never file a nail, lash, body or hand product under a FACE skincare leaf.

`resolve_path_from_row` feeds the regex backfill (scripts/backfill_pdp_category_path.py), the
seed mirror's at-mirror classification, the LLM backfill's regex step and merchant sync. Its
patterns keep only the noun they recognise: a merchant type "Nail Polish" matched the exfoliant
pattern through "polish", so 52 live nail polishes sat on beauty/skincare/treat/exfoliant.

On 2026-09-23 the 188 rows with an honest leaf were moved (body care / hair care). The other 60
-- nail polishes, lash and brow serums -- have NO leaf; clearing them would have been undone by
the next backfill run, which re-derived the same face leaf. The rule now lives in
`non_face_leaf`, shared with the curated resolver, and returns NO answer for them.

Measured 2026-09-23 over all 11,531 live prod rows: 415 classify differently, every one names a
non-face area; 0 changes elsewhere, 0 in merchant sync.
"""
import pytest

from services.pdp_category_classifier import classify, non_face_leaf, resolve_path_from_row

BODY = ("Body Care", "beauty/body/care")
HAIR = ("Hair Care", "beauty/haircare/general")


def rp(title, product_type=None, category=None):
    return resolve_path_from_row(category=category, product_type=product_type, title=title)


# Real live rows that were HELD on 2026-09-23 (title, merchant product_type, stored face leaf).
@pytest.mark.parametrize("title,ptype,was", [
    ("Add Blueberry Smoothie - Deep Lilac", "Nail Polish", "beauty/skincare/treat/exfoliant"),
    ("MN36 Cosmic Cutie: Chrome Bronze Peel Off Nail Polish", "Nail Polish", "beauty/skincare/treat/exfoliant"),
    ("Rainbow Mani Magic Set", "Nail Polish Set", "beauty/skincare/treat/exfoliant"),
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
    ("Nailed It Cleansing Balm", None),
    ("Body Sunscreen SPF 50", None),        # sun/sunscreen is not a face leaf
    ("Scalp Shampoo", None),                # already a hair leaf
    ("Lip & Body Balm", None),              # a makeup leaf
    ("Retro Matte Lipstick", None),
])
def test_everything_else_is_the_first_regex_hit(title, ptype):
    assert rp(title, ptype) == (classify(ptype) or classify(title))


def test_the_curated_resolver_and_the_regex_writers_share_one_rule():
    import services.curated_brand_feed as feed
    assert feed._FACE_SKINCARE_LEAF.pattern.startswith("^beauty/skincare/")
    assert feed._non_face_leaf("beauty/skincare/treat/serum", title="My Lash Serum", product_type=None) == ""
    assert non_face_leaf("beauty/skincare/treat/serum", "My Lash Serum") == ""


def test_the_variant_fold_is_guarded_too():
    from services.pdp_category_classifier import fold_category_from_variants
    assert fold_category_from_variants(category=None, product_type="Nail Polish",
                                       title="Deep Lilac", variants=None) is None


@pytest.mark.asyncio
async def test_the_backfill_leaves_a_nail_polish_unmatched_and_writes_nothing(monkeypatch):
    """The point of the change: a cleared nail polish is not re-filed under exfoliant."""
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
    assert report["matched"] == 0 and report["unmatched"] == 1, report
    assert writes == []


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


def test_the_mirror_leaves_a_new_nail_polish_uncategorised():
    from scripts.mirror_external_seeds_to_catalog_products import resolve_mirror_category_metadata
    meta = resolve_mirror_category_metadata(category=None, product_type="Nail Polish",
                                            title="Add Blueberry Smoothie - Deep Lilac")
    assert meta["category_path"] is None
