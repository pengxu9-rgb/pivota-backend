"""E1 — CanonicalPdpEnrichmentAgent: should_run gating + execute result shapes.

Mocks the Gemini call, the candidate fetch, and the persistence/publish calls
(upsert_enrichment + refresh_agent_pdp_view_for_content_key) so the test is
pure-logic — no DB, no real LLM. The compliance guard runs for real (it's a
pure keyword check) so the block path is exercised end-to-end.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from services.executor_agents.base import ExecutorContext
from services.executor_agents.canonical_pdp_enrichment import (
    CanonicalPdpEnrichmentAgent,
    _audit_flagged_intents_by_product_key,
    _audit_thin_product_keys,
    _build_enrichment_prompt,
    _factual_gate_enabled,
    _generated_claim_text,
    _grounding_facts_for_candidate,
    _grounding_facts_text,
    _resolve_candidates,
    _source_ids_from_product_keys,
    _verify_enrichment_grounding,
)

_MOD = "services.executor_agents.canonical_pdp_enrichment"


def _candidate(**overrides) -> Dict[str, Any]:
    base = {
        "merchant_id": "m1",
        "platform": "shopify",
        "source_product_id": "sp-1",
        "content_key": "ck-1",
        "title": "Good Night Collagen, 30 sticks",
        "description": "thin",
        "brand": "BB Lab",
        "product_type": "supplement",
        "category": "health",
    }
    base.update(overrides)
    return base


def _enrichment(**overrides) -> Dict[str, Any]:
    base = {
        "description_markdown": (
            "Low-molecular-weight collagen in single-serve sticks. Each box has "
            "30 sticks designed for daily use. Easy to take on the go; mixes into "
            "water or your morning drink. Made in Korea; halal-certified per the "
            "manufacturer. Suitable for adults looking to add collagen to a daily "
            "routine."
        ),
        "summary_short": "Low-molecular collagen, 30 single-serve sticks.",
        "bullet_points": ["30 sticks per box", "Single-serve", "Halal-certified"],
        "usage_scenarios": ["Daily morning routine", "On-the-go travel"],
        "audience_tags": ["adults", "collagen users"],
        "title_override": "Good Night Collagen (Low-Molecular), 30 Sticks",
    }
    base.update(overrides)
    return base


def _pass_verdict() -> Dict[str, Any]:
    """A factual-gate verdict that PASSES (copy is grounded) — lets the
    publish-path tests through the now-default-ON gate."""
    return {
        "passed": True,
        "reason": "grounded",
        "misstates_facts": False,
        "supports_recommendation": True,
        "note": "",
    }


# ---------------------------------------------------------------------------
# should_run gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_run_false_without_merchant_id():
    agent = CanonicalPdpEnrichmentAgent()
    assert await agent.should_run(ExecutorContext(merchant_id=None)) is False


@pytest.mark.asyncio
async def test_should_run_false_without_gemini_key():
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value=None):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is False


@pytest.mark.asyncio
async def test_should_run_false_when_no_candidates():
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=AsyncMock(return_value=_pass_verdict())), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[])):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is False


@pytest.mark.asyncio
async def test_should_run_true_when_candidates_exist():
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=AsyncMock(return_value=_pass_verdict())), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is True


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_skipped_without_key():
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value=None):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "skipped"


@pytest.mark.asyncio
async def test_execute_skipped_no_candidates():
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=AsyncMock(return_value=_pass_verdict())), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[])):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "skipped"
    assert result.evidence["reason"] == "no thin canonical PDPs to enrich"


@pytest.mark.asyncio
async def test_execute_enriches_and_publishes():
    """Happy path: generate → compliance OK → upsert → refresh (publish)."""
    agent = CanonicalPdpEnrichmentAgent()
    upsert = AsyncMock()
    refresh = AsyncMock(return_value=True)
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=AsyncMock(return_value=_pass_verdict())), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=_enrichment())), \
         patch("db.product_enrichment.upsert_enrichment", new=upsert), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=refresh):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))

    assert result.status == "succeeded"
    assert result.evidence["enriched_count"] == 1
    assert result.evidence["blocked_count"] == 0
    assert result.evidence["enriched"][0]["published"] is True
    upsert.assert_awaited_once()
    upsert_data = upsert.await_args.args[4]
    assert "Low-molecular-weight collagen" in upsert_data["description_markdown"]
    refresh.assert_awaited_once()
    assert refresh.await_args.args[0] == "ck-1"


@pytest.mark.asyncio
async def test_execute_blocks_noncompliant_copy():
    """A risky-keyword description is blocked by the compliance guard — never
    persisted or published."""
    agent = CanonicalPdpEnrichmentAgent()
    bad = _enrichment(
        description_markdown=(
            "This supplement offers a guaranteed return on your health and will "
            "cure fatigue for everyone who takes it daily over the long term, no "
            "exceptions, with results that compound month over month for years."
        )
    )
    upsert = AsyncMock()
    refresh = AsyncMock(return_value=True)
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=AsyncMock(return_value=_pass_verdict())), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=bad)), \
         patch("db.product_enrichment.upsert_enrichment", new=upsert), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=refresh):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))

    assert result.status == "failed"  # nothing enriched
    assert result.evidence["blocked_count"] == 1
    assert result.evidence["enriched_count"] == 0
    upsert.assert_not_awaited()
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_failed_generation_counts_but_does_not_crash():
    """A None generation (LLM failed/unparseable) lands in `failed`, not a crash."""
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=AsyncMock(return_value=_pass_verdict())), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=None)):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "failed"
    assert result.evidence["failed_count"] == 1
    assert result.evidence["enriched_count"] == 0


def test_agent_registered_in_dispatcher_and_worker():
    """Guard the two-place registration hazard: the agent must be in BOTH the
    dispatcher registry (which enqueues) and the worker registry (which
    executes) — or enqueued rows hit `unknown_agent` and never run."""
    from services.executor_agents.dispatcher import _registry
    from services.executor_run_worker import _agent_registry_by_name

    name = CanonicalPdpEnrichmentAgent().name
    assert name in {a.name for a in _registry()}
    assert name in _agent_registry_by_name()


# ---------------------------------------------------------------------------
# audit-driven candidate selection (the fix: enrich what the audit flagged)
# ---------------------------------------------------------------------------


def test_audit_thin_product_keys_extracts_flagged_canonical_skus():
    # Keys off product_key (not content_key) — content_key can be null/mismatched
    # and silently excluded SKUs from enrichment (the live-caught bug).
    report = {
        "per_sku_reports": [
            # thin (52 < 70) + has canonical PDP -> included
            {"product_key": "m1|shopify|pk-thin", "indexing_arc": {"phase": "fresh"},
             "scores": {"content_richness": {"score": 52}}},
            # not thin (80 >= 70) -> excluded
            {"product_key": "m1|shopify|pk-ready", "indexing_arc": {"phase": "fresh"},
             "scores": {"content_richness": {"score": 80}}},
            # thin but NO canonical PDP -> excluded (E1 can't enrich it)
            {"product_key": "m1|shopify|pk-nopdp", "indexing_arc": None,
             "scores": {"content_richness": {"score": 30}}},
            # null content_key but valid product_key -> STILL included (the fix)
            {"product_key": "m1|shopify|pk-nullck", "content_key": None,
             "indexing_arc": {"phase": "fresh"},
             "scores": {"content_richness": {"score": 40}}},
            # duplicate product_key -> deduped
            {"product_key": "m1|shopify|pk-thin", "indexing_arc": {"phase": "fresh"},
             "scores": {"content_richness": {"score": 20}}},
        ]
    }
    assert _audit_thin_product_keys(report) == [
        "m1|shopify|pk-thin", "m1|shopify|pk-nullck",
    ]
    # defensive: None / unparseable string -> []
    assert _audit_thin_product_keys(None) == []
    assert _audit_thin_product_keys("{not json") == []


def test_source_ids_from_product_keys():
    # Extracts the source_product_id (3rd segment); dedups; skips malformed.
    assert _source_ids_from_product_keys([
        "m1|shopify|10100856914217",
        "m1|shopify|10100856914217",  # dup -> deduped
        "m1|shopify|sp-2",
        "bad-key",                      # no pipes -> skipped
        "m1|shopify|",                  # empty source id -> skipped
        "a|b|c|d",                       # too many segments -> skipped
        "",
    ]) == ["10100856914217", "sp-2"]
    assert _source_ids_from_product_keys([]) == []


@pytest.mark.asyncio
async def test_resolve_candidates_prioritizes_audit_then_falls_back():
    ctx = ExecutorContext(merchant_id="m1", audit_report={"per_sku_reports": [
        {"product_key": "m1|shopify|sp-audit", "indexing_arc": {"x": 1},
         "scores": {"content_richness": {"score": 50}}},
    ]})
    audit_cand = _candidate(source_product_id="sp-audit")
    catalog_cand = _candidate(source_product_id="sp-catalog")
    with patch(f"{_MOD}._fetch_canonical_pdps_by_product_keys", new=AsyncMock(return_value=[audit_cand])), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[catalog_cand])):
        out = await _resolve_candidates(ctx, cap=5)
    pairs = [(c["source_product_id"], c["_candidate_source"]) for c in out]
    assert pairs[0] == ("sp-audit", "audit")  # audit-flagged SKU first
    assert ("sp-catalog", "catalog") in pairs  # then the catalog fallback fills


@pytest.mark.asyncio
async def test_resolve_candidates_catalog_only_without_audit_report():
    ctx = ExecutorContext(merchant_id="m1")  # no audit_report
    catalog_cand = _candidate(source_product_id="sp-catalog")
    with patch(f"{_MOD}._fetch_canonical_pdps_by_product_keys", new=AsyncMock(return_value=[])), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[catalog_cand])):
        out = await _resolve_candidates(ctx, cap=5)
    assert len(out) == 1 and out[0]["_candidate_source"] == "catalog"


@pytest.mark.asyncio
async def test_execute_enriches_the_audit_flagged_sku():
    """The fix end-to-end: an audit-flagged thin canonical SKU gets enriched +
    tagged source=audit (not a blind newest-5 catalog pick)."""
    agent = CanonicalPdpEnrichmentAgent()
    ctx = ExecutorContext(merchant_id="m1", audit_report={"per_sku_reports": [
        {"product_key": "m1|shopify|sp-1", "indexing_arc": {"x": 1},
         "scores": {"content_richness": {"score": 50}}},
    ]})
    upsert = AsyncMock()
    refresh = AsyncMock(return_value=True)
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=AsyncMock(return_value=_pass_verdict())), \
         patch(f"{_MOD}._fetch_canonical_pdps_by_product_keys", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=_enrichment())), \
         patch("db.product_enrichment.upsert_enrichment", new=upsert), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=refresh):
        result = await agent.execute(ctx)
    assert result.status == "succeeded"
    assert result.evidence["enriched_count"] == 1
    assert result.evidence["audit_driven_count"] == 1
    assert result.evidence["enriched"][0]["source"] == "audit"
    upsert.assert_awaited_once()


# ---------------------------------------------------------------------------
# flagged-intent -> enrichment brief (win the queries AI mentions you on but
# recommends competitors for)
# ---------------------------------------------------------------------------


def test_audit_flagged_intents_extracts_only_lost_rec():
    """product_key -> the 'mention, no rec' intents (supports_recommendation is
    exactly False). Excludes supports-True (incl. misstates-only-but-supported),
    empty-query, and SKUs with no flagged probes; dedups case-insensitively."""
    report = {
        "per_sku_reports": [
            {"product_key": "m1|shopify|a", "verify_summary": {"flagged_probes": [
                {"query": "halal collagen", "supports_recommendation": False,
                 "misstates_facts": False},
                # supported -> not a rec gap, excluded (even if misstates a fact)
                {"query": "collagen dosage", "supports_recommendation": True,
                 "misstates_facts": True},
                # missing/None supports -> skipped/unparsed verdict, not a gap
                {"query": "best collagen", "misstates_facts": False},
                # duplicate (case-insensitive) -> deduped
                {"query": "Halal Collagen", "supports_recommendation": False},
                # empty query -> dropped
                {"query": "   ", "supports_recommendation": False},
            ]}},
            {"product_key": "m1|shopify|b", "verify_summary": {"flagged_probes": [
                {"query": "marine collagen", "supports_recommendation": False}]}},
            {"product_key": "m1|shopify|c", "verify_summary": {"flagged_probes": []}},
            {"product_key": "m1|shopify|d"},  # no verify_summary at all
        ]
    }
    assert _audit_flagged_intents_by_product_key(report) == {
        "m1|shopify|a": ["halal collagen"],
        "m1|shopify|b": ["marine collagen"],
    }
    assert _audit_flagged_intents_by_product_key(None) == {}
    assert _audit_flagged_intents_by_product_key("{bad json") == {}


def test_flagged_intents_read_raw_verify_outputs_post_factual_gate():
    """The flag gate went factual-only (2026-07-16): editorial-only verdicts
    (supports=False, misstates=False) — this lane's PRIMARY target — no longer
    enter flagged_probes on new runs. The executor must read the raw
    verify_outputs (verdict-nested shape) so its targeting doesn't silently
    starve; flagged_probes stays as the legacy-report fallback (flat shape,
    covered by the test above)."""
    report = {
        "per_sku_reports": [
            {
                "product_key": "m1|shopify|a",
                # New-run shape: full outputs, verdict nested, editorial-only
                # entry present even though flagged_probes is EMPTY.
                "verify_outputs": [
                    {"query": "halal collagen",
                     "verdict": {"supports_recommendation": False,
                                 "misstates_facts": False}},
                    {"query": "collagen dosage",
                     "verdict": {"supports_recommendation": True,
                                 "misstates_facts": True}},
                ],
                "verify_summary": {"flagged_probes": []},
            },
        ]
    }
    assert _audit_flagged_intents_by_product_key(report) == {
        "m1|shopify|a": ["halal collagen"],
    }


@pytest.mark.asyncio
async def test_resolve_candidates_attaches_target_intents_no_leak():
    """Each candidate carries ITS sku's lost-rec intents (joined by product_key,
    reconstructed from the candidate's identity); a different SKU's intents don't
    leak; the catalog-fallback candidate gets []."""
    ctx = ExecutorContext(merchant_id="m1", audit_report={"per_sku_reports": [
        {"product_key": "m1|shopify|sp-audit", "indexing_arc": {"x": 1},
         "scores": {"content_richness": {"score": 50}},
         "verify_summary": {"flagged_probes": [
             {"query": "halal collagen", "supports_recommendation": False}]}},
    ]})
    audit_cand = _candidate(source_product_id="sp-audit")
    catalog_cand = _candidate(source_product_id="sp-catalog")
    with patch(f"{_MOD}._fetch_canonical_pdps_by_product_keys", new=AsyncMock(return_value=[audit_cand])), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[catalog_cand])):
        out = await _resolve_candidates(ctx, cap=5)
    by_sp = {c["source_product_id"]: c for c in out}
    assert by_sp["sp-audit"]["_target_intents"] == ["halal collagen"]
    assert by_sp["sp-catalog"]["_target_intents"] == []  # sp-catalog not audited -> no leak


def test_build_enrichment_prompt_injects_intents():
    base = _candidate(content_key="ck-1")
    # no intents -> no targeting block
    p0 = _build_enrichment_prompt({**base, "_target_intents": []})
    assert "recommend COMPETITORS" not in p0
    # with intents -> block + each intent listed, truthfulness guard intact
    p1 = _build_enrichment_prompt(
        {**base, "_target_intents": ["halal collagen", "best collagen for travel"]}
    )
    assert "recommend COMPETITORS" in p1
    assert '"halal collagen"' in p1 and '"best collagen for travel"' in p1
    assert "never invent" in p1  # the in-block guard
    assert "If you cannot verify a fact, omit it." in p1  # the standing rule


@pytest.mark.asyncio
async def test_execute_records_targeted_intents():
    """Evidence carries the per-SKU targeted intents + the count, so the
    measure -> enrich -> re-measure loop is auditable."""
    agent = CanonicalPdpEnrichmentAgent()
    ctx = ExecutorContext(merchant_id="m1", audit_report={"per_sku_reports": [
        {"product_key": "m1|shopify|sp-1", "indexing_arc": {"x": 1},
         "scores": {"content_richness": {"score": 50}},
         "verify_summary": {"flagged_probes": [
             {"query": "halal collagen", "supports_recommendation": False}]}},
    ]})
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=AsyncMock(return_value=_pass_verdict())), \
         patch(f"{_MOD}._fetch_canonical_pdps_by_product_keys", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=_enrichment())), \
         patch("db.product_enrichment.upsert_enrichment", new=AsyncMock()), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=AsyncMock(return_value=True)):
        result = await agent.execute(ctx)
    assert result.status == "succeeded"
    assert result.evidence["intent_targeted_count"] == 1
    assert result.evidence["enriched"][0]["targeted_intents"] == ["halal collagen"]


@pytest.mark.asyncio
async def test_execute_scores_and_recomputes_serving_eligibility():
    """Phase A (R1/R2/R6): after enriching, E1 writes a fresh quality snapshot
    (full_quality_eval) AND recomputes serving_eligibility — so the enrichment can
    actually flip serving and reach agents, instead of sitting behind a stale/missing
    score. Records serving_eligible in evidence."""
    agent = CanonicalPdpEnrichmentAgent()
    ctx = ExecutorContext(merchant_id="m1", audit_report={"per_sku_reports": [
        {"product_key": "m1|shopify|sp-1", "indexing_arc": {"x": 1},
         "scores": {"content_richness": {"score": 50}}},
    ]})
    full_eval = AsyncMock(return_value={"content_quality_score": 71.2})
    recompute = AsyncMock(return_value=True)
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=AsyncMock(return_value=_pass_verdict())), \
         patch(f"{_MOD}._fetch_canonical_pdps_by_product_keys", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=_enrichment())), \
         patch("db.product_enrichment.upsert_enrichment", new=AsyncMock()), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=AsyncMock(return_value=True)), \
         patch("db.products.get_product_cache_row", new=AsyncMock(return_value=None)), \
         patch("services.product_quality_service.full_quality_eval", new=full_eval), \
         patch("services.index_pipeline_state_service.recompute_serving_eligibility", new=recompute):
        result = await agent.execute(ctx)
    assert result.status == "succeeded"
    # Scored + recomputed for the enriched SKU.
    full_eval.assert_awaited_once()
    assert full_eval.await_args.kwargs["platform_product_id"] == "sp-1"
    assert full_eval.await_args.kwargs["geo_code"] == "default"
    recompute.assert_awaited_once()
    assert recompute.await_args.args[0] == "ck-1"  # the candidate's content_key
    # serving_eligible surfaced in evidence (per-SKU + count).
    assert result.evidence["enriched"][0]["serving_eligible"] is True
    assert result.evidence["serving_eligible_count"] == 1


@pytest.mark.asyncio
async def test_execute_phase_a_failure_is_best_effort():
    """If scoring/recompute fails, enrichment + publish still count (best-effort):
    serving_eligible just records None — Phase A never sinks the enrichment."""
    agent = CanonicalPdpEnrichmentAgent()
    ctx = ExecutorContext(merchant_id="m1", audit_report={"per_sku_reports": [
        {"product_key": "m1|shopify|sp-1", "indexing_arc": {"x": 1},
         "scores": {"content_richness": {"score": 50}}},
    ]})
    boom = AsyncMock(side_effect=RuntimeError("quality service down"))
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=AsyncMock(return_value=_pass_verdict())), \
         patch(f"{_MOD}._fetch_canonical_pdps_by_product_keys", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=_enrichment())), \
         patch("db.product_enrichment.upsert_enrichment", new=AsyncMock()), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=AsyncMock(return_value=True)), \
         patch("db.products.get_product_cache_row", new=AsyncMock(return_value=None)), \
         patch("services.product_quality_service.full_quality_eval", new=boom):
        result = await agent.execute(ctx)
    assert result.status == "succeeded"
    assert result.evidence["enriched_count"] == 1
    assert result.evidence["enriched"][0]["serving_eligible"] is None
    assert result.evidence["serving_eligible_count"] == 0


# ---------------------------------------------------------------------------
# R4 — generation reliability (retry + token budget)
# ---------------------------------------------------------------------------


def _gemini_ok_json():
    import json as _j
    return {"candidates": [{"content": {"parts": [{"text": _j.dumps({
        "description_markdown": "A factual, specific product description. " * 8,
        "summary_short": "One factual sentence.",
        "bullet_points": ["a", "b", "c"],
    })}]}}]}


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p


@pytest.mark.asyncio
async def test_generate_enrichment_retries_then_succeeds(monkeypatch):
    """A first-attempt failure (the ~55% case) is retried, not given up on."""
    from services.executor_agents import canonical_pdp_enrichment as mod
    calls = {"n": 0, "bodies": []}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            calls["n"] += 1
            calls["bodies"].append(json)
            if calls["n"] == 1:
                return _FakeResp(500, {})          # transient fail
            return _FakeResp(200, _gemini_ok_json())  # then succeed

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)
    out = await mod._generate_enrichment(_candidate(), "k", timeout_s=1, max_attempts=3)
    assert out is not None and out["description_markdown"]
    assert calls["n"] == 2  # retried once, then succeeded
    # raised output cap (vs the old 2048 that truncated grounded JSON)
    assert calls["bodies"][0]["generationConfig"]["maxOutputTokens"] == mod._GEMINI_MAX_OUTPUT_TOKENS


@pytest.mark.asyncio
async def test_generate_enrichment_gives_up_after_max_attempts(monkeypatch):
    from services.executor_agents import canonical_pdp_enrichment as mod
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            calls["n"] += 1
            return _FakeResp(500, {})

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)
    out = await mod._generate_enrichment(_candidate(), "k", timeout_s=1, max_attempts=3)
    assert out is None
    assert calls["n"] == 3  # exhausted all attempts


# ---------------------------------------------------------------------------
# Factual grounding gate (SAFETY) — fact-check generated copy against the
# product's grounding source before publishing; fail CLOSED.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factual_gate_grounded_passes_and_publishes(monkeypatch):
    """Gate ON + a 'grounded' verdict -> copy is persisted + published."""
    monkeypatch.delenv("E1_FACTUAL_GATE_ENABLED", raising=False)
    agent = CanonicalPdpEnrichmentAgent()
    upsert = AsyncMock()
    refresh = AsyncMock(return_value=True)
    verify = AsyncMock(return_value=_pass_verdict())
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=_enrichment())), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=verify), \
         patch("db.product_enrichment.upsert_enrichment", new=upsert), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=refresh):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "succeeded"
    assert result.evidence["enriched_count"] == 1
    assert result.evidence["factual_gate_enabled"] is True
    assert result.evidence["factual_gate_blocked_count"] == 0
    verify.assert_awaited_once()
    upsert.assert_awaited_once()
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_factual_gate_unsupported_blocks_publish(monkeypatch):
    """misstates_facts=True -> NOT persisted/published; recorded as a
    factual_grounding block; status failed (nothing enriched)."""
    monkeypatch.delenv("E1_FACTUAL_GATE_ENABLED", raising=False)
    agent = CanonicalPdpEnrichmentAgent()
    upsert = AsyncMock()
    refresh = AsyncMock(return_value=True)
    verify = AsyncMock(return_value={
        "passed": False, "reason": "misstates_facts", "misstates_facts": True,
        "supports_recommendation": True, "note": "invented a certification",
    })
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=_enrichment())), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=verify), \
         patch("db.product_enrichment.upsert_enrichment", new=upsert), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=refresh):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "failed"  # nothing enriched
    assert result.evidence["enriched_count"] == 0
    assert result.evidence["blocked_count"] == 1
    assert result.evidence["factual_gate_blocked_count"] == 1
    assert result.evidence["blocked"][0]["gate"] == "factual_grounding"
    assert result.evidence["blocked"][0]["reason"] == "misstates_facts"
    upsert.assert_not_awaited()
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_factual_gate_verify_error_fails_closed(monkeypatch):
    """A verify-call error resolves to a fail-CLOSED block — unverifiable copy is
    never published."""
    monkeypatch.delenv("E1_FACTUAL_GATE_ENABLED", raising=False)
    agent = CanonicalPdpEnrichmentAgent()
    upsert = AsyncMock()
    refresh = AsyncMock(return_value=True)
    verify = AsyncMock(return_value={
        "passed": False, "reason": "verify_error", "misstates_facts": None,
        "supports_recommendation": None, "note": "boom",
    })
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=_enrichment())), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=verify), \
         patch("db.product_enrichment.upsert_enrichment", new=upsert), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=refresh):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.evidence["enriched_count"] == 0
    assert result.evidence["factual_gate_blocked_count"] == 1
    assert result.evidence["blocked"][0]["reason"] == "verify_error"
    upsert.assert_not_awaited()
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_factual_gate_disabled_skips_check(monkeypatch):
    """E1_FACTUAL_GATE_ENABLED=0 -> gate skipped (verify never called); reverts to
    compliance-only; copy publishes."""
    monkeypatch.setenv("E1_FACTUAL_GATE_ENABLED", "0")
    agent = CanonicalPdpEnrichmentAgent()
    upsert = AsyncMock()
    refresh = AsyncMock(return_value=True)
    verify = AsyncMock(return_value=_pass_verdict())
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=_enrichment())), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=verify), \
         patch("db.product_enrichment.upsert_enrichment", new=upsert), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=refresh):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "succeeded"
    assert result.evidence["enriched_count"] == 1
    assert result.evidence["factual_gate_enabled"] is False
    verify.assert_not_awaited()  # gate off -> never called
    upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_enrichment_grounding_missing_key_fails_closed():
    """No DeepSeek key configured -> fail-closed (passed=False), no network call."""
    with patch("config.settings.settings.deepseek_api_key", None):
        out = await _verify_enrichment_grounding(
            _enrichment(),
            _grounding_facts_for_candidate(_candidate()),
            merchant_id="m1",
        )
    assert out["passed"] is False
    assert out["reason"] == "missing_deepseek_api_key"


@pytest.mark.asyncio
async def test_verify_enrichment_grounding_passes_through_verdict():
    """With a key + a parseable verdict: misstates_facts False -> passed; True ->
    blocked. Exercises the probe + _extract_verify_verdict integration on a
    realistic raw_runs shape."""
    def _result(misstates: bool) -> Dict[str, Any]:
        return {"raw_runs": [{"parsed": {
            "supports_recommendation": True, "misstates_facts": misstates, "note": "ok"}}]}
    grounding = _grounding_facts_for_candidate(_candidate())
    with patch("config.settings.settings.deepseek_api_key", "k"), \
         patch("services.agent_center_llm_client.probe",
               new=AsyncMock(return_value=_result(False))):
        ok = await _verify_enrichment_grounding(_enrichment(), grounding, merchant_id="m1")
    assert ok["passed"] is True and ok["reason"] == "grounded"
    with patch("config.settings.settings.deepseek_api_key", "k"), \
         patch("services.agent_center_llm_client.probe",
               new=AsyncMock(return_value=_result(True))):
        bad = await _verify_enrichment_grounding(_enrichment(), grounding, merchant_id="m1")
    assert bad["passed"] is False and bad["reason"] == "misstates_facts"


def test_generated_claim_text_and_grounding_facts_text():
    """The input-mapping helpers: claim text concatenates the claim-bearing fields
    (truncated 4000); facts text emits Title/Brand/Category/Original-desc (2000)."""
    claim = _generated_claim_text(_enrichment())
    assert "Good Night Collagen" in claim          # title_override
    assert "Low-molecular-weight collagen" in claim  # description
    assert "Halal-certified" in claim               # a bullet
    assert len(claim) <= 4000
    facts = _grounding_facts_text(_grounding_facts_for_candidate(_candidate()))
    assert "Title: Good Night Collagen, 30 sticks" in facts
    assert "Brand: BB Lab" in facts
    assert "Original description: thin" in facts
    assert len(facts) <= 2000


def test_factual_gate_enabled_default_on_and_explicit_off(monkeypatch):
    monkeypatch.delenv("E1_FACTUAL_GATE_ENABLED", raising=False)
    assert _factual_gate_enabled() is True          # default ON (safety)
    monkeypatch.setenv("E1_FACTUAL_GATE_ENABLED", "0")
    assert _factual_gate_enabled() is False
    monkeypatch.setenv("E1_FACTUAL_GATE_ENABLED", "off")
    assert _factual_gate_enabled() is False


@pytest.mark.asyncio
async def test_factual_gate_helper_raise_fails_closed(monkeypatch):
    """If the verify helper itself RAISES, execute() catches it and fails CLOSED
    (blocks) — a gate error can never abort the batch or publish unverified copy."""
    monkeypatch.delenv("E1_FACTUAL_GATE_ENABLED", raising=False)
    agent = CanonicalPdpEnrichmentAgent()
    upsert = AsyncMock()
    refresh = AsyncMock(return_value=True)
    verify = AsyncMock(side_effect=RuntimeError("unexpected"))
    with patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=_enrichment())), \
         patch(f"{_MOD}._verify_enrichment_grounding", new=verify), \
         patch("db.product_enrichment.upsert_enrichment", new=upsert), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=refresh):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.evidence["enriched_count"] == 0
    assert result.evidence["factual_gate_blocked_count"] == 1
    assert result.evidence["blocked"][0]["reason"] == "gate_error"
    assert result.evidence["blocked"][0]["gate"] == "factual_grounding"
    upsert.assert_not_awaited()
    refresh.assert_not_awaited()


# --- helper-level fail-closed branches (the real probe/verdict mapping) -----


@pytest.mark.asyncio
async def test_verify_enrichment_grounding_unparseable_fails_closed():
    """A verdict that can't be parsed (missing/non-bool fields, empty/absent
    raw_runs) -> verify_unparseable block."""
    grounding = _grounding_facts_for_candidate(_candidate())
    for bad in ({"raw_runs": [{"parsed": {"note": "x"}}]}, {"raw_runs": []}, {}):
        with patch("config.settings.settings.deepseek_api_key", "k"), \
             patch("services.agent_center_llm_client.probe",
                   new=AsyncMock(return_value=bad)):
            out = await _verify_enrichment_grounding(
                _enrichment(), grounding, merchant_id="m1")
        assert out["passed"] is False
        assert out["reason"] == "verify_unparseable"


@pytest.mark.asyncio
async def test_verify_enrichment_grounding_insufficient_grounding_short_circuits():
    """Empty grounding (no title/brand/category/desc) -> insufficient_grounding
    BEFORE any network call (no probe, no metered cost)."""
    probe = AsyncMock()
    empty = _grounding_facts_for_candidate(
        _candidate(title="", brand="", category="", product_type="", description=""))
    with patch("config.settings.settings.deepseek_api_key", "k"), \
         patch("services.agent_center_llm_client.probe", new=probe):
        out = await _verify_enrichment_grounding(_enrichment(), empty, merchant_id="m1")
    assert out["passed"] is False
    assert out["reason"] == "insufficient_grounding"
    probe.assert_not_awaited()  # short-circuits before the metered call


@pytest.mark.asyncio
async def test_verify_enrichment_grounding_timeout_fails_closed():
    """A probe timeout -> verify_timeout block (the most operationally likely
    failure: DeepSeek slow)."""
    grounding = _grounding_facts_for_candidate(_candidate())
    with patch("config.settings.settings.deepseek_api_key", "k"), \
         patch("services.agent_center_llm_client.probe",
               new=AsyncMock(side_effect=asyncio.TimeoutError())):
        out = await _verify_enrichment_grounding(_enrichment(), grounding, merchant_id="m1")
    assert out["passed"] is False
    assert out["reason"] == "verify_timeout"


@pytest.mark.asyncio
async def test_verify_enrichment_grounding_probe_error_fails_closed():
    """A probe exception (4xx/5xx/network) -> verify_error block, error in note."""
    grounding = _grounding_facts_for_candidate(_candidate())
    with patch("config.settings.settings.deepseek_api_key", "k"), \
         patch("services.agent_center_llm_client.probe",
               new=AsyncMock(side_effect=RuntimeError("boom"))):
        out = await _verify_enrichment_grounding(_enrichment(), grounding, merchant_id="m1")
    assert out["passed"] is False
    assert out["reason"] == "verify_error"
    assert "boom" in (out["note"] or "")


@pytest.mark.asyncio
async def test_verify_enrichment_grounding_supports_false_still_passes():
    """Gate is on misstates_facts ONLY: supports_recommendation=False must NOT
    block (recorded, not gated). Also pins the probe call args + claim/source
    mapping (a regression that over-gates or inverts the mapping would fail)."""
    grounding = _grounding_facts_for_candidate(_candidate())
    probe = AsyncMock(return_value={"raw_runs": [{"parsed": {
        "supports_recommendation": False, "misstates_facts": False, "note": "ok"}}]})
    with patch("config.settings.settings.deepseek_api_key", "k"), \
         patch("services.agent_center_llm_client.probe", new=probe):
        out = await _verify_enrichment_grounding(_enrichment(), grounding, merchant_id="m1")
    assert out["passed"] is True and out["reason"] == "grounded"
    assert out["supports_recommendation"] is False
    probe.assert_awaited_once()
    kw = probe.await_args.kwargs
    assert kw["provider"] == "deepseek"
    assert kw["scan_mode"] == "answer_quality_verify"
    assert kw["max_runs"] == 1
    # generated copy -> answer; source facts -> evidence (mapping not inverted)
    assert "Low-molecular-weight collagen" in kw["context"]["verify_answer_text"]
    assert "Title: Good Night Collagen" in kw["context"]["verify_evidence_excerpt"]


def test_grounding_facts_for_candidate_intent_join_and_category_fallback():
    """intent joins _target_intents; category falls back to product_type."""
    g = _grounding_facts_for_candidate(
        _candidate(category="", _target_intents=["halal collagen", "sleep"]))
    assert g["category"] == "supplement"   # falls back to product_type
    assert g["intent"] == "halal collagen; sleep"
