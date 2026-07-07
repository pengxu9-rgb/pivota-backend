from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

import routes.admin_partners as module


def _partner_row(partner_id: int, status: str) -> dict[str, Any]:
    return {
        "id": partner_id,
        "legal_name": f"Partner {partner_id}",
        "archetype": "agency",
        "status": status,
        "term_start_date": None,
        "term_months": 12,
        "auto_renew": True,
        "per_brand_tail_months": 36,
        "churn_clawback_days": 90,
        "nonpayment_clawback_days": 60,
        "per_brand_subsidy_cap_cents": 500000,
        "gmv_take_rate_bp": 1000,
        "gmv_take_definition": "net",
        "stripe_connect_account_id": None,
    }


class _FakeDb:
    def __init__(self) -> None:
        self.partners: dict[int, dict[str, Any]] = {}

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        sql = " ".join(query.split()).lower()
        params = dict(values or {})
        pid = int(params.get("id") or params.get("channel_partner_id"))
        if "from channel_partners cp" in sql:  # get_admin_partner
            row = self.partners.get(pid)
            if row is None:
                return None
            return {**row, "active_brand_count": 0, "ytd_gmv_cents": 0}
        if "from channel_partners" in sql:  # existence check
            return {"id": pid} if pid in self.partners else None
        raise AssertionError(f"Unhandled fetch_one: {query}")

    async def execute(self, query: str, values: dict[str, Any] | None = None):
        sql = " ".join(query.split()).lower()
        params = dict(values or {})
        if sql.startswith("update channel_partners set status"):
            pid = int(params["id"])
            if pid in self.partners:
                self.partners[pid]["status"] = params["status"]
            return None
        raise AssertionError(f"Unhandled execute: {query}")


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeDb:
    db = _FakeDb()
    monkeypatch.setattr(module, "database", db)

    async def _no_cohort(channel_partner_id: int) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(
        module.cohort_target_evaluator, "get_partner_target_progress", _no_cohort
    )
    return db


def _client(*, authed: bool = True) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(module.router)
    if authed:
        app.dependency_overrides[module.require_admin] = lambda: {
            "email": "admin@example.com",
            "role": "admin",
        }
    return TestClient(app), app


def test_deactivate_sets_status_inactive(fake_db: _FakeDb) -> None:
    fake_db.partners[21] = _partner_row(21, "active")
    client, app = _client()
    try:
        r = client.post("/admin/partners/21/deactivate")
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 200
    assert r.json()["status"] == "inactive"
    assert fake_db.partners[21]["status"] == "inactive"


def test_reactivate_sets_status_active(fake_db: _FakeDb) -> None:
    fake_db.partners[21] = _partner_row(21, "inactive")
    client, app = _client()
    try:
        r = client.post("/admin/partners/21/reactivate")
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 200
    assert r.json()["status"] == "active"
    assert fake_db.partners[21]["status"] == "active"


def test_deactivate_unknown_partner_404(fake_db: _FakeDb) -> None:
    client, app = _client()
    try:
        r = client.post("/admin/partners/999/deactivate")
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 404
    assert r.json()["error"] == "partner_not_found"


def test_deactivate_requires_admin(fake_db: _FakeDb) -> None:
    fake_db.partners[21] = _partner_row(21, "active")
    client, app = _client(authed=False)
    try:
        r = client.post("/admin/partners/21/deactivate")
    finally:
        app.dependency_overrides.clear()

    assert r.status_code in {401, 403}
