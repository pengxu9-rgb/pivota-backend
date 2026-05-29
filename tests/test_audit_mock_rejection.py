"""
Mock-data guard for the merchant audit endpoint.

The audit pipeline has three legitimate pollution sources where
synthetic fallback data flows through the same prose templates as
real Gemini-grounded data:

  1. `_local_mock_result` in services.agent_center_llm_client (this
     backend's key unset)
  2. Upstream Pivota-Agent service's mock (its Gemini key unset)
  3. Explicit provider="mock" via feature flag

Each produces a per_product `upstream_status.is_real=False`. The
merchant-facing route MUST refuse to ship audit prose against this
synthetic data — better to fail with a clear 503 than to fabricate
a report that looks identical to a real run. BD-internal flows can
still render mock data with a "MOCK DATA — DO NOT SHARE" banner;
merchant-facing flows cannot.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from routes.merchant_audit_routes import _detect_mock_per_product


def _per_product(*, is_real: bool, reason: str | None = None) -> Dict[str, Any]:
    """Build a per-product report shape with the upstream_status fields
    the guard reads."""
    return {
        "merchant_view": {},
        "verdict": {"label": "PARTIAL"},
        "upstream_status": {
            "is_real": is_real,
            "reason": reason,
            "requested_provider": "gemini",
            "visibility_provider": "gemini" if is_real else "local_mock_no_internal_key",
            "attribution_provider": "gemini" if is_real else "local_mock_no_internal_key",
        },
    }


def test_no_mock_when_all_real():
    report = {
        "per_product": [
            _per_product(is_real=True),
            _per_product(is_real=True),
        ],
    }
    assert _detect_mock_per_product(report) == []


def test_detects_local_mock_fallback():
    """Source #1: this backend's PIVOTA_AGENT_INTERNAL_API_KEY unset."""
    report = {
        "per_product": [
            _per_product(
                is_real=False,
                reason="Local mock fired; PIVOTA_AGENT_INTERNAL_API_KEY not configured.",
            ),
        ],
    }
    detected = _detect_mock_per_product(report)
    assert len(detected) == 1
    assert "Local mock" in detected[0]["upstream_status"]["reason"]


def test_detects_upstream_mock_fallback():
    """Source #2: upstream Pivota-Agent's Gemini key unset."""
    report = {
        "per_product": [
            _per_product(
                is_real=False,
                reason="Upstream returned mock_fallback_no_gemini_key.",
            ),
        ],
    }
    assert len(_detect_mock_per_product(report)) == 1


def test_detects_explicit_mock_provider():
    """Source #3: explicit provider='mock' (via feature flag)."""
    report = {
        "per_product": [
            _per_product(
                is_real=False,
                reason="Operator requested provider=mock.",
            ),
        ],
    }
    assert len(_detect_mock_per_product(report)) == 1


def test_detects_partial_mock_pollution():
    """If even one per-product report is mock, the whole audit is
    suspect. Don't ship a partial audit where some products have real
    data and others are synthesised — the merchant can't tell which
    is which from the rendered prose."""
    report = {
        "per_product": [
            _per_product(is_real=True),
            _per_product(is_real=False),
            _per_product(is_real=True),
        ],
    }
    detected = _detect_mock_per_product(report)
    assert len(detected) == 1


def test_missing_upstream_status_treated_as_real():
    """Conservative default: missing/malformed upstream_status field
    is NOT a mock signal — could be a different bug class entirely
    (e.g., engine forgot to populate the field). We only reject when
    is_real is EXPLICITLY False."""
    report = {
        "per_product": [
            {
                "merchant_view": {},
                "verdict": {"label": "PARTIAL"},
                # No upstream_status at all
            },
        ],
    }
    assert _detect_mock_per_product(report) == []


def test_empty_report_no_mock():
    assert _detect_mock_per_product({}) == []
    assert _detect_mock_per_product({"per_product": []}) == []
    assert _detect_mock_per_product({"per_product": None}) == []


def test_non_dict_per_product_entries_skipped():
    """Defensive: skip non-dict entries gracefully (don't raise)."""
    report = {
        "per_product": [
            None,
            "not a dict",
            _per_product(is_real=False),
        ],
    }
    detected = _detect_mock_per_product(report)
    assert len(detected) == 1


@pytest.mark.asyncio
async def test_probe_raises_when_key_unset_and_no_opt_in(monkeypatch):
    """Source #1 elimination: probe() now RAISES when
    PIVOTA_AGENT_INTERNAL_API_KEY is unset, instead of silently
    returning the local mock. This prevents silent pollution of
    merchant-facing audit / BD report prose. Demand-test opts in
    via allow_local_mock=True; everyone else fails loud."""
    from services import agent_center_llm_client as llm_client
    monkeypatch.setattr(llm_client.settings, "pivota_agent_internal_api_key", None)
    with pytest.raises(llm_client.AgentCenterLlmClientError) as excinfo:
        await llm_client.probe(
            scan_mode="open_product_visibility_test",
            scan_target_id="t1",
            merchant_id="m1",
            store_id="s1",
            provider="gemini",
            max_runs=1,
        )
    assert "PIVOTA_AGENT_INTERNAL_API_KEY" in str(excinfo.value)
    assert "refusing" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_probe_local_mock_fires_only_with_explicit_opt_in(monkeypatch):
    """Demand-test runner opts into local mock by passing
    allow_local_mock=True. Verify the opt-in actually works."""
    from services import agent_center_llm_client as llm_client
    monkeypatch.setattr(llm_client.settings, "pivota_agent_internal_api_key", None)
    result = await llm_client.probe(
        scan_mode="open_product_visibility_test",
        scan_target_id="t1",
        merchant_id="m1",
        store_id="s1",
        provider="mock",
        max_runs=1,
        allow_local_mock=True,
    )
    assert result["provider"] == "local_mock_no_internal_key"


def test_classify_provider_marks_real_engine_providers():
    """End-to-end sanity: services.agent_center_bd_report_service
    `_classify_provider` returns is_real=True for engine-backed real
    providers. Mock fallbacks still yield is_real=False, which the
    guard then catches."""
    from services.agent_center_bd_report_service import _classify_provider

    assert _classify_provider("gemini")["is_real"] is True
    assert _classify_provider("chatgpt")["is_real"] is True
    assert _classify_provider("claude")["is_real"] is True
    assert _classify_provider("gemini,chatgpt")["is_real"] is True
    assert _classify_provider("local_mock_no_internal_key")["is_real"] is False
    assert _classify_provider("mock_fallback_no_gemini_key")["is_real"] is False
    assert _classify_provider("mock")["is_real"] is False
    # Unknown providers default to NOT real (safe).
    assert _classify_provider("some_new_provider")["is_real"] is False
