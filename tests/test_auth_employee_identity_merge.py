import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import auth as auth_module
from utils.auth import decode_token, hash_password


def _client():
    app = FastAPI()
    app.include_router(auth_module.router)
    return TestClient(app)


def test_api_auth_login_prefers_active_employee_identity_over_existing_public_user(monkeypatch):
    password = "Admin123!"
    user_record = {
        "id": 44,
        "email": "peng@pivota.cc",
        "password_hash": hash_password(password),
        "full_name": "Public Peng",
        "role": "merchant",
        "active": True,
        "merchant_id": "merch_old",
    }
    employee_record = {
        "employee_id": "emp_peng",
        "name": "Peng Xu",
        "email": "peng@pivota.cc",
        "password": None,
        "role": "admin",
        "status": "active",
        "permissions": '["reviews.read"]',
    }
    executed = []

    async def fake_fetch_one(*args, **kwargs):
        query = str(kwargs.get("query") or (args[0] if args else ""))
        if "FROM employees" in query:
            return employee_record
        if "FROM users" in query:
            return user_record
        return None

    async def fake_execute(*args, **kwargs):
        query = str(kwargs.get("query") or (args[0] if args else ""))
        values = kwargs.get("values")
        if values is None and len(args) > 1:
            values = args[1]
        executed.append((query, values))
        return None

    monkeypatch.setattr(auth_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(auth_module.database, "execute", fake_execute)

    res = _client().post(
        "/api/auth/login",
        json={"email": " peng@pivota.cc ", "password": password},
    )

    assert res.status_code == 200
    payload = res.json()
    assert payload["success"] is True
    assert payload["user"]["email"] == "peng@pivota.cc"
    assert payload["user"]["role"] == "admin"
    assert payload["user"]["employee_id"] == "emp_peng"
    assert "merchant_id" not in payload["user"]

    token_payload = decode_token(payload["token"])
    assert token_payload["role"] == "admin"
    assert token_payload["employee_id"] == "emp_peng"
    assert "merchant_id" not in token_payload

    assert any("UPDATE users" in query and "merchant_id = NULL" in query for query, _ in executed)


def test_api_auth_login_employee_portal_uses_scoped_employee_membership(monkeypatch):
    password = "Admin123!"
    user_record = {
        "id": 44,
        "email": "peng@pivota.cc",
        "password_hash": hash_password(password),
        "full_name": "Public Peng",
        "role": "merchant",
        "active": True,
        "merchant_id": "merch_old",
    }
    employee_record = {
        "employee_id": "emp_peng",
        "name": "Peng Xu",
        "email": "peng@pivota.cc",
        "password": None,
        "role": "admin",
        "status": "active",
        "permissions": ["reviews.read"],
    }
    employee_membership = {
        "membership_id": "membership_emp_peng",
        "identity_id": "identity_peng",
        "membership_type": "employee",
        "role": "admin",
        "status": "active",
        "entity_id": "emp_peng",
        "permissions": ["reviews.read"],
        "identity": {
            "identity_id": "identity_peng",
            "email": "peng@pivota.cc",
            "email_normalized": "peng@pivota.cc",
            "full_name": "Peng Xu",
            "status": "active",
        },
    }

    async def fake_fetch_one(*args, **kwargs):
        query = str(kwargs.get("query") or (args[0] if args else ""))
        if "FROM employees" in query:
            return employee_record
        if "FROM users" in query:
            return user_record
        return None

    async def fake_execute(*args, **kwargs):
        return None

    async def fake_get_active_membership(email, membership_type):
        if email == "peng@pivota.cc" and membership_type == "employee":
            return employee_membership
        return None

    monkeypatch.setattr(auth_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(auth_module.database, "execute", fake_execute)
    monkeypatch.setattr(auth_module, "_safe_get_active_membership", fake_get_active_membership)

    res = _client().post(
        "/api/auth/login",
        json={"email": "peng@pivota.cc", "password": password, "portal": "employee"},
    )

    assert res.status_code == 200
    payload = res.json()
    assert payload["user"]["id"] == "identity_peng"
    assert payload["user"]["membership_type"] == "employee"
    assert payload["user"]["employee_id"] == "emp_peng"
    assert "merchant_id" not in payload["user"]

    token_payload = decode_token(payload["token"])
    assert token_payload["sub"] == "identity_peng"
    assert token_payload["aud"] == "employee-portal"
    assert token_payload["membership_type"] == "employee"
    assert token_payload["membership_id"] == "membership_emp_peng"
    assert token_payload["employee_id"] == "emp_peng"
    assert "merchant_id" not in token_payload


def test_api_auth_login_employee_portal_rejects_customer_or_merchant_only_identity(monkeypatch):
    password = "Admin123!"
    user_record = {
        "id": 44,
        "email": "peng@pivota.cc",
        "password_hash": hash_password(password),
        "full_name": "Public Peng",
        "role": "merchant",
        "active": True,
        "merchant_id": "merch_old",
    }

    async def fake_fetch_one(*args, **kwargs):
        query = str(kwargs.get("query") or (args[0] if args else ""))
        if "FROM employees" in query:
            return None
        if "FROM users" in query:
            return user_record
        return None

    async def fake_execute(*args, **kwargs):
        return None

    async def fake_get_active_membership(email, membership_type):
        return None

    monkeypatch.setattr(auth_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(auth_module.database, "execute", fake_execute)
    monkeypatch.setattr(auth_module, "_safe_get_active_membership", fake_get_active_membership)

    res = _client().post(
        "/api/auth/login",
        json={"email": "peng@pivota.cc", "password": password, "portal": "employee"},
    )

    assert res.status_code == 403
    assert "No active employee membership" in res.json()["detail"]


def test_api_auth_login_backfills_users_row_for_legacy_employee_only(monkeypatch):
    password = "Admin123!"
    employee_record = {
        "employee_id": "emp_peng",
        "name": "Peng Xu",
        "email": "peng@pivota.cc",
        "password": hashlib.sha256(f"{password}pivota_employee_salt_v1".encode()).hexdigest(),
        "role": "employee",
        "status": "active",
        "permissions": [],
    }
    executed = []

    async def fake_fetch_one(*args, **kwargs):
        query = str(kwargs.get("query") or (args[0] if args else ""))
        if "FROM employees" in query:
            return employee_record
        if "FROM users" in query:
            return None
        return None

    async def fake_execute(*args, **kwargs):
        query = str(kwargs.get("query") or (args[0] if args else ""))
        values = kwargs.get("values")
        if values is None and len(args) > 1:
            values = args[1]
        executed.append((query, values))
        return None

    monkeypatch.setattr(auth_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(auth_module.database, "execute", fake_execute)

    res = _client().post(
        "/api/auth/login",
        json={"email": "peng@pivota.cc", "password": password},
    )

    assert res.status_code == 200
    payload = res.json()
    assert payload["user"]["role"] == "employee"
    assert payload["user"]["employee_id"] == "emp_peng"

    sync_values = [values for query, values in executed if "INSERT INTO users" in query]
    assert sync_values
    assert sync_values[0]["email"] == "peng@pivota.cc"
    assert sync_values[0]["role"] == "employee"
    assert sync_values[0]["password_hash"].startswith("$2")
