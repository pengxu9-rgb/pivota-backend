import hashlib
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from services.consumer_answer_evidence import SYSTEM
from services.recommendation_observation import (
    PILOT_QUESTIONS,
    RECOMMENDATION_BASIS,
    attach_recommendation_reviews,
    pilot_observations,
    validate_review,
)
from services.revenue_recovery_report import recovery_from_report
from services.selection_measurement import response_observations

PRODUCT = "prod::pilot::shopify::hydrating-essence"
CITATION = "https://example.com/products/hydrating-essence"
EXCERPT = "Try Example Hydrating Essence for a lightweight layer."


def _response(query, *, status="complete", model="fixture-model", probe_id="one"):
    return {
        "query": query, "axis_metadata": {"axis": "category"}, "_provider": "chatgpt",
        "_probe_run_id": probe_id, "evidence_kind": "consumer_answer", "prompt_contract": "consumer_query_v1",
        "grounding_sources": [{"uri": CITATION}],
        "answer": {
            "text": EXCERPT, "sha256": hashlib.sha256(EXCERPT.encode()).hexdigest(),
            "complete": status == "complete", "status": status, "provider": "chatgpt", "finish_reason": "completed",
            "model": model, "prompt_sha256": hashlib.sha256(json.dumps([SYSTEM, query], separators=(",", ":")).encode()).hexdigest(),
        },
        **({"raw": "__error__:provider_failure"} if status == "failed" else {}),
    }


def _report():
    first, second, third = sorted(PILOT_QUESTIONS)
    rows = response_observations([
        _response(first),
        _response(second, probe_id="two"),
        _response("Best hydrating essence in the UK?", probe_id="nonus"),
        _response(third, status="failed", probe_id="failed"),
        _response(first, model="new-model", probe_id="new-model"),
    ], sku_key=PRODUCT, merchant_brand="Example", merchant_host="example.com")
    return {
        "per_sku_reports": [{"product_key": PRODUCT, "sku_title": "Example Hydrating Essence"}],
        "brand_rollup": {"skus_audited": 1}, "consumer_selection_observations": rows,
    }


def _review(observation, *, verdict="recommended"):
    return {"payload_jsonb": {
        **validate_review(observation, answer_sha256=observation["answer_sha256"],
                          verdict=verdict, excerpt=EXCERPT, citation_url=CITATION),
        "basis": RECOMMENDATION_BASIS, "reviewed_by": "internal-staff-id",
        "observation_id": observation["observation_id"], "product_key": PRODUCT,
    }}


def test_recovery_counts_only_reviewed_exact_recommendations_and_keeps_models_apart():
    report = _report()
    recovery = recovery_from_report(report, run_id=str(uuid4()))
    selection = recovery["selection"]
    observations = list(pilot_observations(report).values())
    assert len(observations) == 4
    assert "recommendations" not in selection
    assert selection["answers"][0]["product_key"] == PRODUCT
    attach_recommendation_reviews(selection, report, [_review(observations[0])])
    rows = {row["model"]: row for row in selection["recommendations"]["rows"]}
    original = rows["fixture-model"]
    assert (original["attempted"], original["recommended"], original["unknown"], original["cited"]) == (2, 1, 1, 1)
    assert rows["Unavailable"]["failed"] == 1
    assert rows["new-model"]["unknown"] == 1
    assert "internal-staff-id" not in json.dumps(selection)
    assert selection["answers"][0]["recommendation_review"]["excerpt"] == EXCERPT


def test_citation_is_measured_separately_and_stale_reviews_are_ignored():
    report = _report()
    observation = next(iter(pilot_observations(report).values()))
    review = _review(observation, verdict="other")
    selection = {"answers": []}
    attach_recommendation_reviews(selection, report, [review])
    fixture = next(row for row in selection["recommendations"]["rows"] if row["model"] == "fixture-model")
    assert (fixture["recommended"], fixture["other"], fixture["cited"]) == (0, 1, 1)
    report["consumer_selection_observations"][0]["answer_evidence"]["text"] = "Changed."
    fresh_selection = {"answers": []}
    attach_recommendation_reviews(fresh_selection, report, [review])
    assert "recommendations" not in fresh_selection


@pytest.mark.parametrize("query", [
    "Is Example Hydrating Essence available in the US?",
    "Which CeraVe products ship to Canada from the US?",
    "What hydrating essences are available in Canada under 25 US dollars?",
    "Which essences are available in the UK, excluding the United States?",
])
def test_questions_outside_the_frozen_pilot_panel_are_not_counted(query):
    report = _report()
    report["per_sku_reports"][0]["identity"] = {"anchors": {"brand": "Example"}}
    report["consumer_selection_observations"] = response_observations(
        [_response(query)],
        sku_key=PRODUCT, merchant_brand="Independent Retailer", merchant_host="retailer.example",
    )
    assert not pilot_observations(report)


def test_private_answer_citation_cannot_become_a_pilot_review_source():
    report = _report()
    rows = report["consumer_selection_observations"]
    rows[0]["cited_sources"] = [{"uri": f"{CITATION}?access_token=private"}]
    observation = next(iter(pilot_observations(report).values()))
    assert observation["cited_urls"] == []
    with pytest.raises(ValueError, match="retained sources"):
        validate_review(observation, answer_sha256=observation["answer_sha256"],
                        verdict="recommended", excerpt=EXCERPT, citation_url=f"{CITATION}?access_token=private")


@pytest.mark.parametrize("override", [
    {"excerpt": "Invented fit"}, {"citation_url": "https://example.com/other"},
    {"answer_sha256": "old"}, {"citation_url": ""}, {"excerpt": "   "},
])
def test_positive_review_requires_matching_answer_excerpt_and_citation(override):
    observation = next(iter(pilot_observations(_report()).values()))
    values = {"answer_sha256": observation["answer_sha256"], "verdict": "recommended",
              "excerpt": EXCERPT, "citation_url": CITATION, **override}
    with pytest.raises(ValueError):
        validate_review(observation, **values)


@pytest.mark.asyncio
async def test_staff_reviews_are_bound_to_run_merchant_and_observation(monkeypatch):
    from db import audit_evidence
    from routes import agent_center_bd_routes as routes

    run_id = uuid4()
    report = _report()
    observation = next(iter(pilot_observations(report).values()))
    run = {"run_id": str(run_id), "merchant_id": "pilot", "stage": "completed", "report_jsonb": report}
    read_run = AsyncMock(return_value=run)
    monkeypatch.setattr(routes, "fetch_audit_run_by_id", read_run)
    monkeypatch.setattr(audit_evidence, "upsert_review_finding", AsyncMock(return_value="finding"))
    monkeypatch.setenv("PIVOTA_RECOMMENDATION_AUDIT_PILOT", "true")
    body = routes.ConsumerRecommendationReview(
        exact_product_and_fit_checked=True, answer_sha256=observation["answer_sha256"],
        verdict="recommended", excerpt=EXCERPT, citation_url=CITATION,
    )
    observation_id = observation["observation_id"]
    with pytest.raises(HTTPException) as exc:
        await routes.review_consumer_recommendation(run_id, observation_id, body, {"role": "outsourced", "user_id": "x"})
    assert exc.value.status_code == 403
    read_run.assert_not_awaited()
    staff = {"role": "employee", "employee_id": "reviewer"}
    with pytest.raises(HTTPException) as exc:
        await routes.review_consumer_recommendation(run_id, "from-another-audit", body, staff)
    assert exc.value.status_code == 404
    result = await routes.review_consumer_recommendation(run_id, observation_id, body, staff)
    assert result == {"finding_id": "finding", "verdict": "recommended"}
    saved = audit_evidence.upsert_review_finding.await_args.kwargs
    assert (saved["audit_run_id"], saved["merchant_id"], saved["product_key"]) == (
        str(run_id), "pilot", PRODUCT,
    )
