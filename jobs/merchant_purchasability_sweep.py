"""The SWEEP that gathers merchant purchasability facts (WP6).

It renders one merchant's landed checkout, reads whether that checkout offers a CARD and what it
charges, and records the fact. `db/merchant_purchasability.py` owns the rule; this module owns
the LOOP and its BOUNDS. Nothing here decides anything.

    services/shopify_cart_link_preflight.preflight  the one thing that touches a merchant.
    db/merchant_purchasability.record_check         the one thing that writes a fact.
    services/audit_scheduler                        registers `merchant_purchasability_sweep`.

── THE POPULATION, AND WHY IT IS THIS ONE ───────────────────────────────────────────────────

Surveyed 2026-09-22. There is NO single merchant x market table in this repo. Three candidates:

  * `execution_routes` (the UCP probe lane) — REJECTED. It has no market column at all, and it
    is not a purchase allowlist; `routes/store_audit_ops.checkout_tier_coverage` exists precisely
    to report that its merchant coverage is zero.
  * `reap_agentic_eligibility` — the VARIANT lane's allowlist, keyed
    (merchant_domain, market_country, product_key, variant_key). The merchant-grain row is
    `product_key = ''`, and this job does not spell that predicate itself: it calls
    `db.reap_agentic_ledger.list_enabled_merchant_markets`, which is the SET form of the exact
    predicate `routes/agent_commerce_reap` decides eligibility with. A population narrower than
    the door's allowlist is a merchant the gate never sees.
  * `tierb_cart_link_eligibility` — the CART-LINK lane's allowlist, keyed (shop_domain, market),
    and it already carries a confirmed `variant_id`.

THE POPULATION IS THE UNION OF THE TWO REAP ALLOWLISTS, AT MERCHANT GRAIN. Those two are
EXACTLY what `routes/agent_commerce_reap.py` consults before it will start a purchase — the
variant lane through `_eligibility`, the cart-link lane through `is_cart_link_eligible` — and
nothing else in this repo can make the door offer to buy. It is therefore the smallest set that
covers what the agent door can recommend for purchase. A merchant absent from both cannot be
bought from, so a fact about it would gate nothing.

One representative variant per merchant x market: the Tier B row's confirmed `variant_id` when we
have one, else our catalog's variant for that domain, else NONE — and with none the preflight
picks the storefront's first available variant itself, reading availability with
`?country=<market>` pinned. The expected price is OUR indexed unit price for that variant when
we hold one, so that a drift is a statement about our index and not about an unrelated SKU.

── THE GATE IS INSIDE THE JOB ───────────────────────────────────────────────────────────────

`MERCHANT_PURCHASABILITY_SWEEP_ENABLED`, read PER RUN, gates the whole run — and unlike the Reap
poller, gating the whole run is correct here, because this job has no sweep that must keep
running while the rail is off. It holds no PII, expires nothing and promises nothing to a buyer;
with the dial off it should touch no merchant at all, because every fetch it makes CREATES AN
ABANDONED CHECKOUT on that merchant's store. Registering it in the scheduler is therefore inert,
exactly like `reap_agentic_purchase_poll`, and arming it needs an env var rather than a redeploy.

IT IS A DIFFERENT DIAL FROM THE CONSUMERS' (`MERCHANT_PURCHASABILITY_ENFORCE`), and that is not
tidiness. `services.audit_scheduler._add_job` registers jobs ONLY on the production worker, and a
normal backend deploy does not ship the worker — so one shared dial, armed on the backend, would
switch the refusals on while this job never ran anywhere, and every merchant in the catalogue
would answer a permanent 409. Sweep first, verify coverage, enforce second: see
`db.merchant_purchasability.is_enforcement_enabled` and the runbook.

── VANTAGE ──────────────────────────────────────────────────────────────────────────────────

Every check runs from the worker's own egress and is recorded under vantage `worker`. When
`VANTAGE_PROXY_URL` is set the same merchant is ALSO checked through that HTTPS proxy and
recorded under vantage `proxy`, so a buyer-market vantage can be configured without code.
REACHABILITY IS EGRESS-DEPENDENT and that is measured, not assumed: judydoll.com resets direct
TCP from one of our egresses while answering through another. `is_purchasable` reads only the
vantage named by MERCHANT_PURCHASABILITY_BUYER_VANTAGE, which must match the buyer's.

── PII ──────────────────────────────────────────────────────────────────────────────────────

`SweepReport` carries COUNTS AND A DURATION. No domains, no variant ids, nothing from a row. The
preflight is called with `buyer=None` — the Reap path carries no buyer data in the link — so
there is no buyer anywhere on this path to log or store.

── WHAT ONE RUN COSTS A MERCHANT ────────────────────────────────────────────────────────────

One abandoned Shopify checkout per merchant per vantage, the same side effect every UCP probe
already has. That is why the batch is bounded, the run is budgeted, and the due list is
least-recently-checked first: a merchant is swept once per TTL window, not once per tick.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

import db.merchant_purchasability as facts
import db.reap_agentic_ledger as ledger
from db.database import database
from services.shopify_cart_link_preflight import (
    REQUEST_TIMEOUT_S,
    USER_AGENT,
    PreflightResult,
    preflight,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SweepReport",
    "is_enabled",
    "job_interval_seconds",
    "load_population",
    "run_merchant_purchasability_sweep",
]


# ── dials ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Dial:
    env: str
    default: int
    minimum: int
    maximum: int


#: Read PER RUN, never cached at import. The rail's own switch is not here: it is a boolean and
#: `is_enabled()` is its one authoritative reader.
DIALS: Dict[str, _Dial] = {
    # Registration-time only: the interval `_add_job` is given. A change needs a restart, which
    # is why it is the one dial not read inside the run. Hourly by default — the TTL is 72h, so
    # this is 72 chances to refresh a fact before it expires.
    "interval_seconds": _Dial("MERCHANT_PURCHASABILITY_INTERVAL_SECONDS", 3600, 60, 86400),
    # Merchants per run. Each one is a full redirect chain plus up to 20 catalog pages, and each
    # leaves an abandoned checkout behind, so this is a politeness bound as much as a time bound.
    "batch": _Dial("MERCHANT_PURCHASABILITY_BATCH", 20, 1, 200),
    # Wall-clock budget for one run. It stops the job STARTING a new merchant; a merchant already
    # in flight runs to completion, so a run can exceed this by one merchant's worth of fetches.
    # The entry in `_JOB_RUN_DEADLINES` is sized as budget + that one merchant.
    "budget_seconds": _Dial("MERCHANT_PURCHASABILITY_BUDGET_SECONDS", 600, 30, 3600),
    # Seconds to wait between merchants. One store at a time, unhurried: this rail has no
    # latency requirement and a burst of checkout creations against one platform does not help us.
    "pause_ms": _Dial("MERCHANT_PURCHASABILITY_PAUSE_MS", 1500, 0, 60000),
}

_WARNED: set = set()


def _env_int(dial: _Dial) -> int:
    """One dial's value, or its default WITH A WARNING NAMING THE VARIABLE (once per process).
    Never raises and never silently zeroes."""
    raw = (os.getenv(dial.env) or "").strip()
    if not raw:
        return dial.default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _warn_once(dial.env, "merchant_purchasability_sweep: %s is not an integer (%r); using %d",
                   dial.env, raw, dial.default)
        return dial.default
    if value < dial.minimum or value > dial.maximum:
        _warn_once(dial.env, "merchant_purchasability_sweep: %s=%d is outside %d..%d; using %d",
                   dial.env, value, dial.minimum, dial.maximum, dial.default)
        return dial.default
    return value


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(message, *args)


def _reset_dial_warnings() -> None:
    """Test seam."""
    _WARNED.clear()


def is_enabled() -> bool:
    """THE SWEEP DIAL (`MERCHANT_PURCHASABILITY_SWEEP_ENABLED`), and ONLY that one.

    DELEGATED to `db.merchant_purchasability.is_sweep_enabled` rather than re-read here, so the
    dial has one authoritative reader. It is a DIFFERENT dial from the one the consumers read
    (`MERCHANT_PURCHASABILITY_ENFORCE`), and that separation is the whole point: this job runs
    only on the worker, so arming enforcement without first arming and completing a sweep would
    refuse every merchant in the catalogue. See `is_enforcement_enabled` for the arming order.

    Read per run and NOTHING caches or shortcuts around it: with it off this job must touch no
    merchant, because every check it makes creates an abandoned checkout on a live store."""
    return facts.is_sweep_enabled()


def job_interval_seconds() -> int:
    """The registration interval. Read ONCE, by `services.audit_scheduler.start_scheduler`."""
    return _env_int(DIALS["interval_seconds"])


def proxy_url() -> Optional[str]:
    """The optional second vantage. An https/http proxy URL; anything else is ignored rather than
    handed to httpx, which would raise inside the run."""
    raw = (os.getenv("VANTAGE_PROXY_URL") or "").strip()
    return raw if raw.startswith(("http://", "https://")) else None


# ── population ──────────────────────────────────────────────────────────────────────────────
#
# Module-level constants, no f-strings: tests/test_repo_sql_prepare_postgres.py resolves a
# `database.*` first argument only when it is a literal or a module-level name.

#: The CART-LINK lane's allowlist, with the variant it already confirmed on the storefront.
#: `verdict = 'ELIGIBLE'` matches what `is_cart_link_eligible` requires before the door will use
#: this lane; a row in any other verdict cannot produce a purchase, so a fact about it gates
#: nothing.
_CART_LANE_SQL = """
SELECT shop_domain AS domain, market AS market, variant_id
  FROM tierb_cart_link_eligibility
 WHERE verdict = 'ELIGIBLE'
"""

#: OUR indexed unit price for a variant on a domain. `catalog_offers.market` is NOT a conjunct:
#: it is a NOT NULL DEFAULT 'US' that the external-seed writer never sets, so it claims 'US' for
#: rows that are not, and the base cart-link loader (`_CART_OFFER_SQL`) already excludes it for
#: that reason. Joining on it would silently price zero merchants.
_CATALOG_PRICE_SQL = """
SELECT s.source_variant_id AS variant_id,
       o.currency AS currency,
       CAST(COALESCE(o.merchant_effective_price, o.estimated_best_price, o.list_price) AS TEXT) AS price
  FROM catalog_products p
  JOIN catalog_skus s ON s.product_key = p.product_key
  JOIN catalog_offers o ON o.sku_key = s.sku_key
 WHERE lower(p.source_domain) = :domain
   AND s.source_variant_id = :variant_id
   AND COALESCE(o.merchant_effective_price, o.estimated_best_price, o.list_price) IS NOT NULL
 ORDER BY COALESCE(o.merchant_effective_price, o.estimated_best_price, o.list_price) ASC
 LIMIT 1
"""

#: A representative variant for a domain when the Tier B lane has none. Deterministic ordering so
#: two runs pick the same variant and their prices are comparable.
_CATALOG_VARIANT_SQL = """
SELECT s.source_variant_id AS variant_id
  FROM catalog_products p
  JOIN catalog_skus s ON s.product_key = p.product_key
 WHERE lower(p.source_domain) = :domain
   AND s.source_variant_id IS NOT NULL
   AND s.source_variant_id <> ''
 ORDER BY length(s.source_variant_id) ASC, s.source_variant_id ASC
 LIMIT 1
"""


@dataclass(frozen=True)
class Target:
    """One merchant x market to check. `variant_id` may be None — the preflight then picks a
    representative available variant itself, for the right market."""

    domain: str
    market: str
    variant_id: Optional[str] = None
    expected_price_minor: Optional[int] = None
    expected_currency: Optional[str] = None


async def _rows(statement: str) -> List[Dict[str, Any]]:
    """One population query, failing SOFT. A lane whose table does not exist on this database
    (SQLite dev, a partially migrated environment) contributes nothing rather than ending the
    sweep — the other lane is still worth sweeping."""
    try:
        return [dict(r) for r in await database.fetch_all(statement)]
    except Exception:  # noqa: BLE001
        logger.warning("merchant_purchasability_sweep: a population lane could not be read")
        return []


async def load_population(limit: int) -> List[Target]:
    """The merchant x market set to sweep, bounded and deduped.

    The two lanes are unioned on (domain, market). WHERE THEY OVERLAP THE CART LANE WINS the
    variant, because its `variant_id` was confirmed available on the storefront for that market
    by the Tier B job — a stronger fact than anything the catalog holds.
    """
    merged: Dict[Tuple[str, str], Optional[str]] = {}
    # THE VARIANT LANE'S SET COMES FROM THE LANE ITSELF, not from a second copy of its predicate
    # written here. `routes/agent_commerce_reap` decides eligibility with the same sentinel this
    # helper filters on, so the population cannot be narrower than what the door accepts — which
    # it was, until a reviewer measured a merchant the route admitted and the sweep never visited
    # (the sweep additionally required `variant_key = ''`; the route never has).
    for row in await ledger.list_enabled_merchant_markets():
        key = (
            facts.normalize_domain(row.get("merchant_domain")),
            facts.normalize_market(row.get("market_country")),
        )
        if key[0] and len(key[1]) == 2:
            merged.setdefault(key, None)
    for row in await _rows(_CART_LANE_SQL):
        key = (facts.normalize_domain(row.get("domain")), facts.normalize_market(row.get("market")))
        variant = str(row.get("variant_id") or "").strip() or None
        if key[0] and len(key[1]) == 2 and (variant or key not in merged):
            merged[key] = variant

    # Least-recently-checked FIRST, and never-checked first of all — ordered before the catalog
    # lookups so that a big population does not spend its budget pricing merchants this run will
    # not reach. Sorted in Python against what `list_due` reports rather than in SQL, because the
    # population comes from two OTHER tables and cannot be joined to this one portably.
    seen: Dict[Tuple[str, str], Any] = {}
    for row in await facts.list_due(500, vantage=facts.WORKER_VANTAGE):
        key = (str(row.get("merchant_domain") or ""), str(row.get("market_country") or ""))
        seen[key] = row.get("checked_at")

    def _staleness(key: Tuple[str, str]) -> Tuple[int, str]:
        checked = seen.get(key)
        # (0, "") sorts a never-checked merchant ahead of every checked one; the ISO string
        # orders the rest oldest-first without comparing an aware datetime to a naive one.
        return (1, checked.isoformat()) if checked is not None else (0, "")

    ordered = sorted(merged.items(), key=lambda item: (_staleness(item[0]), item[0]))

    targets: List[Target] = []
    for (domain, market), variant in ordered[: max(1, int(limit))]:
        if variant is None:
            variant = await _catalog_variant(domain)
        price_minor, currency = await _catalog_price(domain, variant) if variant else (None, None)
        targets.append(Target(domain, market, variant, price_minor, currency))
    return targets


async def _catalog_variant(domain: str) -> Optional[str]:
    try:
        row = await database.fetch_one(_CATALOG_VARIANT_SQL, {"domain": domain})
    except Exception:  # noqa: BLE001
        return None
    return (str(row["variant_id"]).strip() or None) if row is not None else None


async def _catalog_price(domain: str, variant_id: str) -> Tuple[Optional[int], Optional[str]]:
    """OUR indexed unit price, in exact minor units. `(None, None)` whenever it cannot be read
    EXACTLY — a price we had to round is not a price a drift may be computed from."""
    from services.shopify_cart_link_preflight import amount_to_minor

    try:
        row = await database.fetch_one(_CATALOG_PRICE_SQL, {"domain": domain, "variant_id": variant_id})
    except Exception:  # noqa: BLE001
        return None, None
    if row is None:
        return None, None
    currency = str(row["currency"] or "").strip().upper() or None
    if not currency:
        return None, None
    return amount_to_minor(str(row["price"] or "").strip(), currency), currency


# ── the run ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SweepReport:
    """What one run did. COUNTS ONLY — no domains, no variant ids, no rows.

    `skipped_disabled` is 1 when the dial was off. `checked` counts merchant x vantage pairs
    actually fetched; `positive` / `negative` / `unverifiable` partition them by what the store
    said. `errors` is the only count that should page anyone.
    """

    population: int = 0
    checked: int = 0
    positive: int = 0
    negative: int = 0
    unverifiable: int = 0
    written: int = 0
    abandoned_budget: int = 0
    errors: int = 0
    skipped_disabled: int = 0
    duration_ms: int = 0


_COUNTS = (
    "population", "checked", "positive", "negative", "unverifiable", "written",
    "abandoned_budget", "errors", "skipped_disabled",
)

#: Indirected so a test can make the budget elapse without sleeping. MONOTONIC: the budget must
#: survive an NTP step.
_monotonic: Callable[[], float] = time.monotonic

#: The one thing that touches a merchant. Indirected so tests inject a fake fetcher and no test
#: ever opens a socket — the rail's autouse forbid-httpx fixture would fail them if they did.
_preflight = preflight


def _click_id() -> str:
    """A fresh click id per check. It is written as a cart attribute on the merchant's abandoned
    checkout, so it is deliberately a throwaway that ties to no buyer and no campaign."""
    return f"clk_mpx_{uuid.uuid4().hex[:16]}"


async def _check_one(
    target: Target, *, vantage: str, client: Optional[httpx.AsyncClient]
) -> Optional[PreflightResult]:
    """One merchant, one vantage. `buyer=None` ALWAYS: the Reap path carries no buyer data in the
    link, and this sweep has no buyer to carry."""
    return await _preflight(
        target.domain,
        market=target.market,
        variant_id=target.variant_id,
        quantity=1,
        buyer=None,
        click_id=_click_id(),
        check_card=True,
        expected_price_minor=target.expected_price_minor,
        expected_currency=target.expected_currency,
        client=client,
    )


async def run_merchant_purchasability_sweep(*, worker_id: Optional[str] = None) -> SweepReport:
    """One sweep tick. Returns a `SweepReport` and never raises for anything a merchant did.

    It DOES raise `asyncio.CancelledError`, on purpose: `except Exception`, never
    `except BaseException`, so a cancellation keeps travelling and the scheduler's run deadline
    can actually cut a wedged run.
    """
    started = _monotonic()
    counts = {name: 0 for name in _COUNTS}

    def _report() -> SweepReport:
        return SweepReport(duration_ms=int((_monotonic() - started) * 1000), **counts)

    def _budget_spent() -> bool:
        return (_monotonic() - started) >= budget_seconds

    budget_seconds = _env_int(DIALS["budget_seconds"])

    # THE GATE, INSIDE THE JOB. See the module header: with it off this touches no merchant.
    if not is_enabled():
        counts["skipped_disabled"] = 1
        logger.debug("merchant_purchasability_sweep: disabled; no merchant was contacted")
        return _report()

    batch = _env_int(DIALS["batch"])
    pause_s = _env_int(DIALS["pause_ms"]) / 1000.0

    try:
        targets = await load_population(batch)
    except Exception:  # noqa: BLE001 — a population read must not end the run with a traceback
        counts["errors"] += 1
        logger.exception("merchant_purchasability_sweep: the population could not be built")
        return _report()
    counts["population"] = len(targets)

    vantages: List[Tuple[str, Optional[str]]] = [(facts.WORKER_VANTAGE, None)]
    proxy = proxy_url()
    if proxy:
        vantages.append((facts.PROXY_VANTAGE, proxy))

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    for target in targets:
        if _budget_spent():
            # Stop STARTING merchants. The rest are picked up next tick, least-recently-checked
            # first, so nothing is starved — and no merchant is contacted for a run we cannot
            # finish, which on this rail means no abandoned checkout we did not need.
            counts["abandoned_budget"] += 1
            continue
        for vantage, via in vantages:
            # PER-MERCHANT, PER-VANTAGE try/except. One store that hangs, 403s or returns
            # something nobody has classified must not end the batch behind it.
            try:
                client_kwargs: Dict[str, Any] = {"headers": headers, "timeout": REQUEST_TIMEOUT_S}
                if via:
                    client_kwargs["proxy"] = via
                async with httpx.AsyncClient(**client_kwargs) as client:
                    result = await _check_one(target, vantage=vantage, client=client)
            except Exception as exc:  # noqa: BLE001 — never BaseException; see the docstring
                counts["errors"] += 1
                # THE TYPE AND THE VANTAGE, AND NOTHING ELSE. A driver or client exception's
                # message carries the URL, which carries the merchant.
                logger.error(
                    "merchant_purchasability_sweep: a check raised (vantage=%s error_type=%s)",
                    vantage, type(exc).__name__,
                )
                continue
            if result is None:
                counts["errors"] += 1
                continue
            counts["checked"] += 1
            if facts.is_positive(result):
                counts["positive"] += 1
            elif facts.is_negative(result):
                counts["negative"] += 1
            else:
                counts["unverifiable"] += 1
            try:
                written = await facts.record_check(
                    target.domain, target.market, result,
                    vantage=vantage, expected_price_minor=target.expected_price_minor,
                )
            except Exception as exc:  # noqa: BLE001
                counts["errors"] += 1
                logger.error(
                    "merchant_purchasability_sweep: a fact could not be written "
                    "(vantage=%s error_type=%s)", vantage, type(exc).__name__,
                )
                continue
            if written is None:
                counts["errors"] += 1
            else:
                counts["written"] += 1
        if pause_s:
            await asyncio.sleep(pause_s)

    report = _report()
    logger.info("merchant_purchasability_sweep: %s", report)
    return report
