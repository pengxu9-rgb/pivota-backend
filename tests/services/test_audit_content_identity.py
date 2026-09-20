"""Regression cases reduced from the ANUKO and Judydoll production reports."""

from services.audit_content_repair import repair_report_content
from services.probe_identity_guard import guard_probe_identity
from services.agent_center_bd_report_service import _grounding_evidence
from services.sku_opportunity import _run_visibility_verdict


ANUKO = {"brand": "ANUKO", "title": "기프트 쇼핑백", "pdp_url": "https://anuko.co.kr/products/gift-bag"}
TARGET = {"uri": "https://www.target.com.au/p/kraft-gift-bag-anko/68760946", "title": "Kraft Gift Bag - Anko"}


def test_target_anko_cannot_count_as_anuko_sku_but_genuine_and_mixed_answers_remain():
    wrong = {"query": "where can I buy ANUKO 기프트 쇼핑백",
             "evidence_excerpt": "The 'Kraft Gift Bag - Anko' is available at Target Australia for $1.75.",
             "grounding_sources": [TARGET], "parsed": {"product_visible": True}}
    genuine = {"query": "where can I buy ANUKO 기프트 쇼핑백",
               "evidence_excerpt": "ANUKO 기프트 쇼핑백 is available on the official ANUKO shop.",
               "grounding_sources": [{"uri": ANUKO["pdp_url"], "title": "ANUKO gift bag"}],
               "parsed": {"product_visible": True}}
    mixed = {"query": "ANUKO 기프트 쇼핑백 reviews",
             "evidence_excerpt": "Target has Anko bags; ANUKO also lists its own gift shopping bag.",
             "grounding_sources": [TARGET, {"uri": ANUKO["pdp_url"]}],
             "parsed": {"product_visible": True}}
    original = [{"raw_runs": [wrong, genuine, mixed]}]
    runs = guard_probe_identity(original, ANUKO, {"product": ANUKO})[0]["raw_runs"]
    assert runs[0]["parsed"]["product_visible"] is False
    assert runs[0]["identity_mismatch"]
    verdict = _run_visibility_verdict(runs[0], {"product": ANUKO}, ANUKO)
    assert verdict["sku_identified"] is False
    assert verdict["grounded_positive"] is False
    evidence = _grounding_evidence(guard_probe_identity(original, ANUKO, {"product": ANUKO}))
    assert len(evidence) == 2
    assert all("Target Australia" not in (item.get("evidence_excerpt") or "") for item in evidence)
    assert runs[1]["parsed"]["product_visible"] is True
    assert runs[2]["parsed"]["product_visible"] is True
    assert original[0]["raw_runs"][0]["parsed"]["product_visible"] is True


def test_legacy_report_repairs_false_evidence_and_diagnostic_action_without_mutation():
    report = {
        "merchant_name": "ANUKO",
        "consumer_selection_observations": [{
            "product_key": "sku-1", "tier": "dupe", "status": "answered",
            "evidence_kind": "consumer_answer",
            "answer_evidence": {"text": "Judydoll is often compared with ISEHAN and Benefit.", "complete": True},
        }],
        "per_sku_reports": [{
            "sku_key": "sku-1",
            "sku_title": "기프트 쇼핑백",
            "verbatim_grounding_evidence": [{
                "query": "where can I buy ANUKO 기프트 쇼핑백", "product_visible": True,
                "evidence_excerpt": "The Kraft Gift Bag - Anko is at Target Australia.",
                "grounding_sources": [TARGET],
            }],
            "next_best_action": {
                "primary_gap": "substitution_leak", "headline": "When buyers ask for alternatives, AI names Elleevate, not this product.",
                "strategic_brief": {"first_moves": ["Fix your Target listing for ANUKO"]},
                "evidence_used": {"substitution_alert": {"prompt": "mascara for natural lash lift", "substituted_by": "Elleevate"}},
            },
        }],
        "merchant_narrative": {"prioritized_actions": [{"sku_title": "기프트 쇼핑백", "primary_gap": "substitution_leak", "headline": "old"}]},
    }
    fixed = repair_report_content(report)
    sku = fixed["per_sku_reports"][0]
    assert sku["verbatim_grounding_evidence"] == []
    assert sku["identity_rejected_evidence_count"] == 1
    assert sku["historical_identity_review_required"] is True
    assert "strategic_brief" not in sku["next_best_action"]
    assert "diagnostic question" in sku["next_best_action"]["why_this_first"]
    assert "Elleevate was not named" in sku["next_best_action"]["why_this_first"]
    assert sku["next_best_action"]["consumer_crosscheck"]["complete_answers"] == 1
    assert "Publish a comparison" not in str(fixed["merchant_narrative"]["prioritized_actions"])
    assert "Elleevate" in fixed["merchant_narrative"]["prioritized_actions"][0]["headline"]
    assert report["per_sku_reports"][0]["verbatim_grounding_evidence"][0]["product_visible"] is True
