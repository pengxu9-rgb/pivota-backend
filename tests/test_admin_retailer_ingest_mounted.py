"""The live app mounts the retailer ingest admin router, every route behind require_admin.

Top-level on purpose: importing `main` from tests/services ran it early in the sweep's single pytest
process, and the process printed its summary and then hung until the 25-minute job timeout
(RuntimeError: Event loop is closed from a thread main started). The other top-level suites that
import main run late in collection order, as this one now does.
"""
from routes import admin_retailer_ingest as module
from utils.auth import require_admin


def test_main_mounts_the_router_behind_the_guard():
    import main

    mounted = {r.path: r for r in main.app.routes if getattr(r, "path", "").startswith("/admin/retailer-ingest")}
    assert set(mounted) == {r.path for r in module.router.routes}
    for path, route in mounted.items():
        assert any(d.call is require_admin for d in route.dependant.dependencies), path
