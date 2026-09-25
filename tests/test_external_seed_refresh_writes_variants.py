"""A refresh must move the values the builders SERVE, or refuse to call the row fresh.

THE DEFECT. `_refresh_external_seed_by_id` corrected the `price_amount` / `availability`
columns and stamped `snapshot.extracted_at` (which clears the `stale_snapshot` gate), but left
`seed_data.variants[]` alone. The serving builders price and stock every offer from those
variants (`_seed_primary_price` reads them first; each variant carries its own `price` and
`in_stock`), so the gate said healthy, `commerce_verification.price_trusted` was true, and the
offer carried the OLD values. Prod 2026-09-25: eyurs.com Round Lab Birch Juice sunscreen
re-crawled to 17.0 / in_stock, served as v2 offer {"price": "16.0", "in_stock": false}.

WHAT THE REFRESH READS decides the fix. It parses the page's JSON-LD offers, whose ids are a
SKU, an `@id` path or a positional `offer_N` -- while stored ids are mostly Shopify numeric
variant ids -- and a multi-shade page often lists ONE offer. So:
  * a stored variant with an unambiguous id match takes the page's own price and stock;
  * a seed with ONE stored variant takes the product-level values the refresh re-read;
  * any other stored variant that carries a price or stock was NOT re-read, and the row is
    not fresh: no `extracted_at` stamp, and a stamp left by an earlier refresh is removed, so
    the gate serves the row for recall with `live_quote_required`.

Every test drives the REAL refresh, the REAL readiness gate and the REAL builder + agent_v2
serializer. Only the DB and the network are stubbed.
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

DEST = "https://eyurs.com/products/round-lab-birch-juice-moisturizing-sunscreen"
_NOW = datetime.now(timezone.utc)
_ALLOWED = ["eyurs.com"]


class _FakeReq:
    base_url = "https://api.pivota.cc/"


def _variant(vid: str, price: float, availability: str, title: str = "50ml") -> Dict[str, Any]:
    return {
        "variant_id": vid,
        "title": title,
        "price_amount": price,
        "price_currency": "USD",
        "availability": availability,
    }


def _seed_row(
    *,
    variants: List[Dict[str, Any]],
    price: float,
    availability: str,
    extracted_days_ago: int = 1,
) -> Dict[str, Any]:
    """A row the real gate calls healthy: fresh content stamp, live destination."""
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
        "title": "Round Lab Birch Juice Moisturizing Sunscreen",
        "image_url": "https://eyurs.com/cdn/sunscreen.jpg",
        "price_amount": price,
        "price_currency": "USD",
        "availability": availability,
        "seed_data": {
            "title": "Round Lab Birch Juice Moisturizing Sunscreen",
            "brand": "Round Lab",
            "description": "A moisturizing daily sunscreen with birch sap.",
            "image_url": "https://eyurs.com/cdn/sunscreen.jpg",
            "availability": availability,
            "in_stock": availability == "in_stock",
            "variants": variants,
            "snapshot": {
                "extracted_at": (_NOW - timedelta(days=extracted_days_ago)).isoformat(),
                "canonical_url": DEST,
                "title": "Round Lab Birch Juice Moisturizing Sunscreen",
            },
        },
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


def _page(
    *, price: float, availability: str, variants: Optional[List[Dict[str, Any]]] = None
) -> SimpleNamespace:
    """ExternalOfferSnapshot's real field set (see test_refresh_clears_stale_snapshot)."""
    return SimpleNamespace(
        canonical_url=DEST,
        domain="eyurs.com",
        title="Round Lab Birch Juice Moisturizing Sunscreen",
        image_url="https://eyurs.com/cdn/sunscreen.jpg",
        price_amount=price,
        price_currency="USD",
        availability=availability,
        last_checked_at=_NOW,
        evidence={"provider": "jsonld", "variants": variants or []},
    )


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
        if observed is not None and reached:
            observed["status_code"] = 200
            observed["final_url"] = DEST
        elif observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = DEST
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


# ----------------------------------------------------------------- the prod case, end to end


@pytest.mark.parametrize(
    "page_variant_id",
    [
        # The page's JSON-LD offer carried a SKU, not the stored Shopify id (the usual case).
        "RL-BIRCH-SUN-50",
        # The ids happen to agree.
        "46536716517563",
    ],
)
def test_a_single_variant_refresh_moves_the_served_v2_offer(monkeypatch, page_variant_id):
    row = _seed_row(
        variants=[_variant("46536716517563", 16.0, "out_of_stock")],
        price=16.0,
        availability="out_of_stock",
    )
    before = _serve(row)
    assert _offers(before) == [{"price": "16.0", "in_stock": False}]

    page = _page(
        price=17.0,
        availability="in_stock",
        variants=[{"variant_id": page_variant_id, "price_amount": 17.0, "price_currency": "USD", "availability": "in_stock"}],
    )
    result, stored = _refresh(monkeypatch, row, page)

    assert result["price_refresh"]["status"] == "applied"
    assert result["variant_refresh"]["status"] == "all_re_read"
    after = _serve(stored)
    verification = after["product"]["commerce_verification"]
    assert verification["price_trusted"] is True and verification["availability_trusted"] is True
    # The defect: this read {"price": "16.0", "in_stock": False} with price_trusted true.
    assert _offers(after) == [{"price": "17.0", "in_stock": True}]
    assert after["product"]["price"] == 17.0
    # The top-level copies of the same fact move with it.
    assert stored["seed_data"]["availability"] == "in_stock"
    assert stored["seed_data"]["in_stock"] is True
    assert stored["seed_data"]["snapshot"].get("extracted_at")


def test_a_single_variant_is_not_restated_at_a_sibling_sizes_price(monkeypatch):
    """The seed is curated to the 50ml; the page lists 30ml at 20 and 50ml at 32 under SKUs
    the seed does not store, and its product-level price is the 30ml's."""
    row = _seed_row(variants=[_variant("50ml-shopify-id", 32.0, "out_of_stock", "50ml")], price=32.0, availability="out_of_stock")
    page = _page(
        price=20.0,
        availability="in_stock",
        variants=[
            {"variant_id": "SKU-30", "price_amount": 20.0, "price_currency": "USD", "availability": "in_stock"},
            {"variant_id": "SKU-50", "price_amount": 32.0, "price_currency": "USD", "availability": "out_of_stock"},
        ],
    )
    result, persisted = _refresh(monkeypatch, row, page)

    variant = persisted["seed_data"]["variants"][0]
    # Neither the 30ml's price nor the product's "in stock" (true of the 30ml) is this variant's.
    assert (variant["price_amount"], variant["availability"]) == (32.0, "out_of_stock")
    assert result["variant_refresh"]["not_re_read"] == ["50ml-shopify-id"]
    assert _serve(persisted)["product"]["commerce_verification"]["price_trusted"] is False


def test_a_single_variant_takes_a_price_every_listed_offer_agrees_on(monkeypatch):
    row = _seed_row(variants=[_variant("1", 16.0, "out_of_stock")], price=16.0, availability="out_of_stock")
    page = _page(
        price=17.0,
        availability="in_stock",
        variants=[
            {"variant_id": "a", "price_amount": 17.0, "price_currency": "USD", "availability": "in_stock"},
            {"variant_id": "b", "price_amount": 17.0, "price_currency": "USD", "availability": "in_stock"},
        ],
    )
    result, persisted = _refresh(monkeypatch, row, page)

    assert result["variant_refresh"]["status"] == "all_re_read"
    assert _offers(_serve(persisted)) == [{"price": "17.0", "in_stock": True}]


def test_a_page_that_says_unknown_does_not_flip_a_single_variants_stock(monkeypatch):
    row = _seed_row(variants=[_variant("1", 16.0, "out_of_stock")], price=16.0, availability="out_of_stock")
    page = _page(price=17.0, availability="unknown", variants=[{"variant_id": "x", "price_amount": 17.0, "availability": "unknown"}])
    _, stored = _refresh(monkeypatch, row, page)

    variant = stored["seed_data"]["variants"][0]
    assert variant["price_amount"] == 17.0
    assert variant["availability"] == "out_of_stock", "'unknown' is no observation"
    assert _offers(_serve(stored)) == [{"price": "17.0", "in_stock": False}]


# ----------------------------------------------------------------- siblings nobody re-read


def _shades(price: float) -> List[Dict[str, Any]]:
    return [
        _variant("46202081018098", price, "in_stock", "1WN"),
        _variant("46202080624882", price, "in_stock", "1N"),
        _variant("46202080493810", price, "in_stock", "2C"),
    ]


def test_a_multi_variant_refresh_that_read_one_price_does_not_vouch_for_the_siblings(monkeypatch):
    """kyliecosmetics.com shape: three stored shades at 34, the page lists one offer at 35."""
    row = _seed_row(variants=_shades(34.0), price=34.0, availability="in_stock")
    before = _serve(row)
    assert before["product"]["commerce_verification"]["price_trusted"] is True

    page = _page(price=35.0, availability="in_stock", variants=[{"variant_id": "offer_1", "price_amount": 35.0, "price_currency": "USD", "availability": "in_stock"}])
    result, stored = _refresh(monkeypatch, row, page)

    assert result["variant_refresh"]["status"] == "not_all_re_read"
    assert result["variant_refresh"]["not_re_read_count"] == 3
    # No inference: the page did not say what 1N or 2C cost.
    assert [v["price_amount"] for v in stored["seed_data"]["variants"]] == [34.0, 34.0, 34.0]
    # The stamp an earlier refresh left is removed; it made the same unearned claim.
    assert "extracted_at" not in stored["seed_data"]["snapshot"]

    after = _serve(stored)
    verification = after["product"]["commerce_verification"]
    assert verification["required"] is True
    assert verification["price_trusted"] is False
    assert "stale_snapshot" in verification["reasons"]
    assert all(o["price"] is None for o in after["v2"]["offers"]), "no sibling's stale price served"


def test_a_multi_variant_refresh_that_re_read_every_id_serves_each_offers_own_price(monkeypatch):
    shades = _shades(34.0)
    # Stored sold out; the page will say 'unknown' for it, which must not erase that.
    shades[2]["availability"] = "out_of_stock"
    row = _seed_row(variants=shades, price=34.0, availability="in_stock")
    page = _page(
        price=35.0,
        availability="in_stock",
        variants=[
            {"variant_id": "46202081018098", "price_amount": 35.0, "price_currency": "USD", "availability": "in_stock"},
            {"variant_id": "46202080624882", "price_amount": 36.0, "price_currency": "USD", "availability": "out_of_stock"},
            {"variant_id": "46202080493810", "price_amount": 37.0, "price_currency": "USD", "availability": "unknown"},
        ],
    )
    result, stored = _refresh(monkeypatch, row, page)

    assert result["variant_refresh"]["status"] == "all_re_read"
    after = _serve(stored)
    assert after["product"]["commerce_verification"]["price_trusted"] is True
    assert _offers(after) == [
        {"price": "35.0", "in_stock": True},
        {"price": "36.0", "in_stock": False},
        # Price re-read; 'unknown' stock is no observation, so the stored state stands
        # (overwriting it would hand the variant the product's in-stock claim instead).
        {"price": "37.0", "in_stock": False},
    ]


def test_one_unmatched_sibling_is_enough_to_withhold_trust(monkeypatch):
    row = _seed_row(variants=_shades(34.0), price=34.0, availability="in_stock")
    page = _page(
        price=35.0,
        availability="in_stock",
        variants=[
            {"variant_id": "46202081018098", "price_amount": 35.0, "price_currency": "USD", "availability": "in_stock"},
            {"variant_id": "46202080624882", "price_amount": 35.0, "price_currency": "USD", "availability": "in_stock"},
        ],
    )
    result, stored = _refresh(monkeypatch, row, page)

    assert result["variant_refresh"]["not_re_read"] == ["46202080493810"]
    # The two it did read are still corrected; only the claim of freshness is withheld.
    assert [v["price_amount"] for v in stored["seed_data"]["variants"]] == [35.0, 35.0, 34.0]
    assert _serve(stored)["product"]["commerce_verification"]["price_trusted"] is False


def test_a_page_keyed_by_sku_re_reads_variants_stored_under_a_shopify_id(monkeypatch):
    """perfumania.com shape: stored `variant_id` is the Shopify id, the page's offer is the SKU.

    The enrichment lane stores `price` beside `price_amount` and `currency` beside
    `price_currency`, and `external_seed_audit` falls back to the twins -- they move together.
    """
    stored = [
        {**_variant("31839072878725", 24.99, "in_stock", "1.6 oz."), "sku": "202214263", "price": 24.99, "currency": "USD", "in_stock": True},
        {**_variant("31839072911493", 68.95, "in_stock", "3.3 oz."), "sku": "202214267", "price": 68.95, "currency": "USD", "in_stock": True, "stock": "In Stock"},
    ]
    row = _seed_row(variants=stored, price=24.99, availability="in_stock")
    page = _page(
        price=22.0,
        availability="in_stock",
        variants=[
            {"variant_id": "202214263", "price_amount": 22.0, "price_currency": "USD", "availability": "in_stock"},
            {"variant_id": "202214267", "price_amount": 68.95, "price_currency": "USD", "availability": "out_of_stock"},
        ],
    )
    result, persisted = _refresh(monkeypatch, row, page)

    assert result["variant_refresh"]["status"] == "all_re_read"
    assert result["variant_refresh"]["changed"] == 2
    first, second = persisted["seed_data"]["variants"]
    assert (first["price_amount"], first["price"]) == (22.0, 22.0)
    assert (second["availability"], second["in_stock"], second["stock"]) == ("out_of_stock", False, "out_of_stock")
    assert _offers(_serve(persisted)) == [
        {"price": "22.0", "in_stock": True},
        {"price": "68.95", "in_stock": False},
    ]


def test_two_stored_variants_claiming_one_page_offer_are_both_unread(monkeypatch):
    """A SKU the merchant reused across shades names neither of them."""
    stored = [
        {**_variant("111", 20.0, "in_stock", "Light"), "sku": "SHARED"},
        {**_variant("222", 20.0, "in_stock", "Dark"), "sku": "SHARED"},
    ]
    row = _seed_row(variants=stored, price=20.0, availability="in_stock")
    page = _page(price=25.0, availability="in_stock", variants=[{"variant_id": "SHARED", "price_amount": 25.0, "price_currency": "USD", "availability": "in_stock"}])
    result, persisted = _refresh(monkeypatch, row, page)

    assert result["variant_refresh"]["not_re_read"] == ["111", "222"]
    assert [v["price_amount"] for v in persisted["seed_data"]["variants"]] == [20.0, 20.0]


def test_a_duplicated_page_id_is_ambiguous_not_a_match(monkeypatch):
    """misshaus.com shape: four JSON-LD offers all carrying the product's `@id` path."""
    path_id = "/products/missha-m-perfect-cover-bb-cream"
    row = _seed_row(
        variants=[_variant(path_id, 27.0, "in_stock", "#13"), _variant("46536716550331", 27.0, "in_stock", "#17")],
        price=27.0,
        availability="in_stock",
    )
    page = _page(
        price=21.0,
        availability="in_stock",
        variants=[{"variant_id": path_id, "price_amount": 21.0 + i, "price_currency": "USD", "availability": "in_stock"} for i in range(4)],
    )
    result, stored = _refresh(monkeypatch, row, page)

    assert stored["seed_data"]["variants"][0]["price_amount"] == 27.0
    assert result["variant_refresh"]["not_re_read_count"] == 2
    assert "extracted_at" not in stored["seed_data"]["snapshot"]


def test_a_variant_read_in_another_currency_is_not_a_re_read(monkeypatch):
    row = _seed_row(variants=[_variant("A", 20.0, "in_stock"), _variant("B", 25.0, "in_stock")], price=20.0, availability="in_stock")
    page = _page(
        price=20.0,
        availability="in_stock",
        variants=[
            {"variant_id": "A", "price_amount": 20.0, "price_currency": "USD", "availability": "in_stock"},
            {"variant_id": "B", "price_amount": 36000.0, "price_currency": "KRW", "availability": "in_stock"},
        ],
    )
    result, stored = _refresh(monkeypatch, row, page)

    assert stored["seed_data"]["variants"][1]["price_amount"] == 25.0
    assert result["variant_refresh"]["not_re_read"] == ["B"]


# ----------------------------------------------------------------- what must stay untouched


def test_a_refused_page_price_is_not_a_re_read_of_the_single_variant(monkeypatch):
    """`price: 0` is a broken-offer shape the column refuses; the variant must not be vouched for."""
    row = _seed_row(variants=[_variant("1", 16.0, "in_stock")], price=16.0, availability="in_stock")
    page = _page(price=0.0, availability="in_stock", variants=[{"variant_id": "x", "price_amount": 0.0, "availability": "in_stock"}])
    result, stored = _refresh(monkeypatch, row, page)

    assert result["price_refresh"]["status"] == "skipped_non_positive"
    assert result["variant_refresh"]["not_re_read"] == ["1"]
    assert stored["seed_data"]["variants"][0]["price_amount"] == 16.0


def test_a_cached_fallback_touches_no_variant(monkeypatch):
    """No origin reading, no write: the cached page is not evidence about the product."""
    row = _seed_row(variants=[_variant("1", 16.0, "out_of_stock")], price=16.0, availability="out_of_stock")
    page = _page(price=17.0, availability="in_stock", variants=[{"variant_id": "1", "price_amount": 17.0, "availability": "in_stock"}])
    result, stored = _refresh(monkeypatch, row, page, reached=False)

    assert result["variant_refresh"] == {"status": "not_read"}
    assert stored["seed_data"]["variants"][0]["price_amount"] == 16.0
    assert stored["seed_data"]["variants"][0]["availability"] == "out_of_stock"


def test_variants_that_carry_no_fact_do_not_block_the_stamp(monkeypatch):
    """A bare variant (no price, no stock) inherits the product values; nothing to re-read."""
    row = _seed_row(
        variants=[{"variant_id": "A", "title": "30ml"}, {"variant_id": "B", "title": "50ml"}],
        price=20.0,
        availability="in_stock",
        extracted_days_ago=400,
    )
    page = _page(price=22.0, availability="in_stock", variants=[{"variant_id": "offer_1", "price_amount": 22.0, "availability": "in_stock"}])
    result, stored = _refresh(monkeypatch, row, page)

    assert result["variant_refresh"]["status"] == "all_re_read"
    assert stored["seed_data"]["snapshot"].get("extracted_at")
    served = _serve(stored)
    assert served["product"]["commerce_verification"]["price_trusted"] is True
    assert {o["price"] for o in served["v2"]["offers"]} == {"22.0"}


# ----------------------------------------------------------------- the pure reconcile


def test_reconcile_does_not_mutate_its_input():
    from routes.employee_products import _reconcile_seed_variants_with_read

    stored = [_variant("1", 16.0, "out_of_stock")]
    snapshot = copy.deepcopy(stored)
    out, _ = _reconcile_seed_variants_with_read(
        stored, [], product_amount=17.0, product_currency="USD", product_availability="in_stock"
    )
    assert stored == snapshot
    assert out[0]["price_amount"] == 17.0 and out[0]["availability"] == "in_stock"


def test_reconcile_keeps_a_stored_boolean_in_step_with_availability():
    from routes.employee_products import _reconcile_seed_variants_with_read

    stored = [{**_variant("1", 16.0, "out_of_stock"), "in_stock": False}]
    out, _ = _reconcile_seed_variants_with_read(
        stored, [], product_amount=16.0, product_currency="USD", product_availability="in_stock"
    )
    assert out[0]["in_stock"] is True


def test_single_variant_without_a_re_read_price_is_not_re_read():
    """product_amount None means the column price was refused (0, currency mismatch...)."""
    from routes.employee_products import _reconcile_seed_variants_with_read

    out, report = _reconcile_seed_variants_with_read(
        [_variant("1", 16.0, "in_stock")], [], product_amount=None, product_currency="USD", product_availability="in_stock"
    )
    assert out[0]["price_amount"] == 16.0
    assert report["not_re_read"] == ["1"]
