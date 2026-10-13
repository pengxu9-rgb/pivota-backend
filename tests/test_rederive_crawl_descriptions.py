"""scripts/rederive_crawl_descriptions.py: which jobs, which rows, the write guard, the gates, and
the write phase's batching, lock and dry-run behaviour -- driven through fakes. The UPDATE's own SQL
semantics are executed on real Postgres in tests/test_rederive_crawl_descriptions_postgres.py.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.rederive_crawl_descriptions as rd

HOST = "a.example.com"
OTHER = "b.example.com"
LONG_OLD = "Old crawled copy of this example product, long enough to clear the fifty character floor."
LONG_NEW = "New clean copy of this example product, long enough to clear the fifty character floor too."
NOON = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _row(pk, handle, desc, *, crawled=None, stage="published", host=HOST, ck=None, tags='["serum"]'):
    return {
        "product_key": pk, "content_key": ck or f"ck-{pk}",
        "canonical_url": f"https://{host}/products/{handle}", "source_domain": host, "host": host,
        "title": "Example Serum", "description": desc, "image_url": "https://cdn.example.com/x.jpg",
        "category_path": "beauty/skincare", "tags": tags, "demographic": None,
        "use_case_tags": "[]", "lifestyle_tags": "[]", "pdp_scope": None,
        "source_system": "catalog_enrichment_agent_v1", "pdp_lifecycle_stage": stage,
        "crawled_copy": crawled,
    }


def _pdp(pk, handle, desc, host=HOST):
    return {"product_key": pk, "description": desc, "canonical_url": f"https://{host}/products/{handle}"}


def _job(job_id, *, host=HOST, scope=None, source=None, status="done", day=11, hour=0):
    options = {"source": source} if source else {}
    return {"id": job_id, "domain": host, "brand": "Example", "options": options,
            "scope_key": scope or f"scope-{job_id}", "status": status,
            "updated_at": datetime(2026, 10, day, hour, tzinfo=timezone.utc), "host": host}


class _Tx:
    def __init__(self, db):
        self.db = db

    async def start(self):
        self.db.events.append("begin_read")

    async def rollback(self):
        self.db.events.append("rollback_read")

    async def __aenter__(self):
        self.db.batches.append([])
        self.db.events.append("begin_write")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.db.events.append("commit" if exc_type is None else "rollback_write")
        return False


class FakeDB:
    """Population reads return a SNAPSHOT (`rows`); the UPDATE runs against the live `catalog`, so a
    test moves a row between the read and the write by editing `catalog`."""

    def __init__(self, rows, jobs):
        self.rows = [dict(r) for r in rows]
        self.jobs = list(jobs)
        self.catalog = {r["product_key"]: dict(r) for r in rows}
        self.events, self.batches, self.updates = [], [], []
        self.fail_next_update = None

    def transaction(self):
        return _Tx(self)

    async def connect(self):
        self.events.append("connect")

    async def disconnect(self):
        self.events.append("disconnect")

    async def execute(self, sql, values=None):
        self.events.append(sql.strip())

    async def fetch_all(self, sql, values=None):
        if "FROM catalog_products" in sql:
            return [dict(r) for r in self.rows]
        if "FROM retailer_ingest_jobs" in sql:
            return [dict(j) for j in self.jobs if j["host"] in values["hosts"]]
        raise AssertionError(sql)

    async def fetch_one(self, sql, values=None):
        assert "RETURNING product_key" in sql
        if self.fail_next_update is not None:
            exc, self.fail_next_update = self.fail_next_update, None
            raise exc
        self.updates.append(values)
        self.batches[-1].append(values["pk"])
        row = self.catalog.get(values["pk"])
        if (row is None or row.get("suppressed_at") is not None or row["description"] != values["old"]
                or row.get("pdp_lifecycle_stage") != values["old_stage"]
                or (row.get("pdp_lifecycle_stage") or "") in ("hold", "archived")):
            return None
        row.update(description=values["new"], pdp_lifecycle_stage=values["stage"])
        return {"product_key": values["pk"]}


def _deps(db, crawls, *, now=NOON, clock=None, lock_error=None):
    """crawls: job id -> (pdps, products). The fake plan hands the pdps straight back."""
    state = SimpleNamespace(lock_enters=0, refreshed=[], sleeps=[], crawled=[], reconnects=0)

    @contextlib.asynccontextmanager
    async def lock():
        if lock_error is not None:
            raise lock_error
        state.lock_enters += 1
        yield 0.0

    async def crawl(job):
        state.crawled.append(job["id"])
        value = crawls[job["id"]]
        if isinstance(value, Exception):
            raise value
        return value

    async def refresh(ck, *, refresh_source, db):
        assert refresh_source == rd.REFRESH_SOURCE
        state.refreshed.append(ck)

    async def reconnect():
        state.reconnects += 1

    async def sleep(seconds):
        state.sleeps.append(seconds)

    ticks = iter(clock) if clock is not None else None
    deps = SimpleNamespace(
        db=db, crawl=crawl, plan=lambda job, records, rows: {p["product_key"]: p for p in records},
        lock=lock, refresh=refresh, reconnect=reconnect, sleep=sleep, now=lambda: now,
        clock=(lambda: next(ticks)) if ticks else (lambda: 0.0), started=0.0,
    )
    return deps, state


async def _run(argv, db, crawls, **kw):
    deps, state = _deps(db, crawls, **kw)
    code, summary = await rd.run(rd.parse_args(argv), deps)
    return code, summary, state


# --- the frozen old producer --------------------------------------------------------------------------

def test_the_legacy_producer_is_the_old_flattening():
    body = "<style>.x{}</style><p>Soft&nbsp;skin &amp; glow</p><!-- c --><ul><li>One</li><li>Two</li></ul>"
    assert rd.legacy_body_html_to_text(body) == "Soft\xa0skin & glow One Two".replace("\xa0", " ")
    assert rd.legacy_body_html_to_text(None) == ""
    long = ("word " * 600).strip()
    out = rd.legacy_body_html_to_text(long)
    assert len(out) <= 2000 and out.endswith("word") and not out.endswith(" ")


# --- step 1: which jobs -------------------------------------------------------------------------------

def test_only_the_latest_done_storefront_job_per_cohort_newest_first():
    jobs = [
        _job("old-same-scope", scope="s1", day=10),
        _job("new-same-scope", scope="s1", day=12),
        _job("other-cohort", scope="s2", day=11),
        _job("affiliate", source="affiliate_feed", day=13),
        _job("markets", source="shopify_markets", day=13),
        _job("not-done", status="failed", day=13),
        _job("too-old", scope="s3", day=1),
    ]
    picked, excluded = rd.select_jobs(jobs, jobs_since=datetime(2026, 10, 9, tzinfo=timezone.utc))
    assert [j["id"] for j in picked] == ["new-same-scope", "other-cohort"]
    assert excluded == ["affiliate_feed", "shopify_markets"]
    unbounded, _ = rd.select_jobs(jobs, jobs_since=None)
    assert [j["id"] for j in unbounded] == ["new-same-scope", "other-cohort", "too-old"]


def test_job_options_stored_as_json_text_are_read():
    job = {**_job("j"), "options": '{"source": "affiliate_feed"}'}
    picked, excluded = rd.select_jobs([job], jobs_since=None)
    assert picked == [] and excluded == ["affiliate_feed"]


# --- step 4: the guard ----------------------------------------------------------------------------------

def _classify(row, pdp, legacy=None, allow_demote=False):
    return rd.classify_row(row, pdp, legacy, allow_demote=allow_demote)


def test_the_crawled_copy_arm_owns_the_row():
    d = _classify(_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _pdp("p1", "h1", LONG_NEW))
    assert d.outcome == rd.WRITE and d.new == LONG_NEW


def test_the_legacy_body_arm_owns_the_row():
    d = _classify(_row("p1", "h1", LONG_OLD, crawled="a clean copy already re-crawled"),
                  _pdp("p1", "h1", LONG_NEW), legacy=LONG_OLD)
    assert d.outcome == rd.WRITE


def test_copy_matching_neither_arm_is_not_crawl_owned():
    d = _classify(_row("p1", "h1", LONG_OLD, crawled=LONG_OLD + " "), _pdp("p1", "h1", LONG_NEW),
                  legacy=LONG_OLD.upper())
    assert d.outcome == rd.NOT_CRAWL_OWNED
    assert _classify(_row("p1", "h1", LONG_OLD), _pdp("p1", "h1", LONG_NEW)).outcome == rd.NOT_CRAWL_OWNED


def test_equal_text_is_unchanged_even_when_owned():
    d = _classify(_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _pdp("p1", "h1", LONG_OLD))
    assert d.outcome == rd.UNCHANGED


def test_a_written_row_reads_back_unchanged_on_a_rerun():
    """After a write the stored text matches neither arm any more; the re-run must call it unchanged,
    not not_crawl_owned."""
    d = _classify(_row("p1", "h1", LONG_NEW, crawled=LONG_OLD), _pdp("p1", "h1", LONG_NEW), legacy=LONG_OLD)
    assert d.outcome == rd.UNCHANGED


def test_a_row_the_plan_does_not_produce_is_not_in_feed_and_an_empty_one_is_never_written():
    assert _classify(_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), None).outcome == rd.NOT_IN_FEED
    d = _classify(_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _pdp("p1", "h1", None))
    assert d.outcome == rd.WOULD_EMPTY


# --- step 6: the demotion gate ----------------------------------------------------------------------------

def test_a_demoting_row_is_skipped_without_allow_demote_and_written_with_it():
    row = _row("p1", "h1", LONG_OLD, crawled=LONG_OLD, stage="published")
    short = "Too short to stay published."
    held = _classify(row, _pdp("p1", "h1", short))
    assert held.would_demote and held.stage == "draft" and held.outcome == rd.SKIP_DEMOTE
    allowed = _classify(row, _pdp("p1", "h1", short), allow_demote=True)
    assert allowed.would_demote and allowed.outcome == rd.WRITE


def test_the_same_stage_is_not_a_demotion():
    d = _classify(_row("p1", "h1", LONG_OLD, crawled=LONG_OLD, stage="published"), _pdp("p1", "h1", LONG_NEW))
    assert d.stage == "published" and not d.would_demote and d.outcome == rd.WRITE


# --- the safety-cue rule ------------------------------------------------------------------------------

def test_a_row_that_loses_a_safety_cue_is_never_written():
    old = LONG_OLD + " Avoid contact with eyes."
    d = _classify(_row("p1", "h1", old, crawled=old), _pdp("p1", "h1", LONG_NEW), allow_demote=True)
    assert d.outcome == rd.SKIP_SAFETY and d.safety_row
    assert d.lost_cues == ["avoid", "eyes"]


def test_a_cue_kept_in_the_new_text_is_not_lost():
    old = LONG_OLD + " Discontinue use if irritation occurs."
    new = LONG_NEW + " Discontinue use if irritation occurs."
    d = _classify(_row("p1", "h1", old, crawled=old), _pdp("p1", "h1", new))
    assert d.outcome == rd.WRITE and d.safety_row and d.lost_cues == []


@pytest.mark.parametrize("old_word,new_word", [
    ("irritation2", "irritation"),
    ("irritation", "irritation2"),
    ("irritation¹", "irritation"),
    ("eyes3", "eyes"),
])
def test_a_footnote_digit_glued_to_a_cue_is_still_that_cue(old_word, new_word):
    old = f"{LONG_OLD} May cause {old_word}."
    new = f"{LONG_NEW} May cause {new_word}."
    assert rd.lost_safety_cues(old, new) == []
    d = _classify(_row("p1", "h1", old, crawled=old), _pdp("p1", "h1", new))
    assert d.outcome == rd.WRITE


def test_a_glued_cue_that_really_goes_is_still_lost():
    old = f"{LONG_OLD} Keep away from eyes2."
    assert rd.lost_safety_cues(old, LONG_NEW) == ["eyes", "keep away from"]


# --- the run: dry run, apply, raced, batches, lock ------------------------------------------------------

def _population():
    rows = [
        _row("p1", "h1", LONG_OLD, crawled=LONG_OLD),                       # rewrite (crawled arm)
        _row("p2", "h2", LONG_OLD + " two", crawled="clean"),               # rewrite (legacy arm)
        _row("p3", "h3", LONG_OLD + " three", crawled=LONG_OLD + " three"),  # unchanged
        _row("p4", "h4", "Hand-edited copy that no crawl wrote, long enough."),  # not crawl owned
        _row("p5", "h5", LONG_OLD + " five", crawled=LONG_OLD + " five"),    # not in feed
        _row("p6", "h6", LONG_OLD + " six", crawled=LONG_OLD + " six"),      # would demote
        _row("p7", "h7", LONG_OLD + " Avoid eyes.", crawled=LONG_OLD + " Avoid eyes."),  # safety lost
    ]
    pdps = [
        _pdp("p1", "h1", LONG_NEW), _pdp("p2", "h2", LONG_NEW + " two"),
        _pdp("p3", "h3", LONG_OLD + " three"), _pdp("p4", "h4", LONG_NEW + " four"),
        _pdp("p6", "h6", "Short now."), _pdp("p7", "h7", LONG_NEW + " seven"),
    ]
    products = [{"handle": "h2", "body_html": f"<p>{LONG_OLD} two</p>"}]
    return rows, pdps, products


async def test_the_dry_run_classifies_everything_and_writes_nothing():
    rows, pdps, products = _population()
    db = FakeDB(rows, [_job("j1")])
    code, s, state = await _run([], db, {"j1": (pdps, products)})
    assert code == 0
    assert {k: s[k] for k in rd.SUMMARY_KEYS} == {
        "rows": 7, "would_rewrite": 4, "written": 0, "raced": 0, "unchanged": 1, "not_crawl_owned": 1,
        "not_in_feed": 1, "would_demote": 1, "skipped_demote": 1, "safety_rows": 1, "safety_cue_lost": 1}
    assert s["not_in_feed_url_in_plan"] == 0
    assert db.updates == [] and db.batches == []
    assert state.lock_enters == 0 and state.refreshed == [] and state.sleeps == []
    assert "begin_write" not in db.events and db.events.count("SET TRANSACTION READ ONLY") == 2


async def test_apply_writes_only_the_rows_that_pass_every_gate():
    rows, pdps, products = _population()
    db = FakeDB(rows, [_job("j1")])
    code, s, state = await _run(["--apply"], db, {"j1": (pdps, products)})
    assert code == 0
    assert s["written"] == 2 and s["raced"] == 0 and s["skipped_demote"] == 1 and s["safety_cue_lost"] == 1
    assert sorted(u["pk"] for u in db.updates) == ["p1", "p2"]
    assert db.catalog["p1"]["description"] == LONG_NEW and db.catalog["p6"]["description"] == rows[5]["description"]
    assert state.lock_enters == 1 and sorted(state.refreshed) == ["ck-p1", "ck-p2"]
    update = next(u for u in db.updates if u["pk"] == "p1")
    assert update["old"] == LONG_OLD and update["old_stage"] == "published" and update["stage"] == "published"
    assert update["use_case_tags"].startswith("[") and update["lifestyle_tags"].startswith("[")


async def test_allow_demote_writes_the_demoting_row():
    rows, pdps, products = _population()
    db = FakeDB(rows, [_job("j1")])
    _, s, _ = await _run(["--apply", "--allow-demote"], db, {"j1": (pdps, products)})
    assert s["written"] == 3 and s["skipped_demote"] == 0 and s["would_demote"] == 1
    assert db.catalog["p6"]["description"] == "Short now." and db.catalog["p6"]["pdp_lifecycle_stage"] == "draft"


@pytest.mark.parametrize("field,value", [("description", "Edited after the read, by someone else entirely."),
                                         ("pdp_lifecycle_stage", "validated")])
async def test_a_row_that_moved_since_the_read_is_raced_not_written(field, value):
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    db.catalog["p1"][field] = value
    code, s, state = await _run(["--apply"], db, {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])})
    assert code == 0 and s["raced"] == 1 and s["written"] == 0
    assert db.catalog["p1"][field] == value and state.refreshed == []


async def test_writes_commit_in_batches_each_under_the_lock_with_a_pause_between():
    rows = [_row(f"p{i}", f"h{i}", LONG_OLD, crawled=LONG_OLD) for i in range(5)]
    pdps = [_pdp(f"p{i}", f"h{i}", LONG_NEW) for i in range(5)]
    db = FakeDB(rows, [_job("j1")])
    _, s, state = await _run(["--apply", "--batch-size", "2", "--batch-sleep-s", "0.5"], db, {"j1": (pdps, [])})
    assert [len(b) for b in db.batches] == [2, 2, 1]
    assert db.events.count("commit") == 3 and state.lock_enters == 3 and state.sleeps == [0.5, 0.5]
    assert s["written"] == 5


async def test_the_default_batch_is_two_hundred():
    assert rd.parse_args([]).batch_size == 200


async def test_a_busy_lock_stops_the_run_and_writes_nothing():
    class CatalogWriteLockBusy(Exception):
        pass

    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _row("q1", "h1", LONG_OLD, crawled=LONG_OLD, host=OTHER)]
    db = FakeDB(rows, [_job("j1"), _job("j2", host=OTHER)])
    crawls = {"j1": ([_pdp("p1", "h1", LONG_NEW)], []), "j2": ([_pdp("q1", "h1", LONG_NEW, host=OTHER)], [])}
    code, s, state = await _run(["--apply"], db, crawls, lock_error=CatalogWriteLockBusy(600.0))
    assert code == 3 and s["stopped"] and s["lock_unwritten"] == 1 and s["written"] == 0
    assert db.updates == [] and len(state.crawled) == 1


async def test_a_transport_failure_is_healed_and_the_batch_retried():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    db.fail_next_update = ConnectionResetError("reset")
    code, s, state = await _run(["--apply"], db, {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])})
    assert code == 0 and s["written"] == 1 and state.reconnects == 2  # the start, then the heal


async def test_a_data_error_fails_the_batch_and_the_exit_code():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    db.fail_next_update = ValueError("bad value")
    code, s, _ = await _run(["--apply"], db, {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])})
    assert code == 3 and s["update_failed"] == 1 and s["written"] == 0


@pytest.mark.parametrize("hour,minute,refused", [(5, 14, False), (5, 15, True), (6, 14, True), (6, 15, False)])
async def test_apply_refuses_to_start_inside_the_sweep_window(hour, minute, refused):
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    now = datetime(2026, 10, 13, hour, minute, tzinfo=timezone.utc)
    code, s, _ = await _run(["--apply"], db, {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])}, now=now)
    if refused:
        assert code == 2 and s == {} and db.events == []
    else:
        assert code == 0 and s["written"] == 1


async def test_a_dry_run_inside_the_window_still_runs():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    now = datetime(2026, 10, 13, 5, 30, tzinfo=timezone.utc)
    code, s, _ = await _run([], db, {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])}, now=now)
    assert code == 0 and s["would_rewrite"] == 1


# --- hosts, jobs and budget in the run -----------------------------------------------------------------

async def test_a_host_without_a_storefront_job_is_skipped_and_reported():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("aff", source="affiliate_feed")])
    _, s, state = await _run([], db, {})
    assert s["rows"] == 1 and s["host_skipped"] == 1 and s["would_rewrite"] == 0 and state.crawled == []


async def test_a_failed_crawl_skips_its_host():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    _, s, _ = await _run([], db, {"j1": rd.CrawlRefused("crawl completeness was not proven")})
    assert s["host_skipped"] == 1 and s["jobs_failed"] == 1


async def test_rows_come_from_the_newest_job_that_produces_them():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _row("p2", "h2", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("new", scope="s1", day=12), _job("old", scope="s2", day=10)])
    crawls = {"new": ([_pdp("p1", "h1", LONG_NEW)], []),
              "old": ([_pdp("p1", "h1", "stale text " * 10), _pdp("p2", "h2", LONG_NEW)], [])}
    _, s, state = await _run(["--apply"], db, crawls)
    assert state.crawled == ["new", "old"] and s["written"] == 2
    assert db.catalog["p1"]["description"] == LONG_NEW


async def test_no_further_job_is_crawled_once_every_row_is_matched():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("new", scope="s1", day=12), _job("old", scope="s2", day=10)])
    _, _, state = await _run([], db, {"new": ([_pdp("p1", "h1", LONG_NEW)], []), "old": ([], [])})
    assert state.crawled == ["new"]


async def test_a_key_planned_on_another_listing_is_not_this_rows_copy():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    _, s, _ = await _run([], db, {"j1": ([_pdp("p1", "other-page", LONG_NEW)], [])})
    assert s["not_in_feed"] == 1 and s["would_rewrite"] == 0


async def test_hosts_limits_the_run():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _row("q1", "h1", LONG_OLD, crawled=LONG_OLD, host=OTHER)]
    db = FakeDB(rows, [_job("j1"), _job("j2", host=OTHER)])
    _, s, state = await _run(["--hosts", f"www.{OTHER}"], db, {"j2": ([_pdp("q1", "h1", LONG_NEW, host=OTHER)], [])})
    assert s["rows"] == 1 and state.crawled == ["j2"]


async def test_the_time_budget_stops_new_crawls():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _row("q1", "h1", LONG_OLD, crawled=LONG_OLD, host=OTHER)]
    db = FakeDB(rows, [_job("j1"), _job("j2", host=OTHER)])
    crawls = {"j1": ([_pdp("p1", "h1", LONG_NEW)], []), "j2": ([_pdp("q1", "h1", LONG_NEW, host=OTHER)], [])}
    _, s, state = await _run(["--budget-s", "10"], db, crawls, clock=[0.0, 1.0, 50.0, 60.0, 61.0])
    assert len(state.crawled) == 1 and s["host_skipped"] == 1


async def test_the_summary_names_every_required_count():
    rows, pdps, products = _population()
    _, s, _ = await _run([], FakeDB(rows, [_job("j1")]), {"j1": (pdps, products)})
    for key in ("rows", "would_rewrite", "written", "raced", "unchanged", "not_crawl_owned", "not_in_feed",
                "would_demote", "skipped_demote", "safety_rows", "safety_cue_lost"):
        assert key in s


def test_allow_demote_needs_apply_and_since_all_drops_both_bounds():
    with pytest.raises(SystemExit):
        rd.parse_args(["--allow-demote"])
    opts = rd.parse_args(["--since", "all"])
    assert opts.since is None and opts.jobs_since is None
    opts = rd.parse_args([])
    assert opts.since == datetime(2026, 10, 10) and opts.jobs_since == datetime(2026, 10, 9, tzinfo=timezone.utc)


# --- the crawl and the plan, for real -------------------------------------------------------------------

def _crawl_job():
    return {**_job("j1"), "options": {"vendors": ["Example"]}}


def _shopify_product(handle="glow-serum", body="<p>A light serum for daily use that leaves skin soft all day.</p>"):
    return {"id": 101, "handle": handle, "title": "Glow Serum", "vendor": "Example", "product_type": "Serum",
            "body_html": body, "tags": ["serum"], "images": [{"src": "https://cdn.example.com/a.jpg"}],
            "variants": [{"id": 1, "title": "Default Title", "price": "25.00", "available": True, "sku": "GS1"}],
            "options": [{"name": "Title"}]}


def test_the_plan_yields_each_rows_key_with_the_producers_text():
    import services.curated_brand_feed as feed

    record = feed.shopify_product_to_record(_shopify_product(), domain=HOST, category_path="beauty/skincare",
                                            brand_override="Example", currency="USD", source_role="retailer",
                                            retailer_name="Example Shop")
    planned = rd.plan_rows({"id": "j", "domain": HOST, "brand": "Example", "options": {}}, [record], [])
    (pk, pdp), = planned.items()
    assert pdp["description"] == record["pdp"]["attribute_summary"]
    assert pdp["canonical_url"] == f"https://{HOST}/products/glow-serum"


async def test_crawl_job_turns_pdp_fetches_off_captures_products_and_restores_the_feed(monkeypatch):
    import services.curated_brand_feed as feed

    seen = {}
    products = [_shopify_product()]

    async def fake_products(domain, **kwargs):
        return list(products)

    class Batch(list):
        crawl_report = {"status": "complete"}

    async def fake_records_for_brand(**kwargs):
        seen.update(kwargs)
        fetched = await feed.fetch_shopify_products(kwargs["domain"])
        out = Batch(feed.shopify_product_to_record(p, domain=kwargs["domain"], category_path="beauty/skincare",
                                                   currency="USD", source_role="retailer") for p in fetched)
        return out

    monkeypatch.setattr(feed, "fetch_shopify_products", fake_products)
    monkeypatch.setattr(feed, "records_for_brand", fake_records_for_brand)
    records, captured = await rd.crawl_job(_crawl_job())
    assert captured == products and len(records) == 1
    assert seen["max_pdp_inci_fetches"] == 0 and seen["enrich_missing_gtin"] is False
    assert seen["enrich_missing_inci"] is False and seen["max_pdp_identity_fetches"] == 0
    assert "market" not in seen
    assert feed.fetch_shopify_products is fake_products


async def test_crawl_job_refuses_an_unproven_crawl(monkeypatch):
    import services.curated_brand_feed as feed

    class Batch(list):
        crawl_report = {"status": "capped"}

    async def fake_records_for_brand(**kwargs):
        return Batch()

    monkeypatch.setattr(feed, "records_for_brand", fake_records_for_brand)
    with pytest.raises(rd.CrawlRefused):
        await rd.crawl_job(_crawl_job())


def test_the_script_can_be_handed_over_as_an_inline_program():
    """scripts/ops/run_oneoff_job.sh picks an --args delimiter absent from the payload; an at-sign
    in the program is the one that makes that choice fragile (the inline dry-run recipe)."""
    text = Path(rd.__file__).read_text(encoding="utf-8")
    assert "@" not in text
