"""
Regression: an anonymous caller must not be able to obtain a JWT whose `role`
is privileged.

Three separate lanes used to hand out admin tokens to unauthenticated callers:

1. `POST /auth/signup` accepted a caller-supplied `role` (`UserRole.ADMIN`),
   wrote it to the in-memory `user_roles_db` with `"approved": True`, and
   `POST /auth/signin` checked that dict *before* any DB lookup and minted a JWT
   with the self-chosen role.
2. `GET /auth/admin-token` returned an `role: "admin"` JWT to an anonymous GET
   with no credentials at all.
3. `POST /admin/employees/reset-password` was unauthenticated ("emergency
   access") and INSERTed an `employees` row with a hardcoded `role: "admin"`,
   `status: "active"`, and a caller-chosen password; `/auth/signin` then minted
   an admin JWT from it. Its `ON CONFLICT (email) DO UPDATE` also took over any
   existing employee and re-activated disabled ones, and
   `GET /admin/employees/list` published the roster to pick a target from.
4. `POST /api/auth/register` is mounted unauthenticated and persisted a
   caller-supplied `role` -- including `admin`/`super_admin` -- into the `users`
   table, which `POST /api/auth/login` and `POST /auth/signin` then trust.

Such a token satisfies `require_admin`, `get_current_admin`,
`require_admin_or_key`, `ADMIN_ROLES`, and every route checking
`role in ["admin", "super_admin"]`, so the blast radius is the whole admin
surface.

These tests drive the real `main.app` -- no `dependency_overrides`, no test
tokens, no bypasses.
"""

import inspect
import re

import jwt as pyjwt
import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from config.settings import settings
from utils.auth import ADMIN_ROLES

# Roles that clear an admin gate somewhere in the codebase. ADMIN_ROLES covers
# `require_admin`/`get_current_admin`; `employee`/`outsourced` clear
# `require_employee` and the employee-portal routes.
PRIVILEGED_ROLES = frozenset(ADMIN_ROLES) | {"employee", "outsourced"}

# Imported, never copied: a local list would let main.py's real set shrink with
# this suite still green.
REMOVED_INMEMORY_AUTH_PATHS = sorted(main.LEGACY_INMEMORY_AUTH_PATHS)

# Roles a caller may legitimately self-assign, per the production policy.
SELF_SERVICE_ROLES = frozenset({"merchant", "agent"})


def _validator_roles() -> set:
    """The role vocabulary read out of `_validate_role_value` itself."""
    from routes.auth import _validate_role_value

    src = inspect.getsource(_validate_role_value)
    return set(re.findall(r"'([a-z_]+)'", src.split("valid_roles")[1]))


def test_privileged_role_universe_is_exactly_the_non_self_service_roles():
    """Pins PRIVILEGED_ROLES against the validator, in BOTH directions.

    Without this, narrowing upstream `ADMIN_ROLES` silently drops parametrized
    cases: coverage decreases and the suite still reports green.
    """
    accepted = _validator_roles()
    assert accepted, "could not read the role vocabulary; the parse is stale"
    assert SELF_SERVICE_ROLES < accepted
    assert PRIVILEGED_ROLES == accepted - SELF_SERVICE_ROLES, (
        "PRIVILEGED_ROLES drifted from the validator's vocabulary: "
        f"{sorted(PRIVILEGED_ROLES)} vs {sorted(accepted - SELF_SERVICE_ROLES)}"
    )


@pytest.fixture
def client():
    # Bare TestClient (not a context manager) so app lifespan/DB startup is not run.
    return TestClient(main.app)


def _roles_in_any_token(payload) -> list:
    """Decode every JWT-looking string in a response body and return its `role`."""
    roles = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and node.count(".") == 2 and node.startswith("ey"):
            try:
                claims = pyjwt.decode(
                    node, settings.jwt_secret_key, algorithms=["HS256"]
                )
            except Exception:
                return
            if "role" in claims:
                roles.append(claims["role"])

    walk(payload)
    return roles


def _assert_no_privileged_token(response, label: str):
    """Scan a response for a JWT carrying a privileged role.

    Deliberately runs on EVERY status, not just 2xx. An earlier version bailed
    out on `status >= 400`, which made it dead code at all seven call sites --
    each is preceded by an assert that the status is 401/403/404/405. A 500 that
    leaks a token in its body is exactly the case worth catching.
    """
    try:
        body = response.json()
    except ValueError:
        return
    for role in _roles_in_any_token(body):
        assert role not in PRIVILEGED_ROLES, (
            f"{label} returned an anonymously-obtainable token with "
            f"privileged role {role!r}"
        )


# ---------------------------------------------------------------------------
# 1. The removed in-memory lanes stay removed
# ---------------------------------------------------------------------------

def test_removed_inmemory_auth_routes_are_not_mounted():
    mounted = {getattr(route, "path", None) for route in main.app.routes}
    still_mounted = sorted(p for p in REMOVED_INMEMORY_AUTH_PATHS if p in mounted)
    assert not still_mounted, (
        "in-memory auth fixtures are mounted again: " + ", ".join(still_mounted)
    )


def test_guard_bans_exactly_the_expected_paths():
    """Pins the CONTENTS of the banned set against an explicit literal.

    The parametrized test below iterates over the real set, so it cannot catch
    that set SHRINKING -- deleting an entry silently deletes its own test case.
    This literal is the counterweight: removing a path fails here. Adding one is
    also a deliberate act, so it fails here too and the author updates both.
    """
    assert set(main.LEGACY_INMEMORY_AUTH_PATHS) == {
        "/auth/signup",
        "/auth/admin-token",
        "/auth/admin/users",
        "/auth/admin/users/{user_id}/approve",
        "/auth/admin/users/{user_id}/role",
    }


@pytest.mark.parametrize("banned_path", REMOVED_INMEMORY_AUTH_PATHS)
def test_guard_detects_a_reintroduction_of_every_banned_path(banned_path):
    """Every entry in the banned set must be load-bearing.

    Probing only `/auth/signup` let four of the five entries be deleted with the
    suite still green.
    """
    probe = FastAPI()
    probe.post(banned_path)(lambda: {})  # pragma: no cover - never called

    real_app = main.app
    try:
        main.app = probe
        with pytest.raises(RuntimeError, match=re.escape(banned_path)):
            main._guard_legacy_inmemory_auth_routes()
    finally:
        main.app = real_app

    # ...and it passes against the real app.
    main._guard_legacy_inmemory_auth_routes()


def test_guard_is_actually_invoked_at_import_time():
    """The guard's job is to fail STARTUP, so the call site is the thing.

    Calling the function directly from a test leaves `main.py`'s module-level
    invocation untested -- deleting it kept the whole suite green.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(main.__file__).read_text())
    guard_calls = [
        node.lineno
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None)
        == "_guard_legacy_inmemory_auth_routes"
    ]
    assert guard_calls, (
        "main.py defines the guard but never calls it at module scope, so it "
        "cannot fail startup"
    )

    # It must also run AFTER the router it polices is mounted.
    mounts = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "include_router"
        and any(getattr(a, "id", None) == "auth_router" for a in node.args)
    ]
    assert mounts, "could not locate app.include_router(auth_router) in main.py"
    assert max(guard_calls) > max(mounts), (
        "the guard runs before auth_router is mounted, so it inspects an "
        "incomplete route table"
    )


def test_inmemory_user_stores_are_gone():
    import routes.auth_routes as auth_routes_module

    for name in ("users_db", "user_roles_db", "sessions_db"):
        assert not hasattr(auth_routes_module, name), (
            f"routes.auth_routes.{name} is back; it let signin resolve a "
            "self-chosen role before any DB lookup"
        )


def test_anonymous_signup_and_admin_token_endpoints_are_dead(client):
    # Accept either status: an unrouted path returns 404 normally, but if an
    # OPTIONS-only catch-all like `/{full_path:path}` is ever (re-)mounted, the
    # path partial-matches and Starlette answers 405 instead. Both mean "no such
    # route" -- the authoritative check is the unmounted-routes test above.
    signup = client.post(
        "/auth/signup",
        json={
            "email": "mallory@evil.example",
            "password": "hunter2hunter2",
            "role": "admin",
        },
    )
    assert signup.status_code in (404, 405), signup.text
    _assert_no_privileged_token(signup, "POST /auth/signup")

    admin_token = client.get("/auth/admin-token")
    assert admin_token.status_code in (404, 405), admin_token.text
    _assert_no_privileged_token(admin_token, "GET /auth/admin-token")

    # Signin must not authenticate the account signup used to create. NOTE: in
    # this environment the DB is not connected, so both signin lanes error out
    # and return 401 for their own reason -- this assertion confirms no token is
    # issued, it does NOT by itself prove the in-memory lane is gone. The
    # authoritative proof of that is `test_inmemory_user_stores_are_gone` and the
    # unmounted-routes test above.
    signin = client.post(
        "/auth/signin",
        json={"email": "mallory@evil.example", "password": "hunter2hunter2"},
    )
    assert signin.status_code == 401, signin.text
    _assert_no_privileged_token(signin, "POST /auth/signin")


# ---------------------------------------------------------------------------
# 2. The DB-backed twin: /api/auth/register must not persist a privileged role
# ---------------------------------------------------------------------------

@pytest.fixture
def register_db(monkeypatch):
    """Stub the users table and record every INSERT attempt."""
    import routes.auth as auth_api_module

    inserts = []

    async def fake_fetch_one(query=None, values=None):
        return None  # no existing user

    async def fake_fetch_val(query=None, values=None):
        inserts.append(dict(values or {}))
        return 1

    monkeypatch.setattr(auth_api_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(auth_api_module.database, "fetch_val", fake_fetch_val)
    return inserts


@pytest.mark.parametrize("role", sorted(PRIVILEGED_ROLES))
def test_anonymous_register_cannot_self_assign_a_privileged_role(
    client, register_db, role
):
    res = client.post(
        "/api/auth/register",
        json={
            "email": f"mallory-{role}@evil.example",
            "password": "Hunter2hunter2",
            "role": role,
        },
    )
    assert res.status_code == 403, res.text
    assert register_db == [], (
        f"role {role!r} was rejected only AFTER the row was written: {register_db}"
    )
    _assert_no_privileged_token(res, f"POST /api/auth/register role={role}")


def test_anonymous_register_cannot_use_the_privileged_default_role(
    client, register_db
):
    """Omitting `role` defaults to `employee`, which is privileged."""
    res = client.post(
        "/api/auth/register",
        json={"email": "mallory-default@evil.example", "password": "Hunter2hunter2"},
    )
    assert res.status_code == 403, res.text
    assert register_db == []


@pytest.mark.parametrize("role", ["merchant", "agent"])
def test_anonymous_register_still_works_for_self_service_roles(
    client, register_db, role
):
    """The guard must be a role split, not a blanket denial of registration."""
    res = client.post(
        "/api/auth/register",
        json={
            "email": f"someone-{role}@example.com",
            "password": "Hunter2hunter2",
            "role": role,
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["success"] is True, res.text
    assert [row["role"] for row in register_db] == [role]


def test_admin_bearer_may_still_provision_a_privileged_role(client, register_db):
    """An authenticated admin keeps the ability to create staff accounts."""
    from utils.auth import create_access_token

    admin_token = create_access_token(
        {"sub": "1", "email": "admin@pivota.cc", "role": "admin"}
    )
    res = client.post(
        "/api/auth/register",
        json={
            "email": "new-employee@pivota.cc",
            "password": "Hunter2hunter2",
            "role": "employee",
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, res.text
    assert [row["role"] for row in register_db] == ["employee"]


@pytest.mark.parametrize("caller_role", ["merchant", "agent", "employee", "outsourced"])
def test_non_admin_bearer_cannot_provision_a_privileged_role(
    client, register_db, caller_role
):
    """Every non-admin role must be refused, not just `merchant`.

    Testing one role let the gate widen to any authenticated caller with the
    suite still green.
    """
    from utils.auth import create_access_token

    token = create_access_token(
        {"sub": "2", "email": f"{caller_role}@example.com", "role": caller_role}
    )
    res = client.post(
        "/api/auth/register",
        json={
            "email": f"escalate-{caller_role}@example.com",
            "password": "Hunter2hunter2",
            "role": "admin",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403, res.text
    # Pin the MECHANISM: a bare 403 also matches "Not authenticated" from a
    # different dependency, so the number alone does not prove the role policy ran.
    assert "may only be granted by an authenticated admin" in res.text, res.text
    assert register_db == []


def test_a_present_but_invalid_bearer_is_rejected_not_treated_as_anonymous(
    client, register_db
):
    """`_optional_current_user` promises 401 on a bad token rather than a
    silent downgrade to the anonymous path. Nothing tested that promise."""
    for bad in ("not-a-jwt", "a.b.c"):
        res = client.post(
            "/api/auth/register",
            json={
                "email": "stale@example.com",
                "password": "Hunter2hunter2",
                "role": "merchant",  # a role anonymous callers MAY self-assign
            },
            headers={"Authorization": f"Bearer {bad}"},
        )
        assert res.status_code == 401, f"token {bad!r} -> {res.status_code} {res.text}"
        assert register_db == [], "a bad token still reached the INSERT"
        _assert_no_privileged_token(res, "register with invalid bearer")


def test_an_empty_bearer_degrades_to_anonymous_and_stays_unprivileged(
    client, register_db
):
    """`HTTPBearer(auto_error=False)` yields None for an EMPTY credential, so
    `Authorization: Bearer ` is treated as no credential at all rather than
    rejected. That is safe -- anonymous is the least-privileged path -- but it
    means the 401 above does not cover this shape, so pin the property that
    actually matters: it still cannot buy a privileged role."""
    res = client.post(
        "/api/auth/register",
        json={
            "email": "empty-bearer-admin@evil.example",
            "password": "Hunter2hunter2",
            "role": "admin",
        },
        headers={"Authorization": "Bearer "},
    )
    assert res.status_code == 403, res.text
    assert register_db == []
    _assert_no_privileged_token(res, "register with empty bearer")


def test_the_default_role_is_the_privileged_one_it_is_documented_to_be(client):
    """Pins the default. Flipping it to `admin` otherwise stayed green, and
    combined with any allowlist widening the default becomes the escalation."""
    from routes.auth import RegisterRequest

    default_role = RegisterRequest(
        email="x@example.com", password="Hunter2hunter2"
    ).role
    assert default_role == "employee"
    assert default_role in PRIVILEGED_ROLES


# ---------------------------------------------------------------------------
# 3. Keep the two parallel role constants from drifting apart
# ---------------------------------------------------------------------------

def test_self_service_and_privileged_role_sets_partition_the_valid_roles():
    """`routes.auth` now carries two independent role sets.

    `EMPLOYEE_AUTH_ROLES` arrived separately (portal/membership login) and
    `SELF_SERVICE_REGISTRATION_ROLES` gates registration. They must stay
    disjoint and jointly cover every role `_validate_role_value` accepts --
    otherwise a newly added role silently lands in neither and the split stops
    describing reality.
    """
    import inspect

    from routes.auth import (
        EMPLOYEE_AUTH_ROLES,
        SELF_SERVICE_REGISTRATION_ROLES,
        _validate_role_value,
    )

    overlap = set(SELF_SERVICE_REGISTRATION_ROLES) & set(EMPLOYEE_AUTH_ROLES)
    assert not overlap, f"a role is both self-service and privileged: {overlap}"

    # Read the accepted roles out of the validator itself rather than hardcoding
    # them, so adding a role to the validator fails this test.
    source = inspect.getsource(_validate_role_value)
    accepted = set(re.findall(r"'([a-z_]+)'", source.split("valid_roles")[1]))
    uncovered = accepted - set(SELF_SERVICE_REGISTRATION_ROLES) - set(EMPLOYEE_AUTH_ROLES)
    assert not uncovered, (
        f"role(s) {sorted(uncovered)} are in neither set; registration treats "
        "unknown roles as privileged (fail-closed), but the sets should be "
        "updated deliberately"
    )


def test_an_unknown_future_role_fails_closed(client, register_db):
    """A role in neither set must require admin, not sail through."""
    from routes.auth import _authorize_requested_role

    with pytest.raises(HTTPException) as excinfo:
        _authorize_requested_role("brand_new_privileged_role", None)
    assert excinfo.value.status_code == 403


# ---------------------------------------------------------------------------
# 4. The employee break-glass router must not be anonymous
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path,payload",
    [
        (
            "post",
            "/admin/employees/reset-password",
            {"email": "pwn@evil.example", "new_password": "Admin123!"},
        ),
        ("get", "/admin/employees/list", None),
    ],
)
def test_employee_break_glass_routes_reject_anonymous(client, method, path, payload):
    """These wrote an admin `employees` row / leaked the roster to anyone.

    They must reject BEFORE touching the database: if the dependency ran after
    the handler body, an anonymous caller would already have created the admin
    row by the time the 401 was returned.
    """
    import routes.admin_reset_employee as mod

    touched = []

    async def explode(*args, **kwargs):
        touched.append(args[0] if args else "?")
        raise AssertionError("database was reached by an anonymous caller")

    original_execute = mod.database.execute
    original_fetch_one = mod.database.fetch_one
    original_fetch_all = mod.database.fetch_all
    mod.database.execute = explode
    mod.database.fetch_one = explode
    mod.database.fetch_all = explode
    try:
        res = getattr(client, method)(path, **({"json": payload} if payload else {}))
    finally:
        mod.database.execute = original_execute
        mod.database.fetch_one = original_fetch_one
        mod.database.fetch_all = original_fetch_all

    assert res.status_code == 401, res.text
    assert touched == [], f"anonymous caller reached the DB: {touched}"
    _assert_no_privileged_token(res, f"{method.upper()} {path}")


def test_employee_break_glass_still_reachable_with_admin_key(client, monkeypatch):
    """The break-glass path must survive -- it exists for when nobody can log in."""
    import routes.admin_reset_employee as mod

    monkeypatch.setenv("ADMIN_API_KEY", "break-glass-key")

    seen = {}

    async def fake_fetch_one(query=None, values=None):
        seen["fetch_one"] = True
        return None  # employee absent -> create path

    async def fake_execute(query=None, values=None):
        seen["insert_role"] = (values or {}).get("role")
        return None

    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod.database, "execute", fake_execute)

    res = client.post(
        "/admin/employees/reset-password",
        json={"email": "ops@pivota.cc", "new_password": "Admin123!"},
        headers={"X-ADMIN-KEY": "break-glass-key"},
    )
    assert res.status_code == 200, res.text
    assert seen.get("insert_role") == "admin"


def test_break_glass_header_fails_closed_when_no_key_is_configured(client, monkeypatch):
    """With no ADMIN_API_KEY set, presenting any header must NOT authenticate."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("PROMOTIONS_ADMIN_KEY", raising=False)

    res = client.post(
        "/admin/employees/reset-password",
        json={"email": "pwn@evil.example", "new_password": "Admin123!"},
        headers={"X-ADMIN-KEY": ""},
    )
    assert res.status_code == 401, res.text
