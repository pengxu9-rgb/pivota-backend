"""Deterministic brand-level next-best-action projection.

This module consumes the already-built merchant_view evidence/action
inventory and returns one merchant-facing prescription. It does not call
LLMs, does not recompute audit scores, and deliberately avoids per-SKU
rollups or content-revision wiring.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple


logger = logging.getLogger(__name__)

PRIMARY_INTEGRATION_COMPLETION = "integration_completion_gap"
PRIMARY_RETRIEVAL_FOUNDATION = "retrieval_foundation_gap"
PRIMARY_RETAILER_ROUTE_LEAK = "retailer_route_leak"
PRIMARY_CATEGORY_DISCOVERY = "category_discovery_gap"
PRIMARY_COMPETITOR_SOURCE = "competitor_source_gap"
PRIMARY_FIRST_PARTY_DEFENSE = "first_party_defense"

PRIMARY_SKU_OPEN_LANE_CAPTURE = "open_lane_capture"
PRIMARY_SKU_SUBSTITUTION_LEAK = "substitution_leak"
PRIMARY_SKU_CONTENT_REVISION_GAP = "content_revision_gap"
PRIMARY_SKU_SOURCE_ROUTE_REPAIR = "source_route_repair"
PRIMARY_SKU_PROTECTED_MONITORING = "protected_monitoring"
PRIMARY_SKU_INSUFFICIENT_DATA = "insufficient_data"

_VERDICT_INVISIBLE = "INVISIBLE"
_VERDICT_VIA_RETAILERS = "VISIBLE VIA RETAILERS"
_VERDICT_MISATTRIBUTED = "VISIBLE BUT MISATTRIBUTED"
_VERDICT_CATEGORY_MENTION_NO_FIRST_PARTY = "CATEGORY MENTION, NO FIRST-PARTY"
_VERDICT_STRONG = "STRONG"

_RETAIL_HOST_TYPES = {"retailer", "marketplace"}
_SOURCE_HOST_TYPES = {
    "community",
    "editorial",
    "forum",
    "publisher",
    "reddit",
    "social",
    "video",
}
_LOW_CONFIDENCE_HOST_TYPES = {"cdn", "unclassified"}
_REQUIRED_INTEGRATION_PIECES = {"store_platform", "psp"}
_SEVERITY_RANK = {"critical": 5, "high": 4, "medium": 3, "low": 2}
_SKU_REPAIR_OWNERSHIP = {
    "retailer-owned",
    "marketplace-owned",
    "publisher-owned",
    "forum-owned",
    "competitor-owned",
}
_SKU_REPAIR_ROUTES = {"retailer", "marketplace", "publisher", "forum", "brand"}


def classify_primary_gap(
    *,
    merchant_view: Mapping[str, Any],
    integration_state: Optional[Mapping[str, Any]] = None,
    is_cold_start: bool = False,
) -> str:
    """Pick the single brand-level commercial leak to prescribe first."""

    headline = _as_mapping(merchant_view.get("headline"))
    receipts = _as_mapping(merchant_view.get("receipts"))
    scores = _as_mapping(headline.get("scores"))
    verdict_label = str(headline.get("verdict_label") or "").strip().upper()

    visibility = _score(scores.get("visibility"))
    attribution = _score(scores.get("attribution"))
    category_visibility = _optional_score(scores.get("category_visibility"))
    best_visibility = max(
        visibility,
        category_visibility if category_visibility is not None else 0,
    )
    gap_visibility_to_attribution = (
        (category_visibility if category_visibility is not None else visibility)
        - attribution
    )

    cited_hosts = _high_confidence_hosts(receipts.get("cited_hosts_detailed"))
    retailer_hosts = [h for h in cited_hosts if _host_type(h) in _RETAIL_HOST_TYPES]
    source_hosts = [h for h in cited_hosts if _host_type(h) in _SOURCE_HOST_TYPES]
    competitor_names, competitor_counts = _competitor_evidence(receipts)

    if _has_required_integration_gap(
        integration_state,
        is_cold_start=is_cold_start,
    ):
        return PRIMARY_INTEGRATION_COMPLETION

    if (
        verdict_label == _VERDICT_INVISIBLE
        and attribution < 30
        and visibility < 30
    ):
        return PRIMARY_RETRIEVAL_FOUNDATION

    if (
        best_visibility >= 50
        and gap_visibility_to_attribution >= 25
        and retailer_hosts
    ):
        return PRIMARY_RETAILER_ROUTE_LEAK

    if (
        category_visibility is not None
        and visibility >= 50
        and visibility - category_visibility >= 25
    ):
        return PRIMARY_CATEGORY_DISCOVERY

    repeated_competitor = any(count >= 2 for count in competitor_counts.values())
    if (
        source_hosts
        and any(_times_cited(h) >= 2 for h in source_hosts)
        and (len(competitor_names) >= 3 or repeated_competitor)
    ):
        return PRIMARY_COMPETITOR_SOURCE

    return PRIMARY_FIRST_PARTY_DEFENSE


def build_next_best_action(
    *,
    merchant_view: Mapping[str, Any],
    competitive_pressure: Optional[Mapping[str, Any]] = None,
    integration_state: Optional[Mapping[str, Any]] = None,
    is_cold_start: bool = False,
) -> Dict[str, Any]:
    """Build merchant_view.next_best_action from existing report fields."""

    primary_gap = classify_primary_gap(
        merchant_view=merchant_view,
        integration_state=integration_state,
        is_cold_start=is_cold_start,
    )
    evidence = _build_evidence_used(
        merchant_view=merchant_view,
        competitive_pressure=competitive_pressure,
        integration_state=integration_state,
    )
    prescription = _prescription_for_gap(
        primary_gap=primary_gap,
        merchant_view=merchant_view,
        evidence=evidence,
        is_cold_start=is_cold_start,
    )
    prescription["secondary_moves"] = _select_secondary_moves(
        actions=_as_list(merchant_view.get("actions")),
        primary_gap=primary_gap,
        evidence=evidence,
    )
    return prescription


def build_sku_next_best_action(
    *,
    opportunity: Mapping[str, Any],
    primary_gaps: Optional[List[Mapping[str, Any]]] = None,
    scores: Optional[Mapping[str, Any]] = None,
    failing_prompts: Optional[List[Mapping[str, Any]]] = None,
    verify_summary: Optional[Mapping[str, Any]] = None,
    identity: Optional[Mapping[str, Any]] = None,
    sku_title: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a deterministic per-SKU next-best-action prescription."""

    opportunity_map = _as_mapping(opportunity)
    gaps = [
        dict(gap)
        for gap in _as_list(primary_gaps)
        if isinstance(gap, Mapping)
    ]
    primary_gap = _classify_sku_primary_gap(
        opportunity=opportunity_map,
        primary_gaps=gaps,
        scores=_as_mapping(scores),
        identity=_as_mapping(identity),
    )
    evidence = _build_sku_evidence_used(
        opportunity=opportunity_map,
        primary_gaps=gaps,
        scores=_as_mapping(scores),
        failing_prompts=_as_list(failing_prompts),
        verify_summary=_as_mapping(verify_summary),
        identity=_as_mapping(identity),
        sku_title=sku_title,
    )
    prescription = _sku_prescription_for_gap(
        primary_gap=primary_gap,
        evidence=evidence,
    )
    prescription["secondary_moves"] = _sku_secondary_moves(
        primary_gap=primary_gap,
        primary_gaps=gaps,
        failing_prompts=_as_list(failing_prompts),
    )
    return prescription


async def attach_sku_strategic_brief(
    next_best_action: Mapping[str, Any],
    *,
    opportunity: Mapping[str, Any],
    attribute_graph: Mapping[str, Any],
    primary_gaps: Optional[List[Mapping[str, Any]]] = None,
    scores: Optional[Mapping[str, Any]] = None,
    identity: Optional[Mapping[str, Any]] = None,
    sku_title: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Optionally attach an evidence-grounded strategic brief.

    The deterministic next_best_action remains the contract and fallback. Any
    synthesis, parsing, validation, or config failure returns the original
    fields unchanged and without a strategic_brief key.
    """

    out = dict(next_best_action or {})
    try:
        from services.strategic_brief import (
            assemble_sku_brief_evidence,
            generate_sku_strategic_brief,
        )

        evidence = assemble_sku_brief_evidence(
            opportunity=opportunity,
            attribute_graph=attribute_graph,
            primary_gaps=primary_gaps,
            scores=scores,
            identity=identity,
            sku_title=sku_title,
        )
        brief = await generate_sku_strategic_brief(
            evidence,
            provider=provider,
            model=model,
        )
    except Exception:
        logger.warning("strategic brief attach failed; using deterministic NBA", exc_info=True)
        return out
    if brief:
        out["strategic_brief"] = brief
    return out


def _classify_sku_primary_gap(
    *,
    opportunity: Mapping[str, Any],
    primary_gaps: List[Mapping[str, Any]],
    scores: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> str:
    if _sku_top_open_lane(opportunity):
        return PRIMARY_SKU_OPEN_LANE_CAPTURE

    if _as_mapping(opportunity.get("substitution_alert")).get("present"):
        return PRIMARY_SKU_SUBSTITUTION_LEAK

    if _sku_top_content_gap(primary_gaps):
        return PRIMARY_SKU_CONTENT_REVISION_GAP

    if _sku_source_route_prompt(opportunity):
        return PRIMARY_SKU_SOURCE_ROUTE_REPAIR

    if _sku_is_protected(
        opportunity=opportunity,
        primary_gaps=primary_gaps,
        scores=scores,
        identity=identity,
    ):
        return PRIMARY_SKU_PROTECTED_MONITORING

    if _sku_has_resolved_coverage(
        opportunity=opportunity,
        identity=identity,
        require_demand=True,
    ):
        return PRIMARY_SKU_PROTECTED_MONITORING

    return PRIMARY_SKU_INSUFFICIENT_DATA


def _build_sku_evidence_used(
    *,
    opportunity: Mapping[str, Any],
    primary_gaps: List[Mapping[str, Any]],
    scores: Mapping[str, Any],
    failing_prompts: List[Any],
    verify_summary: Mapping[str, Any],
    identity: Mapping[str, Any],
    sku_title: Optional[str],
) -> Dict[str, Any]:
    return {
        "sku_title": _sku_title(identity=identity, sku_title=sku_title),
        "identity": dict(identity),
        "scores": _sku_scores(scores),
        "top_open_lane": _sku_lane_chip(_sku_top_open_lane(opportunity)),
        "substitution_alert": dict(_as_mapping(opportunity.get("substitution_alert"))),
        "content_gap": _sku_gap_chip(_sku_top_content_gap(primary_gaps)),
        "source_route_prompt": _sku_prompt_chip(_sku_source_route_prompt(opportunity)),
        "coverage": dict(_as_mapping(opportunity.get("confidence"))),
        "demand_state_summary": opportunity.get("demand_state_summary"),
        "intent_ladder": dict(_as_mapping(opportunity.get("intent_ladder"))),
        "failing_prompt_examples": [
            _sku_failing_prompt_chip(prompt)
            for prompt in failing_prompts[:5]
            if _sku_failing_prompt_chip(prompt)
        ],
        "verify_summary": dict(verify_summary),
    }


def _sku_prescription_for_gap(
    *,
    primary_gap: str,
    evidence: Mapping[str, Any],
) -> Dict[str, Any]:
    sku_title = str(evidence.get("sku_title") or "this SKU")
    top_lane = _as_mapping(evidence.get("top_open_lane"))
    substitution = _as_mapping(evidence.get("substitution_alert"))
    content_gap = _as_mapping(evidence.get("content_gap"))
    route_prompt = _as_mapping(evidence.get("source_route_prompt"))
    first_pivota_path = _sku_pivota_path(sku_title)

    if primary_gap == PRIMARY_SKU_OPEN_LANE_CAPTURE:
        query = _sku_query_phrase(top_lane.get("query"))
        why_fit = _phrase(_as_str_list(top_lane.get("why_fit")), "your product attributes")
        return _base_payload(
            primary_gap=primary_gap,
            headline=f"Own the answer to {query} while it's still up for grabs.",
            why_this_first=(
                f"Shoppers are already asking AI for {query} and no brand owns the "
                f"answer yet, and you're a literal match ({why_fit})."
            ),
            first_move=f"Add a page section and FAQ that answer {query}.",
            self_serve_actions=[
                (
                    f"Write a short section and FAQ in plain buyer words for {query}, "
                    f"leading with what makes you the match: {why_fit}."
                ),
                (
                    "Give AI a reason to cite you: add a couple of real reviews and "
                    "one comparison page for this use case."
                ),
            ],
            pivota_path=first_pivota_path,
            evidence_used=evidence,
            cta=_sku_cta("Create the answer AI cites for this"),
        )

    if primary_gap == PRIMARY_SKU_SUBSTITUTION_LEAK:
        substitute = str(substitution.get("substituted_by") or "a competitor").strip()
        prompt = _sku_query_phrase(substitution.get("prompt"))
        return _base_payload(
            primary_gap=primary_gap,
            headline=f"When buyers ask for alternatives, AI names {substitute}, not {sku_title}.",
            why_this_first=(
                f"On {prompt}, AI answers with {substitute} instead of {sku_title}. "
                "Those buyers are already shopping your category, they just don't "
                "have a reason to pick you yet."
            ),
            first_move=f"Publish a {sku_title} vs {substitute} comparison that shows when you win.",
            self_serve_actions=[
                (
                    f"Make a clear {sku_title} vs {substitute} comparison: use cases, "
                    "what's different, proof, and price, so the choice is obvious."
                ),
                (
                    f"Answer the {prompt} question on your own page so it resolves to "
                    "you, not a third party."
                ),
            ],
            pivota_path=first_pivota_path,
            evidence_used=evidence,
            cta=_sku_cta("Turn the comparison into an answer AI cites"),
        )

    if primary_gap == PRIMARY_SKU_CONTENT_REVISION_GAP:
        bucket = _sku_gap_label(content_gap)
        return _base_payload(
            primary_gap=primary_gap,
            headline=f"Fill the gaps on {sku_title}'s page before chasing reach.",
            why_this_first=(
                f"AI can't confidently recommend a product it can't read, and "
                f"{sku_title}'s page is thin on {bucket}. That's the first fix."
            ),
            first_move=f"Add the missing {bucket} to {sku_title}'s page.",
            self_serve_actions=[
                (
                    f"Fill in {bucket} in plain language: what it is, who it's for, "
                    "how to use it, and the facts a buyer needs to decide."
                ),
                (
                    "Make sure those facts show up in the page itself, then re-run "
                    "the audit to confirm they landed."
                ),
            ],
            pivota_path=first_pivota_path,
            evidence_used=evidence,
            cta=_sku_cta("Publish the enriched product page"),
        )

    if primary_gap == PRIMARY_SKU_SOURCE_ROUTE_REPAIR:
        query = _sku_query_phrase(route_prompt.get("query"))
        hosts = _phrase(_host_names(route_prompt.get("sources")), "other sites")
        return _base_payload(
            primary_gap=primary_gap,
            headline=f"For {query}, AI leans on {hosts}, not you.",
            why_this_first=(
                f"This lane isn't empty, AI is just citing {hosts} for it. You win "
                "it by getting into the source that already shapes the answer, then "
                "making your own page the better one."
            ),
            first_move=_sku_source_route_first_move(route_prompt),
            self_serve_actions=[
                _sku_source_route_self_serve(route_prompt),
                "Then make your own page the better answer: clearer facts, proof, and a few reviews.",
            ],
            pivota_path=first_pivota_path,
            evidence_used=evidence,
            cta=_sku_cta("Win back the cited answer"),
        )

    if primary_gap == PRIMARY_SKU_PROTECTED_MONITORING:
        return _base_payload(
            primary_gap=primary_gap,
            headline=f"You own {sku_title}'s answers, keep it that way.",
            why_this_first=(
                f"{sku_title} is showing up well and nothing is slipping. Don't "
                "invent work, protect what's working and watch for changes."
            ),
            first_move=(
                "Keep an eye out for AI citing you less, or a competitor starting "
                "to win these prompts."
            ),
            self_serve_actions=[
                (
                    "Keep your page facts current, price, stock, shipping, returns, "
                    "and images, before any big catalog or theme change."
                ),
                (
                    "Watch the sites AI cites, and competitors' pages, for stale "
                    "facts or new comparison claims that could pull answers away."
                ),
            ],
            pivota_path=first_pivota_path,
            evidence_used=evidence,
            cta=_sku_cta("Monitor for drift"),
        )

    identity = _as_mapping(evidence.get("identity"))
    reason = (
        "we couldn't pin down exactly which product this is"
        if bool(identity.get("unresolved"))
        else "there wasn't enough signal this run"
    )
    return _base_payload(
        primary_gap=PRIMARY_SKU_INSUFFICIENT_DATA,
        headline=f"We need a bit more to go on for {sku_title}.",
        why_this_first=(
            f"We won't invent a recommendation, {reason}. Tighten the product "
            "details and we'll give you a real next move."
        ),
        first_move="Make sure the product details are complete, then re-run the audit.",
        self_serve_actions=[
            (
                "Complete the basics: title, brand, category, variants, price, "
                "images, and a clear description."
            ),
            "Then re-run, so we can test it as a real product with real demand.",
        ],
        pivota_path=first_pivota_path,
        evidence_used=evidence,
        cta=_sku_cta("Complete the details and retest"),
    )


def _sku_pivota_path(sku_title: str) -> str:
    return (
        f"Pivota can make {sku_title}'s page the one AI cites and lets shoppers "
        "buy, and tell you when these prompts start naming you."
    )


def _sku_cta(label: str) -> Dict[str, str]:
    return {
        "label": label,
        "trust_note": (
            "The merchant can make the PDP, listing, content, and outreach fixes "
            "directly; Pivota is for canonical SKU serving, checkout, and monitoring."
        ),
    }


def _sku_top_open_lane(opportunity: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    for lane in _as_list(opportunity.get("top_open_lanes")):
        if isinstance(lane, Mapping) and str(lane.get("query") or "").strip():
            return lane
    return None


def _sku_top_content_gap(primary_gaps: List[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    content_gaps = [
        gap for gap in primary_gaps
        if str(gap.get("dimension") or "").strip() == "content_richness"
    ]
    if not content_gaps:
        return None
    content_gaps.sort(
        key=lambda gap: (
            -_score(gap.get("gap")),
            str(gap.get("bucket") or ""),
        )
    )
    return content_gaps[0]


def _sku_source_route_prompt(opportunity: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    candidates: List[Mapping[str, Any]] = []
    for row in _as_list(opportunity.get("per_prompt")):
        if not isinstance(row, Mapping):
            continue
        ownership = str(row.get("ownership_state") or "").strip().lower()
        source_route = str(row.get("source_route") or "").strip().lower()
        if ownership not in _SKU_REPAIR_OWNERSHIP and source_route not in _SKU_REPAIR_ROUTES:
            continue
        if ownership == "merchant-owned":
            continue
        if _score(row.get("opportunity_score")) <= 0 and float(row.get("demand_signal") or 0) <= 0:
            continue
        candidates.append(row)
    if not candidates:
        return None
    candidates.sort(
        key=lambda row: (
            -float(row.get("opportunity_score") or 0),
            str(row.get("query") or "").lower(),
        )
    )
    return candidates[0]


def _sku_has_resolved_coverage(
    *,
    opportunity: Mapping[str, Any],
    identity: Mapping[str, Any],
    require_demand: bool,
) -> bool:
    if identity.get("unresolved"):
        return False
    coverage = _as_mapping(opportunity.get("confidence"))
    if _score(coverage.get("prompt_count")) <= 0:
        return False
    if not require_demand:
        return True
    if _score(coverage.get("prompts_with_demand")) > 0:
        return True
    return any(
        isinstance(row, Mapping)
        and float(row.get("demand_signal") or 0) > 0
        for row in _as_list(opportunity.get("per_prompt"))
    )


def _sku_is_protected(
    *,
    opportunity: Mapping[str, Any],
    primary_gaps: List[Mapping[str, Any]],
    scores: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> bool:
    if identity.get("unresolved"):
        return False
    coverage = _as_mapping(opportunity.get("confidence"))
    if _score(coverage.get("prompt_count")) <= 0:
        return False

    score_values = [
        value for value in _sku_scores(scores).values()
        if value is not None
    ]
    strong_scores = bool(score_values) and min(score_values) >= 70
    material_gap = any(_score(gap.get("gap")) >= 20 for gap in primary_gaps)
    if not strong_scores or material_gap:
        return False

    rows = [
        row for row in _as_list(opportunity.get("per_prompt"))
        if isinstance(row, Mapping)
    ]
    demand_rows = [
        row for row in rows
        if str(row.get("ownership_state") or "") != "no-demand"
        and float(row.get("demand_signal") or 0) > 0
    ]
    if not demand_rows:
        return False
    owned_rows = [
        row for row in demand_rows
        if str(row.get("ownership_state") or "").lower()
        in {"merchant-owned", "merchant-mentioned"}
    ]
    return len(owned_rows) >= max(1, (len(demand_rows) + 1) // 2)


def _sku_title(*, identity: Mapping[str, Any], sku_title: Optional[str]) -> str:
    return str(identity.get("name") or sku_title or "this SKU").strip() or "this SKU"


def _sku_scores(scores: Mapping[str, Any]) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {}
    for dimension, payload in scores.items():
        if isinstance(payload, Mapping):
            raw_score = payload.get("score")
        else:
            raw_score = payload
        out[str(dimension)] = None if raw_score is None else _score(raw_score)
    return out


def _sku_lane_chip(lane: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not lane:
        return {}
    return {
        "query": lane.get("query"),
        "why_fit": _as_str_list(lane.get("why_fit")),
        "current_ownership": lane.get("current_ownership"),
        "source_route": lane.get("source_route"),
        "demand_state": lane.get("demand_state"),
        "density_band": lane.get("density_band"),
        "opportunity_score": lane.get("opportunity_score"),
        "first_move": lane.get("first_move"),
    }


def _sku_gap_chip(gap: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not gap:
        return {}
    return {
        "dimension": gap.get("dimension"),
        "bucket": gap.get("bucket"),
        "points": gap.get("points"),
        "max": gap.get("max"),
        "gap": gap.get("gap"),
        "reason": gap.get("reason"),
    }


def _sku_prompt_chip(prompt: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not prompt:
        return {}
    source_summary = _as_mapping(prompt.get("source_summary"))
    return {
        "query": prompt.get("query"),
        "ownership_state": prompt.get("ownership_state"),
        "source_route": prompt.get("source_route"),
        "opportunity_score": prompt.get("opportunity_score"),
        "why_fit": prompt.get("attribute_basis") or prompt.get("evidence"),
        "sources": _as_list(source_summary.get("top_cited_hosts"))[:3],
        "competitors": _as_list(prompt.get("competitors"))[:5],
    }


def _sku_failing_prompt_chip(prompt: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(prompt, Mapping):
        return None
    query = str(prompt.get("query") or "").strip()
    if not query:
        return None
    return {
        "query": query,
        "reason": prompt.get("reason"),
        "provider": prompt.get("provider"),
        "grounding_sources": _as_list(prompt.get("grounding_sources"))[:3],
        "competitors_named": _as_list(prompt.get("competitors_named"))[:5],
    }


def _sku_gap_label(gap: Mapping[str, Any]) -> str:
    bucket = str(gap.get("bucket") or "content_richness").strip()
    return bucket.replace("_", " ")


def _sku_query_phrase(value: Any) -> str:
    text = str(value or "").strip()
    return f'"{text}"' if text else "the tested SKU prompt"


def _sku_source_route_first_move(prompt: Mapping[str, Any]) -> str:
    route = str(prompt.get("source_route") or "").strip().lower()
    ownership = str(prompt.get("ownership_state") or "").strip().lower()
    query = _sku_query_phrase(prompt.get("query"))
    competitors = _phrase(_as_str_list(prompt.get("competitors")), "the named competitor")
    if ownership in {"retailer-owned", "marketplace-owned"} or route in {"retailer", "marketplace"}:
        return f"Fix the cited retailer/marketplace listing for {query}"
    if ownership == "publisher-owned" or route == "publisher":
        return f"Pitch the cited publisher for roundup inclusion around {query}"
    if ownership == "forum-owned" or route == "forum":
        return f"Build reviews/UGC that answer {query}"
    return f"Publish comparison proof for {query} against {competitors}"


def _sku_source_route_self_serve(prompt: Mapping[str, Any]) -> str:
    route = str(prompt.get("source_route") or "").strip().lower()
    ownership = str(prompt.get("ownership_state") or "").strip().lower()
    sources = _phrase(_host_names(prompt.get("sources")), "the cited sources")
    if ownership in {"retailer-owned", "marketplace-owned"} or route in {"retailer", "marketplace"}:
        return (
            f"Audit {sources} for title, images, claims, variants, price, "
            "availability, authorization, and SKU facts."
        )
    if ownership == "publisher-owned" or route == "publisher":
        return (
            f"Pitch {sources} with SKU facts, proof assets, images, pricing, "
            "availability, and a specific comparison angle."
        )
    if ownership == "forum-owned" or route == "forum":
        return (
            f"Build review and UGC proof that can show up near {sources}, "
            "then link it back to the official PDP."
        )
    return (
        "Add comparison content naming the competitor, with substantiated "
        "differences, use cases, specs/ingredients, and review proof."
    )


def _sku_secondary_moves(
    *,
    primary_gap: str,
    primary_gaps: List[Mapping[str, Any]],
    failing_prompts: List[Any],
) -> List[Dict[str, Any]]:
    moves: List[Dict[str, Any]] = []
    primary_content_gap = _sku_top_content_gap(primary_gaps)
    for gap in primary_gaps:
        if primary_gap == PRIMARY_SKU_CONTENT_REVISION_GAP and gap == primary_content_gap:
            continue
        title = f"Fix {str(gap.get('dimension') or 'sku')}.{str(gap.get('bucket') or 'gap')}"
        moves.append({
            "title": title,
            "severity": "medium" if _score(gap.get("gap")) < 20 else "high",
            "lever": "sku_gap_repair",
            "target_host": None,
            "concrete_next_step": str(gap.get("reason") or "Close the next largest per-SKU score gap."),
            "reason": (
                f"It is the next largest per-SKU gap at "
                f"{_score(gap.get('gap'))} missing points."
            ),
            "evidence": _sku_gap_chip(gap),
        })
        if len(moves) >= 2:
            return moves

    for prompt in failing_prompts:
        chip = _sku_failing_prompt_chip(prompt)
        if not chip:
            continue
        moves.append({
            "title": f"Re-test failed SKU prompt: {chip['query']}",
            "severity": "medium",
            "lever": "sku_prompt_retest",
            "target_host": None,
            "concrete_next_step": "Revise the PDP/source evidence, then re-run this exact prompt.",
            "reason": "It is named in the per-SKU failing prompt evidence.",
            "evidence": chip,
        })
        if len(moves) >= 2:
            break
    return moves


def _prescription_for_gap(
    *,
    primary_gap: str,
    merchant_view: Mapping[str, Any],
    evidence: Mapping[str, Any],
    is_cold_start: bool,
) -> Dict[str, Any]:
    retailer_phrase = _phrase(_host_names(evidence.get("retailer_hosts")), "no named retailer hosts")
    source_phrase = _phrase(_host_names(evidence.get("source_hosts")), "no named editorial hosts")
    cited_phrase = _phrase(_host_names(evidence.get("cited_hosts")), "no high-confidence cited hosts")
    competitor_phrase = _phrase(_as_str_list(evidence.get("competitors_named")), "no repeated named competitors")
    failed_query_phrase = _phrase(_query_examples(evidence.get("failed_query_examples")), "no failed-query examples")

    if primary_gap == PRIMARY_INTEGRATION_COMPLETION:
        missing = _integration_missing_labels(evidence.get("integration_missing_pieces"))
        missing_phrase = _phrase(missing, "required onboarding steps")
        return _base_payload(
            primary_gap=primary_gap,
            headline="Finish connecting your store before we optimize the rest.",
            why_this_first=(
                f"You're mid-setup, but {missing_phrase} isn't connected yet. "
                "Until it is, Pivota can't serve your products to AI or take "
                "checkout, so finishing setup comes first."
            ),
            first_move=f"Finish connecting {missing_phrase}, then re-run this audit.",
            self_serve_actions=[
                (
                    "Keep your product pages live and accurate while you finish: "
                    "current price, stock, shipping, returns, and core details."
                ),
                (
                    f"Line up the first page fixes from the questions you're losing "
                    f"({failed_query_phrase}) so they're ready right after setup."
                ),
            ],
            pivota_path=(
                f"Finish Pivota setup for {missing_phrase} so your products can be "
                "served to AI and bought directly."
            ),
            evidence_used=evidence,
            cta={
                "label": "Finish Pivota setup",
                "trust_note": (
                    "This only leads for merchants already setting up; a cold audit "
                    "keeps setup as the Pivota option, not the first step."
                ),
            },
        )

    if primary_gap == PRIMARY_RETRIEVAL_FOUNDATION:
        return _base_payload(
            primary_gap=primary_gap,
            headline="AI can't find you yet — fix that before anything else.",
            why_this_first=(
                f"When buyers asked {failed_query_phrase}, AI didn't cite your "
                f"site at all; the answers went to {cited_phrase}. AI can't "
                "recommend a page it can't find and read."
            ),
            first_move=(
                "Get your product pages indexed and readable: submit them to "
                "Google, and make sure the content and key facts are in the page itself."
            ),
            self_serve_actions=[
                (
                    "Submit your sitemap and top product pages in Google Search "
                    "Console, and check each one isn't accidentally blocked from search."
                ),
                (
                    "Make sure price, availability, and the core product facts are "
                    "visible in the page text, not only in images or scripts."
                ),
            ],
            pivota_path=(
                "Pivota can publish clean, AI-ready versions of your pages, make "
                "them buyable, and re-check these same questions over time."
            ),
            evidence_used=evidence,
            cta={
                "label": "Make your pages the answer AI cites",
                "trust_note": (
                    "You can do the indexing and content cleanup yourself; Pivota "
                    "is for serving a clean buyable page and monitoring."
                ),
            },
        )

    if primary_gap == PRIMARY_RETAILER_ROUTE_LEAK:
        return _base_payload(
            primary_gap=primary_gap,
            headline="AI sends your buyers to retailers — take that path back.",
            why_this_first=(
                f"Shoppers can find you, but AI points them to {retailer_phrase} "
                "instead of your own page. That's lost margin and customer data, "
                "not a PR problem."
            ),
            first_move=(
                "Make your own page the best place to buy, then fix the retailer "
                "listings AI is citing."
            ),
            self_serve_actions=[
                (
                    "Make your product page beat the retailer pages: full details, "
                    "proof, reviews, current price, stock, and returns."
                ),
                (
                    f"Check the cited retailer listings ({retailer_phrase}) for wrong "
                    "titles, images, price, or stock, and decide which routes to lean into."
                ),
            ],
            pivota_path=(
                "Pivota can make your own page the one AI cites and buys from, and "
                "show you whether direct sales rise against those retailer-won questions."
            ),
            evidence_used=evidence,
            cta={
                "label": "Win back the buying path",
                "trust_note": (
                    "Fixing retailer listings is yours to do; Pivota makes your own "
                    "page the cited, buyable one and proves whether direct sales rise."
                ),
            },
        )

    if primary_gap == PRIMARY_CATEGORY_DISCOVERY:
        source_or_cited = (
            source_phrase if source_phrase != "no named editorial hosts" else cited_phrase
        )
        return _base_payload(
            primary_gap=primary_gap,
            headline="Shoppers who know you find you — new shoppers don't.",
            why_this_first=(
                "When people search the category instead of your name, you're not "
                f"in the answer. {failed_query_phrase} went to {competitor_phrase}. "
                "That's where you're losing new buyers."
            ),
            first_move=(
                "Add category and comparison content to your pages, and get into "
                "the sources that shape those answers."
            ),
            self_serve_actions=[
                (
                    "Answer the exact category questions on your pages: best-for, "
                    "compare-to, who it's for, and the proof behind it."
                ),
                (
                    f"Pitch the sites AI cites for these ({source_or_cited}) on why "
                    f"you belong next to {competitor_phrase}."
                ),
            ],
            pivota_path=(
                "Pivota can turn these gaps into category pages AI cites, and "
                "re-check the category questions each month."
            ),
            evidence_used=evidence,
            cta={
                "label": "Turn category gaps into pages AI cites",
                "trust_note": (
                    "The content and outreach are yours to do; Pivota keeps it "
                    "structured, measurable, and buyable."
                ),
            },
        )

    if primary_gap == PRIMARY_COMPETITOR_SOURCE:
        return _base_payload(
            primary_gap=primary_gap,
            headline="Get into the sources teaching AI to prefer your competitors.",
            why_this_first=(
                f"{competitor_phrase} keep showing up because AI leans on "
                f"{source_phrase}. Since it's the same few sources, the move is "
                "getting into them, not broad awareness."
            ),
            first_move=(
                "Pitch the specific sites AI cites with a concrete comparison and "
                "proof, tied to the questions you're losing."
            ),
            self_serve_actions=[
                (
                    "Send each cited site a specific pitch: your product facts, proof, "
                    f"and why you belong in the same comparison as {competitor_phrase}."
                ),
                (
                    f"Back it with your own page answering {failed_query_phrase}, so "
                    "any new coverage has a clear source to point to."
                ),
            ],
            pivota_path=(
                "Pivota can rank the right sites to target, draft the pitches from "
                "your audit, and track whether new coverage shows up in AI answers."
            ),
            evidence_used=evidence,
            cta={
                "label": "Prioritize the sites AI cites",
                "trust_note": (
                    "Pivota can't force coverage; it finds the right targets, drafts "
                    "from your evidence, and measures whether citations move."
                ),
            },
        )

    return _base_payload(
        primary_gap=PRIMARY_FIRST_PARTY_DEFENSE,
        headline="You're in good shape — defend it and watch for changes.",
        why_this_first=(
            "We didn't find a clear leak to retailers, the category, or competitor "
            "sources this run. Don't manufacture work, protect what's working and "
            "revisit when something shifts."
        ),
        first_move=(
            "Keep monitoring, and act only on real drops in how AI cites you or "
            "new competitor pressure."
        ),
        self_serve_actions=[
            (
                "Keep your page facts, price, stock, and returns current before any "
                "big catalog or theme change."
            ),
            (
                "Watch the sites AI cites and competitor pages for stale facts or "
                "new comparison claims."
            ),
        ],
        pivota_path=(
            "Pivota can keep monitoring, alert you to changes, and keep your pages "
            "AI-ready and buyable if that becomes a goal."
        ),
        evidence_used=evidence,
        cta={
            "label": "Monitor for drift",
            "trust_note": (
                "A strong result shouldn't be scared into a rebuild; Pivota is for "
                "monitoring and keeping your pages durable when you need it."
            ),
        },
    )


def _base_payload(
    *,
    primary_gap: str,
    headline: str,
    why_this_first: str,
    first_move: str,
    self_serve_actions: List[str],
    pivota_path: str,
    evidence_used: Mapping[str, Any],
    cta: Mapping[str, str],
) -> Dict[str, Any]:
    return {
        "primary_gap": primary_gap,
        "headline": headline,
        "why_this_first": why_this_first,
        "first_move": first_move,
        "self_serve_actions": list(self_serve_actions[:2]),
        "pivota_path": pivota_path,
        "evidence_used": dict(evidence_used),
        "secondary_moves": [],
        "cta": dict(cta),
    }


def _build_evidence_used(
    *,
    merchant_view: Mapping[str, Any],
    competitive_pressure: Optional[Mapping[str, Any]],
    integration_state: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    headline = _as_mapping(merchant_view.get("headline"))
    receipts = _as_mapping(merchant_view.get("receipts"))
    scores = _as_mapping(headline.get("scores"))
    cited_hosts = _high_confidence_hosts(receipts.get("cited_hosts_detailed"))
    retailer_hosts = [h for h in cited_hosts if _host_type(h) in _RETAIL_HOST_TYPES]
    source_hosts = [h for h in cited_hosts if _host_type(h) in _SOURCE_HOST_TYPES]
    competitor_names, _counts = _competitor_evidence(receipts)

    return {
        "verdict_label": headline.get("verdict_label"),
        "scores": {
            "visibility": _score(scores.get("visibility")),
            "attribution": _score(scores.get("attribution")),
            "category_visibility": _optional_score(scores.get("category_visibility")),
        },
        "cited_hosts": [_host_chip(h) for h in cited_hosts[:5]],
        "retailer_hosts": [_host_chip(h) for h in retailer_hosts[:5]],
        "source_hosts": [_host_chip(h) for h in source_hosts[:5]],
        "competitors_named": competitor_names[:8],
        "failed_query_examples": [
            _query_chip(q)
            for q in _as_list(receipts.get("failed_queries_detailed"))[:5]
            if _query_chip(q)
        ],
        "competitive_table": _as_list(receipts.get("competitive_table"))[:5],
        "competitive_pressure": dict(competitive_pressure or {}),
        "integration_missing_pieces": _required_missing_pieces(integration_state),
    }


def _select_secondary_moves(
    *,
    actions: List[Any],
    primary_gap: str,
    evidence: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    candidates: List[Tuple[int, int, Mapping[str, Any]]] = []
    evidence_hosts = {
        h.get("host")
        for h in _as_list(evidence.get("cited_hosts"))
        if isinstance(h, Mapping) and h.get("host")
    }

    for idx, raw_action in enumerate(actions or []):
        if not isinstance(raw_action, Mapping):
            continue
        if _duplicates_primary_move(raw_action, primary_gap):
            continue
        lever = str(raw_action.get("lever") or "").strip()
        if lever == "content_revision":
            continue

        score = _SEVERITY_RANK.get(str(raw_action.get("severity") or "low"), 1)
        if raw_action.get("concrete_next_step"):
            score += 4
        if raw_action.get("pitch_draft"):
            score += 3
        if raw_action.get("target_host") in evidence_hosts:
            score += 2
        if lever in _preferred_secondary_levers(primary_gap):
            score += 3
        evidence_dict = _as_mapping(raw_action.get("evidence"))
        score += min(_score(evidence_dict.get("times_cited")), 4)
        candidates.append((score, -idx, raw_action))

    candidates.sort(reverse=True, key=lambda row: (row[0], row[1]))
    selected: List[Dict[str, Any]] = []
    seen_titles: set[str] = set()
    for _score_value, _neg_idx, action in candidates:
        title = str(action.get("title") or "").strip()
        if not title or title.lower() in seen_titles:
            continue
        seen_titles.add(title.lower())
        selected.append(_secondary_move(action))
        if len(selected) >= 2:
            break
    return selected


def _secondary_move(action: Mapping[str, Any]) -> Dict[str, Any]:
    evidence = _as_mapping(action.get("evidence"))
    target_host = action.get("target_host") or evidence.get("host")
    times_cited = evidence.get("times_cited")
    reason_bits: List[str] = []
    if target_host:
        if times_cited:
            reason_bits.append(f"{target_host} was cited {times_cited} times")
        else:
            reason_bits.append(f"{target_host} is named in the cited-host evidence")
    if action.get("concrete_next_step"):
        reason_bits.append("the existing playbook includes a concrete next step")
    if action.get("pitch_draft"):
        reason_bits.append("the existing playbook includes a pitch draft")
    if not reason_bits:
        reason_bits.append("it is already prioritized in merchant_view.actions")

    out: Dict[str, Any] = {
        "title": action.get("title"),
        "severity": action.get("severity"),
        "lever": action.get("lever"),
        "target_host": target_host,
        "concrete_next_step": action.get("concrete_next_step"),
        "reason": "; ".join(reason_bits) + ".",
    }
    if action.get("pitch_draft"):
        out["pitch_draft"] = action.get("pitch_draft")
    if evidence:
        out["evidence"] = dict(evidence)
    return out


def _preferred_secondary_levers(primary_gap: str) -> set[str]:
    if primary_gap == PRIMARY_RETRIEVAL_FOUNDATION:
        return {"editorial_outreach", "wholesale_onboarding"}
    if primary_gap == PRIMARY_RETAILER_ROUTE_LEAK:
        return {"wholesale_onboarding", "marketplace_listing", "editorial_outreach"}
    if primary_gap == PRIMARY_CATEGORY_DISCOVERY:
        return {"editorial_outreach", "creator_partnership", "wholesale_onboarding"}
    if primary_gap == PRIMARY_COMPETITOR_SOURCE:
        return {"editorial_outreach", "creator_partnership", "social_proof"}
    if primary_gap == PRIMARY_FIRST_PARTY_DEFENSE:
        return {"gsc_integration", "editorial_outreach", "wholesale_onboarding"}
    return set()


def _duplicates_primary_move(action: Mapping[str, Any], primary_gap: str) -> bool:
    lever = str(action.get("lever") or "").strip()
    text = " ".join(
        str(action.get(k) or "")
        for k in ("title", "body", "concrete_next_step")
    ).lower()
    if primary_gap == PRIMARY_INTEGRATION_COMPLETION:
        return lever in {"pivota_integration", "gsc_integration"} or "integration" in text
    if primary_gap == PRIMARY_RETRIEVAL_FOUNDATION:
        return any(token in text for token in ("index your canonical", "schema + sitemap"))
    if primary_gap == PRIMARY_RETAILER_ROUTE_LEAK:
        return any(token in text for token in ("reclaim attribution", "ai-channel funnel"))
    if primary_gap == PRIMARY_CATEGORY_DISCOVERY:
        return "category discovery" in text and "pitch" not in text
    if primary_gap == PRIMARY_FIRST_PARTY_DEFENSE:
        return "maintain attribution with monitoring" in text
    return False


def _has_required_integration_gap(
    integration_state: Optional[Mapping[str, Any]],
    *,
    is_cold_start: bool,
) -> bool:
    if not integration_state or is_cold_start:
        return False
    if integration_state.get("fully_integrated"):
        return False
    return bool(_required_missing_pieces(integration_state))


def _required_missing_pieces(
    integration_state: Optional[Mapping[str, Any]],
) -> List[str]:
    if not integration_state:
        return []
    return [
        str(piece)
        for piece in _as_list(integration_state.get("missing_pieces"))
        if str(piece) in _REQUIRED_INTEGRATION_PIECES
    ]


def _integration_missing_labels(value: Any) -> List[str]:
    labels = []
    for piece in _as_str_list(value):
        if piece == "store_platform":
            labels.append("store platform")
        elif piece == "psp":
            labels.append("payment provider")
        else:
            labels.append(piece.replace("_", " "))
    return labels


def _high_confidence_hosts(value: Any) -> List[Mapping[str, Any]]:
    hosts: List[Mapping[str, Any]] = []
    for host in _as_list(value):
        if not isinstance(host, Mapping):
            continue
        if not host.get("host"):
            continue
        if _host_type(host) in _LOW_CONFIDENCE_HOST_TYPES:
            continue
        hosts.append(host)
    hosts.sort(key=lambda h: (-_times_cited(h), str(h.get("host") or "")))
    return hosts


def _host_chip(host: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "host": host.get("host"),
        "type": host.get("type"),
        "subtype": host.get("subtype"),
        "times_cited": _times_cited(host),
        "coverage_note": host.get("coverage_note"),
        "outreach_hint": host.get("outreach_hint"),
    }


def _query_chip(query: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(query, Mapping):
        return None
    raw_host = query.get("top_cited_host")
    classification = _as_mapping(query.get("host_classification"))
    host_type = str(classification.get("type") or "").strip().lower()
    top_cited_host = (
        raw_host
        if raw_host and host_type not in _LOW_CONFIDENCE_HOST_TYPES
        else None
    )
    query_text = str(query.get("query") or "").strip()
    if not query_text:
        return None
    return {
        "query": query_text,
        "top_cited_host": top_cited_host,
        "host_type": host_type or None,
        "competitors_named": _as_str_list(query.get("competitors_named"))[:5],
    }


def _competitor_evidence(
    receipts: Mapping[str, Any],
) -> Tuple[List[str], Dict[str, int]]:
    counts: Dict[str, int] = {}
    display: Dict[str, str] = {}

    def add(name: Any) -> None:
        if not isinstance(name, str):
            return
        cleaned = name.strip()
        if not cleaned:
            return
        key = cleaned.lower()
        display.setdefault(key, cleaned)
        counts[key] = counts.get(key, 0) + 1

    for name in _as_list(receipts.get("top_competitor_brands")):
        add(name)
    for row in _as_list(receipts.get("failed_queries_detailed")):
        if not isinstance(row, Mapping):
            continue
        for name in _as_list(row.get("competitors_named")):
            add(name)

    ordered_keys = sorted(counts, key=lambda k: (-counts[k], display[k].lower()))
    return [display[k] for k in ordered_keys], counts


def _host_names(value: Any) -> List[str]:
    out: List[str] = []
    for row in _as_list(value):
        if isinstance(row, Mapping) and row.get("host"):
            out.append(str(row.get("host")))
        elif isinstance(row, str) and row.strip():
            out.append(row.strip())
    return out[:4]


def _query_examples(value: Any) -> List[str]:
    out: List[str] = []
    for row in _as_list(value):
        if isinstance(row, Mapping) and row.get("query"):
            out.append(f'"{str(row.get("query")).strip()}"')
        elif isinstance(row, str) and row.strip():
            out.append(f'"{row.strip()}"')
    return out[:3]


def _phrase(values: List[str], fallback: str) -> str:
    cleaned = [v for v in values if v]
    if not cleaned:
        return fallback
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


def _host_type(host: Mapping[str, Any]) -> str:
    return str(host.get("type") or "unclassified").strip().lower()


def _times_cited(host: Mapping[str, Any]) -> int:
    return _score(host.get("times_cited"))


def _score(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_score(value: Any) -> Optional[int]:
    if value is None:
        return None
    return _score(value)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_str_list(value: Any) -> List[str]:
    out: List[str] = []
    for item in _as_list(value):
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out
