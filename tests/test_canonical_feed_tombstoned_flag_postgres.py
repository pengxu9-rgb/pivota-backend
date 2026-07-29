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

This is a Postgres gate because the feed's SELECT is Postgres-only (correlated
EXISTS renderability + the LATERAL joins), and a flag that is silently always
false would be indistinguishable from "no tombstones exist" — which is exactly
the reading that let 187 of them stay advertised. So the tests below assert the
flag answers BOTH ways on real rows.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)


def _engine():
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1))


def test_tombstoned_column_compiles_and_selects_on_postgres():
    """PREPARE-time gate: the expression must compile into a real SELECT."""
    from routes.pivota_canonical_routes import _tombstoned_column
    from sqlalchemy import select

    from db.catalog import catalog_products

    stmt = select(_tombstoned_column()).select_from(catalog_products).limit(1)
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS catalog_products ("
                " product_key text, suppression_reason text, suppressed_at timestamp)"
            )
        )
        conn.execute(text("DELETE FROM catalog_products"))
        conn.execute(stmt).fetchall()
    eng.dispose()


def test_flag_answers_both_ways():
    """A flag that is always false reads exactly like 'no tombstones exist'.

    That misreading is what kept 187 retired rows advertised, so both directions
    are asserted on real rows rather than only the positive case.
    """
    from routes.pivota_canonical_routes import _tombstoned_column
    from sqlalchemy import select

    from db.catalog import catalog_products

    eng = _engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS catalog_products ("
                " product_key text, suppression_reason text, suppressed_at timestamp)"
            )
        )
        conn.execute(text("DELETE FROM catalog_products"))
        conn.execute(
            text(
                "INSERT INTO catalog_products (product_key, suppression_reason, suppressed_at)"
                " VALUES ('clean', NULL, NULL),"
                # The load-bearing shape: reason set, suppressed_at NULL. This is
                # the row that passes every suppressed_at IS NULL filter.
                "        ('retired', 'wrong_brand_namesake_wave3_20260718', NULL),"
                "        ('dedupe',  'cross_merchant_redundant_external_seed', NULL)"
            )
        )
        rows = conn.execute(
            select(catalog_products.c.product_key, _tombstoned_column())
            .select_from(catalog_products)
            .order_by(catalog_products.c.product_key)
        ).fetchall()
    eng.dispose()

    got = {r[0]: r[1] for r in rows}
    assert got["clean"] is False, "a live row must not be flagged"
    assert got["dedupe"] is True
    assert got["retired"] is True, (
        "a row with suppression_reason set and suppressed_at NULL is the exact "
        "state that slips past every suppressed_at IS NULL filter — it must flag"
    )


def test_suppressed_at_is_not_what_this_measures():
    """Guards the misreading that `suppressed_at IS NULL` already covers this.

    It does not, and believing it did is the whole defect: the two columns encode
    different decisions and only `suppression_reason` marks the retire-but-keep-
    serving state.
    """
    from routes.pivota_canonical_routes import _tombstoned_column
    from sqlalchemy import select

    from db.catalog import catalog_products

    eng = _engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS catalog_products ("
                " product_key text, suppression_reason text, suppressed_at timestamp)"
            )
        )
        conn.execute(text("DELETE FROM catalog_products"))
        conn.execute(
            text(
                "INSERT INTO catalog_products (product_key, suppression_reason, suppressed_at)"
                " VALUES ('withdrawn', NULL, NOW())"
            )
        )
        row = conn.execute(
            select(_tombstoned_column()).select_from(catalog_products)
        ).one()
    eng.dispose()
    assert row[0] is False, (
        "suppressed_at alone is a different state (fully withdrawn) and is "
        "already handled by sitemap_candidate_filter; this flag is only about "
        "the retire-but-still-serving rows it cannot see"
    )
