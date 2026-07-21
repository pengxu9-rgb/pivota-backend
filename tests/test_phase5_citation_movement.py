"""Phase 5.6 — public_llm_citation_movement verifier tests.

Validates baseline extraction + re-probe call + movement
classification. The verifier reads the audit's report_jsonb,
calls services.agent_center_llm_client.probe, and compares scores.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest


# =====================================================================
# Pure helpers — movement classification
# =====================================================================


def test_classify_movement_improved_above_threshold():
    from services.verifiers.citation_movement import _classify_movement
    assert _classify_movement(baseline=50, reprobed=60) == "improved"
    # Just at the +5 threshold
    assert _classify_movement(baseline=50, reprobed=55) == "improved"


def test_classify_movement_regressed_below_threshold():
    from services.verifiers.citation_movement import _classify_movement
    assert _classify_movement(baseline=50, reprobed=40) == "regressed"
    assert _classify_movement(baseline=50, reprobed=45) == "regressed"


def test_classify_movement_unchanged_within_noise():
    """Deltas within ±5 are classified as unchanged — within
    probe-to-probe noise."""
    from services.verifiers.citation_movement import _classify_movement
    assert _classify_movement(baseline=50, reprobed=50) == "unchanged"
    assert _classify_movement(baseline=50, reprobed=52) == "unchanged"
    assert _classify_movement(baseline=50, reprobed=47) == "unchanged"
    # Boundary: +4 → unchanged, +5 → improved
    assert _classify_movement(baseline=50, reprobed=54) == "unchanged"


# =====================================================================
# Baseline extraction
# =====================================================================


@pytest.mark.asyncio
async def test_extract_baseline_from_audit_report_canonical_path(
    monkeypatch,
):
    """Baseline lives at report.per_product[i].raw.category_visibility.
    scores.category_visibility_score for the matching product."""
    from services.verifiers import citation_movement
    from db import merchant_audit_runs as mar

    async def fake_fetch(*, run_id):
        return {
            "report_jsonb": {
                "per_product": [
                    {
                        "product_key": "shopify::sp-1",
                        "raw": {
                            "category_visibility": {
                                "scores": {
                                    "category_visibility_score": 67,
                                },
                            },
                        },
                    },
                ],
            },
        }
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)

    score = await citation_movement._extract_baseline_score(
        audit_run_id="audit-1", product_key="shopify::sp-1",
    )
    assert score == 67


@pytest.mark.asyncio
async def test_extract_baseline_falls_back_to_merchant_view_headline(
    monkeypatch,
):
    """When the canonical raw.category_visibility path is missing,
    try merchant_view.headline.category_visibility_score as a
    fallback."""
    from services.verifiers import citation_movement
    from db import merchant_audit_runs as mar

    async def fake_fetch(*, run_id):
        return {
            "report_jsonb": {
                "per_product": [{
                    "product_key": "shopify::sp-1",
                    # No raw.category_visibility
                    "merchant_view": {
                        "headline": {
                            "category_visibility_score": 42,
                        },
                    },
                }],
            },
        }
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)

    score = await citation_movement._extract_baseline_score(
        audit_run_id="audit-1", product_key="shopify::sp-1",
    )
    assert score == 42


@pytest.mark.asyncio
async def test_extract_baseline_returns_none_when_audit_missing(
    monkeypatch,
):
    from services.verifiers import citation_movement
    from db import merchant_audit_runs as mar

    async def fake_fetch(*, run_id):
        return None
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)

    score = await citation_movement._extract_baseline_score(
        audit_run_id="audit-1", product_key="shopify::sp-1",
    )
    assert score is None


@pytest.mark.asyncio
async def test_extract_baseline_returns_none_when_product_not_in_report(
    monkeypatch,
):
    """Audit report present but the product_key isn't in
    per_product — possibly an audit re-key issue."""
    from services.verifiers import citation_movement
    from db import merchant_audit_runs as mar

    async def fake_fetch(*, run_id):
        return {
            "report_jsonb": {
                "per_product": [
                    {"product_key": "shopify::sp-OTHER",
                     "raw": {"category_visibility": {
                         "scores": {"category_visibility_score": 50},
                     }}},
                ],
            },
        }
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)

    score = await citation_movement._extract_baseline_score(
        audit_run_id="audit-1", product_key="shopify::sp-1",
    )
    assert score is None


# =====================================================================
# run_citation_movement — full flow
# =====================================================================


def _sample_product() -> Dict[str, Any]:
    return {
        "merchant_id": "merch-1",
        "product_key": "shopify::sp-1",
        "title": "Wellness Greens Gummies",
        "brand": "Test Brand",
        "pivota_signature_id": "sig_abc",
        "pivota_canonical_url": (
            "https://agent.pivota.cc/products/sig_abc"
        ),
    }


def _ctx():
    from services.verification_run_worker import VerifierContext
    return VerifierContext(
        verify_id="v-1",
        audit_run_id="audit-1",
        verifier_id="public_llm_citation_movement",
        product_key="shopify::sp-1",
    )


def _patch_product_loader(monkeypatch, product=None):
    async def fake_load(*, audit_run_id, product_key):
        return product

    from services.verifiers import citation_movement
    monkeypatch.setattr(
        citation_movement, "load_product_context", fake_load,
    )


def _patch_baseline(monkeypatch, baseline=None):
    async def fake_baseline(*, audit_run_id, product_key):
        return baseline

    from services.verifiers import citation_movement
    monkeypatch.setattr(
        citation_movement, "_extract_baseline_score", fake_baseline,
    )


def _patch_baseline_provider(monkeypatch, provider="gemini"):
    """P5.8.6: tests need to stub the baseline-provider lookup.
    The default 'gemini' matches what the original P5.6 test
    fixtures assumed (audit ran with provider='gemini')."""
    async def fake_provider(*, audit_run_id):
        return provider

    from services.verifiers import citation_movement
    monkeypatch.setattr(
        citation_movement, "_extract_baseline_provider", fake_provider,
    )


def _patch_probe(monkeypatch, *, scores=None, raise_exc=None):
    captured: Dict[str, Any] = {"calls": []}

    async def fake_probe(**kwargs):
        captured["calls"].append(kwargs)
        if raise_exc is not None:
            raise raise_exc
        return {"scores": scores or {}}

    import services.agent_center_llm_client as client
    monkeypatch.setattr(client, "probe", fake_probe)
    return captured


@pytest.mark.asyncio
async def test_succeeded_when_score_improved_after_30d(monkeypatch):
    """Reprobed score significantly above baseline → succeeded
    with movement=improved in evidence."""
    from services.verifiers import citation_movement as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_baseline_provider(monkeypatch)  # P5.8.6
    _patch_baseline(monkeypatch, baseline=50)
    _patch_probe(
        monkeypatch, scores={"category_visibility_score": 65},
    )
    result = await v.run_citation_movement(_ctx())
    assert result.status == "succeeded"
    assert result.evidence_jsonb["baseline_category_visibility_score"] == 50
    assert result.evidence_jsonb["reprobed_category_visibility_score"] == 65
    assert result.evidence_jsonb["score_delta"] == 15
    assert result.evidence_jsonb["score_movement"] == "improved"


@pytest.mark.asyncio
async def test_succeeded_when_score_regressed_after_30d(monkeypatch):
    """Reprobed score significantly below baseline ALSO returns
    succeeded — the verifier's job is to MEASURE, not judge. The
    merchant projection surfaces the delta to ops who decide
    whether to action."""
    from services.verifiers import citation_movement as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_baseline_provider(monkeypatch)  # P5.8.6
    _patch_baseline(monkeypatch, baseline=60)
    _patch_probe(
        monkeypatch, scores={"category_visibility_score": 30},
    )
    result = await v.run_citation_movement(_ctx())
    assert result.status == "succeeded"
    assert result.evidence_jsonb["score_movement"] == "regressed"
    assert result.evidence_jsonb["score_delta"] == -30


@pytest.mark.asyncio
async def test_succeeded_when_unchanged_within_noise(monkeypatch):
    from services.verifiers import citation_movement as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_baseline_provider(monkeypatch)  # P5.8.6
    _patch_baseline(monkeypatch, baseline=55)
    _patch_probe(
        monkeypatch, scores={"category_visibility_score": 53},
    )
    result = await v.run_citation_movement(_ctx())
    assert result.status == "succeeded"
    assert result.evidence_jsonb["score_movement"] == "unchanged"


@pytest.mark.asyncio
async def test_blocked_when_no_baseline_available(monkeypatch):
    """Audit report missing category_visibility data → blocked.
    Verifier can't run without baseline."""
    from services.verifiers import citation_movement as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_baseline_provider(monkeypatch)  # P5.8.6
    _patch_baseline(monkeypatch, baseline=None)
    result = await v.run_citation_movement(_ctx())
    assert result.status == "blocked"
    assert "no_baseline_score" in result.error_message


@pytest.mark.asyncio
async def test_blocked_when_no_product_context(monkeypatch):
    from services.verifiers import citation_movement as v
    _patch_product_loader(monkeypatch, product=None)
    result = await v.run_citation_movement(_ctx())
    assert result.status == "blocked"


@pytest.mark.asyncio
async def test_failed_when_probe_raises(monkeypatch):
    """LLM probe raises (rate-limited, transient) → failed
    (retryable)."""
    from services.verifiers import citation_movement as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_baseline_provider(monkeypatch)  # P5.8.6
    _patch_baseline(monkeypatch, baseline=50)
    _patch_probe(monkeypatch, raise_exc=RuntimeError("rate limited"))
    result = await v.run_citation_movement(_ctx())
    assert result.status == "failed"
    assert "rate limited" in result.error_message


@pytest.mark.asyncio
async def test_failed_when_probe_returns_no_score(monkeypatch):
    """Probe succeeded but didn't return a category_visibility_score
    in the result — failed (retryable)."""
    from services.verifiers import citation_movement as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_baseline_provider(monkeypatch)  # P5.8.6
    _patch_baseline(monkeypatch, baseline=50)
    _patch_probe(monkeypatch, scores={"different_metric": 99})
    result = await v.run_citation_movement(_ctx())
    assert result.status == "failed"
    assert "reprobe_missing_score" in result.error_message


@pytest.mark.asyncio
async def test_probe_called_with_category_visibility_scan_mode(
    monkeypatch,
):
    """Verifies the verifier requests the right scan_mode + the
    PINNED baseline provider (P5.8.6). Was previously 'auto' but
    that conflated cross-LLM variance with citation movement."""
    from services.verifiers import citation_movement as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_baseline_provider(monkeypatch)  # P5.8.6: default 'gemini'
    _patch_baseline(monkeypatch, baseline=50)
    captured = _patch_probe(
        monkeypatch, scores={"category_visibility_score": 50},
    )
    await v.run_citation_movement(_ctx())
    assert captured["calls"][0]["scan_mode"] == "category_visibility_test"
    # P5.8.6: pinned to baseline provider (default 'gemini')
    assert captured["calls"][0]["provider"] == "gemini"
    assert captured["calls"][0]["merchant_id"] == "merch-1"


@pytest.mark.asyncio
async def test_blocked_when_baseline_provider_unavailable(monkeypatch):
    """P5.8.6: when the audit's report_jsonb doesn't carry the
    LLM provider used at baseline, the verifier MUST refuse to
    substitute (cross-LLM comparison is apples-to-oranges).
    Returns blocked, not failed (terminal, no retry)."""
    from services.verifiers import citation_movement as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_baseline(monkeypatch, baseline=50)
    _patch_baseline_provider(monkeypatch, provider=None)
    result = await v.run_citation_movement(_ctx())
    assert result.status == "blocked"
    assert "baseline_provider_unavailable" in result.error_message


@pytest.mark.asyncio
async def test_probe_uses_pinned_baseline_provider_not_auto(
    monkeypatch,
):
    """P5.8.6: verifier MUST call probe() with the baseline
    provider, not 'auto'. Catches regression where someone resets
    the provider back to the orchestrator default."""
    from services.verifiers import citation_movement as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_baseline(monkeypatch, baseline=50)
    _patch_baseline_provider(monkeypatch, provider="deepseek")
    captured = _patch_probe(
        monkeypatch, scores={"category_visibility_score": 50},
    )
    await v.run_citation_movement(_ctx())
    assert captured["calls"][0]["provider"] == "deepseek"
    # Must NOT be "auto" — that's the bug class
    assert captured["calls"][0]["provider"] != "auto"


def test_citation_movement_registers():
    import services.verifiers  # noqa: F401
    from services.verification_run_worker import (
        get_registered_verifier_ids,
    )
    assert "public_llm_citation_movement" in get_registered_verifier_ids()
