"""P1 — audit -> commerce-index SKU mapping (pure, no DB).

The automatic index-population path: a fetched audit-product becomes a canonical
catalog_products row keyed by content_key + a stable URL identity.
"""

from services.audit_index_intake import (
    PLATFORM_URL_AUDIT,
    audit_product_to_index_fields,
    stable_source_id,
)
from services.catalog_identity import make_content_key


def _shopify_audit_product():
    return {
        "title": "Heartleaf 77% Soothing Toner",
        "raw_title": "Anua Heartleaf 77% Soothing Toner 250ml",
        "pdp_url": "https://www.anua.com/products/heartleaf-toner?variant=42",
        "vendor": "Anua",
        "product_type": "Toner",
        "attributes_raw": {"size": "250ml"},
    }


def test_maps_audit_product_to_canonical_fields():
    f = audit_product_to_index_fields("m_anua", _shopify_audit_product())
    assert f["merchant_id"] == "m_anua"
    assert f["platform"] == PLATFORM_URL_AUDIT
    assert f["title"] == "Heartleaf 77% Soothing Toner"
    assert f["brand"] == "Anua"
    assert f["content_key"] == make_content_key("Anua", "Heartleaf 77% Soothing Toner")
    assert f["canonical_url"].startswith("https://www.anua.com/products/heartleaf-toner")
    assert f["source_domain"] == "anua.com"  # www + scheme stripped
    assert f["product_type"] == "Toner"
    assert f["product_key"] == f"m_anua|{PLATFORM_URL_AUDIT}|{f['source_product_id']}"


def test_stable_source_id_dedups_url_variants():
    # trailing slash, www, scheme, query, fragment all normalize to the same id
    a = stable_source_id("https://www.anua.com/products/heartleaf-toner/")
    b = stable_source_id("http://anua.com/products/heartleaf-toner?utm=x#reviews")
    assert a == b == "anua.com/products/heartleaf-toner"
    # a different product is a different id
    assert stable_source_id("https://anua.com/products/cleanser") != a


def test_requires_title_and_url_identity():
    assert audit_product_to_index_fields("m1", {"pdp_url": "https://x.com/p"}) is None  # no title
    assert audit_product_to_index_fields("m1", {"title": "X"}) is None  # no URL identity
    assert audit_product_to_index_fields("", _shopify_audit_product()) is None  # no merchant
    assert audit_product_to_index_fields("m1", None) is None


def test_brand_optional():
    p = _shopify_audit_product()
    p.pop("vendor")
    f = audit_product_to_index_fields("m1", p)
    assert f["brand"] is None
    # content_key still mints from title alone? make_content_key needs brand -> None
    # (brand+title key); brandless seeds resolve via the identity gate downstream.
    assert "content_key" in f
