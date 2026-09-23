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

    async def start_run(self, *, job_id, stage, image_sha, execution, db=None):
        run_id = f"run{len(self.runs)}"
        self.runs[run_id] = {"job_id": job_id, "stage": stage}
        return run_id

    async def finish_run(self, run_id, **fields):
        fields.pop("db", None)
        self.runs[run_id].update(fields)

    async def transition(self, job_id, **fields):
        fields.pop("db", None)
        self.transitions.append(fields)

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
                     "pipeline_stage": "public_indexed", "offers": 1, "offers_in_currency": 1}
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
    assert out["status"] == "apply_due"
    assert feed._LIP_TITLE_EVIDENCE.get() is False  # scoped to the stage


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
