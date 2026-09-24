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


class Ledger:
    def __init__(self):
        self.runs, self.transitions = {}, []
        self.unfinished = None      # the previous run a killed execution left behind
        self.current_status = None  # set to simulate an operator changing the job mid-stage

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
    state = SimpleNamespace(ledger=ledger, rows=[TINT], crawl_error=None, applied=[], readback_rows=None)

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
                     "offers_in_currency": 1}
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
                 "pipeline_stage": "shadow_indexed", "lifecycle": lifecycle, "offers": 2, "offers_in_currency": 2}
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


async def test_an_interrupted_dry_run_spends_an_attempt_and_backs_off(env):
    env.ledger.unfinished = {"id": "run_killed", "stage": "dry_run"}
    out = await pipeline.run_stage(job(), db=env.db)
    t = env.ledger.transitions[-1]
    assert out["outcome"] == "interrupted" and t["status"] == "queued" and t["count_attempt"]
    assert t["next_run_at"] is not None


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
                 "pipeline_stage": "shadow_indexed", "lifecycle": "candidate", "offers": 1, "offers_in_currency": 1}
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
    opts = {"multi_brand": True, "vendors": ["3CE", "rom&nd"]}
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
