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
    """Review finding: no test crossed two listed families, so a mutant writing ANY listed family
    whenever the vendor was listed survived. A Purito product with --brand Manyo keeps its vendor."""
    assert brand_on("ohlolly.com", "Purito", "Manyo") == "Purito"
    assert brand_on("sokoglam.com", "MANYO FACTORY", "Purito Seoul") == "MANYO FACTORY"


def test_every_host_of_a_renamed_brand_now_shares_one_content_key():
    """The point of the change, stated on the identity key itself: three retailers' spellings of
    one product, each run with a DIFFERENT --brand spelling, produce ONE content_key."""
    keys = {
        make_content_key(brand_on(host, vendor, override), "Pure Fit Cica Cleansing Foam", None)
        for host, vendor, override in (("sokoglam.com", "Purito Seoul", "Purito Seoul"),
                                       ("eyurs.com", "Purito SEOUL", "PURITO"),
                                       ("ohlolly.com", "Purito", "purito seoul"))
    }
    assert len(keys) == 1


def test_before_this_change_the_renamed_brand_split_identity():
    """The control: the raw vendors themselves DO produce different content keys, so the test above
    is not passing because content_key ignores the brand."""
    raw = {make_content_key(v, "Pure Fit Cica Cleansing Foam", None) for v in ("Purito Seoul", "Purito")}
    assert len(raw) == 2


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


def test_no_override_writes_the_vendor():
    assert brand_on("retailer.com", "MANYO FACTORY", None) == "MANYO FACTORY"


def test_a_vendor_naming_the_store_is_still_refused_in_retailer_mode():
    """The store-name heuristic belongs to brand-official mode. Here it stays a refusal: a vendor
    that is the retailer's own name is not maker evidence, and no override may paper over it."""
    with pytest.raises(ValueError, match="retailer_maker_unproven"):
        brand_on("sokoglam.com", "Soko Glam", "Soko Glam")
