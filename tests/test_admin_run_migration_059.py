from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.admin_run_migration_059 as module
from utils.auth import require_admin


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[require_admin] = lambda: {
        "sub": "admin_123",
        "email": "admin@example.com",
        "role": "admin",
    }
    return app


def test_verify_catalog_migration_059_route(monkeypatch) -> None:
    app = _build_app()

    monkeypatch.setattr(module, "_resolved_database_url", lambda: "sqlite:////tmp/test.sqlite3")
    monkeypatch.setattr(
        module,
        "run_catalog_migration_059",
        lambda mode, database_url: {
            "mode": mode,
            "database_kind": "postgres",
            "migration_path": "/tmp/059_catalog_pivot_search_indexes.sql",
            "success": True,
            "apply": None,
            "verification": {
                "missing_indexes_count": 0,
                "missing_indexes": [],
            },
        },
    )

    client = TestClient(app)
    response = client.get("/admin/migrations/verify/059")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "verify"
    assert payload["success"] is True
    assert payload["verification"]["missing_indexes_count"] == 0


def test_run_catalog_migration_059_route(monkeypatch) -> None:
    app = _build_app()

    monkeypatch.setattr(module, "_resolved_database_url", lambda: "sqlite:////tmp/test.sqlite3")
    monkeypatch.setattr(
        module,
        "run_catalog_migration_059",
        lambda mode, database_url: {
            "mode": mode,
            "database_kind": "postgres",
            "migration_path": "/tmp/059_catalog_pivot_search_indexes.sql",
            "success": True,
            "apply": {"applied": True},
            "verification": {
                "missing_indexes_count": 0,
                "missing_indexes": [],
            },
        },
    )

    client = TestClient(app)
    response = client.post("/admin/migrations/run/059", json={"mode": "apply-verify"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "apply-verify"
    assert payload["apply"]["applied"] is True
