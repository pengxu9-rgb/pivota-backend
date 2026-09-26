"""The seed serving-currency conjunct, executed on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_external_seed_serving_currency_postgres.py

tests/test_external_seed_serving_currency.py proves the rule on SQLite. This file proves what
SQLite cannot: that Postgres plans `upper(trim(coalesce(price_currency, ''))) = :serving_currency`
with a BOUND currency against the real TEXT column (the #1588 class -- a bind Postgres cannot type
is invisible to SQLite), inside the `SET LOCAL statement_timeout` transaction prod takes, beside
the quarantine anti-join and the recall text clause.

ISOLATION. The dialect gate runs every file against ONE database, so this file never touches
`public`: it creates a per-process scratch schema, applies the REAL migrations there (the table
under test is 044 + 169 + 200, the anti-join's table is 134) through a connection whose
search_path is that schema ALONE, and drops it at teardown.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"
_TABLE_MIGRATIONS = (
    "044_external_product_seeds.sql",
    "169_external_product_seeds_seller_ref.sql",
    "200_external_seed_destination_liveness.sql",
    "134_catalog_source_quarantine.sql",
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"seed_serving_currency_test_{os.getpid()}"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _url() -> str:
    # The form prod's `database` carries (db.database._normalize_database_url): a plain
    # postgresql:// URL is what makes fetch_external_seed_rows take its SET LOCAL path.
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


@pytest.fixture()
async def scoped_db():
    import databases

    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    admin = databases.Database(_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = databases.Database(_url(), server_settings={"search_path": _SCHEMA})
    await scoped.connect()
    try:
        for name in _TABLE_MIGRATIONS:
            for statement in split_statements((_MIGRATIONS / name).read_text()):
                if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
                    continue
                await scoped.execute(statement)
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


# The prod shapes measured 2026-09-26 (see tests/test_external_seed_serving_currency.py).
CORPUS = [
    ("us_usd", "US", "USD"),
    ("us_usd_padded", "US", " usd "),
    ("us_sgd", "US", "SGD"),
    ("us_gbp", "US", "GBP"),
    ("us_aud", "US", "AUD"),
    ("us_jpy", "US", "JPY"),
    ("us_null", "US", None),
    ("us_blank", "US", ""),
    ("jp_jpy", "JP", "JPY"),
    ("kr_krw", "KR", "KRW"),
]


async def _load(db, *, quarantined_domain=None):
    for sid, market, currency in CORPUS:
        await db.execute(
            "INSERT INTO external_product_seeds (id, external_product_id, market, destination_url,"
            " domain, title, price_amount, price_currency, status)"
            " VALUES (:id, :id, :market, :url, :domain, 'hydrating serum', 10, :currency, 'active')",
            {
                "id": sid,
                "market": market,
                "url": f"https://{sid}.example/products/serum",
                "domain": f"{sid}.example",
                "currency": currency,
            },
        )
    if quarantined_domain:
        await db.execute(
            "INSERT INTO catalog_source_quarantine (match_type, match_value, state, created_by)"
            " VALUES ('domain', :d, 'active', 'test')",
            {"d": quarantined_domain},
        )


async def _fetch(db, **kw):
    from services.external_seed_search import fetch_external_seed_rows

    kw.setdefault("query", "serum")
    kw.setdefault("limit", 50)
    kw.setdefault("only_unattached", False)
    kw.setdefault("include_total_count", True)
    kw.setdefault("query_timeout_seconds", 5.0)
    return await fetch_external_seed_rows(database=db, **kw)


def _ids(result):
    return sorted(r["id"] for r in result["rows"])


@pytest.mark.asyncio
async def test_the_prod_path_is_the_one_under_test(scoped_db):
    from services.external_seed_search import _database_supports_statement_timeout

    assert _database_supports_statement_timeout(scoped_db), (
        "the scoped connection must take fetch_external_seed_rows' SET LOCAL path, as prod does"
    )


@pytest.mark.asyncio
async def test_a_us_read_serves_usd_and_refuses_sgd_gbp_aud_jpy_and_no_currency(scoped_db):
    await _load(scoped_db)
    result = await _fetch(scoped_db, market="US")
    assert result["query_timeout"] is False and result["table_missing"] is False
    assert _ids(result) == ["us_usd", "us_usd_padded"]
    assert result["total_count"] == 2


@pytest.mark.asyncio
async def test_the_unpartitioned_multi_lane_read_is_usd_for_a_market_less_request(scoped_db):
    await _load(scoped_db)
    result = await _fetch(scoped_db, market=None, serving_market=None)
    assert _ids(result) == ["us_usd", "us_usd_padded"]


@pytest.mark.asyncio
async def test_a_jp_buyer_on_the_unpartitioned_lane_gets_jpy(scoped_db):
    await _load(scoped_db)
    result = await _fetch(scoped_db, market=None, serving_market="JP")
    assert _ids(result) == ["jp_jpy", "us_jpy"]


@pytest.mark.asyncio
async def test_the_lean_and_text_scan_shapes_plan_with_the_conjunct(scoped_db):
    """Stage A's lean WHERE and stage B's seed_data text scan are different statements."""
    await _load(scoped_db)
    lean = await _fetch(
        scoped_db, market=None, query="hydrating serum", lean_where_min_tokens=2, fast_multiterm=True
    )
    scan = await _fetch(scoped_db, market=None, include_seed_data_text_match=True)
    assert _ids(lean) == ["us_usd", "us_usd_padded"]
    assert _ids(scan) == ["us_usd", "us_usd_padded"]


@pytest.mark.asyncio
async def test_the_quarantine_anti_join_still_composes(scoped_db):
    await _load(scoped_db, quarantined_domain="us_usd.example")
    result = await _fetch(scoped_db, market=None)
    assert _ids(result) == ["us_usd_padded"]
    assert result["total_count"] == 1
