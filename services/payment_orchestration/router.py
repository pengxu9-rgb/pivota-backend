"""Which payment rail a new purchase goes to (payment orchestration P2c).

Two rails place merchant orders for a buyer (executor `rail_managed`): Reap, and the A2A token rail.
This module decides between them for ONE create request, before anything is written and before any
rail is called. It reads configuration and its arguments only: no database, no rail, no clock. The
route (routes/agent_commerce_purchases.py) reads the worker's heartbeat, passes it in, and acts on
the answer.

── THE RULES ────────────────────────────────────────────────────────────────────────────────

1. HARD FILTERS for the A2A rail, each a named reason when it fails:
     a2a_rail_disabled        the rail's client is off (A2A_RAIL_ENABLED) or creating is off
                              (A2A_RAIL_CREATE_ENABLED)
     agent_not_in_pilot       the calling agent is not on A2A_RAIL_PILOT_AGENT_IDS
     a2a_worker_unavailable   no worker tick has started within 15 minutes (the route reads the
                              worker's heartbeat, db/a2a_rail_ledger.worker_is_alive, for a pilot
                              agent only): a purchase opened now would never advance, expire or have
                              its contact purged. After the pilot check, so an agent outside the pilot
                              is told that, not that the worker is down (the route never reads the
                              heartbeat for it and passes False)
     item_source_unsupported  the rail takes a merchant cart link only (`item_source=cart_link`)
     offer_code_unsupported   the rail has no field for the buyer's offer code
     merchant_not_proven      (merchant, market) is not on A2A_RAIL_MERCHANTS: a merchant is routable
                              to this rail only after a live order there has been proven
     currency_unsupported     the shown currency is not one the rail's token step can carry
                              (a2a_rail.TOKEN_CURRENCIES: the rail's amount has no currency field)
     amount_limit_unset       A2A_RAIL_MAX_TOTAL_MINOR names no cap at all: no cap, no rail
     agent_run_condition_unmet  only while PARTNER_AGENT_RUN_CONDITION_RAILS names `a2a`: the merchant
                              does not meet ADR-030 D11's run condition for this rail's checkout agent
                              (no fresh stored "publishes an agentic checkout" fact, or an open
                              challenge or block on this rail). The route reads both records
                              (services/payment_orchestration/agent_run_condition.py) only for a request
                              that passes every filter above, and passes the answer in
   The amount itself is checked by the route once OUR catalog has priced the item (`amount_ok`),
   because the price is never the caller's. Caps are PER CURRENCY (`USD:10000,SGD:13500`): a minor
   unit is not one risk bound across currencies, and a currency with no cap is refused.
2. AN EXPLICIT CHOICE WINS, and is never silently changed. `payment_preference.rail` = "a2a" on a
   request the A2A rail cannot take is REFUSED with that rail's reason, not sent to Reap; "reap" goes
   to Reap, whose route applies its own gates.
3. NO CHOICE: the A2A rail when it passes every filter, else Reap. Every rail not taken is recorded
   with its reason (`dropped`), so the purchase can say why it is where it is.
4. NO FALLBACK AFTER ROUTING. A request routed to the A2A rail that the A2A create then refuses (pricing, the
   Tier B verdict, the cap, the buyer's details) is refused with that reason, never retried on Reap.
5. NO FAILOVER AFTER THE FIRST WRITE. This runs once, before the purchase exists. Once a purchase is
   opened on a rail it stays there: it finishes or fails visibly, and a new purchase is a new
   request. A retried request with the same idempotency key replays the ORIGINAL purchase on its
   original rail, whatever this module would answer today (the route checks the key first).

Everything is off by default: with no configuration every request goes to Reap, exactly as before.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Set, Tuple

from services.payment_orchestration import a2a_rail

logger = logging.getLogger(__name__)

PAYMENT_ORCHESTRATOR_ENABLED_ENV = "PAYMENT_ORCHESTRATOR_ENABLED"
A2A_RAIL_CREATE_ENABLED_ENV = "A2A_RAIL_CREATE_ENABLED"
A2A_RAIL_PILOT_AGENT_IDS_ENV = "A2A_RAIL_PILOT_AGENT_IDS"
A2A_RAIL_MERCHANTS_ENV = "A2A_RAIL_MERCHANTS"
A2A_RAIL_MAX_TOTAL_MINOR_ENV = "A2A_RAIL_MAX_TOTAL_MINOR"
#: The rails whose checkout agent must meet ADR-030 D11's run condition: a comma-separated list of
#: names from RAILS (`a2a`, `reap`, `reap,a2a`). Unset or empty: no rail, and nothing is read.
PARTNER_AGENT_RUN_CONDITION_RAILS_ENV = "PARTNER_AGENT_RUN_CONDITION_RAILS"

RAILS = ("reap", "a2a")
#: The name of THE RULES above. It is stored with every decision (`decision_record`), so a decision made
#: under a later rule set can be told from one made under this one. Change it when a rule changes.
POLICY_VERSION = "env_v1"
#: The same rules plus `agent_run_condition_unmet`: the name stored while the token rail's run
#: condition is enforced (`policy_version`).
POLICY_VERSION_RUN_CONDITION = "env_v2"
_TRUTHY = frozenset({"1", "true", "on", "yes"})


def _on(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TRUTHY


def is_enabled() -> bool:
    """The rail-neutral create route. Off: it answers 404 to everything, and callers keep using
    the Reap route directly."""
    return _on(PAYMENT_ORCHESTRATOR_ENABLED_ENV)


def _csv(name: str) -> FrozenSet[str]:
    return frozenset(p.strip() for p in (os.getenv(name) or "").split(",") if p.strip())


def pilot_agents() -> FrozenSet[str]:
    return _csv(A2A_RAIL_PILOT_AGENT_IDS_ENV)


#: The setting's raw values already warned about, so a misspelt value is logged once per process,
#: and again only when the value changes.
_RUN_CONDITION_WARNED: Set[str] = set()


def run_condition_rails() -> FrozenSet[str]:
    """The rails on PARTNER_AGENT_RUN_CONDITION_RAILS. An entry that is not a rail name is ignored
    (it enforces nothing), never widened to every rail, and logged as a WARNING: a misspelt list
    (`reap;a2a`, `reap a2a`, `a2a-rail`) would otherwise leave the filter silently off."""
    names = frozenset(name.lower() for name in _csv(PARTNER_AGENT_RUN_CONDITION_RAILS_ENV))
    rails = names & frozenset(RAILS)
    unknown = names - rails
    if unknown:
        raw = os.getenv(PARTNER_AGENT_RUN_CONDITION_RAILS_ENV) or ""
        if raw not in _RUN_CONDITION_WARNED:
            _RUN_CONDITION_WARNED.add(raw)
            logger.warning(
                "router: %s has %d entr%s that %s not a rail name (rails are %s, comma-separated); "
                "enforced on: %s", PARTNER_AGENT_RUN_CONDITION_RAILS_ENV, len(unknown),
                "y" if len(unknown) == 1 else "ies", "is" if len(unknown) == 1 else "are",
                ",".join(RAILS), ",".join(sorted(rails)) or "no rail")
    return rails


def run_condition_enforced(rail: str) -> bool:
    return rail in run_condition_rails()


def policy_version() -> str:
    """The rule set in force now: `env_v2` while the token rail's run condition is enforced, else
    `env_v1`. The Reap route's own filter is not a routing rule and does not change it."""
    return POLICY_VERSION_RUN_CONDITION if run_condition_enforced("a2a") else POLICY_VERSION


def proven_merchants() -> FrozenSet[Tuple[str, str]]:
    """(canonical domain, MARKET) pairs, from `domain:MARKET` entries, the domain folded by the one
    merchant-host canonicaliser (`www.` removed), as the route folds the request's. A malformed entry
    is ignored (it proves nothing), never widened."""
    from services.tierb_cart_link_merchants import canonical_merchant_domain

    out = set()
    for entry in _csv(A2A_RAIL_MERCHANTS_ENV):
        domain, sep, market = entry.rpartition(":")
        market = market.strip().upper()
        if not (sep and len(market) == 2 and market.isascii() and market.isalpha()):
            continue
        try:
            out.add((canonical_merchant_domain(domain.strip()), market))
        except ValueError:
            continue
    return frozenset(out)


_CAP_RE = re.compile(r"^([A-Z]{3}):([1-9][0-9]{0,11})$")


def max_total_minor() -> Dict[str, int]:
    """Currency -> cap in minor units, from `CUR:amount` entries. Malformed entries are ignored."""
    caps: Dict[str, int] = {}
    for entry in _csv(A2A_RAIL_MAX_TOTAL_MINOR_ENV):
        match = _CAP_RE.match(entry.replace(" ", "").upper())
        if match:
            caps[match[1]] = int(match[2])
    return caps


def amount_ok(total_minor: int, currency: str) -> bool:
    """True when `total_minor` (OUR price x quantity) is within the A2A rail's cap for `currency`.
    No cap for that currency: False."""
    cap = max_total_minor().get(str(currency or "").upper())
    return cap is not None and type(total_minor) is int and 0 < total_minor <= cap


@dataclass(frozen=True)
class RailChoice:
    """The decision. `rail` is None only when an explicit choice was refused (`refusal`)."""

    rail: Optional[str]
    #: Who decided: "buyer_selected_rail" (an explicit choice) or "orchestrator_selected_rail".
    basis: str
    #: Every rail not taken, with the named reason it was not.
    dropped: Dict[str, str] = field(default_factory=dict)
    #: For an explicit choice that cannot be honoured: the reason, and nothing was routed.
    refusal: Optional[str] = None


def decision_record(choice: RailChoice, *, market_country: str) -> Dict[str, object]:
    """What the purchase ledger stores for a decision made here (db/agent_purchase_ledger.py, the
    parent's `routing_plan`): who decided, every rail not taken with its named reason, the rule set, and
    the buyer's market. The market is here because the A2A rail's own row keeps none, and "which rail,
    for which merchant, in which market" is the question a routing decision is later judged by.
    The keys are `ensure_reap_parent` / `ensure_a2a_parent`'s keyword arguments.

    NOT DURABLE. The parent row is written after the purchase commits, best-effort, and the first write
    wins: when a heal or the backfill writes it first, the plan says `basis="backfill"` and this record
    is lost. It is evidence for a report, never a fact a later decision may depend on."""
    return {
        "basis": choice.basis, "dropped": dict(choice.dropped), "policy": policy_version(),
        "market": str(market_country or "").upper(),
    }


def a2a_create_dials_on() -> bool:
    """Both of the rail's create dials. The route reads the worker's heartbeat only when they are on."""
    return a2a_rail.is_enabled() and _on(A2A_RAIL_CREATE_ENABLED_ENV)


def a2a_unavailable_reason(
    *, agent_id: str, merchant_domain: str, market_country: str, item_source: str, currency: str,
    worker_alive: bool, has_offer_code: bool = False, agent_may_run: bool = True,
) -> Optional[str]:
    """None when the A2A rail may take this request (before pricing); else the named reason.
    `agent_may_run` is the route's run-condition read (True when it did not read: the filter is off,
    or an earlier reason answers first)."""
    if not a2a_create_dials_on():
        return "a2a_rail_disabled"
    if str(agent_id or "") not in pilot_agents():
        return "agent_not_in_pilot"
    if worker_alive is not True:
        return "a2a_worker_unavailable"
    if item_source != "cart_link":
        return "item_source_unsupported"
    if has_offer_code:
        return "offer_code_unsupported"
    if (str(merchant_domain or "").lower(), str(market_country or "").upper()) not in proven_merchants():
        return "merchant_not_proven"
    if str(currency or "").upper() not in a2a_rail.TOKEN_CURRENCIES:
        return "currency_unsupported"
    if not max_total_minor():
        return "amount_limit_unset"
    if agent_may_run is not True and run_condition_enforced("a2a"):
        return "agent_run_condition_unmet"
    return None


def choose_rail(
    *, agent_id: str, merchant_domain: str, market_country: str, item_source: str, currency: str,
    worker_alive: bool, has_offer_code: bool = False, preferred: Optional[str] = None,
    agent_may_run: bool = True,
) -> RailChoice:
    """See the module docstring. `merchant_domain` is the canonical merchant (lower case, no `www.`),
    `currency` the one the buyer was shown, `worker_alive` the A2A worker's heartbeat and
    `agent_may_run` the run-condition read (both the route's reads)."""
    a2a_reason = a2a_unavailable_reason(
        agent_id=agent_id, merchant_domain=merchant_domain, market_country=market_country, item_source=item_source,
        currency=currency, worker_alive=worker_alive, has_offer_code=has_offer_code, agent_may_run=agent_may_run)
    if preferred == "a2a":
        if a2a_reason is not None:
            return RailChoice(rail=None, basis="buyer_selected_rail", refusal=a2a_reason)
        return RailChoice(rail="a2a", basis="buyer_selected_rail", dropped={"reap": "not_selected"})
    if preferred == "reap":
        return RailChoice(rail="reap", basis="buyer_selected_rail", dropped={"a2a": "not_selected"})
    if a2a_reason is None:
        return RailChoice(rail="a2a", basis="orchestrator_selected_rail", dropped={"reap": "a2a_preferred"})
    return RailChoice(rail="reap", basis="orchestrator_selected_rail", dropped={"a2a": a2a_reason})
