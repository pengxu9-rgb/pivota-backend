"""The Shopify CART PERMALINK a Reap agentic quote may be asked to price. Pure; no I/O.

WHY THIS EXISTS. Reap confirmed (2026-09-18) that its quote endpoint will accept a Shopify cart
permalink and check it out AS RECEIVED. That lets the agentic rail buy from "Tier B" Shopify
merchants whose own agent checkout refuses us, and — because Reap checks out the URL unchanged —
our `attributes[pivota_click_id]` cart attribute reaches the merchant's order, which is the join
the attribution hook closes on.

"AS RECEIVED" IS THE WHOLE RISK, SO THIS IS AN ALLOWLIST OF ONE SHAPE:

    https://<shop>/cart/<variant>:<qty>?attributes[pivota_click_id]=<our click id>&country=<MARKET>

(the two query keys in either order) and nothing else. `country=` PINS the checkout's market —
without it the market is whatever Reap's egress IP resolves to, not our buyer's. A Shopify cart permalink can carry far more than a line item — `checkout[email]`,
`checkout[shipping_address][address1]`, `discount=`, `payment=shop_pay`, `note=`, `ref=` — and a
URL we hand to a partner that checks it out verbatim is a URL whose every key becomes part of an
order we are responsible for. `checkout[...]` in particular is BUYER PII: it would be stored in
our ledger (the URL is a column), sent to Reap, and prefilled into a checkout. The buyer's email
and address already travel in the quote BODY, where the client whitelists them field by field; a
URL is never a second route for them.

`validate_cart_link` returns the CANONICAL URL (brackets literal, host lowercased — the form
`services.outbound_links_service.build_shopify_cart_permalink` builds) or None. It never raises
and never echoes the input: a refused URL may be carrying exactly the PII it was refused for, so
`cart_link_refusal` names a REASON CODE and nothing else, and every caller logs or raises with the
code alone.

EVERY RULE HAS ITS OWN CODE AND THE CODES ARE CHECKED IN A FIXED ORDER. Several rules overlap —
an extra query key is refused by the single-key rule as well as by the `checkout` rule — and a
test that only asserted "refused" could not notice one of them being deleted. The tests assert
the code, so each rule is individually load-bearing.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Optional, Tuple
from urllib.parse import unquote, urlsplit

__all__ = [
    "CART_CLICK_ATTRIBUTE",
    "MAX_CART_LINK_QUANTITY",
    "MAX_CART_URL_LENGTH",
    "cart_link_click_id",
    "cart_link_line",
    "cart_link_refusal",
    "validate_cart_link",
]

#: The cart attribute our click id rides on. The SAME string as
#: `services.outbound_links_service.SHOPIFY_CART_CLICK_ATTRIBUTE`; it is written out rather than
#: imported because that module pulls in the database layer and this one must stay pure (the
#: ledger imports it). tests/test_reap_cart_link.py pins the two equal.
CART_CLICK_ATTRIBUTE = "attributes[pivota_click_id]"

#: Longer than any permalink this rail builds by an order of magnitude; the bound is so a hostile
#: value cannot become a large column or a large request body.
MAX_CART_URL_LENGTH = 2048

#: The URL's own bound on a line quantity. The purchase service applies its tighter
#: `MAX_QUANTITY` on top; this is the validator's floor/ceiling for "a quantity at all".
MAX_CART_LINK_QUANTITY = 100

# Every character a legitimate permalink can contain: RFC 3986 unreserved + reserved + '%'.
# Anything else — a space, a backslash, a quote, a non-ASCII letter — is refused before parsing,
# because `urlsplit` and a browser disagree about several of them and the string we validate must
# be the string Reap receives.
_ALLOWED_CHARS_RE = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*\Z")
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")

# A plain DNS hostname: labels of letters/digits/hyphen, at least two labels, alphabetic TLD. The
# alphabetic TLD is what refuses a dotted-quad; a hostname with no dot (`localhost`) fails the
# two-label rule. IP literals are ALSO refused explicitly below, so the reason is named.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)

# `/cart/<variant>:<qty>` — ONE line, ASCII digits only (`\d` would admit Arabic-Indic digits),
# no trailing slash. Leading zeros and the quantity's range are separate rules below, so each has
# its own reason code.
_CART_PATH_RE = re.compile(r"^/cart/([0-9]{1,20}):([0-9]{1,4})\Z")

# The click-attribute KEY, in the two spellings a browser or a builder produces: literal brackets
# (what `append_shopify_cart_click_attribute` writes) or percent-encoded ones, hex in either case.
_CLICK_KEY_RE = re.compile(r"^attributes(?:\[|%5[Bb])pivota_click_id(?:\]|%5[Dd])\Z")

# Our click ids are `clk_<hex>`; the shape is kept a little wider (any token of these characters)
# so a future id format is not refused here — but NO percent-encoding and nothing that needs it,
# so the raw value and the decoded value are the same string and compare exactly.
_CLICK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")

# THE MARKET PIN. A cart permalink with no `country=` opens a checkout in whatever market the
# OPENER's IP resolves to — and on the Reap path the opener is Reap's egress, not our buyer.
# Measured (coordinator, 2026-09-18): from a JP egress the bare judydoll.com link opened an
# `/en-jp` checkout; the same link plus `&country=US` opened `/en-us`. So the URL MUST name the
# row's market, and `country` is the only key allowed besides our click attribute. The key is
# matched exactly (lowercase); the VALUE is two ASCII letters, compared case-insensitively and
# stored upper-case.
_COUNTRY_KEY = "country"
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}\Z")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _shop_host(shop_domain: object) -> Optional[str]:
    """The shop's bare hostname, lowercased, or None when it is not one."""
    if not isinstance(shop_domain, str):
        return None
    host = shop_domain.strip().lower()
    if not _HOSTNAME_RE.match(host) or _is_ip_literal(host):
        return None
    return host


def _parse(url: str) -> Tuple[Optional[str], Optional[Tuple[str, str, str, str, str]]]:
    """Every STRUCTURAL rule, in order. Returns (refusal_code, None) or
    (None, (host, variant, qty, click, COUNTRY)) with the country upper-cased.

    The ORDER is part of the contract: the PII rule runs before the shape rules so that a
    `checkout[...]` URL is always reported as what it is, and the transport-level rules (length,
    control characters) run before anything parses the string at all.
    """
    if len(url) > MAX_CART_URL_LENGTH:
        return "too_long", None
    if _CONTROL_RE.search(url):
        # CR, LF, NUL, TAB, space, DEL. `urlsplit` STRIPS tab/CR/LF out of what it returns, so a
        # parse-then-approve of the raw string would approve a string it never looked at.
        return "control_character", None
    if not _ALLOWED_CHARS_RE.match(url):
        return "disallowed_character", None

    # BUYER PII. Any `checkout` anywhere in the query — raw or percent-decoded, any case — is a
    # `checkout[...]` prefill key or an attempt to spell one. Refused before the shape rules so
    # the reason is always this one for such a URL.
    query_raw = url.split("?", 1)[1] if "?" in url else ""
    if "checkout" in query_raw.lower() or "checkout" in unquote(unquote(query_raw)).lower():
        return "checkout_prefill", None

    if not url.startswith("https://"):
        return "not_https", None
    if "#" in url:
        return "fragment", None

    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        return "userinfo", None
    if "[" in netloc or _is_ip_literal(netloc):
        # An IPv6 literal (`[::1]`) or a dotted quad. A cart on a bare IP is not a shop.
        return "ip_literal", None
    if ":" in netloc:
        # A port, `:443` included — nothing we build writes one.
        return "port", None
    host = netloc.lower()
    if not _HOSTNAME_RE.match(host):
        # `localhost`, an empty host, a trailing dot, a bad label.
        return "host_not_a_hostname", None

    path = parts.path
    if path.startswith("/cart/c/"):
        # An OPAQUE cart token (Shopify's `/cart/c/<token>` share link). Not ours, not
        # inspectable: it can hold any lines and any attributes, including a different click id.
        return "opaque_cart_token", None
    if path.startswith("/cart/") and "," in path:
        return "multiple_lines", None
    match = _CART_PATH_RE.match(path)
    if match is None:
        return "path_shape", None
    variant, qty = match.group(1), match.group(2)
    if not (1 <= int(qty) <= MAX_CART_LINK_QUANTITY):
        return "quantity_out_of_range", None
    if variant.startswith("0") or qty != str(int(qty)):
        # A leading zero is a different string for the same line; nothing we build writes one,
        # and a canonical form that admitted two spellings of one line would not be canonical.
        return "path_shape", None

    query = parts.query
    pairs = [pair.partition("=") for pair in query.split("&")] if query else []
    click_pairs = [p for p in pairs if _CLICK_KEY_RE.match(p[0])]
    country_pairs = [p for p in pairs if p[0] == _COUNTRY_KEY]
    if len(click_pairs) + len(country_pairs) != len(pairs):
        # Any key but our two: `discount=`, `payment=shop_pay`, `note=`, `ref=`, our own
        # recovery-key attribute, a `Country=` in another case, or an empty segment.
        return "extra_query_key", None
    if len(click_pairs) > 1:
        # Two values for one key is a question about which one Shopify keeps; we do not ask it.
        return "extra_query_key", None
    if len(country_pairs) > 1:
        return "country_repeated", None
    if not click_pairs or not click_pairs[0][1]:
        return "click_id_missing", None
    if not country_pairs:
        return "country_missing", None
    click = click_pairs[0][2]
    if not _CLICK_ID_RE.match(click):
        return "click_id_malformed", None
    country = country_pairs[0][2]
    if not country_pairs[0][1] or not _COUNTRY_RE.match(country):
        # `country=USA`, `country=`, a bare `country`, a percent-encoded value.
        return "country_malformed", None
    return None, (host, variant, qty, click, country.upper())


def _market(market: object) -> Optional[str]:
    if not isinstance(market, str) or not _COUNTRY_RE.match(market.strip()):
        return None
    return market.strip().upper()


def cart_link_refusal(
    url: object, *, click_id: object, shop_domain: object, market: object
) -> Optional[str]:
    """None when `url` is an acceptable cart link for this click, shop and market; a reason CODE
    otherwise.

    The code is a fixed vocabulary word, safe to log and to put in an exception. The URL is never
    part of it.
    """
    if not isinstance(url, str):
        return "not_a_string"
    reason, parsed = _parse(url)
    if reason is not None:
        return reason
    assert parsed is not None
    host, _variant, _qty, click, country = parsed

    shop = _shop_host(shop_domain)
    if shop is None:
        return "shop_domain_invalid"
    if host != shop and host != f"www.{shop}":
        return "host_mismatch"

    if not isinstance(click_id, str) or not _CLICK_ID_RE.match(click_id):
        return "expected_click_id_invalid"
    if click != click_id:
        return "click_id_mismatch"

    expected_market = _market(market)
    if expected_market is None:
        return "expected_market_invalid"
    if country != expected_market:
        return "country_mismatch"
    return None


def validate_cart_link(
    url: object, *, click_id: object, shop_domain: object, market: object
) -> Optional[str]:
    """The canonical cart link, or None. See the module docstring for the one admitted shape.

    CANONICAL means: lowercase host exactly as given (apex or `www.`), `/cart/<variant>:<qty>`,
    the click attribute with LITERAL brackets, then `country=` in UPPER case — so a
    percent-encoded input, a literal one, either parameter order and either case of the country
    are all stored as the same string.
    """
    if cart_link_refusal(url, click_id=click_id, shop_domain=shop_domain, market=market):
        return None
    _reason, parsed = _parse(url)  # type: ignore[arg-type]
    assert parsed is not None
    host, variant, qty, click, country = parsed
    return (
        f"https://{host}/cart/{variant}:{qty}"
        f"?{CART_CLICK_ATTRIBUTE}={click}&{_COUNTRY_KEY}={country}"
    )


def cart_link_line(url: object) -> Optional[Tuple[str, int]]:
    """`(shopify_variant_id, quantity)` from a structurally valid cart link, or None.

    STRUCTURE ONLY — no shop, click or market comparison. For callers that already hold a URL
    that went through `validate_cart_link` (a stored row) and need the line it names.
    """
    if not isinstance(url, str):
        return None
    reason, parsed = _parse(url)
    if reason is not None or parsed is None:
        return None
    return parsed[1], int(parsed[2])


def cart_link_click_id(url: object) -> Optional[str]:
    """The click id a structurally valid cart link carries, or None. Structure only."""
    if not isinstance(url, str):
        return None
    reason, parsed = _parse(url)
    if reason is not None or parsed is None:
        return None
    return parsed[3]
