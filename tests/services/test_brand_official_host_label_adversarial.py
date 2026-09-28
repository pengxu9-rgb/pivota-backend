"""The host-label arm of resolve_record_brand on regional brand stores; no network or DB.

Measured 2026-09-26 on retailer-ingest job rij_c04316a4c9594359ba54d4055c18cb93 (us.sandandsky.com):
the host's first label is "us", and the arm had no floor on it, so any vendor containing "us" took
the job brand -- `Sand and Sky US` by coincidence, while `Sand and Sky INT` / `AU` did not.
"""
import pytest

from services import curated_brand_feed as feed

SAND_AND_SKY_VENDORS = ["Sand and Sky US", "Sand and Sky INT", "Sand & Sky CA", "Sand & Sky US",
                        "Sand and Sky", "Sand & Sky", "Sand and Sky AU"]
METRO_VENDOR = "Metro Singapore Departmental Store - Celebrating 69 Years in SG"


def product(vendor):
    return {
        "id": 9000002, "handle": "self-tan-mousse", "title": "Self Tan Mousse", "vendor": vendor,
        "product_type": "Self Tanner", "images": [{"src": "https://cdn.example/mousse.jpg"}],
        "variants": [{"id": 45000000000002, "price": "39.00", "available": True}],
    }


def brand_written(host, vendor, override):
    rec = feed.shopify_product_to_record(product(vendor), domain=host, category_path="beauty",
                                         currency="USD", brand_override=override)
    return rec["pdp"]["brand"]


@pytest.mark.parametrize("host,vendor,override", [
    ("us.sandandsky.com", "Lush", "Sand & Sky"),
    ("us.sandandsky.com", "Aesop Australia", "Sand & Sky"),
    # Not a listed prefix, so only the floor stands between these labels and the vendor.
    ("hk.sandandsky.com", "Sulwhasoo HK", "Sand & Sky"),
    ("jp.frankbody.com", "Shiseido JP", "Frank Body"),
    ("hm.com", "Rohm Beauty", "H&M Beauty"),
])
def test_a_short_host_label_never_relabels_an_unrelated_vendor(host, vendor, override):
    assert feed.resolve_record_brand(vendor, override, host) == (vendor, "vendor_disagrees")
    assert brand_written(host, vendor, override) == vendor


@pytest.mark.parametrize("host", ["metro.com.sg", "www.metro.com.sg", "shop.metro.com.sg", "us.metro.com.sg"])
def test_the_store_label_behind_a_regional_prefix_still_names_the_store(host):
    """The arm's job (the metro.com.sg store-name vendor) must survive a www./shop./us. host: before,
    the label read was "www"/"shop"/"us" and the store's own name went into the brand column."""
    assert feed.resolve_record_brand(METRO_VENDOR, "ETUDE HOUSE", host) == ("ETUDE HOUSE", "override_vendor_is_store")
    assert brand_written(host, METRO_VENDOR, "ETUDE HOUSE") == "ETUDE HOUSE"


@pytest.mark.parametrize("host,label", [
    ("us.sandandsky.com", "sandandsky"), ("us.frankbody.com", "frankbody"), ("us.koraorganics.com", "koraorganics"),
    ("www.brand.co.uk", "brand"), ("sandandsky.com", "sandandsky"),
    # Never skip onto a public second level or a TLD: the store label there IS the first one.
    ("shop.com.sg", "shop"), ("store.co.uk", "store"), ("us.com", "us"), ("https://www.metro.com.sg/", "metro"),
])
def test_the_store_host_label(host, label):
    assert feed._store_host_label(host) == label


@pytest.mark.parametrize("override", ["Sand & Sky", "Sand and Sky"])
def test_the_measured_sand_and_sky_store_writes_one_brand(override):
    """Every vendor the store publishes is the one brand, whichever spelling the job uses. Before:
    "Sand & Sky" wrote 4 brands (itself, "Sand and Sky", "... AU", "... INT"), each its own flag."""
    written = {brand_written("us.sandandsky.com", v, override) for v in SAND_AND_SKY_VENDORS}
    assert written == {override}


def test_an_ampersand_is_the_word_and():
    assert feed._brand_key("Sand & Sky") == feed._brand_key("Sand and Sky") == "sandandsky"
    assert feed._brand_key("Dolce & Gabbana") == "dolceandgabbana"


def test_every_listed_spelling_is_its_own_brand_key():
    """The & rule cannot move a RETAILER_BRAND_SPELLINGS key: each is already a fixed point."""
    assert [k for k in feed.RETAILER_BRAND_SPELLINGS if feed._brand_key(k) != k] == []
