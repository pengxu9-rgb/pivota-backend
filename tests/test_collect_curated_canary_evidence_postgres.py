"""Plan the collector's statements against a real PostgreSQL schema.

The unit tests drive a fake connection, which by construction cannot know whether a column exists:
the first version of this collector selected `catalog_products.market` and `.currency` — columns
that table does not have — and shipped green because the fixture rows carried those keys. It also
queried `product_group_members.product_key`, which does not exist either (that table is keyed by
merchant_id/platform/platform_product_id), so every product reported "no group" including ones that
have one.

EXPLAIN parses, resolves every name and plans the query without executing it.

THE SCHEMA IS BUILT HERE, in a private schema, rather than assumed. The dialect gate shares one
database between files and `product_group_members` has no `Table()` declaration — it exists only
where some other gate file happened to create it. Depending on that made this test pass or fail by
glob order (it failed: "relation product_group_members does not exist"), which is a worse outcome
than not testing at all, because the reason looks like a schema problem in the code under test.
"""
import os
import pathlib

import pytest

from scripts.collect_curated_canary_evidence import (
    GROUP_SQL,
    INCI_SQL,
    OFFER_SQL,
    PRODUCT_SQL,
    SKU_SQL,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"), reason="requires test Postgres"
)

_SCHEMA = "collector_explain_gate"


@pytest.fixture(scope="module")
def explain_schema():
    """A private schema holding the REAL metadata plus the REAL group-table migration."""
    from sqlalchemy import create_engine, text

    from db.catalog import metadata

    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    raw = create_engine(url, future=True)
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
    eng = create_engine(url, future=True, connect_args={"options": f"-csearch_path={_SCHEMA}"})
    with eng.begin() as c:
        metadata.create_all(bind=c)
        # No Table() declares this one; apply the migration rather than typing DDL that drifts.
        c.execute(text(pathlib.Path("db/migrations/045_product_groups.sql").read_text(encoding="utf-8")))
    yield url
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))


@pytest.mark.parametrize("name,sql", [
    ("products", PRODUCT_SQL), ("skus", SKU_SQL), ("offers", OFFER_SQL),
    ("inci", INCI_SQL), ("groups", GROUP_SQL),
])
async def test_every_statement_plans_against_the_real_schema(name, sql, explain_schema):
    import asyncpg

    conn = await asyncpg.connect(explain_schema, server_settings={"search_path": _SCHEMA})
    try:
        # One text[] bind satisfies every statement: each takes exactly one, hosts or keys.
        await conn.execute(f"EXPLAIN {sql}", [])
    except asyncpg.UndefinedColumnError as exc:
        pytest.fail(f"{name}: selects a column the schema does not have — {exc}")
    except asyncpg.UndefinedTableError as exc:
        pytest.fail(f"{name}: names a table that does not exist — {exc}")
    finally:
        await conn.close()


async def test_the_group_statement_is_planned_not_skipped(explain_schema):
    """Guard on the guard: if the fixture ever stops creating product_group_members, the test
    above would fail for an environmental reason and read as a defect in the query."""
    import asyncpg

    conn = await asyncpg.connect(explain_schema, server_settings={"search_path": _SCHEMA})
    try:
        exists = await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", f"{_SCHEMA}.product_group_members")
        assert exists, "the fixture did not create product_group_members"
        columns = set(await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = 'product_group_members'", _SCHEMA))
        names = {r["column_name"] for r in columns} if columns else set()
        assert {"merchant_id", "platform", "platform_product_id", "product_group_id"} <= names
        assert "product_key" not in names, (
            "product_group_members gained a product_key column — the collector's join, which goes "
            "through catalog_products because this table has no such column, should be revisited")
    finally:
        await conn.close()
