"""The weekly re-enqueue and the claim window on migration 234's real tables.

What only Postgres can prove: the evidence query reduces a real readback (jsonb) to the counts the market
rule reads, the re-enqueued row lands with an explicit options.market under the SAME scope_key, the open-job
unique index makes a second run (or a racing one) a no-op, and a claim refused by the window writes no
lease while a stage already leased keeps its lease and finishes.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from db import retailer_ingest as ledger
from services.retailer_ingest import weekly_reenqueue as rq

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="requires test Postgres")
SOURCE = "test_retailer_ingest_weekly_reenqueue"
HOST = "weekly-reenqueue.example.com"
LONG_AGO = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
async def db():
    import asyncpg
    from databases import Database

    dbname = urlsplit(URL).path.rsplit("/", 1)[-1]
    if "test" not in dbname and dbname != "pivota_dialect_check":
        pytest.skip("synthetic ledger tests require a test database")
    connection = await asyncpg.connect(URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await connection.execute((Path(__file__).resolve().parents[1] /
                                  "db/migrations/234_retailer_ingest_pipeline.sql").read_text())
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
    mine = "(SELECT id FROM retailer_ingest_jobs WHERE source = :s OR source LIKE 'weekly_reenqueue:rij_wr_%')"
    await database.execute(f"DELETE FROM retailer_ingest_runs WHERE job_id IN {mine}", {"s": SOURCE})
    await database.execute(f"DELETE FROM retailer_ingest_jobs WHERE id IN {mine}", {"s": SOURCE})


async def _done_job(db, job_id, *, brand, options, created=LONG_AGO, domain=HOST):
    await db.execute(
        "INSERT INTO retailer_ingest_jobs (id, domain, brand, options, scope_key, status, source, created_at, updated_at) "
        "VALUES (:id, :domain, :brand, CAST(:options AS jsonb), :key, 'done', :s, :created, :created)",
        {"id": job_id, "domain": domain, "brand": brand, "options": json.dumps(options),
         "key": ledger.scope_key(domain, brand, options), "s": SOURCE, "created": created})


async def _applied_run(db, job_id, *, store="USD", presented=None, checked="US", rows=None, started=LONG_AGO,
                       outcome="applied"):
    presented = {"currencies": [store], "pages_by_currency": {store: 2}, "pages_without_cookie": 0,
                 "problems": {}, "unproven_values": []} if presented is None else presented
    rows = rows if rows is not None else [
        {"product_key": "ext:retailer:a", "offers": 1, "offers_in_currency": 1, "offers_in_market": 1},
        {"product_key": "ext:retailer:b", "offers": 2, "offers_in_currency": 2, "offers_in_market": 2}]
    await db.execute(
        "INSERT INTO retailer_ingest_runs (id, job_id, stage, outcome, crawl, checks, readback, started_at, finished_at) "
        "VALUES (:id, :job, 'apply', :outcome, CAST(:crawl AS jsonb), CAST(:checks AS jsonb), "
        "CAST(:readback AS jsonb), :started, :started)",
        {"id": f"rir_{job_id}_{started.timestamp():.0f}", "job": job_id, "started": started, "outcome": outcome,
         "crawl": json.dumps({"status": "complete", "storefront": {"currency": store}, "presented_currency": presented}),
         "checks": json.dumps({"storefront": {"currency": store, "market": checked}}),
         "readback": json.dumps({"ok": True, "rows": rows})})


def _slot_time(brand, options, offset_h=0):
    key = ledger.scope_key(HOST, brand, options)
    return datetime(2026, 10, 19, tzinfo=timezone.utc) + timedelta(weeks=1, hours=rq.week_slot(key) + offset_h,
                                                                    minutes=11)


async def _rows(db, brand):
    rows = await db.fetch_all("SELECT * FROM retailer_ingest_jobs WHERE domain = :d AND brand = :b ORDER BY created_at",
                              {"d": HOST, "b": brand})
    out = []
    for r in rows:
        r = dict(r)
        r["options"] = r["options"] if isinstance(r["options"], dict) else json.loads(r["options"])
        out.append(r)
    return out


async def test_an_evidence_proven_cohort_is_requeued_with_its_market_once(db):
    opts = {"vendors": ["Evidence"]}
    await _done_job(db, "rij_wr_ev", brand="Evidence", options=opts)
    # only the NEWEST APPLIED run is read: an older applied run and a newer failed apply both prove nothing
    no_cookie = {"pages_by_currency": {}, "pages_without_cookie": 3}
    await _applied_run(db, "rij_wr_ev", started=LONG_AGO - timedelta(days=1), presented=no_cookie)
    await _applied_run(db, "rij_wr_ev", started=LONG_AGO)
    await _applied_run(db, "rij_wr_ev", started=LONG_AGO + timedelta(hours=1), presented=no_cookie,
                       outcome="readback_failed")
    now = _slot_time("Evidence", opts)
    summary = await rq.run_reenqueue(db=db, now=now, hosts=[HOST])
    (decision,) = [d for d in summary["decisions"] if d["brand"] == "Evidence"]
    assert decision["decision"] == "enqueue" and decision["market_source"] == "evidence"
    done, new = await _rows(db, "Evidence")
    assert new["status"] == "queued" and new["options"] == {**opts, "market": "US"}
    assert new["scope_key"] == done["scope_key"] and new["source"] == "weekly_reenqueue:rij_wr_ev"
    assert new["priority"] == 0 and new["lease_until"] is None
    # again in the same hour, and an hour later: the open job answers
    for later in (now, now + timedelta(hours=1)):
        again = await rq.run_reenqueue(db=db, now=later, hosts=[HOST])
        assert [d["reason"] for d in again["decisions"] if d["brand"] == "Evidence"] == [rq.OPEN_JOB]
    assert len(await _rows(db, "Evidence")) == 2


async def test_the_evidence_query_reduces_the_readback_the_rule_reads(db):
    opts = {"vendors": ["Partial"]}
    await _done_job(db, "rij_wr_partial", brand="Partial", options=opts)
    await _applied_run(db, "rij_wr_partial", rows=[
        {"offers": 1, "offers_in_currency": 1, "offers_in_market": 1},
        {"offers": 2, "offers_in_currency": 1, "offers_in_market": 2},   # one offer in another currency
        {"offers": 0, "offers_in_currency": 0, "offers_in_market": 0},   # no offer at all
        {"offers": "1", "offers_in_currency": "1", "offers_in_market": "1"},  # not a number
        {"offers": 1, "offers_in_currency": 1, "offers_in_market": 0}])  # not stamped the market
    evidence = await rq.load_evidence(db, ["rij_wr_partial", "rij_wr_missing"])
    assert set(evidence) == {"rij_wr_partial"}
    ev = evidence["rij_wr_partial"]
    assert (ev["readback_rows"], ev["readback_rows_proven"]) == (5, 1)
    assert (ev["store_currency"], ev["checked_market"]) == ("USD", "US")
    summary = await rq.run_reenqueue(db=db, now=_slot_time("Partial", opts), hosts=[HOST])
    (decision,) = [d for d in summary["decisions"] if d["brand"] == "Partial"]
    assert (decision["reason"], decision["detail"]) == (rq.MARKET_UNPROVEN, rq.READBACK_UNPROVEN)
    assert len(await _rows(db, "Partial")) == 1


async def test_a_readback_that_is_not_a_list_proves_nothing(db):
    opts = {"vendors": ["Shapeless"]}
    await _done_job(db, "rij_wr_shape", brand="Shapeless", options=opts)
    await db.execute(
        "INSERT INTO retailer_ingest_runs (id, job_id, stage, outcome, crawl, checks, readback) "
        "VALUES ('rir_wr_shape', 'rij_wr_shape', 'apply', 'applied', NULL, NULL, CAST(:rb AS jsonb))",
        {"rb": json.dumps({"rows": {"not": "a list"}})})
    ev = (await rq.load_evidence(db, ["rij_wr_shape"]))["rij_wr_shape"]
    assert (ev["readback_rows"], ev["readback_rows_proven"], ev["store_currency"]) == (0, 0, None)


async def test_presented_currency_absent_is_skipped_not_defaulted(db):
    opts = {"vendors": ["NoCookie"]}
    await _done_job(db, "rij_wr_nocookie", brand="NoCookie", options=opts)
    await _applied_run(db, "rij_wr_nocookie", presented={"pages_by_currency": {}, "pages_without_cookie": 4})
    summary = await rq.run_reenqueue(db=db, now=_slot_time("NoCookie", opts), hosts=[HOST])
    (decision,) = [d for d in summary["decisions"] if d["brand"] == "NoCookie"]
    assert (decision["reason"], decision["detail"]) == (rq.MARKET_UNPROVEN, rq.PRESENTED_CURRENCY_ABSENT)
    assert len(await _rows(db, "NoCookie")) == 1


async def test_an_explicit_sg_cohort_keeps_its_market_and_key(db):
    opts = {"vendors": ["Explicit"], "market": "SG", "exclude_handles": ["x"]}
    await _done_job(db, "rij_wr_sg", brand="Explicit", options=opts)
    summary = await rq.run_reenqueue(db=db, now=_slot_time("Explicit", opts), hosts=[HOST], dry_run=True)
    assert [d["decision"] for d in summary["decisions"] if d["brand"] == "Explicit"] == ["would_enqueue"]
    assert len(await _rows(db, "Explicit")) == 1  # the dry run wrote nothing
    await rq.run_reenqueue(db=db, now=_slot_time("Explicit", opts), hosts=[HOST])
    done, new = await _rows(db, "Explicit")
    assert new["options"] == opts and new["scope_key"] == done["scope_key"]


async def test_a_racing_insert_is_a_no_op_under_the_open_job_index(db):
    opts = {"vendors": ["Race"], "market": "US"}
    await _done_job(db, "rij_wr_race", brand="Race", options=opts)
    now = _slot_time("Race", opts)
    # both runs read before either writes: the second insert meets the first's open job
    jobs = await rq.load_jobs(db, [HOST])
    cohorts = rq.group_cohorts(j for j in jobs if j["brand"] == "Race")
    (key, members), = cohorts.items()
    plans = [rq.plan_cohort(key, members, now=now, excluded=set(), evidence={}) for _ in range(2)]
    ids = [await ledger.enqueue_job(domain=p["domain"], brand=p["brand"], options=p["options"], priority=0,
                                    source=p["source"], db=db) for p in plans]
    assert ids[0] and ids[1] is None
    assert len(await _rows(db, "Race")) == 2


async def test_only_the_named_hosts_are_read(db):
    opts = {"vendors": ["Narrow"], "market": "US"}
    await _done_job(db, "rij_wr_narrow", brand="Narrow", options=opts)
    await _done_job(db, "rij_wr_narrow_www", brand="Narrow", options=opts, domain="WWW." + HOST)
    assert sorted(j["id"] for j in await rq.load_jobs(db, ["www." + HOST]) if j["id"].startswith("rij_wr_narrow")) == \
        ["rij_wr_narrow", "rij_wr_narrow_www"]
    assert [j for j in await rq.load_jobs(db, ["elsewhere.example.com"]) if j["id"] == "rij_wr_narrow"] == []
    assert sorted(j["id"] for j in await rq.load_jobs(db) if j["id"].startswith("rij_wr_narrow")) == \
        ["rij_wr_narrow", "rij_wr_narrow_www"]


# ------------------------------------------------------------------ the claim window


async def _only_ours_due(db):
    await db.execute("UPDATE retailer_ingest_jobs SET next_run_at = NOW() + interval '1 day' "
                     "WHERE source IS DISTINCT FROM :s AND status IN ('queued','apply_due')", {"s": SOURCE})
    await db.execute("UPDATE retailer_ingest_jobs SET lease_until = NULL "
                     "WHERE source IS DISTINCT FROM :s AND lease_until IS NOT NULL", {"s": SOURCE})


async def test_a_claim_inside_the_window_takes_no_lease_and_the_first_claim_after_it_does(db):
    await _only_ours_due(db)
    job_id = await ledger.enqueue_job(domain=HOST, brand="Window", options={"vendors": ["Window"]}, source=SOURCE, db=db)
    window = ledger.parse_claim_blackout(ledger.DEFAULT_CLAIM_BLACKOUT)
    day = datetime(2026, 10, 21, tzinfo=timezone.utc)
    for hh, mm in ((3, 0), (4, 45), (6, 29)):
        assert await ledger.claim_due_job(lease_seconds=3600, db=db, blackout=window,
                                          now=day.replace(hour=hh, minute=mm)) is None
        row = await db.fetch_one("SELECT lease_until, status FROM retailer_ingest_jobs WHERE id = :id", {"id": job_id})
        assert row["lease_until"] is None and row["status"] == "queued"
    claimed = await ledger.claim_due_job(lease_seconds=3600, db=db, blackout=window, now=day.replace(hour=6, minute=30))
    assert claimed and claimed["id"] == job_id and claimed["lease_until"] is not None


async def test_a_stage_leased_before_the_window_keeps_its_lease_and_finishes(db):
    await _only_ours_due(db)
    job_id = await ledger.enqueue_job(domain=HOST, brand="InFlight", options={"vendors": ["InFlight"]}, source=SOURCE,
                                      db=db)
    window = ledger.parse_claim_blackout(ledger.DEFAULT_CLAIM_BLACKOUT)
    day = datetime(2026, 10, 21, tzinfo=timezone.utc)
    claimed = await ledger.claim_due_job(lease_seconds=3600, db=db, blackout=window, now=day.replace(hour=2, minute=59))
    assert claimed["id"] == job_id
    lease = claimed["lease_until"]
    # inside the window: no other claim, and the running stage's lease is untouched
    assert await ledger.claim_due_job(lease_seconds=3600, db=db, blackout=window, now=day.replace(hour=3, minute=5)) is None
    row = await db.fetch_one("SELECT lease_until FROM retailer_ingest_jobs WHERE id = :id", {"id": job_id})
    assert row["lease_until"] == lease
    # the stage records its verdict and releases its lease, window or not
    assert await ledger.transition(job_id, status="apply_due", reason="clean", run_id=None,
                                   expected_status="queued", db=db)
    row = await db.fetch_one("SELECT lease_until, status FROM retailer_ingest_jobs WHERE id = :id", {"id": job_id})
    assert row["lease_until"] is None and row["status"] == "apply_due"
