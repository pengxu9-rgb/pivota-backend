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


# --- DEMO_MERCHANT_IDS backfill lane -----------------------------------
#
# A separate lane from the demo_accounts dict above: a REAL, bcrypt-verified
# `users` row with role=merchant and no merchant_id set gets one backfilled
# from DEMO_MERCHANT_IDS. Unlike demo_accounts (built inline in signin() and
# already gated), this module-level dict used to have no is_production()
# check, so any anonymous /api/auth/register self-registration of
# merchant@test.com would bind to the real demo merchant_id in production.

_MERCHANT_ROW = {
    "id": "user_merchant_1",
    "email": "merchant@test.com",
    "password_hash": "irrelevant-hash",
    "full_name": "Test Merchant",
    "role": "merchant",
    "active": True,
    "merchant_id": None,
}


def _client_with_merchant_row(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def fake_fetch_one(query, values=None):
        if "FROM users" in query:
            return dict(_MERCHANT_ROW)
        return None

    monkeypatch.setattr(auth_routes_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        auth_routes_module, "verify_bcrypt_password", lambda _pw, _hash: True
    )
    app = FastAPI()
    app.include_router(auth_routes_module.router)
    return TestClient(app)


def test_signin_rejects_demo_merchant_id_backfill_by_default(monkeypatch):
    """Mutant check: DEMO_MERCHANT_ID alone (fixtures flag off) must not
    bind merchant@test.com to the demo merchant_id."""
    client = _client_with_merchant_row(monkeypatch)
    assert auth_routes_module.settings.enable_internal_demo_fixtures is False
    monkeypatch.setenv("DEMO_MERCHANT_ID", "merch_demo_001")

    res = client.post(
        "/auth/signin",
        json={"email": "merchant@test.com", "password": "anything"},
    )

    assert res.status_code == 200
    assert "merchant_id" not in res.json()["user"]


@pytest.mark.parametrize("prod_var", ["PIVOTA_ENV", "RAILWAY_ENVIRONMENT"])
def test_signin_rejects_demo_merchant_id_backfill_in_production_even_with_flag(
    monkeypatch, prod_var
):
    """Mutant check: the flag alone must not arm the merchant_id backfill in
    production — this is the lane PR #1889 left ungated. POST
    /api/auth/register lets anyone self-register merchant@test.com; this
    backfill used to bind that attacker-controlled account to the real
    DEMO_MERCHANT_ID with no is_production() check."""
    client = _client_with_merchant_row(monkeypatch)
    monkeypatch.setattr(
        auth_routes_module.settings, "enable_internal_demo_fixtures", True
    )
    _clear_environment_labels(monkeypatch)
    monkeypatch.setenv(prod_var, "production")
    monkeypatch.setenv("DEMO_MERCHANT_ID", "merch_demo_001")

    res = client.post(
        "/auth/signin",
        json={"email": "merchant@test.com", "password": "anything"},
    )

    assert res.status_code == 200
    assert "merchant_id" not in res.json()["user"]


def test_signin_backfills_demo_merchant_id_when_fixtures_enabled_outside_production(
    monkeypatch,
):
    client = _client_with_merchant_row(monkeypatch)
    monkeypatch.setattr(
        auth_routes_module.settings, "enable_internal_demo_fixtures", True
    )
    _clear_environment_labels(monkeypatch)
    monkeypatch.setenv("DEMO_MERCHANT_ID", "merch_demo_001")

    res = client.post(
        "/auth/signin",
        json={"email": "merchant@test.com", "password": "anything"},
    )

    assert res.status_code == 200
    payload = res.json()
    assert payload["user"]["role"] == "merchant"
    assert payload["user"]["merchant_id"] == "merch_demo_001"
