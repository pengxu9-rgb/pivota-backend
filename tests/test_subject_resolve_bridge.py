import asyncio
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient


from main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_subject_resolve_uuid_bridge_hit_returns_canonical(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.subject_resolve as subject_resolve

    async def fake_fetch_one(query: str, values: Optional[Dict[str, Any]] = None):
        if "FROM id_bridge" in str(query):
            if (values or {}).get("key_type") == "aurora_product_uuid":
                return {
                    "subject_kind": "canonical_product",
                    "merchant_id": "merch_efbc46b4619cfbdf",
                    "product_id": "9886499864904",
                    "product_group_id": None,
                }
        return None

    monkeypatch.setattr(subject_resolve.database, "fetch_one", fake_fetch_one)

    res = client.post(
        "/v1/subject/resolve",
        json={"aurora_product_uuid": "123e4567-e89b-12d3-a456-426614174000"},
        headers={"X-Request-Id": "rid_bridge_hit_1"},
    )
    assert res.status_code == 200
    body = res.json()

    assert body["schema"] == "resolve_subject_response.v1"
    assert body["resolved"] is True
    assert body["reason_code"] == "mapped_hit"
    assert body["bridge_hit"] is True
    assert body["latency_ms"] >= 0
    assert body.get("subject", {}).get("schema") == "pdp_target.v1"
    assert body.get("subject", {}).get("kind") == "canonical_product"
    assert body.get("subject", {}).get("product_ref") == {
        "merchant_id": "merch_efbc46b4619cfbdf",
        "product_id": "9886499864904",
    }


def test_subject_resolve_alias_hit_returns_product_group(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.subject_resolve as subject_resolve

    async def fake_fetch_one(query: str, values: Optional[Dict[str, Any]] = None):
        q = str(query)
        if "FROM id_bridge" in q:
            return None
        if "FROM product_group_members" in q:
            return {"product_group_id": "pg_9886499864904"}
        return None

    async def fake_fetch_all(query: str, values: Optional[Dict[str, Any]] = None):
        if "FROM products_cache" in str(query):
            return [
                {
                    "merchant_id": "merch_efbc46b4619cfbdf",
                    "platform_product_id": "9886499864904",
                    "product_data": {
                        "id": "9886499864904",
                        "title": "The Ordinary Niacinamide 10% + Zinc 1%",
                        "brand": "The Ordinary",
                    },
                }
            ]
        return []

    monkeypatch.setattr(subject_resolve.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(subject_resolve.database, "fetch_all", fake_fetch_all)

    res = client.post(
        "/v1/subject/resolve",
        json={"alias": "The Ordinary Niacinamide 10% + Zinc 1%", "brand": "The Ordinary"},
    )
    assert res.status_code == 200
    body = res.json()

    assert body["resolved"] is True
    assert body["reason_code"] == "mapped_hit"
    assert body["bridge_hit"] is False
    assert body.get("subject", {}).get("kind") == "product_group"
    assert body.get("subject", {}).get("product_group_id") == "pg_9886499864904"
    sources = body.get("metadata", {}).get("sources") or []
    assert any(s.get("source") == "products_cache_alias" and s.get("ok") is True for s in sources)


def test_subject_resolve_db_timeout_returns_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.subject_resolve as subject_resolve

    async def slow_fetch_one(query: str, values: Optional[Dict[str, Any]] = None):
        await asyncio.sleep(0.05)
        return None

    monkeypatch.setenv("SUBJECT_RESOLVE_DB_TIMEOUT_S", "0.01")
    monkeypatch.setattr(subject_resolve.database, "fetch_one", slow_fetch_one)

    res = client.post(
        "/v1/subject/resolve",
        json={"aurora_product_uuid": "123e4567-e89b-12d3-a456-426614174001"},
    )
    assert res.status_code == 200
    body = res.json()

    assert body["resolved"] is False
    assert body["reason_code"] == "db_timeout"
    assert body["subject"] is None
    sources = body.get("metadata", {}).get("sources") or []
    assert any(s.get("source") == "id_bridge" and s.get("reason_code") == "db_timeout" for s in sources)


def test_subject_resolve_no_candidates_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.subject_resolve as subject_resolve

    async def fake_fetch_one(query: str, values: Optional[Dict[str, Any]] = None):
        return None

    async def fake_fetch_all(query: str, values: Optional[Dict[str, Any]] = None):
        return []

    monkeypatch.setattr(subject_resolve.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(subject_resolve.database, "fetch_all", fake_fetch_all)

    res = client.post(
        "/v1/subject/resolve",
        json={"alias": "non-existent-product-alias", "brand": "unknown"},
    )
    assert res.status_code == 200
    body = res.json()

    assert body["schema"] == "resolve_subject_response.v1"
    assert body["resolved"] is False
    assert body["reason_code"] == "no_candidates"
    assert body["subject"] is None
    assert body["latency_ms"] >= 0
