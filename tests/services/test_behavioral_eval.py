"""Behavioral eval harness (model-in-the-loop) — pure logic, fake judge."""

import json

from services import behavioral_eval as be


def _pivota_record() -> dict:
    return {
        "category_kind": "skincare",
        "skincare_format": "serum",
        "concerns": ["dullness", "dryness"],
        "active_ingredients": [
            {"label": "Niacinamide", "source": "inci"},
            {"label": "Centella Asiatica", "source": "inci"},
        ],
        "evidence_claims": [
            {"claim_text": "Contains Niacinamide", "source_ref": "INCI",
             "substantiation_status": "substantiated"},
        ],
        "required_disclaimers": [],
        "best_us_offer": {"is_first_party": False, "official_source": True,
                          "source_system": "external_product_seeds_mirror_v1"},
    }


def _fake_judge(prompt: str) -> str:
    """A stand-in for the LLM: says yes on an axis only when the card actually
    carries the data (so Pivota scores high, native low) — exercising the harness
    the way a faithful grounded judge would."""
    card = prompt.split("PRODUCT RECORD:\n", 1)[-1]
    fit = "fit / concerns addressed:" in card and "(none provided)" not in card.split("key active")[0]
    justify = "status: substantiated" in card
    trust = "official brand source" in card or "first-party" in card
    recommend = fit and justify and trust
    return "```json\n" + json.dumps(
        {"fit": fit, "justify": justify, "trust": trust, "recommend": recommend, "notes": "ok"}
    ) + "\n```"


def test_build_context_card_includes_key_signals():
    card = be.build_context_card(_pivota_record())
    assert "Niacinamide (source: inci)" in card
    assert "status: substantiated" in card
    assert "official brand source" in card
    assert "dullness" in card


def test_parse_actionability_tolerates_fences_and_prose():
    v = be.parse_actionability('here is my answer:\n```json\n{"fit":true,"justify":false,"trust":true,"recommend":false,"notes":"x"}\n``` done')
    assert v["fit"] is True and v["justify"] is False and v["trust"] is True
    assert v["score"] == 2


def test_parse_actionability_handles_garbage():
    v = be.parse_actionability("model refused, no json")
    assert v["score"] == 0
    assert all(v[a] is False for a in ("fit", "justify", "trust", "recommend"))


def test_pivota_outscores_native_behaviorally():
    res = be.compare_behavioral("brightening serum for dull skin", _pivota_record(), _fake_judge)
    assert res["pivota"]["score"] == 4          # fit+justify+trust+recommend
    assert res["native"]["score"] == 0          # baseline stripped of all of it
    assert res["score_lift"] == 4
    assert res["pivota_recommends"] is True and res["native_recommends"] is False
    assert set(res["axes_won"]) == {"fit", "justify", "trust", "recommend"}


def test_native_baseline_loses_justify_and_trust_specifically():
    # Even if native somehow kept concerns, it loses substantiated claims + official source.
    res = be.compare_behavioral("q", _pivota_record(), _fake_judge)
    assert res["native"]["justify"] is False
    assert res["native"]["trust"] is False


def test_cohort_aggregation():
    items = [{"query": "q1", "record": _pivota_record()},
             {"query": "q2", "record": _pivota_record()}]
    agg = be.compare_behavioral_cohort(items, _fake_judge)
    assert agg["n"] == 2
    assert agg["avg_pivota_score"] == 4.0
    assert agg["avg_native_score"] == 0.0
    assert agg["avg_score_lift"] == 4.0
    assert agg["pivota_recommend_rate"] == 1.0
    assert agg["native_recommend_rate"] == 0.0


def test_empty_cohort():
    assert be.compare_behavioral_cohort([], _fake_judge)["n"] == 0
