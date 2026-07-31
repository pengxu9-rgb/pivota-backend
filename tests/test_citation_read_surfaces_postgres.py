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

Tables are created empty, because the failure class is in PARSE/PREPARE and zero
rows are enough to detect it. ONE test is the exception and inserts three rows:
every one of these statements sits behind a fail-closed `except`, so a broken
query returns False/None rather than raising, and only a POSITIVE answer proves
the statement ran at all. That test therefore encodes real eligibility semantics
— an active seed on the content route plus `serving_eligible`, i.e. get_pdp_v2's
two gates. Keep it at exactly that. A fixture that is subtly wrong about
eligibility is how a Postgres gate manufactures false confidence, and the
row-level matrix belongs in the SQLite suites, which are the right engine for it.

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
CREATE TABLE IF NOT EXISTS index_pipeline_state (content_key text PRIMARY KEY);
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS serving_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS index_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS pipeline_stage text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_code text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_detail text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS content_quality_score double precision;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS quality_scored_at timestamptz;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS last_extracted_at timestamptz;
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
async def test_sig_renderable_and_elected_lookup_answer_for_a_real_row(db_connected):
    """Both embedded-SQL strings must be capable of a POSITIVE answer.

    A fail-closed guard that is permanently broken also returns False/None, so a
    negative result proves nothing about whether the statement ran. This is the
    only test in the file that can tell "nothing renders" from "the check is
    dead", which is why it is worth the fixture.

    THE ONE PLACE THIS FILE ENCODES ROW SEMANTICS, deliberately and narrowly: an
    active seed on the content route plus `serving_eligible`, i.e. exactly
    get_pdp_v2's two gates and nothing else. Do not grow this into an eligibility
    matrix — the SQLite suites own that, and a fixture that is subtly wrong about
    eligibility is how a Postgres gate produces false confidence.
    """
    from db.database import database
    from routes.agent_citation_v1 import _elected_canonical_sig, _sig_renderable

    sig = "sig_" + "a" * 32
    elected = "sig_" + "b" * 32
    # Every insert inside the try: a failure part-way through must still clean up,
    # or `pk_render_probe` survives and wedges every later run on a duplicate key.
    try:
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
        assert await _sig_renderable(sig) is True

        # The OTHER embedded string this change adds on the same route, behind the
        # same shape of fail-closed except (returns None). Uncovered, it would be
        # free to be silently broken in exactly the way the fragment was. The
        # election names a sig that is NOT electable here (no row backs it), so
        # the validated lookup must answer None — a bare table read would answer
        # with the stored winner, which is the failure this validation prevents.
        await database.execute(
            (
                "INSERT INTO content_canonical_election (content_key,"
                " canonical_sig_id) VALUES (:ck, :elected)"
            ),
            {"ck": "ck_render_probe", "elected": elected},
        )
        assert await _elected_canonical_sig("ck_render_probe") is None
    finally:
        await database.execute(
            "DELETE FROM content_canonical_election WHERE content_key = :ck",
            {"ck": "ck_render_probe"},
        )
        await database.execute(
            "DELETE FROM external_product_seeds WHERE product_key = :pk",
            {"pk": "pk_render_probe"},
        )
        await database.execute(
            "DELETE FROM index_pipeline_state WHERE content_key = :ck",
            {"ck": "ck_render_probe"},
        )
        await database.execute(
            "DELETE FROM catalog_products WHERE product_key = :pk",
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


@pytest.mark.asyncio
async def test_agent_pdp_gated_lanes_execute_on_postgres(db_connected):
    """`GET /api/agent/pdp/{id}` — all THREE gated lanes and both eligibility
    variants, now that #1602 splices the renderability fragment into them inline.

    THE BLAST RADIUS HERE IS WORSE THAN THE CITATION SURFACES ABOVE, which is why
    this test exists rather than trusting the shape assertions. `_sig_renderable`
    is fail-closed — a broken fragment there logs and returns False, so the
    citation read still serves. These SELECTs have no such wrapper: a fragment
    that fails to PARSE or PREPARE is a 500 on EVERY agent PDP read, on the
    surface the gateway itself calls.

    Executes rather than inspects, for exactly the reason this file opens with:
    the fragment is a Postgres-dialect string, so nothing on the SQLite suite ever
    parses it, and the first version of it was not valid SQL while 142 tests
    passed.
    """
    from db.database import database
    from routes import agent_pdp_v1

    lanes = {
        "content_key": (agent_pdp_v1.SELECT_BY_CONTENT_KEY_SQL, "ck_" + "0" * 32),
        "signature": (agent_pdp_v1.SELECT_BY_SIGNATURE_SQL, "sig_" + "0" * 32),
        "product_group": (agent_pdp_v1.SELECT_BY_PRODUCT_GROUP_SQL, "pg_" + "0" * 32),
        # INDEX_ELIGIBLE_READ widened forms — a separate compiled string each.
        "content_key/widened": (
            agent_pdp_v1.INDEX_SELECT_BY_CONTENT_KEY_SQL,
            "ck_" + "0" * 32,
        ),
        "signature/widened": (
            agent_pdp_v1.INDEX_SELECT_BY_SIGNATURE_SQL,
            "sig_" + "0" * 32,
        ),
        "product_group/widened": (
            agent_pdp_v1.INDEX_SELECT_BY_PRODUCT_GROUP_SQL,
            "pg_" + "0" * 32,
        ),
    }
    for name, (sql, lookup_id) in lanes.items():
        row = await database.fetch_one(sql, {"id": lookup_id})
        assert row is None, f"{name} lane unexpectedly matched an empty table"


@pytest.mark.asyncio
async def test_agent_pdp_bypass_lanes_execute_on_postgres(db_connected):
    """The emergency lanes must also prepare — they are what an operator reaches
    for when the gated ones are failing, so a syntax error there is discovered at
    the worst possible moment. They carry NO renderability fragment by design."""
    from db.database import database
    from routes import agent_pdp_v1

    for sql, lookup_id in (
        (agent_pdp_v1.BYPASS_SELECT_BY_CONTENT_KEY_SQL, "ck_" + "0" * 32),
        (agent_pdp_v1.BYPASS_SELECT_BY_SIGNATURE_SQL, "sig_" + "0" * 32),
        (agent_pdp_v1.BYPASS_SELECT_BY_PRODUCT_GROUP_SQL, "pg_" + "0" * 32),
    ):
        assert await database.fetch_one(sql, {"id": lookup_id}) is None
