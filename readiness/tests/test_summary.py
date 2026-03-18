from __future__ import annotations

import pytest

from readiness.models import (
    CapabilityStatus,
    ChannelCoverageStatus,
    MerchantReadinessSnapshot,
    ReadyProduct,
    ReadyVariant,
)
from readiness.summary import (
    build_readiness_optimization,
    build_readiness_summary,
    summarize_readiness_snapshot,
)


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

    assert payload.readiness_summary.tier == "red"
    assert payload.issue_buckets[0].code in {"shipping_returns_setup", "price_currency", "inventory_availability"}
    assert payload.product_queue[0].platform == "shopify"
    assert payload.product_queue[0].blocked_variant_count == 1
    assert payload.product_queue[0].top_issues
    assert payload.merchant_actions
