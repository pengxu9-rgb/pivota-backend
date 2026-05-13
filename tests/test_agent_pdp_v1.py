from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routes.agent_pdp_v1 as agent_pdp_v1  # noqa: E402


CK_A = "ck_" + "a" * 32
CK_B = "ck_" + "b" * 32
CK_C = "ck_" + "c" * 32
SIG_A = "sig_" + "1" * 32
SIG_C = "sig_" + "3" * 32
GROUP_ID = "pg_" + "9" * 32


def _row(
    *,
    content_key: str = CK_A,
    pivota_signature_id: Optional[str] = SIG_A,
    product_group_id: str = GROUP_ID,
    title: str = "Watermelon Glow Serum",
) -> Dict[str, Any]:
    return {
        "content_key": content_key,
        "pivota_signature_id": pivota_signature_id,
        "product_group_id": product_group_id,
        "brand": "Glow Recipe",
        "title": title,
        "description": "A brightening serum.",
        "image_url": "https://cdn.example.com/main.jpg",
        "image_urls": [
            "https://cdn.example.com/main.jpg",
            "https://cdn.example.com/alt.jpg",
        ],
        "currency": "USD",
        "price_min": Decimal("34.00"),
        "price_max": Decimal("39.00"),
        "offer_count": 2,
        "offers": [
            {
                "merchant_id": "merch_1",
                "merchant_name": "Primary Store",
                "price": Decimal("34.00"),
                "currency": "USD",
                "availability": "in_stock",
                "url": "https://merchant.example/products/serum",
            }
        ],
        "variants": [
            {
                "variant_id": "sku_1",
                "sku": "SKU-1",
                "title": "30ml",
                "options": {"size": "30ml"},
                "price": Decimal("34.00"),
                "currency": "USD",
                "availability": "in_stock",
            }
        ],
        "variants_count": 1,
        "gtin13": "00123456789012",
        "category_path": "beauty/skincare/serum",
        "taxonomy_tags": ["serum", "brightening"],
        "breadcrumb": [
            {"position": 1, "name": "Products", "item": "/products"},
            {"position": 2, "name": "Serum", "item": "/products?category=serum"},
        ],
        "pdp_lifecycle_stage": "validated",
        "sync_status": "synced",
        "primary_merchant_id": "merch_1",
        "refreshed_at": datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
        "refreshed_by_proposal_id": 123,
        "refresh_source": "test",
    }


class FakeAgentPdpDatabase:
    def __init__(
        self,
        rows: List[Dict[str, Any]],
        ext_id_to_content_key: Optional[Dict[str, str]] = None,
    ) -> None:
        self.rows = rows
        self.ext_id_to_content_key = ext_id_to_content_key or {}
        self.calls: List[Dict[str, Any]] = []

    async def fetch_one(self, query: str, values: Optional[Dict[str, Any]] = None):
        self.calls.append({"query": str(query), "values": dict(values or {})})
        params = values or {}

        # ext_* resolution path (different bind name + JOIN shape)
        if "FROM external_product_seeds eps" in str(query):
            ext_id = str(params.get("ext_id") or "")
            ck = self.ext_id_to_content_key.get(ext_id)
            return {"content_key": ck} if ck else None

        lookup_id = str(params.get("id") or "")

        if "WHERE content_key = :id" in str(query):
            return next((row for row in self.rows if row["content_key"] == lookup_id), None)

        if "WHERE pivota_signature_id = :id" in str(query):
            return next(
                (row for row in self.rows if row.get("pivota_signature_id") == lookup_id),
                None,
            )

        if "WHERE product_group_id = :id" in str(query):
            group_rows = [
                row for row in self.rows if row.get("product_group_id") == lookup_id
            ]
            group_rows.sort(
                key=lambda row: (
                    0 if row.get("pivota_signature_id") else 1,
                    str(row.get("content_key") or ""),
                )
            )
            return group_rows[0] if group_rows else None

        raise AssertionError(f"unexpected query: {query}")


def _client(
    monkeypatch,
    rows: List[Dict[str, Any]],
    *,
    ext_id_to_content_key: Optional[Dict[str, str]] = None,
):
    db = FakeAgentPdpDatabase(rows, ext_id_to_content_key=ext_id_to_content_key)
    monkeypatch.setattr(agent_pdp_v1, "database", db)
    app = FastAPI()
    app.include_router(agent_pdp_v1.router)
    return TestClient(app), db


def _canonical_product(body: Dict[str, Any]) -> Dict[str, Any]:
    modules = body["modules"]
    canonical = next(module for module in modules if module["type"] == "canonical")
    return canonical["data"]["pdp_payload"]["product"]


def _offers_module(body: Dict[str, Any]) -> Dict[str, Any]:
    return next(module for module in body["modules"] if module["type"] == "offers")


def test_get_agent_pdp_by_content_key_returns_modules_envelope(monkeypatch) -> None:
    client, db = _client(monkeypatch, [_row()])

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 200
    body = response.json()
    product = _canonical_product(body)
    assert product["content_key"] == CK_A
    assert product["pivota_signature_id"] == SIG_A
    assert product["product_group_id"] == GROUP_ID
    assert product["title"] == "Watermelon Glow Serum"
    assert product["name"] == "Watermelon Glow Serum"
    assert product["brand"] == "Glow Recipe"
    assert product["brand_name"] == "Glow Recipe"
    assert product["product_id"] == SIG_A
    assert product["image_url"] == "https://cdn.example.com/main.jpg"
    assert product["image_urls"][1] == "https://cdn.example.com/alt.jpg"
    assert product["variants"][0]["variant_id"] == "sku_1"
    assert body["subject"] == {"type": "product_group", "id": GROUP_ID}
    assert _offers_module(body)["data"]["offers_count"] == 2
    assert db.calls[0]["values"] == {"id": CK_A}
    assert "WHERE content_key = :id" in db.calls[0]["query"]


def test_get_agent_pdp_by_signature_returns_same_row(monkeypatch) -> None:
    client, db = _client(monkeypatch, [_row()])

    response = client.get(f"/api/agent/pdp/{SIG_A}")

    assert response.status_code == 200
    product = _canonical_product(response.json())
    assert product["content_key"] == CK_A
    assert product["pivota_signature_id"] == SIG_A
    assert db.calls[0]["values"] == {"id": SIG_A}
    assert "WHERE pivota_signature_id = :id" in db.calls[0]["query"]


def test_get_agent_pdp_by_group_id_prefers_signature_bearing_row(monkeypatch) -> None:
    signed = _row(
        content_key=CK_C,
        pivota_signature_id=SIG_C,
        title="Canonical Signed Row",
    )
    unsigned = _row(
        content_key=CK_B,
        pivota_signature_id=None,
        title="Unsigned Row",
    )
    client, db = _client(monkeypatch, [unsigned, signed])

    response = client.get(f"/api/agent/pdp/{GROUP_ID}")

    assert response.status_code == 200
    product = _canonical_product(response.json())
    assert product["content_key"] == CK_C
    assert product["title"] == "Canonical Signed Row"
    query = db.calls[0]["query"]
    assert "WHERE product_group_id = :id" in query
    assert "CASE WHEN pivota_signature_id IS NOT NULL THEN 0 ELSE 1 END" in query
    assert "content_key ASC" in query


def test_get_agent_pdp_by_group_id_uses_lowest_content_key_when_no_signature(monkeypatch) -> None:
    higher = _row(content_key=CK_C, pivota_signature_id=None, title="Higher")
    lower = _row(content_key=CK_B, pivota_signature_id=None, title="Lower")
    client, _db = _client(monkeypatch, [higher, lower])

    response = client.get(f"/api/agent/pdp/{GROUP_ID}")

    assert response.status_code == 200
    product = _canonical_product(response.json())
    assert product["content_key"] == CK_B
    assert product["title"] == "Lower"


def test_get_agent_pdp_unknown_id_returns_404(monkeypatch) -> None:
    client, db = _client(monkeypatch, [])

    response = client.get(f"/api/agent/pdp/{CK_A}")

    assert response.status_code == 404
    assert db.calls[0]["values"] == {"id": CK_A}


def test_get_agent_pdp_malformed_id_returns_404_without_query(monkeypatch) -> None:
    client, db = _client(monkeypatch, [_row()])

    response = client.get("/api/agent/pdp/prod_123")
    malformed_ck_response = client.get("/api/agent/pdp/ck_nothex")

    assert response.status_code == 404
    assert malformed_ck_response.status_code == 404
    assert db.calls == []


def test_get_agent_pdp_strips_pg_colon_prefix_before_group_lookup(monkeypatch) -> None:
    """Gateway emits canonical refs as `pg:<actual_group_id>`. The route
    must strip the `pg:` so the SQL queries product_group_id correctly."""
    row = _row()  # has product_group_id = GROUP_ID
    client, db = _client(monkeypatch, [row])

    response = client.get(f"/api/agent/pdp/pg:{GROUP_ID}")

    assert response.status_code == 200
    # The SQL bind must be the bare GROUP_ID, not "pg:GROUP_ID"
    assert db.calls[0]["values"] == {"id": GROUP_ID}


def test_get_agent_pdp_resolves_ext_id_via_external_product_seeds(monkeypatch) -> None:
    """ext_* ids resolve through external_product_seeds.external_product_id
    → catalog_products.content_key → agent_pdp_view."""
    row = _row()
    client, db = _client(
        monkeypatch,
        [row],
        ext_id_to_content_key={"ext_xyz123": CK_A},
    )

    response = client.get("/api/agent/pdp/ext_xyz123")

    assert response.status_code == 200
    # Two queries: resolve ext → content_key, then SELECT by content_key
    assert len(db.calls) == 2
    assert "external_product_seeds eps" in db.calls[0]["query"]
    assert db.calls[0]["values"] == {"ext_id": "ext_xyz123"}
    assert "WHERE content_key = :id" in db.calls[1]["query"]
    assert db.calls[1]["values"] == {"id": CK_A}


def test_get_agent_pdp_unresolvable_ext_id_returns_404(monkeypatch) -> None:
    """ext_* that doesn't resolve to any content_key returns 404 without
    falling back to other dispatch paths."""
    client, db = _client(monkeypatch, [_row()], ext_id_to_content_key={})

    response = client.get("/api/agent/pdp/ext_missing")

    assert response.status_code == 404
    assert len(db.calls) == 1  # only the resolve attempt


def test_sql_uses_agent_pdp_view_indexed_lookup_paths() -> None:
    sql_by_kind = {
        "content_key": agent_pdp_v1.SELECT_BY_CONTENT_KEY_SQL,
        "signature": agent_pdp_v1.SELECT_BY_SIGNATURE_SQL,
        "product_group": agent_pdp_v1.SELECT_BY_PRODUCT_GROUP_SQL,
    }

    for sql in sql_by_kind.values():
        normalized = " ".join(sql.split())
        assert "SELECT *" not in normalized
        assert "FROM agent_pdp_view" in normalized
        assert " JOIN " not in f" {normalized.upper()} "
        assert "catalog_products" not in normalized
        assert "catalog_skus" not in normalized
        assert "catalog_offers" not in normalized
        assert "product_group_members" not in normalized
        assert "subject_resolve" not in normalized

    assert "WHERE content_key = :id" in " ".join(sql_by_kind["content_key"].split())
    assert "WHERE pivota_signature_id = :id" in " ".join(sql_by_kind["signature"].split())
    group_sql = " ".join(sql_by_kind["product_group"].split())
    assert "WHERE product_group_id = :id" in group_sql
    assert "CASE WHEN pivota_signature_id IS NOT NULL THEN 0 ELSE 1 END" in group_sql
    assert "content_key ASC" in group_sql
