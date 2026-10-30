"""An IP-level throttle breaker, and the 429/503 headers that tell one throttle from another.

WHY THIS EXISTS. Every crawl leaves from ONE reserved NAT address per environment
(`infra/gcp/setup_crawl_egress.sh`), and most storefronts sit behind Shopify's shared edge. On
2026-09-30 the nightly `external-referral-refresh` got 1,177 429s across 331 distinct hosts,
spread evenly from its first minute. Every backed-off host served `powered-by: Shopify`; ulta and
theordinary read fine. That is Shopify's edge throttling our IP across ALL its shops, not 331
hosts each deciding to throttle us. The per-host breaker (`host_backoff_tripped`) cannot see it:
it tripped on 261 hosts one by one, skipped 13,056 rows, spent 19 minutes asking a throttled IP,
and the night was reported as a generic "degraded".

WHAT IT DOES:
  * `capture_throttle_headers` keeps an ALLOWLIST of response headers (Retry-After, server,
    powered-by, cf-ray, cf-mitigated, the Shopify shop headers), each truncated. An allowlist, not
    a denylist, so a cookie or an auth header can never reach a log line by construction.
  * `IpThrottleBreaker` counts DISTINCT hosts that answered 429 (or 503 with a Retry-After) inside
    a sliding window, and trips when `trip_hosts` of them do so within `window_seconds`. A tripped
    breaker `blocks()` every host whose latest answer was a throttle, and every host known to be
    Shopify-served; a host that answers (ulta, theordinary on 09-30) carries on. It
    changes WHETHER a caller asks, never how fast: pacing stays in `crawl_politeness`.
  * A Shopify-served count is kept beside the all-hosts one. It trips nothing; it is the evidence
    for which edge did it.

WHY THE TRIP COUNTS ALL HOSTS, NOT SHOPIFY-SERVED ONES. The first version tripped on Shopify
markers and, replayed against 09-30, would most likely not have fired: only 3 of the 332 throttled
hosts had a read before their first 429, the 429s themselves may carry no Shopify header (the logs
never recorded them, which is what this module fixes), and their Retry-After was never longer than
our own 2s first hold. Markerless, the Shopify count peaks at 4. Many distinct hosts throttling one
IP inside a minute is the signal whatever edge sends it; no healthy night comes close (below).

WHY `blocks()` INCLUDES EVERY THROTTLED HOST. 321 of the 332 hosts were first contacted after the
trip point, so almost none could ever be learned as Shopify-served. Stopping a host after its first
429 costs one request per host, not the four the per-host breaker needs. It is a host's LATEST
answer that counts: one that 429'd early and has read fine since keeps being read.

HOW IT IS FED. `crawl_politeness.note_response(..., headers=resp.headers)` is the one funnel every
crawl lane already reports through, and it forwards each response here. A batch `install()`s a
breaker for the length of its run; with none installed this module does nothing but format the
per-429 log line. Module-level, not a ContextVar: the refresh runs its workers in FRESH contexts
(`services.scheduler_job_runner.spawn_isolated`), which a ContextVar would not reach. The state is
plain dicts and floats, so it is loop-agnostic like `crawl_politeness`'s own.

DEFAULTS (10 distinct hosts in 60s), measured on prod `crawl backoff` lines, all hosts counted.
Healthy nights 09-21..09-29 peaked at 1-4 distinct 429/503 hosts in any 60s window (09-29,
execution zxx62: 42 lines, 17 hosts, peak 3). 09-30 (hbr9g) peaked at 55 and crossed 10 at +33s
after 34 throttled responses, instead of running for 19 minutes.
"""
from __future__ import annotations

import logging
import math
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Set

logger = logging.getLogger(__name__)

# Lower-case. `powered-by` is what Shopify sends; `x-powered-by` is the common spelling elsewhere.
THROTTLE_HEADER_NAMES = (
    "retry-after",
    "server",
    "powered-by",
    "x-powered-by",
    "cf-ray",
    "cf-mitigated",
    "x-shopid",
    "x-shopify-stage",
)
MAX_HEADER_VALUE_CHARS = 80

TRIP_HOSTS_DEFAULT = 10
WINDOW_SECONDS_DEFAULT = 60.0
# The host sets are keyed by hostnames from third-party pages. Past this many, NEW hosts are no
# longer remembered; nothing already remembered is ever dropped, because dropping a host from a
# tripped breaker would silently start asking it again. A run's candidate list is capped at
# 30,000 rows (`_CANDIDATE_LIMIT_CAP`), so a refresh never gets near it.
_MAX_TRACKED_HOSTS = 50_000
# Distinct keys per diagnostic histogram. The keys are header values a third party chooses; past
# this many, new values fold into "other" as they arrive.
_MAX_DIAGNOSTIC_KEYS = 32


def _now() -> float:
    return time.monotonic()


def _wall_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def capture_throttle_headers(headers: Any) -> Dict[str, str]:
    """The allowlisted headers present on `headers`, lower-cased names, values truncated.

    Accepts `httpx.Headers` or any mapping. A mapping is matched case-insensitively, as HTTP
    headers are. CR/LF are stripped so a hostile value cannot forge a second log line.
    """
    if not headers:
        return {}
    try:
        items = headers.items()
    except AttributeError:
        return {}
    lowered: Dict[str, Any] = {}
    for name, value in items:
        key = str(name or "").strip().lower()
        if key in THROTTLE_HEADER_NAMES and key not in lowered:
            lowered[key] = value
    out: Dict[str, str] = {}
    for name in THROTTLE_HEADER_NAMES:
        if name not in lowered:
            continue
        text = str(lowered[name] or "").replace("\r", " ").replace("\n", " ").strip()
        if text:
            out[name] = text[:MAX_HEADER_VALUE_CHARS]
    return out


def format_throttle_headers(diag: Mapping[str, str]) -> str:
    """`retry-after=60 server=cloudflare ...` for the per-429 log line; "" when there are none."""
    return " ".join(f"{name}={diag[name]}" for name in THROTTLE_HEADER_NAMES if diag.get(name))


def looks_shopify_served(diag: Mapping[str, str]) -> bool:
    """Did this response come through Shopify's edge?

    Any ONE marker: `powered-by`/`x-powered-by` or `server` naming Shopify, or a Shopify shop
    header (`x-shopid`, `x-shopify-stage`). `server: cloudflare` alone is NOT a marker: Shopify
    fronts with Cloudflare, but so do thousands of sites that share no rate limit with it.
    """
    for name in ("powered-by", "x-powered-by", "server"):
        if "shopify" in str(diag.get(name) or "").lower():
            return True
    return bool(diag.get("x-shopid") or diag.get("x-shopify-stage"))


def is_ip_throttle_signal(status_code: int, diag: Mapping[str, str]) -> bool:
    """A 429, or a 503 that says when to come back. A bare 503 is an outage, not a throttle."""
    if status_code == 429:
        return True
    return status_code == 503 and bool(diag.get("retry-after"))


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        # `int(inf)` raises and a NaN window silently never slides; neither is what anyone set.
        logger.warning("%s=%r is not a finite number; using the default %s", name, raw, default)
        return default
    return value


def _bump_bounded(counter: Dict[str, int], key: str) -> None:
    if key not in counter and len(counter) >= _MAX_DIAGNOSTIC_KEYS - 1:
        key = "other"
    counter[key] = counter.get(key, 0) + 1


def _remember(hosts: Set[str], host: str) -> None:
    if host in hosts or len(hosts) < _MAX_TRACKED_HOSTS:
        hosts.add(host)


def _env_enabled(name: str) -> bool:
    return str(os.getenv(name, "true")).strip().lower() not in {"0", "false", "no", "off"}


class IpThrottleBreaker:
    """Trips when `trip_hosts` distinct hosts 429 (or 503 + Retry-After) within `window_seconds`.

    `enabled=False` or `trip_hosts <= 0` is the kill switch: diagnostics are still aggregated,
    nothing ever trips, and `blocks()` is always False.

    LATCHED FOR THE RUN. Once tripped it stays tripped: Shopify's throttle on 09-30 lasted the
    whole run, and a host that answers one request after the trip says nothing about whether the
    next hundred will. The rows it holds back are the head of tomorrow's queue.
    """

    def __init__(
        self,
        *,
        trip_hosts: int = TRIP_HOSTS_DEFAULT,
        window_seconds: float = WINDOW_SECONDS_DEFAULT,
        enabled: bool = True,
    ) -> None:
        self.trip_hosts = int(trip_hosts)
        window = float(window_seconds)
        # A NaN window never prunes (every comparison is False), and a window <= 0 prunes every
        # host but the current one, which silently disables the trip. Neither is how to turn the
        # breaker off; the kill switch (`enabled=False`, trip_hosts <= 0) is.
        if not math.isfinite(window) or window <= 0:
            logger.warning(
                "crawl ip throttle: window %r is not a positive number of seconds; using the "
                "default %.0fs (disable the breaker with its kill switch instead)",
                window_seconds, WINDOW_SECONDS_DEFAULT,
            )
            window = WINDOW_SECONDS_DEFAULT
        self.window_seconds = window
        self.enabled = bool(enabled) and self.trip_hosts > 0
        self.tripped = False
        self.tripped_at: Optional[str] = None
        self.trip_host_count = 0
        self.trip_shopify_host_count = 0
        self.first_throttle_at: Optional[str] = None
        self.last_throttle_at: Optional[str] = None
        # host -> monotonic instant of its latest throttle signal, pruned to the window.
        self._recent_shopify: Dict[str, float] = {}
        self._recent_all: Dict[str, float] = {}
        self.peak_shopify_hosts = 0
        self.peak_all_hosts = 0
        self.shopify_hosts: Set[str] = set()
        # Every host that throttled us at any point this run (reported), and the hosts whose
        # LATEST answer was a throttle (what `blocks()` holds back).
        self.throttled_hosts: Set[str] = set()
        self.last_answer_throttled: Set[str] = set()
        self.throttled_shopify_hosts: Set[str] = set()
        # Diagnostics over every 429/503 seen, whether or not it counted toward a trip.
        self.responses = 0
        self.by_server: Dict[str, int] = {}
        self.by_powered_by: Dict[str, int] = {}
        self.retry_after: Dict[str, int] = {}
        self.cf_mitigated = 0

    @classmethod
    def from_env(
        cls,
        *,
        trip_hosts: Optional[int] = None,
        window_seconds: Optional[float] = None,
        enabled: Optional[bool] = None,
    ) -> "IpThrottleBreaker":
        """An explicit argument wins, then CRAWL_IP_THROTTLE_TRIP_HOSTS /
        CRAWL_IP_THROTTLE_WINDOW_SECONDS / CRAWL_IP_THROTTLE_BREAKER_ENABLED, then the defaults."""
        if trip_hosts is None:
            trip_hosts = int(_env_float("CRAWL_IP_THROTTLE_TRIP_HOSTS", TRIP_HOSTS_DEFAULT))
        if window_seconds is None:
            window_seconds = _env_float("CRAWL_IP_THROTTLE_WINDOW_SECONDS", WINDOW_SECONDS_DEFAULT)
        if enabled is None:
            enabled = _env_enabled("CRAWL_IP_THROTTLE_BREAKER_ENABLED")
        return cls(trip_hosts=trip_hosts, window_seconds=window_seconds, enabled=enabled)

    @staticmethod
    def _prune(recent: Dict[str, float], now: float, window: float) -> None:
        for host in [h for h, t in recent.items() if now - t > window]:
            del recent[host]

    def observe(self, host: str, status_code: int, diag: Mapping[str, str]) -> None:
        """Feed one response. Any status: a 200 is how a host becomes known Shopify-served."""
        host = str(host or "").strip().lower()
        if not host:
            return
        shopify = looks_shopify_served(diag)
        if shopify:
            _remember(self.shopify_hosts, host)
        if not is_ip_throttle_signal(status_code, diag):
            # ANY OTHER ANSWER CLEARS IT. A host that 429'd once early and has read fine since
            # is not part of the storm: on 09-30 ulta (392 of the run's 556 reads) had 429s too,
            # and a sticky set would have held back every remaining ulta row after a trip.
            # Shopify-ness stays sticky; it is a fact about the host, not about its last answer.
            self.last_answer_throttled.discard(host)
        if status_code not in (429, 503):
            return
        self.responses += 1
        server = str(diag.get("server") or "").lower() or "none"
        powered = str(diag.get("powered-by") or diag.get("x-powered-by") or "").lower() or "none"
        _bump_bounded(self.by_server, server)
        _bump_bounded(self.by_powered_by, powered)
        _bump_bounded(self.retry_after, str(diag.get("retry-after") or "") or "none")
        if diag.get("cf-mitigated"):
            self.cf_mitigated += 1
        if not is_ip_throttle_signal(status_code, diag):
            return

        wall = _wall_now()
        self.first_throttle_at = self.first_throttle_at or wall
        self.last_throttle_at = wall
        _remember(self.throttled_hosts, host)
        _remember(self.last_answer_throttled, host)
        now = _now()
        self._recent_all[host] = now
        self._prune(self._recent_all, now, self.window_seconds)
        self.peak_all_hosts = max(self.peak_all_hosts, len(self._recent_all))
        # Diagnostic only. Shopify-ness is sticky per host: a 429 page may carry fewer headers
        # than the product page the same host served a minute earlier.
        if shopify or host in self.shopify_hosts:
            _remember(self.throttled_shopify_hosts, host)
            self._recent_shopify[host] = now
        self._prune(self._recent_shopify, now, self.window_seconds)
        self.peak_shopify_hosts = max(self.peak_shopify_hosts, len(self._recent_shopify))
        # ONE host 429ing over and over is ONE distinct host: that is the per-host breaker's job.
        if self.enabled and not self.tripped and len(self._recent_all) >= self.trip_hosts:
            self.tripped = True
            self.tripped_at = wall
            self.trip_host_count = len(self._recent_all)
            self.trip_shopify_host_count = len(self._recent_shopify)
            logger.warning(
                "crawl ip throttle: %d distinct hosts (%d Shopify-served) answered 429 within "
                "%.0fs (first throttle %s); a shared edge is throttling this egress IP. No new "
                "requests to hosts that throttled us or are Shopify-served this run",
                self.trip_host_count, self.trip_shopify_host_count, self.window_seconds,
                self.first_throttle_at,
            )

    def blocks(self, host: str) -> bool:
        """Should the caller stop asking `host`? Only after a trip, and only a host whose LATEST
        answer was a throttle, or that is known to be Shopify-served (the edge that did it on
        09-30)."""
        if not self.tripped:
            return False
        key = str(host or "").strip().lower()
        return key in self.last_answer_throttled or key in self.shopify_hosts

    def summary(self) -> Dict[str, Any]:
        def top(counter: Dict[str, int], n: int = 10) -> Dict[str, int]:
            return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n])

        return {
            "ip_throttled": self.tripped,
            "ip_throttle_breaker_armed": self.enabled,
            "ip_throttle_trip_hosts": self.trip_hosts,
            "ip_throttle_window_seconds": self.window_seconds,
            "ip_throttle_tripped_at": self.tripped_at,
            "ip_throttle_trip_host_count": self.trip_host_count,
            "ip_throttle_trip_shopify_host_count": self.trip_shopify_host_count,
            "ip_throttle_first_429_at": self.first_throttle_at,
            "ip_throttle_last_429_at": self.last_throttle_at,
            "ip_throttle_hosts": len(self.throttled_hosts),
            "ip_throttle_shopify_hosts": len(self.throttled_shopify_hosts),
            # The busiest window of the run, all hosts and Shopify-served ones. These are the
            # numbers to re-calibrate the defaults from.
            "ip_throttle_peak_hosts_in_window": self.peak_all_hosts,
            "ip_throttle_peak_shopify_hosts_in_window": self.peak_shopify_hosts,
            "throttle_diagnostics": {
                "responses": self.responses,
                "by_server": top(self.by_server),
                "by_powered_by": top(self.by_powered_by),
                "retry_after": top(self.retry_after, 12),
                "cf_mitigated": self.cf_mitigated,
            },
        }


def is_challenge(diag: Mapping[str, str]) -> bool:
    """A Cloudflare bot challenge (`cf-mitigated: challenge`), whatever the status it came with.

    Counted APART from a plain 429: a 429 says "slower", a challenge says Cloudflare has scored our
    address as a bot, and on 2026-10-16 that verdict followed the IP across 334 unrelated hosts.
    """
    return str(diag.get("cf-mitigated") or "").strip().lower() == "challenge"


# 8, not 3 (controller review of be#2640, 2026-10-17): the refresh's healthy nights already draw a few
# challenges. Distinct hosts answering `cf-mitigated: challenge` (429/503, from the `crawl backoff`
# lines) in the busiest 120 s of each night, 10-08..10-15: 4 / 3 / 3 / 1 / 2 / 1 (10-12 and 10-14: none),
# with ichibanm.com challenged on 5 of those nights. The listing night, 10-16 (tm899), peaked at 360. A
# default of 3 would have cooled down 3 of 6 healthy nights; 8 is twice the healthy peak and still trips
# within the first seconds of a 10-16 night. Hosts that challenge every night belong in
# CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS (a data fact set at rollout), not in this number.
CHALLENGE_TRIP_HOSTS_DEFAULT = 8
CHALLENGE_WINDOW_SECONDS_DEFAULT = 120.0
COOLDOWN_SECONDS_DEFAULT = 900.0
# Hosts that challenge EVERY request whatever the IP's standing (a store whose Cloudflare zone runs
# "under attack" mode for all bots, say). Their answers are recorded but never counted toward a trip,
# so three of them in one queue head cannot trip a healthy night. Comma- or space-separated, `www.`
# folded. EMPTY BY DEFAULT: the 2026-10-17 review found the 3-in-120 s rule has no healthy-night
# baseline yet (docs: the pacing census's challenge_baseline.md); fill it from that baseline, never
# by guessing.
CHALLENGE_EXCLUDED_HOSTS_ENV = "CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS"
_MAX_TOP_CHALLENGE_HOSTS = 20


def _fold_host(host: Any) -> str:
    key = str(host or "").strip().lower()
    return key[4:] if key.startswith("www.") else key


def parse_excluded_hosts(raw: Any) -> Set[str]:
    """`CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS` as a set of `www.`-folded hosts."""
    if raw is None:
        return set()
    if isinstance(raw, str):
        parts = raw.replace(",", " ").split()
    else:
        parts = [str(item) for item in raw]
    return {folded for folded in (_fold_host(part) for part in parts) if folded}


class StagedIpThrottleBreaker(IpThrottleBreaker):
    """The IP breaker with an EARLIER trip and a cool-down stage, for a run that has a global pacer.

    TWO TRIP RULES, either one trips:
      * CHALLENGE: `challenge_trip_hosts` distinct hosts answering `cf-mitigated: challenge` within
        `challenge_window_seconds` (default 8 in 120 s; healthy nights peak at 4). 2026-10-16: tm899 peaked at 262
        challenge-or-429 hosts in 60 s and zt5zz drew 359 challenges in its first two minutes; no
        night of 10-08..10-15 had more than 12 throttled hosts in the whole run;
      * THROTTLE: the parent's rule over 429 / 503 + Retry-After, `trip_hosts` in `window_seconds`
        (default 10 in 60 s), unchanged.

    WHAT A TRIP DOES. The first one (of `max_trips`, default 2) starts a cool-down of
    `cooldown_seconds` through `on_trip` (the refresh pauses its pacer and halves its rate) and the
    run carries on after it; from then on a host whose latest answer was a throttle or a
    challenge is not asked again, and, if the throttle rule tripped (the 09-30 Shopify-edge shape),
    neither is a known Shopify-served host. The last one sets `tripped`, which ends the run, with
    `status: ip_throttled` and exit 3 as before. `cooldown_seconds <= 0` makes the first trip the
    last, i.e. no cool-down stage.

    AFTER A COOL-DOWN ONLY NEW EVIDENCE COUNTS. Answers to requests that were already in flight when
    the trip landed arrive during the cool-down; they are recorded in the diagnostics but cannot
    count toward the next trip, and neither can anything seen before the cool-down ended.

    The kill switch is the parent's (`enabled=False`); setting one rule's threshold <= 0 disables
    that rule only.
    """

    def __init__(
        self,
        *,
        trip_hosts: int = TRIP_HOSTS_DEFAULT,
        window_seconds: float = WINDOW_SECONDS_DEFAULT,
        enabled: bool = True,
        challenge_trip_hosts: int = CHALLENGE_TRIP_HOSTS_DEFAULT,
        challenge_window_seconds: float = CHALLENGE_WINDOW_SECONDS_DEFAULT,
        cooldown_seconds: float = COOLDOWN_SECONDS_DEFAULT,
        max_trips: int = 2,
        on_trip: Optional[Callable[["StagedIpThrottleBreaker", Dict[str, Any]], None]] = None,
        excluded_hosts: Optional[Iterable[str]] = None,
    ) -> None:
        super().__init__(trip_hosts=trip_hosts, window_seconds=window_seconds, enabled=enabled)
        self.excluded_hosts: Set[str] = parse_excluded_hosts(excluded_hosts)
        self.excluded_challenge_responses = 0
        # host -> challenges it answered this run, for the per-night baseline the 3-in-120 s rule
        # never had (review M5). Bounded like every other host-keyed store here.
        self.challenge_counts: Dict[str, int] = {}
        self.challenge_trip_hosts = int(challenge_trip_hosts)
        cwin = float(challenge_window_seconds)
        if not math.isfinite(cwin) or cwin <= 0:
            logger.warning(
                "crawl ip throttle: challenge window %r is not a positive number of seconds; "
                "using the default %.0fs", challenge_window_seconds, CHALLENGE_WINDOW_SECONDS_DEFAULT,
            )
            cwin = CHALLENGE_WINDOW_SECONDS_DEFAULT
        self.challenge_window_seconds = cwin
        cool = float(cooldown_seconds)
        self.cooldown_seconds = cool if math.isfinite(cool) and cool > 0 else 0.0
        self.max_trips = max(1, int(max_trips)) if self.cooldown_seconds > 0 else 1
        # Armed when EITHER rule is: the parent disarms itself on `trip_hosts <= 0`.
        self.enabled = bool(enabled) and (self.trip_hosts > 0 or self.challenge_trip_hosts > 0)
        self.on_trip = on_trip
        self.trips = 0
        self.trip_events: List[Dict[str, Any]] = []
        self.cooling_until = 0.0
        # Nothing observed before this monotonic instant counts toward a trip.
        self._counts_since = -math.inf
        self._block_shopify = False
        self._recent_challenge: Dict[str, float] = {}
        self.challenge_responses = 0
        self.plain_throttle_responses = 0
        self.challenge_hosts: Set[str] = set()
        self.peak_challenge_hosts = 0

    @classmethod
    def from_env(  # type: ignore[override]
        cls,
        *,
        trip_hosts: Optional[int] = None,
        window_seconds: Optional[float] = None,
        enabled: Optional[bool] = None,
        challenge_trip_hosts: Optional[int] = None,
        challenge_window_seconds: Optional[float] = None,
        cooldown_seconds: Optional[float] = None,
        on_trip: Optional[Callable[["StagedIpThrottleBreaker", Dict[str, Any]], None]] = None,
        excluded_hosts: Optional[Iterable[str]] = None,
    ) -> "StagedIpThrottleBreaker":
        """An explicit argument wins, then the env (the parent's three, plus
        CRAWL_IP_CHALLENGE_TRIP_HOSTS / CRAWL_IP_CHALLENGE_WINDOW_SECONDS /
        CRAWL_IP_THROTTLE_COOLDOWN_SECONDS / CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS), then the
        defaults."""
        if excluded_hosts is None:
            excluded_hosts = parse_excluded_hosts(os.getenv(CHALLENGE_EXCLUDED_HOSTS_ENV))
        if trip_hosts is None:
            trip_hosts = int(_env_float("CRAWL_IP_THROTTLE_TRIP_HOSTS", TRIP_HOSTS_DEFAULT))
        if window_seconds is None:
            window_seconds = _env_float("CRAWL_IP_THROTTLE_WINDOW_SECONDS", WINDOW_SECONDS_DEFAULT)
        if enabled is None:
            enabled = _env_enabled("CRAWL_IP_THROTTLE_BREAKER_ENABLED")
        if challenge_trip_hosts is None:
            challenge_trip_hosts = int(
                _env_float("CRAWL_IP_CHALLENGE_TRIP_HOSTS", CHALLENGE_TRIP_HOSTS_DEFAULT)
            )
        if challenge_window_seconds is None:
            challenge_window_seconds = _env_float(
                "CRAWL_IP_CHALLENGE_WINDOW_SECONDS", CHALLENGE_WINDOW_SECONDS_DEFAULT
            )
        if cooldown_seconds is None:
            cooldown_seconds = _env_float("CRAWL_IP_THROTTLE_COOLDOWN_SECONDS", COOLDOWN_SECONDS_DEFAULT)
        return cls(
            trip_hosts=trip_hosts, window_seconds=window_seconds, enabled=enabled,
            challenge_trip_hosts=challenge_trip_hosts,
            challenge_window_seconds=challenge_window_seconds,
            cooldown_seconds=cooldown_seconds, on_trip=on_trip, excluded_hosts=excluded_hosts,
        )

    def is_excluded(self, host: str) -> bool:
        return bool(self.excluded_hosts) and _fold_host(host) in self.excluded_hosts

    def cooling(self, now: Optional[float] = None) -> bool:
        return (_now() if now is None else now) < self.cooling_until

    def _counted(self, recent: Dict[str, float]) -> int:
        return sum(1 for at in recent.values() if at >= self._counts_since)

    def observe(self, host: str, status_code: int, diag: Mapping[str, str]) -> None:
        key = str(host or "").strip().lower()
        if not key:
            return
        status = int(status_code)
        armed = self.enabled
        # The parent records its window and every diagnostic, and never trips: the trip is ours.
        self.enabled = False
        try:
            super().observe(key, status, diag)
        finally:
            self.enabled = armed
        now = _now()
        excluded = self.is_excluded(key)
        if excluded:
            # AN ALWAYS-CHALLENGING HOST NEVER COUNTS (CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS): its 429
            # leaves the parent's throttle window too. Its diagnostics stay, and `blocks()` still
            # holds it back after a trip, since its latest answer was still a refusal.
            self._recent_all.pop(key, None)
            self._recent_shopify.pop(key, None)
        if is_challenge(diag):
            self.challenge_responses += 1
            _remember(self.challenge_hosts, key)
            if key in self.challenge_counts or len(self.challenge_counts) < _MAX_TRACKED_HOSTS:
                self.challenge_counts[key] = self.challenge_counts.get(key, 0) + 1
            # A challenge is a throttle answer for `blocks()`, whatever its status (a 403 is not one
            # to the parent, which has just cleared the host).
            _remember(self.last_answer_throttled, key)
            if excluded:
                self.excluded_challenge_responses += 1
            else:
                self._recent_challenge[key] = now
        elif is_ip_throttle_signal(status, diag):
            self.plain_throttle_responses += 1
        self._prune(self._recent_challenge, now, self.challenge_window_seconds)
        self.peak_challenge_hosts = max(self.peak_challenge_hosts, len(self._recent_challenge))
        # No separate "cooling" test: everything observed during a cool-down is older than
        # `_counts_since`, which is the cool-down's end, so `_counted` already leaves it out.
        if not armed or self.tripped:
            return
        challenge_hosts = self._counted(self._recent_challenge)
        throttle_hosts = self._counted(self._recent_all)
        if self.challenge_trip_hosts > 0 and challenge_hosts >= self.challenge_trip_hosts:
            self._trip("challenge", now, challenge_hosts, throttle_hosts)
        elif self.trip_hosts > 0 and throttle_hosts >= self.trip_hosts:
            self._trip("throttle", now, challenge_hosts, throttle_hosts)

    def _trip(self, reason: str, now: float, challenge_hosts: int, throttle_hosts: int) -> None:
        self.trips += 1
        wall = _wall_now()
        final = self.trips >= self.max_trips
        if reason == "throttle":
            self._block_shopify = True
        event: Dict[str, Any] = {
            "trip": self.trips,
            "at": wall,
            "reason": reason,
            "challenge_hosts": challenge_hosts,
            "throttle_hosts": throttle_hosts,
            "shopify_hosts": self._counted(self._recent_shopify),
            "action": "stop" if final else "cooldown",
        }
        if final:
            self.tripped = True
            self.tripped_at = wall
            self.trip_host_count = max(challenge_hosts, throttle_hosts)
            self.trip_shopify_host_count = event["shopify_hosts"]
        else:
            self.cooling_until = now + self.cooldown_seconds
            self._counts_since = self.cooling_until
            event["cooldown_seconds"] = self.cooldown_seconds
        self.trip_events.append(event)
        logger.warning(
            "crawl ip throttle: trip %d/%d (%s): %d distinct hosts answered a Cloudflare challenge "
            "within %.0fs, %d answered 429/503+Retry-After within %.0fs (first throttle %s); %s",
            self.trips, self.max_trips, reason, challenge_hosts, self.challenge_window_seconds,
            throttle_hosts, self.window_seconds, self.first_throttle_at,
            "ending the run" if final else f"cooling down {self.cooldown_seconds:.0f}s, then half rate",
        )
        if self.on_trip is not None:
            self.on_trip(self, event)

    def blocks(self, host: str) -> bool:
        """After ANY trip: a host whose latest answer was a throttle or a challenge, and, once the
        throttle rule has tripped, a known Shopify-served host. Never before the first trip."""
        if not self.trips:
            return False
        key = str(host or "").strip().lower()
        return key in self.last_answer_throttled or (self._block_shopify and key in self.shopify_hosts)

    def summary(self) -> Dict[str, Any]:
        out = super().summary()
        out.update({
            "ip_throttle_challenge_trip_hosts": self.challenge_trip_hosts,
            "ip_throttle_challenge_window_seconds": self.challenge_window_seconds,
            "ip_throttle_cooldown_seconds": self.cooldown_seconds,
            "ip_throttle_max_trips": self.max_trips,
            "ip_throttle_challenge_responses": self.challenge_responses,
            "ip_throttle_plain_429_responses": self.plain_throttle_responses,
            "ip_throttle_challenge_hosts": len(self.challenge_hosts),
            "ip_throttle_peak_challenge_hosts_in_window": self.peak_challenge_hosts,
            "breaker_trips": self.trips,
            "breaker_trip_events": list(self.trip_events),
            # The per-host challenge counts behind the rule, busiest first (review M5): the evidence
            # a host belongs on CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS, or does not.
            "ip_throttle_challenge_top_hosts": dict(
                sorted(self.challenge_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_TOP_CHALLENGE_HOSTS]
            ),
            "ip_throttle_challenge_excluded_hosts": sorted(self.excluded_hosts)[:_MAX_TOP_CHALLENGE_HOSTS],
            "ip_throttle_excluded_challenge_responses": self.excluded_challenge_responses,
        })
        return out


_ACTIVE: List[IpThrottleBreaker] = []


@contextmanager
def installed(breaker: IpThrottleBreaker) -> Iterator[IpThrottleBreaker]:
    """Feed every response `crawl_politeness.note_response` sees to `breaker` for this block."""
    _ACTIVE.append(breaker)
    try:
        yield breaker
    finally:
        try:
            _ACTIVE.remove(breaker)
        except ValueError:
            pass


def observe_response(host: str, status_code: int, headers: Any) -> None:
    """Forward one response to every installed breaker. A no-op when none is installed."""
    if not _ACTIVE:
        return
    diag = capture_throttle_headers(headers)
    for breaker in list(_ACTIVE):
        breaker.observe(host, int(status_code), diag)


def reset_for_tests() -> None:
    _ACTIVE.clear()
