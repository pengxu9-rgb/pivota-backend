import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import auth as auth_module
from utils.auth import decode_token, hash_password


def _client():
    app = FastAPI()
    app.include_router(auth_module.router)
    return TestClient(app)


def test_api_auth_login_does_not_promote_a_merchant_row_with_the_merchant_password(monkeypatch):
    """A merchant password is not an employee password.

    This test used to assert the opposite: an employees row with no password
    of its own, plus a merchant users row at the same address, and the MERCHANT
    password came back as an admin token, with the users row rewritten to
    role=admin. A merchant can move its login email onto an unclaimed address
    without proving the mailbox, so that was a merchant -> staff escalation.
    The login now authenticates the account whose password it verified.
    """
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

    memberships = []

    async def fake_upsert_membership(**kwargs):
        memberships.append(kwargs)
        return None

    async def no_membership(email, membership_type):
        return None

    monkeypatch.setattr(auth_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(auth_module.database, "execute", fake_execute)
    monkeypatch.setattr(auth_module, "_safe_upsert_membership", fake_upsert_membership)
    monkeypatch.setattr(auth_module, "_safe_get_active_membership", no_membership)

    res = _client().post(
        "/api/auth/login",
        json={"email": " peng@pivota.cc ", "password": password},
    )

    assert res.status_code == 200
    payload = res.json()
    assert payload["success"] is True
    assert payload["user"]["email"] == "peng@pivota.cc"
    assert payload["user"]["role"] == "merchant"
    assert payload["user"]["merchant_id"] == "merch_old"
    assert "employee_id" not in payload["user"]

    token_payload = decode_token(payload["token"])
    assert token_payload["role"] == "merchant"
    assert "employee_id" not in token_payload

    # The users row was not rewritten to staff, and no employee membership was
    # seeded with the merchant's password hash.
    assert not any("UPDATE users" in query and ":role" in query for query, _ in executed)
    assert not any("INSERT INTO users" in query for query, _ in executed)
    assert not any(m.get("membership_type") == "employee" for m in memberships)

    # Portal-scoped: the employee portal is refused outright.
    res = _client().post(
        "/api/auth/login",
        json={"email": "peng@pivota.cc", "password": password, "portal": "employee"},
    )
    assert res.status_code == 403


def test_api_auth_login_employee_password_still_reclaims_a_merchant_row(monkeypatch):
    """The employee's own (legacy employees.password) password is proof: the
    row is promoted and its password replaced by the employee's."""
    merchant_password = "Merchant123!"
    employee_password = "Employee123!"
    user_record = {
        "id": 44,
        "email": "peng@pivota.cc",
        "password_hash": hash_password(merchant_password),
        "full_name": "Public Peng",
        "role": "merchant",
        "active": True,
        "merchant_id": "merch_old",
    }
    employee_record = {
        "employee_id": "emp_peng",
        "name": "Peng Xu",
        "email": "peng@pivota.cc",
        "password": hashlib.sha256(f"{employee_password}pivota_employee_salt_v1".encode()).hexdigest(),
        "role": "admin",
        "status": "active",
        "permissions": [],
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

    async def no_membership(email, membership_type):
        return None

    monkeypatch.setattr(auth_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(auth_module.database, "execute", fake_execute)
    monkeypatch.setattr(auth_module, "_safe_get_active_membership", no_membership)

    res = _client().post(
        "/api/auth/login",
        json={"email": "peng@pivota.cc", "password": employee_password},
    )

    assert res.status_code == 200
    payload = res.json()
    assert payload["user"]["role"] == "admin"
    assert payload["user"]["employee_id"] == "emp_peng"

    upserts = [values for query, values in executed if "INSERT INTO users" in query]
    assert upserts and upserts[0]["role"] == "admin"
    assert upserts[0]["password_hash"].startswith("$2")
    assert auth_module.verify_password(employee_password, upserts[0]["password_hash"])


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
