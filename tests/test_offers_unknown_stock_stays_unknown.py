"""Unknown or missing availability is never served as in stock (flag OFFERS_UNKNOWN_STOCK_STAYS_UNKNOWN_ENABLED)."""
from __future__ import annotations

import pytest

import routes.agent_shop_gateway as gateway

FLAG = gateway.UNKNOWN_STOCK_STAYS_UNKNOWN_FLAG
CASES = [  # (availability, flag off, flag on)
    ("in_stock", True, True),
    ("InStock", True, True),
    ("https://schema.org/InStock", True, True),
    ("out_of_stock", False, False),
    ("sold out", False, False),
    ("unknown", True, None),
    ("", True, None),
    (None, True, None),
    ("check_at_store", True, None),
]


@pytest.fixture(params=["off", "on"])
def flag(request, monkeypatch):
    if request.param == "on":
        monkeypatch.setenv(FLAG, "true")
    else:
        monkeypatch.delenv(FLAG, raising=False)
    return request.param


def test_the_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    assert gateway._unknown_stock_stays_unknown() is False
    for value in ("1", "yes", "TRUE ", "on"):
        monkeypatch.setenv(FLAG, value)
        assert gateway._unknown_stock_stays_unknown() is (value.strip().lower() == "true")


@pytest.mark.parametrize(("availability", "off", "on"), CASES)
def test_catalog_arm_offer(flag, availability, off, on):
    row = {"offer_id": "o1", "merchant_id": "m1", "price_amount": 10, "currency": "USD", "availability": availability,
           "destination_url": "https://shop.example/products/x"}
    for group_mode in (True, False):
        offer = gateway._catalog_offer_entry(row, group_mode=group_mode, redirect_url=None)
        expected = off if flag == "off" else on
        if group_mode is False and availability == "sold out" and flag == "off":
            expected = True      # today, outside a group, the arm's own vocabulary misses "sold out" (flag on fixes it)
        assert offer["in_stock"] is expected, (availability, group_mode)
        assert offer["currency"] == "USD"            # currency untouched


@pytest.mark.parametrize("currency", ["USD", "SGD", "GBP", "EUR", None])
def test_currency_is_passed_through_unchanged_with_the_flag_on(monkeypatch, currency):
    monkeypatch.setenv(FLAG, "true")
    row = {"offer_id": "o1", "availability": "unknown", "price_amount": 9, "currency": currency}
    for group_mode in (True, False):
        assert gateway._catalog_offer_entry(row, group_mode=group_mode, redirect_url=None)["currency"] == currency


@pytest.mark.parametrize(("availability", "off", "on"), CASES)
def test_external_seed_product(flag, availability, off, on):
    row = {"id": "s1", "title": "Tinted Balm", "canonical_url": "https://shop.example/products/x",
           "destination_url": "https://shop.example/products/x", "price_amount": 12, "price_currency": "USD"}
    if availability is not None:
        row["availability"] = availability
    product = gateway._external_seed_to_shop_product(row=row, seed_data={}, redirect_url=None)
    in_stock = off if flag == "off" else on
    if flag == "off" and availability in ("sold out", "https://schema.org/InStock", "InStock"):
        in_stock = {"sold out": True, "https://schema.org/InStock": True, "InStock": True}[availability]
    assert product["in_stock"] is in_stock, availability
    assert product["inventory_quantity"] == ({True: 999, False: 0, None: None}[in_stock])
    assert product["currency"] == "USD"


@pytest.mark.parametrize(("candidate", "off", "on"), [
    ({"availability": "in_stock"}, "in_stock", "in_stock"),
    ({"availability": "out_of_stock"}, "out_of_stock", "out_of_stock"),
    ({"in_stock": True}, "in_stock", "in_stock"),
    ({"in_stock": False}, "out_of_stock", "out_of_stock"),
    ({}, "in_stock", None),
    ({"in_stock": None}, "in_stock", None),
])
def test_prefetched_candidate_availability(flag, candidate, off, on):
    assert gateway._prefetched_candidate_availability(candidate) == (off if flag == "off" else on)


def test_an_unknown_offer_is_never_best_priced_as_sold_out_and_never_counted_in_stock(monkeypatch):
    """Readers already treat a non-bool in_stock by its availability text: unknown is not out of stock (it can
    still rank), and the in-stock-only pivot (bool(in_stock)) leaves it out."""
    monkeypatch.setenv(FLAG, "true")
    offer = gateway._catalog_offer_entry({"offer_id": "o1", "availability": None, "price_amount": 5, "currency": "USD"},
                                         group_mode=True, redirect_url=None)
    assert offer["in_stock"] is None
    assert not bool(offer["in_stock"])
    assert not (offer["in_stock"] is False or gateway._availability_says_out_of_stock(offer["availability"]))


# ---- review follow-ups (P2-1, P3-1, P3-3, P3-4, parity) ----------------------------------------------------

import json
from pathlib import Path

from decimal import Decimal

import services.beauty_external_ranking as ranking
import services.unknown_stock as unknown_stock
from models.catalog import MerchantNode, OfferNode, PivotPricing, PivotQueryResponse, PivotResultItem, ProductNode, SkuNode
from services.offer_buyability import OFFER_UNAVAILABLE_AVAILABILITIES
from utils.availability_vocabulary import normalize_availability

PARITY_FIXTURE = Path(__file__).parent / "fixtures" / "availability_spellings_parity.json"


@pytest.mark.parametrize("value", ["1", "yes", "on", "enabled", "y", "t", "truee", ""])
def test_the_flag_reads_only_a_trimmed_case_insensitive_true(monkeypatch, value):
    """Documented semantics (both repos): case-insensitive "true" after trimming; "1"/"yes"/"on" stay off."""
    monkeypatch.setenv(FLAG, value)
    assert unknown_stock.unknown_stock_stays_unknown() is False
    assert gateway._unknown_stock_stays_unknown() is False
    for on in ("true", "TRUE", " True\t"):
        monkeypatch.setenv(FLAG, on)
        assert unknown_stock.unknown_stock_stays_unknown() is True
        assert gateway._unknown_stock_stays_unknown() is True


def test_one_flag_name_in_both_modules():
    assert gateway.UNKNOWN_STOCK_STAYS_UNKNOWN_FLAG == unknown_stock.UNKNOWN_STOCK_STAYS_UNKNOWN_FLAG == FLAG


@pytest.mark.parametrize(("in_stock", "off", "on"), [
    (0, "in_stock", None), (1, "in_stock", None), (0.0, "in_stock", None), ("false", "in_stock", None),
    (True, "in_stock", "in_stock"), (False, "out_of_stock", "out_of_stock"),
])
def test_prefetched_int_or_string_in_stock_is_not_a_stated_bool(flag, in_stock, off, on):
    """P3-3: 0/1 compare equal to False/True as dict keys; only the bools themselves are a statement."""
    assert gateway._prefetched_candidate_availability({"in_stock": in_stock}) == (off if flag == "off" else on)


def _parity_cases():
    return json.loads(PARITY_FIXTURE.read_text())["cases"]


def test_parity_fixture_is_whole_and_names_no_store():
    cases = _parity_cases()
    assert len(cases) >= 100
    assert {c["expected"] for c in cases} == {"in_stock", "out_of_stock", None}
    for url in [str(c["input"]) for c in cases if "://" in str(c["input"])]:
        assert "schema.org/" in url or "example.com" in url, url


@pytest.mark.parametrize("case", _parity_cases(), ids=lambda c: repr(c["input"]))
def test_availability_spellings_parity(monkeypatch, case):
    """Shared parity fixture: tests/fixtures/availability_spellings_parity.json. PIVOTA-Agent (the gateway) carries
    an IDENTICAL copy and runs the same cases through its own classifier; both copies must change together.
    Regenerate from utils/availability_vocabulary.normalize_availability (gen.py), never hand-edit."""
    assert normalize_availability(case["input"]) == case["expected"]
    monkeypatch.setenv(FLAG, "true")
    expected_stated = {"in_stock": True, "out_of_stock": False, None: None}[case["expected"]]
    assert gateway._stated_in_stock(case["input"]) is expected_stated
    assert unknown_stock.stated_in_stock(case["input"]) is expected_stated


# P3-1: the catalog arm's ORDER BY bind.

def test_catalog_unavailable_bind_is_the_base_list_with_the_flag_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    assert gateway._catalog_unavailable_spellings() == sorted(OFFER_UNAVAILABLE_AVAILABILITIES)


def test_catalog_unavailable_bind_widens_with_the_flag_on_and_never_contradicts_in_stock(monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    bound = gateway._catalog_unavailable_spellings()
    assert set(OFFER_UNAVAILABLE_AVAILABILITIES) <= set(bound)
    for spelling in ("sold out", "sold-out", "out of stock", "discontinued", "reserved", "oos",
                     "https://schema.org/soldout", "http://www.schema.org/outofstock"):
        assert spelling in bound, spelling
    for spelling in bound:
        assert spelling == spelling.strip().lower()            # the SQL compares lower(btrim(...))
        assert gateway._stated_in_stock(spelling) is False, spelling
    # Every parity case the normalizer calls out of stock and that is a bare word form is listed.
    for case in _parity_cases():
        text = case["input"]
        if case["expected"] == "out_of_stock" and isinstance(text, str) and text.strip().lower() in {
                "out_of_stock", "out of stock", "sold out", "soldout", "unavailable", "not available"}:
            assert text.strip().lower() in bound


@pytest.mark.parametrize("flag_on", [False, True])
def test_the_catalog_arm_statement_is_bound_with_the_flagged_list(monkeypatch, flag_on):
    """Through offers.resolve: flag off the bind is the base list (the statement text never changes); on, the
    wider list, and a "sold out" seller is flagged sold out and ranked after the in-stock one."""
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from main import app

    if flag_on:
        monkeypatch.setenv(FLAG, "true")
    else:
        monkeypatch.delenv(FLAG, raising=False)
    base = {"product_key": "prod::merch_obs_x::external_seed::example-serum", "currency": "USD",
            "offer_type": "retailer", "is_first_party": False, "readiness_tier": "referral_only",
            "price_confidence": 0.9, "updated_at": None, "content_key": "ck_example_serum"}
    rows = [
        {**base, "offer_id": "of_a", "merchant_id": "seller_a", "merchant_name": "Seller A",
         "availability": "sold out", "price_amount": 13.0, "destination_url": "https://a.example/products/serum"},
        {**base, "offer_id": "of_b", "merchant_id": "seller_b", "merchant_name": "Seller B",
         "availability": "in_stock", "price_amount": 19.5, "destination_url": "https://b.example/products/serum"},
    ]
    bound = {}
    statements = []

    async def fake_fetch_all(query, values=None):
        q = str(query)
        if "FROM catalog_products" in q and "JOIN catalog_offers" in q:
            bound.update(values or {})
            statements.append(q)
            return list(rows)
        return []

    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")
    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?t=1"))
    res = TestClient(app).post("/agent/shop/v1/invoke", json={
        "operation": "offers.resolve",
        "payload": {"product": {"product_id": "ext:retailer:example-serum"}, "limit": 10, "market": "US", "tool": "*",
                    "commerce_surface": "agent_api"},
        "metadata": {"source": "creator-agent-ui"}})
    assert res.status_code == 200
    expected = list(unknown_stock.SQL_OUT_OF_STOCK_SPELLINGS) if flag_on else sorted(OFFER_UNAVAILABLE_AVAILABILITIES)
    assert bound.get("unavailable") == expected
    assert statements and all(q == gateway._catalog_offers_statement("", "") for q in statements)
    offers = {o["source"]["offer_id"]: o for o in res.json().get("offers") or []}
    assert offers["of_a"]["in_stock"] is (False if flag_on else True)
    assert offers["of_b"]["in_stock"] is True


def test_both_catalog_statement_call_sites_use_the_flagged_bind():
    import inspect

    src = inspect.getsource(gateway)
    assert src.count('"unavailable": _catalog_unavailable_spellings(),') == 2   # handler + group shadow
    assert '"unavailable": sorted(OFFER_UNAVAILABLE_AVAILABILITIES)' not in src


# P2-1: the external-seed FILTER product (strict-ingredient lane's in-stock-only check).

@pytest.mark.parametrize(("availability", "off", "on"), [
    ("in_stock", (True, 999), (True, 999)),
    ("out_of_stock", (False, 0), (False, 0)),
    ("sold out", (True, 999), (False, 0)),
    ("https://schema.org/SoldOut", (True, 999), (False, 0)),
    ("InStock", (True, 999), (True, 999)),
    ("unknown", (True, 999), (False, 0)),        # flag on: in_stock/qty None in, StandardProduct reads False/0
    (None, (True, 999), (False, 0)),
])
def test_external_seed_filter_product_stock(flag, availability, off, on):
    row = {"id": "s1", "title": "Example Brand Serum", "canonical_url": "https://shop.example/products/x",
           "price_amount": 12, "price_currency": "USD"}
    if availability is not None:
        row["availability"] = availability
    product = ranking.build_external_seed_filter_product(row=row, seed_data={})
    assert (product.in_stock, product.inventory_quantity) == (off if flag == "off" else on)
    assert product.currency == "USD"


def test_external_seed_filter_product_reads_a_json_ld_availability_raw(monkeypatch):
    """The raw value, not its lowercased repr: a JSON-LD {"@id": IRI} out of stock is out of stock."""
    monkeypatch.setenv(FLAG, "true")
    for iri, expected in (("https://schema.org/OutOfStock", (False, 0)), ("https://schema.org/InStock", (True, 999))):
        row = {"id": "s1", "title": "Example Brand Serum", "price_amount": 12, "price_currency": "USD",
               "availability": {"@id": iri}}
        product = ranking.build_external_seed_filter_product(row=row, seed_data={})
        assert (product.in_stock, product.inventory_quantity) == expected, iri


def _prefetched(idx, availability):
    url = f"https://shop.example/products/hyaluronic-acid-serum-{idx}"
    cand = {"id": f"ext_prefetch_ha_{idx}", "product_id": f"ext_prefetch_ha_{idx}", "external_seed_id": f"seed_ha_{idx}",
            "title": f"Hyaluronic Acid 2% Serum No. {idx}", "description": "Example hyaluronic serum.",
            "price": 14.0 + idx, "currency": "USD", "product_type": "external", "source": "external_seed",
            "market": "US", "tool": "*", "ingredient_ids": ["hyaluronic_acid"],
            "canonical_url": url, "destination_url": url}
    if availability is not None:
        cand["availability"] = availability
    return cand


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_on", [False, True])
async def test_strict_ingredient_in_stock_only_drops_unknown_and_sold_out_seeds(monkeypatch, flag_on):
    """P3-5, end to end through _handle_find_products_multi: strict serving, agent_api, an ingredient intent,
    prefetched external seeds, in_stock_only. Flag on: only the stated in-stock seed survives the in-memory
    in_stock_only check; off: today's (unknown and "sold out" pass it)."""
    if flag_on:
        monkeypatch.setenv(FLAG, "true")
    else:
        monkeypatch.delenv(FLAG, raising=False)

    async def fake_fetch_all(query, values=None):
        return []

    async def fake_get_products_hybrid(merchant_id, limit, agent_id, background_tasks=None, force_cache_only=False):
        return [], "cache_all_platforms", None

    async def fake_redirect(**kwargs):
        return "https://api.example/r/ext"

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", fake_redirect)
    monkeypatch.setattr(gateway, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(gateway, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(gateway, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(gateway, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    seeds = {"in": "in_stock", "unknown": "unknown", "missing": None, "sold": "sold out",
             "iri": "https://schema.org/SoldOut"}
    candidates = [_prefetched(i, a) for i, a in enumerate(seeds.values())]
    payload = gateway.FindProductsMultiPayload(
        search=gateway.MultiSearchFilters(query="hyaluronic serum", page=1, limit=10, in_stock_only=True,
                                          commerce_surface="agent_api"),
        metadata=gateway.RequestMetadata(source="shopping_agent"),
    )
    result = await gateway._handle_find_products_multi(
        payload, {"source": "shopping_agent", "external_seed_candidates": candidates}, gateway.BackgroundTasks())
    served = {p["title"] for p in result.get("products") or []}   # product ids are re-minted from the URL
    metadata = result.get("metadata") or {}
    assert metadata.get("ingredient_intents") == ["hyaluronic_acid"]
    if flag_on:
        assert served == {"Hyaluronic Acid 2% Serum No. 0"}
        assert metadata["ingredient_candidate_breakdown"]["eligible_external_seed"] == 1
    else:
        assert served == {f"Hyaluronic Acid 2% Serum No. {i}" for i in range(len(seeds))}


def _pivot_item(idx, availability, inventory_quantity):
    return PivotResultItem(
        merchant=MerchantNode(merchant_id="merch_1", merchant_name="Example Merchant", primary_platform="external"),
        product=ProductNode(product_key=f"prod::merch_1::external::{idx}", source_product_id=str(idx),
                            title=f"Example Brand Serum {idx}", description="A serum", brand="Example Brand",
                            product_type="serum", category="serum",
                            canonical_url=f"https://shop.example/products/{idx}",
                            image_url="https://shop.example/image.jpg"),
        sku=SkuNode(sku_key=f"sku::{idx}", source_variant_id=f"var_{idx}", sku=f"EX-{idx}", title=f"Serum {idx}",
                    visible_attributes={}, visible_option_labels=[], ingredient_ids=[]),
        offers=[OfferNode(
            offer_id=f"offer::{idx}", merchant_id="seller_1", merchant_name="Example Seller",
            catalog_track="external_referral", truth_tier="primary", readiness_tier="knowledge_ready",
            offer_mode="affiliate_outbound", source_system="external_seed", availability=availability,
            inventory_quantity=inventory_quantity,
            pricing=PivotPricing(currency="USD", list_price=Decimal("20.00"), merchant_effective_price=Decimal("20.00"),
                                 estimated_best_price=Decimal("20.00"), price_confidence=Decimal("1.0")),
            incentives=[])],
        catalog_track="external_referral", truth_tier="primary", readiness_tier="knowledge_ready",
        freshness={"updated_at": "2026-11-01T00:00:00Z"}, source_system="external_seed",
        match_explanation={"lane": "catalog_discovery", "exact_match": False}, verticals={},
    )


@pytest.mark.asyncio
async def test_in_stock_only_pivot_leaves_out_an_unknown_seed(flag, monkeypatch):
    """P3-5: the in-stock-only pivot (bool(in_stock) or a positive count). An unknown seed (no availability
    statement, no count) is not counted in stock, flag on or off; the stated in-stock one is served."""
    async def fake_search(req):
        return PivotQueryResponse(query=req.query, total=3, items=[
            _pivot_item(1, "in_stock", None), _pivot_item(2, "unknown", None), _pivot_item(3, None, None)])

    monkeypatch.setattr(gateway, "search_pivot_catalog", fake_search)
    result = await gateway._handle_find_products_multi_via_pivot(
        gateway.FindProductsMultiPayload(
            search=gateway.MultiSearchFilters(query="serum", page=1, limit=10, in_stock_only=True),
            metadata=gateway.RequestMetadata(source="shopping_agent"),
        ),
        {"source": "shopping_agent"},
    )
    assert [p["product_id"] for p in (result or {}).get("products") or []] == ["1"]
