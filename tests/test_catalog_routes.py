from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.catalog_routes as module
from utils.auth import get_current_user


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(module.router)
    # Merchant-scoped on purpose, NOT admin. These routes are ops-driven (see
    # the ADMIN_JWT block in docs/ops/CELESTIAL_PIVOT_MULTI_RELEASE_RUNBOOK.md),
    # so admin would have been the obvious fixture — but then every test in this
    # file would reach its subject through the admin bypass, and the
    # merchant-equality branch could break completely with this file still
    # green. Both tests below already act on `merch_1`. Refusal cases live in
    # tests/test_catalog_routes_tenant_scope.py.
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user_123",
        "email": "user@example.com",
        "role": "merchant",
        "merchant_id": "merch_1",
    }
    return app


def test_create_sync_job_route(monkeypatch) -> None:
    app = _build_app()

    async def fake_create_catalog_sync_job(**kwargs):
        return {
            "job_id": "job_123",
            "merchant_id": kwargs["merchant_id"],
            "connector": kwargs["connector"],
            "mode": kwargs["mode"],
            "status": "pending",
            "scope_json": kwargs["scope"],
            "stats_json": {},
            "error_message": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    monkeypatch.setattr(module, "create_catalog_sync_job", fake_create_catalog_sync_job)

    client = TestClient(app)
    response = client.post(
        "/v1/catalog/sync/jobs",
        json={
            "merchant_id": "merch_1",
            "connector": "shopify",
            "mode": "reconcile",
            "platform": "shopify",
            "force_refresh": True,
            "limit": 250,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == "job_123"
    assert payload["merchant_id"] == "merch_1"
    assert payload["connector"] == "shopify"
    assert payload["scope"]["limit"] == 250
    assert payload["scope"]["force_refresh"] is True


def test_reconcile_incentives_route(monkeypatch) -> None:
    app = _build_app()

    async def fake_reconcile_catalog_incentives_for_merchant(**kwargs):
        return {
            "merchant_id": kwargs["merchant_id"],
            "source_system": kwargs["source_system"],
            "payment_incentives_synced": 1,
            "offer_links_synced": 7,
            "reconciled_at": datetime.now(timezone.utc),
        }

    monkeypatch.setattr(module, "reconcile_catalog_incentives_for_merchant", fake_reconcile_catalog_incentives_for_merchant)

    client = TestClient(app)
    response = client.post(
        "/v1/catalog/incentives/reconcile/merch_1",
        json={
            "source_system": "merchant_config",
            "payment_incentives": [
                {
                    "incentive_type": "card_discount",
                    "label": "Mastercard 5% Off",
                    "benefit_kind": "percentage_off",
                    "benefit_value": "5.0",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["merchant_id"] == "merch_1"
    assert payload["source_system"] == "merchant_config"
    assert payload["payment_incentives_synced"] == 1
