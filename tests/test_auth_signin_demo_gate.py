"""Gate tests for the `/auth/signin` hardcoded demo-account lane.

The demo fixtures mint role=admin JWTs, so the lane must be dead in the
default configuration and unsatisfiable in production even when
ENABLE_INTERNAL_DEMO_FIXTURES is set.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import auth_routes as auth_routes_module

DEMO_ADMIN_CREDENTIALS = [
    ("employee@pivota.com", "Admin123!"),
    ("superadmin@pivota.com", "admin123"),
]


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def fake_fetch_one(query, values=None):
        return None

    monkeypatch.setattr(auth_routes_module.database, "fetch_one", fake_fetch_one)
    app = FastAPI()
    app.include_router(auth_routes_module.router)
    return TestClient(app)


def _clear_environment_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "PIVOTA_ENV",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_ENVIRONMENT_NAME",
        "K_SERVICE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("email,password", DEMO_ADMIN_CREDENTIALS)
def test_signin_rejects_demo_admin_by_default(monkeypatch, email, password):
    client = _client(monkeypatch)
    assert auth_routes_module.settings.enable_internal_demo_fixtures is False

    res = client.post("/auth/signin", json={"email": email, "password": password})

    assert res.status_code == 401


@pytest.mark.parametrize("prod_var", ["PIVOTA_ENV", "RAILWAY_ENVIRONMENT"])
@pytest.mark.parametrize("email,password", DEMO_ADMIN_CREDENTIALS)
def test_signin_rejects_demo_admin_in_production_even_with_flag(
    monkeypatch, prod_var, email, password
):
    """Mutant check: the flag alone must not arm the lane in production."""
    client = _client(monkeypatch)
    monkeypatch.setattr(
        auth_routes_module.settings, "enable_internal_demo_fixtures", True
    )
    _clear_environment_labels(monkeypatch)
    monkeypatch.setenv(prod_var, "production")

    res = client.post("/auth/signin", json={"email": email, "password": password})

    assert res.status_code == 401


def test_signin_accepts_demo_admin_when_fixtures_enabled_outside_production(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        auth_routes_module.settings, "enable_internal_demo_fixtures", True
    )
    _clear_environment_labels(monkeypatch)

    res = client.post(
        "/auth/signin",
        json={"email": "employee@pivota.com", "password": "Admin123!"},
    )

    assert res.status_code == 200
    payload = res.json()
    assert payload["user"]["role"] == "admin"
    assert payload["token"]
