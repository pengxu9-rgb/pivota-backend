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
-- Create minimal, then widen.
--
-- product_quality_snapshot needs the additive form for a SECOND reason:
-- test_dead_quality_component_canary_postgres DROPs and recreates it (it needs
-- real rows, and the production NOT NULL identity columns get in its way). So
-- this module cannot assume ANY shape — whatever it finds, it widens.
CREATE TABLE IF NOT EXISTS product_quality_snapshot (id bigserial PRIMARY KEY);
ALTER TABLE product_quality_snapshot ADD COLUMN IF NOT EXISTS merchant_id text;
ALTER TABLE product_quality_snapshot ADD COLUMN IF NOT EXISTS platform text;
ALTER TABLE product_quality_snapshot ADD COLUMN IF NOT EXISTS platform_product_id text;
ALTER TABLE product_quality_snapshot ADD COLUMN IF NOT EXISTS content_quality_score double precision;
ALTER TABLE product_quality_snapshot ADD COLUMN IF NOT EXISTS snapshot_date date;
-- `canonical_sig_id` is deliberately NOT NULL-constrained here. Prod (mig 181)
-- declares it NOT NULL, but ADD COLUMN on an already-populated shared table
-- cannot, and these tests never insert a NULL.
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
    "product_quality_snapshot",
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


# ---------------------------------------------------------------------------
# elected_canonical_below_quality_floor — the circularity check
# ---------------------------------------------------------------------------


def _score(conn, *, merchant_id="external_seed", source_product_id, score):
    from sqlalchemy import text

    conn.execute(
        text("INSERT INTO product_quality_snapshot "
             "(merchant_id, platform, platform_product_id, content_quality_score, snapshot_date) "
             "VALUES (:m, 'external_seed', :spid, :sc, CURRENT_DATE)"),
        {"m": merchant_id, "spid": source_product_id, "sc": score},
    )


def _floor():
    from services.index_pipeline_state_service import QUALITY_SCORE_THRESHOLD

    return QUALITY_SCORE_THRESHOLD


def test_elected_below_floor_is_counted(pg_engine):
    """The prod shape, reduced: the elected row is the WEAK one and holds the URL
    on its sibling's strength. `_product` inserts source_product_id = pk."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_weak", ck="ck_circ", sig="sig_weak")
        _product(conn, pk="pk_strong", ck="ck_circ", sig="sig_strong")
        _score(conn, source_product_id="pk_weak", score=_floor() - 20)
        _score(conn, source_product_id="pk_strong", score=_floor() + 12)
        _elect(conn, "ck_circ", "sig_weak")
        assert _count_of(conn, "elected_canonical_below_quality_floor") == 1


def test_elected_above_floor_is_not_counted(pg_engine):
    """The other direction. A check that only counts up is not a signal."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_weak", ck="ck_ok", sig="sig_weak")
        _product(conn, pk="pk_strong", ck="ck_ok", sig="sig_strong")
        _score(conn, source_product_id="pk_weak", score=_floor() - 20)
        _score(conn, source_product_id="pk_strong", score=_floor() + 12)
        _elect(conn, "ck_ok", "sig_strong")  # the STRONG row holds the URL
        assert _count_of(conn, "elected_canonical_below_quality_floor") == 0


def test_not_counted_when_the_content_key_is_not_serving(pg_engine):
    """Only live content matters. A weak canonical on a content_key nothing
    serves is not advertising anything, and counting it would drown the signal
    in rows no surface can reach."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_weak", ck="ck_dark", sig="sig_weak")
        _score(conn, source_product_id="pk_weak", score=_floor() - 20)
        _elect(conn, "ck_dark", "sig_weak")
        conn.execute(text("UPDATE index_pipeline_state SET serving_eligible = FALSE "
                          "WHERE content_key = 'ck_dark'"))
        assert _count_of(conn, "elected_canonical_below_quality_floor") == 0


def test_a_missing_quality_snapshot_counts_as_below_floor(pg_engine):
    """An unscored canonical is not a passing canonical — `coalesce(..., -1)`.
    If this ever reads NULL as "fine", an entirely unscored row can hold the URL
    and the check stays silent, which is the failure mode the whole
    serving-coverage programme keeps re-learning."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_unscored", ck="ck_unscored", sig="sig_unscored")
        _elect(conn, "ck_unscored", "sig_unscored")
        assert _count_of(conn, "elected_canonical_below_quality_floor") == 1


def test_the_floor_is_read_from_the_scorer_not_hardcoded(pg_engine):
    """The threshold moved 65.0 -> 71.4 once already, in lockstep with dropping a
    dead scorer component. A hardcoded copy here would have silently kept the old
    bar. Pin that the SQL carries the live constant."""
    assert str(_floor()) in _named("elected_canonical_below_quality_floor")["count_sql"]


def test_elected_below_floor_sample_returns_the_content_key(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_weak", ck="ck_sample_circ", sig="sig_weak")
        _score(conn, source_product_id="pk_weak", score=_floor() - 20)
        _elect(conn, "ck_sample_circ", "sig_weak")
        rows = conn.execute(
            __import__("sqlalchemy").text(
                _named("elected_canonical_below_quality_floor")["sample_sql"])).fetchall()
        assert [r[0] for r in rows] == ["ck_sample_circ"]


# ---------------------------------------------------------------------------
# cross_domain_content_key_fragmented_identity — one product, two entities
# ---------------------------------------------------------------------------


def _listing(conn, *, source_product_id, sig_group, merchant_id="external_seed"):
    from sqlalchemy import text

    conn.execute(
        text("INSERT INTO pdp_identity_listing "
             "(source_listing_ref, product_id, merchant_id, sellable_item_group_id, "
             " identity_status) "
             "VALUES (:ref, :pid, :m, :sig, 'approved')"),
        {"ref": f"{merchant_id}:{source_product_id}", "pid": source_product_id,
         "m": merchant_id, "sig": sig_group},
    )


def _domain(conn, pk, domain):
    from sqlalchemy import text

    conn.execute(text("UPDATE catalog_products SET source_domain = :d "
                      "WHERE product_key = :pk"), {"d": domain, "pk": pk})


def test_fragmented_identity_counts_a_split_cross_domain_product(pg_engine):
    """The prod shape: one physical product, two retailer domains, TWO entities.

    Every surface that keys off sellable_item_group_id — checkout handoff, the
    ACP feed, discovery, recommendations — then sees half the product.
    """
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_brand", ck="ck_split", sig="sig_brand")
        _product(conn, pk="pk_retailer", ck="ck_split", sig="sig_retailer")
        _domain(conn, "pk_brand", "brand.com")
        _domain(conn, "pk_retailer", "ulta.com")
        _listing(conn, source_product_id="pk_brand", sig_group="grp_a")
        _listing(conn, source_product_id="pk_retailer", sig_group="grp_b")
        _trust_public(conn, "pk_brand")
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 1


def test_fragmented_identity_clean_when_both_domains_share_one_entity(pg_engine):
    """The other direction — 83 of 103 cross-domain content_keys look like this
    today, and they must never be counted."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_brand", ck="ck_joined", sig="sig_brand")
        _product(conn, pk="pk_retailer", ck="ck_joined", sig="sig_retailer")
        _domain(conn, "pk_brand", "brand.com")
        _domain(conn, "pk_retailer", "ulta.com")
        _listing(conn, source_product_id="pk_brand", sig_group="grp_same")
        _listing(conn, source_product_id="pk_retailer", sig_group="grp_same")
        _trust_public(conn, "pk_brand")  # satisfy the anchor, or this is vacuous
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 0


def test_fragmented_identity_ignores_single_domain_splits(pg_engine):
    """Two rows on the SAME domain splitting into two entities is a dedup
    problem, not a multi-retailer one. 89 of 192 multi-row content_keys are
    same-domain; folding them in would quadruple the count and change what the
    alarm means."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_a", ck="ck_same_domain", sig="sig_a")
        _product(conn, pk="pk_b", ck="ck_same_domain", sig="sig_b")
        _domain(conn, "pk_a", "brand.com")
        _domain(conn, "pk_b", "brand.com")
        _listing(conn, source_product_id="pk_a", sig_group="grp_a")
        _listing(conn, source_product_id="pk_b", sig_group="grp_b")
        _trust_public(conn, "pk_a")  # satisfy the anchor, or this is vacuous
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 0


def test_fragmented_identity_ignores_suppressed_siblings(pg_engine):
    """A retired row is not a second retailer — and retirement has TWO markers.

    This test used to set only `suppressed_at`, which is the column the query
    already filtered, so it asserted a property the SQL did not have.
    `suppression_reason` WITHOUT `suppressed_at` is a real, populated state
    (canonical_sitemap_candidates.not_tombstoned enumerates the writers), and
    migration 139 — `cross_merchant_redundant_external_seed` — sets ONLY the
    reason. Its predicate is "an external_seed row whose content_key has a live
    first-party sibling under a different merchant": a retired cross-merchant
    duplicate that still carries its own identity listing, i.e. this check's
    exact false positive. Both markers are covered now.
    """
    from sqlalchemy import text

    for marker in ("suppressed_at = NOW()",
                   "suppression_reason = 'cross_merchant_redundant_external_seed'"):
        with pg_engine.begin() as conn:
            _reset(conn)
            _product(conn, pk="pk_live", ck="ck_supp", sig="sig_live")
            _product(conn, pk="pk_dead", ck="ck_supp", sig="sig_dead")
            _domain(conn, "pk_live", "brand.com")
            _domain(conn, "pk_dead", "ulta.com")
            _listing(conn, source_product_id="pk_live", sig_group="grp_a")
            _listing(conn, source_product_id="pk_dead", sig_group="grp_b")
            _trust_public(conn, "pk_live")  # satisfy the anchor, or this is vacuous
            conn.execute(text(f"UPDATE catalog_products SET {marker} "
                              "WHERE product_key = 'pk_dead'"))
            assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 0, marker


def test_fragmented_identity_uses_the_canonical_identity_join(pg_engine):
    """The pil join must carry BOTH conjuncts the trust upserter carries.

    A minted (catalog_enrichment_agent_v1) row's source_product_id is a name
    slug, not a seed id, so a naive `pil.product_id = cp.source_product_id`
    misses and the group reads NULL — and because count(DISTINCT) skips NULLs,
    the whole Path-C brand-mint lane would be INVISIBLE here. That lane is one
    half of exactly the brand-plus-retailer pair this check exists to find.
    """
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_minted", ck="ck_minted", sig="sig_minted")
        _product(conn, pk="pk_retailer", ck="ck_minted", sig="sig_retailer")
        _domain(conn, "pk_minted", "brand.com")
        _domain(conn, "pk_retailer", "ulta.com")
        conn.execute(text("UPDATE catalog_products "
                          "SET source_system = 'catalog_enrichment_agent_v1', "
                          "    source_product_id = 'a-name-slug-not-a-seed-id' "
                          "WHERE product_key = 'pk_minted'"))
        conn.execute(text(
            "INSERT INTO external_product_seeds "
            "(external_product_id, attached_product_key, status, updated_at) "
            "VALUES ('ext_minted_seed', 'pk_minted', 'active', NOW())"))
        _listing(conn, source_product_id="ext_minted_seed", sig_group="grp_minted")
        _listing(conn, source_product_id="pk_retailer", sig_group="grp_retailer")
        _trust_public(conn, "pk_minted")
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 1


def test_fragmented_identity_does_not_guess_at_unknown_domains(pg_engine):
    """#1643: 28% of rows carry no source_domain. A bare column with a '?'
    sentinel makes unknown-vs-unknown read as AGREEMENT and unknown-vs-known as
    DISAGREEMENT — wrong in both directions. The chain + NULL means the check
    fires only when we positively KNOW there are two different domains."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_known", ck="ck_unknown_dom", sig="sig_a")
        _product(conn, pk="pk_unknown", ck="ck_unknown_dom", sig="sig_b")
        _domain(conn, "pk_known", "brand.com")
        conn.execute(text("UPDATE catalog_products SET source_domain = NULL "
                          "WHERE product_key = 'pk_unknown'"))
        _listing(conn, source_product_id="pk_known", sig_group="grp_a")
        _listing(conn, source_product_id="pk_unknown", sig_group="grp_b")
        _trust_public(conn, "pk_known")  # satisfy the anchor, or this is vacuous
        # One known domain + one unknown is NOT evidence of two retailers.
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 0


def test_fragmented_identity_skips_a_sibling_with_no_identity_listing(pg_engine):
    """Documents the deliberate exemption. "One identified, one not" is a
    DIFFERENT defect (run the identity graph) from "resolved into two" (merge
    the groups), so it is out of scope here — the same call
    public_non_canonical_duplicate makes for a missing election. If this ever
    starts counting, the threshold becomes a blend of two populations."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_listed", ck="ck_half", sig="sig_a")
        _product(conn, pk="pk_unlisted", ck="ck_half", sig="sig_b")
        _domain(conn, "pk_listed", "brand.com")
        _domain(conn, "pk_unlisted", "ulta.com")
        _listing(conn, source_product_id="pk_listed", sig_group="grp_a")
        _trust_public(conn, "pk_listed")  # satisfy the anchor, or this is vacuous
        # pk_unlisted deliberately has NO pdp_identity_listing row.
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 0


def test_fragmented_identity_threshold_is_pinned(pg_engine):
    """The comment says the threshold must never be raised. Nothing enforced
    that, so a silent bump to 25 would pass CI. This is the enforcement — and
    the sibling checks pin theirs the same way. Re-measure prod before moving
    it, and move it DOWN as the merge lands."""
    assert _named("cross_domain_content_key_fragmented_identity")["default_threshold"] == 7


def test_fragmented_identity_sample_returns_the_content_key(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_brand", ck="ck_sample_frag", sig="sig_brand")
        _product(conn, pk="pk_retailer", ck="ck_sample_frag", sig="sig_retailer")
        _domain(conn, "pk_brand", "brand.com")
        _domain(conn, "pk_retailer", "ulta.com")
        _listing(conn, source_product_id="pk_brand", sig_group="grp_a")
        _listing(conn, source_product_id="pk_retailer", sig_group="grp_b")
        _trust_public(conn, "pk_brand")
        rows = conn.execute(
            __import__("sqlalchemy").text(
                _named("cross_domain_content_key_fragmented_identity")["sample_sql"])
        ).fetchall()
        assert [r[0] for r in rows] == ["ck_sample_frag"]


def test_fragmented_identity_requires_a_serving_row(pg_engine):
    """THE SERVING ANCHOR. Same lesson as the `public` conjunct on
    public_multi_row_content_key_without_election.

    Measured 2026-08-01: of 18 fragmented cross-domain content_keys, only 7 carry
    any trust-'public' row — 11 were dark. This check's whole justification is
    what checkout handoff / ACP feed / discovery / recommendations SEE, and a
    split identity on a content_key no surface reaches misroutes nothing.
    Counting the dark ones would park this permanently amber.
    """
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_brand", ck="ck_dark_frag", sig="sig_brand")
        _product(conn, pk="pk_retailer", ck="ck_dark_frag", sig="sig_retailer")
        _domain(conn, "pk_brand", "brand.com")
        _domain(conn, "pk_retailer", "ulta.com")
        _listing(conn, source_product_id="pk_brand", sig_group="grp_a")
        _listing(conn, source_product_id="pk_retailer", sig_group="grp_b")
        # Genuinely fragmented, but NOTHING serves it.
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 0

        # One public row is enough to make it a live defect.
        _trust_public(conn, "pk_retailer")
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 1


def test_fragmented_identity_join_carries_the_merchant_conjunct(pg_engine):
    """`source_listing_ref` is merchant_id:product_id (ADR-008), so product_id
    ALONE is not unique. Without the merchant conjunct the LATERAL can pick a
    FOREIGN merchant's listing for this row.

    Every other test here uses merchant_id='external_seed' for both rows, so the
    conjunct was untested — dropping it survived the whole suite.
    """
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_ours", ck="ck_mconj", sig="sig_ours")
        _product(conn, pk="pk_theirs", ck="ck_mconj", sig="sig_theirs",
                 merchant_id="merch_other")
        _domain(conn, "pk_ours", "brand.com")
        _domain(conn, "pk_theirs", "ulta.com")
        # SAME product_id under TWO merchants, pointing at DIFFERENT groups.
        conn.execute(text("UPDATE catalog_products SET source_product_id = 'shared_pid' "
                          "WHERE product_key IN ('pk_ours','pk_theirs')"))
        _listing(conn, source_product_id="shared_pid", sig_group="grp_ours",
                 merchant_id="external_seed")
        _listing(conn, source_product_id="shared_pid", sig_group="grp_theirs",
                 merchant_id="merch_other")
        _trust_public(conn, "pk_ours")
        # Each row must read ITS OWN merchant's listing -> two groups -> counted.
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 1


def test_fragmented_identity_serving_anchor_is_correlated(pg_engine):
    """The anchor must correlate to THIS content_key. Mutating
    `cp2.content_key = cp.content_key` to `cp2.content_key = cp2.content_key`
    survived every test, because none had two content_keys — a public row on an
    UNRELATED key would then satisfy the anchor for a dark one."""
    with pg_engine.begin() as conn:
        _reset(conn)
        # Fragmented but entirely dark.
        _product(conn, pk="pk_dark_a", ck="ck_dark", sig="sig_da")
        _product(conn, pk="pk_dark_b", ck="ck_dark", sig="sig_db")
        _domain(conn, "pk_dark_a", "brand.com")
        _domain(conn, "pk_dark_b", "ulta.com")
        _listing(conn, source_product_id="pk_dark_a", sig_group="grp_a")
        _listing(conn, source_product_id="pk_dark_b", sig_group="grp_b")
        # A DIFFERENT content_key that IS public.
        _product(conn, pk="pk_other", ck="ck_other", sig="sig_other")
        _domain(conn, "pk_other", "elsewhere.com")
        _trust_public(conn, "pk_other")
        # The public row belongs to ck_other, so ck_dark must NOT be counted.
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 0


def test_fragmented_identity_minted_pick_prefers_a_seed_carrying_a_listing(pg_engine):
    """Path-C attaches one seed PER OFFER to a product_key, and
    catalog_row_trust_upserter.minted_seed_one prefers the seed that CARRIES a
    listing (MINTED_SEED_IDENTITY_LEG) because the winner's external_product_id
    IS the pil join key. A bare `updated_at DESC` pick selects the identity-less
    sibling, the group reads NULL, and real fragmentation goes UNCOUNTED.
    """
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_minted", ck="ck_pick", sig="sig_minted")
        _product(conn, pk="pk_retailer", ck="ck_pick", sig="sig_retailer")
        _domain(conn, "pk_minted", "brand.com")
        _domain(conn, "pk_retailer", "ulta.com")
        conn.execute(text("UPDATE catalog_products "
                          "SET source_system = 'catalog_enrichment_agent_v1', "
                          "    source_product_id = 'slug-not-a-seed-id' "
                          "WHERE product_key = 'pk_minted'"))
        # NEWER seed with NO listing, OLDER seed WITH one. updated_at alone picks
        # the newer (wrong); the identity leg picks the older (right).
        conn.execute(text(
            "INSERT INTO external_product_seeds "
            "(external_product_id, attached_product_key, status, updated_at) VALUES "
            "('ext_no_listing', 'pk_minted', 'active', NOW()), "
            "('ext_has_listing', 'pk_minted', 'active', NOW() - INTERVAL '10 days')"))
        _listing(conn, source_product_id="ext_has_listing", sig_group="grp_minted")
        _listing(conn, source_product_id="pk_retailer", sig_group="grp_retailer")
        _trust_public(conn, "pk_minted")
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 1


def test_fragmented_identity_minted_pick_skips_an_inactive_seed(pg_engine):
    """The other direction of the same divergence: a NEWER but INACTIVE seed
    pointing at a stale group. SEED_PICK_ORDER puts `status='active'` first, so
    the active seed wins and the key reads CLEAN. A bare updated_at pick would
    take the inactive one and count a false positive."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_minted", ck="ck_stale", sig="sig_minted")
        _product(conn, pk="pk_retailer", ck="ck_stale", sig="sig_retailer")
        _domain(conn, "pk_minted", "brand.com")
        _domain(conn, "pk_retailer", "ulta.com")
        conn.execute(text("UPDATE catalog_products "
                          "SET source_system = 'catalog_enrichment_agent_v1', "
                          "    source_product_id = 'slug-2' "
                          "WHERE product_key = 'pk_minted'"))
        conn.execute(text(
            "INSERT INTO external_product_seeds "
            "(external_product_id, attached_product_key, status, updated_at) VALUES "
            "('ext_stale', 'pk_minted', 'inactive', NOW()), "
            "('ext_active', 'pk_minted', 'active', NOW() - INTERVAL '10 days')"))
        _listing(conn, source_product_id="ext_stale", sig_group="grp_stale")
        _listing(conn, source_product_id="ext_active", sig_group="grp_shared")
        _listing(conn, source_product_id="pk_retailer", sig_group="grp_shared")
        _trust_public(conn, "pk_minted")
        # Active seed agrees with the retailer -> clean.
        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 0


# ---------------------------------------------------------------------------
# Issue #1665 — the identity leg must be merchant-scoped
# ---------------------------------------------------------------------------


def test_identity_leg_ignores_a_listing_at_another_merchant(pg_engine):
    """A seed must not win the identity leg on a FOREIGN merchant's listing.

    `source_listing_ref` is merchant_id:product_id (ADR-008), so `product_id`
    alone is not unique across merchants. While the leg matched on product_id
    ALONE, a seed carrying a listing at merchant M2 satisfied it, beat the seed
    carrying the RIGHT listing at M1 on `updated_at`, and the outer lateral —
    which does carry the merchant conjunct — then found nothing. The group read
    NULL, `count(DISTINCT)` skipped it, and a genuinely fragmented cross-domain
    content_key reported CLEAN.

    This is the exact table from issue #1665: with the rival's foreign listing
    present the count went 1 -> 0. It must now stay 1.
    """
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_minted", ck="ck_x", sig="sig_minted",
                 merchant_id="merch_m1")
        _product(conn, pk="pk_retailer", ck="ck_x", sig="sig_retailer",
                 merchant_id="merch_m1")
        _domain(conn, "pk_minted", "brand.com")
        _domain(conn, "pk_retailer", "ulta.com")
        conn.execute(text("UPDATE catalog_products "
                          "SET source_system = 'catalog_enrichment_agent_v1', "
                          "    source_product_id = 'slug-not-a-seed-id' "
                          "WHERE product_key = 'pk_minted'"))
        # ext_old is OLDER but carries the listing at THIS merchant (M1).
        # ext_new is NEWER and carries a listing only at a FOREIGN merchant (M2).
        conn.execute(text(
            "INSERT INTO external_product_seeds "
            "(external_product_id, attached_product_key, status, updated_at) VALUES "
            "('ext_new', 'pk_minted', 'active', NOW()), "
            "('ext_old', 'pk_minted', 'active', NOW() - INTERVAL '10 days')"))
        _listing(conn, source_product_id="ext_old", sig_group="grp_minted",
                 merchant_id="merch_m1")
        _listing(conn, source_product_id="ext_new", sig_group="grp_foreign",
                 merchant_id="merch_m2")
        _listing(conn, source_product_id="pk_retailer", sig_group="grp_retailer",
                 merchant_id="merch_m1")
        _trust_public(conn, "pk_minted")

        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 1


def test_identity_leg_still_prefers_the_seed_that_resolves_at_this_merchant(pg_engine):
    """Control for the test above: with NO foreign listing in play the pick is
    unchanged, so the merchant conjunct is not simply disabling the leg."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_minted", ck="ck_x", sig="sig_minted",
                 merchant_id="merch_m1")
        _product(conn, pk="pk_retailer", ck="ck_x", sig="sig_retailer",
                 merchant_id="merch_m1")
        _domain(conn, "pk_minted", "brand.com")
        _domain(conn, "pk_retailer", "ulta.com")
        conn.execute(text("UPDATE catalog_products "
                          "SET source_system = 'catalog_enrichment_agent_v1', "
                          "    source_product_id = 'slug-not-a-seed-id' "
                          "WHERE product_key = 'pk_minted'"))
        conn.execute(text(
            "INSERT INTO external_product_seeds "
            "(external_product_id, attached_product_key, status, updated_at) VALUES "
            "('ext_new', 'pk_minted', 'active', NOW()), "
            "('ext_old', 'pk_minted', 'active', NOW() - INTERVAL '10 days')"))
        _listing(conn, source_product_id="ext_old", sig_group="grp_minted",
                 merchant_id="merch_m1")
        _listing(conn, source_product_id="pk_retailer", sig_group="grp_retailer",
                 merchant_id="merch_m1")
        _trust_public(conn, "pk_minted")

        assert _count_of(conn, "cross_domain_content_key_fragmented_identity") == 1


def test_the_identity_leg_refuses_to_build_without_a_merchant():
    """There must be no spelling of this leg that omits the merchant."""
    import pytest

    from services.source_quarantine import minted_seed_identity_leg_sql

    for bad in ("", "   "):
        with pytest.raises(ValueError, match="merchant"):
            minted_seed_identity_leg_sql(bad)

    sql = minted_seed_identity_leg_sql("cp.merchant_id")
    assert "spl.merchant_id = cp.merchant_id" in sql
    assert "spl.product_id = s.external_product_id" in sql


def test_the_upserter_cte_joins_the_catalog_row_for_its_merchant():
    """`cp` is not in scope inside a CTE, so the merchant must be joined in.
    Without this the leg would silently compare against nothing."""
    from services.catalog_row_trust_upserter import _PRODUCT_JOIN_CTES

    start = _PRODUCT_JOIN_CTES.find("minted_seed_one AS (")
    block = _PRODUCT_JOIN_CTES[start: _PRODUCT_JOIN_CTES.find("\n  ),", start)]
    assert "LEFT JOIN catalog_products cpx" in block
    assert "cpx.product_key = s.attached_product_key" in block
    assert "spl.merchant_id = cpx.merchant_id" in block

    # The dead, fan-out-prone join it replaced must be gone. Assert against the
    # CODE only: the comment explaining the removal quotes the removed join
    # verbatim, and would otherwise fail this on its own description.
    code = "\n".join(ln for ln in block.split("\n")
                     if not ln.strip().startswith("--"))
    assert "LEFT JOIN pdp_identity_listing spl" not in code
