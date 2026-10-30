"""The live app mounts the Web Bot Auth key directory (routes/crawl_identity.py), unauthenticated.

Top-level and importing `main` inside the test, as tests/test_admin_retailer_ingest_mounted.py
explains: importing main early in the sweep's single pytest process hung it.
"""
from routes import crawl_identity as module
from services import crawl_identity


def test_main_mounts_the_directory_route_without_dependencies():
    import main

    mounted = [r for r in main.app.routes if getattr(r, "path", "") == crawl_identity.DIRECTORY_PATH]
    assert len(mounted) == 1 and {r.path for r in module.router.routes} == {crawl_identity.DIRECTORY_PATH}
    assert "GET" in mounted[0].methods
    assert not mounted[0].dependant.dependencies  # a verifier fetches it with no credentials
