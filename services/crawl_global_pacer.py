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
  * after `stop()` nothing starts at all: `acquire` raises `GlobalPaceStopped`;
  * NOTHING STARTS AT OR AFTER THE RUN'S DEADLINE: a request whose reserved start falls there stops
    the pacer instead of sleeping towards it (2026-10-17 review, L1);
  * EVERY REDIRECT HOP takes a slot too, and a hop onto another host is admitted as a new host
    (`redirect_client_kwargs`, the httpx request hook a crawl client installs; review M2).

ZERO NEVER MEANS "UNPACED" (2026-10-17 review, M3/H1). Every knob a run resolves goes through
`resolve_setting` here, once, for both jobs that install a pacer: 0, a negative number, NaN, inf or
an unparsable string is a typo, logged at ERROR, and the default is used. The rate is clamped to
[`RPS_MIN`, `RPS_MAX`], the new-host cap is rounded UP and is at least 1, and a budget must be > 0.
The only way to run without pacing is the explicit kill switch, `CRAWL_GLOBAL_PACER_DISABLED=true`
(`pacer_disabled`), which logs at ERROR on every run that honours it; even then the pacer is still
installed as an `unpaced` one, so the IP breaker's cool-down and stop still reach every request.

It changes only WHEN a request starts, never whether the per-host rules apply: the per-host
interval, robots, backoff and the Shopify-edge pacer (`services.shopify_edge_pacer`) all still run.

NO LOCKS, the same way `crawl_politeness` has none: every reservation reads and writes its state
with no `await` in between, which one event loop makes atomic, and the state is plain floats and
dicts, so it is loop-agnostic. Module-level installation, not a ContextVar, for the reason
`crawl_ip_throttle` gives: the refresh runs its workers in fresh contexts.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import math
import os
import random
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

NEW_HOST_WINDOW_SECONDS = 60.0
JITTER_DEFAULT = 0.3
# The rate every run gets unless told otherwise, and the band any value is clamped into. 0.5/s is
# measured against what got the address listed (`external_referral_readiness`'s note on it); 1.0/s
# is the 09-27 serial loop that was never listed, so nothing above it is accepted; 0.05/s (one
# request every 20 s) is the floor below which a run reads too little to be worth scheduling, and
# a value that small is far likelier a unit slip than an intent. Read at call time, so a test can
# move them.
RPS_DEFAULT = 0.5
RPS_MIN = 0.05
RPS_MAX = 1.0
MAX_NEW_HOSTS_PER_MINUTE_DEFAULT = 6
# Redirect hops one crawl request may follow. httpx's default is 20, and every hop is an origin
# request; 5 covers the longest chain seen in the robots cohort (3) with room.
MAX_REDIRECTS = 5
# The ONE kill switch for every lane that installs a pacer.
DISABLED_ENV = "CRAWL_GLOBAL_PACER_DISABLED"
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
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


def resolve_setting(
    explicit: Any,
    env_name: Optional[str],
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    """A run's pacing knob: the flag wins, then `env_name`, then `default`. THE one place every knob
    of every paced lane is read (the refresh's and the destination sweep's), so the rule below is
    stated once.

    A value must be a finite number > 0. Anything else -- 0, -0, a negative, NaN, inf, "abc" -- is
    refused LOUDLY (ERROR) and the default is used: before 2026-10-17 `GLOBAL_RPS=0` meant
    "unpaced", so one stray 0 in an env var would have put the crawl IP straight back at the rate
    that got it challenge-listed. The value is then clamped into [minimum, maximum] (WARNING: a
    clamp is a value we understood and corrected, not a typo)."""
    raw: Any = explicit
    source = "argument"
    if raw is None and env_name:
        raw = (os.getenv(env_name) or "").strip() or None
        source = env_name
    if raw is None:
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        logger.error(
            "crawl global pacer: %s=%r is not a positive number (0 never means unpaced; set %s=true "
            "to disable the pacer); using the default %s", source, raw, DISABLED_ENV, default,
        )
        return float(default)
    clamped = value
    if minimum is not None and clamped < minimum:
        clamped = float(minimum)
    if maximum is not None and clamped > maximum:
        clamped = float(maximum)
    if clamped != value:
        logger.warning(
            "crawl global pacer: %s=%r is outside [%s, %s]; using %s", source, raw, minimum, maximum, clamped,
        )
    return clamped


def resolve_rps(explicit: Any, env_name: Optional[str], default: Optional[float] = None) -> float:
    """A run's global rate: positive, clamped to [RPS_MIN, RPS_MAX]."""
    return resolve_setting(
        explicit, env_name, RPS_DEFAULT if default is None else default, minimum=RPS_MIN, maximum=RPS_MAX,
    )


def resolve_new_host_cap(explicit: Any, env_name: Optional[str], default: Optional[int] = None) -> int:
    """New hosts per minute: rounded UP (`int()` used to truncate 0.9 to 0, which disabled the cap),
    and at least 1."""
    value = resolve_setting(
        explicit, env_name, MAX_NEW_HOSTS_PER_MINUTE_DEFAULT if default is None else default,
    )
    return max(1, int(math.ceil(value)))


def resolve_budget_seconds(explicit: Any, env_name: Optional[str], default: float) -> float:
    """A run's wall-clock budget: > 0. There is no "no deadline" value any more; a run that must go
    longer passes a longer budget."""
    return resolve_setting(explicit, env_name, default)


def pacer_disabled(*, legacy_enabled_env: Optional[str] = None, lane: str = "crawl") -> bool:
    """The explicit kill switch: `CRAWL_GLOBAL_PACER_DISABLED=true` for every lane, or a lane's
    legacy `<...>_GLOBAL_PACER_ENABLED=false`. Logged at ERROR on every call that returns True, so
    every unpaced run says so in its own log. An unreadable value is NOT a disable (fails paced)."""
    raw = (os.getenv(DISABLED_ENV) or "").strip().lower()
    disabled = raw in _TRUE
    source = DISABLED_ENV
    if not disabled and legacy_enabled_env:
        legacy = (os.getenv(legacy_enabled_env) or "").strip().lower()
        if legacy in _FALSE:
            disabled, source = True, legacy_enabled_env
    if raw and raw not in _TRUE and raw not in _FALSE:
        logger.error("crawl global pacer: %s=%r is not true/false; the pacer stays ON", DISABLED_ENV, raw)
    if disabled:
        logger.error(
            "crawl global pacer DISABLED for %s by %s: no global rate and no new-host cap this run "
            "(the IP breaker's cool-down and stop still apply). This is the kill switch, not a "
            "setting; unset it as soon as the incident it was for is over.", lane, source,
        )
    return disabled


class GlobalOriginPacer:
    """See the module docstring. `rps` and `max_new_hosts_per_minute` are validated exactly as
    `resolve_rps` / `resolve_new_host_cap` validate a flag: 0, negative or non-finite is an ERROR and
    the default, never "off". `unpaced=True` is the ONLY way to turn the rate and the cap off, and
    is what the kill switch builds: a pacer that only ever pauses or stops."""

    def __init__(
        self,
        *,
        rps: float = RPS_DEFAULT,
        max_new_hosts_per_minute: int = MAX_NEW_HOSTS_PER_MINUTE_DEFAULT,
        jitter: float = JITTER_DEFAULT,
        deadline: Optional[float] = None,
        unpaced: bool = False,
    ) -> None:
        self.unpaced = bool(unpaced)
        if self.unpaced:
            self.initial_rps = 0.0
            self.max_new_hosts_per_minute = 0
        else:
            self.initial_rps = resolve_rps(rps, None)
            self.max_new_hosts_per_minute = resolve_new_host_cap(max_new_hosts_per_minute, None)
        self.rps = self.initial_rps
        # Clamped to [0, 0.9]: a jitter of 1 would allow a zero gap, i.e. a burst.
        self.jitter = min(max(_finite_non_negative(jitter, JITTER_DEFAULT), 0.0), 0.9)
        # A monotonic instant past which no cool-down may be waited out (the run's budget).
        self.deadline = deadline
        # No request STARTS at or after `deadline` either (be#2640 review L1): the first one that
        # would stops the pacer. This was the destination sweep's opt-in `hard_deadline` (be#2641);
        # the review made it the rule for every run, so the flag is gone.
        self.paused_until = 0.0
        self.stopped = False
        self.stop_reason: Optional[str] = None
        self._next_start = 0.0
        # THE NEW-HOST CAP COUNTS REAL FIRST CONTACTS (2026-10-17). It used to count the instant a
        # host was ADMITTED (at take time), but with the rate on the host's first request then went
        # out later, behind the bucket, so the reserved instants met the cap while the actual first
        # requests did not: 9 first contacts in one 60 s window under a cap of 6 (caught by
        # `test_the_batch_never_contacts_more_than_six_new_hosts_a_minute` at 1.0/s). Now:
        #   * `_first_contacts`: the START instant of every host's first request, recorded in
        #     `acquire` in the same atomic step that reserves the start, so the rule is enforced on
        #     what actually leaves;
        #   * `_pending`: hosts the scheduler took (`admit`) whose first request has not started, at
        #     a provisional instant. They hold room under the cap, so two workers cannot both take
        #     the last slot, and age out of the window like a contact does.
        self._contacted: Dict[str, float] = {}
        self._first_contacts: List[float] = []
        self._pending: Dict[str, float] = {}
        # Counters for the run summary.
        self.requests = 0
        self.new_hosts = 0
        self.paces_waited_seconds = 0.0
        self.cooldowns = 0
        self.cooldown_seconds = 0.0
        self.grants: List[float] = []

    # ── configuration the breaker changes mid-run ─────────────────────────────────────────────

    def set_rate(self, rps: float) -> None:
        """The breaker's half rate. Positive and finite, or the call is ignored (it can never turn the
        rate off); capped at RPS_MAX but NOT floored at RPS_MIN, because slower is always allowed.
        A no-op on an unpaced pacer."""
        if self.unpaced:
            return
        try:
            value = float(rps)
        except (TypeError, ValueError):
            return
        if math.isfinite(value) and value > 0:
            self.rps = min(value, RPS_MAX)

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

    def _prune(self, now: float) -> None:
        horizon = now - NEW_HOST_WINDOW_SECONDS
        if self._first_contacts and self._first_contacts[0] < horizon:
            self._first_contacts = [t for t in self._first_contacts if t >= horizon]
        for key in [k for k, t in self._pending.items() if t < horizon]:
            del self._pending[key]

    def _next_admission(self, now: float, exclude: str = "") -> float:
        """The earliest instant a host not yet contacted could START, counting real first contacts
        and the provisional instants of hosts taken but not yet started (`exclude`: the host
        asking, whose own provisional instant must not hold room against itself)."""
        cap = self.max_new_hosts_per_minute
        if cap <= 0:
            return now
        self._prune(now)
        entries = sorted(
            self._first_contacts + [t for k, t in self._pending.items() if k != exclude]
        )
        at = now
        if entries:
            at = max(at, entries[-1])
        if len(entries) >= cap:
            at = max(at, entries[-cap] + NEW_HOST_WINDOW_SECONDS)
        return at

    def is_admitted(self, host: str) -> bool:
        key = str(host or "").strip().lower()
        return key in self._contacted or key in self._pending

    def new_host_wait(self) -> float:
        """Seconds until one more new host could be admitted (0 when one could be now)."""
        now = _now()
        return max(0.0, self._next_admission(now) - now)

    def admit(self, host: str) -> float:
        """The scheduler takes `host`: hold a provisional place under the cap until its first
        request starts (`acquire` replaces it with the real start). Idempotent. Atomic (no await),
        so concurrent takers hold distinct places."""
        key = str(host or "").strip().lower()
        now = _now()
        if not key:
            return now
        if key in self._contacted:
            return self._contacted[key]
        if key in self._pending:
            return self._pending[key]
        at = self._next_admission(now)
        if self.max_new_hosts_per_minute > 0:
            self._pending[key] = at
        return at

    def _record_first_contact(self, key: str, start: float) -> None:
        self._pending.pop(key, None)
        if self.max_new_hosts_per_minute > 0:
            bisect.insort(self._first_contacts, start)
        # Past `_MAX_TRACKED_HOSTS` a host is not remembered, so its next request counts as a first
        # contact again: over-counting, never under-counting.
        if len(self._contacted) < _MAX_TRACKED_HOSTS:
            self._contacted[key] = start
        self.new_hosts += 1

    def _unrecord_first_contact(self, key: str, start: float) -> None:
        """The request a first contact was recorded for did not go out (stopped, or a cool-down
        landed while it waited): the host is still uncontacted."""
        if self._contacted.get(key) == start:
            del self._contacted[key]
        try:
            self._first_contacts.remove(start)
        except ValueError:
            pass
        self.new_hosts -= 1

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
            # RESERVE, THEN SLEEP: both reservations (the rate slot and, for a host's first
            # request, its place under the new-host cap) happen here with no await between them.
            key = str(host or "").strip().lower()
            first = bool(key) and key not in self._contacted
            admitted_at = self._next_admission(now, exclude=key) if first else now
            start = max(now, admitted_at, self._next_start)
            if self.deadline is not None and start >= self.deadline:
                # THE SLOT IS PAST THE RUN'S END (review L1). Sleeping to it would send a request
                # after the budget (and, near a 3,300 s budget, into the 3,600 s task timeout), so
                # the pacer stops instead, and every later caller is refused the same way.
                self.stop("budget ends before the next slot")
                self._check_stopped()
            self._next_start = start + self._gap()
            if first:
                self._record_first_contact(key, start)
            delay = start - now
            if delay > 0:
                waited += delay
                self.paces_waited_seconds += delay
                await _sleep(delay)
            if self.stopped or self.paused_until > _now():
                if first:
                    self._unrecord_first_contact(key, start)
                self._check_stopped()
                # A trip landed while this request waited for its turn. It does not go out into
                # the cool-down: it waits the cool-down out and takes a fresh slot after it.
                continue
            self.requests += 1
            if len(self.grants) < _MAX_RECORDED_GRANTS:
                self.grants.append(start)
            return waited

    def summary(self) -> Dict[str, Any]:
        return {
            # None, not 0, when the kill switch ran the pacer unpaced: 0 is never a rate.
            "global_rps": None if self.unpaced else self.initial_rps,
            "final_rate": None if self.unpaced else self.rps,
            "global_pacer_disabled": self.unpaced,
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


_HOP_MARK = "pivota_global_pacer_request"


async def _pace_redirect_hop(request: Any) -> None:
    """httpx `request` event hook: every redirect HOP takes a global slot (review M2).

    httpx calls request hooks once per request it sends, the first one and every hop
    (`_send_handling_redirects`), and builds each hop with the SAME `extensions` dict as the request
    before it. So the first call marks that dict and returns (the caller paced the first request
    through `crawl_politeness`), and any call that finds the mark is a hop. Marking the request, not
    counting calls on the client, keeps this right on a client shared by many requests.

    The hop pays `acquire(host)` for ITS host, so a hop onto another host is admitted against the
    new-hosts-per-minute cap like any first contact. A no-op with no pacer installed, so a lane
    without one only gains the lower `MAX_REDIRECTS`. `GlobalPaceStopped` propagates out of
    `client.get` and the hop is not sent.
    """
    extensions = getattr(request, "extensions", None)
    if not isinstance(extensions, dict):
        return
    if not extensions.get(_HOP_MARK):
        extensions[_HOP_MARK] = True
        return
    host = str(getattr(getattr(request, "url", None), "host", "") or "").strip().lower()
    if host:
        await acquire(host)


def redirect_client_kwargs(max_redirects: int = MAX_REDIRECTS) -> Dict[str, Any]:
    """Splat into an `httpx.AsyncClient(follow_redirects=True, ...)`: the hop hook and the hop cap."""
    return {"event_hooks": {"request": [_pace_redirect_hop]}, "max_redirects": int(max_redirects)}


def reset_for_tests() -> None:
    _ACTIVE.clear()
