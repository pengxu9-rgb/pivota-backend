"""P1 store-less URL intake — brand attribution for a URL-audit seed.

The capability (submit a product URL -> read + audit -> seed the index) already
exists via POST /url-readiness, with no domain constraint. The gap this covers:
an explicitly-declared brand must WIN over a retailer PDP's fetched vendor, so a
store-less brand pointing at its Amazon / Olive Young listing indexes under its
OWN brand (and content_key) and can then claim it.
"""

from services.audit_index_intake import (
    audit_product_to_index_fields,
    resolve_seed_vendor,
)
from services.catalog_identity import make_content_key


def test_declared_brand_overrides_fetched_vendor():
    # retailer PDP fetched a misleading vendor; the merchant declared its brand
    assert (
        resolve_seed_vendor(
            fetched_vendor="Amazon.com", declared_brand="Anua", fallback_brand="x"
        )
        == "Anua"
    )


def test_fetched_vendor_used_when_no_declared_brand():
    assert (
        resolve_seed_vendor(
            fetched_vendor="Anua", declared_brand=None, fallback_brand="fallback"
        )
        == "Anua"
    )


def test_fallback_brand_when_no_vendor_and_no_declared():
    assert (
        resolve_seed_vendor(
            fetched_vendor="  ", declared_brand="", fallback_brand="Resolved Brand"
        )
        == "Resolved Brand"
    )


def test_none_when_nothing_resolves():
    assert (
        resolve_seed_vendor(
            fetched_vendor=None, declared_brand=None, fallback_brand=None
        )
        is None
    )


def test_seed_brand_and_content_key_follow_declared_brand():
    # End-to-end on the mapping: a retailer URL whose vendor would be wrong gets
    # the declared brand on BOTH `brand` and the brand+title content_key, so the
    # store-less brand can find + claim its product in the index.
    product = {
        "title": "Heartleaf 77% Soothing Toner",
        "vendor": resolve_seed_vendor(
            fetched_vendor="Amazon Seller XYZ", declared_brand="Anua"
        ),
        "pdp_url": "https://www.amazon.com/dp/B0ABC123",
    }
    fields = audit_product_to_index_fields("m1", product)
    assert fields["brand"] == "Anua"
    assert fields["content_key"] == make_content_key(
        "Anua", "Heartleaf 77% Soothing Toner"
    )
    assert fields["platform"] == "url_audit"
    assert fields["canonical_url"] == "https://www.amazon.com/dp/B0ABC123"
