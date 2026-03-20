from __future__ import annotations

import pytest

from readiness.models import (
    CapabilityStatus,
    ChannelCoverageStatus,
    MerchantReadinessSnapshot,
    QualityCoverageSummary,
    ReadyProduct,
    ReadyVariant,
)
from readiness.summary import (
    build_readiness_optimization,
    build_readiness_summary,
    reset_readiness_optimization_cache_observability,
    summarize_readiness_snapshot,
)


@pytest.fixture(autouse=True)
def _reset_optimization_cache():
    reset_readiness_optimization_cache_observability()


def _snapshot(
    *,
    readiness_score: int,
    checkout_status: str = "ready",
    order_sync_status: str = "ready",
    ready_variant_count: int = 1,
    blocked_variant_count: int = 0,
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
) -> MerchantReadinessSnapshot:
    return MerchantReadinessSnapshot(
        merchant_id="merch_efbc46b4619cfbdf",
        merchant_name="Alpha Merchant",
        channel="ucp",
        generated_at="2026-03-18T00:00:00Z",
        merchant_alpha_mode="real_merchant_alpha",
        readiness_score=readiness_score,
        domain_scores={},
        capability_status={
            "checkout_execution": checkout_status,
            "order_writeback_state_sync": order_sync_status,
        },
        blockers=blockers or [],
        warnings=warnings or [],
        merchant_capabilities=[],
        channel_coverage=[
            ChannelCoverageStatus(
                channel="ucp",
                status="ready" if blocked_variant_count == 0 else "partial",
                ready_variant_count=ready_variant_count,
                blocked_variant_count=blocked_variant_count,
            )
        ],
        source_of_truth={},
        stubbed_capabilities=[],
        audit_notes=[],
        products=[],
    )


def test_summarize_readiness_snapshot_green():
    summary = summarize_readiness_snapshot(_snapshot(readiness_score=88))

    assert summary.tier == "green"
    assert summary.assessment_state == "assessed"
    assert summary.ready_variant_count == 1
    assert summary.blocked_variant_count == 0
    assert "ready for supervised LLM commerce" in str(summary.summary_text)


def test_summarize_readiness_snapshot_yellow_when_variants_blocked():
    summary = summarize_readiness_snapshot(
        _snapshot(
            readiness_score=77,
            ready_variant_count=10,
            blocked_variant_count=2,
            warnings=["inventory_snapshot_stale"],
        )
    )

    assert summary.tier == "yellow"
    assert summary.top_warnings == ["inventory_snapshot_stale"]
    assert "Resolve blocked variants" in str(summary.next_action)
    assert "Most of the catalog is usable" in str(summary.summary_text)


def test_summarize_readiness_snapshot_red_when_checkout_blocked():
    summary = summarize_readiness_snapshot(
        _snapshot(
            readiness_score=82,
            checkout_status="blocked",
            blockers=["merchant_checkout_capability_missing"],
        )
    )

    assert summary.tier == "red"
    assert summary.top_blockers == ["merchant_checkout_capability_missing"]


@pytest.mark.asyncio
async def test_build_readiness_summary_returns_not_assessed_when_non_alpha(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    summary = await build_readiness_summary("merch_not_assessed")

    assert summary.tier == "red"
    assert summary.assessment_state == "not_assessed"
    assert summary.top_blockers == ["merchant_not_assessed_for_readiness_alpha"]
    assert summary.recommended_actions


@pytest.mark.asyncio
async def test_build_readiness_optimization_returns_issue_buckets_and_product_queue(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={
                "checkout_execution": "ready",
                "order_writeback_state_sync": "ready",
            },
            blockers=["merchant_shipping_policy_missing"],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=1,
                    blocked_variant_count=1,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_1",
                    platform="shopify",
                    title="Alpha Product",
                    default_image_url="https://example.com/p.jpg",
                    variants=[
                        ReadyVariant(
                            variant_id="var_1",
                            title="Default",
                            price={"amount": 10, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": ["out_of_stock", "missing_price"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(
                                capability="checkout",
                                status="blocked",
                                score=40,
                                blockers=["out_of_stock", "missing_price"],
                            ),
                            channel_coverage={"ucp": "blocked"},
                        ),
                        ReadyVariant(
                            variant_id="var_2",
                            title="Backup",
                            price={"amount": 10, "currency": "USD"},
                            inventory={"quantity": 5, "availability": "in_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": []},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                            channel_coverage={"ucp": "ready"},
                        ),
                    ],
                )
            ],
        )

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    assert payload.plan.snapshot_id.startswith("rdsnap_")
    assert payload.plan.plan_id.startswith("rdplan_")
    assert payload.plan.workspace_version == "agent_commerce_optimization.v1"
    assert payload.plan.refresh_state in {"fresh", "stale", "expired"}
    assert payload.score_bundle.readiness_score == 77
    assert payload.readiness_summary.tier == "red"
    assert payload.issue_buckets[0].code in {"shipping_returns_setup", "price_currency", "inventory_availability"}
    assert payload.issue_buckets[0].priority_score > 0
    assert payload.product_queue[0].platform == "shopify"
    assert payload.product_queue[0].queue_item_scope == "product"
    assert payload.product_queue[0].queue_item_id.startswith("product:shopify:")
    assert payload.product_queue[0].blocked_variant_count == 0
    assert payload.product_queue[0].agent_push_status == "eligible_for_agent_push"
    assert payload.product_queue[0].eligible_variant_count == 1
    assert payload.product_queue[0].excluded_variant_count == 1
    assert payload.product_queue[0].priority_score > 0
    assert payload.product_queue[0].top_issues
    assert payload.merchant_actions
    assert payload.agent_push_summary.eligible_products == 1
    assert payload.agent_push_summary.excluded_variants == 1


@pytest.mark.asyncio
async def test_build_readiness_optimization_uses_truthful_action_mapping(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=0,
                    blocked_variant_count=2,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_text",
                    platform="shopify",
                    title="Text Fix Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_text",
                            title="Default",
                            price={"amount": 10, "currency": "USD"},
                            inventory={"quantity": 2, "availability": "in_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": ["missing_description"], "checkout": []},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(
                                capability="discovery",
                                status="blocked",
                                score=35,
                                blockers=["missing_description"],
                            ),
                            checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                ),
                ReadyProduct(
                    product_id="prod_catalog",
                    platform="shopify",
                    title="Catalog Fix Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_catalog",
                            title="Default",
                            price={"amount": None, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": ["missing_primary_image"], "checkout": ["missing_price"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(
                                capability="discovery",
                                status="blocked",
                                score=20,
                                blockers=["missing_primary_image"],
                            ),
                            checkout=CapabilityStatus(
                                capability="checkout",
                                status="blocked",
                                score=20,
                                blockers=["missing_price"],
                            ),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                ),
            ],
        )

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    queue_by_product_id = {item.product_id: item for item in payload.product_queue}

    assert queue_by_product_id["prod_text"].recommended_action_type == "run_product_enrichment"
    assert queue_by_product_id["prod_text"].fix_surface == "product_content"
    assert queue_by_product_id["prod_catalog"].recommended_action_type == "review_catalog_data"
    assert queue_by_product_id["prod_catalog"].fix_surface == "catalog_data"


@pytest.mark.asyncio
async def test_build_readiness_optimization_projects_quality_coverage(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=1,
                    blocked_variant_count=1,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_1",
                    platform="shopify",
                    title="Alpha Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_1",
                            title="Default",
                            price={"amount": 10, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": ["out_of_stock"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(capability="checkout", status="blocked", score=40),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                )
            ],
        )

    async def fake_apply_quality_projection(_merchant_id: str, *, snapshot_products, product_queue):
        product_queue[0].content_quality_score = 83.0
        product_queue[0].model_readiness_score = 71.0
        product_queue[0].quality_source = "preview"
        return product_queue, QualityCoverageSummary(
            total_products=1,
            snapshot_scored_products=0,
            effective_scored_products=1,
            preview_only_products=1,
            unscored_products=0,
            coverage_state="full",
            backfill_recommended=True,
        )

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)
    monkeypatch.setattr("readiness.summary._apply_quality_projection", fake_apply_quality_projection)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    assert payload.product_queue[0].content_quality_score == 83.0
    assert payload.product_queue[0].model_readiness_score == 71.0
    assert payload.product_queue[0].quality_source == "preview"
    assert payload.quality_coverage.effective_scored_products == 1
    assert payload.quality_coverage.preview_only_products == 1
    assert payload.quality_coverage.coverage_state == "full"
