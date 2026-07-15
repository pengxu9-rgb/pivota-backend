"""Report Summary Contract v1 — one condensed summary layer over the per-SKU
brand report, consumed by three renderers (portal 3-page condensed view, PPT
export, homepage hero). See docs/report_summary_contract_v1_2026-07-15.md.

Pure + defensive, like merchant_narrative_builder: every section degrades to
None/[] rather than fabricating. All prose (verdict headline, finding
summaries, action copy) is reused VERBATIM from the already-built narrative
layer — this module never authors a new claim, so every renderer tells the
same story the full report tells.

Honesty rule for supporting prompts: an action only carries supporting
prompt-level evidence when a real join exists —
  - "evidence_used": the failing prompts the action builder itself consumed
    (next_best_action.evidence_used.failing_prompt_examples, or the SKU's
    top-level failing_prompts, which build_sku_next_best_action classified
    the gap from);
  - "none": no join — the action ships with an empty list, never an inferred
    or LLM-generated linkage.
"""

from typing import Any, Dict, List, Mapping, Optional, Tuple

from services.win_plan_builder import interleave_by_provider, is_broad_head_query

# 1.1: supporting-prompt selection is niche-first (broad head terms are only
# shown when they are literally all that was measured) + rows carry
# prompt_source so renderers can badge spec-matched evidence.
# 1.2: calibration decision (a)+(c) — dimensions the run CANNOT measure
# (URL wedge without a connected catalog: routability) are excluded from the
# weakest-link score, and the score block carries a prewritten `explainer`
# (+ weakest_dimension / unmeasured_excluded) for the ⓘ popover. Unmeasurable
# is not zero; the displayed score only counts what was actually measured.
CONTRACT_VERSION = "1.2"

# Display banding on the 0-100 raw scale, mirrored onto the 0-10 display
# ("6 = pass"). Anchor calibration is an OPEN decision (contract doc §7) —
# thresholds live here in one place so the flip is a constant change. Raw
# scores are never rescaled or inflated; only the display layer reads these.
_BAND_THRESHOLDS = (60.0, 75.0, 90.0)  # pass / good / excellent cutoffs

_TOP_ACTIONS_CAP = 3
_TOP_FINDINGS_CAP = 3
_SUPPORTING_PROMPTS_CAP = 3
_SNAPSHOT_CAP = 8


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _display_score(raw: Any) -> Optional[float]:
    """0-100 raw → 0-10 display at ONE decimal. Never integer-round: the
    do-action→score-moves feedback loop needs 42→47 to render as 4.2→4.7."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return round(value / 10.0, 1)


def _band_for(raw: Any) -> Optional[str]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value >= _BAND_THRESHOLDS[2]:
        return "excellent"
    if value >= _BAND_THRESHOLDS[1]:
        return "good"
    if value >= _BAND_THRESHOLDS[0]:
        return "pass"
    return "needs_work"


def _score_payload(raw: Any) -> Dict[str, Any]:
    return {
        "raw": raw,
        "display": _display_score(raw),
        "scale_max": 10,
        "band": _band_for(raw),
    }


_DIMENSION_LABELS = {
    "citation": "AI citations",
    "identity": "product identity",
    "content_richness": "content richness",
    "routability": "routability",
}


def _dimension_label(key: str) -> str:
    return _DIMENSION_LABELS.get(key, str(key).replace("_", " "))


def _measurable_dimensions(
    sku_report: Mapping[str, Any],
    unmeasured: Tuple[str, ...],
) -> Dict[str, float]:
    """Numeric dimension scores minus the ones this run type cannot measure.
    Unmeasurable != zero: a URL-wedge run has no catalog, so catalog-dependent
    dimensions (routability) score near-zero for lack of signal, not lack of
    merit — counting them in the weakest-link overall punished merchants for
    something the run could not see (calibration decision a)."""
    scores = _as_dict(sku_report.get("scores"))
    out: Dict[str, float] = {}
    for key, payload in scores.items():
        if key in unmeasured:
            continue
        if isinstance(payload, dict) and isinstance(
            payload.get("score"), (int, float)
        ):
            out[str(key)] = float(payload["score"])
    return out


def _recomputed_overall(
    per_sku_reports: List[Dict[str, Any]],
    unmeasured: Tuple[str, ...],
) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    """(brand raw, weakest-dimension descriptor) with unmeasured dims
    excluded. Brand raw = mean over SKUs of min(measurable dims) — the same
    weakest-link semantics as _overall_score, minus what wasn't measurable.
    Weakest descriptor = the argmin dimension of the lowest-scoring SKU."""
    overalls: List[float] = []
    weakest: Optional[Dict[str, Any]] = None
    for report in per_sku_reports:
        dims = _measurable_dimensions(report, unmeasured)
        if not dims:
            continue
        key = min(dims, key=lambda k: dims[k])
        value = dims[key]
        overalls.append(value)
        if weakest is None or value < float(weakest["raw"]):
            weakest = {
                "key": key,
                "label": _dimension_label(key),
                "raw": value,
                "display": _display_score(value),
            }
    if not overalls:
        return None, None
    return round(sum(overalls) / len(overalls), 2), weakest


def _score_explainer(
    weakest: Optional[Mapping[str, Any]],
    excluded: Tuple[str, ...],
) -> Optional[str]:
    """Prewritten copy for the score's ⓘ popover (calibration decision c).
    States the weakest-link method, names this run's weakest measured
    dimension, and discloses what was NOT counted and why — never a number
    the run didn't measure."""
    if not weakest:
        return None
    parts = [
        "How this score works: each product is scored on its weakest "
        "measured dimension, and the brand score averages your products. "
        f"Weakest measured dimension this run: {weakest.get('label')} "
        f"({weakest.get('display')}/10)."
    ]
    if excluded:
        labels = ", ".join(_dimension_label(k) for k in excluded)
        parts.append(
            f"Not counted: {labels} — these signals can't be measured "
            "without a connected catalog. Connect your store to measure "
            "and improve them."
        )
    return " ".join(parts)


def _score_block(
    brand_rollup: Mapping[str, Any],
    per_sku_reports: Optional[List[Dict[str, Any]]] = None,
    unmeasured: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    run_scores = _as_dict(brand_rollup.get("run_scores"))
    raw = run_scores.get("avg_visibility")
    raw_persisted = raw
    weakest: Optional[Dict[str, Any]] = None
    excluded_applied: Tuple[str, ...] = ()
    if per_sku_reports:
        # weakest is always derived (feeds the explainer); the persisted raw
        # is only OVERRIDDEN when the caller declared unmeasurable dimensions
        # — default () keeps historical numbers byte-identical.
        recomputed, weakest = _recomputed_overall(per_sku_reports, unmeasured)
        if unmeasured and recomputed is not None:
            raw = recomputed
            excluded_applied = tuple(unmeasured)
    subscores = []
    for key, source in (
        ("visibility", "avg_visibility"),
        ("attribution", "avg_attribution"),
        ("category_visibility", "avg_category_visibility"),
    ):
        value = run_scores.get(source)
        if value is None:
            continue
        subscores.append(
            {"key": key, "raw": value, "display": _display_score(value)}
        )
    history = _as_dict(_as_dict(brand_rollup.get("tracking")).get("history"))
    delta_map = _as_dict(history.get("delta_from_most_recent"))
    delta = None
    if delta_map.get("visibility") is not None:
        delta = {
            "raw": delta_map.get("visibility"),
            "previous_audit_run_id": _as_dict(
                history.get("most_recent_audit")
            ).get("run_id"),
            "days_since_last_audit": delta_map.get("days_since_last_audit"),
        }
    out = _score_payload(raw)
    out["band_thresholds"] = [t / 10.0 for t in _BAND_THRESHOLDS]
    out["subscores"] = subscores
    # The persisted run-over-run delta compares OLD-semantics numbers; once
    # exclusions actually change the displayed score the comparison is
    # apples-to-oranges, so it's dropped rather than shown wrong.
    out["delta"] = (
        None if (excluded_applied and raw != raw_persisted) else delta
    )
    out["weakest_dimension"] = weakest
    out["unmeasured_excluded"] = list(excluded_applied)
    out["explainer"] = _score_explainer(weakest, excluded_applied)
    return out


def _prompt_evidence(entry: Any) -> Optional[Dict[str, Any]]:
    """One supporting-prompt row, tolerant of both shapes it can arrive in:
    the SKU's top-level failing_prompts (has axis + evidence_run_id) and the
    action's evidence_used.failing_prompt_examples chips (has
    competitors_named). Only fields the probe actually measured — no
    re-summarization."""
    row = _as_dict(entry)
    query = str(row.get("query") or "").strip()
    if not query:
        return None
    out: Dict[str, Any] = {"query": query}
    for key in (
        "axis",
        "provider",
        "reason",
        "evidence_run_id",
        "competitors_named",
        "prompt_source",
    ):
        value = row.get(key)
        if value not in (None, "", []):
            out[key] = value
    return out


def _supporting_prompts(
    next_best_action: Mapping[str, Any],
    sku_report: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], str]:
    """Attach prompt-level evidence to an action via a REAL join only.
    Preference order: the failing-prompt chips embedded in the action's own
    evidence, then the SKU's top-level failing_prompts (the exact list
    build_sku_next_best_action classified the gap from). Both are
    'evidence_used'; anything else is 'none' + empty."""
    evidence = _as_dict(next_best_action.get("evidence_used"))
    # UNION of both sources (chips lead for dedup precedence): the chips are
    # a [:5] slice of a provider-GROUPED list, so alone they can be single-
    # engine (live Mojawa run: 5/5 Gemini while ChatGPT losses existed).
    # failing_prompts is the same measured population, so merging stays
    # 'evidence_used'; dedup on (query, provider).
    merged: List[Dict[str, Any]] = []
    seen = set()
    for candidate in (
        evidence.get("failing_prompt_examples"),
        sku_report.get("failing_prompts"),
    ):
        for entry in _as_list(candidate):
            prompt = _prompt_evidence(entry)
            if not prompt:
                continue
            key = (
                prompt["query"].lower(),
                str(prompt.get("provider") or "").lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(prompt)
    if not merged:
        return [], "none"
    # Niche-first, then provider round-robin so the cap shows every engine
    # that measured a loss, not just the one that sorts first.
    pool = interleave_by_provider(_niche_first(merged))
    return pool[:_SUPPORTING_PROMPTS_CAP], "evidence_used"


def _niche_first(prompts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Never showcase broad head terms ("best headphones") as an action's
    evidence while spec-matched losses exist: mid/long-tail brands win NICHE
    prompts that match their product's attributes — head terms are big-budget
    turf. Shares the win plan's classifier (one definition of "head"). Head
    rows survive only when they are literally all that was measured — some
    honest evidence beats none."""
    niche = [
        p
        for p in prompts
        if not is_broad_head_query(
            p.get("query"), prompt_source=p.get("prompt_source")
        )
    ]
    return niche if niche else prompts


def _match_sku_report(
    action: Mapping[str, Any],
    per_sku_reports: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Re-find the per-SKU report a prioritized action came from.
    _prioritized_actions built each row from exactly one report's
    next_best_action, so headline equality is an exact join; sku_title is the
    fallback when headlines were deduped across SKUs."""
    headline = str(action.get("headline") or "").strip()
    action_gap = str(action.get("primary_gap") or "").strip().lower()
    for report in per_sku_reports:
        nba = _as_dict(report.get("next_best_action"))
        if headline and str(nba.get("headline") or "").strip() == headline:
            nba_gap = str(nba.get("primary_gap") or "").strip().lower()
            # Producer dedup keys actions on (gap, headline) — mirror it so a
            # headline shared across two gaps can't attach the wrong SKU's
            # evidence. Missing gap on either side degrades to headline-only.
            if action_gap and nba_gap and action_gap != nba_gap:
                continue
            return report
    sku_title = str(action.get("sku_title") or "").strip()
    for report in per_sku_reports:
        if sku_title and str(report.get("sku_title") or "").strip() == sku_title:
            return report
    return {}


def _top_actions(
    narrative: Mapping[str, Any],
    per_sku_reports: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for action in _as_list(narrative.get("prioritized_actions"))[
        :_TOP_ACTIONS_CAP
    ]:
        if not isinstance(action, dict):
            continue
        sku_report = _match_sku_report(action, per_sku_reports)
        nba = _as_dict(sku_report.get("next_best_action"))
        prompts, basis = _supporting_prompts(nba, sku_report)
        cta = _as_dict(nba.get("cta"))
        out.append(
            {
                "action_id": None,  # Surface B FK, absent on wedge reports
                "headline": action.get("headline"),
                "why_this_first": action.get("why_this_first")
                or nba.get("why_this_first"),
                "first_move": action.get("first_move") or nba.get("first_move"),
                "evidence_summary": nba.get("evidence_summary"),
                "how_to_track": _as_list(nba.get("how_to_track"))[:2],
                "primary_gap": action.get("primary_gap"),
                "growth_phase": action.get("growth_phase"),
                "sku_title": action.get("sku_title"),
                "target_sku_key": cta.get("target_sku_key")
                or sku_report.get("sku_key"),
                "cta": cta or None,
                "supporting_prompts": prompts,
                "supporting_prompts_basis": basis,
            }
        )
    return out


def _top_findings(narrative: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Findings are the narrative's pre-written sections mapped into summary
    slots — verbatim text, never re-authored here."""
    findings: List[Dict[str, Any]] = []
    losing = _as_dict(narrative.get("where_youre_losing"))
    if losing.get("summary"):
        findings.append(
            {
                "finding_id": None,
                "kind": "independent_endorsement",
                "title": "Who AI recommends instead",
                "severity": (
                    "info"
                    if losing.get("independently_recommended_for_category")
                    else "high"
                ),
                "evidence_summary": losing.get("summary"),
                "supporting_prompts": [],
            }
        )
    working = _as_dict(narrative.get("whats_working"))
    if working.get("summary"):
        findings.append(
            {
                "finding_id": None,
                "kind": "findability",
                "title": "What's already working",
                "severity": "info",
                "evidence_summary": working.get("summary"),
                "supporting_prompts": [],
            }
        )
    verify = _as_dict(narrative.get("verify_summary_plain"))
    if verify.get("text"):
        flagged = 0
        try:
            flagged = int(verify.get("flagged") or 0)
        except (TypeError, ValueError):
            pass
        findings.append(
            {
                "finding_id": None,
                "kind": "answer_quality",
                "title": "Are AI answers about you accurate?",
                "severity": "medium" if flagged > 0 else "info",
                "evidence_summary": verify.get("text"),
                "supporting_prompts": [],
            }
        )
    return findings[:_TOP_FINDINGS_CAP]


def _competitive_snapshot(narrative: Mapping[str, Any]) -> Dict[str, Any]:
    who = _as_dict(
        _as_dict(narrative.get("where_youre_losing")).get(
            "who_ai_cites_instead"
        )
    )
    return {
        "available": bool(who.get("available")),
        "top_cited_hosts": [
            host
            for host in (
                _as_dict(row).get("host")
                for row in _as_list(who.get("cited_hosts"))
            )
            if host
        ][:_SNAPSHOT_CAP],
        "competitors_named": [
            name
            for name in (
                _as_dict(row).get("name")
                for row in _as_list(who.get("competitors"))
            )
            if name
        ][:_SNAPSHOT_CAP],
        "note": who.get("note"),
    }


def _sku_summary(
    report: Mapping[str, Any],
    unmeasured: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """Per-SKU condensed row for the 3-page per-product view. Overall raw =
    the SKU's weakest MEASURED dimension (mirrors _overall_score semantics,
    minus dimensions this run type cannot measure — see _score_block)."""
    dims = _measurable_dimensions(report, unmeasured)
    raw = min(dims.values()) if dims else None
    nba = _as_dict(report.get("next_best_action"))
    prompts, basis = _supporting_prompts(nba, report)
    sku_score = _score_payload(raw)
    # No contract band at SKU level: the per-SKU card's band (band_display,
    # thresholds in _band_for_score) and the contract band (_band_for) use
    # different cutoffs, so emitting both let one product read "pass" and
    # "Needs work" at once. band_display stays the single per-SKU authority
    # until the calibration decision (doc §7) reconciles the two ladders.
    sku_score.pop("band", None)
    return {
        "sku_key": report.get("sku_key"),
        "sku_title": report.get("sku_title"),
        "score": sku_score,
        # Existing merchant-safe copy ({band, label, meaning}) — reused so the
        # summary can never disagree with the full per-SKU card.
        "band_display": _as_dict(report.get("band_display")) or None,
        "primary_gap": nba.get("primary_gap"),
        "action_headline": nba.get("headline"),
        "supporting_prompts": prompts,
        "supporting_prompts_basis": basis,
    }


def build_report_summary(
    report: Mapping[str, Any],
    *,
    unmeasured_dimensions: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """Assemble the contract summary from an already-built per-SKU brand
    report (report_jsonb shape). `unmeasured_dimensions` names dimensions the
    RUN TYPE cannot measure (URL wedge without a catalog: routability) — they
    are excluded from the displayed weakest-link score and disclosed in the
    explainer. Default () keeps historical semantics byte-identical."""
    report = _as_dict(report)
    narrative = _as_dict(report.get("merchant_narrative"))
    brand_rollup = _as_dict(report.get("brand_rollup"))
    per_sku_reports = [
        r for r in _as_list(report.get("per_sku_reports")) if isinstance(r, dict)
    ]
    actions = _top_actions(narrative, per_sku_reports)
    prioritized_total = len(_as_list(narrative.get("prioritized_actions")))
    return {
        "contract_version": CONTRACT_VERSION,
        "audit_run_id": report.get("audit_run_id"),
        "generated_at": report.get("timestamp"),
        "subject": {
            "type": "brand",
            "merchant_id": report.get("merchant_id"),
            "merchant_name": report.get("merchant_name"),
        },
        "score": _score_block(
            brand_rollup, per_sku_reports, unmeasured_dimensions
        ),
        "verdict": {
            "headline": narrative.get("headline_story"),
            "label": narrative.get("verdict_label")
            or brand_rollup.get("brand_verdict_label")
            or report.get("brand_verdict_label"),
            "explanation": narrative.get("verdict_explanation")
            or report.get("brand_verdict_explanation"),
            "primary_gap": actions[0].get("primary_gap") if actions else None,
        },
        "top_findings": _top_findings(narrative),
        "top_actions": actions,
        "competitive_snapshot": _competitive_snapshot(narrative),
        "sku_summaries": [
            _sku_summary(r, unmeasured_dimensions) for r in per_sku_reports
        ],
        "meta": {
            "source": "per_sku_brand_report",
            "audit_mode": report.get("audit_mode"),
            "providers": _as_list(report.get("providers")),
            "verify_providers": _as_list(report.get("verify_providers")),
            "products_audited": len(per_sku_reports),
            # Truncation disclosed, never silent (contract §3): how many
            # actions existed before the top-3 cap.
            "actions_total": prioritized_total,
            # The narrative's pre-written coverage/limits disclosures — the
            # PPT's methodology slide reads these verbatim.
            "honest_limits": _as_list(narrative.get("honest_limits")),
        },
    }
