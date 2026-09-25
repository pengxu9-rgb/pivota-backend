"""The live app mounts the GMV invoice credit admin router, every route behind require_admin.

Approving a credit issues a Stripe credit note, so an open route here would let anyone credit a
merchant. Top-level on purpose, like tests/test_admin_retailer_ingest_mounted.py: importing `main`
early in the sweep's single pytest process hangs it.
"""
from routes import admin_gmv_invoice_credits as module
from utils.auth import require_admin


def test_main_mounts_the_router_behind_the_guard():
    import main

    prefix = "/admin/billing/invoice-credits"
    mounted = {(r.path, tuple(sorted(r.methods))): r for r in main.app.routes
               if getattr(r, "path", "").startswith(prefix)}
    assert {p for p, _ in mounted} == {r.path for r in module.router.routes}
    for key, route in mounted.items():
        assert any(d.call is require_admin for d in route.dependant.dependencies), key


def test_every_route_refuses_a_caller_without_admin_credentials():
    """The guard is present AND effective: no bearer token is refused before any handler runs."""
    import asyncio

    import httpx
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(module.router)
    base = "/admin/billing/invoice-credits"
    calls = [("GET", base), ("GET", f"{base}/summary"), ("GET", f"{base}/1"),
             ("POST", f"{base}/1/approve"), ("POST", f"{base}/1/issue"), ("POST", f"{base}/1/cancel"),
             ("POST", f"{base}/compute")]

    async def _call_all():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return [(m, p, (await client.request(m, p, json={})).status_code) for m, p in calls]

    for method, path, status in asyncio.run(_call_all()):
        assert status in (401, 403), (method, path, status)
