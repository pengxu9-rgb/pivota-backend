from __future__ import annotations

import hashlib

import pytest
from fastapi import HTTPException

from routes import auth

DEMO_CREDENTIALS = [
    ("employee@pivota.com", "Admin123!"),
    ("superadmin@pivota.com", "admin123"),
]


def _stub_db(monkeypatch: pytest.MonkeyPatch, fetch_one=None) -> None:
    async def fake_ready() -> None:
        return None

    async def default_fetch_one(_query: str, _values: dict):
        return None

    monkeypatch.setattr(auth, "_ensure_auth_database_ready", fake_ready)
    monkeypatch.setattr(auth, "_auth_fetch_one", fetch_one or default_fetch_one)


def _enable_demo_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.settings, "enable_internal_demo_fixtures", True)
    # Make sure nothing in the ambient environment resolves to production (or
    # to an unlabeled managed host, which fails closed to production).
    for var in (
        "PIVOTA_ENV",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_ENVIRONMENT_NAME",
        "K_SERVICE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("email,password", DEMO_CREDENTIALS)
async def test_api_auth_login_rejects_demo_employee_by_default(
    monkeypatch: pytest.MonkeyPatch, email: str, password: str
) -> None:
    """In the default configuration the hardcoded demo credentials are dead.

    ENABLE_INTERNAL_DEMO_FIXTURES defaults to false, so the demo lane must
    401 exactly like any other unknown credential pair.
    """
    _stub_db(monkeypatch)
    assert auth.settings.enable_internal_demo_fixtures is False

    with pytest.raises(HTTPException) as exc_info:
        await auth.login(auth.LoginRequest(email=email, password=password))

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("prod_var", ["PIVOTA_ENV", "RAILWAY_ENVIRONMENT"])
@pytest.mark.parametrize("email,password", DEMO_CREDENTIALS)
async def test_api_auth_login_rejects_demo_employee_in_production_even_with_flag(
    monkeypatch: pytest.MonkeyPatch, prod_var: str, email: str, password: str
) -> None:
    """Mutant check: the demo gate must be unsatisfiable in production.

    Even with ENABLE_INTERNAL_DEMO_FIXTURES forced on, an environment that
    resolves to production keeps the lane dark — a hardcoded role=admin
    password is a hardcoded password to /admin/payment-issuers charge
    authority.
    """
    _stub_db(monkeypatch)
    _enable_demo_fixtures(monkeypatch)
    monkeypatch.setenv(prod_var, "production")

    with pytest.raises(HTTPException) as exc_info:
        await auth.login(auth.LoginRequest(email=email, password=password))

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_api_auth_login_rejects_demo_employee_on_unlabeled_managed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A managed host that cannot be classified fails closed to production."""
    _stub_db(monkeypatch)
    _enable_demo_fixtures(monkeypatch)
    monkeypatch.setenv("K_SERVICE", "pivota-backend")  # Cloud Run, no env label

    with pytest.raises(HTTPException) as exc_info:
        await auth.login(
            auth.LoginRequest(email="employee@pivota.com", password="Admin123!")
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_api_auth_login_accepts_demo_employee_when_fixtures_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-dev lane: flag on, non-production environment, no users row."""
    _stub_db(monkeypatch)
    _enable_demo_fixtures(monkeypatch)

    response = await auth.login(
        auth.LoginRequest(email="employee@pivota.com", password="Admin123!")
    )

    assert response.success is True
    assert response.user["email"] == "employee@pivota.com"
    assert response.user["role"] == "admin"
    assert response.token


@pytest.mark.asyncio
async def test_api_auth_login_accepts_legacy_employee_table_when_users_row_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    password = "Admin123!"
    salt = "pivota_employee_salt_v1"
    hashed_password = hashlib.sha256(f"{password}{salt}".encode()).hexdigest()

    async def fake_fetch_one(query: str, _values: dict):
        if "FROM users" in query:
            return None
        if "FROM employees" in query:
            return {
                "employee_id": "emp_test",
                "name": "Test Employee",
                "email": "reviewer@pivota.com",
                "password": hashed_password,
                "role": "employee",
            }
        return None

    _stub_db(monkeypatch, fetch_one=fake_fetch_one)

    response = await auth.login(auth.LoginRequest(email="reviewer@pivota.com", password=password))

    assert response.success is True
    # Identity-first contract: user.id is the canonical identity_id. A
    # legacy-employees-table login with no users/auth_identities rows gets the
    # synthesized fallback identity ("legacy:<email>"); the employee_id claim
    # still carries the legacy row's key.
    assert response.user["id"] == "legacy:reviewer@pivota.com"
    assert response.user["identity_id"] == "legacy:reviewer@pivota.com"
    assert response.user["employee_id"] == "emp_test"
    assert response.user["role"] == "employee"


@pytest.mark.asyncio
async def test_api_auth_login_rejects_bad_demo_password_even_with_fixtures_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_db(monkeypatch)
    _enable_demo_fixtures(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        await auth.login(auth.LoginRequest(email="employee@pivota.com", password="wrong-password"))

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_api_auth_login_allows_demo_employee_when_canonical_password_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_one(query: str, _values: dict):
        if "FROM users" in query:
            return {
                "id": "user_employee",
                "email": "employee@pivota.com",
                "password_hash": "different-canonical-hash",
                "full_name": "Canonical Employee",
                "role": "admin",
                "active": True,
                "merchant_id": None,
            }
        return None

    _stub_db(monkeypatch, fetch_one=fake_fetch_one)
    _enable_demo_fixtures(monkeypatch)
    monkeypatch.setattr(auth, "verify_password", lambda _password, _password_hash: False)

    response = await auth.login(auth.LoginRequest(email="employee@pivota.com", password="Admin123!"))

    assert response.success is True
    assert response.user["email"] == "employee@pivota.com"
    assert response.user["role"] == "admin"


def _merchant_user_row(merchant_id_in_db=None) -> dict:
    return {
        "id": "user_merchant_1",
        "email": "merchant@test.com",
        "password_hash": "irrelevant-hash",
        "full_name": "Test Merchant",
        "role": "merchant",
        "active": True,
        "merchant_id": merchant_id_in_db,
    }


def _stub_merchant_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real, bcrypt-authenticated `users` row with role=merchant and no
    merchant_id set — the exact row shape DEMO_MERCHANT_IDS backfills onto."""

    async def fake_fetch_one(query: str, _values: dict):
        if "FROM users" in query:
            return _merchant_user_row()
        return None

    async def fake_execute(_query: str, _values: dict):
        return None

    _stub_db(monkeypatch, fetch_one=fake_fetch_one)
    monkeypatch.setattr(auth, "_auth_execute", fake_execute)
    monkeypatch.setattr(auth, "verify_password", lambda _password, _password_hash: True)


@pytest.mark.asyncio
async def test_api_auth_login_backfills_demo_merchant_id_when_fixtures_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-dev lane: flag on, non-production, DEMO_MERCHANT_ID set — the
    same fixtures a developer relies on to exercise the merchant portal
    without a merchant_onboarding row."""
    _stub_merchant_login(monkeypatch)
    _enable_demo_fixtures(monkeypatch)
    monkeypatch.setenv("DEMO_MERCHANT_ID", "merch_demo_001")

    response = await auth.login(auth.LoginRequest(email="merchant@test.com", password="Admin123!"))

    assert response.success is True
    assert response.user["role"] == "merchant"
    assert response.user["merchant_id"] == "merch_demo_001"


@pytest.mark.asyncio
async def test_api_auth_login_rejects_demo_merchant_id_backfill_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutant check: DEMO_MERCHANT_ID alone (fixtures flag off) must not
    bind an attacker-registered merchant@test.com to the demo merchant_id.
    With no merchant_id resolved, the merchant membership never seeds and
    the login is refused for lack of an active portal membership."""
    _stub_merchant_login(monkeypatch)
    assert auth.settings.enable_internal_demo_fixtures is False
    monkeypatch.setenv("DEMO_MERCHANT_ID", "merch_demo_001")

    with pytest.raises(HTTPException) as exc_info:
        await auth.login(auth.LoginRequest(email="merchant@test.com", password="Admin123!"))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("prod_var", ["PIVOTA_ENV", "RAILWAY_ENVIRONMENT"])
async def test_api_auth_login_rejects_demo_merchant_id_backfill_in_production_even_with_flag(
    monkeypatch: pytest.MonkeyPatch, prod_var: str,
) -> None:
    """Mutant check: the flag alone must not arm the merchant_id backfill in
    production, exactly like _demo_employee_accounts() above. This is the
    lane PR #1889 left ungated: POST /api/auth/register lets anyone
    self-register merchant@test.com, and this backfill used to bind that
    attacker-controlled account to the real DEMO_MERCHANT_ID with no
    is_production() check."""
    _stub_merchant_login(monkeypatch)
    _enable_demo_fixtures(monkeypatch)
    monkeypatch.setenv(prod_var, "production")
    monkeypatch.setenv("DEMO_MERCHANT_ID", "merch_demo_001")

    with pytest.raises(HTTPException) as exc_info:
        await auth.login(auth.LoginRequest(email="merchant@test.com", password="Admin123!"))

    assert exc_info.value.status_code == 403
