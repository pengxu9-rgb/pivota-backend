"""Explicit primary-ingestion acceptance; neither enumeration nor writes prove recall."""

from __future__ import annotations

import json
from typing import Any, Dict

from services.category_path_aliases import resolve


class PrimaryIngestionIncomplete(ValueError):
    def __init__(self, report: Dict[str, Any]):
        self.report = report
        # Queue.error is capped at 500 chars. Preserve reasons and core counts;
        # verbose product keys/adoption counters remain available on report.
        compact = {
            k: report[k]
            for k in ("status", "reasons", "planned", "missing", "unresolved_category_count", "skipped_records")
            if k in report
        }
        if "applied" in report:
            compact["applied"] = {k: report["applied"].get(k, 0) for k in ("pdps", "skus", "offers")}
        super().__init__("primary_ingestion_incomplete: " + json.dumps(compact, sort_keys=True))


def inspect_primary_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    unresolved = [p.get("product_key") for p in plan.get("pdps", []) if not resolve(p.get("category_path"))]
    counts = {name: len(plan.get(name) or []) for name in ("pdps", "skus", "offers")}
    reasons = []
    if unresolved:
        reasons.append("category_unresolved")
    if plan.get("skipped"):
        reasons.append("records_skipped")
    if not counts["pdps"]:
        reasons.append("no_products")
    if plan.get("skipped_reasons", {}).get("seller_of_record_conflict"):
        reasons.append("seller_of_record_conflict")
    # Aggregate counts can hide one product's missing children behind another's.
    sku_products = {s.get("sku_key"): s.get("product_key") for s in plan.get("skus", [])}
    linked_products = {
        o.get("product_key") for o in plan.get("offers", [])
        if o.get("sku_key") in sku_products
        and sku_products[o["sku_key"]] == o.get("product_key")
    }
    native_keys = set()
    for sku in plan.get("skus", []):
        payload = sku.get("sku_payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        if payload.get("variant_id_provenance") == "merchant_issued":
            native_keys.add(sku.get("sku_key"))
    native_products = {o.get("product_key") for o in plan.get("offers", [])
                       if o.get("sku_key") in native_keys
                       and sku_products.get(o.get("sku_key")) == o.get("product_key")}
    missing_native = [p.get("product_key") for p in plan.get("pdps", [])
                      if str(p.get("product_key") or "").startswith("ext:retailer:")
                      and p.get("product_key") not in native_products]
    if missing_native:
        reasons.append("no_native_retailer_commerce_chain")
    missing_chains = [p.get("product_key") for p in plan.get("pdps", [])
                      if p.get("product_key") not in linked_products]
    if missing_chains:
        reasons.append("no_usable_commerce_chain")
    return {
        "status": "blocked" if reasons else "ready_to_apply",
        "planned": counts,
        "unresolved_category_count": len(unresolved),
        "unresolved_product_keys": unresolved,
        "missing_commerce_product_keys": missing_chains,
        "missing_native_product_keys": missing_native,
        "skipped_records": int(plan.get("skipped") or 0),
        "reasons": reasons,
        "primary_search_verified": False,
        "shared_identity_verified": False,
    }


def require_primary_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    report = inspect_primary_plan(plan)
    if report["reasons"]:
        raise PrimaryIngestionIncomplete(report)
    return report


def require_primary_apply(plan_report: Dict[str, Any], applied: Dict[str, Any]) -> Dict[str, Any]:
    expected = plan_report["planned"]
    counts = {name: int(applied.get(name) or 0) for name in ("pdps", "skus", "offers")}
    missing = {name: max(0, expected[name] - counts[name]) for name in counts}
    # Only an explicitly counted natural-key deduplication explains fewer SKUs.
    # A lost native variant is a partial ingest even if a canonical SKU survived.
    deduped = max(0, int(applied.get("skus_deduped_same_identity") or 0))
    unexplained_skus = max(0, missing["skus"] - deduped)
    incomplete = int(applied.get("product_groups_failed") or 0) or missing["pdps"] or missing["offers"] or unexplained_skus or (expected["skus"] and not counts["skus"])
    report = {
        **plan_report,
        "status": "partial" if incomplete else "applied",
        "applied": dict(applied),
        "missing": missing,
    }
    if incomplete:
        report["reasons"] = ["incomplete_primary_writes"]
        raise PrimaryIngestionIncomplete(report)
    return report
