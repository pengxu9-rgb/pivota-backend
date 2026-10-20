"""The re-enqueue: cohort selection, the cadence on the age of the last applied read, the skip reasons
(retired host, ships-to false, lineage), and the market derivation (explicit, then the run's evidence,
then skipped -- never a default). The rules against the REAL producer's run JSON are in
test_retailer_ingest_weekly_reenqueue_producer.py."""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

import jobs.retailer_ingest_weekly_reenqueue as job
from db import retailer_ingest as ledger
from services.retailer_ingest import weekly_reenqueue as rq

NOW = datetime(2026, 11, 2, 10, 11, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=10)
DOMAIN = "shop.example.com"
BASE = {"vendors": ["Example Brand"]}


def key_of(domain=DOMAIN, brand="Example Brand", options=None):
    return ledger.scope_key(domain, brand, dict(BASE if options is None else options))


def mk(job_id, *, status="done", created=None, read=None, options=None, domain=DOMAIN, brand="Example Brand",
       lease=None):
    options = dict(BASE) if options is None else (options if isinstance(options, str) else dict(options))
    created = created or OLD - timedelta(hours=1)
    if read is None and status == "done" and isinstance(created, datetime):
        read = created + timedelta(hours=1)
    return {"id": job_id, "domain": domain, "brand": brand, "options": options, "status": status,
            "lease_until": lease, "created_at": created, "last_applied_at": read}


def run(market="US", currency="USD", ships=True, pages=None, rows=None, presented=True):
    """An applied run in the shape ledger.finish_run stores (see the producer tests for the real one)."""
    crawl = {"status": "complete", "storefront": {"currency": currency, "ships_to_countries": [market]}}
    if presented:
        crawl["presented_currency"] = {"currencies": [currency], "pages_by_currency": {currency: 3} if pages is None else pages,
                                       "pages_without_cookie": 0, "problems": {}, "unproven_values": []}
    storefront = {"currency": currency, "market": market}
    if ships is not None:
        storefront["ships_to_market"] = ships
    rows = rows if rows is not None else [
        {"product_key": "ext:retailer:a", "offers": 1, "offers_in_currency": 1, "offers_in_market": 1},
        {"product_key": "ext:retailer:b", "offers": 2, "offers_in_currency": 2, "offers_in_market": 2}]
    return {"crawl": crawl, "checks": {"storefront": storefront}, "readback": {"ok": True, "rows": rows}}


def ev(**kw):
    return rq.evidence_from_run(run(**kw))


class FakeDB:
    """The three reads and the enqueue INSERT, with migration 234's one-open-job-per-scope_key index."""

    def __init__(self, jobs, runs=None, products=None):
        self.jobs = list(jobs)
        self.runs = dict(runs or {})
        self.products = dict(products or {})
        self.inserts, self.fact_reads, self.product_reads = [], [], []
        self.now = NOW

    async def fetch_all(self, sql, values=None):
        if sql is rq._JOBS_SQL:
            hosts = set(values["hosts"])
            return [dict(j) for j in self.jobs if values["all_hosts"] or rq.fold_host(j["domain"]) in hosts]
        if sql is rq._FACTS_SQL:
            self.fact_reads.append(list(values["ids"]))
            return [{"job_id": i, **{k: json.dumps(v) for k, v in self.runs[i].items()}}
                    for i in values["ids"] if i in self.runs]
        if sql is rq._RETIRED_SQL:
            self.product_reads.append(list(values["keys"]))
            return [{"product_key": k, **self.products[k]} for k in values["keys"] if k in self.products]
        raise AssertionError(f"unexpected read: {sql}")

    async def fetch_one(self, sql, values=None):
        assert "INSERT INTO retailer_ingest_jobs" in sql
        open_keys = {ledger.scope_key(j["domain"], j["brand"], j["options"]) for j in self.jobs
                     if j["status"] in ledger.OPEN_STATUSES}
        if values["scope_key"] in open_keys:
            return None
        row = {"id": values["id"], "domain": values["domain"], "brand": values["brand"],
               "options": json.loads(values["options"]), "status": "queued", "lease_until": None,
               "created_at": self.now, "last_applied_at": None, "priority": values["priority"],
               "source": values["source"], "scope_key": values["scope_key"]}
        self.jobs.append(row)
        self.inserts.append(row)
        return {"id": row["id"]}


async def go(db, now=NOW, **kw):
    db.now = now
    return await rq.run_reenqueue(db=db, now=now, **kw)


def only(summary):
    (decision,) = summary["decisions"]
    return decision


# ------------------------------------------------------------------ market derivation and the ships-to gate


async def test_an_explicit_market_is_copied_with_the_ships_to_gate_and_the_same_key():
    options = {**BASE, "market": "SG", "exclude_handles": ["a-gift"], "accepted_flags": ["k"],
               "only_category": "beauty/makeup"}
    db = FakeDB([mk("rij_done", options=options)], runs={"rij_done": run(market="SG", currency="SGD")})
    summary = await go(db)
    (row,) = db.inserts
    assert row["options"] == {**options, "require_ships_to_market": True}  # approvals carried, gate added
    assert row["source"] == "weekly_reenqueue:rij_done" and row["priority"] == 0
    assert row["scope_key"] == key_of(options=options)
    assert summary["enqueued"] == 1 and summary["market_source"] == {"explicit": 1}


async def test_an_explicit_market_with_ships_unknown_is_still_queued_with_the_gate():
    options = {**BASE, "market": "US"}
    for runs in ({"rij_done": run(ships=None)}, {}):  # a run that cannot say, or no applied run at all
        db = FakeDB([mk("rij_done", options=options)], runs=runs)
        decision = only(await go(db))
        assert decision["decision"] == "enqueue" and decision["options"]["require_ships_to_market"] is True


async def test_any_cohort_whose_run_says_the_store_does_not_ship_there_is_skipped_before_the_market():
    for options, runs in (({**BASE, "market": "US"}, run(ships=False)),        # explicit
                          ({**BASE, "market": "DE"}, run(ships=False)),        # even an invalid market
                          (BASE, run(ships=False))):                            # evidence
        db = FakeDB([mk("rij_done", options=options)], runs={"rij_done": runs})
        summary = await go(db)
        assert only(summary)["reason"] == rq.SHIPS_TO_MARKET_FALSE and db.inserts == []
        assert summary["named"][rq.SHIPS_TO_MARKET_FALSE][0]["prev_job_id"] == "rij_done"


async def test_without_an_explicit_market_the_runs_evidence_names_us_and_gates_the_job():
    db = FakeDB([mk("rij_done")], runs={"rij_done": run()})
    summary = await go(db)
    (row,) = db.inserts
    assert row["options"] == {**BASE, "market": "US", "require_ships_to_market": True}
    assert row["scope_key"] == key_of()
    assert only(summary)["market_source"] == "evidence"
    assert db.fact_reads == [["rij_done"]]


async def test_a_shopify_markets_capture_is_queued_without_the_storefront_only_gate():
    options = {"vendors": ["Example Brand"], "source": "shopify_markets", "market": "SG"}
    db = FakeDB([mk("rij_cap", options=options)], runs={"rij_cap": run(market="SG", currency="SGD")})
    decision = only(await go(db))
    assert decision["decision"] == "enqueue" and "require_ships_to_market" not in decision["options"]


@pytest.mark.parametrize("evidence, why", [
    (None, rq.NO_APPLIED_RUN),
    (ev(market=None), rq.NO_CHECKED_MARKET),
    (ev(market="DE"), rq.NO_CHECKED_MARKET),
    (ev(currency=None), rq.NO_STORE_CURRENCY),
    ({**ev(), "store_currency": "SGD"}, rq.CURRENCY_MARKET_MISMATCH),
    (ev(ships=None), rq.SHIPS_TO_MARKET_UNPROVEN),
    (ev(ships=False), rq.SHIPS_TO_MARKET_UNPROVEN),
    ({**ev(), "ships_to_market": "true"}, rq.SHIPS_TO_MARKET_UNPROVEN),   # only a real JSON true
    (ev(presented=False), rq.PRESENTED_CURRENCY_ABSENT),
    (ev(pages={}), rq.PRESENTED_CURRENCY_ABSENT),
    (ev(pages={"USD": 0}), rq.PRESENTED_CURRENCY_ABSENT),
    (ev(pages={"USD": True}), rq.PRESENTED_CURRENCY_ABSENT),
    ({**ev(), "presented": "not json"}, rq.PRESENTED_CURRENCY_ABSENT),
    ({**ev(), "presented": '{"pages_by_currency": []}'}, rq.PRESENTED_CURRENCY_ABSENT),
    (ev(pages={"USD": 3, "QAR": 1}), rq.PRESENTED_CURRENCY_MISMATCH),
    (ev(pages={"QAR": 3}), rq.PRESENTED_CURRENCY_MISMATCH),
    ({**ev(), "presented": {"pages_by_currency": {"USD": 3}, "unproven_values": ["QAR"]}}, rq.PRESENTED_CURRENCY_MISMATCH),
    ({**ev(), "presented": {"pages_by_currency": {"USD": 3}, "problems": {"x": 1}}}, rq.PRESENTED_CURRENCY_MISMATCH),
    (ev(rows=[{"offers": 1, "offers_in_currency": 1, "offers_in_market": 1},
              {"offers": 2, "offers_in_currency": 1, "offers_in_market": 2}]), rq.READBACK_UNPROVEN),
    (ev(rows=[]), rq.READBACK_UNPROVEN),
])
def test_evidence_that_does_not_show_the_market_is_unproven_never_defaulted(evidence, why):
    assert rq.evidence_market(evidence) == (None, why)
    assert rq.derive_market({}, evidence) == (None, "", why)


async def test_a_market_the_evidence_does_not_show_is_skipped_and_named():
    db = FakeDB([mk("rij_done")], runs={"rij_done": run(presented=False)})
    summary = await go(db)
    decision = only(summary)
    assert (decision["reason"], decision["detail"]) == (rq.MARKET_UNPROVEN, rq.PRESENTED_CURRENCY_ABSENT)
    assert "market" not in decision and db.inserts == []
    assert summary["market_unproven_detail"] == {rq.PRESENTED_CURRENCY_ABSENT: 1}
    assert summary["named"][rq.MARKET_UNPROVEN] == [{"host": DOMAIN, "brand": "Example Brand", "prev_job_id": "rij_done",
                                                     "detail": rq.PRESENTED_CURRENCY_ABSENT}]
    db = FakeDB([mk("rij_done")])  # no applied run at all
    assert only(await go(db))["detail"] == rq.NO_APPLIED_RUN


def test_the_readback_reduction_counts_only_whole_numeric_rows():
    rows = [{"offers": 1, "offers_in_currency": 1, "offers_in_market": 1},
            {"offers": 0, "offers_in_currency": 0, "offers_in_market": 0},           # no offer
            {"offers": True, "offers_in_currency": True, "offers_in_market": True},  # not a number
            {"offers": "1", "offers_in_currency": "1", "offers_in_market": "1"},
            {"offers": 2, "offers_in_currency": 2, "offers_in_market": 1},           # not stamped the market
            {"offers": 2, "offers_in_currency": 1, "offers_in_market": 2},           # not in the currency
            "not a row"]
    got = rq.evidence_from_run({"readback": {"rows": rows}})
    assert (got["readback_rows"], got["readback_rows_proven"]) == (7, 1)
    assert rq.evidence_from_run({"readback": {"rows": {"not": "a list"}}})["readback_rows"] == 0
    assert rq.evidence_from_run(None) is None and rq.evidence_from_run({}) is None
    text = {k: json.dumps(v) for k, v in run(market="sg", currency="sgd", pages={"sgd": 2}).items()}
    assert rq.evidence_market(rq.evidence_from_run(text)) == ("SG", None)


@pytest.mark.parametrize("market", ["DE", "XX", "usa"])
def test_an_explicit_market_outside_the_ingest_markets_is_unproven_not_replaced(market):
    assert rq.derive_market({"market": market}, ev()) == (None, "", rq.EXPLICIT_MARKET_INVALID)


@pytest.mark.parametrize("market", [None, ""])
def test_an_empty_explicit_market_falls_through_to_the_evidence(market):
    assert rq.derive_market({"market": market}, ev()) == ("US", rq.MARKET_EVIDENCE, None)
    assert rq.derive_market({"market": market}, None) == (None, "", rq.NO_APPLIED_RUN)


def test_an_explicit_market_wins_over_contradicting_evidence():
    assert rq.derive_market({"market": " sg "}, ev()) == ("SG", rq.MARKET_EXPLICIT, None)


# ------------------------------------------------------------------ retired hosts


RETIRED = {"suppressed_at": NOW, "suppression_reason": "hand_retired_example", "has_active_seed": False,
           "has_live_offer": False}


@pytest.mark.parametrize("products, retired", [
    ({"ext:retailer:a": RETIRED, "ext:retailer:b": RETIRED}, True),
    ({"ext:retailer:a": RETIRED, "ext:retailer:b": {**RETIRED, "has_live_offer": True}}, False),
    ({"ext:retailer:a": RETIRED, "ext:retailer:b": {**RETIRED, "has_active_seed": True}}, False),
    ({"ext:retailer:a": RETIRED, "ext:retailer:b": {**RETIRED, "suppression_reason": None}}, False),
    ({"ext:retailer:a": RETIRED, "ext:retailer:b": {**RETIRED, "suppression_reason": "stale_after_sync"}}, False),
    ({"ext:retailer:a": RETIRED, "ext:retailer:b": {**RETIRED, "suppressed_at": None}}, False),
    ({"ext:retailer:a": RETIRED}, False),  # a product missing from the catalog is no proof
])
async def test_a_cohort_whose_read_back_products_are_all_retired_is_skipped(products, retired):
    db = FakeDB([mk("rij_done", options={**BASE, "market": "US"})], runs={"rij_done": run()}, products=products)
    decision = only(await go(db))
    assert (decision.get("reason") == rq.RETIRED_HOST) is retired
    assert db.product_reads == [["ext:retailer:a", "ext:retailer:b"]]


async def test_a_run_that_read_back_no_product_proves_no_retirement():
    db = FakeDB([mk("rij_done", options={**BASE, "market": "US"})], runs={"rij_done": run(rows=[])})
    assert only(await go(db))["decision"] == "enqueue" and db.product_reads == []
    assert not rq.host_retired([], {})


async def test_retirement_is_judged_before_the_market():
    db = FakeDB([mk("rij_done")], runs={"rij_done": run(presented=False)},
                products={"ext:retailer:a": RETIRED, "ext:retailer:b": RETIRED})
    assert only(await go(db))["reason"] == rq.RETIRED_HOST


# ------------------------------------------------------------------ lineage and skip reasons


@pytest.mark.parametrize("status", ["queued", "apply_due", "held"])
async def test_a_cohort_with_an_open_job_is_skipped(status):
    db = FakeDB([mk("rij_old", created=NOW - timedelta(days=30)), mk("rij_open", status=status)],
                runs={"rij_old": run()})
    assert only(await go(db))["reason"] == rq.OPEN_JOB and db.inserts == []


async def test_a_cohort_with_a_live_lease_is_skipped_and_an_expired_lease_is_not():
    db = FakeDB([mk("rij_done", lease=NOW + timedelta(minutes=5))], runs={"rij_done": run()})
    assert only(await go(db))["reason"] == rq.LEASED
    db = FakeDB([mk("rij_done", lease=NOW - timedelta(minutes=5))], runs={"rij_done": run()})
    assert only(await go(db))["decision"] == "enqueue"


def test_a_naive_or_text_lease_is_read_as_utc():
    assert rq.lineage_reason([mk("a", lease=(NOW + timedelta(minutes=1)).replace(tzinfo=None))], NOW)[0] == rq.LEASED
    assert rq.lineage_reason([mk("a", lease=(NOW + timedelta(minutes=1)).isoformat())], NOW)[0] == rq.LEASED
    assert rq.lineage_reason([mk("a", lease=NOW)], NOW)[0] is None


async def test_a_cohort_that_never_finished_is_skipped():
    assert only(await go(FakeDB([mk("rij_f", status="failed")])))["reason"] == rq.NO_DONE_JOB


@pytest.mark.parametrize("status, reason", [("failed", rq.FAILED_LINEAGE), ("cancelled", rq.CANCELLED_LINEAGE),
                                            ("nothing", rq.NOTHING_LINEAGE)])
async def test_a_lineage_that_ended_after_its_latest_done_job_is_skipped(status, reason):
    db = FakeDB([mk("rij_done", created=NOW - timedelta(days=30)),
                 mk("rij_end", status=status, created=NOW - timedelta(days=20))], runs={"rij_done": run()})
    decision = only(await go(db))
    assert decision["reason"] == reason and decision["prev_job_id"] == "rij_done" and db.inserts == []


async def test_a_later_done_job_reopens_a_failed_lineage():
    db = FakeDB([mk("rij_f", status="failed", created=NOW - timedelta(days=30)),
                 mk("rij_c", status="cancelled", created=NOW - timedelta(days=25)),
                 mk("rij_done", created=NOW - timedelta(days=20))], runs={"rij_done": run()})
    decision = only(await go(db))
    assert decision["decision"] == "enqueue" and decision["prev_job_id"] == "rij_done"


async def test_the_options_of_the_latest_done_job_are_the_ones_copied():
    old = {**BASE, "market": "US", "max_products": 50}
    new = {**BASE, "market": "US", "max_products": 50, "exclude_handles": ["late-exclusion"]}
    assert key_of(options=old) == key_of(options=new)  # an exclusion is bookkeeping, not scope
    db = FakeDB([mk("rij_a", options=old, created=NOW - timedelta(days=30)),
                 mk("rij_b", options=new, created=NOW - timedelta(days=20))])
    await go(db)
    assert db.inserts[0]["options"] == {**new, "require_ships_to_market": True}


async def test_an_excluded_host_is_skipped_whatever_its_spelling():
    db = FakeDB([mk("rij_done", domain="www.shop.example.com")], runs={"rij_done": run()})
    decision = only(await go(db, excluded={"shop.example.com"}))
    assert decision["reason"] == rq.EXCLUDED_HOST and db.inserts == [] and db.fact_reads == []


def test_excluded_hosts_the_run_cap_and_the_interval_come_from_env(monkeypatch):
    monkeypatch.setenv(rq.EXCLUDED_HOSTS_ENV, " WWW.Shop.Example.com, other.example.com ,,")
    assert rq.excluded_hosts_from_env() == {"shop.example.com", "other.example.com"}
    monkeypatch.delenv(rq.EXCLUDED_HOSTS_ENV)
    assert rq.excluded_hosts_from_env() == set()
    for raw, want in ((None, 10), ("", 10), ("3", 3), (" 1 ", 1), ("100", 100), ("0", 10), ("101", 10),
                      ("-2", 10), ("lots", 10)):
        _set(monkeypatch, rq.MAX_PER_RUN_ENV, raw)
        assert rq.max_per_run_from_env() == want, raw
    for raw, want in ((None, 120), ("", 120), ("96", 96), ("1", 1), ("167", 167), ("168", 167), ("500", 167),
                      ("0", 120), ("-5", 120), ("weekly", 120)):
        _set(monkeypatch, rq.INTERVAL_HOURS_ENV, raw)
        assert rq.interval_hours_from_env() == want, raw
    assert rq.DEFAULT_INTERVAL_HOURS == 120 and rq.MAX_INTERVAL_HOURS == rq.FRESHNESS_HOURS - 1 == 167


def _set(monkeypatch, name, raw):
    if raw is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, raw)


async def test_options_the_validator_now_refuses_are_skipped():
    options = {**BASE, "market": "US", "retired_option": True}
    decision = only(await go(FakeDB([mk("rij_done", options=options)])))
    assert decision["reason"] == rq.INVALID_OPTIONS and "retired_option" in decision["detail"]
    assert rq.summarize([decision], dry_run=False, now=NOW)["named"][rq.INVALID_OPTIONS]


async def test_a_derived_market_that_would_change_the_cohort_key_is_refused(monkeypatch):
    # The guard behind "neither addition is scope": no producer emits this, so force the market.
    monkeypatch.setattr(rq, "derive_market", lambda options, evidence: ("SG", rq.MARKET_EVIDENCE, None))
    db = FakeDB([mk("rij_done")], runs={"rij_done": run()})
    assert only(await go(db))["reason"] == rq.SCOPE_CHANGED and db.inserts == []


async def test_an_insert_that_meets_a_job_opened_since_the_read_reports_exists():
    db = FakeDB([mk("rij_done")], runs={"rij_done": run()})

    async def raced(sql, values=None):
        return None  # ON CONFLICT DO NOTHING: another run (or a human) queued it first
    db.fetch_one = raced
    summary = await go(db)
    assert only(summary)["reason"] == rq.EXISTS and summary["enqueued"] == 0


# ------------------------------------------------------------------ the cadence


def test_due_on_the_age_of_the_last_read():
    assert rq.due_reason(read_at=NOW - timedelta(hours=120), now=NOW, interval_hours=120) is None
    assert rq.due_reason(read_at=NOW - timedelta(hours=119, minutes=59), now=NOW, interval_hours=120) == rq.NOT_DUE_RECENT
    assert rq.due_reason(read_at=NOW - timedelta(hours=96), now=NOW, interval_hours=96) is None


def test_the_last_read_is_the_newest_applied_read_or_a_newer_job():
    t = NOW - timedelta(days=9)
    assert rq.last_read([mk("a", created=t, read=t + timedelta(hours=5))]) == t + timedelta(hours=5)
    # a hand-queued run's applied read counts, whichever job of the cohort holds it
    assert rq.last_read([mk("a", created=t, read=t + timedelta(hours=5)),
                         mk("b", created=t + timedelta(days=2), read=t + timedelta(days=3))]) == t + timedelta(days=3)
    # a done job with no applied run: its creation, so it is not due every hour
    no_read = mk("a", created=t)
    no_read["last_applied_at"] = None
    assert rq.last_read([no_read]) == t
    # a newer job of any status (e.g. a later failure) is newer than the read
    assert rq.last_read([mk("a", created=t, read=t + timedelta(hours=1)),
                         mk("b", status="failed", created=t + timedelta(days=1))]) == t + timedelta(days=1)
    # text and naive timestamps are UTC
    assert rq.last_read([{**mk("a", created=t), "created_at": t.replace(tzinfo=None).isoformat(),
                          "last_applied_at": (t + timedelta(hours=2)).replace(tzinfo=None)}]) == t + timedelta(hours=2)


@pytest.mark.parametrize("age_h, due", [(119, False), (120, True), (200, True)])
async def test_a_cohort_is_queued_once_its_read_is_interval_old(age_h, due):
    db = FakeDB([mk("rij_done", created=NOW - timedelta(days=30), read=NOW - timedelta(hours=age_h))],
                runs={"rij_done": run()})
    decision = only(await go(db))
    assert (decision["decision"] == "enqueue") is due
    if not due:
        assert decision["reason"] == rq.NOT_DUE_RECENT and db.fact_reads == []


async def test_hourly_runs_are_idempotent():
    db = FakeDB([mk("rij_done", created=NOW - timedelta(days=30))], runs={"rij_done": run()})
    assert (await go(db))["enqueued"] == 1
    assert only(await go(db))["reason"] == rq.OPEN_JOB                    # the same hour again
    assert only(await go(db, NOW + timedelta(hours=1)))["reason"] == rq.OPEN_JOB
    queued = db.jobs[-1]
    queued.update(status="done", last_applied_at=NOW + timedelta(hours=30))  # the drain finished it
    db.runs[queued["id"]] = run()
    for hours in (31, 60, 149):
        assert only(await go(db, NOW + timedelta(hours=hours)))["reason"] == rq.NOT_DUE_RECENT
    assert len(db.inserts) == 1
    decision = only(await go(db, NOW + timedelta(hours=150)))
    assert decision["decision"] == "enqueue" and len(db.inserts) == 2
    # the second copies the first re-crawl's options, explicit market and gate included
    assert db.inserts[1]["options"] == {**BASE, "market": "US", "require_ships_to_market": True}
    assert decision["market_source"] == "explicit"


class Drain:
    """A drain that applies each queued job `latency` hours after it is queued."""

    def __init__(self, db, latency):
        self.db, self.latency, self.reads = db, latency, []

    def tick(self, now):
        for j in self.db.jobs:
            if j["status"] == "queued" and j["created_at"] + timedelta(hours=self.latency) <= now:
                j.update(status="done", last_applied_at=now)
                self.db.runs[j["id"]] = run()
                self.reads.append(now)


async def _simulate(latency, *, hand_runs=(), weeks=8, interval=rq.DEFAULT_INTERVAL_HOURS, start_read=None):
    first = start_read or NOW - timedelta(hours=interval)
    db = FakeDB([mk("rij_0", options={**BASE, "market": "US"}, created=first - timedelta(hours=1), read=first)])
    drain = Drain(db, latency)
    drain.reads.append(first)
    hands = sorted(hand_runs)
    for h in range(weeks * 168):
        now = NOW + timedelta(hours=h)
        while hands and hands[0] <= h:  # a hand-queued run: created and applied by an operator now
            hands.pop(0)
            db.jobs.append(mk(f"rij_hand_{h}", options={**BASE, "market": "US"}, created=now, read=now))
            db.runs[db.jobs[-1]["id"]] = run()
            drain.reads.append(now)
        drain.tick(now)
        await go(db, now, interval_hours=interval)
    reads = sorted(drain.reads)
    return max((b - a) for a, b in zip(reads, reads[1:])) / timedelta(hours=1), len(reads)


@pytest.mark.parametrize("latency", [0, 1, 12, 24, 46])
async def test_the_gap_between_applied_reads_stays_under_the_freshness_window(latency):
    budget = rq.FRESHNESS_HOURS - rq.DEFAULT_INTERVAL_HOURS - 1
    assert budget == 47
    gap, reads = await _simulate(latency)
    assert gap <= rq.DEFAULT_INTERVAL_HOURS + 1 + latency < rq.FRESHNESS_HOURS
    assert reads >= 8  # it kept re-reading


@pytest.mark.parametrize("hand_at", [(1,), (40,), (100, 101), (5, 200, 333)])
async def test_a_hand_queued_run_never_stretches_the_gap(hand_at):
    gap, _ = await _simulate(46, hand_runs=hand_at)
    assert gap <= rq.DEFAULT_INTERVAL_HOURS + 1 + 46 < rq.FRESHNESS_HOURS


async def test_a_drain_slower_than_the_budget_is_what_breaks_the_window():
    # The documented limit, not a guarantee: past FRESHNESS - INTERVAL - 1 hours of drain latency, reads age out.
    gap, _ = await _simulate(48)
    assert gap >= rq.FRESHNESS_HOURS


async def test_a_run_cap_queues_the_stalest_reads_first():
    jobs = []
    for i in range(6):
        opts = {"vendors": [f"B{i}"], "market": "US"}
        jobs.append(mk(f"rij_{i}", brand=f"B{i}", options=opts, created=NOW - timedelta(days=30),
                       read=NOW - timedelta(hours=130 + 10 * ((i * 7) % 6))))
    jobs.append(mk("rij_fresh", brand="Fresh", options={"vendors": ["Fresh"], "market": "US"},
                   created=NOW - timedelta(days=30), read=NOW - timedelta(hours=2)))
    db = FakeDB(jobs)
    summary = await go(db, max_per_run=2)
    stalest = sorted(jobs[:6], key=lambda j: j["last_applied_at"])[:2]
    assert [r["brand"] for r in db.inserts] == [j["brand"] for j in stalest]
    assert summary["skipped"] == {rq.NOT_DUE_RECENT: 1, rq.RUN_CAP: 4}


# ------------------------------------------------------------------ dry run, flag, summary, CLI


async def test_a_dry_run_writes_nothing_and_says_what_it_would_queue():
    db = FakeDB([mk("rij_done")], runs={"rij_done": run()})
    summary = await go(db, dry_run=True)
    assert db.inserts == [] and summary["would_enqueue"] == 1 and "enqueued" not in summary
    assert only(summary)["decision"] == "would_enqueue" and only(summary)["market"] == "US"


async def test_a_dry_run_counts_against_the_cap_like_a_real_one():
    jobs = [mk(f"rij_{i}", brand=f"B{i}", options={"vendors": [f"B{i}"], "market": "US"}) for i in range(5)]
    real = await go(FakeDB(jobs), max_per_run=1)
    dry = await go(FakeDB(jobs), max_per_run=1, dry_run=True)
    assert real["enqueued"] == dry["would_enqueue"] == 1 and real["skipped"] == dry["skipped"]


class Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"the flag is off, but the tick used db.{name}")


async def test_the_flag_off_tick_does_nothing(monkeypatch):
    monkeypatch.delenv(rq.ENABLED_ENV, raising=False)
    assert await job.run_retailer_ingest_weekly_reenqueue_tick(db=Untouchable()) is None
    for off in ("0", "false", "", "no", "maybe"):
        monkeypatch.setenv(rq.ENABLED_ENV, off)
        assert await job.run_retailer_ingest_weekly_reenqueue_tick(db=Untouchable()) is None


@pytest.mark.parametrize("on", ["1", "true", "TRUE", " yes ", "on"])
async def test_the_flag_on_tick_runs_and_logs_one_warning_summary(monkeypatch, caplog, on):
    monkeypatch.setenv(rq.ENABLED_ENV, on)
    monkeypatch.delenv(rq.DRY_RUN_ENV, raising=False)
    monkeypatch.setenv(rq.INTERVAL_HOURS_ENV, "96")
    db = FakeDB([mk("rij_done", created=datetime.now(timezone.utc) - timedelta(days=400))], runs={"rij_done": run()})
    with caplog.at_level(logging.WARNING, logger=rq.__name__):
        summary = await job.run_retailer_ingest_weekly_reenqueue_tick(db=db)
    lines = [r for r in caplog.records if r.getMessage().startswith(rq.SUMMARY_MARKER)]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    logged = json.loads(lines[0].getMessage()[len(rq.SUMMARY_MARKER):])
    assert logged["cohorts_seen"] == 1 and "decisions" not in logged and logged["interval_hours"] == 96
    assert logged["enqueued"] == 1 and summary["dry_run"] is False
    assert logged["queued"] == [{"host": DOMAIN, "brand": "Example Brand", "market": "US",
                                 "market_source": "evidence", "job_id": db.inserts[0]["id"]}]


async def test_the_dry_run_env_makes_the_tick_write_nothing(monkeypatch):
    monkeypatch.setenv(rq.ENABLED_ENV, "1")
    monkeypatch.setenv(rq.DRY_RUN_ENV, "1")
    db = FakeDB([mk("rij_done", created=datetime.now(timezone.utc) - timedelta(days=30))], runs={"rij_done": run()})
    summary = await job.run_retailer_ingest_weekly_reenqueue_tick(db=db)
    assert db.inserts == [] and summary["would_enqueue"] == 1 and summary["dry_run"] is True


def test_the_cli_refuses_a_real_run_with_the_flag_off(monkeypatch, capsys):
    monkeypatch.delenv(rq.ENABLED_ENV, raising=False)

    async def no_connect():
        raise AssertionError("connected with the flag off")
    import db.database as dbmod
    monkeypatch.setattr(dbmod.database, "connect", no_connect)
    assert job.main([]) == 2
    assert json.loads(capsys.readouterr().out)["outcome"] == "disabled"
    with pytest.raises(SystemExit):
        job.main(["--now", "2026-10-21T10:00:00+00:00"])  # --now is for dry runs only


@pytest.mark.parametrize("raw", ["2026-11-02T10:11", "2026-11-02T10:11:00+00:00", "2026-11-02T11:11:00+01:00"])
def test_the_cli_dry_run_reads_now_as_utc_and_labels_its_lines(monkeypatch, capsys, raw):
    monkeypatch.delenv(rq.ENABLED_ENV, raising=False)
    import db.database as dbmod
    fake = FakeDB([mk("rij_done")], runs={"rij_done": run()})

    async def nothing():
        return None
    monkeypatch.setattr(dbmod.database, "connect", nothing)
    monkeypatch.setattr(dbmod.database, "disconnect", nothing)
    monkeypatch.setattr(dbmod.database, "fetch_all", fake.fetch_all)
    monkeypatch.setattr(dbmod.database, "fetch_one", fake.fetch_one)
    assert job.main(["--dry-run", "--host", "WWW.shop.example.com", "--now", raw]) == 0
    first, last = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert first["kind"] == "decision" and first["decision"] == "would_enqueue" and "options" not in first
    assert last["kind"] == "summary" and last["would_enqueue"] == 1 and "decisions" not in last
    assert last["at"] == NOW.isoformat() and fake.inserts == []
    assert job.parse_now("2026-11-02T10:11") == NOW


# ------------------------------------------------------------------ cohort identity


def test_cohorts_group_by_scope_key_with_us_folded_and_other_markets_apart():
    jobs = [mk("a", options={**BASE}), mk("b", options={**BASE, "market": "US"}),
            mk("c", options={**BASE, "market": "us", "require_ships_to_market": True}),
            mk("d", options={**BASE, "market": "SG"}),
            mk("e", options=json.dumps({**BASE, "exclude_handles": ["x"]})),
            mk("f", domain="other.example.com")]
    groups = rq.group_cohorts(jobs)
    assert sorted(sorted(j["id"] for j in g) for g in groups.values()) == [["a", "b", "c", "e"], ["d"], ["f"]]


def test_a_cohorts_jobs_are_ordered_by_creation_then_id():
    t = NOW
    jobs = [mk("z", created=t), mk("b", created=t - timedelta(days=1)), mk("a", created=t),
            mk("c", created=(t - timedelta(days=2)).replace(tzinfo=None)),
            mk("d", created=(t + timedelta(days=1)).isoformat())]
    (group,) = rq.group_cohorts(jobs).values()
    assert [j["id"] for j in group] == ["c", "b", "a", "z", "d"]


def test_unreadable_values_are_empty_not_a_crash():
    assert rq._options("{not json") == {} and rq._options(None) == {} and rq._options([1]) == {}
    assert rq._json("{not json") is None and rq._json({"a": 1}) == {"a": 1} and rq._dict("[1]") == {}
    assert rq.fold_host(" WWW.Example.COM ") == "example.com" and rq.fold_host(None) == ""
