"""
Phase C-4 (PR-A) — separation enforcement test.

The audit report has two distinct content surfaces:

  * **Diagnostic** — verdict.explanation + action_items[*].body. Must be
    purely descriptive: cite this audit's actual evidence (failed query
    counts, top retailers, gap percentages) and the typical root-cause
    framing. No marketing.

  * **Pivota value-prop** — what_pivota_changes (and PR-B's
    merchant_view.pivota_value_prop). The only sanctioned home for
    "Pivota's canonical PDP", "in-chat checkout via Pivota's agentic-
    commerce protocol", "12% → 25-30%" macros, and similar BD framing.

This test rejects pitch tokens leaking into the diagnostic surfaces.
If a future PR genuinely needs to mention Pivota in an action body,
move the value-prop into what_pivota_changes (the right home) — don't
weaken this test.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services.agent_center_bd_report_service import (
    VERDICT_INVISIBLE,
    VERDICT_MISATTRIBUTED,
    VERDICT_PARTIAL,
    VERDICT_STRONG,
    VERDICT_VIA_RETAILERS,
    _generate_action_items,
    verdict_for,
)


PITCH_TOKENS = [
    "Pivota",
    "in-chat checkout",
    "agentic-commerce",
    "12%",
    "25-30%",
    "complementary to existing retail",
    "AI-native commerce",
    "AI-channel-native",
]


def _attribution_run(query: str, *, merchant_url_found: bool = False) -> Dict[str, Any]:
    return {
        "query": query,
        "parsed": {"merchant_url_found": merchant_url_found},
        "grounding_chunks": ["https://sephora.com/p/x"],
    }


def _visibility_run(query: str, *, product_visible: bool = False) -> Dict[str, Any]:
    return {
        "query": query,
        "parsed": {"product_visible": product_visible},
        "grounding_chunks": ["https://sephora.com/p/x"] if product_visible else [],
    }


def _make_action_items(verdict_label: str) -> List[Dict[str, Any]]:
    return _generate_action_items(
        verdict_label=verdict_label,
        visibility_runs=[
            _visibility_run("query a"),
            _visibility_run("query b"),
            _visibility_run("query c"),
        ],
        attribution_runs=[
            _attribution_run("buyer query 1"),
            _attribution_run("buyer query 2"),
            _attribution_run("buyer query 3"),
        ],
        competitor_hosts=[
            {"host": "sephora.com", "times_cited": 3},
            {"host": "ulta.com", "times_cited": 2},
        ],
        merchant_cited_runs=0,
        runs_with_any_citation=3,
        visibility_score=20,
        attribution_score=10,
        category_retailer_hosts=[
            {"host": "sephora.com", "times_cited": 3},
            {"host": "ulta.com", "times_cited": 2},
        ],
        category_competitor_brands=[{"name": "Drunk Elephant", "times_cited": 2}],
    )


def _evidence_dict() -> Dict[str, Any]:
    return {
        "attribution_runs_total": 9,
        "merchant_cited_runs": 0,
        "top_retailers": ["sephora.com", "ulta.com"],
        "competitive_pressure_framing": None,
        "category_score": 70,
        "gap_pct": 70,
        "failed_attribution_query_sample": ["q1", "q2"],
    }


# -----------------------------------------------------------------
# verdict.explanation — every tier must be pitch-free
# -----------------------------------------------------------------


@pytest.mark.parametrize(
    "vis,attr,cat,expected_label",
    [
        (5, 0, None, VERDICT_INVISIBLE),
        (60, 10, None, VERDICT_MISATTRIBUTED),
        (10, 10, 80, VERDICT_VIA_RETAILERS),
        (85, 75, None, VERDICT_STRONG),
        (50, 40, None, VERDICT_PARTIAL),
    ],
)
def test_verdict_explanation_contains_no_pitch_tokens(vis, attr, cat, expected_label):
    label, explanation = verdict_for(
        visibility_score=vis,
        attribution_score=attr,
        category_visibility_score=cat,
        evidence=_evidence_dict(),
    )
    assert label == expected_label, f"unexpected verdict: {label}"
    for token in PITCH_TOKENS:
        assert token not in explanation, (
            f"pitch token {token!r} leaked into {label} verdict explanation: "
            f"{explanation!r}"
        )


# -----------------------------------------------------------------
# action_items bodies — every tier must be pitch-free
# -----------------------------------------------------------------


@pytest.mark.parametrize(
    "verdict_label",
    [
        VERDICT_INVISIBLE,
        VERDICT_MISATTRIBUTED,
        VERDICT_VIA_RETAILERS,
        VERDICT_STRONG,
        VERDICT_PARTIAL,
    ],
)
def test_action_item_bodies_contain_no_pitch_tokens(verdict_label):
    items = _make_action_items(verdict_label)
    assert items, f"expected at least one action item for {verdict_label}"
    for item in items:
        # Phase 0 + Phase D carve-out: lever="pivota_integration"
        # and lever="gsc_integration" actions legitimately mention
        # Pivota / canonical PDP / in-chat checkout — the whole point
        # of these actions is to surface Pivota's value prop where
        # the merchant can act on it. All other actions must remain
        # pitch-free. See tests/test_integration_aware_actions.py
        # and tests/test_gsc_integration.py for dedicated coverage.
        lever = (item.get("lever") or "").lower()
        if lever in ("pivota_integration", "gsc_integration"):
            continue
        body = item.get("body") or ""
        title = item.get("title") or ""
        for token in PITCH_TOKENS:
            assert token not in body, (
                f"pitch token {token!r} leaked into {verdict_label} action body: "
                f"{title!r} → {body!r}"
            )
            assert token not in title, (
                f"pitch token {token!r} leaked into {verdict_label} action title: "
                f"{title!r}"
            )
