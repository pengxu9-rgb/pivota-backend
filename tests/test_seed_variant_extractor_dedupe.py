"""One page read yields ONE entry per merchant variant: no plans, no barcode ids, no double reads.

THE DEFECT (production rule 2026-10-16: the main list `seed_data.variants` is what serves). 6,748
active seeds carry >1 main-list entry, mostly defective, and nearly every main list is written
from ONE page read (`_extract_from_html` -> `seed_variants_from_evidence`). Three shapes came out
of a single read:

  1. the same variant read twice -- once by its Shopify link `@id`, once by a node carrying its
     SKU (jurlique eps_019f2b9eef9a429385cccc5c);
  2. a 13-digit GTIN as a variant id, which the identity classifier calls MERCHANT_ISSUED;
  3. subscription plans as variants (murad eps_0f70f7177cecd95416d98b53: `offer_1..N` titled
     "One-Time Purchase", "1 month", "2 months" at the plans' prices).

Pages are JSON-LD shaped as Shopify themes and subscription apps emit them, and go through the
REAL extractor; nothing below hand-builds a variant dict.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.parse import urlparse

import pytest

from services.external_offers_service import _extract_from_html, seed_variants_from_evidence
from services.variant_identity import MERCHANT_ISSUED, variant_id_provenance
from tests.test_external_seed_refresh_writes_variants import DEST, IN, OUT, TITLE, _offer, _page
from tests.test_seed_refresh_keeps_merchant_variants import V15, V50

HANDLE = DEST.rsplit("/products/", 1)[1]
STORE = f"https://{urlparse(DEST).hostname}"
GTIN50, GTIN15 = "8809640733451", "8809640733468"
TRANSIENT = ("_offer_ref", "_price_refused", "_id_source", "_selling_plan")


def _link_id(variant: str, handle: str = HANDLE) -> Dict[str, str]:
    return {"@id": f"/products/{handle}?variant={variant}#offer"}


def _link_url(variant: str, handle: str = HANDLE) -> Dict[str, str]:
    return {"url": f"{STORE}/products/{handle}?variant={variant}"}


def _product(offers: Any, **extra: Any) -> Dict[str, Any]:
    return {"@context": "https://schema.org", "@type": "Product", "name": TITLE,
            "image": f"{STORE}/cdn/p.jpg", "offers": offers, **extra}


def _html(*blocks: Dict[str, Any]) -> str:
    scripts = "".join('<script type="application/ld+json">' + json.dumps(b) + "</script>" for b in blocks)
    return f"<html><head><title>{TITLE}</title>{scripts}</head><body></body></html>"


def _extract(*blocks: Dict[str, Any]) -> Dict[str, Any]:
    return _extract_from_html(DEST, _html(*blocks))


def _variants(*blocks: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _extract(*blocks)["variants"]


def _seed_list(*blocks: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What a seed writer adopts as the main list from this page (seed create / refresh)."""
    return seed_variants_from_evidence(_page(html=_html(*blocks)).evidence)


# ==== (a) a subscription plan is not a variant ===============================================


def _murad_offers(*, plans_first: bool = False) -> List[Dict[str, Any]]:
    """Recharge/Skio-style JSON-LD: positional offers (no sku, no @id), one per purchase mode."""
    one_time = _offer(48, IN, name="One-Time Purchase")
    plans = [_offer(40.80, IN, name=n) for n in ("1 month", "2 months", "3 months")]
    return plans + [one_time] if plans_first else [one_time] + plans


def test_the_murad_shape_reads_as_the_one_time_offer_alone():
    out = _extract(_product(_murad_offers()))
    assert [(v["variant_id"], v["title"], v["price_amount"]) for v in out["variants"]] == [("offer_1", TITLE, 48.0)]
    census = out["variant_census"]
    assert (census["offers"], census["exact_prices"], census["selling_plans"]) == (1, [48.0], 3)
    assert census["duplicate_ids"] == 0  # the plans' second visits as lone Offer nodes are gone too


def test_plans_listed_first_do_not_become_the_products_price():
    # The product-level price is `offers[0]`; with the plans first that was the plan's 40.80, and
    # a census of one offer would then hand it to a single stored variant as the product's.
    out = _extract(_product(_murad_offers(plans_first=True)))
    assert [(v["title"], v["price_amount"]) for v in out["variants"]] == [(TITLE, 48.0)]
    assert out["price_amount"] == 48.0
    assert out["variant_census"]["offers"] == 1


def test_the_seed_writer_adopts_one_entry_with_no_read_marks():
    (entry,) = _seed_list(_product(_murad_offers()))
    assert (entry["title"], entry["price_amount"]) == (TITLE, 48.0)
    assert not any(k in entry for k in TRANSIENT + ("offer_shopify_variant_id", "price_exact"))


@pytest.mark.parametrize("plan_name", [
    "Subscribe & Save 15%", "Subscription - every 30 days", "Auto-Replenish", "Delivery every 2 months",
    "Ships every 4 weeks", "Every 6 weeks",
])
def test_a_plan_named_as_one_is_dropped_beside_any_sibling(plan_name):
    variants = _variants(_product([_offer(48, IN, name="Travel 15 mL"), _offer(40, IN, name=plan_name)]))
    assert [v["title"] for v in variants] == ["Travel 15 mL"]


@pytest.mark.parametrize("plan", [
    {"sellingPlan": {"id": 691234, "name": "Deliver every month"}},
    {"selling_plan_id": 691234},
    {"url": f"{STORE}/products/{HANDLE}?variant={V50}&selling_plan=691234"},
    {"priceSpecification": {"@type": "UnitPriceSpecification", "price": "40.80", "priceCurrency": "USD",
                            "billingDuration": 1, "unitCode": "MON"}},
    # `billingDuration` alone (no unit) is a recurring charge too.
    {"priceSpecification": [{"@type": "UnitPriceSpecification", "price": "40.80", "priceCurrency": "USD",
                             "billingDuration": 1}]},
], ids=["sellingPlan_object", "selling_plan_id", "selling_plan_link", "billing_duration_monthly",
        "billing_duration_alone"])
def test_a_structural_plan_is_dropped_even_when_it_carries_an_id(plan):
    # The one-time offer and the plan both name V50; the plan is cheaper. Only the one-time stays,
    # and the plan is not a second listing of V50 either.
    offers = [_offer(48, IN, **_link_id(V50)), _offer(40.80, IN, sku="PLAN-50", **plan)]
    out = _extract(_product(offers))
    assert [(v["variant_id"], v["price_amount"]) for v in out["variants"]] == [(_link_id(V50)["@id"], 48.0)]
    assert (out["variant_census"]["offers"], out["variant_census"]["selling_plans"]) == (1, 1)


@pytest.mark.parametrize("offers", [
    # "N Month Supply" is a SIZE (supplements): not a bare cadence.
    [_offer(30, IN, name="1 Month Supply"), _offer(80, IN, name="3 Month Supply")],
    # A bare cadence with no one-time or plan sibling says nothing about subscriptions.
    [_offer(30, IN, name="1 month"), _offer(80, IN, name="3 months")],
    # Every offer plan-named: a subscription box sold by duration keeps its offers.
    [_offer(30, IN, name="Subscribe monthly"), _offer(80, IN, name="Subscribe quarterly")],
    # Unit pricing ("per 100 ml") is a one-time sale's reference quantity, not a billing period.
    [_offer(30, IN, sku="A30", priceSpecification={"@type": "UnitPriceSpecification", "price": "30",
      "priceCurrency": "USD", "referenceQuantity": {"value": 100, "unitCode": "MLT"}}),
     _offer(50, IN, sku="A50")],
    # A theme's one-time link with an EMPTY selling_plan=.
    [_offer(30, IN, sku="A30", url=f"{STORE}/products/{HANDLE}?variant={V50}&selling_plan="), _offer(50, IN, sku="A50")],
    # `billingIncrement` alone is billing granularity, not recurrence.
    [_offer(30, IN, sku="A30", priceSpecification={"price": "30", "priceCurrency": "USD", "billingIncrement": 1}),
     _offer(50, IN, sku="A50")],
    # A SKU-keyed offer is never judged by its name.
    [_offer(30, IN, sku="SUB1", name="Subscribe & Save"), _offer(50, IN, sku="A50")],
], ids=["month_supply_sizes", "cadence_without_context", "all_plan_named", "unit_pricing",
        "empty_selling_plan", "billing_increment_only", "sku_offer_named_subscribe"])
def test_twin_offers_that_are_not_plans_are_kept(offers):
    assert len(_variants(_product(offers))) == 2


def test_twin_a_lone_offer_is_never_dropped_by_its_name():
    # A product NAMED like a subscription ("Beauty Subscription Box") has one offer named as it.
    variants = _variants(_product([_offer(25, IN, name="Beauty Subscription Box")], name="Beauty Subscription Box"))
    assert [v["price_amount"] for v in variants] == [25.0]


# ==== (b) a barcode is not a variant id =======================================================


def test_a_gtin_beside_the_variant_link_is_the_barcode_not_the_id():
    (v,) = _variants(_product([_offer(31, IN, gtin13=GTIN50, **_link_id(V50))]))
    assert v["variant_id"] == _link_id(V50)["@id"]
    assert (v["barcode"], v["offer_shopify_variant_id"]) == (GTIN50, V50)


@pytest.mark.parametrize("key", ["gtin13", "gtin", "gtin14", "gtin12", "gtin8"])
@pytest.mark.parametrize("level", ["offer", "item"])
def test_no_gtin_is_ever_an_id_the_classifier_calls_merchant_issued(key, level):
    def size(price, gtin):
        ids = {key: gtin}
        return _offer(price, IN, **(ids if level == "offer" else {"itemOffered": {"@type": "Product", **ids}}))

    variants = _variants(_product([size(48, GTIN50), size(30, GTIN15)]))
    assert [v["barcode"] for v in variants] == [GTIN50, GTIN15]
    assert [v["variant_id"] for v in variants] == ["offer_1", "offer_2"]
    assert not any(variant_id_provenance(v["variant_id"]) == MERCHANT_ISSUED for v in variants)


def test_a_sku_still_outranks_the_link_and_keeps_the_barcode_beside_it():
    (v,) = _variants(_product([_offer(31, IN, sku="114807", gtin13=GTIN50, **_link_id(V50))]))
    assert (v["variant_id"], v["barcode"]) == ("114807", GTIN50)


# ==== (c) one variant read twice is one entry =================================================


def test_the_jurlique_shape_folds_into_the_link_entry_keeping_the_sku():
    # Block 1: the theme's Product, the variant named only by its link `@id`. Block 2: an app's
    # Product, the same variant carrying its SKU and the same ?variant= in its `url`.
    out = _extract(_product([_offer(31, IN, **_link_id(V50))]),
                   _product({"@type": "Offer", "price": "31.00", "priceCurrency": "USD", "availability": IN,
                             "sku": "114807", **_link_url(V50)}))
    (v,) = out["variants"]
    assert (v["variant_id"], v["sku"], v["offer_shopify_variant_id"]) == (_link_id(V50)["@id"], "114807", V50)
    assert (out["variant_census"]["offers"], out["variant_census"]["folded_duplicates"]) == (1, 1)
    assert "_id_source" not in v


def test_a_variant_named_by_link_and_by_its_numeric_id_keeps_the_numeric_id():
    # The #2631 prod shape: the link, and the variant id as `sku`. Kept: the bare numeric id.
    out = _extract(_product([_offer(100, IN, **_link_id(V50)), _offer(100, IN, sku=V50)]))
    (v,) = out["variants"]
    assert (v["variant_id"], v["offer_shopify_variant_id"]) == (V50, V50)
    assert variant_id_provenance(v["variant_id"]) == MERCHANT_ISSUED
    assert out["variant_census"]["offers"] == 1


def test_a_barcode_node_linking_the_variant_folds_into_it():
    # Rules (b)+(c): a node carrying only the GTIN and the variant's url used to be a 13-digit
    # "variant" beside the link entry at the same price.
    # The barcode node is keyed by the digits its url names, so it is the entry kept.
    out = _extract(_product([_offer(31, IN, **_link_id(V50)), _offer(31, IN, gtin13=GTIN50, **_link_url(V50))]))
    (v,) = out["variants"]
    assert (v["variant_id"], v["barcode"], v["offer_shopify_variant_id"]) == (V50, GTIN50, V50)


def test_a_folded_read_still_re_reads_the_drains_size():
    from routes.employee_products import _reconcile_seed_variants_with_read

    page = _page(html=_html(_product([_offer(85, IN, **_link_id(V50)), _offer(85, IN, sku=V50),
                                      _offer(34, IN, **_link_id(V15))])))
    stored = [{"variant_id": V50, "title": "50 mL", "price_amount": 80.0, "price_currency": "USD", "availability": "in_stock"},
              {"variant_id": V15, "title": "15 mL", "price_amount": 30.0, "price_currency": "USD", "availability": "in_stock"}]
    out, report = _reconcile_seed_variants_with_read(
        stored, page.evidence["variants"], product_amount=None, product_currency="USD",
        product_availability=None, census=page.evidence["variant_census"])
    assert [v["price_amount"] for v in out] == [85.0, 34.0]
    assert report["not_re_read_count"] == 0


@pytest.mark.parametrize("offers", [
    # A genuine multi-size product: two ?variant= ids at two prices.
    [_offer(48, IN, **_link_id(V50)), _offer(30, IN, **_link_id(V15))],
    # Two ?variant= ids at the SAME price (shades): equal price never folds.
    [_offer(30, IN, **_link_id(V50)), _offer(30, IN, **_link_id(V15))],
    # A SKU node with no link at all: nothing ties it to the link entry, whatever the price.
    [_offer(31, IN, **_link_id(V50)), _offer(31, IN, sku="114807")],
    # The same variant priced twice, differently: the reconcile must still see the dispute.
    [_offer(99, IN, **_link_id(V50)), _offer(100, IN, sku=V50)],
    # ... or in two currencies, or stocked differently.
    [_offer(31, IN, **_link_id(V50)), {**_offer(31, IN, sku=V50), "priceCurrency": "CAD"}],
    [_offer(31, IN, **_link_id(V50)), _offer(31, OUT, sku=V50)],
    # An entry keyed by one variant's id while linking another names nothing to fold on.
    [_offer(31, IN, **_link_id(V50)), _offer(31, IN, sku=V15, **_link_url(V50)), _offer(31, IN, **_link_id(V15))],
], ids=["multi_size", "same_price_shades", "unlinked_sku", "prices_disagree", "currencies_disagree",
        "stock_disagrees", "keyed_v15_linking_v50"])
def test_twin_entries_not_positively_the_same_variant_at_one_price_never_fold(offers):
    out = _extract(_product(offers))
    assert len(out["variants"]) == len(offers)
    assert out["variant_census"]["folded_duplicates"] == 0
    assert out["variant_census"]["offers"] == len(offers)


# ==== review of #2634 ==========================================================================


def _reconcile(stored: List[Dict[str, Any]], html: str, product_amount: Any = None):
    from routes.employee_products import _reconcile_seed_variants_with_read

    page = _page(html=html)
    return _reconcile_seed_variants_with_read(
        stored, page.evidence["variants"], product_amount=product_amount, product_currency="USD",
        product_availability=None, census=page.evidence["variant_census"], product_title=TITLE)


def _stored(vid: str, price: float, title: str = "50 mL") -> Dict[str, Any]:
    return {"variant_id": vid, "title": title, "price_amount": price, "price_currency": "USD", "availability": "in_stock"}


def _dawn_offers(price50: float = 48, price15: float = 30) -> List[Dict[str, Any]]:
    """Shopify Dawn: no sku, no @id -- the barcode and the variant's url."""
    return [_offer(price50, IN, gtin13=GTIN50, **_link_url(V50)), _offer(price15, IN, gtin13=GTIN15, **_link_url(V15))]


# ---- 1. a sku-less offer is keyed by the variant it links, and a stored barcode id still bridges


def test_a_dawn_offer_is_keyed_by_its_variant_not_its_position():
    variants = _variants(_product(_dawn_offers()))
    assert [(v["variant_id"], v["barcode"]) for v in variants] == [(V50, GTIN50), (V15, GTIN15)]
    assert all(variant_id_provenance(v["variant_id"]) == MERCHANT_ISSUED for v in variants)


def test_a_seed_stored_with_barcode_ids_is_still_re_read_by_the_barcode(monkeypatch):
    from tests.test_external_seed_refresh_writes_variants import _refresh, _seed_row

    row = _seed_row(variants=[_stored(GTIN50, 45.0), _stored(GTIN15, 28.0, "15 mL")], price=28.0, availability="in_stock")
    _, persisted = _refresh(monkeypatch, row, _page(html=_html(_product(_dawn_offers(48, 30)))))
    seed = persisted["seed_data"]
    assert {v["variant_id"]: v["price_amount"] for v in seed["variants"]} == {GTIN50: 48.0, GTIN15: 30.0}
    assert seed["snapshot"]["variant_refresh"]["status"] == "all_re_read"


def test_twin_offers_repeating_the_bare_product_url_stay_two_entries():
    offers = [_offer(48, IN, url=DEST), _offer(30, IN, url=DEST)]
    assert [v["variant_id"] for v in _variants(_product(offers))] == ["offer_1", "offer_2"]


def test_a_url_naming_another_products_variant_keys_by_the_url_but_marks_nothing():
    url = f"{STORE}/products/another-product?variant={V50}"
    (v,) = _variants(_product([_offer(48, IN, url=url)]))
    assert v["variant_id"] == url and "offer_shopify_variant_id" not in v


# ---- 2. a page whose every offer is a plan names no single value


def _plans_only_html(meta_price: float) -> str:
    offers = [_offer(40, IN, sku="A", url=f"{STORE}/products/{HANDLE}?variant={V15}&selling_plan=1"),
              _offer(60, IN, sku="B", url=f"{STORE}/products/{HANDLE}?variant={V50}&selling_plan=1")]
    meta = (f'<meta property="product:price:amount" content="{meta_price}">'
            '<meta property="product:price:currency" content="USD">')
    return _html(_product(offers)).replace("</head>", meta + "</head>")


def test_a_plans_only_page_never_hands_a_single_variant_another_variants_price(monkeypatch):
    # The page lists A at 40 and B at 60, both as plans; og:price is A's 40. The seed stores only B
    # at 60. Re-reading "the product's one price" would rewrite B to 40 and stamp it fresh.
    from tests.test_external_seed_refresh_writes_variants import _refresh, _seed_row

    page = _page(html=_plans_only_html(40))
    census = page.evidence["variant_census"]
    assert (census["offers"], census["selling_plans"], census["plans_only"]) == (0, 2, True)
    row = _seed_row(variants=[_stored("B", 60.0)], price=60.0, availability="in_stock")
    _, persisted = _refresh(monkeypatch, row, page)
    seed = persisted["seed_data"]
    assert [v["price_amount"] for v in seed["variants"]] == [60.0]
    assert seed["snapshot"]["variant_refresh"]["status"] == "not_all_re_read"
    assert "extracted_at" not in seed["snapshot"]


# ---- 3. one-time offers that only LOOK structural are kept, and keep the product price


@pytest.mark.parametrize("extra", [
    {"sellingPlan": False},
    {"selling_plan_id": 0},
    # "Pay in 4" installments: billed over 4 weeks, but a one-time sale.
    {"priceSpecification": [{"@type": "UnitPriceSpecification", "price": "12", "priceCurrency": "USD",
                             "priceComponentType": "https://schema.org/Installment",
                             "billingDuration": 4, "unitCode": "WEE"}]},
    {"priceSpecification": [{"@type": "UnitPriceSpecification", "price": "10", "priceCurrency": "USD",
                             "priceComponentType": "Downpayment", "billingDuration": 1}]},
    # Unit pricing per day / per month of supply is not a billing period.
    {"priceSpecification": {"@type": "UnitPriceSpecification", "price": "1.60", "priceCurrency": "USD",
                            "referenceQuantity": {"value": 1, "unitCode": "DAY"}}},
    {"priceSpecification": {"@type": "UnitPriceSpecification", "price": "48", "priceCurrency": "USD",
                            "referenceQuantity": {"value": 1, "unitCode": "MON"}}},
    # A unit on the spec with no billingDuration is not a recurring charge.
    {"priceSpecification": {"@type": "UnitPriceSpecification", "price": "48", "priceCurrency": "USD",
                            "unitText": "month"}},
    # A zero duration bills nothing recurring.
    {"priceSpecification": {"@type": "UnitPriceSpecification", "price": "48", "priceCurrency": "USD",
                            "billingDuration": 0}},
], ids=["sellingPlan_false", "selling_plan_id_zero", "installment", "downpayment",
        "per_day_unit_price", "per_month_unit_price", "unit_text_without_duration", "billing_duration_zero"])
def test_twin_a_one_time_offer_with_a_benign_spec_is_kept_and_prices_the_product(extra):
    out = _extract(_product([_offer(48, IN, sku="A50", **extra), _offer(30, IN, sku="A15")]))
    assert [v["variant_id"] for v in out["variants"]] == ["A50", "A15"]
    assert out["price_amount"] == 48.0  # the sitemap ingest reads this product-level price


# ---- 4. two shades behind one (wrong) link never fold


@pytest.mark.parametrize("a,b", [
    ({"sku": "LIP-RUBY", "name": "Ruby"}, {"sku": "LIP-CORAL", "name": "Coral"}),
    ({"gtin13": GTIN50}, {"gtin13": GTIN15}),
    ({"name": "Ruby"}, {"name": "Coral"}),
    ({"sku": "LIP-RUBY"}, {"sku": "LIP-CORAL"}),
], ids=["two_skus_two_titles", "two_barcodes", "two_titles", "two_skus_same_title"])
def test_two_variants_sharing_the_selected_variants_link_stay_apart(a, b):
    # A theme stamps the SELECTED variant's url on every offer.
    out = _extract(_product([_offer(22, IN, **_link_url(V50), **a), _offer(22, IN, **_link_url(V50), **b)]))
    assert len(out["variants"]) == 2
    assert out["variant_census"]["folded_duplicates"] == 0


def test_a_stored_two_shade_seed_re_reads_both_shades_behind_one_link():
    offers = [_offer(24, IN, sku="LIP-RUBY", name="Ruby", **_link_url(V50)),
              _offer(24, IN, sku="LIP-CORAL", name="Coral", **_link_url(V50))]
    out, report = _reconcile([_stored("LIP-RUBY", 22.0, "Ruby"), _stored("LIP-CORAL", 22.0, "Coral")],
                             _html(_product(offers)))
    assert [v["price_amount"] for v in out] == [24.0, 24.0]
    assert report["re_read"] == 2


def test_a_title_that_is_only_the_product_name_does_not_block_the_fold():
    out = _extract(_product([_offer(31, IN, name=TITLE, **_link_id(V50)),
                             _offer(31, IN, sku="114807", name="50 mL", **_link_url(V50))]))
    (v,) = out["variants"]
    assert v["sku"] == "114807"


# ---- 5. a correct sku-keyed list keeps every re-read after the fold


def test_a_seed_stored_by_sku_is_re_read_through_the_folded_entrys_sku():
    # Theme block: link entries. App block: the same variants carrying sku and url.
    html = _html(_product([_offer(85, IN, **_link_id(V50)), _offer(34, IN, **_link_id(V15))]),
                 _product([_offer(85, IN, sku="SKU-A", **_link_url(V50)), _offer(34, IN, sku="SKU-B", **_link_url(V15))]))
    assert [v.get("sku") for v in _page(html=html).evidence["variants"]] == ["SKU-A", "SKU-B"]
    out, report = _reconcile([_stored("SKU-A", 80.0), _stored("SKU-B", 30.0, "15 mL")], html)
    assert [v["price_amount"] for v in out] == [85.0, 34.0]
    assert report["re_read"] == 2 and report["not_re_read_count"] == 0


# ---- surviving mutants of the first review


@pytest.mark.parametrize("second", [
    {"@type": "Offer", "lowPrice": "31", "priceCurrency": "USD", "availability": IN, **_link_url(V50)},
    {"@type": "AggregateOffer", "lowPrice": "31", "highPrice": "31", "price": "31", "priceCurrency": "USD",
     "availability": IN, **_link_url(V50)},
], ids=["range_bound_price", "aggregate_offer"])
def test_twin_a_range_price_or_an_aggregate_offer_never_folds(second):
    out = _extract(_product([_offer(31, IN, **_link_id(V50)), second]))
    assert len(out["variants"]) == 2


def test_twin_a_bare_cadence_beside_an_ordinary_sibling_is_kept():
    # Only a one-time or plan sibling makes "2 months" a plan.
    assert len(_variants(_product([_offer(48, IN, name="Full size"), _offer(40, IN, name="2 months")]))) == 2


def test_twin_an_offer_named_as_its_product_is_never_dropped_by_its_name():
    name = "Glow Subscription Kit"
    variants = _variants(_product([_offer(30, IN, name=name), _offer(20, IN, name="Refill")], name=name))
    assert [v["price_amount"] for v in variants] == [30.0, 20.0]


@pytest.mark.parametrize("offers", [
    [_offer(22, IN, gtin13=GTIN50, **_link_url(V50)), _offer(22, IN, gtin13=GTIN15, **_link_url(V50))],
    [_offer(48, IN, name="Full size"), _offer(30, IN, name="Travel")],
], ids=["sibling_shared_link", "positional"])
def test_an_offers_second_visit_is_not_an_id_collision(offers):
    # The lone-Offer revisit re-keys an offer (`offer_1`, or the link its siblings shared); that is
    # the same offer, not a second one under that id.
    out = _extract(_product(offers))
    assert not any(v.get("id_collided") for v in out["variants"])
    assert out["variant_census"]["duplicate_ids"] == 0


# ==== second review of #2634 ===================================================================


# ---- 1. a stored id spelled as the @id link meets a read keyed by the digits


def test_a_list_stored_by_its_at_id_links_re_reads_beside_a_url_only_block():
    # The controller's repro: an @id block and a url-only block. The url-only offers are keyed by
    # their digits and the fold keeps the digits; the stored `@id` spellings must still meet them.
    html = _html(_product([_offer(85, IN, **_link_id(V50)), _offer(34, IN, **_link_id(V15))]),
                 _product([_offer(85, IN, **_link_url(V50)), _offer(34, IN, **_link_url(V15))]))
    assert [v["variant_id"] for v in _page(html=html).evidence["variants"]] == [V50, V15]
    out, report = _reconcile([_stored(_link_id(V50)["@id"], 80.0), _stored(_link_id(V15)["@id"], 30.0, "15 mL")], html)
    assert [v["price_amount"] for v in out] == [85.0, 34.0]
    assert (report["re_read"], report["not_re_read_count"]) == (2, 0)


@pytest.mark.parametrize("spell", [
    lambda variant, gtin, sku: _link_id(variant)["@id"],
    lambda variant, gtin, sku: gtin,
    lambda variant, gtin, sku: variant,
    lambda variant, gtin, sku: f"gid://shopify/ProductVariant/{variant}",
    lambda variant, gtin, sku: sku,
], ids=["at_id_link", "gtin", "digits", "gid", "sku"])
def test_every_repaired_spelling_of_a_list_re_reads(spell):
    # A theme block (@id + sku) and an app block (gtin + url): the read keeps one entry per size,
    # keyed by its digits, carrying its sku and barcode. A list repaired to ANY of these spellings
    # must keep its re-reads.
    html = _html(_product([_offer(85, IN, sku="SKU-50", **_link_id(V50)), _offer(34, IN, sku="SKU-15", **_link_id(V15))]),
                 _product([_offer(85, IN, gtin13=GTIN50, **_link_url(V50)), _offer(34, IN, gtin13=GTIN15, **_link_url(V15))]))
    read = _page(html=html).evidence["variants"]
    assert [(v["variant_id"], v.get("sku"), v.get("barcode")) for v in read] == [(V50, "SKU-50", GTIN50), (V15, "SKU-15", GTIN15)]
    stored = [_stored(spell(V50, GTIN50, "SKU-50"), 80.0), _stored(spell(V15, GTIN15, "SKU-15"), 30.0, "15 mL")]
    out, report = _reconcile(stored, html)
    assert [v["price_amount"] for v in out] == [85.0, 34.0]
    assert report["re_read"] == 2


def test_twin_a_stored_link_spelling_naming_two_read_offers_reads_neither():
    # One stored entry, two spellings: its `@id` names V50 while its `sku` is V15's. One stored
    # entry cannot be two offers.
    html = _html(_product([_offer(85, IN, **_link_url(V50)), _offer(34, IN, sku="SKU-15", **_link_url(V15))]))
    (v,), report = _reconcile([_stored(_link_id(V50)["@id"], 80.0) | {"sku": "SKU-15"}], html)
    assert (v["price_amount"], report["re_read"]) == (80.0, 0)


# ---- 2. a plan naming a variant no remaining offer names


def _orphan_plan_html(plan_variant: str = V50) -> str:
    # A (V15) is sold one-time at 40; B (V50) only as a plan at 60.
    plan = _offer(60, IN, **{"@id": f"/products/{HANDLE}?variant={plan_variant}&selling_plan=7#offer"})
    return _html(_product([_offer(40, IN, **_link_id(V15)), plan]))


@pytest.mark.parametrize("stored_id", [_link_id(V50)["@id"], "B-60"], ids=["at_id_link", "non_native_code"])
def test_a_plan_for_another_variant_never_prices_a_single_stored_variant(monkeypatch, stored_id):
    from tests.test_external_seed_refresh_writes_variants import _refresh, _seed_row

    page = _page(html=_orphan_plan_html())
    assert page.evidence["variant_census"]["orphan_plan_variants"] == [V50]
    row = _seed_row(variants=[_stored(stored_id, 60.0)], price=60.0, availability="in_stock")
    _, persisted = _refresh(monkeypatch, row, page)
    seed = persisted["seed_data"]
    assert [v["price_amount"] for v in seed["variants"]] == [60.0]
    assert seed["snapshot"]["variant_refresh"]["status"] == "not_all_re_read"


def test_twin_a_plan_of_the_remaining_variant_is_no_orphan(monkeypatch):
    # The plan names the one-time offer's own variant: the page still sells one value.
    from tests.test_external_seed_refresh_writes_variants import _refresh, _seed_row

    page = _page(html=_orphan_plan_html(V15))
    assert page.evidence["variant_census"]["orphan_plan_variants"] == []
    row = _seed_row(variants=[_stored("A-40", 38.0)], price=38.0, availability="in_stock")
    _, persisted = _refresh(monkeypatch, row, page)
    assert [v["price_amount"] for v in persisted["seed_data"]["variants"]] == [40.0]


# ---- 3. a shared barcode never hands a missing shade its sibling's read


def test_a_shade_missing_from_the_read_never_takes_its_siblings_read_by_a_shared_barcode():
    coral = {**_offer(26, OUT, name="Coral", gtin13=GTIN50, **_link_url(V15))}
    stored_ruby = _stored(GTIN50, 22.0, "Ruby")
    (v,), report = _reconcile([stored_ruby], _html(_product([coral])))
    assert (v["price_amount"], v["availability"], report["re_read"]) == (22.0, "in_stock", 0)
    # The same barcode under the same title is the same variant.
    (v,), report = _reconcile([_stored(GTIN50, 22.0, "Coral")], _html(_product([coral])))
    assert (v["price_amount"], report["re_read"]) == (26.0, 1)


def test_twin_a_barcode_two_read_offers_carry_bridges_neither():
    # A merchant data error: one barcode on two sizes, same price, different stock.
    offers = [_offer(30, IN, gtin13=GTIN50, **_link_url(V50)), _offer(30, OUT, gtin13=GTIN50, **_link_url(V15))]
    (v,), report = _reconcile([_stored(GTIN50, 28.0)], _html(_product(offers)))
    assert (v["price_amount"], report["re_read"]) == (28.0, 0)


# ---- the fallback still sees a folded entry's sku as a page code


def test_a_folded_entrys_sku_still_vouches_for_the_single_variant_fallback():
    # The jurlique fold keeps the `@id` key and the app node's sku. With no currency on the
    # offers the per-offer price is not taken; the single-variant fallback takes the product's,
    # because the stored sku is a code the page states.
    def no_currency(offer):
        return {k: v for k, v in offer.items() if k != "priceCurrency"}

    html = _html(_product([no_currency(_offer(31, IN, **_link_id(V50)))]),
                 _product([no_currency(_offer(31, IN, sku="114807", **_link_url(V50)))]))
    (read,) = _page(html=html).evidence["variants"]
    assert (read["variant_id"], read["sku"]) == (_link_id(V50)["@id"], "114807")
    (v,), _ = _reconcile([_stored("local-1", 30.0) | {"sku": "114807"}], html, product_amount=31.0)
    assert v["price_amount"] == 31.0


def test_a_plans_only_page_whose_plans_name_no_variant_still_names_no_single_value(monkeypatch):
    # Plans marked by a selling-plan key, with no link: nothing for `orphan_plan_variants` to name,
    # and `plans_only` alone keeps og:price (A's 40) off the stored B.
    from tests.test_external_seed_refresh_writes_variants import _refresh, _seed_row

    offers = [_offer(40, IN, sku="A", selling_plan_id=11), _offer(60, IN, sku="B", selling_plan_id=12)]
    meta = '<meta property="product:price:amount" content="40"><meta property="product:price:currency" content="USD">'
    page = _page(html=_html(_product(offers)).replace("</head>", meta + "</head>"))
    census = page.evidence["variant_census"]
    assert (census["plans_only"], census["orphan_plan_variants"]) == (True, [])
    row = _seed_row(variants=[_stored("B-60", 60.0)], price=60.0, availability="in_stock")
    _, persisted = _refresh(monkeypatch, row, page)
    assert [v["price_amount"] for v in persisted["seed_data"]["variants"]] == [60.0]


def test_a_single_variant_stored_by_its_at_id_link_never_takes_another_variants_price():
    # The page lists only V15; the seed stores V50 under its `@id` spelling. The product-level
    # fallback must see V50 as a different native variant, as it would the bare digits.
    html = _html(_product([_offer(40, IN, **_link_id(V15))]))
    (v,), _ = _reconcile([_stored(_link_id(V50)["@id"], 60.0)], html, product_amount=40.0)
    assert v["price_amount"] == 60.0
    (v,), _ = _reconcile([_stored(V50, 60.0)], html, product_amount=40.0)  # the digits, same answer
    assert v["price_amount"] == 60.0
