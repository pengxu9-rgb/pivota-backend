"""The weekly re-enqueue: cohort selection, the once-a-week rule, the spread, the skip reasons, and the
market derivation (explicit, then the run's evidence, then skipped -- never a default)."""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

import jobs.retailer_ingest_weekly_reenqueue as job
from db import retailer_ingest as ledger
from services.retailer_ingest import weekly_reenqueue as rq

MONDAY = datetime(2026, 10, 19, tzinfo=timezone.utc)  # a Monday, 00:00Z: hour of week 0
DOMAIN = "shop.example.com"
BASE = {"vendors": ["Example Brand"]}


def key_of(domain=DOMAIN, brand="Example Brand", options=None):
    return ledger.scope_key(domain, brand, dict(BASE if options is None else options))


def at_slot(key, offset_h=0, weeks=2):
    """An instant `offset_h` hours after the cohort's slot, `weeks` weeks after MONDAY."""
    return MONDAY + timedelta(weeks=weeks, hours=rq.week_slot(key) + offset_h, minutes=11)


def mk(job_id, *, status="done", created=None, options=None, domain=DOMAIN, brand="Example Brand", lease=None):
    options = dict(BASE) if options is None else (options if isinstance(options, str) else dict(options))
    return {"id": job_id, "domain": domain, "brand": brand, "options": options,
            "status": status, "lease_until": lease, "created_at": created or MONDAY}


def proven(market="US", currency="USD", **over):
    """The evidence a clean applied run of a no-market job records."""
    ev = {"store_currency": currency, "checked_market": market,
          "presented": {"currencies": [currency], "pages_by_currency": {currency: 3}, "pages_without_cookie": 0,
                        "problems": {}, "unproven_values": []},
          "readback_rows": 4, "readback_rows_proven": 4}
    ev.update(over)
    return ev


class FakeDB:
    """The two reads and the enqueue INSERT, with migration 234's one-open-job-per-scope_key index."""

    def __init__(self, jobs, evidence=None, now=None):
        self.jobs = list(jobs)
        self.evidence = dict(evidence or {})
        self.now = now or MONDAY
        self.inserts = []
        self.evidence_reads = []

    async def fetch_all(self, sql, values=None):
        if sql is rq._JOBS_SQL:
            hosts = set(values["hosts"])
            return [dict(j) for j in self.jobs if values["all_hosts"] or rq.fold_host(j["domain"]) in hosts]
        if sql is rq._EVIDENCE_SQL:
            self.evidence_reads.append(list(values["ids"]))
            return [{"job_id": i, **self.evidence[i]} for i in values["ids"] if i in self.evidence]
        raise AssertionError(f"unexpected read: {sql}")

    async def fetch_one(self, sql, values=None):
        assert "INSERT INTO retailer_ingest_jobs" in sql
        open_keys = {ledger.scope_key(j["domain"], j["brand"], j["options"]) for j in self.jobs
                     if j["status"] in ledger.OPEN_STATUSES}
        if values["scope_key"] in open_keys:
            return None
        row = {"id": values["id"], "domain": values["domain"], "brand": values["brand"],
               "options": json.loads(values["options"]), "status": "queued", "lease_until": None,
               "created_at": self.now, "priority": values["priority"], "source": values["source"],
               "scope_key": values["scope_key"]}
        self.jobs.append(row)
        self.inserts.append(row)
        return {"id": row["id"]}


async def run(db, now, **kw):
    db.now = now
    return await rq.run_reenqueue(db=db, now=now, **kw)


def only(summary):
    (decision,) = summary["decisions"]
    return decision


# ------------------------------------------------------------------ market derivation


async def test_an_explicit_market_is_copied_onto_the_new_job_with_the_same_options():
    options = {**BASE, "market": "SG", "exclude_handles": ["a-gift"], "only_category": "beauty/makeup"}
    key = key_of(options=options)
    db = FakeDB([mk("rij_done", options=options)])
    summary = await run(db, at_slot(key))
    (row,) = db.inserts
    assert row["options"] == options
    assert row["source"] == "weekly_reenqueue:rij_done" and row["priority"] == 0
    assert row["scope_key"] == key
    assert summary["enqueued"] == 1 and summary["market_source"] == {"explicit": 1}
    assert db.evidence_reads == []  # an explicit market needs no evidence read


async def test_without_an_explicit_market_the_runs_evidence_names_it_and_the_key_is_unchanged():
    key = key_of()
    db = FakeDB([mk("rij_done")], evidence={"rij_done": proven()})
    summary = await run(db, at_slot(key))
    (row,) = db.inserts
    assert row["options"] == {**BASE, "market": "US"}
    assert row["scope_key"] == key  # "US" folds into the no-market key: the same cohort
    assert only(summary)["market_source"] == "evidence"
    assert db.evidence_reads == [["rij_done"]]


@pytest.mark.parametrize("evidence, why", [
    (None, rq.NO_APPLIED_RUN),
    (proven(checked_market=None), rq.NO_CHECKED_MARKET),
    (proven(checked_market="DE"), rq.NO_CHECKED_MARKET),
    (proven(store_currency=None), rq.NO_STORE_CURRENCY),
    # a US-checked run whose store says SGD: the pair does not match
    (proven(store_currency="SGD", presented={"pages_by_currency": {"SGD": 2}}), rq.CURRENCY_MARKET_MISMATCH),
    (proven(presented=None), rq.PRESENTED_CURRENCY_ABSENT),
    (proven(presented={"pages_by_currency": {}, "pages_without_cookie": 5}), rq.PRESENTED_CURRENCY_ABSENT),
    (proven(presented={"pages_by_currency": {"USD": 0}}), rq.PRESENTED_CURRENCY_ABSENT),
    (proven(presented={"pages_by_currency": {"USD": True}}), rq.PRESENTED_CURRENCY_ABSENT),
    (proven(presented='{"pages_by_currency": []}'), rq.PRESENTED_CURRENCY_ABSENT),
    (proven(presented="not json"), rq.PRESENTED_CURRENCY_ABSENT),
    (proven(presented={"pages_by_currency": {"USD": 3, "QAR": 1}}), rq.PRESENTED_CURRENCY_MISMATCH),
    (proven(presented={"pages_by_currency": {"QAR": 3}}), rq.PRESENTED_CURRENCY_MISMATCH),
    (proven(presented={"pages_by_currency": {"USD": 3}, "unproven_values": ["QAR"]}), rq.PRESENTED_CURRENCY_MISMATCH),
    (proven(presented={"pages_by_currency": {"USD": 3}, "problems": {"conflicting": 1}}),
     rq.PRESENTED_CURRENCY_MISMATCH),
    (proven(readback_rows=4, readback_rows_proven=3), rq.READBACK_UNPROVEN),
    (proven(readback_rows=0, readback_rows_proven=0), rq.READBACK_UNPROVEN),
    (proven(readback_rows=None, readback_rows_proven=None), rq.READBACK_UNPROVEN),
])
async def test_a_market_the_evidence_does_not_prove_is_skipped_never_defaulted(evidence, why):
    key = key_of()
    db = FakeDB([mk("rij_done")], evidence={} if evidence is None else {"rij_done": evidence})
    summary = await run(db, at_slot(key))
    assert db.inserts == []
    decision = only(summary)
    assert (decision["reason"], decision["detail"]) == (rq.MARKET_UNPROVEN, why)
    assert "market" not in decision
    assert summary["market_unproven_detail"] == {why: 1}
    assert summary["named"][rq.MARKET_UNPROVEN] == [{"host": DOMAIN, "brand": "Example Brand",
                                                     "prev_job_id": "rij_done", "detail": why}]


def test_evidence_with_lowercase_codes_and_a_json_string_payload_still_proves_its_market():
    ev = proven(market="sg", currency="sgd",
                presented=json.dumps({"pages_by_currency": {"sgd": 2}, "pages_without_cookie": 1}))
    assert rq.evidence_market(ev) == ("SG", None)
    assert rq.derive_market({}, ev) == ("SG", rq.MARKET_EVIDENCE, None)


@pytest.mark.parametrize("market", ["DE", "XX", "usa"])
def test_an_explicit_market_outside_the_ingest_markets_is_unproven_not_replaced(market):
    assert rq.derive_market({"market": market}, proven()) == (None, "", rq.EXPLICIT_MARKET_INVALID)


@pytest.mark.parametrize("market", [None, ""])
def test_an_empty_explicit_market_falls_through_to_the_evidence(market):
    assert rq.derive_market({"market": market}, proven()) == ("US", rq.MARKET_EVIDENCE, None)
    assert rq.derive_market({"market": market}, None) == (None, "", rq.NO_APPLIED_RUN)


def test_an_explicit_market_wins_over_contradicting_evidence():
    assert rq.derive_market({"market": " sg "}, proven()) == ("SG", rq.MARKET_EXPLICIT, None)


# ------------------------------------------------------------------ lineage and skip reasons


@pytest.mark.parametrize("status", ["queued", "apply_due", "held"])
async def test_a_cohort_with_an_open_job_is_skipped(status):
    key = key_of()
    db = FakeDB([mk("rij_old", created=MONDAY - timedelta(days=30)), mk("rij_open", status=status)],
                evidence={"rij_old": proven()})
    assert only(await run(db, at_slot(key)))["reason"] == rq.OPEN_JOB
    assert db.inserts == []


async def test_a_cohort_with_a_live_lease_is_skipped_and_an_expired_lease_is_not():
    key = key_of()
    now = at_slot(key)
    db = FakeDB([mk("rij_done", lease=now + timedelta(minutes=5))], evidence={"rij_done": proven()})
    assert only(await run(db, now))["reason"] == rq.LEASED
    db = FakeDB([mk("rij_done", lease=now - timedelta(minutes=5))], evidence={"rij_done": proven()})
    assert only(await run(db, now))["decision"] == "enqueue"


def test_a_naive_or_text_lease_is_read_as_utc():
    now = MONDAY + timedelta(days=1)
    assert rq.lineage_reason([mk("a", lease=(now + timedelta(minutes=1)).replace(tzinfo=None))], now)[0] == rq.LEASED
    assert rq.lineage_reason([mk("a", lease=(now + timedelta(minutes=1)).isoformat())], now)[0] == rq.LEASED
    assert rq.lineage_reason([mk("a", lease=now)], now)[0] is None


async def test_a_cohort_that_never_finished_is_skipped():
    key = key_of()
    db = FakeDB([mk("rij_f", status="failed")])
    assert only(await run(db, at_slot(key)))["reason"] == rq.NO_DONE_JOB


@pytest.mark.parametrize("status, reason", [("failed", rq.FAILED_LINEAGE), ("cancelled", rq.CANCELLED_LINEAGE),
                                            ("nothing", rq.NOTHING_LINEAGE)])
async def test_a_lineage_that_ended_after_its_latest_done_job_is_skipped(status, reason):
    key = key_of()
    db = FakeDB([mk("rij_done", created=MONDAY - timedelta(days=30)),
                 mk("rij_end", status=status, created=MONDAY - timedelta(days=20))],
                evidence={"rij_done": proven()})
    decision = only(await run(db, at_slot(key)))
    assert decision["reason"] == reason and decision["prev_job_id"] == "rij_done"
    assert db.inserts == []


async def test_a_later_done_job_reopens_a_failed_lineage():
    key = key_of()
    db = FakeDB([mk("rij_f", status="failed", created=MONDAY - timedelta(days=30)),
                 mk("rij_c", status="cancelled", created=MONDAY - timedelta(days=25)),
                 mk("rij_done", created=MONDAY - timedelta(days=20))],
                evidence={"rij_done": proven()})
    decision = only(await run(db, at_slot(key)))
    assert decision["decision"] == "enqueue" and decision["prev_job_id"] == "rij_done"
    assert db.inserts[0]["source"] == "weekly_reenqueue:rij_done"


async def test_the_options_of_the_latest_done_job_are_the_ones_copied():
    old = {**BASE, "market": "US", "max_products": 50}
    new = {**BASE, "market": "US", "max_products": 50, "exclude_handles": ["late-exclusion"]}
    key = key_of(options=old)
    assert key == key_of(options=new)  # an exclusion is bookkeeping, not scope
    db = FakeDB([mk("rij_a", options=old, created=MONDAY - timedelta(days=30)),
                 mk("rij_b", options=new, created=MONDAY - timedelta(days=20))])
    await run(db, at_slot(key))
    assert db.inserts[0]["options"] == new


async def test_an_excluded_host_is_skipped_whatever_its_spelling():
    key = key_of(domain="www.shop.example.com")
    db = FakeDB([mk("rij_done", domain="www.shop.example.com")], evidence={"rij_done": proven()})
    decision = only(await run(db, at_slot(key), excluded={"shop.example.com"}))
    assert decision["reason"] == rq.EXCLUDED_HOST and db.inserts == []


def test_excluded_hosts_and_the_run_cap_come_from_env(monkeypatch):
    monkeypatch.setenv(rq.EXCLUDED_HOSTS_ENV, " WWW.Shop.Example.com, other.example.com ,,")
    assert rq.excluded_hosts_from_env() == {"shop.example.com", "other.example.com"}
    monkeypatch.delenv(rq.EXCLUDED_HOSTS_ENV)
    assert rq.excluded_hosts_from_env() == set()
    for raw, want in ((None, rq.DEFAULT_MAX_PER_RUN), ("", rq.DEFAULT_MAX_PER_RUN), ("3", 3), (" 1 ", 1),
                      ("100", 100), ("0", rq.DEFAULT_MAX_PER_RUN), ("101", rq.DEFAULT_MAX_PER_RUN),
                      ("-2", rq.DEFAULT_MAX_PER_RUN), ("lots", rq.DEFAULT_MAX_PER_RUN)):
        if raw is None:
            monkeypatch.delenv(rq.MAX_PER_RUN_ENV, raising=False)
        else:
            monkeypatch.setenv(rq.MAX_PER_RUN_ENV, raw)
        assert rq.max_per_run_from_env() == want, raw


async def test_options_the_validator_now_refuses_are_skipped():
    options = {**BASE, "market": "US", "retired_option": True}
    key = key_of(options=options)
    db = FakeDB([mk("rij_done", options=options)])
    decision = only(await run(db, at_slot(key)))
    assert decision["reason"] == rq.INVALID_OPTIONS and "retired_option" in decision["detail"]
    assert summary_named(decision) and db.inserts == []


def summary_named(decision):
    return rq.summarize([decision], dry_run=False, now=MONDAY)["named"][decision["reason"]]


async def test_a_market_that_would_change_the_cohort_key_is_refused(monkeypatch):
    key = key_of()
    db = FakeDB([mk("rij_done")], evidence={"rij_done": proven(market="SG", currency="SGD",
                                                                presented={"pages_by_currency": {"SGD": 1}})})
    decision = only(await run(db, at_slot(key)))
    # a no-market (US) cohort whose run was checked as SG cannot be renamed SG: that is another cohort
    assert decision["reason"] == rq.SCOPE_CHANGED and db.inserts == []


async def test_an_insert_that_meets_a_job_opened_since_the_read_reports_exists():
    key = key_of()
    db = FakeDB([mk("rij_done")], evidence={"rij_done": proven()})

    async def raced(sql, values=None):
        return None  # ON CONFLICT DO NOTHING: another run (or a human) queued it first
    db.fetch_one = raced
    summary = await run(db, at_slot(key))
    assert only(summary)["reason"] == rq.EXISTS and summary["enqueued"] == 0


# ------------------------------------------------------------------ once a week, and the spread


def test_the_slot_is_stable_and_spreads_cohorts_across_the_week():
    assert rq.week_slot("rij:a.example.com:1") == rq.week_slot("rij:a.example.com:1")
    slots = {rq.week_slot(f"rij:host{i}.example.com:{i}") for i in range(2000)}
    assert slots == set(range(rq.HOURS_PER_WEEK))
    counts = [0] * rq.HOURS_PER_WEEK
    for i in range(16800):
        counts[rq.week_slot(f"rij:h{i}.example.com:{i}")] += 1
    assert max(counts) < 2 * min(counts)  # no hour carries twice another's load


def test_hour_of_week_counts_from_monday_midnight_utc():
    assert rq.hour_of_week(MONDAY) == 0
    assert rq.hour_of_week(MONDAY + timedelta(days=6, hours=23, minutes=59)) == 167
    assert rq.hour_of_week(MONDAY + timedelta(days=7)) == 0
    # an aware non-UTC instant is converted first
    assert rq.hour_of_week(datetime(2026, 10, 19, 1, 30, tzinfo=timezone(timedelta(hours=2)))) == 167
    assert rq.floor_hour(datetime(2026, 10, 19, 1, 30, 5, 7, tzinfo=timezone(timedelta(hours=2)))) == \
        datetime(2026, 10, 18, 23, tzinfo=timezone.utc)
    # a half-hour zone: the hour is floored in UTC, not in the instant's own zone
    assert rq.floor_hour(datetime(2026, 10, 19, 1, 50, tzinfo=timezone(timedelta(hours=5, minutes=30)))) == \
        datetime(2026, 10, 18, 20, tzinfo=timezone.utc)


@pytest.mark.parametrize("offset_h, due", [(-1, False), (0, True), (1, True), (23, True), (24, False), (100, False)])
def test_a_cohort_is_due_only_in_the_window_after_its_slot(offset_h, due):
    key = key_of()
    now = at_slot(key, offset_h)
    got = rq.due_reason(key=key, last_created=now - timedelta(days=30), now=now)
    assert got == (None if due else rq.NOT_DUE_SLOT)


def test_at_most_once_per_168_whole_hours():
    key = key_of()
    now = at_slot(key)
    hour = rq.floor_hour(now)
    assert rq.due_reason(key=key, last_created=hour - timedelta(hours=168), now=now) is None
    assert rq.due_reason(key=key, last_created=hour - timedelta(hours=168) + timedelta(minutes=59), now=now) is None
    assert rq.due_reason(key=key, last_created=hour - timedelta(hours=167), now=now) == rq.NOT_DUE_RECENT
    assert rq.due_reason(key=key, last_created=hour - timedelta(hours=166, minutes=30), now=now) == rq.NOT_DUE_RECENT
    # whole hours: a job created at hh:59 a week ago counts from hh:00, so the slot does not drift
    assert rq.due_reason(key=key, last_created=hour - timedelta(hours=167, minutes=1), now=now) is None
    assert rq.due_reason(key=key, last_created=now, now=now) == rq.NOT_DUE_RECENT


async def test_the_newest_job_of_any_status_starts_the_week():
    key = key_of()
    now = at_slot(key)
    db = FakeDB([mk("rij_done", created=now - timedelta(days=30)),
                 mk("rij_done2", created=now - timedelta(days=3))], evidence={"rij_done2": proven()})
    assert only(await run(db, now))["reason"] == rq.NOT_DUE_RECENT


async def test_hourly_runs_queue_each_cohort_once_a_week_and_never_twice():
    key = key_of()
    db = FakeDB([mk("rij_done", created=MONDAY - timedelta(days=30))], evidence={"rij_done": proven()})
    week2 = at_slot(key)
    assert (await run(db, week2))["enqueued"] == 1
    assert only(await run(db, week2))["reason"] == rq.OPEN_JOB             # same hour, re-run: idempotent
    assert only(await run(db, week2 + timedelta(hours=1)))["reason"] == rq.OPEN_JOB
    db.jobs[-1]["status"] = "done"                                            # the drain finished it
    db.evidence[db.jobs[-1]["id"]] = proven()
    for hours in (1, 2, 23, 24, 100, 167):
        assert only(await run(db, week2 + timedelta(hours=hours)))["reason"] in (rq.NOT_DUE_RECENT, rq.NOT_DUE_SLOT)
    assert len(db.inserts) == 1
    week3 = week2 + timedelta(hours=168)
    decision = only(await run(db, week3))
    assert decision["decision"] == "enqueue" and len(db.inserts) == 2
    # the second one copies the first re-crawl's options, explicit market included
    assert db.inserts[1]["options"] == {**BASE, "market": "US"}
    assert decision["market_source"] == "explicit"


async def test_a_missed_tick_is_caught_up_inside_the_window():
    key = key_of()
    db = FakeDB([mk("rij_done", created=MONDAY - timedelta(days=30))], evidence={"rij_done": proven()})
    assert (await run(db, at_slot(key, offset_h=5)))["enqueued"] == 1


async def test_a_run_cap_defers_the_cohorts_with_the_most_window_left():
    jobs, keys = [], []
    for i in range(30):
        brand = f"Brand {i}"
        opts = {"vendors": [brand]}
        keys.append(ledger.scope_key(DOMAIN, brand, opts))
        jobs.append(mk(f"rij_{i}", brand=brand, options={**opts, "market": "US"}, created=MONDAY - timedelta(days=30)))
    now = MONDAY + timedelta(weeks=2, hours=150)
    due = sorted((k for k in keys if (rq.hour_of_week(now) - rq.week_slot(k)) % 168 < rq.SLOT_WINDOW_HOURS),
                 key=lambda k: (-((rq.hour_of_week(now) - rq.week_slot(k)) % 168), k))
    assert len(due) >= 3, "the fixture needs at least three cohorts in the window"
    db = FakeDB(jobs)
    summary = await run(db, now, max_per_run=2)
    assert summary["enqueued"] == 2 and summary["skipped"][rq.RUN_CAP] == len(due) - 2
    assert [r["scope_key"] for r in db.inserts] == due[:2]


# ------------------------------------------------------------------ dry run, flag, summary


async def test_a_dry_run_writes_nothing_and_says_what_it_would_queue():
    key = key_of()
    db = FakeDB([mk("rij_done")], evidence={"rij_done": proven()})
    summary = await run(db, at_slot(key), dry_run=True)
    assert db.inserts == [] and summary["would_enqueue"] == 1 and "enqueued" not in summary
    assert only(summary)["decision"] == "would_enqueue" and only(summary)["market"] == "US"


async def test_a_dry_run_counts_against_the_cap_like_a_real_one():
    jobs = []
    for i in range(30):
        jobs.append(mk(f"rij_{i}", brand=f"B{i}", options={"vendors": [f"B{i}"], "market": "US"},
                       created=MONDAY - timedelta(days=30)))
    now = MONDAY + timedelta(weeks=2, hours=150)
    real = await run(FakeDB(jobs), now, max_per_run=1)
    dry = await run(FakeDB(jobs), now, max_per_run=1, dry_run=True)
    assert real["enqueued"] == dry["would_enqueue"] == 1
    assert real["skipped"] == dry["skipped"]


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
    monkeypatch.setenv(rq.EXCLUDED_HOSTS_ENV, "other.example.com")
    db = FakeDB([mk("rij_done", created=MONDAY - timedelta(days=400))], evidence={"rij_done": proven()})
    with caplog.at_level(logging.WARNING, logger=rq.__name__):
        summary = await job.run_retailer_ingest_weekly_reenqueue_tick(db=db)
    lines = [r for r in caplog.records if r.getMessage().startswith(rq.SUMMARY_MARKER)]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    logged = json.loads(lines[0].getMessage()[len(rq.SUMMARY_MARKER):])
    assert logged["cohorts_seen"] == 1 and "decisions" not in logged
    assert logged["enqueued"] + sum(logged["skipped"].values()) == 1
    assert summary["dry_run"] is False


async def test_the_dry_run_env_makes_the_tick_write_nothing(monkeypatch, caplog):
    monkeypatch.setenv(rq.ENABLED_ENV, "1")
    monkeypatch.setenv(rq.DRY_RUN_ENV, "1")
    key = key_of()
    db = FakeDB([mk("rij_done", created=MONDAY - timedelta(days=30))], evidence={"rij_done": proven()})
    seen = {}
    real_run = rq.run_reenqueue

    async def fake_run(**kw):
        seen.update(kw)
        return await real_run(**{**kw, "now": at_slot(key)})
    monkeypatch.setattr(rq, "run_reenqueue", fake_run)
    with caplog.at_level(logging.WARNING, logger=rq.__name__):
        summary = await job.run_retailer_ingest_weekly_reenqueue_tick(db=db)
    assert seen["dry_run"] is True and db.inserts == [] and summary["would_enqueue"] == 1
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith(rq.SUMMARY_MARKER)]
    logged = json.loads(line[len(rq.SUMMARY_MARKER):])
    assert logged["queued"] == [{"host": DOMAIN, "brand": "Example Brand", "market": "US",
                                 "market_source": "evidence", "job_id": None}]


async def test_the_summary_counts_every_cohort_once_by_reason():
    a, b, c = (f"Brand {x}" for x in "abc")
    jobs = [mk("rij_a", brand=a, options={"vendors": [a]}, created=MONDAY - timedelta(days=30)),
            mk("rij_b", brand=b, options={"vendors": [b]}, status="queued"),
            mk("rij_c", brand=c, options={"vendors": [c], "market": "SG"}, created=MONDAY - timedelta(days=30))]
    now = MONDAY + timedelta(weeks=3)
    summary = await run(FakeDB(jobs), now)
    assert summary["cohorts_seen"] == 3
    assert summary["enqueued"] + sum(summary["skipped"].values()) == 3


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


def test_the_cli_dry_run_prints_one_line_per_cohort_and_the_summary(monkeypatch, capsys):
    monkeypatch.delenv(rq.ENABLED_ENV, raising=False)
    import db.database as dbmod
    key = key_of()
    fake = FakeDB([mk("rij_done", created=MONDAY - timedelta(days=30))], evidence={"rij_done": proven()})

    async def nothing():
        return None
    monkeypatch.setattr(dbmod.database, "connect", nothing)
    monkeypatch.setattr(dbmod.database, "disconnect", nothing)
    monkeypatch.setattr(dbmod.database, "fetch_all", fake.fetch_all)
    monkeypatch.setattr(dbmod.database, "fetch_one", fake.fetch_one)
    assert job.main(["--dry-run", "--host", "WWW.shop.example.com", "--now", at_slot(key).isoformat()]) == 0
    first, last = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert first["decision"] == "would_enqueue" and "options" not in first
    assert last["would_enqueue"] == 1 and "decisions" not in last and fake.inserts == []


# ------------------------------------------------------------------ cohort identity


def test_cohorts_group_by_scope_key_with_us_folded_and_other_markets_apart():
    jobs = [mk("a", options={**BASE}), mk("b", options={**BASE, "market": "US"}),
            mk("c", options={**BASE, "market": "us"}), mk("d", options={**BASE, "market": "SG"}),
            mk("e", options=json.dumps({**BASE, "exclude_handles": ["x"]})),
            mk("f", domain="other.example.com")]
    groups = rq.group_cohorts(jobs)
    assert sorted(sorted(j["id"] for j in g) for g in groups.values()) == [["a", "b", "c", "e"], ["d"], ["f"]]


def test_a_cohorts_jobs_are_ordered_by_creation_then_id():
    t = MONDAY
    jobs = [mk("z", created=t), mk("b", created=t - timedelta(days=1)), mk("a", created=t),
            mk("c", created=(t - timedelta(days=2)).replace(tzinfo=None)), mk("d", created=(t + timedelta(days=1)).isoformat())]
    (group,) = rq.group_cohorts(jobs).values()
    assert [j["id"] for j in group] == ["c", "b", "a", "z", "d"]


def test_unreadable_options_are_an_empty_scope_not_a_crash():
    assert rq._options("{not json") == {} and rq._options(None) == {} and rq._options([1]) == {}
    assert rq._json("{not json") is None and rq._json({"a": 1}) == {"a": 1}
    assert rq.fold_host(" WWW.Example.COM ") == "example.com" and rq.fold_host(None) == ""
