"""Actual canonical route -> pivot service -> recall SQL -> product projection.

The only catalog is an in-memory SQL database, with invented, clearly labelled
test rows. No canned products are returned by the search/route implementations.
Production reproduction receipts live outside this test corpus.
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from fastapi import BackgroundTasks
import pytest

import routes.agent_shop_gateway as gateway
import services.pivot_query_service as pivot
from services.canonical_search_query import normalize_catalog_query, prepare_canonical_search_query


@pytest.mark.parametrize("query,expected", [
    ("moisturizers", "moisturizer"), ("moisturisers", "moisturiser"),
    ("cleansers", "cleanser"), ("serums", "serum"), ("sunscreens", "sunscreen"),
    ("blushes", "blush"), ("dresses", "dress"),
    ("Find two ceramide moisturizers", "ceramide moisturizer"),
    ("show me SPF 50 sunscreens", "SPF 50 sunscreen"),
    ("zzqvyst moisturizers", "zzqvyst moisturizer"),
    ("headphones", "headphones"), ("glass", "glass"), ("NARS", "NARS"),
    ("iPhone 15 Pro", "iPhone 15 Pro"), ("mysterious widgets", "mysterious widgets"),
])
def test_only_known_category_inflections_are_normalized(query, expected):
    assert normalize_catalog_query(query) == expected


@pytest.mark.parametrize("query,currency,maximum,exclusive", [
    ("moisturizers. under USD 30", "USD", "30", True),
    ("moisturizers. under USD 30.", "USD", "30", True),
    ("moisturizers under USD30", "USD", "30", True),
    ("moisturizers under USD 30.00.", "USD", "30.00", True),
    ("moisturizers under $30", "USD", "30", True),
    ("moisturizers up to 30 euros", "EUR", "30", False),
    ("moisturizers below GBP 30.50", "GBP", "30.50", True),
    ("moisturizers maximum CAD 40", "CAD", "40", False),
    ("moisturizers under USD .50", "USD", ".50", True),
])
def test_budget_is_preserved_separately_from_retrieval(query, currency, maximum, exclusive):
    plan = prepare_canonical_search_query(query)
    assert plan.original_query == query
    assert plan.retrieval_query == "moisturizer"
    assert (plan.currency, plan.price_max, plan.max_exclusive, plan.error) == (currency, Decimal(maximum), exclusive, None)


@pytest.fixture
def catalog(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    columns = {
        "catalog_products": "product_key content_key pivota_signature_id pivota_canonical_url source_product_id title description brand product_type category canonical_url image_url catalog_track truth_tier readiness_tier pdp_scope pdp_lifecycle_stage source_system freshness_json updated_at merchant_id category_path sync_status suppressed_at suppression_reason".split(),
        "catalog_skus": "sku_key product_key source_variant_id sku barcode title visible_attributes visible_option_labels ingredient_ids image_url updated_at suppressed_at suppression_reason".split(),
        "catalog_merchants": "merchant_id merchant_name primary_platform status indexable metadata_json".split(),
        "catalog_offers": "offer_id sku_key merchant_id catalog_track truth_tier readiness_tier offer_mode availability inventory_quantity currency list_price merchant_effective_price estimated_best_price price_confidence source_system offer_type market is_first_party source_domain why_buy_direct offer_payload suppressed_at updated_at".split(),
    }
    for table, names in columns.items():
        conn.execute(f"CREATE TABLE {table} ({', '.join(names)})")

    def insert(table, values):
        conn.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))

    insert("catalog_merchants", {"merchant_id": "test_owner", "merchant_name": "Test Owner", "status": "active", "indexable": 1})
    insert("catalog_merchants", {"merchant_id": "test_seller", "merchant_name": "Test Seller", "status": "active", "indexable": 1})
    for key, amount, currency, kind in [
        ("cheap", 12, "USD", "moisturizer"), ("mid", 24.5, "USD", "moisturizer"),
        ("boundary", 30, "USD", "moisturizer"), ("expensive", 48, "USD", "moisturizer"),
        ("eur", 12, "EUR", "moisturizer"), ("unknown", 10, None, "moisturizer"),
        ("serum", 15, "USD", "serum"),
    ]:
        title = f"Test {key} {kind}"
        insert("catalog_products", {
            "product_key": key, "pivota_signature_id": "sig_test_"+key, "source_product_id": key,
            "title": title, "description": title, "brand": "Test", "product_type": kind,
            "category": kind, "merchant_id": "test_owner", "sync_status": "live",
            "category_path": "beauty/skincare/moisturize/cream" if kind == "moisturizer" else "beauty/skincare/treat/serum",
            "catalog_track": "external_referral", "truth_tier": "primary", "readiness_tier": "knowledge_ready",
            "canonical_url": "https://merchant.example/test/"+key, "image_url": "https://merchant.example/test.png",
            "pdp_scope": "merchant_owned", "pdp_lifecycle_stage": "published",
        })
        insert("catalog_skus", {"sku_key": key, "product_key": key, "title": title, "sku": key,
            "source_variant_id": "variant_"+key, "visible_attributes": json.dumps({"product_category": [kind]}),
            "visible_option_labels": "[]", "ingredient_ids": "[]"})
        insert("catalog_offers", {"offer_id": "offer_"+key, "sku_key": key, "merchant_id": "test_seller",
            "catalog_track": "external_referral", "truth_tier": "primary", "readiness_tier": "knowledge_ready",
            "offer_mode": "external_redirect", "availability": "in_stock", "inventory_quantity": 2,
            "currency": currency, "list_price": amount, "merchant_effective_price": amount,
            "estimated_best_price": amount, "price_confidence": 1, "market": "US"})

    class Observations(list):
        pass
    observed = Observations()
    observed.connection = conn
    observed.insert = insert

    class SqlCatalog:
        async def fetch_all(self, query, values=None):
            observed.append((str(query), dict(values or {})))
            return conn.execute(str(query), values or {}).fetchall()

    async def no_brand(query):
        return [], None

    monkeypatch.setattr(pivot, "database", SqlCatalog())
    # Brand dictionary is an independent DB read. Keep it empty here; recall,
    # assembly, query matching, price filtering and pagination are all real.
    monkeypatch.setattr(gateway, "_resolve_brand_anchor_terms", no_brand)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_INCLUDE_INCENTIVES", False)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SHADOW_ENABLED", False)
    monkeypatch.setenv("INDEX_ELIGIBLE_RECALL", "false")
    yield observed
    conn.close()


async def run_query(query, **fields):
    metadata = {"source": "shopping_agent", "catalog_surface": "agent_api", "commerce_surface": "agent_api"}
    if "market" in fields:
        metadata["market"] = fields.pop("market")
    return await gateway._handle_find_products_multi_inner(
        gateway.FindProductsMultiPayload(search=gateway.MultiSearchFilters(
            query=query, catalog_entity_mode="canonical_sig", commerce_surface="agent_api",
            in_stock_only=True, limit=fields.pop("limit", 12), **fields,
        )), metadata, BackgroundTasks(),
    )


ALL_USD_MOISTURIZERS = {"sig_test_cheap", "sig_test_mid", "sig_test_boundary", "sig_test_expensive"}


def assert_budget_reported_not_enforced(result, query):
    """A money clause we cannot represent is never enforced as a wrong bound,
    never empties the search, and is handed back as an unverified constraint."""
    metadata = result["metadata"]
    plan = metadata["canonical_query"]
    assert "strict_empty_reason" not in metadata
    assert (plan["price_min"], plan["price_max"], plan["error"]) == (None, None, None)
    assert plan["unparsed_budget_clause"] is True
    assert metadata["unverified_constraints"] == [query]
    assert ALL_USD_MOISTURIZERS <= {p["product_id"] for p in result["products"]}


@pytest.mark.asyncio
async def test_actual_canonical_route_singular_plural_and_ui_compound(catalog):
    singular = await run_query("moisturizer")
    plural = await run_query("moisturizers")
    compound = await run_query("moisturizers. under USD 30")
    assert {p["product_id"] for p in singular["products"]} == {p["product_id"] for p in plural["products"]}
    assert {p["product_id"] for p in compound["products"]} == {"sig_test_cheap", "sig_test_mid"}
    assert compound["metadata"]["canonical_query"]["original_query"] == "moisturizers. under USD 30"
    assert all(p["merchant_id"] == "test_seller" for p in compound["products"])
    assert all(p["offers"][0]["catalog_track"] == "external_referral" for p in compound["products"])
    assert all(params["query_exact"] == "moisturizer" for _, params in catalog)
    assert all(params["category_path_prefix"] == "beauty/skincare/moisturize/%" for _, params in catalog)


@pytest.mark.asyncio
async def test_budget_currency_and_boundaries_before_page_slice(catalog):
    inclusive = await run_query("moisturizers. up to USD 30")
    assert {p["product_id"] for p in inclusive["products"]} == {"sig_test_cheap", "sig_test_mid", "sig_test_boundary"}
    euros = await run_query("moisturizers. under EUR 30")
    assert [p["product_id"] for p in euros["products"]] == ["sig_test_eur"]
    tight = await run_query("moisturizers under USD 30", price_max=15, currency="USD")
    assert [p["product_id"] for p in tight["products"]] == ["sig_test_cheap"]
    impossible = await run_query("moisturizers under USD 10")
    assert impossible["products"] == []
    assert impossible["metadata"]["direct_external_seed_lane"] is False
    second_page = await run_query("moisturizers under USD 30", limit=1, page=2)
    assert [p["product_id"] for p in second_page["products"]] == ["sig_test_mid"]
    assert second_page["total"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("query,fields,error", [
    ("moisturizers over USD 30 under USD 20", {}, "empty_budget_range"),
    ("under USD 30", {}, "missing_product_query"),
    # A caller's own invalid API bound is its error, not a shopper's phrasing.
    ("moisturizers", {"price_max": -5}, "invalid_budget_amount"),
])
async def test_contradictory_or_invalid_bounds_return_empty(catalog, query, fields, error):
    result = await run_query(query, **fields)
    assert result["products"] == []
    assert result["metadata"]["strict_empty_reason"] == error
    assert catalog == []


@pytest.mark.asyncio
async def test_unknown_product_query_remains_unknown(catalog):
    result = await run_query("zzqvyst widgets under USD 30")
    assert result["products"] == []
    assert catalog[0][1]["query_exact"] == "zzqvyst widgets"


@pytest.mark.asyncio
async def test_unrelated_categories_are_not_admitted(catalog):
    result = await run_query("serums under USD 30")
    assert [p["product_id"] for p in result["products"]] == ["sig_test_serum"]


@pytest.mark.asyncio
@pytest.mark.parametrize("clause", [
    "at most USD 30", "maximum of USD30", "budget of USD30", "budget USD30",
    "with a budget of USD30", "no more than USD30", "not more than USD30",
])
async def test_budget_paraphrases_are_enforced_on_actual_canonical_route(catalog, clause):
    result = await run_query(f"moisturizers {clause}")
    assert {p["product_id"] for p in result["products"]} == {"sig_test_cheap", "sig_test_mid", "sig_test_boundary"}
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_max"], plan["budget_currency"], plan["max_exclusive"], plan["error"]) == ("30", "USD", False, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("clause", ["at least USD30", "minimum of USD30", "no less than USD30", "not less than USD30"])
async def test_lower_budget_paraphrases_keep_direction(catalog, clause):
    result = await run_query(f"moisturizers {clause}")
    assert {p["product_id"] for p in result["products"]} == {"sig_test_boundary", "sig_test_expensive"}
    assert result["metadata"]["canonical_query"]["price_min"] == "30"


@pytest.mark.asyncio
@pytest.mark.parametrize("query,fields", [
    ("moisturizers under USD 30", {"currency": "EUR"}),
    ("moisturizers under USD -30", {}),
    ("moisturizers under USD 1,000", {}),
    ("moisturizers under 30", {"market": "ZZ"}),
])
async def test_unrepresentable_budgets_are_reported_not_enforced(catalog, query, fields):
    assert_budget_reported_not_enforced(await run_query(query, **fields), query)


@pytest.mark.asyncio
@pytest.mark.parametrize("clause", [
    "under about USD30", "maximum around USD30", "budget is USD30", "budget = 30",
    "price<=30", "priced at USD30", "around USD30", "for 30 dollars", "under USD30cm",
    "not under USD30", "not above USD30", "costing around USD30", "under USD1e3",
    "budget of 30cm", "price below 30cm",
])
async def test_unrepresented_numeric_money_clauses_are_reported_not_enforced(catalog, clause):
    query = f"moisturizers {clause}"
    assert_budget_reported_not_enforced(await run_query(query), query)


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", [
    "under30cm", "under 30 cm", "at most 30cm", "under 30-inch", "under 30 inches",
    "below 1.5kg", "above 30ml", "at least 256GB", "SPF30", "XPS13", "iPhone 15 Pro",
])
async def test_measurements_and_model_numbers_remain_retrieval_terms(catalog, goal):
    result = await run_query(f"moisturizers {goal}")
    plan = result["metadata"]["canonical_query"]
    assert plan["error"] is None
    assert plan["price_min"] is None and plan["price_max"] is None
    assert plan["retrieval_query"] == f"moisturizer {goal}"
    assert catalog[0][1]["query_exact"] == f"moisturizer {goal}".lower()


@pytest.mark.asyncio
async def test_measurement_and_money_constraints_coexist(catalog):
    result = await run_query("moisturizers under30cm. at most USD30")
    assert {p["product_id"] for p in result["products"]} == {"sig_test_cheap", "sig_test_mid", "sig_test_boundary"}
    assert result["metadata"]["canonical_query"]["retrieval_query"] == "moisturizer under30cm"


@pytest.mark.asyncio
@pytest.mark.parametrize("clause", [
    "do not cost more than USD30", "does not cost above USD30",
    "don't cost over USD30", "doesn’t cost more than USD30", "not priced over USD30",
    "do not be priced above USD30", "without spending over USD30",
    "never pay more than USD30", "not paying over USD30", "not charged over USD30",
    "never cost more than USD30",
])
async def test_complete_negated_money_clauses_do_not_reverse_budget(catalog, clause):
    result = await run_query(f"moisturizers {clause}")
    assert {p["product_id"] for p in result["products"]} == {"sig_test_cheap", "sig_test_mid", "sig_test_boundary"}
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_min"], plan["price_max"], plan["max_exclusive"], plan["budget_currency"]) == (None, "30", False, "USD")
    assert plan["retrieval_query"] == "moisturizer"


@pytest.mark.asyncio
@pytest.mark.parametrize("clause,expected,lower,upper,exclusive", [
    ("not priced under USD30", {"sig_test_boundary", "sig_test_expensive"}, "30", None, False),
    ("without spending less than USD30", {"sig_test_boundary", "sig_test_expensive"}, "30", None, False),
    ("not priced at least USD30", {"sig_test_cheap", "sig_test_mid"}, None, "30", True),
    ("not priced at most USD30", {"sig_test_expensive"}, "30", None, True),
])
async def test_negated_money_bounds_preserve_direction_and_boundary(catalog, clause, expected, lower, upper, exclusive):
    result = await run_query(f"moisturizers {clause}")
    assert {p["product_id"] for p in result["products"]} == expected
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_min"], plan["price_max"]) == (lower, upper)
    assert plan["min_exclusive" if lower else "max_exclusive"] is exclusive


@pytest.mark.asyncio
async def test_recognized_negation_does_not_invalidate_a_following_bound(catalog):
    result = await run_query("moisturizers not priced over USD30 and at least USD20")
    assert {p["product_id"] for p in result["products"]} == {"sig_test_mid", "sig_test_boundary"}


@pytest.mark.asyncio
@pytest.mark.parametrize("clause", [
    "do not ever cost more than USD30", "do not want products costing more than USD30",
    "without having to spend over USD30", "not priced no more than USD30",
    "not necessarily over USD30", "under thirty dollars", "under USD thirty",
    "budget of thirty dollars", "under several euros", "under XYZ30",
    "I do not want to spend more than USD30 on", "with no price over USD30",
])
async def test_unsupported_full_money_clauses_are_reported_not_enforced(catalog, clause):
    query = f"moisturizers {clause}"
    assert_budget_reported_not_enforced(await run_query(query), query)


@pytest.mark.asyncio
@pytest.mark.parametrize("clause", [
    "under USDNaN", "under USD NaN", "under NaN USD", "under NaN",
    "under USD Infinity", "under USD -Infinity", "under -Infinity USD",
    "under infinityUSD", "under USD inf", "under USD +inf", "budget of NaN",
    "at most USD sNaN", "maximum USD ∞", "under thirty dollars and under USDNaN",
])
async def test_nonfinite_text_money_is_reported_not_enforced(catalog, clause):
    query = f"moisturizers {clause}"
    assert_budget_reported_not_enforced(await run_query(query), query)


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", [
    "under SPF 30", "for women under age 30", "with under 5% niacinamide",
    "with at least 10% urea", "less than 2 ounces", "rated over 4 stars",
    "under 5 percent niacinamide", "with at least 10 percentage urea",
])
async def test_typed_non_money_numbers_never_acquire_currency_from_later_budget(catalog, constraint):
    result = await run_query(f"moisturizers {constraint} under USD30")
    # Existing ingredient/SPF gates may reject these deliberately plain test
    # products; their absence of evidence must not be relaxed by the parser.
    expected = set() if "niacinamide" in constraint or "SPF" in constraint else {"sig_test_cheap", "sig_test_mid"}
    assert {p["product_id"] for p in result["products"]} == expected
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_min"], plan["price_max"], plan["budget_currency"]) == (None, "30", "USD")
    assert plan["retrieval_query"] == f"moisturizer {constraint}"


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", ["under SPF 30", "for women under age 30"])
async def test_three_letter_attributes_are_not_invented_currencies(catalog, constraint):
    result = await run_query(f"moisturizers {constraint}")
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_min"], plan["price_max"], plan["budget_currency"], plan["error"]) == (None, None, None, None)
    assert constraint in plan["retrieval_query"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["moisturizers under 5 under USD30", "moisturizers above 2 frobs under USD30"])
async def test_an_unclear_number_beside_a_written_budget_stays_text(catalog, query):
    # Only the written USD bound is enforced; the unclear number is reported.
    result = await run_query(query)
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_min"], plan["price_max"], plan["budget_currency"]) == (None, "30", "USD")
    assert plan["unparsed_budget_clause"] is True
    assert result["metadata"]["unverified_constraints"] == [query]
    assert {p["product_id"] for p in result["products"]} == {"sig_test_cheap", "sig_test_mid"}


@pytest.mark.asyncio
async def test_an_untyped_bound_never_takes_a_different_written_currency(catalog):
    query = "moisturizers under 30 and over 10 euros"
    result = await run_query(query)
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_min"], plan["price_max"], plan["budget_currency"]) == (None, None, None)
    assert plan["unparsed_budget_clause"] is True


def test_nonfinite_words_without_monetary_context_remain_product_text():
    plan = prepare_canonical_search_query("Infinity moisturizer")
    assert plan.error is None
    assert plan.retrieval_query == "Infinity moisturizer"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["NOK", "SEK", "AED", "nok", "sek", "aed"])
@pytest.mark.parametrize("position", ["prefix", "suffix"])
@pytest.mark.parametrize("context_currency", [None, "USD"])
async def test_unrecognized_currency_codes_never_become_usd_budgets(catalog, code, position, context_currency):
    clause = f"under {code}30" if position == "prefix" else f"under 30 {code}"
    fields = {"currency": context_currency} if context_currency else {}
    query = f"moisturizers {clause}"
    assert_budget_reported_not_enforced(await run_query(query, **fields), query)


@pytest.mark.asyncio
async def test_budget_friendly_is_a_goal_not_a_money_bound(catalog):
    result = await run_query("budget friendly moisturizers")
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_min"], plan["price_max"], plan["budget_currency"], plan["error"]) == (None, None, None, None)
    assert plan["retrieval_query"] == "budget friendly moisturizer"


@pytest.mark.asyncio
async def test_explicit_currency_allows_an_unambiguous_numeric_bound(catalog):
    result = await run_query("moisturizers under 30", currency="USD")
    assert {p["product_id"] for p in result["products"]} == {"sig_test_cheap", "sig_test_mid"}


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["budget friendly SPF50 sunscreens", "budget iPhone 15 cases", "budget moisturizers with 30ml size"])
async def test_budget_adjective_does_not_turn_model_or_size_into_money(catalog, query):
    result = await run_query(query)
    plan = result["metadata"]["canonical_query"]
    assert plan["error"] is None
    assert (plan["price_min"], plan["price_max"], plan["budget_currency"]) == (None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("clause", ["do not have a price above USD30", "no dearer than thirty dollars"])
async def test_other_negated_money_predicates_are_reported_not_enforced(catalog, clause):
    query = f"moisturizers {clause}"
    assert_budget_reported_not_enforced(await run_query(query), query)


@pytest.mark.asyncio
@pytest.mark.parametrize("qualifier", ["zzqvyst", "fragrance-free", "without fragrance", "SPF 50", "not for sensitive skin"])
async def test_residual_qualifiers_are_not_certified_by_category_recall(catalog, qualifier):
    original = f"{qualifier} moisturizers under USD30"
    result = await run_query(original)
    assert result["metadata"]["canonical_query"]["original_query"] == original
    # The qualifier is always handed back unverified: alone, or inside the
    # shopper's full query when a nearby negation left the budget unenforced.
    # Existing SPF/ingredient gates may reject these plain test products.
    if result["products"]:
        assert any(qualifier in constraint for constraint in result["metadata"]["unverified_constraints"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"merchant_id": "test_seller", "search_all_merchants": False},
    {"merchant_ids": ["test_seller"], "search_all_merchants": False},
    {"merchantId": "test_seller", "searchAllMerchants": False},
])
async def test_exact_seller_scope_is_offer_scoped(catalog, fields):
    catalog.insert("catalog_merchants", {"merchant_id": "test_seller_extra", "merchant_name": "Other Seller", "status": "active", "indexable": 1})
    catalog.connection.execute("UPDATE catalog_offers SET merchant_id = 'test_seller_extra' WHERE sku_key = 'mid'")
    result = await run_query("moisturizers under USD30", **fields)
    assert [p["product_id"] for p in result["products"]] == ["sig_test_cheap"]
    assert all(p["merchant_id"] == "test_seller" for p in result["products"])
    assert all(offer["merchant_id"] == "test_seller" for p in result["products"] for offer in p["offers"])
    assert (await run_query("moisturizers under USD30", merchant_id="test_owner"))["products"] == []


@pytest.mark.asyncio
async def test_direct_canonical_seller_scope_keeps_owner_and_seller_gates(catalog):
    rows = await pivot._fetch_canonical_search_rows(query="moisturizers", merchant_id="test_seller", limit=12, require_signature=True)
    assert rows and all(row["offer_merchant_id"] == "test_seller" for row in rows)
    catalog.connection.execute("UPDATE catalog_merchants SET status = 'inactive' WHERE merchant_id = 'test_owner'")
    assert await pivot._fetch_canonical_search_rows(query="moisturizers", merchant_id="test_seller", limit=12, require_signature=True) == []


def add_crowded_catalog_rows(catalog, *, count=205, seller="test_seller", amount=100):
    for index in range(count):
        key = f"premium_{index}"
        for table in ("catalog_products", "catalog_skus", "catalog_offers"):
            selector = "product_key" if table != "catalog_offers" else "sku_key"
            row = dict(catalog.connection.execute(f"SELECT * FROM {table} WHERE {selector} = 'cheap'").fetchone())
            if table == "catalog_products":
                row.update(product_key=key, pivota_signature_id="sig_test_"+key, source_product_id=key, title="moisturizer")
            elif table == "catalog_skus":
                row.update(product_key=key, sku_key=key, source_variant_id="v_"+key, title="moisturizer")
            else:
                row.update(offer_id="o_"+key, sku_key=key, merchant_id=seller, list_price=amount, merchant_effective_price=amount, estimated_best_price=amount)
            catalog.insert(table, row)


@pytest.mark.asyncio
async def test_budget_applies_before_candidate_limit(catalog):
    add_crowded_catalog_rows(catalog)
    result = await run_query("moisturizers under USD30")
    assert {p["product_id"] for p in result["products"]} == {"sig_test_cheap", "sig_test_mid"}
    assert result["metadata"]["canonical_recall"] == {
        "exhaustive": False, "candidate_result_limit": 48,
        "budget_applied_before_candidate_limit": True,
        "seller_scope_applied_before_candidate_limit": False,
    }


@pytest.mark.asyncio
async def test_seller_scope_applies_before_candidate_limit(catalog):
    catalog.insert("catalog_merchants", {"merchant_id": "other_seller", "status": "active", "indexable": 1})
    add_crowded_catalog_rows(catalog, seller="other_seller", amount=10)
    result = await run_query("moisturizers under USD30", merchant_id="test_seller")
    assert {p["product_id"] for p in result["products"]} == {"sig_test_cheap", "sig_test_mid"}


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", 0, -1])
@pytest.mark.parametrize("bounded", [True, False])
async def test_corrupt_offer_money_does_not_crash_canonical_search(catalog, amount, bounded):
    catalog.connection.execute("UPDATE catalog_offers SET list_price = ?, merchant_effective_price = ?, estimated_best_price = ? WHERE sku_key = 'expensive'", (amount, amount, amount))
    result = await run_query("moisturizers under USD30" if bounded else "moisturizers")
    ids = {p["product_id"] for p in result["products"]}
    assert {"sig_test_cheap", "sig_test_mid"} <= ids
    if bounded or isinstance(amount, str):
        assert "sig_test_expensive" not in ids


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["list_price", "merchant_effective_price", "estimated_best_price", "price_confidence"])
async def test_nonfinite_auxiliary_money_is_rejected_before_projection(catalog, field):
    catalog.connection.execute(f"UPDATE catalog_offers SET {field} = 'NaN' WHERE sku_key = 'cheap'")
    result = await run_query("moisturizers under USD30")
    assert [p["product_id"] for p in result["products"]] == ["sig_test_mid"]


@pytest.mark.asyncio
async def test_missing_exact_seller_scope_does_not_broaden(catalog):
    result = await run_query("moisturizers", merchant_ids=[" "], search_all_merchants=False)
    assert result["products"] == []
    assert result["metadata"]["strict_empty_reason"] == "seller_scope_required"
    assert catalog == []


def test_flat_multi_payload_keeps_seller_and_currency_scope():
    payload = gateway._normalize_find_products_multi_payload({
        "query": "moisturizers", "merchant_id": "seller_a", "merchant_ids": ["seller_b"],
        "search_all_merchants": False, "currency": "USD", "request_context": {"currency": "USD"},
    })
    assert payload["search"]["merchant_id"] == "seller_a"
    assert payload["search"]["merchant_ids"] == ["seller_b"]
    assert payload["search"]["search_all_merchants"] is False
    assert payload["search"]["currency"] == "USD"


@pytest.mark.asyncio
@pytest.mark.parametrize("clause", ["under thirty NOK", "under NOK thirty", "budget of thirty AED", "maximum SEK forty", "under thirty"])
async def test_word_amounts_are_reported_whatever_the_currency(catalog, clause):
    query = f"moisturizers {clause}"
    assert_budget_reported_not_enforced(await run_query(query), query)


def test_word_valued_measurement_stays_non_monetary():
    plan = prepare_canonical_search_query("moisturizers under five percent niacinamide under USD30")
    assert plan.error is None
    assert plan.price_max == Decimal(30) and plan.price_min is None
    assert "under five percent" in plan.retrieval_query


@pytest.mark.asyncio
@pytest.mark.parametrize("clause", [
    "under about thirty NOK", "price should be thirty NOK",
    "below approximately forty AED", "budget around thirty SEK",
    "under no more than roughly thirty NOK", "price should ideally be thirty AED",
    "cost roughly twenty SEK", "spending around about fifty NOK",
    "afford approximately forty AED", "no dearer than around thirty SEK",
    "maximum in total about thirty NOK", "budget of approximately thirty",
    "under around thirty USD", "below roughly forty dollars",
    "price should be around thirty", "budget would be about thirty XYZ",
])
async def test_modified_word_money_clauses_are_reported_not_enforced(catalog, clause):
    query = f"moisturizers {clause}"
    assert_budget_reported_not_enforced(await run_query(query), query)


@pytest.mark.parametrize("constraint", [
    "under about five percent niacinamide", "below approximately two ounces",
    "rated over about four stars", "under around thirty cm",
    "with under roughly five per cent niacinamide",
])
def test_modified_word_measurements_remain_typed(constraint):
    plan = prepare_canonical_search_query(f"moisturizers {constraint} under USD30")
    assert plan.error is None
    assert plan.price_min is None and plan.price_max == Decimal(30)
    assert constraint in plan.retrieval_query


@pytest.mark.parametrize("query", [
    "budget moisturizers in thirty ml", "budget friendly creams in two ounces",
    "budget friendly SPF50 sunscreens", "budget iPhone 15 cases",
    "budget moisturizers with 30ml size", "budget oneplus cases",
])
def test_word_amount_refusal_keeps_budget_adjectives_and_typed_sizes(query):
    plan = prepare_canonical_search_query(query)
    assert plan.error is None
    assert plan.price_min is None and plan.price_max is None


# --- 2026-10-05 review regressions: real shopper phrasing ------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "moisturizers for women over 50", "moisturizers for skin over 40",
    "moisturizers for over 40s", "moisturizers for over 60 skin",
    "moisturizers for women over 50s", "moisturizers for under 18s",
])
async def test_age_and_audience_numbers_are_never_price_floors(catalog, query):
    # Before the fix "women over 50" became price > $50 and "over 40s" emptied
    # the search. Neither is money: every moisturizer is still offered.
    result = await run_query(query)
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_min"], plan["price_max"], plan["error"]) == (None, None, None)
    assert "strict_empty_reason" not in result["metadata"]
    assert ALL_USD_MOISTURIZERS <= {p["product_id"] for p in result["products"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("clause", [
    "between $20 and $30", "$20-$30", "$20 - $30", "$20 to $30", "from 20 to 30 dollars",
    "between USD 20 and USD 30", "20-30 dollars", "between 20 and 30", "between $30 and $20",
])
async def test_price_ranges_are_enforced_inclusively(catalog, clause):
    result = await run_query(f"moisturizers {clause}")
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_min"], plan["price_max"], plan["budget_currency"]) == ("20", "30", "USD")
    assert (plan["min_exclusive"], plan["max_exclusive"], plan["unparsed_budget_clause"]) == (False, False, False)
    assert plan["retrieval_query"] == "moisturizer"
    assert {p["product_id"] for p in result["products"]} == {"sig_test_mid", "sig_test_boundary"}


@pytest.mark.parametrize("query", [
    "toner 100-200ml", "SPF 30-50 sunscreen", "lip 2-3 pack", "k18-20 mask", "moisturizers 20-30",
])
def test_ranges_without_money_markers_stay_text(query):
    plan = prepare_canonical_search_query(query, market_currency="USD")
    assert (plan.price_min, plan.price_max, plan.error) == (None, None, None)
    assert plan.retrieval_query == normalize_catalog_query(query)


@pytest.mark.asyncio
async def test_market_currency_prices_an_untyped_bound(catalog):
    # The UI and Aurora send "under 30" with no currency. US -> USD.
    result = await run_query("moisturizers under 30")
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_max"], plan["budget_currency"], plan["unparsed_budget_clause"]) == ("30", "USD", False)
    assert {p["product_id"] for p in result["products"]} == {"sig_test_cheap", "sig_test_mid"}


@pytest.mark.asyncio
async def test_market_currency_prices_explicit_api_bounds(catalog):
    result = await run_query("moisturizers", price_max=25)
    plan = result["metadata"]["canonical_query"]
    assert (Decimal(plan["price_max"]), plan["budget_currency"]) == (Decimal(25), "USD")
    assert {p["product_id"] for p in result["products"]} == {"sig_test_cheap", "sig_test_mid"}


@pytest.mark.asyncio
async def test_a_written_currency_beats_the_market_currency(catalog):
    result = await run_query("moisturizers under 30 euros")
    assert result["metadata"]["canonical_query"]["budget_currency"] == "EUR"
    assert [p["product_id"] for p in result["products"]] == ["sig_test_eur"]


@pytest.mark.asyncio
async def test_a_non_us_market_prices_in_its_own_currency(catalog):
    result = await run_query("moisturizers under 30", market="SG")
    plan = result["metadata"]["canonical_query"]
    assert (plan["price_max"], plan["budget_currency"]) == ("30", "SGD")
    # The test catalog has no SGD offers; USD rows are never compared as SGD.
    assert result["products"] == []
    assert "strict_empty_reason" not in result["metadata"]


@pytest.mark.parametrize("query", ["best price on k18", "k18 under SPF 30", "moisturizers under age30"])
def test_model_numbers_and_attributes_are_not_unparsed_money(query):
    plan = prepare_canonical_search_query(query, market_currency="USD")
    assert (plan.price_min, plan.price_max, plan.error, plan.unparsed_budget) == (None, None, None, False)


@pytest.mark.asyncio
async def test_recent_same_category_rows_cannot_crowd_out_products_that_name_the_query(catalog):
    # 2026-10-05 prod: "moisturizer" recalled 48 category rows, 37 of them one
    # brand's recently ingested creams, and the route's category-word filter
    # then served 3 products. Rows whose own text names the query must win the
    # candidate slots ahead of mere recency within the category.
    for index in range(210):
        key = f"zeta_{index}"
        title = f"Zeta Glow Cream {index}"
        catalog.insert("catalog_products", {
            "product_key": key, "pivota_signature_id": "sig_test_" + key, "source_product_id": key,
            "title": title, "description": title, "brand": "Zeta", "product_type": "cream",
            "category": "cream", "merchant_id": "test_owner", "sync_status": "live",
            "category_path": "beauty/skincare/moisturize/cream", "updated_at": "2026-10-05T00:00:00",
            "catalog_track": "external_referral", "truth_tier": "primary", "readiness_tier": "knowledge_ready",
            "canonical_url": "https://merchant.example/test/" + key, "image_url": "https://merchant.example/test.png",
            "pdp_scope": "multi_merchant_canonical", "pdp_lifecycle_stage": "published",
        })
        catalog.insert("catalog_skus", {"sku_key": key, "product_key": key, "title": title, "sku": key,
            "source_variant_id": "variant_" + key, "updated_at": "2026-10-05T00:00:00",
            "visible_attributes": json.dumps({}), "visible_option_labels": "[]", "ingredient_ids": "[]"})
        catalog.insert("catalog_offers", {"offer_id": "offer_" + key, "sku_key": key, "merchant_id": "test_seller",
            "catalog_track": "external_referral", "truth_tier": "primary", "readiness_tier": "knowledge_ready",
            "offer_mode": "external_redirect", "availability": "in_stock", "inventory_quantity": 2,
            "currency": "USD", "list_price": 20, "merchant_effective_price": 20,
            "estimated_best_price": 20, "price_confidence": 1, "market": "US"})
    result = await run_query("moisturizers")
    served = {p["product_id"] for p in result["products"]}
    # Every older product whose title says "moisturizer" is still served.
    assert {"sig_test_cheap", "sig_test_mid", "sig_test_boundary", "sig_test_expensive"} <= served
