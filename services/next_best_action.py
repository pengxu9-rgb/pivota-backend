"""Deterministic brand-level next-best-action projection.

This module consumes the already-built merchant_view evidence/action
inventory and returns one merchant-facing prescription. It does not call
LLMs, does not recompute audit scores, and deliberately avoids per-SKU
rollups or content-revision wiring.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple


PRIMARY_INTEGRATION_COMPLETION = "integration_completion_gap"
PRIMARY_RETRIEVAL_FOUNDATION = "retrieval_foundation_gap"
PRIMARY_RETAILER_ROUTE_LEAK = "retailer_route_leak"
PRIMARY_CATEGORY_DISCOVERY = "category_discovery_gap"
PRIMARY_COMPETITOR_SOURCE = "competitor_source_gap"
PRIMARY_FIRST_PARTY_DEFENSE = "first_party_defense"

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


def _prescription_for_gap(
    *,
    primary_gap: str,
    merchant_view: Mapping[str, Any],
    evidence: Mapping[str, Any],
    is_cold_start: bool,
) -> Dict[str, Any]:
    headline = _as_mapping(merchant_view.get("headline"))
    verdict_label = str(headline.get("verdict_label") or "").strip()
    scores = _as_mapping(evidence.get("scores"))
    score_text = _score_text(scores)
    retailer_phrase = _phrase(_host_names(evidence.get("retailer_hosts")), "no named retailer hosts")
    source_phrase = _phrase(_host_names(evidence.get("source_hosts")), "no named editorial hosts")
    cited_phrase = _phrase(_host_names(evidence.get("cited_hosts")), "no high-confidence cited hosts")
    competitor_phrase = _phrase(_as_str_list(evidence.get("competitors_named")), "no repeated named competitors")
    failed_query_phrase = _phrase(_query_examples(evidence.get("failed_query_examples")), "no failed-query examples")
    attr_gap = _attribution_gap(scores)
    category_gap = _category_gap(scores)

    if primary_gap == PRIMARY_INTEGRATION_COMPLETION:
        missing = _integration_missing_labels(evidence.get("integration_missing_pieces"))
        missing_phrase = _phrase(missing, "required onboarding steps")
        return _base_payload(
            primary_gap=primary_gap,
            headline="Complete the missing integration step before optimizing the audit queue.",
            why_this_first=(
                f"{score_text}. This merchant is already in onboarding, "
                f"but {missing_phrase} is still incomplete. Until that gate "
                "is closed, Pivota cannot reliably serve canonical product "
                "surfaces or complete agent checkout, so integration is the "
                "only prescription allowed to outrank the diagnostic fixes."
            ),
            first_move=(
                f"Finish {missing_phrase}, then re-run the same audit so the "
                "diagnostic actions below are measured against an active "
                "canonical and checkout path."
            ),
            self_serve_actions=[
                (
                    "Keep the official PDPs indexable while onboarding finishes: "
                    "canonical tags, robots, Product/Offer schema, price, stock, "
                    "shipping, returns, and core product facts should be current."
                ),
                (
                    f"Use the failed-query evidence ({failed_query_phrase}) to "
                    "prepare the first PDP/content fixes before the next audit."
                ),
            ],
            pivota_path=(
                f"Complete Pivota onboarding for {missing_phrase} so canonical "
                "AI-channel PDPs, normalized catalog data, monitoring, and "
                "agent checkout can operate."
            ),
            evidence_used=evidence,
            cta={
                "label": "Finish Pivota onboarding",
                "trust_note": (
                    "This leads only for non-cold merchants already in onboarding; "
                    "cold audits keep integration as the Pivota path, not the "
                    "diagnostic lead."
                ),
            },
        )

    if primary_gap == PRIMARY_RETRIEVAL_FOUNDATION:
        cold_note = (
            " If this is a cold audit, connect Pivota only after the "
            "retrieval work is clear."
            if is_cold_start
            else ""
        )
        return _base_payload(
            primary_gap=primary_gap,
            headline="Fix retrieval foundations before chasing PR or retailer expansion.",
            why_this_first=(
                f"The audit verdict is {verdict_label or 'INVISIBLE'} with "
                f"{score_text}. Failed buyer-intent queries such as "
                f"{failed_query_phrase} did not cite the merchant-owned URL; "
                f"the cited slots went to {cited_phrase}. Agents cannot "
                "reliably cite or transact through a page they cannot retrieve, "
                "parse, and trust."
            ),
            first_move=(
                "Submit and validate the canonical PDPs: sitemap, URL "
                "Inspection, indexable server-rendered content, and clean "
                "Product/Offer/Breadcrumb/FAQ schema."
            ),
            self_serve_actions=[
                (
                    "Submit the sitemap and top PDPs in Google Search Console; "
                    "inspect each URL for noindex, robots, canonical, redirect, "
                    "and server-rendering issues."
                ),
                (
                    "Repair Product, Offer, AggregateRating, BreadcrumbList, "
                    "and FAQ schema, then add factual PDP depth for price, "
                    "availability, shipping, returns, images, materials or "
                    "ingredients, sizing, variants, and buyer-intent wording."
                ),
            ],
            pivota_path=(
                "Use Pivota if you want canonical AI-channel PDPs, normalized "
                "structured product data, checkout-ready surfaces, and recurring "
                f"monitoring against these same failed queries.{cold_note}"
            ),
            evidence_used=evidence,
            cta={
                "label": "Create the canonical AI-channel path",
                "trust_note": (
                    "You can do the indexing and schema cleanup yourself; "
                    "Pivota is for canonical serving, checkout, and monitoring."
                ),
            },
        )

    if primary_gap == PRIMARY_RETAILER_ROUTE_LEAK:
        gap_phrase = (
            f" by {attr_gap} points"
            if attr_gap is not None
            else ""
        )
        return _base_payload(
            primary_gap=primary_gap,
            headline="Reclaim the first-party buying path retailers are taking.",
            why_this_first=(
                f"{score_text}. Category or product visibility is meaningful, "
                f"but first-party attribution trails{gap_phrase}; AI agents "
                f"cited retailer or marketplace routes including {retailer_phrase}. "
                "That is a margin and customer-data leak, not a generic PR problem."
            ),
            first_move=(
                "Make the official PDP the richest, clearest, most reliable "
                "buying source, then correct the retailer or marketplace pages "
                "agents already cite."
            ),
            self_serve_actions=[
                (
                    "Upgrade the official PDP beyond the cited retailer pages: "
                    "complete specs, proof, reviews, media, FAQs, current price, "
                    "availability, return policy, and truthful official-store language."
                ),
                (
                    f"Audit the cited retailer or marketplace listings ({retailer_phrase}) "
                    "for title, images, claims, variants, price, availability, and "
                    "authorization status while deciding whether to lean into or "
                    "pull back from those routes."
                ),
            ],
            pivota_path=(
                "Use Pivota to publish a canonical, agent-resolvable owned PDP, "
                "make the path transactable through checkout, and monitor whether "
                "direct attribution rises against the same retailer-captured queries."
            ),
            evidence_used=evidence,
            cta={
                "label": "Build the owned AI buying path",
                "trust_note": (
                    "Retailer cleanup is merchant-led; Pivota uniquely handles "
                    "canonical serving, agent checkout, and recurring attribution proof."
                ),
            },
        )

    if primary_gap == PRIMARY_CATEGORY_DISCOVERY:
        gap_phrase = (
            f" by {category_gap} points"
            if category_gap is not None
            else ""
        )
        return _base_payload(
            primary_gap=primary_gap,
            headline="Win category discovery before optimizing more named-product visibility.",
            why_this_first=(
                f"{score_text}. Named-product visibility beats category visibility"
                f"{gap_phrase}, which means shoppers who already know the brand "
                "can find it more easily than shoppers asking the non-branded "
                f"category question. Failed prompts such as {failed_query_phrase} "
                f"and competitors including {competitor_phrase} show where the "
                "consideration set is leaking."
            ),
            first_move=(
                "Add category-intent comparison and proof modules to the official "
                "PDPs and supporting pages, then pitch the cited sources that "
                "shape those category answers."
            ),
            self_serve_actions=[
                (
                    "Use the exact failed query wording to add intent modules: "
                    "best for, compare-to, ingredients or materials, dose or specs, "
                    "who should use it, claims substantiation, FAQs, and alternatives."
                ),
                (
                    f"Pitch cited editorial, review, listicle, creator, or retailer "
                    f"surfaces ({source_phrase if source_phrase != 'no named editorial hosts' else cited_phrase}) "
                    f"with the competitor angle: why the brand belongs next to "
                    f"{competitor_phrase}."
                ),
            ],
            pivota_path=(
                "Use Pivota to convert failed-query evidence into canonical "
                "category modules, serve them on AI-channel PDPs, and re-test "
                "category prompts monthly; integrate if the owned path also needs "
                "to become transactable."
            ),
            evidence_used=evidence,
            cta={
                "label": "Turn category gaps into canonical modules",
                "trust_note": (
                    "The content and pitching work is merchant-owned; Pivota "
                    "keeps it canonical, structured, measurable, and buyable."
                ),
            },
        )

    if primary_gap == PRIMARY_COMPETITOR_SOURCE:
        return _base_payload(
            primary_gap=primary_gap,
            headline="Get into the sources that are teaching AI to prefer competitors.",
            why_this_first=(
                f"{score_text}. Competitors including {competitor_phrase} appeared "
                f"in the failed-query evidence, and AI cited source hosts such as "
                f"{source_phrase}. Because those sources are repeated and named, "
                "the next move is targeted inclusion, not generic awareness work."
            ),
            first_move=(
                "Pitch the cited editorial, video, forum, or publisher surfaces "
                "with a concrete comparison and proof angle tied to the failed prompts."
            ),
            self_serve_actions=[
                (
                    "Use each cited host's coverage note and outreach hint to send "
                    "a specific pitch with product facts, proof assets, images, "
                    "pricing, availability, and why the product belongs in the "
                    "same comparison set as the named competitors."
                ),
                (
                    f"Build supporting pages or creator proof around the failed "
                    f"queries ({failed_query_phrase}) so newly earned coverage "
                    "has a first-party source to cite."
                ),
            ],
            pivota_path=(
                "Use Pivota to rank the cited source targets, generate evidence-backed "
                "pitch drafts from the audit, and re-test whether new coverage is "
                "indexed and cited; canonical PDPs and checkout become the owned "
                "path once inclusion starts moving."
            ),
            evidence_used=evidence,
            cta={
                "label": "Prioritize cited-source outreach",
                "trust_note": (
                    "Pivota cannot force editorial inclusion; it can identify the "
                    "right targets, draft from evidence, and measure citation movement."
                ),
            },
        )

    return _base_payload(
        primary_gap=PRIMARY_FIRST_PARTY_DEFENSE,
        headline="Defend the owned path and monitor for drift.",
        why_this_first=(
            f"{score_text}. The audit did not find a higher-confidence retailer, "
            f"category, or competitor-source leak from the available evidence "
            f"({cited_phrase}; {competitor_phrase}). Do not manufacture urgency: "
            "the right first move is to protect current first-party attribution "
            "and expand only where future audits expose a real gap."
        ),
        first_move=(
            "Keep recurring monitoring active and investigate only material drops "
            "in first-party attribution, category visibility, or competitor pressure."
        ),
        self_serve_actions=[
            (
                "Maintain schema, sitemap, canonical tags, pricing, availability, "
                "shipping, return policy, and PDP factual depth before major theme "
                "or catalog changes."
            ),
            (
                "Watch cited retailer, marketplace, editorial, and competitor pages "
                "for stale facts or new comparison language that could change AI answers."
            ),
        ],
        pivota_path=(
            "Use Pivota for recurring monitoring, drift alerts, canonical AI-channel "
            "serving, and agent checkout only if agent-direct transactability is a "
            "strategic goal."
        ),
        evidence_used=evidence,
        cta={
            "label": "Monitor AI attribution drift",
            "trust_note": (
                "Strong audits should not be scared into a rebuild; Pivota is useful "
                "when monitoring, canonical serving, or checkout needs to be durable."
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


def _score_text(scores: Mapping[str, Any]) -> str:
    visibility = _score(scores.get("visibility"))
    attribution = _score(scores.get("attribution"))
    category_visibility = _optional_score(scores.get("category_visibility"))
    if category_visibility is None:
        return f"Visibility is {visibility}/100 and attribution is {attribution}/100"
    return (
        f"Visibility is {visibility}/100, attribution is {attribution}/100, "
        f"and category visibility is {category_visibility}/100"
    )


def _attribution_gap(scores: Mapping[str, Any]) -> Optional[int]:
    attribution = _score(scores.get("attribution"))
    category_visibility = _optional_score(scores.get("category_visibility"))
    visibility = _score(scores.get("visibility"))
    basis = category_visibility if category_visibility is not None else visibility
    gap = basis - attribution
    return gap if gap >= 25 else None


def _category_gap(scores: Mapping[str, Any]) -> Optional[int]:
    category_visibility = _optional_score(scores.get("category_visibility"))
    if category_visibility is None:
        return None
    gap = _score(scores.get("visibility")) - category_visibility
    return gap if gap >= 25 else None


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
