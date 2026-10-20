"""The census script's pure half, in the regular (SQLite) sweep: its copies of
the rule are pinned to the originals, it is safe to pass inline to
scripts/ops/run_oneoff_job.sh, and its projection arithmetic. Its SQL runs on
Postgres in tests/test_crawl_fresh_trust_postgres.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import census_retailer_crawl_fresh_trust as census

_REPO = Path(__file__).resolve().parent.parent
DRAIN = "catalog_enrichment_agent_v1"
RETAILER = "ext:retailer:0123456789abcdef0123456789abcdef"
BRAND = "ext:serum-slug::0123abcd"
OTHER = "prod::external_seed::merch_obs_x::ext_1"
NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def test_census_copies_are_pinned_to_their_originals():
    from services import catalog_row_trust_upserter as up
    from services import catalog_trust_policy as policy

    assert census.OFFER_LINK_SQL == up.CRAWL_OFFER_LINK_SQL
    assert census.OFFER_NOT_AFFILIATE_SQL == up.CRAWL_OFFER_NOT_AFFILIATE_SQL
    assert census.CRAWL_LISTING_KEY_PREFIX == policy.CRAWL_LISTING_KEY_PREFIX
    assert census.CRAWL_SOURCE_SYSTEM == policy.CRAWL_SOURCE_SYSTEM
    assert census.DEFAULT_DAYS == policy.CRAWL_FRESH_DEFAULT_DAYS
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    for hours in (-1, 0, 1, 23, 24, 25, 72, 167, 168, 169, 400):
        for days in (0.5, 1, 3, 7, 14):
            ts = now - timedelta(hours=hours)
            assert census.is_fresh(ts, now=now, max_age_days=days) == policy.is_crawl_fresh(
                ts, now=now, max_age_days=days)
    assert census.is_fresh(None, now=now, max_age_days=7) is policy.is_crawl_fresh(None, now=now, max_age_days=7)
    for pk, ss in ((RETAILER, DRAIN), (BRAND, DRAIN), (OTHER, DRAIN), (RETAILER, "x"), ("ext", DRAIN)):
        assert census.is_crawl_listing(pk, ss) == policy.is_crawl_listing(
            {"product_key": pk, "source_system": ss})


def test_census_file_is_inline_safe():
    body = (_REPO / "scripts/census_retailer_crawl_fresh_trust.py").read_text()
    assert "@" not in body
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ", "TRUNCATE "):
        assert verb not in body.upper()


def _row(age_days, *, decision="shadow", reasons=(), renderable=True, markets=("US",), lane="retailer",
         identity_status="unknown", market_ages=None, host="shop.example.com"):
    read = None if age_days is None else NOW - timedelta(days=age_days)
    reads = {m: read for m in markets}
    for m, age in (market_ages or {}).items():
        reads[m] = NOW - timedelta(days=age)
    return census.classify(
        {"product_key": f"k{age_days}{decision}", "lane": lane, "host": host,
         "serving_decision": decision, "reason_codes": list(reasons), "identity_status": identity_status,
         "renderable": renderable, "last_price_read_at": read,
         "market_reads": reads},
        now=NOW, max_age_days=7)


def test_classify_only_coverage_shadow_renderable_fresh_rows_flip():
    assert _row(1)["would_flip"] is True
    assert _row(8)["would_flip"] is False
    assert _row(None)["would_flip"] is False
    assert _row(1, decision="public")["would_flip"] is False
    assert _row(1, reasons=["NON_CANONICAL_DUPLICATE"])["would_flip"] is False
    assert _row(1, reasons=["IDENTITY_REVIEW_REQUIRED_LIVE_READ"])["blocking"] == ["review_required"]
    assert _row(1, renderable=False)["fresh_not_renderable"] is True
    assert _row(8, markets=())["candidate"] is False
    assert _row(8)["candidate"] is True
    # review_required identity with no stored reason code still blocks
    assert _row(1, identity_status="review_required")["blocking"] == ["review_required"]
    assert _row(1, identity_status="review_required")["would_flip"] is False


def test_age_buckets_are_inclusive():
    assert _row(1)["bucket"] == "le_1d"
    assert _row(3)["bucket"] == "le_3d"
    assert _row(7)["bucket"] == "le_7d"
    assert _row(14)["bucket"] == "le_14d"
    assert _row(14.01)["bucket"] == "gt_14d"
    assert _row(None)["bucket"] == "null"


def test_projection_ages_out_on_the_inclusive_bound():
    out = census.project([_row(4)], max_age_days=7, nightly_probability=0.0)
    assert out["no_reread"]["by_day"]["3"] == 1 and out["no_reread"]["by_day"]["4"] == 0


def test_weekly_drain_with_a_window_under_seven_days():
    out = census.project([_row(20)], max_age_days=3, nightly_probability=0.0)
    assert out["drain_weekly"]["by_day"]["5"] == pytest.approx(3 / 7, abs=0.05)
    assert out["drain_weekly"]["steady_state"] == pytest.approx(3 / 7, abs=0.05)


def test_markets_and_hosts_count_only_fresh_evidence():
    rows = [
        # flips on its fresh US read; its GB read is stale
        _row(1, market_ages={"GB": 10}),
        # a stale non-canonical duplicate on the same host
        _row(10, reasons=["NON_CANONICAL_DUPLICATE"]),
    ]
    lines = census.summarize(rows, now=NOW, max_age_days=7, seed_pool_by_market={"US": 10})
    markets = {(line["lane"], line["market"]): line for line in lines if line["kind"] == "by_market"}
    assert markets[("all", "US")]["would_flip"] == 1
    assert markets[("all", "GB")].get("would_flip", 0) == 0
    assert markets[("all", "GB")]["rows"] == 1
    host = [line for line in lines if line["kind"] == "by_host"][0]
    assert host["fresh_still_blocked_by_reason"] == 0
    assert host["would_flip"] == 1


def test_projection_arithmetic():
    rows = [_row(0.5), _row(3.5), _row(None), _row(20)]
    out = census.project(rows, max_age_days=7, nightly_probability=0.1)
    assert out["no_reread"]["by_day"] == {"0": 2, "1": 2, "2": 2, "3": 2, "4": 1, "5": 1, "6": 1, "7": 0}
    # pro rata: the two stale candidates each get 1 - 0.9^d by day d (d <= 7 < N + 1 nights)
    refresh = out["refresh_pro_rata"]["by_day"]
    assert refresh["0"] == 2
    assert refresh["7"] == pytest.approx(2 * (1 - 0.9 ** 7) + 2 * (1 - 0.9 ** 7), abs=0.1)
    assert out["refresh_pro_rata"]["steady_state_round_robin"] == pytest.approx(4 * 0.7)
    assert out["refresh_pro_rata"]["steady_state_pro_rata"] == pytest.approx(4 * (1 - 0.9 ** 8), abs=0.1)
    weekly = out["drain_weekly"]
    assert weekly["by_day"]["7"] == 4 and weekly["steady_state"] == 4
    assert weekly["by_day"]["0"] == 2
    # monotone non-increasing without re-reads
    days = [out["no_reread"]["by_day"][str(d)] for d in range(8)]
    assert days == sorted(days, reverse=True)


def test_projection_with_no_pool_is_no_reread():
    rows = [_row(0.5), _row(20)]
    out = census.project(rows, max_age_days=7, nightly_probability=0.0)
    assert out["refresh_pro_rata"]["by_day"] == out["no_reread"]["by_day"]


def test_summarize_splits_lanes_and_uses_the_us_pool():
    rows = [_row(1), _row(1, lane="brand_official"), _row(9, lane="brand_official")]
    lines = census.summarize(rows, now=NOW, max_age_days=7, refresh_per_night=100,
                             seed_pool_by_market={"US": 1000, "SG": 50})
    params = lines[0]
    assert params["seed_pool"] == 1000 and params["nightly_reread_probability"] == 0.1
    summaries = {line["lane"]: line for line in lines if line["kind"] == "summary"}
    assert summaries["all"]["would_flip"] == 2
    assert summaries["retailer"]["would_flip"] == 1
    assert summaries["brand_official"]["would_flip"] == 1
    assert summaries["brand_official"]["candidates_any_age"] == 2
    override = census.summarize(rows, now=NOW, max_age_days=7, refresh_per_night=100, seed_pool=400,
                                seed_pool_by_market={"US": 1000})
    assert override[0]["seed_pool"] == 400 and override[0]["seed_pool_source"] == "--seed-pool"
