"""agent_pdp_view.market_prices on real Postgres (migration 263).

Pinned here, because only the server can answer them:

  * BEFORE the migration (the column absent), the flag-off write path -- UPSERT_SQL, unchanged --
    still inserts and updates, and the route's flag-off SELECT still reads. Prod deploys skip
    db/migrations/, so this is the state every environment is in until schema_guard runs.
  * Migration 263 applies, and re-applies, cleanly.
  * The flag-on upsert writes the summary as jsonb, the conflict branch overwrites it, '{}' is
    stored as an empty object (not NULL), and the route's market_prices SELECT reads it back into
    an SG buyer's SGD view.
  * The backfill's keyset query for rows missing the summary plans and seeks.

The statements are driven verbatim from the modules. PRIVATE DATABASE, created and dropped here,
like test_agent_pdp_view_overlay_preservation_postgres.py: the real agent_pdp_view is a db.catalog
Table, and building it in the database the sibling *_postgres.py files share is the blast radius
those files warn about.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_DB_NAME = f"apv_market_prices_{os.getpid()}"
_MIGRATION = REPO_ROOT / "db" / "migrations" / "263_agent_pdp_view_market_prices.sql"
CK = "ck_" + "f" * 32
SIG = "sig_" + "f" * 32


def _private_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{_DB_NAME}"))


@pytest.fixture(scope="module")
def db_url():
    import psycopg2

    admin = psycopg2.connect(os.environ["DATABASE_URL"])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{_DB_NAME}"')
    admin.close()
    try:
        yield _private_url()
    finally:
        admin = psycopg2.connect(os.environ["DATABASE_URL"])
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (_DB_NAME,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        admin.close()


def _sync(url: str, *statements: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(url, future=True)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
    finally:
        engine.dispose()


def _create_pre_migration_table(url: str) -> None:
    """The real model table as every environment has it BEFORE 263: the model now carries the
    column, so drop it to get the shipped pre-migration shape."""
    from sqlalchemy import create_engine

    import db.catalog  # noqa: F401  (registers agent_pdp_view on the shared MetaData)
    from db.database import metadata

    engine = create_engine(url, future=True)
    try:
        metadata.tables["agent_pdp_view"].drop(engine, checkfirst=True)
        metadata.tables["agent_pdp_view"].create(engine)
    finally:
        engine.dispose()
    _sync(url, "ALTER TABLE agent_pdp_view DROP COLUMN market_prices")


def _drive(url, coro_factory):
    import databases

    async def run():
        database = databases.Database(url.replace("postgresql://", "postgresql+asyncpg://"))
        await database.connect()
        try:
            return await coro_factory(database)
        finally:
            await database.disconnect()

    return asyncio.new_event_loop().run_until_complete(run())


def _offer(n, merchant, currency, market, price):
    return {
        "offer_id": f"of_{n}", "sku_key": f"sku_{n}", "product_key": "pk_store",
        "merchant_id": merchant, "availability": "in_stock", "currency": currency,
        "list_price": Decimal(price), "merchant_effective_price": None,
        "estimated_best_price": None, "market": market, "offer_type": "retail",
        "is_first_party": False, "merchant_name": merchant,
    }


OFFERS = [_offer(i, f"m_us{i}", "USD", "US", f"{18 + i}.00") for i in range(6)] + [
    _offer(10, "m_sg0", "SGD", "SG", "32.00"),
    _offer(11, "m_sg1", "SGD", "SG", "29.90"),
]


def _assembled(offers):
    from services.agent_pdp_view_assembler import assemble_row

    return assemble_row(
        content_key=CK,
        products=[{
            "product_key": "pk_store", "merchant_id": "m_store", "platform": "shopify",
            "source_product_id": "sp_1", "title": "Barrier Cream", "description": "A cream.",
            "brand": "Example", "product_payload": {}, "pdp_lifecycle_stage": "published",
            "pivota_signature_id": SIG, "canonical_url": "https://store.example.com/p/1",
            "sync_status": "live", "product_group_id": "grp_1", "group_is_primary": True,
        }],
        skus=[], offers=offers, external_seed=None,
    )


def test_market_prices_lifecycle(db_url, monkeypatch) -> None:
    from routes import agent_pdp_v1
    from services import agent_pdp_view_assembler as assembler

    monkeypatch.setenv("PIVOTA_SERVING_PRICING_REGIONS", "US,SG")
    monkeypatch.delenv(assembler.MARKET_PRICES_FLAG_ENV, raising=False)
    _create_pre_migration_table(db_url)

    legacy_row = _assembled(OFFERS)
    assert assembler.upsert_sql_for_row(legacy_row) is assembler.UPSERT_SQL
    legacy_select = agent_pdp_v1.BYPASS_SELECT_BY_CONTENT_KEY_SQL
    market_select = agent_pdp_v1._MARKET_PRICES_SQL[legacy_select]

    async def before_migration(database):
        # Flag off: insert, then the conflict branch, both on a table with no market_prices.
        for _ in range(2):
            await database.execute(assembler.UPSERT_SQL, assembler.row_to_upsert_params(legacy_row))
        row = await database.fetch_one(legacy_select, {"id": CK})
        assert row is not None and row["currency"] == "USD"
        # The flag-on SELECT is what must stay OFF until the column exists.
        with pytest.raises(Exception, match="market_prices"):
            await database.fetch_one(market_select, {"id": CK})

    _drive(db_url, before_migration)

    sql = _MIGRATION.read_text(encoding="utf-8")
    _sync(db_url, sql)
    _sync(db_url, sql)  # idempotent

    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")
    row = _assembled(OFFERS)
    empty = _assembled([])

    async def after_migration(database):
        # Not computed yet: NULL, and the route keeps the legacy view for an SG buyer.
        stored = agent_pdp_v1._row_to_dict(await database.fetch_one(market_select, {"id": CK}))
        assert stored["market_prices"] is None
        assert agent_pdp_v1._market_view(stored, "SG") is stored

        await database.execute(assembler.upsert_sql_for_row(row), assembler.row_to_upsert_params(row))
        typed = await database.fetch_one(
            "SELECT jsonb_typeof(market_prices) AS t, market_prices->'SG'->>'currency' AS sg_cur, "
            "(market_prices->'SG'->>'price_min')::numeric AS sg_min, "
            "jsonb_array_length(market_prices->'SG'->'offers') AS sg_offers, "
            "currency, price_min, offer_count, jsonb_array_length(offers) AS n_offers "
            "FROM agent_pdp_view WHERE content_key = :ck", {"ck": CK})
        assert dict(typed) == {
            "t": "object", "sg_cur": "SGD", "sg_min": Decimal("29.9"), "sg_offers": 2,
            # Legacy columns: exactly what the flag-off write stored.
            "currency": "USD", "price_min": Decimal("18.00"), "offer_count": 8, "n_offers": 5,
        }

        stored = agent_pdp_v1._row_to_dict(await database.fetch_one(market_select, {"id": CK}))
        view = agent_pdp_v1._market_view(stored, "SG")
        assert view["currency"] == "SGD" and view["price_min"] == 29.9
        assert [o["currency"] for o in view["offers"]] == ["SGD", "SGD"] + ["USD"] * 5
        assert agent_pdp_v1._market_view(stored, "US") is stored

        # A recompute that finds nothing priced writes {} (computed), never NULL.
        empty_params = assembler.row_to_upsert_params({**empty, "title": "Barrier Cream"})
        await database.execute(assembler.upsert_sql_for_row(empty), empty_params)
        assert await database.fetch_val(
            "SELECT market_prices::text FROM agent_pdp_view WHERE content_key = :ck", {"ck": CK}
        ) == "{}"

        # The backfill's keyset page: plans, seeks past :after, and skips computed rows.
        import scripts.backfill_agent_pdp_view as backfill

        await database.execute(
            "UPDATE agent_pdp_view SET market_prices = NULL WHERE content_key = :ck", {"ck": CK})
        page_sql, params = backfill.build_content_key_query(
            scope="market_prices_missing", limit=10, offset=0, after="")
        assert [r["content_key"] for r in await database.fetch_all(page_sql, params)] == [CK]
        page_sql, params = backfill.build_content_key_query(
            scope="market_prices_missing", limit=10, offset=0, after=CK)
        assert await database.fetch_all(page_sql, params) == []

    _drive(db_url, after_migration)


def test_down_migration_drops_the_column(db_url) -> None:
    _create_pre_migration_table(db_url)
    _sync(db_url, _MIGRATION.read_text(encoding="utf-8"))
    down = REPO_ROOT / "db" / "migrations" / "down" / "263_agent_pdp_view_market_prices_down.sql"
    _sync(db_url, down.read_text(encoding="utf-8"))

    async def check(database):
        return await database.fetch_val(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'agent_pdp_view' AND column_name = 'market_prices'")

    assert _drive(db_url, check) == 0
