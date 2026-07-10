"""Per-prompt SKU opportunity scoring.

Piece 3 is intentionally pure analysis: it consumes already-collected probe
runs and product attributes, then returns prompt-level ownership, density,
opportunity, and SKU-level summaries. Route wiring/rendering stays elsewhere.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from services.agent_center_bd_report_service import (
    _VERTEX_REDIRECTOR_HOSTS,
    _first_party_url_candidates,
    _flatten_probe_runs,
    _is_first_party_host,
    _run_text,
    _source_urls,
    _text_mentions_any,
    _url_in_sources,
    extract_category_competitors,
    extract_cited_hosts,
    normalize_host,
)
from services.audit_facts import compute_run_facts, parity_check, parity_measure
from services.brand_alias import derive_brand_aliases, text_mentions_brand
from services.buyer_path_stable_controllers import stable_buyer_path_controllers
from services.cited_host_classifier import classify_host
from services.sku_lane_priority import build_lane_product_evidence, is_synthetic_probe_query
from services.vertical_profiles import BEAUTY_PROFILE, VerticalProfile


# Ordering/fallback for per-prompt provider verdicts. _expected_providers also
# auto-discovers any provider present in the runs (so this list is for stable
# ordering, not a gate) — chatgpt is added as the wedge hero-SKU tri-prober.
_KNOWN_PROVIDERS = ("gemini", "deepseek", "chatgpt")
_OWNED_PROVIDER_VERDICTS = {"win", "partial"}
_SHOPPING_WORDS = {
    "buy",
    "best",
    "price",
    "shop",
    "store",
    "review",
    "reviews",
    "alternative",
    "alternatives",
    "vs",
    "compare",
    "comparison",
    "supplement",
    "for sale",
    "under",
}
_ROLE_PRIORITY = {
    "brand": 7,
    "marketplace": 6,
    "retailer": 5,
    "publisher": 4,
    "forum": 3,
    "unclassified": 1,
}
_DOMAIN_TOKEN_RE = re.compile(
    r"(?<!@)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b",
    re.IGNORECASE,
)
_COMPARISON_TOKEN_RE = re.compile(
    r"\b(?:alternatives?|dupes?|versus|vs|compare|compared|comparison)\b",
    re.IGNORECASE,
)
_NO_INFO_DENIAL_RE = re.compile(
    r"\b(?:"
    r"(?:do not|don't|does not|doesn't|did not|didn't|cannot|can't)\s+"
    r"have\s+(?:enough\s+)?(?:information|data|details|context)\b"
    r"|(?:do not|don't|does not|doesn't|did not|didn't|cannot|can't)\s+"
    r"(?:find|identify|locate|verify|determine)\b"
    r"|could(?:\s+not|n't)\s+(?:find|identify|locate|determine)\b"
    r"|no\s+(?:information|listing|listings|result|results)\b"
    r"|not\s+enough\s+information\b"
    r"|insufficient\s+information\b"
    r"|unable\s+to\s+(?:find|identify|locate|verify|determine)\b"
    r")",
    re.IGNORECASE,
)


def build_sku_opportunity(
    sku_ctx: Dict[str, Any],
    probe_runs: Any,
    *,
    attribute_graph: Optional[Dict[str, Any]] = None,
    vertical_profile: VerticalProfile = BEAUTY_PROFILE,
) -> Dict[str, Any]:
    """Build additive per-prompt opportunity intelligence for one SKU.

    `vertical_profile` selects the vertical's competitor type-token vocab for the
    substitution-brand filter (defaults to beauty = byte-identical; electronics
    drops 'wireless earbuds' as a type, not a substitute brand)."""
    product = _get_product(sku_ctx)
    graph = attribute_graph if isinstance(attribute_graph, dict) else {}
    runs = _flatten_probe_runs(probe_runs)
    groups = _group_runs_by_query(runs)
    all_providers = _expected_providers(runs)

    per_prompt = [
        _score_prompt_group(
            sku_ctx=sku_ctx,
            product=product,
            query=query,
            runs=group_runs,
            providers=all_providers,
            attribute_graph=graph,
            vertical_profile=vertical_profile,
        )
        for query, group_runs in sorted(groups.items(), key=lambda item: item[0])
    ]
    per_prompt.sort(
        key=lambda row: (
            -float(row.get("opportunity_score") or 0),
            str(row.get("query") or "").lower(),
        )
    )
    aggregate = _build_sku_aggregate(per_prompt, providers=all_providers)
    product_evidence = build_lane_product_evidence(
        product=product,
        sku_ctx=sku_ctx,
        attribute_graph=graph,
    )

    # W1 site 9 (measure): per-prompt verdicts credit the merchant on ANSWER-
    # TEXT mentions (merchant_mention -> win/partial); the decision-sheet
    # target is T3 from the same source-walk as every other surface. One line
    # per SKU quantifying the gap, computed with THIS module's exact identity
    # inputs so the measure isolates the matcher, not identity.
    parity_measure(
        "sku_opportunity.per_prompt_verdicts.mention_prompts",
        sum(
            1
            for row in per_prompt
            if any(
                v in _OWNED_PROVIDER_VERDICTS
                for v in (row.get("provider_verdicts") or {}).values()
            )
        ),
        sum(
            1
            for p in compute_run_facts(
                runs,
                merchant_host=_merchant_host(sku_ctx, product),
                merchant_brand=str(
                    product.get("brand")
                    or product.get("vendor")
                    or sku_ctx.get("merchant_brand")
                    or ""
                ),
                merchant_vendors=_merchant_identity_values(sku_ctx, product),
            ).prompts
            if p.brand_mentioned
        ),
        context={
            "sku_key": sku_ctx.get("sku_key") or product.get("sku_key"),
            "prompt_groups": len(per_prompt),
        },
    )

    return {
        "per_prompt": per_prompt,
        "sku_aggregate": aggregate,
        "product_evidence": product_evidence,
        "opportunity_formula": {
            "score": (
                "attribute_fit * intent_weight * demand_signal * "
                "(1 - density) * volume_proxy * actionability * confidence"
            ),
            "scale": "0-100 capped deterministic heuristic",
            "volume_proxy": (
                "Proxy only: derived from query class, grounded sources, "
                "and named competitors. It is not measured search volume."
            ),
        },
        # Top-level aliases keep the renderer simple without changing the
        # canonical sku_aggregate block.
        "intent_ladder": aggregate["intent_ladder"],
        "top_open_lanes": aggregate["top_open_lanes"],
        "demand_state_summary": aggregate["demand_state_summary"],
        "substitution_alert": aggregate["substitution_alert"],
        "confidence": aggregate["confidence"],
    }


def _lane_cited_evidence(
    runs: List[Dict[str, Any]],
    sku_ctx: Dict[str, Any],
    competitors: List[str],
) -> Optional[Dict[str, Any]]:
    """The verbatim AI answer excerpt for a lane plus who it named, so the
    merchant can SEE what the AI actually said — especially on lanes it LOSES,
    where the answer routes the buyer to a competitor/retailer rather than the
    merchant's own site. Prefers a run that cites an external (controller) host."""
    best: Optional[Dict[str, Any]] = None
    best_rank: Tuple[int, int] = (-1, -1)
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
        excerpt = str(
            run.get("evidence_excerpt") or parsed.get("evidence_excerpt") or ""
        ).strip()
        if not excerpt:
            continue
        external_hosts = [
            ident.get("host")
            for ident in _run_source_identifiers(run)
            if ident.get("host") and not _is_first_party_host(ident.get("host"), sku_ctx or {})
        ]
        rank = (1 if external_hosts else 0, len(excerpt))
        if rank > best_rank:
            best_rank = rank
            best = {
                "provider": run.get("_provider"),
                "excerpt": excerpt[:400],
                "cited_hosts": list(dict.fromkeys(h for h in external_hosts if h))[:3],
                "competitors_named": list(dict.fromkeys(competitors or []))[:5],
            }
    return best


def _brand_in_grounding_titles(
    runs: List[Dict[str, Any]],
    *,
    merchant_brand: Optional[str],
) -> bool:
    """True when the merchant brand appears in a grounding SOURCE TITLE — i.e.
    the cited page IS the brand's own product/store listing ("Anuko NOURISHING
    HAIR BUTTER … | Hwahae"), as opposed to an independent article whose title
    is the category ("The 15 Best Hair Butters | StyleCraze") that merely
    surfaces the brand in its body.

    This is the discriminator between FINDABILITY (the brand's own listing was
    retrieved for a category query) and ENDORSEMENT (an independent source
    recommends the brand). For a discovery/category appearance, the former is a
    far weaker signal — verified live (run 57d9183b): every ChatGPT category
    "win" for Anuko was its own Hwahae listing being retrieved, never an
    independent roundup pick."""
    needle = str(merchant_brand or "").strip().lower()
    if len(needle) < 3:
        return False
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        for src in (run.get("grounding_sources") or []):
            if not isinstance(src, dict):
                continue
            title = str(src.get("title") or "").lower()
            if title and needle in title:
                return True
    return False


def _score_prompt_group(
    *,
    sku_ctx: Dict[str, Any],
    product: Dict[str, Any],
    query: str,
    runs: List[Dict[str, Any]],
    providers: List[str],
    attribute_graph: Dict[str, Any],
    vertical_profile: VerticalProfile = BEAUTY_PROFILE,
) -> Dict[str, Any]:
    merchant_category = (
        product.get("product_type")
        or product.get("category")
        or sku_ctx.get("category")
    )
    merchant_brand = (
        product.get("brand")
        or product.get("vendor")
        or sku_ctx.get("merchant_brand")
    )
    merchant_host = _merchant_host(sku_ctx, product)
    merchant_identities = _merchant_identity_values(sku_ctx, product)
    axis_metadata = _merged_axis_metadata(runs)
    axis = str(axis_metadata.get("axis") or "").strip().lower() or "unknown"
    query_class = _query_class(query, axis)
    attribute_fit = _attribute_fit(query, axis_metadata, attribute_graph, product)
    intent = _intent_for(query, axis, axis_metadata)

    source_roles = _source_roles_for_runs(runs, merchant_category)
    source_counts = Counter({
        str(source["host"]): int(source.get("times_cited") or 0)
        for source in source_roles
    })
    cited_counter, merchant_cited_runs, runs_with_citations = extract_cited_hosts(
        runs,
        merchant_host=merchant_host,
        merchant_brand=str(merchant_brand or ""),
        merchant_vendors=merchant_identities,
    )
    # W1 T3 secondary CUTOVER (per-prompt-group opportunity scorer): the group's
    # citedness counts now derive from the RunFacts fold — same source as the verdict
    # path (slice 1) and sku_opportunity's own site-9 rollup. Parity-proven == the
    # legacy extract_cited_hosts scalars (Anuko 2026-07-04: 0 drift / 14 prompt
    # groups; test_parity_with_legacy_implementations is the hard net). The
    # parity_check calls stay as runtime tripwires; extract_cited_hosts still supplies
    # `cited_counter` (the competitor rollup).
    _grp_facts = compute_run_facts(
        runs,
        merchant_host=merchant_host,
        merchant_brand=str(merchant_brand or ""),
        merchant_vendors=merchant_identities,
    )
    try:
        parity_check(
            "sku_opportunity.prompt_group.merchant_cited_runs",
            merchant_cited_runs,
            _grp_facts.brand_mentioned_runs,
            context={"query": str(query)[:80]},
        )
        parity_check(
            "sku_opportunity.prompt_group.runs_with_citations",
            runs_with_citations,
            _grp_facts.runs_with_citations,
            context={"query": str(query)[:80]},
        )
    except Exception:  # noqa: BLE001 — parity must never sink scoring
        pass
    merchant_cited_runs = _grp_facts.brand_mentioned_runs
    runs_with_citations = _grp_facts.runs_with_citations
    source_route = _source_route(source_roles, sku_ctx)
    competitors, competitor_counts = _competitors_for_runs(
        runs,
        merchant_host=merchant_host,
        merchant_brand=str(merchant_brand or ""),
        merchant_vendors=merchant_identities,
        merchant_title=str(product.get("title") or product.get("raw_title") or ""),
    )
    any_competitor_first_party = _any_competitor_first_party(runs, competitors)
    provider_analysis = _provider_analysis(runs, sku_ctx=sku_ctx, product=product)
    # Strict SKU-verified run count across providers (vs the brand-level
    # merchant_cited_runs above) — the sibling-conflation split.
    sku_cited_runs = sum(
        int(row.get("sku_identified_runs") or 0)
        for row in provider_analysis.values()
    )
    provider_verdicts = {
        provider: provider_analysis.get(provider, {}).get("verdict", "absent")
        for provider in providers
    }
    engine_agreement = _engine_agreement(provider_verdicts)
    demand_signal = _demand_signal(runs, provider_analysis, competitors)
    density = _density(
        query=query,
        query_class=query_class,
        source_counts=source_counts,
        source_roles=source_roles,
        competitor_count=len(competitors),
        competitor_counts=competitor_counts,
        any_competitor_first_party=any_competitor_first_party,
        attribute_fit=attribute_fit,
    )
    confidence = _confidence(provider_analysis)
    volume_proxy = _volume_proxy(
        query_class=query_class,
        source_roles=source_roles,
        competitor_count=len(competitors),
    )
    actionability = _actionability(
        attribute_fit=attribute_fit,
        density_score=density["score"],
        source_route=source_route,
        demand_signal=demand_signal,
    )

    durable_competitor = _durable_competitor(competitor_counts)
    open_lane = _is_open_lane(
        query_class=query_class,
        demand_signal=demand_signal,
        durable_competitor=durable_competitor,
        source_route=source_route,
        density_score=density["score"],
        density_features=density.get("features") or {},
        attribute_fit=attribute_fit,
        intent_weight=intent["weight"],
        actionability=actionability,
        provider_analysis=provider_analysis,
    )
    ownership_state = _ownership_state(
        runs=runs,
        sku_ctx=sku_ctx,
        provider_analysis=provider_analysis,
        provider_verdicts=provider_verdicts,
        source_route=source_route,
        source_roles=source_roles,
        durable_competitor=durable_competitor,
        open_lane=open_lane,
        demand_signal=demand_signal,
        branded_transactional=_is_branded_transactional(query_class, axis, query),
    )
    who_owns = _who_owns_state(
        ownership_state=ownership_state,
        source_roles=source_roles,
        sku_ctx=sku_ctx,
        durable_competitor=durable_competitor,
    )
    substitution = _substitution(
        vertical_profile=vertical_profile,
        query=query,
        axis=axis,
        query_class=query_class,
        sku_ctx=sku_ctx,
        product=product,
        provider_analysis=provider_analysis,
        provider_verdicts=provider_verdicts,
        competitors=competitors,
        competitor_counts=competitor_counts,
    )
    opportunity_score, opportunity_factors = _opportunity_score(
        query_class=query_class,
        attribute_fit=attribute_fit,
        intent_weight=intent["weight"],
        demand_signal=demand_signal,
        density_score=density["score"],
        volume_proxy=volume_proxy,
        actionability=actionability,
        confidence=confidence,
        ownership_state=ownership_state,
    )
    demand_state = _prompt_demand_state(
        demand_signal=demand_signal,
        open_lane=open_lane,
        ownership_state=ownership_state,
    )
    top_cited_hosts = [
        {"host": host, "times_cited": count}
        for host, count in cited_counter.most_common(5)
    ]
    buyer_path_controllers = stable_buyer_path_controllers(
        who_owns=who_owns,
        top_cited_hosts=top_cited_hosts,
        source_roles=source_roles,
        source_route=source_route,
        ownership_state=ownership_state,
    )

    return {
        "query": _display_query(runs, query),
        "normalized_query": query,
        "axis": axis,
        "query_class": query_class,
        "provider_verdicts": provider_verdicts,
        "engine_agreement": engine_agreement,
        "ownership_state": ownership_state,
        "who_owns": who_owns,
        "source_roles": source_roles,
        "source_route": source_route,
        "source_summary": {
            "merchant_cited_runs": merchant_cited_runs,
            # Strict SKU-level split: merchant_cited_runs is BRAND-level (RunFacts
            # brand_mentioned_runs, parity-locked to the verdict path — unchanged);
            # sku_cited_runs counts only runs where THIS SKU was verified. The gap
            # between the two is sibling/brand-only citation.
            "sku_cited_runs": sku_cited_runs,
            "runs_with_citations": runs_with_citations,
            "top_cited_hosts": top_cited_hosts,
            "buyer_path_controllers": buyer_path_controllers,
        },
        # Brand cited on this query, but THIS SKU never verified in any answer —
        # the "AI answers with your brand/older product, not this SKU" signal
        # (Purra Swim probes answered with Run Plus content, run 7420c2b5).
        "brand_cited_sku_absent": bool(
            merchant_cited_runs > 0 and sku_cited_runs == 0
        ),
        "competitors": competitors,
        "competitor_count": len(competitors),
        "any_competitor_first_party": any_competitor_first_party,
        "density": density,
        "demand_signal": demand_signal,
        "demand_state": demand_state,
        "attribute_fit": attribute_fit,
        "intent": intent,
        "intent_weight": intent["weight"],
        "volume_proxy": volume_proxy,
        "actionability": actionability,
        "confidence": confidence,
        "opportunity_score": opportunity_score,
        "opportunity_factors": opportunity_factors,
        "open_lane": open_lane,
        # When the brand appears, did it appear via its OWN listing (brand in a
        # grounding source title) vs an independent source? Lets the discovery
        # appearance split findability from endorsement (see
        # build_product_competitiveness).
        "appearance_via_listing": _brand_in_grounding_titles(
            runs, merchant_brand=merchant_brand
        ),
        "substitution": substitution,
        "attribute_basis": _clean_basis(axis_metadata.get("sidewalk_attribute_basis")),
        "evidence": axis_metadata.get("sidewalk_evidence"),
        # Generator stamp for LLM discovery prompts (llm_winnable/llm_scenario):
        # downstream lane classification must not treat these SPECIFIC prompts
        # as generic head terms just because they share axis="category".
        "prompt_source": (
            str(axis_metadata.get("prompt_source") or "").strip() or None
        ),
        "cited_evidence": _lane_cited_evidence(runs, sku_ctx, competitors),
    }


def _group_runs_by_query(runs: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        key = _norm_query(run.get("query"))
        if not key:
            continue
        grouped[key].append(run)
    return dict(grouped)


def _expected_providers(runs: List[Dict[str, Any]]) -> List[str]:
    seen = {
        str(run.get("_provider") or run.get("provider") or "").strip().lower()
        for run in runs or []
    }
    ordered = [provider for provider in _KNOWN_PROVIDERS if provider in seen]
    extras = sorted(provider for provider in seen if provider and provider not in ordered)
    providers = ordered + extras
    return providers or list(_KNOWN_PROVIDERS)


def _provider_analysis(
    runs: List[Dict[str, Any]],
    *,
    sku_ctx: Dict[str, Any],
    product: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    by_provider: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for run in runs:
        provider = str(run.get("_provider") or run.get("provider") or "").strip().lower()
        by_provider[provider or "unknown"].append(run)

    out: Dict[str, Dict[str, Any]] = {}
    for provider, provider_runs in by_provider.items():
        verdicts = [_run_visibility_verdict(run, sku_ctx, product) for run in provider_runs]
        shopping_answer = any(v["shopping_answer"] for v in verdicts)
        competitors_named = any(v["competitors_named"] for v in verdicts)
        product_positive = any(v["product_positive"] for v in verdicts)
        grounded_positive = any(v["grounded_positive"] for v in verdicts)
        merchant_mention = any(v["merchant_mention"] for v in verdicts)
        negative = any(v["negative_verdict"] for v in verdicts)

        if grounded_positive:
            verdict = "win"
        elif product_positive or merchant_mention:
            verdict = "partial"
        elif competitors_named and (negative or not merchant_mention):
            verdict = "loss"
        elif shopping_answer:
            verdict = "partial" if merchant_mention else "loss"
        else:
            verdict = "absent"
        out[provider] = {
            "verdict": verdict,
            "shopping_answer": shopping_answer,
            "competitors_named": competitors_named,
            "product_positive": product_positive,
            "grounded_positive": grounded_positive,
            "merchant_mention": merchant_mention,
            "negative_verdict": negative,
            "grounded_first_party": any(v["grounded_first_party"] for v in verdicts),
            # Strict per-SKU identification (no brand-text fallback) — feeds the
            # sibling-conflation split (sku_cited_runs vs merchant_cited_runs).
            "sku_identified": any(v["sku_identified"] for v in verdicts),
            "sku_identified_runs": sum(1 for v in verdicts if v["sku_identified"]),
        }
    return out


def _run_visibility_verdict(
    run: Dict[str, Any],
    sku_ctx: Dict[str, Any],
    product: Dict[str, Any],
) -> Dict[str, Any]:
    """Mirror compute_citation_score's per-run visibility gates."""
    parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
    url_match = run.get("url_match") if isinstance(run.get("url_match"), dict) else {}
    llm_report = (
        url_match.get("llm_self_report")
        if isinstance(url_match.get("llm_self_report"), dict)
        else {}
    )
    product_visible = parsed.get("product_visible")
    if product_visible is None:
        product_visible = run.get("product_visible")
    if product_visible is None:
        product_visible = llm_report.get("product_visible")

    correct_sku = (
        parsed.get("correct_sku")
        if parsed.get("correct_sku") is not None
        else llm_report.get("correct_sku")
    )
    sku_mentioned = (
        parsed.get("sku_mentioned")
        if parsed.get("sku_mentioned") is not None
        else llm_report.get("sku_mentioned")
    )
    negative_verdict = (
        product_visible is False
        or correct_sku is False
        or llm_report.get("correct_sku") is False
    )

    canonical_url = product.get("canonical_url") or sku_ctx.get("canonical_url")
    pivota_url = product.get("pivota_canonical_url") or sku_ctx.get("pivota_canonical_url")
    source_identifiers = _run_source_identifiers(run)
    source_hosts = [
        str(identifier.get("host") or "")
        for identifier in source_identifiers
        if identifier.get("host")
    ]
    grounded_first_party = _first_party_grounding_primary_for_run(
        run,
        sku_ctx=sku_ctx,
        product=product,
        source_identifiers=source_identifiers,
    )
    grounded_any = bool(
        run.get("grounding_sources")
        or run.get("grounding_chunks")
        or source_hosts
    )
    text = _run_text(run)
    merchant_mention = _text_mentions_any(
        text,
        [
            product.get("title"),
            product.get("raw_title"),
            product.get("brand"),
            product.get("vendor"),
            sku_ctx.get("sku_title"),
            (sku_ctx.get("sku") or {}).get("title")
            if isinstance(sku_ctx.get("sku"), dict)
            else None,
        ],
    )
    product_positive = (
        correct_sku is True
        or product_visible is True
        or sku_mentioned is True
        or (merchant_mention and not negative_verdict)
    )
    positive_product_signal = product_positive and not negative_verdict
    # STRICT SKU-level identification — no brand-text fallback. merchant_mention
    # fires on the BRAND name too, which conflates sibling products: a Purra
    # Swim probe answered with Run Plus content counted as "merchant cited" and
    # inflated this SKU's citation rate (live run 7420c2b5). This flag is true
    # only when THIS SKU was verified in the answer (probe self-report or the
    # SKU's own canonical URL in the grounding set).
    sku_identified = bool(
        not negative_verdict
        and (
            correct_sku is True
            or sku_mentioned is True
            or product_visible is True
            or url_match.get("in_grounding") is True
        )
    )
    positive_merchant_signal = merchant_mention and not negative_verdict
    grounded_positive = bool(
        not negative_verdict
        and grounded_any
        and (correct_sku is True or product_visible is True)
    )
    competitors_named = bool(_raw_competitors(run))
    no_info_denial = _looks_like_no_info_denial(text)
    retail_marketplace_route = _run_has_retail_marketplace_route(run, product)
    category_answer = grounded_any and _mentions_product_category(text, product)
    shopping_answer = bool(
        not no_info_denial
        and (
            competitors_named
            or positive_product_signal
            or positive_merchant_signal
            or retail_marketplace_route
            or _looks_like_shopping_answer(text)
            or category_answer
        )
    )
    return {
        "product_visible": product_visible,
        "correct_sku": correct_sku,
        "sku_mentioned": sku_mentioned,
        "sku_identified": sku_identified,
        "negative_verdict": negative_verdict,
        "no_info_denial": no_info_denial,
        "grounded_first_party": grounded_first_party and not negative_verdict,
        "grounded_any": grounded_any,
        "product_positive": positive_product_signal,
        "grounded_positive": grounded_positive,
        "merchant_mention": positive_merchant_signal,
        "competitors_named": competitors_named,
        "shopping_answer": shopping_answer,
    }


def _run_source_identifiers(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return source identifiers with redirector titles resolved when possible."""
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(*, host: Optional[str], label: str, is_redirector: bool) -> None:
        clean_host = normalize_host(host or "") if host else None
        clean_label = str(label or clean_host or "").strip()
        key = clean_host or clean_label.lower()
        if not key or key in seen:
            return
        seen.add(key)
        out.append({
            "host": clean_host,
            "label": clean_label,
            "is_redirector": is_redirector,
        })

    sources_raw = run.get("grounding_sources")
    if isinstance(sources_raw, list) and sources_raw:
        for source in sources_raw:
            if not isinstance(source, dict):
                continue
            uri = source.get("uri") or ""
            title = str(source.get("title") or "").strip()
            raw_host = normalize_host(uri) or ""
            is_redirector = raw_host in _VERTEX_REDIRECTOR_HOSTS
            if is_redirector:
                add(
                    host=_host_from_source_label(title),
                    label=title,
                    is_redirector=True,
                )
            else:
                add(
                    host=raw_host or _host_from_source_label(title),
                    label=title or raw_host,
                    is_redirector=False,
                )
        return out

    for source_url in _source_urls(run):
        host = normalize_host(source_url)
        if not host or host in _VERTEX_REDIRECTOR_HOSTS:
            continue
        add(host=host, label=host, is_redirector=False)
    return out


def _first_party_grounding_primary_for_run(
    run: Dict[str, Any],
    *,
    sku_ctx: Dict[str, Any],
    product: Dict[str, Any],
    source_identifiers: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    identifiers = (
        source_identifiers
        if source_identifiers is not None
        else _run_source_identifiers(run)
    )
    if not identifiers:
        return False
    first_party = 0
    external = 0
    for identifier in identifiers:
        host = identifier.get("host")
        label = str(identifier.get("label") or "").lower()
        if (
            _is_first_party_host(host, sku_ctx or {})
            or _label_contains_first_party_host(label, sku_ctx=sku_ctx, product=product)
        ):
            first_party += 1
        else:
            external += 1
    return first_party > 0 and first_party >= external


def _label_contains_first_party_host(
    label: str,
    *,
    sku_ctx: Dict[str, Any],
    product: Dict[str, Any],
) -> bool:
    if not label:
        return False
    hosts = {
        normalize_host(product.get("canonical_url") or ""),
        normalize_host(product.get("pivota_canonical_url") or ""),
        normalize_host(sku_ctx.get("canonical_url") or ""),
        normalize_host(sku_ctx.get("pivota_canonical_url") or ""),
    }
    hosts.discard(None)
    return any(host and host in label for host in hosts)


def _host_from_source_label(label: str) -> Optional[str]:
    text = str(label or "").strip()
    if not text:
        return None
    domain_match = _DOMAIN_TOKEN_RE.search(text)
    if domain_match:
        return normalize_host(domain_match.group(0))
    classification = classify_host(text)
    if classification.get("type") != "unclassified":
        return str(classification.get("host") or "") or None
    return None


def _source_label_matches_merchant(
    label: str,
    *,
    sku_ctx: Dict[str, Any],
    product: Dict[str, Any],
) -> bool:
    label_lower = str(label or "").strip().lower()
    if not label_lower:
        return False
    merchant_host = _merchant_host(sku_ctx, product)
    if merchant_host and merchant_host in label_lower:
        return True
    merchant_brand = (
        product.get("brand")
        or product.get("vendor")
        or sku_ctx.get("merchant_brand")
    )
    if _text_mentions_any(
        label_lower,
        [
            product.get("title"),
            product.get("raw_title"),
            product.get("brand"),
            product.get("vendor"),
            sku_ctx.get("sku_title"),
            (sku_ctx.get("sku") or {}).get("title")
            if isinstance(sku_ctx.get("sku"), dict)
            else None,
        ],
    ):
        return True
    return text_mentions_brand(
        label_lower,
        derive_brand_aliases(str(merchant_brand or ""), merchant_host),
    )


def _source_roles_for_runs(
    runs: List[Dict[str, Any]],
    merchant_category: Optional[str],
) -> List[Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for run in runs:
        seen_in_run = set()
        for identifier in _run_source_identifiers(run):
            host = identifier.get("host")
            if host:
                seen_in_run.add(str(host))
        for host in seen_in_run:
            classification = classify_host(host, merchant_category=merchant_category)
            role = _normalize_role(classification.get("type"), host)
            row = rows.setdefault(
                host,
                {
                    "host": host,
                    "role": role,
                    "raw_type": classification.get("type") or "unclassified",
                    "tier": classification.get("tier"),
                    "times_cited": 0,
                },
            )
            row["times_cited"] += 1

    return sorted(
        rows.values(),
        key=lambda row: (
            -int(row.get("times_cited") or 0),
            -_ROLE_PRIORITY.get(str(row.get("role") or ""), 0),
            str(row.get("host") or ""),
        ),
    )


def _normalize_role(raw_type: Any, host: Optional[str] = None) -> str:
    role = str(raw_type or "").strip().lower()
    host_text = str(host or "").lower()
    if role == "editorial":
        return "publisher"
    if role in {"reddit", "forum", "social", "video", "community"}:
        return "forum"
    if "reddit." in host_text or host_text == "reddit.com":
        return "forum"
    if role in {"retailer", "publisher", "marketplace", "brand", "unclassified"}:
        return role
    return "unclassified"


def _source_route(source_roles: List[Dict[str, Any]], sku_ctx: Dict[str, Any]) -> str:
    if not source_roles:
        return "none"
    role_counts: Counter = Counter()
    for row in source_roles:
        host = row.get("host")
        if _is_first_party_host(host, sku_ctx or {}):
            role_counts["first-party"] += int(row.get("times_cited") or 1)
        else:
            role_counts[row.get("role") or "unclassified"] += int(row.get("times_cited") or 1)
    if not role_counts:
        return "none"
    role = _dominant_role(role_counts)
    if role is None:
        return "fragmented"
    return role


def _dominant_role(role_counts: Counter) -> Optional[str]:
    if not role_counts:
        return None
    ordered = role_counts.most_common()
    role, count = ordered[0]
    total = sum(role_counts.values()) or 1
    if len(ordered) > 1 and (count / total < 0.5 or count == ordered[1][1]):
        return None
    return str(role)


def _source_owned_state(
    *,
    source_route: str,
    source_roles: List[Dict[str, Any]],
    sku_ctx: Dict[str, Any],
) -> Optional[str]:
    role_counts: Counter = Counter()
    for row in source_roles:
        if _is_first_party_host(row.get("host"), sku_ctx or {}):
            continue
        role_counts[row.get("role") or "unclassified"] += int(row.get("times_cited") or 1)
    role = _dominant_role(role_counts)
    if role in {None, "unclassified"} and role_counts:
        non_generic = {
            key: count
            for key, count in role_counts.items()
            if key not in {"unclassified", "first-party"}
        }
        if non_generic:
            role = sorted(
                non_generic.items(),
                key=lambda item: (
                    -int(item[1]),
                    -_ROLE_PRIORITY.get(str(item[0]), 0),
                    str(item[0]),
                ),
            )[0][0]
    if role == "retailer" or source_route == "retailer":
        return "retailer-owned"
    if role == "marketplace" or source_route == "marketplace":
        return "marketplace-owned"
    if role == "publisher" or source_route == "publisher":
        return "publisher-owned"
    if role == "forum" or source_route == "forum":
        return "forum-owned"
    if role == "brand":
        return "competitor-owned"
    return None


def _first_party_source_share(
    source_roles: List[Dict[str, Any]],
    sku_ctx: Dict[str, Any],
) -> Tuple[int, int, int]:
    first_party = 0
    external_total = 0
    max_external = 0
    for row in source_roles:
        count = int(row.get("times_cited") or 1)
        if _is_first_party_host(row.get("host"), sku_ctx or {}):
            first_party += count
        else:
            external_total += count
            max_external = max(max_external, count)
    return first_party, external_total, max_external


def _first_party_dominates_sources(
    source_roles: List[Dict[str, Any]],
    sku_ctx: Dict[str, Any],
) -> bool:
    first_party, external_total, max_external = _first_party_source_share(
        source_roles,
        sku_ctx,
    )
    if first_party <= 0:
        return False
    return first_party >= max_external and first_party >= external_total


def _owner_role_for_state(ownership_state: str) -> Optional[str]:
    return {
        "publisher-owned": "publisher",
        "retailer-owned": "retailer",
        "marketplace-owned": "marketplace",
        "forum-owned": "forum",
        "competitor-owned": "brand",
    }.get(str(ownership_state or "").strip().lower())


def _dominant_owner_hosts(
    *,
    ownership_state: str,
    source_roles: List[Dict[str, Any]],
    sku_ctx: Dict[str, Any],
) -> List[str]:
    role = _owner_role_for_state(ownership_state)
    if not role:
        return []
    candidates = [
        row for row in source_roles
        if not _is_first_party_host(row.get("host"), sku_ctx or {})
        and str(row.get("role") or "") == role
        and row.get("host")
    ]
    if not candidates:
        return []
    candidates.sort(
        key=lambda row: (
            -int(row.get("times_cited") or 0),
            str(row.get("host") or ""),
        )
    )
    top_count = int(candidates[0].get("times_cited") or 0)
    hosts = [
        str(row.get("host"))
        for row in candidates
        if int(row.get("times_cited") or 0) == top_count
    ]
    return hosts[:3]


def _who_owns_state(
    *,
    ownership_state: str,
    source_roles: List[Dict[str, Any]],
    sku_ctx: Dict[str, Any],
    durable_competitor: Optional[str],
) -> Optional[Any]:
    if ownership_state == "competitor-owned" and durable_competitor:
        return durable_competitor
    hosts = _dominant_owner_hosts(
        ownership_state=ownership_state,
        source_roles=source_roles,
        sku_ctx=sku_ctx,
    )
    if not hosts:
        return None
    return hosts[0] if len(hosts) == 1 else hosts


def _first_move_for_lane(lane: Mapping[str, Any]) -> str:
    ownership = str(lane.get("current_ownership") or "").lower()
    source_route = str(lane.get("source_route") or "").lower()
    if ownership in {"retailer-owned", "marketplace-owned"} or source_route in {"retailer", "marketplace"}:
        return "Fix your listing on the cited retailer"
    if ownership == "publisher-owned" or source_route == "publisher":
        return "Pitch the cited publisher for roundup inclusion"
    if ownership == "forum-owned" or source_route == "forum":
        return "Build reviews/UGC"
    return "Add a PDP section + FAQ for this lane"


def _competitors_for_runs(
    runs: List[Dict[str, Any]],
    *,
    merchant_host: Optional[str],
    merchant_brand: str,
    merchant_vendors: Tuple[str, ...] = (),
    merchant_title: str = "",
) -> Tuple[List[str], Counter]:
    counter: Counter = Counter()
    first_seen: Dict[str, int] = {}

    def _self(name: str) -> bool:
        return _is_merchant_self(
            name,
            merchant_brand=merchant_brand,
            merchant_vendors=merchant_vendors,
            merchant_title=merchant_title,
        )

    def remember(name: str) -> None:
        if name not in first_seen:
            first_seen[name] = len(first_seen)
        counter[name] += 1

    for run in runs:
        for name in _raw_competitors(run):
            normalized = _clean_competitor_name(name)
            if not normalized:
                continue
            if _self(normalized):
                continue
            remember(normalized)

    competitor_rows, _host_rows = extract_category_competitors(
        runs,
        merchant_host=merchant_host,
        merchant_brand=merchant_brand,
        merchant_vendors=merchant_vendors,
    )
    for row in competitor_rows or []:
        normalized = _clean_competitor_name(row.get("name"))
        if not normalized or _self(normalized):
            continue
        if normalized not in first_seen:
            first_seen[normalized] = len(first_seen)
        counter[normalized] += int(row.get("times_cited") or 1)

    # Dedup among observed names: case variants (Shokz/SHOKZ), 0-vs-o typo
    # variants (H2O/H20 Audio), and product-names onto their observed brand
    # (Suunto Aqua -> Suunto). Split counts also understated competitor
    # durability (_durable_competitor needs a repeated name).
    from services.competitor_brand_filter import canonicalize_competitor_counts

    collapsed, alias_map = canonicalize_competitor_counts(dict(counter))
    counter = Counter(collapsed)
    display_first_seen: Dict[str, int] = {}
    for raw_name, order in sorted(first_seen.items(), key=lambda item: item[1]):
        display = alias_map.get(raw_name, raw_name)
        display_first_seen.setdefault(display, order)
    first_seen = display_first_seen

    names = [
        name
        for name, _count in sorted(
            counter.items(),
            key=lambda item: (-item[1], first_seen.get(item[0], 9999), item[0].lower()),
        )
    ]
    return names, counter


def _raw_competitors(run: Dict[str, Any]) -> List[Any]:
    parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
    out: List[Any] = []
    for value in (
        parsed.get("competitors_listed"),
        parsed.get("competitors_appearing"),
        run.get("competitors_listed"),
        run.get("competitors_appearing"),
    ):
        if isinstance(value, list):
            out.extend(value)
    return out


def _any_competitor_first_party(
    runs: List[Dict[str, Any]],
    competitors: List[str],
) -> bool:
    if not competitors:
        return False
    competitor_keys = [re.sub(r"[^a-z0-9]+", "", name.lower()) for name in competitors]
    competitor_words = [
        [word for word in re.findall(r"[a-z0-9]+", name.lower()) if len(word) >= 4]
        for name in competitors
    ]
    for run in runs:
        for source in run.get("grounding_sources") or []:
            if not isinstance(source, dict):
                continue
            host = normalize_host(source.get("uri") or "")
            title = str(source.get("title") or "").lower()
            haystack = re.sub(r"[^a-z0-9]+", "", " ".join([host or "", title]))
            if any(key and key in haystack for key in competitor_keys):
                return True
            host_segment = (host or "").split(".", 1)[0]
            for words in competitor_words:
                if words and all(word in host_segment for word in words):
                    return True
    return False


def _density(
    *,
    query: str,
    query_class: str,
    source_counts: Counter,
    source_roles: List[Dict[str, Any]],
    competitor_count: int,
    competitor_counts: Counter,
    any_competitor_first_party: bool,
    attribute_fit: float,
) -> Dict[str, Any]:
    if competitor_count <= 0:
        competitor_score = 0.0
    elif competitor_count == 1:
        competitor_score = 0.2
    elif competitor_count <= 4:
        competitor_score = 0.55
    else:
        competitor_score = 0.9

    repeated_source = any(count >= 2 for count in source_counts.values())
    repeated_competitor = any(count >= 2 for count in competitor_counts.values())
    repeated_owner = repeated_source or repeated_competitor
    repeated_owner_score = 1.0 if repeated_competitor else (0.6 if repeated_source else 0.0)
    total_sources = sum(source_counts.values())
    source_concentration = (
        max(source_counts.values()) / total_sources
        if total_sources
        else 0.0
    )
    prompt_specificity_score = {
        "head": 1.0,
        "category": 0.55,
        "attribute": 0.25,
        "sidewalk": 0.1,
        "objection": 0.35,
        "branded": 0.2,
    }.get(query_class, 0.4)
    source_authority_bonus = _source_authority_bonus(source_roles)
    score = (
        (0.35 * competitor_score)
        + (0.20 * repeated_owner_score)
        + (0.15 if any_competitor_first_party else 0.0)
        + (0.12 * source_concentration)
        + (0.12 * prompt_specificity_score)
        + (0.04 * (1.0 - attribute_fit))
        + (0.02 * source_authority_bonus)
    )
    score = _clamp(score)
    if score < 0.33:
        band = "low"
    elif score < 0.60:
        band = "medium"
    else:
        band = "high"
    repeated_owner_value: Any = False
    if repeated_competitor:
        repeated_owner_value = _durable_competitor(competitor_counts)
    elif repeated_source:
        repeated_owner_value = source_counts.most_common(1)[0][0]
    return {
        "score": round(score, 4),
        "band": band,
        "features": {
            "competitor_count": competitor_count,
            "repeated_owner": repeated_owner_value,
            "first_party_competitor": any_competitor_first_party,
            "source_concentration": round(source_concentration, 4),
            "prompt_specificity": query_class,
            "merchant_fit": round(attribute_fit, 4),
        },
    }


def _source_authority_bonus(source_roles: List[Dict[str, Any]]) -> float:
    if not source_roles:
        return 0.0
    top_roles = {row.get("role") for row in source_roles[:3]}
    if "publisher" in top_roles or "marketplace" in top_roles:
        return 1.0
    if "retailer" in top_roles:
        return 0.7
    if "forum" in top_roles:
        return 0.4
    return 0.2


def _demand_signal(
    runs: List[Dict[str, Any]],
    provider_analysis: Dict[str, Dict[str, Any]],
    competitors: List[str],
) -> float:
    shopping_runs = sum(1 for row in provider_analysis.values() if row.get("shopping_answer"))
    source_count = sum(
        len(run.get("grounding_sources") or [])
        for run in runs
        if not _looks_like_no_info_denial(_run_text(run))
    )
    competitor_count = len(competitors)
    if shopping_runs <= 0:
        return 0.0
    if shopping_runs >= 2 and (source_count or competitor_count):
        return 1.0 if competitor_count >= 2 or source_count >= 3 else 0.7
    if source_count >= 2 or competitor_count >= 2:
        return 0.7
    return 0.4


def _opportunity_score(
    *,
    query_class: str,
    attribute_fit: float,
    intent_weight: float,
    demand_signal: float,
    density_score: float,
    volume_proxy: float,
    actionability: float,
    confidence: float,
    ownership_state: str,
) -> Tuple[float, Dict[str, float]]:
    candidate_lane = query_class in {"head", "category", "attribute", "sidewalk", "objection"}
    if not candidate_lane or demand_signal <= 0:
        raw = 0.0
    else:
        raw = (
            attribute_fit
            * intent_weight
            * demand_signal
            * (1.0 - density_score)
            * volume_proxy
            * actionability
            * confidence
        )
        if ownership_state == "merchant-owned":
            raw *= 0.15
        elif ownership_state == "merchant-mentioned":
            raw *= 0.65
        elif ownership_state.endswith("-owned") and ownership_state != "open-lane":
            raw *= 0.45
    return (
        round(min(100.0, max(0.0, raw * 100.0)), 2),
        {
            "attribute_fit": round(attribute_fit, 4),
            "intent_weight": round(intent_weight, 4),
            "demand_signal": round(demand_signal, 4),
            "density_inverse": round(1.0 - density_score, 4),
            "volume_proxy": round(volume_proxy, 4),
            "actionability": round(actionability, 4),
            "confidence": round(confidence, 4),
        },
    )


def _is_branded_transactional(query_class: str, axis: str, query: str) -> bool:
    """The merchant's OWN branded buyer-path lane (e.g. "where can I buy <brand>",
    "<brand> ... for sale", "shop <brand> online"). Mirrors the
    `branded_transactional` rule in `_intent_ladder_layer` so ownership and the
    intent ladder agree on what counts as a branded buy-intent lane. Review /
    comparison branded lanes are intentionally excluded (not buy-intent)."""
    if axis in {"intent", "price", "brand", "identity"}:
        return True
    if query_class == "branded" and any(
        token in _norm_query(query) for token in ("buy", "price", "shop", "where")
    ):
        return True
    return False


def _ownership_state(
    *,
    runs: List[Dict[str, Any]],
    sku_ctx: Dict[str, Any],
    provider_analysis: Dict[str, Dict[str, Any]],
    provider_verdicts: Dict[str, str],
    source_route: str,
    source_roles: List[Dict[str, Any]],
    durable_competitor: Optional[str],
    open_lane: bool,
    demand_signal: float,
    branded_transactional: bool = False,
) -> str:
    if demand_signal <= 0:
        return "no-demand"
    first_party_primary = _first_party_dominates_sources(source_roles, sku_ctx)
    first_party_positive = first_party_primary and any(
        row.get("grounded_first_party") and row.get("verdict") == "win"
        for row in provider_analysis.values()
    )
    wins = sum(1 for verdict in provider_verdicts.values() if verdict == "win")
    merchant_mentions = sum(
        1 for verdict in provider_verdicts.values()
        if verdict in _OWNED_PROVIDER_VERDICTS
    )
    if first_party_positive and wins >= 1:
        return "merchant-owned"
    # Intent-aware floor (the merchant's OWN branded buyer path). The strict
    # dominance gate above requires first-party citations to outweigh the SUM of
    # all external sources — unachievable when AI grounds an answer in a long
    # tail of 10-20 hosts, so a merchant cited on its own branded query was being
    # handed to a co-cited retailer/marketplace. On a branded-transactional lane,
    # if the merchant's own site is cited (first-party present) AND the product
    # wins, the merchant owns its branded lane even amid fragmentation. Honesty
    # guard: keep the PAIRWISE check (first-party cited >= any single external
    # host) so a retailer that genuinely out-cites the merchant on a branded
    # query still wins — and never extend this to category/sidewalk lanes, where
    # one citation among many is honestly third-party-controlled, not owned.
    if branded_transactional and wins >= 1:
        first_party, _external_total, max_external = _first_party_source_share(
            source_roles, sku_ctx,
        )
        if first_party > 0 and first_party >= max_external:
            return "merchant-owned"
    source_owner = _source_owned_state(
        source_route=source_route,
        source_roles=source_roles,
        sku_ctx=sku_ctx,
    )
    if source_owner:
        return source_owner
    if durable_competitor:
        return "competitor-owned"
    if merchant_mentions:
        return "merchant-mentioned"
    if open_lane:
        return "open-lane"
    return "open-lane"


def _is_open_lane(
    *,
    query_class: str,
    demand_signal: float,
    durable_competitor: Optional[str],
    source_route: str,
    density_score: float,
    density_features: Optional[Mapping[str, Any]] = None,
    attribute_fit: float,
    intent_weight: float,
    actionability: float,
    provider_analysis: Dict[str, Dict[str, Any]],
) -> bool:
    if query_class not in {"sidewalk", "attribute", "category", "objection"}:
        return False
    if demand_signal < 0.4:
        return False
    if demand_signal < 0.7 and _confidence(provider_analysis) < 0.8:
        return False
    if any(row.get("grounded_first_party") for row in provider_analysis.values()):
        return False
    if source_route in {"retailer", "publisher", "forum", "marketplace"}:
        return False
    if any(
        row.get("merchant_mention")
        or row.get("product_positive")
        or row.get("verdict") in _OWNED_PROVIDER_VERDICTS
        for row in provider_analysis.values()
    ):
        return False
    # WEAKLY-HELD lanes: in grounded search every query cites SOMEONE, so
    # "open = unowned" makes open lanes structurally impossible in any
    # established category (audio: Shokz is name-dropped on nearly every
    # query -> durable_competitor always set -> zero open lanes, and the
    # sideways-wedge product promise dies). A lane is still winnable when the
    # ROUTE is weak: citations fragmented across hosts, no single source
    # concentration, and no competitor grounded first-party. There, a
    # name-dropped competitor doesn't block, and the density ceiling relaxes.
    features = density_features or {}
    weakly_held = (
        source_route == "fragmented"
        and float(features.get("source_concentration") or 0.0) <= 0.4
        and not features.get("first_party_competitor")
    )
    if durable_competitor and not weakly_held:
        return False
    max_density = 0.65 if weakly_held else 0.45
    return (
        density_score <= max_density
        and attribute_fit >= 0.70
        and intent_weight >= 0.80
        and actionability >= 0.80
    )


# Non-branded category-demand classes: the brand SHOULD surface here on its own
# merit. When it's absent/loss and a rival is named, that IS substitution — "AI
# recommends <competitor> instead of you for your own category" — even though
# the query never names the brand. (Branded substitution is the merchant-named
# path below; "branded"/comparison classes route there, not here.)
_CATEGORY_DEMAND_CLASSES = {"head", "category", "attribute", "sidewalk", "objection"}


def _substitution(
    *,
    query: str,
    axis: str,
    query_class: str,
    sku_ctx: Dict[str, Any],
    product: Dict[str, Any],
    provider_analysis: Dict[str, Dict[str, Any]],
    provider_verdicts: Dict[str, str],
    competitors: List[str],
    competitor_counts: Counter,
    vertical_profile: VerticalProfile = BEAUTY_PROFILE,
) -> Dict[str, Any]:
    comparison_signal = axis in {"comparison", "alternatives"} or bool(
        _COMPARISON_TOKEN_RE.search(query or "")
    )
    merchant_named = _query_mentions_merchant(query, sku_ctx=sku_ctx, product=product)
    # A non-branded category query is an organic-demand lane the brand owns by
    # category — losing it to a named rival is substitution even with no brand
    # mention. Discovery queries (category_visibility_test) land here.
    category_demand = query_class in _CATEGORY_DEMAND_CLASSES
    product_absent_or_loss = not any(
        row.get("product_positive") or row.get("grounded_positive")
        for row in provider_analysis.values()
    ) and any(verdict in {"loss", "absent"} for verdict in provider_verdicts.values())
    if (merchant_named or category_demand) and product_absent_or_loss and competitors:
        # Substitution must name a rival BRAND. The competitor list can include
        # ingredient/category TYPES ("Shea Butter", "Castor Oil") that produce
        # nonsense like "AI names Shea Butter, not you / publish a vs Shea
        # Butter comparison". Drop those; if no real brand displaced the
        # merchant, there's no brand substitution to act on.
        from services.competitor_brand_filter import filter_competitor_brands

        brand_competitors = filter_competitor_brands(
            competitors,
            ingredient_tokens=vertical_profile.competitor_ingredient_tokens,
            form_tokens=vertical_profile.competitor_form_tokens,
        )
        if not brand_competitors:
            return {"present": False}
        # The most-recommended rival brand (durable across the answer set).
        top = max(brand_competitors, key=lambda b: competitor_counts.get(b, 0))
        engines = sorted(
            provider
            for provider, verdict in provider_verdicts.items()
            if verdict in {"loss", "absent"}
        )
        return {
            "present": True,
            "substituted_by": top,
            "prompt": query,
            "engines": engines,
            # "branded" = brand named in the query but loses; "category" = the
            # brand is displaced organically in its own category lane.
            "kind": "branded" if merchant_named else "category",
        }
    _ = comparison_signal  # Secondary signal is intentionally not the alert gate.
    return {"present": False}


def _build_sku_aggregate(
    per_prompt: List[Dict[str, Any]],
    *,
    providers: List[str],
) -> Dict[str, Any]:
    intent_ladder = _intent_ladder(per_prompt)
    top_open_lanes = _top_open_lanes(per_prompt)
    substitution_alert = _substitution_alert(per_prompt)
    return {
        "intent_ladder": intent_ladder,
        "top_open_lanes": top_open_lanes,
        "demand_state_summary": _demand_state_summary(per_prompt, intent_ladder, top_open_lanes),
        "substitution_alert": substitution_alert,
        "confidence": _coverage_confidence(per_prompt, providers=providers),
    }


def _intent_ladder(per_prompt: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    layers = {
        "branded_transactional": [],
        "branded_consideration": [],
        "head_category": [],
        "attribute_category": [],
        "sidewalk_opportunity": [],
        "objection": [],
    }
    for row in per_prompt:
        layer = _ladder_layer(row)
        if layer:
            layers[layer].append(row)

    out: Dict[str, Dict[str, Any]] = {}
    for layer, rows in layers.items():
        if layer == "sidewalk_opportunity":
            open_scores = [
                float(row.get("opportunity_score") or 0)
                for row in rows
                if row.get("open_lane")
            ]
            # Count x strength: one strong open sidewalk lane should be visible,
            # several strong lanes should approach 100.
            score = min(100, int(round(sum(open_scores) * 0.75)))
            out[layer] = {
                "score": score,
                "prompts": len(rows),
                "open_lanes": len(open_scores),
                "basis": "opportunity_score_count_x_strength",
            }
        else:
            owned = sum(
                1
                for row in rows
                if row.get("ownership_state") == "merchant-owned"
            )
            answerable = sum(1 for row in rows if row.get("demand_signal", 0) > 0)
            denominator = len(rows)
            if layer == "objection":
                numerator = owned + sum(
                    1
                    for row in rows
                    if row.get("ownership_state") == "merchant-mentioned"
                    or row.get("demand_signal", 0) >= 0.7
                )
            else:
                numerator = owned
            score = int(round((numerator / denominator) * 100)) if denominator else 0
            out[layer] = {
                "score": score,
                "prompts": denominator,
                "owned_or_answerable": numerator,
                "answered": answerable,
                "basis": "% owned/visible" if layer != "objection" else "% answerable",
            }
    return out


def _top_open_lanes(per_prompt: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    lanes = [
        row
        for row in per_prompt
        if row.get("open_lane")
        and row.get("query_class") in {"sidewalk", "attribute", "category", "objection"}
        # Drop synthetic placeholder probes ("<title> shopper question N") — they
        # are scaffolding, not real demand, and must never surface as a lane the
        # merchant is told to "own".
        and not is_synthetic_probe_query(row.get("query"))
    ]
    lanes.sort(
        key=lambda row: (
            -float(row.get("opportunity_score") or 0),
            str(row.get("query") or "").lower(),
        )
    )
    out = []
    for row in lanes[:3]:
        lane = {
            "query": row.get("query"),
            "why_fit": row.get("attribute_basis") or _why_fit_from_features(row),
            "current_ownership": row.get("ownership_state"),
            "density_band": (row.get("density") or {}).get("band"),
            "intent": row.get("intent"),
            "source_route": row.get("source_route"),
            "demand_state": row.get("demand_state"),
            "opportunity_score": row.get("opportunity_score"),
        }
        lane["first_move"] = _first_move_for_lane(lane)
        out.append(lane)
    return out


def _substitution_alert(per_prompt: List[Dict[str, Any]]) -> Dict[str, Any]:
    alerts = [
        row
        for row in per_prompt
        if isinstance(row.get("substitution"), dict)
        and row["substitution"].get("present")
    ]
    if not alerts:
        return {"present": False}
    alerts.sort(
        key=lambda row: (
            -float(row.get("demand_signal") or 0),
            -len(row.get("substitution", {}).get("engines") or []),
            str(row.get("query") or "").lower(),
        )
    )
    sub = alerts[0]["substitution"]
    return {
        "present": True,
        "prompt": sub.get("prompt") or alerts[0].get("query"),
        "substituted_by": sub.get("substituted_by"),
        "engines": sub.get("engines") or [],
        # "category" = displaced organically in its own category lane;
        # "branded" = brand named in the query but loses. Lets the UI phrase it
        # ("AI recommends X for <category>" vs "…when buyers search your name").
        "kind": sub.get("kind"),
    }


def _demand_state_summary(
    per_prompt: List[Dict[str, Any]],
    intent_ladder: Dict[str, Dict[str, Any]],
    top_open_lanes: List[Dict[str, Any]],
) -> str:
    if top_open_lanes:
        return "open lane detected"
    demand_exists_absent = any(
        row.get("demand_signal", 0) > 0
        and row.get("ownership_state") in {
            "competitor-owned",
            "retailer-owned",
            "publisher-owned",
            "forum-owned",
            "open-lane",
        }
        for row in per_prompt
    )
    branded_layer = intent_ladder.get("branded_transactional") or {}
    branded_score = branded_layer.get("score") or 0
    unbranded_layers = [
        intent_ladder.get("head_category") or {},
        intent_ladder.get("attribute_category") or {},
    ]
    measured_unbranded = [
        layer
        for layer in unbranded_layers
        if int(layer.get("prompts") or 0) > 0
    ]
    if branded_score >= 70:
        if measured_unbranded and max((layer.get("score") or 0) for layer in measured_unbranded) < 40:
            return "branded demand protected, unbranded absent"
        if not measured_unbranded:
            return "branded demand protected, unbranded not measured"
    if demand_exists_absent:
        return "demand exists but you are absent"
    return "no meaningful demand detected"


def _coverage_confidence(
    per_prompt: List[Dict[str, Any]],
    *,
    providers: List[str],
) -> Dict[str, Any]:
    prompts = len(per_prompt)
    provider_counts = Counter()
    agreement_counts = Counter()
    for row in per_prompt:
        agreement_counts[row.get("engine_agreement") or "neither"] += 1
        for provider, verdict in (row.get("provider_verdicts") or {}).items():
            if verdict != "absent":
                provider_counts[provider] += 1
    covered = sum(1 for row in per_prompt if row.get("demand_signal", 0) > 0)
    return {
        "providers": providers,
        "provider_run_counts": dict(sorted(provider_counts.items())),
        "engine_agreement_counts": dict(sorted(agreement_counts.items())),
        "prompt_count": prompts,
        "prompts_with_demand": covered,
        "coverage_summary": (
            f"{covered}/{prompts} prompts showed demand; "
            f"{agreement_counts.get('both', 0)} had both-engine SKU visibility"
        )
        if prompts
        else "0/0 prompts showed demand",
    }


def _ladder_layer(row: Dict[str, Any]) -> Optional[str]:
    axis = str(row.get("axis") or "").lower()
    query_class = row.get("query_class")
    query = str(row.get("normalized_query") or row.get("query") or "").lower()
    if query_class == "sidewalk" or axis == "sidewalk":
        return "sidewalk_opportunity"
    if axis in {"intent", "price", "brand", "identity"} or (
        query_class == "branded" and any(t in query for t in ("buy", "price", "shop", "where"))
    ):
        return "branded_transactional"
    if axis in {"review", "comparison"} or any(
        token in query
        for token in ("review", "reviews", "alternative", "alternatives", " vs ", "worth")
    ):
        return "branded_consideration"
    if axis in {"objection", "brand-objection"} or query.startswith("is "):
        return "objection"
    if query_class == "head":
        return "head_category"
    if query_class in {"attribute", "category"}:
        return "attribute_category"
    return None


def _prompt_demand_state(
    *,
    demand_signal: float,
    open_lane: bool,
    ownership_state: str,
) -> str:
    if demand_signal <= 0:
        return "no-demand"
    if open_lane:
        return "open-lane"
    if ownership_state == "merchant-owned":
        return "protected"
    if ownership_state == "merchant-mentioned":
        return "repair"
    return "contested"


def _engine_agreement(provider_verdicts: Dict[str, str]) -> str:
    positive = sum(1 for verdict in provider_verdicts.values() if verdict in _OWNED_PROVIDER_VERDICTS)
    if positive >= 2:
        return "both"
    if positive == 1:
        return "one"
    return "neither"


def _confidence(provider_analysis: Dict[str, Dict[str, Any]]) -> float:
    shopping = [row for row in provider_analysis.values() if row.get("shopping_answer")]
    if len(shopping) >= 2:
        return 1.0
    if any(row.get("grounded_positive") or row.get("competitors_named") for row in shopping):
        return 0.8
    if shopping:
        return 0.5
    return 0.0


def _query_class(query: str, axis: str) -> str:
    q = _norm_query(query)
    if axis == "sidewalk":
        return "sidewalk"
    if axis in {"objection", "brand-objection"}:
        return "objection"
    if axis in {"intent", "price", "brand", "identity", "review", "comparison"}:
        return "branded"
    if axis in {"attribute", "concern", "use_case"}:
        return "attribute"
    if axis == "category":
        modifiers = [
            "for ",
            "with ",
            "without ",
            "under ",
            "halal",
            "korean",
            "stick",
            "sticks",
            "before",
        ]
        if q.startswith(("best ", "top ", "what is the best ")) and not any(m in q for m in modifiers):
            return "head"
        return "category"
    if q.startswith(("best ", "top ", "what is the best ")):
        return "head"
    return "attribute" if len(q.split()) >= 4 else "category"


def _intent_for(query: str, axis: str, axis_metadata: Mapping[str, Any]) -> Dict[str, Any]:
    passthrough = axis_metadata.get("sidewalk_intent_weight")
    weight = _coerce_float(passthrough)
    label: str
    q = _norm_query(query)
    if weight is not None:
        label = _intent_label_from_weight(weight)
    elif any(token in q for token in ("buy", "shop", "price", "for sale", "where can i buy", "discount")):
        weight = 1.2
        label = "transactional"
    elif q.startswith(("best ", "top ", "what is the best ")) or axis == "category":
        weight = 1.0
        label = "category-purchase"
    elif (
        axis in {"review", "comparison"}
        or "review" in q
        or "worth" in q
        or _COMPARISON_TOKEN_RE.search(q)
    ):
        weight = 0.8
        label = "consideration"
    elif axis in {"objection", "brand-objection"}:
        weight = 0.8
        label = "consideration"
    elif axis == "sidewalk":
        weight = 1.0
        label = "category-purchase"
    else:
        weight = 0.6
        label = "education"
    return {"label": label, "weight": round(float(weight), 4)}


def _intent_label_from_weight(weight: float) -> str:
    if weight >= 1.15:
        return "transactional"
    if weight >= 0.95:
        return "category-purchase"
    if weight >= 0.75:
        return "consideration"
    return "education"


def _attribute_fit(
    query: str,
    axis_metadata: Mapping[str, Any],
    attribute_graph: Dict[str, Any],
    product: Dict[str, Any],
) -> float:
    q = _norm_query(query)
    basis = _clean_basis(axis_metadata.get("sidewalk_attribute_basis"))
    if basis:
        basis_hits = sum(1 for attr in basis if _attr_in_query(attr, q))
        if basis_hits == len(basis):
            return 1.0
        return round(min(1.0, 0.75 + (0.25 * basis_hits / max(1, len(basis)))), 4)

    attrs = _flatten_attributes(attribute_graph)
    hits = sum(1 for attr in attrs if _attr_in_query(attr, q))
    score = min(0.85, hits * 0.18)
    product_type = _norm_query(product.get("product_type") or product.get("category"))
    if product_type and any(word in q for word in product_type.split() if len(word) >= 4):
        score = max(score, 0.45)
    if _text_mentions_any(
        query,
        [
            product.get("title"),
            product.get("raw_title"),
            product.get("brand"),
            product.get("vendor"),
        ],
    ):
        score = max(score, 0.9)
    return round(_clamp(score), 4)


def _flatten_attributes(attribute_graph: Dict[str, Any]) -> List[str]:
    classes = attribute_graph.get("classes") if isinstance(attribute_graph.get("classes"), dict) else {}
    out: List[str] = []
    for values in classes.values():
        if isinstance(values, list):
            out.extend(str(value).strip().lower() for value in values if str(value).strip())
    return sorted(set(out), key=lambda value: (-len(value), value))


def _volume_proxy(
    *,
    query_class: str,
    source_roles: List[Dict[str, Any]],
    competitor_count: int,
) -> float:
    base = {
        "head": 1.2,
        "category": 1.0,
        "attribute": 0.8,
        "sidewalk": 0.8,
        "objection": 0.7,
        "branded": 0.6,
    }.get(query_class, 0.7)
    if competitor_count >= 5:
        base = max(base, 1.0)
    if len(source_roles) >= 3 and query_class != "sidewalk":
        base = min(1.2, base + 0.1)
    return round(base, 4)


def _actionability(
    *,
    attribute_fit: float,
    density_score: float,
    source_route: str,
    demand_signal: float,
) -> float:
    if demand_signal <= 0:
        return 0.0
    if attribute_fit >= 0.75 and density_score <= 0.45:
        return 1.0
    if source_route in {"retailer", "marketplace", "publisher"}:
        return 0.5 if density_score >= 0.6 else 0.8
    if source_route == "forum":
        return 0.7
    return 0.8


def _durable_competitor(competitor_counts: Counter) -> Optional[str]:
    if not competitor_counts:
        return None
    name, count = sorted(
        competitor_counts.items(),
        key=lambda item: (-item[1], item[0].lower()),
    )[0]
    return name if count >= 2 else None


def _looks_like_shopping_answer(text: str) -> bool:
    haystack = str(text or "").strip().lower()
    if not haystack:
        return False
    return any(word in haystack for word in _SHOPPING_WORDS)


def _looks_like_no_info_denial(text: str) -> bool:
    return bool(_NO_INFO_DENIAL_RE.search(str(text or "")))


def _run_has_retail_marketplace_route(run: Dict[str, Any], product: Dict[str, Any]) -> bool:
    merchant_category = product.get("product_type") or product.get("category")
    for identifier in _run_source_identifiers(run):
        host = identifier.get("host")
        if not host:
            continue
        role = _normalize_role(
            classify_host(str(host), merchant_category=merchant_category).get("type"),
            str(host),
        )
        if role in {"retailer", "marketplace"}:
            return True
    return False


def _mentions_product_category(text: str, product: Dict[str, Any]) -> bool:
    haystack = str(text or "").strip().lower()
    if not haystack:
        return False
    candidates: List[str] = []
    for value in (
        product.get("product_type"),
        product.get("category"),
        product.get("title"),
        product.get("raw_title"),
        product.get("description"),
    ):
        candidates.extend(
            token
            for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if len(token) >= 5
        )
    return any(token in haystack for token in set(candidates))


def _merchant_host(sku_ctx: Dict[str, Any], product: Dict[str, Any]) -> Optional[str]:
    # Shares the candidate list with _is_first_party_host so the primary host and
    # the first-party membership set cannot drift. Canonical fields rank first
    # (onboarded path unchanged); pdp_url/url cover the cold-start URL audit.
    for candidate in _first_party_url_candidates(sku_ctx, product):
        normalized = normalize_host(candidate or "")
        if normalized:
            return normalized
    return None


def _merchant_identity_values(sku_ctx: Dict[str, Any], product: Dict[str, Any]) -> Tuple[str, ...]:
    sku = sku_ctx.get("sku") if isinstance(sku_ctx.get("sku"), dict) else {}
    values = (
        product.get("brand"),
        product.get("vendor"),
        sku_ctx.get("merchant_brand"),
        sku_ctx.get("merchant_name"),
        product.get("storefront_name"),
        product.get("parent_brand"),
        sku_ctx.get("storefront_name"),
        sku_ctx.get("parent_brand"),
        sku.get("brand"),
        sku.get("vendor"),
    )
    out: List[str] = []
    seen = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value or "").strip())
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return tuple(out)


def _query_mentions_merchant(
    query: str,
    *,
    sku_ctx: Dict[str, Any],
    product: Dict[str, Any],
) -> bool:
    sku = sku_ctx.get("sku") if isinstance(sku_ctx.get("sku"), dict) else {}
    values: List[Any] = [
        product.get("title"),
        product.get("raw_title"),
        product.get("brand"),
        product.get("vendor"),
        sku_ctx.get("merchant_brand"),
        sku_ctx.get("merchant_name"),
        sku_ctx.get("sku_title"),
        sku.get("sku"),
        sku.get("barcode"),
        sku.get("gtin"),
    ]
    if _text_mentions_any(query, values):
        return True

    merchant_host = _merchant_host(sku_ctx, product)
    primary = (
        product.get("brand")
        or product.get("vendor")
        or sku_ctx.get("merchant_brand")
        or sku_ctx.get("merchant_name")
    )
    return text_mentions_brand(
        _norm_query(query),
        derive_brand_aliases(str(primary or ""), merchant_host, _merchant_identity_values(sku_ctx, product)),
    )


def _merged_axis_metadata(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for run in runs:
        meta = run.get("axis_metadata") if isinstance(run.get("axis_metadata"), dict) else {}
        for key, value in meta.items():
            if key not in merged and value not in (None, "", []):
                merged[key] = value
    return merged


def _display_query(runs: List[Dict[str, Any]], fallback: str) -> str:
    for run in runs:
        query = str(run.get("query") or "").strip()
        if query:
            return query
    return fallback


def _clean_basis(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = re.split(r"[,/+|]", str(value))
    out = []
    seen = set()
    for raw in values:
        cleaned = str(raw or "").strip().lower()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _attr_in_query(attr: str, query_norm: str) -> bool:
    attr_norm = _norm_query(attr)
    if not attr_norm:
        return False
    if attr_norm in query_norm:
        return True
    words = [word for word in attr_norm.split() if len(word) >= 4]
    return bool(words and all(word in query_norm for word in words))


def _clean_competitor_name(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:80]


def _same_brand(candidate: str, merchant_brand: str) -> bool:
    c = _norm_query(candidate)
    b = _norm_query(merchant_brand)
    if not c or not b:
        return False
    return c == b or c in b or b in c


def _is_merchant_self(
    candidate: str,
    *,
    merchant_brand: str = "",
    merchant_vendors: Tuple[str, ...] = (),
    merchant_title: str = "",
) -> bool:
    """True if an extracted "competitor" is actually the merchant's own product
    or brand. Guards against the merchant's own product line ("Good Night
    Collagen ...") being mined as a competitor and then surfaced as `who_owns`.
    Checks brand + every merchant identity value, plus a high-overlap match
    against the merchant's own product title (its distinctive tokens contained in
    the candidate)."""
    if _same_brand(candidate, merchant_brand):
        return True
    for vendor in merchant_vendors or ():
        if vendor and _same_brand(candidate, vendor):
            return True
    name_tokens = set(_norm_query(candidate).split())
    title_tokens = {tok for tok in _norm_query(merchant_title).split() if len(tok) > 2}
    if len(title_tokens) >= 2 and len(title_tokens & name_tokens) / len(title_tokens) >= 0.8:
        return True
    return False


def _why_fit_from_features(row: Dict[str, Any]) -> List[str]:
    basis = row.get("attribute_basis") or []
    if basis:
        return basis
    query = str(row.get("normalized_query") or row.get("query") or "")
    return [query]


def _norm_query(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _get_product(sku_ctx: Dict[str, Any]) -> Dict[str, Any]:
    product = sku_ctx.get("product") if isinstance(sku_ctx, dict) else None
    return product if isinstance(product, dict) else (sku_ctx or {})


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))
