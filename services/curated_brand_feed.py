"""Curated-brand-list feed — the CLEAN primary source for catalog coverage.

Given a curated list of brand storefront domains, enumerate their products via
Shopify's PUBLIC `/products.json` and turn each into a Path-C *validated record*
(the `{pdp, offers}` shape `ingestion.ingest_validated_record` consumes). The
brand's own storefront is the authoritative PDP, so this bypasses Gemini URL
resolution entirely — it's deterministic, cheap, and carries the brand's own
title/price/image, variant **barcode (GTIN)**, and tags. The records then ingest
as depositable canonical anchors via the existing FK-order executor.

This is the "crawl the brand before they integrate" engine for the (very common)
Shopify-hosted D2C brand. Non-Shopify/failed or capped scans raise CrawlIncomplete
so callers cannot ingest a prefix as a completed catalog. PURE-ish: this module fetches public pages + builds records; the
caller runs `ingest_validated_jsonl` + `apply_ingest_plan` (gated).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import html
import json
import logging
import math
import os
import re
import time
import unicodedata
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin, urlsplit

import httpx
from http.cookiejar import CookieJar, DefaultCookiePolicy

from services.catalog_identity import validated_source_gtin
from services import storefront_currency

from services.retailer_ingest.sitemap_crawler import _looks_like_inci_list
from services import crawl_politeness, shopify_edge_pacer
from services.variant_identity import MERCHANT_ISSUED, variant_id_provenance
# Re-exported: the measured brand families and the brand comparison key live in a module with no I/O
# imports (services/brand_spelling.py) so pure matchers can use them without importing this one.
from services.brand_spelling import (  # noqa: F401 - re-exported names
    RETAILER_BRAND_CANONICAL,
    RETAILER_BRAND_SPELLINGS,
    brand_key as _brand_key,
)

logger = logging.getLogger("curated_brand_feed")


def _note_response(url: str, resp: Any) -> None:
    """Feed one storefront response back to the crawl gates -- the ONE place this module does it.

    crawl_politeness paces the next request to this host (unchanged) and, given the headers, forwards
    them to any installed IP-throttle breaker and puts their throttle diagnostics on a 429/503
    backoff line (#2473). The shared Shopify-edge pacer (#2474) learns the host that ANSWERED as
    Shopify-served from its response headers, so later requests to it share the crawl IP's budget.
    Both pacer calls are no-ops unless CRAWL_SHOPIFY_EDGE_PACER_ENABLED is on."""
    headers = getattr(resp, "headers", None)
    crawl_politeness.note_response(
        url, resp.status_code,
        retry_after=headers.get("retry-after") if headers is not None else None,
        headers=headers,
    )
    shopify_edge_pacer.learn_from_response(str(getattr(resp, "url", None) or url), headers)

_UA = "PivotaCommerceIndex/1.0 (+https://pivota.cc; catalog coverage)"
_PER_PAGE = 250  # Shopify max

# Attempts per /products.json page before a crawl gives up (CrawlIncomplete). Default 3 -- the value
# every lane has always used. A large retailer (holiholic.com, koolseoul.com: 5,000-9,000 products)
# that answers 429 once mid-crawl fails the whole run at 3, and the operator's retry starts again at
# page 1: MORE requests to the same store than waiting on the throttled page. A one-off crawl may
# raise it with CURATED_CRAWL_PAGE_ATTEMPTS; crawl_politeness still doubles its hold per consecutive
# block, so extra attempts are patience, not pressure. Capped; a malformed value refuses.
_PAGE_ATTEMPTS_DEFAULT = 3
_PAGE_ATTEMPTS_MAX = 8


def _page_attempts() -> int:
    raw = os.getenv("CURATED_CRAWL_PAGE_ATTEMPTS")
    if raw is None or not raw.strip():
        return _PAGE_ATTEMPTS_DEFAULT
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"CURATED_CRAWL_PAGE_ATTEMPTS must be an integer, got {raw!r}") from None
    if not 1 <= value <= _PAGE_ATTEMPTS_MAX:
        raise ValueError(f"CURATED_CRAWL_PAGE_ATTEMPTS must be 1..{_PAGE_ATTEMPTS_MAX}, got {value}")
    return value
# Lowest variant price (in the store's currency) that counts as a real offer. Across
# the four Meitu-US feeds measured 2026-09-05 (2,108 products) exactly one variant sat
# in (0, 1.00): the $0.01 stila promo described at the variant pick below. Nothing legitimate in a beauty D2C feed is
# priced under a dollar; a floor this low cannot drop a real product.
MIN_SELLABLE_PRICE = 1.0

# Gift-with-purchase / free-sample items a store lists with a NOMINAL price, so the $1 floor above keeps them
# (measured 2026-09-24: westman-atelier.com's $26 "Blush Stick" deluxe GWP mini, tagged `gwp`). Census
# 2026-09-24, 24,523 products across 54 US stores: these EXACT tag tokens (casefolded, whole tag) mark only
# gifts -- and a SUBSTRING match is not safe: paid products carry `solo_gwp_elta_pca`,
# `rationale_4_eye_cream_gwp`, `excludefromgwp:sitewide`. Tags that LOOK related but mark sellable products and
# must never be added here: yblocklist, searchanise_ignore, gorgias_do_not_recommend, brand promo, freesample
# (Sol de Janeiro $26 mists), sample (travel sprays), free-gifts / free_gifts (perfumania $24.95 sets),
# "sets not for sale" (sokoglam $199 routines). Measured cost of this rule on the census: 0 sellable dropped.
GIFT_WITH_PURCHASE_TAGS = frozenset({
    "gwp", "filter::type_gwp", "filter::type_sample", "_free_gift", "_free_gift_sample", "checkout-sample",
    "motivator_hidden_product", "gwp:brand", "gwp:sitewide", "subscription gwp", "gwp-choice",
    "gift with purchase",
})
# Titles that mark a paid-listed gift no tag marks: an EXACT "[FREE GIFT]" / "[FREE SAMPLE]" bracket, or an
# unbracketed "FREE GIFT" / "FREE SAMPLE" followed by a dash or colon ("FREE GIFT - SkinMedica ... Sample"),
# "... Gift with Purchase", "Loyalty Reward - ...". NOT a bare leading "Free" (ezenzia sells "Free Random
# Fragrance" at $19.99), NOT "[Free Gift Set]" -- a product SOLD WITH a gift (dodoskin "[Free Gift Set] beaund
# Nmode Pro + Booster Gel", $159). A title carrying "+" is such a bundle ("[Free Gift] 1+1 SAMJIWON ... + FREE
# medicube Mask", $79) and is always kept (_GIFT_BUNDLE).
_GIFT_WITH_PURCHASE_TITLE = re.compile(
    r"^\[\s*free\s+(?:gift|sample)s?\s*\]|^free\s+(?:gift|sample)s?\s*[-–—:]|\bgift\s+with\s+purchase\b"
    r"|^loyalty\s+reward\b", re.I)
_GIFT_BUNDLE = re.compile(r"\+")
# NOT the merchant product type: `GWP`-typed items include in-stock full-size products (elizabetharden.com
# PREVAGE set $169, night capsules $99; beautybrands $44.99 hair dryer) -- measured 2026-09-25. Known gap.


def gift_with_purchase_reason(product: Dict[str, Any]) -> Optional[str]:
    """Why this Shopify product is a gift/sample rather than something a buyer can order, or None."""
    raw = product.get("tags")
    tags = raw if isinstance(raw, list) else str(raw or "").split(",")
    tokens = {str(t).strip().casefold() for t in tags if str(t).strip()}
    hit = sorted(tokens & GIFT_WITH_PURCHASE_TAGS)
    if hit:
        return f"tag:{hit[0]}"
    title = str(product.get("title") or "").strip()
    if _GIFT_BUNDLE.search(title):
        return None  # sold WITH a gift, not a gift
    m = _GIFT_WITH_PURCHASE_TITLE.search(title)
    return f"title:{m.group(0).strip()}" if m else None

# The axis a variant varies on, named the way the shop names it. Shopify reports
# a product's axes in `options`, and `option1` is a value on the FIRST of them.
# A shop with no axis at all reports the placeholder "Title" / "Default Title",
# which names nothing.
# "Color", NOT "Shade", and the difference is not cosmetic. The renderer treats
# the two names ASYMMETRICALLY when its own keyword gate does not read the
# product as cosmetic: `shade|tone|hue|undertone` falls through to
# NON_DISPLAYABLE, while `color|colour` falls through to a working `color` axis
# (both still divert a volume-looking value to a volume axis first). Measured
# across the folded bases of the six cached brand feeds, this literal alone is
# the difference between 48 and 75 of 79 products rendering a selector — the
# axis we invent should be the one the consumer accepts.
_DEFAULT_SHADE_OPTION_NAME = "Color"
_PLACEHOLDER_OPTION_NAMES = {"title", "option", "variant", "selection", "default title"}
_SHADE_AXIS_NAMES = {"shade", "color", "colour", "tone", "hue"}


def _base_option_name(product: Dict[str, Any]) -> str:
    """The shop's own name for the product's FIRST option axis, "" if it names none."""
    # Shopify `/products.json` emits `options` as a list of dicts, and it is the
    # only writer that reaches here — the string-shaped branches this used to
    # carry were unfalsifiable by any input, so they are gone rather than left
    # as coverage nobody can earn.
    options = product.get("options")
    first = options[0] if isinstance(options, list) and options else None
    name = str(first.get("name") or "").strip() if isinstance(first, dict) else ""
    if name.lower() in _PLACEHOLDER_OPTION_NAMES:
        return ""
    return name


_SIZE_LIKE_VALUE = re.compile(
    r"""(?ix)
    ^\s*(?:
        [\d.,/]+\s*(?:ml|l|g|kg|mg|oz|fl\.?\s*oz|floz|lb|ct|count|pc|pcs|pack|x)\b
      | (?:x?\s*[\d.,]+\s*(?:ml|g|oz))
      | (?:travel|mini|deluxe|jumbo|full|full\s*size|trial|sample|refill)\s*(?:size)?
      | (?:small|medium|large|x-?large|xs|s|m|l|xl|xxl|one\s*size)
    )\s*$
    """
)


def _looks_like_a_size(value: str) -> bool:
    """A quantity, a pack, or a garment/format size — never a colour."""
    return bool(_SIZE_LIKE_VALUE.match(value or ""))


def _variant_option_name(variant: Dict[str, Any], base_option_name: str) -> str:
    """The axis THIS variant varies on — per variant, because a folded product's
    variant list is not homogeneous.

    `fold_shade_listings` appends variants taken from OTHER products (the
    per-shade listings) onto a base that keeps its own `options`. On a base whose
    real axis is Size — a foundation sold in 30ml and 50ml — naming every variant
    from `options[0]` published the shades as "Size: NC15". That is not just an
    ugly label: the renderer only demands a swatch when the axis reads as a
    shade, so a mislabelled shade also rendered without one.

    A folded-in variant is on the shade axis BY CONSTRUCTION, whatever the base
    calls its own. The base's own variants really are on the base's axis, so they
    keep it — and "" when the shop names no axis, because a guess would be a
    label the merchant never wrote.
    """
    # PRESENCE, not truthiness. The fold stamps this key with the handle it took
    # the variant from, and a shade row with an empty handle stores "" — which a
    # truthiness test reads as "not folded", handing that shade the base's own
    # axis and reproducing the mislabel this function exists to prevent.
    # The extended fold (retailer lane, `fold_shade_listings(extended=True)`) already decided the axis per base,
    # from the shop's own option names where it gave one -- on every folded-in variant, and on the parent's own
    # item it labelled (_parent_item_label). The legacy fold never stamps it.
    decided = str(variant.get(FOLD_AXIS_KEY) or "").strip()
    if decided:
        return decided
    if FOLDED_FROM_KEY in variant:
        if base_option_name.strip().lower() in _SHADE_AXIS_NAMES:
            return base_option_name
        # The fold collapses listings that differ by a TITLE SUFFIX, and a suffix
        # is not always a shade: "Fix+ - 3.4 fl oz" and "Blot Powder - Medium"
        # fold exactly like "Retro Matte Lipstick - Ruby Woo". Inventing a colour
        # axis over a quantity publishes "Color: 3.4 fl oz". When the shop has not
        # named an axis we can trust and the value reads as a size, decline —
        # naming nothing is the honest answer, and by the product-level rule the
        # whole product then serves as the bare list it was before.
        if _looks_like_a_size(str(variant.get("option1") or variant.get("title") or "")):
            return ""
        return _DEFAULT_SHADE_OPTION_NAME
    return base_option_name


def _clean_domain(domain: str) -> str:
    d = str(domain or "").strip().lower()
    d = d.replace("https://", "").replace("http://", "").rstrip("/")
    return d.split("/")[0]


def _same_storefront_host(requested: str, actual: Optional[str]) -> bool:
    """Is `actual` (a redirect chain's final host) the SAME storefront we asked?

    Only the `www.` prefix is treated as noise. A SUBDOMAIN is not: `uk.brand.com` and
    `shop.brand.com` are separate Shopify stores with their own `shop.description`, their own
    catalogue and their own currency, which is precisely the confusion a host pin exists to
    refuse. Suffix matching (`endswith(host)`) would accept both, and would additionally accept
    `evilbrand.com` for `brand.com`.
    """
    a = str(actual or "").strip().lower().rstrip(".")
    r = str(requested or "").strip().lower().rstrip(".")
    if not a or not r:
        return False
    return a.removeprefix("www.") == r.removeprefix("www.")


_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")


async def fetch_shopify_shop_locale(
    domain: str,
    *,
    timeout_s: float = 10.0,
) -> Dict[str, Optional[str]]:
    """The storefront's own currency, via the module that already reads /meta.json.

    `/products.json` carries prices but NEVER the currency they are in, so every record this
    module built was currency-less and the ingest lane stamped USD on all of them. Measured
    2026-09-06: jsmbeauty.sg prices LIP-PRESSION Glowy Tint at 3000 minor = SGD 30.00, and
    ingesting it through that lane wrote USD 30.

    DELEGATES to `services.storefront_currency.fetch_storefront_meta` rather than fetching here.
    A first draft of this function was a second, uncached reader of the same endpoint -- which is
    why a THIRD gated fetch had to be registered in this file's crawl-politeness budget. That
    module already validates the currency, caches per domain for the process lifetime, and is the
    place this knowledge belongs.

    The politeness gate is preserved by injecting the fetch: `fetch_storefront_meta` takes a
    `fetch` seam precisely so a caller can supply its own transport, so the shared gate still sees
    every request this crawl lane makes against a merchant host.

    CURRENCY ONLY, and `country` is deliberately NOT returned. `storefront_currency`'s own
    docstring records why they are different axes ("a KR/HK exporter legitimately prices in USD"),
    and measurement made the asymmetry concrete: `external_product_seeds.market` is a HARD serving
    partition -- `external_seed_search` appends `market = :market` and every serving caller passes
    DEFAULT_EXTERNAL_SEED_MARKET="US" -- so a seed stamped with the storefront's country vanishes
    from US seed search. Returning the value at all invites that mistake again.

    NEGATIVE RESULTS ARE NOT CACHED ACROSS BRANDS. `fetch_storefront_meta` caches per domain for
    the PROCESS lifetime, negatives included, and its docstring says a long-lived caller should
    clear periodically. This lane is exactly that caller (`catalog_onboard_worker` drains a queue
    with a retry budget), and a single transient timeout would otherwise pin that brand to None ->
    USD for the whole process, silently defeating the retry AND the fix. So a miss is evicted.
    """
    host = _clean_domain(domain)
    if not host:
        return {"currency": None}
    # /meta.json is a Shopify storefront endpoint: pace it on the shared Shopify-edge budget from
    # the first request (a no-op with the pacer off).
    shopify_edge_pacer.mark_shopify_host(host)

    async def _gated_fetch(url: str) -> Optional[str]:
        headers = {"User-Agent": _UA, "Accept": "application/json"}
        timeout = httpx.Timeout(timeout_s, connect=5.0)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout, headers=headers
            ) as client:
                await crawl_politeness.before_request(url, user_agent=_UA, max_wait=0)
                resp = await client.get(url)
                _note_response(url, resp)
                if not _same_storefront_host(host, getattr(getattr(resp, "url", None), "host", None)):
                    return None
                if resp.status_code != 200:
                    return None
                if "application/json" not in (resp.headers.get("content-type") or ""):
                    return None
                return resp.text
        except Exception:
            return None

    meta = await storefront_currency.fetch_storefront_meta(host, fetch=_gated_fetch)
    if not isinstance(meta, dict):
        # Evict the negative so the next brand (or a retry of this one) asks again.
        storefront_currency.clear_cache()
        return {"currency": None}
    cur = str(meta.get("currency") or "").strip().upper()
    return {"currency": cur if _ISO_CURRENCY.match(cur) else None}


def shop_storefront_identity(domain: str) -> Optional[Dict[str, Any]]:
    """WHICH store the crawl read, from the /meta.json `fetch_shopify_shop_locale` just fetched (the
    per-host cache: no second request). {name, myshopify_domain, currency, ships_to_countries}, or
    None when nothing was proven for this host.

    Recorded on the crawl report so a retailer_ingest run says which Shopify store it crawled
    (`us.frankbody.com` is `letsbefrankusa`, a separate store from `frankbody.com`'s `letsbefrank`)
    and where it ships; the lane's brand-official Tier B rule reads it. Evidence only: it never
    changes what the crawl accepts or refuses, and none of it is stamped on a row (see the
    `country` note above -- `ships_to_countries` is fulfilment reach, not a market)."""
    meta = storefront_currency.cached_meta(_clean_domain(domain))
    if not isinstance(meta, dict):
        return None
    return {k: meta.get(k) for k in ("name", "myshopify_domain", "currency", "ships_to_countries")}


class CrawlIncomplete(RuntimeError):
    """A bounded scan did not establish a complete catalog; never ingest its prefix.

    next_page is diagnostic retry position, not an ingestion checkpoint. Callers retry
    the whole read before writing, so no partial batch can become a successful job.

    `reason_code` names WHY when the cause is known (see classify_transport_failure).
    `status` is deliberately NOT derived from it: every consumer gates on
    `status == "complete"`, and an unreachable host is a failed crawl like any other.
    """
    def __init__(self, message: str, *, status: str, next_page: int,
                 scanned_products: int, selected_products: int,
                 reason_code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.next_page = next_page
        self.scanned_products = scanned_products
        self.selected_products = selected_products
        self.reason_code = reason_code

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": self.status, "next_page": self.next_page,
                               "scanned_products": self.scanned_products,
                               "selected_products": self.selected_products, "reason": str(self)}
        # Additive: existing readers key on status/reason and must not have to know this.
        if self.reason_code:
            out["reason_code"] = self.reason_code
        return out


#: Where to look first. A transport failure does not prove whose fault it is, so the
#: classification says which side the evidence points at and nothing stronger.
_BLAME_NEXT_STEP = {
    "host": "verify the storefront from a vantage outside the crawl subnet before suspecting "
            "this crawler",
    "client": "suspect this client's egress or TLS configuration before the storefront",
    "unknown": "the failure happened before any HTTP response; verify reachability from outside "
               "the crawl subnet before suspecting this crawler",
}

#: (needle, reason_code, explanation, blame). First match wins, so specific cases precede
#: general ones. Needles are matched against "TypeName: message", lowercased.
#:
#: BLAME IS EVIDENCE, NOT A VERDICT. `SSLV3_ALERT_HANDSHAKE_FAILURE` is what an edge returns
#: for a hostname it does not serve — and also what a server sends on a cipher-suite mismatch
#: with a hardened client. `services/official_domain_liveness.py` already records this class
#: of failure as `unverifiable` rather than a confirmed negative, after 213/286 brand hosts
#: answered with WAF challenges; this table keeps that posture.
_TRANSPORT_SIGNATURES: Tuple[Tuple[str, str, str, str], ...] = (
    # Client-side first: these say our own egress or trust store, and blaming the site here
    # is the same misdirection this classifier exists to remove, pointed the other way.
    ("unable to get local issuer", "client_trust_store",
     "this client could not build a trust chain for the host's certificate, which the error "
     "attributes to the LOCAL issuer store (a stale CA bundle in the image, or interception)",
     "client"),
    ("self-signed certificate", "client_trust_store",
     "the presented certificate is self-signed, which an intercepting proxy also produces",
     "client"),
    ("network is unreachable", "client_egress_unreachable",
     "this client's network could not route to the host at all", "client"),
    ("no route to host", "client_egress_unreachable",
     "this client's network has no route to the host", "client"),
    # Host-side.
    ("sslv3_alert_handshake_failure", "host_tls_refused",
     "the host's TLS layer rejected the handshake before any HTTP request, which is what an "
     "edge returns for a hostname it has no certificate for — and also what a server sends "
     "when no cipher suite is shared",
     "host"),
    ("tlsv1_alert_protocol_version", "tls_version_mismatch",
     "the host and this client share no TLS protocol version", "unknown"),
    ("certificate_verify_failed", "tls_untrusted",
     "the host's certificate did not verify (expired or wrongly issued at the host, or a "
     "trust-store problem here)", "unknown"),
    ("tlsv1_alert", "host_tls_refused", "the host rejected the TLS handshake", "host"),
    ("[ssl:", "tls_error", "the TLS handshake failed", "unknown"),
    ("ssl/tls", "tls_error", "the TLS handshake failed", "unknown"),
    ("_ssl.c", "tls_error", "the TLS handshake failed", "unknown"),
    ("name or service not known", "host_dns_unresolved", "the hostname does not resolve", "host"),
    ("nodename nor servname", "host_dns_unresolved", "the hostname does not resolve", "host"),
    ("no address associated with hostname", "host_dns_unresolved",
     "the hostname resolves to no address", "host"),
    ("temporary failure in name resolution", "dns_failure",
     "the hostname could not be resolved right now, which a resolver problem here also "
     "produces", "unknown"),
    ("getaddrinfo", "dns_failure", "the hostname could not be resolved", "unknown"),
    ("connection refused", "host_connection_refused",
     "the host refused the TCP connection", "host"),
    ("connection reset", "connection_reset",
     "the connection was reset after it was established, which rate limiting also produces",
     "unknown"),
    ("server disconnected", "connection_reset",
     "the host closed an established connection without responding", "unknown"),
)

#: Failures that are OURS by construction — a malformed request, an unsupported URL scheme,
#: our proxy, our own connection pool. Classifying these as anything about the host would
#: send an operator to check a site that was never contacted.
_CLIENT_SIDE_TYPES = (
    httpx.LocalProtocolError,
    httpx.UnsupportedProtocol,
    httpx.ProxyError,
    httpx.PoolTimeout,
)


def classify_transport_failure(exc: BaseException) -> Optional[Dict[str, str]]:
    """{reason_code, explanation, blame} for a transport failure, else None.

    A raw `ConnectError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]` on page 1 reads as a bug in
    this crawler; it usually means the storefront did not answer. Not naming that cost a day
    of diagnosis on the 2026-09-15 A'PIEU canary. But the opposite error is just as costly:
    a proxy failure or a stale CA bundle blamed on the merchant sends an operator to check a
    site that is perfectly healthy. So this reports WHERE THE EVIDENCE POINTS (`blame`), never
    a verdict, and returns None for failures that are ours by construction — those keep the
    raw error, which is the right thing to read when the bug is here.

    This NEVER changes control flow: an unreachable host is still an incomplete crawl that
    must not ingest a partial prefix, and the retry budget is untouched — a site that is down
    now can answer on the next attempt.
    """
    if isinstance(exc, _CLIENT_SIDE_TYPES):
        return None
    if not isinstance(exc, httpx.TransportError):
        return None
    text = f"{type(exc).__name__}: {exc}".lower()
    for needle, code, explanation, blame in _TRANSPORT_SIGNATURES:
        if needle in text:
            return {"reason_code": code, "explanation": explanation, "blame": blame}
    if isinstance(exc, httpx.TimeoutException):
        return {"reason_code": "timeout",
                "explanation": "no response arrived within the request timeout",
                "blame": "unknown"}
    return {"reason_code": "transport_failure",
            "explanation": "the connection attempt failed before any HTTP response",
            "blame": "unknown"}


class ShopifyProductBatch(list):
    """List-compatible result; successful results always represent an exhausted feed -- of the
    store, or (scope "collection:<handle>") of one collection only, never claimed for the store.
    `presented` is the crawl's own `cart_currency` evidence (`_PresentedCurrency.report()`), when read."""
    def __init__(self, products: list, *, scanned_products: int, pages: int, scope: str = "store",
                 presented: Optional[Dict[str, Any]] = None):
        super().__init__(products)
        self.crawl_report = {"status": "complete", "scanned_products": scanned_products,
                             "selected_products": len(products), "pages": pages}
        if scope != "store":
            self.crawl_report["scope"] = scope
        if presented is not None:
            self.crawl_report["presented_currency"] = presented


class _PresentedCurrency:
    """The currency each `/products.json` page was PRESENTED in, from the `cart_currency` Set-Cookie of
    that page's own response (services/shopify_presentment: the one rule for "which currency was this
    read in"). The body carries prices but no currency, and `/meta.json` names the shop's BASE currency,
    not the one a given requester is shown: Shopify presents by the requester's geography and the store's
    Markets setup. ADR-026 (Appendix B, 2026-10-11) measured one served store presenting our crawl
    egress a foreign currency while its /meta.json named USD -- a crawl would have labelled those amounts
    USD. `records_for_brand` refuses a crawl whose pages were presented in another currency
    (`PresentedCurrencyMismatch`). A page with no cookie proves nothing either way and is only counted."""

    def __init__(self) -> None:
        self.pages: Dict[str, int] = {}  # currency -> pages presented in it
        self.pages_without_cookie = 0
        self.problems: Dict[str, int] = {}
        #: Currency-shaped values from a response that is no proof (two different values, or e.g. `qar`): the
        #: payload's currency is unknown there, so a foreign one among them still refuses (fail closed).
        self.unproven_values: set = set()

    def note(self, resp: Any) -> None:
        from services.shopify_presentment import presentment_currency
        headers = getattr(resp, "headers", None)
        cookies = headers.get_list("set-cookie") if hasattr(headers, "get_list") else []
        currency, problem = presentment_currency(cookies)
        if currency is not None:
            self.pages[currency] = self.pages.get(currency, 0) + 1
            return
        if problem == "cookie_absent":
            self.pages_without_cookie += 1
            return
        # Two different values on one response, or a malformed one: no proof, and recorded as such.
        self.problems[problem] = self.problems.get(problem, 0) + 1
        for header in cookies:
            name, sep, rest = str(header).partition("=")
            value = rest.split(";", 1)[0].strip().upper()
            if sep and name.strip() == "cart_currency" and _ISO_CURRENCY.fullmatch(value):
                self.unproven_values.add(value)

    def merge(self, report: Optional[Dict[str, Any]]) -> None:
        if not isinstance(report, dict):
            return
        for k, v in (report.get("pages_by_currency") or {}).items():
            self.pages[k] = self.pages.get(k, 0) + int(v or 0)
        self.pages_without_cookie += int(report.get("pages_without_cookie") or 0)
        for k, v in (report.get("problems") or {}).items():
            self.problems[k] = self.problems.get(k, 0) + int(v or 0)
        self.unproven_values.update(report.get("unproven_values") or ())

    def report(self) -> Dict[str, Any]:
        return {"currencies": sorted(self.pages), "pages_by_currency": dict(sorted(self.pages.items())),
                "pages_without_cookie": self.pages_without_cookie, "problems": dict(self.problems),
                "unproven_values": sorted(self.unproven_values)}


#: Shopify serves /products.json pages 1..100 only: page 101 answers HTTP 400 (measured 2026-09-24 on
#: beautycarebag.com, japanwithlovestore.com, marissacollections.com, shopcgx.com). A store with more
#: than 100 * 250 products cannot reach its empty page; crawl its brand collections instead.
SHOPIFY_MAX_PAGES = 100
_COLLECTION_HANDLE = re.compile(r"[a-z0-9][a-z0-9_-]{0,254}")


def valid_collection_handle(handle: Any) -> bool:
    return isinstance(handle, str) and bool(_COLLECTION_HANDLE.fullmatch(handle))


async def fetch_shopify_products(
    domain: str,
    *,
    max_products: int = 500,
    timeout_s: float = 15.0,
    only_vendors: Optional[Sequence[str]] = None,
    max_scan_products: Optional[int] = None,
    collection: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Enumerate to exhaustion, selecting vendors before the product budget.

    `collection` pages /collections/<handle>/products.json instead of the whole store: the batch is
    complete for THAT collection only (crawl_report.scope) -- a store's brand collection can hold other
    vendors (the vendor filter still applies) and can miss some of the brand's products.

    max_scan_products bounds the whole retailer scan, independently of the selected
    max_products budget. A cap or failed page raises CrawlIncomplete, never returns a
    misleading partial success. Exhaustion is an EMPTY page, never a short one, so every
    crawl ends with one lookahead request. (Still an inference: a mid-catalog page whose every
    slot is a hidden product would also come back empty. Not observed.) Transient errors get paced attempts per page: three, unless the
    process sets CURATED_CRAWL_PAGE_ATTEMPTS (see _page_attempts).
    """
    host = _clean_domain(domain)
    if not host:
        raise ValueError("a storefront domain is required")
    scan_limit = max_scan_products if max_scan_products is not None else max_products
    if max_products < 1 or scan_limit < 1:
        raise ValueError("product and scan budgets must be positive")
    if only_vendors is not None and not any(str(v or "").strip() for v in only_vendors):
        raise ValueError("only_vendors cannot contain only blank values")
    if collection is not None and not valid_collection_handle(collection):
        raise ValueError(f"not a Shopify collection handle: {collection!r}")
    listing = f"/collections/{collection}/products.json" if collection else "/products.json"
    scope = f"collection:{collection}" if collection else "store"
    out: List[Dict[str, Any]] = []
    scanned = 0
    page = 1
    seen_pages: set = set()
    presented = _PresentedCurrency()
    # /products.json (and a collection's) is Shopify's storefront feed: this host is Shopify-served
    # by construction, so every page of the crawl -- the first included -- is paced on the crawl IP's
    # shared Shopify-edge budget (a no-op with CRAWL_SHOPIFY_EDGE_PACER_ENABLED off).
    # MARKED BEFORE THE ANSWER, on purpose: page 1 is where pacing matters under a throttle. The cost
    # is that a non-Shopify brand domain probed here (records_for_brand tries this first; such a
    # domain returns nothing) spends one or two requests of the shared budget and stays cached as
    # Shopify for this process. That can only slow its requests, never cause a 429 (review of #2477).
    shopify_edge_pacer.mark_shopify_host(host)
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    attempts = _page_attempts()

    def incomplete(reason: str, status: str = "failed",
                   reason_code: Optional[str] = None) -> CrawlIncomplete:
        return CrawlIncomplete(f"{host}: page {page}: {reason}", status=status,
                               next_page=page, scanned_products=scanned,
                               selected_products=len(out), reason_code=reason_code)

    try:
        # NO cookie jar: a cart_currency/localization cookie set by one response (a redirect hop included) must
        # never ride a later request, or it steers what the store presents and the page answering under it may
        # not re-send the cookie that would reveal it (`_PresentedCurrency`). Each page is its own evidence.
        no_cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers,
                                     cookies=no_cookies) as client:
            while True:
                if page > SHOPIFY_MAX_PAGES:
                    raise incomplete(
                        f"Shopify serves at most {SHOPIFY_MAX_PAGES} pages of {listing}; this listing is "
                        "larger -- crawl the brand's collection (options.collections) instead", "capped")
                url = f"https://{host}{listing}?limit={_PER_PAGE}&page={page}"
                for attempt in range(attempts):
                    await crawl_politeness.before_request(url, user_agent=_UA, max_wait=0)
                    try:
                        resp = await client.get(url)
                    except (httpx.TimeoutException, httpx.TransportError):
                        if attempt == attempts - 1:
                            raise
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    _note_response(url, resp)
                    actual_host = getattr(getattr(resp, "url", None), "host", None)
                    if not _same_storefront_host(host, actual_host):
                        # A regional/sibling store can have a different catalog and
                        # currency. Never pair its prices with this host's locale.
                        raise incomplete(f"storefront host changed to {actual_host or '(unknown)'}")
                    if resp.status_code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                        break
                    await asyncio.sleep(0.5 * (2 ** attempt))
                if resp.status_code != 200:
                    raise incomplete(f"HTTP {resp.status_code}")
                if "application/json" not in (resp.headers.get("content-type") or ""):
                    raise incomplete("storefront did not return JSON")
                presented.note(resp)
                body = resp.json()
                if not isinstance(body, dict) or not isinstance(body.get("products"), list):
                    raise incomplete("invalid products.json envelope")
                products = body["products"]
                if not all(isinstance(p, dict) for p in products):
                    raise incomplete("products.json contains a non-object product")
                if not products:
                    return ShopifyProductBatch(out, scanned_products=scanned, pages=page, scope=scope,
                                               presented=presented.report())
                fingerprint = json.dumps(products, sort_keys=True, default=str)
                if fingerprint in seen_pages:
                    raise incomplete("repeated page; pagination did not advance")
                seen_pages.add(fingerprint)
                if scanned + len(products) > scan_limit:
                    raise incomplete(f"scan budget {scan_limit} exhausted", "capped")
                scanned += len(products)
                selected = filter_products_by_vendor(products, only_vendors)
                if len(out) + len(selected) > max_products:
                    raise incomplete(f"selected-product budget {max_products} exhausted", "capped")
                out.extend(selected)
                # A short page is NOT the end of the catalog: only an empty page is. Shopify counts
                # products hidden from the storefront toward `limit`, so it serves short pages
                # mid-catalog -- measured 2026-09-24, bluemercury.com page 1 = 249 then page 2 = 250
                # (23 pages, 5,593 products). Stopping on the first short page reported "complete"
                # after 249 of them.
                page += 1
    except CrawlIncomplete:
        raise
    except Exception as exc:
        raw = f"{type(exc).__name__}: {str(exc)[:160]}"
        classified = classify_transport_failure(exc)
        if classified is None:
            raise incomplete(raw) from exc
        # ORDER IS LOAD-BEARING: code and raw error first. The queue stores this text
        # truncated (db/catalog_onboard_queue.py), and the original error is the part a
        # reader cannot reconstruct — a classification is a reading of evidence, and a
        # wrong reading has to stay checkable against what was actually raised.
        served = ""
        if scanned:
            # The host answered before this, so "could not be reached" would be false.
            served = (f" The host served {scanned} product(s) across {page - 1} page(s) "
                      f"before this, so this is a failure mid-crawl, not an unreachable host.")
        raise incomplete(
            f"{classified['reason_code']}: {raw} — {classified['explanation']}.{served} "
            f"Next: {_BLAME_NEXT_STEP[classified['blame']]}.",
            reason_code=classified["reason_code"],
        ) from exc


async def fetch_shopify_collections(
    domain: str,
    collections: Sequence[str],
    *,
    only_vendors: Optional[Sequence[str]],
    max_products: int,
    max_scan_products: int,
) -> "ShopifyProductBatch":
    """The listed brand collections of a store too large for /products.json (> 100 pages), each crawled
    to ITS empty page, vendor-filtered, and merged by product id. Complete for those collections only:
    the report's scope names them, never the store. A collection that lists none of the vendors fails
    the crawl loudly -- a mistyped or re-purposed handle must not read as "the store carries nothing"."""
    handles = list(dict.fromkeys(str(h) for h in collections))
    if not handles or not all(valid_collection_handle(h) for h in handles):
        raise ValueError(f"collections must be Shopify collection handles: {list(collections)!r}")
    merged: Dict[Any, Dict[str, Any]] = {}
    per: Dict[str, Dict[str, Any]] = {}
    scanned = pages = 0
    presented = _PresentedCurrency()
    for handle in handles:
        remaining = max_scan_products - scanned  # the scan budget bounds ALL collections together
        if remaining < 1:
            raise CrawlIncomplete(f"{_clean_domain(domain)}: scan budget {max_scan_products} exhausted before "
                                  f"collection {handle!r}", status="capped", next_page=0,
                                  scanned_products=scanned, selected_products=len(merged))
        try:
            batch = await fetch_shopify_products(domain, only_vendors=only_vendors, max_products=max_products,
                                                 max_scan_products=remaining, collection=handle)
        except CrawlIncomplete as exc:
            # Report the WHOLE crawl so far, not just this collection's share.
            exc.scanned_products += scanned
            exc.selected_products += len(merged)
            raise
        report = batch.crawl_report
        presented.merge(report.get("presented_currency"))
        per[handle] = {k: report.get(k) for k in ("pages", "scanned_products", "selected_products")}
        scanned += int(report.get("scanned_products") or 0)
        pages += int(report.get("pages") or 0)
        if not batch:
            raise CrawlIncomplete(
                f"{_clean_domain(domain)}: collection {handle!r} lists no product from vendors "
                f"{list(only_vendors or [])} ({report.get('scanned_products')} scanned)",
                status="failed", next_page=0, scanned_products=scanned, selected_products=len(merged))
        for product in batch:
            merged.setdefault(product.get("id") or product.get("handle"), product)
    if len(merged) > max_products:
        raise CrawlIncomplete(f"{_clean_domain(domain)}: selected-product budget {max_products} exhausted",
                              status="capped", next_page=0, scanned_products=scanned, selected_products=len(merged))
    out = ShopifyProductBatch(list(merged.values()), scanned_products=scanned, pages=pages,
                              scope="collections:" + ",".join(handles), presented=presented.report())
    out.crawl_report["collections"] = per
    return out


def _native_shopify_id(value: Any) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text if len(text) <= 30 and re.fullmatch(r"[0-9]+", text) and int(text) > 0 else None


def _missing_barcode(variant: Dict[str, Any]) -> bool:
    return variant.get("barcode") is None or str(variant["barcode"]).strip() == ""



async def _fetch_missing_variant_gtins(
    product: Dict[str, Any], *, domain: str, client: httpx.AsyncClient, deadline: Optional[float] = None,
    hosts: Optional[set] = None,
) -> Tuple[Dict[str, str], int]:
    """Return only validated (native variant ID -> GTIN), never detail-page copy.

    A product recovery uses at most three HTTP requests (two same-storefront
    redirects). Check each redirect before following, including www/apex aliases,
    and never visit another region/store. Price units differ on .js, so no price,
    product fields, or new variant identities may escape this helper.
    """
    requests = 0
    host = _clean_domain(domain)
    product_id = _native_shopify_id(product.get("id"))
    handle = str(product.get("handle") or "").strip()
    vendor = _vendor_token(product.get("vendor"))
    variants = product.get("variants") or []
    source_ids = [_native_shopify_id(v.get("id")) for v in variants if isinstance(v, dict)]
    wanted = {
        _native_shopify_id(v.get("id")) for v in variants
        if isinstance(v, dict) and _missing_barcode(v)
        and variant_id_provenance(str(v.get("id") or ""), product_id=product_id, handle=handle) == MERCHANT_ISSUED
    } - {None}
    if (not host or not product_id or not handle or not vendor or not wanted
            or any(source_ids.count(vid) != 1 for vid in wanted)):
        return {}, requests
    url = f"https://{host}/products/{quote(handle, safe='')}.js"
    shopify_edge_pacer.mark_shopify_host(host)  # /products/<handle>.js is a Shopify endpoint
    try:
        for redirect in range(3):
            # A request the shared Shopify-edge budget paces waits for its turn, bounded by what is left
            # of the recovery's budget: a fixed 10s refused every product once that schedule was more
            # than 10s out, while an unbounded wait slept out a 429'ing host's growing backoff product
            # after product (~16h for 200). Any other request keeps main's 10s bound, so with the pacer
            # off this is main exactly. A refusal (CrawlPaced) propagates to
            # recover_missing_variant_gtins, which stops the batch and counts why (review of #2477).
            if hosts is not None:
                hosts.add(crawl_politeness.host_of(url))  # the redirect target too: blocks are its own
            wait = _remaining_wait(deadline) if shopify_edge_pacer.applies(url) else 10.0
            await crawl_politeness.before_request(url, user_agent=_UA, max_wait=wait)
            requests += 1
            response = await client.get(url)
            _note_response(url, response)
            if not _same_storefront_host(host, getattr(getattr(response, "url", None), "host", None)):
                return {}, requests
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                target = urlsplit(urljoin(url, location or ""))
                if (not location or redirect == 2 or target.scheme != "https"
                        or target.username or target.password or target.port not in (None, 443)
                        or not _same_storefront_host(host, target.hostname)):
                    return {}, requests
                url = target.geturl()
                continue
            if response.status_code != 200:
                return {}, requests
            detail = response.json()
            if (not isinstance(detail, dict) or _native_shopify_id(detail.get("id")) != product_id
                    or str(detail.get("handle") or "").strip() != handle
                    or _vendor_token(detail.get("vendor")) != vendor
                    or not isinstance(detail.get("variants"), list)):
                return {}, requests
            detail_variants = detail["variants"]
            if not all(isinstance(v, dict) for v in detail_variants):
                return {}, requests
            detail_ids = [_native_shopify_id(v.get("id")) for v in detail_variants]
            recovered = {}
            for variant in detail_variants:
                vid = _native_shopify_id(variant.get("id"))
                if vid not in wanted or detail_ids.count(vid) != 1:
                    continue
                gtin = validated_source_gtin(variant.get("barcode"))
                if gtin:
                    recovered[vid] = gtin
            return recovered, requests
    except crawl_politeness.CrawlPaced:
        if requests:  # refused on a redirect hop: this product did ask, so it failed like any other
            return {}, requests
        raise  # refused before asking: not this product's failure; the caller stops and counts why
    except Exception as exc:
        logger.debug("GTIN recovery refused for %s/%s: %s", host, handle, type(exc).__name__)
    return {}, requests


#: A GTIN recovery never runs longer than this (seconds). Above what the unthrottled path needs at the
#: drain's pacing (200 products x CRAWL_MIN_INTERVAL_SECONDS 4 = 800s), so it bounds only a backed-up
#: Shopify-edge schedule or a slow host; the INCI step's budget (pipeline.DRAIN_PDP_INCI_BUDGET_S) is
#: the same 900s.
GTIN_RECOVERY_BUDGET_S = 900.0
#: Stop asking a host after this many 429/503s in a row: one, as on main, where the first throttle
#: ended the batch after a single request. Asking again only feeds the throttle (2026-09-30).
GTIN_RECOVERY_STOP_AFTER_BLOCKS = 1


def _clock() -> float:
    """The recovery budget's clock -- a seam, so a test can move it without moving asyncio's."""
    return time.monotonic()


def _remaining_wait(deadline: Optional[float]) -> float:
    """The longest a paced request may wait for its turn: the rest of the budget. 0 means unbounded in
    crawl_politeness, so no deadline -> 0, and an exhausted one raises rather than going unbounded."""
    if deadline is None:
        return 0
    left = deadline - _clock()
    if left <= 0:
        raise crawl_politeness.CrawlPaced("GTIN recovery budget exhausted")
    return left


async def recover_missing_variant_gtins(
    products: List[Dict[str, Any]], *, domain: str, max_fetches: int = 100,
    budget_s: Optional[float] = GTIN_RECOVERY_BUDGET_S,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Recover selected products only, before shade folding changes product identity.

    attempted/recovered/failed/capped/paced count PRODUCTS; recovered_gtins counts variant
    barcodes and http_requests includes bounded redirect hops. The batch STOPS, and the products it
    did not attempt are counted by why -- never `failed`:
      * blocked_stopped: the host, or a host it redirected to, answered GTIN_RECOVERY_STOP_AFTER_BLOCKS
        429/503s in a row;
      * budget_stopped: `budget_s` (GTIN_RECOVERY_BUDGET_S, 900s) ran out. It applies only with the
        shared Shopify-edge pacer on, bounding each paced request's wait to what is left of it;
      * paced: the crawl gate refused to wait (a Crawl-delay longer than we honour, or main's 10s
        bound on a request the shared pacer does not gate). A failed observation
    never changes its product. Each attempt has at most three requests with the
    client's ten-second timeout, paced by the shared merchant crawl gate.
    """
    if type(max_fetches) is not int or max_fetches < 0:
        raise ValueError("max_pdp_identity_fetches must be a nonnegative integer")
    copied = [dict(p, variants=[dict(v) if isinstance(v, dict) else v for v in (p.get("variants") or [])])
              for p in products]
    candidates = [p for p in copied if any(isinstance(v, dict) and _missing_barcode(v)
                                          for v in p["variants"])]
    report = {"attempted": 0, "recovered": 0, "failed": 0, "paced": 0, "budget_stopped": 0,
              "blocked_stopped": 0,
              "capped": max(0, len(candidates) - max_fetches), "recovered_gtins": 0, "http_requests": 0}
    if not candidates or not max_fetches:
        return copied, report
    async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(10.0, connect=5.0),
                                 headers={"User-Agent": _UA, "Accept": "application/json"}) as client:
        batch = candidates[:max_fetches]
        hosts = {_clean_domain(domain)}
        # The budget bounds the shared pacer's waits; with the pacer off there is none, as on main.
        deadline = _clock() + budget_s if budget_s and shopify_edge_pacer.enabled() else None
        for index, product in enumerate(batch):
            left = len(batch) - index
            if max(crawl_politeness.consecutive_blocks(h) for h in hosts) >= GTIN_RECOVERY_STOP_AFTER_BLOCKS:
                report["blocked_stopped"] = left
                logger.warning("GTIN recovery for %s stopped: the host is throttling (429/503); %d "
                               "product(s) not attempted", domain, left)
                break
            try:
                _remaining_wait(deadline)  # raises once the budget is spent, before counting an attempt
                recovered, requests = await _fetch_missing_variant_gtins(
                    product, domain=domain, client=client, deadline=deadline, hosts=hosts)
            except crawl_politeness.CrawlPaced as exc:
                # Refused before any request for this product: it is not attempted, and neither is
                # anything after it.
                budget_spent = deadline is not None and not isinstance(exc, crawl_politeness.CrawlDelayTooLong)
                key = "budget_stopped" if budget_spent else "paced"
                report[key] = left
                logger.warning("GTIN recovery for %s stopped (%s: %s); %d product(s) not attempted",
                               domain, key, type(exc).__name__, left)
                break
            report["attempted"] += 1
            report["http_requests"] += requests
            applied = 0
            for variant in product["variants"]:
                if not isinstance(variant, dict):
                    continue
                gtin = recovered.get(_native_shopify_id(variant.get("id")))
                if gtin and _missing_barcode(variant):
                    variant["barcode"] = gtin
                    applied += 1
            report["recovered_gtins"] += applied
            report["recovered" if applied else "failed"] += 1
    return copied, report


def _first(seq: Any) -> Optional[Dict[str, Any]]:
    return seq[0] if isinstance(seq, list) and seq and isinstance(seq[0], dict) else None


# <script>/<style> INNER TEXT is code, not prose — page-builder exports
# (PageFly/GemPages) routinely embed style blocks in body_html; a naive
# tag-strip would keep the CSS soup and could auto-publish it as the brand's
# words. Strip whole blocks (and comments) BEFORE the tag pass.
_HTML_BLOCK_RE = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>|<!--.*?-->")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
BODY_TEXT_MAX_LEN = 2000  # PDP prose field, not a document store

# --- body_html -> description ------------------------------------------------
# What a buyer reads. Three kinds of damage are removed here: words split by markup, entity-encoded
# markup surviving as literal tags, and store boilerplate (disclaimers, promo codes, policy text)
# that is the store's, not the product's.
#
# WORD SPLITTING. A tag is not a word break. A drop-cap theme writes
# `<span class="dropcap">D</span>ue to ...`; replacing every tag with a space served "D ue to".
# Phrasing (inline) elements render with NO gap, so they leave a mark that is resolved once all the
# markup is gone (`_inline_gap`); every other tag -- block-level elements, <br>, <img>, unknown
# ones -- is a boundary, which is what it renders as.
_INLINE_TAGS = frozenset({
    "span", "a", "b", "strong", "em", "i", "u", "font", "small", "mark",
    "abbr", "s", "strike", "del", "ins", "big", "tt", "wbr", "nobr",
})
# Sub/superscripts attach to their base: H<sub>2</sub>O, vitamin B<sub>5</sub>, 10<sup>th</sup>.
_ATTACHED_TAGS = frozenset({"sub", "sup"})
# A NUMERIC sub/superscript keeps its position: rendered as the Unicode sub/superscript characters,
# still attached. Flattened to plain digits, a footnote marker glued to its word reads as part of it --
# "Ceramide<sup>1</sup>" became "Ceramide1", "week<sup>3</sup>." became "week3." -- and a formula lost
# its subscript. Now: "Ceramide\u00b9", "week\u00b3.", "H\u2082O", "m\u00b2", "10\u2076". Only content
# that is digits (with + - = ( ) and a minus sign), or a comma list of such ("1, 2" keeps its ", "),
# is converted; anything else -- "10<sup>th</sup>", "<sup>*</sup>", "<sup>TM</sup>", a letter -- is
# attached as before. Tags nested inside ("<sup><span>1</span></sup>") are looked through.
_SCRIPT_TAG_RE = re.compile(r"(?is)<\s*(sup|sub)\b[^<>]*>(.{0,200}?)<\s*/\s*\1\s*>")
_SCRIPT_CONTENT_RE = re.compile(r"[0-9+\-=()\u2212]*[0-9][0-9+\-=()\u2212]*(?:, *[0-9+\-=()\u2212]*[0-9][0-9+\-=()\u2212]*)*")
_SUPERSCRIPT = str.maketrans("0123456789+-=()\u2212", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b\u207c\u207d\u207e\u207b")
_SUBSCRIPT = str.maketrans("0123456789+-=()\u2212", "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089\u208a\u208b\u208c\u208d\u208e\u208b")
# Every character a numeric sub/superscript can become: a safety cue right before one is still a cue.
SCRIPT_NUMERAL_CHARS = "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b\u207c\u207d\u207e" \
                       "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089\u208a\u208b\u208c\u208d\u208e"
_SCRIPT_NUMERAL_RE = re.compile(f"[{SCRIPT_NUMERAL_CHARS}]")
_TAG_NAME_RE = re.compile(r"<\s*/?\s*([A-Za-z][A-Za-z0-9]*)")
# A tag-shaped token in text that has already been unescaped: a letter right after `<` or `</`,
# and (checked in `_escaped_tag_replacement`) a real HTML element name. Narrower than _HTML_TAG_RE
# on purpose -- by then a `<` can be prose: "<5% alcohol", "results <in 3 days>".
_TAG_LIKE_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9]*)(?:\s[^<>]*)?/?>")
_HTML_ELEMENTS = frozenset(
    "p div br hr li ul ol dl dt dd h1 h2 h3 h4 h5 h6 table thead tbody tfoot tr td th caption col colgroup "
    "section article header footer nav aside main blockquote figure figcaption pre code img picture source "
    "video audio iframe embed object svg path g meta link title body html head script style noscript "
    "template form input button label select option textarea center details summary q cite time data var "
    "kbd samp address".split())
# ENTITY-ENCODED MARKUP. `&lt;br&gt;` only becomes `<br>` when unescaped, which used to happen
# AFTER the tag pass, so a literal "<br>" reached the description. Strip, unescape, and strip
# again, for up to this many layers of escaping; a last pass drops what is left.
_UNESCAPE_ROUNDS = 3
_INLINE_MARK = "\x00"     # an inline OPENING tag was removed here; `_inline_gap` decides space or nothing
_INLINE_CLOSE_MARK = "\x02"  # an inline CLOSING tag was removed here
_ATTACHED_MARK = "\x01"   # a sub/sup tag was removed here; always nothing
_MARKS = _INLINE_MARK + _INLINE_CLOSE_MARK + _ATTACHED_MARK
_MARK_RUN_RE = re.compile(r"[\x00\x01\x02]+")
_PREV_FRAGMENT_RE = re.compile(r"[^\s\x00\x01\x02]*$")
_NEXT_FRAGMENT_RE = re.compile(r"[^\s\x00\x01\x02]*")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff66-\uff9f]")


def _tag_replacement(m: "re.Match[str]") -> str:
    name = _TAG_NAME_RE.match(m.group(0))
    tag = name.group(1).lower() if name else ""
    if tag in _ATTACHED_TAGS:
        return _ATTACHED_MARK
    if tag in _INLINE_TAGS:
        return _INLINE_CLOSE_MARK if re.match(r"<\s*/", m.group(0)) else _INLINE_MARK
    return "\n"


def _script_numeral(m: "re.Match[str]") -> str:
    """A numeric <sup>/<sub> -> its Unicode characters between attached marks (no gap either side);
    any other one is left for the tag pass, which attaches it as before. Converted only when nothing
    in it could be a separator: no whitespace but the spaces after a list comma (kept: "1, 2" keeps
    its ", ", so "1 2" never reads as twelve), and no tag but an inline one (a block break inside a
    malformed sup is still a break)."""
    inner = m.group(2)
    if any(name.lower() not in _INLINE_TAGS for name in _TAG_NAME_RE.findall(inner)):
        return m.group(0)
    content = html.unescape(_HTML_TAG_RE.sub("", inner))
    if not _SCRIPT_CONTENT_RE.fullmatch(content):
        return m.group(0)
    table = _SUPERSCRIPT if m.group(1).lower() == "sup" else _SUBSCRIPT
    return _ATTACHED_MARK + content.translate(table) + _ATTACHED_MARK


def _escaped_tag_replacement(m: "re.Match[str]") -> str:
    name = m.group(1).lower()
    if name in _INLINE_TAGS or name in _ATTACHED_TAGS or name in _HTML_ELEMENTS:
        return _tag_replacement(m)
    return m.group(0)


def _is_symbol(ch: str) -> bool:
    # & and dashes, math and other symbols ("<b>Vegan</b>&amp;<b>Cruelty free</b>",
    # "<strong>Benefits</strong>– hydrates", a decorative ✨). Not currency: "$<b>20</b>".
    # The ASCII hyphen is not one: it joins a compound ("Long<b>-</b>wear", "Non<b>-comedogenic</b>").
    return ch == "&" or (ch != "-" and unicodedata.category(ch) in {"Sm", "Sk", "So", "Pd"})


def _inline_gap(m: "re.Match[str]") -> str:
    """Space or nothing where inline tags were removed. Joined where the two sides continue one
    word -- "D|ue" (a drop cap), "hydra|ting", "10:|30", "$1,|000", CJK -- and spaced where a
    theme relied on the tags to separate them: "Size|50ml", "Skin type|All", "Vegan|Cruelty-free",
    "Size:|50ml", "Vegan|&|Cruelty free", "10%|Niacinamide", and between two sibling
    elements that each hold a word: "<span>Vegan</span><span>cruelty-free</span>"."""
    run = m.group(0)
    if _ATTACHED_MARK in run:
        return ""
    s, i, j = m.string, m.start(), m.end()
    # The characters around the run, reading past any OTHER mark runs ("Vegan|,|Cruelty").
    before = "".join(c for c in s[max(0, i - 8):i] if c not in _MARKS)
    prev = before[-1:]
    prev2 = before[-2:-1]
    nxt = s[j] if j < len(s) else ""
    if not prev or not nxt or prev.isspace() or nxt.isspace():
        return ""
    if prev in ":;!?,." and prev2.isalpha() and nxt.isalnum():
        return " " if prev != "." or nxt.isupper() else ""
    if prev.islower() and nxt.isupper():
        return " "
    if (prev.isalpha() and nxt.isdigit()) or (prev.isdigit() and nxt.isalpha()):
        return " "
    if _is_symbol(prev) or _is_symbol(nxt) or (prev == "%" and nxt.isalpha()):
        return " "
    # Sibling elements -- one closes, the next opens -- each holding a word of 2+ letters. A
    # per-letter span run ("<span>G</span><span>l</span>...", gradient text) stays joined, as
    # does CJK, which is written without spaces.
    close = run.find(_INLINE_CLOSE_MARK)
    if close != -1 and _INLINE_MARK in run[close:] and prev.isalpha() and nxt.isalpha():
        prev_frag = _PREV_FRAGMENT_RE.search(s[max(0, i - 40):i]).group(0)
        next_frag = _NEXT_FRAGMENT_RE.match(s, j, j + 40).group(0)
        if (len(prev_frag) >= 2 and len(next_frag) >= 2
                and not _CJK_RE.search(prev) and not _CJK_RE.search(nxt)):
            return " "
    return ""


# SENTENCES. A sentence ends at . ! ? (or the full-width 。！？, which need no following space),
# optionally closed by a quote or bracket -- but not after a common abbreviation ("Dr. Kim",
# "No. 5", "approx. 30", "vs. 1%") or a dotted initialism ("e.g.", "i.e.", "U.S.").
_SENTENCE_END_RE = re.compile(
    r"[.!?][\"'”’)\]]*\s+|[。！？][\"'”’)\]」』]*\s*")
_ABBREVIATION_TAIL_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z])(?:dr|no|nos|mt|st|vs|approx|incl|inc|co|ltd|mr|mrs|ms|jr|sr|oz|fl|vol|fig)\.$"
    r"|(?:^|[^A-Za-z])(?:[A-Za-z]\.){2,}$")
_SENTENCE_TERMINAL = ".!?。！？"


# A boilerplate LABEL starts a new unit even without punctuation before it: "...Made in Japan Disclaimer: Due
# to photographic lighting..." -- otherwise the product's own words and the store's disclaimer are one
# sentence, and dropping the disclaimer would take "Made in Japan" with it.
_LABEL_BOUNDARY_RE = re.compile(r"\s+(?=(?:disclaimer|please\s+note)\s*:)|(?<!please)\s+(?=note\s*:)", re.IGNORECASE)


def _split_sentences(block: str) -> List[str]:
    out: List[str] = []
    for part in _LABEL_BOUNDARY_RE.split(block):
        out.extend(_split_punctuated(part))
    return out


def _split_punctuated(block: str) -> List[str]:
    out: List[str] = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(block):
        if block[m.start()] == "." and _ABBREVIATION_TAIL_RE.search(block[max(0, m.start() - 12):m.start() + 1]):
            continue
        piece = block[start:m.end()].strip()
        if piece:
            out.append(piece)
        start = m.end()
    tail = block[start:].strip()
    if tail:
        out.append(tail)
    return out


# STORE BOILERPLATE, class by class. A class is a list of rules; a rule matches when ALL of its
# patterns are found in the sentence, so each rule pairs the boilerplate's own anchor with the
# context that makes it boilerplate ("% off" only beside a store sale, "business days" only beside
# shipping, "colours may vary" only beside a monitor/screen). Loose keywords that product copy
# also uses ("takes 30% off the look of fine lines", "express delivery of hyaluronic acid",
# "follow us on our journey", "shade code: NC20") never match on their own.
#
# A matched SENTENCE (it ends in sentence punctuation) is dropped whole, up to
# _BOILERPLATE_MAX_UNIT_LEN. A run with no sentence punctuation -- a banner, a list item, an
# unpunctuated CJK block -- is dropped only when the WHOLE run is boilerplate: the matches cover at
# least _BOILERPLATE_MIN_COVERAGE of its non-space characters, or at most
# _BOILERPLATE_MAX_RESIDUAL_WORDS word is left once the matches and filler words are taken out
# (each CJK character counts as a word: CJK is written without spaces). "NOW ON SALE 25% OFF" and
# "Use code GLOW20 at checkout" go; "Cruelty-free, vegan, free shipping", "Hydrating Serum 50ml
# Free Shipping" and a CJK sentence beside a URL stay.
_BOILERPLATE_MAX_UNIT_LEN = 400
_BOILERPLATE_MIN_COVERAGE = 0.7
_BOILERPLATE_MAX_RESIDUAL_WORDS = 1
_RESIDUAL_TOKEN_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff66-\uff9f]"
    r"|[^\W_\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff66-\uff9f]+")
_BOILERPLATE_FILLER = frozenset(
    "a an the and or of to in on at by for from with is are be will may your our we you this that "
    "it its all any due please note".split())
_CURRENCY = r"[$£€]"
_SHIP_CONTEXT = r"(?i)\b(?:ship(?:s|ped|ping)?|deliver(?:y|ed|ies)?|dispatch(?:ed)?|orders?|carriers?|courier|mail(?:ed)?|transit|process(?:ed|ing)?|arrive)\b"
_SALE_CONTEXT = (r"(?i)\b(?:sale|sitewide|site-wide|storewide|store-wide|checkout|orders?|subscri\w*|"
                 r"promo|coupon|discount|today\s+only|ends|limited[- ]time)\b")
_PLATFORM = r"(?:instagram|tiktok|facebook|youtube|pinterest|twitter)"


def _rules(*rules: Sequence[str]) -> Tuple[Tuple["re.Pattern[str]", ...], ...]:
    return tuple(tuple(re.compile(p) for p in rule) for rule in rules)


_BOILERPLATE_CLASSES: Tuple[Tuple[str, Tuple[Tuple["re.Pattern[str]", ...], ...]], ...] = (
    ("shipping_returns_policy", _rules(
        (r"(?i)\bfree\s+(?:shipping|delivery)\b(?!\s+(?:kit|set|bundle|edition|collection)\b)"
         rf"(?:\s+(?:on\s+)?(?:all\s+)?(?:orders?\s+)?(?:over|above)\s+{_CURRENCY}?\d+)?",),
        (r"(?i)\b(?:standard|express|expedited|international|domestic|ground|overnight|flat[- ]rate)\s+shipping\b",),
        (r"(?i)\b(?:shipping|returns?|refunds?|exchanges?|delivery)\s+polic(?:y|ies)\b",),
        (r"(?i)\bshipping\s+(?:times?|rates?|fees?|costs?|restrictions?|charges?)\b",),
        (r"(?i)\bships?\s+(?:with)?in\s+\d+(?:\s*[-–]\s*\d+)?\s+(?:business\s+)?(?:days?|hours?|weeks?)\b(?!\s+of\b)",),
        (r"(?i)\b(?:business|working)\s+days\b", _SHIP_CONTEXT),
        (r"(?i)\bnon[- ]?(?:returnable|refundable)\b",),
        (r"(?i)\ball\s+sales\s+(?:are\s+)?final\b",),
        (r"(?i)\bfinal\s+sale\b(?!\s+of\b)",),
        (r"(?i)\b(?:returns?|exchanges?|refunds?)\s+(?:are|is)\s+not\s+(?:accepted|possible|available|offered)\b",),
        (r"(?i)\b(?:cannot|can\s*not|can't|unable\s+to)\s+(?:be\s+)?(?:ship(?:ped)?|return(?:ed)?|refund(?:ed)?|exchang(?:e|ed))\b",),
        (r"(?i)(?<!\bno\s)(?<!\bfree\sof\s)\b(?:hazardous\s+(?:materials?|goods|items?)|hazmat|dangerous\s+goods)\b",
         _SHIP_CONTEXT),
        (rf"(?i)\borders?\s+(?:over|above)\s+{_CURRENCY}?\d+",),
    )),
    ("promo_code", _rules(
        # The code itself is case-sensitive: an all-caps/digit token, as stores print them.
        (r"\b(?i:use|enter|apply|with)\s+(?i:the\s+)?(?i:(?:promo|discount|coupon|voucher)\s+)?(?i:code)\s*:?\s*[\"'“]?[A-Z0-9]{3,}\b",),
        (r"(?i)\b(?:promo|discount|coupon|voucher)\s*codes?\b",),
        (r"(?i)\b(?:bulk|wholesale)\s+(?:discounts?|pricing|prices|orders?|inquiries|enquiries|accounts?)\b",),
        (r"(?i)\bbuy(?:ing)?\s+in\s+bulk\b", r"(?i)\b(?:save|savings|discount|off)\b"),
        # Quantity-tier pricing: "10 pcs and above - 12% off", "MIX & MATCH BULK DISCOUNT".
        (r"(?i)\b\d+\s*(?:pcs|pieces|units|items|bottles|sets)\s+(?:and|&|or)\s+(?:above|more|over)\b",
         r"(?i)\b(?:off|discount|save|savings)\b"),
        (r"(?i)\bmix\s*(?:&|and)\s*match\b", r"(?i)\b(?:bulk|discount|off|save)\b"),
        (r"(?i)\bbuy\s+(?:\d+|one|two|three)\s*,?\s*get\s+(?:\d+|one|two|three)\s+(?:free|half|\d+\s*%)",),
    )),
    ("store_link", _rules(
        (r"(?i)(?:https?://|\bwww\.)\S+",),
        (r"(?i)\bcontact\s+us\b",),
        (r"(?i)\b(?:email|e-mail|call|message|dm)\s+us\s+(?:at|on|via|for|if|with)\b",),
    )),
    ("click_here", _rules(
        (r"(?i)\bclick\s+(?:here|the\s+link|below)\b",),
        (r"(?i)\btap\s+here\b",),
        (r"(?i)\bshop\s+the\s+(?:set|kit|bundle|collection|full\s+size)\s+here\b",),
    )),
    ("sale_banner", _rules(
        (r"(?i)\bnow\s+on\s+sale\b",),
        (r"(?i)\bon\s+sale\s+(?:now|today|this\s+week|until|through|for\s+a\s+limited)\b",),
        (r"(?i)\b\d{1,3}\s*%\s*off\b", _SALE_CONTEXT),
        (r"(?i)\b(?:save|enjoy)\s+(?:an?\s+)?(?:extra\s+)?\d{1,3}\s*%", _SALE_CONTEXT),
        (r"(?i)\b(?:limited[- ]time|flash|black\s+friday|cyber\s+monday|clearance)\s+(?:sale|offer|deal|discount)s?\b",),
        (r"(?i)\bsale\s+price\b",),
        (rf"(?i)\bwas\s+{_CURRENCY}\d[\d.,]*\s*,?\s*(?:now|only|today)\b",),
    )),
    ("social_embed", _rules(
        (rf"(?i)\bview\s+this\s+(?:post|photo|video|reel|profile)\s+on\s+{_PLATFORM}\b",),
        (r"(?i)\ban?\s+(?:post|photo|video|reel)\s+shared\s+by\b[^@]{0,80}@[\w.]+\)?",),
        (rf"(?i)\bfollow\s+us\s+(?:on\s+)?{_PLATFORM}\b",),
        (r"(?i)\bfollow\s+us\s+@[\w.]+",),
        (rf"(?i)\bfollow\s+(?:[^\s,.;:!?]+\s+){{0,3}}(?:on\s+)?{_PLATFORM}\b",),
        (rf"(?i)\b(?:find|join)\s+us\s+on\s+{_PLATFORM}\b",),
        (r"(?i)\btag\s+us\b", rf"(?i)@[\w.]+|#\w+|\b{_PLATFORM}\b"),
    )),
    ("colour_disclaimer", _rules((
        r"(?i)\b(?:colou?rs?|shades?)(?:\s+(?:in|of|on|shown\s+in)\s+(?:the|this|these|our)\s+"
        r"(?:images?|photos?|pictures?|photographs?))?\s+(?:may|might|can|could)\s+(?:vary|differ|appear\s+"
        r"(?:slightly\s+)?different|look\s+(?:slightly\s+)?different)\b",
        r"(?i)\bphotographic\b|\blighting\s+(?:sources?|conditions?)\b"
        r"|\b(?:monitors?|screens?|displays?|devices?|computers?)\s+(?:settings?|resolutions?|calibrations?)\b"
        r"|\bscreen\s+to\s+screen\b|\bmonitor\s+to\s+monitor\b"
        r"|\b(?:on|from)\s+(?:your|each|every|different|individual|one)\s+(?:monitors?|screens?|displays?|devices?|computers?)\b",
    ),)),
    ("packaging_note", _rules(
        (r"(?i)\b(?:please|kindly)\s+note\b",
         r"(?i)\b(?:packaging|packages?|labels?|outer\s+box(?:es)?|box\s+designs?|package\s+designs?)\b",
         r"(?i)\b(?:vary|varies|differ|differs|change[sd]?|updated?|receive|arrive)\b"),
        (r"(?i)\b(?:packaging|labels?|package\s+designs?)\s+(?:may|might)\s+(?:vary|differ|change)\b"
         r"(?!\s+colou?r)",),
    )),
)


# SAFETY TEXT IS NEVER STORE BOILERPLATE. A warning can share a sentence with a boilerplate anchor
# ("Discontinue use if irritation occurs and contact us for advice.", "Warning: flammable aerosol;
# contents under pressure, cannot be shipped by air.") and can repeat on every product of a store
# ("Avoid contact with eyes; if contact occurs, rinse thoroughly."). Neither pass may remove it: a
# sentence carrying a safety cue is kept whole by the pattern pass (only a `;`-separated clause that is
# pure boilerplate with no cue of its own is cut) and is never dropped by the repetition pass.
_SAFETY_CUE_RE = re.compile(
    r"(?i)\b(?:warnings?|cautions?|avoid|discontinue|irritat\w*|rash(?:es)?|redness|swelling|allerg\w*"
    r"|flammable|inflammable|combustible|under\s+pressure|pressuri[sz]ed|ignit\w*|doctor|physician"
    r"|dermatologist|medical|eyes?|keep\s+(?:out\s+of|away\s+from)|reach\s+of\s+children|children"
    r"|external\s+use|do\s+not\s+(?:ingest|swallow|use|apply|inhale|puncture|incinerate|spray|expose)"
    r"|patch\s+test|poison\w*|pregnan\w*|breast[- ]?feeding|nursing|broken\s+skin|sensitivity|burns?"
    r"|harmful|toxic|first\s+aid|rinse\s+(?:thoroughly|immediately|with\s+water)|seek\s+(?:medical|advice))\b")


def is_safety_text(sentence: str) -> bool:
    """True when the sentence carries a safety cue (a warning, a precaution, a medical instruction).
    A sub/superscript numeral is a word boundary here: "avoid the eyes\u00b9" carries "eyes"."""
    return bool(_SAFETY_CUE_RE.search(_SCRIPT_NUMERAL_RE.sub(" ", sentence or "")))


def _without_boilerplate(sentence: str) -> Optional[str]:
    """The sentence with its store boilerplate removed, or None when all of it is boilerplate. Safety
    text is kept: a safety sentence loses only a `;`-separated clause that is boilerplate and carries no
    safety cue of its own; without such a clause it is kept whole."""
    if boilerplate_class(sentence) is None:
        return sentence
    if not is_safety_text(sentence):
        return None
    clauses = [c.strip() for c in sentence.split(";")]
    kept = [c for c in clauses if c and (is_safety_text(c) or boilerplate_class(c) is None)]
    if len(kept) == len(clauses) or not kept:
        return sentence
    text = "; ".join(kept)
    end = sentence.rstrip()[-1:]
    if text[-1:] not in _SENTENCE_TERMINAL and end in _SENTENCE_TERMINAL:
        text += end  # the cut clause carried the sentence's full stop
    return text


def _matching_rules(sentence: str) -> List[Tuple[str, Tuple["re.Pattern[str]", ...]]]:
    return [(name, rule) for name, rules in _BOILERPLATE_CLASSES for rule in rules
            if all(p.search(sentence) for p in rule)]


def boilerplate_class(sentence: str) -> Optional[str]:
    """The store-boilerplate class of one sentence (or unpunctuated run), or None for product copy."""
    if len(sentence) > _BOILERPLATE_MAX_UNIT_LEN:
        return None
    hits = _matching_rules(sentence)
    if not hits:
        return None
    name = hits[0][0]
    if sentence.rstrip("\"'”’)]」』")[-1:] in _SENTENCE_TERMINAL:
        return name
    # Unpunctuated: boilerplate only if the matches of every rule that fired cover the run.
    covered = [False] * len(sentence)
    for _, rule in hits:
        for p in rule:
            for m in p.finditer(sentence):
                covered[m.start():m.end()] = [True] * (m.end() - m.start())
    chars = [k for k, c in enumerate(sentence) if not c.isspace()]
    if chars and sum(covered[k] for k in chars) >= _BOILERPLATE_MIN_COVERAGE * len(chars):
        return name
    residual = "".join(" " if covered[k] else c for k, c in enumerate(sentence))
    words = [w for w in _RESIDUAL_TOKEN_RE.findall(residual.casefold()) if w not in _BOILERPLATE_FILLER]
    return name if len(words) <= _BOILERPLATE_MAX_RESIDUAL_WORDS else None


def _strip_markup(body_html: str) -> str:
    text = _MARK_RUN_RE.sub("", str(body_html))
    for round_no in range(_UNESCAPE_ROUNDS):
        text = _HTML_BLOCK_RE.sub("\n", text)
        text = _SCRIPT_TAG_RE.sub(_script_numeral, text)
        # Raw markup: every `<...>` is a tag. After an unescape, only a tag-SHAPED token is.
        text = (_HTML_TAG_RE.sub(_tag_replacement, text) if round_no == 0
                else _TAG_LIKE_RE.sub(_escaped_tag_replacement, text))
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    text = _HTML_BLOCK_RE.sub("\n", text)
    text = _SCRIPT_TAG_RE.sub(_script_numeral, text)
    text = _TAG_LIKE_RE.sub(_escaped_tag_replacement, text)
    return _MARK_RUN_RE.sub(_inline_gap, text)


def body_html_sentences(body_html: Optional[str]) -> List[str]:
    """body_html -> the description's sentences, in order, store boilerplate removed.

    A "sentence" is a sentence inside a block, or a whole block when it has no sentence
    punctuation (a list item, a heading). Joined (`join_description_sentences`) they are the description,
    before the length cap. Deterministic; no LLM."""
    if not body_html:
        return []
    out: List[str] = []
    for block in _strip_markup(body_html).split("\n"):
        block = re.sub(r"\s+", " ", block).strip()
        if not block:
            continue
        for sentence in _split_sentences(block):
            kept = _without_boilerplate(sentence)
            if kept:
                out.append(kept)
    return out


_CJK_CLOSERS = "\u3002\uff01\uff1f\u300d\u300f"


def _join_sentences(sentences: Sequence[str]) -> str:
    """Single spaces between sentences, none after a full-width terminal (CJK runs on)."""
    parts: List[str] = []
    for s in sentences:
        if s:
            if parts and parts[-1][-1] not in _CJK_CLOSERS:
                parts.append(" ")
            parts.append(s)
    return "".join(parts)


_HEADING_MAX_WORDS = 5


def _is_heading_unit(unit: str) -> bool:
    """A section heading: a short unit with no sentence punctuation ("Full Ingredients", "How to use:")."""
    text = (unit or "").strip()
    if not text or text.rstrip(":").rstrip()[-1:] in _SENTENCE_TERMINAL:
        return False
    return len(text.rstrip(":").split()) <= _HEADING_MAX_WORDS


def join_description_sentences(sentences: Sequence[str], max_len: int = BODY_TEXT_MAX_LEN) -> str:
    """Join sentences into the description, capped at `max_len` on the LAST sentence boundary
    that fits, so the copy does not end mid-sentence. When that boundary would keep less than half
    of `max_len` (a short heading before one long unpunctuated block), the word boundary keeps far
    more of the copy and is used instead."""
    text = _join_sentences(sentences)
    if len(text) <= max_len:
        return text
    kept: List[str] = []
    for s in sentences:
        if s and len(_join_sentences(kept + [s])) > max_len:
            break
        if s:
            kept.append(s)
    # A cut that lands right after a section heading ("Full Ingredients", "How to use:") would end the copy on
    # the bare heading: the section it introduces did not fit. Drop such trailing headings.
    while len(kept) > 1 and _is_heading_unit(kept[-1]) and not is_safety_text(kept[-1]):
        kept.pop()  # a short SAFETY line ("For external use only", "Avoid eyes") is never a heading to drop
    head = _join_sentences(kept)
    if len(head) * 2 >= max_len:
        return head
    cut = text[:max_len]
    return cut.rsplit(" ", 1)[0] if " " in cut else cut


def body_html_to_text(body_html: Optional[str]) -> str:
    """Deterministic Shopify body_html → plain text: drop script/style/comment
    blocks, strip tags (inline ones without splitting words, the rest as a
    boundary) through up to three layers of entity-escaping, drop store-boilerplate
    sentences, collapse whitespace, cap at a sentence boundary. Output is the
    brand's own words or ''.

    Boilerplate that repeats across one store's products without matching a known
    class is removed per crawl batch, by `drop_repeated_store_sentences`."""
    return join_description_sentences(body_html_sentences(body_html))


# STORE-LEVEL REPETITION. A sentence the store prints on most of its products, as a header or a
# footer, is the store's template (a shipping block, an "about us"), not this product's copy --
# the backstop for boilerplate no pattern above names. A drain batch is ONE brand, and a product
# line's shared copy (twelve nail shades over one formula paragraph) can be on most of it, so a
# repeated sentence is dropped only when every guard below agrees it is template, not copy:
#   * it is on at least max(3, 50%) of the batch's described rows, in a batch of 10+ of them, and
#     has 5+ words (short attributes like "Vegan and cruelty free." legitimately recur);
#   * it sits in the leading or trailing run of repeated sentences -- a template's position; line
#     copy interleaved with the product's own sentences stays;
#   * it is on rows of at least 2 different product kinds (product_type, else category leaf): a
#     store template covers the whole store, while a line's copy -- twelve lipsticks over one
#     formula paragraph that never says "lipstick" -- sits on ONE kind. A single-kind batch is
#     therefore never touched by this pass;
#   * it does not name the row's own product noun (its product_type or category leaf: "nail",
#     "lipstick") -- template text is about the store, line copy is about the product;
#   * the description keeps at least max(50 chars, half its length before the pass) -- otherwise
#     the row keeps it whole. 50 is pdp_lifecycle.CANDIDATE_DESCRIPTION_MIN_LEN: the pass must
#     never be what moves a row back to draft.
DESCRIPTION_REPEAT_MIN_ROWS = 10
DESCRIPTION_REPEAT_MIN_SHARE = 0.5
DESCRIPTION_REPEAT_MIN_COUNT = 3
DESCRIPTION_REPEAT_MIN_WORDS = 5
DESCRIPTION_REPEAT_MIN_KEEP_CHARS = 50
DESCRIPTION_REPEAT_MIN_KEEP_SHARE = 0.5
DESCRIPTION_REPEAT_MIN_KINDS = 2
_GENERIC_NOUNS = frozenset(
    "beauty other misc general product products item items and the for with set sets kit kits".split())


def _sentence_key(sentence: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", sentence.casefold()))


def description_nouns(category_path: Optional[str], product_type: Optional[str]) -> FrozenSet[str]:
    """The words that name what a row IS: its product_type and the last two category_path levels."""
    parts = [str(product_type or "")] + str(category_path or "").split("/")[-2:]
    return frozenset(w for w in re.findall(r"[a-z]{3,}", " ".join(parts).casefold()) if w not in _GENERIC_NOUNS)


def description_kind(category_path: Optional[str], product_type: Optional[str]) -> str:
    """What kind of product a row is, for the repetition pass: its product_type, else its category leaf."""
    kind = " ".join(re.findall(r"[^\W_]+", str(product_type or "").casefold()))
    return kind or str(category_path or "").rsplit("/", 1)[-1].casefold()


def _names_noun(sentence: str, nouns: FrozenSet[str]) -> bool:
    return any(re.search(rf"\b{re.escape(n)}(?:e?s)?\b", sentence, re.IGNORECASE) for n in nouns)


def drop_repeated_store_sentences(
    descriptions: Sequence[Sequence[str]],
    nouns: Optional[Sequence[FrozenSet[str]]] = None,
    kinds: Optional[Sequence[str]] = None,
) -> Tuple[List[List[str]], Dict[str, Any]]:
    """Per crawl batch: drop the store's repeated header/footer sentences (see the guards above).
    `descriptions` is one sentence list per row (see `body_html_sentences`), `nouns` the matching
    `description_nouns` and `kinds` the `description_kind` per row (None: every row its own kind);
    returns the lists in the same order plus a report. Pure and deterministic."""
    rows = [list(d) for d in descriptions]
    row_nouns = list(nouns) if nouns is not None else [frozenset()] * len(rows)
    row_kinds = list(kinds) if kinds is not None else [str(k) for k in range(len(rows))]
    described = sum(1 for d in rows if d)
    report: Dict[str, Any] = {"described_rows": described, "threshold": None,
                              "repeated_sentences": 0, "rows_changed": 0, "sample": []}
    if described < DESCRIPTION_REPEAT_MIN_ROWS:
        return rows, report
    threshold = max(DESCRIPTION_REPEAT_MIN_COUNT, math.ceil(DESCRIPTION_REPEAT_MIN_SHARE * described))
    report["threshold"] = threshold
    counts: "collections.Counter[str]" = collections.Counter()
    kinds_of: Dict[str, set] = {}
    for d, kind in zip(rows, row_kinds):
        for k in {k for k in map(_sentence_key, d) if len(k.split()) >= DESCRIPTION_REPEAT_MIN_WORDS}:
            counts[k] += 1
            kinds_of.setdefault(k, set()).add(kind)
    repeated = {k for k, c in counts.items()
                if c >= threshold and len(kinds_of[k]) >= DESCRIPTION_REPEAT_MIN_KINDS}
    report["repeated_sentences"] = len(repeated)
    if not repeated:
        return rows, report
    out: List[List[str]] = []
    sample: Dict[str, str] = {}
    for d, row_noun in zip(rows, row_nouns):
        droppable = [_sentence_key(s) in repeated and not _names_noun(s, row_noun) and not is_safety_text(s)
                     for s in d]
        lead = 0
        while lead < len(d) and droppable[lead]:
            lead += 1
        tail = len(d)
        while tail > lead and droppable[tail - 1]:
            tail -= 1
        kept = d[lead:tail]
        before_len = len(join_description_sentences(d))
        after_len = len(join_description_sentences(kept))
        if len(kept) == len(d) or after_len < max(DESCRIPTION_REPEAT_MIN_KEEP_CHARS,
                                                  DESCRIPTION_REPEAT_MIN_KEEP_SHARE * before_len):
            out.append(d)
            continue
        for s in d[:lead] + d[tail:]:
            sample.setdefault(_sentence_key(s), s[:160])
        out.append(kept)
        report["rows_changed"] += 1
    report["sample"] = [sample[k] for k in sorted(sample)][:10]
    return out, report


# Brand storefronts routinely list the INCI under an "Ingredients" heading inside
# body_html. Capture it deterministically (no LLM): find the label, take the
# following text, and stop at the next section heading. Null when absent — never
# fabricated; the downstream INCI intake re-validates it parses as a real list.
_INCI_LABEL_RE = re.compile(
    r"(?is)\b(?:full\s+|all\s+|key\s+)?ingredients?\b(?:\s*list)?\s*[:\-]\s*(.{15,3000})"
)
_INCI_STOP_RE = re.compile(
    r"(?is)\b(?:how\s+to\s+use|directions|how\s+to\s+apply|usage|warnings?|caution|"
    r"about\s+the\s+brand|shipping|net\s+wt|precautions)\b"
)


def inci_from_body_html(body_html: Optional[str]) -> Optional[str]:
    """Deterministic INCI extraction from a Shopify body_html: strip tags, find an
    'Ingredients:' label, capture the following list, and cut at the next section
    heading. Returns None when there's no label or the captured text isn't a
    comma-delimited list (a single blob / prose is rejected here and again by the
    canonical INCI intake)."""
    if not body_html:
        return None
    text = _HTML_BLOCK_RE.sub(" ", str(body_html))
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    m = _INCI_LABEL_RE.search(text)
    if not m:
        return None
    tail = m.group(1).strip()
    stop = _INCI_STOP_RE.search(tail)
    if stop:
        tail = tail[: stop.start()].strip()
    tail = tail.rstrip(". ").strip()
    # A real INCI is a comma-delimited list of several ingredients, not a blurb.
    if len([p for p in tail.split(",") if p.strip()]) < 2:
        return None
    return tail or None


# --- Rendered-PDP INCI (the metafield / accordion source) ------------------
# The K-beauty cohort keeps its full INCI OUT of `/products.json` body_html — it
# lives in a Shopify product METAFIELD that the theme renders into the PDP either
# as a visible <p> inside a "Full Ingredients" popup/accordion/modal (cosrx,
# axis-y, iunik, skin1004 confirmed 2026-07-19) or as a JSON string inside a data
# island (rich-text `{"type":"text","value":"Water, ..."}`, or an escaped-HTML
# metafield string). Neither is in `/products.json`, so body_html capture
# recovered INCI for only ~4.6% of the cohort. This extractor reads the RENDERED
# PDP HTML from BOTH surfaces and recovers the list deterministically — never
# fabricated; re-validated by the canonical INCI intake before it can write.
_PDP_STRIP_RE = re.compile(r"(?is)<(script|style|template|noscript)\b.*?</\1\s*>|<!--.*?-->")
_PDP_BLOCK_BOUNDARY_RE = re.compile(r"(?is)</(p|div|li|td|section|h[1-6])>|<br\s*/?>")
# Block-level closers WITHOUT <br>, plus the <br> tag alone. Used by the join-<br>
# segmentation variant: some themes wrap ONE continuous ingredient list across
# several visual lines with <br> (even mid-ingredient-name, e.g. dasique's
# "Ethylhexyl<br/>Hydroxystearate"), so treating <br> as a break shreds the list.
# The join variant collapses <br> to a space and only breaks on real block tags.
_PDP_BLOCK_ONLY_RE = re.compile(r"(?is)</(p|div|li|td|section|h[1-6])>")
_PDP_BR_RE = re.compile(r"(?is)<br\s*/?>")
# A leading "Full Ingredients:" / "INCI —" / "[INGREDIENTS]" label sometimes shares
# the <p>/value with the list (cosrx / barr-cosmetics); strip it (incl. surrounding
# brackets/parens) so the written value is the list itself.
_PDP_INCI_LABEL_RE = re.compile(
    r"(?i)^\s*[\[(]?\s*"
    r"(?:full\s+|all\s+|key\s+|active\s+|main\s+)?"
    r"(?:ingredients?(?:\s+list)?|inci)"
    r"\s*[\])]?\s*[:\-–—]?\s*"
)
# A full INCI opens with the highest-concentration ingredient, which for an
# aqueous cosmetic is water/aqua (regulatory descending-order). Matching this is
# the FAST qualifier: it cleanly tells a full INCI from a short "key ingredients"
# highlight (which opens with an active) and rejects comma-heavy noise. But it is
# NOT required — a real full INCI can open with a non-water ingredient (a Centella
# serum whose extract outranks water; an anhydrous balm/cushion that opens with a
# wax/oil). Those qualify via `_pdp_is_full_inci`'s secondary path instead, so we
# no longer leave them null. The same regex also tests an individual comma-part for
# "is this ingredient the solvent?" (used to require water be PRESENT in the list).
_PDP_SOLVENT_OPENER_RE = re.compile(
    r"(?i)^\s*(?:purified\s+|deionized\s+|distilled\s+)?(?:aqua|water|eau)\b"
)
# JSON string literals in a data island; pre-filtered to comma-bearing strings
# that plausibly contain the solvent before the (costlier) decode+gate.
_PDP_JSON_STR_RE = re.compile(r'"((?:[^"\\]|\\.){20,8000})"')
_PDP_SOLVENT_HINT_RE = re.compile(r"(?i)(?:aqua|water|eau)")
# A full INCI lists many ingredients; a "key ingredients" highlight is short. The
# secondary (non-water-opener) qualifier requires at least this many parts so a
# short active-forward highlight can never be mistaken for a full list.
_PDP_FULL_INCI_MIN_PARTS = 10
# Shape of a real INCI opening token: a short noun phrase of chemical/botanical
# words, no sentence punctuation. Rejects a product-name/heading that a join-<br>
# pass may prepend to a list (e.g. "GLOW LAYERING FIT CUSHION (NO.17 IVORY) WATER").
_PDP_INGREDIENT_TOKEN_RE = re.compile(r"^[A-Za-z0-9 ()\-./+'&]+$")
# When several distinct full lists appear on one page they are EITHER shade variants
# of the SAME product (collapse to one) OR different products — a bundle/kit, or a
# neighbor/related-product's list carried in a recommendations JSON island (stay
# ambiguous -> None, never attribute another product's list to this page). The
# separation is tight and precision-critical: measured 2026-07-20, real shade groups
# score ingredient-set Jaccard 0.90 (misshaus cushion, min pairwise) to 0.98
# (dasique balm), while two GENUINELY DIFFERENT same-line K-beauty products that
# share a large aqueous base score only ~0.67–0.73. A 0.7 floor (the first cut of
# this fix) wrongly collapsed those neighbors and, picking the longest, PUBLISHED the
# neighbor's list. The floor is raised to 0.85 (well above the ~0.73 different-product
# ceiling, below the 0.90 shade floor) AND gated by two corroborating signals that a
# shared-base neighbor fails: identical opening ingredient and near-identical length.
_PDP_SHADE_SIM_MIN = 0.85
# Shade variants are the same formula (reordered / pigment-tail only), so their
# ingredient COUNT barely moves; a different product (toner vs serum) differs more.
_PDP_SHADE_LEN_RATIO = 1.15


def _pdp_visible_segments(page_html: str, *, join_br: bool = False) -> List[str]:
    """Block-level VISIBLE text lines: drop code/comment blocks, turn block-closers
    into newlines (so a self-contained <p> INCI is its own line), strip tags,
    unescape entities, collapse whitespace. Covers the accordion/modal/rich-text
    <div> surface.

    Two <br> modes, both run by the extractor: the default treats <br> as a line
    break (separates a product-name heading from its list — misshaus's
    `<strong>NAME</strong><br>WATER, ...`); `join_br=True` treats <br> as a space
    so a single list wrapped across many <br> lines is reassembled whole (dasique's
    `Ethylhexyl<br/>Hydroxystearate, ...`). Fragments from the break mode that are
    sub-strings of the reassembled list are dropped downstream."""
    view = _PDP_STRIP_RE.sub(" ", page_html)
    if join_br:
        view = _PDP_BR_RE.sub(" ", view)
        view = _PDP_BLOCK_ONLY_RE.sub("\n", view)
    else:
        view = _PDP_BLOCK_BOUNDARY_RE.sub("\n", view)
    view = _HTML_TAG_RE.sub(" ", view)
    view = html.unescape(view)
    out: List[str] = []
    for line in view.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            out.append(line)
    return out


def _pdp_json_island_segments(page_html: str) -> List[str]:
    """Candidate lines from JSON string literals in a data island (including inside
    <script> JSON), covering the metafield-as-JSON surface: a rich-text text node
    (`{"type":"text","value":"Water, ..."}`) or an escaped-HTML metafield string
    (`"\\u003cp\\u003eWater, ...\\u003c/p\\u003e"`). Each candidate string is
    JSON-decoded, tag-stripped, and split on rich-text paragraph breaks."""
    if not _PDP_SOLVENT_HINT_RE.search(page_html):
        return []
    out: List[str] = []
    for m in _PDP_JSON_STR_RE.finditer(page_html):
        raw = m.group(1)
        if "," not in raw or not _PDP_SOLVENT_HINT_RE.search(raw):
            continue
        try:
            decoded = json.loads('"' + raw + '"')
        except Exception:  # noqa: BLE001 — a non-JSON match just isn't a candidate
            continue
        decoded = html.unescape(_HTML_TAG_RE.sub(" ", decoded))
        for chunk in re.split(r"\n\s*\n|\r", decoded):
            chunk = re.sub(r"\s+", " ", chunk).strip()
            if chunk:
                out.append(chunk)
    return out


def _pdp_ingredient_like(part: str) -> bool:
    """Does a comma-part look like a single INCI ingredient (short chemical/botanical
    noun phrase) rather than a product-name/heading or a sentence? Guards the opener
    of a non-water-opening list so a join-<br> pass can't smuggle a leading heading
    ("GLOW LAYERING FIT CUSHION (NO.17 IVORY) WATER") in as the first "ingredient"."""
    p = part.strip()
    if not p or len(p.split()) > 6:
        return False
    if not _PDP_INGREDIENT_TOKEN_RE.match(p):
        return False
    return bool(re.search(r"[A-Za-z]{3}", p))


def _pdp_is_full_inci(cand: str) -> bool:
    """A candidate that already cleared `_looks_like_inci_list` is a FULL product
    INCI when EITHER it opens with the solvent (aqueous product, INCI descending-
    order — the fast, high-precision path) OR it is unambiguously a full list that
    happens to open with a non-water ingredient: a clean ingredient opener, MANY
    ingredients, and water/aqua present somewhere as an ingredient. The secondary
    path recovers Centella serums, balms and cushions (opener is an extract/wax/oil)
    without admitting a short "key ingredients" highlight (few parts, active opener,
    typically no water)."""
    if _PDP_SOLVENT_OPENER_RE.match(cand):
        return True
    parts = [p.strip() for p in cand.split(",") if p.strip()]
    if len(parts) < _PDP_FULL_INCI_MIN_PARTS:
        return False
    if not _pdp_ingredient_like(parts[0]):
        return False
    return any(_PDP_SOLVENT_OPENER_RE.match(p) for p in parts)


def _pdp_norm_key(text: str) -> str:
    """Alphanumeric-only lowercase fingerprint of a list (for dedup / substring tests)."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _pdp_parts(text: str) -> List[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def _pdp_solvent_opener_count(cand: str) -> int:
    """How many comma-parts are a solvent opener (water/aqua/eau at the start of the
    part). A real INCI has EXACTLY ONE solvent entry; two means two lists were
    concatenated (a kit/routine block whose per-product lists were joined across a
    bare <br>). Note the first solvent may be mid-list — a Centella serum opens with
    the extract and lists Water at #4 — so we count occurrences, not position."""
    return sum(1 for p in _pdp_parts(cand) if _PDP_SOLVENT_OPENER_RE.match(p))


def _pdp_inci_similarity(a: str, b: str) -> float:
    """Jaccard overlap of two lists' normalized ingredient SETS (order-independent —
    shade variants reorder ingredients). 1.0 = identical set, 0.0 = disjoint."""
    def _set(text: str) -> set:
        return {_pdp_norm_key(p) for p in text.split(",") if _pdp_norm_key(p)}
    sa, sb = _set(a), _set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _pdp_collapse_shades(cands: List[str]) -> Optional[str]:
    """Given >1 distinct full lists, return one ONLY when they are shade variants of
    the SAME product; else None (never guess which product's list to attribute).

    Three corroborating signals must ALL hold vs the longest (most complete) list —
    a shared-base neighbor or a bundle component fails at least one:
      * ingredient-set Jaccard >= _PDP_SHADE_SIM_MIN (near-identical formula);
      * identical opening ingredient (same #1 by concentration);
      * ingredient count within _PDP_SHADE_LEN_RATIO (a variant barely changes size).
    Comparing every candidate to the longest ref (not just the top two) means one odd
    list out of many still forces None."""
    ref = max(cands, key=len)
    ref_parts = _pdp_parts(ref)
    if not ref_parts:
        return None
    ref_opener = _pdp_norm_key(ref_parts[0])
    for cand in cands:
        if cand is ref:
            continue
        parts = _pdp_parts(cand)
        if not parts or _pdp_norm_key(parts[0]) != ref_opener:
            return None
        lo, hi = sorted((len(parts), len(ref_parts)))
        if lo == 0 or hi > lo * _PDP_SHADE_LEN_RATIO:
            return None
        if _pdp_inci_similarity(ref, cand) < _PDP_SHADE_SIM_MIN:
            return None
    return ref


def inci_from_pdp_html(page_html: Optional[str]) -> Optional[str]:
    """Deterministic INCI extraction from a RENDERED brand PDP.

    Scans the visible block text (accordion/modal/rich-text div) and the JSON data
    island (metafield rendered as a JSON string) for FULL ingredient lists. A segment
    is a candidate when it clears the strong INCI-list gate (`_looks_like_inci_list` —
    the reseller-tier safety net) AND reads as a FULL list (`_pdp_is_full_inci`) AND
    is a SINGLE list (exactly one solvent opener; two = two products concatenated).
    Then, so we never guess or fabricate:

      * exactly one distinct full INCI -> return it;
      * several that are shade variants of ONE product -> the longest (via
        `_pdp_collapse_shades`; near-identical set + opener + length);
      * several genuinely different lists (a bundle, or a neighbor/related-product's
        list in a recommendations island) -> None (never attribute another's list);
      * none -> None.

    The <br> handling is deliberately two-tier for precision. The DEFAULT visible pass
    breaks on <br>, so two products separated by a bare <br> in one block become two
    SEPARATE candidates (their ambiguity is then visible). A SECOND join-<br> pass
    reassembles a single list a theme wrapped across many <br> — but it may only
    REPAIR: a join candidate is adopted only when it strictly contains exactly ONE
    trusted (default/JSON) candidate (the <br>-shredded fragment made whole). A join
    candidate that spans TWO trusted candidates is a concatenation, not a repair, and
    is discarded — so join-<br> can never fabricate a franken list the strict passes
    didn't already see as separate.

    Whatever it returns is re-validated by
    `canonical_inci_intake.ingest_canonical_inci` before any write."""
    if not page_html:
        return None
    page_html = str(page_html)

    def _collect(segments) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for seg in segments:
            cand = _PDP_INCI_LABEL_RE.sub("", seg).strip().rstrip(". ").strip()
            if not _looks_like_inci_list(cand):
                continue
            if not _pdp_is_full_inci(cand):
                continue
            if _pdp_solvent_opener_count(cand) >= 2:
                continue  # two lists concatenated into one string -> not a single INCI
            key = _pdp_norm_key(cand)
            if key and (key not in out or len(cand) > len(out[key])):
                out[key] = cand
        return out

    # Trusted candidates come from REAL block/JSON boundaries.
    base = _collect((
        *_pdp_visible_segments(page_html),
        *_pdp_json_island_segments(page_html),
    ))
    # Reassembly candidates come from collapsing <br> to a space within a block.
    joined = _collect(_pdp_visible_segments(page_html, join_br=True))

    pool: Dict[str, str] = dict(base)
    for jkey, jcand in joined.items():
        contained = [bkey for bkey in base if bkey != jkey and bkey in jkey]
        if len(contained) >= 2:
            # The reassembled string strictly contains TWO OR MORE distinct trusted
            # lists -> it concatenated separate products (a kit/routine block), not a
            # single <br>-wrapped list. Discard it and keep the trusted lists apart so
            # the ambiguity check sees them. (Aqueous+aqueous concatenations are
            # already rejected upstream by the two-solvent-opener guard; this also
            # catches a concatenation whose second list is anhydrous.)
            continue
        # 0 or 1 trusted fragment inside: a single list the theme wrapped across <br>.
        # Adopt the reassembled whole; drop the lone shredded fragment if base saw one.
        for bkey in contained:
            pool.pop(bkey, None)
        pool[jkey] = jcand

    if not pool:
        return None
    # Same-list cleanup among trusted candidates: drop a candidate that is a contiguous
    # substring of another (e.g. a JSON-island whole vs a visible partial of the SAME
    # list). Concatenations were already rejected above, so a substring here is a
    # genuine partial of one list, never a distinct product.
    keys = list(pool)
    survivors = [
        cand
        for key, cand in pool.items()
        if not any(other != key and key in other for other in keys)
    ]
    if len(survivors) == 1:
        return survivors[0]
    return _pdp_collapse_shades(survivors)


_META_DESC_RE = re.compile(
    r"<meta\b[^>]*>",
    re.I,
)


def description_from_pdp_html(raw_html: str) -> Optional[str]:
    """Brand-authored copy from a PDP's own meta description, or None.

    WHY THIS EXISTS. `backfill_brand_official_descriptions` takes body copy from
    `/products.json` body_html, and some storefronts publish none — measured on jsmbeauty.sg
    2026-09-06: 158 of 232 products carry under 50 characters of body_html TEXT (LIP-PRESSION
    Glowy Tint's 123 characters of markup render to zero), so the whole cohort failed the 50-char
    floor. The copy is not missing from the site, only from that field: the same PDP serves 190
    characters of real prose in its meta description.

    PREFERS `name="description"`, NOT og. The og tag is frequently theme-generated; the name tag
    is the per-product SEO field a merchant fills. Measured on kyliecosmetics.com: og carried the
    STORE blurb while name carried the product's own line, so preferring og took the strictly
    worse string.

    BOILERPLATE IS NOT REJECTED HERE, and deliberately so. A theme with no per-product SEO
    description substitutes the SHOP blurb (`page_description | default: shop.description`), and
    app vendors write operational text into the name tag; both clear the 50-char floor with zero
    product information, and both are worse than staying blocked, because `is_published_ready`
    auto-publishes this lane. But neither is visible in ONE page's markup — the tell is that the
    value REPEATS across the storefront, or equals the shop's own blurb. That needs the whole
    domain, so it lives in the caller (`backfill_brand_official_descriptions.
    drop_shared_boilerplate`), against `fetch_shop_description` below.

    An earlier version of this function keyed on a PRESENT-BUT-EMPTY name tag. That is a THEME
    detail, not the substitution mechanism: Dawn-family themes wrap the name tag in
    `{% if page_description %}` and so OMIT it entirely. A 60-PDP sweep across 10 storefronts
    (2026-09-06) found the shop blurb served under an ABSENT name tag 9 times and under a
    present-but-empty one 0 times — the rule fired only on the single storefront it was derived
    from, and three parser paths (a raw `>` inside the value, a `content`-less tag, an unquoted
    value) silently disabled it even there.

    Returns None when there is nothing trustworthy — never a partial and never a guess.
    """
    if not raw_html:
        return None

    og: Optional[str] = None
    name: Optional[str] = None

    for tag in _META_DESC_RE.findall(raw_html)[:400]:
        key = ""
        for attr in ("property", "name"):
            m = re.search(rf'{attr}\s*=\s*"([^"]+)"', tag, re.I) or re.search(
                rf"{attr}\s*=\s*'([^']+)'", tag, re.I
            )
            if m:
                key = m.group(1).strip().lower()
                break
        if key not in ("og:description", "description"):
            continue
        # MATCH THE OPENING DELIMITER. A character class of both quotes ends the capture at the
        # first apostrophe inside a double-quoted attribute — measured live on kyliecosmetics.com,
        # an 88-character sentence truncated to 25 at "that's", which still cleared the 50-char
        # floor as a mid-sentence fragment. Raw apostrophes are ubiquitous in this copy.
        m = re.search(r'content\s*=\s*"([^"]*)"', tag, re.I | re.S) or re.search(
            r"content\s*=\s*'([^']*)'", tag, re.I | re.S
        )
        if not m:
            continue
        value = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
        if key == "description":
            name = name or (value or None)
        elif og is None and value:
            og = value

    if name:
        return name
    return og


async def fetch_pdp_description(
    domain: str,
    handle: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 15.0,
) -> Optional[str]:
    """Fetch one brand PDP and recover its meta description.

    Deliberately the same shape as `fetch_pdp_inci` beside it, including gating BOTH branches:
    the caller-supplied-client branch is the one a batch loop uses, so gating only the standalone
    branch would leave the high-volume path unpaced. Any failure returns None — a brand site
    hiccup must never fabricate copy or raise into a backfill loop.
    """
    host = _clean_domain(domain)
    handle = str(handle or "").strip().strip("/")
    if not host or not handle:
        return None
    url = f"https://{host}/products/{handle}"
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "text/html"}
    try:
        await crawl_politeness.before_request(url, user_agent=_UA, max_wait=0)
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout, headers=headers
            ) as c:
                resp = await c.get(url)
        _note_response(url, resp)
        if resp.status_code != 200:
            return None
        return description_from_pdp_html(resp.text)
    except Exception as exc:  # noqa: BLE001 — a brand PDP being down must not break the batch
        logger.debug("fetch_pdp_description failed for %s/%s: %s", host, handle, str(exc)[:160])
        return None


async def fetch_shop_description(
    domain: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 15.0,
) -> Optional[str]:
    """The storefront's OWN blurb, from its homepage meta description, or None.

    NOT product copy — the opposite of it, and that is the point. A Shopify theme renders
    og:description as `page_description | default: shop.description`, so every product without an
    SEO description serves THIS string on its PDP: over the 50-char floor, identical across the
    storefront, and carrying no product information. Measured 2026-09-06, it reached
    `description_from_pdp_html` unchallenged on 9 of 60 PDPs across cosrx.com, mixsoon.us and
    medicube.us.

    Fetching it once per domain turns "is this the shop blurb?" from a guess about the theme's
    markup into an exact string comparison against the blurb itself — the substitution mechanism,
    not a symptom of it. One request per domain, gated like every other call in this module.
    Returns None on any failure, which simply leaves the comparison unarmed.
    """
    host = _clean_domain(domain)
    if not host:
        return None
    url = f"https://{host}/"
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "text/html"}
    try:
        await crawl_politeness.before_request(url, user_agent=_UA, max_wait=0)
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout, headers=headers
            ) as c:
                resp = await c.get(url)
        _note_response(url, resp)
        if resp.status_code != 200:
            return None
        return description_from_pdp_html(resp.text)
    except Exception as exc:  # noqa: BLE001 — a homepage being down must not break the batch
        logger.debug("fetch_shop_description failed for %s: %s", host, str(exc)[:160])
        return None


async def fetch_shop_description_from_meta(
    domain: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 15.0,
) -> Optional[str]:
    """The storefront's OWN blurb again, read from Shopify's `/meta.json` `description`.

    THE SAME STRING AS THE HOMEPAGE META, from a different door. Measured on jsmbeauty.sg
    2026-09-08: the homepage meta description and `/meta.json` `description` are the identical
    135 characters. The homepage is a 700 KB themed page that a Cloudflare-fronted store starts
    refusing after a run has fetched sixty product pages from the same egress, while the JSON
    endpoints keep answering — which is exactly the moment the description backfill asks for the
    blurb. This is the fallback for that moment, not a replacement: `fetch_shop_description`
    stays first because a theme can override `shop.description` on the homepage, and it is the
    HOMEPAGE string a PDP without its own SEO copy repeats. Returns None on any failure.

    NOT THE SAME STRING AS THE THEME RENDERS, and the caller must be told. `/meta.json` returns
    `shop.description` RAW, while the homepage meta and the PDP og tag both pass it through the
    theme (escaping, truncation, `| append: shop.name`), so the two can differ by exactly the
    filters that make the PDP comparison an EXACT match. Callers that use this to arm an
    equality test must treat the result as unverified — see `_load_shop_blurb` in
    scripts/backfill_brand_official_descriptions.py.
    """
    host = _clean_domain(domain)
    if not host:
        return None
    url = f"https://{host}/meta.json"
    shopify_edge_pacer.mark_shopify_host(host)  # a Shopify storefront endpoint
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    try:
        await crawl_politeness.before_request(url, user_agent=_UA, max_wait=0)
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout, headers=headers
            ) as c:
                resp = await c.get(url)
        _note_response(url, resp)
        if resp.status_code != 200:
            return None
        # THE ANSWER MUST COME FROM THE HOST WE ASKED. `follow_redirects=True` with no check
        # will happily read `/meta.json` off whatever storefront the redirect chain ends on --
        # and a regional redirect (brand.com -> uk.brand.com, or an apex parked on a partner's
        # shop) lands on a DIFFERENT Shopify store with a different `shop.description`. That
        # string would then arm an equality comparison for a domain whose theme never renders
        # it: not merely useless, but the exact "non-empty blurb that matches nothing" shape
        # that switches OFF this lane's fail-closed refusal. www<->apex is the same storefront
        # and is allowed; anything else is not.
        if not _same_storefront_host(host, getattr(resp.url, "host", None)):
            logger.debug(
                "fetch_shop_description_from_meta: %s redirected off-host to %s — refusing",
                host, getattr(resp.url, "host", None),
            )
            return None
        data = resp.json()
        desc = data.get("description") if isinstance(data, dict) else None
        # A STRING OR NOTHING. `str(desc)` of a list renders `['a', 'b']` -- punctuation and all,
        # comfortably over the 50-char floor -- and a dict renders its repr. Both would be
        # published-shaped garbage rather than the shop blurb, and neither can ever equal a PDP
        # meta description, so both arrive as an unmatchable "blurb" instead of an absent one.
        if not isinstance(desc, str):
            return None
        desc = " ".join(desc.split())
        return desc or None
    except Exception as exc:  # noqa: BLE001 — same contract as fetch_shop_description
        logger.debug("fetch_shop_description_from_meta failed for %s: %s", host, str(exc)[:160])
        return None


async def fetch_pdp_inci(
    domain: str,
    handle: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout_s: float = 15.0,
) -> Optional[str]:
    """Fetch a single brand PDP and recover its INCI via `inci_from_pdp_html`.

    The polite, additive fallback for the (very common) cohort product whose
    `/products.json` body_html carries no ingredients. Returns None on any
    network/parse failure or when the PDP doesn't publish a recoverable INCI — a
    brand site hiccup must never fabricate or raise into the mint/backfill loop."""
    host = _clean_domain(domain)
    handle = str(handle or "").strip().strip("/")
    if not host or not handle:
        return None
    url = f"https://{host}/products/{handle}"
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    headers = {"User-Agent": _UA, "Accept": "text/html"}
    try:
        # Gated on BOTH branches. The caller-supplied-client branch is the one the batch loop
        # uses, so gating only the standalone branch would leave the high-volume path unpaced —
        # the shape of "a guard on one path does not cover the path that bypasses it".
        await crawl_politeness.before_request(url, user_agent=_UA, max_wait=0)
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout, headers=headers
            ) as c:
                resp = await c.get(url)
        _note_response(url, resp)
        if resp.status_code != 200:
            return None
        return inci_from_pdp_html(resp.text)
    except Exception as exc:  # noqa: BLE001 — a brand PDP being down must not break the batch
        logger.debug("fetch_pdp_inci failed for %s/%s: %s", host, handle, str(exc)[:160])
        return None


def _to_float(value: Any) -> Optional[float]:
    """Coerce Shopify's string prices (e.g. '56.00') to float; None if absent/invalid.
    Numeric columns (catalog_offers.*_price, external_product_seeds.price_amount) reject
    strings, so the mapper must hand downstream a real number or None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# A FIRST host label that names a region or a storefront role, not the store: us.sandandsky.com,
# us.frankbody.com, us.koraorganics.com (measured 2026-09-26), www./shop./store. hosts. The store's
# own name is the next label. Not stripped when the next label is a public second level
# (shop.com.sg's store label is "shop", not "com").
_HOST_PREFIX_LABELS = frozenset({"www", "shop", "store", "us", "uk", "au", "ca", "eu", "int"})
_PUBLIC_SECOND_LEVEL_LABELS = frozenset({"com", "co", "net", "org"})


def _store_host_label(domain: Optional[str]) -> str:
    """`_brand_key` of the label that names the store in `domain`, regional prefix skipped."""
    labels = _clean_domain(domain or "").split(".")
    while (len(labels) > 2 and labels[0] in _HOST_PREFIX_LABELS
           and labels[1] not in _PUBLIC_SECOND_LEVEL_LABELS):
        labels = labels[1:]
    return _brand_key(labels[0])


def _retailer_brand_family(key: str) -> Optional[str]:
    """The listed brand family for an alphanumeric brand key, or None when it is not listed."""
    return RETAILER_BRAND_SPELLINGS.get(key)


def _looks_like_a_brand_name(value: Optional[str]) -> bool:
    """Only explicit supplier-code shapes are codes; short/Unicode names are brands.

    3CE and Chinese/Korean labels are actual Meitu vendors. Length or ASCII-only
    checks cannot establish that a merchant's different label is not a brand.
    """
    raw = str(value or "").strip()
    return bool(raw) and not raw.isdecimal() and not bool(re.fullmatch(r"[A-Za-z]{1,3}-[A-Za-z]?\d{3,}", raw))


def resolve_record_brand(
    vendor: Optional[str], brand_override: Optional[str], domain: Optional[str]
) -> Tuple[str, str]:
    """Decide the brand for ONE product, returning `(brand, reason)`.

    `brand_override` is a per-DOMAIN claim by the operator; `vendor` is the storefront's
    own per-PRODUCT claim. They disagree on brand-family storefronts: misshaus.com
    publishes 125 products of which 17 carry `vendor: "APIEU"` (Able C&C owns both
    labels). Renaming those to "Missha" is not a spelling normalisation, it is an
    assertion that A'pieu's products are Missha's — and it was measured live on
    2026-09-11 doing exactly that: 15 A'pieu rows in the index branded `Missha`, which
    also made them invisible to brand-strict recall (`external_seed_brand_strict_rows: 0`
    on a search for `A'PIEU` that nonetheless returned all 15).

    So the override still wins everywhere it is a NORMALISATION, and loses where it
    would be a RELABEL:
      * no vendor              -> override           (nothing to contradict it)
      * same brand, differently spelt -> override    (`MISSHA US` -> `Missha`)
      * vendor names the STORE -> override           (`thefaceshopny` on thefaceshopny.com)
      * genuine disagreement   -> VENDOR             (`APIEU` on misshaus.com)

    Containment, not equality, decides "same brand": `missha` is a substring of
    `misshaus`. It is deliberately narrow — both sides are short brand labels, and the
    cost of a false "same" is only today's behaviour, while the cost of a false
    "different" is a wrong brand on the row. Guarded by a 3-character floor so a
    2-letter vendor cannot be a substring of half the brands in the catalogue.
    """
    v_raw = str(vendor or "").strip()
    o_raw = str(brand_override or "").strip()
    if not o_raw:
        return v_raw, "vendor_only"
    if not v_raw:
        return o_raw, "override_no_vendor"
    v, o = _brand_key(v_raw), _brand_key(o_raw)
    if not v:
        return v_raw, "vendor_disagrees"
    # Containment subsumes equality (`v == o` implies `v in o`), including below the
    # 3-char floor: two equal sub-floor keys cannot carry a 3-letter run either, so
    # they fall to `_looks_like_a_brand_name` and return the same string anyway. A
    # separate equality arm here was provably unreachable — it changed no output under
    # mutation — and is deliberately absent rather than kept as untested reassurance.
    if len(v) >= 3 and len(o) >= 3 and (v in o or o in v):
        return o_raw, "override_same_brand"
    # The same 3-char floor on the HOST label: without it the "us" of us.sandandsky.com matched
    # every vendor containing "us" -- `Sand and Sky US` by coincidence, and equally `Lush`.
    host_label = _store_host_label(domain)
    if len(host_label) >= 3 and len(v) >= 3 and (v in host_label or host_label in v):
        # The vendor field is the STORE's name, not a brand — the override is the only
        # brand claim available and is what the operator came to assert. Measured:
        # metro.com.sg publishes `vendor: "Metro Singapore Departmental Store -
        # Celebrating 69 Years in SG"`, which contains the host label and names no brand.
        return o_raw, "override_vendor_is_store"
    if not _looks_like_a_brand_name(v_raw):
        # A SUPPLIER CODE is not a brand. sukoshi.com publishes `vendor: "VC-B004"` on
        # products whose brand appears only in the title; adopting that verbatim would
        # put "VC-B004" in the brand column, which is worse than the override it
        # replaced. The override at least names a real brand.
        return o_raw, "override_vendor_is_not_a_name"
    return v_raw, "vendor_disagrees"


# Reuses PR #2158's confidence contract and distinct-path ambiguity guard. The
# source label remains enrichment_agent_v1: it is also a canonical-scope lane ID.
CATEGORY_CONFIDENCE_MERCHANT_TYPE = 0.9
CATEGORY_CONFIDENCE_EXPLICIT_TITLE = 0.8
CATEGORY_CONFIDENCE_FEED_DEFAULT = 0.3
# A leaf taken from a MEASURED (host, merchant product_type) pair. Below a merchant type
# that names its class directly (0.9): the merchant declared a shelf, the leaf was inferred
# from reading every product on it. Distinct from every other writer's value so a stored
# (label, confidence) pair can be traced to this table.
CATEGORY_CONFIDENCE_MEASURED_HOST_TYPE = 0.82  # not 0.85: the regex backfill and variant fold write 0.85

# Generic shelves are not assertions of a purchasable product class. In particular,
# the shared legacy regex maps Lip Care to balm, contradicting the measured lip oil.
_GENERIC_PRODUCT_TYPES = frozenset({
    "beauty", "cosmetics", "makeup", "make up", "skin care", "skincare",
    "face care", "hair care", "haircare", "lip care", "lip treatment", "lip treatments",
    "lip color", "lip colour",
})
_GENERIC_LIP_TYPES = frozenset({"lip care", "lip treatment", "lip treatments"})
_TOOL_NOUN_SUFFIX = re.compile(r"\bbrush(?:es)?(?:\s+#?\d{1,4})?\s*$", re.I)
# Formula names ending in an included applicator are not tool names. This is a
# noun/suffix exception, not a general pass of marketing titles through the taxonomy.
_TOOL_FORMULA_CONTEXT = re.compile(r"[+&/]|\b(?:and|with|includes?|including|for|using|built[- ]in)\b|brush[- ]on", re.I)
# ...but where the merchant TYPE is itself "<something> Brush" ("Concealer Brush", "Eyeshadow Brush,
# Cheek Brush"), the type names the tool, and "&" inside the title joins two uses of one brush
# ("double-ended shadow & liner brush"). Still refused: a formula SOLD WITH a brush ("CC undereye &
# brush", "Foundation with Brush") -- the brush is then the second thing, not the product.
_TOOL_TYPE_FORMULA_CONTEXT = re.compile(r"\b(?:with|includes?|including|for|using|built[- ]in)\b|brush[- ]on", re.I)
# ...and a title split at a joiner is a brush only when no part BEFORE the last names a product:
# "Gel Eyeliner + Angled Brush", "CC undereye & mini brush", "Concealer, Brush" are a formula and a
# brush (review of #2403).
_TOOL_TITLE_JOINER = re.compile(r"\s*(?:[+&/,]|\s-\s|\band\b|\bplus\b)\s*", re.I)
# ...and after a joiner the brush must say what it is FOR ("liner brush", "buff concealer brush",
# "cheek brush"), or "Gel Liner & Brush" / "Pomade + Angled Brush" is a formula and a bare brush.
_BRUSH_USE_WORDS = re.compile(
    r"\b(?:liner|shadow|shader|crease|smudge|smudger|blend(?:ing|er)?|buff(?:ing|er)?|detail(?:ing)?|cheek|eyes?|"
    r"lips?|brows?|face|contour(?:ing)?|sculpt(?:ing)?|highlight(?:ing|er)?|powder|foundation|concealer|"
    r"corrector|blush|bronz(?:er|ing)|kabuki|fan|stippl(?:e|ing)|complexion|setting|precision)\b", re.I)
# The leaves a makeup brush's own name may carry: its use ("concealer brush") and texture words
# ("cream foundation brush", "serum foundation brush"). Not skincare formulas ("Sunscreen Brush" is a
# sunscreen dispensed through a brush) and not nails (a "Gel Polish Brush" is not a makeup tool).
# formula nouns no leaf pattern reads on their own ("CC undereye & brush")
_BRUSH_TITLE_FORMULA_WORDS = re.compile(r"\b(?:cc|bb|creams?|serums?|oils?|balms?|sticks?|palettes?|gloss|liquids?|"
                                        r"tints?|stains?|minis?|gels?|pomades?|kohls?|pigments?|glue|wax|mousse|putty)\b", re.I)


def _is_makeup_brush_title(title: str) -> bool:
    from services.pdp_category_classifier import CATEGORY_PATTERNS
    # one space between words: a leading \s* retried across a long whitespace run is quadratic
    # (re-review of #2403: 10,000 spaces took 9.7s; the classifier fixed the same bug the same way)
    title = " ".join(title.split())
    parts = [x for x in _TOOL_TITLE_JOINER.split(title) if x.strip()]
    if not parts or not _TOOL_NOUN_SUFFIX.search(parts[-1]) or _TOOL_TYPE_FORMULA_CONTEXT.search(title):
        return False
    if len(parts) > 1 and not _BRUSH_USE_WORDS.search(_TOOL_NOUN_SUFFIX.sub(" ", parts[-1])):
        return False
    for part in parts[:-1]:
        if (_BRUSH_TITLE_FORMULA_WORDS.search(part)
                or any(pattern.search(part) for _label, _path, pattern in CATEGORY_PATTERNS)):
            return False
    named = {path for _label, path, pattern in CATEGORY_PATTERNS if pattern.search(title)}
    return all(path == "beauty/tools/brush" or path in _TEXTURE_LEAVES
               or (path.startswith("beauty/makeup/") and not path.startswith("beauty/makeup/nails/"))
               for path in named)


# Direct product nouns, not a general title classifier. These two families
# exposed swapped Stila merchant types in the 2026-09-12 source review. A hybrid
# naming both families keeps its type; shade names like "Blush" are not proof.
_EXPLICIT_PRODUCT_FAMILIES = {
    "eye_liner": re.compile(r"\beye[\s-]*liner\b", re.I),
    "cheek": re.compile(r"\b(?:cheek\s+(?:duo|stick|cream|colou?r|palette)|(?:liquid|powder|cream)\s+blush)\b", re.I),
}
_TYPE_FAMILIES = {
    "beauty/makeup/eye/eyeliner": "eye_liner",
    "beauty/makeup/face/blush": "cheek",
    "beauty/makeup/face/bronzer": "cheek",
}
_AMBIGUOUS_PRODUCT_TYPE_KEYS = frozenset({"lipglossoil", "lipglossliptint"})


# A setting spray is sprayed over the face. Measured 2026-10-10 on holiholic.com's "Setting Spray" shelf:
# it also holds Etude's "Dr. Mascara Fixer", "Bare Edge Brow Fixer", "Pang Pang Haircara Fixer" and
# A'PIEU's "Pro Curling The Black Fixer Mascara" -- fixers for lashes, brows and hair, not for a face.
# The rule is the classifier's own (setting_spray_names_another_area), so a typed shelf and a title
# are judged alike.
_SETTING_SPRAY_LEAF = "beauty/makeup/face/setting-spray"
# A shelf that names two products by design. credobeauty.com ("makeup, face, setting spray & powder") and
# unitedbeautysupply.com ("Setting Spray and Powder") shelve both; until setting spray had a leaf, the
# type read as powder alone and filed every product on it. Shelf -> (the new leaf, the shelf's other
# product). A title naming the spray and nothing else takes the spray leaf; every other title keeps the
# shelf's old answer at its old confidence, so the new leaf only ever moves sprays.
_COMBINED_SHELVES = {
    frozenset({_SETTING_SPRAY_LEAF, "beauty/makeup/face/powder"}): (_SETTING_SPRAY_LEAF, "beauty/makeup/face/powder"),
}


def _title_contradicts_product_type(title: Optional[str], path: str) -> bool:
    if path == _SETTING_SPRAY_LEAF:
        from services.pdp_category_classifier import setting_spray_names_another_area
        if setting_spray_names_another_area(title):
            return True
    claimed = _TYPE_FAMILIES.get(path)
    if not claimed:
        return False
    explicit = {family for family, pattern in _EXPLICIT_PRODUCT_FAMILIES.items()
                if pattern.search(str(title or ""))}
    return bool(explicit and claimed not in explicit)


def _pattern_matches(text: Optional[str]) -> int:
    """PR #2158 guard: first-match-wins is not evidence when multiple paths match."""
    return len(_shelf_paths(text))


def _shelf_paths(text: Optional[str]) -> frozenset:
    # Plural-aware for a shelf naming a setting spray ("Face Mists & Setting Sprays" is two products, as
    # its singular spelling is); the classifier's own reading, shared with resolve_path_from_row.
    from services.pdp_category_classifier import shelf_paths

    return shelf_paths(text)


# MEASURED (host, merchant product_type) -> leaf. Every CATEGORY_PATTERNS entry matches the
# SINGULAR noun, so a merchant filing products under "Cleansers" / "Sheet Masks" / "Serums"
# resolves to nothing -- and one unresolved row blocks its whole curated cohort. This is NOT a
# plural rule: a general singularising door was drafted and reviewed, and it gave confident
# wrong leaves on hosts nobody had read ("Pads" -> toner, "Lash Serums" -> skincare serum,
# "Pet Shampoos" -> shampoo, "Masks & Peels" -> exfoliant). A merchant's shelf name means what
# THAT merchant files under it; it is evidence only where somebody read the shelf.
#
# Each entry below was read product by product on 2026-09-18 over every product on that host
# (eyurs.com 434, ohlolly.com 510, sokoglam.com 567), and every product on the shelf belonged
# to the leaf -- or named a different leaf in its title, which `_resolve_category` checks per
# product before using the entry. Shelves that were NOT one class are deliberately absent:
# eyurs "Cotton Pads" (an accessory the toner pattern catches through "pad"), "Foot Masks"
# (body care), ohlolly "Exfoliator" (body scrubs + facial peel pads), sokoglam "chemical" (an
# azelaic treatment and a tea tree stick, not one class). Re-read 2026-09-22 and also left out:
# eyurs "Exfoliators" (a peel-off mask among the exfoliants) and "Wrinkle Patch" (an acne patch
# among the wrinkle patches). sokoglam "physical" moved IN on 2026-09-23 -- see its own note.
_MEASURED_HOST_PRODUCT_TYPES = {
    "eyurs.com": {
        "sheet masks": "beauty/skincare/treat/mask",
        "masks": "beauty/skincare/treat/mask",
        "eye masks": "beauty/skincare/treat/mask",
        "wash off mask": "beauty/skincare/treat/mask",
        "moisturizers": "beauty/skincare/moisturize/cream",
        "face moisturizers": "beauty/skincare/moisturize/cream",
        "serums": "beauty/skincare/treat/serum",
        "face serums": "beauty/skincare/treat/serum",
        "ampoules": "beauty/skincare/treat/serum",
        "essences": "beauty/skincare/treat/serum",
        "cleansers": "beauty/skincare/cleanse/cleanser",
        "oil cleansers": "beauty/skincare/cleanse/cleanser",
        "gel cleansers": "beauty/skincare/cleanse/cleanser",
        "toners": "beauty/skincare/tone/toner",
        "suncream": "beauty/skincare/sun/sunscreen",
        # Added 2026-09-22 from a re-read of every product on eyurs.com (433). Both types name TWO
        # pattern families, so the evidence policy reads them as ambiguous; every product on each
        # shelf was one class: four rinse-off / clay masks, and three acne patches. (The per-product title
        # check still sets the shelf aside for "Wash Off Pack": "wash" names the cleanser leaf.)
        "masks, exfoliators": "beauty/skincare/treat/mask",
        # treat/treatment, not treat/mask: Google 5976 and Shopify file patches under Acne
        # Treatments. CATEGORY_PATTERNS now agrees (#2250), so this type usually resolves on its own
        # evidence before the shelf is read; the entry stays as the measured record of the shelf.
        "acne pimple patch": "beauty/skincare/treat/treatment",
    },
    "ohlolly.com": {
        # "Wash off mask" is AMBIGUOUS to the regexes (cleanser "wash" + mask); every product
        # on the shelf was a rinse-off mask.
        "wash off mask": "beauty/skincare/treat/mask",
        # Overnight "sleeping packs" are leave-on masks.
        "sleeping pack": "beauty/skincare/treat/mask",
        "sun care": "beauty/skincare/sun/sunscreen",
    },
    "sokoglam.com": {
        "lip balms": "beauty/makeup/lip/balm",
        # 2026-09-22, every product on sokoglam.com (564): nine blemish patches and spot treatments.
        "spot": "beauty/skincare/treat/treatment",
        # Read again 2026-09-23, once an acid/peel pad stopped reading as a toner: all six products
        # on this shelf exfoliate (an AHA-BHA-PHA pad, a peel gel, three gauze peels) except
        # NEOGEN's soothing Real Cica Pad, whose title names the toner leaf and steps the shelf
        # aside. The shelf NAME is still no evidence on its own: the neighbouring "Chemical" shelf
        # holds an azelaic treatment and a tea tree stick, and stays out.
        # KNOWN FORWARD RISK: physical exfoliation is where TOOLS land, and the title check above is
        # a membership test, so a konjac pad or a silicone applicator filed here would be claimed as
        # a topical exfoliant. sokoglam files its tools under their own "Tools" shelf today (8
        # products). A general tools veto was drafted and REJECTED in review: `_title_paths` matches
        # a bare "brush", so it refused "Clay Mask with Applicator Brush" and "Lip Balm with Brush
        # Applicator" on the shelves above -- and an unresolved row stops the whole cohort at the
        # apply gate. Re-read this shelf before trusting it again.
        "physical": "beauty/skincare/treat/exfoliant",
    },
    # Read 2026-09-24, every product on the host (5,120; USD storefront; Japanese beauty). Its types are
    # breadcrumbs ("beauty & personal care / skincare / face masks"): each names two or more pattern
    # families, so the evidence policy reads every one as ambiguous and NO beauty row resolved. Two
    # shelves were one class: 97 masks and sheet packs, and 77 sunscreens. Their titles still step the
    # shelf aside eight times: 5 UV primers/CC/concealer and 2 UV makeup-base gels name another leaf,
    # and a cleansing mud paste names the cleanser. The set detector holds three filled mask rows until
    # someone accepts them: a mask-and-cream SET, a 6-sheet pack (a false positive: "6 Pieces"), and a
    # 20-piece dry compressed sheet you soak in your own lotion (arguably an accessory, not a mask).
    # The shelves read and LEFT OUT, each for rows no title check would catch:
    #   "skincare / cleansers" (203): blotting papers, pore strips, a clay face pack.
    #   "skincare / japanese lotions" (126): toners (化粧水) AND milky emulsions (乳液), two classes.
    #   "skincare / moisturizers" (121): a rice-bran beauty oil and an overnight sleeping pack.
    #   "skincare / facial treatments" (83): serums, beauty oils, emulsions, balms, a massage gel.
    #   "bath & body / body skincare" (189): liquid bandages, an antiseptic ointment, antiperspirants.
    "japanesetaste.com": {
        "beauty & personal care / skincare / face masks": "beauty/skincare/treat/mask",
        "beauty & personal care / skincare / sun care": "beauty/skincare/sun/sunscreen",
    },
    # Read 2026-09-26, every product on the host (181; MineTan's own US store, USD; it also sells the
    # sister brand b.tan). Its types are house shelf names ("Mine Foam") that name no pattern family, so
    # MineTan's self-tanners resolved nowhere and only its bundles and a brush went live. Two shelves
    # were one class: 12 self-tan foams/mousses and 4 self-tan gels ("Hydrating Tan Gelly"). This is
    # the first BODY shelf in the table; none of the 16 titles names another body area, so the
    # non-face veto below changes nothing here. KNOWN LIMIT: that veto is face-shaped, so a future
    # "Self Tan Body Mousse" on these shelves would be set aside (unresolved, never mislabelled); and a
    # title naming no leaf ("Glow Butter") would take the shelf's leaf. Re-read before trusting again.
    # The shelves read and LEFT OUT, each mixing classes:
    #   "Mine Body" (5): a blending brush, an applicator mitt, a tan-remover foam, a body butter and a
    #     gradual tan lotion. The mitt and the butter name no leaf, so the title check could not stop them.
    #   "Mine Face" (5): facial tan mists, bronzing drops, a bronzing serum and highlighter glow drops.
    #     Titles set 4 of 5 aside (toner/serum/highlighter); the shelf would add one row, so not worth it.
    #   "Mine Bronze Babe" (2): a home spray-tan machine kit (equipment; its title names gift-set).
    #   "Mine Pro Mist" (21): salon spray-tan solutions; already resolve (to toner, via "mist") and
    #   are excluded per cohort, so they are not a shelf question.
    # b.tan's own shelves ("b.tan Foam", ...) are a different brand's products; not read for it here.
    "us.shop.minetanbodyskin.com": {
        "mine foam": "beauty/body/tanning",
        "mine tan gellies": "beauty/body/tanning",
    },
    # Read 2026-09-27, every product on the host (435; tarte's own US store). Its brow, lip-plump and
    # sponge shelves name no pattern family ("Eyebrow" is not "brow pencil"), so 13 brow products, 5
    # plumping glosses and 5 sponges/puffs resolved nowhere and the brand's re-run kept none of them.
    #   "Eyebrow" (13): pencils, gels, pomade, mousse, a detailing pen, a brow lift, a pencil-and-gel duo.
    #   "Lip Plump" (6): five plumping glosses / gloss-balms (the copy says "gloss") and one "lip & cheek
    #     shift", a two-area tint that the lip-shelf area veto below sets aside.
    #   "Face Tool" (5): three blending sponges and two powder puffs.
    # The shelves read and LEFT OUT: "Skincare" (14: body butter, rollers, a mitt, self-tan, an eye
    # treatment, face oil), "Freckle" (4 freckle stamps/sticks -- no leaf -- and a pouch), "Setting
    # Spray" (no leaf: category_path_aliases), "Eyelash Curlers" (tools/* are brushes and sponges
    # only), "Blush, Bronzer" / "Blush, Highlighter" (multi-stick and palettes, two classes each).
    "tartecosmetics.com": {
        "eyebrow": "beauty/makeup/eye/brow",
        "lip plump": "beauty/makeup/lip/gloss",
        "face tool": "beauty/tools/sponge",
    },
    # Read 2026-09-27, every product on the host (131; Stila's own US store). Its types are sometimes
    # swapped ("Blush" on an eyeliner, "Eye Liner" on a cheek duo); those rows are REFUSED by
    # `_title_contradicts_product_type` and a shelf never fills a refusal.
    #   "Brows" (2): a brow pencil and a waterproof brow colour.
    #   "Lip Care" (2): a lip balm, and "Buff & Blur Lip Enzyme Exfoliator" -- which the lip shelves'
    #     multi-use rule sets aside for its "&" (the safe side; a lip scrub would otherwise be a balm).
    #     KNOWN FORWARD RISK: a gloss moved onto this shelf ("Plumping Lip Glaze" sits on "Lip Gloss/Oil"
    #     today) names no leaf and would become a balm. Re-read before trusting the shelf again.
    # LEFT OUT: "Complexion" (8: lip & cheek sticks, a sponge, a correcting palette, a primer, a
    # foundation-and-concealer), "Lip Products" (stains, a balm and a bundle).
    "stilacosmetics.com": {
        "brows": "beauty/makeup/eye/brow",
        "lip care": "beauty/makeup/lip/balm",
    },
}


_NON_FACE_TITLE = re.compile(r"\b(?:hair|scalp|body|foot|feet|hands?|nails?|lash(?:es)?|brows?|beard)\b", re.I)


# The non-face body-area rule lives with the taxonomy so every writer shares ONE function: this
# feed, the regex backfill, the seed mirror and merchant sync (services/pdp_category_classifier.py).
from services.pdp_category_classifier import FACE_SKINCARE_LEAF as _FACE_SKINCARE_LEAF  # noqa: E402
from services.pdp_category_classifier import non_face_leaf  # noqa: E402


def _non_face_leaf(path: str, *, title: Optional[str], product_type: Optional[str]) -> Optional[str]:
    return non_face_leaf(path, title, product_type)


def _title_paths(title: Optional[str]) -> set:
    from services.pdp_category_classifier import CATEGORY_PATTERNS
    return {path for _label, path, pattern in CATEGORY_PATTERNS if pattern.search(str(title or ""))}


def _measured_host_type_leaf(*, domain: Optional[str], product_type: Optional[str], title: Optional[str]) -> Optional[str]:
    """The measured leaf for this host's shelf, unless THIS product's title names another leaf."""
    host = _clean_domain(domain or "").lower()
    host = host[4:] if host.startswith("www.") else host
    ptype = " ".join(str(product_type or "").casefold().split())
    leaf = _MEASURED_HOST_PRODUCT_TYPES.get(host, {}).get(ptype)
    if not leaf:
        return None
    named = _title_paths(title)
    # eyurs files "Pyunkang Yul Essence Toner" under "Cleansers": a shelf is evidence about the
    # shelf, and the product's own title outranks it. A title naming no leaf does not.
    if named and leaf not in named:
        return None
    # Measured shelves are FACE (or lip) care, except MineTan's two tanning shelves (whose 16 titles
    # name no other body area, so this veto is inert there). A title naming another body area is a different
    # product the patterns cannot see: measured on eyurs' own "Moisturizers" shelf, a "Hand & Nail
    # Cream" and a "Body Lotion"; a future "Argan Oil Hair Mask" on "Masks" would otherwise become a
    # facial mask, and a "Lip & Body Balm" is not only a lip balm.
    # A brow shelf's own area word is not another area: "tinted brow gel" is what that shelf holds.
    own_area = _SHELF_OWN_AREA.get(leaf)
    text = own_area.sub(" ", str(title or "")) if own_area else str(title or "")
    if _NON_FACE_TITLE.search(text):
        return None
    # A lip shelf holds lip products; one that also names the cheek or eyes is two products in one
    # (tarte's "lip & cheek shift" on its "Lip Plump" shelf). The lip title door's own area rule.
    if leaf.startswith(_LIP_LEAF_PREFIX) and (_LIP_OTHER_AREA.search(str(title or ""))
                                            or _LIP_MULTI_USE.search(str(title or ""))):
        return None  # ...or two lip products ("lip plump & liner")
    # A makeup shelf's tool or accessory is not its formula ("spoolie", "brow shaping razors" on a brow
    # shelf), and a sponge shelf holds sponges and puffs, not every face tool (gua sha, rollers, curlers).
    if leaf.startswith("beauty/makeup/") and _SHELF_TOOL_WORDS.search(str(title or "")):
        return None
    required = _SHELF_REQUIRES.get(leaf)
    if required and not required.search(str(title or "")):
        return None
    return leaf


_SHELF_OWN_AREA = {"beauty/makeup/eye/brow": re.compile(r"\b(?:eye)?brows?\b", re.I)}
_SHELF_TOOL_WORDS = re.compile(r"\b(?:spoolies?|razors?|tweezers?|scissors|stencils?|sharpeners?|brush(?:es)?|"
                               r"curlers?|cases?)\b", re.I)
_SHELF_REQUIRES = {"beauty/tools/sponge": re.compile(r"\b(?:sponges?|puffs?|blenders?)\b", re.I)}


# A MEASURED host whose products carry no usable merchant type (blank, or a house shelf such as
# "Sale" / "new") -- the title's HEAD noun, the last leaf it names, is the product. "Swipe Serum
# Foundation" is a foundation, "BeachPlease Cream Blush" a blush, "SOS Recovery Cream" a cream.
# NOT a general title classifier: only these hosts, only these types, each read product by product;
# on any other host the head noun is no evidence (PR #2158: "Powder Kiss Lipstick", "Strobe Cream").
# Read on the product NAME only: a shade or variant suffix is cut first (" in Michelle (Warm Blush)",
# " - Last Chance Shades", ": Two Colors"), so a shade word is never the head. Refused, and left
# unresolved (review of #2403, each a real or plausible title on these hosts):
#   * no leaf; two leaves ending at the same place; a head outside makeup/skincare (merch: "Blush Tee");
#   * a LIP head: the lip title door owns lip rows, with its accessory / multi-use vetoes and its
#     per-row hold (placed_by_lip_title) -- this rule never places a lip row around that hold;
#   * two products in one name: any second leaf, unless it is a texture word right before the head
#     ("Serum Foundation", "Cream Blush", "Matte Powder Blush" -- not "Foundation & Concealer");
#   * an area the head's leaf does not belong to ("Lash Primer", "Brow Powder", "Body Highlighter",
#     "Lip & Cheek Cream");
#   * sets / bundles / duos / palettes, accessories (case, sharpener, remover, bag, clip), giveaways
#     ("Free Mini ..."), and anything the lip door calls not a product (samples, cards, socks).
_MEASURED_TITLE_HOST_TYPES = {
    # Read 2026-09-27, every product (64). 45 have a blank type: the SOS skincare line, MakeWaves
    # mascara, GoGo eyeshadow, GetSet powders and blush, cream blush / bronzer / contour, SuperDew
    # highlighter; samples, merch (a claw clip, socks, a crewneck, a keychain), a gift card and sets.
    # "Foundations & Concealers" (4) holds a foundation, a concealer, its sample and a tinted SPF --
    # four titles that each name their own leaf.
    # Its lip rows (ShineOn, LipSoftie, OneLiner) are NOT placed here: lip heads are the lip door's.
    "tower28beauty.com": frozenset({"", "foundations & concealers"}),
    # Read 2026-09-27, every product (131). The blank / "Sale" / "new" / "Eye Products" rows are
    # eyeliners, liquid eyeshadows, a primer, a mascara and bundles; the "Hidden" free minis are not
    # on the list.
    "stilacosmetics.com": frozenset({"", "sale", "new", "eye products"}),
}
# Distinct from every other writer's value (see CATEGORY_CONFIDENCE_LIP_TITLE).
CATEGORY_CONFIDENCE_MEASURED_HOST_TITLE = 0.81


# The product name ends where a shade / variant / edition suffix starts.
_TITLE_VARIANT_SUFFIX = re.compile(r"\s+(?:in|-|\u2013|\u2014)\s+|\s*[(:|]")
# Texture words that name a leaf of their own but only describe the head right after them.
_TEXTURE_LEAVES = frozenset({"beauty/skincare/moisturize/cream", "beauty/skincare/treat/serum",
                             "beauty/makeup/face/powder", "beauty/skincare/moisturize/oil"})
_TITLE_AREA_WORDS = {
    "lip": re.compile(r"\blips?\b", re.I),
    "cheek": re.compile(r"\bcheeks?\b", re.I),
    "eye": re.compile(r"\b(?:eyes?|eyelids?|lids?|under[\s-]?eyes?)\b", re.I),
    "lash": re.compile(r"\b(?:eye)?lash(?:es)?\b", re.I),
    "brow": re.compile(r"\b(?:eye)?brows?\b", re.I),
    "other": re.compile(r"\b(?:body|hair|scalp|nails?|cuticles?|hands?|feet|foot|neck|decollet[ae]ge|beard|legs?)\b", re.I),
}
# the areas a head leaf may name; anything else is another product (or two)
_HEAD_AREAS = (
    ("beauty/makeup/eye/mascara", {"lash", "eye"}),
    ("beauty/makeup/eye/brow", {"brow"}),
    ("beauty/makeup/eye/", {"eye"}),
    ("beauty/makeup/face/concealer", {"eye"}),
    ("beauty/makeup/face/", set()),
    ("beauty/skincare/", {"eye"}),
)
_TITLE_ACCESSORY_OR_GIVEAWAY = re.compile(
    r"\b(?:free|gwp|brush(?:es)?|sharpeners?|applicators?|cases?|holders?|pouch(?:es)?|mirrors?|removers?|"
    r"wipes?|organi[sz]ers?|bags?|totes?|clips?|tees?|t-?shirts?|shirts?|hoodies?|crewnecks?|sweatshirts?|"
    r"hats?|caps?|refills?|wands?)\b", re.I)
# A measured host's own head noun that no CATEGORY_PATTERNS entry reads. Read 2026-10-10: tower28's
# only "spray" is the SOS Daily Rescue Facial Spray (singles, a travel size, a refill and a duo, all
# with a blank type). A facial mist is a toner here: the Toner pattern already claims "mist", and the
# gateway's canonical leaf is tone/toner. Consulted ONLY where no pattern names anything, only on a
# blank type, and never past the veto below -- a setting, hair, body, sun or scented spray is another
# product, never this host's facial spray (a setting spray is the Setting Spray pattern's, where one exists).
_MEASURED_TITLE_HOST_HEADS = {
    "tower28beauty.com": (("beauty/skincare/tone/toner", re.compile(r"\bspray\b", re.I)),),
}
_HOST_HEAD_VETO = re.compile(r"\b(?:setting|set|fix(?:er|ing)?|make\s*-?up|finishing|hair|scalp|body|hands?|"
                             r"sun|spf|uv|tan(?:ning)?|perfume|parfum|fragrance|scent(?:ed)?|room|linen)\b", re.I)


def _measured_host_title_leaf(*, domain: Optional[str], product_type: Optional[str],
                              title: Optional[str]) -> Optional[str]:
    """The head-noun leaf of this product's name, on a measured title host only; else None."""
    host = _clean_domain(domain or "").lower()
    host = host[4:] if host.startswith("www.") else host
    ptype = " ".join(str(product_type or "").casefold().split())
    if ptype not in _MEASURED_TITLE_HOST_TYPES.get(host, frozenset()):
        return None
    text = " ".join(str(title or "").split())  # see _is_makeup_brush_title: no quadratic split
    if _LIP_SET.search(text) or _LIP_NOT_A_PRODUCT.search(text) or _TITLE_ACCESSORY_OR_GIVEAWAY.search(text):
        return None
    name = _TITLE_VARIANT_SUFFIX.split(text, maxsplit=1)[0]
    from services.pdp_category_classifier import CATEGORY_PATTERNS
    spans: Dict[str, Tuple[int, int]] = {}
    for _label, path, pattern in CATEGORY_PATTERNS:
        for m in pattern.finditer(name):
            if path not in spans or m.end() > spans[path][1]:
                start = m.start(1) if m.lastindex and m.start(1) >= 0 else m.start()
                spans[path] = (start, m.end())
    if not spans and not ptype and not _HOST_HEAD_VETO.search(text):
        for path, pattern in _MEASURED_TITLE_HOST_HEADS.get(host, ()):
            m = pattern.search(name)
            if m:
                spans[path] = (m.start(), m.end())
                break
    if not spans:
        return None
    # the leaf whose match ends last; a tie, like a named gift set, is a second leaf and refused below
    head = max(spans, key=lambda path: spans[path][1])
    if not head.startswith(("beauty/makeup/", "beauty/skincare/")) or head.startswith(_LIP_LEAF_PREFIX):
        return None
    head_start = spans[head][0]
    for path, (_start, end) in spans.items():
        if path != head and not (path in _TEXTURE_LEAVES and not name[end:head_start].strip()):
            return None
    allowed = next((areas for prefix, areas in _HEAD_AREAS if head.startswith(prefix)), None)
    if allowed is None:
        return None  # a leaf with no area row (nails, lashes...) is not this rule's to place
    # over the WHOLE title: a suffix can name another area ("Highlighter (Face & Body)")
    if any(pattern.search(text) for area, pattern in _TITLE_AREA_WORDS.items() if area not in allowed):
        # A FACE skincare head on a product naming another body area is that area's product: the
        # body-area guard's own answer, as for every other writer ("SOS Rescue + Relief Body Wash
        # Treatment" -> body/care). Where the guard has no honest leaf, or the face is named too
        # ("Face & Body Wash"), or the head is makeup ("Body Highlighter"), it stays refused.
        return non_face_leaf(head, text) or None
    return head


# A lip product's own title, where the merchant type says nothing. Measured 2026-09-23 on the
# UCP-ready Meitu retailers: k-touch.us, belleandblush.com and openthebeauty.com file lipsticks
# under a blank type, "Cosmetics", or a comma tag list ("HERA,Face,Makeup,..."), so every
# "Soft Matte Lipstick" / "Rouge Opulent Lipstick" / "Lip Liner" landed unresolved and blocked
# its whole cohort, while the same stores' mascaras resolved from a clean type.
#
# Deliberately NOT a general title classifier: only a lip leaf, only when the title names that
# ONE leaf and nothing else, and only where the type and the measured shelf left the row
# unresolved. What it refuses stays unresolved -- never a guessed leaf:
#
# OFF unless an operator asks for it on a manual run (`onboard_curated_brands.py
# --lip-title-evidence`). The queue worker, the brand-official lane, the key-retirement script and
# the repair planner all share `_resolve_category`, and none of them sets this: their output is
# byte-identical to before the door existed.
_LIP_TITLE_EVIDENCE: contextvars.ContextVar[bool] = contextvars.ContextVar("lip_title_evidence", default=False)


@contextlib.contextmanager
def lip_title_evidence():
    """Enable `_explicit_lip_title_leaf` for the calls inside this block (and tasks they start)."""
    token = _LIP_TITLE_EVIDENCE.set(True)
    try:
        yield
    finally:
        _LIP_TITLE_EVIDENCE.reset(token)


_LIP_LEAF_PREFIX = "beauty/makeup/lip/"
# Distinct from every other writer's value (0.3/0.7/0.8/0.82/0.85/0.9/0.95), so a stored row this
# door placed can be found by (category_label_source, category_confidence) without re-running it.
# Nothing thresholds on category_confidence.
CATEGORY_CONFIDENCE_LIP_TITLE = 0.78
# two things joined: "Lip & Cheek", "Lipstick & Liner", "Eye and Lip Remover", "Lip/Cheek Tint". Any
# "&", "+" or "and" refuses (a shade like "Rose & Honey" stays unresolved -- the safe side), except a
# "+" after a digit ("SPF 30+"); "/" only beside "lip", because sizes read "3.5g/0.12oz".
_LIP_MULTI_USE = re.compile(r"&|(?<!\d)\+|\band\b|\blips?\s*/|/\s*lips?\b", re.I)
# ...and any other area named ANYWHERE: "Lip Tint & Cheek", "Lip Stain for Cheeks Too", "Lip Liner Eye Pencil"
_LIP_OTHER_AREA = re.compile(r"\b(?:cheeks?|eyes?|eyelids?|face|facial|brows?|cuticles?|nails?|body|hands?|"
                             r"hair|feet|foot)\b", re.I)
# a set, kit or bundle: its own shelf (beauty/sets), whatever lip product is inside it
_LIP_SET = re.compile(r"\b(?:sets?|kits?|bundles?|packs?|combos?|duos?|trios?|quads?|twins?|palettes?|wardrobes?|"
                      r"collections?|gift|sampler|discovery|advent|vault|(?:[2-9]|\d{2,})\s*-?\s*(?:pcs?|pieces?|ea)|"
                      r"\d+\s*x|x\s*\d+)\b", re.I)
# a tool or accessory FOR a lip product, not a lip product
_LIP_ACCESSORY = re.compile(r"\b(?:brush(?:es)?|sharpeners?|applicators?|cases?|holders?|pouch(?:es)?|"
                            r"mirrors?|removers?|wipes?|cleansers?|organi[sz]ers?)\b", re.I)
# not a lip product at all: merch, toys, craft supplies, packaging, displays, samples, other
# audiences, ingestibles, fragrance. Measured by adversarial review of PR #2257.
_LIP_NOT_A_PRODUCT = re.compile(
    r"\b(?:dogs?|cats?|pets?|kids?|girls?|boys?|bab(?:y|ies)|child(?:ren)?|toys?|plush|squishy|pretend|"
    r"charms?|earrings?|jewel(?:ry|lery)|necklaces?|pendants?|candles?|lighters?|socks?|usb|power\s*banks?|"
    r"key\s*rings?|keyrings?|key\s*chains?|keychains?|lanyards?|stickers?|decals?|magnets?|ornaments?|"
    r"empty|base|beeswax|molds?|moulds?|stencils?|dispensers?|displays?|stands?|tubes?|tins?|containers?|"
    r"bottles?|cards?|testers?|samples?|swatch(?:es)?|gumm(?:y|ies)|supplements?|vitamins?|capsules?|"
    r"candy|chocolate|perfume|parfum|eau|cologne|fragrance|"
    # review of #2263 (catch-all types reach the door): merch, media, toys, trade stock
    r"posters?|enamel\s+pins?|tumblers?|mugs?|diffusers?|dolls?|barbie|squish(?:y|ies)|wall\s+art|dvd|books?|"
    r"e-?books?|labels?|mica|glitter\s+pigment|wholesale|bulk|gwp|promo(?:tional)?|starter)\b", re.I)
# "Lip Color" is a family word, not a form: "Glossy Lip Color" is a gloss, "Lip Color Balm" a balm.
_LIP_FAMILY_WORD = re.compile(r"\blip\s+colou?r\b", re.I)
_LIP_FORM_WORD = re.compile(r"\b(?:gloss|glossy|tint|stain|balm|oil|liner|pencil|crayon|butter|serum)\b", re.I)
# Merchant types that say "lip" and nothing more specific. With _GENERIC_PRODUCT_TYPES and a blank
# type, the ONLY types the door accepts: an allowlist, because a type no pattern reads ("Toys",
# "Supplements", "Gift Sets", "Eyes", "Packaging") is still the merchant saying what it is.
_LIP_AREA_TYPES = frozenset({"lip", "lips", "lip makeup", "lip make up", "lip products", "lip product"})
# The neutral types the door accepts. NOT _GENERIC_PRODUCT_TYPES: that set also holds "hair care",
# "face care" and "skin care", which say the product is something other than a lip product.
# thisisbeauty.us (2026-09-23) types all 8,367 products "Misc": a catch-all says as little as a blank.
_LIP_NEUTRAL_TYPES = frozenset({"beauty", "cosmetics", "makeup", "make up", "lip care", "lip treatment",
                                "lip treatments", "lip color", "lip colour",
                                "misc", "miscellaneous", "other", "others", "general", "default"})


def _lip_word_tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def _explicit_lip_title_leaf(*, product_type: Optional[str], title: Optional[str]) -> Optional[str]:
    """The lip leaf a title names outright, or None. The caller asks only for UNRESOLVED rows."""
    if not _LIP_TITLE_EVIDENCE.get():
        return None
    text = str(title or "")
    named = _title_paths(text)
    if len(named) != 1:
        # Zero is no evidence; two is PR #2158's ambiguity ("Powder Kiss Lipstick" names a powder).
        return None
    leaf = next(iter(named))
    if not leaf.startswith(_LIP_LEAF_PREFIX):
        return None
    if any(p.search(text) for p in (_LIP_MULTI_USE, _LIP_OTHER_AREA, _LIP_SET, _LIP_ACCESSORY,
                                     _LIP_NOT_A_PRODUCT, _NON_FACE_TITLE)):
        return None
    if leaf == "beauty/makeup/lip/lipstick" and _LIP_FAMILY_WORD.search(text) and _LIP_FORM_WORD.search(text):
        return None
    ptype = " ".join(str(product_type or "").casefold().split())
    if not ptype or ptype in _LIP_NEUTRAL_TYPES or ptype in _LIP_AREA_TYPES:
        return leaf
    if "," not in ptype:
        return None
    # A comma tag list IS the merchant's own classification. Every tag must be generic, the lip
    # area, or a word the title already says (the brand, a finish) -- so "Lip,Cheek",
    # "Makeup,Lips,Sets" and "HERA,Face,Makeup,Cushion" say something the title does not -- and at
    # least one tag must name the lip area.
    title_words = _lip_word_tokens(text)
    tags = [t.strip() for t in ptype.split(",") if t.strip()]
    lip_tag = False
    for tag in tags:
        if tag in _LIP_AREA_TYPES or _title_paths(tag) == {leaf}:
            lip_tag = True
        elif tag in _LIP_NEUTRAL_TYPES:
            continue
        elif not _lip_word_tokens(tag) or not _lip_word_tokens(tag) <= title_words:
            return None
    return leaf if lip_tag else None


# A false-lash or nail product's own title, where the merchant type says nothing. Measured 2026-09-26:
# kissusa.com types 1,395 of 1,464 products "Physical Products" (39 more blank), so its crawl kept 3
# rows although 505 titles name exactly one lash or nail leaf; its stockists shelve the same products
# as "EYELASHES", "Eye lashes", "COSMETICS - EYELASH - CLUSTERS", "NAILS", "Nail Styling",
# "COSMETICS - NAILS - PRESS ONS" -- a lash/nail shelf no pattern reads as one leaf.
#
# The lip door's shape, for the lash and nail leaves only (the lip door stays lip-only): OFF unless a
# job asks (options.lash_nail_title_evidence), fills ONLY a row everything else left unresolved, and
# only when the title names ONE leaf and that leaf is a lash or nail leaf. Every row it places is held
# per row by the detectors (placed_by_lash_nail_title) until someone accepts that row.
_LASH_NAIL_TITLE_EVIDENCE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "lash_nail_title_evidence", default=False)


@contextlib.contextmanager
def lash_nail_title_evidence():
    """Enable `_explicit_lash_nail_title_leaf` for the calls inside this block (and tasks they start)."""
    token = _LASH_NAIL_TITLE_EVIDENCE.set(True)
    try:
        yield
    finally:
        _LASH_NAIL_TITLE_EVIDENCE.reset(token)


# Distinct from every other writer's value (see CATEGORY_CONFIDENCE_LIP_TITLE), so a stored row this door
# placed can be found without re-running it.
CATEGORY_CONFIDENCE_LASH_NAIL_TITLE = 0.76
_LASH_TITLE_LEAVES = frozenset({"beauty/makeup/eye/false-lashes"})
_NAIL_TITLE_LEAVES = frozenset({"beauty/makeup/nails/nail-polish", "beauty/makeup/nails/nail-polish-remover",
                                "beauty/makeup/nails/cuticle-oil", "beauty/makeup/nails/press-on-nails"})
# Types that say nothing about which product this is. NOT the lip door's "lip care"/"lip color" types.
# "accessories": shopstarbeauty.com shelves every KISS lash and nail there; the title must still name
# one lash/nail leaf, which no hair clip or bag does.
_LASH_NAIL_NEUTRAL_TAGS = frozenset({"beauty", "cosmetics", "cosmetic", "makeup", "make up", "misc",
                                     "miscellaneous", "other", "others", "general", "default",
                                     "physical products", "physical product", "accessories"})
# Shelf words that name the lash (or nail) area and nothing more specific than the title does.
_LASH_AREA_TAGS = frozenset({"lash", "lashes", "eyelash", "eyelashes", "eye lash", "eye lashes",
                             "false lashes", "false eyelashes", "strip lashes", "strips", "lash clusters",
                             "clusters"})
_NAIL_AREA_TAGS = frozenset({"nail", "nails", "nail care", "nail styling", "fake nails", "false nails",
                             "press ons", "press on nails", "press-on nails", "artificial nails", "manicure",
                             "pedicure", "nail polish", "nail color", "nail colour", "nail lacquer"})
# One shelf, written as a path or a list: "COSMETICS - EYELASH - CLUSTERS", "Makeup > Nails", "Nails, KISS".
_LASH_NAIL_TAG_SPLIT = re.compile(r"\s+-\s+|\s*[>,|/]\s*")
# Not a lash or nail product at all: merch, cards, stickers, toys, displays, samples, other audiences
# (the lip door's list, less "base" -- a base coat IS nail polish -- and less the lip-only words) --
# and a TOOL or ACCESSORY for one, which the lip door refuses through _LIP_ACCESSORY: "Individual Lash
# Tweezers", "Press On Nails Cuticle Pusher", "Nail Polish Rack Holder" (review of #2377). It costs
# KISS lashes that list an applicator in the box ("... | 12 Lash Clusters, With Mini Applicator"):
# they stay unresolved, the safe side.
_LASH_NAIL_NOT_A_PRODUCT = re.compile(
    r"\b(?:gift\s*cards?|e-?gift|cards?|posters?|stickers?|decals?|charms?|earrings?|jewel(?:ry|lery)|"
    r"necklaces?|pendants?|key\s*rings?|keyrings?|key\s*chains?|keychains?|lanyards?|magnets?|ornaments?|"
    r"toys?|plush|dolls?|squish(?:y|ies)|pretend|puzzles?|games?|costumes?|socks?|wall\s+art|art\s+prints?|"
    r"dogs?|cats?|pets?|candles?|tumblers?|mugs?|books?|e-?books?|dvd|"
    r"displays?|stands?|empty|testers?|samples?|swatch(?:es)?|wholesale|bulk|gwp|promo(?:tional)?|"
    r"bags?|pouch(?:es)?|cases?|organi[sz]ers?|storage|box(?:es)?|trays?|racks?|holders?|openers?|"
    r"tweezers?|applicators?|pushers?|files?|buffers?|clippers?|cutters?|brush(?:es)?|prep|"
    r"lamps?|dryers?|drills?)\b", re.I)


def _explicit_lash_nail_title_leaf(*, product_type: Optional[str], title: Optional[str]) -> Optional[str]:
    """The lash or nail leaf a title names outright, or None. The caller asks only for UNRESOLVED rows."""
    if not _LASH_NAIL_TITLE_EVIDENCE.get():
        return None
    text = str(title or "")
    named = _title_paths(text)
    if len(named) != 1:
        # Zero is no evidence; two is ambiguity ("Magnetic Eyeliner & False Eyelashes", "... Collection").
        return None
    leaf = next(iter(named))
    if leaf in _LASH_TITLE_LEAVES:
        area = _LASH_AREA_TAGS
    elif leaf in _NAIL_TITLE_LEAVES:
        area = _NAIL_AREA_TAGS
    else:
        return None
    if _LASH_NAIL_NOT_A_PRODUCT.search(text):
        return None
    ptype = " ".join(str(product_type or "").casefold().split())
    if not ptype:
        return leaf
    # Every tag of the merchant's shelf must be neutral or name THIS leaf's area: "Eye lashes" vouches for
    # a lash, never for a nail; "BROW & LASH TOOLS", "NAIL TOOLS", "Hair Care" say something else.
    tags = [t for t in _LASH_NAIL_TAG_SPLIT.split(ptype) if t]
    if tags and all(t in _LASH_NAIL_NEUTRAL_TAGS or t in area for t in tags):
        return leaf
    return None


def _resolve_category(*, product_type: Optional[str], title: Optional[str], flag_path: str,
                      domain: Optional[str] = None) -> Tuple[str, float]:
    """Resolve, then refuse a FACE skincare leaf for a product that names another body area.

    The body-area rule changes an answer ONLY where the answer is a face skincare leaf AND the
    title or merchant type names a non-face area (and not the face as well). Every other row --
    every face-titled row -- is returned exactly as before.

    A caller's own taxonomy LEAF is not second-guessed here: the repair planner passes a stored
    leaf as the flag, and a stored leaf may be challenged only with fresh merchant evidence
    (--review-existing-leaves, which passes a coarse flag instead). Curated feeds pass a coarse
    flag ("beauty"), so every face leaf the resolver derives itself is checked.
    """
    path, confidence = _resolve_category_unguarded(
        product_type=product_type, title=title, flag_path=flag_path, domain=domain)
    from services.pdp_category_classifier import CATEGORY_PATTERNS
    flag = str(flag_path or "").strip().strip("/").lower()
    if path == flag and flag in {leaf for _label, leaf, _pattern in CATEGORY_PATTERNS}:
        return path, confidence
    from services.category_path_aliases import resolve
    leaf = _non_face_leaf(resolve(path) or "", title=title, product_type=product_type)
    if leaf is None:
        return path, confidence
    if not leaf:
        return "", CATEGORY_CONFIDENCE_FEED_DEFAULT
    # The leaf now rests on the product's own area word, not on the merchant's type alone.
    return leaf, min(confidence, CATEGORY_CONFIDENCE_EXPLICIT_TITLE)


def _resolve_category_unguarded(*, product_type: Optional[str], title: Optional[str], flag_path: str,
                                domain: Optional[str] = None) -> Tuple[str, float]:
    """Evidence policy, then -- ONLY where it left the product unresolved -- a measured host shelf,
    then an explicit lip title (`_explicit_lip_title_leaf`), then an explicit lash/nail title
    (`_explicit_lash_nail_title_leaf`).

    Structural no-regression: anything the evidence policy resolves to a leaf, and every
    deliberate refusal (""), is returned untouched. The measured shelf fills only a coarse
    FALLBACK -- which includes the evidence policy's multi-pattern AMBIGUITY return: ohlolly's
    "Wash Off Mask" matches both the cleanser ("wash") and mask patterns, and the table names
    it because every product on that shelf was read. It never fills a refusal or a caller's leaf.
    """
    path, confidence = _resolve_category_by_evidence(product_type=product_type, title=title, flag_path=flag_path)
    from services.category_path_aliases import resolve
    if path == "" or resolve(path):
        return path, confidence
    fallback = str(flag_path or "").strip().strip("/").lower()
    if fallback and fallback.split("/", 1)[0] != "beauty":
        return path, confidence
    leaf = _measured_host_type_leaf(domain=domain, product_type=product_type, title=title)
    if leaf:
        return leaf, CATEGORY_CONFIDENCE_MEASURED_HOST_TYPE
    leaf = _measured_host_title_leaf(domain=domain, product_type=product_type, title=title)
    if leaf:
        return leaf, CATEGORY_CONFIDENCE_MEASURED_HOST_TITLE
    # Last, and on the same terms as the shelf: fills only what everything above left unresolved.
    leaf = _explicit_lip_title_leaf(product_type=product_type, title=title)
    if leaf:
        return leaf, CATEGORY_CONFIDENCE_LIP_TITLE
    leaf = _explicit_lash_nail_title_leaf(product_type=product_type, title=title)
    if leaf:
        return leaf, CATEGORY_CONFIDENCE_LASH_NAIL_TITLE
    return path, confidence


def _resolve_category_by_evidence(*, product_type: Optional[str], title: Optional[str], flag_path: str) -> Tuple[str, float]:
    """One category evidence policy shared by feed mapping and repair planning.

    Retain #2158's ambiguity guard, conservative marketing-title behavior and
    confidence semantics. Deliberately replace its storefront-area veto: strong
    per-product evidence can disagree with a COARSE storefront shelf (MISSHA tools
    were all labelled skincare). An explicit taxonomy leaf remains protected.
    Title evidence has only two narrow doors here: a tool noun suffix without formula
    context, and an explicit lip-oil title refining the measured generic lip shelves.
    (A third, `_explicit_lip_title_leaf`, runs after the measured shelf, on unresolved rows only.)
    """
    from services.pdp_category_classifier import CATEGORY_PATTERNS, classify
    fallback = str(flag_path or "").strip().strip("/").lower()
    if fallback and fallback.split("/", 1)[0] != "beauty":
        return flag_path, CATEGORY_CONFIDENCE_FEED_DEFAULT
    leaves = {path for _label, path, _pattern in CATEGORY_PATTERNS}

    def accept(path: str, confidence: float) -> Tuple[str, float]:
        if _title_contradicts_product_type(title, path):
            # Refuse even when the caller supplied that same leaf: conflicting
            # product evidence cannot become a resolved category by repetition.
            return "", CATEGORY_CONFIDENCE_FEED_DEFAULT
        if not path.startswith("beauty/") or (fallback in leaves and path != fallback):
            return fallback, CATEGORY_CONFIDENCE_FEED_DEFAULT
        return path, confidence

    ptype = " ".join(str(product_type or "").casefold().split())
    if re.sub(r"[^a-z0-9]", "", ptype) in _AMBIGUOUS_PRODUCT_TYPE_KEYS:
        # Shared regexes currently recognize only one half of these merchant
        # alternatives. One regex hit is not a unique class assertion.
        return "", CATEGORY_CONFIDENCE_FEED_DEFAULT
    # Exact merchant product types observed on the primary retailer feed. Lip
    # Scrub belongs to the existing lip-care leaf despite also matching the
    # generic exfoliator regex; Sun Protection names sunscreen, not an SPF claim
    # on an unrelated formula. No marketing-title or storefront substitution.
    explicit_types = {
        "lip scrub": "beauty/makeup/lip/balm",
        "sun protection": "beauty/skincare/sun/sunscreen",
        # Foot Care is a body-care treatment, not footwear or a facial peel.
        "foot care": "beauty/body/care",
    }
    if ptype in explicit_types:
        return accept(explicit_types[ptype], CATEGORY_CONFIDENCE_MERCHANT_TYPE)
    matches = _pattern_matches(product_type)
    if matches > 1:
        # "Concealer Brush" matches the concealer AND brush patterns, so the type alone is not one
        # class -- but its head noun is the tool, and a title that also ENDS in brush agrees.
        # Measured 2026-09-27 on tartecosmetics.com: 33 brushes on "Concealer Brush", "Eyeshadow
        # Brush" and "Eyeliner Brush" shelves resolved nowhere. Its "Foundation Brush" shelf also
        # holds sponges and a twist tool; their titles do not end in brush, so they stay unresolved.
        # Only a brush FOR makeup: every other family the type names must be a makeup leaf. sokoglam's
        # "Hair Brush" names haircare -- a hairbrush is not a beauty/tools/brush (makeup applicators).
        # Nail types sit under beauty/makeup/nails but a "Gel Polish Brush" is not a makeup tool.
        from services.pdp_category_classifier import CATEGORY_PATTERNS
        type_uses = {path for _label, path, pattern in CATEGORY_PATTERNS
                     if pattern.search(str(product_type or "")) and path != "beauty/tools/brush"}
        if (_TOOL_NOUN_SUFFIX.search(ptype) and _is_makeup_brush_title(str(title or ""))
                and all(path.startswith("beauty/makeup/") and not path.startswith("beauty/makeup/nails/")
                        for path in type_uses)):
            return accept("beauty/tools/brush", CATEGORY_CONFIDENCE_EXPLICIT_TITLE)
        shelf = _shelf_paths(product_type)
        if shelf in _COMBINED_SHELVES:
            leaf, other = _COMBINED_SHELVES[shelf]
            named = _title_paths(title)
            # The title must name the spray and nothing else: a "Fragrance Setting Mist" or a "Cleanser &
            # Setting Spray" on that shelf is not plainly a setting spray, so it keeps the old answer.
            if named == {leaf}:
                return accept(leaf, CATEGORY_CONFIDENCE_EXPLICIT_TITLE)
            # The old answer is what the shelf read as before the leaf: the singular spelling read as the
            # powder alone; the plural ("Setting Sprays & Powders") as nothing, the #2158 ambiguity --
            # where only a title naming the powder, and nothing else, is filed on it.
            from services.pdp_category_classifier import PATTERNS_BEFORE_SETTING_SPRAY
            before = {path for _label, path, pattern in PATTERNS_BEFORE_SETTING_SPRAY
                      if pattern.search(str(product_type or ""))}
            if before == {other}:
                return accept(other, CATEGORY_CONFIDENCE_MERCHANT_TYPE)
            if named == {other}:
                return accept(other, CATEGORY_CONFIDENCE_EXPLICIT_TITLE)
            return fallback, CATEGORY_CONFIDENCE_FEED_DEFAULT
        return fallback, CATEGORY_CONFIDENCE_FEED_DEFAULT
    if ptype in _GENERIC_LIP_TYPES:
        title_hit = classify(title)
        if title_hit and title_hit[1] == "beauty/makeup/lip/oil" and _pattern_matches(title) == 1:
            return accept(title_hit[1], CATEGORY_CONFIDENCE_EXPLICIT_TITLE)
    if ptype not in _GENERIC_PRODUCT_TYPES and matches == 1:
        hit = classify(product_type)
        if (hit and hit[1] == _SETTING_SPRAY_LEAF and _TOOL_NOUN_SUFFIX.search(str(title or ""))
                and not _TOOL_FORMULA_CONTEXT.search(str(title or ""))):
            # A brush on a "Setting Spray" shelf: before the leaf that type read as nothing and the tool
            # rescue below filed the brush; the shelf's new leaf must not take it.
            return accept("beauty/tools/brush", CATEGORY_CONFIDENCE_EXPLICIT_TITLE)
        if hit:
            return accept(hit[1], CATEGORY_CONFIDENCE_MERCHANT_TYPE)
    # An unclassifiable multi-use label (e.g. Lip & Cheek) is not permission to
    # choose a competing category from its title.
    if matches == 0 and re.search(r"[&/]|\band\b", ptype):
        return fallback, CATEGORY_CONFIDENCE_FEED_DEFAULT
    if _TOOL_NOUN_SUFFIX.search(str(title or "")) and not _TOOL_FORMULA_CONTEXT.search(str(title or "")):
        return accept("beauty/tools/brush", CATEGORY_CONFIDENCE_EXPLICIT_TITLE)
    # Powder Kiss Liquid Lipcolour / Slim Stick and Strobe Cream are explicitly
    # not classified from their marketing titles; shallow backfill owns the residue.
    return fallback, CATEGORY_CONFIDENCE_FEED_DEFAULT


def product_category_path(*, title: Optional[str], product_type: Optional[str], fallback: str,
                          domain: Optional[str] = None) -> str:
    """Path-only wrapper for the review-only repair planner; no second classifier."""
    path = _resolve_category(product_type=product_type, title=title, flag_path=fallback, domain=domain)[0]
    # The repair planner expresses abstention as no change, never a blank-path
    # update. Fresh mapping consumes the unresolved result directly above.
    return path or fallback


def shopify_product_to_record(
    product: Dict[str, Any],
    *,
    domain: str,
    category_path: str,
    brand_override: Optional[str] = None,
    emit_variants: bool = False,
    # Separate switch for NATIVE (un-folded) multi-variant rows, so the fold lane's
    # `emit_variants=True` cannot silently start emitting variants for the rows it
    # did not fold — `--base-listings-only` runs (MAC) stay byte-identical.
    emit_native_variants: bool = False,
    currency: Optional[str] = None,
    source_role: str = "brand_official",
    retailer_name: Optional[str] = None,
    # The product's `body_html_sentences`, when the caller already computed them (records_for_brand
    # does, for its batch pass); derived here otherwise. Same description either way.
    body_sentences: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Map one Shopify `/products.json` product → a Path-C validated record
    (`{pdp, offers}`). Returns None if it lacks a title/handle (not actionable).
    The brand storefront is the authoritative PDP, so the offer is brand-direct
    and carries the variant barcode (GTIN) when present."""
    if not isinstance(product, dict):
        return None
    if source_role not in {"brand_official", "retailer"}:
        raise ValueError("source_role must be brand_official or retailer")
    host = _clean_domain(domain)
    title = str(product.get("title") or "").strip()
    handle = str(product.get("handle") or "").strip()
    if not title or not handle:
        return None
    # NOT `brand_override or vendor`: that renames across a brand boundary. See
    # `resolve_record_brand` — the override normalises spelling, it does not relabel
    # a sibling brand the storefront names itself.
    if source_role == "retailer":
        vendor = str(product.get("vendor") or "").strip()
        vendor_key = "".join(c for c in vendor.casefold() if c.isalnum())
        # The STORE's label, regional prefix skipped: the first label of us.mcobeauty.com is "us",
        # under the floor, so `MCoBeauty US` passed as maker evidence on the brand's own store --
        # 495 offers (and 323 on us.inikaorganic.com) written as retailer stock, measured 2026-09-26,
        # while dhccare.com's `DHC Care` was refused. A brand's own store runs as brand_official.
        host_label = _store_host_label(host)
        if (not _looks_like_a_brand_name(vendor) or not vendor_key
                or (len(host_label) >= 3 and host_label in vendor_key)):
            raise ValueError(f"{host}: retailer_maker_unproven: vendor {vendor!r} is not maker evidence")
        # Only exact normalized maker equivalence permits a spelling override --
        # or a MEASURED rename in RETAILER_BRAND_SPELLINGS. Brand-direct store/supplier-code
        # heuristics cannot label retailer stock.
        override_key = "".join(c for c in str(brand_override or "").casefold() if c.isalnum())
        # The VENDOR's own listed family decides its spelling -- a normalisation of the maker the
        # retailer named, never a relabel into whatever family --brand belongs to. It does not
        # depend on --brand at all, so a run without one converges too.
        vendor_family = _retailer_brand_family(vendor_key)
        if vendor_family is not None:
            brand = RETAILER_BRAND_CANONICAL[vendor_family]
        else:
            brand = brand_override if override_key == vendor_key else vendor
    else:
        brand, _brand_reason = resolve_record_brand(product.get("vendor"), brand_override, host)
        # The same one-spelling-per-family rule for brand-official stores: a manyo.us re-run with
        # --brand "Manyo" would otherwise write "Manyo" and split from its own 78 "Ma:nyo" rows.
        official_family = _retailer_brand_family("".join(c for c in str(brand or "").casefold() if c.isalnum()))
        if official_family is not None:
            brand = RETAILER_BRAND_CANONICAL[official_family]
    brand = str(brand or "").strip()
    if not brand:
        return None
    variants = product.get("variants")
    variants = variants if isinstance(variants, list) else []
    # Pick the first sellable variant — priced at or above MIN_SELLABLE_PRICE.
    # Gift-with-purchase and other $0/unpriced items have no purchasable offer —
    # drop the product entirely so it never enters the commerce index (these were
    # landing as junk PDPs/seeds, the offers_skipped noise seen onboarding kosas).
    # The floor exists because "positive" was not enough: stilacosmetics.com lists a
    # "Free Travel … (TikTok Shop)" promo at $0.01, which cleared `p > 0`, ingested
    # as a canonical anchor and served (measured 2026-09-05). A token price is a
    # promo mechanic, not an offer.
    variant = None
    price = None
    for v in variants:
        p = _to_float((v or {}).get("price"))
        if p is not None and p >= MIN_SELLABLE_PRICE:
            variant, price = v, p
            break
    if variant is None:
        return None
    image = _first(product.get("images")) or {}
    if not str(image.get("src") or "").strip():
        # No product-level image: a variant's own swatch is a real image of this
        # product and is better than publishing a row the scorer counts as
        # imageless. Only a fallback — a product image always wins.
        for _v in variants:
            _fi = _v.get("featured_image") if isinstance(_v, dict) else None
            _src = (
                str((_fi or {}).get("src") or "").strip() if isinstance(_fi, dict)
                else str((_v or {}).get("image_src") or "").strip()
            )
            if _src:
                image = {"src": _src}
                break
    # The product's OWN structured gallery: its `images[].src` in feed order (normalised by
    # ingestion.structured_image_gallery, which ingestion applies again), carried so the row owns its
    # gallery instead of borrowing a page scrape's. Kept only when its first image IS the row image
    # above, so nothing reorders; otherwise (no product images, the variant-swatch fallback, a first
    # image that is not http(s), a single image that adds nothing to the row image) the record carries
    # none and is what it was. A fold base keeps its own images only: images the fold adopted from folded
    # children are other listings' pictures, and each folded variant keeps its own image_url below.
    # Behind ingestion.STRUCTURED_GALLERY_FLAG (default off): off, every record is what it always was.
    from services.catalog_enrichment_agent.ingestion import structured_gallery_enabled, structured_image_gallery
    image_urls = (structured_image_gallery(_image_srcs(product))
                  if structured_gallery_enabled() and not product.get(FOLDED_IMAGES_KEY) else [])
    if len(image_urls) < 2 or image_urls[0] != str(image.get("src") or "").strip():
        image_urls = []
    # A line with multiple native variants is not one physical item, just as a
    # folded shade line is not. Never promote the first variant's GTIN to Tier-0
    # PDP identity: array order and stock/price changes must not change identity.
    # A sole native variant may provide it; all native SKU barcodes remain intact.
    barcode = (
        str(variants[0].get("barcode") or "").strip() or None
        if not product.get(FOLDED_INTO_KEY) and len(variants) == 1
        and isinstance(variants[0], dict)
        else None
    )
    # Every sellable variant: the ingest writes one SKU + offer per entry beside
    # the canonical SKU, so a folded shade line (see fold_shade_listings) keeps
    # its purchasable SKUs. Since #2120 a single-variant product emits its one
    # variant too -- the canonical SKU's source_variant_id is a storage token the
    # gateway refuses to spend against, so the merchant's own id has to ride here
    # for #2113 (SKU) and #2123 (seed) to keep it.
    sellable = [
        v for v in variants
        if isinstance(v, dict) and (_to_float(v.get("price")) or 0.0) >= MIN_SELLABLE_PRICE
    ]
    pdp_variants: List[Dict[str, Any]] = []
    # OPT-IN. Emitting variants writes one extra SKU + offer per variant downstream,
    # which changes recall fan-out, offer aggregation and INCI attachment for EVERY
    # row a caller ingests — so it never fires unless a caller asked for it.
    #
    # WHY THE FOLD IS NO LONGER THE ONLY GATE. This used to additionally require
    # `product.get(FOLDED_INTO_KEY)`, i.e. only a listing the shade-fold had just
    # BUILT could carry variants. That silently excluded every storefront that
    # publishes its shades natively, as one product with many variants — which is
    # the normal Shopify shape, not the exception. Measured on flowerbeauty.com
    # 2026-09-07: `/products.json` serves 49 products, 29 of them multi-variant,
    # carrying 185 real numeric Shopify variant ids; `fold_shade_listings` folds
    # ZERO of them (bases=0, shades=0), so the gate refused all 185 and the brand
    # ingested 49 SKUs whose `source_variant_id` was the product key. #2113 taught
    # ingestion to stop DISCARDING real variant ids; this is the other half — the
    # feed never put them in the record for it to keep.
    #
    # The two lanes admit on different rules, deliberately. A FOLDED row's variants
    # were assembled by us out of separate per-shade listings and a variant there
    # may legitimately carry a synthesised `<handle>:<i>` id (see the fallback
    # below), which is display data the shade selector needs; ingestion's own
    # provenance check decides whether it may also be sold. A NATIVE row has no
    # such excuse: its ids come straight off the merchant's own feed, so anything
    # `variant_identity` cannot positively place as merchant-issued is dropped
    # here rather than carried forward as a decoy that looks purchasable.
    native = not product.get(FOLDED_INTO_KEY)
    emit_here = emit_native_variants if native else emit_variants
    if emit_here and len(sellable) >= 1:
        seen_ids: set = set()
        base_option_name = _base_option_name(product)
        for i, v in enumerate(sellable):
            vid = str(v.get("id") or v.get("variant_id") or f"{handle}:{i}").strip()
            if vid in seen_ids:
                continue
            if native and variant_id_provenance(
                vid,
                product_id=product.get("id"),
                handle=handle,
            ) != MERCHANT_ISSUED:
                continue
            seen_ids.add(vid)
            # option1 is the merchant's own shade value and outranks a name derived
            # from the title suffix; `featured_image` is where a real Shopify variant
            # carries its swatch (`image_src` is set only by the fold).
            featured = v.get("featured_image")
            featured_src = str((featured or {}).get("src") or "").strip() if isinstance(featured, dict) else ""
            pdp_variants.append({
                "variant_id": vid,
                "sku": str(v.get("sku") or "").strip() or None,
                "barcode": str(v.get("barcode") or "").strip() or None,
                "title": str(v.get("option1") or v.get("title") or "").strip() or None,
                "option_name": _variant_option_name(v, base_option_name),
                "price": _to_float(v.get("price")),
                "in_stock": v.get("available") if isinstance(v.get("available"), bool) else None,
                "image_url": (
                    featured_src
                    or str(v.get("image_src") or "").strip()
                    or str(image.get("src") or "").strip()
                    or None
                ),
                "source_handle": str(v.get(FOLDED_FROM_KEY) or "").strip() or None,
            })
            # Only the EXTENDED fold (FOLD_AXIS_KEY) names the listing a folded variant is sold on; ingestion
            # points that variant's offer there. The legacy fold's variants carry no such key, so every
            # record and offer it produced before stays byte-identical.
            own_handle = str(v.get(FOLDED_FROM_KEY) or "").strip()
            if FOLD_AXIS_KEY in v and own_handle and "/" not in own_handle:
                pdp_variants[-1]["own_listing_url"] = f"https://{host}/products/{own_handle}"
    raw_tags = product.get("tags")
    tags = (
        raw_tags
        if isinstance(raw_tags, list)
        else [t.strip() for t in str(raw_tags or "").split(",") if t.strip()]
    )
    canonical_url = f"https://{host}/products/{handle}"
    # Where the canonical offer's item is SOLD. The product's own page, except when the extended fold replaced
    # an unsellable parent's variants: then the first sellable variant is a child's item, priced and stocked on
    # the child's own listing, and the offer goes there (its price belongs to the page it points at). The
    # record's canonical_url -- the row's key and served page -- stays the parent's.
    sold_at = canonical_url
    sold_handle = str(variant.get(FOLDED_FROM_KEY) or "").strip()
    if FOLD_AXIS_KEY in variant and sold_handle and "/" not in sold_handle:
        sold_at = f"https://{host}/products/{sold_handle}"
    category_input_path = category_path
    category_path, category_confidence = _resolve_category(
        title=title, product_type=product.get("product_type"), flag_path=category_path, domain=domain,
    )
    from services.category_path_aliases import resolve
    category_path = resolve(category_path)
    category_resolution_status = "resolved" if category_path else "unresolved"
    return {
        "pdp": {
            "brand": brand,
            "product_name": title,
            "category_path": category_path,
            "category_confidence": category_confidence,
            "category_resolution_status": category_resolution_status,
            "category_input_path": category_input_path,
            "category_source_product_type": str(product.get("product_type") or "").strip() or None,
            # Brand-authored body copy when present (it becomes the row's
            # description and feeds the lifecycle candidate gate + taxonomy
            # extractors); product_type alone otherwise. Rows minted without
            # body copy land 'draft' and rely on the description backfill /
            # LLM enrichment lane to promote.
            "attribute_summary": (
                (join_description_sentences(body_sentences) if body_sentences is not None
                 else body_html_to_text(product.get("body_html")))
                or str(product.get("product_type") or "").strip()
            ),
            "barcode": barcode,  # real GTIN when the brand fills it — strongest deposit basis
            "source_domain": host,
            "source_role": source_role,
            "tags": tags,
            # Brand-official INCI when the storefront lists it under an Ingredients
            # heading (many don't — None then, ingest skips it). brand_official is
            # the top INCI authority tier (ADR-001) so it outranks reseller lists.
            "raw_inci": inci_from_body_html(product.get("body_html")),
            "inci_source": "reseller_listing" if source_role == "retailer" else "brand_official",
            # Shopify /products.json exposes no review aggregate — ratings stay null
            # on this lane (captured on the retailer-PDP lane instead).
            "rating_value": None,
            "rating_count": None,
            # The STOREFRONT's own currency, from /meta.json. Omitted (None) rather than
            # defaulted here: direct ingestion also refuses an unknown currency.
            "currency": currency,
            "variants": pdp_variants,
            # Omitted when empty, so a product without a gallery builds the record it always did.
            **({"image_urls": image_urls} if image_urls else {}),
        },
        "offers": [
            {
                # The MERCHANT is the storefront, which is not always the brand: once
                # `resolve_record_brand` can keep a sibling brand's own vendor (APIEU on
                # misshaus.com), `brand` names the maker and the override names the shop.
                # Identical on every single-brand feed, where the two are the same string.
                "merchant_inferred": (
                    str(retailer_name or "").strip() or host
                    if source_role == "retailer"
                    else str(brand_override or "").strip() or brand
                ),
                # Seller identity must be host-based in retailer mode even when two
                # storefronts use the same friendly name. Official-mode IDs are untouched.
                "seller_domain": host if source_role == "retailer" else None,
                "canonical_url": canonical_url,
                "destination_url": sold_at,
                "image_url": str(image.get("src") or "").strip(),
                "price": price,
                "in_stock": variant.get("available") if isinstance(variant.get("available"), bool) else None,
                "validated_at": "shopify_products_json",
            }
        ],
    }


# Some storefronts (maccosmetics.com, measured 2026-09-04: 1,366 of a 1,500-product
# sample) publish EVERY shade as its own single-variant product — "Retro Matte
# Lipstick - Ruby Woo" beside the base "Retro Matte Lipstick". The Path-C plan keys
# PDPs on (brand, title), so ingesting that feed as-is mints one PDP per shade:
# ~1,900 near-duplicates for one brand.
#
# `fold_shade_listings` FOLDS those shade rows into the base listing's variants
# instead of dropping them: the base keeps one PDP, and every shade becomes a
# variant of it (title = shade name, its own sku / barcode / price / image), so
# the purchasable SKUs survive. Measured on the MAC feed, every base row is
# itself a single-variant PARENT STUB (variants[0].option1 == title, sku P2000_*):
# that stub variant is replaced by the shades, never kept beside them. A base
# that already carries real variants keeps them and gains the folded shades.
#
# Titles are compared through `normalize_title` (the same normaliser
# `make_content_key` uses downstream), because the feed is not case- or
# punctuation-stable across a line: stila lists "HUGE™ Extreme Lash Mascara" beside
# "Huge™ Extreme Lash Mascara - Intense Black", and "Heaven's" beside "Heaven’s".
# Shade names may themselves contain hyphens ("Lady-Be-Good", "Brick-O-La"), so
# every " - " split point is tried, longest base first.
_SHADE_SEP = " - "
FOLDED_FROM_KEY = "_folded_from_handle"
FOLDED_INTO_KEY = "_folded_shades"
#: Set on a fold base whose `images` the fold ADOPTED from its folded children (the base had none of its
#: own). Those are other listings' pictures: the base's row image comes from them as before, but they are
#: not the base's gallery (shopify_product_to_record).
FOLDED_IMAGES_KEY = "_images_adopted_from_folded"
# A suffix that names a merchandising state, not a shade. tarte sells "<line> - <X>
# charm" as separate $10 accessories and stila suffixes "- Last Chance"/"- Limited
# Edition" onto whole palettes; folding those makes an accessory a "shade" of the
# product it accessorises and destroys its own PDP. Measured 2026-09-05: 9 such
# false folds across the five cached feeds, 0 legitimate shades excluded.
_NON_SHADE_SUFFIX_RE = re.compile(
    r"(?i)\b(charm|last chance|limited edition|refill|travel size|mini|set|kit|bundle|gift card|sample)\b"
)
# A shade of a product costs what the product costs. A folded row priced far from its
# base is a different item wearing a similar name.
_FOLD_PRICE_RATIO = 1.5


def _shade_bases(title: str) -> List[str]:
    """Every '<base>' a '<base> - <shade>' title could be split into, longest
    base first, so 'Lip Pencil - Brick-O-La' yields ['Lip Pencil - Brick-O',
    'Lip Pencil']. Only ' - ' (space-hyphen-space) is a separator."""
    parts = title.split(_SHADE_SEP)
    return [_SHADE_SEP.join(parts[:i]).strip() for i in range(len(parts) - 1, 0, -1)]


def _image_srcs(product: Dict[str, Any]) -> List[str]:
    """Every usable image URL on a Shopify product row, in feed order."""
    out: List[str] = []
    for img in (product or {}).get("images") or []:
        src = str((img or {}).get("src") or "").strip() if isinstance(img, dict) else str(img or "").strip()
        if src:
            out.append(src)
    return out


def _first_price(product: Dict[str, Any]) -> Optional[float]:
    for v in (product or {}).get("variants") or []:
        p = _to_float((v or {}).get("price")) if isinstance(v, dict) else None
        if p is not None and p > 0:
            return p
    return None


def _fold_refused(base: Dict[str, Any], shade: Dict[str, Any], suffix: str) -> Optional[str]:
    """Why this row must NOT be folded into that base, or None to fold."""
    if _NON_SHADE_SUFFIX_RE.search(suffix or ""):
        return "non_shade_suffix"
    bp, sp = _first_price(base), _first_price(shade)
    if bp and sp and (max(bp, sp) / min(bp, sp)) > _FOLD_PRICE_RATIO:
        return "price_mismatch"
    return None


def _is_stub_variant(product: Dict[str, Any]) -> bool:
    """A single placeholder variant that names no shade: its option/title is the
    product's own title or Shopify's 'Default Title'. MAC's P2000_ parents are
    this shape; a real single-shade product ('Ruby Woo' as option1) is not."""
    variants = (product or {}).get("variants") or []
    if len(variants) != 1:
        return False
    v = variants[0] or {}
    title = str((product or {}).get("title") or "").strip()
    label = str(v.get("option1") or v.get("title") or "").strip()
    return label in ("", "Default Title", title)


def fold_shade_listings(products: List[Dict[str, Any]], *,
                        extended: bool = False) -> "Tuple[List[Dict[str, Any]], Dict[str, Any]]":
    """Pure. Fold single-variant `<base> - <shade>` rows into the variants of
    the base row (matched through normalize_title). Returns (products, report):
    the base rows now carry the shades as variants (a stub placeholder variant
    is replaced; real variants are kept and extended), the shade rows are
    removed, order is otherwise preserved, and multi-variant rows are never
    folded — a suffixed multi-variant title is a distinct line, not a shade.
    `report` names what happened so the caller can print it: bases folded,
    shade rows folded, stub variants replaced, and every folded handle by base.

    `extended` (the retailer-ingest lane's options.fold_shades): the rule set in
    `_fold_listings_extended`. Off, this is byte-identical to the fold the brand
    CLI's --fold-shades has always run."""
    if extended:
        return _fold_listings_extended(products)
    from services.catalog_identity import normalize_title

    by_norm: Dict[str, Dict[str, Any]] = {}
    for p in products:
        key = normalize_title(str((p or {}).get("title") or ""))
        if key and key not in by_norm:
            by_norm[key] = p
    folded_into: Dict[int, List[Dict[str, Any]]] = {}  # id(base) -> shade rows
    shade_of: Dict[int, Dict[str, Any]] = {}            # id(shade row) -> base
    refusals: List[Dict[str, str]] = []
    for p in products:
        title = str((p or {}).get("title") or "").strip()
        variants = (p or {}).get("variants") or []
        if len(variants) > 1:
            continue
        for base_title in _shade_bases(title):
            base = by_norm.get(normalize_title(base_title)) if base_title else None
            if base is None or base is p:
                continue
            suffix = title[len(base_title):].lstrip(" -").strip()
            refused = _fold_refused(base, p, suffix)
            if refused:
                refusals.append({"handle": str(p.get("handle") or ""), "title": title, "reason": refused})
                break
            folded_into.setdefault(id(base), []).append(p)
            shade_of[id(p)] = base
            break
    report: Dict[str, Any] = {"bases": 0, "shades": 0, "stubs_replaced": 0, "images_adopted": 0,
                             "folded": {}, "refused": refusals}
    out: List[Dict[str, Any]] = []
    for p in products:
        if id(p) in shade_of:
            continue
        shades = folded_into.get(id(p))
        if not shades:
            out.append(p)
            continue
        base_title = str(p.get("title") or "").strip()
        base = dict(p)
        own = [] if _is_stub_variant(p) else [
            dict(v, title=str(v.get("title") or v.get("option1") or "").strip())
            for v in (p.get("variants") or []) if isinstance(v, dict)
        ]
        if not own and (p.get("variants") or []):
            report["stubs_replaced"] += 1
        new_variants: List[Dict[str, Any]] = list(own)
        handles: List[str] = []
        for s in shades:
            shade_title = str(s.get("title") or "").strip()
            for bt in _shade_bases(shade_title):
                if normalize_title(bt) == normalize_title(base_title):
                    shade_name = shade_title[len(bt):].lstrip(" -").strip() or shade_title
                    break
            else:
                shade_name = shade_title
            sv = dict((s.get("variants") or [{}])[0] or {})
            # The shade row's OWN option1 is the merchant's shade value and wins:
            # stila's "Calligraphy Lip Stain - Last Chance Shade" carries
            # option1 "Elizabeth (Pinky Nude)", and taking the title suffix minted a
            # phantom second SKU for the same merchant code.
            own_label = str(sv.get("option1") or "").strip()
            if own_label and own_label.lower() not in ("default title",):
                shade_name = own_label
            sv["title"] = shade_name
            sv["option1"] = shade_name
            sv.setdefault("id", s.get("id"))
            img = _first(s.get("images")) or {}
            if img.get("src"):
                sv["image_src"] = str(img.get("src"))
            sv[FOLDED_FROM_KEY] = str(s.get("handle") or "")
            new_variants.append(sv)
            handles.append(str(s.get("handle") or ""))
        base["variants"] = new_variants
        base[FOLDED_INTO_KEY] = len(shades)
        # A parent stub carries no images of its own — measured on maccosmetics.com
        # 2026-09-05, 106 of 109 folded bases have an EMPTY `images` list while the
        # shade rows carry the swatches. The product row is what the quality scorer
        # reads (`_extract_main_image`), so a base left imageless forfeits the whole
        # images component: MAC scored 66.7 against a 71.4 gate and every row was
        # blocked `low_quality`. Adopt the folded shades' images when the base has
        # none; a base with its own images keeps them untouched.
        if not _image_srcs(p):
            adopted: List[Dict[str, Any]] = []
            seen_src: set = set()
            for s in shades:
                for src in _image_srcs(s):
                    if src not in seen_src:
                        seen_src.add(src)
                        adopted.append({"src": src})
            if adopted:
                base["images"] = adopted
                base[FOLDED_IMAGES_KEY] = True
                report["images_adopted"] += 1
        report["bases"] += 1
        report["shades"] += len(shades)
        report["folded"][str(p.get("handle") or base_title)] = handles
        out.append(base)
    return out, report


# ---- the extended fold (retailer-ingest lane, options.fold_shades) -------------------------------------
#
# Retailer storefronts often list every shade or size of a line as its own Shopify product, keep a parent
# listing beside its per-size / per-shade children, or re-list one set under a code suffix ("<Set>" and
# "<Set> - AB12CD"). The legacy fold above would mislabel or refuse those (a 30 ml / 100 ml pair is priced
# past its 1.5x ratio, a code suffix folds as a "shade", a multi-brand store can fold across vendors), so
# the retailer lane runs these rules instead. Every refusal is reported; a refused row stays the separate
# product it was.

#: The decided axis of a folded-in variant, stamped by the extended fold (`_variant_option_name` reads it).
FOLD_AXIS_KEY = "_fold_axis"
#: Shop axis names that mean quantity. A child's own Shopify option name in this set (or in
#: _SHADE_AXIS_NAMES) is used as written; any other name is not one the renderer reads as either axis.
_SIZE_AXIS_NAMES = {"size", "volume", "capacity", "weight", "amount"}
_DEFAULT_SIZE_OPTION_NAME = "Size"
#: Size siblings are priced by quantity (a 100 ml beside a 30 ml), so the 1.5x shade ratio refuses them. A
#: size sibling priced past this ratio is still a different item.
_FOLD_SIZE_PRICE_RATIO = 4.0
#: Suffixes that name a different product in the retailer feeds, on top of _NON_SHADE_SUFFIX_RE: nail
#: accessories sold beside a colour line ("<Line> - Top Coat"), multi-item sets ("<Line> - Duo"), and the
#: travel / deluxe / trial editions the legacy list only catches as "travel size" / "mini" / "sample".
_EXTENDED_NON_SHADE_SUFFIX_RE = re.compile(
    r"(?i)\b(remover|top coat|base coat|tester|brush|sharpener|case|pouch|holder|duo|trio|collection|"
    r"travel|deluxe|trial)\b"
)
#: A merchant's listing code, not a name: one upper-case token of letters AND digits, at least 3 letters
#: ("AB12CD"). Shade codes that are mostly digits ("N12", "GC014", "M0N904") do not match.
_CODE_SUFFIX_RE = re.compile(r"(?=(?:[^A-Z]*[A-Z]){3})(?=[^0-9]*[0-9])[A-Z0-9]{5,12}")
#: A formula claim names a different product, never a shade: "<Sun Fluid> - SPF 30" beside "- SPF 50".
#: Refused whatever the option names say.
_FORMULA_CLAIM_SUFFIX_RE = re.compile(r"(?i)\bSPF\s*\d+|\bPA\s*\+|\bUV[AB]?\b")
#: Finish and formula words. A suffix made ONLY of them ("<Lipstick> - Matte" / "- Satin", "<Foundation> -
#: Oil Free" / "- Waterproof" / "- Tinted", "- Matte Finish") names another formula, not a shade; so do
#: siblings that differ ONLY by them ("- Matte Red" beside "- Satin Red"). Both are refused unless the
#: child's own Shopify option names a shade or size axis. A shade NAME that merely contains one of them
#: ("Glitter Gold", "Pearl Pink", "Frosted Lilac", "Cream Beige") is a shade and folds -- which is why
#: words that are also everyday shade names on their own ("Light", "Rich", "Classic") are not listed.
_FINISH_WORDS = (r"matte|satin|gloss|glossy|shimmer|metallic|sheer|velvet|cream|creme|crème|dewy|radiant|"
                 r"luminous|glitter|frost|frosted|pearl|pearlescent|oil[- ]?free|waterproof|water[- ]?resistant|"
                 r"tinted|fragrance[- ]?free|unscented|scented|hydrating|mattifying|long[- ]?wear(?:ing)?|vegan|"
                 r"sensitive|finish|formula")
_FINISH_OR_FORMULA_SUFFIX_RE = re.compile(rf"(?i)\s*(?:{_FINISH_WORDS})(?:[\s/&+,-]+(?:{_FINISH_WORDS}))*\s*")
_FINISH_WORD_RE = re.compile(rf"(?i)\b(?:{_FINISH_WORDS})\b")


def _without_finish_words(suffix: str) -> str:
    """The suffix with every finish / formula word removed, as a comparison key ("Matte Red" -> "red")."""
    return " ".join(re.sub(r"[^\w]+", " ", _FINISH_WORD_RE.sub(" ", suffix or "")).casefold().split())
#: A bare number ("<Serum> - 30" / "- 50") is a size without a unit, a shade code, or a strength: the fold
#: cannot tell which, so it folds only when the child's own option names the axis.
_BARE_NUMBER_SUFFIX_RE = re.compile(r"\d+(?:[.,]\d+)?")
#: SKU strings that identify nothing ("N/A", "none", "-"), and too short to: a copy is never proven by one.
_PLACEHOLDER_SKUS = {"n/a", "na", "none", "null", "nil", "tbd", "tba", "default", "sku", "-", "0"}
_MIN_PROOF_SKU_ALNUM = 4
#: A proven copy of a listing is sold at the listing's price.
_COPY_PRICE_RATIO = 1.02


def _norm_label(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _variant_label(variant: Dict[str, Any]) -> str:
    """The value a variant names on its first axis, "" for Shopify's placeholder."""
    label = str((variant or {}).get("option1") or (variant or {}).get("title") or "").strip()
    return "" if label.lower() in _PLACEHOLDER_SHADE_LABELS else label


_PLACEHOLDER_SHADE_LABELS = {"", "default title", "default", "title"}


def _axis_family(name: str) -> Optional[str]:
    n = str(name or "").strip().lower()
    if n in _SHADE_AXIS_NAMES:
        return "shade"
    if n in _SIZE_AXIS_NAMES:
        return "size"
    return None


def _proof_sku(value: Any) -> Optional[str]:
    """A merchant SKU that can identify a product, casefolded, or None for a placeholder or a stub."""
    sku = str(value or "").strip().casefold()
    if sku in _PLACEHOLDER_SKUS or sum(c.isalnum() for c in sku) < _MIN_PROOF_SKU_ALNUM:
        return None
    return sku


def _same_product_proof(base: Dict[str, Any], copy: Dict[str, Any]) -> Optional[str]:
    """Why `copy` is the SAME product as `base` (a re-listing under a code suffix), or None.

    Identity only, never a resemblance: a shared VALID GTIN (check digit right, not all zeros), a shared
    merchant SKU that is not a placeholder ("N/A") or a stub, or two or more variants with the identical
    label -> price set -- and in every case the two are sold at the same price (_COPY_PRICE_RATIO). A shared
    description, image or price alone also describes every shade of a colour line, so it proves nothing."""
    bv = [v for v in (base.get("variants") or []) if isinstance(v, dict)]
    cv = [v for v in (copy.get("variants") or []) if isinstance(v, dict)]
    bp, cp = _first_price(base), _first_price(copy)
    if not bp or not cp or max(bp, cp) / min(bp, cp) > _COPY_PRICE_RATIO:
        return None
    gtins = [{validated_source_gtin(str(v.get("barcode") or "").strip()) for v in vs} - {None} for vs in (bv, cv)]
    if gtins[0] & gtins[1]:
        return "barcode"
    if ({_proof_sku(v.get("sku")) for v in bv} & {_proof_sku(v.get("sku")) for v in cv}) - {None}:
        return "sku"

    def label_prices(vs: List[Dict[str, Any]]) -> Optional[Dict[str, Optional[float]]]:
        out = {_norm_label(_variant_label(v)): _to_float(v.get("price")) for v in vs}
        return out if len(out) == len(vs) and "" not in out else None
    if len(bv) >= 2 and len(bv) == len(cv) and label_prices(bv) is not None and label_prices(bv) == label_prices(cv):
        return "variant_set"
    return None


def _is_extended_stub(product: Dict[str, Any]) -> bool:
    """A parent that SELLS NOTHING: no variant priced at or above MIN_SELLABLE_PRICE (a "choose your shade"
    parent at 0.00). Only such a parent's own variants are replaced by its children.

    NOT the legacy `_is_stub_variant` shape. A single "Default Title" variant at a real price is the normal
    retailer product: it is the item the parent page sells, at that price. Dropping it put the parent URL's
    canonical offer on the first child's price and stock (a 40.00 parent offered at its 100 ml child's 90.00).
    A parent whose item a child repeats (same SKU or barcode) keeps it too; the child is absorbed."""
    variants = [v for v in (product or {}).get("variants") or [] if isinstance(v, dict)]
    return not any((_to_float(v.get("price")) or 0.0) >= MIN_SELLABLE_PRICE for v in variants)


#: A quantity written in a title ("Glow Serum 30ml"): the parent's own item on a size axis.
_TITLE_QUANTITY_RE = re.compile(r"(?i)\b\d+(?:[.,]\d+)?\s*(?:ml|l|g|kg|mg|oz|fl\.?\s*oz)\b")
#: A title that counts units ("2 x 4.5g", "4.5g x2", "Duo", "Set of 3", "4.5g Set", "3 Pack", "25g 10 Sheets"): its
#: quantity is one unit's, not the item's size, so it names no size for the parent's own item.
_TITLE_MULTI_UNIT_RE = re.compile(
    r"(?i)\d\s*[x×]\s*\d|\b[x×]\s*\d|\d\s*[x×]\b|\b(duo|trio|set|sets|pack|packs|bundle|kit)\b"
    r"|\b\d+\s*(?:sheets?|pcs|pieces?|pads?|count|ct|capsules?|ampoules?|masks?|sachets?|patch(?:es)?|pairs?)\b")
#: Neutral labels for a parent's own placeholder item on a SIZE axis, in order of preference.
_PARENT_SIZE_LABELS = ("Standard", "Regular")


def _listing_handle_usable(base_handle: str, handle: str) -> bool:
    """Will the offer writer point a folded variant at /products/<handle>? The SAME predicate it uses
    (ingestion.own_listing_url), on the URLs the record builder spells: the base's listing and the child's,
    on one host. The fold does not know the host, and the predicate's host rule only asks that the two share
    one, so any host gives the writer's answer for the handle itself."""
    from services.catalog_enrichment_agent.ingestion import own_listing_url

    if not str(handle or "").strip() or "/" in str(handle):
        return False
    host = "fold.invalid"
    url = f"https://{host}/products/{handle}"
    return own_listing_url(f"https://{host}/products/{base_handle or handle}", url) == url


def _parent_item_label(title: str, *, taken: set) -> Optional[str]:
    """The label for a priced parent's own placeholder item on a SIZE axis, or None to leave it unlabelled.

    The ONE quantity its title states ("Glow Serum 30ml" -> "30ml"; not a multi-unit title, not
    a title naming two quantities), else "Standard" -- the first candidate no child already uses (a repeated
    label makes the seed writer drop them all). "Standard" says what the item is (the regular size), and invents
    no measurement.

    Never called for any other axis (the fold labels the item only when every folded child is a size). A colour
    name the shop never wrote ("Original", "Classic") would be presented as a real shade in the picker, so on a
    shade axis the item stays unlabelled and the existing all-or-nothing rule
    (ingestion._drop_options_that_do_not_distinguish) serves the product as a plain list of named variants: no
    picker, but nothing false in it."""
    title = title or ""
    found = [] if _TITLE_MULTI_UNIT_RE.search(title) else _TITLE_QUANTITY_RE.findall(title)
    candidates = ([" ".join(found[0].split())] if len(found) == 1 else []) + list(_PARENT_SIZE_LABELS)
    return next((c for c in candidates if _norm_label(c) not in taken), None)


def _child_axis(child: Dict[str, Any], suffix: str) -> Tuple[Optional[str], str, bool]:
    """(the shop's own axis name for this single-variant child, or None; its family; evidence).

    The family is "shade", "size", or "named:<name>" for an axis the shop named that is neither ("Scent":
    folded under the shop's own word, never relabelled a colour). `evidence` is True when the child's OWN
    `options[0].name` names a shade or size axis for a value it really carries (option1 not a placeholder):
    the merchant said what the variant is, which is what lets a finish word, a bare number or an untyped
    product fold. Without a name the value decides: a quantity is a size, anything else a shade (the legacy
    fold's assumption), with no evidence."""
    variant = ((child.get("variants") or [{}])[0]) or {}
    value = _variant_label(variant) or suffix
    name = _base_option_name(child) if _variant_label(variant) else ""
    family = _axis_family(name)
    if family:
        return name, family, True
    if name:
        return name, f"named:{name.casefold()}", False
    return None, ("size" if _looks_like_a_size(value) else "shade"), False


def _fold_refused_extended(base: Dict[str, Any], child: Dict[str, Any], suffix: str, family: str,
                           evidence: bool) -> Optional[str]:
    if _NON_SHADE_SUFFIX_RE.search(suffix or "") or _EXTENDED_NON_SHADE_SUFFIX_RE.search(suffix or ""):
        return "non_shade_suffix"
    if not evidence and _FINISH_OR_FORMULA_SUFFIX_RE.fullmatch(suffix or ""):
        return "finish_or_formula_suffix"
    if not evidence and _BARE_NUMBER_SUFFIX_RE.fullmatch((suffix or "").strip()):
        return "bare_number_suffix"
    bt, ct = (" ".join(str(p.get("product_type") or "").casefold().split()) for p in (base, child))
    if bt and ct and bt != ct:
        # A shade of a product is filed where the product is: "<Line> - Cuticle Oil" typed "Nail Care"
        # beside a "Nail Polish" line is another product sharing a title prefix.
        return "product_type_mismatch"
    if not (bt and ct) and not evidence:
        # An untyped side gives the type check nothing to compare: "<Lip Oil> - Cuticle Oil" would fold.
        return "product_type_unknown"
    # The parent's first SELLABLE price: an item under the floor (a 0.50 "choose a shade" stub, or a 0.00 sample
    # listed before the real 40.00 item) names no price for the line. A parent that sells nothing has none,
    # and its children are not price-compared.
    bp = next((p for p in (_to_float(v.get("price")) for v in (base.get("variants") or []) if isinstance(v, dict))
               if p is not None and p >= MIN_SELLABLE_PRICE), None)
    cp = _first_price(child)
    ratio = _FOLD_SIZE_PRICE_RATIO if family == "size" else _FOLD_PRICE_RATIO
    if bp and cp and (max(bp, cp) / min(bp, cp)) > ratio:
        return "price_mismatch"
    return None


def _fold_listings_extended(products: List[Dict[str, Any]]) -> "Tuple[List[Dict[str, Any]], Dict[str, Any]]":
    """Pure. The legacy fold's collapse, with the rules the retailer feeds need:

    * VENDOR-SCOPED bases: a title only matches a base of the same Shopify `vendor` (a multi-brand store
      sells "Lip Oil" by two makers).
    * SIZE SIBLINGS fold as Size variants: a quantity suffix or a child whose own option is named Size, at
      the wider _FOLD_SIZE_PRICE_RATIO.
    * OPTION NAMES from Shopify: a child's own axis name is kept, one spelling per base (the base's own
      when it names the same axis), because the seed writer drops every label of a product whose variants
      name two axes (ingestion._drop_options_that_do_not_distinguish).
    * PARENT STUBS: a parent whose one variant sells nothing (placeholder or under MIN_SELLABLE_PRICE) is
      replaced by its children; a parent's REAL variant that a child repeats (same label, SKU or barcode)
      is kept and the child is absorbed, so no variant is listed twice.
    * CODE-SUFFIX COPIES ("<Set> - AB12CD"): dropped as a re-listing of the base only with an identity
      proof (`_same_product_proof`); unproven, they are refused and stay separate rows.
    * REFUSALS on top of the legacy ones: product_type_mismatch, the extended non-shade suffixes,
      code_suffix_unproven, and chained_fold (a base that is itself a child of a shorter base keeps its
      own children rather than being folded away with them).

    Report: the legacy keys plus `axes` (variants folded per axis family), `duplicates_absorbed` and
    `copies_deduped` (by base handle), `superseded` (every listing that is no longer emitted, with the base
    that took it over and why: the rows a migration retires), and `baseless_groups` (2+ single-variant
    "<prefix> - <x>" rows with no base listing to fold into, which this fold leaves alone)."""
    from services.catalog_identity import normalize_title

    def title_of(p: Dict[str, Any]) -> str:
        return str((p or {}).get("title") or "").strip()

    def handle_of(p: Dict[str, Any]) -> str:
        return str((p or {}).get("handle") or "")

    rows = [p for p in products if isinstance(p, dict)]
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for p in rows:
        key = (_vendor_token(p.get("vendor")), normalize_title(title_of(p)))
        if key[1] and key not in by_key:
            by_key[key] = p
    refusals: List[Dict[str, str]] = []

    def refuse(p: Dict[str, Any], reason: str) -> None:
        refusals.append({"handle": handle_of(p), "title": title_of(p), "reason": reason})

    # Pass 1: each row's candidate (base, kind, suffix, axis name, family).
    target: Dict[int, Tuple[Dict[str, Any], str, str, Optional[str], str]] = {}
    for p in rows:
        title = title_of(p)
        multi = len(p.get("variants") or []) > 1
        for base_title in _shade_bases(title):
            base = by_key.get((_vendor_token(p.get("vendor")), normalize_title(base_title))) if base_title else None
            if base is None or base is p:
                continue
            suffix = title[len(base_title):].lstrip(" -").strip()
            if _FORMULA_CLAIM_SUFFIX_RE.search(suffix):
                refuse(p, "formula_claim_suffix")  # before the code test: "SPF50" is code-shaped
                break
            if _CODE_SUFFIX_RE.fullmatch(suffix):
                proof = _same_product_proof(base, p)
                if proof:
                    target[id(p)] = (base, "copy", suffix, None, proof)
                else:
                    refuse(p, "code_suffix_unproven")
                break
            if multi:
                break  # a suffixed multi-variant listing is a distinct line, as in the legacy fold
            if not _listing_handle_usable(handle_of(base), handle_of(p)):
                # A folded variant is sold on its own listing (/products/<handle>); a handle the offer writer will
                # not point at (ingestion.own_listing_url: empty, "/", "?", "#", ...) would put its offer -- or
                # the canonical one -- on the parent's page at the child's price.
                refuse(p, "child_handle_unusable")
                break
            name, family, evidence = _child_axis(p, suffix)
            refused = _fold_refused_extended(base, p, suffix, family, evidence)
            if refused:
                refuse(p, refused)
            else:
                target[id(p)] = (base, "variant", suffix, name, family)
            break
    # A base is never folded away: its own children would go with it.
    bases = {id(t[0]) for t in target.values()}
    for p in rows:
        if id(p) in target and id(p) in bases:
            del target[id(p)]
            refuse(p, "chained_fold")
    # Siblings that differ ONLY by finish / formula words ("- Matte Red" beside "- Satin Red", "- Gold" beside
    # "- Glitter Gold") are formulas of one shade: all of them refused, unless the shop's own option names
    # the axis (evidence, as in _fold_refused_extended).
    by_rest: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for p in rows:
        t = target.get(id(p))
        if t is not None and t[1] == "variant" and not _axis_family(t[3] or ""):
            by_rest.setdefault((id(t[0]), _without_finish_words(t[2])), []).append(p)
    for (_base_id, rest), group in by_rest.items():
        if rest and len({_norm_label(target[id(p)][2]) for p in group}) > 1:
            for p in group:
                del target[id(p)]
                refuse(p, "finish_variant_siblings")
    children: Dict[int, List[Dict[str, Any]]] = {}
    for p in rows:
        if id(p) in target:
            children.setdefault(id(target[id(p)][0]), []).append(p)

    report: Dict[str, Any] = {"bases": 0, "shades": 0, "stubs_replaced": 0, "images_adopted": 0,
                              "folded": {}, "refused": refusals, "axes": {"shade": 0, "size": 0, "named": 0},
                              "duplicates_absorbed": {}, "copies_deduped": {}, "superseded": [],
                              "mixed_axes": [], "canonical_moved": [], "parent_items_labelled": []}
    out: List[Dict[str, Any]] = []
    for p in products:
        if not isinstance(p, dict):
            out.append(p)
            continue
        if id(p) in target:
            continue
        kids = children.get(id(p))
        if not kids:
            out.append(p)
            continue
        base_handle = handle_of(p) or title_of(p)
        base_axis = _base_option_name(p)
        stub = _is_extended_stub(p)
        own = [] if stub else [dict(v, title=str(v.get("title") or v.get("option1") or "").strip())
                               for v in (p.get("variants") or []) if isinstance(v, dict)]
        seen_labels = {_norm_label(_variant_label(v)) for v in own} - {""}
        seen_skus = {str(v.get("sku") or "").strip().casefold() for v in own} - {""}
        seen_barcodes = {str(v.get("barcode") or "").strip() for v in own} - {""}
        # One spelling per axis family for this base: the base's own, else the first a child wrote.
        spelling: Dict[str, str] = {}
        if _axis_family(base_axis) and not stub:
            spelling[_axis_family(base_axis)] = base_axis
        for k in kids:
            _, _, _, name, family = target[id(k)]
            if name and target[id(k)][1] == "variant":
                spelling.setdefault(family, name)
        added: List[Dict[str, Any]] = []
        folded_handles: List[str] = []
        for k in kids:
            _, kind, suffix, _name, family = target[id(k)]
            superseded = {"handle": handle_of(k), "base_handle": base_handle}
            if kind == "copy":
                report["copies_deduped"].setdefault(base_handle, []).append(handle_of(k))
                report["superseded"].append({**superseded, "kind": "copy", "proof": family})
                continue
            sv = dict((k.get("variants") or [{}])[0] or {})
            label = _variant_label(sv) or suffix or title_of(k)
            sku = str(sv.get("sku") or "").strip().casefold()
            barcode = str(sv.get("barcode") or "").strip()
            if (_norm_label(label) in seen_labels or (sku and sku in seen_skus)
                    or (barcode and barcode in seen_barcodes)):
                report["duplicates_absorbed"].setdefault(base_handle, []).append(handle_of(k))
                report["superseded"].append({**superseded, "kind": "duplicate"})
                continue
            seen_labels.add(_norm_label(label))
            if sku:
                seen_skus.add(sku)
            if barcode:
                seen_barcodes.add(barcode)
            sv["title"] = label
            sv["option1"] = label
            sv.setdefault("id", k.get("id"))
            img = _first(k.get("images")) or {}
            if img.get("src"):
                sv["image_src"] = str(img.get("src"))
            sv[FOLDED_FROM_KEY] = handle_of(k)
            sv[FOLD_AXIS_KEY] = spelling.get(family) or (
                _DEFAULT_SIZE_OPTION_NAME if family == "size" else _DEFAULT_SHADE_OPTION_NAME)
            added.append(sv)
            folded_handles.append(handle_of(k))
            kind_of = "named" if family.startswith("named:") else family
            report["axes"][kind_of] += 1
            report["superseded"].append({**superseded, "kind": kind_of})
        if not added:
            out.append(p)  # only copies or repeats: the base is unchanged, and is not a folded row
            continue
        # A priced parent's own item is usually Shopify's placeholder ("Default Title"). Unlabelled beside labelled
        # children it makes the seed writer drop EVERY label (ingestion._drop_options_that_do_not_distinguish), so
        # the folded product would have no picker. It gets a label on the children's axis instead.
        added_families = {target[id(k)][4] for k in kids if handle_of(k) in folded_handles}
        # Only when EVERY folded child is a size (_parent_item_label: no shade name is invented, and across
        # mixed axes the seed writer drops the labels anyway).
        if len(own) == 1 and not _variant_label(own[0]) and added_families == {"size"}:
            label = _parent_item_label(title_of(p), taken={_norm_label(v.get("title")) for v in added})
            if label:
                own[0] = dict(own[0], title=label, option1=label,
                              **{FOLD_AXIS_KEY: spelling.get("size") or _DEFAULT_SIZE_OPTION_NAME})
                report["parent_items_labelled"].append({"base_handle": base_handle, "label": label})
        # Two axes on one product: the seed writer then drops every label and serves the bare list
        # (ingestion._drop_options_that_do_not_distinguish). Folded anyway, and named for the reviewer.
        families = {_axis_family(base_axis)} - {None} if own else set()
        families |= {target[id(k)][4] for k in kids if handle_of(k) in folded_handles}
        if len(families) > 1:
            report["mixed_axes"].append({"base_handle": base_handle, "axes": sorted(families)})
        base = dict(p)
        if stub and (p.get("variants") or []):
            report["stubs_replaced"] += 1
        if stub:
            # The parent sells nothing, so the product's canonical offer is the first sellable child's item,
            # sold on THAT child's listing (shopify_product_to_record points the offer there). The parent row
            # keeps its key and URL; its canonical offer is not on its own page.
            first = next((v for v in added if (_to_float(v.get("price")) or 0.0) >= MIN_SELLABLE_PRICE), None)
            if first is not None:
                report["canonical_moved"].append({"base_handle": base_handle, "handle": first[FOLDED_FROM_KEY]})
        base["variants"] = own + added
        base[FOLDED_INTO_KEY] = len(added)
        if not _image_srcs(p):
            adopted, seen_src = [], set()
            for k in kids:
                if target[id(k)][1] != "variant":
                    continue
                for src in _image_srcs(k):
                    if src not in seen_src:
                        seen_src.add(src)
                        adopted.append({"src": src})
            if adopted:
                base["images"] = adopted
                base[FOLDED_IMAGES_KEY] = True
                report["images_adopted"] += 1
        report["bases"] += 1
        report["shades"] += len(added)
        report["folded"][base_handle] = folded_handles
        out.append(base)
    # What this fold leaves fragmented: single-variant "<prefix> - <x>" rows with no base listing.
    groups: Dict[Tuple[str, str], List[str]] = {}
    for p in rows:
        if id(p) in target or id(p) in bases or len(p.get("variants") or []) > 1:
            continue
        parts = title_of(p).split(_SHADE_SEP)
        if len(parts) > 1 and parts[0].strip():
            groups.setdefault((_vendor_token(p.get("vendor")), parts[0].strip()), []).append(handle_of(p))
    baseless = sorted(((prefix, hs) for (_v, prefix), hs in groups.items() if len(hs) >= 2),
                      key=lambda g: (-len(g[1]), g[0]))
    report["baseless_groups"] = [{"prefix": prefix, "count": len(hs), "sample": hs[:5]} for prefix, hs in baseless]
    return out, report


def fold_report_summary(report: Optional[Dict[str, Any]], *, cap: int = 200) -> Optional[Dict[str, Any]]:
    """A bounded copy of a fold report for a crawl report / run row: every count complete, every list at
    most `cap` entries with a `*_truncated` flag (a store with a thousand shades must not bloat one ledger row)."""
    if not isinstance(report, dict):
        return None
    out: Dict[str, Any] = {k: report.get(k) for k in ("bases", "shades", "stubs_replaced", "images_adopted")}
    out["axes"] = dict(report.get("axes") or {})
    out["superseded_count"] = len(report.get("superseded") or [])
    out["refused_count"] = len(report.get("refused") or [])
    out["refused_by_reason"] = dict(collections.Counter(r.get("reason") for r in report.get("refused") or []))
    out["mixed_axes"] = list(report.get("mixed_axes") or [])[:cap]
    out["canonical_moved"] = list(report.get("canonical_moved") or [])[:cap]
    out["parent_items_labelled"] = list(report.get("parent_items_labelled") or [])[:cap]
    for key in ("superseded", "refused", "baseless_groups"):
        items = list(report.get(key) or [])
        out[key] = items[:cap]
        out[f"{key}_truncated"] = len(items) > cap
    out["baseless_group_rows"] = sum(g.get("count", 0) for g in report.get("baseless_groups") or [])
    return out


def drop_shade_listings(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compatibility name for `fold_shade_listings`: same collapse, report dropped."""
    return fold_shade_listings(products)[0]


class CurrencyNotProven(RuntimeError):
    """A storefront's own currency could not be proven, or is not the one asked for.

    Both the enumerator and direct ingestion refuse unknown currency. A blocked
    or unreadable /meta.json remains a failed observation; prices are never
    assigned USD because the source did not prove its currency.

    NEVER a conversion. This raises; it does not rewrite an amount into another
    currency. See services/region_pricing (ADR-024 commitment 5).
    """


class PresentedCurrencyMismatch(CurrencyNotProven):
    """The crawl's own `/products.json` responses were presented in a currency other than the one
    `/meta.json` proved: the prices it read are not in the currency they would be labelled with
    (`_PresentedCurrency`). The whole brand is refused before a record is built; nothing is relabelled."""


def _vendor_token(value: Optional[str]) -> str:
    """Comparison form of a Shopify `vendor` string: casefolded, whitespace collapsed.

    Deliberately NOT `catalog_identity.normalize_title`: that one is tuned for product
    titles (it keeps hyphens because 'Anti-Aging Serum' is a real distinction) and a
    vendor field is a short label where the only variation worth absorbing is case and
    stray spacing. Anything looser would be a hazard on a multi-brand retailer feed,
    which is the whole reason this exists -- cocomo.sg lists 224 vendors, and a filter
    that matched approximately would quietly pull in a neighbour brand's products under
    the target brand's name.
    """
    return " ".join(str(value or "").split()).casefold()


def filter_products_by_vendor(
    products: List[Dict[str, Any]], vendors: "Sequence[str]"
) -> List[Dict[str, Any]]:
    """Keep only the products whose Shopify `vendor` is one of `vendors`. Pure.

    A multi-brand RETAILER feed is not a brand feed. `records_for_brand`'s existing
    `brand` argument is a brand_override -- it RENAMES every product it sees -- so
    pointing it at cocomo.sg with brand='VELY VELY' would not select VELY VELY's 24
    products, it would relabel all 1,000 of that retailer's products (MEDICUBE, ANUA,
    BEAUTY OF JOSEON, ...) as VELY VELY and deposit them as brand-official anchors.
    Selection and renaming are different operations and this is the selecting one.

    Matching is exact after `_vendor_token` normalisation. An empty/None `vendors`
    returns the list unchanged -- "no filter asked for", which is what every existing
    single-brand caller means.
    """
    wanted = {_vendor_token(v) for v in (vendors or []) if str(v or "").strip()}
    if not wanted:
        return list(products)
    return [
        p for p in products
        if isinstance(p, dict) and _vendor_token(p.get("vendor")) in wanted
    ]


class CuratedRecordBatch(list):
    """Validated records and their own scan outcome, safe across concurrent calls."""
    def __init__(self, records: list, *, crawl_report: Optional[Dict[str, Any]] = None):
        super().__init__(records)
        self.crawl_report = dict(crawl_report) if crawl_report is not None else None
        # The extended shade fold's FULL report (records_for_brand(fold_extended=True)), or None: the crawl
        # report carries only a bounded summary, and a migration preview needs every superseded listing.
        self.fold_report: Optional[Dict[str, Any]] = None


async def records_for_brand(
    *,
    domain: str,
    category_path: str,
    brand: Optional[str] = None,
    collection_handles: Optional[Sequence[str]] = None,
    brand_by_vendor: Optional[Mapping[str, str]] = None,
    max_products: int = 500,
    base_listings_only: bool = False,
    # With base_listings_only: fold with the retailer lane's extended rules (`_fold_listings_extended`:
    # size siblings, parent stubs, code-suffix copies, the shop's own option names, vendor-scoped bases).
    # The batch then carries the full fold report (`.fold_report`) and its crawl report a bounded summary.
    fold_extended: bool = False,
    # Emit the merchant's OWN variants for products that are natively multi-variant
    # (the normal Shopify shape). Off by default: it adds one SKU + one offer per
    # variant, which moves recall fan-out and offer aggregation for every row the
    # caller ingests. `base_listings_only` implies it for the rows the fold builds.
    emit_real_variants: bool = False,
    only_vendors: Optional[Sequence[str]] = None,
    require_currency: Optional[str] = None,
    source_role: str = "brand_official",
    retailer_name: Optional[str] = None,
    max_scan_products: int = 10000,
    enrich_missing_inci: bool = False,
    max_pdp_inci_fetches: int = 300,
    enrich_missing_gtin: bool = False,
    max_pdp_identity_fetches: int = 100,
    # 0.0 since the shared politeness gate owns pacing. This ad-hoc sleep predates it and now
    # STACKS on top: every INCI fetch already waits its per-host interval, so a 0.3s sleep on
    # each of 300 fetches added ~90s of pure duplication. Left as a parameter rather than deleted
    # so a caller that wants extra slack on a specific brand can still ask for it.
    pdp_delay_s: float = 0.0,
    pdp_inci_budget_s: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Fetch a curated brand's storefront and return Path-C validated records.

    When `enrich_missing_inci` is set, records whose body_html carried no INCI get
    a SECOND, polite try: fetch the product's own PDP and recover the metafield /
    accordion INCI via `fetch_pdp_inci` (the cohort keeps INCI out of body_html).
    Additive — body_html INCI stays the first try and is never overwritten here;
    the fetch is capped, delayed, and best-effort (a miss leaves raw_inci None).

    `enrich_missing_gtin` optionally recovers validated missing variant barcodes
    from identity-matched product .js responses, before folding. The product-attempt
    budget is `max_pdp_identity_fetches`; batch crawl_report.gtin_recovery records
    attempts, successes, failures, capped products and actual HTTP requests.

    `source_role="retailer"` uses storefront seller identity and reseller INCI authority,
    and refuses unproven currency. Official-mode identity stays backward compatible.

    `only_vendors` selects a subset of a MULTI-BRAND retailer feed by Shopify `vendor`
    (see `filter_products_by_vendor`) — the selecting operation, as distinct from
    `brand`, which renames. Applied BEFORE the shade fold, so a fold never matches a
    base across a brand boundary.

    `require_currency` (an ISO-4217 code) refuses the whole brand with
    `CurrencyNotProven` unless the storefront's own `/meta.json` proves that currency.
    All runs require a proven currency; this option additionally asserts the
    exact requested code. No amount is converted or assigned a default currency.
    """
    if source_role not in {"brand_official", "retailer"}:
        raise ValueError("source_role must be brand_official or retailer")
    if source_role == "retailer" and (
        not isinstance(only_vendors, (list, tuple)) or not only_vendors
        or any(not isinstance(v, str) or not v.strip() for v in only_vendors)
    ):
        raise ValueError("retailer onboarding requires explicit nonempty only_vendors maker selection")
    if not isinstance(enrich_missing_gtin, bool):
        raise ValueError("enrich_missing_gtin must be a boolean")
    if type(max_pdp_identity_fetches) is not int or max_pdp_identity_fetches < 0:
        raise ValueError("max_pdp_identity_fetches must be a nonnegative integer")
    fetch_options: Dict[str, Any] = {"max_products": max_products}
    if only_vendors is not None or source_role == "retailer":
        fetch_options.update(only_vendors=only_vendors, max_scan_products=max_scan_products)
    if collection_handles:  # NOT `collections`: that name is the stdlib module this function uses
        products = await fetch_shopify_collections(domain, collection_handles, only_vendors=only_vendors,
                                                   max_products=max_products, max_scan_products=max_scan_products)
    else:
        products = await fetch_shopify_products(domain, **fetch_options)
    crawl_report = getattr(products, "crawl_report", None)
    # Compatibility/debug only; callers must use the returned batch's own report.
    records_for_brand.last_crawl_report = crawl_report  # type: ignore[attr-defined]
    # ONCE per brand, not per product: it is one storefront-wide setting and a per-product fetch
    # would multiply outbound requests by the catalogue size against a single host.
    locale = await fetch_shopify_shop_locale(domain)
    if not _ISO_CURRENCY.fullmatch(str(locale.get("currency") or "")):
        raise CurrencyNotProven(f"{domain}: storefront currency is unproven; expected {require_currency or 'an explicit currency'}; refusing ingestion")
    if require_currency:
        expected = str(require_currency).strip().upper()
        actual = locale.get("currency")
        if actual != expected:
            # BEFORE the records are built, so a refused brand cannot half-ingest.
            raise CurrencyNotProven(
                f"{domain}: expected currency {expected}, storefront /meta.json proved "
                f"{actual or 'nothing'}. Prices cannot be claimed as {expected}. "
                f"If /meta.json was unreadable, retry: a 429 bot-check "
                f"page reads exactly like a missing file. If {actual or 'the proven value'} "
                f"is genuinely right, pass --require-currency {actual or '<code>'} instead."
            )
    presented = ((crawl_report or {}).get("presented_currency") if isinstance(crawl_report, dict) else None) or {}
    proven = locale.get("currency")
    foreign = {c: n for c, n in (presented.get("pages_by_currency") or {}).items() if c != proven}
    unproven = sorted(set(presented.get("unproven_values") or ()) - {proven})
    if foreign or unproven:
        # BEFORE the records are built: those pages' prices are not in the currency they would be labelled.
        seen = ", ".join([f"{c} on {n} page(s)" for c, n in sorted(foreign.items())]
                         + [f"{c} in a conflicting or malformed cookie" for c in unproven])
        raise PresentedCurrencyMismatch(
            f"{domain}: /products.json was presented in {seen} (the cart_currency cookie of the crawl's own "
            f"responses), but /meta.json proved {proven}: those prices are not {proven} amounts. Refusing the "
            f"crawl; nothing is relabelled or converted.")
    if only_vendors is not None:
        # A filter that was ASKED FOR but normalises to nothing is refused, not skipped.
        # `--only-vendor "$VENDOR"` with the variable unset, or a jsonl row
        # `"only_vendors": [""]`, would otherwise pass an empty set to the filter, which
        # returns the whole feed — and the brand override then relabels every product of a
        # 224-vendor retailer as the target brand, signalled only by a "1000 -> 1000" line.
        wanted = [v for v in only_vendors if str(v or "").strip()]
        if not wanted:
            raise ValueError(
                f"{domain}: --only-vendor was given but every entry is blank "
                f"({list(only_vendors)!r}). Name the vendor, or drop the flag to ingest "
                f"the whole feed deliberately."
            )
        only_vendors = wanted
        before = (getattr(products, "crawl_report", None) or {}).get("scanned_products", len(products))
        products = filter_products_by_vendor(products, only_vendors)
        if not products:
            # LOUD, not empty. An unmatched vendor filter otherwise reports the same
            # "0 products" a non-Shopify storefront does, and the operator reads a typo
            # ("Vely Vely " with a stray character) as "this brand has nothing here".
            raise ValueError(
                f"{domain}: --only-vendor {list(only_vendors)!r} matched 0 of {before} "
                f"products. Check the spelling against the feed's own `vendor` values."
            )
        records_for_brand.last_vendor_filter_report = {  # type: ignore[attr-defined]
            "vendors": list(only_vendors), "before": before, "after": len(products),
        }
    # Gifts go BEFORE GTIN recovery and shade folding, so a gift costs no PDP fetch. Recorded, never silent.
    gifts_dropped: List[Dict[str, Any]] = []
    kept_products = []
    for p in products:
        gift = gift_with_purchase_reason(p)
        if gift:
            gifts_dropped.append({"handle": str(p.get("handle") or "")[:200], "title": str(p.get("title") or "")[:200],
                                  "reason": gift[:120]})
        else:
            kept_products.append(p)
    if gifts_dropped:
        products = kept_products
    identity_report = None
    if enrich_missing_gtin:
        products, identity_report = await recover_missing_variant_gtins(
            products, domain=domain, max_fetches=max_pdp_identity_fetches)
    # A brand-family storefront (misshaus.com: 89 MISSHA + 17 APIEU + 7 CHOGONGJIN)
    # is not visibly different from a single-brand one until something counts the
    # vendors. Computed from the SAME resolver the record builder uses, so the report
    # cannot drift from the decision it describes.
    brand_census: Dict[str, Dict[str, Any]] = {}
    for _p in products:
        if not isinstance(_p, dict):
            continue
        _v = str(_p.get("vendor") or "").strip()
        _resolved, _why = resolve_record_brand(_v, brand, domain)
        _slot = brand_census.setdefault(
            _v or "(no vendor)", {"count": 0, "resolved_brand": _resolved, "reason": _why}
        )
        _slot["count"] += 1
    records_for_brand.last_brand_census = {  # type: ignore[attr-defined]
        "brand_override": brand,
        "vendors": brand_census,
        "kept_vendor_count": sum(
            v["count"] for v in brand_census.values() if v["reason"] == "vendor_disagrees"
        ),
    }
    if not isinstance(fold_extended, bool):
        raise ValueError("fold_extended must be a boolean")
    fold_report: Optional[Dict[str, Any]] = None
    if base_listings_only:
        # The legacy call is spelled exactly as before, so a caller that wraps the fold keeps working.
        products, fold_report = (fold_shade_listings(products, extended=True) if fold_extended
                                 else fold_shade_listings(products))
        records_for_brand.last_fold_report = fold_report  # type: ignore[attr-defined]
    records: List[Dict[str, Any]] = []
    pairs: List[Dict[str, Any]] = []  # (product, record) needing a PDP INCI try
    # (record, its body_html sentences) for every record whose description IS its body copy (not
    # the product_type fallback): the input of the batch-level repetition pass below.
    described: List[Tuple[Dict[str, Any], List[str]]] = []
    for p in products:
        sentences = body_html_sentences((p or {}).get("body_html")) if isinstance(p, dict) else []
        rec = shopify_product_to_record(
            # brand_by_vendor (multi-brand retailer cohorts): each vendor's OWN canonical spelling, applied
            # by the same resolve_record_brand rule a single-brand job's `brand` gets.
            p, domain=domain, category_path=category_path,
            brand_override=(brand_by_vendor or {}).get(_vendor_token(p.get("vendor")), brand),
            emit_variants=base_listings_only,
            emit_native_variants=emit_real_variants,
            currency=locale.get("currency"),
            source_role=source_role, retailer_name=retailer_name,
            body_sentences=sentences,
        )
        if not rec:
            continue
        records.append(rec)
        if sentences and (rec.get("pdp") or {}).get("attribute_summary") == join_description_sentences(sentences):
            described.append((rec, sentences))
        if enrich_missing_inci and not (rec.get("pdp") or {}).get("raw_inci"):
            handle = str((p or {}).get("handle") or "").strip()
            if handle:
                pairs.append({"handle": handle, "rec": rec})
    # Store-level boilerplate: a sentence on most of THIS batch's products is the store's template.
    # Re-joined (and re-capped) from the cleaned sentences, so removed boilerplate frees room
    # under the cap for the product's own copy.
    cleaned, repeat_report = drop_repeated_store_sentences(
        [s for _, s in described],
        [description_nouns(r["pdp"].get("category_path"), r["pdp"].get("category_source_product_type"))
         for r, _ in described],
        [description_kind(r["pdp"].get("category_path"), r["pdp"].get("category_source_product_type"))
         for r, _ in described])
    for (rec, before), after in zip(described, cleaned):
        if after != before:
            rec["pdp"]["attribute_summary"] = join_description_sentences(after)
    inci_report: Optional[Dict[str, Any]] = None
    if enrich_missing_inci and pairs:
        # A wall-clock budget as well as a count: parsing a PDP for INCI is CPU-bound and scales with the page
        # (measured 2026-09-25: 2.2 s per ~2 MB luxiface.com page), so 300 fetches ran one drain stage past its
        # 3600 s task timeout -- twice -- with nothing logged. Stop early, and say so, rather than be killed.
        timeout = httpx.Timeout(15.0, connect=5.0)
        headers = {"User-Agent": _UA, "Accept": "text/html"}
        started, attempted, found, stopped = time.monotonic(), 0, 0, False
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
            for i, pair in enumerate(pairs[:max_pdp_inci_fetches]):
                if pdp_inci_budget_s is not None and time.monotonic() - started >= pdp_inci_budget_s:
                    stopped = True
                    break
                attempted += 1
                inci = await fetch_pdp_inci(domain, pair["handle"], client=client)
                if inci:
                    pair["rec"]["pdp"]["raw_inci"] = inci
                    found += 1
                if pdp_delay_s and i + 1 < min(len(pairs), max_pdp_inci_fetches):
                    await asyncio.sleep(pdp_delay_s)
        inci_report = {"candidates": len(pairs), "cap": max_pdp_inci_fetches, "attempted": attempted,
                       "found": found, "stopped_on_budget": stopped,
                       "seconds": round(time.monotonic() - started, 1)}
    # ONE spelling per brand. misshaus.com publishes both `APIEU` (16 products) and
    # `Apieu` (1); kept verbatim they are two brands to every consumer that groups by
    # the brand string, which splits a brand's catalogue for exactly the reason this
    # fix exists. Fold each `_brand_key` group onto its most common raw spelling —
    # a no-op for the override groups, whose members already share one string.
    spellings: Dict[str, "collections.Counter[str]"] = {}
    for rec in records:
        b = str((rec.get("pdp") or {}).get("brand") or "")
        if b:
            spellings.setdefault(_brand_key(b), collections.Counter())[b] += 1
    canonical = {
        k: c.most_common(1)[0][0] for k, c in spellings.items() if len(c) > 1
    }
    if canonical:
        for rec in records:
            pdp = rec.get("pdp") or {}
            b = str(pdp.get("brand") or "")
            want = canonical.get(_brand_key(b))
            if want and want != b:
                pdp["brand"] = want
        records_for_brand.last_brand_spelling_folds = canonical  # type: ignore[attr-defined]
    else:
        records_for_brand.last_brand_spelling_folds = {}  # type: ignore[attr-defined]
    report = {**crawl_report, "emitted_records": len(records),
              "gift_items_dropped": len(gifts_dropped),
              "gift_items_dropped_sample": gifts_dropped[:50]} if crawl_report is not None else None
    if report is not None and repeat_report["threshold"] is not None:
        # Only when the batch was big enough for the pass to run: evidence of what it removed.
        report["description_repeats"] = repeat_report
    if report is not None and inci_report is not None:
        report["inci_enrichment"] = inci_report
    if report is not None and identity_report is not None:
        report["gtin_recovery"] = identity_report
    storefront = shop_storefront_identity(domain)
    if report is not None and storefront is not None:
        report["storefront"] = storefront
    if report is not None and fold_report is not None and fold_extended:
        report["shade_fold"] = fold_report_summary(fold_report)
    batch = CuratedRecordBatch(records, crawl_report=report)
    batch.fold_report = fold_report if fold_extended else None
    return batch
