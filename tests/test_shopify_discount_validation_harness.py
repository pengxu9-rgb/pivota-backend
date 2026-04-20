import argparse
import json

import pytest

from scripts.validate_shopify_discounts import Scenario, _quote_request
from scripts.preflight_shopify_discounts import _scenario_blocker


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        merchant_id="merch_1",
        customer_email="buyer@example.com",
        product_id="prod_default",
        variant_id="var_default",
    )


def test_quote_request_uses_scenario_specific_items_json(monkeypatch):
    monkeypatch.setenv(
        "SHOPIFY_DISCOUNT_TEST_BXGY_ITEMS_JSON",
        json.dumps(
            [
                {"product_id": "buy_prod", "variant_id": "buy_var", "quantity": 2},
                {"product_id": "get_prod", "variant_id": "get_var", "quantity": 1},
            ]
        ),
    )

    body = _quote_request(
        _args(),
        Scenario(
            "SFD-004",
            "Buy X Get Y code",
            ["BXGY"],
            quantity=3,
            items_env="SHOPIFY_DISCOUNT_TEST_BXGY_ITEMS_JSON",
        ),
    )

    assert body["items"] == [
        {"product_id": "buy_prod", "variant_id": "buy_var", "quantity": 2},
        {"product_id": "get_prod", "variant_id": "get_var", "quantity": 1},
    ]


def test_quote_request_rejects_invalid_scenario_items_json(monkeypatch):
    monkeypatch.setenv("SHOPIFY_DISCOUNT_TEST_BXGY_ITEMS_JSON", json.dumps([{"product_id": "p"}]))

    with pytest.raises(SystemExit, match="must include product_id, variant_id, and quantity > 0"):
        _quote_request(
            _args(),
            Scenario(
                "SFD-004",
                "Buy X Get Y code",
                ["BXGY"],
                items_env="SHOPIFY_DISCOUNT_TEST_BXGY_ITEMS_JSON",
            ),
        )


def test_automatic_and_new_customer_scenarios_can_run_without_codes(monkeypatch):
    monkeypatch.setenv("SHOPIFY_DISCOUNT_TEST_AUTOMATIC_ENABLED", "1")
    assert (
        _scenario_blocker(
            Scenario(
                "SFD-003",
                "automatic amount-off discount",
                [],
                expected="automatic_discount",
                env_required="SHOPIFY_DISCOUNT_TEST_AUTOMATIC_ENABLED",
            )
        )
        is None
    )

    monkeypatch.setenv("SHOPIFY_DISCOUNT_TEST_NEW_CUSTOMER_ENABLED", "1")
    assert (
        _scenario_blocker(
            Scenario(
                "SFD-006",
                "new-customer or segment eligibility",
                [],
                expected="customer_eligibility_evidence",
                env_required="SHOPIFY_DISCOUNT_TEST_NEW_CUSTOMER_ENABLED",
            )
        )
        is None
    )
