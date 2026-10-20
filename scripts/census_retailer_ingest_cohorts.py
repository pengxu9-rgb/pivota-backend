#!/usr/bin/env python3
"""READ-ONLY census of the retailer-ingest cohorts the weekly re-enqueue would re-crawl.

Per cohort (db.retailer_ingest.scope_key, the key the open-job index is built on), one JSON line:
its host, newest status, last done date, whether the latest done job names options.market, the market
its last APPLIED run's evidence proves (and why not, when it does not), the market the re-enqueue would
use (explicit first, then evidence, NEVER a default), whether its lineage is eligible (no failed /
cancelled / nothing job after the latest done one), and the hours its last cycle took (the latest
dry run that crawled -- clean or held -- plus the latest applied apply of the latest done job).

Then one line per host (cohorts, last done, explicit / evidence / market_unproven counts with the
reasons, markets, projected weekly hours) and one total line, with the lane hours a week leaves outside
the drain's claim window (RETAILER_INGEST_CLAIM_BLACKOUT_UTC, default 03:00-06:30).

SAFETY. One READ ONLY transaction (REPEATABLE READ, so every query reads one snapshot) with SET LOCAL
statement_timeout; it writes nothing and creates nothing: two queries. Prints JSON lines only. It carries
no at-sign so it can be passed inline to scripts/ops/run_oneoff_job.sh, and it needs no crawl subnet:

    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/census_retailer_ingest_cohorts.py)"
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/census_retailer_ingest_cohorts.py)" --cohorts-only

Self-contained on purpose: the deployed image may predate the re-enqueue, so the market derivation and
the lineage rule below are COPIES of services/retailer_ingest/weekly_reenqueue.py's, pinned to it by
tests/test_census_retailer_ingest_cohorts.py. Only db.retailer_ingest.scope_key (in every image since
migration 234) is imported from the image.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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
# COPIES (pinned by test): pipeline.INGEST_MARKETS and region_pricing's currency for each.
INGEST_MARKET_CURRENCY = {"US": "USD", "AU": "AUD", "JP": "JPY", "SG": "SGD"}
OPEN_STATUSES = ("queued", "apply_due", "held")
TERMINAL_AFTER_DONE = ("failed", "cancelled", "nothing")
DEFAULT_CLAIM_BLACKOUT_MINUTES = 210  # 03:00-06:30, the drain's default claim window

JOBS_SQL = """
    SELECT id, domain, brand, options, status, created_at, updated_at
    FROM retailer_ingest_jobs
"""

# Per job: its newest applied run's evidence (a COPY of weekly_reenqueue._EVIDENCE_SQL's columns, pinned
# by test) and the seconds of its latest crawling dry run and latest applied apply.
RUNS_SQL = """
    WITH applied AS (
        SELECT DISTINCT ON (r.job_id)
               r.job_id,
               r.crawl -> 'storefront' ->> 'currency' AS store_currency,
               r.crawl -> 'presented_currency' AS presented,
               r.checks -> 'storefront' ->> 'market' AS checked_market,
               (SELECT count(*) FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(r.readback -> 'rows') = 'array' THEN r.readback -> 'rows'
                         ELSE '[]'::jsonb END) e) AS readback_rows,
               (SELECT count(*) FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(r.readback -> 'rows') = 'array' THEN r.readback -> 'rows'
                         ELSE '[]'::jsonb END) e
                 WHERE CASE WHEN jsonb_typeof(e -> 'offers') = 'number'
                            THEN CAST(e ->> 'offers' AS numeric) > 0 ELSE false END
                   AND e -> 'offers_in_currency' = e -> 'offers'
                   AND e -> 'offers_in_market' = e -> 'offers') AS readback_rows_proven,
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
    SELECT coalesce(a.job_id, c.job_id) AS job_id, a.store_currency, a.presented, a.checked_market,
           a.readback_rows, a.readback_rows_proven, a.apply_s, c.dry_run_s
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


def fold_host(domain: Any) -> str:
    host = str(domain or "").strip().lower()
    return host[4:] if host.startswith("www.") else host


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
    presented = _json(evidence.get("presented"))
    presented = presented if isinstance(presented, dict) else {}
    pages = presented.get("pages_by_currency")
    pages = {_norm(c): n for c, n in pages.items()} if isinstance(pages, dict) else {}
    seen = {c for c, n in pages.items() if isinstance(n, int) and not isinstance(n, bool) and n > 0}
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


def _at(value: Any) -> datetime:
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


def cohort_lines(jobs: List[Dict[str, Any]], runs: Dict[str, Dict[str, Any]],
                 scope_key: Any) -> List[Dict[str, Any]]:
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
                                "newest_status": members[-1].get("status"), "lineage": state,
                                "slot": int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % HOURS_PER_WEEK}
        if done is not None:
            options = _options(done.get("options"))
            run = runs.get(done["id"]) or {}
            # A job whose only run row is a dry run has no applied evidence (RUNS_SQL is a FULL JOIN).
            evidence = run if run.get("readback_rows") is not None else None
            ev_market, ev_why = evidence_market(evidence)
            market, source, why = derive_market(options, evidence)
            seconds = sum(float(run.get(k) or 0) for k in ("dry_run_s", "apply_s"))
            line.update({
                "last_done_job_id": done["id"], "last_done_at": _at(done.get("updated_at") or done["created_at"]).isoformat(),
                "explicit_market": options.get("market") not in (None, ""),
                "evidence_market": ev_market, "evidence_unproven": ev_why,
                "market": market, "market_source": source or None, "market_unproven": market is None,
                "unproven_reason": why, "cycle_hours": round(seconds / 3600.0, 3),
            })
        out.append(line)
    return out


def host_lines(cohorts: List[Dict[str, Any]], *, claim_blackout_minutes: int) -> List[Dict[str, Any]]:
    hosts: Dict[str, List[Dict[str, Any]]] = {}
    for c in cohorts:
        hosts.setdefault(c["host"], []).append(c)

    def agg(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        done = [r for r in rows if r.get("last_done_job_id")]
        recrawl = [r for r in done if r["lineage"] in ("eligible", "open") and not r["market_unproven"]]
        return {
            "cohorts": len(rows), "done_cohorts": len(done),
            "last_done_at": max((r["last_done_at"] for r in done), default=None),
            "explicit_market": sum(1 for r in done if r["explicit_market"]),
            "evidence_market": sum(1 for r in done if r["market_source"] == "evidence"),
            "market_unproven": sum(1 for r in done if r["market_unproven"]),
            "unproven_reasons": dict(sorted(Counter(r["unproven_reason"] for r in done if r["market_unproven"]).items())),
            "markets": dict(sorted(Counter(r["market"] for r in done if r["market"]).items())),
            "lineage": dict(sorted(Counter(r["lineage"] for r in rows).items())),
            "projected_weekly_hours": round(sum(r["cycle_hours"] for r in recrawl), 2),
            "unproven_weekly_hours": round(sum(r["cycle_hours"] for r in done if r["market_unproven"]), 2),
        }
    out = [{"kind": "host", "host": h, **agg(rows)} for h, rows in sorted(hosts.items())]
    total = {"kind": "total", "hosts": len(hosts), **agg(cohorts)}
    total["lane_hours_per_week_outside_claim_window"] = round(HOURS_PER_WEEK - 7 * claim_blackout_minutes / 60.0, 2)
    out.append(total)
    return out


async def census(conn: Any, *, claim_blackout_minutes: int = DEFAULT_CLAIM_BLACKOUT_MINUTES) -> List[Dict[str, Any]]:
    """Run the census on an asyncpg connection, inside its own READ ONLY transaction."""
    from db.retailer_ingest import scope_key

    async with conn.transaction(readonly=True, isolation="repeatable_read"):
        await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        jobs = [dict(r) for r in await conn.fetch(JOBS_SQL)]
        runs = {r["job_id"]: dict(r) for r in await conn.fetch(RUNS_SQL)}
    cohorts = cohort_lines(jobs, runs, scope_key)
    return cohorts + host_lines(cohorts, claim_blackout_minutes=claim_blackout_minutes)


async def _main(argv: List[str]) -> int:
    import asyncpg

    parser = argparse.ArgumentParser(description="read-only census of retailer-ingest cohorts")
    parser.add_argument("--cohorts-only", action="store_true", help="print the cohort lines only")
    parser.add_argument("--hosts-only", action="store_true", help="print the host and total lines only")
    parser.add_argument("--claim-blackout-minutes", type=int, default=DEFAULT_CLAIM_BLACKOUT_MINUTES)
    args = parser.parse_args(argv)
    url = (os.getenv("DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    if not url.startswith("postgres"):
        print(json.dumps({"kind": "error", "error": "DATABASE_URL must be Postgres"}))
        return 2
    conn = await asyncpg.connect(url)
    try:
        lines = await census(conn, claim_blackout_minutes=args.claim_blackout_minutes)
    finally:
        await conn.close()
    for line in lines:
        if (args.cohorts_only and line["kind"] != "cohort") or (args.hosts_only and line["kind"] == "cohort"):
            continue
        print(json.dumps(line, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
