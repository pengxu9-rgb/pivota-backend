"""`super_admin` must not be locked out of the employee portal it can log into.

THE DEFECT, live on prod until this fix (observed 2026-09-03 against
api.pivota.cc: GET /employee/agents answered 403 to a signed, valid employee
portal token). Two allowlists disagreed about what an employee-portal role is:

  * routes.auth.EMPLOYEE_AUTH_ROLES -- what /auth/signin ADMITS --
    {"super_admin", "admin", "employee", "outsourced"}
  * the per-route guards -- what the pages BEHIND that login accept --
    spelled inline as ["employee", "admin"] (and ["admin", "employee"], and
    ["merchant", "employee", "admin"]) in 88 places across 21 route modules.

`super_admin` is in the first set and was absent from the second, so the most
privileged role in the system could authenticate into the employee portal and
then be refused by 88 of the surfaces behind it. The 403 body was "Not
authorized" (the route's own role check), NOT "Not authenticated" -- the token
was always valid; only the role test rejected it.

THE RULE: employee-portal staff surfaces admit exactly
utils.auth.EMPLOYEE_STAFF_ROLES = super_admin + admin + employee. `outsourced`
stays OUT -- it is an employee-portal login role, but contractor scope is a
separate decision and this fix deliberately did not widen it.

Mutants each test kills are named inline. Tokens are REAL signed JWTs, never
the `test-token` placeholder, whose pytest-only bypass in utils.auth returns
role=admin and would make every claim here vacuous.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

_ROUTE = "/employee/agents"

# EMPLOYEE_AUTH_ROLES minus `outsourced`: every role that both reaches the
# employee-portal login AND is meant to work once inside.
_ADMITTED = ("super_admin", "admin", "employee")

# Roles that must still be refused. `outsourced` documents the deliberate
# scoping choice; the rest are the positive counterpart proving the gate still
# gates rather than having been flung open.
_REFUSED = ("outsourced", "agent", "merchant", "buyer")


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _token(role: str) -> str:
    from utils.auth import create_access_token

    return create_access_token(
        {"sub": f"u-{role}", "email": f"{role}@example.com", "role": role}
    )


class _DatabaseSpy:
    """Stands in for routes.employee_agent_mgmt.database.

    Records every call so a test can assert whether the handler body ran. An
    empty fetch_all drives the real handler to its ordinary empty response, so
    the ADMIT tests exercise the shipped code path rather than a stub of it.
    """

    def __init__(self) -> None:
        self.calls: List[str] = []

    async def fetch_all(self, query: str, *a: Any, **kw: Any) -> List[Dict[str, Any]]:
        self.calls.append("fetch_all")
        return []

    async def fetch_one(self, query: str, *a: Any, **kw: Any) -> None:
        self.calls.append("fetch_one")
        return None

    async def execute(self, query: str, *a: Any, **kw: Any) -> None:
        self.calls.append("execute")
        return None


@pytest.fixture
def db_spy(monkeypatch) -> _DatabaseSpy:
    from routes import employee_agent_mgmt as mod

    spy = _DatabaseSpy()
    monkeypatch.setattr(mod, "database", spy)
    return spy


@pytest.mark.parametrize("role", _ADMITTED)
def test_staff_roles_reach_the_handler(client, db_spy, role):
    """Kills: dropping super_admin from EMPLOYEE_STAFF_ROLES (the shipped bug),
    and any rewrite that admits the role but never enters the handler body."""
    resp = client.get(_ROUTE, headers={"Authorization": f"Bearer {_token(role)}"})

    assert resp.status_code == 200, (
        f"{role} was refused with {resp.status_code}: {resp.text}"
    )
    # Reaching the handler body is the claim -- a 200 synthesized before the
    # DB call would pass the status assertion while the gate still rejected.
    assert db_spy.calls, f"{role} never reached the handler body"


def test_super_admin_is_not_told_it_is_unauthorized(client, db_spy):
    """The exact prod symptom, pinned: the 403/"Not authorized" pair.

    Kills a regression that swaps super_admin's admission for any other refusal
    (401, or a 403 with different wording) -- all of which would still lock the
    portal's most privileged role out of its own pages.
    """
    resp = client.get(
        _ROUTE, headers={"Authorization": f"Bearer {_token('super_admin')}"}
    )

    # `!= 403` alone would also pass on a 500, so pin the success status too.
    assert resp.status_code == 200, resp.text
    assert "Not authorized" not in resp.text


@pytest.mark.parametrize("role", _REFUSED)
def test_non_staff_roles_are_refused_before_the_handler(client, db_spy, role):
    """Kills: widening the guard to `EMPLOYEE_ROLES` (which would let
    `outsourced` in), or deleting the role check outright. A refused request
    must not touch the database."""
    resp = client.get(_ROUTE, headers={"Authorization": f"Bearer {_token(role)}"})

    assert resp.status_code == 403, f"{role} was admitted: {resp.status_code}"
    assert not db_spy.calls, f"refused {role} still reached the handler body"


def test_unauthenticated_is_still_rejected(client, db_spy):
    """Positive counterpart: admitting super_admin must not have opened the
    route to anonymous callers."""
    resp = client.get(_ROUTE)

    assert resp.status_code in (401, 403)
    assert not db_spy.calls


# ---------------------------------------------------------------------------
# The constants themselves. Route tests above only exercise
# EMPLOYEE_STAFF_ROLES; its two siblings are guarded by nothing but their
# derivation from it. Respell MERCHANT_OR_EMPLOYEE_STAFF_ROLES as the literal
# ["merchant", "employee", "admin"] and the original prod defect returns across
# 15 sites with no route test and no ratchet line firing -- a review caught
# exactly that mutant surviving. These pin the invariant directly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ("EMPLOYEE_STAFF_ROLES", "MERCHANT_OR_EMPLOYEE_STAFF_ROLES",
     "AGENT_OR_EMPLOYEE_STAFF_ROLES"),
)
def test_every_staff_constant_admits_every_admin_role(name):
    """Kills: respelling any staff constant as a literal that drops super_admin.

    ADMIN_ROLES is the system's own answer to "who is an admin" (utils.auth,
    and what require_admin enforces). A staff surface that admits `admin` while
    refusing `super_admin` contradicts it -- that contradiction WAS the bug.
    """
    import utils.auth as auth

    roles = getattr(auth, name)
    missing = [r for r in auth.ADMIN_ROLES if r not in roles]
    assert not missing, f"{name} omits admin role(s) {missing}: {roles}"


def test_staff_constants_still_exclude_outsourced():
    """The deliberate scoping decision, pinned.

    Kills a well-meaning "just use EMPLOYEE_ROLES everywhere" cleanup, which
    would silently widen 88 staff surfaces to contractors.
    """
    import utils.auth as auth

    for name in (
        "EMPLOYEE_STAFF_ROLES",
        "MERCHANT_OR_EMPLOYEE_STAFF_ROLES",
        "AGENT_OR_EMPLOYEE_STAFF_ROLES",
    ):
        assert "outsourced" not in getattr(auth, name), (
            f"{name} now admits contractors -- that is a scope decision, not a "
            "refactor"
        )


# ---------------------------------------------------------------------------
# Ratchet: the inline spelling must not come back.
# ---------------------------------------------------------------------------

# Lists, tuples AND sets, single- or double-quoted. The first cut of this
# ratchet matched only double-quoted lists, and a review caught what that missed:
# agent_management.py guarded GET /agents/{id} with a TUPLE, ("agent",
# "employee", "admin"), so the same super_admin omission survived in a module
# this very change had already edited five times. Container delimiters are not
# matched as a pair here -- role allowlists in real source are well-formed, and
# a lint heuristic that over-matches is the safe direction.
_ALLOWLIST_RE = re.compile(r"""not in [\[({]((?:\s*["'][a-z_]+["']\s*,?)+)[\])}]""")
_ROLE_RE = re.compile(r"""["']([a-z_]+)["']""")
_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_route_hardcodes_an_employee_allowlist_missing_super_admin():
    """The 88 inline lists this PR replaced must not be reintroduced.

    Against the parent commit this test reports 88 offenders across 21 files;
    it is the reason the fix is a shared constant and not 88 edited literals.
    """
    offenders = []
    for path in sorted(_REPO_ROOT.glob("**/*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel.startswith((".claude/", "tests/")) or "/.venv" in f"/{rel}":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for match in _ALLOWLIST_RE.finditer(line):
                roles = set(_ROLE_RE.findall(match.group(1)))
                if {"employee", "admin"} <= roles and "super_admin" not in roles:
                    offenders.append(f"{rel}:{lineno}")

    assert not offenders, (
        "Role allowlist hardcoded without super_admin -- import "
        "EMPLOYEE_STAFF_ROLES / MERCHANT_OR_EMPLOYEE_STAFF_ROLES from "
        "utils.auth instead:\n  " + "\n  ".join(offenders)
    )
