"""Central severity scorer for audit-emitted actions (Q-P1-6).

Pre-fix, severities were hardcoded at action-emission sites:
  - services/agent_center_bd_report_service.py:_generate_action_items:
    "Capture the AI-channel funnel..." was always `critical` for
    VERDICT_VIA_RETAILERS, regardless of whether the underlying
    evidence supported a critical-tier label.
  - services/audit_playbook_engine.py emitted whatever severity
    `data/playbooks.json` declared, with no adjustment for the
    actual audit's score gap, evidence quality, or competitor
    evidence.

The Winona prod artifact (run 932d8261) showed two specific failures
of this scheme:

  * A "Pitch whowhatwear.com fashion team" action carried
    `severity: "high"` despite `competitors_named: []` and
    `example_failed_query: null` — the playbook's authored "high"
    flowed through even though the audit had zero supporting
    evidence for the pitch.
  * "Specific queries where your URL was missing" came out at
    `severity: "medium"` even though buyer-intent attribution was
    0/100 — the canonical case where a critical severity is
    actually warranted.

This module exposes one function, `compute_action_severity`, that
takes the actual audit evidence and a base severity (from a
playbook or a hardcoded site) and returns a calibrated severity +
a short `severity_reason` for auditability. Callers MUST surface
`severity_reason` alongside the severity so the merchant portal
can render "why critical" tooltips and so future regression
investigations can grep for the rule that fired.

The scorer's design rules favor:
  - **High** for exact failed buyer-intent remediation with
    measurable score gaps and concrete failed-query examples.
  - **Critical** for compound evidence: large score gap + named
    competitors + retailer/marketplace host capture.
  - **Down-tier** for weak evidence: pitch actions without
    competitors_named and without a failed_query_example.
  - **Pass through** the base severity when the audit-evidence
    inputs are too sparse to confidently re-tier (the conservative
    default — don't override an author's call without evidence).
"""

from __future__ import annotations

from typing import Optional, Tuple


# Ordered weakest → strongest. Used for clamping / comparisons.
_SEVERITY_ORDER = ("low", "medium", "high", "critical")
_SEVERITY_RANK = {s: i for i, s in enumerate(_SEVERITY_ORDER)}

# Score-gap thresholds. score_gap_pct = (category_score -
# attribution_score) when both are present, OR (100 -
# attribution_score) for INVISIBLE-shape audits where the gap is
# anchored to the goal state. Calibrated against the Winona
# artifact (cat=33, attr=0 → gap=33) and stronger gaps.
_GAP_BIG = 67       # ≥ 67 → critical-tier on its own
_GAP_MEDIUM = 33    # 33-66 → high-tier when evidence supports

# Evidence-tier strength. Matches the tiers defined in
# `services/agent_center_bd_report_service:_category_recognition_phrase`
# (PR-1 / commit b9c3a8d). prompt_echo_only is the weakest: the LLM
# self-reported visibility because the prompt named the product, not
# because any source backed it up.
_STRONG_EVIDENCE_TIERS = frozenset({
    "exact_product_grounded",
    "brand_grounded",
})
_WEAK_EVIDENCE_TIERS = frozenset({
    "brand_excerpt_only",
    "prompt_echo_only",
})

# Retailer/marketplace host capture is the strongest pitch evidence
# (named competitor with verified host type). Publisher capture is
# strong but lower-tier. CDN/brand/unclassified shouldn't drive
# severity at all — they're not editorial sources.
_RETAIL_HOST_TYPES = frozenset({"retailer", "marketplace"})
_EDITORIAL_HOST_TYPES = frozenset({"publisher", "editorial"})


def _coerce_severity(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().lower()
    return v if v in _SEVERITY_RANK else None


def _max_severity(a: str, b: str) -> str:
    return a if _SEVERITY_RANK[a] >= _SEVERITY_RANK[b] else b


def _min_severity(a: str, b: str) -> str:
    return a if _SEVERITY_RANK[a] <= _SEVERITY_RANK[b] else b


def compute_action_severity(
    *,
    score_gap_pct: Optional[int] = None,
    evidence_tier: Optional[str] = None,
    host_type: Optional[str] = None,
    has_failed_query_example: bool = False,
    has_competitors_named: bool = False,
    base_severity: Optional[str] = None,
) -> Tuple[str, str]:
    """Return `(severity, severity_reason)` for an audit-emitted
    action.

    Inputs:
      - score_gap_pct: 0-100. Higher means a bigger discovery /
        attribution shortfall. Driver of upgrade decisions.
      - evidence_tier: one of `exact_product_grounded`,
        `brand_grounded`, `brand_excerpt_only`, `prompt_echo_only`,
        or None when not applicable.
      - host_type: classifier output (retailer / marketplace /
        publisher / editorial / cdn / brand / unclassified / None).
      - has_failed_query_example: True if the action carries at
        least one specific failed buyer-intent query as evidence.
      - has_competitors_named: True if the action lists at least
        one named competitor brand.
      - base_severity: the author's / playbook's call. Used as the
        conservative fallback when the audit evidence is too sparse
        to confidently re-tier.

    Returns:
      `(severity, reason)`. Severity ∈ {critical, high, medium, low}.
      Reason is a short stable string (e.g. "score_gap_big+retailer_capture")
      suitable for telemetry/grep.

    The function never PROMOTES above the base severity for pitch
    actions that lack supporting evidence (competitors_named +
    failed_query_example). The function MAY DEMOTE a high-severity
    pitch to medium when its evidence is empty. This matches
    codex's recommendation: "Downgrade weak source-target pitches;
    upgrade exact failed buyer-intent remediations."
    """
    base = _coerce_severity(base_severity)
    gap = score_gap_pct if isinstance(score_gap_pct, int) else None
    tier = (evidence_tier or "").strip().lower() or None
    host = (host_type or "").strip().lower() or None

    # Rule 1 — critical upgrade: large score gap + retailer/marketplace
    # capture with named competitors. The exact "competitors are
    # eating your funnel" case.
    if gap is not None and gap >= _GAP_BIG and host in _RETAIL_HOST_TYPES \
            and has_competitors_named:
        return ("critical", "score_gap_big+retailer_capture+competitors_named")

    # Rule 2 — critical for exact buyer-intent zero-attribution
    # remediation. When attribution is effectively absent and we
    # have a concrete failed-query example, the remediation is
    # critical regardless of the playbook's base.
    if gap is not None and gap >= _GAP_BIG and has_failed_query_example:
        return ("critical", "score_gap_big+failed_query_example")

    # Q-P1-6 PR-6 — guard: Rules 3, 4, 7 are UPGRADE rules to "high".
    # They must not fire when `base` is already higher than "high"
    # (i.e. base="critical"), or they would silently downgrade a
    # critical-tier authored action. This surfaced when PR-6 migrated
    # the verdict-tier hardcoded severities through the scorer — a
    # base=critical action with gap=60 + supporting evidence was
    # being knocked down to high by Rule 3. The scorer's intent for
    # Rules 3/4/7 is "lift conservative bases TO high"; respect a
    # higher authored base when no explicit downgrade rule applies.
    _base_already_above_high = (
        base is not None and _SEVERITY_RANK[base] > _SEVERITY_RANK["high"]
    )

    # Rule 3 — high for medium score gap with concrete supporting
    # evidence. The audit identified a real problem and a real
    # corrective action.
    if not _base_already_above_high and gap is not None and gap >= _GAP_MEDIUM and (
        has_failed_query_example or host in _RETAIL_HOST_TYPES
    ):
        return ("high", "score_gap_medium+supporting_evidence")

    # Rule 4 — high for medium gap on strong evidence tier even
    # without a failed-query example, when the host is editorial
    # (publisher / editorial site). The site captured grounded
    # citations; the pitch has a real basis.
    if not _base_already_above_high and gap is not None and gap >= _GAP_MEDIUM \
            and host in _EDITORIAL_HOST_TYPES and tier in _STRONG_EVIDENCE_TIERS:
        return ("high", "score_gap_medium+editorial_host+strong_tier")

    # Rule 5 — DOWNGRADE: host-targeted pitch with NO failed-query
    # example AND NO named competitors AND the audit DID measure a
    # score_gap. The Winona "Pitch whowhatwear.com fashion team"
    # case (high → medium). Requires `gap is not None` so we don't
    # demote when the audit simply hasn't collected query evidence
    # (e.g. category-only audits without attribution data — those
    # pass through to the playbook author's call).
    is_pitch_action = host is not None
    rule_4_already_applied = (
        not _base_already_above_high
        and gap is not None and gap >= _GAP_MEDIUM
        and host in _EDITORIAL_HOST_TYPES
        and tier in _STRONG_EVIDENCE_TIERS
    )
    if (
        base == "high"
        and is_pitch_action
        and gap is not None
        and not rule_4_already_applied
        and not has_failed_query_example
        and not has_competitors_named
    ):
        return ("medium", "weak_pitch_evidence_downgrade")

    # Rule 6 — DOWNGRADE: weak evidence tier (excerpt-only or
    # prompt-echo) never drives a critical. Cap at high.
    if base == "critical" and tier in _WEAK_EVIDENCE_TIERS:
        return ("high", "weak_evidence_tier_critical_cap")

    # Rule 7 — UPGRADE: when base is conservative (medium/low) but
    # the score gap is huge AND we have a concrete query, upgrade
    # to high. The "Specific queries where your URL was missing"
    # case — pre-fix it shipped at medium when it should have been
    # high or critical.
    if base in {"medium", "low"} and gap is not None and gap >= _GAP_BIG \
            and has_failed_query_example:
        return ("high", "score_gap_big+failed_query_example+base_low")

    # Pass-through: base severity is the fall-through default. When
    # the audit gives us nothing to re-tier on, respect the author.
    if base is not None:
        return (base, "base_severity_passthrough")

    # No base + no evidence → conservative medium.
    return ("medium", "no_evidence_default")
