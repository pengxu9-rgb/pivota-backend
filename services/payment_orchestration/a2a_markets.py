"""The markets the A2A rail may take: which ones are switched on, and their currency.

ONE TABLE, `MARKETS`, read by every A2A reader (the door, the address write, the router, the worker's token and
ceiling steps and the checkout relay), so they cannot disagree on what a market is.

ON BY CONFIG, ONE MARKET AT A TIME. `A2A_RAIL_MARKETS` (comma-separated ISO 3166-1 codes, read per call) names
the markets that are on. Unset or blank means exactly today's rail: the US only. A code that is not in
`MARKETS` is ignored, so a typo turns nothing on. Set to anything, it is the whole list: "CA" alone switches the
US off.

A MARKET IS SWITCHED ON ONLY AFTER ITS OWN SANDBOX MEASURE (docs/agent_purchases_routes.md, "A2A markets"). The
provider's `token-setup` takes `amount_cents` with no currency field. This module assumes the amount is in the
currency's ISO 4217 minor units, which is how every amount here is stored (`utils/money`,
`db/reap_agentic_ledger.amount_minor_or_none`): yen and won are whole units, the rest are hundredths. One sandbox
token per currency must echo the cap we sent before that market goes on. Until then a wrong reading would set
the cap 100 times too high or too low.

THE CURRENCY of a market is the market registry's (`services.market_registry.currency_for`, ADR-026). This table
does not repeat it as a literal: `currency_for` below reads the registry, and tests/test_a2a_markets.py pins
that every market here is listed there.

NO ADDRESS RULES HERE, ON PURPOSE. Which address fields a purchase needs, and in what format, is the store
platform's to say (its quote and checkout answer), and Visa validates what it is sent (VIC: country required, a
postcode where the country has one). Pivota passes the buyer's address through: the only address fields the rail
itself requires are the ones the checkout relay sends (a2a_shipping_address.REQUIRED_ADDRESS_FIELDS), and a region
or postcode given is sent as written.

PRICING. The rail prices a cart link through the Reap lane's loader, passing its own market's currency
(`currency_for`, routes/agent_commerce_reap._cart_market_currency). So KR and TW are priced here without being in
the Reap lane's map, which is also the merchant directory's list of Reap's agentic markets.

NOT HERE, ON PURPOSE:
  - FX. No amount is ever converted: a purchase is priced, capped, minted and checked in its market's currency.
"""

from __future__ import annotations

import logging
import os
from typing import Any, FrozenSet, Mapping, Optional, Tuple

from services import market_registry

logger = logging.getLogger(__name__)

MARKETS_ENV = "A2A_RAIL_MARKETS"
#: The rail as it was before this table: the US only. Also the answer when `A2A_RAIL_MARKETS` is unset.
DEFAULT_MARKETS: Tuple[str, ...] = ("US",)

#: Every market the A2A rail can be switched on for. Adding one here turns nothing on (`A2A_RAIL_MARKETS` does).
MARKETS: Tuple[str, ...] = ("US", "CA", "AU", "NZ", "SG", "JP", "KR", "TW", "HK")


#: The distinct unusable `A2A_RAIL_MARKETS` values already warned about (bounded), so a typo is loud once per process.
_WARNED: set = set()


def enabled_markets() -> FrozenSet[str]:
    """The markets switched on (`A2A_RAIL_MARKETS`), each one in `MARKETS`. Unset or blank: the US only. A code
    outside `MARKETS` ("USA", "GB") turns nothing on and is logged as a WARNING, once per value per process."""
    raw = os.getenv(MARKETS_ENV)
    if raw is None or not raw.strip():
        return frozenset(DEFAULT_MARKETS)
    codes = [part.strip().upper() for part in raw.split(",") if part.strip()]
    unknown = sorted({code for code in codes if code not in MARKETS})
    if not codes and raw not in _WARNED and len(_WARNED) < 32:
        _WARNED.add(raw)
        logger.warning("a2a_markets: %s is set but names no market: every market is off", MARKETS_ENV)
    if unknown and raw not in _WARNED and len(_WARNED) < 32:
        _WARNED.add(raw)
        # WARNING, not INFO: prod drops module-logger INFO. Codes are config, never a secret.
        logger.warning("a2a_markets: %s names markets that are not in the table and are ignored: %s",
                       MARKETS_ENV, ",".join(code[:8] for code in unknown[:10]))
    return frozenset(code for code in codes if code in MARKETS)


def currency_for(market: Any) -> Optional[str]:
    """The market's currency (the market registry's), or None for a market not in `MARKETS`."""
    code = str(market or "").strip().upper()
    return market_registry.currency_for(code) if code in MARKETS else None


def market_for_currency(currency: Any) -> Optional[str]:
    """The one market in `MARKETS` whose currency is `currency`, or None (none, or not one). Never a default."""
    code = str(currency or "").strip().upper()
    found = [market for market in MARKETS if currency_for(market) == code]
    return found[0] if len(found) == 1 else None


def address_matches_currency(address: Any, currency: Any) -> bool:
    """Whether the address's country is the market of the purchase's currency: a CAD purchase ships to Canada
    only. An address with no country, or a currency with no one market here, does not match."""
    country = str(address.get("country") or "").strip().upper() if isinstance(address, Mapping) else ""
    return bool(country) and country == market_for_currency(currency)


def enabled_currencies() -> FrozenSet[str]:
    """The currencies of the markets switched on: the only ones a token, a ceiling or a create may be in."""
    return frozenset(currency_for(market) for market in enabled_markets())


def market_enabled(market: Any) -> bool:
    return str(market or "").strip().upper() in enabled_markets()
