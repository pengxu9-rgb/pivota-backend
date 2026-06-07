"""Deterministic re-audit delta projection.

This module is intentionally pure: callers fetch any prior audit rows/reports
and pass the structured report payloads in. The output is merchant-facing, so it
only diffs wave-1-stable signals and gates score noise before naming movement.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional


SCORE_SIGNALS = ("visibility", "attribution", "category_visibility")
SIGNAL_LABELS = {
    "visibility": "AI visibility",
    "attribution": "First-party citation",
    "category_visibility": "Category visibility",
    "primary_gap": "Primary recommendation",
    "controller_archetype": "Controller archetype",
    "top_controller": "#1 controller",
    "verdict": "Verdict",
}
MATERIAL_SCORE_DELTA = 15


def build_reaudit_delta(
    *,
    current_report: Mapping[str, Any],
    prior_report: Optional[Mapping[str, Any]],
    prior_row: Optional[Mapping[str, Any]],
    days_since: Optional[int],
) -> Dict[str, Any]:
    """Build the honest "Since your last audit" payload.

    ``current_report`` and ``prior_report`` may be either a single structured
    product report or the full brand report with ``per_product``. Only the first
    product report is used because the renderers and merchant-view contract do
    the same today.
    """

    current = _primary_report(current_report)
    prior = _primary_report(prior_report)
    metrics = _tracking_metrics(current)

    if not prior:
        return {
            "is_first_audit": True,
            "days_since_last": None,
            "headline": "Baseline established — re-audit in ~30 days to see movement.",
            "movements": [],
            "tracked_metric_results": [
                _not_measurable(
                    metric,
                    "No prior audit exists yet; this run is the baseline.",
                )
                for metric in metrics
            ],
        }

    movements: List[Dict[str, Any]] = []
    prior_scores = _scores(prior)
    current_scores = _scores(current)
    for signal in SCORE_SIGNALS:
        movements.append(
            _score_movement(
                signal=signal,
                prior=prior_scores.get(signal),
                current=current_scores.get(signal),
            )
        )

    prior_stable = _stable_fields(prior)
    current_stable = _stable_fields(current)
    for signal in ("primary_gap", "controller_archetype", "top_controller", "verdict"):
        movements.append(
            _categorical_movement(
                signal=signal,
                prior=prior_stable.get(signal),
                current=current_stable.get(signal),
            )
        )

    return {
        "is_first_audit": False,
        "days_since_last": days_since,
        "headline": _headline(movements, days_since),
        "movements": movements,
        "tracked_metric_results": [
            _tracked_metric_result(metric, movements) for metric in metrics
        ],
    }


def _primary_report(report: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not isinstance(report, Mapping):
        return {}
    per_product = report.get("per_product")
    if isinstance(per_product, list):
        for item in per_product:
            if isinstance(item, Mapping):
                return dict(item)
        return {}
    return dict(report)


def _merchant_view(report: Mapping[str, Any]) -> Mapping[str, Any]:
    mv = report.get("merchant_view")
    return mv if isinstance(mv, Mapping) else {}


def _next_best_action(report: Mapping[str, Any]) -> Mapping[str, Any]:
    nba = _merchant_view(report).get("next_best_action")
    return nba if isinstance(nba, Mapping) else {}


def _scores(report: Mapping[str, Any]) -> Dict[str, Optional[int]]:
    headline = _as_mapping(_merchant_view(report).get("headline"))
    headline_scores = _as_mapping(headline.get("scores"))
    verdict = _as_mapping(report.get("verdict"))
    category = _as_mapping(report.get("category_visibility"))
    return {
        "visibility": _int_or_none(
            headline_scores.get("visibility", verdict.get("visibility_score"))
        ),
        "attribution": _int_or_none(
            headline_scores.get("attribution", verdict.get("attribution_score"))
        ),
        "category_visibility": _int_or_none(
            headline_scores.get(
                "category_visibility",
                verdict.get("category_visibility_score", category.get("score")),
            )
        ),
    }


def _stable_fields(report: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    mv = _merchant_view(report)
    headline = _as_mapping(mv.get("headline"))
    verdict = _as_mapping(report.get("verdict"))
    nba = _next_best_action(report)
    play = _as_mapping(nba.get("canonical_page_play"))
    profile = _as_mapping(play.get("controller_profile"))
    evidence = _as_mapping(nba.get("evidence") or nba.get("evidence_used"))
    return {
        "primary_gap": _clean(nba.get("primary_gap")),
        "controller_archetype": _first_clean(
            profile.get("strategy"),
            play.get("controller_strategy"),
            _operator_move_controller_strategy(nba),
        ),
        "top_controller": _top_controller(report, play=play, evidence=evidence),
        "verdict": _first_clean(
            headline.get("verdict_label"),
            verdict.get("label"),
            report.get("verdict_label"),
        ),
    }


def _score_movement(
    *,
    signal: str,
    prior: Optional[int],
    current: Optional[int],
) -> Dict[str, Any]:
    # Materiality is gated on magnitude ONLY. A band boundary (e.g. 40) sitting
    # between two near-identical scores must NOT turn a 2-3 point probe jitter
    # into "improved" — that is exactly the run-to-run noise this layer exists to
    # suppress. A genuine >= MATERIAL_SCORE_DELTA move that also crosses a band is
    # still caught; the band only describes the move, it never triggers it.
    material = (
        prior is not None
        and current is not None
        and abs(current - prior) >= MATERIAL_SCORE_DELTA
    )
    direction = "stable"
    if material and current is not None and prior is not None:
        direction = "improved" if current > prior else "regressed"
    return {
        "signal": signal,
        "label": SIGNAL_LABELS[signal],
        "from": prior,
        "to": current,
        "direction": direction,
        "is_material": material,
    }


def _categorical_movement(
    *,
    signal: str,
    prior: Optional[str],
    current: Optional[str],
) -> Dict[str, Any]:
    changed = bool(prior and current and prior != current)
    return {
        "signal": signal,
        "label": SIGNAL_LABELS[signal],
        "from": prior,
        "to": current,
        "direction": "changed" if changed else "stable",
        "is_material": changed,
    }


def _headline(movements: List[Mapping[str, Any]], days_since: Optional[int]) -> str:
    material = [
        movement for movement in movements
        if movement.get("is_material")
    ]
    day_phrase = _day_phrase(days_since)
    if not material:
        return f"No material change since your last audit{day_phrase} — keep the current plan running."

    improved = [
        str(m.get("label") or "").strip()
        for m in material
        if m.get("direction") == "improved" and str(m.get("label") or "").strip()
    ]
    regressed = [
        str(m.get("label") or "").strip()
        for m in material
        if m.get("direction") == "regressed" and str(m.get("label") or "").strip()
    ]
    changed = [
        str(m.get("label") or "").strip()
        for m in material
        if m.get("direction") == "changed" and str(m.get("label") or "").strip()
    ]
    parts: List[str] = []
    if improved:
        parts.append(f"improved: {_phrase(improved)}")
    if regressed:
        parts.append(f"regressed: {_phrase(regressed)}")
    if changed:
        parts.append(f"changed: {_phrase(changed)}")
    return f"Material change since your last audit{day_phrase}: {'; '.join(parts)}."


def _tracked_metric_result(
    metric: str,
    movements: List[Mapping[str, Any]],
) -> Dict[str, str]:
    mapped = _metric_signals(metric)
    if not mapped:
        return _not_measurable(
            metric,
            "This metric needs data that is not stored in audit history yet.",
        )
    by_signal = {
        str(movement.get("signal")): movement
        for movement in movements
        if movement.get("signal")
    }
    available = [by_signal[s] for s in mapped if s in by_signal]
    if not available:
        return _not_measurable(
            metric,
            "The comparable audit signal is not present in stored history.",
        )
    if any(movement.get("is_material") for movement in available):
        labels = [
            str(movement.get("label") or movement.get("signal"))
            for movement in available
            if movement.get("is_material")
        ]
        return {
            "metric": metric,
            "status": "moved",
            "note": f"Mapped to material movement in {_phrase(labels)}.",
        }
    labels = [
        str(movement.get("label") or movement.get("signal"))
        for movement in available
    ]
    return {
        "metric": metric,
        "status": "unchanged",
        "note": f"Mapped to {_phrase(labels)}; no material movement.",
    }


def _metric_signals(metric: str) -> List[str]:
    text = str(metric or "").lower()
    if any(token in text for token in ("checkout", "orders", "instrumented", "store and payment", "indexed")):
        return []
    signals: List[str] = []
    if "first-party citation" in text or "official pdp facts" in text:
        signals.append("attribution")
    if "category visibility" in text:
        signals.append("category_visibility")
    if any(
        token in text
        for token in (
            "cited buying paths",
            "losing share",
            "cited path",
            "cited hosts",
            "hosts entering",
            "retailer",
            "marketplace",
            "controller",
        )
    ):
        signals.extend(["top_controller", "controller_archetype"])
    if "visibility" in text and not signals:
        signals.append("visibility")
    return _dedupe(signals)


def _tracking_metrics(report: Mapping[str, Any]) -> List[str]:
    nba = _next_best_action(report)
    raw = nba.get("tracking_metrics") or nba.get("how_to_track") or []
    out: List[str] = []
    for item in _as_list(raw):
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _top_controller(
    report: Mapping[str, Any],
    *,
    play: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> Optional[str]:
    candidates = [
        play.get("controllers"),
        evidence.get("retailer_hosts"),
        evidence.get("source_hosts"),
        evidence.get("cited_hosts"),
        _as_mapping(_merchant_view(report).get("receipts")).get("top_cited_hosts"),
        _as_mapping(_merchant_view(report).get("receipts")).get("cited_hosts_detailed"),
        _as_mapping(report.get("verdict")).get("buyer_path_verdict", {}),
        _as_mapping(report.get("aggregate")).get("buyer_path_verdict", {}),
    ]
    for value in candidates:
        host = _first_host(value)
        if host:
            return host
    return None


def _operator_move_controller_strategy(nba: Mapping[str, Any]) -> Optional[str]:
    for move in _as_list(nba.get("operator_moves")):
        if not isinstance(move, Mapping):
            continue
        evidence = _as_mapping(move.get("evidence"))
        strategy = _clean(evidence.get("controller_strategy"))
        if strategy:
            return strategy
    return None


def _first_host(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        for key in ("host", "domain"):
            text = _clean(value.get(key))
            if text:
                return text
        for key in ("top_controllers", "controllers", "hosts"):
            host = _first_host(value.get(key))
            if host:
                return host
        return None
    for item in _as_list(value):
        if isinstance(item, Mapping):
            host = _first_host(item)
        else:
            host = _clean(item)
        if host:
            return host
    return None




def _day_phrase(days_since: Optional[int]) -> str:
    if days_since is None:
        return ""
    unit = "day" if days_since == 1 else "days"
    return f" {days_since} {unit} ago"


def _not_measurable(metric: str, note: str) -> Dict[str, str]:
    return {"metric": metric, "status": "not_measurable", "note": note}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_clean(*values: Any) -> Optional[str]:
    for value in values:
        text = _clean(value)
        if text:
            return text
    return None


def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _dedupe(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _phrase(values: List[str]) -> str:
    cleaned = [v for v in values if v]
    if not cleaned:
        return "the mapped signal"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"
