"""Re-read the price and availability of the external seeds we serve.

ARMED. Cloud Scheduler `external-referral-refresh-cron` (us-west1, `15 5 * * *` UTC) invokes the
Cloud Run job `external-referral-refresh`, which runs `--limit 4000 --budget-seconds 3300` on
subnet `pivota-crawl` — the reserved crawl-egress NAT, without which most brand hosts answer a bot
challenge. Verified running daily 2026-09-06. (This docstring previously said "nothing schedules
this"; that was true when written and is not now. A GitHub Actions cron remains impossible — Cloud
SQL `pivota-pg` is private-IP only, so a runner has no route to the DB, `tests/test_run_oneoff_job.py`.)

WHAT THE JOB DOES NOT DO, and why the exit code matters. It refreshes `external_product_seeds`.
The search/offers lane reads the seed, so it sees fresh prices; the PDP (`agent_pdp_view`) and the
index's `serving_eligible.has_price` gate read `catalog_offers`, which is re-projected only when
`EXTERNAL_OFFER_DUAL_WRITE_ENABLED` is armed (see
`routes/employee_products._project_refreshed_seed_to_serving_surfaces`). With that flag off this
job runs green while a quarter of the catalogue serves a stale price on the product page.

The image is pinned on the Cloud Run job and does NOT auto-deploy on merge — a change here ships
only when someone rebuilds and updates the job.

Usage:

    python3 -m jobs.external_referral_refresh --limit 500 [--budget-seconds 3300] [--host-concurrency 4]

COVERAGE, measured 2026-09-27 (image 8f7605b1e, serial): 3,282 rows in 3,300s, 2,220 origin
reads, a ~12-day cycle over ~23.8k active seeds. The launch target is every SERVED seed re-read
within 3 days. Three levers, all in this job: the queue puts served, stale seeds first and skips
suppressed, quarantined and retired ones (`get_external_referral_refresh_candidate_seed_ids`); a
host that gives no answer five rows in a row stops costing rows (`_host_unreachable_trip`); and
`--host-concurrency` reads several hosts at once while each host still sees one request at a
time, paced by `crawl_politeness` as before.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Dict, Optional

from db.database import database
from routes.employee_products import _refresh_external_seed_by_id
from services.external_referral_readiness import run_external_referral_refresh_batch


logger = logging.getLogger(__name__)


async def _refresh_unbounded(seed_id: str) -> Dict[str, Any]:
    """The batch's patience is UNBOUNDED, and it has to say so.

    `crawl_politeness` refuses a slot further out than the caller allows, and the default
    ceiling (CRAWL_MAX_WAIT_SECONDS, 10s) exists for the interactive route where a human is
    waiting. In a batch that ceiling is actively harmful: most of the backoff curve sits
    beyond it, so a host that has 429'd a couple of times can never be waited for. The
    refusal surfaces as a generic failure, every remaining row on that host resolves in
    milliseconds, and the run reports them as unreadable when in fact we declined to wait.

    `max_wait=0` disables the ceiling (`crawl_politeness.before_request`: the refusal is
    guarded on `ceiling > 0`). The pacing itself is unchanged — we still wait our turn; we
    simply stop giving up on the turn. The sibling destination sweep already does this.

    UNBOUNDED PATIENCE IS NOT UNBOUNDED TIME, and it used to be. `max_wait=0` removed the only
    ceiling that ever clamped a host's `Crawl-delay`, so one host serving `Crawl-delay: 86400`
    made a single row sleep 24h and pushed every remaining row on that host a day out.
    `CRAWL_MAX_ROBOTS_DELAY_SECONDS` now bounds what any ONE row here can cost, and
    `--budget-seconds` bounds the run. Both are prerequisites for ever putting this job on a
    schedule.

    NOR IS IT PATIENCE FOR ONE HOST AT THE EXPENSE OF ALL THE OTHERS. The loop is serial, so every
    second spent waiting out one host's hold is a second no other host is read. On 09-20/21/22
    fentybeauty.com's 429 cycle (x9 up to the 300s cap, one request through, repeat) spent the
    entire 3300s budget and the run refreshed 81-85 of 4,000 rows. The batch now stops asking a
    host once it has answered `EXTERNAL_REFERRAL_REFRESH_HOST_BLOCK_TRIP` 429/503s in a row
    (`services.external_referral_readiness.host_backoff_tripped`). That is strictly MORE polite,
    not less — the host gets no further requests this run — and its rows stay unstamped at the
    head of tomorrow's queue. Per-row patience is unchanged.
    """
    return await _refresh_external_seed_by_id(seed_id, max_wait=0)


async def run_daily_external_referral_refresh(
    *,
    limit: int = 500,
    budget_seconds: Optional[float] = None,
    host_concurrency: Optional[int] = None,
) -> Dict[str, Any]:
    return await run_external_referral_refresh_batch(
        refresh_seed_by_id=_refresh_unbounded,
        limit=limit,
        budget_seconds=budget_seconds,
        host_concurrency=host_concurrency,
    )


PRICE_REFRESH_KEYS = (
    "price_changed",
    "price_unchanged",
    "price_filled",
    "price_unavailable",
    "price_skipped_non_positive",
    "price_skipped_incomplete_pair",
    "price_skipped_currency_mismatch",
    "price_skipped_unreadable",
    "price_unreadable_reasons",
    "price_unreadable_top_hosts",
)


def price_refresh_line(summary: Dict[str, Any]) -> Dict[str, Any]:
    """The run's price outcomes, as the one `PRICE_REFRESH` log line carries them."""
    return {key: summary.get(key) for key in PRICE_REFRESH_KEYS}


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh active external referral seeds for runtime gating.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum number of referral seeds to refresh")
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=None,
        help=(
            "Wall-clock ceiling for the run; stop starting rows once it is spent. "
            "Defaults to EXTERNAL_REFERRAL_REFRESH_BUDGET_SECONDS, then to "
            "services.external_referral_readiness.EXTERNAL_REFERRAL_REFRESH_BUDGET_SECONDS. "
            "0 disables it."
        ),
    )
    parser.add_argument(
        "--host-concurrency",
        type=int,
        default=None,
        help=(
            "Hosts read at once, never two rows of one host at a time (1 = serial). Defaults to "
            "EXTERNAL_REFERRAL_REFRESH_HOST_CONCURRENCY, then 1. Clamped to [1, 8]."
        ),
    )
    args = parser.parse_args()

    # THE POOL DOES NOT EXIST UNTIL SOMEONE OPENS IT. Inside the API process the lifespan hook
    # connects `database` before any route runs; a CLI entrypoint has no lifespan, so it has to
    # do it itself. Without this the first query raises AssertionError("DatabaseBackend is not
    # running") out of `db.database`'s patched `PostgresConnection.acquire` — i.e. this job could
    # never have completed a single run against Postgres. It looked fine locally only because the
    # sqlite backend has no such guard, which is why the regression test around this asserts on
    # the CONNECT CALL rather than on a run succeeding.
    async def _run() -> Dict[str, Any]:
        await database.connect()
        try:
            return await run_daily_external_referral_refresh(
                limit=args.limit,
                budget_seconds=args.budget_seconds,
                host_concurrency=args.host_concurrency,
            )
        finally:
            await database.disconnect()

    summary = asyncio.run(_run())
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    # ONE LINE, PREFIXED, for the price outcomes. The dump above is multi-line (one log entry per
    # line in Cloud Logging) and the logger.info below is dropped in prod (root at WARNING), so
    # neither can be filtered for a night's price counts. The prefix keeps it textPayload.
    print("PRICE_REFRESH " + json.dumps(price_refresh_line(summary), separators=(",", ":"), default=str))
    logger.info("external referral refresh completed", extra={"summary": summary})
    # EXIT CODE FOLLOWS THE SUMMARY. This used to `return 0` unconditionally, so Cloud Run
    # showed a green tick over every run — including nights that stopped on budget with 659
    # rows untouched, or that served half their rows from cache without reaching an origin.
    # A scheduler tick is the only place anyone would notice, so the summary has to reach it.
    # `maxRetries` is 0 on the job, so a non-zero exit surfaces the run without a retry storm.
    status = str(summary.get("status") or "").strip().lower()
    if status != "success":
        logger.warning(
            "external referral refresh finished %s (stopped_early=%s budget_reach=%s "
            "origin_yield=%s host_backoff_skips=%s reasons=%s)",
            status,
            summary.get("stopped_early"),
            summary.get("budget_reach"),
            summary.get("origin_yield"),
            summary.get("host_backoff_skips"),
            summary.get("degraded_reason_counts"),
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
