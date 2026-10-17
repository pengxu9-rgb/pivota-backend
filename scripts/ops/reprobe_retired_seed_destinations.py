"""OBSERVE-ONLY re-probe of seeds the destination sweep retired.

WHY (2026-10-17). The crawl address was Cloudflare challenge-listed on 10-16, and before be#2641 a
bot wall that redirected to a challenge page could classify as `redirected_off_product` (confirmed
dead). A read-only check found no retirement on the listed address (the 10-16 sweep retired 0), but
19 seeds retired on 10-15 as `redirected_off_product` and 49 addros.com seeds retired on 10-14 as
`dead_404` (all crawled fine on 10-09) are worth one more look from the new address. The database
keeps no final URL, so only a fetch can say where those pages land today.

WHAT IT DOES. Selects inactive seeds by their retirement note (`auto-retired <day>: destination
<verdict> (http N)`, written by services.external_seed_destination_liveness), optionally on one host,
in ONE READ ONLY transaction that is closed before any fetch. Then fetches each destination ONCE
through `probe_destination` -- the sweep's own politeness gate, robots, paced and robots-checked
redirect hops, challenge -> unverifiable -- under a global pacer (0.5 rps, 6 new hosts a minute,
`--deadline-seconds`) and the staged IP breaker, whose first trip of any kind STOPS this run.

WHAT IT NEVER DOES. Write. Nothing calls record_destination_observation, retire or reactivate, and
the only database statement is the SELECT below. A seed that comes back `live` is reactivated, if
at all, by a separate coordinator dry run with Peng's go.

RUN. A merchant fetch, so SUBNET=pivota-crawl (the default subnet leaves by the payment NAT):

    IMAGE=.../backend:<sha with be#2640 + be#2641> SUBNET=pivota-crawl JOB_PREFIX=reprobe-retired \\
      bash scripts/ops/run_oneoff_job.sh scripts/ops/reprobe_retired_seed_destinations.py \\
      --select 2026-10-15:redirected_off_product --select 2026-10-14:dead_404:addros.com

Output: one JSON line per seed ({"rp": "seed", retired_on, retired_as, now_verdict, now_http,
final_url, note, cross_host}) and a summary with the breaker's own.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

# `python -c` / an exec has no __file__: fall back to the repo root as the working directory.
_HERE = globals().get("__file__") or os.path.join(
    os.getcwd(), "scripts", "ops", "reprobe_retired_seed_destinations.py")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(_HERE)))))

import httpx  # noqa: E402

from services import crawl_global_pacer, crawl_ip_throttle  # noqa: E402
from services import external_seed_destination_liveness as liveness  # noqa: E402

RPS = 0.5
MAX_NEW_HOSTS_PER_MINUTE = 6
DEFAULT_DEADLINE_SECONDS = 900.0
DEFAULT_MAX_TARGETS = 80

_DAY = re.compile(r"^2\d{3}-\d{2}-\d{2}$")
_VERDICT = re.compile(r"^[a-z_0-9]+$")
_HOST = re.compile(r"^[a-z0-9.-]+$")
_NOTE = re.compile(r"auto-retired (\d{4}-\d{2}-\d{2}): destination ([a-z_0-9]+) \(http ([^)]*)\)")

_DEST = "coalesce(nullif(trim(s.canonical_url), ''), s.destination_url)"
SELECT_SQL = f"""
SELECT s.id, {_DEST} AS url, s.notes
  FROM external_product_seeds s
 WHERE s.status = 'inactive'
   AND s.notes LIKE '%auto-retired ' || $1::text || ': destination ' || $2::text || ' (%'
   AND ($3::text IS NULL
        OR lower(regexp_replace(substring({_DEST} from '^https?://([^/:?#]+)'), '^www\\.', '')) = $3::text)
 ORDER BY 2
"""


def parse_select(value: str) -> Tuple[str, str, Optional[str]]:
    """`<day>:<verdict>[:<host>]`, each part validated (they reach SQL only as bind values)."""
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError(f"--select wants <day>:<verdict>[:<host>], got {value!r}")
    day, verdict = parts[0], parts[1]
    host = parts[2].lower().removeprefix("www.") if len(parts) == 3 else None
    if not _DAY.match(day) or not _VERDICT.match(verdict) or (host is not None and not _HOST.match(host)):
        raise argparse.ArgumentTypeError(f"--select part not recognised in {value!r}")
    return day, verdict, host


def emit(kind: str, **payload: Any) -> None:
    print(json.dumps({"rp": kind, **payload}, default=str), flush=True)


async def load_targets(selections: Sequence[Tuple[str, str, Optional[str]]]) -> List[Dict[str, Any]]:
    import asyncpg

    conn = await asyncpg.connect(
        os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"), timeout=30)
    try:
        return await select_targets(conn, selections)
    finally:
        await conn.close()


async def select_targets(conn: Any, selections: Sequence[Tuple[str, str, Optional[str]]]) -> List[Dict[str, Any]]:
    """The selected seeds, de-duplicated, in ONE READ ONLY transaction (checked) with a 60 s cap."""
    async with conn.transaction(readonly=True):
        await conn.execute("SET LOCAL statement_timeout = '60s'")
        if await conn.fetchval("SHOW transaction_read_only") != "on":
            raise SystemExit("refusing: the transaction is not read-only")
        seen: Dict[str, Dict[str, Any]] = {}
        for day, verdict, host in selections:
            for row in await conn.fetch(SELECT_SQL, day, verdict, host):
                seen.setdefault(row["id"], dict(row))
        return sorted(seen.values(), key=lambda r: str(r["url"] or ""))


async def reprobe(
    targets: Sequence[Dict[str, Any]],
    *,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Probe each target once, paced; print a line per seed; return the summary. No writes."""
    pacer = crawl_global_pacer.GlobalOriginPacer(
        rps=RPS, max_new_hosts_per_minute=MAX_NEW_HOSTS_PER_MINUTE,
        deadline=crawl_global_pacer._now() + float(deadline_seconds))
    # ANY trip stops: this is a one-off on a freshly swapped address, not a nightly run that
    # should ride out a cool-down.
    breaker = crawl_ip_throttle.StagedIpThrottleBreaker.from_env(
        on_trip=lambda _b, event: pacer.stop(f"ip breaker {event.get('action')}"))
    owns_client = client is None
    client = client or httpx.AsyncClient(
        timeout=25.0, follow_redirects=True,
        **crawl_global_pacer.redirect_client_kwargs(robots_user_agent=liveness.USER_AGENT))
    verdicts: Dict[str, int] = {}
    stopped: Optional[str] = None
    try:
        with crawl_ip_throttle.installed(breaker), crawl_global_pacer.installed(pacer):
            for target in targets:
                match = _NOTE.search(str(target.get("notes") or ""))
                try:
                    obs = await liveness.probe_destination(client, str(target["url"]), max_wait=None)
                except crawl_global_pacer.GlobalPaceStopped as exc:
                    stopped = str(exc) or "stopped"
                    emit("stopped", reason=stopped, at_seed=target["id"])
                    break
                verdicts[obs.verdict] = verdicts.get(obs.verdict, 0) + 1
                emit("seed", id=target["id"], url=target["url"],
                     retired_on=match and match.group(1), retired_as=match and match.group(2),
                     retired_http=match and match.group(3), now_verdict=obs.verdict,
                     now_http=obs.http_status, final_url=obs.final_url, note=obs.note,
                     cross_host=obs.cross_host)
    finally:
        if owns_client:
            await client.aclose()
    summary = {"targets": len(targets), "probed": sum(verdicts.values()), "verdicts": verdicts,
               "stopped": stopped, **breaker.summary()}
    emit("summary", **summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--select", action="append", type=parse_select, required=True,
                        help="<day>:<verdict>[:<host>] of the retirement note; repeatable")
    parser.add_argument("--max-targets", type=int, default=DEFAULT_MAX_TARGETS,
                        help="refuse when the selection is larger than this (default 80)")
    parser.add_argument("--deadline-seconds", type=float, default=DEFAULT_DEADLINE_SECONDS)
    return parser


async def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_targets < 1 or args.deadline_seconds <= 0:
        raise SystemExit("--max-targets must be >= 1 and --deadline-seconds > 0")
    targets = await load_targets(args.select)
    emit("targets", n=len(targets), selections=[list(s) for s in args.select])
    if len(targets) > args.max_targets:
        emit("refused", reason=f"{len(targets)} targets > --max-targets {args.max_targets}")
        return 2
    await reprobe(targets, deadline_seconds=args.deadline_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
