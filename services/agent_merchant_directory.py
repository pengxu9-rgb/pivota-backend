"""The protocol merchant directory: what `GET /agent/v1/merchants?scope=protocol` lists.

A partner agent buys from merchants it reaches through a CHECKOUT PROTOCOL — a payment
partner completing the order on the merchant's own checkout, or a prefilled cart the buyer
finishes on the merchant's site. None of those merchants is integrated through our merchant
portal, so `merchant_onboarding` (the default `available` scope) does not know them. This
module lists them: one row per (merchant domain, market) with at least one route a purchase
can take today.

── WHERE EACH ANSWER COMES FROM ─────────────────────────────────────────────────────────────

Nothing here re-states an eligibility rule. Every fact is the answer of the function that
gates the purchase itself, called once per (domain, market):

  disabled             routes.agent_commerce_reap._refuse_if_merchant_disabled — an operator's
                       "turn this merchant off" row, which stops every purchase lane
  cart_link_eligible   db.tierb_cart_link_eligibility.is_cart_link_eligible — the daily
                       cart-permalink verdict
  card_checkout        db.merchant_purchasability.is_purchasable — a fresh fact that the
                       merchant's checkout offered an on-site card
  human_handoff        db.merchant_purchasability.human_handoff_allowed — a fresh fact that a
                       prefilled cart can be handed to the buyer to finish
  variant_lane         db.reap_agentic_ledger.list_enabled_merchant_markets — the merchants an
                       operator enabled for the payment partner's variant lane

The POPULATION (which pairs to ask about) is the union of the three tables those functions
read. It is a superset by construction; a pair with no positive answer is not listed.

── THE TWO ROUTES ───────────────────────────────────────────────────────────────────────────

  `agentic_checkout`     a payment partner places the order for the buyer, who enters a card
                         and approves on the partner's hosted page. Listed only when the
                         purchase route would admit THIS agent at THIS merchant and market:
                         the rail is live and creating, the market is one the route prices
                         (`_MARKET_CURRENCY`), the pilot scope (if any) names the agent,
                         merchant and market, the merchant is not disabled, a fresh card fact
                         exists when purchasability enforcement is on, and the merchant is on
                         a lane (the variant lane, or the cart-link lane while it is armed and
                         the merchant's cart-link verdict is fresh).
  `storefront_checkout`  the buyer finishes on the merchant's own checkout from a prefilled
                         cart. Listed when the handoff fact is fresh.

Both are MERCHANT-level answers. A purchase still checks the product (its catalog row, its
variant, our price), so a listed merchant can refuse a particular item.

── WHAT IS NEVER LISTED ─────────────────────────────────────────────────────────────────────

  * a merchant an operator disabled, in that market;
  * a test or demo rig: the store domains of every id `services/test_merchant_policy`
    excludes (its baked-in ids, `PIVOTA_TEST_MERCHANT_IDS`, the demo-domain resolver), read
    from `merchant_stores` and `merchant_onboarding`, plus any host under the demo prefix;
  * the store domain of a merchant whose portal onboarding was `rejected`;
  * a domain with an active `domain` quarantine (`services/source_quarantine`);
  * a `*.myshopify.com` host — a real merchant is listed under its storefront domain, and a
    myshopify host in these tables is a development store;
  * a wholesale storefront (first label `wholesale`): it does not sell to a consumer;
  * a domain in `AGENT_MERCHANT_DIRECTORY_EXCLUDED_DOMAINS` (comma-separated), the operator's
    lever for anything else.

── FAILURE ──────────────────────────────────────────────────────────────────────────────────

The exclusion reads (rig and rejected store domains, quarantines) RAISE: without them the
directory cannot tell a rig from a merchant, so the build fails. The caller then gets the last
clean build if it is younger than STALE_SERVE_SECONDS (it had every exclusion applied), else the
route answers 500 rather than a list.
A per-pair fact read that fails answers False for that pair (closed). That includes the two
purchasability readers, which swallow their own database errors: their ERROR log records are
counted while the directory is built. A build with ANY failure is served for that call and
NEVER cached, so a blip cannot pin a partial list for a cache window.

── COST ─────────────────────────────────────────────────────────────────────────────────────

Four small reads per pair, sequentially (the driver shares one connection per task). The
per-pair facts are agent-independent and change daily at most, so a clean build is cached
in-process for `CACHE_TTL_SECONDS`, and concurrent callers on one event loop share one build,
which runs in its own task (a cancelled caller never cancels it) and is waited on for at most
`BUILD_WAIT_SECONDS`.
The route composition (dials, pilot scope, allowlist, market) runs per call, after the cache.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from db.database import database

logger = logging.getLogger(__name__)

ROUTE_AGENTIC = "agentic_checkout"
ROUTE_STOREFRONT = "storefront_checkout"

EXCLUDED_DOMAINS_ENV = "AGENT_MERCHANT_DIRECTORY_EXCLUDED_DOMAINS"
CACHE_TTL_SECONDS = 300.0
#: How long any caller waits for a build. A build normally takes a second or two; one that is
#: stuck (a gate that never releases) must not hold every request open. On timeout the caller gets
#: the last clean build while it is younger than STALE_SERVE_SECONDS, else `DirectoryUnavailable`
#: (the route's 503). A stuck build is not retried while it runs, so without that age bound one
#: stuck build would serve an ever older directory until the process restarts.
BUILD_WAIT_SECONDS = 15.0
#: After a FAILED or TIMED-OUT build, the last clean build is served while it is younger than this.
#: It had every exclusion applied when it was built, so serving it is safer than serving nothing.
STALE_SERVE_SECONDS = 1800.0


class DirectoryUnavailable(RuntimeError):
    """No clean directory to serve, and the current build did not finish in time."""

#: The population's two table reads (the variant lane's is the ledger's own reader). Module-level
#: constants called directly, so tests/test_repo_sql_prepare_postgres.py plans them.
_TIERB_PAIRS_SQL = """
SELECT DISTINCT shop_domain AS merchant_domain, market AS market_country
  FROM tierb_cart_link_eligibility
"""

_PURCHASABILITY_PAIRS_SQL = """
SELECT DISTINCT merchant_domain, market_country
  FROM merchant_purchasability
"""

#: The store domains a portal merchant id is known under. Both tables are small (connected
#: stores and onboarding rows); the rig and rejected ids are matched in Python.
_STORE_DOMAINS_SQL = """
SELECT merchant_id, domain FROM merchant_stores
"""

_ONBOARDING_DOMAINS_SQL = """
SELECT merchant_id, status, store_url, website FROM merchant_onboarding
"""


@dataclass(frozen=True)
class MerchantFacts:
    """One (domain, market)'s answers, as the purchase gates give them. Agent-independent."""

    merchant_domain: str
    market: str
    disabled: bool
    variant_lane: bool
    cart_link_eligible: bool
    card_checkout: bool
    human_handoff: bool


_cache: Optional[Tuple[float, Tuple[MerchantFacts, ...]]] = None
_inflight: Optional["asyncio.Task[Tuple[MerchantFacts, ...]]"] = None


def reset_cache() -> None:
    global _cache, _inflight
    if _inflight is not None and not _inflight.done():
        try:
            _inflight.cancel()
        except RuntimeError:  # its event loop is already closed
            pass
    _cache = None
    _inflight = None


def _canonical_domain(value: Any) -> Optional[str]:
    from services.tierb_cart_link_merchants import canonical_merchant_domain

    try:
        domain = canonical_merchant_domain(value)
    except (ValueError, TypeError):
        return None
    return domain or None


def _host_of(value: Any) -> Optional[str]:
    """A stored URL or host (`https://www.Brand.com/shop`) as a canonical domain."""
    from db.merchant_purchasability import normalize_domain

    text = str(value or "").strip()
    return _canonical_domain(normalize_domain(text)) if text else None


def _market(value: Any) -> Optional[str]:
    from db.merchant_purchasability import normalize_market

    return normalize_market(value)


def _pair(row: Any) -> Optional[Tuple[str, str]]:
    r = dict(row)
    domain, market = _canonical_domain(r.get("merchant_domain")), _market(r.get("market_country"))
    return (domain, market) if domain and market else None


def excluded_domains(env: Optional[Dict[str, str]] = None) -> FrozenSet[str]:
    raw = (env if env is not None else os.environ).get(EXCLUDED_DOMAINS_ENV) or ""
    out = set()
    for part in raw.split(","):
        domain = _canonical_domain(part.strip()) if part.strip() else None
        if domain:
            out.add(domain)
    return frozenset(out)


def is_listable_domain(domain: str, *, excluded: Iterable[str] = ()) -> bool:
    """The structural exclusions in the module header, plus a resolved exclusion set."""
    from services.test_merchant_policy import DEMO_DOMAIN_PREFIX

    if domain == "myshopify.com" or domain.endswith(".myshopify.com"):
        return False
    if domain.startswith(DEMO_DOMAIN_PREFIX):
        return False
    if domain.split(".", 1)[0] == "wholesale":
        return False
    return domain not in set(excluded)


async def _rig_and_rejected_domains() -> Set[str]:
    """Store domains of every test/demo rig id and every rejected portal merchant. RAISES on a
    failed read (see FAILURE in the header); the rig ID set itself fails soft to static ids
    inside `get_excluded_merchant_ids`."""
    from services.test_merchant_policy import get_excluded_merchant_ids

    rig_ids = await get_excluded_merchant_ids(database)
    out: Set[str] = set()
    for row in await database.fetch_all(_STORE_DOMAINS_SQL):
        r = dict(row)
        if str(r.get("merchant_id") or "").strip() in rig_ids:
            domain = _host_of(r.get("domain"))
            if domain:
                out.add(domain)
    for row in await database.fetch_all(_ONBOARDING_DOMAINS_SQL):
        r = dict(row)
        mid = str(r.get("merchant_id") or "").strip()
        if mid in rig_ids or str(r.get("status") or "").strip().lower() == "rejected":
            for column in ("store_url", "website"):
                domain = _host_of(r.get(column))
                if domain:
                    out.add(domain)
    return out


async def _population(failures: List[str]) -> Tuple[Set[Tuple[str, str]], Set[Tuple[str, str]]]:
    """(every pair to ask about, the variant lane's enabled pairs). A table that cannot be read
    contributes nothing and is a failure; if NONE can be read, that is an error, not an empty
    directory."""
    from db.reap_agentic_ledger import list_enabled_merchant_markets

    pairs: Set[Tuple[str, str]] = set()
    variant_lane: Set[Tuple[str, str]] = set()
    readable = 0
    try:
        tierb_rows = await database.fetch_all(_TIERB_PAIRS_SQL)
        readable += 1
    except Exception:  # noqa: BLE001
        failures.append("population:tierb")
        tierb_rows = []
    try:
        purchasability_rows = await database.fetch_all(_PURCHASABILITY_PAIRS_SQL)
        readable += 1
    except Exception:  # noqa: BLE001
        failures.append("population:purchasability")
        purchasability_rows = []
    try:
        enabled_rows = await list_enabled_merchant_markets(strict=True)
        readable += 1
    except Exception:  # noqa: BLE001
        failures.append("population:variant_lane")
        enabled_rows = []
    if readable == 0:
        raise RuntimeError("no merchant directory source is readable")
    for row in list(tierb_rows) + list(purchasability_rows):
        pair = _pair(row)
        if pair:
            pairs.add(pair)
    for row in enabled_rows:
        pair = _pair(row)
        if pair:
            variant_lane.add(pair)
    return pairs | variant_lane, variant_lane


async def _facts_for(domain: str, market: str, variant_lane: bool, failures: List[str]) -> MerchantFacts:
    import db.merchant_purchasability as purchasability
    import db.tierb_cart_link_eligibility as tierb
    import routes.agent_commerce_reap as reap_routes
    import services.reap_agentic_purchase as reap_svc

    try:
        await reap_routes._refuse_if_merchant_disabled(merchant_domain=domain, market_country=market)
        disabled = False
    except reap_svc.PurchaseRefused:
        disabled = True
    except Exception:  # noqa: BLE001 - unknown means closed: never list a merchant we cannot clear
        failures.append("disabled")
        disabled = True
    try:
        cart_link = await tierb.is_cart_link_eligible(domain, market)
    except Exception:  # noqa: BLE001
        failures.append("cart_link")
        cart_link = False
    # Both answer False on a database error and log it at ERROR; `_ErrorCounter` sees that.
    card = await purchasability.is_purchasable(domain, market)
    handoff = await purchasability.human_handoff_allowed(domain, market)
    return MerchantFacts(
        merchant_domain=domain,
        market=market,
        disabled=disabled,
        variant_lane=variant_lane,
        cart_link_eligible=bool(cart_link),
        card_checkout=bool(card),
        human_handoff=bool(handoff),
    )


class _ErrorCounter(logging.Handler):
    """Counts ERROR records on one logger while attached."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


async def _build() -> Tuple[Tuple[MerchantFacts, ...], List[str]]:
    """The directory, and every failure that degraded it (empty for a clean build)."""
    import db.merchant_purchasability as purchasability
    from services.source_quarantine import load_active_quarantines, quarantine_matches_source

    failures: List[str] = []
    pairs, variant_lane = await _population(failures)
    excluded = set(excluded_domains()) | await _rig_and_rejected_domains()
    quarantines = await load_active_quarantines(db=database)

    def _quarantined(domain: str) -> bool:
        return any(
            quarantine_matches_source(
                q, domain=domain, merchant_id=None, platform=None, source_system=None, source_ref=None
            )
            for q in quarantines
        )

    counter = _ErrorCounter()
    purchasability.logger.addHandler(counter)
    try:
        facts = []
        for domain, market in sorted(pairs, key=lambda p: (p[1], p[0])):
            if not is_listable_domain(domain, excluded=excluded) or _quarantined(domain):
                continue
            facts.append(await _facts_for(domain, market, (domain, market) in variant_lane, failures))
    finally:
        purchasability.logger.removeHandler(counter)
    failures.extend(["purchasability"] * counter.count)
    return tuple(facts), failures


async def _build_and_cache(clock: float) -> Tuple[MerchantFacts, ...]:
    """One build, run as its OWN task: it caches a clean result itself, so the cache is written
    even when every caller that was waiting for it has gone."""
    global _cache
    result, failures = await _build()
    if failures:
        logger.warning(
            "agent_merchant_directory: %d fact reads failed and answered closed (%s); not cached",
            len(failures), ",".join(sorted(set(failures))),
        )
    else:
        _cache = (clock, result)
    return result


def _build_done(task: "asyncio.Task[Tuple[MerchantFacts, ...]]") -> None:
    global _inflight
    if _inflight is task:
        _inflight = None
    if not task.cancelled():
        task.exception()  # retrieved: a build whose callers all left must not warn


async def merchant_facts(*, now: Optional[float] = None) -> Tuple[MerchantFacts, ...]:
    """Every listable (domain, market) and its facts, sorted by market then domain.

    A clean build is cached for CACHE_TTL_SECONDS; a degraded one is returned and not cached.
    Concurrent callers on one event loop share one build, which runs in its own task: a caller
    that is cancelled (a client disconnect) never cancels it, and every caller's wait is bounded
    by BUILD_WAIT_SECONDS. A failed or timed-out build falls back to the last clean one while it is
    younger than STALE_SERVE_SECONDS."""
    global _inflight
    clock = time.monotonic() if now is None else now
    if _cache is not None and clock - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]

    loop = asyncio.get_running_loop()
    task = _inflight
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(_build_and_cache(clock))
        _inflight = task
        task.add_done_callback(_build_done)
    try:
        return await asyncio.wait_for(asyncio.shield(task), BUILD_WAIT_SECONDS)
    except asyncio.TimeoutError:
        if _cache is not None and clock - _cache[0] < STALE_SERVE_SECONDS:
            logger.warning("agent_merchant_directory: build still running; serving the last clean build")
            return _cache[1]
        raise DirectoryUnavailable("the merchant directory is still being built") from None
    except Exception:
        if _cache is not None and clock - _cache[0] < STALE_SERVE_SECONDS:
            logger.warning(
                "agent_merchant_directory: build failed; serving the last clean build", exc_info=True
            )
            return _cache[1]
        raise


@dataclass(frozen=True)
class RailPosture:
    """The purchase route's dials, read once per call."""

    agentic_live: bool
    cart_link_lane: bool
    enforce_card_fact: bool
    #: None = unrestricted; {} = an invalid scope (admits nothing); else the parsed scope.
    pilot_scope: Optional[Dict[str, Any]]
    #: Markets the purchase route can price (it refuses `row_currency_mismatch` elsewhere).
    agentic_markets: FrozenSet[str]


def rail_posture() -> RailPosture:
    import db.merchant_purchasability as purchasability
    import routes.agent_commerce_reap as reap_routes
    import services.reap_agentic_purchase as reap_svc

    live = bool(reap_routes._rail_is_live() and reap_svc.is_create_enabled())
    return RailPosture(
        agentic_live=live,
        cart_link_lane=bool(reap_svc.is_cart_link_enabled()),
        enforce_card_fact=bool(purchasability.is_enforcement_enabled()),
        pilot_scope=reap_svc.pilot_admission_scope() if live else {},
        agentic_markets=frozenset(reap_routes._MARKET_CURRENCY),
    )


def _pilot_admits(scope: Optional[Dict[str, Any]], *, agent_id: str, domain: str, market: str) -> bool:
    if scope is None:
        return True
    if not scope:
        return False
    return (
        agent_id in (scope.get("agent_ids") or ())
        and domain in (scope.get("merchant_domains") or ())
        and market in (scope.get("markets") or ())
    )


def routes_for(facts: MerchantFacts, posture: RailPosture, *, agent_id: str) -> List[str]:
    """The routes a purchase by `agent_id` can take at this merchant and market, in order."""
    if facts.disabled:
        return []
    out: List[str] = []
    on_a_lane = facts.variant_lane or (posture.cart_link_lane and facts.cart_link_eligible)
    if (
        posture.agentic_live
        and facts.market in posture.agentic_markets
        and on_a_lane
        and (facts.card_checkout or not posture.enforce_card_fact)
        and _pilot_admits(posture.pilot_scope, agent_id=agent_id, domain=facts.merchant_domain, market=facts.market)
    ):
        out.append(ROUTE_AGENTIC)
    if facts.human_handoff:
        out.append(ROUTE_STOREFRONT)
    return out


def row(facts: MerchantFacts, routes: Sequence[str]) -> Dict[str, Any]:
    return {
        "merchant_domain": facts.merchant_domain,
        "market": facts.market,
        "store_url": f"https://{facts.merchant_domain}",
        "status": "active",
        "checkout_routes": list(routes),
        # The MERCHANT's own checkout offered an on-site card when last checked. Not a Pivota
        # payment capability.
        "merchant_checkout_accepts_cards": facts.card_checkout,
    }


async def list_for_agent(
    *, agent_id: str, allowed_merchants: Optional[Sequence[str]], market: Optional[str] = None
) -> List[Dict[str, Any]]:
    """The directory as `agent_id` sees it: listable merchants with at least one route.

    `allowed_merchants` is the agent's allowlist, as `AgentContext.can_access_merchant` reads it:
    None means every merchant; a list (even an empty one) means only those, matched against the
    merchant DOMAIN here (portal merchant ids name no protocol merchant). The purchase route
    checks merchant ids instead, so an agent restricted by id lists nothing here.
    """
    if allowed_merchants is not None:
        allowed = {d for d in (_canonical_domain(m) for m in allowed_merchants) if d}
        if not allowed:
            return []
    else:
        allowed = None
    posture = rail_posture()
    out = []
    for facts in await merchant_facts():
        if market is not None and facts.market != market:
            continue
        if allowed is not None and facts.merchant_domain not in allowed:
            continue
        routes = routes_for(facts, posture, agent_id=agent_id)
        if routes:
            out.append(row(facts, routes))
    return out
