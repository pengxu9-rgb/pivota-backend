"""How stale are the external seeds we SERVE? A read-only census, one job, three statements.

THE CLOCK. Per active seed, the age of the last SUCCESSFUL ORIGIN READ: `last_crawled_at`
(migration 202). Only `_refresh_external_seed_by_id` writes it, and only after a fetch that
reached the origin AND read the product we serve (`_read_the_served_product`); a cached-snapshot
fallback keeps the old value. It is not `last_crawl_attempt_at` (stamped on every outcome, it
orders the queue) and not `updated_at` (any writer bumps it).

  <7d | 7-30d | 30-90d | >90d | never

"never" is split by `created_at`, because a seed made by a crawl in the last week was read
at creation even though no refresh has read it since.

SERVED means the row passes the seed lane's own filters, imported from the running image rather
than re-spelled here:
  quarantine   services.external_seed_search.build_seed_quarantine_anti_join()
  suppression  services.external_seed_search.SEED_SUPPRESSED_PRODUCT_ANTI_JOIN
  currency     price_currency == seed_serving_currency(the seed's own market partition)
  stock        services.external_seed_stock.seed_stock(...).product is not False
Not modelled: the referral runtime gate (destination_dead and friends). The gate can still refuse
a "served" row at request time, so these counts are an upper bound on what buyers see.

SECTIONS
  A  read-age buckets: all active rows, served rows, and served rows split attached/unattached.
     The refresh queue before this PR took attached rows only, so an unattached served seed was
     never a candidate.
  B  the top 30 hosts by served seeds: read-age buckets, the share of reads under 3 days, and
     whether the host's latest attempts failed.
  C  hosts whose last N attempts ALL failed, for N = 3, 5 and 10. Each seed keeps only its latest
     attempt, so a host's last N attempts are its N most recently attempted seeds. An attempt
     "failed" when it did not advance `last_crawled_at`. Also: how many of those hosts gave ANY
     client a conclusive answer in the last 7 days (the sweep reads products.json from the same
     egress). This sizes the chronically-unreachable rule.
  D  per-host throughput of the last three refresh runs, from attempt timestamps. The loop was
     serial, so the gap before a row's stamp is what that row cost. Summed per host, that gap is
     the host's share of the budget.
  E  the canonical-chain offers of served, attached seeds: `catalog_offers.updated_at` age, and how
     often the seed was read AFTER the offer was last written. The refresh cannot heal those offers.
     `sync_offer_for_seed` writes only a mirror product's offer (09-27: no_mirror_product
     2,220/2,220). This is the drift PIVOTA-Agent #2215 surfaced.

SAFETY. The transaction is READ ONLY with `SET LOCAL statement_timeout = '30s'`. It runs three
statements, one after another, and none of them re-executes. pivota-pg has 2 vCPU and also serves
live search, so START IT ONLY WHILE CPU IS UNDER 35%, and never during the 05:15-06:15Z refresh
or while an index build runs:

    TOKEN=$(gcloud auth print-access-token)
    curl -s -G -H "Authorization: Bearer $TOKEN" \\
      https://monitoring.googleapis.com/v3/projects/pivota-prod/timeSeries \\
      --data-urlencode 'filter=metric.type="cloudsql.googleapis.com/database/cpu/utilization" AND resource.labels.database_id="pivota-prod:pivota-pg"' \\
      --data-urlencode "interval.startTime=$(date -u -v-5M +%Y-%m-%dT%H:%M:%SZ)" \\
      --data-urlencode "interval.endTime=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

RUN IT (it needs no crawl subnet, because it fetches nothing):

    # before this file is in the deployed image -- inline, the program must stay free of the
    # at-sign (run_oneoff_job.sh picks gcloud's --args delimiter from a fixed list):
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/ops/external_seed_freshness_census.py)"
    # once it is:
    bash scripts/ops/run_oneoff_job.sh scripts/ops/external_seed_freshness_census.py

Output: a text report, then one `CENSUS_JSON {...}` line. The prefix keeps the line as
textPayload, where a bare JSON line would land in jsonPayload and read back blank.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# `python scripts/ops/<this>.py` puts scripts/ops/ on sys.path, and `python -c` has no __file__.
# /app is the image's repo root.
try:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except NameError:
    _REPO_ROOT = "/app"
sys.path.insert(0, _REPO_ROOT)

AGE_BUCKETS: Tuple[str, ...] = ("<7d", "7-30d", "30-90d", ">90d", "never")
NEVER_SPLIT: Tuple[str, ...] = ("never:created<7d", "never:created>=7d")
TOP_HOSTS = 30
FAIL_STREAK_NS: Tuple[int, ...] = (3, 5, 10)
# Two attempt stamps further apart than this belong to different runs. The nightly loop stamps a
# row every ~1s, and a gap of a single row is bounded by CRAWL_MAX_BACKOFF_SECONDS (300s).
RUN_GAP = timedelta(minutes=30)
RUNS_TO_PROFILE = 3
# A row's measured cost is its gap to the previous stamp in the same run. A gap past this is a
# pause (for example a host hold), not the row itself. Capped so one pause cannot dominate a host.
ROW_GAP_CAP_SECONDS = 330.0
# An origin read and its attempt are stamped by the same UPDATE, so they are equal in practice.
# This slack absorbs clock rounding only.
READ_ON_ATTEMPT_SLACK = timedelta(seconds=2)


TIMESTAMP_FIELDS = ("last_crawled_at", "last_crawl_attempt_at", "destination_checked_at", "created_at")


def as_utc(value: Any) -> Optional[datetime]:
    """Aware UTC. `catalog_offers.updated_at` is a naive TIMESTAMP holding UTC; the seed clocks
    are TIMESTAMPTZ. Comparing the two raw raises TypeError."""
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _normalised(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(r, **{f: as_utc(r.get(f)) for f in TIMESTAMP_FIELDS if f in r}) for r in rows]


def age_bucket(last_read: Optional[datetime], now: datetime) -> str:
    if last_read is None:
        return "never"
    age = now - last_read
    if age < timedelta(days=7):
        return "<7d"
    if age < timedelta(days=30):
        return "7-30d"
    if age < timedelta(days=90):
        return "30-90d"
    return ">90d"


def never_split(created_at: Optional[datetime], now: datetime) -> str:
    if created_at is not None and now - created_at < timedelta(days=7):
        return "never:created<7d"
    return "never:created>=7d"


def attempt_read_the_origin(row: Dict[str, Any]) -> Optional[bool]:
    """Did the row's LATEST attempt read the origin? None when it was never attempted."""
    attempted = row.get("last_crawl_attempt_at")
    if attempted is None:
        return None
    read = row.get("last_crawled_at")
    return read is not None and read >= attempted - READ_ON_ATTEMPT_SLACK


def host_last_n_failed(rows: Sequence[Dict[str, Any]], n: int) -> bool:
    """True when the host's n most recent attempts all failed. A host with fewer than n
    attempted seeds does not qualify: too few observations to call it chronic."""
    attempted = [r for r in rows if r.get("last_crawl_attempt_at") is not None]
    if len(attempted) < n:
        return False
    attempted.sort(key=lambda r: r["last_crawl_attempt_at"], reverse=True)
    return not any(attempt_read_the_origin(r) for r in attempted[:n])


def split_runs(stamps: Sequence[Tuple[datetime, str, bool]]) -> List[List[Tuple[datetime, str, bool]]]:
    """Cluster (attempt_at, host, read_ok) stamps into runs, ordered oldest first."""
    ordered = sorted(stamps, key=lambda s: s[0])
    runs: List[List[Tuple[datetime, str, bool]]] = []
    for stamp in ordered:
        if runs and stamp[0] - runs[-1][-1][0] <= RUN_GAP:
            runs[-1].append(stamp)
        else:
            runs.append([stamp])
    return runs


def profile_run(run: Sequence[Tuple[datetime, str, bool]]) -> Dict[str, Any]:
    """Per-host cost of one serial run: sum of the gap before each of the host's stamps."""
    per_host: Dict[str, Dict[str, float]] = defaultdict(lambda: {"rows": 0, "reads": 0, "seconds": 0.0})
    for index, (at, host, ok) in enumerate(run):
        gap = (at - run[index - 1][0]).total_seconds() if index else 0.0
        bucket = per_host[host]
        bucket["rows"] += 1
        bucket["reads"] += 1 if ok else 0
        bucket["seconds"] += min(max(gap, 0.0), ROW_GAP_CAP_SECONDS)
    span = (run[-1][0] - run[0][0]).total_seconds() if run else 0.0
    hosts = sorted(per_host.items(), key=lambda kv: -kv[1]["seconds"])
    return {
        "started": run[0][0].isoformat() if run else None,
        "rows": len(run),
        "reads": sum(1 for _at, _h, ok in run if ok),
        "span_seconds": round(span, 1),
        "rows_per_second": round(len(run) / span, 3) if span else None,
        "hosts": len(per_host),
        "top_hosts_by_seconds": [
            {
                "host": host,
                "rows": int(v["rows"]),
                "reads": int(v["reads"]),
                "seconds": round(v["seconds"], 1),
                "seconds_per_row": round(v["seconds"] / v["rows"], 2) if v["rows"] else None,
            }
            for host, v in hosts[:25]
        ],
    }


def _variant_container(is_list: Any, availabilities: Any) -> Optional[List[Dict[str, Any]]]:
    if not is_list:
        return None
    values = availabilities if isinstance(availabilities, list) else json.loads(availabilities or "[]")
    return [{"availability": value} for value in values]


def compact_seed_data(row: Dict[str, Any]) -> Dict[str, Any]:
    """The four fields `seed_stock` reads, rebuilt from the census row's JSON projections.

    Shipping all of seed_data to the job would move hundreds of MB off a 2-vCPU primary.
    `seed_stock` reads only `availability`, `variants[*].availability` and the same two under
    `snapshot`, so those are projected in SQL and reassembled here in the same shape.
    """
    def _load(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value

    seed_data: Dict[str, Any] = {"availability": _load(row.get("sd_availability"))}
    top = _variant_container(row.get("top_variants_is_list"), _load(row.get("top_variant_availability")))
    if top is not None:
        seed_data["variants"] = top
    if row.get("snapshot_is_object"):
        snapshot: Dict[str, Any] = {"availability": _load(row.get("snap_availability"))}
        snap_variants = _variant_container(
            row.get("snap_variants_is_list"), _load(row.get("snap_variant_availability"))
        )
        if snap_variants is not None:
            snapshot["variants"] = snap_variants
        seed_data["snapshot"] = snapshot
    return seed_data


def classify(row: Dict[str, Any], *, serving_currency, seed_stock, host_of) -> Dict[str, Any]:
    """Pure: the census facts for one seed row."""
    market = row.get("market")
    expected_currency = serving_currency(market)
    currency = str(row.get("price_currency") or "").strip().upper()
    currency_ok = expected_currency is not None and currency == expected_currency
    stock = seed_stock({"availability": row.get("availability")}, compact_seed_data(row))
    stock_ok = stock.product is not False
    passes_quarantine = bool(row.get("passes_quarantine"))
    passes_suppression = bool(row.get("passes_suppression"))
    return {
        "host": host_of(str(row.get("destination_url") or "")) or "(none)",
        "served": passes_quarantine and passes_suppression and currency_ok and stock_ok,
        "attached": bool(row.get("attached")),
        "fails": {
            "quarantine": not passes_quarantine,
            "suppression": not passes_suppression,
            "currency": not currency_ok,
            "stock": not stock_ok,
        },
    }


SEED_SQL_TEMPLATE = """
SELECT
  id, market, destination_url, price_currency, availability,
  attached_product_key IS NOT NULL AS attached,
  last_crawled_at, last_crawl_attempt_at, destination_checked_at, created_at,
  (TRUE {quarantine}) AS passes_quarantine,
  (TRUE {suppression}) AS passes_suppression,
  seed_data -> 'availability' AS sd_availability,
  jsonb_typeof(seed_data -> 'variants') = 'array' AS top_variants_is_list,
  (SELECT coalesce(jsonb_agg(v -> 'availability'), '[]'::jsonb)
     FROM jsonb_array_elements(CASE WHEN jsonb_typeof(seed_data -> 'variants') = 'array'
                                    THEN seed_data -> 'variants' ELSE '[]'::jsonb END) v
    WHERE jsonb_typeof(v) = 'object') AS top_variant_availability,
  jsonb_typeof(seed_data -> 'snapshot') = 'object' AS snapshot_is_object,
  seed_data -> 'snapshot' -> 'availability' AS snap_availability,
  jsonb_typeof(seed_data -> 'snapshot' -> 'variants') = 'array' AS snap_variants_is_list,
  (SELECT coalesce(jsonb_agg(v -> 'availability'), '[]'::jsonb)
     FROM jsonb_array_elements(CASE WHEN jsonb_typeof(seed_data -> 'snapshot' -> 'variants') = 'array'
                                    THEN seed_data -> 'snapshot' -> 'variants' ELSE '[]'::jsonb END) v
    WHERE jsonb_typeof(v) = 'object') AS snap_variant_availability
FROM external_product_seeds
WHERE status = 'active'
"""

# Attempt stamps for the throughput profile. INACTIVE rows too: a row the sweep retired after the
# refresh read it still spent that run's time.
ATTEMPTS_SQL = """
SELECT destination_url, last_crawl_attempt_at, last_crawled_at
FROM external_product_seeds
WHERE last_crawl_attempt_at >= NOW() - INTERVAL '4 days'
"""

# One row per served attached seed that has canonical-chain offers: the newest offer write on
# its product. The id list is bound as an array, never interpolated.
OFFERS_SQL = """
SELECT s.id, max(o.updated_at) AS offer_updated_at
FROM external_product_seeds s
JOIN catalog_offers o ON o.product_key = s.attached_product_key
WHERE s.id = ANY($1::text[])
GROUP BY s.id
"""


def build_seed_sql() -> str:
    from services.external_seed_search import (
        SEED_SUPPRESSED_PRODUCT_ANTI_JOIN,
        build_seed_quarantine_anti_join,
    )

    return SEED_SQL_TEMPLATE.format(
        quarantine=build_seed_quarantine_anti_join(),
        suppression=SEED_SUPPRESSED_PRODUCT_ANTI_JOIN,
    )


def _buckets(rows: Iterable[Dict[str, Any]], now: datetime) -> Dict[str, int]:
    counts: Counter = Counter()
    for row in rows:
        bucket = age_bucket(row.get("last_crawled_at"), now)
        counts[bucket] += 1
        if bucket == "never":
            counts[never_split(row.get("created_at"), now)] += 1
    return {key: int(counts.get(key, 0)) for key in AGE_BUCKETS + NEVER_SPLIT}


def summarise(
    rows: Sequence[Dict[str, Any]],
    attempts: Sequence[Dict[str, Any]],
    offers: Dict[str, Optional[datetime]],
    *,
    now: datetime,
    serving_currency,
    seed_stock,
    host_of,
) -> Dict[str, Any]:
    """Every census number, from the fetched rows. Pure, so the test runs it on fixtures."""
    now = as_utc(now)
    rows = _normalised(rows)
    attempts = _normalised(attempts)
    offers = {seed_id: as_utc(at) for seed_id, at in offers.items()}
    facts = [classify(r, serving_currency=serving_currency, seed_stock=seed_stock, host_of=host_of) for r in rows]
    enriched = [dict(r, **f) for r, f in zip(rows, facts)]
    served = [r for r in enriched if r["served"]]

    fails: Counter = Counter()
    for row in enriched:
        for name, failed in row["fails"].items():
            if failed:
                fails[name] += 1

    section_a = {
        "active": len(enriched),
        "served": len(served),
        "not_served_because": dict(fails),
        "all_active": _buckets(enriched, now),
        "served_all": _buckets(served, now),
        "served_attached": _buckets([r for r in served if r["attached"]], now),
        "served_unattached": _buckets([r for r in served if not r["attached"]], now),
        "served_read_within_3d": sum(
            1 for r in served
            if r.get("last_crawled_at") is not None and now - r["last_crawled_at"] < timedelta(days=3)
        ),
    }

    by_host: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        by_host[row["host"]].append(row)

    def _host_view(host: str) -> Dict[str, Any]:
        host_rows = by_host[host]
        host_served = [r for r in host_rows if r["served"]]
        latest = max((r["last_crawl_attempt_at"] for r in host_rows if r.get("last_crawl_attempt_at")), default=None)
        last_read = max((r["last_crawled_at"] for r in host_rows if r.get("last_crawled_at")), default=None)
        return {
            "host": host,
            "active": len(host_rows),
            "served": len(host_served),
            "served_buckets": _buckets(host_served, now),
            "served_read_within_3d": sum(
                1 for r in host_served
                if r.get("last_crawled_at") is not None and now - r["last_crawled_at"] < timedelta(days=3)
            ),
            "last_attempt": latest.isoformat() if latest else None,
            "last_origin_read": last_read.isoformat() if last_read else None,
            "last_5_attempts_failed": host_last_n_failed(host_rows, 5),
        }

    top_hosts = sorted(by_host, key=lambda h: (-sum(1 for r in by_host[h] if r["served"]), h))[:TOP_HOSTS]
    section_b = [_host_view(h) for h in top_hosts]

    week_ago = now - timedelta(days=7)
    section_c: Dict[str, Any] = {}
    for n in FAIL_STREAK_NS:
        failing = [h for h, host_rows in by_host.items() if host_last_n_failed(host_rows, n)]
        no_read_7d = [
            h for h in failing
            if not any(r.get("last_crawled_at") and r["last_crawled_at"] >= week_ago for r in by_host[h])
        ]
        answered_7d = [
            h for h in failing
            if any(r.get("destination_checked_at") and r["destination_checked_at"] >= week_ago for r in by_host[h])
        ]
        section_c[f"last_{n}"] = {
            "hosts": len(failing),
            "active_seeds": sum(len(by_host[h]) for h in failing),
            "served_seeds": sum(sum(1 for r in by_host[h] if r["served"]) for h in failing),
            "hosts_no_origin_read_7d": len(no_read_7d),
            "served_seeds_no_origin_read_7d": sum(sum(1 for r in by_host[h] if r["served"]) for h in no_read_7d),
            "hosts_any_client_answered_7d": len(answered_7d),
            "top": sorted(
                (
                    {
                        "host": h,
                        "served": sum(1 for r in by_host[h] if r["served"]),
                        "active": len(by_host[h]),
                        "no_origin_read_7d": h in no_read_7d,
                        "any_client_answered_7d": h in answered_7d,
                    }
                    for h in failing
                ),
                key=lambda d: -d["served"],
            )[:25],
        }

    stamps = [
        (
            a["last_crawl_attempt_at"],
            host_of(str(a.get("destination_url") or "")) or "(none)",
            bool(attempt_read_the_origin(a)),
        )
        for a in attempts
        if a.get("last_crawl_attempt_at") is not None
    ]
    runs = split_runs(stamps)
    # Only runs big enough to be the nightly job; an operator's one-row refresh is its own "run".
    nightly = [run for run in runs if len(run) >= 200][-RUNS_TO_PROFILE:]
    section_d = [profile_run(run) for run in nightly]

    offer_buckets: Counter = Counter()
    seed_read_after_offer = 0
    for row in served:
        if not row["attached"]:
            continue
        if row["id"] not in offers:
            offer_buckets["no_offer"] += 1
            continue
        offer_at = offers[row["id"]]
        offer_buckets[age_bucket(offer_at, now)] += 1
        if offer_at is not None and row.get("last_crawled_at") and row["last_crawled_at"] > offer_at:
            seed_read_after_offer += 1
    section_e = {
        "served_attached": sum(1 for r in served if r["attached"]),
        "offer_updated_at_buckets": {k: int(offer_buckets.get(k, 0)) for k in AGE_BUCKETS + ("no_offer",)},
        "seed_read_after_offer_write": seed_read_after_offer,
    }

    return {
        "generated_at": now.isoformat(),
        "A_read_age": section_a,
        "B_top_hosts": section_b,
        "C_failing_hosts": section_c,
        "D_run_throughput": section_d,
        "E_canonical_offers": section_e,
    }


def render(summary: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    a = summary["A_read_age"]
    lines.append(f"== A. last successful origin read (generated {summary['generated_at']})")
    lines.append(f"active={a['active']} served={a['served']} not_served_because={a['not_served_because']}")
    header = f"{'':<18}" + " ".join(f"{b:>18}" for b in AGE_BUCKETS + NEVER_SPLIT)
    lines.append(header)
    for key in ("all_active", "served_all", "served_attached", "served_unattached"):
        lines.append(f"{key:<18}" + " ".join(f"{a[key][b]:>18}" for b in AGE_BUCKETS + NEVER_SPLIT))
    lines.append(f"served read within 3d: {a['served_read_within_3d']}")
    lines.append("== B. top hosts by served seeds")
    for h in summary["B_top_hosts"]:
        sb = h["served_buckets"]
        lines.append(
            f"{h['host']:<34} served={h['served']:>5} active={h['active']:>5} "
            f"<3d={h['served_read_within_3d']:>5} " + " ".join(f"{b}={sb[b]}" for b in AGE_BUCKETS)
            + f" last5failed={h['last_5_attempts_failed']} last_read={h['last_origin_read']}"
        )
    lines.append("== C. hosts whose last N attempts all failed")
    for key, c in summary["C_failing_hosts"].items():
        lines.append(
            f"{key}: hosts={c['hosts']} active={c['active_seeds']} served={c['served_seeds']} "
            f"| no origin read 7d: hosts={c['hosts_no_origin_read_7d']} served={c['served_seeds_no_origin_read_7d']} "
            f"| any client answered 7d: hosts={c['hosts_any_client_answered_7d']}"
        )
        for t in c["top"][:15]:
            lines.append(f"    {t['host']:<34} served={t['served']:>5} active={t['active']:>5} "
                         f"no_read_7d={t['no_origin_read_7d']} answered_7d={t['any_client_answered_7d']}")
    lines.append("== D. refresh run throughput (serial loop; gap before each stamp = that row's cost)")
    for run in summary["D_run_throughput"]:
        lines.append(f"run {run['started']}: rows={run['rows']} reads={run['reads']} span={run['span_seconds']}s "
                     f"rows/s={run['rows_per_second']} hosts={run['hosts']}")
        for t in run["top_hosts_by_seconds"][:15]:
            lines.append(f"    {t['host']:<34} rows={t['rows']:>5} reads={t['reads']:>5} "
                         f"seconds={t['seconds']:>8} s/row={t['seconds_per_row']}")
    e = summary["E_canonical_offers"]
    lines.append("== E. canonical-chain offers of served attached seeds (catalog_offers.updated_at)")
    lines.append(f"served_attached={e['served_attached']} buckets={e['offer_updated_at_buckets']} "
                 f"seed_read_after_offer_write={e['seed_read_after_offer_write']}")
    return lines


async def collect(conn: Any) -> Dict[str, Any]:
    """Run the census on an open asyncpg connection: one READ ONLY transaction, then summarise."""
    from services.crawl_politeness import host_of
    from services.external_seed_search import seed_serving_currency
    from services.external_seed_stock import seed_stock

    async with conn.transaction(readonly=True):
        await conn.execute("SET LOCAL statement_timeout = '30s'")
        now = await conn.fetchval("SELECT NOW()")
        rows = [dict(r) for r in await conn.fetch(build_seed_sql())]
        attempts = [dict(r) for r in await conn.fetch(ATTEMPTS_SQL)]
        served_attached_ids = [
            r["id"] for r in rows
            if r.get("attached")
            and classify(r, serving_currency=seed_serving_currency, seed_stock=seed_stock, host_of=host_of)["served"]
        ]
        offers: Dict[str, Optional[datetime]] = {}
        try:
            async with conn.transaction():  # a savepoint: E failing must not void A-D
                for r in await conn.fetch(OFFERS_SQL, served_attached_ids):
                    offers[r["id"]] = r["offer_updated_at"]
        except Exception as exc:  # noqa: BLE001 - section E is optional
            print(f"section E skipped: {type(exc).__name__}: {str(exc)[:200]}")
    return summarise(
        rows, attempts, offers,
        now=now, serving_currency=seed_serving_currency, seed_stock=seed_stock, host_of=host_of,
    )


async def _main() -> int:
    import asyncpg

    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        summary = await collect(conn)
    finally:
        await conn.close()
    for line in render(summary):
        print(line)
    print("CENSUS_JSON " + json.dumps(summary, default=str, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
