from __future__ import annotations

import pytest

from readiness.models import MerchantReadinessOptimizationPayload
from readiness.remediation import (
    ActionNotExecutableError,
    PlanSupersededError,
    preview_remediation_action,
    run_remediation_action,
)


def _optimization_payload(*, plan_id: str, snapshot_id: str, score: int) -> MerchantReadinessOptimizationPayload:
    return MerchantReadinessOptimizationPayload.model_validate(
        {
            "plan": {
                "plan_id": plan_id,
                "snapshot_id": snapshot_id,
                "workspace_version": "agent_commerce_optimization.v1",
                "priority_policy_version": "merchant_readiness_priority.v1",
                "refresh_state": "fresh",
                "generated_at": "2026-03-19T00:00:00Z",
                "expires_at": "2026-03-19T06:00:00Z",
                "can_apply_actions": True,
                "last_successful_rescore_at": "2026-03-19T00:00:00Z",
            },
            "score_bundle": {
                "readiness_score": score,
            },
            "readiness_summary": {
                "tier": "yellow",
                "label": "Needs Attention",
                "assessment_state": "assessed",
                "channel": "ucp",
                "score": score,
                "ready_variant_count": 3,
                "blocked_variant_count": 1,
                "generated_at": "2026-03-19T00:00:00Z",
            },
            "issue_buckets": [
                {
                    "code": "catalog_content",
                    "label": "Catalog content",
                    "severity": "medium",
                    "scope": "product",
                    "affected_count": 1,
                    "fix_surface": "product_content",
                    "fixability": "merchant_fixable",
                    "impact": "discovery_only",
                    "direct_target": "/dashboard/product-optimization?focus=catalog_content",
                    "priority_score": 120,
                    "priority_reason": "Fixing this issue can improve how agents understand and retrieve products.",
                    "reason_codes": ["missing_description"],
                }
            ],
            "merchant_actions": [],
            "product_queue": [
                {
                    "queue_item_scope": "product",
                    "queue_item_id": "product:shopify:prod_1",
                    "product_id": "prod_1",
                    "platform": "shopify",
                    "platform_product_id": "prod_1",
                    "title": "Alpha Product",
                    "blocked_variant_count": 1,
                    "ready_variant_count": 0,
                    "top_issues": [
                        {
                            "code": "missing_description",
                            "label": "Missing description",
                            "impact": "discovery_only",
                            "affected_variant_count": 1,
                        }
                    ],
                    "primary_action": "Improve the content for this product.",
                    "fix_surface": "product_content",
                    "fixability": "merchant_fixable",
                    "impact": "discovery_only",
                    "priority_score": 111,
                    "priority_reason": "Improving this product can increase agent understanding and retrieval.",
                    "recommended_action_id": "act_product:shopify:prod_1",
                    "recommended_action_type": "run_product_enrichment",
                }
            ],
            "last_generated_at": "2026-03-19T00:00:00Z",
        }
    )


@pytest.mark.asyncio
async def test_preview_remediation_action_returns_candidate_patches(monkeypatch):
    from readiness import remediation

    payload = _optimization_payload(plan_id="rdplan_current", snapshot_id="rdsnap_current", score=77)

    async def fake_build_readiness_optimization(_merchant_id: str, *, channel: str = "ucp"):
        assert channel == "ucp"
        return payload

    async def fake_get_product_cache_row(**_kwargs):
        return {
            "product_data": {
                "id": "prod_1",
                "platform": "shopify",
                "merchant_id": "merch_efbc46b4619cfbdf",
                "title": "Alpha Product",
                "description": "A helpful daily product that supports multiple usage scenarios.",
                "vendor": "Alpha Brand",
                "product_type": "Serum",
                "tags": ["hydrating"],
                "price": 19.0,
                "currency": "USD",
                "inventory_quantity": 12,
                "image_url": "https://example.com/product.jpg",
                "images": ["https://example.com/product.jpg"],
                "variants": [],
                "orderable": True,
            }
        }

    async def fake_get_enrichment(**_kwargs):
        return {}

    monkeypatch.setattr(remediation, "build_readiness_optimization", fake_build_readiness_optimization)
    monkeypatch.setattr(remediation, "get_product_cache_row", fake_get_product_cache_row)
    monkeypatch.setattr(remediation, "get_enrichment", fake_get_enrichment)

    preview = await preview_remediation_action(
        "merch_efbc46b4619cfbdf",
        plan_id="rdplan_current",
        action_id="act_product:shopify:prod_1",
    )

    assert preview["action"]["action_type"] == "run_product_enrichment"
    assert preview["candidate_patches"]
    assert preview["requires_approval"] is True
    assert preview["expected_impact"]["targets"][0]["delta"]["content_quality_score"] >= 0


@pytest.mark.asyncio
async def test_preview_remediation_action_rejects_superseded_plan(monkeypatch):
    from readiness import remediation

    payload = _optimization_payload(plan_id="rdplan_latest", snapshot_id="rdsnap_latest", score=77)

    async def fake_build_readiness_optimization(_merchant_id: str, *, channel: str = "ucp"):
        return payload

    monkeypatch.setattr(remediation, "build_readiness_optimization", fake_build_readiness_optimization)

    with pytest.raises(PlanSupersededError):
        await preview_remediation_action(
            "merch_efbc46b4619cfbdf",
            plan_id="rdplan_old",
            action_id="act_product:shopify:prod_1",
        )


@pytest.mark.asyncio
async def test_run_remediation_action_executes_pipeline_and_returns_verification(monkeypatch):
    from readiness import remediation

    before_payload = _optimization_payload(plan_id="rdplan_before", snapshot_id="rdsnap_before", score=77)
    after_payload = _optimization_payload(plan_id="rdplan_after", snapshot_id="rdsnap_after", score=82)
    after_payload.readiness_summary.score = 82
    after_payload.score_bundle.readiness_score = 82
    after_payload.readiness_summary.blocked_variant_count = 0

    call_count = {"count": 0}

    async def fake_build_readiness_optimization(_merchant_id: str, *, channel: str = "ucp"):
        call_count["count"] += 1
        return before_payload if call_count["count"] == 1 else after_payload

    async def fake_run_enrichment_for_product(**kwargs):
        assert kwargs["platform"] == "shopify"
        assert kwargs["platform_product_id"] == "prod_1"
        return {"status": "ok"}

    monkeypatch.setattr(remediation, "build_readiness_optimization", fake_build_readiness_optimization)
    monkeypatch.setattr(remediation, "run_enrichment_for_product", fake_run_enrichment_for_product)

    result = await run_remediation_action(
        "merch_efbc46b4619cfbdf",
        plan_id="rdplan_before",
        action_id="act_product:shopify:prod_1",
    )

    assert result["job"]["status"] == "completed"
    assert result["verification"]["before_snapshot_id"] == "rdsnap_before"
    assert result["verification"]["after_snapshot_id"] == "rdsnap_after"
    assert result["verification"]["delta_scores"]["readiness_score"] == 5
    assert result["after_plan"]["plan_id"] == "rdplan_after"


@pytest.mark.asyncio
async def test_run_remediation_action_rejects_non_executable_action(monkeypatch):
    from readiness import remediation

    payload = MerchantReadinessOptimizationPayload.model_validate(
        {
            "plan": {
                "plan_id": "rdplan_current",
                "snapshot_id": "rdsnap_current",
            },
            "readiness_summary": {
                "tier": "red",
                "label": "Blocked",
                "assessment_state": "assessed",
            },
            "merchant_actions": [
                {
                    "action_id": "act_review_integrations",
                    "action_type": "review_and_fix",
                    "label": "Review integrations",
                    "description": "Checkout is not connected.",
                    "target_url": "/dashboard/integrations",
                    "fix_surface": "integrations",
                    "scope": "merchant",
                    "impact": "full_agent_commerce",
                }
            ],
            "issue_buckets": [],
            "product_queue": [],
        }
    )

    async def fake_build_readiness_optimization(_merchant_id: str, *, channel: str = "ucp"):
        return payload

    monkeypatch.setattr(remediation, "build_readiness_optimization", fake_build_readiness_optimization)

    with pytest.raises(ActionNotExecutableError):
        await run_remediation_action(
            "merch_efbc46b4619cfbdf",
            plan_id="rdplan_current",
            action_id="act_review_integrations",
        )
