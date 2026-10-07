"""Write storefront cart proofs for ENRICHMENT catalog rows (option 2, PR B). Dry-run by default.

    python -m jobs.enrichment_cart_variant_proof --on-crawl-egress --domain tartecosmetics.com          # dry run
    python -m jobs.enrichment_cart_variant_proof --on-crawl-egress --domain tartecosmetics.com --apply  # write

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
  * shop_host              the host this job REQUESTED: the canonical_url host. A redirect to a host
                           that fails `_same_storefront_host` against it, or to anything but https,
                           is the non-ok outcome `host_redirected` (maccosmetics.com ->
                           www.maccosmetics.com is the same storefront; anything else is not), and
                           that other host is never requested.
  * handle                 Shopify's own `handle` from the response, verbatim. A response whose
                           handle is not the one the sku expects (`sku_payload.source_handle`,
                           else the canonical_url's) is `handle_mismatch`, never re-keyed.
  * variant_id             taken from the response. 'ok' only when the sku's own numeric id
                           (`source_variant_id`, strictly) is among that handle's variants; for
                           the `::canonical` placeholder, only when the handle has exactly ONE
                           variant, the catalog has NO `::v:` sku for the product at all
                           (suppressed ones counted, the carry-over from #2460's review), and the
                           live product's title is the catalog's (see PRODUCT IDENTITY).
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
  * variant_title          (migration 249) the live variant's own title ("NC50 / 1 fl oz"),
                           cleaned by THE cart-link title rule
                           (`services.shopify_variant_identity.clean_variant_title`), for display
                           only: the buyer never picks the shade on this lane, so the purchase says
                           which one it buys. NULL when no variant was identified.

PRODUCT IDENTITY (`product_changed`). A handle is not a product: a store can delete a product and
give its handle to a new one. A variant sku is bound to its product by the variant id (a new
product has new ids, so it reads `variant_gone`), but the placeholder names no variant. So:
  * once a proof row records a `shopify_product_id`, a live product under the same handle with a
    DIFFERENT id is `product_changed`, for every sku, and the row KEEPS the recorded id, so the
    refusal is sticky: it does not turn into an 'ok' for the new product on the next run. A
    person clears it (delete the proof row) after checking the catalog row. The recorded id is
    never erased by a later row that knows no product (a 404, a redirect, a malformed payload
    write NULL): the upsert COALESCEs it, so "A ok, B product_changed, 404, B again" still reads
    `product_changed`;
  * a placeholder is 'ok' only when the live product's title equals the catalog's
    `catalog_products.title` after the display-text rule, case-folded (`clean_display_text`:
    controls dropped, whitespace folded, NFC). Otherwise `product_changed`.

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
So the read currency is the `cart_currency` Set-Cookie of THE SAME FINAL response that carried the
prices: exactly one value, three upper-case letters. Every listing page carries its own. No request
ever carries a cookie, not even across a redirect: the job follows redirects itself, clears the
client's jar before every hop, and `main` builds the client on a jar whose policy accepts nothing
(MAC's 301 sets `_shopify_essential`; a hop that set `cart_currency` or `localization` must not
steer the final response). Every request asks `?country=<market>` so a multi-currency store
presents the market's currency where it can. Then:
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
renamed product answers there with its new handle -> `handle_mismatch`; a deleted one 404s or
410s -> `revoked_404`). An INCOMPLETE listing (a blocked page, the 100-page cap, a repeated page)
proves nothing about the handles it did not reach: they are reported `listing_incomplete` and
nothing is written for them. Exhaustion is an EMPTY page, never a short one (bluemercury serves
249-product pages mid-catalogue; services/curated_brand_feed.py measured it).

PARENT STUBS (`parent_stub`). MAC's storefront keeps a parent product per shade family whose ONE
variant restates the product title (option "Title" = "Studio Fix Fluid SPF 15 ...", sku
`P2000_120613`, no image, no barcode) while each shade is its own product with real options, images
and a barcode. Buying the parent buys that stub. Spot-checked 2026-09-29: the known family parent,
and two canonical-only catalog rows (Fix+, Connect In Colour palette: Rose Lens), all three carry that
signature, while the NC50 shade does not. Shopify's own default single variant is "Default Title",
so "Default Title" is NOT the signal (it is every ordinary one-variant product, tarte's brushes
included). A handle is a parent stub, and every sku on it `parent_stub`, when its SOLE variant
  * has a title equal to the product title (case-folded), store-independent; or
  * carries a MAC product-code sku (`P2000_...`) on a product with no image at all (the second
    signal the parents share, for a parent whose variant title were ever renamed).
It fails closed: an ordinary product that happens to name its lone variant after itself is refused
too, and shows up in the report by count.

WHAT IS NOT WRITTEN. A transient read (429/403/5xx/timeout/a challenge page served as 200, a path
robots.txt disallows, a request pacing held back, a redirect without a Location) is not evidence
of anything: nothing is written for those skus, a prior
proof keeps its checked_at and ages out after the verifier's 72h. Neither is a row this job cannot
name a proof FOR (an unusable canonical_url, a canonical host that is not the requested domain, an
unreadable source_handle). Everything else, every definitive refusal included, IS written, so a
proof that stopped being true is revoked by the same run that found out.

WHERE AND HOW IT RUNS. Only on the crawl subnet (`SUBNET=pivota-crawl`, NAT 34.82.199.35), never
the default NAT whose address payment partners allowlist. Nothing inside a Cloud Run container can
see which subnet it egresses by, so the operator (or the job definition) must say so:
`--on-crawl-egress` is REQUIRED for every run, dry runs included (a dry run fetches exactly as
hard; `--apply` gates the write, not the crawl), and without it the job exits 2 before any request.
Only for domains on Pivota's Tier B cart-link list (config/tierb_cart_link_merchants.json via
services.tierb_cart_link_merchants), which also gives each domain its market. Pacing:
  * request STARTS (redirect hops included) at least `ENRICHMENT_PROOF_REQUEST_GAP_S` apart
    (default 3.0 s, floor 1.5 s), one domain at a time;
  * every request, hop included, also goes through `services.crawl_politeness.before_request`, the
    crawl lane's owner of robots.txt (a disallowed path is skipped, a `Crawl-delay` is honoured, one
    over its cap skips the HOST for the rest of the run) and of the backoff that `note_response`
    arms from a 429/503's `Retry-After`. The wait is BOUNDED by `ENRICHMENT_PROOF_MAX_POLITE_WAIT_S`
    (default 60 s): a host held longer is not asked (`crawl_paced`, nothing written) rather than
    stalling the run. crawl_politeness keys its state by exact hostname, so each answer is reported
    for BOTH the host requested and the storefront's requested host when a redirect moved to its
    `www.` twin (one storefront, one backoff), while each request is GATED on the URL actually
    requested only -- so it takes one shared Shopify-edge slot, not two -- after the run's own pacer;
  * redirects (301/302/303/307/308, relative Locations resolved) are followed by hand, at most
    5 hops; a 3xx with no Location is a transient `redirect_without_location`, never a 404;
  * `ENRICHMENT_PROOF_ABORT_AFTER_BLOCKS` (default 5) consecutive block-shaped answers (429, 403,
    5xx, a transport error, on any endpoint /meta.json included) abort the whole run, since the
    2026-08-21 block was IP-level and cross-domain. A challenge page neither aborts nor resets.

SCHEDULED ONLY THROUGH ONE DARK-BY-DEFAULT WRAPPER. Nothing here is registered with
services/audit_scheduler, and this module has no default domain list. Its one scheduled caller is
jobs/reap_cart_proof_refresh.py (`enrichment` lane: a pinned list of five stores, `run_domain` 250
products at a time with one shared pacer and block streak, a budget, a stored cursor per store; it
calls this module's `ensure_table` itself on apply, as `run()` does), provisioned by the operator-run
infra/gcp/setup_reap_cart_proof_jobs.sh as the Cloud Run Job `reap-cart-proof-enrichment`, dry-run
and paused unless --enable. A test pins that no
other infra file, workflow or scheduler names this module or that wrapper.

EXIT CODES: 0 done; 1 aborted on a block; 2 bad arguments, a domain not on the Tier B list, or
no `--on-crawl-egress`; 3 crashed (an unexpected exception; the report line is not printed).
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
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from db.enrichment_cart_variant_proofs import OUTCOME_OK, PROOF_SOURCES, TABLE  # noqa: E402
from db.reap_agentic_ledger import amount_minor_or_none  # noqa: E402
# The map the purchase lane prices with. One map, so the proof's currency and the lane's agree.
from routes.agent_commerce_reap import _MARKET_CURRENCY  # noqa: E402
from services import crawl_politeness, shopify_edge_pacer  # noqa: E402
from services.curated_brand_feed import _same_storefront_host  # noqa: E402
from services.reap_enrichment_cart_proof import (  # noqa: E402
    ENRICHMENT_SOURCE_SYSTEM,
    PLACEHOLDER_SUFFIX,
    _expected_handle,
    _sku_payload,
    _sku_variant,
    enrichment_offer_price_ok,
    storefront_page,
)
from services.shopify_presentment import (  # noqa: E402
    CURRENCY_NOT_MARKET,
    no_cookie_client,
    presentment_currency,
    products_js_price_minor,
)
from services.shopify_variant_identity import MAX_VARIANTS, clean_variant_title  # noqa: E402
from services.text_normalization import clean_display_text  # noqa: E402
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
PRODUCT_CHANGED = "product_changed"
PAYLOAD_MALFORMED = "payload_malformed"
VARIANT_COUNT_UNVERIFIABLE = "variant_count_unverifiable"
PARENT_STUB = "parent_stub"
PLACEHOLDER_HAS_VARIANT_SKUS = "placeholder_has_variant_skus"
PLACEHOLDER_MULTI_VARIANT = "placeholder_multi_variant"
SKU_VARIANT_UNVERIFIED = "sku_variant_unverified"
VARIANT_GONE = "variant_gone"
CURRENCY_UNVERIFIED = "currency_unverified"
PRICE_UNREADABLE = "price_unreadable"
WRITTEN_OUTCOMES = frozenset({
    OUTCOME_OK, REVOKED_404, HOST_REDIRECTED, HANDLE_MISMATCH, PRODUCT_CHANGED, PAYLOAD_MALFORMED,
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
MAX_REDIRECTS = 5
#: The longest this job waits for crawl_politeness to give a host a slot (a Retry-After backoff or a
#: Crawl-delay). Longer than this, the request is not sent: `crawl_paced`, no evidence, nothing
#: written. Bounded (never crawl_politeness's `max_wait=0`, "forever"), so one host's long
#: Retry-After cannot stall the run past its task timeout.
DEFAULT_MAX_POLITE_WAIT_S = 60.0
PER_PAGE = 250
#: Shopify serves /products.json pages 1..100 only (services/curated_brand_feed.SHOPIFY_MAX_PAGES).
MAX_LISTING_PAGES = 100
BLOCK_OUTCOMES = frozenset({"rate_limited", "http_403", "http_500", "http_502", "http_503",
                            "http_504", "http_520", "http_521", "http_522", "http_524"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: Outcomes of a request that was NOT sent (robots, a pacing refusal): neither a block nor an answer,
#: so they neither count toward nor reset the block streak.
NEUTRAL_OUTCOMES = frozenset({"not_json", "robots_disallowed", "crawl_paced", "crawl_delay_too_long"})
DEFAULT_LIMIT = 2000

#: MAC's product-code sku prefix on its parent stubs (P2000_120613); real shade skus are SRMX11.
_MAC_PARENT_SKU_PREFIX = "P2000_"

EXIT_OK = 0
EXIT_ABORTED_ON_BLOCK = 1
EXIT_BAD_ARGS = 2
EXIT_CRASHED = 3


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


def max_polite_wait_s(environ: Optional[Mapping[str, str]] = None) -> float:
    """Positive and finite, always: 0 would mean "wait forever" to crawl_politeness."""
    env = os.environ if environ is None else environ
    try:
        value = float(env.get("ENRICHMENT_PROOF_MAX_POLITE_WAIT_S") or DEFAULT_MAX_POLITE_WAIT_S)
    except ValueError:
        value = DEFAULT_MAX_POLITE_WAIT_S
    if not math.isfinite(value) or value <= 0:
        value = DEFAULT_MAX_POLITE_WAIT_S
    return value


def is_block(outcome: str) -> bool:
    return outcome in BLOCK_OUTCOMES or outcome.startswith("error:")


# `no_cookie_client` and `presentment_currency` (THE CURRENCY RULE's two halves) live in
# services/shopify_presentment.py, shared with the mirror backfill; imported above.


# ── pure: one storefront product, parsed strictly ───────────────────────────────────────────────


@dataclass(frozen=True)
class LiveVariant:
    variant_id: str
    available: bool
    price: Any  # raw: an int (`.js`, x100) or a major-unit string (/products.json)
    title: Optional[str]
    sku: Optional[str] = None


@dataclass(frozen=True)
class LiveProduct:
    handle: str
    product_id: str
    title: str
    variants: Tuple[LiveVariant, ...]
    #: How many images the product has; None when the response carries no `images` list.
    image_count: Optional[int] = None

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
        sku = raw.get("sku")
        variants.append(LiveVariant(variant_id=variant_id, available=available, price=raw.get("price"),
                                    title=title if isinstance(title, str) else None,
                                    sku=sku if isinstance(sku, str) else None))
    title = product.get("title")
    images = product.get("images")
    return LiveProduct(handle=handle, product_id=product_id, title=title if isinstance(title, str) else "",
                       variants=tuple(variants),
                       image_count=len(images) if isinstance(images, list) else None), None


def is_parent_stub(live: LiveProduct) -> bool:
    """A MAC-style family parent: the SOLE variant restates the product title, or carries MAC's
    product-code sku on a product with no image. Shopify's own default single variant is
    'Default Title', which is NOT this."""
    if live.live_variant_count != 1:
        return False
    variant = live.variants[0]
    product_title = live.title.strip().casefold()
    variant_title = (variant.title or "").strip().casefold()
    if product_title and variant_title == product_title:
        return True
    return live.image_count == 0 and (variant.sku or "").startswith(_MAC_PARENT_SKU_PREFIX)


def title_key(value: Any) -> Optional[str]:
    """A product title for comparison: the display-text rule (controls dropped, whitespace folded,
    NFC), case-folded, NFC again (casefold can denormalise). None for no usable title."""
    cleaned = clean_display_text(value, max_chars=10_000)
    if cleaned is None:
        return None
    return unicodedata.normalize("NFC", cleaned.casefold())


def live_price_minor(price: Any, currency: str, source: str) -> Optional[int]:
    """The variant's price in ISO minor units of `currency`, or None (refused, never rounded)."""
    if source == SOURCE_PRODUCTS_JS:
        return products_js_price_minor(price, currency)
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
    variant_title: Optional[str] = None

    def as_params(self, written_at: datetime) -> Dict[str, Any]:
        return {
            "product_key": self.product_key, "sku_key": self.sku_key, "shop_host": self.shop_host,
            "handle": self.handle, "shopify_product_id": self.shopify_product_id,
            "variant_id": self.variant_id, "live_variant_count": self.live_variant_count,
            "available": self.available, "live_price_minor": self.live_price_minor,
            "currency": self.currency, "source": self.source, "checked_at": self.checked_at,
            "outcome": self.outcome, "updated_at": written_at, "variant_title": self.variant_title,
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
    #: `catalog_products.title`, for the placeholder's product-identity check.
    catalog_title: Optional[str] = None
    #: The `shopify_product_id` an earlier proof of this sku recorded, if any.
    prior_product_id: Optional[str] = None


def target_for_row(row: Mapping[str, Any], domain: str,
                   prior_product_ids: Optional[Mapping[Tuple[str, str], str]] = None
                   ) -> Tuple[Optional[SkuTarget], Optional[str]]:
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
    title = row.get("catalog_title")
    return SkuTarget(
        product_key=product_key, sku_key=sku_key, shop_host=host, handle=handle,
        placeholder=sku_key == product_key + PLACEHOLDER_SUFFIX,
        source_variant_id=row.get("source_variant_id"),
        catalog_variant_sku_count=int(count) if count is not None else 0,
        catalog_title=title if isinstance(title, str) else None,
        prior_product_id=(prior_product_ids or {}).get((product_key, sku_key)),
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
    if target.prior_product_id is not None and target.prior_product_id != live.product_id:
        # Another product holds this handle now. Keep the recorded id: sticky until a person looks.
        row.outcome = PRODUCT_CHANGED
        row.shopify_product_id = target.prior_product_id
        return row
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
        catalog = title_key(target.catalog_title)
        if catalog is None or catalog != title_key(live.title):
            row.outcome = PRODUCT_CHANGED
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
    row.variant_title = clean_variant_title(variant.title)
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


def _clear_cookies(client: Any) -> None:
    try:
        client.cookies.clear()
    except AttributeError:
        pass


def _politeness_urls(url: str, requested_host: str) -> List[str]:
    """The URLs crawl_politeness is told about for one request: the one actually requested, and the
    same path on the REQUESTED storefront host when a redirect moved to its `www.` twin. The two
    are one storefront (`_same_storefront_host`) behind one rate limiter, so a 429 from
    www.brand.com must hold the next apex request too, and crawl_politeness keys its state by
    exact hostname. Both hosts are gated and both are told about every answer."""
    parts = urlsplit(url)
    urls = [url]
    if (parts.hostname or "") != requested_host:
        urls.append(parts._replace(netloc=requested_host).geturl())
    return urls


async def fetch_json(client: Any, url: str, *, requested_host: str, pacer: Pacer,
                     now: Callable[[], datetime]) -> Fetched:
    """GET `url`, following same-storefront https redirects BY HAND: every hop is paced, gated by
    crawl_politeness (robots, Crawl-delay, Retry-After backoff, keyed on the storefront) and sent
    with no cookie. Only the FINAL response's Set-Cookie is kept. Never raises: every failure is a
    classified outcome."""
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        polite_urls = _politeness_urls(current, requested_host)
        # Every request here is a Shopify storefront endpoint (/products.json, /products/<h>.js,
        # /meta.json) on a Tier B Shopify store, so the shared Shopify-edge budget applies from the
        # FIRST request (services/shopify_edge_pacer.py; a no-op while its flag is off).
        shopify_edge_pacer.mark_shopify_host(current)
        # The run's own spacing FIRST, then the gate: a shared Shopify-edge slot reserved before a
        # 3 s pacer sleep would be spent late or expire. And the gate is asked for the URL actually
        # requested ONLY, so a request takes ONE shared slot even on a www hop; the storefront's twin
        # host still shares its backoff, because every answer is noted for both (below).
        await pacer.wait()
        try:
            await crawl_politeness.before_request(polite_urls[0], user_agent=USER_AGENT,
                                                  max_wait=max_polite_wait_s())
        except crawl_politeness.RobotsDisallowed:
            return Fetched(outcome="robots_disallowed")
        except crawl_politeness.CrawlDelayTooLong:
            # The host asks for a rate we will not sustain: the caller skips the whole host.
            return Fetched(outcome="crawl_delay_too_long")
        except crawl_politeness.CrawlPaced:
            # Held longer than we wait (a long Retry-After): not sent, no evidence.
            return Fetched(outcome="crawl_paced")
        _clear_cookies(client)
        try:
            resp = await client.get(current, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                    timeout=REQUEST_TIMEOUT_S, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - classified, not swallowed
            return Fetched(outcome=f"error:{type(exc).__name__}")
        for index, polite_url in enumerate(polite_urls):
            # Headers for the URL actually requested only: they feed the IP-throttle breaker, which
            # counts DISTINCT hosts, and the storefront's www twin is the same store, not a second host.
            crawl_politeness.note_response(polite_url, resp.status_code,
                                           retry_after=resp.headers.get("retry-after"),
                                           headers=resp.headers if index == 0 else None)
        shopify_edge_pacer.learn_from_response(current, resp.headers)
        checked_at = now()
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                # A redirect that names nowhere says nothing about the product: never a 404.
                return Fetched(outcome="redirect_without_location")
            nxt = urlsplit(urljoin(current, location))
            if nxt.scheme != "https" or not _same_storefront_host(requested_host, nxt.hostname or ""):
                # Never requested: the redirect itself is the answer.
                return Fetched(outcome=HOST_REDIRECTED, checked_at=checked_at)
            current = nxt.geturl()
            continue
        # No final-host check here: the first URL is ours (https, the requested host), and every
        # hop to another one was refused above before it was requested.
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
    return Fetched(outcome="too_many_redirects")


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
    redirected_at: Optional[datetime] = None


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
            listing.redirected_at = fetched.checked_at
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
        # THIS page's own cookie: a later page may be presented in another currency.
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
                                    now: Callable[[], datetime]) -> Tuple[Optional[int], str]:
    """`(pages, fetch_outcome)`: pages /products.json would take (plus its empty lookahead page),
    from /meta.json; None when that could not be read."""
    fetched = await fetch_json(client, meta_json_url(host), requested_host=host, pacer=pacer, now=now)
    if fetched.outcome != "ok":
        return None, fetched.outcome
    count = fetched.payload.get("published_products_count") if isinstance(fetched.payload, Mapping) else None
    if type(count) is not int or count < 0:
        return None, "ok"
    return math.ceil(count / PER_PAGE) + 1, "ok"


# ── SQL ─────────────────────────────────────────────────────────────────────────────────────────
# Both dialects (the unit tests run it on SQLite, tests/*_postgres.py on Postgres). The variant
# count is EVERY `::v:` sku of the product, suppressed ones included: a live-only count of 0 would
# let the placeholder stand for a MAC family whose shades are merely suppressed. `substr` rather
# than LIKE: a product_key may hold `_`, which LIKE reads as a wildcard.
SELECT_TARGETS_SQL = """
    WITH p AS (
        SELECT product_key, source_domain, canonical_url, title
          FROM catalog_products
         WHERE source_system = :source_system
           AND suppressed_at IS NULL AND suppression_reason IS NULL
           AND lower(source_domain) IN (:domain, :www_domain)
           {cursor_clause}
         ORDER BY product_key
         LIMIT :limit
    )
    SELECT p.product_key, p.source_domain, p.canonical_url, p.title AS catalog_title,
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

# What earlier proofs recorded, for the product-identity check.
SELECT_PRIOR_PRODUCT_IDS_SQL = f"""
    SELECT product_key, sku_key, shopify_product_id
      FROM {TABLE}
     WHERE product_key IN ({{keys}})
       AND shopify_product_id IS NOT NULL
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
# or a re-run of an old report); the same instant may (a re-run of the same reading). updated_at is
# bound, never left to the INSERT-only default. A recorded shopify_product_id survives every later
# row that knows none (see PRODUCT IDENTITY in the module docstring).
UPSERT_PROOF_SQL = f"""
    INSERT INTO {TABLE}
        (product_key, sku_key, shop_host, handle, shopify_product_id, variant_id, live_variant_count,
         available, live_price_minor, currency, source, checked_at, outcome, updated_at, variant_title)
    VALUES
        (:product_key, :sku_key, :shop_host, :handle, :shopify_product_id, :variant_id, :live_variant_count,
         :available, :live_price_minor, :currency, :source, :checked_at, :outcome, :updated_at, :variant_title)
    ON CONFLICT (product_key, sku_key) DO UPDATE SET
        shop_host = excluded.shop_host,
        handle = excluded.handle,
        -- THE PRODUCT-IDENTITY ANCHOR IS NEVER ERASED. A handle-level refusal (a 404, a redirect, a
        -- malformed payload) knows no product and writes NULL; overwriting the stored id with it
        -- would let the NEXT run accept whatever product holds the handle then.
        shopify_product_id = COALESCE(excluded.shopify_product_id, {TABLE}.shopify_product_id),
        variant_id = excluded.variant_id,
        live_variant_count = excluded.live_variant_count,
        available = excluded.available,
        live_price_minor = excluded.live_price_minor,
        currency = excluded.currency,
        source = excluded.source,
        checked_at = excluded.checked_at,
        outcome = excluded.outcome,
        variant_title = excluded.variant_title,
        updated_at = excluded.updated_at
     WHERE {TABLE}.checked_at <= excluded.checked_at
    RETURNING product_key
"""

_KEY_CHUNK = 200


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


async def _fetch_by_keys(db: Any, template: str, product_keys: Sequence[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    keys = list(dict.fromkeys(product_keys))
    for start in range(0, len(keys), _KEY_CHUNK):
        chunk = keys[start:start + _KEY_CHUNK]
        params = {f"k{i}": key for i, key in enumerate(chunk)}
        sql = template.format(keys=", ".join(f":k{i}" for i in range(len(chunk))))
        out.extend(dict(r) for r in await db.fetch_all(sql, params) or [])
    return out


async def select_offers(db: Any, product_keys: Sequence[str]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in await _fetch_by_keys(db, SELECT_OFFERS_SQL, product_keys):
        out.setdefault((row["product_key"], row["sku_key"]), []).append(
            {"currency": row["currency"], "price": row["price"]})
    return out


async def select_prior_product_ids(db: Any, product_keys: Sequence[str], *,
                                   table_must_exist: bool) -> Dict[Tuple[str, str], str]:
    """(product_key, sku_key) -> the shopify_product_id an earlier proof recorded. A dry run on an
    environment that never wrote a proof has no table: that reads as no prior (and is reported)."""
    try:
        rows = await _fetch_by_keys(db, SELECT_PRIOR_PRODUCT_IDS_SQL, product_keys)
    except Exception:
        if table_must_exist:
            raise
        return {}
    return {(r["product_key"], r["sku_key"]): str(r["shopify_product_id"]) for r in rows}


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
                     now: Callable[[], datetime] = _utcnow,
                     should_stop: Optional[Callable[[], bool]] = None,
                     on_block: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """One domain's page. `should_stop`, when given, is asked after every fetch: True aborts the run
    exactly as a block streak does (jobs/reap_cart_proof_refresh.py passes its run-level breakers).
    `on_block`, when given, is told the outcome of every block-shaped answer (the caller's run-level
    block breaker)."""
    report: Dict[str, Any] = {
        "market": plan.market, "market_currency": plan.market_currency, "products": 0, "skus": 0,
        "outcomes": Counter(), "skipped": Counter(), "fetches": Counter(), "currency_read": Counter(),
        "currency_problems": Counter(), "written": 0, "write_older_than_stored": 0,
        "price_check": Counter(), "price_drift_samples": [], "hosts": {},
        "next_cursor": None, "exhausted": False, "aborted_on_block": False,
    }
    rows = await select_targets(db, plan.domain, limit=limit, after=after)
    product_keys = list(dict.fromkeys(r["product_key"] for r in rows))
    report["products"] = len(product_keys)
    # Fewer products than asked for: the domain is exhausted, and there is no cursor to resume.
    report["exhausted"] = len(product_keys) < max(1, int(limit))
    if product_keys and not report["exhausted"]:
        report["next_cursor"] = product_keys[-1]
    prior = await select_prior_product_ids(db, product_keys, table_must_exist=apply) if product_keys else {}

    # Targets, grouped by the host each one is requested from and the handle it lives under.
    by_host: Dict[str, Dict[str, List[SkuTarget]]] = {}
    for row in rows:
        if row.get("sku_key") is None:
            report["skipped"]["skip_no_live_sku"] += 1
            continue
        report["skus"] += 1
        target, skip = target_for_row(row, plan.domain, prior)
        if target is None:
            report["skipped"][skip] += 1
            continue
        by_host.setdefault(target.shop_host, {}).setdefault(target.handle, []).append(target)

    def on_fetch(outcome: str) -> bool:
        report["fetches"][outcome] += 1
        if is_block(outcome) and on_block is not None:
            on_block(outcome)
        if should_stop is not None and should_stop():
            return False
        if is_block(outcome):
            block_state["consecutive"] += 1
            if block_state["consecutive"] >= block_limit:
                return False
        elif outcome not in NEUTRAL_OUTCOMES:
            # `not_json` is neither a block nor a clean answer (a challenge page or a soft-404), and
            # a request robots or pacing kept us from sending is no answer at all: neither may
            # reset the streak (scripts/backfill_shopify_variant_ids.py, measured).
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

    skipped_hosts: Dict[str, str] = {}

    async def js(host: str, handle: str, targets: List[SkuTarget]) -> None:
        if host in skipped_hosts:
            # A host that asked for a Crawl-delay over the cap is not asked again this run.
            report["skipped"][f"{SKIP_TRANSIENT}:{skipped_hosts[host]}"] += len(targets)
            return
        evidence, fetch_outcome = await read_handle_js(client, host, handle, plan.market, pacer=pacer, now=now)
        if not on_fetch(fetch_outcome):
            raise _Abort()
        if fetch_outcome == "crawl_delay_too_long":
            skipped_hosts[host] = fetch_outcome
        record(evidence, targets)

    try:
        for host, handles in by_host.items():
            host_report: Dict[str, Any] = {"source_mode": None, "listing": None, "handles": len(handles)}
            report["hosts"][host] = host_report
            mode = source_mode
            if mode == "auto":
                estimate, meta_outcome = await estimate_listing_requests(client, host, pacer=pacer, now=now)
                # A 429/403 on /meta.json is a block like any other.
                if not on_fetch(meta_outcome):
                    raise _Abort()
                mode = SOURCE_PRODUCTS_JSON if estimate is not None and estimate < len(handles) else SOURCE_PRODUCTS_JS
            host_report["source_mode"] = mode
            if mode == SOURCE_PRODUCTS_JS:
                for handle, targets in handles.items():
                    await js(host, handle, targets)
                continue
            listing = await read_listing(client, host, plan.market, pacer=pacer, now=now, on_fetch=on_fetch)
            host_report["listing"] = {"pages": listing.pages, "complete": listing.complete, "stop": listing.stop,
                                      "products_seen": len(listing.by_handle),
                                      "duplicate_handles": len(listing.duplicates)}
            if listing.stop == "aborted_on_block":
                raise _Abort()
            for handle, targets in handles.items():
                if listing.host_redirected:
                    record(HandleEvidence(requested_handle=handle, source=SOURCE_PRODUCTS_JSON,
                                          checked_at=listing.redirected_at or now(), outcome=HOST_REDIRECTED),
                           targets)
                elif handle in listing.by_handle:
                    record(listing.by_handle[handle], targets)
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
    parser.add_argument("--on-crawl-egress", action="store_true",
                        help="REQUIRED: this process egresses by the crawl subnet (SUBNET=pivota-crawl), "
                             "never the payment NAT. Every run fetches, dry runs included.")
    parser.add_argument("--source", choices=("auto", SOURCE_PRODUCTS_JSON, SOURCE_PRODUCTS_JS), default="auto")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="products per domain")
    parser.add_argument("--after", default=None, help="resume ONE domain past this product_key (next_cursor)")
    args = parser.parse_args(argv)
    try:
        if not args.on_crawl_egress:
            raise MerchantListError(
                "refusing to crawl without --on-crawl-egress: run on SUBNET=pivota-crawl and say so")
        plans = plan_domains(args.domain)
        if args.after and len(plans) != 1:
            raise MerchantListError("--after resumes ONE domain's cursor")
    except (MerchantListError, ValueError) as exc:
        print(f"PROOF_ERROR {exc}", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS

    async def _main() -> Dict[str, Any]:
        from db.database import database

        await database.connect()
        try:
            async with no_cookie_client() as client:
                return await run(database, client, plans, apply=args.apply, source_mode=args.source,
                                 limit=args.limit, after=args.after)
        finally:
            await database.disconnect()

    try:
        summary = asyncio.run(_main())
    except Exception as exc:  # noqa: BLE001 - a crash is its own exit code, never "aborted"
        logger.exception("enrichment cart proof job crashed")
        print(f"PROOF_CRASH {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr, flush=True)
        return EXIT_CRASHED
    # A text prefix keeps the line in textPayload (a bare JSON line lands in jsonPayload and reads
    # blank through scripts/ops/run_oneoff_job.sh).
    print("PROOF_REPORT " + json.dumps(summary, sort_keys=True, default=str), flush=True)
    return EXIT_ABORTED_ON_BLOCK if summary.get("aborted_on_block") else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
