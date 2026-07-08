"""W2 payoff — the honest visibility-tracking series.

Turns a merchant's audit history into a time-series a chart can render: brand-level
visibility / attribution / category scores over time, plus per-provider lines.

The load-bearing bit is HONESTY: a trend line only means something if the points are
COMPARABLE — measured on the same pinned prompt set (W2). Two points on different
prompt sets aren't a "drop", they're different questions. So each point carries its
`basis_id` and a `comparable_with_prev` flag; the chart draws a continuous line only
across same-basis points and breaks (annotates "measurement basis refreshed") where
a new basis appears. Without this the chart would re-introduce exactly the run-to-run
noise W2 removed.

Comparability is a property of the BASIS, not of adjacency: a check is comparable
to every earlier check on the same pinned prompt set, even when a differently-based
check ran in between (a merchant alternating two URL sets, say). Segments therefore
group ALL points sharing a basis — not just consecutive runs — and the chart
connects each basis's points across interleaved others.

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
                                and comparable_with_prev (shares its basis with an
                                EARLIER point — not necessarily the adjacent one —
                                → the line may connect it into that basis's segment).
      basis_changes[]         — point indices where a NEW basis first appears
                                (draw a break: a fresh comparison thread starts).
      segments[]              — per-basis point groups ({basis_id, indices});
                                indices are ascending but need not be consecutive.
      is_baseline_only        — <=1 point (nothing to trend yet).
    """
    from db.merchant_audit_runs import _provider_scores_from_report

    points: List[Dict[str, Any]] = []
    for r in rows or []:
        report = r.get("report_jsonb") or {}
        points.append({
            "run_id": r.get("run_id"),
            "date": _iso(r.get("requested_at")),
            "scores": {k: r.get(k) for k in SCORE_KEYS},
            "basis_id": _basis_id(report),
            "provider_scores": _provider_scores_from_report(report),
            "comparable_with_prev": False,  # set below once its basis group is known
        })

    # Group points into per-basis segments across the WHOLE series. Consecutive-only
    # grouping rendered an interleaved same-basis re-audit as a break even though
    # basis pinning worked — the honesty rule is "same prompt set", not "same prompt
    # set AND adjacent". Points with no basis (pre-pinning runs) can't be asserted
    # comparable to anything, so each stays a singleton segment.
    segments: List[Dict[str, Any]] = []
    seg_index_by_basis: Dict[str, int] = {}
    for i, p in enumerate(points):
        basis_id = p["basis_id"]
        si = seg_index_by_basis.get(basis_id) if basis_id else None
        if si is None:
            if basis_id:
                seg_index_by_basis[basis_id] = len(segments)
            segments.append({"basis_id": basis_id, "indices": [i]})
        else:
            segments[si]["indices"].append(i)
            p["comparable_with_prev"] = True

    # A break marks where a NEW basis first appears — not every alternation back
    # to an already-seen basis (those points continue their existing segment).
    basis_changes = [i for i, p in enumerate(points) if i > 0 and not p["comparable_with_prev"]]

    return {
        "points": points,
        "basis_changes": basis_changes,
        "segments": segments,
        "is_baseline_only": len(points) <= 1,
    }
