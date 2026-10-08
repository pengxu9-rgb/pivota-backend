"""The currency a Shopify storefront response was ACTUALLY read in, and a `.js` price in it.

ONE RULE, TWO WRITERS. Our two storefront-proof writers both use it: the enrichment proof job
(jobs/enrichment_cart_variant_proof.py, whose module docstring records the measurements behind
THE CURRENCY RULE) and the mirror backfill (scripts/backfill_shopify_variant_ids.py). The
purchase lane accepts a changed Reap price only when one of those proofs states the same price
in the purchase's currency (services/reap_price_corroboration.py), so both must decide "which
currency was this price in" identically. It lives here so neither restates it.

THE RULE, in short: neither `/products.json` nor `/products/<handle>.js` names its currency, and
the presentment currency follows the requester's geography and the store's Markets setup. The
read currency is therefore the `cart_currency` Set-Cookie of THE SAME FINAL response that carried
the price: exactly one value, three upper-case letters. No request may carry a cookie (a hop that
set `cart_currency` or `localization` must not steer the final response): use
`no_cookie_client`. Ask `?country=<market>` so a multi-currency store presents the market's
currency where it can; then only the market's own currency counts (`market_read_currency`).
"""

from __future__ import annotations

import re
from decimal import Decimal
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, Optional, Sequence, Tuple

import httpx

from db.reap_agentic_ledger import amount_minor_or_none

_CURRENCY = re.compile(r"[A-Z]{3}")
_CART_CURRENCY_COOKIE = "cart_currency"

#: `market_read_currency` problems beyond `presentment_currency`'s own.
CURRENCY_NOT_MARKET = "currency_not_market"
MARKET_UNKNOWN = "market_unknown"


def no_cookie_client(**kwargs: Any) -> httpx.AsyncClient:
    """An httpx client whose jar accepts NO cookie from any domain, so nothing a response sets can
    ride a later request (a redirect hop included)."""
    jar = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
    # The JAR itself, not httpx.Cookies(jar): httpx copies a Cookies object into a fresh default jar.
    return httpx.AsyncClient(cookies=jar, **kwargs)


def presentment_currency(set_cookie_headers: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    """`(currency, None)` from the `cart_currency` Set-Cookie of ONE response, or `(None, problem)`.

    Exactly one distinct value, exactly three upper-case ASCII letters. Absent, two different
    values, or anything else is a problem, never a default."""
    values = set()
    for header in set_cookie_headers or ():
        if not isinstance(header, str):
            continue
        name, sep, rest = header.partition("=")
        if not sep or name.strip() != _CART_CURRENCY_COOKIE:
            continue
        values.add(rest.split(";", 1)[0].strip())
    if not values:
        return None, "cookie_absent"
    if len(values) > 1:
        return None, "cookie_conflict"
    (value,) = values
    if not _CURRENCY.fullmatch(value):
        return None, "cookie_malformed"
    return value, None


def market_currency(market: Any) -> Optional[str]:
    """The currency the purchase lane prices `market` in (the lane's own map), or None."""
    from routes.agent_commerce_reap import _MARKET_CURRENCY

    return _MARKET_CURRENCY.get(str(market or "").strip().upper())


def market_read_currency(
    set_cookie_headers: Sequence[str], market: Any
) -> Tuple[Optional[str], Optional[str]]:
    """`(currency, None)` only when the response was read in `market`'s own currency, else
    `(None, problem)`: a `presentment_currency` problem, `market_unknown`, or
    `currency_not_market` (a store that presented another currency, e.g. a single-currency SGD
    store answering a US request)."""
    expected = market_currency(market)
    if expected is None:
        return None, MARKET_UNKNOWN
    currency, problem = presentment_currency(set_cookie_headers)
    if currency is None:
        return None, problem
    if currency != expected:
        return None, CURRENCY_NOT_MARKET
    return currency, None


def products_js_price_minor(price: Any, currency: str) -> Optional[int]:
    """A `/products/<handle>.js` price in ISO minor units of `currency`, or None (refused, never
    rounded). `.js` is x100 for EVERY currency, JPY included (measured: luafee.jp `.js` 220000 =
    JPY 2,200), so it is divided by 100 into a major amount first. An int only: a float or a
    string is not this shape."""
    if type(price) is not int:
        return None
    return amount_minor_or_none(Decimal(price) / Decimal(100), currency)


__all__ = [
    "CURRENCY_NOT_MARKET",
    "MARKET_UNKNOWN",
    "market_currency",
    "market_read_currency",
    "no_cookie_client",
    "presentment_currency",
    "products_js_price_minor",
]
