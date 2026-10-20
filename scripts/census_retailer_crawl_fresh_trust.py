#!/usr/bin/env python3
"""READ-ONLY census: what CRAWL_FRESH would move, now and in steady state.

The rule (services/catalog_trust_policy, switched by the upserter's
CATALOG_TRUST_CRAWL_FRESH_ENABLED, default off): a drain-lane storefront crawl
row -- source_system `catalog_enrichment_agent_v1`, product_key `ext:...`, i.e.
the retailer listings (`ext:retailer:...`) AND the brand-official / curated
canonicals -- is exempt from the identity COVERAGE gates
(IDENTITY_CONFIDENCE_NULL, IDENTITY_LIVE_READ_DISABLED, the silent
identity-'unknown' shadow) while at least one of its live drain-lane offers
carries a listing price read (`catalog_offers.price_checked_at`, migration 246)
no older than N days and links to a plain http(s) listing URL. Review,
conflict, non-canonical and every blocked cause still win, and a PDP known not
to render is never promoted.

It answers, BEFORE the switch flips, from the STORED catalog_row_trust rows (the
6h backfill's last pass) plus one read of the offers, split by LANE (`retailer`
= ext:retailer:, otherwise the row's enrichment_meta.source_role, e.g.
`brand_official`; `unlabelled` when it has none):

  * summary         -- would flip shadow -> public at N, and at N = 1/3/7/14;
  * by crawl age    -- newest qualifying price read: <=1d, <=3d, <=7d, <=14d,
                       >14d, NULL (never read);
  * by market       -- the markets of each row's fresh drain-lane offers;
  * by host         -- catalog_products.source_domain;
  * still blocked   -- fresh rows a reason the rule never lifts keeps out
                       (review / conflict / non-canonical), and fresh rows whose
                       PDP would not render;
  * projection      -- the next 7 days, pure arithmetic over the rows read:
      no_reread         nothing re-reads anything: each row drops out the day
                        its read passes N;
      refresh_pro_rata  the paced external-referral refresh re-reads
                        --refresh-per-night seeds a night (default 1,300) spread
                        pro rata over --seed-pool (default: the active US seeds,
                        counted here); expected values;
      drain_weekly      the drain re-crawls every listing weekly: every row with
                        a qualifying live offer is re-stamped within 7 days,
                        evenly; expected values.
    Each with its steady state, so the switch is not flipped on a post-catch-up
    peak.

"Would flip" = stored decision 'shadow', no review/conflict/non-canonical
reason and identity not review_required (so coverage is the only thing
shadowing it), crawl-fresh at N, renderable. It is a census of the stored
decision, so a row whose inputs moved since the last backfill pass is counted
on its stored verdict.

ROUND 2 additions: `backfill_coverage` (catalog_products vs the backfill cron's
CATALOG_TRUST_BACKFILL_LIMIT default -- above it, rows the oldest-first pass
never reaches keep their verdict); every summary, market and host line also
carries the PER-MARKET rule (`would_flip_per_market_rule`, see
SERVED_MARKET_CURRENCY_SQL) next to the max-any rule, the flips owed to an
out-of-stock-only fresh offer, and projections for both rules.

SAFETY. One READ ONLY transaction with SET LOCAL statement_timeout; it writes
nothing and creates nothing: one row query plus three aggregates. Prints JSON
lines only. It carries no at-sign so it can be passed inline to
scripts/ops/run_oneoff_job.sh (see its header), and it needs no crawl subnet
(it reads the database only):

    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/census_retailer_crawl_fresh_trust.py)"
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/census_retailer_crawl_fresh_trust.py)" --days 3

Self-contained on purpose: the deployed image may predate the rule, so the
offer predicate below is a COPY of services/catalog_row_trust_upserter's, the
row predicate a copy of catalog_trust_policy.is_crawl_listing and the freshness
test a copy of catalog_trust_policy.is_crawl_fresh.
tests/test_crawl_fresh_trust_postgres.py pins every copy to its original. Only
`seed_route_resolves_sql` (services/pdp_renderability, in every image since
2026-07) is imported from the image.
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

# COPIES of services/catalog_row_trust_upserter's fragments (pinned by test).
OFFER_LINK_SQL = (
    "btrim(coalesce(co.offer_payload->>'destination_url', co.source_ref),"
    r" E' \t\n\r\f\v') ~ '^https?://\S+$'"
)
OFFER_NOT_AFFILIATE_SQL = (
    "coalesce(co.offer_payload->>'validated_at', '') NOT LIKE 'affiliate_feed:%'"
)
# The upserter's statement minus `price_checked_at IS NOT NULL`, so a row with
# live offers but no read on record still reports its markets; max() ignores
# NULL exactly as that conjunct does.
OFFER_CONJUNCTS_SQL = (
    f"co.source_system = '{CRAWL_SOURCE_SYSTEM}'\n"
    "           AND co.suppressed_at IS NULL\n"
    "           AND coalesce(co.merchant_effective_price, co.list_price) > 0\n"
    f"           AND {OFFER_LINK_SQL}\n"
    f"           AND {OFFER_NOT_AFFILIATE_SQL}"
)

# ROUND 2, the PER-MARKET rule (what the next policy revision will ship): the
# qualifying offer must ALSO be
#   * declared for a SERVED market and priced in that market's currency
#     (config/market_registry.json served markets; region_pricing's currencies);
#   * not explicitly out of stock (offer_buyability.OFFER_UNAVAILABLE_AVAILABILITIES);
#   * not dated by an employee price edit (offer_payload.price_read_source);
#   * on a variant-scoped row (one with a live drain-lane variant offer), a
#     VARIANT offer: the refresh dates `<pk>::canonical` even when it skipped the
#     variants, so a canonical stamp alone proves nothing about them;
# and the read must not be dated in the future (0 <= age <= N).
SERVED_MARKET_CURRENCY_SQL = (
    "(upper(btrim(coalesce(co.market, ''))), upper(btrim(coalesce(co.currency, ''))))"
    " IN (('SG', 'SGD'), ('US', 'USD'))"
)
IN_STOCK_SQL = (
    "lower(btrim(coalesce(co.availability, '')))"
    " NOT IN ('out_of_stock', 'outofstock', 'sold_out', 'soldout', 'unavailable')"
)
NOT_EMPLOYEE_EDIT_SQL = "coalesce(co.offer_payload->>'price_read_source', '') <> 'employee_edit'"
CANONICAL_SKU_SUFFIX = "::canonical"

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

STILL_BLOCKING = {
    "review_required": "IDENTITY_REVIEW_REQUIRED_LIVE_READ",
    "conflict": "IDENTITY_CONFLICT",
    "non_canonical": "NON_CANONICAL_DUPLICATE",
}
AGE_BUCKETS = (("le_1d", 1.0), ("le_3d", 3.0), ("le_7d", 7.0), ("le_14d", 14.0))
DAY_SECONDS = 24 * 60 * 60
ALL = "all"


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
       rd.last_strict_read_at,
       rd.strict_market_reads
  FROM catalog_products cp
  LEFT JOIN catalog_row_trust crt
    ON crt.subject_type = 'product'
   AND crt.subject_key = cp.product_key
  CROSS JOIN LATERAL (
    SELECT EXISTS (
             SELECT 1 FROM catalog_offers cv
              WHERE cv.product_key = cp.product_key
                AND cv.sku_key IS DISTINCT FROM cp.product_key || '{CANONICAL_SKU_SUFFIX}'
                AND cv.source_system = '{CRAWL_SOURCE_SYSTEM}'
                AND cv.suppressed_at IS NULL
           ) AS variant_scoped
  ) vx
  LEFT JOIN LATERAL (
    SELECT max(m.last_read) AS last_price_read_at,
           jsonb_object_agg(m.market, m.last_read) AS market_reads,
           max(m.last_in_stock) AS last_in_stock_read_at,
           max(m.last_strict) AS last_strict_read_at,
           jsonb_object_agg(m.market, m.last_strict) FILTER (WHERE m.last_strict IS NOT NULL)
             AS strict_market_reads
      FROM (
        SELECT coalesce(nullif(upper(btrim(co.market)), ''), '(none)') AS market,
               max(co.price_checked_at) AS last_read,
               max(co.price_checked_at) FILTER (WHERE {IN_STOCK_SQL}) AS last_in_stock,
               max(co.price_checked_at) FILTER (
                 WHERE {SERVED_MARKET_CURRENCY_SQL}
                   AND {IN_STOCK_SQL}
                   AND {NOT_EMPLOYEE_EDIT_SQL}
                   AND (NOT vx.variant_scoped
                        OR co.sku_key IS DISTINCT FROM cp.product_key || '{CANONICAL_SKU_SUFFIX}')
               ) AS last_strict
          FROM catalog_offers co
         WHERE co.product_key = cp.product_key
           AND {OFFER_CONJUNCTS_SQL}
         GROUP BY 1
      ) m
  ) rd ON TRUE
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
    """COPY of catalog_trust_policy.is_crawl_fresh: inclusive bound, a stamp
    ahead of `now` is fresh, no read is not fresh."""
    ts = _to_datetime(last_read)
    if ts is None:
        return False
    return (now - ts).total_seconds() <= float(max_age_days) * DAY_SECONDS


def is_fresh_strict(last_read: Any, *, now: datetime, max_age_days: float) -> bool:
    """The round-2 freshness test: as is_fresh, but a stamp dated in the FUTURE
    is stale, as relgraph_freshness_refresh treats it (0 <= age <= N)."""
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
    strict_markets = _market_reads(row.get("strict_market_reads"))
    fresh_strict = is_fresh_strict(row.get("last_strict_read_at"), now=now, max_age_days=max_age_days)
    would_flip = coverage_only and fresh and renderable
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
        # Round 2: the per-market rule (see SERVED_MARKET_CURRENCY_SQL).
        # a future-dated stamp is stale under this rule, so it never ages in
        "strict_age_days": _non_negative(age_days(row.get("last_strict_read_at"), now=now)),
        "would_flip_strict": coverage_only and fresh_strict and renderable,
        # flips under the max-any rule only because of an out-of-stock read
        "would_flip_oos_only": would_flip and not is_fresh(
            row.get("last_in_stock_read_at"), now=now, max_age_days=max_age_days),
        "strict_fresh_markets": sorted(
            m for m, ts in strict_markets.items()
            if is_fresh_strict(ts, now=now, max_age_days=max_age_days)
        ),
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
    if backfill is not None:
        lines.append({"kind": "backfill_coverage", **backfill})

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
                     seed_pool_by_market=pool_by_market, backfill=backfill)


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
