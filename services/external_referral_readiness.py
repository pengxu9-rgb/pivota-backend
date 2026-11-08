from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import logging
import math
import os
import re
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Awaitable, Callable, Deque, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from urllib.parse import urlparse

from db.database import database
from db.merchant_onboarding import get_all_merchant_onboardings
from services.external_seed_audit import (
    audit_external_seed_row,
    get_content_extracted_at,
    get_destination_failure_streak,
    get_destination_verdict,
    get_last_destination_check_at,
    get_last_extracted_at,
    get_snapshot,
)
from services.external_seed_destination_liveness import (
    CONFIRMED_DEAD_VERDICTS,
    RETIREMENT_STREAK,
    ambiguous_price_guard_mode,
    apply_unconfirmed_hides,
    count_unconfirmed_observation,
    handleless_read_mode,
    unconfirmed_hide_mode,
)
from services import crawl_global_pacer, crawl_ip_throttle, crawl_politeness
from services import external_seed_catalogue_refresh as catalogue_lane
from services.external_seed_search import (
    build_seed_quarantine_anti_join,
    seed_serving_currency,
)
from services.region_pricing import pricing_currency_for_region_or_none
from services.outbound_links_service import (
    DEFAULT_UTM_TEMPLATE,
    apply_utm,
    get_allowed_domains_for_market,
    is_destination_domain_allowed,
)


logger = logging.getLogger(__name__)

EXTERNAL_REFERRAL_GATING_POLICY_VERSION = "external_referral_v1"
EXTERNAL_REFERRAL_STALE_DAYS = 7

# Minimum share of a run's candidates that must actually reach the origin before the run counts
# as a success. Measured on prod 2026-09-06 the nightly run sat at ~0.50 (2,647 refreshed minus
# 654 cache-served, over 4,000 candidates), so this threshold is deliberately set AT the current
# level: it is meant to fire now, because a night that reaches the origin for half its rows is
# the failure we are trying to make visible. Tunable without a deploy while hosts are triaged.
_MIN_ORIGIN_YIELD_DEFAULT = 0.5

# Minimum share of a run's candidates the loop must REACH before a budget stop counts as the
# steady state rather than an incident. Measured over the 20 nights 09-05..09-24 (prod, image
# 8caa012e9, --limit 4000): the healthy budget stops reached 3,119-3,647 rows (0.78-0.91) and the
# starved ones 81-1,602 (0.02-0.40), with nothing in between. Half sits in that gap, and it is
# also the drain arithmetic: 0.5 x 4,000 = 2,000 rows/night is roughly what it takes to touch
# ~14.4k attached seeds inside the 7-day `EXTERNAL_REFERRAL_STALE_DAYS` window. Tunable without
# a deploy, like the origin-yield floor.
_MIN_BUDGET_REACH_DEFAULT = 0.5

# A host that has answered this many 429/503s IN A ROW is skipped for the rest of the run. One
# host used to be able to spend the whole budget: `max_wait=0` waits out every hold, each hold
# doubles to the 300s cap, and fentybeauty.com answered 429 x9, let one request through, and
# started again — ~810s a cycle, four cycles a night, 81-85 rows refreshed on 09-20/21/22 out of
# 4,000. Four is the measured knee: across 208 host-nights with any 429, 151 never reached a
# streak of 4, and a streak of 4 costs 2+4+8 = 14s of holds before it trips. `<= 0` disables.
_HOST_BLOCK_TRIP_DEFAULT = 4

# The only two HTTPException details that mean "this seed can never be crawled", as opposed to
# "something went wrong this time". Kept as an explicit set so a new 4xx cannot quietly join them.
_UNPROCESSABLE_SEED_DETAILS = frozenset({"SEED_NOT_FOUND", "INVALID_URL"})


def _min_origin_yield() -> float:
    raw = os.getenv("EXTERNAL_REFERRAL_REFRESH_MIN_ORIGIN_YIELD", "").strip()
    try:
        value = float(raw) if raw else _MIN_ORIGIN_YIELD_DEFAULT
    except ValueError:
        return _MIN_ORIGIN_YIELD_DEFAULT
    return min(max(value, 0.0), 1.0)


def _min_budget_reach() -> float:
    """EXTERNAL_REFERRAL_REFRESH_MIN_BUDGET_REACH, read through the pacing knobs' shared helper
    (`crawl_global_pacer.resolve_setting`) since 2026-10-17: a share in (0, 1], and 0, negative or
    garbage is an ERROR and the default. TUNABLE BECAUSE THE GLOBAL PACER MOVED THE FLOOR UNDER IT:
    at 0.5 rows/s and 6 new hosts a minute a host-diverse queue head (1 row per host) reaches ~331
    of 2,000 rows in 3,300 s (0.17) and reads as degraded every night, while a head of >= 4 rows per
    host reaches 0.5. Which one prod has is what `pacing_census/refresh_head_hosts.py` measures;
    this knob and EXTERNAL_REFERRAL_REFRESH_MAX_NEW_HOSTS_PER_MINUTE are what to move after it,
    without a deploy (review M4)."""
    return crawl_global_pacer.resolve_setting(
        None, "EXTERNAL_REFERRAL_REFRESH_MIN_BUDGET_REACH", _MIN_BUDGET_REACH_DEFAULT, maximum=1.0,
    )


def _host_block_trip() -> int:
    raw = os.getenv("EXTERNAL_REFERRAL_REFRESH_HOST_BLOCK_TRIP", "").strip()
    try:
        return int(raw) if raw else _HOST_BLOCK_TRIP_DEFAULT
    except ValueError:
        return _HOST_BLOCK_TRIP_DEFAULT


def host_backoff_tripped(consecutive_blocks: int, trip: int) -> bool:
    """Should the batch stop asking this host for the rest of the run? Pure.

    `trip <= 0` disables the breaker, which restores the old unbounded behaviour exactly.
    """
    return trip > 0 and int(consecutive_blocks or 0) >= trip


# Degraded-reason buckets that mean the host gave NO ANSWER: nothing came back that says anything
# about the product. `_degraded_reason_bucket` names them.
_UNREACHABLE_REASON_BUCKETS = frozenset({"connection", "timeout", "dns", "tls"})

# A host that gave no answer this many rows IN A ROW is skipped for the rest of the run. `<= 0`
# disables. It exists for hosts that truly never answer; it CANNOT tell those apart from our own
# egress failing, and until 2026-09-28 almost every `connection` failure was our own egress.
#
# THE MEASURED CAUSE. `pivota-crawl-nat` ran on Cloud NAT defaults: 64 ports per VM, no dynamic
# allocation, 120s TCP TIME_WAIT. `_fetch_html` opens a fresh connection per fetch, and most
# storefronts sit behind Shopify's shared edge (23.227.38.x:443), which is ONE destination to NAT.
# 64 ports / 120s = ~0.53 new connections a second to it. The serial loop asked ~1/s, so ~half
# connected. That is ichibanm.com's "55% read", which an earlier version of this comment called
# a flaky host. NAT DROPPED logs matched the refresh's connection failures one for one:
# 1,431/1,431 on 09-26, 1,023/1,014 on 09-27, ~7,900/~7,650 on 09-28. On 09-28, four lanes pushed
# ~3.7/s, and this breaker skipped 2,764 rows on 20 HEALTHY hosts (kissusa, tarte, holiholic, ...).
# The NAT was widened on 09-28 (min 1024 / max 16384 ports, dynamic, 30s TIME_WAIT).
#
# 20, not 5, still stands, on the old arithmetic: at any interleaved failure rate f a false trip
# costs ~f^n per row. Re-derive it from post-widening runs before relying on the breaker, or on
# any per-host "unreachable" statistic measured before then.
_HOST_UNREACHABLE_TRIP_DEFAULT = 20

# A host that has answered this many Cloudflare CHALLENGES IN A ROW (`cf-mitigated: challenge`, at
# any status) is skipped for the rest of the run, its remaining rows unstamped (controller review of
# be#2640, 2026-10-17). Neither existing breaker could see it: the 429/503 one counts only those
# statuses, the unreachable one only network errors, and `CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS` only
# keeps a host out of the IP rule. ichibanm.com answered 239 requests with a 403 challenge in one run
# and was asked every time: about 15% of the run's slots spent on answers that cannot change. 3, like
# the IP rule's own idea of "not a fluke"; through the shared helper, so 0 is never "off".
_HOST_CHALLENGE_TRIP_DEFAULT = 3


def _host_challenge_trip() -> int:
    """EXTERNAL_REFERRAL_REFRESH_HOST_CHALLENGE_TRIP, then 3. Rounded up, at least 1."""
    return crawl_global_pacer.resolve_count(
        None, "EXTERNAL_REFERRAL_REFRESH_HOST_CHALLENGE_TRIP", _HOST_CHALLENGE_TRIP_DEFAULT
    )


# A host that has answered this many HTTP 423s ("Locked") IN A ROW is skipped for the rest of the run.
# None of the other breakers sees it: 423 is neither a 429/503 nor a challenge, and it is an answer, so
# it never counts as "no answer". Measured 2026-11-02..05: one host answered 423 to all 621 rows the
# refresh asked of it over 39 runs (0 origin reads, ~8 s a row, ~1,200 s a day), every row of it.
# The likeliest meaning is a firewall lock on our egress address, so this stops asking that ONE host
# for the run and retires nothing. A 423 on many unrelated hosts at once would point at the address
# itself; `crawl_ip_throttle` has NO 423 rule today, so that case would need one there, not here.
#
# ITS SKIPPED ROWS ARE STAMPED, UNLIKE THE OTHER HOST SKIPS. A 429 or a challenge is a throttle that
# passes, so those skips keep their place and lead the next run. A host that locks every request does
# not pass: unstamped, its rows would lead every run, take up to the per-host cap of the cut, cost 3
# requests and give the rest of their slots back to nobody. Stamping `last_crawl_attempt_at` (and only
# that: `last_crawled_at` stays, so the rows stay stale) keeps the rotation every degraded read gets:
# the host's rows wait behind the rest of the queue and are tried again when their turn comes.
# 3, like the challenge trip; through the shared helper, so 0 is never "off".
_HOST_LOCKED_TRIP_DEFAULT = 3


def _host_locked_trip() -> int:
    """EXTERNAL_REFERRAL_REFRESH_HOST_LOCKED_TRIP, then 3. Rounded up, at least 1."""
    return crawl_global_pacer.resolve_count(
        None, "EXTERNAL_REFERRAL_REFRESH_HOST_LOCKED_TRIP", _HOST_LOCKED_TRIP_DEFAULT
    )


async def _stamp_crawl_attempts(seed_ids: Sequence[str]) -> int:
    """Record an attempt (`last_crawl_attempt_at = NOW()`) on rows the 423 breaker passed over, and
    nothing else. Returns how many ids were sent. FAILS SOFT, loudly: unstamped, the rows simply lead
    the next run, which is the behaviour before this breaker existed."""
    ids = [str(s) for s in seed_ids if s]
    sent = 0
    try:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            params = {f"id{i}": seed_id for i, seed_id in enumerate(chunk)}
            placeholders = ", ".join(f":id{i}" for i in range(len(chunk)))
            await database.execute(
                f"UPDATE external_product_seeds SET last_crawl_attempt_at = NOW() WHERE id IN ({placeholders})",
                params,
            )
            sent += len(chunk)
    except Exception as exc:  # noqa: BLE001 - a failed stamp only costs the rotation, never the run
        logger.warning(
            "external referral refresh: could not stamp %d rows the 423 breaker passed over (%s); "
            "they lead the next run", len(ids) - sent, type(exc).__name__,
        )
    return sent

# Hosts read at once. 1 is the serial loop. Capped at 8: a host is never read by two workers at
# once, but every worker holds an event-loop slice for HTML parsing on a 1-vCPU job, and the
# DB pool (DB_POOL_MAX_SIZE) is shared.
_HOST_CONCURRENCY_DEFAULT = 1
_HOST_CONCURRENCY_MAX = 8


def _host_unreachable_trip() -> int:
    raw = os.getenv("EXTERNAL_REFERRAL_REFRESH_HOST_UNREACHABLE_TRIP", "").strip()
    try:
        return int(raw) if raw else _HOST_UNREACHABLE_TRIP_DEFAULT
    except ValueError:
        return _HOST_UNREACHABLE_TRIP_DEFAULT


def _refresh_host_concurrency(explicit: Optional[int]) -> int:
    """The flag wins, then EXTERNAL_REFERRAL_REFRESH_HOST_CONCURRENCY, then 1. Clamped to [1, 8]."""
    raw: Any = explicit
    if raw is None:
        raw = os.getenv("EXTERNAL_REFERRAL_REFRESH_HOST_CONCURRENCY", "").strip() or None
    try:
        value = int(raw) if raw is not None else _HOST_CONCURRENCY_DEFAULT
    except (TypeError, ValueError):
        value = _HOST_CONCURRENCY_DEFAULT
    return min(max(value, 1), _HOST_CONCURRENCY_MAX)


# THE IP'S TOTAL RATE (`services.crawl_global_pacer`), and how fast the run may start on new hosts.
#
# 0.5 origin requests/s, MEASURED AGAINST WHAT GOT THE ADDRESS LISTED. 10-08..10-15 the refresh read
# 8,155-13,156 rows per 3,300 s run at host concurrency 4: 2.5-4.0 requests/s, every night, from one
# NAT address, plus the destination sweep's ~5,000 reads in ~15 min (~5/s) at 03:15Z. On 10-16
# Cloudflare challenge-listed the address. The serial loop of 09-27 (3,282 rows/3,300 s, ~1/s) was
# never listed; 0.5/s is half of that and one fifth to one eighth of the listing nights, and leaves
# room for the other crawl jobs on the same IP (merchant-purchasability-sweep: <= ~7 requests/hour;
# the store-audit probes: no merchant requests at all in the 24 h measured). 1,650 requests per
# 3,300 s budget, of which every new host's robots.txt is one.
#
# 6 NEW HOSTS A MINUTE because a challenged request answers instantly: with no cap, four workers
# swept 262 distinct hosts in 60 s on 10-16. Cloudflare scores the address across every zone it
# fronts, so breadth is what it sees. 6/min (one every 10 s) is <= 330 hosts in a 55 min run.
_GLOBAL_RPS_DEFAULT = crawl_global_pacer.RPS_DEFAULT
_MAX_NEW_HOSTS_PER_MINUTE_DEFAULT = crawl_global_pacer.MAX_NEW_HOSTS_PER_MINUTE_DEFAULT


# ZERO IS NOT "OFF" (2026-10-17 review, M3). Both knobs used to accept 0 as "disabled", and the cap
# went through `int()`, so 0.9 truncated to 0 and switched it off too. Both now resolve through
# `crawl_global_pacer`, the one helper the destination sweep's knobs use as well: 0, negative, NaN or
# garbage is an ERROR and the default; the rate is clamped to [0.05, 1.0]; the cap is rounded up and
# is at least 1. The only off switch is CRAWL_GLOBAL_PACER_DISABLED=true (the exact word; there is no
# per-lane alias), which logs at ERROR every run.
def _refresh_global_rps(explicit: Optional[float]) -> float:
    """`--global-rps`, then EXTERNAL_REFERRAL_REFRESH_GLOBAL_RPS, then 0.5; clamped to [0.05, 1.0]."""
    return crawl_global_pacer.resolve_rps(explicit, "EXTERNAL_REFERRAL_REFRESH_GLOBAL_RPS", _GLOBAL_RPS_DEFAULT)


def _refresh_max_new_hosts_per_minute(explicit: Optional[float]) -> int:
    """`--max-new-hosts-per-minute`, then EXTERNAL_REFERRAL_REFRESH_MAX_NEW_HOSTS_PER_MINUTE, then
    6; rounded up, at least 1."""
    return crawl_global_pacer.resolve_new_host_cap(
        explicit, "EXTERNAL_REFERRAL_REFRESH_MAX_NEW_HOSTS_PER_MINUTE", _MAX_NEW_HOSTS_PER_MINUTE_DEFAULT
    )


def _refresh_global_pacer_disabled() -> bool:
    """The kill switch, logged at ERROR by `crawl_global_pacer.pacer_disabled` on every run."""
    return crawl_global_pacer.pacer_disabled(
        lane="external-referral-refresh",
    )


_MAX_STRUCTURAL_SHARE_DEFAULT = 0.2


def _max_structural_share() -> float:
    """Above this share of attempted projections, structural skips with no attached write degrade
    the run. EXTERNAL_REFERRAL_REFRESH_MAX_STRUCTURAL_SHARE; >= 1 disables the rule."""
    raw = (os.getenv("EXTERNAL_REFERRAL_REFRESH_MAX_STRUCTURAL_SHARE") or "").strip()
    try:
        return float(raw) if raw else _MAX_STRUCTURAL_SHARE_DEFAULT
    except ValueError:
        return _MAX_STRUCTURAL_SHARE_DEFAULT


# A SPIKE IN CURRENCY REFUSALS (services/seed_offer_currency). A seed whose column and page read
# disagree, or that states no currency, makes the offer writer write nothing (`refused_currency`, a
# structural skip). On 2026-10-19 that was 1,254 of 26,950 active seeds, ~4.7%. A night sampling the
# queue should refuse about that share of its ATTEMPTED projections: every attempted projection is one
# seed, and both lanes (mirror and attached) refuse a seed with no currency of its own. The count is
# a LOWER bound: the attached lane reports `refused_currency` only for a seed that reaches its
# currency check (after no_listing_offer / listing_offer_suppressed / ambiguous_listing_seller /
# currency_not_read); past that check every such seed is refused whole, whoever wrote its rows
# (Peng 2026-10-22). Twice it (10%) means reads started disagreeing with the
# stored column (a geo-served storefront, an extractor regression), and those seeds silently stop
# being priced. The structural-share rule cannot see it while the attached lane writes anything.
# The minimum count keeps a tiny night (1 refusal in 5) from flapping.
_MAX_REFUSED_CURRENCY_SHARE_DEFAULT = 0.10
_MIN_REFUSED_CURRENCY_COUNT_DEFAULT = 20


def _max_refused_currency_share() -> float:
    """EXTERNAL_REFERRAL_REFRESH_MAX_REFUSED_CURRENCY_SHARE; >= 1 disables the rule.

    Clamped: a share <= 0 (or not a number) would degrade every night that refuses anything, and a
    degraded run exits 1 and fails the Cloud Run execution, so it falls back to the default."""
    raw = (os.getenv("EXTERNAL_REFERRAL_REFRESH_MAX_REFUSED_CURRENCY_SHARE") or "").strip()
    try:
        value = float(raw) if raw else _MAX_REFUSED_CURRENCY_SHARE_DEFAULT
    except ValueError:
        return _MAX_REFUSED_CURRENCY_SHARE_DEFAULT
    return value if math.isfinite(value) and value > 0 else _MAX_REFUSED_CURRENCY_SHARE_DEFAULT


def _min_refused_currency_count() -> int:
    """EXTERNAL_REFERRAL_REFRESH_MIN_REFUSED_CURRENCY_COUNT: below this many refusals the share is
    noise. Clamped to >= 1 for the same reason as the share."""
    raw = (os.getenv("EXTERNAL_REFERRAL_REFRESH_MIN_REFUSED_CURRENCY_COUNT") or "").strip()
    try:
        value = int(raw) if raw else _MIN_REFUSED_CURRENCY_COUNT_DEFAULT
    except ValueError:
        return _MIN_REFUSED_CURRENCY_COUNT_DEFAULT
    return value if value >= 1 else _MIN_REFUSED_CURRENCY_COUNT_DEFAULT


def batch_run_status(
    *,
    failed: int,
    stopped_early: bool,
    attempted_count: int,
    origin_reads: int,
    price_changes: int = 0,
    projections_attempted: int = 0,
    projections_written: int = 0,
    projections_errored: int = 0,
    projections_structural_skips: int = 0,
    projections_written_attached: int = 0,
    candidate_count: int = 0,
    skipped_for_budget: int = 0,
    projections_refused_currency: int = 0,
) -> str:
    """The run's health, as one word. Pure so it can be tested without a database.

    Extracted rather than inlined in the summary dict BECAUSE a test that re-implements this
    arithmetic proves nothing about the job — it passes whether or not the production path uses
    it. Both the summary and the tests call this.

    `failed` alone is the old rule and it is nearly always 0: a degraded read is not an
    exception. The two ways a run silently does nothing are stopping on budget and reaching the
    origin for too few of its candidates.
    """
    if failed:
        return "degraded"
    # A budget stop ALONE is the steady state, not an incident: with --limit 4000 over ~14k
    # attached seeds the queue drains across several nights, and healthy nights stop with
    # 78-91% of their candidates reached. What is an incident is a stop FAR short of that. The
    # yield rule below cannot see it — 09-23 reached 369/4,000 rows and still read 0.63 yield,
    # because the few rows it did reach mostly read fine. Judged on rows REACHED (candidates
    # minus `skipped_for_budget`), so rows the host breaker deliberately passed over do not
    # count against the run; they are reported on their own.
    if (
        stopped_early
        and candidate_count > 0
        and (candidate_count - max(0, skipped_for_budget)) / candidate_count < _min_budget_reach()
    ):
        return "degraded"
    if attempted_count and (origin_reads / attempted_count) < _min_origin_yield():
        return "degraded"
    # Projection ran and healed nothing while prices were moving: the offers table is still
    # drifting from the seed, which is the whole defect this hook exists to close. Silent
    # before, because the outcome dicts were discarded — `no_mirror_product` x2,000 and
    # `synced` x2,000 read identically.
    #
    # Judged on the projections that SHOULD write. A structural skip (the seed has no offer row
    # the writer may touch: OFFER_SYNC_STRUCTURAL_SKIP_STATUSES) is the catalogue's shape, not a
    # failed write. Counting them made 09-27 exit 1 on `no_mirror_product` 2,220/2,220 with
    # nothing broken, and a job that fails every night hides the night something does break.
    projections_expected = max(0, projections_attempted - max(0, projections_structural_skips))
    if price_changes and projections_expected and projections_written == 0:
        return "degraded"
    # ...BUT A NIGHT THAT IS MOSTLY STRUCTURAL AND WROTE NO CANONICAL OFFER IS NOT HEALTHY EITHER.
    # Since the attached lane exists, 19,782 of 19,808 served attached seeds have a listing row to
    # write (2026-09-28), so a structural skip is the exception. A run where they dominate and the
    # attached lane wrote nothing is 09-27 again (2,220 skips, 0 writes, 16 prices moved): the lane
    # did not run, or the queue holds only rows it cannot reach. Without this, the rule above
    # would report that night as success.
    if (
        projections_attempted
        and projections_written_attached == 0
        and max(0, projections_structural_skips) / projections_attempted > _max_structural_share()
    ):
        return "degraded"
    # Every projection that was attempted raised. Distinct from the rule above: prices may
    # not have moved (so `price_changes` is 0) and the writer may still have blown up on
    # every row -- the error count was previously collected and read by nothing.
    if projections_attempted and projections_errored >= projections_attempted:
        return "degraded"
    # Refusals are structural (they leave the rules above alone) but a SPIKE of them is not the
    # catalogue's shape: see _MAX_REFUSED_CURRENCY_SHARE_DEFAULT.
    refused = max(0, projections_refused_currency)
    if (
        projections_attempted
        and refused >= _min_refused_currency_count()
        and refused / projections_attempted > _max_refused_currency_share()
    ):
        return "degraded"
    return "success"


def _bump(counter: Dict[str, int], key: str) -> None:
    counter[key] = int(counter.get(key) or 0) + 1


# How many hosts the handle-less read summary lists (by rows decided), beside its totals.
HANDLELESS_TOP_HOSTS = 25


def count_handleless_read(
    totals: Dict[str, int], by_host: Dict[str, Dict[str, int]], host: str, info: Any
) -> None:
    """Count one row's handle-less read decision (`result["handleless_read"]`, from
    routes/employee_products._handleless_read_outcome), into the run's totals and its host's counts:
    `decided`, `would_read`, `read`, `would_flip_healthy`, `refused:<reason>`, `segment:<segment>`."""
    if not isinstance(info, dict):
        return
    counters = (totals, by_host.setdefault(host or "unknown", {}))
    keys = ["decided"]
    if info.get("would_read"):
        keys.append("would_read")
        if info.get("mode") == "on":
            keys.append("read")
        if info.get("would_flip_healthy"):
            keys.append("would_flip_healthy")
    if info.get("refusal"):
        keys.append(f"refused:{info['refusal']}")
    if info.get("segment"):
        keys.append(f"segment:{info['segment']}")
    for counter in counters:
        for key in keys:
            _bump(counter, key)


def handleless_read_summary(mode: str, totals: Dict[str, int], by_host: Dict[str, Dict[str, int]]) -> Dict[str, Any]:
    """The run's handle-less read decisions: the mode, the totals, and the `HANDLELESS_TOP_HOSTS` hosts with
    the most rows decided."""
    top = sorted(by_host.items(), key=lambda kv: (-int(kv[1].get("decided") or 0), kv[0]))[:HANDLELESS_TOP_HOSTS]
    return {"mode": mode, "totals": dict(sorted(totals.items())), "top_hosts": {h: dict(sorted(c.items())) for h, c in top}}


def _degraded_reason_bucket(error: Any) -> str:
    """Collapse a degraded row's error into a small, readable bucket.

    Buckets, not raw strings: the raw text carries URLs and per-host detail, which makes the
    histogram unreadable and can leak a destination into a log line. An HTTP status keeps its
    code because that is the actionable part (403/429 = blocked, 404 = gone, 5xx = their side).
    """
    text = str(error or "").strip().lower()
    if not text:
        return "unspecified"
    # Keywords FIRST, and the status pattern anchored on an http-ish prefix. A bare
    # `\b([45]\d{2})\b` run first turned `port=443` inside a timeout message into bucket
    # `http_443` — a port number is not a status code, and the timeout was the real reason.
    for needle, bucket in (
        ("timeout", "timeout"), ("timed out", "timeout"),
        ("robots", "robots"), ("crawl", "crawl_paced"),
        ("ssl", "tls"), ("tls", "tls"), ("certificate", "tls"),
        ("dns", "dns"), ("name or service", "dns"), ("getaddrinfo", "dns"),
        ("connection", "connection"), ("refused", "connection"),
        ("challenge", "bot_challenge"), ("captcha", "bot_challenge"),
        ("snapshot_failed", "snapshot_failed"),
    ):
        if needle in text:
            return bucket
    # A status code is introduced by a word that means "status", never by `port=`. Keywords have
    # already run, so a timeout carrying `port=443` never reaches here; this guards the residue.
    match = re.search(
        r"(?:http|https|status|code|returned|responded)[ _:=]*\s*([45]\d{2})(?!\d)", text
    )
    if match:
        return f"http_{match.group(1)}"
    return "other"

EXTERNAL_REFERRAL_BLOCKER_ANOMALIES = {
    "locale_market_mismatch",
    "non_product_fallback_page",
    "price_currency_mismatch",
    "zero_variants",
    "stale_snapshot",
    "redirect_unavailable",
    "destination_domain_not_allowed",
    # A FACT, not a clock. `stale_snapshot` says "we have not looked recently";
    # `destination_dead` says "we looked, twice, a day apart, and the product was gone".
    # Only the second one can be trusted enough to be worth acting on, which is why
    # retirement hangs off it and not off age (docs/external-seed-dead-pdp-link-audit.md).
    "destination_dead",
    # Never verified at all. This used to be the FAIL-OPEN case: the staleness check was
    # guarded on `extracted_dt is not None`, so a seed with no observation behind it passed.
    "destination_never_verified",
    # The LINK check has gone stale. Distinct from `stale_snapshot`, which is about the
    # CONTENT: a sweep proves the URL resolves without reading a price, and a content refresh
    # reads a price without proving the URL still resolves. One field cannot answer both.
    "destination_stale",
}
# Recall only needs a real product destination that is safe to hand to the
# merchant checkout.  The broader audit blocker set above still drives refresh
# work and transaction-readiness reporting, but it must not remove an otherwise
# usable offer from discovery merely because its cached content is old, its
# local variant projection is incomplete, or its last liveness check is stale.
EXTERNAL_REFERRAL_RUNTIME_BLOCKER_ANOMALIES = {
    "non_product_fallback_page",
    "redirect_unavailable",
    "destination_domain_not_allowed",
    "destination_dead",
}


def external_referral_live_verification_reasons(status: ExternalReferralStatus) -> List[str]:
    """Audit blockers that require live commerce validation but allow recall."""
    return sorted(
        set(getattr(status, "blocker_anomaly_types", []) or [])
        - EXTERNAL_REFERRAL_RUNTIME_BLOCKER_ANOMALIES
    )


EXTERNAL_REFERRAL_REVIEW_ANOMALIES = {
    "zero_images",
    "generic_template_description",
    "manual_image_override_present",
    "gift_card_duplicate_sku",
}

_REFERRAL_METRICS_LOCK = Lock()
_REFERRAL_METRICS: Dict[str, Any] = {
    "referral_seeds_evaluated": 0,
    "referral_seeds_blocked_total": 0,
    "referral_seeds_blocked_by_reason": {},
    "referral_runtime_filtered_total": 0,
    "referral_redirect_generation_failures": 0,
    "referral_domain_allowlist_failures": 0,
    "referral_stale_snapshot_total": 0,
}


@dataclass(frozen=True)
class ExternalReferralIssueBucket:
    issue_type: str
    severity: str
    count: int
    operator_action: str
    sample_seed_ids: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExternalReferralStatus:
    seed_id: str
    status: str
    gating_policy_version: str
    matched_via: str
    blocker_anomaly_types: List[str] = field(default_factory=list)
    review_anomaly_types: List[str] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    last_extracted_at: Optional[str] = None
    tracked_destination_url: Optional[str] = None


@dataclass(frozen=True)
class AttachedFallbackSeedSummary:
    merchant_id: str
    status: str
    gating_policy_version: str
    matched_domains: List[str]
    total_active_seeds: int
    attached_seed_count: int
    domain_unattached_seed_count: int
    healthy_seed_count: int
    blocked_seed_count: int
    review_seed_count: int
    issue_buckets: List[Dict[str, Any]]
    sample_blocked_seeds: List[Dict[str, Any]]
    last_extracted_at_oldest: Optional[str]
    last_extracted_at_newest: Optional[str]


@dataclass(frozen=True)
class PlatformFallbackIssueBucket:
    issue_type: str
    severity: str
    count: int
    operator_action: str
    sample_seed_ids: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlatformFallbackDomainSummary:
    domain: str
    count: int


@dataclass(frozen=True)
class PlatformFallbackRuntimeSurfaceCoverageSummary:
    total_surface_eligible_seeds: int
    total_surface_blocked_seeds: int
    surface_eligible_rate_pct: float
    attached_surface_eligible_seed_count: int
    attached_surface_blocked_seed_count: int


@dataclass(frozen=True)
class PlatformFallbackProgramSummary:
    status: str
    generated_at: str
    gating_policy_version: str
    total_active_seeds: int
    healthy_seed_count: int
    blocked_seed_count: int
    review_seed_count: int
    attached_seed_count: int
    unattached_seed_count: int
    issue_buckets: List[Dict[str, Any]]
    top_domains: List[Dict[str, Any]]
    top_blocked_domains: List[Dict[str, Any]]
    runtime_surface_coverage_summary: Dict[str, Any]


@dataclass(frozen=True)
class MerchantCommerceCohortMerchant:
    merchant_id: str
    business_name: str
    store_count: int
    catalog_product_count: int
    psp_connected: bool
    psp_providers: List[str]
    invalid_reasons: List[str]
    operator_action: str


@dataclass(frozen=True)
class MerchantCommerceCohortSummary:
    generated_at: str
    total_registered_merchants: int
    store_connected_merchants: int
    store_connected_with_psp_merchants: int
    merchant_valid_count: int
    merchant_invalid_count: int
    top_invalid_merchants: List[Dict[str, Any]]


@dataclass(frozen=True)
class MerchantCommerceReadinessListItem:
    merchant_id: str
    business_name: str
    status: str
    merchant_valid: bool
    rollout_ready: bool
    store_count: int
    connected_store_domain_count: int
    catalog_product_count: int
    psp_connected: bool
    psp_provider_count: int
    psp_providers: List[str]
    paid_orders_last_30_days: int
    all_time_paid_orders: int
    invalid_reasons: List[str]
    operator_action: str


@dataclass(frozen=True)
class MerchantCommerceReadinessListSummary:
    generated_at: str
    total_registered_merchants: int
    merchant_valid_count: int
    rollout_ready_count: int
    attention_count: int
    merchants: List[Dict[str, Any]]


# The ambiguous-price guard (EXTERNAL_SEED_AMBIGUOUS_PRICE_GUARD): hosts listed beside its totals, and at most this
# many AMBIGUOUS_PRICE_DECISION lines per run (refusals only; the rest are counted as `suppressed`).
AMBIGUOUS_PRICE_TOP_HOSTS = 25
AMBIGUOUS_PRICE_LOG_MAX = 500


def count_ambiguous_price(
    totals: Dict[str, int],
    by_host: Dict[str, Dict[str, int]],
    lines: Dict[str, int],
    seed_id: Any,
    host: Any,
    info: Any,
) -> None:
    """Count one row's ambiguous-price decision (`result["ambiguous_price"]`, routes/employee_products: a page with
    several priced products or a listing) into the run's totals and its host's: `decided`, `named_by:<how>`,
    `would_refuse` (shadow) or `refused` (on), `refused:<reason>`. A refusal is also logged, one
    `AMBIGUOUS_PRICE_DECISION {json}` line through the `pivota` logger (the one prod keeps at INFO), at most
    `AMBIGUOUS_PRICE_LOG_MAX` per run (`lines` counts `logged` / `suppressed`). Ids, the host and the decision only: no
    URL or title. NEVER RAISES: it runs inside the row's try, where a raise would count a refreshed row as failed."""
    try:
        if not isinstance(info, dict):
            return
        host_key = str(host or "unknown")
        keys = ["decided"]
        if info.get("named_by"):
            keys.append(f"named_by:{info['named_by']}")
        refusal = info.get("refusal")
        if refusal:
            keys.append("refused" if info.get("mode") == "on" else "would_refuse")
            keys.append(f"refused:{refusal}")
        for counter in (totals, by_host.setdefault(host_key, {})):
            for key in keys:
                _bump(counter, key)
        if not refusal:
            return
        if int(lines.get("logged") or 0) >= AMBIGUOUS_PRICE_LOG_MAX:
            lines["suppressed"] = int(lines.get("suppressed") or 0) + 1
            return
        lines["logged"] = int(lines.get("logged") or 0) + 1
        from utils.logger import logger as pivota_log

        line = {
            "seed_id": str(seed_id) if seed_id is not None else None,
            "host": host_key,
            "mode": info.get("mode"),
            "refusal": refusal,
            "priced_identities": info.get("priced_identities"),
            # What a reviewer needs to tell a bundle from a false refusal: how the node's url relates to the served
            # one, and how close the titles are. Still no URL and no title.
            "url_relation": info.get("url_relation"),
            "title_dice": info.get("title_dice"),
            # Who wrote the title the guard was given (`_guard_title_and_origin`): employee | page | legacy_equal |
            # legacy_differs | unknown_stamp | none. The label only, never the title (review P3-5).
            "title_from": info.get("title_from"),
        }
        pivota_log.info("AMBIGUOUS_PRICE_DECISION " + json.dumps(line, separators=(",", ":"), sort_keys=True, default=str))
    except Exception:  # noqa: BLE001 - reporting must never cost a row its count
        return


def ambiguous_price_summary(
    mode: str, totals: Dict[str, int], by_host: Dict[str, Dict[str, int]], lines: Dict[str, int]
) -> Dict[str, Any]:
    """The run's ambiguous-price decisions: the mode, the totals, the `AMBIGUOUS_PRICE_TOP_HOSTS` hosts with the most
    refusals (then decisions), and `decision_lines` {logged, suppressed}."""
    def rank(kv: Tuple[str, Dict[str, int]]) -> Tuple[int, int, str]:
        c = kv[1]
        return (-int(c.get("refused") or c.get("would_refuse") or 0), -int(c.get("decided") or 0), kv[0])

    top = sorted(by_host.items(), key=rank)[:AMBIGUOUS_PRICE_TOP_HOSTS]
    return {
        "mode": mode,
        "totals": dict(sorted(totals.items())),
        "top_hosts": {h: dict(sorted(c.items())) for h, c in top},
        "decision_lines": {"logged": int(lines.get("logged") or 0), "suppressed": int(lines.get("suppressed") or 0)},
    }


def _record_metric(name: str, delta: int = 1) -> None:
    with _REFERRAL_METRICS_LOCK:
        _REFERRAL_METRICS[name] = int(_REFERRAL_METRICS.get(name) or 0) + int(delta)


def _record_blocked_reasons(reasons: Iterable[str]) -> None:
    normalized = [str(reason or "").strip() for reason in reasons if str(reason or "").strip()]
    if not normalized:
        return
    with _REFERRAL_METRICS_LOCK:
        _REFERRAL_METRICS["referral_seeds_blocked_total"] = int(
            _REFERRAL_METRICS.get("referral_seeds_blocked_total") or 0
        ) + 1
        bucket = dict(_REFERRAL_METRICS.get("referral_seeds_blocked_by_reason") or {})
        for reason in normalized:
            bucket[reason] = int(bucket.get(reason) or 0) + 1
            if reason == "destination_domain_not_allowed":
                _REFERRAL_METRICS["referral_domain_allowlist_failures"] = int(
                    _REFERRAL_METRICS.get("referral_domain_allowlist_failures") or 0
                ) + 1
            elif reason == "redirect_unavailable":
                _REFERRAL_METRICS["referral_redirect_generation_failures"] = int(
                    _REFERRAL_METRICS.get("referral_redirect_generation_failures") or 0
                ) + 1
            elif reason == "stale_snapshot":
                _REFERRAL_METRICS["referral_stale_snapshot_total"] = int(
                    _REFERRAL_METRICS.get("referral_stale_snapshot_total") or 0
                ) + 1
        _REFERRAL_METRICS["referral_seeds_blocked_by_reason"] = bucket


def get_external_referral_metrics() -> Dict[str, Any]:
    with _REFERRAL_METRICS_LOCK:
        return {
            "referral_seeds_evaluated": int(_REFERRAL_METRICS.get("referral_seeds_evaluated") or 0),
            "referral_seeds_blocked_total": int(_REFERRAL_METRICS.get("referral_seeds_blocked_total") or 0),
            "referral_seeds_blocked_by_reason": dict(_REFERRAL_METRICS.get("referral_seeds_blocked_by_reason") or {}),
            "referral_runtime_filtered_total": int(_REFERRAL_METRICS.get("referral_runtime_filtered_total") or 0),
            "referral_redirect_generation_failures": int(
                _REFERRAL_METRICS.get("referral_redirect_generation_failures") or 0
            ),
            "referral_domain_allowlist_failures": int(
                _REFERRAL_METRICS.get("referral_domain_allowlist_failures") or 0
            ),
            "referral_stale_snapshot_total": int(_REFERRAL_METRICS.get("referral_stale_snapshot_total") or 0),
        }


def normalize_referral_domain(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    if not candidate:
        return ""
    try:
        if "://" in candidate:
            candidate = (urlparse(candidate).hostname or "").strip().lower()
        else:
            candidate = candidate.split("/", 1)[0].strip().lower()
    except Exception:
        return ""
    candidate = candidate.strip(".")
    if candidate.startswith("www."):
        candidate = candidate[4:]
    return candidate


def _row_to_dict(row: Any) -> Dict[str, Any]:
    return dict(row or {})


def _match_seed_domain_to_merchant_domains(seed_domain: Optional[str], merchant_domains: Sequence[str]) -> bool:
    normalized_seed_domain = normalize_referral_domain(seed_domain)
    if not normalized_seed_domain:
        return False
    for merchant_domain in merchant_domains:
        if normalized_seed_domain == merchant_domain or normalized_seed_domain.endswith(f".{merchant_domain}"):
            return True
    return False


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_iso(value: Any) -> Optional[str]:
    parsed = _parse_timestamp(value)
    return parsed.isoformat() if parsed else None


def _product_data_to_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _extract_storefront_handle_from_product_data(product_data: Dict[str, Any]) -> Optional[str]:
    raw = _product_data_to_dict(product_data.get("raw"))
    platform_metadata = _product_data_to_dict(product_data.get("platform_metadata"))
    for candidate in (
        product_data.get("handle"),
        raw.get("handle"),
        platform_metadata.get("handle"),
    ):
        value = str(candidate or "").strip().strip("/")
        if value:
            return value
    return None


def _product_has_storefront_backfill_candidate(product_data: Dict[str, Any]) -> bool:
    handle = _extract_storefront_handle_from_product_data(product_data)
    if not handle:
        return False
    if product_data.get("price") is not None:
        return True
    variants = product_data.get("variants")
    if isinstance(variants, list) and variants:
        return True
    raw = _product_data_to_dict(product_data.get("raw"))
    raw_variants = raw.get("variants")
    if isinstance(raw_variants, list) and raw_variants:
        return True
    return False


def _status_from_counts(*, total_active_seeds: int, blocked_seed_count: int, review_seed_count: int) -> str:
    if total_active_seeds <= 0:
        return "red"
    if blocked_seed_count > 0:
        return "red"
    if review_seed_count > 0:
        return "yellow"
    return "green"


def _fleet_status_from_counts(
    *,
    total_merchants: int,
    merchants_with_attached_referral_seeds: int,
    merchants_with_blocked_referrals: int,
    merchants_with_review_referrals: int,
) -> str:
    if total_merchants <= 0:
        return "red"
    coverage_rate = merchants_with_attached_referral_seeds / float(total_merchants)
    if merchants_with_attached_referral_seeds <= 0 or coverage_rate < 0.5:
        return "red"
    if (
        merchants_with_attached_referral_seeds < total_merchants
        or merchants_with_blocked_referrals > 0
        or merchants_with_review_referrals > 0
    ):
        return "yellow"
    return "green"


async def get_merchant_referral_domains(merchant_id: str) -> List[str]:
    rows = await database.fetch_all(
        """
        SELECT domain
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND domain IS NOT NULL
          AND TRIM(domain) != ''
        ORDER BY connected_at DESC NULLS LAST, store_id ASC
        """,
        {"merchant_id": merchant_id},
    )
    seen: set[str] = set()
    domains: List[str] = []
    for row in rows or []:
        normalized = normalize_referral_domain(_row_to_dict(row).get("domain"))
        if normalized and normalized not in seen:
            seen.add(normalized)
            domains.append(normalized)
    return domains


async def _fetch_catalog_product_counts_by_merchant() -> Dict[str, int]:
    rows = await database.fetch_all(
        """
        SELECT merchant_id, COUNT(DISTINCT (platform || '|' || platform_product_id)) AS product_count
        FROM products_cache
        WHERE platform_product_id IS NOT NULL
          AND (expires_at IS NULL OR expires_at > NOW())
        GROUP BY merchant_id
        """
    )
    counts: Dict[str, int] = {}
    for row in rows or []:
        row_dict = _row_to_dict(row)
        merchant_id = str(row_dict.get("merchant_id") or "").strip()
        if not merchant_id:
            continue
        counts[merchant_id] = int(row_dict.get("product_count") or 0)
    return counts


async def _fetch_store_domains_by_merchant() -> Dict[str, List[str]]:
    rows = await database.fetch_all(
        """
        SELECT merchant_id, domain
        FROM merchant_stores
        WHERE domain IS NOT NULL
          AND TRIM(domain) != ''
        ORDER BY merchant_id ASC, connected_at DESC NULLS LAST, store_id ASC
        """
    )
    domains_by_merchant: Dict[str, List[str]] = {}
    seen_pairs: set[Tuple[str, str]] = set()
    for row in rows or []:
        row_dict = _row_to_dict(row)
        merchant_id = str(row_dict.get("merchant_id") or "").strip()
        domain = normalize_referral_domain(row_dict.get("domain"))
        if not merchant_id or not domain:
            continue
        pair = (merchant_id, domain)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        domains_by_merchant.setdefault(merchant_id, []).append(domain)
    return domains_by_merchant


async def _fetch_active_psp_providers_by_merchant() -> Dict[str, List[str]]:
    rows = await database.fetch_all(
        """
        SELECT merchant_id, provider
        FROM merchant_psps
        WHERE status = 'active'
          AND merchant_id IS NOT NULL
          AND TRIM(merchant_id) != ''
        ORDER BY merchant_id ASC, provider ASC
        """
    )
    providers_by_merchant: Dict[str, List[str]] = {}
    seen_pairs: set[Tuple[str, str]] = set()
    for row in rows or []:
        row_dict = _row_to_dict(row)
        merchant_id = str(row_dict.get("merchant_id") or "").strip()
        provider = str(row_dict.get("provider") or "").strip().lower()
        if not merchant_id or not provider:
            continue
        pair = (merchant_id, provider)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        providers_by_merchant.setdefault(merchant_id, []).append(provider)
    return providers_by_merchant


async def _fetch_paid_order_evidence_by_merchant() -> Dict[str, Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT
            merchant_id,
            SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days'
                     AND payment_status IN ('paid','completed','succeeded','success','settled','partially_refunded')
                     THEN 1 ELSE 0 END) AS paid_orders_last_30_days,
            SUM(CASE WHEN payment_status IN ('paid','completed','succeeded','success','settled','partially_refunded')
                     THEN 1 ELSE 0 END) AS paid_orders_all_time
        FROM orders
        WHERE merchant_id IS NOT NULL
          AND TRIM(merchant_id) != ''
          AND (is_deleted IS NULL OR is_deleted = FALSE)
        GROUP BY merchant_id
        """
    )
    evidence_by_merchant: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        row_dict = _row_to_dict(row)
        merchant_id = str(row_dict.get("merchant_id") or "").strip()
        if not merchant_id:
            continue
        evidence_by_merchant[merchant_id] = {
            "paid_orders_last_30_days": int(row_dict.get("paid_orders_last_30_days") or 0),
            "paid_orders_all_time": int(row_dict.get("paid_orders_all_time") or 0),
        }
    return evidence_by_merchant


async def _fetch_all_active_referral_seed_rows() -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT *
        FROM external_product_seeds
        WHERE status = 'active'
        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        """
    )
    return [_row_to_dict(row) for row in rows or []]


async def estimate_storefront_backfill_candidate_count(
    merchant_id: str,
    *,
    limit: int = 50,
) -> int:
    row_limit = max(int(limit or 50) * 8, 200)
    rows = await database.fetch_all(
        """
        SELECT product_data
        FROM (
          SELECT DISTINCT ON (platform, platform_product_id)
            platform,
            platform_product_id,
            product_data,
            cached_at,
            id
          FROM products_cache
          WHERE merchant_id = :merchant_id
            AND platform = 'shopify'
          ORDER BY platform, platform_product_id, cached_at DESC NULLS LAST, id DESC NULLS LAST
        ) latest
        LIMIT :limit
        """,
        {"merchant_id": merchant_id, "limit": row_limit},
    )
    candidate_count = 0
    for row in rows or []:
        product_data = _product_data_to_dict(_row_to_dict(row).get("product_data"))
        if not _product_has_storefront_backfill_candidate(product_data):
            continue
        candidate_count += 1
        if candidate_count >= int(limit or 50):
            break
    return candidate_count


def _build_domain_filter_sql(domains: Sequence[str], *, param_prefix: str) -> Tuple[str, Dict[str, Any]]:
    clauses: List[str] = []
    values: Dict[str, Any] = {}
    for idx, domain in enumerate(domains):
        exact_key = f"{param_prefix}_exact_{idx}"
        sub_key = f"{param_prefix}_sub_{idx}"
        values[exact_key] = domain
        values[sub_key] = f"%.{domain}"
        clauses.append(
            "("
            f"LOWER(COALESCE(domain, '')) = :{exact_key}"
            f" OR LOWER(COALESCE(domain, '')) LIKE :{sub_key}"
            ")"
        )
    if not clauses:
        return "FALSE", values
    return "(" + " OR ".join(clauses) + ")", values


async def fetch_merchant_referral_inventory(
    *,
    merchant_id: str,
    status: str = "active",
) -> Dict[str, Any]:
    matched_domains = await get_merchant_referral_domains(merchant_id)
    attached_rows = await database.fetch_all(
        """
        SELECT *
        FROM external_product_seeds
        WHERE status = :status
          AND attached_product_key LIKE :attached_prefix
        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        """,
        {
            "status": status,
            "attached_prefix": f"{merchant_id}|%",
        },
    )

    unattached_rows: List[Dict[str, Any]] = []
    if matched_domains:
        domain_clause, domain_values = _build_domain_filter_sql(matched_domains, param_prefix="merchant_domain")
        rows = await database.fetch_all(
            f"""
            SELECT *
            FROM external_product_seeds
            WHERE status = :status
              AND attached_product_key IS NULL
              AND {domain_clause}
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            """,
            {
                "status": status,
                **domain_values,
            },
        )
        unattached_rows = [_row_to_dict(row) for row in rows or []]

    attached = [_row_to_dict(row) for row in attached_rows or []]
    seen_seed_ids: set[str] = set()
    deduped_rows: List[Dict[str, Any]] = []
    matched_via_by_seed: Dict[str, str] = {}
    for row in attached:
        seed_id = str(row.get("id") or "").strip()
        if not seed_id or seed_id in seen_seed_ids:
            continue
        seen_seed_ids.add(seed_id)
        matched_via_by_seed[seed_id] = "attached_product_key"
        deduped_rows.append(row)
    for row in unattached_rows:
        seed_id = str(row.get("id") or "").strip()
        if not seed_id or seed_id in seen_seed_ids:
            continue
        if not _match_seed_domain_to_merchant_domains(row.get("domain"), matched_domains):
            continue
        seen_seed_ids.add(seed_id)
        matched_via_by_seed[seed_id] = "merchant_domain"
        deduped_rows.append(row)

    return {
        "merchant_id": merchant_id,
        "matched_domains": matched_domains,
        "attached_rows": attached,
        "domain_unattached_rows": [
            row for row in deduped_rows if matched_via_by_seed.get(str(row.get("id") or "").strip()) == "merchant_domain"
        ],
        "rows": deduped_rows,
        "matched_via_by_seed": matched_via_by_seed,
    }


def _seed_matches_q(row: Dict[str, Any], q: Optional[str]) -> bool:
    normalized_q = str(q or "").strip().lower()
    if not normalized_q:
        return True
    haystacks = [
        str(row.get("id") or ""),
        str(row.get("external_product_id") or ""),
        str(row.get("domain") or ""),
        str(row.get("title") or ""),
        str(row.get("canonical_url") or ""),
        str(row.get("destination_url") or ""),
        str((row.get("seed_data") or {}).get("external_product_id") or ""),
        str((row.get("seed_data") or {}).get("product_id") or ""),
    ]
    return any(normalized_q in value.lower() for value in haystacks if value)


def filter_referral_inventory_rows(
    rows: Sequence[Dict[str, Any]],
    *,
    q: Optional[str] = None,
    attached: Optional[bool] = None,
    domain: Optional[str] = None,
    market: Optional[str] = None,
    seed_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    normalized_domain = normalize_referral_domain(domain)
    normalized_market = str(market or "").strip().upper()
    normalized_seed_id = str(seed_id or "").strip()
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        if attached is True and not str(row.get("attached_product_key") or "").strip():
            continue
        if attached is False and str(row.get("attached_product_key") or "").strip():
            continue
        if normalized_domain and not _match_seed_domain_to_merchant_domains(row.get("domain"), [normalized_domain]):
            continue
        if normalized_market and str(row.get("market") or "").strip().upper() != normalized_market:
            continue
        if normalized_seed_id and str(row.get("id") or "").strip() != normalized_seed_id:
            continue
        if not _seed_matches_q(row, q):
            continue
        filtered.append(row)
        if limit is not None and len(filtered) >= int(limit):
            break
    return filtered


async def evaluate_external_referral_seed(
    row: Dict[str, Any],
    *,
    market: Optional[str] = None,
    tool: Optional[str] = None,
    matched_via: str = "unknown",
    allowed_domains: Optional[List[str]] = None,
) -> ExternalReferralStatus:
    normalized_row = _row_to_dict(row)
    _record_metric("referral_seeds_evaluated")
    audit_result = audit_external_seed_row(normalized_row)
    findings = [dict(finding) for finding in (audit_result.get("findings") or []) if isinstance(finding, dict)]
    payload = get_snapshot(normalized_row)
    seed_data = payload["seed_data"]
    snapshot = payload["snapshot"]
    seed_market = str(market or normalized_row.get("market") or "US").strip().upper() or "US"
    seed_tool = str(tool or normalized_row.get("tool") or "*").strip() or "*"
    utm_template = (
        normalized_row.get("utm_template")
        or seed_data.get("utm_template")
        or DEFAULT_UTM_TEMPLATE
    )
    destination_url = (
        str(normalized_row.get("canonical_url") or "").strip()
        or str(normalized_row.get("destination_url") or "").strip()
        or str(snapshot.get("canonical_url") or "").strip()
        or str(snapshot.get("destination_url") or "").strip()
        or str(seed_data.get("canonical_url") or "").strip()
        or str(seed_data.get("destination_url") or "").strip()
    )
    tracked_destination_url: Optional[str] = None

    if not destination_url.startswith(("http://", "https://")):
        findings.append(
            {
                "anomaly_type": "redirect_unavailable",
                "severity": "blocker",
                "recommended_action": "Patch destination_url or canonical_url before allowing referral runtime.",
                "auto_fixable": False,
                "evidence": {"destination_url": destination_url or None},
            }
        )
    else:
        try:
            tracked_destination_url = apply_utm(
                destination_url,
                utm_template,
                {"market": seed_market, "tool": seed_tool},
            )
        except Exception:
            tracked_destination_url = None
            findings.append(
                {
                    "anomaly_type": "redirect_unavailable",
                    "severity": "blocker",
                    "recommended_action": "Patch destination_url or utm_template before allowing referral runtime.",
                    "auto_fixable": False,
                    "evidence": {"destination_url": destination_url},
                }
            )

    if tracked_destination_url:
        runtime_allowed_domains = allowed_domains if allowed_domains is not None else await get_allowed_domains_for_market(market=seed_market)
        if not is_destination_domain_allowed(
            destination_url=tracked_destination_url,
            allowed_domains=runtime_allowed_domains,
        ):
            findings.append(
                {
                    "anomaly_type": "destination_domain_not_allowed",
                    "severity": "blocker",
                    "recommended_action": "Add the destination domain to the outbound allowlist or patch the referral destination.",
                    "auto_fixable": False,
                    "evidence": {
                        "destination_url": tracked_destination_url,
                        "allowed_domains": list(runtime_allowed_domains[:20]),
                    },
                }
            )

    # IS THE LINK STILL THERE? — three answers, and "we don't know" is one of them.
    #
    # This block used to ask `get_last_extracted_at`, whose fallback chain ends at
    # `row["updated_at"]`. That column moves whenever ANY writer touches the row, so it
    # measured "when did we last write this" and called it freshness — and it was guarded on
    # `extracted_dt is not None`, so a row with no timestamp at all passed. Both defects had
    # the same effect: a seed could look fresh without anyone ever having loaded the page.
    #
    # `destination_checked_at` is written only by a fetch that reached the origin
    # (services/external_seed_destination_liveness), so NULL genuinely means never verified
    # and is a blocker rather than a pass.
    verdict = get_destination_verdict(normalized_row)
    streak = get_destination_failure_streak(normalized_row)
    if verdict in CONFIRMED_DEAD_VERDICTS and streak >= RETIREMENT_STREAK:
        findings.append(
            {
                "anomaly_type": "destination_dead",
                "severity": "blocker",
                "recommended_action": "The brand no longer serves this product page. Repoint the seed or retire it.",
                "auto_fixable": False,
                "evidence": {
                    "destination_verdict": verdict,
                    "destination_failure_streak": streak,
                    "destination_http_status": normalized_row.get("destination_http_status"),
                },
            }
        )

    # TWO INDEPENDENT QUESTIONS, AND ONE FIELD CANNOT ANSWER BOTH.
    #
    # The first version of this block replaced the content-age check with the destination
    # check, which reads as a strict improvement and is not one: the sweep stamps
    # `destination_checked_at` from a catalogue read or a HEAD-shaped probe WITHOUT ever
    # reading a price. So the day the first full pass completed, ~11.3k rows carrying a
    # median-56-to-99-day-old price would have flipped from blocked to healthy — a serving
    # regression created by the very change meant to close one, and invisible to any
    # measurement taken before the sweep ran.
    #
    #   stale_snapshot     the CONTENT (price, availability) is old  -> the number we quote is wrong
    #   destination_stale  the LINK has not been re-verified         -> it may be gone by now
    #
    # Both are blockers, both are separately clearable, and neither substitutes for the other.
    extracted_at = get_content_extracted_at(normalized_row, snapshot)
    extracted_dt = _parse_timestamp(extracted_at)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=EXTERNAL_REFERRAL_STALE_DAYS)
    if extracted_dt is None or extracted_dt < stale_cutoff:
        findings.append(
            {
                "anomaly_type": "stale_snapshot",
                "severity": "blocker",
                "recommended_action": f"Refresh the seed snapshot to keep referral content fresher than {EXTERNAL_REFERRAL_STALE_DAYS} days.",
                "auto_fixable": True,
                "evidence": {
                    "last_extracted_at": _to_iso(extracted_dt) if extracted_dt else None,
                    "threshold_days": EXTERNAL_REFERRAL_STALE_DAYS,
                },
            }
        )

    last_checked_at = get_last_destination_check_at(normalized_row)
    checked_dt = _parse_timestamp(last_checked_at)
    if checked_dt is None:
        findings.append(
            {
                "anomaly_type": "destination_never_verified",
                "severity": "blocker",
                "recommended_action": "Run the destination sweep (jobs/external_seed_destination_sweep) before serving this seed.",
                "auto_fixable": True,
                "evidence": {"destination_checked_at": None},
            }
        )
    elif checked_dt < stale_cutoff:
        findings.append(
            {
                "anomaly_type": "destination_stale",
                "severity": "blocker",
                "recommended_action": f"Re-verify the seed destination to keep the link check fresher than {EXTERNAL_REFERRAL_STALE_DAYS} days.",
                "auto_fixable": True,
                "evidence": {
                    "destination_checked_at": _to_iso(checked_dt),
                    # Kept for continuity with the dashboards that already read this key.
                    "last_extracted_at": _to_iso(get_last_extracted_at(normalized_row, snapshot)),
                    "threshold_days": EXTERNAL_REFERRAL_STALE_DAYS,
                },
            }
        )

    blocker_anomaly_types = sorted(
        {
            str(finding.get("anomaly_type") or "").strip()
            for finding in findings
            if str(finding.get("severity") or "").strip().lower() == "blocker"
            or str(finding.get("anomaly_type") or "").strip() in EXTERNAL_REFERRAL_BLOCKER_ANOMALIES
        }
        & EXTERNAL_REFERRAL_BLOCKER_ANOMALIES
    )
    review_anomaly_types = sorted(
        {
            str(finding.get("anomaly_type") or "").strip()
            for finding in findings
            if (
                str(finding.get("severity") or "").strip().lower() in {"review", "info"}
                or str(finding.get("anomaly_type") or "").strip() in EXTERNAL_REFERRAL_REVIEW_ANOMALIES
            )
        }
        | ({str(finding.get("anomaly_type") or "").strip() for finding in findings} & EXTERNAL_REFERRAL_REVIEW_ANOMALIES)
    )
    status = "blocked" if blocker_anomaly_types else "review" if review_anomaly_types else "healthy"
    if blocker_anomaly_types:
        _record_blocked_reasons(blocker_anomaly_types)
    return ExternalReferralStatus(
        seed_id=str(normalized_row.get("id") or "").strip(),
        status=status,
        gating_policy_version=EXTERNAL_REFERRAL_GATING_POLICY_VERSION,
        matched_via=matched_via,
        blocker_anomaly_types=blocker_anomaly_types,
        review_anomaly_types=review_anomaly_types,
        findings=findings,
        # Still the CONTENT-extraction time: this field is what the employee dashboards
        # render, and re-pointing it at `destination_checked_at` would silently change a
        # column of dates people read as "when was this seed last extracted". The gate above
        # deliberately no longer uses it.
        last_extracted_at=_to_iso(get_last_extracted_at(normalized_row, snapshot)),
        tracked_destination_url=tracked_destination_url,
    )


async def build_external_referral_summary(merchant_id: str) -> Dict[str, Any]:
    inventory = await fetch_merchant_referral_inventory(merchant_id=merchant_id, status="active")
    rows = inventory["rows"]
    matched_via_by_seed = inventory["matched_via_by_seed"]
    statuses = await asyncio.gather(
        *[
            evaluate_external_referral_seed(
                row,
                matched_via=matched_via_by_seed.get(str(row.get("id") or "").strip(), "unknown"),
            )
            for row in rows
        ]
    )

    issue_counter: Counter[Tuple[str, str]] = Counter()
    issue_seed_samples: Dict[Tuple[str, str], List[str]] = {}
    blocked_samples: List[Dict[str, Any]] = []
    healthy_seed_count = 0
    blocked_seed_count = 0
    review_seed_count = 0
    extracted_timestamps: List[datetime] = []

    for row, status in zip(rows, statuses):
        seed_id = str(row.get("id") or "").strip()
        if status.status == "healthy":
            healthy_seed_count += 1
        elif status.status == "blocked":
            blocked_seed_count += 1
            blocked_samples.append(
                {
                    "seed_id": seed_id,
                    "title": row.get("title") or (row.get("seed_data") or {}).get("title"),
                    "domain": row.get("domain"),
                    "attached_product_key": row.get("attached_product_key"),
                    "matched_via": status.matched_via,
                    "blocker_anomaly_types": list(status.blocker_anomaly_types),
                    "last_extracted_at": status.last_extracted_at,
                }
            )
        else:
            review_seed_count += 1

        parsed_dt = _parse_timestamp(status.last_extracted_at)
        if parsed_dt:
            extracted_timestamps.append(parsed_dt)

        for issue_type in status.blocker_anomaly_types:
            key = ("blocker", issue_type)
            issue_counter[key] += 1
            issue_seed_samples.setdefault(key, [])
            if seed_id and seed_id not in issue_seed_samples[key] and len(issue_seed_samples[key]) < 5:
                issue_seed_samples[key].append(seed_id)
        for issue_type in status.review_anomaly_types:
            key = ("review", issue_type)
            issue_counter[key] += 1
            issue_seed_samples.setdefault(key, [])
            if seed_id and seed_id not in issue_seed_samples[key] and len(issue_seed_samples[key]) < 5:
                issue_seed_samples[key].append(seed_id)

    issue_buckets: List[Dict[str, Any]] = []
    for (severity, issue_type), count in sorted(
        issue_counter.items(),
        key=lambda item: (
            0 if item[0][0] == "blocker" else 1,
            -item[1],
            item[0][1],
        ),
    ):
        issue_buckets.append(
            asdict(
                ExternalReferralIssueBucket(
                    issue_type=issue_type,
                    severity=severity,
                    count=count,
                    operator_action=(
                        "Open the audit queue and remediate blocker seeds before referral runtime."
                        if severity == "blocker"
                        else "Open the audit queue and review/refine affected seed content."
                    ),
                    sample_seed_ids=list(issue_seed_samples.get((severity, issue_type), [])),
                )
            )
        )

    extracted_timestamps.sort()
    summary = AttachedFallbackSeedSummary(
        merchant_id=merchant_id,
        status=_status_from_counts(
            total_active_seeds=len(rows),
            blocked_seed_count=blocked_seed_count,
            review_seed_count=review_seed_count,
        ),
        gating_policy_version=EXTERNAL_REFERRAL_GATING_POLICY_VERSION,
        matched_domains=list(inventory["matched_domains"]),
        total_active_seeds=len(rows),
        attached_seed_count=len(inventory["attached_rows"]),
        domain_unattached_seed_count=len(inventory["domain_unattached_rows"]),
        healthy_seed_count=healthy_seed_count,
        blocked_seed_count=blocked_seed_count,
        review_seed_count=review_seed_count,
        issue_buckets=issue_buckets,
        sample_blocked_seeds=blocked_samples[:5],
        last_extracted_at_oldest=extracted_timestamps[0].isoformat() if extracted_timestamps else None,
        last_extracted_at_newest=extracted_timestamps[-1].isoformat() if extracted_timestamps else None,
    )
    return asdict(summary)


def _merchant_invalid_action_and_reasons(
    *,
    has_store_domain: bool,
    has_catalog_products: bool,
    has_psp: bool,
) -> Tuple[List[str], str]:
    reasons: List[str] = []
    if not has_store_domain:
        reasons.append("missing_store_domain")
    if not has_catalog_products:
        reasons.append("missing_catalog_sync")
    if not has_psp:
        reasons.append("missing_psp_or_checkout")

    if "missing_store_domain" in reasons:
        action = "Connect or repair merchant store metadata before treating this merchant as commerce-valid."
    elif "missing_catalog_sync" in reasons:
        action = "Sync active merchant catalog into products_cache before rollout."
    elif "missing_psp_or_checkout" in reasons:
        action = "Connect a live PSP or internal checkout path before rollout."
    else:
        action = "Merchant already satisfies the commerce-valid baseline."
    return reasons, action


def _merchant_invalid_sort_key(row: Dict[str, Any]) -> Tuple[int, int, str]:
    reasons = list(row.get("invalid_reasons") or [])
    if reasons == ["missing_psp_or_checkout"]:
        priority = 0
    elif "missing_catalog_sync" in reasons and "missing_store_domain" not in reasons:
        priority = 1
    elif "missing_store_domain" in reasons:
        priority = 2
    else:
        priority = 3
    return (
        priority,
        -int(row.get("catalog_product_count") or 0),
        str(row.get("business_name") or row.get("merchant_id") or ""),
    )


async def build_platform_fallback_program_summary() -> Dict[str, Any]:
    rows = await _fetch_all_active_referral_seed_rows()
    if not rows:
        return asdict(
            PlatformFallbackProgramSummary(
                status="red",
                generated_at=datetime.now(timezone.utc).isoformat(),
                gating_policy_version=EXTERNAL_REFERRAL_GATING_POLICY_VERSION,
                total_active_seeds=0,
                healthy_seed_count=0,
                blocked_seed_count=0,
                review_seed_count=0,
                attached_seed_count=0,
                unattached_seed_count=0,
                issue_buckets=[],
                top_domains=[],
                top_blocked_domains=[],
                runtime_surface_coverage_summary=asdict(
                    PlatformFallbackRuntimeSurfaceCoverageSummary(
                        total_surface_eligible_seeds=0,
                        total_surface_blocked_seeds=0,
                        surface_eligible_rate_pct=0.0,
                        attached_surface_eligible_seed_count=0,
                        attached_surface_blocked_seed_count=0,
                    )
                ),
            )
        )

    statuses = await asyncio.gather(
        *[
            evaluate_external_referral_seed(
                row,
                matched_via="program_summary",
            )
            for row in rows
        ]
    )

    issue_counter: Counter[Tuple[str, str]] = Counter()
    issue_seed_samples: Dict[Tuple[str, str], List[str]] = {}
    domain_counter: Counter[str] = Counter()
    blocked_domain_counter: Counter[str] = Counter()
    healthy_seed_count = 0
    blocked_seed_count = 0
    review_seed_count = 0
    attached_seed_count = 0
    unattached_seed_count = 0
    attached_surface_eligible_seed_count = 0
    attached_surface_blocked_seed_count = 0

    for row, status in zip(rows, statuses):
        seed_id = str(row.get("id") or "").strip()
        domain = normalize_referral_domain(row.get("domain"))
        if domain:
            domain_counter[domain] += 1
        is_attached = bool(str(row.get("attached_product_key") or "").strip())
        if is_attached:
            attached_seed_count += 1
        else:
            unattached_seed_count += 1

        if status.status == "healthy":
            healthy_seed_count += 1
            if is_attached:
                attached_surface_eligible_seed_count += 1
        elif status.status == "blocked":
            blocked_seed_count += 1
            if domain:
                blocked_domain_counter[domain] += 1
            if is_attached:
                attached_surface_blocked_seed_count += 1
        else:
            review_seed_count += 1
            if is_attached:
                attached_surface_eligible_seed_count += 1

        for issue_type in status.blocker_anomaly_types:
            key = ("blocker", issue_type)
            issue_counter[key] += 1
            issue_seed_samples.setdefault(key, [])
            if seed_id and seed_id not in issue_seed_samples[key] and len(issue_seed_samples[key]) < 5:
                issue_seed_samples[key].append(seed_id)
        for issue_type in status.review_anomaly_types:
            key = ("review", issue_type)
            issue_counter[key] += 1
            issue_seed_samples.setdefault(key, [])
            if seed_id and seed_id not in issue_seed_samples[key] and len(issue_seed_samples[key]) < 5:
                issue_seed_samples[key].append(seed_id)

    issue_buckets: List[Dict[str, Any]] = []
    for (severity, issue_type), count in sorted(
        issue_counter.items(),
        key=lambda item: (
            0 if item[0][0] == "blocker" else 1,
            -item[1],
            item[0][1],
        ),
    ):
        issue_buckets.append(
            asdict(
                PlatformFallbackIssueBucket(
                    issue_type=issue_type,
                    severity=severity,
                    count=count,
                    operator_action=(
                        "Open the external-seed audit queue and remediate blocker seeds before runtime surfacing."
                        if severity == "blocker"
                        else "Review or refine fallback seed content quality without treating it as a merchant-readiness failure."
                    ),
                    sample_seed_ids=list(issue_seed_samples.get((severity, issue_type), [])),
                )
            )
        )

    top_domains = [
        asdict(PlatformFallbackDomainSummary(domain=domain, count=count))
        for domain, count in sorted(domain_counter.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]
    top_blocked_domains = [
        asdict(PlatformFallbackDomainSummary(domain=domain, count=count))
        for domain, count in sorted(blocked_domain_counter.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]
    total_active_seeds = len(rows)
    total_surface_eligible_seeds = healthy_seed_count + review_seed_count

    return asdict(
        PlatformFallbackProgramSummary(
            status=_status_from_counts(
                total_active_seeds=total_active_seeds,
                blocked_seed_count=blocked_seed_count,
                review_seed_count=review_seed_count,
            ),
            generated_at=datetime.now(timezone.utc).isoformat(),
            gating_policy_version=EXTERNAL_REFERRAL_GATING_POLICY_VERSION,
            total_active_seeds=total_active_seeds,
            healthy_seed_count=healthy_seed_count,
            blocked_seed_count=blocked_seed_count,
            review_seed_count=review_seed_count,
            attached_seed_count=attached_seed_count,
            unattached_seed_count=unattached_seed_count,
            issue_buckets=issue_buckets,
            top_domains=top_domains,
            top_blocked_domains=top_blocked_domains,
            runtime_surface_coverage_summary=asdict(
                PlatformFallbackRuntimeSurfaceCoverageSummary(
                    total_surface_eligible_seeds=total_surface_eligible_seeds,
                    total_surface_blocked_seeds=blocked_seed_count,
                    surface_eligible_rate_pct=(
                        round((total_surface_eligible_seeds / total_active_seeds) * 100, 1)
                        if total_active_seeds > 0
                        else 0.0
                    ),
                    attached_surface_eligible_seed_count=attached_surface_eligible_seed_count,
                    attached_surface_blocked_seed_count=attached_surface_blocked_seed_count,
                )
            ),
        )
    )


async def build_merchant_commerce_cohort_summary() -> Dict[str, Any]:
    merchants = await get_all_merchant_onboardings(include_deleted=False)
    if not merchants:
        return asdict(
            MerchantCommerceCohortSummary(
                generated_at=datetime.now(timezone.utc).isoformat(),
                total_registered_merchants=0,
                store_connected_merchants=0,
                store_connected_with_psp_merchants=0,
                merchant_valid_count=0,
                merchant_invalid_count=0,
                top_invalid_merchants=[],
            )
        )

    product_counts_by_merchant, domains_by_merchant, psp_providers_by_merchant = await asyncio.gather(
        _fetch_catalog_product_counts_by_merchant(),
        _fetch_store_domains_by_merchant(),
        _fetch_active_psp_providers_by_merchant(),
    )

    total_registered_merchants = 0
    store_connected_merchants = 0
    store_connected_with_psp_merchants = 0
    merchant_valid_count = 0
    invalid_rows: List[Dict[str, Any]] = []

    for merchant in merchants:
        merchant_id = str(merchant.get("merchant_id") or "").strip()
        if not merchant_id:
            continue
        total_registered_merchants += 1
        store_domains = list(domains_by_merchant.get(merchant_id) or [])
        store_count = len(store_domains)
        catalog_product_count = int(product_counts_by_merchant.get(merchant_id) or 0)
        psp_providers = list(psp_providers_by_merchant.get(merchant_id) or [])
        has_store_domain = store_count > 0
        has_catalog_products = catalog_product_count > 0
        has_psp = bool(psp_providers)

        if has_store_domain:
            store_connected_merchants += 1
        if has_store_domain and has_psp:
            store_connected_with_psp_merchants += 1

        invalid_reasons, operator_action = _merchant_invalid_action_and_reasons(
            has_store_domain=has_store_domain,
            has_catalog_products=has_catalog_products,
            has_psp=has_psp,
        )
        if not invalid_reasons:
            merchant_valid_count += 1
            continue

        invalid_rows.append(
            asdict(
                MerchantCommerceCohortMerchant(
                    merchant_id=merchant_id,
                    business_name=str(merchant.get("business_name") or merchant_id),
                    store_count=store_count,
                    catalog_product_count=catalog_product_count,
                    psp_connected=has_psp,
                    psp_providers=sorted(psp_providers),
                    invalid_reasons=invalid_reasons,
                    operator_action=operator_action,
                )
            )
        )

    return asdict(
        MerchantCommerceCohortSummary(
            generated_at=datetime.now(timezone.utc).isoformat(),
            total_registered_merchants=total_registered_merchants,
            store_connected_merchants=store_connected_merchants,
            store_connected_with_psp_merchants=store_connected_with_psp_merchants,
            merchant_valid_count=merchant_valid_count,
            merchant_invalid_count=max(0, total_registered_merchants - merchant_valid_count),
            top_invalid_merchants=sorted(invalid_rows, key=_merchant_invalid_sort_key)[:8],
        )
    )


def _merchant_commerce_status(*, merchant_valid: bool, order_payment_loop_observed: bool) -> str:
    if merchant_valid and order_payment_loop_observed:
        return "green"
    if merchant_valid:
        return "yellow"
    return "red"


def _merchant_commerce_list_sort_key(row: Dict[str, Any]) -> Tuple[int, str]:
    status = str(row.get("status") or "").lower()
    if status == "red":
        priority = 0
    elif status == "yellow":
        priority = 1
    else:
        priority = 2
    return (
        priority,
        str(row.get("business_name") or row.get("merchant_id") or "").lower(),
    )


async def build_merchant_commerce_readiness_list() -> Dict[str, Any]:
    merchants = await get_all_merchant_onboardings(include_deleted=False)
    if not merchants:
        return asdict(
            MerchantCommerceReadinessListSummary(
                generated_at=datetime.now(timezone.utc).isoformat(),
                total_registered_merchants=0,
                merchant_valid_count=0,
                rollout_ready_count=0,
                attention_count=0,
                merchants=[],
            )
        )

    product_counts_by_merchant, domains_by_merchant, psp_providers_by_merchant, paid_order_evidence_by_merchant = await asyncio.gather(
        _fetch_catalog_product_counts_by_merchant(),
        _fetch_store_domains_by_merchant(),
        _fetch_active_psp_providers_by_merchant(),
        _fetch_paid_order_evidence_by_merchant(),
    )

    rows: List[Dict[str, Any]] = []
    merchant_valid_count = 0
    rollout_ready_count = 0

    for merchant in merchants:
        merchant_id = str(merchant.get("merchant_id") or "").strip()
        if not merchant_id:
            continue

        store_domains = list(domains_by_merchant.get(merchant_id) or [])
        store_count = len(store_domains)
        catalog_product_count = int(product_counts_by_merchant.get(merchant_id) or 0)
        psp_providers = list(psp_providers_by_merchant.get(merchant_id) or [])
        evidence = dict(paid_order_evidence_by_merchant.get(merchant_id) or {})
        paid_orders_last_30_days = int(evidence.get("paid_orders_last_30_days") or 0)
        all_time_paid_orders = int(evidence.get("paid_orders_all_time") or 0)

        has_store_domain = store_count > 0
        has_catalog_products = catalog_product_count > 0
        has_psp = bool(psp_providers)
        order_payment_loop_observed = all_time_paid_orders > 0
        merchant_valid = has_store_domain and has_catalog_products and has_psp
        status = _merchant_commerce_status(
            merchant_valid=merchant_valid,
            order_payment_loop_observed=order_payment_loop_observed,
        )

        invalid_reasons, operator_action = _merchant_invalid_action_and_reasons(
            has_store_domain=has_store_domain,
            has_catalog_products=has_catalog_products,
            has_psp=has_psp,
        )
        if merchant_valid and not order_payment_loop_observed:
            operator_action = "Run a real order and verify paid order evidence before rollout."

        if merchant_valid:
            merchant_valid_count += 1
        if merchant_valid and order_payment_loop_observed:
            rollout_ready_count += 1

        rows.append(
            asdict(
                MerchantCommerceReadinessListItem(
                    merchant_id=merchant_id,
                    business_name=str(merchant.get("business_name") or merchant_id),
                    status=status,
                    merchant_valid=merchant_valid,
                    rollout_ready=merchant_valid and order_payment_loop_observed,
                    store_count=store_count,
                    connected_store_domain_count=store_count,
                    catalog_product_count=catalog_product_count,
                    psp_connected=has_psp,
                    psp_provider_count=len(psp_providers),
                    psp_providers=sorted(psp_providers),
                    paid_orders_last_30_days=paid_orders_last_30_days,
                    all_time_paid_orders=all_time_paid_orders,
                    invalid_reasons=invalid_reasons,
                    operator_action=operator_action,
                )
            )
        )

    sorted_rows = sorted(rows, key=_merchant_commerce_list_sort_key)
    total_registered_merchants = len(sorted_rows)
    attention_count = max(0, total_registered_merchants - rollout_ready_count)

    return asdict(
        MerchantCommerceReadinessListSummary(
            generated_at=datetime.now(timezone.utc).isoformat(),
            total_registered_merchants=total_registered_merchants,
            merchant_valid_count=merchant_valid_count,
            rollout_ready_count=rollout_ready_count,
            attention_count=attention_count,
            merchants=sorted_rows,
        )
    )


async def build_external_referral_fleet_summary() -> Dict[str, Any]:
    """Deprecated compatibility alias for the old merchant-centric fleet summary."""
    return await build_merchant_commerce_cohort_summary()


async def should_block_external_referral_runtime(
    row: Dict[str, Any],
    *,
    matched_via: str = "runtime",
    allowed_domains: Optional[List[str]] = None,
) -> Tuple[bool, ExternalReferralStatus]:
    """Block recall only when the merchant handoff is unusable or unsafe.

    ``status`` intentionally retains every audit blocker so operators and the
    transaction path can require refresh or quote validation.  Recall is
    source-neutral: nonterminal audit findings do not hide external offers.
    """
    status = await evaluate_external_referral_seed(
        row,
        matched_via=matched_via,
        allowed_domains=allowed_domains,
    )
    runtime_blockers = sorted(
        set(status.blocker_anomaly_types) & EXTERNAL_REFERRAL_RUNTIME_BLOCKER_ANOMALIES
    )
    blocked = bool(runtime_blockers)
    if blocked:
        _record_metric("referral_runtime_filtered_total")
        logger.info(
            "external referral runtime filtered seed",
            extra={
                "seed_id": status.seed_id,
                "matched_via": matched_via,
                "blockers": runtime_blockers,
                "audit_blockers": list(status.blocker_anomaly_types),
            },
        )
    return blocked, status


# A served seed read within this window is FRESH and waits behind every stale one. Sized for the
# launch target -- every served seed re-read from its origin within 3 days: a row crosses into the
# stale tier at 48h, and a nightly run that drains the stale tier reaches it before 72h.
_FRESH_HOURS_DEFAULT = 48.0

# The candidate slice cap. The old 5000 sat far under the ~23.8k active seeds; it only bounded a
# SQL LIMIT, and the queue is now tiered in Python over every eligible row, so this is a sanity cap.
_CANDIDATE_LIMIT_CAP = 30000

# THE PER-HOST CAP on one run's candidate cut. Without it a backlog on one host fills the whole cut: when a served
# region's currency started counting as served (2026-11-05), 2,632 never-refreshed SGD seeds in the US partition, on
# 17 hosts, moved to the head of the queue (census 2026-11-06), and the next 1,300-row cut would have been all of
# them, 1,009 on ONE host, with no stale USD row at all. A host is read one request at a time, so those rows are also
# the slowest way to spend the run, and one host breaker trip would waste most of it.
#
# SIZED FROM THE RUN'S LIMIT: max(200, ceil(0.3 * limit)), 390 at the job's 1,300. The cap is per run, so a host can
# be re-read at most cap x runs-per-48h times inside the fresh window; at a flat 200 and ~10 runs a day a host past
# ~4,000 served seeds could never be fresh again (simulated: an 8,000-seed host settles at ~86 h). 30% of the cut
# still keeps a backlog on one host from taking most of a run. EXTERNAL_REFERRAL_REFRESH_MAX_ROWS_PER_HOST sets it
# outright.
#
# DEFERRED ROWS ARE NOT STAMPED: they are left out of the cut, so they keep their place and lead the next run.
# An unset, blank, malformed, non-positive or non-finite value falls back to the default, never to "no cap".
_MAX_ROWS_PER_HOST_FLOOR = 200
_MAX_ROWS_PER_HOST_SHARE = 0.3


def _default_max_rows_per_host(limit: int) -> int:
    return max(_MAX_ROWS_PER_HOST_FLOOR, math.ceil(_MAX_ROWS_PER_HOST_SHARE * max(1, int(limit))))


def _refresh_max_rows_per_host(limit: int) -> int:
    default = _default_max_rows_per_host(limit)
    raw = os.getenv("EXTERNAL_REFERRAL_REFRESH_MAX_ROWS_PER_HOST", "").strip()
    try:
        value = float(raw) if raw else float(default)
    except ValueError:
        return default
    if not math.isfinite(value) or value < 1:
        return default
    return int(value)


def _refresh_fresh_hours() -> float:
    raw = os.getenv("EXTERNAL_REFERRAL_REFRESH_FRESH_HOURS", "").strip()
    try:
        value = float(raw) if raw else _FRESH_HOURS_DEFAULT
    except ValueError:
        return _FRESH_HOURS_DEFAULT
    return value if math.isfinite(value) and value >= 0 else _FRESH_HOURS_DEFAULT


def served_region_currencies() -> FrozenSet[str]:
    """The currencies the served pricing regions price in: `serving_pricing_regions()` (the market registry's served
    markets, which PIVOTA_SERVING_PRICING_REGIONS may only narrow), the list every serving path reads, each mapped
    through region_pricing. E.g. {'USD', 'SGD'}.

    FAILS SAFE TO EMPTY, loudly: an empty list, or a registry or env that does not load, leaves the refresh tier on
    the partition's currency alone (`refresh_queue_tier` with no served currencies), never "every currency served".
    """
    try:
        from services.index_pipeline_state_service import serving_pricing_regions

        regions = list(serving_pricing_regions() or [])
    except Exception as exc:
        logger.warning("external referral refresh: no served pricing regions (%s); the queue tier uses the "
                       "partition's currency alone", exc)
        return frozenset()
    return frozenset(c for c in (pricing_currency_for_region_or_none(r) for r in regions) if c)


def refresh_queue_tier(
    *, is_fresh: bool, market: Any, price_currency: Any, served_currencies: Iterable[str] = ()
) -> Tuple[int, int]:
    """Where a candidate sits in the refresh queue. Pure; lower sorts first.

    Stale before fresh, then a row priced in a SERVED currency before one that is not. A served currency is the
    partition's own (`seed_serving_currency(market)`, Peng 2026-09-26) or, since 2026-11-05, the currency of any
    served pricing region (`served_currencies`, from `served_region_currencies()`), IN A PARTITION WHOSE OWN
    CURRENCY IS SERVED. SG rows live in the US partition on purpose, so on the partition alone every SGD seed sorted
    behind every stale USD one, and with the run's limit under the stale served tier it was never read (2,632 SGD
    seeds in the US partition, census 2026-11-06).

    The partition condition keeps the rule to the partitions the served regions read: a USD row in the JP
    partition is not served while JP is not, so it stays behind, as before.

    A row in a currency no served region prices is still not served, and a refresh cannot make it so, because
    `_refresh_external_seed_by_id` refuses a currency switch (`skipped_currency_mismatch`). It is kept rather than
    dropped. A NULL currency can still be filled by a read.
    """
    expected = seed_serving_currency(market)
    currency = str(price_currency or "").strip().upper()
    # Blank codes never enter `served`, and no market's currency is blank, so a row with no currency is never served.
    served = {str(c).strip().upper() for c in served_currencies if str(c or "").strip()}
    served_currency = expected is not None and (currency == expected or (expected in served and currency in served))
    return (1 if is_fresh else 0, 0 if served_currency else 1)


#: The refresh queue's suppressed-product filter: the seed lane's (`SEED_SUPPRESSED_PRODUCT_ANTI_JOIN`), except that a
#: product suppressed ONLY under the unconfirmed-dead reason keeps its seed in the queue. The mirror back-links each
#: seed's `attached_product_key` to its own mirror product, which is exactly what that hide suppresses; dropping the
#: seed from the queue would leave nothing to observe it again (the sweep cannot read those hosts' catalogues), so
#: the hide could never lift. The serving lanes keep the seed lane's filter. The reason is spelled out (a literal, for
#: the repo's static PREPARE sweep); a test pins it to `external_seed_destination_liveness.UNCONFIRMED_DEAD_REASON`.
REFRESH_SUPPRESSED_PRODUCT_ANTI_JOIN = """
AND (
  external_product_seeds.attached_product_key IS NULL
  OR external_product_seeds.attached_product_key NOT IN (
    SELECT seed_cp.product_key FROM catalog_products seed_cp
    WHERE seed_cp.suppressed_at IS NOT NULL AND seed_cp.product_key IS NOT NULL
      AND (seed_cp.suppression_reason IS NULL
           OR seed_cp.suppression_reason <> 'external_seed_destination_dead_unconfirmed_pending')
  )
)
"""


async def get_external_referral_refresh_candidate_seed_ids(
    limit: int = 500, *, seed_ids: Optional[Sequence[str]] = None, market: Optional[str] = None
) -> List[str]:
    """Which seeds the nightly refresh re-reads, in order: served and stale first.

    THE QUEUE USED TO SKIP UNATTACHED SEEDS. (The 2026-09-28 census found 0 served unattached seeds
    -- all 19,808 served seeds are attached -- so this is a latent fix, not a coverage one.) It took `attached_product_key IS NOT NULL` rows
    first, then UNATTACHED rows only when their domain matched a connected merchant store, and
    only when the attached rows left room under the limit. With ~14k attached rows and a 4,000-row
    limit, there was never room. The seed lanes serve unattached seeds
    (`fetch_external_seed_rows`, `only_unattached` True by default and False on the multi and
    shop lanes), so an unattached seed was served on its ingest-time price and never re-read. Now
    every active seed the seed lane can serve is a candidate.

    NOT candidates, filtered in SQL with the seed lane's own fragments:
      * a seed attached to a SUPPRESSED catalog product (`REFRESH_SUPPRESSED_PRODUCT_ANTI_JOIN`, the seed lane's
        SEED_SUPPRESSED_PRODUCT_ANTI_JOIN with one exception). Measured prod 2026-09-27: 682. Lifting the
        suppression puts it back, near the head. THE EXCEPTION: a product hidden under the unconfirmed-dead reason
        stays a candidate, because this refresh is the only lane that can lift it (the sweep cannot read those
        hosts' catalogues) and a hide nothing re-observes is not reversible;
      * a seed whose domain is QUARANTINED (`build_seed_quarantine_anti_join`), for the same reason;
      * a RETIRED seed. The destination sweep sets `status = 'inactive'`.

    ORDER: `refresh_queue_tier` (stale before fresh, a served currency -- the partition's or any served pricing
    region's -- before not), then within a
    tier `last_crawl_attempt_at` NULLS FIRST, as before. The tier uses `last_crawled_at`, the
    origin-read clock. The within-tier order uses the ATTEMPT clock, so a seed that can never be
    read still rotates behind the others instead of heading the queue forever (migration 202). The
    SQL keeps that order and a stable Python sort applies the tiers on top.

    `updated_at` stays only as the last tiebreak (see migration 202 for why it never leads).

    THE CUT takes at most `_refresh_max_rows_per_host(limit)` rows per host (`crawl_politeness.host_of` on the
    destination URL, the key the run paces and breaks on), in queue order, WITHIN EACH TIER: a row over its host's
    cap gives its slot to the next row OF ITS OWN TIER, and only when the tier has no row left under the cap do its
    deferred rows take the remaining slots, still ahead of any lower tier. So the cap spreads a tier across hosts
    and never hands a stale row's slot to a fresh one. A row with no host is never capped. An operator's targeted
    `seed_ids` list is not capped: it is at most 200 ids, named on purpose.
    """
    normalized_limit = max(1, min(int(limit or 500), _CANDIDATE_LIMIT_CAP))
    fresh_cutoff = datetime.now(timezone.utc) - timedelta(hours=_refresh_fresh_hours())
    scope_sql = ""
    scope_values: Dict[str, Any] = {"fresh_cutoff": fresh_cutoff}
    if seed_ids is not None:
        # Targeted operator handoff accepts only stored IDs, never URLs. Reapply the same
        # suppression/quarantine/status guards after planning, rather than trusting a manifest.
        ids = list(dict.fromkeys(seed_ids))
        if len(ids) > 200 or any(not isinstance(s, str) or not s or len(s) > 255 for s in ids):
            raise ValueError("invalid_targeted_refresh_seed_ids")
        if not ids:
            return []
        expected = seed_serving_currency(market)
        if not market or not expected:
            raise ValueError("targeted_refresh_requires_market")
        tokens = []
        for index, seed_id in enumerate(ids):
            name = f"refresh_seed_{index}"
            tokens.append(f":{name}")
            scope_values[name] = seed_id
        scope_sql = f"AND id IN ({', '.join(tokens)}) AND upper(trim(market)) = :refresh_market " \
                    "AND (price_currency IS NULL OR btrim(price_currency) = '' " \
                    "OR upper(trim(price_currency)) = :refresh_currency)"
        scope_values.update(refresh_market=str(market).strip().upper(), refresh_currency=expected)
    rows = await database.fetch_all(
        f"""
        SELECT id, market, price_currency, destination_url,
               CASE WHEN last_crawled_at IS NOT NULL AND last_crawled_at >= :fresh_cutoff
                    THEN 1 ELSE 0 END AS is_fresh
        FROM external_product_seeds
        WHERE status = 'active'
        {REFRESH_SUPPRESSED_PRODUCT_ANTI_JOIN}
        {build_seed_quarantine_anti_join()}
        {scope_sql}
        ORDER BY last_crawl_attempt_at ASC NULLS FIRST, last_crawled_at ASC NULLS FIRST, updated_at ASC NULLS FIRST
        """,
        scope_values,
    )
    ranked: List[Tuple[Tuple[int, int], str, str]] = []
    served_currencies = served_region_currencies()
    for row in rows or []:
        row_dict = _row_to_dict(row)
        seed_id = str(row_dict.get("id") or "").strip()
        if not seed_id:
            continue
        tier = refresh_queue_tier(
            is_fresh=bool(int(row_dict.get("is_fresh") or 0)),
            market=row_dict.get("market"),
            price_currency=row_dict.get("price_currency"),
            served_currencies=served_currencies,
        )
        ranked.append((tier, seed_id, crawl_politeness.host_of(str(row_dict.get("destination_url") or ""))))
    ranked.sort(key=lambda item: item[0])  # stable: keeps the SQL order inside a tier
    if seed_ids is not None:
        return [seed_id for _tier, seed_id, _host in ranked[:normalized_limit]]
    per_host_cap = _refresh_max_rows_per_host(normalized_limit)
    per_host: Counter = Counter()
    cut: List[str] = []
    deferred = 0
    deferred_hosts: set = set()
    for _tier, group in itertools.groupby(ranked, key=lambda item: item[0]):
        if len(cut) >= normalized_limit:
            break
        tier_deferred: List[Tuple[str, str]] = []
        for _t, seed_id, host in group:
            if len(cut) >= normalized_limit:
                break
            if host:
                if per_host[host] >= per_host_cap:
                    tier_deferred.append((seed_id, host))
                    continue
                per_host[host] += 1
            cut.append(seed_id)
        # The tier's own deferred rows, in queue order, before any lower tier's row.
        room = max(0, normalized_limit - len(cut))
        cut.extend(seed_id for seed_id, _host in tier_deferred[:room])
        left_out = tier_deferred[room:]
        deferred += len(left_out)
        deferred_hosts.update(host for _seed_id, host in left_out)
    if deferred:
        # WARNING, not INFO: nothing configures the root logger, so a plain module logger's INFO never reaches the
        # job's logs, and this line is how a catch-up is watched draining.
        logger.warning(
            "external referral refresh: %d candidate rows deferred past the %d-rows-per-host cap (on %d hosts)",
            deferred, per_host_cap, len(deferred_hosts),
        )
    return cut


# The wall-clock ceiling for one refresh run, and it is not a nicety. Every row here is a
# politeness-gated fetch of a third-party storefront, and the gate can legitimately sleep for
# minutes: `CRAWL_MAX_BACKOFF_SECONDS` alone is 300s, so `limit=500` rows against a
# persistently-429ing host is a worst case near 41 hours WITHOUT any hostile robots.txt in the
# picture. `crawl_politeness`'s `CRAWL_MAX_ROBOTS_DELAY_SECONDS` bounds what any ONE row can
# cost; only a budget bounds what the RUN costs.
#
# Default sits under the 3600s Cloud Run task timeout its sibling sweep documents
# (`services/external_seed_destination_liveness.SWEEP_HOST_CONCURRENCY`) so the run prints its
# own summary instead of being killed mid-row with nothing to show for it.
EXTERNAL_REFERRAL_REFRESH_BUDGET_SECONDS = 3300.0


def _refresh_budget_seconds(explicit: Optional[float]) -> float:
    """Resolve the run's wall-clock budget: the flag, then EXTERNAL_REFERRAL_REFRESH_BUDGET_SECONDS,
    then 3300. Always > 0.

    NON-FINITE FALLS BACK TO THE DEFAULT rather than through. `argparse type=float` accepts
    `nan` and `inf`, and either would (a) silently disable the budget, since `budget > 0` is
    False for NaN, while the summary still reported `stopped_early: false`, and (b) reach
    `json.dumps` in `jobs/external_referral_refresh.main`, which emits a bare `NaN` that is
    not valid JSON for any downstream parser.

    AND SO DOES 0 (2026-10-17 review, M3). `<= 0` used to disable the budget, which also left the
    global pacer with no deadline: a cool-down could then be waited out into the 3,600 s task
    timeout. It is an ERROR and the default now (`crawl_global_pacer.resolve_budget_seconds`); a run
    that needs longer passes a longer budget.
    """
    return crawl_global_pacer.resolve_budget_seconds(
        explicit, "EXTERNAL_REFERRAL_REFRESH_BUDGET_SECONDS", EXTERNAL_REFERRAL_REFRESH_BUDGET_SECONDS
    )


async def _fetch_refresh_candidate_hosts(seed_ids: Sequence[str]) -> Dict[str, str]:
    """seed id -> the host `crawl_politeness` paces its fetch on.

    `host_of(destination_url)`, NOT the seed's `domain` column: the pacing state is keyed by the
    host of the URL actually requested (`_fetch_html` -> `note_response(url, ...)`), and the two
    disagree whenever `domain` drops a `www.`. A breaker keyed on the other spelling would never
    see the streak it is meant to act on.

    FAILS OPEN TO "NO BREAKER", loudly. A lookup error must not stop the refresh itself; without
    hosts the run behaves exactly as it did before the breaker existed.
    """
    hosts: Dict[str, str] = {}
    ids = [str(s) for s in seed_ids if s]
    try:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            params = {f"id{i}": seed_id for i, seed_id in enumerate(chunk)}
            placeholders = ", ".join(f":id{i}" for i in range(len(chunk)))
            rows = await database.fetch_all(
                f"SELECT id, destination_url FROM external_product_seeds WHERE id IN ({placeholders})",
                params,
            )
            for row in rows or []:
                row_dict = _row_to_dict(row)
                host = crawl_politeness.host_of(str(row_dict.get("destination_url") or ""))
                if host:
                    hosts[str(row_dict.get("id"))] = host
    except Exception as exc:  # noqa: BLE001 - the breaker is an optimisation, the refresh is not
        logger.warning(
            "external referral refresh: host lookup failed (%s); the per-host 429 breaker is "
            "OFF for this run", str(exc)[:200],
        )
        return {}
    return hosts


_CATALOGUE_PLAN_SQL = """
SELECT id, destination_url, last_crawled_at, last_page_read_at, destination_verdict, destination_failure_streak
FROM external_product_seeds WHERE id IN ({placeholders})
"""

# Resolved the way the refresh's own statements resolve the table: the first schema on the search_path.
_PAGE_READ_COLUMN_SQL = """
SELECT 1 FROM information_schema.columns
WHERE table_schema = (current_schemas(false))[1] AND table_name = 'external_product_seeds'
  AND column_name = :column
"""


async def _page_read_column_ready() -> bool:
    """Does `external_product_seeds.last_page_read_at` exist (migration 274, applied by hand)?

    The lane cannot run without it: the column is where a catalogue read carries the last page read
    forward. Fails closed to "no catalogue lane"."""
    try:
        return bool(await database.fetch_one(_PAGE_READ_COLUMN_SQL, {"column": catalogue_lane.PAGE_READ_COLUMN}))
    except Exception as exc:  # noqa: BLE001 - the lane is an optimisation, the refresh is not
        logger.warning("external referral refresh: page-read column check failed (%s)", str(exc)[:200])
        return False


async def _fetch_refresh_candidate_dests(seed_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """seed id -> what the catalogue price lane plans from: `destination_url` (the URL the refresh reads)
    and the fields `external_seed_catalogue_refresh.page_read_due` reads.

    Fails open to "no catalogue lane": a lookup error leaves every row on its page read."""
    dests: Dict[str, Dict[str, Any]] = {}
    ids = [str(s) for s in seed_ids if s]
    try:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            params = {f"id{i}": seed_id for i, seed_id in enumerate(chunk)}
            placeholders = ", ".join(f":id{i}" for i in range(len(chunk)))
            rows = await database.fetch_all(_CATALOGUE_PLAN_SQL.format(placeholders=placeholders), params)
            for row in rows or []:
                row_dict = _row_to_dict(row)
                if row_dict.get("destination_url"):
                    dests[str(row_dict.get("id"))] = row_dict
    except Exception as exc:  # noqa: BLE001 - the lane is an optimisation, the refresh is not
        logger.warning(
            "external referral refresh: destination lookup failed (%s); the catalogue lane is OFF "
            "for this run", str(exc)[:200],
        )
        return {}
    return dests


async def _run_workers(worker: Callable[[], Awaitable[None]], concurrency: int) -> None:
    if concurrency <= 1:
        # Inline, in this task's own context: exactly the serial loop this replaced.
        await worker()
        return
    # A FRESH CONTEXT PER WORKER. `databases` 0.7.0 parks its Connection in a ContextVar and
    # this task has already queried, so a plain child task would share that one Connection
    # and silently join a sibling's open transaction (reference: services/scheduler_job_runner
    # `spawn_isolated`). Isolated, each worker checks its own connection out of the pool,
    # whose size (DB_POOL_MAX_SIZE, 2 on the job) caps the DB concurrency whatever this is.
    from services.scheduler_job_runner import spawn_isolated

    workers = [
        spawn_isolated(worker(), name=f"external-referral-refresh-worker-{index}")
        for index in range(concurrency)
    ]
    await asyncio.gather(*workers)


async def run_external_referral_refresh_batch(
    *,
    refresh_seed_by_id: Callable[[str], Awaitable[Dict[str, Any]]],
    limit: int = 500,
    budget_seconds: Optional[float] = None,
    host_concurrency: Optional[int] = None,
    ip_throttle_trip_hosts: Optional[int] = None,
    ip_throttle_window_seconds: Optional[float] = None,
    ip_throttle_enabled: Optional[bool] = None,
    candidate_seed_ids: Optional[Sequence[str]] = None,
    market: Optional[str] = None,
    global_rps: Optional[float] = None,
    max_new_hosts_per_minute: Optional[float] = None,
    ip_challenge_trip_hosts: Optional[int] = None,
    ip_challenge_window_seconds: Optional[float] = None,
    ip_throttle_cooldown_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    candidate_seed_ids = await get_external_referral_refresh_candidate_seed_ids(
        limit=limit, **({"seed_ids": candidate_seed_ids, "market": market} if candidate_seed_ids is not None else {})
    )
    # THE IP BREAKER, beside the per-host one and for a different failure. The per-host breaker
    # sees one host's streak; on 09-30 Shopify's shared edge throttled our one egress IP across
    # 331 hosts at once, and the per-host breaker tripped 261 times, one host at a time, while the
    # run kept asking. It trips on DISTINCT hosts throttling inside a window, whatever edge they
    # sit behind. See `services.crawl_ip_throttle`.
    #
    # STAGED SINCE 2026-10-16 (`crawl_ip_throttle.StagedIpThrottleBreaker`): it also trips on 3
    # distinct hosts answering a Cloudflare challenge within 120 s, and its FIRST trip pauses every
    # origin read for a cool-down (15 min) and resumes at half the global rate; only the second ends
    # the run. Wired to the run's global pacer through `on_trip`.
    budget = _refresh_budget_seconds(budget_seconds)
    # THE GLOBAL PACER (`services.crawl_global_pacer`): every origin request this run makes, page
    # fetches, retries and robots.txt, starts no sooner than its slot in one shared bucket. Its
    # deadline is the budget, so a cool-down that would outlast the run stops it instead.
    #
    # THE KILL SWITCH (CRAWL_GLOBAL_PACER_DISABLED=true) builds an UNPACED pacer rather than none:
    # no rate and no new-host cap, but the breaker's cool-down and stop and the budget's deadline
    # still reach every request.
    pacer_deadline = crawl_global_pacer._now() + budget
    if _refresh_global_pacer_disabled():
        pacer = crawl_global_pacer.GlobalOriginPacer(unpaced=True, deadline=pacer_deadline)
    else:
        pacer = crawl_global_pacer.GlobalOriginPacer(
            rps=_refresh_global_rps(global_rps),
            max_new_hosts_per_minute=_refresh_max_new_hosts_per_minute(max_new_hosts_per_minute),
            deadline=pacer_deadline,
        )

    def _on_trip(breaker: crawl_ip_throttle.StagedIpThrottleBreaker, event: Dict[str, Any]) -> None:
        if event.get("action") == "cooldown":
            pacer.pause(breaker.cooldown_seconds)
            pacer.set_rate(pacer.rps / 2.0)
        else:
            pacer.stop("ip breaker tripped")

    ip_breaker = crawl_ip_throttle.StagedIpThrottleBreaker.from_env(
        trip_hosts=ip_throttle_trip_hosts,
        window_seconds=ip_throttle_window_seconds,
        enabled=ip_throttle_enabled,
        challenge_trip_hosts=ip_challenge_trip_hosts,
        challenge_window_seconds=ip_challenge_window_seconds,
        cooldown_seconds=ip_throttle_cooldown_seconds,
        on_trip=_on_trip,
    )
    ip_throttle_skips: Dict[str, int] = {}
    skipped_for_ip_throttle = 0
    host_trip = _host_block_trip()
    unreachable_trip = _host_unreachable_trip()
    challenge_trip = _host_challenge_trip()
    locked_trip = _host_locked_trip()
    concurrency = _refresh_host_concurrency(host_concurrency)
    # Looked up whatever the breakers say: the host is also the concurrency key (one row in
    # flight per host) and the per-host timing key. Without hosts every row is its own lane.
    candidate_hosts = await _fetch_refresh_candidate_hosts(candidate_seed_ids)
    # THE CATALOGUE PRICE LANE (`services.external_seed_catalogue_refresh`, default OFF). A host with
    # at least `catalogue_min_seeds()` candidates whose URL its own catalogue prices has its
    # `/products.json` read ONCE, by the worker that takes its lane (so still one request in flight per
    # host, every request on the global pacer), and each of its rows is then refreshed from memory.
    # Every row the catalogue does not price keeps its page read, unchanged.
    catalogue_on = catalogue_lane.catalogue_refresh_enabled()
    # Switched on without migration 274, the lane stays off for the run and says so; with it, every
    # refresh of the run (page reads too) keeps `last_page_read_at`.
    catalogue_column_missing = catalogue_on and not await _page_read_column_ready()
    if catalogue_column_missing:
        logger.warning(
            "external referral refresh: %s is on but external_product_seeds.%s is missing "
            "(migration 274); the catalogue lane is OFF for this run",
            catalogue_lane.FLAG, catalogue_lane.PAGE_READ_COLUMN,
        )
        catalogue_on = False
    refresh_kwargs: Dict[str, Any] = {"page_read_column": True} if catalogue_on else {}
    catalogue_min = catalogue_lane.catalogue_min_seeds()
    catalogue_wanted: Dict[str, set] = {}
    candidate_dests: Dict[str, Dict[str, Any]] = {}
    # Seeds whose PAGE read is due (`page_read_due`): never asked of the catalogue, so they cost it no page.
    catalogue_page_due: Dict[str, str] = {}
    if catalogue_on and candidate_hosts:
        candidate_dests = await _fetch_refresh_candidate_dests(candidate_seed_ids)
        plan_now, page_max_age = datetime.now(timezone.utc), catalogue_lane.page_max_age_days()
        for seed_id, host in candidate_hosts.items():
            info = candidate_dests.get(seed_id) or {}
            handle = catalogue_lane.catalogue_handle(info.get("destination_url"))
            if not handle:
                continue
            due = catalogue_lane.page_read_due(info, now=plan_now, max_age_days=page_max_age)
            if due:
                catalogue_page_due[seed_id] = due
                continue
            catalogue_wanted.setdefault(host, set()).add(handle)
        catalogue_wanted = {h: w for h, w in catalogue_wanted.items() if len(w) >= catalogue_min}
    host_catalogues: Dict[str, Any] = {}
    catalogue_reasons: Dict[str, int] = {}
    catalogue_fallbacks: Dict[str, int] = {}
    catalogue_rows_read = 0
    # Hosts this run has stopped asking, and how many of their rows it passed over. Those rows
    # are NOT stamped (`refresh_seed_by_id` is never called), so they keep their place at the
    # head of tomorrow's queue instead of being recorded as an attempt that never happened.
    tripped_hosts: Dict[str, int] = {}
    skipped_for_host_backoff = 0
    started = time.monotonic()
    skipped_for_budget = 0
    degraded_reasons: Dict[str, int] = {}
    unprocessable = 0
    unprocessable_reasons: Dict[str, int] = {}
    proj_attempted = 0
    proj_written = 0
    proj_errored = 0
    proj_seconds = 0.0
    proj_skips: Dict[str, int] = {}
    proj_writes: Dict[str, int] = {}
    proj_offer_skips: Dict[str, int] = {}
    pdp_refreshed = 0
    pdp_errored = 0
    pdp_skips: Dict[str, int] = {}
    degraded_hosts: Dict[str, int] = {}
    stopped_early = False
    # A "success" that never contacted the origin. `resolve_external_offer` honours
    # `raise_on_unavailable` only for `ExternalOfferUnavailable`; a timeout, TLS error,
    # `RobotsDisallowed`, or a `CrawlDelayTooLong` skip returns the CACHED snapshot instead,
    # and this loop counts that row as `refreshed`. Without this counter a run in which a
    # hostile robots.txt voided every row is indistinguishable from a complete one — the same
    # failure mode `stopped_early` exists to prevent.
    refreshed_from_cache = 0
    refreshed = 0
    degraded = 0
    failed = 0
    errors: List[Dict[str, Any]] = []
    # Drift accounting. "refreshed" only says we re-fetched; it says nothing about
    # whether anything was WRONG, which is the number that justifies running this
    # on a schedule at all. `price_changed` is the measured staleness rate of the
    # index, per run. `price_skipped_incomplete_pair` is the honest residue: a
    # fetch that produced an amount without a currency, deliberately not applied
    # (see the pairing note in routes/employee_products._refresh_external_seed_by_id)
    # — if it grows, the extractor needs work, not the writer.
    price_changed = 0
    price_filled = 0
    price_unchanged = 0
    price_unavailable = 0
    price_skipped_incomplete_pair = 0
    # A scraped currency that disagrees with the stored one is refused rather than
    # applied (see routes/employee_products._refresh_external_seed_by_id). This counter
    # is the visible cost of that refusal: if it is non-trivial, the currency EXTRACTOR
    # needs work, not the writer. (Its main source, `resolve_external_offer` defaulting an
    # unread currency by market, is gone: that now lands in `price_skipped_unreadable`.)
    price_skipped_currency_mismatch = 0
    # A fetch that produced a non-positive amount. Distinct from `unavailable` (no
    # reading at all) because it means the extractor read something and got a broken
    # offer shape — a different problem with a different fix.
    price_skipped_non_positive = 0
    # The page carried a price the extractor would not read (utils/crawled_price.py refuses an
    # ambiguous separator rather than guessing; an unread currency is no longer invented). By
    # reason, so a rising count points at the signal the extractor is missing.
    price_skipped_unreadable = 0
    # A success that did not read the product we serve (routes/employee_products._read_the_served_product:
    # a canonical or redirect naming another product, a 200 challenge, a cached fallback, a URL with no
    # product handle). Its price and stock are not written; counted so a host that never reads is visible.
    price_not_read = 0
    # Refused by the ambiguous-price guard (`on`): another product's price, kept apart from `not_read`.
    price_skipped_ambiguous = 0
    availability_not_read = 0
    # What the handle-less read rule decided, per row (EXTERNAL_SEED_HANDLELESS_READ shadow / on): totals and per host.
    handleless_totals: Dict[str, int] = {}
    handleless_hosts: Dict[str, Dict[str, int]] = {}
    # The unconfirmed-dead hide (EXTERNAL_SEED_DEAD_UNCONFIRMED_HIDE): each host's observations this run and its
    # candidates, applied (or, in shadow, reported) after the run by `apply_unconfirmed_hides`.
    unconfirmed_mode = unconfirmed_hide_mode()
    # The ambiguous-price guard (EXTERNAL_SEED_AMBIGUOUS_PRICE_GUARD): per host, in total, and its bounded log lines.
    ambiguous_mode = ambiguous_price_guard_mode()
    ambiguous_totals: Dict[str, int] = {}
    ambiguous_hosts: Dict[str, Dict[str, int]] = {}
    ambiguous_lines: Dict[str, int] = {}
    unconfirmed_hosts: Dict[str, Dict[str, Any]] = {}
    price_unreadable_reasons: Dict[str, int] = {}
    price_unreadable_hosts: Dict[str, int] = {}
    availability_changed = 0
    # Hosts the run stopped asking because they did not answer at all (connection refused or
    # reset, timeout, DNS, TLS), with how many of their rows it passed over. Kept apart from the
    # 429 breaker because the cause and the fix differ.
    unreachable_hosts: Dict[str, int] = {}
    unreachable_streak: Dict[str, int] = {}
    # One error per no-answer host, URLs stripped: enough to tell a connection failure from a DNS or
    # TLS one without reading per-row logs. "All connection attempts failed" on many hosts at once
    # is our NAT (see _HOST_UNREACHABLE_TRIP_DEFAULT), not the hosts: every top no-answer host of
    # 09-26/27 answered a plain HEAD from outside the crawl egress.
    unreachable_samples: Dict[str, str] = {}
    skipped_for_unreachable_host = 0
    # Hosts that answered `challenge_trip` Cloudflare challenges in a row, and the rows passed over.
    challenged_hosts: Dict[str, int] = {}
    skipped_for_host_challenge = 0
    # Hosts that answered `locked_trip` HTTP 423s in a row, the rows passed over, and their ids: these
    # skips ARE stamped after the run (see _HOST_LOCKED_TRIP_DEFAULT). Per run, like every breaker here.
    locked_hosts: Dict[str, int] = {}
    locked_streak: Dict[str, int] = {}
    locked_skipped_ids: List[str] = []
    skipped_for_host_423 = 0
    # Per-host cost, measured around the refresh call: the fetch, the pacing wait it includes,
    # the writes and the projection. This is how to see which hosts the budget goes to.
    host_stats: Dict[str, Dict[str, float]] = {}
    rows_started = 0
    # Rows IN FLIGHT when the pacer stopped (review M1): their request was refused, not sent, so
    # they are deferred unstamped like the rows still queued -- to `skipped_for_ip_throttle` on the
    # breaker's final trip, to `skipped_for_budget` when the budget ended.
    pace_stopped_for_budget = 0

    async def _process(seed_id: str, host: str) -> None:
        nonlocal refreshed, refreshed_from_cache, degraded, failed, unprocessable
        nonlocal skipped_for_ip_throttle, pace_stopped_for_budget, stopped_early
        nonlocal price_changed, price_filled, price_unchanged, price_unavailable
        nonlocal price_skipped_incomplete_pair, price_skipped_currency_mismatch
        nonlocal price_skipped_non_positive, price_skipped_unreadable, availability_changed
        nonlocal price_not_read, availability_not_read, price_skipped_ambiguous
        nonlocal proj_attempted, proj_written, proj_errored, proj_seconds
        nonlocal pdp_refreshed, pdp_errored, catalogue_rows_read
        outcome = "failed"
        outcome_bucket = ""
        row_started = time.monotonic()
        try:
            catalogue_read = None
            if seed_id in catalogue_page_due:
                _bump(catalogue_reasons, "page_read_due:" + catalogue_page_due[seed_id])
            elif host in catalogue_wanted:
                if host not in host_catalogues:
                    host_catalogues[host] = await catalogue_lane.read_host_catalogue(host, catalogue_wanted[host])
                catalogue_read, why = catalogue_lane.seed_catalogue_read(
                    host_catalogues[host], (candidate_dests.get(seed_id) or {}).get("destination_url")
                )
                _bump(catalogue_reasons, why)
            if catalogue_read is not None:
                result = await refresh_seed_by_id(seed_id, catalogue_read=catalogue_read, **refresh_kwargs)
            else:
                result = await refresh_seed_by_id(seed_id, **refresh_kwargs)
            status = str(result.get("status") or "success")
            outcome = status
            if unconfirmed_mode != "off":
                # Every outcome: a 404 comes back `degraded`, a read `success`.
                count_unconfirmed_observation(unconfirmed_hosts, host, seed_id, result.get("destination_refresh"))
            if ambiguous_mode != "off":
                count_ambiguous_price(
                    ambiguous_totals, ambiguous_hosts, ambiguous_lines, seed_id, host, result.get("ambiguous_price")
                )
            # Counted whatever the row's outcome: a seed sent back to its page read can still end degraded.
            if catalogue_read is not None and result.get("catalogue_fallback"):
                _bump(catalogue_fallbacks, str(result["catalogue_fallback"]))
            if status == "success":
                refreshed += 1
                if result.get("read_source") == catalogue_lane.READ_SOURCE:
                    catalogue_rows_read += 1
                if result.get("snapshot_from_cache"):
                    refreshed_from_cache += 1
                # isinstance, not truthiness: a truthy non-dict here used to raise
                # INSIDE the try and land the same seed in BOTH `refreshed` and
                # `failed`, which is a worse outcome than an uncounted drift report.
                price = result.get("price_refresh")
                price_status = str(price.get("status") or "") if isinstance(price, dict) else ""
                if price_status == "applied":
                    price_changed += 1
                elif price_status == "filled":
                    price_filled += 1
                elif price_status == "unchanged":
                    price_unchanged += 1
                elif price_status == "skipped_incomplete_pair":
                    price_skipped_incomplete_pair += 1
                elif price_status == "skipped_currency_mismatch":
                    price_skipped_currency_mismatch += 1
                elif price_status == "skipped_non_positive":
                    price_skipped_non_positive += 1
                elif price_status == "skipped_unreadable":
                    price_skipped_unreadable += 1
                    reason = str(price.get("reason") or "unknown")
                    price_unreadable_reasons[reason] = price_unreadable_reasons.get(reason, 0) + 1
                    price_unreadable_hosts[host or "unknown"] = price_unreadable_hosts.get(host or "unknown", 0) + 1
                elif price_status == "unavailable":
                    price_unavailable += 1
                elif price_status == "not_read":
                    price_not_read += 1
                elif price_status == "skipped_ambiguous_product":
                    price_skipped_ambiguous += 1
                proj = result.get("projection")
                if isinstance(proj, dict):
                    proj_attempted += int(proj.get("attempted") or 0)
                    proj_written += int(proj.get("projected") or 0)
                    proj_errored += int(proj.get("errored") or 0)
                    proj_seconds += float(proj.get("seconds") or 0.0)
                    pdp_refreshed += int(proj.get("pdp_refreshed") or 0)
                    pdp_errored += int(proj.get("pdp_errored") or 0)
                    for _k, _v in proj.items():
                        if _k.startswith("pdp_skip_"):
                            _bump(pdp_skips, _k[len("pdp_skip_"):])
                        elif _k.startswith("skip_"):
                            _bump(proj_skips, _k[5:])
                        elif _k.startswith("wrote_"):
                            _bump(proj_writes, _k[len("wrote_"):])
                        elif _k.startswith("offer_skip_"):
                            _key = _k[len("offer_skip_"):]
                            proj_offer_skips[_key] = proj_offer_skips.get(_key, 0) + int(_v or 0)
                availability = result.get("availability_refresh")
                if isinstance(availability, dict) and availability.get("status") == "applied":
                    availability_changed += 1
                elif isinstance(availability, dict) and availability.get("status") == "not_read":
                    availability_not_read += 1
                count_handleless_read(handleless_totals, handleless_hosts, host, result.get("handleless_read"))
            elif status == "degraded":
                degraded += 1
                # THE REASON USED TO BE DROPPED HERE. `errors[]` is populated only on the
                # `failed` branch below, and `failed` is structurally always 0 for a degraded
                # read — so a night with 1,353 degraded rows reported a bare count and no way
                # to tell a bot-challenge from a TLS error from a 404. Bucketed rather than
                # raw so the histogram stays small enough to read in a log line.
                outcome_bucket = _degraded_reason_bucket(result.get("error"))
                _bump(degraded_reasons, outcome_bucket)
                _bump(degraded_hosts, str(result.get("domain") or "unknown"))
            else:
                failed += 1
                errors.append({"seed_id": seed_id, "status": status, "error": result.get("error")})
        except crawl_global_pacer.GlobalPaceStopped:
            # REFUSED, NOT TRIED (review M1, 2026-10-17). `resolve_external_offer` and
            # `_refresh_external_seed_by_id` re-raise this so the row is never stamped; here it is
            # counted with the rows the stop deferred and touches nothing else: not `failed`, not
            # the host's stats, not its unreachable streak.
            if ip_breaker.tripped:
                skipped_for_ip_throttle += 1
                ip_throttle_skips[host or seed_id] = ip_throttle_skips.get(host or seed_id, 0) + 1
            else:
                pace_stopped_for_budget += 1
                stopped_early = True
            return
        except HTTPException as exc:
            # NARROW ON PURPOSE. Only the two permanent per-seed data conditions are absorbed:
            # SEED_NOT_FOUND (404) and INVALID_URL (400). ~628 seeds carry no usable destination
            # today, so counting those as `failed` would redden the exit code every night over a
            # backlog the run cannot fix. Anything else — a 5xx from a helper, an auth failure —
            # is a real fault and must stay in `failed`/`errors[]` where it can move the exit
            # code; a broad `except HTTPException` would silently reclassify those as permanent.
            detail = str(getattr(exc, "detail", "") or "").strip()
            if getattr(exc, "status_code", None) in (400, 404) and detail in _UNPROCESSABLE_SEED_DETAILS:
                outcome = "unprocessable"
                unprocessable += 1
                _bump(unprocessable_reasons, detail[:40])
            else:
                failed += 1
                errors.append(
                    {"seed_id": seed_id, "status": "failed", "error": f"http {getattr(exc, 'status_code', '?')}: {detail[:200]}"}
                )
        except Exception as exc:
            failed += 1
            errors.append({"seed_id": seed_id, "status": "failed", "error": str(exc)[:300]})
        # AFTER the row, from the pacing layer's own counter rather than from this row's result:
        # a streak is only visible there (the result says "degraded: http 429", not how many in
        # a row), and it also sees 503s and any 429 a projection fetch earned on the same host.
        # The breaker changes WHETHER we ask the host again this run, never how fast.
        if host and host not in tripped_hosts and host_backoff_tripped(
            crawl_politeness.consecutive_blocks(host), host_trip
        ):
            tripped_hosts[host] = 0
            logger.warning(
                "external referral refresh: %s answered %d 429/503s in a row; skipping its "
                "remaining rows this run", host, crawl_politeness.consecutive_blocks(host),
            )
        # A CHALLENGE STREAK, at any status: stop asking. The same place and the same reasoning as
        # the 429 breaker above: the streak lives in the pacing layer (`note_response`, which sees
        # every challenged answer this row earned, robots.txt included), and it changes WHETHER the
        # host is asked again, never how fast. An excluded host (CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS)
        # is skipped here like any other: exclusion keeps it out of the IP rule, not in the queue.
        if (
            host
            and host not in tripped_hosts
            and crawl_politeness.consecutive_challenges(host) >= challenge_trip
        ):
            challenged_hosts[host] = 0
            logger.warning(
                "external referral refresh: %s answered %d Cloudflare challenges in a row; skipping "
                "its remaining rows this run", host, crawl_politeness.consecutive_challenges(host),
            )
        if not host:
            return
        stats = host_stats.setdefault(
            host, {"rows": 0, "origin_reads": 0, "degraded": 0, "seconds": 0.0}
        )
        stats["rows"] += 1
        stats["seconds"] += max(0.0, time.monotonic() - row_started)
        if outcome == "success" and not (isinstance(result, dict) and result.get("snapshot_from_cache")):
            stats["origin_reads"] += 1
        elif outcome == "degraded":
            stats["degraded"] += 1
        # NO ANSWER AT ALL, row after row: stop asking for the rest of the run. Any answer --
        # a read, a 404, a 429 -- proves the host is reachable and breaks the streak. An
        # exception or an unprocessable seed says nothing about the host and leaves it alone.
        if outcome == "degraded" and outcome_bucket in _UNREACHABLE_REASON_BUCKETS:
            unreachable_streak[host] = unreachable_streak.get(host, 0) + 1
            if host not in unreachable_samples and len(unreachable_samples) < 20:
                unreachable_samples[host] = re.sub(
                    r"https?://\S+", "<url>", str(result.get("error") or "")
                )[:160]
        elif outcome in ("success", "degraded"):
            unreachable_streak[host] = 0
        if (
            host not in unreachable_hosts
            and host not in tripped_hosts
            and host_backoff_tripped(unreachable_streak.get(host, 0), unreachable_trip)
        ):
            unreachable_hosts[host] = 0
            logger.warning(
                "external referral refresh: %s gave no answer %d rows in a row (%s); skipping "
                "its remaining rows this run", host, unreachable_streak[host], outcome_bucket,
            )
        # 423 ("Locked"), row after row: stop asking for the rest of the run. Only a REAL ANSWER breaks
        # the streak: a read that reached the origin, or another HTTP status (a challenge included).
        # A timeout, a connection/DNS/TLS failure, a pacer or robots refusal, a cached snapshot, an
        # exception or an unprocessable seed says nothing about the lock and leaves the streak alone;
        # otherwise a host alternating 423 and timeouts would trip neither this breaker nor the
        # no-answer one (each resets the other's streak).
        if outcome == "degraded" and outcome_bucket == "http_423":
            locked_streak[host] = locked_streak.get(host, 0) + 1
        elif (
            outcome == "success" and not (isinstance(result, dict) and result.get("snapshot_from_cache"))
        ) or (
            outcome == "degraded" and (outcome_bucket.startswith("http_") or outcome_bucket == "bot_challenge")
        ):
            locked_streak[host] = 0
        if (
            host not in locked_hosts
            and host not in tripped_hosts
            and host not in unreachable_hosts
            and host not in challenged_hosts
            and locked_streak.get(host, 0) >= locked_trip
        ):
            locked_hosts[host] = 0
            logger.warning(
                "external referral refresh: %s answered %d HTTP 423s in a row; skipping its "
                "remaining rows this run", host, locked_streak[host],
            )

    # THE SCHEDULER. One lane per host, holding that host's rows in queue order. A worker takes
    # the lane whose next row is EARLIEST in the queue among lanes with nothing in flight, so:
    #   * no host ever has two requests in flight. `crawl_politeness` still paces every request
    #     on its own (it reserves the slot before sleeping, which is safe across tasks), and the
    #     breakers see one row at a time per host, exactly as in the serial loop;
    #   * with one worker this IS the serial loop: nothing is in flight at take time, so the
    #     earliest head is simply the next row in the queue.
    # What concurrency buys is the idle time. On a serial loop, every second one host spends
    # waiting out its interval, its Crawl-delay, a slow TLS handshake or a timeout is a second
    # no other host is read. The sibling destination sweep already reads 4 hosts at once from the
    # same crawl egress, daily at 03:15Z (`SWEEP_HOST_CONCURRENCY`).
    #
    # TWO READY HEAPS SINCE THE NEW-HOST CAP (2026-10-16). `ready_new` holds lanes on hosts this
    # run has not contacted; `ready_known` holds the rest (and host-less rows, which have no pacing
    # key). The earliest head of the two is taken, exactly the one-heap rule, EXCEPT when it is a
    # new host and the pacer's new-hosts-per-minute cap has no room right now: then the earliest
    # known lane is read instead of parking a worker on the cap. With the cap off (or room under
    # it) this is the old rule. The admission is reserved at take time, with no await before it.
    queue_position = {seed_id: index for index, seed_id in enumerate(candidate_seed_ids)}
    lanes: Dict[str, Deque[str]] = {}
    for seed_id in candidate_seed_ids:
        lane_host = candidate_hosts.get(seed_id, "")
        # A row with no host has no pacing key, so it is its own lane.
        lanes.setdefault(lane_host or f"\x00{seed_id}", deque()).append(seed_id)
    ready_new: List[Tuple[int, str]] = []
    ready_known: List[Tuple[int, str]] = []
    for key, lane in lanes.items():
        (ready_known if key.startswith("\x00") else ready_new).append((queue_position[lane[0]], key))
    heapq.heapify(ready_new)
    heapq.heapify(ready_known)
    in_flight = 0
    lane_freed = asyncio.Condition()

    def _take() -> str:
        if ready_new and (not ready_known or ready_new[0] < ready_known[0]):
            if not ready_known or pacer.new_host_wait() <= 0:
                key = heapq.heappop(ready_new)[1]
                pacer.admit(key)
                return key
        if ready_known:
            return heapq.heappop(ready_known)[1]
        key = heapq.heappop(ready_new)[1]
        pacer.admit(key)
        return key

    def _drain_all_ip_throttled() -> None:
        # THE FINAL TRIP ENDS THE RUN: every lane not in flight is deferred, unstamped. A lane in
        # flight is drained when its row returns (the `finally` below).
        for heap in (ready_new, ready_known):
            while heap:
                _drain_ip_throttled_lane(heapq.heappop(heap)[1])

    def _drain_ip_throttled_lane(key: str) -> None:
        nonlocal skipped_for_ip_throttle
        # Unstamped, exactly like the per-host breaker's skips: `refresh_seed_by_id` is never
        # called for these rows, so `last_crawl_attempt_at` is untouched and they lead tomorrow.
        remaining = len(lanes.pop(key, ()))
        ip_throttle_skips[key] = ip_throttle_skips.get(key, 0) + remaining
        skipped_for_ip_throttle += remaining

    def _drain_tripped_lane(key: str) -> None:
        nonlocal skipped_for_host_backoff, skipped_for_unreachable_host, skipped_for_host_challenge
        nonlocal skipped_for_host_423
        # Passed-over rows are NOT stamped (`refresh_seed_by_id` is never called), so they keep
        # their place at the head of tomorrow's queue instead of being recorded as an attempt
        # that never happened. The 423 breaker's are the exception: stamped after the run.
        lane = lanes.pop(key, ())
        remaining = len(lane)
        if key in locked_hosts:
            locked_hosts[key] += remaining
            skipped_for_host_423 += remaining
            locked_skipped_ids.extend(lane)
        elif key in tripped_hosts:
            tripped_hosts[key] += remaining
            skipped_for_host_backoff += remaining
        elif key in challenged_hosts:
            challenged_hosts[key] += remaining
            skipped_for_host_challenge += remaining
        else:
            unreachable_hosts[key] += remaining
            skipped_for_unreachable_host += remaining

    async def _worker() -> None:
        nonlocal in_flight, stopped_early, rows_started
        while True:
            async with lane_freed:
                while not (ready_new or ready_known) and in_flight:
                    await lane_freed.wait()
                if ip_breaker.tripped:
                    _drain_all_ip_throttled()
                    lane_freed.notify_all()
                    return
                # CHECKED BEFORE THE ROW, and after any wait: the point is to stop STARTING work,
                # and a check after the call would still pay one more full row past the budget.
                # It cannot interrupt rows already in flight, so the true ceiling is `budget`
                # plus the slowest in-flight row -- bounded, since `crawl_politeness` caps what
                # one row can cost. A row that could only start after a cool-down that outlasts
                # the budget is not started either, nor one after the pacer has stopped on the
                # budget (its next slot fell past the deadline, review L1): that row's request
                # would only be refused.
                if (
                    (time.monotonic() - started) >= budget
                    or (pacer.deadline is not None and pacer.paused_until >= pacer.deadline)
                    or pacer.stopped
                ):
                    stopped_early = True
                    return
                if not (ready_new or ready_known):
                    return
                key = _take()
                # AT TAKE TIME, the one place it needs to be: a host whose lane is back in
                # a ready heap has no row in flight. After the IP breaker's first trip (the
                # cool-down stage), a host whose latest answer was a throttle or a challenge -- and,
                # if the 429 rule tripped, a known Shopify-served host -- is not asked again. A host
                # first seen after the trip costs one request; if it throttles, that is its last.
                if ip_breaker.blocks(key):
                    _drain_ip_throttled_lane(key)
                    lane_freed.notify_all()
                    continue
                seed_id = lanes[key].popleft()
                in_flight += 1
                rows_started += 1
            try:
                await _process(seed_id, candidate_hosts.get(seed_id, ""))
            finally:
                async with lane_freed:
                    in_flight -= 1
                    # A PHANTOM PLACE (controller review, LOW): a row that ended without sending a
                    # request (a missing seed, a refusal before the fetch) leaves its host's
                    # provisional new-host place behind, holding room under the cap for nobody.
                    # Give it back. A no-op once the host has really been contacted. (A drained lane
                    # needs no release of its own: a breaker only ever drains a host that answered.)
                    pacer.release(key)
                    if (
                        key in tripped_hosts or key in unreachable_hosts or key in challenged_hosts
                        or key in locked_hosts
                    ):
                        _drain_tripped_lane(key)
                    elif ip_breaker.tripped and lanes.get(key):
                        _drain_ip_throttled_lane(key)
                    elif lanes.get(key):
                        heapq.heappush(ready_known, (queue_position[lanes[key][0]], key))
                    else:
                        lanes.pop(key, None)
                    lane_freed.notify_all()

    with crawl_ip_throttle.installed(ip_breaker), crawl_global_pacer.installed(pacer):
        await _run_workers(_worker, concurrency)
    host_423_stamped = await _stamp_crawl_attempts(locked_skipped_ids) if locked_skipped_ids else 0
    # AFTER the run, when every host's share of hard 404s is known (the brake), never per row. And ALONE: every worker
    # has returned, so no refresh of this run shares the apply step's connection (`databases` hands tasks gathered on
    # one Database the same connection, which a FOR UPDATE does not block). See `apply_unconfirmed_hides`.
    unconfirmed_hide = (
        await apply_unconfirmed_hides(unconfirmed_hosts, mode=unconfirmed_mode)
        if unconfirmed_mode != "off" else {"mode": "off", "totals": {}, "top_hosts": {}}
    )
    if ip_breaker.tripped:
        logger.warning(
            "external referral refresh: IP throttled at %s (trip %d of %d, %d distinct hosts); the "
            "run ended and %d rows were deferred to the next run",
            ip_breaker.tripped_at, ip_breaker.trips, ip_breaker.max_trips,
            ip_breaker.trip_host_count, skipped_for_ip_throttle,
        )
    if stopped_early:
        # The rows still queued, plus the ones in flight when the pacer stopped on the budget.
        skipped_for_budget = sum(len(lane) for lane in lanes.values()) + pace_stopped_for_budget
        # LOUD, because a silent truncation reads exactly like a completed sweep: the summary
        # would otherwise show a smaller `refreshed` with no reason attached.
        logger.warning(
            "external referral refresh hit its %.0fs wall-clock budget after %d/%d rows; "
            "%d candidates left unrefreshed this run",
            budget, rows_started, len(candidate_seed_ids), skipped_for_budget,
        )
    elapsed = time.monotonic() - started
    # Real origin contact, not the success count: `refreshed` includes rows served from the
    # cached snapshot because the gate refused, timed out or paced out. This ratio is the one
    # number that says whether the run did its job.
    origin_reads = max(0, refreshed - refreshed_from_cache)
    # ATTEMPTED rows, not candidates. `skipped_for_budget` rows were never tried, so counting
    # them in the denominator charges the run for work it deliberately deferred and sets the
    # floor to flap: the 09-05 night reads 0.498 by candidates but ~0.596 by attempts.
    # `unprocessable` rows leave the denominator too: a seed with no usable destination was
    # never a candidate for an origin read, so charging the yield for it understates a healthy
    # run by roughly the size of that permanent backlog (~628 today).
    # Rows the host breaker passed over leave it too, for the same reason as budget skips: they
    # were never tried.
    attempted_count = max(
        0,
        len(candidate_seed_ids)
        - skipped_for_budget
        - unprocessable
        - skipped_for_host_backoff
        - skipped_for_unreachable_host
        - skipped_for_host_challenge
        - skipped_for_host_423
        - skipped_for_ip_throttle,
    )
    origin_yield = (origin_reads / attempted_count) if attempted_count else 1.0
    min_yield = _min_origin_yield()
    low_yield = bool(attempted_count) and origin_yield < min_yield

    if refreshed_from_cache:
        logger.warning(
            "external referral refresh: %d/%d rows counted as refreshed came from the cached "
            "snapshot without reaching the origin (paced-out host, robots, timeout or TLS)",
            refreshed_from_cache, refreshed,
        )
    # Derived from the skip histogram with the WRITER's set, never restated here and never a
    # second counter: skips with no offer row the writer may touch.
    from services.external_offer_dual_write import OFFER_SYNC_STRUCTURAL_SKIP_STATUSES

    proj_structural = sum(
        n for status, n in proj_skips.items() if status in OFFER_SYNC_STRUCTURAL_SKIP_STATUSES
    )
    base_status = batch_run_status(
        failed=failed,
        stopped_early=stopped_early,
        attempted_count=attempted_count,
        origin_reads=origin_reads,
        price_changes=price_changed,
        projections_attempted=proj_attempted,
        projections_written=proj_written,
        projections_errored=proj_errored,
        projections_structural_skips=proj_structural,
        projections_written_attached=int(proj_writes.get("attached") or 0),
        candidate_count=len(candidate_seed_ids),
        skipped_for_budget=skipped_for_budget,
        projections_refused_currency=int(proj_skips.get("refused_currency") or 0),
    )
    return {
        # HONEST STATUS. This used to be `success if failed == 0`, and `failed` only counts
        # exceptions — so a run that stopped on budget, or that served half its rows from cache
        # without reaching a single origin, reported success. The job then exited 0 and Cloud
        # Run showed a green tick over a night that refreshed almost nothing. `stopped_early`
        # and `low_origin_yield` are the two ways that happens in practice.
        #
        # `ip_throttled` OUTRANKS the other rules, and says so. The 429s before the trip drag the
        # yield under its floor, so the yield rule would call every IP-throttled night
        # "degraded" and hide the cause again. What the other rules would have said is kept in
        # `status_without_ip_throttle`, so a night that was ALSO broken for another reason
        # (failed rows, a starved budget) is still visible.
        "status": "ip_throttled" if ip_breaker.tripped else base_status,
        "status_without_ip_throttle": base_status,
        # "healed 2,000" vs "healed 0" — the projection OUTCOME, not just that it was called.
        "unprocessable": unprocessable,
        "unprocessable_reasons": dict(sorted(unprocessable_reasons.items(), key=lambda kv: -kv[1])[:8]),
        # How much of the budget the projection itself consumed. With the flag armed the PDP
        # rebuild is ~1s/key, so this is the number that says whether arming it halved throughput.
        "projection_seconds": round(proj_seconds, 2),
        "projections_attempted": proj_attempted,
        "projections_written": proj_written,
        # Errors were counted in the helper and read NOWHERE: a projection that raised on every
        # row summarised as healthy. The PDP half has its own counters because `projections_written`
        # means the OFFER landed, and the view rebuild can silently no-op or fail independently.
        "projections_errored": proj_errored,
        "projection_skips": dict(sorted(proj_skips.items(), key=lambda kv: -kv[1])[:8]),
        # Skips with no offer row to write (a subset of `projection_skips`). The status rule
        # judges `projections_attempted - projection_structural_skips`, the ones that should write.
        "projection_structural_skips": proj_structural,
        # Seeds whose offer the writer refused for want of a currency they can vouch for (a subset of
        # the structural skips, counted on its own because a spike degrades the run).
        "projections_refused_currency": int(proj_skips.get("refused_currency") or 0),
        # Mirror offers left stale because the stored row is declared for another market (a subset of the
        # structural skips): each one is a row to re-stamp.
        "projections_frozen_market_mismatch": int(proj_skips.get("frozen_market_mismatch") or 0),
        # Written rows by target: `mirror` (the seed's mirror product) or `attached` (the
        # canonical's offer for the seed's listing).
        "projection_writes": dict(sorted(proj_writes.items())),
        # Offer ROWS the attached lane refused, by reason (a seed can write its product-level row
        # and refuse a variant the page did not list).
        "projection_offer_skips": dict(sorted(proj_offer_skips.items(), key=lambda kv: -kv[1])[:8]),
        "pdp_refreshed": pdp_refreshed,
        "pdp_errored": pdp_errored,
        "pdp_skips": dict(sorted(pdp_skips.items(), key=lambda kv: -kv[1])[:8]),
        "attempted_count": attempted_count,
        "degraded_reason_counts": dict(sorted(degraded_reasons.items(), key=lambda kv: -kv[1])[:12]),
        "top_degraded_hosts": dict(sorted(degraded_hosts.items(), key=lambda kv: -kv[1])[:10]),
        "origin_reads": origin_reads,
        "origin_yield": round(origin_yield, 4),
        "min_origin_yield": min_yield,
        "low_origin_yield": low_yield,
        "gating_policy_version": EXTERNAL_REFERRAL_GATING_POLICY_VERSION,
        "candidate_count": len(candidate_seed_ids),
        # `candidate_count` is what we INTENDED to refresh, so on a budget stop it and
        # `refreshed + degraded + failed` no longer agree. These three say why, rather than
        # leaving a short run looking like a complete one.
        "stopped_early": stopped_early,
        "budget_seconds": budget,
        "elapsed_seconds": round(elapsed, 3),
        # Hosts read at once, one row in flight per host. 1 = the serial loop.
        "host_concurrency": concurrency,
        "rows_started": rows_started,
        "rows_per_second": round(rows_started / elapsed, 3) if elapsed > 0 else None,
        "skipped_for_budget": skipped_for_budget,
        # Share of candidates the loop got to before the budget ran out; `batch_run_status`
        # degrades a budget stop below `min_budget_reach`.
        "budget_reach": round(
            (len(candidate_seed_ids) - skipped_for_budget) / len(candidate_seed_ids), 4
        ) if candidate_seed_ids else 1.0,
        "min_budget_reach": _min_budget_reach(),
        # Rows NOT attempted because their host tripped the 429/503 breaker, per host. Kept
        # apart from `skipped_for_budget` because the fix differs: a budget skip is a slow
        # night, a host skip is one merchant throttling our egress IP.
        "skipped_for_host_backoff": skipped_for_host_backoff,
        "host_backoff_skips": dict(sorted(tripped_hosts.items(), key=lambda kv: -kv[1])[:10]),
        "host_block_trip": host_trip,
        "host_breaker_armed": bool(candidate_hosts) and host_trip > 0,
        # Rows NOT attempted because their host gave no answer (connection, timeout, DNS, TLS)
        # `host_unreachable_trip` rows in a row. Unstamped, like the 429 breaker's skips.
        "skipped_for_unreachable_host": skipped_for_unreachable_host,
        "unreachable_host_skips": dict(sorted(unreachable_hosts.items(), key=lambda kv: -kv[1])[:10]),
        "host_unreachable_trip": unreachable_trip,
        # Rows NOT attempted because their host answered `host_challenge_trip` Cloudflare
        # challenges in a row (any status), per host. Unstamped, like the other host skips.
        "skipped_for_host_challenge": skipped_for_host_challenge,
        "host_challenge_skips": dict(sorted(challenged_hosts.items(), key=lambda kv: -kv[1])[:10]),
        "host_challenge_trip": challenge_trip,
        # Rows NOT attempted because their host answered `host_423_trip` HTTP 423s in a row, per host.
        # STAMPED (`host_423_stamped` of them), unlike the other host skips: see _HOST_LOCKED_TRIP_DEFAULT.
        "skipped_for_host_423": skipped_for_host_423,
        "host_423_skips": dict(sorted(locked_hosts.items(), key=lambda kv: -kv[1])[:10]),
        "host_423_trip": locked_trip,
        "host_423_stamped": host_423_stamped,
        "unreachable_host_errors": unreachable_samples,
        # Rows NOT attempted because the IP breaker tripped and their host had throttled us or is
        # Shopify-served.
        # Unstamped, like the per-host skips, so they lead tomorrow's queue.
        "skipped_for_ip_throttle": skipped_for_ip_throttle,
        "ip_throttle_skips": dict(sorted(ip_throttle_skips.items(), key=lambda kv: -kv[1])[:10]),
        # `ip_throttled`, the trip time, host counts, first/last 429 and the 429/503 header
        # aggregate (`throttle_diagnostics`): see `crawl_ip_throttle.IpThrottleBreaker.summary`.
        **ip_breaker.summary(),
        # The global pacer: `global_rps` as configured, `final_rate` after any halving,
        # `paces_waited_seconds` (summed over workers), `cooldowns`, the new-host cap and counts.
        **pacer.summary(),
        # Where the budget went, per host: the 15 hosts that cost the most wall-clock.
        "host_seconds": {
            host: {
                "rows": int(v["rows"]),
                "origin_reads": int(v["origin_reads"]),
                "degraded": int(v["degraded"]),
                "seconds": round(v["seconds"], 1),
            }
            for host, v in sorted(host_stats.items(), key=lambda kv: -kv[1]["seconds"])[:15]
        },
        "refreshed": refreshed,
        # SUBSET of `refreshed`, not a separate bucket: these rows took the success path on a
        # cached snapshot without reaching the origin. `refreshed - refreshed_from_cache` is
        # the number of seeds this run actually re-read.
        "refreshed_from_cache": refreshed_from_cache,
        "degraded": degraded,
        "failed": failed,
        "price_changed": price_changed,
        "price_unchanged": price_unchanged,
        "price_unavailable": price_unavailable,
        "price_filled": price_filled,
        "price_skipped_incomplete_pair": price_skipped_incomplete_pair,
        "price_skipped_currency_mismatch": price_skipped_currency_mismatch,
        "price_skipped_non_positive": price_skipped_non_positive,
        "price_skipped_unreadable": price_skipped_unreadable,
        "price_not_read": price_not_read,
        **({"price_skipped_ambiguous": price_skipped_ambiguous} if ambiguous_mode != "off" else {}),
        "availability_not_read": availability_not_read,
        "price_unreadable_reasons": price_unreadable_reasons,
        # Which hosts' pages the extractor would not price, so night one says WHERE (a host whose
        # pages name no currency shows up here every night until it is looked at).
        "price_unreadable_top_hosts": dict(
            sorted(price_unreadable_hosts.items(), key=lambda kv: (-kv[1], kv[0]))[:25]
        ),
        "availability_changed": availability_changed,
        # Rows whose served URL names no product handle, and what EXTERNAL_SEED_HANDLELESS_READ decided for them
        # (`count_handleless_read`): nothing is counted with the flag off.
        "handleless_read": handleless_read_summary(handleless_read_mode(), handleless_totals, handleless_hosts),
        # The unconfirmed-dead hide (EXTERNAL_SEED_DEAD_UNCONFIRMED_HIDE): per host and in total, what the run
        # observed, its candidates, what it hid (`on`) or would have (`shadow`), and what the host brake held.
        "unconfirmed_hide": unconfirmed_hide,
        # The ambiguous-price guard (EXTERNAL_SEED_AMBIGUOUS_PRICE_GUARD): absent with the flag off.
        **(
            {"ambiguous_price": ambiguous_price_summary(ambiguous_mode, ambiguous_totals, ambiguous_hosts, ambiguous_lines)}
            if ambiguous_mode != "off"
            else {}
        ),
        # The catalogue price lane: off, or how many hosts it planned and read, what each read came to,
        # how many rows it priced, and why the others kept their page read (`rows`).
        "catalogue": {
            "enabled": catalogue_on,
            "page_read_column_missing": catalogue_column_missing,
            "min_seeds": catalogue_min,
            "hosts_planned": len(catalogue_wanted),
            "hosts_read": len(host_catalogues),
            "host_status": dict(Counter(c.status for c in host_catalogues.values())),
            "pages_read": sum(c.pages_read for c in host_catalogues.values()),
            "rows_read": catalogue_rows_read,
            "rows": dict(sorted(catalogue_reasons.items(), key=lambda kv: -kv[1])),
            "page_max_age_days": catalogue_lane.page_max_age_days(),
            # Handed a catalogue read, and read from their page after all (the route's reason).
            "fallbacks": dict(sorted(catalogue_fallbacks.items(), key=lambda kv: -kv[1])),
            "hosts": [
                c.summary() for c in sorted(host_catalogues.values(), key=lambda c: -c.wanted)[:15]
            ],
        },
        "errors": errors[:20],
    }
