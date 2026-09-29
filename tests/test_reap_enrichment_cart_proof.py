"""services/reap_enrichment_cart_proof.py -- pure checks for buying an ENRICHMENT row by cart link.

Every fixture is a LIVE shape from the option-2 census (read-only prod jobs q1-q3, 2026-09-29):
  * tarte     brand-direct, sole-variant (flat blush brush, variant 52729903251478, $34) and a
              3-shade product (maneater silk stick bronzer);
  * MAC       folded shades: each shade is its own Shopify product, named by
              sku_payload.source_handle (Studio Fix Fluid NC10 = 54057377923267 under
              `...-nc10`), plus a canonical-only product (Powder Kiss Chestnut);
  * bluemercury  an `ext:retailer:` row whose variant skus are keyed `::v:retailer-<hex>`, so the
              numeric id lives only in source_variant_id (32903948173387);
  * jsmbeauty.sg an SGD row whose catalog offer drifted from the storefront.
Each rule has an accepting case and at least one refusing case.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import services.reap_enrichment_cart_proof as m
from services.reap_enrichment_cart_proof import (
    derive_enrichment_seller,
    enrichment_offer_price_ok,
    storefront_page,
    verify_enrichment_cart_proof,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
CHECKED = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)
SS = "catalog_enrichment_agent_v1"


# ── live-shaped rows ────────────────────────────────────────────────────────────────────────────


def product(key, url, *, domain=None, brand=None, merchant_id=None, **over):
    row = {
        "product_key": key,
        "source_system": SS,
        "canonical_url": url,
        "source_domain": domain if domain is not None else url.split("/")[2],
        "brand": brand,
        "merchant_id": merchant_id,
    }
    row.update(over)
    return row


def sku(key, svid, *, handle=None, payload=None):
    body = {"agent_version": SS, "variant_id": svid, "variant_id_provenance": "merchant_issued"}
    if handle is not None:
        body["source_handle"] = handle
    return {"sku_key": key, "source_variant_id": svid,
            "sku_payload": json.dumps(payload if payload is not None else body)}


def placeholder(product_key, svid=None):
    return {"sku_key": product_key + "::canonical", "source_variant_id": svid or product_key,
            "sku_payload": json.dumps({"agent_version": SS})}


def proof(prod, sk, *, host, handle, variant, lvc=1, price=3400, currency="USD", **over):
    row = {
        "product_key": prod["product_key"],
        "sku_key": sk["sku_key"],
        "shop_host": host,
        "handle": handle,
        "shopify_product_id": "8123456789012",
        "variant_id": variant,
        "live_variant_count": lvc,
        "available": True,
        "live_price_minor": price,
        "currency": currency,
        "source": "products_json_v1",
        "checked_at": CHECKED,
        "outcome": "ok",
    }
    row.update(over)
    return row


TARTE_PK = "ext:tarte-flat-blush-brush::4e837abf"
TARTE = product(TARTE_PK, "https://tartecosmetics.com/products/flat-blush-brush",
                brand="tarte", merchant_id="merch_obs_c75008da8d4366d6")
TARTE_SKU = sku(TARTE_PK + "::v:52729903251478", "52729903251478")
TARTE_PROOF = proof(TARTE, TARTE_SKU, host="tartecosmetics.com", handle="flat-blush-brush",
                    variant="52729903251478", price=3400)

BRONZER_PK = "ext:tarte-maneater-silk-stick-bronzer::3af55d78"
BRONZER = product(BRONZER_PK, "https://tartecosmetics.com/products/maneater-silk-stick-bronzer",
                  brand="tarte", merchant_id="merch_obs_c75008da8d4366d6")
BRONZER_ECLIPSE = sku(BRONZER_PK + "::v:52789595242518", "52789595242518")
BRONZER_PROOF = proof(BRONZER, BRONZER_ECLIPSE, host="tartecosmetics.com",
                      handle="maneater-silk-stick-bronzer", variant="52789595242518", lvc=3, price=2900)

MAC_PK = "ext:mac-cosmetics-studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control::63815275"
MAC = product(MAC_PK, "https://maccosmetics.com/products/studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control",
              brand="MAC Cosmetics", merchant_id="merch_obs_28b3afd14edf211f")
MAC_NC10_HANDLE = "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control-nc10"
MAC_NC10 = sku(MAC_PK + "::v:54057377923267", "54057377923267", handle=MAC_NC10_HANDLE)
MAC_NC10_PROOF = proof(MAC, MAC_NC10, host="maccosmetics.com", handle=MAC_NC10_HANDLE,
                       variant="54057377923267", price=3900)

CHESTNUT_PK = "ext:mac-cosmetics-powder-kiss-liquid-lipcolour-chestnut::127b834c"
CHESTNUT = product(CHESTNUT_PK, "https://maccosmetics.com/products/powder-kiss-liquid-lipcolour-chestnut",
                   brand="MAC Cosmetics", merchant_id="merch_obs_28b3afd14edf211f")
CHESTNUT_CANON = placeholder(CHESTNUT_PK)
CHESTNUT_PROOF = proof(CHESTNUT, CHESTNUT_CANON, host="maccosmetics.com",
                       handle="powder-kiss-liquid-lipcolour-chestnut", variant="54057001234567", price=2800)

BM_PK = "ext:retailer:37065ae3db2eba22d6b959e19dbb1284"
BM = product(BM_PK, "https://bluemercury.com/products/nars-16-blush-brush",
             brand="NARS", merchant_id="merch_obs_a2e07b1e8a08148b")
BM_SKU = sku(BM_PK + "::v:retailer-04135f0544a19b871664b06d14c8bf6b", "32903948173387")
BM_PROOF = proof(BM, BM_SKU, host="bluemercury.com", handle="nars-16-blush-brush",
                 variant="32903948173387", price=3600)

KIEHLS_PK = "ext:retailer:41087cfb3a611475c4b50d4b097450f2"
KIEHLS = product(KIEHLS_PK, "https://bluemercury.com/products/kiehls-since-1851-facial-fuel-energizing-face-wash",
                 brand="Kiehl's Since 1851", merchant_id="merch_obs_a2e07b1e8a08148b")
KIEHLS_8OZ = sku(KIEHLS_PK + "::v:retailer-e440397ba11ff2d25ba45ea816bb0351", "39763585630283")
KIEHLS_PROOF = proof(KIEHLS, KIEHLS_8OZ, host="bluemercury.com",
                     handle="kiehls-since-1851-facial-fuel-energizing-face-wash",
                     variant="39763585630283", lvc=2, price=2800)

JSM_PK = "ext:jungsaemmool-essential-mool-stick-glow::c9ca9cdb"
JSM = product(JSM_PK, "https://jsmbeauty.sg/products/essential-mool-stick-glow",
              brand="JUNGSAEMMOOL", merchant_id="merch_obs_88382424262f3e0f")
JSM_SKU = sku(JSM_PK + "::v:49018740343105", "49018740343105")
JSM_PROOF = proof(JSM, JSM_SKU, host="jsmbeauty.sg", handle="essential-mool-stick-glow",
                  variant="49018740343105", price=3854, currency="SGD")

#: derive_product_key's longest shape: 'ext:' + 200 + '::' + 8 = 214 characters.
LONG_PK = (
    "ext:tarte-shape-tape-ultra-creamy-concealer-with-hydrating-hyaluronic-acid-and-mango-seed-"
    "butter-for-all-day-wear-ultra-creamy-concealer-with-hydrating-hyaluronic-acid-and-mango-seed-"
    "butter-for-all-day-we::578c4614"
)
LONG_SPID = ("tarte-shape-tape-ultra-creamy-concealer-with-hydrating-hyaluronic-acid-and-mango-seed-"
             "butter-for-all-day-wear-ultra-cre-578c4614")
LONG = product(LONG_PK, "https://tartecosmetics.com/products/shape-tape-ultra-creamy-concealer",
               brand="tarte", merchant_id="merch_obs_c75008da8d4366d6")


def verify(prod, sk, pr, *, count=None, **kw):
    """`count` is catalog_variant_sku_count. Default: 0 for a placeholder (a canonical-only
    product), 1 otherwise (the sku itself). Tests that are ABOUT the count pass it."""
    if count is None:
        is_canon = isinstance(sk, dict) and str(sk.get("sku_key", "")).endswith("::canonical")
        count = 0 if is_canon else 1
    return verify_enrichment_cart_proof(prod, sk, pr, catalog_variant_sku_count=count,
                                        now=kw.pop("now", NOW), **kw)


def with_(row, **over):
    out = dict(row)
    out.update(over)
    return out


# ── accepted: the live shapes ───────────────────────────────────────────────────────────────────


def test_tarte_sole_variant_is_accepted():
    assert verify(TARTE, TARTE_SKU, TARTE_PROOF) == (True, "52729903251478", "sole_variant")


def test_the_placeholder_of_a_product_with_a_variant_sku_is_refused():
    """tarte flat blush brush carries `::canonical` AND `::v:52729903251478`: the cart must name the
    variant sku, never the placeholder, even though the handle has one variant."""
    canon = placeholder(TARTE_PK)
    pr = with_(TARTE_PROOF, sku_key=canon["sku_key"])
    assert verify(TARTE, canon, pr, count=1) == (False, None, "placeholder_has_variant_skus")
    assert verify(TARTE, canon, pr, count=0) == (True, "52729903251478", "sole_variant")


def test_the_mac_parent_stub_is_never_bought_through_the_placeholder():
    """MAC's 99 folded families: canonical_url is the PARENT handle, whose one variant is a
    'Default Title' stub (P2000_...), while the placeholder is priced as the first shade. The proof
    of the parent handle says lvc=1 -- the catalog's shade skus are what refuse it."""
    canon = placeholder(MAC_PK)
    parent = proof(MAC, canon, host="maccosmetics.com",
                   handle="studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control",
                   variant="54057000000001", lvc=1, price=3900)
    assert verify(MAC, canon, parent, count=3) == (False, None, "placeholder_has_variant_skus")


def test_a_native_three_shade_product_with_two_sold_out_refuses_its_placeholder():
    """live_variant_count counts ALL variants on the handle. Two sold-out shades still make the
    placeholder ambiguous even though only one shade can be bought."""
    canon = placeholder(BRONZER_PK)
    pr = with_(BRONZER_PROOF, sku_key=canon["sku_key"], live_variant_count=3, available=True)
    assert verify(BRONZER, canon, pr, count=0) == (False, None, "placeholder_multi_variant")


@pytest.mark.parametrize("count", [-1, True, "0", None, 0.0])
def test_a_malformed_catalog_variant_sku_count_is_a_caller_bug(count):
    with pytest.raises(ValueError):
        verify_enrichment_cart_proof(CHESTNUT, CHESTNUT_CANON, CHESTNUT_PROOF,
                                     catalog_variant_sku_count=count, now=NOW)


def test_tarte_shade_of_a_three_shade_product_is_named():
    assert verify(BRONZER, BRONZER_ECLIPSE, BRONZER_PROOF) == (True, "52789595242518", "named_variant")


def test_mac_folded_shade_is_proven_under_its_own_handle():
    assert verify(MAC, MAC_NC10, MAC_NC10_PROOF) == (True, "54057377923267", "sole_variant")
    named = with_(MAC_NC10_PROOF, live_variant_count=2)
    assert verify(MAC, MAC_NC10, named) == (True, "54057377923267", "named_variant")


def test_mac_folded_shade_is_refused_under_the_canonical_handle():
    """The NC10 variant does not live under the family's canonical handle."""
    pr = with_(MAC_NC10_PROOF, handle="studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control")
    assert verify(MAC, MAC_NC10, pr) == (False, None, "handle_mismatch")


def test_mac_canonical_only_product_is_accepted_as_sole_variant():
    assert verify(CHESTNUT, CHESTNUT_CANON, CHESTNUT_PROOF) == (True, "54057001234567", "sole_variant")


def test_bluemercury_retailer_sku_uses_its_source_variant_id_not_its_key():
    assert verify(BM, BM_SKU, BM_PROOF) == (True, "32903948173387", "sole_variant")
    assert verify(KIEHLS, KIEHLS_8OZ, KIEHLS_PROOF) == (True, "39763585630283", "named_variant")


def test_bluemercury_other_size_is_refused_against_this_sizes_proof():
    sixteen = sku(KIEHLS_PK + "::v:retailer-559902939bce237c4a5b8826d342a649", "39763585663051")
    pr = with_(KIEHLS_PROOF, sku_key=sixteen["sku_key"])
    assert verify(KIEHLS, sixteen, pr) == (False, None, "variant_mismatch")


def test_jsm_sgd_row_is_accepted():
    assert verify(JSM, JSM_SKU, JSM_PROOF) == (True, "49018740343105", "sole_variant")


def test_a_product_key_longer_than_128_characters():
    """The placeholder's source_variant_id is then the bounded source_product_id, not the key, so
    the placeholder is known by its KEY. A variant sku of a 214-char key still spells its id."""
    assert len(LONG_PK) == 214
    canon = placeholder(LONG_PK, svid=LONG_SPID)
    pr = proof(LONG, canon, host="tartecosmetics.com", handle="shape-tape-ultra-creamy-concealer",
               variant="52729903251478")
    assert verify(LONG, canon, pr, count=0) == (True, "52729903251478", "sole_variant")
    shade = sku(LONG_PK + "::v:52729903251478", "52729903251478")
    assert len(shade["sku_key"]) > 128
    pr = with_(pr, sku_key=shade["sku_key"], live_variant_count=4)
    assert verify(LONG, shade, pr) == (True, "52729903251478", "named_variant")
    assert verify(LONG, canon, with_(pr, sku_key=canon["sku_key"]), count=0) == (
        False, None, "placeholder_multi_variant")


# ── refused: the proof's own state ──────────────────────────────────────────────────────────────


def test_a_missing_proof_is_refused():
    assert verify(TARTE, TARTE_SKU, None) == (False, None, "proof_missing")


def test_a_revoked_404_proof_is_refused():
    pr = with_(TARTE_PROOF, outcome="revoked_404", available=None)
    assert verify(TARTE, TARTE_SKU, pr) == (False, None, "proof_outcome_not_ok")
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, outcome="variant_gone")) == (False, None, "proof_outcome_not_ok")
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, outcome="OK")) == (False, None, "proof_outcome_not_ok")


def test_a_proof_older_than_72_hours_is_refused():
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, checked_at=NOW - timedelta(hours=72, seconds=1))) == (
        False, None, "proof_stale")
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, checked_at=NOW - timedelta(hours=72)))[0] is True
    # max_age is the caller's to tighten.
    assert verify(TARTE, TARTE_SKU, TARTE_PROOF, max_age=timedelta(hours=1)) == (False, None, "proof_stale")


def test_a_proof_from_the_future_is_refused():
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, checked_at=NOW + timedelta(seconds=1))) == (
        False, None, "proof_from_future")
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, checked_at=NOW))[0] is True


@pytest.mark.parametrize("checked_at", [
    datetime(2026, 9, 29, 5, 0),  # naive
    "2026-09-29T05:00:00",        # naive text
    "yesterday",
    None,
    1759122000,
])
def test_an_unreadable_or_naive_checked_at_is_refused(checked_at):
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, checked_at=checked_at)) == (False, None, "proof_malformed")


@pytest.mark.parametrize("checked_at", ["2026-09-29T05:00:00+00:00", "2026-09-29T05:00:00Z",
                                        "2026-09-29T13:00:00+08:00"])
def test_an_aware_iso_checked_at_is_read(checked_at):
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, checked_at=checked_at))[0] is True


@pytest.mark.parametrize("available", [False, None, 0, "true", "1", 2])
def test_an_unavailable_variant_is_refused(available):
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, available=available)) == (False, None, "proof_unavailable")


def test_sqlite_true_is_available():
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, available=1))[0] is True


@pytest.mark.parametrize("source", ["seed_label_match", None, "", "PRODUCTS_JSON_V1"])
def test_a_proof_not_read_by_the_proof_job_is_refused(source):
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, source=source)) == (False, None, "proof_source_unknown")


def test_both_proof_sources_are_accepted():
    for source in ("products_json_v1", "products_js_v1"):
        assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, source=source))[0] is True


@pytest.mark.parametrize("lvc", [0, -1, 101, None, "1", True, 1.0])
def test_a_malformed_live_variant_count_is_refused(lvc):
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, live_variant_count=lvc)) == (False, None, "proof_malformed")


def test_the_shopify_variant_cap_is_accepted():
    pr = with_(BRONZER_PROOF, live_variant_count=100)
    assert verify(BRONZER, BRONZER_ECLIPSE, pr) == (True, "52789595242518", "named_variant")


@pytest.mark.parametrize("variant", [None, "", "gid://shopify/ProductVariant/52729903251478",
                                     " 52729903251478", "5272990325147a", 52729903251478,
                                     "٥٢", "1" * 21])
def test_a_proof_variant_that_is_not_a_bare_numeric_id_is_refused(variant):
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, variant_id=variant)) == (False, None, "proof_malformed")


# ── refused: the proof is not for this row ──────────────────────────────────────────────────────


def test_a_proof_for_another_product_or_sku_is_refused():
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, product_key=BRONZER_PK)) == (False, None, "proof_key_mismatch")
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, sku_key=TARTE_PK + "::canonical")) == (
        False, None, "proof_key_mismatch")


def test_a_sku_whose_id_is_not_the_proofs_variant_is_refused():
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, variant_id="52729903251479")) == (False, None, "variant_mismatch")
    # Named mode too: the eclipse sku against a proof of the midnight shade.
    assert verify(BRONZER, BRONZER_ECLIPSE, with_(BRONZER_PROOF, variant_id="52789595275286")) == (
        False, None, "variant_mismatch")


# ── refused: hosts ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shop_host", [
    "evil-tarte.com", "tartecosmetics.com.evil.io", "shop.tartecosmetics.com", "www.shop.tartecosmetics.com",
    "wwwtartecosmetics.com", "artecosmetics.com", "tartecosmetics.co", "", None, 42,
])
def test_a_proof_from_any_other_host_is_refused(shop_host):
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, shop_host=shop_host)) == (False, None, "host_mismatch")


@pytest.mark.parametrize("shop_host", ["www.tartecosmetics.com", "TarteCosmetics.com", "tartecosmetics.com."])
def test_a_proof_from_the_same_storefront_after_one_www_fold_is_accepted(shop_host):
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, shop_host=shop_host))[0] is True


def test_the_www_fold_is_the_same_on_all_three_sides():
    www = with_(TARTE, canonical_url="https://www.tartecosmetics.com/products/flat-blush-brush")
    assert verify(www, TARTE_SKU, TARTE_PROOF)[0] is True
    assert verify(www, TARTE_SKU, with_(TARTE_PROOF, shop_host="www.tartecosmetics.com"))[0] is True
    assert verify(with_(www, source_domain="www.tartecosmetics.com"), TARTE_SKU, TARTE_PROOF)[0] is True
    assert verify(with_(www, source_domain="shop.tartecosmetics.com"), TARTE_SKU, TARTE_PROOF) == (
        False, None, "host_mismatch")


@pytest.mark.parametrize("url", [
    "https://evil-tarte.com/products/flat-blush-brush",
    "https://tartecosmetics.com.evil.io/products/flat-blush-brush",
])
def test_a_lookalike_canonical_host_is_refused_even_when_the_proof_agrees_with_it(url):
    """The row's source_domain pins the host: a proof read from the lookalike cannot vouch for it."""
    host = url.split("/")[2]
    prod = with_(TARTE, canonical_url=url)
    assert verify(prod, TARTE_SKU, with_(TARTE_PROOF, shop_host=host)) == (False, None, "host_mismatch")


def test_the_canonical_host_must_be_the_rows_source_domain():
    for other in ("shop.tartecosmetics.com", "evil-tarte.com", "tartecosmetics.com.evil.io", None, ""):
        assert verify(with_(TARTE, source_domain=other), TARTE_SKU, TARTE_PROOF) == (
            False, None, "host_mismatch"), other
    assert verify(with_(TARTE, source_domain="www.tartecosmetics.com"), TARTE_SKU, TARTE_PROOF)[0] is True
    # Case is not identity for a DNS name: the stored domain is compared folded.
    assert verify(with_(TARTE, source_domain="TarteCosmetics.com"), TARTE_SKU, TARTE_PROOF)[0] is True


# ── refused: canonical_url shape ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://tartecosmetics.com/products/flat-blush-brush",
    "ftp://tartecosmetics.com/products/flat-blush-brush",
    "https:///products/flat-blush-brush",
    "https://evil@tartecosmetics.com/products/flat-blush-brush",
    "https://@tartecosmetics.com/products/flat-blush-brush",
    "https://:@tartecosmetics.com/products/flat-blush-brush",
    "https://tartecosmetics.com:443/products/flat-blush-brush",
    "https://tartecosmetics.com:8443/products/flat-blush-brush",
    "https://tartecosmetics.com:abc/products/flat-blush-brush",
    "https://tartecosmetics.com/products/flat-blush-brush?variant=52729903251478",
    "https://tartecosmetics.com/products/flat-blush-brush#reviews",
    "https://tartecosmetics.com/collections/face/products/flat-blush-brush",
    "https://tartecosmetics.com/products/flat-blush-brush/",
    "https://tartecosmetics.com/products/",
    "https://tartecosmetics.com/products/flat-blush-brush.js",
    "https://tartecosmetics.com/products/flat-blush-brush.json",
    "https://tartecosmetics.com/products/flat\\blush",
    "https://tartecosmetics.com/products/flat-blush-brush\t",
    "https://tartecosmetics.com/products/flat-blush\x00brush",
    " https://tartecosmetics.com/products/flat-blush-brush",
    "",
    None,
    42,
    b"https://tartecosmetics.com/products/flat-blush-brush",
])
def test_an_unusable_canonical_url_is_refused(url):
    assert storefront_page(url) is None
    assert verify(with_(TARTE, canonical_url=url), TARTE_SKU, TARTE_PROOF) == (False, None, "canonical_url_unusable")


def test_the_canonical_page_is_host_and_handle():
    assert storefront_page("https://tartecosmetics.com/products/flat-blush-brush") == (
        "tartecosmetics.com", "flat-blush-brush")
    assert storefront_page("HTTPS://TarteCosmetics.com/products/flat-blush-brush") == (
        "tartecosmetics.com", "flat-blush-brush")


# ── refused: handles ────────────────────────────────────────────────────────────────────────────


def test_a_proof_for_another_handle_is_refused():
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, handle="flat-blush-brush-2")) == (False, None, "handle_mismatch")
    assert verify(TARTE, TARTE_SKU, with_(TARTE_PROOF, handle=None)) == (False, None, "handle_mismatch")


def test_a_blank_source_handle_falls_back_to_the_canonical_handle():
    for blank in ("", "   ", None):
        sk = sku(TARTE_SKU["sku_key"], "52729903251478",
                 payload={"agent_version": SS, "source_handle": blank})
        assert verify(TARTE, sk, TARTE_PROOF)[0] is True


@pytest.mark.parametrize("source_handle", [42, ["nc10"], "a/b", "nc 10", "nc10?x=1", "nc10#top"])
def test_an_unusable_source_handle_is_refused(source_handle):
    sk = sku(MAC_NC10["sku_key"], "54057377923267", payload={"source_handle": source_handle})
    assert verify(MAC, sk, MAC_NC10_PROOF) == (False, None, "sku_payload_malformed")


@pytest.mark.parametrize("payload", ["{not json", "[1, 2]", "\"nc10\"", 7])
def test_an_unreadable_sku_payload_is_refused(payload):
    sk = dict(MAC_NC10, sku_payload=payload)
    assert verify(MAC, sk, MAC_NC10_PROOF) == (False, None, "sku_payload_malformed")


def test_a_sku_payload_may_be_a_dict_or_absent():
    assert verify(MAC, dict(MAC_NC10, sku_payload={"source_handle": MAC_NC10_HANDLE}), MAC_NC10_PROOF)[0] is True
    assert verify(TARTE, dict(TARTE_SKU, sku_payload=None), TARTE_PROOF)[0] is True


# ── refused: the placeholder ────────────────────────────────────────────────────────────────────


def test_a_canonical_placeholder_on_a_multi_variant_handle_is_refused():
    canon = placeholder(CHESTNUT_PK)
    pr = with_(CHESTNUT_PROOF, sku_key=canon["sku_key"])
    for lvc in (2, 3, 100):
        assert verify(CHESTNUT, canon, with_(pr, live_variant_count=lvc), count=0) == (
            False, None, "placeholder_multi_variant")


# ── refused: the sku's own identity ─────────────────────────────────────────────────────────────


def test_a_sku_key_that_contradicts_its_source_variant_id_is_refused():
    lying = sku(TARTE_PK + "::v:52729903251478", "52729903251479")
    pr = with_(TARTE_PROOF, variant_id="52729903251479")
    assert verify(TARTE, lying, pr) == (False, None, "sku_variant_contradiction")
    keyed_only = sku(TARTE_PK + "::v:52729903251478", "FG06034")
    assert verify(TARTE, keyed_only, TARTE_PROOF) == (False, None, "sku_variant_contradiction")


@pytest.mark.parametrize("svid", ["FG06034", "", None, "52729903251478x"])
def test_a_variant_sku_with_no_numeric_id_is_refused_even_on_a_sole_variant_handle(svid):
    """Sole mode would buy the one live variant; a sku that cannot say it IS that variant (an
    8 oz sku whose 16.9 oz sibling was delisted) must not."""
    sk = sku(BM_SKU["sku_key"], svid)
    assert verify(BM, sk, BM_PROOF) == (False, None, "sku_variant_unverified")


@pytest.mark.parametrize("svid", [
    " 32903948173387", "32903948173387 ", "x gid://shopify/ProductVariant/32903948173387",
    "gid://shopify/ProductVariant/32903948173387?x=1", "gid://shopify/ProductVariant/32903948173387 ",
    "gid://shopify/Product/32903948173387",
])
def test_a_source_variant_id_is_read_strictly(svid):
    """Only the two exact spellings; the shared reader alone would find an id in each of these."""
    assert verify(BM, sku(BM_SKU["sku_key"], svid), BM_PROOF) == (False, None, "sku_variant_unverified")


def test_a_gid_source_variant_id_is_read():
    sk = sku(BM_SKU["sku_key"], "gid://shopify/ProductVariant/32903948173387")
    assert verify(BM, sk, BM_PROOF) == (True, "32903948173387", "sole_variant")


@pytest.mark.parametrize("sku_key", [
    TARTE_PK + "::sku_FG06034", TARTE_PK + "::canonical2", TARTE_PK + "::", TARTE_PK + "::V:52729903251478",
])
def test_a_sku_key_of_no_enrichment_shape_is_refused(sku_key):
    sk = sku(sku_key, "52729903251478")
    assert verify(TARTE, sk, with_(TARTE_PROOF, sku_key=sku_key)) == (False, None, "sku_key_unrecognized")


def test_a_sku_of_another_product_is_refused():
    assert verify(TARTE, BRONZER_ECLIPSE, with_(BRONZER_PROOF, product_key=TARTE_PK)) == (
        False, None, "sku_not_of_product")
    # A product key that is a `::`-PREFIX of the sku's product key is still another product: the
    # rest of the sku key is then neither `canonical` nor `v:<token>`.
    shorter = with_(TARTE, product_key="ext:tarte-flat-blush-brush")
    assert verify(shorter, TARTE_SKU, with_(TARTE_PROOF, product_key="ext:tarte-flat-blush-brush")) == (
        False, None, "sku_key_unrecognized")
    # ... and so is the LONGER product's placeholder: it ends in `::canonical` but is not
    # `<this product_key>::canonical`.
    longer_canon = placeholder(TARTE_PK)
    assert verify(shorter, longer_canon, with_(TARTE_PROOF, product_key="ext:tarte-flat-blush-brush",
                                               sku_key=longer_canon["sku_key"])) == (
        False, None, "sku_key_unrecognized")


def test_an_empty_product_key_owns_no_sku():
    prod = with_(TARTE, product_key="")
    canon = {"sku_key": "::canonical", "source_variant_id": "", "sku_payload": None}
    pr = with_(TARTE_PROOF, product_key="", sku_key="::canonical")
    assert verify(prod, canon, pr) == (False, None, "sku_not_of_product")


@pytest.mark.parametrize("source_system", ["external_product_seeds_mirror_v1", None, "merchant_sync"])
def test_a_row_that_is_not_an_enrichment_row_is_refused(source_system):
    assert verify(with_(TARTE, source_system=source_system), TARTE_SKU, TARTE_PROOF) == (
        False, None, "row_not_enrichment")


@pytest.mark.parametrize("which", ["product", "sku", "proof"])
def test_a_row_that_is_not_a_mapping_is_refused(which):
    rows = {"product": TARTE, "sku": TARTE_SKU, "proof": TARTE_PROOF}
    rows[which] = [("sku_key", TARTE_SKU["sku_key"])]
    verdict = verify(rows["product"], rows["sku"], rows["proof"])
    assert verdict == (False, None, "proof_missing" if which == "proof" else "row_malformed")


def test_a_naive_now_is_a_caller_bug():
    with pytest.raises(ValueError):
        verify_enrichment_cart_proof(TARTE, TARTE_SKU, TARTE_PROOF, catalog_variant_sku_count=1,
                                     now=datetime(2026, 9, 29, 12, 0))


# ── the seller ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("row", [TARTE, BRONZER, MAC, CHESTNUT, BM, KIEHLS, JSM], ids=lambda r: r["product_key"][:40])
def test_the_seller_re_derives_to_the_live_merchant_id(row):
    assert derive_enrichment_seller(row) == row["merchant_id"]


def test_the_second_tarte_and_stila_identities_re_derive_from_their_brand():
    """Two live merchant ids per store (tarte, stila): the brand the row carries decides which."""
    assert derive_enrichment_seller(with_(TARTE, brand="Tarte Cosmetics")) == "merch_obs_4a586a53cd1571d6"
    stila = product("ext:stila-stay-all-day-matte-lip-liner-last-chance-shades::7b81d518",
                    "https://stilacosmetics.com/products/stay-all-day-matte-lip-liner", brand="Stila")
    assert derive_enrichment_seller(stila) == "merch_obs_7f59d9487c6e0762"
    assert derive_enrichment_seller(with_(stila, brand="Stila Cosmetics")) == "merch_obs_e65708e6dc609674"


def test_a_retailer_row_is_keyed_on_the_domain_alone_whatever_its_brand():
    for brand in ("NARS", "Kiehl's Since 1851", None, ""):
        assert derive_enrichment_seller(with_(BM, brand=brand)) == "merch_obs_a2e07b1e8a08148b"


def test_the_brand_direct_dispatch_would_have_minted_a_different_seller_for_bluemercury():
    """Why the key prefix, not resolve_seed_seller_identity, decides a retailer row: bluemercury.com
    is not on the known-retailer list (census q3: 250/250 mismatch through the brand-direct path)."""
    from services.seller_identity import resolve_seed_seller_identity

    assert resolve_seed_seller_identity(brand="NARS", domain="bluemercury.com")["merchant_id"] != BM["merchant_id"]
    not_a_retailer_key = with_(BM, product_key="ext:nars-16-blush-brush::0badc0de")
    assert derive_enrichment_seller(not_a_retailer_key) != BM["merchant_id"]


@pytest.mark.parametrize("key", ["ext:retailer-beauty-flat-brush::0badc0de", "ext:tarte-retailer:exclusive::0badc0de",
                                 "ext:Retailer:37065ae3db2eba22d6b959e19dbb1284", "retailer:37065ae3db2eba22d6b959e19dbb1284"])
def test_only_the_exact_retailer_prefix_keys_a_row_on_its_domain(key):
    """A brand slug that merely CONTAINS 'retailer' is a brand-direct row."""
    brand_direct = with_(BM, product_key=key, brand="NARS")
    assert derive_enrichment_seller(brand_direct) == "merch_obs_2b6ad333b9173fb7"


def test_the_seller_is_read_from_the_canonical_host_not_another_url():
    assert derive_enrichment_seller(with_(TARTE, canonical_url="https://evil-tarte.com/products/flat-blush-brush",
                                          destination_url="https://tartecosmetics.com/")) != TARTE["merchant_id"]
    moved = with_(BM, canonical_url="https://evil.example/products/nars-16-blush-brush")
    assert derive_enrichment_seller(moved) not in (None, BM["merchant_id"])


@pytest.mark.parametrize("over", [
    {"source_system": "external_product_seeds_mirror_v1"},
    {"canonical_url": "http://tartecosmetics.com/products/flat-blush-brush"},
    {"canonical_url": None},
    {"brand": None},
    {"brand": "   "},
    {"brand": 7},
    {"canonical_url": "https://localhost/products/flat-blush-brush"},
])
def test_an_underivable_seller_is_none(over):
    assert derive_enrichment_seller(with_(TARTE, **over)) is None


def test_a_retailer_row_on_an_unregistrable_host_is_none():
    assert derive_enrichment_seller(with_(BM, canonical_url="https://com.vn/products/x")) is None


def test_a_non_mapping_product_has_no_seller():
    assert derive_enrichment_seller(None) is None
    assert derive_enrichment_seller([("product_key", BM_PK)]) is None


# ── the price ───────────────────────────────────────────────────────────────────────────────────


def offer(price, currency="USD"):
    return {"price": price, "currency": currency}


def test_an_offer_equal_to_the_live_price_is_accepted():
    assert enrichment_offer_price_ok([offer("34.00")], TARTE_PROOF, "USD") == (True, 3400, "ok")
    assert enrichment_offer_price_ok([offer(Decimal("34.00")), offer("34")], TARTE_PROOF, "USD") == (True, 3400, "ok")
    assert enrichment_offer_price_ok([offer("38.54", "SGD")], JSM_PROOF, "SGD") == (True, 3854, "ok")
    assert enrichment_offer_price_ok([offer("39.00", "usd")], MAC_NC10_PROOF, "USD") == (True, 3900, "ok")


def test_jsm_drift_is_stale():
    """jsmbeauty.sg offers were all written 09-08; the storefront has moved since."""
    drifted = with_(JSM_PROOF, live_price_minor=3600)
    assert enrichment_offer_price_ok([offer("38.54", "SGD")], drifted, "SGD") == (False, None, "row_price_stale")


def test_jsm_read_in_the_wrong_presentment_currency_is_a_currency_mismatch():
    """A proof read from our US egress can come back in USD for an SGD store."""
    usd = with_(JSM_PROOF, currency="USD", live_price_minor=3854)
    assert enrichment_offer_price_ok([offer("38.54", "SGD")], usd, "SGD") == (False, None, "row_currency_mismatch")
    assert enrichment_offer_price_ok([offer("38.54", "SGD")], with_(JSM_PROOF, currency=None), "SGD") == (
        False, None, "row_currency_mismatch")


def test_spellings_that_disagree_are_ambiguous():
    assert enrichment_offer_price_ok([offer("34.00"), offer("30.00")], TARTE_PROOF, "USD") == (
        False, None, "row_price_ambiguous")
    assert enrichment_offer_price_ok([offer("34.00"), offer("34.00", "CAD")], TARTE_PROOF, "USD") == (
        False, None, "row_price_ambiguous")


def test_no_usable_offer_is_unpriced():
    for offers in ([], None, [offer(None)], [offer("abc")], [offer(34.0)], [offer("0")], [offer("-1")],
                   [offer("34.001")], [offer("34.00", None)], [offer("34.00", "")], [offer("34.00", "US")],
                   ["34.00"], [None]):
        assert enrichment_offer_price_ok(offers, TARTE_PROOF, "USD") == (False, None, "row_unpriced"), offers


@pytest.mark.parametrize("bad", [offer("abc"), offer("34.00", None), offer(None), offer(34.0), "34.00", None])
def test_an_unusable_offer_beside_a_usable_one_is_ambiguous(bad):
    """Fail closed: a spelling whose price nobody can read is not a spelling that agrees."""
    assert enrichment_offer_price_ok([bad, offer("34.00")], TARTE_PROOF, "USD") == (False, None, "row_price_ambiguous")
    assert enrichment_offer_price_ok([offer("34.00"), bad], TARTE_PROOF, "USD") == (False, None, "row_price_ambiguous")


def test_the_market_currency_is_compared_upper_cased():
    assert enrichment_offer_price_ok([offer("34.00")], TARTE_PROOF, "usd") == (True, 3400, "ok")
    assert enrichment_offer_price_ok([offer("34.00")], TARTE_PROOF, " USD ") == (True, 3400, "ok")


def test_an_offer_in_another_currency_than_the_market_is_refused():
    assert enrichment_offer_price_ok([offer("34.00")], TARTE_PROOF, "SGD") == (False, None, "row_currency_mismatch")
    # Even when the proof itself was read in the market's currency at the same number.
    sgd = with_(TARTE_PROOF, currency="SGD")
    assert enrichment_offer_price_ok([offer("34.00", "USD")], sgd, "SGD") == (False, None, "row_currency_mismatch")
    assert enrichment_offer_price_ok([offer("34.00")], TARTE_PROOF, None) == (False, None, "row_currency_mismatch")


def test_a_proof_without_a_usable_live_price_is_stale():
    for live in (None, 0, 3399, 3401, "3400", 3400.0, True):
        assert enrichment_offer_price_ok([offer("34.00")], with_(TARTE_PROOF, live_price_minor=live), "USD") == (
            False, None, "row_price_stale"), live
    assert enrichment_offer_price_ok([offer("34.00")], None, "USD") == (False, None, "row_price_stale")


def test_a_zero_decimal_currency_is_priced_in_whole_units():
    jp = with_(TARTE_PROOF, currency="JPY", live_price_minor=4400)
    assert enrichment_offer_price_ok([offer("4400", "JPY")], jp, "JPY") == (True, 4400, "ok")
    assert enrichment_offer_price_ok([offer("4400.50", "JPY")], jp, "JPY") == (False, None, "row_unpriced")


# ── the constants are the ingestion lane's own spellings ────────────────────────────────────────


def test_the_constants_match_the_enrichment_lane():
    from services.catalog_enrichment_agent import ingestion
    from services.linked_listing_inci_fill import RETAILER_LISTING_PREFIX

    assert m.ENRICHMENT_SOURCE_SYSTEM == ingestion.AGENT_VERSION
    assert m.PLACEHOLDER_SUFFIX == ingestion.SKU_SUFFIX
    assert m.VARIANT_INFIX == ingestion.VARIANT_SKU_INFIX
    assert m.RETAILER_KEY_PREFIX == RETAILER_LISTING_PREFIX


def test_the_retailer_lane_and_long_keys_really_do_hide_the_id_from_the_key():
    """The module docstring's reason for reading source_variant_id: pinned against the minters."""
    from services.catalog_enrichment_agent import ingestion

    assert ingestion.derive_variant_sku_key(BM_PK, "32903948173387", seller_scope="bluemercury.com") == BM_SKU["sku_key"]
    assert ingestion.canonical_sku_variant_id(LONG_PK, LONG_SPID) == LONG_SPID != LONG_PK
