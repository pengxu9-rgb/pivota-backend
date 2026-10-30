"""F2 (2026-10-10) on the production dialect: the widened Tier-0a lookup, migration 262's index,
the review dedupe, and the backfill proposer's SQL.

The unit matrix (tests/services/test_intake_identity_variant_barcode.py) answers the lookups from a
fake that mimics barcode_lookup_sql. This file EXECUTES the real SQL: the UNION ALL of two IN-lists
(a typed NULL arm, a repeated named bind list), the `checklist->>'matcher'` dedupe, the backfill's
regexp/lpad normalization, and the planner's use of idx_catalog_products_gtin and
idx_catalog_skus_barcode for both arms.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_intake_identity_barcode_lookup_postgres.py

ISOLATION as in test_catalog_products_category_path_index_postgres.py: a per-process scratch schema,
reached through connections whose search_path is that schema ALONE, dropped at teardown. The tables
are the model's own DDL (catalog_products, catalog_skus) plus the migrations that own the rest
(178's gtin index, 262, 045 product_group_members, 179 proposals + pdp_review_tasks).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"
_UP = _MIGRATIONS / "262_catalog_skus_barcode_index.sql"
_DOWN = _MIGRATIONS / "down/262_catalog_skus_barcode_index_down.sql"
_MORE = ("178_catalog_products_gtin.sql", "045_product_groups.sql", "179_identity_resolution_d2.sql")
_INDEX = "idx_catalog_skus_barcode"
_SCHEMA = f"intake_barcode_lookup_test_{os.getpid()}"

SHALIMAR = "3346470113541"   # stored raw (13 digits) on a SKU; canonical 03346470113541
OPI = "619828139641"         # stored raw (12 digits); canonical 00619828139641


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r}; throwaway only")


def _plain_url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _async_url() -> str:
    return _plain_url().replace("postgresql://", "postgresql+asyncpg://", 1)


async def _apply(db, path: Path) -> None:
    from db.sql_migrations import split_statements

    for stmt in split_statements(path.read_text(encoding="utf-8")):
        code = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--"))
        if code.strip().rstrip(";").upper() in {"", "BEGIN", "COMMIT"}:
            continue
        await db.execute(stmt)


@pytest.fixture()
async def db(monkeypatch):
    import databases
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import db.database as dbmod
    from db.catalog import catalog_products, catalog_skus

    _assert_throwaway_database()
    admin = databases.Database(_async_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = databases.Database(_async_url(), server_settings={"search_path": _SCHEMA})
    await scoped.connect()
    try:
        for table in (catalog_products, catalog_skus):
            await scoped.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
        for name in _MORE:
            await _apply(scoped, _MIGRATIONS / name)
        await _apply(scoped, _UP)
        # intake_identity reads `from db.database import database` at call time.
        monkeypatch.setattr(dbmod, "database", scoped)
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


@pytest.fixture()
async def conn(db):
    import asyncpg

    c = await asyncpg.connect(_plain_url(), server_settings={"search_path": _SCHEMA})
    try:
        yield c
    finally:
        await c.close()


async def _product(db, key, merchant, title, content_key, *, gtin=None, domain="shop.example",
                   created="2026-09-01", suppressed=None, brand="Guerlain"):
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand,"
        " content_key, gtin, source_domain, created_at, suppression_reason)"
        " VALUES (:k, :m, 'external_seed', :k, :t, :b, :ck, :g, :d, :c, :s)",
        {"k": key, "m": merchant, "t": title, "b": brand, "ck": content_key, "g": gtin, "d": domain,
         "c": datetime.fromisoformat(created), "s": suppressed},
    )
    await db.execute(
        "INSERT INTO product_group_members (product_group_id, merchant_id, platform, platform_product_id)"
        " VALUES (:pg, :m, 'external_seed', :k)",
        {"pg": f"pg_{content_key}", "m": merchant, "k": key},
    )


async def _sku(db, product_key, merchant, variant, title, barcode, *, suppressed=None):
    await db.execute(
        "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id,"
        " source_variant_id, barcode, title, suppression_reason)"
        " VALUES (:sk, :p, :m, 'external_seed', :p, :v, :b, :t, :s)",
        {"sk": f"{product_key}::v:{variant}", "p": product_key, "m": merchant, "v": variant, "b": barcode,
         "t": title, "s": suppressed},
    )


async def _index_def(db):
    return await db.fetch_one(
        "SELECT pg_get_expr(i.indpred, i.indrelid) AS pred, pg_get_indexdef(i.indexrelid) AS def "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = :n AND n.nspname = :s",
        {"n": _INDEX, "s": _SCHEMA},
    )


async def test_migration_262_builds_partial_drops_and_reruns(db):
    from db.sql_migrations import needs_autocommit

    assert needs_autocommit(_UP.read_text(encoding="utf-8"))
    assert needs_autocommit(_DOWN.read_text(encoding="utf-8"))
    row = await _index_def(db)
    assert row is not None and row["pred"] == "(barcode IS NOT NULL)", row and row["def"]
    await _apply(db, _DOWN)
    assert await _index_def(db) is None
    for _ in range(2):  # IF NOT EXISTS: re-runnable
        await _apply(db, _UP)
    assert await _index_def(db) is not None


async def test_the_lookup_finds_product_gtins_and_raw_sku_spellings_live_rows_only(db):
    from services.intake_identity import _rows_by_barcodes

    await _product(db, "pf_shalimar", "m_perfumania", "Shalimar Perfume", "ck_pf", gtin="03346470113541",
                   created="2026-09-01")
    await _product(db, "be_shalimar", "m_beautyencounter", "Shalimar by Guerlain for Women", "ck_be",
                   created="2026-09-02")
    await _sku(db, "be_shalimar", "m_beautyencounter", "1", "1.0 oz", SHALIMAR)
    await _sku(db, "be_shalimar", "m_beautyencounter", "2", "1.6 oz", "3346470113602")
    await _sku(db, "be_shalimar", "m_beautyencounter", "3", "gone", SHALIMAR, suppressed="stale")
    await _product(db, "dead", "m_dead", "Shalimar", "ck_dead", gtin="03346470113541", suppressed="d2_x")
    await _product(db, "opi_girl", "m_uns", "It's A Girl #H39", "ck_opi", created="2026-08-01", brand="OPI")
    await _sku(db, "opi_girl", "m_uns", "1", "Default Title", OPI)

    rows = await _rows_by_barcodes(["03346470113541", "00619828139641"], "m_beautyencounter")
    got = [(r["product_key"], r["match_source"], r["matched_barcode"], r["matched_gtin"]) for r in rows]
    # Same merchant first, then oldest; a product once per way it carries a barcode.
    assert got == [
        ("be_shalimar", "sku_barcode", SHALIMAR, "03346470113541"),
        ("opi_girl", "sku_barcode", OPI, "00619828139641"),
        ("pf_shalimar", "product_gtin", "03346470113541", "03346470113541"),
    ]
    assert rows[0]["sku_title"] == "1.0 oz" and rows[0]["source_domain"] == "shop.example"
    assert rows[2]["sku_key"] is None and rows[2]["sku_title"] is None


async def test_both_arms_are_served_by_their_partial_indexes(db):
    from services.intake_identity import barcode_lookup_sql, gtin_spellings

    spellings = gtin_spellings("03346470113541")
    params = {f"b{i}": s for i, s in enumerate(spellings)} | {"merchant_id": "m"}
    async with db.transaction():
        # Empty tables: forbid the seq scan so the plan shows what each arm CAN use.
        await db.execute("SET LOCAL enable_seqscan = off")
        plan = await db.fetch_val("EXPLAIN (FORMAT JSON) " + barcode_lookup_sql(len(spellings)), params)
    plan = json.loads(plan) if isinstance(plan, str) else plan

    def _nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from _nodes(child)

    used = {n.get("Index Name") for n in _nodes(plan[0]["Plan"])}
    assert {"idx_catalog_products_gtin", _INDEX} <= used, plan


async def test_the_review_dedupe_reads_the_task_enqueue_writes(db):
    from services.audit_index_intake import enqueue_audit_identity_review
    from services.intake_identity import _open_identity_review_exists

    assert await _open_identity_review_exists("ext:retailer:abc", "gtin_reused_within_seller") is False
    task = await enqueue_audit_identity_review(
        {"product_key": "ext:retailer:abc", "content_key": "ck_x"},
        {"product_key": "other", "matcher": "gtin_reused_within_seller", "confidence": None, "evidence": {}},
    )
    assert task is not None
    assert await _open_identity_review_exists("ext:retailer:abc", "gtin_reused_within_seller") is True
    assert await _open_identity_review_exists("ext:retailer:abc", "variant_barcode_ambiguous_family") is False
    await db.execute("UPDATE pdp_review_tasks SET status = 'resolved' WHERE id = :i", {"i": task})
    assert await _open_identity_review_exists("ext:retailer:abc", "gtin_reused_within_seller") is False


async def test_the_resolver_attaches_a_new_listing_through_the_real_sql(db, monkeypatch):
    """End to end on Postgres: every Tier-0a lookup is real (provenance/review writes are best-effort
    and land in tables this schema lacks or has)."""
    from services.intake_identity import ACTION_FLAG, VARIANT_BARCODE_MATCH_ENV, resolve_or_attach_content_identity

    monkeypatch.setenv(VARIANT_BARCODE_MATCH_ENV, "1")  # default OFF until migration 262 lands

    await _product(db, "pf_shalimar", "m_perfumania", "Shalimar Perfume", "ck_pf", gtin="03346470113541")
    out = await resolve_or_attach_content_identity(
        "Guerlain", "Shalimar by Guerlain for Women", gtin=None,
        source_product_id="be_new", door="catalog_enrichment",
        merchant_ctx={"merchant_id": "m_beautyencounter", "platform": "external_seed",
                      "product_key": "ext:retailer:be_new", "source_domain": "beautyencounter.com"},
        variants=[{"barcode": SHALIMAR, "title": "1.0 oz"}, {"barcode": "3346470113602", "title": "1.6 oz"}],
    )
    assert (out["action"], out["content_key"]) == (ACTION_FLAG, "ck_pf")
    assert out["evidence"]["matcher"] == "variant_barcode_match_brand_title_drift"
    assert out["product_group_id"] == "pg_ck_pf"


async def test_the_backfill_sql_pairs_sellers_and_drops_the_reused_barcode(db, conn):
    from services.identity_barcode_link import STRATEGY, load_and_build
    from services.identity_resolution import upsert_proposals

    # Shalimar: perfumania (older, product gtin) and a beautyencounter family (SKU barcode, raw 13).
    await _product(db, "ext:retailer:pf", "m_perfumania", "Shalimar Perfume", "ck_pf",
                   gtin="03346470113541", created="2026-09-01")
    await _product(db, "ext:retailer:be", "m_beautyencounter", "Shalimar by Guerlain for Women", "ck_be",
                   created="2026-09-05")
    await _sku(db, "ext:retailer:be", "m_beautyencounter", "1", "1.0 oz", SHALIMAR)
    await _sku(db, "ext:retailer:be", "m_beautyencounter", "2", "bad check", "3346470113542")
    # OPI: one store's barcode on two shades, and ulta carrying it -- the guard refuses it.
    await _product(db, "ext:retailer:g", "m_uns", "It's A Girl #H39", "ck_g", brand="OPI")
    await _sku(db, "ext:retailer:g", "m_uns", "1", "Default Title", OPI)
    await _product(db, "ext:retailer:b", "m_uns", "Barefoot In Barcelona #E41", "ck_b", brand="OPI")
    await _sku(db, "ext:retailer:b", "m_uns", "1", "Default Title", " 0619828139641 ")
    await _product(db, "ext:retailer:u", "m_ulta", "GelColor It's A Girl", "ck_u", brand="OPI",
                   gtin="00619828139641")

    proposals, counts, pairs = await load_and_build(conn)
    assert [(p["a"]["product_key"], p["b"]["product_key"], p["barcode"]) for p in pairs] == [
        ("ext:retailer:pf", "ext:retailer:be", "03346470113541")]
    assert counts["barcode_untrusted:titles_differ_within_seller"] == 1
    assert [(p["keeper_product_key"], p["evidence"]["listing_product_key"], p["content_key"])
            for p in proposals] == [("ext:retailer:pf", "ext:retailer:be", "ck_pf")]
    assert proposals[0]["evidence"]["from_product_group_id"] == "pg_ck_be"
    assert proposals[0]["evidence"]["to_product_group_id"] == "pg_ck_pf"

    written = await upsert_proposals(conn, proposals)
    assert written == {"proposed": 1, "inserted": 1, "deduped": 0}
    row = await conn.fetchrow(
        "SELECT kind, strategy, status FROM identity_resolution_proposals WHERE proposal_id = $1",
        proposals[0]["proposal_id"])
    assert dict(row) == {"kind": "attach_membership", "strategy": STRATEGY, "status": "proposed"}
    assert (await upsert_proposals(conn, proposals))["deduped"] == 1
