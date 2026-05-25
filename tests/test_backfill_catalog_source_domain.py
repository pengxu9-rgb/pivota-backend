from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import backfill_catalog_source_domain as backfill


class FakeDb:
    def __init__(
        self,
        *,
        products=None,
        seeds=None,
        stores=None,
        count_rows=None,
    ):
        self.products = products or []
        self.seeds = seeds or []
        self.stores = stores or []
        self.count_rows = count_rows or {}
        self.executed = []

    async def fetch_all(self, query, values=None):
        values = values or {}
        if "FROM merchant_stores" in query:
            return list(self.stores)
        if "FROM writer_audit_log" in query:
            return []
        if "GROUP BY COALESCE(source_system" in query:
            selected = {}
            for row in self.products:
                if row.get("current_source_domain") is None:
                    source = row.get("source_system") or "<NULL>"
                    selected[source] = selected.get(source, 0) + 1
            return [
                {"source_system": source, "null_products": count}
                for source, count in sorted(selected.items())
            ]
        if "WHERE merchant_id = :merchant_id" in query and "source_system = :source_system" in query:
            return [
                self._db_product(row)
                for row in self.products
                if row.get("merchant_id") == values["merchant_id"]
                and row.get("source_system") == values["source_system"]
            ]
        if "LEFT JOIN external_product_seeds eps_ref" in query:
            return [self._legacy_external_seed_row(row) for row in self._null_products(values["source_system"])]
        if "LEFT JOIN external_product_seeds eps" in query:
            return [self._external_seed_row(row) for row in self._null_products(values["source_system"])]
        if "FROM catalog_products" in query and "product_payload" in query:
            return [self._db_product(row) for row in self._null_products(values["source_system"])]
        return []

    async def fetch_one(self, query, values=None):
        values = values or {}
        if "SELECT COUNT(*)::int AS count FROM updated" in query:
            table = "catalog_products"
            if "UPDATE catalog_skus" in query:
                table = "catalog_skus"
            if "UPDATE catalog_offers" in query:
                table = "catalog_offers"
            return {"count": self.count_rows.get(("updated", table), 0)}
        if "FROM jsonb_to_recordset" in query and "SELECT COUNT(*)::int AS count" in query:
            table = "catalog_products"
            if "FROM catalog_skus" in query:
                table = "catalog_skus"
            if "FROM catalog_offers" in query:
                table = "catalog_offers"
            return {"count": self.count_rows.get(("recoverable", table), 0)}
        if "FROM catalog_products cp" in query and "cp.source_domain IS NULL" in query:
            source = values.get("source_system")
            return {"count": sum(1 for row in self.products if row.get("source_system") == source and row.get("current_source_domain") is None)}
        if "FROM catalog_skus s" in query:
            return {"count": self.count_rows.get(("null", "catalog_skus"), 0)}
        if "FROM catalog_offers o" in query:
            return {"count": self.count_rows.get(("null", "catalog_offers"), 0)}
        return {"count": 0}

    async def execute(self, query, values=None):
        self.executed.append((query, values or {}))
        return 1

    def _null_products(self, source_system):
        return [
            row
            for row in self.products
            if row.get("source_system") == source_system
            and (row.get("current_source_domain") is None or row.get("child_null"))
        ]

    def _db_product(self, row):
        return {
            "product_key": row["product_key"],
            "merchant_id": row.get("merchant_id", "merch_1"),
            "platform": row.get("platform", "shopify"),
            "source_system": row["source_system"],
            "source_ref": row.get("source_ref"),
            "current_source_domain": row.get("current_source_domain"),
            "product_payload": row.get("product_payload"),
        }

    def _seed_by_id(self, seed_id):
        for seed in self.seeds:
            if seed.get("id") == seed_id:
                return seed
        return None

    def _seed_by_attached_product_key(self, product_key):
        for seed in self.seeds:
            if seed.get("attached_product_key") == product_key:
                return seed
        return None

    def _external_seed_row(self, row):
        seed = self._seed_by_id(row.get("source_ref"))
        db_row = self._db_product(row)
        db_row["resolved_source_domain"] = seed.get("domain") if seed else None
        return db_row

    def _legacy_external_seed_row(self, row):
        seed = self._seed_by_id(row.get("source_ref"))
        rule = "external_product_seeds.id" if seed else "legacy_external_seed_unresolved"
        if seed is None:
            seed = self._seed_by_attached_product_key(row["product_key"])
            if seed:
                rule = "external_product_seeds.attached_product_key"
        db_row = self._db_product(row)
        db_row["resolved_source_domain"] = seed.get("domain") if seed else None
        db_row["resolution_rule"] = rule
        return db_row


def _product(source_system, product_key="p1", **overrides):
    row = {
        "product_key": product_key,
        "merchant_id": "merch_1",
        "platform": "shopify",
        "source_system": source_system,
        "source_ref": None,
        "current_source_domain": None,
        "product_payload": {},
    }
    row.update(overrides)
    return row


def _store(domain, *, merchant_id="merch_1", platform="shopify", status="active", store_id="store_1", api_key=None):
    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "domain": domain,
        "status": status,
        "store_id": store_id,
        "api_key": api_key,
    }


@pytest.mark.asyncio
async def test_external_seed_mirror_resolves_from_seed_id():
    db = FakeDb(
        products=[
            _product(
                backfill.SOURCE_EXTERNAL_SEEDS,
                source_ref="seed_1",
                merchant_id="external_seed",
                platform="external_seed",
            )
        ],
        seeds=[{"id": "seed_1", "domain": "Example.com"}],
    )

    rows = await backfill.CatalogSourceDomainBackfill(db).resolve_external_product_seeds_mirror()

    assert rows[0].resolved_source_domain == "example.com"
    assert rows[0].resolution_rule == "external_product_seeds.id"


@pytest.mark.asyncio
async def test_shopify_resolves_payload_domain_before_single_store_fallback():
    db = FakeDb(
        products=[
            _product(
                backfill.SOURCE_SHOPIFY,
                product_payload={"source": {"domain": "first.myshopify.com"}},
            )
        ],
        stores=[
            _store("first.myshopify.com"),
            _store("second.myshopify.com", store_id="store_2", status="inactive"),
        ],
    )

    rows = await backfill.CatalogSourceDomainBackfill(db).resolve_shopify_products_sync()

    assert rows[0].resolved_source_domain == "first.myshopify.com"
    assert rows[0].resolution_rule == "payload_domain"


@pytest.mark.asyncio
async def test_shopify_resolves_source_ref_domain_for_multi_store_merchant():
    db = FakeDb(
        products=[
            _product(
                backfill.SOURCE_SHOPIFY,
                source_ref="sync:second.myshopify.com:batch",
            )
        ],
        stores=[
            _store("first.myshopify.com"),
            _store("second.myshopify.com", store_id="store_2", status="inactive"),
        ],
    )

    rows = await backfill.CatalogSourceDomainBackfill(db).resolve_shopify_products_sync()

    assert rows[0].resolved_source_domain == "second.myshopify.com"
    assert rows[0].resolution_rule == "source_ref_domain"


@pytest.mark.asyncio
async def test_shopify_single_historical_store_fallback():
    db = FakeDb(
        products=[_product(backfill.SOURCE_SHOPIFY)],
        stores=[_store("solo.myshopify.com")],
    )

    rows = await backfill.CatalogSourceDomainBackfill(db).resolve_shopify_products_sync()

    assert rows[0].resolved_source_domain == "solo.myshopify.com"
    assert rows[0].resolution_rule == "single_historical_store"


@pytest.mark.asyncio
async def test_shopify_multi_store_without_row_clue_stays_unresolved():
    db = FakeDb(
        products=[_product(backfill.SOURCE_SHOPIFY)],
        stores=[
            _store("first.myshopify.com"),
            _store("second.myshopify.com", store_id="store_2", status="inactive"),
        ],
    )

    rows = await backfill.CatalogSourceDomainBackfill(db).resolve_shopify_products_sync()

    assert rows[0].resolved_source_domain is None
    assert rows[0].unresolved_reason == "multi_store_ambiguous"


@pytest.mark.asyncio
async def test_shopify_existing_product_domain_can_drive_child_backfill():
    db = FakeDb(
        products=[
            _product(
                backfill.SOURCE_SHOPIFY,
                current_source_domain="https://first.myshopify.com/",
                child_null=True,
            )
        ],
        stores=[
            _store("first.myshopify.com"),
            _store("second.myshopify.com", store_id="store_2", status="inactive"),
        ],
    )

    rows = await backfill.CatalogSourceDomainBackfill(db).resolve_shopify_products_sync()

    assert rows[0].resolved_source_domain == "first.myshopify.com"
    assert rows[0].resolution_rule == "existing_product_source_domain"


@pytest.mark.asyncio
async def test_universal_wix_resolves_site_id_to_store_domain():
    db = FakeDb(
        products=[
            _product(
                backfill.SOURCE_UNIVERSAL,
                platform="wix",
                product_payload={"platform_metadata": {"site_id": "site_abc123"}},
            )
        ],
        stores=[
            _store(
                "wix-domain.example",
                platform="wix",
                api_key={"site_id": "site_abc123", "api_key": "redacted"},
            )
        ],
    )

    rows = await backfill.CatalogSourceDomainBackfill(db).resolve_universal_product_sync()

    assert rows[0].resolved_source_domain == "wix-domain.example"
    assert rows[0].resolution_rule == "payload_site_id"


@pytest.mark.asyncio
async def test_enrichment_rows_are_reported_unrecoverable():
    db = FakeDb(products=[_product(backfill.SOURCE_ENRICHMENT)])

    rows = await backfill.CatalogSourceDomainBackfill(db).resolve_catalog_enrichment_agent()

    assert rows[0].resolved_source_domain is None
    assert rows[0].unresolved_reason == "enrichment_not_store_scoped"


@pytest.mark.asyncio
async def test_legacy_external_seed_resolves_from_attached_product_key():
    db = FakeDb(
        products=[
            _product(
                backfill.SOURCE_LEGACY_EXTERNAL_SEED,
                product_key="legacy_product",
                source_ref="missing_seed",
            )
        ],
        seeds=[
            {
                "id": "seed_2",
                "attached_product_key": "legacy_product",
                "domain": "legacy.example",
            }
        ],
    )

    rows = await backfill.CatalogSourceDomainBackfill(db).resolve_external_seed_catalog_mirror()

    assert rows[0].resolved_source_domain == "legacy.example"
    assert rows[0].resolution_rule == "external_product_seeds.attached_product_key"


def test_apply_production_requires_second_confirm():
    parser = backfill.build_parser()
    args = parser.parse_args(
        [
            "--database-url",
            "postgresql://example/test",
            "--source",
            backfill.SOURCE_EXTERNAL_SEEDS,
            "apply",
            "--target",
            "production",
            "--confirm",
            backfill.CONFIRM_TOKEN,
        ]
    )

    with pytest.raises(SystemExit, match=backfill.PROD_CONFIRM_TOKEN):
        backfill._require_apply_confirm(args)


def test_source_can_be_passed_after_subcommand():
    parser = backfill.build_parser()
    args = parser.parse_args(
        [
            "--database-url",
            "postgresql://example/test",
            "apply",
            "--target",
            "staging",
            "--source",
            backfill.SOURCE_EXTERNAL_SEEDS,
            "--confirm",
            backfill.CONFIRM_TOKEN,
        ]
    )

    assert backfill._selected_source_arg(args) == backfill.SOURCE_EXTERNAL_SEEDS


def test_low_recovery_stop_exempts_enrichment_only():
    with pytest.raises(SystemExit, match="recovery rate below 50%"):
        backfill._raise_if_low_recovery(
            backfill.SOURCE_SHOPIFY,
            {"total_null_rows": 10, "recoverable_rows": 4},
        )

    backfill._raise_if_low_recovery(
        backfill.SOURCE_ENRICHMENT,
        {"total_null_rows": 10, "recoverable_rows": 0},
    )
