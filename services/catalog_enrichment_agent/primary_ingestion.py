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
    if counts["pdps"] and (not counts["skus"] or not counts["offers"]):
        reasons.append("no_usable_commerce_chain")
    return {
        "status": "blocked" if reasons else "ready_to_apply",
        "planned": counts,
        "unresolved_category_count": len(unresolved),
        "unresolved_product_keys": unresolved,
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
    incomplete = missing["pdps"] or missing["offers"] or unexplained_skus or (expected["skus"] and not counts["skus"])
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
