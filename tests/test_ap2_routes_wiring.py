"""
Smoke coverage for the AP2 surface wiring in main.py.

Two layers:

1. Real-app boots (subprocess): import the actual main.py under both
   ENABLE_AP2_ROUTES states and assert the flag gates the surface — the router
   is mounted (and /ap2/consent/grant routable) only when enabled, and the
   AP2SecurityMiddleware is installed. Run in a clean subprocess because
   settings.enable_ap2_routes is evaluated at import time, so the flag cannot be
   flipped for an already-imported main within one interpreter.

2. In-process wiring (mirrors tests/test_ap2_consent_grant_route.py): build a
   FastAPI app wired exactly as main.py wires the AP2 surface with the middleware
   ENABLED, and assert the consent-grant bootstrap is NOT blocked by the
   middleware's consent-token requirement while genuinely protected routes still
   are.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ---------------------------------------------------------------------------
# Layer 1 — real main.py boots under each flag state
# ---------------------------------------------------------------------------

def _boot_main(flag_value: str) -> dict:
    """Import the real main.py in a clean subprocess with ENABLE_AP2_ROUTES set."""
    script = textwrap.dedent(
        """
        import json
        import main

        ap2_paths = sorted(
            {getattr(r, "path", "") for r in main.app.routes
             if getattr(r, "path", "").startswith("/ap2")}
        )
        middlewares = [m.cls.__name__ for m in main.app.user_middleware]
        print("RESULT " + json.dumps({
            "flag": bool(main.settings.enable_ap2_routes),
            "grant_routable": "/ap2/consent/grant" in ap2_paths,
            "ap2_route_count": len(ap2_paths),
            "middleware_installed": "AP2SecurityMiddleware" in middlewares,
        }))
        """
    )
    env = dict(os.environ)
    env["ENABLE_AP2_ROUTES"] = flag_value
    # main imports cleanly with only a DB URL present; the connect happens in the
    # lifespan, not at import, so no live database is required for this boot check.
    env.setdefault(
        "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"main.py failed to import with ENABLE_AP2_ROUTES={flag_value}:\n"
        f"{proc.stderr[-3000:]}"
    )
    result_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert result_lines, f"no RESULT emitted; stdout tail:\n{proc.stdout[-2000:]}"
    return json.loads(result_lines[-1][len("RESULT "):])


def test_app_boots_and_grant_routable_when_enabled():
    res = _boot_main("true")
    assert res["flag"] is True
    assert res["grant_routable"] is True, "POST /ap2/consent/grant must be mounted"
    assert res["ap2_route_count"] >= 1
    assert res["middleware_installed"] is True


def test_ap2_surface_absent_when_disabled():
    res = _boot_main("false")
    assert res["flag"] is False
    assert res["grant_routable"] is False
    assert res["ap2_route_count"] == 0, "no /ap2 routes should mount when disabled"


# ---------------------------------------------------------------------------
# Layer 2 — in-process wiring with the middleware ENABLED
# ---------------------------------------------------------------------------

class _EmptyDB:
    """DB stub: no agents, no nonces — enough to reach route logic."""

    async def fetch_one(self, query, values=None):
        return None

    async def execute(self, query, values=None):
        return None


@pytest.fixture
def enforced_client(monkeypatch):
    """FastAPI app wired like main.py wires AP2, with the middleware ENABLED."""
    from routes.ap2_routes import router as ap2_router
    from middleware.ap2_security import AP2SecurityMiddleware
    from db.database import database

    empty = _EmptyDB()
    monkeypatch.setattr(database, "fetch_one", empty.fetch_one)
    monkeypatch.setattr(database, "execute", empty.execute)

    app = FastAPI()
    app.add_middleware(AP2SecurityMiddleware, enabled=True)
    app.include_router(ap2_router)
    return TestClient(app)


def test_grant_bootstrap_not_blocked_by_enabled_middleware(enforced_client):
    # No X-Agent-Consent (the caller has none yet) but signature + nonce present.
    # With enforcement ON, the grant bootstrap must be exempt and reach the route,
    # which self-verifies. Empty DB -> the route (not the middleware) answers with
    # 401 "Unknown agent" rather than the middleware's "Missing X-Agent-Consent".
    res = enforced_client.post(
        "/ap2/consent/grant",
        json={"agent_id": "agent_x", "scope": ["read"]},
        headers={"X-AP2-Signature": "sig", "X-AP2-Nonce": "nonce-wiring-1"},
    )
    assert res.status_code == 401
    assert res.json().get("detail") == "Unknown agent"


def test_enabled_middleware_still_guards_protected_route(enforced_client):
    # A genuinely protected AP2 route with no auth headers must be blocked by the
    # enabled middleware — proving enforcement is actually live (and scoped).
    res = enforced_client.get("/ap2/wallet/balance")
    assert res.status_code == 401
    assert res.json().get("error") == "Missing X-Agent-Consent header"


def test_public_status_endpoint_open_under_enforcement(enforced_client):
    res = enforced_client.get("/ap2/status")
    assert res.status_code == 200
    assert res.json().get("protocol") == "AP2"


def test_receipt_get_is_public_but_non_get_fails_closed(enforced_client):
    # GET /ap2/receipt/{id} is a read route and is exempt: it reaches the handler
    # (empty DB -> 404 from the route), NOT blocked by the middleware.
    got = enforced_client.get("/ap2/receipt/txn-xyz")
    assert got.status_code == 404
    assert got.json().get("detail") == "Transaction not found"

    # The receipt-prefix exemption is GET-only: a (hypothetical future) non-GET
    # under the same prefix must fail closed at the middleware, not inherit public
    # status. Today there is no POST route there, so the middleware's auth check
    # is what answers — proving the exemption did not leak to other methods.
    posted = enforced_client.post("/ap2/receipt/txn-xyz", json={})
    assert posted.status_code == 401
    assert posted.json().get("error") == "Missing X-Agent-Consent header"
