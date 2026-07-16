"""Deterministic brand-level next-best-action projection.

This module consumes the already-built merchant_view evidence/action
inventory and returns one merchant-facing prescription. It does not call
LLMs, does not recompute audit scores, and deliberately avoids per-SKU
rollups or content-revision wiring.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from services.text_normalization import sanitize_display_name
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
    is_synthetic_probe_query,
    is_third_party_controlled_lane,
    prioritize_lanes,
)
from services.win_plan_builder import is_broad_head_query


logger = logging.getLogger(__name__)

PRIMARY_INTEGRATION_COMPLETION = "integration_completion_gap"
PRIMARY_RETRIEVAL_FOUNDATION = "retrieval_foundation_gap"
PRIMARY_RETAILER_ROUTE_LEAK = "retailer_route_leak"
PRIMARY_CATEGORY_DISCOVERY = "category_discovery_gap"
PRIMARY_COMPETITOR_SOURCE = "competitor_source_gap"
PRIMARY_FIRST_PARTY_DEFENSE = "first_party_defense"

PRIMARY_SKU_GET_INDEXED = "get_indexed"
PRIMARY_SKU_OPEN_LANE_CAPTURE = "open_lane_capture"
PRIMARY_SKU_SUBSTITUTION_LEAK = "substitution_leak"
PRIMARY_SKU_CONTENT_REVISION_GAP = "content_revision_gap"
PRIMARY_SKU_SOURCE_ROUTE_REPAIR = "source_route_repair"
PRIMARY_SKU_PROTECTED_MONITORING = "protected_monitoring"
PRIMARY_SKU_INSUFFICIENT_DATA = "insufficient_data"

# Content-richness score at/above which a page is readable enough to chase
# demand moves. Below it (the "blocked" band) AI can't reliably describe the
# product, so the foundation fix leads — a "vs competitor" comparison can't win
# a lane the SKU can't even be described in yet. Mirrors _band_for_score's
# blocked boundary in agent_center_bd_report_service.
_CONTENT_FOUNDATION_MAX = 40

# Per-SKU CTA action descriptor. Maps the deterministic primary gap to the
# Pivota-executable action the frontend wires its "have Pivota do it" button to.
# 'request_indexing' submits the canonical page for indexing (the get-indexed
# play needs zero merchant effort); 'request_enrichment' drafts/publishes the
# enriched canonical page via the SKU_OPT_OVERLAY path; 'none' is a
# merchant-only / monitor move with no one-click Pivota job. The button POSTs
# to the existing overlay/indexing-approval endpoints with target_sku_key, so
# the frontend never has to invent routing.
SKU_CTA_ACTIONS = ("request_enrichment", "request_indexing", "none")
_SKU_CTA_ACTION: Dict[str, str] = {
    PRIMARY_SKU_GET_INDEXED: "request_indexing",
    PRIMARY_SKU_OPEN_LANE_CAPTURE: "request_enrichment",
    PRIMARY_SKU_SUBSTITUTION_LEAK: "request_enrichment",
    PRIMARY_SKU_CONTENT_REVISION_GAP: "request_enrichment",
    PRIMARY_SKU_SOURCE_ROUTE_REPAIR: "request_enrichment",
    PRIMARY_SKU_INSUFFICIENT_DATA: "request_enrichment",
    PRIMARY_SKU_PROTECTED_MONITORING: "none",
}

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

    # Weak first-party retrieval no longer requires the verdict label to be
    # exactly INVISIBLE, but it must not preempt the category-visible stories
    # (retailer route leak / category discovery both need best_visibility >= 50).
    # Without the best_visibility gate, a merchant strong in category but weak on
    # branded queries would be told to "get indexed" instead of winning the
    # click back from the retailers AI already cites.
    if attribution < 30 and visibility < 30 and best_visibility < 50:
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

    if attribution < 30 and visibility < 30:
        return PRIMARY_RETRIEVAL_FOUNDATION

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
    merchant_host: Optional[str] = None,
    sku_key: Optional[str] = None,
    catalog_unavailable: bool = False,
    competitor_intel: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build a deterministic per-SKU next-best-action prescription.

    `competitor_intel` (when provided) is the grounded "what the category winner
    is known for" probe result — {competitor, attributes_present, known_for}. It
    lets the content-gap / substitution moves name the actual winner and the
    decision factors AI credits them with, instead of generic "add more detail".

    `catalog_unavailable=True` (URL audits — a pasted product with no synced
    catalog) suppresses the "get indexed / serving" primary gate: serving
    eligibility + orderability can't be measured or fixed without a connected
    store, so leading with them would bury the URL-actionable citation insight
    behind the connect-store funnel. With the gate off, the action leads with
    open-lane / substitution / content moves the merchant can act on directly.
    """

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
        catalog_unavailable=catalog_unavailable,
    )
    evidence = _build_sku_evidence_used(
        opportunity=opportunity_map,
        primary_gaps=gaps,
        scores=_as_mapping(scores),
        failing_prompts=_as_list(failing_prompts),
        verify_summary=_as_mapping(verify_summary),
        identity=_as_mapping(identity),
        sku_title=sku_title,
        merchant_host=merchant_host,
        competitor_intel=competitor_intel,
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
    # Stamp the action target so the frontend CTA can POST to the
    # enrichment/indexing-approval endpoint without inventing routing. The
    # action itself is set by _base_payload from the primary gap.
    cta = prescription.get("cta")
    if sku_key and isinstance(cta, dict):
        cta["target_sku_key"] = sku_key
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
    competitor_attributes: Optional[Any] = None,
) -> Dict[str, Any]:
    """Optionally attach an evidence-grounded strategic brief.

    The deterministic next_best_action remains the contract and fallback. Any
    synthesis, parsing, validation, or config failure returns the original
    fields unchanged and without a strategic_brief key.
    """

    out = dict(next_best_action or {})
    try:
        from config.settings import settings
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
            competitor_attributes=competitor_attributes,
        )
        brief_debug: Dict[str, Any] = {}
        brief = await generate_sku_strategic_brief(
            evidence,
            provider=provider,
            model=model,
            debug=brief_debug,
        )
        brief_enabled = bool(getattr(settings, "strategic_brief_enabled", False))
    except Exception as exc:
        logger.warning("strategic brief attach failed; using deterministic NBA", exc_info=True)
        # Surface the failure so a missing per-SKU brief is diagnosable rather
        # than a silent fall-through to generic NBA boilerplate.
        out["brief_status"] = "unavailable"
        out["brief_debug"] = {"outcome": "attach_exception", "error": f"{type(exc).__name__}: {exc}"[:300]}
        return out
    # Persist exactly why the brief did/didn't ship (outcome=="llm" means the
    # main LLM lane worked; anything else means it fell back — diagnosable from
    # the stored report). Keep it compact. When the feature is OFF we leave the
    # NBA untouched (no debug noise).
    if brief_enabled:
        out["brief_debug"] = brief_debug
    if brief:
        out["strategic_brief"] = brief
        out["brief_status"] = "ok"
        out["brief_source"] = "llm" if brief_debug.get("outcome") == "llm" else "deterministic"
    elif brief_enabled:
        # Feature is on but no brief was produced (e.g. grounding rejected every
        # attempt). Make it visible instead of silently dropping to boilerplate.
        out["brief_status"] = "unavailable"
        logger.warning(
            "strategic brief unavailable for sku_title=%r despite feature enabled",
            sku_title,
        )
    return out


def _sku_not_indexed(
    primary_gaps: List[Mapping[str, Any]],
    scores: Mapping[str, Any],
) -> bool:
    """True when the SKU isn't live/serving in the AI shopping surface.

    Indexing is the prerequisite for every other move: an un-indexed SKU
    can't be cited no matter how good the page is. Detected from the
    routability `serving_eligibility` bucket scoring zero (serving_eligible is
    False), with a fallback to the merchant-safe primary-gap signal.
    """
    routability = _as_mapping(scores.get("routability"))
    breakdown = _as_mapping(routability.get("breakdown"))
    serving = _as_mapping(breakdown.get("serving_eligibility"))
    if serving:
        max_points = int(serving.get("max") or 0)
        points = int(serving.get("points") or 0)
        if max_points > 0 and points == 0:
            return True
    # Fallback (breakdown absent): a full serving_eligibility gap surfaced.
    for gap in primary_gaps:
        if str(gap.get("bucket") or "") == "serving_eligibility":
            max_points = int(gap.get("max") or 0)
            if max_points > 0 and int(gap.get("gap") or 0) >= max_points:
                return True
    return False


def _classify_sku_primary_gap(
    *,
    opportunity: Mapping[str, Any],
    primary_gaps: List[Mapping[str, Any]],
    scores: Mapping[str, Any],
    identity: Mapping[str, Any],
    catalog_unavailable: bool = False,
) -> str:
    # Indexing comes first — an un-indexed SKU can't be cited regardless of
    # page quality or open lanes, so this gates every other primary move.
    # NOTE: this is the per-SKU (v3) surface's get-indexed path. The legacy
    # brand-report path surfaces the same intent via the `get_indexed`
    # playbook in audit_playbook_engine (select_playbooks); the two engines
    # serve different report surfaces.
    #
    # URL audits (catalog_unavailable) skip this gate: serving eligibility is
    # un-measurable + un-fixable without a connected store, so it's the
    # connect-store funnel, not the lead action.
    if not catalog_unavailable and _sku_not_indexed(primary_gaps, scores):
        return PRIMARY_SKU_GET_INDEXED

    if _sku_top_open_lane(opportunity):
        return PRIMARY_SKU_OPEN_LANE_CAPTURE

    # Foundation before comparison: when the page is too thin for AI to describe
    # (content in the blocked band), lead with the content fix even if a
    # substitution signal is present — you can't win a "vs competitor"
    # comparison for a product AI can't read yet. Gated on the content SCORE,
    # not on a content gap making the top-N primary_gaps, so it still fires when
    # citation is the single biggest gap (the ANUKO case: content 14, top gap
    # citation).
    if _sku_content_critically_thin(scores):
        return PRIMARY_SKU_CONTENT_REVISION_GAP

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
    merchant_host: Optional[str],
    competitor_intel: Optional[Any] = None,
) -> Dict[str, Any]:
    sideways_wedge = _as_mapping(opportunity.get("sideways_wedge")) or build_sideways_wedge(
        _as_list(opportunity.get("per_prompt")),
        product_evidence=_as_mapping(opportunity.get("product_evidence")),
    )
    return {
        "competitor_intel": _sku_competitor_intel_chip(competitor_intel),
        "sku_title": _sku_title(identity=identity, sku_title=sku_title),
        "identity": dict(identity),
        # The merchant's own host — copy that names "the host buyers get
        # routed through" must never name the merchant to themselves.
        "merchant_host": merchant_host,
        "merchant_path": _merchant_path(identity=identity, opportunity=opportunity),
        "scores": _sku_scores(scores),
        "top_open_lane": _sku_lane_chip(_sku_top_open_lane(opportunity)),
        "substitution_alert": dict(_as_mapping(opportunity.get("substitution_alert"))),
        "content_gap": _sku_gap_chip(_sku_top_content_gap(primary_gaps)),
        "source_route_prompt": _sku_prompt_chip(
            _sku_source_route_prompt(opportunity),
            merchant_host=merchant_host,
        ),
        "coverage": dict(_as_mapping(opportunity.get("confidence"))),
        "demand_state_summary": opportunity.get("demand_state_summary"),
        "intent_ladder": dict(_as_mapping(opportunity.get("intent_ladder"))),
        "sideways_wedge": dict(sideways_wedge),
        # Interleaved by provider BEFORE the slice: failing_prompts is
        # provider-grouped, so a plain [:5] persisted single-engine chips
        # (live Mojawa run: 5/5 Gemini while ChatGPT losses existed).
        "failing_prompt_examples": [
            _sku_failing_prompt_chip(prompt)
            for prompt in _interleave_failing_prompts(failing_prompts)[:5]
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

    if primary_gap == PRIMARY_SKU_GET_INDEXED:
        return _base_payload(
            primary_gap=primary_gap,
            headline=f"Get {sku_title} indexed so AI can find it.",
            why_this_first=(
                f"{sku_title} isn't live in the AI shopping surface yet, so assistants "
                "can't see or recommend it. Until it's indexed, nothing else you do to "
                "the page can move your visibility — this is the first step."
            ),
            first_move=(
                f"Get {sku_title} live and crawlable: confirm it's published, in your "
                "sitemap, and has a canonical product page AI can reach."
            ),
            self_serve_actions=[
                (
                    "Confirm the product is published (not hidden or draft) and its page "
                    "returns a clean, fast, crawlable URL."
                ),
                (
                    "Make sure it's in your sitemap and the page shows the core product "
                    "facts — title, price, availability, and a clear description."
                ),
            ],
            pivota_path=(
                f"Pivota submits {sku_title}'s canonical page for indexing and tracks it "
                "to live, then re-audits to measure real AI visibility — you don't need "
                "to do anything to start."
            ),
            evidence_used=evidence,
            cta=_sku_cta("Get this product indexed"),
        )

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
        # Niche-first reframe: when the ONLY substitution evidence is a broad
        # head prompt ("best headphones" -> Bose), a "publish a vs-Bose
        # comparison" first move sells a mid/long-tail brand the hardest
        # possible fight. Name the reality (flagship owns the head question)
        # and point the first move at the specific lane the brand can win —
        # the measured beachhead lane when one exists, the specific buyer
        # asks in the prompt table otherwise. Specific-prompt substitutions
        # keep the comparison play below (there the fight IS winnable).
        if substitution.get("broad_head_prompt"):
            beachhead = _as_mapping(
                sideways_wedge.get("recommended_beachhead_lane")
            )
            # The beachhead is picked by a DIFFERENT classifier (axis/lane
            # shape) than the head flag (query text shape) — an attribute-axis
            # prompt like "best waterproof earbuds" can be both the flagged
            # head prompt AND the beachhead, which would render "X owns the
            # broad Q — win Q first". Only pivot to a beachhead that is
            # specific by the SAME text classifier, isn't the alert prompt
            # itself, and isn't a synthetic padding query (which _sku_query_
            # phrase would blur into circular "category question" copy).
            _bh_raw = str(beachhead.get("query") or "").strip()
            _alert_raw = str(substitution.get("prompt") or "").strip()
            beachhead_usable = bool(
                _bh_raw
                and _bh_raw.lower() != _alert_raw.lower()
                and not is_synthetic_probe_query(_bh_raw)
                and not is_broad_head_query(
                    _bh_raw, prompt_source=beachhead.get("prompt_source"),
                )
            )
            # Phrase the lane from its evidenced attributes ("the IP68
            # waterproof certified, open-ear, daily sports ask") — the raw
            # attribute-stacked probe spec reads like the SKU title reworded,
            # and "add a FAQ that answers <your own title>" confused the
            # exact merchant this reframe is for.
            _basis_all = [
                str(b).strip()
                for b in (beachhead.get("attribute_basis") or [])
                if str(b or "").strip()
            ]
            # Phrase length cap: an attribute item can be a whole clause
            # ("no ear pressure no water trapped in ears") — prefer the
            # short, name-like attributes for the lane phrase and skip
            # clause-length ones while shorter alternatives exist.
            _basis = [b for b in _basis_all if len(b) <= 32] or _basis_all[:1]
            lane_phrase = (
                f"the {', '.join(_basis[:3])} ask"
                if _basis
                else _sku_query_phrase(_bh_raw)
            ) if beachhead_usable else None
            # The DETAIL move depends on what the lane's measurement actually
            # says — not a one-size "add a section and FAQ":
            #   third-party routed — AI already surfaces the product here, but
            #     buyers resolve to a marketplace/retailer listing; the move
            #     is winning the ROUTE (why-buy-direct), not the answer.
            #   open lane — nobody owns it; create the answer, and say WHAT
            #     to write (the evidenced attributes).
            #   competitor-owned — take it with a section that leads with the
            #     evidenced attributes the competitor's page can't claim.
            _ownership = str(beachhead.get("ownership_state") or "").strip().lower()
            # who_owns is host-or-LIST (ties on times_cited persist as a
            # list — test-pinned shape); normalize to ONE host and never
            # name the merchant's own domain as the thing buyers are
            # "routed through" (controllers can include the first-party
            # host — the chip builder doesn't exclude it).
            _merchant_host = str(
                evidence.get("merchant_host") or ""
            ).strip().lower()

            def _owner_host(value: Any) -> str:
                if isinstance(value, (list, tuple)):
                    value = next((v for v in value if v), "")
                host = str(value or "").strip().lower()
                return "" if (host and host == _merchant_host) else host

            _route_host = _owner_host(beachhead.get("who_owns")) or next(
                (
                    h
                    for h in (
                        _owner_host(c)
                        for c in (beachhead.get("controllers") or [])
                    )
                    if h
                ),
                "",
            )
            _basis_words = ", ".join(_basis[:4]) if _basis else "your evidenced product facts"
            if beachhead_usable and _ownership in {
                "marketplace-owned", "retailer-owned",
            } and _route_host:
                first_move = (
                    f"AI already surfaces you for {lane_phrase} — but buyers "
                    f"get routed through {_route_host}. Make your own page "
                    "the cited, buyable answer."
                )
                lane_actions = [
                    (
                        f"Add a why-buy-direct block to the product page — "
                        "guarantee, returns, support, a first-order offer — "
                        f"so choosing you over {_route_host} is obvious."
                    ),
                    (
                        f"Put the lane's facts on YOUR page in plain buyer "
                        f"words ({_basis_words}) so AI can ground the answer "
                        "on you directly instead of the listing."
                    ),
                ]
            elif beachhead_usable and _ownership in {
                "publisher-owned", "forum-owned",
            } and _route_host:
                # Publishers/forums don't sell — "why buy direct vs
                # healthline.com" reads as nonsense. The play there is
                # getting INTO the source AI grounds on.
                first_move = (
                    f"AI answers {lane_phrase} from {_route_host} — get "
                    f"cited there: pitch them with your evidenced facts."
                )
                lane_actions = [
                    (
                        f"Pitch {_route_host} to include you for this ask, "
                        f"leading with {_basis_words} — the facts their "
                        "current answer is missing."
                    ),
                    (
                        f"Put the same facts on your own page ({_basis_words}) "
                        "so AI can also ground the answer on you directly."
                    ),
                ]
            elif beachhead_usable and _ownership == "competitor-owned" and _route_host:
                first_move = (
                    f"Take {lane_phrase} from {_route_host}: publish a page "
                    f"section that leads with {_basis_words}."
                )
                lane_actions = [
                    (
                        f"Write the section in plain buyer words, leading "
                        f"with {_basis_words} — the evidenced facts "
                        f"{_route_host}'s page can't claim."
                    ),
                    (
                        "Back it with proof AI can cite: a couple of real "
                        "reviews and one use-case comparison for this ask."
                    ),
                ]
            elif beachhead_usable:
                first_move = (
                    f"Create the answer for {lane_phrase}: a page section + "
                    f"FAQ leading with {_basis_words}."
                )
                lane_actions = [
                    (
                        f"Answer {_sku_query_phrase(_bh_raw)} on your product "
                        f"page in plain buyer words, leading with {_basis_words}."
                    ),
                    (
                        "Give AI a reason to cite you there: a couple of real "
                        "reviews and one use-case comparison page — not a "
                        f"head-on {substitute} fight."
                    ),
                ]
            else:
                first_move = (
                    "Win a specific buyer question first — pick one from "
                    "your prompt table and answer it on your page."
                )
                lane_actions = [
                    (
                        "Pick the most specific losing question in your "
                        "prompt table and answer it on your product page "
                        "in plain buyer words."
                    ),
                    (
                        "Give AI a reason to cite you there: a couple of real "
                        "reviews and one use-case comparison page — not a "
                        f"head-on {substitute} fight."
                    ),
                ]
            return _base_payload(
                primary_gap=primary_gap,
                headline=(
                    f"{substitute} owns the broad {prompt} question — "
                    "win your specific lane first."
                ),
                why_this_first=(
                    f"On {prompt}, AI answers with {substitute}. That's a "
                    "big-budget head term — outranking them there is the "
                    "most expensive first fight you can pick. The faster win "
                    "is the specific ask you're already a match for; head "
                    "visibility follows brands that own their niches."
                ),
                first_move=first_move,
                self_serve_actions=lane_actions,
                pivota_path=first_pivota_path,
                evidence_used=evidence,
                cta=_sku_cta("Win the specific ask AI already gets"),
            )
        # If the grounded winner probe assessed THIS substitute, fold in the
        # decision factors AI credits them with so the comparison meets them
        # head-on instead of a generic "vs" page. Only when the names match, so
        # we never attribute one competitor's factors to another.
        win = _as_mapping(evidence.get("competitor_intel"))
        attrs_phrase = (
            _phrase(_as_str_list(win.get("attributes")), "")
            if str(win.get("competitor") or "").strip().lower() == substitute.lower()
            else ""
        )
        compare_action = (
            f"Make a clear {sku_title} vs {substitute} comparison — meet them on "
            f"what AI credits {substitute} for ({attrs_phrase}) where you match, "
            "plus use cases, proof, and price — so the choice is obvious."
            if attrs_phrase
            else (
                f"Make a clear {sku_title} vs {substitute} comparison: use cases, "
                "what's different, proof, and price, so the choice is obvious."
            )
        )
        return _base_payload(
            primary_gap=primary_gap,
            headline=f"When buyers ask for alternatives, AI names {substitute}, not {sku_title}.",
            why_this_first=(
                f"On {prompt}, AI answers with {substitute} instead of {sku_title}. "
                "Those buyers are already shopping your category, they just don't "
                "have a reason to pick you yet."
            ),
            first_move=f"Publish a comparison — {sku_title} vs {substitute} — showing when you win.",
            self_serve_actions=[
                compare_action,
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
        # Merchant-safe copy only: the gap's _GAP_DISPLAY label/why, never the
        # scoring bucket name ("product quality score" is an internal metric,
        # not something a merchant can add to a page).
        gap_phrase = _sku_gap_label(content_gap)
        gap_why = str(content_gap.get("why") or "").strip()
        if gap_why and not gap_why.endswith("."):
            gap_why += "."
        # Competitor-targeted framing: when we know who wins the category and the
        # decision factors AI credits them with (grounded probe, not fabricated),
        # name them so the merchant fills the page with the specifics that decide
        # THIS category — not a generic "add more detail". Falls back to the
        # generic copy when there's no assessed winner (competitor_intel empty).
        win = _as_mapping(evidence.get("competitor_intel"))
        competitor = str(win.get("competitor") or "").strip()
        attrs_phrase = _phrase(_as_str_list(win.get("attributes")), "")
        if competitor:
            headline = (
                f"Give AI enough on {sku_title}'s page to pick you over {competitor}."
            )
            why_this_first = (
                f"When shoppers ask the category question, AI recommends "
                f"{competitor}, not {sku_title} — and {sku_title}'s page is still "
                "too thin for AI to compare you against it. "
                + (
                    f"AI credits {competitor} for {attrs_phrase}; cover those same "
                    f"decision points on your page (only where they're true of "
                    f"{sku_title}) so AI can weigh you against them. "
                    if attrs_phrase
                    else ""
                )
                + "That's the first fix."
            )
            first_move = (
                f"Strengthen {sku_title}'s page: {gap_phrase}"
                + (
                    f", leading with the specifics AI rewards {competitor} for — "
                    f"{attrs_phrase}"
                    if attrs_phrase
                    else ""
                )
                + "."
            )
            self_serve_actions = [
                (
                    f"State the decision factors AI credits {competitor} with"
                    + (f" — {attrs_phrase} — " if attrs_phrase else " ")
                    + f"in plain text on {sku_title}'s page, only where they're "
                    "genuinely true of your product."
                ),
                (
                    "Fill the rest in plain language — what it is, who it's for, "
                    "how to use it, the facts a buyer needs to decide — then "
                    "re-run the audit to confirm they landed."
                ),
            ]
        else:
            headline = f"Fill the gaps on {sku_title}'s page before chasing reach."
            why_this_first = (
                "AI can't confidently recommend a product it can't read. "
                + (
                    gap_why
                    or f"{sku_title}'s page doesn't yet answer what shoppers ask."
                )
                + " That's the first fix."
            )
            first_move = f"Strengthen {sku_title}'s page: {gap_phrase}."
            self_serve_actions = [
                (
                    "Fill the gap in plain language: what it is, who it's for, "
                    "how to use it, and the facts a buyer needs to decide."
                ),
                (
                    "Make sure those facts show up in the page itself, then re-run "
                    "the audit to confirm they landed."
                ),
            ]
        return _base_payload(
            primary_gap=primary_gap,
            headline=headline,
            why_this_first=why_this_first,
            first_move=first_move,
            self_serve_actions=self_serve_actions,
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
    """Per-SKU CTA. `action` (request_enrichment | request_indexing | none) is
    added by _base_payload from the primary gap, and `target_sku_key` is stamped
    by build_sku_next_best_action, so the frontend can wire a real button."""
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


def _sku_content_critically_thin(scores: Mapping[str, Any]) -> bool:
    """True when the SKU's page content is in the blocked band — too thin for AI
    to reliably describe the product. Gates the foundation-first ordering:
    prioritise the content fix over demand moves (substitution) when AI can't
    even read the page yet. Score None (unmeasured) is NOT thin — don't override
    a real demand signal on missing data."""
    content = _as_mapping(scores.get("content_richness"))
    score = content.get("score")
    # None (unmeasured) or a non-numeric score is NOT "thin" — never override a
    # real demand signal on missing/garbage data.
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return False
    return _score(score) < _CONTENT_FOUNDATION_MAX


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
    # Sanitize before it renders into NBA copy: a dirty identity name (e.g.
    # "...30 sticks's page" from a leaked template) must not surface verbatim.
    # Conservative — legit names pass through unchanged. See sanitize_display_name.
    return (
        sanitize_display_name(identity.get("name"))
        or sanitize_display_name(sku_title)
        or "this SKU"
    )


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
    """Merchant-facing evidence chip for a scoring gap. Carries ONLY merchant-safe
    copy: the `label`/`why` from _GAP_DISPLAY. The raw scoring keys
    (dimension="citation", bucket="first_party_rate"), the internal `reason`,
    the humanized bucket ("product quality score" is an internal metric name),
    and the 0-100 points/max/gap model are internal vocabulary and must NOT
    ride into the merchant-facing next-best-action evidence — they have no
    frontend consumer and the sanitizer does not reach this chip. The raw gap
    objects keep those keys for internal sort/match; the chip does not."""
    if not gap:
        return {}
    return {
        "label": gap.get("label"),
        "why": gap.get("why"),
    }


def _sku_competitor_intel_chip(competitor_intel: Optional[Any]) -> Dict[str, Any]:
    """Merchant-safe chip for the grounded category-winner probe.

    Carries the winner's name + the decision factors AI credits them with
    (`attributes_present`, humanized) so the content-gap / substitution moves can
    target the actual winner instead of prescribing generic "add more detail".
    Empty dict when the probe wasn't run or found no assessed winner — callers
    fall back to the generic copy. Only grounded, AI-named attributes ride in; no
    claim is made about the merchant's own product."""
    intel = _as_mapping(competitor_intel)
    if intel.get("status") != "assessed":
        return {}
    competitor = str(intel.get("competitor") or "").strip()
    if not competitor:
        return {}
    attrs: List[str] = []
    seen = set()
    for raw in _as_str_list(intel.get("attributes_present")):
        label = re.sub(r"[_\-]+", " ", raw).strip().lower()
        if label and label not in seen:
            seen.add(label)
            attrs.append(label)
    return {
        "competitor": competitor,
        "attributes": attrs[:4],
    }


def _sku_prompt_chip(
    prompt: Optional[Mapping[str, Any]],
    *,
    merchant_host: Optional[str] = None,
) -> Dict[str, Any]:
    if not prompt:
        return {}
    source_summary = _as_mapping(prompt.get("source_summary"))
    stable_sources = _exclude_merchant_host_sources(
        stable_buyer_path_controllers_for_row(prompt),
        merchant_host=merchant_host,
    )
    out = {
        "query": prompt.get("query"),
        "ownership_state": prompt.get("ownership_state"),
        "source_route": prompt.get("source_route"),
        "opportunity_score": prompt.get("opportunity_score"),
        "why_fit": prompt.get("attribute_basis") or prompt.get("evidence"),
        "sources": stable_sources[:3],
        "raw_top_cited_hosts": _exclude_merchant_host_sources(
            source_summary.get("top_cited_hosts"),
            merchant_host=merchant_host,
        )[:3],
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


def _exclude_merchant_host_sources(
    sources: Any,
    *,
    merchant_host: Optional[str],
) -> List[Any]:
    excluded = _normalize_host_value(merchant_host)
    if not excluded:
        return list(_as_list(sources))
    out: List[Any] = []
    for row in _as_list(sources):
        host = ""
        if isinstance(row, Mapping):
            host = _normalize_host_value(
                row.get("host") or row.get("domain") or row.get("url") or row.get("uri")
            )
        else:
            host = _normalize_host_value(row)
        if host and _same_or_subdomain(host, excluded):
            continue
        out.append(row)
    return out


def _same_or_subdomain(host: str, parent: str) -> bool:
    return bool(host and parent and (host == parent or host.endswith("." + parent)))


def _normalize_host_value(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else f"//{text}")
    host = parsed.netloc or parsed.path.split("/", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host else ""


def _interleave_failing_prompts(failing_prompts: List[Any]) -> List[Any]:
    """Niche-first, then provider-fair: specific failing prompts fill the
    capped evidence slots before any head baseline (which otherwise sat at
    the head of its provider bucket and always took a slot), and within each
    partition providers interleave so no engine crowds the other out."""
    from services.win_plan_builder import interleave_by_provider

    rows = [p for p in failing_prompts if isinstance(p, Mapping)]
    specific = [
        r for r in rows
        if not is_broad_head_query(r.get("query"), prompt_source=r.get("prompt_source"))
    ]
    head = [
        r for r in rows
        if is_broad_head_query(r.get("query"), prompt_source=r.get("prompt_source"))
    ]
    return interleave_by_provider(specific) + interleave_by_provider(head)


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
        # Generator stamp (llm_winnable/llm_scenario) — MUST survive into the
        # chip: the report-summary evidence selector exempts spec-matched
        # prompts from head-term filtering by this stamp, and the chips are
        # its PREFERRED input (review P1: dropping it here silently killed
        # the exemption on the primary path).
        "prompt_source": prompt.get("prompt_source"),
    }


# Generic merchant-safe phrase when a content gap carries no display label
# (e.g. foundation-first fired on the content SCORE with no content gap in the
# top-N list). Honest and never an internal metric name.
_CONTENT_GAP_FALLBACK_PHRASE = "richer product detail"


def _sku_gap_label(gap: Mapping[str, Any]) -> str:
    """Merchant-safe phrase for a content gap, shaped for mid-sentence use.

    Uses the gap chip's _GAP_DISPLAY label ("Richer product detail" ->
    "richer product detail") — NEVER the humanized bucket name: "product
    quality score" / "model readiness" are Pivota-internal metrics, and telling
    a merchant to "add the missing product quality score to your page" is the
    flagged credibility-killer (ANUKO 2026-07-03 re-run, DamDam review). The
    first char is lowercased only when the label isn't an acronym/proper form
    ("AI-ready content" stays as-is)."""
    label = str(gap.get("label") or "").strip()
    if label:
        if len(label) > 1 and label[1:2].islower():
            return label[0].lower() + label[1:]
        return label
    return _CONTENT_GAP_FALLBACK_PHRASE


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
    # Safety net: a synthetic placeholder ("<title> shopper question N") must
    # never leak into merchant-facing copy. Lanes are already filtered upstream
    # (_top_open_lanes); this guards any other caller.
    if not text or is_synthetic_probe_query(text):
        return "this product's category question"
    return f'"{text}"'


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
            f"Then {_controller_source_route_action(profile, sources, query, page)}. "
            "Keep SKU name, attributes, availability, and images consistent across "
            f"{page} and {sources}; re-audit the lane and verify materiality before "
            "treating exposure as lost buyer traffic."
        )
    if str(profile.get("strategy") or "") == "source_authority_gap":
        return (
            f"Then {_controller_source_route_action(profile, sources, query, page)}. "
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


def _split_controllers_by_move_type(profile: Mapping[str, Any]):
    forum: List[str] = []
    publisher: List[str] = []
    other: List[str] = []
    seen: set[str] = set()
    for row in _as_list(profile.get("classified_controllers")):
        if not isinstance(row, Mapping):
            continue
        host = str(row.get("host") or "").strip()
        if not host or host in seen:
            continue
        seen.add(host)
        role = str(row.get("input_role") or row.get("type") or "").strip().lower()
        if role in _COMMUNITY_CONTROLLER_TYPES:
            forum.append(host)
        elif role in _PUBLISHER_CONTROLLER_TYPES:
            publisher.append(host)
        else:
            other.append(host)
    return forum, publisher, other


def _controller_source_route_action(
    profile: Mapping[str, Any],
    controller: str,
    lane: str,
    page: str,
) -> str:
    move_type = _controller_source_route_move_type(profile)
    if move_type == "community_source_participation":
        forum, publisher, other = _split_controllers_by_move_type(profile)
        if publisher or other:
            # Mixed controller sets: address each by its own play instead of
            # calling non-community sources part of "the discussion".
            clauses: List[str] = []
            if forum:
                clauses.append(
                    f"participate in or seed accurate product info in the "
                    f"{_phrase(forum, controller)} discussion"
                )
            if publisher:
                clauses.append(
                    f"pitch {_phrase(publisher, controller)} with exact SKU facts, "
                    "proof assets, images, and availability"
                )
            if other:
                clauses.append(
                    f"work the evidenced source trail around {_phrase(other, controller)}"
                )
            return (
                f"{_phrase(clauses, '')} for {lane}, using only facts already "
                f"published on {page}"
            )
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


def _first_specific_query(queries: List[str]) -> str:
    """First example query that isn't a broad head term. These brand-level
    example strings arrive in attribution-run order (head baselines first)
    and carry no prompt_source — a plain [0] made "best headphones" THE lane
    of the canonical-page play and operator moves. Text classifier only;
    falls back to the first query when every example is head-shaped.

    _query_examples wraps each query in literal double quotes — strip them
    before classifying, or the leading quote defeats the prefix rules and
    every quoted head term classifies as specific (review P1: the whole
    fix was a no-op without this)."""
    for q in queries:
        if not is_broad_head_query(str(q or "").strip().strip('"')):
            return q
    return queries[0] if queries else ""


def _brand_canonical_page_play(
    evidence: Mapping[str, Any],
    merchant_path: Mapping[str, Any],
) -> Dict[str, Any]:
    queries = _query_examples(evidence.get("failed_query_examples"))
    lane = _first_specific_query(queries).strip("\"") or "the retailer-routed demand"
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
    lane = _first_specific_query(queries) or "the retailer-routed questions"
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
        # Use the gap's merchant-safe label/why, NEVER the raw scoring
        # dimension.bucket ("citation.first_party_rate") or internal `reason` —
        # those are internal vocabulary and read as nonsense to a merchant.
        label = str(gap.get("label") or "").strip()
        why = str(gap.get("why") or "").strip()
        moves.append({
            "title": label or "Strengthen your next-biggest visibility gap",
            "severity": "medium" if _score(gap.get("gap")) < 20 else "high",
            "lever": "sku_gap_repair",
            "target_host": None,
            "concrete_next_step": (
                "Strengthen this next, after the top-priority move above."
            ),
            "reason": why or (
                "This is the next area to strengthen after your top priority above."
            ),
            # Only the merchant-safe label/why — never the chip's `descriptor`
            # (a humanized bucket like "first party rate" reads as jargon here).
            "evidence": {"label": gap.get("label"), "why": gap.get("why")},
        })
        if len(moves) >= 2:
            return moves

    # Niche-first + provider-fair (was raw failing_prompts[0], i.e. probe
    # order: the head baseline became the prescribed PDP-revision target).
    for prompt in _interleave_failing_prompts(failing_prompts):
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
        # Build the "you're losing the answer to X" clause without the
        # "{failed_query_phrase} went to {competitor_phrase}" templating bug:
        # when there are no failed-query examples the phrase falls back to the
        # noun "no failed-query examples", which read as "no failed-query
        # examples went to <competitors>". Branch on what evidence we actually
        # have so the sentence is always grammatical.
        _has_failed = bool(_query_examples(evidence.get("failed_query_examples")))
        _has_competitors = bool(_as_str_list(evidence.get("competitors_named")))
        if _has_failed and _has_competitors:
            _losing_clause = (
                f"Category questions like {failed_query_phrase} surfaced "
                f"{competitor_phrase} instead of you."
            )
        elif _has_competitors:
            _losing_clause = (
                f"Those category questions surface {competitor_phrase} "
                "instead of you."
            )
        elif _has_failed:
            _losing_clause = (
                f"Category questions like {failed_query_phrase} aren't "
                "surfacing your product."
            )
        else:
            _losing_clause = (
                "Those category questions aren't surfacing your product."
            )
        return _base_payload(
            primary_gap=primary_gap,
            headline="Shoppers who know you find you — new shoppers don't.",
            why_this_first=(
                "When people search the category instead of your name, you're not "
                f"in the answer. {_losing_clause} "
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
                    # Same guard as why_this_first above: only name competitors
                    # when we actually have them, else competitor_phrase falls
                    # back to the noun "no repeated named competitors" and the
                    # verb slot reads broken ("...belong next to no repeated...").
                    (
                        f"Pitch the sites AI cites for these ({source_or_cited}) "
                        f"on why you belong next to {competitor_phrase}."
                    )
                    if _has_competitors else
                    (
                        f"Pitch the sites AI cites for these ({source_or_cited}) "
                        "on why you belong in the category answer."
                    )
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
    tracking_metrics = _tracking_metrics_for_gap(
        primary_gap=primary_gap,
        evidence=evidence_used,
    )
    evidence_read = _evidence_read_for_gap(
        primary_gap=primary_gap,
        evidence=evidence_used,
    )
    out = {
        "primary_gap": primary_gap,
        "headline": headline,
        "why_this_first": why_this_first,
        "first_move": first_move,
        "self_serve_actions": list(self_serve_actions[:2]),
        "pivota_path": pivota_path,
        "evidence_used": dict(evidence_used),
        "evidence": dict(evidence_used),
        "evidence_summary": evidence_read["summary"],
        "evidence_chips": evidence_read["chips"],
        "self_serve": list(self_serve_actions[:2]),
        "pivota_assisted": [pivota_path],
        "tracking_metrics": tracking_metrics,
        "how_to_track": list(tracking_metrics),
        "secondary_moves": [],
        "cta": dict(cta),
    }
    # Per-SKU prescriptions carry an executable action descriptor on the CTA so
    # the frontend can wire a real button (target_sku_key is stamped later by
    # build_sku_next_best_action). Brand-report gaps are left untouched.
    if primary_gap in _SKU_CTA_ACTION:
        out["cta"]["action"] = _SKU_CTA_ACTION[primary_gap]
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


def _evidence_read_for_gap(
    *,
    primary_gap: str,
    evidence: Mapping[str, Any],
) -> Dict[str, Any]:
    scores = _as_mapping(evidence.get("scores"))
    visibility = _optional_score(scores.get("visibility"))
    attribution = _optional_score(scores.get("attribution"))
    category_visibility = _optional_score(scores.get("category_visibility"))
    sku_score_chips = _sku_score_evidence_chips(scores)

    if primary_gap == PRIMARY_INTEGRATION_COMPLETION:
        missing = _integration_missing_labels(evidence.get("integration_missing_pieces"))
        missing_phrase = _phrase(missing, "required setup pieces")
        return {
            "summary": f"Setup is incomplete: {missing_phrase} still needs to be connected.",
            "chips": [f"Missing: {missing_phrase}"],
        }

    if primary_gap == PRIMARY_RETRIEVAL_FOUNDATION:
        chips = _brand_score_chips(
            visibility=visibility,
            attribution=attribution,
            category_visibility=category_visibility,
        )
        if visibility is not None and attribution is not None:
            summary = (
                f"Visibility is {visibility} and first-party attribution is {attribution}, "
                "so the official path is not yet reliably retrievable."
            )
        else:
            summary = "The official path is not yet reliably retrievable in this audit."
        return {"summary": summary, "chips": chips}

    if primary_gap == PRIMARY_RETAILER_ROUTE_LEAK:
        best_visibility = max(
            visibility if visibility is not None else 0,
            category_visibility if category_visibility is not None else 0,
        )
        route_gap = best_visibility - (attribution if attribution is not None else 0)
        visibility_label = (
            "Category visibility"
            if category_visibility is not None and category_visibility >= (visibility or 0)
            else "Visibility"
        )
        summary = (
            f"{visibility_label} is {best_visibility} while first-party "
            f"attribution is {attribution if attribution is not None else 0}: "
            f"a {route_gap}-point route gap."
        )
        chips = _brand_score_chips(
            visibility=visibility,
            attribution=attribution,
            category_visibility=category_visibility,
        )
        hosts = _host_names(evidence.get("retailer_hosts"))
        if hosts:
            chips.append("Retailer routes: " + _phrase(hosts, "retailers"))
        return {"summary": summary, "chips": chips[:4]}

    if primary_gap == PRIMARY_CATEGORY_DISCOVERY:
        category_gap = (
            (visibility if visibility is not None else 0)
            - (category_visibility if category_visibility is not None else 0)
        )
        summary = (
            f"Named visibility is {visibility if visibility is not None else 0} "
            f"vs category visibility {category_visibility if category_visibility is not None else 0}: "
            f"a {category_gap}-point category gap."
        )
        chips = _brand_score_chips(
            visibility=visibility,
            attribution=attribution,
            category_visibility=category_visibility,
        )
        competitors = _as_str_list(evidence.get("competitors_named"))[:4]
        if competitors:
            chips.append("Competitors named: " + _phrase(competitors, "competitors"))
        return {"summary": summary, "chips": chips[:4]}

    if primary_gap == PRIMARY_COMPETITOR_SOURCE:
        source_hosts = _host_names(evidence.get("source_hosts"))
        competitors = _as_str_list(evidence.get("competitors_named"))[:4]
        summary = (
            f"The same source trail is teaching AI to name {_phrase(competitors, 'competitors')} "
            f"on {_tracking_lane(evidence)}."
        )
        chips = []
        if source_hosts:
            chips.append("Sources: " + _phrase(source_hosts, "cited sources"))
        if competitors:
            chips.append("Competitors named: " + _phrase(competitors, "competitors"))
        return {"summary": summary, "chips": chips}

    if primary_gap == PRIMARY_FIRST_PARTY_DEFENSE:
        chips = _brand_score_chips(
            visibility=visibility,
            attribution=attribution,
            category_visibility=category_visibility,
        )
        summary = "The tested surface is healthy; the job is to defend it and watch drift."
        return {"summary": summary, "chips": chips}

    if primary_gap == PRIMARY_SKU_GET_INDEXED:
        return {
            "summary": "This product isn't live in the AI shopping surface yet — get it indexed first.",
            "chips": sku_score_chips,
        }

    if primary_gap == PRIMARY_SKU_OPEN_LANE_CAPTURE:
        lane = _as_mapping(evidence.get("top_open_lane"))
        summary = f"Open lane found: {_sku_query_phrase(lane.get('query') or '')}."
        return {"summary": summary, "chips": sku_score_chips}

    if primary_gap == PRIMARY_SKU_SUBSTITUTION_LEAK:
        substitution = _as_mapping(evidence.get("substitution_alert"))
        substitute = str(substitution.get("substituted_by") or "a substitute").strip()
        prompt = _sku_query_phrase(substitution.get("prompt") or "")
        return {
            "summary": f"On {prompt}, AI substitutes {substitute} for this SKU.",
            "chips": sku_score_chips,
        }

    if primary_gap == PRIMARY_SKU_CONTENT_REVISION_GAP:
        content_gap = _sku_gap_label(_as_mapping(evidence.get("content_gap")))
        return {
            "summary": f"The first SKU repair is page evidence: {content_gap}.",
            "chips": sku_score_chips,
        }

    if primary_gap == PRIMARY_SKU_SOURCE_ROUTE_REPAIR:
        lane = _tracking_source_lane(evidence)
        hosts = _tracking_source_hosts(evidence)
        summary = f"AI is routing {lane} through {hosts}, not the official path."
        chips = list(sku_score_chips)
        if hosts:
            chips.append(f"Controllers: {hosts}")
        return {"summary": summary, "chips": chips[:4]}

    if primary_gap == PRIMARY_SKU_PROTECTED_MONITORING:
        return {
            "summary": "This SKU is protected on the tested surface; monitor for drift.",
            "chips": sku_score_chips,
        }

    return {
        "summary": "The first job is to complete the product evidence foundation.",
        "chips": sku_score_chips,
    }


def _brand_score_chips(
    *,
    visibility: Optional[int],
    attribution: Optional[int],
    category_visibility: Optional[int],
) -> List[str]:
    chips: List[str] = []
    if visibility is not None:
        chips.append(f"Visibility {visibility}")
    if attribution is not None:
        chips.append(f"Attribution {attribution}")
    if category_visibility is not None:
        chips.append(f"Category visibility {category_visibility}")
    return chips


def _sku_score_evidence_chips(scores: Mapping[str, Any]) -> List[str]:
    labels = {
        "identity": "Identity",
        "content_richness": "Page content",
        "routability": "Buying path",
        "citation": "Citation",
    }
    chips: List[str] = []
    for key, label in labels.items():
        value = _optional_score(scores.get(key))
        if value is not None:
            chips.append(f"{label} {value}")
    return chips


def _tracking_metrics_for_gap(
    *,
    primary_gap: str,
    evidence: Mapping[str, Any],
) -> List[str]:
    lane = _tracking_lane(evidence)
    cited_hosts = _tracking_hosts(evidence)
    competitors = _tracking_competitors(evidence)
    source_lane = _tracking_source_lane(evidence)
    source_hosts = _tracking_source_hosts(evidence)
    open_lane = _tracking_top_lane(evidence)
    substitute = str(
        _as_mapping(evidence.get("substitution_alert")).get("substituted_by")
        or "the substitute"
    ).strip()
    content_gap = _sku_gap_label(_as_mapping(evidence.get("content_gap")))

    if primary_gap == PRIMARY_INTEGRATION_COMPLETION:
        return [
            "Store and payment setup marked complete.",
            f"Same audit compared after setup, with first-party citation rate on {lane} measured against this baseline.",
        ]
    if primary_gap == PRIMARY_RETRIEVAL_FOUNDATION:
        return [
            "Official URLs indexed for the top pages tested in this audit.",
            f"First-party citation rate on {lane}.",
            f"Fewer answers routing to {cited_hosts} before the merchant page appears.",
        ]
    if primary_gap == PRIMARY_RETAILER_ROUTE_LEAK:
        return [
            f"First-party citation rate on {lane}.",
            f"Share of cited buying paths going to the merchant-owned page versus {cited_hosts}.",
            "Checkout starts or orders from the owned agent-ready path once instrumented.",
        ]
    if primary_gap == PRIMARY_CATEGORY_DISCOVERY:
        return [
            f"Category visibility on {lane}.",
            f"Competitor-only answers naming {competitors} where the merchant is still absent.",
            f"New citations from {cited_hosts}.",
        ]
    if primary_gap == PRIMARY_COMPETITOR_SOURCE:
        return [
            f"Inclusion in {cited_hosts}.",
            f"Competitor-only answer count on {lane}.",
            "Citations that point back to the official page or comparison proof.",
        ]
    if primary_gap == PRIMARY_FIRST_PARTY_DEFENSE:
        return [
            "First-party citation rate on the protected prompt set.",
            "New retailer, marketplace, or competitor hosts entering the top cited list.",
            "Citation movement after major PDP, catalog, or theme changes.",
        ]
    if primary_gap == PRIMARY_SKU_GET_INDEXED:
        return [
            "The product page becomes reachable and indexed in the AI shopping surface.",
            "Re-audit shows the SKU moving from blocked to scored once it's live.",
        ]
    if primary_gap == PRIMARY_SKU_OPEN_LANE_CAPTURE:
        return [
            f"Repeat check of {open_lane} after the PDP section ships.",
            "Answers citing the official SKU page instead of leaving the lane unowned.",
        ]
    if primary_gap == PRIMARY_SKU_SUBSTITUTION_LEAK:
        # Head-reframed action (broad_head_prompt): the copy told the merchant
        # NOT to build a vs-flagship comparison page — the tracked artifact is
        # the specific-lane answer, not a comparison page it advised against.
        if _as_mapping(evidence.get("substitution_alert")).get("broad_head_prompt"):
            return [
                "Repeat check of the specific lane you targeted — answers naming this SKU there.",
                f"Answers citing your page for that lane instead of defaulting to {substitute}.",
            ]
        return [
            f"Alternative prompts that name this SKU instead of {substitute}.",
            "Answers citing the official comparison page or product proof.",
        ]
    if primary_gap == PRIMARY_SKU_CONTENT_REVISION_GAP:
        return [
            f"Product-page completeness ({content_gap}) after the missing facts are added.",
            "Failed SKU prompts that now answer from the official PDP facts.",
        ]
    if primary_gap == PRIMARY_SKU_SOURCE_ROUTE_REPAIR:
        return [
            f"First-party citation rate on {source_lane}.",
            f"{source_hosts} losing share to the official PDP or canonical buying path.",
            "Checkout starts or orders from the owned agent-ready path once instrumented.",
        ]
    if primary_gap == PRIMARY_SKU_PROTECTED_MONITORING:
        return [
            "First-party citation rate on the protected SKU prompt set.",
            "New competitor or retailer hosts entering the top cited list.",
        ]
    return [
        "Validated SKU facts present on the official page.",
        "Same prompt set checked after product evidence is complete.",
    ]


def _tracking_lane(evidence: Mapping[str, Any]) -> str:
    queries = _query_examples(evidence.get("failed_query_examples"))
    return _phrase(queries, "the same failed buyer questions")


def _tracking_hosts(evidence: Mapping[str, Any]) -> str:
    hosts = (
        _host_names(evidence.get("retailer_hosts"))
        or _host_names(evidence.get("source_hosts"))
        or _host_names(evidence.get("cited_hosts"))
    )
    return _phrase(hosts, "the cited third-party hosts")


def _tracking_competitors(evidence: Mapping[str, Any]) -> str:
    return _phrase(_as_str_list(evidence.get("competitors_named"))[:4], "named competitors")


def _tracking_source_lane(evidence: Mapping[str, Any]) -> str:
    prompt = _as_mapping(evidence.get("source_route_prompt"))
    query = str(prompt.get("query") or "").strip()
    return _sku_query_phrase(query or "the exposed SKU lane")


def _tracking_source_hosts(evidence: Mapping[str, Any]) -> str:
    prompt = _as_mapping(evidence.get("source_route_prompt"))
    hosts = _host_names(prompt.get("sources"))
    return _phrase(hosts, "cited third-party hosts")


def _tracking_top_lane(evidence: Mapping[str, Any]) -> str:
    lane = _as_mapping(evidence.get("top_open_lane"))
    query = str(lane.get("query") or "").strip()
    return _sku_query_phrase(query or "the exact open-lane query")


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
