from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

import services.catalog_sync_service as module


async def _generated_sku_key(**kwargs):
    return module.make_catalog_sku_key(kwargs["product_key"], kwargs["source_variant_id"])


async def _noop_execute(*_args, **_kwargs):
    return None


class _DummyTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _PruneFakeDatabase:
    def __init__(self) -> None:
        self.products: dict[str, dict] = {}
        self.skus: dict[str, dict] = {}
        self.offers: dict[str, dict] = {}
        self.audit_rows: list[dict] = []
        self.stale_product_keys: list[str] = []
        self.stale_sku_keys: list[str] = []
        self.stale_offer_ids: list[str] = []

    def transaction(self):
        return _DummyTransaction()

    def add_catalog_tree(self, *, source_domain: str, source_product_id: str) -> tuple[str, str, str]:
        product_key = module.make_catalog_product_key("merch_shared", "shopify", source_product_id)
        sku_key = module.make_catalog_sku_key(product_key, f"{source_product_id}_variant")
        offer_id = module.make_catalog_offer_id(sku_key, "default", "internal_merchant")
        common = {
            "merchant_id": "merch_shared",
            "platform": "shopify",
            "source_product_id": source_product_id,
            "source_domain": source_domain,
            "suppression_reason": None,
            "suppressed_at": None,
            "suppression_metadata": None,
        }
        self.products[product_key] = {
            **common,
            "product_key": product_key,
            "source_system": "shopify_products_sync",
        }
        self.skus[sku_key] = {
            **common,
            "sku_key": sku_key,
            "product_key": product_key,
        }
        self.offers[offer_id] = {
            "offer_id": offer_id,
            "sku_key": sku_key,
            "product_key": product_key,
            "merchant_id": "merch_shared",
            "source_domain": source_domain,
            "suppression_reason": None,
            "suppressed_at": None,
            "suppression_metadata": None,
        }
        return product_key, sku_key, offer_id

    async def execute(self, query, values=None):
        sql = str(query)
        params = values or {}
        if "INSERT INTO writer_audit_log" in sql:
            self.audit_rows.append(dict(params))
            return None
        if "CREATE TEMP TABLE stale_catalog_products" in sql:
            valid_ids = {str(item) for item in params.get("valid_source_product_ids") or []}
            prune_all = bool(params.get("prune_all"))
            self.stale_product_keys = [
                row["product_key"]
                for row in self.products.values()
                if row["merchant_id"] == params["merchant_id"]
                and row["platform"] == params["platform"]
                and row["source_system"] == params["source_system"]
                and row["source_domain"] == params["source_domain"]
                and (prune_all or row["source_product_id"] not in valid_ids)
            ]
            return None
        if "CREATE TEMP TABLE stale_catalog_skus" in sql:
            self.stale_sku_keys = [
                row["sku_key"]
                for row in self.skus.values()
                if row["merchant_id"] == params["merchant_id"]
                and row["platform"] == params["platform"]
                and row["source_domain"] == params["source_domain"]
                and row["product_key"] in self.stale_product_keys
            ]
            return None
        if "CREATE TEMP TABLE stale_catalog_offers" in sql:
            self.stale_offer_ids = [
                row["offer_id"]
                for row in self.offers.values()
                if row["merchant_id"] == params["merchant_id"]
                and row["source_domain"] == params["source_domain"]
                and (
                    row["product_key"] in self.stale_product_keys
                    or row["sku_key"] in self.stale_sku_keys
                )
            ]
            return None
        return None

    async def fetch_all(self, query, values=None):
        sql = str(query)
        # The prune now fetches ALL stale keys (first 10 feed the audit
        # sample; the full list feeds the post-commit trust-row recompute).
        if "FROM stale_catalog_products" in sql:
            return [{"product_key": key} for key in sorted(self.stale_product_keys)]
        return []

    async def fetch_val(self, query, values=None):
        sql = str(query)
        params = values or {}
        if "SELECT count(*) FROM stale_catalog_products" in sql:
            return len(self.stale_product_keys)
        if "UPDATE catalog_products" in sql:
            return self._tombstone(self.products, set(self.stale_product_keys), "product_key", params)
        if "UPDATE catalog_skus" in sql:
            return self._tombstone(self.skus, set(self.stale_sku_keys), "sku_key", params)
        if "UPDATE catalog_offers" in sql:
            return self._tombstone(self.offers, set(self.stale_offer_ids), "offer_id", params)
        return 0

    def _tombstone(self, table: dict[str, dict], keys: set[str], key_name: str, params: dict) -> int:
        count = 0
        for row in table.values():
            if row[key_name] not in keys or row.get("suppressed_at") is not None:
                continue
            row["suppression_reason"] = params["suppression_reason"]
            row["suppressed_at"] = "now"
            row["suppression_metadata"] = {
                "sync_run_id": params["sync_run_id"],
                "pruned_by": params["pruned_by"],
                "source_domain": params["source_domain"],
            }
            count += 1
        return count


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


def test_catalog_stale_suppression_migration_shape() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    up_sql = (
        repo_root
        / "db"
        / "migrations"
        / "135_catalog_product_sku_stale_suppression.sql"
    ).read_text()
    down_sql = (
        repo_root
        / "db"
        / "migrations"
        / "down"
        / "135_catalog_product_sku_stale_suppression_down.sql"
    ).read_text()

    for table in ("catalog_products", "catalog_skus"):
        assert f"ALTER TABLE IF EXISTS {table}" in up_sql
        assert "ADD COLUMN IF NOT EXISTS suppression_reason TEXT NULL" in up_sql
        assert "ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ NULL" in up_sql
        assert "ADD COLUMN IF NOT EXISTS suppression_metadata JSONB NULL" in up_sql
        assert f"ALTER TABLE IF EXISTS {table}" in down_sql
        assert "DROP COLUMN IF EXISTS suppression_metadata" in down_sql
        assert "DROP COLUMN IF EXISTS suppressed_at" in down_sql
        assert "DROP COLUMN IF EXISTS suppression_reason" in down_sql

    assert "ALTER TABLE IF EXISTS catalog_offers" in up_sql
    assert "ADD COLUMN IF NOT EXISTS suppression_metadata JSONB NULL" in up_sql
    assert "DROP COLUMN IF EXISTS suppression_metadata" in down_sql


@pytest.mark.asyncio
async def test_upsert_field_fact_uses_logical_key_and_prunes_run_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed_sql: list[str] = []
    upsert_rows: list[dict] = []

    async def fake_execute(query, values=None):
        executed_sql.append(str(query))
        return None

    async def fake_upsert_by_pk(table, pk_name, values):
        upsert_rows.append(dict(values))
        return None

    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)

    common = {
        "entity_type": "sku",
        "entity_id": "sku_1",
        "field_family": "inventory",
        "field_key": "availability",
        "source_system": "shopify_products_sync",
        "observed_at": datetime(2026, 6, 6, 12, 0, 0),
    }
    await module._upsert_field_fact(
        **common,
        source_ref="shopify_products_sync:merch_1:2026-06-06T12:00:00",
        value={"available": True},
    )
    await module._upsert_field_fact(
        **common,
        source_ref="shopify_products_sync:merch_1:2026-06-06T12:05:00",
        value={"available": False},
    )

    expected_fact_id = module._stable_key(
        "fact",
        "sku",
        "sku_1",
        "inventory",
        "availability",
        "shopify_products_sync",
    )
    assert [row["fact_id"] for row in upsert_rows] == [expected_fact_id, expected_fact_id]
    assert upsert_rows[0]["source_ref"] != upsert_rows[1]["source_ref"]
    assert len(executed_sql) == 2
    assert all("DELETE FROM catalog_field_facts" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_emit_product_projection_facts_covers_content_taxonomy_and_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict] = []

    async def fake_upsert_field_fact(**kwargs):
        emitted.append(kwargs)

    monkeypatch.setattr(module, "_upsert_field_fact", fake_upsert_field_fact)
    product = module.StandardProduct(
        id="product_1",
        product_id="product_1",
        platform="shopify",
        merchant_id="merchant_1",
        title="Barrier Cream",
        description="A rich daily moisturizer.",
        product_type="Moisturizer",
        tags=["barrier", "barrier", "dry skin"],
        price=24.0,
        image_url="https://cdn.example/primary.jpg",
        images=["https://cdn.example/primary.jpg", "https://cdn.example/detail.jpg"],
    )

    await module._emit_product_projection_facts(
        product=product,
        product_key="prod::merchant_1::shopify::product_1",
        description="A rich daily moisturizer.",
        category_path="beauty.skincare.moisturizer",
        category_label_source="merchant_payload",
        category_confidence=1.0,
        normalized_category="moisturizer",
        source_system="shopify_products_sync",
        source_ref="sync_1",
        merchant_id="merchant_1",
        commerce_index_source={
            "source_id": "ci_source_merchant_1_shopify",
            "field_source_kind": "merchant_api",
        },
    )

    assert [(row["field_family"], row["field_key"]) for row in emitted] == [
        ("content", "description"),
        ("taxonomy", "classification"),
        ("media", "images"),
    ]
    assert emitted[1]["value"]["tags"] == ["barrier", "dry skin"]
    assert emitted[2]["value"] == {
        "primary": "https://cdn.example/primary.jpg",
        "items": ["https://cdn.example/primary.jpg", "https://cdn.example/detail.jpg"],
    }
    assert all(row["commerce_index_source_id"] == "ci_source_merchant_1_shopify" for row in emitted)


@pytest.mark.asyncio
async def test_emit_product_projection_facts_emits_tombstones_when_fields_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict] = []

    async def fake_upsert_field_fact(**kwargs):
        emitted.append(kwargs)

    monkeypatch.setattr(module, "_upsert_field_fact", fake_upsert_field_fact)
    product = module.StandardProduct(
        id="product_1",
        product_id="product_1",
        platform="shopify",
        merchant_id="merchant_1",
        title="Barrier Cream",
        description=None,
        product_type=None,
        tags=[],
        price=24.0,
        image_url=None,
        images=[],
    )

    await module._emit_product_projection_facts(
        product=product,
        product_key="prod::merchant_1::shopify::product_1",
        description=None,
        category_path=None,
        category_label_source=None,
        category_confidence=None,
        normalized_category=None,
        source_system="shopify_products_sync",
        source_ref="sync_2",
        merchant_id="merchant_1",
        commerce_index_source={
            "source_id": "ci_source_merchant_1_shopify",
            "field_source_kind": "merchant_api",
        },
    )

    assert [(row["field_family"], row["field_key"], row["value"]) for row in emitted] == [
        ("content", "description", None),
        ("taxonomy", "classification", None),
        ("media", "images", None),
    ]


@pytest.mark.asyncio
async def test_upsert_field_fact_emits_v2_delta_only_when_feature_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict] = []

    async def fake_fetch_one(_query):
        return None

    async def fake_execute(_query, _values=None):
        return None

    async def fake_upsert_by_pk(*_args, **_kwargs):
        return None

    async def fake_delta(**kwargs):
        emitted.append(kwargs)

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module, "record_field_change_and_publications", fake_delta)
    monkeypatch.setenv("COMMERCE_INDEX_V2_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_INDEX_V2_MERCHANT_ALLOWLIST", "merchant_1")

    await module._upsert_field_fact(
        entity_type="offer",
        entity_id="offer_1",
        field_family="pricing",
        field_key="merchant_effective_price",
        source_system="shopify_products_sync",
        source_ref="sync_1",
        value={"amount": "12.00", "currency": "USD"},
        merchant_id="merchant_1",
        commerce_index_source_id="ci_source_merchant_1_shopify",
        commerce_index_source_kind="merchant_api",
    )

    assert len(emitted) == 1
    assert emitted[0]["merchant_id"] == "merchant_1"
    assert emitted[0]["observation"].source_kind == "merchant_api"
    assert emitted[0]["source_id"] == "ci_source_merchant_1_shopify"


@pytest.mark.asyncio
async def test_upsert_field_fact_withholds_v2_delta_without_active_source_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def should_not_emit(**_kwargs):
        raise AssertionError("v2 publication requires an active source contract")

    async def fake_upsert_by_pk(*_args, **_kwargs):
        return None

    async def fake_execute(*_args, **_kwargs):
        return None

    monkeypatch.setattr(module, "record_field_change_and_publications", should_not_emit)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setenv("COMMERCE_INDEX_V2_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_INDEX_V2_MERCHANT_ALLOWLIST", "merchant_1")

    await module._upsert_field_fact(
        entity_type="offer",
        entity_id="offer_1",
        field_family="pricing",
        field_key="merchant_effective_price",
        source_system="universal_product_sync",
        source_ref="sync_1",
        value={"amount": "12.00", "currency": "USD"},
        merchant_id="merchant_1",
    )


@pytest.mark.asyncio
async def test_allowlisted_v2_ingest_does_not_mutate_catalog_without_active_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_source(**_kwargs):
        return None

    monkeypatch.setattr(module, "resolve_active_catalog_source", no_source)
    monkeypatch.setenv("COMMERCE_INDEX_V2_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_INDEX_V2_MERCHANT_ALLOWLIST", "merchant_1")

    result = await module.ingest_standard_products(
        merchant_id="merchant_1",
        platform="shopify",
        product_payloads=[{"id": "p_1", "title": "Blocked product"}],
        source_system="universal_product_sync",
    )

    assert result["commerce_index_v2_withheld"] is True
    assert result["products_ingested"] == 0


@pytest.mark.asyncio
async def test_append_snapshot_keeps_latest_offer_source_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed_sql: list[str] = []

    async def fake_execute(query, values=None):
        executed_sql.append(str(query))
        return None

    monkeypatch.setattr(module.database, "execute", fake_execute)

    await module._append_snapshot(
        module.catalog_price_snapshots,
        {
            "offer_id": "offer_1",
            "sku_key": "sku_1",
            "merchant_id": "merch_1",
            "source_system": "shopify_products_sync",
            "currency": "USD",
        },
    )

    assert len(executed_sql) == 2
    assert "DELETE FROM catalog_price_snapshots" in executed_sql[0]
    assert "INSERT INTO catalog_price_snapshots" in executed_sql[1]


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
async def test_prune_tombstones_only_dropped_products_for_same_source_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The tombstone writer ships inert — see the ADR comment above
    # prune_missing_catalog_products_for_source. These tests assert what it does
    # once enabled, so they opt in.
    monkeypatch.setenv("CATALOG_SYNC_PRUNE_TOMBSTONE_ENABLED", "true")
    fake_db = _PruneFakeDatabase()
    x1_product, x1_sku, x1_offer = fake_db.add_catalog_tree(
        source_domain="store-a.myshopify.com",
        source_product_id="X1",
    )
    x2_product, x2_sku, x2_offer = fake_db.add_catalog_tree(
        source_domain="store-a.myshopify.com",
        source_product_id="X2",
    )
    y1_product, y1_sku, y1_offer = fake_db.add_catalog_tree(
        source_domain="store-b.myshopify.com",
        source_product_id="Y1",
    )

    monkeypatch.setattr(module, "database", fake_db)

    stats = await module.prune_missing_catalog_products_for_source(
        merchant_id="merch_shared",
        platform="shopify",
        valid_source_product_ids=["X1"],
        source_system="shopify_products_sync",
        source_domain="store-a.myshopify.com",
        sync_run_id="sync_store_a_2",
    )

    assert stats["catalog_products"] == 1
    assert stats["catalog_skus"] == 1
    assert stats["catalog_offers"] == 1
    for key, table in ((x2_product, fake_db.products), (x2_sku, fake_db.skus), (x2_offer, fake_db.offers)):
        assert table[key]["suppression_reason"] == module.STALE_AFTER_SYNC
        assert table[key]["suppressed_at"] == "now"
        assert table[key]["suppression_metadata"]["sync_run_id"] == "sync_store_a_2"

    for key, table in (
        (x1_product, fake_db.products),
        (x1_sku, fake_db.skus),
        (x1_offer, fake_db.offers),
        (y1_product, fake_db.products),
        (y1_sku, fake_db.skus),
        (y1_offer, fake_db.offers),
    ):
        assert table[key]["suppressed_at"] is None


@pytest.mark.asyncio
async def test_prune_source_domain_scope_isolates_other_store_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = _PruneFakeDatabase()
    fake_db.add_catalog_tree(source_domain="store-a.myshopify.com", source_product_id="X1")
    y1_product, y1_sku, y1_offer = fake_db.add_catalog_tree(
        source_domain="store-b.myshopify.com",
        source_product_id="Y1",
    )

    monkeypatch.setattr(module, "database", fake_db)

    await module.prune_missing_catalog_products_for_source(
        merchant_id="merch_shared",
        platform="shopify",
        valid_source_product_ids=["X1"],
        source_system="shopify_products_sync",
        source_domain="store-a.myshopify.com",
        sync_run_id="sync_store_a_1",
    )

    assert fake_db.products[y1_product]["suppressed_at"] is None
    assert fake_db.skus[y1_sku]["suppressed_at"] is None
    assert fake_db.offers[y1_offer]["suppressed_at"] is None


@pytest.mark.asyncio
async def test_prune_writes_writer_audit_row_with_sample_product_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The tombstone writer ships inert — see the ADR comment above
    # prune_missing_catalog_products_for_source. These tests assert what it does
    # once enabled, so they opt in.
    monkeypatch.setenv("CATALOG_SYNC_PRUNE_TOMBSTONE_ENABLED", "true")
    fake_db = _PruneFakeDatabase()
    x2_product, _, _ = fake_db.add_catalog_tree(
        source_domain="store-a.myshopify.com",
        source_product_id="X2",
    )
    x3_product, _, _ = fake_db.add_catalog_tree(
        source_domain="store-a.myshopify.com",
        source_product_id="X3",
    )

    monkeypatch.setattr(module, "database", fake_db)

    await module.prune_missing_catalog_products_for_source(
        merchant_id="merch_shared",
        platform="shopify",
        valid_source_product_ids=[],
        source_system="shopify_products_sync",
        source_domain="store-a.myshopify.com",
        sync_run_id="sync_store_a_full",
    )

    assert len(fake_db.audit_rows) == 1
    audit_row = fake_db.audit_rows[0]
    assert audit_row["writer_name"] == "catalog_sync_service_prune"
    assert audit_row["batch_id"] == "sync_store_a_full"
    assert audit_row["applied_rows"] == 0
    assert audit_row["skipped_rows"] == 2
    reasons = json.loads(audit_row["reasons"])
    assert reasons["stale_after_sync"] == 2
    assert reasons["tombstoned_product_keys_sample"] == sorted([x2_product, x3_product])


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
    claims = []

    async def fake_get_catalog_sync_job(job_id: str):
        assert job_id == "job_123"
        return dict(stored)

    async def fake_claim(job_id: str):
        # The real claim is a conditional UPDATE (pending -> running) that
        # returns the row only to the winner; model exactly that.
        claims.append(job_id)
        if stored["status"] != "pending":
            return None
        stored["status"] = "running"
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
    monkeypatch.setattr(module, "claim_catalog_sync_job", fake_claim)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert)
    monkeypatch.setattr(module, "sync_products_cache_to_catalog", fake_sync_products_cache_to_catalog)

    result = await module.run_catalog_sync_job("job_123")

    assert claims == ["job_123"]
    assert stored["status"] == "running"
    assert result["job_id"] == "job_123"
    assert result["status"] == "running" or result["status"] == "completed"


@pytest.mark.asyncio
async def test_run_catalog_sync_job_logs_the_failure_it_only_records_in_the_row(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing ingest must say so in the LOGS, not only in
    `catalog_sync_jobs.status`.

    On 2026-08-29 a sync that wrote zero rows was invisible except as one
    `status='failed'` row: this job was handed to a FastAPI BackgroundTask
    (routes/merchant_store_connections.py), so it ran after the response was
    sent, and re-raising is not a log line — the exception landed in the ASGI
    server's handler, outside this service's logging, while the endpoint had
    already answered `catalog_ingest_queued: true` with a 200. The runner moved
    out of band since, but a `failed` row still says only THAT it failed.
    """
    stored = {
        "job_id": "job_fail",
        "merchant_id": "merch_1",
        "connector": "shopify",
        "mode": "reconcile",
        "scope_json": {"platform": "shopify"},
        "status": "pending",
        "stats_json": {},
    }
    updates = []

    async def fake_get_catalog_sync_job(job_id: str):
        return dict(stored)

    async def fake_claim(_job_id: str):
        stored["status"] = "running"
        return dict(stored)

    async def fake_upsert(_table, _pk_name, values):
        updates.append(dict(values))
        stored.update(values)

    async def boom(**_kwargs):
        raise RuntimeError(
            'duplicate key value violates unique constraint "beauty_shades_pkey"'
        )

    monkeypatch.setattr(module, "get_catalog_sync_job", fake_get_catalog_sync_job)
    monkeypatch.setattr(module, "claim_catalog_sync_job", fake_claim)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert)
    monkeypatch.setattr(module, "sync_products_cache_to_catalog", boom)

    with caplog.at_level(logging.ERROR, logger=module.logger.name):
        with pytest.raises(RuntimeError):
            await module.run_catalog_sync_job("job_fail")

    assert updates[-1]["status"] == "failed"
    failures = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert failures, "the job failed and logged nothing"
    logged = failures[0].getMessage()
    assert "job_fail" in logged and "merch_1" in logged, logged
    assert "beauty_shades_pkey" in logged, logged
    # logger.exception, not logger.error — without the traceback the log names
    # the job but not the statement that broke it.
    assert failures[0].exc_info is not None


@pytest.mark.asyncio
async def test_run_catalog_sync_job_does_not_re_run_a_job_it_did_not_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the claim must not ingest a second time.

    The drain tick and a request handler can both reach the same row. Without
    the conditional claim, `run_catalog_sync_job` blind-wrote `status='running'`
    and ingested regardless of who was already working on it.
    """
    stored = {
        "job_id": "job_busy",
        "merchant_id": "merch_1",
        "connector": "shopify",
        "mode": "reconcile",
        "scope_json": {"platform": "shopify"},
        "status": "running",
    }
    ingested = []

    async def fake_get_catalog_sync_job(job_id: str):
        return dict(stored)

    async def fake_claim(_job_id: str):
        return None  # somebody else holds it

    async def fake_sync_products_cache_to_catalog(**kwargs):
        ingested.append(kwargs)
        return {}

    monkeypatch.setattr(module, "get_catalog_sync_job", fake_get_catalog_sync_job)
    monkeypatch.setattr(module, "claim_catalog_sync_job", fake_claim)
    monkeypatch.setattr(module, "sync_products_cache_to_catalog", fake_sync_products_cache_to_catalog)

    result = await module.run_catalog_sync_job("job_busy")

    assert ingested == [], "a job we did not claim was ingested anyway"
    assert result["status"] == "running"


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
    # ADR-024: `market` is the column default, and the row now SAYS so. The
    # value is unchanged (this is a provenance change, not a behavior change);
    # what is new is that a reader can tell a defaulted market from a known one.
    # Mutant killed: dropping the provenance key, or writing a market that is
    # not the declared default constant.
    assert offer_writes[0]["market"] == module.MARKET_UNKNOWN_DEFAULT == "US"
    assert (
        offer_writes[0]["offer_payload"]["market_provenance"]
        == module.MARKET_PROVENANCE_PLATFORM_DEFAULT
        == "platform_default_unknown"
    )
    # The CURRENCY is a real observation from the platform payload and must not
    # be tarred with the same brush -- it carries no provenance marker, because
    # it needs none. (The 2026-07-29 Wix pilot's 433 rows were honest EUR under
    # a fabricated US market; only the market half was ever the defect.)
    assert "currency_provenance" not in offer_writes[0]["offer_payload"]
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
    # ADR-024: `market` is the column default, and the row now SAYS so. The
    # value is unchanged (this is a provenance change, not a behavior change);
    # what is new is that a reader can tell a defaulted market from a known one.
    # Mutant killed: dropping the provenance key, or writing a market that is
    # not the declared default constant.
    assert offer_writes[0]["market"] == module.MARKET_UNKNOWN_DEFAULT == "US"
    assert (
        offer_writes[0]["offer_payload"]["market_provenance"]
        == module.MARKET_PROVENANCE_PLATFORM_DEFAULT
        == "platform_default_unknown"
    )
    # The CURRENCY is a real observation from the platform payload and must not
    # be tarred with the same brush -- it carries no provenance marker, because
    # it needs none. (The 2026-07-29 Wix pilot's 433 rows were honest EUR under
    # a fabricated US market; only the market half was ever the defect.)
    assert "currency_provenance" not in offer_writes[0]["offer_payload"]
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
async def test_ingest_standard_products_recovers_stale_after_sync_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product_key = module.make_catalog_product_key("merch_recover", "shopify", "X2")
    sku_key = module.make_catalog_sku_key(product_key, "X2_variant")
    offer_id = module.make_catalog_offer_id(sku_key, "default", "internal_merchant")
    tombstone = {
        "suppression_reason": module.STALE_AFTER_SYNC,
        "suppressed_at": datetime(2026, 5, 26, 1, 0, 0),
        "suppression_metadata": {"sync_run_id": "sync_old"},
    }
    records = {
        "catalog_products": {
            product_key: {
                "product_key": product_key,
                **tombstone,
            }
        },
        "catalog_skus": {
            sku_key: {
                "sku_key": sku_key,
                **tombstone,
            }
        },
        "catalog_offers": {
            offer_id: {
                "offer_id": offer_id,
                **tombstone,
            }
        },
    }
    audit_rows = []

    async def fake_upsert_catalog_merchant(**_kwargs):
        return None

    async def fake_upsert_by_pk(table, pk_name, values):
        table_name = getattr(table, "name", None)
        key = values[pk_name]
        existing = records.setdefault(table_name, {}).get(key)
        records[table_name][key] = {**(existing or {}), **dict(values)}
        return dict(existing) if existing else None

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

    async def fake_resolve_catalog_sku_key(**_kwargs):
        return sku_key

    async def fake_fold_category_with_llm_fallback(**_kwargs):
        return None

    monkeypatch.setattr(module.database, "transaction", lambda: _DummyTransaction())
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module, "upsert_catalog_merchant", fake_upsert_catalog_merchant)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module, "_upsert_field_fact", fake_upsert_field_fact)
    monkeypatch.setattr(module, "_append_snapshot", fake_append_snapshot)
    monkeypatch.setattr(module, "_replace_child_rows_multi", fake_replace_child_rows_multi)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", fake_resolve_catalog_sku_key)
    monkeypatch.setattr(module, "fold_category_with_llm_fallback", fake_fold_category_with_llm_fallback)
    monkeypatch.setattr(module, "_schedule_fashion_enrichment", lambda **_kwargs: None)

    stats = await module.ingest_standard_products(
        merchant_id="merch_recover",
        platform="shopify",
        product_payloads=[
            {
                "id": "X2",
                "product_id": "X2",
                "merchant_id": "merch_recover",
                "platform": "shopify",
                "title": "Recovered Serum",
                "price": 29.0,
                "currency": "USD",
                "variants": [
                    {
                        "id": "X2_variant",
                        "title": "Default",
                        "price": 29.0,
                        "inventory_quantity": 3,
                    },
                ],
            }
        ],
        source_system="shopify_products_sync",
        source_ref="sync_recovery",
        source_domain="store-a.myshopify.com",
    )

    assert stats["products_recovered_after_stale"] == 1
    assert records["catalog_products"][product_key]["suppressed_at"] is None
    assert records["catalog_skus"][sku_key]["suppressed_at"] is None
    assert records["catalog_offers"][offer_id]["suppressed_at"] is None
    assert records["catalog_products"][product_key]["suppression_reason"] is None
    assert records["catalog_skus"][sku_key]["suppression_reason"] is None
    assert records["catalog_offers"][offer_id]["suppression_reason"] is None
    assert len(audit_rows) == 1
    reasons = json.loads(audit_rows[0]["reasons"])
    assert reasons["recovered_after_stale"] == 1


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

    class _DummyTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(module, "_upsert_by_pk", _capture_upsert)
    monkeypatch.setattr(module, "upsert_catalog_merchant", _noop_upsert_merchant)
    monkeypatch.setattr(module, "_upsert_field_fact", _noop_upsert_field_fact)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)
    monkeypatch.setattr(module.database, "execute", _noop_execute)
    # Order-robustness: without a transaction stub this test only passed by
    # inheriting connection state from earlier tests in the same session
    # (the sibling tests at DummyTransaction already stub it).
    monkeypatch.setattr(module.database, "transaction", lambda: _DummyTx())

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

    class _DummyTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(module, "_upsert_by_pk", _capture_upsert)
    monkeypatch.setattr(module, "upsert_catalog_merchant", _noop)
    monkeypatch.setattr(module, "_upsert_field_fact", _noop)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)
    monkeypatch.setattr(module.database, "execute", _noop)
    # Order-robustness: see the transaction-stub note in the metafields test.
    monkeypatch.setattr(module.database, "transaction", lambda: _DummyTx())

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


# ---------------------------------------------------------------------------
# The kill-switch. These statements never executed in production (they failed to
# PREPARE from #666 until the fix in this branch), so enabling them is a
# behaviour change with a ~2.5-month backlog behind it, not a repair. A switch
# that is silently always-on reads exactly like no switch at all, so both
# directions are asserted.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prune_tombstone_is_suppressed_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = _PruneFakeDatabase()
    fake_db.add_catalog_tree(source_domain="store-a.myshopify.com", source_product_id="X1")
    x2_product, _, _ = fake_db.add_catalog_tree(
        source_domain="store-a.myshopify.com", source_product_id="X2")
    monkeypatch.delenv("CATALOG_SYNC_PRUNE_TOMBSTONE_ENABLED", raising=False)
    monkeypatch.setattr(module, "database", fake_db)

    stats = await module.prune_missing_catalog_products_for_source(
        merchant_id="merch_shared", platform="shopify", valid_source_product_ids=["X1"],
        source_system="shopify_products_sync", source_domain="store-a.myshopify.com",
        sync_run_id="sync_default_off",
    )

    assert stats["catalog_products"] == 0, "default run must not tombstone anything"
    assert stats.get("tombstone_suppressed") == 1
    assert stats.get("stale_detected") == 1, (
        "the stale set must still be COMPUTED and reported — an operator needs "
        "the count to decide whether to enable the writer"
    )
    assert fake_db.products[x2_product]["suppressed_at"] is None, (
        "the row the enabled path would tombstone must be untouched"
    )


@pytest.mark.asyncio
async def test_prune_tombstone_respects_the_row_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = _PruneFakeDatabase()
    for pid in ("X1", "X2", "X3"):
        fake_db.add_catalog_tree(source_domain="store-a.myshopify.com", source_product_id=pid)
    monkeypatch.setenv("CATALOG_SYNC_PRUNE_TOMBSTONE_ENABLED", "true")
    monkeypatch.setenv("CATALOG_SYNC_PRUNE_MAX_ROWS", "1")
    monkeypatch.setattr(module, "database", fake_db)

    stats = await module.prune_missing_catalog_products_for_source(
        merchant_id="merch_shared", platform="shopify", valid_source_product_ids=["X1"],
        source_system="shopify_products_sync", source_domain="store-a.myshopify.com",
        sync_run_id="sync_over_cap",
    )

    assert stats["catalog_products"] == 0, "2 stale rows must not pass a cap of 1"
    assert stats.get("tombstone_suppressed") == 1
