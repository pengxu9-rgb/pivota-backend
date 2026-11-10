"""Freshness clocks over the protocol directory (FIX_PLAN R-03, flow-2 exit). Read-only, periodic,
behind FRESHNESS_DIRECTORY_CHECK_ENABLED (default off).

A purchase gate trusts facts that each age out on their own clock. A job that stops (a crawl throttle,
a pinned image, a paused trigger) turns a working merchant into a refusal within days and nothing says
so. The flow-2 exit asks that EVERY merchant the protocol directory (`GET /agent/v1/merchants?scope=
protocol`) can list has a fact younger than its clock, and that a stale or missing fact says so.

THE POPULATION is the directory's own: `services.agent_merchant_directory.merchant_facts()`, the
pairs the route composes its list from after every exclusion (rigs, rejected stores, quarantines,
development and wholesale hosts, the operator's excluded domains). A pair an operator DISABLED is
skipped: it is never listed. The population is NOT narrowed to pairs that list a route today,
because a route is exactly what a stale fact takes away; a check over listed pairs only would
never see the merchant it exists to see.

THE CLOCKS, per (domain, market):
  card_fact        merchant_purchasability, the BUYER vantage row (the one both directory readers
                   ask): age = now - checked_at, limit = the TTL (MERCHANT_PURCHASABILITY_TTL_HOURS,
                   default 72 h). Any verdict: the sweep re-checks every row, so a negative fact
                   that ages out also means the sweep stopped. No buyer-vantage row: `missing`.
  tierb_cart_link  tierb_cart_link_eligibility, ONLY for a pair that has a row (a pair with none is
                   not on the cart-link lane): ELIGIBLE older than 48 h
                   (db/tierb_cart_link_eligibility.DEFAULT_MAX_AGE_HOURS, what the routes pass) is
                   `stale`; another verdict is the store's own answer (`negative`, reported, never
                   warned on: that is not a clock).

THE LINES, through the "pivota" logger (they survive in production):
  freshness_directory: WARNING {...}  one per (clock, pair) that is `stale` or `missing`, plus one
                                      `freshness_directory_past_limit` money alert line.
  freshness_directory_check: {...}    the per-tick summary (a heartbeat): pairs, fresh, stale,
                                      missing, negative, unavailable. A fresh pair prints no line
                                      of its own (the directory can hold hundreds).
Two table reads per tick (both whole-table, both small: one row per pair and vantage), after the
directory's own build. Read only. Never raises.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from db.database import database
from db.reap_agentic_ledger import _decode_dt
from utils import money_alerts
from utils.logger import logger

_TRUTHY = frozenset({"1", "true", "yes", "on"})

TIERB_LIMIT = timedelta(hours=48)


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


def _result(clock: str, pair: Dict[str, Any], *, status: str, age: Optional[timedelta],
            limit: timedelta, **extra: Any) -> Dict[str, Any]:
    out = {
        "clock": clock, "domain": pair["domain"], "market": pair["market"], "status": status,
        "age_seconds": int(age.total_seconds()) if age is not None else None,
        "limit_seconds": int(limit.total_seconds()),
    }
    out.update(extra)
    return out


def _judge(age: Optional[timedelta], limit: timedelta) -> str:
    if age is None:
        return "missing"
    return "stale" if age > limit else "fresh"


def _line(result: Dict[str, Any]) -> str:
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


DIRECTORY_FLAG_ENV = "FRESHNESS_DIRECTORY_CHECK_ENABLED"
DIRECTORY_JOB_ID = "freshness_directory_check"
DIRECTORY_INTERVAL_SECONDS = 3600

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
    return _result(clock, {"domain": domain, "market": market}, status=status, age=age, limit=limit,
                   verdict=(str(verdict) if verdict is not None else None))


def _directory_card(row: Optional[Dict[str, Any]], domain: str, market: str, now: datetime,
                    limit: timedelta) -> Dict[str, Any]:
    if row is None or row.get("checked_at") is None:
        return _pair_result("card_fact", domain, market, status="missing", age=None, limit=limit,
                            verdict=(row or {}).get("verdict"))
    age = now - row["checked_at"]
    return _pair_result("card_fact", domain, market, status=_judge(age, limit), age=age, limit=limit,
                        verdict=row.get("verdict"))


def _directory_tierb(row: Optional[Dict[str, Any]], domain: str, market: str,
                     now: datetime) -> Optional[Dict[str, Any]]:
    if row is None:
        return None  # not on the cart-link lane: no clock to keep
    checked = row.get("checked_at")
    age = now - checked if checked is not None else None
    if row.get("verdict") is None:
        status = "missing"
    elif row["verdict"] != "ELIGIBLE":
        status = "negative"
    else:
        status = _judge(age, TIERB_LIMIT)
    return _pair_result("tierb_cart_link", domain, market, status=status, age=age, limit=TIERB_LIMIT,
                        verdict=row.get("verdict"))


async def run_freshness_directory_check_tick(*, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """One check over the protocol directory. None (nothing read, nothing logged) while
    FRESHNESS_DIRECTORY_CHECK_ENABLED is off. Never raises."""
    if not directory_check_enabled():
        return None
    import db.merchant_purchasability as mp
    from services import agent_merchant_directory as directory

    now = now or datetime.now(timezone.utc)
    summary: Dict[str, Any] = {"pairs": None, "fresh": 0, "stale": 0, "missing": 0, "negative": 0,
                               "unavailable": 0}
    try:
        facts = await directory.merchant_facts()
        limit = timedelta(hours=mp.ttl_hours())
        cards = _freshest(await database.fetch_all(_DIRECTORY_CARD_SQL, {"vantage": mp.buyer_vantage()}))
        tierbs = _freshest(await database.fetch_all(_DIRECTORY_TIERB_SQL))
    except Exception as exc:  # noqa: BLE001 - a check that cannot read says so, and is counted
        summary["unavailable"] = 1
        logger.warning("freshness_directory_check: directory unavailable (error_type=%s)", type(exc).__name__)
        logger.info("freshness_directory_check: %s", _line(summary))
        return summary
    pairs = sorted({(f.merchant_domain, f.market) for f in facts if not f.disabled}, key=lambda p: (p[1], p[0]))
    summary["pairs"] = len(pairs)
    for domain, market in pairs:
        results = [_directory_card(cards.get((domain, market)), domain, market, now, limit)]
        tierb_result = _directory_tierb(tierbs.get((domain, market)), domain, market, now)
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
    logger.info("freshness_directory_check: %s", _line(summary))
    return summary
