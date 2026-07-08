"""W2 tracking series — the basis-segmentation honesty is the point under test."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.audit_tracking_series import build_tracking_series

_T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _row(day, vis, att, cat, basis_id="sel_abc"):
    return {
        "run_id": f"run-{day}",
        "requested_at": _T0 + timedelta(days=day),
        "visibility": vis,
        "attribution": att,
        "category_visibility": cat,
        "report_jsonb": {
            "prompt_basis": {"selected_set_id": basis_id} if basis_id else {},
            "brand_rollup": {"citation_by_provider": {"gemini": {"median": vis}}},
        },
    }


def test_same_basis_points_are_comparable():
    series = build_tracking_series([
        _row(0, 60, 45, 55),
        _row(30, 68, 52, 60),
    ])
    pts = series["points"]
    assert pts[0]["comparable_with_prev"] is False        # first point
    assert pts[1]["comparable_with_prev"] is True         # same basis → connect
    assert series["basis_changes"] == []
    assert len(series["segments"]) == 1                   # one continuous line
    assert series["is_baseline_only"] is False
    assert pts[1]["scores"] == {"visibility": 68, "attribution": 52, "category_visibility": 60}
    assert pts[1]["provider_scores"] == {"gemini": 68}


def test_basis_change_breaks_the_line():
    series = build_tracking_series([
        _row(0, 60, 45, 55, basis_id="sel_old"),
        _row(30, 62, 47, 57, basis_id="sel_old"),
        _row(60, 40, 30, 35, basis_id="sel_new"),   # basis refreshed here
        _row(90, 44, 33, 38, basis_id="sel_new"),
    ])
    pts = series["points"]
    assert [p["comparable_with_prev"] for p in pts] == [False, True, False, True]
    assert series["basis_changes"] == [2]                 # the refresh point
    # two segments: the old-basis stretch and the new-basis stretch
    assert [seg["indices"] for seg in series["segments"]] == [[0, 1], [2, 3]]
    assert [seg["basis_id"] for seg in series["segments"]] == ["sel_old", "sel_new"]


def test_single_run_is_baseline_only():
    series = build_tracking_series([_row(0, 60, 45, 55)])
    assert series["is_baseline_only"] is True
    assert series["points"][0]["comparable_with_prev"] is False
    assert series["basis_changes"] == []


def test_missing_basis_is_never_comparable():
    # A run that predates pinning (no basis) can't be asserted comparable to anything.
    series = build_tracking_series([
        _row(0, 60, 45, 55, basis_id=None),
        _row(30, 80, 60, 70, basis_id=None),
    ])
    assert [p["comparable_with_prev"] for p in series["points"]] == [False, False]
    assert series["basis_changes"] == [1]


def test_interleaved_same_basis_points_still_connect():
    # A merchant alternating two URL sets: comparability is a property of the
    # BASIS, not of adjacency. A same-basis re-audit must join its earlier
    # points' segment even when differently-based checks ran in between.
    series = build_tracking_series([
        _row(0, 60, 45, 55, basis_id="sel_a"),
        _row(1, 30, 20, 25, basis_id="sel_b"),   # other URL set interleaved
        _row(2, 32, 22, 27, basis_id="sel_b"),
        _row(3, 68, 52, 60, basis_id="sel_a"),   # back to the first set
    ])
    pts = series["points"]
    assert [p["comparable_with_prev"] for p in pts] == [False, False, True, True]
    # Two per-basis segments; sel_a spans the interleaved run.
    assert [seg["basis_id"] for seg in series["segments"]] == ["sel_a", "sel_b"]
    assert [seg["indices"] for seg in series["segments"]] == [[0, 3], [1, 2]]
    # One break: where the NEW basis first appeared. Returning to sel_a at
    # index 3 continues an existing thread — no break there.
    assert series["basis_changes"] == [1]


def test_empty_history():
    series = build_tracking_series([])
    assert series["points"] == []
    assert series["is_baseline_only"] is True
