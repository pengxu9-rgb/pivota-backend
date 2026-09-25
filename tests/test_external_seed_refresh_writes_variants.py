"""A refresh must move the values the builders SERVE, or refuse to call the row fresh.

THE DEFECT. `_refresh_external_seed_by_id` corrected the `price_amount` / `availability`
columns and stamped `snapshot.extracted_at` (which clears the `stale_snapshot` gate), but left
the stored variants alone. The serving builders price and stock every offer from those
variants, so the gate said healthy, `commerce_verification.price_trusted` was true, and the
offer carried the OLD values. Prod 2026-09-25: eyurs.com Round Lab Birch Juice sunscreen
re-crawled to 17.0 / in_stock, served as v2 offer {"price": "16.0", "in_stock": false}.

EVERY PAGE HERE GOES THROUGH THE REAL EXTRACTOR. The first version of this file handed the
refresh hand-built `evidence.variants`, including four read variants sharing one id -- a shape
`_extract_jsonld_variants` never emits, because it keeps the first offer per id and drops the
rest. The guards it "proved" never saw a duplicate in production, and review of #2340
reproduced a 50ml stored at 32 being rewritten to the 30ml's 20. Pages are now JSON-LD HTML,
parsed by `_extract_from_html`, and turned into evidence by the same `evidence_variant_fields`
`resolve_external_offer` uses; only the network and the DB are stubbed. The serving side is
the REAL readiness gate, builder and agent_v2 serializer.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from services.external_offers_service import (
    _extract_from_html,
    _extract_jsonld_variants_with_census,
    evidence_variant_fields,
)

DEST = "https://eyurs.com/products/round-lab-birch-juice-moisturizing-sunscreen"
TITLE = "Round Lab Birch Juice Moisturizing Sunscreen"
_NOW = datetime.now(timezone.utc)
_ALLOWED = ["eyurs.com"]

IN = "https://schema.org/InStock"
OUT = "https://schema.org/OutOfStock"


class _FakeReq:
    base_url = "https://api.pivota.cc/"


# ----------------------------------------------------------------- pages, as the web serves them


def _offer(price: Any, availability: str = IN, **ids: Any) -> Dict[str, Any]:
    return {"@type": "Offer", "price": str(price), "priceCurrency": "USD", "availability": availability, **ids}


def _product(offers: Any) -> Dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": TITLE,
        "image": "https://eyurs.com/cdn/sunscreen.jpg",
        "description": "A moisturizing daily sunscreen with birch sap.",
        "offers": offers,
    }


def _html(offers: Any) -> str:
    return (
        "<html><head><title>" + TITLE + "</title>"
        '<script type="application/ld+json">' + json.dumps(_product(offers)) + "</script>"
        "</head><body></body></html>"
    )


def _meta_only_html(price: float) -> str:
    """A page with no JSON-LD at all: one `product:price:amount`, no variants, no stock."""
    return (
        "<html><head><title>" + TITLE + "</title>"
        '<meta property="og:title" content="' + TITLE + '">'
        '<meta property="product:price:amount" content="' + str(price) + '">'
        '<meta property="product:price:currency" content="USD">'
        "</head><body></body></html>"
    )


def _page(offers: Any = None, *, html: Optional[str] = None) -> SimpleNamespace:
    """What `resolve_external_offer` hands the refresh for this page (ExternalOfferSnapshot's
    real field set; see test_refresh_clears_stale_snapshot)."""
    extracted = _extract_from_html(DEST, html if html is not None else _html(offers))
    return SimpleNamespace(
        canonical_url=DEST,
        domain="eyurs.com",
        title=extracted.get("title"),
        image_url=extracted.get("image_url"),
        price_amount=extracted.get("price_amount"),
        # resolve_external_offer's own defaulting, reproduced: USD for a US market.
        price_currency=(extracted.get("price_currency") or "USD"),
        availability=extracted.get("availability") or "unknown",
        last_checked_at=_NOW,
        evidence={"provider": extracted.get("evidence_provider"), **evidence_variant_fields(extracted)},
    )


# The review repro: the seed is curated to the 50ml; the page sells 30ml at 20 and 50ml at 32.
AGGREGATE = {"@type": "AggregateOffer", "lowPrice": "20.00", "highPrice": "32.00", "priceCurrency": "USD", "offerCount": 2, "availability": IN}
# misshaus: every offer carries the product `@id`, so the extractor keeps ONE and drops the rest.
MISSHAUS_ID = "https://misshaus.com/products/missha-m-perfect-cover-bb-cream"


def _same_id_offers(prices: List[float]) -> List[Dict[str, Any]]:
    return [_offer(p, **{"@id": MISSHAUS_ID}) for p in prices]


# ----------------------------------------------------------------- the seed, and the harness


def _variant(vid: str, amount: float, availability: str, title: str = "50ml", **extra: Any) -> Dict[str, Any]:
    return {
        "variant_id": vid,
        "title": title,
        "price_amount": amount,
        "price_currency": "USD",
        "availability": availability,
        **extra,
    }


def _seed_row(
    *,
    variants: Optional[List[Dict[str, Any]]],
    price: float,
    availability: str,
    extracted_days_ago: int = 1,
    snapshot_variants: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """A row the real gate calls healthy: fresh content stamp, live destination."""
    seed_data: Dict[str, Any] = {
        "title": TITLE,
        "brand": "Round Lab",
        "description": "A moisturizing daily sunscreen with birch sap.",
        "image_url": "https://eyurs.com/cdn/sunscreen.jpg",
        "availability": availability,
        "in_stock": availability == "in_stock",
        "snapshot": {
            "extracted_at": (_NOW - timedelta(days=extracted_days_ago)).isoformat(),
            "canonical_url": DEST,
            "title": TITLE,
            **({"variants": snapshot_variants} if snapshot_variants is not None else {}),
        },
    }
    if variants is not None:
        seed_data["variants"] = variants
    return {
        "id": "seed:catalog_enrichment_agent_v1:fd32cd23af083a86",
        "external_product_id": "eyurs:fd32cd23af083a86",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": DEST,
        "canonical_url": DEST,
        "domain": "eyurs.com",
        "title": TITLE,
        "image_url": "https://eyurs.com/cdn/sunscreen.jpg",
        "price_amount": price,
        "price_currency": "USD",
        "availability": availability,
        "seed_data": seed_data,
        "status": "active",
        "attached_product_key": None,
        "attached_variant_id": None,
        "seller_ref": None,
        "seed_kind": None,
        "destination_checked_at": _NOW,
        "destination_http_status": 200,
        "destination_verdict": "live",
        "destination_failure_streak": 0,
        "last_crawled_at": None,
        "last_crawl_attempt_at": None,
        "created_at": _NOW - timedelta(days=30),
        "updated_at": _NOW,
    }


def _refresh(monkeypatch: pytest.MonkeyPatch, row: Dict[str, Any], page, *, reached: bool = True):
    """Drive the real refresh; return (result, the persisted row)."""
    import routes.employee_products as mod

    stored = copy.deepcopy(row)

    async def fake_fetch_one(_q, values=None):
        return stored if values and values.get("id") == stored["id"] else None

    async def fake_exec(_query: str, values):
        # Round-trip through JSON: what a real write persists, and no aliasing of the dicts
        # the function keeps using after the statement.
        persisted = json.loads(json.dumps(values, default=str))
        if isinstance(persisted.get("seed_data"), str):
            persisted["seed_data"] = json.loads(persisted["seed_data"])
        stored.update({k: v for k, v in persisted.items() if k not in ("id", "read_the_served_product")})

    async def resolve(*, observed=None, **_kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = DEST
            if not reached:
                observed["from_cache"] = True
        return page

    async def record(seed_id, observation, *, now=None):
        return {"seed_id": seed_id, "verdict": observation.verdict, "failure_streak": 0}

    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", fake_exec)
    monkeypatch.setattr(mod, "resolve_external_offer", resolve)
    monkeypatch.setattr(mod.destination_liveness, "record_destination_observation", record)
    monkeypatch.setattr(mod, "_project_refreshed_seed_to_serving_surfaces", AsyncMock(return_value={}))
    result = asyncio.run(mod._refresh_external_seed_by_id(stored["id"], max_wait=0))
    return result, stored


def _serve(row: Dict[str, Any]) -> Dict[str, Any]:
    """The deployed serving path: REAL gate -> builder -> agent_v2 serializer."""
    from routes import agent_api
    from routes.agent_v2 import _canonicalize_search_product

    product = asyncio.run(
        agent_api._build_external_seed_product(
            req=_FakeReq(), seed_row=copy.deepcopy(row), allowed_domains=_ALLOWED
        )
    )
    assert product is not None, "the gate must keep this row in recall either way"
    return {"product": product, "v2": _canonicalize_search_product(copy.deepcopy(product))}


def _offers(served: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"price": o["price"], "in_stock": (o.get("availability") or {}).get("in_stock")}
        for o in served["v2"]["offers"]
    ]


def _trusted(served: Dict[str, Any]) -> bool:
    return served["product"]["commerce_verification"]["price_trusted"] is True


# ----------------------------------------------------------------- what the extractor emits


def test_the_extractor_marks_an_aggregate_offer_as_a_range_not_a_price():
    variants, census = _extract_jsonld_variants_with_census([_product(AGGREGATE)])
    assert len(variants) == 1
    assert variants[0]["price_amount"] == 20.0, "the extractor still reports lowPrice"
    assert variants[0]["price_exact"] is False and variants[0]["offer_aggregate"] is True
    assert census["aggregate"] is True and census["exact_prices"] == []
    assert _extract_from_html(DEST, _html(AGGREGATE))["variant_census"]["product_price_exact"] is False


def test_the_extractor_reports_the_offers_its_de_dupe_dropped():
    variants, census = _extract_jsonld_variants_with_census([_product(_same_id_offers([21.0, 22.0, 23.0, 24.0]))])
    assert [v["price_amount"] for v in variants] == [21.0], "one kept, three dropped"
    assert variants[0]["id_collided"] is True
    assert census["offers"] == 4 and census["duplicate_ids"] == 1
    assert census["exact_prices"] == [21.0, 22.0, 23.0, 24.0]


def test_a_product_visited_twice_is_not_a_collision():
    """`_iter_jsonld_nodes` yields each Offer again after its Product; same object, no clash."""
    variants, census = _extract_jsonld_variants_with_census([_product([_offer(20, sku="A"), _offer(32, sku="B")])])
    assert [(v["variant_id"], v.get("id_collided")) for v in variants] == [("A", None), ("B", None)]
    assert census["offers"] == 2 and census["duplicate_ids"] == 0


# ----------------------------------------------------------------- the prod case, end to end


@pytest.mark.parametrize(
    "page_ids",
    [
        {"sku": "RL-BIRCH-SUN-50"},  # the page's SKU, not the stored Shopify id (the usual case)
        {"sku": "46536716517563"},  # the ids happen to agree
    ],
)
def test_a_single_variant_refresh_moves_the_served_v2_offer(monkeypatch, page_ids):
    row = _seed_row(variants=[_variant("46536716517563", 16.0, "out_of_stock")], price=16.0, availability="out_of_stock")
    assert _offers(_serve(row)) == [{"price": "16.0", "in_stock": False}]

    result, stored = _refresh(monkeypatch, row, _page([_offer(17, IN, **page_ids)]))

    assert result["price_refresh"]["status"] == "applied"
    assert result["variant_refresh"]["status"] == "all_re_read"
    after = _serve(stored)
    verification = after["product"]["commerce_verification"]
    assert verification["price_trusted"] is True and verification["availability_trusted"] is True
    # The defect: this read {"price": "16.0", "in_stock": False} with price_trusted true.
    assert _offers(after) == [{"price": "17.0", "in_stock": True}]
    assert stored["seed_data"]["availability"] == "in_stock"
    assert stored["seed_data"]["in_stock"] is True
    assert stored["seed_data"]["snapshot"].get("extracted_at")


def test_every_replaced_value_is_recorded_so_the_overwrite_can_be_undone(monkeypatch):
    original = _variant("46536716517563", 16.0, "out_of_stock", price=16.0, in_stock=False)
    row = _seed_row(variants=[original], price=16.0, availability="out_of_stock")
    result, stored = _refresh(monkeypatch, row, _page([_offer(17, IN, sku="RL")]))

    (entry,) = result["variant_refresh"]["replaced"]
    assert entry["variant_id"] == "46536716517563"
    assert entry["before"] == {"price_amount": 16.0, "price": 16.0, "availability": "out_of_stock", "in_stock": False}
    assert entry["after"] == {"price_amount": 17.0, "price": 17.0, "availability": "in_stock", "in_stock": True}
    # Persisted beside the row, and applying `before` restores the variant exactly.
    assert stored["seed_data"]["snapshot"]["variant_refresh"]["replaced"] == [entry]
    assert {**stored["seed_data"]["variants"][0], **entry["before"]} == original


# ----------------------------------------------------------------- the review repro, and its kin


def test_an_aggregate_offer_never_rewrites_a_single_variant(monkeypatch):
    """#2340 review: a 50ml stored at 32 was rewritten to the AggregateOffer's lowPrice 20."""
    row = _seed_row(variants=[_variant("50ml-shopify-id", 32.0, "out_of_stock", "50ml")], price=32.0, availability="out_of_stock")
    result, stored = _refresh(monkeypatch, row, _page(AGGREGATE))

    variant = stored["seed_data"]["variants"][0]
    assert (variant["price_amount"], variant["availability"]) == (32.0, "out_of_stock")
    assert result["variant_refresh"]["not_re_read"] == ["50ml-shopify-id"]
    assert result["variant_refresh"]["replaced"] == []
    assert "extracted_at" not in stored["seed_data"]["snapshot"]
    assert not _trusted(_serve(stored))


def test_an_aggregate_offer_never_rewrites_a_variant_it_happens_to_match(monkeypatch):
    """An AggregateOffer carrying the seed's own sku is still a range, not that variant's price."""
    row = _seed_row(variants=[_variant("SKU-50", 32.0, "out_of_stock")], price=32.0, availability="out_of_stock")
    result, stored = _refresh(monkeypatch, row, _page({**AGGREGATE, "sku": "SKU-50"}))

    assert stored["seed_data"]["variants"][0]["price_amount"] == 32.0
    assert stored["seed_data"]["variants"][0]["availability"] == "out_of_stock"
    assert result["variant_refresh"]["not_re_read"] == ["SKU-50"]


def test_a_matched_offer_that_states_only_a_range_does_not_price_its_variant(monkeypatch):
    """A plain Offer (not an AggregateOffer) carrying `lowPrice`/`highPrice` and no `price`."""
    ranged = {"@type": "Offer", "lowPrice": "20", "highPrice": "32", "priceCurrency": "USD", "availability": OUT, "sku": "SKU-50"}
    row = _seed_row(variants=[_variant("A", 25.0, "in_stock", sku="SKU-50"), _variant("B", 25.0, "in_stock")], price=25.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page([ranged, _offer(25, IN, sku="B")]))

    first = stored["seed_data"]["variants"][0]
    assert first["price_amount"] == 25.0, "a range bound is not this variant's price"
    assert first["availability"] == "out_of_stock", "its own stock is still per-variant"
    assert result["variant_refresh"]["not_re_read"] == ["A"]


def test_one_range_offer_among_exact_ones_withholds_the_single_variant_fallback(monkeypatch):
    """The exact prices agree with the product price, but one offer only states a range."""
    ranged = {"@type": "Offer", "lowPrice": "20", "highPrice": "40", "priceCurrency": "USD", "availability": IN, "sku": "b"}
    row = _seed_row(variants=[_variant("c", 30.0, "in_stock")], price=30.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page([_offer(32, IN, sku="a"), ranged]))

    assert stored["seed_data"]["variants"][0]["price_amount"] == 30.0
    assert result["variant_refresh"]["not_re_read"] == ["c"]


def test_an_aggregate_wrapping_one_offer_does_not_price_the_product_at_its_low_bound(monkeypatch):
    """The product-level amount is the wrapper's lowPrice 15; the one real offer says 20."""
    wrapper = {"@type": "AggregateOffer", "lowPrice": "15", "highPrice": "20", "priceCurrency": "USD", "offers": [_offer(20, IN, sku="a")]}
    row = _seed_row(variants=[_variant("c", 18.0, "in_stock")], price=18.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(wrapper))

    assert stored["seed_data"]["variants"][0]["price_amount"] == 18.0
    assert result["variant_refresh"]["not_re_read"] == ["c"]


def test_offers_past_the_extractors_cap_leave_the_agreement_unproven(monkeypatch):
    """MAX_VARIANTS offers agree on 17; the one after the cap, never read, says 99."""
    from services.external_offers_service import MAX_VARIANTS

    offers = [_offer(17, IN, sku=f"s{i}") for i in range(MAX_VARIANTS)] + [_offer(99, IN, sku="last")]
    row = _seed_row(variants=[_variant("c", 16.0, "in_stock")], price=16.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(offers))

    assert stored["seed_data"]["variants"][0]["price_amount"] == 16.0
    assert result["variant_refresh"]["not_re_read"] == ["c"]


def test_a_budget_spent_on_revisits_still_counts_as_truncated(monkeypatch):
    """The extractor visits each Offer twice (via its Product, then as a node), so 26 offers
    spend the 50-variant budget before a SECOND Product on the page is reached."""
    first = _product([_offer(17, IN, sku=f"s{i}") for i in range(26)])
    second = {**_product([_offer(99, IN, sku="other")]), "name": "Travel size"}
    html = (
        "<html><head><title>" + TITLE + "</title>"
        '<script type="application/ld+json">' + json.dumps([first, second]) + "</script>"
        "</head><body></body></html>"
    )
    row = _seed_row(variants=[_variant("c", 16.0, "in_stock")], price=16.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(html=html))

    assert stored["seed_data"]["variants"][0]["price_amount"] == 16.0
    assert result["variant_refresh"]["not_re_read"] == ["c"]


def test_a_data_attribute_payload_the_de_dupe_collapsed_proves_nothing(monkeypatch):
    """The other variant source de-dupes too: two skus under one id at 17 and 25 become one."""
    payload = json.dumps([{"id": "s1", "price": 17, "size": "30ml"}, {"id": "s1", "price": 25, "size": "50ml"}])
    html = _meta_only_html(17.0).replace(
        "<body></body>", "<body><div data-product-skus-value='" + payload + "'></div></body>"
    )
    row = _seed_row(variants=[_variant("c", 16.0, "in_stock")], price=16.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(html=html))

    assert stored["seed_data"]["variants"][0]["price_amount"] == 16.0
    assert result["variant_refresh"]["not_re_read"] == ["c"]


def test_adopted_page_variants_never_carry_the_read_marks(monkeypatch):
    """A seed with no variants adopts the page's, even on a cached fallback the reconcile skips."""
    from services.external_offers_service import READ_PROVENANCE_KEYS

    row = _seed_row(variants=None, price=20.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page([_offer(20, IN, sku="a"), _offer(32, IN, sku="b")]), reached=False)

    adopted = stored["seed_data"]["variants"]
    assert [v["variant_id"] for v in adopted] == ["a", "b"]
    assert not any(k in v for v in adopted for k in READ_PROVENANCE_KEYS)


def test_a_single_variant_is_not_restated_at_a_sibling_sizes_price(monkeypatch):
    """Distinct offers under SKUs the seed does not store; the product price is the 30ml's."""
    row = _seed_row(variants=[_variant("50ml-shopify-id", 32.0, "out_of_stock", "50ml")], price=32.0, availability="out_of_stock")
    result, stored = _refresh(monkeypatch, row, _page([_offer(20, IN, sku="SKU-30"), _offer(32, OUT, sku="SKU-50")]))

    variant = stored["seed_data"]["variants"][0]
    # Neither the 30ml's price nor the product's "in stock" (true of the 30ml) is this variant's.
    assert (variant["price_amount"], variant["availability"]) == (32.0, "out_of_stock")
    assert result["variant_refresh"]["not_re_read"] == ["50ml-shopify-id"]
    assert not _trusted(_serve(stored))


def test_offers_the_de_dupe_collapsed_do_not_vouch_for_a_single_variant(monkeypatch):
    """misshaus: four offers under one `@id` at four prices arrive as ONE read variant at 21."""
    row = _seed_row(variants=[_variant("47541190164667", 25.0, "in_stock", "BE02")], price=25.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(_same_id_offers([21.0, 22.0, 23.0, 24.0])))

    assert stored["seed_data"]["variants"][0]["price_amount"] == 25.0
    assert result["variant_refresh"]["not_re_read"] == ["47541190164667"]
    assert not _trusted(_serve(stored))


def test_a_collided_id_is_not_a_match_even_for_the_variant_that_carries_it(monkeypatch):
    row = _seed_row(variants=[_variant(MISSHAUS_ID, 25.0, "in_stock"), _variant("2", 25.0, "in_stock")], price=25.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(_same_id_offers([21.0, 22.0])))

    assert [v["price_amount"] for v in stored["seed_data"]["variants"]] == [25.0, 25.0]
    assert result["variant_refresh"]["not_re_read_count"] == 2


def test_collapsed_offers_that_all_agree_do_name_the_single_variants_price(monkeypatch):
    """misshaus "Default Title": the page repeats one offer; every copy says 19.5 (a sale)."""
    row = _seed_row(variants=[_variant("43201220313275", 25.0, "in_stock", "Default Title")], price=25.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(_same_id_offers([19.5, 19.5])))

    assert result["variant_refresh"]["status"] == "all_re_read"
    assert _offers(_serve(stored)) == [{"price": "19.5", "in_stock": True}]


def test_a_single_variant_takes_a_price_every_distinct_offer_agrees_on(monkeypatch):
    row = _seed_row(variants=[_variant("1", 16.0, "out_of_stock")], price=16.0, availability="out_of_stock")
    result, stored = _refresh(monkeypatch, row, _page([_offer(17, IN, sku="a"), _offer(17, IN, sku="b")]))

    assert result["variant_refresh"]["status"] == "all_re_read"
    assert _offers(_serve(stored)) == [{"price": "17.0", "in_stock": True}]


def test_a_page_that_says_unknown_does_not_flip_a_single_variants_stock(monkeypatch):
    row = _seed_row(variants=[_variant("1", 16.0, "out_of_stock")], price=16.0, availability="out_of_stock")
    no_stock = {"@type": "Offer", "price": "17", "priceCurrency": "USD", "sku": "x"}
    _, stored = _refresh(monkeypatch, row, _page([no_stock]))

    variant = stored["seed_data"]["variants"][0]
    assert variant["price_amount"] == 17.0
    assert variant["availability"] == "out_of_stock", "no availability on the page is no observation"
    assert _offers(_serve(stored)) == [{"price": "17.0", "in_stock": False}]


# ----------------------------------------------------------------- siblings nobody re-read


def _shades(price: float) -> List[Dict[str, Any]]:
    return [
        _variant("46202081018098", price, "in_stock", "1WN", sku="KY-1WN"),
        _variant("46202080624882", price, "in_stock", "1N", sku="KY-1N"),
        _variant("46202080493810", price, "in_stock", "2C", sku="KY-2C"),
    ]


def test_a_multi_variant_refresh_that_read_one_price_does_not_vouch_for_the_siblings(monkeypatch):
    """kyliecosmetics.com shape: three stored shades at 34, the page lists one offer at 35."""
    row = _seed_row(variants=_shades(34.0), price=34.0, availability="in_stock")
    assert _trusted(_serve(row))

    result, stored = _refresh(monkeypatch, row, _page(_offer(35, IN)))

    assert result["variant_refresh"]["not_re_read_count"] == 3
    # No inference: the page did not say what 1N or 2C cost.
    assert [v["price_amount"] for v in stored["seed_data"]["variants"]] == [34.0, 34.0, 34.0]
    # The stamp an earlier refresh left is removed; it made the same unearned claim.
    assert "extracted_at" not in stored["seed_data"]["snapshot"]
    after = _serve(stored)
    verification = after["product"]["commerce_verification"]
    assert verification["required"] is True and verification["price_trusted"] is False
    assert "stale_snapshot" in verification["reasons"]
    assert all(o["price"] is None for o in after["v2"]["offers"]), "no sibling's stale price served"


def test_a_page_keyed_by_sku_re_reads_variants_stored_under_a_shopify_id(monkeypatch):
    """perfumania / bluemercury shape: stored `variant_id` is the Shopify id, the offer the SKU.

    The enrichment lane stores `price` beside `price_amount` and `currency` beside
    `price_currency`, and `external_seed_audit` falls back to the twins -- they move together.
    """
    shades = _shades(34.0)
    shades[0].update(price=34.0, currency="USD", in_stock=True)
    shades[1].update(stock="In Stock")
    shades[2]["availability"] = "out_of_stock"  # the page will say nothing about it
    row = _seed_row(variants=shades, price=34.0, availability="in_stock")
    page = _page(
        [
            _offer(35, IN, sku="KY-1WN"),
            _offer(36, OUT, sku="KY-1N"),
            {"@type": "Offer", "price": "37", "priceCurrency": "USD", "sku": "KY-2C"},
        ]
    )
    result, stored = _refresh(monkeypatch, row, page)

    assert result["variant_refresh"]["status"] == "all_re_read"
    first, second, _third = stored["seed_data"]["variants"]
    assert (first["price_amount"], first["price"], first["currency"]) == (35.0, 35.0, "USD")
    assert (second["availability"], second["stock"]) == ("out_of_stock", "out_of_stock")
    assert "price_exact" not in first and "id_collided" not in first, "read marks stay off the seed"
    served = _serve(stored)
    assert _trusted(served)
    assert _offers(served) == [
        {"price": "35.0", "in_stock": True},
        {"price": "36.0", "in_stock": False},
        # Price re-read; no stock on the page, so the stored sold-out stands.
        {"price": "37.0", "in_stock": False},
    ]


def test_one_unmatched_sibling_is_enough_to_withhold_trust(monkeypatch):
    row = _seed_row(variants=_shades(34.0), price=34.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page([_offer(35, IN, sku="KY-1WN"), _offer(35, IN, sku="KY-1N")]))

    assert result["variant_refresh"]["not_re_read"] == ["46202080493810"]
    # The two it did read are still corrected; only the claim of freshness is withheld.
    assert [v["price_amount"] for v in stored["seed_data"]["variants"]] == [35.0, 35.0, 34.0]
    assert not _trusted(_serve(stored))


def test_a_positional_offer_id_is_not_an_identifier(monkeypatch):
    """Review of #2340: the extractor names id-less offers `offer_1..N`, and seeds adopted from
    sku-less pages STORE those names. Today's page lists one id-less Offer at 32; the stored
    `offer_1` is the 30ml at 20, sold out. Position 1 then is not position 1 now."""
    sizes = [
        _variant("offer_1", 20.0, "out_of_stock", "30ml"),
        _variant("offer_2", 32.0, "in_stock", "50ml"),
        _variant("offer_3", 45.0, "in_stock", "100ml"),
    ]
    row = _seed_row(variants=sizes, price=20.0, availability="out_of_stock")
    page = _page([{"@type": "Offer", "price": "32", "priceCurrency": "USD", "availability": IN}])
    assert page.evidence["variants"][0]["variant_id"] == "offer_1", "the real extractor's invented id"

    result, stored = _refresh(monkeypatch, row, page)

    assert [(v["price_amount"], v["availability"]) for v in stored["seed_data"]["variants"]] == [
        (20.0, "out_of_stock"), (32.0, "in_stock"), (45.0, "in_stock"),
    ]
    assert result["variant_refresh"]["replaced"] == []
    assert result["variant_refresh"]["not_re_read"] == ["offer_1", "offer_2", "offer_3"]
    assert not _trusted(_serve(stored))


def test_the_last_write_survives_a_night_that_changes_nothing(monkeypatch):
    row = _seed_row(variants=[_variant("1", 16.0, "out_of_stock")], price=16.0, availability="out_of_stock")
    first, stored = _refresh(monkeypatch, row, _page([_offer(17, IN, sku="RL")]))
    written = stored["seed_data"]["snapshot"]["variant_refresh_last_write"]
    assert written["replaced"] == first["variant_refresh"]["replaced"] != []

    second, stored_again = _refresh(monkeypatch, stored, _page([_offer(17, IN, sku="RL")]))

    assert second["variant_refresh"]["replaced"] == []
    assert stored_again["seed_data"]["snapshot"]["variant_refresh_last_write"] == written


def test_two_stored_variants_claiming_one_page_offer_are_both_unread(monkeypatch):
    """A SKU the merchant reused across shades names neither of them."""
    row = _seed_row(
        variants=[_variant("111", 20.0, "in_stock", "Light", sku="SHARED"), _variant("222", 20.0, "in_stock", "Dark", sku="SHARED")],
        price=20.0,
        availability="in_stock",
    )
    result, stored = _refresh(monkeypatch, row, _page([_offer(25, IN, sku="SHARED")]))

    assert result["variant_refresh"]["not_re_read"] == ["111", "222"]
    assert [v["price_amount"] for v in stored["seed_data"]["variants"]] == [20.0, 20.0]


def test_an_offer_that_states_no_currency_does_not_price_a_variant(monkeypatch):
    """No `priceCurrency` on the offer: "3600" is not $3,600 because the column says USD."""
    bare = {"@type": "Offer", "price": "3600", "availability": IN, "sku": "EP026"}
    row = _seed_row(variants=[_variant("A", 20.0, "in_stock"), _variant("43062652469401", 22.0, "out_of_stock", sku="EP026")], price=20.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page([_offer(20, IN, sku="A"), bare]))

    second = stored["seed_data"]["variants"][1]
    assert second["price_amount"] == 22.0
    assert second["availability"] == "in_stock", "the stock it stated is still its own"
    assert result["variant_refresh"]["not_re_read"] == ["43062652469401"]


def test_a_variant_read_in_another_currency_is_not_a_re_read(monkeypatch):
    row = _seed_row(variants=[_variant("A", 20.0, "in_stock"), _variant("B", 25.0, "in_stock")], price=20.0, availability="in_stock")
    krw = {"@type": "Offer", "price": "36000", "priceCurrency": "KRW", "availability": IN, "sku": "B"}
    result, stored = _refresh(monkeypatch, row, _page([_offer(20, IN, sku="A"), krw]))

    assert stored["seed_data"]["variants"][1]["price_amount"] == 25.0
    assert result["variant_refresh"]["not_re_read"] == ["B"]


# ----------------------------------------------------------------- where the served list lives


# A page WITH JSON-LD variants is adopted as the seed's top-level list when it has none (the
# refresh's pre-existing adoption rule), and then that list is what is served and reconciled.
# The snapshot fallback matters for a page that lists no variants: a meta-tag price only.


def test_variants_stored_only_on_the_snapshot_are_the_ones_reconciled(monkeypatch):
    """agent_api serves `snapshot.variants` when the seed has no top-level list."""
    row = _seed_row(variants=None, snapshot_variants=[_variant("1", 16.0, "in_stock")], price=16.0, availability="in_stock")
    assert _offers(_serve(row)) == [{"price": "16.0", "in_stock": True}]

    result, stored = _refresh(monkeypatch, row, _page(html=_meta_only_html(17.0)))

    assert result["variant_refresh"]["served_from"] == "snapshot"
    assert result["variant_refresh"]["status"] == "all_re_read"
    assert "variants" not in stored["seed_data"], "the served list stays where the builder reads it"
    assert _offers(_serve(stored)) == [{"price": "17.0", "in_stock": True}]


def test_unread_snapshot_siblings_withhold_trust_like_top_level_ones(monkeypatch):
    row = _seed_row(variants=None, snapshot_variants=_shades(34.0), price=34.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(html=_meta_only_html(35.0)))

    assert result["variant_refresh"]["not_re_read_count"] == 3
    assert not _trusted(_serve(stored))


# ----------------------------------------------------------------- what must stay untouched


def test_a_refused_page_price_is_not_a_re_read_of_the_single_variant(monkeypatch):
    """`price: 0` is a broken-offer shape; the extractor reports no price and the column keeps
    the old one. The variant must not be vouched for either."""
    row = _seed_row(variants=[_variant("1", 16.0, "in_stock")], price=16.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page([_offer(0, IN, sku="x")]))

    assert result["price_refresh"]["status"] == "unavailable"
    assert result["variant_refresh"]["not_re_read"] == ["1"]
    assert stored["seed_data"]["variants"][0]["price_amount"] == 16.0


def test_a_cached_fallback_touches_no_variant(monkeypatch):
    """No origin reading, no write: the cached page is not evidence about the product."""
    row = _seed_row(variants=[_variant("1", 16.0, "out_of_stock")], price=16.0, availability="out_of_stock")
    result, stored = _refresh(monkeypatch, row, _page([_offer(17, IN, sku="1")]), reached=False)

    assert result["variant_refresh"] == {"status": "not_read"}
    assert stored["seed_data"]["variants"][0]["price_amount"] == 16.0
    assert stored["seed_data"]["variants"][0]["availability"] == "out_of_stock"


def test_evidence_without_a_census_writes_nothing(monkeypatch):
    """Evidence from an extractor that did not record what its de-dupe dropped proves nothing."""
    row = _seed_row(variants=[_variant("1", 16.0, "out_of_stock")], price=16.0, availability="out_of_stock")
    page = _page([_offer(17, IN, sku="1")])
    page.evidence.pop("variant_census")
    result, stored = _refresh(monkeypatch, row, page)

    assert result["variant_refresh"]["census"] == "absent"
    assert stored["seed_data"]["variants"][0]["price_amount"] == 16.0
    assert not _trusted(_serve(stored))


def test_variants_that_carry_no_fact_do_not_block_the_stamp(monkeypatch):
    """A bare variant (no price, no stock) inherits the product values; nothing to re-read."""
    row = _seed_row(
        variants=[{"variant_id": "A", "title": "30ml"}, {"variant_id": "B", "title": "50ml"}],
        price=20.0,
        availability="in_stock",
        extracted_days_ago=400,
    )
    result, stored = _refresh(monkeypatch, row, _page(_offer(22, IN)))

    assert result["variant_refresh"]["status"] == "all_re_read"
    assert stored["seed_data"]["snapshot"].get("extracted_at")
    served = _serve(stored)
    assert _trusted(served)
    assert {o["price"] for o in served["v2"]["offers"]} == {"22.0"}


def test_reconcile_does_not_mutate_its_input():
    from routes.employee_products import _reconcile_seed_variants_with_read

    stored = [_variant("1", 16.0, "out_of_stock")]
    snapshot = copy.deepcopy(stored)
    out, _ = _reconcile_seed_variants_with_read(
        stored, [], product_amount=17.0, product_currency="USD", product_availability="in_stock",
        census={"offers": 0, "data_attr_skus": 0, "product_price_exact": True},
    )
    assert stored == snapshot
    assert out[0]["price_amount"] == 17.0 and out[0]["availability"] == "in_stock"


def test_the_repair_scripts_simulate_mode_reconciles_without_writing(monkeypatch):
    """`--simulate` must reach the same reconcile over a real extraction, and write nothing."""
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "refresh_seeds_with_unverified_variants",
        pathlib.Path(__file__).resolve().parents[1] / "scripts/ops/refresh_seeds_with_unverified_variants.py",
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    row = _seed_row(variants=[_variant("50ml-shopify-id", 32.0, "out_of_stock")], price=32.0, availability="out_of_stock")

    async def fetch_one(_q, values=None):
        return row

    async def refuse_write(*_a, **_k):
        raise AssertionError("simulate must not write")

    async def fetch_html(url, **_k):
        return _html(AGGREGATE), "text/html"

    monkeypatch.setattr(script.database, "fetch_one", fetch_one)
    monkeypatch.setattr(script.database, "execute", refuse_write)
    monkeypatch.setattr("services.external_offers_service._fetch_html", fetch_html)
    report = asyncio.run(script._simulate(row["id"]))

    assert report["not_re_read"] == ["50ml-shopify-id"] and report["replaced"] == []

    async def fetch_html_one_offer(url, **_k):
        return _html([_offer(28, IN, sku="RL")]), "text/html"

    monkeypatch.setattr("services.external_offers_service._fetch_html", fetch_html_one_offer)
    report = asyncio.run(script._simulate(row["id"]))
    assert report["replaced"][0]["after"] == {"price_amount": 28.0, "availability": "in_stock"}
