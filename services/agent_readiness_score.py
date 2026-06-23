"""ARO — Agent Readiness Score (slice 4).

Scores how AGENT-READABLE a product PDP is, purely from the signals the single-URL
audit fetcher (bd_cold_start_service.fetch_curated_audit_product) already captures
— so it's asset-light + compute-on-read (no new crawl, no heavy DB). This is the
merchant-facing diagnostic the brand pays to watch: an overall score, sub-scores,
and the actionable GAPS (the improvement suggestions = the value they buy).

Distinct from services/merchant_audit_readiness.py, which scores CATALOG quality
(will the audit return real per-SKU scores); this scores per-PDP agent-readability.

Honest framing: the score reflects what an agent-proxy could READ from the PDP's
structured data, and the gaps say what's missing — never more than we observed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _has_image(attrs: Dict[str, Any]) -> bool:
    images = attrs.get("images")
    if isinstance(images, dict):
        return bool(images.get("first_url") or images.get("count"))
    return bool(images)  # non-empty list


def _has_price(attrs: Dict[str, Any]) -> bool:
    if attrs.get("offers"):
        return True
    for v in attrs.get("variants") or []:
        if isinstance(v, dict) and v.get("price") is not None:
            return True
    return False


def _claim_evidence_coverage(
    evidence_profile: Optional[Dict[str, Any]],
) -> Optional[int]:
    """% of marketing claims that are SUBSTANTIATED (not 'unverified'). None when
    there are no claims to score (so it's omitted from the overall, not a 0)."""
    if not evidence_profile:
        return None
    claims = evidence_profile.get("claims") or []
    if not claims:
        return None
    substantiated = sum(
        1
        for c in claims
        if isinstance(c, dict)
        and str(c.get("substantiation_status") or "unverified").lower()
        == "substantiated"
    )
    return round(100 * substantiated / len(claims))


def compute_agent_readiness_score(
    audit_product: Optional[Dict[str, Any]],
    *,
    evidence_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return {overall, locatability, structured_data, claim_evidence, signals, gaps}.

    Pure + safe on partial/empty input. `evidence_profile` (the audit probe's
    graded claims) is optional — when absent, claim_evidence is None and the
    overall is the structured-data + locatability blend only.
    """
    ap = audit_product or {}
    attrs = ap.get("attributes_raw") or {}
    source = attrs.get("source")

    # 1. Locatability — how cleanly an agent-proxy could read the product data.
    if source == "shopify_native":
        locatability = 100  # structured product JSON (.json API)
    elif source == "pdp_metadata":
        locatability = 70  # schema.org JSON-LD / OpenGraph
    else:
        locatability = 0  # no structured product data resolved

    # 2. Structured-data completeness — agent-critical fields actually present.
    signals = {
        "title": bool(str(ap.get("title") or "").strip()),
        "description": bool(attrs.get("description") or attrs.get("body_html")),
        "brand": bool(str(ap.get("vendor") or "").strip()),
        "price": _has_price(attrs),
        "image": _has_image(attrs),
        "rating": bool(attrs.get("aggregateRating")),
    }
    structured_data = round(100 * sum(1 for v in signals.values() if v) / len(signals))

    # 3. Claim-to-evidence coverage (optional; needs the probe's evidence_profile).
    claim_evidence = _claim_evidence_coverage(evidence_profile)

    if claim_evidence is not None:
        parts = [(locatability, 0.3), (structured_data, 0.5), (claim_evidence, 0.2)]
    else:
        parts = [(locatability, 0.4), (structured_data, 0.6)]
    overall = round(sum(v * w for v, w in parts) / sum(w for _, w in parts))

    # gaps = the actionable improvement suggestions (the merchant-paid value).
    gaps: List[str] = []
    if locatability == 0:
        gaps.append(
            "No structured product data found — add schema.org JSON-LD Product "
            "markup so agents can read your PDP."
        )
    if not signals["price"]:
        gaps.append(
            "No structured price/offer — add a schema.org Offer (price + availability)."
        )
    if not signals["image"]:
        gaps.append("No product image in structured data.")
    if not signals["description"]:
        gaps.append("No product description in structured data.")
    if not signals["rating"]:
        gaps.append(
            "No review data — expose aggregateRating so agents can weigh social proof."
        )
    if claim_evidence is not None and claim_evidence < 100:
        gaps.append(
            "Some marketing claims are unsubstantiated — add evidence so agents "
            "can cite them safely."
        )

    return {
        "overall": overall,
        "locatability": locatability,
        "structured_data": structured_data,
        "claim_evidence": claim_evidence,
        "signals": signals,
        "gaps": gaps,
    }
