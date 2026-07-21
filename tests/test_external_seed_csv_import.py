from typing import Any, Dict, Optional, Tuple
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_employee_external_seeds_import_csv_upsert_is_idempotent(monkeypatch) -> None:
    from utils.auth import get_current_employee

    async def override_employee() -> Dict[str, Any]:
        return {"employee_id": "emp_test", "email": "test@example.com"}

    import routes.employee_products as employee_products_module

    app = FastAPI()
    app.include_router(employee_products_module.router)
    app.dependency_overrides[get_current_employee] = override_employee
    try:
        store_by_id: Dict[str, Dict[str, Any]] = {}
        store_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        async def fake_fetch_one(_query: str, values: Optional[Dict[str, Any]] = None):
            v = values or {}
            market = str(v.get("market") or "")
            tool = str(v.get("tool") or "")
            external_product_id = str(v.get("external_product_id") or "")
            if external_product_id:
                return store_by_key.get((market, tool, external_product_id))

            match_url = str(v.get("match_url") or "")
            if match_url:
                for row in store_by_id.values():
                    if row.get("market") != market or row.get("tool") != tool:
                        continue
                    if row.get("canonical_url") == match_url or row.get("destination_url") == match_url:
                        return row
            return None

        async def fake_execute_seed_data_stmt(query: str, values: Dict[str, Any]) -> None:
            q = str(query or "").strip().lower()
            if q.startswith("insert"):
                row = dict(values)
                store_by_id[str(row["id"])] = row
                store_by_key[(str(row["market"]), str(row["tool"]), str(row["external_product_id"]))] = row
                return

            if q.startswith("update"):
                rid = str(values.get("id") or "")
                existing = store_by_id.get(rid)
                assert existing is not None
                existing.update(values)
                store_by_key[(str(existing["market"]), str(existing["tool"]), str(existing["external_product_id"]))] = existing
                return

            raise AssertionError(f"Unexpected query: {query}")

        monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
        monkeypatch.setattr(employee_products_module.database, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(employee_products_module, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)

        client = TestClient(app)
        csv_text = "destination_url,title\nhttps://example.com/p/1,Example\n"

        res1 = client.post(
            "/employee/products/external-seeds/import-csv?market=US&tool=*&mode=upsert",
            content=csv_text,
            headers={"Content-Type": "text/csv"},
        )
        assert res1.status_code == 200, res1.text
        payload1 = res1.json()
        assert payload1["created"] == 1
        assert payload1["updated"] == 0
        assert len(payload1.get("seedIds") or []) == 1

        res2 = client.post(
            "/employee/products/external-seeds/import-csv?market=US&tool=*&mode=upsert",
            content=csv_text,
            headers={"Content-Type": "text/csv"},
        )
        assert res2.status_code == 200, res2.text
        payload2 = res2.json()
        assert payload2["created"] == 0
        assert payload2["updated"] == 1
        assert len(payload2.get("seedIds") or []) == 1
    finally:
        app.dependency_overrides.pop(get_current_employee, None)


def test_employee_external_seeds_import_csv_catalog_groups_variants(monkeypatch) -> None:
    """
    Accept catalog exports (one row per variant) and group into a single external seed per Product URL.
    """
    from utils.auth import get_current_employee

    async def override_employee() -> Dict[str, Any]:
        return {"employee_id": "emp_test", "email": "test@example.com"}

    import routes.employee_products as employee_products_module

    app = FastAPI()
    app.include_router(employee_products_module.router)
    app.dependency_overrides[get_current_employee] = override_employee
    try:
        store_by_id: Dict[str, Dict[str, Any]] = {}
        store_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        async def fake_fetch_one(_query: str, values: Optional[Dict[str, Any]] = None):
            v = values or {}
            market = str(v.get("market") or "")
            tool = str(v.get("tool") or "")
            external_product_id = str(v.get("external_product_id") or "")
            if external_product_id:
                return store_by_key.get((market, tool, external_product_id))

            match_url = str(v.get("match_url") or "")
            if match_url:
                for row in store_by_id.values():
                    if row.get("market") != market or row.get("tool") != tool:
                        continue
                    if row.get("canonical_url") == match_url or row.get("destination_url") == match_url:
                        return row
            return None

        async def fake_execute_seed_data_stmt(query: str, values: Dict[str, Any]) -> None:
            q = str(query or "").strip().lower()
            if q.startswith("insert"):
                row = dict(values)
                store_by_id[str(row["id"])] = row
                store_by_key[(str(row["market"]), str(row["tool"]), str(row["external_product_id"]))] = row
                return

            if q.startswith("update"):
                rid = str(values.get("id") or "")
                existing = store_by_id.get(rid)
                assert existing is not None
                existing.update(values)
                store_by_key[(str(existing["market"]), str(existing["tool"]), str(existing["external_product_id"]))] = existing
                return

            raise AssertionError(f"Unexpected query: {query}")

        monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
        monkeypatch.setattr(employee_products_module.database, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(employee_products_module, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)

        client = TestClient(app)
        csv_text = (
            "Brand,Product Title,Product URL,Variant ID,SKU,Option Name,Option Value,Price,Currency,Availability,Variant Image URL,Variant Label Image URL,AI Merged Description,AI Marketing Copy,Deep Link\n"
            'Tom Ford Beauty,"Oud Wood Eau de Parfum",https://example.com/p/oud,111,T6K601,Size,30 ml,75.00,USD,In Stock,https://example.com/img1.jpg,https://example.com/swatch1.png,"Desc",,"https://example.com/p/oud?variant=111&utm_source=pivota"\n'
            'Tom Ford Beauty,"Oud Wood Eau de Parfum",https://example.com/p/oud,222,T43001,Size,50 ml,195.00,USD,In Stock,https://example.com/img2.jpg,https://example.com/swatch2.png,"Desc",,"https://example.com/p/oud?variant=222&utm_source=pivota"\n'
        )

        res1 = client.post(
            "/employee/products/external-seeds/import-csv?market=US&tool=*&mode=upsert",
            content=csv_text,
            headers={"Content-Type": "text/csv"},
        )
        assert res1.status_code == 200, res1.text
        payload1 = res1.json()
        assert payload1["created"] == 1
        assert payload1["updated"] == 0
        assert len(payload1.get("seedIds") or []) == 1

        assert len(store_by_id) == 1
        stored = next(iter(store_by_id.values()))
        seed_data = stored.get("seed_data") or {}
        assert isinstance(seed_data, dict)
        variants = seed_data.get("variants") or []
        assert isinstance(variants, list)
        assert len(variants) == 2
        # Variant titles should be row option values (e.g. "30 ml", "50 ml").
        titles = [v.get("title") for v in variants]
        assert "30 ml" in titles
        assert "50 ml" in titles
        assert any(v.get("variant_id") == "T6K601" and v.get("image_url") == "https://example.com/img1.jpg" for v in variants)
        assert any(v.get("variant_id") == "T43001" and v.get("image_url") == "https://example.com/img2.jpg" for v in variants)
        assert any(
            v.get("variant_id") == "T6K601" and v.get("label_image_url") == "https://example.com/swatch1.png" for v in variants
        )
        assert any(
            v.get("variant_id") == "T43001" and v.get("label_image_url") == "https://example.com/swatch2.png" for v in variants
        )

        res2 = client.post(
            "/employee/products/external-seeds/import-csv?market=US&tool=*&mode=upsert",
            content=csv_text,
            headers={"Content-Type": "text/csv"},
        )
        assert res2.status_code == 200, res2.text
        payload2 = res2.json()
        assert payload2["created"] == 0
        assert payload2["updated"] == 1
        assert len(payload2.get("seedIds") or []) == 1
        assert len(store_by_id) == 1
    finally:
        app.dependency_overrides.pop(get_current_employee, None)
