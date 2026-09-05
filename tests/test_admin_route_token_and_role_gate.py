"""`/admin/*` behind `routes.auth_routes.require_admin`: TWO stacked defects.

OBSERVED on prod 2026-09-05 against api.pivota.cc, with a valid `super_admin`
employee-portal JWT (the same token answered 200 on
`/ops/store-audit/checkout-tier-coverage`, so the token itself was good):

    GET /admin/cleanup/list-merchants -> 401 {"code":"UNAUTHORIZED",
                                              "message":"Invalid token"}

401, not 403 -- the request never reached the role check. Two independent
defects stack on this surface, and fixing either one alone leaves it closed:

  1. TOKEN. `routes.auth_routes.verify_jwt_token` called `jwt.decode()` with
     no `audience=` and no `verify_aud` option. PyJWT does NOT ignore an
     unchecked `aud`: `_validate_aud` raises `InvalidAudienceError("Invalid
     audience")` whenever the token CARRIES `aud` and the caller named none.
     Every token `/api/auth/login` issues carries one
     (`db.auth_identity.PORTAL_TO_AUDIENCE`: employee-portal /
     merchant-portal / agent-portal), so this second validator rejected, as
     malformed, every token the shared validator
     (`utils.auth.get_current_user` -> `decode_token`, which passes
     `options={"verify_aud": False}`) accepts. Only the legacy `/auth/signin`
     tokens -- which carry no `aud` -- got through.

  2. ROLE. `routes.auth_routes.require_admin` was `role != "admin"`, so
     `super_admin` -- the most privileged role, and one the employee portal
     issues -- was refused 403. Same defect #2031 fixed elsewhere, in a
     spelling the list-literal ratchet cannot match.

The two hide each other: with only the role fix the surface still answers 401;
with only the token fix it answers 403. Both halves are pinned separately
below (`_portal_token` carries `aud` and isolates defect 1; `_legacy_token`
does not and isolates defect 2), so a partial regression names itself instead
of reproducing one symptom for two causes.

Tokens here are REAL signed JWTs. The `test-token` placeholder is never used:
its pytest-only bypass in `utils.auth.get_current_user` returns role=admin, so
every refusal asserted here would be vacuous.
"""
from __future__ import annotations

import ast
import datetime
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

# require_admin surface carrying BOTH defects: it is the route observed on
# prod, and its dependency chain is routes.auth_routes (the second validator).
_CLEANUP_ROUTE = "/admin/cleanup/list-merchants"

# Read-only surface in the employee-management family. It depends on the
# SHARED validator already, so it carries defect 2 only -- it is here to pin
# the deliberate scope decision: employee management admits ADMIN_ROLES and is
# NOT widened to EMPLOYEE_STAFF_ROLES, so a plain `employee` stays refused.
_AUDIT_LOG_ROUTE = "/security/audit-logs"

# ADMIN_ROLES: who these admin-only surfaces must admit.
_ADMITTED = ("super_admin", "admin")

# Must still be refused. `employee` is the load-bearing one: it is staff, it
# reaches the employee portal, and these surfaces (migrations, seeding, store
# deletion, employee management) deliberately do not admit it. The rest prove
# the gate still gates.
_REFUSED = ("employee", "outsourced", "merchant", "agent", "buyer")


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _sign(claims: Dict[str, Any]) -> str:
    """Sign with the process's real secret, via the app's own encoder."""
    from utils.auth import create_access_token

    return create_access_token(claims)


def _portal_token(role: str, membership_type: str = "employee") -> str:
    """A token shaped exactly like `/api/auth/login` issues.

    The `aud` claim is the whole point: routes.auth.\\_claims_for_membership
    stamps PORTAL_TO_AUDIENCE[membership_type] onto every login token, and it
    is what the second validator choked on.
    """
    from db.auth_identity import PORTAL_TO_AUDIENCE

    identity_id = f"identity:{role}"
    claims = {
        "sub": identity_id,
        "identity_id": identity_id,
        "user_id": identity_id,
        "email": f"{role}@example.com",
        "role": role,
        "membership_id": f"legacy:{membership_type}:{role}",
        "membership_type": membership_type,
        "aud": PORTAL_TO_AUDIENCE[membership_type],
        "scope": membership_type,
    }
    if membership_type == "employee":
        claims["employee_id"] = f"emp-{role}"
    elif membership_type == "merchant":
        claims["merchant_id"] = f"merch-{role}"
    elif membership_type == "agent":
        claims["agent_id"] = f"agent-{role}"
    return _sign(claims)


def _legacy_token(role: str) -> str:
    """A token shaped like legacy `/auth/signin` issues: NO `aud` claim.

    This is the token the old validator accepted, so a request carrying it
    reaches the role check. It isolates defect 1 from defect 2.
    """
    return _sign(
        {
            "sub": f"u-{role}",
            "user_id": f"u-{role}",
            "email": f"{role}@example.com",
            "role": role,
        }
    )


class _DatabaseSpy:
    """Records every DB call so a test can prove the handler body ran.

    An empty fetch_all drives the real handler down its ordinary empty-result
    path, so the ADMIT tests exercise the shipped code, not a stub of it.
    """

    def __init__(self) -> None:
        self.calls: List[str] = []

    async def fetch_all(self, *a: Any, **kw: Any) -> List[Dict[str, Any]]:
        self.calls.append("fetch_all")
        return []

    async def fetch_one(self, *a: Any, **kw: Any) -> None:
        self.calls.append("fetch_one")
        return None

    async def execute(self, *a: Any, **kw: Any) -> None:
        self.calls.append("execute")
        return None


@pytest.fixture
def cleanup_db_spy(monkeypatch) -> _DatabaseSpy:
    from routes import admin_cleanup as mod

    spy = _DatabaseSpy()
    monkeypatch.setattr(mod, "database", spy)
    return spy


@pytest.fixture
def audit_db_spy(monkeypatch) -> _DatabaseSpy:
    from routes import employees_security as mod

    spy = _DatabaseSpy()
    monkeypatch.setattr(mod, "database", spy)
    return spy


# ---------------------------------------------------------------------------
# Defect 2 in isolation: the TOKEN. Role is `admin`, which the shipped role
# check already admitted -- so any refusal here is the validator's alone.
# ---------------------------------------------------------------------------


def test_a_portal_token_is_not_rejected_as_invalid(client, cleanup_db_spy):
    """Kills the shipped `jwt.decode(...)` with no aud handling.

    `admin` passes the role check on both sides of this fix, so a 401 here can
    only be the second validator refusing a token the rest of the app accepts.
    """
    resp = client.get(
        _CLEANUP_ROUTE,
        headers={"Authorization": f"Bearer {_portal_token('admin')}"},
    )

    assert resp.status_code != 401, (
        "a valid employee-portal token was rejected as invalid: " + resp.text
    )
    assert resp.status_code == 200, resp.text
    assert cleanup_db_spy.calls, "admin never reached the handler body"


async def test_the_two_validators_agree_on_one_token():
    """The root cause, pinned directly: one token, two verdicts.

    Kills any reintroduction of a second decoder in routes.auth_routes,
    whatever the route tests happen to cover. The shared validator
    (utils.auth) and the auth_routes dependency must accept exactly the same
    token -- disagreeing about what "valid" means is the defect itself, and a
    route test only notices once someone wires a route to the wrong one.
    """
    import inspect

    from fastapi.security import HTTPAuthorizationCredentials

    from routes.auth_routes import verify_jwt_token
    from utils.auth import decode_token

    token = _portal_token("super_admin")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    shared = decode_token(token)
    assert shared["role"] == "super_admin"

    # Tolerates verify_jwt_token being sync again; the claim is the verdict,
    # not the calling convention.
    result = verify_jwt_token(creds)
    if inspect.isawaitable(result):
        result = await result

    assert result["role"] == "super_admin"
    assert result.get("user_id"), "verify_jwt_token must still expose user_id"


# ---------------------------------------------------------------------------
# Defect 1 in isolation: the ROLE. A legacy no-aud token clears the validator
# on both sides of this fix, so any refusal here is the role check's alone.
# ---------------------------------------------------------------------------


def test_super_admin_clears_the_role_check(client, cleanup_db_spy):
    """Kills `role != "admin"` in routes.auth_routes.require_admin.

    The token carries no `aud`, so it reached the role check even before the
    validator fix -- and was answered 403 "Admin access required".
    """
    resp = client.get(
        _CLEANUP_ROUTE,
        headers={"Authorization": f"Bearer {_legacy_token('super_admin')}"},
    )

    assert resp.status_code != 403, (
        "super_admin was refused by the role check: " + resp.text
    )
    assert resp.status_code == 200, resp.text
    assert cleanup_db_spy.calls, "super_admin never reached the handler body"


# ---------------------------------------------------------------------------
# Both halves together: the exact prod request.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", _ADMITTED)
def test_admin_roles_reach_the_handler_with_a_portal_token(
    client, cleanup_db_spy, role
):
    """The prod symptom, pinned end to end.

    Fixing only one defect leaves this red: role-only still answers 401,
    token-only still answers 403 for super_admin.
    """
    resp = client.get(
        _CLEANUP_ROUTE,
        headers={"Authorization": f"Bearer {_portal_token(role)}"},
    )

    assert resp.status_code == 200, f"{role} was refused: {resp.text}"
    # A 200 synthesized before the query would satisfy the status assertion
    # while the gate still rejected; the handler body is the claim.
    assert cleanup_db_spy.calls, f"{role} never reached the handler body"


# ---------------------------------------------------------------------------
# The positive counterpart. "Accept the token" must not mean "accept
# anything", and "admit super_admin" must not mean "admit everyone".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", _REFUSED)
def test_non_admin_roles_are_refused_before_the_handler(
    client, cleanup_db_spy, role
):
    """Kills widening require_admin to EMPLOYEE_STAFF_ROLES or EMPLOYEE_ROLES.

    These surfaces delete merchants, run migrations and seed data. `employee`
    and `outsourced` reach the employee portal and must still be refused here.
    A refused request must not touch the database.
    """
    membership = {"merchant": "merchant", "agent": "agent"}.get(role, "employee")
    resp = client.get(
        _CLEANUP_ROUTE,
        headers={
            "Authorization": f"Bearer {_portal_token(role, membership)}"
        },
    )

    assert resp.status_code == 403, f"{role} was admitted: {resp.status_code}"
    assert not cleanup_db_spy.calls, f"refused {role} reached the handler body"


def test_a_refused_role_is_refused_by_ROLE_not_by_token(client, cleanup_db_spy):
    """A merchant-portal token is a VALID token that this route refuses.

    Pins the deliberate consequence of dropping audience enforcement here: the
    shared validator does not bind a token to a portal, so a merchant token is
    turned away by the role check (403), not mistaken for a forgery (401).
    Making that explicit keeps a future "just verify aud" change from silently
    re-splitting the two validators.
    """
    resp = client.get(
        _CLEANUP_ROUTE,
        headers={
            "Authorization": f"Bearer {_portal_token('merchant', 'merchant')}"
        },
    )

    assert resp.status_code == 403, resp.text
    assert not cleanup_db_spy.calls


def test_unauthenticated_is_still_rejected(client, cleanup_db_spy):
    resp = client.get(_CLEANUP_ROUTE)

    assert resp.status_code in (401, 403)
    assert not cleanup_db_spy.calls


def test_a_forged_signature_is_still_rejected(client, cleanup_db_spy):
    """The load-bearing counterpart to the validator fix.

    Kills a "fix" that stops verifying instead of stopping over-verifying --
    e.g. `options={"verify_signature": False}`, which would also have made the
    prod request succeed.
    """
    import jwt

    forged = jwt.encode(
        {
            "sub": "attacker",
            "user_id": "attacker",
            "email": "attacker@example.com",
            "role": "super_admin",
            "aud": "employee-portal",
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1),
        },
        "not-the-real-signing-secret-not-the-real-signing-secret",
        algorithm="HS256",
    )

    resp = client.get(
        _CLEANUP_ROUTE, headers={"Authorization": f"Bearer {forged}"}
    )

    assert resp.status_code == 401, resp.text
    assert not cleanup_db_spy.calls


def test_an_expired_token_is_still_rejected(client, cleanup_db_spy):
    """Kills a blanket `options={"verify_exp": False}` relaxation."""
    from config.settings import require_jwt_secret

    import jwt

    expired = jwt.encode(
        {
            "sub": "u",
            "user_id": "u",
            "email": "u@example.com",
            "role": "super_admin",
            "aud": "employee-portal",
            "exp": datetime.datetime.utcnow() - datetime.timedelta(hours=1),
            "iat": datetime.datetime.utcnow() - datetime.timedelta(hours=2),
        },
        require_jwt_secret(),
        algorithm="HS256",
    )

    resp = client.get(
        _CLEANUP_ROUTE, headers={"Authorization": f"Bearer {expired}"}
    )

    assert resp.status_code == 401, resp.text
    assert not cleanup_db_spy.calls


# ---------------------------------------------------------------------------
# The employee-management family: role fix only, and deliberately narrow.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", _ADMITTED)
def test_admin_roles_reach_employee_security_surfaces(client, audit_db_spy, role):
    """`routes/employees_security.py` spelled all seven of its guards
    `current_user["role"] != "admin"`. super_admin must clear them."""
    resp = client.get(
        _AUDIT_LOG_ROUTE,
        headers={"Authorization": f"Bearer {_portal_token(role)}"},
    )

    assert resp.status_code == 200, f"{role} was refused: {resp.text}"
    assert audit_db_spy.calls, f"{role} never reached the handler body"


@pytest.mark.parametrize("role", ("employee", "outsourced"))
def test_employee_security_stays_closed_to_non_admin_staff(
    client, audit_db_spy, role
):
    """The scope decision, pinned.

    Employee management creates, updates and DEACTIVATES employees and reads
    audit logs and API keys. Admitting super_admin is a correction; admitting
    `employee` would be a widening, and this fix deliberately does not do it.
    """
    resp = client.get(
        _AUDIT_LOG_ROUTE,
        headers={"Authorization": f"Bearer {_portal_token(role)}"},
    )

    assert resp.status_code == 403, f"{role} was admitted: {resp.status_code}"
    assert not audit_db_spy.calls


# ---------------------------------------------------------------------------
# Ratchet: the `!= "admin"` spelling must not come back.
#
# The #2031 ratchet (tests/test_super_admin_employee_route_access.py) is a
# line-scoped REGEX for `not in <container of quoted strings>`. Every one of
# the 67 sites this change fixed is invisible to it -- which is why they were
# still live on main after that fix landed. This is the companion for the
# comparison spellings, and it walks the AST rather than lines, so it sees a
# condition however it is wrapped and never reads a docstring as code.
#
# WHAT IT STILL DOES NOT SEE, stated plainly rather than implied:
#   * a POSITIVE allowlist -- `if role in ["employee", "admin"]`. Both live
#     instances (routes/agent_metrics.py, utils.auth.validate_entity_access)
#     already list super_admin, so this is a coverage gap, not a hidden defect
#     today; the sibling ratchet covers only the `not in` direction of it.
#   * any check routed through a helper, or a role compared against a name
#     built at runtime.
# Neither ratchet is a proof of absence. Together they pin the two spellings
# that have actually shipped this bug, twice.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _reads_a_role(node: ast.AST) -> bool:
    """True if `node` is an expression that reads a caller's role."""
    # current_user["role"]
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value == "role"
    # current_user.get("role") / current_user.get("role", "")
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" and node.args:
            first = node.args[0]
            return isinstance(first, ast.Constant) and first.value == "role"
        return False
    # a bare `role` local, as in `role = user["role"]` two lines up
    if isinstance(node, ast.Name):
        return node.id == "role" or node.id.endswith("_role")
    return False


def _mentions_super_admin(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Constant) and sub.value == "super_admin"
        for sub in ast.walk(node)
    )


def test_no_guard_compares_a_role_against_the_bare_string_admin():
    """Kills reintroducing `current_user["role"] != "admin"`.

    Against the parent commit this reports 67 offenders across 25 files -- the
    family #2031 could not see. `super_admin` is strictly more privileged than
    `admin` (utils.auth.ADMIN_ROLES holds both, and check_permission grants
    super_admin every permission unconditionally), so a guard that admits one
    and refuses the other is wrong however the comparison is spelled.

    A function that ALSO handles "super_admin" is not flagged: that is the
    shape of utils.auth.check_permission, whose `role == "admin"` branch sits
    below an unconditional `role == "super_admin": return True`. The rule is
    "super_admin must be accounted for", not "the string must not appear".
    """
    offenders = []
    for path in sorted(_REPO_ROOT.glob("**/*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel.startswith((".claude/", "tests/")) or "/.venv" in f"/{rel}":
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # not ours to police
            continue

        # Scopes that already answer for super_admin somewhere in their body.
        exempt: List[ast.AST] = [
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _mentions_super_admin(fn)
        ]
        exempt_nodes = {id(n) for fn in exempt for n in ast.walk(fn)}

        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or id(node) in exempt_nodes:
                continue
            if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                continue
            operands = [node.left, *node.comparators]
            has_admin = any(
                isinstance(o, ast.Constant) and o.value == "admin"
                for o in operands
            )
            if has_admin and any(_reads_a_role(o) for o in operands):
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        'Role compared against the bare string "admin" -- this silently '
        "refuses super_admin. Use `not in ADMIN_ROLES` (utils.auth):\n  "
        + "\n  ".join(sorted(offenders))
    )


# ---------------------------------------------------------------------------
# The other dependency in routes.auth_routes. `require_employee` already
# spelled its role check with EMPLOYEE_STAFF_ROLES, so it never carried defect
# 1 -- and was still unreachable with a portal token, because it chains the
# same validator. It is here to show the validator fix unblocks the whole
# module, not just the route the prod report named, and that widening the
# TOKEN did not widen the ROLE scope of a staff surface.
# ---------------------------------------------------------------------------

_PSP_OVERVIEW_ROUTE = "/api/psp/overview"


@pytest.fixture
def psp_db_spy(monkeypatch) -> _DatabaseSpy:
    from routes import psp_overview_routes as mod

    spy = _DatabaseSpy()
    monkeypatch.setattr(mod, "database", spy)
    return spy


@pytest.mark.parametrize("role", ("super_admin", "admin", "employee"))
def test_require_employee_admits_staff_with_a_portal_token(
    client, psp_db_spy, role
):
    """EMPLOYEE_STAFF_ROLES, reached at last.

    Kills a validator "fix" narrow enough to unblock only require_admin.
    """
    resp = client.get(
        _PSP_OVERVIEW_ROUTE,
        headers={"Authorization": f"Bearer {_portal_token(role)}"},
    )

    assert resp.status_code == 200, f"{role} was refused: {resp.text}"
    assert psp_db_spy.calls, f"{role} never reached the handler body"


@pytest.mark.parametrize("role", ("outsourced", "merchant", "agent"))
def test_require_employee_still_refuses_non_staff(client, psp_db_spy, role):
    """`outsourced` is the load-bearing one: it is an employee-portal login
    role that EMPLOYEE_STAFF_ROLES deliberately excludes, and accepting its
    token as VALID must not turn into accepting it as STAFF."""
    membership = {"merchant": "merchant", "agent": "agent"}.get(role, "employee")
    resp = client.get(
        _PSP_OVERVIEW_ROUTE,
        headers={"Authorization": f"Bearer {_portal_token(role, membership)}"},
    )

    assert resp.status_code == 403, f"{role} was admitted: {resp.status_code}"
    assert not psp_db_spy.calls


# ---------------------------------------------------------------------------
# The ownership-OR-admin guards.
#
# `routes/payment_routing_routes.py` (5 sites) and `routes/agent_protocol_test.py`
# (1) spell their guard `caller is the agent OR caller is an admin`. This
# change edits only the second conjunct, and a review found the first one is
# pinned by nothing: replacing all five conditions with `if False:` -- deleting
# authorization from execute-payment, read-routes, create/update/delete-route
# -- left `pytest -k "routing or payment or agent"` at 1662 passed, 0 failed.
#
# So the conjunct this PR did NOT touch gets its coverage here, next to the one
# it did. Editing an admin allowlist beside an unpinned ownership test is how a
# later "cleanup" quietly turns an agent-scoped route into an open one.
# ---------------------------------------------------------------------------

_AGENT_ROUTES = "/agents/{agent_id}/routes"
_OWNER_AGENT = "agent-owner-1"
_OTHER_AGENT = "agent-other-2"


def _agent_token(user_id: str) -> str:
    """An agent-portal token whose `user_id` is what the guard compares."""
    return _sign(
        {
            "sub": user_id,
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "role": "agent",
            "membership_type": "agent",
            "agent_id": user_id,
            "aud": "agent-portal",
            "scope": "agent",
        }
    )


@pytest.fixture
def routing_db_spy(monkeypatch) -> _DatabaseSpy:
    from routes import payment_routing_routes as mod

    spy = _DatabaseSpy()
    monkeypatch.setattr(mod, "database", spy)
    return spy


def test_an_agent_reaches_its_own_routing_config(client, routing_db_spy):
    """The ownership conjunct, admit side.

    Kills a "fix" that drops `user_id != agent_id` and leaves admin-only --
    which would lock every agent out of its own configuration.
    """
    resp = client.get(
        _AGENT_ROUTES.format(agent_id=_OWNER_AGENT),
        headers={"Authorization": f"Bearer {_agent_token(_OWNER_AGENT)}"},
    )

    assert resp.status_code == 200, resp.text
    assert routing_db_spy.calls, "the owning agent never reached the handler"


def test_an_agent_cannot_read_another_agents_routing_config(
    client, routing_db_spy
):
    """The ownership conjunct, refuse side -- the one nothing pinned.

    Kills deleting the guard, and kills any rewrite that admits every
    authenticated agent. `psp_priority` and `routing_strategy` are another
    tenant's payment configuration.
    """
    resp = client.get(
        _AGENT_ROUTES.format(agent_id=_OWNER_AGENT),
        headers={"Authorization": f"Bearer {_agent_token(_OTHER_AGENT)}"},
    )

    assert resp.status_code == 403, (
        f"agent {_OTHER_AGENT} read {_OWNER_AGENT}'s routes: {resp.text}"
    )
    assert not routing_db_spy.calls, "a refused agent reached the handler body"


@pytest.mark.parametrize("role", _ADMITTED)
def test_admin_roles_override_agent_ownership(client, routing_db_spy, role):
    """The admin conjunct this PR fixed: `super_admin` was refused here too.

    Its user_id is not the agent_id, so it clears the guard only on the second
    conjunct -- which is exactly the one that read `!= "admin"`.
    """
    resp = client.get(
        _AGENT_ROUTES.format(agent_id=_OWNER_AGENT),
        headers={"Authorization": f"Bearer {_portal_token(role)}"},
    )

    assert resp.status_code == 200, f"{role} was refused: {resp.text}"
    assert routing_db_spy.calls, f"{role} never reached the handler body"


@pytest.mark.parametrize("role", ("employee", "outsourced", "merchant"))
def test_non_admin_non_owner_is_refused_agent_routing(
    client, routing_db_spy, role
):
    """Staff are not owners and not admins here, and stay refused."""
    membership = "merchant" if role == "merchant" else "employee"
    resp = client.get(
        _AGENT_ROUTES.format(agent_id=_OWNER_AGENT),
        headers={"Authorization": f"Bearer {_portal_token(role, membership)}"},
    )

    assert resp.status_code == 403, f"{role} was admitted: {resp.status_code}"
    assert not routing_db_spy.calls
