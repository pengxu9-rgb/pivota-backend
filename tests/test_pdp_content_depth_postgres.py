"""THE GATE THAT WAS MISSING. Executes the canonical feed route on real Postgres.

Every other test in this repo runs on SQLite. That is why #1588 shipped a query
Postgres refuses to prepare: 149 tests passed, CI was green, two reviews signed
off, and `GET /api/canonical/products` 500ed for every caller the moment it
deployed. Reverted 16 minutes later in #1590.

WHY A COMPILE-LEVEL CHECK IS NOT ENOUGH, measured 2026-07-26. The sibling test
`test_content_depth_emits_no_indeterminate_params` inspects the compiled SQL,
and it does catch this particular regression. But the outage does NOT reproduce
from the expression alone: executing `pdp_content_depth_expression` by itself
against real Postgres, with the real JSONB type, SUCCEEDS both before and after
the fix. Postgres only refuses the parameter inside the full route query, where
it lands at `$28` among 28 binds:

    pre-fix : IndeterminateDatatypeError: could not determine data type of parameter $28
    fixed   : OK

So the only check that actually models production is executing the real route
against a real engine. A predicate can be individually valid and still be
un-preparable in the statement that ships it.

RUNNING THIS. Skipped unless DATABASE_URL points at a Postgres — the module
also selects JSONB_TYPE from that URL at import time, so a Postgres URL is
what makes this exercise the production types rather than generic JSON.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_pdp_content_depth_postgres.py

The database is created and left empty: the failure is in statement PREPARE, so
zero rows are required to detect it. Never point this at prod.
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

# Tables the feed reads that are declared as lightweight `table()` constructs
# rather than MetaData `Table`s, so `metadata.create_all` does not know them.
# Column sets are the ones the query actually touches.
_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS external_product_seeds (
  external_product_id text, attached_product_key text, status text,
  merchant_id text, source text, product_key text, source_product_id text
);
CREATE TABLE IF NOT EXISTS index_pipeline_state (
  content_key text, serving_eligible boolean, index_eligible boolean,
  blocker_code text, blocker_detail text,
  content_quality_score double precision, quality_scored_at timestamp
);
CREATE TABLE IF NOT EXISTS content_canonical_election (
  content_key text, canonical_sig_id text
);
CREATE TABLE IF NOT EXISTS aurora_product_intel_kb (kb_key text, analysis jsonb);
"""


@pytest.fixture(scope="module")
def pg_schema():
    """Build the feed's schema in the target database. Empty tables suffice."""
    import db.catalog  # noqa: F401  (registers Tables on the shared MetaData)
    import routes.pivota_canonical_routes  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(statement))
    yield engine
    engine.dispose()


@pytest.mark.asyncio
async def test_canonical_feed_route_prepares_on_postgres(pg_schema):
    """The whole route must PREPARE and execute. This is the #1588 regression.

    Asserting on the route rather than the expression is the entire point: the
    expression alone passes even when the route it ships in cannot prepare.
    """
    from db.database import database
    from routes.pivota_canonical_routes import list_canonical_pdp_signatures

    await database.connect()
    try:
        result = await list_canonical_pdp_signatures(limit=200, offset=0, cursor=None)
    finally:
        await database.disconnect()

    # Empty database, so the contract is what matters, not the contents.
    assert result["items"] == []
    assert result["total"] == 0


# DELIBERATELY NOT TESTED HERE: which rows come back, and whether a given row
# scores deep. Row-level semantics are covered exhaustively by the SQLite matrix
# in test_pdp_content_depth.py, which is the right engine for that — it is fast
# and needs no server. This file exists for the one thing SQLite structurally
# cannot answer: whether Postgres will PREPARE the statement the route ships.
# Keeping the scope that narrow is deliberate. A Postgres fixture that has to
# satisfy the route's full eligibility join is easy to get subtly wrong, and a
# wrong fixture here would produce exactly the false confidence that caused the
# outage this file guards against.
