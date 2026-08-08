"""The canonical feed must state when a row has been RETIRED but still serves.

READ THIS FIRST — the state these fixtures build is one prod can no longer
produce, and that is deliberate. They are a REGRESSION GATE, not a model of the
corpus.

WHAT WAS TRUE WHEN THIS FILE WAS WRITTEN. `suppression_reason` set WITHOUT
`suppressed_at` was a real, populated state — the step-5 lanes, migration 139's
cross-merchant sweep, the brand-namesake retirements and the d2_* identity
resolutions all wrote the LABEL and left the GATE column null. `suppressed_at` is
what every serving gate reads, so those rows passed every `suppressed_at IS NULL`
filter in the system, `sitemap_candidate_filter` included, and were advertised as
though nothing had been decided about them.

Measured on the live 7,509-URL sitemap 2026-07-29 — 187 advertised URLs pointed
at such a row: 135 `wrong_brand_namesake_wave3_20260718`, 50
`cross_merchant_redundant_external_seed`, 2 `step5_campaign_clone_dup`. The first
group is why it mattered rather than being tidy: those rows were retired for
carrying the WRONG BRAND, so serving them published a PDP with incorrect brand
attribution.

WHAT IS TRUE NOW. The 2026-07-30 backfill gave all 2,332 reason-only rows a
`suppressed_at` (reconstructed per cohort at its own apply instant), #1660
(#1648 P1a) taught every writer to set both columns and every revert to clear
both, and `catalog_invariant_checks` pins both directions at threshold 0. Prod
reports 0 on `suppression_reason_without_timestamp` and 0 on
`suppression_timestamp_without_reason`. So `suppression_reason IS NOT NULL` now
implies `suppressed_at IS NOT NULL`, the feed's own WHERE drops the row at ROW
grain, and `tombstoned` is false on every feed row: 0 of 8,906 on prod
2026-08-08. Drop only that conjunct and 1,189 reason-bearing rows come back into
the feed with 441 of them advertisable — the gate is what closed it.

IT IS NOT THE CONTENT_KEY GRAIN, checked because it is the obvious wrong guess.
`index_pipeline_state` is keyed on content_key and `_select_content_key_state`
stores the MAX across the key's rows, so a key whose retired row has a live
sibling stays `serving_eligible`. Real — 593 of the 8,064 advertised rows share a
content_key with a suppressed row, and 539 share its exact `canonical_url`,
because collapsing same-URL duplicates is what step-5 lane 2 does — but every one
of those 593 is the clean KEEPER, and 0 content_keys are `serving_eligible` with
all of their rows suppressed. The grain keeps the KEY eligible for its live
siblings; it never advertises the retired row. (Those 539 shared URLs are a trap
for the measurement, not for the filter: a sitemap-URL→row join keyed on URL will
report "advertised URL points at a retired row" for rows that are clean.)

WHY THE FIXTURES STAY. The flag's whole remaining job is to catch a writer that
regresses to label-without-timestamp — such a row passes the feed's WHERE, lands
on the page the sitemap generator reads, and this column is what says so, a
second and independent alarm to the invariant check. A flag that is silently
always false is indistinguishable from "no tombstones exist", which is exactly
the reading that let 187 of them stay advertised, so the tests below still assert
it answers BOTH ways. They construct the reason-only row directly because nothing
in the system will hand them one any more.

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


# `index_pipeline_state` is NOT a `db.catalog` table — it is the lightweight
# Core handle declared in `services.canonical_sitemap_candidates`, so
# `metadata.create_all` does not know about it. Same additive pattern the
# sibling gate `test_serving_not_renderable_invariant_postgres.py` uses: only
# tables `db.catalog` does NOT own, and only ADD COLUMN IF NOT EXISTS, so a
# shared database keeps whatever another gate already put there.
_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS index_pipeline_state (content_key text PRIMARY KEY);
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS serving_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS index_eligible boolean;
"""


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401  (registers catalog_products on the shared MetaData)
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for stmt in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(stmt))
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


def _insert_feed_row(conn, *, pk, ck, reason=None, suppressed=False):
    """A row shaped to PASS `sitemap_candidate_filter(widen=False)` unless gated.

    Merchant + index-pipeline rows are upserted so several rows can share one
    `content_key` — which is the whole point of the grain test below.
    """
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO catalog_merchants (merchant_id, indexable, status) "
            "VALUES ('m_feed', TRUE, 'active') ON CONFLICT DO NOTHING"
        )
    )
    conn.execute(
        text(
            "INSERT INTO index_pipeline_state (content_key, serving_eligible) "
            "VALUES (:ck, TRUE) ON CONFLICT (content_key) DO UPDATE "
            "SET serving_eligible = TRUE"
        ),
        {"ck": ck},
    )
    conn.execute(
        text(
            "INSERT INTO catalog_products "
            "(product_key, merchant_id, platform, source_product_id, title, "
            " content_key, catalog_track, pivota_signature_id, "
            " suppression_reason, suppressed_at) "
            "VALUES (:pk, 'm_feed', 'external_seed', :pk, :pk, :ck, "
            "        'external_referral', :sig, :reason, "
            f"        {'NOW()' if suppressed else 'NULL'})"
        ),
        {"pk": pk, "ck": ck, "sig": f"sig_{pk}", "reason": reason},
    )


def _feed_keys(conn, *, widen):
    from sqlalchemy import select

    from db.catalog import catalog_products
    from services.canonical_sitemap_candidates import (
        sitemap_candidate_filter,
        sitemap_candidate_join,
    )

    rows = conn.execute(
        select(catalog_products.c.product_key)
        .select_from(sitemap_candidate_join(widen=widen))
        .where(sitemap_candidate_filter(widen=widen))
    ).fetchall()
    return {r[0] for r in rows}


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

    The reason-only rows here are HAND-BUILT because no writer produces them any
    more (#1660 + the 2026-07-30 backfill; invariant threshold 0). That is the
    point: this asserts the DETECTOR still fires, on the one shape that would
    mean a writer had regressed. See the module docstring.
    """
    from sqlalchemy import select, text

    from db.catalog import catalog_products
    from routes.pivota_canonical_routes import _tombstoned_column

    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_products"))
        _insert(conn, pk="clean", reason=None)
        # The load-bearing shape: reason set, suppressed_at NULL — the state
        # that passes every suppressed_at IS NULL filter. Now an invariant
        # VIOLATION (suppression_reason_without_timestamp, threshold 0) rather
        # than a corpus state, which is why it has to be inserted by hand.
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

    Note the fixture is the MIRROR violation — timestamp without label, which
    `suppression_timestamp_without_reason` also pins at 0, and which the 07-30
    backfill created the risk of by leaving every revert path clearing the label
    alone. Both fixtures in this file are therefore invariant violations by
    construction; what they pin is that this column keeps distinguishing them,
    so a regression in either direction reads differently at the feed.
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


@pytest.mark.parametrize("widen", [False, True])
def test_a_both_column_tombstone_never_reaches_the_feed(pg_engine, widen):
    """The mechanism that actually closed this, asserted end-to-end.

    Since #1660 every writer sets BOTH columns, so a retired row is caught by
    `sitemap_candidate_filter`'s own `suppressed_at IS NULL` conjunct and never
    reaches the SELECT list that computes `tombstoned`. That is why the flag is
    false on all 8,906 prod feed rows — not because retirement stopped
    happening.

    The reason-only row is asserted PRESENT in the same breath, because that is
    the pair of facts the rest of this file rests on: the flag is unreachable
    for a correctly-written tombstone and reachable the moment a writer drops
    the timestamp. Without the second half, keeping the reason-only fixtures
    would be pinning a state the feed could never show.

    Both flag states are asserted because the conjunct is in both arms of the
    filter; a widening that relaxed the identity or merchant terms must not
    relax this one.
    """
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_products"))
        _insert_feed_row(conn, pk="live", ck="ck_live")
        _insert_feed_row(
            conn,
            pk="retired",
            ck="ck_retired",
            reason="step5_same_merchant_same_url_dup",
            suppressed=True,
        )
        # The regression shape: label written, timestamp forgotten.
        _insert_feed_row(
            conn,
            pk="regressed",
            ck="ck_regressed",
            reason="step5_same_merchant_same_url_dup",
            suppressed=False,
        )
        keys = _feed_keys(conn, widen=widen)
        conn.execute(text("DELETE FROM catalog_products"))

    assert "live" in keys
    assert "retired" not in keys, (
        "a row with BOTH suppression columns set must be filtered out before "
        "the feed can carry it — this is what makes `tombstoned` false rather "
        "than the corpus having no tombstones"
    )
    assert "regressed" in keys, (
        "a label-without-timestamp row must still reach the feed, or the "
        "`tombstoned` field has nothing left to detect and the reason-only "
        "fixtures above are pinning an unreachable state"
    )


def test_content_key_grain_does_not_readvertise_a_retired_row(pg_engine):
    """The wrong explanation, disproved on real rows.

    `index_pipeline_state` is keyed on content_key and
    `index_pipeline_state_service._select_content_key_state` stores the MAX
    state across the key's rows, so a key holding a retired row AND a live one
    stays `serving_eligible`. The tempting conclusion is that the key's state
    drags the retired row back onto the sitemap. It does not: the suppression
    conjunct is asked of THIS row, so the key stays eligible for its live
    sibling and the retired row is still dropped.

    Prod 2026-08-08 measures the same shape at scale — 593 of the 8,064
    advertised rows share a content_key with a suppressed row (539 of them
    sharing its exact `canonical_url`, since collapsing same-URL duplicates is
    what step-5 lane 2 does), and every one of the 593 is the clean KEEPER.
    """
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_products"))
        # One content_key, two rows: the dedupe keeper and its retired loser.
        _insert_feed_row(conn, pk="keeper", ck="ck_shared")
        _insert_feed_row(
            conn,
            pk="loser",
            ck="ck_shared",
            reason="step5_same_merchant_same_url_dup",
            suppressed=True,
        )
        keys = _feed_keys(conn, widen=False)
        conn.execute(text("DELETE FROM catalog_products"))

    assert keys == {"keeper"}, (
        "the shared content_key must keep the KEEPER advertised and the loser "
        "out — suppression is row-grained, index eligibility is key-grained, "
        "and conflating the two is the mis-attribution this pins"
    )
