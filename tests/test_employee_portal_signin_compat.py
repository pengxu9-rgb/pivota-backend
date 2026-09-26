import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import auth_routes as auth_routes_module
from utils.auth import hash_password as bcrypt_hash_password


def test_auth_signin_accepts_users_table_when_employees_missing(monkeypatch):
    """
    Regression: Employee Portal historically used `/auth/signin` (demo + employees table).
    Newer deployments create real users in `users` (bcrypt) via `/api/auth/*`.

    Ensure `/auth/signin` can authenticate against `users` when `employees` does not
    contain the account, so newly created accounts (e.g. *@pivota.cc) can login.
    """
    app = FastAPI()
    app.include_router(auth_routes_module.router)
    client = TestClient(app)

    password = "Admin123!"
    user_record = {
        "id": 123,
        "email": "peng@pivota.cc",
        "password_hash": bcrypt_hash_password(password),
        "full_name": "Peng",
        "role": "admin",
        "active": True,
        "merchant_id": None,
    }

    async def fake_fetch_one(query, values=None):
        q = str(query)
        if "FROM employees" in q:
            return None
        if "FROM users" in q:
            return user_record
        return None

    monkeypatch.setattr(auth_routes_module.database, "fetch_one", fake_fetch_one)

    res = client.post(
        "/auth/signin",
        json={"email": user_record["email"], "password": password},
    )

    assert res.status_code == 200
    payload = res.json()
    assert payload["status"] == "success"
    assert payload["user"]["email"] == user_record["email"]
    assert payload["user"]["role"] == user_record["role"]
    assert isinstance(payload["token"], str)
    assert payload["token"]

