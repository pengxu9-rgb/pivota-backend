"""Every route keyed on a tenant id in its PATH must prove the caller owns it.

Sweeps the mounted app: each route with a `{merchant_id}` or `{agent_id}`
path parameter must either

  1. call the shared guard from utils/tenant_scope.py on that parameter
     (assert_merchant_scope / assert_agent_scope / assert_agent_scope_or_owner
     / assert_merchant_agent_relationship -- the async ones awaited), or
  2. depend on a staff-only / key-only gate listed in STAFF_ONLY_DEPENDENCIES,
     so no merchant or agent token can reach it at all, or
  3. sit in ALLOWLIST with a one-line reason.

A new route that does none of these fails here. That is the point: a role gate
("merchant" may call this) was the whole defect on the merchant dashboard,
order, payout, bank-details and onboarding routes, and only a sweep catches the
next one.

A second test calls every guarded endpoint directly with a FOREIGN merchant /
agent token and asserts a 403 before the endpoint's module touches its
database -- so a guard placed after the read, or on the wrong variable, fails
too.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

MERCHANT_GUARDS = {"assert_merchant_scope", "assert_merchant_agent_relationship"}
AGENT_GUARDS = {"assert_agent_scope", "assert_agent_scope_or_owner", "assert_merchant_agent_relationship"}
ASYNC_GUARDS = {"assert_agent_scope_or_owner", "assert_merchant_agent_relationship"}
ALL_GUARDS = MERCHANT_GUARDS | AGENT_GUARDS

# Dependencies that refuse every merchant and agent token (staff JWT, or an
# operator key header). A route depending on one of these is staff-only.
STAFF_ONLY_DEPENDENCIES: Dict[str, str] = {
    "utils.auth.require_admin": "admin/super_admin JWT",
    "utils.auth.get_current_admin": "admin/super_admin JWT",
    "utils.auth.get_current_employee": "employee-membership JWT",
    "utils.auth.require_admin_or_key": "admin JWT or ADMIN_API_KEY",
    "routes.admin_catalog_debug.require_admin_key": "X-ADMIN-KEY header",
    "routes.admin_audit_backfill.require_admin_key": "X-ADMIN-KEY header",
    "routes.admin_sync_refresh_presence.require_sync_admin": "X-ADMIN-KEY header",
    "routes.shopify_products_sync_api.require_shopify_products_admin": "X-ADMIN-KEY header",
    "routes.platform_products_sync_api.require_platform_products_admin": "X-ADMIN-KEY header",
    "routes.shakeout_debug._gate": "non-prod only + X-Shakeout-Token",
    "routes.auth_routes.require_admin": "admin JWT (legacy copy of require_admin)",
}

# (method, path) -> why this route does not call the shared guard.
ALLOWLIST: Dict[Tuple[str, str], str] = {
    # Retired: unconditional 410, no data read or written.
    ("GET", "/merchants/{merchant_id}/payouts/pending-commissions"): "retired 410 stub, reads nothing",
    ("POST", "/merchants/{merchant_id}/payouts/generate-from-commissions"): "retired 410 stub, writes nothing",
    ("POST", "/merchants/{merchant_id}/commission/offers"): "retired 410 stub behind an owner-only check",
    ("GET", "/merchants/{merchant_id}/commission/offers"): "retired 410 stub behind an owner-only check",
    ("DELETE", "/merchants/{merchant_id}/commission/offers/{offer_id}"): "retired 410 stub behind an owner-only check",
    ("GET", "/agents/{agent_id}/settlements"): "retired 410 stub, reads nothing",
    ("POST", "/agents/{agent_id}/settlements"): "retired 410 stub, writes nothing",
    ("GET", "/agents/{agent_id}/settlements/pending"): "retired 410 stub, reads nothing",
    ("POST", "/agents/{agent_id}/settlements/calculate"): "retired 410 stub, writes nothing",
    ("GET", "/agents/{agent_id}/settlements/payouts"): "retired 410 stub, reads nothing",
    ("GET", "/agents/{agent_id}/settlements/{settlement_id}"): "retired 410 stub, reads nothing",
    # Not JWT routes: the caller is a platform, an agent API key or an operator key.
    ("POST", "/webhooks/shopify/{merchant_id}"): "Shopify webhook, authenticated by HMAC signature",
    ("POST", "/agents/{agent_id}/webhooks/managed-inbox"): "managed webhook inbox, authenticated by its signing secret",
    ("GET", "/internal/store-audit/commerce-probes/capabilities/{merchant_id}"): "X-Internal-Key + flag gated",
    ("POST", "/payment/internal/canary/merchants/{merchant_id}/execute"): "internal canary key gated",
    ("POST", "/payment/internal/canary/merchants/{merchant_id}/order-backed/execute"): "internal canary key gated",
    ("POST", "/payment/internal/canary/merchants/{merchant_id}/orders/{order_id}/cancel"): "internal canary key gated",
    ("GET", "/agent/v1/products/{merchant_id}/{product_id}"): "agent API-key catalog read, scoped by AgentContext.can_access_merchant",
    ("GET", "/agent/v1/products/merchants/{merchant_id}"): "agent API-key catalog read, scoped by AgentContext.can_access_merchant",
    ("GET", "/agent/v1/products/merchants/{merchant_id}/product/{product_id}"): "agent API-key catalog read, scoped by AgentContext.can_access_merchant",
    ("GET", "/agent/v1/products/merchants/{merchant_id}/variant/{variant_id}"): "agent API-key catalog read, scoped by AgentContext.can_access_merchant",
    ("GET", "/agent/v1/products/merchants/{merchant_id}/product/{product_id}/related"): "agent API-key catalog read, scoped by AgentContext.can_access_merchant",
    # Own reviewed ownership helper with semantics the shared guard does not have.
    ("POST", "/v1/catalog/reconcile/merchants/{merchant_id}"): "_require_catalog_scope: normalizes the id and keeps merch_obs_* admin-only",
    ("POST", "/v1/catalog/verticals/beauty/rebuild/{merchant_id}"): "_require_catalog_scope: normalizes the id and keeps merch_obs_* admin-only",
    ("POST", "/v1/catalog/incentives/reconcile/{merchant_id}"): "_require_catalog_scope: normalizes the id and keeps merch_obs_* admin-only",
    # agent_management's inline checks: employee staff, or an `agent` token whose own
    # agent_id claim is the path id. No email arm (owner_email or email == id) and no
    # user_id arm; pinned by tests/test_auth_takeover_followups.py.
    ("GET", "/agents/{agent_id}"): "staff, or _is_own_agent_record (agent token's agent_id claim only), agent_management",
    ("PUT", "/agents/{agent_id}"): "staff, or agent_id claim == path id, plus field-level self-escalation refusal, agent_management",
    ("GET", "/agents/{agent_id}/funnel"): "read-only: staff, or agent_id claim == path id (no email arm), agent_management",
    ("GET", "/agents/{agent_id}/query-analytics"): "read-only: staff, or agent_id claim == path id (no email arm), agent_management",
    ("GET", "/agents/{agent_id}/merchants"): "read-only: staff, or agent_id claim == path id (no email arm), agent_management",
    # Pre-existing fail-closed lockout: compares user_id (a users-table id) with the
    # agent id, so only admins pass. Admitting agent owners is a separate, reviewed
    # decision pinned by tests/test_admin_route_token_and_role_gate.py.
    ("POST", "/agents/{agent_id}/payments/route"): "admin-only in effect (user_id lockout), widening needs its own review",
    ("GET", "/agents/{agent_id}/routes"): "admin-only in effect (user_id lockout), widening needs its own review",
    ("POST", "/agents/{agent_id}/routes"): "admin-only in effect (user_id lockout), widening needs its own review",
    ("PUT", "/agents/{agent_id}/routes/{route_id}"): "admin-only in effect (user_id lockout), widening needs its own review",
    ("DELETE", "/agents/{agent_id}/routes/{route_id}"): "admin-only in effect (user_id lockout), widening needs its own review",
    ("POST", "/agents/{agent_id}/protocols/{protocol_name}/test"): "admin-only in effect (user_id lockout), widening needs its own review",
    # Staff-only by an inline role check (no tenant token passes it).
    ("GET", "/agents/{agent_id}/analytics"): "read-only: admin/employee/super_admin, or agent_id claim == path id (no email arm), agent_management",
    ("GET", "/agents/{agent_id}/usage"): "read-only: admin/employee/super_admin, or agent_id claim == path id (no email arm), agent_management",
    ("PATCH", "/agents/{agent_id}/tier"): "inline staff-only role check",
    ("POST", "/agents/{agent_id}/revenue/policies"): "inline staff-only role check",
    ("POST", "/agents/{agent_id}/protocols/"): "inline admin-only role check",
    ("DELETE", "/agents/{agent_id}/protocols/{protocol_name}"): "inline admin-only role check",
    ("GET", "/admin/governance/agents/{agent_id}/governance-history"): "inline admin-only role check",
    ("POST", "/admin/seed/agent-routing-history/{agent_id}"): "inline admin-only role check",
    ("GET", "/admin/debug-mcp/merchant/{merchant_id}/full-data"): "inline admin-only role check",
    ("GET", "/merchant/onboarding/kyb/{merchant_id}/documents"): "inline employee-staff role check",
    ("POST", "/merchant/onboarding/restore/{merchant_id}"): "inline employee-staff role check",
    ("GET", "/employee/agents/{agent_id}"): "inline employee-staff role check",
    ("GET", "/employee/agents/{agent_id}/calls"): "inline employee-staff role check",
    ("PATCH", "/employee/agents/{agent_id}/tier"): "inline employee-staff role check",
    ("POST", "/employee/agents/{agent_id}/reset-api-key"): "inline employee-staff role check",
    ("POST", "/employee/agents/{agent_id}/deactivate"): "inline employee-staff role check",
    ("POST", "/employee/agents/{agent_id}/reactivate"): "inline employee-staff role check",
    ("POST", "/employee/agents/{agent_id}/activate"): "inline employee-staff role check",
    ("GET", "/employee/agents/{agent_id}/api-keys"): "inline employee-staff role check",
    ("POST", "/employee/agents/{agent_id}/api-keys"): "inline employee-staff role check",
    ("DELETE", "/employee/agents/{agent_id}/api-keys/{key_id}"): "inline employee-staff role check",
    ("POST", "/employee/agents/{agent_id}/api-keys/{key_id}/rotate"): "inline employee-staff role check",
    ("GET", "/employee/agents/{agent_id}/protocols"): "inline employee-staff role check",
    ("POST", "/employee/agents/{agent_id}/protocols"): "inline employee-staff role check",
    ("PUT", "/employee/agents/{agent_id}/protocols/{protocol_id}"): "inline employee-staff role check",
    ("GET", "/employee/agents/{agent_id}/performance"): "inline employee-staff role check",
    ("GET", "/employee/agents/{agent_id}/metrics-history"): "inline employee-staff role check",
    ("GET", "/employee/agents/{agent_id}/alerts"): "inline employee-staff role check",
    ("GET", "/employee/agents/{agent_id}/health-score"): "inline employee-staff role check",
}

TENANT_PARAMS = ("merchant_id", "agent_id", "onboarding_id")
# Path params that name a merchant. /platform-onboarding/{onboarding_id}/* uses the
# onboarding id AS the merchant id (routes/platform_onboarding_routes.py).
MERCHANT_KEYED_PARAMS = {"merchant_id", "onboarding_id"}


@pytest.fixture(scope="module")
def app():
    import main

    return main.app


def _tenant_params(path: str) -> List[str]:
    return [p for p in TENANT_PARAMS if "{" + p + "}" in path or "{" + p + ":" in path]


def _dependency_names(dependant) -> Set[str]:
    out: Set[str] = set()
    for sub in dependant.dependencies:
        call = sub.call
        name = f"{getattr(call, '__module__', '')}.{getattr(call, '__qualname__', '')}"
        out.add(name)
        out |= _dependency_names(sub)
    return out


def _tenant_routes(app) -> List[APIRoute]:
    return [
        r for r in app.routes
        if isinstance(r, APIRoute) and _tenant_params(r.path)
    ]


def _guard_calls(fn) -> List[Tuple[str, bool, Set[str]]]:
    """(guard name, awaited?, names passed to it) for every guard call in fn's body."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    awaited: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            awaited.add(id(node.value))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in ALL_GUARDS:
                names = {
                    a.id for a in list(node.args) + [k.value for k in node.keywords]
                    if isinstance(a, ast.Name)
                }
                calls.append((name, id(node) in awaited, names))
    return calls


def _classify(route: APIRoute) -> Tuple[str, str]:
    deps = _dependency_names(route.dependant)
    staff = sorted(d for d in deps if d in STAFF_ONLY_DEPENDENCIES)
    if staff:
        return "staff_dependency", staff[0]
    methods = sorted(route.methods or [])
    for m in methods:
        if (m, route.path) in ALLOWLIST:
            return "allowlist", ALLOWLIST[(m, route.path)]
    calls = _guard_calls(route.endpoint)
    if not calls:
        return "unguarded", ""
    problems = []
    for name, is_awaited, _ in calls:
        if name in ASYNC_GUARDS and not is_awaited:
            problems.append(f"{name} is not awaited (a bare coroutine checks nothing)")
    for param in _tenant_params(route.path):
        wanted = MERCHANT_GUARDS if param in MERCHANT_KEYED_PARAMS else AGENT_GUARDS
        if not any(name in wanted and param in names for name, _, names in calls):
            problems.append(f"no {sorted(wanted)} call on path param {param!r}")
    if problems:
        return "misguarded", "; ".join(problems)
    return "guarded", ",".join(sorted({c[0] for c in calls}))


def test_every_tenant_path_route_runs_the_shared_scope_guard(app):
    failures = []
    for route in _tenant_routes(app):
        kind, detail = _classify(route)
        if kind in ("unguarded", "misguarded"):
            src = inspect.getsourcefile(route.endpoint)
            line = inspect.getsourcelines(route.endpoint)[1]
            failures.append(
                f"{','.join(sorted(route.methods))} {route.path} ({src}:{line}): "
                f"{kind} {detail}".rstrip()
            )
    assert not failures, (
        "Routes taking {merchant_id}/{agent_id} from the path without the shared "
        "ownership guard (utils/tenant_scope.py). Call the guard first in the "
        "handler, or -- only if the route is not a tenant route -- add it to "
        "ALLOWLIST with a reason:\n  " + "\n  ".join(failures)
    )


def test_the_sweep_actually_sees_the_routes_it_was_written_for(app):
    """A sweep that silently matched nothing would pass forever."""
    kinds: Dict[Tuple[str, str], str] = {}
    for route in _tenant_routes(app):
        for m in route.methods:
            kinds[(m, route.path)] = _classify(route)[0]
    for key in [
        ("GET", "/orders/merchant/{merchant_id}"),
        ("GET", "/merchant/{merchant_id}/orders"),
        ("GET", "/merchant/{merchant_id}/integrations"),
        ("GET", "/merchant/{merchant_id}/psps"),
        ("GET", "/merchant/{merchant_id}/analytics"),
        ("GET", "/merchants/{merchant_id}/agents/{agent_id}/bank-details"),
        ("GET", "/merchants/{merchant_id}/payouts"),
        ("POST", "/merchants/{merchant_id}/payouts/bulk"),
        ("POST", "/merchants/{merchant_id}/payouts/{payout_id}/upload"),
        ("GET", "/merchant/onboarding/details/{merchant_id}"),
        ("GET", "/merchant/onboarding/status/{merchant_id}"),
        ("POST", "/merchant/onboarding/upload/{merchant_id}"),
        ("POST", "/merchant/onboarding/kyc/upload/file/{merchant_id}"),
        ("GET", "/agents/{agent_id}/bank"),
        ("GET", "/agents/{agent_id}/payouts"),
    ]:
        assert kinds.get(key) == "guarded", (key, kinds.get(key))


def test_allowlist_has_no_stale_or_shadowed_entries(app):
    mounted: Dict[Tuple[str, str], APIRoute] = {}
    for route in _tenant_routes(app):
        for m in route.methods:
            mounted[(m, route.path)] = route
    stale = [k for k in ALLOWLIST if k not in mounted]
    assert not stale, f"ALLOWLIST entries for routes that are not mounted: {stale}"
    shadowed = [k for k in ALLOWLIST if _guard_calls(mounted[k].endpoint)]
    assert not shadowed, f"ALLOWLIST entries for routes that now call the guard: {shadowed}"
    for key, reason in ALLOWLIST.items():
        assert reason.strip(), key


# ---------------------------------------------------------------------------
# Every guarded endpoint refuses a foreign tenant BEFORE touching its database
# ---------------------------------------------------------------------------

VICTIM_MERCHANT = "merch_victim"
VICTIM_AGENT = "agent_victim"


class _DbTripwire:
    """Stands in for the endpoint module's `database`.

    The only query a refusal may run is the guard's own merchant-agent
    relationship lookup (answered "none"). The agent guard no longer looks the
    owner email up, so that lookup now fails the test like any other read.
    """

    def __init__(self) -> None:
        self.queries: List[str] = []

    def _record(self, query: Any) -> str:
        q = " ".join(str(query).split())
        self.queries.append(q)
        return q

    async def fetch_one(self, query: Any, values: Any = None, *a: Any, **kw: Any):
        q = self._record(query)
        if "SELECT 1 AS present FROM orders" in q:
            return None
        raise AssertionError(f"endpoint queried before refusing a foreign tenant: {q}")

    async def _boom(self, query: Any = None, *a: Any, **kw: Any):
        raise AssertionError(
            f"endpoint queried before refusing a foreign tenant: {self._record(query)}"
        )

    fetch_all = fetch_val = execute = execute_many = _boom


def _guard_query_only(queries: List[str]) -> bool:
    return all("SELECT 1 AS present FROM orders" in q for q in queries)


def _guarded_routes(app) -> List[APIRoute]:
    """Every tenant route that is neither staff-gated nor allowlisted.

    Deliberately NOT "routes the static check classified as guarded": a route
    whose guard was deleted must still be exercised here, so this test fails on
    behaviour and not only on the source check above.
    """
    seen: Set[Any] = set()
    out = []
    for route in _tenant_routes(app):
        if _classify(route)[0] in ("staff_dependency", "allowlist") or route.endpoint in seen:
            continue
        seen.add(route.endpoint)
        out.append(route)
    return out


def _foreign_callers(path: str) -> List[Tuple[str, Dict[str, Any]]]:
    callers = [
        ("other_merchant", {"sub": "u-a", "email": "a@example.com", "role": "merchant",
                            "membership_type": "merchant", "merchant_id": "merch_attacker"}),
        ("other_agent", {"sub": "u-b", "email": "b@example.com", "role": "agent",
                         "membership_type": "agent", "agent_id": "agent_attacker"}),
        # A merchant membership is never staff, whatever role string it carries.
        ("merchant_membership_with_admin_role", {"sub": "u-d", "email": "d@example.com", "role": "admin",
                                                 "membership_type": "merchant", "merchant_id": "merch_attacker"}),
        # A claim-less agent token is no agent, even with the victim agent's
        # owner email (the guard does not look it up any more).
        ("claimless_agent_with_owner_email", {"sub": "u-e", "email": "someone-else@example.com",
                                              "role": "agent"}),
    ]
    return callers


def _call_kwargs(fn, user: Dict[str, Any]) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name in MERCHANT_KEYED_PARAMS:
            kwargs[name] = VICTIM_MERCHANT
        elif name == "agent_id":
            kwargs[name] = VICTIM_AGENT
        elif name in ("current_user", "user"):
            kwargs[name] = user
        else:
            kwargs[name] = MagicMock(name=name)
    return kwargs


@pytest.fixture(scope="module")
def guarded(app):
    routes = _guarded_routes(app)
    assert len(routes) >= 40, f"sweep found only {len(routes)} guarded routes"
    return routes


async def test_every_guarded_route_refuses_foreign_tenants_before_any_query(guarded, monkeypatch):
    failures = []
    for route in guarded:
        fn = route.endpoint
        for caller_name, user in _foreign_callers(route.path):
            tripwire = _DbTripwire()
            monkeypatch.setitem(fn.__globals__, "database", tripwire)
            try:
                result = fn(**_call_kwargs(fn, user))
                if inspect.isawaitable(result):
                    await result
                failures.append(f"{route.path} [{caller_name}]: no refusal")
            except HTTPException as exc:
                if exc.status_code != 403:
                    failures.append(f"{route.path} [{caller_name}]: {exc.status_code} {exc.detail}")
            except AssertionError as exc:
                failures.append(f"{route.path} [{caller_name}]: {exc}")
            if not _guard_query_only(tripwire.queries):
                failures.append(f"{route.path} [{caller_name}]: queried {tripwire.queries}")
    assert not failures, "\n".join(failures)


async def _first_refusal(fn, user) -> Optional[HTTPException]:
    """Call the endpoint; return the HTTPException it raised, or None when it got past."""
    try:
        result = fn(**_call_kwargs(fn, user))
        if inspect.isawaitable(result):
            await result
    except HTTPException as exc:
        return exc
    except Exception:  # noqa: BLE001 - past the guard, into the (tripwired) body
        return None
    return None


def _agent_guard_staff_roles(fn) -> Optional[str]:
    """The staff_roles name passed to the agent guard in fn, '' for the default, None if absent."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name in ("assert_agent_scope", "assert_agent_scope_or_owner"):
                for kw in node.keywords:
                    if kw.arg == "staff_roles":
                        return ast.unparse(kw.value)
                return ""
    return None


class _PastTheGuard(Exception):
    """Raised by the wrapped guard once it lets the caller through: stops the body."""


def _stop_after_agent_guard(monkeypatch, fn) -> None:
    import utils.tenant_scope as ts

    async def or_owner(*a, **kw):
        await ts.assert_agent_scope_or_owner(*a, **kw)
        raise _PastTheGuard()

    def scope(*a, **kw):
        ts.assert_agent_scope(*a, **kw)
        raise _PastTheGuard()

    if "assert_agent_scope_or_owner" in fn.__globals__:
        monkeypatch.setitem(fn.__globals__, "assert_agent_scope_or_owner", or_owner)
    if "assert_agent_scope" in fn.__globals__:
        monkeypatch.setitem(fn.__globals__, "assert_agent_scope", scope)


async def test_agent_routes_admit_the_staff_set_they_declare(guarded, monkeypatch):
    """M13: a route that passes EMPLOYEE_STAFF_ROLES to the agent guard (keys,
    webhooks, identity issuers, revenue, routing) lets an employee token past it; a
    route that passes () (bank details, payouts) refuses every staff token. The
    guard is wrapped so nothing past it runs."""
    employee = {"sub": "u-emp", "email": "emp@example.com", "role": "employee",
                "membership_type": "employee", "employee_id": "emp_1"}
    admin = {"sub": "u-adm", "email": "adm@example.com", "role": "admin",
             "membership_type": "employee", "employee_id": "emp_2"}
    checked = {"EMPLOYEE_STAFF_ROLES": 0, "()": 0}
    failures = []
    for route in guarded:
        fn = route.endpoint
        roles = _agent_guard_staff_roles(fn)
        if roles not in checked:
            continue
        checked[roles] += 1
        monkeypatch.setitem(fn.__globals__, "database", _DbTripwire())
        with monkeypatch.context() as m:
            _stop_after_agent_guard(m, fn)
            for who, user in (("employee", employee), ("admin", admin)):
                try:
                    result = fn(**_call_kwargs(fn, user))
                    if inspect.isawaitable(result):
                        await result
                    outcome = "no guard ran"
                except _PastTheGuard:
                    outcome = "past"
                except HTTPException as exc:
                    outcome = f"refused {exc.status_code}"
                if roles == "EMPLOYEE_STAFF_ROLES" and outcome != "past":
                    failures.append(f"{route.path}: {who} {outcome}")
                if roles == "()" and not outcome.startswith("refused 403"):
                    failures.append(f"{route.path}: {who} {outcome}")
    assert checked["EMPLOYEE_STAFF_ROLES"] >= 10 and checked["()"] >= 5, checked
    assert not failures, "\n".join(failures)


@pytest.fixture(scope="module")
def onboarding_v2_app():
    """/platform-onboarding/* is mounted only with FEATURE_PLATFORM_ONBOARDING_V2=true
    (off by default), so the main.app sweep never sees it. Sweep its router alone."""
    from fastapi import FastAPI

    from routes.platform_onboarding_routes import router

    app = FastAPI()
    app.include_router(router)
    return app


def test_flag_gated_onboarding_id_routes_run_the_merchant_guard(onboarding_v2_app):
    routes = _tenant_routes(onboarding_v2_app)
    assert len(routes) >= 8, [r.path for r in routes]
    bad = [
        f"{','.join(sorted(r.methods))} {r.path}: {kind} {detail}"
        for r in routes
        for kind, detail in [_classify(r)]
        if kind != "guarded"
    ]
    assert not bad, "\n".join(bad)


async def test_flag_gated_onboarding_id_routes_refuse_foreign_tenants(onboarding_v2_app, monkeypatch):
    failures = []
    for route in _tenant_routes(onboarding_v2_app):
        fn = route.endpoint
        for caller_name, user in _foreign_callers(route.path):
            monkeypatch.setitem(fn.__globals__, "database", _DbTripwire())
            exc = await _first_refusal(fn, user)
            if exc is None or exc.status_code != 403:
                failures.append(f"{route.path} [{caller_name}]: {exc!r}")
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Routes that take the merchant from the BODY or QUERY (not swept above)
# ---------------------------------------------------------------------------

# (method, path, parameter expression the guard must be called on). These were
# anonymous or any-token; each must call the shared merchant guard on that value.
BODY_OR_QUERY_MERCHANT_ROUTES = [
    ("POST", "/products/sync-all-platforms/", "request.merchant_id"),
    ("POST", "/internal/product/quality/eval", "body.merchant_id"),
    ("POST", "/internal/product/enrichment/run", "body.merchant_id"),
    ("GET", "/api/gsc/oauth/start", "merchant_id"),
    ("GET", "/api/gsc/oauth/status", "merchant_id"),
]


def _merchant_guard_targets(fn) -> Set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name in MERCHANT_GUARDS and len(node.args) >= 2:
                out.add(ast.unparse(node.args[1]))
    return out


@pytest.mark.parametrize("method,path,target", BODY_OR_QUERY_MERCHANT_ROUTES)
def test_body_and_query_merchant_routes_run_the_shared_guard(app, method, path, target):
    matches = [
        r for r in app.routes
        if isinstance(r, APIRoute) and r.path == path and method in (r.methods or set())
    ]
    if path.startswith("/api/gsc/") and not matches:
        from fastapi import FastAPI

        from routes.gsc_oauth_routes import router as gsc_router

        gsc_app = FastAPI()
        gsc_app.include_router(gsc_router)
        matches = [r for r in gsc_app.routes if isinstance(r, APIRoute) and r.path == path]
    assert matches, f"{method} {path} is not mounted"
    for route in matches:
        assert target in _merchant_guard_targets(route.endpoint), (
            f"{method} {path} must call assert_merchant_scope on {target}"
        )
