"""Re-read the destinations we publish, and withdraw the ones that are gone.

Runs as a Cloud Run Job on a Cloud Scheduler trigger (`infra/gcp/setup_scheduler.sh`), NOT as
a 33rd APScheduler entry: it crawls third-party storefronts and must leave from the reserved
crawl-egress NAT (`infra/gcp/setup_crawl_egress.sh`), not from a web dyno whose IP is shared
with everything else we do.

SIZING. A full pass has to fit inside the readiness gate's staleness window
(`EXTERNAL_REFERRAL_STALE_DAYS = 7`), or every seed spends part of its life blocked no matter
how well the sweep works. ~11.4k active seeds / 7 days ≈ 1,700 a day, which the default limit
covers with room. Stage 1 means that costs a few hundred requests, not 1,700: one
`/products.json` read per host answers every seed on it.

THE NUMBER TO WATCH IS NOT `dead_links_found`. It is `hosts_unverifiable`. A run that cannot
read its hosts reports zero dead links and looks identical to a healthy one — which is exactly
what a client outside the crawl egress sees today (213 of 286 hosts refuse it). `coverage_alarm`
turns that into a log line at WARNING so it can be alerted on.

Usage:

    python3 -m jobs.external_seed_destination_sweep --limit 1700
    python3 -m jobs.external_seed_destination_sweep --limit 200 --no-retire   # observe only
    python3 -m jobs.external_seed_destination_sweep --limit 3000 --budget-seconds 3300 --global-rps 0.5

PACED SINCE 2026-10-16, with the external-referral refresh's rules (be#2640): one global rate for
every request of the run (`--global-rps`, default 0.5), at most `--max-new-hosts-per-minute` (6) new
hosts a minute, a wall-clock `--budget-seconds` (3300) after which no request starts, and the
staged IP breaker (3 hosts answering a Cloudflare challenge within 120 s: a 900 s cool-down at half
rate; a second trip ends the run, exit 3). Each knob is also an env var, and 0 switches it off.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Dict

from db.database import database
from services import shopify_edge_pacer
from services.external_seed_destination_liveness import coverage_alarm, run_destination_sweep

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 1700

EXIT_OK = 0
# The IP breaker's final trip ended the run: the crawl IP is being challenged or throttled. The same
# code as `jobs.external_referral_refresh.EXIT_IP_THROTTLED`.
EXIT_IP_THROTTLED = 3

# One filterable line a night, the refresh's prefix and field names.
IP_THROTTLE_PREFIX = "IP_THROTTLE "
_IP_THROTTLE_LINE_KEYS = (
    "global_rps", "final_rate", "max_new_hosts_per_minute", "paced_requests", "new_hosts_contacted",
    "paces_waited_seconds", "breaker_trips", "breaker_trip_events", "cooldowns", "cooldown_seconds",
    "stopped", "budget_seconds", "hosts", "hosts_stopped", "hosts_not_started",
    "hosts_skipped_ip_throttle", "throttle_diagnostics",
)


def ip_throttle_line(summary: Dict[str, Any]) -> Dict[str, Any]:
    line: Dict[str, Any] = {"job": "external-seed-destination-sweep"}
    line.update({k: v for k, v in summary.items() if k.startswith("ip_throttle")})
    line.update({k: summary.get(k) for k in _IP_THROTTLE_LINE_KEYS})
    return line


async def run_daily_destination_sweep(*, limit: int = DEFAULT_LIMIT, retire: bool = True, **pacing: Any) -> Dict[str, Any]:
    summary = await run_destination_sweep(limit=limit, retire=retire, **pacing)
    if shopify_edge_pacer.enabled():
        # Only with the flag on, so the summary is unchanged while the pacer is dark.
        summary["shopify_edge_pacer"] = shopify_edge_pacer.stats()
    alarm = coverage_alarm(summary)
    if alarm:
        summary["coverage_alarm"] = alarm
        logger.warning(alarm, extra={"summary": summary})
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--no-retire",
        dest="retire",
        action="store_false",
        help="Record observations but never withdraw a seed. Use for the first production run.",
    )
    parser.set_defaults(retire=True)
    parser.add_argument(
        "--budget-seconds", type=float, default=None,
        help="Wall-clock ceiling: no request starts after it. Defaults to "
             "EXTERNAL_SEED_DESTINATION_SWEEP_BUDGET_SECONDS, then 3300. 0 disables it.",
    )
    parser.add_argument(
        "--global-rps", type=float, default=None,
        help="The IP's total request rate for this run, every host worker sharing it, each gap "
             "jittered +-30%%. Defaults to EXTERNAL_SEED_DESTINATION_SWEEP_GLOBAL_RPS, then 0.5. "
             "0 disables it.",
    )
    parser.add_argument(
        "--max-new-hosts-per-minute", type=int, default=None,
        help="Hosts contacted for the first time in any 60s window. Defaults to "
             "EXTERNAL_SEED_DESTINATION_SWEEP_MAX_NEW_HOSTS_PER_MINUTE, then 6. 0 disables it.",
    )
    parser.add_argument(
        "--ip-challenge-trip-hosts", type=int, default=None,
        help="Distinct hosts answering a Cloudflare challenge within the window that trip the IP "
             "breaker. Defaults to CRAWL_IP_CHALLENGE_TRIP_HOSTS, then 3. <= 0 disables this rule.",
    )
    parser.add_argument(
        "--ip-challenge-window-seconds", type=float, default=None,
        help="Defaults to CRAWL_IP_CHALLENGE_WINDOW_SECONDS, then 120.",
    )
    parser.add_argument(
        "--ip-throttle-cooldown-seconds", type=float, default=None,
        help="Pause after the breaker's first trip, then half rate; a second trip ends the run. "
             "Defaults to CRAWL_IP_THROTTLE_COOLDOWN_SECONDS, then 900. <= 0: the first trip ends it.",
    )
    parser.add_argument(
        "--no-ip-throttle-breaker", action="store_true",
        help="Kill switch for the IP breaker (CRAWL_IP_THROTTLE_BREAKER_ENABLED=false does the same).",
    )
    args = parser.parse_args()

    async def _run() -> Dict[str, Any]:
        await database.connect()
        try:
            return await run_daily_destination_sweep(
                limit=args.limit,
                retire=args.retire,
                budget_seconds=args.budget_seconds,
                global_rps=args.global_rps,
                max_new_hosts_per_minute=args.max_new_hosts_per_minute,
                ip_challenge_trip_hosts=args.ip_challenge_trip_hosts,
                ip_challenge_window_seconds=args.ip_challenge_window_seconds,
                ip_throttle_cooldown_seconds=args.ip_throttle_cooldown_seconds,
                # None, not True, when the flag is absent: the env var still gets its say.
                ip_throttle_enabled=False if args.no_ip_throttle_breaker else None,
            )
        finally:
            await database.disconnect()

    summary = asyncio.run(_run())
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(IP_THROTTLE_PREFIX + json.dumps(ip_throttle_line(summary), separators=(",", ":"), default=str))
    if summary.get("ip_throttled"):
        logger.error(
            "destination sweep finished ip_throttled: the IP breaker tripped %s time(s); %s host(s) "
            "cut short, %s not started", summary.get("breaker_trips"),
            summary.get("hosts_stopped"), summary.get("hosts_not_started"),
        )
        return EXIT_IP_THROTTLED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
