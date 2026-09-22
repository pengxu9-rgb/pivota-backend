"""Measured (host, merchant product_type) shelves resolve -- and only where somebody read them.

Every CATEGORY_PATTERNS entry matches the SINGULAR noun, so a merchant filing products under
"Cleansers" / "Sheet Masks" / "Serums" resolved to nothing, and one unresolved row blocks its whole
curated cohort. Measured 2026-09-18 over every product on eyurs.com (434), ohlolly.com (510) and
sokoglam.com (567): resolved 204 -> 387, 456 -> 479, 477 -> 478; and -- checked under two flags --
nothing that resolved before changed and no deliberate refusal was overridden.

A first draft used a general plural-singularising DOOR. Review showed it gave confident wrong
leaves on hosts nobody had read and changed rows the evidence policy already resolved; the
adversarial inputs below are that review's, and each must now resolve EXACTLY as the evidence
policy alone does. Titles are the real ones from the crawl unless marked.
"""
import pytest

from services import curated_brand_feed as feed

MEASURED = feed.CATEGORY_CONFIDENCE_MEASURED_HOST_TYPE
FEED_DEFAULT = feed.CATEGORY_CONFIDENCE_FEED_DEFAULT


def resolve(product_type, title, domain, flag_path="beauty"):
    return feed._resolve_category(product_type=product_type, title=title, flag_path=flag_path, domain=domain)


def evidence_only(product_type, title, flag_path="beauty"):
    return feed._resolve_category_by_evidence(product_type=product_type, title=title, flag_path=flag_path)


@pytest.mark.parametrize("domain,ptype,title,want", [
    ("eyurs.com", "Sheet Masks", "Beauty of Joseon Centella Asiatica Calming Sheet Mask", "beauty/skincare/treat/mask"),
    ("eyurs.com", "Moisturizers", "Pyunkang Yul Nutrition Cream (100ml)", "beauty/skincare/moisturize/cream"),
    ("eyurs.com", "Serums", "Beauty of Joseon Glow Serum: Propolis + Niacinamide (30ml)", "beauty/skincare/treat/serum"),
    ("eyurs.com", "Cleansers", "Pyunkang Yul Deep Clear Cleansing Balm", "beauty/skincare/cleanse/cleanser"),
    ("eyurs.com", "Ampoules", "SKIN1004 Madagascar Centella Asiatica 100 Ampoule", "beauty/skincare/treat/serum"),
    ("ohlolly.com", "Wash Off Mask", "Beauty of Joseon Red Bean Refreshing Pore Mask", "beauty/skincare/treat/mask"),
    ("ohlolly.com", "Sleeping Pack", "Cosrx Ultimate Nourishing Rice Overnight Spa Mask", "beauty/skincare/treat/mask"),
    ("ohlolly.com", "Sun Care", "Isntree Hyaluronic Acid Daily Sun Gel", "beauty/skincare/sun/sunscreen"),
    ("sokoglam.com", "Lip Balms", "Phyto-Glow Lip Balm SPF 45", "beauty/makeup/lip/balm"),
    # 2026-09-22 re-read.
    ("eyurs.com", "Masks, Exfoliators", "Round Lab 1025 Dokdo Mud Pack 100ml (Wash Off Mask)", "beauty/skincare/treat/mask"),
    ("eyurs.com", "Masks, Exfoliators", "Isntree Mugwort Calming Clay Mask", "beauty/skincare/treat/mask"),
    # A title matching two leaves (face powder + mask) that includes the shelf's leaf is accepted.
    ("eyurs.com", "Masks, Exfoliators", "My Scheming Collagen Blackhead Remover Mask Powder", "beauty/skincare/treat/mask"),
    ("sokoglam.com", "Spot", "Spot the Difference Blemish Treatment", "beauty/skincare/treat/treatment"),
    ("sokoglam.com", "Spot", "Mighty Patch - The Original", "beauty/skincare/treat/treatment"),
    ("sokoglam.com", "Spot", "Might Patch - Micropoint for Blemishes", "beauty/skincare/treat/treatment"),  # "blemishes" is no pattern
    ("sokoglam.com", "Spot", "A-Clear Soothing Pink Eraser", "beauty/skincare/treat/treatment"),
])
def test_a_measured_shelf_resolves_its_products(domain, ptype, title, want):
    assert resolve(ptype, title, domain) == (want, MEASURED)


def test_the_measured_leaf_carries_its_own_confidence():
    # Below a merchant type that names its class directly: a reader must tell the two apart.
    assert feed.CATEGORY_CONFIDENCE_FEED_DEFAULT < MEASURED < feed.CATEGORY_CONFIDENCE_MERCHANT_TYPE


@pytest.mark.parametrize("domain", [None, "", "unknown-retailer.com", "sokoglam.com"])
def test_a_shelf_name_means_nothing_on_a_host_nobody_read(domain):
    # sokoglam never measured "Serums"; an unknown host never measured anything.
    assert resolve("Serums", "Glow Serum", domain) == evidence_only("Serums", "Glow Serum")


@pytest.mark.parametrize("domain", ["https://www.eyurs.com/", "EYURS.COM", "www.eyurs.com"])
def test_the_host_is_matched_however_it_is_spelled(domain):
    assert resolve("Serums", "Glow Serum", domain)[0] == "beauty/skincare/treat/serum"


def test_a_title_naming_another_leaf_steps_the_shelf_aside():
    """eyurs files its Essence Toner under "Cleansers". The shelf is evidence about the shelf; the
    product's own title outranks it -- and the shelf then contributes NOTHING, rather than forcing
    an unresolved "" onto a row the evidence policy would have answered."""
    assert resolve("Cleansers", "Pyunkang Yul Essence Toner (100ml)", "eyurs.com") == evidence_only(
        "Cleansers", "Pyunkang Yul Essence Toner (100ml)")


def test_a_title_naming_another_leaf_on_a_broad_shelf_is_not_forced_in():
    # Review case: "Sun Care" is a broad retail shelf. A tanning mousse on it is not a sunscreen.
    assert resolve("Sun Care", "Self Tanning Mousse", "ohlolly.com")[0] != "beauty/skincare/sun/sunscreen"


def test_a_callers_explicit_leaf_still_wins_where_the_title_disagrees():
    """Review finding: the draft door returned "" here and dropped the caller's leaf, which would
    block the whole cohort. A leaf flag the evidence policy honours is returned untouched."""
    flag = "beauty/skincare/tone/toner"
    assert resolve("Cleansers", "Pyunkang Yul Essence Toner", "eyurs.com", flag) == evidence_only(
        "Cleansers", "Pyunkang Yul Essence Toner", flag)
    assert resolve("Cleansers", "Pyunkang Yul Essence Toner", "eyurs.com", flag)[0] == flag


# The review's adversarial inputs. On ANY host -- including the measured ones, whose tables do not
# list these shelves -- each must resolve exactly as the evidence policy alone does.
ADVERSARIAL = [
    ("Masks", "Silicone Mask Brush"),               # the tool title resolves before any shelf
    ("Cleansers", "Konjac Cleansing Brush"),
    ("Masks & Peels", "Honey Overnight Pack"),       # the multi-use guard
    ("Cleansers & Toners", "Plain Title"),
    ("Pads", "Makeup Remover Rounds"),
    ("Lash Serums", "Lash Growth Serum"),
    ("Pet Shampoos", "Dog Shampoo"),
    ("Kits", "Brow Kit"),
    ("Polishes", "Nail Polish"),
    ("Scrubs", "Coconut Body Scrub"),
    ("Cotton Pads", "Pyunkang Yul 1/3 Cotton Pads (160pcs)"),
    ("Foot Masks", "PUREDERM Shiny & Soft Foot Peeling Mask (1 Pair)"),
    # Re-read 2026-09-22 and deliberately NOT listed: each shelf held more than one class.
    ("Exfoliators", "APRILSKIN Real Calendula Peel Off Pack (100g)"),
    ("Exfoliators", "Cosrx BHA Blackhead Power Liquid (100ml)"),
    ("Wrinkle Patch", "Anua Triple Acid Spot Care Microdart Patch – Exfoliate & Calm Spots"),
    ("Physical", "AHA-BHA-PHA 30 Days Miracle Truecica Clear Pad"),
    ("Exfoliator", "Tiela Perfume Therapy Body Scrub Shine"),
]


@pytest.mark.parametrize("domain", ["eyurs.com", "ohlolly.com", "sokoglam.com", "unknown.com"])
@pytest.mark.parametrize("ptype,title", ADVERSARIAL)
def test_unmeasured_shelves_resolve_exactly_as_the_evidence_policy_does(domain, ptype, title):
    assert resolve(ptype, title, domain) == evidence_only(ptype, title)


def test_a_deliberate_refusal_is_never_filled_by_a_shelf(monkeypatch):
    """The evidence policy returns "" for conflicting evidence (e.g. an ambiguous lip type). That is
    a refusal, not a gap: even a measured shelf listing that exact type may not fill it."""
    ptype = "Lip Gloss/Oil"
    assert evidence_only(ptype, "Juicy Gloss")[0] == ""
    monkeypatch.setitem(feed._MEASURED_HOST_PRODUCT_TYPES, "eyurs.com",
                        {**feed._MEASURED_HOST_PRODUCT_TYPES["eyurs.com"], "lip gloss/oil": "beauty/makeup/lip/oil"})
    assert resolve(ptype, "Juicy Gloss", "eyurs.com")[0] == ""


def test_a_resolved_leaf_is_never_replaced_by_a_shelf(monkeypatch):
    """Structural no-regression: plant a WRONG shelf entry for a type the evidence policy already
    resolves, and the evidence answer must still win."""
    monkeypatch.setitem(feed._MEASURED_HOST_PRODUCT_TYPES, "eyurs.com",
                        {**feed._MEASURED_HOST_PRODUCT_TYPES["eyurs.com"], "cleanser": "beauty/skincare/treat/mask"})
    assert resolve("Cleanser", "Gentle Cleanser", "eyurs.com")[0] == "beauty/skincare/cleanse/cleanser"


@pytest.mark.parametrize("flag", ["fashion", "electronics", "fashion/shoes"])
def test_a_non_beauty_flag_is_left_alone(flag):
    # "fashion" is a non-leaf root: without the guard, a beauty shelf would fill a fashion row.
    assert resolve("Serums", "Glow Serum", "eyurs.com", flag_path=flag) == evidence_only(
        "Serums", "Glow Serum", flag_path=flag)


def test_the_feed_mapper_passes_the_host():
    rec = feed.shopify_product_to_record({
        "title": "Beauty of Joseon Glow Serum", "product_type": "Serums", "handle": "glow", "vendor": "Beauty of Joseon",
        "variants": [{"price": "18", "available": True}], "images": [{"src": "https://x.com/i.jpg"}],
    }, domain="eyurs.com", category_path="beauty")
    assert rec["pdp"]["category_path"] == "beauty/skincare/treat/serum"


def test_the_repair_planner_agrees_with_a_fresh_ingest_when_it_knows_the_host():
    from scripts.plan_curated_category_repair import plan_category_repair
    row = {"product_key": "k1", "category_path": "beauty", "title": "Beauty of Joseon Glow Serum",
           "product_type": "Serums", "source_domain": "eyurs.com"}
    plan = plan_category_repair([row])
    assert plan["changes"][0]["proposed_category_path"] == "beauty/skincare/treat/serum"
    # Without the host it stays conservative: no measured shelf applies.
    unknown = plan_category_repair([{**row, "source_domain": None}])
    assert unknown["proposed_changes"] == 0


def test_a_title_that_names_no_leaf_still_takes_the_measured_shelf():
    """The rows the table exists for. Every positive fixture above names its own leaf, so without
    this case a stricter "the title must name the leaf" rule would pass every test."""
    assert feed._title_paths("Pyunkang Yul 100") == set()
    assert resolve("Serums", "Pyunkang Yul 100", "eyurs.com") == ("beauty/skincare/treat/serum", MEASURED)


@pytest.mark.parametrize("ptype,title", [
    ("Moisturizers", "Haruharu WONDER Black Bamboo Nourishing Calming Hand & Nail Cream (50ml)"),
    ("Moisturizers", "Derma:B Daily Moisture Body Lotion - Hydrating Care for Dry Skin (400ml)"),
    ("Masks", "Argan Oil Hair Mask"),
    ("Serums", "Scalp Revitalizing Serum"),
    ("Serums", "Lash Growth Serum"),
])
def test_a_face_care_shelf_does_not_take_a_product_for_another_body_area(ptype, title):
    """The first two are REAL eyurs products on its "Moisturizers" shelf -- a hand cream and a body
    lotion that the table would have filed as facial creams."""
    assert resolve(ptype, title, "eyurs.com") == evidence_only(ptype, title)


def test_the_body_area_rule_covers_the_lip_shelf_too():
    assert resolve("Lip Balms", "Nourishing Lip Balm for Dry Lips", "sokoglam.com")[0] == "beauty/makeup/lip/balm"
    assert resolve("Lip Balms", "Lip & Body Balm", "sokoglam.com") == evidence_only("Lip Balms", "Lip & Body Balm")


def test_the_measured_confidence_is_no_other_writers_value():
    from scripts.backfill_pdp_category_path import CONFIDENCE_REGEX_BACKFILL
    from services.pdp_category_classifier import CATEGORY_CONFIDENCE_VARIANT
    assert MEASURED not in {CONFIDENCE_REGEX_BACKFILL, CATEGORY_CONFIDENCE_VARIANT,
                            feed.CATEGORY_CONFIDENCE_MERCHANT_TYPE, feed.CATEGORY_CONFIDENCE_EXPLICIT_TITLE,
                            feed.CATEGORY_CONFIDENCE_FEED_DEFAULT}


def test_the_planner_never_lets_a_shelf_challenge_a_stored_leaf():
    """Review finding: --review-existing-leaves sets the fallback to "beauty", so a shelf could have
    proposed replacing a stored toner with a cleanser. A shelf fills gaps; it never overrules a leaf."""
    from scripts.plan_curated_category_repair import plan_category_repair
    row = {"product_key": "k1", "category_path": "beauty/skincare/tone/toner", "source_domain": "eyurs.com",
           "title": "Pyunkang Yul Calming Deep Moisture (200ml)", "product_type": "Toner",
           "category_evidence": {"title": "Pyunkang Yul Calming Deep Moisture (200ml)", "product_type": "Cleansers",
                                 "source_url": "https://eyurs.com/products/x", "observed_at": "2026-09-18T00:00:00Z"}}
    plan = plan_category_repair([row], review_existing_leaves=True)
    assert plan["proposed_changes"] == 0


@pytest.mark.parametrize("domain,ptype,title", [
    # The title names a DIFFERENT leaf than the new shelf: the shelf steps aside.
    ("sokoglam.com", "Spot", "Mineral Sunscreen SPF 50"),                                   # invented
    ("eyurs.com", "Masks, Exfoliators", "Gentle Peeling Gel"),                              # invented
    # Real: "wash" is the cleanser pattern, so the title names a different leaf and the shelf yields.
    ("eyurs.com", "Masks, Exfoliators", "AXIS-Y Mugwort Pore Clarifying Wash Off Pack"),
    # Another body area on a face shelf.
    ("sokoglam.com", "Spot", "Body Acne Spot Treatment"),                                   # invented
    ("eyurs.com", "Masks, Exfoliators", "Argan Hair Mask"),                                 # invented
])
def test_the_new_shelves_step_aside_for_a_title_that_names_another_class(domain, ptype, title):
    # The shelf contributes nothing: the answer is exactly the evidence policy's.
    assert resolve(ptype, title, domain) == evidence_only(ptype, title)


@pytest.mark.parametrize("domain", ["ohlolly.com", "unknown.com"])
@pytest.mark.parametrize("ptype", ["Spot", "Acne Pimple Patch", "Masks, Exfoliators"])
def test_the_new_shelves_mean_nothing_on_a_host_that_did_not_file_them(domain, ptype):
    assert resolve(ptype, "Cosrx Acne Pimple Master Patch", domain) == evidence_only(ptype, "Cosrx Acne Pimple Master Patch")


# #2250: CATEGORY_PATTERNS now files an acne patch under treat/treatment too, so the shelf and the
# classifier agree. eyurs' "Acne Pimple Patch" type now names ONE leaf (it named mask via "pimple
# patch" AND treatment via "acne" before), so it resolves on the merchant's own type, ahead of the shelf.
@pytest.mark.parametrize("title", [
    "Cosrx AC Collection Acne Patch – Heal & Protect Active Breakouts",
    "Cosrx Acne Pimple Master Patch",
    "NEOGEN Dermalogy A-Clear Soothing Clear Spot Patch",
])
def test_the_acne_patch_type_resolves_on_its_own_evidence(title):
    want = ("beauty/skincare/treat/treatment", feed.CATEGORY_CONFIDENCE_MERCHANT_TYPE)
    assert resolve("Acne Pimple Patch", title, "eyurs.com") == want
    assert evidence_only("Acne Pimple Patch", title) == want


def test_a_pimple_patch_title_no_longer_steps_the_spot_shelf_aside():
    # Before #2250 "pimple patch" named mask, the shelf yielded and the row stayed unresolved.
    assert resolve("Spot", "Soft Shield Pimple Patch", "sokoglam.com") == ("beauty/skincare/treat/treatment", MEASURED)


# The evidence policy counts EVERY pattern that matches. An acne-qualified plural must name one
# leaf, not treatment (the acne entry) AND mask (Mask's generic "patches" arm).
@pytest.mark.parametrize("text,paths", [
    ("Pimple Patches", 1), ("Spot Patches", 1), ("Spot Cover Patches", 1),
    ("Acne Patches", 1), ("Blemish Patches", 1), ("Acne Pimple Patch", 1),
    ("Eye Patches", 1), ("Hydrogel Patches", 1),
    # The look-behinds are word-anchored and exact: only the acne qualifiers are declined.
    ("Tattoo Cover Patches", 1), ("Hotspot Patches", 1),
])
def test_an_acne_patch_type_names_one_leaf(text, paths):
    assert feed._pattern_matches(text) == paths


@pytest.mark.parametrize("ptype,want", [
    ("Pimple Patches", "beauty/skincare/treat/treatment"),
    ("Spot Cover Patches", "beauty/skincare/treat/treatment"),
    ("Eye Patches", "beauty/skincare/treat/mask"),
    ("Hydrogel Patches", "beauty/skincare/treat/mask"),
])
def test_a_patch_type_resolves_rather_than_reading_as_ambiguous(ptype, want):
    assert evidence_only(ptype, "Some Product 36ct") == (want, feed.CATEGORY_CONFIDENCE_MERCHANT_TYPE)
