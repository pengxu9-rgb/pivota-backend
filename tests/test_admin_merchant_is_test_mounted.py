"""The live app mounts the merchant is_test router, every route behind require_admin.

Top-level, like tests/test_admin_retailer_ingest_mounted.py, for the reason its docstring gives.
"""
from routes import admin_merchant_is_test as module
from utils.auth import require_admin


def test_main_mounts_the_router_behind_the_guard():
    import main

    ours = {(r.path, tuple(sorted(r.methods))) for r in module.router.routes}
    assert ours == {
        ("/admin/merchants/{merchant_id}/is-test", ("GET",)),
        ("/admin/merchants/{merchant_id}/is-test", ("POST",)),
    }
    mounted = [r for r in main.app.routes if getattr(r, "path", "") == "/admin/merchants/{merchant_id}/is-test"]
    assert {(r.path, tuple(sorted(r.methods))) for r in mounted} == ours
    for route in mounted:
        assert any(d.call is require_admin for d in route.dependant.dependencies), route.methods
