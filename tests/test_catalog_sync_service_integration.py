from __future__ import annotations

from datetime import datetime, timezone

import pytest

import services.catalog_sync_service as module


def test_catalog_sync_service_utcnow_is_naive_utc() -> None:
    value = module._utcnow()

    assert isinstance(value, datetime)
    assert value.tzinfo is None


@pytest.mark.asyncio
async def test_sync_products_cache_to_catalog_dedupes_products_cache_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    async def fake_fetch_all(_query, _params):
        return [
            {
                "product_data": {
                    "id": "prod_1",
                    "product_id": "prod_1",
                    "platform": "shopify",
                    "title": "Vitamin C Serum",
                    "price": 28.0,
                    "currency": "USD",
                    "variants": [],
                }
            },
            {
                "product_data": {
                    "id": "prod_1",
                    "product_id": "prod_1",
                    "platform": "shopify",
                    "title": "Vitamin C Serum",
                    "price": 28.0,
                    "currency": "USD",
                    "variants": [],
                }
            },
            {
                "product_data": {
                    "id": "prod_2",
                    "product_id": "prod_2",
                    "platform": "shopify",
                    "title": "Niacinamide Serum",
                    "price": 24.0,
                    "currency": "USD",
                    "variants": [],
                }
            },
        ]

    async def fake_ingest_standard_products(**kwargs):
        observed.update(kwargs)
        return {"products_ingested": len(kwargs["product_payloads"])}

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(module, "ingest_standard_products", fake_ingest_standard_products)

    stats = await module.sync_products_cache_to_catalog(
        merchant_id="merch_1",
        platform="shopify",
        limit=100,
        include_expired=True,
        source_system="products_cache",
        source_ref="test",
        job_id="job_1",
    )

    assert stats["products_ingested"] == 2
    assert observed["merchant_id"] == "merch_1"
    assert len(observed["product_payloads"]) == 2
    assert {payload["product_id"] for payload in observed["product_payloads"]} == {"prod_1", "prod_2"}


@pytest.mark.asyncio
async def test_run_catalog_sync_job_marks_running_then_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        "job_id": "job_123",
        "merchant_id": "merch_1",
        "connector": "shopify",
        "mode": "reconcile",
        "scope_json": {
            "platform": "shopify",
            "limit": 250,
            "include_expired": True,
            "source_system": "products_cache",
        },
        "status": "pending",
        "stats_json": {},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    updates = []

    async def fake_get_catalog_sync_job(job_id: str):
        assert job_id == "job_123"
        return dict(stored)

    async def fake_upsert(_table, _pk_name, values):
        updates.append(dict(values))
        stored.update(values)

    async def fake_sync_products_cache_to_catalog(**kwargs):
        assert kwargs["merchant_id"] == "merch_1"
        assert kwargs["platform"] == "shopify"
        assert kwargs["limit"] == 250
        return {"products_ingested": 2, "skus_ingested": 2, "offers_ingested": 2}

    monkeypatch.setattr(module, "get_catalog_sync_job", fake_get_catalog_sync_job)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert)
    monkeypatch.setattr(module, "sync_products_cache_to_catalog", fake_sync_products_cache_to_catalog)

    result = await module.run_catalog_sync_job("job_123")

    assert updates[0]["status"] == "running"
    assert result["job_id"] == "job_123"
    assert result["status"] == "running" or result["status"] == "completed"


@pytest.mark.asyncio
async def test_record_catalog_sync_event_is_idempotent_for_same_source_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {}

    async def fake_upsert(_table, _pk_name, values):
        stored[values["event_id"]] = dict(values)

    async def fake_fetch_one_by_pk(_table, _pk_name, pk_value):
        return stored.get(pk_value)

    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert)
    monkeypatch.setattr(module, "_fetch_one_by_pk", fake_fetch_one_by_pk)

    first = await module.record_catalog_sync_event(
        merchant_id="merch_1",
        connector="shopify",
        event_type="products/update",
        topic="products/update",
        payload_json={"id": "prod_1"},
        source_ref="evt_1",
    )
    second = await module.record_catalog_sync_event(
        merchant_id="merch_1",
        connector="shopify",
        event_type="products/update",
        topic="products/update",
        payload_json={"id": "prod_1"},
        source_ref="evt_1",
    )

    assert first["event_id"] == second["event_id"]
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_resolve_merchant_name_uses_available_onboarding_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    async def fake_fetch_one(query):
        observed["selected_columns"] = [column.name for column in query.selected_columns]
        return {"business_name": "Staging Merchant"}

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    merchant_name = await module._resolve_merchant_name("merch_1")

    assert merchant_name == "Staging Merchant"
    assert observed["selected_columns"] == ["business_name"]


@pytest.mark.asyncio
async def test_ingest_standard_products_wraps_merchant_and_product_writes_in_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []

    class DummyTransaction:
        async def __aenter__(self):
            events.append("enter")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            events.append("exit")
            return False

    async def fake_upsert_catalog_merchant(**_kwargs):
        events.append("merchant_upsert")

    async def fake_upsert_by_pk(*_args, **_kwargs):
        events.append("upsert")

    async def fake_upsert_field_fact(*_args, **_kwargs):
        events.append("field_fact")

    async def fake_append_snapshot(*_args, **_kwargs):
        events.append("snapshot")

    async def fake_replace_child_rows_multi(*_args, **_kwargs):
        events.append("replace_children")
        return 0

    monkeypatch.setattr(module.database, "transaction", lambda: DummyTransaction())
    monkeypatch.setattr(module, "upsert_catalog_merchant", fake_upsert_catalog_merchant)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module, "_upsert_field_fact", fake_upsert_field_fact)
    monkeypatch.setattr(module, "_append_snapshot", fake_append_snapshot)
    monkeypatch.setattr(module, "_replace_child_rows_multi", fake_replace_child_rows_multi)

    stats = await module.ingest_standard_products(
        merchant_id="merch_1",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_1",
                "product_id": "prod_1",
                "merchant_id": "merch_1",
                "platform": "shopify",
                "title": "Vitamin C Serum",
                "price": 28.0,
                "currency": "USD",
                "variants": [],
            }
        ],
        source_system="test",
        source_ref="test_ref",
    )

    assert stats["products_ingested"] == 1
    assert events[:2] == ["enter", "merchant_upsert"]
    assert events.count("enter") == 2
    assert events.count("exit") == 2
