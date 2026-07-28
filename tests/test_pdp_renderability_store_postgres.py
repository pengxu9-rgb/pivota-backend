"""Production-dialect gate for the persisted row-grain renderability column.

TWO THINGS ONLY REAL POSTGRES CAN SETTLE HERE.

1. **PREPARE.** ``pdp_will_render_expression`` is a large composite with several
   correlated EXISTS legs; the compute SELECT wraps it and the UPDATE embeds it.
   This repo has shipped statements Postgres refused to prepare while a green
   SQLite suite passed them (#1588, #1593) — so the statements are EXECUTED here
   rather than compile-asserted.

2. **PARITY — the one that matters.** The column's entire justification is that
   there is ONE implementation of renderability. A persisted value that drifts
   from the live expression is the fourth twin under a different name, and the
   drift would be invisible because both sides look authoritative. So this
   asserts, row for row on real Postgres, that the persisted column equals the
   live expression — including on a fixture where the answer is genuinely mixed,
   because a column that is false for everything looks identical to a working
   one.

Run:

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_pdp_renderability_store_postgres.py

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

# Shared lightweight tables are created ADDITIVELY (CREATE IF NOT EXISTS with a
# minimal column set, then ADD COLUMN IF NOT EXISTS). The gate job runs every
# tests/test_*_postgres.py against ONE database in ONE pytest process, and the
# sibling gate files declare some of these with DIFFERENT column sets — a plain
# CREATE makes whichever module runs first the winner and the others fail on a
# missing column. Measured on this repo's own gate. The last four
# index_pipeline_state columns are the union the siblings need, so this file can
# never be the one that narrows a shared table.
_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS external_product_seeds (
  external_product_id text, attached_product_key text, status text,
  merchant_id text, source text, product_key text, source_product_id text
);
CREATE TABLE IF NOT EXISTS index_pipeline_state (content_key text);
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS serving_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS index_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_code text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_detail text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS content_quality_score double precision;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS quality_scored_at timestamp;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS pdp_will_render boolean;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS pdp_will_render_computed_at timestamptz;
"""


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401  (registers catalog_products on the shared MetaData)
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(statement))
    yield engine
    engine.dispose()


def _insert_product(conn, *, pk, ck, sig, source_system, source_product_id, merchant="external_seed"):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO catalog_products "
            "(product_key, merchant_id, platform, source_product_id, title, "
            " content_key, pivota_signature_id, source_system, catalog_track) "
            "VALUES (:pk, :m, 'external_seed', :spid, :pk, :ck, :sig, :ss, 'external_referral')"
        ),
        {"pk": pk, "m": merchant, "spid": source_product_id, "ck": ck, "sig": sig, "ss": source_system},
    )


@pytest.fixture(scope="module")
def seeded(pg_engine):
    """A fixture whose renderability answer is genuinely MIXED.

    Deliberately not all-true or all-false: a column that is false for
    everything is indistinguishable from a working one, so parity over a
    single-valued fixture proves nothing.

    Two rows share content_key `ck_split` and DISAGREE — one seed-routed and
    serving-eligible (renders), one with no seed at all (dead). That is the
    279-content_key case from prod, in miniature, and it is the case a
    content_key-grain column could not represent.
    """
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        for t in ("catalog_products", "external_product_seeds", "index_pipeline_state"):
            conn.execute(text(f"DELETE FROM {t}"))

        # serving-eligible key, two rows, only one of which has a content route
        conn.execute(text("INSERT INTO index_pipeline_state (content_key, serving_eligible) VALUES ('ck_split', true)"))
        _insert_product(conn, pk="pk_live", ck="ck_split", sig="sig_live",
                        source_system="external_product_seeds_mirror_v1", source_product_id="ext_live")
        conn.execute(text(
            "INSERT INTO external_product_seeds (external_product_id, status, source_product_id) "
            "VALUES ('ext_live', 'active', 'ext_live')"
        ))
        _insert_product(conn, pk="pk_dead", ck="ck_split", sig="sig_dead",
                        source_system="external_product_seeds_mirror_v1", source_product_id="ext_missing")

        # NOT serving-eligible: route resolves, serving gate refuses → dead
        conn.execute(text("INSERT INTO index_pipeline_state (content_key, serving_eligible) VALUES ('ck_blocked', false)"))
        _insert_product(conn, pk="pk_blocked", ck="ck_blocked", sig="sig_blocked",
                        source_system="external_product_seeds_mirror_v1", source_product_id="ext_blocked")
        conn.execute(text(
            "INSERT INTO external_product_seeds (external_product_id, status, source_product_id) "
            "VALUES ('ext_blocked', 'active', 'ext_blocked')"
        ))

        # no index_pipeline_state row at all → serving gate fails closed
        _insert_product(conn, pk="pk_noips", ck="ck_noips", sig="sig_noips",
                        source_system="external_product_seeds_mirror_v1", source_product_id="ext_noips")
    return pg_engine


def test_compute_select_prepares_and_executes(pg_engine):
    """The composite is large and correlated; PREPARE is the failure class."""
    from services.pdp_renderability_store import _compute_select

    with pg_engine.connect() as conn:
        conn.execute(_compute_select()).fetchall()
        conn.execute(_compute_select(content_keys=["ck_split"])).fetchall()
        conn.execute(_compute_select(product_keys=["pk_live"])).fetchall()


def test_fixture_is_genuinely_mixed(seeded):
    """Guard the guard: parity over an all-false fixture proves nothing."""
    from services.pdp_renderability_store import _compute_select

    with seeded.connect() as conn:
        values = {r._mapping["product_key"]: r._mapping["will_render"]
                  for r in conn.execute(_compute_select()).fetchall()}
    assert any(values.values()), f"fixture is all-false — parity would be vacuous: {values}"
    assert not all(values.values()), f"fixture is all-true — parity would be vacuous: {values}"


def test_row_grain_is_real_two_sigs_one_content_key_disagree(seeded):
    """The finding that moved this column off index_pipeline_state.

    Same content_key, one sig renders and its sibling does not. No single
    per-content_key value is correct for such a key — which is why the column
    lives on catalog_products (PK product_key) and not on index_pipeline_state
    (PK content_key).
    """
    from services.pdp_renderability_store import _compute_select

    with seeded.connect() as conn:
        rows = {r._mapping["product_key"]: r._mapping["will_render"]
                for r in conn.execute(_compute_select(content_keys=["ck_split"])).fetchall()}
    assert rows["pk_live"] is True
    assert rows["pk_dead"] is False


def test_serving_gate_and_missing_ips_both_fail_closed(seeded):
    from services.pdp_renderability_store import _compute_select

    with seeded.connect() as conn:
        rows = {r._mapping["product_key"]: r._mapping["will_render"]
                for r in conn.execute(_compute_select()).fetchall()}
    assert rows["pk_blocked"] is False, "serving_eligible=false must not render"
    assert rows["pk_noips"] is False, "no index_pipeline_state row must fail CLOSED"


def test_persisted_column_equals_the_live_expression_row_for_row(seeded):
    """PARITY — the assertion this column's existence depends on.

    If the persisted value ever diverges from the live expression, the column
    has become a fourth twin under a different name and the drift is invisible
    because both sides look authoritative.
    """
    from sqlalchemy import text

    from services.pdp_renderability_store import COLUMN_WILL_RENDER, _compute_select

    with seeded.begin() as conn:
        for row in conn.execute(_compute_select()).fetchall():
            m = row._mapping
            conn.execute(
                text(f"UPDATE catalog_products SET {COLUMN_WILL_RENDER} = :wr, "
                     f"pdp_will_render_computed_at = NOW() WHERE product_key = :pk"),
                {"wr": bool(m["will_render"]), "pk": m["product_key"]},
            )

    with seeded.connect() as conn:
        live = {r._mapping["product_key"]: bool(r._mapping["will_render"])
                for r in conn.execute(_compute_select()).fetchall()}
        stored = {r[0]: r[1] for r in conn.execute(
            text(f"SELECT product_key, {COLUMN_WILL_RENDER} FROM catalog_products")
        ).fetchall()}

    assert stored == live, f"persisted column drifted from its source: {stored} != {live}"


def test_the_read_predicate_executes_and_is_fail_closed(seeded):
    """NULL (never computed) must NOT be advertisable."""
    from sqlalchemy import text

    from services.pdp_renderability_store import (
        COLUMN_WILL_RENDER,
        persisted_will_render_predicate,
    )

    with seeded.begin() as conn:
        conn.execute(text(f"UPDATE catalog_products SET {COLUMN_WILL_RENDER} = NULL"))

    predicate = str(persisted_will_render_predicate("cp"))
    with seeded.connect() as conn:
        n = conn.execute(
            text(f"SELECT count(*) FROM catalog_products cp WHERE {predicate}")
        ).scalar()
    assert n == 0, "an uncomputed (NULL) row must never be advertisable"
