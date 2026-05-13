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
        # P1-2: by default the product-key ownership check passes
        # (no keys are missing). Tests that exercise the validation
        # path set missing_keys_returns explicitly.
        self.missing_keys_returns: List[str] = []

    async def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        # P0-3: enqueue_audit_run_with_replay returns
        # (run_id, was_existing). Keep the legacy single-string
        # configuration for back-compat; tests that exercise the
        # race-replay path can set enqueue_returns to a tuple.
        if isinstance(self.enqueue_returns, tuple):
            return self.enqueue_returns
        return (self.enqueue_returns, False)

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

    async def missing_keys(self, *, merchant_id, product_keys):
        return list(self.missing_keys_returns)


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
        audit_runs_routes, "enqueue_audit_run_with_replay", stub.enqueue,
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
    monkeypatch.setattr(
        audit_runs_routes,
        "_missing_product_keys_for_merchant", stub.missing_keys,
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


def test_post_422_when_any_product_key_missing(client, stub):
    """P1-2 regression: when one or more product_keys are not owned by
    the authenticated merchant (or don't exist), POST returns 422
    with the missing keys listed — does NOT enqueue a doomed run."""
    stub.missing_keys_returns = ["pk-foreign", "pk-typo"]
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-1", "pk-foreign", "pk-typo"],
        },
    )
    assert res.status_code == 422
    detail = res.json().get("detail") or {}
    assert "missing_product_keys" in detail
    assert set(detail["missing_product_keys"]) == {"pk-foreign", "pk-typo"}
    # The route must NOT have enqueued anything.
    assert stub.enqueued == [], (
        "422 path must short-circuit before enqueue"
    )


def test_post_validation_runs_before_idempotency_lookup(client, stub):
    """Ordering guard: cross-tenant guard already runs first, but the
    product-key ownership check must also fire BEFORE the idempotency
    lookup. Otherwise a typo would still bump the daily-cap counter
    or hit the idempotency table on every retry."""
    stub.missing_keys_returns = ["pk-not-owned"]
    res = client.post(
        "/api/audits",
        json={
            "merchant_id": "merch-A",
            "product_keys": ["pk-not-owned"],
        },
    )
    assert res.status_code == 422
    assert stub.idem_lookups == [], (
        "Ownership-422 must short-circuit before find_in_flight is "
        "called — otherwise typos pollute the idempotency lookups"
    )


def test_post_happy_path_with_valid_keys_passes_ownership_check(client, stub):
    """Sanity: when missing_keys returns empty, the route proceeds
    to enqueue as before."""
    stub.missing_keys_returns = []
    res = client.post(
        "/api/audits",
        json={"merchant_id": "merch-A", "product_keys": ["pk-1"]},
    )
    assert res.status_code == 202
    assert len(stub.enqueued) == 1


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
# P0-4: audience auth restrictions on /api/audits/{run_id}?audience=
# =====================================================================


def test_get_merchant_audience_allowed(client, stub, monkeypatch):
    """Merchant JWT + ?audience=merchant — the one allowed projection."""
    stub.fetch_returns = _detail_row()
    from routes import audit_runs_routes  # noqa: F401

    async def fake_fetch_projection(*, audit_run_id, audience):
        return {"payload_jsonb": {"audience": "merchant",
                                  "action_queue": []}}

    from db import audit_evidence
    monkeypatch.setattr(
        audit_evidence, "fetch_projection", fake_fetch_projection,
    )
    res = client.get("/api/audits/r-1?audience=merchant")
    assert res.status_code == 200
    body = res.json()
    assert body.get("audience") == "merchant"


def test_get_rejects_internal_ops_audience_for_merchant_jwt(client, stub):
    """The bug: merchant JWT could fetch internal_ops projection of
    their own audit. Must now return 403 — not 200, not 404."""
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1?audience=internal_ops")
    assert res.status_code == 403
    detail = (res.json() or {}).get("detail") or ""
    assert "employee or admin" in detail.lower() or \
        "merchant jwts may only read" in detail.lower(), (
            f"403 detail should explain the auth requirement; got {detail}"
        )


def test_get_rejects_employee_bd_audience_for_merchant_jwt(client, stub):
    """employee_bd projection includes full evidence + cost detail.
    Must not be reachable via a merchant JWT."""
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1?audience=employee_bd")
    assert res.status_code == 403


def test_get_rejects_pivota_pdp_feed_audience_for_merchant_jwt(client, stub):
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1?audience=pivota_pdp_feed")
    assert res.status_code == 403


def test_get_rejects_frontend_agent_feed_audience_for_merchant_jwt(client, stub):
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1?audience=frontend_agent_feed")
    assert res.status_code == 403


def test_get_unknown_audience_returns_422_not_403(client, stub):
    """Schema validation runs BEFORE the role check — an unknown
    audience is a client error (422), not a permission error (403).
    Ordering matters so callers see a clear "fix your audience param"
    signal instead of a misleading 'employee auth required'."""
    stub.fetch_returns = _detail_row()
    res = client.get("/api/audits/r-1?audience=nonsense_audience")
    assert res.status_code == 422


def test_get_cross_tenant_with_internal_audience_still_returns_404(client, stub):
    """Cross-tenant + internal audience: the cross-tenant 404 must
    still win (don't leak existence). The audience-based 403 only
    fires for runs the merchant DOES own."""
    stub.fetch_returns = _detail_row(merchant_id="merch-OTHER")
    res = client.get("/api/audits/r-1?audience=internal_ops")
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
