"""
Unit tests for `scripts/agent_center_baseline.py` — only the PURE
evaluation logic (`evaluate_baseline`). The script's I/O path (httpx →
PIVOTA-Agent → Gemini) is intentionally not tested here because that's
the production path the script is meant to validate; mocking it would
defeat the purpose. Real end-to-end runs are the user's responsibility
(see the script's docstring).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict


# Make `scripts/` importable from tests.
_HERE = os.path.dirname(__file__)
_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def _result(visibility_score: int = 0, runs: int = 1, with_grounding: bool = False) -> Dict[str, Any]:
    raw_runs = []
    for _ in range(runs):
        raw_runs.append({
            "url_match": {"in_grounding": with_grounding, "in_text": False},
            "grounding_chunks": ["https://example.com/cited"] if with_grounding else [],
        })
    return {
        "scores": {"visibility_score": visibility_score, "attribution_echo_rate": 0},
        "runs_count": runs,
        "raw_runs": raw_runs,
        "findings": [],
    }


def test_positive_baseline_passes_when_scores_high_and_grounding_match() -> None:
    from agent_center_baseline import evaluate_baseline
    ok, fails = evaluate_baseline(
        label="POSITIVE",
        expectation="positive",
        visibility_result=_result(visibility_score=70),
        attribution_result=_result(visibility_score=80, with_grounding=True),
    )
    assert ok is True, fails
    assert fails == []


def test_positive_baseline_fails_when_visibility_too_low() -> None:
    from agent_center_baseline import evaluate_baseline
    ok, fails = evaluate_baseline(
        label="POSITIVE",
        expectation="positive",
        visibility_result=_result(visibility_score=10),
        attribution_result=_result(visibility_score=80, with_grounding=True),
        pos_visibility_min=30,
    )
    assert ok is False
    assert any("visibility_score=10" in f for f in fails)


def test_positive_baseline_fails_when_attribution_too_low() -> None:
    from agent_center_baseline import evaluate_baseline
    ok, fails = evaluate_baseline(
        label="POSITIVE",
        expectation="positive",
        visibility_result=_result(visibility_score=70),
        attribution_result=_result(visibility_score=20, with_grounding=True),
        pos_attribution_min=50,
    )
    assert ok is False
    assert any("attribution_score=20" in f for f in fails)


def test_positive_baseline_fails_when_no_grounding_match() -> None:
    """Critical: the grounding-match assertion is what protects against
    "Gemini self-reported the URL but didn't actually cite it" — i.e.
    the bug that PR 13's post-hoc URL match was supposed to fix.
    Regression here = PR 13 has been undone."""
    from agent_center_baseline import evaluate_baseline
    ok, fails = evaluate_baseline(
        label="POSITIVE",
        expectation="positive",
        visibility_result=_result(visibility_score=70),
        attribution_result=_result(visibility_score=80, with_grounding=False),
    )
    assert ok is False
    assert any("url_match.in_grounding=true" in f for f in fails)


def test_negative_baseline_passes_when_visibility_low() -> None:
    from agent_center_baseline import evaluate_baseline
    ok, fails = evaluate_baseline(
        label="NEGATIVE",
        expectation="negative",
        visibility_result=_result(visibility_score=10),
    )
    assert ok is True
    assert fails == []


def test_negative_baseline_fails_when_visibility_high() -> None:
    """Hallucination check: if the probe scores a clearly-bogus product
    above the threshold, something is wrong with grounding or scoring."""
    from agent_center_baseline import evaluate_baseline
    ok, fails = evaluate_baseline(
        label="NEGATIVE",
        expectation="negative",
        visibility_result=_result(visibility_score=80),
        neg_visibility_max=30,
    )
    assert ok is False
    assert any("hallucinating" in f for f in fails)


def test_unknown_expectation_fails_loudly() -> None:
    from agent_center_baseline import evaluate_baseline
    ok, fails = evaluate_baseline(
        label="X",
        expectation="bogus",
        visibility_result=_result(),
    )
    assert ok is False
    assert any("unknown expectation" in f for f in fails)


def test_has_grounding_match_helper_handles_empty_runs() -> None:
    """Defensive: the helper should not throw on empty / missing
    raw_runs structures."""
    from agent_center_baseline import _has_grounding_match
    assert _has_grounding_match({}) is False
    assert _has_grounding_match({"raw_runs": []}) is False
    assert _has_grounding_match({"raw_runs": [{}]}) is False
    assert _has_grounding_match({"raw_runs": [{"url_match": {"in_grounding": False}}]}) is False
    assert _has_grounding_match({"raw_runs": [{"url_match": {"in_grounding": True}}]}) is True
