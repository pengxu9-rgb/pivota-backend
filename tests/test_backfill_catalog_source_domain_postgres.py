"""Production-dialect gate for the mirror rule of scripts/backfill_catalog_source_domain.py.

The script's unit tests (tests/test_backfill_catalog_source_domain.py) answer its SQL from a fake
that re-implements the resolution in Python, so they cannot see the statement itself. This module
EXECUTES the real mirror resolver against a real Postgres.

It touches no shared table: one transaction pins one connection, and TEMP tables created in it
(`ON COMMIT DROP`) shadow the real ones for that connection only (pg_temp is searched first), so the
full gate's shared database is left exactly as it was found.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_backfill_catalog_source_domain_postgres.py
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — this is the production-dialect gate",
)

TEMP_DDL = (
    "CREATE TEMP TABLE catalog_products (product_key TEXT PRIMARY KEY, merchant_id TEXT, platform TEXT,"
    " source_system TEXT, source_ref TEXT, source_domain TEXT, updated_at TIMESTAMPTZ) ON COMMIT DROP",
    "CREATE TEMP TABLE catalog_skus (sku_key TEXT PRIMARY KEY, product_key TEXT, source_domain TEXT,"
    " updated_at TIMESTAMPTZ) ON COMMIT DROP",
    "CREATE TEMP TABLE catalog_offers (offer_id TEXT PRIMARY KEY, product_key TEXT, source_domain TEXT,"
    " updated_at TIMESTAMPTZ) ON COMMIT DROP",
    "CREATE TEMP TABLE external_product_seeds (id TEXT PRIMARY KEY, domain TEXT, destination_url TEXT,"
    " canonical_url TEXT) ON COMMIT DROP",
    "CREATE TEMP TABLE writer_audit_log (writer_name TEXT, batch_id TEXT, dry_run_report_hash TEXT,"
    " applied_rows INT, skipped_rows INT, reasons JSONB, actor TEXT) ON COMMIT DROP",
)


class _Db:
    """The script's db interface (`fetch_all(query, values)` -> dicts) over the app's `databases`."""

    def __init__(self, database):
        self._database = database

    async def fetch_all(self, query, values=None):
        return [dict(row) for row in await self._database.fetch_all(query, values or {})]

    async def fetch_one(self, query, values=None):
        row = await self._database.fetch_one(query, values or {})
        return dict(row) if row is not None else None

    async def execute(self, query, values=None):
        return await self._database.execute(query, values or {})


@pytest.fixture
async def database():
    from db.database import database as db

    was_connected = db.is_connected
    if not was_connected:
        await db.connect()
    try:
        yield db
    finally:
        if not was_connected:
            await db.disconnect()


async def test_mirror_rule_falls_back_to_the_seed_page_host(database):
    import scripts.backfill_catalog_source_domain as backfill

    transaction = await database.transaction()
    try:
        for statement in TEMP_DDL:
            await database.execute(statement)
        mirror = backfill.SOURCE_EXTERNAL_SEEDS
        for key, ref, domain in (("p_domain", "s_domain", None), ("p_dest", "s_dest", None),
                                 ("p_canon", "s_canon", None), ("p_none", "s_none", None),
                                 ("p_junk", "s_junk", None), ("p_space", "s_space", None),
                                 ("p_kept", "s_domain", "kept.example")):
            await database.execute(
                "INSERT INTO catalog_products (product_key, merchant_id, platform, source_system, source_ref,"
                " source_domain) VALUES (:k, 'm', 'external_seed', :s, :r, :d)",
                {"k": key, "s": mirror, "r": ref, "d": domain})
        await database.execute(  # a set source_domain with a NULL child: the product's own value drives it
            "INSERT INTO catalog_skus (sku_key, product_key, source_domain) VALUES ('k_kept', 'p_kept', NULL)")
        for seed_id, domain, destination, canonical in (
            ("s_domain", " www.Example.com ", "https://other.example/products/a", None),  # domain wins
            ("s_dest", None, "https://www.Brand-Two.com/products/x", None),  # no domain, no canonical
            ("s_canon", "  ", "https://dest.example/products/y", "https://three.example/products/y"),  # canonical first
            ("s_junk", "N/A", "https://www.junk-ok.example/products/z", None),  # junk never shadows a page
            ("s_space", "a b.com", None, "https://space-ok.example/products/z"),
            ("s_none", None, None, None),
        ):
            await database.execute(
                "INSERT INTO external_product_seeds (id, domain, destination_url, canonical_url)"
                " VALUES (:i, :d, :u, :c)", {"i": seed_id, "d": domain, "u": destination, "c": canonical})

        rows = {row.product_key: row for row in
                await backfill.CatalogSourceDomainBackfill(_Db(database)).resolve_external_product_seeds_mirror()}
    finally:
        await transaction.rollback()

    got = {key: (row.resolved_source_domain, row.resolution_rule, row.unresolved_reason) for key, row in rows.items()}
    assert got == {
        "p_domain": ("www.example.com", "external_product_seeds.id", None),
        "p_dest": ("www.brand-two.com", "external_product_seeds.url_host", None),
        "p_canon": ("three.example", "external_product_seeds.url_host", None),
        "p_junk": ("www.junk-ok.example", "external_product_seeds.url_host", None),
        "p_space": ("space-ok.example", "external_product_seeds.url_host", None),
        "p_none": (None, "external_product_seeds.id", "seed_missing_or_empty_domain"),
        "p_kept": ("kept.example", "existing_product_source_domain", None),
    }


async def test_apply_fills_only_null_source_domains(database):
    """The REAL apply on TEMP tables: a NULL product, sku and offer get the seed's host; a value
    already set is never touched, the product's own mixed-case one included (the NULL-only guard)."""
    import scripts.backfill_catalog_source_domain as backfill

    transaction = await database.transaction()
    try:
        for statement in TEMP_DDL:
            await database.execute(statement)
        mirror = backfill.SOURCE_EXTERNAL_SEEDS
        await database.execute(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_system, source_ref, source_domain)"
            " VALUES ('p_fill', 'm', 'external_seed', :s, 's_fill', NULL),"
            "        ('p_set', 'm', 'external_seed', :s, 's_other', 'Mixed.Case.Example')", {"s": mirror})
        await database.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, source_domain) VALUES"
            " ('k_fill', 'p_fill', NULL), ('k_set_null', 'p_set', NULL), ('k_set_own', 'p_set', 'sku-own.example')")
        await database.execute(
            "INSERT INTO catalog_offers (offer_id, product_key, source_domain) VALUES"
            " ('o_fill', 'p_fill', NULL), ('o_set_own', 'p_set', 'offer-own.example')")
        await database.execute(
            "INSERT INTO external_product_seeds (id, domain, destination_url, canonical_url) VALUES"
            " ('s_fill', NULL, NULL, 'https://www.Filled.example/products/a'),"
            " ('s_other', 'seed-other.example', NULL, NULL)")

        report = await backfill.CatalogSourceDomainBackfill(_Db(database)).apply(
            source_system=mirror, target="staging", actor="test")
        products = {r["product_key"]: r["source_domain"] for r in await database.fetch_all(
            "SELECT product_key, source_domain FROM catalog_products")}
        skus = {r["sku_key"]: r["source_domain"] for r in await database.fetch_all(
            "SELECT sku_key, source_domain FROM catalog_skus")}
        offers = {r["offer_id"]: r["source_domain"] for r in await database.fetch_all(
            "SELECT offer_id, source_domain FROM catalog_offers")}
        audit_rows = await database.fetch_val("SELECT COUNT(*) FROM writer_audit_log")
    finally:
        await transaction.rollback()

    assert products == {"p_fill": "www.filled.example", "p_set": "Mixed.Case.Example"}
    assert skus == {"k_fill": "www.filled.example", "k_set_null": "mixed.case.example",
                    "k_set_own": "sku-own.example"}
    assert offers == {"o_fill": "www.filled.example", "o_set_own": "offer-own.example"}
    assert report["rows_updated"] == {"catalog_products": 1, "catalog_skus": 2, "catalog_offers": 1}
    assert audit_rows == 1
