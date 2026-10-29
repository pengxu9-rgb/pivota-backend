#!/usr/bin/env python3
"""READ-ONLY census: what CRAWL_FRESH would move, now and in steady state.

THE RULE (services/catalog_trust_policy + services/catalog_row_trust_upserter,
switched by CATALOG_TRUST_CRAWL_FRESH_ENABLED, default off; rounds 4-6). A
drain-lane storefront crawl row -- product_key `ext:...` written by
`catalog_enrichment_agent_v1`, i.e. the retailer listings (`ext:retailer:...`)
AND the brand-official / curated canonicals -- is exempt from the identity
COVERAGE gates (IDENTITY_CONFIDENCE_NULL, IDENTITY_LIVE_READ_DISABLED, the
silent identity-'unknown' shadow) while its price-read date is within N days:

  * the CLASS is EVERY live offer on the row: unsuppressed and priced the way
    the widest reader prices it, coalesce(merchant_effective_price,
    estimated_best_price, list_price) > 0 (round 6) -- any source_system,
    market, currency or link, affiliate offers included (CLASS_SQL; round 5
    dropped the link test, because not every reader tests the link);
  * a VALID read of one needs ALL of: the drain wrote it, a served
    (market, currency) pair, not an affiliate-feed offer, price_checked_at
    (migration 246) no later than now, its market DECLARED by the writer
    (offer_payload.declared_market = market), not an employee price edit
    (VALID_READ_SQL);
  * the row's read = the OLDEST valid read, and only when every class offer
    has one and at least one class offer is not out of stock; else none.
    Fresh when 0 <= age <= N.
Review, conflict, non-canonical and every blocked cause still win, and a PDP
known not to render is never promoted.

It answers, BEFORE the switch flips, from the STORED catalog_row_trust rows (the
6h backfill's last pass) plus reads of the offers, split by LANE (`retailer` =
ext:retailer:, otherwise the row's enrichment_meta.source_role, e.g.
`brand_official`; `unlabelled` when it has none). Rule names in the output:

  * `per_market` / `strict` = THE RULE above (rounds 4-5): would_flip_per_market_rule,
    the per_market projections, the employee-edit and B2 columns;
  * `round3_class` = round 3's narrower class (only live drain-lane,
    non-affiliate offers in a served (market, currency) pair linking to an
    http(s) destination; the rest neither dated nor blocked a row). Kept to
    size what rounds 4-5 cost: `would_flip_but_carries_live_non_class_offer`,
    rows round 3 would flip that carry a live offer the rule now refuses, by
    reason -- `affiliate`, `non_drain_source`, `non_served_market` (a market we
    do not serve, any currency), `currency_mismatch` (a served market priced in
    another currency than its own, e.g. US/SGD), `unservable_link` (a link that
    is not http(s) -- '', a bare path, ftp: -- on an offer whose read is not
    valid) -- and `total`, each row once (a row can carry several reasons).
    `would_flip_without_declared_market_but_carries_live_non_class_offer` is the
    same split keyed to the counterfactual that waives ONLY the declared-market
    requirement on both sides (`would_flip_round3_class_without_declared_market`
    flips; total = those minus `would_flip_without_declared_market_requirement`),
    so it reads before any drain job declares a market;
  * `max_any` = the round-1 rule (the newest read of any live drain-lane,
    non-affiliate offer; a future stamp counted fresh), kept for the trend.

Lines (JSON, one per line):
  * params, served_market_currency, backfill_coverage (catalog_products vs the
    backfill cron's CATALOG_TRUST_BACKFILL_LIMIT default), drain_job_markets
    (how many drain jobs name options.market -- only their offers can be
    declared), employee_edit_dry_run;
  * summary per lane -- would flip shadow -> public under each rule, at N and
    at N = 1/3/7/14 (max_any), B2's cost (`would_flip_lost_to_undeclared_market`),
    B1 (`fresh_in_one_market_stale_in_another`), the out-of-stock-only flips,
    rows still blocked or not renderable;
  * by_age_bucket (newest max_any read: <=1d, <=3d, <=7d, <=14d, >14d, NULL),
    by_market, by_host;
  * projection -- the next 7 days, pure arithmetic over the rows read, per rule:
      no_reread         nothing re-reads anything: each row drops out the day
                        its read passes N;
      refresh_pro_rata  the paced external-referral refresh re-reads
                        --refresh-per-night seeds a night (default 1,300) spread
                        pro rata over --seed-pool (default: the active US seeds,
                        counted here); expected values;
      drain_weekly      the drain re-crawls every listing weekly: every row with
                        a live offer is re-stamped within 7 days, evenly;
                        expected values.
    Each with its steady state, so the switch is not flipped on a post-catch-up
    peak.

"Would flip" = stored decision 'shadow', no review/conflict/non-canonical
reason and identity not review_required (so coverage is the only thing
shadowing it), crawl-fresh at N, renderable. It is a census of the stored
decision, so a row whose inputs moved since the last backfill pass is counted
on its stored verdict.

SAFETY. One READ ONLY transaction with SET LOCAL statement_timeout; it writes
nothing and creates nothing: one row query plus a few small aggregates. Prints
JSON lines only. It carries no at-sign so it can be passed inline to
scripts/ops/run_oneoff_job.sh (see its header), and it needs no crawl subnet
(it reads the database only):

    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/census_retailer_crawl_fresh_trust.py)"
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/census_retailer_crawl_fresh_trust.py)" --days 3

Self-contained on purpose: the deployed image may predate the rule, so the
offer fragments below are COPIES of services/catalog_row_trust_upserter's, the
row predicate a copy of catalog_trust_policy.is_crawl_listing and the freshness
test a copy of catalog_trust_policy.is_crawl_fresh.
tests/test_census_retailer_crawl_fresh_trust.py pins every copy to its original
and tests/test_crawl_fresh_trust_postgres.py pins the census's row read to the
upserter's statement on the same rows. Only `seed_route_resolves_sql`
(services/pdp_renderability, in every image since 2026-07) is imported from the
image.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

if os.path.isdir("/app") and "/app" not in sys.path:
    sys.path.insert(0, "/app")

CRAWL_LISTING_KEY_PREFIX = "ext:"
CRAWL_SOURCE_SYSTEM = "catalog_enrichment_agent_v1"
RETAILER_KEY_PREFIX = "ext:retailer:"
DEFAULT_DAYS = 7.0
DEFAULT_REFRESH_PER_NIGHT = 1300
PROJECTION_DAYS = 7
STATEMENT_TIMEOUT = "300s"

# The gateway offer reader's own `servable` test (routes/agent_shop_gateway,
# pinned by test). Round 5 took it out of the rule's class; the census keeps it
# for round 3's class and the `unservable_link` reason.
OFFER_LINK_SQL = (
    "btrim(coalesce(co.offer_payload->>'destination_url', co.source_ref),"
    r" E' \t\n\r\f\v') ~ '^https?://'"
)
# COPIES of services/catalog_row_trust_upserter's fragments (pinned by test).
OFFER_NOT_AFFILIATE_SQL = (
    "coalesce(co.offer_payload->>'validated_at', '') NOT LIKE 'affiliate_feed:%'"
)
DRAIN_SOURCE_SQL = f"co.source_system = '{CRAWL_SOURCE_SYSTEM}'"
# THE CLASS (rounds 4-6): every unsuppressed offer of the row any reader prices
# (mep, else estimated best, else list) -- the upserter's
# crawl_offer_class_conjuncts minus the product_key join.
CLASS_SQL = (
    "co.suppressed_at IS NULL\n"
    "           AND coalesce(co.merchant_effective_price, co.estimated_best_price, co.list_price) > 0"
)
# The max-any (round-1) and round-3 aggregates read only live drain-lane,
# non-affiliate offers with an http(s) link; max() ignores a NULL read. (Priced
# as CLASS_SQL prices, so the round-3 counterfactual differs from the rule only
# by the reasons below and each reason split's total is exact.)
OFFER_CONJUNCTS_SQL = (
    f"{DRAIN_SOURCE_SQL}\n"
    f"           AND {CLASS_SQL}\n"
    f"           AND {OFFER_LINK_SQL}\n"
    f"           AND {OFFER_NOT_AFFILIATE_SQL}"
)

SERVED_MARKET_CURRENCY_PAIRS = (("SG", "SGD"), ("US", "USD"))
SERVED_MARKET_CURRENCY_SQL = (
    "(upper(btrim(coalesce(co.market, ''))), upper(btrim(coalesce(co.currency, ''))))"
    " IN (('SG', 'SGD'), ('US', 'USD'))"
)
IN_STOCK_SQL = (
    "lower(btrim(coalesce(co.availability, '')))"
    " NOT IN ('out_of_stock', 'outofstock', 'sold_out', 'soldout', 'unavailable')"
)
NOT_EMPLOYEE_EDIT_SQL = "coalesce(co.offer_payload->>'price_read_source', '') <> 'employee_edit'"
DECLARED_MARKET_SQL = (
    "upper(btrim(coalesce(co.offer_payload->>'declared_market', '')))"
    " = upper(btrim(coalesce(co.market, '')))"
)
# A VALID read (round 4). The census evaluates SQL now() inside its one
# transaction, where now() is the transaction's start -- the same instant
# `SELECT now()` hands the Python side.
VALID_READ_SQL = (
    f"CASE WHEN {DRAIN_SOURCE_SQL}"
    f" AND {SERVED_MARKET_CURRENCY_SQL}"
    f" AND {OFFER_NOT_AFFILIATE_SQL}"
    " AND co.price_checked_at <= now()"
    f" AND {DECLARED_MARKET_SQL}"
    f" AND {NOT_EMPLOYEE_EDIT_SQL}"
    " THEN co.price_checked_at END"
)
# The same read without the declared-market requirement, to size what B2 costs.
VALID_READ_ANY_MARKET_SQL = VALID_READ_SQL.replace(f" AND {DECLARED_MARKET_SQL}", "")

# Why a live offer is outside round 3's class and makes the rule's row NULL. A
# served market priced in another currency is `currency_mismatch`; a market we
# do not serve is `non_served_market` whatever its currency.
SERVED_MARKETS_SQL = (
    "upper(btrim(coalesce(co.market, '')))"
    " IN (" + ", ".join(f"'{m}'" for m, _ in SERVED_MARKET_CURRENCY_PAIRS) + ")"
)
NON_CLASS_REASON_SQL = {
    "affiliate": f"NOT ({OFFER_NOT_AFFILIATE_SQL})",
    "non_drain_source": f"NOT coalesce({DRAIN_SOURCE_SQL}, FALSE)",
    "non_served_market": f"NOT ({SERVED_MARKETS_SQL})",
    "currency_mismatch": f"{SERVED_MARKETS_SQL} AND NOT ({SERVED_MARKET_CURRENCY_SQL})",
}
# Round 5's reason. The four above are never a valid read; an offer behind an
# unservable link can be (the link is not part of VALID), so it costs the row
# only when it has no valid read OR its valid read is older than N -- judged in
# Python (classify), where N is. A NULL link (no destination, no source_ref) is
# unservable too: coalesce, so the FILTER never reads it as "not counted".
UNSERVABLE_LINK_SQL = f"NOT coalesce({OFFER_LINK_SQL}, FALSE)"
NON_CLASS_REASONS = tuple(NON_CLASS_REASON_SQL) + ("unservable_link",)

PRICE_CHECK_COLUMN_SQL = """
SELECT EXISTS (
  SELECT 1 FROM pg_attribute
  WHERE attrelid = to_regclass('catalog_offers')
    AND attname = 'price_checked_at'
    AND attnum > 0
    AND NOT attisdropped
) AS present
"""

# The refresh's own pool (services/external_referral_readiness candidate SQL:
# status = 'active'), by market. One aggregate.
SEED_POOL_SQL = """
SELECT coalesce(nullif(upper(btrim(market)), ''), '(none)') AS market, count(*) AS seeds
  FROM external_product_seeds
 WHERE status = 'active'
 GROUP BY 1
"""

# How far the 6h backfill reaches (jobs/catalog_row_trust_backfill_cron: oldest
# updated_at first, LIMIT CATALOG_TRUST_BACKFILL_LIMIT). One aggregate.
BACKFILL_COVERAGE_SQL = """
SELECT (SELECT count(*) FROM catalog_products) AS catalog_products,
       count(*) AS trust_rows,
       count(*) FILTER (WHERE 'CRAWL_FRESH' = ANY(crt.serving_reason_codes)) AS crawl_fresh_rows,
       min(crt.updated_at) AS oldest_trust_write
  FROM catalog_row_trust crt
 WHERE crt.subject_type = 'product'
"""

# B2's trade-off: how many drain jobs name their market explicitly (only their
# offers can be declared). Guarded: a database without the table reports none.
DRAIN_JOB_MARKETS_SQL = """
SELECT coalesce(upper(btrim(options->>'market')), '(default)') AS market,
       count(*) AS jobs,
       count(DISTINCT domain) AS hosts
  FROM retailer_ingest_jobs
 GROUP BY 1
"""
DRAIN_JOBS_TABLE_SQL = "SELECT to_regclass('retailer_ingest_jobs') IS NOT NULL AS present"

# Dry run for the employee-edit gap: a stamp written before the attached lane
# recorded price_read_source cannot say who vouched. An attached seed whose
# stored price differs from its last page read was edited by hand; count the
# drain rows behind such seeds.
EDITED_SEED_ROWS_SQL = f"""
SELECT count(DISTINCT s.attached_product_key) AS rows_with_hand_edited_seed,
       count(*) AS hand_edited_seeds
  FROM external_product_seeds s
  JOIN catalog_products cp ON cp.product_key = s.attached_product_key
 WHERE cp.product_key LIKE '{CRAWL_LISTING_KEY_PREFIX}%'
   AND cp.source_system = '{CRAWL_SOURCE_SYSTEM}'
   AND cp.suppression_reason IS NULL
   AND s.status = 'active'
   AND s.price_amount IS NOT NULL
   AND CAST(s.price_amount AS numeric)
       <> CASE WHEN btrim(s.seed_data->'snapshot'->>'price_amount') ~ '^[0-9]+([.][0-9]+)?$'
               THEN CAST(btrim(s.seed_data->'snapshot'->>'price_amount') AS numeric) END
"""
# (The CASE, not a regex conjunct beside the CAST: Postgres does not promise to
# evaluate AND left to right, so a malformed snapshot price must never reach
# the CAST. A non-numeric snapshot compares NULL and is not counted.)

STILL_BLOCKING = {
    "review_required": "IDENTITY_REVIEW_REQUIRED_LIVE_READ",
    "conflict": "IDENTITY_CONFLICT",
    "non_canonical": "NON_CANONICAL_DUPLICATE",
}
AGE_BUCKETS = (("le_1d", 1.0), ("le_3d", 3.0), ("le_7d", 7.0), ("le_14d", 14.0))
DAY_SECONDS = 24 * 60 * 60
ALL = "all"


def _non_class_counts_sql() -> str:
    return ", ".join(
        [f"'{reason}', count(*) FILTER (WHERE {sql})" for reason, sql in NON_CLASS_REASON_SQL.items()]
        + [f"'unservable_link_n', count(*) FILTER (WHERE {UNSERVABLE_LINK_SQL})",
           f"'unservable_link_all_valid', bool_and(({VALID_READ_SQL}) IS NOT NULL)"
           f" FILTER (WHERE {UNSERVABLE_LINK_SQL})",
           f"'unservable_link_oldest_valid', min({VALID_READ_SQL}) FILTER (WHERE {UNSERVABLE_LINK_SQL})",
           # the same with the declared-market requirement waived
           f"'unservable_link_all_valid_any_market', bool_and(({VALID_READ_ANY_MARKET_SQL}) IS NOT NULL)"
           f" FILTER (WHERE {UNSERVABLE_LINK_SQL})",
           f"'unservable_link_oldest_valid_any_market', min({VALID_READ_ANY_MARKET_SQL})"
           f" FILTER (WHERE {UNSERVABLE_LINK_SQL})"])


def _non_class_reasons(live_non_class: Dict[str, Any], *, declared: bool, now: datetime,
                       max_age_days: float) -> List[str]:
    """The reasons a row carries a live offer outside round 3's class that the rule
    refuses. The four NON_CLASS_REASON_SQL offers are never valid; an offer behind
    an unservable link refuses the row only without a valid read inside N (with
    `declared` False, the declared-market requirement waived)."""
    reasons = [r for r in NON_CLASS_REASON_SQL if int(live_non_class.get(r) or 0) > 0]
    suffix = "" if declared else "_any_market"
    if int(live_non_class.get("unservable_link_n") or 0) > 0 and not (
            live_non_class.get("unservable_link_all_valid" + suffix) is True
            and is_fresh_strict(live_non_class.get("unservable_link_oldest_valid" + suffix), now=now,
                                max_age_days=max_age_days)):
        reasons.append("unservable_link")
    return reasons


def _reason_split(rows: List[Dict[str, Any]], field: str) -> Dict[str, int]:
    return {**{reason: sum(reason in r[field] for r in rows) for reason in NON_CLASS_REASONS},
            "total": sum(bool(r[field]) for r in rows)}


def census_sql() -> str:
    from services.pdp_renderability import seed_route_resolves_sql

    return f"""
SELECT cp.product_key,
       CASE WHEN cp.product_key LIKE '{RETAILER_KEY_PREFIX}%' THEN 'retailer'
            ELSE coalesce(nullif(btrim(cp.product_payload->'enrichment_meta'->>'source_role'), ''),
                          'unlabelled')
       END AS lane,
       lower(coalesce(nullif(btrim(cp.source_domain), ''), '(none)')) AS host,
       crt.serving_decision,
       coalesce(crt.serving_reason_codes, ARRAY[]::text[]) AS reason_codes,
       crt.identity_status,
       {seed_route_resolves_sql("cp")} AS renderable,
       rd.last_price_read_at,
       rd.market_reads,
       rd.last_in_stock_read_at,
       rd.class_by_market,
       fr.final_read_at,
       fr.final_read_any_market_at,
       fr.live_non_class
  FROM catalog_products cp
  LEFT JOIN catalog_row_trust crt
    ON crt.subject_type = 'product'
   AND crt.subject_key = cp.product_key
  LEFT JOIN LATERAL (
    SELECT max(m.last_read) AS last_price_read_at,
           jsonb_object_agg(m.market, m.last_read) AS market_reads,
           max(m.last_in_stock) AS last_in_stock_read_at,
           jsonb_object_agg(m.market, jsonb_build_object(
             'n', m.n_class, 'all_valid', m.all_valid, 'all_valid_any_market', m.all_valid_any_market,
             'any_in_stock', m.any_in_stock, 'oldest_valid', m.oldest_valid,
             'oldest_valid_any_market', m.oldest_valid_any_market,
             'newest_valid_any_market', m.newest_valid_any_market)) AS class_by_market
      FROM (
        SELECT coalesce(nullif(upper(btrim(co.market)), ''), '(none)') AS market,
               max(co.price_checked_at) AS last_read,
               max(co.price_checked_at) FILTER (WHERE {IN_STOCK_SQL}) AS last_in_stock,
               count(*) FILTER (WHERE {SERVED_MARKET_CURRENCY_SQL}) AS n_class,
               bool_and(({VALID_READ_SQL}) IS NOT NULL)
                 FILTER (WHERE {SERVED_MARKET_CURRENCY_SQL}) AS all_valid,
               bool_and(({VALID_READ_ANY_MARKET_SQL}) IS NOT NULL)
                 FILTER (WHERE {SERVED_MARKET_CURRENCY_SQL}) AS all_valid_any_market,
               bool_or({IN_STOCK_SQL}) FILTER (WHERE {SERVED_MARKET_CURRENCY_SQL}) AS any_in_stock,
               min({VALID_READ_SQL}) FILTER (WHERE {SERVED_MARKET_CURRENCY_SQL}) AS oldest_valid,
               min({VALID_READ_ANY_MARKET_SQL})
                 FILTER (WHERE {SERVED_MARKET_CURRENCY_SQL}) AS oldest_valid_any_market,
               max({VALID_READ_ANY_MARKET_SQL})
                 FILTER (WHERE {SERVED_MARKET_CURRENCY_SQL}) AS newest_valid_any_market
          FROM catalog_offers co
         WHERE co.product_key = cp.product_key
           AND {OFFER_CONJUNCTS_SQL}
         GROUP BY 1
      ) m
  ) rd ON TRUE
  -- THE RULE (round 4): the upserter's row read over every live offer, plus
  -- the live offers round 3's class left out, by reason.
  LEFT JOIN LATERAL (
    SELECT CASE WHEN count(*) > 0
                 AND bool_and(({VALID_READ_SQL}) IS NOT NULL)
                 AND bool_or({IN_STOCK_SQL})
                THEN min({VALID_READ_SQL}) END AS final_read_at,
           CASE WHEN count(*) > 0
                 AND bool_and(({VALID_READ_ANY_MARKET_SQL}) IS NOT NULL)
                 AND bool_or({IN_STOCK_SQL})
                THEN min({VALID_READ_ANY_MARKET_SQL}) END AS final_read_any_market_at,
           jsonb_build_object({_non_class_counts_sql()}) AS live_non_class
      FROM catalog_offers co
     WHERE co.product_key = cp.product_key
       AND {CLASS_SQL}
  ) fr ON TRUE
 WHERE cp.product_key LIKE '{CRAWL_LISTING_KEY_PREFIX}%'
   AND cp.source_system = '{CRAWL_SOURCE_SYSTEM}'
   AND cp.suppression_reason IS NULL
"""


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def is_crawl_listing(product_key: Any, source_system: Any) -> bool:
    """COPY of catalog_trust_policy.is_crawl_listing, on the two values."""
    return str(product_key or "").startswith(CRAWL_LISTING_KEY_PREFIX) and str(
        source_system or ""
    ) == CRAWL_SOURCE_SYSTEM


def is_fresh(last_read: Any, *, now: datetime, max_age_days: float) -> bool:
    """The ROUND-1 freshness test (the max-any columns): inclusive bound, a
    stamp ahead of `now` is fresh, no read is not fresh."""
    ts = _to_datetime(last_read)
    if ts is None:
        return False
    return (now - ts).total_seconds() <= float(max_age_days) * DAY_SECONDS


def is_fresh_strict(last_read: Any, *, now: datetime, max_age_days: float) -> bool:
    """COPY of catalog_trust_policy.is_crawl_fresh (round 2): as is_fresh, but a
    stamp dated in the FUTURE is stale, as relgraph_freshness_refresh treats it
    (0 <= age <= N)."""
    ts = _to_datetime(last_read)
    if ts is None:
        return False
    age = (now - ts).total_seconds()
    return 0 <= age <= float(max_age_days) * DAY_SECONDS


def age_days(last_read: Any, *, now: datetime) -> Optional[float]:
    ts = _to_datetime(last_read)
    return None if ts is None else (now - ts).total_seconds() / DAY_SECONDS


def _non_negative(age: Optional[float]) -> Optional[float]:
    return None if age is None or age < 0 else age


def age_bucket(last_read: Any, *, now: datetime) -> str:
    age = age_days(last_read, now=now)
    if age is None:
        return "null"
    for name, days in AGE_BUCKETS:
        if age <= days:
            return name
    return "gt_14d"


def _market_reads(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    return dict(raw) if isinstance(raw, dict) else {}


def row_read_at(by_market: Dict[str, Dict[str, Any]], *, declared: bool) -> Optional[datetime]:
    """ROUND 3's row read from the per-market aggregates of its narrower class
    (live drain-lane, non-affiliate offers in a served pair): None unless that
    class is not empty, every offer in it has a valid read and one is in stock;
    else the OLDEST valid read. The round-4 read is SQL (final_read_at)."""
    if not by_market:
        return None
    valid_key, oldest_key = (("all_valid", "oldest_valid") if declared
                             else ("all_valid_any_market", "oldest_valid_any_market"))
    if not all(v.get(valid_key) is True for v in by_market.values()):
        return None
    if not any(v.get("any_in_stock") is True for v in by_market.values()):
        return None
    reads = [_to_datetime(v.get(oldest_key)) for v in by_market.values()]
    if any(r is None for r in reads):
        return None
    return min(reads)


def cross_market_stale(by_market: Dict[str, Dict[str, Any]], *, now: datetime, max_age_days: float) -> bool:
    """B1's case, without B2: some served market has a fresh valid read while
    ANOTHER has a live class offer that is unread, invalid or stale."""
    fresh_somewhere = {m for m, v in by_market.items()
                       if is_fresh_strict(v.get("newest_valid_any_market"), now=now, max_age_days=max_age_days)}
    stale_somewhere = {m for m, v in by_market.items()
                       if not (v.get("all_valid_any_market") is True and is_fresh_strict(
                           v.get("oldest_valid_any_market"), now=now, max_age_days=max_age_days))}
    return any(a != b for a in fresh_somewhere for b in stale_somewhere)


def classify(row: Dict[str, Any], *, now: datetime, max_age_days: float) -> Dict[str, Any]:
    decision = row.get("serving_decision") or "no_trust_row"
    reasons = set(row.get("reason_codes") or [])
    blocking = sorted(k for k, code in STILL_BLOCKING.items() if code in reasons)
    if row.get("identity_status") == "review_required" and "review_required" not in blocking:
        blocking = sorted(blocking + ["review_required"])
    fresh = is_fresh(row.get("last_price_read_at"), now=now, max_age_days=max_age_days)
    coverage_only = decision == "shadow" and not blocking
    renderable = row.get("renderable") is not False
    markets = _market_reads(row.get("market_reads"))
    by_market = {m: v for m, v in _market_reads(row.get("class_by_market")).items()
                 if isinstance(v, dict) and int(v.get("n") or 0) > 0}
    # THE RULE (round 4): the SQL row read over every live offer.
    final_read = _to_datetime(row.get("final_read_at"))
    any_market_read = _to_datetime(row.get("final_read_any_market_at"))
    fresh_strict = is_fresh_strict(final_read, now=now, max_age_days=max_age_days)
    fresh_any_market = is_fresh_strict(any_market_read, now=now, max_age_days=max_age_days)
    would_flip = coverage_only and fresh and renderable
    # Round 3's narrower class, and the live offers it left out (by reason).
    round3_read = row_read_at(by_market, declared=True)
    would_flip_round3 = coverage_only and renderable and is_fresh_strict(
        round3_read, now=now, max_age_days=max_age_days)
    live_non_class = _market_reads(row.get("live_non_class"))
    non_class_reasons = _non_class_reasons(live_non_class, declared=True, now=now, max_age_days=max_age_days)
    # The same, with ONLY the declared-market requirement waived on both sides.
    would_flip_round3_any_market = coverage_only and renderable and is_fresh_strict(
        row_read_at(by_market, declared=False), now=now, max_age_days=max_age_days)
    non_class_reasons_any_market = _non_class_reasons(
        live_non_class, declared=False, now=now, max_age_days=max_age_days)
    return {
        "product_key": row["product_key"],
        "lane": row.get("lane") or "unlabelled",
        "host": row.get("host") or "(none)",
        "decision": decision,
        "bucket": age_bucket(row.get("last_price_read_at"), now=now),
        "age_days": age_days(row.get("last_price_read_at"), now=now),
        "fresh": fresh,
        "blocking": blocking,
        "coverage_only_shadow": coverage_only,
        "renderable": renderable,
        # Could ever flip on a re-read: coverage is all that shadows it, it
        # renders, and it has at least one qualifying live offer to re-stamp.
        "candidate": coverage_only and renderable and bool(markets),
        "would_flip": would_flip,
        # THE RULE (see VALID_READ_SQL); its row read is never in the future
        # (now() bounds it), and _non_negative keeps a stray one out.
        "strict_read_at": final_read,
        "strict_age_days": _non_negative(age_days(final_read, now=now)),
        "would_flip_strict": coverage_only and fresh_strict and renderable,
        # the same rule without B2's declared-market requirement
        "would_flip_any_market_declaration": coverage_only and fresh_any_market and renderable,
        # round 3's class, and the reasons its flip is refused now (empty: not refused)
        "round3_read_at": round3_read,
        "would_flip_round3": would_flip_round3,
        "non_class_reasons": non_class_reasons if would_flip_round3 else [],
        "would_flip_round3_any_market_declaration": would_flip_round3_any_market,
        "non_class_reasons_any_market": non_class_reasons_any_market if would_flip_round3_any_market else [],
        # B1: fresh in one served market while another has a stale/unread live offer
        "fresh_in_one_market_stale_in_another": coverage_only and renderable
            and cross_market_stale(by_market, now=now, max_age_days=max_age_days),
        # flips under the max-any rule only because of an out-of-stock read
        "would_flip_oos_only": would_flip and not is_fresh(
            row.get("last_in_stock_read_at"), now=now, max_age_days=max_age_days),
        "strict_fresh_markets": sorted(by_market) if fresh_strict else [],
        "fresh_not_renderable": coverage_only and fresh and not renderable,
        "markets": sorted(markets) or ["(no live offer)"],
        "fresh_markets": sorted(
            m for m, ts in markets.items() if is_fresh(ts, now=now, max_age_days=max_age_days)
        ),
    }


# ---- projection: pure arithmetic --------------------------------------------


def _fresh_by_aging(age: Optional[float], day: int, max_age_days: float) -> bool:
    return age is not None and age + day <= max_age_days


def _reread_nights(day: int, max_age_days: float) -> int:
    """Nights k in 1..day whose re-read is still within N at `day` (day-k <= N)."""
    first = max(1, math.ceil(day - max_age_days))
    return max(0, day - first + 1)


def project(
    rows: List[Dict[str, Any]],
    *,
    max_age_days: float,
    nightly_probability: float,
    age_field: str = "age_days",
) -> Dict[str, Dict[str, Any]]:
    """Expected would-flip count for days 0..7 under the three scenarios.

    Day 0 is now (equals `would_flip`). A row counts on day d when it is still
    fresh by aging alone (age + d <= N), else with the probability that a re-read
    landed within N of day d:
      * no_reread:        0;
      * refresh_pro_rata: 1 - (1 - p)^nights, p = refresh-per-night / seed pool;
      * drain_weekly:     min(1, min(d, N) / 7) -- one re-crawl per listing per
                          7 days, spread evenly.
    Steady state (aging spent): round-robin min(1, N * p) and the pro-rata
    1 - (1 - p)^(floor(N) + 1) (the nights whose read is still within N) for
    the refresh, min(1, N / 7) for the weekly drain --
    each times the candidate rows (coverage-only shadow, renderable, with a
    qualifying live offer).
    """
    candidates = [r for r in rows if r["candidate"]]
    p = min(1.0, max(0.0, nightly_probability))
    out: Dict[str, Dict[str, Any]] = {}
    for scenario in ("no_reread", "refresh_pro_rata", "drain_weekly"):
        by_day: Dict[str, float] = {}
        for day in range(PROJECTION_DAYS + 1):
            nights = _reread_nights(day, max_age_days)
            total = 0.0
            for r in candidates:
                if _fresh_by_aging(r[age_field], day, max_age_days):
                    total += 1.0
                elif scenario == "refresh_pro_rata":
                    total += 1.0 - (1.0 - p) ** nights
                elif scenario == "drain_weekly":
                    total += min(1.0, min(day, max_age_days) / 7.0)
            by_day[str(day)] = round(total, 1)
        if scenario == "no_reread":
            steady = {"steady_state": 0}
        elif scenario == "refresh_pro_rata":
            steady = {
                "steady_state_pro_rata": round(
                    len(candidates) * (1.0 - (1.0 - p) ** (math.floor(max_age_days) + 1)), 1),
                "steady_state_round_robin": round(len(candidates) * min(1.0, max_age_days * p), 1),
            }
        else:
            steady = {"steady_state": round(len(candidates) * min(1.0, max_age_days / 7.0), 1)}
        out[scenario] = {"by_day": by_day, **steady}
    return out


# ---- the report ---------------------------------------------------------------


def _count(rows: List[Dict[str, Any]], **kw: Any) -> int:
    return sum(1 for r in rows if all(r.get(k) == v for k, v in kw.items()))


def summarize(
    classified: Iterable[Dict[str, Any]],
    *,
    now: datetime,
    max_age_days: float,
    refresh_per_night: int = DEFAULT_REFRESH_PER_NIGHT,
    seed_pool: Optional[int] = None,
    seed_pool_by_market: Optional[Dict[str, int]] = None,
    backfill: Optional[Dict[str, Any]] = None,
    extras: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    rows = list(classified)
    pool_by_market = dict(seed_pool_by_market or {})
    pool = seed_pool if seed_pool is not None else pool_by_market.get("US", 0)
    p = (refresh_per_night / pool) if pool else 0.0
    lanes = sorted({r["lane"] for r in rows})
    lines: List[Dict[str, Any]] = [{
        "kind": "params",
        "now": now.isoformat(),
        "max_age_days": max_age_days,
        "rows": f"{CRAWL_LISTING_KEY_PREFIX}* written by {CRAWL_SOURCE_SYSTEM}, unsuppressed",
        "source": "stored catalog_row_trust + live catalog_offers read",
        "refresh_per_night": refresh_per_night,
        "seed_pool": pool,
        "seed_pool_source": "--seed-pool" if seed_pool is not None else "active US seeds",
        "active_seeds_by_market": dict(sorted(pool_by_market.items())),
        "nightly_reread_probability": round(p, 5),
    }]
    lines.append({"kind": "served_market_currency",
                  "pairs": [list(pair) for pair in SERVED_MARKET_CURRENCY_PAIRS]})
    if backfill is not None:
        lines.append({"kind": "backfill_coverage", **backfill})
    lines.extend(extras or [])

    for lane in [ALL] + lanes:
        part = rows if lane == ALL else [r for r in rows if r["lane"] == lane]
        by_decision: Dict[str, int] = defaultdict(int)
        still_blocked: Dict[str, int] = defaultdict(int)
        for r in part:
            by_decision[r["decision"]] += 1
            if r["fresh"]:
                for b in r["blocking"]:
                    still_blocked[b] += 1
        lines.append({
            "kind": "summary",
            "lane": lane,
            "rows": len(part),
            "by_decision": dict(sorted(by_decision.items())),
            "shadow_coverage_only": sum(r["coverage_only_shadow"] for r in part),
            "candidates_any_age": sum(r["candidate"] for r in part),
            "would_flip": sum(r["would_flip"] for r in part),
            # Round 2 asks: the per-market rule vs the max-any rule, and the
            # flips owed only to an out-of-stock fresh offer.
            "would_flip_max_any_rule": sum(r["would_flip"] for r in part),
            "would_flip_per_market_rule": sum(r["would_flip_strict"] for r in part),
            "would_flip_max_any_not_per_market": sum(r["would_flip"] and not r["would_flip_strict"] for r in part),
            # B2's cost: flips the declared-market requirement withholds today
            "would_flip_without_declared_market_requirement": sum(
                r["would_flip_any_market_declaration"] for r in part),
            "would_flip_lost_to_undeclared_market": sum(
                r["would_flip_any_market_declaration"] and not r["would_flip_strict"] for r in part),
            # B1's case, sized without B2
            "fresh_in_one_market_stale_in_another": sum(r["fresh_in_one_market_stale_in_another"] for r in part),
            # Round 4's cost: what round 3 would flip that a live offer outside
            # its class now refuses, by reason; `total` counts each row once.
            "would_flip_round3_class_rule": sum(r["would_flip_round3"] for r in part),
            "would_flip_but_carries_live_non_class_offer": _reason_split(part, "non_class_reasons"),
            # The same split keyed to the counterfactual that waives ONLY the
            # declared-market requirement (on both sides), so it reads before any
            # drain job declares a market. total = round-3-class flips minus the
            # rule's flips, both without the requirement.
            "would_flip_round3_class_without_declared_market": sum(
                r["would_flip_round3_any_market_declaration"] for r in part),
            "would_flip_without_declared_market_but_carries_live_non_class_offer": _reason_split(
                part, "non_class_reasons_any_market"),
            "would_flip_on_out_of_stock_only_offer": sum(r["would_flip_oos_only"] for r in part),
            "fresh_but_not_renderable": sum(r["fresh_not_renderable"] for r in part),
            "fresh_still_blocked_by_reason": dict(sorted(still_blocked.items())),
            # The same census at other N, so the window can be chosen on the numbers.
            "would_flip_if_n_days": {
                f"{days:g}": sum(
                    1 for r in part
                    if r["coverage_only_shadow"] and r["renderable"]
                    and _fresh_by_aging(r["age_days"], 0, days)
                )
                for _, days in AGE_BUCKETS
            },
        })
        for rule, age_field in (("max_any", "age_days"), ("per_market", "strict_age_days")):
            for scenario, result in project(part, max_age_days=max_age_days, nightly_probability=p,
                                            age_field=age_field).items():
                lines.append({"kind": "projection", "lane": lane, "rule": rule, "scenario": scenario, **result})

    for lane in [ALL] + lanes:
        part = rows if lane == ALL else [r for r in rows if r["lane"] == lane]
        for name in [n for n, _ in AGE_BUCKETS] + ["gt_14d", "null"]:
            b = [r for r in part if r["bucket"] == name]
            lines.append({
                "kind": "by_age_bucket", "lane": lane, "bucket": name,
                "rows": len(b),
                "shadow_coverage_only": sum(r["coverage_only_shadow"] for r in b),
                "would_flip": sum(r["would_flip"] for r in b),
                "still_blocked_by_reason": sum(bool(r["blocking"]) for r in b),
                "decision_public": _count(b, decision="public"),
                "decision_shadow": _count(b, decision="shadow"),
                "decision_blocked": _count(b, decision="blocked"),
            })

    markets: Dict[tuple, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for lane in (ALL, r["lane"]):
            for m in r["markets"]:
                markets[(lane, m)]["rows"] += 1
                markets[(lane, m)]["shadow_coverage_only"] += int(r["coverage_only_shadow"])
            for m in r["fresh_markets"]:
                markets[(lane, m)]["would_flip"] += int(r["would_flip"])
            for m in r["strict_fresh_markets"]:
                markets[(lane, m)]["would_flip_per_market_rule"] += int(r["would_flip_strict"])
    for lane, m in sorted(markets, key=lambda k: (k[0] != ALL, k)):
        lines.append({"kind": "by_market", "lane": lane, "market": m,
                      **dict(sorted(markets[(lane, m)].items()))})

    hosts: Dict[tuple, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        h = hosts[(r["lane"], r["host"])]
        h["rows"] += 1
        h["shadow_coverage_only"] += int(r["coverage_only_shadow"])
        h["would_flip"] += int(r["would_flip"])
        h["would_flip_per_market_rule"] += int(r["would_flip_strict"])
        h["fresh_still_blocked_by_reason"] += int(r["fresh"] and bool(r["blocking"]))
        h["fresh_not_renderable"] += int(r["fresh_not_renderable"])
    for key in sorted(hosts, key=lambda k: (k[0], -hosts[k]["would_flip"], -hosts[k]["rows"], k[1])):
        lines.append({"kind": "by_host", "lane": key[0], "host": key[1], **dict(sorted(hosts[key].items()))})
    return lines


def backfill_limit_default() -> int:
    """The cron's own default (jobs/catalog_row_trust_backfill_cron._DEFAULT_LIMIT),
    read from the image; 50,000 if the image does not carry it. The prod service
    may override it with CATALOG_TRUST_BACKFILL_LIMIT, which this job cannot see."""
    try:
        from jobs.catalog_row_trust_backfill_cron import _DEFAULT_LIMIT

        return int(_DEFAULT_LIMIT)
    except Exception:  # noqa: BLE001
        return 50_000


async def collect(
    conn: Any,
    *,
    max_age_days: float,
    refresh_per_night: int = DEFAULT_REFRESH_PER_NIGHT,
    seed_pool: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Run the census on an asyncpg connection, inside its own READ ONLY
    transaction. Returns the JSON lines (dicts)."""
    async with conn.transaction(readonly=True):
        await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        if not await conn.fetchval(PRICE_CHECK_COLUMN_SQL):
            return [{"kind": "error", "error": "catalog_offers.price_checked_at is absent (migration 246)"}]
        now = _to_datetime(await conn.fetchval("SELECT now()"))
        raw = [dict(r) for r in await conn.fetch(census_sql())]
        pool_by_market = {str(r["market"]): int(r["seeds"]) for r in await conn.fetch(SEED_POOL_SQL)}
        coverage = dict(await conn.fetchrow(BACKFILL_COVERAGE_SQL))
        extras: List[Dict[str, Any]] = []
        if await conn.fetchval(DRAIN_JOBS_TABLE_SQL):
            jobs = [dict(r) for r in await conn.fetch(DRAIN_JOB_MARKETS_SQL)]
            explicit = [j for j in jobs if j["market"] != "(default)"]
            extras.append({
                "kind": "drain_job_markets",
                "jobs": sum(int(j["jobs"]) for j in jobs),
                "jobs_with_explicit_market": sum(int(j["jobs"]) for j in explicit),
                "by_market": {j["market"]: {"jobs": int(j["jobs"]), "hosts": int(j["hosts"])} for j in jobs},
            })
        edited = dict(await conn.fetchrow(EDITED_SEED_ROWS_SQL))
        extras.append({"kind": "employee_edit_dry_run",
                       "rows_with_hand_edited_seed": int(edited["rows_with_hand_edited_seed"] or 0),
                       "hand_edited_seeds": int(edited["hand_edited_seeds"] or 0)})
    limit = backfill_limit_default()
    env_limit = os.getenv("CATALOG_TRUST_BACKFILL_LIMIT")
    backfill = {
        "catalog_products": int(coverage["catalog_products"]),
        "trust_rows": int(coverage["trust_rows"]),
        "crawl_fresh_rows": int(coverage["crawl_fresh_rows"]),
        "oldest_trust_write": coverage["oldest_trust_write"],
        "backfill_limit_default": limit,
        "backfill_limit_env_in_this_job": env_limit,
        "over_default_limit": int(coverage["catalog_products"]) > limit,
    }
    classified = [classify(r, now=now, max_age_days=max_age_days) for r in raw]
    return summarize(classified, now=now, max_age_days=max_age_days,
                     refresh_per_night=refresh_per_night, seed_pool=seed_pool,
                     seed_pool_by_market=pool_by_market, backfill=backfill, extras=extras)


async def _main(argv: List[str]) -> int:
    import asyncpg

    parser = argparse.ArgumentParser(description="read-only CRAWL_FRESH census")
    parser.add_argument("--days", type=float, default=DEFAULT_DAYS)
    parser.add_argument("--refresh-per-night", type=int, default=DEFAULT_REFRESH_PER_NIGHT)
    parser.add_argument("--seed-pool", type=int, default=None)
    args = parser.parse_args(argv)
    if not (args.days > 0 and math.isfinite(args.days)):
        parser.error("--days must be a positive number")
    if args.refresh_per_night < 0 or (args.seed_pool is not None and args.seed_pool <= 0):
        parser.error("--refresh-per-night must be >= 0 and --seed-pool > 0")
    url = (os.getenv("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    if not url:
        print(json.dumps({"kind": "error", "error": "DATABASE_URL is not set"}))
        return 2
    conn = await asyncpg.connect(url)
    try:
        lines = await collect(conn, max_age_days=args.days, refresh_per_night=args.refresh_per_night,
                              seed_pool=args.seed_pool)
    finally:
        await conn.close()
    for line in lines:
        print(json.dumps(line, sort_keys=True, default=str), flush=True)
    return 2 if any(line.get("kind") == "error" for line in lines) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
