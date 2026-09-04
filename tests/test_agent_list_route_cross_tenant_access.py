"""The sibling that made the GET /agents/{agent_id} fix a confidentiality no-op.

THE DEFECT, live on prod through 2e6671623. PR #2041 paired the role gate on
`GET /agents/{agent_id}` with an ownership check, so an agent can no longer
read another agent's record. `GET /agents/` -- the LIST route, four handlers
down in the same file -- kept handing that record to anyone at all:

    admin_user: dict = Depends(get_current_user)  # Allow authenticated users

No role gate, no ownership scoping, `agents.select()` over every column with
only `api_key` / `api_key_hash` popped. Every authenticated principal -- another
agent, but also a `merchant` or a `buyer`, roles that
AGENT_OR_EMPLOYEE_STAFF_ROLES refuses on the detail route -- could call
`GET /agents/?limit=100` and read every agent's `owner_email`, `webhook_url`,
`allowed_merchants`, `metadata`, quotas and GMV. The `search` parameter runs an
`ilike` against `owner_email`, so it was also an email-enumeration primitive: a
one-request oracle for "which of these addresses runs an agent here".

Fixing one route and leaving its list sibling open protects nothing, which is
why these tests exist as a pair with
tests/test_cross_tenant_merchant_and_agent_route_access.py and follow its
shape:

  * REAL signed JWTs, never the `test-token` placeholder -- that placeholder's
    pytest bypass in utils.auth returns role=admin, which would make every
    refusal here vacuous.
  * A DB spy that is a REAL sqlite database holding agent_A's and agent_B's
    rows, built from db.agents.agents' own column list. The scoping this route
    needs lives in a WHERE clause, so a spy that returned a canned list would
    pass whether or not the clause was ever added; executing the handler's
    actual query is what makes "agent_A saw only agent_A" a fact about the
    query rather than about the double. The recorded queries also prove the
    handler was REACHED on the 200 paths -- an empty list is otherwise
    indistinguishable from a refusal.
  * Both directions: the stranger is refused, and staff plus the owner keep
    exactly what the endpoint is for.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest
import sqlalchemy
from fastapi.testclient import TestClient

AGENT_A = "agent_aaaaaaaa"
AGENT_B = "agent_bbbbbbbb"

AGENT_A_EMAIL = "owner_a@example.com"
VICTIM_EMAIL = "victim@othercompany.example"
VICTIM_WEBHOOK = "https://victim.example/hooks/pivota"
VICTIM_MERCHANT = "merchant_secret_1"

# Every field below survived the api_key/api_key_hash pop and went out in the
# list response. Asserted by substring against the raw body so that a fix which
# merely renames or nests them cannot pass.
_VICTIM_SECRETS = (VICTIM_EMAIL, VICTIM_WEBHOOK, VICTIM_MERCHANT, "victim-only")


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _token(claims: Dict[str, Any]) -> str:
    from utils.auth import create_access_token

    return create_access_token(claims)


def _agent_token(agent_id: str, email: str) -> str:
    return _token(
        {"sub": f"u-{agent_id}", "email": email, "role": "agent", "agent_id": agent_id}
    )


def _staff_token(role: str) -> str:
    return _token({"sub": f"u-{role}", "email": f"{role}@example.com", "role": role})


def _auth(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# The spy: a real sqlite `agents` table, so the handler's WHERE clause decides
# what comes back.
# ---------------------------------------------------------------------------


def _sqlite_agents_table(metadata: sqlalchemy.MetaData) -> sqlalchemy.Table:
    """Mirror db.agents.agents column-for-column, minus the server defaults.

    Derived from the real table rather than hand-written: the handler selects
    every column, so a fixture that guessed the column list would either fail
    to build or quietly stop covering a column that was added later. The
    server_default (`func.now()`) is dropped because sqlite will not accept
    `DEFAULT now()` in DDL; the rows here set those columns explicitly.
    """
    from db.agents import agents as real_agents

    return sqlalchemy.Table(
        "agents",
        metadata,
        *[
            sqlalchemy.Column(
                col.name,
                col.type,
                primary_key=col.primary_key,
                nullable=col.nullable,
            )
            for col in real_agents.columns
        ],
    )


def _rows() -> List[Dict[str, Any]]:
    now = datetime(2026, 9, 1, 12, 0, 0)
    common = {
        "api_key": "pk_live_should_never_be_returned",
        "api_key_hash": "hash_should_never_be_returned",
        "is_active": True,
        "agent_type": "chatbot",
        "rate_limit": 100,
        "daily_quota": 10000,
        "total_requests": 42,
        "total_orders": 7,
        "total_gmv": 1234,
        "success_rate": 99,
        "created_at": now,
        "updated_at": now,
        "last_used_at": now,
    }
    return [
        {
            **common,
            "id": 1,
            "agent_id": AGENT_A,
            "agent_name": "Caller Agent",
            "description": "the agent doing the asking",
            "owner_email": AGENT_A_EMAIL,
            "webhook_url": "https://caller.example/hooks",
            "allowed_merchants": ["merchant_caller"],
            "metadata": {"contract": "caller-only"},
        },
        {
            **common,
            "id": 2,
            "agent_id": AGENT_B,
            "agent_name": "Victim Agent",
            "description": "another tenant entirely",
            "owner_email": VICTIM_EMAIL,
            "webhook_url": VICTIM_WEBHOOK,
            "allowed_merchants": [VICTIM_MERCHANT, "merchant_secret_2"],
            "metadata": {"contract": "victim-only"},
        },
    ]


class _AgentsDbSpy:
    """Stands in for routes.agent_management.database, backed by real sqlite."""

    def __init__(self) -> None:
        # StaticPool + check_same_thread: TestClient runs the app on its own
        # thread, and the default in-memory pool would hand that thread a
        # second, empty database.
        self.engine = sqlalchemy.create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=sqlalchemy.pool.StaticPool,
        )
        metadata = sqlalchemy.MetaData()
        self.table = _sqlite_agents_table(metadata)
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(self.table.insert(), _rows())
        self.queries: List[Any] = []

    def _run(self, query: Any, values: Optional[Dict[str, Any]] = None):
        self.queries.append(query)
        if isinstance(query, str):
            query = sqlalchemy.text(query)
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(query, values or {}).mappings()]

    async def fetch_all(self, query: Any, values: Optional[Dict[str, Any]] = None, *a, **kw):
        return self._run(query, values)

    async def fetch_one(self, query: Any, values: Optional[Dict[str, Any]] = None, *a, **kw):
        rows = self._run(query, values)
        return rows[0] if rows else None

    async def execute(self, query: Any, values: Optional[Dict[str, Any]] = None, *a, **kw):
        self._run(query, values)
        return None


@pytest.fixture
def agents_db(monkeypatch) -> _AgentsDbSpy:
    from routes import agent_management as mod

    spy = _AgentsDbSpy()
    monkeypatch.setattr(mod, "database", spy)
    return spy


def _agent_ids(payload: Dict[str, Any]) -> List[str]:
    return [a["agent_id"] for a in payload["agents"]]


# ---------------------------------------------------------------------------
# The hole itself.
# ---------------------------------------------------------------------------


def test_agent_cannot_list_another_agents_record(client, agents_db):
    """agent_A calls the list route and must come back with agent_A only.

    This is the whole defect: the fields asserted absent here are exactly what
    `agents.select()` returned once api_key/api_key_hash were popped.
    """
    resp = client.get(
        "/agents/", params={"limit": 100}, headers=_auth(_agent_token(AGENT_A, AGENT_A_EMAIL))
    )

    assert resp.status_code == 200, resp.text
    assert agents_db.queries, "handler never reached the database -- test is vacuous"

    body = resp.text
    for leaked in _VICTIM_SECRETS:
        assert leaked not in body, f"GET /agents/ leaked {leaked} to another agent"

    payload = resp.json()
    assert _agent_ids(payload) == [AGENT_A]
    assert payload["total"] == 1, (
        f"`total` published the size of the whole agent roster: {payload['total']}"
    )


def test_agent_still_sees_its_own_record_in_the_list(client, agents_db):
    """The positive counterpart: scoping must not empty the route for its own
    owner. A fix that returned [] for every agent would pass the test above."""
    resp = client.get("/agents/", headers=_auth(_agent_token(AGENT_A, AGENT_A_EMAIL)))

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert _agent_ids(payload) == [AGENT_A]
    assert payload["agents"][0]["owner_email"] == AGENT_A_EMAIL


def test_api_key_columns_stay_out_of_the_list(client, agents_db):
    """Redaction is not ownership, but it still has to happen."""
    resp = client.get("/agents/", headers=_auth(_agent_token(AGENT_A, AGENT_A_EMAIL)))

    assert resp.status_code == 200, resp.text
    assert "pk_live_should_never_be_returned" not in resp.text
    assert "hash_should_never_be_returned" not in resp.text


def test_search_cannot_enumerate_other_owners_emails(client, agents_db):
    """`search` runs an ilike over owner_email. Unscoped, it answered "does
    this address own an agent here" for any address a caller cared to try."""
    resp = client.get(
        "/agents/",
        params={"search": "othercompany"},
        headers=_auth(_agent_token(AGENT_A, AGENT_A_EMAIL)),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["agents"] == [], "search enumerated another tenant's owner_email"
    for leaked in _VICTIM_SECRETS:
        assert leaked not in resp.text


# ---------------------------------------------------------------------------
# Identity spellings -- the same three the detail route had to honour.
# ---------------------------------------------------------------------------


def test_agent_identified_only_by_user_id_sees_its_own_record(client, agents_db):
    """Some agent tokens carry the identity as `user_id`, not `agent_id`; the
    detail route and update_agent both honour that. Kills a scoping fix built
    on can_access_agent alone, which reads `agent_id` only and would hand those
    agents an empty list."""
    token = _token(
        {"sub": AGENT_A, "email": AGENT_A_EMAIL, "role": "agent", "user_id": AGENT_A}
    )

    resp = client.get("/agents/", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    assert _agent_ids(resp.json()) == [AGENT_A]


def test_agent_identified_only_by_email_sees_its_own_record(client, agents_db):
    """The email fallback, matched against the record's own owner_email -- the
    relation _is_own_agent_record settled on for the detail route."""
    token = _token({"sub": "u-legacy", "email": AGENT_A_EMAIL, "role": "agent"})

    resp = client.get("/agents/", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    assert _agent_ids(resp.json()) == [AGENT_A]


def test_email_identity_does_not_open_another_agents_record(client, agents_db):
    """Widening identity must not become a way in."""
    token = _token({"sub": "u-legacy", "email": AGENT_A_EMAIL, "role": "agent"})

    resp = client.get("/agents/", params={"limit": 100}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    assert AGENT_B not in _agent_ids(resp.json())
    for leaked in _VICTIM_SECRETS:
        assert leaked not in resp.text


def test_a_blank_email_claim_matches_no_record(client, agents_db, monkeypatch):
    """The empty-string trap: a token with no usable identity must select
    nothing, not everything. Rows with a NULL owner_email must not be swept in
    by an `owner_email = ''` comparison either."""
    with agents_db.engine.begin() as conn:
        conn.execute(
            agents_db.table.update()
            .where(agents_db.table.c.agent_id == AGENT_B)
            .values(owner_email=None)
        )
    token = _token({"sub": "u-x", "email": "", "role": "agent"})

    resp = client.get("/agents/", params={"limit": 100}, headers=_auth(token))

    # A token with no identity at all gets nothing; a refusal is equally fine.
    if resp.status_code == 200:
        assert resp.json()["agents"] == [], resp.text
    else:
        assert resp.status_code == 403, resp.text
    assert VICTIM_WEBHOOK not in resp.text


# ---------------------------------------------------------------------------
# Staff keep the route. Everyone else loses it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ("super_admin", "admin", "employee", "outsourced"))
def test_staff_keep_the_full_agent_roster(client, agents_db, role):
    """What the endpoint is for. `outsourced` is in EMPLOYEE_ROLES and
    can_access_agent already grants it every agent, so narrowing the gate must
    not lock it out of a list it reads today."""
    resp = client.get("/agents/", params={"limit": 100}, headers=_auth(_staff_token(role)))

    assert resp.status_code == 200, f"{role} was refused: {resp.text}"
    payload = resp.json()
    assert sorted(_agent_ids(payload)) == sorted([AGENT_A, AGENT_B])
    assert payload["total"] == 2


@pytest.mark.parametrize("role", ("merchant", "buyer"))
def test_non_agent_non_staff_roles_are_refused(client, agents_db, role):
    """AGENT_OR_EMPLOYEE_STAFF_ROLES already refuses these on
    GET /agents/{agent_id}. They reached the same fields through the list."""
    claims: Dict[str, Any] = {"sub": f"u-{role}", "email": f"{role}@example.com", "role": role}
    if role == "merchant":
        claims["merchant_id"] = "merchant_caller"

    resp = client.get("/agents/", params={"limit": 100}, headers=_auth(_token(claims)))

    assert resp.status_code == 403, f"{role} still reached the agent roster: {resp.text}"
    for leaked in _VICTIM_SECRETS:
        assert leaked not in resp.text


def test_the_refusal_happens_before_the_query(client, agents_db):
    """A 403 rendered after the roster was already fetched would pass a
    status-only assertion. The spy records every query the handler runs."""
    resp = client.get(
        "/agents/",
        headers=_auth(_token({"sub": "u-b", "email": "b@example.com", "role": "buyer"})),
    )

    assert resp.status_code == 403, resp.text
    assert agents_db.queries == [], (
        f"handler queried the agents table before refusing: {agents_db.queries}"
    )
