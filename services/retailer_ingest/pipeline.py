"""One stage of one (brand, retailer) ingest job: dry run, or apply + verify.

The unattended form of what an operator did by hand on 2026-09-23 (Meitu lip wave): crawl, narrow,
plan, run the guards, read every row, apply only a clean cohort through the apply gate, then read
the written rows back. Every stage writes one `retailer_ingest_runs` row, including the ones that
end early, so "why did store X not land?" is answered by the ledger, not by log archaeology.

Policy (Peng, 2026-09-23): a dry run with zero BLOCK flags is applied automatically; any BLOCK flag
holds the job until someone approves it (optionally excluding handles or accepting flag keys).
The apply stage re-crawls and RE-RUNS every check before writing, so a store that changed between
its dry run and its apply cannot slip a new row past the review.

Applies at different hosts run in parallel lanes; only the catalog write (apply_ingest_plan) is
serial, under db.retailer_ingest.catalog_write_lock. Every run records how long each phase took
(checks.timings), so the lock wait and the write can be told apart from the crawl.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db import retailer_ingest as ledger
from db.retailer_ingest import CatalogWriteLockBusy, CatalogWriteLockUnavailable
from services.retailer_ingest import detectors
from utils.logger import logger  # prod keeps this logger's output; plain module loggers' INFO is dropped

DRY_RUN = "dry_run"
APPLY = "apply"


class _Stop(Exception):
    """End this stage early with a job transition and a recorded outcome."""

    def __init__(self, outcome: str, status: str, reason: str, *, next_run_at: Optional[datetime] = None,
                 count_attempt: bool = False, checks: Optional[Dict[str, Any]] = None):
        super().__init__(reason)
        self.outcome, self.status, self.reason = outcome, status, reason
        self.next_run_at, self.count_attempt = next_run_at, count_attempt
        self.checks = checks  # what the stage had measured when it stopped, so the run records it


_OPTION_TYPES = {
    "vendors": list, "require_currency": str, "category_path": str, "only_category": str,
    "only_resolved_category": bool, "lip_title_evidence": bool, "exclude_handles": list,
    # Reviewer decision (Peng 2026-09-25): a bundle of different products the store filed on a
    # single-product shelf ("Cologne & Hand Cream Duo" under fragrance/perfume) is RE-FILED to the
    # gift-set shelf, not dropped -- shoppers look for gift sets; the harm was the shelf. These handles
    # are filed under REFILE_SETS_LEAF before any check runs.
    "refile_to_sets": list,
    "accepted_flags": list, "max_scan_products": int, "max_products": int,
    "max_pdp_identity_fetches": int, "max_pdp_inci_fetches": int, "retailer_name": str, "notes": str,
    # "storefront" (default: crawl the retailer's /products.json) or "affiliate_feed" (the network's
    # product datafeed; services/retailer_ingest/affiliate_feed.py) -- for stores that block crawlers.
    "source": str, "feed": dict,
    # Whose store this is: "retailer" (default) or "brand_official" (the brand's own storefront, the
    # ADR-001 canonical anchor). services.catalog_onboard_worker.normalize_curated_brand_payload owns
    # the allowed values and the retailer_name rule; enqueue runs that same normalization.
    "source_role": str,
    # One crawl for MANY brands at one retailer: the job's `brand` is only a label, every product keeps
    # its own vendor as its brand (no override), and `vendors` names every brand the cohort selects.
    # 2026-09-24: a (brand, store) cohort re-crawls the whole store twice (dry run + apply), so 16 brands
    # at perfumania.com were 32 full crawls of one store; as one multi_brand cohort they are 2.
    "multi_brand": bool,
    # A store too large for /products.json (> 100 pages of 250: page 101 answers HTTP 400) is crawled
    # through the named brand collections instead (/collections/<handle>/products.json). The vendor
    # filter still applies inside them, and the crawl is complete for THOSE collections, not the store.
    "collections": list,
    # multi_brand only, REQUIRED there: {vendor: canonical brand spelling} for every vendor. Without an
    # override each row keeps the STORE's spelling, and normalize_brand keeps punctuation, so "Dr. Jart+"
    # at one store and "Dr.Jart+" everywhere else become two brands (review of #2301).
    "brands": dict,
    # The buyer market this cohort's offers are priced FOR (ISO-3166-1 alpha-2; absent = DEFAULT_MARKET).
    # It decides the currency the store must prove (require_currency defaults to the market's) and what
    # the readback checks every offer is stamped. Multi-market storefronts ADR, Phase 1.
    "market": str,
}
SOURCES = ("storefront", "affiliate_feed")
DEFAULT_MARKET = "US"
#: The markets a job may name. US ONLY in this phase: every offer this lane writes is stamped
#: catalog_offers.market 'US' (catalog_enrichment_agent.ingestion) and normalize_curated_brand_payload
#: refuses any other market, so an AU or JP job today would crawl an AUD/JPY store and then have
#: nowhere truthful to put it. AU/JP become ACQUISITION markets (rows stored, not served) in the
#: multi-market storefronts ADR's Phase 2, and served markets in its Phase 3 -- each is one entry here
#: plus that phase's writer change, never this list alone.
INGEST_MARKETS = ("US",)
_ISO_ALPHA2 = re.compile(r"[A-Z]{2}")
#: Tier B (brand_official_domain_flags) matches the brand inside the store's /meta.json name after
#: collapsing both to letters and digits. A brand shorter than this collapses to a string too common to
#: be evidence ("ZA" is inside "BAZAAR"), so such a brand needs Tier A or a human, as before.
TIER_B_MIN_BRAND_CHARS = 3
#: The one shelf a reviewed set is re-filed to: canonical in both taxonomies (pivota-backend
#: category_path_aliases, PIVOTA-Agent beautyTaxonomy.js `gift_set`).
REFILE_SETS_LEAF = "beauty/sets/gift-set"
#: A reviewer's re-file writes this confidence, distinct from every other writer's value (0.3/0.7/0.78/
#: 0.8/0.82/0.85/0.9/0.95), so a stored row placed this way can be found without the ledger.
CATEGORY_CONFIDENCE_REVIEW_REFILE = 0.74
#: The flags a re-file answers: the reviewer decided the row IS a set, so "a set on a single-product
#: shelf" and "the title names another shelf" (its contents) are resolved. Every other flag still runs
#: -- on the row as the STORE filed it too, since the lip rules key on the store's shelf.
REFILE_RESOLVES_RULES = frozenset({"set_filed_as_single_product", "title_contradicts_category"})

_handle_key = ledger.handle_key
MAX_PDP_IDENTITY_FETCHES = 300
# PDP INCI enrichment in an unattended stage: the worker's default (300 fetches, no time limit) ran luxiface.com
# past the 3600 s task timeout twice (2.2 s CPU per ~2 MB page). The drain fetches at most this many by default
# (options.max_pdp_inci_fetches overrides, 0 = none, at most MAX_PDP_INCI_FETCHES) and stops at the budget.
DRAIN_PDP_INCI_FETCHES = 60
MAX_PDP_INCI_FETCHES = 300
DRAIN_PDP_INCI_BUDGET_S = 900
# The catalog write lock (db.retailer_ingest.catalog_write_lock): an apply that has crawled and checked
# waits at most WRITE_LOCK_WAIT_S for another apply's write to finish, polling every WRITE_LOCK_POLL_S.
# On timeout it writes NOTHING and goes back to apply_due, retried after WRITE_LOCK_BUSY_RETRY_S without
# spending an attempt (a busy lock is not the store's failure). The retry re-crawls and re-checks, as
# every apply does.
WRITE_LOCK_WAIT_S = 600
WRITE_LOCK_POLL_S = 5
WRITE_LOCK_BUSY_RETRY_S = 120
# The write must finish inside the execution: the drain passes the seconds left in its task (the same
# figure its lease is computed from), and the lock wait ends WRITE_MARGIN_S before that deadline. With
# less than that left, the apply writes nothing and goes back to apply_due (a fresh execution retries
# it with the whole task ahead of it) -- a write the task timeout kills is a partial cohort. A first
# value, not a measurement: tune it from checks.timings.write_s.
WRITE_MARGIN_S = 900
# Outcomes that end an apply before its write with nothing written, retried quietly. After
# WRITE_LOCK_STARVED_AFTER of them in a row the job is HELD (the held alert fires; approve re-queues it)
# with a WARNING: a lock stuck on a dead holder, or an apply whose crawl always eats its write margin,
# must reach a human instead of retrying unseen forever.
WRITE_LOCK_RETRY_OUTCOMES = ("write_lock_busy", "write_lock_unavailable")
WRITE_LOCK_STARVED_AFTER = 12
# Integer options where 0 is meaningful ("fetch none"); every other integer option must be >= 1.
_ZERO_ALLOWED_INT_OPTIONS = frozenset({"max_pdp_inci_fetches"})


def validate_options(options: Dict[str, Any]) -> Dict[str, Any]:
    """The ONE validator for a job's options, used by the enqueue script and again at execution
    (a row written by any other path is checked before it can crawl). Raises ValueError.

    `category_path` must be coarse (beauty, or one level under it): it is the fallback for every
    product the merchant type leaves unresolved, so a leaf here ("beauty/makeup/lip/lipstick")
    would file every untyped product in the cohort under that leaf with no review."""
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    unknown = set(options) - set(_OPTION_TYPES)
    if unknown:
        raise ValueError(f"unknown options {sorted(unknown)}")
    for key in [k for k, v in options.items() if v is None and k != "vendors"]:
        del options[key]  # a null option is an absent one (approve() merges lists into them)
    for key, value in options.items():
        want = _OPTION_TYPES[key]
        floor = 0 if key in _ZERO_ALLOWED_INT_OPTIONS else 1
        if want is int and (isinstance(value, bool) or not isinstance(value, int) or value < floor):
            raise ValueError(f"options.{key} must be a {'nonnegative' if floor == 0 else 'positive'} integer")
        if want is not int and not isinstance(value, want):
            raise ValueError(f"options.{key} must be {want.__name__}")
        if want is list and not all(isinstance(v, str) and v.strip() for v in value):
            raise ValueError(f"options.{key} must be a list of non-empty strings")
    if not options.get("vendors"):
        raise ValueError("options.vendors is required for a retailer cohort")
    source = options.get("source") or "storefront"
    if source not in SOURCES:
        raise ValueError(f"options.source must be one of {list(SOURCES)}")
    if options.get("multi_brand") and (options.get("source_role", "retailer") != "retailer" or source != "storefront"):
        # A brand's own store is one brand (its domain must prove it); a feed maps one retailer listing.
        raise ValueError("options.multi_brand supports only a retailer storefront cohort")
    if options.get("multi_brand") or "brands" in options:
        brands = options.get("brands")
        if not options.get("multi_brand"):
            raise ValueError("options.brands is only meaningful with options.multi_brand")
        from services.curated_brand_feed import _retailer_brand_family, _vendor_token as fold
        if not isinstance(brands, dict) or not all(isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
                                                   for k, v in brands.items()):
            raise ValueError("options.brands must map every vendor to its canonical brand spelling")
        missing = sorted({fold(v) for v in options["vendors"]} - {fold(k) for k in brands})
        extra = sorted({fold(k) for k in brands} - {fold(v) for v in options["vendors"]})
        if missing or extra:
            raise ValueError(f"options.brands must name exactly the vendors: missing {missing}, not a vendor {extra}")
        if len({fold(k) for k in brands}) != len(brands):
            raise ValueError("options.brands has two keys for the same vendor")
        # Retailer mode applies an override only to the SAME brand spelt differently (equal letters and
        # digits) or a measured family (RETAILER_BRAND_SPELLINGS). Anything else would be silently
        # ignored at crawl time -- refuse it here instead of letting the operator think it applied.
        alnum = lambda v: "".join(c for c in str(v).casefold() if c.isalnum())
        # A measured family writes ITS spelling whatever the value says, so a family vendor may only be
        # respelt into that same family (review of #2302: {"Kose": "Shiseido"} passed, then wrote "Kosé").
        ignored = sorted(k for k, v in brands.items()
                         if alnum(k) != alnum(v) and (_retailer_brand_family(alnum(k)) is None
                                                      or _retailer_brand_family(alnum(k)) != _retailer_brand_family(alnum(v))))
        if ignored:
            raise ValueError(f"options.brands can only respell a vendor (same letters and digits); "
                             f"these would be ignored: {ignored}")
    if "collections" in options:
        from services.curated_brand_feed import valid_collection_handle
        if source != "storefront" or not options["collections"] or not all(
                valid_collection_handle(h) for h in options["collections"]):
            raise ValueError("options.collections must be Shopify collection handles on a storefront cohort")
    if int(options.get("max_pdp_inci_fetches") or 0) > MAX_PDP_INCI_FETCHES:
        raise ValueError(f"options.max_pdp_inci_fetches must be at most {MAX_PDP_INCI_FETCHES}")
    overlap = sorted({_handle_key(h) for h in options.get("refile_to_sets") or []}
                     & {_handle_key(h) for h in options.get("exclude_handles") or []})
    if overlap:
        raise ValueError(f"a handle cannot be both re-filed and excluded: {overlap}")
    if int(options.get("max_pdp_identity_fetches") or 0) > MAX_PDP_IDENTITY_FETCHES:
        # Each fetch waits CRAWL_MIN_INTERVAL_SECONDS (4s): 300 is ~20 min of one stage already.
        raise ValueError(f"options.max_pdp_identity_fetches must be at most {MAX_PDP_IDENTITY_FETCHES}")
    if source == "affiliate_feed" and options.get("source_role", "retailer") != "retailer":
        # The feed mapping is retailer-shaped: a retailer-host listing clicked through a network link.
        raise ValueError("options.source = affiliate_feed supports only source_role = retailer")
    if source == "affiliate_feed":
        from services.retailer_ingest.affiliate_feed import validate_feed_options
        validate_feed_options(options.get("feed"))
    elif "feed" in options:
        raise ValueError("options.feed is only meaningful with options.source = affiliate_feed")
    path = str(options.get("category_path") or "beauty").strip().strip("/").lower()
    if not (path == "beauty" or path.startswith("beauty/")) or path.count("/") > 1:
        raise ValueError(f"options.category_path must be coarse (beauty or beauty/<area>), got {path!r}")
    if "market" in options:
        from services.region_pricing import normalize_region
        market = normalize_region(options["market"])
        if not _ISO_ALPHA2.fullmatch(market):
            raise ValueError(f"options.market must be an ISO-3166-1 alpha-2 code, got {options['market']!r}")
        if market not in INGEST_MARKETS:
            raise ValueError(f"options.market {market} is not an ingest market yet (allowed: {list(INGEST_MARKETS)}); "
                             f"AU/JP arrive with the multi-market storefronts ADR's Phase 2/3")
        options["market"] = market  # "us" and "US" are one cohort (db.retailer_ingest.scope_key agrees)
    # The currency is the MARKET's, never a second free choice: the /products.json crawl sees only the
    # store's base currency, so "base currency = the market's currency" is the one honest rule for it
    # (ADR section 3.3). Given, it must say the same; absent, it is derived.
    if "require_currency" in options and options["require_currency"] != job_currency(options):
        raise ValueError(f"options.require_currency {options['require_currency']!r} is not market "
                         f"{job_market(options)}'s currency {job_currency(options)}")
    return options


def job_market(options: Optional[Dict[str, Any]]) -> str:
    """The job's market: options.market, else DEFAULT_MARKET (every job queued before the option)."""
    from services.region_pricing import normalize_region
    return normalize_region((options or {}).get("market") or DEFAULT_MARKET)


def job_currency(options: Optional[Dict[str, Any]]) -> str:
    """The currency the job's store and offers must be in: its market's (services.region_pricing).
    validate_options refuses a require_currency that says anything else."""
    from services.region_pricing import pricing_currency_for_region
    return pricing_currency_for_region(job_market(options))


def _fold(value: Any) -> str:
    """Casefolded letters and digits only: "Bali Body US" -> "balibodyus"."""
    return "".join(c for c in str(value or "").casefold() if c.isalnum())


def storefront_tier_b(brand: str, storefront: Optional[Dict[str, Any]], market: str) -> Dict[str, Any]:
    """Tier B of the brand-official evidence ladder (multi-market storefronts ADR section 3.2): the
    store's own /meta.json names the brand, ships to the job's market, and prices in that market's
    currency. {"passed": bool, <each conjunct>: bool, + what was read}.

    CONJUNCTIVE on purpose: a name alone would pass a distributor's store that names the brand it
    resells ("Sukin Stockist"), and a US-shipping USD store alone proves nothing about whose it is.
    Measured positives (2026-09-26): "Sukin Naturals USA" (sukinnaturals.com), "Bali Body US"
    (us.balibodyco.com), "MineTan USA", "DHC Skincare" -- each USD, ships_to [US]."""
    from services.region_pricing import pricing_currency_for_region_or_none
    sf = storefront if isinstance(storefront, dict) else {}
    want = _fold(brand)
    ships = sf.get("ships_to_countries")
    expected = pricing_currency_for_region_or_none(market)
    out: Dict[str, Any] = {
        "name": sf.get("name"), "myshopify_domain": sf.get("myshopify_domain"), "currency": sf.get("currency"),
        "market": market,
        "name_contains_brand": len(want) >= TIER_B_MIN_BRAND_CHARS and want in _fold(sf.get("name")),
        "ships_to_market": isinstance(ships, list) and market in ships,
        "currency_is_market_currency": bool(expected) and sf.get("currency") == expected,
    }
    out["passed"] = bool(out["name_contains_brand"] and out["ships_to_market"] and out["currency_is_market_currency"])
    return out


def brand_official_domain_flags(domain: str, brands: List[str], *, storefront: Optional[Dict[str, Any]] = None,
                                market: str = DEFAULT_MARKET) -> List[Dict[str, Any]]:
    """The BLOCK flags of `brand_official_domain_review` (see there); enqueue calls it with no
    storefront, where only the known-retailer refusal and Tier A can decide."""
    return brand_official_domain_review(domain, brands, storefront=storefront, market=market)[0]


def brand_official_domain_review(domain: str, brands: List[str], *, storefront: Optional[Dict[str, Any]] = None,
                                 market: str = DEFAULT_MARKET) -> tuple:
    """A brand_official cohort writes CANONICAL rows: a product's key derives from (brand, product
    name) alone -- and its brand is the product's own VENDOR, not the job's `brand` -- so the same
    product at any host lands on the same key, and the upsert re-points that key's source_domain /
    canonical_url / payload at this host and labels its INCI brand-official. Nothing downstream checks
    that the host is the brand's (the legacy-listing report and the native-variant rule look only at
    ext:retailer: keys; the brand-host guard only at the same host), and a clean dry run auto-applies.
    So the host must PROVE it is the store of EVERY brand the cohort would write (`brands`: the job's
    brand and each record's):

      * a known retailer host can never be a brand's own store (not acceptable -- fix the job);
      * Tier A: the domain's name IS that brand (offer_seller_identity.brand_owns_domain, the rule
        that types an offer brand_direct) -- us.frankbody.com for "Frank Body";
      * Tier B: the store's own /meta.json (`storefront`, from the crawl) names the brand, ships to
        the job's `market` and prices in its currency (storefront_tier_b) -- sukinnaturals.com,
        "Sukin Naturals USA", for "Sukin". Peng approved it as automatic on 2026-09-26;
      * otherwise a human accepts the flag (tartecosmetics.com for "Tarte", k18hair.com for "K18"):
        held, never auto-applied. A failed Tier B yields exactly that flag, same key.

    Returns (flags, evidence): `evidence` says which tier admitted each brand (or what Tier B read
    when none did) and is recorded on the run as checks.brand_official_evidence.
    """
    from services.offer_seller_identity import brand_owns_domain, is_known_retailer

    if is_known_retailer(domain):
        # Before any tier: a retailer's /meta.json may well name the brand it sells.
        refused = {"key": "brand_official_on_a_retailer", "rule": "brand_official_on_a_retailer",
                   "severity": detectors.BLOCK, "acceptable": False,
                   "detail": f"{domain} is a known retailer; source_role brand_official would overwrite "
                             f"canonical rows of {sorted(set(brands))} with this retailer's listings"}
        return [refused], {"domain": domain, "known_retailer": True}
    flags: Dict[str, Dict[str, Any]] = {}
    evidence: Dict[str, Any] = {"domain": domain, "market": market, "brands": {}}
    for brand in brands:
        # The brand's own casefolded spelling, NOT normalize_brand: that strips every non-ASCII
        # letter, so 설화수 and 헤라 would share one key and one acceptance would pass both.
        label = " ".join(str(brand).split()).casefold()
        if brand_owns_domain(brand, domain):
            evidence["brands"].setdefault(label, {"tier": "A"})
            continue
        tier_b = storefront_tier_b(brand, storefront, market)
        if tier_b["passed"]:
            evidence["brands"].setdefault(label, {"tier": "B", **tier_b})
            continue
        evidence["brands"].setdefault(label, {"tier": None, "tier_b": tier_b})
        key = f"brand_official_domain_unproven:{domain}:{label}"
        flags.setdefault(key, {
            "key": key, "rule": "brand_official_domain_unproven", "severity": detectors.BLOCK,
            "detail": f"the domain name of {domain} is not the brand {brand!r}, and its /meta.json does not "
                      f"prove it for {market} (name {tier_b['name']!r} names the brand: "
                      f"{tier_b['name_contains_brand']}, ships to {market}: {tier_b['ships_to_market']}, "
                      f"{tier_b['currency']!r} is {market}'s currency: {tier_b['currency_is_market_currency']}); "
                      f"accept this key only if {domain} is {brand}'s own store (its rows become {brand}'s "
                      f"canonical rows)"})
    return list(flags.values()), evidence


def _feed_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    try:
        o = validate_options(dict(job.get("options") or {}))
    except ValueError as exc:
        raise _Stop("invalid_job", "failed", str(exc)) from exc
    return {
        # multi_brand: no override at all, so no product can be renamed to the job's label.
        "domain": job["domain"], "brand": None if o.get("multi_brand") else job["brand"],
        "category_path": o.get("category_path") or "beauty",
        "source_role": o.get("source_role") or "retailer", "retailer_name": o.get("retailer_name"),
        "only_vendors": list(o["vendors"]),
        # The market's currency (validate_options has refused any other require_currency), and the market
        # itself, which normalize_curated_brand_payload refuses unless it is US -- a second lock on the
        # INGEST_MARKETS allowlist, owned by the writer that stamps the offers.
        "require_currency": job_currency(o), "market": job_market(o), "emit_real_variants": True,
        "enrich_missing_gtin": True, "max_products": int(o.get("max_products") or 200),
        "max_scan_products": int(o.get("max_scan_products") or 20000),
        "max_pdp_identity_fetches": int(o.get("max_pdp_identity_fetches") or 200),
        # 0 is a real choice (no INCI fetches), so no `or` here.
        "max_pdp_inci_fetches": (DRAIN_PDP_INCI_FETCHES if o.get("max_pdp_inci_fetches") is None
                                 else int(o["max_pdp_inci_fetches"])),
        "pdp_inci_budget_s": DRAIN_PDP_INCI_BUDGET_S,
    }


def _transient(crawl: Dict[str, Any]) -> bool:
    reason = str(crawl.get("reason") or "")
    return crawl.get("status") == "failed" and bool(
        re.search(r"HTTP (?:429|5\d\d)|[Tt]imeout|TransportError|NetworkError|ConnectError|ReadError|"
                  r"WriteError|RemoteProtocolError|ProtocolError|PoolTimeout", reason))


async def _affiliate_records(job: Dict[str, Any], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The affiliate datafeed as records, reported like a complete crawl. A download that answers
    429/5xx or times out is TRANSIENT (backs off exactly like a throttled crawl); a feed or mapping
    that cannot be trusted fails the job with its reason."""
    import httpx

    from services.curated_brand_feed import CrawlIncomplete, ShopifyProductBatch
    from services.retailer_ingest.affiliate_feed import FeedError, feed_rows_to_records, fetch_feed_text, parse_feed

    feed = job["options"]["feed"]
    if feed["retailer_host"].strip().lower().removeprefix("www.") != job["domain"].strip().lower().removeprefix("www."):
        raise _Stop("invalid_job", "failed", "options.feed.retailer_host must be the job's domain")

    def incomplete(reason: str) -> CrawlIncomplete:
        return CrawlIncomplete(f"{job['domain']}: affiliate feed: {reason}", status="failed", next_page=0,
                               scanned_products=0, selected_products=0)
    # httpx exceptions can carry the request URL (and so the token) in str(): only their TYPE is recorded.
    try:
        text = await fetch_feed_text(feed, env=dict(os.environ))
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise incomplete(f"{type(exc).__name__}") from exc
    except FeedError as exc:
        if re.search(r"HTTP (?:429|5\d\d)", str(exc)):
            raise incomplete(str(exc)) from exc
        raise _Stop("feed_invalid", "failed", f"affiliate feed: {exc}") from exc
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError, ValueError) as exc:
        raise _Stop("feed_invalid", "failed", f"affiliate feed download: {type(exc).__name__}") from exc
    try:
        rows = parse_feed(text, fmt=feed["format"], json_path=feed.get("json_path"))
        records = feed_rows_to_records(rows, feed, vendors=payload["only_vendors"],
                                       category_path=payload["category_path"],
                                       currency=payload["require_currency"])
    except (FeedError, ValueError, UnicodeError) as exc:
        raise _Stop("feed_invalid", "failed", f"affiliate feed: {exc}") from exc
    batch = ShopifyProductBatch(records, scanned_products=len(rows), pages=1)
    batch.crawl_report["source"] = f"affiliate_feed:{feed['network']}"
    batch.crawl_report.update(getattr(records, "stats", None) or {})
    return batch


async def _crawl(job: Dict[str, Any], stage: str) -> List[Dict[str, Any]]:
    from services.catalog_onboard_worker import normalize_curated_brand_payload
    from services.curated_brand_feed import CrawlIncomplete, lip_title_evidence, records_for_brand
    import contextlib

    try:
        payload = normalize_curated_brand_payload(_feed_payload(job))
        if (job.get("options") or {}).get("collections"):
            payload["collection_handles"] = list(job["options"]["collections"])
        if (job.get("options") or {}).get("multi_brand"):
            payload["brand_by_vendor"] = {" ".join(k.split()).casefold(): " ".join(v.split())
                                          for k, v in job["options"]["brands"].items()}
    except ValueError as exc:  # e.g. an unknown source_role on a row written by another path
        raise _Stop("invalid_job", "failed", str(exc)) from exc
    evidence = lip_title_evidence() if (job.get("options") or {}).get("lip_title_evidence") else contextlib.nullcontext()
    try:
        with evidence:
            if (job.get("options") or {}).get("source") == "affiliate_feed":
                records = await _affiliate_records(job, payload)
            else:
                records = await records_for_brand(**{k: v for k, v in payload.items() if k != "market"})
    except CrawlIncomplete as exc:
        crawl = exc.as_dict()
        if crawl.get("status") == "capped":
            reason = str(crawl.get("reason") or "")
            advice = ("crawl the brand's collections (options.collections) -- Shopify pages stop at 100"
                      if "options.collections" in reason else
                      "raise options.max_scan_products (store scan) or options.max_products (selected rows), "
                      "whichever the reason names")
            raise _Stop("crawl_capped", "failed", f"crawl capped: {reason} -- {advice}, or cancel the job") from exc
        if _transient(crawl):
            attempts = int(job.get("attempts") or 0) + 1
            if attempts >= int(job.get("max_attempts") or 6):
                raise _Stop("crawl_throttled", "failed", f"retry budget spent: {crawl.get('reason')}",
                            count_attempt=True) from exc
            raise _Stop("crawl_throttled", "queued" if stage == DRY_RUN else "apply_due",
                        f"throttled, retry later: {crawl.get('reason')}",
                        next_run_at=ledger.backoff_until(attempts - 1), count_attempt=True) from exc
        raise _Stop("crawl_failed", "failed", f"crawl failed: {crawl.get('reason')}") from exc
    report = getattr(records, "crawl_report", None)
    if not isinstance(report, dict) or report.get("status") != "complete":
        raise _Stop("crawl_unproven", "failed", "crawl completeness was not proven")
    currency = payload["require_currency"]  # job_currency: the market's (validated in _feed_payload)
    for record in records:
        seen = (record.get("pdp") or {}).get("currency")
        if seen != currency:
            raise _Stop("currency_unproven", "failed", f"record currency {seen!r} is not {currency}")
    return records


#: Rows the category filter left out, recorded per run. Bounded so one huge store cannot bloat a run
#: row: at most LEFT_OUT_ROWS_CAP rows and LEFT_OUT_TYPES_CAP merchant types, each string at most
#: LEFT_OUT_STR_CAP chars. `count` and `by_reason` are always complete; the `*_truncated` flags say
#: when a list is not.
LEFT_OUT_ROWS_CAP = 300
LEFT_OUT_TYPES_CAP = 25
LEFT_OUT_STR_CAP = 200


def _ledger_safe(value: Any) -> Optional[str]:
    """A merchant string as Postgres jsonb will take it: no NUL (jsonb refuses \\u0000), no NaN
    (refused as a token), no lone surrogate (not encodable as UTF-8), bounded length. The run row must
    never fail to write over a product name: a failed finish_run leaves the run unfinished, and the
    next execution treats the stage as interrupted and spends an attempt on it."""
    if value is None or (isinstance(value, float) and value != value):
        return None
    text = str(value).replace("\x00", "").encode("utf-8", "replace").decode("utf-8")
    return text[:LEFT_OUT_STR_CAP]


def _left_out_summary(left_out: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Which rows a category filter left out and why: counts by reason and by the merchant's own
    product type (the usual culprit), plus the rows themselves up to LEFT_OUT_ROWS_CAP."""
    from collections import Counter
    types = Counter(_ledger_safe(e.get("merchant_product_type")) or "(none)" for e in left_out)
    return {
        "count": len(left_out),
        "by_reason": dict(Counter(e["reason"] for e in left_out)),
        "by_merchant_type": dict(types.most_common(LEFT_OUT_TYPES_CAP)),
        "merchant_types_truncated": len(types) > LEFT_OUT_TYPES_CAP,
        "rows": [{k: _ledger_safe(e.get(k)) for k in ("reason", "product_name", "category_path",
                                                      "merchant_product_type", "handle")}
                 for e in left_out[:LEFT_OUT_ROWS_CAP]],
        "rows_truncated": len(left_out) > LEFT_OUT_ROWS_CAP,
    }


def _refile_to_sets(records: List[Dict[str, Any]], handles: Any, checks: Dict[str, Any],
                    flags: List[Dict[str, Any]]) -> tuple:
    """File the reviewer-named bundles under REFILE_SETS_LEAF; returns (handles re-filed, each re-filed
    record AS THE STORE FILED IT, for the detectors). A named handle the crawl no longer carries blocks
    (like an unmatched exclusion) -- accept its key to go on."""
    import copy
    import scripts.onboard_curated_brands as cli
    wanted = {_handle_key(h) for h in (handles or []) if str(h).strip()}
    if not wanted:
        return set(), []
    matched, as_filed = set(), []
    for record in records:
        handle = cli._record_handle(record)
        pdp = record.get("pdp")
        if handle in wanted and isinstance(pdp, dict):
            as_filed.append(copy.deepcopy(record))
            pdp["category_path"] = REFILE_SETS_LEAF
            pdp["category_resolution_status"] = "resolved"
            pdp["category_confidence"] = CATEGORY_CONFIDENCE_REVIEW_REFILE
            matched.add(handle)
    checks["refiled_to_sets"] = sorted(matched)
    for handle in sorted(wanted - matched):
        flags.append({"key": f"refile_handle_unmatched:{handle}", "rule": "refile_handle_unmatched",
                      "severity": detectors.BLOCK, "handle": handle,
                      "detail": "an approved re-file to gift sets no longer matches any product"})
    return matched, as_filed


async def _check(job: Dict[str, Any], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Narrow, plan and run every check. Returns the plan plus a verdict; never writes."""
    import scripts.onboard_curated_brands as cli
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
    from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan

    o = job.get("options") or {}
    checks: Dict[str, Any] = {"crawl": getattr(records, "crawl_report", None), "selected": len(records)}
    flags: List[Dict[str, Any]] = []
    market = job_market(o)
    storefront = (checks["crawl"] or {}).get("storefront") if isinstance(checks["crawl"], dict) else None
    if isinstance(storefront, dict):
        # WHICH Shopify store this crawl read (a us.<brand> host is usually a separate store from the
        # brand's home one, not an alias) and whether it ships to the job's market. Recorded, never gated.
        ships = storefront.get("ships_to_countries")
        checks["storefront"] = {
            "name": storefront.get("name"), "myshopify_domain": storefront.get("myshopify_domain"),
            "currency": storefront.get("currency"), "market": market,
            "ships_to_count": len(ships) if isinstance(ships, list) else None,
            "ships_to_market": (market in ships) if isinstance(ships, list) else None,
        }
    if o.get("source_role") == "brand_official":
        # Every brand the cohort would write: the job's, and each record's own (its vendor's).
        written = [job["brand"]] + sorted({str((r.get("pdp") or {}).get("brand") or "") for r in records} - {""})
        domain_flags, checks["brand_official_evidence"] = brand_official_domain_review(
            job["domain"], written, storefront=storefront, market=market)
        flags.extend(domain_flags)

    excluded = {str(h).strip().strip("/").casefold() for h in (o.get("exclude_handles") or []) if str(h).strip()}
    if excluded:
        records, matched = cli._exclude_by_handle(records, excluded, domain=job["domain"])
        checks["excluded"] = sorted(matched)
        for handle in sorted(excluded - matched):
            flags.append({"key": f"exclude_handle_unmatched:{handle}", "rule": "exclude_handle_unmatched",
                          "severity": detectors.BLOCK, "handle": handle,
                          "detail": "an approved exclusion no longer matches any product"})
    refiled, refiled_as_filed = _refile_to_sets(records, o.get("refile_to_sets"), checks, flags)
    if o.get("only_category") or o.get("only_resolved_category"):
        selected = len(records)
        # A reviewer's re-file is kept whatever the cohort's category filter: a lip pass that re-files a
        # lip duo must not drop it as "outside beauty/makeup/lip" and still report it re-filed.
        kept_refiled = [r for r in records if cli._record_handle(r) in refiled]
        records, left_out = cli._partition_by_category(
            [r for r in records if cli._record_handle(r) not in refiled], prefix=o.get("only_category"))
        if kept_refiled:
            inside, _ = cli._partition_by_category(kept_refiled, prefix=o.get("only_category"))
            checks["refiled_kept_outside_filter"] = sorted(
                {cli._record_handle(r) for r in kept_refiled} - {cli._record_handle(r) for r in inside})
            records = records + kept_refiled
        cli._print_category_filter(selected, records, left_out, domain=job["domain"],
                                   want=cli._normalize_category_prefix(o.get("only_category")))
        checks["left_out"] = _left_out_summary(left_out)
        if not records:
            checks["kept"] = 0
            raise _Stop("nothing_to_ingest", "nothing",
                        f"no product resolves under {o.get('only_category') or 'a resolved category'}",
                        checks=checks)
    checks["kept"] = len(records)

    plan = ingest_validated_jsonl(records)
    inspection = inspect_primary_plan(plan)
    checks["plan"] = {k: inspection.get(k) for k in ("status", "reasons", "planned", "unresolved_category_count")}
    if inspection.get("reasons"):
        flags.append({"key": "plan_not_ready", "rule": "plan_not_ready", "severity": detectors.BLOCK, "acceptable": False,
                      "detail": f"primary plan {inspection.get('status')}: {inspection.get('reasons')}"})

    legacy = await cli._legacy_listing_report(plan, check=True)
    guard = await cli._brand_host_guard_report(plan, check=True)
    checks["legacy_listings"] = {k: legacy.get(k) for k in ("status", "conflict_count", "planned_listings")}
    checks["brand_host_guard"] = {k: guard.get(k) for k in ("status", "rows_at_risk", "planned_groups")}
    for name, report in (("legacy_listings", legacy), ("brand_host_guard", guard)):
        if report.get("status") in ("conflicts", "error"):
            flags.append({"key": name, "rule": name, "severity": detectors.BLOCK, "acceptable": False,
                          "detail": json.dumps(report, default=str)[:600]})

    row_flags = detectors.detect(records)
    if refiled:
        # Judge each re-filed row on BOTH shelves: the rules keyed on the store's shelf (lip size, lip
        # copy, the lip title door) cannot fire on the gift-set shelf, and a re-file must not hide them.
        seen = {f["key"] for f in row_flags}
        row_flags += [f for f in detectors.detect(refiled_as_filed) if f["key"] not in seen]
        answered = [f for f in row_flags if f.get("handle") in refiled and f.get("rule") in REFILE_RESOLVES_RULES]
        row_flags = [f for f in row_flags if f not in answered]
        checks["refile_resolved_flags"] = sorted(f["key"] for f in answered)
    # Name each row's brand on its flag: in a multi_brand cohort the reviewer must see whose row it is.
    brand_of = {detectors._handle(r): (r.get("pdp") or {}).get("brand") for r in records}
    for f in row_flags:
        if f.get("handle") and not f.get("brand"):
            f["brand"] = brand_of.get(f["handle"])
    flags.extend(row_flags)
    blocking = detectors.blocking(flags, accepted=o.get("accepted_flags") or [])
    checks["flags"] = {"block": len([f for f in flags if f["severity"] == detectors.BLOCK]),
                       "info": len([f for f in flags if f["severity"] == detectors.INFO]),
                       "blocking_after_approval": len(blocking)}
    return {"plan": plan, "inspection": inspection, "checks": checks, "flags": flags, "blocking": blocking}


#: The lifecycle stages backend global recall admits (services/pivot_query_service.py: `IN (...) OR
#: pdp_lifecycle_stage IS NULL`; the filter is skipped for merchant-scoped lanes and for
#: require_signature/canonical_entities_only). NOT the agent door's rule: the gateway gates on
#: serving_eligible only. Used to annotate a readback, never to fail one.
BACKEND_RECALL_LIFECYCLE_STAGES = ("validated", "published")

#: index_pipeline_state blocker codes that mean the index REFUSED a row on its content (thin copy, no image,
#: low quality score, not a core product) -- services/index_pipeline_state_service.py. A readback notes these;
#: any other blocker on an applied row fails the job.
INDEX_CONTENT_REFUSALS = ("low_quality", "no_image", "short_description", "non_core_product")


async def _readback(product_keys: List[str], currency: str, db: Any,
                    planned_images: Optional[Dict[str, bool]] = None, *, market: str = DEFAULT_MARKET) -> Dict[str, Any]:
    """Did what the gate says landed actually land servable? One row per applied product.

    Every live offer of the product must be in the job's currency AND stamped the job's market
    (catalog_offers.market): currency = market, always (multi-market storefronts ADR section 3.3).
    SG rows are the one deliberate exception in the catalog -- stored market 'US', currency 'SGD',
    because external_product_seeds.market is a hard serving partition (curated_brand_feed.
    fetch_shopify_shop_locale) -- and this lane never writes them: INGEST_MARKETS is US only and the
    crawl refuses a non-USD store for a US job."""
    if not product_keys:
        return {"ok": False, "reason": "no product keys to read back", "notes": [], "rows": []}
    from services.index_pipeline_state_service import _RESOLVED_PDP_SCOPES
    from services.priced_offer_sql import priced_offer_exists_sql
    rows = await db.fetch_all(
        """
        SELECT p.product_key, p.category_path, coalesce(ips.serving_eligible, false) AS serving,
               ips.pipeline_stage, ips.blocker_code, ips.blocker_detail, p.pdp_lifecycle_stage AS lifecycle,
               -- Per-PRODUCT evidence, never the IPS flags: an IPS row is per content_key, and when products
               -- share one its flags are the best-ranked sibling's (index_pipeline_state_service warns
               -- callers off exactly this), so a priced sibling could mask this row's lost price write.
               """ + priced_offer_exists_sql("p.product_key") + """ AS row_priced,
               (coalesce(p.image_url, '') <> '') AS row_image,
               (p.pdp_scope = ANY(:resolved_scopes) OR EXISTS (
                   SELECT 1 FROM product_group_members pgm WHERE pgm.merchant_id = p.merchant_id
                     AND pgm.platform = p.platform AND pgm.platform_product_id = p.source_product_id)) AS row_identity,
               (SELECT count(*) FROM catalog_offers o WHERE o.product_key = p.product_key
                  AND o.suppressed_at IS NULL) AS offers,
               (SELECT count(*) FROM catalog_offers o WHERE o.product_key = p.product_key
                  AND o.suppressed_at IS NULL AND o.currency = :currency) AS offers_in_currency,
               (SELECT count(*) FROM catalog_offers o WHERE o.product_key = p.product_key
                  AND o.suppressed_at IS NULL AND upper(o.market) = :market) AS offers_in_market
        FROM catalog_products p LEFT JOIN index_pipeline_state ips USING (content_key)
        WHERE p.product_key = ANY(:keys)
        """,
        {"keys": list(product_keys), "currency": currency, "market": market,
         "resolved_scopes": sorted(_RESOLVED_PDP_SCOPES)},
    )
    out = [dict(r) for r in rows]
    problems, notes = [], []
    found = {r["product_key"] for r in out}
    for key in product_keys:
        if key not in found:
            problems.append({"product_key": key, "problem": "not in catalog_products"})
    for r in out:
        if not r["category_path"]:
            problems.append({"product_key": r["product_key"], "problem": "no category_path"})
        if not r["serving"]:
            # The index's own content gate refusing a thin row (measured 2026-09-24: a gift-with-purchase
            # mini at westman-atelier.com, "Blush Stick", content_quality_score 71.2 < 71.4) is the
            # system working, not a lost write: note it, keep the store applied. Every other reason --
            # suppressed, not live, no seed/extraction, unscored, no price, unresolved identity -- can
            # mean the write itself went wrong, and still fails the job.
            # The index records only the FIRST failed check, and low_quality / non_core_product sit ahead of
            # no_price and entity_unresolved: a content code can mask a lost write. So a refusal is a note only
            # when THIS product's own row shows a priced offer and a resolved identity, and an image this plan
            # wrote actually landed (apply overwrites image_url, so a missing one is ours).
            wrote_image = bool((planned_images or {}).get(r["product_key"]))
            if (r.get("blocker_code") in INDEX_CONTENT_REFUSALS and r.get("row_priced") and r.get("row_identity")
                    and not (wrote_image and not r.get("row_image"))):
                notes.append({"product_key": r["product_key"], "kind": "index_refused",
                              "note": f"not served: the index refused it on content ({r.get('blocker_code')}"
                                      f"{': ' + str(r.get('blocker_detail'))[:160] if r.get('blocker_detail') else ''})"})
            else:
                problems.append({"product_key": r["product_key"],
                                 "problem": f"not serving-eligible (blocker {r.get('blocker_code') or 'unknown'})"})
        # Recorded, never a failure: the agent door (gateway) serves on serving-eligibility alone, while
        # backend global recall admits only BACKEND_RECALL_LIFECYCLE_STAGES (or NULL). Measured
        # 2026-09-24: 14 of 28 O HUI rows at buybeautykorea.com landed `candidate` (no taxonomy signal:
        # the store has no tags) and the agent door still returned them. The run says which rows
        # backend recall will not see.
        if r.get("lifecycle") is not None and r.get("lifecycle") not in BACKEND_RECALL_LIFECYCLE_STAGES:
            notes.append({"product_key": r["product_key"], "kind": "outside_backend_recall",
                          "note": f"outside backend global recall: pdp_lifecycle_stage {r.get('lifecycle')!r}"})
        if not r["offers"] or r["offers_in_currency"] != r["offers"]:
            problems.append({"product_key": r["product_key"],
                             "problem": f"offers {r['offers']}, in {currency}: {r['offers_in_currency']}"})
        if r["offers_in_market"] != r["offers"]:
            problems.append({"product_key": r["product_key"],
                             "problem": f"offers {r['offers']}, stamped market {market}: {r['offers_in_market']}"})
    return {"ok": not problems, "problems": problems, "notes": notes, "rows": out}


async def _move(job: Dict[str, Any], *, db: Any, **fields: Any) -> bool:
    """Every transition is conditional on the status this stage claimed: a job an operator
    cancelled while its crawl ran keeps its cancellation (the stage's verdict is recorded on the
    run only)."""
    moved = await ledger.transition(job["id"], expected_status=job["status"], db=db, **fields)
    if not moved:
        job["superseded"] = True
    return moved


async def run_stage(job: Dict[str, Any], *, db: Any, time_left_s: Optional[float] = None) -> Dict[str, Any]:
    """Run the job's due stage and record it. Returns {job_id, stage, outcome, status, reason}
    (+ superseded=True when an operator changed the job while the stage ran).

    `time_left_s`: seconds left in the calling execution's task when the job was claimed (None = no
    deadline). An apply starts its catalog write only with WRITE_MARGIN_S of it still left."""
    deadline = None if time_left_s is None else time.monotonic() + float(time_left_s)
    out = await _run_stage(job, db=db, deadline=deadline)
    if job.get("superseded"):
        out = {**out, "superseded": True}
    return out


@contextlib.contextmanager
def _timed(timings: Dict[str, float], key: str):
    """Record the seconds the block took under `key`, also when it raised."""
    started = time.monotonic()
    try:
        yield
    finally:
        timings[key] = round(time.monotonic() - started, 3)


def _with_timings(checks: Any, timings: Dict[str, float], stage: str) -> Any:
    """The run's checks with the phase timings in them (a stage that stopped before any check
    returned records the timings alone). An apply that ends before its write keeps the
    catalog_write=not_started evidence start_run wrote, instead of finish_run erasing it."""
    if checks is None and (timings or stage == APPLY):
        checks = {"timings": timings}
    if isinstance(checks, dict):
        checks.setdefault("timings", timings)
        if stage == APPLY:
            checks.setdefault("catalog_write", ledger.CATALOG_WRITE_NOT_STARTED)
    return checks


async def _run_stage(job: Dict[str, Any], *, db: Any, deadline: Optional[float] = None) -> Dict[str, Any]:
    stage = APPLY if job["status"] == "apply_due" else DRY_RUN
    # The previous execution was killed mid-stage (task timeout, OOM): its run never finished.
    interrupted = await ledger.unfinished_run(job["id"], db=db)
    if interrupted:
        note = "execution ended before the stage finished (task timeout or OOM)"
        # An apply whose run was never marked write-started died in its crawl, checks or lock wait:
        # nothing was written. Only the explicit "not_started" marker proves that; a run without any
        # marker (started by an older image) may have been writing.
        write_may_have_started = (interrupted["stage"] == APPLY
                                  and interrupted.get("catalog_write") != ledger.CATALOG_WRITE_NOT_STARTED)
        if interrupted["stage"] == APPLY and not write_may_have_started:
            note += "; the catalog write had not started, nothing was written"
        await ledger.finish_run(interrupted["id"], outcome="interrupted", error=note, db=db)
        if write_may_have_started:
            # It may have written part of the cohort. Never re-apply blindly.
            reason = f"the previous apply was interrupted and may be partial; review before re-queueing"
            await _move(job, status="failed", run_id=interrupted["id"], reason=reason, db=db)
            return {"job_id": job["id"], "stage": APPLY, "outcome": "interrupted", "status": "failed",
                    "reason": reason}
        attempts = int(job.get("attempts") or 0) + 1
        if attempts >= int(job.get("max_attempts") or 6):
            await _move(job, status="failed", run_id=interrupted["id"], count_attempt=True,
                        reason=f"retry budget spent: {note}", db=db)
            return {"job_id": job["id"], "stage": interrupted["stage"], "outcome": "interrupted", "status": "failed"}
        await _move(job, status=job["status"], run_id=interrupted["id"], count_attempt=True,
                    next_run_at=ledger.backoff_until(attempts - 1), reason=f"retry later: {note}", db=db)
        return {"job_id": job["id"], "stage": interrupted["stage"], "outcome": "interrupted", "status": job["status"]}
    run_id = await ledger.start_run(job_id=job["id"], stage=stage,
                                    image_sha=os.getenv("PIVOTA_COMMIT_SHA") or os.getenv("IMAGE_SHA"),
                                    execution=os.getenv("CLOUD_RUN_EXECUTION"), db=db)
    result: Dict[str, Any] = {}
    # Seconds per phase: crawl_s, check_s (every stage), write_lock_wait_s, write_s, readback_s (apply).
    timings: Dict[str, float] = {}
    try:
        with _timed(timings, "crawl_s"):
            records = await _crawl(job, stage)
        with _timed(timings, "check_s"):
            result = await _check(job, records)
        result["checks"]["timings"] = timings
        if stage == APPLY:
            result["checks"].setdefault("catalog_write", ledger.CATALOG_WRITE_NOT_STARTED)
        summary = {"crawl": result["checks"].get("crawl"), "plan": result["checks"].get("plan"),
                   "checks": result["checks"], "flags": result["flags"]}
        if result["blocking"]:
            await ledger.finish_run(run_id, outcome="held", **summary, db=db)
            await _move(job, status="held", run_id=run_id,
                                    reason=f"{len(result['blocking'])} blocking flag(s): "
                                           + ", ".join(sorted({f['rule'] for f in result['blocking']})),
                                    db=db)
            return {"job_id": job["id"], "stage": stage, "outcome": "held", "status": "held"}
        if stage == DRY_RUN:
            await ledger.finish_run(run_id, outcome="clean", **summary, db=db)
            await _move(job, status="apply_due", run_id=run_id,
                                    reason="dry run clean; apply due", next_run_at=datetime.now(timezone.utc),
                                    db=db)
            return {"job_id": job["id"], "stage": stage, "outcome": "clean", "status": "apply_due"}
        return await _apply(job, run_id, result, summary, timings, db=db, deadline=deadline)
    except _Stop as stop:
        await ledger.finish_run(run_id, outcome=stop.outcome,
                                checks=_with_timings(stop.checks or result.get("checks"), timings, stage),
                                flags=result.get("flags"), error=stop.reason, db=db)
        await _move(job, status=stop.status, run_id=run_id, reason=stop.reason,
                                next_run_at=stop.next_run_at, count_attempt=stop.count_attempt, db=db)
        return {"job_id": job["id"], "stage": stage, "outcome": stop.outcome, "status": stop.status,
                "reason": stop.reason}
    except Exception as exc:  # noqa: BLE001 -- recorded, then re-raised so the job execution fails loudly
        reason = f"{type(exc).__name__}: {exc}"
        await ledger.finish_run(run_id, outcome="error", checks=_with_timings(result.get("checks"), timings, stage),
                                error=reason, db=db)
        await _move(job, status="failed", run_id=run_id, reason=reason[:2000], db=db)
        raise


async def _write_not_started(job: Dict[str, Any], outcome: str, why: str, *, db: Any) -> _Stop:
    """The stop for an apply that ends before its catalog write, nothing written: back to apply_due
    shortly, no attempt spent -- unless this is the WRITE_LOCK_STARVED_AFTER-th such end in a row, which
    holds the job for a human (and says so at WARNING)."""
    streak = await ledger.consecutive_outcomes(job["id"], WRITE_LOCK_RETRY_OUTCOMES,
                                               limit=WRITE_LOCK_STARVED_AFTER, db=db) + 1
    if streak >= WRITE_LOCK_STARVED_AFTER:
        logger.warning("retailer_ingest: job %s (%s) ended %d applies in a row before its catalog write "
                       "(last: %s); held for review, nothing written", job["id"], job.get("domain"), streak, why)
        return _Stop("write_lock_starved", "held",
                     f"{streak} applies in a row ended before the catalog write, nothing written (last: {why}); "
                     f"check for a stuck catalog write lock or a crawl that leaves no time to write, then approve "
                     f"to retry", count_attempt=False)
    return _Stop(outcome, "apply_due", f"{why}; nothing written, retry in {WRITE_LOCK_BUSY_RETRY_S}s",
                 next_run_at=datetime.now(timezone.utc) + timedelta(seconds=WRITE_LOCK_BUSY_RETRY_S),
                 count_attempt=False)


async def _apply(job: Dict[str, Any], run_id: str, result: Dict[str, Any], summary: Dict[str, Any],
                 timings: Dict[str, float], *, db: Any, deadline: Optional[float] = None) -> Dict[str, Any]:
    from scripts.curated_apply_gate import evaluate_apply_log
    from services.catalog_enrichment_agent.apply import apply_ingest_plan
    from services.catalog_enrichment_agent.primary_ingestion import require_primary_apply, require_primary_plan

    plan = result["plan"]
    preflight = require_primary_plan(plan)
    waiting = time.monotonic()
    # Wait no longer than leaves WRITE_MARGIN_S of the task for the write itself.
    wait_s = float(WRITE_LOCK_WAIT_S)
    if deadline is not None:
        wait_s = min(wait_s, deadline - waiting - WRITE_MARGIN_S)
    if wait_s <= 0:
        timings["write_lock_wait_s"] = 0.0
        raise await _write_not_started(
            job, "write_lock_busy",
            f"no time left in this execution for the catalog write ({deadline - waiting:.0f}s left, "
            f"{WRITE_MARGIN_S}s needed)", db=db)
    try:
        # The ONLY catalog write of the stage, and the only part serialized across lanes: the crawl and
        # checks above ran unlocked. The lock is released when this block exits, however it exits.
        async with ledger.catalog_write_lock(wait_s=wait_s, poll_s=WRITE_LOCK_POLL_S) as waited:
            timings["write_lock_wait_s"] = round(waited, 3)
            # Durable BEFORE the first catalog write: an execution killed from here on is "may be
            # partial"; one killed before it (crawl, checks, lock wait) is retried, nothing written.
            await ledger.mark_write_started(run_id, db=db)
            result["checks"]["catalog_write"] = ledger.CATALOG_WRITE_STARTED  # kept when the run finishes
            with _timed(timings, "write_s"):
                counts = await apply_ingest_plan(plan, batch_label=f"retailer_ingest:{job['id']}", db=db,
                                                 primary_readiness=True, market=job_market(job.get("options")))
        report = require_primary_apply(preflight, counts)
    except CatalogWriteLockBusy as busy:
        timings["write_lock_wait_s"] = round(time.monotonic() - waiting, 3)
        raise await _write_not_started(
            job, "write_lock_busy",
            f"catalog write lock busy for {busy.waited_s:.0f}s (another apply is writing)", db=db) from busy
    except CatalogWriteLockUnavailable as unavailable:
        # Opening the lock connection or a try-lock failed: the lock was never held, nothing written.
        timings["write_lock_wait_s"] = round(time.monotonic() - waiting, 3)
        raise await _write_not_started(
            job, "write_lock_unavailable",
            f"catalog write lock unavailable ({unavailable.error_type})", db=db) from unavailable
    except ValueError as exc:
        # A refused apply may have written part of the cohort: failed, never retried blindly.
        report = getattr(exc, "report", None) or getattr(getattr(exc, "__cause__", None), "report", None)
        await ledger.finish_run(run_id, outcome="apply_refused", **summary, applied=report,
                                error=str(exc), db=db)
        await _move(job, status="failed", run_id=run_id,
                                reason=f"apply refused (may be partial): {str(exc)[:600]}", db=db)
        return {"job_id": job["id"], "stage": APPLY, "outcome": "apply_refused", "status": "failed"}

    # The same gate an operator ran on the log, fed the same line the CLI prints.
    gate = evaluate_apply_log("primary ingestion: " + json.dumps(report, default=str) + "\nJOB=pipeline RC=0",
                              domain=job["domain"])
    currency, market = job_currency(job.get("options")), job_market(job.get("options"))
    with _timed(timings, "readback_s"):
        readback = await _readback(gate.get("product_keys") or [], currency, db, market=market,
                                   planned_images={p.get("product_key"): bool(p.get("image_url"))
                                                   for p in plan.get("pdps") or []})
    ok = bool(gate.get("ok")) and readback["ok"]
    outcome = "applied" if ok else ("gate_failed" if not gate.get("ok") else "readback_failed")
    await ledger.finish_run(run_id, outcome=outcome, **summary, applied={"gate": gate}, readback=readback,
                            db=db)
    kinds = [n.get("kind") for n in readback.get("notes") or []]
    said = [f"{kinds.count(k)} {label}" for k, label in (("outside_backend_recall", "row(s) outside backend global recall"),
                                                          ("index_refused", "row(s) refused by the index content gate"))
            if kinds.count(k)]
    reason = ("; ".join(["applied and verified", *said]) if ok else
              f"{outcome}: gate {gate.get('reasons')}; readback {readback.get('problems')}")
    await _move(job, status="done" if ok else "failed", run_id=run_id, reason=reason, db=db)
    return {"job_id": job["id"], "stage": APPLY, "outcome": outcome, "status": "done" if ok else "failed"}
