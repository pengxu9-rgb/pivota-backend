"""Freshness clocks over the protocol directory (FIX_PLAN R-03, flow-2 exit). Read-only, periodic,
behind FRESHNESS_DIRECTORY_CHECK_ENABLED (default off).

A purchase gate trusts facts that each age out on their own clock. A job that stops (a crawl throttle,
a pinned image, a paused trigger) turns a working merchant into a refusal within days and nothing says
so. The flow-2 exit asks that EVERY merchant the protocol directory (`GET /agent/v1/merchants?scope=
protocol`) can list has a fact younger than its clock, and that a stale or missing fact says so.

THE PAIRS are the directory's own: `services.agent_merchant_directory.merchant_facts()`, the pairs
the route composes its list from after every exclusion (rigs, rejected stores, quarantines,
development and wholesale hosts, the operator's excluded domains). A pair an operator DISABLED is
skipped: it is never listed. They are NOT narrowed to pairs that list a route today, because a
route is exactly what a stale fact takes away.

A CLOCK IS KEPT ONLY WHERE A JOB REFRESHES IT. Paging on a fact no job will ever refresh keeps the
past-limit incident open for good, and then a NEW stale merchant (what this check exists for) adds
no notification. So each pair is judged against the population of the job that writes its fact,
read from that job's own code, never from a predicate restated here:

  card_fact        merchant_purchasability, the BUYER vantage row (the one both directory readers
                   ask), for a pair in the purchasability sweep's population
                   (`jobs.merchant_purchasability_sweep.collect_population`, the one place that
                   population is decided, with the cart-mint lane read from its CACHED daily scan:
                   this check never scans the seed table and never writes the cache).
                     - a key another lane names: limit = the TTL (MERCHANT_PURCHASABILITY_TTL_HOURS,
                       default 72 h); no buyer-vantage row is `missing`;
                     - a WARM-ONLY key (`warm_only_keys`) whose last check found no Shopify catalog
                       is re-checked only every `..._WARM_CLICK_NOT_SHOPIFY_RECHECK_DAYS` (7), so
                       its limit is that window plus the TTL (once it is due, the sweep has the
                       same TTL to reach it as any key); any other warm-only key gets the TTL;
                     - a warm-only key with no row yet is `pending`, counted and never paged: the
                       sweep takes at most `..._WARM_CLICK_MAX_PER_RUN` of them a run by design;
                     - with that dial at 0 the warm-click lane is out of the population, as in
                       the sweep.
                   FRESH iff checked_at > now - limit, the strict rule `human_handoff_allowed` uses
                   (exactly the limit old is stale). Any verdict: a negative fact also ages.
                   KNOWN DIFFERENCE: the clock is `checked_at`, not `positive_until`. A merchant
                   whose checks keep coming back unverifiable (BLOCKED_UNKNOWN, TRANSPORT_ERROR)
                   loses its positive window while this clock reads `fresh`: it says the sweep is
                   running, not that the merchant is purchasable.
  tierb_cart_link  tierb_cart_link_eligibility, ONLY for a pair with an ELIGIBLE row that is BOTH in
                   the sweep's cart-link lane and on the Tier B job's own merchant list
                   (`services.tierb_cart_link_merchants.load_merchants`, what that job checks):
                   older than `db.tierb_cart_link_eligibility.DEFAULT_MAX_AGE_HOURS` (48 h, what
                   the routes pass; the gate is inclusive, so exactly 48 h is fresh) is `stale`.
                   Another definite verdict is the store's own answer (`negative`, counted, never
                   warned on). A row no job refreshes (off the list, or never given a verdict) is
                   `untracked`.

Every other directory pair is `untracked` on the card clock: counted in the summary, never a page.

THE LINES, through the "pivota" logger (they survive in production):
  freshness_directory: WARNING {...}  one per (clock, pair) that is `stale` or `missing`, plus one
                                      `freshness_directory_past_limit` money alert line.
  freshness_directory_check: {...}    the per-tick summary, ONLY when the tick read everything (the
                                      heartbeat): pairs, fresh, stale, missing, pending, negative,
                                      untracked, unavailable, build_ms.
  freshness_directory_check_unavailable: {...}
                                      INSTEAD of the summary when the tick could not read its
                                      population: the directory raised or took longer than
                                      `BUILD_WAIT_SECONDS` (15 s) to build, a read failed, or a
                                      sweep lane was unreadable (the clocks still run over what was
                                      read). A different text, so it never satisfies the heartbeat:
                                      the went-silent policy and the unavailable policy both see it.
Read only. Never raises.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from db.database import database
from db.reap_agentic_ledger import _decode_dt
from utils import money_alerts
from utils.logger import logger

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _aware(value: Any) -> Optional[datetime]:
    value = _decode_dt(value)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _tierb_limit() -> timedelta:
    # Imported per tick, not at module import, so the flag-off import set stays what it was.
    from db.tierb_cart_link_eligibility import DEFAULT_MAX_AGE_HOURS

    return timedelta(hours=DEFAULT_MAX_AGE_HOURS)


def _line(result: Dict[str, Any]) -> str:
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


DIRECTORY_FLAG_ENV = "FRESHNESS_DIRECTORY_CHECK_ENABLED"
DIRECTORY_JOB_ID = "freshness_directory_check"
#: Hourly CRON, never `interval`: an interval clock restarts on every worker deploy and starves
#: (services/audit_scheduler.py, "merchant_purchasability_sweep is deliberately NOT registered").
#: :34 is after the sweep's own hourly run (:07, task timeout 20 min, so done by :27), clear of the
#: :10-:25 refresh windows and of the worker's :30/:31/:37 crons.
DIRECTORY_CRON_MINUTE = 34
DIRECTORY_MISFIRE_GRACE_SECONDS = 1800

SUMMARY_PREFIX = "freshness_directory_check: "
UNAVAILABLE_PREFIX = "freshness_directory_check_unavailable: "

_DIRECTORY_CARD_SQL = """
SELECT merchant_domain, market_country, verdict, checked_at FROM merchant_purchasability
 WHERE vantage = :vantage
"""
_DIRECTORY_TIERB_SQL = """
SELECT shop_domain AS merchant_domain, market AS market_country, verdict, checked_at
  FROM tierb_cart_link_eligibility
"""


def directory_check_enabled() -> bool:
    return (os.getenv(DIRECTORY_FLAG_ENV) or "").strip().lower() in _TRUTHY


def _freshest(rows: Any) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """{(domain, market): the freshest row}, keyed the way the directory keys its pairs."""
    from services import agent_merchant_directory as directory

    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for record in rows:
        row = dict(record)
        pair = directory._pair(row)
        if pair is None:
            continue
        row["checked_at"] = _aware(row.get("checked_at"))
        held = out.get(pair)
        if held is None or (row["checked_at"] or datetime.min.replace(tzinfo=timezone.utc)) > (
                held["checked_at"] or datetime.min.replace(tzinfo=timezone.utc)):
            out[pair] = row
    return out


def _pair_result(clock: str, domain: str, market: str, *, status: str, age: Optional[timedelta],
                 limit: timedelta, verdict: Any = None) -> Dict[str, Any]:
    return {
        "clock": clock, "domain": domain, "market": market, "status": status,
        "age_seconds": int(age.total_seconds()) if age is not None else None,
        "limit_seconds": int(limit.total_seconds()),
        "verdict": str(verdict) if verdict is not None else None,
    }


def _directory_card(row: Optional[Dict[str, Any]], domain: str, market: str, now: datetime,
                    limit: timedelta, *, absent: str = "missing") -> Dict[str, Any]:
    if row is None or row.get("checked_at") is None:
        return _pair_result("card_fact", domain, market, status=absent, age=None, limit=limit,
                            verdict=(row or {}).get("verdict"))
    age = now - row["checked_at"]
    # STRICT, as `human_handoff_allowed` reads it: fresh iff checked_at > now - limit.
    return _pair_result("card_fact", domain, market, status="stale" if age >= limit else "fresh",
                        age=age, limit=limit, verdict=row.get("verdict"))


def _directory_tierb(row: Optional[Dict[str, Any]], domain: str, market: str, now: datetime,
                     *, refreshed: bool) -> Optional[Dict[str, Any]]:
    if row is None:
        return None  # not on the cart-link lane: no clock to keep
    limit = _tierb_limit()
    checked = row.get("checked_at")
    age = now - checked if checked is not None else None
    if row.get("verdict") is None:
        status = "untracked"  # never given a definite verdict: the door cannot use it either way
    elif row["verdict"] != "ELIGIBLE":
        status = "negative"
    elif not refreshed or age is None:
        status = "untracked"  # no job re-checks it; it ages out of the gate on its own
    else:
        # INCLUSIVE, as `is_cart_link_eligible` reads it: fresh iff checked_at >= now - 48 h.
        status = "stale" if age > limit else "fresh"
    return _pair_result("tierb_cart_link", domain, market, status=status, age=age, limit=limit,
                        verdict=row.get("verdict"))


def _unavailable(summary: Dict[str, Any], reason: str, exc: Optional[BaseException] = None) -> Dict[str, Any]:
    summary["unavailable"] = 1
    summary["reason"] = reason
    if exc is not None:
        summary["error_type"] = type(exc).__name__  # the type only: never a message (a DSN, a key)
    logger.warning("%s%s", UNAVAILABLE_PREFIX, _line(summary))
    return summary


async def run_freshness_directory_check_tick(*, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """One check over the protocol directory. None (nothing read, nothing logged) while
    FRESHNESS_DIRECTORY_CHECK_ENABLED is off. Never raises."""
    if not directory_check_enabled():
        return None
    summary: Dict[str, Any] = {"pairs": None, "fresh": 0, "stale": 0, "missing": 0, "pending": 0,
                               "negative": 0, "untracked": 0, "unavailable": 0, "build_ms": None}
    try:
        import db.merchant_purchasability as mp
        from jobs import merchant_purchasability_sweep as sweep
        from services import agent_merchant_directory as directory
        from services.tierb_cart_link_merchants import load_merchants
    except Exception as exc:  # noqa: BLE001 - an import that fails is a check that cannot read
        return _unavailable(summary, "import_failed", exc)

    # A caller's `now` may be naive (a job harness, a backfill): read it as UTC, the way every stored
    # timestamp is read, so an aware fact minus a naive clock can never raise mid-tick.
    now = (_aware(now) if now is not None else None) or datetime.now(timezone.utc)
    started = time.monotonic()
    try:
        facts = await directory.merchant_facts()
    except Exception as exc:  # noqa: BLE001 - a check that cannot read says so, and is counted
        summary["build_ms"] = int((time.monotonic() - started) * 1000)
        return _unavailable(summary, "directory_unavailable", exc)
    elapsed = time.monotonic() - started
    summary["build_ms"] = int(elapsed * 1000)
    if elapsed > directory.BUILD_WAIT_SECONDS:
        # What came back is the last clean build, served after this hour's build timed out.
        return _unavailable(summary, "directory_build_slow")
    tally: Dict[str, int] = {}
    try:
        lanes = await sweep.collect_population(tally=tally, cart_mint_cache_only=True)
        last = sweep.last_checks(await mp.list_due(mp.LIST_DUE_MAX, vantage=mp.WORKER_VANTAGE, strict=True))
        ttl = timedelta(hours=mp.ttl_hours())
        cards = _freshest(await database.fetch_all(_DIRECTORY_CARD_SQL, {"vantage": mp.buyer_vantage()}))
        tierbs = _freshest(await database.fetch_all(_DIRECTORY_TIERB_SQL))
        tierb_job = {(m.domain, m.market) for m in load_merchants()}
    except Exception as exc:  # noqa: BLE001
        return _unavailable(summary, "read_failed", exc)

    warm_only = sweep.warm_only_keys(lanes)
    population = set(sweep.merge_lanes(lanes))
    if sweep.warm_click_cap() == 0:
        population -= warm_only  # the lane is switched off: not in the sweep's population at all
    recheck = sweep.warm_click_recheck()
    cart_link = set(lanes.get("cart-link", {}))

    pairs = sorted({(f.merchant_domain, f.market) for f in facts if not f.disabled}, key=lambda p: (p[1], p[0]))
    summary["pairs"] = len(pairs)
    for domain, market in pairs:
        key = (domain, market)
        card_row = cards.get(key)
        if key not in population:
            card = _pair_result("card_fact", domain, market, status="untracked", age=None, limit=ttl)
        elif key in warm_only:
            seen = last.get(key) or {}
            no_catalog = sweep._found_no_shopify_catalog(seen.get("verdict"), seen.get("detail"))
            card = _directory_card(card_row, domain, market, now, ttl + recheck if no_catalog else ttl,
                                   absent="pending")
        else:
            card = _directory_card(card_row, domain, market, now, ttl)
        results = [card]
        tierb_result = _directory_tierb(tierbs.get(key), domain, market, now,
                                        refreshed=key in cart_link and key in tierb_job)
        if tierb_result is not None:
            results.append(tierb_result)
        for result in results:
            summary[result["status"]] += 1
            if result["status"] in ("stale", "missing"):
                logger.warning("freshness_directory: WARNING %s", _line(result))
                money_alerts.emit(
                    "freshness_directory_past_limit", level="warning", clock=result["clock"],
                    domain=domain, market=market, status=result["status"],
                    age_seconds=result["age_seconds"], limit_seconds=result["limit_seconds"])
    unreadable = int(tally.get(sweep.UNREADABLE_TALLY, 0))
    if unreadable:
        summary["population_unreadable"] = unreadable
        return _unavailable(summary, "population_unreadable")
    logger.info("%s%s", SUMMARY_PREFIX, _line(summary))
    return summary
