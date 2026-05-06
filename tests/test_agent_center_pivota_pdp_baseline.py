"""
Unit tests for `scripts/agent_center_pivota_pdp_baseline.py` — only the
PURE aggregation + markdown rendering. The probe/HTTP path is the
production path the script is meant to validate; mocking it would defeat
the purpose. Real end-to-end runs are the operator's responsibility (see
the script's docstring).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List


_HERE = os.path.dirname(__file__)
_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def _seed(sig_id: str, vendor: str = "X", product_type: str = "thing") -> Dict[str, str]:
    return {
        "sig_id": sig_id,
        "product_title": f"{sig_id} title",
        "product_vendor": vendor,
        "product_type": product_type,
    }


def _ok_result(
    sig_id: str,
    *,
    visibility_score: int,
    attribution_score: int,
    merchant_cited_runs: int = 0,
    runs_with_any_citation: int = 0,
    verdict_label: str = "PARTIAL",
) -> Dict[str, Any]:
    return {
        "status": "ok",
        "seed": _seed(sig_id),
        "report": {
            "verdict": {
                "label": verdict_label,
                "visibility_score": visibility_score,
                "attribution_score": attribution_score,
            },
            "attribution": {
                "merchant_cited_runs": merchant_cited_runs,
                "runs_with_any_citation": runs_with_any_citation,
            },
        },
    }


def test_aggregate_handles_all_succeeded_runs() -> None:
    from agent_center_pivota_pdp_baseline import aggregate_per_pdp_results
    per_pdp = [
        _ok_result("sig_1", visibility_score=80, attribution_score=60, merchant_cited_runs=2, runs_with_any_citation=3),
        _ok_result("sig_2", visibility_score=40, attribution_score=20, merchant_cited_runs=0, runs_with_any_citation=2),
        _ok_result("sig_3", visibility_score=60, attribution_score=40, merchant_cited_runs=1, runs_with_any_citation=3),
    ]
    agg = aggregate_per_pdp_results(per_pdp)
    assert agg["seeds_count"] == 3
    assert agg["succeeded_count"] == 3
    assert agg["failed_count"] == 0
    assert agg["median_visibility"] == 60
    assert agg["median_attribution"] == 40
    assert agg["cited_count"] == 2  # sig_1 + sig_3 had merchant_cited_runs > 0
    assert agg["not_cited_seeds"] == ["sig_2"]


def test_aggregate_separates_failed_seeds() -> None:
    from agent_center_pivota_pdp_baseline import aggregate_per_pdp_results
    per_pdp = [
        _ok_result("sig_1", visibility_score=50, attribution_score=50, merchant_cited_runs=1, runs_with_any_citation=1),
        {"status": "error", "seed": _seed("sig_2"), "error": "upstream timeout"},
    ]
    agg = aggregate_per_pdp_results(per_pdp)
    assert agg["succeeded_count"] == 1
    assert agg["failed_count"] == 1
    assert len(agg["failed"]) == 1
    assert agg["failed"][0]["error"] == "upstream timeout"
    assert agg["failed"][0]["sig_id"] == "sig_2"
    # Medians only consider successful runs.
    assert agg["median_visibility"] == 50


def test_aggregate_handles_all_failed() -> None:
    """Operational sanity: nothing succeeded → no medians, but the
    aggregate shape is still well-formed (no crashes, no None
    arithmetic) so downstream renderers can show a clear failure
    state."""
    from agent_center_pivota_pdp_baseline import aggregate_per_pdp_results
    per_pdp = [
        {"status": "error", "seed": _seed("sig_1"), "error": "boom"},
        {"status": "error", "seed": _seed("sig_2"), "error": "boom"},
    ]
    agg = aggregate_per_pdp_results(per_pdp)
    assert agg["succeeded_count"] == 0
    assert agg["failed_count"] == 2
    assert agg["median_visibility"] is None
    assert agg["median_attribution"] is None
    assert agg["cited_count"] == 0
    assert agg["not_cited_seeds"] == []


def test_aggregate_per_pdp_summary_carries_seed_metadata() -> None:
    """Per-PDP rows should expose vendor + title + verdict so the
    markdown table can render full BD-friendly identification."""
    from agent_center_pivota_pdp_baseline import aggregate_per_pdp_results
    per_pdp = [
        _ok_result(
            "sig_xyz",
            visibility_score=70,
            attribution_score=50,
            merchant_cited_runs=2,
            runs_with_any_citation=3,
            verdict_label="VISIBLE BUT MISATTRIBUTED",
        ),
    ]
    agg = aggregate_per_pdp_results(per_pdp)
    row = agg["per_pdp_summary"][0]
    assert row["sig_id"] == "sig_xyz"
    assert row["verdict"] == "VISIBLE BUT MISATTRIBUTED"
    assert row["visibility_score"] == 70
    assert row["attribution_score"] == 50
    assert row["merchant_cited_runs"] == 2
    assert row["runs_with_any_citation"] == 3


def test_render_markdown_smoke() -> None:
    from agent_center_pivota_pdp_baseline import (
        aggregate_per_pdp_results,
        render_markdown,
    )
    per_pdp = [
        _ok_result(
            "sig_one",
            visibility_score=70,
            attribution_score=33,
            merchant_cited_runs=0,
            runs_with_any_citation=3,
        ),
        _ok_result(
            "sig_two",
            visibility_score=100,
            attribution_score=66,
            merchant_cited_runs=2,
            runs_with_any_citation=3,
        ),
    ]
    agg = aggregate_per_pdp_results(per_pdp)
    md = render_markdown(
        agg,
        timestamp="2026-05-06T10:00:00+00:00",
        provider="gemini",
        max_runs=3,
    )
    # Title + meta line
    assert "# Pivota PDP Self-Baseline Report" in md
    assert "Provider: `gemini`" in md
    # Operational health figures
    assert "Seeds tested: **2**" in md
    assert "PDPs cited by Gemini grounding (at least once): **1** / 2" in md
    # Action list for un-cited seed
    assert "https://agent.pivota.cc/products/sig_one" in md
    # BD pitch baseline section
    assert "BD-pitch baseline" in md
    assert "median **visibility score is 85" in md  # (70+100)/2
    # Per-PDP detail table
    assert "| PDP | Vendor | Verdict |" in md


def test_render_markdown_handles_zero_successes() -> None:
    from agent_center_pivota_pdp_baseline import (
        aggregate_per_pdp_results,
        render_markdown,
    )
    per_pdp = [
        {"status": "error", "seed": _seed("sig_a"), "error": "upstream 503"},
    ]
    agg = aggregate_per_pdp_results(per_pdp)
    md = render_markdown(
        agg,
        timestamp="2026-05-06T10:00:00+00:00",
        provider="gemini",
        max_runs=3,
    )
    # No median figures, but structure still renders.
    assert "no successful probes" in md
    assert "Probes that failed (upstream error)" in md
    assert "upstream 503" in md


def test_seeds_list_matches_sitemap_seeds_count() -> None:
    """Drift detector: the script's PIVOTA_PDP_SEEDS list mirrors
    `pivota-agent-ui/src/app/sitemap-seeds.ts:SITEMAP_SEED_PRODUCT_IDS`.
    When the sitemap seed count changes, this test fails to flag the
    duplication needs updating. (We keep the duplicate intentionally —
    see the script docstring.)"""
    from agent_center_pivota_pdp_baseline import PIVOTA_PDP_SEEDS
    # As of plan Phase 1c, sitemap-seeds.ts has 6 entries. If you add
    # or remove a seed there, update this expected count and the seeds
    # list in agent_center_pivota_pdp_baseline.py.
    assert len(PIVOTA_PDP_SEEDS) == 6
    sig_ids = [s["sig_id"] for s in PIVOTA_PDP_SEEDS]
    # All entries should be sig_ canonical IDs.
    for sig in sig_ids:
        assert sig.startswith("sig_"), f"Expected canonical sig_ id, got {sig!r}"
    # All entries should have product attributes the BD probe needs.
    for s in PIVOTA_PDP_SEEDS:
        assert s.get("product_title")
        assert s.get("product_vendor")
        assert s.get("product_type")
