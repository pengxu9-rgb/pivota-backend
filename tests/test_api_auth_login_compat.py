from __future__ import annotations

import hashlib

import pytest
from fastapi import HTTPException

from routes import auth


@pytest.mark.asyncio
async def test_api_auth_login_accepts_demo_employee_when_users_row_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ready() -> None:
        return None

    async def fake_fetch_one(_query: str, _values: dict):
        return None

    monkeypatch.setattr(auth, "_ensure_auth_database_ready", fake_ready)
    monkeypatch.setattr(auth, "_auth_fetch_one", fake_fetch_one)

    response = await auth.login(auth.LoginRequest(email="employee@pivota.com", password="Admin123!"))

    assert response.success is True
    assert response.user["email"] == "employee@pivota.com"
    assert response.user["role"] == "admin"
    assert response.token


@pytest.mark.asyncio
async def test_api_auth_login_accepts_legacy_employee_table_when_users_row_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    password = "Admin123!"
    salt = "pivota_employee_salt_v1"
    hashed_password = hashlib.sha256(f"{password}{salt}".encode()).hexdigest()

    async def fake_ready() -> None:
        return None

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

    monkeypatch.setattr(auth, "_ensure_auth_database_ready", fake_ready)
    monkeypatch.setattr(auth, "_auth_fetch_one", fake_fetch_one)

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
async def test_api_auth_login_rejects_bad_demo_password_when_users_row_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ready() -> None:
        return None

    async def fake_fetch_one(_query: str, _values: dict):
        return None

    monkeypatch.setattr(auth, "_ensure_auth_database_ready", fake_ready)
    monkeypatch.setattr(auth, "_auth_fetch_one", fake_fetch_one)

    with pytest.raises(HTTPException) as exc_info:
        await auth.login(auth.LoginRequest(email="employee@pivota.com", password="wrong-password"))

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_api_auth_login_allows_demo_employee_when_canonical_password_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ready() -> None:
        return None

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

    monkeypatch.setattr(auth, "_ensure_auth_database_ready", fake_ready)
    monkeypatch.setattr(auth, "_auth_fetch_one", fake_fetch_one)
    monkeypatch.setattr(auth, "verify_password", lambda _password, _password_hash: False)

    response = await auth.login(auth.LoginRequest(email="employee@pivota.com", password="Admin123!"))

    assert response.success is True
    assert response.user["email"] == "employee@pivota.com"
    assert response.user["role"] == "admin"
