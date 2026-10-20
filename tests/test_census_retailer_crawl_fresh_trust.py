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
            assert census.is_fresh_strict(ts, now=now, max_age_days=days) == policy.is_crawl_fresh(
                ts, now=now, max_age_days=days)
    assert census.is_fresh_strict(None, now=now, max_age_days=7) is policy.is_crawl_fresh(
        None, now=now, max_age_days=7)
    # round 2's per-market fragments
    assert census.SERVED_MARKET_CURRENCY_SQL == up.CRAWL_OFFER_SERVED_MARKET_SQL
    assert census.IN_STOCK_SQL == up.CRAWL_OFFER_IN_STOCK_SQL
    assert census.NOT_EMPLOYEE_EDIT_SQL == up.CRAWL_OFFER_NOT_EMPLOYEE_EDIT_SQL
    assert census.DECLARED_MARKET_SQL == up.CRAWL_OFFER_DECLARED_MARKET_SQL
    assert census.VALID_READ_SQL == up.CRAWL_OFFER_VALID_READ_SQL.replace("CAST(:now AS timestamptz)", "now()")
    # round 4: the class is every live offer, the source moved into the valid read
    assert census.DRAIN_SOURCE_SQL == up.CRAWL_OFFER_DRAIN_SOURCE_SQL
    assert census.CLASS_SQL == "\n           AND ".join(up.crawl_offer_class_conjuncts("k")[1:])
    assert census.VALID_READ_ANY_MARKET_SQL == census.VALID_READ_SQL.replace(
        f" AND {census.DECLARED_MARKET_SQL}", "")
    assert census.DECLARED_MARKET_SQL not in census.VALID_READ_ANY_MARKET_SQL
    assert census.SERVED_MARKET_CURRENCY_PAIRS == up.CRAWL_SERVED_MARKET_CURRENCY_PAIRS
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


def test_the_per_market_rule_treats_a_future_stamp_as_stale():
    assert census.is_fresh_strict(NOW + timedelta(minutes=1), now=NOW, max_age_days=7) is False
    assert census.is_fresh_strict(NOW, now=NOW, max_age_days=7) is True
    assert census.is_fresh_strict(NOW - timedelta(days=7), now=NOW, max_age_days=7) is True
    assert census.is_fresh_strict(NOW - timedelta(days=7, seconds=1), now=NOW, max_age_days=7) is False
    assert census.is_fresh_strict(None, now=NOW, max_age_days=7) is False


def test_the_max_any_columns_keep_the_round_one_clock():
    """The max-any columns report the round-1 rule: inclusive bound, and a
    future stamp counted fresh (which is what round 2 corrected)."""
    assert census.is_fresh(NOW - timedelta(days=7), now=NOW, max_age_days=7) is True
    assert census.is_fresh(NOW - timedelta(days=7, seconds=1), now=NOW, max_age_days=7) is False
    assert census.is_fresh(NOW + timedelta(days=3), now=NOW, max_age_days=7) is True


def _cls(*, n=1, valid=True, valid_any=True, stock=True, oldest=None, oldest_any=None, newest_any=None):
    def at(age):
        return None if age is None else (NOW - timedelta(days=age)).isoformat()

    return {"n": n, "all_valid": valid, "all_valid_any_market": valid_any, "any_in_stock": stock,
            "oldest_valid": at(oldest), "oldest_valid_any_market": at(oldest_any),
            "newest_valid_any_market": at(newest_any)}


def _final_row(class_by_market, *, any_markets=None, final=None, final_any=None, non_class=None):
    """`class_by_market`: round 3's per-market aggregates; `final` / `final_any`:
    the round-4 SQL row reads (age in days); `non_class`: live offers outside
    round 3's class, by reason."""
    def at(age):
        return None if age is None else NOW - timedelta(days=age)

    return census.classify(
        {"product_key": "k_final", "lane": "retailer", "host": "shop.example.com",
         "serving_decision": "shadow", "reason_codes": [], "identity_status": "unknown",
         "renderable": True, "last_price_read_at": at(1),
         "market_reads": {m: at(a) for m, a in (any_markets or {"US": 1}).items()},
         "last_in_stock_read_at": at(1), "class_by_market": class_by_market,
         "final_read_at": at(final), "final_read_any_market_at": at(final_any),
         "live_non_class": non_class},
        now=NOW, max_age_days=7)


def test_the_rule_is_the_sql_row_read():
    """Round 4: the strict columns are the SQL read over every live offer, not
    round 3's per-market class aggregates."""
    row = _final_row({"US": _cls(oldest=1, oldest_any=1, newest_any=1)}, final=2, final_any=2)
    assert row["would_flip_strict"] is True and row["strict_read_at"] == NOW - timedelta(days=2)
    # round 3 would flip it; a live offer outside its class makes the SQL read NULL
    row = _final_row({"US": _cls(oldest=1, oldest_any=1, newest_any=1)}, final=None, final_any=None,
                     non_class={"affiliate": 1})
    assert row["would_flip_strict"] is False and row["would_flip_round3"] is True
    assert row["non_class_reasons"] == ["affiliate"]
    # B2: undeclared -> only the any-market read
    row = _final_row({}, final=None, final_any=1)
    assert row["would_flip_strict"] is False and row["would_flip_any_market_declaration"] is True
    assert _final_row({}, final=8, final_any=8)["would_flip_strict"] is False


def test_the_round3_rule_needs_every_market_fresh_and_declared():
    ok = {"US": _cls(oldest=1, oldest_any=1, newest_any=1), "SG": _cls(oldest=2, oldest_any=2, newest_any=2)}
    row = _final_row(ok)
    assert row["would_flip_round3"] is True and row["round3_read_at"] == NOW - timedelta(days=2)
    stale_us = {"US": _cls(oldest=60, oldest_any=60, newest_any=60), "SG": _cls(oldest=0.1, oldest_any=0.1,
                                                                             newest_any=0.1)}
    row = _final_row(stale_us)
    assert row["would_flip_round3"] is False
    assert row["fresh_in_one_market_stale_in_another"] is True
    undeclared = {"US": _cls(valid=False, oldest=None, oldest_any=1, newest_any=1)}
    assert _final_row(undeclared)["would_flip_round3"] is False
    oos = {"US": _cls(stock=False, oldest=1, oldest_any=1, newest_any=1)}
    assert _final_row(oos)["would_flip_round3"] is False
    assert _final_row({})["would_flip_round3"] is False
    assert _final_row({"US": _cls(n=0)})["would_flip_round3"] is False
    # a market with no class offer (not served) is ignored, its NULL aggregates included
    unserved = {"US": _cls(oldest=1, oldest_any=1, newest_any=1),
                "AU": _cls(n=0, valid=None, valid_any=None, stock=None)}
    assert _final_row(unserved)["would_flip_round3"] is True
    # one market where SOME class offer lacks a valid read sinks the row, even though its other
    # offers were read (so its oldest valid read is not NULL)
    one_bad = {"US": _cls(valid=False, oldest=1, oldest_any=1, newest_any=1),
               "SG": _cls(oldest=1, oldest_any=1, newest_any=1)}
    assert _final_row(one_bad)["would_flip_round3"] is False


def test_the_non_class_counter_splits_by_reason_and_dedupes_the_total():
    fresh3 = {"US": _cls(oldest=1, oldest_any=1, newest_any=1)}
    rows = [
        _final_row(fresh3, non_class={"affiliate": 2, "non_drain_source": 0}),
        _final_row(fresh3, non_class={"non_drain_source": 1, "non_served_market": 1}),
        _final_row(fresh3, non_class={"currency_mismatch": 1, "non_drain_source": 3, "affiliate": 1}),
        # round 3 would not flip it (stale): never counted, whatever it carries
        _final_row({"US": _cls(oldest=30, oldest_any=30, newest_any=30)}, non_class={"affiliate": 1}),
        # round 3 flips, nothing outside its class: round 4 flips it too
        _final_row(fresh3, final=1, final_any=1, non_class={"affiliate": 0}),
        _final_row(fresh3, final=1, final_any=1),
    ]
    total = [line for line in census.summarize(rows, now=NOW, max_age_days=7, seed_pool_by_market={"US": 1})
             if line["kind"] == "summary" and line["lane"] == "all"][0]
    assert total["would_flip_round3_class_rule"] == 5
    assert total["would_flip_per_market_rule"] == 2
    assert total["would_flip_but_carries_live_non_class_offer"] == {
        "affiliate": 2, "non_drain_source": 2, "non_served_market": 1, "currency_mismatch": 1, "total": 3}


def test_the_non_class_reasons_are_the_four_asked_for():
    assert list(census.NON_CLASS_REASON_SQL) == [
        "affiliate", "non_drain_source", "non_served_market", "currency_mismatch"]
    assert census.SERVED_MARKETS_SQL == "upper(btrim(coalesce(co.market, ''))) IN ('SG', 'US')"


def test_row_read_at_and_cross_market_directly():
    assert census.row_read_at({}, declared=True) is None
    m = {"US": _cls(oldest=3, oldest_any=3), "SG": _cls(oldest=1, oldest_any=1)}
    assert census.row_read_at(m, declared=True) == NOW - timedelta(days=3)
    m["SG"]["oldest_valid"] = None
    assert census.row_read_at(m, declared=True) is None
    assert census.row_read_at(m, declared=False) == NOW - timedelta(days=3)
    one = {"US": _cls(oldest_any=1, newest_any=1)}
    assert census.cross_market_stale(one, now=NOW, max_age_days=7) is False
    both_fresh = {"US": _cls(oldest_any=1, newest_any=1), "SG": _cls(oldest_any=2, newest_any=2)}
    assert census.cross_market_stale(both_fresh, now=NOW, max_age_days=7) is False
    unread = {"US": _cls(valid_any=False, oldest_any=None, newest_any=None), "SG": _cls(oldest_any=1, newest_any=1)}
    assert census.cross_market_stale(unread, now=NOW, max_age_days=7) is True


def test_a_future_per_market_read_never_ages_in():
    row = _final_row({"US": _cls(oldest=-2, oldest_any=-2, newest_any=-2)}, final=-2, final_any=-2)
    assert row["strict_age_days"] is None
    assert row["would_flip_strict"] is False
    out = census.project([row], max_age_days=7, nightly_probability=0.0, age_field="strict_age_days")
    assert out["no_reread"]["by_day"]["0"] == 0


def test_per_market_flips_are_counted_only_in_their_strict_markets():
    row = _final_row({"US": _cls(oldest=1, oldest_any=1, newest_any=1)}, any_markets={"US": 1, "AU": 1},
                     final=1, final_any=1)
    assert row["would_flip_strict"] is True
    lines = census.summarize([row], now=NOW, max_age_days=7, seed_pool_by_market={"US": 10},
                             extras=[{"kind": "drain_job_markets", "jobs": 3}])
    markets = {line["market"]: line for line in lines if line["kind"] == "by_market" and line["lane"] == "all"}
    assert markets["US"]["would_flip_per_market_rule"] == 1
    assert markets["AU"].get("would_flip_per_market_rule", 0) == 0
    assert markets["AU"]["would_flip"] == 1
    assert {"kind": "drain_job_markets", "jobs": 3} in lines
    stale = _final_row({"SG": _cls(oldest=30, oldest_any=30, newest_any=30)}, any_markets={"SG": 30})
    stale_lines = census.summarize([stale], now=NOW, max_age_days=7, seed_pool_by_market={"US": 10})
    sg = [line for line in stale_lines if line["kind"] == "by_market" and line["lane"] == "all"][0]
    assert "would_flip_per_market_rule" not in sg  # only a row that flips names its markets
    assert {"kind": "served_market_currency", "pairs": [["SG", "SGD"], ["US", "USD"]]} in lines


def test_census_file_carries_no_write_and_no_at_sign_after_round_two():
    body = (_REPO / "scripts/census_retailer_crawl_fresh_trust.py").read_text()
    assert "@" not in body


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
