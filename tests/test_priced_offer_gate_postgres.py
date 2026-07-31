"""The per-row priced-offer predicate, executed on the production dialect.

WHAT WENT WRONG, because the shape of the bug is the reason this file exists.

`index_pipeline_state.has_price` was never broken. It asked exactly the right
question — "does this catalog_products row have an unsuppressed offer with a
price?" — of exactly the right table. What broke is GRAIN:

  * `index_pipeline_state` is keyed by CONTENT_KEY (migration 098). Its writer
    classifies every catalog_products row under that key and stores the BEST
    one's state (`_select_content_key_state`).
  * `catalog_row_trust` is keyed by PRODUCT_KEY, and every product_key mints its
    own `pivota_signature_id` — its own public PDP at
    agent.pivota.cc/products/{sig}.
  * Both trust upserters join `ips.content_key = cp.content_key`.

So a price-less row sharing a content_key with a priced sibling read the
sibling's `serving_eligible=true` and went public with no price. Measured on
prod 2026-07-31: 4 Tom Ford fragrance PDPs, each with exactly one unsuppressed
offer whose list_price, merchant_effective_price AND estimated_best_price were
all NULL, behind a priced tomfordbeauty.com sibling. The origin was the
2026-07-30 currency remediation, which NULLed prices on placeholder offers
without suppressing them — an honest NULL, and a state that recurs.

POSTGRES GATE because every predicate here is correlated-EXISTS SQL embedded as
a literal string in three modules, and because `_PRODUCT_JOIN_SELECT` is a
CTE-bearing DISTINCT ON query SQLite cannot parse at all. A SQLite-only suite
would report green while the column that carries the fix failed to compile.

Each predicate is proved to count BOTH ways. A check that only ever answers
"clean" looks exactly like a healthy catalog.

🚨 THESE GATE FILES SHARE ONE DATABASE. `metadata.create_all` + DELETE only —
never hand-roll DDL for a table `db.catalog` owns.
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

# Tables the trust join reaches that db.catalog does not own. Column lists are
# the minimum the query names, not the real schema.
_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS external_product_seeds (id text);
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS external_product_id text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS attached_product_key text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS status text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_kind text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS merchant_id text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS product_key text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS source_product_id text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS domain text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS title text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_data jsonb;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS created_at timestamptz;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS updated_at timestamptz;
CREATE TABLE IF NOT EXISTS index_pipeline_state (content_key text PRIMARY KEY);
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS serving_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS index_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS pipeline_stage text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_code text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_detail text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS content_quality_score double precision;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS quality_scored_at timestamptz;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS last_extracted_at timestamptz;
CREATE TABLE IF NOT EXISTS catalog_row_trust (
  subject_type text, subject_key text, serving_decision text,
  serving_reason_codes text[]
);
-- Additive + order-proof, per #1651: these gate modules SHARE ONE DATABASE and
-- `CREATE TABLE IF NOT EXISTS` no-ops once any sibling has created the table, so
-- a narrow declaration that happens to sort first starves every other module.
-- Create with the PK only, then widen. `canonical_sig_id` is deliberately NOT
-- NULL-constrained here. Prod (mig 181) declares it NOT NULL, but ADD COLUMN on
-- an already-populated shared table cannot, and the tests never insert a NULL.
CREATE TABLE IF NOT EXISTS content_canonical_election (content_key text PRIMARY KEY);
ALTER TABLE content_canonical_election ADD COLUMN IF NOT EXISTS canonical_sig_id text;
ALTER TABLE content_canonical_election ADD COLUMN IF NOT EXISTS basis text;
ALTER TABLE content_canonical_election ADD COLUMN IF NOT EXISTS elected_at timestamptz;
CREATE TABLE IF NOT EXISTS pdp_identity_listing (
  source_listing_ref text, product_id text, merchant_id text, identity_status text,
  identity_confidence numeric, live_read_enabled boolean, review_required boolean,
  sellable_item_group_id text, product_line_id text, review_family_id text
);
CREATE TABLE IF NOT EXISTS pdp_identity_override (
  id text, source_listing_ref text, action_type text, active boolean,
  updated_at timestamptz, created_at timestamptz
);
-- merchant_stores must carry every column named by
-- services.source_quarantine.STORE_PICK_ORDER, which the trust join inlines
-- into a DISTINCT ON. Add a leg there and this DDL is what tells you, loudly.
CREATE TABLE IF NOT EXISTS merchant_stores (
  merchant_id text, platform text, domain text, status text, is_primary boolean,
  store_id text, last_sync timestamptz, updated_at timestamptz,
  created_at timestamptz, id text
);
"""

_TABLES_TO_RESET = (
    "content_canonical_election",
    "catalog_row_trust",
    "catalog_offers",
    "catalog_products",
    "index_pipeline_state",
    "external_product_seeds",
    "pdp_identity_listing",
    "pdp_identity_override",
    "merchant_stores",
)


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for stmt in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(stmt))
    yield engine
    engine.dispose()


def _reset(conn):
    from sqlalchemy import text

    for t in _TABLES_TO_RESET:
        conn.execute(text(f"DELETE FROM {t}"))


def _product(conn, *, pk, ck, sig=None, sync_status="live", merchant_id="external_seed"):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO index_pipeline_state (content_key, serving_eligible) "
            "VALUES (:ck, TRUE) ON CONFLICT (content_key) DO NOTHING"
        ),
        {"ck": ck},
    )
    conn.execute(
        text(
            "INSERT INTO catalog_products "
            "(product_key, merchant_id, platform, source_product_id, title, "
            " content_key, catalog_track, sync_status, pivota_signature_id) "
            "VALUES (:pk, :mid, 'external_seed', :pk, :pk, :ck, "
            "        'external_referral', :ss, :sig)"
        ),
        {"pk": pk, "mid": merchant_id, "ck": ck, "ss": sync_status,
         "sig": sig or f"sig_{pk}"},
    )


def _offer(conn, *, pk, list_price=None, effective=None, suppressed=False,
           currency="USD"):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO catalog_offers "
            "(offer_id, sku_key, product_key, merchant_id, list_price, "
            " merchant_effective_price, currency, suppressed_at) "
            "VALUES (:oid, :oid, :pk, 'external_seed', :lp, :mep, :cur, "
            f"        {'NOW()' if suppressed else 'NULL'})"
        ),
        {"oid": f"offer:{pk}:{list_price}:{effective}:{suppressed}", "pk": pk,
         "lp": list_price, "mep": effective, "cur": currency},
    )


def _trust_blocked(conn, pk):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO catalog_row_trust (subject_type, subject_key, serving_decision) "
            "VALUES ('product', :pk, 'blocked')"
        ),
        {"pk": pk},
    )


def _trust_public(conn, pk):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO catalog_row_trust (subject_type, subject_key, serving_decision) "
            "VALUES ('product', :pk, 'public')"
        ),
        {"pk": pk},
    )


# ---------------------------------------------------------------------------
# The shared predicate itself
# ---------------------------------------------------------------------------


def _exists_count(conn, product_key):
    """Run priced_offer_exists_sql standalone and return its boolean."""
    from sqlalchemy import text

    from services.priced_offer_sql import priced_offer_exists_sql

    sql = f"SELECT {priced_offer_exists_sql(':pk')} AS priced"
    return conn.execute(text(sql), {"pk": product_key}).scalar()


def test_predicate_compiles_and_answers_true_for_a_priced_offer(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_priced", ck="ck_a")
        _offer(conn, pk="pk_priced", list_price=75.00)
        assert _exists_count(conn, "pk_priced") is True


def test_predicate_is_false_when_every_price_column_is_null(pg_engine):
    """The exact prod shape: an offer row exists, unsuppressed, carrying nothing.

    EXISTENCE is not price. This is the state the 2026-07-30 remediation left on
    432 unsuppressed prod offers.
    """
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_null", ck="ck_b")
        _offer(conn, pk="pk_null", list_price=None, effective=None)
        assert _exists_count(conn, "pk_null") is False


def test_predicate_is_false_for_a_zero_price(pg_engine):
    """0.00 is not buyable. `> 0`, never `IS NOT NULL`."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_zero", ck="ck_c")
        _offer(conn, pk="pk_zero", list_price=0)
        assert _exists_count(conn, "pk_zero") is False


def test_predicate_is_false_when_the_only_priced_offer_is_suppressed(pg_engine):
    """Suppressed supply is withdrawn supply.

    The invariant used to omit this conjunct while `has_price` required it. They
    agreed on prod by luck; this pins the agreement.
    """
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_supp", ck="ck_d")
        _offer(conn, pk="pk_supp", list_price=75.00, suppressed=True)
        assert _exists_count(conn, "pk_supp") is False


def test_predicate_accepts_a_merchant_effective_price_with_no_list_price(pg_engine):
    """The coalesce leg. 0 prod rows exercise it today — it is here for the day
    a writer populates only the effective price, which is exactly how the
    2026-07-30 price rewrite arrived."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_mep", ck="ck_e")
        _offer(conn, pk="pk_mep", list_price=None, effective=42.00)
        assert _exists_count(conn, "pk_mep") is True


def test_predicate_ignores_estimated_best_price(pg_engine):
    """estimated_best_price is OUR estimate, not a merchant quote. A PDP must
    not be published on the strength of a guess."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_ebp", ck="ck_f")
        _offer(conn, pk="pk_ebp", list_price=None, effective=None)
        conn.execute(
            text("UPDATE catalog_offers SET estimated_best_price = 99.00 "
                 "WHERE product_key = 'pk_ebp'"))
        assert _exists_count(conn, "pk_ebp") is False


def test_predicate_is_false_when_the_row_has_no_offer_at_all(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_bare", ck="ck_g")
        assert _exists_count(conn, "pk_bare") is False


# ---------------------------------------------------------------------------
# public_without_priced_offer — the invariant, on the real dialect
# ---------------------------------------------------------------------------


def _invariant():
    from services.catalog_invariant_checks import _CHECKS

    for c in _CHECKS:
        if c["name"] == "public_without_priced_offer":
            return c
    raise AssertionError("public_without_priced_offer is not registered")


def _count(conn):
    from sqlalchemy import text

    return conn.execute(text(_invariant()["count_sql"])).scalar()


def test_invariant_counts_a_public_row_with_a_priceless_offer(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_v", ck="ck_v")
        _offer(conn, pk="pk_v", list_price=None, effective=None)
        _trust_public(conn, "pk_v")
        assert _count(conn) == 1


def test_invariant_does_not_count_a_properly_priced_public_row(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_ok", ck="ck_ok")
        _offer(conn, pk="pk_ok", list_price=75.00)
        _trust_public(conn, "pk_ok")
        assert _count(conn) == 0


def test_invariant_sample_sql_returns_the_offending_key(pg_engine):
    """A count with no samples is an alert nobody can action."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_sample", ck="ck_sample")
        _offer(conn, pk="pk_sample", list_price=None)
        _trust_public(conn, "pk_sample")
        rows = conn.execute(text(_invariant()["sample_sql"])).fetchall()
        assert [r[0] for r in rows] == ["pk_sample"]


def test_invariant_is_per_product_key_not_per_content_key(pg_engine):
    """THE REGRESSION TEST. The prod shape, reduced.

    A priced sibling under the SAME content_key must not launder the price-less
    row. If this ever passes with count 0, the invariant has been coarsened to
    content grain and the 4 Tom Ford PDPs are back.
    """
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_sibling_priced", ck="ck_shared")
        _offer(conn, pk="pk_sibling_priced", list_price=75.00)
        _trust_public(conn, "pk_sibling_priced")

        _product(conn, pk="pk_sibling_bare", ck="ck_shared")
        _offer(conn, pk="pk_sibling_bare", list_price=None, effective=None)
        _trust_public(conn, "pk_sibling_bare")

        assert _count(conn) == 1
        rows = conn.execute(
            __import__("sqlalchemy").text(_invariant()["sample_sql"])).fetchall()
        assert [r[0] for r in rows] == ["pk_sibling_bare"]


# ---------------------------------------------------------------------------
# The trust join — the column that carries the fix
# ---------------------------------------------------------------------------


def _joined(conn, product_key):
    from sqlalchemy import text

    from services.catalog_row_trust_upserter import _SELECT_BY_PRODUCT_KEY_SQL

    row = conn.execute(
        text(_SELECT_BY_PRODUCT_KEY_SQL), {"product_key": product_key}
    ).mappings().first()
    assert row is not None, f"join returned no row for {product_key}"
    return dict(row)


def test_trust_join_compiles_on_postgres_and_reports_row_has_priced_offer(pg_engine):
    """The whole CTE-bearing DISTINCT ON join must still compile.

    SQLite cannot parse this query at all, so nothing but a PG run proves the
    new column did not break it.
    """
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_join_ok", ck="ck_join_ok")
        _offer(conn, pk="pk_join_ok", list_price=55.00)
        assert _joined(conn, "pk_join_ok")["row_has_priced_offer"] is True


def test_trust_join_reports_false_for_a_priceless_row(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_join_bare", ck="ck_join_bare")
        _offer(conn, pk="pk_join_bare", list_price=None, effective=None)
        assert _joined(conn, "pk_join_bare")["row_has_priced_offer"] is False


def test_trust_join_answers_per_row_under_a_shared_content_key(pg_engine):
    """The fix, at the point where it matters.

    Both rows share a content_key and therefore share `serving_eligible` — the
    join's own ips column proves it — yet the price answer must differ per row.
    """
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_grain_priced", ck="ck_grain")
        _offer(conn, pk="pk_grain_priced", list_price=90.00)
        _product(conn, pk="pk_grain_bare", ck="ck_grain")
        _offer(conn, pk="pk_grain_bare", list_price=None, effective=None)

        priced = _joined(conn, "pk_grain_priced")
        bare = _joined(conn, "pk_grain_bare")

        assert priced["serving_eligible"] is True
        assert bare["serving_eligible"] is True  # the coarse signal agrees…
        assert priced["row_has_priced_offer"] is True
        assert bare["row_has_priced_offer"] is False  # …the per-row one does not


def test_trust_join_never_returns_null_for_the_price_column(pg_engine):
    """EXISTS, not (SELECT TRUE … LIMIT 1).

    The policy gate is tri-state and reads None as "the caller did not compute
    it". If this column could be NULL, a price-less row would silently skip the
    gate instead of being blocked by it.
    """
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_null_probe", ck="ck_null_probe")
        assert _joined(conn, "pk_null_probe")["row_has_priced_offer"] is False


def test_trust_join_ignores_another_rows_offer(pg_engine):
    """Correlation check: the EXISTS must bind to cp.product_key, not to any
    offer in the table. A stray uncorrelated reference would make every row
    priced as soon as one priced offer existed anywhere."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_other_priced", ck="ck_other")
        _offer(conn, pk="pk_other_priced", list_price=75.00)
        _product(conn, pk="pk_mine_bare", ck="ck_mine")
        assert _joined(conn, "pk_mine_bare")["row_has_priced_offer"] is False


# ---------------------------------------------------------------------------
# c1.v0.7 — the canonical-election grain bridge
# ---------------------------------------------------------------------------


def _elect(conn, ck, sig):
    from sqlalchemy import text

    conn.execute(
        text("INSERT INTO content_canonical_election (content_key, canonical_sig_id) "
             "VALUES (:ck, :sig) ON CONFLICT (content_key) DO UPDATE "
             "SET canonical_sig_id = EXCLUDED.canonical_sig_id"),
        {"ck": ck, "sig": sig},
    )


def _named(name):
    from services.catalog_invariant_checks import _CHECKS

    for c in _CHECKS:
        if c["name"] == name:
            return c
    raise AssertionError(f"{name} is not registered")


def _count_of(conn, name):
    from sqlalchemy import text

    return conn.execute(text(_named(name)["count_sql"])).scalar()


def test_duplicate_invariant_counts_a_public_non_elected_sibling(pg_engine):
    """THE GRAIN REGRESSION TEST, reduced to its two rows.

    Both share a content_key. One holds the elected canonical URL; the other is
    public anyway, which is a duplicate PDP being independently promoted while
    its own rel=canonical points at the winner.
    """
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_win", ck="ck_dup", sig="sig_win")
        _product(conn, pk="pk_dupe", ck="ck_dup", sig="sig_dupe")
        _trust_public(conn, "pk_win")
        _trust_public(conn, "pk_dupe")
        _elect(conn, "ck_dup", "sig_win")

        assert _count_of(conn, "public_non_canonical_duplicate") == 1
        rows = conn.execute(
            __import__("sqlalchemy").text(
                _named("public_non_canonical_duplicate")["sample_sql"])).fetchall()
        assert [r[0] for r in rows] == ["pk_dupe"]


def test_duplicate_invariant_is_clean_when_only_the_elected_row_is_public(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_win", ck="ck_ok", sig="sig_win")
        _product(conn, pk="pk_shadowed", ck="ck_ok", sig="sig_other")
        _trust_public(conn, "pk_win")  # the sibling is shadow/blocked, not public
        _elect(conn, "ck_ok", "sig_win")
        assert _count_of(conn, "public_non_canonical_duplicate") == 0


def test_duplicate_invariant_ignores_content_keys_with_no_election(pg_engine):
    """Absence is "not yet decided", never "not canonical" — the same contract
    the policy gate honours. The companion invariant below is what stops this
    exemption becoming a hiding place."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_a", ck="ck_noelect", sig="sig_a")
        _product(conn, pk="pk_b", ck="ck_noelect", sig="sig_b")
        _trust_public(conn, "pk_a")
        _trust_public(conn, "pk_b")
        assert _count_of(conn, "public_non_canonical_duplicate") == 0


def test_missing_election_invariant_counts_an_unelected_multi_row_key(pg_engine):
    """Counted only when a row is actually PUBLIC — that is the set where a
    duplicate could be promoted with nothing to arbitrate between the siblings."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_a", ck="ck_multi", sig="sig_a")
        _product(conn, pk="pk_b", ck="ck_multi", sig="sig_b")
        _trust_public(conn, "pk_a")
        assert _count_of(conn, "public_multi_row_content_key_without_election") == 1


def test_missing_election_invariant_ignores_an_all_blocked_multi_row_key(pg_engine):
    """THE 32. Measured on prod 2026-07-31: 32 un-elected multi-row content_keys
    carrying 69 rows, every one trust-'blocked'. They have no election because
    nothing of theirs is an electable candidate — correct, not a backlog, and
    scripts/elect_content_canonicals writes zero rows for them.

    The first cut of this check omitted the PUBLIC conjunct and would have sat
    32-over-threshold from day one for a non-defect. A row no surface can reach
    needs no canonical URL. If this ever counts again, the check has gone deaf.
    """
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_a", ck="ck_multi", sig="sig_a")
        _product(conn, pk="pk_b", ck="ck_multi", sig="sig_b")
        # no trust rows at all == nothing public
        assert _count_of(conn, "public_multi_row_content_key_without_election") == 0

        _trust_blocked(conn, "pk_a")
        _trust_blocked(conn, "pk_b")
        assert _count_of(conn, "public_multi_row_content_key_without_election") == 0


def test_missing_election_invariant_clean_once_elected(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_a", ck="ck_multi", sig="sig_a")
        _product(conn, pk="pk_b", ck="ck_multi", sig="sig_b")
        _trust_public(conn, "pk_a")
        _elect(conn, "ck_multi", "sig_a")
        assert _count_of(conn, "public_multi_row_content_key_without_election") == 0


def test_missing_election_invariant_ignores_single_row_content_keys(pg_engine):
    """98% of the corpus. A content_key with one row needs no election — if this
    ever counts them, the check goes from 32 to ~10,000 and stops being read."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_solo", ck="ck_solo", sig="sig_solo")
        _trust_public(conn, "pk_solo")
        assert _count_of(conn, "public_multi_row_content_key_without_election") == 0


def test_missing_election_invariant_ignores_suppressed_siblings(pg_engine):
    """A content_key whose extra rows are all suppressed is not multi-row: the
    dedup sweep already decided. Counting it would resurrect retired rows as
    election work."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_live", ck="ck_supp", sig="sig_live")
        _product(conn, pk="pk_dead", ck="ck_supp", sig="sig_dead")
        conn.execute(text("UPDATE catalog_products SET suppressed_at = NOW() "
                          "WHERE product_key = 'pk_dead'"))
        _trust_public(conn, "pk_live")
        assert _count_of(conn, "public_multi_row_content_key_without_election") == 0


def test_trust_join_reports_the_election_per_row(pg_engine):
    """The join column that feeds the policy gate, on the real dialect."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_win", ck="ck_e", sig="sig_win")
        _product(conn, pk="pk_dupe", ck="ck_e", sig="sig_dupe")
        _elect(conn, "ck_e", "sig_win")

        assert _joined(conn, "pk_win")["row_is_elected_canonical"] is True
        assert _joined(conn, "pk_dupe")["row_is_elected_canonical"] is False


def test_trust_join_returns_null_when_no_election_exists(pg_engine):
    """Tri-state at the SQL layer. This column is the one place where NULL is a
    normal production value rather than a missing-caller artifact, so the
    correlated subquery must yield NULL — not FALSE — with no election row."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_noelect", ck="ck_noelect", sig="sig_x")
        assert _joined(conn, "pk_noelect")["row_is_elected_canonical"] is None
