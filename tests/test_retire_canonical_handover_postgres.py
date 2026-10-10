"""The retire's canonical handover, executed on Postgres: the real SQL of both the retire and the election.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml` globs `tests/test_*_postgres.py`).

What only Postgres proves:
- the guarded writes report through RETURNING (`databases` gives no rowcount on asyncpg) and roll the whole retire
  back when they miss;
- NAME_KEEPER_SQL's jsonb merge produces the pointer that the election's real KEEPER_SIGS_SQL joins on;
- the sweep that follows plans NO write, both after the retire and after its revert. That is the
  "no URL churn" property: real KEEPER_SIGS_SQL, the stored election and the real `plan_elections`.

The fixture is the Tower 28 shape measured on prod on 2026-10-10. OLD ("Tower 28 Beauty") holds the election as a
step-5 keeper. T is a 07-10 same-URL tombstone whose keeper_product_key names OLD. NEW ("Tower 28") is the re-run's
row on the same content_key.

Isolation: a PRIVATE schema, dropped afterwards, holding ONLY these tables. The gate files share one database
(tests/test_canonical_feed_tombstoned_flag_postgres.py), so catalog_products is built from the db.catalog MODEL and
content_canonical_election from its own migration (181). catalog_row_trust and external_product_seeds are minimal
stubs, but only inside the private schema.
"""
import glob
import json
import os
from urllib.parse import urlsplit

import pytest

from scripts import retire_superseded_brand_keys as tool
from services.content_canonical_election import (
    KEEPER_SIGS_FOR_CONTENT_KEYS_SQL,
    KEEPER_SIGS_SQL,
    REASON_DEDUPE_KEEPER,
    plan_elections,
)

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="needs a Postgres DATABASE_URL")
_SCHEMA = f"retire_handover_{os.getpid()}"
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_")

CK = "ck_cheeky"
OLD, NEW, T = "ext:tower-28-beauty-the-cheeky-duo::46e75de5", "ext:tower-28-the-cheeky-duo::6508df56", "t_seed"
OLD_SIG, NEW_SIG, T_SIG = "sig_26b604a83751dcf88b18c79da81a7716", "sig_6afee0ba02f17d5646215bf12a51df87", "sig_6e1a"
OTHER = "sig_ffffffffffffffffffffffffffffffff"


@pytest.fixture
async def db(monkeypatch):
    import asyncpg
    import databases
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.catalog import catalog_products
    from db.sql_migrations import split_statements

    dbname = urlsplit(URL).path.rsplit("/", 1)[-1]
    if not any(m in dbname for m in _SAFE_DB_MARKERS):
        pytest.skip("scratch-schema tests require a test database")
    ddl = [str(CreateTable(catalog_products).compile(dialect=postgresql.dialect()))]
    ddl += split_statements(open(glob.glob("db/migrations/181_content_canonical_election.sql")[0]).read())
    ddl += ["""CREATE TABLE catalog_row_trust (subject_type TEXT NOT NULL, subject_key TEXT NOT NULL,
                                              product_key TEXT, serving_decision TEXT NOT NULL,
                                              PRIMARY KEY (subject_type, subject_key))""",
            """CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, status TEXT, attached_product_key TEXT,
                                                   updated_at TIMESTAMPTZ)"""]
    conn = await asyncpg.connect(URL)
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE; CREATE SCHEMA "{_SCHEMA}"')
        await conn.execute(f'SET search_path TO "{_SCHEMA}"')
        for stmt in ddl:
            await conn.execute(stmt)
    finally:
        await conn.close()
    database = databases.Database(URL, server_settings={"search_path": f'"{_SCHEMA}"'})
    await database.connect()
    monkeypatch.setattr(tool, "database", database)

    async def no_cascade(keys, apply=False):
        return []

    async def no_owner(db, url):
        return None

    async def trust_upsert(*, db, product_keys):
        """Stands in for the trust upserter, deciding what catalog_trust_policy decides for these rows: a tombstone
        is blocked, the elected sig's live row is public, any other live row is a shadow NON_CANONICAL_DUPLICATE.
        (The policy itself runs in tests/test_retire_canonical_handover.py.)"""
        await database.execute("""
            INSERT INTO catalog_row_trust (subject_type, subject_key, product_key, serving_decision)
            SELECT 'product', p.product_key, p.product_key,
                   CASE WHEN p.suppression_reason IS NOT NULL THEN 'blocked'
                        WHEN e.canonical_sig_id = p.pivota_signature_id THEN 'public' ELSE 'shadow' END
            FROM catalog_products p LEFT JOIN content_canonical_election e ON e.content_key = p.content_key
            WHERE p.product_key = ANY(:keys)
            ON CONFLICT (subject_type, subject_key) DO UPDATE SET serving_decision = EXCLUDED.serving_decision""",
                               {"keys": list(product_keys)})
        return len(product_keys)
    monkeypatch.setattr(tool, "cascade_for_suppressed_product_keys", no_cascade)
    monkeypatch.setattr(tool, "live_retailer_listing_owner", no_owner)
    monkeypatch.setattr(tool, "upsert_catalog_row_trust_many", trust_upsert)
    try:
        await _seed(database)
        yield database
    finally:
        await database.disconnect()
        conn = await asyncpg.connect(URL)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')
        finally:
            await conn.close()


async def _seed(db):
    rows = [
        (OLD, "Tower 28 Beauty", OLD_SIG, None, None),
        (NEW, "Tower 28", NEW_SIG, None, None),
        (T, "Tower 28 Beauty", T_SIG, "step5_same_merchant_same_url_dup",
         json.dumps({"run_id": "20260710T023815Z", "keeper_product_key": OLD})),
    ]
    for key, brand, sig, reason, meta in rows:
        await db.execute(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand, "
            "content_key, pivota_signature_id, source_domain, canonical_url, pdp_lifecycle_stage, "
            "suppression_reason, suppressed_at, suppression_metadata) "
            "VALUES (:k, 'merch_obs_t28', 'external_seed', :k, 'The Cheeky Duo', :b, :ck, :sig, 'tower28beauty.com', "
            ":url, 'published', :r, CASE WHEN CAST(:r AS text) IS NULL THEN NULL ELSE NOW() END, CAST(:m AS jsonb))",
            {"k": key, "b": brand, "ck": CK, "sig": sig, "url": f"https://tower28beauty.com/{key}", "r": reason,
             "m": meta})
    await db.execute("INSERT INTO content_canonical_election (content_key, canonical_sig_id, election_reason, "
                     "elected_at, updated_at) VALUES (:ck, :sig, :r, '2026-07-27T02:59:14Z', '2026-07-27T02:59:14Z')",
                     {"ck": CK, "sig": OLD_SIG, "r": REASON_DEDUPE_KEEPER})
    await tool.upsert_catalog_row_trust_many(db=db, product_keys=[OLD, NEW, T])


async def _election(db):
    return dict(await db.fetch_one("SELECT * FROM content_canonical_election WHERE content_key = :ck", {"ck": CK}))


async def _next_sweep(db):
    """What the 6-hourly election would plan now: the real KEEPER_SIGS_SQL and plan_elections over the stored
    election. Candidates are the content_key's live signed rows (renderability is not what is under test here)."""
    keepers = {r["content_key"]: r["keeper_sig_id"] for r in await db.fetch_all(KEEPER_SIGS_SQL)}
    candidates = {}
    for r in await db.fetch_all("SELECT content_key, pivota_signature_id FROM catalog_products "
                                "WHERE suppression_reason IS NULL AND pivota_signature_id IS NOT NULL"):
        candidates.setdefault(r["content_key"], []).append(r["pivota_signature_id"])
    stored = {r["content_key"]: r["canonical_sig_id"]
              for r in await db.fetch_all("SELECT content_key, canonical_sig_id FROM content_canonical_election")}
    return keepers, plan_elections(candidates_by_content_key=candidates, stored_by_content_key=stored,
                                   keeper_by_content_key=keepers)


async def _plan(db):
    """The retire plan for the pair, built from the real reads (LIVE_ROWS_SQL, SEARCHABLE_SQL, ELECTIONS_SQL,
    KEEPER_SIGS_FOR_CONTENT_KEYS_SQL) and the real pure selection. The candidate set and the trust preview are given:
    their own SQL is the election's and the upserter's, gated elsewhere."""
    rows = {r["product_key"]: dict(r) for r in await db.fetch_all(tool.LIVE_ROWS_SQL, {"keys": [OLD]})}
    new_rows = {r["product_key"]: dict(r) for r in await db.fetch_all(tool.LIVE_ROWS_SQL, {"keys": [NEW]})}
    searchable = await tool.load_searchable([OLD, NEW])
    cohort = [{"stale_key": OLD, "new_key": NEW, "brand": "Tower 28", "title": "The Cheeky Duo"}]
    pairs = tool.handover_candidates(cohort, rows, new_rows, searchable=searchable)
    elections = {r["content_key"]: dict(r) for r in await db.fetch_all(tool.ELECTIONS_SQL, {"keys": [CK]})}
    keepers = {}
    for r in await db.fetch_all(KEEPER_SIGS_FOR_CONTENT_KEYS_SQL, {"content_keys": [CK]}):
        keepers.setdefault(r["content_key"], []).append(r["keeper_sig_id"])
    handovers = tool.select_handovers(pairs, elections=elections, candidates={CK: [OLD_SIG, NEW_SIG]},
                                      live_keepers=keepers, public_if_elected={NEW})
    split = tool.select_retirable(cohort, rows, {NEW}, "tower28beauty.com", serving={OLD, NEW},
                                  searchable=searchable, handovers=handovers)
    return {**split, "rows": rows, "active_seeds": [], "domain": "tower28beauty.com", "brand_override": "Tower 28",
            "category_path": "beauty", "stale_brand": "Tower 28 Beauty"}


async def test_the_stalemate_is_real_before_the_fix(db):
    """The measured state: OLD holds the URL, NEW is shadowed by it, and the sweep would never move it."""
    assert (await _election(db))["canonical_sig_id"] == OLD_SIG
    assert await tool.load_searchable([OLD, NEW]) == {OLD}
    keepers, planned = await _next_sweep(db)
    assert keepers == {CK: OLD_SIG} and planned == []
    split = tool.select_retirable([{"stale_key": OLD, "new_key": NEW}], {OLD: (await _plan(db))["rows"][OLD]},
                                  {NEW}, "tower28beauty.com", serving={OLD, NEW}, searchable={OLD})
    assert split["new_not_serving"] and not split["live"]


async def test_the_retire_hands_the_url_over_and_the_next_sweep_agrees(db):
    p = await _plan(db)
    assert [c["stale_key"] for c in p["live"]] == [OLD] and len(p["handovers"]) == 1
    before = await _election(db)
    counts = await tool.write_retire(tool.prepare_retire(p))
    assert counts["canonical_handovers"] == 1 and "trust_problems" not in counts

    after = await _election(db)
    assert after["canonical_sig_id"] == NEW_SIG and after["election_reason"] == REASON_DEDUPE_KEEPER
    assert after["elected_at"] == before["elected_at"] and after["updated_at"] > before["updated_at"]
    old = await db.fetch_one("SELECT suppression_reason, suppression_metadata FROM catalog_products "
                             "WHERE product_key = :k", {"k": OLD})
    meta = json.loads(old["suppression_metadata"]) if isinstance(old["suppression_metadata"], str) \
        else old["suppression_metadata"]
    assert old["suppression_reason"] == tool.REASON and meta["keeper_product_key"] == NEW and meta["run_id"]
    assert await tool.load_searchable([OLD, NEW]) == {NEW}  # the product never left search

    keepers, planned = await _next_sweep(db)
    assert keepers == {CK: NEW_SIG}  # the row layer now names the successor itself
    assert planned == []             # the 6-hourly sweep moves nothing: no churn


async def test_an_election_that_moved_after_the_plan_rolls_the_whole_retire_back(db):
    p = await _plan(db)
    await db.execute("UPDATE content_canonical_election SET canonical_sig_id = :s WHERE content_key = :ck",
                     {"s": OTHER, "ck": CK})
    with pytest.raises(RuntimeError, match="canonical not handed over"):
        await tool.write_retire(tool.prepare_retire(p))
    old = await db.fetch_one("SELECT suppression_reason FROM catalog_products WHERE product_key = :k", {"k": OLD})
    assert old["suppression_reason"] is None and (await _election(db))["canonical_sig_id"] == OTHER


async def test_the_keeper_is_named_only_on_this_runs_tombstone(db):
    await db.execute("UPDATE catalog_products SET suppression_reason = :r, suppressed_at = NOW(), "
                     "suppression_metadata = CAST(:m AS jsonb) WHERE product_key = :k",
                     {"r": tool.REASON, "m": json.dumps({"run_id": "retire_other"}), "k": NEW})
    assert await db.fetch_all(tool.NAME_KEEPER_SQL, {"key": NEW, "keeper": OLD, "reason": tool.REASON,
                                                     "run_id": "retire_x"}) == []  # another run's tombstone
    assert [dict(r) for r in await db.fetch_all(tool.NAME_KEEPER_SQL, {
        "key": NEW, "keeper": OLD, "reason": tool.REASON, "run_id": "retire_other"})] == [{"product_key": NEW}]
    assert await db.fetch_all(tool.NAME_KEEPER_SQL, {"key": T, "keeper": NEW, "reason": tool.REASON,
                                                     "run_id": "20260710T023815Z"}) == []  # another reason
    assert await db.fetch_all(tool.NAME_KEEPER_SQL, {"key": OLD, "keeper": NEW, "reason": tool.REASON,
                                                     "run_id": "retire_x"}) == []  # live: not a tombstone at all


async def test_revert_hands_the_url_back_and_the_next_sweep_agrees_again(db):
    prepared = tool.prepare_retire(await _plan(db))
    await tool.write_retire(prepared)
    await tool.revert_manifest(json.loads(json.dumps(prepared["manifest"], default=str)))

    e = await _election(db)
    assert e["canonical_sig_id"] == OLD_SIG and e["election_reason"] == REASON_DEDUPE_KEEPER
    old = await db.fetch_one("SELECT suppression_reason, suppression_metadata FROM catalog_products "
                             "WHERE product_key = :k", {"k": OLD})
    assert old["suppression_reason"] is None and old["suppression_metadata"] is None
    keepers, planned = await _next_sweep(db)
    assert keepers == {CK: OLD_SIG} and planned == []

    await tool.refresh_trust_for_manifest(prepared["manifest"])  # the revert's last step
    assert await tool.load_searchable([OLD, NEW]) == {OLD}


async def test_revert_leaves_an_election_that_moved_since_alone(db):
    prepared = tool.prepare_retire(await _plan(db))
    await tool.write_retire(prepared)
    await db.execute("UPDATE content_canonical_election SET canonical_sig_id = :s WHERE content_key = :ck",
                     {"s": OTHER, "ck": CK})
    await tool.revert_manifest(json.loads(json.dumps(prepared["manifest"], default=str)))
    assert (await _election(db))["canonical_sig_id"] == OTHER
