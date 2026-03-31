from __future__ import annotations

import pytest

from readiness.models import (
    CapabilityStatus,
    ChannelCoverageStatus,
    MerchantReadinessOptimizationPayload,
    MerchantReadinessSnapshot,
    ReadyProduct,
    ReadyVariant,
)
from readiness.remediation import (
    ActionNotExecutableError,
    PlanSupersededError,
    get_product_blocker_detail,
    get_source_data_triage,
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
                    "platform_admin_url": "https://alpha-beauty-demo.myshopify.com/admin/products/prod_1",
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


def _snapshot() -> MerchantReadinessSnapshot:
    return MerchantReadinessSnapshot(
        merchant_id="merch_efbc46b4619cfbdf",
        merchant_name="Alpha Merchant",
        channel="ucp",
        generated_at="2026-03-19T00:00:00Z",
        merchant_alpha_mode="real_merchant_alpha",
        readiness_score=77,
        domain_scores={},
        capability_status={
            "checkout_execution": "ready",
            "order_writeback_state_sync": "ready",
        },
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
                default_image_url="https://example.com/product.jpg",
                variants=[
                    ReadyVariant(
                        variant_id="var_1",
                        title="Blue / Small",
                        sku="ALPHA-BL-S",
                        price={"amount": None, "currency": "USD"},
                        inventory={"quantity": 0, "availability": "out_of_stock"},
                        freshness={},
                        provenance=[],
                        source_of_truth={},
                        blockers={"discovery": [], "checkout": ["missing_price", "out_of_stock"]},
                        warnings={"discovery": [], "checkout": []},
                        discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                        checkout=CapabilityStatus(
                            capability="checkout",
                            status="blocked",
                            score=20,
                            blockers=["missing_price", "out_of_stock"],
                        ),
                        channel_coverage={"ucp": "blocked"},
                    ),
                    ReadyVariant(
                        variant_id="var_2",
                        title="Green / Medium",
                        sku="ALPHA-GR-M",
                        price={"amount": 24.0, "currency": "USD"},
                        inventory={"quantity": 5, "availability": "in_stock"},
                        freshness={},
                        provenance=[],
                        source_of_truth={},
                        blockers={"discovery": [], "checkout": []},
                        warnings={"discovery": [], "checkout": ["inventory_stale"]},
                        discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                        checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                        channel_coverage={"ucp": "ready"},
                    ),
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_preview_remediation_action_returns_candidate_patches(monkeypatch):
    from readiness import remediation

    payload = _optimization_payload(plan_id="rdplan_current", snapshot_id="rdsnap_current", score=77)
    snapshot = _snapshot()

    async def fake_get_readiness_optimization_context(_merchant_id: str, *, channel: str = "ucp"):
        assert channel == "ucp"
        return payload, snapshot

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

    monkeypatch.setattr(
        remediation,
        "get_readiness_optimization_context",
        fake_get_readiness_optimization_context,
    )
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
    assert preview["generated_content_context"][0]["title_health"] == "needs_more_facts"
    assert preview["generated_content_context"][0]["missing_attribute_labels"]
    assert not any(
        patch["target_field"] == "title_override"
        for patch in preview["candidate_patches"]
    )


@pytest.mark.asyncio
async def test_preview_remediation_action_returns_title_patch_when_facts_are_sufficient(monkeypatch):
    from readiness import remediation

    payload = _optimization_payload(plan_id="rdplan_current", snapshot_id="rdsnap_current", score=77)
    snapshot = _snapshot()

    async def fake_get_readiness_optimization_context(_merchant_id: str, *, channel: str = "ucp"):
        return payload, snapshot

    async def fake_get_product_cache_row(**_kwargs):
        return {
            "product_data": {
                "id": "prod_1",
                "platform": "shopify",
                "merchant_id": "merch_efbc46b4619cfbdf",
                "title": "Air Max Special Edition",
                "description": "Breathable running sneakers with Max Air cushioning for daily commuting and training.",
                "vendor": "Nike",
                "product_type": "Sneakers",
                "tags": ["men", "air cushion", "breathable"],
                "price": 129.0,
                "currency": "USD",
                "inventory_quantity": 12,
                "image_url": "https://example.com/product.jpg",
                "images": ["https://example.com/product.jpg"],
                "variants": [
                    {
                        "id": "var_42",
                        "variant_id": "var_42",
                        "title": "Black / White / 42",
                        "price": 129.0,
                        "currency": "USD",
                        "inventory_quantity": 4,
                        "options": {"Color": "Black / White", "Size": "42"},
                    },
                    {
                        "id": "var_45",
                        "variant_id": "var_45",
                        "title": "Black / White / 45",
                        "price": 129.0,
                        "currency": "USD",
                        "inventory_quantity": 3,
                        "options": {"Color": "Black / White", "Size": "45"},
                    },
                ],
                "orderable": True,
            }
        }

    async def fake_get_enrichment(**_kwargs):
        return {}

    monkeypatch.setattr(
        remediation,
        "get_readiness_optimization_context",
        fake_get_readiness_optimization_context,
    )
    monkeypatch.setattr(remediation, "get_product_cache_row", fake_get_product_cache_row)
    monkeypatch.setattr(remediation, "get_enrichment", fake_get_enrichment)

    preview = await preview_remediation_action(
        "merch_efbc46b4619cfbdf",
        plan_id="rdplan_current",
        action_id="act_product:shopify:prod_1",
    )

    assert preview["generated_content_context"][0]["title_health"] == "rewrite_candidate"
    assert preview["generated_content_context"][0]["suggested_title_preview"] == "Nike Air Max Sneakers Men's Black/White air-cushion, breathable Sizes 42-45"
    assert preview["generated_content_context"][0]["facts_used"]["category"] == "Sneakers"
    title_patch = next(
        patch for patch in preview["candidate_patches"]
        if patch["target_field"] == "title_override"
    )
    assert title_patch["after"] == "Nike Air Max Sneakers Men's Black/White air-cushion, breathable Sizes 42-45"


@pytest.mark.asyncio
async def test_preview_remediation_action_rejects_superseded_plan(monkeypatch):
    from readiness import remediation

    payload = _optimization_payload(plan_id="rdplan_latest", snapshot_id="rdsnap_latest", score=77)

    async def fake_get_readiness_optimization_context(_merchant_id: str, *, channel: str = "ucp"):
        return payload, _snapshot()

    monkeypatch.setattr(
        remediation,
        "get_readiness_optimization_context",
        fake_get_readiness_optimization_context,
    )

    with pytest.raises(PlanSupersededError):
        await preview_remediation_action(
            "merch_efbc46b4619cfbdf",
            plan_id="rdplan_old",
            action_id="act_product:shopify:prod_1",
        )


@pytest.mark.asyncio
async def test_preview_remediation_action_rejects_non_executable_manual_target(monkeypatch):
    from readiness import remediation

    payload = _optimization_payload(plan_id="rdplan_current", snapshot_id="rdsnap_current", score=77)
    payload.product_queue[0].top_issues[0].code = "out_of_stock"
    payload.product_queue[0].top_issues[0].label = "Out of stock"
    payload.product_queue[0].primary_action = "Review this product in your source catalog."
    payload.product_queue[0].priority_reason = "This product needs source-data fixes before it can be surfaced."
    payload.product_queue[0].recommended_action_type = "review_catalog_data"

    async def fake_get_readiness_optimization_context(_merchant_id: str, *, channel: str = "ucp"):
        assert channel == "ucp"
        return payload, _snapshot()

    async def fake_list_source_data_decisions(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        remediation,
        "get_readiness_optimization_context",
        fake_get_readiness_optimization_context,
    )

    with pytest.raises(ActionNotExecutableError):
        await preview_remediation_action(
            "merch_efbc46b4619cfbdf",
            plan_id="rdplan_current",
            action_type="run_product_enrichment",
            targets=[
                {
                    "scope": "product",
                    "platform": "shopify",
                    "platform_product_id": "prod_1",
                    "product_id": "prod_1",
                }
            ],
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

    async def fake_get_readiness_optimization_context(
        _merchant_id: str,
        *,
        channel: str = "ucp",
        force_refresh: bool = False,
    ):
        call_count["count"] += 1
        if call_count["count"] == 2:
            assert force_refresh is True
        return (
            (before_payload, _snapshot())
            if call_count["count"] == 1
            else (after_payload, _snapshot())
        )

    async def fake_run_enrichment_for_product(**kwargs):
        assert kwargs["platform"] == "shopify"
        assert kwargs["platform_product_id"] == "prod_1"
        return {"status": "ok"}

    monkeypatch.setattr(
        remediation,
        "get_readiness_optimization_context",
        fake_get_readiness_optimization_context,
    )
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

    async def fake_get_readiness_optimization_context(_merchant_id: str, *, channel: str = "ucp"):
        return payload, _snapshot()

    monkeypatch.setattr(
        remediation,
        "get_readiness_optimization_context",
        fake_get_readiness_optimization_context,
    )

    with pytest.raises(ActionNotExecutableError):
        await run_remediation_action(
            "merch_efbc46b4619cfbdf",
            plan_id="rdplan_current",
            action_id="act_review_integrations",
        )


@pytest.mark.asyncio
async def test_get_product_blocker_detail_returns_variant_cross_reference(monkeypatch):
    from readiness import remediation

    payload = _optimization_payload(plan_id="rdplan_current", snapshot_id="rdsnap_current", score=77)
    payload.product_queue[0].eligible_variant_count = 1
    payload.product_queue[0].excluded_variant_count = 1
    payload.product_queue[0].agent_push_status = "excluded_from_agent_push"
    payload.product_queue[0].agent_push_reason_codes = ["missing_price"]

    async def fake_get_readiness_optimization_context(_merchant_id: str, *, channel: str = "ucp"):
        assert channel == "ucp"
        return payload, _snapshot()

    async def fake_get_product_cache_row(**_kwargs):
        return {
            "product_data": {
                "id": "prod_1",
                "platform": "shopify",
                "merchant_id": "merch_efbc46b4619cfbdf",
                "title": "Alpha Product",
                "description": "Alpha product description",
                "vendor": "Alpha Brand",
                "product_type": "Serum",
                "tags": ["hydrating"],
                "price": 24.0,
                "currency": "USD",
                "inventory_quantity": 5,
                "image_url": "https://example.com/product.jpg",
                "images": ["https://example.com/product.jpg"],
                "variants": [
                    {
                        "id": "var_1",
                        "variant_id": "var_1",
                        "title": "Blue / Small",
                        "sku": "ALPHA-BL-S",
                        "price": 0.0,
                        "inventory_quantity": 0,
                    },
                    {
                        "id": "var_2",
                        "variant_id": "var_2",
                        "title": "Green / Medium",
                        "sku": "ALPHA-GR-M",
                        "price": 24.0,
                        "inventory_quantity": 5,
                    },
                ],
                "orderable": True,
            }
        }

    async def fake_get_enrichment(**_kwargs):
        return {}

    monkeypatch.setattr(
        remediation,
        "get_readiness_optimization_context",
        fake_get_readiness_optimization_context,
    )
    monkeypatch.setattr(remediation, "get_product_cache_row", fake_get_product_cache_row)
    monkeypatch.setattr(remediation, "get_enrichment", fake_get_enrichment)

    detail = await get_product_blocker_detail(
        "merch_efbc46b4619cfbdf",
        plan_id="rdplan_current",
        platform="shopify",
        platform_product_id="prod_1",
    )

    assert detail["product"]["platform"] == "shopify"
    assert detail["summary"]["blocked_variant_count"] == 1
    assert detail["summary"]["excluded_variant_count"] == 1
    assert len(detail["variants"]) == 2
    first_variant = detail["variants"][0]
    assert first_variant["variant_id"] == "var_1"
    assert first_variant["sku"] == "ALPHA-BL-S"
    assert first_variant["inventory_quantity"] == 0
    assert first_variant["readiness_status"] == "blocked"
    assert "missing_price" in first_variant["readiness_blocker_codes"]
    assert first_variant["agent_push_status"] == "excluded_from_agent_push"


@pytest.mark.asyncio
async def test_get_source_data_triage_returns_summary_and_rows(monkeypatch):
    from readiness import remediation

    payload = _optimization_payload(plan_id="rdplan_current", snapshot_id="rdsnap_current", score=77)
    payload.product_queue[0].blocked_variant_count = 1
    payload.product_queue[0].ready_variant_count = 1
    payload.product_queue[0].eligible_variant_count = 1
    payload.product_queue[0].excluded_variant_count = 1
    payload.product_queue[0].agent_push_status = "excluded_from_agent_push"
    payload.product_queue[0].agent_push_reason_codes = ["missing_price", "out_of_stock"]
    payload.product_queue[0].fix_surface = "catalog_data"
    payload.product_queue[0].recommended_action_type = "review_catalog_data"
    payload.product_queue[0].top_issues = [
        {
            "code": "missing_primary_image",
            "label": "Missing primary image",
            "impact": "discovery_only",
            "affected_variant_count": 2,
        }
    ]

    async def fake_get_readiness_optimization_context(_merchant_id: str, *, channel: str = "ucp"):
        assert channel == "ucp"
        return payload, _snapshot()

    async def fake_list_source_data_decisions(*_args, **_kwargs):
        return {}

    async def fake_get_product_cache_row(**_kwargs):
        return None

    monkeypatch.setattr(
        remediation,
        "get_readiness_optimization_context",
        fake_get_readiness_optimization_context,
    )
    monkeypatch.setattr(
        remediation,
        "list_source_data_decisions",
        fake_list_source_data_decisions,
    )
    monkeypatch.setattr(
        remediation,
        "get_product_cache_row",
        fake_get_product_cache_row,
    )

    triage = await get_source_data_triage(
        "merch_efbc46b4619cfbdf",
        plan_id="rdplan_current",
    )

    assert triage["plan_id"] == "rdplan_current"
    summary_by_code = {item["code"]: item for item in triage["summary"]}
    assert summary_by_code["missing_price"]["affected_products"] == 1
    assert summary_by_code["missing_price"]["affected_variants"] == 1
    assert summary_by_code["out_of_stock"]["affected_variants"] == 1
    assert summary_by_code["missing_primary_image"]["affected_products"] == 1
    assert summary_by_code["missing_primary_image"]["affected_variants"] == 2

    rows_by_reason = {row["reason_code"] for row in triage["rows"]}
    assert {"missing_price", "out_of_stock", "missing_primary_image"} <= rows_by_reason

    missing_price_row = next(row for row in triage["rows"] if row["reason_code"] == "missing_price")
    assert missing_price_row["scope"] == "variant"
    assert missing_price_row["variant_id"] == "var_1"
    assert missing_price_row["agent_push_status"] == "excluded_from_agent_push"
    assert (
        missing_price_row["platform_admin_url"]
        == "https://alpha-beauty-demo.myshopify.com/admin/products/prod_1"
    )

    image_row = next(row for row in triage["rows"] if row["reason_code"] == "missing_primary_image")
    assert image_row["scope"] == "product"
    assert image_row["platform_product_id"] == "prod_1"
    assert (
        image_row["platform_admin_url"]
        == "https://alpha-beauty-demo.myshopify.com/admin/products/prod_1"
    )


@pytest.mark.asyncio
async def test_get_source_data_triage_hydrates_saved_decision_states(monkeypatch):
    from readiness import remediation

    payload = _optimization_payload(plan_id="rdplan_current", snapshot_id="rdsnap_current", score=77)
    payload.product_queue[0].blocked_variant_count = 1
    payload.product_queue[0].ready_variant_count = 1
    payload.product_queue[0].eligible_variant_count = 1
    payload.product_queue[0].excluded_variant_count = 1
    payload.product_queue[0].agent_push_status = "excluded_from_agent_push"
    payload.product_queue[0].agent_push_reason_codes = ["missing_price", "out_of_stock"]
    payload.product_queue[0].fix_surface = "catalog_data"
    payload.product_queue[0].recommended_action_type = "review_catalog_data"
    payload.product_queue[0].top_issues = [
        {
            "code": "missing_primary_image",
            "label": "Missing primary image",
            "impact": "discovery_only",
            "affected_variant_count": 2,
        }
    ]

    async def fake_get_readiness_optimization_context(_merchant_id: str, *, channel: str = "ucp"):
        assert channel == "ucp"
        return payload, _snapshot()

    async def fake_list_source_data_decisions(
        _merchant_id: str,
        *,
        reason_code: str | None = None,
        product_keys=None,
    ):
        if reason_code == "missing_price":
            return {
                "shopify|prod_1": {
                    "decision_state": "pricing_fix_saved",
                }
            }
        if reason_code == "missing_primary_image":
            return {
                "shopify|prod_1": {
                    "decision_state": "image_fix_saved",
                }
            }
        return {}

    async def fake_get_product_cache_row(**_kwargs):
        return None

    monkeypatch.setattr(
        remediation,
        "get_readiness_optimization_context",
        fake_get_readiness_optimization_context,
    )
    monkeypatch.setattr(
        remediation,
        "list_source_data_decisions",
        fake_list_source_data_decisions,
    )
    monkeypatch.setattr(
        remediation,
        "get_product_cache_row",
        fake_get_product_cache_row,
    )

    triage = await get_source_data_triage(
        "merch_efbc46b4619cfbdf",
        plan_id="rdplan_current",
    )

    missing_price_row = next(row for row in triage["rows"] if row["reason_code"] == "missing_price")
    image_row = next(row for row in triage["rows"] if row["reason_code"] == "missing_primary_image")
    assert missing_price_row["decision_state"] == "pricing_fix_saved"
    assert image_row["decision_state"] == "image_fix_saved"


@pytest.mark.asyncio
async def test_get_source_data_triage_defaults_out_of_stock_decision_state(monkeypatch):
    from readiness import remediation

    payload = _optimization_payload(plan_id="rdplan_current", snapshot_id="rdsnap_current", score=77)
    payload.product_queue[0].blocked_variant_count = 0
    payload.product_queue[0].ready_variant_count = 1
    payload.product_queue[0].eligible_variant_count = 1
    payload.product_queue[0].excluded_variant_count = 1
    payload.product_queue[0].agent_push_status = "excluded_from_agent_push"
    payload.product_queue[0].agent_push_reason_codes = ["out_of_stock"]
    payload.product_queue[0].fix_surface = "catalog_data"
    payload.product_queue[0].recommended_action_type = "review_catalog_data"
    payload.product_queue[0].top_issues = [
        {
            "code": "out_of_stock",
            "label": "Out of stock",
            "impact": "checkout",
            "affected_variant_count": 1,
        }
    ]

    async def fake_get_readiness_optimization_context(_merchant_id: str, *, channel: str = "ucp"):
        assert channel == "ucp"
        return payload, _snapshot()

    async def fake_list_source_data_decisions(
        _merchant_id: str,
        *,
        reason_code: str | None = None,
        product_keys=None,
    ):
        return {}

    async def fake_get_product_cache_row(
        *,
        merchant_id: str,
        platform: str,
        platform_product_id: str,
        include_expired: bool = False,
    ):
        assert merchant_id == "merch_efbc46b4619cfbdf"
        assert platform == "shopify"
        assert platform_product_id == "prod_1"
        assert include_expired is False
        return {
            "product_data": {
                "status": "active",
                "orderable": True,
                "image_url": "https://example.com/product.jpg",
                "variants": [
                    {
                        "variant_id": "var_1",
                        "inventory_quantity": 0,
                        "price": {"amount": 24.0, "currency": "USD"},
                    },
                    {
                        "variant_id": "var_2",
                        "inventory_quantity": 5,
                        "price": {"amount": 24.0, "currency": "USD"},
                    },
                ],
            }
        }

    monkeypatch.setattr(
        remediation,
        "get_readiness_optimization_context",
        fake_get_readiness_optimization_context,
    )
    monkeypatch.setattr(
        remediation,
        "list_source_data_decisions",
        fake_list_source_data_decisions,
    )
    monkeypatch.setattr(
        remediation,
        "get_product_cache_row",
        fake_get_product_cache_row,
    )

    triage = await get_source_data_triage(
        "merch_efbc46b4619cfbdf",
        plan_id="rdplan_current",
        reason_code="out_of_stock",
    )

    out_of_stock_row = next(row for row in triage["rows"] if row["reason_code"] == "out_of_stock")
    assert out_of_stock_row["decision_state"] == "restock_planned"
