"""One brand, one spelling: the 2026-09-30 families (services/curated_brand_feed.RETAILER_BRAND_SPELLINGS).

Measured in prod 2026-09-30 (read-only census): of the 13,337 rows ingested 09-23..09-29, 23 brand keys
were stored under two or three spellings -- MISSHA/Missha (562 rows), ROUND LAB/Round Lab (168), SKIN1004/
Skin1004 (157), ... Seventeen of them differ only in case, so every spelling already shares one content_key
(normalize_brand lowercases) and the families below change no identity: they only make the next ingest write
one display spelling. The inputs are the vendor strings the stores actually sent -- in retailer mode the
stored brand IS the vendor (an operator --brand only replaces an alnum-equal vendor), read off the stored rows
per host.

The splits that DO change identity ("O HUI (오휘)", "Jung Saem Mool", "APIEU", "Supergoop") are deliberately
NOT families yet: a family live on the drain before the old rows are relabelled fails every re-crawl of a store
that still has an old-spelling row (services/brand_relabel.py ORDER). The last tests pin that.
"""
import json
from pathlib import Path

import pytest

from services import curated_brand_feed as feed
from services.catalog_identity import make_content_key, normalize_brand

NEW_FAMILIES = {
    "missha", "mixsoon", "roundlab", "anua", "arencia", "skin1004", "medicube", "beautyofjoseon", "theordinary",
    "isntree", "centellian24", "makeupforever", "genabelle", "maybelline", "bobbibrown", "molvany", "byterry",
}
CORPUS = json.loads((Path(__file__).resolve().parents[1] / "fixtures" /
                     "catalog_brand_spellings_2026_09_30.json").read_text())["brands"]


def product(vendor, number=1):
    return {
        "id": 9300000 + number, "vendor": vendor, "title": "Pure Fit Cica Cleansing Foam",
        "handle": f"pure-fit-cica-{number}", "product_type": "Cleanser",
        "body_html": "<p>Ingredients: Water, Glycerin</p>",
        "images": [{"src": "https://cdn.example/item.jpg"}],
        "variants": [{"id": 47000000000000 + number, "price": "18.00", "available": True, "sku": f"PF{number}"}],
    }


def written(host, vendor, override=None, role="retailer"):
    rec = feed.shopify_product_to_record(
        product(vendor), domain=host, category_path="beauty/skincare", brand_override=override,
        currency="USD", source_role=role, retailer_name=host if role == "retailer" else None,
    )
    return rec["pdp"]["brand"]


# (host, vendor as the store sent it, spelling written). Every host/vendor pair is read off prod rows.
RETAILER_VENDORS = [
    ("dodoskin.com", "MISSHA", "Missha"), ("holiholic.com", "MISSHA", "Missha"),
    ("kbeautymakeup.com", "MISSHA", "Missha"), ("smkoreabeauty.com", "MISSHA", "Missha"),
    ("eyurs.com", "ROUND LAB", "Round Lab"), ("sokoglam.com", "ROUND LAB", "Round Lab"),
    ("cocomo.sg", "Round Lab", "Round Lab"),
    ("mastersbeautystore.com", "BOBBI BROWN", "Bobbi Brown"),
    ("ikatehouse.com", "MAYBELLINE", "Maybelline"),
    ("frendsbeauty.com", "Make Up For Ever", "MAKE UP FOR EVER"),
    ("frendsbeauty.com", "By Terry", "BY TERRY"), ("belleandblush.com", "BY TERRY", "BY TERRY"),
    ("cocomo.sg", "MEDICUBE", "MEDICUBE"), ("cocomo.sg", "SKIN1004", "SKIN1004"),
    ("cocomo.sg", "Anua", "Anua"), ("eyurs.com", "Anua", "Anua"),
    ("cocomo.sg", "Isntree", "Isntree"), ("eyurs.com", "Isntree", "Isntree"),
    ("cocomo.sg", "Beauty of Joseon", "Beauty of Joseon"), ("ohlolly.com", "Beauty of Joseon", "Beauty of Joseon"),
    ("cocomo.sg", "The Ordinary", "The Ordinary"), ("cocomo.sg", "Arencia", "Arencia"),
    ("cocomo.sg", "Genabelle", "Genabelle"), ("cocomo.sg", "Molvany", "Molvany"),
    ("cocomo.sg", "Centellian24", "Centellian24"), ("cocomo.sg", "Mixsoon", "Mixsoon"),
]


@pytest.mark.parametrize("host,vendor,want", RETAILER_VENDORS)
@pytest.mark.parametrize("override", [None, "__vendor_upper__", "__vendor_lower__"])
def test_a_retailer_vendor_is_written_under_its_brands_one_spelling(host, vendor, want, override):
    """Whatever case the store or the operator used, one spelling is written."""
    o = {None: None, "__vendor_upper__": vendor.upper(), "__vendor_lower__": vendor.lower()}[override]
    assert written(host, vendor, o) == want


# (brand store, vendor, --brand as the store's runs used it, spelling written). Both stored spellings occur on
# roundlab.com and skin1004.com themselves: the operator's --brand decided which, run by run.
BRAND_STORE_VENDORS = [
    ("misshaus.com", "Missha", "Missha", "Missha"), ("misshaus.com", "MISSHA", "Missha", "Missha"),
    ("roundlab.com", "ROUND LAB", "ROUND LAB", "Round Lab"), ("roundlab.com", "Round Lab", "Round Lab", "Round Lab"),
    ("skin1004.com", "Skin1004", "Skin1004", "SKIN1004"), ("skin1004.com", "SKIN1004", "SKIN1004", "SKIN1004"),
    ("medicube.us", "Medicube", None, "MEDICUBE"), ("mixsoon.us", "MIXSOON", None, "Mixsoon"),
    ("theordinary.com", "the ordinary", None, "The Ordinary"), ("arencia.us", "ARENCIA", None, "Arencia"),
    ("makeupforever.sg", "MAKE UP FOR EVER", None, "MAKE UP FOR EVER"),
    ("centellian24usa.com", "Centellian24", "Centellian24", "Centellian24"),
    ("anua.us", "Anua", None, "Anua"), ("isntree-global.com", "Isntree", None, "Isntree"),
    ("beautyofjoseon.com", "Beauty of Joseon", None, "Beauty of Joseon"),
]


@pytest.mark.parametrize("host,vendor,override,want", BRAND_STORE_VENDORS)
def test_a_brand_store_writes_the_same_spelling_as_the_retailers(host, vendor, override, want):
    assert written(host, vendor, override, role="brand_official") == want


@pytest.mark.parametrize("key", sorted(NEW_FAMILIES))
def test_a_new_family_moves_no_content_key(key):
    """Why these may reach the drain before the old rows are relabelled: every crawl-lane spelling stored for
    the family and its canonical normalise alike, so the written brand changes and the content_key does not."""
    stored = {s for s in CORPUS if feed._brand_key(s) == key and s != "Centellian 24"}  # a merchant row, not crawled
    canonical = feed.RETAILER_BRAND_CANONICAL[feed.RETAILER_BRAND_SPELLINGS[key]]
    assert len(stored) >= 1
    assert {normalize_brand(s) for s in stored | {canonical}} == {normalize_brand(canonical)}, stored


def _writes(vendors, role):
    out = {}
    for v in vendors:
        try:
            out[v] = written("store.example", v, None, role=role)
        except ValueError as exc:            # retailer mode refuses a vendor that is not maker evidence
            out[v] = f"REFUSED {str(exc)[:40]}"
    return out


# The ONLY corpus spellings whose written brand this change moves -- the stored minority spellings.
EXPECTED_CHANGES = {
    "MISSHA": "Missha", "MIXSOON": "Mixsoon", "ROUND LAB": "Round Lab", "ANUA": "Anua", "ARENCIA": "Arencia",
    "Skin1004": "SKIN1004", "Medicube": "MEDICUBE", "BEAUTY OF JOSEON": "Beauty of Joseon",
    "the ordinary": "The Ordinary", "ISNTREE": "Isntree", "CENTELLIAN24": "Centellian24",
    "Centellian 24": "Centellian24", "Make Up For Ever": "MAKE UP FOR EVER", "GENABELLE": "Genabelle",
    "MAYBELLINE": "Maybelline", "BOBBI BROWN": "Bobbi Brown", "MOLVANY": "Molvany", "By Terry": "BY TERRY",
}


@pytest.mark.parametrize("role", ["retailer", "brand_official"])
def test_over_every_stored_brand_spelling_only_the_intended_ones_change(role, monkeypatch):
    """No-change invariant over the 676 distinct brand spellings in prod (catalog rows + seeds): the resolver
    with these families vs without them. Neighbours that contain a family's letters -- "Round Lab US",
    "HUE_CALM® X Centellian24", "Innisfree US" -- and every other brand are written exactly as before."""
    after = _writes(CORPUS, role)
    monkeypatch.setattr(feed, "RETAILER_BRAND_SPELLINGS",
                        {k: v for k, v in feed.RETAILER_BRAND_SPELLINGS.items() if v not in NEW_FAMILIES})
    before = _writes(CORPUS, role)
    changed = {v: after[v] for v in CORPUS if after[v] != before[v]}
    assert changed == EXPECTED_CHANGES
    assert before["Round Lab US"] == after["Round Lab US"] == "Round Lab US"
    assert after["HUE_CALM® X Centellian24"] == "HUE_CALM® X Centellian24"


def test_the_corpus_is_the_measured_one():
    assert len(CORPUS) == 676 and len(set(CORPUS)) == 676
    for s in ("MISSHA", "Missha", "O HUI (오휘)", "Jung Saem Mool", "APIEU", "Supergoop", "Centellian 24"):
        assert s in CORPUS


# ---- identity-changing splits: NOT families yet (ORDER: relabel first) ------------------------------------------

@pytest.mark.parametrize("host,vendor,canonical", [
    ("smkoreabeauty.com", "O HUI (오휘)", "O HUI"),          # 20 retailer rows
    ("misshaus.com", "APIEU", "A'PIEU"),                      # 17 brand rows; product_key moves with it
    ("supergoop.com", "Supergoop", "Supergoop!"),             # 8 brand rows; no relabel tool reaches them
])
def test_an_identity_changing_split_is_not_a_family_until_its_rows_are_relabelled(host, vendor, canonical):
    """These spellings normalise apart, so a family would move content_key (and for APIEU the product_key) on
    the next re-crawl while the stored brand stays: services/brand_relabel.py ORDER. Adding one is a follow-up,
    after its relabel is applied and its holds resolved -- which must update this test on purpose."""
    assert make_content_key(vendor, "Serum", None) != make_content_key(canonical, "Serum", None)
    assert feed._retailer_brand_family(feed._brand_key(vendor)) is None
    role = "retailer" if host == "smkoreabeauty.com" else "brand_official"
    assert written(host, vendor, None, role=role) == vendor


def test_jungsaemmool_became_a_family_after_its_relabel():
    """The follow-up the test above asks for: smkoreabeauty.com's 5 "Jung Saem Mool" rows were relabelled first
    (scripts/relabel_retailer_brand.py --family jungsaemmool), so the family now writes the canonical."""
    assert feed._retailer_brand_family(feed._brand_key("Jung Saem Mool")) == "jungsaemmool"
    assert feed.RETAILER_BRAND_CANONICAL["jungsaemmool"] == "JUNGSAEMMOOL"
    row = feed.shopify_product_to_record(
        {"id": 9100777, "vendor": "Jung Saem Mool", "title": "Essential Skin Nutrition Cream", "handle": "jsm-cream",
         "product_type": "Cream", "body_html": "<p>Ingredients: Water, Glycerin</p>",
         "images": [{"src": "https://cdn.example/jsm.jpg"}],
         "variants": [{"id": 45000000007777, "price": "38.00", "available": True, "sku": "jsm-cream"}]},
        domain="smkoreabeauty.com", category_path="beauty/skincare", currency="USD", source_role="retailer",
        retailer_name="smkoreabeauty.com",
    )
    assert row["pdp"]["brand"] == "JUNGSAEMMOOL"

