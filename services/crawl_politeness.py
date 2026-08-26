"""Per-domain pacing, robots.txt compliance, and 429/503 backoff for outbound crawls.

WHY THIS EXISTS. `infra/gcp/setup_crawl_egress.sh` gives crawling its own subnet, NAT and
reserved IP so a burst cannot starve payment egress. That isolates the blast radius; it does not
reduce it. Everything crawled now leaves from ONE address per environment, so a single
unthrottled loop earns a per-IP block that takes the whole crawl lane down with it —
`docs/commerce-index-crawl-lane.md` makes robots + per-domain rate-limit explicit prerequisites
before any job is deployed onto that subnet.

WHAT IT GUARANTEES, per host:
  * at most one request every `CRAWL_MIN_INTERVAL_SECONDS` (default 1.0), or the host's own
    robots.txt `Crawl-delay` if that is longer — we take the more generous of the two, never ours;
  * no request AT ALL to a host asking for more than `CRAWL_MAX_ROBOTS_DELAY_SECONDS`
    (default 300) between requests — that host is skipped for the run, never crawled faster;
  * no request at all to a path the host's robots.txt disallows for our User-Agent;
  * exponential backoff after 429/503, honouring `Retry-After` when the host sends one.

NO LOCKS, DELIBERATELY. Pacing reserves its slot by reading and writing `next_allowed` with no
`await` between the two, which a single-threaded event loop makes atomic. A module-level
`asyncio.Lock` would bind to the first loop that CONTENDS it and then raise
`RuntimeError: bound to a different event loop` on every later one — the hazard documented
against the `db/*` `_DDL_LOCK`s, which survive only because nothing contends them. State here is
plain floats and dicts, so it is loop-agnostic by construction.

ROBOTS FAILURE MODE IS FAIL-OPEN, and that is a choice. A 404 means "no restrictions" and a 5xx
or a timeout means "we could not ask" — neither is consent, but treating an outage as a
disallowed crawl would let one flaky robots endpoint silently stop indexing a merchant. An
EXPLICIT `Disallow` always blocks. This matches the repo's existing `_robots_allows`
(services/brand_product_discovery.py) while fixing that helper's real defect: it asks about the
site ROOT, so a `Disallow: /products/` never bites the very paths we crawl.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

_MIN_INTERVAL_DEFAULT = 1.0
_ROBOTS_TTL_DEFAULT = 3600.0
_ROBOTS_TIMEOUT_DEFAULT = 5.0
_BACKOFF_BASE_DEFAULT = 2.0
_MAX_BACKOFF_DEFAULT = 300.0
_MAX_WAIT_DEFAULT = 10.0
# The longest `Crawl-delay` we will honour by WAITING. Deliberately the same number as
# `_MAX_BACKOFF_DEFAULT`, because it is the same policy: `note_response` already clamps a host's
# stated `Retry-After` at `CRAWL_MAX_BACKOFF_SECONDS` so a host cannot stall the lane
# indefinitely, and `Crawl-delay` was the one host-stated wait with no ceiling at all.
#
# THE TWO CLAMPS RESOLVE OPPOSITE WAYS, and that asymmetry is the point. An over-long
# `Retry-After` is clamped and we then crawl at the clamped rate — defensible, because
# `Retry-After` is a transient "not right now". An over-long `Crawl-delay` is a STANDING request
# about our crawl rate, so clamping it and crawling anyway would be crawling faster than the host
# asked. We skip the host for this run instead (`CrawlDelayTooLong`) and record why.
_MAX_ROBOTS_DELAY_DEFAULT = 300.0
_MAX_ROBOTS_REDIRECTS = 3
_MAX_ROBOTS_BYTES = 512 * 1024
# Both caches are keyed by a hostname that reaches us from an UNAUTHENTICATED body
# (`POST /api/offers/external/resolve` has no auth dependency), so they cannot be
# allowed to grow without bound. Measured ~500 B/host; this caps each at ~10k hosts.
_MAX_TRACKED_HOSTS = 10_000


class CrawlPaced(RuntimeError):
    """This host's next slot is further out than the caller is willing to wait.

    `_fetch_html` sits behind `POST /api/offers/external/resolve`, which has no auth dependency
    and is a live request path. Pacing there must never become an unbounded stall: a host
    advertising `Crawl-delay: 10`, or a queue of nine earlier callers, would otherwise hold a
    request open for as long as it took. Refusing is the honest answer — the caller already
    degrades to the cached snapshot, so the buyer gets slightly stale data instead of a hang, and
    the host still never sees a request faster than its stated rate.

    A batch job that genuinely wants to wait passes `max_wait=0` (unbounded).
    """


class CrawlDelayTooLong(CrawlPaced):
    """This host asks for a `Crawl-delay` longer than we are willing to serialise a batch on.

    A SUBCLASS OF `CrawlPaced` ON PURPOSE: every caller already treats "we did not get a slot"
    as "skip this row, keep the cached data, record nothing about the product", which is exactly
    the right handling here too. The distinct type exists so a caller that wants to log the
    REASON — or a future scheduler that wants to drop the host from the run entirely rather than
    re-deciding per row — can tell it apart from an ordinary queue-too-long refusal.

    Raised BEFORE the slot reservation in `await_slot`. Capping the sleep alone would be a
    non-fix: the reservation is what writes `next_allowed`, so a host advertising
    `Crawl-delay: 86400` would still push every remaining row on that host a full day out even
    if this one row declined to sleep.

    NOT a licence to crawl faster. The host asked for a rate we will not sustain, so we do not
    fetch it at all this run.
    """


class RobotsDisallowed(RuntimeError):
    """The host's robots.txt forbids this path for our User-Agent.

    A distinct type so a caller can tell "we were told not to" apart from "the fetch failed".
    The two look identical to a bare `except Exception` and mean opposite things: one is a
    permanent answer that should stop us retrying, the other is transient.
    """


def _f(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name) or default))
    except (TypeError, ValueError):
        return default


def _min_interval() -> float:
    return _f("CRAWL_MIN_INTERVAL_SECONDS", _MIN_INTERVAL_DEFAULT)


def _robots_enabled() -> bool:
    return str(os.getenv("CRAWL_ROBOTS_ENABLED", "true")).strip().lower() not in {
        "0", "false", "no", "off",
    }


@dataclass
class _DomainState:
    # Monotonic instant the next request to this host may START. Reserved ahead of the sleep, so
    # concurrent callers queue behind each other instead of all reading the same "now".
    next_allowed: float = 0.0
    backoff_until: float = 0.0
    consecutive_blocks: int = 0


_STATE: Dict[str, _DomainState] = {}
# host -> (expires_at_monotonic, parser or None, crawl_delay or None). A None parser is a
# positive cache of "asked, nothing to obey" — without it a site with no robots.txt would be
# re-asked on every single fetch.
_ROBOTS: Dict[str, Tuple[float, Optional[RobotFileParser], Optional[float]]] = {}
# host -> the future of the fetch currently in flight, plus the loop those futures belong to.
# Cleared whenever the running loop changes; see the note in _load_robots.
_ROBOTS_INFLIGHT: Dict[str, "asyncio.Future[None]"] = {}
_ROBOTS_INFLIGHT_LOOP: "Optional[asyncio.AbstractEventLoop]" = None


def host_of(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").strip().lower()
    except Exception:  # noqa: BLE001 - a malformed URL is not a crawl decision
        return ""


def _bounded(store: Dict[str, Any]) -> None:
    """Keep a host-keyed cache from growing without bound.

    Cleared wholesale rather than LRU-evicted: both caches are pure optimisations whose only cost
    on a miss is one extra robots fetch or one skipped pace, and an LRU here would be more moving
    parts than the problem deserves. The point is a ceiling, not a hit rate.
    """
    if len(store) > _MAX_TRACKED_HOSTS:
        logger.warning("crawl politeness cache exceeded %d hosts; clearing", _MAX_TRACKED_HOSTS)
        store.clear()


def reset_for_tests() -> None:
    """Drop all pacing and robots state. Tests only."""
    global _ROBOTS_INFLIGHT_LOOP
    _STATE.clear()
    _ROBOTS.clear()
    _ROBOTS_INFLIGHT.clear()
    _ROBOTS_INFLIGHT_LOOP = None


async def _load_robots(host: str, user_agent: str) -> Tuple[Optional[RobotFileParser], Optional[float]]:
    """Fetch + parse robots.txt for `host`, TTL-cached.

    SINGLE-FLIGHTED, and it has to be. "Two concurrent first-callers may both fetch" was the
    original reasoning and it was wrong at the shape that matters: the audit routes fan out with
    `asyncio.gather` over ~20 urls on ONE merchant host, all of which miss the cache in the same
    tick. That produced 20 simultaneous, entirely unpaced requests to that host — a burst emitted
    from the shared crawl egress IP by the very code meant to prevent per-IP bans.

    The in-flight future is stored beside the loop that created it and the map is dropped
    whenever the running loop changes. That is the shape proven in `utils/database_readiness.py`:
    a module-level future binds to one loop and awaiting a stale one HANGS rather than raising,
    and a WeakKeyDictionary keyed by loop leaks (the future holds `_loop`, a strong ref back to
    the weak key).

    Still not paced against the host's own interval: it is one request per host per TTL, and
    pacing it would mean the gate calling into itself.
    """
    global _ROBOTS_INFLIGHT_LOOP

    now = time.monotonic()
    cached = _ROBOTS.get(host)
    if cached and cached[0] > now:
        return cached[1], cached[2]

    loop = asyncio.get_running_loop()
    if _ROBOTS_INFLIGHT_LOOP is not loop:
        _ROBOTS_INFLIGHT.clear()
        _ROBOTS_INFLIGHT_LOOP = loop

    inflight = _ROBOTS_INFLIGHT.get(host)
    if inflight is not None and not inflight.done():
        # Adopt the leader's fetch rather than racing it. `shield` so this waiter's own
        # cancellation cannot cancel the shared future out from under its siblings.
        try:
            await asyncio.shield(inflight)
        except Exception:  # noqa: BLE001 - the leader logs; a follower just re-reads the cache
            pass
        again = _ROBOTS.get(host)
        if again:
            return again[1], again[2]
        return None, None

    leader = loop.create_future()
    _ROBOTS_INFLIGHT[host] = leader

    ttl = _f("CRAWL_ROBOTS_TTL_SECONDS", _ROBOTS_TTL_DEFAULT)
    parser: Optional[RobotFileParser] = None
    delay: Optional[float] = None
    cancelled = False
    try:
        timeout = _f("CRAWL_ROBOTS_TIMEOUT_SECONDS", _ROBOTS_TIMEOUT_DEFAULT)
        # `follow_redirects` is LOAD-BEARING, not incidental: 7 of 17 domains in the live cohort
        # serve robots.txt via at least one redirect, and without this the 3xx falls past the
        # `status_code == 200` check below and caches as "nothing to obey" — robots silently goes
        # dark for 41% of the cohort. `max_redirects` is capped because httpx applies `timeout`
        # PER HOP, so the default 20 would make this fetch's worst case ~20x the nominal timeout
        # on a request path that has no auth in front of it.
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=_MAX_ROBOTS_REDIRECTS,
            headers={"User-Agent": user_agent},
        ) as client:
            resp = await client.get(f"https://{host}/robots.txt")
        if resp.status_code == 200:
            parser = RobotFileParser()
            # Size-capped for the same reason `MAX_BODY_BYTES` caps the page fetch: an unbounded
            # `resp.text` here is memory an unauthenticated caller can ask a third party to spend.
            parser.parse(resp.text[:_MAX_ROBOTS_BYTES].splitlines())
            try:
                raw_delay = parser.crawl_delay(user_agent)
                delay = float(raw_delay) if raw_delay is not None else None
                # NaN would defeat the `Crawl-delay` cap in `await_slot` silently: every
                # comparison against it is False, so it would slip past the ceiling check and
                # then poison `next_allowed` (`start + nan` is nan, and `max(now, nan)` is nan
                # forever after) for every later caller on this host. `inf` deliberately does
                # NOT get this treatment — it is a finite question with an answer, and the cap
                # refuses it. Today's stdlib parser gates on `isdigit()` so neither value can
                # actually reach here; this is a guard on a value that is not ours, not a fix
                # for an observed bug.
                if delay is not None and math.isnan(delay):
                    delay = None
            except Exception:  # noqa: BLE001 - a malformed Crawl-delay is not a reason to stop
                delay = None
        # Any non-200 (404 = no robots, 5xx = could not ask) caches as "nothing to obey".
    except Exception as exc:  # noqa: BLE001
        logger.info("robots.txt unavailable for %s (%s); proceeding without restrictions", host, exc)
    except asyncio.CancelledError:
        # A CANCELLED FETCH LEARNED NOTHING, and must not be cached as "no restrictions".
        # `_load_robots` is reachable from a request path whose deadline is shorter than this
        # fetch (the live-verification hop), so cancellation here is ROUTINE — and writing the
        # negative entry would silently disable robots for this host for a full TTL, for this
        # lane and for the other lanes that share `_ROBOTS`.
        cancelled = True
        raise
    finally:
        # In a `finally` so a follower can NEVER be stranded. Awaiting a future that is never
        # resolved HANGS rather than raising, so an unexpected error anywhere above would wedge
        # every sibling waiter on that host instead of failing loudly. The cache write is the
        # only part skipped on cancellation — the release is not.
        if not cancelled:
            _bounded(_ROBOTS)
            _ROBOTS[host] = (now + ttl, parser, delay)
        if not leader.done():
            leader.set_result(None)
        _ROBOTS_INFLIGHT.pop(host, None)
    return parser, delay


async def robots_allows(url: str, *, user_agent: str) -> bool:
    """Is `url` — the FULL path, not the site root — crawlable for `user_agent`?"""
    if not _robots_enabled():
        return True
    host = host_of(url)
    if not host:
        return True
    parser, _delay = await _load_robots(host, user_agent)
    if parser is None:
        return True
    try:
        # The full URL, so a `Disallow: /products/` is actually consulted for a product page.
        # The old helper asked about `base + "/"` and therefore never saw path rules at all.
        return bool(parser.can_fetch(user_agent, url))
    except Exception:  # noqa: BLE001
        return True


def _robots_delay_cap() -> float:
    """Longest `Crawl-delay` we will wait out. `<= 0` disables the cap entirely."""
    return _f("CRAWL_MAX_ROBOTS_DELAY_SECONDS", _MAX_ROBOTS_DELAY_DEFAULT)


async def await_slot(url: str, *, user_agent: str, max_wait: Optional[float] = None) -> None:
    """Sleep until this host may be hit again, then reserve the slot.

    `max_wait` bounds the stall; `None` uses `CRAWL_MAX_WAIT_SECONDS`, and an explicit `0` (or
    any non-positive value) means unbounded. Exceeding it raises `CrawlPaced` WITHOUT reserving
    — reserving a slot we then abandon would push every later caller out for a request that
    never happened.

    A host asking for a `Crawl-delay` longer than `CRAWL_MAX_ROBOTS_DELAY_SECONDS` raises
    `CrawlDelayTooLong` regardless of `max_wait`, because the damage is not the sleep — it is
    the INTERVAL, which is written into `next_allowed` and paid again by every later row on
    that host.
    """
    host = host_of(url)
    if not host:
        return
    interval = _min_interval()
    _parser, robots_delay = await _load_robots(host, user_agent) if _robots_enabled() else (None, None)
    if robots_delay is not None:
        cap = _robots_delay_cap()
        if cap > 0 and robots_delay > cap:
            # BEFORE the reservation, and independent of `max_wait`. `max_wait=0` (the ten
            # `max_wait=0` call sites) removed the only ceiling this value ever had, and the
            # cost COMPOUNDS: each row reserves `next_allowed = start + interval`, so on a host
            # serving `Crawl-delay: 86400` row 2 slept 86399.99998s, row 3 slept 172799.99999s,
            # and row N waits N-1 days. Reproduced by construction against ff589e4e during
            # review of #1898/#1899.
            #
            # We refuse rather than clamp. See `CrawlDelayTooLong` — crawling a host at a rate
            # it explicitly asked us not to use is not the fix for our own batch being slow.
            raise CrawlDelayTooLong(
                f"{host} asks for Crawl-delay {robots_delay:.1f}s, over the {cap:.1f}s cap "
                f"(CRAWL_MAX_ROBOTS_DELAY_SECONDS); skipping this host rather than crawling it "
                f"faster than it asked"
            )
        # The host's own stated delay wins whenever it is SLOWER. Taking min() here would let a
        # site asking for 10s be hit every second while technically "having a robots check".
        interval = max(interval, robots_delay)

    _bounded(_STATE)
    state = _STATE.setdefault(host, _DomainState())
    now = time.monotonic()
    start = max(now, state.next_allowed, state.backoff_until)

    if max_wait is None:
        # AN ENV CEILING OF 0 MEANS "NEVER WAIT", NOT "WAIT FOREVER". `max_wait=0` is the
        # caller-side sentinel for unbounded, and reading the env through the same `> 0` test
        # gave `CRAWL_MAX_WAIT_SECONDS=0` — which an operator would set meaning "do not stall my
        # request path" — the exact opposite effect, on the UNAUTHENTICATED
        # `POST /api/offers/external/resolve` of all places. The sentinel's inverted polarity is
        # pre-existing and left alone (it is load-bearing at ten call sites); only the env
        # reading, which no caller opted into, is corrected here.
        ceiling, unbounded = _f("CRAWL_MAX_WAIT_SECONDS", _MAX_WAIT_DEFAULT), False
    else:
        ceiling = float(max_wait)
        unbounded = ceiling <= 0
    if not unbounded and (start - now) > ceiling:
        # Checked BEFORE the reservation below, deliberately.
        raise CrawlPaced(
            f"{host} next free in {start - now:.1f}s, over the {ceiling:.1f}s the caller allows"
        )
    # Reserve BEFORE sleeping. Read-then-write with no await between them is atomic on one loop,
    # so N concurrent callers take N distinct slots instead of all waking at the same instant.
    state.next_allowed = start + interval
    delay = start - now
    if delay > 0:
        await asyncio.sleep(delay)


def note_response(url: str, status_code: int, *, retry_after: Optional[str] = None) -> None:
    """Feed a response back in so the next request to this host is paced accordingly."""
    host = host_of(url)
    if not host:
        return
    # Bounded here too, not only in await_slot: note_response CREATES state, and a caller that
    # only ever records responses (or one whose requests are all refused) would otherwise grow
    # this cache past the ceiling without await_slot ever running.
    _bounded(_STATE)
    state = _STATE.setdefault(host, _DomainState())

    if status_code not in (429, 503):
        # Any other answer — including a 404 — means the host is still talking to us.
        state.consecutive_blocks = 0
        state.backoff_until = 0.0
        return

    state.consecutive_blocks += 1
    base = _f("CRAWL_BACKOFF_BASE_SECONDS", _BACKOFF_BASE_DEFAULT)
    ceiling = _f("CRAWL_MAX_BACKOFF_SECONDS", _MAX_BACKOFF_DEFAULT)
    wait = min(ceiling, base * (2 ** max(0, state.consecutive_blocks - 1)))

    # A host that tells us how long to wait is more authoritative than our own curve — but only
    # ever to LENGTHEN it. The `max` is what guarantees that: a shorter Retry-After than our own
    # backoff is ignored, so a host cannot talk us INTO hammering it. (`_parse_retry_after`
    # additionally rejects a non-positive value, which is belt-and-braces — the `max` already
    # neutralises a `Retry-After: 0`. Stating this precisely because an earlier comment credited
    # the guard with preventing a hot loop it does not, in fact, prevent.)
    parsed = _parse_retry_after(retry_after)
    if parsed is not None:
        wait = max(wait, min(ceiling, parsed))

    state.backoff_until = time.monotonic() + wait
    logger.warning(
        "crawl backoff: %s returned %s (consecutive=%d), holding %.1fs",
        host, status_code, state.consecutive_blocks, wait,
    )


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """`Retry-After` as seconds. Only the delta-seconds form; an HTTP-date returns None.

    A date form would need clock-skew handling to be trustworthy, and getting that wrong turns a
    polite hint into either a hot loop or an unbounded stall. Falling back to our own curve is
    the safe reading.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


async def before_request(
    url: str, *, user_agent: str, max_wait: Optional[float] = None
) -> None:
    """Gate one outbound crawl request.

    Raises `RobotsDisallowed` (told not to), `CrawlPaced` (would wait too long), or returns once
    it is this host's turn.
    """
    if not await robots_allows(url, user_agent=user_agent):
        raise RobotsDisallowed(f"robots.txt disallows {url}")
    await await_slot(url, user_agent=user_agent, max_wait=max_wait)
