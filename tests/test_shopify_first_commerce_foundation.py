from __future__ import annotations

from datetime import datetime, timezone
import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.standard_product import StandardProduct
from readiness.models import (
    CapabilityStatus,
    ChannelReadinessReport,
    MerchantReadinessSnapshot,
    ReadyProduct,
    ReadyVariant,
)


def test_make_canonical_ids_are_stable() -> None:
    from services.canonical_commerce_service import (
        make_canonical_product_id,
        make_canonical_variant_id,
    )

    first_product = make_canonical_product_id("merch_1", "shopify", "prod_1")
    second_product = make_canonical_product_id("merch_1", "shopify", "prod_1")
    first_variant = make_canonical_variant_id("merch_1", "shopify", "prod_1", "var_1")
    second_variant = make_canonical_variant_id("merch_1", "shopify", "prod_1", "var_1")

    assert first_product == second_product
    assert first_variant == second_variant
    assert first_product.startswith("cp_")
    assert first_variant.startswith("cv_")


def test_standard_product_from_record_payload_accepts_standard_product() -> None:
    from services.canonical_commerce_service import standard_product_from_record_payload

    payload = {
        "id": "prod_1",
        "platform": "shopify",
        "merchant_id": "merch_1",
        "title": "Cleanser",
        "price": 19.0,
        "currency": "USD",
        "variants": [],
    }

    product = standard_product_from_record_payload("merch_1", payload)

    assert isinstance(product, StandardProduct)
    assert product.id == "prod_1"
    assert product.title == "Cleanser"


@pytest.mark.asyncio
async def test_fetch_listing_rows_with_catalog_fallback_uses_catalog_rows_when_registry_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.merchant_catalog_listing_fallback_service as module
    from services.canonical_commerce_service import (
        make_canonical_product_id,
        make_canonical_variant_id,
    )

    class FakeDB:
        async def fetch_all(self, query, params=None):
            rendered = str(query)
            if "surface_listing_states" in rendered:
                return []
            return [
                {
                    "product_key": "prod::merch_1::wix::prod_1",
                    "sku_key": "sku::prod::merch_1::wix::var_1",
                    "source_product_id": "prod_1",
                    "source_variant_id": "var_1",
                    "channel": "default",
                    "platform": "wix",
                    "offer_id": "offer_1",
                    "updated_at": "2026-03-30T00:00:00Z",
                }
            ]

    monkeypatch.setattr(module, "database", FakeDB())

    rows = await module.fetch_listing_rows_with_catalog_fallback("merch_1")

    assert len(rows) == 1
    assert rows[0]["status"] == "indexed"
    assert rows[0]["surface"] == "default"
    assert rows[0]["canonical_product_id"] == make_canonical_product_id("merch_1", "wix", "prod_1")
    assert rows[0]["canonical_variant_id"] == make_canonical_variant_id("merch_1", "wix", "prod_1", "var_1")
    assert rows[0]["metadata"]["source"] == "catalog_offer_fallback"
    assert rows[0]["metadata"]["catalog_product_key"] == "prod::merch_1::wix::prod_1"
    assert rows[0]["metadata"]["catalog_sku_key"] == "sku::prod::merch_1::wix::var_1"


def test_model_dump_normalizes_datetimes_for_json_storage() -> None:
    from services.canonical_commerce_service import _model_dump

    product = StandardProduct(
        id="prod_1",
        platform="shopify",
        merchant_id="merch_1",
        title="Cleanser",
        price=19.0,
        currency="USD",
        created_at=datetime(2026, 3, 30, 8, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 3, 30, 9, 0, tzinfo=timezone.utc),
        variants=[],
    )

    payload = _model_dump(product)

    assert payload["created_at"] == "2026-03-30T08:00:00+00:00"
    assert payload["updated_at"] == "2026-03-30T09:00:00+00:00"
    json.dumps(payload)


@pytest.mark.asyncio
async def test_resolve_outbound_link_includes_pvt_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.outbound_links_service as module

    async def fake_select_best_rule(**_kwargs):
        return {
            "id": "rule_1",
            "destination_url": "https://example.com/products/cleanser",
            "purchase_enabled_override": None,
            "disclosure_text": None,
            "partner_type": "partner",
            "utm_template": None,
        }

    async def fake_is_domain_allowed(**_kwargs):
        return True

    monkeypatch.setattr(module, "_select_best_rule", fake_select_best_rule)
    monkeypatch.setattr(module, "_is_domain_allowed", fake_is_domain_allowed)

    resolved = await module.resolve_outbound_link(
        {
            "market": "US",
            "tool": "ucp",
            "candidates": {"skuId": "sku_123"},
            "context": {
                "merchantId": "merch_1",
                "platform": "shopify",
                "platform_product_id": "prod_1",
                "platform_variant_id": "var_1",
                "promptCluster": "hydration",
                "surface": "ucp",
                "source": "shopping-agent-ui",
                "query_source": "cache_multi_intent",
                "protocol_name": "ucp",
                "agent_id": "agent_123",
            },
        },
        request_base_url="https://api.example.com",
    )

    parsed = urlparse(resolved.destination_url)
    qs = parse_qs(parsed.query)
    assert qs["pvt_surface"] == ["ucp"]
    assert "pvt_click_id" in qs
    assert "pvt_product_id" in qs
    assert "pvt_variant_id" in qs
    assert qs["pvt_prompt_cluster"] == ["hydration"]

    token = parse_qs(urlparse(resolved.redirect_url).query)["token"][0]
    payload = module.parse_and_verify_redirect_token(token)
    ctx = payload["ctx"]
    assert ctx["pvt_surface"] == "ucp"
    assert ctx["pvt_prompt_cluster"] == "hydration"
    assert ctx["source_channel"] == "shopping-agent-ui"
    assert ctx["query_source"] == "cache_multi_intent"
    assert ctx["protocol_name"] == "ucp"
    assert ctx["agent_id"] == "agent_123"


@pytest.mark.asyncio
async def test_persist_channel_export_records_ready_and_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.surface_listing_registry_service as module

    executed = []
    state_updates = []
    event_calls = []

    class FakeDB:
        async def fetch_one(self, query):
            return None

        async def execute(self, query):
            executed.append(query)
            return None

    async def fake_record_commerce_event_best_effort(**kwargs):
        event_calls.append(kwargs)
        return {"interaction_id": f"int_{kwargs['event_type']}"}

    async def fake_upsert_listing_state(**kwargs):
        state_updates.append(kwargs)
        return None

    monkeypatch.setattr(module, "database", FakeDB())
    monkeypatch.setattr(module, "record_commerce_event_best_effort", fake_record_commerce_event_best_effort)
    monkeypatch.setattr(module, "_upsert_listing_state", fake_upsert_listing_state)

    snapshot = MerchantReadinessSnapshot(
        merchant_id="merch_1",
        merchant_name="Merchant One",
        channel="ucp",
        generated_at="2026-03-30T00:00:00Z",
        readiness_score=80,
        products=[
            ReadyProduct(
                product_id="prod_1",
                platform="shopify",
                title="Cleanser",
                variants=[
                    ReadyVariant(
                        variant_id="var_ready",
                        title="Default",
                        discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                        checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                        channel_coverage={"ucp": "ready"},
                        blockers={},
                        warnings={},
                    ),
                    ReadyVariant(
                        variant_id="var_blocked",
                        title="Blocked",
                        discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                        checkout=CapabilityStatus(
                            capability="checkout",
                            status="blocked",
                            score=0,
                            blockers=["out_of_stock", "missing_price"],
                            warnings=["inventory_stale"],
                        ),
                        channel_coverage={"ucp": "blocked"},
                        blockers={},
                        warnings={},
                    ),
                ],
            )
        ],
    )
    report = ChannelReadinessReport(
        merchant_id="merch_1",
        channel="ucp",
        generated_at="2026-03-30T00:00:00Z",
        readiness_score=80,
        offers=[
            {
                "offer_id": "ucp:merch_1:prod_1:var_ready",
                "product_id": "prod_1",
                "variant_id": "var_ready",
                "availability": "in_stock",
            }
        ],
    )

    result = await module.persist_channel_export(snapshot, report)

    assert result == {"exported": 1, "blocked": 1, "errors": 0}
    assert len(executed) == 2
    assert len(state_updates) == 2

    ready_state = next(item for item in state_updates if item["status"] == "exported")
    blocked_state = next(item for item in state_updates if item["status"] == "blocked")
    assert ready_state["metadata"]["readiness_blockers"] == []
    assert blocked_state["metadata"]["readiness_blockers"] == ["out_of_stock", "missing_price"]
    assert blocked_state["metadata"]["readiness_warnings"] == ["inventory_stale"]
    assert blocked_state["metadata"]["offer_id"] is None

    blocked_event = next(item for item in event_calls if item["event_type"] == "listing.blocked")
    assert blocked_event["metadata"]["error_code"] == "out_of_stock"
    assert blocked_event["metadata"]["error_message"] == "out_of_stock, missing_price"


@pytest.mark.asyncio
async def test_get_merchant_commerce_funnel_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.merchant_commerce_funnel_service as module

    async def fake_fetch_listing_rows(merchant_id: str, surface: str | None):
        assert merchant_id == "merch_1"
        return [
            {"canonical_variant_id": "cv_1", "status": "exported", "canonical_product_id": "cp_1", "surface": "ucp"},
            {"canonical_variant_id": "cv_2", "status": "blocked", "canonical_product_id": "cp_2", "surface": "ucp"},
            {"canonical_variant_id": "cv_1", "status": "exported", "canonical_product_id": "cp_1", "surface": "acp"},
        ]

    async def fake_fetch_click_rows(merchant_id: str, surface: str | None):
        return [
            {"click_id": "clk_shadow", "click_count": 0, "canonical_product_id": "cp_1", "canonical_variant_id": "cv_1", "surface": "ucp"},
            {"click_id": "clk_1", "click_count": 2, "canonical_product_id": "cp_1", "canonical_variant_id": "cv_1", "surface": "ucp"},
        ]

    async def fake_fetch_edge_rows(merchant_id: str, surface: str | None):
        return [
            {
                "order_id": "ORD_1",
                "canonical_product_id": "cp_1",
                "canonical_variant_id": "cv_1",
                "surface": "ucp",
                "latest_refund_id": "REF_1",
                "refunded_amount": "1.00",
            }
        ]

    async def fake_fetch_order_rows(merchant_id: str, order_ids: list[str]):
        assert merchant_id == "merch_1"
        assert order_ids == ["ORD_1"]
        return [{"order_id": "ORD_1", "payment_status": "paid", "status": "pending"}]

    monkeypatch.setattr(module, "_fetch_listing_rows", fake_fetch_listing_rows)
    monkeypatch.setattr(module, "_fetch_click_rows", fake_fetch_click_rows)
    monkeypatch.setattr(module, "_fetch_edge_rows", fake_fetch_edge_rows)
    monkeypatch.setattr(module, "_fetch_order_rows", fake_fetch_order_rows)

    funnel = await module.get_merchant_commerce_funnel(
        merchant_id="merch_1",
        surface="ucp",
        group_by="product",
    )

    assert funnel["summary"]["indexed_exposure"] == 1
    assert funnel["summary"]["clicked_exposure"] == 1
    assert funnel["summary"]["clicked_events_total"] == 2
    assert funnel["summary"]["ordered_conversion"] == 1
    assert funnel["summary"]["paid_conversion"] == 1
    assert funnel["summary"]["paid_order_rate"] == 1
    assert funnel["summary"]["refunded_orders"] == 1
    assert funnel["summary"]["refunded_amount"] == "1.00"


@pytest.mark.asyncio
async def test_get_merchant_commerce_funnel_groups_click_aliases_into_catalog_fallback_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_commerce_funnel_service as module
    from services.canonical_commerce_service import (
        make_canonical_product_id,
        make_canonical_variant_id,
    )

    short_product_id = make_canonical_product_id("merch_1", "wix", "prod_1")
    short_variant_id = make_canonical_variant_id("merch_1", "wix", "prod_1", "var_1")
    long_product_id = "prod::merch_1::wix::prod_1"
    long_variant_id = "sku::prod::merch_1::wix::prod_1::var_1"

    async def fake_fetch_listing_rows(merchant_id: str, surface: str | None):
        assert merchant_id == "merch_1"
        return [
            {
                "canonical_product_id": short_product_id,
                "canonical_variant_id": short_variant_id,
                "status": "indexed",
                "surface": "default",
                "metadata": {
                    "source": "catalog_offer_fallback",
                    "catalog_product_key": long_product_id,
                    "catalog_sku_key": long_variant_id,
                },
            }
        ]

    async def fake_fetch_click_rows(merchant_id: str, surface: str | None):
        return [
            {
                "click_id": "clk_1",
                "click_count": 1,
                "impression_count": 1,
                "canonical_product_id": long_product_id,
                "canonical_variant_id": long_variant_id,
                "surface": "ucp",
            }
        ]

    async def fake_fetch_edge_rows(merchant_id: str, surface: str | None):
        return [
            {
                "order_id": "ORD_1",
                "canonical_product_id": long_product_id,
                "canonical_variant_id": long_variant_id,
                "surface": "ucp",
                "latest_refund_id": None,
                "refunded_amount": "0",
            }
        ]

    async def fake_fetch_order_rows(merchant_id: str, order_ids: list[str]):
        return [{"order_id": "ORD_1", "payment_status": "paid", "status": "pending"}]

    monkeypatch.setattr(module, "_fetch_listing_rows", fake_fetch_listing_rows)
    monkeypatch.setattr(module, "_fetch_click_rows", fake_fetch_click_rows)
    monkeypatch.setattr(module, "_fetch_edge_rows", fake_fetch_edge_rows)
    monkeypatch.setattr(module, "_fetch_order_rows", fake_fetch_order_rows)

    funnel = await module.get_merchant_commerce_funnel(
        merchant_id="merch_1",
        group_by="product",
    )

    assert funnel["summary"]["surfaced_exposure"] == 1
    assert funnel["summary"]["clicked_exposure"] == 1
    assert funnel["summary"]["ordered_conversion"] == 1
    assert funnel["summary"]["paid_conversion"] == 1
    assert len(funnel["slices"]) == 1
    assert funnel["slices"][0]["key"] == short_product_id
    assert funnel["slices"][0]["indexed_exposure"] == 1
    assert funnel["slices"][0]["surfaced_exposure"] == 1
    assert funnel["slices"][0]["clicked_exposure"] == 1
    assert funnel["slices"][0]["ordered_conversion"] == 1
    assert funnel["summary"]["listing_rows_total"] == 1
    assert funnel["summary"]["listing_status_breakdown_rows"] == {"indexed": 1}
    assert funnel["summary"]["listing_status_breakdown_by_surface"] == {
        "default": {"indexed": 1},
    }

    slices = {row["key"]: row for row in funnel["slices"]}
    assert slices[short_product_id]["indexed_exposure"] == 1
    assert slices[short_product_id]["listing_rows_total"] == 1
    assert slices[short_product_id]["listing_status_breakdown_rows"] == {"indexed": 1}
    assert slices[short_product_id]["listing_status_breakdown_by_surface"] == {
        "default": {"indexed": 1},
    }


async def test_fetch_click_rows_supports_row_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.merchant_commerce_funnel_service as module

    class FakeRow:
        def __init__(self, payload):
            self._mapping = payload

    class FakeDB:
        async def fetch_all(self, query):
            return [FakeRow({"click_id": "clk_1", "surface": "ucp"})]

    monkeypatch.setattr(module, "database", FakeDB())

    rows = await module._fetch_click_rows("merch_1", "ucp")

    assert rows == [{"click_id": "clk_1", "surface": "ucp"}]


@pytest.mark.asyncio
async def test_get_merchant_commerce_funnel_supports_taxonomy_grouping_and_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_commerce_funnel_service as module

    async def fake_fetch_listing_rows(merchant_id: str, surface: str | None):
        assert merchant_id == "merch_1"
        assert surface == "agent_api"
        return [{"canonical_variant_id": "cv_1", "status": "indexed", "canonical_product_id": "cp_1", "surface": "agent_api"}]

    async def fake_fetch_click_rows(merchant_id: str, surface: str | None):
        return [
            {
                "click_id": "clk_1",
                "click_count": 1,
                "impression_count": 1,
                "surface": "agent_api",
                "commerce_surface": "agent_api",
                "source_channel": "shopping-agent-ui",
                "protocol_name": "rest",
                "query_source": "cache_multi_intent",
            },
            {
                "click_id": "clk_2",
                "click_count": 1,
                "impression_count": 1,
                "surface": "agent_api",
                "commerce_surface": "agent_api",
                "source_channel": "partner-foo",
                "protocol_name": "mcp",
                "query_source": "pivot_semantic_core_multi",
            },
        ]

    async def fake_fetch_edge_rows(merchant_id: str, surface: str | None):
        return [
            {
                "order_id": "ORD_1",
                "surface": "agent_api",
                "commerce_surface": "agent_api",
                "source_channel": "partner-foo",
                "protocol_name": "mcp",
                "query_source": "pivot_semantic_core_multi",
                "refunded_amount": "0",
            }
        ]

    async def fake_fetch_order_rows(merchant_id: str, order_ids: list[str]):
        return [{"order_id": "ORD_1", "payment_status": "paid", "status": "pending"}]

    monkeypatch.setattr(module, "_fetch_listing_rows", fake_fetch_listing_rows)
    monkeypatch.setattr(module, "_fetch_click_rows", fake_fetch_click_rows)
    monkeypatch.setattr(module, "_fetch_edge_rows", fake_fetch_edge_rows)
    monkeypatch.setattr(module, "_fetch_order_rows", fake_fetch_order_rows)

    funnel = await module.get_merchant_commerce_funnel(
        merchant_id="merch_1",
        group_by="source_channel",
        protocol_name="mcp",
        commerce_surface="agent_api",
    )

    assert funnel["summary"]["clicked_exposure"] == 1
    assert funnel["summary"]["ordered_conversion"] == 1
    assert funnel["summary"]["paid_conversion"] == 1
    assert funnel["applied_filters"]["protocol_name"] == "mcp"
    assert funnel["slices"][0]["key"] == "partner-foo"
    assert funnel["slices"][0]["clicked_exposure"] == 1
    assert funnel["slices"][0]["ordered_conversion"] == 1
    assert funnel["slices"][0]["paid_conversion"] == 1


def test_merchant_analytics_route_exposes_commerce_funnel(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_analytics_routes as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_principal():
        return {"merchant_id": "merch_1", "role": "merchant"}

    async def fake_funnel(**kwargs):
        assert kwargs["merchant_id"] == "merch_1"
        assert kwargs["group_by"] == "source_channel"
        assert kwargs["source_channel"] == "shopping-agent-ui"
        assert kwargs["query_source"] == "cache_multi_intent"
        assert kwargs["platform"] == "cafe24"
        assert kwargs["store_id"] == "store_cafe"
        return {"merchant_id": "merch_1", "summary": {"indexed_exposure": 3}, "slices": []}

    app.dependency_overrides[module._get_principal] = fake_principal
    monkeypatch.setattr(module, "get_merchant_commerce_funnel", fake_funnel)

    client = TestClient(app)
    response = client.get(
        "/merchant/analytics/commerce-funnel",
        params={
            "group_by": "source_channel",
            "source_channel": "shopping-agent-ui",
            "query_source": "cache_multi_intent",
            "platform": "cafe24",
            "store_id": "store_cafe",
        },
    )

    assert response.status_code == 200
    assert response.json()["summary"]["indexed_exposure"] == 3
