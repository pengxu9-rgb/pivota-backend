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
`Bearer test-token`), so it is not removed here. Instead it gets the same
`not is_production()` conjunct PR #1889 added to the demo login lanes: it
must be provably impossible outside an actual pytest run in a non-production
environment.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

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


@pytest.mark.parametrize("prod_var", ["PIVOTA_ENV", "RAILWAY_ENVIRONMENT", "K_SERVICE"])
def test_test_token_bypass_refuses_in_production_even_with_pytest_env(
    monkeypatch, prod_var
):
    """Mutant check: PYTEST_CURRENT_TEST alone must not arm the bypass.

    PYTEST_CURRENT_TEST is genuinely set here (pytest sets it for every
    running test), so this only passes if the new is_production() conjunct
    is load-bearing.
    """
    assert __import__("os").getenv("PYTEST_CURRENT_TEST")
    _clear_environment_labels(monkeypatch)
    monkeypatch.setenv(
        prod_var,
        "pivota-backend-prod" if prod_var == "K_SERVICE" else "production",
    )
    client = _client()

    res = client.get("/protected", headers={"Authorization": "Bearer test-token"})

    assert res.status_code == 401
