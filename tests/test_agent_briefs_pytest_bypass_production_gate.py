"""Gate tests for `build_brief`'s pytest-only persist-failure bypass.

`routes.agent_briefs.build_brief` swallows a brief-persistence failure and
returns 200 in degraded mode whenever `PYTEST_CURRENT_TEST` is set — so the
unit-test suite can build briefs without a real database. Lower severity than
the `get_agent_context` bypass (it suppresses a durability error rather than
minting privilege), but the same underlying defect: a leakable env var
silently changes production behavior with no fail-closed check. Found in the
same review pass as PR #1893's `utils.auth.get_current_user` fix.

Routed through `config.platform.pytest_bypass_allowed`, which refuses it on
ANY deployed host — `not (is_deployed() or is_production())`, not merely `not
is_production()` as PR #1897 first shipped. On staging that meant a leaked
`PYTEST_CURRENT_TEST` silently downgraded a durability failure to a 200.
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


#: Deployed environments the degraded mode must refuse, with whether each one
#: resolves to `production`. The False rows were ARMED under PR #1897's
#: `not is_production()` gate and are the actual behavior change here.
_REFUSING_ENVS = {
    "production_pivota_env": ({"PIVOTA_ENV": "production"}, True),
    "production_railway": ({"RAILWAY_ENVIRONMENT": "production"}, True),
    "production_cloud_run_fail_closed": ({"K_SERVICE": "pivota-backend-prod"}, True),
    "staging_cloud_run": (
        {"K_SERVICE": "pivota-backend-staging", "PIVOTA_ENV": "staging"},
        False,
    ),
    "staging_railway": ({"RAILWAY_ENVIRONMENT": "staging"}, False),
    "development_cloud_run": (
        {"K_SERVICE": "pivota-backend", "PIVOTA_ENV": "development"},
        False,
    ),
}


@pytest.mark.parametrize(
    "env,expect_production", _REFUSING_ENVS.values(), ids=_REFUSING_ENVS
)
def test_persist_failure_raises_503_on_any_deployed_host(
    monkeypatch, client: TestClient, env, expect_production
):
    """Mutant check: PYTEST_CURRENT_TEST alone must not suppress the error.

    PYTEST_CURRENT_TEST is genuinely set here (pytest sets it for every
    running test), so this only passes if the deployment conjunct, via
    pytest_bypass_allowed(), is load-bearing. The `expect_production=False`
    rows pin that `is_production()` is NOT what refuses them — reverting the
    gate to `not is_production()` re-arms exactly those rows.
    """
    import routes.agent_briefs as module

    assert __import__("os").getenv("PYTEST_CURRENT_TEST")
    monkeypatch.setattr(module, "insert_brief", _fail_insert_brief)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert P.is_production() is expect_production

    resp = client.post(
        "/agent/v1/briefs/build",
        headers={"X-API-Key": "test-agent-key"},
        json=_PAYLOAD,
    )

    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "BRIEF_PERSIST_FAILED"
