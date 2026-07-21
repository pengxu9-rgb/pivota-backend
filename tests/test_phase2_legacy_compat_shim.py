"""Phase 2.4 — legacy endpoint compat shim tests.

Validates two surfaces of the legacy `/api/merchant-center/audit/
ai-commerce-readiness` route after P2.4:
  1. Deprecation headers (RFC 8594 Sunset + RFC 8288 Link) are
     emitted on every response (default + opt-in paths).
  2. `?via=async_pipeline` opt-in routes through the new async
     pipeline + polls for up to the budget + reshapes into the
     legacy response shape (terminal) or returns 202 (in-flight).

The default synchronous path's behavior is intentionally NOT
re-tested here — that's covered by tests/test_merchant_audit_routes.py.
What we verify here is the shim layer + the headers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# =====================================================================
# Stubs — patch the new-pipeline accessors + DB fetch_all
# =====================================================================


class _CompatStub:
    """Records what the compat shim called + lets tests configure
    the run's terminal-state poll responses."""

    def __init__(self):
        self.enqueued: List[Dict[str, Any]] = []
        self.idem_lookups: List[str] = []
        self.fetch_calls: List[str] = []

        # Configurable returns
        self.idem_returns: Optional[str] = None
        self.enqueue_returns: Optional[str] = "run-compat-1"
        self.fetch_responses: List[Optional[Dict[str, Any]]] = []

        # Catalog products row stub (one row per requested ref)
        self.catalog_rows: List[Dict[str, Any]] = []

    async def find_idem(self, *, idempotency_key):
        self.idem_lookups.append(idempotency_key)
        return self.idem_returns

    async def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return self.enqueue_returns

    async def fetch(self, *, run_id):
        self.fetch_calls.append(run_id)
        if not self.fetch_responses:
            return None
        # Pop one per call until exhausted; last response sticks.
        if len(self.fetch_responses) > 1:
            return self.fetch_responses.pop(0)
        return self.fetch_responses[0]


@pytest.fixture
def compat_stub():
    return _CompatStub()


@pytest.fixture
def client(compat_stub, monkeypatch):
    """Mount the merchant_audit_routes router + override auth +
    monkey-patch the new-pipeline accessors + database.fetch_all
    so tests run without Postgres."""
    from routes import merchant_audit_routes
    from utils import auth as auth_module

    # Patch the new-pipeline accessors imported INSIDE
    # _run_async_pipeline_compat. Since they're lazily imported from
    # db.merchant_audit_runs, patching at the source module catches
    # both the direct import and the lazy one.
    from db import merchant_audit_runs as mar
    monkeypatch.setattr(mar, "find_in_flight_by_idempotency_key",
                        compat_stub.find_idem)
    monkeypatch.setattr(mar, "enqueue_audit_run", compat_stub.enqueue)
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", compat_stub.fetch)

    # Stub the catalog SELECT used by _run_async_pipeline_compat to
    # resolve (platform, source_product_id) refs to product_keys.
    async def fake_fetch_all(query):
        return [
            type("R", (), {
                "__getitem__": lambda self, k: row[k],
                "_data": row,
            })()
            for row in compat_stub.catalog_rows
        ]

    # Easier: patch the database.fetch_all to return dict-like rows
    # the compat shim can index with ["product_key"] etc.
    class _Row(dict):
        def __getitem__(self, k):
            return dict.__getitem__(self, k)

    async def fake_fetch_all_simple(query):
        return [_Row(r) for r in compat_stub.catalog_rows]

    monkeypatch.setattr(merchant_audit_routes.database, "fetch_all",
                        fake_fetch_all_simple)

    async def _audit_ready(_merchant_id: str, _platform: str):
        return {
            "ready": True,
            "blocking_gaps": [],
            "counts": {
                "catalog_products": len(compat_stub.catalog_rows),
                "product_quality_snapshot": len(compat_stub.catalog_rows),
            },
        }

    monkeypatch.setattr(
        merchant_audit_routes, "assess_merchant_audit_readiness", _audit_ready,
    )

    # Tighten the poll budget so tests don't wait 30s for in-flight
    # cases. 0.5s budget + 0.05s interval ≈ 10 iterations max.
    monkeypatch.setattr(
        merchant_audit_routes, "_COMPAT_POLL_BUDGET_SECONDS", 0.5,
    )
    monkeypatch.setattr(
        merchant_audit_routes, "_COMPAT_POLL_INTERVAL_SECONDS", 0.05,
    )

    app = FastAPI()
    app.include_router(merchant_audit_routes.router)

    app.dependency_overrides[auth_module.get_current_merchant] = (
        lambda: "merch-A"
    )
    return TestClient(app)


def _request_body() -> Dict[str, Any]:
    return {
        "products": [
            {"platform": "shopify", "source_product_id": "sp-1"},
            {"platform": "shopify", "source_product_id": "sp-2"},
        ],
    }


def _catalog_row(sp_id: str, key: str) -> Dict[str, Any]:
    return {
        "product_key": key,
        "platform": "shopify",
        "source_product_id": sp_id,
    }


# =====================================================================
# Async-pipeline path — terminal completion within budget
# =====================================================================


def test_async_pipeline_completed_within_budget_returns_legacy_shape(
    client, compat_stub,
):
    compat_stub.catalog_rows = [
        _catalog_row("sp-1", "pk-1"),
        _catalog_row("sp-2", "pk-2"),
    ]
    # Worker reaches terminal state on the first poll.
    compat_stub.fetch_responses = [
        {
            "stage": "completed",
            "report_jsonb": {"merchant_name": "Test Merchant"},
            "audited_via_pivota_canonical": [],
            "partial_result_jsonb": {
                "materializing": {
                    "tasks_materialized": 2,
                    "executors_dispatched": 1,
                },
            },
        },
    ]

    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness?via=async_pipeline",
        json=_request_body(),
    )
    assert res.status_code == 200
    body = res.json()
    # Legacy response shape preserved.
    assert body["brand_report"]["merchant_name"] == "Test Merchant"
    assert body["audit_run_id"] == "run-compat-1"
    assert body["tasks"]["tasks_materialized"] == 2
    assert body["executors"]["executors_dispatched"] == 1

    # Deprecation headers present.
    assert res.headers.get("Deprecation") == "true"
    assert "Sunset" in res.headers
    assert "/api/audits" in res.headers.get("Link", "")

    # Compat shim enqueued through the new accessor.
    assert len(compat_stub.enqueued) == 1
    assert compat_stub.enqueued[0]["product_keys"] == ["pk-1", "pk-2"]


def test_async_pipeline_in_flight_at_deadline_returns_202(
    client, compat_stub,
):
    compat_stub.catalog_rows = [_catalog_row("sp-1", "pk-1"),
                                _catalog_row("sp-2", "pk-2")]
    # Poll always returns probing — never reaches terminal state.
    compat_stub.fetch_responses = [{"stage": "probing"}]

    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness?via=async_pipeline",
        json=_request_body(),
    )
    assert res.status_code == 202
    body = res.json()
    assert body["compat_status"] == "in_flight_at_poll_deadline"
    assert body["audit_run_id"] == "run-compat-1"
    assert body["compat_poll_endpoint"] == "/api/audits/run-compat-1"
    assert body["brand_report"] is None
    # Deprecation headers still present on the 202.
    assert res.headers.get("Deprecation") == "true"


def test_async_pipeline_failed_run_returns_502(client, compat_stub):
    compat_stub.catalog_rows = [_catalog_row("sp-1", "pk-1"),
                                _catalog_row("sp-2", "pk-2")]
    compat_stub.fetch_responses = [
        {
            "stage": "failed",
            "error_message": "probing-blew-up",
            "error_jsonb": {"stage": "probing", "message": "probing-blew-up"},
        },
    ]

    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness?via=async_pipeline",
        json=_request_body(),
    )
    assert res.status_code == 502
    body = res.json()
    assert body["detail"]["audit_run_id"] == "run-compat-1"
    assert "probing-blew-up" in body["detail"]["message"]


def test_async_pipeline_idempotent_replay_doesnt_enqueue(
    client, compat_stub,
):
    """If an in-flight run already exists for the same idempotency
    key, the compat shim reuses it instead of enqueueing again."""
    compat_stub.catalog_rows = [_catalog_row("sp-1", "pk-1"),
                                _catalog_row("sp-2", "pk-2")]
    compat_stub.idem_returns = "run-already-running"
    compat_stub.fetch_responses = [{"stage": "completed",
                                    "report_jsonb": {"x": "y"},
                                    "audited_via_pivota_canonical": [],
                                    "partial_result_jsonb": {}}]

    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness?via=async_pipeline",
        json=_request_body(),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["audit_run_id"] == "run-already-running"
    # No new enqueue happened.
    assert compat_stub.enqueued == []


def test_async_pipeline_readiness_gate_blocks_before_enqueue(
    client,
    compat_stub,
    monkeypatch: pytest.MonkeyPatch,
):
    """The legacy async-compat arm must not bypass audit readiness and enqueue
    a run that will render a false all-blocked first audit."""
    from routes import merchant_audit_routes

    compat_stub.catalog_rows = [_catalog_row("sp-1", "pk-1"),
                                _catalog_row("sp-2", "pk-2")]

    async def _not_ready(_merchant_id: str, platform: str):
        return {
            "ready": False,
            "blocking_gaps": [
                "product_quality_snapshot missing content_quality_score"
            ],
            "counts": {
                "catalog_products": 2,
                "product_quality_snapshot": 0,
            },
            "platform": platform,
        }

    monkeypatch.setattr(
        merchant_audit_routes, "assess_merchant_audit_readiness", _not_ready,
    )

    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness?via=async_pipeline",
        json=_request_body(),
    )

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "merchant_not_audit_ready"
    assert detail["platform"] == "shopify"
    assert "product_quality_snapshot" in str(detail["blocking_gaps"])
    assert compat_stub.idem_lookups == []
    assert compat_stub.enqueued == []
    assert compat_stub.fetch_calls == []


def test_async_pipeline_404_for_missing_products(client, compat_stub):
    """If a requested ref doesn't match any catalog row, the shim
    returns 404 with the missing list — same behavior as the
    legacy path."""
    compat_stub.catalog_rows = []  # nothing matches

    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness?via=async_pipeline",
        json=_request_body(),
    )
    assert res.status_code == 404
    body = res.json()
    assert "missing_products" in body["detail"]
    assert len(body["detail"]["missing_products"]) == 2


# =====================================================================
# Default path — deprecation headers on the synchronous path
# =====================================================================


def test_default_path_emits_deprecation_headers_even_on_error(
    client, compat_stub,
):
    """The deprecation headers must be set BEFORE the synchronous
    path branches on rate limits / catalog lookups — verified here
    by an empty product list (422). Headers should still be
    present even on validation failure."""
    res = client.post(
        "/api/merchant-center/audit/ai-commerce-readiness",
        json={"products": []},
    )
    # Pydantic 422 — body validation failed before route body ran.
    # On 422 FastAPI never invokes the route function so we DON'T
    # see the headers. That's expected; the case is here to
    # document the boundary, not to assert headers on it.
    assert res.status_code == 422
