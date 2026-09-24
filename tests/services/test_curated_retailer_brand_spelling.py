"""One product sold by two retailers must carry ONE brand, whatever each retailer calls it.

Retailer mode kept the merchant's vendor spelling unless it equalled the operator's --brand
EXACTLY (alphanumerics, case-folded). Measured 2026-09-18 across the three US K-beauty hosts:
case and punctuation variants already collapse under that rule (ETUDE/Etude, TONYMOLY/TONY MOLY,
manyo/ma:nyo), but two genuine renames did not -- "MANYO FACTORY" (sokoglam) vs "ma:nyo"/"manyo"
(ohlolly, eyurs), and "Purito Seoul" (sokoglam, eyurs) vs "Purito" (ohlolly). The identity key is
built from the brand, so each of those products would get one brand identity per retailer and
never converge.

The fix is an explicit, measured list of renames (RETAILER_BRAND_SPELLINGS) -- NOT a containment
rule. Containment cannot tell a rename ("Purito" -> "Purito Seoul") from a distinct vendor
("A'PIEU" vs "A'PIEU Plus"), and test_retailer_adversarial_acceptance.py pins that the latter
is never overridden. A first draft of this change used containment and failed that test.
"""
import pytest

from services import curated_brand_feed as feed
from services.catalog_identity import make_content_key


def product(vendor, number=1):
    return {
        "id": 9100000 + number, "vendor": vendor, "title": "Pure Fit Cica Cleansing Foam",
        "handle": f"pure-fit-cica-{number}", "product_type": "Cleanser",
        "body_html": "<p>Ingredients: Water, Glycerin</p>",
        "images": [{"src": "https://cdn.example/item.jpg"}],
        "variants": [{"id": 46000000000000 + number, "price": "18.00", "available": True, "sku": f"PF{number}"}],
    }


def brand_on(host, vendor, override):
    rec = feed.shopify_product_to_record(
        product(vendor), domain=host, category_path="beauty/skincare",
        brand_override=override, currency="USD", source_role="retailer", retailer_name=host,
    )
    return rec["pdp"]["brand"]


@pytest.mark.parametrize("host,vendor", [
    ("sokoglam.com", "MANYO FACTORY"), ("ohlolly.com", "ma:nyo"), ("eyurs.com", "manyo"),
])
@pytest.mark.parametrize("override", ["Manyo", "ma:nyo", "Manyo Factory", "MANYO"])
def test_a_renamed_brand_is_written_under_the_catalogs_one_spelling(host, vendor, override):
    """Whatever the operator typed for --brand, the family writes the spelling the catalog already
    carries -- 78 brand-official rows as "Ma:nyo" (manyo.us). normalize_brand keeps punctuation, so
    writing "Manyo" would split every retailer row from those official rows."""
    assert brand_on(host, vendor, override) == "Ma:nyo"


def test_the_family_spelling_joins_the_existing_official_rows_identity():
    official = make_content_key("Ma:nyo", "Pure Cleansing Oil", None)          # the stored official rows
    retail = make_content_key(brand_on("sokoglam.com", "MANYO FACTORY", "Manyo"), "Pure Cleansing Oil", None)
    assert retail == official
    # The control: the operator's plain spelling would NOT have joined them.
    assert make_content_key("Manyo", "Pure Cleansing Oil", None) != official


def test_one_family_never_relabels_another():
    """Review finding: no test crossed two listed families. A product keeps its OWN family's spelling
    whatever family --brand names -- never the other family's."""
    assert brand_on("ohlolly.com", "Purito", "Manyo") == "Purito SEOUL"
    assert brand_on("sokoglam.com", "MANYO FACTORY", "Purito Seoul") == "Ma:nyo"


@pytest.mark.parametrize("vendor,want", [("Purito", "Purito SEOUL"), ("MANYO FACTORY", "Ma:nyo")])
def test_a_listed_vendor_converges_even_without_brand(vendor, want):
    """Review finding: with no --brand the raw vendor was written, so a whole-feed run never joined
    the family. The vendor's own family decides its spelling."""
    assert brand_on("retailer.com", vendor, None) == want


def test_a_brand_official_store_writes_its_familys_spelling_too():
    """Review finding: brand-official mode never consulted the families, so a manyo.us re-run with
    --brand "Manyo" would write "Manyo" and split from the 78 "Ma:nyo" rows it already has."""
    rec = feed.shopify_product_to_record(product("manyo"), domain="manyo.us", category_path="beauty/skincare",
                                         brand_override="Manyo", currency="USD", source_role="brand_official")
    assert rec["pdp"]["brand"] == "Ma:nyo"


@pytest.mark.parametrize("vendor,override", [("Innisfree", "Laneige"), ("COSRX", "Cosmetics")])
def test_a_genuinely_different_vendor_keeps_its_own_name(vendor, override):
    """Containment, not proximity: an operator's --brand cannot relabel an unrelated maker."""
    assert brand_on("retailer.com", vendor, override) == vendor


@pytest.mark.parametrize("vendor,override", [
    ("A'PIEU Plus", "A'PIEU"),      # a distinct vendor, pinned by the adversarial acceptance suite
    ("Klairs Plus", "Klairs"),      # the same shape, unlisted: containment is NOT the rule
    ("Core by Urang", "Urang"),     # a measured sub-line (ohlolly), deliberately not listed
])
def test_an_unlisted_containing_spelling_keeps_the_vendor(vendor, override):
    assert brand_on("retailer.com", vendor, override) == vendor


def test_a_listed_family_does_not_absorb_an_unlisted_neighbour():
    # "Purito Plus" contains "purito" but is not a listed spelling of the family.
    assert brand_on("retailer.com", "Purito Plus", "Purito Seoul") == "Purito Plus"


def test_every_family_has_exactly_one_canonical_spelling():
    assert set(feed.RETAILER_BRAND_CANONICAL) == set(feed.RETAILER_BRAND_SPELLINGS.values())


def test_case_and_punctuation_variants_still_collapse_as_before():
    for vendor in ("ETUDE", "Etude", "etude"):
        assert brand_on("retailer.com", vendor, "Etude") == "Etude"
    assert brand_on("retailer.com", "TONY MOLY", "TONYMOLY") == "TONYMOLY"


def test_no_override_writes_an_unlisted_vendor_as_is():
    assert brand_on("retailer.com", "Some Indie Brand", None) == "Some Indie Brand"


def test_a_vendor_naming_the_store_is_still_refused_in_retailer_mode():
    """The store-name heuristic belongs to brand-official mode. Here it stays a refusal: a vendor
    that is the retailer's own name is not maker evidence, and no override may paper over it."""
    with pytest.raises(ValueError, match="retailer_maker_unproven"):
        brand_on("sokoglam.com", "Soko Glam", "Soko Glam")


# ---- US top-100 families (measured 2026-09-24) -------------------------------------------------

@pytest.mark.parametrize("vendor,want", [
    ("Tom Ford", "Tom Ford Beauty"), ("TOM FORD", "Tom Ford Beauty"), ("Tom Ford Beauty", "Tom Ford Beauty"),
    ("Christian Dior", "Dior"), ("DIOR", "Dior"),
    ("Yves Saint Laurent", "YSL"), ("YSL Beauty", "YSL"),
    ("Jo Malone London", "Jo Malone"), ("JO MALONE", "Jo Malone"),
    ("Lancome", "Lancôme"), ("LANCÔME", "Lancôme"),
    ("Estee Lauder", "Estée Lauder"), ("Estée Lauder", "Estée Lauder"),
    ("Kiehl's", "Kiehl's Since 1851"), ("Kiehl's Since 1851", "Kiehl's Since 1851"),
    ("Dolce & Gabbana", "Dolce and Gabbana"), ("Dolce and Gabbana", "Dolce and Gabbana"),
    ("L'Oréal Paris", "L'Oreal Paris"), ("L'Oreal Paris", "L'Oreal Paris"),
    ("Tresemme", "TRESemmé"), ("Avene", "Avène"), ("Kerastase", "Kérastase"),
])
def test_a_us_store_spelling_converges_on_the_catalogs(vendor, want):
    assert brand_on("perfumania.com", vendor, None) == want


def test_an_accent_is_a_brand_split_without_the_family():
    """Why the accent families exist: the identity key keeps accents."""
    assert make_content_key("Lancome", "Idole Eau de Parfum", None) != make_content_key("Lancôme", "Idole Eau de Parfum", None)
    assert make_content_key(brand_on("perfumania.com", "Lancome", None), "Idole Eau de Parfum", None) == \
        make_content_key("Lancôme", "Idole Eau de Parfum", None)


@pytest.mark.parametrize("vendor", ["TF", "Tom Ford Men", "Dior Homme Parfums", "L'Oréal Professionnel", "REVIVE COLLAGEN"])
def test_a_neighbour_of_a_us_family_keeps_its_own_name(vendor):
    """Only listed spellings join: a sub-line or a different maker is never absorbed."""
    assert brand_on("retailer.com", vendor, None) == vendor


@pytest.mark.parametrize("host,vendor,override,want", [
    ("yslbeautyus.com", "Yves Saint Laurent", "YSL", "YSL"),
    ("jomalone.com", "Jo Malone London", "Jo Malone London", "Jo Malone"),
    ("lancome-usa.com", "Lancome", "Lancome", "Lancôme"),
    ("dior.com", "Christian Dior", "Dior", "Dior"),
    ("kiehls.com", "Kiehl's", "Kiehl's", "Kiehl's Since 1851"),
    ("tomfordbeauty.com", "TF", "Tom Ford Beauty", "TF"),   # "TF" is not a listed spelling
])
def test_a_brand_official_store_writes_the_us_familys_spelling(host, vendor, override, want):
    rec = feed.shopify_product_to_record(product(vendor), domain=host, category_path="beauty/skincare",
                                         brand_override=override, currency="USD", source_role="brand_official")
    assert rec["pdp"]["brand"] == want
