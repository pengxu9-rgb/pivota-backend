"""source = "shopify_markets": sibling offers in a SERVED market's currency for a Shopify storefront whose base
currency is another, proven per session. US (USD siblings) since 2026-09-26; SG (SGD siblings) since 2026-10-10.

Multi-market storefronts ADR, Phase 2 (approved by Peng 2026-09-26): "Allow USD offers from non-USD
stores that show US buyers USD prices, but only when the store's cart confirms USD for a US session --
brand stores first." This is `scripts/capture_us_market_offers.py`'s mechanics (lines 19-24, 447-462)
as a retailer_ingest source, scoped to ONE (storefront, brand) job instead of the no_us_offer cohort.

THE EVIDENCE STANDARD, in order, per job (ADR-026 decision 4.4, accepted 2026-10-11):
  1. `/meta.json` `ships_to_countries` names the market (the store's own declared reach), and the store's
     base currency is not already the market's (then its /products.json crawl IS its market offer).
  2. PRIMARY PROOF, the market-pinned read (services/shopify_presentment, the rule our payment proofs use):
     each `/products/<handle>.js?country=<M>` is read with NO cookie, and its price counts only when the
     `cart_currency` Set-Cookie of THAT SAME response names the market's currency. Every price carries its
     own proof: no session, no cart, nothing a later response must vouch for.
  3. FALLBACK, only when the first pinned answer does not present the market's currency and robots.txt
     allows the cart paths: the cookie session -- a MULTIPART POST `/localization` (`_method=put`,
     `country_code=<M>`), `/cart.js` reporting the market currency BEFORE any price is read, `.js` reads in
     that session, and `/cart.js` re-checked every SESSION_RECHECK_EVERY products and once at the end.
Measured (ADR-026 Appendix B, 2026-10-11, served stores): the pinned read found SGD pricing at every store the
session did, and at more; where both worked they agreed to the cent; an SGD cookie over base-currency numbers
never occurred; a few stores send no cookie at all (`currency_unprovable`). This REVISES the
2026-08 rule that "a `?country=` query is never evidence": that held for the bare query, whose payload names
no currency. With the same-response cookie, the query is the stronger proof.

THE TARGET MARKET (Peng 2026-10-10, "Build the SG currency support"): the job's options.market picks it,
from CAPTURE_MARKETS. Everything above reads with the market substituted -- ships_to names SG, the PUT says
country_code=SG, /cart.js must report SGD -- and the sibling is (market 'SG', currency 'SGD', source_system
'shopify_markets_sg_localization', offer id under 'offer:shopify_markets_sg:'). The US capture is byte-
identical to what it was: same source_system, same offer-id prefix and digest, same outcomes and reasons.
Measured motivation (coverage wave 2026-10-10, laptop probe of /cart.js?country=SG -- a lead, not this
module's evidence): many (brand, store) pairs for SG buyers sit at Shopify stores whose base is not SGD
but whose cart quotes SGD to SG (mostly USD- and JPY-base, nearly all of them retailers).

RETAILER STORES (the ADR phase was "brand stores first"): a retailer capture is allowed for SG only
(pipeline.validate_options). Its candidates are narrowed to the rows a RETAILER storefront crawl writes --
`ext:retailer:` listing keys, one per (host, URL) -- whose brand is one the job's cohort selects (the job's
brand, each vendor, the multi_brand `brands` spellings, and each vendor's measured family spelling:
_cohort_brand_keys). The seller is still read from the base offer (merchant_id, offer_type, offer_mode):
a retailer's sibling is that retailer's redirect offer, never a brand-direct one. And because a retailer's
base crawl in a SERVED market (a USD store's US job) already made it a seller there, the capture refuses
a retailer whose /meta.json does not ship to its base offers' served market: those base rows are a seller
claim the store does not back, and a human must retire them first (`base_market_not_shipped`).

IDENTITY COMES FROM THE BASE-CURRENCY CRAWL, never from the capture. Candidates are the live offers a
retailer_ingest crawl of THIS storefront already wrote (source_system catalog_enrichment_agent_v1,
priced in the store's base currency, on this brand's products); each captured price becomes a SIBLING
row beside its base offer -- same product, same SKU, same destination URL, same seller identity --
`(source_domain, market='US', currency='USD', source_system='shopify_markets_us_localization')`. The
base offer is never modified (ADR-024: sibling offers, never rewrites; no FX ever). A product the base
crawl never wrote is never created here. So the operator order per store is: a base-currency crawl job
(market=AU, require_currency=AUD; rows land stored and unservable), THEN this job. For SG the base crawl
is the store's base-currency market's storefront job: market US for a USD store (served: those rows are
its US offer), market JP for a JPY store (acquisition: stored, not served). A CAD/GBP/KRW store has no
ingest market for its base crawl (pipeline.INGEST_MARKETS), so it cannot be captured for SG yet.

POLITENESS. Same UA and the same per-host gate as the /products.json crawl (services.crawl_politeness:
per-host interval, robots.txt, 429/503 backoff). A 429/5xx stops the stage and retries it later on the
lane's backoff, like a throttled crawl. A 403, a non-JSON answer where JSON is due (a bot wall), or a
redirect to another host is UNVERIFIABLE: the job fails with that reason, nothing is written, and
nothing is retried or worked around.

ROBOTS.TXT IS OBEYED FOR EVERY PATH (Peng's standing rule: never scrape around a crawler block). Every
request -- the pinned `.js?country=` reads included -- goes through crawl_politeness.before_request: the crawl's
own parser, cache and fail-open policy (a robots.txt that 404s, 5xxs or times out is "no restrictions"; only an
explicit Disallow blocks), with no special case. The fallback session needs both SESSION_PATHS (/localization,
/cart.js); robots.txt is asked about them up front and recorded. Where either is disallowed (about a fifth of measured
stores, Appendix B) the pinned read is the only proof, and a store whose pinned read does not present the
market's currency is refused as `robots_disallows_cart` (or `currency_unprovable` when it sent no cookie).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from services import crawl_politeness
from services.crawl_politeness import CrawlPaced, RobotsDisallowed

SOURCE = "shopify_markets"
#: The markets this capture proves and writes, each a SERVED market (ADR-024: `market` is the declared
#: destination). US since 2026-09-26; SG since 2026-10-10 (Peng). The default -- a job with no
#: options.market, every job queued before SG -- is CAPTURE_MARKET.
CAPTURE_MARKETS = ("US", "SG")
CAPTURE_MARKET = "US"
#: catalog_offers.source_system of a US sibling (the ADR's name for it); source_system_for(market) for any.
SOURCE_SYSTEM = "shopify_markets_us_localization"
#: Disjoint from ingestion's "offer:catalog_enrichment_agent_v1:" and the old script's "offer:us_market:"
#: (keyed on product_key alone, so two storefronts of one product would collide there). The US prefix;
#: offer_id_prefix_for(market) for any -- one prefix per market, so a US and an SG sibling of the same base
#: offer can never share an id (and the digest input carries the market too).
OFFER_ID_PREFIX = "offer:shopify_markets_us:"
#: The product_key every RETAILER storefront crawl writes (ingestion._build_pdp_insert: "ext:" + "retailer:"
#: + sha256(host + path)): a retailer capture prices only these, never a canonical row on the same host.
RETAILER_LISTING_KEY_PREFIX = "ext:retailer:"
#: The offers a sibling may stand beside: the ones the base-currency crawl (this lane) wrote.
BASE_SOURCE_SYSTEM = "catalog_enrichment_agent_v1"
#: Re-verify the session every N product reads (the script's value): a decayed localization cookie
#: would silently relabel base-currency prices as USD, the #1636/#1642 defect class.
SESSION_RECHECK_EVERY = 25
#: The paths the fallback session proof cannot do without. robots.txt is asked about them before the first
#: request to the store; a Disallow on either rules the fallback out (module docstring, ROBOTS.TXT).
SESSION_PATHS = ("/localization", "/cart.js")
#: How a sibling's price was proven (offer_payload "proof", evidence "proof"): the market-pinned read
#: (?country=<M> + that response's own cart_currency cookie), or the localized cookie session's cart.
PROOF_PINNED = "pinned_country_cookie"
PROOF_SESSION = "session_cart"
HTTP_TIMEOUT_S = 20.0
#: Tests replace this with an httpx.MockTransport; None is the real network.
HTTP_TRANSPORT = None


class MarketsRefused(Exception):
    """The capture ends without a plan: `outcome`/`status`/`reason` go on the run and the job, and
    `checks` records the evidence read so far. `transient` = a 429/5xx: retry the stage later."""

    def __init__(self, outcome: str, status: str, reason: str, *, checks: Optional[Dict[str, Any]] = None,
                 transient: bool = False):
        super().__init__(reason)
        self.outcome, self.status, self.reason = outcome, status, reason
        self.checks, self.transient = checks, transient


def capture_market(job: Dict[str, Any]) -> str:
    """The market this job captures: options.market (normalized), else CAPTURE_MARKET. Anything outside
    CAPTURE_MARKETS raises -- pipeline.validate_options refuses it first; this is the module's own lock."""
    from services.region_pricing import normalize_region
    market = normalize_region((job.get("options") or {}).get("market") or CAPTURE_MARKET)
    if market not in CAPTURE_MARKETS:
        raise ValueError(f"shopify_markets captures {list(CAPTURE_MARKETS)} only, not {market!r}")
    return market


def source_system_for(market: str) -> str:
    """catalog_offers.source_system of a sibling for `market`: 'shopify_markets_us_localization' for US
    (SOURCE_SYSTEM, unchanged), 'shopify_markets_sg_localization' for SG."""
    return f"shopify_markets_{market.lower()}_localization"


def offer_id_prefix_for(market: str) -> str:
    """The offer-id namespace of a sibling for `market` ('offer:shopify_markets_us:' for US)."""
    return f"offer:shopify_markets_{market.lower()}:"


def sibling_offer_id(base_offer_id: str, market: str = CAPTURE_MARKET) -> str:
    """One sibling per (base offer, market), deterministic, so a re-run refreshes it instead of stacking
    a copy. US ids are byte-identical to the ids written before SG existed."""
    digest = hashlib.sha256(f"{base_offer_id}|{market}".encode("utf-8")).hexdigest()[:32]
    return f"{offer_id_prefix_for(market)}{digest}"


def _host(value: Optional[str]) -> str:
    raw = str(value or "").strip().lower()
    host = (urlsplit(raw if "//" in raw else f"https://{raw}").hostname or "").rstrip(".")
    return host.removeprefix("www.")


def handle_from_url(url: Optional[str]) -> Optional[str]:
    """The Shopify handle: the segment after /products/ in the base offer's own destination URL.
    None when the URL is not a product URL -- skipped, never guessed."""
    segments = [s for s in urlsplit(str(url or "").strip()).path.split("/") if s]
    if len(segments) >= 2 and segments[-2] == "products" and segments[-1].strip():
        return segments[-1]
    return None


def _alnum(value: Any) -> str:
    return "".join(c for c in str(value or "").casefold() if c.isalnum())


#: Every live base-currency offer this storefront's crawl wrote, with the identity it hangs on. Read
#: once per stage; the write re-reads the base offer, product and SKU at write time (SIBLING_UPSERT_SQL).
CANDIDATES_SQL = """
    SELECT co.offer_id AS base_offer_id, co.product_key, co.sku_key, co.source_ref,
           upper(trim(coalesce(co.currency, ''))) AS currency, upper(trim(coalesce(co.market, ''))) AS market,
           cp.content_key, cp.brand, s.source_variant_id
      FROM catalog_offers co
      JOIN catalog_products cp ON cp.product_key = co.product_key
      JOIN catalog_skus s ON s.sku_key = co.sku_key
     WHERE lower(co.source_domain) = ANY(:hosts)
       AND co.source_system = :base_source_system
       AND co.suppressed_at IS NULL
       AND cp.suppressed_at IS NULL
       AND s.suppressed_at IS NULL
       AND upper(trim(coalesce(co.currency, ''))) <> :capture_currency
     ORDER BY co.product_key, co.sku_key
"""

#: One sibling, every identity column read from its BASE offer at write time (INSERT ... SELECT, the
#: capture script's pattern): a base offer, product or SKU retired during the minutes of HTTP between
#: the candidate read and the write yields NO row, never a sibling on a withdrawn identity. The
#: seller (merchant_id, offer_type, is_first_party) is the base offer's: same store, same seller; only
#: the price, the currency and the market differ. market and currency are refreshed by NOTHING on
#: conflict, and the WHERE refuses to touch a row that is suppressed or holds another market/currency:
#: RETURNING then yields nothing and the row is counted not written.
SIBLING_UPSERT_SQL = """
    INSERT INTO catalog_offers
      (offer_id, sku_key, product_key, merchant_id,
       catalog_track, truth_tier, readiness_tier, offer_mode,
       offer_type, is_first_party, market,
       channel, availability, currency,
       list_price, merchant_effective_price, estimated_best_price,
       price_confidence, source_system, source_ref, source_domain, offer_payload)
    SELECT
       :offer_id, base.sku_key, base.product_key, base.merchant_id,
       base.catalog_track, base.truth_tier, base.readiness_tier, base.offer_mode,
       base.offer_type, base.is_first_party, :market,
       base.channel, :availability, :currency,
       :list_price, :merchant_effective_price, :estimated_best_price,
       :price_confidence, :source_system, base.source_ref, base.source_domain, CAST(:offer_payload AS jsonb)
      FROM catalog_offers base
      JOIN catalog_products cp ON cp.product_key = base.product_key
      JOIN catalog_skus s ON s.sku_key = base.sku_key
     WHERE base.offer_id = CAST(:base_offer_id AS text)
       AND base.suppressed_at IS NULL
       AND cp.suppressed_at IS NULL
       AND s.suppressed_at IS NULL
    ON CONFLICT (offer_id) DO UPDATE SET
      availability = EXCLUDED.availability,
      list_price = EXCLUDED.list_price,
      merchant_effective_price = EXCLUDED.merchant_effective_price,
      estimated_best_price = EXCLUDED.estimated_best_price,
      offer_payload = EXCLUDED.offer_payload,
      updated_at = NOW()
     WHERE catalog_offers.suppressed_at IS NULL
       AND catalog_offers.currency = EXCLUDED.currency
       AND upper(catalog_offers.market) = upper(EXCLUDED.market)
    RETURNING offer_id
"""

#: What landed, read back per written sibling, with the index's verdict on its content_key.
READBACK_SQL = """
    SELECT o.offer_id, o.product_key, o.currency, o.market, (o.suppressed_at IS NULL) AS live,
           o.source_system, p.content_key, coalesce(ips.serving_eligible, false) AS serving,
           ips.blocker_code, ips.blocker_detail
      FROM catalog_offers o
      JOIN catalog_products p ON p.product_key = o.product_key
      LEFT JOIN index_pipeline_state ips ON ips.content_key = p.content_key
     WHERE o.offer_id = ANY(:offer_ids)
"""


def is_retailer_job(job: Dict[str, Any]) -> bool:
    """Anything but a brand store is a retailer -- the same test validate_options gates SG retailers on, so a job
    whose stored source_role is neither value (reachable only past enqueue's normalization) is never allowed
    as a retailer and then selected as a brand store (review of #2553)."""
    return (job.get("options") or {}).get("source_role") != "brand_official"


def _cohort_brand_keys(job: Dict[str, Any]) -> frozenset:
    """The folded brands whose base rows this job may price.

    A brand store: the job's brand, as before (byte-identical selection for every US job). A RETAILER
    store (2026-10-10): every spelling its storefront crawl can have written for the cohort. That crawl
    (curated_brand_feed.shopify_product_to_record, retailer branch) writes the vendor's measured family
    spelling (RETAILER_BRAND_CANONICAL) when it has one, else the override when it is the vendor folded
    equal, else the vendor's own. So every row's brand folds to a VENDOR or to a vendor's family spelling,
    never to the job's brand unless that is a vendor's too: the job's brand alone would miss "Purito
    SEOUL" for a "Purito" cohort, and would take a multi_brand cohort's label (only a label there) for a
    brand. Keys: each vendor, each options.brands spelling (multi_brand), each one's family spelling."""
    if not is_retailer_job(job):
        return frozenset({_alnum(job.get("brand"))})
    from services.curated_brand_feed import RETAILER_BRAND_CANONICAL, _retailer_brand_family
    o = job.get("options") or {}
    spellings = list(o.get("vendors") or []) + list((o.get("brands") or {}).values())
    keys = {_alnum(v) for v in spellings}
    for key in list(keys):
        family = _retailer_brand_family(key)
        if family is not None:
            keys.add(_alnum(RETAILER_BRAND_CANONICAL[family]))
    return frozenset(k for k in keys if k)


async def load_candidates(job: Dict[str, Any], db: Any) -> List[Dict[str, Any]]:
    """The base offers of THIS job's storefront and cohort, as rows. A retailer job's are its listing rows
    only (RETAILER_LISTING_KEY_PREFIX): this lane's retailer crawl writes nothing else."""
    host = _host(job["domain"])
    rows = await db.fetch_all(CANDIDATES_SQL, {
        "hosts": [host, f"www.{host}"], "base_source_system": BASE_SOURCE_SYSTEM,
        "capture_currency": _capture_currency(capture_market(job))})
    brands = _cohort_brand_keys(job)
    out = [dict(r) for r in rows or [] if _alnum(dict(r).get("brand")) in brands]
    if is_retailer_job(job):
        out = [r for r in out if str(r.get("product_key") or "").startswith(RETAILER_LISTING_KEY_PREFIX)]
    return out


def _capture_currency(market: str = CAPTURE_MARKET) -> str:
    from services.region_pricing import pricing_currency_for_region
    return pricing_currency_for_region(market)


def _is_json(resp: Any) -> bool:
    """A JSON-shaped answer: application/json, or Shopify's own `text/javascript` (2026-10-11).

    Shopify serves its AJAX endpoints -- `/cart.js` and `/products/<handle>.js` -- as
    `text/javascript; charset=utf-8` with a JSON body. Requiring "json" in the content type refused every
    storefront as a bot wall at the first `/cart.js` read (the session proof), so the capture never produced
    one offer in prod (0 shopify_markets_* siblings; the tests' fakes answered application/json, which
    Shopify does not send). JavaScript-typed answers are accepted HERE only as a
    candidate: `_Session.json` still requires the body to parse to a JSON object or array, so a JS challenge
    page is refused exactly like the HTML wall it stands in for."""
    ctype = str(resp.headers.get("content-type") or "").lower()
    return "json" in ctype or "javascript" in ctype


class _Session:
    """Requests against one storefront host -- cookie-less pinned reads, or the fallback's cookie session --
    every request paced through the crawl's gate,
    every answer classified, the final host of every redirect chain checked."""

    def __init__(self, client: Any, host: str, polite: Any, evidence: Dict[str, Any],
                 market: str = CAPTURE_MARKET):
        self.client, self.host, self.polite, self.evidence = client, host, polite, evidence
        self.market = market
        self.requests = 0

    def _refuse(self, outcome: str, status: str, reason: str, *, transient: bool = False) -> MarketsRefused:
        return MarketsRefused(outcome, status, reason, transient=transient)

    async def request(self, method: str, path: str, *, expect_json: bool, **kw: Any) -> Any:
        """The response, unless it is a block (unverifiable), a throttle (transient) or a redirect to
        another host; a 200 that should be JSON and is not is a bot wall. Other codes are the caller's."""
        from services.curated_brand_feed import _UA
        url = f"https://{self.host}{path}"
        # Shopify Markets capture (/localization, /cart.js) is Shopify by construction: pace it on the
        # crawl IP's shared Shopify-edge budget (a no-op with CRAWL_SHOPIFY_EDGE_PACER_ENABLED off).
        from services import shopify_edge_pacer
        shopify_edge_pacer.mark_shopify_host(self.host)
        try:
            await self.polite.before_request(url, user_agent=_UA, max_wait=0)
        except RobotsDisallowed as exc:
            raise self._refuse("robots_disallowed", "nothing", _robots_refusal(path, self.host, self.market)) from exc
        except CrawlPaced as exc:  # CrawlDelayTooLong: the host asks for less than we can
            raise self._refuse("crawl_paced", "failed", f"{path}: {exc}") from exc
        import httpx
        try:
            self.requests += 1
            resp = await self.client.request(method, url, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise self._refuse("crawl_throttled", "queued", f"{path}: {type(exc).__name__}", transient=True) from exc
        self.polite.note_response(url, resp.status_code, retry_after=resp.headers.get("retry-after"),
                                  headers=resp.headers)
        final = _host(str(getattr(resp, "url", "") or url))
        if final != self.host:
            # A geo-router or a sibling regional store: another catalog, another currency.
            refusal = self._refuse("capture_unverifiable", "failed",
                                   f"{path}: the storefront redirected to {final or '(unknown)'}; a regional "
                                   f"store is a different store")
            refusal.off_host = final or "(unknown)"
            raise refusal
        code = resp.status_code
        if code == 429 or code >= 500:
            raise self._refuse("crawl_throttled", "queued", f"HTTP {code} at {path}", transient=True)
        if code in (401, 403):
            raise self._refuse("capture_unverifiable", "failed",
                               f"HTTP {code} at {path}: a block is unverifiable; never worked around")
        if code == 200 and expect_json and not _is_json(resp):
            raise self._refuse("capture_unverifiable", "failed",
                               f"{path} answered {resp.headers.get('content-type') or 'no content type'}, not "
                               f"JSON (a bot wall reads exactly like this); unverifiable")
        return resp

    async def json(self, path: str) -> Tuple[int, Any]:
        """(status, parsed body); the body is None for any status but 200."""
        resp = await self.request("GET", path, expect_json=True)
        if resp.status_code != 200:
            return resp.status_code, None
        return 200, self._body(resp, path)

    async def json_presented(self, path: str) -> Tuple[int, Any, Optional[str], Optional[str]]:
        """(status, parsed body, currency, problem): `json`, plus the currency THIS response was presented in,
        from its own `cart_currency` Set-Cookie (shopify_presentment.presentment_currency: exactly one
        three-letter value, else `problem`)."""
        from services.shopify_presentment import presentment_currency
        resp = await self.request("GET", path, expect_json=True)
        currency, problem = presentment_currency(resp.headers.get_list("set-cookie"))
        if resp.status_code != 200:
            return resp.status_code, None, currency, problem
        return 200, self._body(resp, path), currency, problem

    def _body(self, resp: Any, path: str) -> Any:
        try:
            body = resp.json()
        except ValueError:
            raise self._refuse("capture_unverifiable", "failed", f"{path} did not parse as JSON; unverifiable")
        if not isinstance(body, (dict, list)):
            # A JS-typed answer that is a bare string or number is no storefront JSON: a challenge or a stub.
            raise self._refuse("capture_unverifiable", "failed",
                               f"{path} answered {type(body).__name__}, not a JSON object; unverifiable")
        return body

    async def cart_currency(self) -> Optional[str]:
        _code, cart = await self.json("/cart.js")
        currency = cart.get("currency") if isinstance(cart, dict) else None
        return currency if isinstance(currency, str) else None


def _robots_refusal(path: str, host: str, market: str = CAPTURE_MARKET) -> str:
    """The recorded reason for a capture refused by robots.txt: `robots_disallowed:<path>` first, so the
    ledger names exactly which path the store forbids."""
    return (f"robots_disallowed:{path} -- {host}'s robots.txt disallows it for our user agent; the capture "
            f"obeys robots.txt and never works around it, so no {market} offer is captured")


async def _session_paths_allowed(polite: Any, host: str, evidence: Dict[str, Any]) -> bool:
    """Ask robots.txt about every SESSION_PATH before the first request to the store (the same gate, parser
    and cache every other request uses). Recorded; a Disallow rules out the fallback session, not the
    pinned read (module docstring, ROBOTS.TXT)."""
    from services.curated_brand_feed import _UA
    for path in SESSION_PATHS:
        if not await polite.robots_allows(f"https://{host}{path}", user_agent=_UA):
            evidence["robots"]["disallowed"] = path
            evidence["robots"]["session_paths_allowed"] = False
            return False
    evidence["robots"]["session_paths_allowed"] = True
    return True


def _first_sellable(variants: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[float]]:
    """The canonical offer's variant, chosen by the BASE crawl's rule (curated_brand_feed.
    shopify_product_to_record: the first variant priced at or above MIN_SELLABLE_PRICE), over the
    session's prices (USD for US, SGD for SG)."""
    from services.curated_brand_feed import MIN_SELLABLE_PRICE
    for v in variants:
        price = _minor_units(v.get("price"))
        if price is not None and price >= MIN_SELLABLE_PRICE:
            return v, price
    return None, None


def _minor_units(cents: Any) -> Optional[float]:
    """/products/<handle>.js prices are integer minor units (cents; both capture currencies, USD and SGD,
    have two decimals). Anything else is not a price."""
    if isinstance(cents, bool) or not isinstance(cents, int) or cents <= 0:
        return None
    return round(cents / 100.0, 2)


def _plan_product(host: str, handle: str, detail: Any, bases: List[Dict[str, Any]],
                  evidence: Dict[str, Any], skipped: Dict[str, int],
                  market: str = CAPTURE_MARKET, *, cart_currency: Optional[str] = None) -> List[Dict[str, Any]]:
    """The sibling rows for one product's base offers, from its market-proven .js payload. `cart_currency`:
    the currency THIS payload's own response proved (the pinned read); else the session's /cart.js answer."""
    from services.catalog_enrichment_agent.ingestion import SKU_SUFFIX, _availability, _observed_stock
    from services.curated_brand_feed import MIN_SELLABLE_PRICE

    def skip(reason: str, n: int = 1) -> List[Dict[str, Any]]:
        skipped[reason] = skipped.get(reason, 0) + n
        return []

    if (not isinstance(detail, dict) or str(detail.get("handle") or "").strip() != handle
            or not isinstance(detail.get("variants"), list)
            or not all(isinstance(v, dict) for v in detail["variants"])):
        return skip("product_js_not_this_product", len(bases))
    variants = detail["variants"]
    by_id = {str(v.get("id")): v for v in variants if v.get("id") is not None}
    currency, source_system = _capture_currency(market), source_system_for(market)
    rows = []
    for base in bases:
        canonical = base["sku_key"] == f"{base['product_key']}{SKU_SUFFIX}"
        if canonical:
            variant, price = _first_sellable(variants)
        else:
            variant = by_id.get(str(base.get("source_variant_id") or ""))
            price = _minor_units(variant.get("price")) if variant else None
            if variant is None:
                skip("variant_not_in_product_js")
                continue
            if price is not None and price < MIN_SELLABLE_PRICE:
                price = None
        if variant is None or price is None:
            skip(f"no_sellable_{currency.lower()}_price")
            continue
        rows.append({
            "offer_id": sibling_offer_id(base["base_offer_id"], market),
            "base_offer_id": base["base_offer_id"],
            "product_key": base["product_key"], "sku_key": base["sku_key"], "content_key": base.get("content_key"),
            "market": market, "currency": currency,
            "availability": _availability(_observed_stock(variant.get("available"))),
            "list_price": price, "merchant_effective_price": price, "estimated_best_price": price,
            "price_confidence": 0.7, "source_system": source_system,
            "offer_payload": json.dumps({
                "capture": source_system, "captured_from": host, "handle": handle,
                "destination_url": base.get("source_ref"), "variant_id": str(variant.get("id")),
                "price_cents": variant.get("price"), "base_offer_id": base["base_offer_id"],
                "base_currency": base.get("currency"),
                "cart_currency": cart_currency or evidence.get("cart_currency"),
                "localization_status": evidence.get("localization_status"), "proof": evidence.get("proof"),
                "captured_at": evidence.get("captured_at"),
            }, ensure_ascii=False, sort_keys=True),
        })
    return rows


async def capture(job: Dict[str, Any], *, db: Any, max_products: int, polite: Any = None,
                  transport: Any = None) -> Dict[str, Any]:
    """Read the storefront's prices in the job's market (capture_market: US, or SG) for the job's base
    offers, each proven by its own market-pinned response, or (fallback) inside one proven session.
    Returns {"planned": [sibling rows], "checks": {...}};
    raises MarketsRefused. Writes nothing."""
    import httpx

    market = capture_market(job)
    currency = _capture_currency(market)
    tag = market.lower()  # outcome names: us_session_unproven (as before), sg_session_unproven, ...
    host = _host(job["domain"])
    evidence: Dict[str, Any] = {"source": SOURCE, "market": market, "host": host,
                                "robots": {"session_paths_checked": list(SESSION_PATHS)}}
    checks: Dict[str, Any] = {"markets_capture": evidence}

    def refused(exc: MarketsRefused) -> MarketsRefused:
        exc.checks = checks
        return exc

    candidates = await load_candidates(job, db)
    by_handle: Dict[str, List[Dict[str, Any]]] = {}
    skipped: Dict[str, int] = {}
    for c in candidates:
        handle = handle_from_url(c.get("source_ref"))
        if not handle or _host(c.get("source_ref")) != host:
            skipped["no_handle_on_this_host"] = skipped.get("no_handle_on_this_host", 0) + 1
            continue
        by_handle.setdefault(handle, []).append(c)
    evidence.update(base_offers=len(candidates), base_products=len({c["product_key"] for c in candidates}),
                    base_markets=sorted({c.get("market") or "" for c in candidates}), skipped=skipped)
    if not by_handle:
        role = "retailer" if is_retailer_job(job) else "brand_official"
        raise refused(MarketsRefused(
            "no_base_rows", "nothing",
            f"no live base-currency offers of {job.get('brand')!r} from {host}: run its base-currency crawl "
            f"(source_role {role}, market = the store's home market) first; identity comes from it"))
    if len(by_handle) > max_products:
        raise refused(MarketsRefused(
            "crawl_capped", "failed",
            f"{len(by_handle)} products to capture, over options.max_products {max_products}: raise it or "
            f"cancel the job"))

    from services.shopify_presentment import no_cookie_client

    headers = {"User-Agent": _ua(), "Accept": "application/json"}
    client_kw = dict(follow_redirects=True, timeout=HTTP_TIMEOUT_S, headers=headers,
                     transport=transport if transport is not None else HTTP_TRANSPORT)
    polite = polite if polite is not None else crawl_politeness
    planned: List[Dict[str, Any]] = []
    requests = 0
    # The pinned reads carry no cookie at all: nothing a response sets may steer a later one, so every price
    # is proven by its own response alone (shopify_presentment.no_cookie_client).
    async with no_cookie_client(**client_kw) as pinned_client:
        pinned = _Session(pinned_client, host, polite, evidence, market)
        try:
            # 0. robots.txt about the fallback session's paths: asked and recorded, before any request.
            cart_allowed = await _session_paths_allowed(polite, host, evidence)
            # 1. The store's own reach and base currency.
            from services.storefront_currency import parse_meta
            resp = await pinned.request("GET", "/meta.json", expect_json=True)
            meta = parse_meta(resp.text) if resp.status_code == 200 else None
            if not meta:
                raise MarketsRefused("capture_unverifiable", "failed",
                                     f"/meta.json (HTTP {resp.status_code}) did not prove the store's currency")
            evidence["storefront"] = {k: meta.get(k) for k in ("name", "myshopify_domain", "currency",
                                                                "ships_to_countries")}
            ships = meta.get("ships_to_countries")
            if not isinstance(ships, list) or market not in ships:
                raise MarketsRefused(f"store_does_not_ship_to_{tag}", "nothing",
                                     f"{host}'s /meta.json ships_to_countries does not include "
                                     f"{market}: no {market} offer to capture")
            if meta["currency"] == currency:
                raise MarketsRefused(f"base_currency_is_{currency.lower()}", "nothing",
                                     f"{host} prices in {meta['currency']} already: its /products.json crawl "
                                     f"(source = storefront, market = {market}) is its {market} offer")
            if is_retailer_job(job):
                # 2026-10-10: a retailer's base crawl in a SERVED market made it a seller there (a USD
                # store's US job). The storefront crawl records ships_to but never gated on it (unless
                # options.require_ships_to_market), so a US seller claim the store does not back may exist.
                # Never stack a second market on it: refuse, loudly, and name the rows to retire.
                from services.region_pricing import ACQUISITION_MARKETS
                served_bases = sorted({str(c.get("market") or "").upper() for rows in by_handle.values()
                                       for c in rows} - set(ACQUISITION_MARKETS) - {""})
                unshipped = [m for m in served_bases if m not in ships]
                evidence["ships_to_base_markets"] = {m: m in ships for m in served_bases}
                if unshipped:
                    raise MarketsRefused(
                        "base_market_not_shipped", "failed",
                        f"{host}'s /meta.json ships_to_countries does not include {unshipped}, yet this lane's "
                        f"base crawl wrote {unshipped} offers for it: that seller claim is unbacked. Retire "
                        f"those base rows (or prove the reach) before capturing {market}")
            stale = [c for rows in by_handle.values() for c in rows if c.get("currency") != meta["currency"]]
            if stale:
                skipped["base_currency_not_the_stores"] = len(stale)
                by_handle = {h: [c for c in rows if c.get("currency") == meta["currency"]]
                             for h, rows in by_handle.items()}
                by_handle = {h: rows for h, rows in by_handle.items() if rows}
            # 2. The market-pinned read: each price proven by its own response's cart_currency cookie. The
            #    first answered product decides whether the store presents the market's currency this way.
            before_probe = dict(skipped)
            probe: Optional[Dict[str, Any]] = None
            for handle in sorted(by_handle):
                path = f"/products/{quote(handle, safe='')}.js?country={market}"
                try:
                    code, detail, presented, problem = await pinned.json_presented(path)
                except MarketsRefused as exc:
                    if not getattr(exc, "off_host", None):
                        raise
                    # The market lives on another host (a regional store, ADR-026 decision 5): this host does
                    # not present it. Never followed or priced -- the probe falls back, a later read is skipped.
                    code, detail, presented, problem = 200, None, None, f"redirected_to:{exc.off_host}"
                if code != 200:  # 404: delisted since the base crawl, or not sold in the market. Counted.
                    key = f"product_js_http_{code}"
                    skipped[key] = skipped.get(key, 0) + len(by_handle[handle])
                    continue
                if probe is None:
                    probe = {"handle": handle, "cart_currency": presented, "problem": problem}
                    evidence["pinned"] = dict(probe)
                    if presented != currency:
                        break  # the store does not present the market's currency to a pinned read
                    evidence["proof"] = PROOF_PINNED
                    evidence["captured_at"] = datetime.now(timezone.utc).isoformat()
                if presented != currency:
                    # This one response is not proof (another currency, no cookie, an off-host redirect): this
                    # product only, counted by why.
                    why = f"presented_{presented.lower()}" if presented else (problem or "unproven").split(":")[0]
                    key = f"pinned_{why}"
                    skipped[key] = skipped.get(key, 0) + len(by_handle[handle])
                    continue
                planned.extend(_plan_product(host, handle, detail, by_handle[handle], evidence, skipped, market,
                                             cart_currency=presented))
            requests = pinned.requests
            if probe is None or evidence.get("proof") == PROOF_PINNED:
                evidence.update(requests=requests, planned=len(planned),
                                planned_products=len({r["product_key"] for r in planned}))
                if probe is None:
                    evidence["pinned"] = {"answered": 0}
                return {"planned": planned, "checks": checks}
        except MarketsRefused as exc:
            evidence["requests"] = pinned.requests
            raise refused(exc)

    # 3. FALLBACK: the pinned read did not present the market's currency. Only a localized cookie session
    #    can still prove one, and only where robots.txt allows its cart paths.
    if not cart_allowed:
        evidence["requests"] = requests
        path = evidence["robots"].get("disallowed")
        if str(probe["problem"] or "").startswith("redirected_to:"):
            raise refused(MarketsRefused(
                "market_on_another_host", "nothing",
                f"market_on_another_host:{path} -- {host}'s pinned ?country={market} read redirected to "
                f"{probe['problem'].split(':', 1)[1]}: the {market} market is another storefront (never followed or "
                f"priced here), and robots.txt disallows {path} for the session fallback"))
        if probe["cart_currency"] is None:
            raise refused(MarketsRefused(
                "currency_unprovable", "nothing",
                f"currency_unprovable:{path} -- {host}'s pinned ?country={market} read set no usable "
                f"cart_currency cookie ({probe['problem']}), and its robots.txt disallows {path} for the session "
                f"fallback: no response can prove a {currency} price, so no {market} offer is captured"))
        raise refused(MarketsRefused(
            "robots_disallows_cart", "nothing",
            f"robots_disallows_cart:{path} -- {host}'s pinned ?country={market} read was presented in "
            f"{probe['cart_currency']}, not {currency}, and its robots.txt disallows {path}, which the session "
            f"fallback needs; robots.txt is never worked around, so no {market} offer is captured"))
    skipped.clear()
    skipped.update(before_probe)  # the session re-reads every product; the probe's counts are not doubled
    async with httpx.AsyncClient(**client_kw) as client:
        session = _Session(client, host, polite, evidence, market)
        session.requests = requests
        evidence["proof"] = PROOF_SESSION
        try:
            # A session in the market: a cookie jar, then the multipart localization PUT.
            evidence["home_status"] = (await session.request("GET", "/", expect_json=False)).status_code
            # Not followed: its answer is the cookie (a 302 back to return_to), and following would be a
            # request the politeness gate never saw.
            loc = await session.request(
                "POST", "/localization", expect_json=False, follow_redirects=False,
                files={"_method": (None, "put"), "country_code": (None, market), "return_to": (None, "/")})
            evidence["localization_status"] = loc.status_code
            # The proof, BEFORE any price is read.
            evidence["cart_currency"] = await session.cart_currency()
            evidence["captured_at"] = datetime.now(timezone.utc).isoformat()
            if evidence["cart_currency"] != currency:
                raise MarketsRefused(f"{tag}_session_unproven", "nothing",
                                     f"{host}'s /cart.js reports {evidence['cart_currency'] or 'no currency'} "
                                     f"after localizing to {market}: the store does not confirm {currency} for "
                                     f"a {market} session, so no price it shows is a {currency} offer")
            # Prices, inside the proven session, committed only past a passing re-check.
            pending: List[Dict[str, Any]] = []
            rechecks = 0
            since = 0
            for handle in sorted(by_handle):
                code, detail = await session.json(f"/products/{quote(handle, safe='')}.js")
                if code != 200:  # 404: delisted since the base crawl. Counted, never guessed.
                    key = f"product_js_http_{code}"
                    skipped[key] = skipped.get(key, 0) + len(by_handle[handle])
                    continue
                pending.extend(_plan_product(host, handle, detail, by_handle[handle], evidence, skipped, market))
                since += 1
                if since >= SESSION_RECHECK_EVERY:
                    rechecks += 1
                    if await session.cart_currency() != currency:
                        raise MarketsRefused(f"{tag}_session_lost", "failed",
                                             f"{host}'s /cart.js stopped reporting {currency} mid-capture (after "
                                             f"{rechecks} re-checks): every price read since the last proof is void")
                    planned.extend(pending)
                    pending, since = [], 0
            if pending:
                rechecks += 1
                if await session.cart_currency() != currency:
                    raise MarketsRefused(f"{tag}_session_lost", "failed",
                                         f"{host}'s /cart.js stopped reporting {currency} at the final re-check: "
                                         f"every price read since the last proof is void")
                planned.extend(pending)
        except MarketsRefused as exc:
            evidence["requests"] = session.requests
            raise refused(exc)
    evidence.update(requests=session.requests, session_rechecks=rechecks, planned=len(planned),
                    planned_products=len({r["product_key"] for r in planned}))
    return {"planned": planned, "checks": checks}


def _ua() -> str:
    from services.curated_brand_feed import _UA
    return _UA


async def write_siblings(planned: List[Dict[str, Any]], *, db: Any) -> Dict[str, Any]:
    """Write the planned siblings. Every row passes the shared offer guard (a price, a LIVE SKU behind
    it) and the currency = market rule first; each write is counted from RETURNING, never assumed."""
    from services.catalog_offer_writer_guard import guard_catalog_offer_rows
    from services.region_pricing import require_market_currency

    for row in planned:
        require_market_currency(row["market"], row["currency"])  # the writer's own lock, per row
    accepted, reasons, _rejected = await guard_catalog_offer_rows(planned, db=db, live_only=True)
    written: List[Dict[str, Any]] = []
    not_written: List[str] = []
    for row in accepted:
        params = {k: row[k] for k in ("offer_id", "base_offer_id", "market", "availability", "currency",
                                       "list_price", "merchant_effective_price", "estimated_best_price",
                                       "price_confidence", "source_system", "offer_payload")}
        landed = await db.fetch_val(SIBLING_UPSERT_SQL, params)
        if landed is None:
            not_written.append(row["offer_id"])
            continue
        written.append(row)
    return {"written": written, "not_written": not_written, "refused": dict(reasons)}


async def republish(content_keys: List[str], *, db: Any, source_system: str = SOURCE_SYSTEM) -> List[str]:
    """Rebuild each touched content_key's PDP view and recompute its serving eligibility (the capture
    script's apply step), recorded under the market's `source_system`. Returns the keys that failed, for
    the readback to name."""
    from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key
    from services.index_pipeline_state_service import recompute_serving_eligibility

    failed = []
    for ck in content_keys:
        try:
            await refresh_agent_pdp_view_for_content_key(ck, refresh_source=source_system, db=db)
            await recompute_serving_eligibility(ck, reason=source_system, db=db)
        except Exception:  # noqa: BLE001 -- named in the readback, never silent
            failed.append(ck)
    return failed


async def readback(written: List[Dict[str, Any]], *, db: Any, republish_failed: Optional[List[str]] = None,
                   market: str = CAPTURE_MARKET) -> Dict[str, Any]:
    """Every sibling reported written must be live, in `market`'s currency and stamped `market` (USD/US,
    or SGD/SG). A content_key the index still blocks as no_us_offer after such a sibling landed on it is a
    problem: the gate reads currency against the served regions, so this means the write is not what it
    claims (for SG, the pipeline refuses to run unless this process serves SG --
    pipeline._require_served_market_is_served -- so the verdict here can rely on it). Any other blocker
    is the product's own content gate and only noted: the capture adds offers, it never touches content."""
    if not written:
        return {"ok": False, "problems": [{"problem": "no sibling offer was written"}], "notes": [], "rows": []}
    from services.agent_decision_gates import BLOCKER_NO_US_OFFER
    rows = [dict(r) for r in await db.fetch_all(READBACK_SQL, {"offer_ids": [w["offer_id"] for w in written]})]
    by_id = {r["offer_id"]: r for r in rows}
    currency = _capture_currency(market)
    problems, notes = [], []
    for w in written:
        r = by_id.get(w["offer_id"])
        if r is None:
            problems.append({"offer_id": w["offer_id"], "problem": "reported written, not in catalog_offers"})
        elif not r.get("live") or r.get("currency") != currency or str(r.get("market") or "").upper() != market:
            problems.append({"offer_id": w["offer_id"],
                             "problem": f"live {r.get('live')}, currency {r.get('currency')}, market {r.get('market')}"})
    served = set()
    for ck in sorted({r.get("content_key") for r in rows if r.get("content_key")}):
        state = next(r for r in rows if r.get("content_key") == ck)
        if state.get("serving"):
            served.add(ck)
        elif state.get("blocker_code") == BLOCKER_NO_US_OFFER:
            problems.append({"content_key": ck, "problem": f"a {currency} sibling landed, but the index still "
                                                           f"blocks it as no_us_offer"})
        else:
            notes.append({"content_key": ck, "kind": "index_refused",
                          "note": f"not served: blocker {state.get('blocker_code') or 'unknown'}"})
    for ck in republish_failed or []:
        problems.append({"content_key": ck, "problem": "republish (PDP view / serving recompute) failed"})
    return {"ok": not problems, "problems": problems, "notes": notes, "rows": rows,
            "served_content_keys": len(served)}
