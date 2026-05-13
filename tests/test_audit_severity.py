"""Q-P1-6: central severity scorer regression tests.

Pre-fix, severities were hardcoded at action-emission sites. The
Winona prod artifact (run 932d8261) exposed two specific failure
modes:

  1. `Pitch whowhatwear.com fashion team` carried severity=high
     despite `competitors_named: []` and `example_failed_query: null`
     — the playbook's authored "high" flowed through with zero
     supporting evidence.
  2. `Specific queries where your URL was missing` carried
     severity=medium even though buyer-intent attribution was 0/100
     — the canonical case where critical was actually warranted.

The scorer must:
  - DOWNGRADE weak pitch evidence (no competitors_named, no
    failed_query_example) so the merchant doesn't see "high" tags
    for actions the audit couldn't justify.
  - UPGRADE conservative-base actions when the underlying score
    gap + concrete failed-query example warrant it.
  - NEVER let weak evidence tiers (excerpt-only, prompt_echo_only)
    drive a critical.

Each test asserts BOTH the resulting severity AND the
`severity_reason` string so the rule-firing is observable in
telemetry and so future regressions get traced to a specific rule.
"""

from __future__ import annotations

from services.audit_severity import compute_action_severity


# =========================================================================
# Upgrade rules (audit evidence promotes the base)
# =========================================================================


def test_critical_when_big_gap_and_retailer_capture_and_competitors_named():
    """Rule 1 — the strongest signal: big score gap, retailer hosts
    captured grounded citations, AND we have named competitors.
    The pitch is critical regardless of what the playbook's base
    was set to."""
    severity, reason = compute_action_severity(
        score_gap_pct=70,
        host_type="retailer",
        has_competitors_named=True,
        has_failed_query_example=True,
        base_severity="medium",
    )
    assert severity == "critical"
    assert reason == "score_gap_big+retailer_capture+competitors_named"


def test_critical_when_big_gap_and_failed_query_example():
    """Rule 2 — exact failed-query remediation at big gap is
    critical (the "Specific queries where your URL was missing"
    case, pre-fix shipped as medium)."""
    severity, reason = compute_action_severity(
        score_gap_pct=100,
        has_failed_query_example=True,
        base_severity="medium",
    )
    assert severity == "critical"
    assert reason == "score_gap_big+failed_query_example"


def test_high_when_medium_gap_and_failed_query_example():
    """Rule 3 — medium gap with a concrete failed-query example
    still earns high."""
    severity, reason = compute_action_severity(
        score_gap_pct=50,
        has_failed_query_example=True,
        base_severity="medium",
    )
    assert severity == "high"
    assert reason == "score_gap_medium+supporting_evidence"


def test_high_when_medium_gap_and_retailer_host():
    """Rule 3 — retailer-host capture at medium gap, no explicit
    failed-query example, still earns high."""
    severity, reason = compute_action_severity(
        score_gap_pct=40,
        host_type="marketplace",
        base_severity="medium",
    )
    assert severity == "high"
    assert reason == "score_gap_medium+supporting_evidence"


def test_high_when_medium_gap_editorial_host_strong_tier():
    """Rule 4 — editorial-host pitch with strong evidence tier is
    high even without a failed-query example."""
    severity, reason = compute_action_severity(
        score_gap_pct=45,
        host_type="publisher",
        evidence_tier="brand_grounded",
        base_severity="medium",
    )
    assert severity == "high"
    assert reason == "score_gap_medium+editorial_host+strong_tier"


def test_upgrade_low_base_to_critical_when_big_gap_and_failed_query():
    """Big gap + concrete failed-query overrides a conservative
    base — Rule 2 fires (critical) regardless of base. The author's
    low call is overruled by the evidence."""
    severity, reason = compute_action_severity(
        score_gap_pct=85,
        has_failed_query_example=True,
        base_severity="low",
    )
    assert severity == "critical"
    assert reason == "score_gap_big+failed_query_example"


def test_upgrade_low_base_to_high_when_medium_gap_failed_query():
    """Rule 3 covers the medium-gap upgrade for conservative-base
    actions with a concrete failed-query example."""
    severity, reason = compute_action_severity(
        score_gap_pct=50,
        has_failed_query_example=True,
        base_severity="low",
    )
    assert severity == "high"
    assert reason == "score_gap_medium+supporting_evidence"


# =========================================================================
# Downgrade rules (audit evidence demotes the base)
# =========================================================================


def test_downgrade_high_to_medium_when_pitch_has_no_evidence():
    """Rule 5 — pre-fix `Pitch whowhatwear.com fashion team` shipped
    as high with `competitors_named: []` and
    `example_failed_query: null`. The downgrade fires when base=high
    AND host_type IS set (signaling a pitch) AND no supporting
    evidence (no competitors_named, no failed_query_example).
    Structural actions without a target_host pass through —
    see `test_passthrough_when_no_evidence_to_retier`."""
    severity, reason = compute_action_severity(
        score_gap_pct=20,
        host_type="publisher",  # pitch action targets a host
        has_failed_query_example=False,
        has_competitors_named=False,
        base_severity="high",
    )
    assert severity == "medium"
    assert reason == "weak_pitch_evidence_downgrade"


def test_downgrade_high_to_medium_when_unclassified_host_no_evidence():
    """Same rule — unclassified host (the ctfassets.net-shape case)
    is not editorial evidence, so a high-tier pitch without
    supporting evidence collapses to medium. Requires `score_gap_pct`
    so we know the audit DID measure attribution (otherwise we
    respect the playbook author's call)."""
    severity, reason = compute_action_severity(
        score_gap_pct=20,
        host_type="unclassified",
        has_failed_query_example=False,
        has_competitors_named=False,
        base_severity="high",
    )
    assert severity == "medium"
    assert reason == "weak_pitch_evidence_downgrade"


def test_cap_critical_to_high_when_evidence_tier_is_weak():
    """Rule 6 — when the evidence tier is excerpt-only or
    prompt_echo_only (the Winona category-match shape: no grounded
    source named the brand), even a base=critical action caps at
    high. Weak evidence cannot drive a critical-tier label."""
    severity, reason = compute_action_severity(
        evidence_tier="brand_excerpt_only",
        base_severity="critical",
    )
    assert severity == "high"
    assert reason == "weak_evidence_tier_critical_cap"


def test_cap_critical_when_prompt_echo_only():
    severity, _ = compute_action_severity(
        evidence_tier="prompt_echo_only",
        base_severity="critical",
    )
    assert severity == "high"


# =========================================================================
# Pass-through + defaults
# =========================================================================


def test_passthrough_when_no_evidence_to_retier():
    """When audit gives us nothing to re-tier on (no gap, no
    evidence_tier, no host_type, no flags), respect the author's
    base severity."""
    severity, reason = compute_action_severity(
        base_severity="high",
    )
    assert severity == "high"
    assert reason == "base_severity_passthrough"


def test_default_medium_when_no_base_and_no_evidence():
    severity, reason = compute_action_severity()
    assert severity == "medium"
    assert reason == "no_evidence_default"


def test_invalid_base_severity_treated_as_none():
    """Garbage `base_severity` strings shouldn't blow up — they're
    coerced to None and the default fires."""
    severity, _ = compute_action_severity(
        base_severity="urgent",  # not a valid tier
    )
    assert severity == "medium"


def test_passthrough_preserves_low_base_when_no_upgrade_signals():
    """Low-tier authored actions stay low when audit evidence
    doesn't justify an upgrade."""
    severity, reason = compute_action_severity(
        score_gap_pct=15,
        base_severity="low",
    )
    assert severity == "low"
    assert reason == "base_severity_passthrough"


# =========================================================================
# Realistic combined-rule scenarios
# =========================================================================


def test_winona_whowhatwear_pitch_downgrade():
    """End-to-end Winona regression: the merchant saw
    `Pitch whowhatwear.com fashion team` at severity=high with
    empty competitors_named and null failed_query. After the fix
    this is medium with the downgrade reason."""
    severity, reason = compute_action_severity(
        score_gap_pct=33,
        host_type="publisher",  # whowhatwear is editorial
        # but no actual evidence:
        has_competitors_named=False,
        has_failed_query_example=False,
        evidence_tier=None,
        base_severity="high",
    )
    # Editorial host with strong tier would Rule 4; but no
    # strong_tier here so it falls through to Rule 5 downgrade.
    assert severity == "medium"
    assert reason == "weak_pitch_evidence_downgrade"


def test_winona_specific_queries_remediation_upgrade():
    """End-to-end Winona regression: "Specific queries where your
    URL was missing" pre-fix shipped at medium with attribution=0
    (gap=100). After the fix this is critical with the
    failed-query-example reason."""
    severity, reason = compute_action_severity(
        score_gap_pct=100,
        has_failed_query_example=True,
        base_severity="medium",
    )
    assert severity == "critical"
    assert reason == "score_gap_big+failed_query_example"
