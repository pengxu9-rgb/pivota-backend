"""Owner / other merchant / agent / staff / anonymous, on every route the scope fix touched.

Before this fix these routes checked a ROLE (or nothing) and then read the
merchant_id out of the URL, so any self-registered account read any merchant's
orders, buyer emails, PSP config, analytics, payouts, KYB file and an agent's
bank account:

  GET  /orders/merchant/{merchant_id}
  GET  /merchant/{merchant_id}/{integrations,psps,orders,analytics}
  GET  /merchants/{merchant_id}/agents/{agent_id}/bank-details
  *    /merchants/{merchant_id}/payouts[...]
  GET  /merchant/onboarding/{all,details/{id},status/{id}}
  POST /merchant/onboarding/{kyc/upload, kyc/upload/file/{id}, upload/{id}}

Every token is a REAL signed JWT (never the `test-token` placeholder, whose
pytest bypass returns role=admin and would make each claim vacuous). Database
access is replaced by recording fakes so "owner ok" also proves the handler
read the owner's rows, and "refused" proves nothing was read or written.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

OWNER = "merch_owner"
OTHER = "merch_other"
AGENT = "agent_seller"
UNRELATED_AGENT = "agent_stranger"


def _token(claims: Dict[str, Any]) -> str:
    from utils.auth import create_access_token

    return create_access_token(claims)


def _merchant(mid: str) -> Dict[str, Any]:
    return {"sub": f"u-{mid}", "email": f"{mid}@example.com", "role": "merchant",
            "membership_type": "merchant", "merchant_id": mid}


def _agent(aid: str) -> Dict[str, Any]:
    return {"sub": f"u-{aid}", "email": f"{aid}@example.com", "role": "agent",
            "membership_type": "agent", "agent_id": aid}


def _staff(role: str) -> Dict[str, Any]:
    return {"sub": f"u-{role}", "email": f"{role}@example.com", "role": role,
            "membership_type": "employee", "employee_id": f"emp_{role}"}


CALLERS = {
    "owner": _merchant(OWNER),
    "other_merchant": _merchant(OTHER),
    "agent": _agent(AGENT),
    "admin": _staff("admin"),
    "super_admin": _staff("super_admin"),
    "employee": _staff("employee"),
}


def _headers(caller: Optional[str]) -> Dict[str, str]:
    if caller is None:
        return {}
    return {"Authorization": f"Bearer {_token(CALLERS[caller])}"}


class _FakeDatabase:
    """Records every query; answers the few lookups the handlers need."""

    def __init__(self, relationship: bool = True) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.relationship = relationship

    def _rec(self, query: Any, values: Any) -> str:
        q = " ".join(str(query).split())
        self.calls.append({"query": q, "values": dict(values or {})})
        return q

    async def fetch_one(self, query: Any, values: Any = None, *a: Any, **kw: Any):
        q = self._rec(query, values)
        if "SELECT 1 AS present FROM orders" in q:
            return {"present": 1} if self.relationship else None
        if "FROM agents" in q:
            return {"agent_id": (values or {}).get("agent_id")}
        if "FROM agent_payouts" in q:
            return {"id": 7, "merchant_id": (values or {}).get("merchant_id")}
        if "COUNT(*)" in q:
            return {"total": 0, "count": 0}
        return None

    async def fetch_all(self, query: Any, values: Any = None, *a: Any, **kw: Any):
        self._rec(query, values)
        return []

    async def fetch_val(self, query: Any, values: Any = None, *a: Any, **kw: Any):
        self._rec(query, values)
        return 0

    async def execute(self, query: Any, values: Any = None, *a: Any, **kw: Any):
        self._rec(query, values)
        return None

    def merchant_ids_read(self) -> set:
        return {
            str(c["values"].get("merchant_id"))
            for c in self.calls
            if "merchant_id" in c["values"]
        }


class _FakePayoutRepo:
    def __init__(self, log: List[Dict[str, Any]]) -> None:
        self.log = log

    async def list(self, *, merchant_id, status=None, limit=50, offset=0):
        self.log.append({"op": "list", "merchant_id": merchant_id})
        return []

    async def get_summary_by_merchant(self, merchant_id):
        self.log.append({"op": "summary", "merchant_id": merchant_id})
        return {}

    async def create_bulk(self, merchant_id, rows):
        self.log.append({"op": "create_bulk", "merchant_id": merchant_id, "rows": rows})
        return [1]

    async def upload(self, **kwargs):
        self.log.append({"op": "upload", **kwargs})
        return True


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


@pytest.fixture
def env(monkeypatch):
    """Fake DB + repos on every module these routes live in."""
    import routes.merchant_agent_bank as bank
    import routes.merchant_dashboard_routes as dash
    import routes.merchant_onboarding_routes as onb
    import routes.merchant_payouts as payouts
    import routes.order_routes as orders

    state: Dict[str, Any] = {"db": _FakeDatabase(), "payout_log": [], "onb_log": [], "orders_for": []}

    for mod in (bank, dash, onb, payouts):
        monkeypatch.setattr(mod, "database", state["db"])

    async def _orders_by_merchant(merchant_id, status=None, limit=50, offset=0):
        state["orders_for"].append(merchant_id)
        return []

    monkeypatch.setattr(orders, "get_orders_by_merchant", _orders_by_merchant)

    async def _telemetry(merchant_id):
        return {}

    monkeypatch.setattr(dash, "get_merchant_psp_telemetry", _telemetry)
    monkeypatch.setattr(payouts, "PayoutRepo", lambda: _FakePayoutRepo(state["payout_log"]))

    async def _get_onboarding(merchant_id):
        state["onb_log"].append({"op": "get", "merchant_id": merchant_id})
        return {
            "merchant_id": merchant_id, "business_name": "B", "status": "approved",
            "psp_connected": False, "created_at": None, "contact_email": "x@example.com",
        }

    async def _all_onboardings(status=None, include_deleted=False):
        state["onb_log"].append({"op": "all"})
        return []

    async def _upload_docs(merchant_id, documents):
        state["onb_log"].append({"op": "upload_docs", "merchant_id": merchant_id})
        return True

    async def _add_doc(merchant_id, meta):
        state["onb_log"].append({"op": "add_doc", "merchant_id": merchant_id})
        return True

    class _Readiness:
        def model_dump(self):
            return {}

    async def _readiness(merchant_id):
        return _Readiness()

    monkeypatch.setattr(onb, "get_merchant_onboarding", _get_onboarding)
    monkeypatch.setattr(onb, "get_all_merchant_onboardings", _all_onboardings)
    monkeypatch.setattr(onb, "upload_kyc_documents", _upload_docs)
    monkeypatch.setattr(onb, "add_kyc_document", _add_doc)
    monkeypatch.setattr(onb, "build_readiness_summary", _readiness)
    return state


def _touched(env) -> set:
    out = set(env["db"].merchant_ids_read())
    out |= {e.get("merchant_id") for e in env["payout_log"] if e.get("merchant_id")}
    out |= {e.get("merchant_id") for e in env["onb_log"] if e.get("merchant_id")}
    out |= set(env["orders_for"])
    return out


def _nothing_touched(env) -> bool:
    return not env["db"].calls and not env["payout_log"] and not env["onb_log"] and not env["orders_for"]


# (name, method, path, request kwargs, staff roles that pass besides admin)
ROUTES = [
    ("orders_legacy", "get", f"/orders/merchant/{OWNER}", {}, ()),
    ("dash_integrations", "get", f"/merchant/{OWNER}/integrations", {}, ()),
    ("dash_psps", "get", f"/merchant/{OWNER}/psps", {}, ()),
    ("dash_orders", "get", f"/merchant/{OWNER}/orders", {}, ()),
    ("dash_analytics", "get", f"/merchant/{OWNER}/analytics", {}, ()),
    ("bank_details", "get", f"/merchants/{OWNER}/agents/{AGENT}/bank-details", {}, ()),
    ("payouts_list", "get", f"/merchants/{OWNER}/payouts", {}, ()),
    ("payouts_bulk", "post", f"/merchants/{OWNER}/payouts/bulk",
     {"json": {"items": [{"agent_id": AGENT, "amount": 1}]}}, ()),
    ("payouts_get", "get", f"/merchants/{OWNER}/payouts/7", {}, ()),
    ("payouts_upload", "post", f"/merchants/{OWNER}/payouts/7/upload", {"json": {"reference": "r"}}, ()),
    ("payouts_csv", "get", f"/merchants/{OWNER}/payouts/export/csv", {}, ()),
    ("onb_details", "get", f"/merchant/onboarding/details/{OWNER}", {}, ("employee",)),
    ("onb_status", "get", f"/merchant/onboarding/status/{OWNER}", {}, ("employee",)),
    ("onb_kyc_json", "post", "/merchant/onboarding/kyc/upload",
     {"json": {"merchant_id": OWNER, "documents": {"d": {"filename": "a.pdf"}}}}, ("employee",)),
    ("onb_kyc_file", "post", f"/merchant/onboarding/kyc/upload/file/{OWNER}",
     {"data": {"document_type": "id"}, "files": {"file": ("a.pdf", b"%PDF", "application/pdf")}}, ("employee",)),
    ("onb_upload", "post", f"/merchant/onboarding/upload/{OWNER}",
     {"data": {"document_type": "id"}, "files": [("files", ("a.pdf", b"%PDF", "application/pdf"))]}, ("employee",)),
]
ROUTE_IDS = [r[0] for r in ROUTES]


def _send(client, method, path, kwargs, caller):
    return getattr(client, method)(path, headers=_headers(caller), **kwargs)


@pytest.mark.parametrize("name,method,path,kwargs,extra_staff", ROUTES, ids=ROUTE_IDS)
def test_owner_is_served_their_own_rows(client, env, name, method, path, kwargs, extra_staff):
    resp = _send(client, method, path, kwargs, "owner")
    assert resp.status_code == 200, f"{name}: {resp.status_code} {resp.text[:300]}"
    assert _touched(env) == {OWNER}, f"{name} read {_touched(env)}"


@pytest.mark.parametrize("staff", ["admin", "super_admin"])
@pytest.mark.parametrize("name,method,path,kwargs,extra_staff", ROUTES, ids=ROUTE_IDS)
def test_admin_keeps_cross_merchant_access(client, env, name, method, path, kwargs, extra_staff, staff):
    # Admin skips the merchant-agent relationship check too.
    env["db"].relationship = False
    resp = _send(client, method, path, kwargs, staff)
    assert resp.status_code == 200, f"{name}: {resp.status_code} {resp.text[:300]}"
    # bank-details reads only agent-keyed tables once the relationship check is skipped.
    assert _touched(env) <= {OWNER}


@pytest.mark.parametrize("name,method,path,kwargs,extra_staff", ROUTES, ids=ROUTE_IDS)
def test_employee_access_matches_what_the_route_had(client, env, name, method, path, kwargs, extra_staff):
    """Onboarding routes keep `employee` staff (the employee portal reads them);
    the merchant dashboard, order and payout routes never admitted `employee`."""
    resp = _send(client, method, path, kwargs, "employee")
    if "employee" in extra_staff:
        assert resp.status_code == 200, f"{name}: {resp.status_code} {resp.text[:300]}"
    else:
        assert resp.status_code == 403, f"{name}: {resp.status_code}"
        assert _nothing_touched(env)


@pytest.mark.parametrize("caller", ["other_merchant", "agent"])
@pytest.mark.parametrize("name,method,path,kwargs,extra_staff", ROUTES, ids=ROUTE_IDS)
def test_other_tenants_are_refused_before_any_read(client, env, name, method, path, kwargs, extra_staff, caller):
    resp = _send(client, method, path, kwargs, caller)
    assert resp.status_code == 403, f"{name} [{caller}]: {resp.status_code} {resp.text[:300]}"
    assert _nothing_touched(env), f"{name} [{caller}] touched data: {env['db'].calls} {env['payout_log']} {env['onb_log']}"


@pytest.mark.parametrize("name,method,path,kwargs,extra_staff", ROUTES, ids=ROUTE_IDS)
def test_no_token_is_401(client, env, name, method, path, kwargs, extra_staff):
    resp = _send(client, method, path, kwargs, None)
    # HTTPBearer refuses a missing header itself: 403 "Not authenticated" on the
    # pinned FastAPI (newer releases answer 401). Either way it is the auth
    # dependency refusing, not the scope guard, and nothing is read or written.
    assert resp.status_code in (401, 403), f"{name}: {resp.status_code}"
    assert resp.json().get("detail") == "Not authenticated", resp.text
    assert _nothing_touched(env)


@pytest.mark.parametrize("name,method,path,kwargs,extra_staff", ROUTES, ids=ROUTE_IDS)
def test_a_bad_token_is_401(client, env, name, method, path, kwargs, extra_staff):
    resp = getattr(client, method)(path, headers={"Authorization": "Bearer not-a-jwt"}, **kwargs)
    assert resp.status_code == 401, f"{name}: {resp.status_code}"
    assert _nothing_touched(env)


# ---------------------------------------------------------------------------
# Route-specific edges
# ---------------------------------------------------------------------------


def test_bank_details_needs_a_real_order_between_merchant_and_agent(client, env):
    """Owning the merchant is not enough to read an arbitrary agent's IBAN."""
    env["db"].relationship = False
    resp = _send(client, "get", f"/merchants/{OWNER}/agents/{UNRELATED_AGENT}/bank-details", {}, "owner")
    assert resp.status_code == 403, resp.text
    queried = [c["query"] for c in env["db"].calls]
    assert queried and all("SELECT 1 AS present FROM orders" in q for q in queried), queried
    assert not any("agent_beneficiaries" in q for q in queried)


def test_bank_details_relationship_lookup_is_keyed_on_both_ids(client, env):
    resp = _send(client, "get", f"/merchants/{OWNER}/agents/{AGENT}/bank-details", {}, "owner")
    assert resp.status_code == 200
    rel = [c for c in env["db"].calls if "SELECT 1 AS present FROM orders" in c["query"]]
    assert rel and rel[0]["values"] == {"merchant_id": OWNER, "agent_id": AGENT}


def test_bulk_payouts_refuse_an_agent_with_no_orders(client, env):
    env["db"].relationship = False
    resp = _send(client, "post", f"/merchants/{OWNER}/payouts/bulk",
                 {"json": {"items": [{"agent_id": UNRELATED_AGENT, "amount": 1}]}}, "owner")
    assert resp.status_code == 403, resp.text
    assert not [e for e in env["payout_log"] if e["op"] == "create_bulk"]


def test_payout_upload_is_confined_to_the_path_merchant(client, env):
    """payout_id and merchant_id arrive separately; the UPDATE must carry both."""
    resp = _send(client, "post", f"/merchants/{OWNER}/payouts/7/upload", {"json": {"reference": "r"}}, "owner")
    assert resp.status_code == 200
    uploads = [e for e in env["payout_log"] if e["op"] == "upload"]
    assert uploads and uploads[0]["merchant_id"] == OWNER and uploads[0]["payout_id"] == 7


async def test_payout_repo_upload_scopes_the_update_by_merchant(monkeypatch):
    import db.payout_repo as repo_mod

    db = _FakeDatabase()
    monkeypatch.setattr(repo_mod, "database", db)
    await repo_mod.PayoutRepo().upload(payout_id=7, reference="r", merchant_id=OWNER)
    q = db.calls[-1]
    assert "merchant_id = :mid" in q["query"] and q["values"]["mid"] == OWNER


@pytest.mark.parametrize("caller,expected", [
    ("admin", 200), ("super_admin", 200), ("employee", 200),
    ("owner", 403), ("other_merchant", 403), ("agent", 403), (None, None),
])
def test_onboarding_roster_is_staff_only(client, env, caller, expected):
    resp = _send(client, "get", "/merchant/onboarding/all", {}, caller)
    if expected is None:
        assert resp.status_code in (401, 403)
        assert resp.json().get("detail") == "Not authenticated"
    else:
        assert resp.status_code == expected, resp.text[:300]
    if expected != 200:
        assert not env["onb_log"]


def test_kyc_json_upload_scopes_on_the_body_merchant_id(client, env):
    resp = _send(client, "post", "/merchant/onboarding/kyc/upload",
                 {"json": {"merchant_id": OTHER, "documents": {}}}, "owner")
    assert resp.status_code == 403
    assert not env["onb_log"]


def test_dashboard_stats_still_serves_the_merchant_its_own_analytics(client, env):
    """/merchant/dashboard/stats calls the analytics handler with the token's id."""
    resp = _send(client, "get", "/merchant/dashboard/stats", {}, "owner")
    assert resp.status_code == 200, resp.text[:300]


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


def _raises_403(fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except HTTPException as exc:
        return exc.status_code == 403
    return False


def test_merchant_guard_semantics():
    from utils.auth import EMPLOYEE_STAFF_ROLES
    from utils.tenant_scope import assert_merchant_scope as g

    g(_merchant(OWNER), OWNER)
    g(_staff("admin"), OWNER)
    g({"role": "admin"}, OWNER)  # legacy staff token without membership_type
    g(_staff("employee"), OWNER, staff_roles=EMPLOYEE_STAFF_ROLES)
    assert _raises_403(g, _staff("employee"), OWNER)
    assert _raises_403(g, _staff("outsourced"), OWNER, staff_roles=EMPLOYEE_STAFF_ROLES)
    assert _raises_403(g, _merchant(OTHER), OWNER)
    assert _raises_403(g, _agent(AGENT), OWNER)
    assert _raises_403(g, {**_agent(AGENT), "merchant_id": OWNER}, OWNER)  # role must be merchant
    assert _raises_403(g, {"role": "admin", "membership_type": "merchant", "merchant_id": OTHER}, OWNER)
    assert _raises_403(g, {"role": "merchant", "merchant_id": None}, None)
    assert _raises_403(g, {"role": "merchant", "merchant_id": ""}, "")
    assert _raises_403(g, {"role": "merchant", "merchant_id": [OWNER]}, OWNER)
    assert _raises_403(g, None, OWNER)
    assert _raises_403(g, _staff("admin"), "")


def test_agent_guard_semantics():
    from utils.tenant_scope import assert_agent_scope as g

    g(_agent(AGENT), AGENT)
    g(_staff("super_admin"), AGENT)
    g({"role": "agent", "email": "Owner@Example.com"}, AGENT, owner_email="owner@example.com")
    assert _raises_403(g, _agent(UNRELATED_AGENT), AGENT)
    g(_agent(AGENT), AGENT, staff_roles=())
    assert _raises_403(g, _staff("admin"), AGENT, staff_roles=())
    assert _raises_403(g, {"role": "agent", "email": AGENT}, AGENT)  # email == id is not ownership
    assert _raises_403(g, {"role": "agent", "user_id": AGENT}, AGENT)  # user_id is a users-table id
    assert _raises_403(g, {"role": "merchant", "email": "o@example.com"}, AGENT, owner_email="o@example.com")
    assert _raises_403(g, _merchant(OWNER), AGENT)
    # A token that names a different agent cannot fall back to its email.
    assert _raises_403(g, {**_agent(UNRELATED_AGENT), "email": "o@example.com"}, AGENT, owner_email="o@example.com")


async def test_agent_owner_lookup_only_runs_for_an_agent_token():
    from utils.tenant_scope import assert_agent_scope_or_owner as g

    db = _FakeDatabase()

    async def owner_row(query, values=None, *a, **kw):
        db.calls.append({"query": str(query), "values": values})
        return {"owner_email": "seller@example.com"}

    db.fetch_one = owner_row  # type: ignore[assignment]
    await g(db, {"role": "agent", "email": "seller@example.com"}, AGENT)
    assert len(db.calls) == 1
    await g(db, _agent(AGENT), AGENT)  # claim match: no lookup
    assert len(db.calls) == 1
    with pytest.raises(HTTPException):
        await g(db, _merchant(OWNER), AGENT)  # not an agent: no lookup
    assert len(db.calls) == 1
    with pytest.raises(HTTPException):
        await g(db, {"role": "agent", "email": "stranger@example.com"}, AGENT)
