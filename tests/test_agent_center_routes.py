"""
HTTP-level tests for the Agent Center routes (demand-test + sku-match).

These tests sit on top of `tests/test_agent_center_service.py`'s FakeDB —
they exercise the route surface (auth wiring, request validation, route →
service plumbing, the /run lock returning 409) without depending on a real
Postgres or a real LLM upstream.

Two layers below cover the runner internals:
  - tests/test_agent_center_service.py — service primitives + lock semantics
  - tests/test_agent_center_sku_match.py — sku-match runner + pure checks

Background runners are monkey-patched to no-op so the lock can be observed
in isolation (the runner would otherwise transition the row to
succeeded/stub_complete during the response cycle of TestClient).
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.auth import get_current_employee
from tests.test_agent_center_service import FakeDB


# ---------------------------------------------------------------------------
# Fixture: build a small FastAPI app with just the agent-center routers and
# a fake DB + no-op runners.
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Tuple[TestClient, FakeDB]:
    db = FakeDB()
    from services import agent_center_service as ac
    monkeypatch.setattr(ac, "database", db)

    # No-op the runners. Without this, BackgroundTasks would call into
    # the real LLM client / products_cache query during the response cycle
    # and break tests that don't have those dependencies stubbed.
    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    from routes import agent_center_demand_test_routes as dtr
    from routes import agent_center_sku_match_routes as smr
    monkeypatch.setattr(dtr, "_service_run_demand_test", _noop)
    monkeypatch.setattr(smr, "run_sku_match", _noop)

    async def _override_employee() -> Dict[str, Any]:
        return {"employee_id": "emp_test", "email": "test@example.com", "role": "admin"}

    app = FastAPI()
    app.include_router(dtr.router)
    app.include_router(smr.router)
    app.dependency_overrides[get_current_employee] = _override_employee
    return TestClient(app), db


# ---------------------------------------------------------------------------
# Demand-test routes
# ---------------------------------------------------------------------------


def test_create_demand_test_returns_queued_row(env: Tuple[TestClient, FakeDB]) -> None:
    client, db = env
    res = client.post(
        "/api/agent-center/demand-tests",
        json={
            "merchant_id": "m1",
            "store_id": "s1",
            "scan_mode": "open_product_visibility_test",
            "payload": {"context": {"products": ["p1"]}},
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "queued"
    assert body["scan_target"]["scan_mode"] == "open_product_visibility_test"
    assert len(db._tables["agent_center_scan_targets"]) == 1


def test_create_demand_test_rejects_unsupported_scan_mode(env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = env
    res = client.post(
        "/api/agent-center/demand-tests",
        json={
            "merchant_id": "m1", "store_id": "s1",
            "scan_mode": "sku_match",  # belongs to the sku-match agent
        },
    )
    assert res.status_code == 400
    assert "unsupported" in res.json()["detail"].lower()


def test_create_demand_test_validates_required_fields(env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = env
    res = client.post(
        "/api/agent-center/demand-tests",
        json={"merchant_id": "m1"},  # missing store_id, scan_mode
    )
    assert res.status_code == 422


def test_get_demand_test_returns_row_and_issues(env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = env
    created = client.post(
        "/api/agent-center/demand-tests",
        json={
            "merchant_id": "m1", "store_id": "s1",
            "scan_mode": "open_product_visibility_test",
        },
    ).json()
    scan_target_id = created["scan_target"]["id"]

    res = client.get(f"/api/agent-center/demand-tests/{scan_target_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["scan_target"]["id"] == scan_target_id
    assert body["issues"] == []


def test_get_demand_test_404_for_wrong_scan_mode(env: Tuple[TestClient, FakeDB]) -> None:
    """A sku-match scan_target accessed via /demand-tests must 404 — keeps
    each agent's surface focused and prevents cross-agent state confusion."""
    client, _ = env
    created = client.post(
        "/api/agent-center/sku-match",
        json={"merchant_id": "m1", "store_id": "s1"},
    ).json()
    scan_target_id = created["scan_target"]["id"]

    res = client.get(f"/api/agent-center/demand-tests/{scan_target_id}")
    assert res.status_code == 404


def test_list_demand_tests_filters_by_status(env: Tuple[TestClient, FakeDB]) -> None:
    client, db = env
    for _ in range(2):
        client.post(
            "/api/agent-center/demand-tests",
            json={
                "merchant_id": "m1", "store_id": "s1",
                "scan_mode": "open_product_visibility_test",
            },
        )
    # Force one of them into stub_complete so the filter has something to
    # discriminate on.
    db._tables["agent_center_scan_targets"][0]["status"] = "stub_complete"

    res = client.get("/api/agent-center/demand-tests?merchant_id=m1&status=queued")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "queued"


def test_run_demand_test_acquires_lock_and_returns_running(env: Tuple[TestClient, FakeDB]) -> None:
    client, db = env
    created = client.post(
        "/api/agent-center/demand-tests",
        json={
            "merchant_id": "m1", "store_id": "s1",
            "scan_mode": "open_product_visibility_test",
        },
    ).json()
    scan_target_id = created["scan_target"]["id"]

    res = client.post(f"/api/agent-center/demand-tests/{scan_target_id}/run")
    assert res.status_code == 200
    assert res.json()["status"] == "running"
    # Lock was acquired — row is `running`.
    assert db._tables["agent_center_scan_targets"][0]["status"] == "running"


def test_run_demand_test_returns_409_when_already_running(env: Tuple[TestClient, FakeDB]) -> None:
    """The actual concurrency guarantee: two parallel /run calls cannot
    both schedule the runner. The second is rejected with 409."""
    client, _ = env
    created = client.post(
        "/api/agent-center/demand-tests",
        json={
            "merchant_id": "m1", "store_id": "s1",
            "scan_mode": "open_product_visibility_test",
        },
    ).json()
    scan_target_id = created["scan_target"]["id"]

    first = client.post(f"/api/agent-center/demand-tests/{scan_target_id}/run")
    assert first.status_code == 200

    second = client.post(f"/api/agent-center/demand-tests/{scan_target_id}/run")
    assert second.status_code == 409
    assert "running" in second.json()["detail"].lower()


def test_run_demand_test_404_for_nonexistent(env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = env
    res = client.post("/api/agent-center/demand-tests/acst_does_not_exist/run")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# SKU-match routes
# ---------------------------------------------------------------------------


def test_create_sku_match_returns_queued_row(env: Tuple[TestClient, FakeDB]) -> None:
    client, db = env
    res = client.post(
        "/api/agent-center/sku-match",
        json={
            "merchant_id": "m1", "store_id": "s1",
            "payload": {"options": {"limit": 10, "max_age_days": 7}},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "queued"
    assert body["scan_target"]["scan_mode"] == "sku_match"
    assert len(db._tables["agent_center_scan_targets"]) == 1


def test_create_sku_match_validates_required_fields(env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = env
    res = client.post("/api/agent-center/sku-match", json={"merchant_id": "m1"})
    assert res.status_code == 422


def test_get_sku_match_404_for_wrong_scan_mode(env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = env
    created = client.post(
        "/api/agent-center/demand-tests",
        json={
            "merchant_id": "m1", "store_id": "s1",
            "scan_mode": "open_product_visibility_test",
        },
    ).json()
    scan_target_id = created["scan_target"]["id"]

    res = client.get(f"/api/agent-center/sku-match/{scan_target_id}")
    assert res.status_code == 404


def test_run_sku_match_acquires_lock_and_returns_running(env: Tuple[TestClient, FakeDB]) -> None:
    client, db = env
    created = client.post(
        "/api/agent-center/sku-match",
        json={"merchant_id": "m1", "store_id": "s1"},
    ).json()
    scan_target_id = created["scan_target"]["id"]

    res = client.post(f"/api/agent-center/sku-match/{scan_target_id}/run")
    assert res.status_code == 200
    assert res.json()["status"] == "running"
    assert db._tables["agent_center_scan_targets"][0]["status"] == "running"


def test_run_sku_match_returns_409_when_already_running(env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = env
    created = client.post(
        "/api/agent-center/sku-match",
        json={"merchant_id": "m1", "store_id": "s1"},
    ).json()
    scan_target_id = created["scan_target"]["id"]

    first = client.post(f"/api/agent-center/sku-match/{scan_target_id}/run")
    assert first.status_code == 200

    second = client.post(f"/api/agent-center/sku-match/{scan_target_id}/run")
    assert second.status_code == 409


def test_run_sku_match_404_for_nonexistent(env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = env
    res = client.post("/api/agent-center/sku-match/acst_does_not_exist/run")
    assert res.status_code == 404


def test_list_sku_match_runs_filters_by_merchant(env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = env
    for merchant in ("m1", "m2"):
        client.post(
            "/api/agent-center/sku-match",
            json={"merchant_id": merchant, "store_id": "s1"},
        )

    res = client.get("/api/agent-center/sku-match?merchant_id=m1")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["merchant_id"] == "m1"


# ---------------------------------------------------------------------------
# Auth — when the override is missing, the route must reject anonymous
# requests. This catches future regressions where someone forgets the
# `Depends(get_current_employee)` on a new endpoint.
# ---------------------------------------------------------------------------


def test_demand_test_route_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `dependency_overrides` for `get_current_employee` — the framework
    falls through to the real auth check, which rejects unauthenticated
    requests with 401/403."""
    db = FakeDB()
    from services import agent_center_service as ac
    monkeypatch.setattr(ac, "database", db)

    from routes import agent_center_demand_test_routes as dtr
    app = FastAPI()
    app.include_router(dtr.router)
    client = TestClient(app)
    res = client.get("/api/agent-center/demand-tests?merchant_id=m1")
    assert res.status_code in {401, 403}


def test_sku_match_route_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    from services import agent_center_service as ac
    monkeypatch.setattr(ac, "database", db)

    from routes import agent_center_sku_match_routes as smr
    app = FastAPI()
    app.include_router(smr.router)
    client = TestClient(app)
    res = client.get("/api/agent-center/sku-match?merchant_id=m1")
    assert res.status_code in {401, 403}


# ---------------------------------------------------------------------------
# Admin routes — stuck-run inspection + force-reset
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> Tuple[TestClient, FakeDB]:
    from datetime import datetime, timedelta, timezone
    db = FakeDB()
    from services import agent_center_service as ac
    monkeypatch.setattr(ac, "database", db)

    from routes import agent_center_admin_routes as admin_routes
    from utils.auth import require_admin

    async def _override_admin() -> Dict[str, Any]:
        return {"employee_id": "emp_admin", "email": "admin@pivota.test", "role": "admin"}

    app = FastAPI()
    app.include_router(admin_routes.router)
    app.dependency_overrides[require_admin] = _override_admin
    return TestClient(app), db


def test_admin_list_stuck_runs_returns_only_stale(admin_env: Tuple[TestClient, FakeDB]) -> None:
    from datetime import datetime, timedelta, timezone
    client, db = admin_env

    # Seed: one fresh running, one stale running, one succeeded.
    base = {
        "merchant_id": "m1", "store_id": "s1",
        "scan_mode": "open_product_visibility_test",
        "payload": {}, "started_at": None, "finished_at": None, "deleted_at": None,
    }
    db._tables["agent_center_scan_targets"].extend([
        {**base, "id": "fresh", "status": "running",
         "updated_at": datetime.now(timezone.utc) - timedelta(minutes=2)},
        {**base, "id": "stale", "status": "running",
         "updated_at": datetime.now(timezone.utc) - timedelta(minutes=120)},
        {**base, "id": "ok", "status": "succeeded",
         "updated_at": datetime.now(timezone.utc) - timedelta(days=1)},
    ])

    res = client.get("/admin/agent-center/scan-targets/stuck?stale_minutes=30")
    assert res.status_code == 200
    body = res.json()
    assert body["stale_minutes"] == 30
    ids = [it["id"] for it in body["items"]]
    assert ids == ["stale"]


def test_admin_force_reset_returns_failed_with_audit(admin_env: Tuple[TestClient, FakeDB]) -> None:
    from datetime import datetime, timedelta, timezone
    client, db = admin_env

    db._tables["agent_center_scan_targets"].append({
        "id": "stuck1", "merchant_id": "m1", "store_id": "s1",
        "scan_mode": "open_product_visibility_test",
        "status": "running", "payload": {}, "started_at": None,
        "finished_at": None, "deleted_at": None,
        "updated_at": datetime.now(timezone.utc) - timedelta(minutes=120),
    })
    res = client.post(
        "/admin/agent-center/scan-targets/stuck1/force-reset",
        json={"reason": "runner crashed (OOM)"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "reset"
    err = body["scan_target"]["payload"]["error"]
    assert err["kind"] == "force_reset"
    assert err["last_known_status"] == "running"
    # `reset_by` defaults to the authenticated user's email when present.
    assert err["reset_by"] == "admin@pivota.test"


def test_admin_force_reset_404_for_nonexistent(admin_env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = admin_env
    res = client.post(
        "/admin/agent-center/scan-targets/acst_nope/force-reset",
        json={"reason": "cleanup"},
    )
    assert res.status_code == 404


def test_admin_force_reset_requires_reason(admin_env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = admin_env
    res = client.post(
        "/admin/agent-center/scan-targets/anything/force-reset",
        json={},
    )
    assert res.status_code == 422  # Pydantic catches the missing field


def test_admin_routes_require_admin_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    from services import agent_center_service as ac
    monkeypatch.setattr(ac, "database", db)

    from routes import agent_center_admin_routes as admin_routes
    app = FastAPI()
    app.include_router(admin_routes.router)
    client = TestClient(app)

    res = client.get("/admin/agent-center/scan-targets/stuck")
    assert res.status_code in {401, 403}


# ---------------------------------------------------------------------------
# BD report route — POST /api/agent-center/bd/external-merchant-report
# ---------------------------------------------------------------------------


@pytest.fixture
def bd_env(monkeypatch: pytest.MonkeyPatch) -> Tuple[TestClient, FakeDB]:
    """BD route doesn't touch the DB, but mocking llm_client.probe is
    essential — otherwise tests would hit real Gemini."""
    db = FakeDB()
    from services import agent_center_service as ac
    monkeypatch.setattr(ac, "database", db)

    # Mock the upstream probe to return deterministic canned shapes.
    # Different responses per scan_mode so the structured report has
    # interesting numbers (visibility=33, attribution=0).
    from services import agent_center_bd_report_service as bd_service

    async def _fake_probe(**kwargs):
        scan_mode = kwargs.get("scan_mode")
        if scan_mode == "open_product_visibility_test":
            return {
                "scan_mode": scan_mode,
                "provider": "mock",
                "scores": {"visibility_score": 33, "attribution_echo_rate": 0},
                "runs_count": 3,
                "raw_runs": [
                    {
                        "query": "where can I buy Cloud Paint",
                        "parsed": {"product_visible": True},
                        "grounding_chunks": ["https://sephora.com/p/cloud-paint"],
                    },
                    {
                        "query": "Cloud Paint reviews",
                        "parsed": {"product_visible": False},
                        "grounding_chunks": [],
                    },
                    {
                        "query": "best blush for sensitive skin",
                        "parsed": {"product_visible": False},
                        "grounding_chunks": ["https://allure.com/best-blushes"],
                    },
                ],
                "findings": [],
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        # attribution
        return {
            "scan_mode": scan_mode,
            "provider": "mock",
            "scores": {"visibility_score": 0, "attribution_echo_rate": 0},
            "runs_count": 3,
            "raw_runs": [
                {
                    "query": "shop Cloud Paint online",
                    "parsed": {"merchant_url_found": False},
                    "grounding_chunks": [
                        "https://sephora.com/p/cloud-paint",
                        "https://ulta.com/p/cloud-paint",
                    ],
                },
                {
                    "query": "Cloud Paint discount",
                    "parsed": {"merchant_url_found": False},
                    "grounding_chunks": ["https://sephora.com/p/cloud-paint"],
                },
                {
                    "query": "where can I buy Glossier blush",
                    "parsed": {"merchant_url_found": False},
                    "grounding_chunks": [],
                },
            ],
            "findings": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    # Patch the import inside the service module.
    monkeypatch.setattr(bd_service.llm_client, "probe", _fake_probe)

    from routes import agent_center_bd_routes as bd_routes
    from utils.auth import get_current_employee

    async def _override_employee() -> Dict[str, Any]:
        return {"employee_id": "emp_bd", "email": "bd@pivota.test", "role": "bd"}

    app = FastAPI()
    app.include_router(bd_routes.router)
    app.dependency_overrides[get_current_employee] = _override_employee
    return TestClient(app), db


def test_bd_external_merchant_report_returns_structured_report(
    bd_env: Tuple[TestClient, FakeDB],
) -> None:
    client, _ = bd_env
    res = client.post(
        "/api/agent-center/bd/external-merchant-report",
        json={
            "merchant_name": "Glossier",
            "merchant_pdp_url": "https://glossier.com/products/cloud-paint",
            "product_title": "Cloud Paint",
            "product_vendor": "Glossier",
            "product_type": "blush",
            "provider": "mock",
            "max_runs": 3,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok"
    report = body["report"]

    # Verdict reflects the canned scores: vis=33, attr=0 → MISATTRIBUTED.
    assert report["verdict"]["label"] == "VISIBLE BUT MISATTRIBUTED"
    assert report["verdict"]["visibility_score"] == 33
    assert report["verdict"]["attribution_score"] == 0

    # Per-query rows from both probes.
    assert len(report["visibility"]["queries"]) == 3
    assert len(report["attribution"]["queries"]) == 3

    # Competitor list extracted from grounding_chunks; merchant host
    # (glossier.com) excluded; sephora cited 2x, ulta 1x.
    hosts = {entry["host"]: entry["times_cited"] for entry in report["attribution"]["competitor_hosts"]}
    assert hosts.get("sephora.com") == 2
    assert hosts.get("ulta.com") == 1
    assert "glossier.com" not in hosts


def test_bd_route_rejects_invalid_url(bd_env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = bd_env
    res = client.post(
        "/api/agent-center/bd/external-merchant-report",
        json={
            "merchant_name": "X",
            "merchant_pdp_url": "not-a-url",
            "product_title": "Y",
        },
    )
    assert res.status_code == 422


def test_bd_route_caps_max_runs_at_8(bd_env: Tuple[TestClient, FakeDB]) -> None:
    client, _ = bd_env
    res = client.post(
        "/api/agent-center/bd/external-merchant-report",
        json={
            "merchant_name": "X",
            "merchant_pdp_url": "https://example.com/p/1",
            "product_title": "Y",
            "max_runs": 99,  # should fail Pydantic ge=1, le=8
        },
    )
    assert res.status_code == 422


def test_bd_route_requires_employee_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    from services import agent_center_service as ac
    monkeypatch.setattr(ac, "database", db)

    from routes import agent_center_bd_routes as bd_routes
    app = FastAPI()
    app.include_router(bd_routes.router)
    client = TestClient(app)
    res = client.post(
        "/api/agent-center/bd/external-merchant-report",
        json={
            "merchant_name": "X",
            "merchant_pdp_url": "https://example.com/p/1",
            "product_title": "Y",
        },
    )
    assert res.status_code in {401, 403}
