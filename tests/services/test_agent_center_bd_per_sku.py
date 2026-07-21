from __future__ import annotations

from typing import Any, Dict, List

import pytest


def _base_sku_ctx() -> Dict[str, Any]:
    return {
        "sku_key": "sku-1",
        "merchant_id": "m-1",
        "product_key": "prod-1",
        "content_key": "ck_abc",
        "product": {
            "product_key": "prod-1",
            "merchant_id": "m-1",
            "platform": "shopify",
            "source_product_id": "p1",
            "content_key": "ck_abc",
            "pivota_signature_id": "sig_123",
            "pivota_canonical_url": "https://agent.pivota.cc/products/sig_123",
            "canonical_url": "https://merchant.test/products/serum",
            "title": "Bright Skin Serum 30ml",
            "brand": "TestBrand",
            "product_type": "Beauty Serum",
            "category": "beauty",
            "description": "A dermatologist-tested brightening serum with niacinamide and a lightweight daily-use texture. " * 3,
            "image_url": "https://img.test/serum.jpg",
            "freshness_json": {"current": True},
            "readiness_tier": "vertical_ready",
            "truth_tier": "primary",
            "sync_status": "live",
            "product_payload": {
                "ingredients": ["niacinamide"],
                "watchouts": ["patch test"],
                "substantiation": {"study": "merchant supplied"},
            },
        },
        "sku": {
            "sku_key": "sku-1",
            "product_key": "prod-1",
            "title": "Bright Skin Serum 30ml",
            "sku": "SERUM-30",
            "barcode": "1234567890123",
            "visible_option_labels": ["30ml"],
            "visible_attributes": {"size": "30ml"},
            "ingredient_ids": ["niacinamide"],
        },
        "all_skus": [
            {"sku_key": "sku-1", "sku": "SERUM-30", "visible_option_labels": ["30ml"]},
            {"sku_key": "sku-2", "barcode": "999", "visible_option_labels": ["50ml"]},
        ],
        "product_group_members": [{"product_group_id": "pg-1"}],
        "index_pipeline_state": {
            "identity_resolved": True,
            "serving_eligible": True,
            "pipeline_stage": "public_indexed",
            "product_group_id": "pg-1",
        },
        "content_key_peers": [
            {"product_key": "prod-1", "brand": "TestBrand", "title": "Bright Skin Serum 30ml", "product_group_id": "pg-1"},
            {"product_key": "prod-1b", "brand": "TestBrand", "title": "Bright Skin Serum 30ml", "product_group_id": "pg-1"},
        ],
        "product_quality_snapshot": {
            "content_quality_score": 100,
            "model_readiness_score": 100,
        },
        "product_enrichment": {
            "summary_short": "Brightening daily serum for dull skin.",
            "bullet_points": ["Brightens", "Lightweight", "Daily use"],
            "usage_scenarios": ["morning routine"],
            "audience_tags": ["sensitive skin shopper"],
            "topic_tags": ["dullness"],
            "llm_safety_flags": [],
        },
        "beauty_sku_ingredients": [{"raw_inci": "Niacinamide"}],
        "beauty_usage_guides": [{"how_to_use_text": "Apply daily"}],
        "beauty_compatibility_rules": [{"verdict": "compatible"}],
        "catalog_field_facts": [{"review_state": "reviewed", "field_key": "benefit"}],
        "offers": [
            {
                "offer_id": "offer-1",
                "sku_key": "sku-1",
                "product_key": "prod-1",
                "merchant_id": "m-1",
                "truth_tier": "primary",
                "readiness_tier": "commerce_ready",
                "offer_mode": "merchant_checkout",
                "availability": "in_stock",
                "inventory_quantity": 10,
                "currency": "USD",
                "list_price": 39.0,
                "merchant_effective_price": 35.0,
                "estimated_best_price": 35.0,
                "price_confidence": 0.95,
                "offer_payload": {"ship_to_market": "US", "shipping": {"domestic": True}},
            }
        ],
        "merchant_commerce_readiness_state": {
            "primary_platform": "shopify",
            "active_psp": "stripe",
            "execute_status": "ready",
            "execute_blockers": [],
        },
        "merchant": {"merchant_id": "m-1", "country": "US", "verification_status": "verified"},
        "pcs_shop_policies": [
            {"policy_type": "shipping", "url": "https://merchant.test/shipping"},
            {"policy_type": "refund", "url": "https://merchant.test/refund"},
            {"policy_type": "terms", "url": "https://merchant.test/terms"},
        ],
    }


def _probe_runs() -> List[Dict[str, Any]]:
    return [
        {
            "provider": "gemini",
            "probe_run_id": "probe-1",
            "raw_runs": [
                {
                    "query": "where can I buy Bright Skin Serum",
                    "parsed": {
                        "product_visible": True,
                        "sku_mentioned": True,
                        "correct_sku": True,
                        "authority_near_variant_found": True,
                    },
                    "evidence_excerpt": "TestBrand Bright Skin Serum is available from the brand.",
                    "grounding_sources": [
                        {"uri": "https://merchant.test/products/serum", "title": "Bright Skin Serum"},
                        {"uri": "https://forbes.com/best-serums", "title": "Best serums"},
                    ],
                    "grounding_chunks": ["https://merchant.test/products/serum"],
                    "url_match": {"in_grounding": True},
                    "axis_metadata": {"axis": "intent", "source": "auto_generated", "sku_key": "sku-1"},
                },
                {
                    "query": "best serum for dullness",
                    "parsed": {
                        "product_visible": False,
                        "competitors_listed": ["GlowCo"],
                    },
                    "evidence_excerpt": "GlowCo was cited.",
                    "grounding_sources": [
                        {"uri": "https://reddit.com/r/SkincareAddiction/comments/abc/thread", "title": "Best serum thread"}
                    ],
                    "grounding_chunks": ["https://reddit.com/r/SkincareAddiction/comments/abc/thread"],
                    "url_match": {"in_grounding": False},
                    "axis_metadata": {"axis": "concern", "source": "auto_generated", "sku_key": "sku-1"},
                },
            ],
        }
    ]


def _multi_provider_probe_runs() -> List[Dict[str, Any]]:
    runs = _probe_runs()
    runs.append(
        {
            "provider": "chatgpt",
            "model": "chat-latest",
            "model_is_override": False,
            "probe_run_id": "probe-2",
            "raw_runs": [
                {
                    "query": "where can I buy Bright Skin Serum",
                    "parsed": {"product_visible": False},
                    "grounding_sources": [],
                    "grounding_chunks": [],
                    "url_match": {"in_grounding": False},
                    "axis_metadata": {"axis": "intent", "source": "auto_generated", "sku_key": "sku-1"},
                },
                {
                    "query": "best serum for dullness",
                    "parsed": {
                        "product_visible": True,
                        "sku_mentioned": True,
                        "correct_sku": True,
                        "authority_near_variant_found": True,
                    },
                    "evidence_excerpt": "TestBrand Bright Skin Serum is cited by ChatGPT.",
                    "grounding_sources": [
                        {"uri": "https://merchant.test/products/serum", "title": "Bright Skin Serum"},
                        {"uri": "https://allure.com/best-serums", "title": "Best serums"},
                    ],
                    "grounding_chunks": ["https://merchant.test/products/serum"],
                    "url_match": {"in_grounding": True},
                    "axis_metadata": {"axis": "concern", "source": "auto_generated", "sku_key": "sku-1"},
                },
            ],
        }
    )
    return runs


def _positive_probe_runs(count: int = 4) -> List[Dict[str, Any]]:
    raw_runs: List[Dict[str, Any]] = []
    for idx in range(count):
        raw_runs.append({
            "query": f"where can I buy Bright Skin Serum query {idx}",
            "parsed": {
                "product_visible": True,
                "sku_mentioned": True,
                "correct_sku": True,
                "authority_near_variant_found": True,
            },
            "raw": "TestBrand Bright Skin Serum is recommended for dull skin.",
            "evidence_excerpt": "TestBrand Bright Skin Serum is available from the brand.",
            "grounding_sources": [
                {
                    "uri": "https://merchant.test/products/serum",
                    "title": "Bright Skin Serum",
                },
                {
                    "uri": "https://forbes.com/best-serums",
                    "title": "Best serums",
                }
            ],
            "grounding_chunks": ["https://merchant.test/products/serum"],
            "url_match": {"in_grounding": True},
            "axis_metadata": {
                "axis": "intent",
                "source": "auto_generated",
                "sku_key": "sku-1",
            },
        })
    return [{
        "provider": "gemini",
        "probe_run_id": "probe-positive",
        "raw_runs": raw_runs,
    }]


def test_us_shopper_resolves_deepseek_verify_provider():
    from services.coverage_profiles import (
        load_coverage_profile_config,
        resolve_coverage_profile,
    )

    load_coverage_profile_config.cache_clear()
    coverage = resolve_coverage_profile(coverage_profile="us_shopper")
    assert coverage["providers"] == ["gemini", "chatgpt"]
    assert coverage["verify_providers"] == ["deepseek"]
    assert coverage["pending_engine_support"] == []


def test_wedge_profiles_resolve_brand_and_hero_primary_providers():
    from services.coverage_profiles import (
        load_coverage_profile_config,
        resolve_coverage_profile,
    )

    load_coverage_profile_config.cache_clear()
    brand = resolve_coverage_profile(coverage_profile="pilot_gemini")
    hero = resolve_coverage_profile(coverage_profile="us_shopper")

    assert brand["providers"] == ["gemini"]
    assert brand["verify_providers"] == ["deepseek"]
    assert hero["providers"] == ["gemini", "chatgpt"]
    assert hero["verify_providers"] == ["deepseek"]


def test_gemini_deepseek_resolves_dual_primary_providers():
    from services.coverage_profiles import (
        load_coverage_profile_config,
        resolve_coverage_profile,
    )

    load_coverage_profile_config.cache_clear()
    coverage = resolve_coverage_profile(coverage_profile="gemini_deepseek")
    assert coverage["providers"] == ["gemini", "deepseek"]
    assert coverage["verify_providers"] == []
    assert coverage["pending_engine_support"] == []
    assert coverage["provider_default_models"]["deepseek"] == "deepseek-chat"


def test_identity_score_good_and_missing_data():
    from services.agent_center_bd_report_service import compute_identity_score

    score, breakdown = compute_identity_score(_base_sku_ctx())
    assert score == 100
    assert breakdown["variant_identity"]["points"] == 15

    missing = _base_sku_ctx()
    missing["product"] = {"title": "Too short"}
    missing["sku"] = {"sku_key": "sku-1"}
    missing["all_skus"] = [{"sku_key": "sku-1"}]
    missing.pop("content_key", None)
    missing.pop("content_key_peers", None)
    score, breakdown = compute_identity_score(missing)
    assert 0 <= score < 100
    assert breakdown["content_key"]["reason"] == "data unavailable"
    assert "catalog_products.content_key" in breakdown["missing_inputs"]


def test_content_richness_score_good_partial_missing():
    from services.agent_center_bd_report_service import compute_content_richness_score

    score, breakdown = compute_content_richness_score(_base_sku_ctx())
    assert score == 100
    assert breakdown["vertical_structure"]["points"] == 20

    partial = _base_sku_ctx()
    partial["product_quality_snapshot"] = {}
    partial["product_enrichment"] = {"bullet_points": ["one"], "llm_safety_flags": ["blocking_claim"]}
    partial["beauty_sku_ingredients"] = []
    partial["beauty_usage_guides"] = []
    partial["beauty_compatibility_rules"] = []
    partial["product"]["description"] = "short"
    partial["product"]["image_url"] = None
    partial["product"]["freshness_json"] = {}
    score, breakdown = compute_content_richness_score(partial)
    # Missing Pivota enrichment still drags the score down, but the bucket now
    # reflects whatever RAW content survives (title, attributes, priced offer)
    # instead of a flat 0 — and still flags the Pivota artifact as the real gap.
    assert 0 <= score < 60
    assert "product_quality_snapshot.content_quality_score" in breakdown["missing_inputs"]
    assert breakdown["safety_claims"]["points"] == 0


def test_has_substantiation_via_general_evidence_flag():
    # Phase 2b: the plumbed has_substantiated_evidence flag (general
    # product_evidence store) substantiates on its own — even with an otherwise
    # empty product/ctx — exactly like the existing payload/profile signals.
    from services.agent_center_bd_report_service import _has_substantiation

    assert _has_substantiation({}, {"has_substantiated_evidence": True}) is True
    assert _has_substantiation({}, {}) is False
    assert _has_substantiation({}, {"has_substantiated_evidence": False}) is False
    # Existing signals still substantiate independently (no regression).
    assert _has_substantiation({"product_payload": {"substantiation": {"study": "x"}}}, {}) is True


def test_general_evidence_lifts_safety_claims_bucket():
    # The product behavior the wiring buys: a merchant making claims it can't yet
    # back scores 5/10 on "Substantiated claims"; confirming substantiated evidence
    # lifts that bucket to the full 10 (+5) — reusing the existing weight.
    from services.agent_center_bd_report_service import compute_content_richness_score

    ctx = _base_sku_ctx()
    # Claims PRESENT (markers in copy) but UNSUBSTANTIATED (strip every signal).
    ctx["product"]["description"] = "Clinically tested anti-aging serum for acne-prone skin. " * 4
    ctx["product"]["product_payload"] = {"ingredients": ["niacinamide"]}  # no substantiation/watchouts
    ctx["beauty_product_profile"] = {}
    ctx["product_enrichment"] = {"bullet_points": ["a", "b", "c"]}  # no blocking llm_safety_flags

    _, breakdown = compute_content_richness_score(ctx)
    assert breakdown["safety_claims"]["points"] == 5  # docked: claims w/o substantiation

    ctx["has_substantiated_evidence"] = True
    _, breakdown2 = compute_content_richness_score(ctx)
    assert breakdown2["safety_claims"]["points"] == 10  # full once substantiated


def test_evidence_signal_is_non_scoring():
    # The signal annotates the safety_claims bucket WITHOUT changing its points. To
    # prove that meaningfully (not just "10 capped at 10"), use a ctx whose
    # safety_claims is BELOW max — claims present + UNsubstantiated → 5/10 — and add
    # ONLY the signal fields (not has_substantiated_evidence). The bucket must STAY 5
    # while the annotation appears.
    from services.agent_center_bd_report_service import compute_content_richness_score

    def _unsubstantiated_ctx():
        c = _base_sku_ctx()
        c["product"]["description"] = "Clinically tested anti-aging serum for acne-prone skin. " * 4
        c["product"]["product_payload"] = {"ingredients": ["niacinamide"]}
        c["beauty_product_profile"] = {}
        c["product_enrichment"] = {"bullet_points": ["a", "b", "c"]}
        return c

    score_before, bd_before = compute_content_richness_score(_unsubstantiated_ctx())
    assert bd_before["safety_claims"]["points"] == 5
    assert "evidence_signal" not in bd_before["safety_claims"]

    ctx = _unsubstantiated_ctx()
    ctx["substantiated_evidence_count"] = 3
    ctx["third_party_evidence_sources"] = 2
    score_after, bd_after = compute_content_richness_score(ctx)

    # points + total score identical — the signal is purely informational
    assert bd_after["safety_claims"]["points"] == 5
    assert score_after == score_before
    # structured signal + reason annotation present
    assert bd_after["safety_claims"]["evidence_signal"] == {"substantiated_claims": 3, "third_party_sources": 2}
    assert "backed by 2 third-party sources" in bd_after["safety_claims"]["reason"]


def test_evidence_signal_singular_phrasing_and_count_only():
    from services.agent_center_bd_report_service import compute_content_richness_score

    ctx = _base_sku_ctx()
    ctx["substantiated_evidence_count"] = 1
    ctx["third_party_evidence_sources"] = 1
    _, bd = compute_content_richness_score(ctx)
    assert "backed by 1 third-party source" in bd["safety_claims"]["reason"]

    # substantiated claims but none third-party (e.g. merchant lab only): signal
    # present, no third-party phrasing appended.
    ctx2 = _base_sku_ctx()
    ctx2["substantiated_evidence_count"] = 2
    ctx2["third_party_evidence_sources"] = 0
    _, bd2 = compute_content_richness_score(ctx2)
    assert bd2["safety_claims"]["evidence_signal"] == {"substantiated_claims": 2, "third_party_sources": 0}
    assert "third-party" not in bd2["safety_claims"]["reason"]


def test_content_richness_scores_raw_pdp_when_pivota_enrichment_absent():
    """Audit regression: a content-rich brand PDP (long description, image,
    specs, priced offer) with NO Pivota enrichment must NOT be scored as thin.
    Previously product_quality_score (25) + model_readiness (15) came only from
    product_quality_snapshot, so fresh ingests scored ~18/100 and got a
    "build a PDP you already have" recommendation."""
    from services.agent_center_bd_report_service import compute_content_richness_score

    ctx = _base_sku_ctx()
    # Strip every Pivota enrichment artifact — simulate a fresh ingest.
    ctx["product_quality_snapshot"] = {}
    ctx["product_enrichment"] = {}
    ctx["product"]["description"] = (
        "A dermatologist-tested brightening serum with niacinamide, a lightweight "
        "daily-use texture, and a clinically informed formulation. "
    ) * 6  # ~600+ chars of REAL merchant content

    score, breakdown = compute_content_richness_score(ctx)

    pq = breakdown["product_quality_score"]
    mr = breakdown["model_readiness"]
    # Raw content now earns real points instead of 0.
    assert pq["points"] > 0, pq
    assert mr["points"] > 0, mr
    # But the recommendation target stays honest: the gap is Pivota enrichment,
    # not "go write a description" — the merchant already has one.
    assert "product_quality_snapshot.content_quality_score" in breakdown["missing_inputs"]
    assert "product_quality_snapshot.model_readiness_score" in breakdown["missing_inputs"]
    # A content-rich-but-unenriched PDP clears the old ~18 floor decisively.
    assert score > 40, (score, breakdown)


def test_routability_score_good_and_missing_offer_data():
    from services.agent_center_bd_report_service import compute_routability_score

    score, breakdown = compute_routability_score(_base_sku_ctx())
    assert score == 100
    assert breakdown["offer_orderability"]["points"] == 25

    missing = _base_sku_ctx()
    missing["offers"] = []
    missing["index_pipeline_state"] = {"pipeline_stage": "extracted"}
    score, breakdown = compute_routability_score(missing)
    assert score < 70
    assert breakdown["serving_eligibility"]["points"] == 10
    assert breakdown["offer_orderability"]["reason"] == "data unavailable"


def test_routability_score_unknown_availability_is_not_orderable():
    from services.agent_center_bd_report_service import compute_routability_score

    ctx = _base_sku_ctx()
    ctx["offers"][0]["availability"] = "unknown"
    ctx["offers"][0]["inventory_quantity"] = None

    score, breakdown = compute_routability_score(ctx)

    assert score < 100
    assert breakdown["offer_orderability"]["points"] == 17
    assert breakdown["offer_orderability"]["reason"] == "partial offer orderability"
    assert "catalog_offers.availability" in breakdown["missing_inputs"]


def test_deliverability_prediction_requires_explicit_execute_ready():
    from services.agent_center_bd_report_service import build_sku_deliverability_prediction

    ctx = _base_sku_ctx()
    ctx["merchant_commerce_readiness_state"] = {
        "primary_platform": "shopify",
        "active_psp": "stripe",
    }

    prediction = build_sku_deliverability_prediction(ctx)

    assert prediction["status"] == "servable_not_transactable"
    assert prediction["checkout"]["status"] == "blocked"
    assert "merchant_commerce_readiness_state.execute_status" in prediction["checkout"]["missing_inputs"]
    assert "active PSP" not in prediction["summary"]


def test_deliverability_prediction_requires_explicit_available_stock():
    from services.agent_center_bd_report_service import build_sku_deliverability_prediction

    ctx = _base_sku_ctx()
    ctx["offers"][0]["availability"] = "unknown"
    ctx["offers"][0]["inventory_quantity"] = None

    prediction = build_sku_deliverability_prediction(ctx)

    assert prediction["status"] == "servable_not_transactable"
    assert prediction["checkout"]["status"] == "blocked"
    assert prediction["checkout"]["orderable_offer"] is False
    assert prediction["checkout"]["offer"]["points"] == 17
    assert "catalog_offers.availability" in prediction["checkout"]["missing_inputs"]


def test_deliverability_prediction_blocks_unservable_sku_before_checkout():
    from services.agent_center_bd_report_service import build_sku_deliverability_prediction

    ctx = _base_sku_ctx()
    ctx["index_pipeline_state"] = {
        "serving_eligible": False,
        "pipeline_stage": "quality_gated",
    }

    prediction = build_sku_deliverability_prediction(ctx)

    assert prediction["status"] == "not_publishable"
    assert prediction["serving"]["status"] == "blocked"
    assert prediction["checkout"]["status"] == "ready"
    assert prediction["checkout"]["orderable_offer"] is True


def test_deliverability_prediction_softens_unmeasured_serving_summary():
    from services.agent_center_bd_report_service import build_sku_deliverability_prediction

    ctx = _base_sku_ctx()
    ctx["index_pipeline_state"] = {}

    prediction = build_sku_deliverability_prediction(ctx)

    assert prediction["status"] == "not_publishable"
    assert prediction["serving"]["status"] == "unknown"
    assert prediction["checkout"]["status"] == "ready"
    assert "not confirmed" in prediction["summary"]
    assert "not serving eligible" not in prediction["summary"]


def test_deliverability_prediction_does_not_overclaim_non_direct_platform():
    from services.agent_center_bd_report_service import build_sku_deliverability_prediction

    ctx = _base_sku_ctx()
    ctx["product"]["platform"] = "woocommerce"
    ctx["merchant_commerce_readiness_state"] = {
        "primary_platform": "woocommerce",
        "active_psp": "stripe",
        "execute_status": "ready",
        "execute_blockers": [],
    }

    prediction = build_sku_deliverability_prediction(ctx)

    assert prediction["status"] == "servable_not_direct_purchase"
    assert prediction["checkout"]["status"] == "limited"
    assert prediction["checkout"]["allows_pivota_order"] is False
    assert prediction["checkout"]["commerce_path"] == "unsupported"


def test_deliverability_prediction_calls_ready_shopify_sku_transactable():
    from services.agent_center_bd_report_service import build_sku_deliverability_prediction

    prediction = build_sku_deliverability_prediction(_base_sku_ctx())

    assert prediction["status"] == "transactable"
    assert prediction["serving"]["status"] == "ready"
    assert prediction["checkout"]["status"] == "ready"
    assert prediction["checkout"]["allows_pivota_order"] is True


def test_citation_score_weighted_formula_and_missing_runs():
    from services.agent_center_bd_report_service import compute_citation_score

    score, breakdown = compute_citation_score(_base_sku_ctx(), _probe_runs())
    # No verify_outputs were passed → answer_quality is unscored (unchecked
    # claims earn 0), so the citation total is 5 lower than the old behavior,
    # which credited deterministic answer_quality without verification.
    assert score == 45
    assert breakdown["first_party_rate"]["numerator"] == 1
    assert breakdown["first_party_rate"]["denominator"] == 2
    assert breakdown["answer_quality_rate"]["points"] == 0
    assert breakdown["answer_quality_rate"]["denominator"] == 0
    assert breakdown["answer_quality_rate"]["deterministic_numerator"] == 1

    # No probes ran → score is None (no signal), NOT a measured 0. A 0 here
    # falsely reads as "measured, brand never cited" → false INVISIBLE verdict.
    score, breakdown = compute_citation_score(_base_sku_ctx(), [])
    assert score is None
    assert breakdown["total"] is None
    assert breakdown["no_probes"] is True
    assert breakdown["first_party_rate"]["reason"] == "no probes ran for this SKU"


def test_per_sku_query_records_never_empty_for_any_prompts_per_sku():
    """Audit P2: the 'low prompts_per_sku -> 0 probes' anomaly is NOT in query
    building — prove >=1 query (and thus >=1 chunk) for prompts_per_sku>=1 across
    a rich product, a thin fresh-ingest, and a bare-variant-label SKU. A zero
    probe count can therefore only come from an empty provider set / producer not
    running, which _probe_per_sku_ctx now logs loudly."""
    from services.agent_center_bd_report_service import (
        _build_per_sku_audit_query_records,
        _chunk_query_specs,
    )

    contexts = {
        "rich": {"sku_key": "s1", "product": {"title": "Triple Shine Grape", "brand": "Ownist",
                 "product_type": "collagen", "attributes_raw": {"flavor": "grape", "format": "powder"}}},
        "thin": {"sku_key": "s2", "product": {"title": "White Collagen", "product_type": "supplement"}},
        "bare": {"sku_key": "s3", "sku": {"title": "14 Servings, 2-Week Routine"}, "product": {}},
    }
    for name, ctx in contexts.items():
        for n in (1, 2, 3, 5, 40):
            records = _build_per_sku_audit_query_records(ctx, n)
            assert len(records) >= 1, (name, n, records)
            assert len(records) <= n, (name, n, len(records))
            specs = [(r["query"], r["axis"]) for r in records]
            assert len(_chunk_query_specs(specs)) >= 1, (name, n)


def _assert_query_well_formed(query: str, ctx_name: str) -> None:
    """A generated probe query must be a clean shopper prompt, not a fragment."""
    assert isinstance(query, str), (ctx_name, query)
    stripped = query.strip()
    assert len(stripped) >= 4, ("too short", ctx_name, repr(query))
    # No interpolation/serialization debris leaked into the template.
    for junk in ("[", "]", "{", "}", "<", ">"):
        assert junk not in stripped, ("bracket leak", ctx_name, repr(query))
    assert stripped.count("(") == stripped.count(")"), ("unbalanced paren", ctx_name, repr(query))
    assert stripped.count('"') % 2 == 0, ("unbalanced quote", ctx_name, repr(query))
    # No empty-attribute template left a dangling connective ("best toner for").
    tokens = stripped.lower().split()
    last_word = tokens[-1] if tokens else ""
    assert last_word not in {
        "for", "with", "and", "or", "the", "a", "an", "of", "to", "in", "on", "by", "from",
    }, ("dangling connective", ctx_name, repr(query))
    # The "f toner" orphan-token shape can't be caught by scanning output tokens
    # (legit queries contain "I", and "vitamin c" contains "c"). It's prevented
    # at the source instead — asserted directly via _clean_prompt_term below.


def test_per_sku_queries_are_well_formed_even_with_garbled_attributes():
    """Regression: malformed enrichment/attributes (lone bracket, stray quote,
    single-letter junk, empty category/material/variant) must not leak fragment
    queries like "best toner for [", 'best ... set for "', or "f toner" into the
    probe set. The fixture deliberately stuffs every interpolation source with
    junk so a single missed branch surfaces as a malformed query."""
    from services.agent_center_bd_report_service import (
        _build_per_sku_audit_query_records,
        _is_well_formed_query,
        _clean_prompt_term,
    )

    # Direct unit checks of the gate against the exact shapes seen in prod.
    assert not _is_well_formed_query("best toner for [")
    assert not _is_well_formed_query('best women\'s lingerie set for "')
    assert not _is_well_formed_query("best toner for")
    assert _is_well_formed_query("best rated women's lingerie set")
    assert _is_well_formed_query("does Winona Soothing Repair Serum actually work")
    assert _is_well_formed_query("buy TestBrand Serum (30ml) online")  # balanced parens ok

    # Source cleaner drops debris/orphan tokens before they can interpolate.
    for junk in ("[", "]", '"', "'", "f", "{", "}", "(", "  ", "", "<unset"):
        assert _clean_prompt_term(junk) in ("", "unset"), repr(junk)
    assert _clean_prompt_term("sensitive skin") == "sensitive skin"

    garbled_ctx = {
        "sku_key": "sku-garbled",
        "merchant_id": "m-1",
        "product": {
            "title": "Mystery Toner",
            "product_type": "toner",
            # Empty/None attributes are exactly what triggered the leaks.
            "category": "",
            "attributes_raw": {
                "material": "",
                "variant": None,
                "tags": ["[", '"', "f", "{placeholder}", "", "women"],
            },
        },
        "product_enrichment": {
            # Junk tokens that used to interpolate straight into templates.
            "topic_tags": ["[", '"', "f", "", "  ", "]"],
            "audience_tags": ['"', "(", "f"],
            "usage_scenarios": ["<unset>", "f"],
            "bullet_points": ["f", "[", '"', "g"],
        },
    }

    for n in (1, 3, 8, 16, 40):
        records = _build_per_sku_audit_query_records(garbled_ctx, n)
        assert records, ("expected non-empty fallback set", n)
        for record in records:
            q = record["query"]
            assert _is_well_formed_query(q), ("gate disagrees", n, repr(q))
            _assert_query_well_formed(q, f"garbled@n={n}")
            # axis must survive intact/additive on every record.
            assert str(record.get("axis") or "").strip(), ("missing axis", n, repr(q))


def test_description_sentences_never_become_query_terms():
    """DAMDAM 2026-07-01: PDP description sentences leaked in as attributes and
    produced garbled multi-clause queries ("description a gentle scrub formulated
    with a natural exfoliator … face cleansers"). A shopper term is a short noun
    phrase; sentence-length values are dropped, real multi-word attributes kept."""
    from services.agent_center_bd_report_service import _clean_prompt_term

    assert _clean_prompt_term(
        "description a gentle scrub formulated with a natural exfoliator to draw "
        "out impurities and prevent pore buildup without stripping skin dry"
    ) == ""
    assert _clean_prompt_term(
        "a hybrid between an essence and setting spray this provides extra care"
    ) == ""
    # Real attributes (well under the word cap) survive unchanged.
    for legit in (
        "bond repair treatment",
        "low molecular weight collagen",
        "green tea and shea butter",
        "vitamin c serum",
    ):
        assert _clean_prompt_term(legit) == legit, repr(legit)


def test_generic_container_category_falls_through_to_title():
    """DAMDAM 2026-07-01: a Vitamin C serum whose category resolved to "set" was
    probed as "best set" and returned cookware brands as skincare rivals. Generic
    container categories are rejected so the anchor derives from the title."""
    from services.agent_center_bd_report_service import (
        _category_for_unbranded_prompts,
        _unbranded_category_specs,
    )

    product = {"title": "GINKGO BOUNCY Water Cream", "product_type": "set"}
    category = _category_for_unbranded_prompts(product, "set", {})
    assert category not in {"set", "gift set", "kit", "bundle", "collection"}
    assert "cream" in category  # title-derived, not the bundle label

    # And a container category passed straight to the spec builder yields nothing.
    assert _unbranded_category_specs(
        category="gift set", graph={}, topics=[], bullets=[],
    ) == []


def test_container_category_never_reaches_the_filler_pool():
    """The filler-pool path builds queries straight off product_type; a container
    product_type ("set") must not produce 'best set to buy online' / 'compare set
    options' filler even when base+sidewalk under-fill the target."""
    from services.agent_center_bd_report_service import _build_per_sku_audit_query_records

    ctx = {
        "sku_key": "sku-set",
        "merchant_id": "m-1",
        "product": {"title": "GINKGO BOUNCY Water Cream", "product_type": "set"},
    }
    container_filler = (
        "top set", "recommended set", "best rated set", "popular set",
        "compare set options", "set reviews", "best set to buy online",
    )
    for n in (8, 16, 40):
        records = _build_per_sku_audit_query_records(ctx, n)
        assert records, ("expected queries", n)
        for record in records:
            q = str(record["query"]).lower()
            for junk in container_filler:
                assert junk not in q, ("container filler leaked", n, junk, record["query"])


def test_lane_product_evidence_drops_promo_phrases():
    """DAMDAM 2026-07-01: "skincare discount" rode PDP/banner copy into the lane
    evidence phrases and became the merchant's headline recommendation. The lane
    evidence collector must gate promo phrases the same way query generation does."""
    from services.sku_lane_priority import build_lane_product_evidence

    evidence = build_lane_product_evidence(
        product={
            "title": "GINKGO BOUNCY Water Cream",
            "product_type": "moisturizer",
            "tags": ["skincare discount", "free shipping", "hydrating"],
        },
    )
    phrases = " | ".join(evidence["phrases"]).lower()
    assert "discount" not in phrases
    assert "free shipping" not in phrases
    # A real attribute from the same tag list is kept.
    assert any("hydrating" in p for p in evidence["phrases"])


def test_promo_terms_never_become_query_axis_terms():
    """Regression: a promotional/marketing term ("skincare discount") leaked in as
    an enrichment topic and produced nonsense merchant-facing queries like
    "best moisturizer for skincare discount" across every SKU of the DAMDAM audit,
    which a DTC founder flagged as an outright trust-killing bug. Promo/discount
    noise must be dropped at the source cleaner so it never interpolates into a
    query template."""
    from services.agent_center_bd_report_service import (
        _build_per_sku_audit_query_records,
        _clean_prompt_term,
        _is_promo_term,
    )

    # Direct unit checks: promo terms collapse to "" (word-boundary matched).
    for promo in (
        "skincare discount",
        "discount",
        "20% off",
        "50 percent off",
        "free shipping",
        "on sale",
        "flash sale",
        "clearance",
        "coupon code",
        "buy one get one",
        "black friday deal",
        "gift with purchase",
        "shop now",
        "bestseller",
    ):
        assert _clean_prompt_term(promo) == "", repr(promo)
        assert _is_promo_term(promo.lower()), repr(promo)

    # Real attributes that merely share a substring with a promo token survive.
    for legit in (
        "salicylic acid",  # not "sale"
        "paraben free",    # "free" only promo as "free shipping"/"free gift"
        "cruelty free",
        "fragrance free",
        "sensitive skin",
        "vitamin c",
        "anti aging",
    ):
        assert _clean_prompt_term(legit) == legit, repr(legit)
        assert not _is_promo_term(legit), repr(legit)

    # End-to-end: promo topics/bullets never reach a generated query.
    promo_ctx = {
        "sku_key": "sku-promo",
        "merchant_id": "m-1",
        "product": {
            "title": "DAMDAM Moisturizer",
            "product_type": "moisturizer",
            "attributes_raw": {"tags": ["skincare discount", "sale", "hydrating"]},
        },
        "product_enrichment": {
            "topic_tags": ["skincare discount", "free shipping", "hydration"],
            "audience_tags": ["50% off shoppers", "sensitive skin"],
            "usage_scenarios": ["black friday", "daily routine"],
            "bullet_points": ["coupon code SAVE20", "lightweight formula"],
        },
    }
    # Attribute-derived noise that used to interpolate as a query axis term
    # ("best moisturizer for skincare discount"). The fixed intent template
    # "{title} for sale" is a legitimate availability query and out of scope —
    # it isn't derived from topics/bullets, so it is intentionally preserved.
    leaked_attribute_noise = (
        "discount", "% off", "percent off", "free shipping",
        "coupon", "on sale", "flash sale", "black friday", "bestseller",
    )
    for n in (1, 3, 8, 16, 40):
        records = _build_per_sku_audit_query_records(promo_ctx, n)
        assert records, ("expected non-empty query set", n)
        for record in records:
            q = str(record["query"]).lower()
            # No "best {category} for {promo}" / "{promo} {category}" leaks.
            assert "for skincare discount" not in q, ("bug regressed", n, repr(record["query"]))
            for noise in leaked_attribute_noise:
                assert noise not in q, ("promo attribute leaked", n, noise, repr(record["query"]))


def test_deepseek_verify_deweights_only_answer_quality():
    from services.agent_center_bd_report_service import compute_citation_score

    verify_outputs = [{
        "provider": "deepseek",
        "role": "verify",
        "scan_mode": "answer_quality_verify",
        "sku_key": "sku-1",
        "target_prompt_key": [
            "sku-1",
            "intent",
            "where can i buy bright skin serum",
        ],
        "verdict": {
            "supports_recommendation": False,
            "misstates_facts": True,
            "note": "The answer overstates the SKU fit.",
        },
    }]

    score, breakdown = compute_citation_score(
        _base_sku_ctx(),
        _probe_runs(),
        verify_outputs=verify_outputs,
    )

    assert score == 45
    assert breakdown["first_party_rate"]["numerator"] == 1
    assert breakdown["sku_mention_rate"]["numerator"] == 1
    assert breakdown["authority_near_variant_rate"]["numerator"] == 1
    assert breakdown["answer_quality_rate"]["numerator"] == 0
    assert breakdown["answer_quality_rate"]["deterministic_numerator"] == 1
    assert breakdown["answer_quality_rate"]["verify_deweighted"] == 1


def test_any_provider_first_party_is_or_not_majority_over_union():
    """BUG A regression (ANUKO 2026-07-02 false-INVISIBLE): the combined
    any_profile_provider first_party_rate must be an OR of the per-provider
    decision, never below max(per-provider). Each provider grounds on the
    merchant PDP for a DIFFERENT prompt; the earlier majority-over-union test
    let each provider's third-party sources veto the other's first-party hit."""
    from services.agent_center_bd_report_service import (
        _any_provider_probe_runs,
        build_citation_by_provider,
        compute_citation_score,
    )

    ctx = _base_sku_ctx()
    runs = _multi_provider_probe_runs()

    by_provider = build_citation_by_provider(ctx, runs)
    gem_fp = by_provider["gemini"]["breakdown"]["first_party_rate"]["numerator"]
    gpt_fp = by_provider["chatgpt"]["breakdown"]["first_party_rate"]["numerator"]
    assert gem_fp == 1  # gemini cites the PDP on "where can I buy"
    assert gpt_fp == 1  # chatgpt cites the PDP on "best serum for dullness"

    _score, combined = compute_citation_score(
        ctx, _any_provider_probe_runs(runs, sku_ctx=ctx),
    )
    # OR across providers over 2 distinct prompts → 2/2, and never below either
    # provider's own count.
    assert combined["first_party_rate"]["numerator"] == 2
    assert combined["first_party_rate"]["denominator"] == 2
    assert combined["first_party_rate"]["numerator"] >= max(gem_fp, gpt_fp)


def test_any_provider_first_party_respects_negative_verdict():
    """The OR is over the FULL per-run credit (grounded-primary AND not an
    explicit negative), so a provider that grounds on the PDP while denying the
    product does not earn first-party credit."""
    from services.agent_center_bd_report_service import (
        _any_provider_probe_runs,
        compute_citation_score,
    )

    ctx = _base_sku_ctx()
    runs = [{
        "provider": "gemini",
        "probe_run_id": "probe-neg",
        "raw_runs": [{
            "query": "is bright skin serum any good",
            # PDP is a grounding source, but the answer explicitly denies the
            # product is the right/visible one → no first-party credit.
            "parsed": {"product_visible": False, "correct_sku": False},
            "grounding_sources": [
                {"uri": "https://merchant.test/products/serum", "title": "Bright Skin Serum"},
            ],
            "url_match": {"in_grounding": True},
            "axis_metadata": {"axis": "intent", "source": "auto_generated", "sku_key": "sku-1"},
        }],
    }]

    _score, combined = compute_citation_score(
        ctx, _any_provider_probe_runs(runs, sku_ctx=ctx),
    )
    assert combined["first_party_rate"]["numerator"] == 0


@pytest.mark.asyncio
async def test_build_per_sku_report_end_to_end_with_mocked_loaders(monkeypatch):
    from services import agent_center_bd_report_service as bd

    async def fake_load_sku_context(sku_key: str, merchant_id: str) -> Dict[str, Any]:
        ctx = _base_sku_ctx()
        ctx["sku_key"] = sku_key
        ctx["merchant_id"] = merchant_id
        return ctx

    async def fake_load_runs(sku_key: str, merchant_id: str, audit_run_id: str, include_internal_comparison: bool = False) -> List[Dict[str, Any]]:
        return _multi_provider_probe_runs()

    monkeypatch.setattr(bd, "load_sku_context", fake_load_sku_context)
    monkeypatch.setattr(bd, "load_per_sku_probe_runs", fake_load_runs)

    report = await bd.build_per_sku_report("sku-1", "m-1", "audit-1")
    assert report["sku_key"] == "sku-1"
    assert set(report["scores"]) == {"identity", "content_richness", "routability", "citation"}
    # build_per_sku_report runs no verify pass → answer_quality is unscored (0).
    # any_profile_provider first_party is an OR across providers: gemini cites the
    # merchant PDP on the "where can I buy" prompt and chatgpt cites it on the
    # "best serum for dullness" prompt, so BOTH prompts earn first-party credit
    # (2/2). The earlier code re-scored a MAJORITY test over the union of every
    # provider's grounding sources, letting a non-citing provider's third-party
    # sources veto a citing provider's hit — deflating this to 1/2 (score 68) and
    # driving false-INVISIBLE verdicts (the ANUKO 2026-07-02 regression).
    assert report["scores"]["citation"]["score"] == 90
    assert report["scores"]["citation"]["breakdown"]["answer_quality_rate"]["points"] == 0
    assert report["scores"]["citation"]["breakdown"]["first_party_rate"]["numerator"] == 2
    assert report["scores"]["citation"]["breakdown"]["first_party_rate"]["denominator"] == 2
    assert report["scores"]["citation"]["breakdown"]["sku_mention_rate"]["numerator"] == 2
    assert report["scores"]["citation"]["breakdown"]["aggregation_rule"].startswith("any_profile_provider")
    assert set(report["citation_by_provider"]) == {"gemini", "chatgpt"}
    # Per-provider citation also excludes unverified answer_quality (no verify
    # ran in this end-to-end), so each provider is 5 below the old credit.
    assert report["citation_by_provider"]["gemini"]["score"] == 45
    assert report["citation_by_provider"]["chatgpt"]["score"] == 45
    assert report["deliverability"]["status"] == "transactable"
    assert report["deliverability"]["checkout"]["commerce_path"] == "pivota_direct_quote_first"
    assert report["checkout_handoff"]["status"] == "eligible"
    assert report["checkout_handoff"]["offer_id"] == "offer-1"
    assert report["checkout_handoff"]["pivota_signature_id"] == "sig_123"
    assert "handoff_url" not in report["checkout_handoff"]
    assert report["axis_coverage"] == {"intent": 2, "concern": 2}
    assert report["verbatim_grounding_evidence"][0]["probe_run_id"] == "probe-1"
    assert report["failing_prompts"][0]["evidence_run_id"] == "probe-1"
    assert report["provider_models"]["chatgpt"] == {
        "model": "chat-latest",
        "model_is_override": False,
    }
    assert report["model_is_override"] is False


@pytest.mark.asyncio
async def test_run_brand_report_per_sku_runs_bounded_deepseek_verify(monkeypatch):
    from config import settings as settings_module
    from services import agent_center_bd_report_service as bd

    monkeypatch.setattr(settings_module.settings, "deepseek_api_key", "test-key")

    calls: List[Dict[str, Any]] = []

    async def fake_load_sku_context(sku_key: str, merchant_id: str) -> Dict[str, Any]:
        ctx = _base_sku_ctx()
        ctx["sku_key"] = sku_key
        ctx["merchant_id"] = merchant_id
        return ctx

    async def fake_load_runs(sku_key: str, merchant_id: str, audit_run_id: str, include_internal_comparison: bool = False) -> List[Dict[str, Any]]:
        return _positive_probe_runs(count=4)

    async def fake_probe(**kwargs):
        calls.append(kwargs)
        return {
            "scan_mode": kwargs["scan_mode"],
            "provider": "deepseek",
            "role": "verify",
            "raw_runs": [{
                "provider": "deepseek",
                "role": "verify",
                "query": (kwargs.get("context") or {}).get("verify_query"),
                "parsed": {
                    "supports_recommendation": False,
                    "misstates_facts": True,
                    "note": "Answer does not support this SKU.",
                },
            }],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "scores": {"visibility_score": 0},
        }

    monkeypatch.setattr(bd, "load_sku_context", fake_load_sku_context)
    monkeypatch.setattr(bd, "load_per_sku_probe_runs", fake_load_runs)
    monkeypatch.setattr(bd.llm_client, "probe", fake_probe)

    report = await bd.run_brand_report(
        merchant_name="TestBrand",
        merchant_domain="merchant.test",
        products=[{"sku_key": "sku-1", "product_key": "prod-1"}],
        coverage_profile="us_shopper",
        audit_mode="per_sku",
        merchant_id="m-1",
        audit_run_id="audit-1",
        prompts_per_sku=4,
    )

    assert len(calls) == 1
    assert calls[0]["provider"] == "deepseek"
    assert calls[0]["scan_mode"] == "answer_quality_verify"
    # Perf fix: verify probes run concurrently (asyncio.gather) with a tighter
    # timeout than the 30s generation default, so a slow/flaky DeepSeek can't
    # serialize into a multi-minute (apparently-hung) audit.
    assert calls[0]["timeout_s"] == bd._VERIFY_PROBE_TIMEOUT_S
    sku_report = report["per_sku_reports"][0]
    assert sku_report["verify_summary"]["verified"] == 1
    assert sku_report["verify_summary"]["flagged"] == 1
    assert sku_report["verify_summary"]["not_verified"] == 3
    assert sku_report["verify_summary"]["sample_cap"] == 1
    assert sku_report["verify_outputs"][0]["provider"] == "deepseek"
    assert sku_report["verify_outputs"][0]["role"] == "verify"
    # provider_models records the citation/scoring model (gemini) — not {} — so a
    # run is reproducible; the completed verify_summary records the verify model.
    assert sku_report["provider_models"]["gemini"] == {
        "model": "gemini-2.5-flash",
        "default_model": "gemini-2.5-flash",
        "model_is_override": False,
    }
    assert sku_report["verify_summary"]["model"] == settings_module.settings.deepseek_model
    assert set(sku_report["citation_by_provider"]) == {"gemini"}
    citation = sku_report["scores"]["citation"]
    assert citation["breakdown"]["first_party_rate"]["points"] == 45
    assert citation["breakdown"]["sku_mention_rate"]["points"] == 25
    assert citation["breakdown"]["authority_near_variant_rate"]["points"] == 20
    # The single verified prompt was flagged (supports_recommendation=false),
    # and it's the only verified prompt → answer_quality is 0/1 = 0. (Unsampled
    # prompts no longer earn unverified credit.)
    assert citation["breakdown"]["answer_quality_rate"]["points"] == 0
    assert citation["breakdown"]["answer_quality_rate"]["numerator"] == 0
    assert citation["breakdown"]["answer_quality_rate"]["denominator"] == 1


@pytest.mark.asyncio
async def test_run_brand_report_skips_verify_when_deepseek_key_missing(monkeypatch):
    from config import settings as settings_module
    from services import agent_center_bd_report_service as bd

    monkeypatch.setattr(settings_module.settings, "deepseek_api_key", None)

    async def fake_load_sku_context(sku_key: str, merchant_id: str) -> Dict[str, Any]:
        ctx = _base_sku_ctx()
        ctx["sku_key"] = sku_key
        ctx["merchant_id"] = merchant_id
        return ctx

    async def fake_load_runs(sku_key: str, merchant_id: str, audit_run_id: str, include_internal_comparison: bool = False) -> List[Dict[str, Any]]:
        return _positive_probe_runs(count=2)

    async def fail_probe(**kwargs):
        raise AssertionError("DeepSeek verify should be skipped without API key")

    monkeypatch.setattr(bd, "load_sku_context", fake_load_sku_context)
    monkeypatch.setattr(bd, "load_per_sku_probe_runs", fake_load_runs)
    monkeypatch.setattr(bd.llm_client, "probe", fail_probe)

    report = await bd.run_brand_report(
        merchant_name="TestBrand",
        merchant_domain="merchant.test",
        products=[{"sku_key": "sku-1", "product_key": "prod-1"}],
        coverage_profile="us_shopper",
        audit_mode="per_sku",
        merchant_id="m-1",
        audit_run_id="audit-1",
        prompts_per_sku=4,
    )

    sku_report = report["per_sku_reports"][0]
    assert sku_report["verify_summary"]["status"] == "skipped"
    assert sku_report["verify_summary"]["reason"] == "missing_deepseek_api_key"
    assert sku_report["verify_summary"]["not_verified"] == 2
    assert sku_report["verify_outputs"] == []
    # Verify was skipped (no DeepSeek key) → answer_quality is unscored, so the
    # ceiling is 90, not a verified-looking 100. This is the honesty fix: the
    # free/no-verify path no longer awards the 10 answer-quality points it never
    # checked.
    assert sku_report["scores"]["citation"]["score"] == 90
    assert (
        sku_report["scores"]["citation"]["breakdown"]["answer_quality_rate"]["points"]
        == 0
    )


@pytest.mark.asyncio
async def test_per_sku_report_and_cost_summary_stamp_model_override(monkeypatch):
    from services import agent_center_bd_report_service as bd

    async def fake_load_sku_context(sku_key: str, merchant_id: str) -> Dict[str, Any]:
        ctx = _base_sku_ctx()
        ctx["sku_key"] = sku_key
        ctx["merchant_id"] = merchant_id
        return ctx

    async def fake_load_runs(sku_key: str, merchant_id: str, audit_run_id: str, include_internal_comparison: bool = False) -> List[Dict[str, Any]]:
        runs = _multi_provider_probe_runs()
        runs[1]["model"] = "gpt-5.5-mini"
        runs[1]["model_is_override"] = True
        runs[1]["default_model"] = "chat-latest"
        return runs

    monkeypatch.setattr(bd, "load_sku_context", fake_load_sku_context)
    monkeypatch.setattr(bd, "load_per_sku_probe_runs", fake_load_runs)

    report = await bd.build_per_sku_report("sku-1", "m-1", "audit-1")
    assert report["provider_models"]["chatgpt"] == {
        "model": "gpt-5.5-mini",
        "model_is_override": True,
        "default_model": "chat-latest",
    }
    assert report["model_is_override"] is True

    cost_summary = await bd._cost_summary_for_per_sku_audit(
        None,
        {"sku-1": await fake_load_runs("sku-1", "m-1", "audit-1")},
    )
    assert cost_summary["provider_models"]["chatgpt"] == {
        "model": "gpt-5.5-mini",
        "model_is_override": True,
        "default_model": "chat-latest",
    }
    assert cost_summary["model_is_override"] is True


def test_build_brand_rollup_priority_queue_ordering():
    from services.agent_center_bd_report_service import build_brand_rollup

    reports = [
        {
            "sku_key": "sku-a",
            "product_key": "prod-a",
            # citation=0 -> min dimension is 0 -> _sku_band == "blocked". (The
            # pipeline always sets band via _sku_band(scores); a "partial" band
            # with citation=0 isn't a state it produces.) blocked_skus is now
            # derived from band, so this realistic band keeps sku-a blocked.
            "band": "blocked",
            "impact_proxy": 10,
            "scores": {
                "identity": {"score": 80},
                "content_richness": {"score": 40},
                "routability": {"score": 90},
                "citation": {"score": 0},
            },
            "primary_gaps": [{"dimension": "content_richness", "bucket": "answer_shaped_modules", "reason": "missing FAQ"}],
            "deliverability": {
                "status": "servable_not_transactable",
                "summary": "This SKU can be served, but checkout is not ready enough to promise a transaction.",
                "serving": {"status": "ready"},
                "checkout": {"status": "blocked"},
            },
        },
        {
            "sku_key": "sku-b",
            "product_key": "prod-b",
            "band": "ready",
            "impact_proxy": 2,
            "scores": {
                "identity": {"score": 90},
                "content_richness": {"score": 80},
                "routability": {"score": 90},
                "citation": {"score": 70},
            },
            "primary_gaps": [{"dimension": "citation", "bucket": "authority_near_variant_rate", "reason": "no authority"}],
            "deliverability": {
                "status": "transactable",
                "summary": "This SKU is serving eligible and has a ready merchant-checkout path for Pivota direct purchase.",
                "serving": {"status": "ready"},
                "checkout": {"status": "ready"},
            },
            "checkout_handoff": {
                "status": "eligible",
                "label": "Open buyable Pivota product page",
                "handoff_url": "https://agent.pivota.cc/checkout/handoff?token=t",
            },
        },
    ]
    rollup = build_brand_rollup(reports, "m-1")
    assert rollup["dimensions"]["content_richness"]["median"] == 60
    assert rollup["priority_queue"][0]["sku_key"] == "sku-a"
    assert rollup["priority_queue"][0]["priority_score"] == 600
    assert rollup["blocked_skus"][0]["sku_key"] == "sku-a"
    assert rollup["deliverability"]["status_counts"] == {
        "servable_not_transactable": 1,
        "transactable": 1,
    }
    assert rollup["deliverability"]["attention_skus"][0]["sku_key"] == "sku-a"
    assert rollup["deliverability"]["transactable_skus"][0]["sku_key"] == "sku-b"
    assert (
        rollup["deliverability"]["transactable_skus"][0]["checkout_handoff"]["handoff_url"]
        == "https://agent.pivota.cc/checkout/handoff?token=t"
    )


def test_default_brand_markdown_surfaces_deliverability_before_detail():
    from services.agent_center_bd_report_service import render_brand_markdown

    report = {
        "merchant_name": "TestBrand",
        "merchant_domain": "merchant.test",
        "timestamp": "2026-06-08T00:00:00Z",
        "brand_rollup": {
            "deliverability": {
                "status_counts": {
                    "transactable": 1,
                    "servable_not_transactable": 1,
                }
            }
        },
        "per_sku_reports": [
            {
                "sku_key": "sku-ready",
                "sku_title": "Ready Serum",
                "checkout_handoff": {
                    "status": "eligible",
                    "label": "Open buyable Pivota product page",
                    "handoff_url": "https://agent.pivota.cc/checkout/handoff?token=t",
                },
                "deliverability": {
                    "status": "transactable",
                    "summary": "This SKU is serving eligible and has a ready merchant-checkout path.",
                    "serving": {"status": "ready"},
                    "checkout": {"status": "ready"},
                },
            },
            {
                "sku_key": "sku-stock",
                "sku_title": "Unknown Stock Serum",
                "deliverability": {
                    "status": "servable_not_transactable",
                    "summary": "This SKU can be served, but checkout is not ready enough to promise a transaction.",
                    "serving": {"status": "ready"},
                    "checkout": {"status": "blocked"},
                },
            },
        ],
    }

    md = render_brand_markdown(report)

    assert "## Servability and checkout" in md
    assert "1 of 2 audited SKUs is confirmed transactable." in md
    assert "explicit available-stock signal" in md
    assert "[Open buyable Pivota product page](https://agent.pivota.cc/checkout/handoff?token=t)" in md
    assert "Unknown Stock Serum" in md
    assert md.find("## Servability and checkout") < md.find("## Per-product detail")


def test_build_authority_map_classification_and_reddit_shape():
    from services.agent_center_bd_report_service import build_authority_map

    per_sku_reports = [{"sku_key": "sku-1", "product_key": "prod-1", "content_key": "ck_abc"}]
    authority_map = build_authority_map(per_sku_reports, {"sku-1": _multi_provider_probe_runs()})
    sku_entry = authority_map["skus"][0]
    host_types = {h["host"]: h["host_type"] for h in sku_entry["authority_hosts"]}
    hosts = {h["host"]: h for h in sku_entry["authority_hosts"]}
    assert host_types["forbes.com"] == "editorial"
    assert host_types["reddit.com"] == "reddit"
    assert hosts["forbes.com"]["providers"] == ["gemini"]
    assert hosts["merchant.test"]["providers"] == ["chatgpt", "gemini"]
    reddit = sku_entry["reddit"]["subreddits"][0]
    assert reddit["name"] == "r/SkincareAddiction"
    assert reddit["threads"][0]["provider"] == "gemini"
    assert reddit["threads"][0]["sentiment"] is None
    assert reddit["sentiment_proxy"] is None
    assert "chatgpt" in authority_map["hosts"][0]["providers"]


def test_host_is_first_party_recognizes_brand_storefront_affix():
    """BUG B (ANUKO 2026-07-02): a brand's second storefront whose domain is the
    brand name plus a generic storefront affix (tryanuko.com, anukoofficial.com,
    shopbblab.com) must be tagged first-party — not surfaced as a third party to
    run outreach to. Bounded to alias>=5 + exact-affix so a same-category rival
    that merely contains the brand token is NOT swept in."""
    from services.agent_center_bd_report_service import _host_is_first_party
    from services.brand_alias import derive_brand_aliases

    anuko_aliases = derive_brand_aliases("ANUKO", "anukoofficial.com", ())
    assert _host_is_first_party("tryanuko.com", frozenset(), anuko_aliases) is True
    assert _host_is_first_party("anukoofficial.com", frozenset(), anuko_aliases) is True
    assert _host_is_first_party("shopanuko.com", frozenset(), anuko_aliases) is True
    # A genuine third party that just mentions the brand is NOT first-party.
    assert _host_is_first_party("anukoreviews.blogspot.com", frozenset(), anuko_aliases) is False
    assert _host_is_first_party("sephora.com", frozenset(), anuko_aliases) is False

    # Short aliases (< 5 chars) must not affix-match — "glow" + "recipe" is a rival.
    glow_aliases = derive_brand_aliases("Glow", "glow.com", ())
    assert _host_is_first_party("glowrecipe.com", frozenset(), glow_aliases) is False


def test_build_authority_map_folds_in_merchant_owned_domains():
    """A domain Pivota already knows the merchant owns (onboarding/catalog),
    passed via merchant_extra_hosts, is tagged first_party and thus excluded
    from 'who AI cites instead' / outreach."""
    from services.agent_center_bd_report_service import build_authority_map
    from services.merchant_narrative_builder import _who_ai_cites_instead

    runs = [{
        "provider": "gemini",
        "raw_runs": [{
            "query": "where to buy the serum",
            "parsed": {"product_visible": True, "correct_sku": True},
            "grounding_sources": [
                {"uri": "https://us-store.example-brand.com/p/serum", "title": "us-store.example-brand.com"},
            ],
            "url_match": {"in_grounding": True},
            "axis_metadata": {"axis": "intent", "sku_key": "sku-1"},
        }],
    }]
    per_sku_reports = [{"sku_key": "sku-1", "product_key": "prod-1"}]

    # Without the owned-domain hint, the second storefront reads as a third party.
    am_without = build_authority_map(
        per_sku_reports, {"sku-1": runs}, merchant_host="brand.example.com",
    )
    host_without = {h["host"]: h for h in am_without["hosts"]}["us-store.example-brand.com"]
    assert host_without["first_party"] is False

    # With it folded in, it's the merchant's own findability, not an outreach target.
    am_with = build_authority_map(
        per_sku_reports,
        {"sku-1": runs},
        merchant_host="brand.example.com",
        merchant_extra_hosts={"us-store.example-brand.com"},
    )
    host_with = {h["host"]: h for h in am_with["hosts"]}["us-store.example-brand.com"]
    assert host_with["first_party"] is True
    who = _who_ai_cites_instead(am_with)
    assert "us-store.example-brand.com" not in {
        c["host"] for c in (who.get("cited_hosts") or [])
    }


def test_build_authority_map_strips_merchant_own_brand_from_competitors():
    """Follow-up to #1382: an engine's grounded self-report sometimes lists the
    merchant itself among "competitors". That own-brand name must be stripped
    before it reaches a cited host's competitors_named — otherwise it surfaces as
    a named rival and (post-#1382) fires a `recommends_rival` outreach move whose
    "rival" is actually the merchant. A genuine rival on another host must
    survive so the filter isn't over-stripping."""
    from services.agent_center_bd_report_service import build_authority_map
    from services.merchant_narrative_builder import (
        _outreach_moves,
        _who_ai_cites_instead,
    )

    probe_runs = [
        {
            "provider": "gemini",
            "probe_run_id": "probe-self",
            "raw_runs": [
                {
                    # goodhousekeeping.com was cited with ONLY the merchant's own
                    # name (and an aliased form) listed as a "competitor".
                    "query": "best vitamin c serum",
                    "parsed": {
                        "product_visible": True,
                        "correct_sku": True,
                        "competitors_listed": ["GlowCo", "GlowCo Skincare"],
                    },
                    "grounding_sources": [
                        {"uri": "https://www.goodhousekeeping.com/beauty/serums", "title": "goodhousekeeping.com"},
                    ],
                    "url_match": {"in_grounding": False},
                },
                {
                    # allure.com was cited with a GENUINE rival (plus the merchant
                    # again) — the rival must be retained.
                    "query": "top rated vitamin c serums",
                    "parsed": {
                        "product_visible": True,
                        "competitors_listed": ["Rival Beauty", "GlowCo"],
                    },
                    "grounding_sources": [
                        {"uri": "https://www.allure.com/gallery/best-vitamin-c-serums", "title": "allure.com"},
                    ],
                    "url_match": {"in_grounding": False},
                },
            ],
        }
    ]
    authority_map = build_authority_map(
        [{"sku_key": "sku-1", "product_key": "prod-1"}],
        {"sku-1": probe_runs},
        merchant_host="glowco.com",
        merchant_brand="GlowCo",
    )

    # The merchant's own brand / alias never surfaces as a named competitor.
    all_named = [
        c
        for sku in authority_map["skus"]
        for row in sku["authority_hosts"]
        for c in (row.get("competitors_named") or [])
    ]
    assert "GlowCo" not in all_named
    assert "GlowCo Skincare" not in all_named
    # The genuine rival is retained (no over-stripping).
    assert "Rival Beauty" in all_named

    # goodhousekeeping.com had only the merchant's own name as a "competitor", so
    # its outreach move must NOT carry the rival co-citation framing; allure.com
    # kept a real rival, so its move still does. The rendered copy is co-citation
    # framing ("grounds answers that recommend competitors over you"), NOT the
    # older per-host "recommends a competitor over you" claim — see the
    # recommends_rival comment in merchant_narrative_builder._outreach_moves.
    _RIVAL_FRAMING = "grounds answers that recommend competitors over you"
    who = _who_ai_cites_instead(authority_map)
    moves = {m["host"]: m for m in _outreach_moves(who)}
    assert "goodhousekeeping.com" in moves
    assert _RIVAL_FRAMING not in moves["goodhousekeeping.com"]["why"]
    assert not moves["goodhousekeeping.com"].get("competitors_named")
    assert "allure.com" in moves
    assert _RIVAL_FRAMING in moves["allure.com"]["why"]


def test_strip_own_brand_competitors_word_boundary_guard():
    """The own-brand strip must not erase a genuine rival whose name merely
    CONTAINS the merchant brand as a sub-word — a plain bidirectional substring
    would (e.g. brand "Glow" swallowing "Glowbiotics"). It uses a word-boundary
    match once the brand is specific enough (len >= 4), so only the merchant's
    own bounded brand token is dropped."""
    from services.agent_center_bd_report_service import _strip_own_brand_competitors
    from services.brand_alias import derive_brand_aliases

    brand = "Glow"
    aliases = derive_brand_aliases(brand, "glow.com", ())
    kept = _strip_own_brand_competitors(
        ["Glow", "Glowbiotics", "Sephora", "Rival Beauty"],
        brand.lower(),
        aliases,
    )
    # Merchant's own bounded brand token dropped; sub-word rivals retained.
    assert "Glow" not in kept
    assert "Glowbiotics" in kept
    assert "Sephora" in kept
    assert "Rival Beauty" in kept

    # No merchant identity -> everything passes through (legacy callers).
    assert _strip_own_brand_competitors(["Anything", "GlowCo"], "", ()) == [
        "Anything",
        "GlowCo",
    ]


def _gemini_redirector_run(query: str, title: str) -> Dict[str, Any]:
    """A Gemini grounding run whose only source is delivered as a Vertex
    redirector URI with the REAL publisher domain in `title` — the prod v3
    per-SKU shape that regressed authority_map onto vertexaisearch."""
    return {
        "query": query,
        "parsed": {"product_visible": True, "correct_sku": True},
        "grounding_sources": [
            {
                "uri": (
                    "https://vertexaisearch.cloud.google.com/"
                    f"grounding-api-redirect/AUZIabc-{title}"
                ),
                "title": title,
            }
        ],
        "url_match": {"in_grounding": False},
    }


def test_build_authority_map_resolves_gemini_redirector_to_real_host():
    """Fix 1 regression: every Gemini citation used to collapse onto the Vertex
    redirector host (`vertexaisearch.cloud.google.com`, host_type unclassified)
    because build_authority_map keyed off the redirector URI. The real domain in
    each grounding chunk's `title` must drive the host rollup instead — and each
    resolved host must classify via the cited-host registry (no vertexaisearch in
    merchant-facing output)."""
    from services.agent_center_bd_report_service import build_authority_map

    probe_runs = [
        {
            "provider": "gemini",
            "probe_run_id": "probe-redir",
            "raw_runs": [
                _gemini_redirector_run("where to buy this serum", "oliveyoung.com"),
                _gemini_redirector_run("best lash serum", "ebay.com"),
                _gemini_redirector_run("buy lash serum online", "desertcart.com"),
                _gemini_redirector_run("editorial roundup", "goodhousekeeping.com"),
            ],
        }
    ]
    authority_map = build_authority_map(
        [{"sku_key": "sku-1", "product_key": "prod-1"}],
        {"sku-1": probe_runs},
    )

    matrix = {h["host"]: h for h in authority_map["hosts"]}
    # Acceptance: real classified hosts present, zero vertexaisearch.
    assert "vertexaisearch.cloud.google.com" not in matrix
    assert {"oliveyoung.com", "ebay.com", "desertcart.com", "goodhousekeeping.com"} <= set(matrix)
    # Hosts classify via cited_host_classifier (not "unclassified").
    assert matrix["oliveyoung.com"]["host_type"] == "retailer"
    assert matrix["ebay.com"]["host_type"] == "retailer"  # marketplace folds to retailer
    assert matrix["goodhousekeeping.com"]["host_type"] == "editorial"
    assert matrix["oliveyoung.com"]["provider_counts"] == {"gemini": 1}

    sku_hosts = {h["host"] for h in authority_map["skus"][0]["authority_hosts"]}
    assert "vertexaisearch.cloud.google.com" not in sku_hosts
    assert "oliveyoung.com" in sku_hosts


def test_build_authority_map_drops_unresolvable_redirector_no_vertex_leak():
    """A redirector source with no usable title (nothing to resolve to) is
    dropped, never emitted as `vertexaisearch.cloud.google.com`. A real
    (non-redirector) URI in the same run still resolves directly."""
    from services.agent_center_bd_report_service import build_authority_map

    probe_runs = [
        {
            "provider": "gemini",
            "probe_run_id": "probe-mixed",
            "raw_runs": [
                {
                    "query": "best serum",
                    "parsed": {"product_visible": True},
                    "grounding_sources": [
                        # Redirector with empty title -> unresolvable -> dropped.
                        {
                            "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIxyz",
                            "title": "",
                        },
                        # Real publisher URI (e.g. probe-time 302 already followed).
                        {"uri": "https://www.allure.com/best-serums", "title": "Best serums 2026"},
                    ],
                    "url_match": {"in_grounding": False},
                }
            ],
        }
    ]
    authority_map = build_authority_map(
        [{"sku_key": "sku-1", "product_key": "prod-1"}],
        {"sku-1": probe_runs},
    )
    hosts = {h["host"] for h in authority_map["hosts"]}
    assert "vertexaisearch.cloud.google.com" not in hosts
    assert "allure.com" in hosts


# ---------------------------------------------------------------------------
# Fix 2 — listing-vs-endorsement
# ---------------------------------------------------------------------------


def _redir_run(query: str, title: str, axis: str = "intent") -> Dict[str, Any]:
    """Gemini redirector run with a real domain in `title` and an axis tag, so
    findability/endorsement and query-class can be exercised together."""
    return {
        "query": query,
        "axis_metadata": {"axis": axis, "source": "auto_generated", "sku_key": "sku-1"},
        "parsed": {"product_visible": True, "correct_sku": True},
        "grounding_sources": [
            {
                "uri": (
                    "https://vertexaisearch.cloud.google.com/"
                    f"grounding-api-redirect/AUZI-{title}"
                ),
                "title": title,
            }
        ],
        "url_match": {"in_grounding": False},
    }


def _editorial_run(
    query: str, host: str, brand: str, axis: str = "category"
) -> Dict[str, Any]:
    """A real-URI editorial grounding whose title NAMES the brand — the honest
    endorsement shape. Post W1 site-8 cutover, endorsement requires the source
    itself to name the brand (RunFacts T2), not merely a run-level correct_sku
    flag; `_redir_run`'s bare-host titles no longer imply endorsement."""
    return {
        "query": query,
        "axis_metadata": {"axis": axis, "source": "auto_generated", "sku_key": "sku-1"},
        "parsed": {"product_visible": True, "correct_sku": True},
        "grounding_sources": [
            {
                "uri": f"https://www.{host}/reviews/{brand.lower()}-collagen",
                "title": f"{brand} Collagen Review | {host}",
            }
        ],
        "url_match": {"in_grounding": False},
    }


def test_recommendation_class_axis():
    from services.cited_host_classifier import recommendation_class

    assert recommendation_class("editorial") == "recommends"
    assert recommendation_class("video") == "recommends"
    assert recommendation_class("retailer") == "lists"
    assert recommendation_class("marketplace") == "lists"
    assert recommendation_class("brand") == "lists"
    assert recommendation_class("unclassified") == "unknown"
    assert recommendation_class(None) == "unknown"


def test_merchant_relative_role_and_signal_mapping():
    """Fix 2 merchant-relative classification: each host folds into a spec role
    and each role into exactly one signal (findability / endorsement / neither)."""
    from services.cited_host_classifier import (
        merchant_relative_role,
        role_signal,
        is_findability_role,
        is_endorsement_role,
    )

    assert merchant_relative_role("retailer", first_party=True) == "own_domain"
    assert merchant_relative_role("retailer") == "marketplace_self_listing"
    assert merchant_relative_role("marketplace") == "marketplace_self_listing"
    assert merchant_relative_role("editorial") == "editorial_review"
    assert merchant_relative_role("trade") == "editorial_review"
    assert merchant_relative_role("creator") == "creator"
    assert merchant_relative_role("reddit") == "forum"
    assert merchant_relative_role("unclassified") == "unclassified"
    # is_competitor wins over the bare host type, but first_party always wins.
    assert merchant_relative_role("retailer", is_competitor=True) == "competitor"
    assert (
        merchant_relative_role("brand", first_party=True, is_competitor=True)
        == "own_domain"
    )

    # Findability never bleeds into endorsement (the whole point of Fix 2).
    assert role_signal("own_domain") == "findability"
    assert role_signal("marketplace_self_listing") == "findability"
    assert role_signal("editorial_review") == "endorsement"
    assert role_signal("creator") == "endorsement"
    assert role_signal("forum") == "endorsement"
    assert role_signal("independent_retailer") == "endorsement"
    assert role_signal("competitor") == "neither"
    assert role_signal("unclassified") == "neither"
    assert is_findability_role("own_domain") and not is_endorsement_role("own_domain")
    assert is_endorsement_role("editorial_review") and not is_findability_role(
        "editorial_review"
    )


def test_authority_map_competitor_storefront_excluded_from_signals(monkeypatch):
    """A competitor's brand storefront (classify_host type 'brand', not the
    merchant) is tagged `competitor` and counts toward NEITHER findability nor
    endorsement — a rival's listing surfaced on the merchant's category query
    must never read as the merchant's own visibility."""
    import services.agent_center_bd_report_service as svc

    real_classify = svc.classify_host

    def fake_classify(host, *args, **kwargs):
        if str(host or "").strip().lower() == "rivalbrand.com":
            return {"host": "rivalbrand.com", "type": "brand", "subtype": "dtc_storefront"}
        return real_classify(host, *args, **kwargs)

    monkeypatch.setattr(svc, "classify_host", fake_classify)

    probe_runs = [
        {
            "provider": "gemini",
            "probe_run_id": "p",
            "raw_runs": [
                _redir_run("buy Aruen collagen", "aruen.com", "intent"),
                _redir_run("best collagen supplement", "rivalbrand.com", "category"),
            ],
        }
    ]
    am = svc.build_authority_map(
        [{"sku_key": "sku-1", "product_key": "prod-1"}],
        {"sku-1": probe_runs},
        merchant_host="aruen.com",
        merchant_brand="Aruen",
    )

    roles = {h["host"]: h["citation_role"] for h in am["hosts"]}
    assert roles["aruen.com"] == "own_domain"
    assert roles["rivalbrand.com"] == "competitor"
    rival = next(h for h in am["hosts"] if h["host"] == "rivalbrand.com")
    assert rival["is_competitor"] is True

    summary = am["host_attribution_summary"]
    assert summary["competitor_hosts"] == ["rivalbrand.com"]
    assert "rivalbrand.com" not in summary["findability_hosts"]
    assert "rivalbrand.com" not in summary["endorsement_hosts"]
    assert "rivalbrand.com" not in summary["endorsement_category_hosts"]
    # Own site = findability, the rival on the category query is NOT endorsement.
    assert summary["has_independent_endorsement"] is False
    assert summary["independently_recommended_for_category"] is False
    assert summary["surfaced_only_via_own_listing"] is True


def test_authority_map_separates_findability_from_endorsement():
    """Fix 2 acceptance: own site + marketplaces (eBay/Desertcart/GoSupps) read
    as *findability*; an independent editorial that cites on a category query is
    the only *endorsement* — and is the sole driver of category recommendation."""
    from services.agent_center_bd_report_service import build_authority_map

    probe_runs = [
        {
            "provider": "gemini",
            "probe_run_id": "p",
            "raw_runs": [
                _redir_run("where to buy Aruen collagen", "aruen.com", "intent"),
                _redir_run("Aruen collagen for sale", "ebay.com", "intent"),
                _redir_run("shop Aruen collagen online", "desertcart.com", "intent"),
                _redir_run("best price Aruen collagen", "gosupps.com", "price"),
                # Editorial that actually names the brand on a category query — the
                # honest endorsement (post site-8 cutover: naming, not a run flag).
                _editorial_run("best collagen supplement", "goodhousekeeping.com", "Aruen", "category"),
            ],
        }
    ]
    am = build_authority_map(
        [{"sku_key": "sku-1", "product_key": "prod-1"}],
        {"sku-1": probe_runs},
        merchant_host="aruen.com",
        merchant_brand="Aruen",
    )

    # Merchant-relative roles use the spec vocabulary: own_domain /
    # marketplace_self_listing (findability) vs editorial_review (endorsement).
    roles = {h["host"]: h["citation_role"] for h in am["hosts"]}
    assert roles["aruen.com"] == "own_domain"
    assert roles["ebay.com"] == "marketplace_self_listing"
    assert roles["desertcart.com"] == "marketplace_self_listing"
    assert roles["gosupps.com"] == "marketplace_self_listing"
    assert roles["goodhousekeeping.com"] == "editorial_review"

    # recommend-vs-list axis carried on each host row.
    rec = {h["host"]: h["recommendation_class"] for h in am["hosts"]}
    assert rec["ebay.com"] == "lists"
    assert rec["goodhousekeeping.com"] == "recommends"

    summary = am["host_attribution_summary"]
    assert summary["by_role"] == {
        "own_domain": 1,
        "marketplace_self_listing": 3,
        "editorial_review": 1,
    }
    assert set(summary["findability_hosts"]) == {
        "aruen.com", "ebay.com", "desertcart.com", "gosupps.com",
    }
    assert summary["endorsement_hosts"] == ["goodhousekeeping.com"]
    # Category recommendation is endorsement-driven (the editorial cited on the
    # category query), not the indexed own/marketplace listings.
    assert summary["endorsement_category_hosts"] == ["goodhousekeeping.com"]
    assert summary["has_independent_endorsement"] is True
    assert summary["independently_recommended_for_category"] is True
    assert summary["surfaced_only_via_own_listing"] is False

    sku_signals = am["skus"][0]["citation_signals"]
    assert sku_signals["endorsement_category_hosts"] == ["goodhousekeeping.com"]

    gh = next(h for h in am["hosts"] if h["host"] == "goodhousekeeping.com")
    assert gh["cited_on_category_query"] is True
    ebay = next(h for h in am["hosts"] if h["host"] == "ebay.com")
    assert ebay["cited_on_category_query"] is False


def test_authority_map_own_listing_only_never_reads_as_recommended():
    """A SKU surfaced only through its own site + a marketplace listing has
    findability but zero endorsement — it must never read as 'AI recommends
    you'."""
    from services.agent_center_bd_report_service import build_authority_map

    probe_runs = [
        {
            "provider": "gemini",
            "probe_run_id": "p",
            "raw_runs": [
                _redir_run("buy Ownist Triple Shine", "ownist.com", "intent"),
                _redir_run("Ownist on ebay", "ebay.com", "intent"),
            ],
        }
    ]
    am = build_authority_map(
        [{"sku_key": "sku-1", "product_key": "prod-1"}],
        {"sku-1": probe_runs},
        merchant_host="ownist.com",
        merchant_brand="Ownist",
    )
    sig = am["skus"][0]["citation_signals"]
    assert sig["has_independent_endorsement"] is False
    assert sig["independently_recommended_for_category"] is False
    assert sig["surfaced_only_via_own_listing"] is True
    assert am["host_attribution_summary"]["surfaced_only_via_own_listing"] is True


def test_authority_map_editorial_not_naming_brand_is_not_endorsement():
    """W1 site-8 cutover — the over-attribution fix. Legacy credited EVERY host
    cited in a run where the model self-reported correct_sku as 'naming the
    merchant', so an editorial cited for the CATEGORY (never naming the brand)
    read as an endorsement. RunFacts T2 requires the source itself to name the
    brand: the own site is found, but the category editorial that doesn't name
    Aruen is NOT an endorsement."""
    from services.agent_center_bd_report_service import build_authority_map

    probe_runs = [
        {
            "provider": "gemini",
            "probe_run_id": "p",
            "raw_runs": [
                # Own site found on a branded query (correct_sku True) ...
                _redir_run("buy Aruen collagen", "aruen.com", "intent"),
                # ... and an editorial cited on the CATEGORY query whose grounding
                # never names Aruen (bare-host title). Same run flag correct_sku,
                # but no per-source brand mention.
                _redir_run("best collagen supplement", "goodhousekeeping.com", "category"),
            ],
        }
    ]
    am = build_authority_map(
        [{"sku_key": "sku-1", "product_key": "prod-1"}],
        {"sku-1": probe_runs},
        merchant_host="aruen.com",
        merchant_brand="Aruen",
    )
    summary = am["host_attribution_summary"]
    # goodhousekeeping is still classified an editorial host and surfaces as a
    # "who AI cites instead" channel — it just isn't a merchant ENDORSEMENT.
    assert summary["endorsement_hosts"] == []
    assert summary["has_independent_endorsement"] is False
    assert summary["independently_recommended_for_category"] is False
    assert am["skus"][0]["citation_signals"]["endorsement_hosts"] == []


def test_authority_map_without_merchant_identity_has_no_first_party():
    """Back-compat: callers that omit merchant identity still get a valid map —
    nothing is tagged first-party, roles fall back to host-type semantics."""
    from services.agent_center_bd_report_service import build_authority_map

    probe_runs = [
        {"provider": "gemini", "probe_run_id": "p", "raw_runs": [_redir_run("buy", "ownist.com")]}
    ]
    am = build_authority_map(
        [{"sku_key": "sku-1", "product_key": "prod-1"}],
        {"sku-1": probe_runs},
    )
    own = next(h for h in am["hosts"] if h["host"] == "ownist.com")
    assert own["first_party"] is False
    assert own["citation_role"] in {"unclassified", "marketplace_self_listing"}


def test_query_class_coverage_splits_branded_from_category():
    from services.agent_center_bd_report_service import _query_class_coverage

    probe_runs = [
        {
            "provider": "gemini",
            "probe_run_id": "p",
            "raw_runs": [
                {"query": "where to buy X", "axis_metadata": {"axis": "intent"}},
                {"query": "X reviews", "axis_metadata": {"axis": "review"}},
                {"query": "best supplement", "axis_metadata": {"axis": "category"}},
                {"query": "buy Brand online", "axis_metadata": {"axis": "brand"}},
            ],
        }
    ]
    cov = _query_class_coverage(probe_runs)
    assert cov == {"branded_navigational": 3, "category_discovery": 1}


# ---------------------------------------------------------------------------
# Provider honesty gate: a provider whose probes all errored (e.g. OpenAI 429
# quota) measured NOTHING and must be surfaced as coverage-unavailable, not
# scored as a real 0 that drags the aggregate verdict toward INVISIBLE.
# Reproduces run d4837efe (ANUKO): Gemini succeeded, every ChatGPT run 429'd.
# ---------------------------------------------------------------------------
_OPENAI_429 = (
    "__error__:429 You exceeded your current quota, please check your plan "
    "and billing details."
)


def _chatgpt_all_429_probe_runs(count: int = 3) -> List[Dict[str, Any]]:
    """A ChatGPT payload shaped like the gateway's HTTP-200 all-runs-failed
    response: raw_runs present but each carries an `__error__:` raw, and
    usage.succeeded_runs==0 / failed_runs==count. No status='probe_failed'."""
    raw_runs = [
        {
            "query": f"where can I buy Bright Skin Serum q{idx}",
            "raw": _OPENAI_429,
            "parsed": None,
            "product_visible": False,
            "grounding_sources": [],
            "grounding_chunks": [],
            "url_match": None,
            "axis_metadata": {"axis": "intent", "source": "auto_generated", "sku_key": "sku-1"},
        }
        for idx in range(count)
    ]
    return [{
        "provider": "chatgpt",
        "model": "chat-latest",
        "probe_run_id": "probe-chatgpt-429",
        "runs_count": count,
        "usage": {"succeeded_runs": 0, "failed_runs": count, "tokens_in": 0, "tokens_out": 0},
        "raw_runs": raw_runs,
    }]


def test_provider_probes_all_failed_detects_429_vs_real_answers():
    from services.agent_center_bd_report_service import (
        _provider_probe_run_health,
        _provider_probes_all_failed,
    )

    # All runs 429'd → attempted but zero succeeded → unavailable.
    failed = _chatgpt_all_429_probe_runs(count=4)
    assert _provider_probe_run_health(failed) == (0, 4)
    assert _provider_probes_all_failed(failed) is True

    # A real Gemini payload that answered → measured, not unavailable.
    ok = _probe_runs()
    succeeded, attempted = _provider_probe_run_health(ok)
    assert succeeded == attempted == 2
    assert _provider_probes_all_failed(ok) is False

    # An explicit probe_failed payload (exception path) is still unavailable.
    assert _provider_probes_all_failed(
        [{"provider": "chatgpt", "status": "probe_failed", "runs_count": 0, "raw_runs": []}]
    ) is True


def test_build_citation_by_provider_marks_all_failed_provider_unavailable():
    from services.agent_center_bd_report_service import build_citation_by_provider

    sku_ctx = _base_sku_ctx()
    probe_runs = _probe_runs() + _chatgpt_all_429_probe_runs(count=3)
    cbp = build_citation_by_provider(sku_ctx, probe_runs)

    # Gemini measured real signal → a scored entry (not unavailable).
    assert "coverage_unavailable" not in cbp["gemini"]
    assert cbp["gemini"].get("status") != "probe_failed"
    assert cbp["gemini"]["score"] is not None

    # ChatGPT 429'd wholesale → coverage unavailable, NOT a real 0.
    chatgpt = cbp["chatgpt"]
    assert chatgpt["status"] == "probe_failed"
    assert chatgpt["coverage_unavailable"] is True
    assert chatgpt["score"] is None  # unmeasured, not 0
    assert chatgpt["prompts"] == 0
    assert "429" in chatgpt["error"]


def test_build_citation_by_provider_scores_real_zero_when_answered_no_cite():
    """Regression guard: a provider that ANSWERED but never cited the merchant
    (no errors, succeeded_runs>0) is a genuine measured 0 — it must keep a real
    score and NOT be hidden as coverage-unavailable."""
    from services.agent_center_bd_report_service import build_citation_by_provider

    answered_no_cite = [{
        "provider": "chatgpt",
        "probe_run_id": "probe-answered",
        "runs_count": 2,
        "usage": {"succeeded_runs": 2, "failed_runs": 0},
        "raw_runs": [
            {
                "query": f"best serum q{idx}",
                "raw": "I recommend a different competitor product.",
                "parsed": {"product_visible": False},
                "grounding_sources": [{"uri": "https://competitor.test/x", "title": "Competitor"}],
                "grounding_chunks": [],
                "url_match": {"in_grounding": False},
                "axis_metadata": {"axis": "intent", "sku_key": "sku-1"},
            }
            for idx in range(2)
        ],
    }]
    cbp = build_citation_by_provider(_base_sku_ctx(), answered_no_cite)
    entry = cbp["chatgpt"]
    assert entry.get("status") != "probe_failed"
    assert not entry.get("coverage_unavailable")
    assert entry["score"] is not None  # a real measured score (0-ish), not None
    assert entry["prompts"] == 2


def test_models_cited_excludes_unavailable_provider():
    from services.agent_center_bd_report_service import (
        _models_cited_for_sku,
        build_citation_by_provider,
    )

    probe_runs = _probe_runs() + _chatgpt_all_429_probe_runs(count=3)
    cbp = build_citation_by_provider(_base_sku_ctx(), probe_runs)
    # of == 1: only Gemini was measured; the 429'd ChatGPT is excluded.
    assert _models_cited_for_sku(cbp)["of"] == 1


def test_brand_rollup_surfaces_coverage_unavailable_provider():
    from services.agent_center_bd_report_service import _brand_citation_by_provider

    per_sku_reports = [
        {
            "sku_key": f"sku-{i}",
            "citation_by_provider": {
                "gemini": {
                    "score": 14,
                    "prompts": 16,
                    "breakdown": {"first_party_rate": {"numerator": 0, "denominator": 16}},
                },
                "chatgpt": {
                    "status": "probe_failed",
                    "coverage_unavailable": True,
                    "error": "429 You exceeded your current quota",
                    "score": None,
                    "prompts": 0,
                },
            },
        }
        for i in range(3)
    ]
    rollup = _brand_citation_by_provider(per_sku_reports)
    # Gemini gets the normal scored rollup shape.
    assert rollup["gemini"]["median"] == 14
    assert rollup["gemini"]["skus_scored"] == 3
    # ChatGPT is surfaced as coverage-unavailable, NOT a median-0 provider.
    chatgpt = rollup["chatgpt"]
    assert chatgpt["status"] == "coverage_unavailable"
    assert chatgpt["skus_unavailable"] == 3
    assert "median" not in chatgpt
    assert "429" in chatgpt["error"]


def test_all_per_sku_probes_failed_catches_wholesale_429():
    from services.audit_run_worker import _all_per_sku_probes_failed

    # Every provider 429'd across every SKU → nothing real to score → fire.
    all_failed = {
        "sku-1": _chatgpt_all_429_probe_runs(count=3),
        "sku-2": _chatgpt_all_429_probe_runs(count=3),
    }
    assert _all_per_sku_probes_failed(all_failed) is True

    # Gemini succeeded on the same SKUs while ChatGPT 429'd → real evidence
    # exists → do NOT fire (the run finalizes on Gemini's data).
    mixed = {
        "sku-1": _probe_runs() + _chatgpt_all_429_probe_runs(count=3),
    }
    assert _all_per_sku_probes_failed(mixed) is False

    # A fully successful run never fires.
    assert _all_per_sku_probes_failed({"sku-1": _probe_runs()}) is False


# ---------------------------------------------------------------------
# _failing_prompts own-brand competitor filter (#1384 follow-up)
#
# competitors_named on failing prompts feeds rival-framing copy
# (audit_playbook pitch drafts, next_best_action competitor phrases via
# failed_queries_detailed). The merchant's own brand/aliases must never
# surface there as a named "competitor".
# ---------------------------------------------------------------------


def _failing_run(query: str, competitors: List[str]) -> Dict[str, Any]:
    # A run that is NOT cited (no correct_sku / sku_mentioned / grounding /
    # product_visible) so _failing_prompts keeps it, carrying competitors.
    return {
        "query": query,
        "parsed": {
            "correct_sku": False,
            "sku_mentioned": False,
            "competitors_appearing": list(competitors),
        },
        "url_match": {"in_grounding": False},
    }


def test_failing_prompts_strips_own_brand_and_aliases():
    from services.agent_center_bd_report_service import _failing_prompts
    from services.brand_alias import derive_brand_aliases

    aliases = derive_brand_aliases("BB Lab Global", "bblabglobal.com", None)
    runs = [
        # "BB Lab Global" (word-boundary own-brand), de-spaced alias "BBLab"
        # (alias-only — not a substring of the brand), and a genuine rival.
        _failing_run("best korean collagen serum", ["BB Lab Global", "BBLab", "Torriden"]),
    ]
    out = _failing_prompts(
        runs,
        brand_lower="bb lab global",
        brand_aliases=aliases,
    )
    assert len(out) == 1
    named = out[0]["competitors_named"]
    assert "BB Lab Global" not in named
    assert "BBLab" not in named
    assert named == ["Torriden"]


def test_failing_prompts_without_identity_keeps_list_unfiltered():
    # Back-compat: no brand identity supplied => no own-brand stripping, list
    # passes through as before (the pre-fix behavior).
    from services.agent_center_bd_report_service import _failing_prompts

    runs = [_failing_run("q1", ["BB Lab Global", "Torriden"])]
    out = _failing_prompts(runs)
    assert out[0]["competitors_named"] == ["BB Lab Global", "Torriden"]


def test_strip_own_brand_competitors_helper_matrix():
    from services.agent_center_bd_report_service import _strip_own_brand_competitors
    from services.brand_alias import derive_brand_aliases

    aliases = derive_brand_aliases("BB Lab Global", "bblabglobal.com", ("Anua",))
    names = ["BB Lab Global Inc", "BBLab", "Torriden", "", 123, "Anua"]
    out = _strip_own_brand_competitors(names, "bb lab global", aliases)
    # "BB Lab Global Inc" (word-boundary brand), "BBLab" (de-spaced alias) and
    # "Anua" (vendor alias) dropped; "" and non-str 123 skipped; order kept.
    assert out == ["Torriden"]

    # Word-boundary guard: a brand (len>=4) must not erase a genuine rival that
    # merely STARTS with the brand's letters as part of a larger word — the
    # plain substring the run-brand tally uses would over-strip "Glowbiotics"
    # for brand "Glow"; the word boundary keeps it.
    kept = _strip_own_brand_competitors(["Glowbiotics"], "glow", ())
    assert kept == ["Glowbiotics"]
