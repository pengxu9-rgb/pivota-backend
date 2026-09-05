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
AUDIENCE_REVENUE_RECOVERY = "revenue_recovery"
AUDIENCE_PUBLIC_ANONYMOUS = "public_anonymous"

VALID_AUDIENCES = frozenset({
    AUDIENCE_EMPLOYEE_BD, AUDIENCE_MERCHANT,
    AUDIENCE_INTERNAL_OPS, AUDIENCE_PIVOTA_PDP_FEED,
    AUDIENCE_FRONTEND_AGENT_FEED,
    AUDIENCE_REVENUE_RECOVERY,
    AUDIENCE_PUBLIC_ANONYMOUS,
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



# =====================================================================
# C2: revenue_recovery + public_anonymous
# =====================================================================

# Evidence types produced WITHOUT a model call AND actually written by
# something today. An ALLOWLIST, for the same reason _SHARE_ALLOWED_TOP_KEYS
# is one: a denylist on an unauthenticated surface ships every future type by
# default, and the share view already leaked registry pitch emails that way.
#
# ONLY TYPES WITH A LIVE WRITER. An earlier cut also allowlisted
# commerce_integration_authorization, commerce_return_policy and
# commerce_after_sales_review — none of which any code writes. Pre-approving a
# payload shape that does not exist is the same failure one level up: whoever
# implements it later will not know their payload became public. Two of those
# names are where crawled third-party prose would land, and
# commerce_integration_authorization's shape (see
# services/commerce_capability_resolver.py) carries authorization_scope, i.e.
# a merchant's commercial relationship with Pivota. Adding a type here means
# reading its writer first.
#
# Excluded, each for a reason: grounding_chunk and competitor_mention are model
# output whose excerpts are third-party prose; url_match is derived from
# grounding; missing_signal cannot be told from its model-derived twin by type
# alone; industry_stat is static copy, not a fact about this store.
_DETERMINISTIC_EVIDENCE_TYPES = frozenset({
    "acceptance_signal",
    "commerce_platform",
    "commerce_checkout_route",
    "commerce_cartability",
})

# evidence_items.evidence_level is a COLUMN, and these are its only values
# (db/audit_evidence.py). Note "detected" here means "observed indirectly",
# NOT a boolean — it is a strength enum, and the weaker of the two.
_EVIDENCE_LEVELS = frozenset({"detected", "tested"})

_STAGE_GET_SELECTED = "get_selected"
_STAGE_GET_CITED = "get_cited"
_STAGE_CONVERT_SALES = "convert_sales"
_STAGES = (_STAGE_GET_SELECTED, _STAGE_GET_CITED, _STAGE_CONVERT_SALES)

# The finding types a producer can actually emit. services/audit_evidence_
# builder.py::extract_findings is the ONLY writer of readiness_findings, and
# these four are everything it can produce. Keeping the list explicit is what
# lets a stage say "nothing can be measured here yet" instead of "all clear".
_PRODUCIBLE_FINDING_TYPES = frozenset({
    # Legacy `aggregate`/`per_product` shape.
    "merchant_visible_via_retailers_only",
    "category_visibility_low",
    "first_party_pdp_indexing_gap",
    "integration_state_incomplete",
    # The shape production actually writes (`brand_rollup.dimensions`). Absent
    # from this set, a stage whose only producer is the rollup would report
    # NO_FINDINGS — "checked, fine" — because _stage_is_measurable would say
    # nothing can produce for it. That is the same false all-clear from the
    # other direction.
    "product_identity_unresolvable",
    "category_citation_weak",
    "destination_unroutable",
    "content_too_thin_to_cite",
    # Meta: the extractor did not recognise the report. Producible, and handled
    # specially below — it must never let ANY stage claim an all-clear.
    "report_shape_unreadable",
})

# The finding that says we could not read the report at all. It is not evidence
# about the merchant; it is evidence about us.
_UNREADABLE_FINDING = "report_shape_unreadable"

_STAGE_FOR_FINDING_TYPE = {
    # Not chosen for the category's queries at all.
    "category_visibility_low": _STAGE_GET_SELECTED,
    # Pivota plumbing incomplete — blocks everything downstream of selection.
    "integration_state_incomplete": _STAGE_GET_SELECTED,
    # Something is cited for this brand, but it is not the merchant.
    "merchant_visible_via_retailers_only": _STAGE_GET_CITED,
    # The merchant's own page cannot be cited because it is not indexable.
    "first_party_pdp_indexing_gap": _STAGE_GET_CITED,

    # Rollup dimensions, mapped by what the dimension MEANS, not by where the
    # default would drop them. Unmapped types fall to GET SELECTED, so leaving
    # these out silently filed every citation problem under selection.
    #   identity — an agent cannot tell which product this is, so it is never
    #   the one chosen.
    "product_identity_unresolvable": _STAGE_GET_SELECTED,
    #   content richness — too thin to be picked out of a category.
    "content_too_thin_to_cite": _STAGE_GET_SELECTED,
    #   citation — mentioned, but rarely as the answer.
    "category_citation_weak": _STAGE_GET_CITED,
    #   routability — cited, but the answer has no buyable offer to send anyone
    #   to. NOT convert_sales: that stage means a real purchase path was
    #   exercised, and only the browser commerce lane can say that. Filing it
    #   there would make a stage that has never run look measured.
    "destination_unroutable": _STAGE_GET_CITED,
}


def _stage_for(finding_type: Optional[str]) -> str:
    """Unmapped findings land in GET SELECTED — the leading stage, and the one
    the measured evidence says the leak is actually in."""
    return _STAGE_FOR_FINDING_TYPE.get(
        str(finding_type or "").strip().lower(), _STAGE_GET_SELECTED,
    )


# A stage with no producer behind it cannot report "no findings" — that reads
# as an all-clear for something never measured. convert_sales is the obvious
# case (the browser commerce lane has never produced a production
# observation); get_cited is the one an earlier cut got wrong, mapping it to
# four finding types no producer emits and letting it report NO_FINDINGS
# forever.
_STAGE_UNVERIFIED_REASON = {
    _STAGE_CONVERT_SALES: (
        "No production observation exists for this stage yet: the browser "
        "commerce lane has never run against a live store."
    ),
}


def _stage_is_measurable(stage: str) -> bool:
    """True iff some producible finding type routes to this stage."""
    if stage in _STAGE_UNVERIFIED_REASON:
        return False
    return any(
        _stage_for(ft) == stage for ft in _PRODUCIBLE_FINDING_TYPES
    )


_HEADLINE_FINDING = "brand_headline_distribution"


def _headline_distribution(
    findings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """The headline, as a distribution the report actually measured.

    Reads the `brand_headline_distribution` finding the evidence builder copies
    verbatim out of the rollup, so the headline and the stage detail cannot
    disagree — they were read from the same rows in the same pass.

    WHAT IT DELIBERATELY DOES NOT DO. It computes no average across dimensions
    and emits no single number: collapsing bands into one figure is the stage
    score Rule 1 removes, and the run that prompted this showed why — identity
    16 and citation 48 average to something that describes neither.

    A run with no such finding gets `unavailable_reason`, NOT an empty
    distribution that reads like a clean sheet. That confusion is the whole
    reason this file was touched.
    """
    payload: Dict[str, Any] = {}
    for finding in findings or []:
        if str((finding or {}).get("finding_type") or "").strip().lower() == (
            _HEADLINE_FINDING
        ):
            payload = (finding.get("payload") or {})
            break

    dims = payload.get("dimensions")
    if not isinstance(dims, list) or not dims:
        return {
            "kind": "distribution",
            "dimensions": [],
            "prompt_split": None,
            "unavailable_reason": (
                "This run produced no per-dimension distribution. That is a "
                "missing measurement, not a score of zero and not a pass."
            ),
        }

    split = payload.get("prompt_split")
    out: Dict[str, Any] = {
        "kind": "distribution",
        "dimensions": dims,
        "dimensions_considered": payload.get("dimensions_considered"),
        "skus_audited": payload.get("skus_audited"),
        "prompt_split": split,
        "note": (
            "Per-dimension distribution with n. Not a composite score: "
            "averaging these describes no dimension."
        ),
    }
    if split is None:
        # Absent must read as absent. Without this the caller sees a null and
        # is free to assume branded and unbranded performed alike.
        out["prompt_split_unavailable_reason"] = (
            "This report did not carry the intent-axis citation counts the "
            "branded/unbranded split is computed from. Not parity — unknown."
        )
    return out


def build_revenue_recovery_projection(
    *,
    evidence: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    audit_run_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The merchant's recovery funnel: three stages, GET SELECTED leading.

    Same data the merchant projection may show, arranged as the funnel. A
    stage reports NO_FINDINGS only when something could have put a finding
    there and did not; a stage with no producer behind it reports UNVERIFIED
    with the reason, because a false all-clear is worse than an honest gap.
    """
    row = audit_run_row or {}
    stages: Dict[str, Dict[str, Any]] = {
        name: {"stage": name, "findings": [], "actions": []}
        for name in _STAGES
    }

    # parent_finding_id is the ONLY link between an action and a finding —
    # action_plan_items has no finding_type column, so routing actions by one
    # silently put every action in the default stage.
    stage_by_finding_id: Dict[str, str] = {}
    for f in findings:
        stage = _stage_for(f.get("finding_type"))
        fid = f.get("finding_id")
        if fid is not None:
            stage_by_finding_id[str(fid)] = stage
        if f.get("severity") not in ("critical", "high", "medium"):
            continue
        stages[stage]["findings"].append({
            "type": f.get("finding_type"),
            "severity": f.get("severity"),
            "summary": f.get("short_summary"),
        })

    for a in sorted(actions, key=_severity_phase_sort_key):
        parent = a.get("parent_finding_id")
        stage = stage_by_finding_id.get(
            str(parent) if parent is not None else "", _STAGE_GET_SELECTED,
        )
        stages[stage]["actions"].append(_action_for_merchant(a))

    unreadable = any(
        str((f or {}).get("finding_type") or "").strip().lower()
        == _UNREADABLE_FINDING
        for f in (findings or [])
    )
    for name in _STAGES:
        if not _stage_is_measurable(name):
            stages[name]["status"] = "UNVERIFIED"
            stages[name]["unverified_reason"] = _STAGE_UNVERIFIED_REASON.get(
                name,
                "Nothing that runs today can produce a finding for this "
                "stage, so its absence is not an all-clear.",
            )
        elif unreadable:
            # We could not read the report. NOTHING about this merchant was
            # established, so no stage may claim an all-clear — including the
            # stages this particular finding did not land in.
            stages[name]["status"] = "UNVERIFIED"
            stages[name]["unverified_reason"] = (
                "The findings extractor did not recognise this audit's report "
                "shape, so nothing was read for this stage. Absence here is "
                "not an all-clear."
            )
        else:
            stages[name]["status"] = (
                "MEASURED" if stages[name]["findings"] else "NO_FINDINGS"
            )

    return {
        "audience": AUDIENCE_REVENUE_RECOVERY,
        "builder_version": _BUILDER_VERSION,
        "audit_run_id": row.get("run_id"),
        # NOT a headline score. `visibility_score_avg` sat here as a bare
        # integer with no n and no interval, which Rule 1 forbids outright — "no
        # stage scores. Distributions only. A 'CAPTURE INTENT 41%' implies a
        # formula. Ours moves 5.6x on denominator choice." The §6 definition of
        # done asks this surface for a split "with `n` beside it", so a
        # distribution carrying its own counts is what belongs here.
        "headline": _headline_distribution(findings),
        "stages": [stages[name] for name in _STAGES],
    }


def build_public_anonymous_projection(
    *,
    evidence: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    audit_run_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The ONLY projection an unauthenticated visitor may read.

    Deterministic tier only. Everything here is computable without a model
    call, so nothing in it can be a fabricated claim about a store whose owner
    has not asked us for anything.

    PRESENCE IS THE CLAIM. A signal appears iff evidence of that type exists
    for this run, carrying the strength we recorded. There is deliberately no
    `detected: false` — no writer records one, so emitting it would state that
    a store lacks an agent checkout when the truth is that we never observed
    one. That is the same fabrication `convert_sales` is marked UNVERIFIED to
    avoid, and an earlier cut of this builder made it for every real store by
    reading `detected` and `evidence_level` out of payload_jsonb, where
    neither has ever lived (evidence_level is a COLUMN).

    What it deliberately does NOT carry:
      * merchant_id / any owner identity — the run may be unclaimed, and a
        claimed one must not become publicly attributable by being read here.
      * visibility / attribution scores — model-derived, and a headline number
        on an unregistered store is the fabrication risk in its purest form.
      * a severity histogram. Counts by severity look like metadata and are
        not: extract_findings assigns each severity by a fixed threshold on
        the very scores above (avg_attribution < 30, avg_category < 20/40,
        phase_0_complete is False), so the histogram is an invertible encoding
        of them — and a `critical` would disclose that a merchant's Pivota
        onboarding is incomplete. Totals only.
      * finding summaries and action content — that is the paid layer.
      * evidence payloads — those carry endpoint URLs and probe ids. Being
        allowed to report a signal is not being allowed to republish it.
    """
    row = audit_run_row or {}
    signals: List[Dict[str, Any]] = []
    for e in evidence:
        etype = str(e.get("evidence_type") or "")
        if etype not in _DETERMINISTIC_EVIDENCE_TYPES:
            continue
        # The COLUMN, coerced to its documented enum. Anything else becomes
        # None rather than passing an unreviewed value to an anonymous reader.
        level = e.get("evidence_level")
        # isinstance FIRST: `x in frozenset` raises TypeError on an unhashable
        # value, and a dict in this column would take the whole projection
        # down (the builder loop swallows it as projections_failed, so all
        # seven silently vanish for that run).
        if not isinstance(level, str) or level not in _EVIDENCE_LEVELS:
            level = None
        signals.append({"signal": etype, "evidence_level": level})

    return {
        "audience": AUDIENCE_PUBLIC_ANONYMOUS,
        "builder_version": _BUILDER_VERSION,
        "audit_run_id": row.get("run_id"),
        # Says out loud what tier this is, so the absence of GEO numbers cannot
        # be misread as a store that scored zero.
        "deterministic_only": True,
        "observed_signals": signals,
        # How much is waiting, never what it says, and never how severe.
        "locked": {
            "findings": len(findings),
            "actions": len(actions),
        },
        # An unclaimed run is claimable; a claimed one is not, and saying so
        # is what lets the page render "this is yours" vs "sign in".
        "claimable": bool(row) and not row.get("merchant_id"),
    }


_BUILDERS = {
    AUDIENCE_EMPLOYEE_BD: build_employee_bd_projection,
    AUDIENCE_MERCHANT: build_merchant_projection,
    AUDIENCE_INTERNAL_OPS: build_internal_ops_projection,
    AUDIENCE_PIVOTA_PDP_FEED: build_pivota_pdp_feed_projection,
    AUDIENCE_FRONTEND_AGENT_FEED: build_frontend_agent_feed_projection,
    AUDIENCE_REVENUE_RECOVERY: build_revenue_recovery_projection,
    AUDIENCE_PUBLIC_ANONYMOUS: build_public_anonymous_projection,
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
    """Load canonical data + build + upsert every PERSISTED projection.

    Six of the seven audiences: public_anonymous is built on demand instead —
    see the loop below.
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

    # PR-codex-review-followup: the per-audience builders include a
    # merchant_id field inside the payload, but the report_projections
    # table also has its own merchant_id COLUMN (added by migration
    # 088 for the schema-tenancy invariant). The column was staying
    # NULL because this loop didn't pass merchant_id through to
    # upsert_projection. Pull it from the audit row once and thread
    # it through.
    merchant_id_for_row = (
        audit_row.get("merchant_id") if audit_row else None
    )

    # public_anonymous is deliberately NOT persisted here. This runs for every
    # audit run, including a paying merchant's, and would leave the table
    # pre-populated with a public projection row per run — carrying that
    # merchant_id in its own column, cheaply scannable by
    # idx_report_projections_merchant_audience. The public lane builds it on
    # demand for the unclaimed funnel runs that are its only subject, so
    # storing one for every merchant run creates exposure and buys nothing.
    for audience in sorted(VALID_AUDIENCES - {AUDIENCE_PUBLIC_ANONYMOUS}):
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
                merchant_id=merchant_id_for_row,
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
