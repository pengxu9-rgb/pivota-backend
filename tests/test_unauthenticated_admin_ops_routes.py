"""Eight /admin/* routers shipped with NO authentication of any kind.

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
    ("POST", "/admin/merchants/canonicalize", {"merchant_ids": []}),
    ("POST", "/admin/migrations/run/006-psp-constraints", None),
    ("GET", "/admin/migrations/status/006-psp-constraints", None),
    ("POST", "/admin/init/agent-test-key", None),
]


def _call(client: TestClient, method: str, path: str, body: Any, headers=None):
    kwargs: Dict[str, Any] = {"headers": headers or {}}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, path, **kwargs)


@pytest.mark.parametrize("method,path,body", UNAUTHENTICATED_ROUTES)
def test_admin_ops_routes_refuse_an_anonymous_caller(client, method, path, body):
    """No Authorization header, no X-ADMIN-KEY. Must be refused."""
    resp = _call(client, method, path, body)

    assert resp.status_code in (401, 403), (
        f"{method} {path} answered {resp.status_code} to an unauthenticated "
        f"caller: {resp.text[:400]}"
    )


@pytest.mark.parametrize("method,path,body", UNAUTHENTICATED_ROUTES)
def test_admin_ops_routes_refuse_a_non_admin_token(client, method, path, body):
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


@pytest.mark.parametrize("method,path,body", UNAUTHENTICATED_ROUTES)
def test_admin_ops_routes_refuse_a_bogus_admin_key(client, method, path, body):
    """An X-ADMIN-KEY that is not the configured one must not pass.

    Kills a guard that merely checks the header is PRESENT.
    """
    resp = _call(client, method, path, body, {"X-ADMIN-KEY": "not-the-key"})

    assert resp.status_code in (401, 403), (
        f"{method} {path} accepted a bogus admin key: {resp.status_code} "
        f"{resp.text[:400]}"
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
