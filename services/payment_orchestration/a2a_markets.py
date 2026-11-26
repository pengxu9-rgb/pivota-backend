"""The markets the A2A rail may take: which ones are switched on, their currency, and their address rules.

ONE TABLE, `MARKETS`, read by every A2A reader (the door, the address write, the router, the worker's token and
ceiling steps and the checkout relay), so they cannot disagree on what a market is.

ON BY CONFIG, ONE MARKET AT A TIME. `A2A_RAIL_MARKETS` (comma-separated ISO 3166-1 codes, read per call) names
the markets that are on. Unset or blank means exactly today's rail: the US only. A code that is not in
`MARKETS` is ignored, so a typo turns nothing on. Set to anything, it is the whole list: "CA" alone switches the
US off.

A MARKET IS SWITCHED ON ONLY AFTER ITS OWN SANDBOX MEASURE (docs/agent_purchases_routes.md, "A2A markets"). The
provider's `token-setup` takes `amount_cents` with no currency field. This module assumes the amount is in the
currency's ISO 4217 minor units, which is how every amount here is stored (`utils/money`,
`db/reap_agentic_ledger.amount_minor_or_none`): yen are whole units, the rest are hundredths. One sandbox
token per currency must echo the cap we sent before that market goes on. Until then a wrong reading would set
the cap 100 times too high or too low.

THE CURRENCY of a market is the market registry's (`services.market_registry.currency_for`, ADR-026). This table
does not repeat it as a literal: `currency_for` below reads the registry, and tests/test_a2a_markets.py pins
that every market here is listed there.

THE ADDRESS RULES. Each market says:
  - `region`: "required", "optional" or "none". The provider's checkout fills the store's form with it as
    `state`; it is the ISO 3166-2 subdivision code without the country prefix ("ON", "NSW", "13").
  - `regions`: the codes accepted when a region is given (None for a market with no region).
  - `postal`: the postcode's pattern. Every market here has one, because the provider's checkout requires
    `zip_code` (a2a_rail.CHECKOUT_SHIPPING_REQUIRED).

NOT HERE, ON PURPOSE:
  - South Korea and Taiwan. The rail prices a cart link through the Reap lane's market map
    (routes/agent_commerce_reap._MARKET_CURRENCY), which is also what the merchant directory advertises as Reap's
    agentic markets. They are added once that map is split, so the A2A rail can price them without Reap
    advertising them.
  - Hong Kong. Its addresses have no postcode and the provider's checkout requires `zip_code`. It is added
    once the provider says what to send.
  - FX. No amount is ever converted: a purchase is priced, capped, minted and checked in its market's currency.
"""

from __future__ import annotations

import os
import re
from types import MappingProxyType
from typing import Any, FrozenSet, Mapping, Optional, Tuple

from services import market_registry

MARKETS_ENV = "A2A_RAIL_MARKETS"
#: The rail as it was before this table: the US only. Also the answer when `A2A_RAIL_MARKETS` is unset.
DEFAULT_MARKETS: Tuple[str, ...] = ("US",)

US_REGIONS = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
    "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
})
#: The 10 provinces and 3 territories.
CA_REGIONS = frozenset({"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"})
#: The 6 states and 2 mainland territories.
AU_REGIONS = frozenset({"ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"})
#: The 16 regions and the Chatham Islands territory.
NZ_REGIONS = frozenset({"AUK", "BOP", "CAN", "CIT", "GIS", "HKB", "MBH", "MWT", "NSN", "NTL", "OTA", "STL",
                        "TAS", "TKI", "WGN", "WKO", "WTC"})
#: The 47 prefectures, by their JIS / ISO 3166-2:JP number.
JP_REGIONS = frozenset(f"{n:02d}" for n in range(1, 48))

REGION_REQUIRED, REGION_OPTIONAL, REGION_NONE = "required", "optional", "none"


def _market(region: str, regions: Optional[FrozenSet[str]], postal: str) -> Mapping[str, Any]:
    return MappingProxyType({"region": region, "regions": regions, "postal": re.compile(postal)})


#: Every market the A2A rail can be switched on for. Adding one here turns nothing on (`A2A_RAIL_MARKETS` does).
MARKETS: Mapping[str, Mapping[str, Any]] = MappingProxyType({
    "US": _market(REGION_REQUIRED, US_REGIONS, r"^[0-9]{5}(?:-[0-9]{4})?\Z"),
    "CA": _market(REGION_REQUIRED, CA_REGIONS, r"^[ABCEGHJ-NPRSTVXY][0-9][ABCEGHJ-NPRSTV-Z] ?[0-9][ABCEGHJ-NPRSTV-Z][0-9]\Z"),
    "AU": _market(REGION_REQUIRED, AU_REGIONS, r"^[0-9]{4}\Z"),
    "NZ": _market(REGION_OPTIONAL, NZ_REGIONS, r"^[0-9]{4}\Z"),
    "SG": _market(REGION_NONE, None, r"^[0-9]{6}\Z"),
    "JP": _market(REGION_REQUIRED, JP_REGIONS, r"^[0-9]{3}-?[0-9]{4}\Z"),
})


def enabled_markets() -> FrozenSet[str]:
    """The markets switched on (`A2A_RAIL_MARKETS`), each one in `MARKETS`. Unset or blank: the US only."""
    raw = os.getenv(MARKETS_ENV)
    if raw is None or not raw.strip():
        return frozenset(DEFAULT_MARKETS)
    return frozenset(code for code in (part.strip().upper() for part in raw.split(",")) if code in MARKETS)


def currency_for(market: Any) -> Optional[str]:
    """The market's currency (the market registry's), or None for a market not in `MARKETS`."""
    code = str(market or "").strip().upper()
    return market_registry.currency_for(code) if code in MARKETS else None


def enabled_currencies() -> FrozenSet[str]:
    """The currencies of the markets switched on: the only ones a token, a ceiling or a create may be in."""
    return frozenset(currency_for(market) for market in enabled_markets())


def market_enabled(market: Any) -> bool:
    return str(market or "").strip().upper() in enabled_markets()


def region_rule(market: Any) -> str:
    """REGION_REQUIRED / REGION_OPTIONAL / REGION_NONE. A market not in `MARKETS` is `required`: the strictest."""
    entry = MARKETS.get(str(market or "").strip().upper())
    return entry["region"] if entry is not None else REGION_REQUIRED


def state_required(market: Any) -> bool:
    """Whether the provider's checkout must be sent a `state` for this market: only a market in `MARKETS` whose
    region is required. Any other country is not this table's to judge (a2a_rail.checkout_shipping)."""
    entry = MARKETS.get(str(market or "").strip().upper())
    return entry is not None and entry["region"] == REGION_REQUIRED


def address_errors(address: Mapping[str, Any]) -> Mapping[str, str]:
    """The market's rules on a cleaned address (`region` and `country` upper-cased), by field: `required` or
    `invalid`. A country not in `MARKETS` returns nothing here; whether it is ON is the caller's check."""
    entry = MARKETS.get(str(address.get("country") or "").upper())
    if entry is None:
        return {}
    errors = {}
    region = str(address.get("region") or "").upper()
    if entry["region"] == REGION_REQUIRED and not region:
        errors["region"] = "required"
    elif region and (entry["regions"] is None or region not in entry["regions"]):
        errors["region"] = "invalid"
    postal = str(address.get("postalCode") or "").upper()
    if not postal:
        errors["postalCode"] = "required"
    elif not entry["postal"].match(postal):
        errors["postalCode"] = "invalid"
    return errors
