"""
BD-route mock-rejection guard tests.

Closes the parallel of merchant-route PR #366's `_detect_mock_per_product`
guard for the three BD routes:
  - /api/agent-center/bd/external-merchant-report
  - /api/agent-center/bd/brand-report
  - /api/agent-center/bd/cold-start-audit

Without this guard, BD operators could see fabricated audit prose
against synthetic upstream data — same fabrication risk we explicitly
guarded against on the merchant side.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


def _per_product(*, is_real: bool, reason: str | None = None) -> Dict[str, Any]:
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


# -----------------------------------------------------------------
# _detect_mock_reports — pure helper
# -----------------------------------------------------------------


def test_detect_no_mock_when_all_real():
    from routes.agent_center_bd_routes import _detect_mock_reports
    out = _detect_mock_reports([_per_product(is_real=True), _per_product(is_real=True)])
    assert out == []


def test_detect_local_mock_fallback():
    """Source #1: this backend's PIVOTA_AGENT_INTERNAL_API_KEY unset."""
    from routes.agent_center_bd_routes import _detect_mock_reports
    out = _detect_mock_reports([
        _per_product(is_real=False, reason="Local mock fired; key not configured."),
    ])
    assert len(out) == 1


def test_detect_upstream_mock_fallback():
    """Source #2: upstream Pivota-Agent's Gemini key unset."""
    from routes.agent_center_bd_routes import _detect_mock_reports
    out = _detect_mock_reports([
        _per_product(is_real=False, reason="Upstream returned mock_fallback_no_gemini_key"),
    ])
    assert len(out) == 1


def test_detect_partial_pollution_rejects_whole_audit():
    """Even one mock per_product → reject. Operators can't tell from
    rendered prose which products are real vs synthetic."""
    from routes.agent_center_bd_routes import _detect_mock_reports
    out = _detect_mock_reports([
        _per_product(is_real=True),
        _per_product(is_real=False),
        _per_product(is_real=True),
    ])
    assert len(out) == 1


def test_detect_missing_upstream_status_treated_as_real():
    """Conservative: missing upstream_status field is NOT a mock
    signal — could be a different bug class. Reject only on
    EXPLICIT is_real=False."""
    from routes.agent_center_bd_routes import _detect_mock_reports
    out = _detect_mock_reports([
        {"merchant_view": {}, "verdict": {"label": "PARTIAL"}},
        # No upstream_status at all
    ])
    assert out == []


def test_detect_handles_empty_or_none():
    from routes.agent_center_bd_routes import _detect_mock_reports
    assert _detect_mock_reports([]) == []
    assert _detect_mock_reports(None) == []


def test_detect_handles_non_dict_entries():
    """Defensive: skip non-dict entries gracefully."""
    from routes.agent_center_bd_routes import _detect_mock_reports
    out = _detect_mock_reports([
        None,
        "not a dict",
        _per_product(is_real=False),
    ])
    assert len(out) == 1


# -----------------------------------------------------------------
# _raise_mock_rejection — error shape
# -----------------------------------------------------------------


def test_raise_mock_rejection_uses_503_with_structured_detail():
    from fastapi import HTTPException
    from routes.agent_center_bd_routes import _raise_mock_rejection
    mock_reports = [
        _per_product(
            is_real=False,
            reason="Operator requested provider=mock.",
        ),
    ]
    with pytest.raises(HTTPException) as ei:
        _raise_mock_rejection(mock_reports, "brand-report")
    assert ei.value.status_code == 503
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "upstream_mock_fallback"
    assert "Operator requested provider=mock" in detail["message"]
    assert "PIVOTA_AGENT_INTERNAL_API_KEY" in detail["message"]


def test_raise_mock_rejection_handles_missing_reason():
    from fastapi import HTTPException
    from routes.agent_center_bd_routes import _raise_mock_rejection
    # upstream_status with is_real=False but no reason field
    mock_reports = [{
        "upstream_status": {"is_real": False},
    }]
    with pytest.raises(HTTPException) as ei:
        _raise_mock_rejection(mock_reports, "cold-start-audit")
    assert ei.value.status_code == 503
    # Falls back to default message
    assert "Upstream returned mock data" in ei.value.detail["message"]
