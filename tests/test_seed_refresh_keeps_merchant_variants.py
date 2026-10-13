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


UNCOVERING = [{"variant_id": "A", "title": "Fair"}, {"variant_id": "B", "title": "Light"},
              {"variant_id": "C", "title": "Deep"}]


def test_the_fold_stamp_spelled_here_is_the_writers():
    from routes.employee_products import _FOLD_URL_SOURCE
    from services.catalog_enrichment_agent.ingestion import SEED_VARIANT_URL_SOURCE

    assert _FOLD_URL_SOURCE == SEED_VARIANT_URL_SOURCE


async def _folded_seed_variants(monkeypatch) -> List[Dict[str, Any]]:
    """A seed the EXTENDED shade fold wrote, through the real feed and the real _build_seed_inserts."""
    from services.catalog_enrichment_agent import ingestion
    from tests.services.test_retailer_ingest_shade_fold import LINE, _records

    records = await _records(monkeypatch, LINE)
    seed = ingestion.ingest_validated_jsonl(list(records), market="US")["seeds"][0]
    variants = json.loads(seed["seed_data"])["variants"]
    assert sum(v.get("url_source") == ingestion.SEED_VARIANT_URL_SOURCE for v in variants) == 3  # the premise
    return variants


async def test_a_folded_seed_is_protected_by_its_url_stamp_alone(monkeypatch):
    folded = await _folded_seed_variants(monkeypatch)
    # The url_source arm on its own: the same variants without the drain's provenance stamp.
    stamped_only = [{k: val for k, val in v.items() if k != "variant_id_provenance"} for v in folded]
    assert not _incoming_keeps_protected_variants(existing=stamped_only, incoming=UNCOVERING)
    assert not _should_overwrite_seed_variants(existing=stamped_only, incoming=UNCOVERING, product_title="Gel Colour")
    # A read naming every id may replace, and each folded shade keeps the listing it is sold on.
    covering = [{"variant_id": v["variant_id"], "title": v.get("title"), "price_amount": 16.0} for v in stamped_only]
    adopted = _adopt_seed_variants(existing=stamped_only, incoming=covering)
    assert [(v.get("url"), v.get("url_source")) for v in adopted] == [
        (v.get("url"), v.get("url_source")) for v in stamped_only]
    assert {v["price_amount"] for v in adopted} == {16.0}


def test_provenance_alone_protects():
    stored = [{"variant_id": "EX-FAIR", "title": "Fair", "variant_id_provenance": "merchant_issued"}]
    assert not _incoming_keeps_protected_variants(existing=stored, incoming=UNCOVERING)


@pytest.mark.parametrize("stored", [
    {"variant_id": V50, "title": "Fair"},                        # Shopify-shaped digits, no stamp
    {"variant_id": "0012345678905", "title": "Fair"},            # a GTIN the extractor read as sku/gtin13
    {"variant_id": "EX-FAIR", "title": "Fair",                   # a bare url: other writers store ?variant= links
     "url": f"https://brand.example/products/{HANDLE}?variant={V50}"},
    {"variant_id": "EX-FAIR", "title": "Fair",                   # another writer's url_source
     "url": f"https://brand.example/products/{HANDLE}?variant={V50}", "url_source": "employee_products"},
    {"variant_id": "EX-FAIR", "title": "Fair", "variant_id_provenance": "product_derived"},
])
def test_without_a_writers_stamp_nothing_is_protected(stored):
    # A page read stores digits too, and other writers store urls; the refresh must stay free to
    # correct its own earlier read.
    assert _incoming_keeps_protected_variants(existing=[stored], incoming=UNCOVERING)
    assert _should_overwrite_seed_variants(existing=[stored], incoming=UNCOVERING, product_title="X")


def test_a_stamped_variant_with_no_id_does_not_hold_the_list():
    # No read can ever name it, so it cannot block a replacement (that would freeze the seed and
    # leave it stale every night); an identified stamped sibling still holds the list.
    nameless = {"title": "Fair", "variant_id_provenance": "merchant_issued"}
    assert _incoming_keeps_protected_variants(existing=[nameless], incoming=UNCOVERING)
    named = {"variant_id": V50, "title": "50 ml", "variant_id_provenance": "merchant_issued"}
    assert not _incoming_keeps_protected_variants(existing=[nameless, named], incoming=UNCOVERING)


def test_an_unprotected_list_is_still_judged_by_the_old_rule():
    existing = [{"variant_id": "TEF701", "title": "100 ml"}]
    incoming = [{"variant_id": "TEF501", "title": "10 ml"}, {"variant_id": "TEF701", "title": "100 ml"}]
    assert _incoming_keeps_protected_variants(existing=existing, incoming=incoming)
    assert _should_overwrite_seed_variants(existing=existing, incoming=incoming, product_title="X")


def test_carried_stamps_never_overwrite_what_the_read_says_about_price_or_stock():
    existing = [{"variant_id": V50, "price_amount": 80.0, "availability": "in_stock",
                 "variant_id_provenance": "merchant_issued"}]
    incoming = [{"variant_id": V50, "price_amount": 85.0, "availability": "out_of_stock", "price_currency": "USD"}]
    (adopted,) = _adopt_seed_variants(existing=existing, incoming=incoming)
    assert (adopted["price_amount"], adopted["availability"], adopted["price_currency"]) == (85.0, "out_of_stock", "USD")
    assert adopted["variant_id_provenance"] == "merchant_issued"


def test_the_folds_url_wins_over_a_url_the_read_carries():
    # A read can carry its own variant url (a ?variant= link on the seed's page); the folded shade is
    # sold on its own listing, which only the fold knows.
    fold_url = "https://brand.example/products/example-fair"
    existing = [{"variant_id": V50, "url": fold_url, "url_source": "shade_fold_listing"}]
    read = [{"variant_id": V50, "price_amount": 80.0, "url": f"https://brand.example/products/{HANDLE}?variant={V50}"}]
    (adopted,) = _adopt_seed_variants(existing=existing, incoming=read)
    assert (adopted["url"], adopted["url_source"], adopted["price_amount"]) == (fold_url, "shade_fold_listing", 80.0)


def test_only_the_folds_url_is_carried():
    existing = [{"variant_id": V50, "variant_id_provenance": "merchant_issued",
                 "url": f"https://brand.example/products/{HANDLE}?variant={V50}", "url_source": "employee_products"}]
    (adopted,) = _adopt_seed_variants(existing=existing, incoming=[{"variant_id": V50, "price_amount": 80.0}])
    assert "url" not in adopted and "url_source" not in adopted


# ---- the localized-copy branch, through the real refresh ----------------------------------------
# The JSON-LD extractor emits no per-variant description, so this branch is reached only by a read
# whose variants carry one. The page below is the real extractor's output with descriptions added
# to its variants; the drain's stored variants carry a German description.

GERMAN, ENGLISH = "Ein frischer Duft für jeden Tag.", "A fresh fragrance for every day."


def _covering_page_with_variant_descriptions():
    page = _page([_offer(80, IN, sku=V50, name="1.7 fl. oz. / 50 mL"),
                  _offer(32, IN, sku=V15, name="0.5 fl. oz. / 15 mL")])
    for v in page.evidence["variants"]:
        v["description"] = ENGLISH
    return page


def test_the_localized_copy_branch_is_the_one_replacing_here():
    # The premise: the overwrite rule declines this read (same ids, same titles), the copy rule takes it.
    stored = [dict(v, description=GERMAN) for v in _drain_seed_variants()]
    read = _covering_page_with_variant_descriptions().evidence["variants"]
    assert not _should_overwrite_seed_variants(existing=stored, incoming=read, product_title=TITLE)
    assert _should_replace_seed_variant_content(existing=stored, incoming=read, market="US",
                                                previous_canonical_url=None, refreshed_canonical_url=None)


def test_the_localized_copy_branch_keeps_the_writers_stamps(monkeypatch):
    stored = [dict(v, description=GERMAN) for v in _drain_seed_variants()]
    row = _seed_row(variants=stored, price=32.0, availability="in_stock")
    _, persisted = _refresh(monkeypatch, row, _covering_page_with_variant_descriptions())
    variants = persisted["seed_data"]["variants"]
    assert [(v["variant_id"], v.get("description")) for v in variants] == [(V50, ENGLISH), (V15, ENGLISH)]
    assert {v.get("variant_id_provenance") for v in variants} == {"merchant_issued"}


def test_the_localized_copy_branch_never_drops_an_uncovered_protected_variant():
    stored = [dict(v, description=GERMAN) for v in _drain_seed_variants()]
    partial = [{"variant_id": V50, "title": "1.7 fl. oz. / 50 mL", "description": ENGLISH}]
    assert not _should_replace_seed_variant_content(existing=stored, incoming=partial, market="US",
                                                    previous_canonical_url=None, refreshed_canonical_url=None)


# ---- the employee create path, through the real endpoint ----------------------------------------


def test_re_creating_an_existing_seed_keeps_the_writers_stamps(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import routes.employee_products as ep
    from tests.test_external_seed_refresh_writes_variants import DEST

    existing = _seed_row(variants=_drain_seed_variants(), price=32.0, availability="in_stock")
    page = _page([_offer(80, IN, sku=V50, name=f"{TITLE} - 1.7 fl. oz. / 50 mL"),
                  _offer(32, IN, sku=V15, name=f"{TITLE} - 0.5 fl. oz. / 15 mL"),
                  _offer(120, IN, sku="46955875467415", name=f"{TITLE} - 3.4 fl. oz. / 100 mL")])
    writes: List[Dict[str, Any]] = []

    async def fetch_all(query, values=None):
        return [existing]

    async def execute_seed(query, values):
        writes.append(values)

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=page))
    monkeypatch.setattr(ep.database, "fetch_all", fetch_all)
    monkeypatch.setattr(ep.database, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.pivota.cc/r?token=t"))
    asyncio.run(ep.create_external_seed(ep.CreateExternalSeedRequest(destination_url=DEST, market="US"),
                                        request=SimpleNamespace(), current_user={"employee_id": "emp_test"}))
    (update,) = writes
    seed_data = update["seed_data"] if isinstance(update["seed_data"], dict) else json.loads(update["seed_data"])
    by_id = {v["variant_id"]: v for v in seed_data["variants"]}
    assert set(by_id) == {V50, V15, "46955875467415"}, "the read covered every id, so the new size is added"
    assert by_id[V50]["variant_id_provenance"] == by_id[V15]["variant_id_provenance"] == "merchant_issued"
    assert "variant_id_provenance" not in by_id["46955875467415"]


def test_re_creating_an_existing_seed_never_drops_the_drains_ids(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import routes.employee_products as ep
    from tests.test_external_seed_refresh_writes_variants import DEST

    existing = _seed_row(variants=_drain_seed_variants(), price=32.0, availability="in_stock")
    writes: List[Dict[str, Any]] = []

    async def execute_seed(query, values):
        writes.append(values)

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "resolve_external_offer",
                        AsyncMock(return_value=_page(_page_offers_with_a_product_level_offer())))
    monkeypatch.setattr(ep.database, "fetch_all", AsyncMock(return_value=[existing]))
    monkeypatch.setattr(ep.database, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.pivota.cc/r?token=t"))
    asyncio.run(ep.create_external_seed(ep.CreateExternalSeedRequest(destination_url=DEST, market="US"),
                                        request=SimpleNamespace(), current_user={"employee_id": "emp_test"}))
    (update,) = writes
    seed_data = update["seed_data"] if isinstance(update["seed_data"], dict) else json.loads(update["seed_data"])
    assert [v["variant_id"] for v in seed_data["variants"]] == [V50, V15]
