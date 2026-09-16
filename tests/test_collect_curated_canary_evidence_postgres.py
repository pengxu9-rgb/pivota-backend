"""Plan the collector's statements against real PostgreSQL.

The unit tests drive a fake connection, which by construction cannot know whether a column exists:
the first version of this collector selected `catalog_products.market` and `.currency` — columns
that table does not have — and shipped green because the fixture rows carried those keys. It also
queried `product_group_members.product_key`, which does not exist either (that table is keyed by
merchant_id/platform/platform_product_id), so every product reported "no group" including ones that
have one.

EXPLAIN parses, resolves every name and plans the query without executing it. `product_group_members`
is migration-created rather than declared in db/catalog.py, so this is the only check in the suite
that can see its real shape at all.
"""
import os

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


@pytest.mark.parametrize("name,sql", [
    ("products", PRODUCT_SQL), ("skus", SKU_SQL), ("offers", OFFER_SQL),
    ("inci", INCI_SQL), ("groups", GROUP_SQL),
])
async def test_every_statement_plans_against_the_real_schema(name, sql):
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        # A text[] bind satisfies every statement: each takes exactly one, either hosts or keys.
        await conn.execute(f"EXPLAIN {sql}", [])
    except asyncpg.PostgresSyntaxError:
        raise
    except asyncpg.UndefinedColumnError as exc:
        pytest.fail(f"{name}: selects a column the schema does not have — {exc}")
    except asyncpg.UndefinedTableError as exc:
        pytest.fail(f"{name}: names a table that does not exist — {exc}")
    finally:
        await conn.close()
