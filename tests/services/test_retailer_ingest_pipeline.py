"""The retailer ingest state machine, driven through an in-memory ledger.

Every transition an operator made by hand on 2026-09-23 is a case here: a clean dry run advances to
apply, a mislabelled row holds the store, a throttled crawl is retried later (never back to back),
a capped crawl and a partial apply stop for a human, and an apply re-runs every check first.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import scripts.onboard_curated_brands as cli
from services import curated_brand_feed as feed
from services.retailer_ingest import pipeline

_REAL_RECORDS_FOR_BRAND = feed.records_for_brand


def record(title, ptype, handle, body="<p>A lip colour for soft, velvet lips.</p>", currency="USD"):
    return feed.shopify_product_to_record(
        {"id": abs(hash(handle)) % 10**9, "vendor": "3CE", "title": title, "handle": handle,
         "product_type": ptype, "body_html": body, "images": [{"src": "https://cdn.example/i.jpg"}],
         "variants": [{"id": abs(hash(handle + "v")) % 10**12, "price": "20.00", "available": True,
                       "sku": handle}]},
        domain="k-touch.us", category_path="beauty", brand_override="3CE", currency=currency,
        source_role="retailer", retailer_name="k-touch.us", emit_native_variants=True,
    )


TINT = ("3CE - Velvet Lip Tint Plush 4g", "LIP TINT", "velvet-lip-tint-plush")
# Its real k-touch.us description (2026-09-23): a tone-up cream that never mentions lips.
TONE_UP = ("3CE - TONE UP TINT 40ml", "LIP TINT", "3ce-tone-up-tint-40ml",
           "<p>Instantly Brighten and Refresh Your Complexion 3CE Tone Up Tint is a Korean tone-up cream "
           "designed to help improve the appearance of dull skin while creating a brighter complexion.</p>")
LIPSTICK_NO_TYPE = ("3CE - Soft Matte Lipstick 3.5g", "", "soft-matte")
PALETTE = ("3CE - New Take Eyeshadow Palette", "", "new-take")


class LockServer:
    """One Postgres server's advisory lock, as seen by FakeLockConnection sessions: held by at most
    one session, released by an unlock or by the holding session ending."""

    def __init__(self):
        self.holder = None
        self.sessions = []
        self.connect_error = None  # opening the lock connection fails
        self.try_error = None      # the try-lock statement fails

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        conn = FakeLockConnection(self)
        self.sessions.append(conn)
        return conn


class FakeLockConnection:
    def __init__(self, server):
        self.server, self.closed, self.statements = server, False, []

    async def fetchval(self, sql):
        assert not self.closed
        self.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            if self.server.try_error:
                raise self.server.try_error
            if self.server.holder in (None, self):
                self.server.holder = self
                return True
            return False
        if "pg_advisory_unlock" in sql:
            if self.server.holder is self:
                self.server.holder = None
                return True
            return False
        raise AssertionError(sql)

    def _end_session(self):
        self.closed = True
        if self.server.holder is self:
            self.server.holder = None

    async def close(self):
        self._end_session()

    def terminate(self):
        self._end_session()


class Ledger:
    def __init__(self):
        self.runs, self.transitions = {}, []
        self.unfinished = None      # the previous run a killed execution left behind
        self.current_status = None  # set to simulate an operator changing the job mid-stage
        self.lock_server = LockServer()
        self.lock_waits = []
        self.events = []  # ordering of the write marker vs the catalog write

    from db.retailer_ingest import CATALOG_WRITE_NOT_STARTED, CATALOG_WRITE_STARTED

    async def consecutive_outcomes(self, job_id, outcomes, *, limit, db=None):
        count = 0
        for run in reversed([r for r in self.runs.values() if "outcome" in r][-limit:]):
            if run["outcome"] not in outcomes:
                break
            count += 1
        return count

    async def mark_write_started(self, run_id, db=None):
        import asyncio
        await asyncio.sleep(0)  # a marker that is not awaited to completion lands AFTER the write
        if getattr(self, "mark_error", None):
            raise self.mark_error
        holder = self.lock_server.holder
        self.events.append(("mark_write_started", run_id, holder is not None and not holder.closed))
        self.runs[run_id]["catalog_write"] = self.CATALOG_WRITE_STARTED

    def catalog_write_lock(self, **kw):
        """The REAL lock, over a fake server: what the pipeline holds is what prod holds."""
        from db.retailer_ingest import catalog_write_lock
        self.lock_waits.append(kw)
        return catalog_write_lock(connect=self.lock_server.connect, **kw)

    async def unfinished_run(self, job_id, db=None):
        return self.unfinished

    async def start_run(self, *, job_id, stage, image_sha, execution, db=None):
        run_id = f"run{len(self.runs)}"
        self.runs[run_id] = {"job_id": job_id, "stage": stage}
        return run_id

    async def finish_run(self, run_id, **fields):
        fields.pop("db", None)
        self.runs.setdefault(run_id, {}).update(fields)

    async def transition(self, job_id, **fields):
        fields.pop("db", None)
        self.transitions.append(fields)
        expected = fields.get("expected_status")
        return self.current_status is None or expected is None or expected == self.current_status

    @staticmethod
    def backoff_until(attempts, **kw):
        from db.retailer_ingest import backoff_until
        return backoff_until(attempts, **kw)


@pytest.fixture
def env(monkeypatch):
    ledger = Ledger()
    monkeypatch.setattr(pipeline, "ledger", ledger)
    state = SimpleNamespace(ledger=ledger, rows=[TINT], crawl_error=None, applied=[], readback_rows=None,
                            apply_error=None, locked_during_apply=[])

    async def fetch(**kw):
        if state.crawl_error:
            raise state.crawl_error
        # Built INSIDE the crawl, as records_for_brand does, so the lip switch must be on here.
        return feed.ShopifyProductBatch([record(*r) for r in state.rows], scanned_products=len(state.rows), pages=1)
    monkeypatch.setattr(feed, "records_for_brand", fetch)

    async def clear(plan, *, check):
        return {"status": "clear", "conflict_count": 0, "rows_at_risk": 0}
    monkeypatch.setattr(cli, "_legacy_listing_report", clear)
    monkeypatch.setattr(cli, "_brand_host_guard_report", clear)

    from services.catalog_enrichment_agent import apply as apply_mod, primary_ingestion as pi

    async def fake_apply(plan, **kw):
        holder = ledger.lock_server.holder
        state.locked_during_apply.append(holder is not None and not holder.closed)
        ledger.events.append(("apply_ingest_plan",))
        if state.apply_error:
            raise state.apply_error
        state.applied.append(plan)
        return {"pdps": len(plan["pdps"]), "skus": len(plan["skus"]), "offers": len(plan["offers"])}
    monkeypatch.setattr(apply_mod, "apply_ingest_plan", fake_apply)

    def fake_require_apply(preflight, counts):
        products = [{"product_key": p["product_key"], "canonical_url": f"https://k-touch.us/products/{i}"}
                    for i, p in enumerate(state.applied[-1]["pdps"])]
        return {"status": "applied", "missing": {}, "applied": {**counts, "primary_readiness":
                {"status": "complete", "products": products}}}
    monkeypatch.setattr(pi, "require_primary_apply", fake_require_apply)

    class DB:
        async def fetch_all(self, sql, values):
            if state.readback_rows is not None:
                return state.readback_rows
            return [{"product_key": k, "category_path": "beauty/makeup/lip/tint", "serving": True,
                     "pipeline_stage": "public_indexed", "lifecycle": "published", "offers": 1,
                     "offers_in_currency": 1, "offers_in_market": 1}
                    for k in values["keys"]]
    state.db = DB()
    return state


def job(status="queued", **options):
    return {"id": "rij_1", "domain": "k-touch.us", "brand": "3CE", "status": status, "attempts": 0,
            "max_attempts": 6, "options": {"vendors": ["3CE"], "require_currency": "USD", **options}}


async def test_a_clean_dry_run_advances_to_apply(env):
    out = await pipeline.run_stage(job(), db=env.db)
    assert out["status"] == "apply_due" and out["outcome"] == "clean"
    assert env.ledger.transitions[-1]["status"] == "apply_due"
    assert env.applied == []  # a dry run never writes


async def test_the_mislabelled_row_holds_the_store_and_is_recorded(env):
    env.rows = [TINT, TONE_UP]
    out = await pipeline.run_stage(job(), db=env.db)
    assert out["status"] == "held"
    run = list(env.ledger.runs.values())[-1]
    held = {f["handle"] for f in run["flags"] if f["severity"] == "block"}
    assert held == {"3ce-tone-up-tint-40ml"}


async def test_an_approval_that_excludes_the_row_lets_the_store_apply(env):
    env.rows = [TINT, TONE_UP]
    out = await pipeline.run_stage(job("apply_due", exclude_handles=["3ce-tone-up-tint-40ml"]), db=env.db)
    assert out["status"] == "done" and out["outcome"] == "applied"
    assert len(env.applied) == 1 and len(env.applied[0]["pdps"]) == 1


async def test_an_apply_reruns_the_checks_and_writes_nothing_when_a_new_flag_appears(env):
    env.rows = [TINT, TONE_UP]  # the store changed after a clean dry run
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert out["status"] == "held" and env.applied == []


async def test_accepted_flag_keys_release_exactly_those_flags(env):
    env.rows = [TINT, TONE_UP]
    await pipeline.run_stage(job(), db=env.db)
    keys = [f["key"] for f in list(env.ledger.runs.values())[-1]["flags"] if f["severity"] == "block"]
    out = await pipeline.run_stage(job("apply_due", accepted_flags=keys[:1]), db=env.db)
    assert out["status"] == "held"
    out = await pipeline.run_stage(job("apply_due", accepted_flags=keys), db=env.db)
    assert out["status"] == "done"


async def test_a_throttled_crawl_is_retried_later_never_back_to_back(env):
    env.crawl_error = feed.CrawlIncomplete("k-touch.us: page 5: HTTP 429", status="failed", next_page=5,
                                           scanned_products=1000, selected_products=3)
    before = datetime.now(timezone.utc)
    out = await pipeline.run_stage(job(), db=env.db)
    t = env.ledger.transitions[-1]
    assert out["outcome"] == "crawl_throttled" and t["status"] == "queued" and t["count_attempt"]
    assert (t["next_run_at"] - before).total_seconds() >= 29 * 60


async def test_the_retry_budget_ends_in_failed(env):
    env.crawl_error = feed.CrawlIncomplete("x: page 1: HTTP 429", status="failed", next_page=1,
                                           scanned_products=0, selected_products=0)
    out = await pipeline.run_stage({**job(), "attempts": 5}, db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "crawl_throttled"


async def test_a_capped_crawl_stops_for_a_human(env):
    env.crawl_error = feed.CrawlIncomplete("x: page 81: scan budget 20000 exhausted", status="capped",
                                           next_page=81, scanned_products=20000, selected_products=2)
    out = await pipeline.run_stage(job(), db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "crawl_capped"


async def test_a_lip_pass_with_no_lip_rows_is_closed_as_nothing(env):
    env.rows = [PALETTE]
    out = await pipeline.run_stage(job(only_category="beauty/makeup/lip"), db=env.db)
    assert out["status"] == "nothing"


async def test_the_lip_title_option_reaches_the_crawl(env):
    env.rows = [LIPSTICK_NO_TYPE]
    assert (await pipeline.run_stage(job(only_category="beauty/makeup/lip"), db=env.db))["status"] == "nothing"
    out = await pipeline.run_stage(job(only_category="beauty/makeup/lip", lip_title_evidence=True), db=env.db)
    assert out["status"] == "held"  # the door placed it: held until that row is accepted
    out = await pipeline.run_stage(job("apply_due", only_category="beauty/makeup/lip", lip_title_evidence=True,
                                       accepted_flags=["placed_by_lip_title:soft-matte"]), db=env.db)
    assert out["status"] == "done"
    assert feed._LIP_TITLE_EVIDENCE.get() is False  # scoped to the stage


@pytest.mark.parametrize("lifecycle,noted", [("candidate", True), ("draft", True), (None, False),
                                             ("validated", False), ("published", False)])
async def test_a_row_backend_recall_cannot_see_is_noted_but_never_fails_the_job(env, lifecycle, noted):
    """The agent door serves on serving-eligibility alone (2026-09-24: it returned `candidate` O HUI
    rows); backend global recall admits validated/published/NULL. The run records the difference."""
    async def fetch_all(sql, values):
        return [{"product_key": k, "category_path": "beauty/skincare/moisturize/cream", "serving": True,
                 "pipeline_stage": "shadow_indexed", "lifecycle": lifecycle, "offers": 2, "offers_in_currency": 2, "offers_in_market": 2}
                for k in values["keys"]]
    env.db.fetch_all = fetch_all
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert (out["status"], out["outcome"]) == ("done", "applied")
    readback = list(env.ledger.runs.values())[-1]["readback"]
    assert readback["problems"] == []
    assert bool(readback["notes"]) is noted
    reason = env.ledger.transitions[-1]["reason"]
    if noted:
        assert readback["notes"][0]["note"] == f"outside backend global recall: pdp_lifecycle_stage {lifecycle!r}"
        assert reason.endswith("row(s) outside backend global recall")
    else:
        assert reason == "applied and verified"


async def test_a_readback_problem_fails_the_job_after_apply(env):
    env.readback_rows = []  # the gate said it landed; the catalog has none of it
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "readback_failed"
    run = list(env.ledger.runs.values())[-1]
    assert run["readback"]["problems"][0]["problem"] == "not in catalog_products"


async def test_a_job_without_vendors_is_refused(env):
    bad = job()
    bad["options"] = {"require_currency": "USD"}
    out = await pipeline.run_stage(bad, db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "invalid_job"


async def test_a_record_in_another_currency_stops_the_stage(env):
    env.rows = [TINT + ("<p>Soft velvet lips.</p>", "SGD")]
    out = await pipeline.run_stage(job(), db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "currency_unproven"


async def test_a_cancel_while_the_crawl_ran_is_not_overwritten(env):
    env.ledger.current_status = "cancelled"  # an operator cancelled mid-stage
    out = await pipeline.run_stage(job(), db=env.db)
    assert out.get("superseded") is True
    assert env.ledger.transitions[-1]["expected_status"] == "queued"


async def test_every_transition_is_conditional_on_the_claimed_status(env):
    env.rows = [TINT, TONE_UP]
    await pipeline.run_stage(job(), db=env.db)
    await pipeline.run_stage(job("apply_due", exclude_handles=["3ce-tone-up-tint-40ml"]), db=env.db)
    assert [t["expected_status"] for t in env.ledger.transitions] == ["queued", "apply_due"]


async def test_an_interrupted_apply_fails_and_is_never_reapplied(env):
    env.ledger.unfinished = {"id": "run_killed", "stage": "apply"}
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "interrupted"
    assert env.applied == []
    assert env.ledger.runs == {"run_killed": {"outcome": "interrupted",
                                              "error": env.ledger.runs["run_killed"]["error"]}}


async def test_an_apply_interrupted_after_its_write_marker_fails_as_may_be_partial(env):
    env.ledger.unfinished = {"id": "run_killed", "stage": "apply", "catalog_write": "started"}
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "interrupted" and "may be partial" in out["reason"]
    assert env.applied == [] and env.ledger.transitions[-1]["status"] == "failed"
    assert not env.ledger.transitions[-1].get("count_attempt")


async def test_an_apply_interrupted_before_its_write_began_is_retried_not_failed(env):
    # Killed in its crawl, checks or lock wait: the run still says catalog_write = not_started.
    env.ledger.unfinished = {"id": "run_killed", "stage": "apply", "catalog_write": "not_started"}
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert out == {"job_id": "rij_1", "stage": "apply", "outcome": "interrupted", "status": "apply_due"}
    t = env.ledger.transitions[-1]
    assert t["status"] == "apply_due" and t["expected_status"] == "apply_due" and t["count_attempt"]
    assert t["next_run_at"] is not None and "may be partial" not in t["reason"]
    assert env.ledger.runs["run_killed"]["outcome"] == "interrupted"
    assert "nothing was written" in env.ledger.runs["run_killed"]["error"]
    assert env.applied == []  # the retry is the next execution's stage, not this one


async def test_an_apply_interrupted_before_its_write_still_respects_the_retry_budget(env):
    env.ledger.unfinished = {"id": "run_killed", "stage": "apply", "catalog_write": "not_started"}
    spent = job("apply_due")
    spent["attempts"] = 5
    out = await pipeline.run_stage(spent, db=env.db)
    assert out["status"] == "failed" and env.ledger.transitions[-1]["reason"].startswith("retry budget spent")


async def test_the_write_marker_is_durable_before_the_catalog_write_starts(env):
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert out["outcome"] == "applied"
    run_id = list(env.ledger.runs)[-1]
    # marked while holding the lock, and before (not alongside) the write
    assert env.ledger.events == [("mark_write_started", run_id, True), ("apply_ingest_plan",)]
    assert env.ledger.runs[run_id]["checks"]["catalog_write"] == "started"


async def test_a_failed_write_marker_means_no_write(env):
    env.ledger.mark_error = OSError("ledger unreachable")
    with pytest.raises(OSError):
        await pipeline.run_stage(job("apply_due"), db=env.db)
    assert env.applied == [] and env.locked_during_apply == []
    assert _released(env.ledger.lock_server)


async def test_a_busy_write_lock_never_marks_the_write_started(env, monkeypatch):
    monkeypatch.setattr(pipeline, "WRITE_LOCK_WAIT_S", 0.02)
    monkeypatch.setattr(pipeline, "WRITE_LOCK_POLL_S", 0.01)
    other_apply = await env.ledger.lock_server.connect()
    assert await other_apply.fetchval(_TRY)
    assert (await pipeline.run_stage(job("apply_due"), db=env.db))["outcome"] == "write_lock_busy"
    assert env.ledger.events == []


async def test_an_interrupted_dry_run_spends_an_attempt_and_backs_off(env):
    env.ledger.unfinished = {"id": "run_killed", "stage": "dry_run"}
    out = await pipeline.run_stage(job(), db=env.db)
    t = env.ledger.transitions[-1]
    assert out["outcome"] == "interrupted" and t["status"] == "queued" and t["count_attempt"]
    assert t["next_run_at"] is not None


# ------------------------------------------------------------------ the catalog write lock

_TRY = "SELECT pg_try_advisory_lock(hashtext('retailer_ingest_catalog_write'))"


def _released(server):
    """Nobody holds the lock and every session the pipeline opened has ended."""
    return server.holder is None and all(s.closed for s in server.sessions)


async def test_an_apply_writes_only_while_holding_the_catalog_write_lock_then_releases_it(env):
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert out["outcome"] == "applied" and env.locked_during_apply == [True]
    assert _released(env.ledger.lock_server)
    assert env.ledger.lock_waits == [{"wait_s": pipeline.WRITE_LOCK_WAIT_S, "poll_s": pipeline.WRITE_LOCK_POLL_S}]


async def test_a_dry_run_never_takes_the_catalog_write_lock(env):
    await pipeline.run_stage(job(), db=env.db)
    assert env.ledger.lock_server.sessions == [] and env.ledger.lock_waits == []


async def test_a_busy_write_lock_writes_nothing_and_returns_the_job_to_apply_due_without_an_attempt(env, monkeypatch):
    monkeypatch.setattr(pipeline, "WRITE_LOCK_WAIT_S", 0.05)
    monkeypatch.setattr(pipeline, "WRITE_LOCK_POLL_S", 0.01)
    other_apply = await env.ledger.lock_server.connect()
    assert await other_apply.fetchval(_TRY)
    before = datetime.now(timezone.utc)
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert out["outcome"] == "write_lock_busy" and out["status"] == "apply_due"
    assert env.locked_during_apply == [] and env.applied == []  # apply_ingest_plan never called
    t = env.ledger.transitions[-1]
    assert t["status"] == "apply_due" and t["expected_status"] == "apply_due" and not t["count_attempt"]
    retry_in = (t["next_run_at"] - before).total_seconds()
    assert pipeline.WRITE_LOCK_BUSY_RETRY_S - 5 <= retry_in <= pipeline.WRITE_LOCK_BUSY_RETRY_S + 5
    run = list(env.ledger.runs.values())[-1]
    assert run["outcome"] == "write_lock_busy" and "nothing written" in run["error"]
    timings = run["checks"]["timings"]
    assert timings["write_lock_wait_s"] >= 0.05 and "write_s" not in timings
    # the other apply still holds its lock; the waiter's own session ended
    assert env.ledger.lock_server.holder is other_apply
    assert all(s.closed for s in env.ledger.lock_server.sessions if s is not other_apply)


async def test_a_write_lock_freed_during_the_wait_is_taken_and_the_apply_writes(env, monkeypatch):
    monkeypatch.setattr(pipeline, "WRITE_LOCK_POLL_S", 0.01)
    other_apply = await env.ledger.lock_server.connect()
    assert await other_apply.fetchval(_TRY)

    import asyncio

    async def finish_other_write():
        await asyncio.sleep(0.05)
        await other_apply.close()
    finisher = asyncio.ensure_future(finish_other_write())
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    await finisher
    assert out["outcome"] == "applied" and env.locked_during_apply == [True]
    assert list(env.ledger.runs.values())[-1]["checks"]["timings"]["write_lock_wait_s"] >= 0.05
    assert _released(env.ledger.lock_server)


@pytest.mark.parametrize("error, outcome", [(RuntimeError("connection lost mid-write"), "error"),
                                            (ValueError("primary apply incomplete"), "apply_refused")])
async def test_the_write_lock_is_released_when_the_catalog_write_raises(env, error, outcome):
    env.apply_error = error
    if outcome == "error":
        with pytest.raises(RuntimeError):
            await pipeline.run_stage(job("apply_due"), db=env.db)
    else:
        assert (await pipeline.run_stage(job("apply_due"), db=env.db))["outcome"] == outcome
    assert env.locked_during_apply == [True]
    assert _released(env.ledger.lock_server)
    run = list(env.ledger.runs.values())[-1]
    assert run["outcome"] == outcome and env.ledger.transitions[-1]["status"] == "failed"
    # the next apply can take the lock
    env.apply_error = None
    assert (await pipeline.run_stage(job("apply_due"), db=env.db))["outcome"] == "applied"


async def test_every_stage_records_its_phase_timings(env):
    import json as _json

    await pipeline.run_stage(job(), db=env.db)
    dry = list(env.ledger.runs.values())[-1]["checks"]["timings"]
    assert set(dry) == {"crawl_s", "check_s"}
    await pipeline.run_stage(job("apply_due"), db=env.db)
    applied = list(env.ledger.runs.values())[-1]["checks"]["timings"]
    assert set(applied) == {"crawl_s", "check_s", "write_lock_wait_s", "write_s", "readback_s"}
    for value in [*dry.values(), *applied.values()]:
        assert isinstance(value, float) and value >= 0
    _json.loads(_json.dumps(applied, allow_nan=False))  # JSON-safe as recorded
    # a stage that stops in its crawl still records how long the crawl took
    env.crawl_error = feed.CrawlIncomplete("x: HTTP 429", status="failed", next_page=1, scanned_products=0,
                                           selected_products=0)
    await pipeline.run_stage(job(), db=env.db)
    assert set(list(env.ledger.runs.values())[-1]["checks"]["timings"]) == {"crawl_s"}


async def test_the_lock_is_released_by_the_session_ending_when_the_unlock_fails():
    from db.retailer_ingest import catalog_write_lock

    server = LockServer()

    class UnlockFails(FakeLockConnection):
        async def fetchval(self, sql):
            if "pg_advisory_unlock" in sql:
                raise OSError("connection reset")
            return await super().fetchval(sql)

    async def connect():
        conn = UnlockFails(server)
        server.sessions.append(conn)
        return conn
    async with catalog_write_lock(wait_s=1, connect=connect):
        assert server.holder is server.sessions[0]
    assert _released(server)


async def test_a_cancelled_write_still_ends_the_lock_session():
    import asyncio

    from db.retailer_ingest import catalog_write_lock

    server = LockServer()
    entered = asyncio.Event()

    async def write():
        async with catalog_write_lock(wait_s=1, connect=server.connect):
            entered.set()
            await asyncio.sleep(3600)
    task = asyncio.ensure_future(write())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _released(server)


async def test_a_busy_lock_never_runs_the_block_and_closes_its_session():
    from db.retailer_ingest import CatalogWriteLockBusy, catalog_write_lock

    server = LockServer()
    holder = await server.connect()
    assert await holder.fetchval(_TRY)
    ran = []
    with pytest.raises(CatalogWriteLockBusy):
        async with catalog_write_lock(wait_s=0.03, poll_s=0.01, connect=server.connect):
            ran.append(True)
    assert ran == [] and server.sessions[1].closed and server.holder is holder


# ------------------------------------------------------------------ the write deadline, lock errors, starvation

async def test_no_time_left_for_the_write_means_no_marker_no_write_and_back_to_apply_due(env):
    out = await pipeline.run_stage(job("apply_due"), db=env.db, time_left_s=pipeline.WRITE_MARGIN_S - 1)
    assert out["outcome"] == "write_lock_busy" and out["status"] == "apply_due"
    assert "no time left" in out["reason"]
    assert env.ledger.events == [] and env.applied == []          # never marked, never written
    assert env.ledger.lock_server.sessions == []                   # never even waited for the lock
    t = env.ledger.transitions[-1]
    assert t["status"] == "apply_due" and not t["count_attempt"] and t["next_run_at"] is not None
    run = list(env.ledger.runs.values())[-1]
    assert run["checks"]["catalog_write"] == "not_started" and run["checks"]["timings"]["write_lock_wait_s"] == 0.0


async def test_the_lock_wait_is_cut_to_the_task_deadline(env, monkeypatch):
    monkeypatch.setattr(pipeline, "WRITE_LOCK_WAIT_S", 1.0)
    monkeypatch.setattr(pipeline, "WRITE_LOCK_POLL_S", 0.01)
    other_apply = await env.ledger.lock_server.connect()
    assert await other_apply.fetchval(_TRY)
    out = await pipeline.run_stage(job("apply_due"), db=env.db, time_left_s=pipeline.WRITE_MARGIN_S + 0.1)
    assert out["outcome"] == "write_lock_busy" and env.ledger.events == [] and env.applied == []
    [wait] = env.ledger.lock_waits
    assert 0 < wait["wait_s"] <= 0.1  # the deadline, not WRITE_LOCK_WAIT_S
    assert list(env.ledger.runs.values())[-1]["checks"]["timings"]["write_lock_wait_s"] < 0.5


async def test_with_time_to_spare_the_wait_is_the_full_wait(env):
    out = await pipeline.run_stage(job("apply_due"), db=env.db, time_left_s=3600)
    assert out["outcome"] == "applied" and env.ledger.lock_waits[0]["wait_s"] == pipeline.WRITE_LOCK_WAIT_S


@pytest.mark.parametrize("where", ["connect", "try_lock"])
async def test_a_lock_connection_failure_is_retried_not_failed(env, where):
    if where == "connect":
        env.ledger.lock_server.connect_error = ConnectionRefusedError("db proxy down")
    else:
        env.ledger.lock_server.try_error = OSError("connection reset")
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert out["outcome"] == "write_lock_unavailable" and out["status"] == "apply_due"
    assert ("ConnectionRefusedError" if where == "connect" else "OSError") in out["reason"]
    assert env.ledger.events == [] and env.applied == []
    t = env.ledger.transitions[-1]
    assert t["status"] == "apply_due" and not t["count_attempt"] and t["next_run_at"] is not None
    assert list(env.ledger.runs.values())[-1]["checks"]["catalog_write"] == "not_started"
    assert all(s.closed for s in env.ledger.lock_server.sessions)


async def test_every_apply_that_ends_before_its_write_records_that_nothing_was_written(env):
    # finish_run replaces the run's checks: the not_started evidence start_run wrote must survive it
    env.crawl_error = feed.CrawlIncomplete("x: HTTP 429", status="failed", next_page=1, scanned_products=0,
                                           selected_products=0)
    await pipeline.run_stage(job("apply_due"), db=env.db)   # stopped in its crawl
    assert list(env.ledger.runs.values())[-1]["checks"]["catalog_write"] == "not_started"
    env.crawl_error, env.rows = None, [TINT, TONE_UP]
    assert (await pipeline.run_stage(job("apply_due"), db=env.db))["outcome"] == "held"  # held by a new flag
    assert list(env.ledger.runs.values())[-1]["checks"]["catalog_write"] == "not_started"
    await pipeline.run_stage(job(), db=env.db)              # a dry run carries no write marker
    assert "catalog_write" not in list(env.ledger.runs.values())[-1]["checks"]


def _prior_runs(env, outcomes):
    for i, outcome in enumerate(outcomes):
        env.ledger.runs[f"prior{i}"] = {"job_id": "rij_1", "stage": "apply", "outcome": outcome}


class _Warnings:
    def __init__(self):
        self.lines = []

    def warning(self, msg, *args):
        self.lines.append(msg % args)


@pytest.mark.parametrize("prior, starved", [
    (["write_lock_busy"] * (pipeline.WRITE_LOCK_STARVED_AFTER - 1), True),
    (["write_lock_busy", "write_lock_unavailable"] * 5 + ["write_lock_busy"], True),  # both kinds count
    (["write_lock_busy"] * (pipeline.WRITE_LOCK_STARVED_AFTER - 2), False),
    # a streak broken by any other outcome starts again (a hold that was approved breaks it too)
    (["write_lock_busy"] * 20 + ["write_lock_starved"] + ["write_lock_busy"] * 3, False),
    (["write_lock_busy"] * 20 + ["clean"], False),
])
async def test_consecutive_busy_applies_hold_the_job_and_warn_at_the_threshold(env, monkeypatch, prior, starved):
    warnings = _Warnings()
    monkeypatch.setattr(pipeline, "logger", warnings)
    monkeypatch.setattr(pipeline, "WRITE_LOCK_WAIT_S", 0.02)
    monkeypatch.setattr(pipeline, "WRITE_LOCK_POLL_S", 0.01)
    _prior_runs(env, prior)
    other_apply = await env.ledger.lock_server.connect()
    assert await other_apply.fetchval(_TRY)
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert env.applied == [] and env.ledger.events == []
    t = env.ledger.transitions[-1]
    assert not t["count_attempt"]
    if starved:
        assert out["outcome"] == "write_lock_starved" and out["status"] == "held" and t["status"] == "held"
        assert len(warnings.lines) == 1 and "rij_1" in warnings.lines[0]
    else:
        assert out["outcome"] == "write_lock_busy" and out["status"] == "apply_due" and warnings.lines == []


async def test_cohort_level_flags_cannot_be_accepted(env, monkeypatch):
    async def conflict(plan, *, check):
        return {"status": "conflicts", "conflict_count": 1, "rows_at_risk": 5}
    monkeypatch.setattr(cli, "_brand_host_guard_report", conflict)
    out = await pipeline.run_stage(job("apply_due", accepted_flags=["brand_host_guard"]), db=env.db)
    assert out["status"] == "held" and env.applied == []


async def test_a_read_error_is_transient(env):
    env.crawl_error = feed.CrawlIncomplete("x: page 3: ReadError: connection reset", status="failed",
                                           next_page=3, scanned_products=500, selected_products=2)
    out = await pipeline.run_stage(job(), db=env.db)
    assert out["outcome"] == "crawl_throttled" and out["status"] == "queued"


@pytest.mark.parametrize("options", [
    {"vendors": ["3CE"], "category_path": "beauty/makeup/lip/lipstick"},  # a leaf fallback
    {"vendors": "3CE"},                                                    # a string splits into letters
    {"vendors": ["3CE"], "max_scan_products": "20000"},
    {"vendors": ["3CE"], "apply_now": True},
    {"vendors": ["3CE"], "category_path": "beautyfoo/x"},
])
async def test_invalid_options_are_refused_before_any_crawl(env, options):
    bad = job()
    bad["options"] = options
    out = await pipeline.run_stage(bad, db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "invalid_job"


async def test_a_null_option_is_an_absent_one(env):
    out = await pipeline.run_stage(job(exclude_handles=None, accepted_flags=None), db=env.db)
    assert out["status"] == "apply_due"


async def test_an_exclusion_the_merchant_delisted_can_be_accepted(env):
    out = await pipeline.run_stage(job("apply_due", exclude_handles=["gone-product"]), db=env.db)
    assert out["status"] == "held"
    out = await pipeline.run_stage(job("apply_due", exclude_handles=["gone-product"],
                                       accepted_flags=["exclude_handle_unmatched:gone-product"]), db=env.db)
    assert out["status"] == "done"


# --- the affiliate datafeed source (services/retailer_ingest/affiliate_feed.py) ---------------------

OY_FEED = {"network": "example_network", "url_env": "AFFILIATE_FEED_OY_TEST", "format": "csv",
           "retailer_host": "global.oliveyoung.com", "link_hosts": ["invl.example-network.com"],
           "fields": {"id": "sku", "title": "name", "brand": "brand", "product_url": "page", "link": "click",
                      "price": "price", "currency": "ccy", "category": "cat", "description": "desc"}}
OY_CSV = ("sku,name,brand,page,click,price,ccy,cat,desc\n"
          "GA1,3CE Velvet Lip Tint,3CE,https://global.oliveyoung.com/product/detail?prdtNo=GA1,"
          "https://invl.example-network.com/c/GA1,18.00,USD,Lip Tint,A soft velvet colour for lips.\n"
          "GA2,3CE Blur Water Tint,3CE,https://global.oliveyoung.com/product/detail?prdtNo=GA2,"
          "https://invl.example-network.com/c/GA2,17.00,USD,Lip Tint,A water tint for lips.\n")


def feed_job(status="queued", **overrides):
    j = job(status, source="affiliate_feed", feed=OY_FEED)
    return {**j, "domain": "global.oliveyoung.com", **overrides}


@pytest.fixture
def oy(env, monkeypatch):
    from services.retailer_ingest import affiliate_feed as af
    env.crawl_error = AssertionError("the storefront crawler must not run for a feed job")
    env.feed = OY_CSV
    env.feed_error = None

    async def fetch(feed, *, env: dict, timeout_s=120.0):
        if state.feed_error:
            raise state.feed_error
        return state.feed
    state = env
    monkeypatch.setattr(af, "fetch_feed_text", fetch)

    from services.catalog_enrichment_agent import primary_ingestion as pi

    def require_apply(preflight, counts):  # readiness reports each applied product's real listing URL
        import json
        urls = {o["product_key"]: json.loads(o["offer_payload"])["canonical_url"] for o in state.applied[-1]["offers"]}
        products = [{"product_key": p["product_key"], "canonical_url": urls[p["product_key"]]}
                    for p in state.applied[-1]["pdps"]]
        return {"status": "applied", "missing": {}, "applied": {**counts, "primary_readiness":
                {"status": "complete", "products": products}}}
    monkeypatch.setattr(pi, "require_primary_apply", require_apply)
    return env


async def test_a_feed_job_dry_runs_clean_from_the_feed_not_the_storefront(oy):
    out = await pipeline.run_stage(feed_job(), db=oy.db)
    assert out["status"] == "apply_due" and out["outcome"] == "clean"
    run = list(oy.ledger.runs.values())[-1]
    assert run["checks"]["crawl"]["source"] == "affiliate_feed:example_network"
    assert run["checks"]["crawl"]["rows_kept"] == 2 and run["checks"]["crawl"]["sku_links_collapsed"] == 0
    assert run["checks"]["selected"] == 2


async def test_a_feed_job_applies_one_listing_per_product(oy):
    out = await pipeline.run_stage(feed_job("apply_due"), db=oy.db)
    assert out["status"] == "done"
    [plan] = oy.applied
    assert len(plan["pdps"]) == 2
    assert {o["source_ref"] for o in plan["offers"]} == {
        "https://invl.example-network.com/c/GA1", "https://invl.example-network.com/c/GA2"}


@pytest.mark.parametrize("code", [429, 503])
async def test_a_throttled_feed_download_backs_off(oy, code):
    from services.retailer_ingest.affiliate_feed import FeedError
    oy.feed_error = FeedError(f"feed download answered HTTP {code}")
    out = await pipeline.run_stage(feed_job(), db=oy.db)
    assert out["outcome"] == "crawl_throttled" and out["status"] == "queued"


async def test_a_feed_timeout_backs_off(oy):
    import httpx
    oy.feed_error = httpx.ReadTimeout("slow")
    out = await pipeline.run_stage(feed_job(), db=oy.db)
    assert out["outcome"] == "crawl_throttled" and out["status"] == "queued"


@pytest.mark.parametrize("error_or_text", [
    "403",
    "sku,name,brand\nGA1,3CE Velvet Lip Tint,3CE\n",   # the declared mapping is not this feed's header
    OY_CSV.replace("invl.example-network.com/c/GA2", "evil.example.org/c/GA2"),
])
async def test_an_untrustworthy_feed_fails_the_job_for_a_human(oy, error_or_text):
    from services.retailer_ingest.affiliate_feed import FeedError
    if error_or_text == "403":
        oy.feed_error = FeedError("feed download answered HTTP 403")
    else:
        oy.feed = error_or_text
    out = await pipeline.run_stage(feed_job(), db=oy.db)
    assert out["status"] == "failed" and out["outcome"] == "feed_invalid" and oy.applied == []


async def test_a_feed_for_another_retailer_is_refused(oy):
    out = await pipeline.run_stage(feed_job(domain="k-touch.us"), db=oy.db)
    assert out["status"] == "failed" and out["outcome"] == "invalid_job"


@pytest.mark.parametrize("options", [
    {"vendors": ["3CE"], "source": "affiliate_feed"},                        # no feed block
    {"vendors": ["3CE"], "feed": OY_FEED},                                   # a feed without the source
    {"vendors": ["3CE"], "source": "scrape"},
    {"vendors": ["3CE"], "source": "affiliate_feed", "feed": {**OY_FEED, "url_env": "https://x.example/f?t=1"}},
])
async def test_invalid_feed_options_are_refused_before_any_download(oy, options):
    bad = feed_job()
    bad["options"] = options
    out = await pipeline.run_stage(bad, db=oy.db)
    assert out["status"] == "failed" and out["outcome"] == "invalid_job"


async def test_the_feed_url_never_reaches_the_ledger(env, monkeypatch):
    """The URL embeds the publisher token. Neither a refusal nor a transport error may echo it."""
    import httpx
    secret = "https://feeds.example-network.com/p/12345?token=SEKRET-TOKEN"
    monkeypatch.setenv("AFFILIATE_FEED_OY_TEST", secret)
    env.crawl_error = AssertionError("storefront crawler must not run")
    real = httpx.AsyncClient
    outcomes = iter([
        lambda req: httpx.Response(401, text=f"bad token for {req.url}"),
        lambda req: (_ for _ in ()).throw(httpx.ConnectError(f"cannot reach {req.url}", request=req)),
        lambda req: (_ for _ in ()).throw(httpx.TooManyRedirects(f"loop at {req.url}", request=req)),
    ])
    for expected in ("feed_invalid", "crawl_throttled", "feed_invalid"):
        handler = next(outcomes)
        monkeypatch.setattr(httpx, "AsyncClient",
                            lambda handler=handler, **kw: real(transport=httpx.MockTransport(handler), **kw))
        out = await pipeline.run_stage(feed_job(), db=env.db)
        assert out["outcome"] == expected
    assert "SEKRET" not in repr(env.ledger.runs) + repr(env.ledger.transitions)


async def test_an_olive_young_product_can_be_excluded_by_its_product_id(oy):
    j = feed_job("apply_due")
    j["options"]["exclude_handles"] = ["GA2"]
    out = await pipeline.run_stage(j, db=oy.db)
    assert out["status"] == "done" and len(oy.applied[-1]["pdps"]) == 1


# --- what the category filter left out is recorded on the run, not only its count ------------------

async def test_a_resolved_category_pass_records_each_row_it_left_out_and_why(env):
    env.rows = [TINT, LIPSTICK_NO_TYPE]  # no merchant type and no lip-title evidence: cannot be placed
    out = await pipeline.run_stage(job(only_resolved_category=True), db=env.db)
    assert out["status"] == "apply_due"
    checks = list(env.ledger.runs.values())[-1]["checks"]
    assert checks["kept"] == 1 and checks["left_out"]["count"] == 1
    assert checks["left_out"]["by_reason"] == {"category_unresolved": 1}
    [row] = checks["left_out"]["rows"]
    assert row["handle"] == "soft-matte" and row["product_name"] == LIPSTICK_NO_TYPE[0]


async def test_a_pass_that_keeps_nothing_still_records_what_it_left_out(env):
    env.rows = [LIPSTICK_NO_TYPE, PALETTE]
    out = await pipeline.run_stage(job(only_resolved_category=True), db=env.db)
    assert out["status"] == "nothing" and out["outcome"] == "nothing_to_ingest"
    checks = list(env.ledger.runs.values())[-1]["checks"]
    assert checks["kept"] == 0 and checks["left_out"]["count"] == 2
    assert {r["handle"] for r in checks["left_out"]["rows"]} == {"soft-matte", "new-take"}


@pytest.mark.parametrize("n,truncated", [(pipeline.LEFT_OUT_ROWS_CAP, False), (pipeline.LEFT_OUT_ROWS_CAP + 1, True)])
def test_the_left_out_list_is_capped_but_the_counts_are_complete(n, truncated):
    entries = [{"reason": "category_unresolved", "product_name": f"p{i}", "merchant_product_type": "Misc",
                "handle": f"h{i}"} for i in range(n)]
    summary = pipeline._left_out_summary(entries)
    assert summary["count"] == n and summary["rows_truncated"] is truncated
    assert len(summary["rows"]) == min(n, pipeline.LEFT_OUT_ROWS_CAP)
    assert summary["by_merchant_type"] == {"Misc": n}


def test_merchant_types_are_capped_and_say_so():
    entries = [{"reason": "category_unresolved", "merchant_product_type": f"T{i}"} for i in range(pipeline.LEFT_OUT_TYPES_CAP + 1)]
    summary = pipeline._left_out_summary(entries)
    assert len(summary["by_merchant_type"]) == pipeline.LEFT_OUT_TYPES_CAP and summary["merchant_types_truncated"]
    assert not pipeline._left_out_summary(entries[:pipeline.LEFT_OUT_TYPES_CAP])["merchant_types_truncated"]


def test_a_hostile_product_name_cannot_stop_the_run_row_being_written():
    """jsonb refuses \\u0000 and NaN; the ledger serialises with db.retailer_ingest._dumps."""
    from db.retailer_ingest import _dumps
    summary = pipeline._left_out_summary([{"reason": "category_unresolved", "handle": "bad\ud800name",
                                           "product_name": "a\x00b" + "x" * 5000,
                                           "category_path": float("nan"), "merchant_product_type": "\x00Misc"}])
    text = _dumps(summary)
    assert "\\u0000" not in text and "NaN" not in text
    text.encode("utf-8")  # a lone surrogate would raise here
    [row] = summary["rows"]
    assert row["product_name"].startswith("ab") and len(row["product_name"]) == pipeline.LEFT_OUT_STR_CAP
    assert row["category_path"] is None and summary["by_merchant_type"] == {"Misc": 1}


async def test_a_resolved_row_outside_the_prefix_is_recorded_as_outside_the_filter(env, capsys):
    env.rows = [TINT, ("3CE - Multi Eye Color Palette", "Eyeshadow", "multi-eye")]  # resolves under eye
    out = await pipeline.run_stage(job(only_category="beauty/makeup/lip", lip_title_evidence=True), db=env.db)
    checks = list(env.ledger.runs.values())[-1]["checks"]
    assert out["status"] in {"apply_due", "held"}
    assert checks["left_out"]["by_reason"] == {"outside_category_filter": 1}
    # ...and the job log carries the same per-row line the CLI prints, with exactly the printed keys.
    import json
    from scripts.onboard_curated_brands import LEFT_OUT_PDP_PREFIX, LEFT_OUT_PRINTED_KEYS
    [line] = [l for l in capsys.readouterr().out.splitlines() if l.startswith(LEFT_OUT_PDP_PREFIX)]
    assert set(json.loads(line[len(LEFT_OUT_PDP_PREFIX):])) == set(LEFT_OUT_PRINTED_KEYS)


async def test_the_run_reason_counts_every_noted_row(env):
    env.rows = [TINT, ("3CE - Velvet Lip Tint Rose 4g", "LIP TINT", "velvet-lip-tint-rose")]
    async def fetch_all(sql, values):
        return [{"product_key": k, "category_path": "beauty/makeup/lip/tint", "serving": True,
                 "pipeline_stage": "shadow_indexed", "lifecycle": "candidate", "offers": 1, "offers_in_currency": 1, "offers_in_market": 1}
                for k in values["keys"]]
    env.db.fetch_all = fetch_all
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert out["status"] == "done"
    assert env.ledger.transitions[-1]["reason"] == "applied and verified; 2 row(s) outside backend global recall"


async def test_an_empty_readback_still_carries_its_notes_list():
    readback = await pipeline._readback([], "USD", db=None)
    assert readback == {"ok": False, "reason": "no product keys to read back", "notes": [], "rows": []}


# ------------------------------------------------------------------ source_role (a brand's own store)

async def test_the_source_role_option_reaches_the_crawl_and_defaults_to_retailer(env, monkeypatch):
    seen = []
    built = feed.records_for_brand

    async def spy(**kw):
        seen.append((kw.get("source_role"), kw.get("retailer_name")))
        return await built(**kw)
    monkeypatch.setattr(feed, "records_for_brand", spy)
    await pipeline.run_stage(job(), db=env.db)
    await pipeline.run_stage(job(source_role="brand_official"), db=env.db)
    assert seen == [("retailer", "k-touch.us"), ("brand_official", None)]  # a retailer is named by its host


async def test_a_brand_official_cohort_applies_with_brand_authority(env, monkeypatch):
    """The brand's own store: offers carry the brand's merchant identity and brand-official INCI
    authority, not a reseller's -- the same records Lane A (onboard_curated_brands.py) writes."""
    def official(title, ptype, handle, body="<p>Ingredients: Water, Glycerin, Dimethicone</p>"):
        return feed.shopify_product_to_record(
            {"id": abs(hash(handle)) % 10**9, "vendor": "3CE", "title": title, "handle": handle,
             "product_type": ptype, "body_html": body, "images": [{"src": "https://cdn.example/i.jpg"}],
             "variants": [{"id": abs(hash(handle + "v")) % 10**12, "price": "20.00", "available": True,
                           "sku": handle}]},
            domain="k-touch.us", category_path="beauty", brand_override="3CE", currency="USD",
            source_role="brand_official", emit_native_variants=True)

    async def fetch(**kw):
        assert kw["source_role"] == "brand_official"
        return feed.ShopifyProductBatch([official(*TINT)], scanned_products=1, pages=1)
    monkeypatch.setattr(feed, "records_for_brand", fetch)
    # k-touch.us is not named 3CE: held until a human says it is 3CE's own store.
    assert (await pipeline.run_stage(job(source_role="brand_official"), db=env.db))["status"] == "held"
    accept = ["brand_official_domain_unproven:k-touch.us:3ce"]
    assert (await pipeline.run_stage(job(source_role="brand_official", accepted_flags=accept),
                                     db=env.db))["status"] == "apply_due"
    out = await pipeline.run_stage(job("apply_due", source_role="brand_official", accepted_flags=accept), db=env.db)
    assert out["status"] == "done", env.ledger.runs
    import json
    offer = env.applied[-1]["offers"][0]
    assert not offer["merchant_id"].startswith("agent_seed::retailer::")
    assert json.loads(offer["offer_payload"])["merchant_inferred"] == "3CE"
    assert not env.applied[-1]["pdps"][0]["product_key"].startswith("ext:retailer:")


@pytest.mark.parametrize("options", [
    {"vendors": ["3CE"], "source_role": "brand"},                                   # not a role
    {"vendors": ["3CE"], "source_role": "brand_official", "retailer_name": "X"},     # a retailer's name
    {"vendors": ["3CE"], "source_role": True},                                      # typed
])
async def test_a_bad_source_role_is_refused_before_any_crawl(env, options):
    env.crawl_error = AssertionError("must not crawl")
    bad = job()
    bad["options"] = options
    out = await pipeline.run_stage(bad, db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "invalid_job"


def test_an_affiliate_feed_is_retailer_only():
    with pytest.raises(ValueError, match="source_role = retailer"):
        pipeline.validate_options({"vendors": ["X"], "source": "affiliate_feed", "source_role": "brand_official",
                                   "feed": {}})


async def test_a_brand_store_named_for_the_brand_needs_no_approval(env):
    j = job(source_role="brand_official")
    j["domain"] = "3ce.com"
    out = await pipeline.run_stage(j, db=env.db)
    assert out["status"] == "apply_due"


async def test_a_known_retailer_can_never_be_a_brand_official_store(env):
    for accepted in ([], ["brand_official_on_a_retailer"]):
        j = job(source_role="brand_official", accepted_flags=accepted)
        j["domain"] = "sephora.com"
        out = await pipeline.run_stage(j, db=env.db)
        assert out["status"] == "held"
        run = list(env.ledger.runs.values())[-1]
        assert [f["rule"] for f in run["flags"] if f["rule"].startswith("brand_official")] == ["brand_official_on_a_retailer"]


async def test_the_retailer_default_raises_no_domain_flag(env):
    j = job()
    j["domain"] = "sephora.com"
    await pipeline.run_stage(j, db=env.db)
    run = list(env.ledger.runs.values())[-1]
    assert not [f for f in run.get("flags") or [] if f["rule"].startswith("brand_official")]


async def test_naming_the_store_as_the_brand_cannot_write_another_brands_rows(env, monkeypatch):
    """Review round 2: brand="K-Touch" owns k-touch.us, but the records' brand is their vendor (3CE),
    whose canonical keys they would overwrite. Every written brand must own the host."""
    def official(title, ptype, handle):
        return feed.shopify_product_to_record(
            {"id": abs(hash(handle)) % 10**9, "vendor": "3CE", "title": title, "handle": handle,
             "product_type": ptype, "body_html": "<p>x</p>", "images": [{"src": "https://cdn.example/i.jpg"}],
             "variants": [{"id": abs(hash(handle + "v")) % 10**12, "price": "20.00", "available": True, "sku": handle}]},
            domain="k-touch.us", category_path="beauty", brand_override="K-Touch", currency="USD",
            source_role="brand_official", emit_native_variants=True)

    async def fetch(**kw):
        return feed.ShopifyProductBatch([official(*TINT)], scanned_products=1, pages=1)
    monkeypatch.setattr(feed, "records_for_brand", fetch)
    j = job(source_role="brand_official")
    j["brand"] = "K-Touch"
    out = await pipeline.run_stage(j, db=env.db)
    assert out["status"] == "held"
    run = list(env.ledger.runs.values())[-1]
    assert [f["key"] for f in run["flags"] if f["rule"] == "brand_official_domain_unproven"] == [
        "brand_official_domain_unproven:k-touch.us:3ce"]


def test_each_non_latin_brand_needs_its_own_acceptance():
    keys = [f["key"] for f in pipeline.brand_official_domain_flags("k-touch.us", ["설화수", "헤라", "HERA"])]
    assert keys == ["brand_official_domain_unproven:k-touch.us:설화수",
                    "brand_official_domain_unproven:k-touch.us:헤라",
                    "brand_official_domain_unproven:k-touch.us:hera"]


# ------------------------------------------------------------------ multi_brand (one crawl, many brands)

async def test_a_multi_brand_cohort_crawls_once_and_keeps_every_vendor_as_its_brand(env, monkeypatch):
    seen = []

    async def fetch(**kw):
        seen.append({k: kw.get(k) for k in ("brand", "only_vendors", "source_role")})
        return feed.ShopifyProductBatch([feed.shopify_product_to_record(
            {"id": 9100000 + i, "vendor": vendor, "title": title, "handle": handle, "product_type": "LIP TINT",
             "body_html": "<p>A lip colour for soft, velvet lips.</p>", "images": [{"src": "https://cdn.example/i.jpg"}],
             "variants": [{"id": 45000000000000 + i, "price": "20.00", "available": True, "sku": handle}]},
            domain="k-touch.us", category_path="beauty", brand_override=kw.get("brand"), currency="USD",
            source_role="retailer", retailer_name="k-touch.us", emit_native_variants=True)
            for i, (vendor, title, handle) in enumerate([("3CE", "3CE Velvet Lip Tint", "velvet"),
                                                         ("rom&nd", "Juicy Lasting Tint", "juicy")])],
            scanned_products=2, pages=1)
    monkeypatch.setattr(feed, "records_for_brand", fetch)
    opts = {"multi_brand": True, "vendors": ["3CE", "rom&nd"], "brands": {"3CE": "3CE", "rom&nd": "rom&nd"}}
    assert (await pipeline.run_stage({**job(**opts), "brand": "k-touch.us (2 brands)"}, db=env.db))["status"] == "apply_due"
    out = await pipeline.run_stage({**job("apply_due", **opts), "brand": "k-touch.us (2 brands)"}, db=env.db)
    assert out["status"] == "done", env.ledger.runs
    assert seen[0]["brand"] is None and sorted(seen[0]["only_vendors"]) == ["3ce", "rom&nd"]
    assert sorted(p["brand"] for p in env.applied[-1]["pdps"]) == ["3CE", "Rom&nd"]  # vendor, proper-cased


@pytest.mark.parametrize("options", [
    {"vendors": ["3CE"], "multi_brand": True, "source_role": "brand_official"},
    {"vendors": ["3CE"], "multi_brand": "yes"},
])
async def test_multi_brand_is_retailer_storefront_only(env, options):
    env.crawl_error = AssertionError("must not crawl")
    bad = job()
    bad["options"] = options
    out = await pipeline.run_stage(bad, db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "invalid_job"


def test_multi_brand_refuses_an_affiliate_feed():
    with pytest.raises(ValueError, match="multi_brand"):
        pipeline.validate_options({"vendors": ["X"], "multi_brand": True, "source": "affiliate_feed", "feed": {}})


async def test_multi_brand_writes_each_vendors_canonical_spelling(env, monkeypatch):
    """Review of #2301: with no override a row kept the STORE's spelling ("Dr. Jart+"), which
    normalize_brand keys apart from the catalog's "Dr.Jart+". options.brands restores the per-brand
    override, through the same resolve_record_brand rule, inside the one crawl."""
    products = [
        {"id": 9100001, "vendor": "Dr. Jart+", "title": "Cicapair Lip Tint", "handle": "cicapair",
         "product_type": "LIP TINT", "body_html": "<p>A lip colour for soft, velvet lips.</p>",
         "images": [{"src": "https://cdn.example/i.jpg"}],
         "variants": [{"id": 45000000000001, "price": "48.00", "available": True, "sku": "cica"}]},
        {"id": 9100002, "vendor": "SKIN1004", "title": "Centella Lip Tint", "handle": "ampoule",
         "product_type": "LIP TINT", "body_html": "<p>A lip colour for soft, velvet lips.</p>",
         "images": [{"src": "https://cdn.example/i.jpg"}],
         "variants": [{"id": 45000000000002, "price": "20.00", "available": True, "sku": "amp"}]},
    ]

    async def fetch_products(domain, **kw):
        return feed.ShopifyProductBatch(feed.filter_products_by_vendor(products, kw.get("only_vendors")),
                                        scanned_products=2, pages=2)

    async def locale(domain, **kw):
        return {"currency": "USD"}
    monkeypatch.setattr(feed, "fetch_shopify_products", fetch_products)
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", locale)
    monkeypatch.setattr(feed, "records_for_brand", _REAL_RECORDS_FOR_BRAND)
    opts = dict(multi_brand=True, vendors=["Dr. Jart+", "SKIN1004"],
                brands={"Dr. Jart+": "Dr.Jart+", "SKIN1004": "Skin1004"})
    first = await pipeline.run_stage({**job(**opts), "brand": "store (2 brands)"}, db=env.db)
    assert first["status"] == "apply_due", list(env.ledger.runs.values())[-1]
    out = await pipeline.run_stage({**job("apply_due", **opts), "brand": "store (2 brands)"}, db=env.db)
    assert out["status"] == "done", env.ledger.runs
    assert sorted(p["brand"] for p in env.applied[-1]["pdps"]) == ["Dr.Jart+", "Skin1004"]


@pytest.mark.parametrize("options", [
    {"vendors": ["A", "B"], "multi_brand": True},                                   # brands required
    {"vendors": ["A", "B"], "multi_brand": True, "brands": {"A": "A"}},             # every vendor mapped
    {"vendors": ["A"], "multi_brand": True, "brands": {"A": "A", "C": "C"}},        # only vendors
    {"vendors": ["A"], "brands": {"A": "A"}},                                       # only with multi_brand
    {"vendors": ["A"], "max_pdp_identity_fetches": 301},                            # a stage must fit a task
    {"vendors": ["ROMAND"], "multi_brand": True, "brands": {"ROMAND": "rom&nd"}},   # not a respelling: ignored
    {"vendors": ["Dr. Jart+"], "multi_brand": True,
     "brands": {"Dr. Jart+": "X", "dr.  jart+": "Dr.Jart+"}},                       # two keys, one vendor
])
def test_multi_brand_spellings_and_fetch_budget_are_validated(options):
    with pytest.raises(ValueError):
        pipeline.validate_options(dict(options))


async def test_the_collections_option_reaches_the_crawl(env, monkeypatch):
    seen = []
    built = feed.records_for_brand

    async def spy(**kw):
        seen.append(kw.get("collection_handles"))
        return await built(**kw)
    monkeypatch.setattr(feed, "records_for_brand", spy)
    await pipeline.run_stage(job(collections=["3ce", "3ce-lip"]), db=env.db)
    await pipeline.run_stage(job(), db=env.db)
    assert seen == [["3ce", "3ce-lip"], None]


@pytest.mark.parametrize("collections", [[], ["../x"], ["Upper"], "3ce"])
def test_bad_collections_are_refused(collections):
    with pytest.raises(ValueError):
        pipeline.validate_options({"vendors": ["3CE"], "collections": collections})


async def test_a_store_past_shopifys_last_page_is_told_to_use_collections(env):
    env.crawl_error = feed.CrawlIncomplete(
        "big.com: page 101: Shopify serves at most 100 pages of /products.json; this listing is larger -- "
        "crawl the brand's collection (options.collections) instead", status="capped", next_page=101,
        scanned_products=25000, selected_products=0)
    out = await pipeline.run_stage(job(), db=env.db)
    assert out["outcome"] == "crawl_capped" and "options.collections" in out["reason"]
    assert "raise options.max_scan_products" not in out["reason"]


async def test_an_ordinary_capped_crawl_is_told_to_raise_its_budget(env):
    env.crawl_error = feed.CrawlIncomplete("k.com: page 81: scan budget 20000 exhausted", status="capped",
                                           next_page=81, scanned_products=20000, selected_products=0)
    out = await pipeline.run_stage(job(), db=env.db)
    assert "raise options.max_scan_products" in out["reason"] and "options.collections" not in out["reason"]


@pytest.mark.parametrize("brands,ok", [
    ({"Lancome": "Lancôme"}, True),                # a measured family, named by its own spelling
    ({"Christian Dior": "Dior"}, True),
    ({"Dior": "Chanel"}, False),                   # a family vendor mapped outside its family: ignored
    ({"Lancome": "Lancome Paris"}, False),
    ({"Chanel": "Dior"}, False),                   # not a respelling
])
def test_a_family_vendor_can_only_be_mapped_within_its_family(brands, ok):
    options = {"vendors": list(brands), "multi_brand": True, "brands": brands}
    if ok:
        pipeline.validate_options(options)
    else:
        with pytest.raises(ValueError, match="ignored"):
            pipeline.validate_options(options)



# --- a row the index refused on content is a note, not a failed store (westman-atelier.com, 2026-09-24) ----

def _rows(blockers, **evidence):
    """One readback row per key; `evidence` overrides the IPS columns on the refused row(s)."""
    async def fetch_all(sql, values):
        out = []
        for i, k in enumerate(values["keys"]):
            b = blockers[i] if i < len(blockers) else None
            row = {"product_key": k, "category_path": "beauty/makeup/lip/tint", "serving": b is None,
                   "pipeline_stage": "public_indexed" if b is None else "extracted", "blocker_code": b or "none",
                   "blocker_detail": "content_quality_score=71.2 < 71.4" if b else None,
                   "lifecycle": "published", "offers": 1, "offers_in_currency": 1, "offers_in_market": 1,
                   "row_priced": True, "row_identity": True, "row_image": True}
            if b:
                row.update(evidence)
            out.append(row)
        return out
    return fetch_all


@pytest.mark.parametrize("blocker", ["low_quality", "no_image", "short_description", "non_core_product"])
async def test_a_row_the_index_refused_on_content_is_noted_and_the_store_still_applies(env, blocker):
    env.rows = [TINT, ("3CE - Velvet Lip Tint Rose 4g", "LIP TINT", "velvet-lip-tint-rose")]
    env.db.fetch_all = _rows([None, blocker])
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert (out["status"], out["outcome"]) == ("done", "applied")
    run = list(env.ledger.runs.values())[-1]
    assert run["readback"]["problems"] == []
    [note] = run["readback"]["notes"]
    assert note["kind"] == "index_refused" and blocker in note["note"]
    assert env.ledger.transitions[-1]["reason"] == "applied and verified; 1 row(s) refused by the index content gate"


@pytest.mark.parametrize("blocker", ["suppressed", "not_live", "no_seed", "no_extraction", "not_scored", "no_price",
                                     "entity_unresolved", "seed_audit_fail", "no_leaf_category", "unknown_future_code", None])
async def test_any_other_reason_a_row_is_not_served_still_fails_the_store(env, blocker):
    async def fetch_all(sql, values):
        return [{"product_key": k, "category_path": "beauty/makeup/lip/tint", "serving": False, "pipeline_stage": None,
                 "blocker_code": blocker, "blocker_detail": None, "lifecycle": "published", "offers": 1,
                 "offers_in_currency": 1, "offers_in_market": 1} for k in values["keys"]]
    env.db.fetch_all = fetch_all
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert (out["status"], out["outcome"]) == ("failed", "readback_failed")
    problem = list(env.ledger.runs.values())[-1]["readback"]["problems"][0]["problem"]
    assert problem.startswith("not serving-eligible")


def test_the_content_refusal_codes_are_codes_the_index_assigns():
    import pathlib, re
    src = (pathlib.Path(__file__).resolve().parents[2] / "services" / "index_pipeline_state_service.py").read_text()
    assigned = set(re.findall(r'blocker_code = "([a-z_]+)"', src))
    assert set(pipeline.INDEX_CONTENT_REFUSALS) <= assigned, set(pipeline.INDEX_CONTENT_REFUSALS) - assigned



@pytest.mark.parametrize("evidence", [{"row_priced": False}, {"row_identity": False}, {"row_image": False}],
                         ids=["no_price_masked", "identity_masked", "planned_image_lost"])
async def test_a_content_refusal_that_may_mask_a_lost_write_still_fails(env, evidence):
    """The index records only the first failed check: low_quality sits ahead of no_price and entity_unresolved,
    and apply overwrites image_url -- so the note path needs the index's own evidence that the write landed."""
    env.rows = [TINT, ("3CE - Velvet Lip Tint Rose 4g", "LIP TINT", "velvet-lip-tint-rose")]  # both carry an image
    env.db.fetch_all = _rows([None, "low_quality"], **evidence)
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert (out["status"], out["outcome"]) == ("failed", "readback_failed")


async def test_a_product_the_store_publishes_without_an_image_is_still_only_a_note(env):
    no_image = ("3CE - Velvet Lip Tint Nude 4g", "LIP TINT", "velvet-lip-tint-nude")
    env.rows = [TINT, no_image]
    real = feed.shopify_product_to_record
    def without_image(product, **kw):
        if product.get("handle") == "velvet-lip-tint-nude":
            product = {**product, "images": []}
        return real(product, **kw)
    import services.curated_brand_feed as cbf
    env.db.fetch_all = _rows([None, "no_image"], row_image=False)
    import unittest.mock as um
    with um.patch.object(cbf, "shopify_product_to_record", side_effect=without_image):
        out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert (out["status"], out["outcome"]) == ("done", "applied")



async def test_the_readback_reads_each_products_own_row_not_the_shared_index_flags(env):
    """A priced, resolved, imaged sibling on the same content_key must not vouch for this product: the query
    computes price, image and identity from p.* per product_key (IPS flags are the best-ranked sibling's)."""
    seen = {}
    async def fetch_all(sql, values):
        seen["sql"], seen["values"] = sql, values
        return [{"product_key": k, "category_path": "beauty/makeup/lip/tint", "serving": True, "pipeline_stage": "public_indexed",
                 "blocker_code": "none", "blocker_detail": None, "lifecycle": "published", "offers": 1,
                 "offers_in_currency": 1, "offers_in_market": 1, "row_priced": True, "row_identity": True, "row_image": True}
                for k in values["keys"]]
    env.db.fetch_all = fetch_all
    await pipeline.run_stage(job("apply_due"), db=env.db)
    sql = " ".join(seen["sql"].split())
    assert "ips.has_price" not in sql and "ips.identity_resolved" not in sql and "ips.has_image" not in sql
    assert "p.product_key" in sql.split("AS row_priced")[0].rsplit("EXISTS", 1)[-1]
    assert "coalesce(p.image_url, '') <> '') AS row_image" in sql
    assert "pgm.platform_product_id = p.source_product_id" in sql
    from services.index_pipeline_state_service import _RESOLVED_PDP_SCOPES
    assert set(seen["values"]["resolved_scopes"]) == set(_RESOLVED_PDP_SCOPES)


# ------------------------------------------------------------------ refile_to_sets (Peng 2026-09-25)
# A bundle of different products on a single-product shelf is RE-FILED to the gift-set shelf, not
# excluded: shoppers look for gift sets, and the harm was the shelf.

def product(title, ptype, handle, price="20.00"):
    return feed.shopify_product_to_record(
        {"id": abs(hash(handle)) % 10**9, "vendor": "3CE", "title": title, "handle": handle,
         "product_type": ptype, "body_html": "<p>A hydrating cream for the face.</p>",
         "images": [{"src": "https://cdn.example/i.jpg"}],
         "variants": [{"id": abs(hash(handle + "v")) % 10**12, "price": price, "available": True, "sku": handle}]},
        domain="k-touch.us", category_path="beauty", brand_override="3CE", currency="USD",
        source_role="retailer", retailer_name="k-touch.us", emit_native_variants=True,
    )


@pytest.fixture
def sets_env(env, monkeypatch):
    env.products = []

    async def fetch(**kw):
        return feed.ShopifyProductBatch(list(env.products), scanned_products=len(env.products), pages=1)
    monkeypatch.setattr(feed, "records_for_brand", fetch)
    return env


def _last_run(env):
    return list(env.ledger.runs.values())[-1]


async def test_a_set_on_a_single_product_shelf_holds_the_store(sets_env):
    sets_env.products = [product("3CE Cream & Hand Cream Duo Gift Set", "Moisturizer", "cream-duo-gift-set")]
    out = await pipeline.run_stage(job(), db=sets_env.db)
    assert out["status"] == "held"
    assert {f["rule"] for f in _last_run(sets_env)["flags"]} == {"set_filed_as_single_product"}


async def test_a_refiled_set_lands_on_the_gift_set_shelf_and_the_store_applies(sets_env):
    sets_env.products = [product("3CE Cream & Hand Cream Duo Gift Set", "Moisturizer", "cream-duo-gift-set"),
                         product("3CE Velvet Cream", "Moisturizer", "velvet-cream")]
    out = await pipeline.run_stage(job("apply_due", refile_to_sets=["Cream-Duo-Gift-Set/"]), db=sets_env.db)
    assert out["status"] == "done" and out["outcome"] == "applied"
    pdps = {p["canonical_url"].rsplit("/", 1)[-1]: p for p in sets_env.applied[0]["pdps"]}
    assert set(pdps) == {"cream-duo-gift-set", "velvet-cream"}  # re-filed, not dropped
    assert pdps["cream-duo-gift-set"]["category_path"] == pipeline.REFILE_SETS_LEAF
    assert pdps["velvet-cream"]["category_path"] != pipeline.REFILE_SETS_LEAF
    run = _last_run(sets_env)
    assert run["checks"]["refiled_to_sets"] == ["cream-duo-gift-set"]
    # The set rule fired where the store filed it; the re-file is what answered it.
    assert run["checks"]["refile_resolved_flags"] == ["set_filed_as_single_product:cream-duo-gift-set"]


async def test_a_refiled_system_whose_title_names_only_its_contents_is_not_a_contradiction(sets_env):
    # Named like La Roche-Posay's "Effaclar 3 Step Acne System" (skintypesolutions.com, 2026-09-24):
    # no set word, so on the gift-set shelf its title names only "treatment" -- one of its contents,
    # which the reviewer already judged. Without the re-file answering that flag it would hold again.
    sets_env.products = [product("3CE Clear Skin 3 Step Acne System", "Moisturizer", "acne-system")]
    out = await pipeline.run_stage(job(), db=sets_env.db)
    assert out["status"] == "held"
    out = await pipeline.run_stage(job("apply_due", refile_to_sets=["acne-system"]), db=sets_env.db)
    assert out["status"] == "done"
    assert sets_env.applied[0]["pdps"][0]["category_path"] == pipeline.REFILE_SETS_LEAF
    assert _last_run(sets_env)["checks"]["refile_resolved_flags"] == ["title_contradicts_category:acne-system"]


async def test_a_refile_answers_only_the_set_flags_not_the_others(sets_env):
    sets_env.products = [product("3CE Luxury Cream Gift Set", "Moisturizer", "lux-set", price="1200.00")]
    out = await pipeline.run_stage(job("apply_due", refile_to_sets=["lux-set"]), db=sets_env.db)
    assert out["status"] == "held"
    assert {f["rule"] for f in _last_run(sets_env)["flags"]} == {"placeholder_product"}


async def test_a_store_that_prices_everything_at_one_dollar_holds_row_by_row(sets_env):
    """headandshoulders.com, 2026-09-26: every variant at 1.00, applied unflagged. It holds now, one flag per
    row, and accepting a row's key releases exactly that row's flag."""
    sets_env.products = [product(f"3CE Velvet Cream {i}", "Moisturizer", f"velvet-cream-{i}", price="1.00")
                         for i in range(25)]
    out = await pipeline.run_stage(job("apply_due"), db=sets_env.db)
    assert out["status"] == "held" and sets_env.applied == []
    flags = _last_run(sets_env)["flags"]
    assert {f["rule"] for f in flags} == {"placeholder_price_store"}
    assert sorted(f["key"] for f in flags) == sorted(f"placeholder_price_store:velvet-cream-{i}" for i in range(25))
    assert "25/25 variants at 1.00" in flags[0]["detail"]
    out = await pipeline.run_stage(job("apply_due", accepted_flags=[f["key"] for f in flags]), db=sets_env.db)
    assert out["status"] == "done"


async def test_a_refiled_subset_is_not_judged_as_a_store_of_its_own(sets_env):
    # The whole cohort is 20 of 120 at 1.00 (0.17): not a placeholder store. The 20 re-filed $1 sets alone
    # would read as one (20/20) if the re-file re-check judged the store again.
    sets_env.products = ([product(f"3CE Velvet Cream {i}", "Moisturizer", f"velvet-cream-{i}", price=f"{20 + i}.00")
                          for i in range(100)]
                         + [product(f"3CE Cream Gift Set {i}", "Moisturizer", f"cream-gift-set-{i}", price="1.00")
                            for i in range(20)])
    out = await pipeline.run_stage(
        job("apply_due", refile_to_sets=[f"cream-gift-set-{i}" for i in range(20)]), db=sets_env.db)
    assert "placeholder_price_store" not in {f["rule"] for f in _last_run(sets_env)["flags"]}
    assert out["status"] == "done", _last_run(sets_env)["flags"]


async def test_a_refile_the_store_no_longer_carries_blocks_until_accepted(sets_env):
    sets_env.products = [product("3CE Velvet Cream", "Moisturizer", "velvet-cream")]
    out = await pipeline.run_stage(job("apply_due", refile_to_sets=["gone-set"]), db=sets_env.db)
    assert out["status"] == "held"
    assert [f["key"] for f in _last_run(sets_env)["flags"]] == ["refile_handle_unmatched:gone-set"]
    out = await pipeline.run_stage(job("apply_due", refile_to_sets=["gone-set"],
                                       accepted_flags=["refile_handle_unmatched:gone-set"]), db=sets_env.db)
    assert out["status"] == "done"


def test_a_handle_cannot_be_both_refiled_and_excluded():
    with pytest.raises(ValueError, match="both re-filed and excluded"):
        pipeline.validate_options({"vendors": ["3CE"], "refile_to_sets": ["Duo-Set"], "exclude_handles": ["duo-set/"]})
    with pytest.raises(ValueError):
        pipeline.validate_options({"vendors": ["3CE"], "refile_to_sets": [""]})
    assert pipeline.validate_options({"vendors": ["3CE"], "refile_to_sets": ["a"], "exclude_handles": ["b"]})


def test_the_refile_shelf_is_a_leaf_both_taxonomies_serve():
    from services.category_path_aliases import resolve
    assert resolve(pipeline.REFILE_SETS_LEAF) == pipeline.REFILE_SETS_LEAF


async def test_a_refile_answers_its_own_rows_not_another_rows_set_flag(sets_env):
    # Review of #2316: clearing by rule alone would let one re-file wave through every set in the store.
    sets_env.products = [product("3CE Cream & Hand Cream Duo Gift Set", "Moisturizer", "cream-duo-gift-set"),
                         product("3CE Cleansing Oil Set", "Cleanser", "oil-set")]
    out = await pipeline.run_stage(job("apply_due", refile_to_sets=["cream-duo-gift-set"]), db=sets_env.db)
    assert out["status"] == "held"
    assert [f["key"] for f in _last_run(sets_env)["flags"]] == ["set_filed_as_single_product:oil-set"]


async def test_a_refile_does_not_hide_the_rules_keyed_on_the_stores_shelf(env):
    # Review of #2316: on the gift-set shelf the lip rules cannot fire, so they run on the row as the
    # store filed it. A 40 ml "lip tint" that is a complexion cream still holds after a re-file.
    env.rows = [TINT, TONE_UP]
    out = await pipeline.run_stage(job("apply_due", refile_to_sets=["3ce-tone-up-tint-40ml"]), db=env.db)
    assert out["status"] == "held"
    rules = {f["rule"] for f in list(env.ledger.runs.values())[-1]["flags"]}
    assert rules & {"lip_row_implausible_size", "lip_row_copy_not_about_lips"}


async def test_a_lip_pass_keeps_a_refiled_lip_set_instead_of_dropping_it(env):
    # Review of #2316: an only_category=beauty/makeup/lip cohort filtered the re-filed row out as
    # "outside the filter" while still reporting it re-filed -- a re-file turned into an exclusion.
    env.rows = [TINT, ("3CE - Velvet Lip Tint Duo Set", "LIP TINT", "lip-duo-set")]
    out = await pipeline.run_stage(job("apply_due", only_category="beauty/makeup/lip", refile_to_sets=["lip-duo-set"]),
                                   db=env.db)
    assert out["status"] == "done"
    paths = {p["canonical_url"].rsplit("/", 1)[-1]: p["category_path"] for p in env.applied[0]["pdps"]}
    assert paths["lip-duo-set"] == pipeline.REFILE_SETS_LEAF and "velvet-lip-tint-plush" in paths
    assert list(env.ledger.runs.values())[-1]["checks"]["refiled_kept_outside_filter"] == ["lip-duo-set"]
