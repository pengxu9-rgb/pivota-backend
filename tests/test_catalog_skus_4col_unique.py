from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.catalog import catalog_skus
from scripts import backfill_catalog_skus_default_variant_id as backfill
from services import catalog_sync_service as catalog_sync


def _sqlite_catalog_skus() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE catalog_skus (
          sku_key TEXT PRIMARY KEY,
          product_key TEXT NOT NULL,
          merchant_id TEXT NOT NULL,
          platform TEXT NOT NULL,
          source_product_id TEXT NOT NULL,
          source_variant_id TEXT NOT NULL,
          title TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_catalog_skus_source_identity_v2
        ON catalog_skus (merchant_id, platform, product_key, source_variant_id)
        """
    )
    return conn


def _insert_sku(
    conn: sqlite3.Connection,
    *,
    sku_key: str,
    product_key: str,
    merchant_id: str = "merch_1",
    platform: str = "shopify",
    source_product_id: str | None = None,
    source_variant_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO catalog_skus (
          sku_key,
          product_key,
          merchant_id,
          platform,
          source_product_id,
          source_variant_id,
          title
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sku_key,
            product_key,
            merchant_id,
            platform,
            source_product_id or product_key,
            source_variant_id,
            "Catalog SKU",
        ),
    )


def test_catalog_skus_model_uses_4col_unique_identity_v2() -> None:
    index = next(
        idx for idx in catalog_skus.indexes if idx.name == "idx_catalog_skus_source_identity_v2"
    )

    assert index.unique is True
    assert [col.name for col in index.columns] == [
        "merchant_id",
        "platform",
        "product_key",
        "source_variant_id",
    ]
    assert "idx_catalog_skus_source_identity" not in {
        idx.name for idx in catalog_skus.indexes
    }


def test_two_unvarianted_products_insert_with_product_key_variant_identity() -> None:
    conn = _sqlite_catalog_skus()
    try:
        product_a = "prod::merch_1::shopify::prod_a"
        product_b = "prod::merch_1::shopify::prod_b"

        _insert_sku(conn, sku_key="sku_a", product_key=product_a, source_variant_id=product_a)
        _insert_sku(conn, sku_key="sku_b", product_key=product_b, source_variant_id=product_b)

        count = conn.execute("SELECT COUNT(*) FROM catalog_skus").fetchone()[0]
    finally:
        conn.close()

    assert count == 2


def test_two_variants_of_same_product_insert_with_distinct_variant_ids() -> None:
    conn = _sqlite_catalog_skus()
    try:
        product_key = "prod::merch_1::shopify::prod_a"

        _insert_sku(conn, sku_key="sku_a_red", product_key=product_key, source_variant_id="red")
        _insert_sku(conn, sku_key="sku_a_blue", product_key=product_key, source_variant_id="blue")

        count = conn.execute("SELECT COUNT(*) FROM catalog_skus").fetchone()[0]
    finally:
        conn.close()

    assert count == 2


def test_duplicate_4col_sku_identity_fails() -> None:
    conn = _sqlite_catalog_skus()
    try:
        product_key = "prod::merch_1::shopify::prod_a"
        _insert_sku(
            conn,
            sku_key="sku_a_1",
            product_key=product_key,
            source_product_id="source_prod_a",
            source_variant_id="variant_1",
        )

        with pytest.raises(sqlite3.IntegrityError):
            _insert_sku(
                conn,
                sku_key="sku_a_2",
                product_key=product_key,
                source_product_id="source_prod_b",
                source_variant_id="variant_1",
            )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_resolve_catalog_sku_key_uses_product_key_in_identity_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}

    async def fake_fetch_one(query):
        captured["query"] = query
        return None

    monkeypatch.setattr(catalog_sync.database, "fetch_one", fake_fetch_one)

    sku_key = await catalog_sync._resolve_catalog_sku_key(
        merchant_id="merch_1",
        platform="shopify",
        product_key="prod::merch_1::shopify::prod_a",
        source_variant_id="variant_1",
    )

    compiled = str(captured["query"].compile(compile_kwargs={"literal_binds": True}))
    assert "catalog_skus.product_key" in compiled
    assert sku_key == "sku::prod::merch_1::shopify::prod_a::variant_1"


@pytest.mark.asyncio
async def test_ingest_uses_product_key_when_source_variant_id_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sku_writes: List[Dict[str, Any]] = []

    class DummyTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_upsert_by_pk(table, _pk_name, values):
        if getattr(table, "name", None) == "catalog_skus":
            sku_writes.append(dict(values))

    async def fake_resolve_catalog_sku_key(**kwargs):
        return catalog_sync.make_catalog_sku_key(kwargs["product_key"], kwargs["source_variant_id"])

    async def noop_async(*_args, **_kwargs):
        return None

    async def no_category_fold(*_args, **_kwargs):
        return None

    monkeypatch.setattr(catalog_sync.database, "transaction", lambda: DummyTransaction())
    monkeypatch.setattr(catalog_sync.database, "execute", noop_async)
    monkeypatch.setattr(catalog_sync, "upsert_catalog_merchant", noop_async)
    monkeypatch.setattr(catalog_sync, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(catalog_sync, "_upsert_field_fact", noop_async)
    monkeypatch.setattr(catalog_sync, "_append_snapshot", noop_async)
    monkeypatch.setattr(catalog_sync, "_resolve_catalog_sku_key", fake_resolve_catalog_sku_key)
    monkeypatch.setattr(catalog_sync, "fold_category_with_llm_fallback", no_category_fold)
    monkeypatch.setattr(catalog_sync, "_schedule_fashion_enrichment", lambda **_kwargs: None)

    stats = await catalog_sync.ingest_standard_products(
        merchant_id="merch_1",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_a",
                "product_id": "prod_a",
                "merchant_id": "merch_1",
                "platform": "shopify",
                "title": "Plain Item A",
                "price": 10.0,
                "currency": "USD",
                "variants": [{"id": "", "title": "Default", "price": 10.0}],
            },
            {
                "id": "prod_b",
                "product_id": "prod_b",
                "merchant_id": "merch_1",
                "platform": "shopify",
                "title": "Plain Item B",
                "price": 12.0,
                "currency": "USD",
                "variants": [{"id": "", "title": "Default", "price": 12.0}],
            },
        ],
        source_system="test",
        source_ref="test_ref",
    )

    assert stats["skus_ingested"] == 2
    assert [row["source_variant_id"] for row in sku_writes] == [
        "prod::merch_1::shopify::prod_a",
        "prod::merch_1::shopify::prod_b",
    ]


@pytest.mark.asyncio
async def test_backfill_dry_run_reports_default_count(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_one(_sql):
        return {"count": 3}

    async def fail_execute(*_args, **_kwargs):
        raise AssertionError("dry-run must not UPDATE")

    monkeypatch.setattr(backfill.database, "is_connected", True)
    monkeypatch.setattr(backfill.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(backfill.database, "execute", fail_execute)

    report = await backfill._drive(SimpleNamespace(apply=False))

    assert report["mode"] == "dry_run"
    assert report["pre_default_count"] == 3
    assert report["post_default_count"] == 3
    assert report["updated_count"] == 0


@pytest.mark.asyncio
async def test_backfill_apply_updates_only_default_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"default_count": 2}
    executed: List[str] = []

    class DummyTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_fetch_one(_sql):
        return {"count": state["default_count"]}

    async def fake_execute(sql):
        executed.append(str(sql))
        state["default_count"] = 0

    monkeypatch.setattr(backfill.database, "is_connected", True)
    monkeypatch.setattr(backfill.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(backfill.database, "execute", fake_execute)
    monkeypatch.setattr(backfill.database, "transaction", lambda: DummyTransaction())
    monkeypatch.setattr(backfill, "_prompt_for_confirmation", lambda count: count == 2)

    report = await backfill._drive(SimpleNamespace(apply=True))

    assert report["mode"] == "apply"
    assert report["pre_default_count"] == 2
    assert report["pre_default_count_in_transaction"] == 2
    assert report["post_default_count"] == 0
    assert report["updated_count"] == 2
    assert len(executed) == 1
    assert "SET source_variant_id = product_key" in executed[0]
    assert "WHERE source_variant_id = 'default'" in executed[0]
