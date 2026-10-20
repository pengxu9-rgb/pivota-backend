"""The re-enqueue and the claim window on real Postgres, fed the REAL drain stage's runs.

Each run is produced by pipeline.run_stage over the SG-lane fixture (the real _check and readback), stored the
way ledger.finish_run stores it, and read back through the module's own SQL. What only Postgres proves: the
facts query hands the reducer exactly what the producer wrote (from the newest APPLIED run only), the jobs
query's last applied read, the retired-host read against catalog rows, the re-queued row landing with an
explicit options.market and the ships-to gate under the SAME scope_key, the open-job unique index making a
second (or racing) run a no-op, and a claim refused by the window writing no lease.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from db import retailer_ingest as ledger
from services.retailer_ingest import weekly_reenqueue as rq

from tests.services.test_retailer_ingest_markets_phase2 import gates_on  # noqa: F401 -- fixture
from tests.services.test_retailer_ingest_markets_sg import (  # noqa: F401 -- fixtures
    HOST, USD_META, _clean_meta_cache, sg_lane, usd_store)
from tests.services.test_retailer_ingest_pipeline import env  # noqa: F401 -- fixture
from tests.services.test_retailer_ingest_weekly_reenqueue_producer import presented, produce, use_crawl

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="requires test Postgres")
SOURCE = "test_retailer_ingest_weekly_reenqueue"
OTHER = "weekly-reenqueue.example.com"
READ = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
async def db():
    import asyncpg
    from databases import Database
    from sqlalchemy import create_engine

    import db.catalog  # noqa: F401  (registers catalog_products / catalog_offers on the MetaData)
    from db.database import metadata

    dbname = urlsplit(URL).path.rsplit("/", 1)[-1]
    if "test" not in dbname and dbname != "pivota_dialect_check":
        pytest.skip("synthetic ledger tests require a test database")
    engine = create_engine(URL)
    metadata.create_all(engine, checkfirst=True)
    engine.dispose()
    connection = await asyncpg.connect(URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await connection.execute((Path(__file__).resolve().parents[1] /
                                  "db/migrations/234_retailer_ingest_pipeline.sql").read_text())
        # _RETIRED_SQL's seed predicate: only the columns it reads, idempotently (the gate shares one database).
        await connection.execute("CREATE TABLE IF NOT EXISTS external_product_seeds (id text PRIMARY KEY);"
                                 "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS attached_product_key text;"
                                 "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS status text;")
    finally:
        await connection.close()
    database = Database(URL)
    await database.connect()
    await _cleanup(database)
    try:
        yield database
    finally:
        await _cleanup(database)
        await database.disconnect()


async def _cleanup(database):
    mine = ("(SELECT id FROM retailer_ingest_jobs WHERE source = :s OR source LIKE 'weekly_reenqueue:rij_wr_%')")
    await database.execute(f"DELETE FROM retailer_ingest_runs WHERE job_id IN {mine}", {"s": SOURCE})
    await database.execute(f"DELETE FROM retailer_ingest_jobs WHERE id IN {mine}", {"s": SOURCE})
    await database.execute("DELETE FROM catalog_offers WHERE offer_id LIKE 'wrq-%'")
    await database.execute("DELETE FROM catalog_products WHERE merchant_id = 'wrq_m'")


async def _job(db, job, *, job_id, created=READ - timedelta(hours=2), status="done"):
    await db.execute(
        "INSERT INTO retailer_ingest_jobs (id, domain, brand, options, scope_key, status, source, created_at, updated_at) "
        "VALUES (:id, :domain, :brand, CAST(:options AS jsonb), :key, :status, :s, :created, :created)",
        {"id": job_id, "domain": job["domain"], "brand": job["brand"], "options": json.dumps(job["options"]),
         "key": ledger.scope_key(job["domain"], job["brand"], job["options"]), "s": SOURCE, "created": created,
         "status": status})


async def _run(db, job_id, run, *, started, outcome="applied", stage="apply"):
    """A run row as ledger.finish_run writes it (ledger._dumps of each part)."""
    await db.execute(
        "INSERT INTO retailer_ingest_runs (id, job_id, stage, outcome, crawl, checks, readback, started_at, finished_at) "
        "VALUES (:id, :job, :stage, :outcome, CAST(:crawl AS jsonb), CAST(:checks AS jsonb), CAST(:readback AS jsonb), "
        ":started, :finished)",
        {"id": f"rir_{job_id}_{started.timestamp():.0f}", "job": job_id, "stage": stage, "outcome": outcome,
         "crawl": ledger._dumps(run.get("crawl")), "checks": ledger._dumps(run.get("checks")),
         "readback": ledger._dumps(run.get("readback")), "started": started, "finished": started + timedelta(minutes=20)})


async def _rows(db, brand="Purito"):
    rows = await db.fetch_all("SELECT * FROM retailer_ingest_jobs WHERE domain = :d AND brand = :b ORDER BY created_at",
                              {"d": HOST, "b": brand})
    out = []
    for r in rows:
        r = dict(r)
        r["options"] = r["options"] if isinstance(r["options"], dict) else json.loads(r["options"])
        out.append(r)
    return out


async def test_the_producers_evidence_survives_the_sql_and_queues_a_gated_us_job_once(db, sg_lane, monkeypatch):
    env, catalog, applied, state = sg_lane
    use_crawl(monkeypatch, state, presented("USD", "USD"))
    job, run = await produce(env, catalog)
    await _job(db, job, job_id="rij_wr_ev")
    # only the NEWEST APPLIED run is read: an older applied run and a newer failed apply say "does not ship"
    no_ship = json.loads(json.dumps(run))
    no_ship["checks"]["storefront"]["ships_to_market"] = False
    await _run(db, "rij_wr_ev", no_ship, started=READ - timedelta(days=3))
    await _run(db, "rij_wr_ev", run, started=READ)
    await _run(db, "rij_wr_ev", no_ship, started=READ + timedelta(hours=1), outcome="readback_failed")
    facts = await rq.load_facts(db, ["rij_wr_ev"])
    assert facts["rij_wr_ev"] == {"evidence": rq.evidence_from_run(run), "retired": False}
    (loaded,) = [j for j in await rq.load_jobs(db, [HOST]) if j["id"] == "rij_wr_ev"]
    assert loaded["last_applied_at"] == READ + timedelta(minutes=20)  # the applied run, not the failed one

    now = READ + timedelta(minutes=20, hours=rq.DEFAULT_INTERVAL_HOURS)
    early = await rq.run_reenqueue(db=db, now=now - timedelta(minutes=1), hosts=[HOST])
    assert [d["reason"] for d in early["decisions"]] == [rq.NOT_DUE_RECENT]
    summary = await rq.run_reenqueue(db=db, now=now, hosts=[HOST])
    (decision,) = summary["decisions"]
    assert (decision["decision"], decision["market_source"]) == ("enqueue", "evidence")
    done, new = await _rows(db)
    assert new["status"] == "queued" and new["options"] == {**job["options"], "market": "US",
                                                           "require_ships_to_market": True}
    assert new["scope_key"] == done["scope_key"] and new["source"] == "weekly_reenqueue:rij_wr_ev"
    assert new["priority"] == 0 and new["lease_until"] is None
    for later in (now, now + timedelta(hours=1)):
        again = await rq.run_reenqueue(db=db, now=later, hosts=[HOST])
        assert [d["reason"] for d in again["decisions"]] == [rq.OPEN_JOB]
    assert len(await _rows(db)) == 2


async def test_an_explicit_cohort_whose_run_says_no_shipping_is_skipped_through_the_sql(db, sg_lane, monkeypatch):
    env, catalog, applied, state = sg_lane
    state.store = usd_store(meta={**USD_META, "ships_to_countries": ["CA", "SG"]})
    use_crawl(monkeypatch, state, presented("USD"))
    job, run = await produce(env, catalog, market="US")
    await _job(db, job, job_id="rij_wr_noship")
    await _run(db, "rij_wr_noship", run, started=READ)
    summary = await rq.run_reenqueue(db=db, now=READ + timedelta(days=10), hosts=[HOST])
    assert [d["reason"] for d in summary["decisions"]] == [rq.SHIPS_TO_MARKET_FALSE]
    assert len(await _rows(db)) == 1


async def test_a_store_whose_read_back_products_are_all_retired_is_skipped_through_the_sql(db, sg_lane, monkeypatch):
    env, catalog, applied, state = sg_lane
    use_crawl(monkeypatch, state, presented("USD"))
    job, run = await produce(env, catalog, market="US")
    await _job(db, job, job_id="rij_wr_retired")
    await _run(db, "rij_wr_retired", run, started=READ)
    keys = rq._readback_keys(run)
    assert keys
    for k in keys:  # hand-retired: suppressed with a reason no lane lifts, no seed, no live offer
        await db.execute(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, suppressed_at,"
            " suppression_reason) VALUES (:k, 'wrq_m', 'external_seed', :k, 'T', :t, 'hand_retired_example')",
            {"k": k, "t": READ})
    # a SUPPRESSED offer is not a live one: the product stays retired
    await db.execute("INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, currency,"
                     " merchant_effective_price, suppressed_at) VALUES ('wrq-o0', :k, :k, 'wrq_m', 'USD', 10, :t)",
                     {"k": keys[0], "t": READ})
    now = READ + timedelta(days=10)
    summary = await rq.run_reenqueue(db=db, now=now, hosts=[HOST])
    assert [d["reason"] for d in summary["decisions"]] == [rq.RETIRED_HOST]
    # one live offer on one of them: the store is not retired
    await db.execute("INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, currency,"
                     " merchant_effective_price) VALUES ('wrq-o1', :k, :k, 'wrq_m', 'USD', 10)", {"k": keys[0]})
    summary = await rq.run_reenqueue(db=db, now=now, hosts=[HOST])
    assert [d["decision"] for d in summary["decisions"]] == ["enqueue"]


async def test_a_racing_insert_is_a_no_op_under_the_open_job_index(db):
    opts = {"vendors": ["Race"], "market": "US"}
    await _job(db, {"domain": OTHER, "brand": "Race", "options": opts}, job_id="rij_wr_race")
    now = READ + timedelta(days=10)
    jobs = await rq.load_jobs(db, [OTHER])
    (key, members), = rq.group_cohorts(j for j in jobs if j["brand"] == "Race").items()
    plans = [rq.plan_cohort(key, members, now=now, excluded=set(), facts={}) for _ in range(2)]
    ids = [await ledger.enqueue_job(domain=p["domain"], brand=p["brand"], options=p["options"], priority=0,
                                    source=p["source"], db=db) for p in plans]
    assert ids[0] and ids[1] is None


async def test_only_the_named_hosts_are_read(db):
    opts = {"vendors": ["Narrow"], "market": "US"}
    await _job(db, {"domain": OTHER, "brand": "Narrow", "options": opts}, job_id="rij_wr_narrow")
    await _job(db, {"domain": "WWW." + OTHER, "brand": "Narrow", "options": opts}, job_id="rij_wr_narrow_www")
    mine = lambda rows: sorted(j["id"] for j in rows if j["id"].startswith("rij_wr_narrow"))  # noqa: E731
    assert mine(await rq.load_jobs(db, ["www." + OTHER])) == ["rij_wr_narrow", "rij_wr_narrow_www"]
    assert mine(await rq.load_jobs(db, ["elsewhere.example.com"])) == []
    assert mine(await rq.load_jobs(db)) == ["rij_wr_narrow", "rij_wr_narrow_www"]
    assert await rq.load_facts(db, []) == {}


# ------------------------------------------------------------------ the claim window


async def _only_ours_due(db):
    await db.execute("UPDATE retailer_ingest_jobs SET next_run_at = NOW() + interval '1 day' "
                     "WHERE source IS DISTINCT FROM :s AND status IN ('queued','apply_due')", {"s": SOURCE})
    await db.execute("UPDATE retailer_ingest_jobs SET lease_until = NULL "
                     "WHERE source IS DISTINCT FROM :s AND lease_until IS NOT NULL", {"s": SOURCE})


async def test_a_refused_claim_takes_no_lease_and_the_first_claim_after_the_window_does(db):
    await _only_ours_due(db)
    job_id = await ledger.enqueue_job(domain=OTHER, brand="Window", options={"vendors": ["Window"]}, source=SOURCE, db=db)
    window = ledger.parse_claim_blackout(ledger.DEFAULT_CLAIM_BLACKOUT)
    day = datetime(2026, 10, 21, tzinfo=timezone.utc)
    for hh, mm in ((2, 0), (3, 0), (4, 45), (6, 29)):  # 02:00 is inside the hour's lead
        assert await ledger.claim_due_job(lease_seconds=3600, db=db, blackout=window, lead_s=3600,
                                          now=day.replace(hour=hh, minute=mm)) is None
        row = await db.fetch_one("SELECT lease_until, status FROM retailer_ingest_jobs WHERE id = :id", {"id": job_id})
        assert row["lease_until"] is None and row["status"] == "queued"
    claimed = await ledger.claim_due_job(lease_seconds=3600, db=db, blackout=window, lead_s=3600,
                                         now=day.replace(hour=6, minute=30))
    assert claimed and claimed["id"] == job_id and claimed["lease_until"] is not None


async def test_a_stage_leased_before_the_window_keeps_its_lease_and_finishes(db):
    await _only_ours_due(db)
    job_id = await ledger.enqueue_job(domain=OTHER, brand="InFlight", options={"vendors": ["InFlight"]}, source=SOURCE,
                                      db=db)
    window = ledger.parse_claim_blackout(ledger.DEFAULT_CLAIM_BLACKOUT)
    day = datetime(2026, 10, 21, tzinfo=timezone.utc)
    claimed = await ledger.claim_due_job(lease_seconds=3600, db=db, blackout=window, lead_s=3600,
                                         now=day.replace(hour=1, minute=59))
    assert claimed["id"] == job_id
    lease = claimed["lease_until"]
    assert await ledger.claim_due_job(lease_seconds=3600, db=db, blackout=window, lead_s=3600,
                                      now=day.replace(hour=2, minute=5)) is None
    row = await db.fetch_one("SELECT lease_until FROM retailer_ingest_jobs WHERE id = :id", {"id": job_id})
    assert row["lease_until"] == lease
    assert await ledger.transition(job_id, status="apply_due", reason="clean", run_id=None,
                                   expected_status="queued", db=db)
    row = await db.fetch_one("SELECT lease_until, status FROM retailer_ingest_jobs WHERE id = :id", {"id": job_id})
    assert row["lease_until"] is None and row["status"] == "apply_due"
