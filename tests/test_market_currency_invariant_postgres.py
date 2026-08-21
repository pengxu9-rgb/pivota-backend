"""ADR-024 Phase 0 item 2 invariant, executed on the production dialect.

WHY THIS FILE EXISTS SEPARATELY FROM tests/test_market_currency_invariant.py.
That suite runs on SQLite and proves the scope rules; this one proves Postgres
will PARSE and PLAN the same statements. The two are not redundant — the #1588
class (a statement Postgres refuses to prepare) is invisible to SQLite, and
this check's SQL carries three things worth executing for real:

  * a CTE that both the pair query and the row query build on;
  * `upper(trim(coalesce(...)))` over a `VARCHAR(8)` market and a
    `VARCHAR(16)` currency — Postgres must infer a type for each;
  * BOUND pair parameters in an OR-chain, which is exactly the shape that
    produced `IndeterminateDatatypeError` in #1588. A currency code read out
    of `catalog_offers` is bound, never interpolated, so those bind types have
    to resolve against a real schema.

The repo-wide static sweep (tests/test_repo_sql_prepare_postgres.py) does NOT
cover them: it resolves a module-level constant only when it is a plain string
literal, and both statements here are built — one by concatenation with the
shared served-offer conjuncts, one by a function. Said plainly rather than left
to be assumed covered.

🚨 THESE GATE FILES SHARE ONE DATABASE. `metadata.create_all` + DELETE only —
never hand-roll DDL for a table `db.catalog` owns. The synchronous engine (not
the async `databases` singleton) is this directory's convention for the same
reason tests/test_priced_offer_gate_postgres.py uses it: `databases` 0.7.0
binds its pool to the loop that connected it, and a per-test loop then trips
`another operation is in progress` on teardown.
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

MERCHANT = "merch_mkt_cur_pg"


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine

    from db.catalog import catalog_offers, catalog_products
    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, tables=[catalog_products, catalog_offers], checkfirst=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def conn(pg_engine):
    """One transaction per test, ROLLED BACK. Nothing this file writes survives
    it, which is what makes sharing the gate database safe."""
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _insert(conn, offer_id, market, currency, **cols):
    from sqlalchemy import text

    values = {
        "oid": offer_id,
        "market": market,
        "cur": currency,
        "lp": cols.get("list_price", 10),
        "mep": cols.get("merchant_effective_price"),
    }
    suppressed = "NOW()" if cols.get("suppressed") else "NULL"
    conn.execute(
        text(
            "INSERT INTO catalog_offers "
            "(offer_id, sku_key, product_key, merchant_id, market, currency, "
            " list_price, merchant_effective_price, suppressed_at) "
            f"VALUES (:oid, :oid, 'pk_mkt_cur_pg', '{MERCHANT}', :market, :cur, "
            f"        :lp, :mep, {suppressed})"
        ),
        values,
    )


def _pairs(conn):
    from sqlalchemy import text

    from services.catalog_invariant_checks import _MARKET_CURRENCY_PAIRS_SQL

    rows = conn.execute(text(_MARKET_CURRENCY_PAIRS_SQL)).mappings().all()
    return {(r["market_norm"], r["currency_norm"]): int(r["n"]) for r in rows}


def test_the_pair_query_plans_and_runs_on_postgres(conn):
    # Parse + Plan is the claim; an empty corpus is a pass.
    assert isinstance(_pairs(conn), dict)


def test_served_scope_and_normalization_on_the_real_dialect(conn):
    before = _pairs(conn)
    _insert(conn, "pg_ok", "US", "EUR")
    _insert(conn, "pg_suppressed", "US", "EUR", suppressed=True)
    _insert(conn, "pg_no_price", "US", "EUR", list_price=None)
    _insert(conn, "pg_zero", "US", "EUR", list_price=0)
    _insert(conn, "pg_eff", "US", "EUR", list_price=None, merchant_effective_price=7)
    # Padded + lower-cased: normalised by the SERVING predicate's own spelling,
    # so it joins the same group rather than minting a second one.
    _insert(conn, "pg_padded", " us ", " eur ")

    after = _pairs(conn)
    delta = {k: after[k] - before.get(k, 0) for k in after if after[k] - before.get(k, 0)}
    assert delta == {("US", "EUR"): 3}, (
        "served supply is the shared priced-offer predicate: the suppressed, "
        "price-less and zero-priced rows are out of scope; the "
        "effective-price-only and the padded lower-case rows are in it"
    )


def test_the_classifier_reads_the_real_rows(conn):
    from services.catalog_invariant_checks import classify_market_currency_pairs

    before = _pairs(conn)
    _insert(conn, "pg_agree", "US", "USD")
    _insert(conn, "pg_disagree", "US", "EUR")
    _insert(conn, "pg_unmapped", "DE", "EUR")
    _insert(conn, "pg_blank", "US", "")
    after = _pairs(conn)
    delta = {k: after[k] - before.get(k, 0) for k in after if after[k] - before.get(k, 0)}

    buckets = classify_market_currency_pairs(
        [{"market_norm": m, "currency_norm": c, "n": n} for (m, c), n in delta.items()]
    )
    assert buckets["agreeing"] == 1
    assert buckets["disagreeing"] == 1
    assert buckets["unmapped_market"] == 1
    assert buckets["blank_currency"] == 1


def test_the_row_query_binds_its_pairs_on_postgres(conn):
    from sqlalchemy import text

    from services.catalog_invariant_checks import _market_currency_rows_sql

    _insert(conn, "pg_usd", "US", "USD")
    _insert(conn, "pg_eur", "US", "EUR")
    _insert(conn, "pg_gbp", "GB", "USD")

    rows = conn.execute(
        text(_market_currency_rows_sql(2)),
        {"m0": "US", "c0": "EUR", "m1": "GB", "c1": "USD"},
    ).mappings().all()
    ids = {r["offer_id"] for r in rows}
    assert "pg_eur" in ids and "pg_gbp" in ids
    assert "pg_usd" not in ids
    # Every identity column the quarantine matcher reads must come back, or
    # `is_quarantined_row` silently sees None for all of them and exempts
    # nothing while looking like it checked.
    row = next(r for r in rows if r["offer_id"] == "pg_eur")
    for column in (
        "merchant_id", "source_system", "source_ref", "source_domain",
        "domain", "platform", "market_norm", "currency_norm",
    ):
        assert column in row, f"{column} missing from the row query"


def test_a_single_pair_row_query_also_plans(conn):
    # One bound pair emits a different predicate (no `OR` at all); plan it too.
    from sqlalchemy import text

    from services.catalog_invariant_checks import _market_currency_rows_sql

    rows = conn.execute(
        text(_market_currency_rows_sql(1)), {"m0": "US", "c0": "JPY"}
    ).mappings().all()
    assert list(rows) == []
