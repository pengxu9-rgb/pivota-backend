"""One aggregate request budget for Shopify-served hosts, shared by every crawl job on the crawl egress IP.

WHY. On 2026-09-30 Shopify's shared edge throttled our single crawl-egress address (Cloud NAT
pivota-crawl-nat, 34.82.199.35) across EVERY shop from ~00:00Z: the external-referral refresh took
1,177 429s over 331 hosts, and the destination sweep, the Tier B eligibility job and the hourly
purchasability sweep all failed with it. Every one of those jobs paces PER HOST
(`services/crawl_politeness`: >= 1s between requests to one host) or per RUN (the Tier B
`RequestPacer`: 1.5s between request starts), and they run as separate Cloud Run processes. The edge
counts every shop reached from one IP as ONE client, so the number it throttles on — the aggregate —
was invisible to all of them. The 09-29 refresh alone ran ~4 req/s for 55 minutes (4 host lanes at
~1 req/s each), most of it to Shopify storefronts.

WHAT THIS GUARANTEES, when `CRAWL_SHOPIFY_EDGE_PACER_ENABLED` is on in every crawl process:
  * request STARTS to hosts known to be Shopify-served are spaced so that, across every process that
    shares the database, they do not exceed `CRAWL_SHOPIFY_EDGE_RPS` (default 2.0, clamped to
    [0.1, 20]) on average, with no stored burst: an idle bucket hands out slots from "now", never
    from the past, and a slot a full interval in the past is dropped rather than spent;
  * each process reserves slots in LEASES with ONE single-row upsert
    (db/crawl_egress_pacer.lease_slots) and spends them locally;
  * a host we do not know to be Shopify-served is NOT paced here at all — it still gets
    `crawl_politeness`'s per-host pacing, exactly as before.

It is ADDITIVE to the per-host gate, never a replacement (see `crawl_politeness.await_slot`).

LEASES ARE SIZED BY DEMAND, NOT FIXED. A leased slot nobody uses is budget no other process can have
(the schedule is shared; an unused slot just expires). A fixed lease of 10 let the purchasability
sweep (one request per ~3s) reserve 10 slots and use ~3, pushing the refresh back. So a lease is
`n = waiters + grants in the last second`, capped at `CRAWL_SHOPIFY_EDGE_LEASE` (default 10, max
20), never below 1. A low-demand
process leases one slot at a time and wastes nothing; a busy one leases about a second's worth.

WORST-CASE DATABASE LOAD. A process leases only when it holds no usable slot, and n slots cannot be
used up (or expire) in less than n / rate seconds, so leases are at least 1 / rate apart. Because
n >= 1 + (grants in the trailing second), a process that leased under a second ago has granted at
least one slot since and leases at least 2. In steady state that is <= max(1, rate / max_lease) statements/s per process whatever its demand (a process
needing D req/s leases about D+waiters slots, one statement per ~second; 1/s at the defaults), and
a single-slot lease is only ever issued by a process granting under one request a second. Hard
ceiling: `rate` statements/s per process (2/s at the default), reachable only by pathological
start/stop churn. Each statement is a primary-key
upsert of one row. A DB failure costs one attempt per `_DB_RETRY_SECONDS` (30s) per process.

BOUNDED WAITS. A shared wait can never grow without limit, whatever the row, the clocks or the env:
  * the rate is clamped to [0.1, 20] req/s and a lease to at most 20 slots, so one lease spans at
    most 200s;
  * the lease statement never starts a lease more than `horizon` seconds after the DB's "now": a
    schedule further out than that is a row poisoned by a typo'd rate, a DB clock step or a process
    running a different rate, and it restarts at "now" — the row HEALS on its next lease instead of
    stalling every later run;
  * a lease that still comes back further out than the horizon is treated as a DB failure: ERROR,
    local pacing, and the DB retried after 30s.
  * a process never holds more than TWO max leases of future time: `refill` does not lease while
    this process has already handed a caller a slot more than `max_lease / rate` ahead (the
    lookahead cap), so a burst of K concurrent callers waits in the process instead of reserving
    K slots of shared future.
The horizon is `max(60, 16 * max_lease / rate)` seconds (80s at the defaults, 3,200s at the most
extreme settings, rate 0.1 with lease 20): room for 8 processes each holding its two-lease cap,
so a legitimate backlog never looks poisoned.

EVERY PROCESS MUST USE THE SAME `CRAWL_SHOPIFY_EDGE_RPS`. The rate is not stored in the row: each
lease advances the shared schedule by `n / (that process's rate)`. One job set to 0.1 reserves 10s
per slot on the shared timeline and drags every other job down with it (the reviewer measured the
refresh at 0.18 req/s beside a 0.1 process). Set the rate once, identically, on every crawl job.

HOW A HOST BECOMES "SHOPIFY-SERVED", decided before the request:
  * by its lane: the Tier B and purchasability sweeps check Shopify merchants only (their preflight
    is Shopify's cart permalink), so their transport (`PacedTransport(shopify_edge=True)`) paces
    every request without consulting the host list;
  * learned from a previous response (`learn_from_response`, at the refresh's and the destination
    sweep's fetch sites): `x-shopid`, `x-shopify-stage`, `x-sorting-hat-shopid`,
    `x-storefront-renderer-rendered`, or `powered-by: Shopify`, keyed by the host that ANSWERED
    (the final URL after redirects). The first request to an unknown host is therefore unpaced by
    this budget (it still has the per-host one). No seed or merchant row carries a platform field
    for external seeds (external_product_seeds has none), so the learned cache is the general path.
    It is per-process and bounded (10k hosts). `mark_shopify_host` is for a caller that knows: the
    curated brand feed / retailer-ingest drain marks its Shopify-by-construction endpoints (#2477).

FAIL OPEN, LOUDLY AND SLOWLY. If the lease statement fails, takes longer than
`CRAWL_SHOPIFY_EDGE_DB_TIMEOUT_SECONDS` (3s), or returns an out-of-horizon lease, the process logs at
ERROR and paces Shopify hosts at a LOCAL rate, `CRAWL_SHOPIFY_EDGE_FALLBACK_RPS` (default a quarter
of the shared rate), and retries the database after 30s. It never stops the crawl and never runs
unpaced. THE FALLBACK IS ONLY WITHIN BUDGET WHEN EVERY PROCESS FAILS TOGETHER (pivota-pg down:
4 x rate/4 = rate). A process that fails ALONE runs its local rate ON TOP of the others' shared
budget, so the edge sees up to rate + fallback (2.5 req/s at the defaults) for up to 30s at a time.
Local slots and DB slots are separate schedules; a DB lease after a local run keeps its DB times
(shifting it later would move it into windows other processes hold).

FLAG OFF IS THE OLD CODE PATH. With `CRAWL_SHOPIFY_EDGE_PACER_ENABLED` unset every entry point
returns before touching any state: no DB statement, no learned host, no sleep.

ONLY SET THE FLAG ON JOBS THAT LEAVE FROM THE CRAWL EGRESS. The budget models ONE IP. A process on
another egress (the web service, the worker) would spend the crawl IP's budget for requests the edge
attributes to a different client.

NO LOCKS, like `crawl_politeness`: taking a slot is a read-then-pop with no `await` between them,
which one event loop makes atomic; the lease itself is single-flighted with a future stored beside
the loop that created it (the `_load_robots` pattern), so a new loop never awaits a stale future.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, Mapping, Optional, Set, Tuple

from services.crawl_politeness import CrawlPaced, host_of

logger = logging.getLogger(__name__)

ENABLED_ENV = "CRAWL_SHOPIFY_EDGE_PACER_ENABLED"
RATE_ENV = "CRAWL_SHOPIFY_EDGE_RPS"
LEASE_ENV = "CRAWL_SHOPIFY_EDGE_LEASE"
FALLBACK_RATE_ENV = "CRAWL_SHOPIFY_EDGE_FALLBACK_RPS"
DB_TIMEOUT_ENV = "CRAWL_SHOPIFY_EDGE_DB_TIMEOUT_SECONDS"

BUCKET = "shopify_edge"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_RATE_DEFAULT = 2.0
_RATE_MIN = 0.1  # a typo'd 0.001 would otherwise mean one request per 1,000s
_RATE_MAX = 20.0
_LEASE_DEFAULT = 10
_LEASE_MAX = 20
_DEMAND_WINDOW = 1.0
_HORIZON_MIN = 60.0
_FALLBACK_SHARE = 0.25
_DB_TIMEOUT_DEFAULT = 3.0
_DB_RETRY_SECONDS = 30.0
_MAX_KNOWN_HOSTS = 10_000

_SHOPIFY_HEADERS = frozenset(
    {"x-shopid", "x-shopify-stage", "x-sorting-hat-shopid", "x-storefront-renderer-rendered"}
)


class EdgePaced(CrawlPaced):
    """The shared Shopify-edge slot is further out than the caller will wait.

    A `CrawlPaced`, so every existing caller already handles it as "we did not get a slot".
    Raised WITHOUT consuming the slot: the next caller gets it.
    """


class LeaseOutOfBounds(RuntimeError):
    """The database handed back a lease further out than the horizon allows."""


LeaseFn = Callable[..., Awaitable[Tuple[float, float]]]


async def _db_lease(
    bucket: str, *, slots: int, rate_per_s: float, horizon_s: float
) -> Tuple[float, float]:
    from db.crawl_egress_pacer import lease_slots  # noqa: PLC0415 - keeps db out of import time

    return await lease_slots(bucket, slots=slots, rate_per_s=rate_per_s, horizon_s=horizon_s)


#: Seams. Tests swap the lease (a stubbed or real DB), the clock and the sleep.
_lease_fn: LeaseFn = _db_lease
_monotonic: Callable[[], float] = time.monotonic
_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep

_slots: Deque[float] = deque()
_grants: Deque[float] = deque()  # when slots were granted, trailing `_DEMAND_WINDOW` only
_waiting = 0  # callers currently waiting on a lease refill
_granted_until = 0.0  # the furthest-out slot this process has handed to a caller (monotonic)
_local_next = 0.0  # the local fallback schedule's own horizon (monotonic)
_db_down_until = 0.0
_inflight: "Optional[asyncio.Future[None]]" = None
_inflight_loop: "Optional[asyncio.AbstractEventLoop]" = None
_announced = False
_known: Set[str] = set()


def _zero_stats() -> Dict[str, float]:
    return {
        "granted": 0, "waited_seconds": 0.0, "leases": 0, "leased_slots": 0, "db_errors": 0,
        "local_slots": 0, "expired_slots": 0, "refused": 0, "hosts_learned": 0, "hosts_marked": 0,
        "held_back_seconds": 0.0,
    }


_stats: Dict[str, float] = _zero_stats()


# ── configuration, read per call (an operator can flip the env on the job without a rebuild) ──


def enabled() -> bool:
    return str(os.getenv(ENABLED_ENV) or "").strip().lower() in _TRUTHY


def _number(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def rate() -> float:
    """Shared request starts per second, clamped to [0.1, 20]: a typo cannot stall every crawl for
    hours (0.001 -> 0.1) nor switch pacing off (1e9 -> 20)."""
    return min(_RATE_MAX, max(_RATE_MIN, _number(RATE_ENV, _RATE_DEFAULT)))


def lease_size() -> int:
    """The LARGEST lease one statement may take; the actual size follows demand (`_lease_size`)."""
    try:
        raw = int(float(os.getenv(LEASE_ENV) or _LEASE_DEFAULT))
    except (TypeError, ValueError, OverflowError):
        raw = _LEASE_DEFAULT
    return max(1, min(_LEASE_MAX, raw))


def fallback_rate() -> float:
    # Never faster than the shared rate: failing open must not be a way to crawl faster.
    return min(rate(), _number(FALLBACK_RATE_ENV, rate() * _FALLBACK_SHARE))


def db_timeout() -> float:
    return _number(DB_TIMEOUT_ENV, _DB_TIMEOUT_DEFAULT)


def horizon() -> float:
    """How far past the DB's "now" a lease may start; see BOUNDED WAITS."""
    return max(_HORIZON_MIN, 16.0 * lease_size() / rate())


# ── which hosts are Shopify-served ──────────────────────────────────────────────────────────


def _remember(host: str) -> bool:
    if not host or host in _known:
        return False
    if len(_known) >= _MAX_KNOWN_HOSTS:
        logger.warning("shopify edge pacer: host cache exceeded %d; clearing", _MAX_KNOWN_HOSTS)
        _known.clear()
    _known.add(host)
    return True


def mark_shopify_host(host_or_url: str) -> None:
    """For a caller that KNOWS this host is Shopify-served: the curated brand feed / retailer-ingest
    drain marks the Shopify-by-construction endpoints it crawls (/products.json, /products/<h>.js,
    /meta.json, the Markets capture) before the first request (#2477)."""
    if not enabled():
        return
    raw = str(host_or_url or "")
    host = host_of(raw) if "://" in raw else raw.strip().lower()
    if _remember(host):
        _stats["hosts_marked"] += 1


def looks_shopify_served(headers: Optional[Mapping[str, Any]]) -> bool:
    """Response headers Shopify's storefront edge sets. Keys compared case-insensitively."""
    if not headers:
        return False
    try:
        items = list(headers.items())
    except Exception:  # noqa: BLE001 - an odd headers object is not evidence
        return False
    for key, value in items:
        name = str(key).strip().lower()
        if name in _SHOPIFY_HEADERS:
            return True
        if name == "powered-by" and "shopify" in str(value).lower():
            return True
    return False


def learn_from_response(url: str, headers: Optional[Mapping[str, Any]]) -> bool:
    """Remember `url`'s host as Shopify-served when its response says so. True if newly learned.

    Pass the URL that ANSWERED (the final one after redirects): the headers are that host's."""
    if not enabled() or not looks_shopify_served(headers):
        return False
    if _remember(host_of(url)):
        _stats["hosts_learned"] += 1
        return True
    return False


def is_shopify_host(host: str) -> bool:
    return str(host or "").strip().lower() in _known


def applies(url: str) -> bool:
    """Does the shared budget gate a request to `url`? False, with no side effect, when off."""
    return enabled() and is_shopify_host(host_of(url))


# ── the budget ──────────────────────────────────────────────────────────────────────────────


def _trim_grants(now: float) -> None:
    while _grants and _grants[0] <= now - _DEMAND_WINDOW:
        _grants.popleft()


def _lease_size(now: float) -> int:
    """Demand-sized: the callers waiting now plus the slots granted in the last second (about a
    second of this process's demand), within [1, lease_size()]."""
    _trim_grants(now)
    return max(1, min(lease_size(), _waiting + len(_grants)))


def _local_slots(count: int) -> None:
    global _local_next
    step = 1.0 / fallback_rate()
    start = max(_monotonic(), _local_next)
    for i in range(count):
        _slots.append(start + i * step)
    _local_next = start + count * step
    _stats["local_slots"] += count


async def _lease() -> None:
    global _db_down_until, _announced
    now = _monotonic()
    r = rate()
    n = _lease_size(now)
    if now < _db_down_until:
        _local_slots(n)
        return
    reach = horizon()
    try:
        start_db, db_now = await asyncio.wait_for(
            _lease_fn(BUCKET, slots=n, rate_per_s=r, horizon_s=reach), timeout=db_timeout()
        )
        offset = float(start_db) - float(db_now)
        # `not <=` so a NaN is out of bounds too. +1s: the statement reads the clock more than once.
        if not offset <= reach + 1.0:
            raise LeaseOutOfBounds(
                f"lease starts {offset:.0f}s after the DB's now, past the {reach:.0f}s horizon"
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail open, but slowly and loudly
        _db_down_until = _monotonic() + _DB_RETRY_SECONDS
        _stats["db_errors"] += 1
        logger.error(
            "shopify edge pacer: the shared budget is unreachable (%s: %s); pacing Shopify-served "
            "hosts LOCALLY at %.2f req/s for the next %.0fs. The aggregate across crawl jobs is NOT "
            "enforced while this lasts.",
            type(exc).__name__, str(exc)[:200], fallback_rate(), _DB_RETRY_SECONDS,
        )
        _local_slots(n)
        return
    # The DB's own times, not shifted: moving a DB lease later (say past a local fallback run)
    # would move it into windows other processes hold. `max(0, ...)` only absorbs clock jitter.
    base = _monotonic() + max(0.0, offset)
    for i in range(n):
        _slots.append(base + i / r)
    _stats["leases"] += 1
    _stats["leased_slots"] += n
    if not _announced:
        _announced = True
        logger.warning(
            "shopify edge pacer ON: bucket=%s rate=%.2f req/s max_lease=%d fallback=%.2f req/s "
            "horizon=%.0fs", BUCKET, r, lease_size(), fallback_rate(), reach,
        )


async def refill() -> None:
    """Lease more slots, single-flighted per process: concurrent callers adopt the leader's lease.
    Every caller in here counts as a waiter for the lease size."""
    global _inflight, _inflight_loop, _waiting
    _waiting += 1
    try:
        loop = asyncio.get_running_loop()
        if _inflight_loop is not loop:
            _inflight = None
            _inflight_loop = loop
        if _inflight is not None and not _inflight.done():
            try:
                await asyncio.shield(_inflight)
            except Exception:  # noqa: BLE001 - the leader logs; a follower just re-checks the slots
                pass
            return
        leader = loop.create_future()
        _inflight = leader
        try:
            # THE LOOKAHEAD CAP. `take_nowait` hands a future slot to a caller at once, so K
            # concurrent callers would otherwise hold K slots of future time, and four processes
            # bursting 50 callers each outran the horizon: the SQL heal then restarted the schedule
            # at "now" underneath slots already handed out (3.17 req/s against 2.0 in the
            # reviewer's simulation). No new lease while this process has already handed out a slot
            # more than one max lease (max_lease / rate) ahead; the waiters wait here instead. A
            # process therefore never holds more than two max leases of future time.
            ahead = _granted_until - _monotonic() - lease_size() / rate()
            if ahead > 0:
                _stats["held_back_seconds"] += ahead
                await _sleep(ahead)
            await _lease()
        finally:
            # In a `finally` so a follower is never stranded on a future nobody resolves.
            if not leader.done():
                leader.set_result(None)
            if _inflight is leader:
                _inflight = None
    finally:
        _waiting -= 1


def take_nowait(*, max_wait: Optional[float] = None) -> Optional[float]:
    """Take this process's next slot WITHOUT awaiting; return its start time (monotonic).

    None when no slot is held (the caller awaits `refill()` and asks again). A slot further out than
    `max_wait` raises `EdgePaced` and is NOT taken. Expired slots are dropped first: spending one
    late would be a burst the shared schedule never granted.
    """
    global _granted_until
    now = _monotonic()
    grace = 1.0 / rate()
    while _slots and _slots[0] < now - grace:
        _slots.popleft()
        _stats["expired_slots"] += 1
    if not _slots:
        return None
    slot = _slots[0]
    wait = slot - now
    if max_wait is not None and wait > max_wait:
        _stats["refused"] += 1
        raise EdgePaced(
            f"the shared Shopify-edge budget's next slot is {wait:.1f}s out, over the "
            f"{max_wait:.1f}s the caller allows"
        )
    _slots.popleft()
    _granted_until = max(_granted_until, slot)
    _stats["granted"] += 1
    _stats["waited_seconds"] += max(0.0, wait)
    _trim_grants(now)
    _grants.append(now)
    return slot


async def acquire(*, max_wait: Optional[float] = None) -> float:
    """Wait for this process's next shared slot and take it. Returns seconds waited.

    `max_wait=None` waits as long as the budget needs (a batch). A number is the caller's patience:
    a slot further out raises `EdgePaced` without being taken.
    """
    # The patience is a DEADLINE: time spent held back by the lookahead cap or waiting on a lease
    # refill counts against it.
    deadline = None if max_wait is None else _monotonic() + max_wait
    while True:
        slot = take_nowait(max_wait=None if deadline is None else deadline - _monotonic())
        if slot is None:
            await refill()
            continue
        wait = slot - _monotonic()
        if wait > 0:
            await _sleep(wait)
        return max(0.0, wait)


async def acquire_for_url(url: str, *, max_wait: Optional[float] = None) -> float:
    """0.0 and no side effect unless the flag is on and `url`'s host is known Shopify-served."""
    if not applies(url):
        return 0.0
    return await acquire(max_wait=max_wait)


def stats() -> Dict[str, Any]:
    """Counts for a job's summary line. Hosts are counted, never listed."""
    out: Dict[str, Any] = dict(_stats)
    out["waited_seconds"] = round(float(out["waited_seconds"]), 1)
    out["held_back_seconds"] = round(float(out["held_back_seconds"]), 1)
    out.update(enabled=enabled(), rate=rate(), max_lease=lease_size(),
               fallback_rate=fallback_rate(), known_hosts=len(_known))
    return out


def reset_for_tests() -> None:
    """Drop all pacer state and restore the seams. Tests only."""
    global _slots, _grants, _waiting, _granted_until, _local_next, _db_down_until
    global _inflight, _inflight_loop, _announced, _stats, _lease_fn, _monotonic, _sleep
    _slots = deque()
    _grants = deque()
    _waiting = 0
    _granted_until = 0.0
    _local_next = 0.0
    _db_down_until = 0.0
    _inflight = None
    _inflight_loop = None
    _announced = False
    _known.clear()
    _stats = _zero_stats()
    _lease_fn = _db_lease
    _monotonic = time.monotonic
    _sleep = asyncio.sleep
