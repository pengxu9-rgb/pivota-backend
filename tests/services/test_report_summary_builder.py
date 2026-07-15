from __future__ import annotations

from typing import Any, Dict

from services.report_summary_builder import (
    CONTRACT_VERSION,
    build_report_summary,
)


def _sku_report(
    *,
    sku_key: str = "sku-1",
    sku_title: str = "Hydra Serum",
    headline: str = "Get Hydra Serum indexed so AI can find it.",
    citation_score: int = 40,
    routability_score: int = 20,
    failing_prompts: Any = None,
    evidence_chips: Any = None,
) -> Dict[str, Any]:
    nba: Dict[str, Any] = {
        "primary_gap": "get_indexed",
        "headline": headline,
        "why_this_first": "It is not live in the AI surface yet.",
        "first_move": "Get it live and crawlable.",
        "evidence_summary": "Not indexed yet.",
        "how_to_track": ["indexing status", "citation rate"],
        "cta": {"label": "Get this product indexed", "target_sku_key": sku_key},
        "evidence_used": {},
    }
    if evidence_chips is not None:
        nba["evidence_used"]["failing_prompt_examples"] = evidence_chips
    report: Dict[str, Any] = {
        "sku_key": sku_key,
        "sku_title": sku_title,
        "scores": {
            "citation": {"score": citation_score},
            "routability": {"score": routability_score},
        },
        "band": "blocked",
        "band_display": {
            "band": "blocked",
            "label": "Not yet visible",
            "meaning": "AI cannot recommend this yet.",
        },
        "next_best_action": nba,
    }
    if failing_prompts is not None:
        report["failing_prompts"] = failing_prompts
    return report


def _brand_report(**overrides: Any) -> Dict[str, Any]:
    failing = [
        {
            "query": "best hydrating serum for dry skin",
            "axis": "category_discovery",
            "reason": "no first-party or correct-SKU grounded citation",
            "provider": "gemini",
            "evidence_run_id": "run-9",
        },
        {
            "query": "hydra serum reviews",
            "axis": "trust",
            "reason": "no first-party or correct-SKU grounded citation",
            "provider": "openai",
            "evidence_run_id": "run-10",
        },
    ]
    report: Dict[str, Any] = {
        "audit_run_id": "audit-1",
        "merchant_id": "m-1",
        "merchant_name": "GlowLab",
        "timestamp": "2026-07-15T00:00:00+00:00",
        "audit_mode": "per_sku",
        "providers": ["gemini", "openai"],
        "verify_providers": ["deepseek"],
        "per_sku_reports": [_sku_report(failing_prompts=failing)],
        "brand_rollup": {
            "brand_verdict_label": "Not yet recommended",
            "run_scores": {
                "avg_visibility": 42.0,
                "avg_attribution": 30.0,
                "avg_category_visibility": None,
            },
            "tracking": {
                "history": {
                    "most_recent_audit": {"run_id": "audit-0"},
                    "delta_from_most_recent": {
                        "visibility": 5,
                        "days_since_last_audit": 14,
                    },
                }
            },
        },
        "merchant_narrative": {
            "headline_story": "GlowLab is invisible to AI shopping agents today.",
            "verdict_label": "Not yet recommended",
            "verdict_explanation": "No independent source cites the brand.",
            "whats_working": {"summary": "Listed on amazon.com."},
            "where_youre_losing": {
                "summary": "No independent source recommends GlowLab.",
                "independently_recommended_for_category": False,
                "who_ai_cites_instead": {
                    "available": True,
                    "cited_hosts": [
                        {"host": "byrdie.com", "prompts_cited_count": 3},
                        {"host": "allure.com", "prompts_cited_count": 1},
                    ],
                    "competitors": [
                        {"name": "CeraVe", "times_named": 2},
                    ],
                    "note": None,
                },
            },
            "verify_summary_plain": {
                "text": "We fact-checked 2 of 3 cited answers; 1 flagged.",
                "flagged": 1,
            },
            "prioritized_actions": [
                {
                    "sku_title": "Hydra Serum",
                    "primary_gap": "get_indexed",
                    "headline": "Get Hydra Serum indexed so AI can find it.",
                    "first_move": "Get it live and crawlable.",
                    "why_this_first": "It is not live in the AI surface yet.",
                    "growth_phase": "create_and_distribute",
                },
            ],
            "honest_limits": ["Provider coverage: grounded on gemini, openai."],
        },
    }
    report.update(overrides)
    return report


def test_contract_envelope_and_score_block():
    out = build_report_summary(_brand_report())
    assert out["contract_version"] == CONTRACT_VERSION
    assert out["audit_run_id"] == "audit-1"
    assert out["subject"] == {
        "type": "brand",
        "merchant_id": "m-1",
        "merchant_name": "GlowLab",
    }
    score = out["score"]
    assert score["raw"] == 42.0
    assert score["display"] == 4.2  # one decimal, never int-rounded
    assert score["scale_max"] == 10
    assert score["band"] == "needs_work"
    assert score["band_thresholds"] == [6.0, 7.5, 9.0]
    # None subscores (category_visibility on per_sku runs) are omitted, not zeroed.
    assert [s["key"] for s in score["subscores"]] == ["visibility", "attribution"]
    assert score["delta"] == {
        "raw": 5,
        "previous_audit_run_id": "audit-0",
        "days_since_last_audit": 14,
    }


def test_display_score_preserves_small_deltas():
    low = build_report_summary(
        _brand_report(
            brand_rollup={"run_scores": {"avg_visibility": 42.0}},
        )
    )
    high = build_report_summary(
        _brand_report(
            brand_rollup={"run_scores": {"avg_visibility": 47.0}},
        )
    )
    assert low["score"]["display"] == 4.2
    assert high["score"]["display"] == 4.7


def test_verdict_reuses_narrative_verbatim():
    out = build_report_summary(_brand_report())
    verdict = out["verdict"]
    assert verdict["headline"] == (
        "GlowLab is invisible to AI shopping agents today."
    )
    assert verdict["label"] == "Not yet recommended"
    assert verdict["primary_gap"] == "get_indexed"


def test_top_actions_carry_supporting_prompts_via_real_join():
    out = build_report_summary(_brand_report())
    actions = out["top_actions"]
    assert len(actions) == 1
    action = actions[0]
    assert action["headline"] == "Get Hydra Serum indexed so AI can find it."
    assert action["target_sku_key"] == "sku-1"
    assert action["supporting_prompts_basis"] == "evidence_used"
    assert [p["query"] for p in action["supporting_prompts"]] == [
        "best hydrating serum for dry skin",
        "hydra serum reviews",
    ]
    assert action["supporting_prompts"][0]["axis"] == "category_discovery"
    assert action["supporting_prompts"][0]["evidence_run_id"] == "run-9"


def test_action_evidence_merges_chips_and_failing_prompts():
    # Chips lead (dedup precedence) but the sku failing_prompts fill the cap —
    # chips alone are a [:5] slice of a provider-grouped list and can be
    # single-engine (live Mojawa run: 5/5 Gemini).
    chips = [
        {
            "query": "best serum with hyaluronic acid",
            "reason": "no first-party citation",
            "provider": "gemini",
            "competitors_named": ["CeraVe"],
        }
    ]
    report = _brand_report()
    report["per_sku_reports"] = [
        _sku_report(
            failing_prompts=report["per_sku_reports"][0]["failing_prompts"],
            evidence_chips=chips,
        )
    ]
    out = build_report_summary(report)
    prompts = out["top_actions"][0]["supporting_prompts"]
    # Chip first; then provider round-robin pulls the openai row before the
    # second gemini row.
    assert [p["query"] for p in prompts] == [
        "best serum with hyaluronic acid",
        "hydra serum reviews",
        "best hydrating serum for dry skin",
    ]
    assert prompts[0]["competitors_named"] == ["CeraVe"]


def test_evidence_shows_every_engine_that_measured_a_loss():
    # Live-Mojawa regression: gemini rows precede chatgpt rows in the grouped
    # list; the capped selection must still surface both engines.
    failing = [
        {"query": "bone conduction open-ear daily sports no pressure", "provider": "gemini"},
        {"query": "gemini niche loss two attributes stacked", "provider": "gemini"},
        {"query": "gemini niche loss three attributes stacked", "provider": "gemini"},
        {"query": "ip68 waterproof headphones for competitive swimmers", "provider": "chatgpt"},
    ]
    report = _brand_report()
    report["per_sku_reports"] = [_sku_report(failing_prompts=failing)]
    out = build_report_summary(report)
    providers = [p["provider"] for p in out["top_actions"][0]["supporting_prompts"]]
    assert "chatgpt" in providers and "gemini" in providers


def test_interleave_by_provider_round_robins():
    from services.win_plan_builder import interleave_by_provider

    rows = [
        {"provider": "gemini", "n": 1},
        {"provider": "gemini", "n": 2},
        {"provider": "chatgpt", "n": 3},
        {"provider": "chatgpt", "n": 4},
    ]
    assert [r["n"] for r in interleave_by_provider(rows)] == [1, 3, 2, 4]
    assert interleave_by_provider([]) == []


def test_no_join_means_no_prompts_never_inferred():
    report = _brand_report()
    report["per_sku_reports"] = [_sku_report(failing_prompts=[])]
    out = build_report_summary(report)
    action = out["top_actions"][0]
    assert action["supporting_prompts"] == []
    assert action["supporting_prompts_basis"] == "none"


def test_supporting_prompts_capped_at_three():
    failing = [
        {"query": f"query {i}", "axis": "intent", "provider": "gemini"}
        for i in range(5)
    ]
    report = _brand_report()
    report["per_sku_reports"] = [_sku_report(failing_prompts=failing)]
    out = build_report_summary(report)
    assert len(out["top_actions"][0]["supporting_prompts"]) == 3


def test_actions_capped_and_truncation_disclosed():
    report = _brand_report()
    actions = [
        {
            "sku_title": f"SKU {i}",
            "primary_gap": "get_indexed",
            "headline": f"Action {i}",
        }
        for i in range(5)
    ]
    report["merchant_narrative"]["prioritized_actions"] = actions
    out = build_report_summary(report)
    assert len(out["top_actions"]) == 3
    assert out["meta"]["actions_total"] == 5


def test_top_findings_map_narrative_sections():
    out = build_report_summary(_brand_report())
    findings = out["top_findings"]
    assert [f["kind"] for f in findings] == [
        "independent_endorsement",
        "findability",
        "answer_quality",
    ]
    # Not category-endorsed → the losing finding is high severity; one flagged
    # verify answer → answer-quality is medium.
    assert findings[0]["severity"] == "high"
    assert findings[2]["severity"] == "medium"
    assert findings[0]["evidence_summary"] == (
        "No independent source recommends GlowLab."
    )


def test_competitive_snapshot_hosts_and_competitors():
    out = build_report_summary(_brand_report())
    snapshot = out["competitive_snapshot"]
    assert snapshot["available"] is True
    assert snapshot["top_cited_hosts"] == ["byrdie.com", "allure.com"]
    assert snapshot["competitors_named"] == ["CeraVe"]


def test_sku_summary_uses_weakest_dimension():
    out = build_report_summary(_brand_report())
    sku = out["sku_summaries"][0]
    # citation 40, routability 20 → overall = min = 20 (mirrors _overall_score).
    assert sku["score"]["raw"] == 20
    assert sku["score"]["display"] == 2.0
    # No contract band at SKU level: band_display (the per-SKU card's own
    # ladder) is the single authority, so the two can never contradict.
    assert "band" not in sku["score"]
    assert sku["band_display"]["label"] == "Not yet visible"
    assert sku["action_headline"] == (
        "Get Hydra Serum indexed so AI can find it."
    )


def test_sku_summary_ignores_non_numeric_dimension_scores():
    # One malformed dimension must degrade THAT value, not raise TypeError in
    # min() and nuke the whole summary to null via the route's blanket except.
    report = _brand_report()
    report["per_sku_reports"][0]["scores"] = {
        "citation": {"score": 40},
        "routability": {"score": "corrupt"},
    }
    out = build_report_summary(report)
    assert out["sku_summaries"][0]["score"]["raw"] == 40


def test_action_match_requires_gap_agreement():
    # Two SKUs share a headline but carry different primary gaps (the producer
    # dedup key is (gap, headline), so both survive). Each action must attach
    # its OWN SKU's evidence, never the first headline hit's.
    report = _brand_report()
    first = report["per_sku_reports"][0]
    second = _sku_report(
        sku_key="sku-2",
        sku_title="Other Serum",
        headline=first["next_best_action"]["headline"],
        failing_prompts=[
            {"query": "other sku query", "axis": "trust", "provider": "gemini"}
        ],
    )
    second["next_best_action"]["primary_gap"] = "open_lane_capture"
    report["per_sku_reports"].append(second)
    report["merchant_narrative"]["prioritized_actions"].append(
        {
            "sku_title": "Other Serum",
            "primary_gap": "open_lane_capture",
            "headline": first["next_best_action"]["headline"],
        }
    )
    out = build_report_summary(report)
    assert out["top_actions"][1]["target_sku_key"] == "sku-2"
    assert [p["query"] for p in out["top_actions"][1]["supporting_prompts"]] == [
        "other sku query"
    ]


def test_empty_report_degrades_without_raising():
    out = build_report_summary({})
    assert out["contract_version"] == CONTRACT_VERSION
    assert out["score"]["raw"] is None
    assert out["score"]["display"] is None
    assert out["score"]["band"] is None
    assert out["top_findings"] == []
    assert out["top_actions"] == []
    assert out["sku_summaries"] == []
    assert out["verdict"]["headline"] is None
    assert out["competitive_snapshot"]["available"] is False


def test_malformed_sections_degrade_without_raising():
    out = build_report_summary(
        {
            "merchant_narrative": "not-a-dict",
            "brand_rollup": ["not", "a", "dict"],
            "per_sku_reports": [None, "junk", {"scores": "junk"}],
        }
    )
    assert out["top_actions"] == []
    assert len(out["sku_summaries"]) == 1  # only the dict entry survives
    assert out["sku_summaries"][0]["score"]["raw"] is None


def test_band_boundaries():
    def band_of(raw: float) -> str:
        return build_report_summary(
            _brand_report(brand_rollup={"run_scores": {"avg_visibility": raw}})
        )["score"]["band"]

    assert band_of(59.9) == "needs_work"
    assert band_of(60.0) == "pass"
    assert band_of(75.0) == "good"
    assert band_of(90.0) == "excellent"


def test_meta_disclosures():
    out = build_report_summary(_brand_report())
    meta = out["meta"]
    assert meta["products_audited"] == 1
    assert meta["providers"] == ["gemini", "openai"]
    assert meta["honest_limits"] == [
        "Provider coverage: grounded on gemini, openai."
    ]


def test_shape_url_audit_response_attaches_report_summary():
    from routes.merchant_audit_routes import _shape_url_audit_response

    row = {
        "run_id": "r1",
        "report_jsonb": _brand_report(),
        "partial_result_jsonb": {},
    }
    out = _shape_url_audit_response(row)
    summary = out["report_summary"]
    assert summary["contract_version"] == CONTRACT_VERSION
    assert summary["subject"]["type"] == "brand"
    # Dark + additive: the existing envelope keys are untouched.
    assert out["status"] == "succeeded"
    assert out["per_sku_reports"] == _brand_report()["per_sku_reports"]


def test_get_run_summary_only_returns_slim_payload():
    import asyncio

    import routes.merchant_audit_routes as mar

    row = {
        "run_id": "r1",
        "merchant_id": "m-1",
        "subject_type": "merchant_url",
        "status": "succeeded",
        "report_jsonb": _brand_report(),
        "partial_result_jsonb": {},
    }

    async def fake_fetch(*, run_id):
        return row

    orig = mar.fetch_audit_run_by_id
    mar.fetch_audit_run_by_id = fake_fetch
    try:
        out = asyncio.run(
            mar.get_merchant_url_audit("r1", merchant_id="m-1", summary_only=True)
        )
    finally:
        mar.fetch_audit_run_by_id = orig
    assert set(out.keys()) == {"status", "run_id", "audit_run_id", "report_summary"}
    assert out["report_summary"]["contract_version"] == CONTRACT_VERSION
    # The heavy keys must NOT ride along on the homepage-hero path.
    assert "per_sku_reports" not in out and "brand_report" not in out


def test_supporting_prompts_prefer_niche_over_head_terms():
    # Strategy guard (partner feedback): mid/long-tail brands win spec-matched
    # niche prompts — broad head terms must never be showcased as an action's
    # evidence while niche losses exist.
    failing = [
        {"query": "best headphones", "provider": "gemini"},
        {"query": "what headphones should I buy", "provider": "gemini"},
        {
            "query": "bone conduction headphones open-ear no water trapped daily sports",
            "provider": "gemini",
            "axis": "constraint",
        },
    ]
    report = _brand_report()
    report["per_sku_reports"] = [_sku_report(failing_prompts=failing)]
    out = build_report_summary(report)
    prompts = out["top_actions"][0]["supporting_prompts"]
    assert [p["query"] for p in prompts] == [
        "bone conduction headphones open-ear no water trapped daily sports"
    ]


def test_supporting_prompts_head_only_still_shows_honest_evidence():
    failing = [
        {"query": "best headphones", "provider": "gemini"},
        {"query": "top earbuds", "provider": "openai"},
    ]
    report = _brand_report()
    report["per_sku_reports"] = [_sku_report(failing_prompts=failing)]
    out = build_report_summary(report)
    prompts = out["top_actions"][0]["supporting_prompts"]
    # All-head loss is still the measured truth — shown rather than hidden.
    assert [p["query"] for p in prompts] == ["best headphones", "top earbuds"]


def test_spec_matched_prompt_source_exempts_short_queries():
    # A short llm_winnable prompt is spec-matched by construction — the
    # generator stamp, not query length, decides.
    failing = [
        {"query": "best headphones", "provider": "gemini"},
        {"query": "best swim mp3", "provider": "gemini", "prompt_source": "llm_winnable"},
    ]
    report = _brand_report()
    report["per_sku_reports"] = [_sku_report(failing_prompts=failing)]
    out = build_report_summary(report)
    prompts = out["top_actions"][0]["supporting_prompts"]
    assert [p["query"] for p in prompts] == ["best swim mp3"]
    assert prompts[0]["prompt_source"] == "llm_winnable"


def test_spec_matched_exemption_survives_the_chips_path():
    # Review P1 guard: the action's embedded chips are the PREFERRED evidence
    # input — the llm_winnable exemption must hold there too, not just on the
    # sku failing_prompts fallback.
    chips = [
        {"query": "best headphones", "provider": "gemini"},
        {
            "query": "best swim mp3",
            "provider": "gemini",
            "prompt_source": "llm_winnable",
        },
    ]
    report = _brand_report()
    report["per_sku_reports"] = [_sku_report(evidence_chips=chips)]
    out = build_report_summary(report)
    prompts = out["top_actions"][0]["supporting_prompts"]
    assert [p["query"] for p in prompts] == ["best swim mp3"]
    assert prompts[0]["prompt_source"] == "llm_winnable"


def test_chip_builder_preserves_prompt_source():
    from services.next_best_action import _sku_failing_prompt_chip

    chip = _sku_failing_prompt_chip(
        {"query": "best swim mp3", "prompt_source": "llm_winnable"}
    )
    assert chip["prompt_source"] == "llm_winnable"


def test_unmeasured_dimensions_excluded_from_weakest_link():
    # Calibration decision (a), live-Mojawa shape: citation 46 / identity 23 /
    # content 46 / routability 6 with no connected catalog. Routability is
    # unmeasurable, not zero — overall = min(46, 23, 46) = 23, not 6.
    report = _brand_report()
    report["per_sku_reports"][0]["scores"] = {
        "citation": {"score": 46},
        "identity": {"score": 23},
        "content_richness": {"score": 46},
        "routability": {"score": 6},
    }
    out = build_report_summary(
        report, unmeasured_dimensions=("routability",)
    )
    score = out["score"]
    assert score["raw"] == 23.0
    assert score["display"] == 2.3
    assert score["unmeasured_excluded"] == ["routability"]
    assert score["weakest_dimension"]["key"] == "identity"
    assert score["weakest_dimension"]["display"] == 2.3
    explainer = score["explainer"]
    assert "weakest" in explainer.lower()
    assert "product identity" in explainer
    assert "routability" in explainer
    assert "connected catalog" in explainer
    # Old-semantics run-over-run delta is dropped once exclusions changed the
    # number — never compare apples to oranges.
    assert score["delta"] is None
    # SKU row mirrors the same semantics.
    assert out["sku_summaries"][0]["score"]["raw"] == 23


def test_no_exclusions_keeps_historical_semantics():
    # Default () — the persisted run_scores number is NEVER overridden;
    # weakest/explainer are still derived so the popover works everywhere.
    out = build_report_summary(_brand_report())
    score = out["score"]
    assert score["raw"] == 42.0  # persisted, untouched
    assert score["unmeasured_excluded"] == []
    assert score["weakest_dimension"]["key"] == "routability"
    assert score["explainer"] and "Not counted" not in score["explainer"]
    assert score["delta"] is not None


def test_wedge_route_declares_routability_unmeasurable():
    from routes.merchant_audit_routes import _shape_url_audit_response

    report = _brand_report()
    report["per_sku_reports"][0]["scores"] = {
        "citation": {"score": 46},
        "routability": {"score": 6},
    }
    row = {
        "run_id": "r1",
        "report_jsonb": report,
        "partial_result_jsonb": {},
    }
    out = _shape_url_audit_response(row)
    assert out["report_summary"]["score"]["raw"] == 46.0
    assert out["report_summary"]["score"]["unmeasured_excluded"] == ["routability"]


def test_since_last_audit_passthrough_and_absence():
    # Absent on reports that predate the per-SKU attach (presence-gated).
    assert build_report_summary(_brand_report())["since_last_audit"] is None
    report = _brand_report()
    report["reaudit_delta"] = {
        "is_first_audit": False,
        "days_since_last": 7,
        "headline": "Visibility improved materially since your last audit.",
        "movements": [
            {"signal": "visibility", "label": "AI visibility", "from": 20,
             "to": 33, "is_material": True, "direction": "improved"},
            {"signal": "attribution", "label": "Attribution", "from": 46,
             "to": 47, "is_material": False, "direction": "stable"},
        ],
        "measurement_basis": {"same": True},
    }
    out = build_report_summary(report)["since_last_audit"]
    assert out["days_since_last"] == 7
    assert out["material_movements"] == 1
    assert out["basis_same"] is True
    assert out["movements"][0]["label"] == "AI visibility"  # verbatim


def test_action_impact_dimension_stamped():
    out = build_report_summary(_brand_report())
    action = out["top_actions"][0]
    # fixture primary_gap = get_indexed -> routability
    assert action["impact"] == {"dimension": "routability", "label": "routability"}


def test_unknown_gap_has_no_impact_never_guessed():
    report = _brand_report()
    report["merchant_narrative"]["prioritized_actions"][0]["primary_gap"] = "novel_gap"
    out = build_report_summary(report)
    assert out["top_actions"][0]["impact"] is None


def test_reaudit_delta_end_to_end_on_real_per_sku_shape():
    # Review P0 guard: feed GENUINE per-SKU brand reports (per_sku_reports,
    # no per_product/merchant_view) through build_reaudit_delta — movements
    # must carry real numbers, not None→None.
    from services.audit_delta import build_reaudit_delta

    prior = _brand_report()
    prior["per_sku_reports"][0]["scores"] = {
        "citation": {"score": 20},
        "identity": {"score": 30},
    }
    current = _brand_report()
    current["per_sku_reports"][0]["scores"] = {
        "citation": {"score": 46},
        "identity": {"score": 33},
    }
    delta = build_reaudit_delta(
        current_report=current,
        prior_report=prior,
        prior_row=None,
        days_since=7,
    )
    moves = {m["signal"]: m for m in delta["movements"] if "signal" in m}
    # visibility = weakest dim (20 -> 33), attribution = citation (20 -> 46)
    assert moves["visibility"]["from"] == 20 and moves["visibility"]["to"] == 33
    assert moves["attribution"]["from"] == 20 and moves["attribution"]["to"] == 46
    assert moves["attribution"]["is_material"] is True
    # And the contract counter reads the real key end-to-end.
    current["reaudit_delta"] = delta
    out = build_report_summary(current)["since_last_audit"]
    assert out["material_movements"] >= 1


def test_share_of_voice_prompt_level_counts():
    report = _brand_report()
    report["per_sku_reports"][0]["opportunity"] = {
        "per_prompt": [
            {
                "query": "best hydrating serum for dry skin",
                "source_summary": {"merchant_cited_runs": 0, "sku_cited_runs": 0},
                "competitors": ["CeraVe", "The Ordinary", "CeraVe"],  # dup in one answer
            },
            {
                "query": "fragrance-free serum sensitive skin",
                "source_summary": {"merchant_cited_runs": 1},
                "competitors": ["cerave"],  # case-folds into CeraVe
            },
            {
                "query": "hydra serum reviews",
                "source_summary": {"sku_cited_runs": 2},
                "competitors": [],
            },
            {"query": ""},  # empty query rows don't count toward the basis
        ]
    }
    sov = build_report_summary(report)["share_of_voice"]
    assert sov["available"] is True
    assert sov["prompts_probed"] == 3  # one shared denominator
    assert sov["brand"] == {"name": "GlowLab", "prompts_cited": 2, "pct": 66.7}
    top = sov["competitors"][0]
    # CeraVe: named on 2 prompts (dup within one answer counts once)
    assert top["name"] == "CeraVe" and top["prompts_named"] == 2 and top["pct"] == 66.7
    assert sov["competitors"][1]["prompts_named"] == 1


def test_share_of_voice_unavailable_without_prompts():
    sov = build_report_summary(_brand_report())["share_of_voice"]
    assert sov == {"available": False, "prompts_probed": 0}


def test_share_of_voice_dedups_prompts_across_skus():
    # Review P1: the same query probed for two SKUs is ONE prompt — the
    # denominator and competitor weights must not scale with SKU fan-out.
    report = _brand_report()
    shared = {
        "query": "best hydrating serum for dry skin",
        "source_summary": {"merchant_cited_runs": 0},
        "competitors": ["CeraVe"],
    }
    sku_a = _sku_report(sku_key="sku-1", sku_title="Serum A")
    sku_a["opportunity"] = {"per_prompt": [dict(shared)]}
    sku_b = _sku_report(sku_key="sku-2", sku_title="Serum B")
    sku_b["opportunity"] = {
        "per_prompt": [
            {**shared, "source_summary": {"sku_cited_runs": 1}},  # B won it
        ]
    }
    report["per_sku_reports"] = [sku_a, sku_b]
    sov = build_report_summary(report)["share_of_voice"]
    assert sov["prompts_probed"] == 1  # one distinct prompt, not two rows
    assert sov["brand"]["prompts_cited"] == 1  # any-SKU citation wins the prompt
    assert sov["competitors"][0]["prompts_named"] == 1  # not doubled by fan-out
