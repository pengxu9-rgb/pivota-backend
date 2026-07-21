from typing import Any, Dict
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


from utils.auth import get_current_employee


def _build_client(monkeypatch):
    import routes.employee_products as employee_products_module

    async def override_employee() -> Dict[str, Any]:
        return {"employee_id": "emp_test", "email": "test@example.com"}

    app = FastAPI()
    app.include_router(employee_products_module.router)
    app.dependency_overrides[get_current_employee] = override_employee
    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    return employee_products_module, TestClient(app), app


def test_delete_external_seed_hard_deletes_single_seed(monkeypatch) -> None:
    employee_products_module, client, app = _build_client(monkeypatch)
    try:
        row = {
            "id": "eps_test_1",
            "external_product_id": "ext_test_1",
            "status": "active",
            "domain": "pixibeauty.com",
            "title": "Glow Mist",
            "destination_url": "https://pixibeauty.com/products/glow-mist",
            "canonical_url": "https://pixibeauty.com/products/glow-mist",
        }

        async def fake_fetch_one(query: str, values=None):
            assert "FROM external_product_seeds" in str(query)
            assert values == {"id": "eps_test_1"}
            return row

        async def fake_fetch_all(query: str, values=None):
            assert "DELETE FROM external_product_seeds" in str(query)
            assert values == {"id": "eps_test_1"}
            return [row]

        monkeypatch.setattr(employee_products_module.database, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(employee_products_module.database, "fetch_all", fake_fetch_all)

        res = client.delete("/employee/products/external-seeds/eps_test_1")
        assert res.status_code == 200, res.text
        payload = res.json()
        assert payload["status"] == "success"
        assert payload["deleted_count"] == 1
        assert payload["deleted_ids"] == ["eps_test_1"]
        assert payload["items"][0]["destination_url"] == "https://pixibeauty.com/products/glow-mist"
    finally:
        app.dependency_overrides.clear()


def test_hard_delete_external_seeds_by_domain_dry_run_normalizes_domain(monkeypatch) -> None:
    employee_products_module, client, app = _build_client(monkeypatch)
    try:
        captured = {}
        sample_rows = [
            {
                "id": "eps_1",
                "external_product_id": "ext_1",
                "status": "active",
                "domain": "www.pixibeauty.com",
                "title": "Glow Tonic",
                "destination_url": "https://pixibeauty.com/products/glow-tonic-100ml",
                "canonical_url": "https://pixibeauty.com/products/glow-tonic-100ml",
            },
            {
                "id": "eps_2",
                "external_product_id": "ext_2",
                "status": "inactive",
                "domain": "pixibeauty.com",
                "title": "Glow Mist",
                "destination_url": "https://pixibeauty.com/products/glow-mist",
                "canonical_url": "https://pixibeauty.com/products/glow-mist",
            },
        ]

        async def fake_fetch_one(query: str, values=None):
            captured["count_values"] = dict(values or {})
            return {"n": 2}

        async def fake_fetch_all(query: str, values=None):
            captured["sample_values"] = dict(values or {})
            assert "SELECT id, external_product_id, status, domain, title, destination_url, canonical_url" in str(query)
            return sample_rows

        monkeypatch.setattr(employee_products_module.database, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(employee_products_module.database, "fetch_all", fake_fetch_all)

        res = client.post(
            "/employee/products/external-seeds/hard-delete-by-domain",
            json={
                "domain": "https://www.pixibeauty.com/products/glow-mist",
                "include_subdomains": True,
                "dry_run": True,
                "sample_limit": 5,
            },
        )
        assert res.status_code == 200, res.text
        payload = res.json()
        assert payload["status"] == "success"
        assert payload["domain"] == "pixibeauty.com"
        assert payload["dry_run"] is True
        assert payload["match_count"] == 2
        assert payload["deleted_count"] == 0
        assert len(payload["sample"]) == 2
        assert captured["count_values"]["domain_filter_exact"] == "pixibeauty.com"
        assert captured["count_values"]["domain_filter_www"] == "www.pixibeauty.com"
        assert captured["count_values"]["domain_filter_subdomain"] == "%.pixibeauty.com"
        assert captured["count_values"]["domain_filter_url_like"] == "%pixibeauty.com%"
        assert captured["sample_values"]["sample_limit"] == 5
    finally:
        app.dependency_overrides.clear()


def test_hard_delete_external_seeds_by_domain_deletes_all_matches(monkeypatch) -> None:
    employee_products_module, client, app = _build_client(monkeypatch)
    try:
        sample_rows = [
            {
                "id": "eps_1",
                "external_product_id": "ext_1",
                "status": "active",
                "domain": "pixibeauty.com",
                "title": "Glow Tonic",
                "destination_url": "https://pixibeauty.com/products/glow-tonic-100ml",
                "canonical_url": "https://pixibeauty.com/products/glow-tonic-100ml",
            },
            {
                "id": "eps_2",
                "external_product_id": "ext_2",
                "status": "inactive",
                "domain": "www.pixibeauty.com",
                "title": "Glow Mist",
                "destination_url": "https://pixibeauty.com/products/glow-mist",
                "canonical_url": "https://pixibeauty.com/products/glow-mist",
            },
        ]

        async def fake_fetch_one(query: str, values=None):
            assert "SELECT COUNT(*) AS n" in str(query)
            return {"n": 2}

        async def fake_fetch_all(query: str, values=None):
            q = str(query)
            if "SELECT id, external_product_id, status, domain, title, destination_url, canonical_url" in q:
                return sample_rows
            if "DELETE FROM external_product_seeds" in q:
                return sample_rows
            raise AssertionError(f"Unexpected query: {query}")

        monkeypatch.setattr(employee_products_module.database, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(employee_products_module.database, "fetch_all", fake_fetch_all)

        res = client.post(
            "/employee/products/external-seeds/hard-delete-by-domain",
            json={"domain": "pixibeauty.com", "dry_run": False, "sample_limit": 10},
        )
        assert res.status_code == 200, res.text
        payload = res.json()
        assert payload["status"] == "success"
        assert payload["dry_run"] is False
        assert payload["match_count"] == 2
        assert payload["deleted_count"] == 2
        assert payload["deleted_ids"] == ["eps_1", "eps_2"]
        assert len(payload["sample"]) == 2
    finally:
        app.dependency_overrides.clear()
