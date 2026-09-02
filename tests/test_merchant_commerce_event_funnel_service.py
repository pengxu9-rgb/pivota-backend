from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import databases
import pytest
from sqlalchemy import create_engine

from db.commerce_interactions import commerce_interaction_events
from db.database import metadata


REPO_ROOT = Path(__file__).resolve().parents[1]


def _event(
    event_id: str,
    interaction_id: str,
    event_type: str,
    *,
    platform: str,
    store_id: str,
    payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "interaction_id": interaction_id,
        "merchant_id": "merch_1",
        "platform": platform,
        "store_id": store_id,
        "surface": "merchant_storefront",
        "event_type": event_type,
        "payload": payload or {},
    }


def test_funnel_indexes_roll_out_concurrently_outside_startup_guard() -> None:
    migration = (REPO_ROOT / "db/migrations/206_commerce_event_funnel_read_index.sql").read_text()
    schema_guard = (REPO_ROOT / "db/schema_guard.py").read_text()

    for index_name in (
        "idx_commerce_interaction_events_merchant_occurred",
        "idx_commerce_interaction_events_merchant_platform_occurred",
        "idx_commerce_interaction_events_merchant_store_occurred",
    ):
        assert f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name}" in migration
        assert index_name not in schema_guard


@pytest.mark.asyncio
async def test_default_funnel_excludes_ops_canary_but_explicit_surface_can_read_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.merchant_commerce_event_funnel_service as module

    db_path = tmp_path / "funnel-canary-exclusion.sqlite3"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(sync_engine, tables=[commerce_interaction_events], checkfirst=True)
    with sync_engine.begin() as connection:
        connection.execute(
            commerce_interaction_events.insert(),
            [
                {
                    "event_id": "evt_real",
                    "interaction_id": "int_real",
                    "merchant_id": "merch_1",
                    "platform": "shopify",
                    "store_id": "store_1",
                    "surface": "merchant_storefront",
                    "event_type": "payment.succeeded",
                    "occurred_at": datetime(2026, 9, 2, 12, 0),
                    "payload": {"amount_cents": 5000, "currency": "USD"},
                },
                {
                    "event_id": "evt_canary",
                    "interaction_id": "int_canary",
                    "merchant_id": "merch_1",
                    "platform": "shopify",
                    "store_id": "store_1",
                    "surface": "ops_canary",
                    "event_type": "payment.succeeded",
                    "occurred_at": datetime(2026, 9, 2, 12, 1),
                    "payload": {"amount_cents": 100, "currency": "USD"},
                },
            ],
        )
    sync_engine.dispose()

    test_database = databases.Database(f"sqlite+aiosqlite:///{db_path}")
    await test_database.connect()
    monkeypatch.setattr(module, "database", test_database)
    try:
        default_result = await module.get_merchant_commerce_event_funnel(
            merchant_id="merch_1", group_by="store"
        )
        canary_result = await module.get_merchant_commerce_event_funnel(
            merchant_id="merch_1", group_by="store", surface="ops_canary"
        )

        assert default_result.payload["summary"]["events_total"] == 1
        assert default_result.payload["summary"]["paid_amount_cents_by_currency"] == {
            "USD": 5000
        }
        assert canary_result.payload["summary"]["events_total"] == 1
        assert canary_result.payload["summary"]["paid_amount_cents_by_currency"] == {
            "USD": 100
        }
    finally:
        await test_database.disconnect()


@pytest.fixture
def ledger_rows() -> List[Dict[str, Any]]:
    return [
        _event(
            "evt_1",
            "int_a",
            "product.viewed",
            platform="cafe24",
            store_id="store_cafe",
            payload={"canonical_product_id": "cp_1", "source_channel": "chatgpt"},
        ),
        _event(
            "evt_2",
            "int_a",
            "cart.item_added",
            platform="cafe24",
            store_id="store_cafe",
            payload={"order_id": "ORDER_A", "source_channel": "chatgpt"},
        ),
        _event(
            "evt_3",
            "int_a",
            "order.created",
            platform="cafe24",
            store_id="store_cafe",
            payload={"order_id": "ORDER_A", "amount_cents": 1000, "currency": "KRW"},
        ),
        _event(
            "evt_4",
            "int_a",
            "order.paid",
            platform="cafe24",
            store_id="store_cafe",
            payload={"order_id": "ORDER_A", "amount_cents": 1000, "currency": "KRW"},
        ),
        _event(
            "evt_5",
            "int_a",
            "payment.succeeded",
            platform="cafe24",
            store_id="store_cafe",
            payload={"order_id": "ORDER_A", "amount_cents": 1000, "currency": "KRW"},
        ),
        _event(
            "evt_6",
            "int_b",
            "order.paid",
            platform="woocommerce",
            store_id="store_woo",
            payload={"order_id": "ORDER_B", "amount_cents": 2500, "currency": "USD"},
        ),
        _event(
            "evt_7",
            "int_a",
            "refund.succeeded",
            platform="cafe24",
            store_id="store_cafe",
            payload={
                "order_id": "ORDER_A",
                "refund_id": "REFUND_A",
                "amount_cents": 200,
                "currency": "KRW",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_event_funnel_aggregates_stitched_stages_platforms_and_amounts(
    monkeypatch: pytest.MonkeyPatch,
    ledger_rows: List[Dict[str, Any]],
) -> None:
    import services.merchant_commerce_event_funnel_service as module

    async def fake_fetch(**kwargs):
        assert kwargs["merchant_id"] == "merch_1"
        return ledger_rows, False

    monkeypatch.setattr(module, "_fetch_event_rows", fake_fetch)
    result = await module.get_merchant_commerce_event_funnel(
        merchant_id="merch_1",
        group_by="platform",
    )

    summary = result.payload["summary"]
    assert summary["events_total"] == 7
    assert summary["interactions_total"] == 2
    assert summary["stages"]["product_viewed"] == 1
    assert summary["stages"]["cart_active"] == 1
    assert summary["stages"]["order_created"] == 2
    assert summary["stages"]["paid"] == 2
    assert summary["stages"]["refunded"] == 1
    assert summary["platform_breakdown"] == {"cafe24": 6, "woocommerce": 1}
    # order.paid + payment.succeeded describe the same stitched purchase.
    assert summary["paid_amount_cents_by_currency"] == {"KRW": 1000, "USD": 2500}
    assert summary["refunded_amount_cents_by_currency"] == {"KRW": 200}
    assert result.order_keys == {
        ("cafe24", "store_cafe", "ORDER_A"),
        ("woocommerce", "store_woo", "ORDER_B"),
    }
    assert result.paid_keys == {
        ("cafe24", "store_cafe", "ORDER_A"),
        ("woocommerce", "store_woo", "ORDER_B"),
    }
    assert result.refund_keys == {("cafe24", "store_cafe", "ORDER_A")}
    assert [row["key"] for row in result.payload["slices"]] == ["cafe24", "woocommerce"]


@pytest.mark.asyncio
async def test_event_funnel_applies_platform_and_taxonomy_filters(
    monkeypatch: pytest.MonkeyPatch,
    ledger_rows: List[Dict[str, Any]],
) -> None:
    import services.merchant_commerce_event_funnel_service as module

    async def fake_fetch(**kwargs):
        assert kwargs["platform"] == "cafe24"
        return ledger_rows, True

    monkeypatch.setattr(module, "_fetch_event_rows", fake_fetch)
    result = await module.get_merchant_commerce_event_funnel(
        merchant_id="merch_1",
        group_by="store",
        platform="cafe24",
        source_channel="chatgpt",
    )

    assert result.payload["truncated"] is True
    assert result.payload["available"] is True
    assert result.payload["summary"]["events_total"] == 2
    assert result.payload["summary"]["platform_breakdown"] == {"cafe24": 2}
    assert [row["key"] for row in result.payload["slices"]] == ["store_cafe"]


@pytest.mark.asyncio
async def test_event_funnel_reports_event_store_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_commerce_event_funnel_service as module

    async def unavailable(**_kwargs):
        raise RuntimeError("schema rollout in progress")

    monkeypatch.setattr(module, "_fetch_event_rows", unavailable)
    result = await module.get_merchant_commerce_event_funnel(
        merchant_id="merch_1",
        group_by="platform",
    )

    assert result.payload["available"] is False
    assert result.payload["unavailable_reason"] == "canonical_event_store_unavailable"
    assert result.payload["summary"]["events_total"] == 0


@pytest.mark.asyncio
async def test_paid_amounts_keep_distinct_orders_within_one_interaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_commerce_event_funnel_service as module

    rows = [
        _event(
            "evt_order_a",
            "int_shared",
            "order.paid",
            platform="custom",
            store_id="store_1",
            payload={"order_id": "ORDER_A", "amount_cents": 1000, "currency": "USD"},
        ),
        _event(
            "evt_order_b",
            "int_shared",
            "order.paid",
            platform="custom",
            store_id="store_1",
            payload={"order_id": "ORDER_B", "amount_cents": 2500, "currency": "USD"},
        ),
    ]

    async def fake_fetch(**_kwargs):
        return rows, False

    monkeypatch.setattr(module, "_fetch_event_rows", fake_fetch)
    result = await module.get_merchant_commerce_event_funnel(
        merchant_id="merch_1",
        group_by="store",
    )

    assert result.payload["summary"]["paid_amount_cents_by_currency"] == {"USD": 3500}
    assert result.paid_keys == {
        ("custom", "store_1", "ORDER_A"),
        ("custom", "store_1", "ORDER_B"),
    }


@pytest.mark.asyncio
async def test_same_native_order_id_is_distinct_across_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_commerce_event_funnel_service as module

    rows = [
        _event(
            "evt_store_a",
            "int_store_a",
            "order.paid",
            platform="cafe24",
            store_id="store_a",
            payload={"order_id": "100", "amount_cents": 100, "currency": "USD"},
        ),
        _event(
            "evt_store_b",
            "int_store_b",
            "order.paid",
            platform="woocommerce",
            store_id="store_b",
            payload={"order_id": "100", "amount_cents": 200, "currency": "USD"},
        ),
        _event(
            "evt_refund_store_a",
            "int_store_a",
            "refund.succeeded",
            platform="cafe24",
            store_id="store_a",
            payload={
                "order_id": "100",
                "refund_id": "123",
                "amount_cents": 10,
                "currency": "USD",
            },
        ),
        _event(
            "evt_refund_store_b",
            "int_store_b",
            "refund.succeeded",
            platform="woocommerce",
            store_id="store_b",
            payload={
                "order_id": "100",
                "refund_id": "123",
                "amount_cents": 20,
                "currency": "USD",
            },
        ),
    ]

    async def fake_fetch(**_kwargs):
        return rows, False

    monkeypatch.setattr(module, "_fetch_event_rows", fake_fetch)
    result = await module.get_merchant_commerce_event_funnel(
        merchant_id="merch_1",
        group_by="platform",
    )

    assert len(result.order_keys) == 2
    assert len(result.paid_keys) == 2
    assert result.order_ids == {"100"}
    assert result.payload["summary"]["paid_amount_cents_by_currency"] == {"USD": 300}
    assert len(result.refund_keys) == 2
    assert result.payload["summary"]["refunded_amount_cents_by_currency"] == {"USD": 30}


@pytest.mark.asyncio
async def test_refund_amount_uses_largest_authority_total_per_order(monkeypatch):
    import services.merchant_commerce_event_funnel_service as module

    rows = [
        _event(
            "evt_psp_a",
            "int_order",
            "refund.succeeded",
            platform="shopify",
            store_id="store_1",
            payload={
                "order_id": "ORDER_1",
                "refund_id": "re_psp_a",
                "amount_cents": 300,
                "currency": "USD",
            },
        ),
        _event(
            "evt_psp_b",
            "int_order",
            "refund.succeeded",
            platform="shopify",
            store_id="store_1",
            payload={
                "order_id": "ORDER_1",
                "refund_id": "re_psp_b",
                "amount_cents": 200,
                "currency": "USD",
            },
        ),
        _event(
            "evt_store",
            "int_order",
            "refund.succeeded",
            platform="shopify",
            store_id="store_1",
            payload={
                "order_id": "ORDER_1",
                "refund_id": "shopify_refund_unrelated_id",
                "amount_cents": 500,
                "currency": "USD",
            },
        ),
    ]
    rows[0]["surface"] = "psp"
    rows[0]["source"] = "stripe_webhook"
    rows[1]["surface"] = "psp"
    rows[1]["source"] = "stripe_webhook"
    rows[2]["source"] = "shopify_webhook"

    async def fake_fetch(**_kwargs):
        return rows, False

    monkeypatch.setattr(module, "_fetch_event_rows", fake_fetch)
    result = await module.get_merchant_commerce_event_funnel(
        merchant_id="merch_1",
        group_by="store",
    )

    assert result.payload["summary"]["refunded_amount_cents_by_currency"] == {"USD": 500}


@pytest.mark.asyncio
async def test_commerce_surface_does_not_pre_filter_physical_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_commerce_event_funnel_service as module

    row = _event(
        "evt_surface",
        "int_surface",
        "product.viewed",
        platform="custom",
        store_id="store_1",
        payload={"commerce_surface": "agent_api"},
    )

    async def fake_fetch(**kwargs):
        assert kwargs["surface"] is None
        return [row], False

    monkeypatch.setattr(module, "_fetch_event_rows", fake_fetch)
    result = await module.get_merchant_commerce_event_funnel(
        merchant_id="merch_1",
        group_by="commerce_surface",
        commerce_surface="agent_api",
    )

    assert result.payload["summary"]["events_total"] == 1
    assert [item["key"] for item in result.payload["slices"]] == ["agent_api"]


@pytest.mark.asyncio
async def test_refund_created_is_not_counted_as_refunded_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_commerce_event_funnel_service as module

    row = _event(
        "evt_refund_requested",
        "int_refund",
        "refund.created",
        platform="custom",
        store_id="store_1",
        payload={"order_id": "ORDER_A", "refund_id": "REFUND_A"},
    )

    async def fake_fetch(**_kwargs):
        return [row], False

    monkeypatch.setattr(module, "_fetch_event_rows", fake_fetch)
    result = await module.get_merchant_commerce_event_funnel(
        merchant_id="merch_1",
        group_by="store",
    )

    assert result.payload["summary"]["stages"]["refund_active"] == 1
    assert result.payload["summary"]["stages"].get("refunded", 0) == 0
    assert result.refund_keys == set()


@pytest.mark.asyncio
async def test_existing_funnel_exposes_event_only_platform_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_commerce_funnel_service as module
    from services.merchant_commerce_event_funnel_service import CommerceEventFunnelResult

    async def unexpected_legacy_read(*_args, **_kwargs):
        raise AssertionError("platform-scoped requests must not read unscoped legacy tables")

    async def event_funnel(**kwargs):
        assert kwargs["group_by"] == "platform"
        assert kwargs["platform"] == "cafe24"
        return CommerceEventFunnelResult(
            payload={
                "summary": {
                    "events_total": 4,
                    "interactions_total": 1,
                    "stages": {
                        "product_viewed": 1,
                        "cart_active": 1,
                        "order_created": 1,
                        "paid": 1,
                    },
                    "event_type_breakdown": {},
                    "platform_breakdown": {"cafe24": 4},
                    "store_breakdown": {"store_cafe": 4},
                    "paid_amount_cents_by_currency": {"KRW": 1000},
                    "refunded_amount_cents_by_currency": {},
                },
                "slices": [
                    {
                        "key": "cafe24",
                        "events_total": 4,
                        "interactions_total": 1,
                        "stages": {
                            "product_viewed": 1,
                            "cart_active": 1,
                            "order_created": 1,
                            "paid": 1,
                        },
                    }
                ],
                "truncated": False,
                "event_limit": 50000,
            },
            order_keys={("cafe24", "store_cafe", "ORDER_A")},
            paid_keys={("cafe24", "store_cafe", "ORDER_A")},
            order_ids={"ORDER_A"},
            paid_order_ids={"ORDER_A"},
        )

    monkeypatch.setattr(module, "_fetch_listing_rows", unexpected_legacy_read)
    monkeypatch.setattr(module, "_fetch_click_rows", unexpected_legacy_read)
    monkeypatch.setattr(module, "_fetch_edge_rows", unexpected_legacy_read)
    monkeypatch.setattr(module, "get_merchant_commerce_event_funnel", event_funnel)

    funnel = await module.get_merchant_commerce_funnel(
        merchant_id="merch_1",
        group_by="platform",
        platform="cafe24",
    )

    assert funnel["summary"]["ordered_conversion"] == 0
    assert funnel["summary"]["observed_order_conversion"] == 1
    assert funnel["summary"]["observed_paid_conversion"] == 1
    assert funnel["summary"]["ledger_events_total"] == 4
    assert funnel["metric_scopes"] == {
        "legacy_attribution": {
            "included": False,
            "slices_grouped": False,
            "unsupported_filters": ["platform"],
            "reason": (
                "Legacy click and attribution rows do not carry a reliable platform/store identity; "
                "legacy metrics are excluded instead of being assigned to the wrong store."
            ),
        },
        "canonical_events": {
            "included": True,
            "scoped_filters": ["platform"],
        },
    }
    assert funnel["slices"] == [
        {
            "key": "cafe24",
            "indexed_exposure": 0,
            "surfaced_exposure": 0,
            "clicked_exposure": 0,
            "clicked_events_total": 0,
            "ordered_conversion": 0,
            "attributed_orders": 0,
            "paid_conversion": 0,
            "refunded_orders": 0,
            "refunded_amount": "0",
            "clicked_rate": 0,
            "ordered_rate": 0,
            "paid_order_rate": 0,
            "listing_rows_total": 0,
            "listing_status_breakdown_rows": {},
            "listing_status_breakdown_by_surface": {},
            "observed_product_views": 1,
            "observed_cart_interactions": 1,
            "observed_checkouts": 0,
            "observed_payment_attempts": 0,
            "observed_orders": 1,
            "observed_paid_interactions": 1,
            "observed_refunds": 0,
            "event_funnel": {
                "events_total": 4,
                "interactions_total": 1,
                "stages": {
                    "product_viewed": 1,
                    "cart_active": 1,
                    "order_created": 1,
                    "paid": 1,
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_blank_platform_and_store_filters_do_not_disable_legacy_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_commerce_funnel_service as module
    from services.merchant_commerce_event_funnel_service import empty_event_funnel_result

    reads = []

    async def empty_rows(*_args, **_kwargs):
        reads.append(True)
        return []

    async def event_funnel(**kwargs):
        assert kwargs["platform"] is None
        assert kwargs["store_id"] is None
        return empty_event_funnel_result()

    monkeypatch.setattr(module, "_fetch_listing_rows", empty_rows)
    monkeypatch.setattr(module, "_fetch_click_rows", empty_rows)
    monkeypatch.setattr(module, "_fetch_edge_rows", empty_rows)
    monkeypatch.setattr(module, "get_merchant_commerce_event_funnel", event_funnel)

    funnel = await module.get_merchant_commerce_funnel(
        merchant_id="merch_1",
        platform="   ",
        store_id=" ",
    )

    assert len(reads) == 3
    assert funnel["applied_filters"] == {}
    assert funnel["metric_scopes"]["legacy_attribution"]["included"] is True


@pytest.mark.asyncio
async def test_observed_counts_preserve_store_scoped_orders_and_dedupe_legacy_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_commerce_funnel_service as module
    from services.merchant_commerce_event_funnel_service import CommerceEventFunnelResult

    async def no_rows(*_args, **_kwargs):
        return []

    async def legacy_edge_rows(*_args, **_kwargs):
        return [{"order_id": "100", "latest_refund_id": "refund_legacy"}]

    async def paid_order_rows(*_args, **_kwargs):
        return [{"order_id": "100", "payment_status": "paid", "status": "paid"}]

    async def event_funnel(**_kwargs):
        scoped_orders = {
            ("cafe24", "store_a", "100"),
            ("woocommerce", "store_b", "100"),
        }
        return CommerceEventFunnelResult(
            payload={
                "summary": {"events_total": 4, "interactions_total": 2, "stages": {}},
                "slices": [],
                "truncated": False,
                "event_limit": 50000,
                "available": True,
                "unavailable_reason": None,
            },
            order_keys=set(scoped_orders),
            paid_keys=set(scoped_orders),
            refund_keys=set(scoped_orders),
            order_ids={"100"},
            paid_order_ids={"100"},
            refund_order_ids={"100"},
        )

    monkeypatch.setattr(module, "_fetch_listing_rows", no_rows)
    monkeypatch.setattr(module, "_fetch_click_rows", no_rows)
    monkeypatch.setattr(module, "_fetch_edge_rows", legacy_edge_rows)
    monkeypatch.setattr(module, "_fetch_order_rows", paid_order_rows)
    monkeypatch.setattr(module, "get_merchant_commerce_event_funnel", event_funnel)

    funnel = await module.get_merchant_commerce_funnel(
        merchant_id="merch_1",
        group_by="platform",
    )

    assert funnel["summary"]["ordered_conversion"] == 1
    assert funnel["summary"]["observed_order_conversion"] == 2
    assert funnel["summary"]["observed_paid_conversion"] == 2
    assert funnel["summary"]["observed_refunded_orders"] == 2
