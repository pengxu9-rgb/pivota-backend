"""Migration 243: a category_path prefix index over the WHOLE catalog.

069's idx_catalog_products_category_path_active is partial (internal_merchant only), and 94% of
catalog_products rows are external_seed, so the gateway's category-browse predicate had no index.
These tests apply 243 the way the runner does (one statement at a time, autocommit), in a scratch
schema so the shared dialect-gate database's catalog_products is never touched, and pin that the
index is not partial and that the planner can serve the gateway's exact category arm with it.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_catalog_products_category_path_index_postgres.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"
_UP = _MIGRATIONS / "243_catalog_products_category_path_pattern_index.sql"
_DOWN = _MIGRATIONS / "down/243_catalog_products_category_path_pattern_index_down.sql"
_INDEX = "idx_catalog_products_category_path_pattern"
_SCHEMA = f"catalog_category_path_index_test_{os.getpid()}"

# The category arm exactly as PIVOTA-Agent canonicalCatalogSearch emits it (categoryPredicate).
_GATEWAY_CATEGORY_ARM = (
    "p.category_path IS NOT NULL AND (p.category_path = :exact OR p.category_path LIKE :prefix)"
)


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r}; throwaway only")


def _async_url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def _apply(db, path: Path) -> None:
    from db.sql_migrations import split_statements

    for stmt in split_statements(path.read_text(encoding="utf-8")):
        await db.execute(stmt)


async def _index_names(db) -> set[str]:
    rows = await db.fetch_all(
        "SELECT indexname FROM pg_indexes WHERE schemaname = :s AND tablename = 'catalog_products'",
        {"s": _SCHEMA},
    )
    return {r["indexname"] for r in rows}


@pytest.fixture(autouse=True)
async def db():
    import databases
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.catalog import catalog_products

    _assert_throwaway_database()
    admin = databases.Database(_async_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    # The schema ALONE on the path: nothing falls through to the shared public tables.
    scoped = databases.Database(_async_url(), server_settings={"search_path": _SCHEMA})
    await scoped.connect()
    try:
        # The model's own DDL, so category_path has prod's type (varchar(255)).
        await scoped.execute(str(CreateTable(catalog_products).compile(dialect=postgresql.dialect())))
        await _apply(scoped, _UP)
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


async def test_migration_243_builds_drops_and_reruns(db):
    from db.sql_migrations import needs_autocommit

    assert needs_autocommit(_UP.read_text(encoding="utf-8"))
    assert needs_autocommit(_DOWN.read_text(encoding="utf-8"))
    assert _INDEX in await _index_names(db)

    await _apply(db, _DOWN)
    assert _INDEX not in await _index_names(db)
    for _ in range(2):  # IF NOT EXISTS: re-runnable
        await _apply(db, _UP)
    assert _INDEX in await _index_names(db)


async def test_the_index_is_not_partial(db):
    # The whole point: 069's index stops at internal_merchant rows, and the catalog is external_seed.
    row = await db.fetch_one(
        "SELECT pg_get_expr(i.indpred, i.indrelid) AS pred, pg_get_indexdef(i.indexrelid) AS def "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = :n AND n.nspname = :s",
        {"n": _INDEX, "s": _SCHEMA},
    )
    assert row is not None
    assert row["pred"] is None, row["def"]
    assert "varchar_pattern_ops" in row["def"]


@pytest.mark.parametrize(
    "exact,prefix",
    [("beauty/haircare", "beauty/haircare/%"), ("beauty/skincare/tone", "beauty/skincare/tone/%")],
)
async def test_the_gateway_category_arm_is_served_by_the_index(db, exact, prefix):
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, "
        "category_path, catalog_track) VALUES "
        "('k1', 'external_seed', 'external_seed', 's1', 'Hair Mask', 'beauty/haircare/treatment', 'external_seed'), "
        "('k2', 'external_seed', 'external_seed', 's2', 'Toner', 'beauty/skincare/tone/toner', 'external_seed'), "
        "('k3', 'external_seed', 'external_seed', 's3', 'Lipstick', 'beauty/makeup/lip/lipstick', 'external_seed')"
    )
    async with db.transaction():
        # A three-row table: forbid the seq scan so the plan shows what the predicate CAN use.
        await db.execute("SET LOCAL enable_seqscan = off")
        plan = await db.fetch_val(
            f"EXPLAIN (FORMAT JSON) SELECT p.product_key FROM catalog_products p WHERE {_GATEWAY_CATEGORY_ARM}",
            {"exact": exact, "prefix": prefix},
        )
    plan = json.loads(plan) if isinstance(plan, str) else plan

    def _nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from _nodes(child)

    conds = [n.get("Index Cond", "") for n in _nodes(plan[0]["Plan"]) if n.get("Index Name") == _INDEX]
    # Both arms: the equality, and the LIKE prefix as a pattern range (~>=~ / ~<~).
    assert len(conds) == 2, plan
    assert any("=" in c and "~" not in c for c in conds), conds
    assert any("~>=~" in c for c in conds), conds
