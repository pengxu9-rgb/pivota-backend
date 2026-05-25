from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import services.catalog_sync_service as module


async def _generated_sku_key(**kwargs):
    return module.make_catalog_sku_key(kwargs["product_key"], kwargs["source_variant_id"])


async def _noop_execute(*_args, **_kwargs):
    return None


def test_catalog_sync_service_utcnow_is_naive_utc() -> None:
    value = module._utcnow()

    assert isinstance(value, datetime)
    assert value.tzinfo is None


def test_catalog_source_domain_migration_shape() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    up_sql = (repo_root / "db" / "migrations" / "133_catalog_source_domain.sql").read_text()
    down_sql = (
        repo_root
        / "db"
        / "migrations"
        / "down"
        / "133_catalog_source_domain_down.sql"
    ).read_text()

    for table in ("catalog_products", "catalog_skus", "catalog_offers"):
        assert f"ALTER TABLE IF EXISTS {table}" in up_sql
        assert "ADD COLUMN IF NOT EXISTS source_domain TEXT NULL" in up_sql
        assert f"ALTER TABLE IF EXISTS {table}" in down_sql
        assert "DROP COLUMN IF EXISTS source_domain" in down_sql


@pytest.mark.asyncio
async def test_resolve_catalog_sku_key_preserves_existing_source_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_one(_query):
        return {"sku_key": "prod::merch_1::shopify::prod_1::v::var_1"}

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    sku_key = await module._resolve_catalog_sku_key(
        merchant_id="merch_1",
        platform="shopify",
        product_key="prod::merch_1::shopify::prod_1",
        source_variant_id="var_1",
    )

    assert sku_key == "prod::merch_1::shopify::prod_1::v::var_1"


@pytest.mark.asyncio
async def test_resolve_catalog_sku_key_generates_when_source_identity_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_one(_query):
        return None

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    sku_key = await module._resolve_catalog_sku_key(
        merchant_id="merch_1",
        platform="shopify",
        product_key="prod::merch_1::shopify::prod_1",
        source_variant_id="var_1",
    )

    assert sku_key == "sku::prod::merch_1::shopify::prod_1::var_1"


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
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)
    monkeypatch.setattr(module.database, "execute", _noop_execute)

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


@pytest.mark.asyncio
async def test_ingest_standard_products_shopify_offer_guard_filters_invalid_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted_skus: set[str] = set()
    product_writes = []
    offer_writes = []
    audit_rows = []

    class DummyTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_upsert_catalog_merchant(**_kwargs):
        return None

    async def fake_upsert_by_pk(table, _pk_name, values):
        if getattr(table, "name", None) == "catalog_products":
            product_writes.append(dict(values))
        if getattr(table, "name", None) == "catalog_skus":
            inserted_skus.add(values["sku_key"])
        if getattr(table, "name", None) == "catalog_offers":
            offer_writes.append(dict(values))

    async def fake_fetch_all(_sql, _values):
        raise AssertionError("guarded offer ingest should validate against the SKU inserted in this transaction")

    async def fake_execute(*args, **_kwargs):
        if len(args) >= 2 and isinstance(args[1], dict) and args[1].get("writer_name"):
            audit_rows.append(dict(args[1]))
        return None

    async def fake_upsert_field_fact(*_args, **_kwargs):
        return None

    async def fake_append_snapshot(*_args, **_kwargs):
        return None

    async def fake_replace_child_rows_multi(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(module.database, "transaction", lambda: DummyTransaction())
    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module, "upsert_catalog_merchant", fake_upsert_catalog_merchant)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module, "_upsert_field_fact", fake_upsert_field_fact)
    monkeypatch.setattr(module, "_append_snapshot", fake_append_snapshot)
    monkeypatch.setattr(module, "_replace_child_rows_multi", fake_replace_child_rows_multi)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)

    stats = await module.ingest_standard_products(
        merchant_id="merch_guard",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_guard",
                "product_id": "prod_guard",
                "merchant_id": "merch_guard",
                "platform": "shopify",
                "title": "Vitamin C Serum",
                "price": 12.0,
                "currency": "USD",
                "variants": [
                    {"id": "v_valid", "title": "Valid", "price": 12.0, "inventory_quantity": 2},
                    {"id": "v_zero", "title": "Zero", "price": 0.0, "inventory_quantity": 2},
                    {"id": "v_negative", "title": "Negative", "price": -1.0, "inventory_quantity": 2},
                ],
            }
        ],
        source_system="shopify_products_sync",
        source_ref="batch_guard",
        source_domain="guard-shop.myshopify.com",
    )

    assert stats["offers_ingested"] == 1
    assert stats["offers_skipped"] == 2
    assert stats["offer_skip_reasons"] == {"zero_or_missing_price": 2}
    assert product_writes[0]["source_domain"] == "guard-shop.myshopify.com"
    assert len(offer_writes) == 1
    assert offer_writes[0]["source_domain"] == "guard-shop.myshopify.com"
    assert offer_writes[0]["offer_payload"]["variant_id"] == "v_valid"
    assert len(audit_rows) == 1
    assert audit_rows[0]["writer_name"] == "shopify_products_sync"
    assert audit_rows[0]["batch_id"] == "batch_guard"
    assert audit_rows[0]["applied_rows"] == 1
    assert audit_rows[0]["skipped_rows"] == 2
    assert '"zero_or_missing_price": 2' in audit_rows[0]["reasons"]


@pytest.mark.asyncio
async def test_ingest_standard_products_wix_offer_guard_filters_invalid_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted_skus: set[str] = set()
    product_writes = []
    offer_writes = []
    audit_rows = []
    wix_source_system = "universal_product_sync"

    class DummyTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_upsert_catalog_merchant(**_kwargs):
        return None

    async def fake_upsert_by_pk(table, _pk_name, values):
        if getattr(table, "name", None) == "catalog_products":
            product_writes.append(dict(values))
        if getattr(table, "name", None) == "catalog_skus":
            inserted_skus.add(values["sku_key"])
        if getattr(table, "name", None) == "catalog_offers":
            offer_writes.append(dict(values))

    async def fake_fetch_all(_sql, _values):
        raise AssertionError("guarded offer ingest should validate against the SKU inserted in this transaction")

    async def fake_execute(*args, **_kwargs):
        if len(args) >= 2 and isinstance(args[1], dict) and args[1].get("writer_name"):
            audit_rows.append(dict(args[1]))
        return None

    async def fake_upsert_field_fact(*_args, **_kwargs):
        return None

    async def fake_append_snapshot(*_args, **_kwargs):
        return None

    async def fake_replace_child_rows_multi(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(module.database, "transaction", lambda: DummyTransaction())
    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module, "upsert_catalog_merchant", fake_upsert_catalog_merchant)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module, "_upsert_field_fact", fake_upsert_field_fact)
    monkeypatch.setattr(module, "_append_snapshot", fake_append_snapshot)
    monkeypatch.setattr(module, "_replace_child_rows_multi", fake_replace_child_rows_multi)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)

    stats = await module.ingest_standard_products(
        merchant_id="merch_guard",
        platform="wix",
        product_payloads=[
            {
                "id": "prod_guard",
                "product_id": "prod_guard",
                "merchant_id": "merch_guard",
                "platform": "wix",
                "title": "Vitamin C Serum",
                "price": 12.0,
                "currency": "USD",
                "variants": [
                    {"id": "v_valid", "title": "Valid", "price": 12.0, "inventory_quantity": 2},
                    {"id": "v_zero", "title": "Zero", "price": 0.0, "inventory_quantity": 2},
                    {"id": "v_negative", "title": "Negative", "price": -1.0, "inventory_quantity": 2},
                ],
            }
        ],
        source_system=wix_source_system,
        source_ref="batch_guard",
        source_domain="guard-shop.wixsite.com",
    )

    assert stats["offers_ingested"] == 1
    assert stats["offers_skipped"] == 2
    assert stats["offer_skip_reasons"] == {"zero_or_missing_price": 2}
    assert product_writes[0]["source_domain"] == "guard-shop.wixsite.com"
    assert len(offer_writes) == 1
    assert offer_writes[0]["source_domain"] == "guard-shop.wixsite.com"
    assert offer_writes[0]["offer_payload"]["variant_id"] == "v_valid"
    assert len(audit_rows) == 1
    assert audit_rows[0]["writer_name"] == wix_source_system
    assert audit_rows[0]["batch_id"] == "batch_guard"
    assert audit_rows[0]["applied_rows"] == 1
    assert audit_rows[0]["skipped_rows"] == 2
    assert '"zero_or_missing_price": 2' in audit_rows[0]["reasons"]


@pytest.mark.asyncio
async def test_ingest_standard_products_propagates_source_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = {
        "catalog_products": [],
        "catalog_skus": [],
        "catalog_offers": [],
    }

    class DummyTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_upsert_catalog_merchant(**_kwargs):
        return None

    async def fake_upsert_by_pk(table, _pk_name, values):
        table_name = getattr(table, "name", None)
        if table_name in writes:
            writes[table_name].append(dict(values))

    async def fake_upsert_field_fact(*_args, **_kwargs):
        return None

    async def fake_append_snapshot(*_args, **_kwargs):
        return None

    async def fake_replace_child_rows_multi(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(module.database, "transaction", lambda: DummyTransaction())
    monkeypatch.setattr(module.database, "execute", _noop_execute)
    monkeypatch.setattr(module, "upsert_catalog_merchant", fake_upsert_catalog_merchant)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module, "_upsert_field_fact", fake_upsert_field_fact)
    monkeypatch.setattr(module, "_append_snapshot", fake_append_snapshot)
    monkeypatch.setattr(module, "_replace_child_rows_multi", fake_replace_child_rows_multi)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)
    monkeypatch.setattr(module, "_schedule_fashion_enrichment", lambda **_kwargs: None)

    stats = await module.ingest_standard_products(
        merchant_id="merch_source_domain",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_source_domain",
                "product_id": "prod_source_domain",
                "merchant_id": "merch_source_domain",
                "platform": "shopify",
                "title": "Source Domain Serum",
                "price": 29.0,
                "currency": "USD",
                "variants": [
                    {
                        "id": "v_source_domain",
                        "title": "Default",
                        "price": 29.0,
                        "inventory_quantity": 5,
                    },
                ],
            }
        ],
        source_system="shopify_products_sync",
        source_ref="batch_source_domain",
        source_domain="source-domain.example",
    )

    assert stats["products_ingested"] == 1
    assert stats["skus_ingested"] == 1
    assert stats["offers_ingested"] == 1
    assert writes["catalog_products"][0]["source_domain"] == "source-domain.example"
    assert writes["catalog_skus"][0]["source_domain"] == "source-domain.example"
    assert writes["catalog_offers"][0]["source_domain"] == "source-domain.example"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "source_system"),
    [
        ("shopify", "shopify_products_sync"),
        ("wix", "universal_product_sync"),
    ],
)
async def test_ingest_standard_products_captures_strong_identifiers_into_sku_barcode(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    source_system: str,
) -> None:
    sku_writes = []
    audit_rows = []

    class DummyTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_upsert_catalog_merchant(**_kwargs):
        return None

    async def fake_upsert_by_pk(table, _pk_name, values):
        if getattr(table, "name", None) == "catalog_skus":
            sku_writes.append(dict(values))

    async def fake_execute(*args, **_kwargs):
        if len(args) >= 2 and isinstance(args[1], dict) and args[1].get("writer_name"):
            audit_rows.append(dict(args[1]))
        return None

    async def fake_upsert_field_fact(*_args, **_kwargs):
        return None

    async def fake_append_snapshot(*_args, **_kwargs):
        return None

    async def fake_replace_child_rows_multi(*_args, **_kwargs):
        return 0

    async def fake_fold_category_with_llm_fallback(**_kwargs):
        return None

    monkeypatch.setattr(module.database, "transaction", lambda: DummyTransaction())
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module, "upsert_catalog_merchant", fake_upsert_catalog_merchant)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module, "_upsert_field_fact", fake_upsert_field_fact)
    monkeypatch.setattr(module, "_append_snapshot", fake_append_snapshot)
    monkeypatch.setattr(module, "_replace_child_rows_multi", fake_replace_child_rows_multi)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)
    monkeypatch.setattr(module, "fold_category_with_llm_fallback", fake_fold_category_with_llm_fallback)
    monkeypatch.setattr(module, "_schedule_fashion_enrichment", lambda **_kwargs: None)

    stats = await module.ingest_standard_products(
        merchant_id="merch_barcode",
        platform=platform,
        product_payloads=[
            {
                "id": "prod_barcode",
                "product_id": "prod_barcode",
                "merchant_id": "merch_barcode",
                "platform": platform,
                "title": "Barrier Repair Serum",
                "price": 12.0,
                "currency": "USD",
                "variants": [
                    {"id": "v_gtin13", "title": "GTIN-13", "price": 12.0, "inventory_quantity": 2, "gtin": "1234567890123"},
                    {"id": "v_upc12", "title": "UPC-12", "price": 12.0, "inventory_quantity": 2, "upc": "123456789012"},
                    {"id": "v_gtin8", "title": "GTIN-8", "price": 12.0, "inventory_quantity": 2, "gtin": "12345678"},
                    {"id": "v_formatted", "title": "Formatted", "price": 12.0, "inventory_quantity": 2, "barcode": "0-12345-67890-5"},
                    {"id": "v_missing", "title": "Missing", "price": 12.0, "inventory_quantity": 2},
                    {"id": "v_garbage", "title": "Garbage", "price": 12.0, "inventory_quantity": 2, "barcode": "N/A"},
                ],
            }
        ],
        source_system=source_system,
        source_ref="batch_barcode",
    )

    by_variant = {row["source_variant_id"]: row for row in sku_writes}
    assert by_variant["v_gtin13"]["barcode"] == "1234567890123"
    assert by_variant["v_upc12"]["barcode"] == "123456789012"
    assert by_variant["v_gtin8"]["barcode"] == "12345678"
    assert by_variant["v_formatted"]["barcode"] == "012345678905"
    assert by_variant["v_missing"]["barcode"] is None
    assert by_variant["v_garbage"]["barcode"] is None
    assert stats["skus_ingested"] == 6
    assert stats["offers_ingested"] == 6
    assert stats["offers_skipped"] == 0
    assert len(audit_rows) == 1
    assert audit_rows[0]["writer_name"] == source_system
    assert audit_rows[0]["skipped_rows"] == 0
    assert '"no_strong_identifier": 2' in audit_rows[0]["reasons"]


@pytest.mark.asyncio
async def test_ingest_standard_products_persists_merchant_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase O-1: StandardProduct.tags[] from the merchant feed must reach
    catalog_products.tags. Before mig 075 + this wiring, the field was
    populated upstream and silently dropped at ingest. See
    docs/PDP_ONBOARDING_PLAYBOOK.md gap #2."""

    catalog_products_writes: list[dict] = []

    class DummyTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_upsert_catalog_merchant(**_kwargs):
        return None

    async def fake_upsert_by_pk(table, _pk_name, values):
        if getattr(table, "name", None) == "catalog_products":
            catalog_products_writes.append(dict(values))

    async def fake_upsert_field_fact(*_args, **_kwargs):
        return None

    async def fake_append_snapshot(*_args, **_kwargs):
        return None

    async def fake_replace_child_rows_multi(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(module.database, "transaction", lambda: DummyTransaction())
    monkeypatch.setattr(module, "upsert_catalog_merchant", fake_upsert_catalog_merchant)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module, "_upsert_field_fact", fake_upsert_field_fact)
    monkeypatch.setattr(module, "_append_snapshot", fake_append_snapshot)
    monkeypatch.setattr(module, "_replace_child_rows_multi", fake_replace_child_rows_multi)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)
    monkeypatch.setattr(module.database, "execute", _noop_execute)

    # Product WITH tags — merchant has diligently tagged it.
    await module.ingest_standard_products(
        merchant_id="merch_tagged",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_with_tags",
                "product_id": "prod_with_tags",
                "merchant_id": "merch_tagged",
                "platform": "shopify",
                "title": "Vitamin C Serum",
                "price": 28.0,
                "currency": "USD",
                "tags": ["serum", "vitamin-c", "anti-aging"],
                "variants": [],
            }
        ],
        source_system="test",
        source_ref="test_ref",
    )

    assert len(catalog_products_writes) == 1
    write = catalog_products_writes[0]
    assert "tags" in write, (
        "catalog_products write must include tags column (Phase O-1)"
    )
    assert write["tags"] == ["serum", "vitamin-c", "anti-aging"]

    # Product WITHOUT tags — must still write [] (not NULL or missing key)
    # so future operators can distinguish "ingest saw merchant feed and
    # it was empty" from "row predates the column".
    catalog_products_writes.clear()
    await module.ingest_standard_products(
        merchant_id="merch_untagged",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_no_tags",
                "product_id": "prod_no_tags",
                "merchant_id": "merch_untagged",
                "platform": "shopify",
                "title": "Generic Product",
                "price": 10.0,
                "currency": "USD",
                "variants": [],
            }
        ],
        source_system="test",
        source_ref="test_ref",
    )

    assert len(catalog_products_writes) == 1
    write = catalog_products_writes[0]
    assert write.get("tags") == [], (
        "catalog_products write must include tags=[] when merchant feed has no tags"
    )


@pytest.mark.asyncio
async def test_ingest_standard_products_writes_o2_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase O-2: ingest_standard_products must populate price_tier /
    use_case_tags / lifestyle_tags / demographic via derive_taxonomy_v1.
    Asserts the four new columns land in the catalog_products values
    dict alongside the existing fields."""

    catalog_products_writes: list[dict] = []

    class DummyTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_upsert_catalog_merchant(**_kwargs):
        return None

    async def fake_upsert_by_pk(table, _pk_name, values):
        if getattr(table, "name", None) == "catalog_products":
            catalog_products_writes.append(dict(values))

    async def fake_upsert_field_fact(*_args, **_kwargs):
        return None

    async def fake_append_snapshot(*_args, **_kwargs):
        return None

    async def fake_replace_child_rows_multi(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(module.database, "transaction", lambda: DummyTransaction())
    monkeypatch.setattr(module, "upsert_catalog_merchant", fake_upsert_catalog_merchant)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module, "_upsert_field_fact", fake_upsert_field_fact)
    monkeypatch.setattr(module, "_append_snapshot", fake_append_snapshot)
    monkeypatch.setattr(module, "_replace_child_rows_multi", fake_replace_child_rows_multi)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)
    monkeypatch.setattr(module.database, "execute", _noop_execute)

    # Product with price + lifestyle + demographic + use-case signals.
    await module.ingest_standard_products(
        merchant_id="merch_o2",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_o2",
                "product_id": "prod_o2",
                "merchant_id": "merch_o2",
                "platform": "shopify",
                "title": "Vegan Daily Moisturizer for Women",
                "description": "Cruelty-free, fragrance-free formula for everyday use.",
                "price": 75.0,
                "currency": "USD",
                "tags": ["k-beauty"],
                "variants": [],
            }
        ],
        source_system="test",
        source_ref="test_ref",
    )

    assert len(catalog_products_writes) == 1
    write = catalog_products_writes[0]
    assert write["price_tier"] == "50_100"
    assert "vegan" in (write.get("lifestyle_tags") or [])
    assert "cruelty_free" in (write.get("lifestyle_tags") or [])
    assert "fragrance_free" in (write.get("lifestyle_tags") or [])
    assert "daily" in (write.get("use_case_tags") or [])
    assert write["demographic"] == "women"

    # Product with no taxonomy signals → empty lists / None scalars,
    # column never missing.
    catalog_products_writes.clear()
    await module.ingest_standard_products(
        merchant_id="merch_o2_blank",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_blank",
                "product_id": "prod_blank",
                "merchant_id": "merch_o2_blank",
                "platform": "shopify",
                "title": "Generic Item",
                "price": 250.0,
                "currency": "USD",
                "variants": [],
            }
        ],
        source_system="test",
        source_ref="test_ref",
    )

    assert len(catalog_products_writes) == 1
    write = catalog_products_writes[0]
    assert write["price_tier"] == "200_500"  # always derivable from price
    assert write["use_case_tags"] == []
    assert write["lifestyle_tags"] == []
    assert write["demographic"] is None  # NULL is correct here


@pytest.mark.asyncio
async def test_ingest_standard_products_writes_o4_lifecycle_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase O-4: ingest_standard_products must compute and persist
    pdp_lifecycle_stage on every Path A write. Without this, the recall
    live-stage filter (O-5) treats Shopify ingest rows as draft and
    drops them from the candidate set."""

    catalog_products_writes: list[dict] = []

    class DummyTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_upsert_catalog_merchant(**_kwargs):
        return None

    async def fake_upsert_by_pk(table, _pk_name, values):
        if getattr(table, "name", None) == "catalog_products":
            catalog_products_writes.append(dict(values))

    async def fake_upsert_field_fact(*_args, **_kwargs):
        return None

    async def fake_append_snapshot(*_args, **_kwargs):
        return None

    async def fake_replace_child_rows_multi(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(module.database, "transaction", lambda: DummyTransaction())
    monkeypatch.setattr(module, "upsert_catalog_merchant", fake_upsert_catalog_merchant)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module, "_upsert_field_fact", fake_upsert_field_fact)
    monkeypatch.setattr(module, "_append_snapshot", fake_append_snapshot)
    monkeypatch.setattr(module, "_replace_child_rows_multi", fake_replace_child_rows_multi)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)
    monkeypatch.setattr(module.database, "execute", _noop_execute)

    # Validated-grade row: title + image + long description + taxonomy
    # signals via derive_taxonomy_v1. Phase O-5 classifies category_path
    # inline at sync time (services.pdp_category_classifier.fold_category_from_variants),
    # so a recognizable title like "Moisturizer" now promotes the row to
    # 'validated' on the initial Path A write. (Previously, category_path
    # was hard-coded to None and the row stopped at 'candidate' until the
    # backfill classifier ran.)
    await module.ingest_standard_products(
        merchant_id="merch_o4",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_o4_valid",
                "product_id": "prod_o4_valid",
                "merchant_id": "merch_o4",
                "platform": "shopify",
                "title": "Vegan Daily Moisturizer for Women",
                "description": "Cruelty-free fragrance-free moisturizer for everyday hydration without irritation.",
                "image_url": "https://example.com/img.jpg",
                "price": 28.0,
                "currency": "USD",
                "tags": [],
                "variants": [],
            }
        ],
        source_system="test",
        source_ref="test_ref",
    )

    assert len(catalog_products_writes) == 1
    write = catalog_products_writes[0]
    assert "pdp_lifecycle_stage" in write, (
        "Path A write must include pdp_lifecycle_stage column (Phase O-4)"
    )
    # Phase O-5: classifier hits inline → row promotes to validated.
    assert write["pdp_lifecycle_stage"] == "validated"
    # Phase O-5: confirm the new category_path + provenance columns are populated.
    assert write["category_path"] == "beauty/skincare/moisturize/cream"
    assert write["category_label_source"] == "merchant_payload"
    assert write["category_confidence"] == 1.0

    # Draft-grade row: missing image + short description → can't even
    # promote to candidate. Must still write the column (so writes don't
    # NULL it on conflict update).
    catalog_products_writes.clear()
    await module.ingest_standard_products(
        merchant_id="merch_o4_thin",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_o4_thin",
                "product_id": "prod_o4_thin",
                "merchant_id": "merch_o4_thin",
                "platform": "shopify",
                "title": "Bare Bones Item",
                "price": 5.0,
                "currency": "USD",
                "variants": [],
            }
        ],
        source_system="test",
        source_ref="test_ref",
    )

    assert len(catalog_products_writes) == 1
    write = catalog_products_writes[0]
    assert write.get("pdp_lifecycle_stage") == "draft"


@pytest.mark.asyncio
async def test_ingest_standard_products_passes_through_shopify_metafields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase O-5b (#3): when a merchant publishes a Shopify metafield like
    custom.material, the value flows into catalog_products with
    source='merchant_payload' confidence=1.0. Authoritative path — wins
    over the LLM extractor v2 (which is the fallback)."""
    from services import catalog_sync_service as module

    catalog_products_writes: List[Dict[str, Any]] = []

    async def _capture_upsert(table, primary_key, payload, *, conflict_update=None):
        # Mirror the recording-stub pattern used by the existing O-4 test:
        # only capture writes to catalog_products; let other tables no-op.
        if getattr(table, "name", None) == "catalog_products":
            catalog_products_writes.append(dict(payload))

    async def _noop_upsert_merchant(**kwargs):
        return None

    async def _noop_upsert_field_fact(**kwargs):
        return None

    async def _generated_sku_key(**kwargs):
        return f"sku::{kwargs.get('product_key')}::{kwargs.get('source_variant_id')}"

    async def _noop_execute(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "_upsert_by_pk", _capture_upsert)
    monkeypatch.setattr(module, "upsert_catalog_merchant", _noop_upsert_merchant)
    monkeypatch.setattr(module, "_upsert_field_fact", _noop_upsert_field_fact)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)
    monkeypatch.setattr(module.database, "execute", _noop_execute)

    await module.ingest_standard_products(
        merchant_id="merch_fashion",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_fashion_1",
                "product_id": "prod_fashion_1",
                "merchant_id": "merch_fashion",
                "platform": "shopify",
                "title": "Linen Summer Dress",
                "description": "A breezy linen dress for warm days.",
                "image_url": "https://example.com/dress.jpg",
                "price": 89.0,
                "currency": "USD",
                "product_type": "Dress",
                "tags": [],
                "variants": [],
                "platform_metadata": {
                    # Shopify standard metafield shape.
                    "metafields": [
                        {"namespace": "shopify", "key": "material",
                         "value": "100% organic linen", "type": "single_line_text_field"},
                        {"namespace": "custom", "key": "care_instructions",
                         "value": "Hand wash cold; lay flat to dry."},
                    ],
                },
            }
        ],
        source_system="test",
        source_ref="test_ref",
    )

    assert len(catalog_products_writes) == 1
    write = catalog_products_writes[0]
    # Merchant-published values flow through with the highest trust tier.
    assert write["material"] == "100% organic linen"
    assert write["material_source"] == "merchant_payload"
    assert write["material_confidence"] == 1.0
    assert write["care"] == "Hand wash cold; lay flat to dry."
    assert write["care_source"] == "merchant_payload"
    assert write["care_confidence"] == 1.0
    # size_guide was not provided → column stays out of the upsert dict
    # (preserves NULL so the fallback LLM extractor can fill in later
    # without racing the merchant_payload write).
    assert "size_guide" not in write


@pytest.mark.asyncio
async def test_ingest_standard_products_omits_fashion_keys_when_no_metafields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No metafields = no fashion keys in the upsert dict (don't NULL out
    a value some other path may have set)."""
    from services import catalog_sync_service as module

    catalog_products_writes: List[Dict[str, Any]] = []

    async def _capture_upsert(table, primary_key, payload, *, conflict_update=None):
        if getattr(table, "name", None) == "catalog_products":
            catalog_products_writes.append(dict(payload))

    async def _noop(*args, **kwargs):
        return None

    async def _generated_sku_key(**kwargs):
        return f"sku::{kwargs.get('product_key')}::{kwargs.get('source_variant_id')}"

    monkeypatch.setattr(module, "_upsert_by_pk", _capture_upsert)
    monkeypatch.setattr(module, "upsert_catalog_merchant", _noop)
    monkeypatch.setattr(module, "_upsert_field_fact", _noop)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)
    monkeypatch.setattr(module.database, "execute", _noop)

    await module.ingest_standard_products(
        merchant_id="merch_no_meta",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_no_meta",
                "product_id": "prod_no_meta",
                "merchant_id": "merch_no_meta",
                "platform": "shopify",
                "title": "Plain Item",
                "price": 10.0,
                "currency": "USD",
                "variants": [],
            }
        ],
        source_system="test",
        source_ref="test_ref",
    )
    assert len(catalog_products_writes) == 1
    write = catalog_products_writes[0]
    for k in ("material", "material_source", "material_confidence",
              "care", "care_source", "care_confidence",
              "size_guide", "size_guide_source", "size_guide_confidence"):
        assert k not in write, f"fashion field {k} unexpectedly in upsert dict"
