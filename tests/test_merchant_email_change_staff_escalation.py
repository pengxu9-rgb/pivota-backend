"""Merchant -> staff escalation through an unverified login-email change.

The chain, on main before this fix:
  1. PUT /merchant/profile moved the merchant's login (users.email and
     auth_identities) onto ANY address absent from users/auth_identities. It
     never checked employees or agents, and never verified the mailbox.
  2. /api/auth/login matches an employees row by email alone. With a merchant
     users row at that address, the merchant's own password verified, the
     users row was rewritten to the employee role (password_hash kept), an
     employee membership was seeded with the merchant's hash, and the response
     was a staff token.

These tests fail on that code: each link is closed at its own source.
"""

import contextlib

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes import auth as auth_module
from utils.auth import decode_token, hash_password

MERCHANT_PASSWORD = "Merchant123!"
STAFF_EMAIL = "ops-admin@pivota.cc"


class World:
    """Just enough of users / employees / agents / merchant_onboarding for the
    profile route and the login route to share one state."""

    def __init__(self):
        self.users = {
            "merchant@shop.example": {
                "id": 7,
                "email": "merchant@shop.example",
                "password_hash": hash_password(MERCHANT_PASSWORD),
                "full_name": "Shop Owner",
                "role": "merchant",
                "active": True,
                "merchant_id": "merch_attacker",
            }
        }
        # An admin created through /admin/employee/reset-password, which inserts
        # an employees row and no users row.
        self.employees = {
            STAFF_EMAIL: {
                "employee_id": "emp_ops",
                "name": "Ops Admin",
                "email": STAFF_EMAIL,
                "password": None,
                "role": "admin",
                "status": "active",
                "permissions": [],
            }
        }
        self.agents = {}  # email -> (agent_id, the column that holds it)
        # What routes.agent_management._agent_email_columns reports for the
        # live table; () is its "could not probe" answer.
        self.agent_columns = ("owner_email", "email")
        self.merchants = {
            "merch_attacker": {
                "merchant_id": "merch_attacker",
                "business_name": "Shop",
                "contact_email": "merchant@shop.example",
                "contact_phone": None,
                "website": None,
            }
        }
        self.executed = []
        self.fail_on = None

    async def fetch_one(self, query=None, values=None, **kwargs):
        q = str(query)
        values = values or {}
        email = (values.get("email") or "").strip().lower()
        if self.fail_on and self.fail_on in q:
            raise RuntimeError("connection reset")
        if "FROM employees" in q:
            return self.employees.get(email)
        if "FROM agents" in q:
            column = "owner_email" if "TRIM(owner_email)" in q else "email"
            agent = self.agents.get(email)
            return {"agent_id": agent[0]} if agent and agent[1] == column else None
        if "FROM merchant_onboarding" in q:
            if "merchant_id = :merchant_id" in q and "contact_email" not in q.split("WHERE")[1]:
                return self.merchants.get(values["merchant_id"])
            for row in self.merchants.values():
                if (row["contact_email"] or "").lower() == email and row["merchant_id"] != values.get("merchant_id"):
                    return row
            return None
        if "FROM users" in q:
            return self.users.get(email)
        return None

    async def execute(self, query=None, values=None, **kwargs):
        q = str(query)
        values = dict(values or {})
        self.executed.append((q, values))
        if "UPDATE users SET email" in q:
            row = self.users.pop(values["old_email"], None)
            if row:
                row["email"] = values["new_email"]
                self.users[values["new_email"]] = row
        elif "UPDATE merchant_onboarding" in q:
            self.merchants[values["merchant_id"]]["contact_email"] = values["contact_email"]
        elif "UPDATE users" in q and ":role" in q:
            row = self.users.get(values["email"])
            if row:
                row["role"] = values["role"]
        return None

    def transaction(self):
        @contextlib.asynccontextmanager
        async def _txn():
            yield

        return _txn()


@pytest.fixture()
def world(monkeypatch):
    import db.auth_identity as auth_identity_module
    import routes.merchant_api_extensions as merchant_module

    w = World()
    memberships = []

    async def fake_upsert_membership(**kwargs):
        memberships.append(kwargs)
        return None

    async def no_membership(email, membership_type):
        return None

    async def no_identity(email, password):
        return None

    async def no_event(**kwargs):
        return None

    import routes.agent_management as agent_management

    async def fake_agent_email_columns():
        return w.agent_columns

    for module in (auth_module, merchant_module):
        monkeypatch.setattr(module, "database", w)
    monkeypatch.setattr(agent_management, "_agent_email_columns", fake_agent_email_columns)
    monkeypatch.setattr(auth_module, "_safe_upsert_membership", fake_upsert_membership)
    monkeypatch.setattr(auth_module, "_safe_get_active_membership", no_membership)
    monkeypatch.setattr(auth_module, "_safe_verify_identity_password", no_identity)
    monkeypatch.setattr(auth_module, "_safe_record_identity_event", no_event)
    monkeypatch.setattr(auth_identity_module, "record_identity_event", no_event)
    w.memberships = memberships
    w.merchant_module = merchant_module
    return w


def _login(email, password, portal=None):
    app = FastAPI()
    app.include_router(auth_module.router)
    body = {"email": email, "password": password}
    if portal:
        body["portal"] = portal
    return TestClient(app).post("/api/auth/login", json=body)


MERCHANT_USER = {
    "role": "merchant",
    "merchant_id": "merch_attacker",
    "email": "merchant@shop.example",
    "sub": "identity_attacker",
}


async def _change_email(world, new_email):
    return await world.merchant_module.update_merchant_profile(
        profile_data={"contact_email": new_email},
        current_user=dict(MERCHANT_USER),
    )


def _assert_nothing_written(world):
    assert [q for q, _ in world.executed if "UPDATE" in q] == []
    assert "merchant@shop.example" in world.users


# --- link 1: the email change ------------------------------------------------


@pytest.mark.asyncio
async def test_full_chain_merchant_cannot_take_an_employee_address_then_log_in_as_staff(world):
    with pytest.raises(HTTPException) as exc_info:
        await _change_email(world, STAFF_EMAIL.upper())
    assert exc_info.value.status_code == 409
    _assert_nothing_written(world)

    # With the address still free of a users row, the merchant's password does
    # not open it.
    res = _login(STAFF_EMAIL, MERCHANT_PASSWORD)
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_email_change_refuses_an_inactive_employee_address(world):
    world.employees[STAFF_EMAIL]["status"] = "inactive"
    with pytest.raises(HTTPException) as exc_info:
        await _change_email(world, STAFF_EMAIL)
    assert exc_info.value.status_code == 409
    _assert_nothing_written(world)


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["owner_email", "email"])
async def test_email_change_refuses_an_agent_address(world, column):
    world.agents["owner@agent.example"] = ("agent_123", column)
    with pytest.raises(HTTPException) as exc_info:
        await _change_email(world, "Owner@Agent.example")
    assert exc_info.value.status_code == 409
    _assert_nothing_written(world)


@pytest.mark.asyncio
async def test_email_change_refuses_another_merchants_contact_email(world):
    world.merchants["merch_victim"] = {
        "merchant_id": "merch_victim",
        "business_name": "Victim",
        "contact_email": "victim@shop.example",
        "contact_phone": None,
        "website": None,
    }
    with pytest.raises(HTTPException) as exc_info:
        await _change_email(world, "victim@shop.example")
    assert exc_info.value.status_code == 409
    _assert_nothing_written(world)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["employees", "agents probe"])
async def test_email_change_fails_closed_when_a_check_cannot_run(world, failure):
    if failure == "employees":
        world.fail_on = "FROM employees"
    else:
        world.agent_columns = ()
    with pytest.raises(HTTPException) as exc_info:
        await _change_email(world, "fresh@shop.example")
    assert exc_info.value.status_code == 503
    _assert_nothing_written(world)


@pytest.mark.asyncio
async def test_email_change_to_an_unclaimed_address_still_works(world):
    world.agent_columns = ("owner_email",)  # a table without agents.email is not a failure
    response = await _change_email(world, "fresh@shop.example")
    assert response["login_email_changed"] is True
    assert "fresh@shop.example" in world.users


# --- link 2: the login promotion ---------------------------------------------


def _squat(world):
    """A merchant users row already sitting at the employee's address, however
    it got there (a change made before this fix, or an employees row created
    after the merchant moved in)."""
    row = world.users.pop("merchant@shop.example")
    row["email"] = STAFF_EMAIL
    world.users[STAFF_EMAIL] = row


@pytest.mark.parametrize("portal", [None, "employee"])
def test_merchant_password_on_a_squatted_employee_address_is_not_staff(world, portal):
    _squat(world)

    res = _login(STAFF_EMAIL, MERCHANT_PASSWORD, portal=portal)

    if portal == "employee":
        assert res.status_code == 403
    else:
        assert res.status_code == 200
        payload = res.json()
        assert payload["user"]["role"] == "merchant"
        assert "employee_id" not in payload["user"]
        assert decode_token(payload["token"])["role"] == "merchant"

    assert world.users[STAFF_EMAIL]["role"] == "merchant"
    assert not any("INSERT INTO users" in q for q, _ in world.executed)
    assert not any(m.get("membership_type") == "employee" for m in world.memberships)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["merchant", "agent", "buyer"])
async def test_sync_refuses_to_promote_a_non_staff_row_without_a_password(world, role):
    _squat(world)
    world.users[STAFF_EMAIL]["role"] = role

    promoted = await auth_module._sync_employee_auth_user(
        employee=world.employees[STAFF_EMAIL],
        plain_password=None,
    )

    assert promoted is False
    assert world.users[STAFF_EMAIL]["role"] == role
    assert [q for q, _ in world.executed if "UPDATE users" in q or "INSERT INTO users" in q] == []
    assert world.memberships == []


@pytest.mark.asyncio
async def test_sync_still_repairs_a_staff_row_without_a_password(world):
    _squat(world)
    world.users[STAFF_EMAIL]["role"] = "employee"

    promoted = await auth_module._sync_employee_auth_user(
        employee=world.employees[STAFF_EMAIL],
        plain_password=None,
    )

    assert promoted is True
    assert world.users[STAFF_EMAIL]["role"] == "admin"
    update = next(q for q, _ in world.executed if "UPDATE users" in q)
    # The UPDATE itself is guarded too, so a row that turns non-staff between the
    # read and the write is not promoted.
    assert "'merchant'" not in update
    assert "LOWER(COALESCE(role, '')) IN (''" in update
    assert [m["membership_type"] for m in world.memberships] == ["employee"]


def test_the_sql_staff_role_list_is_employee_auth_roles():
    """The guarded UPDATE spells EMPLOYEE_AUTH_ROLES as a literal so the static
    PREPARE sweep can plan it; the two must not drift."""
    import inspect
    import re

    source = inspect.getsource(auth_module._sync_employee_auth_user)
    match = re.search(r"LOWER\(COALESCE\(role, ''\)\) IN \(''((?:, '[a-z_]+')*)\)", source)
    assert match, "guarded UPDATE not found"
    spelled = set(re.findall(r"'([a-z_]+)'", match.group(1)))
    assert spelled == auth_module.EMPLOYEE_AUTH_ROLES
