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

from services.catalog_identity import validated_source_gtin  # noqa: E402
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


#: Identity fields an acceptance gate has to read off a plan. `product_key` and
#: `content_key` are the listing/canonical identities; `gtin` and `category_path` are
#: what the canary validator actually asserts (scripts/validate_meitu_canary_evidence.py
#: checks the exact GTIN and a `beauty/makeup/lip/` leaf per product); `merchant_id` and
#: `source_domain` say WHICH seller the row belongs to, which is what makes a two-retailer
#: plan reviewable. Every name here exists on a planned PDP row — `category_resolution_status`
#: deliberately does NOT: the mapper stamps it on the record's `pdp`, but _build_pdp_insert
#: does not carry it onto the planned row, so printing it would show None for every product
#: and read as "unresolved" when it only means "absent". `inspect_primary_plan`'s
#: `unresolved_category_count` remains the authority on blocked categories.
_PLAN_IDENTITY_FIELDS = (
    "product_key", "brand", "title", "content_key", "gtin",
    "category_path", "merchant_id", "source_domain", "source_product_id",
)


#: Dry-run default. --apply defaults to printing EVERY row instead (see _effective_print_limit):
#: on apply the print is the only record of what was about to be written, and a 269-PDP
#: runbook cohort truncated at 50 leaves 81% of the write undisclosed.
_PLAN_PRINT_DEFAULT = 50

#: Printed beside the row's own fields but DERIVED from plan["skus"], not read off the PDP row.
_PLAN_DERIVED_FIELDS = ("variant_ids", "variant_count")


def _variant_ids_by_product(plan: Dict[str, Any]) -> Dict[str, List[str]]:
    """Merchant variant ids per product, from the planned SKUs.

    The canonical SKU restates the product_key as its `source_variant_id`
    (ingestion.derive_variant_sku_key's sibling path), which is not a merchant variant —
    and `validate_meitu_canary_evidence.py` rejects exactly that (`variant_id` starting
    `ext:`/`sig_`, or provenance != merchant_issued). Only the real ones are listed.
    """
    out: Dict[str, List[str]] = {}
    for sku in plan.get("skus") or []:
        product_key = str(sku.get("product_key") or "")
        variant_id = str(sku.get("source_variant_id") or "")
        if not product_key or not variant_id or variant_id == product_key:
            continue
        out.setdefault(product_key, []).append(variant_id)
    return out


def _print_plan_identity(plan: Dict[str, Any], *, limit: int) -> None:
    """Print WHICH products a plan contains, not just how many.

    Counts alone cannot distinguish a stable cohort from one that churned while its
    size held: a retailer that delists the product a canary selected and lists another
    the same day still plans the same number of PDPs. Reviewing that plan by eye, or
    gating on it in a script, needs the per-row identity — so this prints one sorted
    JSON object per PDP, greppable for an exact GTIN or category leaf.

    It runs for BOTH dry-run and --apply: the apply path re-crawls and re-plans, so the
    rows it is about to write are not necessarily the rows that were reviewed.

    `ensure_ascii=False` because a gate greps these lines: escaping a Korean or accented
    title to \\uXXXX makes the very cohort this lane exists for unsearchable.
    """
    pdps = plan.get("pdps") or []
    variants = _variant_ids_by_product(plan)
    shown = pdps if limit <= 0 or len(pdps) <= limit else pdps[:limit]
    for row in shown:
        disclosed = {k: row.get(k) for k in _PLAN_IDENTITY_FIELDS}
        ids = variants.get(str(row.get("product_key") or ""), [])
        # Variant identity is re-keyed or dropped by apply AFTER this print
        # (apply._resolve_offer_keys / _adopt_existing_sku_identities), so this is the
        # planned intent, not a guarantee of what lands.
        disclosed["variant_ids"] = ids[:8]
        disclosed["variant_count"] = len(ids)
        print("    pdp " + json.dumps(disclosed, sort_keys=True, ensure_ascii=False))
    if len(shown) < len(pdps):
        print(f"    ... {len(pdps) - len(shown)} further PDP row(s) not printed "
              f"(raise --plan-print-limit, 0 prints all)")


def _effective_print_limit(args: argparse.Namespace) -> int:
    """An explicit --plan-print-limit always wins; otherwise apply discloses everything."""
    if args.plan_print_limit is not None:
        return args.plan_print_limit
    return 0 if args.apply else _PLAN_PRINT_DEFAULT


def _canonical_gtins(wanted: List[str]) -> set:
    """Every requested GTIN, or refuse. A single unusable value is a typo in a command line that
    decides what gets written to production: ignoring it because a SIBLING matched is how a run
    certifies a cohort nobody asked for."""
    canonical = set()
    for value in wanted:
        gtin = validated_source_gtin(value)
        if not gtin:
            raise ValueError(f"--only-gtin {value!r} is not a valid GS1 GTIN")
        canonical.add(gtin)
    if not canonical:
        # Unreachable from _run (guarded by `if args.only_gtin`, and every element either raises
        # or is added), but this is a module-level helper: a future caller passing [] should be
        # told, not handed an empty set that quietly matches nothing downstream.
        raise ValueError("--only-gtin needs at least one valid GS1 GTIN")
    return canonical


def _record_gtins(record: Dict[str, Any]) -> set:
    """Every GTIN this record can be identified by: the PDP's own, and its variants'.

    `pdp.barcode` is set only for a single-variant, unfolded product (curated_brand_feed), so a
    multi-variant listing — exactly what --emit-real-variants produces — carries its barcodes on
    the VARIANTS. Matching the PDP level alone refused those products with "matched none" while the help
    text promised to keep "products carrying this GTIN".
    """
    pdp = record.get("pdp") or {}
    found = {validated_source_gtin(pdp.get("gtin") or pdp.get("barcode"))}
    for variant in (pdp.get("variants") or []):
        if isinstance(variant, dict):
            found.add(validated_source_gtin(variant.get("barcode") or variant.get("gtin")))
    return {g for g in found if g}


def _select_by_gtin(records: List[Dict[str, Any]], canonical: set, *, domain: str) -> tuple:
    """Keep only the records carrying one of `canonical`; return (kept, gtins that matched).

    An acceptance case is about specific products, but the vendor filter is the narrowest tool the
    lane had: a Pyunkang Yul run at eyurs.com selects 10 products, 7 of which carry a merchant
    product_type this taxonomy does not map, so the whole cohort lands `category_unresolved` and
    apply refuses it — over products the case never wanted.

    A filter that matches NOTHING raises, mirroring --only-vendor: an empty run that looks like a
    clean one is how a canary certifies a cohort it never actually selected. Which of several
    requested GTINs went unmatched is decided by the CALLER, across every domain in the run — one
    roster row may legitimately carry only some of them.
    """
    kept, matched = [], set()
    for record in records:
        found = _record_gtins(record) & canonical
        if found:
            kept.append(record)
            matched |= found
    if not kept:
        raise ValueError(f"{domain}: --only-gtin matched none of the selected products")
    return kept, matched


async def _run(args: argparse.Namespace) -> int:
    brands = _read_brand_list(args)
    # Validated BEFORE the first fetch: a typo'd GTIN should cost nothing and stop everything.
    wanted_gtins = _canonical_gtins(args.only_gtin) if args.only_gtin else set()
    matched_gtins: set = set()
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
        if wanted_gtins:
            before = len(recs)
            recs, matched = _select_by_gtin(recs, wanted_gtins, domain=b["domain"])
            matched_gtins |= matched
            print(f"    gtin filter {sorted(wanted_gtins)}: {before} -> {len(recs)} products "
                  f"(matched {sorted(matched)})")
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

    # A requested GTIN that matched nothing ANYWHERE is a request that was silently dropped. Across
    # the whole run, not per row: a roster row may legitimately carry only some of them.
    if wanted_gtins - matched_gtins:
        raise ValueError(f"--only-gtin values matched no product in this run: "
                         f"{sorted(wanted_gtins - matched_gtins)}")
    plan = ingest_validated_jsonl(all_records)
    print(
        f"plan: pdps={len(plan.get('pdps') or [])} skus={len(plan.get('skus') or [])} "
        f"offers={len(plan.get('offers') or [])} seeds={len(plan.get('seeds') or [])} "
        f"skipped={plan.get('skipped')}"
    )
    print("primary ingestion: " + json.dumps(inspect_primary_plan(plan), sort_keys=True))
    _print_plan_identity(plan, limit=_effective_print_limit(args))
    if not args.apply:
        print("  DRY-RUN — re-run with --apply to ingest as depositable anchors.")
        return 0

    preflight = require_primary_plan(plan)
    from db.database import database  # noqa: E402
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        counts = await apply_ingest_plan(plan, batch_label=f"curated_brands:{len(brands)}", db=database, primary_readiness=True)
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
    p.add_argument(
        "--only-gtin",
        action="append",
        default=[],
        metavar="GTIN",
        help=(
            "keep only products carrying this GTIN (repeatable). Narrows a cohort to the exact "
            "products a case is about, so unrelated products whose merchant product_type this "
            "taxonomy cannot map do not block the apply. Matches the PDP's own barcode OR any of "
            "its merchant variants', with GS1 normalisation, so 13- and 14-digit spellings agree. "
            "Every requested GTIN must be valid and must match somewhere in the run, and a host "
            "matching none of them is an error, not an empty run. It narrows the PLAN only: the "
            "crawl still reads the whole feed and GTIN recovery still spends its budget first."
        ),
    )
    p.add_argument("--plan-print-limit", type=int, default=None, metavar="N",
                   help="print identity (product_key/gtin/category_path/variant ids/...) for at "
                        "most N planned PDPs; 0 prints every row. Printed for dry-run AND --apply. "
                        f"Default: {_PLAN_PRINT_DEFAULT} on a dry run, ALL rows with --apply")
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
