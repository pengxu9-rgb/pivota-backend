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
    shares the database, they never exceed `CRAWL_SHOPIFY_EDGE_RPS` (default 2.0) on average, with
    no stored burst: an idle bucket hands out slots from "now", never from the past;
  * each process reserves slots in LEASES of `CRAWL_SHOPIFY_EDGE_LEASE` (default 10) with ONE
    single-row upsert (db/crawl_egress_pacer.lease_slots) and spends them locally;
  * a host we do not know to be Shopify-served is NOT paced here at all — it still gets
    `crawl_politeness`'s per-host pacing, exactly as before.

It is ADDITIVE to the per-host gate, never a replacement: `crawl_politeness.await_slot` takes the
host's own slot first and this budget second, so a request waits for whichever is later.

HOW A HOST BECOMES "SHOPIFY-SERVED", decided before the request:
  * a caller that knows (`mark_shopify_host`): the Tier B and purchasability sweeps check Shopify
    merchants only (their preflight is Shopify's cart permalink), and a `/products.json` catalogue
    read is a Shopify endpoint by construction;
  * learned from a previous response (`learn_from_response`): `x-shopid`, `x-shopify-stage`,
    `x-sorting-hat-shopid`, `x-storefront-renderer-rendered`, or `powered-by: Shopify`. The first
    request to an unknown host is therefore unpaced by this budget (it still has the per-host one);
    every later one is paced. No seed or merchant row carries a platform field for external seeds
    (external_product_seeds has none), so the learned cache is the general path. It is per-process
    and bounded (10k hosts).

WORST-CASE DATABASE LOAD. A process leases only when it has no usable slot left, and a leased run of
N slots cannot be spent faster than the slots' own spacing (N / rate seconds). So ONE process issues
at most rate / (N - 1) leases per second in steady state even if its slots expire unused (a slot is
dropped only once it is a full interval in the past). With rate 2.0 and N 10 that is <= 0.22 lease
statements/s per process; four crawl processes at once is <= 0.9 statements/s, each a primary-key
upsert of one row (tens of microseconds of CPU). A DB failure costs one attempt per
`_DB_RETRY_SECONDS` (30s) per process, not one per request.

FAIL OPEN, LOUDLY, AND SLOWLY. If the lease statement fails or takes longer than
`CRAWL_SHOPIFY_EDGE_DB_TIMEOUT_SECONDS` (3s), the process logs at ERROR and paces Shopify hosts at a
LOCAL rate, `CRAWL_SHOPIFY_EDGE_FALLBACK_RPS` (default a quarter of the shared rate, so four
processes all failing open together still stay inside the shared budget), and retries the database
after 30s. It never stops the crawl and never runs unpaced.

FLAG OFF IS THE OLD CODE PATH. With `CRAWL_SHOPIFY_EDGE_PACER_ENABLED` unset every entry point
returns before touching any state: no DB statement, no learned host, no sleep.

ONLY SET THE FLAG ON JOBS THAT LEAVE FROM THE CRAWL EGRESS. The budget models ONE IP. A process on
another egress (the web service, the worker) would spend the crawl IP's budget for requests the edge
attributes to a different client.

NO LOCKS, like `crawl_politeness`: slot reservation is a read-then-pop with no `await` between them,
which one event loop makes atomic; the lease itself is single-flighted with a future stored beside
the loop that created it (the `_load_robots` pattern), so a new loop never awaits a stale future.
"""

from __future__ import annotations

import asyncio
import logging
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
_LEASE_DEFAULT = 10
_LEASE_MIN = 2  # the DB-load bound divides by (N - 1)
_LEASE_MAX = 50
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


LeaseFn = Callable[..., Awaitable[Tuple[float, float]]]


async def _db_lease(bucket: str, *, slots: int, rate_per_s: float) -> Tuple[float, float]:
    from db.crawl_egress_pacer import lease_slots  # noqa: PLC0415 - keeps db out of import time

    return await lease_slots(bucket, slots=slots, rate_per_s=rate_per_s)


#: Seams. Tests swap the lease (a stubbed or real DB), the clock and the sleep.
_lease_fn: LeaseFn = _db_lease
_monotonic: Callable[[], float] = time.monotonic
_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep

_slots: Deque[float] = deque()
_horizon = 0.0  # no slot of this process may start before this (monotonic)
_db_down_until = 0.0
_inflight: "Optional[asyncio.Future[None]]" = None
_inflight_loop: "Optional[asyncio.AbstractEventLoop]" = None
_announced = False
_known: Set[str] = set()
_stats: Dict[str, float] = {}


def _zero_stats() -> Dict[str, float]:
    return {
        "granted": 0, "waited_seconds": 0.0, "leases": 0, "db_errors": 0, "local_slots": 0,
        "expired_slots": 0, "refused": 0, "hosts_learned": 0, "hosts_marked": 0,
    }


_stats = _zero_stats()


# ── configuration, read per call (an operator can flip the env on the job without a rebuild) ──


def enabled() -> bool:
    return str(os.getenv(ENABLED_ENV) or "").strip().lower() in _TRUTHY


def _positive(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default
    # NaN and inf fail `0 < value < inf`; a non-positive rate would divide by zero or never pace.
    return value if 0 < value < float("inf") else default


def rate() -> float:
    return _positive(RATE_ENV, _RATE_DEFAULT)


def lease_size() -> int:
    try:
        raw = int(float(os.getenv(LEASE_ENV) or _LEASE_DEFAULT))
    except (TypeError, ValueError, OverflowError):
        raw = _LEASE_DEFAULT
    return max(_LEASE_MIN, min(_LEASE_MAX, raw))


def fallback_rate() -> float:
    # Never faster than the shared rate: failing open must not be a way to crawl faster.
    return min(rate(), _positive(FALLBACK_RATE_ENV, rate() * _FALLBACK_SHARE))


def db_timeout() -> float:
    return _positive(DB_TIMEOUT_ENV, _DB_TIMEOUT_DEFAULT)


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
    """A caller that KNOWS this host is Shopify-served (a Shopify-only lane, a `/products.json`)."""
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
    """Remember `url`'s host as Shopify-served when its response says so. True if newly learned."""
    if not enabled() or not looks_shopify_served(headers):
        return False
    if _remember(host_of(url)):
        _stats["hosts_learned"] += 1
        return True
    return False


def is_shopify_host(host: str) -> bool:
    return str(host or "").strip().lower() in _known


# ── the budget ──────────────────────────────────────────────────────────────────────────────


def _append_slots(base: float, count: int, step: float) -> None:
    """Queue `count` slots `step` apart from `base`, never before this process's horizon.

    Moving a slot LATER only ever lowers the rate, so the horizon clamp is always safe; it stops a
    DB lease that lands right behind a local fallback run from starting two requests back to back.
    """
    global _horizon
    start = max(base, _horizon)
    for i in range(count):
        _slots.append(start + i * step)
    _horizon = start + count * step


def _local_slots(count: int) -> None:
    fr = fallback_rate()
    _append_slots(_monotonic(), count, 1.0 / fr)
    _stats["local_slots"] += count


async def _lease() -> None:
    global _db_down_until, _announced
    n, r = lease_size(), rate()
    if _monotonic() < _db_down_until:
        _local_slots(n)
        return
    try:
        start_db, db_now = await asyncio.wait_for(
            _lease_fn(BUCKET, slots=n, rate_per_s=r), timeout=db_timeout()
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
    received = _monotonic()
    _append_slots(received + (float(start_db) - float(db_now)), n, 1.0 / r)
    _stats["leases"] += 1
    if not _announced:
        _announced = True
        logger.warning(
            "shopify edge pacer ON: bucket=%s rate=%.2f req/s lease=%d fallback=%.2f req/s",
            BUCKET, r, n, fallback_rate(),
        )


async def _refill() -> None:
    """Lease more slots, single-flighted per process: concurrent callers adopt the leader's lease."""
    global _inflight, _inflight_loop
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
        await _lease()
    finally:
        # In a `finally` so a follower is never stranded on a future nobody resolves.
        if not leader.done():
            leader.set_result(None)
        if _inflight is leader:
            _inflight = None


async def acquire(*, max_wait: Optional[float] = None) -> float:
    """Wait for this process's next shared Shopify-edge slot and take it. Returns seconds waited.

    `max_wait=None` waits as long as the budget needs (a batch). A number is the caller's remaining
    patience: a slot further out than that raises `EdgePaced` without being taken.

    Callers normally go through `acquire_for_url`, which is a no-op unless the flag is on AND the
    host is known to be Shopify-served.
    """
    while True:
        now = _monotonic()
        grace = 1.0 / rate()
        # A slot a full interval in the past is dropped, not spent: spending it late would be a
        # burst the shared schedule never granted.
        while _slots and _slots[0] < now - grace:
            _slots.popleft()
            _stats["expired_slots"] += 1
        if _slots:
            wait = _slots[0] - now
            if max_wait is not None and wait > max_wait:
                _stats["refused"] += 1
                raise EdgePaced(
                    f"the shared Shopify-edge budget's next slot is {wait:.1f}s out, over the "
                    f"{max_wait:.1f}s the caller allows"
                )
            # Taken BEFORE the sleep, with no await between the check and the pop.
            _slots.popleft()
            _stats["granted"] += 1
            if wait > 0:
                _stats["waited_seconds"] += wait
                await _sleep(wait)
            return max(0.0, wait)
        await _refill()


async def acquire_for_url(url: str, *, max_wait: Optional[float] = None) -> float:
    """The gate a crawl request calls. 0.0 and no side effect unless the flag is on and `url`'s host
    is known to be Shopify-served."""
    if not enabled():
        return 0.0
    if not is_shopify_host(host_of(url)):
        return 0.0
    return await acquire(max_wait=max_wait)


def stats() -> Dict[str, Any]:
    """Counts for a job's summary line. Hosts are counted, never listed."""
    out: Dict[str, Any] = dict(_stats)
    out["waited_seconds"] = round(float(out["waited_seconds"]), 1)
    out.update(enabled=enabled(), rate=rate(), lease=lease_size(), fallback_rate=fallback_rate(),
               known_hosts=len(_known))
    return out


def reset_for_tests() -> None:
    """Drop all pacer state and restore the seams. Tests only."""
    global _slots, _horizon, _db_down_until, _inflight, _inflight_loop, _announced, _stats
    global _lease_fn, _monotonic, _sleep
    _slots = deque()
    _horizon = 0.0
    _db_down_until = 0.0
    _inflight = None
    _inflight_loop = None
    _announced = False
    _known.clear()
    _stats = _zero_stats()
    _lease_fn = _db_lease
    _monotonic = time.monotonic
    _sleep = asyncio.sleep
