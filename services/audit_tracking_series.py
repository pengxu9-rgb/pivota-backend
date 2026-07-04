"""W2 payoff — the honest visibility-tracking series.

Turns a merchant's audit history into a time-series a chart can render: brand-level
visibility / attribution / category scores over time, plus per-provider lines.

The load-bearing bit is HONESTY: a trend line only means something if the points are
COMPARABLE — measured on the same pinned prompt set (W2). Two points on different
prompt sets aren't a "drop", they're different questions. So each point carries its
`basis_id` and a `comparable_with_prev` flag; the chart draws a continuous line only
across same-basis points and breaks (annotates "measurement basis refreshed") where
the basis changed. Without this the chart would re-introduce exactly the run-to-run
noise W2 removed.

Reuses audit_delta's basis extraction so this series and the pairwise "since your
last audit" delta can never disagree about basis identity.

Pure: callers fetch rows (oldest-first) via db.score_history_for_merchant and pass
them in; no I/O here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

SCORE_KEYS = ("visibility", "attribution", "category_visibility")


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _basis_id(report: Mapping[str, Any]) -> Optional[str]:
    """The run's pinned-basis identity, via audit_delta (single source of truth)."""
    from services import audit_delta as ad

    return ad._prompt_set_id(report, ad._primary_report(report))


def build_tracking_series(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rows are OLDEST-first, each {run_id, requested_at, visibility, attribution,
    category_visibility, report_jsonb}. Returns the chart payload:

      points[]                — one per run, with scores, basis_id, provider_scores,
                                and comparable_with_prev (same basis as the point
                                before it → the line may connect them).
      basis_changes[]         — point indices where the basis changed (draw a break).
      segments[]              — runs of same-basis points ({basis_id, indices}).
      is_baseline_only        — <=1 point (nothing to trend yet).
    """
    from db.merchant_audit_runs import _provider_scores_from_report

    points: List[Dict[str, Any]] = []
    prev_basis: Optional[str] = None
    for r in rows or []:
        report = r.get("report_jsonb") or {}
        basis_id = _basis_id(report)
        comparable = bool(basis_id and prev_basis and basis_id == prev_basis)
        points.append({
            "run_id": r.get("run_id"),
            "date": _iso(r.get("requested_at")),
            "scores": {k: r.get(k) for k in SCORE_KEYS},
            "basis_id": basis_id,
            "provider_scores": _provider_scores_from_report(report),
            "comparable_with_prev": comparable,
        })
        prev_basis = basis_id

    basis_changes = [i for i, p in enumerate(points) if i > 0 and not p["comparable_with_prev"]]

    # Group consecutive same-basis points into segments the chart connects as one line.
    segments: List[Dict[str, Any]] = []
    for i, p in enumerate(points):
        if i == 0 or not p["comparable_with_prev"]:
            segments.append({"basis_id": p["basis_id"], "indices": [i]})
        else:
            segments[-1]["indices"].append(i)

    return {
        "points": points,
        "basis_changes": basis_changes,
        "segments": segments,
        "is_baseline_only": len(points) <= 1,
    }
