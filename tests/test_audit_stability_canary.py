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


# --- auto-detect grouping + window gate (_stability_pairs) ----------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

_T0 = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


def _row(merchant_id, run_id, hours_ago, **scores):
    return {
        "merchant_id": merchant_id,
        "run_id": run_id,
        "requested_at": _T0 - timedelta(hours=hours_ago),
        "report_jsonb": _report(**scores),
    }


def test_pairs_auto_detects_all_merchants(monkeypatch):
    monkeypatch.setattr(sc, "STABILITY_TOLERANCE", 5)
    monkeypatch.setattr(sc, "STABILITY_WINDOW_HOURS", 48)
    rows = [
        # m1: stable pair (Δ3) within window
        _row("m1", "r1b", 1, visibility=63, attribution=45, category_visibility=55),
        _row("m1", "r1a", 5, visibility=60, attribution=45, category_visibility=57),
        # m2: breach pair (Δ20) within window
        _row("m2", "r2b", 2, visibility=80, attribution=45, category_visibility=55),
        _row("m2", "r2a", 6, visibility=60, attribution=45, category_visibility=55),
    ]
    out = {r["merchant_id"]: r for r in sc._stability_pairs(rows, allow=set())}
    assert out["m1"]["status"] == "stable"
    assert out["m2"]["status"] == "breach" and out["m2"]["max_delta"] == 20


def test_pairs_skips_time_separated_same_basis(monkeypatch):
    # Same basis, big delta, but 100h apart (> 48h window) → real trend, not a canary
    # breach → skipped entirely.
    monkeypatch.setattr(sc, "STABILITY_WINDOW_HOURS", 48)
    rows = [
        _row("m1", "b", 1, visibility=80, attribution=45, category_visibility=55),
        _row("m1", "a", 101, visibility=60, attribution=45, category_visibility=55),
    ]
    assert sc._stability_pairs(rows, allow=set()) == []


def test_pairs_skips_single_run_and_different_basis(monkeypatch):
    monkeypatch.setattr(sc, "STABILITY_WINDOW_HOURS", 48)
    rows = [
        _row("solo", "x", 1, visibility=60, attribution=45, category_visibility=55),
        # different basis pair → not comparable
        {"merchant_id": "m2", "run_id": "p", "requested_at": _T0 - timedelta(hours=1),
         "report_jsonb": _report(visibility=80, attribution=45, category_visibility=55, basis_id="sel_new")},
        {"merchant_id": "m2", "run_id": "q", "requested_at": _T0 - timedelta(hours=2),
         "report_jsonb": _report(visibility=60, attribution=45, category_visibility=55, basis_id="sel_old")},
    ]
    assert sc._stability_pairs(rows, allow=set()) == []


def test_pairs_allowlist_narrows(monkeypatch):
    monkeypatch.setattr(sc, "STABILITY_WINDOW_HOURS", 48)
    rows = [
        _row("m1", "b", 1, visibility=80, attribution=45, category_visibility=55),
        _row("m1", "a", 3, visibility=60, attribution=45, category_visibility=55),
        _row("m2", "b", 1, visibility=80, attribution=45, category_visibility=55),
        _row("m2", "a", 3, visibility=60, attribution=45, category_visibility=55),
    ]
    out = sc._stability_pairs(rows, allow={"m2"})
    assert [r["merchant_id"] for r in out] == ["m2"]
