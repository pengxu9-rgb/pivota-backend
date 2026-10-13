"""Which payment rail a new purchase goes to (payment orchestration P2c).

Two rails place merchant orders for a buyer (executor `rail_managed`): Reap, and the A2A token rail.
This module decides between them for ONE create request, before anything is written and before any
rail is called. It reads configuration only: no database, no rail, no clock. The route
(routes/agent_commerce_purchases.py) acts on the answer.

── THE RULES ────────────────────────────────────────────────────────────────────────────────

1. HARD FILTERS for the A2A rail, each a named reason when it fails:
     a2a_rail_disabled        the rail's client is off (A2A_RAIL_ENABLED) or creating is off
                              (A2A_RAIL_CREATE_ENABLED)
     agent_not_in_pilot       the calling agent is not on A2A_RAIL_PILOT_AGENT_IDS
     item_source_unsupported  the rail takes a merchant cart link only (`item_source=cart_link`)
     offer_code_unsupported   the rail has no field for the buyer's offer code
     merchant_not_proven      (merchant, market) is not on A2A_RAIL_MERCHANTS: a merchant is routable
                              to this rail only after a live order there has been proven
     amount_limit_unset       A2A_RAIL_MAX_TOTAL_MINOR is unset or invalid: no cap, no rail
   The amount itself is checked by the route once OUR catalog has priced the item (`amount_ok`),
   because the price is never the caller's.
2. AN EXPLICIT CHOICE WINS, and is never silently changed. `payment_preference.rail` = "a2a" on a
   request the A2A rail cannot take is REFUSED with that rail's reason, not sent to Reap; "reap" goes
   to Reap, whose route applies its own gates.
3. NO CHOICE: the A2A rail when it passes every filter, else Reap. Every rail not taken is recorded
   with its reason (`dropped`), so the purchase can say why it is where it is.
4. NO FAILOVER AFTER THE FIRST WRITE. This runs once, before the purchase exists. Once a purchase is
   opened on a rail it stays there: it finishes or fails visibly, and a new purchase is a new
   request. A retried request with the same idempotency key replays the ORIGINAL purchase on its
   original rail, whatever this module would answer today (the route checks the key first).

Everything is off by default: with no configuration every request goes to Reap, exactly as before.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple

from services.payment_orchestration import a2a_rail

PAYMENT_ORCHESTRATOR_ENABLED_ENV = "PAYMENT_ORCHESTRATOR_ENABLED"
A2A_RAIL_CREATE_ENABLED_ENV = "A2A_RAIL_CREATE_ENABLED"
A2A_RAIL_PILOT_AGENT_IDS_ENV = "A2A_RAIL_PILOT_AGENT_IDS"
A2A_RAIL_MERCHANTS_ENV = "A2A_RAIL_MERCHANTS"
A2A_RAIL_MAX_TOTAL_MINOR_ENV = "A2A_RAIL_MAX_TOTAL_MINOR"

RAILS = ("reap", "a2a")
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


def proven_merchants() -> FrozenSet[Tuple[str, str]]:
    """(domain, MARKET) pairs, from `domain:MARKET` entries. A malformed entry is ignored (it proves
    nothing), never widened."""
    out = set()
    for entry in _csv(A2A_RAIL_MERCHANTS_ENV):
        domain, sep, market = entry.rpartition(":")
        domain, market = domain.strip().lower(), market.strip().upper()
        if sep and domain and len(market) == 2 and market.isalpha():
            out.add((domain, market))
    return frozenset(out)


def max_total_minor() -> Optional[int]:
    raw = (os.getenv(A2A_RAIL_MAX_TOTAL_MINOR_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def amount_ok(total_minor: int) -> bool:
    """True when `total_minor` (OUR price x quantity) is within the A2A rail's cap. No cap: False."""
    cap = max_total_minor()
    return cap is not None and isinstance(total_minor, int) and 0 < total_minor <= cap


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


def a2a_unavailable_reason(
    *, agent_id: str, merchant_domain: str, market_country: str, item_source: str, has_offer_code: bool = False
) -> Optional[str]:
    """None when the A2A rail may take this request (before pricing); else the named reason."""
    if not a2a_rail.is_enabled() or not _on(A2A_RAIL_CREATE_ENABLED_ENV):
        return "a2a_rail_disabled"
    if str(agent_id or "") not in pilot_agents():
        return "agent_not_in_pilot"
    if item_source != "cart_link":
        return "item_source_unsupported"
    if has_offer_code:
        return "offer_code_unsupported"
    if (str(merchant_domain or "").lower(), str(market_country or "").upper()) not in proven_merchants():
        return "merchant_not_proven"
    if max_total_minor() is None:
        return "amount_limit_unset"
    return None


def choose_rail(
    *, agent_id: str, merchant_domain: str, market_country: str, item_source: str, has_offer_code: bool = False,
    preferred: Optional[str] = None,
) -> RailChoice:
    """See the module docstring. `merchant_domain` is the canonical merchant (lower case, no `www.`)."""
    a2a_reason = a2a_unavailable_reason(
        agent_id=agent_id, merchant_domain=merchant_domain, market_country=market_country, item_source=item_source,
        has_offer_code=has_offer_code)
    if preferred == "a2a":
        if a2a_reason is not None:
            return RailChoice(rail=None, basis="buyer_selected_rail", refusal=a2a_reason)
        return RailChoice(rail="a2a", basis="buyer_selected_rail", dropped={"reap": "not_selected"})
    if preferred == "reap":
        return RailChoice(rail="reap", basis="buyer_selected_rail", dropped={"a2a": "not_selected"})
    if a2a_reason is None:
        return RailChoice(rail="a2a", basis="orchestrator_selected_rail", dropped={"reap": "a2a_preferred"})
    return RailChoice(rail="reap", basis="orchestrator_selected_rail", dropped={"a2a": a2a_reason})
