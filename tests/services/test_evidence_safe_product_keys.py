"""Identity-confidence gate (move #1, slice 1): evidence_safe_product_keys scopes
the agent_pdp_view evidence fetch so a non-unique content_key collision can never
bake Brand A's substantiated claim onto Brand B's served row.

A no-GTIN content_key collapses to brand+title and is DELIBERATELY non-unique, so
a cluster can merge two distinct products. The gate restricts the evidence fetch
to the canonical member unless the cluster is provably one product.
"""

from services.agent_pdp_view_assembler import (
    evidence_safe_product_keys,
    pick_canonical,
)


def _p(pk, brand, title, sig=None, primary=False):
    return {
        "product_key": pk,
        "brand": brand,
        "title": title,
        "pivota_signature_id": sig,
        "group_is_primary": primary,
    }


def _sku(barcode=None):
    return {"barcode": barcode}


def test_single_member_returns_all():
    products = [_p("m1|x", "Anua", "Heartleaf Toner")]
    assert evidence_safe_product_keys(products, []) == ["m1|x"]


def test_gtin_resolved_returns_all_even_if_brands_differ():
    # a real barcode folded the cluster -> the cross-merchant merge is intended
    products = [_p("m1|a", "Anua", "Toner"), _p("m2|b", "Other Brand", "Different")]
    skus = [_sku("00012345678905")]  # pick_gtin13 accepts -> GTIN-resolved
    assert set(evidence_safe_product_keys(products, skus)) == {"m1|a", "m2|b"}


def test_same_brand_and_title_multi_merchant_returns_all():
    # legit: one real product sold by two merchants, no GTIN -> NOT a collision
    products = [
        _p("m1|a", "Anua", "Heartleaf 77 Toner"),
        _p("m2|b", "ANUA", "Heartleaf 77  Toner"),  # case/space differences only
    ]
    assert set(evidence_safe_product_keys(products, [])) == {"m1|a", "m2|b"}


def test_three_merchant_same_brand_not_oversuppressed():
    # red-team regression: a thin canonical member must NOT sink a legit cluster
    products = [
        _p("m1|a", "Anua", "Heartleaf Toner"),
        _p("m2|b", "Anua", "Heartleaf Toner"),
        _p("m3|c", "Anua", "Heartleaf Toner"),
    ]
    assert len(set(evidence_safe_product_keys(products, []))) == 3


def test_brand_collision_restricts_to_canonical_only():
    # THE leak: two DISTINCT products collided on a no-GTIN content_key.
    # Evidence must scope to the canonical member, never the whole cluster.
    a = _p("m1|a", "Anua", "Heartleaf Toner", sig="sig_a")
    b = _p("m2|b", "Beauty of Joseon", "Glow Serum")
    out = evidence_safe_product_keys([a, b], [])
    assert len(out) == 1
    # red-team regression: must be the canonical member, NOT just "the first" —
    # an approved-canonical + thin-other cluster cannot leak the other's claim
    assert out == [pick_canonical([a, b])["product_key"]]


def test_collision_with_empty_brand_is_not_treated_as_one_brand():
    # a blank brand must not collapse two products into "one brand" -> still gated
    a = _p("m1|a", "", "Mystery Toner")
    b = _p("m2|b", "Anua", "Heartleaf Toner")
    out = evidence_safe_product_keys([a, b], [])
    assert len(out) == 1
    assert out == [pick_canonical([a, b])["product_key"]]
