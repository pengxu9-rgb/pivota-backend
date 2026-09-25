"""/admin/psp/* must not answer the internet.

THE DEFECT, live on prod until this fix (probed 2026-08-26: both GETs answered
HTTP 200 to a bare curl against api.pivota.cc). routes/admin_psp_integrity.py
registered three routes with NO auth dependency of any kind:

  * GET  /admin/psp/integrity-check — full order-table statistics: total order
    count, per-PSP order/merchant distribution, and sample rows carrying
    order_id + merchant_id + psp_id. Business-volume reconnaissance for free.
  * POST /admin/psp/auto-heal — with ?dry_run=false, UPDATEs the orders table
    (psp_used/psp_id rewrites). An anonymous caller could mutate payment
    attribution on every order.
  * GET  /admin/psp/specification — static, but the same missing gate.

THE RULE (the money-admin norm, utils.auth.require_admin): an ADMIN credential
— role admin or super_admin — is required on every route in this file. A valid
non-admin token is refused with 403, and a refused request must never reach the
handler body (zero database calls).

Mutants each test exists to kill are named inline. The non-admin tests mint
REAL signed JWTs, not the `test-token` placeholder whose pytest-only bypass in
utils.auth hands back role=admin and would make the non-admin claim untestable.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

_PSP_ROUTES = (
    ("GET", "/admin/psp/integrity-check"),
    ("POST", "/admin/psp/auto-heal"),
    ("GET", "/admin/psp/specification"),
)

_ADMIN = {"Authorization": "Bearer test-token"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _token(role: str) -> str:
    from utils.auth import create_access_token

    return create_access_token({"sub": "u1", "email": "u1@example.com", "role": role})


class _DatabaseSpy:
    """Stands in for routes.admin_psp_integrity.database.

    Records every fetch_one/fetch_all/execute so a test can assert the handler
    body never ran. Returns canned shapes so the ADMIT tests can drive the real
    handlers without a database.
    """

    def __init__(self) -> None:
        self.calls: List[str] = []

    async def fetch_one(self, query: str, *a: Any, **kw: Any) -> Dict[str, int]:
        self.calls.append("fetch_one")
        # Superset of the keys any fetch_one caller in the module indexes.
        return {
            "total_orders": 0,
            "null_psp_used": 0,
            "null_psp_id": 0,
            "incomplete": 0,
            "complete": 0,
            "count": 0,
        }

    async def fetch_all(self, query: str, *a: Any, **kw: Any) -> list:
        self.calls.append("fetch_all")
        return []

    async def execute(self, query: str, *a: Any, **kw: Any) -> int:
        self.calls.append("execute")
        return 0


@pytest.fixture()
def db_spy(monkeypatch) -> _DatabaseSpy:
    import routes.admin_psp_integrity as mod

    spy = _DatabaseSpy()
    monkeypatch.setattr(mod, "database", spy)
    return spy


def _request(client: TestClient, method: str, path: str, **kw: Any):
    return client.get(path, **kw) if method == "GET" else client.post(path, **kw)


# ── refusals ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", _PSP_ROUTES)
def test_anonymous_callers_are_refused_with_zero_side_effects(
    client: TestClient, db_spy: _DatabaseSpy, method: str, path: str
) -> None:
    res = _request(client, method, path)

    assert res.status_code in (401, 403), res.text
    assert db_spy.calls == [], f"handler for {method} {path} touched the database"
    assert "total_orders" not in res.text
    assert "psp_stripe_" not in res.text


@pytest.mark.parametrize("method,path", _PSP_ROUTES)
@pytest.mark.parametrize("role", ["merchant", "agent", "employee", "user", "viewer"])
def test_a_valid_non_admin_credential_is_still_refused(
    client: TestClient, db_spy: _DatabaseSpy, role: str, method: str, path: str
) -> None:
    """KILLS the mutant that swaps require_admin for get_current_user.

    Authentication alone would hand order-table statistics — and the auto-heal
    write path — to every merchant and agent holding a token.
    """
    res = _request(
        client, method, path, headers={"Authorization": f"Bearer {_token(role)}"}
    )

    assert res.status_code == 403, f"role={role} on {method} {path}: {res.text}"
    assert db_spy.calls == [], f"role={role} reached the {method} {path} handler"


def test_anonymous_auto_heal_write_mode_is_refused_before_any_update(
    client: TestClient, db_spy: _DatabaseSpy
) -> None:
    """The worst case pinned by name: ?dry_run=false is the branch that would
    UPDATE orders. The refusal must land before any database call."""
    res = client.post("/admin/psp/auto-heal?dry_run=false")

    assert res.status_code in (401, 403), res.text
    assert db_spy.calls == []


# ── admins still get through (the gate must not close the door it guards) ────


def test_admin_integrity_check_still_works(
    client: TestClient, db_spy: _DatabaseSpy
) -> None:
    res = client.get("/admin/psp/integrity-check", headers=_ADMIN)

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "healthy"
    assert "fetch_one" in db_spy.calls, "handler did not run for an admin"


def test_admin_auto_heal_dry_run_still_works(
    client: TestClient, db_spy: _DatabaseSpy
) -> None:
    res = client.post("/admin/psp/auto-heal", headers=_ADMIN)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["dry_run"] is True
    assert "execute" not in db_spy.calls, "dry_run executed a write"


def test_admin_auto_heal_write_mode_still_reaches_the_update_path(
    client: TestClient, db_spy: _DatabaseSpy
) -> None:
    """Proves the 403s above come from the GATE, not from the write path being
    broken for everyone: an admin's dry_run=false call must reach execute()."""
    res = client.post("/admin/psp/auto-heal?dry_run=false", headers=_ADMIN)

    assert res.status_code == 200, res.text
    assert res.json()["dry_run"] is False
    assert "execute" in db_spy.calls


def test_admin_specification_still_works(client: TestClient) -> None:
    res = client.get("/admin/psp/specification", headers=_ADMIN)

    assert res.status_code == 200, res.text
    assert res.json()["version"] == "1.0.0"


# ── structural sweep ─────────────────────────────────────────────────────────


def _carries_require_admin(dependant) -> bool:
    from utils.auth import require_admin

    return any(
        dep.call is require_admin or _carries_require_admin(dep)
        for dep in dependant.dependencies
    )


def test_every_admin_psp_route_is_gated_by_require_admin() -> None:
    """KILLS the mutant that gates two routes and forgets the third — and
    catches the FUTURE route added under /admin/psp without a gate. Iterates
    the APP's routes, not this test's list, so a new path cannot hide from it.

    Deliberately wider than this module: routes/admin_api.py also registers
    /admin/psp/{status,list,connect,...} (gated at the time of writing), and
    those must not regress either.
    """
    import main

    psp_routes = [
        r
        for r in main.app.routes
        if getattr(r, "path", "").startswith("/admin/psp")
    ]
    # If the integrity router is ever unmounted the loop would pass vacuously.
    found = {r.path for r in psp_routes}
    assert {path for _m, path in _PSP_ROUTES} <= found, found

    for route in psp_routes:
        assert _carries_require_admin(route.dependant), (
            f"{route.path} is not gated by require_admin"
        )
