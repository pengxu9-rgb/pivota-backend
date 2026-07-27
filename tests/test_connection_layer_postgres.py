"""Production-dialect gate for the ADR-018 connection-layer SQL twin.

WHY THIS EXISTS. ``services/connection_layer.connection_layer_sql`` is a
string-built SQL fragment carrying four correlated subqueries. Everything about
it — the correlation, the ``NOT EXISTS`` leg, the ``CASE`` arms' return types,
the parenthesisation — is invisible to SQLite and to code review. On 2026-07-26
this repo shipped a statement Postgres refused to PREPARE (#1588, untyped bind,
16-minute prod outage, 149 SQLite tests green, two reviews signed off), and on
2026-07-27 a sibling case where a compiled fragment carried an implicit
``AS anon_1`` and produced ``syntax error at or near "AS"`` with 142 SQLite tests
green. A fragment can be individually valid and still be un-preparable in the
statement that ships it, so this gate EXECUTES it inside a serving-shaped
statement rather than asserting on its text.

WHAT IS DIFFERENT HERE FROM THE SIBLING GATE. ``test_pdp_content_depth_postgres``
runs on empty tables on purpose: its failure is in PREPARE, so zero rows detect
it. This module needs both halves — PREPARE, and then the row-level semantics of
a four-arm ``CASE`` over correlated subqueries, which is precisely the shape
where Postgres 3-valued logic diverges from what the Python twin does with
``None``. So it seeds a small fixture and asserts the twins agree row for row.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_connection_layer_postgres.py

Never point this at prod.
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

# merchant_stores is queried through raw SQL everywhere in this repo and is not
# registered on the shared MetaData, so create_all does not know it. Only the
# columns the expression touches.
#
# ⚠️ `index_pipeline_state` is created ADDITIVELY (CREATE IF NOT EXISTS with the
# columns this file needs, then ADD COLUMN IF NOT EXISTS for each). The gate job
# runs every `tests/test_*_postgres.py` against ONE database in one pytest
# process, and `tests/test_citation_read_surfaces_postgres.py` declares the same
# table with a DIFFERENT column set. Plain `CREATE TABLE IF NOT EXISTS` makes
# whichever module runs first the winner and the other one fail on a missing
# column — measured: adding this file with a plain CREATE turned the sibling gate
# red with `column "has_price" does not exist`. Any future gate file that shares
# a lightweight table must be additive too.
_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS merchant_stores (
  store_id text, merchant_id text, platform text, domain text, status text
);
CREATE TABLE IF NOT EXISTS index_pipeline_state (content_key text);
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS serving_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS index_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_code text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_detail text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS content_quality_score double precision;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS quality_scored_at timestamp;
"""
# The last four columns are not read by anything in THIS module. They are the
# union of what the sibling gate files declare, and they are here so this file
# can never be the one that narrows a shared table: `CREATE TABLE IF NOT EXISTS`
# is a no-op once the table exists, so whichever module runs first fixes the
# shape for the whole job. Both orderings were executed before this was written —
# without these four, running this file first turns
# `test_pdp_content_depth_postgres` red.


@pytest.fixture(scope="module")
def pg_engine():
    """Build the schema the expression reads. Imports register Tables first."""
    import db.catalog  # noqa: F401  (catalog_products, catalog_offers, catalog_skus)
    import db.merchant_onboarding  # noqa: F401
    import db.pcs_tables  # noqa: F401  (pcs_merchant_capabilities)
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(statement))
    yield engine
    engine.dispose()


# (product_key, merchant_id, catalog_track, expected_layer)
#
# Every row here is shaped after something measured in prod on 2026-07-27, so a
# future reader can tell which real cohort each case is defending.
_FIXTURE = [
    # The 12,542-row layer-1 bulk: crawled, seller not in merchant_onboarding.
    ("pk_crawled_bucket", "external_seed", "external_referral", 1),
    # ADR-009 observed seller (45 of them in prod), also absent from onboarding.
    ("pk_crawled_observed", "merch_obs_example", "external_referral", 1),
    # F3: internal_merchant track, but the merchant has NO onboarding row. Must
    # be layer 1 by the explicit NOT EXISTS leg, not by COALESCE luck.
    ("pk_unknown_merchant", "merch_ghost", "internal_merchant", 1),
    # Synced with a live store, no PSP fact of any kind.
    ("pk_synced", "merch_synced", "internal_merchant", 2),
    # Synced, onboarding row exists, but psp_connected is NULL (never went
    # through the flow). NULL is not yes.
    ("pk_synced_psp_null", "merch_psp_null", "internal_merchant", 2),
    # Synced but the store is disconnected — the sync is stale by definition.
    ("pk_synced_no_store", "merch_no_store", "internal_merchant", 1),
    # Layer 3 via Pivota-side PSP.
    ("pk_psp", "merch_psp", "internal_merchant", 3),
    # Layer 3 via a verified native-payments fact instead.
    ("pk_native", "merch_native", "internal_merchant", 3),
    # A crawled ROW under a fully connected merchant stays layer 1 (ADR-001).
    ("pk_crawled_under_connected", "merch_psp", "external_referral", 1),
    # Whitespace-padded tracks. The Python twin normalises with `.strip()`; the
    # SQL needs `btrim` to match. Without these rows the equality test cannot
    # see the divergence — it was executed and real: sql=1, py=2.
    ("pk_padded_internal", "merch_synced", " internal_merchant ", 2),
    ("pk_padded_external", "merch_synced", "external_referral\t", 1),
    # `connected` is a live store status everywhere else in this repo
    # (merchant_store_service: `status IN ('active','connected')`).
    ("pk_connected_status", "merch_connected", "internal_merchant", 2),
]


@pytest.fixture(scope="module")
def seeded(pg_engine):
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        for table in (
            "catalog_products",
            "merchant_onboarding",
            "merchant_stores",
            "pcs_merchant_capabilities",
            "index_pipeline_state",
        ):
            conn.execute(text(f"DELETE FROM {table}"))

        for product_key, merchant_id, track, _expected in _FIXTURE:
            conn.execute(
                text(
                    "INSERT INTO catalog_products "
                    "(product_key, merchant_id, catalog_track, content_key, platform, "
                    " source_product_id, title) "
                    "VALUES (:pk, :mid, :track, :ck, 'external_seed', :pk, :pk)"
                ),
                {"pk": product_key, "mid": merchant_id, "track": track, "ck": "ck_" + product_key},
            )
            conn.execute(
                text(
                    "INSERT INTO index_pipeline_state "
                    "(content_key, serving_eligible, index_eligible) "
                    "VALUES (:ck, true, true)"
                ),
                {"ck": "ck_" + product_key},
            )

        for merchant_id, psp in (
            ("merch_synced", None),
            ("merch_psp_null", None),
            ("merch_no_store", True),
            ("merch_psp", True),
            ("merch_native", False),
            ("merch_connected", None),
        ):
            conn.execute(
                text(
                    "INSERT INTO merchant_onboarding "
                    "(merchant_id, business_name, contact_email, psp_connected) "
                    "VALUES (:mid, :mid, :email, :psp)"
                ),
                {"mid": merchant_id, "email": f"{merchant_id}@example.test", "psp": psp},
            )

        for merchant_id, status in (
            ("merch_synced", "active"),
            ("merch_psp_null", "active"),
            ("merch_no_store", "disconnected"),
            ("merch_psp", "active"),
            ("merch_native", "ACTIVE"),  # case-insensitivity is load-bearing
            ("merch_connected", " connected "),  # padding + the second live status
        ):
            conn.execute(
                text(
                    "INSERT INTO merchant_stores (store_id, merchant_id, platform, domain, status) "
                    "VALUES (:sid, :mid, 'shopify', 'x.myshopify.com', :st)"
                ),
                {"sid": "st_" + merchant_id, "mid": merchant_id, "st": status},
            )

        conn.execute(
            text(
                "INSERT INTO pcs_merchant_capabilities (merchant_id, has_shopify_payments) "
                "VALUES ('merch_native', true)"
            )
        )
    return pg_engine


def test_expression_prepares_inside_a_serving_shaped_statement(pg_engine):
    """The failure class is PREPARE, and it only shows in the full statement.

    This is the feed lane's real shape: the layer expression in the SELECT list
    of a statement that also joins index_pipeline_state, LEFT JOIN LATERALs a
    best-offer row, and windows over content_key.
    """
    from sqlalchemy import text

    from services.connection_layer import connection_layer_sql

    statement = f"""
        WITH ranked AS (
          SELECT
            cp.product_key,
            cp.content_key,
            {connection_layer_sql('cp')} AS connection_layer,
            ROW_NUMBER() OVER (PARTITION BY cp.content_key ORDER BY cp.product_key) AS row_rank
          FROM catalog_products cp
          INNER JOIN index_pipeline_state ips
            ON ips.content_key = cp.content_key AND ips.serving_eligible = TRUE
        )
        SELECT ranked.product_key, ranked.connection_layer, best_offer.price_amount
        FROM ranked
        LEFT JOIN LATERAL (
          SELECT COALESCE(o.merchant_effective_price, o.list_price) AS price_amount
          FROM catalog_offers o
          WHERE o.product_key = ranked.product_key
            AND o.suppressed_at IS NULL
            AND o.currency IS NOT NULL
          ORDER BY COALESCE(o.merchant_effective_price, o.list_price) ASC
          LIMIT 1
        ) best_offer ON TRUE
        WHERE ranked.row_rank = 1
        ORDER BY ranked.connection_layer DESC, ranked.product_key ASC
    """
    with pg_engine.connect() as conn:
        conn.execute(text(statement)).fetchall()


def test_joined_form_prepares(pg_engine):
    """The ``onboarding_alias`` variant is a different statement and needs its
    own execution — a caller who already joined merchant_onboarding uses it."""
    from sqlalchemy import text

    from services.connection_layer import connection_layer_sql

    statement = f"""
        SELECT cp.product_key, {connection_layer_sql('cp', onboarding_alias='mo')} AS connection_layer
        FROM catalog_products cp
        LEFT JOIN merchant_onboarding mo ON mo.merchant_id = cp.merchant_id
    """
    with pg_engine.connect() as conn:
        conn.execute(text(statement)).fetchall()


def test_expression_is_usable_in_a_where_and_group_by(pg_engine):
    """A CASE expression that only works in a SELECT list is a trap for the next
    caller. Prove the planner takes it in the filtering positions too."""
    from sqlalchemy import text

    from services.connection_layer import connection_layer_sql

    expr = connection_layer_sql("cp")
    with pg_engine.connect() as conn:
        conn.execute(
            text(
                f"SELECT {expr} AS layer, count(*) FROM catalog_products cp "
                f"WHERE {expr} >= 1 GROUP BY 1 ORDER BY 1"
            )
        ).fetchall()


def test_sql_twin_agrees_with_the_python_twin_row_for_row(seeded):
    """The reason both twins live in one module: they must not diverge.

    Postgres 3-valued logic and Python ``None`` handling are the exact place a
    hand-maintained pair drifts, so this asserts equality on a fixture that
    includes every NULL-bearing case.
    """
    from sqlalchemy import text

    from services.connection_layer import connection_layer_sql

    expected = {product_key: layer for product_key, _mid, _track, layer in _FIXTURE}

    with seeded.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT cp.product_key, {connection_layer_sql('cp')} AS layer "
                "FROM catalog_products cp ORDER BY cp.product_key"
            )
        ).fetchall()

    assert rows, "fixture produced no rows — the gate would pass having tested nothing"
    actual = {row[0]: int(row[1]) for row in rows}
    assert actual == expected


def test_both_sql_forms_agree_with_each_other(seeded):
    """The joined and correlated forms are two spellings of one policy."""
    from sqlalchemy import text

    from services.connection_layer import connection_layer_sql

    with seeded.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT cp.product_key, "
                f"{connection_layer_sql('cp')} AS correlated, "
                f"{connection_layer_sql('cp', onboarding_alias='mo')} AS joined "
                "FROM catalog_products cp "
                "LEFT JOIN merchant_onboarding mo ON mo.merchant_id = cp.merchant_id "
                "ORDER BY cp.product_key"
            )
        ).fetchall()

    assert rows
    mismatched = [(r[0], int(r[1]), int(r[2])) for r in rows if int(r[1]) != int(r[2])]
    assert not mismatched, f"correlated and joined forms disagree: {mismatched}"


def test_joined_form_requires_a_left_join(seeded):
    """An INNER JOIN does not mislabel layer-1 rows — it ELIMINATES them.

    Per the ADR-018 census that is 100% of the real serving catalog, i.e. a
    silently empty feed. Executed rather than left as a docstring warning.
    """
    from sqlalchemy import text

    from services.connection_layer import connection_layer_sql

    expr = connection_layer_sql("cp", onboarding_alias="mo")
    with seeded.connect() as conn:
        left = conn.execute(
            text(
                f"SELECT count(*) FROM (SELECT {expr} AS layer FROM catalog_products cp "
                "LEFT JOIN merchant_onboarding mo ON mo.merchant_id = cp.merchant_id) t"
            )
        ).scalar()
        inner = conn.execute(
            text(
                f"SELECT count(*) FROM (SELECT {expr} AS layer FROM catalog_products cp "
                "INNER JOIN merchant_onboarding mo ON mo.merchant_id = cp.merchant_id) t"
            )
        ).scalar()

    assert inner < left, (
        "the fixture no longer contains a merchant-less row, so this test can no "
        "longer show that an INNER JOIN drops layer-1 rows"
    )


def test_python_twin_agrees_on_the_same_fixture(seeded):
    """Close the triangle: SQL == Python, computed from the same seeded facts."""
    from sqlalchemy import text

    from services.connection_layer import LIVE_STORE_STATUSES, classify_connection_layer

    # Built from the module's own constant so this fact-extraction query cannot
    # drift from the predicate it is meant to reproduce.
    live = ", ".join(f"'{status}'" for status in LIVE_STORE_STATUSES)

    with seeded.connect() as conn:
        facts = conn.execute(
            text(
                f"""
                SELECT
                  cp.product_key,
                  cp.catalog_track,
                  (mo.merchant_id IS NOT NULL) AS merchant_known,
                  EXISTS (
                    SELECT 1 FROM merchant_stores ms
                    WHERE ms.merchant_id = cp.merchant_id
                      AND lower(btrim(COALESCE(ms.status, ''))) IN ({live})
                  ) AS has_active_store,
                  mo.psp_connected,
                  pmc.has_shopify_payments
                FROM catalog_products cp
                LEFT JOIN merchant_onboarding mo ON mo.merchant_id = cp.merchant_id
                LEFT JOIN pcs_merchant_capabilities pmc ON pmc.merchant_id = cp.merchant_id
                ORDER BY cp.product_key
                """
            )
        ).fetchall()

    assert facts
    expected = {product_key: layer for product_key, _mid, _track, layer in _FIXTURE}
    for product_key, track, known, active, psp, native in facts:
        assert (
            classify_connection_layer(
                catalog_track=track,
                merchant_known=bool(known),
                has_active_store=bool(active),
                psp_connected=psp,
                has_native_payments=native,
            )
            == expected[product_key]
        ), product_key
