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

THE ONE SIDE EFFECT. Following the permalink CREATES AN ABANDONED SHOPIFY CHECKOUT on the
merchant's store — the same side effect as every UCP probe before it. Nothing else is written,
nothing is paid, no payment step is touched. Resolving the variant reads only the public
`/products/<handle>.js` and `/products.json` endpoints.

NEVER EXPOSE THIS TO A USER-SUPPLIED OR ANONYMOUS DOMAIN. It runs only over Pivota's internal
merchant list (today: `scripts/ops/tierb_cart_link_preflight.py`, an operator tool). A route
that let a caller name the host would let an anonymous party make us create checkouts, under
our IP and our click ids, on any store they like — and would turn this into a fetcher of
arbitrary URLs besides. Redirect hops are held to https on a DNS name (no IP literals, no
localhost), but that is a floor, not a licence to widen the input.

WHAT IT CANNOT SEE: SHIPPING. Shopify's checkout page ships `deliveryLines: []` in its server
HTML and loads rates with JavaScript, so no HTTP-only check can prove a rate exists. Measured on
the same day: heartpercent.us (no delivery to the US for the item) and anua.us / skin1004.com
(qty 1 below a basket minimum, "Shipping not available") all land prefilled and therefore pass
this preflight as ELIGIBLE. `PreflightResult.shipping_verified` is always False for that reason;
shipping must be proven downstream before a buyer is sent to pay — on the Reap path that proof
is Reap's quote (`shippingOptions` + `amountBreakdown`); a quote with no shipping option is not
eligible.

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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote_plus, urljoin, urlparse, urlunparse

import httpx

from services.outbound_links_service import (
    CartPrefill,
    build_shopify_cart_permalink,
    cart_prefill_refusal,
    extract_shopify_numeric_variant_id,
    normalize_shop_host,
    redact_cart_permalink,
)
from services.shopify_variant_identity import parse_product_js

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

_CHECKOUT_PATH = re.compile(r"/checkouts/(?:cn|c|co)/")
_NOT_ACCEPTING_TEXT = "set up to receive orders"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
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
    shipping_verified: bool = field(default=False, init=False)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["verdict"] = self.verdict.value
        out["resolve_chain"] = [list(h) for h in self.resolve_chain]
        out["chain"] = [list(h) for h in self.chain]
        out["missing"] = list(self.missing)
        return out


# --- PII-safe logging ------------------------------------------------------------------------

_PREFLIGHT_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "shopify_cart_link_preflight_active", default=False
)
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+")


def _redact_text(text: str) -> str:
    return _URL_IN_TEXT.sub(lambda m: redact_cart_permalink(m.group(0)), text)


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


def _host_refusal(host: str) -> Optional[str]:
    if not host or "." not in host:
        return "host_not_a_domain"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return "host_local"
    try:
        ipaddress.ip_address(host.strip("[]"))
        return "host_is_ip_literal"
    except ValueError:
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
        for key, value in (pin_params or {}).items():
            current = _with_query_param(current, key, value)
        refusal = _hop_refusal(current)
        if refusal:
            raise _HopRefused(refusal, chain)
        request = client.build_request("GET", current, headers=_HEADERS, timeout=REQUEST_TIMEOUT_S)
        try:
            response = await client.send(request, stream=True, follow_redirects=False)
        except httpx.TransportError as exc:
            raise _TransportFailure(exc, chain) from None
        try:
            chain.append((response.status_code, redact_cart_permalink(str(response.url))))
            logger.debug("cart-link preflight hop %s %s", response.status_code, chain[-1][1])
            location = response.headers.get("location")
            if response.status_code in _REDIRECT_STATUSES and location:
                current = urljoin(str(response.url), location)
                continue
            body = await _read_bounded(response)
        except httpx.TransportError as exc:
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
    if "/customer_authentication/" in path:
        return True
    return (host == "shopify.com" or host.endswith(".shopify.com")) and path.startswith("/authentication/")


def _page_contains(text: str, value: str) -> bool:
    """Exact presence in the HTML-unescaped page. No other decoding is attempted: a value the
    page only carries in some other encoding reads as missing, which errs toward not ELIGIBLE."""
    return bool(value) and value in text


def classify_landing(
    *,
    chain: List[Hop],
    final_status: int,
    body: str,
    variant_id: str,
    click_id: str,
    buyer: Optional[CartPrefill],
) -> Tuple[Verdict, Tuple[str, ...]]:
    """Pure: the verdict for a finished redirect chain, plus what an almost-checkout lacked.

    Reads only the REDACTED chain (host and path survive redaction) and the landing body.
    Order matters: a login hop wins whatever the final status is (the 406 at
    shopify.com/authentication is incidental), and ELIGIBLE is reached only by a 200 on a
    /checkouts/(cn|c|co)/ path carrying the exact variant gid and the click id — plus, when a
    buyer was given, the email and address1.
    """
    final_path = urlparse(chain[-1][1]).path if chain else ""
    if any(_is_login_hop(url) for _, url in chain):
        return Verdict.LOGIN_REQUIRED, ()
    if final_status == 410 and final_path.startswith("/cart/"):
        return Verdict.VARIANT_GONE, ()
    page = html.unescape(body or "")
    if final_status == 403:
        if "/checkouts/" in final_path and _NOT_ACCEPTING_TEXT in page.lower():
            return Verdict.NOT_ACCEPTING_ORDERS, ()
        return Verdict.BLOCKED_UNKNOWN, ()
    if final_path.startswith("/password"):
        return Verdict.PASSWORD_PAGE, ()
    if final_status == 200 and _CHECKOUT_PATH.search(final_path):
        missing: List[str] = []
        if not re.search(rf"ProductVariant/{re.escape(variant_id)}(?!\d)", page):
            missing.append("variant")
        if not _page_contains(page, click_id):
            missing.append("click_id")
        if buyer is not None:
            if not _page_contains(page, buyer.email.strip()):
                missing.append("email")
            if not _page_contains(page, buyer.address1.strip()):
                missing.append("address1")
        if not missing:
            return Verdict.ELIGIBLE, ()
        if "variant" in missing or "click_id" in missing:
            return Verdict.UNCLASSIFIED, tuple(missing)
        return Verdict.CHECKOUT_PREFILL_MISSING, tuple(missing)
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
    for page in range(1, MAX_CATALOG_PAGES + 1):
        landing = await _fetch_following(
            client,
            f"https://{host}/products.json?limit={CATALOG_PAGE_SIZE}&page={page}",
            pin_params={"country": market},
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
    if variant_id:
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
    client: Optional[httpx.AsyncClient] = None,
) -> PreflightResult:
    """Resolve a live variant, follow its (optionally prefilled) cart permalink, classify.

    `market` (ISO-2, required) is the BUYER's market: `available` is read for it, because
    Shopify scopes availability to the market it resolves for the request. With a buyer,
    `buyer.country` must equal `market`. The Reap path passes `buyer=None` (the buyer travels
    in Reap's quote body, not in the link).

    Only for hosts on Pivota's internal merchant list — see the module docstring. Creates one
    abandoned checkout per call that reaches the permalink step. Never raises for a network
    outcome: transport failures come back as TRANSPORT_ERROR with retryable=True.
    `shipping_verified` is always False.
    """
    token = _PREFLIGHT_ACTIVE.set(True)
    try:
        if client is not None:
            result = await _preflight(host, market, variant_id, product_handle, quantity, buyer, click_id, client)
        else:
            async with httpx.AsyncClient(headers=_HEADERS, timeout=REQUEST_TIMEOUT_S) as own:
                result = await _preflight(host, market, variant_id, product_handle, quantity, buyer, click_id, own)
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
) -> PreflightResult:
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
        shop_domain=host, variant_id=chosen.variant_id, click_id=click_id, quantity=quantity, buyer=buyer
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
    verdict, missing = classify_landing(
        chain=landing.chain, final_status=landing.status, body=landing.body,
        variant_id=chosen.variant_id, click_id=click_id, buyer=buyer,
    )
    total = _CHECKOUT_TOTAL.search(html.unescape(landing.body)) if landing.status == 200 else None
    return _done(PreflightResult(
        verdict=verdict,
        chain=tuple(landing.chain),
        final_status=landing.status,
        final_host=final_host,
        final_url=final_url,
        missing=missing,
        checkout_total=(total.group(1) if total else None),
        checkout_currency=(total.group(2) if total else None),
        detail=(None if verdict is not Verdict.UNCLASSIFIED else f"status_{landing.status}"),
        **known,
    ))


def _done(result: PreflightResult) -> PreflightResult:
    logger.info(
        "cart-link preflight host=%s verdict=%s retryable=%s final=%s detail=%s",
        result.host, result.verdict.value, result.retryable, result.final_url, result.detail,
    )
    return result
