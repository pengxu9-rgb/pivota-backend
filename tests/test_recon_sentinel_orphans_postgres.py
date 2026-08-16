"""scripts/recon_sentinel_orphans.py against a REAL Postgres — production dialect.

WHY THIS FILE EXISTS. The fake-DB suite pins control flow (which tables get
classified, which inputs abort) but answers every classification query with a
canned row, so it constrains nothing about what those queries COMPUTE. Review
(2026-08-16) mutated the script 23 ways and 12 mutants survived the fake suite
— including flipping `WHERE merchant_id = :banned` to `<>`, which would
classify the entire NON-residue population and still print a JSON document of
exactly the right shape. A recon whose numbers the founder gates a production
disposition on must have its numbers tested.

Every assertion below is checked by MUTATION: break the thing it claims to
protect and it goes red. The mutants it kills, in the order they appear:
  = vs <> on the residue filter        (both by_source_id classifiers)
  the product_key join key             (product_exists / current_merchant)
  the checkpoint phase conjunct + the CHECKPOINT_PHASE constant
  scope_null's FILTER polarity
  the scope-column priority tuple
  the catalog_products exclusion
  LIMIT :lim, and the sample ORDER BY
  the _ident() CALL SITES (not just the helper in isolation)
  the seed-attachment "unsuppressed" conjunct
  jsonb bullets: '[]' is not content

A DEDICATED DATABASE, not a schema. The repo's other Postgres gates own a
per-process SCHEMA, but this script reads `information_schema` filtered to
`table_schema = 'public'` (inherited from the verifier it grades alongside),
so a non-public schema is invisible to it and the suite would pass vacuously.
Per-process database keeps concurrent runs isolated the same way.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_DB = f"recon_gate_{os.getpid()}"
_BANNED = "external_seed"
_REAL = "merch_obs_real"
_OTHER = "merch_obs_other"

# Tables the script's queries name directly, plus residue tables covering every
# scope shape. The un-identifier-like table lives only inside the poison test:
# the verifier's own global_residue would break on it unguarded, which is
# precisely the ordering the guard has to win.
_DDL = """
CREATE TABLE catalog_merchants (merchant_id text, indexable boolean, status text);
CREATE TABLE catalog_products (
  product_key text PRIMARY KEY, content_key varchar(40), merchant_id text,
  source_product_id text, platform text, suppression_reason text,
  suppressed_at timestamptz, source_system text, updated_at timestamptz DEFAULT now());
CREATE TABLE catalog_skus (sku_key text PRIMARY KEY, product_key text);
CREATE TABLE a9_4_backfill_checkpoint (
  phase text, ref_id text, observed_id text, previous_value text, status text,
  updated_at timestamptz, PRIMARY KEY (phase, ref_id));
CREATE TABLE external_product_seeds (
  id text, attached_product_key text, status text, domain text,
  external_product_id text, destination_url text, updated_at timestamptz);
CREATE TABLE products_cache (merchant_id text, platform text, platform_product_id text);
CREATE TABLE pdp_identity_listing (
  source_listing_ref text, product_id text, merchant_id text, identity_status text,
  live_read_enabled boolean, created_at timestamptz DEFAULT now());
CREATE TABLE pdp_identity_override (id text, source_listing_ref text);
CREATE TABLE pdp_identity_review_queue (id text, source_listing_ref text);
CREATE TABLE product_enrichment (
  merchant_id varchar(100), platform varchar(50), platform_product_id varchar(200),
  geo_code varchar(16) DEFAULT 'default', bullet_points json, description_markdown text,
  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());
CREATE TABLE catalog_offers (
  offer_id text, product_key text, sku_key text, merchant_id text,
  created_at timestamptz DEFAULT now());
CREATE TABLE index_pipeline_state (
  content_key text PRIMARY KEY, merchant_id text, serving_eligible boolean,
  updated_at timestamptz DEFAULT now());
CREATE TABLE niche_target_outcomes (
  id bigserial, merchant_id text, normalized_query text, content_key varchar(40),
  seen_at timestamptz DEFAULT now());
CREATE TABLE product_reviews (
  id bigserial, product_key text, sku_key text, merchant_id text, platform text,
  platform_product_id text, created_at timestamptz DEFAULT now());
CREATE TABLE sku_scoped_thing (sku_key text, merchant_id text, created_at timestamptz DEFAULT now());
CREATE TABLE both_scopes_thing (product_key text, content_key varchar(40), merchant_id text);
CREATE TABLE no_time_thing (product_key text, merchant_id text);
CREATE TABLE int_merchant_thing (merchant_id integer, product_key text);
CREATE TABLE agent_product_events (id bigserial, merchant_id text, product_id text);
"""

# pk_moved  : product exists, moved off the bucket, checkpointed 'catalog'
# pk_tomb   : product exists under a seller but TOMBSTONED
# pk_seeds  : product exists, checkpointed under phase 'seeds' only
# pk_gone   : referenced by dependents, no catalog row at all
_FIXTURE = f"""
INSERT INTO catalog_products
  (product_key, content_key, merchant_id, source_product_id, platform, suppression_reason) VALUES
  ('pk_moved', 'ck_moved', '{_REAL}',  'sp_moved', 'external_seed', NULL),
  ('pk_tomb',  'ck_tomb',  '{_OTHER}', 'sp_tomb',  'external_seed', 'step5_test_rig_retirement'),
  ('pk_seeds', 'ck_seeds', '{_REAL}',  'sp_seeds', 'external_seed', NULL),
  ('pk_bucket','ck_bucket','{_BANNED}','sp_bucket','external_seed', NULL);
INSERT INTO catalog_skus (sku_key, product_key) VALUES
  ('sku_moved', 'pk_moved'), ('sku_tomb', 'pk_tomb');
INSERT INTO a9_4_backfill_checkpoint (phase, ref_id, observed_id, status) VALUES
  ('catalog', 'pk_moved', '{_REAL}',  'done'),
  ('seeds',   'pk_seeds', '{_REAL}',  'done');

-- catalog_offers: 3 residue rows (moved / tombstoned / gone) + 1 NON-residue.
INSERT INTO catalog_offers (offer_id, product_key, sku_key, merchant_id) VALUES
  ('o1', 'pk_moved', 'sku_moved', '{_BANNED}'),
  ('o2', 'pk_tomb',  'sku_tomb',  '{_BANNED}'),
  ('o3', 'pk_gone',  'sku_gone',  '{_BANNED}'),
  ('o4', 'pk_moved', 'sku_moved', '{_REAL}');

-- index_pipeline_state: content_key scope (the column is this table's PK in
-- prod, so a NULL-scope row is impossible here; niche_target_outcomes carries
-- the NULL-scope case).
INSERT INTO index_pipeline_state (content_key, merchant_id) VALUES
  ('ck_moved', '{_BANNED}'), ('ck_gone', '{_BANNED}');

-- niche_target_outcomes: tenant rows, scope NULL on every one.
INSERT INTO niche_target_outcomes (merchant_id, normalized_query, content_key) VALUES
  ('{_BANNED}', 'q1', NULL), ('{_BANNED}', 'q2', NULL);

-- product_reviews: keys in its OWN namespace, so nothing resolves to catalog.
INSERT INTO product_reviews (product_key, sku_key, merchant_id, platform, platform_product_id) VALUES
  ('{_BANNED}|external_seed|sp_moved', '{_BANNED}|external_seed|sp_moved|x',
   '{_BANNED}', 'external_seed', 'sp_moved');

INSERT INTO sku_scoped_thing (sku_key, merchant_id) VALUES
  ('sku_moved', '{_BANNED}'), ('sku_gone', '{_BANNED}');
INSERT INTO both_scopes_thing (product_key, content_key, merchant_id) VALUES
  ('pk_moved', 'ck_moved', '{_BANNED}');
INSERT INTO no_time_thing (product_key, merchant_id) VALUES
  ('pk_moved', '{_BANNED}'), ('pk_gone', '{_BANNED}');
INSERT INTO int_merchant_thing (merchant_id, product_key) VALUES (7, 'pk_moved');
INSERT INTO agent_product_events (merchant_id, product_id) VALUES ('{_BANNED}', 'sp_moved');

-- pdp_identity_listing: 2 residue + 1 NON-residue (kills the = -> <> mutant).
--   l_moved : seed ACTIVE, attached to the LIVE product        -> unsuppressed 1
--   l_tomb  : seed ACTIVE, attached to the TOMBSTONED product  -> unsuppressed 0
INSERT INTO pdp_identity_listing
  (source_listing_ref, product_id, merchant_id, identity_status, live_read_enabled) VALUES
  ('{_BANNED}:sp_moved', 'sp_moved', '{_BANNED}', 'approved', true),
  ('{_BANNED}:sp_tomb',  'sp_tomb',  '{_BANNED}', 'review_required', false),
  ('{_REAL}:sp_moved',   'sp_moved', '{_REAL}',   'approved', true);
INSERT INTO external_product_seeds (id, attached_product_key, status, external_product_id) VALUES
  ('s_moved', 'pk_moved', 'active', 'sp_moved'),
  ('s_tomb',  'pk_tomb',  'active', 'sp_tomb');
INSERT INTO pdp_identity_override (id, source_listing_ref) VALUES ('ov1', '{_BANNED}:sp_moved');
INSERT INTO pdp_identity_review_queue (id, source_listing_ref) VALUES ('q1', '{_BANNED}:sp_tomb');

-- product_enrichment: 3 residue + 1 NON-residue.
--   sp_moved : real bullets, target VACANT, no cache row       -> H0-eligible
--   sp_tomb  : bullets '[]' (NOT content), target OCCUPIED
--   sp_cache : blocked by a products_cache row under the bucket
INSERT INTO product_enrichment
  (merchant_id, platform, platform_product_id, bullet_points, description_markdown) VALUES
  ('{_BANNED}', 'external_seed', 'sp_moved', '["a","b"]'::json, NULL),
  ('{_BANNED}', 'external_seed', 'sp_tomb',  '[]'::json, NULL),
  ('{_BANNED}', 'external_seed', 'sp_cache', '["c"]'::json, NULL),
  ('{_OTHER}',  'external_seed', 'sp_tomb',  '["x"]'::json, NULL);
INSERT INTO products_cache (merchant_id, platform, platform_product_id) VALUES
  ('{_BANNED}', 'external_seed', 'sp_cache');
"""


def _admin_url() -> str:
    return os.environ["DATABASE_URL"]


def _gate_url() -> str:
    base = _admin_url()
    head, _, _tail = base.rpartition("/")
    return f"{head}/{_DB}"


@pytest.fixture(scope="module")
def gate_db():
    from sqlalchemy import create_engine, text

    admin = create_engine(_admin_url(), future=True, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{_DB}"'))
        c.execute(text(f'CREATE DATABASE "{_DB}"'))
    gate = create_engine(_gate_url(), future=True)
    with gate.begin() as c:
        for stmt in filter(None, (s.strip() for s in _DDL.split(";"))):
            c.execute(text(stmt))
        for stmt in filter(None, (s.strip() for s in _FIXTURE.split(";\n"))):
            c.execute(text(stmt.rstrip(";")))
    gate.dispose()
    yield
    with admin.connect() as c:
        c.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d"),
            {"d": _DB})
        c.execute(text(f'DROP DATABASE IF EXISTS "{_DB}"'))
    admin.dispose()


def _run(also=("pdp_identity_listing", "product_enrichment"), sample=10, poison=False):
    """Run recon() against the gate database and return its JSON dict."""
    import databases

    from scripts import recon_sentinel_orphans as recon

    async def go():
        db = databases.Database(_gate_url())
        await db.connect()
        try:
            if poison:
                await db.execute(
                    'CREATE TABLE "weird tbl x" (product_key text, merchant_id text)')
                await db.execute(
                    'INSERT INTO "weird tbl x" (product_key, merchant_id) VALUES (:p, :m)',
                    {"p": "pk_moved", "m": _BANNED})
            return await recon.recon(db, also_history=list(also), sample=sample)
        finally:
            if poison:
                await db.execute('DROP TABLE "weird tbl x"')
            await db.disconnect()

    return asyncio.run(go())


@pytest.fixture(scope="module")
def report(gate_db):
    return _run()


def _one(rows, **match):
    hits = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
    assert len(hits) == 1, f"expected exactly one row matching {match}, got {hits}"
    return hits[0]


def test_only_residue_rows_are_classified_and_the_bucket_row_is_not_a_target(report):
    """`= :banned`, not `<>`; and catalog_products is graded by the verifier,
    never classified here — even though it carries a sentinel row."""
    assert report["catalog_products_under_sentinel"] == 1
    assert "catalog_products" not in report["tables"]
    assert "int_merchant_thing" not in report["tables"]      # INTEGER seller column
    assert "agent_product_events" not in report["tables"]    # history, not named
    # Every classified table's row count equals its residue count exactly. If
    # the filter flipped to `<>`, offers would report the one non-residue row.
    offers = report["tables"]["catalog_offers"]
    assert offers["residue"] == 3
    assert sum(r["n"] for r in offers["by_product"]) == 3
    listing = report["tables"]["pdp_identity_listing"]
    assert listing["residue"] == 2
    assert sum(r["n"] for r in listing["by_source_id"]) == 2
    enrich = report["tables"]["product_enrichment"]
    assert enrich["residue"] == 3
    assert sum(r["n"] for r in enrich["by_source_id"]) == 3


def test_by_product_resolves_existence_owner_tombstone_and_the_catalog_phase(report):
    """The join is on product_key, the checkpoint conjunct pins phase
    'catalog', and a 'seeds'-phase checkpoint must not be read as moved."""
    rows = report["tables"]["catalog_offers"]["by_product"]
    moved = _one(rows, product_exists=True, current_merchant="merch_obs_real")
    assert moved["checkpoint_status"] == "done"
    assert moved["checkpoint_target"] == "merch_obs_real"
    assert moved["product_tombstoned"] is False

    tomb = _one(rows, current_merchant="merch_obs_other")
    assert tomb["product_tombstoned"] is True
    assert tomb["tombstone_reason"] == "step5_test_rig_retirement"
    assert tomb["checkpoint_status"] is None      # never checkpointed

    gone = _one(rows, product_exists=False)
    assert gone["current_merchant"] is None and gone["checkpoint_status"] is None

    # `no_time_thing` references pk_seeds' sibling only through pk_moved/pk_gone,
    # so use both_scopes_thing to pin the phase: pk_moved is 'catalog'-checkpointed.
    both = report["tables"]["both_scopes_thing"]["by_product"]
    assert _one(both, product_exists=True)["checkpoint_status"] == "done"


def test_a_checkpoint_under_another_phase_is_not_read_as_moved(gate_db):
    """Kills both the dropped phase conjunct and CHECKPOINT_PHASE = 'seeds'.
    pk_seeds is checkpointed ONLY under phase 'seeds'."""
    import databases

    from scripts import recon_sentinel_orphans as recon

    async def go():
        db = databases.Database(_gate_url())
        await db.connect()
        try:
            await db.execute(
                "INSERT INTO catalog_offers (offer_id, product_key, merchant_id)"
                " VALUES (:o, :p, :m)",
                {"o": "o_seeds", "p": "pk_seeds", "m": _BANNED})
            out = await recon.recon(db, also_history=[], sample=5)
            return out["tables"]["catalog_offers"]["by_product"]
        finally:
            await db.execute("DELETE FROM catalog_offers WHERE offer_id = :o",
                             {"o": "o_seeds"})
            await db.disconnect()

    row = _one(asyncio.run(go()), product_exists=True, current_merchant="merch_obs_real",
               product_tombstoned=False, checkpoint_status=None)
    assert row["n"] == 1  # pk_seeds: exists, moved, but NOT catalog-checkpointed


def test_scope_nulls_counts_nulls_and_the_key_space_signal_flags_a_foreign_namespace(report):
    """FILTER polarity, and the signal that stops a namespace mismatch reading
    as 'the product was deleted'."""
    niche = report["tables"]["niche_target_outcomes"]
    assert niche["scope_nulls"] == {"total": 2, "scope_null": 2}
    assert niche["scope_key_space"]["resolves_to_catalog"] == 0
    assert niche["scope_key_space"]["keys_are_catalog_keys"] is False

    ips = report["tables"]["index_pipeline_state"]
    assert ips["scope_nulls"] == {"total": 2, "scope_null": 0}
    assert ips["scope_key_space"]["resolves_to_catalog"] == 1   # ck_moved only

    # product_reviews keys `merchant|platform|id` — real product, zero resolution.
    rev = report["tables"]["product_reviews"]
    assert rev["scope_nulls"]["scope_null"] == 0
    assert rev["scope_key_space"] == {
        "total": 1, "scoped": 1, "resolves_to_catalog": 0, "keys_are_catalog_keys": False}

    offers = report["tables"]["catalog_offers"]["scope_key_space"]
    assert offers["resolves_to_catalog"] == 2 and offers["keys_are_catalog_keys"] is True


def test_scope_column_priority_prefers_product_key_over_content_key(report):
    """both_scopes_thing carries both; product_key is the finer scope and the
    order the flip itself uses."""
    assert report["tables"]["both_scopes_thing"]["scope_col"] == "product_key"
    assert "by_product" in report["tables"]["both_scopes_thing"]
    assert report["tables"]["index_pipeline_state"]["scope_col"] == "content_key"
    assert report["tables"]["sku_scoped_thing"]["scope_col"] == "sku_key"


def test_by_sku_walks_skus_to_products(report):
    rows = report["tables"]["sku_scoped_thing"]["by_sku"]
    assert sum(r["n"] for r in rows) == 2
    live = _one(rows, sku_exists=True)
    assert live["product_exists"] is True and live["current_merchant"] == "merch_obs_real"
    assert _one(rows, sku_exists=False)["product_exists"] is False


def test_listing_seed_attachment_requires_an_UNSUPPRESSED_product(report):
    """A seed attached to a tombstoned product is not 'live'. Both listings
    have an ACTIVE attached seed; only one product is servable."""
    rows = report["tables"]["pdp_identity_listing"]["by_source_id"]
    moved = _one(rows, identity_status="approved")
    assert moved["n_seeds_active"] == 1
    assert moved["n_seeds_attached_unsuppressed"] == 1
    assert moved["n_products"] == 1 and moved["merchants"] == ["merch_obs_real"]
    assert moved["has_overrides"] is True and moved["has_queue"] is False

    tomb = _one(rows, identity_status="review_required")
    assert tomb["n_seeds_active"] == 1
    assert tomb["n_seeds_attached_unsuppressed"] == 0   # the whole point
    assert tomb["has_queue"] is True


def test_enrichment_classifies_against_the_reattribution_tools_preconditions(report):
    """An empty bullet array is not content; a products_cache row blocks the
    orphan guard; an occupied target blocks the apply."""
    rows = report["tables"]["product_enrichment"]["by_source_id"]
    eligible = _one(rows, has_content=True, target_occupied=False, n_cache_rows=0)
    assert eligible["n"] == 1
    assert eligible["merchants_any_platform"] == ["merch_obs_real"]

    empty = _one(rows, has_content=False)
    assert empty["target_occupied"] is True   # merch_obs_other already holds sp_tomb

    blocked = _one(rows, n_cache_rows=1)
    assert blocked["has_content"] is True and blocked["target_occupied"] is False


def test_sample_honours_the_limit_and_reports_whether_it_is_ordered(gate_db):
    small = _run(sample=1)
    assert len(small["tables"]["catalog_offers"]["sample"]["rows"]) == 1
    assert small["tables"]["catalog_offers"]["sample"]["ordered_by"] == "created_at"
    big = _run(sample=10)
    assert len(big["tables"]["catalog_offers"]["sample"]["rows"]) == 3
    # A table with no known timestamp column is explicitly UNORDERED, not
    # silently "the newest N".
    assert big["tables"]["no_time_thing"]["sample"]["ordered_by"] is None
    assert len(big["tables"]["no_time_thing"]["sample"]["rows"]) == 2


def test_an_unquotable_table_name_aborts_before_it_is_interpolated(gate_db):
    """Kills the _ident() CALL SITES, not just the helper. Without the guard
    the name reaches the SQL and asyncpg raises a syntax error instead — a
    different failure, and one that says nothing about identifier safety."""
    from scripts import recon_sentinel_orphans as recon

    with pytest.raises(RuntimeError, match="refusing to interpolate"):
        _run(poison=True)
    assert recon._ident("catalog_offers") == "catalog_offers"
