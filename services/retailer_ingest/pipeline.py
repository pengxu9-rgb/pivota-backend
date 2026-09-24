"""One stage of one (brand, retailer) ingest job: dry run, or apply + verify.

The unattended form of what an operator did by hand on 2026-09-23 (Meitu lip wave): crawl, narrow,
plan, run the guards, read every row, apply only a clean cohort through the apply gate, then read
the written rows back. Every stage writes one `retailer_ingest_runs` row, including the ones that
end early, so "why did store X not land?" is answered by the ledger, not by log archaeology.

Policy (Peng, 2026-09-23): a dry run with zero BLOCK flags is applied automatically; any BLOCK flag
holds the job until someone approves it (optionally excluding handles or accepting flag keys).
The apply stage re-crawls and RE-RUNS every check before writing, so a store that changed between
its dry run and its apply cannot slip a new row past the review.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db import retailer_ingest as ledger
from services.retailer_ingest import detectors

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
    "accepted_flags": list, "max_scan_products": int, "max_products": int,
    "max_pdp_identity_fetches": int, "retailer_name": str, "notes": str,
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
}
SOURCES = ("storefront", "affiliate_feed")
MAX_PDP_IDENTITY_FETCHES = 300


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
        if want is int and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(f"options.{key} must be a positive integer")
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
    return options


def brand_official_domain_flags(domain: str, brands: List[str]) -> List[Dict[str, Any]]:
    """A brand_official cohort writes CANONICAL rows: a product's key derives from (brand, product
    name) alone -- and its brand is the product's own VENDOR, not the job's `brand` -- so the same
    product at any host lands on the same key, and the upsert re-points that key's source_domain /
    canonical_url / payload at this host and labels its INCI brand-official. Nothing downstream checks
    that the host is the brand's (the legacy-listing report and the native-variant rule look only at
    ext:retailer: keys; the brand-host guard only at the same host), and a clean dry run auto-applies.
    So the host must PROVE it is the store of EVERY brand the cohort would write (`brands`: the job's
    brand and each record's):

      * a known retailer host can never be a brand's own store (not acceptable -- fix the job);
      * otherwise the domain's name must BE that brand (offer_seller_identity.brand_owns_domain, the
        rule that types an offer brand_direct), or a human accepts the flag (tartecosmetics.com for
        "Tarte", k18hair.com for "K18"): held, never auto-applied.
    """
    from services.offer_seller_identity import brand_owns_domain, is_known_retailer

    if is_known_retailer(domain):
        return [{"key": "brand_official_on_a_retailer", "rule": "brand_official_on_a_retailer",
                 "severity": detectors.BLOCK, "acceptable": False,
                 "detail": f"{domain} is a known retailer; source_role brand_official would overwrite "
                           f"canonical rows of {sorted(set(brands))} with this retailer's listings"}]
    flags: Dict[str, Dict[str, Any]] = {}
    for brand in brands:
        if brand_owns_domain(brand, domain):
            continue
        # The brand's own casefolded spelling, NOT normalize_brand: that strips every non-ASCII
        # letter, so 설화수 and 헤라 would share one key and one acceptance would pass both.
        key = f"brand_official_domain_unproven:{domain}:{' '.join(str(brand).split()).casefold()}"
        flags.setdefault(key, {
            "key": key, "rule": "brand_official_domain_unproven", "severity": detectors.BLOCK,
            "detail": f"the domain name of {domain} is not the brand {brand!r}; accept this key only if "
                      f"{domain} is {brand}'s own store (its rows become {brand}'s canonical rows)"})
    return list(flags.values())


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
        "require_currency": o.get("require_currency") or "USD", "emit_real_variants": True,
        "enrich_missing_gtin": True, "max_products": int(o.get("max_products") or 200),
        "max_scan_products": int(o.get("max_scan_products") or 20000),
        "max_pdp_identity_fetches": int(o.get("max_pdp_identity_fetches") or 200),
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
            raise _Stop("crawl_capped", "failed", f"crawl capped: {crawl.get('reason')} -- raise "
                        "options.max_scan_products (store scan) or options.max_products (selected rows), "
                        "whichever the reason names, or cancel the job") from exc
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
    currency = (job.get("options") or {}).get("require_currency") or "USD"
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


async def _check(job: Dict[str, Any], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Narrow, plan and run every check. Returns the plan plus a verdict; never writes."""
    import scripts.onboard_curated_brands as cli
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
    from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan

    o = job.get("options") or {}
    checks: Dict[str, Any] = {"crawl": getattr(records, "crawl_report", None), "selected": len(records)}
    flags: List[Dict[str, Any]] = []
    if o.get("source_role") == "brand_official":
        # Every brand the cohort would write: the job's, and each record's own (its vendor's).
        written = [job["brand"]] + sorted({str((r.get("pdp") or {}).get("brand") or "") for r in records} - {""})
        flags.extend(brand_official_domain_flags(job["domain"], written))

    excluded = {str(h).strip().strip("/").casefold() for h in (o.get("exclude_handles") or []) if str(h).strip()}
    if excluded:
        records, matched = cli._exclude_by_handle(records, excluded, domain=job["domain"])
        checks["excluded"] = sorted(matched)
        for handle in sorted(excluded - matched):
            flags.append({"key": f"exclude_handle_unmatched:{handle}", "rule": "exclude_handle_unmatched",
                          "severity": detectors.BLOCK, "handle": handle,
                          "detail": "an approved exclusion no longer matches any product"})
    if o.get("only_category") or o.get("only_resolved_category"):
        selected = len(records)
        records, left_out = cli._partition_by_category(records, prefix=o.get("only_category"))
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


async def _readback(product_keys: List[str], currency: str, db: Any) -> Dict[str, Any]:
    """Did what the gate says landed actually land servable? One row per applied product."""
    if not product_keys:
        return {"ok": False, "reason": "no product keys to read back", "notes": [], "rows": []}
    rows = await db.fetch_all(
        """
        SELECT p.product_key, p.category_path, coalesce(ips.serving_eligible, false) AS serving,
               ips.pipeline_stage, p.pdp_lifecycle_stage AS lifecycle,
               (SELECT count(*) FROM catalog_offers o WHERE o.product_key = p.product_key
                  AND o.suppressed_at IS NULL) AS offers,
               (SELECT count(*) FROM catalog_offers o WHERE o.product_key = p.product_key
                  AND o.suppressed_at IS NULL AND o.currency = :currency) AS offers_in_currency
        FROM catalog_products p LEFT JOIN index_pipeline_state ips USING (content_key)
        WHERE p.product_key = ANY(:keys)
        """,
        {"keys": list(product_keys), "currency": currency},
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
            problems.append({"product_key": r["product_key"], "problem": "not serving-eligible"})
        # Recorded, never a failure: the agent door (gateway) serves on serving-eligibility alone, while
        # backend global recall admits only BACKEND_RECALL_LIFECYCLE_STAGES (or NULL). Measured
        # 2026-09-24: 14 of 28 O HUI rows at buybeautykorea.com landed `candidate` (no taxonomy signal:
        # the store has no tags) and the agent door still returned them. The run says which rows
        # backend recall will not see.
        if r.get("lifecycle") is not None and r.get("lifecycle") not in BACKEND_RECALL_LIFECYCLE_STAGES:
            notes.append({"product_key": r["product_key"],
                          "note": f"outside backend global recall: pdp_lifecycle_stage {r.get('lifecycle')!r}"})
        if not r["offers"] or r["offers_in_currency"] != r["offers"]:
            problems.append({"product_key": r["product_key"],
                             "problem": f"offers {r['offers']}, in {currency}: {r['offers_in_currency']}"})
    return {"ok": not problems, "problems": problems, "notes": notes, "rows": out}


async def _move(job: Dict[str, Any], *, db: Any, **fields: Any) -> bool:
    """Every transition is conditional on the status this stage claimed: a job an operator
    cancelled while its crawl ran keeps its cancellation (the stage's verdict is recorded on the
    run only)."""
    moved = await ledger.transition(job["id"], expected_status=job["status"], db=db, **fields)
    if not moved:
        job["superseded"] = True
    return moved


async def run_stage(job: Dict[str, Any], *, db: Any) -> Dict[str, Any]:
    """Run the job's due stage and record it. Returns {job_id, stage, outcome, status, reason}
    (+ superseded=True when an operator changed the job while the stage ran)."""
    out = await _run_stage(job, db=db)
    if job.get("superseded"):
        out = {**out, "superseded": True}
    return out


async def _run_stage(job: Dict[str, Any], *, db: Any) -> Dict[str, Any]:
    stage = APPLY if job["status"] == "apply_due" else DRY_RUN
    # The previous execution was killed mid-stage (task timeout, OOM): its run never finished.
    interrupted = await ledger.unfinished_run(job["id"], db=db)
    if interrupted:
        note = "execution ended before the stage finished (task timeout or OOM)"
        await ledger.finish_run(interrupted["id"], outcome="interrupted", error=note, db=db)
        if interrupted["stage"] == APPLY:
            # It may have written part of the cohort. Never re-apply blindly.
            reason = f"the previous apply was interrupted and may be partial; review before re-queueing"
            await _move(job, status="failed", run_id=interrupted["id"], reason=reason, db=db)
            return {"job_id": job["id"], "stage": APPLY, "outcome": "interrupted", "status": "failed",
                    "reason": reason}
        attempts = int(job.get("attempts") or 0) + 1
        if attempts >= int(job.get("max_attempts") or 6):
            await _move(job, status="failed", run_id=interrupted["id"], count_attempt=True,
                        reason=f"retry budget spent: {note}", db=db)
            return {"job_id": job["id"], "stage": DRY_RUN, "outcome": "interrupted", "status": "failed"}
        await _move(job, status=job["status"], run_id=interrupted["id"], count_attempt=True,
                    next_run_at=ledger.backoff_until(attempts - 1), reason=f"retry later: {note}", db=db)
        return {"job_id": job["id"], "stage": DRY_RUN, "outcome": "interrupted", "status": job["status"]}
    run_id = await ledger.start_run(job_id=job["id"], stage=stage,
                                    image_sha=os.getenv("PIVOTA_COMMIT_SHA") or os.getenv("IMAGE_SHA"),
                                    execution=os.getenv("CLOUD_RUN_EXECUTION"), db=db)
    result: Dict[str, Any] = {}
    try:
        records = await _crawl(job, stage)
        result = await _check(job, records)
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
        return await _apply(job, run_id, result, summary, db=db)
    except _Stop as stop:
        await ledger.finish_run(run_id, outcome=stop.outcome, checks=stop.checks or result.get("checks"),
                                flags=result.get("flags"), error=stop.reason, db=db)
        await _move(job, status=stop.status, run_id=run_id, reason=stop.reason,
                                next_run_at=stop.next_run_at, count_attempt=stop.count_attempt, db=db)
        return {"job_id": job["id"], "stage": stage, "outcome": stop.outcome, "status": stop.status,
                "reason": stop.reason}
    except Exception as exc:  # noqa: BLE001 -- recorded, then re-raised so the job execution fails loudly
        reason = f"{type(exc).__name__}: {exc}"
        await ledger.finish_run(run_id, outcome="error", checks=result.get("checks"), error=reason, db=db)
        await _move(job, status="failed", run_id=run_id, reason=reason[:2000], db=db)
        raise


async def _apply(job: Dict[str, Any], run_id: str, result: Dict[str, Any], summary: Dict[str, Any],
                 *, db: Any) -> Dict[str, Any]:
    from scripts.curated_apply_gate import evaluate_apply_log
    from services.catalog_enrichment_agent.apply import apply_ingest_plan
    from services.catalog_enrichment_agent.primary_ingestion import require_primary_apply, require_primary_plan

    plan = result["plan"]
    preflight = require_primary_plan(plan)
    try:
        counts = await apply_ingest_plan(plan, batch_label=f"retailer_ingest:{job['id']}", db=db,
                                         primary_readiness=True)
        report = require_primary_apply(preflight, counts)
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
    currency = (job.get("options") or {}).get("require_currency") or "USD"
    readback = await _readback(gate.get("product_keys") or [], currency, db)
    ok = bool(gate.get("ok")) and readback["ok"]
    outcome = "applied" if ok else ("gate_failed" if not gate.get("ok") else "readback_failed")
    await ledger.finish_run(run_id, outcome=outcome, **summary, applied={"gate": gate}, readback=readback,
                            db=db)
    noted = len(readback.get("notes") or [])
    reason = ((f"applied and verified; {noted} row(s) outside backend global recall" if noted
               else "applied and verified") if ok else
              f"{outcome}: gate {gate.get('reasons')}; readback {readback.get('problems')}")
    await _move(job, status="done" if ok else "failed", run_id=run_id, reason=reason, db=db)
    return {"job_id": job["id"], "stage": APPLY, "outcome": outcome, "status": "done" if ok else "failed"}
