"""Deep-tier slice 3: route wiring + substitution-rate rollup (spec §5/§6).

Contracts under test:
  - launch/preview tier resolution: unknown tier and flag-off deep are 422
    (never a silent downgrade — billing and worker must agree on the tier);
    the effective prompt count follows the tier unless explicitly overridden;
  - the request models no longer force a defaulted prompts_per_sku (the
    slice-1 CAUTION: a persisted default 40 would shadow the deep budget);
  - build_deep_landscape_rollup: substitution rate + Defend/Contest/Skip
    contest map from internal comparison runs only; None on standard runs;
  - sanitize_report_for_merchant strips deep_landscape_internal keys AND
    internal comparison runs from raw_runs anywhere in the response payload
    (GET returns partial_result_jsonb, so the response boundary must match
    the report-assembly loader), without mutating the stored original.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import HTTPException

from routes import audit_runs_routes as r
from services import agent_center_bd_report_service as m
from services.deep_tier_prompts import build_deep_landscape_rollup


def _set_deep_flag(monkeypatch, value: bool) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "audit_deep_tier_enabled", value, raising=False)


# ---- route tier resolution ---------------------------------------------------

def test_tier_resolution_defaults_and_flag_gate(monkeypatch):
    _set_deep_flag(monkeypatch, False)
    assert r._resolve_request_audit_tier(None) == "standard"
    assert r._resolve_request_audit_tier("standard") == "standard"
    with pytest.raises(HTTPException) as exc:
        r._resolve_request_audit_tier("deep")
    assert exc.value.status_code == 422

    _set_deep_flag(monkeypatch, True)
    assert r._resolve_request_audit_tier("DEEP ") == "deep"


def test_unknown_tier_is_422_not_silent_standard(monkeypatch):
    _set_deep_flag(monkeypatch, True)
    with pytest.raises(HTTPException) as exc:
        r._resolve_request_audit_tier("premium")
    assert exc.value.status_code == 422


def test_effective_prompts_follow_tier_unless_overridden():
    assert r._effective_prompts_per_sku(None, "standard") == 40
    assert r._effective_prompts_per_sku(None, "deep") == 80
    assert r._effective_prompts_per_sku(14, "deep") == 14


def test_request_models_no_longer_force_default_forty():
    body = r.CreateAuditRequest(merchant_id="m1", product_keys=["p1"])
    assert body.prompts_per_sku is None
    assert body.audit_tier == "standard"
    preview = r.AuditPreviewRequest(
        merchant_id="m1", scope=r.AuditPreviewScope(sku_keys=["s1"]),
    )
    assert preview.prompts_per_sku is None
    assert preview.audit_tier == "standard"


# ---- substitution-rate rollup ------------------------------------------------

def _cmp_run(
    query: str,
    *,
    merchant: bool = False,
    competitors: tuple = (),
    provider: str = "gemini",
) -> Dict[str, Any]:
    return {
        "query": query,
        "_provider": provider,
        "raw": "answer mentioning BB Lab" if merchant else "answer",
        "parsed": {
            "sku_mentioned": merchant,
            "competitors_appearing": list(competitors),
        },
        "url_match": {"in_grounding": False, "llm_self_report": {}},
        "axis_metadata": {"axis": "comparison", "prompt_source": "deep_tier"},
    }


def test_rollup_none_without_internal_comparison_runs():
    standard_run = _cmp_run("best collagen")
    standard_run["axis_metadata"] = {"axis": "category"}
    assert build_deep_landscape_rollup([standard_run], own_brand="BB Lab") is None
    assert build_deep_landscape_rollup([], own_brand="BB Lab") is None


def test_rollup_v2_volunteered_lane_drives_verdicts_and_rate():
    runs = [
        # ECHO lane: the prompt names the merchant — cited, but that's not
        # visibility and must not count toward GHD's verdict.
        _cmp_run("BB Lab collagen vs GHD", merchant=True, competitors=("GHD",)),
        # VOLUNTEERED lane, GHD-anchored, merchant cited -> 1/1 -> defend.
        _cmp_run("is GHD worth it", merchant=True, competitors=("GHD",),
                 provider="chatgpt"),
        # VOLUNTEERED, Vital Proteins-anchored: merchant absent, competitor
        # cited -> skip, and both runs are substitutions.
        _cmp_run("best alternatives to Vital Proteins",
                 competitors=("Vital Proteins", "Sports Research")),
        _cmp_run("problems with Vital Proteins", competitors=("Vital Proteins",)),
    ]
    rollup = build_deep_landscape_rollup(runs, own_brand="BB Lab")
    assert rollup["version"] == 2
    assert rollup["total_comparison_runs"] == 4
    assert rollup["merchant_cited_runs"] == 2
    assert rollup["volunteered_runs"] == 3
    assert rollup["volunteered_merchant_cited_runs"] == 1
    # Substitution rate is volunteered-lane only: 2 of 3.
    assert rollup["substitution_runs"] == 2
    assert rollup["substitution_rate"] == 0.667
    by_comp = {row["competitor"]: row for row in rollup["contest_map"]}
    assert by_comp["GHD"]["verdict"] == "defend"
    assert by_comp["GHD"]["volunteered_runs"] == 1
    assert by_comp["GHD"]["echo_runs"] == 1  # tallied, never verdict-bearing
    assert by_comp["Vital Proteins"]["verdict"] == "skip"
    # Cited in answers but never anchored in a query -> reported separately.
    answer_only = {row["name"] for row in rollup["answer_only_competitors"]}
    assert "Sports Research" in answer_only
    # Own brand never appears as a competitor anywhere.
    assert "BB Lab" not in by_comp and "BB Lab" not in answer_only


def test_rollup_echo_only_competitor_is_insufficient_data_not_defend():
    # HBN pilot failure shape: a competitor probed ONLY via merchant-anchored
    # prompts. v1 called this "defend"; v2 must refuse a verdict.
    runs = [
        _cmp_run("BB Lab collagen vs Anua", merchant=True, competitors=("Anua",)),
    ]
    rollup = build_deep_landscape_rollup(runs, own_brand="BB Lab")
    row = rollup["contest_map"][0]
    assert row["competitor"] == "Anua"
    assert row["verdict"] == "insufficient_data"
    assert rollup["volunteered_runs"] == 0
    assert rollup["substitution_rate"] == 0.0


def test_mention_detection_ignores_negations_in_raw_text():
    # HBN pilot failure shape: the probe's raw answer embeds its own analysis
    # ("the brand HBN is not mentioned in the search results") — raw-text
    # scanning counted that as a citation. v2 uses grounding-source facts.
    run = _cmp_run("is Paula's Choice worth it", competitors=())
    run["raw"] = (
        "The search results focused on Paula's Choice products. The merchant "
        "brand HBN is not mentioned in the provided search results."
    )
    rollup = build_deep_landscape_rollup([run], own_brand="HBN")
    assert rollup["merchant_cited_runs"] == 0


def test_mention_detection_counts_grounding_source_naming_brand():
    # The production-trusted positive signal: a cited grounding source whose
    # title names the brand.
    run = _cmp_run("is Paula's Choice worth it", competitors=())
    run["grounding_sources"] = [{
        "uri": "https://example.com/reviews/hbn-double-retinol",
        "title": "HBN Double Retinol Serum review",
    }]
    run["grounding_chunks"] = [run["grounding_sources"][0]["uri"]]
    rollup = build_deep_landscape_rollup([run], own_brand="HBN")
    assert rollup["merchant_cited_runs"] == 1


def test_substitution_is_per_run_not_per_query_union():
    # Same query, two providers: run 1 cites merchant AND GHD; run 2 cites
    # NOBODY. A row-level union would call run 2 a substitution (a competitor
    # appears somewhere on the row) — per-run attribution must not.
    runs = [
        _cmp_run("BB Lab collagen vs GHD", merchant=True, competitors=("GHD",)),
        _cmp_run("BB Lab collagen vs GHD", provider="chatgpt"),
    ]
    rollup = build_deep_landscape_rollup(runs, own_brand="BB Lab")
    assert rollup["total_comparison_runs"] == 2
    assert rollup["substitution_runs"] == 0
    assert rollup["substitution_rate"] == 0.0


def test_rollup_contest_verdict_partial_is_contest():
    runs = [
        _cmp_run("is GHD worth it", merchant=True, competitors=("GHD",)),
        _cmp_run("problems with GHD", competitors=("GHD",)),
        _cmp_run("GHD reviews", competitors=("GHD",)),
    ]
    rollup = build_deep_landscape_rollup(runs, own_brand="BB Lab")
    row = rollup["contest_map"][0]
    assert row["competitor"] == "GHD"
    assert row["verdict"] == "contest"  # 1/3 volunteered cited: >0 but <0.5


# ---- wedge-route deep tier ---------------------------------------------------

def test_wedge_request_model_accepts_audit_tier():
    from routes.merchant_audit_routes import MerchantUrlAuditRequest

    body = MerchantUrlAuditRequest(product_urls=["https://x.com/p"])
    assert body.audit_tier == "standard"
    body = MerchantUrlAuditRequest(
        product_urls=["https://x.com/p"], audit_tier="deep",
    )
    assert body.audit_tier == "deep"


# ---- merchant-response sanitizer --------------------------------------------

def test_sanitizer_strips_internal_key_and_comparison_runs_everywhere():
    internal_run = _cmp_run("x vs GHD")
    organic_run = _cmp_run("best collagen")
    organic_run["axis_metadata"] = {"axis": "category"}
    row = {
        "run_id": "run-1",
        "report_jsonb": {
            "brand_report": {
                "per_sku_reports": [{
                    "sku_key": "sku-1",
                    "deep_landscape_internal": {"substitution_rate": 0.5},
                }],
            },
        },
        "partial_result_jsonb": {
            "per_sku_probe_runs": {
                "sku-1": [{
                    "provider": "gemini",
                    "raw_runs": [internal_run, organic_run],
                }],
            },
        },
    }
    safe = m.sanitize_report_for_merchant(row)
    sku_report = safe["report_jsonb"]["brand_report"]["per_sku_reports"][0]
    assert "deep_landscape_internal" not in sku_report
    runs = safe["partial_result_jsonb"]["per_sku_probe_runs"]["sku-1"][0]["raw_runs"]
    assert [r_["query"] for r_ in runs] == ["best collagen"]
    # The stored original is untouched (deep copy).
    assert len(
        row["partial_result_jsonb"]["per_sku_probe_runs"]["sku-1"][0]["raw_runs"]
    ) == 2
    assert "deep_landscape_internal" in (
        row["report_jsonb"]["brand_report"]["per_sku_reports"][0]
    )
