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


# --- multi-model judge (panel consensus) --------------------------------------

def _judge_yes_all(prompt):
    return '{"fit":true,"justify":true,"trust":true,"recommend":true,"notes":""}'

def _judge_strict(prompt):
    # only says yes when the card actually has substantiated claims + official source
    card = prompt.split("PRODUCT RECORD:\n", 1)[-1]
    j = "status: substantiated" in card
    t = "official brand source" in card or "first-party" in card
    f = "(none provided)" not in card.split("key active")[0]
    return ('{"fit":%s,"justify":%s,"trust":%s,"recommend":%s,"notes":""}'
            % (str(f).lower(), str(j).lower(), str(t).lower(), str(f and j and t).lower()))

def _judge_no_all(prompt):
    return '{"fit":false,"justify":false,"trust":false,"recommend":false,"notes":""}'


def test_consensus_is_per_axis_majority():
    # 2 of 3 judges say yes on every axis for the Pivota card -> consensus yes.
    judges = {"a": _judge_yes_all, "b": _judge_strict, "c": _judge_no_all}
    res = be.compare_behavioral_multi("brightening serum for dull skin", _pivota_record(), judges)
    # pivota: a=yes-all(4), strict=4 (has claims+official), c=0 -> majority yes all 4 axes
    assert res["pivota"]["score"] == 4
    assert res["pivota"]["consensus"]["justify"] is True
    # native: a=4, strict=0 (no claims/official), c=0 -> only 1/3 yes -> NO majority
    assert res["native"]["score"] == 0
    assert res["consensus_score_lift"] == 4
    assert set(res["judges"]) == {"a", "b", "c"}


def test_agreement_reports_unanimity():
    judges = {"a": _judge_yes_all, "b": _judge_yes_all}
    res = be.compare_behavioral_multi("q", _pivota_record(), judges)
    assert res["pivota"]["agreement"] == 1.0  # both judges identical -> unanimous
    judges2 = {"a": _judge_yes_all, "b": _judge_no_all}
    res2 = be.compare_behavioral_multi("q", _pivota_record(), judges2)
    assert res2["pivota"]["agreement"] == 0.5  # split on every axis


def test_per_judge_lift_breakdown():
    judges = {"yes": _judge_yes_all, "strict": _judge_strict}
    res = be.compare_behavioral_multi("q", _pivota_record(), judges)
    # yes-judge: pivota4 - native4 = 0 (it always says yes, no discrimination)
    assert res["per_judge_lift"]["yes"] == 0
    # strict judge: pivota4 - native0 = 4 (discriminates on real data)
    assert res["per_judge_lift"]["strict"] == 4


def test_multi_cohort_aggregation():
    judges = {"a": _judge_strict, "b": _judge_strict}
    items = [{"query": "q", "record": _pivota_record()} for _ in range(3)]
    agg = be.compare_behavioral_multi_cohort(items, judges)
    assert agg["n"] == 3
    assert agg["avg_pivota_consensus_score"] == 4.0
    assert agg["avg_native_consensus_score"] == 0.0
    assert agg["avg_consensus_lift"] == 4.0
    assert agg["avg_pivota_agreement"] == 1.0
