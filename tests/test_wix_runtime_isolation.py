from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_legacy_wix_sync_fix_route_stays_unmounted() -> None:
    main_source = _read("main.py")

    assert "wix_sync_fix" not in main_source


def test_wix_admin_store_mutation_routes_are_runtime_gated() -> None:
    cleanup_source = _read("routes/admin_cleanup_stores.py")

    assert "ALLOW_PROD_ADMIN_STORE_MUTATION" in cleanup_source
    assert "Superadmin access required" in cleanup_source


def test_destructive_cleanup_rebuild_routes_are_auth_and_runtime_gated() -> None:
    source = _read("routes/admin_cleanup_rebuild.py")

    assert "Depends(require_admin)" in source
    assert "ENABLE_DESTRUCTIVE_ADMIN_CLEANUP" in source
    assert "ENABLE_ADMIN_CLEANUP_VERIFY" in source
    assert "NO AUTH" not in source
