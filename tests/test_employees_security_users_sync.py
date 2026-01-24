from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import employees_security as employees_security_module


def test_update_employee_syncs_users_table(monkeypatch):
    app = FastAPI()
    app.include_router(employees_security_module.router)
    client = TestClient(app)

    calls = []

    async def fake_execute(query, values=None):
        calls.append((str(query), values or {}))
        return None

    async def fake_fetch_one(query, values=None):
        if "SELECT email FROM employees" in str(query):
            return {"email": "peng@pivota.cc"}
        return None

    monkeypatch.setattr(employees_security_module.database, "execute", fake_execute)
    monkeypatch.setattr(employees_security_module.database, "fetch_one", fake_fetch_one)

    res = client.put(
        "/employees/emp_test?name=Peng&role=admin&status=inactive",
        headers={"Authorization": "Bearer test-token"},
    )

    assert res.status_code == 200
    assert any(
        "UPDATE users SET" in query and values.get("active") is False
        for query, values in calls
    )


def test_delete_employee_deactivates_users_table(monkeypatch):
    app = FastAPI()
    app.include_router(employees_security_module.router)
    client = TestClient(app)

    calls = []

    async def fake_execute(query, values=None):
        calls.append((str(query), values or {}))
        return None

    async def fake_fetch_one(query, values=None):
        if "SELECT email FROM employees" in str(query):
            return {"email": "peng@pivota.cc"}
        return None

    monkeypatch.setattr(employees_security_module.database, "execute", fake_execute)
    monkeypatch.setattr(employees_security_module.database, "fetch_one", fake_fetch_one)

    res = client.delete(
        "/employees/emp_test",
        headers={"Authorization": "Bearer test-token"},
    )

    assert res.status_code == 200
    assert any(
        "UPDATE users SET active" in query and values.get("active") is False
        for query, values in calls
    )

