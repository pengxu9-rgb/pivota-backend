"""Explicit primary-ingestion acceptance; neither enumeration nor writes prove recall."""

from __future__ import annotations

import json
from typing import Any, Dict

from services.category_path_aliases import resolve


#: Queue.error is capped at 500 chars (catalog_onboard_queue); the message must fit it.
QUEUE_ERROR_CAP = 500

#: The apply counters that explain a shortfall. Printed with the report, never only logged.
APPLY_GAP_COUNTERS = (
    "pdps_skipped_identity", "pdps_skipped_insert", "products_fully_skipped", "product_groups_failed",
    "skus_identity_conflict", "offers_dropped_for_refused_sku", "offers_skipped", "offers_skipped_insert",
)


def skipped_reason_key(row: Dict[str, Any]) -> str:
    """`identity_skip:brand_host_fragmentation`, `insert_failed`, ... — one tally bucket per cause."""
    reason = str(row.get("reason") or "unknown")
    matcher = row.get("matcher")
    return f"{reason}:{matcher}" if reason == "identity_skip" and matcher else reason


def skipped_by_reason(rows: Any) -> Dict[str, int]:
    tally: Dict[str, int] = {}
    for row in rows or []:
        if isinstance(row, dict):
            key = skipped_reason_key(row)
            tally[key] = tally.get(key, 0) + 1
    return tally


class PrimaryIngestionIncomplete(ValueError):
    def __init__(self, report: Dict[str, Any]):
        self.report = report
        # Queue.error is capped at 500 chars. Preserve reasons and core counts;
        # verbose product keys/adoption counters and the per-row `skipped_products`
        # remain available on report (the CLI prints it whole before raising).
        compact = {
            k: report[k]
            for k in ("status", "reasons", "planned", "missing", "unresolved_category_count", "skipped_records")
            if k in report
        }
        if "applied" in report:
            compact["applied"] = {k: report["applied"].get(k, 0) for k in ("pdps", "skus", "offers")}
        tally = skipped_by_reason(report.get("skipped_products"))
        if tally:
            compact["skipped_by_reason"] = tally
        message = "primary_ingestion_incomplete: " + json.dumps(compact, sort_keys=True)
        if len(message) > QUEUE_ERROR_CAP and "skipped_by_reason" in compact:
            # Never trade a core count for the tally: fall back to its total.
            compact["skipped_by_reason"] = {"total": sum(tally.values())}
            message = "primary_ingestion_incomplete: " + json.dumps(compact, sort_keys=True)
        super().__init__(message)


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
    applied_counts = dict(applied)
    # Which planned products did not land, and why — hoisted out of `applied` so the
    # report names them beside `missing` instead of burying a list among counters.
    skipped = [r for r in (applied_counts.pop("skipped_products", None) or []) if isinstance(r, dict)]
    report = {
        **plan_report,
        "status": "partial" if incomplete else "applied",
        "applied": applied_counts,
        "missing": missing,
        "skipped_products": skipped,
        "skipped_by_reason": skipped_by_reason(skipped),
        "apply_gap_counters": {k: applied_counts.get(k) for k in APPLY_GAP_COUNTERS if k in applied_counts},
    }
    if incomplete:
        report["reasons"] = ["incomplete_primary_writes"]
        raise PrimaryIngestionIncomplete(report)
    return report
