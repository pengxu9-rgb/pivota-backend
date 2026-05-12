"""P4.3 — derive canonical evidence + findings from the legacy
brand_report shape.

Strategy: don't touch the giant agent_center_bd_report_service.py.
Instead, the audit_run_worker's verifying stage hands the completed
brand_report to this builder, which extracts evidence_items +
readiness_findings rows.

This is a shadow-write — same data, derived view persisted in
canonical form. Phase 6 will retire the legacy JSONB once consumers
migrate to the canonical tables; until then the canonical tables
are a derived index over the source of truth.

Why shadow-write (not dual-write inside build_structured_report):
  - agent_center_bd_report_service.py is 3000+ lines and touched
    by many overlapping PRs (PR-7a/b/c/d/e + PR-8a-d, etc.)
  - Adding writes inline increases merge-conflict surface and
    couples canonical-table writes to every report-shape change
  - A separate builder lets P4 evolve the canonical schema without
    re-opening the giant report builder

What this module extracts (best-effort — missing fields silently
skip):
  - grounding_chunk evidence: per_product[*].evidence_quotes
  - competitor_mention evidence: cross_product_competitors
  - url_match evidence: per_product[*].raw.merchant_store_attribution
  - merchant_visible_via_retailers_only finding: verdict label +
    attribution score mismatch
  - category_visibility_low finding: avg_category_visibility < 40
  - integration_state_incomplete finding: merchant_view tracking
    block signals
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Confidence values per evidence/finding type. Pinned constants make
# it easy to audit confidence calibration in one place rather than
# spreading magic numbers through the extractor.
CONFIDENCE_EVIDENCE_HIGH = 90
CONFIDENCE_EVIDENCE_MEDIUM = 70
CONFIDENCE_EVIDENCE_LOW = 50

CONFIDENCE_FINDING_HIGH = 85
CONFIDENCE_FINDING_MEDIUM = 65


# =====================================================================
# Evidence extraction
# =====================================================================


def extract_evidence_items(
    brand_report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return a list of evidence-item dicts ready for
    insert_evidence_item. Each dict has:
      {evidence_type, payload, product_key?, confidence?}

    Order matters less than coverage — the writer iterates and
    inserts; sort order within evidence_items table is by
    created_at (set at insert time).
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(brand_report, dict):
        return out

    # Per-product evidence_quotes (PR-7e) → grounding_chunk evidence.
    for product in (brand_report.get("per_product") or []):
        if not isinstance(product, dict):
            continue
        product_key = _product_key_from_report(product)
        quotes = product.get("evidence_quotes") or []
        for q in quotes:
            if not isinstance(q, dict):
                continue
            excerpt = (q.get("excerpt_text") or "").strip()
            host = (q.get("source_host") or "").strip()
            if not excerpt:
                continue
            out.append({
                "evidence_type": "grounding_chunk",
                "payload": {
                    "host": host,
                    "source_title": q.get("source_title"),
                    "excerpt_text": excerpt[:1000],
                    "query": q.get("query"),
                    "attribution_path": q.get("attribution_path"),
                },
                "product_key": product_key,
                "confidence": (
                    CONFIDENCE_EVIDENCE_HIGH if host
                    else CONFIDENCE_EVIDENCE_MEDIUM
                ),
            })

    # Cross-product competitor mentions → competitor_mention evidence.
    competitors = brand_report.get("cross_product_competitors") or []
    for c in competitors:
        if not isinstance(c, dict):
            continue
        host = (c.get("host") or "").strip()
        if not host:
            continue
        out.append({
            "evidence_type": "competitor_mention",
            "payload": {
                "host": host,
                "times_cited": c.get("times_cited"),
            },
            "product_key": None,  # brand-level
            "confidence": CONFIDENCE_EVIDENCE_MEDIUM,
        })

    # Per-product merchant URL matches → url_match evidence.
    for product in (brand_report.get("per_product") or []):
        if not isinstance(product, dict):
            continue
        product_key = _product_key_from_report(product)
        raw = product.get("raw") or {}
        attribution = raw.get("merchant_store_attribution") or {}
        # Each run that hit a URL match becomes a row.
        for run in (attribution.get("raw_runs") or []):
            if not isinstance(run, dict):
                continue
            url_match = run.get("url_match")
            if not isinstance(url_match, dict):
                continue
            if not url_match.get("matched"):
                continue
            out.append({
                "evidence_type": "url_match",
                "payload": {
                    "matched_url": url_match.get("matched_url"),
                    "matched_in": url_match.get("matched_in"),
                    "query": run.get("query"),
                },
                "product_key": product_key,
                "confidence": CONFIDENCE_EVIDENCE_HIGH,
            })

    return out


# =====================================================================
# Finding extraction
# =====================================================================


def extract_findings(
    brand_report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return a list of finding dicts ready for insert_finding.
    Each dict has:
      {finding_type, payload, severity, product_key?,
       confidence?, short_summary?}
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(brand_report, dict):
        return out

    aggregate = brand_report.get("aggregate") or {}
    avg_vis = aggregate.get("avg_visibility")
    avg_attr = aggregate.get("avg_attribution")
    avg_cat = aggregate.get("avg_category_visibility")
    verdict_label = aggregate.get("brand_verdict_label") or ""

    # Paradox finding — "visible via retailers" with weak attribution.
    # The hand-written Grüns report led with this; PR-8a executive
    # summary builder consumes this finding to fire the paradox
    # narrative template.
    visible_via_retailers = (
        "VISIBLE VIA RETAILERS" in str(verdict_label).upper()
    )
    weak_attribution = (
        isinstance(avg_attr, (int, float)) and avg_attr < 30
    )
    if visible_via_retailers and weak_attribution:
        out.append({
            "finding_type": "merchant_visible_via_retailers_only",
            "severity": "high",
            "payload": {
                "verdict_label": verdict_label,
                "avg_visibility": avg_vis,
                "avg_attribution": avg_attr,
                "avg_category_visibility": avg_cat,
            },
            "confidence": CONFIDENCE_FINDING_HIGH,
            "short_summary": (
                f"Brand surfaces via editorial / retailer mentions "
                f"(visibility={avg_vis}) but first-party attribution "
                f"is weak ({avg_attr})."
            ),
        })

    # Low category visibility finding.
    if isinstance(avg_cat, (int, float)) and avg_cat < 40:
        out.append({
            "finding_type": "category_visibility_low",
            "severity": "medium" if avg_cat >= 20 else "high",
            "payload": {
                "avg_category_visibility": avg_cat,
                "products_with_category_data": aggregate.get(
                    "products_succeeded",
                ),
            },
            "confidence": CONFIDENCE_FINDING_HIGH,
            "short_summary": (
                f"Category-open queries surface the brand only "
                f"{int(avg_cat)}% of the time."
            ),
        })

    # First-party PDP indexing gap — Pivota canonical URLs in use.
    # When the audit used the Pivota canonical PDP for any product,
    # that signals the merchant's own URL was unavailable / not
    # indexable. Phase 5 verifier `pdp_in_sitemap` will produce
    # paired verification evidence.
    audited_via_pivota = brand_report.get(
        "audited_via_pivota_canonical",
    ) or []
    # NOTE: that field on the brand_report itself isn't populated
    # today (it's on the audit response, not the report). Check
    # per-product url_source instead.
    pivota_used_count = 0
    for p in (brand_report.get("per_product") or []):
        if not isinstance(p, dict):
            continue
        mv = p.get("merchant_view") or {}
        headline = mv.get("headline") or {}
        if headline.get("audited_via_pivota_canonical"):
            pivota_used_count += 1
    if pivota_used_count > 0:
        out.append({
            "finding_type": "first_party_pdp_indexing_gap",
            "severity": "medium",
            "payload": {
                "products_audited_via_pivota_canonical": (
                    pivota_used_count
                ),
                "total_products": aggregate.get("products_succeeded"),
            },
            "confidence": CONFIDENCE_FINDING_MEDIUM,
            "short_summary": (
                f"{pivota_used_count} of "
                f"{aggregate.get('products_succeeded') or '?'} "
                f"products audited against Pivota canonical PDP "
                f"(merchant's own URL not available / not indexable)."
            ),
        })

    # Integration state incomplete — check the first product's
    # tracking block. Integration state is merchant-level so all
    # per_product reports carry the same value.
    per_product = brand_report.get("per_product") or []
    if per_product and isinstance(per_product[0], dict):
        mv = per_product[0].get("merchant_view") or {}
        tracking = mv.get("tracking") or {}
        integration = tracking.get("integration_state") or {}
        if isinstance(integration, dict):
            phase_0_complete = integration.get("phase_0_complete")
            if phase_0_complete is False:
                out.append({
                    "finding_type": "integration_state_incomplete",
                    "severity": "critical",
                    "payload": dict(integration),
                    "confidence": CONFIDENCE_FINDING_HIGH,
                    "short_summary": (
                        "Pivota integration incomplete — auditing "
                        "results reflect partial pipeline. Complete "
                        "onboarding to unlock the full action loop."
                    ),
                })

    return out


# =====================================================================
# Persist — calls the P4.2 accessors
# =====================================================================


async def persist_canonical_evidence(
    *, audit_run_id: str, brand_report: Dict[str, Any],
) -> Dict[str, int]:
    """Extract + persist evidence_items + readiness_findings for
    one brand_report. Best-effort: each write goes through
    insert_evidence_item / insert_finding which already swallow
    persistence errors. Returns a count summary for the worker's
    partial_result tracking.
    """
    from db.audit_evidence import insert_evidence_item, insert_finding

    summary = {
        "evidence_items_inserted": 0,
        "evidence_items_failed": 0,
        "findings_inserted": 0,
        "findings_failed": 0,
    }

    for ev in extract_evidence_items(brand_report):
        try:
            new_id = await insert_evidence_item(
                audit_run_id=audit_run_id,
                evidence_type=ev["evidence_type"],
                payload=ev["payload"],
                product_key=ev.get("product_key"),
                confidence=ev.get("confidence"),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "persist_canonical_evidence: insert_evidence_item "
                "raised for audit=%s: %s",
                audit_run_id, str(exc)[:200],
            )
            new_id = None
        if new_id is None:
            summary["evidence_items_failed"] += 1
        else:
            summary["evidence_items_inserted"] += 1

    for finding in extract_findings(brand_report):
        try:
            new_id = await insert_finding(
                audit_run_id=audit_run_id,
                finding_type=finding["finding_type"],
                payload=finding["payload"],
                severity=finding.get("severity"),
                product_key=finding.get("product_key"),
                confidence=finding.get("confidence"),
                short_summary=finding.get("short_summary"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "persist_canonical_evidence: insert_finding "
                "raised for audit=%s: %s",
                audit_run_id, str(exc)[:200],
            )
            new_id = None
        if new_id is None:
            summary["findings_failed"] += 1
        else:
            summary["findings_inserted"] += 1

    return summary


# =====================================================================
# Internal helpers
# =====================================================================


def _product_key_from_report(
    product_report: Dict[str, Any],
) -> Optional[str]:
    """Extract the product_key from a per-product report.
    Different shapes exist across the various PRs that touched
    build_structured_report; check all known locations."""
    # Most reliable: explicit product_key field if it exists
    pk = product_report.get("product_key")
    if isinstance(pk, str) and pk:
        return pk
    # Fall back to (platform, source_product_id) tuple if present
    platform = product_report.get("platform")
    source_id = product_report.get("source_product_id")
    if platform and source_id:
        return f"{platform}::{source_id}"
    # Fall back to merchant_view.headline.product_key
    mv = product_report.get("merchant_view") or {}
    headline = mv.get("headline") or {}
    pk = headline.get("product_key")
    if isinstance(pk, str) and pk:
        return pk
    return None
