"""Phase 2.3 — /api/audits endpoint tests.

Strategy: spin up a FastAPI app with just the audit_runs router +
override the auth dep + monkey-patch the DB accessors. Validates the
HTTP surface (status codes, body shape, idempotency, cross-tenant
guard) without touching Postgres.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# =====================================================================
# Test app + accessor stub
# =====================================================================


class _AccessorStub:
    """Records every accessor call so tests can assert behavior.
    Each accessor is overridable via the *_returns dicts."""

    def __init__(self):
        self.enqueued: List[Dict[str, Any]] = []
        self.cancelled: List[str] = []
        self.idem_lookups: List[str] = []

        # Configurable returns — tests set these before requests.
        self.enqueue_returns: Optional[str] = "run-new-1"
        self.idem_returns: Optional[str] = None
        self.fetch_returns: Optional[Dict[str, Any]] = None
        self.list_returns: List[Dict[str, Any]] = []
        self.cancel_returns: bool = True

    async def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return self.enqueue_returns

    async def find_idem(self, *, idempotency_key):
        self.idem_lookups.append(idempotency_key)
        return self.idem_returns

    async def fetch(self, *, run_id):
        return self.fetch_returns

    async def cancel(self, *, run_id):
        self.cancelled.append(run_id)
        return self.cancel_returns

    async def recent(self, *, merchant_id, limit):
        return self.list_returns


@pytest.fixture
def stub():
    return _AccessorStub()


@pytest.fixture
def client(stub, monkeypatch):
    """Mount the audit_runs router in an isolated app + patch every
    DB accessor + auth dep so tests don't need Postgres."""
    from routes import audit_runs_routes
    from utils import auth as auth_module

    monkeypatch.setattr(
        audit_runs_routes, "enqueue_audit_run", stub.enqueue,
    )
    monkeypatch.setattr(
        audit_runs_routes, "find_in_flight_by_idempotency_key",
        stub.find_idem,
    )
    monkeypatch.setattr(
        audit_runs_routes, "fetch_audit_run_by_id", stub.fetch,
    )
    monkeypatch.setattr(
        audit_runs_routes, "cancel_audit_run", stub.cancel,
    )
    monkeypatch.setattr(
        audit_runs_routes, "recent_runs_for_merchant", stub.recent,
    )

    app = FastAPI()
    app.include_router(audit_runs_routes.router)

    # Auth override: every request authenticates as merch-A.
    app.dependency_overrides[auth_module.get_current_merchant] = (
        lambda: "merch-A"
    )
    return TestClient(app)


# =====================================================================
# POST /api/audits
# =====================================================================


def test_post_enqueues_and_returns_202(client, stub):
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1", "pk-2"],
        },
    )
    assert res.status_code == 202
    body = res.json()
    assert body["run_id"] == "run-new-1"
    assert body["stage"] == "queued"
    assert body["idempotent_replay"] is False
    assert len(stub.enqueued) == 1
    assert stub.enqueued[0]["merchant_id"] == "merch-A"
    assert stub.enqueued[0]["product_keys"] == ["pk-1", "pk-2"]
    # Idempotency lookup happened (default force=False).
    assert len(stub.idem_lookups) == 1


def test_post_returns_existing_run_on_idempotent_replay(client, stub):
    stub.idem_returns = "run-already-running"
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 202
    body = res.json()
    assert body["run_id"] == "run-already-running"
    assert body["idempotent_replay"] is True
    # Worker did NOT enqueue — replayed instead.
    assert stub.enqueued == []


def test_post_force_skips_idempotency_dedupe(client, stub):
    stub.idem_returns = "run-already-running"
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
            "force": True,
        },
    )
    assert res.status_code == 202
    body = res.json()
    assert body["idempotent_replay"] is False
    # No idempotency lookup happened.
    assert stub.idem_lookups == []
    # Worker enqueued with idempotency_key=None.
    assert len(stub.enqueued) == 1
    assert stub.enqueued[0]["idempotency_key"] is None


def test_post_rejects_cross_tenant_merchant_id(client, stub):
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-OTHER",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 403
    assert stub.enqueued == []


def test_post_rejects_cold_start_subject_for_merchant_auth(client, stub):
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
            "subject_type": "cold_start",
        },
    )
    assert res.status_code == 403
    assert stub.enqueued == []


def test_post_rejects_too_many_products(client, stub):
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1", "pk-2", "pk-3", "pk-4", "pk-5", "pk-6"],
        },
    )
    assert res.status_code == 422


def test_post_returns_503_on_persistence_failure(client, stub):
    stub.enqueue_returns = None
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1"],
        },
    )
    assert res.status_code == 503


# =====================================================================
# GET /api/audits/{run_id}
# =====================================================================


def _detail_row(run_id: str = "r-1", stage: str = "completed",
                merchant_id: str = "merch-A") -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "merchant_id": merchant_id,
        "subject_type": "merchant",
        "stage": stage,
        "stage_updated_at": "2026-05-09T12:00:00+00:00",
        "requested_at": "2026-05-09T11:55:00+00:00",
        "completed_at": "2026-05-09T12:00:00+00:00",
        "cancelled_at": None,
        "product_keys": ["pk-1"],
        "verdict_labels": ["VISIBLE VIA RETAILERS"],
        "visibility_score_avg": 67,
        "attribution_score_avg": 25,
        "category_visibility_score_avg": 60,
        "audited_via_pivota_canonical": [],
        "partial_result_jsonb": None,
        "report_jsonb": {"merchant_name": "Test"},
        "cost_summary_jsonb": None,
        "error_jsonb": None,
        "error_message": None,
        "idempotency_key": "idem-x",
    }


def test_get_returns_canonical_shape(client, stub):
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1")
    assert res.status_code == 200
    body = res.json()
    assert body["run_id"] == "r-1"
    assert body["stage"] == "completed"
    assert body["report_jsonb"]["merchant_name"] == "Test"


def test_get_404_when_not_found(client, stub):
    stub.fetch_returns = None
    res = client.get("/api/audits/nonexistent")
    assert res.status_code == 404


def test_get_404_for_cross_tenant_run(client, stub):
    stub.fetch_returns = _detail_row(merchant_id="merch-OTHER")
    res = client.get("/api/audits/r-1")
    # Don't leak existence — cross-tenant looks identical to not-found.
    assert res.status_code == 404


# =====================================================================
# POST /api/audits/{run_id}/cancel
# =====================================================================


def test_cancel_active_run_succeeds(client, stub):
    stub.fetch_returns = _detail_row(stage="probing")
    res = client.post("/api/audits/r-1/cancel")
    assert res.status_code == 202
    body = res.json()
    assert body["cancellation_requested"] is True
    assert body["current_stage"] == "probing"
    assert stub.cancelled == ["r-1"]


def test_cancel_terminal_run_is_noop(client, stub):
    stub.fetch_returns = _detail_row(stage="completed")
    res = client.post("/api/audits/r-1/cancel")
    assert res.status_code == 202
    body = res.json()
    assert body["cancellation_requested"] is False
    assert "terminal" in body["reason"].lower()
    assert stub.cancelled == []


def test_cancel_404_for_cross_tenant_run(client, stub):
    stub.fetch_returns = _detail_row(merchant_id="merch-OTHER",
                                      stage="probing")
    res = client.post("/api/audits/r-1/cancel")
    assert res.status_code == 404
    assert stub.cancelled == []


# =====================================================================
# GET /api/audits (list)
# =====================================================================


def test_list_returns_recent_runs(client, stub):
    stub.list_returns = [
        {"run_id": "r-1", "status": "succeeded"},
        {"run_id": "r-2", "status": "running"},
    ]
    res = client.get("/api/audits")
    assert res.status_code == 200
    assert len(res.json()) == 2


def test_list_rejects_invalid_limit(client, stub):
    res = client.get("/api/audits?limit=999")
    assert res.status_code == 422
    res = client.get("/api/audits?limit=0")
    assert res.status_code == 422
