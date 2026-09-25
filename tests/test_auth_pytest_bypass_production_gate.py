"""Gate tests for `get_current_user`'s pytest-only `test-token` bypass.

`utils.auth.get_current_user` short-circuits to a hardcoded `role=admin`
identity whenever `PYTEST_CURRENT_TEST` is set in the process AND the caller
presents the literal bearer token `test-token` — no signature, no password.
`PYTEST_CURRENT_TEST` is only ever set by pytest itself, but it is just an
environment variable: a debug image built from a test stage, a copied `.env`,
or a misconfigured smoke harness could leak it into a real server process. On
a real deployment that would mint an unauthenticated admin identity for
anyone who knows the string "test-token" — the same class of bug PR #1889
fixed for the demo admin login lanes.

The bypass is heavily load-bearing (hundreds of tests authenticate with
`Bearer test-token`), so it is not removed here. Instead it routes through
`config.platform.pytest_bypass_allowed`, which refuses it on ANY deployed
host — `not (is_deployed() or is_production())`, not merely the `not
is_production()` PR #1889/#1893/#1897 shipped. Staging is the case that
matters: it runs a restored production snapshot and real third-party
credentials, so a leaked `PYTEST_CURRENT_TEST` on a staging revision still
minted `role=admin` for anyone who knows the string "test-token".
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from config import platform as P
from utils.auth import get_current_user
from tests.test_platform_shim import _ALL_KEYS as _PLATFORM_SIGNAL_KEYS


def _clear_environment_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reuses config/platform.py's own canonical key list (test_platform_shim.py's
    # _ALL_KEYS) rather than a hand-picked subset, so a developer's shell with a
    # leftover RAILWAY_SERVICE_ID/PROJECT_ID/etc. can't make this test fail for
    # the wrong reason.
    for var in _PLATFORM_SIGNAL_KEYS:
        monkeypatch.delenv(var, raising=False)


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/protected")
    async def protected(current_user: dict = Depends(get_current_user)):
        return {"role": current_user["role"]}

    return TestClient(app)


def test_test_token_bypass_works_outside_production(monkeypatch):
    """Sanity check: the bypass must still work for the rest of the suite."""
    _clear_environment_labels(monkeypatch)
    client = _client()

    res = client.get("/protected", headers={"Authorization": "Bearer test-token"})

    assert res.status_code == 200
    assert res.json()["role"] == "admin"


#: Deployed environments the bypass must refuse, with whether each resolves to
#: `production`. The False rows were ARMED under the `not is_production()`
#: gate and are the actual behavior change here.
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
def test_test_token_bypass_refuses_on_any_deployed_host(
    monkeypatch, env, expect_production
):
    """Mutant check: PYTEST_CURRENT_TEST alone must not arm the bypass.

    PYTEST_CURRENT_TEST is genuinely set here (pytest sets it for every
    running test), so this only passes if the deployment conjunct, via
    pytest_bypass_allowed(), is load-bearing. The `expect_production=False`
    rows pin that `is_production()` is NOT what refuses them — reverting the
    gate to `not is_production()` re-arms exactly those rows, which is how
    this shipped between PR #1893 and this change.
    """
    assert __import__("os").getenv("PYTEST_CURRENT_TEST")
    _clear_environment_labels(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert P.is_production() is expect_production
    client = _client()

    res = client.get("/protected", headers={"Authorization": "Bearer test-token"})

    assert res.status_code == 401
