"""Daily Tier B cart-link eligibility: which Shopify merchants accept our cart-permalink checkout.

    python -m jobs.tierb_cart_link_eligibility               # record verdicts (needs the gate)
    python -m jobs.tierb_cart_link_eligibility --dry-run     # print verdicts, write nothing
    python -m jobs.tierb_cart_link_eligibility --dry-run --only judydoll.com

For each merchant on `config/tierb_cart_link_merchants.json` (Pivota's own list, validated by
services/tierb_cart_link_merchants.py) it runs services/shopify_cart_link_preflight.preflight in
that row's market, WITH NO BUYER — the Reap path's shape, where the buyer travels in Reap's quote
body and never in the link — retries once when the result is `retryable`, and records the
outcome with db/tierb_cart_link_eligibility.record_result (definite verdicts overwrite; an
indefinite one never does).

── THIS IS NOT A SCHEDULER JOB, AND MUST NEVER BECOME ONE ──────────────────────────────────

Do NOT register this in services/audit_scheduler.py. Every `_add_job` job runs on the `worker`
service, whose egress is the default NAT, 8.231.167.230 — THE ADDRESS PAYMENT PARTNERS
ALLOWLIST. Crawling merchants from it is forbidden: NAT port exhaustion is per-IP, and ~50
requests over 37 Cloudflare-fronted domains in ~1 minute once tripped a cross-domain, IP-level
429 lasting ~15 minutes. This runs as its own Cloud Run JOB on subnet `pivota-crawl` (NAT
34.82.199.35), triggered daily by Cloud Scheduler — infra/gcp/setup_tierb_cart_link_eligibility_job.sh,
docs/runbooks/tierb_cart_link_eligibility.md.

── PACING ──────────────────────────────────────────────────────────────────────────────────

  * ONE limiter across every merchant: request STARTS are at least 1.5 s apart
    (`MIN_REQUEST_INTERVAL_S`, a floor the caller cannot lower). It wraps the HTTP transport, so
    it sees every request the preflight makes — catalog pages and redirect hops included — not
    one per merchant.
  * At most 3 merchants in flight (`MAX_CONCURRENCY`); each preflight is sequential inside.
  * A wall-clock budget (default 20 min). Once it is spent no new request starts. A merchant not
    yet begun, or cut short mid-preflight (which yields no result), is reported `budget_stopped`
    and nothing is written for it: a row it already has keeps its verdict and ages out on its own
    clock. A merchant whose RETRY was cut short keeps its first (indefinite) result.

── THE GATE ────────────────────────────────────────────────────────────────────────────────

`TIERB_CART_LINK_ELIGIBILITY_ENABLED` is read INSIDE the job on every run, dry-run included (a
dry run still creates checkouts). Only 1/true/yes/on (any case) turn it on; anything else,
unset included, is off: the job logs at WARNING that it did nothing and exits 0.

── SIDE EFFECT ─────────────────────────────────────────────────────────────────────────────

Each preflight that reaches the permalink creates ONE ABANDONED SHOPIFY CHECKOUT on that
merchant's store (two on a retried attempt), carrying our click id and no buyer data. Nothing is
paid and no payment step is reached.

── LOGGING ─────────────────────────────────────────────────────────────────────────────────

No full cart URL is ever logged or printed: the preflight redacts its own, the HTTP client's
loggers are held at WARNING while this runs, and the one URL this module prints (the landing,
in the per-merchant line) goes through `redact_cart_permalink` first.

── EXIT CODE (highest applicable wins) ─────────────────────────────────────────────────────

  2  the merchant list is invalid — nothing was attempted
  4  a result could not be recorded, or a preflight raised unexpectedly
  3  the budget ran out before every merchant produced a result
  1  more than a quarter of the checked merchants ended INDEFINITE (after their retry) — the
     shape of a systemic failure (the crawl address blocked, a proxy down), not of one flaky store
  0  otherwise (or the gate is off). A few indefinite merchants are normal: their prior verdicts
     were kept, and each one is listed at WARNING.

A non-zero exit fails the Cloud Run execution, which is what the "Cloud Run job failing" alert
watches, so a single flaky storefront deliberately does NOT page. The job is created with
`--max-retries 0`: a failed execution is never re-run automatically, because a re-run re-crawls
every merchant and creates another round of abandoned checkouts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

import httpx

from db.tierb_cart_link_eligibility import DEFINITE_VERDICTS
from services.outbound_links_service import redact_cart_permalink
from services.shopify_cart_link_preflight import (
    REQUEST_TIMEOUT_S,
    PreflightResult,
    Verdict,
    preflight,
)
from services.tierb_cart_link_merchants import (
    Merchant,
    MerchantListError,
    load_merchants,
    select_merchants,
)

logger = logging.getLogger(__name__)

GATE_ENV = "TIERB_CART_LINK_ELIGIBILITY_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

MIN_REQUEST_INTERVAL_S = 1.5
MAX_CONCURRENCY = 3
DEFAULT_BUDGET_S = 20 * 60
RETRY_DELAY_S = 2.0
MAX_ATTEMPTS = 2  # the first try plus ONE retry, and only for a `retryable` result

EXIT_OK = 0
EXIT_INDEFINITE = 1
EXIT_BAD_MERCHANT_LIST = 2
EXIT_BUDGET = 3
EXIT_RECORD_FAILED = 4
# More than this fraction of checked merchants ending indefinite is a systemic failure (exit 1).
INDEFINITE_ALARM_FRACTION = 0.25

_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")

PreflightFn = Callable[..., Awaitable[PreflightResult]]
RecordFn = Callable[[str, str, PreflightResult], Awaitable[Any]]
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[Any]]


def gate_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Strict: exactly 1/true/yes/on after trimming, any case. Everything else is off."""
    env = os.environ if environ is None else environ
    raw = env.get(GATE_ENV)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value:
        logger.warning("%s=%r is not a recognised truthy value; treating it as off", GATE_ENV, raw)
    return False


# ── pacing ──────────────────────────────────────────────────────────────────────────────────


class BudgetExhausted(Exception):
    """The run's wall-clock budget is spent; no further request may start. Deliberately NOT an
    httpx.TransportError: the preflight would turn that into TRANSPORT_ERROR, and a merchant we
    stopped asking about is not a merchant whose store failed to answer."""


class RequestPacer:
    """Spaces request STARTS at least `min_interval_s` apart across every caller that shares it.

    Waiters queue on one lock and are released in arrival order. The clock and the sleep are
    injectable so the spacing can be proven with a fake clock. With a `deadline` (a value of
    `clock`), a request that could only start at or after it raises BudgetExhausted instead of
    waiting."""

    def __init__(
        self,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        deadline: Optional[float] = None,
    ) -> None:
        self.min_interval_s = max(float(min_interval_s), MIN_REQUEST_INTERVAL_S)
        self._clock = clock
        self._sleep = sleep
        self.deadline = deadline
        self._lock = asyncio.Lock()
        self._next_start: Optional[float] = None
        self.starts: List[float] = []

    def expired(self) -> bool:
        return self.deadline is not None and self._clock() >= self.deadline

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._next_start is not None and now < self._next_start:
                if self.deadline is not None and self._next_start >= self.deadline:
                    raise BudgetExhausted("the next request slot is past the budget")
                await self._sleep(self._next_start - now)
                now = self._clock()
            if self.deadline is not None and now >= self.deadline:
                raise BudgetExhausted("the run's budget is spent")
            self.starts.append(now)
            self._next_start = now + self.min_interval_s


class PacedTransport(httpx.AsyncBaseTransport):
    """An httpx transport that waits for the pacer before handing each request to `inner`."""

    def __init__(self, inner: httpx.AsyncBaseTransport, pacer: RequestPacer) -> None:
        self._inner = inner
        self._pacer = pacer

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._pacer.acquire()
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _default_inner_transport() -> httpx.AsyncBaseTransport:
    """Direct, unless the process has an HTTPS proxy configured (an operator's laptop). Passing
    our own transport switches off httpx's environment-proxy lookup, so it is honoured here
    explicitly. The proxy URL is never logged (it can carry credentials)."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
    if proxy:
        logger.info("tierb eligibility: using the process HTTPS proxy")
    return httpx.AsyncHTTPTransport(proxy=proxy)


# ── one merchant ────────────────────────────────────────────────────────────────────────────


def click_id_for(domain: str, stamp: str) -> str:
    return f"clk_tierbelig_{stamp}_{re.sub(r'[^a-z0-9]+', '_', domain.lower()).strip('_')}"


@dataclass
class MerchantOutcome:
    merchant: Merchant
    result: Optional[PreflightResult] = None
    attempts: int = 0
    status: str = "pending"  # checked | budget_stopped | crashed
    recorded: Optional[bool] = None  # None on a dry run or when there was nothing to record
    error: Optional[str] = None


async def _check_merchant(
    merchant: Merchant,
    *,
    client: httpx.AsyncClient,
    pacer: RequestPacer,
    preflight_fn: PreflightFn,
    sleep: Sleep,
    retry_delay_s: float,
    stamp: str,
) -> MerchantOutcome:
    outcome = MerchantOutcome(merchant=merchant)
    while outcome.attempts < MAX_ATTEMPTS:
        if pacer.expired():
            break
        outcome.attempts += 1
        try:
            result = await preflight_fn(
                merchant.domain,
                market=merchant.market,
                variant_id=merchant.variant_id,
                product_handle=merchant.product_handle,
                quantity=1,
                buyer=None,  # NEVER a buyer: the Reap path carries no PII in the link
                click_id=click_id_for(merchant.domain, stamp),
                client=client,
            )
        except BudgetExhausted:
            outcome.attempts -= 1  # cut short: no result, so not an attempt we can use
            break
        except Exception as exc:  # noqa: BLE001 — a bug in the preflight must not stop the run
            outcome.status, outcome.error = "crashed", type(exc).__name__
            return outcome
        outcome.result = result
        if not result.retryable:
            break
        if outcome.attempts < MAX_ATTEMPTS:
            await sleep(retry_delay_s)
    outcome.status = "checked" if outcome.result is not None else "budget_stopped"
    return outcome


# ── the run ─────────────────────────────────────────────────────────────────────────────────


@dataclass
class RunSummary:
    gate_enabled: bool
    dry_run: bool
    exit_code: int = EXIT_OK
    merchants: int = 0
    checked: int = 0
    definite: int = 0
    indefinite: int = 0
    budget_stopped: int = 0
    crashed: int = 0
    record_failures: int = 0
    requests: int = 0
    elapsed_s: float = 0.0
    counts: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None
    outcomes: List[MerchantOutcome] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gate_enabled": self.gate_enabled,
            "dry_run": self.dry_run,
            "exit_code": self.exit_code,
            "merchants": self.merchants,
            "checked": self.checked,
            "definite": self.definite,
            "indefinite": self.indefinite,
            "budget_stopped": self.budget_stopped,
            "crashed": self.crashed,
            "record_failures": self.record_failures,
            "requests": self.requests,
            "elapsed_s": round(self.elapsed_s, 1),
            "counts": dict(sorted(self.counts.items())),
            "error": self.error,
        }


def _exit_code(summary: RunSummary) -> int:
    if summary.error:
        return EXIT_BAD_MERCHANT_LIST
    if summary.record_failures or summary.crashed:
        return EXIT_RECORD_FAILED
    if summary.budget_stopped:
        return EXIT_BUDGET
    if summary.checked and summary.indefinite / summary.checked > INDEFINITE_ALARM_FRACTION:
        return EXIT_INDEFINITE
    return EXIT_OK


def outcome_line(outcome: MerchantOutcome) -> str:
    """One line per merchant. The landing URL is passed through `redact_cart_permalink` even
    though the preflight already redacted it: this is the one URL this module prints."""
    m, r = outcome.merchant, outcome.result
    if r is None:
        return f"{m.domain:22} {m.market:3} {outcome.status.upper():26} attempts={outcome.attempts} error={outcome.error or '-'}"
    landing = redact_cart_permalink(r.final_url) if r.final_url else "-"
    return (
        f"{m.domain:22} {m.market:3} {r.verdict.value:26} retryable={'Y' if r.retryable else 'n'} "
        f"attempts={outcome.attempts} variant={r.variant_id or '-'}({r.variant_source or '-'}) "
        f"final={r.final_status or '-'} landing={landing} detail={r.detail or '-'} "
        f"recorded={'-' if outcome.recorded is None else ('Y' if outcome.recorded else 'FAILED')}"
    )


async def run(
    *,
    dry_run: bool = False,
    merchants: Optional[Sequence[Merchant]] = None,
    only: Optional[Sequence[str]] = None,
    budget_s: float = DEFAULT_BUDGET_S,
    concurrency: int = MAX_CONCURRENCY,
    min_interval_s: float = MIN_REQUEST_INTERVAL_S,
    retry_delay_s: float = RETRY_DELAY_S,
    preflight_fn: PreflightFn = preflight,
    record_fn: Optional[RecordFn] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    environ: Optional[Mapping[str, str]] = None,
    emit: Optional[Callable[[str], None]] = None,
    stamp: Optional[str] = None,
) -> RunSummary:
    """One eligibility pass. See the module docstring for the rules; everything that touches
    the outside world (the preflight, the recorder, the transport, the clock) is injectable."""
    emit = emit or _emit_line
    summary = RunSummary(gate_enabled=gate_enabled(environ), dry_run=dry_run)
    if not summary.gate_enabled:
        logger.warning(
            "tierb cart-link eligibility is disabled (%s is not truthy): exiting without "
            "contacting any merchant or writing anything", GATE_ENV,
        )
        return summary

    try:
        chosen = select_merchants(list(merchants) if merchants is not None else load_merchants(), only)
    except (MerchantListError, ValueError, OSError) as exc:
        summary.error = f"{type(exc).__name__}: {exc}"
        summary.exit_code = _exit_code(summary)
        logger.error("tierb cart-link eligibility: merchant list refused: %s", summary.error)
        return summary
    summary.merchants = len(chosen)

    if not budget_s > 0:
        raise ValueError("budget_s must be positive")
    started = clock()
    pacer = RequestPacer(min_interval_s, clock=clock, sleep=sleep, deadline=started + float(budget_s))
    gate = asyncio.Semaphore(max(1, min(int(concurrency), MAX_CONCURRENCY)))
    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    recorder: Optional[RecordFn] = None
    if not dry_run:
        if record_fn is None:
            from db.tierb_cart_link_eligibility import record_result as record_fn  # noqa: PLC0415
        recorder = record_fn

    paced = PacedTransport(transport if transport is not None else _default_inner_transport(), pacer)
    async with httpx.AsyncClient(transport=paced, timeout=REQUEST_TIMEOUT_S, follow_redirects=False) as client:

        async def one(merchant: Merchant) -> MerchantOutcome:
            async with gate:
                outcome = await _check_merchant(
                    merchant, client=client, pacer=pacer, preflight_fn=preflight_fn,
                    sleep=sleep, retry_delay_s=retry_delay_s, stamp=stamp,
                )
                if recorder is not None and outcome.result is not None:
                    try:
                        await recorder(merchant.domain, merchant.market, outcome.result)
                        outcome.recorded = True
                    except Exception as exc:  # noqa: BLE001 — count it, keep going, exit non-zero
                        outcome.recorded = False
                        outcome.error = type(exc).__name__
                        logger.error(
                            "tierb eligibility: could not record %s %s: %s",
                            merchant.domain, merchant.market, type(exc).__name__,
                        )
            emit(outcome_line(outcome))
            return outcome

        outcomes = list(await asyncio.gather(*(one(m) for m in chosen)))

    summary.outcomes = outcomes
    summary.requests = len(pacer.starts)
    summary.elapsed_s = clock() - started

    for outcome in outcomes:
        if outcome.status == "crashed":
            summary.crashed += 1
            summary.counts["CRASHED"] = summary.counts.get("CRASHED", 0) + 1
            continue
        if outcome.result is None:
            summary.budget_stopped += 1
            summary.counts["BUDGET_STOPPED"] = summary.counts.get("BUDGET_STOPPED", 0) + 1
            continue
        summary.checked += 1
        verdict = outcome.result.verdict
        summary.counts[verdict.value] = summary.counts.get(verdict.value, 0) + 1
        if verdict in DEFINITE_VERDICTS:
            summary.definite += 1
        else:
            summary.indefinite += 1
        if outcome.recorded is False:
            summary.record_failures += 1
    for outcome in outcomes:
        if outcome.result is not None and outcome.result.verdict not in DEFINITE_VERDICTS:
            logger.warning(
                "tierb eligibility: %s %s ended %s (%s); its prior verdict, if any, was kept",
                outcome.merchant.domain, outcome.merchant.market, outcome.result.verdict.value,
                outcome.result.detail or "-",
            )
    summary.exit_code = _exit_code(summary)
    return summary


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────


def _emit_line(line: str) -> None:
    # Flushed per line: a Cloud Run job's stdout is a pipe, and a block-buffered line that is
    # still in the buffer when the task is killed at its timeout is a merchant nobody sees.
    print(line, flush=True)


def _quiet_http_client_logs() -> None:
    """httpx logs every request URL at INFO. The preflight redacts those while it runs, but this
    job has no reason to emit them at all."""
    for name in _HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


async def _main_async(args: argparse.Namespace) -> RunSummary:
    if args.dry_run or not gate_enabled():
        return await run(dry_run=args.dry_run, only=args.only, budget_s=args.budget_seconds)
    from db.database import database  # noqa: PLC0415
    from db.tierb_cart_link_eligibility import ensure_schema  # noqa: PLC0415

    connected_here = not database.is_connected
    if connected_here:
        await database.connect()
    try:
        await ensure_schema()
        return await run(dry_run=False, only=args.only, budget_s=args.budget_seconds)
    finally:
        if connected_here:
            await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print verdicts; write nothing")
    parser.add_argument("--only", action="append", default=None, metavar="DOMAIN",
                        help="restrict to these listed domains (repeatable)")
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_S,
                        help=f"wall-clock budget (default {DEFAULT_BUDGET_S}s)")
    args = parser.parse_args(argv)
    if not args.budget_seconds > 0:
        parser.error("--budget-seconds must be positive")

    logging.basicConfig(level=logging.INFO)
    _quiet_http_client_logs()
    summary = asyncio.run(_main_async(args))
    print("summary", json.dumps(summary.to_dict(), sort_keys=True), flush=True)
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
