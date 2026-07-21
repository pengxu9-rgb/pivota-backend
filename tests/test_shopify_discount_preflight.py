import pytest

from scripts.preflight_shopify_discounts import _baseline_result, _scenario_blocker
from scripts.validate_shopify_discounts import Scenario, _evaluate, _scenario_catalog
from services.shopify_graphql_client import ShopifyGraphQLError
from services.shopify_promotions_sync import (
    ShopifyStoreConfig,
    probe_shopify_discount_nodes_access_for_merchant,
)


def test_preflight_baseline_requires_authoritative_shipping_evidence():
    status, actual, action = _baseline_result(
        200,
        {
            "pricing": {"total": "10.00"},
            "discount_evidence": {
                "pricing_confidence": "authoritative",
                "shipping_evidence": {"status": "partial"},
            },
        },
    )

    assert status == "blocked"
    assert "shipping evidence is not authoritative" in actual
    assert "Markets" in action


def test_preflight_blocks_missing_second_combination_code():
    scenario = Scenario(
        "SFD-010",
        "combinable discounts",
        ["PIVOTA_TEST_COMBO_A", ""],
        expected="all_codes_applicable_discount",
        required_code_count=2,
    )

    assert _scenario_blocker(scenario) == "missing discount code fixtures; expected 2, got 1"


def test_validation_fails_when_combinable_fixture_rejects_one_code():
    scenario = Scenario(
        "SFD-010",
        "combinable discounts",
        ["COMBO_A", "AMOUNT10"],
        expected="all_codes_applicable_discount",
        required_code_count=2,
    )
    payload = {
        "pricing": {"discount_total": "2.90"},
        "discount_evidence": {
            "codes": [
                {"code": "COMBO_A", "applicable": False},
                {"code": "AMOUNT10", "applicable": True},
            ],
            "applications": [{"code": "AMOUNT10", "amount": "2.90"}],
        },
    }

    status, actual = _evaluate(scenario, 200, payload)

    assert status == "fail"
    assert "COMBO_A" in actual


def test_validation_requires_one_applied_and_one_rejected_for_noncombinable_conflict():
    scenario = Scenario(
        "SFD-011",
        "non-combinable conflict",
        ["NOCOMBO", "BXGY"],
        expected="conflict_recorded",
        required_code_count=2,
    )
    payload = {
        "pricing": {"discount_total": "29.00"},
        "discount_evidence": {
            "codes": [
                {"code": "NOCOMBO", "applicable": False},
                {"code": "BXGY", "applicable": True},
            ],
            "applications": [{"code": "BXGY", "amount": "29.00"}],
        },
    }

    status, actual = _evaluate(scenario, 200, payload)

    assert status == "pass"
    assert "applied=['BXGY']" in actual


def test_noncombinable_conflict_uses_bxgy_quantity(monkeypatch):
    monkeypatch.setenv("SHOPIFY_DISCOUNT_TEST_BXGY_QUANTITY", "3")
    monkeypatch.setenv("SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_CODE_A", "NOCOMBO")
    monkeypatch.setenv("SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_CODE_B", "BXGY")

    scenario = next(row for row in _scenario_catalog() if row.scenario_id == "SFD-011")

    assert scenario.quantity == 3


def test_combinable_discount_uses_fixture_quantity(monkeypatch):
    monkeypatch.setenv("SHOPIFY_DISCOUNT_TEST_COMBINABLE_CODE_A", "COMBO_A")
    monkeypatch.setenv("SHOPIFY_DISCOUNT_TEST_COMBINABLE_CODE_B", "COMBO_ORDER")
    monkeypatch.setenv("SHOPIFY_DISCOUNT_TEST_COMBINABLE_QUANTITY", "3")

    scenario = next(row for row in _scenario_catalog() if row.scenario_id == "SFD-010")

    assert scenario.quantity == 3


@pytest.mark.asyncio
async def test_discount_nodes_probe_reports_scope_blocker(monkeypatch):
    async def fake_config(_merchant_id):
        return ShopifyStoreConfig(shop_domain="example.myshopify.com", access_token="shpat_test")

    async def fake_scopes(_cfg):
        return ["read_orders", "read_products"]

    async def fake_graphql(**_kwargs):
        raise ShopifyGraphQLError(
            message="Shopify GraphQL errors",
            errors=[
                {
                    "message": "Access denied for discountNodes field.",
                    "extensions": {"code": "ACCESS_DENIED"},
                }
            ],
            request_id="req_123",
        )

    monkeypatch.setattr("services.shopify_promotions_sync.get_shopify_config_for_merchant", fake_config)
    monkeypatch.setattr("services.shopify_promotions_sync._fetch_access_scopes_for_config", fake_scopes)
    monkeypatch.setattr("services.shopify_promotions_sync.shopify_admin_graphql", fake_graphql)

    report = await probe_shopify_discount_nodes_access_for_merchant("merch_1")

    assert report["hasReadDiscountsScope"] is False
    assert report["discountNodesAccess"] == "blocked"
    assert report["errors"][0]["code"] == "ACCESS_DENIED"


@pytest.mark.asyncio
async def test_discount_nodes_probe_reports_success(monkeypatch):
    async def fake_config(_merchant_id):
        return ShopifyStoreConfig(shop_domain="example.myshopify.com", access_token="shpat_test")

    async def fake_scopes(_cfg):
        return ["read_discounts", "read_orders"]

    async def fake_graphql(**_kwargs):
        return {
            "discountNodes": {
                "nodes": [
                    {
                        "id": "gid://shopify/DiscountNode/1",
                        "discount": {"__typename": "DiscountCodeBasic", "title": "SAVE10"},
                    }
                ]
            }
        }

    monkeypatch.setattr("services.shopify_promotions_sync.get_shopify_config_for_merchant", fake_config)
    monkeypatch.setattr("services.shopify_promotions_sync._fetch_access_scopes_for_config", fake_scopes)
    monkeypatch.setattr("services.shopify_promotions_sync.shopify_admin_graphql", fake_graphql)

    report = await probe_shopify_discount_nodes_access_for_merchant("merch_1")

    assert report["hasReadDiscountsScope"] is True
    assert report["discountNodesAccess"] == "ok"
    assert report["sampleNodeCount"] == 1
    assert report["sampleTypenames"] == ["DiscountCodeBasic"]
