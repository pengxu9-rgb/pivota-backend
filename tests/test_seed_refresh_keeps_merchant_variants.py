"""The seed refresh never trades a writer's merchant-issued variants for a page read's ids.

THE DEFECT. The retailer-ingest drain stores a seed's variants under the merchant's own Shopify
variant ids (`variant_id_provenance: merchant_issued`), and the listing's SKUs, offers and the
gateway PDP are keyed by them. The nightly refresh re-reads the page's JSON-LD, whose offers name
a variant by the merchant SKU code or by the offer `@id` (`/products/<handle>?variant=<id>#offer`),
and `_should_overwrite_seed_variants` let that list win whenever it had more distinct ids -- which
it does whenever the page also lists a product-level offer. Prod 2026-10-13: size-variant
listings of a drain-verified brand store had their seeds rewritten the night after ingest; the PDP
matched none of the SKUs and served no price, although every SKU and offer was in stock and priced.

BOTH SIDES COME FROM THEIR REAL PRODUCERS: the stored variants from the drain's own ingest
(`curated_brand_feed.shopify_product_to_record` -> `ingest_validated_jsonl`), the page from the
real JSON-LD extractor, driven through the real `_refresh_external_seed_by_id` harness.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from routes.employee_products import (
    _adopt_seed_variants,
    _incoming_keeps_protected_variants,
    _should_overwrite_seed_variants,
    _should_replace_seed_variant_content,
)
from tests.test_external_seed_refresh_writes_variants import IN, TITLE, _offer, _page, _refresh, _seed_row

V50, V15 = "37169662034071", "41430446375063"  # real-shaped Shopify variant ids
HANDLE = "example-eau-de-toilette"


def _drain_seed_variants() -> List[Dict[str, Any]]:
    """The seed variants the retailer-ingest drain writes for a two-size listing."""
    from services import curated_brand_feed as feed
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

    record = feed.shopify_product_to_record(
        {"id": 6644068500001, "vendor": "Example", "title": "Example Eau De Toilette", "handle": HANDLE,
         "product_type": "Fragrance", "body_html": "<p>A fragrance.</p>",
         "images": [{"src": "https://cdn.brand.example/a.jpg"}], "options": [{"name": "Size"}],
         "variants": [
             {"id": int(V50), "price": "80.00", "available": True, "sku": "EX50", "option1": "1.7 fl. oz. / 50 mL"},
             {"id": int(V15), "price": "32.00", "available": True, "sku": "EX15", "option1": "0.5 fl. oz. / 15 mL"},
         ]},
        domain="brand.example", category_path="beauty", brand_override="Example", currency="USD",
        source_role="brand_official", retailer_name=None, emit_native_variants=True)
    seed_data = ingest_validated_jsonl([record])["seeds"][0]["seed_data"]
    seed_data = json.loads(seed_data) if isinstance(seed_data, str) else seed_data
    return seed_data["variants"]


def _page_offers_with_a_product_level_offer() -> List[Dict[str, Any]]:
    """The prod page shape: one offer per size named only by its `@id`, plus a product-level offer
    carrying the merchant SKU code -- one more distinct id than the store has variants."""
    return [
        _offer(80, IN, name=f"{TITLE} - 1.7 fl. oz. / 50 mL", **{"@id": f"/products/{HANDLE}?variant={V50}#offer"}),
        _offer(32, IN, name=f"{TITLE} - 0.5 fl. oz. / 15 mL", **{"@id": f"/products/{HANDLE}?variant={V15}#offer"}),
        _offer(32, IN, sku="EXBASE"),
    ]


def test_the_drain_writes_merchant_issued_variants():
    # The premise: what the guard protects is what the drain really stores.
    stored = _drain_seed_variants()
    assert [v["variant_id"] for v in stored] == [V50, V15]
    assert {v["variant_id_provenance"] for v in stored} == {"merchant_issued"}


def test_the_page_read_names_variants_by_offer_id_and_sku_code():
    # The premise on the other side: the extractor really does produce more, non-merchant ids.
    read = _page(_page_offers_with_a_product_level_offer()).evidence["variants"]
    ids = [v["variant_id"] for v in read]
    assert len(ids) == 3 and V50 not in ids and V15 not in ids


def test_without_the_guard_this_page_overwrites_the_drains_variants(monkeypatch):
    # The premise, through the real refresh: the old rule alone hands this page the seed.
    import routes.employee_products as mod

    monkeypatch.setattr(mod, "_incoming_keeps_protected_variants", lambda **_: True)
    row = _seed_row(variants=_drain_seed_variants(), price=32.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(_page_offers_with_a_product_level_offer()))
    assert V50 not in [v["variant_id"] for v in stored["seed_data"]["variants"]]


def test_a_refresh_keeps_the_drains_variants(monkeypatch):
    stored_variants = _drain_seed_variants()
    row = _seed_row(variants=stored_variants, price=32.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(_page_offers_with_a_product_level_offer()))
    kept = stored["seed_data"]["variants"]
    assert [v["variant_id"] for v in kept] == [V50, V15]
    assert {v.get("variant_id_provenance") for v in kept} == {"merchant_issued"}


def test_a_refresh_still_replaces_page_read_variants_with_a_richer_read(monkeypatch):
    # The twin: a seed whose variants came from an earlier page read keeps today's behaviour.
    row = _seed_row(variants=[{"variant_id": "EXBASE", "title": "Example Eau De Toilette", "price_amount": 32.0,
                               "price_currency": "USD", "availability": "in_stock"}],
                    price=32.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(_page_offers_with_a_product_level_offer()))
    assert len(stored["seed_data"]["variants"]) == 3


def test_a_read_that_names_every_merchant_id_may_replace_and_keeps_their_stamps(monkeypatch):
    # A page whose offers carry the Shopify ids themselves covers the stored variants: a new size
    # may be added, and the stored ids keep their provenance.
    stored_variants = _drain_seed_variants()
    page = _page([_offer(80, IN, sku=V50, name=f"{TITLE} - 1.7 fl. oz. / 50 mL"),
                  _offer(32, IN, sku=V15, name=f"{TITLE} - 0.5 fl. oz. / 15 mL"),
                  _offer(120, IN, sku="46955875467415", name=f"{TITLE} - 3.4 fl. oz. / 100 mL")])
    row = _seed_row(variants=stored_variants, price=32.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, page)
    by_id = {v["variant_id"]: v for v in stored["seed_data"]["variants"]}
    assert set(by_id) == {V50, V15, "46955875467415"}
    assert by_id[V50]["variant_id_provenance"] == by_id[V15]["variant_id_provenance"] == "merchant_issued"
    assert "variant_id_provenance" not in by_id["46955875467415"], "a page read is never merchant_issued"


@pytest.mark.parametrize("stored", [
    # the shade fold: a synthesised id, but its own listing url and source
    {"variant_id": f"{HANDLE}:0", "title": "Fair", "url": "https://brand.example/products/x-fair",
     "url_source": "fold_extended"},
    {"variant_id": f"{HANDLE}:0", "title": "Fair", "own_listing_url": "https://brand.example/products/x-fair"},
    # provenance alone
    {"variant_id": "EX-FAIR", "title": "Fair", "variant_id_provenance": "merchant_issued"},
    # a numeric Shopify id with no stamp at all
    {"variant_id": V50, "title": "Fair"},
])
def test_every_protected_variant_shape_blocks_an_uncovering_overwrite(stored):
    incoming = [{"variant_id": "A", "title": "Fair"}, {"variant_id": "B", "title": "Light"},
                {"variant_id": "C", "title": "Deep"}]
    assert not _incoming_keeps_protected_variants(existing=[stored], incoming=incoming)
    assert not _should_overwrite_seed_variants(existing=[stored], incoming=incoming, product_title="X")


def test_an_unprotected_list_is_still_judged_by_the_old_rule():
    existing = [{"variant_id": "TEF701", "title": "100 ml"}]
    incoming = [{"variant_id": "TEF501", "title": "10 ml"}, {"variant_id": "TEF701", "title": "100 ml"}]
    assert _incoming_keeps_protected_variants(existing=existing, incoming=incoming)
    assert _should_overwrite_seed_variants(existing=existing, incoming=incoming, product_title="X")


def test_the_localized_copy_replacement_does_not_drop_a_protected_variant():
    # The second replace branch: shared ids with a German description replaced by English copy.
    existing = [{"variant_id": V50, "title": "50 mL", "description": "Ein frischer Duft für jeden Tag.",
                 "variant_id_provenance": "merchant_issued"},
                {"variant_id": V15, "title": "15 mL", "description": "Ein frischer Duft für jeden Tag.",
                 "variant_id_provenance": "merchant_issued"}]
    english = "A fresh fragrance for every day."
    partial = [{"variant_id": V50, "title": "50 mL", "description": english}]
    kwargs = dict(market="US", previous_canonical_url=None, refreshed_canonical_url=None)
    assert not _should_replace_seed_variant_content(existing=existing, incoming=partial, **kwargs)
    full = partial + [{"variant_id": V15, "title": "15 mL", "description": english}]
    assert _should_replace_seed_variant_content(existing=existing, incoming=full, **kwargs)
    adopted = _adopt_seed_variants(existing=existing, incoming=full)
    assert [(v["description"], v["variant_id_provenance"]) for v in adopted] == [(english, "merchant_issued")] * 2


def test_carried_stamps_never_overwrite_what_the_read_says_about_price_or_stock():
    existing = [{"variant_id": V50, "price_amount": 80.0, "availability": "in_stock",
                 "variant_id_provenance": "merchant_issued", "url": "https://brand.example/products/x"}]
    incoming = [{"variant_id": V50, "price_amount": 85.0, "availability": "out_of_stock"}]
    (adopted,) = _adopt_seed_variants(existing=existing, incoming=incoming)
    assert (adopted["price_amount"], adopted["availability"]) == (85.0, "out_of_stock")
    assert (adopted["variant_id_provenance"], adopted["url"]) == ("merchant_issued", "https://brand.example/products/x")
