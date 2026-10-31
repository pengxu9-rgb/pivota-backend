"""The seed refresh's catalogue price lane: price a Shopify store's seeds off its paged `/products.json`.

WHY. The nightly refresh (`jobs/external_referral_refresh`) reads one product page per seed, about 1.3
origin requests a listing, and the crawl egress is held to 0.5 requests/s by the global pacer
(`services.crawl_global_pacer`; at 2.5-4/s the one NAT address was challenge-listed on 2026-10-16). On
2026-10-31 a run made 1,640 paced requests in 3,300 s, its workers waited 90% of the time, and it reached
1,022 of 26,678 active seeds. Most seeds sit on a few Shopify stores, and one `/products.json?limit=250`
page prices 250 products, every variant included (the destination sweep already pages it,
`services.external_seed_destination_liveness.read_brand_catalogue`).

WHAT THIS LANE CHANGES IS THE READ, NOT THE WRITE. For a host it can read, the batch hands
`routes.employee_products._refresh_external_seed_by_id` a `SeedCatalogueRead`: an `ExternalOfferSnapshot`
built from the catalogue's entry for the seed's handle, in the shape a page read builds (product price
and stock, `evidence.variants`, `evidence.variant_census`, `price_read`, `price_currency_source`), plus
the liveness observation the sweep records for a listed handle. Everything after the read is the page
read's code: the price-status rules, the currency and market guards, the variant reconcile, the
`extracted_at` and `last_crawled_at` stamps, the liveness writer and the projection.

THE RULES, each one a refusal that sends the seed back to its page read:
  * COPY AND IMAGES ARE NOT READ. The snapshot declares no description and no gallery, so the refresh
    rewrites neither (and leaves the stored snapshot's title, image and canonical alone).
  * THE CURRENCY IS NEVER DEFAULTED. `/products.json` names none, and the presentment currency follows
    the requester's geography (`services.shopify_presentment`). A host is priced only when its BASE
    currency (`/meta.json` `currency`) is also the currency it PRESENTED to us: every catalogue page's own
    `cart_currency` Set-Cookie names it (the same-response rule), or, when a page carries none,
    `/cart.js` `currency` does. Base equal to presentment leaves one currency the numbers can be in.
    Anything else naming a different currency refuses the host (`decide_host_currency`).
  * ONE STOREFRONT, ONE MARKET: only a `/products/<handle>` (or `/collections/<c>/products/<handle>`) URL
    on the host the catalogue was read from. A locale prefix (`/en-gb/products/...`) is a Markets
    subfolder priced in another currency, and keeps its page read.
  * THE PRODUCT PRICE IS THE PAGE'S: the URL's `?variant=` when it names one (absent from the catalogue:
    page read), else the first available variant, else the first -- og:price's
    `selected_or_first_available_variant`.
  * A host is read only when it pays: at least `catalogue_min_seeds()` candidate seeds this run, at most
    one catalogue page per wanted seed, and not when `/meta.json` says the catalogue needs more pages
    than that (`pages_worth_reading`).

Default OFF (`EXTERNAL_REFERRAL_REFRESH_CATALOGUE_ENABLED`). The dry run is
`scripts/ops/plan_shopify_catalogue_refresh.py`.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, unquote, urlparse

import httpx

from services import crawl_global_pacer, crawl_identity, crawl_politeness, shopify_edge_pacer
from services import external_seed_destination_liveness as liveness
from services.shopify_presentment import no_cookie_client, presentment_currency
from services.storefront_currency import parse_meta
from utils.availability_vocabulary import IN_STOCK, OUT_OF_STOCK

logger = logging.getLogger(__name__)

FLAG = "EXTERNAL_REFERRAL_REFRESH_CATALOGUE_ENABLED"
MIN_SEEDS_ENV = "EXTERNAL_REFERRAL_REFRESH_CATALOGUE_MIN_SEEDS"
MIN_SEEDS_DEFAULT = 3

#: `seed_data.snapshot.read_source` and `evidence.read_source` of a catalogue-priced refresh.
READ_SOURCE = "products_json"
EVIDENCE_PROVIDER = "shopify_products_json"
#: How a host's currency was proven (`evidence.price_currency_proof`).
PROOF_BASE_AND_PAGE_COOKIE = "base_and_page_cookie"
PROOF_BASE_AND_CART = "base_and_cart"

#: A host the lane may price from. Every other `HostCatalogue.status` is a refusal.
STATUS_OK = "ok"
STATUS_BASE_UNREAD = "base_currency_unread"
STATUS_NOT_WORTH_IT = "not_worth_it"
STATUS_CURRENCY_CONFLICT = "currency_conflict"
STATUS_CURRENCY_UNPROVEN = "currency_unproven"
STATUS_NONE_LISTED = "no_wanted_handle_listed"
STATUS_ERROR = "error"

#: Variants kept per product. The snapshot is never stored, so this is not the page lane's 50
#: (`MAX_VARIANTS`); a product listing more is `truncated` in the census.
CATALOGUE_MAX_VARIANTS = 250

_TRUTHY = {"1", "true", "yes", "on"}
_CATALOGUE_PATH = re.compile(r"^/(?:collections/[^/]+/)?products/([^/]+)/?$")
_CATALOGUE_PRICE = re.compile(r"^\d{1,9}(?:\.\d{1,2})?$")
_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


def catalogue_refresh_enabled() -> bool:
    return str(os.getenv(FLAG) or "").strip().lower() in _TRUTHY


def catalogue_min_seeds() -> int:
    """Candidate seeds a host needs this run before its catalogue is read. At least 1; garbage is the default."""
    raw = str(os.getenv(MIN_SEEDS_ENV) or "").strip()
    try:
        value = int(raw) if raw else MIN_SEEDS_DEFAULT
    except ValueError:
        return MIN_SEEDS_DEFAULT
    return max(1, value)


def user_agent() -> str:
    """The agent the page read uses (and robots.txt is asked for): this lane is the same reader."""
    from services.external_offers_service import DEFAULT_UA

    return crawl_identity.robots_user_agent(DEFAULT_UA)


# --------------------------------------------------------------------------- pure rules


def catalogue_handle(url: Any) -> Optional[str]:
    """The lower-cased handle of a URL the host's own catalogue prices, or None.

    Only `/products/<handle>` and `/collections/<c>/products/<handle>`: anything before them (a Markets
    locale subfolder such as `/en-gb/` or `/fr-ca/`) is another storefront, priced in another currency."""
    try:
        path = urlparse(str(url or "")).path or ""
    except ValueError:
        return None
    match = _CATALOGUE_PATH.match(path)
    if not match:
        return None
    return unquote(match.group(1)).strip().lower() or None


def url_variant_id(url: Any) -> Optional[str]:
    """The variant the URL selects: "" when it names none, its digits when it names one, None when its
    `?variant=` cannot be read as one variant (two values, or not digits)."""
    try:
        values = {v.strip() for k, v in parse_qsl(urlparse(str(url or "")).query, keep_blank_values=True)
                  if k == "variant"}
    except ValueError:
        return None
    if not values:
        return ""
    if len(values) != 1:
        return None
    (value,) = values
    return value if value.isascii() and value.isdigit() else None


def catalogue_price(raw: Any) -> Optional[float]:
    """A `/products.json` price ("28.00", always a string with a dot), or None. Nothing else is read:
    a number, a comma, a sign or a third decimal is refused rather than guessed."""
    if not isinstance(raw, str) or not _CATALOGUE_PRICE.match(raw.strip()):
        return None
    return float(raw.strip())


def page_currencies(page_set_cookies: Sequence[Sequence[str]]) -> List[Optional[str]]:
    """Per catalogue page, the currency that response was presented in (its own `cart_currency`), or None."""
    return [presentment_currency(list(headers or ()))[0] for headers in page_set_cookies]


def decide_host_currency(
    *, base: Optional[str], page_cookies: Sequence[Optional[str]], cart: Optional[str]
) -> Tuple[Optional[str], str]:
    """(currency, proof) when the host's base currency is the one it presented to us, else (None, refusal).

    `base` is `/meta.json`'s; `page_cookies` each catalogue page's own `cart_currency`; `cart` is `/cart.js`'s
    (None when not read). Every currency any of them names must be the same one, and the presentment proof
    is every page's cookie, or else `/cart.js`."""
    named = {c for c in (base, cart, *page_cookies) if c}
    if len(named) > 1:
        return None, STATUS_CURRENCY_CONFLICT
    if not base:
        return None, STATUS_BASE_UNREAD
    if page_cookies and all(page_cookies):
        return base, PROOF_BASE_AND_PAGE_COOKIE
    if cart:
        return base, PROOF_BASE_AND_CART
    return None, STATUS_CURRENCY_UNPROVEN


def pages_worth_reading(wanted: int, published_products_count: Any) -> Optional[int]:
    """How many catalogue pages the lane may read for `wanted` seeds, or None when it should not read.

    One page per wanted seed at most: a page that prices no candidate costs what a page read would. When
    `/meta.json` publishes the catalogue's size and it needs more pages than that, not one is read."""
    if wanted < 1:
        return None
    count = published_products_count
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        if math.ceil(count / liveness.PAGE_LIMIT) > wanted:
            return None
    return min(wanted, liveness.MAX_CATALOGUE_PAGES)


def _availability(raw: Any) -> str:
    if raw is True:
        return IN_STOCK
    if raw is False:
        return OUT_OF_STOCK
    return "unknown"


def _variant_title(raw: Any) -> Optional[str]:
    title = str(raw or "").strip()
    # Shopify's name for the one variant of a product without options: not a variant label.
    return None if not title or title == "Default Title" else title


def read_variants(product: Dict[str, Any], currency: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The product's variants as the page extractor emits them, and the census the reconcile reads.

    A catalogue lists every variant with its own price, so the census is COMPLETE and EXACT: `offers` is
    the number of variants, nothing is an aggregate. A price that would not read (`catalogue_price`)
    marks the census `inexact`, so a single stored variant never takes the product price off a list
    with an unread sibling."""
    raw = [v for v in (product.get("variants") or []) if isinstance(v, dict)]
    out: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    duplicate_ids: Set[str] = set()
    refused = 0
    for idx, v in enumerate(raw[:CATALOGUE_MAX_VARIANTS]):
        vid = str(v.get("id") or "").strip()
        amount = catalogue_price(v.get("price"))
        if amount is None:
            refused += 1
        if vid and vid in seen:
            duplicate_ids.add(vid)
            out[seen[vid]]["id_collided"] = True
            continue
        entry: Dict[str, Any] = {
            # A variant with no id is positional, which the reconcile never matches on.
            "variant_id": vid or f"offer_{idx + 1}",
            "title": _variant_title(v.get("title")),
            "price_amount": amount,
            "price_currency": currency,
            "availability": _availability(v.get("available")),
            "price_exact": amount is not None,
        }
        sku = str(v.get("sku") or "").strip()
        if sku:
            entry["sku"] = sku
        compare_at = catalogue_price(v.get("compare_at_price"))
        if compare_at is not None:
            entry["compare_at_amount"] = compare_at
        if vid:
            seen[vid] = len(out)
        out.append(entry)
    census = {
        "source": READ_SOURCE,
        "offers": len(raw),
        "exact_prices": sorted({round(e["price_amount"], 2) for e in out if e["price_amount"] is not None}),
        "availabilities": sorted({e["availability"] for e in out if e["availability"] != "unknown"}),
        "inexact": refused > 0,
        "aggregate": False,
        "duplicate_ids": len(duplicate_ids),
        "truncated": len(raw) > CATALOGUE_MAX_VARIANTS,
        "price_refused": refused,
        "selling_plans": 0,
        "folded_duplicates": 0,
        "plans_only": False,
        "orphan_plan_variants": [],
        "data_attr_skus": 0,
        "data_attr_exact_prices": [],
        "data_attr_availabilities": [],
        "data_attr_duplicate_ids": 0,
    }
    return out, census


def _selected(variants: List[Dict[str, Any]], selected_id: str) -> Optional[Dict[str, Any]]:
    if selected_id:
        return next((v for v in variants if v["variant_id"] == selected_id), None)
    return next((v for v in variants if v["availability"] == IN_STOCK), variants[0] if variants else None)


def catalogue_snapshot(
    product: Dict[str, Any],
    *,
    url: str,
    host: str,
    currency: str,
    proof: str,
    market: str = "",
    now: Optional[datetime] = None,
):
    """The `ExternalOfferSnapshot` a page read of `url` would hand the refresh, from its catalogue entry.

    None when the product lists no variant, or the URL selects a variant the product does not list.
    No title, image, description or gallery: content is not read here. No canonical: the page's own
    claim is not read either, so the refresh keeps the stored one."""
    from services.external_offers_service import ExternalOfferSnapshot

    selected_id = url_variant_id(url)
    if selected_id is None:
        return None
    variants, census = read_variants(product, currency)
    chosen = _selected(variants, selected_id)
    if chosen is None:
        return None
    raw_price = next(
        (v.get("price") for v in (product.get("variants") or [])
         if isinstance(v, dict) and str(v.get("id") or "").strip() == chosen["variant_id"]),
        None,
    )
    amount = chosen["price_amount"]
    census["product_price_exact"] = amount is not None
    if amount is not None:
        status = "parsed"
    elif raw_price in (None, ""):
        status = "empty"
    else:
        status = "catalogue_price_unread"
    stamp = now or datetime.now(timezone.utc)
    evidence = {
        "provider": EVIDENCE_PROVIDER,
        "read_source": READ_SOURCE,
        "fetchedAt": stamp.isoformat(),
        # What the attached lane's projection reads (`currency_read`): the host's currency was READ,
        # base and presentment agreeing, never a market's default.
        "price_currency_source": "page",
        "price_currency_proof": proof,
        "price_read": {"status": status, "source": READ_SOURCE, "raw": str(raw_price)[:64] if raw_price is not None else None,
                       "decimal_hint": None},
        "variants": variants,
        "variant_census": census,
    }
    return ExternalOfferSnapshot(
        snapshot_id=f"catalogue:{host}:{str(product.get('handle') or '').strip().lower()}",
        market=market,
        canonical_url=None,  # type: ignore[arg-type]  # not read; see the docstring
        domain=host,
        title=None,
        brand=None,
        image_url=None,
        price_amount=amount,
        price_currency=currency,
        availability=chosen["availability"],
        last_checked_at=stamp,
        evidence=evidence,
    )


# --------------------------------------------------------------------------- per host


@dataclass
class HostCatalogue:
    """One host's catalogue read for this run: priceable (`status == STATUS_OK`) or why not."""

    host: str
    status: str
    currency: Optional[str] = None
    proof: Optional[str] = None
    products: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    catalogue_status: Optional[str] = None
    pages_read: int = 0
    base_currency: Optional[str] = None
    cart_currency: Optional[str] = None
    page_cookie_currencies: List[Optional[str]] = field(default_factory=list)
    published_products_count: Optional[int] = None
    wanted: int = 0
    note: str = ""

    def summary(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "status": self.status,
            "currency": self.currency,
            "proof": self.proof,
            "catalogue_status": self.catalogue_status,
            "pages_read": self.pages_read,
            "base_currency": self.base_currency,
            "cart_currency": self.cart_currency,
            "page_cookie_currencies": sorted({c or "-" for c in self.page_cookie_currencies}),
            "published_products_count": self.published_products_count,
            "wanted": self.wanted,
            "found": len(self.products),
            "note": self.note[:160],
        }


@dataclass(frozen=True)
class SeedCatalogueRead:
    """What `_refresh_external_seed_by_id(..., catalogue_read=...)` is handed instead of a page read."""

    snapshot: Any
    observation: "liveness.DestinationObservation"
    host: str
    handle: str


async def _get_json(client: httpx.AsyncClient, url: str, agent: str) -> Tuple[Optional[str], str]:
    """(body, "") of ONE politeness-gated JSON GET, or (None, why not). Never raises but on a stopped run."""
    try:
        await crawl_politeness.before_request(url, user_agent=agent, max_wait=0)
    except crawl_global_pacer.GlobalPaceStopped:
        raise
    except crawl_politeness.RobotsDisallowed:
        return None, "robots_disallowed"
    except Exception as exc:  # noqa: BLE001 - a gate failure is a refusal, not a crash
        return None, f"gate_error: {type(exc).__name__}"
    try:
        resp = await client.get(url, headers={"User-Agent": agent, "Accept": "application/json"})
    except crawl_global_pacer.GlobalPaceStopped:
        raise
    except crawl_politeness.RobotsDisallowed:
        return None, "robots_disallowed"
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__
    crawl_politeness.note_response(url, resp.status_code, retry_after=resp.headers.get("retry-after"), headers=resp.headers)
    shopify_edge_pacer.learn_from_response(str(resp.url), resp.headers)
    if resp.headers.get("cf-mitigated"):
        return None, "bot_challenge"
    if resp.status_code != 200:
        return None, f"http_{resp.status_code}"
    if "json" not in (resp.headers.get("content-type") or "").lower():
        return None, "not_json"
    return resp.text, ""


def _cart_currency(body: Optional[str]) -> Optional[str]:
    try:
        data = json.loads(body or "")
    except ValueError:
        return None
    raw = data.get("currency") if isinstance(data, dict) else None
    return raw if isinstance(raw, str) and _ISO_CURRENCY.match(raw) else None


def _client() -> httpx.AsyncClient:
    # NO COOKIE rides a later request (services.shopify_presentment): what one response set must not
    # steer the currency the next one is presented in. Every redirect hop pays a global slot.
    return no_cookie_client(
        timeout=_HTTP_TIMEOUT, follow_redirects=True,
        **crawl_global_pacer.redirect_client_kwargs(robots_user_agent=user_agent()),
    )


async def read_host_catalogue(
    host: str, wanted: Set[str], *, client: Optional[httpx.AsyncClient] = None
) -> HostCatalogue:
    """Read `host`'s currency and the catalogue entries of the `wanted` handles, at the run's pace.

    Requests, in order, each through `crawl_politeness` (robots.txt, per-host pacing, 429 backoff) and the
    run's installed global pacer: `/meta.json`; then catalogue pages until every wanted handle is found or
    `pages_worth_reading` runs out; then `/cart.js`, only when a page carried no `cart_currency`. Never
    raises, except `GlobalPaceStopped` (the run stopped: not a fact about this host)."""
    wanted = {str(h).strip().lower() for h in wanted if str(h).strip()}
    result = HostCatalogue(host=host, status=STATUS_ERROR, wanted=len(wanted))
    owns_client = client is None
    client = client or _client()
    agent = user_agent()
    try:
        body, why = await _get_json(client, f"https://{host}/meta.json", agent)
        meta = parse_meta(body)
        if meta is None:
            result.status, result.note = STATUS_BASE_UNREAD, why or "meta.json names no currency"
            return result
        result.base_currency = meta["currency"]
        try:
            count = json.loads(body or "{}").get("published_products_count")
        except (ValueError, AttributeError):
            count = None
        result.published_products_count = count if isinstance(count, int) and not isinstance(count, bool) else None
        max_pages = pages_worth_reading(len(wanted), result.published_products_count)
        if max_pages is None:
            result.status = STATUS_NOT_WORTH_IT
            return result
        read = await liveness.read_brand_catalogue(
            client, host, want_products=wanted, max_pages=max_pages, user_agent=agent
        )
        result.catalogue_status = read.status
        result.pages_read = len(read.page_set_cookies)
        result.note = read.note
        if not read.products:
            # Nothing to price: a page-1 refusal (`catalogue_<status>`), or pages that listed none of
            # the wanted handles. Either way /cart.js is not asked.
            result.status = STATUS_NONE_LISTED if read.page_set_cookies else f"catalogue_{read.status}"
            return result
        result.page_cookie_currencies = page_currencies(read.page_set_cookies)
        if not all(result.page_cookie_currencies):
            cart_body, _ = await _get_json(client, f"https://{host}/cart.js", agent)
            result.cart_currency = _cart_currency(cart_body)
        currency, proof = decide_host_currency(
            base=result.base_currency, page_cookies=result.page_cookie_currencies, cart=result.cart_currency
        )
        if currency is None:
            result.status = proof
            return result
        result.status, result.currency, result.proof, result.products = STATUS_OK, currency, proof, read.products
        return result
    except crawl_global_pacer.GlobalPaceStopped:
        raise
    except Exception as exc:  # noqa: BLE001 - the host falls back to page reads; the run goes on
        result.status, result.note = STATUS_ERROR, f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning("catalogue price lane: reading %s failed (%s); its seeds keep their page read", host, result.note)
        return result
    finally:
        if owns_client:
            await client.aclose()


def seed_catalogue_read(
    catalogue: Optional[HostCatalogue], url: Any, *, market: str = ""
) -> Tuple[Optional[SeedCatalogueRead], str]:
    """(the read to hand the refresh, "read") for a seed whose destination is `url`, or (None, why not)."""
    if catalogue is None:
        return None, "host_not_read"
    if catalogue.status != STATUS_OK or not catalogue.currency or not catalogue.proof:
        return None, catalogue.status
    if crawl_politeness.host_of(str(url or "")) != catalogue.host:
        return None, "other_host"
    handle = catalogue_handle(url)
    if not handle:
        return None, "path_not_catalogue_shaped"
    product = catalogue.products.get(handle)
    if product is None:
        return None, "handle_not_read"
    if url_variant_id(url) is None:
        return None, "url_variant_unreadable"
    snapshot = catalogue_snapshot(
        product, url=str(url), host=catalogue.host, currency=catalogue.currency, proof=catalogue.proof, market=market
    )
    if snapshot is None:
        return None, "url_variant_not_listed"
    # The sweep's own observation for a listed handle (`run_destination_sweep`): live, no HTTP status,
    # because the product page itself was not fetched.
    observation = liveness.DestinationObservation(liveness.VERDICT_LIVE, None, None, "listed in products.json")
    return SeedCatalogueRead(snapshot=snapshot, observation=observation, host=catalogue.host, handle=handle), "read"


__all__ = (
    "FLAG",
    "HostCatalogue",
    "READ_SOURCE",
    "SeedCatalogueRead",
    "catalogue_handle",
    "catalogue_min_seeds",
    "catalogue_price",
    "catalogue_refresh_enabled",
    "catalogue_snapshot",
    "decide_host_currency",
    "pages_worth_reading",
    "read_host_catalogue",
    "read_variants",
    "seed_catalogue_read",
    "url_variant_id",
)
