"""jobs/enrichment_cart_variant_proof.py -- the enrichment storefront cart-proof writer (option 2, PR B).

Every storefront body here is a REAL response read 2026-09-29, trimmed
(tests/fixtures/enrichment_cart_proof_storefronts_2026_09_29.json):
  * tarte    `.js` of a 3-shade blush (two shades sold out) and a /products.json page, read plain
             (USD) and with `?country=GB` (GBP, SAME product ids, other prices);
  * jsm      an SGD `.js` with 10 variants;
  * MAC      the Studio Fix Fluid family PARENT (one variant restating the product title), its NC50
             shade (its own product, real options), and Fix+, a canonical-only catalog row with the
             parent's signature; all three reached through the 301 to www.maccosmetics.com;
  * luafee   a JPY `.js` (x100: 220000 = JPY 2,200) whose store sends NO cart_currency cookie.
Every catalog row is built by the enrichment lane's own producers
(services/catalog_enrichment_agent/ingestion.py), and every 'ok' row the job builds is replayed
through the READER, services.reap_enrichment_cart_proof.verify_enrichment_cart_proof.

The database cases (`job_db`) run here on SQLite and, star-imported, on Postgres in
tests/test_enrichment_cart_variant_proof_job_postgres.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

import jobs.enrichment_cart_variant_proof as job
from jobs.enrichment_cart_variant_proof import (
    CURRENCY_NOT_MARKET,
    CURRENCY_UNVERIFIED,
    HANDLE_MISMATCH,
    HOST_REDIRECTED,
    PARENT_STUB,
    PAYLOAD_MALFORMED,
    PLACEHOLDER_HAS_VARIANT_SKUS,
    PLACEHOLDER_MULTI_VARIANT,
    PRICE_UNREADABLE,
    REVOKED_404,
    SKU_VARIANT_UNVERIFIED,
    SOURCE_PRODUCTS_JS,
    SOURCE_PRODUCTS_JSON,
    VARIANT_COUNT_UNVERIFIABLE,
    VARIANT_GONE,
    DomainPlan,
    HandleEvidence,
    Pacer,
    ProofRow,
    decide_proof,
    evidence_from_product,
    is_parent_stub,
    live_price_minor,
    parse_live_product,
    presentment_currency,
    target_for_row,
)
from services.catalog_enrichment_agent import ingestion
from services.reap_enrichment_cart_proof import (
    enrichment_offer_price_ok,
    verify_enrichment_cart_proof,
)

FIXTURES = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "enrichment_cart_proof_storefronts_2026_09_29.json")
    .read_text(encoding="utf-8"))
CHECKED = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc)
SS = "catalog_enrichment_agent_v1"


def body(name: str) -> Dict[str, Any]:
    return json.loads(json.dumps(FIXTURES[name]["body"]), parse_float=Decimal)


def cookies(name: str) -> List[str]:
    return [f"cart_currency={c}; path=/; expires=Wed, 29 Oct 2026 05:00:00 GMT" for c in FIXTURES[name]["set_cookie_cart_currency"]]


# ── catalog rows, from the enrichment lane's own producers ──────────────────────────────────────


def build_rows(brand: str, name: str, host: str, handle: str, variants: List[Dict[str, Any]], *,
               retailer: bool = False) -> Dict[str, Any]:
    """(product row, [sku rows]) exactly as ingestion writes them: the placeholder via
    `_build_sku_insert`, the variants via `_build_variant_sku_inserts`."""
    pdp = {"brand": brand, "product_name": name, "source_domain": host, "currency": "USD", "variants": variants}
    if retailer:
        pdp["source_role"] = "retailer"
    product_key = ingestion.derive_product_key(brand, name)
    if retailer:
        product_key = "ext:retailer:ecvpjob" + "0" * 25  # the lane's `ext:retailer:<32 hex>` shape
    url = f"https://{host}/products/{handle}"
    seller = {"merchant_id": "merch_obs_ecvpjob"}
    placeholder = ingestion._build_sku_insert(product_key=product_key, pdp_payload=pdp, canonical_url=url,
                                              image_url=None, seller=seller)
    skus = [placeholder] + ingestion._build_variant_sku_inserts(product_key=product_key, pdp_payload=pdp,
                                                                seller=seller, canonical_url=url)
    product = {"product_key": product_key, "source_system": SS, "source_domain": host, "canonical_url": url,
               "brand": brand, "title": name}
    return {"product": product, "skus": skus}


def selected(rows: Dict[str, Any], sku: Dict[str, Any]) -> Dict[str, Any]:
    """One SELECT_TARGETS_SQL row: the product joined to one sku, with the ::v: count."""
    count = sum(1 for s in rows["skus"] if s["sku_key"].startswith(rows["product"]["product_key"] + "::v:"))
    return {**rows["product"], "catalog_title": rows["product"]["title"],
            "sku_key": sku["sku_key"], "source_variant_id": sku["source_variant_id"],
            "sku_payload": sku["sku_payload"], "catalog_variant_sku_count": count}


def sku_of(rows, suffix):
    return next(s for s in rows["skus"] if s["sku_key"].endswith(suffix))


TARTE = build_rows("ecvpjob tarte", "Amazonian clay baked blush", "tartecosmetics.com", "amazonian-clay-baked-blush",
                   [{"variant_id": "63530896753009", "title": "pink"}, {"variant_id": "63530896785777", "title": "peach"},
                    {"variant_id": "63530896818545", "title": "berry"}])
MAC_FAMILY = build_rows(
    "ecvpjob mac", "Studio Fix Fluid SPF 15 24HR Matte Foundation + Oil Control", "maccosmetics.com",
    "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control",
    [{"variant_id": "54057385787587", "title": "NC50",
      "source_handle": "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control-nc50"},
     {"variant_id": "54057377923267", "title": "NC10",
      "source_handle": "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control-nc10"}])
MAC_FIX = build_rows("ecvpjob mac", "Fix+", "maccosmetics.com", "fix", [])
TARTE_SINGLE = build_rows("ecvpjob tarte", "front row energy travel essentials", "tartecosmetics.com",
                          "front-row-energy-travel-essentials", [])
JSM = build_rows("ecvpjob jsm", "LIP-PRESSION Glowy Tint", "jsmbeauty.sg", "lip-pression-glowy-tint",
                 [{"variant_id": "51905273004353", "title": "Early Peach"},
                  {"variant_id": "51905273069889", "title": "Peach Bare"}])


def target(rows, suffix, domain=None):
    row = selected(rows, sku_of(rows, suffix))
    t, skip = target_for_row(row, domain or rows["product"]["source_domain"])
    assert skip is None, skip
    return t


def js_evidence(name, handle=None, *, cookie_names=None):
    b = body(name)
    currency, problem = presentment_currency(cookies(cookie_names or name))
    return evidence_from_product(b, requested_handle=handle or b["handle"], source=SOURCE_PRODUCTS_JS,
                                 checked_at=CHECKED, currency=currency, currency_problem=problem)


def listing_evidence(name, handle):
    product = next(p for p in body(name)["products"] if p["handle"] == handle)
    currency, problem = presentment_currency(cookies(name))
    return evidence_from_product(product, requested_handle=handle, source=SOURCE_PRODUCTS_JSON,
                                 checked_at=CHECKED, currency=currency, currency_problem=problem)


def replay(rows, suffix, proof: ProofRow, *, now=NOW):
    """The READER's verdict on what the writer built."""
    sku = sku_of(rows, suffix)
    count = sum(1 for s in rows["skus"] if s["sku_key"].startswith(rows["product"]["product_key"] + "::v:"))
    return verify_enrichment_cart_proof(rows["product"], sku, proof.as_params(now),
                                        catalog_variant_sku_count=count, now=now)


# ── the currency a response was read in ─────────────────────────────────────────────────────────


def test_the_read_currency_is_the_cart_currency_cookie_of_that_response():
    assert presentment_currency(["cart_currency=USD; path=/"]) == ("USD", None)
    assert presentment_currency(["_shopify_essential=abc; path=/", "cart_currency=SGD"]) == ("SGD", None)
    # The same value twice is one value.
    assert presentment_currency(["cart_currency=GBP", "cart_currency=GBP; path=/"]) == ("GBP", None)


@pytest.mark.parametrize("headers,problem", [
    ([], "cookie_absent"),
    (["_shopify_essential=abc; path=/"], "cookie_absent"),
    (["xcart_currency=USD"], "cookie_absent"),
    (["cart_currency_x=USD"], "cookie_absent"),
    (["cart_currency=USD", "cart_currency=GBP"], "cookie_conflict"),
    (["cart_currency=usd"], "cookie_malformed"),
    (["cart_currency=US"], "cookie_malformed"),
    (["cart_currency=USDX"], "cookie_malformed"),
    (["cart_currency="], "cookie_malformed"),
])
def test_anything_but_one_well_formed_cookie_value_is_not_a_currency(headers, problem):
    assert presentment_currency(headers) == (None, problem)


def test_luafee_sends_no_cookie_so_its_yen_is_unverified():
    assert FIXTURES["luafee_js_no_currency_cookie"]["set_cookie_cart_currency"] == []
    live = js_evidence("luafee_js_no_currency_cookie")
    assert (live.currency, live.currency_problem) == (None, "cookie_absent")


# ── prices in ISO minor units ───────────────────────────────────────────────────────────────────


def test_js_prices_are_x100_for_every_currency_including_yen():
    assert live_price_minor(3000, "USD", SOURCE_PRODUCTS_JS) == 3000
    assert live_price_minor(3000, "SGD", SOURCE_PRODUCTS_JS) == 3000
    # luafee.jp, live: 220000 in `.js` is JPY 2,200.
    assert live_price_minor(body("luafee_js_no_currency_cookie")["variants"][0]["price"], "JPY", SOURCE_PRODUCTS_JS) == 2200
    # Sub-yen is not a yen amount: refused, never rounded.
    assert live_price_minor(220050, "JPY", SOURCE_PRODUCTS_JS) is None


@pytest.mark.parametrize("price", ["3000", 3000.0, Decimal("3000"), True, None, 0, -100])
def test_a_js_price_is_a_positive_json_integer_or_nothing(price):
    assert live_price_minor(price, "USD", SOURCE_PRODUCTS_JS) is None


def test_products_json_prices_are_major_unit_strings():
    assert live_price_minor("35.00", "USD", SOURCE_PRODUCTS_JSON) == 3500
    assert live_price_minor("33.00", "GBP", SOURCE_PRODUCTS_JSON) == 3300
    assert live_price_minor("2200", "JPY", SOURCE_PRODUCTS_JSON) == 2200
    assert live_price_minor(Decimal("35.5"), "USD", SOURCE_PRODUCTS_JSON) == 3550
    for bad in ("35.001", "2200.5", "abc", "", 35.0, True, None, "0.00"):
        currency = "JPY" if bad == "2200.5" else "USD"
        assert live_price_minor(bad, currency, SOURCE_PRODUCTS_JSON) is None, bad
    assert live_price_minor(3000, "USD", "some_other_source") is None


# ── one storefront product, parsed strictly ─────────────────────────────────────────────────────


def test_the_live_count_is_every_variant_sold_out_included():
    live, failure = parse_live_product(body("tarte_js_amazonian_clay_baked_blush"))
    assert failure is None
    assert live.live_variant_count == 3
    assert [(v.variant_id, v.available) for v in live.variants] == [
        ("63530896753009", False), ("63530896785777", False), ("63530896818545", True)]
    assert live.product_id == "15531947491697" and live.handle == "amazonian-clay-baked-blush"


def _mutated(**over):
    b = body("tarte_js_amazonian_clay_baked_blush")
    for key, value in over.items():
        if key.startswith("v1_"):
            b["variants"][1][key[3:]] = value
        else:
            b[key] = value
    return b


@pytest.mark.parametrize("product,outcome", [
    ("not a product", PAYLOAD_MALFORMED),
    (_mutated(handle=""), PAYLOAD_MALFORMED),
    (_mutated(handle=None), PAYLOAD_MALFORMED),
    (_mutated(id="15531947491697"), PAYLOAD_MALFORMED),
    (_mutated(id=True), PAYLOAD_MALFORMED),
    (_mutated(variants=[]), PAYLOAD_MALFORMED),
    (_mutated(variants="x"), PAYLOAD_MALFORMED),
    (_mutated(v1_id="63530896785777"), PAYLOAD_MALFORMED),
    (_mutated(v1_id=63530896753009), PAYLOAD_MALFORMED),  # a duplicate id
    (_mutated(v1_id=0), PAYLOAD_MALFORMED),
    (_mutated(v1_available="false"), PAYLOAD_MALFORMED),
    (_mutated(v1_available=None), PAYLOAD_MALFORMED),
])
def test_one_unreadable_variant_refuses_the_whole_handle(product, outcome):
    assert parse_live_product(product) == (None, outcome)


def test_a_variant_list_that_may_be_truncated_is_not_counted():
    b = body("tarte_js_amazonian_clay_baked_blush")
    template = b["variants"][0]
    b["variants"] = [{**template, "id": 1000 + i} for i in range(100)]
    assert parse_live_product(b) == (None, VARIANT_COUNT_UNVERIFIABLE)
    b["variants"] = b["variants"][:99]
    live, failure = parse_live_product(b)
    assert failure is None and live.live_variant_count == 99


def test_the_parent_stub_signature_is_the_mac_parent_not_default_title():
    def stub(name):
        return is_parent_stub(parse_live_product(body(name))[0])

    assert stub("mac_js_family_parent_stub")
    assert stub("mac_js_fix_plus_canonical_only")
    assert not stub("mac_js_shade_nc50")
    assert not stub("tarte_js_amazonian_clay_baked_blush")
    # Shopify's own single variant ("Default Title") is every ordinary one-variant product.
    assert not stub("luafee_js_no_currency_cookie")
    tarte_page = body("tarte_products_json_page1_plain")["products"]
    assert not any(is_parent_stub(parse_live_product(p)[0]) for p in tarte_page)
    # Only a SOLE variant: jsm's first shade renamed after the product is still one of ten shades.
    jsm = body("jsm_js_lip_pression_glowy_tint")
    jsm["variants"][0]["title"] = jsm["title"]
    assert not is_parent_stub(parse_live_product(jsm)[0])
    # Case and surrounding space do not hide it; an empty title never matches.
    # (A real sku, so only the title signal can be what catches it.)
    b = body("mac_js_family_parent_stub")
    b["variants"][0]["sku"] = "SRMX11"
    b["variants"][0]["title"] = "  " + b["title"].upper() + " "
    assert is_parent_stub(parse_live_product(b)[0])
    b["title"] = b["variants"][0]["title"] = ""
    b["variants"][0]["sku"] = "SRMX11"
    assert not is_parent_stub(parse_live_product(b)[0])


def test_the_second_stub_signal_is_macs_product_code_on_an_imageless_product():
    """A parent whose variant title were renamed is still caught: sole variant, `P2000_` sku, no
    image. Each of the three conditions alone is not enough."""
    def parent(**over):
        b = body("mac_js_family_parent_stub")
        b["variants"][0]["title"] = "Renamed"
        b["variants"][0].update({k: v for k, v in over.items() if k == "sku"})
        if "images" in over:
            b["images"] = over["images"]
        return parse_live_product(b)[0]

    assert parent().image_count == 0 and parent().variants[0].sku == "P2000_120613"
    assert is_parent_stub(parent())
    assert not is_parent_stub(parent(images=["https://cdn/x.jpg"]))
    assert not is_parent_stub(parent(sku="SRMX11"))
    b = body("mac_js_family_parent_stub")
    del b["images"]  # a response without an image list says nothing about images
    b["variants"][0]["title"] = "Renamed"
    assert not is_parent_stub(parse_live_product(b)[0])
    two = body("mac_js_family_parent_stub")
    two["variants"][0]["title"] = "Renamed"
    two["variants"].append(dict(two["variants"][0], id=54057377759428))
    assert not is_parent_stub(parse_live_product(two)[0])


# ── the proof row each sku gets, and what the reader says of it ─────────────────────────────────


def test_a_named_shade_on_a_multi_variant_handle_is_ok_and_the_reader_buys_it():
    t = target(TARTE, "::v:63530896818545")
    proof = decide_proof(t, js_evidence("tarte_js_amazonian_clay_baked_blush"), market_currency="USD")
    assert proof.outcome == "ok"
    assert (proof.shop_host, proof.handle, proof.variant_id, proof.live_variant_count, proof.available,
            proof.live_price_minor, proof.currency, proof.source, proof.checked_at, proof.shopify_product_id) == (
        "tartecosmetics.com", "amazonian-clay-baked-blush", "63530896818545", 3, True, 3000, "USD",
        "products_js_v1", CHECKED, "15531947491697")
    assert replay(TARTE, "::v:63530896818545", proof) == (True, "63530896818545", "named_variant")
    assert enrichment_offer_price_ok([{"price": "30.00", "currency": "USD"}], proof.as_params(NOW), "USD") == (
        True, 3000, "ok")


def test_a_sold_out_shade_is_recorded_as_it_is_and_the_reader_refuses_it():
    proof = decide_proof(target(TARTE, "::v:63530896753009"), js_evidence("tarte_js_amazonian_clay_baked_blush"),
                         market_currency="USD")
    assert (proof.outcome, proof.available, proof.live_variant_count) == ("ok", False, 3)
    assert replay(TARTE, "::v:63530896753009", proof) == (False, None, "proof_unavailable")


def test_a_shade_the_handle_no_longer_lists_is_variant_gone():
    rows = build_rows("ecvpjob tarte", "Amazonian clay baked blush", "tartecosmetics.com", "amazonian-clay-baked-blush",
                      [{"variant_id": "63530896700000", "title": "plum"}, {"variant_id": "63530896818545", "title": "berry"}])
    proof = decide_proof(target(rows, "::v:63530896700000"), js_evidence("tarte_js_amazonian_clay_baked_blush"),
                         market_currency="USD")
    assert (proof.outcome, proof.variant_id, proof.live_price_minor, proof.live_variant_count) == (
        VARIANT_GONE, None, None, 3)
    assert replay(rows, "::v:63530896700000", proof)[2] == "proof_outcome_not_ok"


def test_the_placeholder_never_stands_for_one_of_several_variants():
    proof = decide_proof(target(TARTE, "::canonical"), js_evidence("tarte_js_amazonian_clay_baked_blush"),
                         market_currency="USD")
    # The catalog knows this product's shades: that refusal comes first.
    assert proof.outcome == PLACEHOLDER_HAS_VARIANT_SKUS
    bare = build_rows("ecvpjob tarte", "Amazonian clay baked blush", "tartecosmetics.com",
                      "amazonian-clay-baked-blush", [])
    proof = decide_proof(target(bare, "::canonical"), js_evidence("tarte_js_amazonian_clay_baked_blush"),
                         market_currency="USD")
    assert (proof.outcome, proof.variant_id) == (PLACEHOLDER_MULTI_VARIANT, None)


def test_the_placeholder_of_a_one_variant_product_is_ok_off_a_listing_page():
    t = target(TARTE_SINGLE, "::canonical")
    proof = decide_proof(t, listing_evidence("tarte_products_json_page1_plain", "front-row-energy-travel-essentials"),
                         market_currency="USD")
    assert (proof.outcome, proof.variant_id, proof.live_variant_count, proof.live_price_minor, proof.source) == (
        "ok", "64341820473713", 1, 3500, "products_json_v1")
    assert replay(TARTE_SINGLE, "::canonical", proof) == (True, "64341820473713", "sole_variant")


def test_a_placeholder_counts_suppressed_variant_skus_too():
    t = target(TARTE_SINGLE, "::canonical")
    shaded = job.SkuTarget(**{**t.__dict__, "catalog_variant_sku_count": 1})
    proof = decide_proof(shaded, listing_evidence("tarte_products_json_page1_plain", "front-row-energy-travel-essentials"),
                         market_currency="USD")
    assert proof.outcome == PLACEHOLDER_HAS_VARIANT_SKUS


def test_the_same_products_read_in_gbp_are_not_a_us_proof():
    proof = decide_proof(target(TARTE_SINGLE, "::canonical"),
                         listing_evidence("tarte_products_json_page1_country_GB", "front-row-energy-travel-essentials"),
                         market_currency="USD")
    assert (proof.outcome, proof.currency, proof.live_price_minor, proof.variant_id) == (
        CURRENCY_NOT_MARKET, "GBP", None, "64341820473713")
    assert replay(TARTE_SINGLE, "::canonical", proof)[2] == "proof_outcome_not_ok"


def test_the_mac_parent_stub_is_never_ok_and_neither_is_fix_plus():
    for rows, suffix, name in ((MAC_FAMILY, "::canonical", "mac_js_family_parent_stub"),
                               (MAC_FIX, "::canonical", "mac_js_fix_plus_canonical_only")):
        proof = decide_proof(target(rows, suffix), js_evidence(name), market_currency="USD")
        assert (proof.outcome, proof.variant_id, proof.live_price_minor) == (PARENT_STUB, None, None), name


def test_a_folded_mac_shade_is_proven_from_its_own_handle():
    t = target(MAC_FAMILY, "::v:54057385787587")
    assert t.handle == "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control-nc50"
    assert t.shop_host == "maccosmetics.com"
    proof = decide_proof(t, js_evidence("mac_js_shade_nc50"), market_currency="USD")
    assert (proof.outcome, proof.handle, proof.variant_id, proof.live_price_minor) == (
        "ok", "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control-nc50", "54057385787587", 3900)
    assert replay(MAC_FAMILY, "::v:54057385787587", proof) == (True, "54057385787587", "sole_variant")
    # The parent's evidence cannot prove the shade, whatever else it says.
    wrong = js_evidence("mac_js_family_parent_stub",
                        handle="studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control-nc50")
    assert decide_proof(t, wrong, market_currency="USD").outcome == HANDLE_MISMATCH


def test_an_sgd_store_is_an_sg_proof_and_not_a_us_one():
    t = target(JSM, "::v:51905273069889")
    ev = js_evidence("jsm_js_lip_pression_glowy_tint")
    sg = decide_proof(t, ev, market_currency="SGD")
    assert (sg.outcome, sg.currency, sg.live_price_minor, sg.live_variant_count) == ("ok", "SGD", 3000, 10)
    assert replay(JSM, "::v:51905273069889", sg) == (True, "51905273069889", "named_variant")
    assert decide_proof(t, ev, market_currency="USD").outcome == CURRENCY_NOT_MARKET


def test_no_cookie_means_currency_unverified_and_a_cookie_means_yen():
    rows = build_rows("ecvpjob luafee", "perfume mist", "luafee.jp", body("luafee_js_no_currency_cookie")["handle"],
                      [{"variant_id": "52912527868213", "title": "Default Title"}])
    t = target(rows, "::v:52912527868213")
    proof = decide_proof(t, js_evidence("luafee_js_no_currency_cookie"), market_currency="JPY")
    assert (proof.outcome, proof.currency, proof.variant_id) == (CURRENCY_UNVERIFIED, None, "52912527868213")
    with_cookie = js_evidence("luafee_js_no_currency_cookie", cookie_names="tarte_js_amazonian_clay_baked_blush")
    with_cookie.currency = "JPY"
    ok = decide_proof(t, with_cookie, market_currency="JPY")
    assert (ok.outcome, ok.live_price_minor, ok.currency) == ("ok", 2200, "JPY")


def test_an_unreadable_price_is_not_ok():
    ev = js_evidence("jsm_js_lip_pression_glowy_tint")
    b = body("jsm_js_lip_pression_glowy_tint")
    b["variants"][1]["price"] = "30.00"  # a string in `.js` is not the x100 integer shape
    ev = evidence_from_product(b, requested_handle=b["handle"], source=SOURCE_PRODUCTS_JS, checked_at=CHECKED,
                               currency="SGD", currency_problem=None)
    assert decide_proof(target(JSM, "::v:51905273069889"), ev, market_currency="SGD").outcome == PRICE_UNREADABLE


def test_a_sku_whose_key_and_id_disagree_is_never_matched():
    rows = json.loads(json.dumps(JSM))
    sku = sku_of(rows, "::v:51905273069889")
    sku["source_variant_id"] = "51905273004353"  # the key says ...069889, the id another shade
    assert decide_proof(target(rows, "::v:51905273069889"), js_evidence("jsm_js_lip_pression_glowy_tint"),
                        market_currency="SGD").outcome == SKU_VARIANT_UNVERIFIED
    sku["source_variant_id"] = "not-a-number"
    assert decide_proof(target(rows, "::v:51905273069889"), js_evidence("jsm_js_lip_pression_glowy_tint"),
                        market_currency="SGD").outcome == SKU_VARIANT_UNVERIFIED


def test_the_retailer_lane_id_lives_in_source_variant_id():
    rows = build_rows("ecvpjob bm", "Amazonian clay baked blush", "bluemercury.com", "amazonian-clay-baked-blush",
                      [{"variant_id": "63530896818545", "title": "berry"}, {"variant_id": "63530896753009", "title": "pink"}],
                      retailer=True)
    sku = next(s for s in rows["skus"] if s["source_variant_id"] == "63530896818545")
    assert "::v:retailer-" in sku["sku_key"]
    row = selected(rows, sku)
    t, skip = target_for_row(row, "bluemercury.com")
    assert skip is None
    proof = decide_proof(t, js_evidence("tarte_js_amazonian_clay_baked_blush"), market_currency="USD")
    assert (proof.outcome, proof.variant_id) == ("ok", "63530896818545")
    count = sum(1 for s in rows["skus"] if "::v:" in s["sku_key"])
    assert verify_enrichment_cart_proof(rows["product"], sku, proof.as_params(NOW),
                                        catalog_variant_sku_count=count, now=NOW) == (
        True, "63530896818545", "named_variant")


def test_handle_level_refusals_are_written_for_every_sku_on_the_handle():
    for outcome in (REVOKED_404, HOST_REDIRECTED, HANDLE_MISMATCH, PAYLOAD_MALFORMED):
        ev = HandleEvidence(requested_handle="amazonian-clay-baked-blush", source=SOURCE_PRODUCTS_JS,
                            checked_at=CHECKED, outcome=outcome)
        proof = decide_proof(target(TARTE, "::v:63530896818545"), ev, market_currency="USD")
        assert (proof.outcome, proof.handle, proof.variant_id, proof.shop_host) == (
            outcome, "amazonian-clay-baked-blush", None, "tartecosmetics.com")
    with pytest.raises(ValueError):
        # Transient is never evidence, whatever else the object happens to carry.
        stale = js_evidence("tarte_js_amazonian_clay_baked_blush")
        stale.transient = "rate_limited"
        decide_proof(target(TARTE, "::v:63530896818545"), stale, market_currency="USD")
    with pytest.raises(ValueError):
        decide_proof(target(TARTE, "::canonical"), HandleEvidence(
            requested_handle="x", source=SOURCE_PRODUCTS_JS, outcome=REVOKED_404), market_currency="USD")


def test_every_non_ok_row_is_refused_by_the_reader():
    evidence = js_evidence("tarte_js_amazonian_clay_baked_blush")
    base = decide_proof(target(TARTE, "::v:63530896818545"), evidence, market_currency="USD")
    for outcome in job.WRITTEN_OUTCOMES - {"ok"}:
        refused = ProofRow(**{**base.__dict__, "outcome": outcome})
        assert replay(TARTE, "::v:63530896818545", refused)[2] == "proof_outcome_not_ok", outcome


@pytest.mark.parametrize("over,skip", [
    ({"canonical_url": "http://tartecosmetics.com/products/amazonian-clay-baked-blush"}, "skip_canonical_url_unusable"),
    ({"canonical_url": "https://tartecosmetics.com/products/a?x=1"}, "skip_canonical_url_unusable"),
    ({"canonical_url": "https://uk.tartecosmetics.com/products/amazonian-clay-baked-blush"}, "skip_canonical_host_not_domain"),
    ({"source_domain": "tartecosmetics.co.uk"}, "skip_source_domain_mismatch"),
    ({"sku_payload": "{not json"}, "skip_sku_payload_malformed"),
    ({"sku_payload": json.dumps({"source_handle": "a/b"})}, "skip_sku_payload_malformed"),
])
def test_a_row_the_job_cannot_name_a_proof_for_is_skipped(over, skip):
    row = {**selected(TARTE, sku_of(TARTE, "::v:63530896818545")), **over}
    assert target_for_row(row, "tartecosmetics.com") == (None, skip)


def test_www_is_the_same_storefront_for_the_domain_and_the_row():
    row = {**selected(TARTE, sku_of(TARTE, "::v:63530896818545")),
           "canonical_url": "https://www.tartecosmetics.com/products/amazonian-clay-baked-blush"}
    t, skip = target_for_row(row, "tartecosmetics.com")
    assert skip is None and t.shop_host == "www.tartecosmetics.com"


# ── plan, pacing, and the not-scheduled guarantee ───────────────────────────────────────────────


def test_domains_come_from_the_tier_b_list_with_their_market_currency():
    plans = job.plan_domains(["tartecosmetics.com", "www.jsmbeauty.sg", "maccosmetics.com"])
    assert [(p.domain, p.market, p.market_currency) for p in plans] == [
        ("tartecosmetics.com", "US", "USD"), ("jsmbeauty.sg", "SG", "SGD"), ("maccosmetics.com", "US", "USD")]
    with pytest.raises(job.MerchantListError):
        job.plan_domains(["notonthelist.com"])
    with pytest.raises(job.MerchantListError):
        job.plan_domains([])


def test_main_refuses_without_a_domain_and_off_the_list(capsys):
    flag = "--on-crawl-egress"
    assert job.main([flag]) == 2
    assert job.main([flag, "--domain", "notonthelist.com"]) == 2
    assert job.main([flag, "--domain", "tartecosmetics.com", "--domain", "jsmbeauty.sg", "--after", "ext:x"]) == 2
    assert "PROOF_ERROR" in capsys.readouterr().err


def test_main_refuses_to_crawl_without_the_crawl_egress_flag_dry_run_included(monkeypatch, capsys):
    async def must_not_run(*a, **k):
        raise AssertionError("ran without --on-crawl-egress")

    monkeypatch.setattr(job, "run", must_not_run)
    for extra in ([], ["--apply"]):
        assert job.main(["--domain", "tartecosmetics.com", *extra]) == job.EXIT_BAD_ARGS
    assert "--on-crawl-egress" in capsys.readouterr().err


@pytest.mark.parametrize("outcome,code", [("ok", 0), ("aborted", 1), ("crash", 3)])
def test_main_exit_codes_tell_aborted_from_crashed(monkeypatch, capsys, outcome, code):
    async def fake_run(db, client, plans, **kwargs):
        if outcome == "crash":
            raise RuntimeError("boom")
        return {"aborted_on_block": outcome == "aborted", "domains": {}}

    monkeypatch.setattr(job, "run", fake_run)
    assert job.main(["--on-crawl-egress", "--domain", "tartecosmetics.com"]) == code
    out = capsys.readouterr()
    if outcome == "crash":
        assert "PROOF_CRASH RuntimeError" in out.err and "PROOF_REPORT" not in out.out
    else:
        assert out.out.startswith("PROOF_REPORT ")


def test_the_request_gap_has_a_floor_and_the_abort_threshold_a_minimum():
    assert job.request_gap_s({}) == 3.0
    assert job.request_gap_s({"ENRICHMENT_PROOF_REQUEST_GAP_S": "0"}) == 1.5
    assert job.request_gap_s({"ENRICHMENT_PROOF_REQUEST_GAP_S": "nan"}) == 3.0
    assert job.request_gap_s({"ENRICHMENT_PROOF_REQUEST_GAP_S": "abc"}) == 3.0
    assert job.request_gap_s({"ENRICHMENT_PROOF_REQUEST_GAP_S": "7.5"}) == 7.5
    assert job.abort_after_blocks({}) == 5
    assert job.abort_after_blocks({"ENRICHMENT_PROOF_ABORT_AFTER_BLOCKS": "0"}) == 1


async def test_the_pacer_spaces_request_starts():
    clock = {"t": 100.0}
    slept: List[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    pacer = Pacer(3.0, clock=lambda: clock["t"], sleep=sleep)
    await pacer.wait()
    clock["t"] += 1.0
    await pacer.wait()
    clock["t"] += 5.0
    await pacer.wait()
    assert slept == [2.0] and pacer.requests == 3


def test_the_job_is_scheduled_only_through_the_dark_by_default_wrapper():
    """Its one scheduled caller is jobs/reap_cart_proof_refresh.py, provisioned by the operator-run
    infra/gcp/setup_reap_cart_proof_jobs.sh (dry run + paused unless --enable; that script's own
    test pins the dark default). Nothing else may name this module or that wrapper: not the worker's
    scheduler, not another infra file, not a workflow."""
    root = Path(__file__).resolve().parents[1]
    allowed = root / "infra" / "gcp" / "setup_reap_cart_proof_jobs.sh"
    names = ("enrichment_cart_variant_proof", "reap_cart_proof_refresh")
    for path in [root / "services" / "audit_scheduler.py", *sorted((root / "infra").rglob("*"))]:
        if path == allowed:
            continue
        if path.is_file() and path.suffix in (".py", ".sh", ".yaml", ".yml", ".json", ".tf"):
            text = path.read_text(encoding="utf-8", errors="replace")
            assert not any(name in text for name in names), path
    for path in sorted((root / ".github" / "workflows").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        assert not any(name in text for name in names), path
    # The wrapper passes an explicit domain list; this module still has no default population.
    assert job.main(["--on-crawl-egress"]) == job.EXIT_BAD_ARGS


def test_the_sources_and_outcomes_fit_the_table():
    from db.enrichment_cart_variant_proofs import PROOF_SOURCES

    assert (SOURCE_PRODUCTS_JSON, SOURCE_PRODUCTS_JS) == PROOF_SOURCES
    assert "ok" in job.WRITTEN_OUTCOMES
    assert not any(o.startswith("skip") for o in job.WRITTEN_OUTCOMES)


# ── the storefront, faked at the transport (real httpx: redirects, headers, cookie jar) ─────────


class Store:
    """A fake set of storefronts, with the real response shapes: `.js` is `text/javascript`,
    `/products.json` and `/meta.json` are `application/json`, and every response (a redirect hop
    included, as MAC's 301 does) sets `_shopify_essential`. `routes[(host, path)]` is
    (status, body, cookie currencies[, extra headers]) or ("redirect", location[, hop cookies]).
    Records every request."""

    def __init__(self) -> None:
        self.routes: Dict[tuple, Any] = {}
        self.requests: List[httpx.Request] = []

    def js(self, host, handle, name, *, status=200, currencies=None, override_body=None):
        b = override_body if override_body is not None else FIXTURES[name]["body"]
        cur = FIXTURES[name]["set_cookie_cart_currency"] if currencies is None else currencies
        self.routes[(host, f"/products/{handle}.js")] = (status, b, cur)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        spec = self.routes.get((request.url.host, request.url.path))
        if spec is None:
            return httpx.Response(404, headers={"content-type": "text/html"}, text="<html>404</html>")
        if spec[0] == "redirect":
            hop_cookies = spec[2] if len(spec) > 2 else ["_shopify_essential=hop; path=/"]
            status = spec[3] if len(spec) > 3 else 301
            return httpx.Response(status, headers=[("location", spec[1] + request.url.path + "?" + request.url.query.decode())]
                                  + [("set-cookie", c) for c in hop_cookies])
        status, payload, currencies = spec[:3]
        extra = list(spec[3]) if len(spec) > 3 else []
        if status != 200:
            return httpx.Response(status, headers=[("content-type", "text/html")] + extra, text="<html>no</html>")
        if payload == "__html__":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>challenge</html>")
        ctype = "text/javascript; charset=utf-8" if request.url.path.endswith(".js") else "application/json; charset=utf-8"
        headers = [("content-type", ctype)]
        headers += [("set-cookie", "_shopify_essential=abc; path=/")]
        headers += [("set-cookie", f"cart_currency={c}; path=/") for c in currencies]
        if callable(payload):
            payload = payload(request)
        return httpx.Response(200, headers=headers, content=json.dumps(payload, default=str).encode())

    def client(self) -> httpx.AsyncClient:
        # A PLAIN client, whose jar keeps every cookie: the job itself must keep them off requests.
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def _crawl_politeness(monkeypatch):
    """The job gates every request through services.crawl_politeness: no real robots.txt fetch and
    no real per-host interval in tests (a robots test turns robots back on with its own transport)."""
    from services import crawl_politeness

    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "false")
    monkeypatch.setenv("CRAWL_BACKOFF_BASE_SECONDS", "0")
    crawl_politeness.reset_for_tests()
    yield
    crawl_politeness.reset_for_tests()


class NoSleepPacer(Pacer):
    def __init__(self):
        super().__init__(0.0)


def _tick():
    state = {"t": CHECKED}

    def now():
        state["t"] = state["t"] + timedelta(seconds=1)
        return state["t"]
    return now


TARTE_HOST = "tarte.ecvpjob.test"
MAC_HOST = "mac.ecvpjob.test"
TARTE_PLAN = DomainPlan(domain=TARTE_HOST, market="US", market_currency="USD")
MAC_PLAN = DomainPlan(domain=MAC_HOST, market="US", market_currency="USD")


def rehost(rows, host):
    """The same producer rows, on a test-only host (the shared test DB selects by domain)."""
    out = json.loads(json.dumps(rows))
    old = out["product"]["source_domain"]
    out["product"]["source_domain"] = host
    out["product"]["canonical_url"] = out["product"]["canonical_url"].replace(old, host)
    return out


RUN_TARTE = rehost(TARTE, TARTE_HOST)
RUN_TARTE_SINGLE = rehost(TARTE_SINGLE, TARTE_HOST)
RUN_MAC_FAMILY = rehost(MAC_FAMILY, MAC_HOST)
RUN_MAC_FIX = rehost(MAC_FIX, MAC_HOST)
PREFIX = "ext:ecvpjob-"


async def _clear(db) -> None:
    from db.enrichment_cart_variant_proofs import TABLE

    for table in ("catalog_offers", "catalog_skus", "catalog_products", TABLE):
        await db.execute(f"DELETE FROM {table} WHERE substr(product_key, 1, {len(PREFIX)}) = :p", {"p": PREFIX})


def _json_param(db_is_pg: bool) -> str:
    return "CAST(:payload AS jsonb)" if db_is_pg else ":payload"


async def insert_rows(db, rows, *, suppressed_skus=(), offers=None) -> None:
    from db.database import IS_POSTGRES

    p = rows["product"]
    await db.execute(
        """INSERT INTO catalog_products
             (product_key, merchant_id, platform, source_product_id, source_system, source_domain,
              canonical_url, title, brand)
           VALUES (:pk, 'merch_obs_ecvpjob', 'external_seed', :spid, :ss, :dom, :url, :title, :brand)""",
        {"pk": p["product_key"], "spid": p["product_key"][:128], "ss": p["source_system"], "dom": p["source_domain"],
         "url": p["canonical_url"], "title": p["title"], "brand": p["brand"]})
    for s in rows["skus"]:
        sup = s["sku_key"] in suppressed_skus
        await db.execute(
            f"""INSERT INTO catalog_skus
                  (sku_key, product_key, merchant_id, platform, source_product_id, source_variant_id, title,
                   readiness_tier, sku_payload, suppressed_at, suppression_reason)
                VALUES (:sk, :pk, 'merch_obs_ecvpjob', 'external_seed', :spid, :svid, :title, 'referral_only',
                        {_json_param(IS_POSTGRES)}, :sup_at, :sup_reason)""",
            {"sk": s["sku_key"], "pk": p["product_key"], "spid": s["source_product_id"],
             "svid": s["source_variant_id"], "title": s["title"], "payload": s["sku_payload"],
             "sup_at": CHECKED if sup else None, "sup_reason": "fixture" if sup else None})
    for i, (sku_key, price, currency) in enumerate(offers or ()):
        await db.execute(
            """INSERT INTO catalog_offers
                 (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier, readiness_tier,
                  offer_mode, channel, market, availability, currency, list_price, source_system)
               VALUES (:oid, :sk, :pk, 'agent_seed::ecvpjob', 'external_referral', 'observed', 'referral_only',
                       'redirect', 'external_referral', 'US', 'in_stock', :cur, :price, :ss)""",
            {"oid": f"offer:ecvpjob:{p['product_key'][-12:]}:{i}", "sk": sku_key, "pk": p["product_key"],
             "cur": currency, "price": Decimal(price) if IS_POSTGRES else float(price), "ss": SS})


@pytest.fixture
async def job_db():
    import db.enrichment_cart_variant_proofs as proofs
    from db.catalog import catalog_offers, catalog_products, catalog_skus
    from db.database import database
    from tests.model_schema import ensure_model_tables

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await ensure_model_tables([catalog_products, catalog_skus, catalog_offers])
    proofs._reset_for_tests()
    assert await proofs.ensure_table()
    await _clear(database)
    yield database
    await _clear(database)
    proofs._reset_for_tests()
    if not was_connected and database.is_connected:
        await database.disconnect()


def as_utc(value: Any) -> datetime:
    """A stored TIMESTAMPTZ as an aware datetime. Postgres returns one; the SQLite test build hands
    back the UTC wall time as naive text, which is UTC by construction (the job binds aware UTC)."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)


async def proofs_by_sku(db) -> Dict[str, Dict[str, Any]]:
    from db.enrichment_cart_variant_proofs import TABLE

    rows = await db.fetch_all(
        f"SELECT * FROM {TABLE} WHERE substr(product_key, 1, {len(PREFIX)}) = :p", {"p": PREFIX})
    return {r["sku_key"]: dict(r) for r in rows}


async def test_the_selection_reads_live_enrichment_skus_and_counts_every_variant_sku(job_db):
    other = json.loads(json.dumps(RUN_TARTE))
    other["product"]["source_system"] = "some_other_lane"
    other["product"]["product_key"] = PREFIX + "other-lane::00000000"
    for s in other["skus"]:
        s["sku_key"] = s["sku_key"].replace(RUN_TARTE["product"]["product_key"], other["product"]["product_key"])
    await insert_rows(job_db, other)
    shade = sku_of(RUN_TARTE, "::v:63530896753009")["sku_key"]
    await insert_rows(job_db, RUN_TARTE, suppressed_skus={shade})
    www = rehost(TARTE_SINGLE, "www." + TARTE_HOST)
    await insert_rows(job_db, www)
    withdrawn = rehost(build_rows("ecvpjob tarte", "withdrawn thing", "x", "withdrawn-thing", []), TARTE_HOST)
    await insert_rows(job_db, withdrawn)
    await job_db.execute("UPDATE catalog_products SET suppression_reason = 'fixture' WHERE product_key = :pk",
                         {"pk": withdrawn["product"]["product_key"]})
    rows = await job.select_targets(job_db, TARTE_HOST, limit=10, after=None)
    got = [(r["product_key"], r["sku_key"], int(r["catalog_variant_sku_count"])) for r in rows]
    pk = RUN_TARTE["product"]["product_key"]
    single = www["product"]["product_key"]
    expected = sorted([
        (pk, pk + "::canonical", 3),
        (pk, pk + "::v:63530896785777", 3),
        (pk, pk + "::v:63530896818545", 3),  # pink (...753009) is suppressed: not proven, but counted
        (single, single + "::canonical", 0),
    ])
    assert sorted(got) == expected
    # The cursor pages past a product; the limit bounds products, not skus.
    first = await job.select_targets(job_db, TARTE_HOST, limit=1, after=None)
    assert {r["product_key"] for r in first} == {min(pk, single)}
    rest = await job.select_targets(job_db, TARTE_HOST, limit=10, after=min(pk, single))
    assert {r["product_key"] for r in rest} == {max(pk, single)}


async def test_the_selection_does_not_read_a_key_with_an_underscore_as_a_wildcard(job_db):
    rows = json.loads(json.dumps(RUN_TARTE_SINGLE))
    old = rows["product"]["product_key"]
    pk = PREFIX + "under_score::00000000"
    rows["product"]["product_key"] = pk
    rows["skus"] = [dict(rows["skus"][0], sku_key=pk + "::canonical", source_variant_id=pk)]
    assert old != pk
    # `LIKE pk || '::v:%'` would read the `_` as any character and count this sku as a variant.
    decoy_key = pk.replace("_", "x") + "::v:123"
    rows["skus"].append({**rows["skus"][0], "sku_key": decoy_key, "source_variant_id": "123"})
    await insert_rows(job_db, rows)
    got = await job.select_targets(job_db, TARTE_HOST, limit=10, after=None)
    assert [(r["sku_key"], int(r["catalog_variant_sku_count"])) for r in got] == [(pk + "::canonical", 0)]


async def test_a_dry_run_reads_the_storefront_and_writes_nothing(job_db):
    await insert_rows(job_db, RUN_TARTE, offers=[(sku_of(RUN_TARTE, "::v:63530896818545")["sku_key"], "30.00", "USD")])
    store = Store()
    store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=False, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
    d = report["domains"][TARTE_HOST]
    assert report["mode"] == "dry_run" and d["written"] == 0
    # Three shades 'ok' (two of them sold out: recorded as such), the placeholder refused.
    assert d["outcomes"] == {"ok": 3, PLACEHOLDER_HAS_VARIANT_SKUS: 1}
    assert d["currency_read"] == {"USD": 1}
    assert d["price_check"] == {"ok": 1, "row_unpriced": 2}
    assert await proofs_by_sku(job_db) == {}
    # One request per handle, carrying no cookies, asking for the market's country.
    assert len(store.requests) == 1
    request = store.requests[0]
    assert "cookie" not in request.headers and request.url.params.get("country") == "US"


async def test_apply_writes_one_row_per_sku_that_the_reader_accepts_and_a_rerun_is_idempotent(job_db):
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
        assert report["domains"][TARTE_HOST]["written"] == 4
        first = await proofs_by_sku(job_db)
        pk = RUN_TARTE["product"]["product_key"]
        berry = first[pk + "::v:63530896818545"]
        assert (berry["outcome"], berry["variant_id"], berry["live_variant_count"], berry["live_price_minor"],
                berry["currency"], berry["source"], berry["shop_host"], berry["handle"]) == (
            "ok", "63530896818545", 3, 3000, "USD", "products_js_v1", TARTE_HOST, "amazonian-clay-baked-blush")
        assert first[pk + "::canonical"]["outcome"] == PLACEHOLDER_HAS_VARIANT_SKUS
        berry_sku = sku_of(RUN_TARTE, "::v:63530896818545")
        stored = {**berry, "available": bool(berry["available"]), "checked_at": as_utc(berry["checked_at"])}
        verdict = verify_enrichment_cart_proof(RUN_TARTE["product"], berry_sku, stored, catalog_variant_sku_count=3,
                                               now=as_utc(berry["checked_at"]) + timedelta(hours=1))
        assert verdict == (True, "63530896818545", "named_variant")
        # Again: same keys, same row count, newer checked_at and updated_at, created_at kept.
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=lambda: CHECKED + timedelta(hours=2))
    second = await proofs_by_sku(job_db)
    assert set(second) == set(first) and report["domains"][TARTE_HOST]["written"] == 4
    for key in first:
        assert str(second[key]["created_at"]) == str(first[key]["created_at"])
        assert as_utc(second[key]["checked_at"]) > as_utc(first[key]["checked_at"])
        assert as_utc(second[key]["updated_at"]) == CHECKED + timedelta(hours=2)


async def test_an_older_reading_never_overwrites_a_newer_proof(job_db):
    await insert_rows(job_db, RUN_TARTE)
    newer = ProofRow(product_key=RUN_TARTE["product"]["product_key"],
                     sku_key=sku_of(RUN_TARTE, "::v:63530896818545")["sku_key"], shop_host=TARTE_HOST,
                     handle="amazonian-clay-baked-blush", source=SOURCE_PRODUCTS_JS,
                     checked_at=CHECKED + timedelta(hours=5), outcome=VARIANT_GONE, live_variant_count=3)
    assert await job.upsert_proof(job_db, newer, written_at=CHECKED + timedelta(hours=5))
    older = ProofRow(**{**newer.__dict__, "checked_at": CHECKED, "outcome": "ok", "variant_id": "63530896818545",
                        "available": True, "live_price_minor": 3000, "currency": "USD"})
    assert not await job.upsert_proof(job_db, older, written_at=CHECKED + timedelta(hours=6))
    stored = (await proofs_by_sku(job_db))[newer.sku_key]
    assert stored["outcome"] == VARIANT_GONE and stored["variant_id"] is None
    with pytest.raises(ValueError):
        await job.upsert_proof(job_db, ProofRow(**{**newer.__dict__, "outcome": "skip_transient"}),
                               written_at=CHECKED)
    with pytest.raises(ValueError):
        await job.upsert_proof(job_db, ProofRow(**{**newer.__dict__, "source": "cart_js"}), written_at=CHECKED)


async def test_a_transient_read_writes_nothing_and_keeps_the_prior_proof(job_db):
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
    async with store.client() as client:
        await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                      pacer=NoSleepPacer(), now=_tick())
        before = await proofs_by_sku(job_db)
        for status_or_body in (503, "__html__"):
            if status_or_body == "__html__":
                store.routes[(TARTE_HOST, "/products/amazonian-clay-baked-blush.js")] = (200, "__html__", [])
            else:
                store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush",
                         status=status_or_body)
            report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                                   pacer=NoSleepPacer(), now=lambda: CHECKED + timedelta(hours=3))
            d = report["domains"][TARTE_HOST]
            assert d["written"] == 0 and d["outcomes"] == {}
            assert sum(d["skipped"].values()) == 4
    assert await proofs_by_sku(job_db) == before


async def test_a_deleted_handle_revokes_and_a_renamed_one_is_a_handle_mismatch(job_db):
    await insert_rows(job_db, RUN_TARTE)
    await insert_rows(job_db, RUN_TARTE_SINGLE)
    store = Store()  # the blush 404s
    renamed = dict(FIXTURES["tarte_products_json_page1_plain"]["body"]["products"][0], handle="front-row-energy-v2")
    store.js(TARTE_HOST, "front-row-energy-travel-essentials", "tarte_js_amazonian_clay_baked_blush",
             override_body=renamed, currencies=["USD"])
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
    assert report["domains"][TARTE_HOST]["outcomes"] == {REVOKED_404: 4, HANDLE_MISMATCH: 1}
    stored = await proofs_by_sku(job_db)
    single = stored[RUN_TARTE_SINGLE["product"]["product_key"] + "::canonical"]
    # The row records the handle it was asked for, never the storefront's new one.
    assert (single["outcome"], single["handle"]) == (HANDLE_MISMATCH, "front-row-energy-travel-essentials")


async def test_a_redirect_to_www_is_the_same_store_and_to_another_host_is_not(job_db):
    await insert_rows(job_db, RUN_MAC_FAMILY)
    await insert_rows(job_db, RUN_MAC_FIX)
    store = Store()
    store.routes[(MAC_HOST, "/products/studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control-nc50.js")] = (
        "redirect", f"https://www.{MAC_HOST}")
    store.js("www." + MAC_HOST, "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control-nc50", "mac_js_shade_nc50")
    store.routes[(MAC_HOST, "/products/studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control.js")] = (
        "redirect", f"https://www.{MAC_HOST}")
    store.js("www." + MAC_HOST, "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control", "mac_js_family_parent_stub")
    store.routes[(MAC_HOST, "/products/fix.js")] = ("redirect", "https://uk.mac.ecvpjob.test")
    store.js("uk." + MAC_HOST, "fix", "mac_js_fix_plus_canonical_only")
    # NC10 is gone from the store.
    async with store.client() as client:
        report = await job.run(job_db, client, [MAC_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
    stored = await proofs_by_sku(job_db)
    fam = RUN_MAC_FAMILY["product"]["product_key"]
    assert stored[fam + "::v:54057385787587"]["outcome"] == "ok"
    assert stored[fam + "::v:54057385787587"]["shop_host"] == MAC_HOST
    assert stored[fam + "::v:54057377923267"]["outcome"] == REVOKED_404
    assert stored[fam + "::canonical"]["outcome"] == PARENT_STUB
    assert stored[RUN_MAC_FIX["product"]["product_key"] + "::canonical"]["outcome"] == HOST_REDIRECTED
    assert report["domains"][MAC_HOST]["outcomes"] == {"ok": 1, REVOKED_404: 1, PARENT_STUB: 1, HOST_REDIRECTED: 1}


def _page(products_name, handles=None):
    products = FIXTURES[products_name]["body"]["products"]
    return {"products": [p for p in products if handles is None or p["handle"] in handles]}


async def test_a_complete_listing_proves_what_it_lists_and_falls_back_to_js_for_the_rest(job_db):
    await insert_rows(job_db, RUN_TARTE_SINGLE)
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    pages = {"1": _page("tarte_products_json_page1_plain"), "2": {"products": []}}
    store.routes[(TARTE_HOST, "/products.json")] = (200, lambda r: pages[r.url.params["page"]], ["USD"])
    store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JSON,
                               pacer=NoSleepPacer(), now=_tick())
    d = report["domains"][TARTE_HOST]
    assert d["hosts"][TARTE_HOST]["listing"] == {"pages": 2, "complete": True, "stop": None, "products_seen": 2,
                                                  "duplicate_handles": 0}
    stored = await proofs_by_sku(job_db)
    single = stored[RUN_TARTE_SINGLE["product"]["product_key"] + "::canonical"]
    assert (single["outcome"], single["source"], single["live_price_minor"]) == ("ok", "products_json_v1", 3500)
    berry = stored[RUN_TARTE["product"]["product_key"] + "::v:63530896818545"]
    assert (berry["outcome"], berry["source"]) == ("ok", "products_js_v1")
    assert [r.url.path for r in store.requests] == ["/products.json", "/products.json",
                                                    "/products/amazonian-clay-baked-blush.js"]
    assert all(r.url.params.get("country") == "US" for r in store.requests)
    # Every response set cookies; no request sent one back (a returned cookie could suppress the
    # Set-Cookie that is this job's only evidence of the currency).
    assert not any("cookie" in r.headers for r in store.requests)


async def test_an_incomplete_listing_writes_nothing_for_the_handles_it_did_not_reach(job_db):
    await insert_rows(job_db, RUN_TARTE_SINGLE)
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    pages = {"1": _page("tarte_products_json_page1_plain")}

    def page(request):
        return pages[request.url.params["page"]]

    store.routes[(TARTE_HOST, "/products.json")] = (200, page, ["USD"])
    pages["2"] = pages["1"]  # a repeated page: paging did not advance
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JSON,
                               pacer=NoSleepPacer(), now=_tick())
    d = report["domains"][TARTE_HOST]
    listing = d["hosts"][TARTE_HOST]["listing"]
    assert listing["complete"] is False and listing["stop"] == "page_2_repeated"
    assert d["skipped"] == {"skip_listing_incomplete": 4}
    stored = await proofs_by_sku(job_db)
    assert set(stored) == {RUN_TARTE_SINGLE["product"]["product_key"] + "::canonical"}


async def test_a_handle_listed_twice_is_read_by_js_instead(job_db):
    await insert_rows(job_db, RUN_TARTE_SINGLE)
    store = Store()
    first = _page("tarte_products_json_page1_plain")
    drifted = json.loads(json.dumps(first["products"][0]))
    drifted["variants"][0]["price"] = "1.00"  # the same handle again, read differently
    pages = {"1": first, "2": {"products": [drifted]}, "3": {"products": []}}
    store.routes[(TARTE_HOST, "/products.json")] = (200, lambda r: pages[r.url.params["page"]], ["USD"])
    js_body = json.loads(json.dumps(first["products"][0]))
    js_body["variants"][0]["price"] = 3500  # the `.js` shape: x100 integer
    store.js(TARTE_HOST, "front-row-energy-travel-essentials", "tarte_js_amazonian_clay_baked_blush",
             override_body=js_body, currencies=["USD"])
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JSON,
                               pacer=NoSleepPacer(), now=_tick())
    d = report["domains"][TARTE_HOST]
    listing = d["hosts"][TARTE_HOST]["listing"]
    assert listing["duplicate_handles"] == 1 and listing["complete"] is True
    stored = (await proofs_by_sku(job_db))[RUN_TARTE_SINGLE["product"]["product_key"] + "::canonical"]
    assert (stored["outcome"], stored["source"], stored["live_price_minor"]) == ("ok", "products_js_v1", 3500)


async def test_auto_pages_the_listing_only_when_that_is_fewer_requests(job_db):
    await insert_rows(job_db, RUN_TARTE_SINGLE)
    await insert_rows(job_db, RUN_TARTE)
    for published, expected in ((600, SOURCE_PRODUCTS_JS), (100, SOURCE_PRODUCTS_JS), (0, SOURCE_PRODUCTS_JSON)):
        store = Store()
        store.routes[(TARTE_HOST, "/meta.json")] = (200, {"published_products_count": published, "currency": "USD"}, [])
        store.routes[(TARTE_HOST, "/products.json")] = (200, lambda r: {"products": []}, ["USD"])
        store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
        store.js(TARTE_HOST, "front-row-energy-travel-essentials", "tarte_js_amazonian_clay_baked_blush",
                 override_body=FIXTURES["tarte_products_json_page1_plain"]["body"]["products"][0])
        async with store.client() as client:
            report = await job.run(job_db, client, [TARTE_PLAN], apply=False, pacer=NoSleepPacer(), now=_tick())
        assert report["domains"][TARTE_HOST]["hosts"][TARTE_HOST]["source_mode"] == expected, published


async def test_consecutive_blocks_abort_the_whole_run(job_db):
    await insert_rows(job_db, RUN_TARTE)
    await insert_rows(job_db, RUN_TARTE_SINGLE)
    store = Store()
    store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush", status=429)
    store.js(TARTE_HOST, "front-row-energy-travel-essentials", "tarte_js_amazonian_clay_baked_blush", status=403)
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN, MAC_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), block_limit=2, now=_tick())
    assert report["aborted_on_block"] is True
    assert list(report["domains"]) == [TARTE_HOST]  # the next domain was never started
    assert report["domains"][TARTE_HOST]["next_cursor"] is None
    assert await proofs_by_sku(job_db) == {}


async def test_the_drift_report_compares_the_live_price_to_the_catalog_offer(job_db):
    berry = sku_of(RUN_TARTE, "::v:63530896818545")["sku_key"]
    peach = sku_of(RUN_TARTE, "::v:63530896785777")["sku_key"]
    pink = sku_of(RUN_TARTE, "::v:63530896753009")["sku_key"]
    await insert_rows(job_db, RUN_TARTE, offers=[(berry, "34.00", "USD"), (peach, "30.00", "USD"),
                                                   (pink, "30.00", "USD"), (pink, "32.00", "USD"),
                                                   (peach, "99.00", "USD")])
    # A suppressed offer is not an offer: peach's $99 must not make it ambiguous.
    await job_db.execute("UPDATE catalog_offers SET suppressed_at = :at WHERE offer_id = :oid",
                         {"at": CHECKED, "oid": f"offer:ecvpjob:{RUN_TARTE['product']['product_key'][-12:]}:4"})
    store = Store()
    store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=False, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
    d = report["domains"][TARTE_HOST]
    assert d["price_check"] == {"ok": 1, "row_price_stale": 1, "row_price_ambiguous": 1}
    stale = next(s for s in d["price_drift_samples"] if s["reason"] == "row_price_stale")
    assert stale["sku_key"] == berry and stale["live_price_minor"] == 3000


async def test_a_challenge_page_neither_aborts_nor_resets_the_block_streak(job_db):
    third = rehost(build_rows("ecvpjob tarte", "zz third", "x", "zz-third", []), TARTE_HOST)
    for rows in (RUN_TARTE, RUN_TARTE_SINGLE, third):
        await insert_rows(job_db, rows)
    store = Store()
    store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush", status=429)
    store.routes[(TARTE_HOST, "/products/front-row-energy-travel-essentials.js")] = (200, "__html__", [])
    store.js(TARTE_HOST, "zz-third", "tarte_js_amazonian_clay_baked_blush", status=429)
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), block_limit=2, now=_tick())
    assert report["aborted_on_block"] is True
    assert report["domains"][TARTE_HOST]["fetches"] == {"rate_limited": 2, "not_json": 1}


def test_a_domain_listed_under_two_markets_is_refused(tmp_path):
    path = tmp_path / "merchants.json"
    path.write_text(json.dumps([{"domain": "brand.com", "market": "US"}, {"domain": "brand.com", "market": "GB"},
                                {"domain": "other.com", "market": "BR"}]))
    with pytest.raises(job.MerchantListError, match="exactly one market"):
        job.plan_domains(["brand.com"], merchants_path=str(path))
    with pytest.raises(job.MerchantListError, match="no currency"):
        job.plan_domains(["other.com"], merchants_path=str(path))


# ── review round 1 (#2464): product identity, variant title, cookies, redirects, pacing ─────────


def test_a_handle_now_held_by_another_product_is_product_changed_and_keeps_the_recorded_id():
    t = target(TARTE, "::v:63530896818545")
    evidence = js_evidence("tarte_js_amazonian_clay_baked_blush")
    same = job.SkuTarget(**{**t.__dict__, "prior_product_id": "15531947491697"})
    assert decide_proof(same, evidence, market_currency="USD").outcome == "ok"
    other = job.SkuTarget(**{**t.__dict__, "prior_product_id": "99999999999999"})
    proof = decide_proof(other, evidence, market_currency="USD")
    assert (proof.outcome, proof.shopify_product_id, proof.variant_id, proof.live_price_minor) == (
        job.PRODUCT_CHANGED, "99999999999999", None, None)


@pytest.mark.parametrize("catalog_title,outcome", [
    ("front row energy travel essentials", "ok"),
    ("  FRONT  ROW\tenergy travel ESSENTIALS\n", "ok"),          # case and whitespace fold
    ("front row energy travel essentials​", "ok"),         # a zero-width char is dropped
    ("front row energy travel essentials 2", job.PRODUCT_CHANGED),
    ("something else entirely", job.PRODUCT_CHANGED),
    (None, job.PRODUCT_CHANGED),
    ("   ", job.PRODUCT_CHANGED),
])
def test_a_placeholder_is_ok_only_when_the_live_title_is_the_catalogs(catalog_title, outcome):
    t = target(TARTE_SINGLE, "::canonical")
    t = job.SkuTarget(**{**t.__dict__, "catalog_title": catalog_title})
    proof = decide_proof(t, listing_evidence("tarte_products_json_page1_plain", "front-row-energy-travel-essentials"),
                         market_currency="USD")
    assert proof.outcome == outcome


def test_titles_compare_in_nfc():
    decomposed = "Café Blush"  # e + combining acute
    composed = "Café blush"
    assert job.title_key(decomposed) == job.title_key(composed)
    assert job.title_key(None) is None and job.title_key(3) is None


def test_the_variant_title_is_recorded_cleaned_for_display():
    proof = decide_proof(target(MAC_FAMILY, "::v:54057385787587"), js_evidence("mac_js_shade_nc50"), market_currency="USD")
    assert proof.variant_title == "NC50 / 1 fl oz"
    b = body("tarte_js_amazonian_clay_baked_blush")
    b["variants"][2]["title"] = " ber‮ry\n  red​ "
    ev = evidence_from_product(b, requested_handle=b["handle"], source=SOURCE_PRODUCTS_JS, checked_at=CHECKED,
                               currency="USD", currency_problem=None)
    proof = decide_proof(target(TARTE, "::v:63530896818545"), ev, market_currency="USD")
    assert (proof.outcome, proof.variant_title) == ("ok", "berry red")
    # No variant identified, no title.
    gone = decide_proof(target(MAC_FAMILY, "::canonical"), js_evidence("mac_js_family_parent_stub"), market_currency="USD")
    assert gone.variant_title is None


def test_the_no_cookie_client_keeps_no_cookie():
    async def go():
        def handler(request):
            return httpx.Response(200, headers=[("set-cookie", "cart_currency=GBP; path=/"),
                                                ("set-cookie", "localization=GB; path=/")], json={})
        async with job.no_cookie_client(transport=httpx.MockTransport(handler)) as client:
            await client.get("https://tarte.ecvpjob.test/cart.js")
            assert list(client.cookies.jar) == []
    import asyncio
    asyncio.run(go())


async def test_a_redirect_hops_cookies_never_reach_the_next_hop_and_only_the_final_cookie_counts(job_db):
    await insert_rows(job_db, RUN_MAC_FAMILY)
    store = Store()
    nc50 = "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control-nc50"
    store.routes[(MAC_HOST, f"/products/{nc50}.js")] = (
        "redirect", f"https://www.{MAC_HOST}",
        ["_shopify_essential=hop; path=/", "cart_currency=GBP; path=/", "localization=GB; path=/"])
    store.js("www." + MAC_HOST, nc50, "mac_js_shade_nc50")
    async with store.client() as client:  # a PLAIN client: its jar would keep the hop's cookies
        pacer = NoSleepPacer()
        report = await job.run(job_db, client, [MAC_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=pacer, now=_tick())
    stored = (await proofs_by_sku(job_db))[RUN_MAC_FAMILY["product"]["product_key"] + "::v:54057385787587"]
    assert (stored["outcome"], stored["currency"], stored["variant_title"]) == ("ok", "USD", "NC50 / 1 fl oz")
    hops = [r for r in store.requests if r.url.path.endswith(nc50 + ".js")]
    assert [r.url.host for r in hops] == [MAC_HOST, "www." + MAC_HOST]
    assert not any("cookie" in r.headers for r in store.requests)
    assert hops[1].url.params.get("country") == "US"
    # Every hop is a paced request start.
    assert report["requests"] >= 2 and pacer.requests == len(store.requests)


async def test_a_redirect_off_https_is_host_redirected_and_never_requested(job_db):
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    store.routes[(TARTE_HOST, "/products/amazonian-clay-baked-blush.js")] = ("redirect", f"http://{TARTE_HOST}")
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
    assert report["domains"][TARTE_HOST]["outcomes"] == {HOST_REDIRECTED: 4}
    assert [r.url.scheme for r in store.requests] == ["https"]


async def test_a_gone_handle_answering_410_is_revoked(job_db):
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush", status=410)
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
    assert report["domains"][TARTE_HOST]["outcomes"] == {REVOKED_404: 4}


async def test_each_listing_page_is_read_in_its_own_currency(job_db):
    await insert_rows(job_db, RUN_TARTE_SINGLE)
    pearls = rehost(build_rows("ecvpjob tarte", "pretty in pearls lash curler", "x", "pretty-in-pearls-lash-curler", []),
                    TARTE_HOST)
    await insert_rows(job_db, pearls)
    store = Store()
    first, second = _page("tarte_products_json_page1_plain")["products"]
    gb_second = next(p for p in FIXTURES["tarte_products_json_page1_country_GB"]["body"]["products"]
                     if p["handle"] == second["handle"])
    pages = {"1": ({"products": [first]}, ["USD"]), "2": ({"products": [gb_second]}, ["GBP"]), "3": ({"products": []}, ["USD"])}

    def handler(request):
        store.requests.append(request)
        payload, currencies = pages[request.url.params["page"]]
        return httpx.Response(200, headers=[("content-type", "application/json")]
                              + [("set-cookie", f"cart_currency={c}; path=/") for c in currencies], json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JSON,
                               pacer=NoSleepPacer(), now=_tick())
    stored = await proofs_by_sku(job_db)
    assert stored[RUN_TARTE_SINGLE["product"]["product_key"] + "::canonical"]["outcome"] == "ok"
    gb = stored[pearls["product"]["product_key"] + "::canonical"]
    assert (gb["outcome"], gb["currency"]) == (CURRENCY_NOT_MARKET, "GBP")
    assert report["domains"][TARTE_HOST]["currency_read"] == {"USD": 1, "GBP": 1}


async def test_a_listing_that_redirects_off_the_store_refuses_every_handle(job_db):
    await insert_rows(job_db, RUN_TARTE)
    await insert_rows(job_db, RUN_TARTE_SINGLE)
    store = Store()
    store.routes[(TARTE_HOST, "/products.json")] = ("redirect", "https://uk.tarte.ecvpjob.test")
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JSON,
                               pacer=NoSleepPacer(), now=_tick())
    d = report["domains"][TARTE_HOST]
    assert d["outcomes"] == {HOST_REDIRECTED: 5}
    assert d["hosts"][TARTE_HOST]["listing"]["stop"] == HOST_REDIRECTED
    assert {r.url.host for r in store.requests} == {TARTE_HOST}


@pytest.mark.parametrize("table,column", [
    ("catalog_products", "suppressed_at"), ("catalog_products", "suppression_reason"),
    ("catalog_skus", "suppressed_at"), ("catalog_skus", "suppression_reason"),
])
async def test_either_suppression_column_alone_withdraws_the_row(job_db, table, column):
    await insert_rows(job_db, RUN_TARTE)
    pk = RUN_TARTE["product"]["product_key"]
    berry = sku_of(RUN_TARTE, "::v:63530896818545")["sku_key"]
    value = CHECKED if column == "suppressed_at" else "fixture"
    where = "product_key = :k" if table == "catalog_products" else "sku_key = :k"
    await job_db.execute(f"UPDATE {table} SET {column} = :v WHERE {where}",
                         {"v": value, "k": pk if table == "catalog_products" else berry})
    got = {r["sku_key"] for r in await job.select_targets(job_db, TARTE_HOST, limit=10, after=None)}
    if table == "catalog_products":
        assert got == set()
    else:
        assert berry not in got and pk + "::canonical" in got


async def test_the_same_instant_rewrites_and_an_older_one_does_not(job_db):
    await insert_rows(job_db, RUN_TARTE)
    row = ProofRow(product_key=RUN_TARTE["product"]["product_key"],
                   sku_key=sku_of(RUN_TARTE, "::v:63530896818545")["sku_key"], shop_host=TARTE_HOST,
                   handle="amazonian-clay-baked-blush", source=SOURCE_PRODUCTS_JS, checked_at=CHECKED,
                   outcome=VARIANT_GONE, live_variant_count=3)
    assert await job.upsert_proof(job_db, row, written_at=CHECKED)
    again = ProofRow(**{**row.__dict__, "outcome": REVOKED_404})
    assert await job.upsert_proof(job_db, again, written_at=CHECKED)
    assert (await proofs_by_sku(job_db))[row.sku_key]["outcome"] == REVOKED_404


async def test_a_reused_handle_is_product_changed_and_stays_so(job_db):
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
    async with store.client() as client:
        await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                      pacer=NoSleepPacer(), now=_tick())
        reborn = dict(FIXTURES["tarte_js_amazonian_clay_baked_blush"]["body"], id=16000000000001)
        store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush", override_body=reborn)
        for hours in (1, 2):  # sticky: the second run does not turn the new product into an 'ok'
            report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                                   pacer=NoSleepPacer(), now=lambda: CHECKED + timedelta(hours=hours))
            assert report["domains"][TARTE_HOST]["outcomes"] == {job.PRODUCT_CHANGED: 4}
            stored = await proofs_by_sku(job_db)
            assert {r["shopify_product_id"] for r in stored.values()} == {"15531947491697"}
            assert {r["outcome"] for r in stored.values()} == {job.PRODUCT_CHANGED}


async def test_a_dry_run_with_no_proof_table_reads_no_prior(job_db):
    from db.enrichment_cart_variant_proofs import TABLE, _reset_for_tests

    await insert_rows(job_db, RUN_TARTE)
    await job_db.execute(f"DROP TABLE {TABLE}")
    _reset_for_tests()
    try:
        assert await job.select_prior_product_ids(job_db, [RUN_TARTE["product"]["product_key"]],
                                                  table_must_exist=False) == {}
        with pytest.raises(Exception):
            await job.select_prior_product_ids(job_db, [RUN_TARTE["product"]["product_key"]], table_must_exist=True)
    finally:
        import db.enrichment_cart_variant_proofs as proofs

        assert await proofs.ensure_table()


async def test_a_blocked_meta_json_counts_as_a_block(job_db):
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    store.routes[(TARTE_HOST, "/meta.json")] = (429, None, [])
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=False, pacer=NoSleepPacer(), block_limit=1,
                               now=_tick())
    assert report["aborted_on_block"] is True
    assert report["domains"][TARTE_HOST]["fetches"] == {"rate_limited": 1}


async def test_retry_after_arms_the_crawl_lanes_backoff(job_db):
    import time as _time

    from services import crawl_politeness

    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    store.routes[(TARTE_HOST, "/products/amazonian-clay-baked-blush.js")] = (429, None, [], [("retry-after", "120")])
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=False, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), block_limit=1, now=_tick())
    assert report["aborted_on_block"] is True
    held = crawl_politeness._STATE[TARTE_HOST].backoff_until - _time.monotonic()
    assert 100 < held <= 120


async def test_a_path_robots_disallows_is_skipped_and_nothing_is_written(job_db, monkeypatch):
    from services import crawl_politeness

    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    token = crawl_politeness.ROBOTS_TRANSPORT_FACTORY.set(lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, text="User-agent: *\nDisallow: /products/\n")))
    try:
        await insert_rows(job_db, RUN_TARTE)
        store = Store()
        store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
        async with store.client() as client:
            report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                                   pacer=NoSleepPacer(), now=_tick())
    finally:
        crawl_politeness.ROBOTS_TRANSPORT_FACTORY.reset(token)
    d = report["domains"][TARTE_HOST]
    assert d["skipped"] == {"skip_transient:robots_disallowed": 4} and d["written"] == 0
    assert store.requests == []


async def test_the_cursor_is_given_only_while_products_remain(job_db):
    await insert_rows(job_db, RUN_TARTE)
    await insert_rows(job_db, RUN_TARTE_SINGLE)
    store = Store()
    async with store.client() as client:
        first = await job.run(job_db, client, [TARTE_PLAN], apply=False, source_mode=SOURCE_PRODUCTS_JS, limit=1,
                              pacer=NoSleepPacer(), now=_tick())
        d = first["domains"][TARTE_HOST]
        assert (d["exhausted"], d["next_cursor"]) == (False, RUN_TARTE["product"]["product_key"])
        rest = await job.run(job_db, client, [TARTE_PLAN], apply=False, source_mode=SOURCE_PRODUCTS_JS, limit=1,
                             after=d["next_cursor"], pacer=NoSleepPacer(), now=_tick())
        d = rest["domains"][TARTE_HOST]
        assert d["products"] == 1 and (d["exhausted"], d["next_cursor"]) == (False, RUN_TARTE_SINGLE["product"]["product_key"])
        done = await job.run(job_db, client, [TARTE_PLAN], apply=False, source_mode=SOURCE_PRODUCTS_JS, limit=5,
                             pacer=NoSleepPacer(), now=_tick())
        d = done["domains"][TARTE_HOST]
        assert (d["products"], d["exhausted"], d["next_cursor"]) == (2, True, None)


async def test_a_prior_refusal_without_a_product_id_does_not_block_the_product(job_db):
    """Only a RECORDED product id is an identity: a 404 row (no id) followed by the product coming
    back is an ordinary 'ok'."""
    await insert_rows(job_db, RUN_TARTE)
    store = Store()  # the blush 404s first
    async with store.client() as client:
        await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                      pacer=NoSleepPacer(), now=_tick())
        assert {r["shopify_product_id"] for r in (await proofs_by_sku(job_db)).values()} == {None}
        store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=lambda: CHECKED + timedelta(hours=1))
    assert report["domains"][TARTE_HOST]["outcomes"] == {"ok": 3, PLACEHOLDER_HAS_VARIANT_SKUS: 1}


async def test_a_dry_run_on_an_environment_that_never_wrote_a_proof_still_runs(job_db):
    from db.enrichment_cart_variant_proofs import TABLE, _reset_for_tests

    import db.enrichment_cart_variant_proofs as proofs

    await insert_rows(job_db, RUN_TARTE)
    await job_db.execute(f"DROP TABLE {TABLE}")
    _reset_for_tests()
    try:
        store = Store()
        store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
        async with store.client() as client:
            report = await job.run(job_db, client, [TARTE_PLAN], apply=False, source_mode=SOURCE_PRODUCTS_JS,
                                   pacer=NoSleepPacer(), now=_tick())
        assert report["domains"][TARTE_HOST]["outcomes"] == {"ok": 3, PLACEHOLDER_HAS_VARIANT_SKUS: 1}
    finally:
        assert await proofs.ensure_table()


async def test_a_rerun_updates_the_recorded_variant_title(job_db):
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
    berry = sku_of(RUN_TARTE, "::v:63530896818545")["sku_key"]
    async with store.client() as client:
        await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                      pacer=NoSleepPacer(), now=_tick())
        assert (await proofs_by_sku(job_db))[berry]["variant_title"] == "berry"
        renamed = json.loads(json.dumps(FIXTURES["tarte_js_amazonian_clay_baked_blush"]["body"]))
        renamed["variants"][2]["title"] = "berry (new)"
        store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush", override_body=renamed)
        await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                      pacer=NoSleepPacer(), now=lambda: CHECKED + timedelta(hours=1))
    assert (await proofs_by_sku(job_db))[berry]["variant_title"] == "berry (new)"



# ── review round 2 (#2464): the identity anchor survives, pacing never crashes, redirects ───────


async def test_a_404_between_two_readings_does_not_erase_the_product_anchor(job_db):
    """The reviewer's repro: A ok -> B product_changed -> 404 (knows no product) -> B again. The
    fourth run must still say product_changed, and the stored id must stay A's throughout."""
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    a_body = FIXTURES["tarte_js_amazonian_clay_baked_blush"]["body"]
    b_body = dict(a_body, id=16000000000001)
    handle = "amazonian-clay-baked-blush"
    runs = [("A", a_body, {"ok": 3, PLACEHOLDER_HAS_VARIANT_SKUS: 1}),
            ("B", b_body, {job.PRODUCT_CHANGED: 4}),
            ("404", None, {REVOKED_404: 4}),
            ("B", b_body, {job.PRODUCT_CHANGED: 4})]
    async with store.client() as client:
        for hour, (label, served, expected) in enumerate(runs):
            if served is None:
                store.routes.pop((TARTE_HOST, f"/products/{handle}.js"), None)
            else:
                store.js(TARTE_HOST, handle, "tarte_js_amazonian_clay_baked_blush", override_body=served)
            report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                                   pacer=NoSleepPacer(), now=lambda h=hour: CHECKED + timedelta(hours=h))
            assert report["domains"][TARTE_HOST]["outcomes"] == expected, label
            stored = await proofs_by_sku(job_db)
            assert {r["shopify_product_id"] for r in stored.values()} == {"15531947491697"}, label


def _slow_backoff(monkeypatch):
    """A Retry-After the crawl lane clamps to 3 s, and a job that waits at most 1 s for a slot."""
    monkeypatch.setenv("CRAWL_MAX_BACKOFF_SECONDS", "3")
    monkeypatch.setenv("ENRICHMENT_PROOF_MAX_POLITE_WAIT_S", "1")


async def test_a_host_held_longer_than_the_job_waits_is_crawl_paced_not_a_crash(job_db, monkeypatch):
    _slow_backoff(monkeypatch)
    await insert_rows(job_db, RUN_TARTE)
    await insert_rows(job_db, RUN_TARTE_SINGLE)
    store = Store()
    store.routes[(TARTE_HOST, "/products/amazonian-clay-baked-blush.js")] = (429, None, [], [("retry-after", "120")])
    store.js(TARTE_HOST, "front-row-energy-travel-essentials", "tarte_js_amazonian_clay_baked_blush")
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
    d = report["domains"][TARTE_HOST]
    assert report["aborted_on_block"] is False
    assert d["fetches"] == {"rate_limited": 1, "crawl_paced": 1}
    assert d["skipped"] == {"skip_transient:rate_limited": 4, "skip_transient:crawl_paced": 1}
    assert [r.url.path for r in store.requests] == ["/products/amazonian-clay-baked-blush.js"]
    assert await proofs_by_sku(job_db) == {}


def test_the_polite_wait_is_bounded_and_never_forever():
    assert job.max_polite_wait_s({}) == 60.0
    assert job.max_polite_wait_s({"ENRICHMENT_PROOF_MAX_POLITE_WAIT_S": "5"}) == 5.0
    for bad in ("0", "-1", "nan", "inf", "x"):
        assert job.max_polite_wait_s({"ENRICHMENT_PROOF_MAX_POLITE_WAIT_S": bad}) == 60.0, bad


async def test_a_backoff_from_the_www_twin_holds_the_next_apex_request(job_db, monkeypatch):
    """MAC answers from www after a 301. A 429 there must hold the storefront, not only www: the
    next handle starts at the apex again."""
    _slow_backoff(monkeypatch)
    await insert_rows(job_db, RUN_MAC_FAMILY)
    store = Store()
    nc50 = "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control-nc50"
    nc10 = "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control-nc10"
    for handle in (nc10, nc50, "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control"):
        store.routes[(MAC_HOST, f"/products/{handle}.js")] = ("redirect", f"https://www.{MAC_HOST}")
    store.routes[("www." + MAC_HOST, "/products/studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control.js")] = (
        429, None, [], [("retry-after", "120")])
    async with store.client() as client:
        report = await job.run(job_db, client, [MAC_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
    d = report["domains"][MAC_HOST]
    assert d["fetches"] == {"rate_limited": 1, "crawl_paced": 2}
    # The parent (first by sku order) went apex -> www -> 429; nothing was sent after it.
    assert [(r.url.host, r.url.path.rsplit("/", 1)[-1]) for r in store.requests] == [
        (MAC_HOST, "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control.js"),
        ("www." + MAC_HOST, "studio-fix-fluid-spf-15-24hr-matte-foundation-oil-control.js")]


async def test_a_crawl_delay_over_the_cap_skips_the_whole_host(job_db, monkeypatch):
    from services import crawl_politeness

    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    token = crawl_politeness.ROBOTS_TRANSPORT_FACTORY.set(lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, text="User-agent: *\nCrawl-delay: 100000\n")))
    try:
        await insert_rows(job_db, RUN_TARTE)
        await insert_rows(job_db, RUN_TARTE_SINGLE)
        store = Store()
        store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
        store.js(TARTE_HOST, "front-row-energy-travel-essentials", "tarte_js_amazonian_clay_baked_blush")
        async with store.client() as client:
            report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                                   pacer=NoSleepPacer(), now=_tick())
    finally:
        crawl_politeness.ROBOTS_TRANSPORT_FACTORY.reset(token)
    d = report["domains"][TARTE_HOST]
    assert store.requests == []
    assert d["fetches"] == {"crawl_delay_too_long": 1}  # asked once, then the host is skipped
    assert d["skipped"] == {"skip_transient:crawl_delay_too_long": 5} and d["written"] == 0


def _chain_handler(store, steps):
    """`steps[(host, path, query)]` -> an httpx.Response factory; records requests."""
    def handler(request):
        store.requests.append(request)
        key = (request.url.host, request.url.path, request.url.query.decode())
        return steps[key]()
    return handler


async def test_a_two_hop_chain_with_a_308_and_a_relative_location_is_followed(job_db):
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    path = "/products/amazonian-clay-baked-blush.js"
    body = FIXTURES["tarte_js_amazonian_clay_baked_blush"]["body"]
    steps = {
        (TARTE_HOST, path, "country=US"): lambda: httpx.Response(
            301, headers={"location": f"https://www.{TARTE_HOST}{path}?country=US"}),
        ("www." + TARTE_HOST, path, "country=US"): lambda: httpx.Response(
            308, headers={"location": f"{path}?country=US&hop=2"}),  # relative
        ("www." + TARTE_HOST, path, "country=US&hop=2"): lambda: httpx.Response(
            200, headers=[("content-type", "text/javascript; charset=utf-8"),
                          ("set-cookie", "cart_currency=USD; path=/")], json=body),
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(_chain_handler(store, steps))) as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
    assert report["domains"][TARTE_HOST]["outcomes"] == {"ok": 3, PLACEHOLDER_HAS_VARIANT_SKUS: 1}
    assert len(store.requests) == 3


async def test_a_redirect_loop_stops_at_the_cap_as_a_transient(job_db):
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    path = "/products/amazonian-clay-baked-blush.js"
    steps = {(TARTE_HOST, path, "country=US"): lambda: httpx.Response(302, headers={"location": f"{path}?country=US"})}
    async with httpx.AsyncClient(transport=httpx.MockTransport(_chain_handler(store, steps))) as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
    d = report["domains"][TARTE_HOST]
    assert d["skipped"] == {"skip_transient:too_many_redirects": 4} and d["written"] == 0
    assert len(store.requests) == job.MAX_REDIRECTS + 1


async def test_a_redirect_without_a_location_is_never_a_404(job_db):
    await insert_rows(job_db, RUN_TARTE)
    store = Store()
    path = "/products/amazonian-clay-baked-blush.js"
    steps = {(TARTE_HOST, path, "country=US"): lambda: httpx.Response(301)}
    async with httpx.AsyncClient(transport=httpx.MockTransport(_chain_handler(store, steps))) as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), now=_tick())
    d = report["domains"][TARTE_HOST]
    assert d["outcomes"] == {} and d["skipped"] == {"skip_transient:redirect_without_location": 4}
    assert await proofs_by_sku(job_db) == {}


async def test_a_request_robots_kept_us_from_sending_neither_counts_nor_resets_the_block_streak(job_db, monkeypatch):
    from services import crawl_politeness

    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "true")
    token = crawl_politeness.ROBOTS_TRANSPORT_FACTORY.set(lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, text="User-agent: *\nDisallow: /products/front-row\n")))
    try:
        third = rehost(build_rows("ecvpjob tarte", "zz third", "x", "zz-third", []), TARTE_HOST)
        for rows in (RUN_TARTE, RUN_TARTE_SINGLE, third):
            await insert_rows(job_db, rows)
        store = Store()
        store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush", status=429)
        store.js(TARTE_HOST, "zz-third", "tarte_js_amazonian_clay_baked_blush", status=429)
        async with store.client() as client:
            report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                                   pacer=NoSleepPacer(), block_limit=2, now=_tick())
    finally:
        crawl_politeness.ROBOTS_TRANSPORT_FACTORY.reset(token)
    assert report["aborted_on_block"] is True
    assert report["domains"][TARTE_HOST]["fetches"] == {"rate_limited": 2, "robots_disallowed": 1}


async def test_a_crawl_paced_request_neither_counts_toward_nor_resets_the_block_streak(job_db, monkeypatch):
    """429 on the apex (held 3 s, the job waits 1 s) -> the apex's next handle is crawl_paced (not
    sent) -> a 429 from the www twin's own product is the SECOND block, and aborts at limit 2. Had
    crawl_paced counted, the run would abort before the www request; had it reset, it never would."""
    _slow_backoff(monkeypatch)
    www = "www." + TARTE_HOST
    on_www = rehost(build_rows("ecvpjob tarte", "zz on www", "x", "zz-on-www", []), www)
    for rows in (RUN_TARTE, RUN_TARTE_SINGLE, on_www):
        await insert_rows(job_db, rows)
    store = Store()
    store.routes[(TARTE_HOST, "/products/amazonian-clay-baked-blush.js")] = (429, None, [], [("retry-after", "120")])
    store.js(TARTE_HOST, "front-row-energy-travel-essentials", "tarte_js_amazonian_clay_baked_blush")
    store.js(www, "zz-on-www", "tarte_js_amazonian_clay_baked_blush", status=429)
    async with store.client() as client:
        report = await job.run(job_db, client, [TARTE_PLAN], apply=True, source_mode=SOURCE_PRODUCTS_JS,
                               pacer=NoSleepPacer(), block_limit=2, now=_tick())
    assert report["aborted_on_block"] is True
    assert report["domains"][TARTE_HOST]["fetches"] == {"rate_limited": 2, "crawl_paced": 1}
    assert [(r.url.host, r.url.path) for r in store.requests] == [
        (TARTE_HOST, "/products/amazonian-clay-baked-blush.js"), (www, "/products/zz-on-www.js")]


# ── the crawl-safety hooks (#2473 breaker, #2474 shared Shopify-edge pacer) ─────────────────────


async def test_should_stop_aborts_the_domain_at_the_next_fetch(job_db):
    """jobs/reap_cart_proof_refresh.py passes its IP-throttle breaker as `should_stop`: once it has
    tripped, the writer stops asking after the fetch in hand, exactly as a block streak aborts."""
    await insert_rows(job_db, RUN_TARTE)
    await insert_rows(job_db, RUN_TARTE_SINGLE)
    store = Store()
    store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
    store.js(TARTE_HOST, "front-row-energy-travel-essentials", "tarte_js_amazonian_clay_baked_blush")
    async with store.client() as client:
        report = await job.run_domain(job_db, client, TARTE_PLAN, apply=False, source_mode=SOURCE_PRODUCTS_JS,
                                      limit=job.DEFAULT_LIMIT, after=None, pacer=NoSleepPacer(), block_limit=5,
                                      block_state={"consecutive": 0}, now=_tick(), should_stop=lambda: True)
    assert report["aborted_on_block"] is True
    assert len(store.requests) == 1, "nothing more is asked once should_stop says so"
    async with store.client() as client:
        report = await job.run_domain(job_db, client, TARTE_PLAN, apply=False, source_mode=SOURCE_PRODUCTS_JS,
                                      limit=job.DEFAULT_LIMIT, after=None, pacer=NoSleepPacer(), block_limit=5,
                                      block_state={"consecutive": 0}, now=_tick(), should_stop=lambda: False)
    assert report["aborted_on_block"] is False


async def test_the_breaker_hears_the_requested_hosts_answer_with_its_headers():
    """note_response gets the response headers (so an installed IP breaker counts the throttle), for the
    URL actually requested only: after a redirect to the www twin, one store is ONE throttling host."""
    from services import crawl_ip_throttle

    store = Store()
    store.routes[("brand.ecvpjob.test", "/products/h.js")] = ("redirect", "https://www.brand.ecvpjob.test")
    store.routes[("www.brand.ecvpjob.test", "/products/h.js")] = (429, None, [], [("retry-after", "30")])
    breaker = crawl_ip_throttle.IpThrottleBreaker(trip_hosts=2, window_seconds=600)
    with crawl_ip_throttle.installed(breaker):
        async with store.client() as client:
            fetched = await job.fetch_json(client, "https://brand.ecvpjob.test/products/h.js",
                                           requested_host="brand.ecvpjob.test", pacer=NoSleepPacer(), now=_tick())
    assert fetched.outcome == "rate_limited"
    assert breaker.throttled_hosts == {"www.brand.ecvpjob.test"}, breaker.throttled_hosts
    assert breaker.summary()["throttle_diagnostics"]["retry_after"] == {"30": 1}
    assert not breaker.tripped


async def test_every_request_is_marked_shopify_and_takes_a_shared_edge_slot(monkeypatch):
    """With CRAWL_SHOPIFY_EDGE_PACER_ENABLED, the writer's hosts are Shopify-served from the FIRST
    request (it only ever asks Shopify endpoints), so every request takes a slot of the shared budget."""
    from services import shopify_edge_pacer

    monkeypatch.setenv("CRAWL_SHOPIFY_EDGE_PACER_ENABLED", "true")
    shopify_edge_pacer.reset_for_tests()
    leases: List[int] = []

    async def lease(bucket, *, slots, rate_per_s):
        leases.append(slots)
        return 0.0, 0.0

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(shopify_edge_pacer, "_lease_fn", lease)
    monkeypatch.setattr(shopify_edge_pacer, "_sleep", no_sleep)
    try:
        store = Store()
        store.js(TARTE_HOST, "amazonian-clay-baked-blush", "tarte_js_amazonian_clay_baked_blush")
        async with store.client() as client:
            for _ in range(3):
                await job.fetch_json(client, job.product_js_url(TARTE_HOST, "amazonian-clay-baked-blush", "US"),
                                     requested_host=TARTE_HOST, pacer=NoSleepPacer(), now=_tick())
        assert shopify_edge_pacer.is_shopify_host(TARTE_HOST)
        assert shopify_edge_pacer.stats()["granted"] == 3 and leases
    finally:
        shopify_edge_pacer.reset_for_tests()
