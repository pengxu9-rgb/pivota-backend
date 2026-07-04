"""W7 stability canary — the pure same-basis |Δ| check."""

from __future__ import annotations

import services.audit_stability_canary as sc


def _report(*, visibility, attribution, category_visibility, basis_id="sel_abc"):
    """Minimal report shape audit_delta._scores / _measurement_basis read."""
    return {
        "prompt_basis": {"selected_set_id": basis_id} if basis_id else {},
        "verdict": {
            "visibility_score": visibility,
            "attribution_score": attribution,
            "category_visibility_score": category_visibility,
        },
        "merchant_view": {
            "headline": {
                "scores": {
                    "visibility": visibility,
                    "attribution": attribution,
                    "category_visibility": category_visibility,
                }
            }
        },
    }


def test_same_basis_within_tolerance_is_stable(monkeypatch):
    monkeypatch.setattr(sc, "STABILITY_TOLERANCE", 5)
    cur = _report(visibility=62, attribution=48, category_visibility=55)
    pri = _report(visibility=60, attribution=45, category_visibility=57)
    res = sc.stability_delta(cur, pri)
    assert res is not None
    assert res["max_delta"] == 3
    assert res["breach"] is False


def test_same_basis_beyond_tolerance_breaches(monkeypatch):
    monkeypatch.setattr(sc, "STABILITY_TOLERANCE", 5)
    cur = _report(visibility=80, attribution=48, category_visibility=55)
    pri = _report(visibility=60, attribution=45, category_visibility=57)
    res = sc.stability_delta(cur, pri)
    assert res["max_delta"] == 20         # visibility 60->80
    assert res["breach"] is True
    assert res["deltas"]["visibility"] == 20


def test_different_basis_is_not_comparable():
    # Different pinned set → not a like-for-like comparison → None (never a false
    # "unstable" from measuring different questions).
    cur = _report(visibility=80, attribution=48, category_visibility=55, basis_id="sel_new")
    pri = _report(visibility=60, attribution=45, category_visibility=57, basis_id="sel_old")
    assert sc.stability_delta(cur, pri) is None


def test_unknown_basis_is_not_comparable():
    cur = _report(visibility=80, attribution=48, category_visibility=55, basis_id=None)
    pri = _report(visibility=60, attribution=45, category_visibility=57, basis_id=None)
    assert sc.stability_delta(cur, pri) is None


def test_configured_merchants_parsing(monkeypatch):
    monkeypatch.setenv("AUDIT_STABILITY_CANARY_MERCHANTS", " m1 , m2 ,")
    assert sc._configured_merchants() == ["m1", "m2"]
    monkeypatch.setenv("AUDIT_STABILITY_CANARY_MERCHANTS", "")
    assert sc._configured_merchants() == []
