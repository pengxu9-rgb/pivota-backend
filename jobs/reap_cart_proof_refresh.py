"""Refresh the Reap cart-link storefront proofs on a schedule: one lane per proof writer.

    python -m jobs.reap_cart_proof_refresh mirror     --on-crawl-egress --budget-seconds 10800
    python -m jobs.reap_cart_proof_refresh enrichment --on-crawl-egress --budget-seconds 3600

WHAT IT IS. The Reap cart-link lane refuses a row without a fresh storefront proof, and two writers
author those proofs, each one --domain at a time:

  * `mirror`      scripts/backfill_shopify_variant_ids.py (option 1, #2459) writes
                  `seed_data.snapshot.shopify_cart_proof` on external-seed mirror rows. Valid 7 days
                  (services.shopify_variant_identity.CART_PROOF_MAX_AGE).
  * `enrichment`  jobs/enrichment_cart_variant_proof.py (option 2, #2464) writes
                  `enrichment_cart_variant_proofs`. Valid 72 hours
                  (services.reap_enrichment_cart_proof.MAX_PROOF_AGE).

This module owns NO proof logic. It picks the domains, walks each one to exhaustion by calling the
writer's own `run()` page after page on the writer's own cursor, bounds the whole pass with a
wall-clock budget, and prints ONE report line. Every rule about what a proof is, how a storefront is
read and paced inside a page, and what is written, stays in the writer.

THE DOMAINS.
  * mirror: every domain on config/tierb_cart_link_merchants.json. That list IS the set of stores the
    cart-link lane can buy from (it refuses a merchant without a fresh Tier B eligibility row, and
    only this list gets one), so a mirror proof anywhere else serves no purchase. The order rotates
    by UTC day, so a pass the budget cuts short starts somewhere else tomorrow instead of starving
    the same tail every day.
  * enrichment: `ENRICHMENT_DOMAINS` below, the five stores #2464's census found enrichment rows on.
    Deliberately a pinned constant, not a query: #2464 refuses a default population, and so does this.
    Each must also be on the Tier B list (the writer's `plan_domains` refuses it otherwise, exit 2).
    Ordered by their worst-case `.js` cost, cheapest first, so MAC (~1,870 handles) is last and a
    pathological MAC run can never keep the other four from being refreshed.

APPLY OR DRY RUN: THE GATE. `REAP_CART_PROOF_APPLY` (1/true/yes/on, any case) makes the run write;
anything else, unset included, is a DRY RUN. A dry run still fetches every storefront, exactly as
hard (both writers gate the write, not the crawl). This is deliberately NOT the Tier B job's "dark
means contact nobody": the dark job is also the dry-run vehicle, so `gcloud run jobs execute` on a
dark job is the dry run on the real job definition (image, subnet, secret) before anything is armed.
infra/gcp/setup_reap_cart_proof_jobs.sh sets the gate false and pauses the trigger unless --enable.

THE EGRESS. Both writers fetch merchant storefronts, so this runs ONLY on the crawl subnet
(`pivota-crawl`, NAT 34.82.199.35), never the default NAT whose address payment partners allowlist.
A container cannot see its own subnet, so `--on-crawl-egress` is REQUIRED (the enrichment writer's
own rule, applied to both lanes): without it this exits 2 before touching the database or the net.

PACING. Inside a page the writer paces itself (mirror: >= 1.0 s globally, >= 3.0 s per storefront;
enrichment: >= 3.0 s between request starts, plus crawl_politeness). Between pages this module adds
the gap a writer's fresh pacer cannot know about: every writer call builds a new pacer that has
never seen the previous page's last request, so each call is preceded by `inter_call_gap_s()`, the
writer's own slowest spacing. The enrichment lane also hands every call ONE shared pacer (`run()`
accepts it), so its request gap holds across domains too.

THE BUDGET is checked before every writer call: once spent, no new page starts. A page already
running finishes (it is the writer's; stopping it midway would strand its writes), which is why the
task timeout in the setup script is the budget PLUS one page's worst case. A domain the budget cut
is `budget_stopped` (walked partway) or `not_reached`.

BLOCKS. A writer that aborts on consecutive block-shaped answers (both do) stops THE WHOLE PASS: the
2026-08-21 block was IP-level and cross-domain, so the next domain would only meet the same wall.
Note one difference from a single multi-domain writer run: the enrichment writer's consecutive-block
counter lives inside one `run()` call, so it restarts at each domain here. A block still aborts the
domain it happens in within `ENRICHMENT_PROOF_ABORT_AFTER_BLOCKS` answers, and that aborts the pass.

A CRASH (an exception out of the writer) is recorded against its domain and the pass moves on: one
store's unreadable data must not cost the other stores their refresh.

THE REPORT: one line, `REAP_CART_PROOF_REPORT {json}` (a text prefix, so it lands in textPayload and
`scripts/ops/run_oneoff_job.sh` shows it), with each domain's status, pages, elapsed seconds and the
writer's own counters (mirror: summed over pages; enrichment: the writer's per-domain report, one per
page). A `REAP_CART_PROOF_PROGRESS` text line is also printed as each domain ends, so a run the task
timeout kills still leaves its finished domains in the log.

EXIT CODES. #2464's four, plus one for the budget:
  0  every domain walked to its end
  1  a writer aborted on a block; the pass stopped there
  2  bad arguments, no --on-crawl-egress, or a domain list the writer refuses; nothing attempted
  3  a writer crashed on at least one domain (the others were still attempted)
  4  the budget ran out before every domain was walked to its end
2 is returned before anything runs; otherwise 1 outranks 3, which outranks 4. A crash of the
whole pass (the database unreachable) is also 3, with a `REAP_CART_PROOF_CRASH` line and no report.

Provisioned by infra/gcp/setup_reap_cart_proof_jobs.sh (dark unless --enable); runbook
docs/runbooks/reap_cart_proofs.md. Not registered with services/audit_scheduler, not run by CI or
by any deploy script; tests/test_reap_cart_proof_refresh.py pins that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

LANES = ("mirror", "enrichment")
GATE_ENV = "REAP_CART_PROOF_APPLY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: The stores #2464's census (2026-09-29) found enrichment rows on, cheapest worst case first.
ENRICHMENT_DOMAINS = (
    "stilacosmetics.com",
    "jsmbeauty.sg",
    "bluemercury.com",
    "tartecosmetics.com",
    "maccosmetics.com",
)

#: Mirror candidates per writer call. The writer's own default is 100; 50 halves the worst case of
#: the one page the budget cannot stop (see the setup script's task timeout).
MIRROR_PAGE_SIZE = 50

EXIT_OK = 0
EXIT_ABORTED_ON_BLOCK = 1
EXIT_BAD_ARGS = 2
EXIT_CRASHED = 3
EXIT_BUDGET = 4

REPORT_PREFIX = "REAP_CART_PROOF_REPORT "
PROGRESS_PREFIX = "REAP_CART_PROOF_PROGRESS "

DONE = "done"
ABORTED = "aborted_on_block"
CRASHED = "crashed"
BUDGET_STOPPED = "budget_stopped"
NOT_REACHED = "not_reached"
CURSOR_STUCK = "cursor_stuck"


def apply_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Strict: exactly 1/true/yes/on after trimming, any case. Everything else is a dry run."""
    env = os.environ if environ is None else environ
    raw = env.get(GATE_ENV)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value:
        logger.warning("%s=%r is not a recognised truthy value; this run is a DRY RUN", GATE_ENV, raw)
    return False


def rotate(domains: Sequence[str], day_index: int) -> List[str]:
    """`domains` starting at position `day_index % len(domains)`, wrapping round."""
    items = list(domains)
    if not items:
        return items
    start = day_index % len(items)
    return items[start:] + items[:start]


def utc_day_index(now: datetime) -> int:
    return (now.astimezone(timezone.utc).date() - datetime(1970, 1, 1).date()).days


# ── the driver: domains -> pages -> one report ──────────────────────────────────────────────────


@dataclass
class Page:
    """One writer call's result. `next_cursor` None means the domain is walked to its end."""
    report: Dict[str, Any]
    next_cursor: Optional[str]
    aborted: bool = False


PageFn = Callable[[str, Optional[str]], Awaitable[Page]]
MergeFn = Callable[[Dict[str, Any], Dict[str, Any]], None]


@dataclass
class DomainResult:
    status: str = NOT_REACHED
    pages: int = 0
    elapsed_s: float = 0.0
    last_cursor: Optional[str] = None
    error: Optional[str] = None
    writer: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": self.status, "pages": self.pages,
                               "elapsed_s": round(self.elapsed_s, 1), "writer": self.writer}
        if self.last_cursor is not None:
            out["last_cursor"] = self.last_cursor
        if self.error is not None:
            out["error"] = self.error
        return out


async def drive(domains: Sequence[str], run_page: PageFn, merge: MergeFn, *, budget_s: float,
                gap_s: float, clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                emit: Optional[Callable[[str], None]] = None) -> Dict[str, DomainResult]:
    """Walk each domain page by page, in order, inside one budget. See the module docstring."""
    if not budget_s > 0:
        raise ValueError("budget_s must be positive")
    started = clock()
    results: Dict[str, DomainResult] = {d: DomainResult() for d in domains}
    first_call = True
    stop_all = False
    for domain in domains:
        if stop_all:
            break
        result = results[domain]
        domain_started = clock()
        after: Optional[str] = None
        while True:
            if clock() - started >= budget_s:
                # Every later domain meets this same check first and is recorded `not_reached`.
                result.status = BUDGET_STOPPED if result.pages else NOT_REACHED
                break
            if not first_call:
                await sleep(gap_s)
            first_call = False
            try:
                page = await run_page(domain, after)
            except Exception as exc:  # noqa: BLE001 - recorded against the domain; the pass goes on
                logger.exception("reap cart proof refresh: %s crashed", domain)
                result.status = CRASHED
                result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                break
            result.pages += 1
            merge(result.writer, page.report)
            if page.aborted:
                result.status = ABORTED
                stop_all = True
                break
            if page.next_cursor is None:
                result.status = DONE
                break
            if page.next_cursor == after:
                # The writer handed back the cursor it was given: another call would read the same
                # page forever. Never expected; stop the domain rather than spin until the timeout.
                result.status = CURSOR_STUCK
                break
            after = page.next_cursor
            result.last_cursor = after
        result.elapsed_s = clock() - domain_started
        if emit is not None and result.status != NOT_REACHED:
            emit(PROGRESS_PREFIX + json.dumps({"domain": domain, **result.as_dict()}, sort_keys=True,
                                              default=str))
    return results


def exit_code(results: Mapping[str, DomainResult]) -> int:
    statuses = {r.status for r in results.values()}
    if ABORTED in statuses:
        return EXIT_ABORTED_ON_BLOCK
    if statuses & {CRASHED, CURSOR_STUCK}:
        return EXIT_CRASHED
    if statuses & {BUDGET_STOPPED, NOT_REACHED}:
        return EXIT_BUDGET
    return EXIT_OK


# ── merging a writer's per-page report into the domain's ────────────────────────────────────────

#: The mirror writer's report keys that are plain counts or count maps, summed over pages.
_MIRROR_COUNTS = ("candidates", "rows_with_new_ids", "variant_ids_stamped", "write_conflicts")
_MIRROR_COUNT_MAPS = ("fetch_outcomes", "match_reasons", "cart_proofs", "most_blocked_domains")


def merge_mirror(total: Dict[str, Any], page: Dict[str, Any]) -> None:
    for key in _MIRROR_COUNTS:
        total[key] = total.get(key, 0) + int(page.get(key) or 0)
    for key in _MIRROR_COUNT_MAPS:
        bucket = total.setdefault(key, {})
        for name, count in (page.get(key) or {}).items():
            bucket[name] = bucket.get(name, 0) + int(count or 0)


def merge_enrichment(total: Dict[str, Any], page: Dict[str, Any]) -> None:
    """The enrichment writer's per-domain report is already a full account of its page; pages are
    kept side by side rather than summed (a domain is one page unless it has > 2,000 products)."""
    total.setdefault("pages", []).append(page)


# ── the two lanes: one writer call per page ─────────────────────────────────────────────────────


def mirror_domains(merchants_path: Optional[str] = None) -> List[str]:
    from services.tierb_cart_link_merchants import load_merchants

    return sorted({m.domain for m in load_merchants(merchants_path)})


def mirror_page_fn(backfill: Any, client: Any, *, apply: bool, page_size: int = MIRROR_PAGE_SIZE) -> PageFn:
    async def run_page(domain: str, after: Optional[str]) -> Page:
        report = await backfill.run(limit=page_size, domain=domain, apply=apply, client=client, after=after)
        aborted = bool(report.get("aborted_on_block"))
        # Fewer candidates than asked for: the domain is walked to its end.
        exhausted = int(report.get("candidates") or 0) < page_size
        cursor = None if (aborted or exhausted) else report.get("next_cursor")
        return Page(report=report, next_cursor=cursor, aborted=aborted)

    return run_page


def enrichment_page_fn(job: Any, db: Any, client: Any, plans: Mapping[str, Any], *, apply: bool,
                       pacer: Any) -> PageFn:
    async def run_page(domain: str, after: Optional[str]) -> Page:
        summary = await job.run(db, client, [plans[domain]], apply=apply, after=after, pacer=pacer)
        report = dict((summary.get("domains") or {}).get(domain) or {})
        aborted = bool(summary.get("aborted_on_block") or report.get("aborted_on_block"))
        cursor = None if (aborted or report.get("exhausted")) else report.get("next_cursor")
        return Page(report=report, next_cursor=cursor, aborted=aborted)

    return run_page


def inter_call_gap_s(lane: str, *, backfill: Any = None, job: Any = None) -> float:
    """The writer's own slowest spacing, applied between two calls (each builds a fresh pacer)."""
    if lane == "mirror":
        return max(float(backfill.GLOBAL_MIN_INTERVAL_S), float(backfill.PER_DOMAIN_MIN_GAP_S))
    return float(job.request_gap_s())


# ── main ────────────────────────────────────────────────────────────────────────────────────────


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lane", choices=LANES)
    parser.add_argument("--on-crawl-egress", action="store_true",
                        help="REQUIRED: this process egresses by the crawl subnet (pivota-crawl)")
    parser.add_argument("--budget-seconds", type=float, required=True,
                        help="wall-clock budget; once spent no new page starts")
    return parser.parse_args(argv)


def _emit(line: str) -> None:
    print(line, flush=True)


@dataclass
class LanePlan:
    """Everything a lane needs, resolved BEFORE the database or the network is touched, so a
    domain list the writer refuses is exit 2 with nothing attempted."""
    lane: str
    domains: List[str]
    writer: Any
    gap_s: float
    plans: Dict[str, Any] = field(default_factory=dict)


def plan_lane(lane: str, now: datetime) -> LanePlan:
    if lane == "mirror":
        from scripts import backfill_shopify_variant_ids as backfill

        return LanePlan(lane=lane, domains=rotate(mirror_domains(), utc_day_index(now)), writer=backfill,
                        gap_s=inter_call_gap_s(lane, backfill=backfill))
    import jobs.enrichment_cart_variant_proof as job

    plans = {p.domain: p for p in job.plan_domains(list(ENRICHMENT_DOMAINS))}
    return LanePlan(lane=lane, domains=list(plans), writer=job, gap_s=inter_call_gap_s(lane, job=job),
                    plans=plans)


async def run_lane(plan: LanePlan, *, apply: bool, budget_s: float,
                   emit: Callable[[str], None]) -> Dict[str, Any]:
    from db.database import database

    out: Dict[str, Any] = {"domains_order": plan.domains, "inter_call_gap_s": plan.gap_s}
    await database.connect()
    try:
        if plan.lane == "mirror":
            import httpx

            async with httpx.AsyncClient() as client:
                out["results"] = await drive(plan.domains, mirror_page_fn(plan.writer, client, apply=apply),
                                             merge_mirror, budget_s=budget_s, gap_s=plan.gap_s, emit=emit)
        else:
            job = plan.writer
            pacer = job.Pacer(job.request_gap_s())
            async with job.no_cookie_client() as client:
                out["results"] = await drive(
                    plan.domains, enrichment_page_fn(job, database, client, plan.plans, apply=apply, pacer=pacer),
                    merge_enrichment, budget_s=budget_s, gap_s=plan.gap_s, emit=emit)
            out["requests"] = pacer.requests
    finally:
        await database.disconnect()
    return out


def main(argv: Optional[List[str]] = None, *, environ: Optional[Mapping[str, str]] = None,
         emit: Callable[[str], None] = _emit) -> int:
    try:
        args = _parse(argv)
    except SystemExit as exc:  # argparse: a usage error is a bad argument, never a crash
        return EXIT_BAD_ARGS if exc.code else EXIT_OK
    if not args.on_crawl_egress:
        print("REAP_CART_PROOF_ERROR refusing to crawl without --on-crawl-egress: run on the "
              "pivota-crawl subnet and say so", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS
    if not (math.isfinite(args.budget_seconds) and args.budget_seconds > 0):
        print("REAP_CART_PROOF_ERROR --budget-seconds must be a positive number", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS
    apply = apply_enabled(environ)
    now = datetime.now(timezone.utc)
    started = time.monotonic()
    from services.tierb_cart_link_merchants import MerchantListError

    try:
        plan = plan_lane(args.lane, now)
    except MerchantListError as exc:
        print(f"REAP_CART_PROOF_ERROR {exc}", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS
    try:
        out = asyncio.run(run_lane(plan, apply=apply, budget_s=args.budget_seconds, emit=emit))
    except Exception as exc:  # noqa: BLE001 - the pass itself could not run (e.g. the DB is unreachable)
        logger.exception("reap cart proof refresh crashed")
        print(f"REAP_CART_PROOF_CRASH {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr, flush=True)
        return EXIT_CRASHED
    results: Dict[str, DomainResult] = out.pop("results")
    code = exit_code(results)
    report = {
        "lane": args.lane, "mode": "apply" if apply else "dry_run", "gate_env": GATE_ENV,
        "budget_s": args.budget_seconds, "elapsed_s": round(time.monotonic() - started, 1),
        "started_at": now.isoformat(), "exit_code": code,
        "status_counts": _status_counts(results),
        "domains": {d: r.as_dict() for d, r in results.items()},
        **out,
    }
    emit(REPORT_PREFIX + json.dumps(report, sort_keys=True, default=str))
    return code


def _status_counts(results: Mapping[str, DomainResult]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for result in results.values():
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
