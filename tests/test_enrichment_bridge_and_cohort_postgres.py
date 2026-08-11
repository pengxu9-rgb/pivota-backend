"""The two identity-triple statements, EXECUTED against real Postgres.

Both statements here fail SILENTLY when they are wrong — they return zero rows
rather than raising — so a test that inspects their text cannot tell a working
one from a dead one.

  * `refresh_agent_pdp_view_for_enrichment_write` resolves a content_key from
    (merchant_id, platform, source_product_id). It shipped naming the column
    `platform_product_id`, which does not exist on catalog_products, so every
    call raised UndefinedColumn into a best-effort `except` and the publish
    bridge was dead. Its four existing unit tests all still PASS with the broken
    column restored, because `_FakeDB.fetch_one` returns a canned row whatever
    SQL it is handed. Nothing in the suite constrained the actual statement;
    only the repo-wide prepare gate did, and that proves plannability, not that
    the row is found.

  * `build_content_key_query(scope="enriched")` joins product_enrichment to
    catalog_products on that same triple. A mistake there returns an empty
    cohort, which reads exactly like "there is nothing to backfill". The
    existing test greps the SQL for the join text, so changing
    `WHERE cp.content_key IS NOT NULL` to `IS NULL` — a plausible typo that
    zeroes the cohort while leaving the asserted text intact — survives it.

Executing them is the only assertion that distinguishes those cases.

🚨 PRIVATE DATABASE, created and dropped here: this needs the real
catalog_products, and building it in the database every sibling
`test_*_postgres.py` shares is the blast radius those files warn about.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_DB_NAME = f"enrichment_bridge_{os.getpid()}"

_MERCHANT, _PLATFORM, _SOURCE_ID = "m-bridge", "shopify", "sp-bridge-1"
_CONTENT_KEY = "ck-bridge-1"


def _private_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{_DB_NAME}"))


def _product_enrichment_ddl() -> str:
    """Lifted from db/product_enrichment.py's own AST — never a hand-written stub,
    because PREPARE and the row semantics both resolve through column types."""
    import ast

    source = (REPO_ROOT / "db" / "product_enrichment.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "CREATE TABLE IF NOT EXISTS product_enrichment" in node.value
        ):
            return node.value
    raise AssertionError("could not find the product_enrichment CREATE TABLE")


@pytest.fixture(scope="module")
def db_url():
    import psycopg2
    from sqlalchemy import create_engine, text

    import db.catalog  # noqa: F401  (registers catalog_products on the shared MetaData)
    from db.database import metadata

    admin = psycopg2.connect(os.environ["DATABASE_URL"])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{_DB_NAME}"')
    admin.close()

    url = _private_url()
    engine = create_engine(url, future=True)
    metadata.tables["catalog_products"].create(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(text(_product_enrichment_ddl()))
        conn.execute(text(
            "INSERT INTO catalog_products "
            "(product_key, merchant_id, platform, source_product_id, title, content_key) "
            "VALUES (:pk, :m, :p, :s, :t, :ck)"
        ), {"pk": "pk-1", "m": _MERCHANT, "p": _PLATFORM, "s": _SOURCE_ID,
            "t": "Glow Serum", "ck": _CONTENT_KEY})
        # A second catalog row with NO enrichment, to prove the cohort is a
        # filter rather than "every content_key".
        conn.execute(text(
            "INSERT INTO catalog_products "
            "(product_key, merchant_id, platform, source_product_id, title, content_key) "
            "VALUES (:pk, :m, :p, :s, :t, :ck)"
        ), {"pk": "pk-2", "m": _MERCHANT, "p": _PLATFORM, "s": "sp-no-overlay",
            "t": "Dew Mist", "ck": "ck-no-overlay"})
        conn.execute(text(
            "INSERT INTO product_enrichment "
            "(merchant_id, platform, platform_product_id, geo_code, bullet_points) "
            "VALUES (:m, :p, :s, 'default', '[\"a\"]'::jsonb)"
        ), {"m": _MERCHANT, "p": _PLATFORM, "s": _SOURCE_ID})
    engine.dispose()
    try:
        yield url
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


def _drive(url, coro_factory):
    import databases

    async def run():
        database = databases.Database(
            url if "+asyncpg" in url else url.replace("postgresql://", "postgresql+asyncpg://")
        )
        await database.connect()
        try:
            return await coro_factory(database)
        finally:
            await database.disconnect()

    return asyncio.new_event_loop().run_until_complete(run())


def test_the_bridge_resolves_a_real_content_key(db_url, monkeypatch) -> None:
    """The statement that shipped dead, run for real.

    Only the content_key RESOLUTION is exercised: the rebuild it then delegates
    to is covered elsewhere and would drag in a dozen unrelated tables. The
    resolution is where the defect was.
    """
    from services import agent_pdp_view_assembler as apv

    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")
    seen = {}

    async def capture(content_key, *, refresh_source, db=None):
        seen["content_key"] = content_key
        seen["refresh_source"] = refresh_source
        return True

    monkeypatch.setattr(apv, "refresh_agent_pdp_view_for_content_key", capture)

    async def scenario(database):
        return await apv.refresh_agent_pdp_view_for_enrichment_write(
            _MERCHANT, _PLATFORM, _SOURCE_ID, db=database
        )

    assert _drive(db_url, scenario) is True, (
        "the bridge did not resolve a content_key for a row that exists — this is "
        "the failure mode that shipped, and it is invisible to a fake DB"
    )
    assert seen["content_key"] == _CONTENT_KEY
    assert seen["refresh_source"] == "enrichment_write"


def test_the_bridge_returns_false_for_an_unknown_product(db_url, monkeypatch) -> None:
    """The negative case must come from the DATABASE, not from a swallowed
    exception — which is exactly how the broken version also returned False."""
    from services import agent_pdp_view_assembler as apv

    monkeypatch.setenv("SERVE_PDP_ENRICHMENT_ON_WRITE", "1")

    async def boom(*args, **kwargs):  # must never be reached
        raise AssertionError("should not rebuild for an unknown product")

    monkeypatch.setattr(apv, "refresh_agent_pdp_view_for_content_key", boom)

    async def scenario(database):
        return await apv.refresh_agent_pdp_view_for_enrichment_write(
            _MERCHANT, _PLATFORM, "sp-does-not-exist", db=database
        )

    assert _drive(db_url, scenario) is False


def test_the_enriched_cohort_query_returns_only_enriched_keys(db_url) -> None:
    """Executed, not grepped. Two catalog rows exist and exactly one has an
    overlay, so a join that is wrong in EITHER direction — zero rows, or every
    key — fails here."""
    import scripts.backfill_agent_pdp_view as script

    sql, params = script.build_content_key_query(scope="enriched", limit=0, offset=0)

    async def scenario(database):
        return [r["content_key"] for r in await database.fetch_all(sql, params)]

    assert _drive(db_url, scenario) == [_CONTENT_KEY]


def test_the_all_scope_returns_every_key(db_url) -> None:
    import scripts.backfill_agent_pdp_view as script

    sql, params = script.build_content_key_query(scope="all", limit=0, offset=0)

    async def scenario(database):
        return sorted(r["content_key"] for r in await database.fetch_all(sql, params))

    assert _drive(db_url, scenario) == ["ck-bridge-1", "ck-no-overlay"]


def test_the_window_actually_bounds_the_cohort(db_url) -> None:
    """--limit/--offset are appended after ORDER BY; prove they page rather than
    silently returning everything."""
    import scripts.backfill_agent_pdp_view as script

    async def scenario(database):
        first_sql, first_params = script.build_content_key_query(
            scope="all", limit=1, offset=0)
        second_sql, second_params = script.build_content_key_query(
            scope="all", limit=1, offset=1)
        first = [r["content_key"] for r in await database.fetch_all(first_sql, first_params)]
        second = [r["content_key"] for r in await database.fetch_all(second_sql, second_params)]
        return first, second

    first, second = _drive(db_url, scenario)
    assert first == ["ck-bridge-1"]
    assert second == ["ck-no-overlay"]
