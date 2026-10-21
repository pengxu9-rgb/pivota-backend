"""scripts/census_pdp_live_price_drift.py on real Postgres: one read-only REPEATABLE READ transaction, three
reads, the own-seller SQL evaluated by Postgres, and a product line built from what the tables hold. (The
pure checks are in tests/test_census_pdp_live_price_drift.py.)

    DATABASE_URL=postgresql://127.0.0.1:5432/<tag>_test .venv/bin/python -m pytest \\
        tests/test_census_pdp_live_price_drift_postgres.py

ISOLATION. A per-process scratch schema built from the model plus migrations 136 and 246 (whose trigger
must not reach other files' catalog_offers), dropped at teardown.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import census_pdp_live_price_drift as census

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith(("postgresql://", "postgres://", "postgresql+asyncpg://"))
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_MIGRATIONS = Path(__file__).resolve().parents[1] / "db/migrations"
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_")
_SCHEMA = f"pdp_drift_census_test_{os.getpid()}"
HOST = "census-drift.example.com"
PK = "ext:snail-serum::0123abcd"
EPID = "census-brand:0123abcd"

# The seed columns the census reads (external_product_seeds is not a db.catalog model).
_SEEDS_DDL = """
CREATE TABLE external_product_seeds (
  id text PRIMARY KEY, external_product_id text, attached_product_key text, status text, market text,
  domain text, price_amount double precision, price_currency text, seed_data jsonb,
  created_at timestamptz, updated_at timestamptz, last_crawled_at timestamptz
)
"""


def _url() -> str:
    url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


@pytest.fixture(scope="module", autouse=True)
def _schema():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create a scratch schema in database {dbname!r} -- throwaway only")
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata
    from db.sql_migrations import split_statements

    admin = create_engine(_url())
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
    admin.dispose()
    engine = create_engine(_url(), connect_args={"options": f"-csearch_path={_SCHEMA}"})
    metadata.create_all(engine, tables=[metadata.tables[t] for t in (
        "catalog_merchants", "catalog_products", "catalog_skus", "catalog_offers")])
    with engine.begin() as conn:
        conn.execute(text(_SEEDS_DDL))
        for name in ("136_catalog_row_trust.sql", "246_catalog_offers_price_checked_at.sql"):
            for statement in split_statements((_MIGRATIONS / name).read_text()):
                if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
                    continue
                conn.connection.cursor().execute(statement)
    engine.dispose()
    yield
    admin = create_engine(_url())
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
    admin.dispose()


async def _seed_rows(conn):
    await conn.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, source_system,"
        " source_domain, canonical_url, brand, sync_status, pivota_signature_id)"
        " VALUES ($1, 'external_seed', 'external_seed', $2, 'Snail Serum', 'catalog_enrichment_agent_v1',"
        " $3, $4, 'Census Brand', 'live', 'sig_censusdrift1')",
        PK, EPID, "www." + HOST, f"https://{HOST}/products/snail-serum")
    await conn.execute(
        "INSERT INTO catalog_row_trust (subject_type, subject_key, product_key, source_lifecycle_state,"
        " identity_status, freshness_state, serving_decision, policy_version)"
        " VALUES ('product', $1, $1, 'active', 'approved', 'fresh', 'public', 'test')", PK)
    # Two variants: the first priced by an own row (the simple arm: the listing's own merchant), the
    # second's own row is out of stock, so the page shows it unpriced.
    for vid, amount, stock in (("11111111", 24.0, "in_stock"), ("22222222", 30.0, "out_of_stock")):
        sku = f"{PK}::v:{vid}"
        await conn.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id,"
            " source_variant_id, title, currency) VALUES ($1, $2, 'external_seed', 'external_seed', $3, $4, 't', 'USD')",
            sku, PK, EPID, vid)
        await conn.execute(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, market, currency, availability,"
            " list_price, merchant_effective_price, estimated_best_price, source_system, price_checked_at)"
            " VALUES ($1, $2, $3, 'external_seed', 'US', 'USD', $4, $5, $5, $5, 'catalog_enrichment_agent_v1',"
            " now() - interval '10 days')",
            f"off_{vid}", sku, PK, stock, amount)
    seed_data = {"variants": [{"variant_id": "11111111", "price_amount": 24, "price": 24},
                              {"variant_id": "22222222", "price_amount": 30, "price": 30}],
                 "snapshot": {"price_amount": 24, "price_currency": "USD"}}
    await conn.execute(
        "INSERT INTO external_product_seeds (id, external_product_id, attached_product_key, status, market, domain,"
        " price_amount, price_currency, seed_data, created_at, updated_at, last_crawled_at)"
        " VALUES ('seed_census_1', $1, $2, 'active', 'US', $3, 24, 'USD', $4::jsonb, now(), now(), now())",
        EPID, PK, HOST, json.dumps(seed_data))


async def test_the_census_runs_read_only_on_postgres():
    import asyncpg

    conn = await asyncpg.connect(_url(), server_settings={"search_path": _SCHEMA})
    try:
        await _seed_rows(conn)
        opened, statements = [], []

        class Spy:
            """The real connection, recording the transaction it is asked for and every statement."""

            def transaction(self, **kw):
                opened.append(kw)
                return conn.transaction(**kw)

            async def execute(self, sql, *args):
                statements.append(sql)
                return await conn.execute(sql, *args)

            async def fetchval(self, sql, *args):
                statements.append(sql)
                return await conn.fetchval(sql, *args)

            async def fetch(self, sql, *args):
                statements.append(sql)
                return await conn.fetch(sql, *args)

        lines = await census.census(Spy(), [HOST])
        assert opened == [{"readonly": True, "isolation": "repeatable_read"}]
        assert statements[0] == f"SET LOCAL statement_timeout = '{census.STATEMENT_TIMEOUT}'"
        assert statements[2:] == list(census.READ_STATEMENTS)
        (params,) = [ln for ln in lines if ln["kind"] == "params"]
        assert params["rows"] == {"products": 1, "offers": 2, "seeds": 1}
        (product,) = [ln for ln in lines if ln["kind"] == "product"]
        assert product["own_money_rows"] == 1
        assert product["own_offer_refusals"] == {"not_in_stock": 1}
        assert product["seed_route"] == "external_product_id" and product["variant_source"] == "variants"
        assert product["variant_counts"]["readable"] == 2
        assert product["variant_counts"]["readable_unpriced"] == 1
        assert product["verdict"] == "some_variants_unpriced_counted_as_changed"
        assert product["offer_flags"]["price_checked_at_stale"] == 2
        assert product["sample"][0]["stored"] == 24.0
        (host,) = [ln for ln in lines if ln["kind"] == "host"]
        assert host["products"] == 1 and host["verdicts"] == {"some_variants_unpriced_counted_as_changed": 1}
        assert all(t.startswith(census.CENSUS_MARKER) for t in census.render(lines))
        # the transaction wrote nothing (read-only would have refused it anyway)
        assert await conn.fetchval("SELECT count(*) FROM catalog_offers") == 2
    finally:
        await conn.close()
