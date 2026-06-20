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
    assert score == 50
    assert breakdown["first_party_rate"]["numerator"] == 1
    assert breakdown["first_party_rate"]["denominator"] == 2
    assert breakdown["answer_quality_rate"]["points"] == 5

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


@pytest.mark.asyncio
async def test_build_per_sku_report_end_to_end_with_mocked_loaders(monkeypatch):
    from services import agent_center_bd_report_service as bd

    async def fake_load_sku_context(sku_key: str, merchant_id: str) -> Dict[str, Any]:
        ctx = _base_sku_ctx()
        ctx["sku_key"] = sku_key
        ctx["merchant_id"] = merchant_id
        return ctx

    async def fake_load_runs(sku_key: str, merchant_id: str, audit_run_id: str) -> List[Dict[str, Any]]:
        return _multi_provider_probe_runs()

    monkeypatch.setattr(bd, "load_sku_context", fake_load_sku_context)
    monkeypatch.setattr(bd, "load_per_sku_probe_runs", fake_load_runs)

    report = await bd.build_per_sku_report("sku-1", "m-1", "audit-1")
    assert report["sku_key"] == "sku-1"
    assert set(report["scores"]) == {"identity", "content_richness", "routability", "citation"}
    assert report["scores"]["citation"]["score"] == 78
    assert report["scores"]["citation"]["breakdown"]["first_party_rate"]["numerator"] == 1
    assert report["scores"]["citation"]["breakdown"]["sku_mention_rate"]["numerator"] == 2
    assert report["scores"]["citation"]["breakdown"]["aggregation_rule"].startswith("any_profile_provider")
    assert set(report["citation_by_provider"]) == {"gemini", "chatgpt"}
    assert report["citation_by_provider"]["gemini"]["score"] == 50
    assert report["citation_by_provider"]["chatgpt"]["score"] == 50
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

    async def fake_load_runs(sku_key: str, merchant_id: str, audit_run_id: str) -> List[Dict[str, Any]]:
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
    assert citation["breakdown"]["answer_quality_rate"]["points"] == 8


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

    async def fake_load_runs(sku_key: str, merchant_id: str, audit_run_id: str) -> List[Dict[str, Any]]:
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
    assert sku_report["scores"]["citation"]["score"] == 100


@pytest.mark.asyncio
async def test_per_sku_report_and_cost_summary_stamp_model_override(monkeypatch):
    from services import agent_center_bd_report_service as bd

    async def fake_load_sku_context(sku_key: str, merchant_id: str) -> Dict[str, Any]:
        ctx = _base_sku_ctx()
        ctx["sku_key"] = sku_key
        ctx["merchant_id"] = merchant_id
        return ctx

    async def fake_load_runs(sku_key: str, merchant_id: str, audit_run_id: str) -> List[Dict[str, Any]]:
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
