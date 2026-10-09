"""SG is a SERVED ingest market (Peng 2026-09-29): "SG is legit serving region as we have crawled and ingested
based on Meitu's merchant list".

Unlike AU/JP (acquisition: stored, never served), an SG job's rows must read back SERVING-ELIGIBLE -- priced in
SGD for the SG region PIVOTA_SERVING_PRICING_REGIONS already serves on web. What changes and what does not:
  1. options: market SG is accepted, its currency is SGD, and nothing else is.
  2. the writer: every offer is stamped catalog_offers.market 'SG' in SGD; the seed stays in the "US" partition
     (ingestion.SEED_PARTITION_MARKET), where SG buyers already find SGD seeds by currency (#2389).
  3. serving: an SGD offer is priced for SG when the process serves SG; a drain that serves only US would read
     every SG row back unserved -- so an SG job refuses to run there (served_market_unconfigured), before any
     crawl, and the drain's env (infra/gcp/setup_scheduler.sh) carries US,SG.
  4. the readback: a served market -- an unserved SG row is a problem, never an acquisition note.
"""
from __future__ import annotations

import pytest

import services.index_pipeline_state_service as ips
from services.catalog_enrichment_agent.ingestion import SEED_PARTITION_MARKET, ingest_validated_jsonl
from services.catalog_onboard_worker import normalize_curated_brand_payload
from services.region_pricing import has_offer_priced_for_any_region_sql
from services.retailer_ingest import pipeline
from tests.services.test_retailer_ingest_markets_phase2 import _RB, _priced_for, _sqlite_offers, goto_records
from tests.services.test_retailer_ingest_pipeline import env, job  # noqa: F401 -- the state-machine fixture

HOST = "cocomo.sg"


def sg_job(status="queued", **options):
    return job(status, market="SG", require_currency="SGD", **options)


# ================================================================== 1. options

@pytest.mark.parametrize("given", ["SG", "sg", " Sg "])
def test_sg_is_an_ingest_market_priced_in_sgd(given):
    o = pipeline.validate_options({"vendors": ["X"], "market": given})
    assert pipeline.job_market(o) == "SG" and pipeline.job_currency(o) == "SGD"
    assert pipeline.validate_options({"vendors": ["X"], "market": "SG", "require_currency": "SGD"})


@pytest.mark.parametrize("wrong", ["USD", "AUD", "sgd "])
def test_an_sg_job_cannot_claim_another_currency(wrong):
    with pytest.raises(ValueError, match="is not market SG's currency SGD"):
        pipeline.validate_options({"vendors": ["X"], "market": "SG", "require_currency": wrong})


def test_sg_is_served_not_acquired():
    assert "SG" in pipeline.INGEST_MARKETS and "SG" not in pipeline.ACQUISITION_MARKETS
    assert pipeline.SERVED_INGEST_MARKETS == ("US", "SG")


def test_the_lane_payload_carries_sg_and_sgd():
    payload = pipeline.ingest_payload(sg_job())
    assert (payload["market"], payload["require_currency"]) == ("SG", "SGD")


def test_the_onboard_queue_still_refuses_sg():
    """Its drain stamps no market: an SG row there would be SGD stamped 'US' (how jsmbeauty.sg was written)."""
    with pytest.raises(ValueError, match="market US only"):
        normalize_curated_brand_payload({"domain": HOST, "brand": "X", "market": "SG"})
    assert normalize_curated_brand_payload({"domain": HOST, "brand": "X", "market": "SG"},
                                           markets=pipeline.INGEST_MARKETS)["market"] == "SG"


# ================================================================== 2. writer

def test_an_sg_plan_stamps_every_offer_sg_in_sgd_and_keeps_its_seeds_in_the_us_partition():
    plan = ingest_validated_jsonl(goto_records("SGD"), market="SG")
    assert plan["offers"] and {(o["market"], o["currency"]) for o in plan["offers"]} == {("SG", "SGD")}
    assert SEED_PARTITION_MARKET == "US"
    assert {(s["market"], s["price_currency"]) for s in plan["seeds"]} == {("US", "SGD")}


def test_an_sg_plan_refuses_a_record_priced_in_usd():
    with pytest.raises(ValueError):
        ingest_validated_jsonl(goto_records("USD"), market="SG")


# ================================================================== 3. serving: only where SG is served

@pytest.mark.parametrize("regions,priced", [(["US", "SG"], True), (["SG"], True), (["US"], False)])
def test_an_sgd_offer_is_priced_for_sg_only_where_sg_is_served(regions, priced):
    con = _sqlite_offers([{"offer_id": "o1", "product_key": "p1", "currency": "SGD", "market": "SG",
                           "list_price": 45.0}])
    assert _priced_for(con, "p1", regions) is priced
    assert "SGD" in has_offer_priced_for_any_region_sql("cp.product_key", ["US", "SG"])


async def test_an_sg_job_refuses_to_run_where_sg_is_not_served(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ips, "serving_pricing_regions", lambda: ["US"])
    env.crawl_error = AssertionError("must not crawl")
    out = await pipeline.run_stage(sg_job(), db=env.db)
    assert (out["status"], out["outcome"]) == ("failed", "served_market_unconfigured")
    assert "PIVOTA_SERVING_PRICING_REGIONS" in out["reason"] and "['US']" in out["reason"]


async def test_an_sg_job_runs_where_sg_is_served(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ips, "serving_pricing_regions", lambda: ["US", "SG"])
    env.crawl_error = RuntimeError("reached the crawl")
    with pytest.raises(RuntimeError, match="reached the crawl"):
        await pipeline.run_stage(sg_job(), db=env.db)


async def test_an_sg_apply_is_refused_before_its_recrawl_too(env, monkeypatch):  # noqa: F811
    """The guard is not a dry-run check: an SG job that reached apply_due on a drain later re-imaged without SG
    must not write rows that read back unserved (review of #2442: a dry-run-only mutant survived)."""
    monkeypatch.setattr(ips, "serving_pricing_regions", lambda: ["US"])
    env.crawl_error = AssertionError("must not crawl")
    out = await pipeline.run_stage(sg_job("apply_due"), db=env.db)
    assert (out["status"], out["outcome"]) == ("failed", "served_market_unconfigured")
    assert env.applied == []


@pytest.mark.parametrize("market", ["US", "AU", "JP"])
async def test_the_served_market_guard_never_touches_us_or_acquisition_jobs(env, monkeypatch, market):  # noqa: F811
    monkeypatch.setattr(ips, "serving_pricing_regions", lambda: ["US"])
    monkeypatch.setattr("services.agent_decision_gates.agent_decision_gates_enabled", lambda: True)
    pipeline._require_served_market_is_served(market)  # no raise


# ================================================================== 4. readback: a served market

async def test_an_unserved_sg_row_is_a_problem_not_an_acquisition_note():
    out = await pipeline._readback(["p1"], "SGD", _RB(), market="SG", domain=HOST)
    assert out["problems"] == [{"product_key": "p1", "problem": "not serving-eligible (blocker no_us_offer)"}]
    assert "acquisition_not_served" not in [n["kind"] for n in out["notes"]]


async def test_a_served_sg_row_reads_back_clean_and_counts_sg_offers_in_sgd():
    db = _RB(serving=True, blocker_code="none")
    out = await pipeline._readback(["p1"], "SGD", db, market="SG", domain=HOST)
    assert out["ok"], out["problems"]
    assert (db.values["currency"], db.values["market"]) == ("SGD", "SG")
    assert "content_served_region_priced" not in db.sql  # the leak detector is for acquisition rows only


async def test_an_sg_row_whose_offers_kept_the_us_stamp_fails():
    """jsmbeauty.sg was written market 'US' with SGD; the column is INSERT-only, so a re-crawl under SG keeps
    'US' -- and the readback must say so, not pass it."""
    out = await pipeline._readback(["p1"], "SGD", _RB(serving=True, blocker_code="none", offers_in_market=0),
                                   market="SG", domain=HOST)
    assert not out["ok"] and "stamped market SG: 0" in out["problems"][0]["problem"]
