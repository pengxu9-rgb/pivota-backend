from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_client_with_employee_override():
    from routes import employee_kb_monitoring as module

    async def override_employee() -> Dict[str, Any]:
        return {"employee_id": "emp_test", "role": "employee", "email": "ops@pivota.cc"}

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_current_employee] = override_employee
    client = TestClient(app)
    return module, app, client


def _snapshot(
    metrics: Optional[Dict[str, int]] = None,
    runtime: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "service": {
            "name": "aurora-bff",
            "base_url": "https://aurora.example",
            "health_status": "healthy",
            "health_http_status": 200,
            "commit_sha": "194502fa7301",
            "deployment_id": "e4f33988-a3e8-4dd4-a115-0f2c2a445bcc",
            "version": "194502fa",
        },
        "runtime": runtime
        or {
            "kb_v0_enabled": True,
            "kb_fail_mode": "closed",
            "kill_switch_enabled": False,
            "source": "expected",
        },
        "metrics": metrics
        or {
            "loader_error_total": 0,
            "rule_match_total": 2055,
            "legacy_fallback_total": 0,
            "climate_fallback_total": 3,
        },
        "_errors": [],
        "_source": "aurora_metrics+health+version",
        "_debug": {},
    }


def test_kb_monitor_summary_healthy(monkeypatch):
    module, app, client = _build_client_with_employee_override()
    module._reset_state_for_tests()

    monkeypatch.setenv("AURORA_BFF_BASE_URL", "https://aurora.example")
    monkeypatch.setenv("AURORA_MONITOR_CACHE_TTL_SEC", "0")

    async def fake_collect(base_url: str, timeout_ms: int, metrics_bearer_token: str) -> Dict[str, Any]:
        assert base_url == "https://aurora.example"
        return _snapshot()

    monkeypatch.setattr(module, "_collect_live_snapshot", fake_collect)

    response = client.get(
        "/employee/monitoring/aurora-kb-v0/summary?window=5m&include_debug=0",
        headers={"Authorization": "Bearer employee-test-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["window"] == "5m"
    assert payload["data"]["service"]["name"] == "aurora-bff"
    assert payload["data"]["runtime"]["kb_fail_mode"] == "closed"
    assert payload["data"]["metrics"]["rule_match_total"] == 2055
    assert payload["data"]["derived"]["legacy_fallback_ratio"] == 0.0
    assert payload["data"]["guardrails"]["loader_error_alert"] is False
    assert payload["meta"]["stale"] is False

    app.dependency_overrides.clear()


def test_extract_service_identity_prefers_stable_version_object():
    from routes import employee_kb_monitoring as module

    identity = module._extract_service_identity(
        base_url="https://aurora.example",
        health_status="healthy",
        health_http_status=200,
        health_json={
            "version": {
                "service": "pivota-backend",
                "commit": "194502fa7301",
                "full_sha": "194502fa73011c5e49ee2ca7e3a13f8ecbabc123",
                "build_id": "194502fa7301",
                "branch": "main",
                "deployment_id": "dep_health",
                "started_at": "2026-03-24T07:04:12Z",
            }
        },
        version_json={
            "version": {
                "service": "pivota-backend",
                "commit": "194502fa7301",
                "full_sha": "194502fa73011c5e49ee2ca7e3a13f8ecbabc123",
                "build_id": "194502fa7301",
                "branch": "main",
                "deployment_id": "dep_version",
                "started_at": "2026-03-24T07:04:12Z",
            }
        },
        metrics_response=None,
        health_response=None,
        version_response=None,
    )

    assert identity["commit_sha"] == "194502fa73011c5e49ee2ca7e3a13f8ecbabc123"
    assert identity["deployment_id"] == "dep_version"
    assert identity["version"] == "194502fa7301"


def test_kb_monitor_summary_requires_auth():
    from routes import employee_kb_monitoring as module

    module._reset_state_for_tests()
    app = FastAPI()
    app.include_router(module.router)
    client = TestClient(app)

    response = client.get("/employee/monitoring/aurora-kb-v0/summary")
    assert response.status_code in {401, 403}


def test_kb_monitor_summary_stale_on_upstream_failure(monkeypatch):
    module, app, client = _build_client_with_employee_override()
    module._reset_state_for_tests()

    monkeypatch.setenv("AURORA_BFF_BASE_URL", "https://aurora.example")
    monkeypatch.setenv("AURORA_MONITOR_CACHE_TTL_SEC", "0")

    calls = {"count": 0}

    async def fake_collect(base_url: str, timeout_ms: int, metrics_bearer_token: str) -> Dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            return _snapshot()
        raise RuntimeError("metrics_fetch_failed:TimeoutError")

    monkeypatch.setattr(module, "_collect_live_snapshot", fake_collect)

    first = client.get(
        "/employee/monitoring/aurora-kb-v0/summary?window=5m",
        headers={"Authorization": "Bearer employee-test-token"},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "healthy"

    second = client.get(
        "/employee/monitoring/aurora-kb-v0/summary?window=5m",
        headers={"Authorization": "Bearer employee-test-token"},
    )
    assert second.status_code == 200
    payload = second.json()
    assert payload["status"] == "degraded"
    assert payload["meta"]["stale"] is True
    assert any("metrics_fetch_failed:TimeoutError" in err for err in payload["errors"])

    app.dependency_overrides.clear()
