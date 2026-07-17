#!/usr/bin/env python3
"""Backfill catalog_products.description for brand-official minted canonicals
from the brand's OWN Shopify storefront (body_html), then recompute
pdp_lifecycle_stage and refresh agent_pdp_view.

WHY. The brand-official MINT lane (services/retailer_ingest/brand_official.py →
curated_brand_feed.shopify_product_to_record) carries only product_type as
attribute_summary, so minted rows land with descriptions like "Toner" (< 50
chars) and fail is_candidate_ready — they stay pdp_lifecycle_stage='draft'
forever and never reach serving (catalog_row_trust stays blocked). The
authoritative description ALREADY EXISTS on the same storefront the row was
minted from: /products.json body_html. This backfill joins each row's
canonical_url (https://{domain}/products/{handle}) back to its own storefront
record — brand-authored copy only (ADR-001), never synthesized, never from a
retailer.

Rows whose storefront has no usable body_html (some brands leave it empty) are
left untouched and reported — they stay draft honestly and route to the LLM
enrichment lane later.

Dry-run (default): report per-domain fill/skip counts, write nothing.
    DATABASE_URL=... python3 scripts/backfill_brand_official_descriptions.py

Apply:
    DATABASE_URL=... python3 scripts/backfill_brand_official_descriptions.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Dict, List
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.agent_pdp_view_assembler import (  # noqa: E402
    refresh_agent_pdp_view_for_content_key,
)
from services.catalog_enrichment_agent.bulk_writer import is_transport_error  # noqa: E402
from services.curated_brand_feed import (  # noqa: E402
    body_html_to_text,
    fetch_shopify_products,
)
from services.pdp_lifecycle import compute_lifecycle_stage  # noqa: E402

REFRESH_SOURCE = "brand_official_description_backfill_v1"
MIN_DESC_LEN = 50          # is_candidate_ready's CANDIDATE_DESCRIPTION_MIN_LEN

# Population: Path-C enrichment-minted rows stuck below 'published' whose
# canonical_url points at a Shopify product page. The description source is
# ALWAYS the row's own canonical_url domain — its authoritative storefront.
_SELECT_ROWS = """
    SELECT product_key, content_key, canonical_url, title, image_url,
           description, category_path, tags, demographic, use_case_tags,
           lifestyle_tags, pdp_scope, source_system, source_domain,
           pdp_lifecycle_stage
    FROM catalog_products
    WHERE source_system = 'catalog_enrichment_agent_v1'
      AND sync_status = 'live'
      AND pdp_lifecycle_stage IN ('draft', 'candidate', 'validated')
      AND canonical_url LIKE 'https://%/products/%'
      AND length(coalesce(description, '')) < 50
"""

_UPDATE_ROW = """
    UPDATE catalog_products
       SET description = :description,
           pdp_lifecycle_stage = :stage,
           updated_at = NOW()
     WHERE product_key = :product_key
"""

def handle_from_url(canonical_url: str) -> tuple[str, str]:
    """('domain', 'handle') from https://{domain}/products/{handle}[?…]."""
    parts = urlsplit(str(canonical_url or ""))
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path or ""
    if "/products/" not in path:
        return host, ""
    handle = path.split("/products/", 1)[1].strip("/").split("/")[0]
    return host, handle


def _should_heal(exc: BaseException) -> bool:
    """Transport failure OR the databases-lib poisoned-connection state (a bare
    AssertionError 'Connection is already acquired') — both mean the shared
    connection is unusable and a reconnect can save the rest of the batch."""
    return is_transport_error(exc) or "already acquired" in str(exc)


async def _reconnect() -> None:
    try:
        await database.disconnect()
    except Exception:  # noqa: BLE001
        pass
    for attempt in range(1, 6):
        try:
            await database.connect()
            return
        except Exception as exc:  # noqa: BLE001
            print(f"  (reconnect attempt {attempt} failed: {str(exc)[:100]})")
            await asyncio.sleep(5 * attempt)
    raise RuntimeError("could not re-establish DB connection")


async def _load_body_map(domain: str, max_products: int) -> Dict[str, str]:
    # fetch_shopify_products swallows errors into [] — a transient network
    # failure would silently mark a whole domain "not_in_feed" (4 domains, 292
    # rows on the first prod run). It also returns a PARTIAL list when
    # pagination dies mid-feed, which looks like an exact multiple of the
    # 250-item page size. Retry both shapes before accepting the result.
    products: List[Dict[str, Any]] = []
    for attempt in range(1, 4):
        products = await fetch_shopify_products(domain, max_products=max_products)
        suspect_truncated = (
            products and len(products) < max_products and len(products) % 250 == 0
        )
        if products and not suspect_truncated:
            break
        if attempt < 3:
            shape = "empty" if not products else f"suspiciously page-aligned ({len(products)})"
            print(f"  ({domain}: {shape} feed on attempt {attempt} — retrying)")
            await asyncio.sleep(10 * attempt)
    return {
        str(p.get("handle") or "").strip(): body_html_to_text(p.get("body_html"))
        for p in products
        if isinstance(p, dict) and p.get("handle")
    }


async def run(apply: bool, domains_filter: List[str], max_products: int) -> int:
    # initial connect through the same healer as mid-run reconnects —
    # a proxy TLS bad-window at process start must not burn a whole
    # stage attempt (observed 2026-07-17).
    await _reconnect()
    rows: List[Dict[str, Any]] = []
    for attempt in range(1, 4):
        try:
            rows = [dict(r) for r in await database.fetch_all(_SELECT_ROWS)]
            break
        except Exception as exc:  # noqa: BLE001
            if not _should_heal(exc) or attempt == 3:
                raise
            print(f"  (population fetch attempt {attempt} failed: {str(exc)[:100]} — reconnecting)")
            await _reconnect()
    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    foreign_domain = 0
    for r in rows:
        host, handle = handle_from_url(r["canonical_url"])
        if not host or not handle:
            continue
        # Provenance guard: fill ONLY from the row's own brand storefront. The
        # mint lane stamps source_domain == canonical host; a Gemini-lane row
        # whose canonical_url points at a Shopify RETAILER (or whose
        # source_domain is NULL — the known validated.jsonl gap) is skipped —
        # retailer copy must never become a brand-official description.
        if host != str(r.get("source_domain") or "").strip().lower():
            foreign_domain += 1
            continue
        if domains_filter and host not in domains_filter:
            continue
        r["_handle"] = handle
        by_domain.setdefault(host, []).append(r)
    if foreign_domain:
        print(f"[provenance] {foreign_domain} row(s) skipped: canonical_url host != source_domain")

    print(f"[population] {sum(len(v) for v in by_domain.values())} rows "
          f"across {len(by_domain)} domain(s)  (apply={apply})")

    totals = {"filled": 0, "published": 0, "validated": 0, "no_body": 0,
              "not_in_feed": 0, "update_failed": 0, "refresh_failed": 0}
    touched_cks: List[str] = []

    for domain in sorted(by_domain):
        drows = by_domain[domain]
        body_map = await _load_body_map(domain, max_products)
        filled = no_body = not_in_feed = 0
        for r in drows:
            body = body_map.get(r["_handle"])
            if body is None:
                not_in_feed += 1
                continue
            if len(body) < MIN_DESC_LEN:
                no_body += 1
                continue
            new_row = {**r, "description": body}
            new_stage = compute_lifecycle_stage(new_row)
            totals[new_stage] = totals.get(new_stage, 0) + 1
            filled += 1
            if apply:
                for attempt in (1, 2):
                    try:
                        await database.execute(_UPDATE_ROW, {
                            "description": body,
                            "stage": new_stage,
                            "product_key": r["product_key"],
                        })
                        if r.get("content_key"):
                            touched_cks.append(str(r["content_key"]))
                        break
                    except Exception as exc:  # noqa: BLE001 — heal a poisoned
                        # connection and retry once; a bad row must not abort
                        # the domain (this is the primary write path).
                        if attempt == 1 and _should_heal(exc):
                            await _reconnect()
                            continue
                        totals["update_failed"] += 1
                        print(f"  WARN update failed pk={r['product_key']}: "
                              f"{type(exc).__name__}: {str(exc)[:150]}")
                        break
        totals["filled"] += filled
        totals["no_body"] += no_body
        totals["not_in_feed"] += not_in_feed
        print(f"  {domain:22s} rows={len(drows):4d} feed={len(body_map):4d} "
              f"fill={filled:4d} empty_body={no_body:3d} not_in_feed={not_in_feed:3d}")

    if apply and touched_cks:
        distinct = sorted(set(touched_cks))
        print(f"[view] refreshing {len(distinct)} content_key view(s) ...")
        for ck in distinct:
            for attempt in (1, 2):
                try:
                    await refresh_agent_pdp_view_for_content_key(ck, refresh_source=REFRESH_SOURCE)
                    break
                except Exception as exc:  # noqa: BLE001 — a transport failure
                    # poisons the pinned connection ("Connection is already
                    # acquired" cascade, 511 refreshes lost on the first prod
                    # run) — heal it and retry once before skipping the key.
                    if attempt == 1 and _should_heal(exc):
                        await _reconnect()
                        continue
                    totals["refresh_failed"] += 1
                    print(f"  WARN view refresh failed for {ck}: {str(exc)[:120]}")
                    break

    await database.disconnect()
    print(f"[done] {totals}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="write updates (default: dry-run)")
    p.add_argument("--domains", default="", help="comma-separated domain allowlist")
    p.add_argument("--max-products", type=int, default=800)
    args = p.parse_args()
    domains = [d.strip().lower() for d in args.domains.split(",") if d.strip()]
    return asyncio.run(run(args.apply, domains, args.max_products))


if __name__ == "__main__":
    raise SystemExit(main())
