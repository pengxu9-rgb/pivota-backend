"""scripts/rederive_crawl_descriptions.py: which jobs, which rows, the write guard, the drain's stage
rule, the gates, the safety rule, the manifest / binding / revert, and the write phase's batching,
lock, window and dry-run behaviour -- driven through fakes. The SQL itself (CAS predicates,
RETURNING, the manifest event's atomicity) is executed on real Postgres in
tests/test_rederive_crawl_descriptions_postgres.py.
"""

from __future__ import annotations

import contextlib
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.rederive_crawl_descriptions as rd

HOST = "a.example.com"
OTHER = "b.example.com"
RESOLVED = "beauty/skincare/treat/serum"
LONG_OLD = "Old crawled copy of this example product, long enough to clear the fifty character floor."
LONG_NEW = "New clean copy of this example product, long enough to clear the fifty character floor too."
NOON = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _row(pk, handle, desc, *, crawled=None, stage="published", host=HOST, category=RESOLVED):
    return {
        "product_key": pk, "content_key": f"ck-{pk}",
        "canonical_url": f"https://{host}/products/{handle}", "source_domain": host, "host": host,
        "title": "Example Serum", "description": desc, "image_url": "https://cdn.example.com/x.jpg",
        "category_path": category, "tags": '["serum"]', "price_tier": "under_50", "demographic": None,
        "use_case_tags": "[]", "lifestyle_tags": "[]", "pdp_scope": None,
        "source_system": "catalog_enrichment_agent_v1", "pdp_lifecycle_stage": stage,
        "crawled_copy": crawled, "content_changed_at": 1, "suppressed_at": None,
    }


def _pdp(pk, handle, desc, *, host=HOST, category=RESOLVED):
    """A planned pdp row with the stage ingestion would give it (its category rule included)."""
    from services.category_path_aliases import resolve
    from services.pdp_lifecycle import compute_lifecycle_stage

    pdp = {"product_key": pk, "title": "Example Serum", "description": desc,
           "canonical_url": f"https://{host}/products/{handle}", "category_path": category,
           "image_url": "https://cdn.example.com/x.jpg", "tags": '["serum"]', "use_case_tags": '["daily"]',
           "lifestyle_tags": "[]", "demographic": None, "price_tier": "under_50",
           "pdp_scope": "multi_merchant_canonical", "source_system": "catalog_enrichment_agent_v1",
           "content_key": f"ck-{pk}"}
    pdp["pdp_lifecycle_stage"] = compute_lifecycle_stage(pdp) if resolve(category) else "draft"
    return pdp


def _job(job_id, *, host=HOST, scope=None, source=None, status="done", day=11):
    options = {"source": source} if source else {}
    return {"id": job_id, "domain": host, "brand": "Example", "options": options,
            "scope_key": scope or f"scope-{job_id}", "status": status,
            "updated_at": datetime(2026, 10, day, tzinfo=timezone.utc), "host": host}


class _Tx:
    """READ transactions start/rollback; WRITE transactions are atomic: an exception restores the
    catalog and the event store as they were."""

    def __init__(self, db):
        self.db = db

    async def start(self):
        self.db.log.append("begin_read")

    async def rollback(self):
        self.db.log.append("rollback_read")

    async def __aenter__(self):
        self.snapshot = (copy.deepcopy(self.db.catalog), copy.deepcopy(self.db.events))
        self.db.tx_batches.append([])
        self.db.log.append("begin_write")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.db.catalog, self.db.events = self.snapshot
            self.db.log.append("rollback_write")
        else:
            self.db.log.append("commit")
        return False


class FakeDB:
    """The catalog the script reads and writes. `on_update` runs once, just before the first UPDATE:
    a test moves a row between the script's read and its write with it."""

    def __init__(self, rows, jobs):
        self.catalog = {r["product_key"]: dict(r) for r in rows}
        self.jobs = list(jobs)
        self.events, self.log, self.tx_batches, self.updates = [], [], [], []
        self.fail_next = None
        self.fail_event = False
        self.on_update = None

    def transaction(self):
        return _Tx(self)

    async def connect(self):
        self.log.append("connect")

    async def disconnect(self):
        self.log.append("disconnect")

    async def execute(self, sql, values=None):
        self.log.append(sql.strip())

    def _live(self, values):
        rows = [r for r in self.catalog.values() if r.get("suppressed_at") is None
                and (r.get("pdp_lifecycle_stage") or "") not in ("hold", "archived")]
        if "hosts" in values:
            rows = [r for r in rows if r["host"] in values["hosts"]]
        if "host" in values:
            rows = [r for r in rows if r["host"] == values["host"]]
        return rows

    async def fetch_all(self, sql, values=None):
        values = values or {}
        self.log.append(("select", sql.split("FROM")[0].strip()[:40], dict(values)))
        if "identity_resolution_events" in sql:
            return [dict(e) for e in self.events
                    if e["run_id"] == values["run_id"] and e["action"] in values["actions"]]
        if "retailer_ingest_jobs" in sql:
            return [dict(j) for j in self.jobs if j["host"] in values["hosts"]]
        live = self._live(values)
        if "COUNT(*)" in sql:
            counts = {}
            for r in live:
                counts[r["host"]] = counts.get(r["host"], 0) + 1
            return [{"host": h, "n": n} for h, n in counts.items()]
        if sql.startswith("SELECT product_key, canonical_url FROM"):
            return [{"product_key": r["product_key"], "canonical_url": r["canonical_url"]} for r in live]
        assert "candidate_attribute_summary" in sql
        return [dict(r) for r in live]

    async def fetch_one(self, sql, values=None):
        if sql is rd._EVENT_SQL:
            if self.fail_event:
                return None
            self.events.append({"action": values["action"], "run_id": values["run_id"],
                                "detail": values["detail"]})
            return {"id": len(self.events)}
        if self.on_update is not None:
            hook, self.on_update = self.on_update, None
            hook(self.catalog)
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        self.updates.append(("revert" if sql is rd._REVERT_SQL else "update", values))
        self.tx_batches[-1].append(values["pk"])
        row = self.catalog.get(values["pk"])
        if row is None or row.get("suppressed_at") is not None:
            return None
        if sql is rd._UPDATE_SQL:
            if (row["description"] != values["old"] or row.get("pdp_lifecycle_stage") != values["old_stage"]
                    or row.get("content_changed_at") != values["old_content_changed_at"]
                    or (row.get("pdp_lifecycle_stage") or "") in ("hold", "archived")):
                return None
            row.update(description=values["new"], pdp_lifecycle_stage=values["stage"],
                       price_tier=values["price_tier"], use_case_tags=values["use_case_tags"],
                       lifestyle_tags=values["lifestyle_tags"], demographic=values["demographic"])
            row["content_changed_at"] += 1
            return {"product_key": values["pk"], "content_changed_at": row["content_changed_at"]}
        assert sql is rd._REVERT_SQL
        if row["description"] != values["new"] or row.get("pdp_lifecycle_stage") != values["new_stage"]:
            return None
        row.update(description=values["old"], pdp_lifecycle_stage=values["old_stage"],
                   price_tier=values["price_tier"], use_case_tags=values["use_case_tags"],
                   lifestyle_tags=values["lifestyle_tags"], demographic=values["demographic"])
        return {"product_key": values["pk"]}


def _deps(db, crawls, *, now=NOON, clock=None, lock_error=None, crawl_hook=None):
    """crawls: job id -> (pdps, products). The fake plan hands the pdps straight back."""
    state = SimpleNamespace(crawled=[], sleeps=[], reconnects=0, manifest=[])

    @contextlib.asynccontextmanager
    async def lock():
        if lock_error is not None:
            raise lock_error
        db.log.append("lock_enter")
        yield 0.0
        db.log.append("lock_exit")

    async def crawl(job):
        state.crawled.append(job["id"])
        if crawl_hook is not None:
            crawl_hook(db.catalog)
        value = crawls[job["id"]]
        if isinstance(value, Exception):
            raise value
        return value

    async def refresh(ck, *, refresh_source, db):
        assert refresh_source == rd.REFRESH_SOURCE
        db.log.append(f"refresh:{ck}")

    async def recompute(ck, *, reason, db):
        assert reason == rd.REFRESH_SOURCE
        db.log.append(f"recompute:{ck}")

    async def reconnect():
        state.reconnects += 1

    async def sleep(seconds):
        state.sleeps.append(seconds)

    now_fn = now if callable(now) else (lambda: now)
    ticks = iter(clock) if clock is not None else None
    deps = SimpleNamespace(
        db=db, crawl=crawl, plan=lambda job, records, rows: {p["product_key"]: p for p in records},
        lock=lock, refresh=refresh, recompute=recompute, reconnect=reconnect, sleep=sleep, now=now_fn,
        clock=(lambda: next(ticks)) if ticks else (lambda: 0.0), started=0.0, stopped=None,
        writes_left=None, manifest=state.manifest.append,
    )
    return deps, state


async def _run(argv, db, crawls, **kw):
    deps, state = _deps(db, crawls, **kw)
    code, summary = await rd.run(rd.parse_args(argv + ["--manifest-out", "none"]), deps)
    return code, summary, state


async def _dry_then_apply(argv, db, crawls, **kw):
    """The reviewed flow: a dry run, then --apply bound to its expect_plan."""
    _, dry, _ = await _run(argv, db, crawls)
    assert db.updates == []
    return await _run(argv + ["--apply", "--expect-plan", dry["expect_plan"]], db, crawls, **kw)


def _writes(db):
    return [v["pk"] for kind, v in db.updates if kind == "update"]


# --- the frozen old producer --------------------------------------------------------------------------

def test_the_legacy_producer_is_the_old_flattening():
    body = "<style>.x{}</style><p>Soft&nbsp;skin &amp; glow</p><!-- c --><ul><li>One</li><li>Two</li></ul>"
    assert rd.legacy_body_html_to_text(body) == "Soft skin & glow One Two"
    assert rd.legacy_body_html_to_text(None) == ""
    out = rd.legacy_body_html_to_text(("word " * 600).strip())
    assert len(out) <= 2000 and out.endswith("word")


# --- which jobs -----------------------------------------------------------------------------------------

def test_only_the_latest_done_storefront_job_per_cohort_newest_first():
    jobs = [
        _job("old-same-scope", scope="s1", day=10), _job("new-same-scope", scope="s1", day=12),
        _job("other-cohort", scope="s2", day=11), _job("affiliate", source="affiliate_feed", day=13),
        _job("markets", source="shopify_markets", day=13), _job("not-done", status="failed", day=13),
        _job("too-old", scope="s3", day=1),
    ]
    picked, excluded = rd.select_jobs(jobs, jobs_since=datetime(2026, 10, 9, tzinfo=timezone.utc))
    assert [j["id"] for j in picked] == ["new-same-scope", "other-cohort"]
    assert excluded == ["affiliate_feed", "shopify_markets"]
    unbounded, _ = rd.select_jobs(jobs, jobs_since=None)
    assert [j["id"] for j in unbounded] == ["new-same-scope", "other-cohort", "too-old"]


def test_job_options_stored_as_json_text_are_read():
    picked, excluded = rd.select_jobs([{**_job("j"), "options": '{"source": "affiliate_feed"}'}], jobs_since=None)
    assert picked == [] and excluded == ["affiliate_feed"]


# --- the guard --------------------------------------------------------------------------------------------

def test_the_crawled_copy_arm_owns_the_row():
    assert rd.guard_outcome(_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _pdp("p1", "h1", LONG_NEW), None) is None


def test_the_legacy_body_arm_owns_the_row():
    row = _row("p1", "h1", LONG_OLD, crawled="a clean copy already re-crawled")
    assert rd.guard_outcome(row, _pdp("p1", "h1", LONG_NEW), LONG_OLD) is None


def test_copy_matching_neither_arm_is_not_crawl_owned():
    row = _row("p1", "h1", LONG_OLD, crawled=LONG_OLD + " ")
    assert rd.guard_outcome(row, _pdp("p1", "h1", LONG_NEW), LONG_OLD.upper()) == rd.NOT_CRAWL_OWNED
    assert rd.guard_outcome(_row("p1", "h1", LONG_OLD), _pdp("p1", "h1", LONG_NEW), None) == rd.NOT_CRAWL_OWNED


def test_equal_text_is_unchanged_and_a_written_row_reads_back_unchanged():
    assert rd.guard_outcome(_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _pdp("p1", "h1", LONG_OLD), None) == rd.UNCHANGED
    written = _row("p1", "h1", LONG_NEW, crawled=LONG_OLD)
    assert rd.guard_outcome(written, _pdp("p1", "h1", LONG_NEW), LONG_OLD) == rd.UNCHANGED


def test_not_in_feed_and_an_empty_new_text_is_never_written():
    assert rd.guard_outcome(_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), None, None) == rd.NOT_IN_FEED
    assert rd.guard_outcome(_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _pdp("p1", "h1", None), None) == rd.WOULD_EMPTY


# --- the stage: the drain's own rule ----------------------------------------------------------------------

async def _classify(row, pdp, legacy=None, **kw):
    return await rd.classify(row, pdp, legacy, allow_demote=kw.get("allow_demote", False),
                             allow_promote=kw.get("allow_promote", False))


async def test_an_unresolved_category_keeps_the_plans_draft_however_good_the_copy():
    """The review's repro: a draft row whose category does not resolve loses one sentence. Judged by
    compute_lifecycle_stage alone it would be written PUBLISHED, served, and flipped back to draft by
    the next re-crawl. The drain's rule keeps it draft."""
    old = LONG_OLD + " Free shipping on every order over fifty dollars this week only."
    row = _row("p1", "h1", old, crawled=old, stage="draft", category="other")
    d = await _classify(row, _pdp("p1", "h1", LONG_OLD, category="other"))
    assert d.stage == "draft" and d.outcome == rd.WRITE and d.rank_move() == 0


async def test_the_stage_is_the_drains_kept_copy_stage():
    """Parity: drain_stage IS apply._stage_from_kept_copy over the planned row."""
    from services.catalog_enrichment_agent.apply import _stage_from_kept_copy

    pdp = _pdp("p1", "h1", LONG_NEW)

    class Kept:
        async def fetch_all(self, sql, values=None):
            return [{"product_key": "p1", "title": "Example Serum", "description": "Short."}]

    plan, _ = await _stage_from_kept_copy({"pdps": [pdp]}, Kept())
    assert await rd.drain_stage(pdp, "Example Serum", "Short.") == plan["pdps"][0]["pdp_lifecycle_stage"] == "draft"
    assert await rd.drain_stage(pdp, "Example Serum", LONG_NEW) == "published"
    other = _pdp("p1", "h1", LONG_NEW, category="other")
    assert await rd.drain_stage(other, "Example Serum", LONG_NEW) == "draft"


async def test_a_demoting_row_is_skipped_without_allow_demote_and_written_with_it():
    row = _row("p1", "h1", LONG_OLD, crawled=LONG_OLD, stage="published")
    held = await _classify(row, _pdp("p1", "h1", "Too short to stay published."))
    assert held.stage == "draft" and held.rank_move() < 0 and held.outcome == rd.SKIP_DEMOTE
    allowed = await _classify(row, _pdp("p1", "h1", "Too short to stay published."), allow_demote=True)
    assert allowed.outcome == rd.WRITE


async def test_a_promoting_row_is_skipped_without_allow_promote_and_written_with_it():
    row = _row("p1", "h1", LONG_OLD, crawled=LONG_OLD, stage="draft")
    held = await _classify(row, _pdp("p1", "h1", LONG_NEW))
    assert held.stage == "published" and held.rank_move() > 0 and held.outcome == rd.SKIP_PROMOTE
    assert held.stage_with_old == "published", "the move is not the new copy's doing"
    allowed = await _classify(row, _pdp("p1", "h1", LONG_NEW), allow_promote=True)
    assert allowed.outcome == rd.WRITE


async def test_the_same_stage_is_neither_a_demotion_nor_a_promotion():
    d = await _classify(_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _pdp("p1", "h1", LONG_NEW))
    assert d.stage == "published" and d.rank_move() == 0 and d.outcome == rd.WRITE


# --- the safety rule: per sentence, counted, with a wider cue list ---------------------------------------

def test_a_store_wide_skin_test_line_the_producer_drops_is_caught():
    """The real repetition pass drops a store-wide skin-test line (its own cue list does not see it)."""
    import services.curated_brand_feed as feed

    skin = "Perform a skin test on a small area 24 hours before first use."
    kinds = ["serum", "toner", "cream", "mask", "balm", "oil", "mist", "gel", "lotion", "essence", "foam", "scrub"]
    rows = [[f"This product number {i} is a unique {k} formula with its own long description of texture.",
             f"Second unique sentence {i} about the {k} benefits and daily use for everyone.", skin]
            for i, k in enumerate(kinds)]
    out, report = feed.drop_repeated_store_sentences(rows, kinds=[str(i) for i in range(len(rows))])
    assert report["rows_changed"] == len(rows) and skin not in out[0]
    old, new = " ".join(rows[0]), " ".join(out[0])
    assert rd.lost_safety_sentences(old, new) == [skin]


@pytest.mark.parametrize("sentence", [
    "Stop using if discomfort occurs.",
    "Please consult your healthcare provider before use.",
    "May increase the risk of sunburn.",
    "Not suitable for skin sensitivities.",
])
def test_cues_the_producer_list_misses_are_safety_sentences(sentence):
    import services.curated_brand_feed as feed

    assert not feed.is_safety_text(sentence)
    assert rd.lost_safety_sentences(f"{LONG_OLD} {sentence}", LONG_NEW) == [sentence]


def test_losing_one_of_two_avoid_sentences_is_a_loss():
    old = f"{LONG_OLD} Avoid contact with eyes. Avoid direct sunlight."
    assert rd.lost_safety_sentences(old, f"{LONG_NEW} Avoid contact with eyes.") == ["Avoid direct sunlight."]


def test_a_repeated_safety_sentence_is_counted():
    old = f"{LONG_OLD} Avoid contact with eyes. Avoid contact with eyes."
    assert rd.lost_safety_sentences(old, f"{LONG_NEW} Avoid contact with eyes.") == ["Avoid contact with eyes."]
    assert rd.lost_safety_sentences(old, f"{LONG_NEW} Avoid contact with eyes. Avoid contact with eyes.") == []


@pytest.mark.parametrize("old_word,new_word", [
    ("irritation2", "irritation"), ("irritation", "irritation2"), ("irritation¹", "irritation"), ("eyes3", "eyes"),
])
def test_a_footnote_digit_glued_to_a_cue_is_still_that_sentence(old_word, new_word):
    old, new = f"{LONG_OLD} May cause {old_word}.", f"{LONG_NEW} May cause {new_word}."
    assert rd.lost_safety_sentences(old, new) == []


def test_a_kept_safety_sentence_is_not_lost_and_a_row_losing_one_is_skipped():
    old = f"{LONG_OLD} Discontinue use if irritation occurs."
    assert rd.lost_safety_sentences(old, f"{LONG_NEW}  discontinue use if irritation occurs!") == []
    d = rd.decide(_row("p1", "h1", old, crawled=old), _pdp("p1", "h1", LONG_NEW), stage="published",
                  stage_with_old="published", allow_demote=True, allow_promote=True)
    assert d.outcome == rd.SKIP_SAFETY and d.lost_safety == ["Discontinue use if irritation occurs."]


def test_removed_sentences_name_what_the_new_text_drops():
    old = "First sentence about the product. Shop our sale today."
    assert rd.removed_sentences(old, "First sentence about the product.") == ["Shop our sale today."]


# --- the run ------------------------------------------------------------------------------------------------

def _population():
    rows = [
        _row("p1", "h1", LONG_OLD, crawled=LONG_OLD),                         # write (crawled arm)
        _row("p2", "h2", LONG_OLD + " two", crawled="clean"),                 # write (legacy arm)
        _row("p3", "h3", LONG_OLD + " three", crawled=LONG_OLD + " three"),    # unchanged
        _row("p4", "h4", "Hand-edited copy that no crawl wrote, long enough."),  # not crawl owned
        _row("p5", "h5", LONG_OLD + " five", crawled=LONG_OLD + " five"),      # not in feed
        _row("p6", "h6", LONG_OLD + " six", crawled=LONG_OLD + " six"),        # would demote
        _row("p7", "h7", LONG_OLD + " Avoid eyes.", crawled=LONG_OLD + " Avoid eyes."),  # safety lost
    ]
    pdps = [_pdp("p1", "h1", LONG_NEW), _pdp("p2", "h2", LONG_NEW + " two"),
            _pdp("p3", "h3", LONG_OLD + " three"), _pdp("p4", "h4", LONG_NEW + " four"),
            _pdp("p6", "h6", "Short now."), _pdp("p7", "h7", LONG_NEW + " seven")]
    products = [{"handle": "h2", "body_html": f"<p>{LONG_OLD} two</p>"}]
    return rows, pdps, products


async def test_the_dry_run_classifies_everything_writes_nothing_and_prints_the_manifest():
    rows, pdps, products = _population()
    db = FakeDB(rows, [_job("j1")])
    code, s, state = await _run([], db, {"j1": (pdps, products)})
    assert code == 0
    assert {k: s[k] for k in rd.SUMMARY_KEYS} == {
        "rows": 7, "would_rewrite": 4, "written": 0, "raced": 0, "unchanged": 1, "not_crawl_owned": 1,
        "not_in_feed": 1, "would_demote": 1, "skipped_demote": 1, "would_promote": 0, "skipped_promote": 0,
        "safety_rows": 1, "safety_cue_lost": 1}
    assert s["stage_transitions"] == {"published->draft": 1}
    assert db.updates == [] and db.events == [] and db.tx_batches == []
    assert "lock_enter" not in db.log and not any(str(x).startswith(("refresh", "recompute")) for x in db.log)
    assert all(x in ("begin_read", "rollback_read", "connect") or isinstance(x, tuple) or x.startswith("SET ")
               for x in db.log)
    manifest = {e["pk"]: e for e in state.manifest}
    assert sorted(manifest) == ["p1", "p2", "p6", "p7"]
    assert manifest["p1"]["decision"] == rd.WRITE and manifest["p6"]["decision"] == rd.SKIP_DEMOTE
    entry = manifest["p7"]
    assert entry["decision"] == rd.SKIP_SAFETY and entry["lost_safety_sentences"] == ["Avoid eyes."]
    assert entry["removed_sentences"] and entry["old"].endswith("Avoid eyes.") and entry["host"] == HOST
    assert set(entry) >= {"pk", "content_key", "host", "old", "new", "old_stage", "new_stage", "removed_sentences",
                          "taxonomy_old", "taxonomy_new", "decision"}
    assert s["expect_plan"].startswith(f"{HOST}=") and len(s["expect_plan"].split("=")[1]) == 16


async def test_every_read_sets_its_own_statement_timeout():
    rows, pdps, products = _population()
    db = FakeDB(rows, [_job("j1")])
    await _run([], db, {"j1": (pdps, products)})
    reads = db.log.count("begin_read")
    assert reads >= 4 and db.log.count(f"SET LOCAL statement_timeout = '{rd.READ_STATEMENT_TIMEOUT}'") == reads


def test_apply_needs_the_reviewed_plan():
    with pytest.raises(SystemExit):
        rd.parse_args(["--apply"])
    with pytest.raises(SystemExit):
        rd.parse_args(["--apply", "--expect-plan", f"{HOST}=nothex"])
    opts = rd.parse_args(["--apply", "--expect-plan", f"www.{HOST}=0123456789abcdef", "--hosts", f"{HOST},{OTHER}"])
    assert opts.expect_plan == {HOST: "0123456789abcdef"} and opts.hosts == {HOST}


async def test_apply_writes_exactly_the_reviewed_rows_and_stores_their_manifest():
    rows, pdps, products = _population()
    db = FakeDB(rows, [_job("j1")])
    code, s, state = await _dry_then_apply([], db, {"j1": (pdps, products)})
    assert code == 0 and s["written"] == 2 and s["raced"] == 0 and s["plan_changed"] == 0
    assert sorted(_writes(db)) == ["p1", "p2"]
    assert db.catalog["p1"]["description"] == LONG_NEW and db.catalog["p6"]["description"] == rows[5]["description"]
    assert json.loads(db.catalog["p1"]["use_case_tags"]) == ["daily"], "the plan's taxonomy, as a re-crawl writes it"
    (event,) = db.events
    assert event["action"] == rd.APPLIED_ACTION and event["run_id"] == s["run_id"]
    stored = json.loads(event["detail"])["rows"]
    assert sorted(e["pk"] for e in stored) == ["p1", "p2"] and all(e["decision"] == "written" for e in stored)
    assert stored[0]["written_content_changed_at"] == 2
    written = [e for e in state.manifest if e["decision"] == "written"]
    assert sorted(e["pk"] for e in written) == ["p1", "p2"]


async def test_a_plan_that_changed_since_the_review_writes_nothing():
    rows, pdps, products = _population()
    db = FakeDB(rows, [_job("j1")])
    _, dry, _ = await _run([], db, {"j1": (pdps, products)})
    changed = [_pdp("p1", "h1", LONG_NEW + " edited on the storefront since")] + pdps[1:]
    code, s, state = await _run(["--apply", "--expect-plan", dry["expect_plan"]], db, {"j1": (changed, products)})
    assert code == 0 and s["written"] == 0 and s["plan_changed"] == 2 and db.updates == []
    assert {e["decision"] for e in state.manifest if e["pk"] in ("p1", "p2")} == {"plan_changed"}


async def test_a_host_the_reviewed_plan_does_not_name_is_not_touched():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _row("q1", "h1", LONG_OLD, crawled=LONG_OLD, host=OTHER)]
    db = FakeDB(rows, [_job("j1"), _job("j2", host=OTHER)])
    crawls = {"j1": ([_pdp("p1", "h1", LONG_NEW)], []), "j2": ([_pdp("q1", "h1", LONG_NEW, host=OTHER)], [])}
    _, dry, _ = await _run(["--hosts", HOST], db, crawls)
    code, s, state = await _run(["--apply", "--expect-plan", dry["expect_plan"]], db, crawls)
    assert s["written"] == 1 and state.crawled == ["j1"] and _writes(db) == ["p1"]


@pytest.mark.parametrize("field,value", [
    ("description", "Edited after the read, by someone else entirely."),
    ("pdp_lifecycle_stage", "validated"),
    ("content_changed_at", 99),
])
async def test_a_row_that_moved_between_the_read_and_the_write_is_raced(field, value):
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    crawls = {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])}
    _, dry, _ = await _run([], db, crawls)
    db.on_update = lambda catalog: catalog["p1"].update({field: value})
    code, s, _ = await _run(["--apply", "--expect-plan", dry["expect_plan"]], db, crawls)
    assert code == 0 and s["raced"] == 1 and s["written"] == 0 and db.events == []
    assert db.catalog["p1"][field] == value and not any(str(x).startswith("refresh") for x in db.log)


async def test_rows_are_judged_as_they_are_after_the_crawl_not_at_the_start():
    """A hand edit that lands while the host is being crawled is seen: not crawl owned, not raced."""
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    hook = lambda catalog: catalog["p1"].update(description="A hand edit made while the crawl ran, long enough.")  # noqa: E731
    _, s, _ = await _run([], db, {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])}, crawl_hook=hook)
    assert s["not_crawl_owned"] == 1 and s["would_rewrite"] == 0


async def test_batches_commit_under_the_lock_and_the_view_work_runs_outside_it():
    rows = [_row(f"p{i}", f"h{i}", LONG_OLD, crawled=LONG_OLD) for i in range(5)]
    pdps = [_pdp(f"p{i}", f"h{i}", LONG_NEW) for i in range(5)]
    db = FakeDB(rows, [_job("j1")])
    code, s, state = await _dry_then_apply(["--batch-size", "2", "--batch-sleep-s", "7"], db, {"j1": (pdps, [])})
    assert code == 0 and s["written"] == 5
    assert [len(b) for b in db.tx_batches] == [2, 2, 1] and db.log.count("commit") == 3
    assert db.log.count("lock_enter") == 3 and state.sleeps == [7.0, 7.0]
    first_exit = db.log.index("lock_exit")
    first_refresh = next(i for i, x in enumerate(db.log) if str(x).startswith("refresh"))
    assert first_refresh > first_exit and db.log[first_exit - 1] == "commit"
    write_settings = [x for x in db.log if x in (f"SET LOCAL statement_timeout = '{rd.WRITE_STATEMENT_TIMEOUT}'",
                                                 f"SET LOCAL lock_timeout = '{rd.WRITE_LOCK_TIMEOUT}'")]
    assert len(write_settings) == 6
    assert len(db.events) == 3


def test_the_defaults_are_two_hundred_rows_and_a_pause_longer_than_the_drains_poll():
    opts = rd.parse_args([])
    assert opts.batch_size == 200 and opts.batch_sleep_s > rd.LOCK_POLL_S


async def test_eligibility_is_recomputed_only_where_the_stage_moved():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _row("p2", "h2", LONG_OLD, crawled=LONG_OLD)]
    pdps = [_pdp("p1", "h1", LONG_NEW), _pdp("p2", "h2", "Short now.")]
    db = FakeDB(rows, [_job("j1")])
    code, s, _ = await _dry_then_apply(["--allow-demote"], db, {"j1": (pdps, [])})
    assert code == 0 and s["written"] == 2 and s["eligibility_recomputed"] == 1
    assert [x for x in db.log if str(x).startswith("recompute")] == ["recompute:ck-p2"]
    assert sorted(x for x in db.log if str(x).startswith("refresh")) == ["refresh:ck-p1", "refresh:ck-p2"]


async def test_max_writes_stops_a_canary_and_the_run():
    rows = [_row(f"p{i}", f"h{i}", LONG_OLD, crawled=LONG_OLD) for i in range(5)]
    rows.append(_row("q1", "h1", LONG_OLD, crawled=LONG_OLD, host=OTHER))
    pdps = [_pdp(f"p{i}", f"h{i}", LONG_NEW) for i in range(5)]
    db = FakeDB(rows, [_job("j1"), _job("j2", host=OTHER)])
    crawls = {"j1": (pdps, []), "j2": ([_pdp("q1", "h1", LONG_NEW, host=OTHER)], [])}
    code, s, state = await _dry_then_apply(["--max-writes", "2"], db, crawls)
    assert code == 0 and s["written"] == 2 and s["unwritten"] == 3 and s["stopped"] == "max writes reached"
    assert "j2" not in state.crawled[2:]
    assert sum(1 for e in state.manifest if e["decision"] == "unwritten") == 3


async def test_the_window_is_checked_before_every_write_batch():
    rows = [_row(f"p{i}", f"h{i}", LONG_OLD, crawled=LONG_OLD) for i in range(4)]
    pdps = [_pdp(f"p{i}", f"h{i}", LONG_NEW) for i in range(4)]
    db = FakeDB(rows, [_job("j1")])
    _, dry, _ = await _run([], db, {"j1": (pdps, [])})
    times = iter([NOON, NOON, NOON, datetime(2026, 10, 13, 5, 20, tzinfo=timezone.utc)] + [NOON] * 10)
    code, s, _ = await _run(["--apply", "--expect-plan", dry["expect_plan"], "--batch-size", "2"], db,
                            {"j1": (pdps, [])}, now=lambda: next(times))
    assert code == 3 and s["written"] == 2 and s["unwritten"] == 2 and "window" in s["stopped"]


async def test_a_busy_lock_stops_the_run_and_writes_nothing():
    class CatalogWriteLockBusy(Exception):
        pass

    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    code, s, _ = await _dry_then_apply([], db, {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])},
                                       lock_error=CatalogWriteLockBusy(600.0))
    assert code == 3 and s["stopped"] and s["unwritten"] == 1 and s["written"] == 0 and db.updates == []


async def test_a_transport_failure_is_healed_and_the_batch_retried():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    crawls = {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])}
    _, dry, _ = await _run([], db, crawls)
    db.fail_next = ConnectionResetError("reset")
    code, s, state = await _run(["--apply", "--expect-plan", dry["expect_plan"]], db, crawls)
    assert code == 0 and s["written"] == 1 and state.reconnects == 2


async def test_a_batch_whose_manifest_is_not_stored_writes_nothing():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    crawls = {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])}
    _, dry, _ = await _run([], db, crawls)
    db.fail_event = True
    code, s, _ = await _run(["--apply", "--expect-plan", dry["expect_plan"]], db, crawls)
    assert code == 3 and s["update_failed"] == 1 and s["written"] == 0
    assert db.catalog["p1"]["description"] == LONG_OLD and "rollback_write" in db.log


@pytest.mark.parametrize("hour,minute,refused", [(5, 14, False), (5, 15, True), (6, 14, True), (6, 15, False)])
async def test_apply_refuses_to_start_inside_the_sweep_window(hour, minute, refused):
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("j1")])
    crawls = {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])}
    _, dry, _ = await _run([], db, crawls)
    db.log.clear()
    now = datetime(2026, 10, 13, hour, minute, tzinfo=timezone.utc)
    code, s, _ = await _run(["--apply", "--expect-plan", dry["expect_plan"]], db, crawls, now=now)
    if refused:
        assert code == 2 and s == {} and db.log == []
    else:
        assert code == 0 and s["written"] == 1


async def test_a_dry_run_inside_the_window_still_runs():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)]
    now = datetime(2026, 10, 13, 5, 30, tzinfo=timezone.utc)
    code, s, _ = await _run([], FakeDB(rows, [_job("j1")]), {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])}, now=now)
    assert code == 0 and s["would_rewrite"] == 1


# --- revert -------------------------------------------------------------------------------------------------

async def _applied():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _row("p2", "h2", LONG_OLD, crawled=LONG_OLD)]
    pdps = [_pdp("p1", "h1", LONG_NEW), _pdp("p2", "h2", "Short now.")]
    db = FakeDB(rows, [_job("j1")])
    _, s, state = await _dry_then_apply(["--allow-demote"], db, {"j1": (pdps, [])})
    assert s["written"] == 2
    return db, s["run_id"], state, rows


async def test_revert_dry_run_writes_nothing():
    db, run_id, _, _ = await _applied()
    before = copy.deepcopy(db.catalog)
    code, s, _ = await _run(["--revert", run_id], db, {})
    assert code == 0 and s["revert_rows"] == 2 and db.catalog == before and len(db.events) == 1


async def test_revert_restores_text_stage_and_taxonomy_and_records_itself():
    db, run_id, _, rows = await _applied()
    db.log.clear()
    code, s, _ = await _run(["--revert", run_id, "--apply"], db, {})
    assert code == 0 and s["reverted"] == 2
    for original in rows:
        row = db.catalog[original["product_key"]]
        assert row["description"] == original["description"]
        assert row["pdp_lifecycle_stage"] == original["pdp_lifecycle_stage"]
        assert json.loads(row["use_case_tags"]) == [] and row["price_tier"] == "under_50"
    assert db.events[-1]["action"] == rd.REVERTED_ACTION and db.events[-1]["run_id"] == run_id
    assert "lock_enter" in db.log and [x for x in db.log if str(x).startswith("recompute")] == ["recompute:ck-p2"]
    again, s2, _ = await _run(["--revert", run_id, "--apply"], db, {})
    assert s2["revert_rows"] == 0, "a reverted row is not reverted twice"


async def test_revert_leaves_a_row_that_moved_after_the_write():
    db, run_id, _, _ = await _applied()
    db.catalog["p1"]["description"] = "Edited after the rederive run, by someone else, long enough."
    _, s, _ = await _run(["--revert", run_id, "--apply"], db, {})
    assert s["reverted"] == 1 and s["revert_raced"] == 1
    assert db.catalog["p1"]["description"].startswith("Edited after")


async def test_revert_from_a_manifest_file_of_row_lines(tmp_path):
    db, run_id, state, rows = await _applied()
    path = tmp_path / "manifest.log"
    path.write_text("\n".join(["START {}"] + [f"ROW {json.dumps(e, default=str)}" for e in state.manifest]))
    _, s, _ = await _run(["--revert", str(path), "--apply"], db, {})
    assert s["reverted"] == 2 and db.catalog["p1"]["description"] == LONG_OLD


# --- hosts, jobs and budget ---------------------------------------------------------------------------------

async def test_hosts_is_pushed_into_the_sql():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _row("q1", "h1", LONG_OLD, crawled=LONG_OLD, host=OTHER)]
    db = FakeDB(rows, [_job("j1"), _job("j2", host=OTHER)])
    _, s, state = await _run(["--hosts", f"www.{OTHER}"], db, {"j2": ([_pdp("q1", "h1", LONG_NEW, host=OTHER)], [])})
    assert s["rows"] == 1 and state.crawled == ["j2"]
    sql, values = rd.host_counts_sql(rd.parse_args(["--hosts", OTHER]))
    assert "= ANY(:hosts)" in sql and values["hosts"] == [OTHER] and "product_payload" not in sql
    sql, values = rd.host_rows_sql(rd.parse_args(["--hosts", OTHER]), OTHER)
    assert "= ANY(:hosts)" in sql and ":host\n" in sql and values["host"] == OTHER


async def test_a_host_without_a_storefront_job_is_skipped_and_reported():
    db = FakeDB([_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)], [_job("aff", source="affiliate_feed")])
    _, s, state = await _run([], db, {})
    assert s["rows"] == 1 and s["host_skipped"] == 1 and state.crawled == []


async def test_a_failed_crawl_skips_its_host():
    db = FakeDB([_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)], [_job("j1")])
    _, s, _ = await _run([], db, {"j1": rd.CrawlRefused("crawl completeness was not proven")})
    assert s["host_skipped"] == 1 and s["jobs_failed"] == 1


async def test_rows_come_from_the_newest_job_and_crawling_stops_once_all_are_matched():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _row("p2", "h2", LONG_OLD, crawled=LONG_OLD)]
    db = FakeDB(rows, [_job("new", scope="s1", day=12), _job("old", scope="s2", day=10), _job("older", scope="s3", day=9)])
    crawls = {"new": ([_pdp("p1", "h1", LONG_NEW)], []),
              "old": ([_pdp("p1", "h1", "stale text " * 10), _pdp("p2", "h2", LONG_NEW)], []), "older": ([], [])}
    _, s, state = await _dry_then_apply([], db, crawls)
    assert state.crawled == ["new", "old"] and s["written"] == 2 and db.catalog["p1"]["description"] == LONG_NEW


async def test_a_key_planned_on_another_listing_is_not_this_rows_copy():
    db = FakeDB([_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)], [_job("j1")])
    _, s, _ = await _run([], db, {"j1": ([_pdp("p1", "other-page", LONG_NEW)], [])})
    assert s["not_in_feed"] == 1 and s["would_rewrite"] == 0


async def test_the_time_budget_stops_new_crawls():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _row("q1", "h1", LONG_OLD, crawled=LONG_OLD, host=OTHER)]
    db = FakeDB(rows, [_job("j1"), _job("j2", host=OTHER)])
    crawls = {"j1": ([_pdp("p1", "h1", LONG_NEW)], []), "j2": ([_pdp("q1", "h1", LONG_NEW, host=OTHER)], [])}
    _, s, state = await _run(["--budget-s", "10"], db, crawls, clock=[0.0, 1.0, 50.0, 60.0, 61.0])
    assert len(state.crawled) == 1 and s["host_skipped"] == 1


def test_since_all_drops_both_bounds():
    opts = rd.parse_args(["--since", "all"])
    assert opts.since is None and opts.jobs_since is None
    opts = rd.parse_args([])
    assert opts.since == datetime(2026, 10, 10) and opts.jobs_since == datetime(2026, 10, 9, tzinfo=timezone.utc)
    assert opts.manifest_out.endswith(f"{opts.run_id}.jsonl")


def test_the_manifest_sink_writes_row_lines_and_jsonl(tmp_path, capsys):
    path = tmp_path / "m.jsonl"
    sink = rd.ManifestSink(str(path))
    sink({"pk": "p1", "decision": "write"})
    assert capsys.readouterr().out.startswith("ROW {")
    assert json.loads(path.read_text().strip()) == {"pk": "p1", "decision": "write"}
    assert sink.rows == 1 and sink.bytes == len(path.read_text().encode())


# --- the crawl and the plan, for real -------------------------------------------------------------------

def _crawl_job():
    return {**_job("j1"), "options": {"vendors": ["Example"]}}


def _shopify_product(handle="glow-serum", body="<p>A light serum for daily use that leaves skin soft all day.</p>",
                     product_type="Serum"):
    return {"id": 101, "handle": handle, "title": "Glow Serum", "vendor": "Example", "product_type": product_type,
            "body_html": body, "tags": ["serum"], "images": [{"src": "https://cdn.example.com/a.jpg"}],
            "variants": [{"id": 1, "title": "Default Title", "price": "25.00", "available": True, "sku": "GS1"}],
            "options": [{"name": "Title"}]}


def _real_planned(product_type="Serum"):
    import services.curated_brand_feed as feed

    record = feed.shopify_product_to_record(_shopify_product(product_type=product_type), domain=HOST,
                                            category_path="beauty", brand_override="Example", currency="USD",
                                            source_role="retailer", retailer_name="Example Shop")
    (pk, pdp), = rd.plan_rows({"id": "j", "domain": HOST, "brand": "Example", "options": {}}, [record], []).items()
    return record, pdp


def test_the_plan_yields_each_rows_key_with_the_producers_text():
    record, pdp = _real_planned()
    assert pdp["description"] == record["pdp"]["attribute_summary"]
    assert pdp["canonical_url"] == f"https://{HOST}/products/glow-serum"


async def test_the_drain_rule_agrees_with_ingestion_on_a_real_plan():
    """On a real planned row, the stage with the planned copy kept is ingestion's own stage."""
    from services.category_path_aliases import resolve

    for product_type in ("Serum", "Lashes"):
        _, pdp = _real_planned(product_type)
        stage = await rd.drain_stage(pdp, pdp["title"], pdp["description"])
        assert stage == pdp["pdp_lifecycle_stage"]
        if not resolve(pdp["category_path"]):
            assert stage == "draft" and await rd.drain_stage(pdp, pdp["title"], LONG_NEW * 2) == "draft"


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
        return Batch(feed.shopify_product_to_record(p, domain=kwargs["domain"], category_path="beauty/skincare",
                                                    currency="USD", source_role="retailer") for p in fetched)

    monkeypatch.setattr(feed, "fetch_shopify_products", fake_products)
    monkeypatch.setattr(feed, "records_for_brand", fake_records_for_brand)
    records, captured = await rd.crawl_job(_crawl_job())
    assert captured == products and len(records) == 1
    assert seen["max_pdp_inci_fetches"] == 0 and seen["enrich_missing_gtin"] is False
    assert seen["enrich_missing_inci"] is False and seen["max_pdp_identity_fetches"] == 0 and "market" not in seen
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
    assert "@" not in Path(rd.__file__).read_text(encoding="utf-8")


async def test_an_apply_with_a_gate_flag_its_dry_run_lacked_writes_nothing():
    """The digest covers the rows a run would write, so the gate flags are part of what was reviewed."""
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD), _row("p2", "h2", LONG_OLD, crawled=LONG_OLD)]
    pdps = [_pdp("p1", "h1", LONG_NEW), _pdp("p2", "h2", "Short now.")]
    db = FakeDB(rows, [_job("j1")])
    _, dry, _ = await _run([], db, {"j1": (pdps, [])})
    _, s, _ = await _run(["--apply", "--allow-demote", "--expect-plan", dry["expect_plan"]], db, {"j1": (pdps, [])})
    assert s["written"] == 0 and s["plan_changed"] == 2 and db.updates == []


async def test_promotions_are_counted_per_transition_and_gated():
    rows = [_row("p1", "h1", LONG_OLD, crawled=LONG_OLD, stage="draft"),
            _row("p2", "h2", LONG_OLD, crawled=LONG_OLD, stage="candidate")]
    pdps = [_pdp("p1", "h1", LONG_NEW), _pdp("p2", "h2", LONG_NEW)]
    db = FakeDB(rows, [_job("j1")])
    _, s, _ = await _run([], db, {"j1": (pdps, [])})
    assert s["would_promote"] == 2 and s["skipped_promote"] == 2 and s["expect_plan"] == ""
    assert s["stage_transitions"] == {"candidate->published": 1, "draft->published": 1}
    assert s["transition_due_to_description"] == 0
    code, s, _ = await _dry_then_apply(["--allow-promote"], db, {"j1": (pdps, [])})
    assert code == 0 and s["written"] == 2 and s["eligibility_recomputed"] == 2


async def test_an_older_job_that_plans_the_rows_own_listing_wins_over_a_newer_one_that_does_not():
    db = FakeDB([_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)], [_job("new", scope="s1", day=12),
                                                                 _job("old", scope="s2", day=10)])
    crawls = {"new": ([_pdp("p1", "another-page", "Another page's copy " * 5)], []),
              "old": ([_pdp("p1", "h1", LONG_NEW)], [])}
    _, s, state = await _run([], db, crawls)
    assert state.crawled == ["new", "old"] and s["would_rewrite"] == 1 and s["not_in_feed"] == 0


async def test_a_row_moved_to_another_listing_during_the_crawl_is_not_in_feed():
    db = FakeDB([_row("p1", "h1", LONG_OLD, crawled=LONG_OLD)], [_job("j1")])
    hook = lambda catalog: catalog["p1"].update(canonical_url=f"https://{HOST}/products/moved")  # noqa: E731
    _, s, _ = await _run([], db, {"j1": ([_pdp("p1", "h1", LONG_NEW)], [])}, crawl_hook=hook)
    assert s["not_in_feed"] == 1 and s["would_rewrite"] == 0


def test_a_safety_sentence_survives_only_as_whole_words():
    old = f"{LONG_OLD} Avoid eyes."
    assert rd.lost_safety_sentences(old, f"{LONG_NEW} Avoid eyeshadow fallout on the cheeks.") == ["Avoid eyes."]


async def test_a_revert_whose_record_is_not_stored_restores_nothing():
    db, run_id, _, _ = await _applied()
    written = copy.deepcopy(db.catalog)
    db.fail_event = True
    code, s, _ = await _run(["--revert", run_id, "--apply"], db, {})
    assert code == 3 and s["revert_failed"] == 2 and s["reverted"] == 0 and db.catalog == written
