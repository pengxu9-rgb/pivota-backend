from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.admin_partner_cohort as module
from utils.auth import require_admin


def _build_app(*, admin: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(module.router)
    if admin:
        app.dependency_overrides[require_admin] = lambda: {
            "sub": "admin_123",
            "email": "admin@example.com",
            "role": "admin",
        }
    return app


def test_cohort_dashboard_requires_admin() -> None:
    app = _build_app(admin=False)
    client = TestClient(app)

    response = client.get("/admin/partners/1/cohort")

    assert response.status_code in {401, 403}


def test_cohort_dashboard_returns_target_progress(
    monkeypatch,
) -> None:
    async def fake_progress(channel_partner_id: int) -> list[dict[str, Any]]:
        assert channel_partner_id == 1
        return [
            {
                "id": 10,
                "label": "20 brands in 12 months",
                "target_brand_count": 20,
                "current_count": 15,
                "window_start_date": date(2025, 1, 1),
                "window_end_date": date(2026, 1, 1),
                "window_open": True,
                "status": "open",
                "achieved_at": None,
                "paid_at": None,
                "bonus_cents": 0,
                "days_remaining": 214,
            }
        ]

    monkeypatch.setattr(
        module.cohort_target_evaluator,
        "get_partner_target_progress",
        fake_progress,
    )
    app = _build_app(admin=True)
    client = TestClient(app)

    response = client.get("/admin/partners/1/cohort")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "channel_partner_id": 1,
        "targets": [
            {
                "id": 10,
                "label": "20 brands in 12 months",
                "target_brand_count": 20,
                "current_count": 15,
                "window_start_date": "2025-01-01",
                "window_end_date": "2026-01-01",
                "window_open": True,
                "status": "open",
                "achieved_at": None,
                "paid_at": None,
                "bonus_cents": 0,
                "days_remaining": 214,
            }
        ],
    }
