"""offer_payload.declared_market: the drain stamps the buyer market a job DECLARED, never a default.

catalog_offers.market is NOT NULL DEFAULT 'US' (mig 149) and pipeline.job_market() falls back to
DEFAULT_MARKET, so neither tells a declared market from a defaulted one. CRAWL_FRESH
(services/catalog_row_trust_upserter) counts a read only on an offer whose declared market equals its
market column; this is the only writer of the key. The real-producer path (ingestion offer rows ->
this stamp -> apply_ingest_plan) runs on Postgres in tests/test_crawl_fresh_real_producers_postgres.py.
"""

from __future__ import annotations

import json

from services.retailer_ingest import pipeline


def _plan(*offers):
    return {"offers": [dict(o) for o in offers]}


def _offer(market, payload=None, *, as_text=True):
    body = payload if payload is not None else {"destination_url": "https://shop.example.com/p/1"}
    return {"offer_id": f"o_{market}", "market": market, "offer_payload": json.dumps(body) if as_text else dict(body)}


def _payload(offer):
    raw = offer["offer_payload"]
    return json.loads(raw) if isinstance(raw, str) else raw


def test_an_explicit_market_stamps_its_offers():
    plan = _plan(_offer("US"), _offer("US", as_text=False))
    assert pipeline.stamp_declared_market(plan, {"market": "US"}) == 2
    assert [_payload(o)["declared_market"] for o in plan["offers"]] == ["US", "US"]
    assert isinstance(plan["offers"][0]["offer_payload"], str)
    assert isinstance(plan["offers"][1]["offer_payload"], dict)
    assert _payload(plan["offers"][0])["destination_url"] == "https://shop.example.com/p/1"


def test_the_default_market_never_stamps():
    for options in (None, {}, {"market": None}, {"market": ""}):
        plan = _plan(_offer("US"))
        assert pipeline.stamp_declared_market(plan, options) == 0
        assert "declared_market" not in _payload(plan["offers"][0])
    assert pipeline.job_market({}) == "US"  # the fallback the stamp refuses to treat as a declaration


def test_only_offers_written_for_that_market_are_stamped():
    plan = _plan(_offer("SG"), _offer("US"), {"offer_id": "o_none", "offer_payload": "{}"})
    assert pipeline.stamp_declared_market(plan, {"market": "sg"}) == 1
    assert _payload(plan["offers"][0])["declared_market"] == "SG"
    assert "declared_market" not in _payload(plan["offers"][1])
    assert plan["offers"][2]["offer_payload"] == "{}"


def test_a_payload_that_is_not_an_object_is_left_alone():
    plan = _plan({"offer_id": "o", "market": "US", "offer_payload": "[1, 2]"})
    assert pipeline.stamp_declared_market(plan, {"market": "US"}) == 0
    assert plan["offers"][0]["offer_payload"] == "[1, 2]"


def test_the_check_stamps_after_every_check_and_reports_the_count():
    import inspect

    source = inspect.getsource(pipeline._check)
    stamp = source.index("stamp_declared_market(plan, o)")
    assert stamp > source.index("_brand_host_guard_report(plan, check=True)")
    assert 'checks["declared_market_offers"]' in source
