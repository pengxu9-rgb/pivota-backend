"""Executes the citation read surfaces on real Postgres. The gate this PR needed.

WHY THIS FILE EXISTS. The renderability signal ships partly as a compiled SQL
FRAGMENT (`services.pdp_renderability.sig_pdp_will_render_sql`) that two callers
splice into hand-written SQL. The first version of that function returned a
fragment with SQLAlchemy's implicit `AS anon_1` label still on the tail, so
wrapping it in parentheses — which is what both callers do — produced

    ERROR:  syntax error at or near "AS"

on every execution. The SQLite suite could not see it (the fragment is a
Postgres-dialect string, so nothing on SQLite ever runs it) and the shape test
that existed asserted only on the HEAD of the string. 142 tests passed while two
routes were dead.

This is the same lesson as `test_pdp_content_depth_postgres.py`, one level
sharper: there, an expression compiled fine and only the full statement failed to
PREPARE. Here the fragment itself was not valid SQL and no amount of inspecting
it in Python said so. Only a real server parsing the real statement does.

RUNNING THIS. Skipped unless DATABASE_URL points at a Postgres. `db/database.py`
picks JSONB_TYPE from that URL AT IMPORT, so it must be set before pytest imports
anything or the test silently exercises generic JSON and proves nothing.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_citation_read_surfaces_postgres.py

Tables are created empty. The failure class is in PARSE/PREPARE, so zero rows are
enough — and an empty fixture cannot be subtly wrong about eligibility semantics
the way a populated one can. Row-level semantics stay in the SQLite matrices.
Never point this at prod.
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith(
    "postgres://"
)

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

# Tables these reads touch that are declared as lightweight `table()` constructs
# rather than MetaData `Table`s, so `metadata.create_all` does not know them.
# Column sets are the ones the queries actually reference.
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
-- The evidence columns (migration 152) postdate `db.catalog.agent_pdp_view`'s
-- Core definition, which is why `routes/pivota_canonical_routes` reads them
-- through a local lightweight `table()` shim instead. `metadata.create_all`
-- therefore builds the table WITHOUT them, exactly as it would on any fresh
-- database, and prod gets them from db/schema_guard.py instead. Mirror that
-- here or the by-sig route cannot prepare.
ALTER TABLE agent_pdp_view
  ADD COLUMN IF NOT EXISTS evidence_profile jsonb,
  ADD COLUMN IF NOT EXISTS required_disclaimers jsonb
"""


@pytest.fixture(scope="module")
def pg_schema():
    """Build the schema these reads need. Empty tables suffice."""
    import db.catalog  # noqa: F401  (registers Tables on the shared MetaData)
    import routes.agent_citation_v1  # noqa: F401
    import routes.pivota_canonical_routes  # noqa: F401
    import services.pivot_query_service  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for chunk in _LIGHTWEIGHT_DDL.split(";"):
            statement = chunk.strip()
            # A chunk that is only comments is not an empty string but IS an
            # empty query as far as psycopg2 is concerned, and it raises. Skip.
            if not statement or all(
                not line.strip() or line.strip().startswith("--")
                for line in statement.splitlines()
            ):
                continue
            conn.execute(text(statement))
    yield engine
    engine.dispose()


@pytest.fixture()
async def db_connected(pg_schema):
    from db.database import database

    await database.connect()
    try:
        yield database
    finally:
        await database.disconnect()


@pytest.mark.asyncio
async def test_sig_renderable_query_executes(db_connected):
    """`_sig_renderable` answers False for an absent sig, without blowing up.

    NOTE WHAT THIS TEST CANNOT SEE — it is the whole trap. The call sits behind a
    fail-closed `except`, so a query that is invalid SQL also returns False.
    Verified by mutation: this assertion PASSES against the `AS anon_1` bug. The
    sibling below, which requires a True, is what distinguishes "nothing renders"
    from "the check is dead". Keep both — this one pins the absent-row contract,
    that one pins that the statement runs at all.
    """
    from routes.agent_citation_v1 import _sig_renderable

    assert await _sig_renderable("sig_" + "0" * 32) is False  # absent row, not an error


@pytest.mark.asyncio
async def test_sig_renderable_reports_true_for_a_row_that_renders(db_connected):
    """And it must be capable of answering True — a fail-closed guard that is
    permanently broken also returns False, so False alone proves nothing."""
    from db.database import database
    from routes.agent_citation_v1 import _sig_renderable

    sig = "sig_" + "a" * 32
    await database.execute(
        (
            "INSERT INTO catalog_products (product_key, content_key,"
            " pivota_signature_id, merchant_id, platform, source_product_id,"
            " title, source_system)"
            " VALUES (:pk, :ck, :sig, 'external_seed', 'external_seed',"
            " 'ext_render_probe', 'Render Probe', 'external_seed')"
        ),
        {"pk": "pk_render_probe", "ck": "ck_render_probe", "sig": sig},
    )
    await database.execute(
        (
            "INSERT INTO index_pipeline_state (content_key, serving_eligible,"
            " index_eligible) VALUES (:ck, TRUE, TRUE)"
        ),
        {"ck": "ck_render_probe"},
    )
    await database.execute(
        (
            "INSERT INTO external_product_seeds (external_product_id,"
            " attached_product_key, status, merchant_id, source, product_key,"
            " source_product_id) VALUES ('ext_render_probe', 'pk_render_probe',"
            " 'active', 'external_seed', 'external_seed', 'pk_render_probe',"
            " 'ext_render_probe')"
        )
    )
    try:
        assert await _sig_renderable(sig) is True
    finally:
        await database.execute(
            ("DELETE FROM external_product_seeds WHERE product_key = :pk"),
            {"pk": "pk_render_probe"},
        )
        await database.execute(
            ("DELETE FROM index_pipeline_state WHERE content_key = :ck"),
            {"ck": "ck_render_probe"},
        )
        await database.execute(
            ("DELETE FROM catalog_products WHERE product_key = :pk"),
            {"pk": "pk_render_probe"},
        )


@pytest.mark.asyncio
async def test_citable_recall_lane_executes(db_connected):
    """The ADR-008 slice-3 citable lane, which splices the SAME fragment plus the
    election subquery. It is wrapped in a broad `except` one level up, so a
    broken statement here returns zero rows on every query and logs — the lane
    goes dark silently. Execute it."""
    from services.pivot_query_service import _fetch_citable_canonical_rows

    rows = await _fetch_citable_canonical_rows(
        query="serum", merchant_id=None, limit=5
    )
    assert rows == []


@pytest.mark.asyncio
async def test_canonical_by_sig_route_executes(db_connected):
    """`GET /api/canonical/products/{sig}` — pure SQLAlchemy Core, so it was never
    at risk from the fragment bug, but it is the other surface carrying the new
    `renderable` field and it costs nothing to prepare it here."""
    from fastapi import HTTPException

    from routes.pivota_canonical_routes import get_canonical_pdp_by_signature

    with pytest.raises(HTTPException) as exc:
        await get_canonical_pdp_by_signature("sig_" + "0" * 32)
    assert exc.value.status_code == 404
