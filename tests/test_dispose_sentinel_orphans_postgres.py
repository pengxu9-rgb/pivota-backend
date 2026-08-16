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
-- The FKs a hardcoded "migration 040" list misses (review 2026-08-17).
CREATE TABLE buyer_review_ownership (
  id bigserial PRIMARY KEY,
  review_id bigint REFERENCES product_reviews(id) ON DELETE CASCADE);
CREATE TABLE buyer_review_idempotency_keys (
  id bigserial PRIMARY KEY, idem_key text,
  review_id bigint REFERENCES product_reviews(id) ON DELETE SET NULL);
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


async def _apply(db, tables, run_id):
    """apply() consumes a plan — never a fresh read (review 2026-08-17)."""
    from scripts import dispose_sentinel_orphans as d

    p = await d.plan(db, tables)
    assert p["doors_failed"] == [], p["doors_failed"]
    return await d.apply(db, tables, run_id, p)


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

    _run(lambda db: _apply(db, ["catalog_offers"], "run-1"))
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
    _run(lambda db: _apply(db, ["product_reviews"], "run-2"))
    left = _rows("SELECT merchant_id, status FROM product_reviews ORDER BY id")
    assert [r["merchant_id"] for r in left] == [_A, _C]
    assert _scalar("SELECT count(*) FROM media_assets") == 0   # cascaded


def test_the_plan_dump_carries_every_fk_child_and_the_full_row(gate):
    """The dump is the ONLY reversal record for a hard DELETE. Asserting a row
    COUNT would pass on a dump that recorded nothing but ids — the mutant that
    degraded the dump to {"id": ...} survived exactly that (review 2026-08-17).
    So: every FK child the graph names, and every column of the parent rows."""
    from scripts import dispose_sentinel_orphans as d
    from sqlalchemy import create_engine, text

    eng = create_engine(_gate_url(), future=True)
    with eng.begin() as c:
        rid = c.execute(text("SELECT id FROM product_reviews WHERE merchant_id = :m"
                             " ORDER BY id LIMIT 1"), {"m": _B}).scalar()
        c.execute(text("INSERT INTO buyer_review_ownership (review_id) VALUES (:r)"), {"r": rid})
        c.execute(text("INSERT INTO buyer_review_idempotency_keys (idem_key, review_id)"
                       " VALUES ('k1', :r)"), {"r": rid})
    eng.dispose()

    out = _run(lambda db: d.plan(db, ["product_reviews"]))
    rev = out["tables"]["product_reviews"]
    kids = rev["cascaded_children"]
    # Derived from the FK graph, so the two tables a migration-040 list misses
    # are present, and the SET NULL parent is labelled as such.
    assert {"media_assets", "review_replies", "review_interactions", "review_featured",
            "buyer_review_ownership", "buyer_review_idempotency_keys"} <= set(kids)
    assert len(kids["media_assets"]["rows"]) == 2
    assert len(kids["buyer_review_ownership"]["rows"]) == 1
    assert kids["media_assets"]["on_delete"] == "cascade"
    assert kids["buyer_review_idempotency_keys"]["on_delete"] == "set null"
    assert len(kids["buyer_review_idempotency_keys"]["rows"]) == 1
    # Full parent rows, not a count and not just ids.
    assert len(rev["dump"]) == 2
    for row in rev["dump"]:
        assert {"id", "product_key", "merchant_id", "status"} <= set(row)


def test_the_delete_takes_the_children_the_dump_recorded(gate):
    from scripts import dispose_sentinel_orphans as d
    from sqlalchemy import create_engine, text

    eng = create_engine(_gate_url(), future=True)
    with eng.begin() as c:
        rid = c.execute(text("SELECT id FROM product_reviews WHERE merchant_id = :m"
                             " ORDER BY id LIMIT 1"), {"m": _B}).scalar()
        c.execute(text("INSERT INTO buyer_review_ownership (review_id) VALUES (:r)"), {"r": rid})
        c.execute(text("INSERT INTO buyer_review_idempotency_keys (idem_key, review_id)"
                       " VALUES ('k1', :r)"), {"r": rid})
    eng.dispose()

    _run(lambda db: _apply(db, ["product_reviews"], "run-kids"))
    assert _scalar("SELECT count(*) FROM buyer_review_ownership") == 0     # cascaded
    # SET NULL: the row SURVIVES with a cleared FK, which is why the dump has
    # to record it — a re-insert of the parent does not restore this link.
    assert _scalar("SELECT count(*) FROM buyer_review_idempotency_keys") == 1
    assert _scalar("SELECT review_id FROM buyer_review_idempotency_keys") is None


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


def test_the_residue_guard_rolls_the_table_back_in_its_OWN_transaction(gate):
    """The guard must roll back on its own, with NO outer transaction helping.

    The first version of this test wrapped apply() in its own transaction, which
    turned apply()'s transaction into a savepoint and let the outer rollback hide
    whether an inner one existed at all — a mutant that ran the write with no
    transaction survived it (review 2026-08-17). Here apply() is called at top
    level: if it does not own a transaction, the partial write COMMITS and the
    row counts below change.
    """
    from scripts import dispose_sentinel_orphans as d
    from sqlalchemy import create_engine, text

    # A residue offer whose product does not exist: the re-key's join finds no
    # catalog row, so it survives the UPDATE and the zero-residue guard fires.
    # It is inserted AFTER the plan, so the plan/current mismatch check is what
    # trips first — both paths must roll back.
    eng = create_engine(_gate_url(), future=True)
    with eng.begin() as c:
        c.execute(text("INSERT INTO catalog_offers (offer_id, product_key, merchant_id)"
                       " VALUES ('o_ghost', 'pk_missing', :b)"), {"b": _B})
    eng.dispose()

    async def go(db):
        p = await d.plan(db, ["catalog_offers"])
        # o_ghost IS genuine residue (so the plan/current sets agree), but its
        # product does not exist, so the re-key's join skips it and it is STILL
        # residue after the UPDATE — which is what the zero-residue guard is
        # for. Only D2's verdict is dropped, so the guard is what fires.
        assert any("D2" in f for f in p["doors_failed"])
        p["doors_failed"] = []
        with pytest.raises(RuntimeError, match="still holds"):
            await d.apply(db, ["catalog_offers"], "run-3", p)

    _run(go)
    # The guard rolled back the WHOLE table: the three good offers, which the
    # UPDATE did move inside the transaction, are back on the sentinel. Without
    # a transaction they would have committed and this reads 1.
    assert _scalar("SELECT count(*) FROM catalog_offers WHERE merchant_id = :b",
                   {"b": _B}) == 4
    for oid in ("o_a", "o_c", "o_tomb"):
        assert _scalar("SELECT merchant_id FROM catalog_offers WHERE offer_id = :o",
                       {"o": oid}) == _B


def test_the_rekey_skips_an_offer_whose_product_is_still_on_the_sentinel(gate):
    """`cp.merchant_id <> :banned`, at the statement level.

    Re-keying a bucket row onto the bucket is a no-op in value but still a
    WRITE: it bumps updated_at and reports rows-affected, so a run could look
    like it moved something it did not. The conjunct makes the statement skip
    it outright."""
    from scripts import dispose_sentinel_orphans as d
    from sqlalchemy import create_engine, text

    eng = create_engine(_gate_url(), future=True)
    with eng.begin() as c:
        c.execute(text("INSERT INTO catalog_products (product_key, merchant_id)"
                       " VALUES ('pk_still_bucket', :b)"), {"b": _B})
        c.execute(text("INSERT INTO catalog_offers (offer_id, product_key, merchant_id,"
                       " created_at, updated_at) VALUES ('o_bucket', 'pk_still_bucket', :b,"
                       " now(), now())"), {"b": _B})
    eng.dispose()

    async def go(db):
        await db.execute(d.OFFERS_REKEY_SQL, {"offer_ids": ["o_bucket"], "banned": _B})

    _run(go)
    assert _scalar("SELECT merchant_id FROM catalog_offers WHERE offer_id = 'o_bucket'") == _B
    assert _scalar("SELECT (updated_at > created_at) FROM catalog_offers"
                   " WHERE offer_id = 'o_bucket'") is False, "the row was rewritten"


def test_apply_refuses_a_population_that_changed_since_the_plan(gate):
    """THE CORE FIX (review 2026-08-17). apply() used to re-read the population,
    so a row that became residue after the plan was written with NO door having
    examined it and NO dump recording it. Now the plan's id set is binding."""
    from scripts import dispose_sentinel_orphans as d
    from sqlalchemy import create_engine, text

    async def go(db):
        p = await d.plan(db, ["product_reviews"])
        assert p["doors_failed"] == []
        planned = {r["id"] for r in p["tables"]["product_reviews"]["dump"]}
        # A review that would have FAILED D5 and D6 arrives after the plan.
        eng = create_engine(_gate_url(), future=True)
        with eng.begin() as c:
            c.execute(text("INSERT INTO catalog_products (product_key, merchant_id)"
                           " VALUES ('pk_live', :m)"), {"m": _A})
            c.execute(text("INSERT INTO product_reviews (product_key, merchant_id, status)"
                           " VALUES ('pk_live', :b, 'active')"), {"b": _B})
        eng.dispose()
        with pytest.raises(RuntimeError, match="residue changed"):
            await d.apply(db, ["product_reviews"], "run-race", p)
        return planned

    _run(go)
    # The late arrival — serving, resolving, never dumped — is still there,
    # and so is everything else.
    assert _scalar("SELECT count(*) FROM product_reviews WHERE merchant_id = :b",
                   {"b": _B}) == 3
    assert _scalar("SELECT count(*) FROM product_reviews WHERE status = 'active'"
                   " AND merchant_id = :b", {"b": _B}) == 1


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

    _run(lambda db: _apply(db, list(d.ALL_TABLES), "run-4"))
    eng = create_engine(_gate_url(), future=True)
    with eng.begin() as c:  # the one scoped orphan this tool does not own
        c.execute(text("DELETE FROM evidence_items WHERE evidence_id = 'e_scoped'"))
    eng.dispose()

    glob = _run(global_residue)
    assert orphan_failures(glob) == []
    assert glob["unscoped"]                      # tenant rows still REPORTED
    assert "catalog_offers" not in glob["ownership"]
    assert "product_reviews" not in glob["ownership"]
