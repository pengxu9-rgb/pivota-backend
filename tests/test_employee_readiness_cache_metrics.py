from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_client_with_employee_override():
    from routes import employee_dashboard_routes as module

    async def override_employee() -> Dict[str, Any]:
        return {"employee_id": "emp_test", "role": "employee", "email": "ops@pivota.cc"}

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_current_employee] = override_employee
    client = TestClient(app)
    return module, app, client


def test_employee_readiness_cache_metrics_filters_scope(monkeypatch):
    module, app, client = _build_client_with_employee_override()

    monkeypatch.setattr(
        module,
        "get_readiness_optimization_cache_metrics",
        lambda: {
            "hits": 8,
            "misses": 2,
            "stores": 4,
            "expired": 1,
            "refreshes": 3,
            "invalidations": 1,
            "invalidated_entries": 2,
            "total_requests": 10,
            "hit_rate": 80.0,
            "entries": 2,
            "ttl_seconds": 60.0,
            "active_keys": [
                {
                    "merchant_id": "merch_1",
                    "channel": "ucp",
                    "plan_id": "rdplan_1",
                    "snapshot_id": "rdsnap_1",
                    "age_seconds": 2.0,
                    "expires_in_seconds": 58.0,
                },
                {
                    "merchant_id": "merch_2",
                    "channel": "ucp",
                    "plan_id": "rdplan_2",
                    "snapshot_id": "rdsnap_2",
                    "age_seconds": 1.0,
                    "expires_in_seconds": 59.0,
                },
            ],
        },
    )

    response = client.get(
        "/employee/readiness/cache/optimization/metrics",
        params={"merchant_id": "merch_1", "channel": "ucp"},
        headers={"Authorization": "Bearer employee-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["hits"] == 8
    assert payload["data"]["scope"]["merchant_id"] == "merch_1"
    assert payload["data"]["scope"]["channel"] == "ucp"
    assert payload["data"]["scoped_entry_count"] == 1
    assert payload["data"]["has_active_entry"] is True
    assert payload["data"]["active_entry"]["plan_id"] == "rdplan_1"
    assert len(payload["data"]["scoped_active_keys"]) == 1

    app.dependency_overrides.clear()


def test_employee_readiness_cache_invalidate_returns_updated_metrics(monkeypatch):
    module, app, client = _build_client_with_employee_override()

    def fake_invalidate(*, merchant_id=None, channel=None):
        assert merchant_id == "merch_1"
        assert channel == "ucp"
        return 1

    monkeypatch.setattr(module, "invalidate_readiness_optimization_cache", fake_invalidate)
    monkeypatch.setattr(
        module,
        "get_readiness_optimization_cache_metrics",
        lambda: {
            "hits": 0,
            "misses": 0,
            "stores": 0,
            "expired": 0,
            "refreshes": 0,
            "invalidations": 1,
            "invalidated_entries": 1,
            "total_requests": 0,
            "hit_rate": 0.0,
            "entries": 0,
            "ttl_seconds": 60.0,
            "active_keys": [],
        },
    )

    response = client.post(
        "/employee/readiness/cache/optimization/invalidate",
        json={"merchant_id": "merch_1", "channel": "ucp"},
        headers={"Authorization": "Bearer employee-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["invalidated_entries"] == 1
    assert payload["data"]["scope"]["merchant_id"] == "merch_1"
    assert payload["data"]["cache_metrics"]["invalidations"] == 1
    assert payload["data"]["cache_metrics"]["scoped_entry_count"] == 0

    app.dependency_overrides.clear()


def test_employee_merchant_integrations_route(monkeypatch):
    module, app, client = _build_client_with_employee_override()

    async def fake_fetch(merchant_id: str):
        assert merchant_id == "merch_1"
        return [{"id": "store_1", "platform": "shopify", "name": "Main Store"}]

    monkeypatch.setattr(module, "_fetch_employee_merchant_stores", fake_fetch)

    response = client.get(
        "/employee/merchant/merch_1/integrations",
        headers={"Authorization": "Bearer employee-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["stores"][0]["id"] == "store_1"

    app.dependency_overrides.clear()


def test_employee_merchant_psps_route(monkeypatch):
    module, app, client = _build_client_with_employee_override()

    async def fake_fetch(merchant_id: str):
        assert merchant_id == "merch_1"
        return [{"id": "psp_1", "provider": "stripe", "status": "active"}]

    monkeypatch.setattr(module, "_fetch_employee_merchant_psps", fake_fetch)

    response = client.get(
        "/employee/merchant/merch_1/psps",
        headers={"Authorization": "Bearer employee-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["psps"][0]["provider"] == "stripe"

    app.dependency_overrides.clear()


def test_employee_merchant_analytics_route(monkeypatch):
    module, app, client = _build_client_with_employee_override()

    async def fake_fetch(merchant_id: str):
        assert merchant_id == "merch_1"
        return {"total_orders": 5, "total_products": 8, "gmv": 12.5}

    monkeypatch.setattr(module, "_fetch_employee_merchant_analytics", fake_fetch)

    response = client.get(
        "/employee/merchant/merch_1/analytics",
        headers={"Authorization": "Bearer employee-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["total_orders"] == 5
    assert payload["data"]["total_products"] == 8

    app.dependency_overrides.clear()


def test_summarize_employee_merchant_commerce_readiness_red_when_core_prereqs_missing():
    from routes import employee_dashboard_routes as module

    summary = module._summarize_employee_merchant_commerce_readiness(
        "merch_red",
        stores=[],
        psps=[],
        analytics={"total_products": 0},
    )

    assert summary["status"] == "red"
    assert summary["merchant_valid"] is False
    assert summary["rollout_ready"] is False
    assert "missing_connected_store_domain" in summary["invalid_reasons"]
    assert "missing_catalog_sync" in summary["invalid_reasons"]
    assert "missing_psp_or_checkout" in summary["invalid_reasons"]
    assert summary["checklist"][3]["state"] == "unproven"


def test_summarize_employee_merchant_commerce_readiness_yellow_when_loop_unproven():
    from routes import employee_dashboard_routes as module

    summary = module._summarize_employee_merchant_commerce_readiness(
        "merch_yellow",
        stores=[
            {
                "domain": "shop.example.com",
                "is_connected": True,
                "status": "active",
                "api_key_present": True,
            }
        ],
        psps=[
            {
                "provider": "stripe",
                "status": "active",
                "configured": True,
            }
        ],
        analytics={
            "total_products": 24,
            "order_breakdown": {"total": 0, "paid": 0, "all_time_paid": 0},
            "revenue_breakdown": {"confirmed": 0, "all_time_confirmed": 0},
        },
    )

    assert summary["status"] == "yellow"
    assert summary["merchant_valid"] is True
    assert summary["rollout_ready"] is False
    assert summary["invalid_reasons"] == []
    assert summary["checklist"][0]["state"] == "ready"
    assert summary["checklist"][1]["state"] == "ready"
    assert summary["checklist"][2]["state"] == "ready"
    assert summary["checklist"][3]["state"] == "unproven"


def test_summarize_employee_merchant_commerce_readiness_green_when_loop_observed():
    from routes import employee_dashboard_routes as module

    summary = module._summarize_employee_merchant_commerce_readiness(
        "merch_green",
        stores=[
            {
                "domain": "shop.example.com",
                "is_connected": True,
                "status": "active",
                "api_key_present": True,
            }
        ],
        psps=[
            {
                "provider": "stripe",
                "status": "active",
                "configured": True,
            }
        ],
        analytics={
            "total_products": 24,
            "order_breakdown": {"total": 6, "paid": 4, "all_time_paid": 12},
            "revenue_breakdown": {"confirmed": 120.0, "all_time_confirmed": 420.0},
            "total_orders": 6,
            "total_payments_succeeded": 4,
            "confirmed_revenue": 120.0,
        },
    )

    assert summary["status"] == "green"
    assert summary["merchant_valid"] is True
    assert summary["rollout_ready"] is True
    assert summary["paid_orders_last_30_days"] == 4
    assert summary["all_time_paid_orders"] == 12
    assert summary["checklist"][3]["state"] == "ready"


def test_employee_merchant_commerce_readiness_route(monkeypatch):
    module, app, client = _build_client_with_employee_override()

    async def fake_build(merchant_id: str):
        assert merchant_id == "merch_1"
        return {
            "merchant_id": merchant_id,
            "generated_at": "2026-03-20T00:00:00+00:00",
            "status": "yellow",
            "merchant_valid": True,
            "rollout_ready": False,
            "operator_action": "Run a real order and verify payment completion before rollout.",
            "invalid_reasons": [],
            "connected_store_count": 1,
            "connected_store_domain_count": 1,
            "connected_store_domains": ["shop.example.com"],
            "catalog_product_count": 24,
            "psp_connected": True,
            "psp_provider_count": 1,
            "psp_providers": ["stripe"],
            "orders_last_30_days": 0,
            "paid_orders_last_30_days": 0,
            "confirmed_revenue_last_30_days": 0.0,
            "all_time_paid_orders": 0,
            "all_time_confirmed_revenue": 0.0,
            "checklist": [
                {"key": "store_domain_connected", "label": "Connected store domain", "state": "ready", "detail": "1 connected domains in scope."},
                {"key": "catalog_synced", "label": "Catalog synced", "state": "ready", "detail": "24 active catalog products in products_cache."},
                {"key": "psp_or_checkout_connected", "label": "PSP or checkout connected", "state": "ready", "detail": "1 configured PSP connections: stripe."},
                {"key": "order_payment_loop_observed", "label": "Order/payment loop observed", "state": "unproven", "detail": "No paid order or confirmed revenue evidence observed yet."},
            ],
        }

    monkeypatch.setattr(module, "build_employee_merchant_commerce_readiness", fake_build)

    response = client.get(
        "/employee/merchant/merch_1/commerce-readiness",
        headers={"Authorization": "Bearer employee-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["status"] == "yellow"
    assert payload["data"]["merchant_valid"] is True
    assert payload["data"]["checklist"][3]["state"] == "unproven"

    app.dependency_overrides.clear()


def test_employee_referral_readiness_summary_route(monkeypatch):
    module, app, client = _build_client_with_employee_override()

    async def fake_build(merchant_id: str):
        assert merchant_id == "merch_1"
        return {
            "merchant_id": merchant_id,
            "status": "yellow",
            "gating_policy_version": "external_referral_v1",
            "matched_domains": ["example.com"],
            "total_active_seeds": 3,
            "attached_seed_count": 2,
            "domain_unattached_seed_count": 1,
            "healthy_seed_count": 1,
            "blocked_seed_count": 1,
            "review_seed_count": 1,
            "issue_buckets": [{"issue_type": "stale_snapshot", "severity": "blocker", "count": 1}],
            "sample_blocked_seeds": [{"seed_id": "eps_1"}],
            "last_extracted_at_oldest": "2026-03-10T00:00:00+00:00",
            "last_extracted_at_newest": "2026-03-19T00:00:00+00:00",
        }

    monkeypatch.setattr(module, "build_external_referral_summary", fake_build)

    response = client.get(
        "/employee/referral-readiness/summary",
        params={"merchant_id": "merch_1"},
        headers={"Authorization": "Bearer employee-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["status"] == "yellow"
    assert payload["data"]["blocked_seed_count"] == 1

    app.dependency_overrides.clear()


def test_employee_referral_program_summary_route(monkeypatch):
    module, app, client = _build_client_with_employee_override()

    async def fake_build():
        return {
            "status": "yellow",
            "generated_at": "2026-03-20T00:00:00+00:00",
            "gating_policy_version": "external_referral_v1",
            "total_active_seeds": 50,
            "healthy_seed_count": 45,
            "blocked_seed_count": 3,
            "review_seed_count": 2,
            "attached_seed_count": 30,
            "unattached_seed_count": 20,
            "issue_buckets": [{"issue_type": "stale_snapshot", "severity": "blocker", "count": 3}],
            "top_domains": [{"domain": "example.com", "count": 25}],
            "top_blocked_domains": [{"domain": "blocked.example", "count": 2}],
            "runtime_surface_coverage_summary": {"surface_eligible_rate_pct": 94.0},
        }

    monkeypatch.setattr(module, "build_platform_fallback_program_summary", fake_build)

    response = client.get(
        "/employee/referral-readiness/program-summary",
        headers={"Authorization": "Bearer employee-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["status"] == "yellow"
    assert payload["data"]["total_active_seeds"] == 50
    assert payload["data"]["top_domains"][0]["domain"] == "example.com"

    app.dependency_overrides.clear()


def test_employee_merchant_commerce_cohort_summary_route(monkeypatch):
    module, app, client = _build_client_with_employee_override()

    async def fake_build():
        return {
            "generated_at": "2026-03-20T00:00:00+00:00",
            "total_registered_merchants": 17,
            "store_connected_merchants": 1,
            "store_connected_with_psp_merchants": 1,
            "merchant_valid_count": 1,
            "merchant_invalid_count": 16,
            "top_invalid_merchants": [{"merchant_id": "merch_2", "invalid_reasons": ["missing_catalog_sync"]}],
        }

    monkeypatch.setattr(module, "build_merchant_commerce_cohort_summary", fake_build)

    response = client.get(
        "/employee/referral-readiness/merchant-commerce-cohort",
        headers={"Authorization": "Bearer employee-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["merchant_valid_count"] == 1
    assert payload["data"]["top_invalid_merchants"][0]["merchant_id"] == "merch_2"

    app.dependency_overrides.clear()


def test_employee_referral_readiness_fleet_summary_route_alias(monkeypatch):
    module, app, client = _build_client_with_employee_override()

    async def fake_build():
        return {
            "generated_at": "2026-03-20T00:00:00+00:00",
            "total_registered_merchants": 17,
            "store_connected_merchants": 1,
            "store_connected_with_psp_merchants": 1,
            "merchant_valid_count": 1,
            "merchant_invalid_count": 16,
            "top_invalid_merchants": [{"merchant_id": "merch_2", "invalid_reasons": ["missing_store_domain"]}],
        }

    monkeypatch.setattr(module, "build_external_referral_fleet_summary", fake_build)

    response = client.get(
        "/employee/referral-readiness/fleet-summary",
        headers={"Authorization": "Bearer employee-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["merchant_invalid_count"] == 16
    assert payload["data"]["top_invalid_merchants"][0]["merchant_id"] == "merch_2"

    app.dependency_overrides.clear()
