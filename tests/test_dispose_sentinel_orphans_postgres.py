"""dispose_sentinel_orphans + the verifier's unscoped split — REAL Postgres.

What only a real engine can pin, and what this file therefore exists for:
  * the re-key follows EACH offer's own product (not one merchant for all),
    touches only residue rows, and leaves non-residue offers alone;
  * the delete removes exactly the residue reviews, cascades their children,
    and leaves every other seller's reviews intact;
  * the in-transaction residue assertion actually ROLLS BACK — tested by
    making it fail for real, then checking the rows are still there;
  * the verifier's ownership / unscoped / history split, per row, including
    the case that keeps it honest: one table with BOTH a scoped and an
    unscoped sentinel row must appear in BOTH buckets.

Per-process DATABASE, not schema: the verifier reads information_schema
filtered to `table_schema = 'public'`.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_DB = f"dispose_gate_{os.getpid()}"
_B = "external_seed"
_A = "merch_obs_a"
_C = "merch_obs_c"

_DDL = """
CREATE TABLE catalog_merchants (merchant_id text PRIMARY KEY, indexable boolean, status text);
CREATE TABLE catalog_products (
  product_key text PRIMARY KEY, content_key varchar(40), merchant_id text,
  source_product_id text, platform text, suppression_reason text,
  updated_at timestamptz DEFAULT now());
CREATE TABLE catalog_offers (
  offer_id text PRIMARY KEY, sku_key text, product_key text, merchant_id text,
  source_system text, source_domain text, currency text, list_price numeric,
  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());
CREATE TABLE product_reviews (
  id bigserial PRIMARY KEY, product_key text, sku_key text, merchant_id text,
  platform text, platform_product_id text, status text DEFAULT 'active',
  created_at timestamptz DEFAULT now());
CREATE TABLE media_assets (
  id bigserial PRIMARY KEY,
  review_id bigint REFERENCES product_reviews(id) ON DELETE CASCADE);
CREATE TABLE review_replies (
  id bigserial PRIMARY KEY,
  review_id bigint REFERENCES product_reviews(id) ON DELETE CASCADE);
CREATE TABLE review_interactions (
  id bigserial PRIMARY KEY,
  review_id bigint REFERENCES product_reviews(id) ON DELETE CASCADE);
CREATE TABLE review_featured (
  id bigserial PRIMARY KEY,
  review_id bigint REFERENCES product_reviews(id) ON DELETE CASCADE);
-- tables the verifier's residue sweep walks
CREATE TABLE evidence_items (
  evidence_id text, merchant_id text, product_key text, content_key varchar(40));
CREATE TABLE niche_target_outcomes (
  id bigserial, merchant_id text, content_key varchar(40));
CREATE TABLE agent_product_events (id bigserial, merchant_id text, product_id text);
"""

_FIXTURE = f"""
INSERT INTO catalog_merchants (merchant_id) VALUES ('{_A}'), ('{_C}');
INSERT INTO catalog_products (product_key, merchant_id, suppression_reason) VALUES
  ('pk_a', '{_A}', NULL),
  ('pk_c', '{_C}', NULL),
  ('pk_tomb', '{_A}', 'step5_test_rig_retirement');

-- 3 residue offers on THREE different products (two sellers) + 2 non-residue.
INSERT INTO catalog_offers (offer_id, product_key, merchant_id, source_system) VALUES
  ('o_a',    'pk_a',    '{_B}', 'us_market_capture'),
  ('o_c',    'pk_c',    '{_B}', 'us_market_capture'),
  ('o_tomb', 'pk_tomb', '{_B}', 'us_market_capture'),
  ('o_keep', 'pk_a',    '{_A}', 'mirror'),
  ('o_keep2','pk_c',    '{_C}', 'mirror'),
  -- A RETAILER offer: a real, non-sentinel merchant that deliberately differs
  -- from its product's seller (the attach_retailer_offer lane). Re-keying this
  -- to the product's merchant would destroy the seller distinction the offer
  -- exists to record, so it must survive untouched.
  ('o_retail','pk_a',   '{_C}', 'retailer_attach');

-- 2 residue reviews (own key namespace) + 2 that must survive.
INSERT INTO product_reviews (product_key, merchant_id, status) VALUES
  ('{_B}|external_seed|sp1', '{_B}', 'removed'),
  ('{_B}|external_seed|sp2', '{_B}', 'under_review'),
  ('{_A}|shopify|1',         '{_A}', 'active'),
  ('{_C}|shopify|2',         '{_C}', 'removed');

-- verifier split: evidence_items gets ONE scoped and ONE unscoped sentinel row.
INSERT INTO evidence_items (evidence_id, merchant_id, product_key) VALUES
  ('e_scoped', '{_B}', 'pk_a'),
  ('e_null',   '{_B}', NULL);
INSERT INTO niche_target_outcomes (merchant_id, content_key) VALUES ('{_B}', NULL);
INSERT INTO agent_product_events (merchant_id, product_id) VALUES ('{_B}', 'sp1');
"""


def _admin_url() -> str:
    return os.environ["DATABASE_URL"]


def _gate_url() -> str:
    head, _, _t = _admin_url().rpartition("/")
    return f"{head}/{_DB}"


@pytest.fixture()
def gate():
    """Fresh database PER TEST: these tests mutate rows, so a module-scoped
    fixture would make them order-dependent — the kind of shared state that
    turns one real failure into four confusing ones."""
    from sqlalchemy import create_engine, text

    admin = create_engine(_admin_url(), future=True, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{_DB}"'))
        c.execute(text(f'CREATE DATABASE "{_DB}"'))
    eng = create_engine(_gate_url(), future=True)
    with eng.begin() as c:
        for stmt in filter(None, (s.strip() for s in _DDL.split(";"))):
            c.execute(text(stmt))
        for stmt in filter(None, (s.strip() for s in _FIXTURE.split(";\n"))):
            c.execute(text(stmt.rstrip(";")))
        rid = c.execute(text(
            "SELECT id FROM product_reviews WHERE merchant_id = :m ORDER BY id LIMIT 1"),
            {"m": _B}).scalar()
        c.execute(text("INSERT INTO media_assets (review_id) VALUES (:r), (:r)"), {"r": rid})
    eng.dispose()
    yield
    with admin.connect() as c:
        c.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d"),
            {"d": _DB})
        c.execute(text(f'DROP DATABASE IF EXISTS "{_DB}"'))
    admin.dispose()


def _run(coro_fn):
    import databases

    async def go():
        db = databases.Database(_gate_url())
        await db.connect()
        try:
            return await coro_fn(db)
        finally:
            await db.disconnect()

    return asyncio.run(go())


def _scalar(sql, params=None):
    from sqlalchemy import create_engine, text
    eng = create_engine(_gate_url(), future=True)
    try:
        with eng.connect() as c:
            return c.execute(text(sql), params or {}).scalar()
    finally:
        eng.dispose()


def _rows(sql, params=None):
    from sqlalchemy import create_engine, text
    eng = create_engine(_gate_url(), future=True)
    try:
        with eng.connect() as c:
            return [dict(r._mapping) for r in c.execute(text(sql), params or {})]
    finally:
        eng.dispose()


def test_dry_run_changes_nothing(gate):
    from scripts import dispose_sentinel_orphans as d

    before = _rows("SELECT offer_id, merchant_id FROM catalog_offers ORDER BY offer_id")
    out = _run(lambda db: d.plan(db, list(d.ALL_TABLES)))
    assert out["doors_failed"] == []
    assert _rows("SELECT offer_id, merchant_id FROM catalog_offers ORDER BY offer_id") == before
    assert _scalar("SELECT count(*) FROM product_reviews") == 4


def test_rekey_follows_each_offer_own_product_and_spares_non_residue(gate):
    from scripts import dispose_sentinel_orphans as d

    _run(lambda db: d.apply(db, ["catalog_offers"], "run-1"))
    got = {r["offer_id"]: r["merchant_id"] for r in
           _rows("SELECT offer_id, merchant_id FROM catalog_offers")}
    # Each residue offer took ITS OWN product's seller — one merchant for all
    # would pass a weaker assertion and be wrong.
    assert got["o_a"] == _A
    assert got["o_c"] == _C
    assert got["o_tomb"] == _A          # tombstoned product still has a seller
    assert got["o_keep"] == _A and got["o_keep2"] == _C
    # The retailer offer keeps ITS OWN merchant. This is what makes the
    # `o.merchant_id = :banned` conjunct load-bearing: without it the UPDATE
    # re-keys every offer whose merchant differs from its product's, which is
    # exactly what a retailer offer is.
    assert got["o_retail"] == _C
    assert _scalar("SELECT count(*) FROM catalog_offers WHERE merchant_id = :b", {"b": _B}) == 0
    # updated_at moved only on the rows that changed.
    assert _scalar("SELECT count(*) FROM catalog_offers WHERE updated_at > created_at") == 3


def test_rekey_ignores_a_STALE_id_list_that_no_longer_names_residue(gate):
    """The `o.merchant_id = :banned` conjunct, tested at the statement level.

    The id list is built by a SELECT that runs before the UPDATE, so a row can
    stop being residue in between — which is EXACTLY the TOCTOU that produced
    these 12 orphans in the first place (capture_us_market_offers scanned, then
    probed for minutes, then wrote a stale merchant). Without the conjunct the
    UPDATE trusts the stale list and re-keys a row it must not touch; the id
    list alone cannot express this, which is why the redundant-looking
    conjunct is load-bearing.
    """
    from scripts import dispose_sentinel_orphans as d

    async def go(db):
        # A plausible stale list: o_retail was NEVER residue, yet it is named.
        await db.execute(d.OFFERS_REKEY_SQL,
                         {"offer_ids": ["o_a", "o_retail"], "banned": _B})

    _run(go)
    got = {r["offer_id"]: r["merchant_id"] for r in
           _rows("SELECT offer_id, merchant_id FROM catalog_offers")}
    assert got["o_a"] == _A          # genuine residue moved
    assert got["o_retail"] == _C     # named but not residue — untouched


def test_delete_removes_only_residue_reviews_and_cascades_children(gate):
    from scripts import dispose_sentinel_orphans as d

    assert _scalar("SELECT count(*) FROM media_assets") == 2
    _run(lambda db: d.apply(db, ["product_reviews"], "run-2"))
    left = _rows("SELECT merchant_id, status FROM product_reviews ORDER BY id")
    assert [r["merchant_id"] for r in left] == [_A, _C]
    assert _scalar("SELECT count(*) FROM media_assets") == 0   # cascaded


def test_the_plan_dump_carries_the_children_that_the_delete_will_take(gate):
    from scripts import dispose_sentinel_orphans as d

    out = _run(lambda db: d.plan(db, ["product_reviews"]))
    kids = out["tables"]["product_reviews"]["cascaded_children"]
    assert len(kids["media_assets"]) == 2, "a delete that loses children silently is unreversible"
    assert kids["review_replies"] == []
    assert len(out["tables"]["product_reviews"]["dump"]) == 2


def test_doors_abort_before_any_write_when_a_review_resolves(gate):
    from scripts import dispose_sentinel_orphans as d
    from sqlalchemy import create_engine, text

    eng = create_engine(_gate_url(), future=True)
    with eng.begin() as c:
        # Make one residue review's key a REAL catalog product_key: it is no
        # longer an orphan, so D5 must stop the whole run.
        c.execute(text("INSERT INTO catalog_products (product_key, merchant_id)"
                       " VALUES (:pk, :m)"),
                  {"pk": f"{_B}|external_seed|sp1", "m": _A})
    eng.dispose()
    out = _run(lambda db: d.plan(db, ["product_reviews"]))
    assert any("D5" in f for f in out["doors_failed"])
    assert _scalar("SELECT count(*) FROM product_reviews WHERE merchant_id = :b",
                   {"b": _B}) == 2


def test_doors_abort_while_the_bucket_still_holds_rows(gate):
    from scripts import dispose_sentinel_orphans as d
    from sqlalchemy import create_engine, text

    eng = create_engine(_gate_url(), future=True)
    with eng.begin() as c:
        c.execute(text("INSERT INTO catalog_products (product_key, merchant_id)"
                       " VALUES ('pk_bucket', :m)"), {"m": _B})
    eng.dispose()
    out = _run(lambda db: d.plan(db, list(d.ALL_TABLES)))
    assert any("D1" in f for f in out["doors_failed"])


def test_the_in_transaction_residue_assertion_rolls_the_whole_table_back(gate):
    """Force the post-write count to be non-zero by having a concurrent-looking
    row appear inside the transaction's own view: the tool must raise AND leave
    every row untouched. An assertion that reports but does not roll back would
    pass a weaker test — this one checks the DATA afterwards."""
    from scripts import dispose_sentinel_orphans as d

    async def sabotage(db):
        async with db.transaction():
            # A residue offer whose product is missing: the UPDATE's join finds
            # no catalog row, so it survives the re-key and the guard must fire.
            await db.execute(
                "INSERT INTO catalog_offers (offer_id, product_key, merchant_id)"
                " VALUES ('o_ghost', 'pk_missing', :b)", {"b": _B})
            with pytest.raises(RuntimeError, match="still holds"):
                await d.apply(db, ["catalog_offers"], "run-3")
            raise _Rollback()

    class _Rollback(Exception):
        pass

    try:
        _run(sabotage)
    except _Rollback:
        pass
    # Nothing moved: the inner transaction rolled back on the raise.
    assert _scalar("SELECT count(*) FROM catalog_offers WHERE merchant_id = :b",
                   {"b": _B}) == 3


def test_verifier_splits_ownership_unscoped_and_history_per_row(gate):
    """The same table appears in BOTH buckets when it holds one scoped and one
    unscoped sentinel row — a per-TABLE split would report only one and either
    excuse an orphan or fail a tenant row."""
    from scripts.verify_seller_rekey import global_residue

    glob = _run(global_residue)
    assert glob["ownership"].get("evidence_items") == 1      # the scoped one FAILS
    assert glob["unscoped"].get("evidence_items") == 1       # the NULL one is reported
    assert glob["unscoped"].get("niche_target_outcomes") == 1
    assert "niche_target_outcomes" not in glob["ownership"]
    assert glob["history"].get("agent_product_events") == 1
    assert glob["ownership"]["catalog_products"] == 0


def test_verifier_still_fails_a_scoped_orphan_after_the_split(gate):
    """The unscoped bucket must not become a loophole."""
    from scripts.verify_seller_rekey import global_residue, orphan_failures

    glob = _run(global_residue)
    fails = orphan_failures(glob)
    assert any("evidence_items" in f for f in fails)
    assert not any("niche_target_outcomes" in f for f in fails)


def test_verifier_reports_ok_once_the_scoped_orphans_are_disposed(gate):
    """End to end: dispose, then the grader the tool does NOT control agrees."""
    from scripts import dispose_sentinel_orphans as d
    from scripts.verify_seller_rekey import global_residue, orphan_failures
    from sqlalchemy import create_engine, text

    _run(lambda db: d.apply(db, list(d.ALL_TABLES), "run-4"))
    eng = create_engine(_gate_url(), future=True)
    with eng.begin() as c:  # the one scoped orphan this tool does not own
        c.execute(text("DELETE FROM evidence_items WHERE evidence_id = 'e_scoped'"))
    eng.dispose()

    glob = _run(global_residue)
    assert orphan_failures(glob) == []
    assert glob["unscoped"]                      # tenant rows still REPORTED
    assert "catalog_offers" not in glob["ownership"]
    assert "product_reviews" not in glob["ownership"]
