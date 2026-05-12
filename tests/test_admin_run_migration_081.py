from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.admin_run_migration_081 as module
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


def test_verify_seed_content_migration_081_route(monkeypatch) -> None:
    app = _build_app()

    monkeypatch.setattr(module, "_resolved_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(module, "_verify", lambda database_url: {"col": 1, "tbl": 1, "trg": 1})

    client = TestClient(app)
    response = client.get("/admin/migrations/verify/081")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "verify"
    assert payload["success"] is True
    assert payload["apply"] is None
    assert payload["verification"] == {"col": 1, "tbl": 1, "trg": 1}


def test_post_run_seed_content_migration_081_apply_alias(monkeypatch) -> None:
    app = _build_app()

    calls: list[str] = []
    monkeypatch.setattr(module, "_resolved_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(module, "_verify", lambda database_url: {"col": 1, "tbl": 1, "trg": 1})
    monkeypatch.setattr(
        module,
        "_apply",
        lambda database_url: calls.append(database_url) or {"applied": True, "migration_bytes": 1234},
    )

    client = TestClient(app)
    response = client.post("/admin/migrations/post/run/081", json={"mode": "apply"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "apply"
    assert payload["success"] is True
    assert payload["apply"] == {"applied": True, "migration_bytes": 1234}
    assert calls == ["postgresql://test"]


def test_run_seed_content_migration_081_fails_until_all_objects_exist(monkeypatch) -> None:
    monkeypatch.setattr(module, "_verify", lambda database_url: {"col": 1, "tbl": 1, "trg": 0})

    report = module._run("verify", "postgresql://test")

    assert report["success"] is False
    assert report["verification"] == {"col": 1, "tbl": 1, "trg": 0}
