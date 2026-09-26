"""source = "shopify_markets": USD sibling offers for a non-USD Shopify storefront, proven per session.

Multi-market storefronts ADR, Phase 2 (approved by Peng 2026-09-26): "Allow USD offers from non-USD
stores that show US buyers USD prices, but only when the store's cart confirms USD for a US session --
brand stores first." This is `scripts/capture_us_market_offers.py`'s mechanics (lines 19-24, 447-462)
as a retailer_ingest source, scoped to ONE (storefront, brand) job instead of the no_us_offer cohort.

THE EVIDENCE STANDARD, in order, per job (ADR section 3.3):
  1. `/meta.json` `ships_to_countries` names US (the store's own declared reach). A USD base store is
     refused too: its /products.json crawl already IS its US offer (source = storefront).
  2. POST `/localization` as MULTIPART with `_method=put`, `country_code=US`, in a cookie session (a
     urlencoded POST without `_method` silently no-ops; probed 2026-08-14).
  3. `/cart.js` in that session reports `currency == "USD"` BEFORE any price is read. Anything else is a
     clean refusal: a recorded reason, no rows.
  4. `/products/<handle>.js` read inside the same session; `/cart.js` is re-checked every
     SESSION_RECHECK_EVERY products and once at the end, and a check that no longer reports USD voids
     the whole capture (the .js payload carries no currency, so a decayed session could only ever be
     caught out of band).
A `?country=US` query is never evidence (ADR section 1.3: it moved prices at 5 of 30 stores, the
payload has no currency field, and an unmoved number proves nothing), so this module never sends one.

IDENTITY COMES FROM THE BASE-CURRENCY CRAWL, never from the capture. Candidates are the live offers a
retailer_ingest crawl of THIS storefront already wrote (source_system catalog_enrichment_agent_v1,
priced in the store's base currency, on this brand's products); each captured price becomes a SIBLING
row beside its base offer -- same product, same SKU, same destination URL, same seller identity --
`(source_domain, market='US', currency='USD', source_system='shopify_markets_us_localization')`. The
base offer is never modified (ADR-024: sibling offers, never rewrites; no FX ever). A product the base
crawl never wrote is never created here. So the operator order per store is: a base-currency crawl job
(market=AU, require_currency=AUD; rows land stored and unservable), THEN this job.

POLITENESS. Same UA and the same per-host gate as the /products.json crawl (services.crawl_politeness:
per-host interval, robots.txt, 429/503 backoff). A 429/5xx stops the stage and retries it later on the
lane's backoff, like a throttled crawl. A 403, a non-JSON answer where JSON is due (a bot wall), or a
redirect to another host is UNVERIFIABLE: the job fails with that reason, nothing is written, and
nothing is retried or worked around. The two SESSION endpoints (/localization, /cart.js) are paced and
backed off through the same gate but not robots-checked -- Shopify's default robots.txt disallows
`/cart`, which is written for indexers and would make the approved evidence (a cart that reports USD)
impossible to read at all; the exemption is exactly those two paths and is recorded on every run.
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
#: catalog_offers.source_system of every sibling this module writes (the ADR's name for it).
SOURCE_SYSTEM = "shopify_markets_us_localization"
#: The ONLY market this capture proves and writes. ADR-024: `market` is the declared destination.
CAPTURE_MARKET = "US"
#: Disjoint from ingestion's "offer:catalog_enrichment_agent_v1:" and the old script's "offer:us_market:"
#: (keyed on product_key alone, so two storefronts of one product would collide there).
OFFER_ID_PREFIX = "offer:shopify_markets_us:"
#: The offers a sibling may stand beside: the ones the base-currency crawl (this lane) wrote.
BASE_SOURCE_SYSTEM = "catalog_enrichment_agent_v1"
#: Re-verify the session every N product reads (the script's value): a decayed localization cookie
#: would silently relabel base-currency prices as USD, the #1636/#1642 defect class.
SESSION_RECHECK_EVERY = 25
#: The robots.txt exemption described in the module docstring: these paths and no others.
SESSION_PATHS_NOT_ROBOTS_CHECKED = ("/localization", "/cart.js")
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


def sibling_offer_id(base_offer_id: str) -> str:
    """One sibling per base offer, deterministic, so a re-run refreshes it instead of stacking a copy."""
    digest = hashlib.sha256(f"{base_offer_id}|{CAPTURE_MARKET}".encode("utf-8")).hexdigest()[:32]
    return f"{OFFER_ID_PREFIX}{digest}"


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


async def load_candidates(job: Dict[str, Any], db: Any) -> List[Dict[str, Any]]:
    """The base offers of THIS job's storefront and brand, as rows."""
    host = _host(job["domain"])
    rows = await db.fetch_all(CANDIDATES_SQL, {
        "hosts": [host, f"www.{host}"], "base_source_system": BASE_SOURCE_SYSTEM,
        "capture_currency": _capture_currency()})
    brand = _alnum(job.get("brand"))
    return [dict(r) for r in rows or [] if _alnum(dict(r).get("brand")) == brand]


def _capture_currency() -> str:
    from services.region_pricing import pricing_currency_for_region
    return pricing_currency_for_region(CAPTURE_MARKET)


def _is_json(resp: Any) -> bool:
    return "json" in str(resp.headers.get("content-type") or "").lower()


class _Session:
    """One cookie session against one storefront host: every request paced through the crawl's gate,
    every answer classified, the final host of every redirect chain checked."""

    def __init__(self, client: Any, host: str, polite: Any, evidence: Dict[str, Any]):
        self.client, self.host, self.polite, self.evidence = client, host, polite, evidence
        self.requests = 0

    def _refuse(self, outcome: str, status: str, reason: str, *, transient: bool = False) -> MarketsRefused:
        return MarketsRefused(outcome, status, reason, transient=transient)

    async def request(self, method: str, path: str, *, expect_json: bool, **kw: Any) -> Any:
        """The response, unless it is a block (unverifiable), a throttle (transient) or a redirect to
        another host; a 200 that should be JSON and is not is a bot wall. Other codes are the caller's."""
        from services.curated_brand_feed import _UA
        url = f"https://{self.host}{path}"
        try:
            if path.split("?")[0] in SESSION_PATHS_NOT_ROBOTS_CHECKED:
                await self.polite.await_slot(url, user_agent=_UA, max_wait=0)
            else:
                await self.polite.before_request(url, user_agent=_UA, max_wait=0)
        except RobotsDisallowed as exc:
            raise self._refuse("robots_disallowed", "failed", f"{path}: {exc}") from exc
        except CrawlPaced as exc:  # CrawlDelayTooLong: the host asks for less than we can
            raise self._refuse("crawl_paced", "failed", f"{path}: {exc}") from exc
        import httpx
        try:
            self.requests += 1
            resp = await self.client.request(method, url, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise self._refuse("crawl_throttled", "queued", f"{path}: {type(exc).__name__}", transient=True) from exc
        self.polite.note_response(url, resp.status_code, retry_after=resp.headers.get("retry-after"))
        final = _host(str(getattr(resp, "url", "") or url))
        if final != self.host:
            # A geo-router or a sibling regional store: another catalog, another currency.
            raise self._refuse("capture_unverifiable", "failed",
                               f"{path}: the storefront redirected to {final or '(unknown)'}; a regional "
                               f"store is a different store")
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
        try:
            return 200, resp.json()
        except ValueError:
            raise self._refuse("capture_unverifiable", "failed", f"{path} did not parse as JSON; unverifiable")

    async def cart_currency(self) -> Optional[str]:
        _code, cart = await self.json("/cart.js")
        currency = cart.get("currency") if isinstance(cart, dict) else None
        return currency if isinstance(currency, str) else None


def _first_sellable(variants: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[float]]:
    """The canonical offer's variant, chosen by the BASE crawl's rule (curated_brand_feed.
    shopify_product_to_record: the first variant priced at or above MIN_SELLABLE_PRICE), over the
    session's USD prices."""
    from services.curated_brand_feed import MIN_SELLABLE_PRICE
    for v in variants:
        price = _usd(v.get("price"))
        if price is not None and price >= MIN_SELLABLE_PRICE:
            return v, price
    return None, None


def _usd(cents: Any) -> Optional[float]:
    """/products/<handle>.js prices are integer minor units. Anything else is not a price."""
    if isinstance(cents, bool) or not isinstance(cents, int) or cents <= 0:
        return None
    return round(cents / 100.0, 2)


def _plan_product(host: str, handle: str, detail: Any, bases: List[Dict[str, Any]],
                  evidence: Dict[str, Any], skipped: Dict[str, int]) -> List[Dict[str, Any]]:
    """The sibling rows for one product's base offers, from its session-read .js payload."""
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
    rows = []
    for base in bases:
        canonical = base["sku_key"] == f"{base['product_key']}{SKU_SUFFIX}"
        if canonical:
            variant, price = _first_sellable(variants)
        else:
            variant = by_id.get(str(base.get("source_variant_id") or ""))
            price = _usd(variant.get("price")) if variant else None
            if variant is None:
                skip("variant_not_in_product_js")
                continue
            if price is not None and price < MIN_SELLABLE_PRICE:
                price = None
        if variant is None or price is None:
            skip("no_sellable_usd_price")
            continue
        rows.append({
            "offer_id": sibling_offer_id(base["base_offer_id"]),
            "base_offer_id": base["base_offer_id"],
            "product_key": base["product_key"], "sku_key": base["sku_key"], "content_key": base.get("content_key"),
            "market": CAPTURE_MARKET, "currency": _capture_currency(),
            "availability": _availability(_observed_stock(variant.get("available"))),
            "list_price": price, "merchant_effective_price": price, "estimated_best_price": price,
            "price_confidence": 0.7, "source_system": SOURCE_SYSTEM,
            "offer_payload": json.dumps({
                "capture": SOURCE_SYSTEM, "captured_from": host, "handle": handle,
                "destination_url": base.get("source_ref"), "variant_id": str(variant.get("id")),
                "price_cents": variant.get("price"), "base_offer_id": base["base_offer_id"],
                "base_currency": base.get("currency"), "cart_currency": evidence.get("cart_currency"),
                "localization_status": evidence.get("localization_status"),
                "captured_at": evidence.get("captured_at"),
            }, ensure_ascii=False, sort_keys=True),
        })
    return rows


async def capture(job: Dict[str, Any], *, db: Any, max_products: int, polite: Any = None,
                  transport: Any = None) -> Dict[str, Any]:
    """Read the storefront's US prices for the job's base offers inside one proven US session.
    Returns {"planned": [sibling rows], "checks": {...}}; raises MarketsRefused. Writes nothing."""
    import httpx

    host = _host(job["domain"])
    evidence: Dict[str, Any] = {"source": SOURCE, "market": CAPTURE_MARKET, "host": host,
                                "robots_not_checked": list(SESSION_PATHS_NOT_ROBOTS_CHECKED)}
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
        raise refused(MarketsRefused(
            "no_base_rows", "nothing",
            f"no live base-currency offers of {job.get('brand')!r} from {host}: run its base-currency crawl "
            f"(source_role brand_official, market = the store's home market) first; identity comes from it"))
    if len(by_handle) > max_products:
        raise refused(MarketsRefused(
            "crawl_capped", "failed",
            f"{len(by_handle)} products to capture, over options.max_products {max_products}: raise it or "
            f"cancel the job"))

    headers = {"User-Agent": _ua(), "Accept": "application/json"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT_S, headers=headers,
                                 transport=transport if transport is not None else HTTP_TRANSPORT) as client:
        session = _Session(client, host, polite if polite is not None else crawl_politeness, evidence)
        try:
            # 1. The store's own reach and base currency.
            from services.storefront_currency import parse_meta
            resp = await session.request("GET", "/meta.json", expect_json=True)
            meta = parse_meta(resp.text) if resp.status_code == 200 else None
            if not meta:
                raise MarketsRefused("capture_unverifiable", "failed",
                                     f"/meta.json (HTTP {resp.status_code}) did not prove the store's currency")
            evidence["storefront"] = {k: meta.get(k) for k in ("name", "myshopify_domain", "currency",
                                                                "ships_to_countries")}
            ships = meta.get("ships_to_countries")
            if not isinstance(ships, list) or CAPTURE_MARKET not in ships:
                raise MarketsRefused("store_does_not_ship_to_us", "nothing",
                                     f"{host}'s /meta.json ships_to_countries does not include "
                                     f"{CAPTURE_MARKET}: no US offer to capture")
            if meta["currency"] == _capture_currency():
                raise MarketsRefused("base_currency_is_usd", "nothing",
                                     f"{host} prices in {meta['currency']} already: its /products.json crawl "
                                     f"(source = storefront, market = US) is its US offer")
            stale = [c for rows in by_handle.values() for c in rows if c.get("currency") != meta["currency"]]
            if stale:
                skipped["base_currency_not_the_stores"] = len(stale)
                by_handle = {h: [c for c in rows if c.get("currency") == meta["currency"]]
                             for h, rows in by_handle.items()}
                by_handle = {h: rows for h, rows in by_handle.items() if rows}
            # 2. A US session: a cookie jar, then the multipart localization PUT.
            evidence["home_status"] = (await session.request("GET", "/", expect_json=False)).status_code
            # Not followed: its answer is the cookie (a 302 back to return_to), and following would be a
            # request the politeness gate never saw.
            loc = await session.request(
                "POST", "/localization", expect_json=False, follow_redirects=False,
                files={"_method": (None, "put"), "country_code": (None, CAPTURE_MARKET), "return_to": (None, "/")})
            evidence["localization_status"] = loc.status_code
            # 3. The proof, BEFORE any price is read.
            evidence["cart_currency"] = await session.cart_currency()
            evidence["captured_at"] = datetime.now(timezone.utc).isoformat()
            if evidence["cart_currency"] != _capture_currency():
                raise MarketsRefused("us_session_unproven", "nothing",
                                     f"{host}'s /cart.js reports {evidence['cart_currency'] or 'no currency'} "
                                     f"after localizing to {CAPTURE_MARKET}: the store does not confirm USD for a "
                                     f"US session, so no price it shows is a USD offer")
            # 4. Prices, inside the proven session, committed only past a passing re-check.
            planned: List[Dict[str, Any]] = []
            pending: List[Dict[str, Any]] = []
            rechecks = 0
            since = 0
            for handle in sorted(by_handle):
                code, detail = await session.json(f"/products/{quote(handle, safe='')}.js")
                if code != 200:  # 404: delisted since the base crawl. Counted, never guessed.
                    key = f"product_js_http_{code}"
                    skipped[key] = skipped.get(key, 0) + len(by_handle[handle])
                    continue
                pending.extend(_plan_product(host, handle, detail, by_handle[handle], evidence, skipped))
                since += 1
                if since >= SESSION_RECHECK_EVERY:
                    rechecks += 1
                    if await session.cart_currency() != _capture_currency():
                        raise MarketsRefused("us_session_lost", "failed",
                                             f"{host}'s /cart.js stopped reporting USD mid-capture (after "
                                             f"{rechecks} re-checks): every price read since the last proof is void")
                    planned.extend(pending)
                    pending, since = [], 0
            if pending:
                rechecks += 1
                if await session.cart_currency() != _capture_currency():
                    raise MarketsRefused("us_session_lost", "failed",
                                         f"{host}'s /cart.js stopped reporting USD at the final re-check: "
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


async def republish(content_keys: List[str], *, db: Any) -> List[str]:
    """Rebuild each touched content_key's PDP view and recompute its serving eligibility (the capture
    script's apply step). Returns the keys that failed, for the readback to name."""
    from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key
    from services.index_pipeline_state_service import recompute_serving_eligibility

    failed = []
    for ck in content_keys:
        try:
            await refresh_agent_pdp_view_for_content_key(ck, refresh_source=SOURCE_SYSTEM, db=db)
            await recompute_serving_eligibility(ck, reason=SOURCE_SYSTEM, db=db)
        except Exception:  # noqa: BLE001 -- named in the readback, never silent
            failed.append(ck)
    return failed


async def readback(written: List[Dict[str, Any]], *, db: Any,
                   republish_failed: Optional[List[str]] = None) -> Dict[str, Any]:
    """Every sibling reported written must be live, USD and stamped US. A content_key the index still
    blocks as no_us_offer after a USD sibling landed on it is a problem (the gate reads currency, so
    this means the write is not what it claims); any other blocker is the product's own content gate
    and only noted: the capture adds offers, it never touches content."""
    if not written:
        return {"ok": False, "problems": [{"problem": "no sibling offer was written"}], "notes": [], "rows": []}
    from services.agent_decision_gates import BLOCKER_NO_US_OFFER
    rows = [dict(r) for r in await db.fetch_all(READBACK_SQL, {"offer_ids": [w["offer_id"] for w in written]})]
    by_id = {r["offer_id"]: r for r in rows}
    currency = _capture_currency()
    problems, notes = [], []
    for w in written:
        r = by_id.get(w["offer_id"])
        if r is None:
            problems.append({"offer_id": w["offer_id"], "problem": "reported written, not in catalog_offers"})
        elif not r.get("live") or r.get("currency") != currency or str(r.get("market") or "").upper() != CAPTURE_MARKET:
            problems.append({"offer_id": w["offer_id"],
                             "problem": f"live {r.get('live')}, currency {r.get('currency')}, market {r.get('market')}"})
    served = set()
    for ck in sorted({r.get("content_key") for r in rows if r.get("content_key")}):
        state = next(r for r in rows if r.get("content_key") == ck)
        if state.get("serving"):
            served.add(ck)
        elif state.get("blocker_code") == BLOCKER_NO_US_OFFER:
            problems.append({"content_key": ck, "problem": "a USD sibling landed, but the index still blocks it "
                                                           "as no_us_offer"})
        else:
            notes.append({"content_key": ck, "kind": "index_refused",
                          "note": f"not served: blocker {state.get('blocker_code') or 'unknown'}"})
    for ck in republish_failed or []:
        problems.append({"content_key": ck, "problem": "republish (PDP view / serving recompute) failed"})
    return {"ok": not problems, "problems": problems, "notes": notes, "rows": rows,
            "served_content_keys": len(served)}
