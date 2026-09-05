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
# Ratchet: a role allowlist that admits `admin` must admit `super_admin`.
#
# The #2031 ratchet is a line-scoped REGEX that flags `not in <container>` only
# when the container holds BOTH "employee" and "admin". Two whole spellings
# walked through it and were still live on main months later:
#
#   * the comparisons -- `current_user["role"] != "admin"`, 67 sites/25 files;
#   * two-element containers -- `["merchant", "admin"]` (7 sites) and
#     `("agent", "admin")` (2 sites, routes/protocol_routes.py), which have no
#     "employee" member for that regex to key on. The protocol_routes pair
#     refused `super_admin` the protocol LIST for an agent whose protocols it
#     could enable and disable two routes further down the same file.
#
# This one walks the AST, so it sees a condition however it is wrapped, never
# reads a docstring as code, and covers both shapes.
#
# WHAT IT STILL DOES NOT SEE, stated plainly rather than implied:
#   * a container built at runtime, or membership routed through a helper;
#   * a role compared against a name that is not a literal.
# It is not a proof of absence. It pins the shapes that have now shipped this
# bug three times.
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


def _string_container(node: ast.AST):
    """The members of a list/tuple/set literal of plain strings, else None."""
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return None
    values = []
    for element in node.elts:
        if not (isinstance(element, ast.Constant)
                and isinstance(element.value, str)):
            return None
        values.append(element.value)
    return values or None


def _accounts_for_super_admin(fn: ast.AST) -> bool:
    """True if this function actually TESTS for super_admin somewhere.

    Deliberately not "mentions the string": a review pointed out that the first
    cut exempted any function whose body merely contained "super_admin", so a
    stray `logger.info("super_admin only")` would have licensed `!= "admin"`
    underneath it. Only a comparison or a membership container counts.
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for operand in operands:
                if (isinstance(operand, ast.Constant)
                        and operand.value == "super_admin"):
                    return True
                members = _string_container(operand)
                if members and "super_admin" in members:
                    return True
    return False


def _iter_reviewable_files():
    for path in sorted(_REPO_ROOT.glob("**/*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel.startswith((".claude/", "tests/")) or "/.venv" in f"/{rel}":
            continue
        try:
            yield rel, ast.parse(path.read_text())
        except SyntaxError:  # not ours to police
            continue


def _exempt_node_ids(tree: ast.AST) -> set:
    """Nodes inside a function that already answers for super_admin."""
    return {
        id(node)
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _accounts_for_super_admin(fn)
        for node in ast.walk(fn)
    }


def test_no_guard_compares_a_role_against_the_bare_string_admin():
    """Kills reintroducing `current_user["role"] != "admin"`.

    Against the parent commit this reports 67 offenders across 25 files -- the
    family #2031 could not see. `super_admin` is strictly more privileged than
    `admin` (utils.auth.ADMIN_ROLES holds both, and check_permission grants
    super_admin every permission unconditionally), so a guard that admits one
    and refuses the other is wrong however the comparison is spelled.

    A function that actually TESTS for "super_admin" is not flagged: that is
    the shape of utils.auth.check_permission, whose `role == "admin"` branch
    sits below an unconditional `role == "super_admin": return True`. The rule
    is "super_admin must be accounted for", not "the string must not appear".
    """
    offenders = []
    for rel, tree in _iter_reviewable_files():
        exempt = _exempt_node_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or id(node) in exempt:
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


def test_no_role_allowlist_admits_admin_while_refusing_super_admin():
    """Kills the container spelling that survived #2031 entirely.

    `["merchant", "admin"]` and `("agent", "admin")` are role allowlists with
    no "employee" member, so #2031's regex had nothing to key on and left 9
    sites live: 7 merchant-or-admin guards over another tenant's orders and PSP
    telemetry, and 2 in routes/protocol_routes.py that refused `super_admin`
    an agent's protocol list while the sibling enable/disable routes in the
    same file admitted it.

    Shared constants exist for exactly this and are what the fix uses --
    utils.auth.MERCHANT_OR_ADMIN_ROLES and AGENT_OR_ADMIN_ROLES. Note they are
    NOT the ...EMPLOYEE_STAFF_ROLES variants: swapping in those would newly
    admit every `employee`, which is a grant rather than a correction.
    """
    offenders = []
    for rel, tree in _iter_reviewable_files():
        exempt = _exempt_node_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or id(node) in exempt:
                continue
            if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                continue
            for comparator in node.comparators:
                members = _string_container(comparator)
                if members and "admin" in members and "super_admin" not in members:
                    offenders.append(f"{rel}:{node.lineno} {members}")

    assert not offenders, (
        "Role allowlist admits `admin` but not `super_admin`. Use a shared "
        "constant from utils.auth (ADMIN_ROLES / MERCHANT_OR_ADMIN_ROLES / "
        "AGENT_OR_ADMIN_ROLES / the ...EMPLOYEE_STAFF_ROLES variants where "
        "staff really are intended):\n  " + "\n  ".join(sorted(offenders))
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


def _agent_token(user_id: str, agent_id: str = None) -> str:
    """An agent-portal token.

    `user_id` is the claim the payment-routing guard actually compares. By
    default `agent_id` mirrors it, which is the shape the guard's ownership
    branch is written for -- and, as a review established, a shape NO issuer
    mints: routes/agent_account.py stamps `user_id` from the `users` PK and
    `agent_id` as `agent_<hex>` (db/agents.py). Pass both to build the real
    thing; see test_a_real_agent_token_is_refused_its_own_routing_config.
    """
    return _sign(
        {
            "sub": user_id,
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "role": "agent",
            "membership_type": "agent",
            "agent_id": agent_id or user_id,
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
    """The ownership conjunct, admit side -- as the guard is WRITTEN.

    Kills a "fix" that drops `user_id != agent_id` and leaves admin-only.

    Read this together with the test below: the token here is SYNTHETIC. It
    sets `user_id == agent_id` because that is what the shipped guard compares,
    and no live issuer produces it. This test therefore pins the branch, not a
    reachable behaviour -- keeping the two apart is the point.
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


def test_a_real_agent_token_is_refused_its_own_routing_config(
    client, routing_db_spy
):
    """The reachable behaviour, pinned as it actually is: a fail-closed lockout.

    A real agent-portal token carries `user_id` = the `users` PK and
    `agent_id` = `agent_<hex>` (routes/agent_account.py, db/agents.py) -- two
    different values. The guard compares `user_id` against the path's agent_id,
    so the ownership branch can never fire for a real agent and every agent is
    403'd on its own payment-routing configuration.

    PRE-EXISTING and NOT changed by this PR, which touches only the admin
    conjunct beside it. Recorded rather than fixed because the fix GRANTS
    access that is denied today -- comparing the `agent_id` claim as well is a
    scope decision that deserves its own review, not a side effect of a
    super_admin correction. This test fails the moment someone makes it, which
    is exactly when it should be revisited.
    """
    resp = client.get(
        _AGENT_ROUTES.format(agent_id=_OWNER_AGENT),
        headers={
            "Authorization": f"Bearer {_agent_token('42', _OWNER_AGENT)}"
        },
    )

    assert resp.status_code == 403, (
        "an agent now reaches its own routing config -- if that is deliberate, "
        "this lockout has been fixed and the test should assert 200: "
        + resp.text
    )
    assert not routing_db_spy.calls


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


# ---------------------------------------------------------------------------
# /auth/me, the other endpoint the validator unblocked.
# ---------------------------------------------------------------------------


def test_auth_me_answers_a_portal_token_with_its_real_email(client):
    """Kills the reverted `"email": current_user["user_id"]`.

    /auth/me chains the same validator, so it rejected every canonical portal
    token too. Unblocking it exposed a field that had been wrong all along and
    unreachable: `email` was filled from `user_id`, which for a portal session
    is an `identity_...` string, not an address.
    """
    resp = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {_portal_token('super_admin')}"},
    )

    assert resp.status_code == 200, resp.text
    user = resp.json()["user"]
    assert user["email"] == "super_admin@example.com", user
    assert user["role"] == "super_admin"


# ---------------------------------------------------------------------------
# The adapter's RETURN SHAPE.
#
# A mutation audit found that reverting the adapter to the old narrow
# `{"user_id", "role"}` dict killed nothing in the entire 14k-test suite, even
# though routes/direct_db_check.py reads `merchant_id` off it. The widened
# shape is a deliberate part of "one validator" -- the shared validator returns
# the claim set, so this must too -- and it was unpinned.
# ---------------------------------------------------------------------------


async def test_the_adapter_returns_the_whole_claim_set():
    """Kills reverting `verify_jwt_token` to `{"user_id", "role"}`.

    Every claim the shared validator hands back must survive the adapter;
    narrowing it here would silently re-introduce the divergence this PR
    exists to remove, just on the response side instead of the accept side.
    """
    import inspect

    from fastapi.security import HTTPAuthorizationCredentials

    from routes.auth_routes import verify_jwt_token

    token = _portal_token("super_admin")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    result = verify_jwt_token(creds)
    if inspect.isawaitable(result):
        result = await result

    for claim in ("sub", "email", "role", "user_id", "identity_id",
                  "membership_type", "membership_id", "employee_id",
                  "aud", "scope"):
        assert claim in result, f"the adapter dropped the {claim!r} claim: {result}"
    assert result["email"] == "super_admin@example.com"
    assert result["aud"] == "employee-portal"


# ---------------------------------------------------------------------------
# The three highest-risk guards this PR edits.
#
# The same audit measured line coverage across all 67 changed guard sites: 5
# were executed by any test, and deleting authorization outright from 18 of the
# rest was invisible to the whole suite. The mechanical equivalence of those
# edits is provable (base + the rewrite == the shipped AST for 75 of 76), but
# "provably unchanged" is not "guarded", and these three are the ones whose
# blast radius justifies a behavioural test rather than a proof.
# ---------------------------------------------------------------------------

_EXECUTE_PAYMENT = "/agents/{agent_id}/payments/route"
_DELETE_EMPLOYEE = "/employees/emp-42"
_OVERRIDE_PERMISSION = "/employee/routing/agents/{agent_id}/override-permission"

# A VALID body matters here. FastAPI validates the request model before the
# handler runs, so an incomplete body answers 422 and the in-handler guard is
# never reached -- the refusal would be the validator's, not the role check's,
# and the test would prove nothing. (It caught exactly that on the first cut:
# a missing `order_id` produced 422 instead of 403.)
_PAYMENT_BODY = {
    "order_id": "ord-1",
    "amount": 10.0,
    "currency": "USD",
    "merchant_id": "m-1",
}


@pytest.mark.parametrize("role", ("employee", "outsourced", "merchant", "agent"))
def test_execute_payment_refuses_non_admin_non_owner(client, routing_db_spy, role):
    """`POST /agents/{id}/payments/route` executes a real payment with failover
    on another tenant's behalf. Highest blast radius of the 67 sites."""
    membership = {"merchant": "merchant", "agent": "agent"}.get(role, "employee")
    resp = client.post(
        _EXECUTE_PAYMENT.format(agent_id=_OWNER_AGENT),
        json=_PAYMENT_BODY,
        headers={"Authorization": f"Bearer {_portal_token(role, membership)}"},
    )

    assert resp.status_code == 403, f"{role} was admitted: {resp.status_code}"
    assert not routing_db_spy.calls, f"refused {role} reached the handler body"


@pytest.mark.parametrize("role", ("employee", "outsourced", "merchant"))
def test_employee_deactivation_refuses_non_admins(client, audit_db_spy, role):
    """`DELETE /employees/{id}` sets an employee inactive -- a privilege write.
    `employee` is staff and must still be refused."""
    membership = "merchant" if role == "merchant" else "employee"
    resp = client.delete(
        _DELETE_EMPLOYEE,
        headers={"Authorization": f"Bearer {_portal_token(role, membership)}"},
    )

    assert resp.status_code == 403, f"{role} was admitted: {resp.status_code}"
    assert not audit_db_spy.calls, f"refused {role} reached the handler body"


@pytest.mark.parametrize("role", _ADMITTED)
def test_admin_roles_can_deactivate_an_employee(client, audit_db_spy, role):
    """The admit side, so the refusals above cannot pass by the route being
    broken for everyone."""
    resp = client.delete(
        _DELETE_EMPLOYEE,
        headers={"Authorization": f"Bearer {_portal_token(role)}"},
    )

    assert resp.status_code != 403, f"{role} was refused: {resp.text}"
    assert audit_db_spy.calls, f"{role} never reached the handler body"


@pytest.fixture
def governance_db_spy(monkeypatch) -> _DatabaseSpy:
    from routes import routing_governance as mod

    spy = _DatabaseSpy()
    monkeypatch.setattr(mod, "database", spy)
    return spy


@pytest.mark.parametrize("role", ("employee", "outsourced"))
def test_routing_override_grant_refuses_non_admin_staff(
    client, governance_db_spy, role
):
    """Grants an agent the right to OVERRIDE merchant routing rules. The route
    sits behind get_current_employee, so `employee` and `outsourced` clear the
    router-level dependency and are stopped only by this guard."""
    resp = client.put(
        _OVERRIDE_PERMISSION.format(agent_id=_OWNER_AGENT),
        params={"enabled": True},
        headers={"Authorization": f"Bearer {_portal_token(role)}"},
    )

    assert resp.status_code == 403, f"{role} was admitted: {resp.status_code}"
    assert not governance_db_spy.calls, f"refused {role} reached the handler"


@pytest.mark.parametrize("role", _ADMITTED)
def test_admin_roles_can_grant_routing_override(client, governance_db_spy, role):
    """The admit side -- and the super_admin half of this PR's fix, on a route
    whose guard was `!= "admin"` behind an employee-only router."""
    resp = client.put(
        _OVERRIDE_PERMISSION.format(agent_id=_OWNER_AGENT),
        params={"enabled": True},
        headers={"Authorization": f"Bearer {_portal_token(role)}"},
    )

    assert resp.status_code != 403, f"{role} was refused: {resp.text}"
    assert governance_db_spy.calls, f"{role} never reached the handler body"
