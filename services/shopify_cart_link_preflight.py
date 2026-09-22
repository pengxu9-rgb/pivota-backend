"""Does a Shopify cart permalink land on a checkout Reap can complete? An HTTP-only preflight.

WHY THIS EXISTS. "Tier B" Shopify merchants price a cart through their UCP (agent checkout)
door but refuse agent completion (`requires_escalation`: `extension_interaction_required`,
`redirect_to_checkout_required`, `customer_account_required`). What Reap CAN complete for them
is the storefront's own checkout, reached through a cart permalink:

    https://{host}/cart/{variant}:{qty}?attributes[pivota_click_id]=clk_...

Measured 2026-09-18 over 40 Tier B merchants: 36 landed on `/checkouts/cn/<token>/...` with the
click id and variant on the page (and, when prefilled, the email and address too). The rest
failed in four distinct ways, and this module names each one instead of reporting a bare "no"
(see `Verdict`).

THE REAP PATH CARRIES NO BUYER PII IN THE LINK: call this with `buyer=None`. Reap's quote
endpoint (`POST /agentic/quotes`) takes the cart URL as received, and the buyer's email and
shipping address travel in the quote BODY. The `buyer=` prefill (`checkout[email]`,
`checkout[shipping_address][...]`) exists for a HUMAN handoff only. Without a buyer, ELIGIBLE
never requires an email or address on the page.

AVAILABILITY IS MARKET-SCOPED. Shopify's storefront `available` flag is computed for the market
Shopify resolves for the REQUEST (egress IP + Accept-Language), not for the buyer. Measured on
podl.us from a JP-geolocated machine: `products.json` with any Accept-Language showed 0 available
variants; with `&country=US`, 2. Every catalog and handle read here therefore pins
`country=<market>`, so this preflight answers "available in THIS market". Read without the
buyer's market it would report the SERVER's market instead (prod egress is US, a laptop may be
JP) — which is why `market` is a required argument and must equal the buyer's country when a
buyer is given.

THE CHECKOUT'S MARKET IS PINNED TOO. Without it Shopify picks the checkout market from the
requester: from a JP egress judydoll's permalink landed on an `en-jp` checkout with buyer
countryCode JP; the same link plus `&country=US` landed on `en-us` / US (verified live
2026-09-18). The permalink is therefore always built with `country=<market>`, the landed
checkout's buyer country is read back (`buyerIdentity.customer.countryCode`), and a checkout in
any other market — or one whose market cannot be read — is CHECKOUT_MARKET_MISMATCH.

WHAT COUNTS AS EVIDENCE ON THE PAGE. Not substrings. The variant must be a MERCHANDISE LINE
(`merchandiseLines[].merchandise.id == gid://shopify/ProductVariantMerchandise/<id>`, a
`MerchandiseLine`), not a mention in a removed line, a recommendation or an echoed URL; the click
id must be the cart attribute pair (`customAttributes[] {key: pivota_click_id, value: <exact>}`),
not the `queryString` / `return_to` echo the page also carries. Both are parsed as JSON out of
the HTML-unescaped page.

THE ONE SIDE EFFECT. Following the permalink CREATES AN ABANDONED SHOPIFY CHECKOUT on the
merchant's store — the same side effect as every UCP probe before it. Nothing else is written,
nothing is paid, no payment step is touched. Resolving the variant reads only the public
`/products/<handle>.js` and `/products.json` endpoints.

NEVER EXPOSE THIS TO A USER-SUPPLIED OR ANONYMOUS DOMAIN. It runs only over Pivota's internal
merchant list (today: `scripts/ops/tierb_cart_link_preflight.py`, an operator tool). A route
that let a caller name the host would let an anonymous party make us create checkouts, under
our IP and our click ids, on any store they like — and would turn this into a fetcher of
arbitrary URLs besides. Redirect hops are held to https on a DNS name (no IP literals, no
numeric/hex hosts such as `127.1` or `0x7f.1`, no `localhost`/`*.localhost`/`.local`/
`.internal`), but that is a floor, not a licence to widen the input. KNOWN LIMITATION: a DNS
name that RESOLVES to a private address is not refused (no resolution is done here); that is
acceptable only because no web caller can reach this and hosts come from our own list.

WHAT IT CANNOT SEE: SHIPPING. Shopify's checkout page ships `deliveryLines: []` in its server
HTML and loads rates with JavaScript, so no HTTP-only check can prove a rate exists. Measured on
the same day: heartpercent.us (no delivery to the US for the item) and anua.us / skin1004.com
(qty 1 below a basket minimum, "Shipping not available") all land prefilled and therefore pass
this preflight as ELIGIBLE. `PreflightResult.shipping_verified` is always False for that reason;
shipping must be proven downstream before a buyer is sent to pay — on the Reap path that proof
is Reap's quote (`shippingOptions` + `amountBreakdown`); a quote with no shipping option is not
eligible.

WHAT IT CAN NOW SEE: THE PAYMENT METHODS. Measured live 2026-09-22. The checkout page's
serialized state carries an `availablePaymentLines` array — the store's ACTUAL accept-list for
this checkout, unlike `/.well-known/ucp` `payment_handlers`, which is a platform constant every
Shopify store repeats. Each element is
`{placements: ["PAYMENT_METHOD"|"ACCELERATED_CHECKOUT"], paymentMethod: {__typename, name,
paymentBrands}}`, and a CARD form is present only when some line is a `PaymentProvider` in the
`PAYMENT_METHOD` placement whose `paymentBrands` name card brands. Live: idewcare.com =
`PaymentProvider/shopify_payments` (VISA, MASTERCARD, AMEX, DISCOVER, ...), judydoll.com =
`PaymentProvider/Airwallex` (VISA, MASTERCARD, AMEX, MAESTRO, JCB, UNIONPAY), flowerbeauty.com =
NO PaymentProvider at all, only PayPal. That last one is `NO_CARD_PAYMENT`: a card-paying
headless checkout (Reap) cannot complete it, and it was served as purchasable before this check
existed.

DO NOT SUBSTRING-MATCH FOR A CARD. `creditCard` appears in the scripts of all three pages
including the PayPal-only one, and `AnyGiftCardPaymentMethod` / `AnyStripeSharedTokenPaymentMethod`
appear as `availablePaymentLines` entries on all three — they are platform constants, not an
accept-list, and reading either as a card is exactly the false positive this module exists to
stop. Only `PaymentProvider` + card `paymentBrands` counts.

`card_available` is a THREE-valued answer: True (a card line was read), False (the accept-list
was read and holds no card line — POSITIVE evidence), None (no accept-list could be read at all).
None is never False: an unreadable page, a bot challenge or a transport failure is unverifiable,
and this module never turns "cannot tell" into "no card".

AND THE NEGATIVE IS DEFENDED AGAINST VOCABULARY DRIFT, because it is the dangerous answer. This
reads an UNVERSIONED blob belonging to somebody else, and a drift in it lands on EVERY Shopify
merchant on the same afternoon. So False requires the array to be present, non-empty, and every
line to have the measured shape — `placements` a list, `paymentMethod` an object with a string
`__typename`, `paymentBrands` a list or null. Anything else is `DETECTOR_SHAPE_UNEXPECTED`:
`card_available=None`, verdict `BLOCKED_UNKNOWN`, `retryable=True`. A parser that shrugged at a
renamed typename would demote the whole catalogue and call it evidence.

BOTH CONJUNCTS OF THE CARD RULE ARE LOAD-BEARING. On every page measured, non-provider lines
carry `paymentBrands: null`, so the brand check ALONE appears to decide — and `ApplePayWalletConfig`
already carries `placements: ["PAYMENT_METHOD"]`. A wallet that also advertised the card brands
behind it (which is what a wallet is) would then read as a card FORM, and a headless payer cannot
authenticate to a wallet. `__typename == "PaymentProvider"` is what stops that, on its own.

PRICE PARITY. The merchandise line carries `totalAmount.value.{amount,currencyCode}` and
`quantity`, so the price the buyer would actually be charged is readable. Live on the same day,
flowerbeauty.com's line was USD 8.00 while our index held USD 14.95. With a caller-supplied
`expected_price_minor` the difference is reported EXACTLY, in minor units, and any non-zero
difference is `PRICE_DRIFT`. There is no tolerance band: a drift is a fact about our index being
wrong, and widening it would re-hide the case it was written for.

PII. A prefilled permalink carries the buyer's email and address. Every URL this module records or logs
goes through `redact_cart_permalink` first, and while a preflight runs, the HTTP client's own
logging (httpx prints the full URL at INFO; httpcore prints response headers, `Location`
included, at DEBUG) is redacted by a filter scoped to the preflight's context — see
`_RedactUrlsWhilePreflighting`. Other sinks that observe requests (e.g. an error tracker's httpx
integration recording query strings) are NOT covered; check them before real buyer data flows.
"""

from __future__ import annotations

import contextvars
import enum
import html
import ipaddress
import json
import logging
import re
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote_plus, urljoin, urlparse, urlunparse

import httpx

from utils.money import ZERO_DECIMAL_CURRENCIES

from services.outbound_links_service import (
    CartPrefill,
    redact_query_string,
    build_shopify_cart_permalink,
    cart_prefill_refusal,
    extract_shopify_numeric_variant_id,
    normalize_shop_host,
    redact_cart_permalink,
)
from services.shopify_variant_identity import MAX_VARIANTS, parse_product_js

logger = logging.getLogger(__name__)

MAX_REDIRECT_HOPS = 10
MAX_BODY_BYTES = 6_000_000  # checkout pages measured ~300 KB; this only bounds a hostile one
CATALOG_PAGE_SIZE = 250
MAX_CATALOG_PAGES = 20
MAX_QUANTITY = 100
REQUEST_TIMEOUT_S = 30.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

_CHECKOUT_PATH = re.compile(r"^/checkouts/(?:cn|c|co)/")
_NOT_ACCEPTING_TEXT = "set up to receive orders"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_INVALID_LOCATION_PREFIX = "Invalid URL in location header"  # httpx 0.27 _redirect_url
_CHECKOUT_TOTAL = re.compile(
    r'totalAmount":\{"value":\{"amount":"([0-9.]+)","currencyCode":"([A-Z]{3})"'
)


class Verdict(str, enum.Enum):
    """What the permalink did. Only ELIGIBLE means "send it to Reap" — and even then,
    shipping is unproven (see the module docstring)."""

    ELIGIBLE = "ELIGIBLE"
    # Landed on a checkout with the variant and click id, but the buyer prefill did not stick.
    CHECKOUT_PREFILL_MISSING = "CHECKOUT_PREFILL_MISSING"
    # A hop went through /customer_authentication/ or shopify.com/authentication/ (forbeaut, podl).
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    # 403 on /checkouts/ saying the store "isn't set up to receive orders yet" (luafee).
    NOT_ACCEPTING_ORDERS = "NOT_ACCEPTING_ORDERS"
    # Any other 403. Not evidence of anything in particular.
    BLOCKED_UNKNOWN = "BLOCKED_UNKNOWN"
    # 410 on /cart/... ("Link no longer exists"), or the storefront no longer lists the variant.
    VARIANT_GONE = "VARIANT_GONE"
    # The storefront lists the variant (or product) but nothing is available to buy.
    VARIANT_UNAVAILABLE = "VARIANT_UNAVAILABLE"
    # The storefront would not let us confirm the variant (non-200 / non-JSON / scan cap).
    VARIANT_UNVERIFIED = "VARIANT_UNVERIFIED"
    PASSWORD_PAGE = "PASSWORD_PAGE"
    # The checkout's own accept-list was READ and holds no card method (flowerbeauty.com: PayPal
    # only). POSITIVE evidence of a checkout a card-paying agent cannot complete — never the
    # verdict for a page we could not read, which stays BLOCKED_UNKNOWN / TRANSPORT_ERROR.
    NO_CARD_PAYMENT = "NO_CARD_PAYMENT"
    # The landed checkout charges a different price than the caller's expected one. Exact, in
    # minor units, no tolerance band.
    PRICE_DRIFT = "PRICE_DRIFT"
    # A checkout with our line and click id, but in another market than the buyer's (or one
    # whose market cannot be read off the page). Definite: not eligible.
    CHECKOUT_MARKET_MISMATCH = "CHECKOUT_MARKET_MISMATCH"
    # Connect error, timeout, proxy flake. RETRYABLE, and never proof of ineligibility.
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    # The caller's arguments were refused before any request was made.
    INVALID_INPUT = "INVALID_INPUT"
    UNCLASSIFIED = "UNCLASSIFIED"


Hop = Tuple[int, str]  # (status, REDACTED url)


@dataclass(frozen=True)
class PreflightResult:
    """The outcome of one preflight.

    `shipping_verified` IS ALWAYS FALSE and cannot be set: an HTTP preflight cannot see
    shipping rates, which the checkout page loads with JavaScript. heartpercent.us (no delivery
    to the US) and anua.us / skin1004.com (qty 1 under a basket minimum) all come back ELIGIBLE
    here. Shipping must be proven downstream before a buyer pays.

    Every URL in here is redacted (`redact_cart_permalink`): host, path and click id survive,
    buyer values do not. `price` is the storefront's listed price for the variant, whose
    currency the storefront JSON does not state, so `currency` stays None unless a later
    source knows it; `checkout_total` / `checkout_currency` are read off the checkout page.
    """

    host: str
    verdict: Verdict
    retryable: bool = False
    market: Optional[str] = None  # the market `available` was read for (None if refused)
    variant_id: Optional[str] = None
    variant_source: Optional[str] = None  # "caller" | "product" | "catalog"
    variant_title: Optional[str] = None
    product_title: Optional[str] = None
    price: Optional[str] = None
    currency: Optional[str] = None
    checkout_total: Optional[str] = None
    checkout_currency: Optional[str] = None
    resolve_chain: Tuple[Hop, ...] = ()
    chain: Tuple[Hop, ...] = ()
    final_status: Optional[int] = None
    final_host: Optional[str] = None
    final_url: Optional[str] = None  # redacted
    missing: Tuple[str, ...] = ()
    detail: Optional[str] = None
    checkout_country: Optional[str] = None  # buyerIdentity countryCode read off the checkout
    # The checkout's own accept-list, as labels (gateway `name` when the page gives one, else the
    # GraphQL `__typename`). Bounded and PII-free: no tokens, no merchant ids, no buyer data.
    payment_methods: Tuple[str, ...] = ()
    # THREE-VALUED. None means "could not determine" and is NEVER False: card-by-absence is the
    # false negative that would let an unreadable page demote a good merchant, just as
    # card-by-substring is the false positive that let flowerbeauty.com through.
    card_available: Optional[bool] = None
    # The landed merchandise line's UNIT price, exact minor units, and its currency.
    landed_price_minor: Optional[int] = None
    landed_currency: Optional[str] = None
    # landed - expected, exact minor units. None when either side is unknown; 0 means parity.
    price_drift_minor: Optional[int] = None
    shipping_verified: bool = field(default=False, init=False)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["verdict"] = self.verdict.value
        out["resolve_chain"] = [list(h) for h in self.resolve_chain]
        out["chain"] = [list(h) for h in self.chain]
        out["missing"] = list(self.missing)
        out["payment_methods"] = list(self.payment_methods)
        return out


# --- PII-safe logging ------------------------------------------------------------------------

_PREFLIGHT_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "shopify_cart_link_preflight_active", default=False
)
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+")
# Any `?query` span, whatever precedes it: httpcore logs a RELATIVE `Location`
# (`/checkouts/cn/T?checkout%5Bemail%5D=...`) or a scheme-relative one (`//s.com/...?...`)
# exactly as the server sent it, and neither matches an `https?://` pattern.
_QUERY_IN_TEXT = re.compile(r"\?([^\s\"'<>#]*)")


def _redact_text(text: str) -> str:
    text = _URL_IN_TEXT.sub(lambda m: redact_cart_permalink(m.group(0)), text)
    return _QUERY_IN_TEXT.sub(lambda m: "?" + redact_query_string(m.group(1)), text)


def _redact_log_arg(arg: Any) -> Any:
    if isinstance(arg, httpx.URL):
        return redact_cart_permalink(str(arg))
    if isinstance(arg, str):
        return _redact_text(arg)
    return arg


class _RedactUrlsWhilePreflighting(logging.Filter):
    """httpx logs every request as ``HTTP Request: GET <full url> ...`` at INFO, and httpcore's
    DEBUG trace logs every response's headers, ``Location`` included. For a prefilled permalink —
    and for the hops Shopify redirects through, which re-emit ``checkout%5Bemail%5D`` and a
    shop_pay_token embedding the landing URL — those lines are buyer PII. This filter redacts
    URLs in those records, but ONLY inside a running preflight (a ContextVar set by
    `preflight`), so no other caller's HTTP-client logging changes."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _PREFLIGHT_ACTIVE.get():
            return True
        if isinstance(record.msg, str):
            record.msg = _redact_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_log_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_log_arg(v) for k, v in record.args.items()}
        return True


# A logger's filters see only records logged on THAT logger, not its children's, so every
# httpcore module logger is named. httpcore's DEBUG trace prints each response's headers —
# including the `Location` of Shopify's next hop, which re-emits `checkout%5Bemail%5D=...`.
# Found by a live DEBUG run on 2026-09-18 (robinsons, luafee), not by the mocked suite, which
# never reaches httpcore.
_HTTP_CLIENT_LOGGERS = (
    "httpx",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.connection",
    "httpcore.proxy",
    "httpcore.socks",
)


def _install_httpx_log_filter() -> None:
    for name in _HTTP_CLIENT_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(f, _RedactUrlsWhilePreflighting) for f in target.filters):
            target.addFilter(_RedactUrlsWhilePreflighting())


_install_httpx_log_filter()


# --- fetching --------------------------------------------------------------------------------


class _TransportFailure(Exception):
    def __init__(self, exc: BaseException, chain: List[Hop]):
        super().__init__(type(exc).__name__)
        self.kind = type(exc).__name__
        self.chain = chain


class _HopRefused(Exception):
    def __init__(self, reason: str, chain: List[Hop]):
        super().__init__(reason)
        self.reason = reason
        self.chain = chain


@dataclass
class _Landing:
    status: int
    body: str
    chain: List[Hop]
    too_many_redirects: bool = False


# A last label that is all digits or `0x`-hex is not a DNS name: `127.1`, `0x7f.1`, `10.1` and
# `0177.0.0.1` are all addresses to the resolver (inet_aton shorthand), none of them a domain.
_NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)")


def _host_refusal(host: str) -> Optional[str]:
    h = str(host or "").strip().lower().rstrip(".")  # `localhost.` is `localhost`
    if h == "localhost" or h.endswith((".localhost", ".local", ".internal")):
        return "host_local"
    if not h or "." not in h:
        return "host_not_a_domain"
    try:
        ipaddress.ip_address(h.strip("[]"))
        return "host_is_ip_literal"
    except ValueError:
        pass
    if _NUMERIC_LABEL.fullmatch(h.split(".")[-1]):
        return "host_is_numeric"
    return None


def _hop_refusal(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return "hop_unparseable"
    if parsed.scheme != "https":
        return "hop_not_https"
    return _host_refusal(host)


async def _read_bounded(response: httpx.Response) -> str:
    chunks: List[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


def _with_query_param(url: str, key: str, value: str) -> str:
    """`url` with exactly one `key=value`, merged into whatever query it already has (an
    existing `key` is replaced, never duplicated)."""
    parsed = urlparse(url)
    kept = [p for p in parsed.query.split("&") if p and unquote_plus(p.partition("=")[0]) != key]
    kept.append(f"{key}={quote(value, safe='')}")
    return urlunparse(parsed._replace(query="&".join(kept)))


async def _fetch_following(
    client: httpx.AsyncClient, url: str, *, pin_params: Optional[Dict[str, str]] = None
) -> _Landing:
    """GET `url`, following redirects BY HAND (at most MAX_REDIRECT_HOPS requests), recording
    every hop REDACTED. Cross-host redirects are followed — poopourri.com lands on pourri.com
    via shop.app — but only to https on a DNS name.

    `pin_params` are merged into EVERY hop's query, not just the first: a storefront that 301s
    `products.json` to another host (robinsons -> www) may drop the query on the way, and a
    catalog read that loses `country=` silently answers for the server's market instead."""
    chain: List[Hop] = []
    current = url
    for _ in range(MAX_REDIRECT_HOPS):
        try:
            for key, value in (pin_params or {}).items():
                current = _with_query_param(current, key, value)
            refusal = _hop_refusal(current)
            if refusal:
                raise _HopRefused(refusal, chain)
            request = client.build_request("GET", current, headers=_HEADERS, timeout=REQUEST_TIMEOUT_S)
        except (httpx.InvalidURL, UnicodeError, ValueError):
            # A `Location` httpx cannot parse (`:abc` port, `https://xn--/` IDNA): not a network
            # failure and not retryable, but never an exception that sinks the caller's batch.
            raise _HopRefused("hop_invalid_url", chain) from None
        try:
            response = await client.send(request, stream=True, follow_redirects=False)
        except (httpx.InvalidURL, UnicodeError):
            raise _HopRefused("hop_invalid_url", chain) from None
        except httpx.RemoteProtocolError as exc:
            # httpx parses a redirect's Location even with follow_redirects=False (to fill
            # `next_request`), and reports an unparseable one (`:abc` port) as this transport
            # error. That is the server's malformed header, not a flaky network: not retryable.
            if str(exc).startswith(_INVALID_LOCATION_PREFIX):
                raise _HopRefused("hop_invalid_url", chain) from None
            raise _TransportFailure(exc, chain) from None
        except httpx.HTTPError as exc:
            raise _TransportFailure(exc, chain) from None
        try:
            chain.append((response.status_code, redact_cart_permalink(str(response.url))))
            logger.debug("cart-link preflight hop %s %s", response.status_code, chain[-1][1])
            location = response.headers.get("location")
            if response.status_code in _REDIRECT_STATUSES and location:
                current = urljoin(str(response.url), location)
                continue
            body = await _read_bounded(response)
        except httpx.HTTPError as exc:
            # TransportError, and DecodingError (a gzip body that is not gzip): both retryable.
            raise _TransportFailure(exc, chain) from None
        finally:
            await response.aclose()
        return _Landing(response.status_code, body, chain)
    return _Landing(chain[-1][0] if chain else 0, "", chain, too_many_redirects=True)


# --- classification --------------------------------------------------------------------------


def _is_login_hop(redacted_url: str) -> bool:
    parsed = urlparse(redacted_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if "/customer_authentication/" in path or path.startswith("/account/login"):
        return True  # new customer accounts, and classic `/account/login`
    return (host == "shopify.com" or host.endswith(".shopify.com")) and path.startswith("/authentication/")


def _page_contains(text: str, value: str) -> bool:
    """Exact presence in the HTML-unescaped page. No other decoding is attempted: a value the
    page only carries in some other encoding reads as missing, which errs toward not ELIGIBLE."""
    return bool(value) and value in text


_JSON = json.JSONDecoder()
_MAX_KEY_HITS = 64
_CLICK_ATTRIBUTE_KEY = "pivota_click_id"


def _json_values_after(page: str, key: str) -> List[Any]:
    """Every JSON value that follows `"<key>":` in the page, parsed (bounded). An occurrence
    whose value does not parse is skipped, never guessed at. The leading quote in the needle
    means `"removedMerchandiseLines"` is NOT a `"merchandiseLines"`."""
    needle = re.compile(r'"' + re.escape(key) + r'"\s*:\s*')
    out: List[Any] = []
    for hit in needle.finditer(page):
        if len(out) >= _MAX_KEY_HITS:
            break
        try:
            value, _ = _JSON.raw_decode(page, hit.end())
        except ValueError:
            continue
        out.append(value)
    return out


def _has_variant_line(page: str, variant_id: str) -> bool:
    """Is `variant_id` a merchandise LINE of this checkout? Exact gid equality (so `5004` is
    not `50041`), inside a `MerchandiseLine` of a `merchandiseLines` array, on a
    `*ProductVariantMerchandise`; a `variantId`, when present, must agree."""
    want_id = f"gid://shopify/ProductVariantMerchandise/{variant_id}"
    want_variant = f"gid://shopify/ProductVariant/{variant_id}"
    for lines in _json_values_after(page, "merchandiseLines"):
        for line in lines if isinstance(lines, list) else []:
            if not isinstance(line, dict) or line.get("__typename") != "MerchandiseLine":
                continue
            merch = line.get("merchandise")
            if not isinstance(merch, dict):
                continue
            if not str(merch.get("__typename") or "").endswith("ProductVariantMerchandise"):
                continue
            if merch.get("id") != want_id:
                continue
            if "variantId" in merch and merch.get("variantId") != want_variant:
                continue
            return True
    return False


def _has_click_attribute(page: str, click_id: str) -> bool:
    """Is the click id the checkout's cart attribute — `{key: pivota_click_id, value: <exact>}`
    in a `customAttributes` array? A URL or queryString echo of it does not count."""
    for attrs in _json_values_after(page, "customAttributes"):
        for attr in attrs if isinstance(attrs, list) else []:
            if isinstance(attr, dict) and attr.get("key") == _CLICK_ATTRIBUTE_KEY and attr.get("value") == click_id:
                return True
    return False


def checkout_buyer_country(body: str) -> Optional[str]:
    """The checkout's buyer country (`buyerIdentity.countryCode`, or `.customer.countryCode` as
    Shopify nests it today), read off the HTML-unescaped page. The empty `"buyerIdentity":[]` of a
    PolicyFactSet is not an object and never matches. None when absent OR ambiguous (two
    different codes): an unreadable market is not a matching one."""
    page = html.unescape(body or "")
    found = set()
    for ident in _json_values_after(page, "buyerIdentity"):
        if not isinstance(ident, dict):
            continue
        for holder in (ident, ident.get("customer")):
            code = holder.get("countryCode") if isinstance(holder, dict) else None
            if isinstance(code, str) and re.fullmatch(r"[A-Z]{2}", code):
                found.add(code)
    return next(iter(found)) if len(found) == 1 else None


# --- payment methods -------------------------------------------------------------------------

# Brands Shopify names on a card gateway's `paymentBrands`. Membership here is what makes a
# payment line a CARD form; a gateway that names none of them is not evidence of a card.
_CARD_BRANDS = frozenset({
    "VISA", "MASTERCARD", "MASTER_CARD", "AMEX", "AMERICAN_EXPRESS", "DISCOVER", "DINERS_CLUB",
    "DINERS", "JCB", "UNIONPAY", "UNION_PAY", "MAESTRO", "ELO", "HIPERCARD", "CARTES_BANCAIRES",
    "CARTE_BLEUE", "DANKORT", "MADA", "INTERAC", "BANCONTACT",
})
# The ONLY `paymentMethod.__typename` that denotes a card gateway. `AnyGiftCardPaymentMethod` and
# `AnyStripeSharedTokenPaymentMethod` are on every Shopify checkout measured, flowerbeauty's
# PayPal-only one included; the `*WalletConfig` types are wallets, which a headless card payer
# cannot drive either.
_CARD_METHOD_TYPENAME = "PaymentProvider"
_PAYMENT_METHOD_PLACEMENT = "PAYMENT_METHOD"
_MAX_PAYMENT_METHOD_LABELS = 32
_MAX_PAYMENT_METHOD_LABEL_LEN = 64


def _payment_method_label(method: Dict[str, Any]) -> Optional[str]:
    """A short, PII-free name for one payment line: the gateway `name` when the page gives one
    (`shopify_payments`, `Airwallex`, `PAYPAL_EXPRESS`), else its `__typename`. Never a token,
    an id or a client secret — this string is stored as evidence."""
    for key in ("name", "__typename"):
        value = method.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_PAYMENT_METHOD_LABEL_LEN]
    return None


#: The note recorded when the accept-list is present but does not have the shape this detector
#: was measured against. It is NOT a negative — see `_read_payment_lines`.
DETECTOR_SHAPE_UNEXPECTED = "detector_shape_unexpected"


class _ShapeSurprise(Exception):
    """One `availablePaymentLines` element was not the shape measured on 2026-09-22."""


def _read_payment_lines(lines: List[Any]) -> Tuple[Tuple[str, ...], bool]:
    """(labels, card_present) for one `availablePaymentLines` array. Raises `_ShapeSurprise`.

    THE NEGATIVE IS THE DANGEROUS ANSWER, SO IT IS THE ONE THAT IS DEFENDED. `card_available` is
    False only when EVERY line in the array has the shape below. That is not fussiness: this
    detector reads an UNVERSIONED serialized blob belonging to somebody else, and every drift in
    it lands on every Shopify merchant at once. If Shopify drops `placements`, renames
    `PaymentProvider`, or starts sending `paymentBrands` as a comma-joined string, a lenient
    parser reads "no card line here" for the entire catalogue on the same afternoon and the rail
    demotes every merchant it has. A shape surprise must therefore mean "I cannot read this page"
    (None, retryable), never "this merchant refuses cards".

    THE SHAPE, per line, all required:
      * the line is an object;
      * `placements` is a LIST (of strings);
      * `paymentMethod` is an object whose `__typename` is a string;
      * `paymentBrands`, when present, is a list or null — never a string or a number.

    BOTH CONJUNCTS OF THE CARD RULE ARE LOAD-BEARING and neither is implied by the other. On
    every page measured, non-provider lines happen to carry `paymentBrands: null`, so the brand
    check ALONE appears to decide — which is exactly why deleting the `__typename` conjunct
    survived the first test suite. It must not: `ApplePayWalletConfig` already carries
    `placements: ["PAYMENT_METHOD"]`, so a wallet that one day also advertises the card brands it
    accepts (which is what a wallet IS — a stored card) would be read as a card FORM a headless
    payer can drive. It is not; the payer cannot authenticate to the wallet. See
    `tests/test_merchant_purchasability.py::test_a_wallet_line_that_advertises_card_brands_is_not_a_card`.
    """
    labels: List[str] = []
    card = False
    for line in lines:
        if not isinstance(line, dict):
            raise _ShapeSurprise("line is not an object")
        method = line.get("paymentMethod")
        if not isinstance(method, dict):
            raise _ShapeSurprise("paymentMethod is not an object")
        typename = method.get("__typename")
        if not isinstance(typename, str) or not typename.strip():
            raise _ShapeSurprise("paymentMethod.__typename is not a string")
        raw = line.get("placements")
        if not isinstance(raw, list):
            raise _ShapeSurprise("placements is not a list")
        brands = method.get("paymentBrands")
        if brands is not None and not isinstance(brands, list):
            raise _ShapeSurprise("paymentBrands is neither a list nor null")

        label = _payment_method_label(method)
        if label:
            labels.append(label)
        if _PAYMENT_METHOD_PLACEMENT not in {p for p in raw if isinstance(p, str)}:
            continue  # an ACCELERATED_CHECKOUT-only line is a wallet button, not a card form
        if typename != _CARD_METHOD_TYPENAME:
            continue  # LOAD-BEARING on its own; see the docstring
        named = {b.strip().upper() for b in (brands or []) if isinstance(b, str)}
        if named & _CARD_BRANDS:
            card = True
    return tuple(sorted(set(labels))[:_MAX_PAYMENT_METHOD_LABELS]), card


def checkout_payment_methods(body: str) -> Tuple[Tuple[str, ...], Optional[bool], Optional[str]]:
    """The checkout's accept-list, whether it offers a CARD, and a detector note.

    Returns `(labels, card_available, note)`. `card_available` is None — not False — whenever:
      * no `availablePaymentLines` array could be read at all;
      * the array is present but EMPTY, or names no method;
      * a line does not have the measured shape (`note` is then `DETECTOR_SHAPE_UNEXPECTED`);
      * two copies of the serialized state disagree.

    Only an array we fully read, every line well-shaped, holding no `PaymentProvider` with card
    brands, answers False. See the module docstring for why a substring must never be used here.
    """
    page = html.unescape(body or "")
    answers = set()
    surprised = False
    for lines in _json_values_after(page, "availablePaymentLines"):
        if not isinstance(lines, list) or not lines:
            continue
        try:
            labels, card = _read_payment_lines(lines)
        except _ShapeSurprise as exc:
            # The type and nothing else: this is somebody else's page and its values are not ours
            # to log. One line at WARNING because a drift here is a whole-platform event.
            surprised = True
            logger.warning(
                "cart-link preflight: availablePaymentLines did not have the measured shape (%s); "
                "reporting card_available=None rather than a negative", exc,
            )
            continue
        if not labels:
            continue  # an accept-list that names nothing determines nothing
        answers.add((labels, card))
    if surprised:
        # A surprise anywhere poisons the read, even if another copy parsed cleanly: the two
        # copies are the same state, so one of them being unreadable means we do not know which
        # is current.
        return (), None, DETECTOR_SHAPE_UNEXPECTED
    if len(answers) != 1:
        return (), None, None
    labels, card = next(iter(answers))
    return labels, card, None


# --- price parity ----------------------------------------------------------------------------

# Bounded on purpose: `Decimal("1e999999999")` is a denial of service, and an amount with more
# than six decimals is not a price Shopify renders.
_AMOUNT = re.compile(r"-?\d{1,15}(?:\.\d{1,6})?")


def amount_to_minor(amount: Any, currency: Any) -> Optional[int]:
    """EXACT minor units, or None. Never 0-on-failure: a price we could not read must not become
    a price of zero, which would mint a drift out of an unreadable page (the `Number(null) is 0`
    trap). `utils.money.to_minor_units` is deliberately not used here — it answers 0 for junk and
    rounds HALF_UP, and this comparison must be exact."""
    if not isinstance(amount, str) or not isinstance(currency, str):
        return None
    text = amount.strip()
    if not _AMOUNT.fullmatch(text):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    factor = 1 if currency.strip().upper() in ZERO_DECIMAL_CURRENCIES else 100
    scaled = value * factor
    if scaled != scaled.to_integral_value():
        return None  # a sub-minor-unit price is not representable; report nothing, not a rounding
    return int(scaled)


def _line_quantity(line: Dict[str, Any]) -> Optional[int]:
    """A merchandise line's quantity as an int, or None.

    IT IS NOT A NUMBER ON THE PAGE. Measured live 2026-09-22 on all three survey merchants, a
    line's `quantity` is a constraint object —
    `{"__typename":"ProposalMerchandiseQuantityByItem","items":{"__typename":"IntValueConstraint",
    "value":1}}` — and reading it as an int silently drops EVERY line, which reads as "no price
    on this checkout" rather than as a parse failure. A bare int is still accepted in case the
    shape changes back; anything else answers None.
    """
    raw = line.get("quantity")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 1 else None
    if isinstance(raw, dict):
        items = raw.get("items")
        value = items.get("value") if isinstance(items, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value >= 1 else None
    return None


def checkout_line_price(body: str, variant_id: str) -> Tuple[Optional[int], Optional[str]]:
    """`(unit_price_minor, currency)` for `variant_id`'s merchandise line on the landed checkout.

    Read from `merchandiseLines[].totalAmount.value` — the LINE total — divided by the line's
    `quantity`, and only when that division is exact. `(None, None)` when the line is absent,
    unreadable, or when two copies of the serialized state disagree: an ambiguous price is not a
    price, and reporting one would invent a drift.
    """
    page = html.unescape(body or "")
    want_id = f"gid://shopify/ProductVariantMerchandise/{variant_id}"
    found = set()
    for lines in _json_values_after(page, "merchandiseLines"):
        for line in lines if isinstance(lines, list) else []:
            if not isinstance(line, dict) or line.get("__typename") != "MerchandiseLine":
                continue
            merch = line.get("merchandise")
            if not isinstance(merch, dict) or merch.get("id") != want_id:
                continue
            total = line.get("totalAmount")
            money = total.get("value") if isinstance(total, dict) else None
            if not isinstance(money, dict):
                continue
            currency = money.get("currencyCode")
            minor = amount_to_minor(money.get("amount"), currency)
            quantity = _line_quantity(line)
            if minor is None or quantity is None:
                continue
            if minor % quantity:
                continue  # a unit price that is not a whole minor unit: report nothing
            found.add((minor // quantity, str(currency).strip().upper()))
    return next(iter(found)) if len(found) == 1 else (None, None)


def price_drift(
    landed_minor: Optional[int],
    landed_currency: Optional[str],
    expected_minor: Optional[int],
    expected_currency: Optional[str],
) -> Optional[int]:
    """`landed - expected` in minor units, or None when either side is unknown or the two
    currencies differ (cross-currency subtraction is not a drift, it is a category error)."""
    if landed_minor is None or expected_minor is None:
        return None
    if isinstance(expected_minor, bool) or not isinstance(expected_minor, int):
        return None
    if not landed_currency or not expected_currency:
        return None
    if landed_currency.strip().upper() != expected_currency.strip().upper():
        return None
    return landed_minor - expected_minor


def classify_landing(
    *,
    chain: List[Hop],
    final_status: int,
    body: str,
    variant_id: str,
    click_id: str,
    buyer: Optional[CartPrefill],
    market: Optional[str] = None,
    card_available: Optional[bool] = None,
    price_drift_minor: Optional[int] = None,
    detector_note: Optional[str] = None,
) -> Tuple[Verdict, Tuple[str, ...]]:
    """Pure: the verdict for a finished redirect chain, plus what an almost-checkout lacked.

    Reads only the REDACTED chain (host and path survive redaction) and the landing body.
    Order matters: a login hop wins whatever the final status is (the 406 at
    shopify.com/authentication is incidental), and ELIGIBLE is reached only by a 200 on a
    /checkouts/(cn|c|co)/ path carrying the exact variant gid and the click id — plus, when a
    buyer was given, the email and address1. With `market` (the preflight always passes it) the
    checkout's buyer country must equal it, else CHECKOUT_MARKET_MISMATCH.

    The variant and click id are read STRUCTURALLY (see `_has_variant_line`,
    `_has_click_attribute`). The email / address1 prefill check stays a presence check on the
    unescaped page; it only runs on the human-handoff path.
    """
    final_path = urlparse(chain[-1][1]).path if chain else ""
    if any(_is_login_hop(url) for _, url in chain):
        return Verdict.LOGIN_REQUIRED, ()
    if final_status == 410 and final_path.startswith("/cart/"):
        return Verdict.VARIANT_GONE, ()
    page = html.unescape(body or "")
    if final_status == 403:
        if final_path.startswith("/checkouts/") and _NOT_ACCEPTING_TEXT in page.lower():
            return Verdict.NOT_ACCEPTING_ORDERS, ()
        return Verdict.BLOCKED_UNKNOWN, ()
    if final_path.startswith("/password"):
        return Verdict.PASSWORD_PAGE, ()
    if final_status == 200 and _CHECKOUT_PATH.search(final_path):
        missing: List[str] = []
        if not _has_variant_line(page, variant_id):
            missing.append("variant")
        if not _has_click_attribute(page, click_id):
            missing.append("click_id")
        if buyer is not None:
            if not _page_contains(page, buyer.email.strip()):
                missing.append("email")
            if not _page_contains(page, buyer.address1.strip()):
                missing.append("address1")
        if "variant" in missing or "click_id" in missing:
            return Verdict.UNCLASSIFIED, tuple(missing)
        if market is not None and checkout_buyer_country(page) != market.upper():
            return Verdict.CHECKOUT_MARKET_MISMATCH, tuple(missing)
        # Both of these outrank a prefill miss: they are facts about whether this checkout can be
        # PAID, and `is False` / `!= 0` are written out so that an undetermined card (None) and a
        # parity of 0 can never fall through as a negative.
        if detector_note:
            # The accept-list was THERE and we could not trust our reading of it. That is a fact
            # about the DETECTOR, not about the merchant, so it lands where every other
            # "cannot verify" lands -- unverifiable and retryable -- and never as a negative.
            return Verdict.BLOCKED_UNKNOWN, tuple(missing)
        if card_available is False:
            return Verdict.NO_CARD_PAYMENT, tuple(missing)
        if price_drift_minor is not None and price_drift_minor != 0:
            return Verdict.PRICE_DRIFT, tuple(missing)
        if missing:
            return Verdict.CHECKOUT_PREFILL_MISSING, tuple(missing)
        return Verdict.ELIGIBLE, ()
    return Verdict.UNCLASSIFIED, ()


# --- variant resolution ----------------------------------------------------------------------


@dataclass
class _Chosen:
    variant_id: str
    source: str
    variant_title: Optional[str] = None
    product_title: Optional[str] = None
    price: Optional[str] = None


@dataclass
class _Resolution:
    chosen: Optional[_Chosen] = None
    verdict: Optional[Verdict] = None
    detail: Optional[str] = None
    chain: List[Hop] = field(default_factory=list)


def _landing_wall(landing: _Landing) -> Optional[Verdict]:
    if any(_is_login_hop(url) for _, url in landing.chain):
        return Verdict.LOGIN_REQUIRED
    if landing.chain and urlparse(landing.chain[-1][1]).path.startswith("/password"):
        return Verdict.PASSWORD_PAGE
    return None


def _json_or_none(landing: _Landing) -> Any:
    if landing.status != 200 or landing.too_many_redirects:
        return None
    try:
        return json.loads(landing.body)
    except ValueError:
        return None


def _representative(product: Dict[str, Any], variant: Dict[str, Any]) -> bool:
    """For a merchant-level probe with no product named: an available, shippable, priced
    variant that is not a gift card or a free sample — those check out differently."""
    try:
        price = float(variant.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    title = str(product.get("title") or "").lower()
    return (
        variant.get("available") is True
        and variant.get("requires_shipping", True) is not False
        and price >= 5
        and "gift" not in title
        and "sample" not in title
    )


async def _resolve_from_handle(
    client: httpx.AsyncClient,
    host: str,
    handle: str,
    variant_id: Optional[str],
    market: str,
    res: _Resolution,
) -> bool:
    """True when the handle settled the question (chosen or terminal verdict); False to fall
    back to the catalog scan (only when the handle 404s and the caller named a variant).
    `available` is read for `market` (`country=` pinned on every hop)."""
    landing = await _fetch_following(
        client, f"https://{host}/products/{quote(handle, safe='')}.js", pin_params={"country": market}
    )
    res.chain.extend(landing.chain)
    wall = _landing_wall(landing)
    if wall:
        res.verdict = wall
        return True
    if landing.status == 404 and not landing.too_many_redirects:
        if variant_id:
            return False  # the handle may have been renamed; the variant can still exist
        res.verdict, res.detail = Verdict.VARIANT_GONE, "product_handle_not_found"
        return True
    payload = _json_or_none(landing)
    variants = parse_product_js(payload)
    if not isinstance(payload, dict) or not variants:
        res.verdict, res.detail = Verdict.VARIANT_UNVERIFIED, f"product_js_status_{landing.status}"
        return True
    product_title = str(payload.get("title") or "") or None
    if variant_id:
        match = [v for v in variants if v["shopify_variant_id"] == variant_id]
        if not match:
            raw = payload.get("variants")
            if isinstance(raw, list) and len(raw) >= MAX_VARIANTS:
                # parse_product_js keeps the first MAX_VARIANTS: absence past that is unproven.
                res.verdict, res.detail = Verdict.VARIANT_UNVERIFIED, "product_js_variants_truncated"
            else:
                res.verdict, res.detail = Verdict.VARIANT_GONE, "variant_not_on_product"
            return True
        pick, source = match[0], "caller"
        if not pick["available"]:
            res.verdict, res.detail = Verdict.VARIANT_UNAVAILABLE, "variant_unavailable"
            return True
    else:
        available = [v for v in variants if v["available"]]
        if not available:
            res.verdict, res.detail = Verdict.VARIANT_UNAVAILABLE, "no_available_variant_on_product"
            return True
        pick, source = available[0], "product"
    price = pick.get("price_amount")
    res.chosen = _Chosen(
        variant_id=pick["shopify_variant_id"],
        source=source,
        variant_title=pick.get("title"),
        product_title=product_title,
        price=(f"{price:.2f}" if isinstance(price, float) else None),
    )
    return True


async def _resolve_from_catalog(
    client: httpx.AsyncClient, host: str, variant_id: Optional[str], market: str, res: _Resolution
) -> None:
    """Scan `/products.json` page by page, EVERY page read for `market` (`country=` pinned on
    every hop). Following redirects matters: robinsons.com.sg 301s this path to another host,
    and the first probe mistook that for no catalog."""
    many_variants = False
    for page in range(1, MAX_CATALOG_PAGES + 1):
        landing = await _fetch_following(
            client,
            f"https://{host}/products.json?limit={CATALOG_PAGE_SIZE}&page={page}",
            # limit/page pinned like country: a redirect that drops the query would otherwise
            # serve page 1 at the default size forever and "prove" a variant absent.
            pin_params={"limit": str(CATALOG_PAGE_SIZE), "page": str(page), "country": market},
        )
        res.chain.extend(landing.chain)
        wall = _landing_wall(landing)
        if wall:
            res.verdict = wall
            return
        payload = _json_or_none(landing)
        products = payload.get("products") if isinstance(payload, dict) else None
        if not isinstance(products, list):
            res.verdict = Verdict.VARIANT_UNVERIFIED
            res.detail = f"products_json_page_{page}_status_{landing.status}"
            return
        if not products:
            break  # end of catalog
        for product in products:
            if not isinstance(product, dict):
                continue
            if len(product.get("variants") or []) >= MAX_VARIANTS:
                many_variants = True
            for variant in product.get("variants") or []:
                if not isinstance(variant, dict):
                    continue
                vid = extract_shopify_numeric_variant_id(str(variant.get("id") or ""))
                if not vid:
                    continue
                if variant_id:
                    if vid != variant_id:
                        continue
                    if variant.get("available") is not True:
                        res.verdict, res.detail = Verdict.VARIANT_UNAVAILABLE, "variant_unavailable"
                        return
                    source = "caller"
                elif _representative(product, variant):
                    source = "catalog"
                else:
                    continue
                res.chosen = _Chosen(
                    variant_id=vid,
                    source=source,
                    variant_title=(str(variant.get("title")) if variant.get("title") else None),
                    product_title=(str(product.get("title")) if product.get("title") else None),
                    price=(str(variant.get("price")) if variant.get("price") is not None else None),
                )
                return
        if len(products) < CATALOG_PAGE_SIZE:
            break
    else:
        # Cap reached with pages still full: absence is not proven.
        res.verdict, res.detail = Verdict.VARIANT_UNVERIFIED, "catalog_scan_cap"
        return
    if variant_id and many_variants:
        # A product listed with MAX_VARIANTS or more may be truncated by the storefront itself.
        res.verdict, res.detail = Verdict.VARIANT_UNVERIFIED, "catalog_variants_possibly_truncated"
    elif variant_id:
        res.verdict, res.detail = Verdict.VARIANT_GONE, "variant_not_in_catalog"
    else:
        res.verdict, res.detail = Verdict.VARIANT_UNAVAILABLE, "no_representative_variant"


async def _resolve_variant(
    client: httpx.AsyncClient,
    host: str,
    variant_id: Optional[str],
    product_handle: Optional[str],
    market: str,
) -> _Resolution:
    """Confirm the caller's variant, or pick one. NEVER substitutes a variant the caller did not
    name: a named variant that is gone or unavailable is reported as such. Without a named
    variant, a handle confines the pick to that product; with neither, the pick is the first
    representative variant in the catalog (a merchant-level probe)."""
    res = _Resolution()
    if product_handle and await _resolve_from_handle(client, host, product_handle, variant_id, market, res):
        return res
    await _resolve_from_catalog(client, host, variant_id, market, res)
    return res


# --- entry point -----------------------------------------------------------------------------


def _input_refusal(
    host: str, market: Any, variant_id: Any, product_handle: Any, quantity: Any, buyer: Any, click_id: Any
) -> Optional[str]:
    refusal = _host_refusal(host)
    if refusal:
        return refusal
    if not isinstance(market, str) or not re.fullmatch(r"[A-Za-z]{2}", market):
        return "market_not_iso2"
    if variant_id is not None and not extract_shopify_numeric_variant_id(str(variant_id)):
        return "variant_id_not_numeric"
    if product_handle is not None and not re.fullmatch(r"[^/?#\s]{1,255}", str(product_handle)):
        return "product_handle_malformed"
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY:
        return "quantity_out_of_range"
    cid = str(click_id or "")
    if not cid.strip() or len(cid) > 256 or re.search(r"[\x00-\x1f\x7f]", cid):
        return "click_id_invalid"
    if buyer is not None:
        refusal = cart_prefill_refusal(buyer)
        if refusal:
            return refusal
        # Refused, not reconciled: availability is read for `market`, the checkout ships to the
        # buyer's country, and a mismatch would answer a question nobody asked.
        if buyer.country.strip().upper() != market.upper():
            return "buyer_country_not_market"
    return None


async def preflight(
    host: str,
    *,
    market: str,
    variant_id: Optional[str] = None,
    product_handle: Optional[str] = None,
    quantity: int = 1,
    buyer: Optional[CartPrefill] = None,
    click_id: str,
    check_card: bool = False,
    expected_price_minor: Optional[int] = None,
    expected_currency: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> PreflightResult:
    """Resolve a live variant, follow its (optionally prefilled) cart permalink, classify.

    `market` (ISO-2, required) is the BUYER's market: `available` is read for it, because
    Shopify scopes availability to the market it resolves for the request. With a buyer,
    `buyer.country` must equal `market`. The Reap path passes `buyer=None` (the buyer travels
    in Reap's quote body, not in the link).

    `expected_price_minor` / `expected_currency` (optional) are OUR indexed unit price for the
    variant. When both are given and the landed line's price can be read, the exact difference is
    reported as `price_drift_minor` and any non-zero difference is PRICE_DRIFT.

    Only for hosts on Pivota's internal merchant list — see the module docstring. Creates one
    abandoned checkout per call that reaches the permalink step. Never raises for a network
    outcome: transport failures come back as TRANSPORT_ERROR with retryable=True.
    `shipping_verified` is always False.
    """
    token = _PREFLIGHT_ACTIVE.set(True)
    expected = (expected_price_minor, expected_currency, bool(check_card))
    try:
        if client is not None:
            result = await _preflight(
                host, market, variant_id, product_handle, quantity, buyer, click_id, client, expected
            )
        else:
            async with httpx.AsyncClient(headers=_HEADERS, timeout=REQUEST_TIMEOUT_S) as own:
                result = await _preflight(
                    host, market, variant_id, product_handle, quantity, buyer, click_id, own, expected
                )
    finally:
        _PREFLIGHT_ACTIVE.reset(token)
    if result.verdict is Verdict.INVALID_INPUT:
        return result
    return replace(result, market=market.upper())


async def _preflight(
    raw_host: str,
    raw_market: Any,
    variant_id: Optional[str],
    product_handle: Optional[str],
    quantity: int,
    buyer: Optional[CartPrefill],
    click_id: str,
    client: httpx.AsyncClient,
    expected: Tuple[Optional[int], Optional[str], bool] = (None, None, False),
) -> PreflightResult:
    expected_price_minor, expected_currency, check_card = expected
    host = normalize_shop_host(raw_host)
    refusal = _input_refusal(host, raw_market, variant_id, product_handle, quantity, buyer, click_id)
    if refusal:
        return _done(PreflightResult(host=host, verdict=Verdict.INVALID_INPUT, detail=refusal))
    market = raw_market.upper()
    named = extract_shopify_numeric_variant_id(str(variant_id)) if variant_id is not None else None

    try:
        resolution = await _resolve_variant(client, host, named, product_handle, market)
    except _TransportFailure as exc:
        return _done(PreflightResult(
            host=host, verdict=Verdict.TRANSPORT_ERROR, retryable=True,
            resolve_chain=tuple(exc.chain), detail=f"resolve:{exc.kind}",
        ))
    except _HopRefused as exc:
        return _done(PreflightResult(
            host=host, verdict=Verdict.UNCLASSIFIED, resolve_chain=tuple(exc.chain),
            detail=f"resolve:{exc.reason}",
        ))
    resolve_chain = tuple(resolution.chain)
    chosen = resolution.chosen
    if chosen is None:
        return _done(PreflightResult(
            host=host, verdict=resolution.verdict or Verdict.UNCLASSIFIED, variant_id=named,
            resolve_chain=resolve_chain, detail=resolution.detail,
        ))

    known = dict(
        host=host,
        variant_id=chosen.variant_id,
        variant_source=chosen.source,
        variant_title=chosen.variant_title,
        product_title=chosen.product_title,
        price=chosen.price,
        resolve_chain=resolve_chain,
    )
    url = build_shopify_cart_permalink(
        shop_domain=host, variant_id=chosen.variant_id, click_id=click_id, quantity=quantity, buyer=buyer,
        country=market,
    )
    if not url:
        return _done(PreflightResult(verdict=Verdict.INVALID_INPUT, detail="permalink_refused", **known))
    logger.info("cart-link preflight start host=%s url=%s", host, redact_cart_permalink(url))

    try:
        landing = await _fetch_following(client, url)
    except _TransportFailure as exc:
        return _done(PreflightResult(
            verdict=Verdict.TRANSPORT_ERROR, retryable=True, chain=tuple(exc.chain),
            detail=f"permalink:{exc.kind}", **known,
        ))
    except _HopRefused as exc:
        return _done(PreflightResult(
            verdict=Verdict.UNCLASSIFIED, chain=tuple(exc.chain), detail=f"permalink:{exc.reason}", **known,
        ))

    final_url = landing.chain[-1][1] if landing.chain else None
    final_host = (urlparse(final_url).hostname if final_url else None) or None
    if landing.too_many_redirects:
        return _done(PreflightResult(
            verdict=Verdict.UNCLASSIFIED, chain=tuple(landing.chain), final_status=landing.status,
            final_host=final_host, final_url=final_url, detail="too_many_redirects", **known,
        ))
    # Read the payment accept-list and the line price BEFORE classifying, and only off a 200: a
    # 403 challenge page or a password wall carries neither, and must stay unverifiable.
    if landing.status == 200:
        payment_methods, card_available, detector_note = checkout_payment_methods(landing.body)
        landed_price_minor, landed_currency = checkout_line_price(landing.body, chosen.variant_id)
    else:
        payment_methods, card_available, detector_note = (), None, None
        landed_price_minor, landed_currency = None, None
    if not check_card:
        # The note only means anything to a caller that asked for the card verdict; without
        # `check_card` it must not move the Tier B lane's verdict either.
        detector_note = None
    drift = price_drift(landed_price_minor, landed_currency, expected_price_minor, expected_currency)
    verdict, missing = classify_landing(
        chain=landing.chain, final_status=landing.status, body=landing.body,
        variant_id=chosen.variant_id, click_id=click_id, buyer=buyer, market=market,
        # THE FIELDS ARE ALWAYS FILLED; ONLY THE VERDICT IS OPT-IN. `check_card` exists so that
        # adding this detector cannot change what the Tier B cart-link lane decides — that lane
        # calls `preflight` without it and keeps every verdict it had, while its rows still gain
        # the observability. The purchasability sweep passes check_card=True, behind its own dial.
        card_available=(card_available if check_card else None), price_drift_minor=drift,
        detector_note=detector_note,
    )
    checkout_country = checkout_buyer_country(landing.body) if landing.status == 200 else None
    total = _CHECKOUT_TOTAL.search(html.unescape(landing.body)) if landing.status == 200 else None
    return _done(PreflightResult(
        verdict=verdict,
        # RETRYABLE, like every other unverifiable outcome: the page is there, our reading of it
        # is not, and the next sweep should try again rather than the row ageing out as a fact.
        retryable=(verdict is Verdict.BLOCKED_UNKNOWN and bool(detector_note)),
        payment_methods=payment_methods,
        card_available=card_available,
        landed_price_minor=landed_price_minor,
        landed_currency=landed_currency,
        price_drift_minor=drift,
        chain=tuple(landing.chain),
        final_status=landing.status,
        final_host=final_host,
        final_url=final_url,
        missing=missing,
        checkout_total=(total.group(1) if total else None),
        checkout_currency=(total.group(2) if total else None),
        detail=(
            f"status_{landing.status}" if verdict is Verdict.UNCLASSIFIED
            else f"checkout_country_{checkout_country or 'unreadable'}" if verdict is Verdict.CHECKOUT_MARKET_MISMATCH
            else detector_note if detector_note and verdict is Verdict.BLOCKED_UNKNOWN
            else "no_card_in_" + ",".join(payment_methods) if verdict is Verdict.NO_CARD_PAYMENT
            else f"drift_{drift}_{landed_currency}" if verdict is Verdict.PRICE_DRIFT
            else None
        ),
        checkout_country=checkout_country,
        **known,
    ))


def _done(result: PreflightResult) -> PreflightResult:
    logger.info(
        "cart-link preflight host=%s verdict=%s retryable=%s final=%s detail=%s",
        result.host, result.verdict.value, result.retryable, result.final_url, result.detail,
    )
    return result
