"""
Phase C-4 (PR-D) tests for the Pivota canonical PDP indexing-arc
state computation.

Two surfaces:
  1. `services.pivota_indexing_arc.compute_indexing_arc_state` —
     pure function, deterministic boundaries (fresh < 7d, indexing
     7-90d, expected_steady > 90d), graceful handling of None
     `minted_at` / mixed naive+aware datetimes.
  2. End-to-end: `merchant_view.diagnosis.indexing_arc_state`
     populated when `url_source == "pivota_canonical_pdp"` AND
     `pivota_signature_minted_at` is set. Otherwise None (the
     diagnostic isn't relevant for merchant-URL audits).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


# ---------------------------------------------------------------------
# 1. Pure-function boundaries
# ---------------------------------------------------------------------


def test_unknown_phase_when_minted_at_is_none():
    from services.pivota_indexing_arc import compute_indexing_arc_state
    out = compute_indexing_arc_state(None)
    assert out["phase"] == "unknown"
    assert out["days_since_mint"] is None
    assert out["minted_at"] is None
    assert out["expected_first_citation_at"] is None
    assert "30-90" in out["caveat"]


def test_fresh_phase_for_recent_mint():
    from services.pivota_indexing_arc import compute_indexing_arc_state
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    minted = now - timedelta(days=3)
    out = compute_indexing_arc_state(minted, now=now)
    assert out["phase"] == "fresh"
    assert out["days_since_mint"] == 3
    assert "Search Console" in out["caveat"]


def test_fresh_phase_boundary_at_day_6():
    from services.pivota_indexing_arc import compute_indexing_arc_state
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    minted = now - timedelta(days=6, hours=1)
    out = compute_indexing_arc_state(minted, now=now)
    assert out["phase"] == "fresh"


def test_indexing_phase_starts_at_day_7():
    from services.pivota_indexing_arc import compute_indexing_arc_state
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    minted = now - timedelta(days=7, hours=1)
    out = compute_indexing_arc_state(minted, now=now)
    assert out["phase"] == "indexing"
    assert "indexing window" in out["caveat"]


def test_indexing_phase_at_day_45():
    from services.pivota_indexing_arc import compute_indexing_arc_state
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    minted = now - timedelta(days=45)
    out = compute_indexing_arc_state(minted, now=now)
    assert out["phase"] == "indexing"
    assert out["days_since_mint"] == 45


def test_indexing_phase_boundary_at_day_89():
    from services.pivota_indexing_arc import compute_indexing_arc_state
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    minted = now - timedelta(days=89, hours=23)
    out = compute_indexing_arc_state(minted, now=now)
    assert out["phase"] == "indexing"


def test_expected_steady_phase_starts_at_day_90():
    from services.pivota_indexing_arc import compute_indexing_arc_state
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    minted = now - timedelta(days=91)
    out = compute_indexing_arc_state(minted, now=now)
    assert out["phase"] == "expected_steady"
    assert "past the typical 90-day" in out["caveat"]


def test_minted_at_naive_treated_as_utc():
    """SQLite returns DateTime without timezone info — should still work."""
    from services.pivota_indexing_arc import compute_indexing_arc_state
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    minted_naive = datetime(2026, 5, 4, 12, 0)  # 3 days ago, no tz
    out = compute_indexing_arc_state(minted_naive, now=now)
    assert out["phase"] == "fresh"
    assert out["days_since_mint"] == 3


def test_expected_first_citation_at_is_minted_plus_90d():
    from services.pivota_indexing_arc import compute_indexing_arc_state
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    minted = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    out = compute_indexing_arc_state(minted, now=now)
    expected = (minted + timedelta(days=90)).isoformat()
    assert out["expected_first_citation_at"] == expected


# ---------------------------------------------------------------------
# 2. End-to-end merchant_view.diagnosis.indexing_arc_state
# ---------------------------------------------------------------------


def _vis_run(q): return {"query": q, "parsed": {"product_visible": False}, "grounding_chunks": []}
def _attr_run(q): return {"query": q, "parsed": {"merchant_url_found": False}, "grounding_chunks": []}


def _build_with_arc(url_source, minted_at):
    from services.agent_center_bd_report_service import build_structured_report
    return build_structured_report(
        merchant_name="TestMerchant",
        merchant_pdp_url="https://testmerchant.com/p/x",
        product_title="Test Product",
        product_vendor="TestMerchant",
        product_type="Sleepwear",
        visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_vis_run("v1")],
        },
        attribution_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_attr_run("a1")],
        },
        provider="gemini",
        url_source=url_source,
        pivota_signature_minted_at=minted_at,
    )


def test_diagnosis_indexing_arc_state_null_for_merchant_url_audit():
    """When the audit ran against the merchant's own URL (not Pivota
    canonical), the indexing arc state isn't relevant — null."""
    minted = datetime.now(timezone.utc) - timedelta(days=10)
    report = _build_with_arc(url_source="merchant_canonical_url", minted_at=minted)
    arc = report["merchant_view"]["diagnosis"]["indexing_arc_state"]
    assert arc is None


def test_diagnosis_indexing_arc_state_computed_for_pivota_canonical_audit():
    """When url_source = 'pivota_canonical_pdp', the diagnosis surfaces
    the real arc phase computed from minted_at."""
    minted = datetime.now(timezone.utc) - timedelta(days=10)
    report = _build_with_arc(url_source="pivota_canonical_pdp", minted_at=minted)
    arc = report["merchant_view"]["diagnosis"]["indexing_arc_state"]
    assert arc is not None
    assert arc["phase"] == "indexing"
    assert arc["days_since_mint"] == 10
    assert arc["minted_at"] is not None
    assert arc["expected_first_citation_at"] is not None


def test_diagnosis_indexing_arc_state_unknown_when_minted_at_missing():
    """Legacy rows the migration backfill couldn't reach (or rows in
    flight before the column existed) → minted_at None → unknown phase
    + the static-style 30-90d caveat. The frontend can render the same
    way as fresh + with a hint about minting."""
    report = _build_with_arc(url_source="pivota_canonical_pdp", minted_at=None)
    arc = report["merchant_view"]["diagnosis"]["indexing_arc_state"]
    assert arc is not None
    assert arc["phase"] == "unknown"
    assert arc["minted_at"] is None
