"""Variant identity must come from the merchant, or be absent.

The failure this pins is not hypothetical. Measured on prod 2026-09-07 across the 13,799
`platform='external_seed'` products: 11,811 catalog_skus rows carry
`source_variant_id = product_key`, another 1,473 carry `<external_product_id>-default`, and 519
products had a REAL merchant variant id in their crawl payload that ingestion threw away because
the product happened to have only one variant. The gateway's `isRestatedProductId` guard refuses
every one of the derived forms, which is correct — so those rows can never complete a purchase.
"""

import json

import pytest

from services.catalog_enrichment_agent.ingestion import _build_variant_sku_inserts
from services.variant_identity import (
    ABSENT,
    MERCHANT_ISSUED,
    PRODUCT_DERIVED,
    UNVERIFIABLE,
    is_merchant_issued_variant_id,
    merchant_issued_variants,
    variant_id_provenance,
)

SELLER = {"merchant_id": "m_tonymoly"}
PRODUCT_KEY = "ext:tonymoly-usa-im-lip-balm::a1b2c3d4"
BRAND = "TONYMOLY USA"
NAME = "I'm Lip Balm"


def _payload(variants, **extra):
    p = {
        "brand": BRAND,
        "product_name": NAME,
        "source_domain": "tonymoly.us",
        "currency": "USD",
        "variants": variants,
    }
    p.update(extra)
    return p


def _build(variants, **extra):
    return _build_variant_sku_inserts(
        product_key=PRODUCT_KEY,
        pdp_payload=_payload(variants, **extra),
        seller=SELLER,
        canonical_url="https://tonymoly.us/products/im-lip-balm",
    )


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vid",
    [
        "43062643884185",                              # bare Shopify bigint
        "30205665411107",
        "gid://shopify/ProductVariant/43062643884185",  # admin GID form
    ],
)
def test_real_storefront_ids_are_merchant_issued(vid):
    assert variant_id_provenance(vid, product_key=PRODUCT_KEY) == MERCHANT_ISSUED
    assert is_merchant_issued_variant_id(vid, product_key=PRODUCT_KEY)


@pytest.mark.parametrize(
    "vid,parent_kwargs",
    [
        # ingestion.py:841 — the storage token, 11,811 rows
        (PRODUCT_KEY, {"product_key": PRODUCT_KEY}),
        # onboard_external_brand_from_crawl.py:351 — 1,473 ids
        ("tonymoly_us_858145855914-default", {"product_id": "tonymoly_us_858145855914"}),
        ("seed-variant-default", {"product_id": "tonymoly_us_858145855914"}),
        # curated_brand_feed.py:919
        ("im-lip-balm:0", {"handle": "im-lip-balm"}),
        ("im-lip-balm:12", {"handle": "im-lip-balm"}),
        # beauty_external_ranking.py:535
        ("tonymoly_us_858145855914_1", {"product_id": "tonymoly_us_858145855914"}),
        # a canonical/single restatement
        (PRODUCT_KEY + "-canonical", {"product_key": PRODUCT_KEY}),
    ],
)
def test_ids_derived_from_the_product_are_refused(vid, parent_kwargs):
    assert variant_id_provenance(vid, **parent_kwargs) == PRODUCT_DERIVED
    assert not is_merchant_issued_variant_id(vid, **parent_kwargs)


def test_a_hex_digest_of_the_product_key_is_refused():
    """166 prod ids are a hex digest. Caught by COMPUTING the digest, not by
    sniffing the alphabet — so a genuine hex-shaped merchant SKU is not condemned."""
    import hashlib

    digest = hashlib.sha256(PRODUCT_KEY.encode()).hexdigest()[:12]
    assert variant_id_provenance(digest, product_key=PRODUCT_KEY) == PRODUCT_DERIVED


def test_an_unrelated_hex_shaped_id_is_not_called_derived():
    """The positive counterpart to the digest test: refusing every hex string would
    be a different bug. It is UNVERIFIABLE (still not spendable), never PRODUCT_DERIVED."""
    assert variant_id_provenance("deadbeefcafe", product_key=PRODUCT_KEY) == UNVERIFIABLE


def test_a_numeric_product_id_is_not_laundered_into_identity():
    """Derivation is checked BEFORE shape. A numeric id restated from a numeric
    product id must not pass merely because digits look like a Shopify id."""
    assert variant_id_provenance("858145855914", product_id="858145855914") == PRODUCT_DERIVED


def test_absent_and_unplaceable_are_both_unspendable():
    assert variant_id_provenance("") == ABSENT
    assert variant_id_provenance(None) == ABSENT
    assert variant_id_provenance("TM-LIP-01") == UNVERIFIABLE
    for v in ("", None, "TM-LIP-01"):
        assert not is_merchant_issued_variant_id(v)


def test_merchant_issued_variants_keeps_only_spendable_rows():
    kept = merchant_issued_variants(
        [
            {"variant_id": "43062643884185", "title": "Peach"},
            {"variant_id": "tonymoly_us_1-default", "title": "Default"},
            {"id": "30205665411107", "title": "Berry"},
            {"variant_id": "", "title": "Blank"},
            "not a dict",
        ],
        product_id="tonymoly_us_1",
    )
    assert [k.get("title") for k in kept] == ["Peach", "Berry"]


# ---------------------------------------------------------------------------
# The writer — these are the rows that reach checkout
# ---------------------------------------------------------------------------


def test_a_lone_real_variant_now_gets_its_own_sku():
    """REGRESSION. Fails against main, which returns [] for len(variants) < 2 and
    so discards a merchant id it was holding. 519 prod products are in this state."""
    rows = _build([{"variant_id": "43062643884185", "title": "Single item", "sku": "TM-01"}])
    assert len(rows) == 1
    assert rows[0]["source_variant_id"] == "43062643884185"
    assert rows[0]["product_key"] == PRODUCT_KEY
    assert json.loads(rows[0]["sku_payload"])["variant_id_provenance"] == MERCHANT_ISSUED


def test_a_lone_fabricated_variant_still_gets_nothing():
    """The other half. Lifting the count gate must not mint a decoy SKU beside the
    canonical row for the 1,608 products whose only variant id we invented."""
    assert _build([{"variant_id": "tonymoly_us_858145855914-default", "title": NAME}]) == []
    assert _build([{"variant_id": PRODUCT_KEY, "title": NAME}]) == []
    assert _build([{"variant_id": "", "title": NAME}]) == []


def test_a_lone_variant_titled_like_its_parent_is_admitted_on_a_real_id():
    """Title collision is NOT the test — provenance is. A genuine single-variant
    Shopify product legitimately repeats the product title (40 such rows on prod)."""
    rows = _build([{"variant_id": "43062643884185", "title": NAME}])
    assert len(rows) == 1


def test_multi_variant_admission_is_unchanged_and_stamped():
    """Multi-variant rows also drive the PDP shade selector, so their admission rule
    stays 'any non-empty id' — tightening it here would drop display data that has
    nothing to do with checkout. Provenance is recorded either way."""
    rows = _build(
        [
            {"variant_id": "43062643884185", "title": "Peach"},
            {"variant_id": "TM-LIP-BERRY", "title": "Berry"},
        ]
    )
    assert len(rows) == 2
    got = {r["source_variant_id"]: json.loads(r["sku_payload"])["variant_id_provenance"] for r in rows}
    assert got == {"43062643884185": MERCHANT_ISSUED, "TM-LIP-BERRY": UNVERIFIABLE}


def test_multi_variant_blank_ids_are_still_skipped():
    rows = _build(
        [
            {"variant_id": "43062643884185", "title": "Peach"},
            {"variant_id": "", "title": "Berry"},
        ]
    )
    assert [r["source_variant_id"] for r in rows] == ["43062643884185"]


# ---------------------------------------------------------------------------
# The promoter — Stage 2b-ii, the lane that would run the backfill
# ---------------------------------------------------------------------------


def test_promoter_stamps_provenance_without_gating_admission():
    """The promoter exists to render shade swatches, so a variant whose id we cannot
    place is still admitted — dropping it would blank the selector. What must not happen
    is that row reaching a buyer looking like merchant identity, so the provenance rides
    on it. 1,645 of the 2,803 promotable prod products are in the untrusted case."""
    from services.catalog_variant_promoter import build_variant_row

    primary = {
        "product_key": PRODUCT_KEY,
        "merchant_id": "external_seed",
        "platform": "external_seed",
        "source_product_id": "tonymoly-usa-im-lip-balm",
    }
    real = build_variant_row(
        variant={"variant_id": "43062643884185", "title": "Peach"}, primary=primary
    )
    minted = build_variant_row(
        variant={"variant_id": "tonymoly-usa-im-lip-balm-default", "title": "Default"},
        primary=primary,
    )
    assert real.sku_payload["variant_id_provenance"] == MERCHANT_ISSUED
    assert minted.sku_payload["variant_id_provenance"] == PRODUCT_DERIVED
    # both still promoted — the selector keeps its swatches
    assert real.source_variant_id and minted.source_variant_id


def test_promoter_does_not_mutate_the_caller_s_variant_dict():
    """sku_payload used to BE the caller's dict. Stamping into it in place would edit
    seed_data's variant array under every other reader of the same object."""
    from services.catalog_variant_promoter import build_variant_row

    variant = {"variant_id": "43062643884185", "title": "Peach"}
    build_variant_row(
        variant=variant,
        primary={
            "product_key": PRODUCT_KEY,
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_product_id": "tonymoly-usa-im-lip-balm",
        },
    )
    assert "variant_id_provenance" not in variant
