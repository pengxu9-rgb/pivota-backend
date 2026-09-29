"""Write storefront cart proofs for ENRICHMENT catalog rows (option 2, PR B). Dry-run by default.

    python -m jobs.enrichment_cart_variant_proof --domain tartecosmetics.com            # dry run
    python -m jobs.enrichment_cart_variant_proof --domain tartecosmetics.com --apply    # write

WHAT IT DOES. For each `--domain` (required, repeatable; no default population), it selects the
live `catalog_enrichment_agent_v1` products of that storefront with their live `::canonical`
placeholder and `::v:` skus, reads the storefront, and computes ONE row per (product_key, sku_key)
for `enrichment_cart_variant_proofs` (db/enrichment_cart_variant_proofs.py): an 'ok' proof, or a
non-ok outcome that says why not. `--apply` upserts them; without it nothing is written anywhere.
The run ends with one JSON report line (`PROOF_REPORT {...}`): per-domain outcomes, the currency
evidence, and the drift between the live price and the current catalog offer.

THE PROOF CONTRACT is the module docstring of services/reap_enrichment_cart_proof.py, and this
writer follows it clause by clause (the tests replay every ok row this job builds through
`verify_enrichment_cart_proof`, the reader):
  * product_key, sku_key   the catalog row, exactly. One proof per sku.
  * shop_host              the host this job REQUESTED: the canonical_url host. A response whose
                           final host fails `_same_storefront_host` against it is the non-ok
                           outcome `host_redirected` (maccosmetics.com -> www.maccosmetics.com is
                           the same storefront; anything else is not).
  * handle                 Shopify's own `handle` from the response, verbatim. A response whose
                           handle is not the one the sku expects (`sku_payload.source_handle`,
                           else the canonical_url's) is `handle_mismatch`, never re-keyed.
  * variant_id             taken from the response. 'ok' only when the sku's own numeric id
                           (`source_variant_id`, strictly) is among that handle's variants; for
                           the `::canonical` placeholder, only when the handle has exactly ONE
                           variant and the catalog has NO `::v:` sku for the product at all
                           (suppressed ones counted, the carry-over from #2460's review).
  * live_variant_count     ALL variants on the handle, available or not. A variant list this job
                           cannot read in full (a malformed entry, a duplicate id, 100+ entries,
                           which /products.json may have truncated) is `payload_malformed` /
                           `variant_count_unverifiable`, never a partial count.
  * available              that variant's own `available`, a JSON boolean or the handle is refused.
  * live_price_minor       ISO minor units via the rail's one converter
                           (`db.reap_agentic_ledger.amount_minor_or_none`, which refuses rather
                           than rounds). `.js` prices are x100 for EVERY currency, JPY included
                           (measured: luafee.jp `.js` 220000 = JPY 2,200), so a `.js` price is
                           divided by 100 into a major amount first; /products.json prices are
                           major-unit strings and go in as they are.
  * currency               the currency the price was ACTUALLY read in. See THE CURRENCY RULE.
  * source                 `products_json_v1` (read off a /products.json page) or
                           `products_js_v1` (one `/products/<handle>.js`); both in PROOF_SOURCES.
  * checked_at             when that response was read; updated_at is SET on every write.

THE CURRENCY RULE, AND WHY IT FAILS CLOSED. Neither `/products.json` nor `.js` names its currency,
and the presentment currency follows the requester's geography and the store's Markets setup.
Measured 2026-09-29 from a laptop that Shopify geolocates to JP:
  * tartecosmetics.com  `.js` plain / `?country=US` / `?currency=USD` / a `localization=US` cookie:
                        price 3000, `Set-Cookie: cart_currency=USD`. `?country=GB`: price 2700
                        and `cart_currency=GBP` ON THE SAME RESPONSE. /products.json behaves the
                        same (`35.00` USD vs `33.00` GBP). Multi-currency: the body alone lies.
  * jsmbeauty.sg        every one of those variants (US included): 3000 and `cart_currency=SGD`.
                        A single-currency SGD store; it does not convert for a US visitor.
  * /meta.json          `currency` is the SHOP's base currency (tarte: USD even when it
                        presents GBP): not the read currency.
  * /browsing_context_suggestions.json  the DETECTED COUNTRY (JP here), no currency.
  * /cart.js            `currency` of the session, but in a SEPARATE response: it cannot vouch
                        for the body another response carried.
  * luafee.jp           sends NO `cart_currency` cookie at all (neither `.js` nor /cart.js).
So the read currency is the `cart_currency` Set-Cookie of THE SAME final response that carried
the prices: exactly one value, three upper-case letters. Every request carries no cookies (a
cookie sent back could suppress the Set-Cookie) and asks `?country=<market>` so a multi-currency
store presents the market's currency where it can. Then:
  * no cookie, two different values, or a malformed one  -> `currency_unverified`;
  * a currency that is not the market's                     -> `currency_not_market`;
  * only the market's own currency (routes.agent_commerce_reap._MARKET_CURRENCY, the map the
    purchase lane prices with) can be 'ok'.
A store like luafee.jp therefore never gets an 'ok' proof from this job. That is the intended
direction: a guessed currency is how a yen amount gets charged as dollars.

WHICH ENDPOINT, AND WHY (`--source`, default `auto`). Both carry `handle`, variant ids, `available`
and the per-response currency cookie. `/products.json` is ~250 products per request; `.js` is one
handle per request, and MAC alone has ~1,870 handles (286 products, 1,583 folded shades, each its own
Shopify product), i.e. ~95 minutes of paced requests against one Cloudflare-fronted store per run.
`auto` reads `/meta.json` once, estimates the listing's page count from `published_products_count`
(plus the lookahead page), and pages `/products.json` when that is fewer requests than one `.js`
per handle; otherwise, or when /meta.json is unreadable, it reads `.js` per handle. A handle the
COMPLETE listing did not contain, or listed twice (paging drift), is read by `.js` instead (a
renamed product answers there with its new handle -> `handle_mismatch`; a deleted one 404s ->
`revoked_404`). An INCOMPLETE listing (a blocked page, the 100-page cap, a repeated page) proves
nothing about the handles it did not reach: they are reported `listing_incomplete` and nothing is
written for them. Exhaustion is an EMPTY page, never a short one (bluemercury serves 249-product
pages mid-catalogue; services/curated_brand_feed.py measured it).

PARENT STUBS (`parent_stub`). MAC's storefront keeps a parent product per shade family whose ONE
variant restates the product title (option "Title" = "Studio Fix Fluid SPF 15 ...", sku
`P2000_120613`, no image, no barcode) while each shade is its own product with real options, images
and a barcode. Buying the parent buys that stub. Spot-checked 2026-09-29: the known family parent,
and two canonical-only catalog rows (Fix+, Connect In Colour palette: Rose Lens), all three carry that
signature, while the NC50 shade does not. Shopify's own default single variant is "Default Title",
so "Default Title" is NOT the signal (it is every ordinary one-variant product, tarte's brushes
included). The rule is structural and store-independent: a handle whose sole variant's title
equals the product title (case-folded) is a parent stub, and every sku on it is `parent_stub`.
It fails closed: an ordinary product that happens to name its lone variant after itself is refused
too, and shows up in the report by count.

WHAT IS NOT WRITTEN. A transient read (429/403/5xx/timeout/a challenge page served as 200) is not
evidence of anything: nothing is written for those skus, a prior proof keeps its checked_at and
ages out after the verifier's 72h. Neither is a row this job cannot name a proof FOR (an unusable
canonical_url, a canonical host that is not the requested domain, an unreadable source_handle).
Everything else, every definitive refusal included, IS written, so a proof that stopped being
true is revoked by the same run that found out.

WHERE AND HOW IT RUNS. Only on the crawl subnet (`SUBNET=pivota-crawl`, NAT 34.82.199.35), never
the default NAT whose address payment partners allowlist. Only for domains on Pivota's Tier B
cart-link list (config/tierb_cart_link_merchants.json via services.tierb_cart_link_merchants), which
also gives each domain its market. Requests start at least `ENRICHMENT_PROOF_REQUEST_GAP_S` apart
(default 3.0 s, floor 1.5 s), one domain at a time; `ENRICHMENT_PROOF_ABORT_AFTER_BLOCKS` (default
5) consecutive block-shaped answers (429, 403, 5xx, a transport error) abort the whole run, since
the 2026-08-21 block was IP-level and cross-domain. A dry run FETCHES just the same: `--apply`
gates the write, not the crawl.

NOT SCHEDULED. Nothing here is registered with services/audit_scheduler or any Cloud Scheduler
trigger, and no default domain list exists; a test pins both. Provisioning a job is a separate,
deliberate step (see the PR that introduced this file for the exact commands).

EXIT CODES: 0 done; 1 aborted on a block; 2 bad arguments or a domain not on the Tier B list.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from db.enrichment_cart_variant_proofs import OUTCOME_OK, PROOF_SOURCES, TABLE  # noqa: E402
from db.reap_agentic_ledger import amount_minor_or_none  # noqa: E402
# The map the purchase lane prices with. One map, so the proof's currency and the lane's agree.
from routes.agent_commerce_reap import _MARKET_CURRENCY  # noqa: E402
from services.curated_brand_feed import _same_storefront_host  # noqa: E402
from services.reap_enrichment_cart_proof import (  # noqa: E402
    ENRICHMENT_SOURCE_SYSTEM,
    PLACEHOLDER_SUFFIX,
    VARIANT_INFIX,
    _expected_handle,
    _sku_payload,
    _sku_variant,
    enrichment_offer_price_ok,
    storefront_page,
)
from services.shopify_variant_identity import MAX_VARIANTS  # noqa: E402
from services.tierb_cart_link_merchants import (  # noqa: E402
    MerchantListError,
    load_merchants,
    normalize_domain,
    select_merchants,
)

logger = logging.getLogger(__name__)

SOURCE_PRODUCTS_JSON = "products_json_v1"
SOURCE_PRODUCTS_JS = "products_js_v1"
assert (SOURCE_PRODUCTS_JSON, SOURCE_PRODUCTS_JS) == PROOF_SOURCES

# ── outcomes ────────────────────────────────────────────────────────────────────────────────────
# Written (definitive): the storefront answered and the answer refuses the sku.
REVOKED_404 = "revoked_404"
HOST_REDIRECTED = "host_redirected"
HANDLE_MISMATCH = "handle_mismatch"
PAYLOAD_MALFORMED = "payload_malformed"
VARIANT_COUNT_UNVERIFIABLE = "variant_count_unverifiable"
PARENT_STUB = "parent_stub"
PLACEHOLDER_HAS_VARIANT_SKUS = "placeholder_has_variant_skus"
PLACEHOLDER_MULTI_VARIANT = "placeholder_multi_variant"
SKU_VARIANT_UNVERIFIED = "sku_variant_unverified"
VARIANT_GONE = "variant_gone"
CURRENCY_UNVERIFIED = "currency_unverified"
CURRENCY_NOT_MARKET = "currency_not_market"
PRICE_UNREADABLE = "price_unreadable"
WRITTEN_OUTCOMES = frozenset({
    OUTCOME_OK, REVOKED_404, HOST_REDIRECTED, HANDLE_MISMATCH, PAYLOAD_MALFORMED,
    VARIANT_COUNT_UNVERIFIABLE, PARENT_STUB, PLACEHOLDER_HAS_VARIANT_SKUS, PLACEHOLDER_MULTI_VARIANT,
    SKU_VARIANT_UNVERIFIED, VARIANT_GONE, CURRENCY_UNVERIFIED, CURRENCY_NOT_MARKET, PRICE_UNREADABLE,
})
# Skipped (nothing written): no evidence, or no proof this job can name.
SKIP_CANONICAL_URL_UNUSABLE = "skip_canonical_url_unusable"
SKIP_HOST_NOT_DOMAIN = "skip_canonical_host_not_domain"
SKIP_SOURCE_DOMAIN_MISMATCH = "skip_source_domain_mismatch"
SKIP_SKU_PAYLOAD_MALFORMED = "skip_sku_payload_malformed"
SKIP_LISTING_INCOMPLETE = "skip_listing_incomplete"
SKIP_TRANSIENT = "skip_transient"  # suffixed with the fetch outcome in the report

# ── pacing ──────────────────────────────────────────────────────────────────────────────────────
USER_AGENT = os.getenv("EXTERNAL_OFFER_USER_AGENT") or "Mozilla/5.0 (compatible; PivotaBot/1.0; +https://pivota.cc)"
MIN_REQUEST_GAP_FLOOR_S = 1.5
REQUEST_TIMEOUT_S = 20.0
PER_PAGE = 250
#: Shopify serves /products.json pages 1..100 only (services/curated_brand_feed.SHOPIFY_MAX_PAGES).
MAX_LISTING_PAGES = 100
BLOCK_OUTCOMES = frozenset({"rate_limited", "http_403", "http_500", "http_502", "http_503",
                            "http_504", "http_520", "http_521", "http_522", "http_524"})
DEFAULT_LIMIT = 2000

_CURRENCY = re.compile(r"[A-Z]{3}")
_CART_CURRENCY_COOKIE = "cart_currency"


def request_gap_s(environ: Optional[Mapping[str, str]] = None) -> float:
    env = os.environ if environ is None else environ
    raw = env.get("ENRICHMENT_PROOF_REQUEST_GAP_S")
    try:
        value = float(raw) if raw not in (None, "") else 3.0
    except ValueError:
        value = 3.0
    if not math.isfinite(value):
        value = 3.0
    return max(value, MIN_REQUEST_GAP_FLOOR_S)


def abort_after_blocks(environ: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if environ is None else environ
    try:
        value = int(env.get("ENRICHMENT_PROOF_ABORT_AFTER_BLOCKS") or 5)
    except ValueError:
        value = 5
    return max(1, value)


def is_block(outcome: str) -> bool:
    return outcome in BLOCK_OUTCOMES or outcome.startswith("error:")


# ── pure: the currency a response was read in ───────────────────────────────────────────────────


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


# ── pure: one storefront product, parsed strictly ───────────────────────────────────────────────


@dataclass(frozen=True)
class LiveVariant:
    variant_id: str
    available: bool
    price: Any  # raw: an int (`.js`, x100) or a major-unit string (/products.json)
    title: Optional[str]


@dataclass(frozen=True)
class LiveProduct:
    handle: str
    product_id: str
    title: str
    variants: Tuple[LiveVariant, ...]

    @property
    def live_variant_count(self) -> int:
        return len(self.variants)

    def variant(self, variant_id: str) -> Optional[LiveVariant]:
        for live in self.variants:
            if live.variant_id == variant_id:
                return live
        return None


def _shopify_id(value: Any) -> Optional[str]:
    """A Shopify numeric id from the response: a positive JSON integer (never a bool)."""
    if type(value) is int and value > 0:
        return str(value)
    return None


def parse_live_product(product: Any) -> Tuple[Optional[LiveProduct], Optional[str]]:
    """`(LiveProduct, None)` or `(None, outcome)`. All-or-nothing: live_variant_count counts ALL
    variants, so a list with one unreadable entry cannot be counted and the handle is refused."""
    if not isinstance(product, Mapping):
        return None, PAYLOAD_MALFORMED
    handle = product.get("handle")
    product_id = _shopify_id(product.get("id"))
    raw_variants = product.get("variants")
    if not isinstance(handle, str) or not handle or product_id is None or not isinstance(raw_variants, list):
        return None, PAYLOAD_MALFORMED
    if not raw_variants:
        return None, PAYLOAD_MALFORMED
    if len(raw_variants) >= MAX_VARIANTS:
        # /products.json may stop at 100 variants; a count that may be truncated is not a count.
        return None, VARIANT_COUNT_UNVERIFIABLE
    variants: List[LiveVariant] = []
    seen = set()
    for raw in raw_variants:
        if not isinstance(raw, Mapping):
            return None, PAYLOAD_MALFORMED
        variant_id = _shopify_id(raw.get("id"))
        available = raw.get("available")
        if variant_id is None or variant_id in seen or type(available) is not bool:
            return None, PAYLOAD_MALFORMED
        seen.add(variant_id)
        title = raw.get("title")
        variants.append(LiveVariant(variant_id=variant_id, available=available, price=raw.get("price"),
                                    title=title if isinstance(title, str) else None))
    title = product.get("title")
    return LiveProduct(handle=handle, product_id=product_id, title=title if isinstance(title, str) else "",
                       variants=tuple(variants)), None


def is_parent_stub(live: LiveProduct) -> bool:
    """The sole variant restates the product title (MAC's family parent). Shopify's own default
    single variant is 'Default Title', which is NOT this."""
    if live.live_variant_count != 1:
        return False
    product_title = live.title.strip().casefold()
    variant_title = (live.variants[0].title or "").strip().casefold()
    return bool(product_title) and variant_title == product_title


def live_price_minor(price: Any, currency: str, source: str) -> Optional[int]:
    """The variant's price in ISO minor units of `currency`, or None (refused, never rounded)."""
    if source == SOURCE_PRODUCTS_JS:
        # `.js` is x100 for EVERY currency. An int only: a float or a string is not this shape.
        if type(price) is not int:
            return None
        return amount_minor_or_none(Decimal(price) / Decimal(100), currency)
    if source == SOURCE_PRODUCTS_JSON:
        # Major units, as a string. A JSON number is accepted only as a Decimal (the fetch parses
        # floats as Decimal); a binary float is refused by the converter itself.
        if isinstance(price, bool) or not isinstance(price, (str, Decimal, int)):
            return None
        try:
            value = price if isinstance(price, Decimal) else Decimal(str(price).strip())
        except (InvalidOperation, ValueError):
            return None
        return amount_minor_or_none(value, currency)
    return None


# ── evidence per handle ─────────────────────────────────────────────────────────────────────────


@dataclass
class HandleEvidence:
    """What one response says about one handle. Exactly one of: `live` (parsed), `outcome` (a
    definitive refusal for every sku on the handle), `transient` (no evidence: write nothing)."""
    requested_handle: str
    source: str
    checked_at: Optional[datetime] = None
    live: Optional[LiveProduct] = None
    currency: Optional[str] = None
    currency_problem: Optional[str] = None
    outcome: Optional[str] = None
    transient: Optional[str] = None


@dataclass
class ProofRow:
    product_key: str
    sku_key: str
    shop_host: str
    handle: str
    source: str
    checked_at: datetime
    outcome: str
    shopify_product_id: Optional[str] = None
    variant_id: Optional[str] = None
    live_variant_count: Optional[int] = None
    available: Optional[bool] = None
    live_price_minor: Optional[int] = None
    currency: Optional[str] = None

    def as_params(self, written_at: datetime) -> Dict[str, Any]:
        return {
            "product_key": self.product_key, "sku_key": self.sku_key, "shop_host": self.shop_host,
            "handle": self.handle, "shopify_product_id": self.shopify_product_id,
            "variant_id": self.variant_id, "live_variant_count": self.live_variant_count,
            "available": self.available, "live_price_minor": self.live_price_minor,
            "currency": self.currency, "source": self.source, "checked_at": self.checked_at,
            "outcome": self.outcome, "updated_at": written_at,
        }


def evidence_from_product(product: Any, *, requested_handle: str, source: str, checked_at: datetime,
                          currency: Optional[str], currency_problem: Optional[str]) -> HandleEvidence:
    live, failure = parse_live_product(product)
    evidence = HandleEvidence(requested_handle=requested_handle, source=source, checked_at=checked_at,
                              currency=currency, currency_problem=currency_problem)
    if live is None:
        evidence.outcome = failure
        return evidence
    if live.handle != requested_handle:
        evidence.outcome = HANDLE_MISMATCH
        return evidence
    evidence.live = live
    return evidence


# ── pure: the one proof row a sku gets ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SkuTarget:
    """A catalog sku this run proves, with everything the decision needs."""
    product_key: str
    sku_key: str
    shop_host: str
    handle: str
    placeholder: bool
    source_variant_id: Any
    catalog_variant_sku_count: int


def target_for_row(row: Mapping[str, Any], domain: str) -> Tuple[Optional[SkuTarget], Optional[str]]:
    """`(SkuTarget, None)` or `(None, skip_reason)` for one selected (product, sku) row."""
    page = storefront_page(row.get("canonical_url"))
    if page is None:
        return None, SKIP_CANONICAL_URL_UNUSABLE
    host, canonical_handle = page
    if not _same_storefront_host(domain, host):
        return None, SKIP_HOST_NOT_DOMAIN
    if not _same_storefront_host(host, row.get("source_domain") if isinstance(row.get("source_domain"), str) else ""):
        return None, SKIP_SOURCE_DOMAIN_MISMATCH
    payload = _sku_payload(row.get("sku_payload"))
    if payload is None:
        return None, SKIP_SKU_PAYLOAD_MALFORMED
    handle = _expected_handle(payload, canonical_handle)
    if handle is None:
        return None, SKIP_SKU_PAYLOAD_MALFORMED
    product_key = row["product_key"]
    sku_key = row["sku_key"]
    count = row.get("catalog_variant_sku_count")
    return SkuTarget(
        product_key=product_key, sku_key=sku_key, shop_host=host, handle=handle,
        placeholder=sku_key == product_key + PLACEHOLDER_SUFFIX,
        source_variant_id=row.get("source_variant_id"),
        catalog_variant_sku_count=int(count) if count is not None else 0,
    ), None


def decide_proof(target: SkuTarget, evidence: HandleEvidence, *, market_currency: str) -> ProofRow:
    """The row THE PROOF CONTRACT allows for this sku given this evidence. Call only with evidence
    that is not transient. Order: identity first (what is this sku on the storefront?), then the
    currency, then the price, so the recorded refusal is the most specific one."""
    if evidence.transient is not None or evidence.checked_at is None:
        raise ValueError("decide_proof needs definitive evidence")
    row = ProofRow(product_key=target.product_key, sku_key=target.sku_key, shop_host=target.shop_host,
                   handle=target.handle, source=evidence.source, checked_at=evidence.checked_at,
                   outcome=OUTCOME_OK)
    if evidence.outcome is not None:
        row.outcome = evidence.outcome
        return row
    live = evidence.live
    if live is None:
        raise ValueError("definitive evidence without a live product or an outcome")
    row.handle = live.handle
    row.shopify_product_id = live.product_id
    row.live_variant_count = live.live_variant_count
    if is_parent_stub(live):
        row.outcome = PARENT_STUB
        return row
    if target.placeholder:
        if target.catalog_variant_sku_count != 0:
            row.outcome = PLACEHOLDER_HAS_VARIANT_SKUS
            return row
        if live.live_variant_count != 1:
            row.outcome = PLACEHOLDER_MULTI_VARIANT
            return row
        variant = live.variants[0]
    else:
        # None when the id is not a strict Shopify id, and None on a key/id contradiction too.
        sku_variant = _sku_variant(target.sku_key, target.product_key, target.source_variant_id)[0]
        if sku_variant is None:
            row.outcome = SKU_VARIANT_UNVERIFIED
            return row
        variant = live.variant(sku_variant)
        if variant is None:
            row.outcome = VARIANT_GONE
            return row
    row.variant_id = variant.variant_id
    row.available = variant.available
    if evidence.currency is None:
        row.outcome = CURRENCY_UNVERIFIED
        return row
    row.currency = evidence.currency
    if evidence.currency != market_currency:
        row.outcome = CURRENCY_NOT_MARKET
        return row
    price = live_price_minor(variant.price, evidence.currency, evidence.source)
    if price is None:
        row.outcome = PRICE_UNREADABLE
        return row
    row.live_price_minor = price
    return row


# ── I/O: fetching ───────────────────────────────────────────────────────────────────────────────


class Pacer:
    """Request STARTS at least `gap_s` apart, across the whole run (one domain at a time)."""

    def __init__(self, gap_s: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self.gap_s = gap_s
        self._clock = clock
        self._sleep = sleep
        self._last: Optional[float] = None
        self.requests = 0

    async def wait(self) -> None:
        if self._last is not None:
            delay = self.gap_s - (self._clock() - self._last)
            if delay > 0:
                await self._sleep(delay)
        self._last = self._clock()
        self.requests += 1


@dataclass
class Fetched:
    outcome: str  # "ok", "not_found", "host_redirected", or a transient class
    payload: Any = None
    set_cookies: Tuple[str, ...] = ()
    checked_at: Optional[datetime] = None


async def fetch_json(client: Any, url: str, *, requested_host: str, pacer: Pacer,
                     now: Callable[[], datetime]) -> Fetched:
    """GET `url` with no cookies. Never raises: every failure is a classified outcome."""
    await pacer.wait()
    try:
        client.cookies.clear()
    except AttributeError:
        pass
    try:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                timeout=REQUEST_TIMEOUT_S, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 - classified, not swallowed
        return Fetched(outcome=f"error:{type(exc).__name__}")
    checked_at = now()
    final = urlsplit(str(resp.url))
    if final.scheme != "https" or not _same_storefront_host(requested_host, final.hostname or ""):
        return Fetched(outcome=HOST_REDIRECTED, checked_at=checked_at)
    if resp.status_code == 429:
        return Fetched(outcome="rate_limited")
    if resp.status_code in (404, 410):
        return Fetched(outcome="not_found", checked_at=checked_at)
    if resp.status_code != 200:
        return Fetched(outcome=f"http_{resp.status_code}")
    ctype = (resp.headers.get("content-type") or "").lower()
    if "json" not in ctype and "javascript" not in ctype:
        # A challenge page and a themed soft-404 look alike: neither is evidence.
        return Fetched(outcome="not_json")
    try:
        payload = json.loads(resp.text, parse_float=Decimal)
    except ValueError:
        return Fetched(outcome="unparseable")
    return Fetched(outcome="ok", payload=payload, checked_at=checked_at,
                   set_cookies=tuple(resp.headers.get_list("set-cookie")))


def product_js_url(host: str, handle: str, market: str) -> str:
    # `%` stays as it is so a handle the catalog stored percent-encoded is not encoded twice.
    return f"https://{host}/products/{quote(handle, safe='%')}.js?country={market}"


def products_json_url(host: str, page: int, market: str) -> str:
    return f"https://{host}/products.json?limit={PER_PAGE}&page={page}&country={market}"


def meta_json_url(host: str) -> str:
    return f"https://{host}/meta.json"


async def read_handle_js(client: Any, host: str, handle: str, market: str, *, pacer: Pacer,
                         now: Callable[[], datetime]) -> Tuple[HandleEvidence, str]:
    """`(evidence, fetch_outcome)` for one `/products/<handle>.js`."""
    fetched = await fetch_json(client, product_js_url(host, handle, market), requested_host=host,
                               pacer=pacer, now=now)
    evidence = HandleEvidence(requested_handle=handle, source=SOURCE_PRODUCTS_JS, checked_at=fetched.checked_at)
    if fetched.outcome == "not_found":
        evidence.outcome = REVOKED_404
    elif fetched.outcome == HOST_REDIRECTED:
        evidence.outcome = HOST_REDIRECTED
    elif fetched.outcome != "ok":
        evidence.transient = fetched.outcome
    else:
        currency, problem = presentment_currency(fetched.set_cookies)
        evidence = evidence_from_product(fetched.payload, requested_handle=handle, source=SOURCE_PRODUCTS_JS,
                                         checked_at=fetched.checked_at, currency=currency,
                                         currency_problem=problem)
    return evidence, fetched.outcome


@dataclass
class Listing:
    """What /products.json said, handle -> evidence. `complete` only after an EMPTY page."""
    by_handle: Dict[str, HandleEvidence] = field(default_factory=dict)
    duplicates: set = field(default_factory=set)
    complete: bool = False
    pages: int = 0
    stop: Optional[str] = None
    host_redirected: bool = False


async def read_listing(client: Any, host: str, market: str, *, pacer: Pacer, now: Callable[[], datetime],
                       on_fetch: Callable[[str], bool]) -> Listing:
    """Page /products.json to its empty page. `on_fetch(outcome)` returns False to abort."""
    listing = Listing()
    seen_pages: set = set()
    for page in range(1, MAX_LISTING_PAGES + 1):
        fetched = await fetch_json(client, products_json_url(host, page, market), requested_host=host,
                                   pacer=pacer, now=now)
        listing.pages = page
        if not on_fetch(fetched.outcome):
            listing.stop = "aborted_on_block"
            return listing
        if fetched.outcome == HOST_REDIRECTED:
            listing.host_redirected = True
            listing.stop = HOST_REDIRECTED
            return listing
        if fetched.outcome != "ok":
            listing.stop = f"page_{page}_{fetched.outcome}"
            return listing
        body = fetched.payload
        products = body.get("products") if isinstance(body, Mapping) else None
        if not isinstance(products, list):
            listing.stop = f"page_{page}_invalid_envelope"
            return listing
        if not products:
            listing.complete = True
            return listing
        fingerprint = json.dumps(products, sort_keys=True, default=str)
        if fingerprint in seen_pages:
            listing.stop = f"page_{page}_repeated"
            return listing
        seen_pages.add(fingerprint)
        currency, problem = presentment_currency(fetched.set_cookies)
        for product in products:
            handle = product.get("handle") if isinstance(product, Mapping) else None
            if not isinstance(handle, str) or not handle:
                continue
            if handle in listing.by_handle or handle in listing.duplicates:
                listing.duplicates.add(handle)
                listing.by_handle.pop(handle, None)
                continue
            listing.by_handle[handle] = evidence_from_product(
                product, requested_handle=handle, source=SOURCE_PRODUCTS_JSON,
                checked_at=fetched.checked_at, currency=currency, currency_problem=problem)
    listing.stop = "page_cap"
    return listing


async def estimate_listing_requests(client: Any, host: str, *, pacer: Pacer,
                                    now: Callable[[], datetime]) -> Optional[int]:
    """Pages /products.json would take (plus its empty lookahead page), from /meta.json."""
    fetched = await fetch_json(client, meta_json_url(host), requested_host=host, pacer=pacer, now=now)
    if fetched.outcome != "ok" or not isinstance(fetched.payload, Mapping):
        return None
    count = fetched.payload.get("published_products_count")
    if type(count) is not int or count < 0:
        return None
    return math.ceil(count / PER_PAGE) + 1


# ── SQL ─────────────────────────────────────────────────────────────────────────────────────────
# Both dialects (the unit tests run it on SQLite, tests/*_postgres.py on Postgres). The variant
# count is EVERY `::v:` sku of the product, suppressed ones included: a live-only count of 0 would
# let the placeholder stand for a MAC family whose shades are merely suppressed. `substr` rather
# than LIKE: a product_key may hold `_`, which LIKE reads as a wildcard.
SELECT_TARGETS_SQL = """
    WITH p AS (
        SELECT product_key, source_domain, canonical_url
          FROM catalog_products
         WHERE source_system = :source_system
           AND suppressed_at IS NULL AND suppression_reason IS NULL
           AND lower(source_domain) IN (:domain, :www_domain)
           {cursor_clause}
         ORDER BY product_key
         LIMIT :limit
    )
    SELECT p.product_key, p.source_domain, p.canonical_url,
           s.sku_key, s.source_variant_id, s.sku_payload,
           (SELECT count(*) FROM catalog_skus v
             WHERE v.product_key = p.product_key
               AND substr(v.sku_key, 1, length(p.product_key) + 4) = p.product_key || '::v:')
               AS catalog_variant_sku_count
      FROM p
      LEFT JOIN catalog_skus s
        ON s.product_key = p.product_key
       AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
       AND (s.sku_key = p.product_key || '::canonical'
            OR substr(s.sku_key, 1, length(p.product_key) + 4) = p.product_key || '::v:')
     ORDER BY p.product_key, s.sku_key
"""

# The drift report's reading of the offers PR C prices from: same sku, not suppressed, priced, not
# out of stock (routes/agent_commerce_reap._CART_ALL_OFFERS_SQL's filters, minus the seller and
# currency ones, which `enrichment_offer_price_ok` judges itself).
SELECT_OFFERS_SQL = """
    SELECT o.product_key, o.sku_key, o.currency,
           CAST(coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price) AS TEXT) AS price
      FROM catalog_offers o
     WHERE o.product_key IN ({keys})
       AND o.suppression_reason IS NULL AND o.suppressed_at IS NULL
       AND coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price) IS NOT NULL
       AND lower(coalesce(o.availability, 'unknown')) NOT IN ('out_of_stock', 'sold_out', 'unavailable')
"""

# Idempotent on (product_key, sku_key). An older reading never overwrites a newer one (two runs,
# or a re-run of an old report). updated_at is bound, never left to the INSERT-only default.
UPSERT_PROOF_SQL = f"""
    INSERT INTO {TABLE}
        (product_key, sku_key, shop_host, handle, shopify_product_id, variant_id, live_variant_count,
         available, live_price_minor, currency, source, checked_at, outcome, updated_at)
    VALUES
        (:product_key, :sku_key, :shop_host, :handle, :shopify_product_id, :variant_id, :live_variant_count,
         :available, :live_price_minor, :currency, :source, :checked_at, :outcome, :updated_at)
    ON CONFLICT (product_key, sku_key) DO UPDATE SET
        shop_host = excluded.shop_host,
        handle = excluded.handle,
        shopify_product_id = excluded.shopify_product_id,
        variant_id = excluded.variant_id,
        live_variant_count = excluded.live_variant_count,
        available = excluded.available,
        live_price_minor = excluded.live_price_minor,
        currency = excluded.currency,
        source = excluded.source,
        checked_at = excluded.checked_at,
        outcome = excluded.outcome,
        updated_at = excluded.updated_at
     WHERE {TABLE}.checked_at <= excluded.checked_at
    RETURNING product_key
"""

_OFFER_KEY_CHUNK = 200


async def select_targets(db: Any, domain: str, *, limit: int, after: Optional[str]) -> List[Dict[str, Any]]:
    values: Dict[str, Any] = {
        "source_system": ENRICHMENT_SOURCE_SYSTEM, "domain": domain, "www_domain": "www." + domain,
        "limit": max(1, int(limit)),
    }
    cursor_clause = ""
    if after:
        cursor_clause = "AND product_key > :after"
        values["after"] = after
    rows = await db.fetch_all(SELECT_TARGETS_SQL.format(cursor_clause=cursor_clause), values)
    return [dict(r) for r in rows or []]


async def select_offers(db: Any, product_keys: Sequence[str]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    keys = list(dict.fromkeys(product_keys))
    for start in range(0, len(keys), _OFFER_KEY_CHUNK):
        chunk = keys[start:start + _OFFER_KEY_CHUNK]
        params = {f"k{i}": key for i, key in enumerate(chunk)}
        sql = SELECT_OFFERS_SQL.format(keys=", ".join(f":k{i}" for i in range(len(chunk))))
        for r in await db.fetch_all(sql, params) or []:
            row = dict(r)
            out.setdefault((row["product_key"], row["sku_key"]), []).append(
                {"currency": row["currency"], "price": row["price"]})
    return out


async def upsert_proof(db: Any, row: ProofRow, *, written_at: datetime) -> bool:
    """True when the row was written; False when a newer proof already holds the key."""
    if row.source not in PROOF_SOURCES or row.outcome not in WRITTEN_OUTCOMES:
        raise ValueError(f"refusing to write source={row.source!r} outcome={row.outcome!r}")
    return await db.fetch_val(UPSERT_PROOF_SQL, row.as_params(written_at)) is not None


# ── the run ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DomainPlan:
    domain: str
    market: str
    market_currency: str


def plan_domains(domains: Sequence[str], merchants_path: Optional[str] = None) -> List[DomainPlan]:
    """Each `--domain` must be on the Tier B list under exactly one market with a known currency."""
    if not domains:
        raise MerchantListError("at least one --domain is required; there is no default population")
    merchants = select_merchants(load_merchants(merchants_path), list(domains))
    plans: List[DomainPlan] = []
    for domain in dict.fromkeys(normalize_domain(d) for d in domains):
        rows = [m for m in merchants if m.domain == domain]
        markets = sorted({m.market for m in rows})
        if len(markets) != 1:
            raise MerchantListError(f"{domain}: expected exactly one market on the Tier B list, got {markets}")
        currency = _MARKET_CURRENCY.get(markets[0])
        if currency is None:
            raise MerchantListError(f"{domain}: market {markets[0]} has no currency in the purchase lane's map")
        plans.append(DomainPlan(domain=domain, market=markets[0], market_currency=currency))
    return plans


class _Abort(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def run_domain(db: Any, client: Any, plan: DomainPlan, *, apply: bool, source_mode: str, limit: int,
                     after: Optional[str], pacer: Pacer, block_limit: int, block_state: Dict[str, int],
                     now: Callable[[], datetime] = _utcnow) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "market": plan.market, "market_currency": plan.market_currency, "products": 0, "skus": 0,
        "outcomes": Counter(), "skipped": Counter(), "fetches": Counter(), "currency_read": Counter(),
        "currency_problems": Counter(), "written": 0, "write_older_than_stored": 0,
        "price_check": Counter(), "price_drift_samples": [], "listing": None, "source_mode": None,
        "next_cursor": None, "aborted_on_block": False,
    }
    rows = await select_targets(db, plan.domain, limit=limit, after=after)
    report["products"] = len({r["product_key"] for r in rows})
    if rows:
        report["next_cursor"] = rows[-1]["product_key"]

    # Targets, grouped by the host each one is requested from and the handle it lives under.
    by_host: Dict[str, Dict[str, List[SkuTarget]]] = {}
    for row in rows:
        if row.get("sku_key") is None:
            report["skipped"]["skip_no_live_sku"] += 1
            continue
        report["skus"] += 1
        target, skip = target_for_row(row, plan.domain)
        if target is None:
            report["skipped"][skip] += 1
            continue
        by_host.setdefault(target.shop_host, {}).setdefault(target.handle, []).append(target)

    def on_fetch(outcome: str) -> bool:
        report["fetches"][outcome] += 1
        if is_block(outcome):
            block_state["consecutive"] += 1
            if block_state["consecutive"] >= block_limit:
                return False
        elif outcome != "not_json":
            # `not_json` is neither a block nor a clean answer (a challenge page or a soft-404):
            # it must not reset the streak (scripts/backfill_shopify_variant_ids.py, measured).
            block_state["consecutive"] = 0
        return True

    decided: List[ProofRow] = []

    def record(evidence: HandleEvidence, targets: List[SkuTarget]) -> None:
        if evidence.transient is not None:
            report["skipped"][f"{SKIP_TRANSIENT}:{evidence.transient}"] += len(targets)
            return
        if evidence.currency is not None:
            report["currency_read"][evidence.currency] += 1
        elif evidence.currency_problem is not None:
            report["currency_problems"][evidence.currency_problem] += 1
        for target in targets:
            decided.append(decide_proof(target, evidence, market_currency=plan.market_currency))

    async def js(host: str, handle: str, targets: List[SkuTarget]) -> None:
        evidence, fetch_outcome = await read_handle_js(client, host, handle, plan.market, pacer=pacer, now=now)
        if not on_fetch(fetch_outcome):
            raise _Abort()
        record(evidence, targets)

    try:
        for host, handles in by_host.items():
            mode = source_mode
            if mode == "auto":
                estimate = await estimate_listing_requests(client, host, pacer=pacer, now=now)
                if not on_fetch("ok" if estimate is not None else "meta_unreadable"):
                    raise _Abort()
                mode = SOURCE_PRODUCTS_JSON if estimate is not None and estimate < len(handles) else SOURCE_PRODUCTS_JS
            report["source_mode"] = mode
            if mode == SOURCE_PRODUCTS_JS:
                for handle, targets in handles.items():
                    await js(host, handle, targets)
                continue
            listing = await read_listing(client, host, plan.market, pacer=pacer, now=now, on_fetch=on_fetch)
            report["listing"] = {"pages": listing.pages, "complete": listing.complete, "stop": listing.stop,
                                 "products_seen": len(listing.by_handle), "duplicate_handles": len(listing.duplicates)}
            if listing.stop == "aborted_on_block":
                raise _Abort()
            for handle, targets in handles.items():
                if handle in listing.by_handle:
                    record(listing.by_handle[handle], targets)
                elif listing.host_redirected:
                    record(HandleEvidence(requested_handle=handle, source=SOURCE_PRODUCTS_JSON,
                                          checked_at=now(), outcome=HOST_REDIRECTED), targets)
                elif listing.complete:
                    await js(host, handle, targets)  # absent from a complete listing, or listed twice
                else:
                    report["skipped"][SKIP_LISTING_INCOMPLETE] += len(targets)
    except _Abort:
        report["aborted_on_block"] = True
        report["next_cursor"] = None

    for proof in decided:
        report["outcomes"][proof.outcome] += 1

    # Drift: every 'ok' proof against the catalog offers the purchase lane would price from.
    ok_proofs = [p for p in decided if p.outcome == OUTCOME_OK]
    offers = await select_offers(db, [p.product_key for p in ok_proofs]) if ok_proofs else {}
    for proof in ok_proofs:
        sku_offers = offers.get((proof.product_key, proof.sku_key), [])
        verdict = enrichment_offer_price_ok(sku_offers, {"currency": proof.currency,
                                                         "live_price_minor": proof.live_price_minor},
                                            plan.market_currency)
        report["price_check"][verdict[2]] += 1
        if not verdict[0] and len(report["price_drift_samples"]) < 25:
            report["price_drift_samples"].append({
                "sku_key": proof.sku_key, "reason": verdict[2], "live_price_minor": proof.live_price_minor,
                "currency": proof.currency, "offers": [f"{o['price']} {o['currency']}" for o in sku_offers[:4]],
            })

    if apply and decided:
        written_at = now()
        for proof in decided:
            if await upsert_proof(db, proof, written_at=written_at):
                report["written"] += 1
            else:
                report["write_older_than_stored"] += 1

    for key in ("outcomes", "skipped", "fetches", "currency_read", "currency_problems", "price_check"):
        report[key] = dict(report[key])
    return report


async def run(db: Any, client: Any, plans: Sequence[DomainPlan], *, apply: bool, source_mode: str = "auto",
              limit: int = DEFAULT_LIMIT, after: Optional[str] = None, pacer: Optional[Pacer] = None,
              block_limit: Optional[int] = None, now: Callable[[], datetime] = _utcnow) -> Dict[str, Any]:
    if source_mode not in ("auto", SOURCE_PRODUCTS_JSON, SOURCE_PRODUCTS_JS):
        raise ValueError(f"unknown source mode {source_mode!r}")
    if after and len(plans) != 1:
        raise ValueError("--after resumes ONE domain's cursor")
    if apply:
        from db.enrichment_cart_variant_proofs import ensure_table

        if not await ensure_table():
            raise RuntimeError(f"could not ensure {TABLE}")
    pacer = pacer or Pacer(request_gap_s())
    block_limit = block_limit or abort_after_blocks()
    block_state = {"consecutive": 0}
    summary: Dict[str, Any] = {"mode": "apply" if apply else "dry_run", "source": source_mode,
                               "domains": {}, "aborted_on_block": False}
    for plan in plans:
        result = await run_domain(db, client, plan, apply=apply, source_mode=source_mode, limit=limit,
                                  after=after, pacer=pacer, block_limit=block_limit,
                                  block_state=block_state, now=now)
        summary["domains"][plan.domain] = result
        if result["aborted_on_block"]:
            summary["aborted_on_block"] = True
            break
    summary["requests"] = pacer.requests
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", action="append", default=[], metavar="HOST",
                        help="a storefront on config/tierb_cart_link_merchants.json (repeatable; required)")
    parser.add_argument("--apply", action="store_true", help="write the proofs; omit for a dry run")
    parser.add_argument("--source", choices=("auto", SOURCE_PRODUCTS_JSON, SOURCE_PRODUCTS_JS), default="auto")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="products per domain")
    parser.add_argument("--after", default=None, help="resume ONE domain past this product_key (next_cursor)")
    args = parser.parse_args(argv)
    try:
        plans = plan_domains(args.domain)
        if args.after and len(plans) != 1:
            raise MerchantListError("--after resumes ONE domain's cursor")
    except (MerchantListError, ValueError) as exc:
        print(f"PROOF_ERROR {exc}", file=sys.stderr, flush=True)
        return 2

    async def _main() -> Dict[str, Any]:
        from db.database import database

        await database.connect()
        try:
            async with httpx.AsyncClient() as client:
                return await run(database, client, plans, apply=args.apply, source_mode=args.source,
                                 limit=args.limit, after=args.after)
        finally:
            await database.disconnect()

    summary = asyncio.run(_main())
    # A text prefix keeps the line in textPayload (a bare JSON line lands in jsonPayload and reads
    # blank through scripts/ops/run_oneoff_job.sh).
    print("PROOF_REPORT " + json.dumps(summary, sort_keys=True, default=str), flush=True)
    return 1 if summary.get("aborted_on_block") else 0


if __name__ == "__main__":
    raise SystemExit(main())
