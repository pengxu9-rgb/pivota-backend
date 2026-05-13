"""Q-P1-6 PR-6: _generate_action_items severity routing regression.

PR-3 wired the central severity scorer (`services.audit_severity`)
into the playbook engine. _generate_action_items in
`agent_center_bd_report_service.py` was left out of that migration
with a TODO marker; the post-merge prod audit (2026-05-13) confirmed
the gap by showing the canonical Winona regression case — "Specific
queries where your URL was missing" — still shipping at severity=medium
when audit evidence (attribution=0, category=67, gap=67, has failed
buyer-intent queries) calls for critical via the scorer's Rule 2.

PR-6 migrates all 10 hardcoded severity sites in the function. These
tests assert per-site:

  1. Every emitted action carries BOTH `severity` and `severity_reason`
     (no MISSING reason — the pre-PR-6 bug).
  2. The scorer's rules fire on the right shapes (downgrade,
     passthrough, upgrade).
  3. The canonical Winona regression case is fixed.

The function takes ~12 kwargs and emits 3-5 items depending on inputs.
These tests use compact fixtures that flip only the inputs each test
case cares about, keeping diffs readable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from services.agent_center_bd_report_service import (
    _generate_action_items,
    VERDICT_INVISIBLE,
    VERDICT_MISATTRIBUTED,
    VERDICT_CATEGORY_MENTION_NO_FIRST_PARTY,
    VERDICT_VIA_RETAILERS,
    VERDICT_STRONG,
    VERDICT_PARTIAL,
)


def _attribution_run(
    *,
    query: str = "buy product",
    found: bool = False,
    cited_host: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a minimal attribution_run dict that the function's
    `_failed_attribution_queries` helper accepts."""
    return {
        "query": query,
        "parsed": {"merchant_url_found": found},
        "grounding_sources": (
            [{"uri": f"https://{cited_host}/x", "title": "src"}]
            if cited_host else []
        ),
    }


def _by_title(items: List[Dict[str, Any]], substring: str) -> Optional[Dict[str, Any]]:
    for it in items:
        if substring.lower() in (it.get("title") or "").lower():
            return it
    return None


# =========================================================================
# Universal contract: every emitted action MUST carry severity_reason
# =========================================================================


def test_every_action_carries_severity_reason_minimal_inputs():
    """No matter what shape we feed in, every emitted action must
    have BOTH severity and severity_reason. Pre-PR-6 the function
    emitted actions WITHOUT severity_reason."""
    items = _generate_action_items(
        verdict_label=VERDICT_INVISIBLE,
        visibility_runs=[],
        attribution_runs=[],
        competitor_hosts=[],
        merchant_cited_runs=0,
        runs_with_any_citation=0,
    )
    assert items, "INVISIBLE verdict must emit at least one action"
    for item in items:
        assert "severity" in item, f"action missing severity: {item.get('title')}"
        assert "severity_reason" in item, (
            f"action missing severity_reason — Q-P1-6 contract violation: {item.get('title')}"
        )
        assert item["severity_reason"], "severity_reason must be a non-empty string"


def test_every_action_carries_severity_reason_partial_verdict():
    items = _generate_action_items(
        verdict_label=VERDICT_PARTIAL,
        visibility_runs=[],
        attribution_runs=[
            _attribution_run(query="q1", found=True),
            _attribution_run(query="q2", found=False, cited_host="ulta.com"),
        ],
        competitor_hosts=[{"host": "ulta.com", "times_cited": 3}],
        merchant_cited_runs=1,
        runs_with_any_citation=2,
        visibility_score=50,
        attribution_score=50,
        category_visibility_score=70,
    )
    for item in items:
        assert "severity_reason" in item, item.get("title")


# =========================================================================
# THE canonical Winona regression — site 9 (Specific queries)
# =========================================================================


def test_specific_queries_action_upgrades_to_critical_on_winona_shape():
    """The headline PR-6 fix: with gap=67 (category=67, attribution=0)
    + has_failed_query_example=True, the scorer's Rule 2 fires and
    returns critical with reason=score_gap_big+failed_query_example.

    Pre-PR-6 this action shipped at hardcoded severity=medium with
    no severity_reason. The merchant saw the canonical critical case
    tagged medium and deprioritized it."""
    items = _generate_action_items(
        verdict_label=VERDICT_CATEGORY_MENTION_NO_FIRST_PARTY,
        visibility_runs=[],
        attribution_runs=[
            _attribution_run(query=q, found=False)
            for q in [
                "where can I buy Winona Soothing Repair Serum",
                "shop Winona Soothing Repair Serum online",
                "Winona Soothing Repair Serum for sale",
            ]
        ],
        competitor_hosts=[],
        merchant_cited_runs=0,
        runs_with_any_citation=3,
        attribution_score=0,
        category_visibility_score=67,
    )
    specific = _by_title(items, "Specific queries where your URL was missing")
    assert specific is not None, (
        "Specific-queries action must be emitted when failed_attribution_queries is non-empty"
    )
    # The fix: upgraded from medium to critical via Rule 2.
    assert specific["severity"] == "critical", (
        f"Winona regression — expected critical, got {specific['severity']!r} "
        f"(reason={specific.get('severity_reason')!r})"
    )
    assert specific["severity_reason"] == "score_gap_big+failed_query_example"


def test_specific_queries_action_upgrades_to_high_on_medium_gap():
    """When gap is moderate (33-66) + has_failed_query_example,
    Rule 3 fires → high with score_gap_medium+supporting_evidence."""
    items = _generate_action_items(
        verdict_label=VERDICT_PARTIAL,
        visibility_runs=[],
        attribution_runs=[
            _attribution_run(query="q1", found=False),
            _attribution_run(query="q2", found=True),
        ],
        competitor_hosts=[],
        merchant_cited_runs=1,
        runs_with_any_citation=2,
        attribution_score=33,
        category_visibility_score=70,  # gap = 37 → medium
    )
    specific = _by_title(items, "Specific queries where your URL was missing")
    assert specific is not None
    assert specific["severity"] == "high"
    assert specific["severity_reason"] == "score_gap_medium+supporting_evidence"


def test_specific_queries_action_passthrough_when_no_gap_signal():
    """When category_visibility_score is unset (0 = not measured),
    the scorer has no gap signal and passes through base=medium."""
    items = _generate_action_items(
        verdict_label=VERDICT_PARTIAL,
        visibility_runs=[],
        attribution_runs=[
            _attribution_run(query="q1", found=False),
            _attribution_run(query="q2", found=True),
        ],
        competitor_hosts=[],
        merchant_cited_runs=1,
        runs_with_any_citation=2,
        attribution_score=33,
        category_visibility_score=0,  # not measured
    )
    specific = _by_title(items, "Specific queries where your URL was missing")
    assert specific is not None
    assert specific["severity"] == "medium"
    assert specific["severity_reason"] == "base_severity_passthrough"


# =========================================================================
# Verdict-tier sites stay critical when they should
# =========================================================================


def test_invisible_verdict_stays_critical():
    """INVISIBLE → base=critical. Brand-level (no host_type) so
    rules 1/4/5 don't fire. Critical caps unless evidence_tier is
    weak (not passed here)."""
    items = _generate_action_items(
        verdict_label=VERDICT_INVISIBLE,
        visibility_runs=[],
        attribution_runs=[_attribution_run(found=False)],
        competitor_hosts=[],
        merchant_cited_runs=0,
        runs_with_any_citation=0,
        attribution_score=0,
        category_visibility_score=0,
    )
    headline = items[0]  # action 1 is always the verdict-tier headline
    assert headline["severity"] == "critical"


def test_misattributed_verdict_stays_critical_with_evidence():
    """MISATTRIBUTED + named retailers + failed buyer queries →
    critical (passthrough or upgrade)."""
    items = _generate_action_items(
        verdict_label=VERDICT_MISATTRIBUTED,
        visibility_runs=[],
        attribution_runs=[
            _attribution_run(query="q1", found=False, cited_host="ulta.com"),
        ],
        competitor_hosts=[{"host": "ulta.com", "times_cited": 3}],
        merchant_cited_runs=0,
        runs_with_any_citation=1,
        attribution_score=20,
        category_visibility_score=80,
        category_retailer_hosts=[
            {"host": "ulta.com", "times_cited": 3, "type": "retailer"},
        ],
    )
    headline = items[0]
    assert headline["severity"] == "critical"


def test_category_mention_no_first_party_stays_critical():
    """The Winona verdict label. Action 1 (Convert category mentions)
    stays critical even with no failed_query — base passthrough."""
    items = _generate_action_items(
        verdict_label=VERDICT_CATEGORY_MENTION_NO_FIRST_PARTY,
        visibility_runs=[],
        attribution_runs=[_attribution_run(found=False)],
        competitor_hosts=[],
        merchant_cited_runs=0,
        runs_with_any_citation=1,
        attribution_score=0,
        category_visibility_score=67,
    )
    convert = _by_title(items, "Convert category mentions")
    assert convert is not None
    assert convert["severity"] == "critical"


def test_strong_verdict_stays_low():
    """STRONG → maintenance action, low severity. Base passthrough."""
    items = _generate_action_items(
        verdict_label=VERDICT_STRONG,
        visibility_runs=[],
        attribution_runs=[],
        competitor_hosts=[],
        merchant_cited_runs=3,
        runs_with_any_citation=3,
        visibility_score=90,
        attribution_score=85,
        category_visibility_score=85,
    )
    maintain = _by_title(items, "Maintain attribution")
    assert maintain is not None
    assert maintain["severity"] == "low"
    assert maintain["severity_reason"] == "base_severity_passthrough"


# =========================================================================
# Visibility-gap site uses passthrough (failed VIS queries ≠ buyer-intent)
# =========================================================================


def test_visibility_gap_action_stays_medium_passthrough():
    """The visibility-gap action (site 10) emits when failed
    VISIBILITY queries exist but not attribution queries. The
    scorer's has_failed_query_example flag is for buyer-intent
    failures specifically; passing visibility failures would mis-fire
    Rule 7. Confirm this site stays at medium passthrough."""
    items = _generate_action_items(
        verdict_label=VERDICT_PARTIAL,
        visibility_runs=[
            {"query": "v1", "parsed": {"merchant_url_found": False}},
        ],
        attribution_runs=[
            _attribution_run(query="a1", found=True),
        ],
        competitor_hosts=[],
        merchant_cited_runs=1,
        runs_with_any_citation=1,
        visibility_score=20,
        attribution_score=80,
        category_visibility_score=80,
    )
    visibility_action = _by_title(items, "Strengthen schema + sitemap")
    if visibility_action is not None:
        assert visibility_action["severity"] == "medium"
        assert visibility_action["severity_reason"] == "base_severity_passthrough"


# =========================================================================
# Top-competitor capture action (site 7)
# =========================================================================


def test_top_competitor_capture_action_carries_reason():
    """top_competitor with times_cited>=2 emits an action. By
    definition this site has has_named_competitors=True."""
    items = _generate_action_items(
        verdict_label=VERDICT_MISATTRIBUTED,
        visibility_runs=[],
        attribution_runs=[
            _attribution_run(query="q1", found=False, cited_host="ulta.com"),
            _attribution_run(query="q2", found=False, cited_host="ulta.com"),
        ],
        competitor_hosts=[{"host": "ulta.com", "times_cited": 5}],
        merchant_cited_runs=0,
        runs_with_any_citation=2,
        attribution_score=20,
        category_visibility_score=80,
    )
    drain = _by_title(items, "Top citation drain")
    assert drain is not None
    assert "severity_reason" in drain
    # base=high with gap=60 + has_failed_query + has_competitors_named
    # → Rule 3 fires (high, score_gap_medium+supporting_evidence)
    # OR base passthrough → high. Either is acceptable; both produce
    # a populated reason and severity stays at high.
    assert drain["severity"] == "high"


# =========================================================================
# Smoke: no AttributeError on minimal/edge inputs
# =========================================================================


def test_smoke_minimal_inputs_no_crash():
    """All zero / empty / None inputs should not crash. Pre-PR-6
    this worked; PR-6's added _score helper and category_visibility_score
    coercion shouldn't introduce a None-handling crash."""
    items = _generate_action_items(
        verdict_label=VERDICT_INVISIBLE,
        visibility_runs=[],
        attribution_runs=[],
        competitor_hosts=[],
        merchant_cited_runs=0,
        runs_with_any_citation=0,
    )
    assert isinstance(items, list)
    for it in items:
        assert "severity_reason" in it
