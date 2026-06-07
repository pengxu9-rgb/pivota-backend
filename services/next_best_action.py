"""Deterministic brand-level next-best-action projection.

This module consumes the already-built merchant_view evidence/action
inventory and returns one merchant-facing prescription. It does not call
LLMs, does not recompute audit scores, and deliberately avoids per-SKU
rollups or content-revision wiring.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from services.buyer_path_stable_controllers import (
    stable_buyer_path_controllers_for_row,
)
from services.buyer_path_controller_quality import (
    controller_profile as build_controller_profile,
    is_canonical_source_vacuum,
)
from services.sku_lane_priority import (
    build_sideways_wedge,
    has_lane_demand,
    is_third_party_controlled_lane,
    prioritize_lanes,
)


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
_OFFER_ECONOMICS_POLICY = (
    "Mechanics only: first-order offer, starter + replenishment bundle, "
    "subscription incentive, and why-buy-direct proof. Do not recommend exact "
    "discount depths, bundle prices, savings percentages, or margin claims "
    "without audited margin or promo evidence."
)

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
_COMMUNITY_CONTROLLER_TYPES = {"community", "forum", "reddit", "social"}
_PUBLISHER_CONTROLLER_TYPES = {"editorial", "publisher", "video"}
_LISTING_CONTROLLER_TYPES = {"marketplace", "retailer"}
_REQUIRED_INTEGRATION_PIECES = {"store_platform", "psp"}
_SEVERITY_RANK = {"critical": 5, "high": 4, "medium": 3, "low": 2}
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
    merchant_host: Optional[str] = None,
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
            merchant_host=merchant_host,
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
    sideways_wedge = _as_mapping(opportunity.get("sideways_wedge")) or build_sideways_wedge(
        _as_list(opportunity.get("per_prompt")),
        product_evidence=_as_mapping(opportunity.get("product_evidence")),
    )
    return {
        "sku_title": _sku_title(identity=identity, sku_title=sku_title),
        "identity": dict(identity),
        "merchant_path": _merchant_path(identity=identity, opportunity=opportunity),
        "scores": _sku_scores(scores),
        "top_open_lane": _sku_lane_chip(_sku_top_open_lane(opportunity)),
        "substitution_alert": dict(_as_mapping(opportunity.get("substitution_alert"))),
        "content_gap": _sku_gap_chip(_sku_top_content_gap(primary_gaps)),
        "source_route_prompt": _sku_prompt_chip(_sku_source_route_prompt(opportunity)),
        "coverage": dict(_as_mapping(opportunity.get("confidence"))),
        "demand_state_summary": opportunity.get("demand_state_summary"),
        "intent_ladder": dict(_as_mapping(opportunity.get("intent_ladder"))),
        "sideways_wedge": dict(sideways_wedge),
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
    merchant_path = _as_mapping(evidence.get("merchant_path"))
    sideways_wedge = _as_mapping(evidence.get("sideways_wedge"))
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
        hosts = _phrase(
            _host_names(route_prompt.get("sources")),
            "fragmented sources with no single cited site",
        )
        wedge_reason = _sku_sideways_wedge_reason(sideways_wedge)
        route_profile = _route_prompt_controller_profile(route_prompt)
        if is_canonical_source_vacuum(route_profile):
            why_this_first = (
                f"This lane isn't empty; AI is citing {hosts}. "
                f"{route_profile.get('exposure_read')} Make {_merchant_destination(merchant_path)} "
                "more retrievable, extractable, and citable before framing this as a retailer value fight."
                f"{wedge_reason}"
            )
        elif str(route_profile.get("strategy") or "") == "source_authority_gap":
            why_this_first = (
                f"This lane isn't empty; AI is citing {hosts}. Read that as source-authority "
                f"evidence: make {_merchant_destination(merchant_path)} the official page those "
                f"sources can reference, then work the cited source trail.{wedge_reason}"
            )
        else:
            why_this_first = (
                f"This lane isn't empty; AI is citing {hosts}. Read that as exposed demand: "
                f"make {_merchant_destination(merchant_path)} the better place to buy, then "
                f"work the source that already shapes the answer.{wedge_reason}"
            )
        return _base_payload(
            primary_gap=primary_gap,
            prescription_class="operational_efficiency",
            merchant_path=merchant_path,
            operator_moves=_sku_operator_moves(
                route_prompt=route_prompt,
                merchant_path=merchant_path,
            ),
            canonical_page_play=_sku_canonical_page_play(
                route_prompt=route_prompt,
                merchant_path=merchant_path,
            ),
            headline=f"For {query}, AI leans on {hosts}, not your buying path.",
            why_this_first=why_this_first,
            first_move=_sku_source_route_first_move(route_prompt, merchant_path),
            self_serve_actions=[
                _sku_source_route_self_serve(route_prompt, merchant_path),
                _sku_source_route_follow_up(route_prompt, merchant_path),
            ],
            pivota_path=first_pivota_path,
            evidence_used=evidence,
            sideways_wedge=sideways_wedge,
            cta=_sku_cta("Win back the cited buying path"),
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
    evidence_focus = (
        "product identity"
        if bool(identity.get("unresolved"))
        else "validated product and prompt evidence"
    )
    return _base_payload(
        primary_gap=PRIMARY_SKU_INSUFFICIENT_DATA,
        headline=f"Build the product evidence foundation for {sku_title}.",
        why_this_first=(
            f"The next useful move is to make {sku_title}'s canonical page strong "
            f"on {evidence_focus}, so AI has official facts and a buyable route "
            "to cite before broader demand work starts."
        ),
        first_move=(
            "Publish a complete canonical PDP with exact product naming, category, "
            "variants, price/stock, images, usage, proof, shipping/returns, and "
            "the direct-buy reason."
        ),
        self_serve_actions=[
            (
                "Fill the page with buyer-usable facts: who it is for, what it is, "
                "how to use it, ingredients/materials, format, proof, price, stock, "
                "shipping, returns, and any authorized where-to-buy path."
            ),
            (
                "Add the direct-site conversion mechanics to the same page: "
                "first-order offer, starter + replenishment bundle where relevant, "
                "subscription incentive, and why-buy-direct proof."
            ),
        ],
        pivota_path=first_pivota_path,
        evidence_used=evidence,
        cta=_sku_cta("Build the canonical product evidence"),
    )


def _sku_pivota_path(sku_title: str) -> str:
    return (
        f"Pivota can make {sku_title}'s canonical page more citable, buyable, and "
        "agent-checkout ready, then tell you when these prompts start routing "
        "toward your owned path."
    )


def _sku_cta(label: str) -> Dict[str, str]:
    return {
        "label": label,
        "trust_note": (
            "The merchant can make the PDP, listing, content, and outreach fixes "
            "directly; Pivota is for more citable + buyable canonical SKU serving, "
            "agent-checkout readiness, and monitoring."
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
        if not is_third_party_controlled_lane(row):
            continue
        if not has_lane_demand(row):
            continue
        candidates.append(row)
    if not candidates:
        return None
    prioritized = prioritize_lanes(
        candidates,
        product_evidence=_as_mapping(opportunity.get("product_evidence")),
    )
    return prioritized[0]


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
    stable_sources = stable_buyer_path_controllers_for_row(prompt)
    out = {
        "query": prompt.get("query"),
        "ownership_state": prompt.get("ownership_state"),
        "source_route": prompt.get("source_route"),
        "opportunity_score": prompt.get("opportunity_score"),
        "why_fit": prompt.get("attribute_basis") or prompt.get("evidence"),
        "sources": stable_sources[:3],
        "raw_top_cited_hosts": _as_list(source_summary.get("top_cited_hosts"))[:3],
        "competitors": _as_list(prompt.get("competitors"))[:5],
    }
    for key in (
        "lane_priority_score",
        "merchant_fit_score",
        "conversion_fit_score",
        "merchant_fit_reasons",
        "fit_penalties",
        "selection_reason",
    ):
        if key in prompt:
            out[key] = prompt.get(key)
    return out


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


def _merchant_path(
    *,
    identity: Optional[Mapping[str, Any]] = None,
    opportunity: Optional[Mapping[str, Any]] = None,
    merchant_view: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    archetype = _merchant_archetype(
        identity=identity,
        opportunity=opportunity,
        merchant_view=merchant_view,
    )
    if archetype == "channel":
        return {
            "archetype": "channel",
            "destination": "your channel's website",
            "page_label": "your channel PDP or category page",
            "goal": "drive buyers to the channel's website",
        }
    return {
        "archetype": "brand",
        "destination": "the brand's own website",
        "page_label": "your official PDP",
        "goal": "drive buyers to the brand's own website",
    }


def _merchant_archetype(
    *,
    identity: Optional[Mapping[str, Any]],
    opportunity: Optional[Mapping[str, Any]],
    merchant_view: Optional[Mapping[str, Any]],
) -> str:
    for payload in (identity, opportunity, merchant_view):
        data = _as_mapping(payload)
        raw = _first_present(
            data,
            (
                "merchant_archetype",
                "merchant_type",
                "business_model",
                "commerce_role",
                "seller_type",
                "retailer_type",
            ),
        )
        normalized = _normalize_merchant_archetype(raw)
        if normalized:
            return normalized
        anchors = _as_mapping(data.get("anchors"))
        normalized = _normalize_merchant_archetype(
            _first_present(
                anchors,
                (
                    "merchant_archetype",
                    "merchant_type",
                    "business_model",
                    "commerce_role",
                    "seller_type",
                ),
            )
        )
        if normalized:
            return normalized
    return "brand"


def _normalize_merchant_archetype(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if any(token in text for token in ("channel", "retailer", "marketplace", "multi-brand", "multibrand")):
        return "channel"
    if any(token in text for token in ("brand", "dtc", "d2c", "manufacturer", "maker")):
        return "brand"
    return None


def _first_present(payload: Mapping[str, Any], keys: Tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _merchant_destination(merchant_path: Mapping[str, Any]) -> str:
    return str(merchant_path.get("destination") or "the merchant-controlled website")


def _merchant_page_label(merchant_path: Mapping[str, Any]) -> str:
    return str(merchant_path.get("page_label") or "the merchant-controlled page")


def _controller_chips(
    controllers: Any,
    *,
    default_role: Optional[str] = None,
) -> List[Dict[str, Any]]:
    chips: List[Dict[str, Any]] = []
    role = str(default_role or "").strip().lower()
    for row in _as_list(controllers):
        host = ""
        row_role = role
        times_cited = None
        if isinstance(row, Mapping):
            host = str(row.get("host") or "").strip()
            row_role = str(row.get("role") or row.get("type") or role).strip().lower()
            times_cited = row.get("times_cited") or row.get("count") or row.get("citations")
        elif isinstance(row, str):
            host = row.strip()
        if not host:
            continue
        chip = {"host": host}
        if row_role:
            chip["role"] = row_role
        if times_cited is not None:
            chip["times_cited"] = times_cited
        chips.append(chip)
    return chips


def _route_prompt_controller_profile(prompt: Mapping[str, Any]) -> Dict[str, Any]:
    route = str(prompt.get("source_route") or "").strip().lower()
    ownership = str(prompt.get("ownership_state") or "").strip().lower()
    default_role = route or ownership
    return build_controller_profile(
        _controller_chips(prompt.get("sources"), default_role=default_role)
    )


def _brand_controller_profile(evidence: Mapping[str, Any]) -> Dict[str, Any]:
    hosts = [
        {
            "host": str(row.get("host") or ""),
            "role": str(row.get("type") or "retailer"),
            "times_cited": row.get("times_cited"),
        }
        for row in _as_list(evidence.get("retailer_hosts"))
        if isinstance(row, Mapping)
    ]
    return build_controller_profile(hosts)


def _sku_query_phrase(value: Any) -> str:
    text = str(value or "").strip()
    return f'"{text}"' if text else "the tested SKU prompt"


def _sku_source_route_first_move(
    prompt: Mapping[str, Any],
    merchant_path: Mapping[str, Any],
) -> str:
    route = str(prompt.get("source_route") or "").strip().lower()
    ownership = str(prompt.get("ownership_state") or "").strip().lower()
    query = _sku_query_phrase(prompt.get("query"))
    competitors = _phrase(_as_str_list(prompt.get("competitors")), "the named competitor")
    page = _merchant_page_label(merchant_path)
    profile = _route_prompt_controller_profile(prompt)
    direct_play = (
        f"Make {page} the more citable + buyable canonical path for {query}: first-order "
        "offer, starter + replenishment bundle, subscription incentive, and "
        "why-buy-direct proof"
    )
    if is_canonical_source_vacuum(profile):
        return (
            f"Make {page} more retrievable and extractable for {query}: target "
            "the exact lane wording, add product/offer/review/FAQ schema, state "
            f"{_authority_attribute_phrase(prompt)} in page text, and keep "
            "direct-buy mechanics until after the authority work."
        )
    if str(profile.get("strategy") or "") == "source_authority_gap":
        return (
            f"Make {page} more retrievable, extractable, and authoritative for "
            f"{query}: target the exact lane wording, add product/offer/review/FAQ "
            f"schema, state {_authority_attribute_phrase(prompt)} in page text, "
            "then work the cited source by controller type."
        )
    if ownership in {"retailer-owned", "marketplace-owned"} or route in {"retailer", "marketplace"}:
        return f"{direct_play}; then fix the cited retailer/marketplace listings."
    if ownership == "publisher-owned" or route == "publisher":
        return f"{direct_play}; then pitch the cited publisher for roundup inclusion."
    if ownership == "forum-owned" or route == "forum":
        return f"{direct_play}; then build review/UGC proof that points back to it."
    return (
        f"{direct_play}; then publish comparison proof against {competitors}."
    )


def _sku_source_route_self_serve(
    prompt: Mapping[str, Any],
    merchant_path: Mapping[str, Any],
) -> str:
    route = str(prompt.get("source_route") or "").strip().lower()
    ownership = str(prompt.get("ownership_state") or "").strip().lower()
    sources = _phrase(
        _host_names(prompt.get("sources")),
        "fragmented sources with no single cited site",
    )
    query = _sku_query_phrase(prompt.get("query"))
    page = _merchant_page_label(merchant_path)
    profile = _route_prompt_controller_profile(prompt)
    if is_canonical_source_vacuum(profile):
        return (
            f"On {page}, make {query} more retrievable and extractable: use the "
            "exact lane wording, add product/offer/review/FAQ schema, and state "
            f"{_authority_attribute_phrase(prompt)} in plain page text."
        )
    if str(profile.get("strategy") or "") == "source_authority_gap":
        return (
            f"On {page}, answer {query} with official proof, reviews, availability, "
            "images, product/offer/review/FAQ schema, and source-consistent facts "
            f"before working {sources}."
        )
    if ownership in {"retailer-owned", "marketplace-owned"} or route in {"retailer", "marketplace"}:
        return (
            f"On {page}, tie {query} to a first-order offer, starter + replenishment "
            "bundle, subscription incentive, and why-buy-direct block so the cited "
            f"page gives buyers a reason to choose you over {sources}."
        )
    if ownership == "publisher-owned" or route == "publisher":
        return (
            f"On {page}, answer {query} with proof, reviews, offer mechanics, "
            "bundle/subscription options, and a why-buy-direct block so the canonical "
            f"page is worth sending buyers to before pitching {sources}."
        )
    if ownership == "forum-owned" or route == "forum":
        return (
            f"Collect reviews/UGC that answer {query}, then link the proof back to "
            f"{page} with the direct buying reason."
        )
    return (
        f"On {page}, add comparison content for {query} with substantiated differences, "
        "proof, and the offer/bundle/subscription reason to buy from you."
    )


def _sku_source_route_follow_up(
    prompt: Mapping[str, Any],
    merchant_path: Mapping[str, Any],
) -> str:
    route = str(prompt.get("source_route") or "").strip().lower()
    ownership = str(prompt.get("ownership_state") or "").strip().lower()
    sources = _phrase(
        _host_names(prompt.get("sources")),
        "fragmented sources with no single cited site",
    )
    query = _sku_query_phrase(prompt.get("query"))
    page = _merchant_page_label(merchant_path)
    profile = _route_prompt_controller_profile(prompt)
    if is_canonical_source_vacuum(profile):
        return (
            f"Then {_controller_source_route_action(profile, sources, query, page)} "
            "Keep SKU name, attributes, availability, and images consistent across "
            f"{page} and {sources}; re-audit the lane and verify materiality before "
            "treating exposure as lost buyer traffic."
        )
    if str(profile.get("strategy") or "") == "source_authority_gap":
        return (
            f"Then {_controller_source_route_action(profile, sources, query, page)} "
            "Keep SKU name, attributes, availability, and images consistent across "
            f"{page} and {sources}; re-audit the lane and verify materiality before "
            "treating exposure as lost buyer traffic."
        )
    if ownership in {"retailer-owned", "marketplace-owned"} or route in {"retailer", "marketplace"}:
        return (
            f"Then audit {sources} for wrong titles, images, variants, price, stock, "
            "authorization, and SKU facts so third-party exposure stops outranking "
            f"{_merchant_destination(merchant_path)}."
        )
    if ownership == "publisher-owned" or route == "publisher":
        return (
            f"Then pitch {sources} with the exact SKU facts, proof assets, images, "
            "pricing/availability, and comparison angle from your canonical page."
        )
    if ownership == "forum-owned" or route == "forum":
        return (
            f"Then seed the review proof where {sources} can discover it, without "
            "claiming a community placement until it is actually evidenced."
        )
    return (
        "Then update the source trail that AI cites so the comparison points back "
        f"to {_merchant_destination(merchant_path)}."
    )


def _authority_attribute_phrase(prompt: Mapping[str, Any]) -> str:
    values = (
        _as_str_list(prompt.get("why_fit"))
        or _as_str_list(prompt.get("attribute_basis"))
        or _as_str_list(prompt.get("evidence"))
    )
    return _phrase(values[:5], "the evidenced lane attributes")


def _controller_types(profile: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for row in _as_list(profile.get("classified_controllers")):
        if not isinstance(row, Mapping):
            continue
        for key in ("input_role", "type"):
            value = str(row.get(key) or "").strip().lower()
            if value:
                out.append(value)
    return out


def _controller_source_route_move_type(profile: Mapping[str, Any]) -> str:
    types = set(_controller_types(profile))
    if types & _COMMUNITY_CONTROLLER_TYPES:
        return "community_source_participation"
    if types & _PUBLISHER_CONTROLLER_TYPES:
        return "publisher_source_pitch"
    if types & _LISTING_CONTROLLER_TYPES:
        return "retailer_listing_accuracy"
    return "evidenced_source_update"


def _controller_source_route_action(
    profile: Mapping[str, Any],
    controller: str,
    lane: str,
    page: str,
) -> str:
    move_type = _controller_source_route_move_type(profile)
    if move_type == "community_source_participation":
        return (
            f"participate in or seed accurate product info in the {controller} "
            f"discussion for {lane}, using only facts already published on {page}"
        )
    if move_type == "publisher_source_pitch":
        return (
            f"pitch {controller} for {lane} with exact SKU facts, proof assets, "
            f"images, availability, and the canonical page at {page}"
        )
    if move_type == "retailer_listing_accuracy":
        return (
            f"claim or fix the {controller} listing for {lane}: title, images, "
            "variant, stock, authorization, and SKU facts"
        )
    return (
        f"work the evidenced source trail around {controller} for {lane} with "
        f"the same facts published on {page}"
    )


def _sentence(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    return cleaned[:1].upper() + cleaned[1:].rstrip(".") + "."


def _authority_mechanism_steps(
    *,
    lane: str,
    page: str,
    destination: str,
    controller: str,
    profile: Mapping[str, Any],
    attributes: str,
) -> List[Dict[str, str]]:
    source_action = _controller_source_route_action(profile, controller, lane, page)
    source_move_type = _controller_source_route_move_type(profile)
    return [
        {
            "type": "retrieval_lane_rank",
            "action": (
                f"Build {page} to rank for the exact lane {lane}: use the lane "
                "wording in the title, H1, metadata, and body copy so the page is "
                "more retrievable and citable."
            ),
            "why": (
                f"Grounded shopping engines retrieve web results before extracting "
                f"facts, so {page} has to surface for {lane}."
            ),
        },
        {
            "type": "structured_extraction_schema",
            "action": (
                f"Add product/offer/review/FAQ schema on {page} for {lane}, "
                "covering price, availability, reviews, and the decision questions."
            ),
            "why": (
                "Structured data makes the official facts easier to extract without "
                "inventing prices, review counts, or claims."
            ),
        },
        {
            "type": "facts_in_page_text",
            "action": (
                f"State {attributes} in plain page text for {lane}, not only in "
                "images, PDFs, or variant labels."
            ),
            "why": (
                "Grounded answers lift readable text; page assets alone do not give "
                "the engine stable lane facts."
            ),
        },
        {
            "type": "reviews_authority_gap",
            "action": (
                f"Build verified review and proof signals on {page} so the page is "
                f"more authoritative for {lane}."
            ),
            "why": (
                f"{controller} currently carries the trust signal; the official "
                "page needs its own review/proof layer."
            ),
        },
        {
            "type": source_move_type,
            "action": _sentence(source_action),
            "why": (
                "Work only the evidenced controller path; the source move changes "
                "by controller type and does not invent new channels."
            ),
        },
        {
            "type": "source_fact_consistency",
            "action": (
                f"Keep SKU name, {attributes}, images, availability, and canonical "
                f"URL consistent across {page} and {controller}."
            ),
            "why": (
                "Consistent facts help the engine trust one version of the SKU "
                "instead of a third-party variant."
            ),
        },
        {
            "type": "measure_reaudit_materiality",
            "action": (
                f"Re-audit {lane} after the changes; verify whether exposure becomes "
                f"more citable through {destination} before treating it as material "
                "buyer traffic."
            ),
            "why": (
                "Exposure is citation evidence, not proof of downstream buyer traffic "
                "until the lane is measured again."
            ),
        },
        {
            "type": "direct_buy_reason",
            "action": (
                "After the page is more retrievable, extractable, and authoritative, "
                "add first-order offer, starter + replenishment bundle, subscription "
                "incentive, and why-buy-direct proof."
            ),
            "why": (
                f"Conversion mechanics belong last; they give {destination} a buying "
                "reason after the source-authority work is credible."
            ),
        },
    ]


def _sku_sideways_wedge_reason(sideways_wedge: Mapping[str, Any]) -> str:
    beachhead = _as_mapping(sideways_wedge.get("recommended_beachhead_lane"))
    why = str(sideways_wedge.get("why_this_lane_not_the_head_prompt") or "").strip()
    if not beachhead or not why:
        return ""
    return f" {why}"


def _sku_operator_moves(
    *,
    route_prompt: Mapping[str, Any],
    merchant_path: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    query = str(route_prompt.get("query") or "").strip()
    sources = _host_names(route_prompt.get("sources"))
    page = _merchant_page_label(merchant_path)
    destination = _merchant_destination(merchant_path)
    lane = query or "the exposed lane"
    controller = _phrase(sources, "fragmented sources with no single cited site")
    profile = _route_prompt_controller_profile(route_prompt)
    if is_canonical_source_vacuum(profile) or str(profile.get("strategy") or "") == "source_authority_gap":
        steps = _authority_mechanism_steps(
            lane=lane,
            page=page,
            destination=destination,
            controller=controller,
            profile=profile,
            attributes=_authority_attribute_phrase(route_prompt),
        )
        return [
            {
                "type": step["type"],
                "lane": lane,
                "move": step["action"],
                "why": step["why"],
                "evidence": {
                    "controllers": sources[:3],
                    "controller_strategy": profile.get("strategy"),
                    "exposure_confidence": profile.get("exposure_confidence"),
                    "exposure_read": profile.get("exposure_read"),
                },
            }
            for step in steps
        ]
    return [
        {
            "type": "retail_listing_accuracy",
            "lane": lane,
            "move": (
                f"Claim or fix the {controller} listing for {lane}: title, images, "
                "variant, price, stock, authorization, and SKU facts."
            ),
            "why": (
                f"Credible retail controllers are present, so keep the listing facts "
                f"accurate while {destination} competes for the click."
            ),
            "evidence": {
                "controllers": sources[:3],
                "controller_strategy": profile.get("strategy"),
            },
        },
        {
            "type": "light_retrieval_schema_layer",
            "lane": lane,
            "move": (
                f"Make {page} more retrievable for {lane} and add product/offer "
                "schema, without treating credible retailers like weak citation "
                "sources."
            ),
            "why": (
                "The authority layer helps extraction, but the main fight is still "
                "listing accuracy plus a better direct-buy reason."
            ),
            "evidence": {
                "controllers": sources[:3],
                "controller_strategy": profile.get("strategy"),
            },
        },
        {
            "type": "first_order_offer",
            "lane": lane,
            "move": f"Attach a first-order offer to the more citable + buyable {page} for {lane}.",
            "why": f"The answer is already exposed through {controller}; buyers need a reason to choose {destination}.",
            "evidence": {"controllers": sources[:3]},
        },
        {
            "type": "bundle_or_replenishment",
            "lane": lane,
            "move": f"Create a starter + replenishment bundle on the more citable + buyable {page} for {lane}.",
            "why": "A bundle gives AI and shoppers a concrete value reason to prefer the merchant-controlled path.",
            "evidence": {"controllers": sources[:3]},
        },
        {
            "type": "subscription_or_why_buy_direct",
            "lane": lane,
            "move": (
                "Add subscription incentive and why-buy-direct proof: guarantee, samples, "
                "loyalty, returns, stock, and fresh product facts."
            ),
            "why": f"Those details make {destination} materially different from the cited third-party route and ready for agent checkout.",
            "evidence": {"controllers": sources[:3]},
        },
    ]


def _canonical_page_play(
    *,
    lane: str,
    controllers: List[str],
    page: str,
    destination: str,
    goal: str,
    controller_profile: Optional[Mapping[str, Any]] = None,
    attributes: str = "the evidenced lane attributes",
) -> Dict[str, Any]:
    controller = _phrase(controllers, "fragmented sources with no single cited site")
    profile = dict(controller_profile or build_controller_profile(controllers))
    if is_canonical_source_vacuum(profile) or str(profile.get("strategy") or "") == "source_authority_gap":
        steps = _authority_mechanism_steps(
            lane=lane,
            page=page,
            destination=destination,
            controller=controller,
            profile=profile,
            attributes=attributes,
        )
        moves = [
            {
                "type": step["type"],
                "operator_action": step["action"],
                "why": step["why"],
            }
            for step in steps
        ]
        checkout_readiness = (
            f"Make {page} more retrievable, extractable, authoritative, buyable, "
            "and agent-checkout ready, then re-audit this same lane."
        )
    else:
        moves = [
            {
                "type": "retail_listing_accuracy",
                "operator_action": (
                    f"Claim or fix {controller} listings for {lane}: title, images, "
                    "variant, price, stock, authorization, and SKU facts."
                ),
                "why": (
                    "Credible retail controllers are present, so the merchant should "
                    "repair listing facts while competing for the direct-buy click."
                ),
            },
            {
                "type": "light_retrieval_schema_layer",
                "operator_action": (
                    f"Make {page} more retrievable for {lane} and add product/offer "
                    "schema as a light authority layer."
                ),
                "why": (
                    "This supports extraction from the merchant page without treating "
                    "credible retailers like weak citation sources."
                ),
            },
            {
                "type": "first_order_offer",
                "operator_action": (
                    f"Attach a first-order offer to {page} for {lane}."
                ),
                "why": (
                    f"AI already exposes the lane through {controller}; the owned "
                    "page needs a direct buying reason."
                ),
            },
            {
                "type": "starter_replenishment_bundle",
                "operator_action": (
                    f"Add a starter + replenishment bundle on {page} for {lane}."
                ),
                "why": (
                    "The bundle gives the answer and the shopper a concrete value "
                    "reason to prefer the merchant-controlled path."
                ),
            },
            {
                "type": "subscription_or_why_buy_direct",
                "operator_action": (
                    "Add subscription incentive and why-buy-direct proof: guarantee, "
                    "samples, loyalty, returns, stock, and fresh product facts."
                ),
                "why": (
                    f"Those are merchant-owned promises {controller} cannot safely "
                    "represent as the direct buying path."
                ),
            },
        ]
        checkout_readiness = (
            f"Make {page} more citable, buyable, and agent-checkout ready before trying "
            "to redirect the cited source trail."
        )
    return {
        "lane": lane,
        "controllers": controllers[:3],
        "controller_strategy": profile.get("strategy"),
        "controller_strategy_label": profile.get("label"),
        "controller_profile": profile,
        "exposure_confidence": profile.get("exposure_confidence"),
        "exposure_read": profile.get("exposure_read"),
        "page": page,
        "destination": destination,
        "goal": goal,
        "economics_policy": _OFFER_ECONOMICS_POLICY,
        "moves": moves,
        "checkout_readiness": checkout_readiness,
    }


def _sku_canonical_page_play(
    *,
    route_prompt: Mapping[str, Any],
    merchant_path: Mapping[str, Any],
) -> Dict[str, Any]:
    query = str(route_prompt.get("query") or "").strip()
    return _canonical_page_play(
        lane=query or "the exposed lane",
        controllers=_host_names(route_prompt.get("sources")),
        page=_merchant_page_label(merchant_path),
        destination=_merchant_destination(merchant_path),
        goal=str(merchant_path.get("goal") or ""),
        controller_profile=_route_prompt_controller_profile(route_prompt),
        attributes=_authority_attribute_phrase(route_prompt),
    )


def _brand_canonical_page_play(
    evidence: Mapping[str, Any],
    merchant_path: Mapping[str, Any],
) -> Dict[str, Any]:
    queries = _query_examples(evidence.get("failed_query_examples"))
    lane = (queries[0].strip("\"") if queries else "") or "the retailer-routed demand"
    return _canonical_page_play(
        lane=lane,
        controllers=_host_names(evidence.get("retailer_hosts")),
        page=_merchant_page_label(merchant_path),
        destination=_merchant_destination(merchant_path),
        goal=str(merchant_path.get("goal") or ""),
        controller_profile=_brand_controller_profile(evidence),
    )


def _brand_operator_moves(
    evidence: Mapping[str, Any],
    merchant_path: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    queries = _query_examples(evidence.get("failed_query_examples"))
    hosts = _host_names(evidence.get("retailer_hosts"))
    lane = queries[0] if queries else "the retailer-routed questions"
    page = _merchant_page_label(merchant_path)
    destination = _merchant_destination(merchant_path)
    controller = _phrase(hosts, "the cited retailers")
    profile = _brand_controller_profile(evidence)
    if is_canonical_source_vacuum(profile):
        return [
            {
                "type": "canonical_source_authority",
                "lane": lane,
                "move": (
                    f"Make {page} more retrievable and citable for the retailer-routed demand: "
                    "exact product naming, canonical URL, structured data, proof, "
                    "stock, returns, and authorized where-to-buy."
                ),
                "why": (
                    f"AI grounding points to {controller}; that reads as an official-source "
                    "gap before it is a retailer value fight."
                ),
                "evidence": {
                    "controllers": hosts[:3],
                    "controller_strategy": profile.get("strategy"),
                    "exposure_confidence": profile.get("exposure_confidence"),
                    "exposure_read": profile.get("exposure_read"),
                },
            },
            {
                "type": "authorized_distribution_or_reseller_cleanup",
                "lane": lane,
                "move": (
                    "Audit the weak third-party trail for wrong titles, images, variants, "
                    "stock, authorization, and stale facts; decide which real authorized "
                    "retail routes deserve investment."
                ),
                "why": (
                    "Random reseller exposure should not be the source AI trusts; "
                    "verify materiality before framing it as lost buyer traffic."
                ),
                "evidence": {
                    "controllers": hosts[:3],
                    "controller_strategy": profile.get("strategy"),
                    "exposure_confidence": profile.get("exposure_confidence"),
                    "exposure_read": profile.get("exposure_read"),
                },
            },
            {
                "type": "direct_buy_reason",
                "lane": lane,
                "move": (
                    "After the official page is source-ready, add first-order offer, "
                    "starter + replenishment bundle, subscription incentive, and why-buy-direct proof."
                ),
                "why": f"Those mechanics give {destination} a concrete reason to become the buyable route once it is citable.",
                "evidence": {
                    "controllers": hosts[:3],
                    "controller_strategy": profile.get("strategy"),
                    "exposure_confidence": profile.get("exposure_confidence"),
                    "exposure_read": profile.get("exposure_read"),
                },
            },
        ]
    return [
        {
            "type": "first_order_offer",
            "lane": lane,
            "move": f"Put a first-order offer on {page} for the retailer-routed demand.",
            "why": f"AI already points shoppers to {controller}; the merchant path needs a better conversion reason.",
            "evidence": {"controllers": hosts[:3]},
        },
        {
            "type": "bundle_subscription",
            "lane": lane,
            "move": "Add starter + replenishment bundle and subscription incentive to the same page.",
            "why": f"Bundle/subscription value gives shoppers a concrete reason to choose {destination}.",
            "evidence": {"controllers": hosts[:3]},
        },
        {
            "type": "why_buy_direct",
            "lane": lane,
            "move": "Add a why-buy-direct block: guarantee, samples, loyalty, returns, stock, and fresh facts.",
            "why": "These are merchant-controlled promises; do not invent retailer facts or unsupported economics.",
            "evidence": {"controllers": hosts[:3]},
        },
    ]


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
    merchant_path = _as_mapping(evidence.get("merchant_path"))
    retailer_profile = _brand_controller_profile(evidence)

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
        if is_canonical_source_vacuum(retailer_profile):
            return _base_payload(
                primary_gap=primary_gap,
                prescription_class="operational_efficiency",
                merchant_path=merchant_path,
                operator_moves=_brand_operator_moves(evidence, merchant_path),
                canonical_page_play=_brand_canonical_page_play(evidence, merchant_path),
                headline="AI cites weak third-party retail routes — make your site the canonical source.",
                why_this_first=(
                    f"AI answers show the lane, but grounding points to {retailer_phrase} "
                    f"instead of {_merchant_destination(merchant_path)}. {retailer_profile.get('exposure_read')} "
                    "Fix the official-source gap before framing this as a retailer value fight."
                ),
                first_move=(
                    f"Make {_merchant_page_label(merchant_path)} more retrievable and citable: "
                    "exact product naming, canonical URL, structured data, proof, stock, "
                    "and authorized where-to-buy; then audit the weak reseller trail."
                ),
                self_serve_actions=[
                    (
                        f"On {_merchant_page_label(merchant_path)}, publish facts that make the page more citable: "
                        "exact product naming, canonical URL, structured data, attributes, proof, "
                        "stock, returns, and authorized where-to-buy."
                    ),
                    (
                        f"Check the cited third-party routes ({retailer_phrase}) for wrong titles, images, "
                        "variants, stock, authorization, and stale SKU facts before deciding which real "
                        "authorized retail routes to pursue."
                    ),
                    (
                        "Add the first-order offer, starter + replenishment bundle, subscription incentive, "
                        "and why-buy-direct proof after the official page is source-ready."
                    ),
                ],
                pivota_path=(
                    "Pivota can make your own page more citable, buyable, and authoritative, "
                    "prepare it for agent checkout, and measure whether the owned buyer path improves."
                ),
                evidence_used=evidence,
                cta={
                    "label": "Claim the canonical buying path",
                    "trust_note": (
                        "Reseller cleanup and retail decisions are yours to make; Pivota "
                        "makes the official page more citable, buyable, and monitored."
                    ),
                },
            )
        return _base_payload(
            primary_gap=primary_gap,
            prescription_class="operational_efficiency",
            merchant_path=merchant_path,
            operator_moves=_brand_operator_moves(evidence, merchant_path),
            canonical_page_play=_brand_canonical_page_play(evidence, merchant_path),
            headline="AI sends your buyers to retailers — give them a better buying path.",
            why_this_first=(
                f"Shoppers can find you, but AI points them to {retailer_phrase} "
                f"instead of {_merchant_destination(merchant_path)}. That's lost margin and customer data, "
                "not a PR problem."
            ),
            first_move=(
                f"Make {_merchant_page_label(merchant_path)} the best place to buy: "
                "first-order offer, starter + replenishment bundle, subscription incentive, "
                "and why-buy-direct proof; then fix the retailer listings AI is citing."
            ),
            self_serve_actions=[
                (
                    f"Make {_merchant_page_label(merchant_path)} beat the retailer pages: "
                    "full facts, proof, reviews, current price/stock, first-order offer, "
                    "bundle, subscription option, and returns."
                ),
                (
                    f"Check the cited retailer listings ({retailer_phrase}) for wrong "
                    "titles, images, price, or stock, and decide which routes to lean into."
                ),
            ],
            pivota_path=(
                "Pivota can make your own page more citable and buyable, and "
                "measure whether those retailer-won questions start moving toward "
                "the owned buyer path."
            ),
            evidence_used=evidence,
            cta={
                "label": "Win back the buying path",
                "trust_note": (
                    "Fixing retailer listings is yours to do; Pivota makes your own "
                    "page more citable and buyable, and measures whether the owned buyer "
                    "path improves."
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
    prescription_class: Optional[str] = None,
    merchant_path: Optional[Mapping[str, Any]] = None,
    operator_moves: Optional[List[Mapping[str, Any]]] = None,
    canonical_page_play: Optional[Mapping[str, Any]] = None,
    sideways_wedge: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    out = {
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
    if prescription_class:
        out["prescription_class"] = prescription_class
    if merchant_path:
        out["merchant_path"] = dict(merchant_path)
    if operator_moves:
        out["operator_moves"] = [dict(move) for move in operator_moves]
    if canonical_page_play:
        out["canonical_page_play"] = dict(canonical_page_play)
    if sideways_wedge:
        out["sideways_wedge"] = dict(sideways_wedge)
    return out


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
        "merchant_path": _merchant_path(merchant_view=merchant_view),
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
