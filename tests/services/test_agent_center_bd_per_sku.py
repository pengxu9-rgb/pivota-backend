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
        "merchant_commerce_readiness_state": {"active_psp": "stripe"},
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
    assert 0 <= score < 60
    assert breakdown["product_quality_score"]["reason"] == "data unavailable"
    assert breakdown["safety_claims"]["points"] == 0


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


def test_citation_score_weighted_formula_and_missing_runs():
    from services.agent_center_bd_report_service import compute_citation_score

    score, breakdown = compute_citation_score(_base_sku_ctx(), _probe_runs())
    assert score == 50
    assert breakdown["first_party_rate"]["numerator"] == 1
    assert breakdown["first_party_rate"]["denominator"] == 2
    assert breakdown["answer_quality_rate"]["points"] == 5

    score, breakdown = compute_citation_score(_base_sku_ctx(), [])
    assert score == 0
    assert breakdown["first_party_rate"]["reason"] == "data unavailable"


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
        },
    ]
    rollup = build_brand_rollup(reports, "m-1")
    assert rollup["dimensions"]["content_richness"]["median"] == 60
    assert rollup["priority_queue"][0]["sku_key"] == "sku-a"
    assert rollup["priority_queue"][0]["priority_score"] == 600
    assert rollup["blocked_skus"][0]["sku_key"] == "sku-a"


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
