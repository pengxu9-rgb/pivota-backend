"""The canonical feed must state when a row has been RETIRED but still serves.

`suppression_reason` set WITHOUT `suppressed_at` is a real, populated state —
step-5 dedupe, cross-merchant redundancy, brand-namesake retirement and the d2_*
identity resolutions all mark rows this way and leave them serving. So tombstoned
rows pass every `suppressed_at IS NULL` filter in the system, including
`sitemap_candidate_filter`, and get advertised as though nothing had been decided
about them.

Measured on the live 7,509-URL sitemap 2026-07-29 — 187 advertised URLs point at
a tombstoned row: 135 `wrong_brand_namesake_wave3_20260718`, 50
`cross_merchant_redundant_external_seed`, 2 `step5_campaign_clone_dup`. The first
group is why this matters rather than being tidy: those rows were retired for
carrying the WRONG BRAND, so serving them publishes a PDP with incorrect brand
attribution.

A flag that is silently always false is indistinguishable from "no tombstones
exist" — exactly the reading that let 187 of them stay advertised — so the tests
below assert it answers BOTH ways on real rows.

🚨 THIS GATE SHARES ONE DATABASE WITH THE OTHER `test_*_postgres.py` FILES.
An earlier cut of this file did `CREATE TABLE IF NOT EXISTS catalog_products
(product_key, suppression_reason, suppressed_at)` and thereby created a
three-column stub under a real table's name, which broke every sibling gate that
inserts `merchant_id`. Use `metadata.create_all` and DELETE rows — never
hand-roll DDL for a table `db.catalog` already owns.
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401  (registers catalog_products on the shared MetaData)
    from sqlalchemy import create_engine

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    yield engine
    engine.dispose()


def _insert(conn, *, pk, reason, suppressed_at_sql="NULL"):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO catalog_products "
            "(product_key, merchant_id, platform, source_product_id, title, "
            " content_key, catalog_track, suppression_reason, suppressed_at) "
            f"VALUES (:pk, 'external_seed', 'external_seed', :pk, :pk, :ck, "
            f"        'external_referral', :reason, {suppressed_at_sql})"
        ),
        {"pk": pk, "ck": f"ck_{pk}", "reason": reason},
    )


def test_tombstoned_column_compiles_and_selects_on_postgres(pg_engine):
    """PREPARE-time gate: the expression must compile into a real SELECT."""
    from sqlalchemy import select, text

    from db.catalog import catalog_products
    from routes.pivota_canonical_routes import _tombstoned_column

    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_products"))
        conn.execute(
            select(_tombstoned_column()).select_from(catalog_products).limit(1)
        ).fetchall()


def test_flag_answers_both_ways(pg_engine):
    """A flag that is always false reads exactly like 'no tombstones exist'.

    That misreading is what kept 187 retired rows advertised, so both directions
    are asserted on real rows rather than only the positive case.
    """
    from sqlalchemy import select, text

    from db.catalog import catalog_products
    from routes.pivota_canonical_routes import _tombstoned_column

    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_products"))
        _insert(conn, pk="clean", reason=None)
        # The load-bearing shape: reason set, suppressed_at NULL — the state that
        # passes every suppressed_at IS NULL filter.
        _insert(conn, pk="retired", reason="wrong_brand_namesake_wave3_20260718")
        _insert(conn, pk="dedupe", reason="cross_merchant_redundant_external_seed")
        rows = conn.execute(
            select(catalog_products.c.product_key, _tombstoned_column())
            .select_from(catalog_products)
            .order_by(catalog_products.c.product_key)
        ).fetchall()
        conn.execute(text("DELETE FROM catalog_products"))

    got = {r[0]: r[1] for r in rows}
    assert got["clean"] is False, "a live row must not be flagged"
    assert got["dedupe"] is True
    assert got["retired"] is True, (
        "a row with suppression_reason set and suppressed_at NULL is the exact "
        "state that slips past every suppressed_at IS NULL filter — it must flag"
    )


def test_suppressed_at_alone_is_not_what_this_measures(pg_engine):
    """Guards the misreading that `suppressed_at IS NULL` already covers this.

    It does not, and believing it did is the whole defect: the two columns encode
    different decisions, and only `suppression_reason` marks the
    retired-but-still-serving state.
    """
    from sqlalchemy import select, text

    from db.catalog import catalog_products
    from routes.pivota_canonical_routes import _tombstoned_column

    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_products"))
        _insert(conn, pk="withdrawn", reason=None, suppressed_at_sql="NOW()")
        row = conn.execute(
            select(_tombstoned_column()).select_from(catalog_products)
        ).one()
        conn.execute(text("DELETE FROM catalog_products"))

    assert row[0] is False, (
        "suppressed_at alone is a DIFFERENT state (fully withdrawn) already "
        "handled by sitemap_candidate_filter; this flag is only about the "
        "retired-but-still-serving rows that filter cannot see"
    )
