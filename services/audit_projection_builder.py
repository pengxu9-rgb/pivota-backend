"""P4.5 — 5-audience projection builder.

Reads from the canonical tables (evidence_items, readiness_findings,
action_plan_items) populated by P4.3 + P4.4 and builds 5 distinct
audience-specific shapes. Each shape is cached in report_projections
keyed by (audit_run_id, audience); GET /api/audits/{id}?audience=X
reads from the cache.

The 5 audiences:
  - employee_bd: BD operator's full report — everything visible
  - merchant: action-queue-focused; emphasizes "what you need to
    do" rather than "what we found"; suppresses internal cost
    detail
  - internal_ops: cost + latency dashboard; minimal content
  - pivota_pdp_feed: PDP rendering hints for agent.pivota.cc;
    optimized for the JSON-LD-ish ingestion path
  - frontend_agent_feed: agent-discoverable shape; structured for
    LLM ingestion (clear claim-evidence pairs)

Why not just expose the canonical tables directly?
  - Each audience has different sensitivity (cost detail → internal,
    publisher contact list → employee_bd only, etc.)
  - Renderer logic is centralized here rather than spread across
    per-portal frontends
  - Pre-rendering at audit-completion time keeps GET /api/audits
    latency low (cached read, not on-demand assembly)

Builder version bumps:
  - When the projection logic changes in a way that should
    invalidate cached projections, bump _BUILDER_VERSION. A
    follow-up reaper can find report_projections rows with
    builder_version < current and re-render.
  - Default: keep old projections; never silently re-render.
    Operators explicitly trigger re-render via a script.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from db._jsonb_safe import coerce_jsonb_to_dict

logger = logging.getLogger(__name__)


# Bump when the projection shape changes in a way that callers
# would notice. Semantic versioning style: major.minor.
_BUILDER_VERSION = "1.0.0"


# Audience constants (mirror db.audit_evidence values; kept as
# strings here so this module doesn't have to import the accessor
# module just to validate).
AUDIENCE_EMPLOYEE_BD = "employee_bd"
AUDIENCE_MERCHANT = "merchant"
AUDIENCE_INTERNAL_OPS = "internal_ops"
AUDIENCE_PIVOTA_PDP_FEED = "pivota_pdp_feed"
AUDIENCE_FRONTEND_AGENT_FEED = "frontend_agent_feed"

VALID_AUDIENCES = frozenset({
    AUDIENCE_EMPLOYEE_BD, AUDIENCE_MERCHANT,
    AUDIENCE_INTERNAL_OPS, AUDIENCE_PIVOTA_PDP_FEED,
    AUDIENCE_FRONTEND_AGENT_FEED,
})


# =====================================================================
# Per-audience builders
# =====================================================================


def build_employee_bd_projection(
    *,
    evidence: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    audit_run_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Full report for BD operators. Includes everything visible —
    raw evidence, all findings, all actions, full cost detail
    when available."""
    return {
        "audience": AUDIENCE_EMPLOYEE_BD,
        "builder_version": _BUILDER_VERSION,
        "audit_run_id": (
            audit_run_row.get("run_id") if audit_run_row else None
        ),
        "merchant_id": (
            audit_run_row.get("merchant_id") if audit_run_row else None
        ),
        "verdict_labels": (
            audit_run_row.get("verdict_labels") if audit_run_row else []
        ),
        "scores": {
            "visibility_avg": (
                audit_run_row.get("visibility_score_avg")
                if audit_run_row else None
            ),
            "attribution_avg": (
                audit_run_row.get("attribution_score_avg")
                if audit_run_row else None
            ),
            "category_visibility_avg": (
                audit_run_row.get("category_visibility_score_avg")
                if audit_run_row else None
            ),
        },
        "findings": [_finding_for_bd(f) for f in findings],
        "evidence": [_evidence_for_bd(e) for e in evidence],
        "actions": [_action_for_bd(a) for a in actions],
        "cost_summary": (
            audit_run_row.get("cost_summary_jsonb")
            if audit_run_row else None
        ),
    }


def build_merchant_projection(
    *,
    evidence: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    audit_run_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merchant-portal shape. Emphasizes the action queue:
    'here's what you need to do', not 'here's everything we found'.
    Filters:
      - Drop low-confidence evidence (<60)
      - Hide cost detail entirely
      - Surface only findings with severity >= medium
      - Re-order actions by severity (critical first) then phase
    """
    visible_findings = [
        f for f in findings if f.get("severity") in (
            "critical", "high", "medium",
        )
    ]
    # Sort actions: critical → high → medium → low, then by phase
    # (earlier phases first).
    sorted_actions = sorted(
        actions, key=_severity_phase_sort_key,
    )
    return {
        "audience": AUDIENCE_MERCHANT,
        "builder_version": _BUILDER_VERSION,
        "audit_run_id": (
            audit_run_row.get("run_id") if audit_run_row else None
        ),
        "headline_score": (
            audit_run_row.get("visibility_score_avg")
            if audit_run_row else None
        ),
        "verdict": (
            audit_run_row.get("verdict_labels")
            if audit_run_row else []
        ),
        "findings_summary": [
            {
                "type": f.get("finding_type"),
                "severity": f.get("severity"),
                "summary": f.get("short_summary"),
            } for f in visible_findings
        ],
        "action_queue": [_action_for_merchant(a) for a in sorted_actions],
        # Quote-boxes the merchant can show in their own marketing
        # ("This brand was named by Forbes Vetted as ..."). Only
        # high-confidence grounding chunks.
        "evidence_quotes": [
            # coerce_jsonb_to_dict defends against the `databases`
            # library returning the JSONB column as a raw JSON
            # string instead of a parsed dict (same race PR #482
            # fixed at the executor hydration boundary).
            {
                "host": (coerce_jsonb_to_dict(e.get("payload_jsonb")) or {}).get("host"),
                "excerpt": (coerce_jsonb_to_dict(e.get("payload_jsonb")) or {}).get(
                    "excerpt_text",
                ),
                "query": (coerce_jsonb_to_dict(e.get("payload_jsonb")) or {}).get("query"),
            }
            for e in evidence
            if e.get("evidence_type") == "grounding_chunk"
            and (e.get("confidence") or 0) >= 60
        ],
    }


def build_internal_ops_projection(
    *,
    evidence: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    audit_run_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cost + latency dashboard. Minimal content; mostly counters.
    Renderer is the ops dashboard (Grafana-style); content not
    expected to ship to merchants."""
    return {
        "audience": AUDIENCE_INTERNAL_OPS,
        "builder_version": _BUILDER_VERSION,
        "audit_run_id": (
            audit_run_row.get("run_id") if audit_run_row else None
        ),
        "merchant_id": (
            audit_run_row.get("merchant_id") if audit_run_row else None
        ),
        "evidence_count": len(evidence),
        "findings_count": len(findings),
        "actions_count": len(actions),
        "evidence_by_type": _count_by_key(evidence, "evidence_type"),
        "findings_by_type": _count_by_key(findings, "finding_type"),
        "findings_by_severity": _count_by_key(findings, "severity"),
        "actions_by_severity": _count_by_key(actions, "severity"),
        "actions_by_lever": _count_by_key(actions, "lever"),
        "cost_summary": (
            audit_run_row.get("cost_summary_jsonb")
            if audit_run_row else None
        ),
        "stage_timing": {
            "requested_at": (
                audit_run_row.get("requested_at")
                if audit_run_row else None
            ),
            "completed_at": (
                audit_run_row.get("completed_at")
                if audit_run_row else None
            ),
        },
    }


def build_pivota_pdp_feed_projection(
    *,
    evidence: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    audit_run_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """PDP rendering hints for agent.pivota.cc. Optimized for the
    JSON-LD-ish ingestion path — minimal nesting, stable keys.
    Phase 5 verify loop's `pdp_renders` verifier will read this
    shape to decide whether the rendered PDP matches the audit's
    truth."""
    return {
        "audience": AUDIENCE_PIVOTA_PDP_FEED,
        "builder_version": _BUILDER_VERSION,
        "audit_run_id": (
            audit_run_row.get("run_id") if audit_run_row else None
        ),
        "merchant_id": (
            audit_run_row.get("merchant_id") if audit_run_row else None
        ),
        # Citation list for the "as seen in" section of the
        # canonical PDP. Only competitor_mention + grounding_chunk
        # rows with a host.
        "citations": [
            # coerce_jsonb_to_dict — see comment in
            # build_merchant_projection's evidence_quotes block.
            {
                "host": (coerce_jsonb_to_dict(e.get("payload_jsonb")) or {}).get("host"),
                "times_cited": (coerce_jsonb_to_dict(e.get("payload_jsonb")) or {}).get(
                    "times_cited",
                ),
                "evidence_type": e.get("evidence_type"),
            }
            for e in evidence
            if e.get("evidence_type") in (
                "grounding_chunk", "competitor_mention",
            )
            and (coerce_jsonb_to_dict(e.get("payload_jsonb")) or {}).get("host")
        ],
        # Visibility scores for the PDP's "agent commerce readiness"
        # badge.
        "scores": {
            "visibility": (
                audit_run_row.get("visibility_score_avg")
                if audit_run_row else None
            ),
            "category_visibility": (
                audit_run_row.get("category_visibility_score_avg")
                if audit_run_row else None
            ),
        },
    }


def build_frontend_agent_feed_projection(
    *,
    evidence: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    audit_run_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Agent-discoverable shape. Structured for LLM ingestion —
    clear claim-evidence pairs so a downstream agent (ChatGPT
    plugin, Claude tool, etc.) can verify our claims by reading
    the evidence directly. Phase 5 verify loop's `frontend_agent_cite`
    verifier will check whether external agents actually consume
    this shape."""
    return {
        "audience": AUDIENCE_FRONTEND_AGENT_FEED,
        "builder_version": _BUILDER_VERSION,
        "audit_run_id": (
            audit_run_row.get("run_id") if audit_run_row else None
        ),
        "merchant_id": (
            audit_run_row.get("merchant_id") if audit_run_row else None
        ),
        # Each finding paired with its supporting evidence. The
        # finding's payload has the claim; the evidence rows have
        # the citations. An LLM can verify the claim by reading
        # the evidence URLs.
        "claims": [
            {
                "claim_type": f.get("finding_type"),
                "claim_summary": f.get("short_summary"),
                "claim_payload": f.get("payload_jsonb"),
                "confidence": f.get("confidence"),
            }
            for f in findings
        ],
        "supporting_evidence": [
            {
                "type": e.get("evidence_type"),
                "payload": e.get("payload_jsonb"),
                "confidence": e.get("confidence"),
            }
            for e in evidence
        ],
    }


# =====================================================================
# Dispatch
# =====================================================================


_BUILDERS = {
    AUDIENCE_EMPLOYEE_BD: build_employee_bd_projection,
    AUDIENCE_MERCHANT: build_merchant_projection,
    AUDIENCE_INTERNAL_OPS: build_internal_ops_projection,
    AUDIENCE_PIVOTA_PDP_FEED: build_pivota_pdp_feed_projection,
    AUDIENCE_FRONTEND_AGENT_FEED: build_frontend_agent_feed_projection,
}


def build_projection(
    *,
    audience: str,
    evidence: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    audit_run_row: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Dispatch to the right builder for the audience. Returns None
    for unknown audiences (caller should 404)."""
    builder = _BUILDERS.get(audience)
    if builder is None:
        return None
    return builder(
        evidence=evidence,
        findings=findings,
        actions=actions,
        audit_run_row=audit_run_row,
    )


async def build_and_persist_all_projections(
    *, audit_run_id: str,
) -> Dict[str, int]:
    """Load canonical data + build + upsert all 5 projections.
    Called at audit-completion time (the audit_run_worker hooks
    this in P4.5 wiring follow-up). Returns count summary."""
    from db.audit_evidence import (
        list_actions_for_run, list_evidence_for_run,
        list_findings_for_run, upsert_projection,
    )
    from db.merchant_audit_runs import fetch_audit_run_by_id

    summary = {
        "projections_built": 0,
        "projections_failed": 0,
    }

    audit_row = await fetch_audit_run_by_id(run_id=audit_run_id)
    evidence = await list_evidence_for_run(audit_run_id=audit_run_id)
    findings = await list_findings_for_run(audit_run_id=audit_run_id)
    actions = await list_actions_for_run(audit_run_id=audit_run_id)

    for audience in VALID_AUDIENCES:
        try:
            payload = build_projection(
                audience=audience,
                evidence=evidence,
                findings=findings,
                actions=actions,
                audit_run_row=audit_row,
            )
            if payload is None:
                summary["projections_failed"] += 1
                continue
            await upsert_projection(
                audit_run_id=audit_run_id,
                audience=audience,
                payload=payload,
                builder_version=_BUILDER_VERSION,
            )
            summary["projections_built"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "build_and_persist_all_projections: failed "
                "audience=%s audit_run=%s: %s",
                audience, audit_run_id, str(exc)[:200],
            )
            summary["projections_failed"] += 1

    return summary


# =====================================================================
# Per-row shapers — small helpers that pick which fields each
# audience sees from a canonical row.
# =====================================================================


def _evidence_for_bd(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "evidence_id": row.get("evidence_id"),
        "evidence_type": row.get("evidence_type"),
        "product_key": row.get("product_key"),
        "payload": row.get("payload_jsonb"),
        "confidence": row.get("confidence"),
        "probe_run_id": row.get("probe_run_id"),
    }


def _finding_for_bd(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "finding_id": row.get("finding_id"),
        "finding_type": row.get("finding_type"),
        "severity": row.get("severity"),
        "product_key": row.get("product_key"),
        "short_summary": row.get("short_summary"),
        "payload": row.get("payload_jsonb"),
        "confidence": row.get("confidence"),
    }


def _action_for_bd(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "action_id": row.get("action_id"),
        "severity": row.get("severity"),
        "lever": row.get("lever"),
        "title": row.get("title"),
        "body": row.get("body"),
        "owner": row.get("owner"),
        "kpi_to_track": row.get("kpi_to_track"),
        "expected_outcome": row.get("expected_outcome"),
        "phase": row.get("phase"),
        "depends_on": row.get("depends_on"),
        "materialized_task_id": row.get("materialized_task_id"),
        "product_key": row.get("product_key"),
    }


def _action_for_merchant(row: Dict[str, Any]) -> Dict[str, Any]:
    """Merchant-facing action: hide internal fields (parent_finding_id,
    depends_on) that aren't actionable for the operator."""
    return {
        "action_id": row.get("action_id"),
        "severity": row.get("severity"),
        "lever": row.get("lever"),
        "title": row.get("title"),
        "body": row.get("body"),
        "owner": row.get("owner"),
        "expected_outcome": row.get("expected_outcome"),
        "phase": row.get("phase"),
        "materialized_task_id": row.get("materialized_task_id"),
    }


# =====================================================================
# Sort helpers
# =====================================================================


_SEVERITY_ORDER = {
    "critical": 0, "high": 1, "medium": 2, "low": 3,
}

_PHASE_ORDER = {
    "week_1_to_4": 0, "week_4_to_12": 1, "week_12_to_24": 2,
}


def _severity_phase_sort_key(action: Dict[str, Any]) -> tuple:
    """Sort actions: critical first, then by phase, then by title.
    Used by the merchant projection's action_queue ordering."""
    sev = action.get("severity") or "medium"
    phase = action.get("phase")
    title = action.get("title") or ""
    return (
        _SEVERITY_ORDER.get(sev, 99),
        _PHASE_ORDER.get(phase, 99),  # unphased actions sort last
        title.lower(),
    )


def _count_by_key(
    rows: List[Dict[str, Any]], key: str,
) -> Dict[str, int]:
    """Group + count rows by a field. Used by the internal_ops
    projection's evidence_by_type / findings_by_severity / etc."""
    out: Dict[str, int] = {}
    for r in rows or []:
        v = r.get(key) or "_unknown"
        out[v] = out.get(v, 0) + 1
    return out
