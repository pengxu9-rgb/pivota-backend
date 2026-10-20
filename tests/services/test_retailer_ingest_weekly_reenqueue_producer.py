"""The re-enqueue's market and ships-to rules against what the REAL drain stage writes.

Every run here comes from pipeline.run_stage (dry run, then apply) over the existing SG-lane fixture: the
real _check writes checks.storefront (market, ships_to_market), the real readback writes readback.rows, and
the crawl's presented currency comes from the real _PresentedCurrency over Set-Cookie headers. The stored
shape is the one ledger.finish_run writes (ledger._dumps, then read back), so nothing here hand-builds the
checks JSON the rules read.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from db import retailer_ingest as ledger
from services import curated_brand_feed as feed
from services.retailer_ingest import pipeline, weekly_reenqueue as rq

from tests.services.test_retailer_ingest_markets_phase2 import gates_on  # noqa: F401 -- fixture
from tests.services.test_retailer_ingest_markets_sg import (  # noqa: F401 -- fixtures
    HOST, USD_META, _both, _clean_meta_cache, base_job, sg_lane, usd_store)
from tests.services.test_retailer_ingest_pipeline import env  # noqa: F401 -- fixture

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
READ_AT = NOW - timedelta(days=10)


def presented(*cookies):
    """The crawl's presented-currency report, from one /products.json response per Set-Cookie value."""
    seen = feed._PresentedCurrency()
    for cookie in cookies:
        headers = [("set-cookie", f"cart_currency={cookie}; path=/")] if cookie else []
        seen.note(httpx.Response(200, headers=headers))
    return seen.report()


def use_crawl(monkeypatch, state, report):
    """The fixture's crawl, plus the presented-currency evidence a real crawl records."""
    async def crawl(**kw):
        batch = feed.ShopifyProductBatch(state.records(), scanned_products=3, pages=1, presented=report)
        batch.crawl_report["storefront"] = {k: state.store.meta.get(k) for k in
                                            ("name", "myshopify_domain", "currency", "ships_to_countries")}
        return batch
    monkeypatch.setattr(feed, "records_for_brand", crawl)


def stored(run):
    """A run as ledger.finish_run stores it and the database hands it back."""
    return {k: json.loads(ledger._dumps(run.get(k))) for k in ("crawl", "checks", "readback")}


def no_market(job):
    job["options"].pop("market")
    return job


async def produce(env, catalog, *, market=None, **options):
    """A done job through the real dry run + apply; returns (the job as the ledger holds it, its applied run)."""
    def make(status="queued"):
        job = base_job(status, **options)
        return no_market(job) if market is None else {**job, "options": {**job["options"], "market": market}}
    await _both(env, catalog, make)
    (applied,) = [r for r in env.ledger.runs.values() if r.get("stage") == "apply" and r.get("outcome") == "applied"][-1:]
    job = {**make(), "id": "rij_done", "status": "done", "created_at": READ_AT - timedelta(hours=1),
           "last_applied_at": READ_AT, "lease_until": None}
    return job, stored(applied)


def plan(job, run, *, retired=False):
    key = rq.cohort_key(job)
    facts = {job["id"]: {"evidence": rq.evidence_from_run(run), "retired": retired}}
    return rq.plan_cohort(key, [job], now=NOW, excluded=set(), facts=facts), key


async def test_a_no_market_job_is_checked_as_the_default_market_so_evidence_can_only_say_us(sg_lane, monkeypatch):
    env, catalog, applied, state = sg_lane
    use_crawl(monkeypatch, state, presented("USD", "USD"))
    job, run = await produce(env, catalog)
    # What the drain itself recorded for a job with no options.market: the DEFAULT market, not a declaration.
    assert run["checks"]["storefront"]["market"] == pipeline.DEFAULT_MARKET == "US"
    assert run["checks"]["storefront"]["ships_to_market"] is True
    decision, key = plan(job, run)
    assert (decision["decision"], decision["market"], decision["market_source"]) == ("enqueue", "US", "evidence")
    assert decision["options"]["market"] == "US" and decision["options"]["require_ships_to_market"] is True
    assert ledger.scope_key(HOST, job["brand"], decision["options"]) == key  # neither addition is scope


@pytest.mark.parametrize("ships, cookies, reason, detail", [
    (["CA", "SG"], ("USD",), rq.SHIPS_TO_MARKET_FALSE, None),            # a USD store that does not ship to the US
    (None, ("USD",), rq.MARKET_UNPROVEN, rq.SHIPS_TO_MARKET_UNPROVEN),   # the store does not say where it ships
    (["US"], (None, None), rq.MARKET_UNPROVEN, rq.PRESENTED_CURRENCY_ABSENT),
    (["US"], ("USD", "QAR"), rq.MARKET_UNPROVEN, rq.PRESENTED_CURRENCY_MISMATCH),
])
async def test_evidence_the_producer_writes_that_does_not_show_a_us_seller_is_never_queued(
        sg_lane, monkeypatch, ships, cookies, reason, detail):
    env, catalog, applied, state = sg_lane
    state.store = usd_store(meta={**USD_META, "ships_to_countries": ships})
    # (records_for_brand itself refuses a crawl presented in a foreign currency; the stand-in crawl records the
    # report as given, so the rule is judged on a stored report that holds one.)
    use_crawl(monkeypatch, state, presented(*cookies))
    job, run = await produce(env, catalog)
    decision, _ = plan(job, run)
    assert decision["decision"] == "skip" and (decision["reason"], decision.get("detail")) == (reason, detail)
    assert "market" not in decision


async def test_an_explicit_market_whose_own_run_says_the_store_does_not_ship_there_is_skipped(sg_lane, monkeypatch):
    env, catalog, applied, state = sg_lane
    state.store = usd_store(meta={**USD_META, "ships_to_countries": ["CA", "SG"]})
    use_crawl(monkeypatch, state, presented("USD"))
    job, run = await produce(env, catalog, market="US")
    assert run["checks"]["storefront"]["ships_to_market"] is False
    decision, _ = plan(job, run)
    assert (decision["decision"], decision["reason"]) == ("skip", rq.SHIPS_TO_MARKET_FALSE)


@pytest.mark.parametrize("ships_now, outcome, status", [
    (["CA", "SG"], "store_does_not_ship_to_market", "nothing"),
    (None, "ships_to_unverifiable", "failed"),
])
async def test_an_explicit_market_with_ships_unknown_is_queued_gated_and_the_drain_enforces_the_gate(
        sg_lane, monkeypatch, ships_now, outcome, status):
    env, catalog, applied, state = sg_lane
    state.store = usd_store(meta={**USD_META, "ships_to_countries": None})  # the run cannot say
    use_crawl(monkeypatch, state, presented("USD"))
    job, run = await produce(env, catalog, market="US")
    assert run["checks"]["storefront"]["ships_to_market"] is None
    decision, key = plan(job, run)
    assert (decision["decision"], decision["market_source"]) == ("enqueue", "explicit")
    assert decision["options"]["require_ships_to_market"] is True
    assert ledger.scope_key(HOST, job["brand"], decision["options"]) == key
    # The queued job, run by the real drain stage against a store that does not (or will not say it does) ship
    # to the US: refused before anything is written.
    state.store = usd_store(meta={**USD_META, "ships_to_countries": ships_now})
    before = len(applied)
    out = await pipeline.run_stage({"id": "rij_requeued", "domain": decision["domain"], "brand": decision["brand"],
                                    "status": "queued", "attempts": 0, "max_attempts": 6,
                                    "options": decision["options"]}, db=catalog)
    assert (out["status"], out["outcome"]) == (status, outcome)
    assert len(applied) == before


async def test_a_queued_evidence_job_runs_clean_through_the_drain_while_the_store_ships(sg_lane, monkeypatch):
    env, catalog, applied, state = sg_lane
    use_crawl(monkeypatch, state, presented("USD"))
    job, run = await produce(env, catalog)
    decision, _ = plan(job, run)
    out = await pipeline.run_stage({"id": "rij_requeued", "domain": decision["domain"], "brand": decision["brand"],
                                    "status": "queued", "attempts": 0, "max_attempts": 6,
                                    "options": decision["options"]}, db=catalog)
    assert out["status"] == "apply_due"
    requeued = [r for r in env.ledger.runs.values() if r.get("job_id") == "rij_requeued"][-1]
    assert requeued["checks"]["storefront"]["market"] == "US"


async def test_a_store_whose_every_read_back_product_is_retired_is_not_requeued(sg_lane, monkeypatch):
    env, catalog, applied, state = sg_lane
    use_crawl(monkeypatch, state, presented("USD"))
    job, run = await produce(env, catalog, market="US")
    keys = rq._readback_keys(run)
    assert keys  # the real readback names the products it read
    retired = {k: {"suppressed_at": NOW, "suppression_reason": "hand_retired_example", "has_active_seed": False,
                   "has_live_offer": False} for k in keys}
    assert rq.host_retired(keys, retired)
    assert plan(job, run, retired=True)[0]["reason"] == rq.RETIRED_HOST
    one_live = {**retired, keys[0]: {**retired[keys[0]], "has_live_offer": True}}
    assert not rq.host_retired(keys, one_live)
