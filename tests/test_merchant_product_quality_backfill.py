from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_client():
    import routes.merchant_products as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_current_user():
        return {
            "role": "merchant",
            "merchant_id": "merch_test",
            "user_id": "merchant_user",
            "email": "merchant@example.com",
        }

    app.dependency_overrides[module.get_current_user] = fake_current_user
    return TestClient(app), module


def test_queue_quality_backfill_creates_job(monkeypatch) -> None:
    client, module = _build_client()

    async def fake_get_active_quality_backfill_job(_merchant_id, platform=None):
        return None

    async def fake_create_quality_backfill_job(**kwargs):
        return {
            "job_id": "qbf_test",
            "merchant_id": kwargs["merchant_id"],
            "platform": kwargs["platform"],
            "status": "queued",
            "requested_by": kwargs["requested_by"],
            "force_refresh": kwargs["force_refresh"],
            "missing_only": kwargs["missing_only"],
            "requested_at": "2026-03-19T00:00:00Z",
        }

    async def fake_process_quality_backfill_job(_job_id):
        return None

    monkeypatch.setattr(module, "get_active_quality_backfill_job", fake_get_active_quality_backfill_job)
    monkeypatch.setattr(module, "create_quality_backfill_job", fake_create_quality_backfill_job)
    monkeypatch.setattr(module, "process_quality_backfill_job", fake_process_quality_backfill_job)

    response = client.post(
        "/merchant/products/quality/backfill",
        json={"platform": "shopify", "force_refresh": False, "missing_only": True},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["data"]["already_active"] is False
    assert body["data"]["job"]["job_id"] == "qbf_test"
    assert body["data"]["job"]["platform"] == "shopify"


def test_get_quality_backfill_status_returns_job_for_current_merchant(monkeypatch) -> None:
    client, module = _build_client()

    async def fake_get_quality_backfill_job(job_id):
        assert job_id == "qbf_test"
        return {
            "job_id": job_id,
            "merchant_id": "merch_test",
            "platform": "shopify",
            "status": "running",
            "processed": 12,
            "total_candidates": 40,
        }

    monkeypatch.setattr(module, "get_quality_backfill_job", fake_get_quality_backfill_job)

    response = client.get("/merchant/products/quality/backfill/qbf_test")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["status"] == "running"
    assert body["data"]["processed"] == 12
