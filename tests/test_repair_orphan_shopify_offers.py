from __future__ import annotations

import argparse
from decimal import Decimal

import pytest

import scripts.repair_orphan_shopify_offers as module


def _target_row(**overrides):
    row = {
        "offer_id": "offer_1",
        "sku_key": "prod::merch_1::shopify::prod_1::v::var_1",
        "product_key": "prod::merch_1::shopify::prod_1",
        "merchant_id": "merch_1",
        "catalog_track": "internal_merchant",
        "truth_tier": "primary",
        "offer_readiness_tier": "commerce_ready",
        "offer_currency": "USD",
        "list_price": Decimal("24.00"),
        "merchant_effective_price": Decimal("20.00"),
        "offer_payload": {"product_id": "prod_1", "variant_id": "var_1", "sku": "SKU-1"},
        "source_ref": "batch_1",
        "product_platform": "shopify",
        "catalog_source_product_id": "prod_1",
        "product_title": "Vitamin C Serum",
        "product_image_url": "https://cdn.example/p.jpg",
        "product_payload": {
            "id": "prod_1",
            "platform": "shopify",
            "product_id": "prod_1",
            "title": "Vitamin C Serum",
            "currency": "USD",
            "variants": [
                {"id": "var_1", "variant_id": "var_1", "title": "30 ml", "sku": "SKU-1", "price": 20.0}
            ],
        },
        "product_readiness_tier": "commerce_ready",
        "cache_product_data": {},
    }
    row.update(overrides)
    return row


def _args(**overrides):
    values = {
        "apply": False,
        "confirm": "",
        "limit": 500,
        "sample_limit": 10,
        "batch_id": "",
        "postcheck": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_build_repair_plan_uses_offer_sku_key_and_source_variant() -> None:
    plan = module._build_repair_plan(_target_row())

    assert plan.action == "repair"
    assert plan.reason is None
    assert plan.sku_values["sku_key"] == "prod::merch_1::shopify::prod_1::v::var_1"
    assert plan.sku_values["source_variant_id"] == "var_1"
    assert plan.source_identity == (
        "merch_1",
        "shopify",
        "prod::merch_1::shopify::prod_1",
        "var_1",
    )


def test_build_repair_plan_suppresses_zero_price_without_fallback() -> None:
    plan = module._build_repair_plan(
        _target_row(list_price=Decimal("0.00"), merchant_effective_price=Decimal("20.00"))
    )

    assert plan.action == "suppress"
    assert plan.reason == module.POSITIVE_LIST_PRICE_MISSING


def test_build_repair_plan_suppresses_variant_conflict() -> None:
    plan = module._build_repair_plan(
        _target_row(
            offer_payload={"product_id": "prod_1", "variant_id": "var_missing"},
        )
    )

    assert plan.action == "suppress"
    assert plan.reason == module.SOURCE_VARIANT_ID_CONFLICT


@pytest.mark.asyncio
async def test_drive_dry_run_classifies_without_writes() -> None:
    class FakeDb:
        is_connected = True

        async def fetch_all(self, sql, values):
            assert "FROM catalog_offers o" in sql
            assert values == {"limit": 500}
            return [_target_row()]

        async def fetch_one(self, sql, values):
            if "FROM catalog_skus" in sql:
                return None
            assert "FROM catalog_offers o" in sql
            return {
                "unsuppressed_orphan_offers": 1,
                "unsuppressed_zero_or_missing_price_offers": 0,
                "suppressed_shopify_offers": 0,
            }

        async def execute(self, *_args, **_kwargs):
            raise AssertionError("dry-run must not execute writes")

    report = await module._drive(_args(), db=FakeDb())

    assert report["apply"] is False
    assert report["planned_actions"] == {"repair": 1}
    assert report["applied"]["sku_inserts_attempted"] == 0
    assert report["safety"]["deletes"] == 0


@pytest.mark.asyncio
async def test_drive_apply_inserts_sku_suppresses_unresolved_and_audits() -> None:
    calls = []

    class DummyTransaction:
        async def __aenter__(self):
            calls.append(("transaction", "enter"))

        async def __aexit__(self, exc_type, exc, tb):
            calls.append(("transaction", "exit"))
            return False

    class FakeDb:
        is_connected = True

        def transaction(self):
            return DummyTransaction()

        async def fetch_all(self, _sql, _values):
            return [
                _target_row(offer_id="offer_repair"),
                _target_row(
                    offer_id="offer_suppress",
                    sku_key="missing_sku",
                    list_price=Decimal("0.00"),
                ),
            ]

        async def fetch_one(self, sql, _values):
            if "FROM catalog_skus" in sql:
                return None
            return {
                "unsuppressed_orphan_offers": 0,
                "unsuppressed_zero_or_missing_price_offers": 0,
                "suppressed_shopify_offers": 1,
            }

        async def execute(self, sql, values):
            calls.append((str(sql), dict(values)))

    report = await module._drive(
        _args(apply=True, confirm=module.CONFIRM_TOKEN, batch_id="repair_batch"),
        db=FakeDb(),
    )

    executed_sql = "\n".join(call[0] for call in calls if isinstance(call[0], str))
    assert "INSERT INTO catalog_skus" in executed_sql
    assert "UPDATE catalog_offers" in executed_sql
    assert "INSERT INTO writer_audit_log" in executed_sql
    assert "DELETE" not in executed_sql.upper()
    assert report["applied"]["sku_inserts_attempted"] == 1
    assert report["applied"]["offers_suppressed"] == 1
    assert report["planned_suppression_reasons"] == {module.POSITIVE_LIST_PRICE_MISSING: 1}


@pytest.mark.asyncio
async def test_apply_requires_confirm_token() -> None:
    with pytest.raises(SystemExit):
        await module._drive(_args(apply=True, confirm="WRONG"), db=object())
