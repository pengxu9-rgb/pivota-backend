"""Twelve /admin/* routers shipped with NO authentication of any kind.

THE DEFECT, live on prod through ad02087c5. Found while reviewing the fix for
the GET /agents/ roster leak (PR #2048): that PR closes an AUTHENTICATED read
of every agent's contact details, while `GET /admin/agents/list` returned the
same `email` column to a caller with no credentials at all. Sweeping the live
app's route table for endpoints whose dependency tree contains no auth
dependency turned up seven more on the same `/admin/*` prefix -- no `Depends`,
no header check, no role check anywhere in the module:

  READS
  * GET  /admin/agents/list              -- every agent's agent_id/name/email,
                                            unpaginated
  * GET  /admin/usage/logs/{agent_id}    -- any agent's request log
  * GET  /admin/usage/stats/{agent_id}   -- any agent's usage stats
  * GET  /admin/data/check/{merchant_id} -- any merchant's data state
  * GET  /admin/cleanup/preview-cleanup  -- what a bulk delete would remove

  WRITES -- these are the sharp ones
  * POST /admin/cleanup/all-test-data    -- anonymous bulk DELETE
  * POST /admin/merchants/reset-all      -- anonymous bulk merchant reset
  * POST /admin/merchants/canonicalize   -- anonymous merchant identity rewrite
  * POST /admin/data/fix/{merchant_id}   -- anonymous write
  * POST /admin/migrations/run/006-psp-constraints -- anonymous DDL
  * POST /admin/init/agent-test-key      -- anonymously MINTS AN API KEY

`routes/admin_orphan_orders.py` is the counter-example in the same family and
the reason this was worth checking one by one rather than by prefix: it sits on
`/admin/orders/*` and DOES authenticate, via its own `_require_internal_key`.

THE RULE, and why the guard is on the ROUTER: these modules did not forget a
role CHECK, they forgot authentication entirely, and each is a file someone
added in a hurry ("Temporary admin endpoint"). A per-handler dependency would
have to be remembered by the next person adding a route to the same file.
`APIRouter(dependencies=[...])` applies to every route the router carries,
including ones added later, so the next handler inherits the guard instead of
inheriting the bug.

The tests below assert the ANONYMOUS case only. That is deliberate: none of
these endpoints has a caller anywhere in the backend, the employee portal or
the merchants portal, so there is no positive path to preserve beyond "an admin
credential still gets in", which is asserted once per router against the guard
itself. Nothing here INVOKES a destructive endpoint -- every write assertion
checks that the request is refused, and a refusal is the only outcome these
tests ever produce.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


class _HandlerReached(Exception):
    """Raised by the tripwire in place of running the statement."""


# Every accessor a handler in these routers could reach the database through.
# Named once so the fixture and the self-test that proves it works cannot drift:
# a self-test that probed only one of these let the tuple be narrowed back to
# nothing while staying green.
_TRAPPED_ACCESSORS = ("execute", "execute_many", "fetch_all", "fetch_one", "fetch_val")


@pytest.fixture
def db_tripwire():
    """Replace every database accessor with a raise, and record the calls.

    Two jobs, and the second is why this is not optional.

    It makes the refusals below say what they mean. `assert status in (401,
    403)` is satisfied by a handler that RAN and then failed a later check;
    what this file actually claims is that the guard sits in FRONT of the
    handler. Recording the calls turns that into an assertion. Note what that
    is worth TODAY: no handler in these twelve modules returns 401/403 itself,
    so with the guards stripped every failure comes from the status assertion
    and `assert not db_tripwire` never fires. It is there for the handler that
    later does return a 403 of its own -- the interception below is the part
    carrying weight now.

    And it bounds the blast radius of its own failure. UNAUTHENTICATED_ROUTES
    contains `POST /admin/fix/agents-table`, whose first statement is
    `DROP TABLE IF EXISTS agents CASCADE`, plus ~12 ALTER TABLEs on `orders`
    and a DELETE from products_cache. Those routes are listed here precisely
    because they were once unguarded -- so the day the guard regresses is the
    day this suite sends them an anonymous request and the handler runs. That
    was measured, not assumed: with the guard removed from
    routes/fix_agents_table.py, the anonymous request these tests send executed
    `DROP TABLE IF EXISTS agents CASCADE` against the test database. sqlite
    happens to reject CASCADE, but the statement was issued, and anyone running
    the suite with DATABASE_URL pointed at Postgres would lose the table.

    A regression should fail this file, not drop a table to do it.
    """
    from db import database as db_module

    calls: List[str] = []

    def _trap(kind):
        async def _call(*args, **kwargs):
            calls.append(f"{kind}: {str(args[0])[:120] if args else ''}")
            raise _HandlerReached(kind)

        return _call

    # --- Finding 2 from review: monkeypatch.setattr on an INSTANCE undoes with
    # setattr, not delattr, so every accessor would be left as a permanent
    # instance attribute shadowing the class. Harmless for direct calls, but it
    # would silently defeat a future test that patches
    # databases.Database.<accessor> at the CLASS level. Restore by hand.
    had = {a: (a in vars(db_module.database)) for a in _TRAPPED_ACCESSORS}
    originals = {a: getattr(db_module.database, a) for a in _TRAPPED_ACCESSORS}
    for accessor in _TRAPPED_ACCESSORS:
        setattr(db_module.database, accessor, _trap(accessor))

    yield calls

    for accessor in _TRAPPED_ACCESSORS:
        if had[accessor]:
            setattr(db_module.database, accessor, originals[accessor])
        else:
            db_module.database.__dict__.pop(accessor, None)


# (method, path, json body or None). Paths are the real ones; the bodies are
# only enough to get past request validation, because a 422 would mean the
# refusal was never reached.
UNAUTHENTICATED_ROUTES: List[Tuple[str, str, Any]] = [
    ("GET", "/admin/agents/list", None),
    ("GET", "/admin/usage/logs/agent_probe", None),
    ("GET", "/admin/usage/stats/agent_probe", None),
    ("GET", "/admin/data/check/merchant_probe", None),
    ("POST", "/admin/data/fix/merchant_probe", None),
    ("GET", "/admin/cleanup/preview-cleanup", None),
    ("POST", "/admin/cleanup/all-test-data", {"confirm": "NO"}),
    ("POST", "/admin/merchants/reset-all", {"confirm": "NO"}),
    ("POST", "/admin/merchants/canonicalize", {"canonical_merchant_id": "merchant_probe"}),
    ("POST", "/admin/migrations/run/006-psp-constraints", None),
    ("GET", "/admin/migrations/status/006-psp-constraints", None),
    ("POST", "/admin/init/agent-test-key", None),
    # Folded in after a review of this PR ran the sweep again and found the
    # first pass had stopped at the routers whose SQL mentioned a sensitive
    # table by name. These four do not -- they DROP, ALTER and DELETE.
    ("POST", "/admin/fix/agents-table", None),
    ("POST", "/admin/fix/orders-table-columns", None),
    ("GET", "/admin/fix/orders-table-info", None),
    ("POST", "/admin/products/compact/merchant_probe", None),
    ("GET", "/admin/shopify/health/merchant_probe", None),
]


def _call(client: TestClient, method: str, path: str, body: Any, headers=None):
    kwargs: Dict[str, Any] = {"headers": headers or {}}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, path, **kwargs)


@pytest.mark.parametrize("method,path,body", UNAUTHENTICATED_ROUTES)
def test_admin_ops_routes_refuse_an_anonymous_caller(client, db_tripwire, method, path, body):
    """No Authorization header, no X-ADMIN-KEY. Must be refused."""
    resp = _call(client, method, path, body)

    assert resp.status_code in (401, 403), (
        f"{method} {path} answered {resp.status_code} to an unauthenticated "
        f"caller: {resp.text[:400]}"
    )
    assert not db_tripwire, (
        f"{method} {path} answered {resp.status_code} but the handler still "
        f"reached the database -- the guard is not in FRONT of it: {db_tripwire}"
    )


@pytest.mark.parametrize("method,path,body", UNAUTHENTICATED_ROUTES)
def test_admin_ops_routes_refuse_a_non_admin_token(client, db_tripwire, method, path, body):
    """A real signed token for a non-admin role is not a way in either.

    Real JWTs throughout -- never the `test-token` placeholder, whose pytest
    bypass in utils.auth returns role=admin and would make every refusal here
    vacuous.
    """
    from utils.auth import create_access_token

    token = create_access_token(
        {"sub": "u-merchant", "email": "m@example.com", "role": "merchant",
         "merchant_id": "merchant_probe"}
    )
    resp = _call(client, method, path, body, {"Authorization": f"Bearer {token}"})

    assert resp.status_code in (401, 403), (
        f"{method} {path} admitted a merchant token: {resp.status_code} "
        f"{resp.text[:400]}"
    )
    assert not db_tripwire, (
        f"{method} {path} reached the database with a merchant token: {db_tripwire}"
    )


@pytest.mark.parametrize("method,path,body", UNAUTHENTICATED_ROUTES)
def test_admin_ops_routes_refuse_a_bogus_admin_key(client, db_tripwire, method, path, body):
    """An X-ADMIN-KEY that is not the configured one must not pass.

    Kills a guard that merely checks the header is PRESENT.
    """
    resp = _call(client, method, path, body, {"X-ADMIN-KEY": "not-the-key"})

    assert resp.status_code in (401, 403), (
        f"{method} {path} accepted a bogus admin key: {resp.status_code} "
        f"{resp.text[:400]}"
    )
    assert not db_tripwire, (
        f"{method} {path} reached the database with a bogus admin key: {db_tripwire}"
    )


def _database_identity(module_names):
    """(how many of these modules expose `database`, which hold a DIFFERENT one).

    The tripwire patches attributes on the db.database singleton. Route modules
    do `from db.database import database`, so they hold that same object by
    reference and see the patch. A module that somehow held its own Database
    would not -- and the refusal tests would read "handler never ran" off an
    empty list that only means "the tripwire was never in the way".
    """
    import importlib

    from db import database as db_module

    checked = 0
    strangers = set()
    for module_name in module_names:
        module = importlib.import_module(module_name)
        if not hasattr(module, "database"):
            continue  # not every guarded module talks to the DB
        checked += 1
        if module.database is not db_module.database:
            strangers.add(module_name)
    return checked, strangers


async def test_the_tripwire_lands_on_the_object_the_routes_use(db_tripwire):
    """The mechanism the three refusal tests now rest on, pinned separately.

    Those tests read "the handler never ran" off `db_tripwire` being EMPTY --
    and an empty list is also what a tripwire that patched nothing produces. So
    a broken tripwire (a renamed accessor, a second Database instance, a module
    that rebound the name) reads exactly like a perfectly guarded route, which
    is the failure mode that matters: it would restore the silent
    DROP-on-regression this fixture exists to prevent, while every test stayed
    green.

    Asserting it needs no request. The route modules all do `from db.database
    import database`, so they hold the singleton by reference and a patch on
    its attributes is visible through every alias -- that identity is the whole
    mechanism, and it is checked directly here. Deliberately independent of the
    guard, so a failure here means the tripwire is broken rather than the
    authentication.
    """
    from db import database as db_module

    checked, strangers = _database_identity(_GUARDED_MODULES)

    assert not strangers, (
        f"these modules hold a different Database object than the one the "
        f"tripwire patches -- the `assert not db_tripwire` in every refusal "
        f"test above is vacuous for them: {sorted(strangers)}"
    )
    assert checked, "no guarded module exposes `database` -- identity check was vacuous"

    # The requirement is stated HERE as a literal, deliberately duplicating
    # _TRAPPED_ACCESSORS rather than iterating it. Iterating the fixture's own
    # tuple made this test narrow in lockstep with the thing it polices:
    # dropping `execute` from the tuple left it green, and `execute` carries
    # every DROP/ALTER/DELETE in this route set. A test whose expectation is
    # read out of the code under test can only ever agree with it.
    must_trap = {"execute", "execute_many", "fetch_all", "fetch_one", "fetch_val"}
    assert must_trap <= set(_TRAPPED_ACCESSORS), (
        f"the tripwire stopped trapping {sorted(must_trap - set(_TRAPPED_ACCESSORS))} "
        f"-- a handler reaching the database through those would leave "
        f"db_tripwire empty, which every refusal test reads as 'never ran'"
    )

    for accessor in sorted(must_trap):
        before = len(db_tripwire)
        with pytest.raises(_HandlerReached):
            await getattr(db_module.database, accessor)("SELECT 1")
        assert len(db_tripwire) == before + 1, (
            f"database.{accessor} is not trapped -- a handler reaching the DB "
            f"through it would leave db_tripwire empty, which every refusal "
            f"test above reads as 'the handler never ran'"
        )


def test_the_identity_check_can_actually_fail():
    """Proves the check above detects what it claims to.

    Every guarded module really does share the singleton, so the assertion in
    `test_the_tripwire_lands_on_the_object_the_routes_use` is true whether or
    not it is computed correctly -- replacing it with `assert True` changes
    nothing observable, and the tripwire's silent failure mode comes back. A
    synthetic module holding a DIFFERENT object must be reported, and one
    holding the real singleton must not.
    """
    import sys
    import types

    from db import database as db_module

    stranger = types.ModuleType("_probe_stranger_database")
    stranger.database = object()  # a second Database instance, in effect
    native = types.ModuleType("_probe_native_database")
    native.database = db_module.database
    silent = types.ModuleType("_probe_no_database")  # talks to no DB at all

    for module in (stranger, native, silent):
        sys.modules[module.__name__] = module
    try:
        checked, strangers = _database_identity(
            (stranger.__name__, native.__name__, silent.__name__)
        )
    finally:
        for module in (stranger, native, silent):
            sys.modules.pop(module.__name__, None)

    assert stranger.__name__ in strangers, (
        "the identity check did not flag a module holding a different Database "
        "object -- it cannot fail, so the assertion it guards proves nothing"
    )
    assert native.__name__ not in strangers, (
        "the identity check flags a module that DOES share the singleton"
    )
    assert checked == 2, (
        f"expected the two modules exposing `database` to be counted, got {checked}"
    )


def test_the_refusal_is_the_guard_not_a_missing_table(client):
    """The read routes 500 or return an error body when the agents/merchant
    tables are absent, which is the state of the local sqlite fixture. If a
    refusal above were really a table error the assertions would be measuring
    the wrong thing, so pin the one route whose pre-fix behaviour was observed
    directly: GET /admin/agents/list answered 200 with a JSON error body
    (`no such table: agents`) BEFORE this change, and must answer 401/403 now.
    """
    resp = client.get("/admin/agents/list")

    assert resp.status_code in (401, 403), resp.text
    assert "no such table" not in resp.text, (
        "the handler still ran -- the guard is not in front of it"
    )


# ---------------------------------------------------------------------------
# The guard is on the ROUTER, so a route added to these files later inherits it.
# ---------------------------------------------------------------------------

_GUARDED_MODULES = (
    "routes.fix_agents_table",
    "routes.fix_orders_table",
    "routes.products_cache_maintenance",
    "routes.admin_shopify_health",
    "routes.admin_agents_debug",
    "routes.admin_usage_debug",
    "routes.admin_data_consistency",
    "routes.admin_cleanup_all_test_data",
    "routes.admin_merchant_reset",
    "routes.admin_merchant_canonicalize",
    "routes.admin_run_migration",
    "routes.init_agent_key",
)


@pytest.mark.parametrize("module_name", _GUARDED_MODULES)
def test_every_route_on_these_routers_carries_the_guard(module_name):
    """Asserted against the router's OWN dependency list, per route, so a new
    handler in one of these files cannot quietly ship unauthenticated."""
    import importlib

    from utils.auth import require_admin_or_key

    module = importlib.import_module(module_name)
    routes = [r for r in module.router.routes if hasattr(r, "dependant")]
    assert routes, f"{module_name} exposes no routes -- test would be vacuous"

    for route in routes:
        calls = {
            getattr(d.call, "__name__", None)
            for d in route.dependant.dependencies
        }
        assert require_admin_or_key.__name__ in calls, (
            f"{module_name} {route.path} has no admin guard"
        )


# ---------------------------------------------------------------------------
# The positive path, and the ratchet over the WHOLE route table.
# ---------------------------------------------------------------------------


def test_an_admin_credential_still_reaches_these_routes(client, db_tripwire):
    """The counterpart the three negative tests above need.

    Without it, every assertion in this file stays green if these routes later
    become deny-all -- refusing everyone passes a refusal test. Uses the least
    destructive route in the set: GET /admin/migrations/status/006-psp-
    constraints only reads.

    Both credential shapes are exercised. The X-ADMIN-KEY path matters
    separately because utils.auth.optional_security is HTTPBearer(auto_error=
    False): a request carrying the key and NO Authorization header must still
    get through, which a guard built on plain HTTPBearer would have broken.
    """
    from utils.auth import create_access_token

    path = "/admin/migrations/status/006-psp-constraints"

    for role in ("admin", "super_admin"):
        token = create_access_token(
            {"sub": f"u-{role}", "email": f"{role}@example.com", "role": role}
        )
        db_tripwire.clear()
        resp = client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code not in (401, 403), (
            f"{role} was refused its own admin route: {resp.status_code} "
            f"{resp.text[:300]}"
        )
        # `not in (401, 403)` on its own is satisfied by a 500, so it proves
        # only that the guard did not refuse -- not that it let anything
        # through. The tripwire settles it: the handler ran far enough to query.
        assert db_tripwire, (
            f"{role} was not refused, but the handler never reached the "
            f"database either -- this does not show the guard admitted anyone"
        )


def test_a_configured_admin_key_is_admitted_without_a_bearer_header(client, monkeypatch, db_tripwire):
    """The header-only path, pinned because it is the one a guard built on a
    non-optional HTTPBearer would silently break."""
    monkeypatch.setenv("ADMIN_API_KEY", "the-configured-key")

    resp = client.get(
        "/admin/migrations/status/006-psp-constraints",
        headers={"X-ADMIN-KEY": "the-configured-key"},
    )

    assert resp.status_code not in (401, 403), (
        f"a correct X-ADMIN-KEY with no Authorization header was refused: "
        f"{resp.status_code} {resp.text[:300]}"
    )
    assert db_tripwire, (
        "the key was not refused, but the handler never reached the database "
        "-- this does not show the header-only path admits anyone"
    )


# Routes under /admin that legitimately carry no auth DEPENDENCY. Each needs a
# reason, because this allowlist is the only thing standing between a new
# unauthenticated admin route and production.
_ADMIN_ROUTES_WITHOUT_AN_AUTH_DEPENDENCY = {
    # Hard-404s unconditionally; there is no handler behind it to protect.
    ("POST", "/admin/sql/execute"),
    # Authenticates INSIDE the handler via its own _require_internal_key, not
    # through a dependency. Correct, just invisible to a dependency-tree sweep.
    ("POST", "/admin/orders/cleanup-orphaned-link-orders"),
}

_AUTHENTICATING_CALLABLES = {
    "get_current_user", "get_current_employee", "get_current_admin",
    "require_admin", "require_admin_or_key", "require_permission",
    "get_current_merchant", "get_agent_context", "verify_jwt_token",
    # Module-local guards that authenticate through a real dependency rather
    # than a shared helper. routes/admin_sync_refresh_presence.py's
    # require_sync_admin takes a MANDATORY X-ADMIN-KEY header and refuses when
    # the env var is unset, so it fails closed; it is authentication, just not
    # spelled with one of the names above.
    "require_sync_admin",
}


def _unauthenticated_admin_routes(app) -> set:
    """Every ("METHOD", "/admin/...") on `app` with no authenticating
    dependency anywhere in its dependency tree."""
    from fastapi.routing import APIRoute

    def authenticates(route: APIRoute) -> bool:
        stack = list(route.dependant.dependencies)
        while stack:
            dep = stack.pop()
            name = getattr(getattr(dep, "call", None), "__name__", "")
            if name in _AUTHENTICATING_CALLABLES:
                return True
            stack.extend(dep.dependencies)
        return False

    found = set()
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/admin"):
            continue
        if authenticates(route):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            found.add((method, route.path))
    return found


def _leaked_admin_routes(app) -> set:
    return _unauthenticated_admin_routes(app) - _ADMIN_ROUTES_WITHOUT_AN_AUTH_DEPENDENCY


def test_the_ratchet_can_actually_fail(client):
    """Proves the sweep below detects what it claims to.

    Without this, every line of the ratchet could be replaced by `set()` and
    the file would stay green -- an assertion that cannot fail is the default
    failure mode for a check like this. A synthetic app with one deliberately
    unguarded /admin route must come back flagged, and a guarded sibling must
    not.
    """
    from fastapi import Depends, FastAPI

    from utils.auth import require_admin_or_key

    probe = FastAPI()

    @probe.get("/admin/deliberately-open")
    async def _open():  # pragma: no cover - never called
        return {}

    @probe.get("/admin/deliberately-guarded", dependencies=[Depends(require_admin_or_key)])
    async def _guarded():  # pragma: no cover - never called
        return {}

    async def _pagination(limit: int = 10):  # a dependency, but not authentication
        return limit

    @probe.get("/admin/dependency-but-not-auth", dependencies=[Depends(_pagination)])
    async def _decorated():  # pragma: no cover - never called
        return {}

    leaked = _leaked_admin_routes(probe)

    assert ("GET", "/admin/dependency-but-not-auth") in leaked, (
        "the sweep counted a non-auth dependency as authentication -- HAVING a "
        "dependency is not the same as having an AUTH one, and conflating them "
        "would blind the ratchet to every /admin route that takes a query param"
    )
    assert ("GET", "/admin/deliberately-open") in leaked, (
        "the sweep did not flag an unguarded /admin route -- it cannot fail, "
        "so the assertion below proves nothing"
    )
    assert ("GET", "/admin/deliberately-guarded") not in leaked, (
        "the sweep flags a route that IS guarded -- it would fail on correct code"
    )


def test_no_admin_route_is_reachable_without_an_auth_dependency():
    """The ratchet that would have caught this whole class.

    The per-module test above only covers the twelve files already fixed -- it
    cannot see a thirteenth hurried /admin file, which is exactly how the first
    pass of this PR shipped with `POST /admin/fix/agents-table` (an anonymous
    `DROP TABLE IF EXISTS agents CASCADE`) still open. This sweeps the ENTIRE
    mounted route table instead, so a new unauthenticated /admin route fails
    here rather than in production.

    A new entry in the allowlist above is a deliberate act that needs a reason
    written next to it. Adding one to make this test pass is the bug.
    """
    import main

    assert _unauthenticated_admin_routes(main.app), (
        "no unguarded /admin routes found at all -- the two allowlisted ones "
        "should always be here, so the sweep is not running"
    )

    leaked = _leaked_admin_routes(main.app)
    assert not leaked, (
        "these /admin routes are reachable with no authentication dependency:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in sorted(leaked))
    )


def test_the_allowlist_does_not_carry_stale_entries():
    """An allowlist claims completeness in both directions. An entry for a
    route that no longer exists, or that has since grown a real dependency,
    would quietly widen what the ratchet above tolerates."""
    import main
    from fastapi.routing import APIRoute

    mounted = {
        (m, r.path)
        for r in main.app.routes
        if isinstance(r, APIRoute)
        for m in r.methods - {"HEAD", "OPTIONS"}
    }
    stale = _ADMIN_ROUTES_WITHOUT_AN_AUTH_DEPENDENCY - mounted
    assert not stale, f"allowlist entries for routes that do not exist: {stale}"
