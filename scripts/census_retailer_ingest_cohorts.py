#!/usr/bin/env python3
"""READ-ONLY census of the retailer-ingest cohorts the re-enqueue would re-crawl.

Per cohort (db.retailer_ingest.scope_key, the key the open-job index is built on), one JSON line:
  host, brand, jobs, newest status, lineage (eligible / open / no_done_job / <status>_lineage);
  last_done_at, last_applied_at and its age in hours;
  explicit_market (does the latest done job name options.market), ships_to_market (what that job's newest
  applied run checked: true / false / null), evidence_market and evidence_unproven (what the run's evidence
  shows, explicit or not), market + market_source (explicit first, then evidence, NEVER a default),
  market_unproven + unproven_reason, ships_to_market_false (the re-enqueue skips the cohort whatever its
  market), ships_unverified (an explicit-market storefront cohort whose run never checked shipping: queued
  without options.require_ships_to_market), cycle_hours (the latest crawling dry run -- clean or held --
  plus the latest applied apply).
Then one line per host and one total line: counts of each of the above, the cycle hours of the cohorts the
re-enqueue would re-crawl, those hours projected per week at the re-enqueue interval (--interval-hours,
default 120: each cohort crawls 168 / interval times a week at most), and the lane hours a week leaves
outside the drain's claim window and its one-hour lead (default 03:00-06:30, so 02:00-06:30).

OUTPUT. Every line is CENSUS_MARKER followed by one JSON object, so a Cloud Run job's stdout lands in
textPayload and is found by grepping the marker; strip the marker to parse the line.

SAFETY. One READ ONLY transaction (REPEATABLE READ, so both queries read one snapshot) with SET LOCAL
statement_timeout; it writes nothing and creates nothing. Prints JSON lines only. It carries no at-sign so
it can be passed inline to scripts/ops/run_oneoff_job.sh, and it needs no crawl subnet:

    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/census_retailer_ingest_cohorts.py)"
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/census_retailer_ingest_cohorts.py)" --hosts-only

Self-contained on purpose: the deployed image may predate the re-enqueue, so the evidence reduction, the
market rule and the lineage rule below are COPIES of services/retailer_ingest/weekly_reenqueue.py's,
pinned to it by tests/test_census_retailer_ingest_cohorts.py. Only db.retailer_ingest.scope_key (in every
image since migration 234) is imported from the image.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

if os.path.isdir("/app") and "/app" not in sys.path:
    sys.path.insert(0, "/app")

STATEMENT_TIMEOUT = "120s"
HOURS_PER_WEEK = 168
DEFAULT_INTERVAL_HOURS = 120
# COPIES (pinned by test): pipeline.INGEST_MARKETS and region_pricing's currency for each.
INGEST_MARKET_CURRENCY = {"US": "USD", "AU": "AUD", "JP": "JPY", "SG": "SGD"}
OPEN_STATUSES = ("queued", "apply_due", "held")
TERMINAL_AFTER_DONE = ("failed", "cancelled", "nothing")
SHIPS_TO_MARKET_FALSE = "ships_to_market_false"
SHIPS_UNVERIFIED = "ships_unverified"
CENSUS_MARKER = "retailer_ingest_cohort_census "
# The drain's default claim window (03:00-06:30) plus its one-hour claim lead.
DEFAULT_CLAIM_BLOCKED_MINUTES = 210 + 60

JOBS_SQL = """
    SELECT id, domain, brand, options, status, created_at, updated_at
    FROM retailer_ingest_jobs
"""

# Per job: its newest applied run in finish_run's shape (a COPY of weekly_reenqueue._FACTS_SQL's columns,
# pinned by test), when it finished, and the seconds of its latest crawling dry run and applied apply.
RUNS_SQL = """
    WITH applied AS (
        SELECT DISTINCT ON (r.job_id)
               r.job_id,
               jsonb_build_object('storefront', r.crawl -> 'storefront',
                              'presented_currency', r.crawl -> 'presented_currency') AS crawl,
           jsonb_build_object('storefront', r.checks -> 'storefront') AS checks,
           jsonb_build_object('rows', r.readback -> 'rows') AS readback,
               r.finished_at AS applied_at,
               EXTRACT(EPOCH FROM (r.finished_at - r.started_at)) AS apply_s
        FROM retailer_ingest_runs r
        WHERE r.stage = 'apply' AND r.outcome = 'applied' AND r.finished_at IS NOT NULL
        ORDER BY r.job_id, r.started_at DESC, r.id DESC
    ), crawled AS (
        SELECT DISTINCT ON (r.job_id)
               r.job_id, EXTRACT(EPOCH FROM (r.finished_at - r.started_at)) AS dry_run_s
        FROM retailer_ingest_runs r
        WHERE r.stage = 'dry_run' AND r.outcome IN ('clean', 'held') AND r.finished_at IS NOT NULL
        ORDER BY r.job_id, r.started_at DESC, r.id DESC
    )
    SELECT coalesce(a.job_id, c.job_id) AS job_id, a.crawl, a.checks, a.readback, a.applied_at, a.apply_s,
           c.dry_run_s
    FROM applied a FULL JOIN crawled c ON c.job_id = a.job_id
"""


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _options(value: Any) -> Dict[str, Any]:
    value = _json(value)
    return dict(value) if isinstance(value, dict) else {}


def _dict(value: Any) -> Dict[str, Any]:
    value = _json(value)
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def fold_host(domain: Any) -> str:
    host = str(domain or "").strip().lower()
    return host[4:] if host.startswith("www.") else host


def evidence_from_run(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """COPY of weekly_reenqueue.evidence_from_run."""
    if not run:
        return None
    crawl, checks, readback = _dict(run.get("crawl")), _dict(run.get("checks")), _dict(run.get("readback"))
    storefront = _dict(checks.get("storefront"))
    rows = readback.get("rows") if isinstance(readback.get("rows"), list) else []
    proven = 0
    for row in rows:
        row = row if isinstance(row, dict) else {}
        offers = _count(row.get("offers"))
        if (offers is not None and offers > 0 and _count(row.get("offers_in_currency")) == offers
                and _count(row.get("offers_in_market")) == offers):
            proven += 1
    return {"store_currency": _dict(crawl.get("storefront")).get("currency"),
            "presented": crawl.get("presented_currency"),
            "checked_market": storefront.get("market"),
            "ships_to_market": storefront.get("ships_to_market"),
            "readback_rows": len(rows), "readback_rows_proven": proven}


def evidence_market(evidence: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """COPY of weekly_reenqueue.evidence_market."""
    if not evidence:
        return None, "no_applied_run"
    checked = _norm(evidence.get("checked_market"))
    if checked not in INGEST_MARKET_CURRENCY:
        return None, "no_checked_market"
    store = _norm(evidence.get("store_currency"))
    if not store:
        return None, "no_store_currency"
    if INGEST_MARKET_CURRENCY[checked] != store:
        return None, "currency_market_mismatch"
    if evidence.get("ships_to_market") is not True:
        return None, "ships_to_market_unproven"
    presented = _dict(evidence.get("presented"))
    pages = presented.get("pages_by_currency")
    pages = {_norm(c): n for c, n in pages.items()} if isinstance(pages, dict) else {}
    seen = {c for c, n in pages.items() if (_count(n) or 0) > 0}
    if not seen:
        return None, "presented_currency_absent"
    if seen != {store} or presented.get("unproven_values") or presented.get("problems"):
        return None, "presented_currency_mismatch"
    rows, proven = int(evidence.get("readback_rows") or 0), int(evidence.get("readback_rows_proven") or 0)
    if rows < 1 or proven != rows:
        return None, "readback_unproven"
    return checked, None


def derive_market(options: Dict[str, Any], evidence: Optional[Dict[str, Any]]) -> Tuple[Optional[str], str, Optional[str]]:
    """COPY of weekly_reenqueue.derive_market."""
    explicit = options.get("market")
    if explicit not in (None, ""):
        market = _norm(explicit)
        if market in INGEST_MARKET_CURRENCY:
            return market, "explicit", None
        return None, "", "explicit_market_invalid"
    market, why = evidence_market(evidence)
    if market:
        return market, "evidence", None
    return None, "", why


def _at(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def lineage(jobs: List[Dict[str, Any]]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """COPY of weekly_reenqueue.lineage_reason's lineage half (no lease, which a census does not judge):
    ("open" | "no_done_job" | "<status>_lineage" | "eligible", latest done job). `jobs` oldest first."""
    done = [i for i, j in enumerate(jobs) if j.get("status") == "done"]
    latest = jobs[done[-1]] if done else None
    if any(j.get("status") in OPEN_STATUSES for j in jobs):
        return "open", latest
    if latest is None:
        return "no_done_job", None
    for job in reversed(jobs[done[-1] + 1:]):
        if job.get("status") in TERMINAL_AFTER_DONE:
            return f"{job['status']}_lineage", latest
    return "eligible", latest


def cohort_lines(jobs: List[Dict[str, Any]], runs: Dict[str, Dict[str, Any]], scope_key: Any,
                 now: datetime) -> List[Dict[str, Any]]:
    cohorts: Dict[str, List[Dict[str, Any]]] = {}
    for job in jobs:
        key = scope_key(str(job.get("domain") or ""), str(job.get("brand") or ""), _options(job.get("options")))
        cohorts.setdefault(key, []).append(job)
    out = []
    for key in sorted(cohorts):
        members = sorted(cohorts[key], key=lambda j: (_at(j["created_at"]), str(j.get("id"))))
        state, done = lineage(members)
        line: Dict[str, Any] = {"kind": "cohort", "cohort": key, "host": fold_host(members[-1].get("domain")),
                                "brand": members[-1].get("brand"), "jobs": len(members),
                                "newest_status": members[-1].get("status"), "lineage": state}
        applied = [_at(runs[j["id"]]["applied_at"]) for j in members
                   if (runs.get(j["id"]) or {}).get("applied_at") is not None]
        if applied:
            line["last_applied_at"] = max(applied).isoformat()
            line["applied_age_hours"] = round((now - max(applied)).total_seconds() / 3600.0, 1)
        if done is not None:
            options = _options(done.get("options"))
            run = runs.get(done["id"]) or {}
            # A job whose only run row is a dry run has no applied evidence (RUNS_SQL is a FULL JOIN).
            evidence = evidence_from_run(run) if run.get("applied_at") is not None else None
            ev_market, ev_why = evidence_market(evidence)
            market, source, why = derive_market(options, evidence)
            seconds = sum(float(run.get(k) or 0) for k in ("dry_run_s", "apply_s"))
            ships = (evidence or {}).get("ships_to_market")
            # COPY of weekly_reenqueue.plan_cohort's ships-to gate: a storefront cohort whose run never checked
            # shipping is queued without the gate. Only an explicit market can get here (evidence required true).
            unverified = (market is not None and ships is not True and ships is not False
                          and (options.get("source") or "storefront") == "storefront"
                          and not options.get("require_ships_to_market"))
            line.update({
                "last_done_job_id": done["id"], "last_done_at": _at(done.get("updated_at") or done["created_at"]).isoformat(),
                "explicit_market": options.get("market") not in (None, ""),
                "ships_to_market": ships if isinstance(ships, bool) else None,
                SHIPS_TO_MARKET_FALSE: ships is False, SHIPS_UNVERIFIED: unverified,
                "evidence_market": ev_market, "evidence_unproven": ev_why,
                "market": market, "market_source": source or None, "market_unproven": market is None,
                "unproven_reason": why, "cycle_hours": round(seconds / 3600.0, 3),
            })
        out.append(line)
    return out


def host_lines(cohorts: List[Dict[str, Any]], *, interval_hours: int = DEFAULT_INTERVAL_HOURS,
               claim_blocked_minutes: int = DEFAULT_CLAIM_BLOCKED_MINUTES) -> List[Dict[str, Any]]:
    hosts: Dict[str, List[Dict[str, Any]]] = {}
    for c in cohorts:
        hosts.setdefault(c["host"], []).append(c)

    def agg(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        done = [r for r in rows if r.get("last_done_job_id")]
        recrawl = [r for r in done if r["lineage"] in ("eligible", "open") and not r["market_unproven"]
                   and not r[SHIPS_TO_MARKET_FALSE]]
        cycle = round(sum(r["cycle_hours"] for r in recrawl), 2)
        return {
            "cohorts": len(rows), "done_cohorts": len(done),
            "last_done_at": max((r["last_done_at"] for r in done), default=None),
            "explicit_market": sum(1 for r in done if r["explicit_market"]),
            "evidence_market": sum(1 for r in done if r["market_source"] == "evidence"),
            "market_unproven": sum(1 for r in done if r["market_unproven"]),
            "unproven_reasons": dict(sorted(Counter(r["unproven_reason"] for r in done if r["market_unproven"]).items())),
            "ships_to_market": dict(sorted(Counter(str(r["ships_to_market"]).lower() for r in done).items())),
            SHIPS_TO_MARKET_FALSE: sum(1 for r in done if r[SHIPS_TO_MARKET_FALSE]),
            SHIPS_UNVERIFIED: sum(1 for r in recrawl if r[SHIPS_UNVERIFIED]),
            "markets": dict(sorted(Counter(r["market"] for r in done if r["market"]).items())),
            "lineage": dict(sorted(Counter(r["lineage"] for r in rows).items())),
            "recrawl_cohorts": len(recrawl),
            "cycle_hours": cycle,
            "projected_weekly_hours": round(cycle * HOURS_PER_WEEK / interval_hours, 2),
            "unproven_cycle_hours": round(sum(r["cycle_hours"] for r in done if r["market_unproven"]), 2),
        }
    out = [{"kind": "host", "host": h, **agg(rows)} for h, rows in sorted(hosts.items())]
    total = {"kind": "total", "hosts": len(hosts), "interval_hours": interval_hours, **agg(cohorts)}
    total["lane_hours_per_week_outside_claim_window"] = round(HOURS_PER_WEEK - 7 * claim_blocked_minutes / 60.0, 2)
    out.append(total)
    return out


async def census(conn: Any, *, interval_hours: int = DEFAULT_INTERVAL_HOURS,
                 claim_blocked_minutes: int = DEFAULT_CLAIM_BLOCKED_MINUTES) -> List[Dict[str, Any]]:
    """Run the census on an asyncpg connection, inside its own READ ONLY transaction."""
    from db.retailer_ingest import scope_key

    async with conn.transaction(readonly=True, isolation="repeatable_read"):
        await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        jobs = [dict(r) for r in await conn.fetch(JOBS_SQL)]
        runs = {r["job_id"]: dict(r) for r in await conn.fetch(RUNS_SQL)}
    cohorts = cohort_lines(jobs, runs, scope_key, datetime.now(timezone.utc))
    return cohorts + host_lines(cohorts, interval_hours=interval_hours, claim_blocked_minutes=claim_blocked_minutes)


async def _main(argv: List[str]) -> int:
    import asyncpg

    parser = argparse.ArgumentParser(description="read-only census of retailer-ingest cohorts")
    parser.add_argument("--cohorts-only", action="store_true", help="print the cohort lines only")
    parser.add_argument("--hosts-only", action="store_true", help="print the host and total lines only")
    parser.add_argument("--interval-hours", type=int, default=DEFAULT_INTERVAL_HOURS)
    parser.add_argument("--claim-blocked-minutes", type=int, default=DEFAULT_CLAIM_BLOCKED_MINUTES)
    args = parser.parse_args(argv)
    if not 1 <= args.interval_hours <= HOURS_PER_WEEK - 1:
        parser.error("--interval-hours must be 1..167")
    url = (os.getenv("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    if not url.startswith("postgres"):
        print(CENSUS_MARKER + json.dumps({"kind": "error", "error": "DATABASE_URL must be Postgres"}))
        return 2
    conn = await asyncpg.connect(url)
    try:
        lines = await census(conn, interval_hours=args.interval_hours,
                             claim_blocked_minutes=args.claim_blocked_minutes)
    finally:
        await conn.close()
    for text in render(lines, cohorts_only=args.cohorts_only, hosts_only=args.hosts_only):
        print(text, flush=True)
    return 0


def render(lines: List[Dict[str, Any]], *, cohorts_only: bool = False, hosts_only: bool = False) -> List[str]:
    """The printed lines: CENSUS_MARKER + one JSON object each."""
    return [CENSUS_MARKER + json.dumps(line, default=str, sort_keys=True) for line in lines
            if not ((cohorts_only and line["kind"] != "cohort") or (hosts_only and line["kind"] == "cohort"))]


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
