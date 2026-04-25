from services.pdp_governance_service import (
    GPT55_REVIEW_MODEL,
    REVIEW_ACTOR_GPT55,
    build_codex_gpt55_quality_gate_result,
    make_pdp_id,
    module_requires_human_review,
    run_gpt55_quality_gate,
    _candidate_with_identity_review_state,
    _identity_candidate_action_set,
    _score_candidate,
)


def test_pdp_id_is_unified_and_stable_for_internal_product_group() -> None:
    first = make_pdp_id("product_group", "pg_123", "US")
    second = make_pdp_id("product_group", "pg_123", "us")

    assert first == second
    assert first.startswith("pdp_")


def test_pdp_id_distinguishes_external_only_projection() -> None:
    internal = make_pdp_id("product_group", "shared-123", "US")
    external = make_pdp_id("external_product", "shared-123", "US")

    assert internal != external


def test_offer_candidate_scoring_rejects_generic_only_overlap_even_same_brand() -> None:
    score, reasons = _score_candidate("PDRN Serum", "Hydrating Serum", "The Inkey List", "The Inkey List")

    assert score == 0.0
    assert "generic_only_overlap:serum" in reasons
    assert "brand_match" not in reasons


def test_offer_candidate_scoring_keeps_distinctive_overlap() -> None:
    score, reasons = _score_candidate("PDRN Serum", "Rose PDRN Soothing Serum")

    assert score >= 0.45
    assert "title_distinctive_overlap:pdrn" in reasons


def test_offer_candidate_scoring_rejects_product_form_mismatch() -> None:
    score, reasons = _score_candidate("PDRN Serum", "Camellia Deep Collagen Milky PDRN Toner 150ml")

    assert score == 0.0
    assert "product_form_mismatch:serum!=toner" in reasons


def test_offer_candidate_scoring_keeps_exact_title_match() -> None:
    score, reasons = _score_candidate("PDRN Serum", "PDRN Serum")

    assert score >= 0.95
    assert "exact_title_match" in reasons


def test_identity_review_candidate_actions_and_human_gate() -> None:
    assert module_requires_human_review("identity", {"identity_review": {"status": "pending"}})
    assert _identity_candidate_action_set({"candidate_type": "external_seed_near_match"}) == [
        "attach_external_offer",
        "reject_candidate",
    ]
    assert _identity_candidate_action_set({"candidate_type": "merchant_product_near_match"}) == [
        "merge_product_group",
        "reject_candidate",
    ]


def test_identity_candidate_state_filters_resolved_and_allows_employee_task_creation() -> None:
    candidate = {
        "id": "eps_1",
        "candidate_type": "external_seed_near_match",
        "title": "PDRN Serum",
    }

    pending = _candidate_with_identity_review_state(
        candidate,
        decisions={"eps_1": {"status": "pending", "task_id": "pdptask_1"}},
        actor_role="employee",
    )
    rejected = _candidate_with_identity_review_state(
        candidate,
        decisions={"eps_1": {"status": "rejected", "task_id": "pdptask_2"}},
        actor_role="employee",
    )

    assert pending is not None
    assert pending["identity_review"]["task_id"] == "pdptask_1"
    assert "create_identity_review_task" in pending["allowed_actions"]
    assert rejected is None


def test_gpt55_quality_gate_passes_source_grounded_low_risk_copy() -> None:
    result = run_gpt55_quality_gate(
        module_key="copy",
        payload={
            "title": "Aloe Vera Moisturizing Gel",
            "description": "Lightweight gel for daily skin hydration.",
        },
        source_refs=[{"type": "product_key", "id": "merchant|shopify|sku-1"}],
    )

    assert result["review_actor_type"] == REVIEW_ACTOR_GPT55
    assert result["review_model"] == GPT55_REVIEW_MODEL
    assert result["decision"] == "pass"


def test_gpt55_quality_gate_rejects_unsupported_and_regulated_claims() -> None:
    result = run_gpt55_quality_gate(
        module_key="copy",
        payload={
            "title": "Aloe Gel",
            "description": "Clinically proven to cure irritation with guaranteed results.",
        },
        source_refs=[{"type": "external_seed", "id": "seed-1"}],
    )

    assert result["decision"] == "reject"
    assert "medical_or_regulated_claim" in result["reasons"]
    assert "guarantee_claim" in result["reasons"]


def test_gpt55_quality_gate_requires_human_for_gallery_and_reviews() -> None:
    gallery = run_gpt55_quality_gate(
        module_key="gallery",
        payload={"images": [{"url": "https://example.com/photo.jpg", "rights_status": "third_party"}]},
        source_refs=[{"type": "external_seed", "id": "seed-1"}],
    )
    reviews = run_gpt55_quality_gate(
        module_key="reviews",
        payload={"featured_reviews": [{"quote": "Great"}]},
        source_refs=[{"type": "review_import", "id": "batch-1"}],
    )

    assert gallery["decision"] == "needs_human_review"
    assert reviews["decision"] == "needs_human_review"
    assert module_requires_human_review("gallery", {"rights_status": "third_party"})


def test_gpt55_quality_gate_rejects_missing_source_refs() -> None:
    result = run_gpt55_quality_gate(
        module_key="copy",
        payload={"title": "Plain Cotton Tee", "description": "Soft everyday shirt."},
        source_refs=[],
    )

    assert result["decision"] == "reject"
    assert "missing_source_refs" in result["reasons"]


def test_codex_gpt55_pass_with_failed_required_check_stays_human_review() -> None:
    result = build_codex_gpt55_quality_gate_result(
        module_key="copy",
        payload={"title": "Plain Cotton Tee", "description": "Soft everyday shirt."},
        source_refs=[{"type": "external_seed", "id": "seed-1"}],
        external_rubric={
            "decision": "pass",
            "confidence": 0.94,
            "reasons": ["reviewed in Codex window"],
            "checks": {
                "source_grounded": False,
                "seller_entity_checkout_not_confused": True,
                "variant_market_consistent": True,
                "no_medical_regulated_promo_or_fake_review_claim": True,
                "machine_publish_allowed_module": True,
            },
            "evidence_refs": ["external_seed:seed-1"],
            "reviewed_in": "codex_external_window",
        },
    )

    assert result["decision"] == "needs_human_review"
    assert "codex_pass_failed_checks:source_grounded" in result["reasons"]
    assert result["codex_gpt55_artifact"]["decision"] == "pass"
    assert result["codex_gpt55_artifact"]["publish_blockers"] == ["codex_pass_failed_checks:source_grounded"]


def test_codex_gpt55_pass_without_evidence_refs_stays_human_review() -> None:
    result = build_codex_gpt55_quality_gate_result(
        module_key="copy",
        payload={"title": "Plain Cotton Tee", "description": "Soft everyday shirt."},
        source_refs=[{"type": "external_seed", "id": "seed-1"}],
        external_rubric={
            "decision": "pass",
            "confidence": 0.94,
            "reasons": ["reviewed in Codex window"],
            "checks": {
                "source_grounded": True,
                "seller_entity_checkout_not_confused": True,
                "variant_market_consistent": True,
                "no_medical_regulated_promo_or_fake_review_claim": True,
                "machine_publish_allowed_module": True,
            },
            "evidence_refs": [],
            "reviewed_in": "codex_external_window",
        },
    )

    assert result["decision"] == "needs_human_review"
    assert "codex_pass_missing_evidence_refs" in result["reasons"]
