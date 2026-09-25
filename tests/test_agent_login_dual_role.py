"""POST /agent/account/login admits an agent owner whose role an employee sync rewrote.

`users` holds one role per email, and an employee-portal login or reset rewrites it
(routes/auth._sync_employee_auth_user). An agent owner who is also staff therefore
became role='super_admin' while still owning the agent, and the developer portal
refused them with "This login is for agents only".

Pinned here:
1. a staff role with an ACTIVE employees row that owns an agents row logs in and gets
   an agent-scoped token;
2. owning the agents row is NOT enough on its own: a merchant who moved their login
   email onto an agent's owner_email (PUT /merchant/profile does not verify it), and a
   staff role with no active employees row, are both refused 403 "agents only";
2b. a merchant that converted FROM this agent (it holds an active agent membership bound
   to this agent_id) logs in; a membership for any other agent does not count;
3. a non-agent role that owns no agent is still refused 403; role='agent' with no agent
   row keeps its 404;
4. a wrong password is a 401 before anything about role or status is revealed;
5. login never rewrites users.role.
"""

import base64
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

EMAIL = "dual-role@example.com"
PASSWORD = "longenough1"


def _client(
    monkeypatch,
    *,
    role: str,
    owns_agent: bool,
    active: bool = True,
    employee: bool = True,
    agent_membership_for: str | None = None,
    membership_calls: list | None = None,
):
    import db.agents as agents_db
    import db.auth_identity as auth_identity
    import routes.agent_account as module
    import routes.auth as auth_module
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

    async def fetch_employee(email):
        return {"employee_id": "emp_1", "email": email, "role": role} if employee else None

    monkeypatch.setattr(module.database, "fetch_one", fetch_one)
    monkeypatch.setattr(module.database, "execute", execute)
    monkeypatch.setattr(module, "_sync_agent_auth_membership", _no_membership)
    async def has_membership(*, email, membership_type, entity_id):
        if membership_calls is not None:
            membership_calls.append((email, membership_type, entity_id))
        return membership_type == "agent" and entity_id == agent_membership_for

    monkeypatch.setattr(auth_module, "_fetch_active_employee_identity", fetch_employee)
    monkeypatch.setattr(auth_identity, "has_active_membership_for_entity", has_membership)
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


def test_merchant_who_took_an_agent_owner_email_is_refused(monkeypatch):
    """PUT /merchant/profile moves users.email to any unclaimed address unverified, so a
    merchant can land on an agent's owner_email; owning the row must not admit them."""
    client, _ = _client(monkeypatch, role="merchant", owns_agent=True, employee=False)

    resp = _login(client)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "This login is for agents only"


def test_staff_role_without_an_active_employee_row_is_refused(monkeypatch):
    client, _ = _client(monkeypatch, role="admin", owns_agent=True, employee=False)

    resp = _login(client)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "This login is for agents only"


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


def test_merchant_converted_from_this_agent_logs_in_through_its_membership(monkeypatch):
    """hr@chydan.com in prod: registered as agent_659a77ae254b8f4c, then converted into a
    merchant (password-verified), which rewrote users.role to 'merchant'."""
    calls: list = []
    client, executed = _client(
        monkeypatch,
        role="merchant",
        owns_agent=True,
        employee=False,
        agent_membership_for="agent_dual",
        membership_calls=calls,
    )

    resp = _login(client)

    assert resp.status_code == 200, resp.text
    claims = _claims(resp.json()["token"])
    assert claims["role"] == "agent"
    assert claims["agent_id"] == "agent_dual"
    # the membership is asked about THIS agent, not "any agent membership"
    assert calls == [(EMAIL, "agent", "agent_dual")]
    assert not any("SET role" in q or "role =" in q for q in executed), executed


def test_a_membership_for_a_different_agent_does_not_admit(monkeypatch):
    client, _ = _client(
        monkeypatch,
        role="merchant",
        owns_agent=True,
        employee=False,
        agent_membership_for="agent_someone_else",
    )

    resp = _login(client)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "This login is for agents only"
