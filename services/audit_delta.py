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
# The only noise floor we have actually MEASURED. Three replicate runs at
# temperature 0 over the same queries (docs/revenue-recovery-joy-spike-
# 2026-08-31.md §4, docs/revenue-recovery-geo-evidence-base.md):
#
#   basis            n     95% CI      min detectable change
#   1 run            44    ±14.6 pts   ~20.5 pts
#   3 runs           131   ±8.5 pts    ~11.9 pts
#   9 runs           393   ±4.9 pts    ~6.9 pts
#
# Three runs of the SAME queries spread 63.6% / 52.3% / 53.5% — 11.3 points from
# noise alone. 15 sits just above the 3-run detection limit, which is why it is
# the threshold.
MATERIAL_SCORE_DELTA = 15

# W2 pinned basis. This was 5, on the argument that pinning the prompt set
# removes prompt-regeneration noise so a smaller move is already real signal.
# The argument is true and does not license the number: the floor above was
# measured at temperature 0 over IDENTICAL queries — the pinned-basis condition
# itself. Pinning removes a DIFFERENT variance component (which questions got
# asked); it does nothing to the response variance that produced the 11.3-point
# spread. So 5 asserted materiality at under half the smallest change we have
# ever been able to detect, and did it specifically on the runs we tell the
# merchant are the most comparable.
#
# Worse for a run like the live Anuko audit: its basis carries 45
# brand-mentioned responses against the study's 131, so its true floor is WIDER
# than ±8.5, nearer the 1-run ±14.6.
#
# Same-basis is still worth stamping — it licenses the narrative "you moved
# X → Y is a real comparison, the questions did not change" — it just does not
# license a tighter threshold. To earn one, measure it: replicate runs on a
# PINNED set, and derive the floor from that run's own n. Until then this
# stays equal to the loose threshold rather than being a number we like.
MATERIAL_SCORE_DELTA_SAME_BASIS = MATERIAL_SCORE_DELTA


def build_reaudit_delta(
    *,
    current_report: Mapping[str, Any],
    prior_report: Optional[Mapping[str, Any]],
    prior_row: Optional[Mapping[str, Any]],
    days_since: Optional[int],
    current_basis: Optional[Mapping[str, Any]] = None,
    prior_basis: Optional[Mapping[str, Any]] = None,
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
            "measurement_basis": _measurement_basis(
                current_report, prior_report, current, {},
                current_basis, prior_basis,
            ),
            "tracked_metric_results": [
                _not_measurable(
                    metric,
                    "No prior audit exists yet; this run is the baseline.",
                )
                for metric in metrics
            ],
        }

    # Resolve the measurement basis BEFORE diffing scores: if this run and the
    # prior one were measured on the same pinned prompt set, a smaller move counts
    # as material (W2). Categorical movements are exact-match and unaffected.
    basis = _measurement_basis(
        current_report, prior_report, current, prior, current_basis, prior_basis,
    )
    material_delta = (
        MATERIAL_SCORE_DELTA_SAME_BASIS
        if basis.get("same") is True
        else MATERIAL_SCORE_DELTA
    )

    movements: List[Dict[str, Any]] = []
    prior_scores = _scores(prior)
    current_scores = _scores(current)
    for signal in SCORE_SIGNALS:
        movements.append(
            _score_movement(
                signal=signal,
                prior=prior_scores.get(signal),
                current=current_scores.get(signal),
                material_delta=material_delta,
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
        # W2 pinned basis: is this delta measured against the SAME prompt set
        # as the prior run? same=True licenses "you moved X→Y" as a real
        # comparison AND tightens the materiality threshold to
        # MATERIAL_SCORE_DELTA_SAME_BASIS; same=False means the basis changed
        # (explicit refresh / generator version bump) and score movement partly
        # reflects the new questions; same=None means one side predates stamping.
        "measurement_basis": basis,
        "tracked_metric_results": [
            _tracked_metric_result(metric, movements) for metric in metrics
        ],
    }


def measurement_basis_between(
    current_report: Optional[Mapping[str, Any]],
    prior_report: Optional[Mapping[str, Any]],
    current_basis: Optional[Mapping[str, Any]] = None,
    prior_basis: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The W2 measurement-basis verdict for an arbitrary report pair, exactly
    as build_reaudit_delta computes it — for callers (e.g. the per-SKU
    outreach-outcomes attach) that need the basis without the score-delta
    layer. Single source of truth: any consumer gating query-level claims on
    "same pinned prompt set?" must go through here (or build_reaudit_delta),
    never re-derive."""
    return _measurement_basis(
        current_report,
        prior_report,
        _primary_report(current_report),
        _primary_report(prior_report),
        current_basis,
        prior_basis,
    )


def _basis_id(basis: Mapping[str, Any]) -> Optional[str]:
    """The strongest measurement-basis identity a `prompt_basis` block carries:
    W2.1 `selected_set_id` (the FULL probed set — the true "measured the same
    way?" key) when present, else W2 `prompt_set_id` (the LLM lists only)."""
    if not isinstance(basis, Mapping):
        return None
    return (
        str(basis.get("selected_set_id") or "")
        or str(basis.get("prompt_set_id") or "")
        or None
    )


def _prompt_set_id(
    full_report: Optional[Mapping[str, Any]],
    primary: Mapping[str, Any],
) -> Optional[str]:
    """The pinned basis identity for a report, tolerant of both shapes: the
    per-product/primary report carrying `prompt_basis` directly, or the full
    payload carrying it under brand_report.per_sku_reports[0]. Prefers the
    W2.1 selected-set identity over the W2 LLM-list identity."""
    from_primary = _basis_id(primary.get("prompt_basis"))
    if from_primary:
        return from_primary
    report = full_report if isinstance(full_report, Mapping) else {}
    brand = report.get("brand_report")
    if isinstance(brand, Mapping):
        report = brand
    for sku_report in report.get("per_sku_reports") or []:
        if not isinstance(sku_report, Mapping):
            continue
        basis_id = _basis_id(sku_report.get("prompt_basis"))
        if basis_id:
            return basis_id
    return None


def _measurement_basis(
    current_full: Optional[Mapping[str, Any]],
    prior_full: Optional[Mapping[str, Any]],
    current_primary: Mapping[str, Any],
    prior_primary: Mapping[str, Any],
    current_basis: Optional[Mapping[str, Any]] = None,
    prior_basis: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    current_id = _prompt_set_id(current_full, current_primary)
    prior_id = _prompt_set_id(prior_full, prior_primary or {})
    if not prior_primary:
        return {
            "same": None,
            "prompt_set_id": current_id,
            "note": "This run establishes the measurement basis for future comparisons.",
        }
    if not current_id or not prior_id:
        return {
            "same": None,
            "prompt_set_id": current_id,
            "note": (
                "One of the compared runs predates prompt-basis pinning, so "
                "basis identity can't be asserted for this delta."
            ),
        }
    same = current_id == prior_id
    if not same:
        return {
            "same": False,
            "prompt_set_id": current_id,
            "note": (
                "The prompt set changed between these runs (measurement basis "
                "refreshed), so score movement partly reflects the new questions."
            ),
        }

    # The prompt set matched. That is necessary but NOT sufficient: the prompt
    # set says WHAT was asked, and says nothing about which model answered, at
    # what temperature, against which official-domain set, or with what tier
    # mix. Any of those moves the score with no merchant behaviour change —
    # measured 2026-09-01, a model generation moved No-Destination 20.9% -> 0.0%
    # and multi-host 50% -> 86%. Before this, such a run reported same=True and
    # so tightened the materiality mask from 15 points to 5, which is the
    # direction that manufactures movement rather than hiding it.
    #
    # Absent basis rows fall through to the prompt-set verdict unchanged: runs
    # that predate audit_basis carry no evidence of a model change either way,
    # and failing them closed would silently desensitise every merchant's next
    # re-audit. This is strictly additive — it can only ever turn a True into a
    # False, never the reverse.
    if isinstance(current_basis, Mapping) and isinstance(prior_basis, Mapping):
        from db.audit_basis import bases_are_comparable

        if not bases_are_comparable(current_basis, prior_basis):
            return {
                "same": False,
                "prompt_set_id": current_id,
                "basis_divergence": "measurement_basis",
                "note": (
                    "The same questions were asked, but something else about "
                    "how this audit was measured changed (the model, the "
                    "official-domain set, the question mix or the market), so "
                    "score movement partly reflects the new measurement rather "
                    "than your store."
                ),
            }

    return {
        "same": True,
        "prompt_set_id": current_id,
        "note": (
            "Measured on the same prompt set as your last audit — score "
            "movement is a real comparison."
        ),
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
    # Per-SKU brand report (the production wedge shape) — same treatment
    # _prompt_set_id already got; without this branch every movement read
    # None scores and the delta shipped dead (review P0, wave-1).
    per_sku = report.get("per_sku_reports")
    if isinstance(per_sku, list):
        for item in per_sku:
            if isinstance(item, Mapping):
                return dict(item)
        return {}
    return dict(report)


def _merchant_view(report: Mapping[str, Any]) -> Mapping[str, Any]:
    mv = report.get("merchant_view")
    return mv if isinstance(mv, Mapping) else {}


def _next_best_action(report: Mapping[str, Any]) -> Mapping[str, Any]:
    nba = _merchant_view(report).get("next_best_action")
    if not isinstance(nba, Mapping):
        # Per-SKU report entries carry next_best_action at the TOP level
        # (they have no merchant_view).
        nba = report.get("next_best_action")
    return nba if isinstance(nba, Mapping) else {}


def _scores(report: Mapping[str, Any]) -> Dict[str, Optional[int]]:
    headline = _as_mapping(_merchant_view(report).get("headline"))
    headline_scores = _as_mapping(headline.get("scores"))
    verdict = _as_mapping(report.get("verdict"))
    category = _as_mapping(report.get("category_visibility"))
    out = {
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
    if all(v is None for v in out.values()):
        # Per-SKU report entry: mirror _per_sku_run_aggregate's documented
        # semantics — visibility = weakest dimension, attribution = citation
        # dimension, category not measured in per-SKU mode.
        dims = _as_mapping(report.get("scores"))
        values = [
            payload.get("score")
            for payload in dims.values()
            if isinstance(payload, Mapping)
            and isinstance(payload.get("score"), (int, float))
        ]
        if values:
            out["visibility"] = _int_or_none(min(values))
            citation = _as_mapping(dims.get("citation"))
            out["attribution"] = _int_or_none(citation.get("score"))
    return out


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
            # Per-SKU entry: band_display.label is the merchant-safe verdict.
            _as_mapping(report.get("band_display")).get("label"),
        ),
    }


def _score_movement(
    *,
    signal: str,
    prior: Optional[int],
    current: Optional[int],
    material_delta: int = MATERIAL_SCORE_DELTA,
) -> Dict[str, Any]:
    # Materiality is gated on magnitude ONLY. A band boundary (e.g. 40) sitting
    # between two near-identical scores must NOT turn a 2-3 point probe jitter
    # into "improved" — that is exactly the run-to-run noise this layer exists to
    # suppress. A genuine >= material_delta move that also crosses a band is
    # still caught; the band only describes the move, it never triggers it.
    # `material_delta` is tightened to MATERIAL_SCORE_DELTA_SAME_BASIS by the caller
    # when the run was measured on the same pinned prompt set (W2).
    observed = (
        abs(current - prior)
        if prior is not None and current is not None
        else None
    )
    material = observed is not None and observed >= material_delta
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
        # `direction: "stable"` is a POSITIVE claim — "this did not move" — and
        # for every sub-threshold delta it is the wrong one. What we know is
        # that the move is smaller than the smallest change this basis can
        # resolve. A merchant told "stable" after a real 12-point drop has been
        # told something false; told "below what we can detect (±15)" they have
        # been told the truth, and can ask for a denser basis if they need a
        # finer answer. Renderers keep reading `is_material` as before.
        "detection": {
            "observed_delta": observed,
            "threshold": material_delta,
            "verdict": (
                "not_comparable" if observed is None
                else "resolved" if material
                else "below_detection_floor"
            ),
            "basis_note": (
                "Three replicate runs at temperature 0 over the same queries "
                "spread 11.3 points; the smallest change a 3-run basis can "
                "resolve is ~11.9 points. A move under this threshold is not "
                "evidence of stability — it is a move we cannot see."
            ),
        },
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
    if days_since == 0:
        # Same-day re-runs happen (verification, weekly + manual) — "0 days
        # ago" read like a bug.
        return " earlier today"
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
