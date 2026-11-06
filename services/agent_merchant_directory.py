"""The protocol merchant directory: what `GET /agent/v1/merchants` lists by default.

A partner agent buys from merchants it reaches through a CHECKOUT PROTOCOL — a payment
partner completing the order on the merchant's own checkout, or a prefilled cart the buyer
finishes on the merchant's site. None of those merchants is integrated through our merchant
portal, so `merchant_onboarding` (the `native` scope) does not know them. This module lists
them: one row per (merchant domain, market) with at least one route a purchase can take today.

── WHERE EACH ANSWER COMES FROM ─────────────────────────────────────────────────────────────

Nothing here re-states an eligibility rule. Every fact is the answer of the function that
gates the purchase itself, called once per (domain, market):

  disabled             routes.agent_commerce_reap._refuse_if_merchant_disabled — an operator's
                       "turn this merchant off" row, which stops every purchase lane
  cart_link_eligible   db.tierb_cart_link_eligibility.is_cart_link_eligible — the daily
                       cart-permalink verdict
  card_checkout        db.merchant_purchasability.is_purchasable — a fresh fact that the
                       checkout offered an on-site card (what an unattended card payment needs)
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
                         the rail is live and creating, the pilot scope (if any) names the
                         agent, merchant and market, the merchant is not disabled, a fresh card
                         fact exists when purchasability enforcement is on, and the merchant is
                         on a lane (the variant lane, or the cart-link lane while it is armed
                         and the merchant's cart-link verdict is fresh).
  `storefront_checkout`  the buyer finishes on the merchant's own checkout from a prefilled
                         cart. Listed when the handoff fact is fresh.

Both are MERCHANT-level answers. A purchase still checks the product (its catalog row, its
variant, our price), so a listed merchant can refuse a particular item.

── WHAT IS NEVER LISTED ─────────────────────────────────────────────────────────────────────

  * a merchant an operator disabled, in that market;
  * a `*.myshopify.com` host — a real merchant is listed under its storefront domain, and a
    myshopify host in these tables is a development store (our own demo store is one);
  * a host under the review-demo prefix (`services/test_merchant_policy.DEMO_DOMAIN_PREFIX`);
  * a wholesale storefront (first label `wholesale`): it does not sell to a consumer;
  * a domain in `AGENT_MERCHANT_DIRECTORY_EXCLUDED_DOMAINS` (comma-separated), the operator's
    lever for anything else.

── COST ─────────────────────────────────────────────────────────────────────────────────────

Four small reads per pair, sequentially (the driver shares one connection per task). The
per-pair facts are agent-independent and change daily at most, so they are cached in-process
for `CACHE_TTL_SECONDS`; the route composition (dials, pilot scope, allowlist) runs per call.
A reader that fails answers False for that pair (closed), and is counted in one WARNING.
"""

from __future__ import annotations

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


def reset_cache() -> None:
    global _cache
    _cache = None


def _canonical_domain(value: Any) -> Optional[str]:
    from services.tierb_cart_link_merchants import canonical_merchant_domain

    try:
        domain = canonical_merchant_domain(value)
    except (ValueError, TypeError):
        return None
    return domain or None


def _market(value: Any) -> Optional[str]:
    from db.merchant_purchasability import normalize_market

    return normalize_market(value)


def excluded_domains(env: Optional[Dict[str, str]] = None) -> FrozenSet[str]:
    raw = (env if env is not None else os.environ).get(EXCLUDED_DOMAINS_ENV) or ""
    out = set()
    for part in raw.split(","):
        domain = _canonical_domain(part.strip()) if part.strip() else None
        if domain:
            out.add(domain)
    return frozenset(out)


def is_listable_domain(domain: str, *, excluded: Iterable[str] = ()) -> bool:
    """The structural exclusions in the module header, plus the operator's list."""
    from services.test_merchant_policy import DEMO_DOMAIN_PREFIX

    if domain == "myshopify.com" or domain.endswith(".myshopify.com"):
        return False
    if domain.startswith(DEMO_DOMAIN_PREFIX):
        return False
    if domain.split(".", 1)[0] == "wholesale":
        return False
    return domain not in set(excluded)


async def _population() -> Tuple[Set[Tuple[str, str]], Set[Tuple[str, str]]]:
    """(every pair to ask about, the variant lane's enabled pairs). A table that cannot be read
    contributes nothing; if NONE can be read, that is an error, not an empty directory."""
    from db.reap_agentic_ledger import list_enabled_merchant_markets

    pairs: Set[Tuple[str, str]] = set()
    readable = 0
    sources = []
    try:
        sources.append(await database.fetch_all(_TIERB_PAIRS_SQL))
    except Exception:  # noqa: BLE001
        logger.warning("agent_merchant_directory: tierb_cart_link_eligibility unreadable; it contributes nothing")
    try:
        sources.append(await database.fetch_all(_PURCHASABILITY_PAIRS_SQL))
    except Exception:  # noqa: BLE001
        logger.warning("agent_merchant_directory: merchant_purchasability unreadable; it contributes nothing")
    for rows in sources:
        readable += 1
        for row in rows:
            r = dict(row)
            domain, market = _canonical_domain(r.get("merchant_domain")), _market(r.get("market_country"))
            if domain and market:
                pairs.add((domain, market))

    variant_lane: Set[Tuple[str, str]] = set()
    try:
        enabled = await list_enabled_merchant_markets(strict=True)
        readable += 1
    except Exception:  # noqa: BLE001
        logger.warning("agent_merchant_directory: reap_agentic_eligibility unreadable; it contributes nothing")
        enabled = []
    for r in enabled:
        domain, market = _canonical_domain(r.get("merchant_domain")), _market(r.get("market_country"))
        if domain and market:
            variant_lane.add((domain, market))
    pairs |= variant_lane

    if readable == 0:
        raise RuntimeError("no merchant directory source is readable")
    return pairs, variant_lane


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
    # Both fail closed internally (False on a database error).
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


async def merchant_facts(*, now: Optional[float] = None) -> Tuple[MerchantFacts, ...]:
    """Every listable (domain, market) and its facts, sorted by market then domain. Cached."""
    global _cache
    clock = time.monotonic() if now is None else now
    if _cache is not None and clock - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]
    pairs, variant_lane = await _population()
    excluded = excluded_domains()
    failures: List[str] = []
    facts = []
    for domain, market in sorted(pairs, key=lambda p: (p[1], p[0])):
        if not is_listable_domain(domain, excluded=excluded):
            continue
        facts.append(await _facts_for(domain, market, (domain, market) in variant_lane, failures))
    if failures:
        logger.warning(
            "agent_merchant_directory: %d fact reads failed and answered closed (%s)",
            len(failures), ",".join(sorted(set(failures))),
        )
    result = tuple(facts)
    _cache = (clock, result)
    return result


@dataclass(frozen=True)
class RailPosture:
    """The purchase route's dials, read once per call."""

    agentic_live: bool
    cart_link_lane: bool
    enforce_card_fact: bool
    #: None = unrestricted; {} = an invalid scope (admits nothing); else the parsed scope.
    pilot_scope: Optional[Dict[str, Any]]


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
        "card_checkout_verified": facts.card_checkout,
    }


async def list_for_agent(
    *, agent_id: str, allowed_merchants: Optional[Sequence[str]], market: Optional[str] = None
) -> List[Dict[str, Any]]:
    """The directory as `agent_id` sees it: listable merchants with at least one route.

    `allowed_merchants` is the agent's allowlist, as `AgentContext.can_access_merchant` reads it:
    None means every merchant; a list (even an empty one) means only those, matched against the
    merchant DOMAIN here (portal merchant ids name no protocol merchant).
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
