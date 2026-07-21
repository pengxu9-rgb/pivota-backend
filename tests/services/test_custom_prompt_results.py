"""Unit tests for build_custom_prompt_results — the merchant "Your Prompts"
per-lane results surface.

Custom prompts are probed once (axis="custom") but the per-SKU scorecard only
scores the auto-generated branded/category queries, so #820's probes ran +
billed without ever being shown. build_custom_prompt_results turns the persisted
axis="custom" runs into the open-vs-contested-lane table the feature promises.

Pure function over probe payloads — no DB, no LLM — so these run on the SQLite
v3-audit gate unchanged (reference_audit_tests_run_on_sqlite).
"""

from __future__ import annotations

from typing import Any, Dict, List

import services.agent_center_bd_report_service as bd

MERCHANT_BRAND = "BB Lab"
MERCHANT_HOST = "bblab.com"


def _run(
    query: str,
    *,
    axis: str = "custom",
    sources: List[Dict[str, str]] | None = None,
    excerpt: str | None = None,
    status: str | None = None,
) -> Dict[str, Any]:
    run: Dict[str, Any] = {
        "query": query,
        "axis_metadata": {"axis": axis},
        "grounding_sources": sources or [],
        "parsed": {},
    }
    if excerpt is not None:
        run["evidence_excerpt"] = excerpt
    if status is not None:
        run["status"] = status
    return run


def _payload(runs: List[Dict[str, Any]], provider: str = "gemini") -> Dict[str, Any]:
    return {"provider": provider, "raw_runs": runs}


def _src(title: str, host: str) -> Dict[str, str]:
    return {"uri": f"https://{host}/p", "title": title}


def _build(probe_runs_by_sku, custom_prompts=None):
    return bd.build_custom_prompt_results(
        probe_runs_by_sku,
        custom_prompts,
        merchant_host=MERCHANT_HOST,
        merchant_brand=MERCHANT_BRAND,
        merchant_vendors=(MERCHANT_BRAND,),
    )


def test_cited_open_lane_low_competition() -> None:
    """Brand grounded + ≤2 competitors → cited, lane=open."""
    runs = [
        _run(
            "best korean collagen for sleep",
            sources=[
                _src("BB Lab Official Store", MERCHANT_HOST),
                _src("Sephora", "sephora.com"),
            ],
            excerpt="BB Lab Good Night Collagen is a top pick...",
        )
    ]
    out = _build({"sku_a": [_payload(runs)]})
    assert len(out) == 1
    row = out[0]
    assert row["prompt"] == "best korean collagen for sleep"
    assert row["cited"] is True
    assert row["lane"] == "open"
    assert row["runs"] == 1
    assert row["runs_cited"] == 1
    assert any("BB Lab" in s for s in row["cited_sources"])
    # Competitors are keyed by the resolved publisher DOMAIN, not the source
    # display title (extract_cited_hosts: OpenAI web_search titles are page
    # headlines that would leak into top_cited_hosts as fake hosts).
    assert "sephora.com" in row["competitors"]
    assert row["evidence_excerpt"].startswith("BB Lab Good Night")


def test_cited_contested_lane_high_competition() -> None:
    """Brand grounded but >2 competitors → cited, lane=contested."""
    runs = [
        _run(
            "collagen before bed",
            sources=[
                _src("BB Lab", MERCHANT_HOST),
                _src("Olive Young Global", "oliveyoung.com"),
                _src("Sephora", "sephora.com"),
                _src("iHerb", "iherb.com"),
            ],
        )
    ]
    out = _build({"sku_a": [_payload(runs)]})
    assert out[0]["cited"] is True
    assert out[0]["lane"] == "contested"
    assert out[0]["competitors_count"] >= 3


def test_absent_lane_competitors_own_it() -> None:
    """Grounded answer that never cites the brand → not cited, lane=absent,
    competitors surfaced so the merchant sees who won the lane."""
    runs = [
        _run(
            "vegan collagen supplement",
            sources=[
                _src("Olive Young Global", "oliveyoung.com"),
                _src("Sephora", "sephora.com"),
            ],
        )
    ]
    out = _build({"sku_a": [_payload(runs)]})
    assert out[0]["cited"] is False
    assert out[0]["lane"] == "absent"
    assert out[0]["cited_sources"] == []
    # Domain-keyed (see test_cited_open_lane_low_competition).
    assert "oliveyoung.com" in out[0]["competitors"]
    assert "sephora.com" in out[0]["competitors"]


def test_no_signal_when_no_grounding() -> None:
    """Probe ran but returned no grounding at all → no_signal (thin demand),
    distinct from a measured 'absent'."""
    runs = [_run("an extremely niche made-up query", sources=[])]
    out = _build({"sku_a": [_payload(runs)]})
    assert out[0]["cited"] is False
    assert out[0]["lane"] == "no_signal"


def test_requested_prompt_with_zero_runs_surfaced_honestly() -> None:
    """A requested custom prompt that produced no runs (dropped/failed) is
    surfaced as no_signal — never silently dropped (same honesty rule as #820)."""
    # Probe set only contains ONE of the two requested prompts.
    runs = [_run("prompt that ran", sources=[_src("BB Lab", MERCHANT_HOST)])]
    out = _build(
        {"sku_a": [_payload(runs)]},
        custom_prompts=["prompt that ran", "prompt that never ran"],
    )
    by_prompt = {r["prompt"]: r for r in out}
    assert by_prompt["prompt that ran"]["cited"] is True
    assert "prompt that never ran" in by_prompt
    assert by_prompt["prompt that never ran"]["lane"] == "no_signal"
    assert by_prompt["prompt that never ran"]["runs"] == 0


def test_requested_order_is_preserved() -> None:
    runs = [
        _run("b prompt", sources=[_src("BB Lab", MERCHANT_HOST)]),
        _run("a prompt", sources=[_src("BB Lab", MERCHANT_HOST)]),
    ]
    out = _build(
        {"sku_a": [_payload(runs)]},
        custom_prompts=["a prompt", "b prompt"],
    )
    assert [r["prompt"] for r in out] == ["a prompt", "b prompt"]


def test_non_custom_axis_runs_are_ignored() -> None:
    """Only axis=custom runs feed this surface; branded/category probes belong
    to the scorecard, not the Your-Prompts table."""
    runs = [
        _run("auto branded query", axis="branded",
             sources=[_src("BB Lab", MERCHANT_HOST)]),
        _run("auto category query", axis="category_visibility",
             sources=[_src("Sephora", "sephora.com")]),
        _run("my custom lane", axis="custom",
             sources=[_src("BB Lab", MERCHANT_HOST)]),
    ]
    out = _build({"sku_a": [_payload(runs)]})
    assert [r["prompt"] for r in out] == ["my custom lane"]


def test_same_prompt_dedups_across_skus_and_providers() -> None:
    """Custom prompts ride the first SKU, but be tolerant: the same prompt seen
    in multiple groups / providers merges into one row with aggregated runs."""
    g = _payload(
        [_run("shared lane", sources=[_src("BB Lab", MERCHANT_HOST)])],
        provider="gemini",
    )
    d = _payload(
        [_run("shared lane", sources=[_src("Sephora", "sephora.com")])],
        provider="deepseek",
    )
    out = _build({"sku_a": [g], "sku_b": [d]})
    assert len(out) == 1
    assert out[0]["prompt"] == "shared lane"
    assert out[0]["runs"] == 2
    # Brand cited in 1 of the 2 provider runs.
    assert out[0]["cited"] is True
    assert out[0]["runs_cited"] == 1


def test_probe_failed_runs_skipped() -> None:
    runs = [
        _run("lane with a failed provider", status="probe_failed"),
        _run("lane with a failed provider",
             sources=[_src("BB Lab", MERCHANT_HOST)]),
    ]
    out = _build({"sku_a": [_payload(runs)]})
    assert out[0]["runs"] == 1
    assert out[0]["cited"] is True


def test_empty_input_returns_empty_list() -> None:
    assert _build({}) == []
    assert _build({"sku_a": []}) == []
    assert _build({}, custom_prompts=[]) == []
