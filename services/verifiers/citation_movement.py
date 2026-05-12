"""P5.6 verifier: public_llm_citation_movement.

Re-runs a subset of the audit's LLM probes 30 days post-audit and
compares citation counts to the original baseline. Detects whether
Pivota's interventions (sitemap submit, content briefs, etc.)
moved the merchant's visibility metric in real LLM channels.

Why category_visibility_test only:
  - It's the cleanest signal of organic visibility (category-open
    queries that DON'T name the brand — measures whether the
    brand surfaces unprompted)
  - merchant_store_attribution_test is gated on the merchant URL,
    which doesn't naturally drift over 30 days
  - open_product_visibility_test overlaps with category_visibility

The verifier reads the baseline `category_visibility_score` from
the audit's `report_jsonb`, runs ONE fresh probe with the same
parameters, and compares.

Scheduling: this verifier is the reason we have `not_before` in
verification_runs (P5.1). At audit completion (P5.7), the enqueue
sets `not_before = completed_at + 30 days`. The worker's
claim_next_pending_verification skips rows where not_before > NOW(),
so the row sits in the queue for ~30 days before being claimed.

Evidence:
  - baseline_category_visibility_score
  - reprobed_category_visibility_score
  - score_delta (reprobed - baseline)
  - score_movement: improved | unchanged | regressed
  - reprobed_at
  - days_since_audit

Status routing:
  - succeeded: reprobed score >= baseline (no regression — Pivota's
    interventions held or improved)
  - failed (retryable): probe call failed transiently (rate limit,
    timeout)
  - blocked: no baseline available (audit's report_jsonb missing
    category_visibility data), product context unresolvable, or
    LLM provider cost-capped (no budget for the re-probe)

Note: a regression (reprobed < baseline) is NOT a verifier
failure — the verifier's job is to MEASURE movement, not judge
it. The merchant's projection layer surfaces the delta to ops
who decide whether to action. Both 'improved' and 'regressed'
return status=succeeded with the delta in evidence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from services.verification_run_worker import (
    VerifierContext, VerifierResult, register_verifier,
)
from services.verifiers._product_context import load_product_context

logger = logging.getLogger(__name__)


# Per-probe limit. Default to 3 runs (same as the original audit)
# but the verifier can choose to use fewer if cost is a concern.
_REPROBE_MAX_RUNS = 3

# Provider for the re-probe. Use "auto" so the orchestrator picks
# the cheapest option (per the per-merchant daily cost cap +
# global cap from PR-7).
_REPROBE_PROVIDER = "auto"


async def _extract_baseline_score(
    *, audit_run_id: str, product_key: str,
) -> Optional[int]:
    """Read the baseline category_visibility_score from the audit's
    report_jsonb. Returns None if the audit's report or the score
    isn't present (audit may have failed, or category_visibility
    was skipped at audit time)."""
    try:
        from db.merchant_audit_runs import fetch_audit_run_by_id
        row = await fetch_audit_run_by_id(run_id=audit_run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_extract_baseline_score: fetch audit run failed for %s: %s",
            audit_run_id, str(exc)[:200],
        )
        return None
    if row is None:
        return None
    report = row.get("report_jsonb") or {}
    if not isinstance(report, dict):
        return None
    # Walk per_product looking for the matching product_key.
    # Different shapes hold the score depending on the PR that
    # touched build_structured_report. Match by EITHER explicit
    # product_key OR by composing (platform::source_product_id).
    for p in (report.get("per_product") or []):
        if not isinstance(p, dict):
            continue
        pk = p.get("product_key")
        platform = p.get("platform")
        sid = p.get("source_product_id")
        composed = (
            f"{platform}::{sid}" if (platform and sid) else None
        )
        matches = (pk == product_key) or (composed == product_key)
        if not matches:
            continue
        # Found the product. Pull the score.
        raw = p.get("raw") or {}
        cat = raw.get("category_visibility") or {}
        scores = cat.get("scores") or {}
        score = scores.get("category_visibility_score")
        if isinstance(score, (int, float)):
            return int(score)
        # Fallback: merchant_view's headline score (aggregated)
        mv = p.get("merchant_view") or {}
        headline = mv.get("headline") or {}
        agg_score = headline.get("category_visibility_score")
        if isinstance(agg_score, (int, float)):
            return int(agg_score)
    return None


def _classify_movement(
    *, baseline: int, reprobed: int,
) -> str:
    """Bucket the delta into improved / unchanged / regressed.
    Threshold ±5 — smaller deltas are within probe noise."""
    delta = reprobed - baseline
    if delta >= 5:
        return "improved"
    if delta <= -5:
        return "regressed"
    return "unchanged"


async def run_citation_movement(
    context: VerifierContext,
) -> VerifierResult:
    product = await load_product_context(
        audit_run_id=context.audit_run_id,
        product_key=context.product_key,
    )
    if product is None:
        return VerifierResult(
            status="blocked",
            error_message="product context unresolvable",
            evidence_jsonb={"resolved_product": False},
        )

    baseline = await _extract_baseline_score(
        audit_run_id=context.audit_run_id,
        product_key=context.product_key,
    )
    if baseline is None:
        return VerifierResult(
            status="blocked",
            error_message="no_baseline_score_in_audit_report",
            evidence_jsonb={
                "audit_run_id": context.audit_run_id,
                "product_key": context.product_key,
                "merchant_id": product.get("merchant_id"),
            },
        )

    # Re-run a single category_visibility probe. Same product info
    # as the original audit; provider=auto so the orchestrator
    # cost-routes.
    title = product.get("title") or ""
    try:
        from services.agent_center_llm_client import probe
        result = await probe(
            scan_mode="category_visibility_test",
            scan_target_id=context.product_key or "",
            merchant_id=product["merchant_id"],
            store_id=product["merchant_id"],
            context={
                "product_title": title,
                "merchant_brand": product.get("brand"),
                "product_type": None,  # category context
            },
            provider=_REPROBE_PROVIDER,
            max_runs=_REPROBE_MAX_RUNS,
        )
    except Exception as exc:  # noqa: BLE001
        return VerifierResult(
            status="failed",
            error_message=f"reprobe_failed: {exc!r}"[:500],
            evidence_jsonb={
                "audit_run_id": context.audit_run_id,
                "baseline_category_visibility_score": baseline,
                "product_key": context.product_key,
            },
        )

    # Extract the re-probed score. The probe result's shape matches
    # the V1 contract: {scores: {category_visibility_score: int, ...}}.
    scores = (result or {}).get("scores") or {}
    reprobed = scores.get("category_visibility_score")
    if not isinstance(reprobed, (int, float)):
        return VerifierResult(
            status="failed",
            error_message="reprobe_missing_score",
            evidence_jsonb={
                "audit_run_id": context.audit_run_id,
                "baseline_category_visibility_score": baseline,
                "probe_result_scores": scores,
            },
        )
    reprobed = int(reprobed)

    delta = reprobed - baseline
    movement = _classify_movement(baseline=baseline, reprobed=reprobed)

    evidence: Dict[str, Any] = {
        "audit_run_id": context.audit_run_id,
        "product_key": context.product_key,
        "merchant_id": product["merchant_id"],
        "baseline_category_visibility_score": baseline,
        "reprobed_category_visibility_score": reprobed,
        "score_delta": delta,
        "score_movement": movement,
        "reprobed_at": datetime.now(timezone.utc).isoformat(),
        "reprobe_max_runs": _REPROBE_MAX_RUNS,
    }

    # Per docstring: the verifier MEASURES movement; both
    # 'improved' and 'regressed' return succeeded. The merchant /
    # ops projections surface the delta separately.
    return VerifierResult(
        status="succeeded",
        evidence_jsonb=evidence,
    )


register_verifier(
    "public_llm_citation_movement", run_citation_movement,
)
