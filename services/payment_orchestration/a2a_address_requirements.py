"""Which address fields a purchase on the A2A rail must have: the ones its store's PLATFORM requires in the buyer's
country, beyond the fields every address has (a2a_shipping_address.REQUIRED_ADDRESS_FIELDS).

WHY THE PLATFORM. The payment provider asks for neither a region nor a postcode; its agent fills the STORE's own
checkout, and that form's rules are the platform's, per country. A store that needs a state and is sent none fails
the checkout after the buyer approved; a store that needs no postcode must not be blocked for lack of one. So the
rail asks for exactly what the store's platform asks for, before the buyer approves.

THE TABLE (`REQUIREMENTS`): platform -> country -> the fields required there, of `region` and `postalCode`. Read
from each platform's own published address rules on 2026-11-27:
  - shopify      Shopify/worldwide (the library Shopify's checkout address forms use): data/regions/<CC>.yml with
                 lib/worldwide/region.rb. A postcode is required when the country has a `zip_regex` and no
                 `zip_requirement` saying otherwise (`zip_required?`); TW says `recommended`, HK has no postcode.
                 A region is required when the country lists province zones and is not `province_optional`
                 (`province_optional?`): NZ is optional; SG and TW list none; HK lists Hong Kong Island, Kowloon
                 and New Territories.
  - woocommerce  woocommerce/woocommerce: includes/class-wc-countries.php (`get_default_address_fields`: state and
                 postcode required; `get_country_locale` per-country overrides) with i18n/states.php. NZ's state is
                 optional, SG's and KR's are hidden and not required, HK's postcode is not required; TW has no
                 states list, so its state is a required free-text field.
A store may configure its own form beyond its platform's defaults; each market's sandbox checkout
(docs/agent_purchases_routes.md, "A2A markets") compares this table with real stores before the market goes on.

THE PLATFORM OF A MERCHANT (`A2A_RAIL_MERCHANT_PLATFORMS`, read per call): `domain:platform` entries, the domain
folded by the one merchant-host canonicaliser as `A2A_RAIL_MERCHANTS` is. It is declared when a merchant is proven
for the rail. A merchant with no entry, a platform not in the table, or a country the table does not list for that
platform requires nothing beyond the base fields (the store's checkout is then the only judge), and is logged as a
WARNING once per value, so an undeclared merchant is visible.

Pure apart from the env and the log: never raises, never logs an address.
"""

from __future__ import annotations

import logging
import os
from types import MappingProxyType
from typing import Any, FrozenSet, Mapping, Optional

logger = logging.getLogger(__name__)

PLATFORMS_ENV = "A2A_RAIL_MERCHANT_PLATFORMS"
SHOPIFY, WOOCOMMERCE = "shopify", "woocommerce"
REGION, POSTCODE = "region", "postalCode"

_BOTH: FrozenSet[str] = frozenset({REGION, POSTCODE})
_POSTCODE: FrozenSet[str] = frozenset({POSTCODE})
_REGION: FrozenSet[str] = frozenset({REGION})
_NONE: FrozenSet[str] = frozenset()

#: platform -> country -> the fields required there (of `region` and `postalCode`). Sources in the module docstring.
REQUIREMENTS: Mapping[str, Mapping[str, FrozenSet[str]]] = MappingProxyType({
    SHOPIFY: MappingProxyType({
        "US": _BOTH, "CA": _BOTH, "AU": _BOTH, "NZ": _POSTCODE, "SG": _POSTCODE, "JP": _BOTH, "KR": _BOTH,
        "TW": _NONE, "HK": _REGION,
    }),
    WOOCOMMERCE: MappingProxyType({
        "US": _BOTH, "CA": _BOTH, "AU": _BOTH, "NZ": _POSTCODE, "SG": _POSTCODE, "JP": _BOTH, "KR": _POSTCODE,
        "TW": _BOTH, "HK": _REGION,
    }),
})

#: The distinct values already warned about (bounded), so each is loud once per process.
_WARNED: set = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _WARNED or len(_WARNED) >= 64:
        return
    _WARNED.add(key)
    # WARNING, not INFO: prod drops module-logger INFO. Domains and platform names are config, never buyer data.
    logger.warning(message, *args)


def _canonical(domain: Any) -> Optional[str]:
    from services.tierb_cart_link_merchants import canonical_merchant_domain

    try:
        return canonical_merchant_domain(str(domain or "").strip())
    except ValueError:
        return None


def merchant_platforms() -> Mapping[str, str]:
    """canonical domain -> platform, from `domain:platform` entries. A malformed entry, or a platform not in
    `REQUIREMENTS`, is ignored (and logged once): it declares nothing."""
    out = {}
    for entry in (os.getenv(PLATFORMS_ENV) or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        domain, sep, platform = entry.rpartition(":")
        platform = platform.strip().lower()
        canonical = _canonical(domain) if sep else None
        if canonical is None or platform not in REQUIREMENTS:
            _warn_once("entry:" + entry[:80], "a2a_address_requirements: %s entry ignored (malformed, or a platform "
                       "not in the table): %s", PLATFORMS_ENV, entry[:80])
            continue
        out[canonical] = platform
    return out


def merchant_platform(merchant_domain: Any) -> Optional[str]:
    """The merchant's declared platform, or None."""
    canonical = _canonical(merchant_domain)
    return merchant_platforms().get(canonical) if canonical else None


def required_fields(merchant_domain: Any, country: Any) -> FrozenSet[str]:
    """The fields of `region` / `postalCode` an address in `country` must have for this merchant's platform. Empty
    when the merchant (None included) has no declared platform or the table does not list the country for it."""
    if merchant_domain is None:
        return _NONE
    platform = merchant_platform(merchant_domain)
    if platform is None:
        _warn_once("domain:" + str(merchant_domain)[:80], "a2a_address_requirements: no platform declared for %s "
                   "(%s): no region or postcode is required for it", str(merchant_domain)[:80], PLATFORMS_ENV)
        return _NONE
    code = str(country or "").strip().upper()
    required = REQUIREMENTS[platform].get(code)
    if required is None:
        if code:
            _warn_once(f"country:{platform}:{code[:4]}", "a2a_address_requirements: the %s table lists no %s: no "
                       "region or postcode is required there", platform, code[:4])
        return _NONE
    return required


def missing(merchant_domain: Any, address: Any) -> FrozenSet[str]:
    """The platform-required fields `address` lacks (by name, never a value)."""
    if not isinstance(address, Mapping):
        return _NONE
    required = required_fields(merchant_domain, address.get("country"))
    return frozenset(f for f in required if not str(address.get(f) or "").strip())
