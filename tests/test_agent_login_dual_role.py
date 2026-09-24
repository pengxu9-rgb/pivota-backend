"""POST /agent/account/login admits the owner of an agent whatever users.role says.

`users` holds one role per email, and an employee-portal login or reset rewrites it
(routes/auth._sync_employee_auth_user). An agent owner who is also staff therefore
became role='super_admin' while still owning the agent, and the developer portal
refused them with "This login is for agents only" even though forgot/reset on that
portal accepted them (_email_has_portal_membership counts the owned agents row).

Pinned here:
1. a non-agent role that owns an agents row logs in and gets an agent-scoped token;
2. a non-agent role that owns no agent is still refused 403 "agents only";
3. role='agent' with no agent row keeps its 404;
4. a wrong password is a 401 before anything about role or status is revealed;
5. login never rewrites users.role.
"""

import base64
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

EMAIL = "dual-role@example.com"
PASSWORD = "longenough1"


def _client(monkeypatch, *, role: str, owns_agent: bool, active: bool = True):
    import db.agents as agents_db
    import routes.agent_account as module
    from utils.auth import hash_password

    executed: list[str] = []

    async def fetch_one(query, values=None):
        q = " ".join(str(query).split())
        if q.startswith("SELECT id, email, password_hash"):
            return {
                "id": 11,
                "email": EMAIL,
                "password_hash": hash_password(PASSWORD),
                "full_name": "Dual Role",
                "role": role,
                "active": active,
            }
        if "FROM agents" in q and "owner_email" in q:
            if not owns_agent:
                return None
            return {
                "agent_id": "agent_dual",
                "agent_name": "Dual Agent",
                "owner_email": EMAIL,
                "api_key": "redacted:agent_dual",
                "agent_type": "basic",
            }
        return None

    async def execute(query, values=None):
        executed.append(" ".join(str(query).split()))

    async def _no_membership(**_kwargs):
        return None

    monkeypatch.setattr(module.database, "fetch_one", fetch_one)
    monkeypatch.setattr(module.database, "execute", execute)
    monkeypatch.setattr(module, "_sync_agent_auth_membership", _no_membership)
    monkeypatch.setattr(agents_db, "IS_POSTGRES", True)

    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app), executed


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _login(client, password=PASSWORD):
    return client.post("/agent/account/login", json={"email": EMAIL, "password": password})


def test_staff_role_that_owns_an_agent_logs_in_with_an_agent_token(monkeypatch):
    client, executed = _client(monkeypatch, role="super_admin", owns_agent=True)

    resp = _login(client)

    assert resp.status_code == 200, resp.text
    claims = _claims(resp.json()["token"])
    assert claims["role"] == "agent"
    assert claims["agent_id"] == "agent_dual"
    assert claims["aud"] == "agent-portal"
    assert resp.json()["agent"]["agent_id"] == "agent_dual"
    # 5. the employee role is left alone; login only stamps last_login.
    assert not any("SET role" in q or "role =" in q for q in executed), executed


def test_merchant_role_that_owns_an_agent_logs_in(monkeypatch):
    client, _ = _client(monkeypatch, role="merchant", owns_agent=True)

    assert _login(client).status_code == 200


def test_non_agent_role_without_an_agent_is_still_refused(monkeypatch):
    client, _ = _client(monkeypatch, role="super_admin", owns_agent=False)

    resp = _login(client)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "This login is for agents only"


def test_agent_role_without_an_agent_row_keeps_its_404(monkeypatch):
    client, _ = _client(monkeypatch, role="agent", owns_agent=False)

    resp = _login(client)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agent record not found"


def test_wrong_password_reveals_neither_role_nor_status(monkeypatch):
    for role, owns_agent, active in (
        ("super_admin", False, True),  # was 403 "agents only" before the password check
        ("agent", True, False),  # was 403 "deactivated" before the password check
    ):
        client, _ = _client(monkeypatch, role=role, owns_agent=owns_agent, active=active)

        resp = _login(client, password="wrong-password1")

        assert resp.status_code == 401, (role, resp.text)
        assert resp.json()["detail"] == "Invalid email or password"


def test_deactivated_owner_is_refused_after_a_correct_password(monkeypatch):
    client, _ = _client(monkeypatch, role="super_admin", owns_agent=True, active=False)

    resp = _login(client)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Account is deactivated"
