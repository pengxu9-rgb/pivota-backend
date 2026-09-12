"""Onboard a curated list of brand storefronts into the commerce index — the
CLEAN primary catalog-coverage feed.

For each curated brand (domain + category), enumerate its products via Shopify's
public /products.json and ingest them as depositable canonical anchors (brand-direct,
official_url = the brand's own storefront — no Gemini needed). Reuses the same
FK-order executor as run_catalog_enrichment / the Path-C runner (no SQL drift).

Input — a JSONL on --file (one brand per line) OR a single --domain:
  {"domain": "kosas.com", "category_path": "beauty/makeup", "brand": "Kosas"}

Usage:
  python -m scripts.onboard_curated_brands --domain kosas.com --category beauty/makeup
  python -m scripts.onboard_curated_brands --file data/catalog_enrichment/curated_brands.jsonl --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.catalog_enrichment_agent.apply import apply_ingest_plan  # noqa: E402
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl  # noqa: E402
from services.catalog_enrichment_agent.primary_ingestion import (  # noqa: E402
    inspect_primary_plan, require_primary_plan, require_primary_apply,
)
from services.curated_brand_feed import CrawlIncomplete, records_for_brand  # noqa: E402
from services.catalog_onboard_worker import normalize_curated_brand_payload  # noqa: E402


def _read_brand_list(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if args.domain:
        return [{"domain": args.domain, "category_path": args.category, "brand": args.brand}]
    brands: List[Dict[str, Any]] = []
    with open(args.file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSON in brand roster") from exc
            if not isinstance(row, dict) or not row.get("domain"):
                raise ValueError("every brand roster row requires a domain")
            row.setdefault("category_path", args.category)
            brands.append(row)
    return brands


async def _run(args: argparse.Namespace) -> int:
    brands = _read_brand_list(args)
    if not brands:
        print("no brands to onboard (need --domain or --file rows with domain+category)", file=sys.stderr)
        return 2

    # Normalize the whole roster before the first fetch. Per-row controls and
    # unsupported markets must have the same contract as unattended queue work.
    defaults = {
        "source_role": args.source_role,
        "retailer_name": args.retailer_name,
        "only_vendors": args.only_vendor or None,
        "require_currency": args.require_currency,
        "max_products": args.max_products,
        "base_listings_only": args.base_listings_only,
        "emit_real_variants": args.emit_real_variants,
        "max_scan_products": args.max_scan_products,
        "enrich_missing_gtin": args.enrich_missing_gtin,
        "max_pdp_identity_fetches": args.max_pdp_identity_fetches,
    }
    brands = [normalize_curated_brand_payload({
        **{k: v for k, v in defaults.items() if v is not None}, **b,
    }) for b in brands]
    all_records: List[Dict[str, Any]] = []
    for b in brands:
        recs = await records_for_brand(**{k: v for k, v in b.items() if k != "market"})
        crawl_report = getattr(recs, "crawl_report", None)
        if not isinstance(crawl_report, dict) or crawl_report.get("status") != "complete":
            raise ValueError(f"{b['domain']}: crawl completeness was not proven")
        print("    crawl: " + json.dumps(crawl_report, sort_keys=True))
        if not recs:
            raise ValueError(f"{b['domain']}: no products enumerated; cohort cannot be applied")
        vendor_report = getattr(records_for_brand, "last_vendor_filter_report", None)
        if vendor_report and vendor_report.get("vendors"):
            print(
                f"    vendor filter {vendor_report['vendors']}: "
                f"{vendor_report['before']} -> {vendor_report['after']} products"
            )
            records_for_brand.last_vendor_filter_report = None  # type: ignore[attr-defined]
        print(f"  {b['domain']}: {len(recs)} products")
        # A brand-family storefront must not ingest silently. misshaus.com shipped 17
        # A'pieu products into the index branded "Missha" because nothing printed the
        # disagreement at run time.
        census = getattr(records_for_brand, "last_brand_census", None)
        if census and census.get("kept_vendor_count"):
            print(
                f"    brand: '{census['brand_override']}' does NOT own "
                f"{census['kept_vendor_count']} product(s) on this storefront — "
                f"keeping each product's own vendor:"
            )
            for vname, info in sorted(
                census["vendors"].items(), key=lambda kv: -kv[1]["count"]
            ):
                if info["reason"] == "vendor_disagrees":
                    print(f"      {info['count']:>4}  {vname} (kept as its own brand)")
            print(
                "      pass --only-vendor to ingest just one brand from this feed."
            )
            records_for_brand.last_brand_census = None  # type: ignore[attr-defined]
        fold = getattr(records_for_brand, "last_fold_report", None) if b["base_listings_only"] else None
        if fold:
            print(
                f"    folded {fold['shades']} shade listing(s) into {fold['bases']} base row(s); "
                f"{fold['stubs_replaced']} placeholder stub variant(s) replaced by their shades"
            )
        all_records.extend(recs)

    plan = ingest_validated_jsonl(all_records)
    print(
        f"plan: pdps={len(plan.get('pdps') or [])} skus={len(plan.get('skus') or [])} "
        f"offers={len(plan.get('offers') or [])} seeds={len(plan.get('seeds') or [])} "
        f"skipped={plan.get('skipped')}"
    )
    print("primary ingestion: " + json.dumps(inspect_primary_plan(plan), sort_keys=True))
    if not args.apply:
        pdps = plan.get("pdps") or []
        for p in pdps[:5]:
            print("   ", {k: p.get(k) for k in ("product_key", "brand", "title", "content_key")})
        print("  DRY-RUN — re-run with --apply to ingest as depositable anchors.")
        return 0

    preflight = require_primary_plan(plan)
    from db.database import database  # noqa: E402
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        counts = await apply_ingest_plan(plan, batch_label=f"curated_brands:{len(brands)}", db=database)
        result = require_primary_apply(preflight, counts)
        print("primary ingestion: " + json.dumps(result, sort_keys=True))
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--domain", help="storefront domain (retailers require --source-role retailer)")
    g.add_argument("--file", help="JSONL of {domain, category_path, brand?} rows")
    p.add_argument("--category", help="category_path (default/override for rows without one)")
    p.add_argument("--brand", help="brand name override (single --domain mode)")
    p.add_argument("--max-products", type=int, default=500, help="selected product budget; cap refuses partial ingestion")
    p.add_argument(
        "--fold-shades", "--base-listings-only",
        dest="base_listings_only",
        action="store_true",
        help=(
            "fold single-variant '<base> - <shade>' listings into the base listing's variants "
            "(maccosmetics.com publishes one product per shade; as-is that mints one PDP per shade). "
            "The base keeps one PDP; each shade becomes a SKU + offer of it."
        ),
    )
    p.add_argument(
        "--emit-real-variants",
        dest="emit_real_variants",
        action="store_true",
        help=(
            "emit the merchant's OWN variants for natively multi-variant products, one "
            "purchasable SKU + priced offer each (flowerbeauty.com publishes 29 such products "
            "carrying 185 real Shopify variant ids; without this the brand ingests 49 SKUs whose "
            "source_variant_id is the product key, which no checkout can spend). Ids that "
            "services/variant_identity cannot place as merchant-issued are dropped, not minted."
        ),
    )
    p.add_argument(
        "--only-vendor",
        action="append",
        default=[],
        metavar="VENDOR",
        help=(
            "keep only products whose Shopify `vendor` is this (repeatable). For a "
            "MULTI-BRAND RETAILER feed — cocomo.sg lists 224 vendors and 1,000 products, "
            "of which 24 are VELY VELY. Use --source-role retailer; --brand normalizes the selected "
            "brand spelling; this flag SELECTS. Matching is exact after case/whitespace "
            "normalisation, and a filter matching nothing is an error, not an empty run."
        ),
    )
    p.add_argument(
        "--require-currency",
        metavar="ISO4217",
        help=(
            "refuse the brand unless its /meta.json proves this currency (e.g. SGD). "
            "All runs require observed currency; this additionally asserts the expected code. "
            "Never converts: it refuses."
        ),
    )
    p.add_argument("--source-role", choices=["brand_official", "retailer"], default=None,
                   help="Retailer mode separates seller from maker and requires proven currency")
    p.add_argument("--retailer-name", help="Retailer display name; defaults to its host")
    p.add_argument("--max-scan-products", type=int, default=10000,
                   help="Whole-feed scan budget, independent of selected-brand --max-products")
    p.add_argument("--enrich-missing-gtin", action="store_true",
                   help="Recover missing barcodes from identity-matched product .js; opt-in, no price changes")
    p.add_argument("--max-pdp-identity-fetches", type=int, default=100,
                   help="Selected-product recovery attempt budget (0 disables requests; up to two redirects each)")
    p.add_argument("--apply", action="store_true", help="ingest (else dry-run plan)")
    args = p.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except CrawlIncomplete as exc:
        print(json.dumps({"crawl": exc.as_dict()}), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
