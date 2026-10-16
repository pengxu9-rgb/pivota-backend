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
    {"priceSpecification": [{"@type": "UnitPriceSpecification", "price": "40.80", "priceCurrency": "USD",
                             "referenceQuantity": {"@type": "QuantitativeValue", "value": 1, "unitCode": "MON"}}]},
])
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
    out = _extract(_product([_offer(31, IN, **_link_id(V50)), _offer(31, IN, gtin13=GTIN50, **_link_url(V50))]))
    (v,) = out["variants"]
    assert (v["variant_id"], v["barcode"]) == (_link_id(V50)["@id"], GTIN50)


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
