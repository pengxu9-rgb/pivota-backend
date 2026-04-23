from services.pdp_governance_service import (
    GPT55_REVIEW_MODEL,
    REVIEW_ACTOR_GPT55,
    make_pdp_id,
    module_requires_human_review,
    run_gpt55_quality_gate,
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
