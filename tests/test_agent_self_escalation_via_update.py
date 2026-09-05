"""An agent must not be able to widen its own authorization fields.

`PUT /agents/{agent_id}` lets an agent update its OWN record -- correctly, that
is what the ownership check on it is for. Three of the fields it accepted are
not settings, they are authorization:

  * `allowed_merchants` is the list of merchants the agent may act for. It is
    ENFORCED downstream -- routes/fulfillment_api.py:157 filters orders by it,
    and routes/agent_api.py `_context_can_access_merchant` falls back to it
    (and treats None as "all merchants"). An agent that could PUT this field
    could grant itself another merchant's orders, or send `null` and grant
    itself every merchant at once.
  * `rate_limit` / `daily_quota` are the commercial limits staff set on the
    agent; self-service defeats them.
  * `is_active` is what employee-only `DELETE /agents/{agent_id}` sets to
    False. An agent that can set it back to True undoes an employee action
    with an agent token.

Staff keep every one of these -- they are the ones meant to set them. This
file pins that split, and the missing None guard in
`utils.auth.can_access_merchant` that the same audit turned up.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

AGENT_A = "agent_aaaaaaaa"


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _token(claims: Dict[str, Any]) -> str:
    from utils.auth import create_access_token

    return create_access_token(claims)


def _auth(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _agent_token() -> str:
    return _token(
        {
            "sub": f"u-{AGENT_A}",
            "email": "owner_a@example.com",
            "role": "agent",
            "agent_id": AGENT_A,
        }
    )


def _staff_token(role: str = "admin") -> str:
    return _token({"sub": f"u-{role}", "email": f"{role}@example.com", "role": role})


class _UpdateSpy:
    """Stands in for routes.agent_management.database.

    `execute` is the sensitive action -- the UPDATE statement itself. Recording
    it is what makes "refused BEFORE the write" checkable: a 403 raised after
    `database.execute` had already run would satisfy a status-only assertion
    while the escalation had already landed.
    """

    def __init__(self) -> None:
        self.executed: List[Any] = []

    async def execute(self, query: Any, values: Optional[Dict[str, Any]] = None, *a, **kw):
        self.executed.append(query)
        return None

    async def fetch_one(self, *a, **kw):
        return None

    async def fetch_all(self, *a, **kw):
        return []

    def written_values(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for stmt in self.executed:
            params = getattr(stmt, "compile", None)
            if params is None:
                continue
            merged.update(dict(stmt.compile().params))
        return merged


@pytest.fixture
def update_spies(monkeypatch) -> _UpdateSpy:
    from routes import agent_management as mod

    async def _get_agent(agent_id: str) -> Dict[str, Any]:
        return {
            "agent_id": agent_id,
            "agent_name": "Caller Agent",
            "owner_email": "owner_a@example.com",
            "allowed_merchants": ["merchant_caller"],
            "rate_limit": 100,
            "daily_quota": 10000,
            "is_active": True,
        }

    spy = _UpdateSpy()
    monkeypatch.setattr(mod, "get_agent", _get_agent)
    monkeypatch.setattr(mod, "database", spy)
    return spy


@pytest.mark.parametrize(
    "field,value",
    (
        ("allowed_merchants", ["merchant_someone_else"]),
        ("allowed_merchants", None),  # null = every merchant, downstream
        ("rate_limit", 100000),
        ("daily_quota", 100000000),
        ("is_active", True),
    ),
)
def test_agent_cannot_widen_its_own_authorization_fields(client, update_spies, field, value):
    resp = client.put(
        f"/agents/{AGENT_A}",
        json={"agent_name": "Caller Agent", field: value},
        headers=_auth(_agent_token()),
    )

    assert resp.status_code == 403, (
        f"agent set its own {field}={value!r}: {resp.status_code} {resp.text}"
    )
    assert update_spies.executed == [], (
        f"the UPDATE ran before the refusal -- {field} was already written"
    )


def test_agent_keeps_updating_its_own_ordinary_settings(client, update_spies):
    """The positive counterpart. A fix that refused the whole route to agents
    would pass every assertion above and break the route's actual purpose."""
    resp = client.put(
        f"/agents/{AGENT_A}",
        json={
            "agent_name": "Renamed Agent",
            "description": "new description",
            "webhook_url": "https://caller.example/hooks/v2",
            "metadata": {"note": "mine"},
        },
        headers=_auth(_agent_token()),
    )

    assert resp.status_code == 200, resp.text
    assert update_spies.executed, "the update never reached the database"
    written = update_spies.written_values()
    assert written.get("agent_name") == "Renamed Agent"


def test_an_agent_resending_its_current_values_is_still_refused(client, update_spies):
    """Deliberate: the refusal is on the FIELD, not on a diff against the
    stored row. A no-op-looking PUT that carries allowed_merchants is one
    request away from a widening one, and a diff-based rule would have to
    re-derive downstream equality (list order, None-vs-[]) to stay safe."""
    resp = client.put(
        f"/agents/{AGENT_A}",
        json={"allowed_merchants": ["merchant_caller"]},
        headers=_auth(_agent_token()),
    )

    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("role", ("super_admin", "admin", "employee"))
def test_staff_still_set_the_authorization_fields(client, update_spies, role):
    resp = client.put(
        f"/agents/{AGENT_A}",
        json={
            "allowed_merchants": ["merchant_x", "merchant_y"],
            "rate_limit": 500,
            "daily_quota": 50000,
            "is_active": False,
        },
        headers=_auth(_staff_token(role)),
    )

    assert resp.status_code == 200, f"{role} was refused: {resp.text}"
    written = update_spies.written_values()
    assert written.get("rate_limit") == 500
    assert written.get("daily_quota") == 50000


# ---------------------------------------------------------------------------
# utils.auth.can_access_merchant -- the None target
# ---------------------------------------------------------------------------


def test_can_access_merchant_refuses_a_falsy_target():
    """`routes/auth.py` mints merchant tokens with an Optional merchant_id, so
    a `merchant` token can carry merchant_id=None. The comparison was a bare
    `==`, which makes None == None True and hands that caller a target it never
    proved anything about. No caller passes a falsy merchant_id today -- they
    all 400 first -- so this is the guard that keeps the next one from being
    bitten, not a live exploit."""
    from utils.auth import can_access_merchant

    merchant_with_no_id = {"role": "merchant", "merchant_id": None}

    assert can_access_merchant(merchant_with_no_id, None) is False
    assert can_access_merchant(merchant_with_no_id, "") is False
    assert can_access_merchant({"role": "merchant"}, None) is False
    # The agent branch has the same shape: no scoping claims means "all
    # merchants", which must still not mean "the null merchant".
    assert can_access_merchant({"role": "agent"}, None) is False
    assert can_access_merchant({"role": "super_admin"}, None) is False


def test_can_access_merchant_still_answers_for_real_targets():
    """The positive counterpart -- the guard must not swallow real access."""
    from utils.auth import can_access_merchant

    assert can_access_merchant({"role": "merchant", "merchant_id": "m1"}, "m1") is True
    assert can_access_merchant({"role": "merchant", "merchant_id": "m1"}, "m2") is False
    assert can_access_merchant({"role": "admin"}, "m1") is True
    assert can_access_merchant({"role": "agent"}, "m1") is True
