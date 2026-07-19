#!/usr/bin/env python3
"""Backfill review ratings + INCI onto EXISTING brand-official canonicals.

WHY. The capture code merged on main writes ratings + INCI only at INGEST. The
~2,168 brand-official canonicals already in the index
(source_system='catalog_enrichment_agent_v1') predate that capture — they carry
rating 0% and INCI ~1.6%. This script re-fetches each row's OWN source and writes
the data using the SAME merged extraction functions, so nothing is fabricated and
the decision-intelligence authoring pass (which reads INCI for evidence bullets)
has something to read.

Two lanes, prefer brand-official (ADR-001 precedence):

  1. INCI (brand-official) — Shopify storefronts. For a cohort row whose
     canonical_url is https://{domain}/products/{handle} AND host==source_domain,
     fetch that domain's /products.json (cached per-domain like the description
     backfill), find the product by handle, run
     curated_brand_feed.inci_from_body_html(body_html), and write via
     canonical_inci_intake.ingest_canonical_inci(source='brand_official'). This is
     the primary lane — a batched storefront read, not a web re-crawl.

  2. Ratings + reseller INCI (StyleKorean) — for a cohort row carrying a
     stylekorean_global catalog_offers row, re-crawl that offer's SK PDP
     (catalog_offers.source_ref) POLITELY (PivotaBot UA, retry, inter-request
     delay, per-URL cache) with sitemap_crawler.extract_product, which returns
     _parse_aggregate_rating's (rating_value, rating_count) + the RSC INCI. Write
     the rating to catalog_products (clamped [0,5]) and the INCI via
     ingest_canonical_inci(source='reseller_listing') — precedence guarantees it
     never overwrites a brand-official list. SK is the ONLY rating source (Shopify
     /products.json exposes none).

REUSE. Models on scripts/backfill_brand_official_descriptions.py and imports its
handle_from_url / _reconnect / _should_heal helpers. Extraction + writes are the
exact merged functions (inci_from_body_html, extract_product,
ingest_canonical_inci, _coerce_rating_value, refresh_agent_pdp_view_for_content_key).

RESUMABLE / IDEMPOTENT. The cohort SQL excludes rows that already carry the data
(brand-official INCI present for lane 1; both rating AND INCI present for lane 2),
so a re-run only touches what is still missing. Keyset pagination by product_key,
per-row failure isolation, _reconnect/_should_heal on transient proxy errors.
Assumes it is the ONLY prod writer (a gated serial chain) — no added concurrency.

NEVER FABRICATE. A row with no recoverable INCI/rating stays null and is counted.
Retailer INCI must clear ingest_canonical_inci's validity gate (relied on, never
bypassed).

Dry-run (default): report per-lane cohort size + a sampled would-gain probe,
write nothing, do not crawl the whole set.
    DATABASE_URL=... python3 scripts/backfill_ratings_inci.py
Apply:
    DB_POOL_MIN_SIZE=1 DB_POOL_MAX_SIZE=3 \
    DB_POOL_ACQUIRE_TIMEOUT_SECONDS=30 DB_COMMAND_TIMEOUT_SECONDS=60 \
    DATABASE_URL=... python3 scripts/backfill_ratings_inci.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from scripts.backfill_brand_official_descriptions import (  # noqa: E402 — shared helpers
    _reconnect,
    _should_heal,
    handle_from_url,
)
from services.agent_pdp_view_assembler import (  # noqa: E402
    refresh_agent_pdp_view_for_content_key,
)
from services.canonical_inci_intake import (  # noqa: E402
    INCI_SOURCE_BRAND_OFFICIAL,
    INCI_SOURCE_RESELLER,
    ingest_canonical_inci,
)
from services.catalog_enrichment_agent.ingestion import (  # noqa: E402
    _coerce_rating_count,
    _coerce_rating_value,
)
from services.curated_brand_feed import (  # noqa: E402
    fetch_shopify_products,
    inci_from_body_html,
)
from services.retailer_ingest.sitemap_crawler import (  # noqa: E402
    extract_product,
    fetch_text,
)

REFRESH_SOURCE = "backfill_ratings_inci_v1"
SK_MERCHANT_ID = "stylekorean_global"
COHORT_SOURCE_SYSTEM = "catalog_enrichment_agent_v1"
_PAGE = 500  # keyset page size

# --- Lane 1: brand-official INCI cohort (Shopify storefronts). A row qualifies
# when its canonical_url is a Shopify product page and it does NOT already carry a
# brand-official INCI list (a reseller-tier list is upgradeable, so it still
# qualifies — ingest precedence decides). Keyset by product_key.
_L1_COHORT = """
    SELECT cp.product_key, cp.content_key, cp.canonical_url, cp.source_domain
      FROM catalog_products cp
     WHERE cp.source_system = :src
       AND cp.sync_status = 'live'
       AND cp.canonical_url LIKE 'https://%/products/%'
       AND cp.product_key > :cursor
       AND NOT EXISTS (
             SELECT 1 FROM beauty_sku_ingredients bsi
              WHERE bsi.product_key = cp.product_key
                AND bsi.source_system = 'brand_official'
                AND length(coalesce(bsi.raw_inci, '')) > 0
       )
     ORDER BY cp.product_key
     LIMIT :limit
"""

_L1_COUNT = """
    SELECT count(*) AS n
      FROM catalog_products cp
     WHERE cp.source_system = :src
       AND cp.sync_status = 'live'
       AND cp.canonical_url LIKE 'https://%/products/%'
       AND NOT EXISTS (
             SELECT 1 FROM beauty_sku_ingredients bsi
              WHERE bsi.product_key = cp.product_key
                AND bsi.source_system = 'brand_official'
                AND length(coalesce(bsi.raw_inci, '')) > 0
       )
"""

# --- Lane 2: StyleKorean ratings + reseller INCI cohort. A row qualifies when it
# carries a stylekorean_global offer with a crawlable source_ref AND is still
# missing a rating OR still missing any INCI (so a row that already has BOTH is
# skipped — idempotent). Keyset by product_key.
_L2_COHORT = """
    SELECT cp.product_key, cp.content_key, cp.rating_value,
           o.source_ref AS sk_url,
           EXISTS (
             SELECT 1 FROM beauty_sku_ingredients bsi
              WHERE bsi.product_key = cp.product_key
                AND length(coalesce(bsi.raw_inci, '')) > 0
           ) AS has_inci
      FROM catalog_products cp
      JOIN catalog_offers o
        ON o.product_key = cp.product_key
       AND o.merchant_id = :sk
     WHERE cp.source_system = :src
       AND cp.sync_status = 'live'
       AND o.source_ref LIKE 'http%'
       AND cp.product_key > :cursor
       AND (
             cp.rating_value IS NULL
             OR NOT EXISTS (
               SELECT 1 FROM beauty_sku_ingredients bsi
                WHERE bsi.product_key = cp.product_key
                  AND length(coalesce(bsi.raw_inci, '')) > 0
             )
       )
     ORDER BY cp.product_key
     LIMIT :limit
"""

_L2_COUNT = """
    SELECT count(DISTINCT cp.product_key) AS n
      FROM catalog_products cp
      JOIN catalog_offers o
        ON o.product_key = cp.product_key
       AND o.merchant_id = :sk
     WHERE cp.source_system = :src
       AND cp.sync_status = 'live'
       AND o.source_ref LIKE 'http%'
       AND (
             cp.rating_value IS NULL
             OR NOT EXISTS (
               SELECT 1 FROM beauty_sku_ingredients bsi
                WHERE bsi.product_key = cp.product_key
                  AND length(coalesce(bsi.raw_inci, '')) > 0
             )
       )
"""

# Rating write: mirror apply.py's COALESCE semantics (a captured value is never
# blanked) but gate on IS NULL so the backfill is idempotent and never clobbers a
# rating another lane already set. rating_value is clamped upstream by
# _coerce_rating_value / _parse_aggregate_rating.
_RATING_UPDATE = """
    UPDATE catalog_products
       SET rating_value = :rv, rating_count = :rc, updated_at = NOW()
     WHERE product_key = :pk AND rating_value IS NULL
"""


async def _healed_fetch_all(query: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """fetch_all with the same transient-proxy healing as the write path."""
    for attempt in (1, 2, 3):
        try:
            return [dict(r) for r in await database.fetch_all(query, params)]
        except Exception as exc:  # noqa: BLE001
            if not _should_heal(exc) or attempt == 3:
                raise
            print(f"  (fetch attempt {attempt} failed: {str(exc)[:100]} — reconnecting)")
            await _reconnect()
    return []


async def _healed_execute(query: str, params: Dict[str, Any]) -> bool:
    """execute with one heal-and-retry; returns False on a non-transient failure."""
    for attempt in (1, 2):
        try:
            await database.execute(query, params)
            return True
        except Exception as exc:  # noqa: BLE001
            if attempt == 1 and _should_heal(exc):
                await _reconnect()
                continue
            print(f"  WARN execute failed: {type(exc).__name__}: {str(exc)[:150]}")
            return False
    return False


async def _refresh_view(ck: str) -> bool:
    for attempt in (1, 2):
        try:
            await refresh_agent_pdp_view_for_content_key(ck, refresh_source=REFRESH_SOURCE)
            return True
        except Exception as exc:  # noqa: BLE001
            if attempt == 1 and _should_heal(exc):
                await _reconnect()
                continue
            print(f"  WARN view refresh failed for {ck}: {str(exc)[:120]}")
            return False
    return False


# --------------------------------------------------------------------------
# Lane 1 — brand-official INCI from the row's own Shopify storefront
# --------------------------------------------------------------------------

async def _load_inci_map(domain: str, cache: Dict[str, Dict[str, str]], max_products: int) -> Dict[str, str]:
    """handle -> brand-official INCI for a domain's /products.json, cached per run.

    Retries an empty / suspiciously page-aligned (truncated) feed before accepting
    it — a transient network blip must not silently mark a whole domain unfillable
    (same failure mode the description backfill hardened against)."""
    if domain in cache:
        return cache[domain]
    products: List[Dict[str, Any]] = []
    for attempt in (1, 2, 3):
        products = await fetch_shopify_products(domain, max_products=max_products)
        suspect_truncated = bool(products) and len(products) < max_products and len(products) % 250 == 0
        if products and not suspect_truncated:
            break
        if attempt < 3:
            shape = "empty" if not products else f"page-aligned({len(products)})"
            print(f"  ({domain}: {shape} feed on attempt {attempt} — retrying)")
            await asyncio.sleep(5 * attempt)
    inci_map: Dict[str, str] = {}
    for p in products:
        if not isinstance(p, dict):
            continue
        handle = str(p.get("handle") or "").strip()
        if not handle:
            continue
        inci = inci_from_body_html(p.get("body_html"))
        if inci:
            inci_map[handle] = inci
    cache[domain] = inci_map
    return inci_map


async def _process_l1_row(
    r: Dict[str, Any],
    *,
    cache: Dict[str, Dict[str, str]],
    max_products: int,
    apply: bool,
    totals: Dict[str, int],
    touched: set,
) -> None:
    host, handle = handle_from_url(r["canonical_url"])
    # Provenance guard: fill ONLY from the row's own brand storefront (ADR-001).
    # A canonical_url pointing at a retailer, or a NULL source_domain, is skipped —
    # retailer copy must never become a brand-official INCI list.
    if not host or not handle:
        totals["l1_no_handle"] += 1
        return
    if host != str(r.get("source_domain") or "").strip().lower():
        totals["l1_foreign_domain"] += 1
        return
    inci_map = await _load_inci_map(host, cache, max_products)
    inci = inci_map.get(handle)
    if not inci:
        totals["l1_no_inci_on_storefront"] += 1
        return
    res = await ingest_canonical_inci(
        r["product_key"], inci, INCI_SOURCE_BRAND_OFFICIAL, dry_run=not apply
    )
    status = res.get("status")
    if status != "ok":
        totals["l1_rejected"] += 1
        return
    if res.get("written_skus"):
        totals["l1_inci_written"] += 1
        if r.get("content_key"):
            touched.add(str(r["content_key"]))
    else:
        # outranked by an existing brand-official list, or no SKU to attach.
        totals["l1_skipped_outranked"] += 1


# --------------------------------------------------------------------------
# Lane 2 — StyleKorean ratings + reseller INCI (polite web re-crawl)
# --------------------------------------------------------------------------

async def _crawl_sk(url: str, cache: Dict[str, Optional[Dict[str, Any]]], delay: float) -> Optional[Dict[str, Any]]:
    """Polite, cached SK PDP fetch+extract. Returns extract_product's dict (rating
    + RSC INCI) or None. fetch_text carries the PivotaBot UA, timeout, and
    Retry-After-aware retry; we add an inter-request delay and run the blocking
    urllib call off the event loop."""
    if url in cache:
        return cache[url]
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        html = await asyncio.to_thread(fetch_text, url)
        result = extract_product(html)
    except Exception as exc:  # noqa: BLE001 — a dead PDP must not abort the lane
        print(f"  WARN SK crawl failed {url}: {type(exc).__name__}: {str(exc)[:120]}")
        result = None
    cache[url] = result
    return result


async def _process_l2_row(
    r: Dict[str, Any],
    *,
    cache: Dict[str, Optional[Dict[str, Any]]],
    delay: float,
    apply: bool,
    totals: Dict[str, int],
    touched: set,
) -> None:
    extracted = await _crawl_sk(r["sk_url"], cache, delay)
    if extracted is None:
        totals["l2_no_product"] += 1
        return
    gained = False

    # --- rating -> catalog_products (only when the row still lacks one) --------
    if r.get("rating_value") is None:
        rv = _coerce_rating_value(extracted.get("rating_value"))
        rc = _coerce_rating_count(extracted.get("rating_count"))
        if rv is not None:
            if apply:
                ok = await _healed_execute(_RATING_UPDATE, {"rv": rv, "rc": rc, "pk": r["product_key"]})
                if not ok:
                    totals["l2_rating_failed"] += 1
            totals["l2_rating_written"] += 1
            gained = True
        else:
            totals["l2_no_rating_on_page"] += 1

    # --- reseller INCI -> beauty_sku_ingredients (only when none present) ------
    if not r.get("has_inci"):
        raw_inci = extracted.get("raw_inci")
        if raw_inci:
            res = await ingest_canonical_inci(
                r["product_key"], raw_inci, INCI_SOURCE_RESELLER, dry_run=not apply
            )
            if res.get("status") == "ok" and res.get("written_skus"):
                totals["l2_inci_written"] += 1
                gained = True
            elif res.get("status") == "ok":
                totals["l2_inci_skipped_outranked"] += 1
            else:
                totals["l2_inci_rejected"] += 1
        else:
            totals["l2_no_inci_on_page"] += 1

    if gained and r.get("content_key"):
        touched.add(str(r["content_key"]))


# --------------------------------------------------------------------------
# Drivers
# --------------------------------------------------------------------------

async def _paginate(query: str, base_params: Dict[str, Any], limit: Optional[int]):
    """Yield cohort rows in keyset (product_key) order until exhausted or `limit`
    rows have been yielded. Each page is a fresh query, so rows written on an
    earlier page fall out of the cohort filter on a re-run."""
    cursor = ""
    yielded = 0
    while True:
        page = min(_PAGE, (limit - yielded)) if limit is not None else _PAGE
        if page <= 0:
            return
        rows = await _healed_fetch_all(query, {**base_params, "cursor": cursor, "limit": page})
        if not rows:
            return
        for r in rows:
            yield r
            yielded += 1
        cursor = rows[-1]["product_key"]
        if len(rows) < page:
            return


async def run(args: argparse.Namespace) -> int:
    await _reconnect()
    lanes = {s.strip() for s in args.lanes.split(",") if s.strip()} or {"inci", "rating"}
    apply = args.apply
    totals: Dict[str, int] = {k: 0 for k in (
        "l1_no_handle", "l1_foreign_domain", "l1_no_inci_on_storefront", "l1_rejected",
        "l1_inci_written", "l1_skipped_outranked",
        "l2_no_product", "l2_rating_written", "l2_rating_failed", "l2_no_rating_on_page",
        "l2_inci_written", "l2_inci_skipped_outranked", "l2_inci_rejected", "l2_no_inci_on_page",
    )}
    touched: set = set()

    if "inci" in lanes:
        n = (await _healed_fetch_all(_L1_COUNT, {"src": COHORT_SOURCE_SYSTEM}))[0]["n"]
        print(f"[lane1/inci] cohort = {n} brand-official Shopify rows missing brand-official INCI  (apply={apply})")
        cache: Dict[str, Dict[str, str]] = {}
        limit = args.sample if not apply else args.limit
        i = 0
        async for r in _paginate(_L1_COHORT, {"src": COHORT_SOURCE_SYSTEM}, limit):
            i += 1
            try:
                await _process_l1_row(r, cache=cache, max_products=args.max_products,
                                      apply=apply, totals=totals, touched=touched)
            except Exception as exc:  # noqa: BLE001 — one bad row never aborts the lane
                if _should_heal(exc):
                    await _reconnect()
                print(f"  WARN l1 row pk={r.get('product_key')}: {type(exc).__name__}: {str(exc)[:120]}")
        print(f"[lane1/inci] processed {i} row(s): "
              f"would_write_inci={totals['l1_inci_written']} "
              f"no_inci_on_storefront={totals['l1_no_inci_on_storefront']} "
              f"foreign_domain={totals['l1_foreign_domain']} "
              f"outranked={totals['l1_skipped_outranked']} rejected={totals['l1_rejected']}")

    if "rating" in lanes:
        n = (await _healed_fetch_all(_L2_COUNT, {"src": COHORT_SOURCE_SYSTEM, "sk": SK_MERCHANT_ID}))[0]["n"]
        print(f"[lane2/sk] cohort = {n} StyleKorean-offer rows missing rating and/or INCI  (apply={apply})")
        cache2: Dict[str, Optional[Dict[str, Any]]] = {}
        limit = args.sample if not apply else args.limit
        i = 0
        async for r in _paginate(_L2_COHORT, {"src": COHORT_SOURCE_SYSTEM, "sk": SK_MERCHANT_ID}, limit):
            i += 1
            try:
                await _process_l2_row(r, cache=cache2, delay=args.sk_delay,
                                      apply=apply, totals=totals, touched=touched)
            except Exception as exc:  # noqa: BLE001
                if _should_heal(exc):
                    await _reconnect()
                print(f"  WARN l2 row pk={r.get('product_key')}: {type(exc).__name__}: {str(exc)[:120]}")
        print(f"[lane2/sk] processed {i} row(s): "
              f"would_write_rating={totals['l2_rating_written']} "
              f"would_write_inci={totals['l2_inci_written']} "
              f"no_product={totals['l2_no_product']} "
              f"no_rating_on_page={totals['l2_no_rating_on_page']} "
              f"no_inci_on_page={totals['l2_no_inci_on_page']} "
              f"inci_outranked={totals['l2_inci_skipped_outranked']}")

    if apply and touched:
        distinct = sorted(touched)
        print(f"[view] refreshing {len(distinct)} content_key view(s) ...")
        refreshed = 0
        for ck in distinct:
            if await _refresh_view(ck):
                refreshed += 1
        print(f"[view] refreshed {refreshed}/{len(distinct)}")

    await database.disconnect()
    if not apply:
        print("[dry-run] no writes performed. Re-run with --apply to write "
              "(sampled probe above; --sample controls the crawl cap).")
    print(f"[done] {totals}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--apply", action="store_true", help="write updates (default: dry-run)")
    p.add_argument("--lanes", default="inci,rating",
                   help="comma list of lanes to run: inci,rating (default both)")
    p.add_argument("--sample", type=int, default=25,
                   help="dry-run: max rows PER LANE to probe (caps the crawl)")
    p.add_argument("--limit", type=int, default=None,
                   help="apply: cap total rows per lane (default: whole cohort)")
    p.add_argument("--sk-delay", type=float, default=0.5,
                   help="lane2: inter-request politeness delay, seconds")
    p.add_argument("--max-products", type=int, default=800,
                   help="lane1: max products fetched per Shopify storefront")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
