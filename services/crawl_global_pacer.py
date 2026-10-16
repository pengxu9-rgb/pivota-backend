"""One run's GLOBAL origin-request pacer: a token bucket every worker shares, a cap on how many NEW
hosts the run may start on per minute, and a cool-down pause the IP breaker can call.

WHY THIS EXISTS. `crawl_politeness` paces each HOST (one request per `CRAWL_MIN_INTERVAL_SECONDS`)
and `--host-concurrency` caps hosts in flight, but nothing capped the IP's TOTAL rate. Every crawl
leaves from one NAT address (`pivota-crawl-nat`), and the nightly `external-referral-refresh` read
8,155-13,156 rows per 3,300 s run between 2026-10-08 and 10-15 (2.5-4.0 origin reads/s, four
workers) from it. On 2026-10-16 Cloudflare challenge-listed that address: run tm899 (05:15Z) took
391 `cf-mitigated: challenge` 429s with a peak of 262 distinct hosts in one 60 s window, and the
14:00Z rerun zt5zz drew 359 challenges from 363 responses across 334 hosts. A challenged request
answers instantly, so four "polite" workers swept hundreds of hosts a minute: the per-host gate saw
one request per host, and nothing saw the IP.

WHAT IT GUARANTEES, while installed (`installed()`), for every request that passes through
`crawl_politeness` (`await_slot`, and the robots.txt fetch `_load_robots` makes):
  * REQUEST STARTS ARE SPACED by `1 / rps` seconds on average, each gap drawn uniformly from
    [1 - jitter, 1 + jitter] of it. A token bucket of depth ONE, implemented as a reserved next
    start (the GCRA form), so there is never a burst, not even after an idle stretch. Retries go
    through the same funnel and pay the same price;
  * AT MOST `max_new_hosts_per_minute` hosts are contacted for the first time in any 60 s window,
    however fast they answer;
  * NOTHING STARTS during a cool-down (`pause`), and a request whose reserved start fell inside one
    re-queues behind it instead of going out late into it;
  * after `stop()` nothing starts at all: `acquire` raises `GlobalPaceStopped`.

It changes only WHEN a request starts, never whether the per-host rules apply: the per-host
interval, robots, backoff and the Shopify-edge pacer (`services.shopify_edge_pacer`) all still run.

NO LOCKS, the same way `crawl_politeness` has none: every reservation reads and writes its state
with no `await` in between, which one event loop makes atomic, and the state is plain floats and
dicts, so it is loop-agnostic. Module-level installation, not a ContextVar, for the reason
`crawl_ip_throttle` gives: the refresh runs its workers in fresh contexts.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Deque, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

NEW_HOST_WINDOW_SECONDS = 60.0
JITTER_DEFAULT = 0.3
# Past this many hosts the run stops remembering NEW ones as admitted, so each further one is
# admitted again (re-counted against the per-minute cap): over-counting, never under-counting.
# A refresh run is capped at 30,000 rows.
_MAX_TRACKED_HOSTS = 50_000
_MAX_RECORDED_GRANTS = 10_000


def _now() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _random() -> float:
    return random.random()


def resolve_setting(explicit: Any, env_name: str, default: float) -> float:
    """A run's pacing knob: the flag wins, then `env_name`, then `default`. A non-finite or negative
    value falls back to the default, loudly: a typo must never silently mean "unpaced". 0 is the
    explicit off switch."""
    raw: Any = explicit
    source = "argument"
    if raw is None:
        raw = (os.getenv(env_name) or "").strip() or None
        source = env_name
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or value < 0:
        logger.warning("%s=%r is not a non-negative number; using the default %s", source, raw, default)
        return default
    return value


class GlobalPaceStopped(RuntimeError):
    """The run's pacer was stopped (the IP breaker's final trip, or the run's budget ends inside a
    cool-down). No request was sent."""


def _finite_non_negative(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out) or out < 0:
        return default
    return out


class GlobalOriginPacer:
    """See the module docstring. `rps <= 0` disables the rate; `max_new_hosts_per_minute <= 0`
    disables the new-host cap. Both off is a pacer that only ever pauses or stops."""

    def __init__(
        self,
        *,
        rps: float,
        max_new_hosts_per_minute: int,
        jitter: float = JITTER_DEFAULT,
        deadline: Optional[float] = None,
        hard_deadline: bool = False,
    ) -> None:
        self.initial_rps = _finite_non_negative(rps, 0.0)
        self.rps = self.initial_rps
        self.max_new_hosts_per_minute = max(0, int(max_new_hosts_per_minute or 0))
        # Clamped to [0, 0.9]: a jitter of 1 would allow a zero gap, i.e. a burst.
        self.jitter = min(max(_finite_non_negative(jitter, JITTER_DEFAULT), 0.0), 0.9)
        # A monotonic instant past which no cool-down may be waited out (the run's budget).
        self.deadline = deadline
        # OPT-IN: with `hard_deadline`, no request STARTS at or after `deadline` either; the first
        # one that would stops the pacer. For a run with no other way to stop work already in
        # progress (the destination sweep reads a whole catalogue per host). The refresh stops
        # starting ROWS at its budget instead and leaves this off.
        self.hard_deadline = bool(hard_deadline) and deadline is not None
        self.paused_until = 0.0
        self.stopped = False
        self.stop_reason: Optional[str] = None
        self._next_start = 0.0
        # host -> the instant it was (or will be) first contacted.
        self._admitted: Dict[str, float] = {}
        self._admissions: Deque[float] = deque(maxlen=max(1, self.max_new_hosts_per_minute))
        # Counters for the run summary.
        self.requests = 0
        self.new_hosts = 0
        self.paces_waited_seconds = 0.0
        self.cooldowns = 0
        self.cooldown_seconds = 0.0
        self.grants: List[float] = []

    # ── configuration the breaker changes mid-run ─────────────────────────────────────────────

    def set_rate(self, rps: float) -> None:
        self.rps = _finite_non_negative(rps, self.rps)

    def pause(self, seconds: float) -> float:
        """Start a cool-down: nothing starts for `seconds`. Returns the instant it ends."""
        seconds = _finite_non_negative(seconds, 0.0)
        now = _now()
        until = now + seconds
        if until > self.paused_until:
            self.cooldowns += 1
            self.cooldown_seconds += until - max(now, self.paused_until)
            self.paused_until = until
        return self.paused_until

    def stop(self, reason: str = "stopped") -> None:
        self.stopped = True
        self.stop_reason = reason

    # ── the two rules ─────────────────────────────────────────────────────────────────────────

    def _gap(self) -> float:
        if self.rps <= 0:
            return 0.0
        base = 1.0 / self.rps
        if not self.jitter:
            return base
        return base * (1.0 + self.jitter * (2.0 * _random() - 1.0))

    def _next_admission(self, now: float) -> float:
        """The earliest instant a host not yet contacted could be admitted. Pure."""
        cap = self.max_new_hosts_per_minute
        if cap <= 0:
            return now
        at = now
        if self._admissions:
            at = max(at, self._admissions[-1])
        if len(self._admissions) >= cap:
            at = max(at, self._admissions[0] + NEW_HOST_WINDOW_SECONDS)
        return at

    def is_admitted(self, host: str) -> bool:
        return str(host or "").strip().lower() in self._admitted

    def new_host_wait(self) -> float:
        """Seconds until one more new host could be admitted (0 when one could be now)."""
        now = _now()
        return max(0.0, self._next_admission(now) - now)

    def admit(self, host: str) -> float:
        """Reserve `host`'s first contact. Idempotent: returns the instant already reserved for a
        host seen before. Atomic (no await), so concurrent callers take distinct admissions."""
        key = str(host or "").strip().lower()
        now = _now()
        if not key:
            return now
        known = self._admitted.get(key)
        if known is not None:
            return known
        at = self._next_admission(now)
        if self.max_new_hosts_per_minute > 0:
            # `maxlen` keeps exactly the last `cap` admissions, which is all the rule reads.
            self._admissions.append(at)
        if len(self._admitted) < _MAX_TRACKED_HOSTS:
            self._admitted[key] = at
        self.new_hosts += 1
        return at

    # ── the gate ──────────────────────────────────────────────────────────────────────────────

    def _check_stopped(self) -> None:
        if self.stopped:
            raise GlobalPaceStopped(f"crawl pacer stopped ({self.stop_reason}); request not sent")

    async def acquire(self, host: str) -> float:
        """Wait for this request's turn. Returns the seconds waited (pacing plus any cool-down)."""
        waited = 0.0
        while True:
            self._check_stopped()
            now = _now()
            if self.paused_until > now:
                if self.deadline is not None and self.paused_until >= self.deadline:
                    # Waiting out the cool-down would run past the run's budget (and, with a
                    # 3,300 s budget, into the 3,600 s task timeout that kills the run before it
                    # prints its summary). Refuse instead.
                    self.stop("budget ends inside the cool-down")
                    self._check_stopped()
                delay = self.paused_until - now
                waited += delay
                await _sleep(delay)
                continue
            # RESERVE, THEN SLEEP: both reservations happen here with no await between them.
            admitted_at = self.admit(host)
            start = max(now, admitted_at, self._next_start)
            if self.hard_deadline and start >= self.deadline:
                self.stop("budget spent")
                self._check_stopped()
            self._next_start = start + self._gap()
            delay = start - now
            if delay > 0:
                waited += delay
                self.paces_waited_seconds += delay
                await _sleep(delay)
            self._check_stopped()
            if self.paused_until > _now():
                # A trip landed while this request waited for its turn. It does not go out into
                # the cool-down: it waits the cool-down out and takes a fresh slot after it.
                continue
            self.requests += 1
            if len(self.grants) < _MAX_RECORDED_GRANTS:
                self.grants.append(start)
            return waited

    def summary(self) -> Dict[str, Any]:
        return {
            "global_rps": self.initial_rps,
            "final_rate": self.rps,
            "max_new_hosts_per_minute": self.max_new_hosts_per_minute,
            "global_jitter": self.jitter,
            "paced_requests": self.requests,
            "new_hosts_contacted": self.new_hosts,
            # Summed over workers, so it can exceed wall-clock time with concurrency > 1.
            "paces_waited_seconds": round(self.paces_waited_seconds, 3),
            "cooldowns": self.cooldowns,
            "cooldown_seconds": round(self.cooldown_seconds, 3),
            "pacer_stopped": self.stop_reason,
        }


_ACTIVE: List[GlobalOriginPacer] = []


@contextmanager
def installed(pacer: GlobalOriginPacer) -> Iterator[GlobalOriginPacer]:
    """Gate every `crawl_politeness` request on `pacer` for this block."""
    _ACTIVE.append(pacer)
    try:
        yield pacer
    finally:
        try:
            _ACTIVE.remove(pacer)
        except ValueError:
            pass


async def acquire(host: str) -> float:
    """Wait on every installed pacer. A no-op (0.0) when none is installed."""
    waited = 0.0
    for pacer in list(_ACTIVE):
        waited += await pacer.acquire(host)
    return waited


def reset_for_tests() -> None:
    _ACTIVE.clear()
