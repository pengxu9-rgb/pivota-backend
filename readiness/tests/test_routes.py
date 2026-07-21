from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.error_handler import ErrorHandlerMiddleware
from readiness.models import MerchantReadinessOptimizationPayload
from readiness.order_sync import InMemoryReadinessJournal
from readiness.service import reset_readiness_snapshot_cache_observability
from readiness.summary import reset_readiness_optimization_cache_observability
from readiness.tests.conftest import (
    ALPHA_MERCHANT_ID as DEFAULT_ALPHA_MERCHANT_ID,
    build_live_shopify_products,
    build_review_summaries,
    load_real_merchant_fixture,
)


@pytest.fixture(autouse=True)
def _reset_readiness_caches():
    reset_readiness_snapshot_cache_observability()
    reset_readiness_optimization_cache_observability()


def _install_live_source_mocks(monkeypatch, *, psp_enabled: bool):
    from readiness.sources import shopify_live

    fixture = load_real_merchant_fixture()
    live_products = build_live_shopify_products()

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": fixture["merchant_id"], "business_name": fixture["merchant_name"]}

    async def fake_get_primary_store(_merchant_id: str):
        return fixture["store"]

    async def fake_get_shopify_cfg(_merchant_id: str):
        return fixture["shopify_config"]

    async def fake_get_cached_products(*, merchant_id: str, platform: str, include_expired: bool = False):
        assert merchant_id == fixture["merchant_id"]
        assert platform == "shopify"
        rows = deepcopy(fixture["products_cache_rows"])
        now = datetime.now(timezone.utc).replace(microsecond=0)
        rows[0]["cached_at"] = now.isoformat().replace("+00:00", "Z")
        rows[0]["expires_at"] = (now + timedelta(days=7)).isoformat().replace("+00:00", "Z")
        rows[1]["cached_at"] = (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        rows[1]["expires_at"] = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        return rows

    async def fake_get_active_psp(_merchant_id: str):
        return fixture["merchant_psp"] if psp_enabled else None

    async def fake_fetch_live_products(_merchant_id: str, _shop_domain: str, _access_token: str):
        return live_products, None

    async def fake_load_product_review_summaries(**_kwargs):
        return build_review_summaries()

    monkeypatch.setattr(shopify_live, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(shopify_live, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(shopify_live, "_get_shopify_config_for_merchant", fake_get_shopify_cfg)
    monkeypatch.setattr(shopify_live, "get_cached_products", fake_get_cached_products)
    monkeypatch.setattr(shopify_live, "_fetch_active_psp_config", fake_get_active_psp)
    monkeypatch.setattr(shopify_live, "_fetch_live_products", fake_fetch_live_products)
    monkeypatch.setattr(shopify_live, "load_product_review_summaries", fake_load_product_review_summaries)


def _build_test_client(monkeypatch, *, psp_enabled: bool, include_error_handler: bool = False) -> TestClient:
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("FEATURE_READINESS_SOURCE_OF_TRUTH_V1", "true")
    monkeypatch.setenv("FEATURE_READINESS_CANONICAL_CHECKOUT_ALPHA", "true")
    monkeypatch.setenv("FEATURE_READINESS_PAYMENT_BRIDGE_ALPHA", "true")
    monkeypatch.setenv("FEATURE_READINESS_PAYMENT_INTENT_ALPHA", "true")
    monkeypatch.setenv("FEATURE_READINESS_PAYMENT_STATUS_SYNC_ALPHA", "true")
    monkeypatch.setenv("FEATURE_READINESS_REFUND_ALPHA", "true")
    monkeypatch.setenv("FEATURE_READINESS_RETURN_SYNC_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALLOW_UNAUTHED_DEV", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", DEFAULT_ALPHA_MERCHANT_ID)

    _install_live_source_mocks(monkeypatch, psp_enabled=psp_enabled)

    from readiness import service as readiness_service
    from readiness import order_sync as readiness_order_sync
    from routes.readiness_internal import router as readiness_router

    readiness_order_sync._default_journal = InMemoryReadinessJournal()
    app = FastAPI()
    if include_error_handler:
        app.add_middleware(ErrorHandlerMiddleware)
    app.include_router(readiness_router)
    return TestClient(app)


def test_real_merchant_report_and_export(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    report = client.get(f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/report?channel=ucp")
    assert report.status_code == 200
    report_json = report.json()
    assert report_json["merchant_alpha_mode"] == "real_merchant_alpha"
    assert report_json["capability_status"]["checkout"] == "ready"
    assert report_json["capability_status"]["reviews_confidence"] == "ready"
    assert report_json["merchant_id"] == DEFAULT_ALPHA_MERCHANT_ID
    assert report_json["products"][0]["reviews"]["review_count"] == 27

    export = client.get(f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/exports/ucp")
    assert export.status_code == 200
    export_json = export.json()
    assert export_json["merchant_alpha_mode"] == "real_merchant_alpha"
    assert export_json["capability_status"]["checkout"] == "ready"
    assert export_json["capability_status"]["reviews_confidence"] == "ready"
    assert len(export_json["offers"]) == 3
    offer_variant_ids = {offer["variant_id"] for offer in export_json["offers"]}
    assert "431000000001" in offer_variant_ids
    assert "431000000002" in offer_variant_ids
    assert "431000000003" in offer_variant_ids
    assert "431000000004" not in offer_variant_ids
    assert all(offer["reviews"]["has_reviews"] is True for offer in export_json["offers"])


def test_real_merchant_summary_report_and_export(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    report = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/report?channel=ucp&summary_only=true&sample_limit=2"
    )
    assert report.status_code == 200
    report_json = report.json()
    assert report_json["response_mode"] == "summary"
    assert report_json["products"] == []
    assert report_json["summary"]["product_count"] == 2
    assert report_json["summary"]["variant_count"] == 4
    assert report_json["summary"]["ready_variant_count"] == 3
    assert report_json["summary"]["blocked_variant_count"] == 1
    assert report_json["summary"]["sample_limit"] == 2
    assert len(report_json["summary"]["ready_variant_ids_sample"]) == 2
    assert report_json["summary"]["blocked_variant_ids_sample"] == ["431000000004"]

    export = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/exports/ucp?summary_only=true&sample_limit=2"
    )
    assert export.status_code == 200
    export_json = export.json()
    assert export_json["response_mode"] == "summary"
    assert export_json["offers"] == []
    assert export_json["summary"]["offer_count"] == 3
    assert export_json["summary"]["review_backed_offer_count"] == 3
    assert export_json["summary"]["sample_limit"] == 2
    assert len(export_json["summary"]["offer_ids_sample"]) == 2


def test_merchant_readiness_optimization_route_returns_payload(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import summary as readiness_summary

    async def fake_build_readiness_optimization(
        merchant_id: str,
        *,
        force_refresh: bool = False,
        channel: str = "ucp",
        queue_mode: str = "full",
        page: int = 1,
        page_size: int = 50,
        search: str | None = None,
        issue_bucket: str | None = None,
        push_status: str = "all",
        blocked_only: bool = False,
        low_quality_only: bool = False,
        sort_by: str = "default",
    ):
        assert merchant_id == DEFAULT_ALPHA_MERCHANT_ID
        assert force_refresh is False
        assert channel == "ucp"
        assert queue_mode == "full"
        assert page == 1
        assert page_size == 50
        assert search is None
        assert issue_bucket is None
        assert push_status == "all"
        assert blocked_only is False
        assert low_quality_only is False
        assert sort_by == "default"
        return MerchantReadinessOptimizationPayload.model_validate(
            {
                "plan": {
                    "plan_id": "rdplan_test",
                    "snapshot_id": "rdsnap_test",
                    "workspace_version": "agent_commerce_optimization.v1",
                    "priority_policy_version": "merchant_readiness_priority.v1",
                    "refresh_state": "fresh",
                    "generated_at": "2026-03-18T00:00:00Z",
                    "expires_at": "2026-03-18T06:00:00Z",
                    "can_apply_actions": True,
                    "last_successful_rescore_at": "2026-03-18T00:00:00Z",
                },
                "score_bundle": {
                    "readiness_score": 77,
                    "exposure_score": None,
                    "conversion_score": None,
                },
                "readiness_summary": {
                    "tier": "yellow",
                    "label": "Needs Attention",
                    "assessment_state": "assessed",
                    "score": 77,
                    "ready_variant_count": 3,
                    "blocked_variant_count": 1,
                },
                "issue_buckets": [
                    {
                        "code": "price_currency",
                        "label": "Price / currency",
                        "severity": "high",
                        "scope": "product",
                        "affected_count": 1,
                        "fix_surface": "catalog_data",
                        "fixability": "merchant_fixable",
                        "impact": "full_agent_commerce",
                        "direct_target": "/dashboard/product-optimization?focus=price_currency",
                        "priority_score": 181,
                        "priority_reason": "Fixing this issue can unlock blocked agent commerce actions.",
                        "reason_codes": ["missing_price"],
                    }
                ],
                "merchant_actions": [
                    {
                        "action_id": "act_price_currency",
                        "action_type": "review_and_fix",
                        "label": "Fix products in Product Optimization",
                        "description": "Price / currency is affecting checkout for part of the catalog.",
                        "target_url": "/dashboard/product-optimization?focus=price_currency",
                        "fix_surface": "catalog_data",
                        "fixability": "merchant_fixable",
                        "scope": "product",
                        "impact": "full_agent_commerce",
                        "affected_count": 1,
                        "priority_score": 181,
                        "priority_reason": "Fixing this issue can unlock blocked agent commerce actions.",
                        "related_bucket_codes": ["price_currency"],
                    }
                ],
                "product_queue": [
                    {
                        "queue_item_scope": "product",
                        "queue_item_id": "product:shopify:prod_1",
                        "product_id": "prod_1",
                        "platform": "shopify",
                        "title": "Alpha Product",
                        "image_url": "https://example.com/p.jpg",
                        "blocked_variant_count": 1,
                        "ready_variant_count": 0,
                        "top_issues": [
                            {
                                "code": "missing_price",
                                "label": "Missing price",
                                "impact": "full_agent_commerce",
                                "affected_variant_count": 1,
                            }
                        ],
                        "primary_action": "Fix missing prices for this product before enabling AI checkout.",
                        "fix_surface": "catalog_data",
                        "fixability": "merchant_fixable",
                        "impact": "full_agent_commerce",
                        "priority_score": 157,
                        "priority_reason": "Fixing this product can unlock checkout for blocked variants.",
                        "content_gap_codes": ["generic_low_information_title", "missing_size_guidance"],
                        "missing_attribute_labels": ["Size guidance", "Material / ingredient info"],
                        "title_health": "rewrite_candidate",
                        "suggested_title_preview": "Nike Air Max Sneakers Men's Black/White air-cushion, breathable Sizes 42-45",
                        "suggestion_language": "en",
                        "suggestion_confidence": 0.77,
                        "suggestion_rationale": "Suggested title uses verified product facts and keeps missing facts out of the copy.",
                    }
                ],
                "product_queue_page": {
                    "page": 1,
                    "page_size": 1,
                    "total_items": 1,
                    "total_pages": 1,
                    "has_next": False,
                    "has_prev": False,
                    "applied_filters": {
                        "push_status": "all",
                        "blocked_only": False,
                        "low_quality_only": False,
                        "sort_by": "default",
                    },
                },
                "last_generated_at": "2026-03-18T00:00:00Z",
            }
        )

    monkeypatch.setattr(readiness_summary, "build_readiness_optimization", fake_build_readiness_optimization)

    from routes import merchant_api_extensions as merchant_api_extensions

    async def fake_get_merchant_id_from_user(_current_user):
        return DEFAULT_ALPHA_MERCHANT_ID

    monkeypatch.setattr(merchant_api_extensions, "build_readiness_optimization", fake_build_readiness_optimization)
    monkeypatch.setattr(merchant_api_extensions, "get_merchant_id_from_user", fake_get_merchant_id_from_user)

    app = FastAPI()
    app.include_router(merchant_api_extensions.router)

    async def fake_current_user():
        return {"role": "merchant", "user_id": "merchant_user"}

    app.dependency_overrides[merchant_api_extensions.get_current_user] = fake_current_user
    route_client = TestClient(app)

    response = route_client.get("/merchant/readiness/optimization")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["plan"]["plan_id"] == "rdplan_test"
    assert body["data"]["score_bundle"]["readiness_score"] == 77
    assert body["data"]["readiness_summary"]["tier"] == "yellow"
    assert body["data"]["issue_buckets"][0]["code"] == "price_currency"
    assert body["data"]["product_queue"][0]["platform"] == "shopify"
    assert body["data"]["product_queue_page"]["total_items"] == 1
    assert body["data"]["product_queue"][0]["title_health"] == "rewrite_candidate"
    assert body["data"]["product_queue"][0]["suggestion_language"] == "en"


def test_merchant_readiness_refresh_route_returns_latest_plan(monkeypatch):
    from routes import merchant_api_extensions as merchant_api_extensions

    async def fake_build_readiness_optimization(
        merchant_id: str,
        *,
        force_refresh: bool = False,
        channel: str = "ucp",
        queue_mode: str = "full",
        page: int = 1,
        page_size: int = 50,
        search: str | None = None,
        issue_bucket: str | None = None,
        push_status: str = "all",
        blocked_only: bool = False,
        low_quality_only: bool = False,
        sort_by: str = "default",
    ):
        assert merchant_id == DEFAULT_ALPHA_MERCHANT_ID
        assert force_refresh is True
        assert queue_mode == "full"
        assert page == 1
        assert page_size == 50
        assert search is None
        assert issue_bucket is None
        assert push_status == "all"
        assert blocked_only is False
        assert low_quality_only is False
        assert sort_by == "default"
        return MerchantReadinessOptimizationPayload.model_validate(
            {
                "plan": {
                    "plan_id": "rdplan_refresh",
                    "snapshot_id": "rdsnap_refresh",
                    "workspace_version": "agent_commerce_optimization.v1",
                    "priority_policy_version": "merchant_readiness_priority.v1",
                    "refresh_state": "fresh",
                    "generated_at": "2026-03-18T00:00:00Z",
                    "expires_at": "2026-03-18T06:00:00Z",
                    "can_apply_actions": True,
                    "last_successful_rescore_at": "2026-03-18T00:00:00Z",
                },
                "score_bundle": {
                    "readiness_score": 77,
                    "exposure_score": None,
                    "conversion_score": None,
                },
                "readiness_summary": {
                    "tier": "yellow",
                    "label": "Needs Attention",
                    "assessment_state": "assessed",
                    "score": 77,
                    "ready_variant_count": 3,
                    "blocked_variant_count": 1,
                },
                "issue_buckets": [],
                "merchant_actions": [],
                "product_queue": [],
                "last_generated_at": "2026-03-18T00:00:00Z",
            }
        )

    async def fake_get_merchant_id_from_user(_current_user):
        return DEFAULT_ALPHA_MERCHANT_ID

    monkeypatch.setattr(merchant_api_extensions, "build_readiness_optimization", fake_build_readiness_optimization)
    monkeypatch.setattr(merchant_api_extensions, "get_merchant_id_from_user", fake_get_merchant_id_from_user)

    app = FastAPI()
    app.include_router(merchant_api_extensions.router)

    async def fake_current_user():
        return {"role": "merchant", "user_id": "merchant_user"}

    app.dependency_overrides[merchant_api_extensions.get_current_user] = fake_current_user
    route_client = TestClient(app)

    response = route_client.post(
        "/merchant/readiness/actions/refresh",
        json={"scope": "merchant", "reason": "manual"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["plan"]["plan_id"] == "rdplan_refresh"
    assert body["meta"]["scope"] == "merchant"
    assert body["meta"]["reason"] == "manual"
    assert body["meta"]["refresh_state"] == "fresh"


def test_merchant_readiness_optimization_route_forwards_page_params(monkeypatch):
    from routes import merchant_api_extensions as merchant_api_extensions

    async def fake_build_readiness_optimization(
        merchant_id: str,
        *,
        force_refresh: bool = False,
        channel: str = "ucp",
        queue_mode: str = "full",
        page: int = 1,
        page_size: int = 50,
        search: str | None = None,
        issue_bucket: str | None = None,
        push_status: str = "all",
        blocked_only: bool = False,
        low_quality_only: bool = False,
        sort_by: str = "default",
    ):
        assert merchant_id == DEFAULT_ALPHA_MERCHANT_ID
        assert force_refresh is False
        assert queue_mode == "page"
        assert page == 2
        assert page_size == 25
        assert search == "air"
        assert issue_bucket == "catalog_content"
        assert push_status == "excluded"
        assert blocked_only is True
        assert low_quality_only is True
        assert sort_by == "cq_desc"
        return MerchantReadinessOptimizationPayload.model_validate(
            {
                "plan": {
                    "plan_id": "rdplan_page",
                    "snapshot_id": "rdsnap_page",
                    "workspace_version": "agent_commerce_optimization.v1",
                    "priority_policy_version": "merchant_readiness_priority.v1",
                    "refresh_state": "fresh",
                    "generated_at": "2026-03-18T00:00:00Z",
                    "expires_at": "2026-03-18T06:00:00Z",
                    "can_apply_actions": True,
                    "last_successful_rescore_at": "2026-03-18T00:00:00Z",
                },
                "score_bundle": {"readiness_score": 77},
                "readiness_summary": {
                    "tier": "yellow",
                    "label": "Needs Attention",
                    "assessment_state": "assessed",
                    "score": 77,
                    "ready_variant_count": 3,
                    "blocked_variant_count": 1,
                },
                "product_queue": [],
                "product_queue_page": {
                    "page": 2,
                    "page_size": 25,
                    "total_items": 25,
                    "total_pages": 1,
                    "has_next": False,
                    "has_prev": True,
                    "applied_filters": {
                        "search": "air",
                        "issue_bucket": "catalog_content",
                        "push_status": "excluded",
                        "blocked_only": True,
                        "low_quality_only": True,
                        "sort_by": "cq_desc",
                    },
                },
            }
        )

    async def fake_get_merchant_id_from_user(_current_user):
        return DEFAULT_ALPHA_MERCHANT_ID

    monkeypatch.setattr(merchant_api_extensions, "build_readiness_optimization", fake_build_readiness_optimization)
    monkeypatch.setattr(merchant_api_extensions, "get_merchant_id_from_user", fake_get_merchant_id_from_user)

    app = FastAPI()
    app.include_router(merchant_api_extensions.router)

    async def fake_current_user():
        return {"role": "merchant", "user_id": "merchant_user"}

    app.dependency_overrides[merchant_api_extensions.get_current_user] = fake_current_user
    route_client = TestClient(app)

    response = route_client.get(
        "/merchant/readiness/optimization",
        params={
            "queue_mode": "page",
            "page": 2,
            "page_size": 25,
            "search": "air",
            "issue_bucket": "catalog_content",
            "push_status": "excluded",
            "blocked_only": "true",
            "low_quality_only": "true",
            "sort_by": "cq_desc",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["plan"]["plan_id"] == "rdplan_page"
    assert body["data"]["product_queue_page"]["page"] == 2


def test_merchant_readiness_action_preview_route_returns_preview(monkeypatch):
    from routes import merchant_api_extensions as merchant_api_extensions

    async def fake_get_merchant_id_from_user(_current_user):
        return DEFAULT_ALPHA_MERCHANT_ID

    async def fake_preview_remediation_action(_merchant_id: str, *, plan_id: str, action_id: str | None = None, action_type: str | None = None, targets=None):
        assert plan_id == "rdplan_test"
        assert action_id == "act_product:shopify:prod_1"
        return {
            "action": {
                "action_id": action_id,
                "action_type": "run_product_enrichment",
            },
            "candidate_patches": [{"target_field": "summary_short"}],
            "expected_impact": {"targets": []},
            "requires_approval": True,
            "warnings": [],
        }

    monkeypatch.setattr(merchant_api_extensions, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(merchant_api_extensions, "preview_remediation_action", fake_preview_remediation_action)

    app = FastAPI()
    app.include_router(merchant_api_extensions.router)

    async def fake_current_user():
        return {"role": "merchant", "user_id": "merchant_user"}

    app.dependency_overrides[merchant_api_extensions.get_current_user] = fake_current_user
    route_client = TestClient(app)

    response = route_client.post(
        "/merchant/readiness/actions/preview",
        json={"plan_id": "rdplan_test", "action_id": "act_product:shopify:prod_1", "dry_run": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["action"]["action_type"] == "run_product_enrichment"
    assert body["data"]["candidate_patches"][0]["target_field"] == "summary_short"


def test_merchant_readiness_action_preview_route_rejects_superseded_plan(monkeypatch):
    from routes import merchant_api_extensions as merchant_api_extensions
    from readiness.remediation import PlanSupersededError

    async def fake_get_merchant_id_from_user(_current_user):
        return DEFAULT_ALPHA_MERCHANT_ID

    async def fake_preview_remediation_action(_merchant_id: str, **_kwargs):
        raise PlanSupersededError(current_plan_id="rdplan_latest", current_snapshot_id="rdsnap_latest")

    monkeypatch.setattr(merchant_api_extensions, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(merchant_api_extensions, "preview_remediation_action", fake_preview_remediation_action)

    app = FastAPI()
    app.include_router(merchant_api_extensions.router)

    async def fake_current_user():
        return {"role": "merchant", "user_id": "merchant_user"}

    app.dependency_overrides[merchant_api_extensions.get_current_user] = fake_current_user
    route_client = TestClient(app)

    response = route_client.post(
        "/merchant/readiness/actions/preview",
        json={"plan_id": "rdplan_old", "action_id": "act_product:shopify:prod_1"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "OPTIMIZATION_PLAN_SUPERSEDED"
    assert detail["current_plan_id"] == "rdplan_latest"


def test_merchant_readiness_action_run_and_job_routes(monkeypatch):
    from routes import merchant_api_extensions as merchant_api_extensions

    async def fake_get_merchant_id_from_user(_current_user):
        return DEFAULT_ALPHA_MERCHANT_ID

    async def fake_run_remediation_action(_merchant_id: str, **_kwargs):
        return {
            "job": {
                "job_id": "rdjob_test",
                "action_id": "act_product:shopify:prod_1",
                "executor_type": "deterministic",
                "status": "completed",
            },
            "action": {"action_id": "act_product:shopify:prod_1"},
            "verification": {"after_snapshot_id": "rdsnap_after"},
            "after_plan": {"plan_id": "rdplan_after"},
        }

    def fake_get_execution_job(job_id: str):
        assert job_id == "rdjob_test"
        from readiness.models import ExecutionJob

        return ExecutionJob(
            job_id="rdjob_test",
            action_id="act_product:shopify:prod_1",
            executor_type="deterministic",
            status="completed",
        )

    monkeypatch.setattr(merchant_api_extensions, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(merchant_api_extensions, "run_remediation_action", fake_run_remediation_action)
    monkeypatch.setattr(merchant_api_extensions, "get_execution_job", fake_get_execution_job)

    app = FastAPI()
    app.include_router(merchant_api_extensions.router)

    async def fake_current_user():
        return {"role": "merchant", "user_id": "merchant_user"}

    app.dependency_overrides[merchant_api_extensions.get_current_user] = fake_current_user
    route_client = TestClient(app)

    run_response = route_client.post(
        "/merchant/readiness/actions/run",
        json={"plan_id": "rdplan_test", "action_id": "act_product:shopify:prod_1", "execution_mode": "sync"},
    )

    assert run_response.status_code == 200
    assert run_response.json()["data"]["job"]["job_id"] == "rdjob_test"

    job_response = route_client.get("/merchant/readiness/jobs/rdjob_test")

    assert job_response.status_code == 200
    assert job_response.json()["data"]["status"] == "completed"


def test_merchant_readiness_product_blockers_route_returns_variant_detail(monkeypatch):
    from routes import merchant_api_extensions as merchant_api_extensions

    async def fake_get_merchant_id_from_user(_current_user):
        return DEFAULT_ALPHA_MERCHANT_ID

    async def fake_get_product_blocker_detail(
        _merchant_id: str,
        *,
        plan_id: str,
        platform: str,
        platform_product_id: str,
    ):
        assert plan_id == "rdplan_test"
        assert platform == "shopify"
        assert platform_product_id == "prod_1"
        return {
            "plan_id": "rdplan_test",
            "snapshot_id": "rdsnap_test",
            "product": {
                "platform": "shopify",
                "platform_product_id": "prod_1",
                "product_id": "prod_1",
                "title": "Alpha Product",
            },
            "summary": {
                "ready_variant_count": 1,
                "blocked_variant_count": 1,
                "eligible_variant_count": 1,
                "excluded_variant_count": 1,
            },
            "variants": [
                {
                    "variant_id": "var_1",
                    "title": "Default",
                    "sku": "SKU-1",
                    "price_value": None,
                    "price_currency": "USD",
                    "inventory_quantity": 0,
                    "readiness_status": "blocked",
                    "readiness_blocker_codes": ["missing_price"],
                    "readiness_warning_codes": [],
                    "agent_push_status": "excluded_from_agent_push",
                    "agent_push_reason_codes": ["missing_price"],
                }
            ],
        }

    monkeypatch.setattr(merchant_api_extensions, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(merchant_api_extensions, "get_product_blocker_detail", fake_get_product_blocker_detail)

    app = FastAPI()
    app.include_router(merchant_api_extensions.router)

    async def fake_current_user():
        return {"role": "merchant", "user_id": "merchant_user"}

    app.dependency_overrides[merchant_api_extensions.get_current_user] = fake_current_user
    route_client = TestClient(app)

    response = route_client.get(
        "/merchant/readiness/optimization/products/shopify/prod_1/blockers",
        params={"plan_id": "rdplan_test"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["product"]["platform_product_id"] == "prod_1"
    assert body["data"]["variants"][0]["variant_id"] == "var_1"
    assert body["data"]["variants"][0]["agent_push_status"] == "excluded_from_agent_push"


def test_merchant_readiness_source_data_triage_route_returns_rows(monkeypatch):
    from routes import merchant_api_extensions as merchant_api_extensions

    async def fake_get_merchant_id_from_user(_current_user):
        return DEFAULT_ALPHA_MERCHANT_ID

    async def fake_get_source_data_triage(
        _merchant_id: str,
        *,
        plan_id: str,
        reason_code: str | None = None,
        limit: int = 500,
    ):
        assert plan_id == "rdplan_test"
        assert reason_code == "missing_price"
        assert limit == 200
        return {
            "plan_id": "rdplan_test",
            "snapshot_id": "rdsnap_test",
            "reason_code": "missing_price",
            "summary": [
                {
                    "code": "missing_price",
                    "label": "Missing price",
                    "scope": "variant",
                    "affected_products": 1,
                    "affected_variants": 2,
                }
            ],
            "rows": [
                {
                    "scope": "variant",
                    "reason_code": "missing_price",
                    "reason_label": "Missing price",
                    "platform": "shopify",
                    "platform_product_id": "prod_1",
                    "platform_admin_url": "https://alpha-beauty-demo.myshopify.com/admin/products/prod_1",
                    "product_id": "prod_1",
                    "product_title": "Alpha Product",
                    "variant_id": "var_1",
                    "variant_title": "Default",
                    "sku": "SKU-1",
                    "price_value": None,
                    "price_currency": "USD",
                    "inventory_quantity": 0,
                    "blocked_variant_count": 1,
                    "excluded_variant_count": 1,
                    "readiness_blocker_codes": ["missing_price"],
                    "readiness_warning_codes": [],
                    "agent_push_status": "excluded_from_agent_push",
                    "agent_push_reason_codes": ["missing_price"],
                    "recommended_action_type": "review_and_fix",
                    "fix_surface": "catalog_data",
                    "decision_state": None,
                }
            ],
            "total_rows": 1,
        }

    monkeypatch.setattr(merchant_api_extensions, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(merchant_api_extensions, "get_source_data_triage", fake_get_source_data_triage)

    app = FastAPI()
    app.include_router(merchant_api_extensions.router)

    async def fake_current_user():
        return {"role": "merchant", "user_id": "merchant_user"}

    app.dependency_overrides[merchant_api_extensions.get_current_user] = fake_current_user
    route_client = TestClient(app)

    response = route_client.get(
        "/merchant/readiness/optimization/source-data-triage",
        params={"plan_id": "rdplan_test", "reason_code": "missing_price", "limit": 200},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["plan_id"] == "rdplan_test"
    assert body["data"]["rows"][0]["variant_id"] == "var_1"
    assert body["data"]["rows"][0]["reason_code"] == "missing_price"
    assert (
        body["data"]["rows"][0]["platform_admin_url"]
        == "https://alpha-beauty-demo.myshopify.com/admin/products/prod_1"
    )


def test_merchant_readiness_source_data_triage_export_route_returns_csv(monkeypatch):
    from routes import merchant_api_extensions as merchant_api_extensions

    async def fake_get_merchant_id_from_user(_current_user):
        return DEFAULT_ALPHA_MERCHANT_ID

    async def fake_get_source_data_triage(
        _merchant_id: str,
        *,
        plan_id: str,
        reason_code: str | None = None,
        limit: int = 5000,
    ):
        assert plan_id == "rdplan_test"
        assert reason_code == "missing_price"
        assert limit == 5000
        return {
            "plan_id": "rdplan_test",
            "snapshot_id": "rdsnap_test",
            "reason_code": "missing_price",
            "summary": [],
            "rows": [
                {
                    "scope": "variant",
                    "reason_code": "missing_price",
                    "reason_label": "Missing price",
                    "platform": "shopify",
                    "platform_product_id": "prod_1",
                    "platform_admin_url": "https://alpha-beauty-demo.myshopify.com/admin/products/prod_1",
                    "product_id": "prod_1",
                    "product_title": "Alpha Product",
                    "variant_id": "var_1",
                    "variant_title": "Default",
                    "sku": "SKU-1",
                    "price_value": None,
                    "price_currency": "USD",
                    "inventory_quantity": 0,
                    "blocked_variant_count": 1,
                    "excluded_variant_count": 1,
                    "readiness_blocker_codes": ["missing_price"],
                    "readiness_warning_codes": [],
                    "agent_push_status": "excluded_from_agent_push",
                    "agent_push_reason_codes": ["missing_price"],
                    "recommended_action_type": "review_and_fix",
                    "fix_surface": "catalog_data",
                    "decision_state": "pricing_fix_saved",
                }
            ],
            "total_rows": 1,
        }

    monkeypatch.setattr(merchant_api_extensions, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(merchant_api_extensions, "get_source_data_triage", fake_get_source_data_triage)

    app = FastAPI()
    app.include_router(merchant_api_extensions.router)

    async def fake_current_user():
        return {"role": "merchant", "user_id": "merchant_user"}

    app.dependency_overrides[merchant_api_extensions.get_current_user] = fake_current_user
    route_client = TestClient(app)

    response = route_client.get(
        "/merchant/readiness/optimization/source-data-triage/export.csv",
        params={"plan_id": "rdplan_test", "reason_code": "missing_price"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in response.headers["content-disposition"]
    assert "platform_admin_url" in response.text
    assert "https://alpha-beauty-demo.myshopify.com/admin/products/prod_1" in response.text
    assert "reason_code" in response.text
    assert "missing_price" in response.text
    assert "pricing_fix_saved" in response.text


def test_merchant_readiness_source_data_decision_routes_persist_and_delete(monkeypatch):
    from routes import merchant_api_extensions as merchant_api_extensions

    async def fake_get_merchant_id_from_user(_current_user):
        return DEFAULT_ALPHA_MERCHANT_ID

    async def fake_upsert_source_data_decision_state(
        _merchant_id: str,
        *,
        reason_code: str,
        platform: str,
        platform_product_id: str,
        decision_state: str,
    ):
        assert reason_code == "missing_price"
        assert platform == "shopify"
        assert platform_product_id == "prod_1"
        assert decision_state == "pricing_fix_saved"
        return {
            "merchant_id": DEFAULT_ALPHA_MERCHANT_ID,
            "reason_code": reason_code,
            "platform": platform,
            "platform_product_id": platform_product_id,
            "decision_state": decision_state,
            "updated_at": "2026-03-30T00:00:00Z",
            "created_at": "2026-03-30T00:00:00Z",
        }

    async def fake_delete_source_data_decision_state(
        _merchant_id: str,
        *,
        reason_code: str,
        platform: str,
        platform_product_id: str,
    ):
        assert reason_code == "missing_price"
        assert platform == "shopify"
        assert platform_product_id == "prod_1"
        return {
            "merchant_id": DEFAULT_ALPHA_MERCHANT_ID,
            "reason_code": reason_code,
            "platform": platform,
            "platform_product_id": platform_product_id,
            "deleted": True,
        }

    monkeypatch.setattr(merchant_api_extensions, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(
        merchant_api_extensions,
        "upsert_source_data_decision_state",
        fake_upsert_source_data_decision_state,
    )
    monkeypatch.setattr(
        merchant_api_extensions,
        "delete_source_data_decision_state",
        fake_delete_source_data_decision_state,
    )

    app = FastAPI()
    app.include_router(merchant_api_extensions.router)

    async def fake_current_user():
        return {"role": "merchant", "user_id": "merchant_user"}

    app.dependency_overrides[merchant_api_extensions.get_current_user] = fake_current_user
    route_client = TestClient(app)

    put_response = route_client.put(
        "/merchant/readiness/source-data-decisions/missing_price/shopify/prod_1",
        json={"decision_state": "pricing_fix_saved"},
    )
    assert put_response.status_code == 200
    assert put_response.json()["data"]["decision_state"] == "pricing_fix_saved"

    delete_response = route_client.delete(
        "/merchant/readiness/source-data-decisions/missing_price/shopify/prod_1"
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["data"]["deleted"] is True


def test_merchant_dashboard_readiness_schedules_optimization_warmup(monkeypatch):
    from routes import merchant_api_extensions as merchant_api_extensions
    from readiness.models import ReadinessSummary

    async def fake_get_merchant_id_from_user(_current_user):
        return DEFAULT_ALPHA_MERCHANT_ID

    async def fake_build_readiness_summary(merchant_id: str, *, channel: str = "ucp"):
        assert merchant_id == DEFAULT_ALPHA_MERCHANT_ID
        assert channel == "ucp"
        return ReadinessSummary(
            tier="yellow",
            label="Needs Attention",
            assessment_state="assessed",
            assessment_scope="one_merchant_alpha",
            channel="ucp",
            score=77,
            ready_variant_count=3,
            blocked_variant_count=1,
        )

    warmups: list[tuple[str, str]] = []

    def fake_warmup(merchant_id: str, *, channel: str = "ucp"):
        warmups.append((merchant_id, channel))
        return True

    monkeypatch.setattr(merchant_api_extensions, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(merchant_api_extensions, "build_readiness_summary", fake_build_readiness_summary)
    monkeypatch.setattr(merchant_api_extensions, "schedule_readiness_optimization_warmup", fake_warmup)

    app = FastAPI()
    app.include_router(merchant_api_extensions.router)

    async def fake_current_user():
        return {"role": "merchant", "user_id": "merchant_user"}

    app.dependency_overrides[merchant_api_extensions.get_current_user] = fake_current_user
    route_client = TestClient(app)

    response = route_client.get("/merchant/dashboard/readiness")
    assert response.status_code == 200
    assert response.json()["data"]["score"] == 77
    assert warmups == [(DEFAULT_ALPHA_MERCHANT_ID, "ucp")]


def test_checkout_blocked_when_capability_missing(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=False)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={"variant_id": "431000000001", "quantity": 1},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "VARIANT_NOT_READY_FOR_CHECKOUT"
    assert "merchant_checkout_capability_missing" in detail["blockers"]


def test_checkout_blocked_error_code_is_preserved_with_error_handler(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=False, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={"variant_id": "431000000001", "quantity": 1},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "VARIANT_NOT_READY_FOR_CHECKOUT"
    assert body["error"]["details"]["code"] == "VARIANT_NOT_READY_FOR_CHECKOUT"
    assert "merchant_checkout_capability_missing" in body["error"]["details"]["blockers"]


def test_report_unsupported_channel_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/report?channel=google"
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "UNSUPPORTED_CHANNEL"
    assert body["error"]["details"]["code"] == "UNSUPPORTED_CHANNEL"
    assert body["error"]["details"]["channel"] == "google"


def test_report_unsupported_merchant_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.get("/internal/readiness/merchants/not-supported-merchant/report?channel=ucp")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "READINESS_MERCHANT_UNSUPPORTED"
    assert body["error"]["details"]["code"] == "READINESS_MERCHANT_UNSUPPORTED"
    assert "supported_merchants" in body["error"]["details"]


def test_checkout_variant_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={"variant_id": "does-not-exist", "quantity": 1},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "VARIANT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "VARIANT_NOT_FOUND"
    assert body["error"]["details"]["variant_id"] == "does-not-exist"


def test_checkout_session_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.get("/internal/readiness/checkout-sessions/rdchk_missing")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_payment_bridge_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_missing/payment-bridge",
        json={"payment_reference": "pi_missing"},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_payment_intent_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_missing/payment-intent",
        json={},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_payment_status_sync_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_missing/payment-status-sync",
        json={},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_refund_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_missing/refund",
        json={},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_return_sync_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_missing/return-sync",
        json={},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_return_eligibility_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_missing/return-eligibility"
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_order_sync_audit_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync-audit/rdchk_missing"
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_order_sync_not_found_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/rdchk_missing",
        json={"replay": False},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["code"] == "CHECKOUT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == "rdchk_missing"


def test_order_sync_audit_route_returns_service_payload(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    async def fake_build_order_sync_audit(merchant_id: str, checkout_id: str, *, sample_limit: int = 10):
        assert merchant_id == DEFAULT_ALPHA_MERCHANT_ID
        assert checkout_id == "rdchk_alpha_1"
        assert sample_limit == 7
        return {
            "merchant_id": merchant_id,
            "checkout_id": checkout_id,
            "merchant_alpha_mode": "real_merchant_alpha",
            "sync_signals": {
                "merchant_writeback": {"status": "ready"},
                "webhook_ingest": {"status": "pending"},
            },
        }

    monkeypatch.setattr(readiness_service, "build_order_sync_audit", fake_build_order_sync_audit)

    response = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync-audit/rdchk_alpha_1?sample_limit=7"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_id"] == "rdchk_alpha_1"
    assert body["sync_signals"]["merchant_writeback"]["status"] == "ready"
    assert body["sync_signals"]["webhook_ingest"]["status"] == "pending"


def test_return_sync_route_returns_service_payload(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    async def fake_sync_returns_for_checkout(
        merchant_id: str,
        checkout_id: str,
        *,
        api_version=None,
        limit: int = 20,
        sample_limit: int = 10,
    ):
        assert merchant_id == DEFAULT_ALPHA_MERCHANT_ID
        assert checkout_id == "rdchk_alpha_return_1"
        assert api_version == "2025-01"
        assert limit == 12
        assert sample_limit == 6
        return {
            "checkout": type("Checkout", (), {
                "checkout_id": checkout_id,
                "order_id": "ORD_RETURN_1",
                "session_payload": {"merchant_alpha_mode": "real_merchant_alpha"},
            })(),
            "order": {"shopify_order_id": "7001002003"},
            "return_sync_result": {"ok": True, "fetched": 1, "upserted": 1},
            "audit": {
                "checkout_id": checkout_id,
                "sync_signals": {
                    "return_sync": {"status": "ready", "return_record_count": 1},
                },
            },
        }

    monkeypatch.setattr(readiness_service, "sync_returns_for_checkout", fake_sync_returns_for_checkout)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_alpha_return_1/return-sync",
        json={"api_version": "2025-01", "limit": 12, "sample_limit": 6},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_id"] == "rdchk_alpha_return_1"
    assert body["order_id"] == "ORD_RETURN_1"
    assert body["shopify_order_id"] == "7001002003"
    assert body["return_sync_result"]["ok"] is True
    assert body["sync_audit"]["sync_signals"]["return_sync"]["status"] == "ready"


def test_return_sync_unavailable_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    from readiness import service as readiness_service

    async def fake_sync_returns_for_checkout(*_args, **_kwargs):
        raise ValueError(
            {
                "code": "CHECKOUT_RETURN_SYNC_UNAVAILABLE",
                "message": "Return sync requires a Shopify primary store for this merchant.",
            }
        )

    monkeypatch.setattr(readiness_service, "sync_returns_for_checkout", fake_sync_returns_for_checkout)

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_alpha_return_2/return-sync",
        json={},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_RETURN_SYNC_UNAVAILABLE"
    assert body["error"]["details"]["code"] == "CHECKOUT_RETURN_SYNC_UNAVAILABLE"


def test_return_eligibility_route_returns_service_payload(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    async def fake_probe_return_eligibility_for_checkout(
        merchant_id: str,
        checkout_id: str,
        *,
        api_version=None,
        sample_limit: int = 10,
    ):
        assert merchant_id == DEFAULT_ALPHA_MERCHANT_ID
        assert checkout_id == "rdchk_alpha_return_eligibility_1"
        assert api_version == "2025-01"
        assert sample_limit == 7
        return {
            "checkout": type("Checkout", (), {
                "checkout_id": checkout_id,
                "order_id": "ORD_RETURN_ELIGIBLE_1",
                "session_payload": {"merchant_alpha_mode": "real_merchant_alpha"},
            })(),
            "order": {"shopify_order_id": "7001002005"},
            "eligibility": {"status": "likely_eligible", "blockers": []},
            "platform_probe": {"return_capabilities": {"order_returns_available": True}},
            "audit": {"sync_signals": {"return_sync": {"status": "not_observed"}}},
        }

    monkeypatch.setattr(readiness_service, "probe_return_eligibility_for_checkout", fake_probe_return_eligibility_for_checkout)

    response = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_alpha_return_eligibility_1/return-eligibility?api_version=2025-01&sample_limit=7"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_id"] == "rdchk_alpha_return_eligibility_1"
    assert body["order_id"] == "ORD_RETURN_ELIGIBLE_1"
    assert body["shopify_order_id"] == "7001002005"
    assert body["eligibility"]["status"] == "likely_eligible"
    assert body["platform_probe"]["return_capabilities"]["order_returns_available"] is True


def test_return_eligibility_unavailable_error_code_is_preserved(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    from readiness import service as readiness_service

    async def fake_probe_return_eligibility_for_checkout(*_args, **_kwargs):
        raise ValueError(
            {
                "code": "CHECKOUT_RETURN_SYNC_UNAVAILABLE",
                "message": "Return eligibility requires a Shopify primary store for this merchant.",
            }
        )

    monkeypatch.setattr(
        readiness_service,
        "probe_return_eligibility_for_checkout",
        fake_probe_return_eligibility_for_checkout,
    )

    response = client.get(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_alpha_return_eligibility_2/return-eligibility"
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_RETURN_SYNC_UNAVAILABLE"
    assert body["error"]["details"]["code"] == "CHECKOUT_RETURN_SYNC_UNAVAILABLE"


def test_real_merchant_checkout_and_order_sync_are_idempotent(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    order_state = {"shopify_order_id": None}

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_1"

    async def fake_get_order(_order_id: str):
        return {"order_id": "ORD_ALPHA_1", "shopify_order_id": order_state["shopify_order_id"]}

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_1"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001002003",
            "shopify_order_name": "#1003",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001002003",
        }

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 2,
            "idempotency_key": "idem-alpha-1",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG"
            }
        },
    )
    assert checkout.status_code == 200
    checkout_json = checkout.json()
    assert checkout_json["payment_mode"] == "merchant_native_alpha"

    sync_1 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_json['checkout_id']}",
        json={"replay": False},
    )
    assert sync_1.status_code == 200
    sync_1_json = sync_1.json()
    assert sync_1_json["order_id"] == "ORD_ALPHA_1"
    assert sync_1_json["status"] == "state_synced"
    event_types = [event["event_type"] for event in sync_1_json["events"]]
    assert "payment_capability_verified" in event_types
    assert "order_created" in event_types
    assert "order_forwarded_to_merchant" in event_types
    assert "state_synced" in event_types

    sync_2 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_json['checkout_id']}",
        json={"replay": True},
    )
    assert sync_2.status_code == 200
    sync_2_json = sync_2.json()
    assert sync_2_json["status"] == "state_synced"
    assert sync_2_json["replayed"] is True


def test_payment_bridge_requires_local_order(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-no-order",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-bridge",
        json={"payment_reference": "pi_alpha_missing_order"},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_ORDER_NOT_CREATED"
    assert body["error"]["details"]["checkout_id"] == checkout_id


def test_payment_intent_requires_local_order(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-payment-intent-no-order",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-intent",
        json={},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_ORDER_NOT_CREATED"
    assert body["error"]["details"]["checkout_id"] == checkout_id


def test_real_merchant_payment_bridge_marks_order_paid_and_syncs_transaction(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    order_state = {
        "order_id": "ORD_ALPHA_PAID",
        "shopify_order_id": None,
        "status": "pending",
        "payment_status": "unpaid",
        "payment_intent_id": None,
        "psp_used": "stripe",
        "total": 29.0,
        "currency": "USD",
        "total_refunded": 0,
    }
    payment_updates = []
    order_updates = []
    bridged_events = []

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_PAID"

    async def fake_get_order(_order_id: str):
        return dict(order_state)

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_PAID"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001888999",
            "shopify_order_name": "#1888",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001888999",
        }

    async def fake_update_payment_info(order_id: str, payment_intent_id: str, client_secret: str, payment_status: str = "processing", psp_used=None):
        assert order_id == "ORD_ALPHA_PAID"
        payment_updates.append(
            {
                "payment_intent_id": payment_intent_id,
                "client_secret": client_secret,
                "payment_status": payment_status,
                "psp_used": psp_used,
            }
        )
        order_state["payment_intent_id"] = payment_intent_id
        order_state["payment_status"] = payment_status
        order_state["psp_used"] = psp_used
        return True

    async def fake_mark_order_paid(order_id: str):
        assert order_id == "ORD_ALPHA_PAID"
        order_state["status"] = "paid"
        order_state["payment_status"] = "paid"
        return True

    async def fake_update_order(order_id: str, update_data):
        assert order_id == "ORD_ALPHA_PAID"
        order_updates.append(dict(update_data))
        if "metadata" in update_data:
            order_state["metadata"] = dict(update_data["metadata"])
        return True

    async def fake_log_order_event(**kwargs):
        bridged_events.append(kwargs)

    async def fake_ensure_external_payment_transaction_best_effort(**kwargs):
        assert kwargs["shopify_order_id"] == "9001888999"
        assert kwargs["external_payment_ref"] == "pi_alpha_bridge_1"
        return {
            "ok": True,
            "created": True,
            "transaction_id": "txn_alpha_bridge_1",
            "parent_transaction_id": 991,
            "parent_transaction_gateway": "manual",
            "parent_transaction_source": "created_manual_parent",
        }

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)
    monkeypatch.setattr(readiness_service, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(readiness_service, "update_order", fake_update_order)
    monkeypatch.setattr(readiness_service, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(readiness_service, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(readiness_service, "ensure_external_payment_transaction_best_effort", fake_ensure_external_payment_transaction_best_effort)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-paid-bridge",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync.status_code == 200
    assert sync.json()["status"] == "state_synced"

    bridge = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-bridge",
        json={
            "payment_reference": "pi_alpha_bridge_1",
            "psp_used": "stripe",
            "source": "operator_canary_bridge",
        },
    )
    assert bridge.status_code == 200
    bridge_json = bridge.json()
    assert bridge_json["order_id"] == "ORD_ALPHA_PAID"
    assert bridge_json["payment_status"] == "paid"
    assert bridge_json["payment_reference"] == "pi_alpha_bridge_1"
    assert bridge_json["psp_used"] == "stripe"
    assert bridge_json["transaction_sync"]["ok"] is True
    assert bridge_json["transaction_sync"]["parent_transaction_id"] == 991
    assert bridge_json["replayed"] is False

    assert payment_updates[0]["payment_intent_id"] == "pi_alpha_bridge_1"
    assert payment_updates[0]["payment_status"] == "paid"
    assert order_updates[0]["metadata"]["shopify_parent_transaction_id"] == 991
    assert order_updates[0]["metadata"]["shopify_parent_transaction_gateway"] == "manual"
    assert bridged_events[0]["event_type"] == "readiness_payment_bridged"

    checkout_view = client.get(f"/internal/readiness/checkout-sessions/{checkout_id}")
    assert checkout_view.status_code == 200
    payload = checkout_view.json()["checkout"]["session_payload"]
    assert payload["payment_reference"] == "pi_alpha_bridge_1"
    assert payload["payment_psp_used"] == "stripe"
    assert payload["shopify_parent_transaction_id"] == 991


def test_real_merchant_payment_bridge_infers_stripe_psp_from_payment_reference(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    order_state = {
        "order_id": "ORD_ALPHA_BRIDGE_INFER",
        "shopify_order_id": None,
        "status": "pending",
        "payment_status": "unpaid",
        "payment_intent_id": None,
        "psp_used": "adyen",
        "total": 29.0,
        "currency": "EUR",
        "total_refunded": 0,
    }
    payment_updates = []

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_BRIDGE_INFER"

    async def fake_get_order(_order_id: str):
        return dict(order_state)

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_BRIDGE_INFER"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001888001",
            "shopify_order_name": "#1889",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001888001",
        }

    async def fake_update_payment_info(order_id: str, payment_intent_id: str, client_secret: str, payment_status: str = "processing", psp_used=None):
        assert order_id == "ORD_ALPHA_BRIDGE_INFER"
        payment_updates.append(
            {
                "payment_intent_id": payment_intent_id,
                "client_secret": client_secret,
                "payment_status": payment_status,
                "psp_used": psp_used,
            }
        )
        order_state["payment_intent_id"] = payment_intent_id
        order_state["payment_status"] = payment_status
        order_state["psp_used"] = psp_used
        return True

    async def fake_mark_order_paid(order_id: str):
        assert order_id == "ORD_ALPHA_BRIDGE_INFER"
        order_state["status"] = "paid"
        order_state["payment_status"] = "paid"
        return True

    async def fake_update_order(order_id: str, update_data):
        assert order_id == "ORD_ALPHA_BRIDGE_INFER"
        if "metadata" in update_data:
            order_state["metadata"] = dict(update_data["metadata"])
        return True

    async def fake_log_order_event(**_kwargs):
        return None

    async def fake_ensure_external_payment_transaction_best_effort(**kwargs):
        assert kwargs["shopify_order_id"] == "9001888001"
        assert kwargs["external_payment_ref"] == "pi_alpha_bridge_infer_1"
        assert kwargs["psp_used"] == "stripe"
        return {"ok": False, "skipped": True, "reason": "unsupported_shopify_transaction_shape"}

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)
    monkeypatch.setattr(readiness_service, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(readiness_service, "update_order", fake_update_order)
    monkeypatch.setattr(readiness_service, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(readiness_service, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(readiness_service, "ensure_external_payment_transaction_best_effort", fake_ensure_external_payment_transaction_best_effort)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-paid-bridge-infer",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync.status_code == 200

    bridge = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-bridge",
        json={
            "payment_reference": "pi_alpha_bridge_infer_1",
            "source": "operator_canary_bridge",
        },
    )

    assert bridge.status_code == 200
    body = bridge.json()
    assert body["psp_used"] == "stripe"
    assert payment_updates[0]["psp_used"] == "stripe"

    checkout_view = client.get(f"/internal/readiness/checkout-sessions/{checkout_id}")
    payload = checkout_view.json()["checkout"]["session_payload"]
    assert payload["payment_psp_used"] == "stripe"
    assert payload["payment_reference_type"] == "payment_intent"


def test_real_merchant_payment_intent_creation_is_idempotent(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from adapters.psp_adapter import PaymentIntent
    from readiness import service as readiness_service

    order_state = {
        "order_id": "ORD_ALPHA_INTENT",
        "shopify_order_id": None,
        "status": "pending",
        "payment_status": "unpaid",
        "payment_intent_id": None,
        "client_secret": None,
        "psp_used": None,
        "total": 29.0,
        "currency": "USD",
        "total_refunded": 0,
    }
    create_calls = []

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_INTENT"

    async def fake_get_order(_order_id: str):
        return dict(order_state)

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_INTENT"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001777000",
            "shopify_order_name": "#1777",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001777000",
        }

    async def fake_update_payment_info(order_id: str, payment_intent_id: str, client_secret: str, payment_status: str = "processing", psp_used=None):
        assert order_id == "ORD_ALPHA_INTENT"
        order_state["payment_intent_id"] = payment_intent_id
        order_state["client_secret"] = client_secret
        order_state["payment_status"] = payment_status
        order_state["psp_used"] = psp_used
        return True

    async def fake_log_order_event(**_kwargs):
        return None

    async def fake_create_payment_with_failover(*, merchant_id: str, amount, currency: str, metadata, preferred_psps=None, canonical_psp_required=False, enforce_live_readiness=False):
        create_calls.append(
            {
                "merchant_id": merchant_id,
                "amount": str(amount),
                "currency": currency,
                "metadata": metadata,
                "preferred_psps": preferred_psps,
                "canonical_psp_required": canonical_psp_required,
                "enforce_live_readiness": enforce_live_readiness,
            }
        )
        return (
            True,
            PaymentIntent(
                id="pi_alpha_intent_1",
                client_secret="cs_alpha_intent_1",
                amount=2900,
                currency="USD",
                status="requires_action",
                psp_type="stripe",
                raw_response={},
            ),
            None,
            "stripe",
        )

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)
    monkeypatch.setattr(readiness_service, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(readiness_service, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(readiness_service, "create_payment_with_failover", fake_create_payment_with_failover)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-payment-intent",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync.status_code == 200

    intent_1 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-intent",
        json={"preferred_psps": ["stripe"]},
    )
    assert intent_1.status_code == 200
    intent_1_json = intent_1.json()
    assert intent_1_json["payment_intent_id"] == "pi_alpha_intent_1"
    assert intent_1_json["payment_intent_status"] == "requires_action"
    assert intent_1_json["payment_status"] == "awaiting_payment"
    assert intent_1_json["payment_action"]["type"] == "stripe_client_secret"
    assert intent_1_json["bridged_to_paid"] is False
    assert intent_1_json["replayed"] is False
    assert create_calls[0]["preferred_psps"] == ["stripe"]
    assert create_calls[0]["enforce_live_readiness"] is True

    intent_2 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-intent",
        json={"preferred_psps": ["stripe"]},
    )
    assert intent_2.status_code == 200
    intent_2_json = intent_2.json()
    assert intent_2_json["payment_intent_id"] == "pi_alpha_intent_1"
    assert intent_2_json["replayed"] is True
    assert len(create_calls) == 1

    checkout_view = client.get(f"/internal/readiness/checkout-sessions/{checkout_id}")
    assert checkout_view.status_code == 200
    payload = checkout_view.json()["checkout"]["session_payload"]
    assert payload["payment_intent_id"] == "pi_alpha_intent_1"
    assert payload["payment_intent_status"] == "requires_action"


def test_payment_intent_route_passes_test_probe_flag(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    captured = {}

    class Checkout:
        checkout_id = "rdchk_route_probe"
        order_id = "ORD_ROUTE_PROBE"
        status = "state_synced"
        session_payload = {"merchant_alpha_mode": "real_merchant_alpha"}

    async def fake_create_payment_intent_for_checkout(
        merchant_id: str,
        checkout_id: str,
        *,
        preferred_psps=None,
        psp_mode=None,
        test_psp_probe=False,
    ):
        captured.update(
            {
                "merchant_id": merchant_id,
                "checkout_id": checkout_id,
                "preferred_psps": preferred_psps,
                "psp_mode": psp_mode,
                "test_psp_probe": test_psp_probe,
            }
        )
        return {
            "checkout": Checkout(),
            "events": [],
            "order": {"payment_status": "awaiting_payment"},
            "payment_intent_id": "pi_route_probe",
            "client_secret": "cs_route_probe",
            "psp_used": "stripe",
            "payment_intent_status": "requires_action",
            "payment_action": {"type": "stripe_client_secret", "client_secret": "cs_route_probe"},
            "bridged_to_paid": False,
            "replayed": False,
        }

    monkeypatch.setattr(
        readiness_service,
        "create_payment_intent_for_checkout",
        fake_create_payment_intent_for_checkout,
    )

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/rdchk_route_probe/payment-intent",
        json={
            "preferred_psps": ["stripe"],
            "psp_mode": "stripe_checkout",
            "test_psp_probe": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["payment_intent_id"] == "pi_route_probe"
    assert captured == {
        "merchant_id": DEFAULT_ALPHA_MERCHANT_ID,
        "checkout_id": "rdchk_route_probe",
        "preferred_psps": ["stripe"],
        "psp_mode": "stripe_checkout",
        "test_psp_probe": True,
    }


async def _exercise_readiness_payment_intent_probe(
    monkeypatch,
    *,
    merchant_id: str,
    allowlist: str,
    test_psp_probe: bool,
):
    from adapters.psp_adapter import PaymentIntent
    from readiness import order_sync as readiness_order_sync
    from readiness import service as readiness_service

    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", allowlist)

    journal = InMemoryReadinessJournal()
    readiness_order_sync._default_journal = journal
    checkout = await journal.create_checkout_session(
        merchant_id=merchant_id,
        channel="ucp",
        variant_id="431000000001",
        quantity=1,
        payment_mode="external",
        session_payload={
            "merchant_alpha_mode": "real_merchant_alpha",
            "product_title": "Alpha Serum",
            "price": {"amount": 29, "currency": "USD"},
        },
        continue_url=None,
        idempotency_key=None,
    )
    await journal.update_checkout_session(checkout.checkout_id, order_id="ORD_ALPHA_PROBE")

    order_state = {
        "id": "ORD_ALPHA_PROBE",
        "total": 29,
        "currency": "USD",
        "payment_status": "pending",
        "payment_intent_id": None,
        "client_secret": None,
        "psp_used": None,
    }
    create_calls = []

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_ALPHA_PROBE"
        return dict(order_state)

    async def fake_update_payment_info(order_id: str, payment_intent_id: str, client_secret: str, payment_status: str = "processing", psp_used=None):
        assert order_id == "ORD_ALPHA_PROBE"
        order_state["payment_intent_id"] = payment_intent_id
        order_state["client_secret"] = client_secret
        order_state["payment_status"] = payment_status
        order_state["psp_used"] = psp_used
        return True

    async def fake_log_order_event(**_kwargs):
        return None

    async def fake_create_payment_with_failover(
        *,
        merchant_id: str,
        amount,
        currency: str,
        metadata,
        preferred_psps=None,
        canonical_psp_required=False,
        enforce_live_readiness=False,
    ):
        create_calls.append(
            {
                "merchant_id": merchant_id,
                "amount": str(amount),
                "currency": currency,
                "metadata": metadata,
                "preferred_psps": preferred_psps,
                "canonical_psp_required": canonical_psp_required,
                "enforce_live_readiness": enforce_live_readiness,
            }
        )
        if enforce_live_readiness:
            return False, None, "live readiness required", "stripe"
        return (
            True,
            PaymentIntent(
                id="pi_alpha_probe_1",
                client_secret="cs_alpha_probe_1",
                amount=2900,
                currency="USD",
                status="requires_action",
                psp_type="stripe",
                raw_response={},
            ),
            None,
            "stripe",
        )

    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(readiness_service, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(readiness_service, "create_payment_with_failover", fake_create_payment_with_failover)

    result = await readiness_service.create_payment_intent_for_checkout(
        merchant_id,
        checkout.checkout_id,
        preferred_psps=["stripe"],
        psp_mode="stripe_checkout",
        test_psp_probe=test_psp_probe,
    )
    return result, create_calls


@pytest.mark.asyncio
async def test_readiness_payment_intent_allowlisted_test_probe_bypasses_live_readiness(monkeypatch):
    result, create_calls = await _exercise_readiness_payment_intent_probe(
        monkeypatch,
        merchant_id=DEFAULT_ALPHA_MERCHANT_ID,
        allowlist=f"merch_other, {DEFAULT_ALPHA_MERCHANT_ID}",
        test_psp_probe=True,
    )

    assert result["payment_intent_id"] == "pi_alpha_probe_1"
    assert create_calls[0]["enforce_live_readiness"] is False
    assert create_calls[0]["canonical_psp_required"] is True
    assert create_calls[0]["preferred_psps"] == ["stripe"]
    assert create_calls[0]["metadata"]["psp_mode"] == "stripe_checkout"


@pytest.mark.asyncio
async def test_readiness_payment_intent_non_allowlisted_test_probe_fails_closed(monkeypatch):
    with pytest.raises(ValueError) as exc:
        await _exercise_readiness_payment_intent_probe(
            monkeypatch,
            merchant_id=DEFAULT_ALPHA_MERCHANT_ID,
            allowlist="merch_other",
            test_psp_probe=True,
        )

    detail = exc.value.args[0]
    assert detail["code"] == "PAYMENT_FAILED"
    assert "live readiness required" in detail["message"]


def test_payment_status_sync_requires_existing_payment_intent(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    from readiness import service as readiness_service

    order_state = {
        "order_id": "ORD_ALPHA_STATUS_1",
        "shopify_order_id": None,
        "status": "pending",
        "payment_status": "unpaid",
        "payment_intent_id": None,
        "client_secret": None,
        "psp_used": "stripe",
        "total": 29.0,
        "currency": "USD",
        "total_refunded": 0,
    }

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_STATUS_1"

    async def fake_get_order(_order_id: str):
        return dict(order_state)

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_STATUS_1"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001777111",
            "shopify_order_name": "#1771",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001777111",
        }

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-payment-status-no-intent",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync.status_code == 200

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-status-sync",
        json={},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_PAYMENT_INTENT_NOT_FOUND"
    assert body["error"]["details"]["checkout_id"] == checkout_id


def test_payment_status_sync_bridges_paid_status(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    order_state = {
        "order_id": "ORD_ALPHA_STATUS_2",
        "shopify_order_id": None,
        "status": "pending",
        "payment_status": "awaiting_payment",
        "payment_intent_id": "pi_alpha_status_1",
        "client_secret": "cs_alpha_status_1",
        "psp_used": "stripe",
        "total": 29.0,
        "currency": "USD",
        "total_refunded": 0,
    }
    bridged_calls = []

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_STATUS_2"

    async def fake_get_order(_order_id: str):
        return dict(order_state)

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_STATUS_2"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001777222",
            "shopify_order_name": "#1772",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001777222",
        }

    async def fake_query_payment_intent_status(_merchant_id: str, *, payment_reference: str, psp_used=None):
        assert payment_reference == "pi_alpha_status_1"
        assert psp_used == "stripe"
        return {
            "ok": True,
            "raw_status": "succeeded",
            "normalized_status": "paid",
            "error": None,
            "psp_used": "stripe",
        }

    async def fake_attach_payment_reference_to_checkout(
        merchant_id: str,
        checkout_id: str,
        *,
        payment_reference: str,
        psp_used=None,
        client_secret=None,
        source="external_payment_execution",
        mark_paid=True,
        sync_shopify_transaction=True,
    ):
        bridged_calls.append(
            {
                "merchant_id": merchant_id,
                "checkout_id": checkout_id,
                "payment_reference": payment_reference,
                "psp_used": psp_used,
                "client_secret": client_secret,
                "source": source,
                "mark_paid": mark_paid,
                "sync_shopify_transaction": sync_shopify_transaction,
            }
        )
        order_state["status"] = "paid"
        order_state["payment_status"] = "paid"
        journal = readiness_service.get_default_journal()
        checkout = await journal.get_checkout_session(checkout_id)
        checkout = await journal.update_checkout_session(
            checkout_id,
            session_payload_patch={"payment_reference": payment_reference},
        ) or checkout
        return {
            "checkout": checkout,
            "events": await journal.list_events(checkout_id),
            "order": dict(order_state),
            "payment_reference": payment_reference,
            "psp_used": psp_used,
            "transaction_sync": {"ok": True, "skipped": False, "created": True},
            "replayed": False,
        }

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)
    monkeypatch.setattr(readiness_service, "_query_payment_intent_status", fake_query_payment_intent_status)
    monkeypatch.setattr(readiness_service, "attach_payment_reference_to_checkout", fake_attach_payment_reference_to_checkout)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-payment-status-success",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync.status_code == 200

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-status-sync",
        json={},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["payment_intent_id"] == "pi_alpha_status_1"
    assert body["payment_intent_status"] == "succeeded"
    assert body["normalized_payment_status"] == "paid"
    assert body["payment_status"] == "paid"
    assert body["bridged_to_paid"] is True
    assert body["transaction_sync"]["ok"] is True
    assert body["psp_used"] == "stripe"
    assert bridged_calls[0]["source"] == "readiness_payment_status_sync"


def test_query_payment_intent_status_resolves_stripe_checkout_session(monkeypatch):
    from readiness import service as readiness_service

    class FakeStripeAdapter:
        api_key = "sk_test_alpha_checkout"

    class FakePaymentIntent:
        id = "pi_alpha_checkout_paid"
        status = "succeeded"
        client_secret = "pi_alpha_checkout_paid_secret"

    class FakeCheckoutSession:
        payment_status = "paid"
        status = "complete"
        payment_intent = FakePaymentIntent()

    async def fake_resolve_psp_adapter_for_checkout(_merchant_id: str, *, psp_used=None):
        assert psp_used == "stripe"
        return FakeStripeAdapter(), "stripe"

    def fake_retrieve(checkout_session_id: str, expand=None):
        assert checkout_session_id == "cs_test_alpha_checkout"
        assert expand == ["payment_intent"]
        return FakeCheckoutSession()

    monkeypatch.setattr(readiness_service, "_resolve_psp_adapter_for_checkout", fake_resolve_psp_adapter_for_checkout)
    monkeypatch.setattr(readiness_service.stripe.checkout.Session, "retrieve", fake_retrieve)

    result = asyncio.run(
        readiness_service._query_payment_intent_status(
            DEFAULT_ALPHA_MERCHANT_ID,
            payment_reference="cs_test_alpha_checkout",
            psp_used="stripe",
        )
    )

    assert result["ok"] is True
    assert result["normalized_status"] == "paid"
    assert result["raw_status"] == "succeeded"
    assert result["payment_reference_type"] == "stripe_checkout_session"
    assert result["checkout_session_id"] == "cs_test_alpha_checkout"
    assert result["resolved_payment_reference"] == "pi_alpha_checkout_paid"
    assert result["resolved_client_secret"] == "pi_alpha_checkout_paid_secret"


def test_payment_status_sync_bridges_paid_stripe_checkout_session(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    order_state = {
        "order_id": "ORD_ALPHA_STATUS_CHECKOUT",
        "shopify_order_id": None,
        "status": "pending",
        "payment_status": "awaiting_payment",
        "payment_intent_id": "cs_test_alpha_checkout",
        "client_secret": "",
        "psp_used": "stripe",
        "total": 29.0,
        "currency": "USD",
        "total_refunded": 0,
    }
    bridged_calls = []

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_STATUS_CHECKOUT"

    async def fake_get_order(_order_id: str):
        return dict(order_state)

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_STATUS_CHECKOUT"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001777555",
            "shopify_order_name": "#1775",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001777555",
        }

    async def fake_query_payment_intent_status(_merchant_id: str, *, payment_reference: str, psp_used=None):
        assert payment_reference == "cs_test_alpha_checkout"
        assert psp_used == "stripe"
        return {
            "ok": True,
            "raw_status": "succeeded",
            "normalized_status": "paid",
            "error": None,
            "psp_used": "stripe",
            "payment_reference_type": "stripe_checkout_session",
            "checkout_session_id": "cs_test_alpha_checkout",
            "resolved_payment_reference": "pi_alpha_checkout_paid",
            "resolved_client_secret": "pi_alpha_checkout_paid_secret",
        }

    async def fake_attach_payment_reference_to_checkout(
        merchant_id: str,
        checkout_id: str,
        *,
        payment_reference: str,
        psp_used=None,
        client_secret=None,
        source="external_payment_execution",
        mark_paid=True,
        sync_shopify_transaction=True,
    ):
        bridged_calls.append(
            {
                "merchant_id": merchant_id,
                "checkout_id": checkout_id,
                "payment_reference": payment_reference,
                "psp_used": psp_used,
                "client_secret": client_secret,
                "source": source,
                "mark_paid": mark_paid,
                "sync_shopify_transaction": sync_shopify_transaction,
            }
        )
        order_state["status"] = "paid"
        order_state["payment_status"] = "paid"
        order_state["payment_intent_id"] = payment_reference
        journal = readiness_service.get_default_journal()
        checkout = await journal.get_checkout_session(checkout_id)
        checkout = await journal.update_checkout_session(
            checkout_id,
            session_payload_patch={
                "payment_reference": "cs_test_alpha_checkout",
                "checkout_session_id": "cs_test_alpha_checkout",
                "payment_intent_id": payment_reference,
                "payment_reference_type": "stripe_checkout_session",
            },
        ) or checkout
        return {
            "checkout": checkout,
            "events": await journal.list_events(checkout_id),
            "order": dict(order_state),
            "payment_reference": payment_reference,
            "psp_used": psp_used,
            "transaction_sync": {"ok": True, "skipped": False, "created": True},
            "replayed": False,
        }

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)
    monkeypatch.setattr(readiness_service, "_query_payment_intent_status", fake_query_payment_intent_status)
    monkeypatch.setattr(readiness_service, "attach_payment_reference_to_checkout", fake_attach_payment_reference_to_checkout)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-payment-status-checkout-session-success",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync.status_code == 200

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/payment-status-sync",
        json={},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["payment_intent_id"] == "pi_alpha_checkout_paid"
    assert body["payment_reference"] == "cs_test_alpha_checkout"
    assert body["payment_reference_type"] == "stripe_checkout_session"
    assert body["checkout_session_id"] == "cs_test_alpha_checkout"
    assert body["payment_intent_status"] == "succeeded"
    assert body["normalized_payment_status"] == "paid"
    assert body["payment_status"] == "paid"
    assert body["bridged_to_paid"] is True
    assert body["transaction_sync"]["ok"] is True
    assert bridged_calls[0]["payment_reference"] == "pi_alpha_checkout_paid"
    assert bridged_calls[0]["client_secret"] == "pi_alpha_checkout_paid_secret"
    assert bridged_calls[0]["source"] == "readiness_payment_status_sync"


def test_refund_requires_paid_order(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True, include_error_handler=True)

    from readiness import service as readiness_service

    order_state = {
        "order_id": "ORD_ALPHA_REFUND_NOPE",
        "shopify_order_id": None,
        "status": "pending",
        "payment_status": "awaiting_payment",
        "payment_intent_id": "pi_alpha_refund_nope",
        "client_secret": "cs_alpha_refund_nope",
        "psp_used": "stripe",
        "total": 29.0,
        "currency": "USD",
        "total_refunded": 0,
    }

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_REFUND_NOPE"

    async def fake_get_order(_order_id: str):
        return dict(order_state)

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_REFUND_NOPE"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001777333",
            "shopify_order_name": "#1773",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001777333",
        }

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-refund-nope",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync.status_code == 200

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/refund",
        json={},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "CHECKOUT_REFUND_NOT_ELIGIBLE"
    assert body["error"]["details"]["payment_status"] == "awaiting_payment"


def test_refund_success_reconciles_order_state(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    order_state = {
        "order_id": "ORD_ALPHA_REFUND_OK",
        "shopify_order_id": None,
        "status": "paid",
        "payment_status": "paid",
        "payment_intent_id": "pi_alpha_refund_ok",
        "client_secret": "cs_alpha_refund_ok",
        "psp_used": "stripe",
        "metadata": {"shopify_parent_transaction_id": 1444},
        "total": 29.0,
        "currency": "USD",
        "total_refunded": 0,
    }

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_REFUND_OK"

    async def fake_get_order(_order_id: str):
        return dict(order_state)

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_REFUND_OK"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001777444",
            "shopify_order_name": "#1774",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001777444",
        }

    async def fake_create_refund(*, order_id: str, amount: float, reason: str, source: str, created_by: str, idempotency_key=None):
        assert order_id == "ORD_ALPHA_REFUND_OK"
        assert amount == 29.0
        order_state["status"] = "refunded"
        order_state["payment_status"] = "refunded"
        order_state["total_refunded"] = 29.0
        return {
            "status": "success",
            "refund_id": "REF_ALPHA_OK",
            "psp_refund_id": "re_alpha_ok",
        }

    async def fake_ensure_external_refund_transaction_best_effort(**kwargs):
        assert kwargs["shopify_order_id"] == "9001777444"
        assert kwargs["external_refund_ref"] == "re_alpha_ok"
        assert kwargs["parent_transaction_id"] == 1444
        return {"ok": True, "created": True, "transaction_id": "txn_ref_alpha_ok"}

    async def fake_log_order_event(**_kwargs):
        return None

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)
    monkeypatch.setattr(readiness_service.refund_service, "create_refund", fake_create_refund)
    monkeypatch.setattr(readiness_service, "ensure_external_refund_transaction_best_effort", fake_ensure_external_refund_transaction_best_effort)
    monkeypatch.setattr(readiness_service, "log_order_event", fake_log_order_event)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-refund-ok",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync.status_code == 200

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/refund",
        json={"reason": "operator_canary_refund"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["refund_status"] == "success"
    assert body["refund_id"] == "REF_ALPHA_OK"
    assert body["psp_refund_id"] == "re_alpha_ok"
    assert body["platform_refund_id"] == "re_alpha_ok"
    assert body["payment_status"] == "refunded"
    assert body["status"] == "refunded"
    assert body["transaction_sync"]["ok"] is True
    assert body["replayed"] is False


def test_refund_success_logs_soft_skipped_transaction_sync_when_shopify_mirror_degrades(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    order_state = {
        "order_id": "ORD_ALPHA_REFUND_SOFTSKIP",
        "shopify_order_id": None,
        "status": "paid",
        "payment_status": "paid",
        "payment_intent_id": "pi_alpha_refund_softskip",
        "client_secret": "cs_alpha_refund_softskip",
        "psp_used": "stripe",
        "metadata": {"shopify_parent_transaction_id": 1444},
        "total": 29.0,
        "currency": "USD",
        "total_refunded": 0,
    }
    logged_events = []

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_REFUND_SOFTSKIP"

    async def fake_get_order(_order_id: str):
        return dict(order_state)

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_REFUND_SOFTSKIP"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001777555",
            "shopify_order_name": "#1775",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001777555",
        }

    async def fake_create_refund(*, order_id: str, amount: float, reason: str, source: str, created_by: str, idempotency_key=None):
        assert order_id == "ORD_ALPHA_REFUND_SOFTSKIP"
        order_state["status"] = "refunded"
        order_state["payment_status"] = "refunded"
        order_state["total_refunded"] = 29.0
        return {
            "status": "success",
            "refund_id": "REF_ALPHA_SOFTSKIP",
            "psp_refund_id": "re_alpha_softskip",
        }

    async def fake_ensure_external_refund_transaction_best_effort(**kwargs):
        assert kwargs["shopify_order_id"] == "9001777555"
        assert kwargs["external_refund_ref"] == "re_alpha_softskip"
        return {
            "ok": True,
            "created": False,
            "soft_skipped": True,
            "reason": "missing_parent_transaction",
            "annotation": {"ok": True, "status": 200, "error": None},
        }

    async def fake_log_order_event(**kwargs):
        logged_events.append(kwargs)
        return None

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)
    monkeypatch.setattr(readiness_service.refund_service, "create_refund", fake_create_refund)
    monkeypatch.setattr(readiness_service, "ensure_external_refund_transaction_best_effort", fake_ensure_external_refund_transaction_best_effort)
    monkeypatch.setattr(readiness_service, "log_order_event", fake_log_order_event)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-refund-softskip",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync.status_code == 200

    response = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout-sessions/{checkout_id}/refund",
        json={"reason": "operator_canary_refund"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["refund_status"] == "success"
    assert body["transaction_sync"]["soft_skipped"] is True
    transaction_events = [event for event in logged_events if event["event_type"] == "readiness_refund_transaction_sync"]
    assert len(transaction_events) == 1
    assert transaction_events[0]["status"] == "soft_skipped"


def test_order_sync_replay_reconciles_cancelled_order_state(monkeypatch):
    client = _build_test_client(monkeypatch, psp_enabled=True)

    from readiness import service as readiness_service

    order_state = {
        "shopify_order_id": None,
        "status": "pending",
        "payment_status": "unpaid",
        "total_refunded": 0,
    }

    async def fake_create_order(_order_data):
        return "ORD_ALPHA_CANCEL"

    async def fake_get_order(_order_id: str):
        return {
            "order_id": "ORD_ALPHA_CANCEL",
            "shopify_order_id": order_state["shopify_order_id"],
            "status": order_state["status"],
            "payment_status": order_state["payment_status"],
            "total_refunded": order_state["total_refunded"],
        }

    async def fake_update_fulfillment_info(order_id: str, shopify_order_id=None, **_kwargs):
        assert order_id == "ORD_ALPHA_CANCEL"
        order_state["shopify_order_id"] = shopify_order_id
        return True

    async def fake_create_shopify_order_for_checkout(**_kwargs):
        return {
            "ok": True,
            "shopify_order_id": "9001002999",
            "shopify_order_name": "#1099",
            "shopify_order_url": "https://alpha-beauty-demo.myshopify.com/admin/orders/9001002999",
        }

    monkeypatch.setattr(readiness_service, "create_order", fake_create_order)
    monkeypatch.setattr(readiness_service, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_service, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(readiness_service, "_create_shopify_order_for_checkout", fake_create_shopify_order_for_checkout)

    checkout = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/checkout",
        json={
            "variant_id": "431000000001",
            "quantity": 1,
            "idempotency_key": "idem-alpha-cancel",
            "buyer_email": "buyer@example.com",
            "customer_name": "Alpha Buyer",
            "shipping_address": {
                "name": "Alpha Buyer",
                "address_line1": "1 Orchard Road",
                "city": "Singapore",
                "postal_code": "238823",
                "country": "SG",
            },
        },
    )
    checkout_id = checkout.json()["checkout_id"]

    sync_1 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": False},
    )
    assert sync_1.status_code == 200
    assert sync_1.json()["status"] == "state_synced"

    order_state["status"] = "cancelled"

    sync_2 = client.post(
        f"/internal/readiness/merchants/{DEFAULT_ALPHA_MERCHANT_ID}/order-sync/{checkout_id}",
        json={"replay": True},
    )
    assert sync_2.status_code == 200
    sync_2_json = sync_2.json()
    assert sync_2_json["status"] == "cancelled"
    assert sync_2_json["replayed"] is True
    event_types = [event["event_type"] for event in sync_2_json["events"]]
    assert "merchant_cancellation_observed" in event_types
