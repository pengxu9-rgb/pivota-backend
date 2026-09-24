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


@pytest.mark.parametrize("lifecycle,ok", [("candidate", False), ("draft", False), (None, False),
                                          ("validated", True), ("published", True)])
async def test_a_row_search_cannot_see_fails_the_readback(env, lifecycle, ok):
    """Serving-eligible is not searchable: recall reads only validated/published (pivot_query_service)."""
    async def fetch_all(sql, values):
        return [{"product_key": k, "category_path": "beauty/skincare/moisturize/cream", "serving": True,
                 "pipeline_stage": "shadow_indexed", "lifecycle": lifecycle, "offers": 2, "offers_in_currency": 2}
                for k in values["keys"]]
    env.db.fetch_all = fetch_all
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert (out["status"], out["outcome"]) == (("done", "applied") if ok else ("failed", "readback_failed"))
    if not ok:
        run = list(env.ledger.runs.values())[-1]
        assert run["readback"]["problems"][0]["problem"] == f"not searchable: pdp_lifecycle_stage {lifecycle!r}"


def test_the_searchable_stages_are_the_ones_recall_filters_on():
    import pathlib, re
    src = pathlib.Path("services/pivot_query_service.py").read_text()
    filters = set(re.findall(r"pdp_lifecycle_stage IN \(([^)]*)\)", src))
    assert filters and all(set(re.findall(r"'(\w+)'", f)) == set(pipeline.SEARCHABLE_LIFECYCLE_STAGES) for f in filters)


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
