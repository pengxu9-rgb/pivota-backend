"""Gate tests for `build_brief`'s pytest-only persist-failure bypass.

`routes.agent_briefs.build_brief` swallows a brief-persistence failure and
returns 200 in degraded mode whenever `PYTEST_CURRENT_TEST` is set — so the
unit-test suite can build briefs without a real database. Lower severity than
the `get_agent_context` bypass (it suppresses a durability error rather than
minting privilege), but the same underlying defect: a leakable env var
silently changes production behavior with no fail-closed check. Found in the
same review pass as PR #1893's `utils.auth.get_current_user` fix.

Now routed through `config.platform.pytest_bypass_allowed`, which adds the
`not is_production()` conjunct.
"""
from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from config import platform as P
from main import app
from routes.agent_auth import AgentContext, get_agent_context
from tests.test_platform_shim import _ALL_KEYS

_PAYLOAD = {
    "raw_query": "我是油痘肌，想去闭口，预算500块。",
    "market": "CN",
    "locale": "zh-CN",
    "currency": "CNY",
}


@pytest.fixture(autouse=True)
def _clean_platform_env(monkeypatch):
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    P.reset_platform_state()
    yield
    P.reset_platform_state()


@pytest.fixture
def client():
    """A TestClient with authentication stubbed out.

    These tests are only about the brief-persist bypass, not the (separately
    tested) `get_agent_context` auth bypass — both key off the same
    `PYTEST_CURRENT_TEST` signal, so a test that sets a production env var
    without overriding auth would get a 401 from `get_agent_context` before
    ever reaching the code under test.
    """

    async def _fake_context(request: Request):
        return AgentContext(
            {"agent_id": "agent_test", "agent_name": "Test Agent", "allowed_merchants": None, "is_active": True},
            request,
        )

    app.dependency_overrides[get_agent_context] = _fake_context
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


def _fail_insert_brief(*args, **kwargs):
    raise RuntimeError("db unavailable in test")


def test_persist_failure_degrades_outside_production(monkeypatch, client: TestClient):
    """Sanity check: the degraded-mode bypass must still work for the suite."""
    import routes.agent_briefs as module

    monkeypatch.setattr(module, "insert_brief", _fail_insert_brief)

    resp = client.post(
        "/agent/v1/briefs/build",
        headers={"X-API-Key": "test-agent-key"},
        json=_PAYLOAD,
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


@pytest.mark.parametrize("prod_var", ["PIVOTA_ENV", "RAILWAY_ENVIRONMENT", "K_SERVICE"])
def test_persist_failure_raises_503_in_production_even_with_pytest_env(
    monkeypatch, client: TestClient, prod_var
):
    """Mutant check: PYTEST_CURRENT_TEST alone must not suppress the error.

    PYTEST_CURRENT_TEST is genuinely set here (pytest sets it for every
    running test), so this only passes if the new is_production() conjunct,
    via pytest_bypass_allowed(), is load-bearing.
    """
    import routes.agent_briefs as module

    assert __import__("os").getenv("PYTEST_CURRENT_TEST")
    monkeypatch.setattr(module, "insert_brief", _fail_insert_brief)
    monkeypatch.setenv(
        prod_var,
        "pivota-backend-prod" if prod_var == "K_SERVICE" else "production",
    )

    resp = client.post(
        "/agent/v1/briefs/build",
        headers={"X-API-Key": "test-agent-key"},
        json=_PAYLOAD,
    )

    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "BRIEF_PERSIST_FAILED"
