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
