"""
BD external-merchant AI visibility report — shared service module.

This module owns the pure analysis + report-rendering logic that both
`scripts/agent_center_bd_external_merchant.py` (CLI) and the new
`/api/agent-center/bd/external-merchant-report` HTTP route consume.

Why factor this out:
  - The CLI was the first surface; the BD UI in employee-portal is the
    second. Keeping the verdict thresholds, competitor extraction, and
    report shape in one place avoids drift between the two.
  - The HTTP route returns structured JSON for the UI to render, while
    the CLI renders markdown. Both formats need the same underlying
    analysis — so analysis is here, formatting (markdown vs JSON-for-UI)
    is here too.
  - Tests stay in one place: `tests/test_agent_center_bd_external_merchant.py`
    already covers the analysis functions; route tests just exercise the
    HTTP wrapper.

Most report projection remains pure over `llm_client.probe` results passed in
by the caller. The re-audit delta layer has one best-effort DB read for the
full prior audit report when the caller provides persisted history context.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import asyncio
import copy
import json
import logging
import math
import re
from typing import AbstractSet, Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from services import agent_center_llm_client as llm_client
from services.audit_delta import build_reaudit_delta, measurement_basis_between
from services.outreach_outcomes import build_outreach_outcomes
from services.prompt_basis import basis_meta_from_probe_runs, PROMPT_BASIS_VERSION
from services.audit_facts import (
    QUERY_CLASS_BRANDED,
    QUERY_CLASS_CATEGORY,
    _VERTEX_REDIRECTOR_HOSTS,
    _clean_identity_tuple,
    _grounding_source_host,
    _identify_run_sources,
    _looks_like_host,
    _source_matches_merchant,
    aggregate_run_facts,
    compute_run_facts,
    normalize_host,
    own_url_cited_runs_any,
    parity_check,
    parity_measure,
    query_class_for_axis as _query_class_for_axis,
    run_query_class as _run_query_class,
)
from services.audit_playbook_engine import select_playbooks
from services.brand_alias import (
    _registrable_name_from_host,
    derive_brand_aliases,
    text_mentions_brand,
)
from services.competitor_brand_filter import (
    filter_competitor_brands,
    is_ingredient_or_category_type,
)
# Deep-tier blocks (spec 2026-07-21). The source stamp is what carries a deep
# record's axis into axis_metadata — without it, a comparison-axis record
# would reach the report with no axis and dodge the internal-first filters.
from services.deep_tier_prompts import (
    COMPARISON_AXIS as _COMPARISON_AXIS,
    run_is_internal_comparison,
    DEEP_TIER_PROMPT_SOURCE as _DEEP_TIER_PROMPT_SOURCE,
)
from services.buyer_path_stable_controllers import (
    stable_buyer_path_controller_hosts,
    stable_buyer_path_controllers_for_row,
)
from services.buyer_path_controller_quality import (
    controller_profile as build_controller_profile,
    aggregate_controller_profile,
    is_canonical_source_vacuum,
)
from services.cited_host_classifier import (
    classify_cited_hosts,
    classify_host,
    is_channel_role,
    is_endorsement_role,
    is_findability_role,
    is_profile_retailer_name,
    merchant_relative_role,
    recommendation_class,
    ROLE_COMPETITOR,
    ROLE_CREATOR,
    ROLE_EDITORIAL_REVIEW,
    ROLE_FORUM,
    ROLE_INDEPENDENT_RETAILER,
    ROLE_MARKETPLACE_SELF_LISTING,
    ROLE_OWN_DOMAIN,
    ROLE_RELATIVE_UNCLASSIFIED,
)
from services.merchant_narrative_builder import build_merchant_narrative
from services.win_plan_builder import build_win_plan, is_broad_head_query
from services.coverage_profiles import (
    resolve_coverage_profile,
    resolve_provider_models,
    verify_supported_providers,
)
from services.commerce_execution_policy import (
    SURFACE_PUBLIC_AGENT_PURCHASE,
    resolve_commerce_execution_policy,
)
from services.checkout_handoff_descriptor import build_checkout_handoff_descriptor
from services.deliverability_report_view import build_deliverability_render_view
from services.next_best_action import (
    attach_sku_strategic_brief,
    build_next_best_action,
    build_sku_next_best_action,
)
from services.pivota_indexing_arc import compute_indexing_arc_state
from services.sku_lane_priority import (
    build_sideways_wedge,
    enrich_lane_priority,
    has_lane_demand,
    is_third_party_controlled_lane,
    lane_priority_sort_key,
)
from services.promo_terms import is_promo_term
from services.sku_sidewalk import (
    build_sku_attribute_graph,
    generate_sidewalk_query_specs,
)
from services.vertical_profiles import (
    BEAUTY_PROFILE,
    VerticalProfile,
    get_profile,
    resolve_profile,
    resolve_profile_for_vertical,
    resolve_vertical,
)
from services.llm_attribute_extractor import (
    GroundedAttribute,
    build_source_text,
    deserialize_grounded,
    extract_attributes,
    merge_grounded_into_graph,
    serialize_grounded,
    should_run_extractor,
    source_fingerprint,
)


_ANSWER_QUALITY_VERIFY_SCAN_MODE = "answer_quality_verify"
_ANSWER_QUALITY_VERIFY_PROVIDER = "deepseek"
# Verify probes run concurrently (asyncio.gather) against a slow/flaky DeepSeek,
# so the per-call timeout is tighter than the 30s generation default — a stuck
# verify call must not drag out (or appear to hang) the audit.
_VERIFY_PROBE_TIMEOUT_S = 12.0
_PER_SKU_AUDIT_PROBE_SCAN_MODE = "open_product_visibility_test"
# Per-SKU probes route the scan mode by query INTENT so each query is measured
# HONESTLY. A single product-centric mode over-reports discovery: a "best X"
# query run under open_product_visibility_test (which names the product in
# context) just retrieves the named product's own listing and reads as a
# category "win" — that's FINDABILITY, not "the brand wins the open category".
#   - BRANDED (navigational/trust) -> open_product_visibility_test
#       FINDABILITY: is the NAMED product / own page retrievable when asked for.
#   - DISCOVERY (category_head/problem_jtbd/constraint) + merchant custom
#       prompts -> category_visibility_test
#       ORGANIC: the query does NOT name the brand and the product is NOT the
#       supplied answer target; the node scores a GROUNDED brand-hit (does the
#       brand surface on its own merit), so "win" == appears organically.
_PER_SKU_BRANDED_SCAN_MODE = _PER_SKU_AUDIT_PROBE_SCAN_MODE
_PER_SKU_DISCOVERY_SCAN_MODE = "category_visibility_test"
# PIVOTA-Agent caps one probe request at 8 runs. We deliberately chunk smaller
# than that cap: each chunk is one grounded LLM call, and an 8-grounded-query
# call runs right at the agent_center_llm_probe_timeout_s (30s) edge — so fat
# chunks were the dominant source of per-SKU ReadTimeouts. Smaller chunks make
# each call faster (well under the timeout) and make a single timeout cost less.
_PER_SKU_AUDIT_UPSTREAM_CHUNK_SIZE = 4
# A single transient chunk failure (e.g. a Gemini ReadTimeout) must NOT zero a
# SKU — we continue to the next chunk so later chunks still produce evidence.
# But bail this (sku, provider) after this many CONSECUTIVE failures so a
# genuinely down/slow provider doesn't grind through every remaining chunk at
# the full per-call timeout (which is what stalled prior runs).
_PER_SKU_AUDIT_MAX_CONSECUTIVE_CHUNK_FAILURES = 2
# A single chunk failure is usually a transient ReadTimeout or a provider
# rate-limit blip (esp. Gemini, which sheds under its global gate) — not a real
# outage. Retry the chunk ONCE, after a short backoff, before counting it lost,
# so the planned prompt budget actually lands instead of silently dropping ~4
# prompts on one hiccup. The consecutive-failure bail above still protects
# against a genuinely-down provider (a retried-then-failed chunk still counts as
# one consecutive failure).
_PER_SKU_AUDIT_CHUNK_RETRIES = 1
_PER_SKU_AUDIT_CHUNK_RETRY_BACKOFF_S = 0.75
_EXPLICIT_AVAILABLE_STATES = {"in_stock", "available"}
_COMPETITOR_ATTRIBUTE_GROUNDED_PROVIDERS = {"gemini", "chatgpt"}
# Sentinel attribute key for the category-agnostic verbatim "what {competitor}
# is known for" capture (vs the typed alias attributes). Routed to the
# competitor_attributes.known_for summary, kept OUT of attributes_present so it
# never pollutes the brief's typed competitor-attribute words.
_COMPETITOR_KNOWN_FOR_KEY = "__known_for__"
_COMPETITOR_ATTRIBUTE_ALIASES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("halal", ("halal",)),
    ("kosher", ("kosher",)),
    ("vegan", ("vegan",)),
    ("organic", ("organic",)),
    ("non-gmo", ("non gmo", "non-gmo")),
    ("gluten-free", ("gluten free", "gluten-free")),
    ("cruelty-free", ("cruelty free", "cruelty-free")),
    ("collagen peptides", ("collagen peptides", "collagen peptide")),
    ("hydrolyzed collagen", ("hydrolyzed collagen", "hydrolysed collagen")),
    ("low-molecular collagen", ("low molecular collagen", "low-molecular collagen")),
    ("marine collagen", ("marine collagen",)),
    ("fish collagen", ("fish collagen",)),
    ("bovine collagen", ("bovine collagen",)),
    ("grass-fed collagen", ("grass fed collagen", "grass-fed collagen")),
    ("pasture-raised collagen", ("pasture raised collagen", "pasture-raised collagen")),
    ("vitamin c", ("vitamin c", "vitamin-c")),
    ("hyaluronic acid", ("hyaluronic acid",)),
    ("biotin", ("biotin",)),
    ("glycine", ("glycine",)),
    ("powder", ("powder", "powders")),
    ("capsules", ("capsule", "capsules")),
    ("gummies", ("gummy", "gummies")),
    ("sticks", ("stick", "sticks")),
    ("liquid", ("liquid", "liquids")),
    ("jelly", ("jelly", "jellies")),
    ("sachets", ("sachet", "sachets")),
    ("skin", ("skin", "skin health", "skin elasticity")),
    ("hair and nails", ("hair and nails", "hair & nails", "hair/nails")),
    ("k-beauty", ("k beauty", "k-beauty")),
    ("sports nutrition", ("sports nutrition",)),
    ("beauty-from-within", ("beauty from within", "beauty-from-within")),
)
logger = logging.getLogger(__name__)
_ANSWER_QUALITY_VERIFY_DEWEIGHT_RULE = (
    "answer_quality_rate is scored ONLY over verified prompts: of the "
    "citation-positive answers DeepSeek actually checked, the fraction that "
    "held up (not misstates_facts; the editorial supports_recommendation "
    "axis is informational and does not flag or de-weight). "
    "Flagged verified prompts contribute 0; UNVERIFIED prompts are excluded "
    "from both numerator and denominator (so a tier that runs no verify — "
    "e.g. the free URL-wedge — scores 0 here rather than earning unchecked "
    "answer-quality points). Deterministic first_party, sku_mention, and "
    "authority buckets are unchanged."
)
_SOURCE_ROLE_COMPETITOR_TYPES = {
    "cdn",
    "community",
    "editorial",
    "forum",
    "marketplace",
    "publisher",
    "reddit",
    "retailer",
    "social",
    "video",
}
_GENERIC_COMPETITOR_PHRASES = {
    "n/a",
    "na",
    "none",
    "no durable owner",
    "no owner",
    "no clear owner",
    "no single owner",
    "various",
    "various brands",
    "several",
    "several brands",
    "multiple brands",
    "many brands",
    "unknown",
    "not available",
}


# ---------------------------------------------------------------------------
# Probe orchestration — both CLI and route call this so the BD test
# definition stays consistent.
# ---------------------------------------------------------------------------


def _bd_synthetic_ids(merchant_name: str) -> Dict[str, str]:
    safe = "".join(c if c.isalnum() else "_" for c in merchant_name.lower())[:32] or "unknown"
    merchant_id = f"external_bd_{safe}"
    store_id = f"{merchant_id}_lead"
    return {"merchant_id": merchant_id, "store_id": store_id}


async def run_bd_probes(
    *,
    merchant_name: str,
    merchant_pdp_url: str,
    product_title: str,
    product_vendor: Optional[str] = None,
    product_type: Optional[str] = None,
    provider: str = "gemini",
    max_runs: int = 3,
    model: Optional[str] = None,
    model_is_override: bool = False,
    include_category_visibility: bool = True,
    parallel_scan_modes: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Run the BD-relevant scan modes against the merchant's product.

    Returns a `{visibility, attribution, category_visibility?}` dict;
    each value is the raw probe result with `scores`, `findings`,
    `raw_runs`, `usage`, etc.

    `category_visibility` is included by default (Phase 2a) — it asks
    Gemini open category queries that DO NOT name the product, and
    checks if the merchant brand/URL appears in grounded sources. This
    is the honest BD-pitch baseline; the product-named visibility test
    is a tautology when the prompt mentions the product. Setting
    `include_category_visibility=False` skips the third probe (saves
    ~50% Gemini cost; useful for iteration).

    Conservative defaults: max_runs=3 per scan_mode. With three modes
    on, that's ~9 grounded Gemini calls = ~225k tokens / report. Bump
    max_runs only after worker-pool isolation lands upstream (see
    feedback_llm_call_multipliers.md / incident #280)."""
    if not merchant_name or not merchant_name.strip():
        raise ValueError("merchant_name is required")
    if not merchant_pdp_url or not merchant_pdp_url.strip():
        raise ValueError("merchant_pdp_url is required")
    if not product_title or not product_title.strip():
        raise ValueError("product_title is required")

    base_context: Dict[str, Any] = {
        "queries": [],
        "product": {
            "title": product_title.strip(),
            "vendor": (product_vendor or "").strip() or None,
            "product_type": (product_type or "").strip() or None,
        },
        "merchant_pdp_url": merchant_pdp_url.strip(),
    }
    ids = _bd_synthetic_ids(merchant_name.strip())

    import os as _os

    async def _one(scan_mode: str) -> Dict[str, Any]:
        scan_target_id = f"bd-{scan_mode}-{ids['merchant_id']}-{_os.urandom(3).hex()}"
        return await llm_client.probe(
            scan_mode=scan_mode,
            scan_target_id=scan_target_id,
            merchant_id=ids["merchant_id"],
            store_id=ids["store_id"],
            context=base_context,
            provider=provider,
            max_runs=max_runs,
            model=model,
            model_is_override=model_is_override,
        )

    # The scan modes are independent. Sequential by default; the wedge opts
    # into parallel_scan_modes so its free audit's ~3 grounded HTTP calls per
    # product overlap instead of serializing — the dominant per-product cost.
    # Total in-flight stays bounded by the caller's product_concurrency (#280).
    # Skip category if product_type is missing — buildCategoryQueries upstream
    # returns [] there and the probe falls back to product_entity_id, making
    # the category test meaningless.
    can_run_category = bool(base_context["product"].get("product_type"))
    scan_modes = [
        "open_product_visibility_test", "merchant_store_attribution_test",
    ]
    if include_category_visibility and can_run_category:
        scan_modes.append("category_visibility_test")
    if parallel_scan_modes:
        results = await asyncio.gather(*[_one(m) for m in scan_modes])
        by_mode = dict(zip(scan_modes, results))
    else:
        by_mode = {mode: await _one(mode) for mode in scan_modes}
    out: Dict[str, Dict[str, Any]] = {
        "visibility": by_mode["open_product_visibility_test"],
        "attribution": by_mode["merchant_store_attribution_test"],
    }
    if "category_visibility_test" in by_mode:
        out["category_visibility"] = by_mode["category_visibility_test"]
    return out


# ---------------------------------------------------------------------------
# Pure analysis: extract cited URLs, group by host, rank by frequency.
# Caller passes raw_runs (from probe result) + the merchant's verified host.
# ---------------------------------------------------------------------------


# normalize_host / _VERTEX_REDIRECTOR_HOSTS / _identify_run_sources /
# _grounding_source_host / _looks_like_host / _source_matches_merchant /
# _clean_identity_tuple / _own_url_cited_runs MOVED to
# services/audit_facts.py (W1 RunFacts phase 1) and re-imported at the top
# of this module — audit_facts must never import this module back.
# W1 site 3 CUTOVER (T1): _own_url_cited_runs is no longer imported here — the
# report path reads own-page citedness solely from run_facts.own_url_cited_runs
# (parity-proven ==, see test_audit_facts.test_parity_with_legacy_implementations).
# The legacy fn survives in audit_facts as the test-only equivalence oracle.

def _vendor_is_merchant(vendor: Any, merchant_own_aliases: frozenset) -> bool:
    """Does a product's vendor/brand refer to the MERCHANT itself (a D2C brand
    selling its own products) vs a third-party brand the merchant RESELLS? True
    when the vendor's brand-forms overlap the merchant's own identity (brand +
    store domain)."""
    if not vendor:
        return False
    return bool(frozenset(derive_brand_aliases(str(vendor))) & merchant_own_aliases)


OPERATING_MODE_STORE_LESS = "store_less"


def _audit_merchant_vendors(
    merchant_name: Optional[str],
    merchant_host: Optional[str],
    product_vendors: List[Any],
    operating_mode: Optional[str] = None,
) -> Tuple[Tuple[str, ...], bool]:
    """Retailer-aware merchant identity (R1). Fold a product's vendor into the
    merchant's identity ONLY when that vendor IS the merchant (a D2C brand selling
    its own products). For a RETAILER/reseller, the brands it carries (e.g.
    NUTRIONE, Ownist) are NOT folded in — so their domains (ownist.com) are not
    mis-credited as the STORE's own findability. The old behavior folded EVERY
    vendor, conflating resold brands with the store.

    Returns (identity_tuple, is_reseller). is_reseller = the merchant carries ≥1
    product whose brand isn't the merchant — derived from the catalog (no schema).

    `operating_mode` is the durable account-level signal from
    merchant_onboarding: a `store_less` signup IS a brand by definition (it has no
    retail storefront), so it is NEVER classified as a reseller regardless of its
    catalog vendor mix. Without this, a store-less brand whose demo catalog carries
    foreign-looking vendor names re-derived as `reseller` on every fresh audit
    (there was no account-level field to anchor it). `storefront` / None leave the
    catalog-derived behavior byte-identical.
    """
    merchant_own = frozenset(derive_brand_aliases(merchant_name, merchant_host))
    folded: List[Any] = [merchant_name]
    saw_foreign_brand = False
    for v in product_vendors or ():
        if not v:
            continue
        if _vendor_is_merchant(v, merchant_own):
            folded.append(v)
        else:
            saw_foreign_brand = True
    if str(operating_mode or "").strip().lower() == OPERATING_MODE_STORE_LESS:
        # A store-less account is a brand by definition — never a reseller.
        saw_foreign_brand = False
    return _merchant_identity_tuple(*folded), saw_foreign_brand


def _merchant_identity_tuple(*values: Any) -> Tuple[str, ...]:
    expanded: List[str] = []
    for value in values:
        if isinstance(value, dict):
            for key in (
                "merchant_name",
                "merchant_brand",
                "brand",
                "brand_name",
                "storefront_name",
                "parent_brand",
                "vendor",
                "product_vendor",
            ):
                item = value.get(key)
                if item:
                    expanded.append(str(item))
            continue
        if isinstance(value, (list, tuple, set)):
            expanded.extend(str(item) for item in value if item)
            continue
        if value:
            expanded.append(str(value))
    return _clean_identity_tuple(tuple(expanded))


def extract_cited_hosts(
    raw_runs: List[Dict[str, Any]],
    *,
    merchant_host: Optional[str],
    merchant_brand: Optional[str] = None,
    merchant_vendors: Optional[Tuple[str, ...]] = None,
) -> Tuple[Counter, int, int]:
    """Walk every run's grounding sources and return:
      - Counter of {competitor_host: occurrences} — keyed by the resolved
        publisher DOMAIN ("sephora.com", "oliveyoung.com"). Critically NOT the
        source title: Gemini titles happen to be bare domains, but OpenAI
        web_search titles are page headlines ("The 15 Best Hair Butters … |
        Marie Claire"), which would otherwise leak into top_cited_hosts as
        fake hosts. Sources whose host can't be resolved are skipped.
      - count of runs that cited the merchant
      - count of runs that cited at least one source

    Within-run dedup: if Gemini cites Sephora 3x in one answer, that
    counts as 1 for Sephora — host frequency across runs, not raw
    chunk counts.
    """
    competitors: Counter = Counter()
    merchant_cited_runs = 0
    runs_with_any_citation = 0
    for run in raw_runs or []:
        sources = _identify_run_sources(run)
        if not sources:
            continue
        runs_with_any_citation += 1
        merchant_in_run = False
        run_competitor_hosts = set()
        for src in sources:
            if _source_matches_merchant(
                src,
                merchant_host=merchant_host,
                merchant_brand=merchant_brand,
                merchant_vendors=merchant_vendors,
            ):
                merchant_in_run = True
            else:
                # Roll up by the resolved domain, not the display title.
                cited_host = (src.get("host") or "").strip()
                if cited_host:
                    run_competitor_hosts.add(cited_host)
        if merchant_in_run:
            merchant_cited_runs += 1
        for host in run_competitor_hosts:
            competitors[host] += 1
    return competitors, merchant_cited_runs, runs_with_any_citation


VERDICT_INVISIBLE = "INVISIBLE"
VERDICT_MISATTRIBUTED = "VISIBLE BUT MISATTRIBUTED"
VERDICT_VIA_RETAILERS = "VISIBLE VIA RETAILERS"
VERDICT_CATEGORY_MENTION_NO_FIRST_PARTY = "CATEGORY MENTION, NO FIRST-PARTY"
VERDICT_STRONG = "STRONG"
VERDICT_PARTIAL = "PARTIAL"


_VERDICT_DISPLAY_LABELS = {
    # Client-facing labels — softer wording that scopes the verdict to
    # what the audit actually measures (Layer 1 grounded LLM citation),
    # rather than reading as a damning summary verdict on the brand.
    # The raw all-caps enum is preserved on the same payload for code
    # that branches on the verdict (tests, downstream rules); this is
    # purely the rendering string.
    VERDICT_INVISIBLE: "Invisible in grounded LLM citations",
    VERDICT_MISATTRIBUTED: "Visible but misattributed",
    VERDICT_VIA_RETAILERS: "Visible via retailers + editorial",
    VERDICT_CATEGORY_MENTION_NO_FIRST_PARTY: (
        "Category-visible, no first-party attribution"
    ),
    VERDICT_STRONG: "Strong AI-channel attribution",
    VERDICT_PARTIAL: "Partial AI-channel attribution",
}

# When the tier is INVISIBLE but the merchant WAS cited on a few prompts, the
# flat "Invisible..." header contradicts the body ("cited in N of M queries").
# Both scores are still below the invisible threshold — the merchant isn't
# strongly visible — but "Invisible" is a factual overstatement atop a nonzero
# count. This softer header stays honest without the self-contradiction. The raw
# INVISIBLE enum is unchanged; this is purely the rendering string.
_VERDICT_INVISIBLE_WITH_CITATIONS_LABEL = "Rarely cited in grounded LLM answers"


def _verdict_display_label(label: str, cited_runs: Optional[int] = None) -> str:
    """Merchant-facing rendering string for a verdict enum.

    `cited_runs` (own-brand citations on buyer-intent prompts) softens ONLY the
    INVISIBLE case: a nonzero count means the header would otherwise contradict
    the "cited in N of M" body. Omit it (default) for a plain enum→label map.
    """
    if (
        label == VERDICT_INVISIBLE
        and cited_runs is not None
        and cited_runs > 0
    ):
        return _VERDICT_INVISIBLE_WITH_CITATIONS_LABEL
    return _VERDICT_DISPLAY_LABELS.get(label, label)


_RETAIL_CITED_HOST_TYPES = {"retailer", "marketplace"}


def _cited_host_type(entry: Dict[str, Any]) -> str:
    host_type = entry.get("type")
    if not host_type and entry.get("host"):
        host_type = classify_host(entry.get("host")).get("type")
    return (host_type or "unclassified").strip().lower()


def _is_retail_cited_host(entry: Dict[str, Any]) -> bool:
    return _cited_host_type(entry) in _RETAIL_CITED_HOST_TYPES


def _is_cdn_cited_host(entry: Dict[str, Any]) -> bool:
    return _cited_host_type(entry) == "cdn"


def _copyworthy_cited_hosts(
    hosts: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    return [h for h in (hosts or []) if h.get("host") and not _is_cdn_cited_host(h)]


def _retail_cited_hosts(
    hosts: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    return [h for h in _copyworthy_cited_hosts(hosts) if _is_retail_cited_host(h)]


def _cited_host_type_label(host_type: Optional[str]) -> str:
    t = (host_type or "unclassified").strip().lower()
    if t == "editorial":
        return "publisher"
    if t == "cdn":
        return "cdn"
    if t == "brand":
        return "brand site"
    if t in _RETAIL_CITED_HOST_TYPES:
        return t
    return "cited host"


def _cited_host_group_label(hosts: Optional[List[Dict[str, Any]]]) -> str:
    typed = _copyworthy_cited_hosts(hosts)
    if typed and all(_is_retail_cited_host(h) for h in typed):
        return "third-party retailers"
    if typed and all(_cited_host_type(h) == "editorial" for h in typed):
        return "publishers"
    if typed and all(_cited_host_type(h) == "brand" for h in typed):
        return "brand sites"
    if typed and all(_cited_host_type(h) == "unclassified" for h in typed):
        return "cited hosts"
    return "third-party sources"


def score_category_visibility(
    runs: List[Dict[str, Any]],
    *,
    merchant_host: Optional[str],
    merchant_brand: Optional[str],
    merchant_vendors: Optional[Tuple[str, ...]] = None,
) -> Tuple[int, List[Dict[str, Any]]]:
    """Re-score category-visibility runs from raw probe data.

    Upstream (`agentCenterLlmProbe.js`) only credits a run when the
    merchant's verified URL appears in grounding chunks — so a brand
    that's surfaced via Sephora / Olive Young / retailer reviews
    scores 0/100 even when Gemini quotes the brand name verbatim. For
    BD pitch this is the wrong reading: the brand IS discoverable in
    the AI channel, just attributed to retailers. We re-score here so
    the category score reflects "did the brand surface at all (via
    any path) for this category query?"

    A run counts as `matched=True` (full credit) when ANY of:
      - merchant URL appears in grounding chunks (`url_match.in_grounding`)
      - merchant brand or host appears in any grounding source title
        ("Beauty of Joseon Official Store" → matches brand "Beauty of Joseon")
      - **excerpt-corroborated path** (post-Grüns fix): merchant brand
        appears in `evidence_excerpt` AND Gemini self-reported
        `brand_appears: true` (`url_match.llm_self_report`) AND there's
        at least one grounding source on the run. Defends against the
        editorial-citation case (Forbes lists 10 brands, Gemini quotes
        Grüns; brand never appears in the redirector grounding URL or
        the bare-hostname title — but all three signals agree).

    The hallucination defense from the prior implementation:
      - Excerpt-match alone (without llm_self_report) STILL doesn't
        credit a run — guards against Gemini paraphrasing a no-name
        brand into the excerpt without genuine grounding. The 1688
        no-name-brand test case (test_no_name_brand_with_only_excerpt
        _mentions_scores_zero) exercises this path.
      - LLM self-report alone (without excerpt match) STILL doesn't
        credit — Gemini sometimes self-reports `brand_appears: true`
        as a generic agreement signal even when the answer doesn't
        actually mention the brand.
      - Triple agreement (excerpt + self-report + grounding source)
        is the threshold for "real editorial citation we should count."

    Upstream-failed runs (`parsed is None` or empty `raw`, typically
    a Gemini timeout / empty response — what surfaces to operators as
    a "network error" in the report) are EXCLUDED from the denominator
    rather than counted as misses. Returned in details with
    `upstream_failed: True` for transparency.

    Returns (score 0–100, per-run match details for audit/UI)."""
    if not runs:
        return (0, [])
    import re
    brand_lower = (merchant_brand or "").strip().lower()
    host_lower = (merchant_host or "").strip().lower()
    # Word-boundary regex for brand matching when the brand is long
    # enough to be specific. Substring matching false-positived no-name
    # brands whose name happens to be a substring of editorial copy
    # (e.g. a 1688 wholesale SKU's brand collapsing to a common word).
    # 3-char brands keep substring match (false-positive class is
    # already moot at that length).
    use_word_boundary = bool(brand_lower) and len(brand_lower) >= 4
    brand_pattern = (
        re.compile(r"\b" + re.escape(brand_lower) + r"\b")
        if use_word_boundary else None
    )
    # Phase B: alias set (trailing-suffix-stripped core / de-spaced / host
    # name) so "BB Lab" in an answer matches a merchant recorded as
    # "BB Lab Global". Purely additive — the literal compare below is
    # unchanged; aliases only catch what it would have missed.
    brand_aliases = derive_brand_aliases(
        merchant_brand,
        merchant_host,
        _clean_identity_tuple(merchant_vendors),
    )

    def _brand_in(text: str) -> bool:
        if brand_lower:
            if brand_pattern is not None:
                if brand_pattern.search(text) is not None:
                    return True
            elif brand_lower in text:
                return True
        return text_mentions_brand(text, brand_aliases)

    details: List[Dict[str, Any]] = []
    matched = 0
    scoreable_runs = 0
    for run in runs:
        # Detect upstream failure: empty raw response or unparseable
        # JSON from Gemini grounded mode. These show up as `raw: ""`
        # + `parsed: null` in the audit dump and are what operators
        # see surfaced as a "network error" in the report. Excluding
        # from denominator avoids dragging the score down for what
        # is a transient probe failure, not a brand-visibility miss.
        raw = run.get("raw")
        parsed_raw = run.get("parsed")
        upstream_failed = (
            parsed_raw is None
            or (isinstance(raw, str) and not raw.strip())
        )
        if upstream_failed:
            details.append({
                "query": run.get("query") or "",
                "in_grounding": False,
                "title_match": False,
                "excerpt_match": False,
                "matched": False,
                "excerpt_only_signal": False,
                "excerpt_corroborated_match": False,
                "upstream_failed": True,
            })
            continue
        scoreable_runs += 1

        url_match = run.get("url_match") or {}
        in_grounding = bool(url_match.get("in_grounding"))
        llm_self_report = bool(url_match.get("llm_self_report"))
        sources = _identify_run_sources(run)
        has_grounding_source = bool(sources)
        title_match = False
        for src in sources:
            label = (src.get("label") or "").lower()
            if _brand_in(label):
                title_match = True
                break
            if host_lower and host_lower in label:
                title_match = True
                break
        parsed = parsed_raw or {}
        # Preserve the original-case evidence excerpt for renderer
        # surfacing (PR-7e). The lowercase copy below is only used
        # for case-insensitive brand matching.
        excerpt_text = (parsed.get("evidence_excerpt") or "").strip()
        excerpt = excerpt_text.lower()
        excerpt_match = _brand_in(excerpt)
        # Three independent paths to a match. The excerpt-corroborated
        # path requires triple agreement (excerpt text + LLM self-report
        # + at least one grounding source) — the threshold designed
        # for the editorial-citation case (Forbes/Women's Health/etc.
        # cited the brand; bare hostname doesn't contain it). See the
        # test_excerpt_corroborated_with_self_report_and_grounding
        # _credits + test_no_name_brand_with_only_excerpt_mentions
        # _scores_zero pair for the discriminator.
        excerpt_corroborated = (
            excerpt_match and llm_self_report and has_grounding_source
        )
        is_match = in_grounding or title_match or excerpt_corroborated
        if is_match:
            matched += 1
        # Capture top source labels for renderer attribution. Limit
        # to first 3 to keep payload size bounded.
        source_labels = [
            (src.get("label") or "").strip()
            for src in sources[:3]
            if (src.get("label") or "").strip()
        ]
        details.append({
            "query": run.get("query") or "",
            "in_grounding": in_grounding,
            "title_match": title_match,
            "excerpt_match": excerpt_match,
            "matched": is_match,
            # excerpt_only_signal flag retained for backwards-compat
            # with the existing UI; now means "excerpt match alone
            # didn't qualify as corroborated" (no llm_self_report or
            # no grounding source).
            "excerpt_only_signal": (
                excerpt_match and not is_match
            ),
            "excerpt_corroborated_match": excerpt_corroborated,
            "upstream_failed": False,
            # PR-7e: preserve the verbatim excerpt + source labels so
            # downstream renderers can build evidence_quotes without
            # re-walking raw_runs. Only attached when the brand was
            # actually mentioned in the excerpt — keeps payload size
            # bounded and avoids leaking unrelated quotes.
            "evidence_excerpt_text": excerpt_text if excerpt_match else None,
            "source_labels": source_labels,
        })
    if scoreable_runs == 0:
        # Every run failed upstream — score is undefined, not zero.
        # Caller's UI surfaces this as "couldn't probe" not "scored 0".
        return (0, details)
    score = round((matched / scoreable_runs) * 100)
    return (score, details)


def extract_category_competitors(
    runs: List[Dict[str, Any]],
    *,
    merchant_host: Optional[str],
    merchant_brand: Optional[str],
    merchant_vendors: Optional[Tuple[str, ...]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Aggregate the rich competitor data Gemini returns on category
    queries — currently dropped on the floor by the BD report. Two
    distinct lists are returned:

      - `competitor_brands`: Counter of brand names from Gemini's
        `competitors_appearing` field (e.g. "Patchology", "Wander
        Beauty"). Direct competitors the merchant should know about.
      - `retailer_hosts`: Legacy alias for non-merchant cited hosts.
        Entries are typed (`type`, `confidence`) so callers can decide
        whether the right label is retailer, publisher, brand site, or
        neutral cited host. Keeping the alias avoids breaking older
        renderers while preventing new copy from treating every host as
        a retailer.

    Within-run dedup: cite Sephora 3× in one answer = 1 for Sephora.
    """
    brand_counter: Counter = Counter()
    retailer_counter: Counter = Counter()
    brand_lower = (merchant_brand or "").strip().lower()
    host_lower = (merchant_host or "").strip().lower()
    # Phase B: alias set so the merchant's own aliased mentions ("BB Lab"
    # for "BB Lab Global") are deduped from competitors / retailers below.
    brand_aliases = derive_brand_aliases(
        merchant_brand,
        merchant_host,
        _clean_identity_tuple(merchant_vendors),
    )
    for run in runs or []:
        # Deep-tier comparison probes NAME a competitor in the question, so
        # counting their answers here would let the probe set inflate the
        # merchant-visible category-competitor panel ("best alternatives to X"
        # guarantees X appears). Organic panels count organic runs only.
        if _run_is_internal_comparison(run):
            continue
        parsed = run.get("parsed") or {}
        run_brands = set()
        for raw_brand in parsed.get("competitors_appearing") or []:
            if not isinstance(raw_brand, str):
                continue
            name = raw_brand.strip()
            if not name:
                continue
            name_lower = name.lower()
            if brand_lower and (
                brand_lower in name_lower or name_lower in brand_lower
            ):
                continue  # skip the merchant's own brand
            if text_mentions_brand(name_lower, brand_aliases):
                continue  # Phase B: an alias of the merchant, not a rival
            if not _valid_competitor_brand_candidate(name):
                continue
            run_brands.add(name)
        for n in run_brands:
            brand_counter[n] += 1

        run_hosts = set()
        for src in _identify_run_sources(run):
            label = src.get("label") or ""
            label_lower = label.lower()
            if not label:
                continue
            if brand_lower and brand_lower in label_lower:
                continue
            if host_lower and host_lower in label_lower:
                continue
            if text_mentions_brand(label_lower, brand_aliases):
                continue  # Phase B: merchant's own aliased citation
            run_hosts.add(label)
        for h in run_hosts:
            retailer_counter[h] += 1

    competitor_brands = [
        {"name": n, "times_cited": c}
        for n, c in brand_counter.most_common(15)
    ]
    retailer_hosts = []
    for h, c in retailer_counter.most_common(15):
        classification = classify_host(h)
        retailer_hosts.append({
            "host": h,
            "times_cited": c,
            "type": classification.get("type") or "unclassified",
            "confidence": classification.get("confidence") or "fallback",
        })
    return (competitor_brands, retailer_hosts)


def _valid_competitor_brand_candidate(name: str) -> bool:
    cleaned = re.sub(r"\s+", " ", str(name or "").strip())
    if not cleaned:
        return False
    lowered = cleaned.lower().strip(" .,:;-/")
    if lowered in _GENERIC_COMPETITOR_PHRASES:
        return False
    if any(
        phrase in lowered
        for phrase in (
            "durable owner",
            "clear owner",
            "single owner",
            "various brand",
            "several brand",
        )
    ):
        return False
    if len(cleaned) > 80:
        return False
    tokens = re.findall(r"[A-Za-z0-9]+", cleaned)
    if not tokens or not any(re.search(r"[A-Za-z]", token) for token in tokens):
        return False
    classification = classify_host(cleaned)
    host_type = str(classification.get("type") or "unclassified").strip().lower()
    if host_type in _SOURCE_ROLE_COMPETITOR_TYPES:
        return False
    return True


# ---------------------------------------------------------------------------
# Competitive pressure — the sharpest BD framing. A merchant might shrug
# off "your visibility is 0/3" if their products still sell through
# retailers. But "competitor X has their own .com cited 2/3 times in the
# same category queries you're invisible in" — that's an immediate
# competitive emergency the merchant can't ignore.
# ---------------------------------------------------------------------------


def _brand_significant_words(brand_name: str) -> List[str]:
    """Lowercased alphanumeric words >=3 chars from a brand name.
    "Beauty of Joseon" → ["beauty", "joseon"] ("of" dropped);
    "YSE Beauty" → ["yse", "beauty"]; "PEACH & LILY" → ["peach", "lily"]."""
    if not brand_name:
        return []
    import re as _re
    words = _re.findall(r"\w+", brand_name.lower())
    return [w for w in words if len(w) >= 3]


def _brand_matches_host_segment(brand_name: str, host_first_segment: str) -> bool:
    """N6 fix (post-#525 codex review P1). Decide whether a competitor
    brand name plausibly OWNS a hostname's first segment.

    The pre-fix heuristic took the single longest brand word and did a
    loose substring check (`"beauty" in "beautyofjoseon"`), so a
    competitor "YSE Beauty" got falsely matched to the merchant's own
    "beautyofjoseon.com" — fabricated peer-host pairing in BD prose.

    Tightened rule — BOTH must hold:
      1. EVERY significant brand word (>=3 chars) is a substring of the
         host's first segment. ("YSE Beauty" → "yse" is not in
         "beautyofjoseon" → reject.)
      2. The brand words cover >=60% of the host segment's length, so a
         brand whose only real word is a generic category term
         ("Beauty Co" → "beauty" = 6/14 of "beautyofjoseon") can't
         claim a much longer unrelated host.

    Still a heuristic (a canonical brand→domain entity map is the P2
    follow-up), but it kills the false-positive class the review flagged
    while keeping true positives: "Beauty of Joseon"→beautyofjoseon.com,
    "PEACH & LILY"→peachandlily.com, "Origins"→origins.com.
    """
    words = _brand_significant_words(brand_name)
    seg = (host_first_segment or "").lower()
    if not words or not seg:
        return False
    if not all(w in seg for w in words):
        return False
    covered = sum(len(w) for w in words)
    return covered / len(seg) >= 0.6


def _build_competitive_pressure(
    *,
    category_competitor_brands: List[Dict[str, Any]],
    category_retailer_hosts: List[Dict[str, Any]],
    merchant_brand: Optional[str],
    merchant_host: Optional[str],
    merchant_attribution_score: int,
) -> Dict[str, Any]:
    """Build the competitive-pressure block surfaced at top-level on the
    structured report. Two parallel lists:

      - peers_named: every competitor brand AI agents name when consumers
        ask about this category. Sorted by mention count.
      - peers_with_first_party_visibility: subset whose .com is cited in
        Gemini grounding for those same category queries. Match via
        _brand_matches_host_segment (all-words + 60%-coverage; the
        merchant's own domains are excluded). The presence of even ONE
        such peer is the BD pressure point.

    The framing string below tells the right story for both cases:
      (a) some peers are first-party visible — urgent: "every retailer-
          routed query is a customer they won and you didn't see"
      (b) no peers are first-party visible — first-mover opportunity:
          "the entire category is retailer-mediated; whoever onboards
          first owns the AI-channel surface"
    """
    peers_named = list(category_competitor_brands or [])
    retailer_hosts = _copyworthy_cited_hosts(category_retailer_hosts)

    # N6 fix: a peer can't be "first-party visible" via the MERCHANT's
    # own domain. The merchant's host set is derived from merchant_brand
    # + merchant_host (the latter is `agent.pivota.cc` for external_seed
    # audits, so the brand-derived candidates are what actually protect
    # the real D2C domain — same shape as the PR-7 rollup exclusion).
    _merchant_own_hosts = _own_host_set(merchant_brand, merchant_host)

    def _normalize_host(h: str) -> str:
        n = (h or "").strip().lower().lstrip(".")
        return n[4:] if n.startswith("www.") else n

    peers_with_fp: List[Dict[str, Any]] = []
    for peer in peers_named:
        brand = peer.get("name") or ""
        if not _brand_significant_words(brand):
            continue
        for host_entry in retailer_hosts:
            host = (host_entry.get("host") or "").lower()
            if not host:
                continue
            # Never let a peer claim the merchant's own domain.
            if _normalize_host(host) in _merchant_own_hosts:
                continue
            first_segment = host.split(".")[0]
            if _brand_matches_host_segment(brand, first_segment):
                peers_with_fp.append({
                    "brand": brand,
                    "first_party_host": host_entry.get("host"),
                    "category_query_mentions": peer.get("times_cited", 0),
                    "host_citations": host_entry.get("times_cited", 0),
                })
                break

    # Did the merchant's own .com appear in any retailer_host? Should
    # almost never be true — but if attribution_score > 0, check.
    merchant_first_party_visible = False
    if merchant_host:
        for host_entry in retailer_hosts:
            host = (host_entry.get("host") or "").lower()
            if merchant_host.lower() in host:
                merchant_first_party_visible = True
                break

    if peers_with_fp:
        framing = (
            f"Of the {len(peers_named)} competitor brands AI agents named "
            f"in this category, {len(peers_with_fp)} have a .com that "
            f"appeared in Gemini grounding sources during the same probe "
            f"window — "
            + ", ".join(
                f"{p['brand']} ({p['first_party_host']})"
                for p in peers_with_fp[:3]
            )
            + (
                f". The peer-host match requires every brand word to "
                f"appear in the hostname (the merchant's own domains "
                f"are excluded), so a coincidental pairing is unlikely "
                f"but not impossible. Your URL appears in "
                f"{merchant_attribution_score}% of buyer-intent queries."
                if not merchant_first_party_visible
                else f". Your own URL also appeared in grounding "
                f"sources during this probe."
            )
        )
        framing += (
            " This is real and immediate competitive pressure: for these "
            "category queries, a peer's own site can be surfaced directly "
            "by AI agents, while your products still depend on third-party "
            "listings."
        )
    else:
        # Name THIS audit's actual cited hosts when available. NOTE
        # these are "non-merchant hosts cited in grounded sources" —
        # could be retailers (nordstrom.com), competitor brand .coms
        # (serenaandlily.com), OR editorial/review sites
        # (businessinsider.com, forbes.com). Don't claim
        # "retailer-mediated" because that's only sometimes true; let
        # the merchant judge the mix from the names. Brand-vs-retailer-
        # vs-media classification is a follow-up.
        if retailer_hosts:
            top_named = ", ".join(
                h.get("host") for h in retailer_hosts[:5] if h.get("host")
            )
            cited_phrase = (
                f"category-level grounding sources came from third-party "
                f"hosts ({top_named})"
                if top_named
                else "category-level grounding sources came from third-party hosts"
            )
        else:
            cited_phrase = (
                "category-level grounding sources came from third-party hosts"
            )
        framing = (
            f"Of the {len(peers_named)} competitor brands AI agents named "
            f"in this category, none had their own .com cited in the "
            f"grounding for these queries. "
            f"{cited_phrase[0].upper() + cited_phrase[1:]}."
        )
        framing += (
            " No peer owns this category in the AI channel yet — it's a "
            "first-mover opportunity to be the brand AI agents cite."
        )

    return {
        "title": "Competitive pressure — your peers in the AI channel",
        "intro": (
            "Your products may still sell well through third-party channels today, "
            "so a 0/3 attribution score might feel low-urgency. The real "
            "BD signal is comparative: which of your direct competitors "
            "have their own .com cited in Gemini grounding for the same "
            "category queries you're invisible in? That's where the "
            "pressure changes."
        ),
        "peers_named": peers_named[:10],
        "peers_with_first_party_visibility": peers_with_fp,
        "merchant_first_party_visible": merchant_first_party_visible,
        "merchant_attribution_score": int(merchant_attribution_score),
        "framing": framing,
    }


# Default thresholds — used when no peer_thresholds is supplied. These
# are intuitive defaults from V1.5 (30 = "barely visible", 60 = "strong
# enough to skip foundational fixes"). They're calibrated to feel right,
# not to peer-distribution data; once Phase 1c (Pivota PDP self-baseline)
# accumulates enough runs, ops can pass `peer_thresholds=` to verdict_for
# with empirical percentile-of-peers values.
DEFAULT_VERDICT_THRESHOLDS: Dict[str, int] = {
    "invisible_max": 30,    # both scores below this → INVISIBLE
    "strong_min": 60,       # both scores at/above this → STRONG
    "misattributed_attr_max": 30,  # MISATTRIBUTED triggers when attribution < this
}


def _failed_attribution_queries(attribution_runs: List[Dict[str, Any]]) -> List[str]:
    """Queries where the merchant's URL was NOT in the grounded sources.
    Shared by verdict_for + _generate_action_items so they read off the
    same evidence vocabulary (no double extraction)."""
    return [
        run.get("query") or ""
        for run in attribution_runs
        if not (run.get("parsed") or {}).get("merchant_url_found")
    ]


def _failed_visibility_queries(visibility_runs: List[Dict[str, Any]]) -> List[str]:
    """Queries where the product wasn't surfaced with grounded sources."""
    return [
        run.get("query") or ""
        for run in visibility_runs
        if not (
            (run.get("parsed") or {}).get("product_visible")
            and (run.get("grounding_chunks") or [])
        )
    ]


def _classify_verdict(
    visibility_score: int,
    attribution_score: int,
    category_visibility_score: Optional[int],
    invisible_max: int,
    strong_min: int,
    misattr_attr_max: int,
) -> str:
    """Pure tier classification — no copy. The gap-based VIA_RETAILERS
    check runs first so cat-strong + attr-weak cases (BoJ cat=67/attr=0,
    COSRX cat=100/attr=33) land in the right tier even when attribution
    clears the misattributed_attr_max floor."""
    if (
        category_visibility_score is not None
        and category_visibility_score >= invisible_max
        and attribution_score < strong_min
        and (category_visibility_score - attribution_score) >= invisible_max
    ):
        return VERDICT_VIA_RETAILERS
    if visibility_score < invisible_max and attribution_score < invisible_max:
        return VERDICT_INVISIBLE
    if attribution_score < misattr_attr_max and visibility_score >= invisible_max:
        return VERDICT_MISATTRIBUTED
    if visibility_score >= strong_min and attribution_score >= strong_min:
        return VERDICT_STRONG
    return VERDICT_PARTIAL


def _explain_verdict(
    label: str,
    visibility_score: int,
    attribution_score: int,
    evidence: Dict[str, Any],
    *,
    metric_label: Optional[str] = None,
) -> str:
    """Compose the merchant-facing diagnostic paragraph for a verdict.

    All output references THIS audit's actual evidence — failed query
    counts, top retailers cited instead of the merchant, gap percentages.
    No BD-pitch macros: no "12% → 25-30%", no "Pivota's agentic-commerce
    protocol", no "complementary to existing retail distribution". Those
    live in `_build_what_pivota_changes` exclusively (and a Phase 6 test
    enforces the boundary).

    When `evidence` is empty (legacy callers that haven't been wired up
    yet — e.g. the calibration-prefix unit tests), falls back to a
    minimal generic sentence that's still data-bound on score values
    and still pitch-free.
    """
    # Lens labeling: the per-SKU path derives BOTH numbers from the citation
    # median (the legacy dual-metric vocabulary predates it), so printing
    # "visibility 48/100, attribution 48/100" collided with the summary score
    # block's differently-defined visibility subscore in the same report.
    # metric_label names the single metric honestly ("citation score 48/100").
    scores_phrase = (
        f"{metric_label} score {visibility_score}/100"
        if metric_label
        else (
            f"visibility {visibility_score}/100, "
            f"attribution {attribution_score}/100"
        )
    )
    vis_phrase = (
        f"{metric_label} score {visibility_score}/100"
        if metric_label
        else f"visibility {visibility_score}/100"
    )
    runs_total = evidence.get("attribution_runs_total")
    cited = evidence.get("merchant_cited_runs")
    top_retailers: List[str] = evidence.get("top_retailers") or []
    cp_framing: Optional[str] = evidence.get("competitive_pressure_framing")
    cat_score = evidence.get("category_score")
    gap_pct = evidence.get("gap_pct")
    failed_sample: List[str] = evidence.get("failed_attribution_query_sample") or []
    # category_match_details: per-run flags from score_category_visibility.
    # Each entry: {in_grounding, title_match, excerpt_match, matched, ...}.
    # Used to gate the "Your URL appears in some category-level grounded
    # sources" claim — that's only true when at least one matched run had
    # in_grounding=True (URL was a grounding chunk). title_match-only
    # signal means the brand was named in a source title but the URL
    # itself did NOT appear, so the URL-appearance claim would be wrong.
    cat_match_details: List[Dict[str, Any]] = (
        evidence.get("category_match_details") or []
    )
    cat_has_url_grounding = any(
        d.get("in_grounding") for d in cat_match_details
    )
    cat_has_title_match = any(
        d.get("title_match") for d in cat_match_details
    )
    # When the audit fell back to fallback (c) — the Pivota canonical
    # PDP at agent.pivota.cc/products/sig_* — the merchant doesn't have
    # an external URL; "your URL" alone is ambiguous to them. Disambig.
    url_source: Optional[str] = evidence.get("url_source")
    is_pivota_canonical = url_source == "pivota_canonical_pdp"
    your_url_label = (
        "Your Pivota canonical URL"
        if is_pivota_canonical
        else "Your URL"
    )

    has_evidence = runs_total is not None and cited is not None
    retailers_phrase = ", ".join(top_retailers[:3])

    if label == VERDICT_INVISIBLE:
        if has_evidence:
            losing = max(0, (runs_total or 0) - (cited or 0))
            if cited and cited > 0:
                base = (
                    f"{your_url_label} was cited in {cited} of "
                    f"{runs_total} buyer-intent queries"
                )
                if losing > 0 and retailers_phrase:
                    base += (
                        f". The other {losing} grounded their answers in "
                        f"third-party sources including {retailers_phrase}"
                    )
                elif losing > 0:
                    base += f". The other {losing} had no grounded merchant URL"
            else:
                base = (
                    f"None of {runs_total} buyer-intent queries cited "
                    f"{your_url_label.lower()}"
                )
                if retailers_phrase:
                    base += (
                        f". Gemini grounded its answers in third-party "
                        f"sources including {retailers_phrase}"
                    )
            base += (
                ". We did not verify whether those sources mention your "
                "brand or products. Possible causes (Google indexing of "
                "your PDPs, query relevance, URL configuration) are "
                "covered by the action items below."
            )
            return base
        return (
            "Across the queries we tested, your URL did not appear in any "
            "grounded source. The next step is to strengthen indexing, "
            "canonical product evidence, and the direct-buy path using "
            "the action items below."
        )

    if label == VERDICT_MISATTRIBUTED:
        if has_evidence:
            losing = max(0, (runs_total or 0) - (cited or 0))
            base = (
                f"AI agents surface your product ({vis_phrase}). "
                f"{your_url_label} was cited "
                f"in {cited} of {runs_total} buyer-intent queries"
            )
            if losing > 0 and retailers_phrase:
                base += (
                    f"; the other {losing} grounded their answers in "
                    f"third-party sources including {retailers_phrase}"
                )
            base += (
                ". We did not verify whether those sources mention your "
                "brand or products."
            )
            if cp_framing:
                base += " " + cp_framing
            return base
        return (
            "AI agents surface your product, but your URL appeared in "
            "few buyer-intent queries; other queries grounded their "
            "answers in third-party sources we did not verify."
        )

    if label == VERDICT_VIA_RETAILERS:
        if has_evidence:
            cs = cat_score if cat_score is not None else "?"
            gp = gap_pct if gap_pct is not None else "?"
            # Pick the right description based on what actually drove
            # the category score. Three cases:
            #   1. URL was a grounding chunk — "Your URL appears..."
            #   2. Brand was named in source titles but URL didn't
            #      appear — "Your brand was named in some source titles
            #      but your URL did not appear..."
            #   3. Score came from neither (legacy or sparse data) —
            #      describe the score without claiming where it came
            #      from.
            if cat_has_url_grounding:
                signal_phrase = (
                    f"{your_url_label} appears in some category-level "
                    f"grounded sources"
                )
            elif cat_has_title_match:
                signal_phrase = (
                    "your brand was named in some category-level "
                    f"grounded source titles, though {your_url_label.lower()} "
                    f"itself did not appear"
                )
            elif cat_match_details:
                # P0-Q1 tier-3: match_details exist but neither
                # url_grounding nor title_match — excerpt-only.
                signal_phrase = (
                    f"your brand was mentioned in category-level "
                    f"answer prose (score {cs}/100), but no grounded "
                    f"source named your brand or your URL"
                )
            else:
                signal_phrase = (
                    "your brand's category presence was not tied to "
                    "specific grounded sources in this analysis"
                )
            base = (
                f"Your category-visibility score is {cs}/100; your "
                f"buyer-intent attribution score is {attribution_score}"
                f"/100 — a {gp}-point gap. " +
                signal_phrase[0].upper() + signal_phrase[1:] +
                ", and your own URL appeared in few of the "
                "buyer-intent queries"
            )
            # P0-Q1: retailers_phrase is category-scope evidence; gate
            # it from buyer-intent prose when cited == 0 (no buyer-
            # intent run returned a grounded source we could verify).
            if retailers_phrase and cited and cited > 0:
                base += (
                    f". For buyer-intent queries where your URL did not "
                    f"appear, Gemini grounded answers in third-party "
                    f"sources including {retailers_phrase}"
                )
                base += (
                    ". We did not verify whether those sources mention "
                    "your brand or products."
                )
            elif (cited == 0) and (runs_total or 0) > 0:
                base += (
                    f". None of the {runs_total} buyer-intent runs "
                    f"returned a grounded source we could attribute "
                    f"to either you or a third-party retailer."
                )
            else:
                base += "."
            if cp_framing:
                base += " " + cp_framing
            return base
        return (
            "There is a gap between this brand's category-level visibility "
            "score and its buyer-intent attribution score. Buyer-intent "
            "queries grounded their answers in third-party retailers and "
            "editorial sources rather than the merchant's own URL; we did "
            "not verify whether those sources mention the brand."
        )

    if label == VERDICT_STRONG:
        # `cited` (merchant_cited_runs) counts runs where the brand was NAMED or
        # its listing surfaced as a source — NOT runs where the merchant's OWN
        # page was the cited source. `own_cited` is that stricter signal. When
        # own-page citation is zero, "cite your URL / goal state" is false: AI
        # mentions the brand but routes buyers to third parties, so the open
        # lever is turning mentions into own-page citations + endorsements — not
        # "post-discovery conversion friction". (own_cited is None for legacy
        # callers that don't supply it → keep the pre-existing wording.)
        own_cited = evidence.get("own_url_cited_runs")
        mention_only = (
            has_evidence
            and own_cited is not None
            and own_cited == 0
            and (cited or 0) > 0
        )
        if mention_only:
            return (
                f"AI names your brand in {cited} of {runs_total} buyer-intent "
                f"queries ({scores_phrase}), but your own page is cited in none "
                "of them — buyers are routed to third-party listings, not your "
                "site. The open lever is turning those brand mentions into "
                "citations of your own page and independent endorsements — see "
                "the recommended actions below."
            )
        # own_cited > 0 (or unknown): report the honest own-URL citation count
        # when we have it, else fall back to the softer `cited`.
        url_cited_count = (
            own_cited if (has_evidence and own_cited is not None) else cited
        )
        # Branded buyer-intent (visibility + attribution) is at goal, but a
        # materially weaker category-visibility score means shoppers who search
        # the CATEGORY rather than the brand still don't surface the product.
        # Mirror classify_primary_gap()'s category_discovery_gap trigger
        # (visibility >= 50 and visibility - category >= 25) exactly, so the
        # top-line verdict never claims "remaining leverage is post-discovery"
        # while next_best_action is prescribing category discovery as the
        # primary open gap. (Without this, STRONG read "both at goal state" for
        # BB Lab cat=33 / Ownist cat=0 — directly contradicting the report's
        # own recommendation.)
        category_discovery_gap = (
            cat_score is not None
            and visibility_score >= 50
            and (visibility_score - int(cat_score)) >= 25
        )
        if category_discovery_gap:
            lead = (
                f"AI agents cite your URL in {url_cited_count} of {runs_total} "
                if has_evidence
                else "AI agents reliably surface this product for "
            )
            ratio_clause = (
                f"buyer-intent queries ({scores_phrase}) — "
                if has_evidence
                else "branded buyer-intent queries AND cite your URL — "
            )
            return (
                lead + ratio_clause
                + "branded discovery and attribution are at goal state. But "
                f"category visibility is {int(cat_score)}/100: shoppers who "
                "search the category rather than your brand still don't surface "
                "your product. Closing that category-discovery gap is the "
                "primary open lever — see the recommended actions below."
            )
        if has_evidence:
            return (
                f"AI agents cite your URL in {url_cited_count} of {runs_total} "
                f"buyer-intent queries ({scores_phrase}). Both discovery "
                "and attribution are at goal state. Remaining leverage "
                "points are post-discovery — conversion friction, schema "
                "drift detection, new-competitor early warning."
            )
        return (
            "AI agents reliably surface this product AND cite the "
            "merchant's own URL as the buying path. Discovery and "
            "attribution are solved at the audit level."
        )

    # PARTIAL
    if has_evidence:
        losing = max(0, (runs_total or 0) - (cited or 0))
        base = (
            f"Mixed result — {scores_phrase}. Of {runs_total} "
            f"buyer-intent queries, {cited} cited {your_url_label.lower()}"
        )
        if losing > 0 and retailers_phrase:
            base += (
                f"; the other {losing} grounded their answers in "
                f"third-party sources including {retailers_phrase}"
            )
        elif losing > 0:
            base += f"; the other {losing} did not cite a merchant URL"
        if failed_sample:
            sample = ", ".join(f'"{q[:50]}"' for q in failed_sample[:2])
            base += f". Failing queries include: {sample}"
        if losing > 0 and retailers_phrase:
            base += (
                ". We did not verify whether those sources mention "
                "your brand or products."
            )
        # The clauses above end inconsistently (the "did not cite a merchant
        # URL" branch has no trailing period), which produced run-ons like
        # "...did not cite a merchant URL The actions below...". Normalize the
        # sentence boundary before appending the closer.
        if not base.endswith("."):
            base += "."
        base += (
            " The actions below show which gap is bigger; close that "
            "one first."
        )
        return base
    return (
        "Mixed result — visibility and attribution scores are both "
        "moderate; neither pattern is consistent across the queries "
        "tested. The action items below show which gap is bigger."
    )


# Brand-level states for the per-SKU audit. "scored" is the normal path
# (a real citation median exists). The other two are honest "we can't compute
# a citation verdict yet" states — they must NOT collapse to a bottom-tier
# "invisible" verdict (the pre-fix bug: verdict_for(int(None or 0)) → invisible).
BRAND_STATE_SCORED = "scored"
BRAND_STATE_BLOCKED_PRE_INDEX = "blocked_pre_index"
BRAND_STATE_INSUFFICIENT_SIGNAL = "insufficient_signal"

_BRAND_VERDICT_BLOCKED_PRE_INDEX = (
    "Not yet visible to AI",
    "Your products aren't indexed in the AI shopping surface yet, so assistants "
    "can't find or recommend them. That's expected this early — getting them "
    "indexed is the first step, and it's the top action below.",
)
_BRAND_VERDICT_INSUFFICIENT_SIGNAL = (
    "Not enough signal yet",
    "We couldn't measure how often AI cites your products in this run. Re-run the "
    "audit once your products are live and indexed to get a representative read.",
)


def _per_sku_brand_verdict(
    median_citation: Optional[int],
    total_skus: int,
    blocked_count: int,
    evidence: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, str]:
    """Honest brand-level verdict for the per-SKU audit.

    Returns (brand_state, label, explanation). When there's no citation signal
    at all (median is None — typically every SKU is blocked / not yet indexed),
    do NOT pass 0 into verdict_for: that emits a false bottom-tier "invisible"
    verdict with no path forward. Tell the merchant the truth instead.

    `evidence` carries the brand's real citation counts (attribution_runs_total,
    merchant_cited_runs summed across SKUs). Passing it is what stops an
    INVISIBLE verdict from asserting the absolute falsehood "your URL did not
    appear in any grounded source": with evidence, _explain_verdict reports the
    honest "cited in N of M queries" and only claims total absence when the
    merchant-cited count is genuinely 0 (the ANUKO 2026-07-02 regression, where
    the brand was first-party cited on real prompts yet told it was invisible).
    """
    if median_citation is None:
        if total_skus > 0 and blocked_count >= total_skus:
            label, explanation = _BRAND_VERDICT_BLOCKED_PRE_INDEX
            return BRAND_STATE_BLOCKED_PRE_INDEX, label, explanation
        label, explanation = _BRAND_VERDICT_INSUFFICIENT_SIGNAL
        return BRAND_STATE_INSUFFICIENT_SIGNAL, label, explanation
    # Both args are the CITATION median (this path has no separate
    # visibility measurement) — metric_label makes the copy say so instead
    # of borrowing the legacy "visibility X/100, attribution Y/100" pair,
    # which collided with the summary score block's visibility subscore.
    label, explanation = verdict_for(
        int(median_citation), int(median_citation), evidence=evidence,
        metric_label="citation",
    )
    return BRAND_STATE_SCORED, label, explanation


def verdict_for(
    visibility_score: int,
    attribution_score: int,
    peer_thresholds: Optional[Dict[str, int]] = None,
    *,
    category_visibility_score: Optional[int] = None,
    evidence: Optional[Dict[str, Any]] = None,
    metric_label: Optional[str] = None,
) -> Tuple[str, str]:
    """Categorize the (visibility, attribution) pair into a verdict
    verdicts and emit an evidence-bound diagnostic paragraph. Returns
    (label, explanation).

    `evidence` is a dict assembled by `build_structured_report` from
    already-extracted probe data:
      - attribution_runs_total: int
      - merchant_cited_runs: int
      - top_retailers: List[str]               (top hosts, len ≤ 3 used)
      - top_cited_hosts: List[Dict]            (typed host shape; when
                                                present, gates retailer
                                                vs publisher wording)
      - competitive_pressure_framing: str|None (from `_build_competitive_pressure`)
      - category_score: int|None
      - gap_pct: int|None                       (category - attribution)
      - failed_attribution_query_sample: List[str]
    When evidence is None or empty (legacy callers, calibration-prefix
    tests), explanations fall back to score-only generic prose.

    `peer_thresholds` (Phase 2c, optional) overrides the V1.5 default
    cutoffs with empirical percentile-of-peers values. Schema:
      {
        invisible_max: int,
        strong_min: int,
        misattributed_attr_max: int,
      }
    Missing keys fall back to defaults individually. When supplied, a
    one-line peer-context prefix is prepended so callers can see "your
    visibility {N}/100" against the calibrated cohort.
    """
    t = dict(DEFAULT_VERDICT_THRESHOLDS)
    if peer_thresholds:
        for k, v in peer_thresholds.items():
            if k in t and isinstance(v, (int, float)) and v >= 0:
                t[k] = int(v)

    invisible_max = t["invisible_max"]
    strong_min = t["strong_min"]
    misattr_attr_max = t["misattributed_attr_max"]

    peer_prefix = ""
    if peer_thresholds:
        peer_prefix = (
            f"_(Calibrated thresholds: peer-cohort INVISIBLE < {invisible_max}/100, "
            f"STRONG ≥ {strong_min}/100. "
            f"Your visibility {visibility_score}/100, attribution {attribution_score}/100.)_  \n\n"
        )

    evidence_dict = evidence or {}
    typed_cited_hosts = evidence_dict.get("top_cited_hosts")
    has_typed_cited_hosts = isinstance(typed_cited_hosts, list)
    typed_retail_hosts = (
        _retail_cited_hosts(typed_cited_hosts) if has_typed_cited_hosts else []
    )
    if (
        has_typed_cited_hosts
        and category_visibility_score is not None
        and category_visibility_score > 0
        and attribution_score == 0
    ):
        # A strong category score is itself evidence the brand surfaces via
        # third parties (Gemini/Vertex grounding often uses redirector hosts
        # with the real retailer in the source TITLE — e.g. "Sephora: <brand>"
        # — so the host alone under-counts retailers). Treat strong category
        # visibility as VIA_RETAILERS; reserve CATEGORY_MENTION for the weaker
        # "mentioned but not clearly via retailers" case.
        label = (
            VERDICT_VIA_RETAILERS
            if (typed_retail_hosts or category_visibility_score >= strong_min)
            else VERDICT_CATEGORY_MENTION_NO_FIRST_PARTY
        )
    else:
        label = _classify_verdict(
            visibility_score,
            attribution_score,
            category_visibility_score,
            invisible_max,
            strong_min,
            misattr_attr_max,
        )
        # NOTE: the attribution==0 typed-hosts case is handled in the `if`
        # branch above (retail-vs-editorial → VIA_RETAILERS vs
        # CATEGORY_MENTION_NO_FIRST_PARTY). Reaching here means attribution>0,
        # so a VIA_RETAILERS verdict came from the category-visibility gap —
        # legitimate evidence the brand surfaces via third parties even when
        # this run's cited hosts aren't classified as retailers. Do NOT
        # downgrade the gap-driven verdict to PARTIAL on host classification.
    explanation = _explain_verdict(
        label, visibility_score, attribution_score, evidence_dict,
        metric_label=metric_label,
    )
    return label, peer_prefix + explanation


def calibrate_thresholds_from_baseline(
    visibility_scores: List[int],
    attribution_scores: List[int],
    *,
    bottom_percentile: int = 25,
    top_percentile: int = 75,
) -> Dict[str, int]:
    """Compute peer-percentile thresholds from a list of historical
    visibility + attribution scores (e.g. from running the Pivota PDP
    self-baseline across all canonical PDPs, or aggregating BD reports
    over time).

    Returns a dict suitable for passing as `peer_thresholds=` to
    verdict_for. Empty inputs return the default thresholds.

    The bottom-quartile (P25) becomes `invisible_max` — anything below
    a quarter of peer scores is genuinely invisible. The top-quartile
    (P75) becomes `strong_min` — STRONG is reserved for the top
    quarter of peers. `misattributed_attr_max` is set to the same P25
    on attribution scores: MISATTRIBUTED fires when attribution is in
    the worst quartile while visibility has cleared the bottom.

    Calibrating per-cohort (e.g. only beauty PDPs, only fashion PDPs)
    is the future direction; V1 just averages globally."""
    if not visibility_scores and not attribution_scores:
        return dict(DEFAULT_VERDICT_THRESHOLDS)

    def _percentile(values: List[int], pct: int) -> Optional[float]:
        if not values:
            return None
        sorted_values = sorted(values)
        # Use the nearest-rank method (simple, no interpolation):
        # rank = ceil(pct/100 * N), clamped to [1, N].
        from math import ceil
        n = len(sorted_values)
        rank = max(1, min(n, ceil((pct / 100) * n)))
        return float(sorted_values[rank - 1])

    vis_p_low = _percentile(visibility_scores, bottom_percentile)
    vis_p_high = _percentile(visibility_scores, top_percentile)
    attr_p_low = _percentile(attribution_scores, bottom_percentile)

    out = dict(DEFAULT_VERDICT_THRESHOLDS)
    if vis_p_low is not None:
        out["invisible_max"] = int(round(vis_p_low))
    if vis_p_high is not None:
        out["strong_min"] = int(round(vis_p_high))
    if attr_p_low is not None:
        out["misattributed_attr_max"] = int(round(attr_p_low))
    return out


# ---------------------------------------------------------------------------
# Structured output the UI consumes (and the CLI converts to markdown)
# ---------------------------------------------------------------------------


# Providers that run a real LLM (vs a mock fallback). DeepSeek is a real
# backend-direct provider — it must be here, or the multi-provider mock guard
# wrongly rejects a combined "gemini,deepseek" report as synthetic.
_REAL_PROVIDERS = {"gemini", "chatgpt", "claude", "deepseek"}


def _classify_provider(upstream_provider: str) -> Dict[str, Any]:
    """Categorize what the upstream actually used.

    Returns:
      - is_real: True if upstream ran a real LLM (gemini), False on any
        mock variant.
      - reason: a human-readable explanation surfaced in UI when a
        fallback happened. None when is_real.
    """
    p = (upstream_provider or "").strip()
    parts = [
        item.strip()
        for item in re.split(r"[,+]", p)
        if item.strip()
    ]
    if parts and all(part in _REAL_PROVIDERS for part in parts):
        return {"is_real": True, "reason": None}
    if p in _REAL_PROVIDERS:
        return {"is_real": True, "reason": None}
    if p == "local_mock_no_internal_key":
        # Emitted by services/agent_center_llm_client.py when none of
        # PROMOTIONS_ADMIN_KEY / AGENT_API_KEY / PIVOTA_AGENT_INTERNAL_API_KEY
        # are set on the backend — the call never left the backend at all.
        return {
            "is_real": False,
            "reason": (
                "Backend probe-auth env var is unset on Railway "
                "(web-production-fedb). The probe accepts any of "
                "`PROMOTIONS_ADMIN_KEY` (preferred — production already "
                "shares this admin secret with PIVOTA-Agent), "
                "`AGENT_API_KEY`, or `PIVOTA_AGENT_INTERNAL_API_KEY`. "
                "The value must match what's set on PIVOTA-Agent "
                "(pivota-agent-production). Without it the probe never "
                "reaches the upstream and pivota-backend synthesizes a "
                "local mock instead."
            ),
        }
    if p == "mock_fallback_no_gemini_key":
        # Emitted by PIVOTA-Agent's buildGeminiProbe when GoogleGenAI
        # client init fails (no GEMINI_API_KEY).
        return {
            "is_real": False,
            "reason": (
                "PIVOTA-Agent's `GEMINI_API_KEY` is unset on Railway "
                "(pivota-agent-production). The probe reached the upstream "
                "service but couldn't initialize the Gemini client. "
                "Configure `GEMINI_API_KEY` in PIVOTA-Agent's Railway env."
            ),
        }
    if p == "mock":
        # Operator explicitly requested provider=mock, OR upstream
        # returned the deterministic stub for some other reason.
        return {
            "is_real": False,
            "reason": (
                "Upstream returned `mock` — usually because the request "
                "explicitly set provider=mock. If you requested gemini and "
                "got this, check both backend and PIVOTA-Agent env vars."
            ),
        }
    return {
        "is_real": False,
        "reason": f"Unrecognized upstream provider value: {p!r}",
    }


# ---------------------------------------------------------------------------
# Per-SKU v3 scorecard builders (Brief 2, spec sections A-D)
# ---------------------------------------------------------------------------


_SKU_CONTEXT_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}

# URL-audit (wedge) synthetic SKU contexts. A merchant pastes product URLs
# with NO synced catalog, so there's no catalog_skus/catalog_products row to
# load. We build a synthetic ctx from the fetched PDP (title/vendor/type/url)
# and register it here so load_sku_context() returns it on a catalog miss.
#
# This is a SEPARATE registry from _SKU_CONTEXT_CACHE on purpose:
# run_brand_report(audit_mode="per_sku") calls reset_sku_context_cache() AFTER
# the probe fan-out but BEFORE the report-assembly loop re-reads each ctx, so a
# context placed only in the cache would be wiped and the assembly loop would
# fall through to the catalog (miss) — nulling every dimension AND skipping
# citation scoring. The registry is never auto-cleared, so the synthetic ctx
# survives the reset and both the fan-out and the assembly loop see the same
# context. The worker re-registers from the persisted launch.synthetic_products
# on resume, so cross-process replay works too. Keyed by (sku_key, merchant_id),
# with sku_key namespaced `urlwedge:*` to guarantee no collision with real keys.
_SYNTHETIC_SKU_CONTEXTS: Dict[Tuple[str, str], Dict[str, Any]] = {}


def reset_sku_context_cache() -> None:
    """Test hook and audit-run boundary helper.

    Clears the per-run catalog-ctx cache. Does NOT clear the synthetic
    URL-audit registry — those contexts must survive this reset (see
    _SYNTHETIC_SKU_CONTEXTS) so the per_sku assembly loop re-reads them.
    """
    _SKU_CONTEXT_CACHE.clear()


def _synthetic_enrichment_from_attrs(
    attrs: Any,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Derive a lightweight product_enrichment + a plain description from the
    fetched PDP's attributes_raw (Shopify body_html/description/tags, or JSON-LD
    description). Deterministic, no LLM — keeps wedge latency low. Returns
    (enrichment, description). These feed the per-SKU base query specs
    (topic_tags/bullet_points) and content-richness scoring (summary_short/
    bullet_points), which were empty for synthetic products before."""
    if not isinstance(attrs, dict):
        return {}, None
    desc = str(attrs.get("description") or "").strip()
    body_html = str(attrs.get("body_html") or "")
    bullets: List[str] = []
    if body_html:
        for raw in re.findall(r"<li[^>]*>(.*?)</li>", body_html, flags=re.I | re.S):
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 3:
                bullets.append(text)
    if not bullets and desc:
        for sentence in re.split(r"(?<=[.!?])\s+", desc):
            s = sentence.strip()
            if len(s) > 20:
                bullets.append(s)
    tags = [str(t).strip() for t in (attrs.get("tags") or []) if str(t).strip()]
    enrichment: Dict[str, Any] = {}
    if desc:
        enrichment["summary_short"] = desc[:280]
    if bullets:
        enrichment["bullet_points"] = bullets[:6]
    if tags:
        enrichment["topic_tags"] = tags[:8]
    return enrichment, (desc or None)


def build_synthetic_sku_context(
    item: Dict[str, Any], merchant_id: str,
) -> Dict[str, Any]:
    """Build a per-SKU context for a URL-audit product with no synced catalog.

    `item` is the fetched/curated product shape from fetch_curated_audit_product
    plus the synthetic keys minted at enqueue:
    `{sku_key, product_key, title, raw_title?, vendor?, product_type?, pdp_url,
      attributes_raw?}`.

    The shape mirrors what the per-SKU consumers read: `_get_product(ctx)`
    returns `ctx["product"]`, the probe anchor reads `product.canonical_url`
    (NOT pdp_url), and build_per_sku_report's null-guard only fires when
    `missing_inputs AND not product.product_key` — so product_key MUST be set
    for citation scoring to run. Catalog-only dimensions degrade to low scores
    with missing_inputs=["catalog_skus"]; that's the "connect store to measure"
    funnel, surfaced at read time.
    """
    title = str(item.get("title") or item.get("raw_title") or "").strip()
    vendor = (str(item.get("vendor") or "").strip() or None)
    product_type = (str(item.get("product_type") or "").strip() or None)
    pdp_url = str(item.get("pdp_url") or "").strip() or None
    # canonical_url = the FIRST-PARTY surface ("is your own URL cited?"). For a
    # retail-channel URL (the merchant pasted a retailer page like oliveyoung),
    # the route sets this to the brand's OWN site so first-party citation is
    # measured against the brand — not the retailer (which would mislabel
    # distribution as first-party endorsement). pdp_url stays the pasted page.
    canonical_url = (str(item.get("canonical_url") or "").strip() or pdp_url)
    product = {
        "product_key": item.get("product_key"),
        "title": title,
        "vendor": vendor,
        "brand": vendor,
        "product_type": product_type,
        "category": product_type,
        "canonical_url": canonical_url,
        "pdp_url": pdp_url,
    }
    attrs = item.get("attributes_raw")
    if attrs is not None:
        product["attributes_raw"] = attrs
    # Deepen the synthetic ctx from the fetched page: a plain description (feeds
    # content-richness raw-PDP fraction) + a product_enrichment block
    # (topic_tags/bullet_points/summary_short) the base query specs + content
    # scoring read. Without these the wedge's niche lanes + content signal were
    # thin even though the page content was sitting in attributes_raw.
    enrichment, description = _synthetic_enrichment_from_attrs(attrs)
    if description:
        product["description"] = description
    ctx: Dict[str, Any] = {
        "sku_key": item.get("sku_key"),
        "merchant_id": merchant_id,
        "sku_title": title,
        "product": product,
        "sku": {"sku_key": item.get("sku_key"), "title": title},
        # Marks catalog dimensions as un-measurable (no synced catalog) without
        # tripping the all-null guard (product_key is set above).
        "missing_inputs": ["catalog_skus"],
        "synthetic_url_audit": True,
    }
    if enrichment:
        ctx["product_enrichment"] = enrichment
    return ctx


def register_synthetic_sku_contexts(
    items: List[Dict[str, Any]], merchant_id: str,
) -> List[str]:
    """Register synthetic URL-audit contexts so load_sku_context() resolves
    them. Idempotent; returns the sku_keys registered. Called by the worker at
    discovery (and on resume) from the persisted launch.synthetic_products."""
    keys: List[str] = []
    for item in items or []:
        sku_key = str((item or {}).get("sku_key") or "").strip()
        if not sku_key:
            continue
        ctx = build_synthetic_sku_context(item, str(merchant_id))
        _SYNTHETIC_SKU_CONTEXTS[(sku_key, str(merchant_id))] = ctx
        keys.append(sku_key)
    return keys


def clear_synthetic_sku_contexts(sku_keys: List[str], merchant_id: str) -> None:
    """Drop a run's synthetic contexts once it's done, to bound registry growth."""
    for sku_key in sku_keys or []:
        _SYNTHETIC_SKU_CONTEXTS.pop((str(sku_key or ""), str(merchant_id)), None)


def _row_dict(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return None


def _json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value.strip() else []
        if isinstance(parsed, list):
            return parsed
        return [parsed] if parsed is not None else []
    return [value]


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def _as_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return n


def _normalize_percent(value: Any) -> Optional[float]:
    n = _as_number(value)
    if n is None:
        return None
    if 0 <= n <= 1:
        n *= 100.0
    return max(0.0, min(100.0, n))


def _points_from_percent(value: Any, max_points: int) -> int:
    pct = _normalize_percent(value)
    if pct is None:
        return 0
    return int(round(max_points * (pct / 100.0)))


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _norm_identity_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _norm_text(value)).strip()


def _get_product(sku_ctx: Dict[str, Any]) -> Dict[str, Any]:
    product = sku_ctx.get("product")
    return product if isinstance(product, dict) else sku_ctx


def _get_sku(sku_ctx: Dict[str, Any]) -> Dict[str, Any]:
    sku = sku_ctx.get("sku")
    return sku if isinstance(sku, dict) else sku_ctx


def _get_index_state(sku_ctx: Dict[str, Any]) -> Dict[str, Any]:
    state = sku_ctx.get("index_pipeline_state")
    return state if isinstance(state, dict) else {}


def _get_enrichment(sku_ctx: Dict[str, Any]) -> Dict[str, Any]:
    enrichment = sku_ctx.get("product_enrichment") or sku_ctx.get("enrichment")
    return enrichment if isinstance(enrichment, dict) else {}


def resolve_sku_identity(sku_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a best-available, bad-name-tolerant product identity for a SKU.

    Merchant catalogs frequently carry variant/format labels as the SKU title
    ("Garden Gift Set", "14 Servings, 2-Week Routine"), so we never trust
    `sku.title` as the identity. Name precedence (most→least curated):
      enrichment.title_override → brand + product.title → product.title → sku.title.
    We also surface name-INDEPENDENT anchors (PDP url/domain, brand, category,
    GTIN/barcode, content_key, product_group) for identity-robust matching, plus
    a `confidence` and `source`. `unresolved` (confidence == "low") means we only
    have a variant label / no product-level name — such SKUs should be flagged
    "enrich before trusting visibility" rather than scored as invisible.
    """
    product = _get_product(sku_ctx or {})
    sku = _get_sku(sku_ctx or {})
    enrichment = _get_enrichment(sku_ctx or {})

    def _s(value: Any) -> str:
        return str(value).strip() if value not in (None, "") else ""

    brand = _s(product.get("brand") or product.get("vendor"))
    product_title = _s(product.get("title"))
    sku_title = _s(sku.get("title"))
    title_override = _s(enrichment.get("title_override"))

    def _with_brand(title: str) -> str:
        # Brand-prefix unless already present (mirrors prompt identity, #713).
        if brand and brand.lower() not in title.lower():
            return f"{brand} {title}"
        return title

    if title_override:
        name, confidence, source = _with_brand(title_override), "high", "enrichment.title_override"
    elif product_title:
        # Product-level catalog name (not the variant label). Usable but uncurated.
        name, confidence, source = _with_brand(product_title), "medium", "catalog.product_title"
    elif sku_title:
        # Only a variant/format label is available — identity is unreliable, but
        # still brand-prefix so the probe name is as useful as possible.
        name, confidence, source = _with_brand(sku_title), "low", "catalog.sku_title"
    else:
        name, confidence, source = _s(sku_ctx.get("sku_key")) or "this product", "low", "fallback.sku_key"

    canonical_url = _s(
        product.get("canonical_url")
        or product.get("pivota_canonical_url")
        or sku_ctx.get("canonical_url")
        or sku_ctx.get("pivota_canonical_url")
    )
    anchors = {
        "canonical_url": canonical_url or None,
        "domain": normalize_host(canonical_url) if canonical_url else None,
        "brand": brand or None,
        "category": _s(product.get("category") or product.get("product_type")) or None,
        "gtin": _s(sku.get("barcode") or sku.get("gtin")) or None,
        "content_key": _s(product.get("content_key") or sku_ctx.get("content_key")) or None,
        "product_group_id": sku_ctx.get("product_group_id") or None,
    }
    return {
        "name": name,
        "confidence": confidence,
        "source": source,
        "anchors": anchors,
        "unresolved": confidence == "low",
    }


def _get_quality(sku_ctx: Dict[str, Any]) -> Dict[str, Any]:
    quality = sku_ctx.get("product_quality_snapshot") or sku_ctx.get("quality_snapshot")
    return quality if isinstance(quality, dict) else {}


def _get_offers(sku_ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        row for row in _json_list(sku_ctx.get("offers") or sku_ctx.get("catalog_offers"))
        if isinstance(row, dict)
    ]


def _get_all_skus(sku_ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    all_skus = [
        row for row in _json_list(sku_ctx.get("all_skus") or sku_ctx.get("catalog_skus"))
        if isinstance(row, dict)
    ]
    if all_skus:
        return all_skus
    sku = _get_sku(sku_ctx)
    return [sku] if sku else []


def _add_bucket(
    breakdown: Dict[str, Any],
    missing_inputs: List[str],
    name: str,
    points: int,
    max_points: int,
    reason: str,
    *,
    missing: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    safe_points = max(0, min(max_points, int(round(points))))
    bucket = {"points": safe_points, "max": max_points, "reason": reason}
    if extra:
        bucket.update(extra)
    breakdown[name] = bucket
    for item in missing or []:
        if item not in missing_inputs:
            missing_inputs.append(item)


def _finish_breakdown(
    breakdown: Dict[str, Any],
    missing_inputs: List[str],
) -> Tuple[int, Dict[str, Any]]:
    total = sum(
        int(v.get("points") or 0)
        for k, v in breakdown.items()
        if isinstance(v, dict) and k not in {"total", "missing_inputs"}
    )
    total = max(0, min(100, int(round(total))))
    breakdown["total"] = total
    if missing_inputs:
        breakdown["missing_inputs"] = missing_inputs
    return total, breakdown


def _vertical_for(product: Dict[str, Any]) -> str:
    # Delegates to the single shared resolver (services.vertical_profiles).
    # Called WITHOUT a title so this keeps _vertical_for's legacy category-text
    # semantics (product_type/category/category_path only) byte-identical for
    # beauty; the resolver only adds unambiguous electronics/audio recall + the
    # incidental-weak-token demotion, neither of which touches a genuine beauty
    # SKU. See the golden-file regression guard.
    return resolve_vertical(product)


def _sku_vertical_signal_text(product: Mapping[str, Any]) -> str:
    """The free-text a SKU's vertical is resolved from when it has no structured
    product_type/category — title PLUS attributes_raw values (tags, description).
    Mirrors the `combined` text the unbranded-category fallback already reads, so
    a beauty SKU whose only beauty signal lives in its tags/description (e.g. a
    supplement whose fetched product_type is the noisy "grape jelly") still
    resolves beauty instead of collapsing to the generic profile."""
    parts: List[str] = [str(product.get("title") or ""), str(product.get("raw_title") or "")]
    attrs = product.get("attributes_raw")
    if isinstance(attrs, Mapping):
        for value in attrs.values():
            if isinstance(value, (str, int, float)):
                parts.append(str(value))
        tags = attrs.get("tags")
        if isinstance(tags, list):
            parts.extend(str(tag) for tag in tags)
    return " ".join(p for p in parts if p)


def _resolved_vertical_for_ctx(
    sku_ctx: Optional[Mapping[str, Any]],
    product: Optional[Mapping[str, Any]],
) -> str:
    """The resolved vertical for a SKU-context, honoring Principle 1 (resolve
    once, pass down). Preference order: a vertical already stashed on the ctx ->
    the durable `resolved_vertical` column on the product row -> live resolution
    (product_type/category first, then the title + tags/description signal, so
    URL-audit / store-less SKUs still resolve). Returns one of
    beauty|fashion|electronics|other."""
    for source in (sku_ctx, product):
        if isinstance(source, Mapping):
            value = str(source.get("vertical") or source.get("resolved_vertical") or "").strip().lower()
            if value in {"beauty", "fashion", "electronics", "other"}:
                return value
    prod = product if isinstance(product, Mapping) else {}
    return resolve_vertical(prod, title=_sku_vertical_signal_text(prod))


def _profile_for_sku_ctx(
    sku_ctx: Optional[Mapping[str, Any]],
    product: Optional[Mapping[str, Any]],
) -> VerticalProfile:
    """The VerticalProfile for a SKU-context. Beauty -> beauty profile (identical
    behavior); unknown -> generic (never beauty-as-fallback). The `electronics`
    vertical is sub-split into its drone vs audio profile from the SKU text (the
    resolved vertical stays `electronics` either way)."""
    vertical = _resolved_vertical_for_ctx(sku_ctx, product)
    prod = product if isinstance(product, Mapping) else {}
    return resolve_profile_for_vertical(
        vertical, prod, title=_sku_vertical_signal_text(prod)
    )


def _merchant_profile_from_reports(
    per_sku_reports: Optional[List[Mapping[str, Any]]],
) -> VerticalProfile:
    """The MERCHANT/run-level profile — the dominant vertical across the audit's
    SKUs. Used by the run-level competitor panels (reseller not-carried, merchant
    narrative) that aggregate names across SKUs and so have no single SKU vertical.
    Defaults to beauty (byte-identical) when nothing resolves. Per Principle 1,
    merchant/audit vertical is a default; per-SKU panels still resolve per-SKU."""
    from collections import Counter

    counts: Counter = Counter()
    for report in per_sku_reports or []:
        if not isinstance(report, Mapping):
            continue
        # Count PROFILE names (not raw verticals) so a drone-dominant run resolves
        # the drone profile, not the electronics_audio default.
        profile = resolve_profile(
            {
                "product_type": report.get("product_type"),
                "category": report.get("product_type"),
            },
            title=report.get("title"),
        )
        counts[profile.name] += 1
    if not counts:
        return BEAUTY_PROFILE
    return get_profile(counts.most_common(1)[0][0])


# --- Phase 2b: live LLM attribute extractor (flag-gated) -------------------- #
# Stash key on the SKU context; the grounded attributes are merged into the
# attribute graph ONLY at the probe-seeding sites, so they influence what the
# audit probes without touching the lexicon path when the flag is off.
_LLM_ATTR_STASH_KEY = "_llm_extracted_attributes"


def _resolve_extractor_provider() -> Tuple[Optional[str], Optional[str]]:
    """Provider/model for the attribute extractor. An explicit, keyed
    ATTRIBUTE_EXTRACTOR_PROVIDER wins; otherwise reuse the (cheap, key-aware)
    prompt-gen chain. ATTRIBUTE_EXTRACTOR_MODEL overrides the model when set."""
    from config.settings import settings as app_settings

    override_model = (app_settings.attribute_extractor_model or "").strip()
    explicit = (app_settings.attribute_extractor_provider or "").strip()
    if explicit:
        from services.llm_synthesis import (
            LLMSynthesisError,
            configured_key_for_provider,
            default_model_for_provider,
            normalize_provider,
        )
        try:
            canonical = normalize_provider(explicit)
            if configured_key_for_provider(canonical):
                return canonical, override_model or default_model_for_provider(canonical)
        except LLMSynthesisError:
            pass
    provider, model = _resolve_prompt_gen_provider()
    return provider, (override_model or model)


def _stashed_grounded_attributes(sku_ctx: Optional[Mapping[str, Any]]) -> List[GroundedAttribute]:
    if not isinstance(sku_ctx, Mapping):
        return []
    stash = sku_ctx.get(_LLM_ATTR_STASH_KEY)
    return list(stash) if isinstance(stash, list) else []


def _attribute_graph_for_probes(
    sku_ctx: Optional[Mapping[str, Any]],
    product: Mapping[str, Any],
) -> Dict[str, Any]:
    """The attribute graph the probe generators consume: the deterministic
    lexicon graph, plus any flag-gated LLM-extracted (and grounded) attributes
    stashed on the ctx. With the flag off / nothing stashed this is exactly
    build_sku_attribute_graph(product) — no behavior change."""
    graph = build_sku_attribute_graph(product)
    grounded = _stashed_grounded_attributes(sku_ctx)
    if grounded:
        merge_grounded_into_graph(graph, grounded)
    return graph


async def _maybe_stash_llm_attributes(ctx: Dict[str, Any]) -> None:
    """When the extractor flag is on, run the LLM attribute extractor for SKUs the
    lexicon can't serve and stash the grounded attributes on the ctx. Best-effort:
    any failure leaves the ctx untouched (the audit falls back to the lexicon
    path). No-op when the flag is off — so beauty and every existing audit are
    byte-identical."""
    from config.settings import settings as app_settings

    if not getattr(app_settings, "attribute_extractor_enabled", False):
        return
    # Per-merchant scoping: when an allowlist is configured, run ONLY for those
    # merchants (Mojawa-scoped pilot). Empty allowlist -> no restriction.
    allowlist = getattr(app_settings, "attribute_extractor_merchants", set()) or set()
    if allowlist:
        merchant_id = str(
            (ctx.get("merchant_id") if isinstance(ctx, Mapping) else "")
            or (_get_product(ctx) or {}).get("merchant_id")
            or ""
        ).strip()
        if merchant_id not in allowlist:
            return
    product = _get_product(ctx)
    if not isinstance(product, Mapping) or not product:
        return
    try:
        profile = _profile_for_sku_ctx(ctx, product)
        lexicon_graph = build_sku_attribute_graph(product)
        if not should_run_extractor(profile, lexicon_graph):
            return
        # Feed Tier-1 retailer evidence into the extractor's source text. This
        # extractor runs INSIDE load_sku_context — BEFORE the probe fan-out loop
        # stashes _retailer_excerpts onto the product — so without this preload
        # build_source_text() below sees only the (often thin) own-page copy and
        # the extractor can never ground attributes in the retailer listings that
        # exist precisely to rescue thin url-wedge pages. Gated AFTER
        # should_run_extractor so the "does this SKU need the LLM?" decision still
        # keys off the own page, and isolated in its own try so a lookup failure
        # falls back to own-page-only extraction rather than disabling the
        # extractor. Only prior completed runs carry excerpts (recycling), so a
        # first-ever audit legitimately has none. Skipped when excerpts are
        # already present (e.g. a re-read of a ctx the probe loop enriched).
        if isinstance(product, dict) and not product.get("_retailer_excerpts"):
            try:
                from services.retailer_evidence import load_prior_retailer_evidence

                _re = await load_prior_retailer_evidence(
                    merchant_id=str(
                        ctx.get("merchant_id")
                        or product.get("merchant_id")
                        or ""
                    ),
                    sku_key=str(ctx.get("sku_key") or ""),
                )
                if _re.get("excerpts"):
                    product["_retailer_excerpts"] = _re["excerpts"]
                    product["_retailer_excerpt_hosts"] = _re["hosts"]
            except Exception:  # noqa: BLE001 - preload must never disable extraction
                logger.debug(
                    "extractor retailer-excerpt preload skipped", exc_info=True
                )
        source_text = build_source_text(product)
        if not source_text.strip():
            return
        fingerprint = source_fingerprint(source_text)
        # Durable cache read-through: use the cached grounded attributes when the
        # product copy is unchanged (source_hash match). A hit — even an empty one
        # (a genuinely attribute-less SKU) — skips the LLM entirely.
        cached = _cached_llm_attributes(product, fingerprint)
        if cached is not None:
            if cached:
                ctx[_LLM_ATTR_STASH_KEY] = cached
            return
        provider, model = _resolve_extractor_provider()
        if not provider:
            return
        from services.llm_synthesis import synthesize

        grounded = await extract_attributes(
            product,
            synthesize=synthesize,
            provider=provider,
            model=model or "",
            max_tokens=int(getattr(app_settings, "attribute_extractor_max_tokens", 4000) or 4000),
            source_text=source_text,
        )
        if grounded:
            ctx[_LLM_ATTR_STASH_KEY] = list(grounded)
        # Persist (best-effort) so the next audit doesn't re-pay — negative results
        # cached too. Only for catalog-resident SKUs (URL-audit synthetics have no row).
        await _persist_llm_attributes(product, fingerprint, grounded)
    except Exception as exc:  # never let extraction break context load
        logger.warning("attribute extractor stash failed: %s", exc)


def _cached_llm_attributes(
    product: Mapping[str, Any],
    fingerprint: str,
) -> Optional[List[GroundedAttribute]]:
    """Return the cached grounded attributes when the durable cache matches the
    current source copy, else None (a miss -> extract). The cache is trusted only
    on a source_hash match, so a stale span can never seed a probe."""
    cached = product.get("llm_attributes")
    if not isinstance(cached, Mapping):
        return None
    if str(cached.get("source_hash") or "") != fingerprint:
        return None
    return deserialize_grounded(cached.get("attributes"))


async def _persist_llm_attributes(
    product: Mapping[str, Any],
    fingerprint: str,
    grounded: Sequence[GroundedAttribute],
) -> None:
    """Best-effort durable write of the extractor cache to catalog_products. Never
    raises into the audit; skips URL-audit synthetics (no catalog row)."""
    product_key = str(product.get("product_key") or "").strip()
    merchant_id = str(product.get("merchant_id") or "").strip()
    if not product_key or not merchant_id:
        return
    payload = json.dumps({
        "source_hash": fingerprint,
        "attributes": serialize_grounded(grounded),
    })
    try:
        from db.database import database

        await database.execute(
            """
            UPDATE catalog_products
               SET llm_attributes = CAST(:payload AS jsonb)
             WHERE product_key = :product_key
               AND merchant_id = :merchant_id
            """,
            {"payload": payload, "product_key": product_key, "merchant_id": merchant_id},
        )
    except Exception as exc:
        logger.warning("attribute extractor cache write failed: %s", exc)


def _confidence_ok(value: Any) -> bool:
    n = _as_number(value)
    return n is None or n >= 0.6


def _freshness_current(value: Any) -> bool:
    data = _json_obj(value)
    if not data:
        return False
    for key in ("current", "is_current", "fresh"):
        if data.get(key) is True:
            return True
    status = _norm_text(data.get("status") or data.get("state"))
    if status in {"current", "fresh", "valid", "ok"}:
        return True
    for key in ("fresh_until", "expires_at", "valid_until"):
        raw = data.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= datetime.now(timezone.utc):
            return True
    return False


def _has_blocking_safety_flag(flags: Any) -> bool:
    for flag in _json_list(flags):
        if isinstance(flag, dict):
            text = " ".join(str(flag.get(k) or "") for k in ("severity", "level", "code", "message")).lower()
        else:
            text = str(flag or "").lower()
        if any(marker in text for marker in ("blocking", "blocker", "unsafe", "unsupported_claim")):
            return True
    return False


def _has_claims(product: Dict[str, Any], sku_ctx: Dict[str, Any]) -> bool:
    text = " ".join(
        str(x or "")
        for x in (
            product.get("description"),
            product.get("title"),
            product.get("product_type"),
            product.get("category"),
            _json_obj(product.get("product_payload")).get("claims"),
            _json_obj(sku_ctx.get("beauty_product_profile") or {}).get("claims_json"),
        )
    ).lower()
    return any(
        marker in text
        for marker in (
            "claim", "clinical", "clinically", "treat", "heals", "cure",
            "anti-aging", "anti aging", "immune", "inflammation", "acne",
            "spf", "fda", "gmp",
        )
    )


def _has_substantiation(product: Dict[str, Any], sku_ctx: Dict[str, Any]) -> bool:
    # Phase 2b: merchant-supplied substantiated evidence (the general
    # product_evidence store, plumbed in by load_sku_context) satisfies
    # substantiation exactly like a beauty-profile claim or intel source_coverage.
    # Boolean gate → no double-counting with the other signals below.
    if sku_ctx.get("has_substantiated_evidence"):
        return True
    payload = _json_obj(product.get("product_payload"))
    intel = _json_obj(payload.get("product_intel") or {}).get("product_intel_core")
    if isinstance(intel, dict) and _nonempty(intel.get("source_coverage")):
        return True
    if _nonempty(payload.get("substantiation")) or _nonempty(payload.get("watchouts")):
        return True
    profile = _json_obj(sku_ctx.get("beauty_product_profile") or {})
    return _nonempty(profile.get("claims_json")) or _nonempty(profile.get("benefits_json"))


def compute_identity_score(sku_ctx: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Spec A.1 identity score. Pure: reads only the normalized SKU context."""
    product = _get_product(sku_ctx or {})
    sku = _get_sku(sku_ctx or {})
    state = _get_index_state(sku_ctx or {})
    breakdown: Dict[str, Any] = {}
    missing: List[str] = []

    content_key = product.get("content_key") or sku_ctx.get("content_key")
    _add_bucket(
        breakdown, missing, "content_key",
        20 if _nonempty(content_key) else 0,
        20,
        "content_key present" if _nonempty(content_key) else "data unavailable",
        missing=None if _nonempty(content_key) else ["catalog_products.content_key"],
    )

    sig = product.get("pivota_signature_id") or sku_ctx.get("pivota_signature_id")
    pivota_url = product.get("pivota_canonical_url") or sku_ctx.get("pivota_canonical_url")
    has_sig = _nonempty(sig) and _nonempty(pivota_url)
    _add_bucket(
        breakdown, missing, "pivota_signature",
        15 if has_sig else 0,
        15,
        "signature and canonical URL present" if has_sig else "data unavailable",
        missing=None if has_sig else ["catalog_products.pivota_signature_id", "catalog_products.pivota_canonical_url"],
    )

    group_members = [
        row for row in _json_list(sku_ctx.get("product_group_members"))
        if isinstance(row, dict) and _nonempty(row.get("product_group_id"))
    ]
    identity_resolved = bool(state.get("identity_resolved")) or bool(group_members) or _nonempty(state.get("product_group_id"))
    _add_bucket(
        breakdown, missing, "identity_resolution",
        20 if identity_resolved else 0,
        20,
        "identity resolved" if identity_resolved else "data unavailable",
        missing=None if identity_resolved else ["index_pipeline_state.identity_resolved", "product_group_members.product_group_id"],
    )

    all_skus = _get_all_skus(sku_ctx or {})
    if not all_skus:
        variant_points = 0
        variant_reason = "data unavailable"
        variant_missing = ["catalog_skus"]
    else:
        def _has_variant_identity(row: Dict[str, Any]) -> bool:
            return (
                _nonempty(row.get("barcode"))
                or _nonempty(row.get("sku"))
                or _nonempty(row.get("visible_option_labels"))
            )
        if all(_has_variant_identity(row) for row in all_skus):
            variant_points = 15
            variant_reason = "variant identity present on every active SKU"
            variant_missing = None
        elif _nonempty(product.get("product_key")) or _nonempty(product.get("source_product_id")) or _nonempty(product.get("title")):
            variant_points = 8
            variant_reason = "only product-level identity present"
            variant_missing = ["catalog_skus.barcode", "catalog_skus.sku", "catalog_skus.visible_option_labels"]
        else:
            variant_points = 0
            variant_reason = "data unavailable"
            variant_missing = ["catalog_skus.barcode", "catalog_skus.sku", "catalog_skus.visible_option_labels"]
    _add_bucket(breakdown, missing, "variant_identity", variant_points, 15, variant_reason, missing=variant_missing)

    title = str(product.get("title") or "")
    title_len_ok = 12 <= len(title.strip()) <= 120
    has_brand = _nonempty(product.get("brand"))
    has_category = _nonempty(product.get("product_type")) or _nonempty(product.get("category"))
    disambig_signals = sum([bool(title.strip()), has_brand, has_category, title_len_ok])
    if title.strip() and has_brand and has_category and title_len_ok:
        disambig_points = 15
        disambig_reason = "title, brand, and category are disambiguated"
        disambig_missing = None
    elif disambig_signals >= 2:
        disambig_points = 8
        disambig_reason = "partial title/brand/category disambiguation"
        disambig_missing = [
            field for field, ok in (
                ("catalog_products.title", bool(title.strip()) and title_len_ok),
                ("catalog_products.brand", has_brand),
                ("catalog_products.product_type_or_category", has_category),
            )
            if not ok
        ]
    else:
        disambig_points = 0
        disambig_reason = "data unavailable"
        disambig_missing = ["catalog_products.title", "catalog_products.brand", "catalog_products.product_type_or_category"]
    _add_bucket(breakdown, missing, "title_brand_category", disambig_points, 15, disambig_reason, missing=disambig_missing)

    peers_key_present = "content_key_peers" in sku_ctx or "collision_audit" in sku_ctx
    peers = [
        row for row in _json_list(sku_ctx.get("content_key_peers") or sku_ctx.get("collision_audit"))
        if isinstance(row, dict)
    ]
    if not _nonempty(content_key):
        collision_points = 0
        collision_reason = "data unavailable"
        collision_missing = ["catalog_products.content_key"]
    elif not peers_key_present:
        collision_points = 0
        collision_reason = "data unavailable"
        collision_missing = ["catalog_products rows sharing content_key"]
    else:
        base_brand = _norm_identity_text(product.get("brand"))
        base_title = _norm_identity_text(product.get("title"))
        base_group = state.get("product_group_id") or (group_members[0].get("product_group_id") if group_members else None)
        divergent = []
        for peer in peers:
            if peer.get("product_key") == product.get("product_key"):
                continue
            peer_group = peer.get("product_group_id")
            intentionally_grouped = _nonempty(base_group) and base_group == peer_group
            peer_brand = _norm_identity_text(peer.get("brand"))
            peer_title = _norm_identity_text(peer.get("title"))
            agrees = (not peer_brand or peer_brand == base_brand) and (not peer_title or peer_title == base_title)
            if not intentionally_grouped and not agrees:
                divergent.append(peer.get("product_key") or peer.get("title") or "unknown")
        collision_points = 0 if divergent else 15
        collision_reason = "divergent content_key collision" if divergent else "no divergent content_key collisions"
        collision_missing = None
    _add_bucket(
        breakdown, missing, "collision_audit",
        collision_points, 15, collision_reason,
        missing=collision_missing,
    )
    return _finish_breakdown(breakdown, missing)


def _raw_pdp_content_fraction(
    product: Dict[str, Any],
    sku_ctx: Dict[str, Any],
    enrichment: Dict[str, Any],
) -> float:
    """Honest 0..1 measure of the REAL merchant PDP's content richness.

    Reads only first-party listing fields (description, image, structured
    facts/specs, bullets, price) — NOT Pivota enrichment artifacts. Used as the
    fallback when product_quality_snapshot is absent (fresh ingests), so a
    content-rich but not-yet-enriched PDP isn't scored as "thin" and pushed a
    "build a PDP you already have" recommendation. The real gap for these SKUs
    is Pivota enrichment / getting cited, which the `missing` field still flags.
    """
    payload = _json_obj(product.get("product_payload"))
    fraction = 0.0

    description = str(product.get("description") or enrichment.get("description_markdown") or "").strip()
    desc_len = len(description)
    if desc_len >= 600:
        fraction += 0.30
    elif desc_len >= 300:
        fraction += 0.20
    elif desc_len >= 120:
        fraction += 0.10

    if _nonempty(product.get("title")):
        fraction += 0.10

    if _nonempty(product.get("image_url") or sku_ctx.get("image_url")):
        fraction += 0.15

    sku = _get_sku(sku_ctx or {})
    has_structured = (
        _nonempty(payload.get("facts") or payload.get("structured_facts") or payload.get("specs")
                  or payload.get("ingredients") or payload.get("fashion_meta") or payload.get("electronics_meta"))
        or _nonempty(_json_obj(sku.get("visible_attributes")))
        or any(isinstance(r, dict) for r in _json_list(sku_ctx.get("catalog_field_facts")))
    )
    if has_structured:
        fraction += 0.20

    bullets = _json_list(product.get("bullet_points") or payload.get("bullet_points"))
    if any(_nonempty(b) for b in bullets) or _nonempty(payload.get("usage") or product.get("usage_scenarios")):
        fraction += 0.10

    offers = _get_offers(sku_ctx or {})
    has_price = any(
        _as_number(o.get("merchant_effective_price")) is not None
        or _as_number(o.get("estimated_best_price")) is not None
        for o in offers
    )
    if has_price:
        fraction += 0.15

    return min(1.0, fraction)


def _raw_vertical_attribute_signals(product: Dict[str, Any]) -> List[str]:
    """Category-specific detail signals readable straight off the fetched PDP
    (attributes_raw + description), for SKUs whose curated vertical artifacts
    (electronics_meta / beauty tables / fashion fields) don't exist yet — every
    URL-wedge product, and any fresh connected ingest before enrichment runs.

    Weak-but-honest proxies: spec-ish tags, a deep description, variant/option
    structure, and structured page metadata. They can't prove curated vertical
    meta (pro_reviews, in_box, INCI...), which is why the fallback bucket is
    capped below the enriched ceiling."""
    attrs = _json_obj(product.get("attributes_raw"))
    signals: List[str] = []
    tags = [str(t).strip() for t in _json_list(attrs.get("tags")) if str(t).strip()]
    if len(tags) >= 3:
        signals.append("spec_tags")
    description = str(
        product.get("description") or attrs.get("description") or ""
    ).strip()
    if len(description) >= 400:
        signals.append("deep_description")
    if _nonempty(attrs.get("variants")) or _nonempty(attrs.get("options")):
        signals.append("variant_structure")
    if _nonempty(attrs.get("offers")) or _nonempty(attrs.get("aggregateRating")) or (
        _nonempty(attrs.get("brand")) and _nonempty(attrs.get("category") or attrs.get("product_type"))
    ):
        signals.append("structured_metadata")
    return signals


def compute_content_richness_score(sku_ctx: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Spec A.2 content-richness score. Pure: reads normalized SKU context."""
    product = _get_product(sku_ctx or {})
    sku = _get_sku(sku_ctx or {})
    enrichment = _get_enrichment(sku_ctx or {})
    quality = _get_quality(sku_ctx or {})
    breakdown: Dict[str, Any] = {}
    missing: List[str] = []

    # Raw-PDP fallback: product_quality_score and model_readiness historically
    # came ONLY from Pivota's product_quality_snapshot, scoring 0 for any fresh
    # ingest — so a real brand PDP with a 1200-char description scored ~18/100
    # and got a "build a PDP" recommendation for a page it already has. When the
    # Pivota snapshot is absent, score the merchant's RAW content directly; keep
    # `missing` pointing at the enrichment artifact so the recommendation still
    # targets the real gap (get Pivota-enriched / cited), not "publish a PDP."
    raw_pdp_fraction = _raw_pdp_content_fraction(product, sku_ctx or {}, enrichment)

    quality_value = quality.get("content_quality_score", sku_ctx.get("content_quality_score"))
    if quality_value is not None:
        quality_points = _points_from_percent(quality_value, 25)
        quality_reason = f"content quality normalized to {quality_points}/25"
        quality_missing = None
    else:
        quality_points = int(round(25 * raw_pdp_fraction))
        quality_reason = (
            f"raw PDP content scored {quality_points}/25 (Pivota enrichment score pending)"
            if quality_points
            else "data unavailable"
        )
        # Even when raw content is rich, the enrichment artifact is the real gap.
        quality_missing = ["product_quality_snapshot.content_quality_score"]
    _add_bucket(
        breakdown, missing, "product_quality_score",
        quality_points, 25,
        quality_reason,
        missing=quality_missing,
    )

    bullets = _json_list(enrichment.get("bullet_points") or product.get("bullet_points"))
    coverage_checks = [
        ("summary_short", _nonempty(enrichment.get("summary_short"))),
        ("bullet_points", len([b for b in bullets if _nonempty(b)]) >= 3),
        ("usage_scenarios", _nonempty(enrichment.get("usage_scenarios") or product.get("usage_scenarios"))),
        ("audience_tags", _nonempty(enrichment.get("audience_tags") or product.get("audience_tags"))),
    ]
    coverage_points = 5 * sum(1 for _, ok in coverage_checks if ok)
    _add_bucket(
        breakdown, missing, "enrichment_coverage",
        coverage_points, 20,
        f"{coverage_points // 5}/4 enrichment elements present",
        missing=[f"product_enrichment.{name}" for name, ok in coverage_checks if not ok] or None,
    )

    vertical = _vertical_for(product)
    payload = _json_obj(product.get("product_payload"))
    field_facts = [
        row for row in _json_list(sku_ctx.get("catalog_field_facts"))
        if isinstance(row, dict)
    ]
    if vertical == "beauty":
        has_ingredients = _nonempty(sku_ctx.get("beauty_sku_ingredients") or _json_list(sku.get("ingredient_ids")) or payload.get("ingredients"))
        has_usage = _nonempty(sku_ctx.get("beauty_usage_guides") or payload.get("usage") or enrichment.get("usage_scenarios"))
        has_compat = _nonempty(sku_ctx.get("beauty_compatibility_rules") or payload.get("watchouts") or payload.get("compatibility"))
        vertical_points = (7 if has_ingredients else 0) + (7 if has_usage else 0) + (6 if has_compat else 0)
        vertical_missing = []
        if not has_ingredients:
            vertical_missing.append("beauty_sku_ingredients")
        if not has_usage:
            vertical_missing.append("beauty_usage_guides")
        if not has_compat:
            vertical_missing.append("beauty_compatibility_rules")
    elif vertical == "fashion":
        fashion = _json_obj(payload.get("fashion_meta") or {})
        has_material = _nonempty(product.get("material") or fashion.get("material")) and _confidence_ok(product.get("material_confidence") or fashion.get("material_confidence"))
        has_care = _nonempty(product.get("care") or fashion.get("care")) and _confidence_ok(product.get("care_confidence") or fashion.get("care_confidence"))
        has_size = _nonempty(product.get("size_guide") or fashion.get("size_guide")) and _confidence_ok(product.get("size_guide_confidence") or fashion.get("size_guide_confidence"))
        vertical_points = (7 if has_material else 0) + (7 if has_care else 0) + (6 if has_size else 0)
        vertical_missing = []
        if not has_material:
            vertical_missing.append("catalog_products.material")
        if not has_care:
            vertical_missing.append("catalog_products.care")
        if not has_size:
            vertical_missing.append("catalog_products.size_guide")
    elif vertical == "electronics":
        electronics = _json_obj(payload.get("electronics_meta") or {})
        checks = [
            ("spec_groups", _nonempty(electronics.get("spec_groups")), 6),
            ("in_box", _nonempty(electronics.get("in_box")), 4),
            ("pro_reviews", _nonempty(electronics.get("pro_reviews")), 4),
            ("compare_or_configurator", _nonempty(electronics.get("compare_with") or electronics.get("configurator_groups") or electronics.get("protection_plans")), 6),
        ]
        vertical_points = sum(points for _, ok, points in checks if ok)
        vertical_missing = [f"electronics_meta.{name}" for name, ok, _ in checks if not ok]
    else:
        reviewed_facts = [
            row for row in field_facts
            if str(row.get("review_state") or "").lower() in {"reviewed", "approved", "verified"}
        ]
        has_payload_facts = _nonempty(payload.get("facts") or payload.get("structured_facts") or payload.get("product_intel"))
        vertical_points = (10 if has_payload_facts else 0) + (10 if reviewed_facts else 0)
        vertical_missing = []
        if not has_payload_facts:
            vertical_missing.append("catalog_products.product_payload.facts")
        if not reviewed_facts:
            vertical_missing.append("catalog_field_facts.reviewed")
    # Raw-PDP fallback, same contract as product_quality_score/model_readiness
    # above: the curated vertical artifacts don't exist for URL-wedge SKUs (or
    # fresh ingests), so this bucket scored a flat 0/20 and the top action told
    # a merchant with a spec-rich page it was "too thin" (live on the Mojawa
    # pilot, content 39 with a rich PDP). When the artifacts are absent but the
    # fetched page itself carries category detail, score that — capped at 16/20
    # because raw signals can't prove curated vertical meta — and keep `missing`
    # on the enrichment artifacts so the recommendation still targets the real
    # gap (get Pivota-enriched), not "publish a page you already have".
    vertical_reason = f"{vertical} structure coverage" if vertical_points else "data unavailable"
    if not vertical_points:
        raw_signals = _raw_vertical_attribute_signals(product)
        if raw_signals:
            vertical_points = min(16, 4 * len(raw_signals))
            vertical_reason = (
                f"raw PDP category signals {len(raw_signals)}/4 "
                "(Pivota vertical enrichment pending)"
            )
    _add_bucket(
        breakdown, missing, "vertical_structure",
        vertical_points, 20,
        vertical_reason,
        missing=vertical_missing or None,
        extra={"vertical": vertical},
    )

    readiness_value = quality.get("model_readiness_score", sku_ctx.get("model_readiness_score"))
    if readiness_value is not None:
        readiness_points = _points_from_percent(readiness_value, 15)
        readiness_reason = f"model readiness normalized to {readiness_points}/15"
        readiness_missing = None
    else:
        # Same raw-PDP fallback as product_quality_score: a content-complete
        # listing carries the fields a model needs even before Pivota computes a
        # readiness score. Keep `missing` flagging the Pivota artifact.
        readiness_points = int(round(15 * raw_pdp_fraction))
        readiness_reason = (
            f"raw PDP model-readiness proxy {readiness_points}/15 (Pivota readiness score pending)"
            if readiness_points
            else "data unavailable"
        )
        readiness_missing = ["product_quality_snapshot.model_readiness_score"]
    _add_bucket(
        breakdown, missing, "model_readiness",
        readiness_points, 15,
        readiness_reason,
        missing=readiness_missing,
    )

    blocking_flags = _has_blocking_safety_flag(enrichment.get("llm_safety_flags"))
    claims_present = _has_claims(product, sku_ctx or {})
    substantiated = _has_substantiation(product, sku_ctx or {})
    if blocking_flags:
        safety_points = 0
        safety_reason = "blocking safety flag present"
        safety_missing = None
    elif claims_present and not substantiated:
        safety_points = 5
        safety_reason = "claims present without substantiation/watchouts"
        safety_missing = ["claim_substantiation_or_watchouts"]
    else:
        safety_points = 10
        safety_reason = "no blocking safety flags; claims substantiated or absent"
        safety_missing = None
    _add_bucket(breakdown, missing, "safety_claims", safety_points, 10, safety_reason, missing=safety_missing)
    # NON-SCORING evidence signal (points unchanged): annotate the safety_claims
    # bucket with how much citable evidence the product carries, recording third-party
    # backing depth in the stored breakdown for agent/analytics consumers without
    # inflating the 100-pt scale. NOTE: this lives inside `breakdown`, which
    # _strip_score_breakdowns removes from the MERCHANT-facing payload — merchants see
    # evidence depth in the intake panel instead; this is the stored/agent surface.
    ev_count = int(sku_ctx.get("substantiated_evidence_count") or 0)
    tp_sources = int(sku_ctx.get("third_party_evidence_sources") or 0)
    if ev_count:
        breakdown["safety_claims"]["evidence_signal"] = {
            "substantiated_claims": ev_count,
            "third_party_sources": tp_sources,
        }
        if tp_sources:
            breakdown["safety_claims"]["reason"] = (
                f"{breakdown['safety_claims']['reason']}; backed by {tp_sources} "
                f"third-party source{'s' if tp_sources != 1 else ''}"
            )

    description = str(product.get("description") or enrichment.get("description_markdown") or "")
    has_description = len(description.strip()) >= 120
    has_image = _nonempty(product.get("image_url") or sku_ctx.get("image_url"))
    has_freshness = _freshness_current(product.get("freshness_json"))
    readiness_tier = str(product.get("readiness_tier") or "").strip()
    readiness_ok = readiness_tier in {"knowledge_ready", "vertical_ready", "commerce_ready"}
    freshness_points = (3 if has_description else 0) + (3 if has_image else 0) + (2 if has_freshness else 0) + (2 if readiness_ok else 0)
    freshness_missing = []
    if not has_description:
        freshness_missing.append("catalog_products.description")
    if not has_image:
        freshness_missing.append("catalog_products.image_url")
    if not has_freshness:
        freshness_missing.append("catalog_products.freshness_json")
    if not readiness_ok:
        freshness_missing.append("catalog_products.readiness_tier")
    _add_bucket(
        breakdown, missing, "freshness_raw_pdp",
        freshness_points, 10,
        "raw PDP completeness and freshness signals" if freshness_points else "data unavailable",
        missing=freshness_missing or None,
    )
    return _finish_breakdown(breakdown, missing)


def compute_routability_score(sku_ctx: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Spec A.3 routability/transactability score."""
    product = _get_product(sku_ctx or {})
    sku = _get_sku(sku_ctx or {})
    state = _get_index_state(sku_ctx or {})
    offers = _get_offers(sku_ctx or {})
    breakdown: Dict[str, Any] = {}
    missing: List[str] = []

    if state.get("serving_eligible") is True:
        serving_points = 30
        serving_reason = "serving eligible"
        serving_missing = None
    else:
        stage = str(state.get("pipeline_stage") or product.get("pdp_lifecycle_stage") or "").strip()
        stage_points = {
            "public_indexed": 25,
            "shadow_indexed": 22,
            "quality_gated": 15,
            "extracted": 10,
            "crawled": 5,
            "discovered": 0,
        }
        serving_points = stage_points.get(stage, 0)
        serving_reason = f"partial by pipeline stage {stage}" if stage else "data unavailable"
        serving_missing = None if stage else ["index_pipeline_state.serving_eligible", "index_pipeline_state.pipeline_stage"]
    _add_bucket(breakdown, missing, "serving_eligibility", serving_points, 30, serving_reason, missing=serving_missing)

    if not offers:
        order_points = 0
        order_reason = "data unavailable"
        order_missing = ["catalog_offers"]
    else:
        best = 0
        for offer in offers:
            availability_ok = str(offer.get("availability") or "").lower() in _EXPLICIT_AVAILABLE_STATES
            inventory = offer.get("inventory_quantity")
            inventory_ok = inventory is None or (_as_number(inventory) or 0) > 0
            mode_ok = (offer.get("offer_mode") or "") == "merchant_checkout"
            price_ok = (_as_number(offer.get("list_price")) or 0) > 0
            linked_ok = _nonempty(offer.get("offer_id")) and offer.get("sku_key") == sku.get("sku_key")
            best = max(
                best,
                (5 if linked_ok else 0)
                + (8 if availability_ok and inventory_ok else 0)
                + (7 if mode_ok else 0)
                + (5 if price_ok else 0),
            )
        order_points = best
        order_reason = "orderable merchant-checkout offer" if best == 25 else "partial offer orderability"
        order_missing = None if best == 25 else ["catalog_offers.availability", "catalog_offers.inventory_quantity", "catalog_offers.offer_mode", "catalog_offers.list_price"]
    _add_bucket(breakdown, missing, "offer_orderability", order_points, 25, order_reason, missing=order_missing, extra={"offer_count": len(offers)})

    if not offers:
        price_points = 0
        price_reason = "data unavailable"
        price_missing = ["catalog_offers"]
    else:
        best_price_points = 0
        for offer in offers:
            has_currency = _nonempty(offer.get("currency"))
            has_price = _as_number(offer.get("merchant_effective_price")) is not None or _as_number(offer.get("estimated_best_price")) is not None
            confidence = _as_number(offer.get("price_confidence"))
            confidence_ok = confidence is not None and confidence >= 0.8
            best_price_points = max(best_price_points, (5 if has_currency else 0) + (5 if has_price else 0) + (5 if confidence_ok else 0))
        price_points = best_price_points
        price_reason = "price, currency, and confidence present" if price_points == 15 else "partial price/currency confidence"
        price_missing = None if price_points == 15 else ["catalog_offers.currency", "catalog_offers.merchant_effective_price", "catalog_offers.price_confidence"]
    _add_bucket(breakdown, missing, "price_currency_confidence", price_points, 15, price_reason, missing=price_missing)

    commerce = sku_ctx.get("merchant_commerce_readiness_state") or sku_ctx.get("commerce_readiness") or {}
    merchant = sku_ctx.get("merchant") or {}
    offer_truth_ok = any((o.get("truth_tier") or "") == "primary" for o in offers) if offers else False
    product_truth_ok = (product.get("truth_tier") or "") == "primary"
    verification = str(merchant.get("verification_status") or commerce.get("verification_status") or "").lower()
    merchant_ok = verification in {"verified", "active"} or bool(commerce.get("active_psp"))
    sync_ok = str(product.get("sync_status") or "live").lower() not in {"stale", "archived", "blocked"}
    trust_points = (3 if product_truth_ok else 0) + (3 if offer_truth_ok else 0) + (2 if merchant_ok else 0) + (2 if sync_ok else 0)
    trust_missing = []
    if not product_truth_ok:
        trust_missing.append("catalog_products.truth_tier")
    if not offer_truth_ok:
        trust_missing.append("catalog_offers.truth_tier")
    if not merchant_ok:
        trust_missing.append("merchants.verification_status")
    if not sync_ok:
        trust_missing.append("catalog_products.sync_status")
    _add_bucket(
        breakdown, missing, "merchant_trust_state",
        trust_points, 10,
        "primary trust tier and merchant state ready" if trust_points == 10 else "partial merchant/trust state",
        missing=trust_missing or None,
    )

    policies = [p for p in _json_list(sku_ctx.get("pcs_shop_policies") or sku_ctx.get("policies")) if isinstance(p, dict)]
    policy_types = {str(p.get("policy_type") or "").lower() for p in policies}
    offer_payloads = [_json_obj(o.get("offer_payload")) for o in offers]
    merchant_payload = _json_obj(merchant.get("metadata_json") or merchant.get("payload") or {})
    country = merchant.get("country") or merchant_payload.get("country")
    has_shipping = "shipping" in policy_types or any(_nonempty(p.get("shipping")) for p in offer_payloads) or _nonempty(merchant_payload.get("shipping_policy"))
    has_refund = "refund" in policy_types or "returns" in policy_types or _nonempty(merchant_payload.get("refund_policy"))
    has_terms = "terms" in policy_types or _nonempty(merchant_payload.get("terms_url"))
    ship_market = any(_nonempty(p.get("ship_to_market") or p.get("ship_to_countries") or p.get("markets")) for p in offer_payloads)
    jurisdiction_points = (2 if _nonempty(country) else 0) + (2 if has_shipping else 0) + (2 if has_refund else 0) + (2 if has_terms else 0) + (2 if ship_market or _nonempty(country) else 0)
    jurisdiction_missing = []
    if not _nonempty(country):
        jurisdiction_missing.append("merchants.country")
    if not has_shipping:
        jurisdiction_missing.append("pcs_shop_policies.shipping")
    if not has_refund:
        jurisdiction_missing.append("pcs_shop_policies.refund")
    if not has_terms:
        jurisdiction_missing.append("pcs_shop_policies.terms")
    if not (ship_market or _nonempty(country)):
        jurisdiction_missing.append("catalog_offers.offer_payload.ship_to_market")
    _add_bucket(
        breakdown, missing, "policy_jurisdiction",
        jurisdiction_points, 10,
        "policy and jurisdiction signals present" if jurisdiction_points == 10 else "partial policy/jurisdiction readiness",
        missing=jurisdiction_missing or None,
    )

    linked_offer = any(o.get("sku_key") == sku.get("sku_key") and _nonempty(o.get("offer_id")) for o in offers)
    visible_labels = _json_list(sku.get("visible_option_labels"))
    visible_attrs = _json_obj(sku.get("visible_attributes"))
    required_options = _json_list(sku_ctx.get("required_visible_options") or sku.get("required_visible_options"))
    options_ok = True
    if required_options:
        available = {str(x).lower() for x in visible_labels}
        available.update(str(k).lower() for k, v in visible_attrs.items() if _nonempty(v))
        options_ok = all(str(opt).lower() in available for opt in required_options)
    elif not visible_labels and not visible_attrs and len(_get_all_skus(sku_ctx or {})) > 1:
        options_ok = False
    route_points = (6 if linked_offer else 0) + (4 if options_ok else 0)
    route_missing = []
    if not linked_offer:
        route_missing.append("catalog_offers.sku_key")
    if not options_ok:
        route_missing.append("catalog_skus.visible_option_labels")
    _add_bucket(
        breakdown, missing, "variant_route_integrity",
        route_points, 10,
        "selected SKU maps to a specific offer and options are complete" if route_points == 10 else "partial variant route integrity",
        missing=route_missing or None,
    )
    return _finish_breakdown(breakdown, missing)


def _orderable_offer_summary(sku_ctx: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    sku = _get_sku(sku_ctx or {})
    offers = _get_offers(sku_ctx or {})
    best_points = -1
    best_offer: Optional[Dict[str, Any]] = None
    for offer in offers:
        availability_ok = str(offer.get("availability") or "").lower() in _EXPLICIT_AVAILABLE_STATES
        inventory = offer.get("inventory_quantity")
        inventory_ok = inventory is None or (_as_number(inventory) or 0) > 0
        mode_ok = (offer.get("offer_mode") or "") == "merchant_checkout"
        price_ok = (_as_number(offer.get("list_price")) or 0) > 0
        linked_ok = _nonempty(offer.get("offer_id")) and offer.get("sku_key") == sku.get("sku_key")
        points = (
            (5 if linked_ok else 0)
            + (8 if availability_ok and inventory_ok else 0)
            + (7 if mode_ok else 0)
            + (5 if price_ok else 0)
        )
        if points > best_points:
            best_points = points
            best_offer = offer

    if best_offer is None:
        return False, {
            "offer_count": 0,
            "reason": "no catalog offer found",
            "missing_inputs": ["catalog_offers"],
        }

    orderable = best_points == 25
    missing: List[str] = []
    if not (_nonempty(best_offer.get("offer_id")) and best_offer.get("sku_key") == sku.get("sku_key")):
        missing.append("catalog_offers.sku_key")
    availability_ok = str(best_offer.get("availability") or "").lower() in _EXPLICIT_AVAILABLE_STATES
    inventory = best_offer.get("inventory_quantity")
    inventory_ok = inventory is None or (_as_number(inventory) or 0) > 0
    if not (availability_ok and inventory_ok):
        missing.append("catalog_offers.availability")
    if (best_offer.get("offer_mode") or "") != "merchant_checkout":
        missing.append("catalog_offers.offer_mode")
    if (_as_number(best_offer.get("list_price")) or 0) <= 0:
        missing.append("catalog_offers.list_price")

    return orderable, {
        "offer_count": len(offers),
        "offer_id": best_offer.get("offer_id"),
        "offer_mode": best_offer.get("offer_mode"),
        "availability": best_offer.get("availability"),
        "inventory_quantity": best_offer.get("inventory_quantity"),
        "currency": best_offer.get("currency"),
        "list_price": best_offer.get("list_price"),
        "points": max(0, best_points),
        "max": 25,
        "reason": "orderable merchant-checkout offer" if orderable else "partial offer orderability",
        "missing_inputs": missing,
    }


def build_sku_deliverability_prediction(
    sku_ctx: Dict[str, Any],
    scores: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Predict whether this SKU can be served and transacted from stored facts.

    This is deliberately stricter than the routability score. A high
    routability score can include partial credit, but this prediction only
    calls a SKU transactable when serving eligibility, offer orderability, and
    merchant execute readiness are all explicit.
    """
    product = _get_product(sku_ctx or {})
    state = _get_index_state(sku_ctx or {})
    commerce = sku_ctx.get("merchant_commerce_readiness_state") or sku_ctx.get("commerce_readiness") or {}
    commerce = commerce if isinstance(commerce, dict) else {}

    serving_eligible = state.get("serving_eligible")
    pipeline_stage = str(state.get("pipeline_stage") or product.get("pdp_lifecycle_stage") or "").strip() or None
    serving_ready = serving_eligible is True
    serving_known = serving_eligible is not None or pipeline_stage is not None
    if serving_ready:
        serving_status = "ready"
        serving_reason = "index pipeline marks the SKU serving eligible"
        serving_missing: List[str] = []
    elif serving_known:
        serving_status = "blocked"
        serving_reason = "index pipeline does not mark the SKU serving eligible"
        serving_missing = ["index_pipeline_state.serving_eligible"]
    else:
        serving_status = "unknown"
        serving_reason = "serving eligibility has not been measured"
        serving_missing = ["index_pipeline_state.serving_eligible", "index_pipeline_state.pipeline_stage"]

    orderable_offer, offer_summary = _orderable_offer_summary(sku_ctx or {})
    execute_status = str(commerce.get("execute_status") or "").strip().lower() or None
    execute_ready = execute_status == "ready"
    platform = (
        str(
            commerce.get("primary_platform")
            or product.get("platform")
            or product.get("source_platform")
            or ""
        )
        .strip()
        .lower()
        or None
    )
    policy = resolve_commerce_execution_policy(
        platform=platform,
        surface=SURFACE_PUBLIC_AGENT_PURCHASE,
    ).as_dict()
    commerce_blockers = [
        str(item)
        for item in _json_list(commerce.get("execute_blockers"))
        if str(item or "").strip()
    ]

    checkout_missing: List[str] = []
    checkout_missing.extend(offer_summary.get("missing_inputs") or [])
    if execute_status is None:
        checkout_missing.append("merchant_commerce_readiness_state.execute_status")
    if not platform:
        checkout_missing.append("merchant_commerce_readiness_state.primary_platform")
    checkout_blockers: List[str] = []
    checkout_blockers.extend(commerce_blockers)
    if execute_status and not execute_ready:
        checkout_blockers.append("merchant_commerce_readiness_state.execute_status")
    if not policy.get("allows_pivota_order"):
        checkout_blockers.append("commerce_execution_policy.allows_pivota_order=false")

    direct_purchase_ready = (
        orderable_offer
        and execute_ready
        and bool(policy.get("allows_pivota_order"))
        and bool(policy.get("allows_psp_creation"))
    )

    if orderable_offer and execute_ready and policy.get("allows_pivota_order"):
        checkout_status = "ready"
        checkout_reason = "orderable offer and merchant execute readiness are present"
    elif orderable_offer and execute_ready:
        checkout_status = "limited"
        checkout_reason = "merchant is commerce-ready, but this platform is not enabled for Pivota direct purchase"
    elif orderable_offer:
        checkout_status = "blocked"
        checkout_reason = "orderable offer exists, but merchant execute readiness is not ready"
    elif execute_ready:
        checkout_status = "blocked"
        checkout_reason = "merchant execute readiness is ready, but no orderable SKU offer is present"
    elif execute_status is None and not orderable_offer:
        checkout_status = "unknown"
        checkout_reason = "checkout readiness has not been measured"
    else:
        checkout_status = "blocked"
        checkout_reason = "checkout readiness is blocked"

    if not serving_known and execute_status is None and not offer_summary.get("offer_count"):
        status = "not_measured"
        summary = "No stored serving, offer, or checkout facts are available for this SKU yet."
    elif not serving_ready:
        status = "not_publishable"
        if serving_status == "unknown":
            summary = "This SKU should not be promised to buyers yet because serving eligibility is not confirmed."
        else:
            summary = "This SKU should not be promised to buyers yet because it is not serving eligible."
    elif direct_purchase_ready:
        status = "transactable"
        summary = "This SKU is serving eligible and has a ready merchant-checkout path for Pivota direct purchase."
    elif orderable_offer and execute_ready:
        status = "servable_not_direct_purchase"
        summary = "This SKU is servable with an orderable offer, but Pivota direct purchase is not enabled for the platform."
    else:
        status = "servable_not_transactable"
        summary = "This SKU can be served, but checkout is not ready enough to promise a transaction."

    routability = (scores or {}).get("routability") if isinstance(scores, dict) else None
    routability_score = routability.get("score") if isinstance(routability, dict) else None

    return {
        "status": status,
        "summary": summary,
        "serving": {
            "status": serving_status,
            "serving_eligible": serving_eligible if isinstance(serving_eligible, bool) else None,
            "pipeline_stage": pipeline_stage,
            "reason": serving_reason,
            "missing_inputs": serving_missing,
        },
        "checkout": {
            "status": checkout_status,
            "reason": checkout_reason,
            "orderable_offer": orderable_offer,
            "offer": offer_summary,
            "execute_status": execute_status,
            "active_psp": commerce.get("active_psp"),
            "primary_platform": platform,
            "execute_blockers": commerce_blockers,
            "commerce_path": policy.get("commerce_path"),
            "allows_pivota_order": bool(policy.get("allows_pivota_order")),
            "allows_psp_creation": bool(policy.get("allows_psp_creation")),
            "validation_authority": policy.get("validation_authority"),
            "execution_policy_version": policy.get("execution_policy_version"),
            "policy_reason": policy.get("reason"),
            "missing_inputs": list(dict.fromkeys(checkout_missing)),
            "blockers": list(dict.fromkeys(checkout_blockers)),
        },
        "score_context": {"routability_score": routability_score},
        "honesty_note": (
            "Prediction uses stored serving eligibility, catalog offer orderability, "
            "merchant commerce readiness, and the public agent purchase execution policy; "
            "citation score alone never makes a SKU transactable."
        ),
    }


def _flatten_probe_runs(per_sku_probe_runs: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for probe in _json_list(per_sku_probe_runs):
        if not isinstance(probe, dict):
            continue
        raw_runs = probe.get("raw_runs")
        if isinstance(raw_runs, list):
            probe_run_id = (
                probe.get("probe_run_id")
                or probe.get("run_id")
                or probe.get("id")
                or probe.get("scan_target_id")
            )
            for idx, run in enumerate(raw_runs):
                if not isinstance(run, dict):
                    continue
                row = dict(run)
                row.setdefault("_provider", probe.get("provider"))
                row.setdefault("_probe_run_id", probe_run_id or f"{probe.get('provider') or 'probe'}:{idx}")
                out.append(row)
        elif "query" in probe:
            row = dict(probe)
            row.setdefault("_probe_run_id", probe.get("probe_run_id") or probe.get("run_id") or probe.get("id"))
            out.append(row)
        else:
            for nested in probe.values():
                out.extend(_flatten_probe_runs(nested))
    return out


def _copy_provider_model_metadata(
    provider_model_metadata: Optional[Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for provider, payload in (provider_model_metadata or {}).items():
        provider_id = str(provider or "").strip().lower()
        if not provider_id or not isinstance(payload, Mapping):
            continue
        model = str(payload.get("model") or "").strip()
        if not model:
            continue
        item: Dict[str, Any] = {
            "model": model,
            "model_is_override": bool(payload.get("model_is_override")),
        }
        default_model = str(payload.get("default_model") or "").strip()
        if default_model:
            item["default_model"] = default_model
        out[provider_id] = item
    return out


def _probe_run_provider_model_metadata(
    probe_runs: Any,
    *,
    fallback: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    out = _copy_provider_model_metadata(fallback)
    for probe in _json_list(probe_runs):
        if not isinstance(probe, dict):
            continue
        provider = str(probe.get("provider") or "").strip().lower()
        model = str(probe.get("model") or probe.get("llm_model") or "").strip()
        if not provider or not model:
            continue
        item: Dict[str, Any] = {
            "model": model,
            "model_is_override": bool(probe.get("model_is_override")),
        }
        default_model = str(probe.get("default_model") or "").strip()
        if default_model:
            item["default_model"] = default_model
        out[provider] = item
    return out


def _any_model_override(provider_models: Mapping[str, Any]) -> bool:
    return any(
        bool(payload.get("model_is_override"))
        for payload in provider_models.values()
        if isinstance(payload, Mapping)
    )


def _source_urls(run: Dict[str, Any]) -> List[str]:
    # Redirector-aware. Vertex AI grounding wraps every cited URL in a
    # vertexaisearch.cloud.google.com/grounding-api-redirect/... URI and puts
    # the REAL publisher domain in the source `title` ("ownist.com"). Matching
    # first-party / authority against the raw redirector URI never sees the real
    # domain — so first_party_rate scored 0 even when the merchant's own site
    # was a grounding source. _identify_run_sources already resolves this
    # (title for redirectors, host otherwise); reuse it so the merchant domain
    # is matchable. Both callers (_url_in_sources, the scorer's source_hosts)
    # want the real source domain, not the opaque redirector.
    return [src["key"] for src in _identify_run_sources(run) if src.get("key")]


def _url_in_sources(run: Dict[str, Any], targets: List[str]) -> bool:
    normalized_targets = [t.strip().lower().rstrip("/") for t in targets if isinstance(t, str) and t.strip()]
    if not normalized_targets:
        return False
    for url in _source_urls(run):
        u = url.lower().rstrip("/")
        if any(target in u or u in target for target in normalized_targets):
            return True
    return False


def _source_identifier_is_first_party(
    source: Mapping[str, Any],
    *,
    target_urls: List[str],
    first_party_hosts: set[str],
) -> bool:
    key = str(source.get("key") or "").strip().lower().rstrip("/")
    label = str(source.get("label") or "").strip().lower().rstrip("/")
    if not key and not label:
        return False
    for host in first_party_hosts:
        if host and (host in key or host in label):
            return True
    for target in target_urls:
        normalized_target = target.strip().lower().rstrip("/")
        if normalized_target and (
            normalized_target in key
            or key in normalized_target
            or normalized_target in label
        ):
            return True
    return False


def _first_party_grounding_primary_for_run(
    run: Dict[str, Any],
    sku_ctx: Dict[str, Any],
    product: Dict[str, Any],
) -> bool:
    target_urls = [
        product.get("canonical_url") or sku_ctx.get("canonical_url") or "",
        product.get("pivota_canonical_url") or sku_ctx.get("pivota_canonical_url") or "",
    ]
    first_party_hosts = {
        host
        for host in (
            normalize_host(target_urls[0]),
            normalize_host(target_urls[1]),
        )
        if host
    }
    sources = _identify_run_sources(run)
    if not sources:
        return False
    first_party = sum(
        1
        for source in sources
        if _source_identifier_is_first_party(
            source,
            target_urls=target_urls,
            first_party_hosts=first_party_hosts,
        )
    )
    if first_party <= 0:
        return False
    external = max(0, len(sources) - first_party)
    return first_party >= external


def _run_negative_verdict(run: Dict[str, Any]) -> bool:
    """True when a single run's answer explicitly denies this is the right /
    visible product. Shared by the citation scorer's per-run gating AND the
    any-provider merge, so both agree on what counts as a negative echo."""
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
    return (
        product_visible is False
        or parsed.get("correct_sku") is False
        or llm_report.get("correct_sku") is False
    )


def _run_first_party_credit(
    run: Dict[str, Any],
    sku_ctx: Dict[str, Any],
    product: Dict[str, Any],
) -> bool:
    """The complete per-run "does this prompt earn first-party credit" decision
    for ONE provider's run: the merchant PDP is a primary grounding source AND
    the answer is not an explicit negative. This is exactly what the
    per-provider scorer counts; the any-provider aggregate ORs it across
    providers BEFORE the source lists are unioned, so a non-citing provider's
    external sources can never veto a citing provider's genuine first-party hit
    (the majority test `first_party >= external` over the union did exactly
    that — deflating combined first_party below the per-provider max)."""
    return _first_party_grounding_primary_for_run(run, sku_ctx or {}, product) and not (
        _run_negative_verdict(run)
    )


def _run_text(run: Dict[str, Any]) -> str:
    parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
    return " ".join(
        str(x or "")
        for x in (
            run.get("raw"),
            run.get("evidence_excerpt"),
            parsed.get("evidence_excerpt"),
            parsed.get("evidence_text"),
            parsed.get("answer"),
        )
    )


def _text_mentions_any(text: str, values: List[Any]) -> bool:
    haystack = _norm_text(text)
    for value in values:
        needle = _norm_text(value)
        if needle and len(needle) >= 4 and needle in haystack:
            return True
    return False


def _has_structured_citation_boolean(
    parsed: Dict[str, Any],
    run: Dict[str, Any],
    llm_report: Dict[str, Any],
) -> bool:
    for key in ("product_visible", "correct_sku", "sku_mentioned"):
        if (
            isinstance(parsed.get(key), bool)
            or isinstance(run.get(key), bool)
            or isinstance(llm_report.get(key), bool)
        ):
            return True
    return False


def _citation_text_denies_product(text: str) -> bool:
    haystack = _norm_text(text)
    if not haystack:
        return False
    denial_phrases = (
        "no listing",
        "no listings",
        "cannot find",
        "cant find",
        "could not find",
        "couldnt find",
        "not available",
        "does not match",
        "doesnt match",
        "not the product",
    )
    return any(phrase in haystack for phrase in denial_phrases)


def _first_party_url_candidates(
    sku_ctx: Dict[str, Any],
    product: Dict[str, Any],
) -> List[Any]:
    """Ordered candidate URLs for the merchant's own host. Canonical fields come
    first so the onboarded path is unchanged; the audited/pasted URL fields
    (pdp_url/url/audited_url) follow so the cold-start URL audit — which sets the
    merchant's own URL as pdp_url, NOT canonical_url — still recognizes the
    merchant's own host as first-party. Single source of truth for both
    `_is_first_party_host` (set membership) and `_merchant_host` (primary)."""
    sku_ctx = sku_ctx or {}
    product = product or {}
    return [
        product.get("canonical_url"),
        product.get("pivota_canonical_url"),
        sku_ctx.get("canonical_url"),
        sku_ctx.get("pivota_canonical_url"),
        product.get("pdp_url"),
        product.get("url"),
        sku_ctx.get("pdp_url"),
        sku_ctx.get("url"),
        sku_ctx.get("audited_url"),
    ]


def _first_party_hosts(
    sku_ctx: Dict[str, Any],
    product: Optional[Dict[str, Any]] = None,
) -> set:
    if product is None:
        product = _get_product(sku_ctx or {})
    hosts = set()
    for candidate in _first_party_url_candidates(sku_ctx, product):
        normalized = normalize_host(candidate or "")
        if normalized:
            hosts.add(normalized)
    return hosts


def _is_first_party_host(host: Optional[str], sku_ctx: Dict[str, Any]) -> bool:
    if not host:
        return False
    return host in _first_party_hosts(sku_ctx)


def _answer_quality_positive(run: Dict[str, Any]) -> bool:
    parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
    url_match = run.get("url_match") if isinstance(run.get("url_match"), dict) else {}
    llm_report = (
        url_match.get("llm_self_report")
        if isinstance(url_match.get("llm_self_report"), dict)
        else {}
    )
    correct_sku = (
        parsed.get("correct_sku")
        if parsed.get("correct_sku") is not None
        else llm_report.get("correct_sku")
    )
    product_visible = (
        parsed.get("product_visible")
        if parsed.get("product_visible") is not None
        else llm_report.get("product_visible")
    )
    return bool(
        correct_sku is True
        or (
            correct_sku is not False
            and product_visible is True
            and (run.get("grounding_sources") or run.get("grounding_chunks"))
        )
    )


def _verify_output_flagged(output: Mapping[str, Any]) -> bool:
    """Factual-only flag gate (founder decision 2026-07-16): a verify output
    counts as flagged ONLY when DeepSeek found a factual misstatement about
    the product. The editorial axis (supports_recommendation) is one LLM's
    opinion about another LLM's answer — it duplicated what the intent/
    citation classifiers already measure deterministically and produced a
    provably-wrong merchant-facing flag ("only lists competitors" on an
    answer with none). It stays in the payload as an internal signal (the
    canonical-PDP enrichment executor targets on it) but never flags, never
    de-weights the answer-quality score, and never reaches merchant copy."""
    verdict = output.get("verdict") if isinstance(output.get("verdict"), Mapping) else {}
    return verdict.get("misstates_facts") is True


def _verify_outputs_by_prompt_key(
    verify_outputs: Optional[Any],
) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for item in _json_list(verify_outputs):
        if not isinstance(item, dict):
            continue
        key_raw = item.get("target_prompt_key")
        if isinstance(key_raw, (list, tuple)) and len(key_raw) == 3:
            key = (
                str(key_raw[0] or "").strip(),
                str(key_raw[1] or "").strip(),
                str(key_raw[2] or "").strip().lower(),
            )
        else:
            key = (
                str(item.get("sku_key") or "").strip(),
                str(item.get("axis") or "").strip(),
                str(item.get("query") or "").strip().lower(),
            )
        verdict = item.get("verdict")
        if key[2] and isinstance(verdict, Mapping):
            out[key] = item
    return out


def _extract_verify_verdict(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for run in result.get("raw_runs") or []:
        if not isinstance(run, dict):
            continue
        parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else None
        if not isinstance(parsed, dict):
            continue
        supports = parsed.get("supports_recommendation")
        misstates = parsed.get("misstates_facts")
        if not isinstance(supports, bool) or not isinstance(misstates, bool):
            return None
        note = str(parsed.get("note") or "").strip()
        return {
            "supports_recommendation": supports,
            "misstates_facts": misstates,
            "note": note[:300],
        }
    return None


def _verify_evidence_excerpt(run: Dict[str, Any]) -> str:
    parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
    lines: List[str] = []
    excerpt = (
        run.get("evidence_excerpt")
        or parsed.get("evidence_excerpt")
        or parsed.get("evidence_text")
    )
    if excerpt:
        lines.append(f"excerpt: {str(excerpt)[:1000]}")
    for source in (run.get("grounding_sources") or [])[:5]:
        if not isinstance(source, dict):
            continue
        uri = str(source.get("uri") or "").strip()
        title = str(source.get("title") or "").strip()
        if uri or title:
            lines.append(f"source: {title} {uri}".strip())
    chunks = [
        str(chunk)
        for chunk in (run.get("grounding_chunks") or [])[:5]
        if isinstance(chunk, (str, int, float)) and str(chunk).strip()
    ]
    if chunks:
        lines.append("chunks: " + " | ".join(chunks)[:1000])
    return "\n".join(lines)


def _verify_sample_cap(
    *,
    positives_count: int,
    prompts_per_sku: Optional[int],
    verify_sample: Optional[Mapping[str, Any]],
    observed_prompt_count: int,
) -> int:
    if positives_count <= 0:
        return 0
    sample = verify_sample or {}
    try:
        fraction = float(sample.get("positive_fraction", 0.25))
    except (TypeError, ValueError):
        fraction = 0.25
    fraction = max(0.0, min(1.0, fraction))
    try:
        prompt_base = int(prompts_per_sku or 0)
    except (TypeError, ValueError):
        prompt_base = 0
    if prompt_base <= 0:
        prompt_base = max(int(observed_prompt_count or 0), positives_count)
    fraction_cap = int(math.ceil(prompt_base * fraction))
    if fraction > 0:
        fraction_cap = max(1, fraction_cap)
    max_per_sku_raw = sample.get("max_per_sku")
    if max_per_sku_raw is not None:
        try:
            fraction_cap = min(fraction_cap, max(0, int(max_per_sku_raw)))
        except (TypeError, ValueError):
            pass
    return min(positives_count, max(0, fraction_cap))


def _citation_positive_verify_candidates(
    sku_ctx: Dict[str, Any],
    probe_runs: Any,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    seen = set()
    for run in _flatten_probe_runs(_any_provider_probe_runs(probe_runs, sku_ctx=sku_ctx)):
        if not _answer_quality_positive(run):
            continue
        key = _citation_prompt_key(run)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(run)
    return candidates


def _verify_skipped_summary(
    *,
    reason: str,
    positives_count: int = 0,
    sample_cap: int = 0,
    verify_sample: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    sample = verify_sample or {}
    return {
        "status": "skipped",
        "reason": reason,
        "provider": _ANSWER_QUALITY_VERIFY_PROVIDER,
        "role": "verify",
        "verified": 0,
        "flagged": 0,
        "not_verified": max(0, positives_count),
        "citation_positive_candidates": max(0, positives_count),
        "sample_cap": max(0, sample_cap),
        "sample_fraction": sample.get("positive_fraction", 0.25),
        "flagged_probes": [],
        "deweight_rule": _ANSWER_QUALITY_VERIFY_DEWEIGHT_RULE,
    }


def _resolve_audit_verify_providers(
    coverage: Dict[str, Any],
    caller_verify_providers: Any,
) -> List[str]:
    """Which providers run the answer-quality (DeepSeek) verify pass.

    The explicit / single-provider coverage paths bound the GENERATION providers
    and return verify_providers=[] — but the merchant "Models to run" UI ALWAYS
    sends an explicit provider list, so without a default the verify pass was
    silently disabled on every merchant audit (the "verify on none" symptom;
    DeepSeek was never even asked to run).

    Resolution order: an explicit caller override wins (including an empty list,
    to deliberately disable); else the coverage profile's verify providers; else
    (the explicit-generation-path case) default to the verify-supported set.
    Downstream still degrades honestly — _run_deepseek_verify_pass skips with
    `missing_deepseek_api_key` / `no_citation_positive_probes` when there's no key
    or nothing cited to verify."""
    if caller_verify_providers is not None:
        return [
            str(p or "").strip().lower()
            for p in caller_verify_providers
            if str(p or "").strip()
        ]
    resolved = [
        str(p or "").strip().lower()
        for p in (coverage.get("verify_providers") or [])
        if str(p or "").strip()
    ]
    return resolved or list(verify_supported_providers())


async def _run_deepseek_verify_pass(
    *,
    sku_ctx: Dict[str, Any],
    probe_runs: Any,
    merchant_id: str,
    audit_run_id: Optional[str],
    verify_providers: Optional[List[str]],
    verify_sample: Optional[Mapping[str, Any]],
    prompts_per_sku: Optional[int],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    providers = [
        str(provider or "").strip().lower()
        for provider in (verify_providers or [])
        if str(provider or "").strip()
    ]
    candidates = _citation_positive_verify_candidates(sku_ctx, probe_runs)
    observed_prompt_count = len(
        _flatten_probe_runs(_any_provider_probe_runs(probe_runs, sku_ctx=sku_ctx))
    )
    sample_cap = _verify_sample_cap(
        positives_count=len(candidates),
        prompts_per_sku=prompts_per_sku,
        verify_sample=verify_sample,
        observed_prompt_count=observed_prompt_count,
    )
    if not providers:
        return (
            _verify_skipped_summary(
                reason="no_verify_providers_resolved",
                positives_count=len(candidates),
                sample_cap=sample_cap,
                verify_sample=verify_sample,
            ),
            [],
        )
    if _ANSWER_QUALITY_VERIFY_PROVIDER not in providers:
        return (
            _verify_skipped_summary(
                reason="deepseek_not_resolved_for_verify",
                positives_count=len(candidates),
                sample_cap=sample_cap,
                verify_sample=verify_sample,
            ),
            [],
        )
    from config.settings import settings as app_settings
    if not (app_settings.deepseek_api_key or "").strip():
        return (
            _verify_skipped_summary(
                reason="missing_deepseek_api_key",
                positives_count=len(candidates),
                sample_cap=sample_cap,
                verify_sample=verify_sample,
            ),
            [],
        )
    if not candidates:
        return (
            _verify_skipped_summary(
                reason="no_citation_positive_probes",
                positives_count=0,
                sample_cap=0,
                verify_sample=verify_sample,
            ),
            [],
        )
    if sample_cap <= 0:
        return (
            _verify_skipped_summary(
                reason="verify_sample_cap_zero",
                positives_count=len(candidates),
                sample_cap=0,
                verify_sample=verify_sample,
            ),
            [],
        )

    product = _get_product(sku_ctx or {})
    sku = _get_sku(sku_ctx or {})
    sku_title = (
        sku.get("title")
        or product.get("title")
        or sku_ctx.get("sku_title")
        or sku_ctx.get("sku_key")
        or "SKU"
    )
    merchant_brand = product.get("brand") or product.get("vendor")
    merchant_url = (
        product.get("canonical_url")
        or product.get("pivota_canonical_url")
        or sku_ctx.get("canonical_url")
        or sku_ctx.get("pivota_canonical_url")
    )

    async def _verify_one(
        idx: int, run: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        query = str(run.get("query") or "").strip()
        prompt_key = _citation_prompt_key(run)
        output_base = {
            "provider": _ANSWER_QUALITY_VERIFY_PROVIDER,
            "role": "verify",
            "scan_mode": _ANSWER_QUALITY_VERIFY_SCAN_MODE,
            "sku_key": sku_ctx.get("sku_key"),
            "target_prompt_key": list(prompt_key),
            "target_probe_run_id": run.get("_probe_run_id"),
            "target_provider": _run_provider(run),
            "query": query,
            "axis": prompt_key[1],
            "axis_metadata": (
                run.get("axis_metadata")
                if isinstance(run.get("axis_metadata"), dict)
                else {}
            ),
        }
        try:
            result = await llm_client.probe(
                scan_mode=_ANSWER_QUALITY_VERIFY_SCAN_MODE,
                scan_target_id=(
                    f"verify-{audit_run_id or 'adhoc'}-"
                    f"{sku_ctx.get('sku_key') or 'sku'}-{idx}"
                ),
                merchant_id=merchant_id,
                store_id=f"{merchant_id}_verify",
                context={
                    "product_title": str(sku_title),
                    "product_type": product.get("product_type") or product.get("category"),
                    "merchant_brand": merchant_brand,
                    "merchant_pdp_url": merchant_url,
                    "verify_query": query,
                    "verify_intent": query,
                    "verify_answer_text": _run_text(run),
                    "verify_evidence_excerpt": _verify_evidence_excerpt(run),
                },
                provider=_ANSWER_QUALITY_VERIFY_PROVIDER,
                max_runs=1,
                timeout_s=_VERIFY_PROBE_TIMEOUT_S,
            )
            verdict = _extract_verify_verdict(result)
            output = {
                **output_base,
                "verdict": verdict,
                "usage": result.get("usage") or {},
                "raw_runs": result.get("raw_runs") or [],
            }
            err = (
                {"query": query, "error": "unparseable_verify_verdict"}
                if verdict is None
                else None
            )
            return output, err
        except Exception as exc:  # noqa: BLE001 - verifier must not fail audit
            return (
                {**output_base, "verdict": None, "error": str(exc)[:200]},
                {"query": query, "error": str(exc)[:200]},
            )

    # Verify the citation-positive sample CONCURRENTLY — a slow/flaky DeepSeek must
    # not serialize into a multi-minute (apparently-hung) audit. Total wall-clock
    # is now ~= the slowest single call, not the sum of the sample.
    verify_results = await asyncio.gather(
        *[_verify_one(idx, run) for idx, run in enumerate(candidates[:sample_cap])]
    )
    outputs: List[Dict[str, Any]] = [out for out, _ in verify_results]
    errors: List[Dict[str, Any]] = [
        err for _, err in verify_results if err is not None
    ]

    valid_outputs = [
        output for output in outputs
        if isinstance(output.get("verdict"), Mapping)
    ]
    flagged_outputs = [
        output for output in valid_outputs
        if _verify_output_flagged(output)
    ]
    summary = {
        "status": "completed" if not errors else "partial",
        "provider": _ANSWER_QUALITY_VERIFY_PROVIDER,
        # Record the resolved verify model so a completed run is reproducible
        # (which DeepSeek SKU produced the answer-quality de-weighting).
        "model": (app_settings.deepseek_model or "").strip() or None,
        "role": "verify",
        "verified": len(valid_outputs),
        "flagged": len(flagged_outputs),
        "not_verified": max(0, len(candidates) - len(valid_outputs)),
        "citation_positive_candidates": len(candidates),
        "sample_cap": sample_cap,
        "sample_fraction": (verify_sample or {}).get("positive_fraction", 0.25),
        "flagged_probes": [
            {
                "query": output.get("query"),
                "target_probe_run_id": output.get("target_probe_run_id"),
                "target_provider": output.get("target_provider"),
                "note": ((output.get("verdict") or {}).get("note") or ""),
                "supports_recommendation": (
                    (output.get("verdict") or {}).get("supports_recommendation")
                ),
                "misstates_facts": (
                    (output.get("verdict") or {}).get("misstates_facts")
                ),
            }
            for output in flagged_outputs
        ],
        "deweight_rule": _ANSWER_QUALITY_VERIFY_DEWEIGHT_RULE,
    }
    if errors:
        summary["errors"] = errors[:5]
    return summary, outputs


def compute_citation_score(
    sku_ctx: Dict[str, Any],
    per_sku_probe_runs: Any,
    verify_outputs: Optional[Any] = None,
) -> Tuple[Optional[int], Dict[str, Any]]:
    """Spec A.4 citation score from Brief 1 per_sku_audit raw_runs.

    Returns score None (not 0) when no probes ran — "no signal" vs a measured 0.
    """
    product = _get_product(sku_ctx or {})
    sku = _get_sku(sku_ctx or {})
    runs = _flatten_probe_runs(per_sku_probe_runs)
    breakdown: Dict[str, Any] = {}
    missing: List[str] = []
    denominator = len(runs)
    if denominator <= 0:
        # No probes ran for this SKU → we measured NOTHING. Return score None
        # (not 0) so it reads as "no citation signal", not a real measured zero.
        # A measured 0 (probes ran, brand never cited) is the genuine INVISIBLE
        # case and is handled below with denominator > 0. Conflating the two made
        # an empty run emit a false INVISIBLE brand verdict.
        for name, max_points in (
            ("first_party_rate", 45),
            ("sku_mention_rate", 25),
            ("authority_near_variant_rate", 20),
            ("answer_quality_rate", 10),
        ):
            _add_bucket(
                breakdown, missing, name, 0, max_points,
                "no probes ran for this SKU",
                missing=["per_sku_audit.raw_runs"],
                extra={"numerator": 0, "denominator": 0, "rate": 0.0},
            )
        _total, finished = _finish_breakdown(breakdown, missing)
        finished["total"] = None
        finished["no_probes"] = True
        return None, finished

    canonical_url = product.get("canonical_url") or sku_ctx.get("canonical_url")
    pivota_url = product.get("pivota_canonical_url") or sku_ctx.get("pivota_canonical_url")
    title = product.get("title") or sku.get("title")
    sku_title = sku.get("title") or title
    variant_name = sku.get("sku") or sku.get("source_variant_id")

    first_party_hits = 0
    sku_mentions = 0
    authority_hits = 0
    quality_hits = 0
    verified_quality_clean = 0
    verify_deweighted = 0
    verify_by_key = _verify_outputs_by_prompt_key(verify_outputs)
    for run in runs:
        parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
        url_match = run.get("url_match") if isinstance(run.get("url_match"), dict) else {}
        llm_report = url_match.get("llm_self_report") if isinstance(url_match.get("llm_self_report"), dict) else {}
        text = _run_text(run)
        # Visibility verdict, shared by ALL buckets. An EXPLICIT negative means
        # the answer denies this is the right/visible product. Identity-robust
        # rule: under poor merchant names, providers echo the queried name AND
        # can ground on the merchant domain even while denying the product — so a
        # negative verdict must not earn first_party / sku_mention / authority
        # credit. (It inflated the lowest-visibility SKUs: e.g. Collagen Garden
        # scored citation 28 on 3/40 visible, ~19 pts from ungated first_party.)
        negative_verdict = _run_negative_verdict(run)

        # first_party: the merchant PDP must be a primary grounding source.
        # `url_match.in_grounding` can be true on branded prompts where the
        # brand is merely mentioned while publishers/retailers carry the
        # citations; that is visibility, not first-party control.
        if "_any_first_party_primary" in run:
            # Pre-merged any-provider run: the per-provider first-party decision
            # (grounded-primary AND not-negative) was already OR'd across
            # providers before their source lists were unioned, so use it
            # directly instead of re-running the majority test over the union.
            if run.get("_any_first_party_primary"):
                first_party_hits += 1
        else:
            grounded_first_party = _first_party_grounding_primary_for_run(
                run,
                sku_ctx or {},
                product,
            )
            if grounded_first_party and not negative_verdict:
                first_party_hits += 1

        # Affirmative structured signal that the provider actually surfaced the SKU.
        # correct_sku=True remains independently affirmative; a mere
        # sku_mentioned=True echo does not override an explicit negative verdict.
        affirmative_sku = (
            parsed.get("correct_sku") is True
            or llm_report.get("correct_sku") is True
            or (
                (
                    parsed.get("sku_mentioned") is True
                    or llm_report.get("sku_mentioned") is True
                )
                and not negative_verdict
            )
        )
        text_mention = _text_mentions_any(text, [title, sku_title, variant_name])
        text_only_denial = (
            text_mention
            and not _has_structured_citation_boolean(parsed, run, llm_report)
            and _citation_text_denies_product(text)
        )
        if affirmative_sku or (text_mention and not negative_verdict and not text_only_denial):
            sku_mentions += 1

        source_hosts = [normalize_host(url) for url in _source_urls(run)]
        external_source_present = any(host and not _is_first_party_host(host, sku_ctx or {}) for host in source_hosts)
        # authority's text-mention branch carries the same negative-echo flaw as
        # sku_mention did — gate it on a non-negative verdict. Affirmative
        # structured signals (correct_sku / product_visible True) still count.
        authority_affirmed = (
            parsed.get("correct_sku") is True
            or llm_report.get("correct_sku") is True
            or parsed.get("product_visible") is True
            or llm_report.get("product_visible") is True
            or (
                _text_mentions_any(text, [title, sku_title, product.get("content_key"), sku_ctx.get("product_group_id")])
                and not negative_verdict
            )
        )
        near_variant_found = (
            parsed.get("authority_near_variant_found") is True
            or llm_report.get("authority_near_variant_found") is True
        )
        if external_source_present and (
            authority_affirmed
            or (near_variant_found and not negative_verdict)
        ):
            authority_hits += 1

        if _answer_quality_positive(run):
            quality_hits += 1
            verify_output = verify_by_key.get(_citation_prompt_key(run))
            if verify_output and _verify_output_flagged(verify_output):
                verify_deweighted += 1
            elif verify_output:
                verified_quality_clean += 1
            # else: this prompt was NOT verified, so it earns no answer_quality
            # credit. Unchecked claims must not score (a tier that runs no
            # verify — e.g. the free URL-wedge — would otherwise report a
            # verified-looking answer_quality it never actually checked).

    def _rate_bucket(name: str, numerator: int, max_points: int) -> None:
        rate = numerator / denominator if denominator else 0.0
        points = int(round(max_points * rate))
        _add_bucket(
            breakdown, missing, name, points, max_points,
            f"{numerator}/{denominator} prompts matched",
            extra={"numerator": numerator, "denominator": denominator, "rate": round(rate, 4)},
        )

    # W1 site 7 (measure): legacy first-party = own page cited as PRIMARY
    # grounding + not negative-verdict; the decision-sheet target is T1 with
    # one unified resolver. own_url_cited_runs_any over the SAME candidate
    # hosts isolates the primary/negative-gating gap from the host-resolution
    # gap the cutover must decide on.
    parity_measure(
        "bd_report.compute_citation_score.first_party_rate",
        first_party_hits,
        own_url_cited_runs_any(
            runs, _first_party_url_candidates(sku_ctx, product)
        ),
        context={
            "sku": str(title or "")[:80],
            "denominator": denominator,
        },
    )
    _rate_bucket("first_party_rate", first_party_hits, 45)
    _rate_bucket("sku_mention_rate", sku_mentions, 25)
    _rate_bucket("authority_near_variant_rate", authority_hits, 20)
    # answer_quality is the ONLY bucket scored against verified prompts, not all
    # prompts: of the citation-positive answers we actually verified, how many
    # held up. When nothing was verified (e.g. the free URL-wedge tier passes
    # verify_providers=[]), it contributes 0 — we no longer award answer-quality
    # points for unchecked claims.
    verified_quality_total = verified_quality_clean + verify_deweighted
    answer_quality_rate = (
        verified_quality_clean / verified_quality_total
        if verified_quality_total
        else 0.0
    )
    _add_bucket(
        breakdown, missing, "answer_quality_rate",
        int(round(10 * answer_quality_rate)), 10,
        (
            f"{verified_quality_clean}/{verified_quality_total} verified prompts held up"
            if verified_quality_total
            else "no prompts verified on this tier — answer quality unscored"
        ),
        extra={
            "numerator": verified_quality_clean,
            "denominator": verified_quality_total,
            "rate": round(answer_quality_rate, 4),
        },
    )
    breakdown["answer_quality_rate"]["deterministic_numerator"] = quality_hits
    breakdown["answer_quality_rate"]["verify_deweighted"] = verify_deweighted
    breakdown["answer_quality_rate"]["unverified_excluded"] = (
        quality_hits - verified_quality_total
    )
    breakdown["answer_quality_rate"]["verify_rule"] = (
        _ANSWER_QUALITY_VERIFY_DEWEIGHT_RULE
    )
    total = int(round(
        45 * (first_party_hits / denominator)
        + 25 * (sku_mentions / denominator)
        + 20 * (authority_hits / denominator)
        + 10 * answer_quality_rate
    ))
    total = max(0, min(100, total))
    breakdown["total"] = total
    if missing:
        breakdown["missing_inputs"] = missing
    return total, breakdown


def _probe_provider(probe: Dict[str, Any]) -> str:
    provider = str(
        probe.get("provider")
        or probe.get("_provider")
        or "unknown"
    ).strip().lower()
    return provider or "unknown"


def _run_provider(run: Dict[str, Any]) -> str:
    provider = str(
        run.get("_provider")
        or run.get("provider")
        or "unknown"
    ).strip().lower()
    return provider or "unknown"


def _group_probe_runs_by_provider(
    per_sku_probe_runs: Any,
) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for probe in _json_list(per_sku_probe_runs):
        if not isinstance(probe, dict):
            continue
        grouped[_probe_provider(probe)].append(probe)
    return dict(grouped)


def _citation_prompt_key(run: Dict[str, Any]) -> Tuple[str, str, str]:
    meta = run.get("axis_metadata") if isinstance(run.get("axis_metadata"), dict) else {}
    return (
        str(meta.get("sku_key") or "").strip(),
        str(meta.get("axis") or "").strip(),
        str(run.get("query") or "").strip().lower(),
    )


def _merge_runs_for_any_provider(
    runs: List[Dict[str, Any]],
    sku_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    merged = dict(runs[0]) if runs else {}
    parsed_out: Dict[str, Any] = {}
    url_match_out: Dict[str, Any] = {}
    sources: List[Dict[str, Any]] = []
    chunks: List[Any] = []
    providers: List[str] = []
    excerpts: List[str] = []

    for run in runs:
        provider = _run_provider(run)
        if provider not in providers:
            providers.append(provider)
        parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
        url_match = run.get("url_match") if isinstance(run.get("url_match"), dict) else {}
        llm_report = (
            url_match.get("llm_self_report")
            if isinstance(url_match.get("llm_self_report"), dict)
            else {}
        )

        for key in (
            "product_visible",
            "sku_mentioned",
            "correct_sku",
            "authority_near_variant_found",
            "merchant_url_found",
        ):
            if parsed.get(key) is True or llm_report.get(key) is True:
                parsed_out[key] = True
            elif key not in parsed_out and parsed.get(key) is not None:
                parsed_out[key] = parsed.get(key)

        if url_match.get("in_grounding") is True:
            url_match_out["in_grounding"] = True
        elif "in_grounding" not in url_match_out and url_match.get("in_grounding") is not None:
            url_match_out["in_grounding"] = url_match.get("in_grounding")

        for source in run.get("grounding_sources") or []:
            if isinstance(source, dict) and source not in sources:
                copy = dict(source)
                copy.setdefault("provider", provider)
                sources.append(copy)
        for chunk in run.get("grounding_chunks") or []:
            if chunk not in chunks:
                chunks.append(chunk)
        excerpt = (
            run.get("evidence_excerpt")
            or parsed.get("evidence_excerpt")
            or parsed.get("evidence_text")
        )
        if excerpt:
            excerpts.append(str(excerpt))

    merged["parsed"] = {**(merged.get("parsed") or {}), **parsed_out}
    merged["url_match"] = {**(merged.get("url_match") or {}), **url_match_out}
    merged["grounding_sources"] = sources
    merged["grounding_chunks"] = chunks
    merged["_provider"] = ",".join(providers)
    merged["_providers"] = providers
    if excerpts:
        merged["evidence_excerpt"] = excerpts[0]
    # OR the per-provider first-party decision BEFORE the source lists were
    # unioned above. Honoured by compute_citation_score so the combined
    # first_party_rate reflects "any provider cited the merchant PDP", matching
    # the printed any_profile_provider aggregation rule (see
    # _run_first_party_credit for why the post-union majority test was wrong).
    if sku_ctx is not None:
        product = _get_product(sku_ctx or {})
        merged["_any_first_party_primary"] = any(
            _run_first_party_credit(run, sku_ctx, product) for run in runs
        )
    return merged


def _any_provider_probe_runs(
    per_sku_probe_runs: Any,
    sku_ctx: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    grouped_runs: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for run in _flatten_probe_runs(per_sku_probe_runs):
        grouped_runs[_citation_prompt_key(run)].append(run)
    merged_runs = [
        _merge_runs_for_any_provider(runs, sku_ctx=sku_ctx)
        for _key, runs in sorted(grouped_runs.items())
        if runs
    ]
    return [{
        "provider": "coverage_profile_any",
        "raw_runs": merged_runs,
    }] if merged_runs else []


def _run_is_error(run: Any) -> bool:
    """True when a single grounded run carried an upstream error instead of a
    real answer. The gateway returns HTTP 200 with the run's `raw` prefixed
    `__error__:` (e.g. an OpenAI 429 quota error) rather than raising, so these
    runs must not be mistaken for a real "answered but didn't cite" run."""
    if not isinstance(run, dict):
        return False
    if run.get("error"):
        return True
    raw = run.get("raw")
    return isinstance(raw, str) and raw.startswith("__error__")


def _provider_probe_run_health(probes: Any) -> Tuple[int, int]:
    """(succeeded_runs, attempted_runs) across one provider's per-SKU payloads.

    Prefers the gateway's authoritative usage.succeeded_runs / failed_runs; when
    a payload predates those fields, falls back to counting non-error raw_runs.
    An explicit probe_failed payload (the exception path) counts as an attempt
    with zero successes."""
    succeeded = 0
    attempted = 0
    for probe in _json_list(probes):
        if not isinstance(probe, dict):
            continue
        if probe.get("status") == "probe_failed":
            attempted += max(1, int(probe.get("runs_count") or 0))
            continue
        usage = probe.get("usage") if isinstance(probe.get("usage"), dict) else {}
        succ = usage.get("succeeded_runs")
        failed = usage.get("failed_runs")
        raw_runs = [r for r in (probe.get("raw_runs") or []) if isinstance(r, dict)]
        if isinstance(succ, int) or isinstance(failed, int):
            s = int(succ or 0)
            f = int(failed or 0)
            succeeded += s
            attempted += (s + f) if (s + f) > 0 else len(raw_runs)
        else:
            # Older gateway response without per-run health: infer from the
            # `__error__:` markers on the raw runs themselves.
            succeeded += sum(1 for r in raw_runs if not _run_is_error(r))
            attempted += len(raw_runs)
    return succeeded, attempted


def _provider_probes_all_failed(probes: Any) -> bool:
    """Per-provider mirror of the worker's `_all_per_sku_probes_failed`: True when
    a provider attempted probes but produced ZERO successful runs (e.g. every
    ChatGPT run came back as a 429 quota error). Such a provider measured
    nothing and must be treated as "coverage unavailable", NOT scored as a real
    0 that would drag the aggregate verdict toward INVISIBLE."""
    succeeded, attempted = _provider_probe_run_health(probes)
    return attempted > 0 and succeeded == 0


def _first_probe_error(probes: Any) -> str:
    """Human-readable upstream error for a wholesale-failed provider, pulled from
    the payload `error` or the first `__error__:`-prefixed run."""
    for probe in _json_list(probes):
        if not isinstance(probe, dict):
            continue
        err = str(probe.get("error") or "").strip()
        if err:
            return err[:300]
        for run in probe.get("raw_runs") or []:
            if not isinstance(run, dict):
                continue
            raw = run.get("raw")
            if isinstance(raw, str) and raw.startswith("__error__"):
                return raw.replace("__error__:", "", 1).strip()[:300]
            if run.get("error"):
                return str(run.get("error")).strip()[:300]
    return "provider probes returned no successful runs"


def build_citation_by_provider(
    sku_ctx: Dict[str, Any],
    per_sku_probe_runs: Any,
    verify_outputs: Optional[Any] = None,
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for provider, probes in sorted(_group_probe_runs_by_provider(per_sku_probe_runs).items()):
        explicit_failed = next(
            (
                probe for probe in _json_list(probes)
                if isinstance(probe, dict)
                and probe.get("status") == "probe_failed"
            ),
            None,
        )
        # Two shapes of "this provider measured nothing":
        #   1. an explicit probe_failed payload (the probe call raised), or
        #   2. the gateway returned HTTP 200 but every grounded run errored
        #      (e.g. OpenAI 429 quota) — succeeded_runs==0 with no probe_failed.
        # Both must be surfaced as coverage-unavailable rather than scored 0.
        # Downstream rollups already skip status == "probe_failed".
        if explicit_failed is not None or _provider_probes_all_failed(probes):
            score, breakdown = compute_citation_score(sku_ctx, [], verify_outputs=verify_outputs)
            error = (
                str(explicit_failed.get("error") or "").strip()[:500]
                if explicit_failed is not None and explicit_failed.get("error")
                else _first_probe_error(probes)
            )
            out[provider] = {
                "status": "probe_failed",
                "coverage_unavailable": True,
                "error": error,
                "score": score,
                "breakdown": breakdown,
                "prompts": 0,
            }
            continue
        score, breakdown = compute_citation_score(
            sku_ctx, probes, verify_outputs=verify_outputs,
        )
        out[provider] = {
            "score": score,
            "breakdown": breakdown,
            "prompts": len(_flatten_probe_runs(probes)),
        }
    return out


def _provider_cited_sku(provider_entry: Dict[str, Any]) -> bool:
    """True when this provider actually surfaced the SKU — cited as the source
    or mentioned by name. Reads the citation breakdown numerators; a non-zero
    first-party or SKU-mention count is a real "the model surfaced you" signal
    (vs. a bare score that can be lifted by answer-quality alone)."""
    if not isinstance(provider_entry, dict) or provider_entry.get("status") == "probe_failed":
        return False
    breakdown = provider_entry.get("breakdown") or {}
    for bucket in ("first_party_rate", "sku_mention_rate"):
        detail = breakdown.get(bucket)
        if isinstance(detail, dict) and int(detail.get("numerator") or 0) > 0:
            return True
    return False


def _models_cited_for_sku(citation_by_provider: Dict[str, Any]) -> Dict[str, int]:
    """Cross-model signal: in how many of the models that ran did this SKU get
    cited/mentioned. `of` counts providers that actually probed (excludes
    probe_failed)."""
    providers = [
        entry for entry in (citation_by_provider or {}).values()
        if isinstance(entry, dict) and entry.get("status") != "probe_failed"
    ]
    cited = sum(1 for entry in providers if _provider_cited_sku(entry))
    return {"cited": cited, "of": len(providers)}


def _brand_citation_by_provider(per_sku_reports: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Brand-level per-model rollup, derived from the per-SKU
    citation_by_provider that already rode in each report. No new LLM calls.
    Per provider: median/p25/p75 citation across SKUs, SKUs scored, SKUs the
    model cited, total prompts. With only Gemini running today this is a single
    entry; it's the surface that lights up as more providers are enabled."""
    by_provider: Dict[str, Dict[str, List[Any]]] = {}
    unavailable: Dict[str, Dict[str, Any]] = {}
    for report in per_sku_reports or []:
        cbp = (report or {}).get("citation_by_provider") or {}
        for provider, entry in cbp.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("status") == "probe_failed" or entry.get("coverage_unavailable"):
                # Provider measured nothing on this SKU (probe failed / all runs
                # errored). Track it so a provider that NEVER scored is surfaced
                # as coverage-unavailable rather than silently dropped — which
                # would read to the merchant as "we only measured Gemini".
                u = unavailable.setdefault(provider, {"skus": 0, "error": None})
                u["skus"] += 1
                if not u["error"] and entry.get("error"):
                    u["error"] = str(entry.get("error"))[:300]
                continue
            acc = by_provider.setdefault(provider, {"scores": [], "skus_cited": 0, "prompts": 0})
            score = entry.get("score")
            if score is not None:
                acc["scores"].append(int(score))
            if _provider_cited_sku(entry):
                acc["skus_cited"] += 1
            acc["prompts"] += int(entry.get("prompts") or 0)
    out: Dict[str, Dict[str, Any]] = {}
    for provider, acc in by_provider.items():
        scores = acc["scores"]
        out[provider] = {
            "median": _percentile(scores, 0.5),
            "p25": _percentile(scores, 0.25),
            "p75": _percentile(scores, 0.75),
            "skus_scored": len(scores),
            "skus_cited": acc["skus_cited"],
            "prompts": acc["prompts"],
        }
    # Providers that were attempted on some SKUs but never produced a scored
    # result → coverage unavailable (not a real 0). A provider that scored on at
    # least one SKU keeps its real entry above.
    for provider, u in unavailable.items():
        if provider in out:
            continue
        out[provider] = {
            "status": "coverage_unavailable",
            "skus_unavailable": u["skus"],
            "error": u["error"] or "provider probes returned no successful runs",
        }
    return out


async def _fetch_one_dict(query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from db.database import database
    try:
        row = await database.fetch_one(query, values)
    except Exception as exc:
        # Degraded-read fallback (an audit shouldn't crash on one bad read), but
        # NOT silent — a swallowed schema error here masked the index_pipeline_state
        # loader bug across every v3 audit. Log so the next one is caught fast.
        logger.warning(
            "audit ctx fetch_one failed (returning None): %s | query=%s",
            exc, " ".join(query.split())[:160],
        )
        return None
    return _row_dict(row)


async def _fetch_all_dicts(query: str, values: Dict[str, Any]) -> List[Dict[str, Any]]:
    from db.database import database
    try:
        rows = await database.fetch_all(query, values)
    except Exception as exc:
        logger.warning(
            "audit ctx fetch_all failed (returning []): %s | query=%s",
            exc, " ".join(query.split())[:160],
        )
        return []
    return [d for d in (_row_dict(row) for row in rows or []) if d is not None]


async def load_sku_context(sku_key: str, merchant_id: str) -> Dict[str, Any]:
    """Read-only SKU-context loader for spec A.1-A.3 catalog signals."""
    cache_key = (str(sku_key or ""), str(merchant_id or ""))
    if cache_key in _SKU_CONTEXT_CACHE:
        return _SKU_CONTEXT_CACHE[cache_key]
    # URL-audit synthetic products have no catalog row — resolve from the
    # registry (survives reset_sku_context_cache, see _SYNTHETIC_SKU_CONTEXTS).
    # Checked before the catalog query so it's authoritative for `urlwedge:*`
    # keys and so the per_sku assembly loop (which runs after the cache reset)
    # sees the same context the probe fan-out used.
    if cache_key in _SYNTHETIC_SKU_CONTEXTS:
        ctx = _SYNTHETIC_SKU_CONTEXTS[cache_key]
        # URL-audit synthetic SKUs return HERE, before the catalog fall-through
        # where the LLM attribute extractor runs (_maybe_stash_llm_attributes,
        # below). Historically that meant the extractor NEVER fired on url_audits —
        # only on connected catalog SKUs — so url_audit electronics/generic SKUs
        # silently got no grounded attribute-axis enrichment. Run it here too, using
        # the same flag/allowlist/should_run gating. Guarded by a sentinel so it runs
        # at most once per synthetic ctx: the ctx persists in _SYNTHETIC_SKU_CONTEXTS
        # across reset_sku_context_cache, and url_audit SKUs (no catalog row) aren't
        # covered by the extractor's durable cache, so an unguarded call would re-pay
        # the LLM on every cache-reset re-read.
        if not ctx.get("_llm_attr_extractor_attempted"):
            ctx["_llm_attr_extractor_attempted"] = True
            await _maybe_stash_llm_attributes(ctx)
            # Observability: url_audit SKUs leave no durable extractor trace
            # (no catalog row, stash is in-memory), so log the grounded count +
            # source size to make the extractor's contribution to url-audits
            # verifiable instead of silent. WARNING level on purpose: the app
            # configures no logging handler, so INFO from app-module loggers is
            # dropped in prod (only Python's last-resort WARNING+ handler emits);
            # this is a real signal worth surfacing.
            logger.warning(
                "url_audit extractor: merchant=%s sku=%s stashed_grounded=%d source_chars=%d",
                ctx.get("merchant_id"),
                cache_key[0],
                len(ctx.get(_LLM_ATTR_STASH_KEY) or []),
                len(build_source_text(_get_product(ctx) or {})),
            )
        _SKU_CONTEXT_CACHE[cache_key] = ctx
        return ctx
    if not cache_key[0] or not cache_key[1]:
        return {"sku_key": sku_key, "merchant_id": merchant_id, "missing_inputs": ["sku_key", "merchant_id"]}

    sku = await _fetch_one_dict(
        """
        SELECT *
          FROM catalog_skus
         WHERE sku_key = :sku_key
           AND merchant_id = :merchant_id
         LIMIT 1
        """,
        {"sku_key": sku_key, "merchant_id": merchant_id},
    )
    if not sku:
        ctx = {"sku_key": sku_key, "merchant_id": merchant_id, "missing_inputs": ["catalog_skus"]}
        _SKU_CONTEXT_CACHE[cache_key] = ctx
        return ctx

    product_key = sku.get("product_key")
    product = await _fetch_one_dict(
        """
        SELECT *
          FROM catalog_products
         WHERE product_key = :product_key
           AND merchant_id = :merchant_id
         LIMIT 1
        """,
        {"product_key": product_key, "merchant_id": merchant_id},
    ) or {}
    platform = product.get("platform") or sku.get("platform")
    source_product_id = product.get("source_product_id") or sku.get("source_product_id")

    all_skus = await _fetch_all_dicts(
        """
        SELECT *
          FROM catalog_skus
         WHERE product_key = :product_key
           AND merchant_id = :merchant_id
        """,
        {"product_key": product_key, "merchant_id": merchant_id},
    )
    offers = await _fetch_all_dicts(
        """
        SELECT *
          FROM catalog_offers
         WHERE merchant_id = :merchant_id
           AND (sku_key = :sku_key OR product_key = :product_key)
        """,
        {"merchant_id": merchant_id, "sku_key": sku_key, "product_key": product_key},
    )
    product_group_members = await _fetch_all_dicts(
        """
        SELECT *
          FROM product_group_members
         WHERE merchant_id = :merchant_id
           AND platform_product_id = :platform_product_id
        """,
        {"merchant_id": merchant_id, "platform_product_id": source_product_id},
    )
    group_id = product_group_members[0].get("product_group_id") if product_group_members else None
    # index_pipeline_state is keyed by content_key (+ carries pivota_signature_id
    # and product_group_id) — it has NO product_key column. The previous query
    # referenced product_key, so it raised UndefinedColumnError on every call and
    # _fetch_one_dict swallowed it → every v3 audit scored with EMPTY index state
    # (serving_eligibility fell back to pdp_lifecycle_stage). Join on the columns
    # that actually exist, preferring the exact content_key match.
    index_state = await _fetch_one_dict(
        """
        SELECT *
          FROM index_pipeline_state
         WHERE content_key = :content_key
            OR pivota_signature_id = :pivota_signature_id
            OR (merchant_id = :merchant_id AND product_group_id = :product_group_id)
         ORDER BY CASE
                    WHEN content_key = :content_key THEN 0
                    WHEN pivota_signature_id = :pivota_signature_id THEN 1
                    ELSE 2
                  END
         LIMIT 1
        """,
        {
            "content_key": product.get("content_key"),
            "pivota_signature_id": product.get("pivota_signature_id"),
            "merchant_id": merchant_id,
            "product_group_id": group_id,
        },
    ) or {}
    enrichment = await _fetch_one_dict(
        """
        SELECT *
          FROM product_enrichment
         WHERE merchant_id = :merchant_id
           AND platform = :platform
           AND platform_product_id = :platform_product_id
         ORDER BY CASE WHEN geo_code = 'default' THEN 0 ELSE 1 END
         LIMIT 1
        """,
        {"merchant_id": merchant_id, "platform": platform, "platform_product_id": source_product_id},
    ) or {}
    quality = await _fetch_one_dict(
        """
        SELECT *
          FROM product_quality_snapshot
         WHERE merchant_id = :merchant_id
           AND platform = :platform
           AND platform_product_id = :platform_product_id
         ORDER BY snapshot_date DESC NULLS LAST, created_at DESC NULLS LAST
         LIMIT 1
        """,
        {"merchant_id": merchant_id, "platform": platform, "platform_product_id": source_product_id},
    ) or {}
    field_facts = await _fetch_all_dicts(
        """
        SELECT *
          FROM catalog_field_facts
         WHERE (entity_id = :product_key OR entity_id = :sku_key)
         ORDER BY observed_at DESC NULLS LAST
         LIMIT 50
        """,
        {"product_key": product_key, "sku_key": sku_key},
    )
    beauty_profile = await _fetch_one_dict(
        """
        SELECT *
          FROM beauty_product_profiles
         WHERE product_key = :product_key
           AND merchant_id = :merchant_id
         LIMIT 1
        """,
        {"product_key": product_key, "merchant_id": merchant_id},
    ) or {}
    beauty_ingredients = await _fetch_all_dicts(
        """
        SELECT *
          FROM beauty_sku_ingredients
         WHERE sku_key = :sku_key
           AND merchant_id = :merchant_id
        """,
        {"sku_key": sku_key, "merchant_id": merchant_id},
    )
    beauty_usage_guides = await _fetch_all_dicts(
        """
        SELECT *
          FROM beauty_usage_guides
         WHERE merchant_id = :merchant_id
           AND (sku_key = :sku_key OR product_key = :product_key)
        """,
        {"sku_key": sku_key, "product_key": product_key, "merchant_id": merchant_id},
    )
    beauty_compatibility = await _fetch_all_dicts(
        """
        SELECT *
          FROM beauty_compatibility_rules
         WHERE merchant_id = :merchant_id
           AND (sku_key = :sku_key OR product_key = :product_key)
        """,
        {"sku_key": sku_key, "product_key": product_key, "merchant_id": merchant_id},
    )
    commerce = await _fetch_one_dict(
        """
        SELECT *
          FROM merchant_commerce_readiness_state
         WHERE merchant_id = :merchant_id
         LIMIT 1
        """,
        {"merchant_id": merchant_id},
    ) or {}
    policies = await _fetch_all_dicts(
        """
        SELECT *
          FROM pcs_shop_policies
         WHERE merchant_id = :merchant_id
        """,
        {"merchant_id": merchant_id},
    )
    # The `merchants` table has NO `merchant_id` column (verified against live
    # schema), so the old `WHERE merchant_id = :merchant_id` read raised an
    # undefined-column error that _fetch_one_dict swallowed to {} on every
    # audit — under-scoring merchant_trust_state and policy_jurisdiction. The
    # `merch_` identity lives in merchant_onboarding; the only reliable link to
    # the merchants row is the shared contact_email (mirrors billing_routes
    # _resolve_merchant_id_from_customer_id, which joins on LOWER(contact_email)).
    merchant = await _fetch_one_dict(
        """
        SELECT m.*
          FROM merchants m
          JOIN merchant_onboarding mo
            ON LOWER(m.contact_email) = LOWER(mo.contact_email)
         WHERE mo.merchant_id = :merchant_id
           AND m.contact_email IS NOT NULL
           AND m.contact_email <> ''
         LIMIT 1
        """,
        {"merchant_id": merchant_id},
    ) or {}
    # Phase 2b: substantiated merchant evidence (general product_evidence store).
    # Drives two things, both derived from the SAME read:
    #   (1) has_substantiated_evidence — the boolean that feeds the existing 10-pt
    #       "Substantiated claims" bucket via _has_substantiation (reusing that
    #       weight, not inventing one).
    #   (2) a NON-SCORING "backed by N third-party sources" signal annotated onto the
    #       content_richness breakdown — recorded in the stored report_jsonb and
    #       available to agent/analytics consumers (it does NOT survive
    #       _strip_score_breakdowns into the merchant-facing payload; merchants see
    #       evidence depth directly in the intake panel). It never inflates or
    #       redistributes the 100-pt scale.
    # Best-effort: absent table / non-Postgres / parse error → no evidence, no signal
    # (the scoring is unaffected). NULL merchant_id rows (web-crawl writes) count too.
    # geo_code pinned to 'default' to match the serve model + the canonical readers
    # (fetch_product_evidence_row / fetch_product_evidence_for_keys); the PK is
    # (product_key, geo_code), so an unpinned LIMIT 1 would read an arbitrary geo.
    has_substantiated_evidence = False
    substantiated_evidence_count = 0
    third_party_evidence_sources = 0
    try:
        evidence_row = await _fetch_one_dict(
            """
            SELECT claims
              FROM product_evidence
             WHERE product_key = :product_key
               AND geo_code = 'default'
               AND (merchant_id = :merchant_id OR merchant_id IS NULL)
             LIMIT 1
            """,
            {"product_key": product_key, "merchant_id": merchant_id},
        )
        third_party_types = {
            "editorial_press", "third_party_review", "third_party_test", "certification",
        }
        third_party_refs = set()
        for claim in _json_list((evidence_row or {}).get("claims")):
            if not isinstance(claim, dict):
                continue
            if str(claim.get("substantiation_status") or "").lower() != "substantiated":
                continue
            substantiated_evidence_count += 1
            if str(claim.get("source_type") or "").lower() in third_party_types:
                ref = str(claim.get("source_ref") or "").strip().lower()
                if ref:
                    third_party_refs.add(ref)
        has_substantiated_evidence = substantiated_evidence_count > 0
        third_party_evidence_sources = len(third_party_refs)
    except Exception:
        pass

    content_key = product.get("content_key")
    peers = []
    if content_key:
        peers = await _fetch_all_dicts(
            """
            SELECT cp.product_key, cp.brand, cp.title, cp.content_key,
                   pgm.product_group_id
              FROM catalog_products cp
              LEFT JOIN product_group_members pgm
                ON pgm.merchant_id = cp.merchant_id
               AND pgm.platform = cp.platform
               AND pgm.platform_product_id = cp.source_product_id
             WHERE cp.content_key = :content_key
             LIMIT 25
            """,
            {"content_key": content_key},
        )

    ctx = {
        "sku_key": sku_key,
        "merchant_id": merchant_id,
        "product_key": product_key,
        "content_key": content_key,
        # Resolve the vertical ONCE at SKU-context build (Principle 1). Honors the
        # durable catalog_products.resolved_vertical column when present, else
        # resolves live (with title). Every downstream component reads this key
        # via _profile_for_sku_ctx instead of re-inferring the vertical.
        "vertical": _resolved_vertical_for_ctx(None, product),
        "product": product,
        "sku": sku,
        "all_skus": all_skus,
        "offers": offers,
        "product_group_members": product_group_members,
        "index_pipeline_state": index_state,
        "product_enrichment": enrichment,
        "product_quality_snapshot": quality,
        "catalog_field_facts": field_facts,
        "beauty_product_profile": beauty_profile,
        "beauty_sku_ingredients": beauty_ingredients,
        "beauty_usage_guides": beauty_usage_guides,
        "beauty_compatibility_rules": beauty_compatibility,
        "merchant_commerce_readiness_state": commerce,
        "pcs_shop_policies": policies,
        "merchant": merchant,
        "content_key_peers": peers,
        "has_substantiated_evidence": has_substantiated_evidence,
        "substantiated_evidence_count": substantiated_evidence_count,
        "third_party_evidence_sources": third_party_evidence_sources,
    }
    # Phase 2b: flag-gated LLM attribute extraction for lexicon-thin SKUs
    # (electronics/generic/thin) — no-op when the flag is off. Runs once here and
    # is cached with the ctx, so the sync probe builders read the stash for free.
    await _maybe_stash_llm_attributes(ctx)
    _SKU_CONTEXT_CACHE[cache_key] = ctx
    return ctx


def _matches_sku_run(run: Dict[str, Any], sku_key: str) -> bool:
    meta = run.get("axis_metadata") if isinstance(run.get("axis_metadata"), dict) else {}
    return not sku_key or (meta.get("sku_key") == sku_key)


def _extract_probe_result_candidates(doc: Any, sku_key: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if isinstance(doc, str):
        # JSONB columns (report_jsonb / partial_result_jsonb) arrive as JSON
        # STRINGS under asyncpg — there is no global JSON codec (see
        # db/database.py), so each read path must decode. load_per_sku_probe_runs
        # reads these via _row_dict (plain dict(row), no decode), so without this
        # the string fails the `isinstance(doc, dict)` guard below and every
        # probe run is silently dropped → citation scores 0 with
        # missing_inputs=["per_sku_audit.raw_runs"]. Mirrors _decode_jsonb_field
        # in db/merchant_audit_runs.py (the #706 fix on the GET path).
        stripped = doc.strip()
        if not stripped:
            return found
        try:
            doc = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return found
    if isinstance(doc, list):
        for item in doc:
            found.extend(_extract_probe_result_candidates(item, sku_key))
        return found
    if not isinstance(doc, dict):
        return found

    for key in ("per_sku_probe_runs", "probe_runs_by_sku", "raw_runs_by_sku"):
        mapping = doc.get(key)
        if isinstance(mapping, dict):
            value = mapping.get(sku_key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        found.append(item)
            elif isinstance(value, dict):
                found.append(value)

    raw_runs = doc.get("raw_runs")
    if isinstance(raw_runs, list) and (
        doc.get("scan_mode") == "per_sku_audit"
        or any(isinstance(r, dict) and _matches_sku_run(r, sku_key) for r in raw_runs)
    ):
        filtered = [r for r in raw_runs if isinstance(r, dict) and _matches_sku_run(r, sku_key)]
        if filtered:
            copy = dict(doc)
            copy["raw_runs"] = filtered
            found.append(copy)

    for value in doc.values():
        if isinstance(value, (dict, list)):
            found.extend(_extract_probe_result_candidates(value, sku_key))
    return found


async def load_per_sku_probe_runs(
    sku_key: str,
    merchant_id: str,
    audit_run_id: Optional[str],
    include_internal_comparison: bool = False,
) -> List[Dict[str, Any]]:
    """Best-effort read of persisted per_sku_audit probe payloads.

    Default excludes internal deep-tier comparison runs (the merchant-visible
    set). `include_internal_comparison=True` is for the internal rollup only
    (build_per_sku_report computes deep_landscape_internal from the raw set,
    then hands every merchant-facing surface the filtered one)."""
    if not audit_run_id:
        return []
    row = await _fetch_one_dict(
        """
        SELECT report_jsonb, partial_result_jsonb, cost_summary_jsonb
          FROM merchant_audit_runs
         WHERE run_id = :audit_run_id
           AND merchant_id = :merchant_id
         LIMIT 1
        """,
        {"audit_run_id": audit_run_id, "merchant_id": merchant_id},
    )
    if not row:
        return []
    candidates: List[Dict[str, Any]] = []
    for key in ("report_jsonb", "partial_result_jsonb"):
        candidates.extend(_extract_probe_result_candidates(row.get(key), sku_key))
    # De-dupe by probe_run_id/provider/raw query tuple.
    out: List[Dict[str, Any]] = []
    seen = set()
    for candidate in candidates:
        raw = candidate.get("raw_runs") if isinstance(candidate, dict) else None
        queries = tuple((r.get("query") or "") for r in raw or [] if isinstance(r, dict))
        key = (candidate.get("probe_run_id") or candidate.get("run_id") or candidate.get("provider"), queries)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    # P2-5: unwrap Vertex grounding redirectors to the real publisher URLs
    # BEFORE any builder consumes these runs, so every operator-facing surface
    # (failing_prompts, verbatim evidence, win-plan grounds_in) stores a
    # clickable, permanent URL instead of the opaque, expiring redirector.
    # Best-effort + process-cached; unresolvable URIs stay as-is.
    try:
        from services.grounding_redirect_resolver import (
            resolve_grounding_redirects_in_runs,
        )

        await resolve_grounding_redirects_in_runs(_flatten_probe_runs(out))
    except Exception as exc:  # noqa: BLE001 — never sink the report build
        logger.warning("grounding redirect unwrap skipped: %s", exc)
    if include_internal_comparison:
        return out
    return _merchant_visible_probe_payloads(out)


def _merchant_visible_probe_payloads(
    payloads: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """INTERNAL-FIRST (founder 2026-07-21): strip deep-tier comparison runs at
    the single boundary where report assembly loads probe runs — so every
    downstream surface (authority map, grounding evidence, citation-score
    denominators, RunFacts, opportunity, coverage counts) is coherent about
    the same merchant-visible run set, instead of each surface needing its own
    gate. The RAW payloads stay untouched in partial_result_jsonb (the loader
    is read-only; the report phase persists only brand_report), so
    substitution data accrues from the first deep run and flipping the surface
    later needs no re-probe. The per-surface gates (_failing_prompts,
    _query_class_coverage, extract_category_competitors,
    build_sku_opportunity) stay as defense in depth for callers that hand
    those functions unloaded run lists. Payload dicts are copied, never
    mutated, and non-run keys (e.g. the riding prompt_basis_meta) carry over."""
    filtered: List[Dict[str, Any]] = []
    for payload in payloads:
        raw_runs = payload.get("raw_runs") if isinstance(payload, dict) else None
        if isinstance(raw_runs, list) and any(
            run_is_internal_comparison(run) for run in raw_runs
        ):
            payload = {
                **payload,
                "raw_runs": [
                    run for run in raw_runs
                    if not run_is_internal_comparison(run)
                ],
            }
        filtered.append(payload)
    return filtered


def _axis_coverage(probe_runs: Any) -> Dict[str, int]:
    counts: Counter = Counter()
    for run in _flatten_probe_runs(probe_runs):
        meta = run.get("axis_metadata") if isinstance(run.get("axis_metadata"), dict) else {}
        axis = str(meta.get("axis") or "unknown").strip() or "unknown"
        counts[axis] += 1
    return dict(counts)


# QUERY_CLASS_BRANDED / QUERY_CLASS_CATEGORY / _query_class_for_axis /
# _run_query_class MOVED to services/audit_facts.py (W1 RunFacts phase 1)
# and re-imported at the top of this module.


# Internal-first gate for deep-tier comparison probes — shared with
# sku_opportunity via services.deep_tier_prompts (single definition, no cycle).
_run_is_internal_comparison = run_is_internal_comparison


def _query_class_coverage(probe_runs: Any) -> Dict[str, int]:
    """Probe counts split into branded/navigational vs category/discovery, so
    the report never conflates "found when shoppers name you" with "found when
    shoppers ask the category question". Comparison-axis probes are excluded:
    internal-first, and the coarse classifier would miscount them as branded."""
    counts = {QUERY_CLASS_BRANDED: 0, QUERY_CLASS_CATEGORY: 0}
    for run in _flatten_probe_runs(probe_runs):
        if _run_is_internal_comparison(run):
            continue
        counts[_run_query_class(run)] += 1
    return counts


# Fine intent-axis taxonomy (Step 2) — a per-query INTENT classification layered on
# top of the coarse `axis`, so the report shows citation performance by the WAY
# shoppers ask (head term vs problem/need vs constraint vs trust vs navigational),
# not just branded-vs-category. Classified from (query, axis) at report-build time
# — additive, no probe-pipeline change. Snapshot-only (no per-intent trend yet; see
# PIVOTA-Agent/docs/ai_readiness_query_axes_build_plan.md).
_INTENT_AXES = ("category_head", "problem_jtbd", "constraint", "trust", "navigational", "custom")


def _intent_axis_for(query: Optional[str], axis: Optional[str]) -> str:
    a = str(axis or "").strip().lower()
    q = str(query or "").strip().lower()
    if a in ("intent", "identity"):
        return "navigational"
    if a == "review":
        return "trust"
    if a in ("attribute", "sidewalk"):
        return "constraint"
    if a == "custom":
        return "custom"
    if a == "category":
        # need/problem-framed ("best X for sleep", "what helps with X", "X for women")
        # vs a bare head term ("best X", "top X", "X reviews").
        if q.startswith("what helps") or " for " in q:
            return "problem_jtbd"
        return "category_head"
    return "category_head"


def _citation_by_intent(per_prompt: Any) -> Dict[str, Dict[str, Any]]:
    """Per-SKU citation rate grouped by fine intent axis: for each intent, how many
    of its probed queries cited the merchant. Surfaces WHERE the SKU wins by question
    type (e.g. cited on trust/navigational but not problem/discovery). Snapshot-only.

    `cited`/`rate` stay BRAND-level (merchant_cited_runs — unchanged for
    run-to-run comparability). `sku_cited`/`sku_rate` are the strict SKU-level
    split (this exact SKU verified in the answer): the gap between the two is
    sibling/brand-only citation — brand visibility this SKU doesn't own."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in per_prompt or []:
        if not isinstance(row, dict):
            continue
        intent = _intent_axis_for(row.get("normalized_query") or row.get("query"), row.get("axis"))
        bucket = buckets.setdefault(intent, {"cited": 0, "sku_cited": 0, "total": 0})
        bucket["total"] += 1
        summary = row.get("source_summary") if isinstance(row.get("source_summary"), dict) else {}
        if int(summary.get("merchant_cited_runs") or 0) > 0:
            bucket["cited"] += 1
        if int(summary.get("sku_cited_runs") or 0) > 0:
            bucket["sku_cited"] += 1
    for bucket in buckets.values():
        bucket["rate"] = round(bucket["cited"] / bucket["total"], 3) if bucket["total"] else 0.0
        bucket["sku_rate"] = round(bucket["sku_cited"] / bucket["total"], 3) if bucket["total"] else 0.0
    return buckets


def _brand_vs_sku_citation(per_prompt: Any) -> Dict[str, Any]:
    """Sibling-conflation summary for the per-SKU report: queries where the
    BRAND was cited but THIS SKU was never verified in any answer — AI is
    answering with the brand's other products / brand-level content, not this
    one. Honest split only; no attempt to name WHICH sibling (that would need
    excerpt attribution we don't have). Additive block beside citation_by_intent."""
    rows = [r for r in (per_prompt or []) if isinstance(r, dict)]
    if not rows:
        return {"detected": False, "brand_only_queries": [], "count": 0}
    brand_only = [
        str(r.get("query") or "").strip()
        for r in rows
        if r.get("brand_cited_sku_absent")
    ]
    sku_cited = sum(
        1 for r in rows
        if int(((r.get("source_summary") or {}).get("sku_cited_runs")) or 0) > 0
    )
    brand_cited = sum(
        1 for r in rows
        if int(((r.get("source_summary") or {}).get("merchant_cited_runs")) or 0) > 0
    )
    return {
        "detected": bool(brand_only),
        "count": len(brand_only),
        "brand_only_queries": brand_only[:8],
        "brand_cited_queries": brand_cited,
        "sku_cited_queries": sku_cited,
        "note": (
            "On these queries AI cites the brand but never identifies this "
            "exact product — brand visibility this SKU doesn't own yet "
            "(often a sibling product carrying the answer)."
        ) if brand_only else None,
    }


def _channel_query_key(query: Any) -> str:
    """Whitespace-collapsed lowercase query key — matches sku_opportunity's
    `_norm_query` so per_prompt rows and RunFacts prompt groups join cleanly."""
    return re.sub(r"\s+", " ", str(query or "").strip().lower())


def build_channel_appearance(
    *,
    per_prompt: Optional[List[Dict[str, Any]]],
    merchant_host: Optional[str],
    retail_channel_host: Optional[str] = None,
    own_cited_by_query: Optional[Mapping[str, bool]] = None,
) -> Dict[str, Any]:
    """Per-product channel-by-channel appearance: across the probed queries, WHERE
    the brand's product shows up in AI answers — the brand's own site vs each
    retailer / marketplace / publisher AI cites instead. The SUBJECT is the
    product; cited third-party hosts are CHANNELS (distribution), not the
    product's identity. Reuses each per_prompt row's cited-host evidence
    (`source_summary.top_cited_hosts` + `merchant_cited_runs`) — no new probes.

    `own_cited_by_query` (W1 site-5 fix, 2026-07-04): per-prompt T1 flags from
    the RunFacts source walk, keyed by `_channel_query_key`. When provided it is
    the source of truth for the own-site row. The legacy fallback — scanning
    `top_cited_hosts` for the own domain — UNDERCOUNTS T1, because that list is
    `extract_cited_hosts`' COMPETITOR rollup: an own-domain source whose label
    names the brand is routed to the merchant bucket and never appears there
    (the "Your site 0/14 next to a cited-everywhere headline" contradiction,
    measured on the 2026-07-04 DamDam run as 0/14 displayed vs 13/14 true).

    Returns {total_queries, own_site_host, own_site_cited(_count), channels[]}
    where each channel is {host, type, type_label, is_own_site, is_your_listing,
    cited_query_count, total_queries, times_cited, intents_cited}, sorted own-site
    first then by how many queries cited it. The own-site row is ALWAYS present
    (even at 0/N) so the merchant sees "your site: cited 0 of N" up front.
    """
    rows = [r for r in (per_prompt or []) if isinstance(r, dict)]
    total = len(rows)
    own = normalize_host(merchant_host) if merchant_host else None
    retail = normalize_host(retail_channel_host) if retail_channel_host else None
    own_cited = 0          # queries where the brand's OWN domain is a cited source
    brand_mentioned = 0    # queries where AI named the brand (possibly via a channel)
    agg: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        ss = r.get("source_summary") if isinstance(r.get("source_summary"), dict) else {}
        if int(ss.get("merchant_cited_runs") or 0) > 0:
            brand_mentioned += 1
        q = r.get("normalized_query") or r.get("query")
        intent = _intent_axis_for(q, r.get("axis"))
        # Honest own-site signal: did the brand's OWN domain appear among the
        # actual cited source hosts for this query? (NOT merchant_cited_runs,
        # which is the softer "brand was named somewhere" signal — a retailer
        # page citing the brand is distribution, not the brand's own page.)
        # Source-walk facts win when supplied; the top_cited_hosts scan below
        # only fills in for legacy callers (see docstring).
        own_cited_here = (
            bool(own_cited_by_query.get(_channel_query_key(q), False))
            if own_cited_by_query is not None
            else False
        )
        for h in (ss.get("top_cited_hosts") or []):
            raw = h.get("host") if isinstance(h, dict) else h
            host = normalize_host(raw)
            if not host:
                continue
            cls = classify_host(host)
            if cls.get("type") == "cdn":
                continue  # asset CDNs aren't a buying channel
            if own and host == own:
                own_cited_here = True
                continue  # the brand's own site is its own channel row
            bucket = agg.setdefault(host, {
                "host": host,
                "type": cls.get("type") or "unclassified",
                "type_label": _cited_host_type_label(cls.get("type")),
                "cited_queries": set(),
                "times_cited": 0,
                "intents": set(),
            })
            bucket["cited_queries"].add(q)
            bucket["times_cited"] += int(
                (h.get("times_cited") if isinstance(h, dict) else 1) or 1
            )
            if intent:
                bucket["intents"].add(intent)
        if own_cited_here:
            own_cited += 1
    channels: List[Dict[str, Any]] = []
    if own:
        channels.append({
            "host": own,
            "type": "own_site",
            "type_label": "Your site",
            "is_own_site": True,
            "is_your_listing": False,
            "cited_query_count": own_cited,
            "total_queries": total,
        })
    for host, bucket in agg.items():
        channels.append({
            "host": host,
            "type": bucket["type"],
            "type_label": bucket["type_label"],
            "is_own_site": False,
            "is_your_listing": bool(retail and host == retail),
            "cited_query_count": len(bucket["cited_queries"]),
            "total_queries": total,
            "times_cited": bucket["times_cited"],
            "intents_cited": sorted(i for i in bucket["intents"] if i),
        })
    channels.sort(
        key=lambda c: (not c["is_own_site"], -c["cited_query_count"], c["host"])
    )
    # No third-party hosts cited AND the brand never appeared anywhere = the AI
    # didn't ground ANY answer this run -> we couldn't measure (transient
    # grounding/quota failure), NOT "your product appears on no channel". The UI
    # must say "couldn't measure, re-run" rather than show an empty channel list
    # as if it were the truth.
    grounding_unavailable = (
        total > 0
        and not agg
        and own_cited == 0
        and brand_mentioned == 0
    )
    return {
        "total_queries": total,
        "own_site_host": own,
        "grounding_unavailable": grounding_unavailable,
        # Your OWN domain cited as a source (the honest "are you the answer?").
        "own_site_cited": own_cited > 0,
        "own_site_cited_count": own_cited,
        # Softer signal: AI named your brand somewhere (often via a channel),
        # even when it didn't cite your own page — kept distinct on purpose.
        "brand_mentioned_count": brand_mentioned,
        "channels": channels,
    }


# Non-branded DISCOVERY intents — where a brand wins NEW demand inside frontier
# models ("best hair oil", "best hair oil for damaged hair"). vs BRANDED intents
# (by name / "is X legit") which are low-value: a shopper who types a product
# name already found it elsewhere, so being cited there isn't the prize.
_DISCOVERY_INTENTS = frozenset({"category_head", "problem_jtbd", "constraint"})
_BRANDED_INTENTS = frozenset({"navigational", "trust"})


# Unambiguous product-form / ingredient nouns that are never a brand's second
# word. Used to decide whether the 2nd leading token belongs to the brand
# (keep "Camille Rose", "Wonder Curl", "Aunt Jackie's") or is the product
# descriptor (drop it: "Cantu Shea Butter" -> "Cantu"). Deliberately excludes
# ambiguous words like "moisture"/"curl"/"repair" that ARE real brand words
# ("Maui Moisture", "Wonder Curl").
_PRODUCT_FORM_WORDS = frozenset({
    "butter", "oil", "cream", "creme", "lotion", "serum", "gel", "milk", "mask",
    "pack", "spray", "balm", "mousse", "wax", "pomade", "shampoo", "conditioner",
    "conditioning", "treatment", "hair", "scalp", "moist",
    "shea", "coconut", "argan", "jojoba", "olive", "castor", "marula", "monoi",
})

# Connector words that bind a brand's tokens together rather than separating the
# brand from a product descriptor. When the 2nd token is one of these, the brand
# spans ACROSS it ("As I Am", "Creme of Nature", "Bumble and bumble"), so the
# 2-token rule would clip mid-name ("As I", "Creme of"). Keep the token after the
# connector too. "&" / "i" are bare-word forms; "&honey" stays one token (no space).
_CONNECTOR_WORDS = frozenset({"and", "of", "the", "&", "i"})


def _competitor_brand_label(name: Any) -> str:
    """Coarse brand label from a competitor product name so a brand's many SKUs
    group into one ("Cantu, Shea Butter, Coconut Curling Cream" / "Cantu Shea
    Butter for…" -> "Cantu"; "&honey Moist Shampoo" -> "&honey"). Keeps genuine
    two-word brands intact ("Camille Rose", "Wonder Curl", "Aunt Jackie's") by
    dropping the 2nd word ONLY when it's an unambiguous product/ingredient noun
    — the old first-word-only rule mangled them to "Camille"/"Wonder"/"Aunt".
    When the 2nd token is a connector ("As I Am", "Creme of Nature", "Bumble and
    bumble"), the brand spans across it, so keep the token after the connector
    too rather than clipping at the connector ("As I" / "Creme of")."""
    s = str(name or "").strip().split(",")[0].strip()
    words = s.split()
    if not words:
        return ""
    second = words[1].strip(".'").lower() if len(words) >= 2 else ""
    if second in _CONNECTOR_WORDS and len(words) >= 3:
        return f"{words[0]} {words[1]} {words[2]}"
    if len(words) >= 2 and second not in _PRODUCT_FORM_WORDS:
        return f"{words[0]} {words[1]}"
    return words[0]


# The shopping-surface models (deepseek is the fact-check/verify pass, not a
# place a shopper discovers products). Gemini and ChatGPT ground DIFFERENT
# indexes/sources, so their wins diverge — and that divergence is actionable
# signal, not noise (win Gemini but lose ChatGPT -> work the sources ChatGPT
# leans on, which differ from Google's).
_SHOPPING_MODELS = ("gemini", "chatgpt")


def _per_model_discovery(
    rows: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Per-model discovery appearance from provider_verdicts (win=appeared,
    loss=grounded-but-not, absent=ungrounded/not-found -> inconclusive), plus
    the per-query divergence between models. Reuses existing per-row verdicts."""
    by_model: Dict[str, Dict[str, Any]] = {}
    divergence: List[Dict[str, Any]] = []
    for r in rows:
        intent = _intent_axis_for(r.get("normalized_query") or r.get("query"), r.get("axis"))
        if intent not in _DISCOVERY_INTENTS:
            continue
        verdicts = r.get("provider_verdicts") if isinstance(r.get("provider_verdicts"), dict) else {}
        q = str(r.get("query") or "").strip()
        wins: Dict[str, bool] = {}
        for m in _SHOPPING_MODELS:
            v = str(verdicts.get(m) or "absent").lower()
            if v not in ("win", "loss"):
                continue  # ungrounded / absent — inconclusive for this model
            slot = by_model.setdefault(m, {"appeared": 0, "total": 0})
            slot["total"] += 1
            won = v == "win"
            wins[m] = won
            if won:
                slot["appeared"] += 1
        graded = [m for m in _SHOPPING_MODELS if m in wins]
        if q and len(graded) >= 2 and len({wins[m] for m in graded}) > 1:
            divergence.append({
                "query": q,
                "won": [m for m in graded if wins[m]],
                "lost": [m for m in graded if not wins[m]],
                # Rides the entry so the niche-first ordering below (and any
                # renderer badge) can exempt spec-matched/merchant prompts.
                "prompt_source": r.get("prompt_source"),
            })
    for slot in by_model.values():
        slot["rate"] = (
            round(slot["appeared"] / slot["total"], 3) if slot["total"] else None
        )
    # Niche-first: divergence[0] is quoted verbatim in the engine-playbook
    # note ('for category queries like "…"') — a specific divergent query
    # makes that example actionable; a head baseline makes it read like the
    # engine gap is about "best headphones".
    divergence.sort(
        key=lambda d: is_broad_head_query(
            d.get("query"), prompt_source=d.get("prompt_source")
        )
    )
    return by_model, divergence[:8]


# Efficacy / certification claims worth THIRD-PARTY proof. When a product makes
# these but the merchant hasn't supplied evidence, AI engines can't substantiate
# them (they cite marketing copy weakly or flag it) — the gap Pivota's commerce
# index uniquely closes by publishing merchant-supplied evidence as grounded,
# citable claims.
_EFFICACY_CLAIM_TERMS = (
    "repair", "repairs", "restore", "restores", "strengthen", "strengthens",
    "reduce", "reduces", "improve", "improves", "treat", "treats", "treatment",
    "clinical", "clinically", "proven", "heal", "heals", "regenerate",
    "regenerates", "prevent", "prevents", "boost", "boosts", "firming",
    "brightening", "anti-aging", "antiaging", "detox", "disulfide", "bond",
)
_CERT_CLAIM_TERMS = (
    "vegan", "cruelty-free", "organic", "halal", "kosher", "dermatologist",
    "dermatologist-tested", "hypoallergenic", "non-toxic", "nontoxic",
    "gluten-free", "paraben-free", "sulfate-free", "fragrance-free", "non-gmo",
    "clinically-tested", "fair-trade", "fairtrade", "fda", "gmp",
)
# Measurable SPEC claims (electronics/wearables) — the beauty-centric lists
# above missed them entirely, so an IP68 swim headphone rendered a generic
# evidence ask with zero specifics (Mojawa verification feedback).
_SPEC_CLAIM_TERMS = (
    "waterproof", "water-resistant", "water resistant", "ip68", "ip67",
    "ipx8", "ipx7", "ip rating", "battery life", "fast charge",
    "fast-charging", "noise-cancelling", "noise cancelling", "mil-spec",
    "drop-tested", "sweatproof",
)
# claim term -> WHAT PROVES IT (deterministic; the merchant asked 'what
# point should I prove?' — every listed claim gets a concrete answer).
_CLAIM_PROOF_HINTS = (
    (("ip68", "ip67", "ipx8", "ipx7", "ip rating", "waterproof",
      "water-resistant", "water resistant", "sweatproof"),
     "the IP-rating test certificate from an accredited lab (name, report "
     "number, date)"),
    (("battery life", "fast charge", "fast-charging"),
     "a manufacturer or third-party test report with the measured figures "
     "and test conditions"),
    (("noise-cancelling", "noise cancelling"),
     "a lab measurement report (attenuation dB across frequencies)"),
    (("mil-spec", "drop-tested"),
     "the MIL-STD test report or drop-test certification"),
    (("vegan", "cruelty-free"),
     "the certifying body's certificate (e.g. Leaping Bunny / Vegan Society "
     "— name + license number)"),
    (("organic",),
     "the USDA / ECOCERT / COSMOS certificate for the certified ingredients"),
    (("dermatologist", "dermatologist-tested", "hypoallergenic",
      "clinically-tested", "clinical", "clinically"),
     "the study or test report (who ran it, N, protocol, result)"),
    (("fda", "gmp"),
     "the registration / facility certificate number"),
    (("paraben-free", "sulfate-free", "fragrance-free", "gluten-free",
      "non-gmo", "non-toxic", "nontoxic"),
     "the full ingredient list (INCI) or a certificate of analysis"),
)


def _proof_hint_for(term: str) -> str:
    low = term.lower()
    for keys, hint in _CLAIM_PROOF_HINTS:
        if low in keys:
            return hint
    return (
        "third-party proof: a lab test, certification, study, or the "
        "underlying spec/ingredient data"
    )


def _detect_claim_terms(text: str, terms: Tuple[str, ...]) -> List[str]:
    low = (text or "").lower()
    return [t for t in terms if re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", low)]


def build_evidence_play(
    *,
    product: Optional[Mapping[str, Any]],
    sku_ctx: Optional[Mapping[str, Any]],
    verify_summary: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The Pivota-moat action: supply lab reports / clinical evidence /
    certifications so Pivota can publish them as GROUNDED, CITABLE claims on the
    merchant's canonical PDP / commerce index. Fires when the product makes
    substantiation-worthy claims (or AI flagged unsupported answers about it) but
    the merchant hasn't supplied evidence yet — the one lever no retailer,
    marketplace, or publisher can offer the brand. Deterministic; no probes."""
    prod = product if isinstance(product, Mapping) else {}
    ctx = sku_ctx if isinstance(sku_ctx, Mapping) else {}
    already = _has_substantiation(dict(prod), dict(ctx))
    text = " ".join(str(v) for v in (
        prod.get("title"), prod.get("raw_title"), prod.get("description"),
        prod.get("product_type"), _json_obj(prod.get("attributes_raw")),
    ) if v)
    efficacy = _detect_claim_terms(text, _EFFICACY_CLAIM_TERMS)
    certs = _detect_claim_terms(text, _CERT_CLAIM_TERMS)
    specs = _detect_claim_terms(text, _SPEC_CLAIM_TERMS)
    flagged = int((verify_summary or {}).get("flagged") or 0)
    present = bool(efficacy or certs or specs or flagged) and not already
    moves: List[str] = []
    if present:
        if efficacy:
            moves.append(
                "Supply third-party proof (lab test, clinical study, or "
                f"ingredient data) for your efficacy claims ({', '.join(efficacy[:4])}) "
                "— Pivota publishes them as grounded, citable claims on your "
                "canonical PDP so AI cites the evidence, not just marketing copy."
            )
        if certs:
            moves.append(
                f"Upload your certifications ({', '.join(certs[:4])}) for Pivota to "
                "publish as verifiable claims AI engines can cite."
            )
        if flagged:
            # Flags are factual-only now — say what was actually found (wrong
            # facts in AI answers), not "unsupported" (the retired editorial
            # framing), and pitch evidence as the correction channel.
            moves.append(
                f"AI stated wrong facts about your product in {flagged} "
                "checked answer(s) — publishing your evidenced facts on the "
                "canonical page gives AI a source to correct against."
            )
        if not moves:
            moves.append(
                "Supply product evidence (specs, sourcing, test results, "
                "certifications) for Pivota to publish as grounded claims on your "
                "canonical PDP."
            )
    # Per-claim proof checklist: 'what point should I prove, with what?'
    all_claims = list(dict.fromkeys(specs + efficacy + certs))
    evidence_checklist = [
        {"claim": term, "prove_with": _proof_hint_for(term)}
        for term in all_claims[:6]
    ]
    # The SPECIFIC answers verify flagged (query + why), so 'N answers
    # flagged' is actionable instead of a bare count. Flags are factual-only
    # now; the else-branch survives defensively for old persisted summaries.
    flagged_answers = [
        {
            "query": q,
            "why": (
                "the answer misstates facts about your product"
                if probe.get("misstates_facts")
                else "the answer couldn't support recommending you"
            ),
            "note": str(probe.get("note") or "").strip()[:300],
        }
        for probe in ((verify_summary or {}).get("flagged_probes") or [])
        if isinstance(probe, Mapping)
        and (q := str(probe.get("query") or "").strip())
    ][:3]
    return {
        "present": present,
        "already_substantiated": already,
        "claims_to_substantiate": specs[:5] + efficacy[:5] + certs[:5],
        "evidence_checklist": evidence_checklist,
        "flagged_answers": flagged_answers,
        "unsubstantiated_in_ai": flagged,
        "moves": moves,
        "pivota_value": (
            "Pivota's commerce index turns merchant-supplied evidence into "
            "grounded, citable claims — a trust signal AI engines reward and that "
            "retailers, marketplaces, and publishers can't provide for the brand."
        ),
    }


def build_product_competitiveness(per_prompt: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Product-FIRST view: does the product WIN non-branded discovery demand
    inside frontier models — where the brand gains NEW buyers — and who does AI
    recommend instead? Branded name queries are reported separately as low-value.
    Reuses per_prompt cited evidence + competitors; no new probes.

    Honesty: a query only counts when the AI actually GROUNDED its answer (cited
    sources). An ungrounded probe (the engine answered with no web sources — a
    transient grounding/quota failure, not "the product is invisible") is
    excluded from the denominator and tallied as `ungrounded`. When discovery
    queries ran but NONE grounded, `grounding_unavailable` is set so the UI says
    "couldn't measure, re-run" instead of a false "appears in 0 of N".

    Returns {has_discovery, grounding_unavailable, discovery:{appeared,total,
    rate,ungrounded,missed[],top_competitors[]}, branded:{appeared,total,rate}}.
    """
    rows = [r for r in (per_prompt or []) if isinstance(r, dict)]
    disc_total = disc_appeared = disc_ungrounded = 0
    # Of the discovery appearances, how many are the brand RECOMMENDED by an
    # independent source vs merely its OWN listing being retrieved for the
    # category query (findability, not endorsement — see
    # `appearance_via_listing`). "7/9 appears" overstates competitiveness when
    # all 7 are the brand's own listing surfacing.
    disc_appeared_recommended = disc_appeared_listing = 0
    br_total = br_appeared = 0
    # (query, prompt_source) pairs — ordered niche-first + flattened to
    # strings at payload build.
    missed: List[Tuple[str, Any]] = []
    comp_counts: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        intent = _intent_axis_for(r.get("normalized_query") or r.get("query"), r.get("axis"))
        # APPEARANCE = the per-model VERDICT. The MEANING depends on the scan
        # mode the query was probed under (routed by intent — see
        # _scan_mode_for_query_spec): a DISCOVERY query ran under
        # category_visibility_test, so win = the brand surfaced ORGANICALLY in
        # the model's grounded answer to a category question that did NOT name
        # it (genuine "wins the category"); a BRANDED query ran under
        # open_product_visibility_test, so win = the NAMED product was
        # retrievable (findability, often VIA A RETAILER). Either way it's the
        # SAME signal by_model uses, so aggregate and per-model can't
        # contradict. (Whether the brand's OWN page is the cited source is a
        # separate, channel-level signal — see channel_appearance.own_site_cited
        # — NOT this.) Grounded = at least one shopping model graded the query
        # (win/loss); all "absent" = the AI didn't ground it (inconclusive), not
        # "the product is invisible".
        verdicts = r.get("provider_verdicts") if isinstance(r.get("provider_verdicts"), dict) else {}
        graded = [
            m for m in _SHOPPING_MODELS
            if str(verdicts.get(m) or "absent").lower() in ("win", "loss")
        ]
        grounded = bool(graded)
        appeared = any(str(verdicts.get(m) or "").lower() == "win" for m in graded)
        if intent in _DISCOVERY_INTENTS:
            if not grounded:
                disc_ungrounded += 1
                continue  # inconclusive — don't count as appeared OR missed
            disc_total += 1
            if appeared:
                disc_appeared += 1
                if r.get("appearance_via_listing"):
                    disc_appeared_listing += 1
                else:
                    disc_appeared_recommended += 1
            else:
                q = str(r.get("query") or "").strip()
                if q:
                    # Keep the row's generator stamp so the niche-first
                    # ordering below can exempt spec-matched/merchant prompts.
                    missed.append((q, r.get("prompt_source")))
            for comp in (r.get("competitors") or []):
                label = _competitor_brand_label(comp)
                if not label:
                    continue
                slot = comp_counts.setdefault(label.lower(), {"name": label, "queries": set()})
                slot["queries"].add(r.get("normalized_query") or r.get("query"))
        elif intent in _BRANDED_INTENTS:
            if not grounded:
                continue
            br_total += 1
            if appeared:
                br_appeared += 1
    top_competitors = sorted(
        (
            {"name": v["name"], "query_count": len(v["queries"])}
            for v in comp_counts.values()
        ),
        key=lambda c: -c["query_count"],
    )[:6]
    by_model, model_divergence = _per_model_discovery(rows)
    return {
        "has_discovery": disc_total > 0,
        # Discovery queries ran but the AI grounded NONE of them -> we couldn't
        # measure this run (vs has_discovery False = no discovery queries built).
        "grounding_unavailable": disc_total == 0 and disc_ungrounded > 0,
        "discovery": {
            "appeared": disc_appeared,
            "total": disc_total,
            "rate": round(disc_appeared / disc_total, 3) if disc_total else None,
            # Endorsement vs findability split of the appearances: recommended
            # by an independent source vs the brand's own listing merely being
            # retrieved for the category query. recommended is the real
            # category-competitiveness signal.
            "appeared_recommended": disc_appeared_recommended,
            "appeared_listing": disc_appeared_listing,
            "ungrounded": disc_ungrounded,
            # Niche-first: missed arrives in probe order (head baselines
            # first), and downstream surfaces read missed[0] as "the category
            # ask to go win" (e.g. the get-cited category hint) — a specific
            # missed query must lead; head rows trail as honest measurements.
            "missed": [
                q
                for q, src in sorted(
                    missed,
                    key=lambda t: is_broad_head_query(t[0], prompt_source=t[1]),
                )
            ][:8],
            "top_competitors": top_competitors,
        },
        "branded": {
            "appeared": br_appeared,
            "total": br_total,
            "rate": round(br_appeared / br_total, 3) if br_total else None,
        },
        # Per-model discovery appearance + divergence: Gemini and ChatGPT ground
        # different indexes, so a product can win one and lose the other — the
        # merchant should act per model (e.g. win ChatGPT by earning the
        # editorial/Bing-indexed sources it leans on). Aggregate above stays.
        "by_model": by_model,
        "model_divergence": model_divergence,
    }


# How each frontier engine GROUNDS its answers — the basis for per-engine ops.
# Gemini retrieves from Google's web index + the review/editorial sources Google
# trusts; ChatGPT retrieves via Bing/OpenAI and leans heavily on Reddit +
# community discussion + independent review sites. Winning each is a different
# game, so the audit must prescribe DIFFERENT moves per engine.
_ENGINE_PROFILES: Dict[str, Dict[str, str]] = {
    "gemini": {
        "label": "Gemini (Google index)",
        "how_it_cites": (
            "Gemini grounds answers in Google's web index and the review / "
            "editorial sources Google trusts."
        ),
    },
    "chatgpt": {
        "label": "ChatGPT (Bing + community)",
        "how_it_cites": (
            "ChatGPT grounds via Bing/OpenAI search and leans heavily on Reddit, "
            "community threads, and independent review sites."
        ),
    },
}


def _engine_appearance_status(appeared: int, total: int) -> str:
    if not total:
        return "couldnt_measure"
    if appeared <= 0:
        return "invisible"
    return "weak" if (appeared / total) < 0.5 else "present"


def _cited_hosts_by_type(channel_appearance: Optional[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """Group this product's cited third-party hosts by classified type (from
    channel_appearance) so per-engine moves can name the REAL sources AI cites
    for the category, not a generic 'pitch a publisher'."""
    out: Dict[str, List[str]] = {}
    ca = channel_appearance if isinstance(channel_appearance, Mapping) else {}
    for ch in (ca.get("channels") or []):
        if not isinstance(ch, Mapping) or ch.get("is_own_site"):
            continue
        host = str(ch.get("host") or "").strip()
        htype = str(ch.get("type") or "unclassified").strip().lower()
        if host and host not in out.setdefault(htype, []):
            out[htype].append(host)
    return out


def _engine_moves(engine: str, status: str, hosts_by_type: Mapping[str, List[str]]) -> List[str]:
    editorial = list(hosts_by_type.get("editorial") or [])
    community = list(hosts_by_type.get("community") or []) + list(hosts_by_type.get("forum") or [])
    if engine == "gemini":
        moves = [
            "Get your official product page indexed by Google and verify it in "
            "Search Console — Gemini can only cite pages in Google's index.",
        ]
        if editorial:
            moves.append(
                "Earn a review/listing on the Google-trusted sources already "
                f"cited for your category: {', '.join(editorial[:3])}."
            )
        else:
            moves.append(
                "Earn placement in the independent review sources Google "
                "surfaces for your category (ingredient/efficacy explainers and "
                "'best of' roundups)."
            )
        moves.append(
            "Add product, review, and FAQ structured data so Google can extract "
            "your product facts and claims."
        )
        return moves
    # chatgpt
    moves = [
        "Seed accurate product info and earn authentic reviews on Reddit and "
        "niche community threads — ChatGPT weights Reddit and community "
        "discussion heavily."
        + (f" (it already cites {', '.join(community[:2])} for your category)." if community else ""),
        "Get reviewed on independent review sites and confirm your page is in "
        "Bing's index (Bing Webmaster Tools) — ChatGPT retrieves via Bing.",
    ]
    if status in ("weak", "present"):
        moves.append(
            "ChatGPT already surfaces you (often via a retailer listing) — "
            "convert that into a recommendation by building review depth and "
            "community mentions, not just a buyable listing."
        )
    return moves


_ENGINE_NOTE_LABELS: Dict[str, str] = {
    "gemini": "Gemini",
    "chatgpt": "ChatGPT",
    "openai": "ChatGPT",
    "deepseek": "DeepSeek",
    "claude": "Claude",
}


def _engine_note_label(engine: str) -> str:
    """Short, capitalized engine name for merchant-facing note copy — never the
    raw lowercase key ('chatgpt')."""
    key = str(engine or "").strip().lower()
    return _ENGINE_NOTE_LABELS.get(key, key.title() or key)


def _humanize_provider_list(providers: List[str]) -> str:
    """['gemini', 'chatgpt'] -> 'Gemini and ChatGPT'. Mirrors the URL-audit
    methodology label in routes/merchant_audit_routes._humanize_provider_list so
    both audit surfaces name the models that ACTUALLY ran instead of a
    hardcoded 'Gemini'. De-dupes on display name (openai/chatgpt collapse)."""
    names: List[str] = []
    for provider in providers or []:
        label = _engine_note_label(provider)
        if label and label not in names:
            names.append(label)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def build_engine_playbook(
    *,
    per_prompt: Optional[List[Dict[str, Any]]],
    channel_appearance: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Per-ENGINE operating plan. Gemini and ChatGPT cite different indexes, so
    the same product can be invisible on one and findable on the other — and the
    moves to win each differ. Reuses the per-model discovery appearance +
    divergence (no new probes); names the real cited sources per engine where
    available. Returns {has_signal, primary_gap, engines{gemini,chatgpt:{label,
    how_it_cites,appeared,total,rate,status,moves[]}}, divergence[], divergence_note}.
    """
    rows = [r for r in (per_prompt or []) if isinstance(r, dict)]
    by_model, divergence = _per_model_discovery(rows)
    hosts_by_type = _cited_hosts_by_type(channel_appearance)
    engines: Dict[str, Any] = {}
    for engine, profile in _ENGINE_PROFILES.items():
        slot = by_model.get(engine) or {}
        appeared = int(slot.get("appeared") or 0)
        total = int(slot.get("total") or 0)
        status = _engine_appearance_status(appeared, total)
        engines[engine] = {
            "label": profile["label"],
            "how_it_cites": profile["how_it_cites"],
            "appeared": appeared,
            "total": total,
            "rate": (round(appeared / total, 3) if total else None),
            "status": status,
            "moves": _engine_moves(engine, status, hosts_by_type),
        }
    # Primary gap = the measured engine where the brand is weakest (most upside).
    measured = {
        e: v for e, v in engines.items() if v["status"] != "couldnt_measure"
    }
    primary_gap = None
    if measured:
        primary_gap = min(measured, key=lambda e: (measured[e]["rate"] or 0.0))
    note = None
    if divergence:
        won = {m for d in divergence for m in (d.get("won") or [])}
        lost = {m for d in divergence for m in (d.get("lost") or [])}
        # Aggregating won/lost across ALL divergent queries is self-contradictory
        # when each engine surfaces the product on DIFFERENT queries (won ∩ lost
        # is non-empty) — it produced "you surface on chatgpt, gemini but not
        # chatgpt, gemini". Only make the directional claim on the CLEAN split
        # (one engine consistently ahead); otherwise say the honest mixed thing.
        clean_won = sorted(won - lost, key=_engine_note_label)
        clean_lost = sorted(lost - won, key=_engine_note_label)
        example = str(divergence[0].get("query") or "").strip()
        if clean_won and clean_lost:
            won_names = ", ".join(_engine_note_label(m) for m in clean_won)
            lost_names = ", ".join(_engine_note_label(m) for m in clean_lost)
            note = (
                f"You surface on {won_names} but not {lost_names}"
                + (f" for category queries like \"{example}\"" if example else "")
                + f" — closing the {lost_names} gap is the per-engine priority."
            )
        elif example:
            note = (
                "Gemini and ChatGPT each surface you on different category "
                f"queries (e.g. \"{example}\") — work each engine's gaps "
                "separately."
            )
    return {
        "has_signal": bool(measured),
        "primary_gap": primary_gap,
        "engines": engines,
        "divergence": divergence,
        "divergence_note": note,
    }


def _brand_citation_by_intent(per_sku_reports: Any) -> Dict[str, Dict[str, Any]]:
    """Brand-level roll-up of per-SKU `citation_by_intent` (sums cited/total per
    intent across SKUs; reads the per-SKU dicts, no new probes)."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for report in per_sku_reports or []:
        if not isinstance(report, dict):
            continue
        per_sku = report.get("citation_by_intent")
        if not isinstance(per_sku, dict):
            continue
        for intent, stats in per_sku.items():
            if not isinstance(stats, dict):
                continue
            bucket = buckets.setdefault(intent, {"cited": 0, "total": 0, "skus": 0})
            bucket["cited"] += int(stats.get("cited") or 0)
            bucket["total"] += int(stats.get("total") or 0)
            bucket["skus"] += 1
    for bucket in buckets.values():
        bucket["rate"] = round(bucket["cited"] / bucket["total"], 3) if bucket["total"] else 0.0
    return buckets


def _store_as_destination(
    citation_by_intent: Optional[Dict[str, Any]],
    authority_hosts: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """R3 — the retailer's real win: for BUY-INTENT queries ("where to buy X", the
    navigational axis), is the merchant's STORE the AI-routed buying destination, and
    who does AI route buyers to INSTEAD? A retailer wins by being where AI sends the
    buyer (vs Amazon / the brand's own site / another retailer), not by the brands it
    carries being recommended. Reuses existing data — no new probes:
      - rate = the navigational citation rate (the store cited on buy-intent queries;
        merchant identity is store-only after the R1 retailer-aware fix).
      - routed_to_instead = the hosts AI cited on branded/navigational (buy-intent)
        queries, excluding the store — the destinations it sent buyers to instead.
    """
    nav = (citation_by_intent or {}).get("navigational") or {}
    total = int(nav.get("total") or 0)
    cited = int(nav.get("cited") or 0)
    routed: List[Dict[str, Any]] = []
    seen: set = set()
    for row in authority_hosts or []:
        if not isinstance(row, dict) or not row.get("cited_on_branded_query"):
            continue
        if row.get("citation_role") == ROLE_OWN_DOMAIN:
            continue  # the store itself, not a competing destination
        host = row.get("host")
        if not host or host in seen:
            continue
        seen.add(host)
        advice = _channel_competition_advice(row.get("citation_role"))
        routed.append({
            "host": host,
            "role": row.get("citation_role"),
            "role_label": advice["role_label"],
            "times_cited": int(row.get("prompts_cited_count") or 0),
            "how_to_compete": advice["how_to_compete"],
        })
    routed.sort(key=lambda r: -r["times_cited"])
    return {
        "rate": round(cited / total, 3) if total else 0.0,
        "cited": cited,
        "total": total,
        "routed_to_instead": routed[:8],
    }


_BRAND_MATCH_STOPWORDS = frozenset({
    "co", "ltd", "inc", "llc", "gmbh", "corp", "company", "the", "of", "and",
    "global", "official", "store", "shop",
})


def _brand_core_words(name: str) -> set:
    """The significant words of a brand/product name (normalized via
    derive_brand_aliases, legal/stop words dropped). Two names refer to the same
    brand when their core words overlap — robust to legal suffixes + extra product
    words ('NUTRIONE BB Lab' shares 'nutrione' with catalog brand 'NUTRIONE CO
    LTD'). Errs toward MORE overlap, so the C3 match errs toward 'carried' — it
    never falsely tells a merchant to stock something they already have."""
    words: set = set()
    for form in derive_brand_aliases(name or ""):
        for w in str(form).split():
            w = w.strip().lower()
            if len(w) >= 2 and w not in _BRAND_MATCH_STOPWORDS:
                words.add(w)
    return words


async def _carried_brand_words(merchant_id: str) -> frozenset:
    """C3 — the significant brand words across every brand the merchant CARRIES
    (its catalog brands). Best-effort: empty set on failure (caller suppresses C3
    rather than emit false 'not carried' suggestions)."""
    from db.database import database

    try:
        rows = await database.fetch_all(
            "SELECT DISTINCT brand FROM catalog_products "
            "WHERE merchant_id = :m AND brand IS NOT NULL AND brand <> '' "
            "AND sync_status = 'live'",
            {"m": merchant_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "C3 carried-brands fetch failed for %s: %s", merchant_id, str(exc)[:200]
        )
        return frozenset()
    words: set = set()
    for r in rows or []:
        words |= _brand_core_words(r["brand"] or "")
    return frozenset(words)


async def _winning_products_not_carried(
    merchant_id: str,
    win_plan: Optional[Dict[str, Any]],
    *,
    cap: int = 12,
    vertical_profile: VerticalProfile = BEAUTY_PROFILE,
) -> List[Dict[str, Any]]:
    """C3 — for a RESELLER: the winning competitor products AI names that the
    merchant does NOT carry (a stocking / sourcing signal). Collect the competitor
    benchmark names across losing queries, drop ingredient/category noise
    (filter_competitor_brands), and exclude any whose brand-forms the merchant
    already carries (derive_brand_aliases vs the catalog brands). A frontier LLM
    can't produce this — it needs the merchant's catalog x the measured winners.
    Ranked by how often AI names each; capped. Empty when the merchant's carried
    brands can't be loaded (never emit a false 'not carried' suggestion)."""
    if not isinstance(win_plan, dict) or not win_plan.get("sku_plans"):
        return []
    agg: Dict[str, Dict[str, Any]] = {}
    for plan in win_plan.get("sku_plans") or []:
        if not isinstance(plan, dict):
            continue
        for q in plan.get("losing_queries") or []:
            if not isinstance(q, dict):
                continue
            query = str(q.get("query") or "").strip()
            for name in filter_competitor_brands(
                list(q.get("competitor_benchmark") or []),
                ingredient_tokens=vertical_profile.competitor_ingredient_tokens,
                form_tokens=vertical_profile.competitor_form_tokens,
            ):
                key = name.strip().lower()
                if not key:
                    continue
                entry = agg.setdefault(
                    key, {"name": name.strip(), "times_named": 0, "example_queries": []}
                )
                entry["times_named"] += 1
                if query and query not in entry["example_queries"]:
                    entry["example_queries"].append(query)
    if not agg:
        return []
    carried_words = await _carried_brand_words(merchant_id)
    if not carried_words:
        # Couldn't establish what the merchant carries — suppress rather than
        # emit every competitor as a false "you don't carry this".
        return []
    out: List[Dict[str, Any]] = []
    for entry in agg.values():
        if _brand_core_words(entry["name"]) & carried_words:
            continue  # shares a brand word with the catalog -> treat as carried
        entry["example_queries"] = entry["example_queries"][:3]
        out.append(entry)
    out.sort(key=lambda r: -r["times_named"])
    return out[:cap]


def _grounding_evidence(probe_runs: Any, cap: int = 12) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []
    for run in _flatten_probe_runs(probe_runs):
        sources = run.get("grounding_sources") or []
        parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
        excerpt = (
            run.get("evidence_excerpt")
            or parsed.get("evidence_excerpt")
            or parsed.get("evidence_text")
        )
        if not sources and not excerpt:
            continue
        evidence.append({
            "probe_run_id": run.get("_probe_run_id"),
            "query": run.get("query"),
            "axis_metadata": run.get("axis_metadata"),
            "grounding_sources": sources,
            "evidence_excerpt": excerpt or None,
            # Whether the SKU was actually found in this answer — lets the
            # narrative use an excerpt as "what's working" proof only when it is
            # a positive result, never a "couldn't find it" line (Fix 3).
            "product_visible": bool(
                parsed.get("correct_sku")
                or parsed.get("sku_mentioned")
                or parsed.get("product_visible")
            ),
        })
        if len(evidence) >= cap:
            break
    return evidence


def _failing_prompts(
    probe_runs: Any,
    cap: int = 20,
    *,
    brand_lower: str = "",
    brand_aliases: Tuple[str, ...] = (),
) -> List[Dict[str, Any]]:
    """One entry per UNIQUE failing query (#1502).

    Probes run per provider × per repeat, so a single failing query used to
    emit one entry per failing RUN — merchant reports listed "best camera
    drone" 2-3× per SKU, scaling with provider count (worst measured: 19
    duplicate rows across one 3-SKU us_shopper run). The earlier duplicate fix
    lived only in the win-plan CONSUMER (its per-query merge, win_plan_builder);
    this dedupes at the source so every consumer (report UI, playbook,
    outreach, summary) sees unique queries. Runs for a query already listed
    still merge their evidence in: `providers` unions every failing engine
    (win plan reads it, falling back to the legacy singular `provider`),
    grounding_sources extend (host union is deduped downstream), and
    competitors_named unions order-preserving. The cap counts unique queries.
    """
    out: List[Dict[str, Any]] = []
    by_query: Dict[str, Dict[str, Any]] = {}
    for run in _flatten_probe_runs(probe_runs):
        # Internal-first: a failing comparison probe would surface competitor
        # names in the merchant's failing-prompts list — exactly the prose the
        # v1 gate exists to hold back.
        if _run_is_internal_comparison(run):
            continue
        parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
        url_match = run.get("url_match") if isinstance(run.get("url_match"), dict) else {}
        llm_report = url_match.get("llm_self_report") if isinstance(url_match.get("llm_self_report"), dict) else {}
        ok = bool(
            parsed.get("correct_sku") is True
            or parsed.get("sku_mentioned") is True
            or llm_report.get("correct_sku") is True
            or llm_report.get("sku_mentioned") is True
            or url_match.get("in_grounding") is True
            or run.get("product_visible") is True
        )
        if ok:
            continue
        provider = str(
            run.get("_provider") or run.get("provider") or ""
        ).strip().lower() or None
        # Strip the merchant's own brand/aliases: this list feeds rival-
        # framing copy (win-plan competitor benchmark, playbook pitch drafts
        # + next_best_action competitor phrases via failed_queries_detailed),
        # where surfacing the merchant as its own "competitor" reads as a
        # bug. Same own-brand filter as the authority-map path (#1384).
        competitors = _strip_own_brand_competitors(
            parsed.get("competitors_listed")
            or parsed.get("competitors_appearing")
            or run.get("competitors_listed")
            or [],
            brand_lower,
            brand_aliases,
        )
        query_key = re.sub(r"\s+", " ", str(run.get("query") or "").strip().lower())
        existing = by_query.get(query_key) if query_key else None
        if existing is not None:
            # Duplicate failing run of an already-listed query — merge evidence.
            if provider and provider not in existing["providers"]:
                existing["providers"].append(provider)
            existing["grounding_sources"] = list(existing.get("grounding_sources") or []) + list(run.get("grounding_sources") or [])
            for name in competitors or []:
                if name not in existing["competitors_named"]:
                    existing["competitors_named"].append(name)
            if not existing.get("prompt_source") and isinstance(run.get("axis_metadata"), dict):
                existing["prompt_source"] = (run.get("axis_metadata") or {}).get("prompt_source")
            continue
        if len(out) >= cap:
            # Cap reached for NEW queries; keep scanning so later duplicate
            # runs of listed queries still merge their evidence above.
            continue
        entry = {
            "query": run.get("query"),
            "axis": (run.get("axis_metadata") or {}).get("axis") if isinstance(run.get("axis_metadata"), dict) else None,
            "reason": "no first-party or correct-SKU grounded citation",
            "evidence_run_id": run.get("_probe_run_id"),
            # Which engine(s) failed this query. `provider` (first engine)
            # stays for back-compat (interleave_by_provider + old readers);
            # `providers` carries the full failing-engine union for the win
            # plan's label.
            "provider": provider,
            "providers": [provider] if provider else [],
            # Generator stamp (llm_winnable/llm_scenario) — the win plan uses
            # it to never treat a specific LLM discovery prompt as a bare head
            # term when gating the own-content win condition.
            "prompt_source": (
                (run.get("axis_metadata") or {}).get("prompt_source")
                if isinstance(run.get("axis_metadata"), dict)
                else None
            ),
            "grounding_sources": run.get("grounding_sources") or [],
            "competitors_named": competitors,
        }
        if query_key:
            by_query[query_key] = entry
        out.append(entry)
    return out


def _custom_prompt_runs_by_prompt(
    probe_runs_by_sku: Any,
) -> Dict[str, List[Dict[str, Any]]]:
    """Group axis="custom" probe runs by their prompt text.

    Custom prompts are probed once (attached to the first SKU — see
    run_per_sku_audit_probe_fanout), so they live in whichever SKU's probe
    payload they rode on. Tolerant of either the per-SKU mapping
    ({sku_key: [probe_payload, ...]}) or a bare probe-payload list.
    """
    groups = (
        probe_runs_by_sku.values()
        if isinstance(probe_runs_by_sku, dict)
        else _json_list(probe_runs_by_sku)
    )
    by_prompt: Dict[str, List[Dict[str, Any]]] = {}
    for sku_runs in groups:
        for run in _flatten_probe_runs(sku_runs):
            meta = run.get("axis_metadata")
            axis = (
                str((meta or {}).get("axis") or "")
                if isinstance(meta, dict)
                else str(run.get("axis") or "")
            ).strip().lower()
            if axis != "custom":
                continue
            # Per-SKU merchant prompts (custom_prompts_by_url, incl. their
            # pinned re-probes on later runs) belong to the per-SKU surfaces —
            # keep them out of the brand-level "Your prompts" panel.
            if (
                isinstance(meta, dict)
                and str(meta.get("custom_scope") or "").strip().lower() == "sku"
            ):
                continue
            prompt = str(run.get("query") or "").strip()
            if not prompt:
                continue
            by_prompt.setdefault(prompt, []).append(run)
    return by_prompt


def _custom_prompt_evidence_excerpt(runs: List[Dict[str, Any]]) -> Optional[str]:
    for run in runs:
        parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
        excerpt = (
            run.get("evidence_excerpt")
            or parsed.get("evidence_excerpt")
            or parsed.get("evidence_text")
            or parsed.get("answer")
        )
        text = str(excerpt or "").strip()
        if text:
            return text[:600]
    return None


def build_custom_prompt_results(
    probe_runs_by_sku: Any,
    custom_prompts: Optional[List[str]] = None,
    *,
    merchant_host: Optional[str] = None,
    merchant_brand: Optional[str] = None,
    merchant_vendors: Optional[Tuple[str, ...]] = None,
) -> List[Dict[str, Any]]:
    """Per-prompt outcomes for the merchant's custom ("Your Prompts") slots.

    The custom prompts a merchant adds to a per-SKU audit are probed once
    (axis="custom") but the per-SKU scorecard only scores the auto-generated
    branded/category queries — so those probes ran and persisted without ever
    being surfaced. This turns them into the open-vs-contested-lane table the
    feature promises: per prompt → was the brand cited, the sources the AI
    grounded in, and which competitors it named instead.

    Pure function over the persisted probe runs (no DB, no LLM). "cited" is the
    brand-grounding signal the rest of the report uses — the merchant's site or
    brand name appeared as a grounding source for at least one provider run
    (alias-aware via extract_cited_hosts), never fabricated. `custom_prompts`
    (the originally-requested slots) is optional; when given, prompts that
    produced zero runs are surfaced honestly as `no_signal` instead of being
    silently dropped (same honesty rule as the billed-but-never-probed bug #820
    closed).
    """
    runs_by_prompt = _custom_prompt_runs_by_prompt(probe_runs_by_sku)

    # Preserve the merchant's requested order when we have it; otherwise fall
    # back to discovery order. Requested-but-unprobed prompts are appended so a
    # dropped/failed slot reads as "no signal yet", not "absent from the lane".
    ordered_prompts: List[str] = []
    seen_prompt_keys: set[str] = set()
    for prompt in custom_prompts or []:
        text = str(prompt or "").strip()
        key = text.lower()
        if text and key not in seen_prompt_keys:
            seen_prompt_keys.add(key)
            ordered_prompts.append(text)
    for prompt in runs_by_prompt:
        if prompt.lower() not in seen_prompt_keys:
            seen_prompt_keys.add(prompt.lower())
            ordered_prompts.append(prompt)

    results: List[Dict[str, Any]] = []
    for prompt in ordered_prompts:
        runs = [
            run
            for run in runs_by_prompt.get(prompt, [])
            if str(run.get("status") or "") != "probe_failed"
        ]
        if not runs:
            results.append({
                "prompt": prompt,
                "cited": False,
                "lane": "no_signal",
                "runs": 0,
                "runs_cited": 0,
                "cited_sources": [],
                "grounding_sources": [],
                "competitors": [],
                "competitors_count": 0,
                "evidence_excerpt": None,
            })
            continue

        competitors, merchant_cited_runs, runs_with_any = extract_cited_hosts(
            runs,
            merchant_host=merchant_host,
            merchant_brand=merchant_brand,
            merchant_vendors=merchant_vendors,
        )
        # W1 T3 secondary site (per-prompt lane classifier). This is one of the
        # extract_cited_hosts scalar reads NOT yet on RunFacts — it gates the
        # rendered lane verdict (open/contested/absent). Instrumented log-only so
        # the next multi-merchant run yields its drift before we decide the flip;
        # per-prompt runs, so the aggregator sees one check per prompt group.
        try:
            _lane_facts = compute_run_facts(
                runs,
                merchant_host=merchant_host,
                merchant_brand=merchant_brand,
                merchant_vendors=merchant_vendors,
            )
            parity_check(
                "bd_report.prompt_lane.merchant_cited_runs",
                merchant_cited_runs,
                _lane_facts.brand_mentioned_runs,
                context={"prompt": str(prompt)[:80]},
            )
            parity_check(
                "bd_report.prompt_lane.runs_with_any",
                runs_with_any,
                _lane_facts.runs_with_citations,
                context={"prompt": str(prompt)[:80]},
            )
        except Exception:  # noqa: BLE001 — parity must never sink the report
            logger.warning("run_facts parity (prompt_lane) failed", exc_info=True)

        # Distinct sources the AI grounded in, split into "the brand" vs
        # everyone else. Dedup across this prompt's provider runs by source key.
        grounding_sources: List[str] = []
        cited_sources: List[str] = []
        seen_source_keys: set[str] = set()
        for run in runs:
            for src in _identify_run_sources(run):
                label = str(src.get("label") or "").strip()
                if not label:
                    continue
                key = str(src.get("key") or label.lower())
                if key in seen_source_keys:
                    continue
                seen_source_keys.add(key)
                grounding_sources.append(label)
                if _source_matches_merchant(
                    src,
                    merchant_host=merchant_host,
                    merchant_brand=merchant_brand,
                    merchant_vendors=merchant_vendors,
                ):
                    cited_sources.append(label)

        competitor_labels = [label for label, _count in competitors.most_common(8)]
        cited = merchant_cited_runs > 0
        if cited:
            lane = "open" if len(competitor_labels) <= 2 else "contested"
        elif runs_with_any > 0:
            # The AI answered with grounded sources but never the brand — the
            # lane is owned by competitors/retailers.
            lane = "absent"
        else:
            # Probes ran but returned no grounding at all (thin/no demand).
            lane = "no_signal"

        results.append({
            "prompt": prompt,
            "cited": cited,
            "lane": lane,
            "runs": len(runs),
            "runs_cited": merchant_cited_runs,
            "cited_sources": cited_sources[:8],
            "grounding_sources": grounding_sources[:12],
            "competitors": competitor_labels,
            "competitors_count": len(competitors),
            "evidence_excerpt": _custom_prompt_evidence_excerpt(runs),
        })

    return results


# Merchant-safe display copy for every scoring bucket emitted by the
# compute_*_score functions. The raw bucket keys and `missing` schema names
# (e.g. "product_quality_score", "catalog_products.content_key") are INTERNAL
# scoring vocabulary and must never reach a merchant. _primary_gaps() and the
# next-best-action gap chip read from this table; the coverage-guard test
# (tests/test_audit_gap_labels.py) asserts every emitted bucket has an entry so
# a new bucket can never silently leak its raw key. Keep copy free of internal
# jargon: no "/100", no schema dots/underscores, no metric names.
_GAP_DISPLAY: Dict[Tuple[str, str], Dict[str, str]] = {
    # identity
    ("identity", "content_key"): {
        "label": "Stable product identity",
        "why": "AI needs a consistent fingerprint to recognize this product across the web.",
    },
    ("identity", "pivota_signature"): {
        "label": "Canonical product page",
        "why": "A single trusted page gives AI one place to cite for this product.",
    },
    ("identity", "identity_resolution"): {
        "label": "Resolved product identity",
        "why": "This product isn't fully matched to one canonical entity yet, so AI can confuse it with others.",
    },
    ("identity", "variant_identity"): {
        "label": "Clear variant details",
        "why": "Each size, shade, or option needs its own identifiers so AI recommends the exact one a shopper wants.",
    },
    ("identity", "title_brand_category"): {
        "label": "Clear title, brand, and category",
        "why": "AI relies on an unambiguous name, brand, and category to match this product to shopper questions.",
    },
    ("identity", "collision_audit"): {
        "label": "Distinct product identity",
        "why": "This product shares an identity fingerprint with another listing, so models can confuse the two.",
    },
    # content_richness
    ("content_richness", "product_quality_score"): {
        "label": "Richer product detail",
        "why": "The product description is thin where shoppers and AI ask the most questions.",
    },
    ("content_richness", "enrichment_coverage"): {
        "label": "Complete product story",
        "why": "Key selling details — summary, highlights, who it's for, when to use it — are missing or incomplete.",
    },
    ("content_richness", "vertical_structure"): {
        "label": "Category-specific details",
        # Vertical-neutral phrasing: this copy renders for every vertical, and
        # "ingredients" read absurd on an electronics report (live Mojawa run).
        "why": "Shoppers in this category expect the specific details they compare products on, and those aren't fully covered yet.",
    },
    ("content_richness", "model_readiness"): {
        "label": "AI-ready content",
        "why": "The product content isn't yet structured the way AI assistants prefer to read and cite it.",
    },
    ("content_richness", "safety_claims"): {
        "label": "Substantiated claims",
        "why": "Product claims need supporting detail or clear usage guidance before AI will repeat them.",
    },
    ("content_richness", "freshness_raw_pdp"): {
        "label": "Up-to-date product page",
        "why": "The core product page is missing fresh detail or imagery that AI looks for.",
    },
    # routability
    ("routability", "serving_eligibility"): {
        "label": "Discoverable by AI",
        "why": "This product isn't live in the AI shopping surface yet, so assistants can't recommend it.",
    },
    ("routability", "offer_orderability"): {
        "label": "Buyable offer",
        "why": "There's no clear, in-stock, orderable offer for AI to hand a shopper to checkout.",
    },
    ("routability", "price_currency_confidence"): {
        "label": "Reliable price and currency",
        "why": "Price or currency detail is incomplete, so AI can't quote this product confidently.",
    },
    ("routability", "merchant_trust_state"): {
        "label": "Verified store status",
        "why": "Store verification or sync status isn't fully established, which limits how confidently AI surfaces you.",
    },
    ("routability", "policy_jurisdiction"): {
        "label": "Shipping and policy clarity",
        "why": "Shipping coverage and store policies aren't fully specified for the markets AI serves.",
    },
    ("routability", "variant_route_integrity"): {
        "label": "Correct variant checkout",
        "why": "The selected option doesn't cleanly map to a specific buyable offer.",
    },
    # citation
    ("citation", "first_party_rate"): {
        "label": "Cited as the source",
        "why": "When AI answers shopper questions in this category, it rarely points to you as the source.",
    },
    ("citation", "sku_mention_rate"): {
        "label": "Mentioned by name",
        "why": "AI seldom mentions this specific product when shoppers ask category questions.",
    },
    ("citation", "authority_near_variant_rate"): {
        "label": "Backed by trusted sources",
        "why": "Few authoritative sources discuss this product near the queries that matter, so AI has little to cite.",
    },
    ("citation", "answer_quality_rate"): {
        "label": "Strong answer coverage",
        "why": "When this product does come up, the answers AI gives are thin or low-confidence.",
    },
}


# Buckets that measure Pivota's OWN serving/pipeline readiness, not the brand's
# organic AI visibility. For a product deliberately held out of serving
# (decision-grade / BD audits) these score 0 and — because _fixability_for gives
# routability a 1.0 weight — dominate primary_gaps and lead the fix narrative,
# conflating "Pivota hasn't served this" with "brand isn't AI-visible" (#1504).
# We KEEP the gap (score math unchanged — a repricing change is out of scope) but
# annotate it (internal_state + an honest `why`) and stop it from headlining.
# Single source of truth for both the annotation and the narrative demotion.
_INTERNAL_STATE_GAPS: FrozenSet[Tuple[str, str]] = frozenset(
    {("routability", "serving_eligibility")}
)

# Overrides the merchant-safe _GAP_DISPLAY `why` for an internal-state gap so the
# copy can't be misread as an organic-visibility failure. Must stay clear of the
# banned internal-jargon substrings enforced by tests/test_audit_gap_labels.py
# (no "score", "/", "_", "pipeline", …) since _primary_gaps emits it verbatim.
_INTERNAL_STATE_WHY = (
    "This reflects whether Pivota has made the product live in the AI shopping "
    "surface yet — a read on our own serving readiness, not on how visible or "
    "cited the brand is. It stays low whenever a product is deliberately held "
    "back from serving, so treat it as an internal serving-state note rather than "
    "an organic AI-visibility gap."
)


def _is_internal_state_gap(dimension: Any, bucket: Any) -> bool:
    return (str(dimension), str(bucket)) in _INTERNAL_STATE_GAPS


def _humanize_bucket(bucket: str) -> str:
    """Last-resort merchant-safe label for an unmapped bucket.

    Every known bucket lives in _GAP_DISPLAY (enforced by the coverage-guard
    test). This fallback only guarantees a brand-new bucket can never leak a
    raw snake_case key: it strips schema dots and Title-Cases the tail.
    """
    tail = str(bucket or "").split(".")[-1]
    words = [w for w in tail.replace("_", " ").split() if w]
    return " ".join(w.capitalize() for w in words) or "Product readiness"


def _gap_display(dimension: str, bucket: str) -> Dict[str, str]:
    entry = _GAP_DISPLAY.get((dimension, bucket))
    if entry:
        return entry
    return {"label": _humanize_bucket(bucket), "why": ""}


def _primary_gaps(scores: Dict[str, Any], cap: int = 3) -> List[Dict[str, Any]]:
    gaps: List[Dict[str, Any]] = []
    for dimension, payload in scores.items():
        breakdown = (payload or {}).get("breakdown") or {}
        for bucket, detail in breakdown.items():
            if bucket in {"total", "missing_inputs"} or not isinstance(detail, dict):
                continue
            max_points = int(detail.get("max") or 0)
            points = int(detail.get("points") or 0)
            gap = max(0, max_points - points)
            if gap <= 0:
                continue
            display = _gap_display(str(dimension), str(bucket))
            gap_entry = {
                "dimension": dimension,
                "bucket": bucket,
                "points": points,
                "max": max_points,
                "gap": gap,
                # Merchant-safe copy. The raw breakdown `reason` is internal
                # scoring vocabulary and is intentionally NOT surfaced here.
                "label": display["label"],
                "why": display["why"],
            }
            # #1504: a gap that measures Pivota's own serving readiness (only
            # present when the product is NOT serving-eligible) is flagged so the
            # narrative doesn't read "held out of serving" as "not AI-visible".
            # Score fields (points/max/gap) are untouched. A serving-eligible SKU
            # has no such gap, so its primary_gaps stay byte-identical.
            if _is_internal_state_gap(dimension, bucket):
                gap_entry["internal_state"] = True
                gap_entry["why"] = _INTERNAL_STATE_WHY
            gaps.append(gap_entry)
    gaps.sort(key=lambda g: (-g["gap"], g["dimension"], g["bucket"]))
    return gaps[:cap]


def _strip_score_breakdowns(node: Any) -> None:
    """In-place: drop the internal scoring `breakdown` block from score and
    per-provider entries.

    A score `breakdown` is pure internal scoring vocabulary — its bucket keys
    ("product_quality_score", "collision_audit"), `reason` strings ("divergent
    content_key collision"), and `missing_inputs` schema names ("catalog_products
    .content_key") are all internal. None of it is rendered (the UI shows the
    dimension `score` only and the merchant-safe `primary_gaps`). It appears
    under per-SKU `scores.<dimension>.breakdown` and `citation_by_provider.
    <provider>.breakdown` — both of which are dicts carrying a sibling `score`.

    Scoped to dicts that carry a `score` (rather than any key literally named
    `breakdown`) so a future, unrelated merchant-facing `breakdown` field can't
    be stripped by accident. Recurses to any depth.
    """
    if isinstance(node, dict):
        if "score" in node:
            node.pop("breakdown", None)
        for value in node.values():
            _strip_score_breakdowns(value)
    elif isinstance(node, list):
        for item in node:
            _strip_score_breakdowns(item)


def sanitize_report_for_merchant(report: Any) -> Any:
    """Return a merchant-safe deep copy of an assembled audit report.

    Drops internal score `breakdown` blocks and every internal deep-tier
    surface (the `deep_landscape_internal` rollup + raw internal comparison
    runs riding probe payloads — GET /api/audits/{run_id} returns
    partial_result_jsonb, so the response boundary must strip what the
    report-assembly loader strips) from the response only. The stored
    report_jsonb/partial_result_jsonb and all server-side consumers (playbook
    engine, next_best_action, strategic_brief, re-audit delta, the internal
    rollup) run on the intact data and are unaffected. Safe on the full
    per_sku payload, the brand_report, the whole run row, or None.
    """
    if not isinstance(report, (dict, list)):
        return report
    clone = copy.deepcopy(report)
    _strip_score_breakdowns(clone)
    _strip_internal_deep_tier(clone)
    return clone


def strip_internal_deep_tier_for_response(payload: Any) -> Any:
    """Deep-copied payload with ONLY the internal deep-tier surfaces removed
    (deep_landscape_internal keys + internal comparison runs in raw_runs).

    For response boundaries that must not otherwise change shape — e.g. the
    wedge/share endpoints, which predate sanitize_report_for_merchant and whose
    clients may rely on fields the full sanitizer strips."""
    if not isinstance(payload, (dict, list)):
        return payload
    clone = copy.deepcopy(payload)
    _strip_internal_deep_tier(clone)
    return clone


def _strip_internal_deep_tier(node: Any, depth: int = 0) -> None:
    """In-place: remove `deep_landscape_internal` keys and filter internal
    deep-tier comparison runs out of any `raw_runs` list, wherever they sit
    in the payload (report_jsonb, partial_result_jsonb.per_sku_probe_runs,
    audience projections). Depth-capped defensively; payloads are JSON-shaped
    and shallow relative to the cap."""
    if depth > 16:
        return
    if isinstance(node, dict):
        node.pop("deep_landscape_internal", None)
        raw_runs = node.get("raw_runs")
        if isinstance(raw_runs, list):
            node["raw_runs"] = [
                run for run in raw_runs if not run_is_internal_comparison(run)
            ]
        for value in node.values():
            _strip_internal_deep_tier(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _strip_internal_deep_tier(value, depth + 1)


def _band_for_score(score: Optional[int]) -> str:
    if score is None:
        return "blocked"
    if score < 40:
        return "blocked"
    if score < 70:
        return "partial"
    if score < 85:
        return "ready"
    return "agent_ready"


def _sku_band(scores: Dict[str, Any]) -> str:
    values = [
        payload.get("score")
        for payload in scores.values()
        if isinstance(payload, dict) and payload.get("score") is not None
    ]
    return _band_for_score(min(values) if values else None)


# Merchant-facing band + dimension copy. Mirrors _GAP_DISPLAY: the band enum
# ("agent_ready"/"ready"/"partial"/"blocked") and the four dimension keys are
# INTERNAL vocabulary that must never reach a merchant as a raw token. The
# per-SKU report emits, per dimension, {band, band_label, meaning,
# dimension_label, question} derived from this table so the threshold lives in
# ONE place (here + _band_for_score) and the frontend can stop re-deriving
# bands from a duplicated color ramp (dimensionScoreColor) that disagreed with
# the backend. The coverage-guard test (tests/test_audit_dimension_meaning.py)
# asserts every (dimension, band) pair has copy so a new band/dimension can't
# ship without it. Keep copy jargon-free: no "/100", no schema underscores, no
# metric names, never the literal word "score".
_BAND_DISPLAY: Dict[str, Dict[str, str]] = {
    "agent_ready": {
        "label": "Agent-ready",
        "meaning": "AI can confidently find, understand, and recommend this.",
    },
    "ready": {
        "label": "Ready",
        "meaning": "AI can find and use this; only minor gaps remain.",
    },
    "partial": {
        "label": "Needs work",
        "meaning": "AI can sometimes use this, but key details are missing.",
    },
    "blocked": {
        "label": "Not yet visible",
        "meaning": "AI can't reliably find or recommend this yet.",
    },
    # Distinct from "blocked": the dimension was never measured (no resolvable
    # product / unaudited SKU), not measured-and-failing. Different copy.
    "unscored": {
        "label": "Not measured",
        "meaning": "This wasn't measured in this audit.",
    },
}

# The four scored dimensions: merchant-safe label + the one-line question the
# dimension answers (rendered as the per-dimension tooltip / sub-label).
_DIMENSION_DISPLAY: Dict[str, Dict[str, str]] = {
    "identity": {
        "label": "Identity",
        "question": "Can AI tell exactly which product this is?",
    },
    "content_richness": {
        "label": "Content",
        "question": "Does the listing answer what shoppers ask?",
    },
    "routability": {
        "label": "Routability",
        "question": "Can AI hand a shopper a buyable offer?",
    },
    "citation": {
        "label": "Citation",
        "question": "Does AI actually recommend you?",
    },
}

# (dimension, band) -> one-line meaning specialized to the dimension. Falls back
# to the generic _BAND_DISPLAY meaning if a pair is missing, but the coverage
# guard requires every (dimension, band) for the four real bands.
_DIMENSION_BAND_MEANING: Dict[Tuple[str, str], str] = {
    ("identity", "agent_ready"): "AI can pinpoint exactly which product this is.",
    ("identity", "ready"): "AI can identify this product, with only minor ambiguity.",
    ("identity", "partial"): "AI can sometimes identify this, but may confuse it with similar products.",
    ("identity", "blocked"): "AI can't reliably tell which product this is.",
    ("content_richness", "agent_ready"): "The listing answers what shoppers and AI ask.",
    ("content_richness", "ready"): "The listing covers most shopper questions; a few details are thin.",
    ("content_richness", "partial"): "The listing answers some questions but leaves clear gaps.",
    ("content_richness", "blocked"): "The listing is too thin for AI to describe this product.",
    ("routability", "agent_ready"): "AI can hand a shopper a buyable offer for this product.",
    ("routability", "ready"): "AI can route shoppers to an offer, with only minor gaps.",
    ("routability", "partial"): "AI can sometimes route to an offer, but price, stock, or eligibility is unclear.",
    ("routability", "blocked"): "AI has no buyable offer to route a shopper to.",
    ("citation", "agent_ready"): "AI actively recommends this product when shoppers ask.",
    ("citation", "ready"): "AI recommends this product in most relevant answers.",
    ("citation", "partial"): "AI mentions this product occasionally, but rarely as the answer.",
    ("citation", "blocked"): "AI doesn't recommend this product yet.",
}


def _dimension_band(score: Optional[int]) -> str:
    """Per-dimension band. Distinguishes 'not measured' (None) from 'blocked'
    (measured but failing) so the merchant sees the right copy. Thresholds are
    inherited from _band_for_score so banding lives in exactly one place."""
    if score is None:
        return "unscored"
    return _band_for_score(score)


def _dimension_display(dimension: str, score: Optional[int]) -> Dict[str, str]:
    """Merchant-safe {band, band_label, meaning, dimension_label, question} for
    one dimension at a given score."""
    band = _dimension_band(score)
    band_copy = _BAND_DISPLAY.get(band, _BAND_DISPLAY["blocked"])
    dim_copy = _DIMENSION_DISPLAY.get(
        dimension,
        {"label": str(dimension).replace("_", " ").title(), "question": ""},
    )
    if band == "unscored":
        meaning = _BAND_DISPLAY["unscored"]["meaning"]
    else:
        meaning = _DIMENSION_BAND_MEANING.get((dimension, band)) or band_copy["meaning"]
    return {
        "band": band,
        "band_label": band_copy["label"],
        "meaning": meaning,
        "dimension_label": dim_copy["label"],
        "question": dim_copy["question"],
    }


# When the SKU-level band is "blocked" (min across dimensions) but the citation
# dimension itself measured partial or better, the flat "Not yet visible" /
# "AI can't reliably find or recommend this yet" copy contradicts the nonzero
# citations rendered right below it. URL-wedge audits hit this structurally —
# identity and routability can't clear their catalog-anchored buckets without a
# connected store, so the min lands in blocked-band even when AI demonstrably
# recommends the product — but the contradiction is the same on any tier.
# Mirror of _VERDICT_INVISIBLE_WITH_CITATIONS_LABEL (#1196): display-only. The
# raw band enum stays "blocked" for everything that branches on it
# (blocked_skus rollup, band_rank sorting, re-audit deltas, frontend).
_BLOCKED_BUT_CITED_BAND_DISPLAY: Dict[str, str] = {
    "label": "Recommended, but not agent-ready",
    "meaning": "AI already recommends this sometimes, but gaps elsewhere keep it from being reliably identified and bought.",
}

# Same softening for the found-not-endorsed case: nonzero citations, but every
# discovery appearance came via a LISTING retrieval (recommended 0). Saying
# "AI already recommends this" beside a discovery panel showing recommended
# 0/N read as the report disagreeing with itself — "recommended" must mean the
# same thing in the label as in the split it sits next to.
_BLOCKED_BUT_FOUND_BAND_DISPLAY: Dict[str, str] = {
    "label": "Found by AI, but not agent-ready",
    "meaning": (
        "AI already finds this product — mostly via your listings, not yet as "
        "an independent recommendation — and gaps elsewhere keep it from being "
        "reliably identified and bought."
    ),
}


def _band_display(
    band: str,
    scores: Optional[Dict[str, Any]] = None,
    *,
    listing_only: Optional[bool] = None,
) -> Dict[str, str]:
    """Merchant-safe {band, label, meaning} for a SKU-level (or rollup) band.

    Pass the SKU's `scores` to enable the blocked-but-cited softening; omit it
    (default) for a plain enum→copy map. `listing_only=True` (discovery
    appearances were all listing retrievals, zero independent recommendations)
    swaps "Recommended" for the honest "Found by AI" variant.
    """
    copy_ = _BAND_DISPLAY.get(band, _BAND_DISPLAY["blocked"])
    label, meaning = copy_["label"], copy_["meaning"]
    if band == "blocked" and isinstance(scores, dict):
        citation = scores.get("citation")
        citation_score = citation.get("score") if isinstance(citation, dict) else None
        if citation_score is not None and _band_for_score(citation_score) != "blocked":
            softened = (
                _BLOCKED_BUT_FOUND_BAND_DISPLAY
                if listing_only
                else _BLOCKED_BUT_CITED_BAND_DISPLAY
            )
            label = softened["label"]
            meaning = softened["meaning"]
    return {"band": band, "label": label, "meaning": meaning}


def _attach_dimension_display(scores: Dict[str, Any]) -> None:
    """In-place: add merchant-safe display copy to each dimension payload in
    `scores`. Additive — leaves `score` and `breakdown` untouched, so it is safe
    to call after the server-side consumers (next_best_action, strategic_brief)
    have already run on the raw scores."""
    for dimension, payload in scores.items():
        if not isinstance(payload, dict):
            continue
        payload.update(_dimension_display(str(dimension), payload.get("score")))


def _impact_proxy_from_context(sku_ctx: Dict[str, Any]) -> float:
    offers = _get_offers(sku_ctx or {})
    prices = [
        _as_number(o.get("merchant_effective_price")) or _as_number(o.get("estimated_best_price")) or _as_number(o.get("list_price"))
        for o in offers
    ]
    prices = [p for p in prices if p is not None and p > 0]
    price = prices[0] if prices else 1.0
    return round(float(price) * math.log(1 + max(1, len(offers))), 4)


def _coerce_minted_at(value: Any) -> Optional[datetime]:
    """Normalize a `pivota_signature_minted_at` cell (a tz-aware datetime from
    Postgres, or an ISO string from a re-serialized row) to a datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _sku_indexing_arc(product: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Per-SKU Google indexing-arc state for the SKU's Pivota canonical PDP
    (issue #902 item 1, ported from the legacy `merchant_view`). Only emitted
    when the SKU actually has a minted Pivota canonical signature — a SKU
    audited solely on the merchant's own indexed URL has no canonical-PDP arc
    to report (omitted rather than shown as a generic 'unknown'). Pure: defers
    to `compute_indexing_arc_state`."""
    if not isinstance(product, dict):
        return None
    has_canonical_pdp = bool(
        product.get("pivota_signature_id") or product.get("pivota_canonical_url")
    )
    if not has_canonical_pdp:
        return None
    return compute_indexing_arc_state(
        _coerce_minted_at(product.get("pivota_signature_minted_at"))
    )


def _brand_indexing_arc(per_sku_reports: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Brand rollup of the per-SKU indexing arcs (issue #902 item 1): how many
    audited SKUs sit on freshly-minted Pivota canonical PDPs still inside
    Google's 30-90 day indexing window — so a merchant reads zero category
    citations there as indexing latency, not a content gap. None when no audited
    SKU has a canonical-PDP arc (i.e. nothing honest to say about indexing)."""
    arcs = [
        r.get("indexing_arc")
        for r in per_sku_reports or []
        if isinstance(r, dict) and isinstance(r.get("indexing_arc"), dict)
    ]
    arcs = [a for a in arcs if a.get("phase") and a.get("phase") != "unknown"]
    if not arcs:
        return None
    phase_counts: Dict[str, int] = {}
    for a in arcs:
        phase_counts[a["phase"]] = phase_counts.get(a["phase"], 0) + 1
    still_indexing = phase_counts.get("fresh", 0) + phase_counts.get("indexing", 0)
    dates = [a.get("expected_first_citation_at") for a in arcs if a.get("expected_first_citation_at")]
    recheck = max(dates) if dates else None  # ISO 8601 sorts chronologically
    if still_indexing:
        recheck_date = (recheck or "")[:10]
        caveat = (
            f"{still_indexing} of {len(arcs)} audited "
            f"{'product is' if still_indexing == 1 else 'products are'} on a "
            "freshly-minted Pivota canonical PDP still inside Google's 30-90 day "
            "indexing window — zero category citations there may reflect indexing "
            "latency, not a content gap"
            + (f". Re-audit on or after {recheck_date} to check progress." if recheck_date else ".")
        )
    else:
        caveat = (
            f"All {len(arcs)} audited products on Pivota canonical PDPs are past "
            "the 90-day indexing window — zero category citations now points to "
            "content/SEO, not indexing latency."
        )
    return {
        "skus_on_canonical_pdp": len(arcs),
        "phase_counts": phase_counts,
        "skus_still_indexing": still_indexing,
        "recheck_on_or_after": recheck,
        "caveat": caveat,
    }


async def _per_sku_integration_block(
    merchant_id: str,
    integration_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Brand-level integration status + CTA for the per-SKU report (issue #902
    item 2). Ports the legacy `merchant_view` integration/GSC action: surface
    the 'Complete Pivota integration' action when store/PSP onboarding is
    incomplete, else the 'Grant GSC access' CTA when Search Console isn't
    connected yet — the only actionable GSC surface. Best-effort: fetches the
    integration state when the caller didn't pass one (the per-SKU path
    historically didn't)."""
    state = integration_state
    if state is None:
        try:
            from services.merchant_integration_state import get_integration_state
            state = await get_integration_state(merchant_id)
        except Exception:  # noqa: BLE001
            logger.warning("per-sku integration state fetch failed", exc_info=True)
            return None
    if not isinstance(state, dict):
        return None
    from services.merchant_integration_state import build_integration_action

    actions: List[Dict[str, Any]] = []
    integ = build_integration_action(state)
    if integ is not None:
        # Store/PSP onboarding still incomplete — that's the higher-priority ask.
        actions.append(integ)
    elif not state.get("gsc_integrated"):
        # Onboarded but Search Console not connected — the secondary GSC CTA.
        from services.gsc_integration import build_gsc_integration_action
        actions.append(build_gsc_integration_action())
    return {
        "gsc_integrated": bool(state.get("gsc_integrated")),
        "store_platform_integrated": bool(state.get("store_platform_integrated")),
        "psp_integrated": bool(state.get("psp_integrated")),
        "fully_integrated": bool(state.get("fully_integrated")),
        "actions": actions,
    }


def _url_audit_seed_report_identity(
    merchant_id: str,
    product: Mapping[str, Any],
    sku_ctx: Mapping[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """(pipe product_key, content_key) for a URL-audit product that was seeded
    into the commerce index, else (None, None).

    Always emits the seed key on the url_audit path (W5 P2 — audit_run_worker
    seeds unconditionally, so the seed always exists for the portal's evidence
    endpoints, which resolve by platform + source_product_id, to attach to it).
    Deterministic: mirrors the catalog row audit_run_worker seeded from the SAME
    brand-surface URL (product.canonical_url), so the pipe key's platform_product_id
    (source_product_id) matches the minted row. The pipe form
    (`merchant|url_audit|source`) is what the portal's parseProductKey expects,
    which is what lights up the 'supply proof' action."""
    try:
        from services.audit_index_intake import (
            PLATFORM_URL_AUDIT,
            stable_source_id,
        )
        from services.catalog_identity import make_content_key
    except Exception:  # noqa: BLE001 — never break report assembly on import
        return None, None
    if not merchant_id:
        return None, None
    seed_url = str((product or {}).get("canonical_url") or "").strip() or None
    source_id = stable_source_id(seed_url) if seed_url else None
    if not source_id:
        return None, None
    brand = (product or {}).get("brand") or (product or {}).get("vendor")
    title = (product or {}).get("title") or sku_ctx.get("sku_title")
    content_key = make_content_key(brand, title) if (brand and title) else None
    return f"{merchant_id}|{PLATFORM_URL_AUDIT}|{source_id}", content_key


async def build_per_sku_report(
    sku_key: str,
    merchant_id: str,
    audit_run_id: Optional[str],
    provider_model_metadata: Optional[Mapping[str, Any]] = None,
    verify_outputs: Optional[List[Dict[str, Any]]] = None,
    verify_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sku_ctx = await load_sku_context(sku_key, merchant_id)
    # Load RAW (incl. internal comparison runs) exactly once: the deep-tier
    # rollup needs them; every other surface below reads the merchant-visible
    # filtered set.
    raw_probe_runs = await load_per_sku_probe_runs(
        sku_key, merchant_id, audit_run_id, include_internal_comparison=True,
    )
    probe_runs = _merchant_visible_probe_payloads(raw_probe_runs)
    product = _get_product(sku_ctx)
    deep_landscape_internal = None
    try:
        from services.deep_tier_prompts import build_deep_landscape_rollup

        deep_landscape_internal = build_deep_landscape_rollup(
            _flatten_probe_runs(raw_probe_runs),
            own_brand=str(product.get("brand") or product.get("vendor") or ""),
            # v2 echo-lane detection needs the probe identity too — comparison
            # prompts embed the resolved title, not just the brand.
            title=str(product.get("title") or ""),
            # Vendor aliases close the gap with the verdict path's RunFacts
            # call (a source label naming the vendor still counts).
            merchant_vendors=tuple(
                v for v in (
                    str(product.get("vendor") or "").strip(),
                    str(product.get("brand") or "").strip(),
                ) if v
            ),
        )
    except Exception:  # noqa: BLE001 — the rollup must never sink the report
        logger.warning("deep-landscape rollup skipped", exc_info=True)
    provider_models = _probe_run_provider_model_metadata(
        probe_runs,
        fallback=provider_model_metadata,
    )
    from services.sku_opportunity import build_sku_opportunity

    attribute_graph = build_sku_attribute_graph(product)
    opportunity = build_sku_opportunity(
        sku_ctx,
        probe_runs,
        attribute_graph=attribute_graph,
        vertical_profile=_profile_for_sku_ctx(sku_ctx, product),
    )

    if sku_ctx.get("missing_inputs") and not product.get("product_key"):
        null_breakdown = {
            "total": None,
            "missing_inputs": list(sku_ctx.get("missing_inputs") or []),
            "reason": "entire SKU unaudited",
        }
        scores = {
            dim: {"score": None, "breakdown": dict(null_breakdown)}
            for dim in ("identity", "content_richness", "routability", "citation")
        }
    else:
        identity_score, identity_breakdown = compute_identity_score(sku_ctx)
        content_score, content_breakdown = compute_content_richness_score(sku_ctx)
        routing_score, routing_breakdown = compute_routability_score(sku_ctx)
        citation_by_provider = build_citation_by_provider(
            sku_ctx, probe_runs, verify_outputs=verify_outputs,
        )
        citation_score, citation_breakdown = compute_citation_score(
            sku_ctx,
            _any_provider_probe_runs(probe_runs, sku_ctx=sku_ctx),
            verify_outputs=verify_outputs,
        )
        citation_breakdown["aggregation_rule"] = (
            "any_profile_provider: a prompt is treated as cited when any "
            "provider in the resolved coverage profile produces the "
            "citation signal; per-provider details remain in "
            "citation_by_provider."
        )
        citation_breakdown["providers"] = sorted(citation_by_provider)
        scores = {
            "identity": {"score": identity_score, "breakdown": identity_breakdown},
            "content_richness": {"score": content_score, "breakdown": content_breakdown},
            "routability": {"score": routing_score, "breakdown": routing_breakdown},
            "citation": {"score": citation_score, "breakdown": citation_breakdown},
        }

    identity = resolve_sku_identity(sku_ctx)
    # URL audits (synthetic, no synced catalog): the routability dimension
    # (serving_eligibility / offer_orderability) can't be measured or fixed
    # without a connected store, so drop it from the surfaced gaps and skip the
    # "get indexed" action gate — the citation/lane insight should lead, with
    # catalog routing framed as the connect-store unlock instead.
    catalog_unavailable = bool(sku_ctx.get("synthetic_url_audit"))
    primary_gaps = _primary_gaps(scores)
    if catalog_unavailable:
        primary_gaps = [
            g for g in primary_gaps if g.get("dimension") != "routability"
        ]
    # Merchant identity so failing_prompts.competitors_named never surfaces the
    # merchant's own brand/aliases as a rival (the list feeds the win-plan
    # competitor benchmark + NBA / playbook framing). Derived from the resolved
    # SKU identity anchors + product; vendor and anchor-domain aliases included
    # so a de-spaced or vendor-recorded echo of the own brand still strips.
    _fp_anchors = identity.get("anchors") if isinstance(identity, dict) else {}
    _fp_anchors = _fp_anchors if isinstance(_fp_anchors, dict) else {}
    _fp_brand = _fp_anchors.get("brand") or product.get("brand") or product.get("vendor") or ""
    _fp_host = (
        _fp_anchors.get("domain")
        or normalize_host(product.get("canonical_url") or product.get("pdp_url"))
    )
    _fp_vendor = product.get("vendor")
    _fp_brand_lower = str(_fp_brand or "").strip().lower()
    _fp_brand_aliases = derive_brand_aliases(
        _fp_brand or None,
        _fp_host,
        _clean_identity_tuple((_fp_vendor,) if _fp_vendor else None),
    )
    failing_prompts = _failing_prompts(
        probe_runs,
        brand_lower=_fp_brand_lower,
        brand_aliases=_fp_brand_aliases,
    )
    verify_summary_out = verify_summary or _verify_skipped_summary(
        reason="not_run",
        positives_count=len(_citation_positive_verify_candidates(sku_ctx, probe_runs)),
        verify_sample=None,
    )
    # Competitor-attribute depth: probe what the durable category winner is
    # "known for" (ingredients/format/positioning) so the diagnosis answers WHAT
    # competitors did right, not just who they are. Scoped to URL/wedge audits
    # (catalog_unavailable => bounded SKU count) to avoid N×grounded calls on a
    # full catalog; the internal gates (brief enabled + a durable competitor
    # exists) mean it only spends when there's a real winner to learn from.
    # build_per_sku_report previously skipped this entirely — only the legacy
    # wedge-hero path ran it — so it never fired on the live per-SKU audit.
    # Probed BEFORE the NBA so the deterministic content-gap / substitution moves
    # can target the actual winner + the decision factors AI credits them with,
    # instead of prescribing generic "add more detail".
    competitor_attributes: Any = "not_assessed"
    if catalog_unavailable:
        competitor_attributes = await _probe_durable_competitor_attributes_for_brief(
            opportunity=opportunity,
            product=product,
            merchant_id=str(merchant_id),
            run_id=audit_run_id or "",
            coverage={"providers": list(provider_models.keys())},
            provider_model_metadata=provider_models,
        )
    competitor_intel_for_nba = (
        competitor_attributes
        if isinstance(competitor_attributes, Mapping)
        and competitor_attributes.get("status") == "assessed"
        else None
    )
    next_best_action = build_sku_next_best_action(
        opportunity=opportunity,
        primary_gaps=primary_gaps,
        scores=scores,
        failing_prompts=failing_prompts,
        verify_summary=verify_summary_out,
        identity=identity,
        sku_title=(_get_sku(sku_ctx).get("title") or product.get("title")),
        merchant_host=normalize_host(product.get("canonical_url") or product.get("pdp_url")),
        sku_key=sku_key,
        catalog_unavailable=catalog_unavailable,
        competitor_intel=competitor_intel_for_nba,
    )
    # competitor_attributes IS fed to the brief: it LICENSES competitor
    # references for the grounding validator. Removing it (a #3c attempt)
    # backfired — every competitor mention then failed as
    # "unassessed-competitor-attribute" and even the deterministic fallback was
    # rejected (brief_status=unavailable, run b29d6a0f). The depth is also
    # surfaced on competitor_intel for the UI.
    next_best_action = await attach_sku_strategic_brief(
        next_best_action,
        opportunity=opportunity,
        attribute_graph=attribute_graph,
        primary_gaps=primary_gaps,
        scores=scores,
        identity=identity,
        sku_title=(_get_sku(sku_ctx).get("title") or product.get("title")),
        merchant_host=normalize_host(product.get("canonical_url") or product.get("pdp_url")),
        competitor_attributes=(
            competitor_attributes if competitor_attributes != "not_assessed" else None
        ),
    )
    # Surface the competitor intelligence on the report so the merchant/UI can
    # see "what AI says <winner> is known for" directly.
    if isinstance(competitor_attributes, Mapping) and competitor_attributes.get("status") == "assessed":
        next_best_action["competitor_intel"] = competitor_attributes

    deliverability = build_sku_deliverability_prediction(sku_ctx, scores)
    checkout_handoff = build_checkout_handoff_descriptor(
        sku_ctx=sku_ctx,
        deliverability=deliverability,
        audit_run_id=audit_run_id,
    )

    # Suppress per-provider citation when the SKU has no resolvable product
    # (same guard as before); models_cited is derived from the same value so
    # the two never disagree.
    _sku_citation_by_provider = (
        citation_by_provider
        if not (sku_ctx.get("missing_inputs") and not product.get("product_key"))
        else {}
    )

    # Attach merchant-safe band + meaning to each dimension AFTER the
    # server-side consumers (next_best_action, strategic_brief) have run on the
    # raw scores. Additive string fields only — score/breakdown are untouched.
    _attach_dimension_display(scores)
    sku_band = _sku_band(scores)

    # A URL-audited product that was auto-seeded into the index gets an
    # evidence-attachable pipe product_key (`merchant|url_audit|source`) so the
    # portal can offer "supply proof / upload docs" on it; without this the report
    # carries the ephemeral `urlwedge:` key, which the portal can't act on. W5 P2:
    # seeding is unconditional, so every url_audit SKU gets the real seed key.
    _seed_pk, _seed_ck = (None, None)
    if sku_ctx.get("synthetic_url_audit"):
        _seed_pk, _seed_ck = _url_audit_seed_report_identity(merchant_id, product, sku_ctx)
        # W5 P4.1: repoint the request_indexing / enrichment CTA away from the
        # EPHEMERAL `urlwedge:*` key (which build_sku_next_best_action stamps and
        # which the portal + resolver "can't act on") to the seed's REAL catalog
        # product_key. resolve_canonical_pdp_url resolves any target to its
        # catalog_products row via one uniform product_key path, so a url_audit
        # seed (no variant sku_key) becomes resolvable through the SAME mechanism
        # as a connected-store SKU — no tier-branching. Derived from the SAME
        # `_seed_pk` lineage (`merchant|url_audit|source`) so the CTA target and
        # the seeded catalog_products.product_key always agree. Seeding is now
        # unconditional (W5 P2), so _seed_pk is set for every url_audit SKU with a
        # resolvable brand-surface URL; a SKU with no seed_url keeps the urlwedge
        # key and the resolver honestly returns no_canonical_url.
        _seed_id_parts = str(_seed_pk or "").split("|") if _seed_pk else []
        if len(_seed_id_parts) == 3 and isinstance(next_best_action, dict):
            _cta = next_best_action.get("cta")
            if isinstance(_cta, dict):
                from services.catalog_sync_service import make_catalog_product_key

                _cta["target_sku_key"] = make_catalog_product_key(*_seed_id_parts)

    # W1 site-5 fix (2026-07-04): the channels table's own-site row reads the
    # RunFacts source walk, not top_cited_hosts (a competitor rollup that drops
    # own-domain sources whose label names the brand — it displayed
    # "Your site 0/14" while the own page was cited on 13/14 prompts).
    _channel_own_host = normalize_host(
        product.get("canonical_url") or product.get("pdp_url")
    )
    _channel_own_map: Dict[str, bool] = {}
    if _channel_own_host:
        _channel_own_map = {
            _channel_query_key(p.query): p.own_url_cited
            for p in compute_run_facts(
                _flatten_probe_runs(probe_runs),
                merchant_host=_channel_own_host,
            ).prompts
        }

    # Hoisted (was inline in the dict): the band label needs the discovery
    # recommended-vs-listing split — "Recommended, but not agent-ready" beside
    # a discovery panel showing recommended 0/8 read as the report disagreeing
    # with itself. listing_only=True when every discovery appearance came via
    # a listing retrieval, i.e. found-not-endorsed.
    _pc = build_product_competitiveness(
        opportunity.get("per_prompt") if isinstance(opportunity, dict) else None
    )
    _pc_disc = _pc.get("discovery") or {}
    _pc_listing_only = bool(
        (_pc_disc.get("appeared") or 0) > 0
        and not (_pc_disc.get("appeared_recommended") or 0)
    )

    report = {
        "sku_key": sku_key,
        "product_key": _seed_pk or sku_ctx.get("product_key") or product.get("product_key"),
        "content_key": _seed_ck or sku_ctx.get("content_key") or product.get("content_key"),
        "sku_title": (_get_sku(sku_ctx).get("title") or product.get("title")),
        # Bad-name-tolerant resolved identity + confidence. When
        # identity.unresolved is True we only have a variant label / no
        # product-level name — downstream should treat low scores as
        # "enrich before trusting", not "invisible".
        "identity": identity,
        "scores": scores,
        "citation_by_provider": _sku_citation_by_provider,
        "models_cited": _models_cited_for_sku(_sku_citation_by_provider),
        "deliverability": deliverability,
        "band": sku_band,
        # Merchant-safe label + meaning for the SKU-level band so the frontend
        # never renders the raw enum (e.g. "band: agent_ready"). `scores` is
        # passed so a blocked band with partial+ citation gets the coherent
        # blocked-but-cited copy instead of "Not yet visible"; listing_only
        # keeps that copy honest ("Found by AI" vs "Recommended") next to the
        # discovery recommended-vs-listing split.
        "band_display": _band_display(
            sku_band, scores, listing_only=_pc_listing_only
        ),
        "primary_gaps": primary_gaps,
        # W2 pinned measurement basis: the LLM-generated prompt lists this SKU
        # was measured against, with a stable prompt_set_id. The NEXT run's
        # resolve_prompt_basis reloads this (same questions → comparable
        # scores); the re-audit delta asserts basis identity from it.
        # Read from the PERSISTED probe payload first — the report phase
        # reloads sku_ctx fresh, so the probing-phase ctx stash is only a
        # same-instance fallback, never the durable carrier.
        "prompt_basis": (
            basis_meta_from_probe_runs(probe_runs)
            or (
                sku_ctx.get("_prompt_basis_meta")
                if isinstance(sku_ctx.get("_prompt_basis_meta"), dict) else None
            )
        ),
        "verbatim_grounding_evidence": _grounding_evidence(probe_runs),
        "axis_coverage": _axis_coverage(probe_runs),
        "query_class_coverage": _query_class_coverage(probe_runs),
        # INTERNAL-FIRST (founder 2026-07-21): substitution-rate + contest map
        # from the deep-tier comparison probes. Names competitors, so the
        # merchant-response sanitizer strips this exact key; ops read it via
        # DB/BD tooling. None on every standard run.
        "deep_landscape_internal": deep_landscape_internal,
        # Step 2 — citation rate by fine intent axis (head/problem/constraint/trust/
        # nav). Snapshot of WHERE this SKU is cited by question type. Additive.
        "citation_by_intent": _citation_by_intent(
            opportunity.get("per_prompt") if isinstance(opportunity, dict) else None
        ),
        # Sibling-conflation split: queries where the BRAND is cited but THIS
        # SKU never verified — brand visibility the SKU doesn't own (AI often
        # answering with a sibling product). Additive.
        "brand_vs_sku_citation": _brand_vs_sku_citation(
            opportunity.get("per_prompt") if isinstance(opportunity, dict) else None
        ),
        # Product-FIRST competitiveness: does the product win NON-BRANDED
        # discovery demand ("best hair oil for damaged hair") — where the brand
        # gains new buyers — and who AI recommends instead. Branded name queries
        # reported separately as low-value. Leads the card (channel is context).
        "product_competitiveness": _pc,
        # Channel-by-channel appearance: across this product's probed queries,
        # where it shows up in AI answers — the brand's own site vs each retailer
        # AI cites instead. Leads the merchant with brand-product status +
        # channels-as-context (not "the retailer is you"). Reuses per_prompt.
        "channel_appearance": build_channel_appearance(
            per_prompt=opportunity.get("per_prompt") if isinstance(opportunity, dict) else None,
            merchant_host=_channel_own_host,
            retail_channel_host=product.get("retail_channel_host"),
            own_cited_by_query=_channel_own_map or None,
        ),
        # Per-ENGINE operating plan: Gemini (Google index) and ChatGPT (Bing +
        # Reddit/community) cite different sources, so the moves to win each
        # differ. Reuses per-model appearance + divergence + the classified
        # channel hosts (build_channel_appearance is a cheap pure fn — no probes).
        "engine_playbook": build_engine_playbook(
            per_prompt=opportunity.get("per_prompt") if isinstance(opportunity, dict) else None,
            channel_appearance=build_channel_appearance(
                per_prompt=opportunity.get("per_prompt") if isinstance(opportunity, dict) else None,
                merchant_host=_channel_own_host,
                retail_channel_host=product.get("retail_channel_host"),
                own_cited_by_query=_channel_own_map or None,
            ),
        ),
        "failing_prompts": failing_prompts,
        # Issue #902 item 1: Google indexing-arc for this SKU's Pivota canonical
        # PDP (None when the SKU has no minted canonical signature).
        "indexing_arc": _sku_indexing_arc(product),
        "impact_proxy": _impact_proxy_from_context(sku_ctx),
        # Pivota-moat action: supply lab reports / clinical evidence /
        # certifications for Pivota to publish as grounded, citable claims on the
        # canonical PDP. Fires when the product makes substantiation-worthy
        # claims (or AI flagged unsupported answers) but no evidence is supplied.
        "evidence_play": build_evidence_play(
            product=product,
            sku_ctx=sku_ctx,
            verify_summary=verify_summary_out,
        ),
        "provider_models": provider_models,
        "model_is_override": _any_model_override(provider_models),
        "verify_summary": verify_summary_out,
        "verify_outputs": verify_outputs or [],
        "opportunity": opportunity,
        # Win-the-specific-long-tail (Step 2): specific, attribute-stacked prompts
        # the engine built from this SKU's evidenced attributes but didn't probe —
        # the niches to test next. [] when every generated prompt already ran (no
        # padding). Rolled up to the brand via build_suggested_prompts.
        # #1503: suggestions consume the PROBE graph (lexicon + any stashed
        # LLM-grounded attributes already paid for during the fan-out) rather
        # than the bare lexicon graph — flag-off/no-stash it is byte-identical
        # to build_sku_attribute_graph, so beauty output is unchanged.
        "suggested_prompts": _suggested_prompts_for_sku(
            sku_ctx,
            opportunity=opportunity if isinstance(opportunity, dict) else {},
            attribute_graph=_attribute_graph_for_probes(
                sku_ctx, _get_product(sku_ctx or {})
            ),
        ),
        "next_best_action": next_best_action,
    }
    if checkout_handoff:
        report["checkout_handoff"] = checkout_handoff
    # W1 site 6 — phase-1 parity over the assembled per-SKU report. (Site 5's
    # measure was retired 2026-07-04 when the own-site row was REWIRED to the
    # RunFacts source walk above — the measured undercount is fixed at source.)
    try:
        # Site 6 (drift): citation_by_intent is a pure view over the per-prompt
        # source_summary — its cited total must equal the rows it viewed.
        _per_prompt_rows = [
            r
            for r in ((report.get("opportunity") or {}).get("per_prompt") or [])
            if isinstance(r, dict)
        ]
        parity_check(
            "bd_report._citation_by_intent.cited_total",
            sum(
                int((b or {}).get("cited") or 0)
                for b in (report.get("citation_by_intent") or {}).values()
            ),
            sum(
                1
                for r in _per_prompt_rows
                if int(
                    ((r.get("source_summary") or {}).get("merchant_cited_runs")) or 0
                )
                > 0
            ),
            context={"sku_key": sku_key},
        )
    except Exception:  # noqa: BLE001 — parity must never sink the report
        logger.warning("run_facts parity (site 6) failed", exc_info=True)
    return report


def _percentile(values: List[int], pct: float) -> Optional[int]:
    nums = sorted(int(v) for v in values if v is not None)
    if not nums:
        return None
    if len(nums) == 1:
        return nums[0]
    pos = (len(nums) - 1) * pct
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return nums[int(pos)]
    interpolated = nums[lo] + (nums[hi] - nums[lo]) * (pos - lo)
    return int(round(interpolated))


def _dimension_distribution(per_sku_reports: List[Dict[str, Any]], dimension: str) -> Dict[str, Any]:
    values = [
        int((r.get("scores") or {}).get(dimension, {}).get("score"))
        for r in per_sku_reports
        if (r.get("scores") or {}).get(dimension, {}).get("score") is not None
    ]
    median = _percentile(values, 0.5)
    # Merchant-safe band + meaning for the median so the rollup can render a
    # band pill + plain-English line instead of the raw "P25 / P75" jargon.
    display = _dimension_display(dimension, median)
    return {
        "median": median,
        "p25": _percentile(values, 0.25),
        "p75": _percentile(values, 0.75),
        "above_count": sum(1 for v in values if median is not None and v >= median),
        "total_count": len(values),
        **display,
    }


def _overall_score(report: Dict[str, Any]) -> int:
    values = [
        int(payload.get("score"))
        for payload in (report.get("scores") or {}).values()
        if isinstance(payload, dict) and payload.get("score") is not None
    ]
    return min(values) if values else 0


def _per_sku_run_aggregate(per_sku_reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run-level scores for a per_sku audit, so the run persists non-NULL score
    columns and the run-over-run trend works (it was permanently empty because the
    finalize path read the legacy-only `aggregate` key, absent on per_sku runs).
    per_sku-specific semantics, documented so the numbers stay honest:
      - avg_visibility  = mean per-SKU _overall_score (the headline "AI-readiness"
                          number; _overall_score = the SKU's weakest dimension).
      - avg_attribution = mean per-SKU citation-dimension score (attribution ≈
                          getting cited — a clean match to the column's intent).
      - avg_category_visibility = None (no category-visibility dimension in per_sku).
    Comparable across per_sku runs only; do NOT mix with legacy runs in one trend."""
    reports = [r for r in (per_sku_reports or []) if isinstance(r, dict)]
    overalls = [_overall_score(r) for r in reports]
    citations = [
        int((r.get("scores") or {}).get("citation", {}).get("score"))
        for r in reports
        if (r.get("scores") or {}).get("citation", {}).get("score") is not None
    ]
    return {
        "avg_visibility": round(sum(overalls) / len(overalls), 2) if overalls else None,
        "avg_attribution": round(sum(citations) / len(citations), 2) if citations else None,
        "avg_category_visibility": None,
        "products_succeeded": len(reports),
        "products_failed": 0,
    }


def _per_sku_prior_runs(
    prior_runs: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Only prior PER_SKU runs are comparable in a per_sku trend: per_sku and legacy
    write different score semantics into the same score columns (per_sku
    visibility = mean weakest-dimension; legacy = mean of one dimension), so mixing
    them would render a misleading run-over-run delta. None/unknown audit_mode is
    excluded conservatively (better no delta than a wrong one)."""
    return [
        r for r in (prior_runs or [])
        if isinstance(r, dict) and r.get("audit_mode") == "per_sku"
    ]


def _legacy_prior_runs(
    prior_runs: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """The legacy-trend counterpart to _per_sku_prior_runs: exclude per_sku priors
    from a legacy run's trend. Legacy runs are untagged (audit_mode None); per_sku
    is the only explicitly-tagged mode, so "not per_sku" keeps legacy priors while
    dropping per_sku runs whose different score semantics would skew the delta."""
    return [
        r for r in (prior_runs or [])
        if isinstance(r, dict) and r.get("audit_mode") != "per_sku"
    ]


def _fixability_for(dimension: str, bucket: Optional[str] = None) -> float:
    if dimension in {"identity", "content_richness", "routability"}:
        return 1.0
    if bucket == "authority_near_variant_rate":
        return 0.2
    if bucket in {"answer_quality_rate", "sku_mention_rate", "first_party_rate"}:
        return 0.5
    return 0.5


# ownership_state values where the answer is controlled by someone else, so the
# merchant can't realistically win the head term head-on (a flagship / retailer /
# marketplace / publisher / forum / competitor owns it).
_WYCW_LOSING_OWNERSHIP = {
    "competitor-owned",
    "retailer-owned",
    "marketplace-owned",
    "publisher-owned",
    "forum-owned",
}
# Only tell a merchant to STOP fighting a head term if it actually has demand —
# abandoning a no-demand term is meaningless.
_WYCW_SKIP_DEMAND_FLOOR = 0.45


def _wycw_why_you_fit(row: Dict[str, Any]) -> Optional[str]:
    basis = [str(b).strip() for b in (row.get("attribute_basis") or []) if str(b or "").strip()]
    return ", ".join(basis[:4]) if basis else None


def _wycw_evidence(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The verbatim AI answer behind this query — the proof, forwarded from the
    per_prompt `cited_evidence`. For a winnable target it shows what AI says today
    (where you can slot in); for a don't-fight row it shows AI literally routing the
    buyer to the owner. Merchant-facing: capped excerpt + the hosts AI cited."""
    ev = row.get("cited_evidence")
    if not isinstance(ev, dict):
        return None
    excerpt = str(ev.get("excerpt") or "").strip()
    if not excerpt:
        return None
    return {
        "excerpt": excerpt[:400],
        "cited_hosts": [str(h) for h in (ev.get("cited_hosts") or []) if h][:5],
        "provider": ev.get("provider"),
    }


def _wycw_factors(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The 'why winnable' decomposition (each 0-1) behind the opportunity_score —
    forwarded from per_prompt opportunity_factors so the merchant sees WHY a niche
    scores high (fit / demand / low-competition / buying-intent), not just a number."""
    f = row.get("opportunity_factors")
    if not isinstance(f, dict):
        return None
    out = {
        "attribute_fit": f.get("attribute_fit"),
        "demand": f.get("demand_signal"),
        "low_competition": f.get("density_inverse"),
        "intent": f.get("intent_weight"),
    }
    return out if any(v is not None for v in out.values()) else None


def _wedge_chase_lanes(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Normalized-query → lane for the per-SKU sideways wedge's CHASE verdicts
    (recommended beachhead + sideways lanes). These are the queries the report's
    next-best-action tells the operator to fight for — build_where_you_can_win
    must not simultaneously file them under skip. do_not_chase_yet lanes are
    deliberately absent (that verdict AGREES with skip)."""
    nba = report.get("next_best_action") or {}
    evidence = nba.get("evidence") if isinstance(nba, dict) else {}
    wedge = (evidence or {}).get("sideways_wedge") or {}
    if not isinstance(wedge, dict):
        return {}
    beachhead = wedge.get("recommended_beachhead_lane")
    beachhead = beachhead if isinstance(beachhead, dict) else {}
    beachhead_q = str(beachhead.get("query") or "").strip().lower()
    out: Dict[str, Dict[str, Any]] = {}
    for lane in [beachhead, *(wedge.get("sideways_wedge_lanes") or [])]:
        if not isinstance(lane, dict):
            continue
        q = str(lane.get("query") or "").strip().lower()
        if not q or q in out:
            continue
        entry = dict(lane)
        entry["is_beachhead"] = bool(beachhead_q) and q == beachhead_q
        out[q] = entry
    return out


def build_where_you_can_win(
    per_sku_reports: List[Dict[str, Any]],
    *,
    max_targets: int = 5,
    max_skip: int = 5,
) -> Dict[str, Any]:
    """Surface the niche-targeting the audit already computes (per-SKU
    sku_opportunity) as a first-class merchant strategy: the winnable niches to
    target, and the flagship/retailer-owned head terms to stop fighting.

    A medium/long-tail merchant who competes on the hottest, flagship-owned
    terms loses; this names the specific niches where their verified attributes
    win and no one owns the answer yet (open lanes), and the head terms to
    abandon (controlled by someone else, with what the AI actually said).

    targets = open lanes (winnable: demand + attribute fit + no owner), ranked by
    opportunity_score, PLUS the sideways-wedge chase lanes (see below). skip =
    head terms the brand loses to a controller that actually have demand. Both
    deduped by query (best SKU kept). The per-target action hands off to the
    create/distribute engine (Phase 3).

    One query, one verdict (P0-2, operator review 2026-07-10): the sideways
    wedge is the report's #1 recommendation ("chase this lane first"), but this
    builder used to file the very same query under `skip` whenever its lane had
    an owner — the report argued with itself on its flagship move (live on both
    the Mojawa and ANUKO pilot runs). The wedge owns the chase/skip decision:
    its beachhead + sideways lanes are surfaced as targets (source
    "sideways_wedge", probed queries — so the suggested_prompts disjointness
    contract still holds) and are never listed in skip. Head prompts the wedge
    files under do_not_chase_yet keep landing in skip — the two verdicts agree
    there.
    """
    targets_by_q: Dict[str, Dict[str, Any]] = {}
    skip_by_q: Dict[str, Dict[str, Any]] = {}
    # Global chase set: a query ANY SKU's wedge chose to chase must not land in
    # skip via a sibling SKU's row (multi-SKU runs share sidewalk queries).
    wedge_chase_all: set = set()
    for report in per_sku_reports or []:
        if isinstance(report, dict):
            wedge_chase_all.update(_wedge_chase_lanes(report))
    for report in per_sku_reports or []:
        if not isinstance(report, dict):
            continue
        opp = report.get("opportunity") or {}
        identity = report.get("identity") or {}
        sku_name = (
            identity.get("name")
            or report.get("sku_title")
            or report.get("sku_key")
        )
        wedge_lanes = _wedge_chase_lanes(report)
        for row in opp.get("per_prompt") or []:
            if not isinstance(row, dict):
                continue
            q = str(row.get("normalized_query") or row.get("query") or "").strip().lower()
            if not q:
                continue
            wedge_lane = wedge_lanes.get(q)
            if row.get("open_lane") is True:
                score = float(row.get("opportunity_score") or 0)
                prev = targets_by_q.get(q)
                # An open lane strictly outranks a wedge form of the same query
                # (no owner beats contested); among open lanes, best score wins.
                if prev is None or prev.get("source") == "sideways_wedge" or (
                    score > float(prev.get("opportunity_score") or 0)
                ):
                    targets_by_q[q] = {
                        "query": row.get("query") or q,
                        "normalized_query": q,
                        "sku": sku_name,
                        "sku_key": report.get("sku_key"),
                        "attribute_fit": row.get("attribute_fit"),
                        "demand_state": row.get("demand_state"),
                        "opportunity_score": score,
                        "why_you_fit": _wycw_why_you_fit(row),
                        "evidence": _wycw_evidence(row),
                        "opportunity_factors": _wycw_factors(row),
                        "action": "create_answer",
                    }
            elif wedge_lane is not None:
                score = float(
                    wedge_lane.get("opportunity_score")
                    or row.get("opportunity_score")
                    or 0
                )
                prev = targets_by_q.get(q)
                # An open-lane target for the same query (any SKU) outranks the
                # wedge form — open lanes are strictly more winnable.
                if prev is None or (
                    prev.get("source") == "sideways_wedge"
                    and score > float(prev.get("opportunity_score") or 0)
                ):
                    targets_by_q[q] = {
                        "query": row.get("query") or q,
                        "normalized_query": q,
                        "sku": sku_name,
                        "sku_key": report.get("sku_key"),
                        "attribute_fit": row.get("attribute_fit"),
                        "demand_state": row.get("demand_state"),
                        "opportunity_score": score,
                        "why_you_fit": _wycw_why_you_fit(row),
                        "evidence": _wycw_evidence(row),
                        "opportunity_factors": _wycw_factors(row),
                        "action": "create_answer",
                        "source": "sideways_wedge",
                        "is_beachhead": bool(wedge_lane.get("is_beachhead")),
                        "controllers": list(wedge_lane.get("controllers") or []),
                        "selection_reason": wedge_lane.get("selection_reason"),
                    }
            elif (
                q not in wedge_chase_all
                and row.get("ownership_state") in _WYCW_LOSING_OWNERSHIP
                and float(row.get("demand_signal") or 0) >= _WYCW_SKIP_DEMAND_FLOOR
            ):
                demand = float(row.get("demand_signal") or 0)
                prev = skip_by_q.get(q)
                if prev is None or demand > prev["demand_signal"]:
                    ev = row.get("cited_evidence") or {}
                    owned_by = row.get("who_owns")
                    skip_by_q[q] = {
                        "query": row.get("query") or q,
                        "owned_by": owned_by if isinstance(owned_by, str) else None,
                        "ownership_state": row.get("ownership_state"),
                        "demand_signal": demand,
                        "competitors_named": list(ev.get("competitors_named") or [])[:3],
                        "evidence": _wycw_evidence(row),
                    }
    targets = sorted(
        targets_by_q.values(), key=lambda t: -float(t.get("opportunity_score") or 0)
    )[:max_targets]
    skip = sorted(
        skip_by_q.values(), key=lambda s: -float(s.get("demand_signal") or 0)
    )[:max_skip]
    # Explicit bucket semantics (founder review of the VODANA pilot: "Skip 5 /
    # Contest 0 / Defend 0 basically tells the brand to give up"). targets ARE
    # the deliverable — the beachhead lanes to win — so they carry the
    # "win_here" bucket; skip is a budget-protection note, not the plan.
    for t in targets:
        t["bucket"] = "win_here"
    for s in skip:
        s["bucket"] = "dont_burn_budget"
    # demand_proxies: which ranking signals the operator can choose between.
    # 'probe' (single-audit probe demand, the default rank) is always available;
    # 'recurrence' (cross-merchant) is populated by attach_niche_recurrence when
    # the history table has data; 'community' is a future method.
    out: Dict[str, Any] = {
        "targets": targets,
        "skip": skip,
        "has_targets": bool(targets),
        "demand_proxies": ["probe"],
        "demand_proxy_default": "probe",
    }
    if skip:
        out["skip_note"] = (
            "These head terms are controlled by an incumbent or platform today "
            "— don't burn budget here yet. Win the win-here lanes first, then "
            "re-audit to see the heads from a position of strength."
        )
    if not targets and skip:
        # All-skip with no beachhead is a portfolio gap, not a verdict on the
        # brand: every probed lane was an owned head term. Say so, and point at
        # the wedge lanes to probe next (suggested_prompts / custom prompts)
        # instead of handing the merchant a give-up list.
        out["no_beachhead_note"] = (
            "Every probed query was a head term another brand or platform "
            "already owns — that measures where you can't win, not where you "
            "can. Probe wedge lanes next: outcome and differentiator queries, "
            "price-band queries, and alternative-to-incumbent queries. Add "
            "them as custom prompts on your next run, or use the suggested "
            "prompts in your report."
        )
    return out


async def attach_niche_recurrence(
    where_you_can_win: Dict[str, Any],
    *,
    db: Any = None,
) -> Dict[str, Any]:
    """Attach the cross-merchant recurrence signal to each winnable target so the
    operator can rank niches by how often they recur across brands (a compounding,
    proprietary demand proxy) instead of single-audit probe demand. Best-effort —
    leaves targets unchanged when the history table is empty/absent.

    Mutates + returns `where_you_can_win` for convenience.
    """
    targets = (where_you_can_win or {}).get("targets") or []
    if not targets:
        return where_you_can_win
    from services.niche_recurrence import recurrence_for_queries

    keys = [t.get("normalized_query") for t in targets if t.get("normalized_query")]
    recurrence = await recurrence_for_queries(keys, db=db)
    any_recurrence = False
    for t in targets:
        rec = recurrence.get(t.get("normalized_query") or "")
        if rec:
            t["recurrence"] = rec
            any_recurrence = True
    if any_recurrence:
        proxies = where_you_can_win.setdefault("demand_proxies", ["probe"])
        if "recurrence" not in proxies:
            proxies.append("recurrence")
    return where_you_can_win


async def attach_niche_movement(
    where_you_can_win: Dict[str, Any],
    *,
    merchant_id: str,
    db: Any = None,
) -> Dict[str, Any]:
    """Phase 4: attach the re-audit movement (won / holding / lost / still_open /
    new) to each winnable target, so the merchant sees whether the niches they
    targeted got won over time. Best-effort. Mutates + returns the dict."""
    targets = (where_you_can_win or {}).get("targets") or []
    if not targets or not merchant_id:
        return where_you_can_win
    from services.niche_outcomes import niche_movement_for_queries

    keys = [t.get("normalized_query") for t in targets if t.get("normalized_query")]
    movement = await niche_movement_for_queries(merchant_id, keys, db=db)
    for t in targets:
        mv = movement.get(t.get("normalized_query") or "")
        # surface only meaningful, non-"new" movement (new = no prior to compare)
        if mv and mv.get("movement") and mv["movement"] != "new":
            t["movement"] = mv["movement"]
    return where_you_can_win


async def build_outcomes_summary(merchant_id: str) -> Optional[Dict[str, Any]]:
    """Phase 4 v2: the transaction-outcomes moat for the brand rollup — orders
    transacted, and (only once a minimum sample is met, per the schema) refund
    rate + GMV. Honest: returns None when there are no transactions yet, and
    never surfaces refund_rate/GMV before min_sample_met. Best-effort."""
    if not merchant_id:
        return None
    try:
        from services.outcome_aggregation_service import get_merchant_outcomes

        row = await get_merchant_outcomes(str(merchant_id))
    except Exception:  # noqa: BLE001
        return None
    if not row or not int(row.get("transacted_count") or 0):
        return None  # no outcomes yet — omit the strip rather than show zeros
    out: Dict[str, Any] = {
        "transacted_count": int(row.get("transacted_count") or 0),
        "min_sample_met": bool(row.get("min_sample_met")),
    }
    if row.get("min_sample_met"):
        rr = row.get("refund_rate")
        out["refund_rate"] = float(rr) if rr is not None else None
        gmv = row.get("gmv_cents")
        out["gmv_cents"] = int(gmv) if gmv else None
    return out


def build_brand_rollup(
    per_sku_reports: List[Dict[str, Any]],
    merchant_id: str,
) -> Dict[str, Any]:
    dimensions = {
        dim: _dimension_distribution(per_sku_reports, dim)
        for dim in ("identity", "content_richness", "routability", "citation")
    }
    top_by_citation = sorted(
        per_sku_reports,
        key=lambda r: ((r.get("scores") or {}).get("citation", {}).get("score") or -1),
        reverse=True,
    )[:5]
    band_rank = {"agent_ready": 3, "ready": 2, "partial": 1, "blocked": 0}
    top_by_band = sorted(
        per_sku_reports,
        key=lambda r: (band_rank.get(r.get("band"), -1), _overall_score(r)),
        reverse=True,
    )[:5]
    blocked = []
    for report in per_sku_reports:
        # Use the authoritative per-SKU band (set by _sku_band = band of the
        # lowest dimension) so blocked_skus can't disagree with the SKU's own
        # band. The prior ad-hoc rule (identity/routability < 40 OR citation==0)
        # ignored content_richness, so SKUs marked band="blocked" by a low
        # content_richness were missing from blocked_skus entirely.
        if report.get("band") != "blocked":
            continue
        scores = report.get("scores") or {}
        blocked.append({
            "sku_key": report.get("sku_key"),
            "product_key": report.get("product_key"),
            "identity": (scores.get("identity") or {}).get("score"),
            "routability": (scores.get("routability") or {}).get("score"),
            "citation": (scores.get("citation") or {}).get("score"),
            "content_richness": (scores.get("content_richness") or {}).get("score"),
        })

    deliverability_counts: Counter[str] = Counter()
    deliverability_attention: List[Dict[str, Any]] = []
    transactable_skus: List[Dict[str, Any]] = []
    for report in per_sku_reports:
        prediction = report.get("deliverability")
        prediction = prediction if isinstance(prediction, dict) else {}
        status = str(prediction.get("status") or "not_measured").strip() or "not_measured"
        deliverability_counts[status] += 1
        serving = prediction.get("serving") if isinstance(prediction.get("serving"), dict) else {}
        checkout = prediction.get("checkout") if isinstance(prediction.get("checkout"), dict) else {}
        row = {
            "sku_key": report.get("sku_key"),
            "product_key": report.get("product_key"),
            "status": status,
            "summary": prediction.get("summary"),
            "serving_status": serving.get("status"),
            "checkout_status": checkout.get("status"),
        }
        if isinstance(report.get("checkout_handoff"), dict):
            row["checkout_handoff"] = report.get("checkout_handoff")
        if status == "transactable":
            transactable_skus.append(row)
        else:
            deliverability_attention.append(row)

    priority_queue: List[Dict[str, Any]] = []
    for report in per_sku_reports:
        impact = _as_number(report.get("impact_proxy")) or 1.0
        for gap in report.get("primary_gaps") or []:
            dimension = gap.get("dimension")
            score = ((report.get("scores") or {}).get(dimension or "") or {}).get("score")
            if score is None:
                continue
            score_gap = max(0, 100 - int(score))
            fixability = _fixability_for(str(dimension), gap.get("bucket"))
            priority = round(float(impact) * score_gap * fixability, 4)
            priority_queue.append({
                "sku_key": report.get("sku_key"),
                "product_key": report.get("product_key"),
                "dimension": dimension,
                "bucket": gap.get("bucket"),
                "dimension_score": score,
                "impact": impact,
                "gap": score_gap,
                "fixability": fixability,
                "priority_score": priority,
                "reason": gap.get("reason"),
            })
    # #1504: internal serving-state gaps (only present for products held out of
    # serving) must never HEADLINE the fix queue — they measure Pivota readiness,
    # not organic AI visibility, yet _fixability_for weights routability at 1.0 so
    # they'd otherwise top it. Demote them below all non-internal gaps; within
    # each group, priority_score desc with insertion-order tie-break (identical to
    # the prior sort for an all-non-internal cohort, so serving SKUs are
    # byte-identical). No new row key, so row shape is unchanged either.
    priority_queue.sort(
        key=lambda row: (
            1 if _is_internal_state_gap(row.get("dimension"), row.get("bucket")) else 0,
            -(row.get("priority_score") or 0),
        )
    )

    return {
        "merchant_id": merchant_id,
        "skus_audited": len(per_sku_reports),
        "dimensions": dimensions,
        "winning_skus_by_citation": [
            {
                "sku_key": r.get("sku_key"),
                "product_key": r.get("product_key"),
                "citation_score": (r.get("scores") or {}).get("citation", {}).get("score"),
                "band": r.get("band"),
            }
            for r in top_by_citation
        ],
        "winning_skus_by_band": [
            {
                "sku_key": r.get("sku_key"),
                "product_key": r.get("product_key"),
                "overall_score": _overall_score(r),
                "band": r.get("band"),
            }
            for r in top_by_band
        ],
        "deliverability": {
            "status_counts": dict(deliverability_counts),
            "transactable_skus": transactable_skus[:10],
            "attention_skus": deliverability_attention[:25],
        },
        "blocked_skus": blocked,
        "priority_queue": priority_queue[:25],
        # Per-model brand rollup, derived from each SKU's citation_by_provider
        # (no new LLM calls). Single entry today (Gemini); the surface fills in
        # as more providers are enabled.
        "citation_by_provider": _brand_citation_by_provider(per_sku_reports),
        # Step 2 — brand-level citation rate by fine intent axis (rolls up the
        # per-SKU citation_by_intent). Snapshot-only; additive.
        "citation_by_intent": _brand_citation_by_intent(per_sku_reports),
        # #1521 — per-run prompt-mix telemetry (branded vs unbranded by axis) so a
        # reviewer can confirm the ≤30%-branded target held this run.
        "prompt_mix": _brand_prompt_mix(per_sku_reports),
        # #1521 — score-semantics annotation. Score MATH is unchanged; this stamps
        # the prompt-basis version + a note so a cross-version citation/visibility
        # delta (branded share drops → cited rate drops mechanically) is read as a
        # measurement-basis change, not a regression.
        "prompt_mix_version": PROMPT_BASIS_VERSION,
        "prompt_mix_note": (
            "Prompt mix rebalanced (#1521): product/brand-naming (branded) prompts "
            "are capped at a minority share so the audit measures unbranded "
            "discovery demand, not just ~100% branded recall. Citation/visibility "
            "scores are NOT directly comparable across a prompt_mix_version change "
            "— a lower branded share mechanically lowers the cited rate. Compare "
            "deltas only within the same prompt_mix_version."
        ),
    }


def _rollup_verify_summaries(
    per_sku_reports: List[Dict[str, Any]],
) -> Dict[str, Any]:
    summaries = [
        report.get("verify_summary")
        for report in per_sku_reports or []
        if isinstance(report.get("verify_summary"), Mapping)
    ]
    if not summaries:
        return _verify_skipped_summary(reason="no_skus_audited")
    statuses = {
        str(summary.get("status") or "").strip().lower()
        for summary in summaries
        if summary.get("status")
    }
    if statuses == {"completed"}:
        status = "completed"
    elif statuses == {"skipped"}:
        status = "skipped"
    else:
        status = "partial"
    reasons = sorted({
        str(summary.get("reason"))
        for summary in summaries
        if summary.get("reason")
    })
    return {
        "status": status,
        "provider": _ANSWER_QUALITY_VERIFY_PROVIDER,
        "role": "verify",
        "skus": len(summaries),
        "verified": sum(int(summary.get("verified") or 0) for summary in summaries),
        "flagged": sum(int(summary.get("flagged") or 0) for summary in summaries),
        "not_verified": sum(
            int(summary.get("not_verified") or 0) for summary in summaries
        ),
        "citation_positive_candidates": sum(
            int(summary.get("citation_positive_candidates") or 0)
            for summary in summaries
        ),
        "sample_cap": sum(
            int(summary.get("sample_cap") or 0) for summary in summaries
        ),
        "reasons": reasons,
        "flagged_probes": [
            probe
            for summary in summaries
            for probe in (summary.get("flagged_probes") or [])
            if isinstance(probe, dict)
        ][:25],
        "deweight_rule": _ANSWER_QUALITY_VERIFY_DEWEIGHT_RULE,
    }


def _classify_authority_host(host: Optional[str]) -> str:
    h = (host or "").strip().lower()
    if not h:
        return "unclassified"
    if h == "reddit.com" or h.endswith(".reddit.com"):
        return "reddit"
    classified = classify_host(h)
    host_type = (classified.get("type") or "unclassified").lower()
    subtype = (classified.get("subtype") or "").lower()
    if host_type == "editorial":
        return "trade" if "trade" in subtype else "editorial"
    if host_type in {"retailer", "marketplace", "brand"}:
        return "retailer"
    if host_type == "video":
        return "creator"
    if host_type in {"community", "forum", "social"}:
        # Registry-classified forums (head-fi.org, audiosciencereview.com) fold
        # to the forum role — merchant_relative_role already handles it; without
        # this fold they read as unclassified in the authority map.
        return "forum"
    return "unclassified"


# Fix 2 — listing-vs-endorsement. Each cited host is classified *relative to the
# merchant* into one of the spec roles (own_domain / marketplace_self_listing /
# independent_retailer / editorial_review / creator / forum / competitor /
# unclassified), and those roles fold into two signals:
#   findability = own_domain + marketplace_self_listing — the product is findable
#                 (its own site + its own product listed on a marketplace).
#   endorsement = an independent third party recommended it (editorial / review /
#                 independent retailer / creator / community).
# competitor + unclassified count toward neither: a rival's storefront is not the
# merchant's distribution, and an unknown host is conservatively not an
# endorsement. The role/signal logic lives in cited_host_classifier
# (merchant_relative_role / is_findability_role / is_endorsement_role); this
# module supplies the two merchant-relative facts (first_party, is_competitor).
CITATION_ROLE_UNCLASSIFIED = ROLE_RELATIVE_UNCLASSIFIED


# Generic storefront affixes a brand bolts onto its own name for a second
# domain (usually a regional / DTC storefront): "tryanuko.com", "shopbblab.com",
# "anukoofficial.com". Used ONLY to recognise a brand-named own-domain — matched
# as an exact affix on an alias of length >= 5 (short aliases collide), never a
# free substring — so a same-category competitor ("glowrecipe" vs "glow") is not
# swept in. First-party classification only moves a host from
# endorsement/"who-cites-instead" to findability/"your own listing", so the
# conservative-error direction is to under-count endorsement, not over-claim it.
_BRAND_STOREFRONT_PREFIXES = ("try", "shop", "get", "buy", "my", "go", "the", "join")
_BRAND_STOREFRONT_SUFFIXES = (
    "official", "store", "shop", "hq", "us", "usa", "global", "eu", "co", "online",
)
_MIN_AFFIX_ALIAS_LEN = 5


def _host_label_matches_brand_storefront(
    label: str, brand_aliases: Tuple[str, ...]
) -> bool:
    """True when a host's registrable label is a brand alias wrapped in a generic
    storefront affix (tryANUKO, shopBBLAB, ANUKOofficial). Bounded on purpose:
    the alias must be >= 5 chars and match the WHOLE residual after stripping one
    known affix — never a loose substring."""
    if not label:
        return False
    for alias in brand_aliases:
        a = alias.replace(" ", "")
        if len(a) < _MIN_AFFIX_ALIAS_LEN:
            continue
        if any(label == p + a for p in _BRAND_STOREFRONT_PREFIXES):
            return True
        if any(label == a + s for s in _BRAND_STOREFRONT_SUFFIXES):
            return True
    return False


def _host_is_first_party(
    host: Optional[str],
    merchant_hosts: frozenset,
    brand_aliases: Tuple[str, ...],
) -> bool:
    """True when a cited host is the merchant's own site — a direct host match,
    a sub/parent-domain relationship, a registrable label that equals one of the
    brand's de-spaced aliases (e.g. brand "BB Lab" -> `bblab.shop`), or that
    alias wrapped in a generic storefront affix (brand "ANUKO" -> `tryanuko.com`,
    the brand's own US storefront). Exact-label / bounded-affix, not free
    substring, so a competitor that merely contains the brand token
    ("glowrecipe.com" vs brand "Glow") is not mis-tagged as first-party."""
    if not host:
        return False
    h = normalize_host(host) or str(host).strip().lower()
    if not h:
        return False
    for mh in merchant_hosts:
        if mh and (h == mh or h.endswith(f".{mh}") or mh.endswith(f".{h}")):
            return True
    label = _registrable_name_from_host(h)
    if not label:
        return False
    if label in brand_aliases:
        return True
    return _host_label_matches_brand_storefront(label, brand_aliases)


def _host_is_competitor(raw_host_type: Optional[str], first_party: bool) -> bool:
    """True when a cited host is a competitor's storefront — a brand storefront
    (classify_host type ``brand``) that is NOT the merchant's own property.

    Precise on purpose: only a brand-typed storefront is flagged, so generic
    marketplaces/retailers (eBay, Desertcart, Ubuy) that merely carry the
    merchant's own listing are never mislabelled a competitor, and the merchant's
    own brand site is excluded via `first_party`. Editorial/forum sources that
    happen to discuss a competitor are NOT flagged here (no reliable per-host
    signal) — stated as a limit rather than guessed, per the no-fabrication
    guardrail."""
    if first_party:
        return False
    return (raw_host_type or "").strip().lower() == "brand"


def _run_competitor_aliases(competitor_brands, exclude) -> frozenset:
    """De-spaced, normalized brand aliases (len >= 4) for the competitor names
    the engines listed across the run, minus the merchant's own aliases.

    The registry-based `_host_is_competitor` only recognises competitor
    storefronts already classified as ``brand``. This catches the ones it hasn't
    seen — a competitor's own .com that an engine cited on a category query — so
    we never tell the merchant to "get cited on" a competitor's store."""
    out = set()
    for name in competitor_brands or ():
        if not isinstance(name, str):
            continue
        # Category queries ("best argan oil") make the engine list ingredient /
        # category TYPES as "competitors". Those are not brands, and their
        # de-spaced forms (arganoil, collagen) would wrongly flag any unclassified
        # host that leads with the term (arganoilshop.com) as a competitor and
        # strip it from outreach. Drop them here — the same guard
        # `_who_ai_cites_instead` applies to the named-competitor list.
        if is_ingredient_or_category_type(name):
            continue
        # Engines also list RETAILERS as "competitors" on branded/where-to-buy
        # queries ("Best Buy", "Walmart (Refurbished)"). A retailer carrying the
        # merchant's listing is a channel, not a competitor storefront — and its
        # alias here is what flipped bestbuy.com to is_competitor on the Mojawa
        # pilot. Drop retailer names (whole-name and per-alias, so the
        # "(Refurbished)" variants can't smuggle the alias back in).
        if is_profile_retailer_name(name):
            continue
        for alias in derive_brand_aliases(name):
            despaced = alias.replace(" ", "")
            if len(despaced) >= 4 and despaced not in exclude and not is_profile_retailer_name(despaced):
                out.add(despaced)
    return frozenset(out)


def _flag_competitor_by_name(row: Dict[str, Any], competitor_aliases: frozenset) -> bool:
    """If a not-yet-classified cited host's registrable label matches a named
    competitor brand, flag it as a competitor and recompute its role. Returns
    True when it flipped the row.

    Only overrides ``unclassified``/``retailer`` hosts — never an editorial /
    forum / creator source that merely shares a brand prefix — so genuine
    independent sources are preserved (precision over recall). The match is
    exact or brand-prefix (alias len >= 5) on the registrable label, so the
    9 other competitors named alongside ``asiamnaturally`` don't false-match."""
    if not competitor_aliases:
        return False
    if row.get("first_party") or row.get("is_competitor"):
        return False
    host_type = (row.get("host_type") or "").strip().lower()
    if host_type not in ("unclassified", "retailer"):
        return False
    reg = _registrable_name_from_host(row.get("host"))
    if not reg or len(reg) < 4:
        return False
    for alias in competitor_aliases:
        if reg == alias or (len(alias) >= 5 and reg.startswith(alias)):
            row["is_competitor"] = True
            row["citation_role"] = _citation_role(
                row.get("host_type"), bool(row.get("first_party")), True
            )
            return True
    return False


def _strip_own_brand_competitors(names, brand_lower: str, brand_aliases) -> List[str]:
    """Drop the merchant's own brand / known aliases from a raw
    ``competitors_listed`` list before those names reach a cited host's
    ``competitors_named`` or the run-level competitor pool.

    Engines sometimes name the merchant itself among "competitors" in their
    grounded self-report. Left unfiltered, the merchant's own brand leaks into
    ``competitors_named`` and — post-#1382 — trips the ``recommends_rival``
    outreach move with the "rival" actually being the merchant.

    Matching mirrors the ``_brand_in`` own-brand test used for RunFacts: a
    word-boundary match once the brand is long enough to be specific
    (``len >= 4``), substring only for very short brands, plus the shared
    alias-boundary matcher. The word boundary stops a short/common-word brand
    (e.g. "Glow") from erasing a genuine rival ("Glow Recipe") now that these
    names feed an outreach surface — the plain bidirectional substring the
    run-brand tally uses would over-strip here."""
    use_word_boundary = bool(brand_lower) and len(brand_lower) >= 4
    brand_pattern = (
        re.compile(r"\b" + re.escape(brand_lower) + r"\b") if use_word_boundary else None
    )
    out: List[str] = []
    for name in names or ():
        if not isinstance(name, str) or not name.strip():
            continue
        name_lower = name.strip().lower()
        if brand_lower:
            if brand_pattern is not None:
                if brand_pattern.search(name_lower) is not None:
                    continue  # the merchant's own brand (word-boundary)
            elif brand_lower in name_lower:
                continue  # short brand: substring, boundary FP class already moot
        if brand_aliases and text_mentions_brand(name_lower, brand_aliases):
            continue  # an alias of the merchant, not a rival
        out.append(name)
    return out


def _citation_role(
    host_type: Optional[str],
    first_party: bool,
    is_competitor: bool = False,
) -> str:
    """Classify a cited host relative to the merchant (see
    :func:`cited_host_classifier.merchant_relative_role`). `host_type` is the
    folded authority type from `_classify_authority_host`."""
    return merchant_relative_role(
        host_type, first_party=first_party, is_competitor=is_competitor
    )


# C1 — channel-competition advice. The merchant sees not just WHO AI cites
# instead of them, but a per-channel display label + WHAT to do about it, keyed
# on the cited host's merchant-relative role.
_ROLE_DISPLAY_LABEL: Dict[str, str] = {
    ROLE_INDEPENDENT_RETAILER: "Retailer",
    ROLE_MARKETPLACE_SELF_LISTING: "Marketplace",
    ROLE_COMPETITOR: "Competing store",
    ROLE_EDITORIAL_REVIEW: "Editorial",
    ROLE_CREATOR: "Creator",
    ROLE_FORUM: "Community",
}

_ROLE_HOW_TO_COMPETE: Dict[str, str] = {
    ROLE_INDEPENDENT_RETAILER: (
        "A store AI sends buyers to. Get your product listed there, or win the "
        "buy-path: get your Pivota canonical page cited for these queries."
    ),
    ROLE_MARKETPLACE_SELF_LISTING: (
        "A marketplace AI routes buyers to. List your product there, or win the "
        "buy-path with your Pivota canonical page."
    ),
    ROLE_COMPETITOR: (
        "A competing store AI routes buyers to. Win the buy-path — get your Pivota "
        "canonical page cited for these queries and match the offer."
    ),
    ROLE_EDITORIAL_REVIEW: (
        "An independent publisher AI trusts. Earn a review — pitch their editorial "
        "desk (see How to win the recommendation below)."
    ),
    ROLE_CREATOR: (
        "A creator channel AI surfaces. Partner with or seed mid-tier reviewers in "
        "your category."
    ),
    ROLE_FORUM: (
        "A community AI cites. Build presence by answering recurring questions; "
        "it isn't a direct placement channel."
    ),
}

_DEFAULT_HOW_TO_COMPETE = (
    "AI cites this source instead of you. Earn a citation, or win the buy-path "
    "with your Pivota canonical page."
)


def _channel_competition_advice(role: Optional[str]) -> Dict[str, str]:
    """C1: per-channel display label + 'how to compete' guidance, keyed on the
    cited host's merchant-relative role — turns the bare 'who AI cites instead'
    host list into an actionable competitive surface."""
    r = role or CITATION_ROLE_UNCLASSIFIED
    return {
        "role": r,
        "role_label": _ROLE_DISPLAY_LABEL.get(r, "Other source"),
        "how_to_compete": _ROLE_HOW_TO_COMPETE.get(r, _DEFAULT_HOW_TO_COMPETE),
    }


def _citation_signals(host_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll a set of cited-host rows into the findability-vs-endorsement split.

      - findability = the merchant's own site + retail/marketplace listings where
        the merchant's SKU is actually present — the product is *findable* there.
      - endorsement = independent sources (editorial / trade / creator /
        community) that recommended THIS MERCHANT on their own merits.
      - endorsement_category_hosts = the independent hosts that recommended it on a
        category/discovery query — the only honest "AI recommends you for the
        category" evidence.

    TRUTHFULNESS GATE (P0): endorsement and THIRD-PARTY findability require the host
    to have actually NAMED the merchant — `cites_exact_sku` or `cites_near_variant`.
    A host merely cited as a grounding source for a category answer (recommending
    the category or a COMPETITOR) is NOT an endorsement of the merchant and is NOT
    "you're listed there"; it surfaces under `cited_not_naming_hosts` ("who AI cites
    instead"), never as "AI recommends you". The merchant's OWN domain is exempt —
    its own cited page is genuine findability regardless. Before this gate, role
    alone (editorial→endorsement, retailer→findability) produced false "you're
    recommended / listed across X" claims for merchants never actually named.

    `surfaced_only_via_own_listing` is the acceptance flag: the SKU was cited,
    but only through own/retail listings, never independently endorsed — so it
    must never read as "AI recommends you".

    `competitor_hosts` are surfaced separately and excluded from both signals.
    """
    by_role: Dict[str, int] = {}
    findability_hosts: List[str] = []
    endorsement_hosts: List[str] = []
    endorsement_category_hosts: List[str] = []
    competitor_hosts: List[str] = []
    # Cited as a grounding source but did NOT name the merchant — the honest
    # "who AI cites instead of you" set (was previously mislabeled findability /
    # endorsement purely on host role).
    cited_not_naming_hosts: List[str] = []
    # C1 — the same "cited instead" hosts, enriched with a display label + a
    # per-channel "how to compete" action (deduped by host). Additive: the bare
    # string lists above stay unchanged for existing consumers.
    channels_ai_cites_instead: List[Dict[str, Any]] = []
    _channel_seen: set = set()

    def _note_channel(h: Optional[str], r: str, times_cited: int) -> None:
        # #1520: only actual sales channels (retailer / marketplace / competing
        # store) belong here. Editorial / review / creator / forum hosts are
        # authority sources — they still surface in `cited_not_naming_hosts` and
        # route to the win-plan "earn the citation" framing — and unclassified
        # hosts default OUT (unknown != sales channel). Filtering here keeps the
        # sibling `cited_not_naming_hosts` set (appended by callers) intact.
        if not h or h in _channel_seen or not is_channel_role(r):
            return
        _channel_seen.add(h)
        entry: Dict[str, Any] = {"host": h, "times_cited": times_cited}
        entry.update(_channel_competition_advice(r))
        channels_ai_cites_instead.append(entry)

    for row in host_rows or []:
        role = row.get("citation_role") or CITATION_ROLE_UNCLASSIFIED
        by_role[role] = by_role.get(role, 0) + 1
        host = row.get("host")
        if not host:
            continue
        names_merchant = bool(
            row.get("cites_exact_sku") or row.get("cites_near_variant")
        )
        if role == ROLE_OWN_DOMAIN:
            # The merchant's own cited page — genuine findability, no name-gate.
            findability_hosts.append(host)
        elif is_findability_role(role):
            # Retailer / marketplace — "you're listed there" only if your SKU is
            # actually present; otherwise it's a retailer cited for the category.
            if names_merchant:
                findability_hosts.append(host)
            else:
                cited_not_naming_hosts.append(host)
                _note_channel(host, role, int(row.get("prompts_cited_count") or 0))
        elif is_endorsement_role(role):
            # Independent source — an endorsement of YOU only if it named you.
            if names_merchant:
                endorsement_hosts.append(host)
                if row.get("cited_on_category_query"):
                    endorsement_category_hosts.append(host)
            else:
                cited_not_naming_hosts.append(host)
                _note_channel(host, role, int(row.get("prompts_cited_count") or 0))
        elif role == ROLE_COMPETITOR:
            competitor_hosts.append(host)
            _note_channel(host, role, int(row.get("prompts_cited_count") or 0))
    return {
        "by_role": by_role,
        "findability_hosts": findability_hosts,
        "endorsement_hosts": endorsement_hosts,
        "endorsement_category_hosts": endorsement_category_hosts,
        "competitor_hosts": competitor_hosts,
        "cited_not_naming_hosts": cited_not_naming_hosts,
        "channels_ai_cites_instead": channels_ai_cites_instead,
        "has_independent_endorsement": bool(endorsement_hosts),
        "independently_recommended_for_category": bool(endorsement_category_hosts),
        "surfaced_only_via_own_listing": bool(findability_hosts) and not endorsement_hosts,
    }


def _overlay_endorsement_from_facts(
    signals: Dict[str, Any], facts: Any
) -> Dict[str, Any]:
    """W1 site 8 CUTOVER (T2): replace `_citation_signals`' SKU-name-gated
    endorsement set with the RunFacts T2 set — an endorsement-role host that names
    the BRAND (not necessarily the exact SKU). Founder decision 2026-07-04: a
    brand-naming independent source IS 'independently recommended', so a review
    publisher that names the brand but not the precise SKU (e.g. hwahae.com naming
    ANUKO) counts. The legacy name-gate produced a false "no endorsement" that
    contradicted the report's own host table (hwahae rendered as a trusted
    publisher). findability / competitor / channels rows stay from
    `_citation_signals`; only the endorsement portion + its derived flags are
    re-sourced, and the legacy value is stashed for the parity tripwire.

    `facts` is a RunFacts (its `.prompts` carry per-query-class `.endorsed_by`, so
    the category-endorsement subset is reconstructable). A falsy `facts` (no fold
    available) leaves the legacy signals untouched — never a silent zero.
    """
    if not facts:
        return signals
    endorsement_hosts = list(getattr(facts, "endorsement_hosts", ()) or ())
    category_hosts = sorted({
        h
        for p in (getattr(facts, "prompts", ()) or ())
        if getattr(p, "cls", None) == QUERY_CLASS_CATEGORY
        for h in (getattr(p, "endorsed_by", ()) or ())
    })
    out = dict(signals)
    out["endorsement_hosts_legacy"] = signals.get("endorsement_hosts", [])
    out["endorsement_hosts"] = endorsement_hosts
    out["endorsement_category_hosts"] = category_hosts
    out["has_independent_endorsement"] = bool(endorsement_hosts)
    out["independently_recommended_for_category"] = bool(category_hosts)
    out["surfaced_only_via_own_listing"] = (
        bool(out.get("findability_hosts")) and not endorsement_hosts
    )
    return out


def _reddit_subreddit_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    parts = [p for p in (parsed.path or "").split("/") if p]
    for i, part in enumerate(parts):
        if part.lower() == "r" and i + 1 < len(parts):
            return f"r/{parts[i + 1]}"
    return None


def build_authority_map(
    per_sku_reports: List[Dict[str, Any]],
    probe_runs_by_sku: Dict[str, Any],
    *,
    merchant_host: Optional[str] = None,
    merchant_brand: Optional[str] = None,
    merchant_vendors: Optional[Tuple[str, ...]] = None,
    merchant_extra_hosts: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    # Fix 2 — merchant identity for first-party / own-listing classification.
    # Cited hosts that are the merchant's own site (or carry the brand's
    # registrable label) are tagged first_party so "the merchant's own listing
    # was surfaced" is never conflated with "independently recommended in
    # category". Omitting identity (legacy callers) is safe: nothing is tagged
    # first-party and roles fall back to host-type semantics.
    # merchant_extra_hosts = other domains Pivota already knows the merchant owns
    # (onboarding store_url/website + catalog source/canonical hosts, from
    # brand_claim_service.merchant_owned_domains). Folding them in stops a
    # merchant's second storefront (e.g. ANUKO's tryanuko.com) being tagged a
    # third party and surfaced under "who AI cites instead" / an outreach move.
    merchant_hosts = frozenset(
        h
        for h in (
            {normalize_host(merchant_host or "")}
            | {normalize_host(x or "") for x in (merchant_extra_hosts or ())}
        )
        if h
    )
    brand_aliases = derive_brand_aliases(
        merchant_brand,
        merchant_host,
        _clean_identity_tuple(merchant_vendors),
    )
    # Lowercased merchant brand for the own-brand competitor skip below (mirrors
    # the run-brand tally's `brand_lower`), so an engine that lists the merchant
    # among its "competitors" can't surface the merchant as its own rival.
    brand_lower = (merchant_brand or "").strip().lower()

    sku_entries: List[Dict[str, Any]] = []
    host_matrix: Dict[str, Dict[str, Any]] = {}
    # Competitor brand names the engines listed anywhere in this run — collected
    # across all SKUs so a competitor's storefront can be recognised even when it
    # was cited under one SKU but named as a competitor under another.
    run_competitor_brands: set = set()
    # W1 site 8: RunFacts folds for the endorsement cutover. Per SKU (feeds each
    # sku_entry's citation_signals) + all runs pooled (feeds the brand rollup).
    _facts_by_sku: Dict[Any, Any] = {}
    _all_runs_for_facts: List[Dict[str, Any]] = []
    _fact_vendors = _clean_identity_tuple(merchant_vendors)
    for report in per_sku_reports or []:
        sku_key = report.get("sku_key")
        probe_runs = probe_runs_by_sku.get(sku_key) if isinstance(probe_runs_by_sku, dict) else []
        _sku_runs = _flatten_probe_runs(probe_runs)
        _all_runs_for_facts.extend(_sku_runs)
        _sku_facts = compute_run_facts(
            _sku_runs,
            merchant_host=merchant_host,
            merchant_brand=merchant_brand,
            merchant_vendors=_fact_vendors,
        )
        _facts_by_sku[sku_key] = _sku_facts
        host_rows: Dict[str, Dict[str, Any]] = {}
        reddit_threads: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for run in _flatten_probe_runs(probe_runs):
            provider = _run_provider(run)
            parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
            url_match = run.get("url_match") if isinstance(run.get("url_match"), dict) else {}
            llm_report = url_match.get("llm_self_report") if isinstance(url_match.get("llm_self_report"), dict) else {}
            # Strip the merchant's own brand/aliases first: an engine that lists
            # the merchant among its "competitors" must not surface the merchant
            # as its own rival (or trip a recommends_rival outreach move) — the
            # run-brand tally already applies the same own-brand skip.
            competitors = _strip_own_brand_competitors(
                parsed.get("competitors_listed") or parsed.get("competitors_appearing") or run.get("competitors_listed") or [],
                brand_lower,
                brand_aliases,
            )
            run_competitor_brands.update(
                c for c in competitors if isinstance(c, str) and c.strip()
            )
            exact = (
                parsed.get("correct_sku") is True
                or parsed.get("sku_mentioned") is True
                or llm_report.get("correct_sku") is True
                or llm_report.get("sku_mentioned") is True
            )
            near = parsed.get("authority_near_variant_found") is True or llm_report.get("authority_near_variant_found") is True
            excerpt = run.get("evidence_excerpt") or parsed.get("evidence_excerpt") or parsed.get("evidence_text")
            for source in run.get("grounding_sources") or []:
                if not isinstance(source, dict):
                    continue
                uri = source.get("uri") or ""
                # Resolve the Vertex grounding redirector to the real publisher
                # host (from the chunk `title`); otherwise every Gemini citation
                # collapses onto vertexaisearch.cloud.google.com and the merchant
                # sees one "unclassified" host instead of the real cited sources.
                host = _grounding_source_host(source)
                if not host:
                    continue
                # Classify once: the folded authority type (host_type) drives the
                # merchant-relative role + display; the raw classify_host type
                # drives the recommend-vs-list axis and competitor detection
                # (only a brand-typed storefront that isn't the merchant counts).
                host_type = _classify_authority_host(host)
                raw_host_type = (classify_host(host).get("type") or "").lower()
                first_party = _host_is_first_party(host, merchant_hosts, brand_aliases)
                is_competitor = _host_is_competitor(raw_host_type, first_party)
                citation_role = _citation_role(host_type, first_party, is_competitor)
                host_recommendation_class = recommendation_class(raw_host_type)
                query_class = _run_query_class(run)
                row = host_rows.setdefault(host, {
                    "host": host,
                    "host_type": host_type,
                    "first_party": first_party,
                    "is_competitor": is_competitor,
                    "citation_role": citation_role,
                    "recommendation_class": host_recommendation_class,
                    "cites_exact_sku": False,
                    "cites_near_variant": False,
                    "cites_category_not_sku": False,
                    "cited_on_category_query": False,
                    "cited_on_branded_query": False,
                    "prompts_cited_count": 0,
                    "providers": [],
                    "provider_counts": {},
                    "evidence_urls": [],
                    "evidence_excerpt": None,
                    "competitors_named": [],
                    "_queries": set(),
                    "_observations": set(),
                })
                if query_class == QUERY_CLASS_CATEGORY:
                    row["cited_on_category_query"] = True
                else:
                    row["cited_on_branded_query"] = True
                row["cites_exact_sku"] = bool(row["cites_exact_sku"] or exact)
                row["cites_near_variant"] = bool(row["cites_near_variant"] or near)
                row["cites_category_not_sku"] = bool(row["cites_category_not_sku"] or (not exact and not near))
                query = run.get("query") or ""
                if query not in row["_queries"]:
                    row["_queries"].add(query)
                    row["prompts_cited_count"] += 1
                # P0.2: retain per-(query, provider) linkage for
                # citation_observations (otherwise lost at the pop below).
                if query and provider:
                    row["_observations"].add((query, query_class, provider))
                if provider not in row["providers"]:
                    row["providers"].append(provider)
                row["provider_counts"][provider] = (
                    int(row["provider_counts"].get(provider) or 0) + 1
                )
                if uri and uri not in row["evidence_urls"]:
                    row["evidence_urls"].append(uri)
                if excerpt and not row.get("evidence_excerpt"):
                    row["evidence_excerpt"] = str(excerpt)[:280]
                for competitor in competitors or []:
                    if competitor and competitor not in row["competitors_named"]:
                        row["competitors_named"].append(competitor)

                matrix = host_matrix.setdefault(host, {
                    "host": host,
                    "host_type": host_type,
                    "first_party": first_party,
                    "is_competitor": is_competitor,
                    "citation_role": citation_role,
                    "recommendation_class": host_recommendation_class,
                    "cited_on_category_query": False,
                    "cited_on_branded_query": False,
                    "cites_exact_sku": False,
                    "cites_near_variant": False,
                    "skus": set(),
                    "prompts_cited_count": 0,
                    "providers": set(),
                    "provider_counts": defaultdict(int),
                })
                if query_class == QUERY_CLASS_CATEGORY:
                    matrix["cited_on_category_query"] = True
                else:
                    matrix["cited_on_branded_query"] = True
                # Aggregate the per-SKU merchant-citation flags onto the brand-level
                # matrix row (authority_map["hosts"]) — the outreach re-verify oracle
                # needs "this host cited THE MERCHANT'S SKU", which previously only
                # existed on the per-SKU rows, so the loop never flipped to cited.
                matrix["cites_exact_sku"] = bool(matrix["cites_exact_sku"] or exact)
                matrix["cites_near_variant"] = bool(matrix["cites_near_variant"] or near)
                matrix["skus"].add(sku_key)
                matrix["prompts_cited_count"] += 1
                matrix["providers"].add(provider)
                matrix["provider_counts"][provider] += 1

                if host_type == "reddit":
                    subreddit = _reddit_subreddit_from_url(uri) or "r/unknown"
                    reddit_threads[subreddit].append({
                        "url": uri,
                        "title": source.get("title") or "",
                        "provider": provider,
                        "sentiment": None,
                        "matched_sku": bool(exact or near),
                    })

        authority_hosts = []
        for row in host_rows.values():
            row.pop("_queries", None)
            # P0.2: convert the retained per-(query, provider) observations
            # into a clean, JSON-safe field the deposit builder reads into
            # citation_observations. Replaces the discard that killed the
            # per-query citation matrix.
            _obs = row.pop("_observations", set())
            row["query_observations"] = [
                {"query": q, "query_class": qc, "provider": p}
                for (q, qc, p) in sorted(_obs)
            ]
            row["providers"] = sorted(row.get("providers") or [])
            row["provider_counts"] = dict(sorted((row.get("provider_counts") or {}).items()))
            authority_hosts.append(row)
        authority_hosts.sort(key=lambda r: r.get("prompts_cited_count") or 0, reverse=True)
        reddit_subreddits = [
            {
                "name": name,
                "threads": threads,
                "sentiment_proxy": None,
                "recurring_objections": [],
            }
            for name, threads in sorted(reddit_threads.items())
        ]
        sku_entries.append({
            "sku_key": sku_key,
            "product_key": report.get("product_key"),
            "content_key": report.get("content_key"),
            "authority_hosts": authority_hosts,
            "citation_signals": _overlay_endorsement_from_facts(
                _citation_signals(authority_hosts), _facts_by_sku.get(sku_key)
            ),
            "reddit": {"subreddits": reddit_subreddits},
        })

    # Second pass — now that the whole run's competitor names are known, flag
    # competitor STOREFRONTS the cited-host registry hasn't classified yet (e.g.
    # asiamnaturally.com, the "As I Am" brand store Gemini cited on category
    # queries). Without this they stay `unclassified`/`is_competitor=False` and
    # get recommended as "get cited on" outreach targets — pointing the merchant
    # at a competitor's store. Runs before matrix_rows / signals so the
    # findability-vs-endorsement rollup and competitor_hosts reflect the flips.
    own_alias_set = frozenset(a.replace(" ", "") for a in brand_aliases)
    competitor_aliases = _run_competitor_aliases(run_competitor_brands, own_alias_set)
    if competitor_aliases:
        for matrix in host_matrix.values():
            _flag_competitor_by_name(matrix, competitor_aliases)
        for entry in sku_entries:
            flipped = False
            for row in entry.get("authority_hosts") or []:
                if _flag_competitor_by_name(row, competitor_aliases):
                    flipped = True
            if flipped:
                entry["citation_signals"] = _overlay_endorsement_from_facts(
                    _citation_signals(entry["authority_hosts"]),
                    _facts_by_sku.get(entry.get("sku_key")),
                )

    matrix_rows = []
    for row in host_matrix.values():
        matrix_rows.append({
            "host": row["host"],
            "host_type": row["host_type"],
            "first_party": row.get("first_party", False),
            "is_competitor": row.get("is_competitor", False),
            "citation_role": row.get("citation_role", CITATION_ROLE_UNCLASSIFIED),
            "recommendation_class": row.get("recommendation_class", "unknown"),
            "cited_on_category_query": bool(row.get("cited_on_category_query")),
            "cited_on_branded_query": bool(row.get("cited_on_branded_query")),
            "cites_exact_sku": bool(row.get("cites_exact_sku")),
            "cites_near_variant": bool(row.get("cites_near_variant")),
            "skus": sorted(s for s in row["skus"] if s),
            "prompts_cited_count": row["prompts_cited_count"],
            "providers": sorted(row.get("providers") or []),
            "provider_counts": dict(sorted((row.get("provider_counts") or {}).items())),
        })
    matrix_rows.sort(key=lambda r: r["prompts_cited_count"], reverse=True)

    # Brand-level findability-vs-endorsement rollup. The merchant-facing report
    # uses ENDORSEMENT (independent third-party recommendation) for category
    # visibility, and FINDABILITY (own site + retail/marketplace listings) only
    # as a distribution signal — so "your listings are indexed" is never sold as
    # "AI recommends you".
    # W1 site 8: endorsement re-sourced from the RunFacts T2 fold over all pooled
    # runs (brand grain), matching the per-SKU overlay above. findability /
    # competitor rows stay from _citation_signals (the host-matrix view).
    _brand_facts = compute_run_facts(
        _all_runs_for_facts,
        merchant_host=merchant_host,
        merchant_brand=merchant_brand,
        merchant_vendors=_fact_vendors,
    )
    signals = _overlay_endorsement_from_facts(
        _citation_signals(matrix_rows), _brand_facts
    )
    host_attribution_summary = {
        "distinct_hosts": len(matrix_rows),
        "by_role": signals["by_role"],
        "findability_hosts": signals["findability_hosts"],
        "endorsement_hosts": signals["endorsement_hosts"],
        # W1 site 8: the retired SKU-name-gated endorsement set, kept for the parity
        # tripwire (run_brand_report) — NOT rendered.
        "endorsement_hosts_legacy": signals.get("endorsement_hosts_legacy", []),
        "endorsement_category_hosts": signals["endorsement_category_hosts"],
        "competitor_hosts": signals["competitor_hosts"],
        "independent_hosts": signals["endorsement_hosts"],  # back-compat alias
        "has_independent_endorsement": signals["has_independent_endorsement"],
        "independently_recommended_for_category": signals[
            "independently_recommended_for_category"
        ],
        "surfaced_only_via_own_listing": signals["surfaced_only_via_own_listing"],
        # Hosts AI grounded answers in but that did NOT name the merchant — the
        # honest "who AI cites instead of you" set (P0: kept out of endorsement/
        # findability, which now require the merchant to be named).
        "cited_not_naming_hosts": signals["cited_not_naming_hosts"],
        # C1 — "who AI cites instead", enriched with role label + how-to-compete.
        "channels_ai_cites_instead": signals["channels_ai_cites_instead"],
    }
    return {
        "skus": sku_entries,
        "hosts": matrix_rows,
        "host_attribution_summary": host_attribution_summary,
    }


async def _sku_keys_for_per_sku_mode(
    products: List[Dict[str, Any]],
    merchant_id: str,
) -> List[str]:
    keys: List[str] = []
    product_keys: List[str] = []
    for product in products or []:
        sku_key = (product.get("sku_key") or "").strip()
        if sku_key and sku_key not in keys:
            keys.append(sku_key)
        product_key = (product.get("product_key") or "").strip()
        if product_key:
            product_keys.append(product_key)
    if keys or not product_keys or not merchant_id:
        return keys
    placeholders = ", ".join(f":pk{i}" for i, _ in enumerate(product_keys))
    values = {"merchant_id": merchant_id, **{f"pk{i}": pk for i, pk in enumerate(product_keys)}}
    # Roll variants up to ONE representative SKU per product (box-count / size
    # variants share a product_key, and merchant rows of the same canonical PDP
    # share a content_key). Probing every variant produced N identical reports
    # and N× the grounded LLM probes for the same recommendation. Dedupe by the
    # canonical identity (content_key, falling back to product_key) in Python —
    # NOT via Postgres-only DISTINCT ON, since the audit test suite runs on
    # SQLite. The deterministic ORDER BY picks a stable representative.
    rows = await _fetch_all_dicts(
        f"""
        SELECT cs.sku_key, cs.product_key, cp.content_key
          FROM catalog_skus cs
          LEFT JOIN catalog_products cp
            ON cp.product_key = cs.product_key
         WHERE cs.merchant_id = :merchant_id
           AND cs.product_key IN ({placeholders})
         ORDER BY cs.product_key, cs.sku_key
        """,
        values,
    )
    seen_identity: set = set()
    for row in rows:
        sku_key = (row.get("sku_key") or "").strip()
        if not sku_key:
            continue
        identity = (
            str(row.get("content_key") or "").strip()
            or str(row.get("product_key") or "").strip()
            or sku_key
        )
        if identity in seen_identity:
            continue
        seen_identity.add(identity)
        if sku_key not in keys:
            keys.append(sku_key)
    return keys


def _dedupe_query_specs(specs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    seen = set()
    for query, axis in specs:
        q = str(query or "").strip()
        if not q or not _is_well_formed_query(q):
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((q, str(axis or "intent").strip() or "intent"))
    return out


# Punctuation a probe term should never carry into a template. Enrichment
# topic/audience/bullet tags occasionally arrive as serialization debris (a lone
# "[", a stray quote, a single orphan letter); interpolating those yields junk
# prompts like "best toner for [", 'best ... set for "', or "f toner". Strip the
# debris from both ends and drop anything that collapses to a fragment.
_PROMPT_TERM_STRIP_CHARS = " \t\r\n,.;:/\\\"'`[]{}<>()|*#~"

# Promotional / marketing-noise terms are NOT product attributes — no shopper
# types "best moisturizer for skincare discount". PDP/marketing copy (topic tags,
# bullets, merchant tags) routinely carries this debris, and once it interpolates
# into a `best {category} for {term}` template it produces nonsense queries that
# make the whole audit look broken (flagged live on the DAMDAM report). The gate
# lives in `services.promo_terms` so query generation here and attribute-graph
# construction in `sku_sidewalk` share one stop list and can never drift apart.
_is_promo_term = is_promo_term


# A shopper attribute/category term is a short noun phrase ("bond repair
# treatment", "low molecular weight collagen"), never a full sentence. When a
# PDP description sentence leaks in as a "term" it becomes a nonsense query
# ("description a gentle scrub formulated with a natural exfoliator … face
# cleansers"). Cap the word count so sentence fragments are dropped while every
# real multi-word attribute (well under this bound) passes untouched.
_PROMPT_TERM_MAX_WORDS = 8


def _clean_prompt_term(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    text = text.strip(_PROMPT_TERM_STRIP_CHARS)
    # A token with no real word content (a lone bracket/quote) or a single stray
    # character is not a shopper term — emitting it leaks a query fragment.
    if len(text) < 2 or not re.search(r"[a-z0-9]", text):
        return ""
    # A full sentence (PDP description copy) is not a shopper attribute; emitting
    # it produces a garbled multi-clause query. Drop anything sentence-length.
    if len(re.findall(r"[a-z0-9]+", text)) > _PROMPT_TERM_MAX_WORDS:
        return ""
    # A promo/discount/marketing term is not a shopper attribute — dropping it
    # here keeps it out of every query template (topics, bullets, attributes).
    if _is_promo_term(text):
        return ""
    return text


# Generated probe queries are shopper prompts, never templates, so a stray
# bracket, an unbalanced quote/paren, a dangling connective, or a sub-minimal
# fragment all signal a template token that resolved to empty/junk. This gate is
# the defense-in-depth backstop at the generation dedupe chokepoints: even if a
# generator (sidewalk, merchant tags) leaks debris that `_clean_prompt_term`
# never saw, a malformed query never reaches a probe. Keep it conservative —
# only reject shapes that are unambiguously broken, so well-formed queries pass.
_QUERY_BRACKET_RE = re.compile(r"[\[\]{}<>]")
_QUERY_DANGLING_TAIL_RE = re.compile(
    r"\b(?:for|with|and|or|the|a|an|of|to|in|on|by|from)$"
)
_QUERY_MIN_LEN = 4


def _is_well_formed_query(query: Any) -> bool:
    q = re.sub(r"\s+", " ", str(query or "").strip())
    if len(q) < _QUERY_MIN_LEN:
        return False
    if _QUERY_BRACKET_RE.search(q):
        return False
    if q.count("(") != q.count(")"):
        return False
    if q.count('"') % 2 != 0:
        return False
    if _QUERY_DANGLING_TAIL_RE.search(q.lower()):
        return False
    return True


def _graph_class_values(graph: Mapping[str, Any], class_name: str) -> List[str]:
    classes = graph.get("classes") if isinstance(graph.get("classes"), dict) else {}
    values = classes.get(class_name) if isinstance(classes, dict) else []
    out: List[str] = []
    seen = set()
    for value in values or []:
        cleaned = _clean_prompt_term(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


# Generic container/merchandising "categories" that describe a bundle, not a
# product kind. Used as a category anchor they produce off-target queries and
# absurd competitors — DAMDAM's CITRUS GLOW serum resolved to "set" and was
# probed as "best set", returning cookware brands (All-Clad, Le Creuset) as
# skincare rivals. Reject them so the anchor falls through to the title-derived
# category ("serum").
_GENERIC_CONTAINER_CATEGORIES = frozenset({
    "product", "products", "item", "items",
    "set", "sets", "kit", "kits", "bundle", "bundles",
    "collection", "collections", "pack", "packs", "gift", "gifts",
    "gift set", "gift sets", "starter kit", "sampler", "box", "value set",
})


def _category_for_unbranded_prompts(
    product: Mapping[str, Any],
    product_type: str,
    graph: Mapping[str, Any],
    *,
    profile: VerticalProfile = BEAUTY_PROFILE,
) -> str:
    direct = _clean_prompt_term(
        product_type
        or product.get("product_type")
        or product.get("category")
    )
    if (
        direct
        and direct not in _GENERIC_CONTAINER_CATEGORIES
        and not _noisy_prompt_category(direct, profile=profile)
    ):
        return direct
    for category in _graph_class_values(graph, "category"):
        if (
            category
            and category not in _GENERIC_CONTAINER_CATEGORIES
            and not _noisy_prompt_category(category, profile=profile)
        ):
            return category
    attrs = product.get("attributes_raw")
    attrs_text = ""
    if isinstance(attrs, Mapping):
        attrs_text = " ".join(
            str(value)
            for value in attrs.values()
            if isinstance(value, (str, int, float))
        ).lower()
        tag_values = attrs.get("tags")
        if isinstance(tag_values, list):
            attrs_text += " " + " ".join(str(tag).lower() for tag in tag_values)
    title_text = str(product.get("title") or product.get("raw_title") or "").lower()
    combined = f"{title_text} {attrs_text}"
    # Vertical-scoped category fallbacks. For beauty these are the historical
    # "beauty supplement" / "supplement" rules; for an unknown vertical the
    # generic profile has NO fallbacks, so a non-beauty URL audit no longer
    # collapses to "beauty supplement".
    for trigger_tokens, label in profile.category_fallbacks:
        if any(token in combined for token in trigger_tokens):
            return label
    # Last resort: derive the category from the product TITLE. Store-less brands
    # audited by URL often have no product_type, no graph, and no catalog
    # (product_type=None, attribute_graph=null) — without a category the per-SKU
    # query gen emits ONLY branded name queries (low-value) and never probes the
    # non-branded discovery demand ("best hair oil for damaged hair") that is the
    # whole point of the audit. A descriptive title ("… Bond & Repair Hair Oil")
    # carries the category; extract it so discovery queries generate.
    from_title = _category_from_title(
        product.get("title") or product.get("raw_title"),
        brand=product.get("vendor") or product.get("brand"),
        profile=profile,
    )
    if from_title:
        return from_title
    return ""


# Personal-care / beauty category head-nouns + the body-part qualifiers that pair
# with them ("hair oil", "face cream", "lip balm"). Used to extract a clean
# category from a product title when no structured category exists.
#
# MIGRATED to the beauty profile (services.vertical_profiles.BEAUTY_PROFILE);
# these module-level names are backward-compatible aliases so every call site
# stays byte-identical. A non-match falls back to "" (no discovery queries, no
# regression), exactly as before.
_CATEGORY_HEAD_NOUNS = BEAUTY_PROFILE.category_head_nouns
_CATEGORY_MODIFIERS = BEAUTY_PROFILE.category_modifiers


def _category_from_title(
    title: Any,
    brand: Optional[str] = None,
    *,
    profile: VerticalProfile = BEAUTY_PROFILE,
) -> str:
    """Extract a clean buyer-facing category ("hair oil", "face cream") from a
    product title. Strips variant noise (after '#'/'(', sizes), drops brand
    tokens, then finds a head-noun and its body-part qualifier. Returns "" when
    no confident category is found (caller then emits no discovery queries)."""
    text = str(title or "").lower()
    text = re.split(r"[#(]", text, maxsplit=1)[0]
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ml|l|g|kg|oz|fl|pcs?|ct|count|x\d+)\b", " ", text)
    brand_tokens = set(re.findall(r"[a-z0-9]+", str(brand or "").lower()))
    tokens = [t for t in re.findall(r"[a-z0-9]+", text) if t and t not in brand_tokens]
    for i, tok in enumerate(tokens):
        if tok in profile.category_head_nouns:
            modifier = (
                tokens[i - 1]
                if i > 0 and tokens[i - 1] in profile.category_modifiers
                else None
            )
            candidate = f"{modifier} {tok}".strip() if modifier else tok
            cleaned = _clean_prompt_term(candidate)
            if cleaned and not _noisy_prompt_category(cleaned, profile=profile):
                return cleaned
    return ""


def _noisy_prompt_category(value: str, *, profile: VerticalProfile = BEAUTY_PROFILE) -> bool:
    cleaned = _clean_prompt_term(value)
    if not cleaned:
        return True
    tokens = set(re.findall(r"[a-z0-9]+", cleaned))
    if tokens & profile.noisy_prompt_tokens:
        return True
    return False


def _term_repeats_category(
    term: str,
    category: str,
    *,
    profile: VerticalProfile = BEAUTY_PROFILE,
) -> bool:
    """True when folding ``term`` into a ``best {category} for {term}`` shape
    would repeat the category noun ("best headphones for bone conduction
    headphones", "best headphones for golf headphones").

    BIDIRECTIONAL — the old ``term not in category`` guard only caught
    term ⊆ category (short use-case inside a longer category); it missed the far
    more common category ⊆ term ("headphones" inside "bone conduction
    headphones"). We reject when either nests, or when ``term`` carries the
    category's own head noun OR any of the vertical's product-type head nouns
    (electronics: earbuds/speaker/headset — "best headphones for wireless
    earbuds" is nesting two product types). Beauty head nouns are all product
    FORMS (oil/serum/cream), so genuine concern use-cases ("dry skin", "frizz")
    never trip this."""
    t = _clean_prompt_term(term)
    c = _clean_prompt_term(category)
    if not t or not c:
        return True
    if t == c or t in c or c in t:
        return True
    term_tokens = set(re.findall(r"[a-z0-9]+", t))
    head_nouns = set(getattr(profile, "category_head_nouns", None) or ())
    ctoks = re.findall(r"[a-z0-9]+", c)
    if ctoks:
        head_nouns.add(ctoks[-1])   # the category's own head noun, vertical-agnostic
    return bool(head_nouns & term_tokens)


# Common "best X under $N" bands shoppers actually ask in. The wedge picks the
# smallest band at/above the SKU's best-known price; above the top band there is
# no budget-wedge story (a $450 tool is not an "under $N" pitch), so no shape.
_WEDGE_PRICE_BANDS_USD = (25, 40, 50, 70, 100, 150, 200)


def _wedge_price_band_usd(sku_ctx: Dict[str, Any]) -> Optional[int]:
    """Price-anchored wedge band for this SKU: the smallest common "under $N"
    band >= its best-known offer price. None without a usable price (or when
    a non-USD currency is stated) — the shape is omitted, never guessed."""
    prices: List[float] = []
    for offer in _get_offers(sku_ctx or {}):
        currency = str(offer.get("currency") or "").strip().upper()
        if currency and currency != "USD":
            continue
        for key in ("merchant_effective_price", "estimated_best_price", "list_price"):
            value = _as_number(offer.get(key))
            if value is not None and value > 0:
                prices.append(float(value))
                break
    if not prices:
        return None
    best = min(prices)
    for band in _WEDGE_PRICE_BANDS_USD:
        if best <= band:
            return band
    return None


def _outcome_repeats_category(outcome: str, category: str) -> bool:
    """True when a profile-level outcome term collides with THIS category —
    the outcome shape would read as a tautology ("curling iron that also curls
    hair"). Word-stem overlap: any 4+-char alnum token in the outcome sharing a
    4-char prefix with a 4+-char category token ("curls"/"curling") collides.
    Conservative by design: dropping a plausible outcome costs one wedge probe;
    emitting a tautology burns merchant-visible credit on junk."""
    outcome_tokens = [t for t in re.findall(r"[a-z0-9]+", str(outcome or "").lower()) if len(t) >= 4]
    category_tokens = [t for t in re.findall(r"[a-z0-9]+", str(category or "").lower()) if len(t) >= 4]
    for o_tok in outcome_tokens:
        for c_tok in category_tokens:
            if o_tok[:4] == c_tok[:4]:
                return True
    return False


def _unbranded_category_specs(
    *,
    category: str,
    graph: Mapping[str, Any],
    topics: List[str],
    bullets: List[str],
    profile: VerticalProfile = BEAUTY_PROFILE,
    price_band_usd: Optional[int] = None,
) -> List[Tuple[str, str]]:
    category = _clean_prompt_term(category)
    if not category or category in _GENERIC_CONTAINER_CATEGORIES:
        return []

    # NOTE on tags: queries keep the COARSE `axis` vocabulary every downstream
    # consumer already handles (discovery="category", attribute-framed="attribute").
    # Only the query SHAPES diversify here — the fine intent taxonomy
    # (category_head / problem_jtbd / constraint) + per-axis scoring is Step 2.
    # See PIVOTA-Agent/docs/ai_readiness_query_axes_build_plan.md.

    # category_head — DEMOTED to two diagnostic head terms (was ~10 near-synonym
    # superlatives all measuring the same "am I in the ranked list?" signal).
    specs: List[Tuple[str, str]] = [
        (f"best {category}", "category"),
        (f"what {category} should I buy", "category"),
    ]

    # Challenger-wedge shapes (profile config pack + SKU price), emitted right
    # after the two diagnostic head terms so a small prompts_per_sku budget
    # still probes lanes a challenger can WIN, not only the heads an incumbent
    # owns (head-only portfolio -> 0 targets / 5 skip, VODANA run 452d9394;
    # wedge portfolio -> 4 targets incl. beachhead, run 7b345df0). Three
    # shapes: buyer-outcome ("{cat} that doesn't snag or pull hair"),
    # price-anchored ("best {cat} under $70"), and alternative-seeker
    # ("affordable GHD alternative"). ALL gated on the profile carrying wedge
    # config — probe-set composition is pinned behavior, so a priced SKU in an
    # unconfigured vertical must stay byte-unchanged (incl. the price shape).
    _wedge_outcomes = tuple(getattr(profile, "seed_outcome_terms", ()) or ())
    _wedge_incumbents = tuple(getattr(profile, "wedge_incumbent_brands", ()) or ())
    for outcome in _wedge_outcomes[:3]:
        outcome = _clean_prompt_term(outcome)
        # A profile's outcome terms are class-wide but a category is specific:
        # "also curls hair" reads as a wedge for a flat iron and as a
        # tautology for a curling iron. Stem-overlap guard drops the
        # tautological pairing (a shared 4-char word stem, "curl"/"curling").
        if outcome and not _outcome_repeats_category(outcome, category):
            specs.append((f"{category} that {outcome}", "category"))
    if (
        (_wedge_outcomes or _wedge_incumbents)
        and price_band_usd
        and int(price_band_usd) > 0
    ):
        specs.append((f"best {category} under ${int(price_band_usd)}", "category"))
    for incumbent in _wedge_incumbents[:2]:
        incumbent = str(incumbent or "").strip()
        if incumbent:
            specs.append((f"affordable {incumbent} alternative", "category"))

    use_cases = [
        cleaned
        for cleaned in (_clean_prompt_term(u) for u in _graph_class_values(graph, "use_case"))
        if cleaned and not _term_repeats_category(cleaned, category, profile=profile)
    ]
    audiences = [
        cleaned
        for cleaned in (_clean_prompt_term(a) for a in _graph_class_values(graph, "audience"))
        if cleaned
    ]

    # problem_jtbd shapes — how shoppers actually ask AI: need/problem-first. A
    # need-framed query returns products, not a bare ingredient rundown, which also
    # cuts the ingredient-as-competitor harvesting at the source (vs "best {cat}").
    # "what helps with X" is gated to genuine problem/routine terms: scenario
    # terms produced observed junk ("what helps with travel/summer") — those get
    # proper scenario shapes in the sidewalk generator instead.
    from services.sku_sidewalk import is_scenario_slug

    # "what helps with X" is problem/concern-framed: only emit it for verticals
    # whose use-cases are genuine concerns (beauty). Electronics use-cases are
    # activities/product types, so the profile gates it off entirely.
    problem_framed = bool(getattr(profile, "problem_framed_prompts", True))
    # A DEVICE SPEC that got mis-classified into use_case (e.g. "dual voltage",
    # "ceramic plates") must NOT be problem-framed — "what helps with dual voltage"
    # is junk. Route a pure-spec term to the attribute shape instead. spec_vocab is
    # the profile's own spec vocabulary (empty for topical/electronics → no change).
    _spec_vocab = {
        tok
        for term in (getattr(profile, "seed_spec_terms", ()) or ())
        for tok in re.findall(r"[a-z0-9]+", str(term).lower())
    }
    for use_case in use_cases[:3]:
        uc_tokens = set(re.findall(r"[a-z0-9]+", str(use_case).lower()))
        if _spec_vocab and uc_tokens and uc_tokens <= _spec_vocab:
            specs.append((f"{use_case} {category}", "attribute"))
            continue
        specs.append((f"best {category} for {use_case}", "category"))
        if problem_framed and not is_scenario_slug(use_case):
            specs.append((f"what helps with {use_case}", "category"))
    for audience in audiences[:2]:
        specs.append((f"{category} for {audience}", "category"))
    for topic in topics[:3]:
        cleaned = _clean_prompt_term(topic)
        if cleaned and not _term_repeats_category(cleaned, category, profile=profile):
            specs.append((f"best {category} for {cleaned}", "category"))

    # constraint shapes — cert / exclusion / ingredient framed ("vegan collagen",
    # "fragrance-free retinol", "marine collagen", "clinically tested collagen").
    constraint_terms: List[str] = []
    for class_name in ("certification_constraint", "exclusion", "ingredient", "proof"):
        constraint_terms.extend(
            cleaned
            for cleaned in (_clean_prompt_term(v) for v in _graph_class_values(graph, class_name))
            if cleaned and cleaned not in category
        )
    for bullet in bullets[:3]:
        cleaned = _clean_prompt_term(bullet)
        if cleaned and cleaned not in category:
            constraint_terms.append(cleaned)
    for attr in constraint_terms[:5]:
        specs.append((f"{attr} {category}", "attribute"))

    # Seed the category DECISION SPACE (per-category config pack) so the audit
    # probes it even before deep extraction: buyer CONCERNS the device solves →
    # problem-framed ("what helps with frizzy hair"); hardware SPECS → attribute
    # shape ("dual voltage flat iron"), never problem-framed. Empty for
    # topical/electronics profiles, so their prompts are byte-unchanged. Deduped.
    for concern in (getattr(profile, "seed_concern_terms", ()) or ()):
        concern = _clean_prompt_term(concern)
        if not concern or _term_repeats_category(concern, category, profile=profile):
            continue
        specs.append((f"best {category} for {concern}", "category"))
        if problem_framed and not is_scenario_slug(concern):
            specs.append((f"what helps with {concern}", "category"))
    for spec in (getattr(profile, "seed_spec_terms", ()) or ()):
        spec = _clean_prompt_term(spec)
        if spec and spec not in category:
            specs.append((f"{spec} {category}", "attribute"))

    return _dedupe_query_specs(specs)


def _build_per_sku_base_query_specs(
    sku_ctx: Dict[str, Any],
) -> Tuple[List[Tuple[str, str]], str, str]:
    product = _get_product(sku_ctx or {})
    sku = _get_sku(sku_ctx or {})
    brand = product.get("brand") or product.get("vendor") or ""
    # Prefer the PRODUCT title (real shopper-facing identity, e.g.
    # "Triple Shine Grape") over the variant/SKU label (e.g.
    # "14 Servings, 2-Week Routine"). Shopper queries built from a bare variant
    # label don't name the product, so providers return no real citations.
    product_title = (
        product.get("title")
        or sku_ctx.get("sku_title")
        or sku.get("title")
        or sku_ctx.get("sku_key")
        or "this product"
    )
    # Probe identity = the resolved SKU identity: enrichment title_override when
    # present, else brand+product title (deduped) — never a bare variant label.
    # Falls back to the local brand+product_title if resolution yields nothing.
    title = resolve_sku_identity(sku_ctx or {}).get("name") or (
        f"{brand} {product_title}"
        if (brand and brand.lower() not in product_title.lower())
        else product_title
    )
    variant_label = (sku.get("title") or "").strip()
    product_type = (
        product.get("product_type")
        or product.get("category")
        or ""
    )
    attribute_graph = _attribute_graph_for_probes(sku_ctx, product)
    profile = _profile_for_sku_ctx(sku_ctx, product)
    unbranded_category = _category_for_unbranded_prompts(
        product,
        str(product_type or ""),
        attribute_graph,
        profile=profile,
    )
    enrichment = (
        sku_ctx.get("product_enrichment")
        if isinstance(sku_ctx.get("product_enrichment"), dict)
        else {}
    )
    topics = [
        str(item).strip()
        for item in (
            enrichment.get("topic_tags")
            or enrichment.get("audience_tags")
            or enrichment.get("usage_scenarios")
            or []
        )
        if str(item).strip()
    ][:6]
    bullets = [
        str(item).strip()
        for item in enrichment.get("bullet_points") or []
        if str(item).strip()
    ][:6]

    # navigational (axis "intent") — demoted to two; "for sale" dropped as a dup.
    specs: List[Tuple[str, str]] = [
        (f"where can I buy {title}", "intent"),
        (f"shop {title} online", "intent"),
    ]
    # trust shapes — validation/legitimacy. Tagged "review" (the coarse branded-
    # consideration axis consumers + the budgeter already handle); fine "trust"
    # tag is Step 2.
    trust_subject = (brand or title).strip()
    specs.extend([
        (f"is {trust_subject} legit", "review"),
        (f"{title} reviews", "review"),
        (f"does {title} actually work", "review"),
    ])
    specs.extend(
        _unbranded_category_specs(
            category=unbranded_category,
            graph=attribute_graph,
            topics=topics,
            bullets=bullets,
            profile=profile,
            price_band_usd=_wedge_price_band_usd(sku_ctx or {}),
        )
    )
    # LLM value-prop discovery prompts (extract_winnable_prompts, stashed on
    # sku_ctx by the async fan-out): NON-branded, SPECIFIC, value-prop-anchored
    # queries the product can realistically win ("best shea butter hair
    # treatment for damaged hair") — vs the generic "best hair oil" heads above,
    # which are kept only as a benchmark. Tagged "category" so they classify as
    # discovery (category_head / problem_jtbd) and feed product_competitiveness.
    # LLM discovery prompts: value-prop winnable + P4a scenario-elicited.
    # INTERLEAVED (w1, e1, w2, e2, ...) because the budgeter consumes the
    # mid-specific base pool in order — appending sequentially gave winnable's
    # 5 all the slots and dropped every elicited scenario probe at the wedge's
    # target=14 (validation run b77a15b2). Interleaving splits the pool fairly:
    # at 14 -> ~3 winnable + 2 elicited; at 40 -> all of both.
    _winnable_texts = [
        str(w or "").strip() for w in (sku_ctx.get("_winnable_prompts") or [])
        if str(w or "").strip()
    ]
    _elicited_texts = [
        str(e or "").strip() for e in (sku_ctx.get("_scenario_elicited") or [])
        if str(e or "").strip()
    ]
    for i in range(max(len(_winnable_texts), len(_elicited_texts))):
        if i < len(_winnable_texts):
            specs.append((_winnable_texts[i], "category"))
        if i < len(_elicited_texts):
            specs.append((_elicited_texts[i], "category"))
    if variant_label and variant_label.lower() not in title.lower():
        # Use the human variant label (e.g. "14 Servings, 2-Week Routine") with
        # the full identity, not the opaque variant id.
        specs.extend([
            (f"{title} {variant_label}", "identity"),
            (f"buy {title} ({variant_label}) online", "identity"),
        ])

    # Deep tier only (spec 2026-07-21): the market-referential blocks —
    # comparison/substitution, incumbent contest, price band, market
    # availability, recency, routine/compat. Standard runs never reach this
    # (the fanout stashes _audit_tier only on deep runs), so their probe set
    # stays byte-unchanged. The dedupe below absorbs overlaps with the
    # challenger-wedge shapes (e.g. the price-band prompt).
    if str((sku_ctx or {}).get("_audit_tier") or "").strip().lower() == "deep":
        from services.deep_tier_prompts import build_deep_tier_specs

        # Seed priority: declared competitors lead (the merchant/BD knows who
        # is gunning for the shelf, incl. AEO-active brands the answer harvest
        # cannot see yet — the fanout stash puts them ahead of the prior-run
        # harvest), then the evidence-ranked harvest, topped up from the
        # profile's configured incumbents — so a merchant's FIRST deep audit
        # (no prior runs) still fires Blocks A/B against the brands that own
        # the head terms (GHD/Dyson on the VODANA pilot).
        from services.deep_tier_prompts import MAX_ANCHOR_SEEDS

        _deep_seeds = [
            str(s).strip()
            for s in ((sku_ctx or {}).get("_deep_competitor_seeds") or [])
            if str(s or "").strip()
        ]
        _seen_seeds = {s.lower() for s in _deep_seeds}
        for _incumbent in (getattr(profile, "wedge_incumbent_brands", ()) or ()):
            if len(_deep_seeds) >= MAX_ANCHOR_SEEDS:
                break
            _incumbent = str(_incumbent or "").strip()
            if _incumbent and _incumbent.lower() not in _seen_seeds:
                _deep_seeds.append(_incumbent)
                _seen_seeds.add(_incumbent.lower())
        deep_specs = build_deep_tier_specs(
            title=title,
            category=str(unbranded_category or ""),
            competitors=_deep_seeds,
            price_band_usd=_wedge_price_band_usd(sku_ctx or {}),
            origin_terms=_graph_class_values(attribute_graph, "geography"),
            market="US",
            year=datetime.now(timezone.utc).year,
            routine_shapes=getattr(profile, "deep_routine_shapes", ()) or (),
        )
        specs.extend(deep_specs)
        # Stash for the budgeter: deep blocks must win leftover slots over the
        # generic template tail (same float the LLM discovery prompts get) and
        # get their source stamp for observability + axis_metadata emission.
        sku_ctx["_deep_tier_queries"] = sorted(
            {q.strip().lower() for q, _axis in deep_specs if q.strip()}
        )

    specs = _dedupe_query_specs(specs)
    return specs, title, str(product_type or unbranded_category or "product")


def _query_tuple_records(
    specs: List[Tuple[str, str]],
) -> List[Dict[str, Any]]:
    return [{"query": query, "axis": axis} for query, axis in specs]


def _dedupe_query_spec_records(
    records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        query = str(record.get("query") or "").strip()
        if not query or not _is_well_formed_query(query):
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        row = dict(record)
        row["query"] = query
        row["axis"] = str(row.get("axis") or "intent").strip() or "intent"
        out.append(row)
    return out


def _fill_per_sku_query_records(
    records: List[Dict[str, Any]],
    *,
    target: int,
    title: str,
    filler_pool: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    # Fill toward `target` with REAL query variants from `filler_pool` (extra
    # category permutations), never synthetic "{title} shopper question N"
    # placeholders — those burned credits on meaningless probes and polluted
    # results. If real variants are exhausted, return fewer real queries rather
    # than padding with junk (honest under-fill > garbage probes).
    records = _dedupe_query_spec_records(records)
    target = max(1, int(target or 0))
    if len(records) >= target:
        return records[:target]
    if filler_pool:
        records = _dedupe_query_spec_records(records + list(filler_pool))
    return records[:target]


def _product_has_attributes_raw(product: Mapping[str, Any]) -> bool:
    attrs = product.get("attributes_raw")
    if not isinstance(attrs, dict):
        return False
    return any(value not in (None, "", [], {}) for value in attrs.values())


def _sidewalk_query_records_for_sku(
    sku_ctx: Dict[str, Any],
    *,
    title: str,
    product_type: str,
    prompts_per_sku: int,
) -> List[Dict[str, Any]]:
    product = _get_product(sku_ctx or {})
    if not _product_has_attributes_raw(product):
        return []

    graph = _attribute_graph_for_probes(sku_ctx, product)
    # Win-the-specific-long-tail: generate enough stacked prompts for the specific
    # lane to be the MAJORITY of the budget (reserve ~6 for the thin diagnostic
    # head/branded spine). Was hard-capped at 16, which starved the only lane that
    # can produce a win.
    target = max(8, int(prompts_per_sku or 0) - 6)
    specs = generate_sidewalk_query_specs(
        graph,
        title=title,
        product_type=product_type,
        n=target,
        sku_ctx=sku_ctx,
    )
    records: List[Dict[str, Any]] = []
    for spec in specs:
        query = str(spec.get("query") or "").strip()
        if not query:
            continue
        records.append({
            "query": query,
            "axis": "sidewalk",
            "attribute_basis": list(spec.get("attribute_basis") or []),
            "evidence": list(spec.get("evidence") or []),
            "intent_weight": float(spec.get("intent_weight") or 0.0),
        })
    return _dedupe_query_spec_records(records)


# Win-the-specific-long-tail (Step 2): how many specific candidate prompts to
# generate per SKU (a generous deterministic pool) and how many to surface as
# "test these next" suggestions per SKU / per brand. The pool exceeds the probe
# budget so a rich SKU reliably yields an un-probed tail; thin SKUs naturally
# yield fewer (honest under-fill).
_SUGGESTED_PROMPT_POOL = 40
_SUGGESTED_PROMPTS_PER_SKU = 6
_SUGGESTED_PROMPTS_BRAND_MAX = 12


def _suggested_prompts_for_sku(
    sku_ctx: Dict[str, Any],
    *,
    opportunity: Dict[str, Any],
    attribute_graph: Mapping[str, Any],
    max_suggestions: int = _SUGGESTED_PROMPTS_PER_SKU,
) -> List[Dict[str, Any]]:
    """Step 2 of win-the-specific-long-tail: surface the specific, attribute-stacked
    prompts the engine BUILDS from this SKU's evidenced attributes but did NOT probe
    in this audit — the niches the merchant is positioned to own and can test next.

    Deterministic, no API churn (`generate_sidewalk_query_specs` is pure). Honest:
    we subtract every query that WAS probed (any axis), so a thin SKU whose every
    generated prompt already ran yields [] — no synthetic padding, no re-listing
    what the audit already measured.

    #1503: deliberately NOT gated on `attributes_raw` — catalog_products rows
    have no such column, so that gate silently emptied suggested_prompts for
    EVERY connected-catalog SKU in every vertical (only url-audit synthetic
    contexts carry attributes_raw). The generator reads title/product_type/
    description off the product itself and an empty spec list already returns
    [] — the gate was strictly stricter than the generator's real inputs. The
    probe lane (`_sidewalk_query_records_for_sku`) keeps its gate: probe-set
    composition is priced/pinned behavior and changes there need their own
    review.
    """
    product = _get_product(sku_ctx or {})
    title = resolve_sku_identity(sku_ctx or {}).get("name") or (
        _get_sku(sku_ctx or {}).get("title") or product.get("title") or ""
    )
    product_type = product.get("product_type") or product.get("category") or ""
    specs = generate_sidewalk_query_specs(
        attribute_graph if isinstance(attribute_graph, dict) else {},
        title=str(title),
        product_type=str(product_type),
        n=_SUGGESTED_PROMPT_POOL,
        sku_ctx=sku_ctx,
    )
    probed = {
        str(row.get("normalized_query") or row.get("query") or "").strip().lower()
        for row in ((opportunity or {}).get("per_prompt") or [])
        if isinstance(row, dict)
    }
    probed.discard("")
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for spec in specs:
        query = str(spec.get("query") or "").strip()
        key = query.lower()
        if not query or key in probed or key in seen:
            continue
        seen.add(key)
        out.append({
            "query": query,
            "attribute_basis": list(spec.get("attribute_basis") or []),
            "intent_weight": float(spec.get("intent_weight") or 0.0),
        })
        if len(out) >= max_suggestions:
            break
    return out


def build_suggested_prompts(
    per_sku_reports: List[Dict[str, Any]],
    *,
    max_total: int = _SUGGESTED_PROMPTS_BRAND_MAX,
) -> Dict[str, Any]:
    """Brand-level rollup of the per-SKU `suggested_prompts`: the specific niches the
    engine computed (from evidenced attributes) but didn't probe — the prompts the
    merchant can 1-click add to test where they can win. Deduped by normalized
    query across SKUs (highest intent_weight kept), ranked by specificity weight.

    Disjoint from `where_you_can_win.targets` by construction: targets are PROBED
    open lanes; these are UN-probed candidates (anything probed is filtered out
    upstream in `_suggested_prompts_for_sku`).
    """
    by_q: Dict[str, Dict[str, Any]] = {}
    for report in per_sku_reports or []:
        if not isinstance(report, dict):
            continue
        identity = report.get("identity") or {}
        sku_name = (
            identity.get("name")
            or report.get("sku_title")
            or report.get("sku_key")
        )
        for s in report.get("suggested_prompts") or []:
            if not isinstance(s, dict):
                continue
            q = str(s.get("query") or "").strip().lower()
            if not q:
                continue
            iw = float(s.get("intent_weight") or 0.0)
            prev = by_q.get(q)
            if prev is None or iw > float(prev.get("intent_weight") or 0.0):
                by_q[q] = {
                    "query": s.get("query"),
                    "normalized_query": q,
                    "sku": sku_name,
                    "sku_key": report.get("sku_key"),
                    "attribute_basis": list(s.get("attribute_basis") or []),
                    "intent_weight": iw,
                    "source": "sidewalk_candidate",
                }
    prompts = sorted(
        by_q.values(), key=lambda p: -float(p.get("intent_weight") or 0.0)
    )[:max_total]
    return {
        "prompts": prompts,
        "has_prompts": bool(prompts),
        # Honest empty state: since the winnable/sidewalk prompts are budgeted
        # INTO the audit itself (spec-matched prompt work, #1281/#1284), the
        # common case is that every specific prompt was already probed — the
        # old rationale ("this audit didn't test yet") then described an empty
        # list with a promise it couldn't keep.
        "rationale": (
            (
                "Specific, attribute-stacked prompts built from your product's "
                "verified attributes that this audit didn't test yet — the niches "
                "you're positioned to own. Add them to your prompts to measure "
                "where you can win."
            )
            if prompts
            else (
                "All of this product's specific winnable prompts were already "
                "probed in this audit (the attribute-stacked discovery queries "
                "above) — there's nothing untested left to suggest."
            )
        ),
    }


def _append_records(
    out: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    *,
    limit: int,
) -> None:
    if limit <= 0:
        return
    seen = {str(item.get("query") or "").strip().lower() for item in out}
    for record in candidates:
        query = str(record.get("query") or "").strip()
        if not query:
            continue
        key = query.lower()
        if key in seen:
            continue
        out.append(record)
        seen.add(key)
        if len(out) >= limit:
            return


def _take_axis_records(
    records: List[Dict[str, Any]],
    axes: set[str],
    *,
    count: int,
    selected: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    taken: List[Dict[str, Any]] = []
    seen = {str(item.get("query") or "").strip().lower() for item in selected}
    for record in records:
        if str(record.get("axis") or "") not in axes:
            continue
        query = str(record.get("query") or "").strip()
        if not query or query.lower() in seen:
            continue
        taken.append(record)
        seen.add(query.lower())
        if len(taken) >= count:
            break
    return taken


def _sidewalk_budget(target: int, available: int) -> int:
    if available <= 0:
        return 0
    if target >= 14:
        desired = 6
    elif target >= 12:
        desired = 4
    else:
        desired = max(1, target // 3)
    return min(available, desired)


def _budgeted_wedge_query_records(
    *,
    base_records: List[Dict[str, Any]],
    sidewalk_records: List[Dict[str, Any]],
    target: int,
    title: str,
    filler_pool: Optional[List[Dict[str, Any]]] = None,
    priority_queries: Optional[AbstractSet[str]] = None,
) -> List[Dict[str, Any]]:
    # Win-the-specific-long-tail allocation (credit-neutral; same `target`):
    #  1. a THIN diagnostic spine — 2 navigational + 2 head + 2 trust — the honest
    #     "yes, you lose the head term, as expected for a niche brand" anchor;
    #  2. the SPECIFIC stacked sidewalk long-tail as the MAJORITY of the budget (the
    #     only lane _is_open_lane can turn into a win — head terms are excluded by
    #     design, so spending the budget on them just re-confirms predictable losses);
    #  3. mid-specific (problem_jtbd / constraint) + remainder fill what's left.
    selected: List[Dict[str, Any]] = []
    for axis_set, count in (
        ({"intent"}, 2),     # navigational
        ({"category"}, 2),   # the 2 demoted head terms (first category specs)
        ({"review"}, 2),     # trust
    ):
        _append_records(
            selected,
            _take_axis_records(base_records, axis_set, count=count, selected=selected),
            limit=target,
        )
    # Reserve a little for mid-specific base prompts, give the rest to the stacked
    # specific long-tail.
    mid_reserve = min(6, max(2, target // 4))
    sidewalk_take = max(2, target - len(selected) - mid_reserve)
    _append_records(selected, sidewalk_records[:sidewalk_take], limit=target)

    selected_keys = {
        str(record.get("query") or "").strip().lower()
        for record in selected
    }
    remaining_base = [
        record for record in base_records
        if str(record.get("query") or "").strip().lower() not in selected_keys
    ]
    remaining_sidewalk = [
        record for record in sidewalk_records
        if str(record.get("query") or "").strip().lower() not in selected_keys
    ]
    # Leftover-slot priority: the LLM value-prop prompts (extract_winnable_prompts
    # + scenario-elicited) are SPECIFIC and winnable — they must beat the generic
    # category-template tail ("best {cat} for {audience}") for the mid-reserve
    # slots. They sit AFTER the templates in base_records order, so a plain
    # `remaining_base` fill dropped them at target=14 (the whole point of Gemini
    # generation was being wasted). Float ONLY those to the front; everything else
    # keeps its original base-before-sidewalk fill order (moving all sidewalk ahead
    # of the mid-specific base prompts stole the reserve — it dropped the audience
    # discovery query "multivitamin for women" at target=14).
    priority = priority_queries or frozenset()
    remaining_base_priority = [
        record for record in remaining_base
        if str(record.get("query") or "").strip().lower() in priority
    ]
    remaining_base_other = [
        record for record in remaining_base
        if str(record.get("query") or "").strip().lower() not in priority
    ]
    return _fill_per_sku_query_records(
        selected
        + remaining_base_priority
        + remaining_base_other
        + remaining_sidewalk,
        target=target,
        title=title,
        filler_pool=filler_pool,
    )


# --- Prompt-mix rebalance (#1521) -------------------------------------------
# Real buyers overwhelmingly ask WITHOUT naming a target product. Branded prompts
# (navigational/trust — "where can I buy {title}", "{title} reviews", "is {brand}
# legit") are ~always cited (findability ≈ 100%), so a branded-heavy set
# over-measures recall and under-measures the unbranded discovery demand that
# actually decides AI visibility. We CAP branded prompts at a minority SHARE of
# the per-SKU budget and give the remainder to unbranded discovery/category/
# problem/sidewalk shapes. A branded FLOOR preserves a minimal identity/trust
# baseline. Both are config knobs (env-overridable) so the ratio isn't hardcoded.
_BRANDED_PROMPT_CAP_DEFAULT = 0.3
_BRANDED_PROMPT_FLOOR_DEFAULT = 2
_BRANDED_PROMPT_CAP_ENV = "AGENT_AUDIT_BRANDED_PROMPT_CAP"
_BRANDED_PROMPT_FLOOR_ENV = "AGENT_AUDIT_BRANDED_PROMPT_FLOOR"


def _branded_prompt_cap_share() -> float:
    """Max branded SHARE of the per-SKU budget (default 0.3). Env-overridable via
    AGENT_AUDIT_BRANDED_PROMPT_CAP; a malformed/out-of-range value falls back to
    the default and is clamped to [0, 1] so a bad env can't invert the intent."""
    import os

    raw = os.getenv(_BRANDED_PROMPT_CAP_ENV)
    if raw is None or not str(raw).strip():
        return _BRANDED_PROMPT_CAP_DEFAULT
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _BRANDED_PROMPT_CAP_DEFAULT
    return min(1.0, max(0.0, val))


def _branded_prompt_floor() -> int:
    """Min branded prompts kept for the identity/trust baseline (default 2).
    Env-overridable via AGENT_AUDIT_BRANDED_PROMPT_FLOOR."""
    import os

    raw = os.getenv(_BRANDED_PROMPT_FLOOR_ENV)
    if raw is None or not str(raw).strip():
        return _BRANDED_PROMPT_FLOOR_DEFAULT
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _BRANDED_PROMPT_FLOOR_DEFAULT


def _is_branded_record(record: Mapping[str, Any]) -> bool:
    """True when a prompt NAMES the product/brand (navigational or trust intent) —
    the findability-recall lane the audit over-weighted before #1521. Reuses the
    canonical `_intent_axis_for` classifier so it stays in lockstep with scoring."""
    intent = _intent_axis_for(
        record.get("normalized_query") or record.get("query"), record.get("axis")
    )
    return intent in _BRANDED_INTENTS


def _branded_prompt_budget(target: int) -> int:
    """Max branded prompts for a `target`-prompt SKU budget: a capped SHARE
    (default 30%) but never below the identity/trust FLOOR (default 2) and never
    above the whole budget. #1521."""
    target = max(0, int(target or 0))
    if target <= 0:
        return 0
    cap = int(target * _branded_prompt_cap_share())
    floor = min(_branded_prompt_floor(), target)
    return min(target, max(floor, cap))


def _enforce_prompt_mix(
    records: List[Dict[str, Any]],
    *,
    target: int,
    unbranded_backfill: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Cap branded prompts at `_branded_prompt_budget` and backfill the freed
    slots with UNBRANDED discovery prompts. Order-preserving: keeps the FIRST
    branded records (the navigational+trust baseline the spine emits first) and
    drops the surplus, so the identity/trust floor is always the retained set.
    #1521."""
    branded_budget = _branded_prompt_budget(target)
    kept: List[Dict[str, Any]] = []
    dropped_branded: List[Dict[str, Any]] = []
    branded_kept = 0
    for record in records:
        if _is_branded_record(record):
            if branded_kept >= branded_budget:
                dropped_branded.append(record)  # surplus — try to replace below
                continue
            branded_kept += 1
        kept.append(record)
    # Prefer UNBRANDED discovery prompts for the freed slots.
    if len(kept) < target and unbranded_backfill:
        unbranded_only = [
            record for record in unbranded_backfill if not _is_branded_record(record)
        ]
        _append_records(kept, unbranded_only, limit=target)
    # Last resort: if unbranded demand is exhausted, RESTORE surplus branded rather
    # than SHRINK the probe set — a genuinely branded-only thin SKU keeps its
    # coverage. The cap only bites when there is unbranded demand to measure
    # instead, which is exactly the case #1521 targets.
    if len(kept) < target and dropped_branded:
        _append_records(kept, dropped_branded, limit=target)
    return kept[:target]


def _prompt_mix_summary(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Branded vs unbranded counts by intent axis for a single SKU's probe set.
    Rolled up per run into `brand_rollup.prompt_mix` (#1521)."""
    by_axis: Dict[str, int] = {}
    branded = 0
    unbranded = 0
    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        intent = _intent_axis_for(
            record.get("normalized_query") or record.get("query"),
            record.get("axis"),
        )
        by_axis[intent] = by_axis.get(intent, 0) + 1
        if intent in _BRANDED_INTENTS:
            branded += 1
        else:
            unbranded += 1
    total = branded + unbranded
    return {
        "branded": branded,
        "unbranded": unbranded,
        "total": total,
        "branded_share": round(branded / total, 3) if total else 0.0,
        "unbranded_share": round(unbranded / total, 3) if total else 0.0,
        "by_axis": by_axis,
    }


def _brand_prompt_mix(per_sku_reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-RUN prompt-mix telemetry (#1521): how the probe budget split across
    branded (navigational/trust — name the product) vs unbranded (discovery /
    category / problem / constraint) intents, by axis. Rolls up the per-SKU
    `citation_by_intent` totals — no new probes. Lets a reviewer confirm the
    ≤30%-branded target and makes cross-version score deltas interpretable."""
    by_intent = _brand_citation_by_intent(per_sku_reports)
    by_axis: Dict[str, int] = {}
    branded = 0
    unbranded = 0
    for intent, stats in by_intent.items():
        total = int((stats or {}).get("total") or 0)
        by_axis[intent] = total
        if intent in _BRANDED_INTENTS:
            branded += total
        else:
            unbranded += total
    grand = branded + unbranded
    return {
        "branded": branded,
        "unbranded": unbranded,
        "total": grand,
        "branded_share": round(branded / grand, 3) if grand else 0.0,
        "unbranded_share": round(unbranded / grand, 3) if grand else 0.0,
        "by_axis": by_axis,
        "branded_axes": sorted(_BRANDED_INTENTS),
        "cap_share": _branded_prompt_cap_share(),
        "floor": _branded_prompt_floor(),
    }


def _build_per_sku_audit_query_records(
    sku_ctx: Dict[str, Any],
    prompts_per_sku: int,
) -> List[Dict[str, Any]]:
    base_specs, title, product_type = _build_per_sku_base_query_specs(sku_ctx or {})
    target = max(1, int(prompts_per_sku or 0))
    base_records = _query_tuple_records(base_specs)
    sidewalk_records = _sidewalk_query_records_for_sku(
        sku_ctx or {},
        title=title,
        product_type=product_type,
        prompts_per_sku=target,
    )
    # Real category variants used ONLY to fill toward the budget when the SKU's
    # attribute graph is too thin to produce enough diverse queries — replaces the
    # old synthetic "shopper question N" junk. These are the superlatives demoted
    # out of the primary set: real queries, lower priority, coarse "category" tag.
    filler_cat = _clean_prompt_term(product_type)
    filler_pool = (
        _query_tuple_records([
            (f"top {filler_cat}", "category"),
            (f"recommended {filler_cat}", "category"),
            (f"best rated {filler_cat}", "category"),
            (f"popular {filler_cat}", "category"),
            (f"compare {filler_cat} options", "category"),
            (f"{filler_cat} reviews", "category"),
            (f"best {filler_cat} to buy online", "category"),
        ])
        if (
            filler_cat
            and filler_cat not in _GENERIC_CONTAINER_CATEGORIES
            and not _noisy_prompt_category(filler_cat)
        )
        else []
    )
    # Deep tier: cap the generic filler at 2. The deep budget is BILLED and its
    # 80 is a ceiling, not a promise — on a thin SKU the standard filler pool
    # let 7 near-duplicate superlative variants ("top/recommended/best rated
    # {cat}") pad the billed set (HBN pilot: 14% of probes). Underfill honestly
    # instead; standard runs keep the full pool byte-unchanged.
    if str((sku_ctx or {}).get("_audit_tier") or "").strip().lower() == "deep":
        filler_pool = filler_pool[:2]
    # LLM value-prop prompts (winnable + scenario-elicited): the SPECIFIC,
    # content-grounded discovery queries Gemini generated. They must win the
    # budgeter's leftover slots over the generic category-template tail — see
    # _budgeted_wedge_query_records. (Also stamped as `source` below for
    # per-model observability.)
    llm_prompt_queries = frozenset(
        str(q or "").strip().lower()
        for q in (
            list((sku_ctx or {}).get("_winnable_prompts") or [])
            + list((sku_ctx or {}).get("_scenario_elicited") or [])
        )
        if str(q or "").strip()
    )
    # Deep-tier blocks share the leftover-slot float: they're the tier's
    # raison d'être, so they must beat the generic template tail exactly like
    # the LLM discovery prompts do. Empty set on standard runs (no stash).
    deep_tier_queries = frozenset(
        str(q or "").strip().lower()
        for q in ((sku_ctx or {}).get("_deep_tier_queries") or ())
        if str(q or "").strip()
    )
    llm_prompt_queries = llm_prompt_queries | deep_tier_queries
    if not sidewalk_records:
        records = _fill_per_sku_query_records(
            base_records,
            target=target,
            title=title,
            filler_pool=filler_pool,
        )
    else:
        # All target sizes go through the sidewalk-majority budgeter (was: only
        # target<=16; target>16 took an unbudgeted base+sidewalk path that let
        # general base prompts crowd out the specific long-tail). Credit-neutral;
        # specific is now the majority.
        records = _budgeted_wedge_query_records(
            base_records=base_records,
            sidewalk_records=sidewalk_records,
            target=target,
            title=title,
            filler_pool=filler_pool,
            priority_queries=llm_prompt_queries,
        )
    # #1521: cap branded (product/brand-naming) prompts at a minority share of the
    # budget and backfill the freed slots with unbranded discovery prompts. The
    # budgeter/filler above compose the set for lane coverage; this is the final
    # mix gate so a branded-heavy base can't dominate. Unbranded backfill pool =
    # non-branded base prompts + the full sidewalk long-tail + category filler.
    records = _enforce_prompt_mix(
        records,
        target=target,
        unbranded_backfill=(
            [record for record in base_records if not _is_branded_record(record)]
            + list(sidewalk_records)
            + list(filler_pool or [])
        ),
    )
    # Stamp LLM-generated discovery prompts so per-model prompt quality is
    # comparable across runs (records keep their coarse "category" axis; the
    # source key is additive observability, mirroring "sidewalk_candidate").
    winnable = {
        str(w or "").strip().lower()
        for w in ((sku_ctx or {}).get("_winnable_prompts") or [])
        if str(w or "").strip()
    }
    if winnable:
        for record in records:
            if str(record.get("query") or "").strip().lower() in winnable:
                record["source"] = "llm_winnable"
    # Scenario-elicited probes: stamp source + a scenario basis marker so they
    # count in the P0 coverage metric and are diffable per generator model.
    elicited = {
        str(w or "").strip().lower()
        for w in ((sku_ctx or {}).get("_scenario_elicited") or [])
        if str(w or "").strip()
    }
    if elicited:
        for record in records:
            if str(record.get("query") or "").strip().lower() in elicited:
                record["source"] = "llm_scenario"
                basis = list(record.get("attribute_basis") or [])
                if "scenario:elicited" not in basis:
                    basis.append("scenario:elicited")
                record["attribute_basis"] = basis
    # Deep-tier records: the source stamp is what carries their axis (incl.
    # the internal-first "comparison") into axis_metadata -> probe payloads ->
    # report-side filters. LLM stamps above win on the (rare) query collision.
    if deep_tier_queries:
        for record in records:
            if record.get("source"):
                continue
            if str(record.get("query") or "").strip().lower() in deep_tier_queries:
                record["source"] = _DEEP_TIER_PROMPT_SOURCE
    # P0 scenario-coverage metric (scenario-demand plan): how much of the probe
    # budget tests scenario/occasion buy-trigger demand. Baseline was 0.
    scenario_count = sum(
        1
        for record in records
        if any(
            str(b).startswith("scenario:")
            for b in (record.get("attribute_basis") or [])
        )
    )
    if records:
        logger.info(
            "prompt-budget: scenario coverage %d/%d for sku=%s",
            scenario_count, len(records),
            (sku_ctx or {}).get("sku_key"),
        )
    return records


# Generator stamps for the LLM value-prop discovery prompts (winnable +
# scenario-elicited). Threaded into axis_metadata as `prompt_source` so the
# lane classifier (sku_lane_priority) can tell a SPECIFIC, content-grounded
# discovery prompt from a generic head term — both carry the coarse
# axis="category", which previously made the wedge classify winnable prompts
# as head-prompt pressure and tell the merchant NOT to chase them.
_LLM_PROMPT_SOURCES = frozenset({"llm_winnable", "llm_scenario"})

# Generator stamp for merchant-authored prompts (brand-level slots + the
# per-SKU custom_prompts_by_url lane). Threaded into axis_metadata like the
# LLM stamps so (a) the evidence selector never drops a deliberately-head
# merchant test as "broad head pressure" and (b) renderers can badge the
# merchant's own prompts in the per-prompt table.
_MERCHANT_PROMPT_SOURCE = "merchant_custom"


def _query_metadata_from_records(
    records: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    metadata: Dict[str, Dict[str, Any]] = {}
    for record in records:
        prompt_source = str(record.get("source") or "").strip()
        is_stamped_prompt = (
            prompt_source in _LLM_PROMPT_SOURCES
            or prompt_source == _MERCHANT_PROMPT_SOURCE
            or prompt_source == _DEEP_TIER_PROMPT_SOURCE
        )
        if record.get("axis") != "sidewalk" and not is_stamped_prompt:
            continue
        query = str(record.get("query") or "").strip()
        if not query:
            continue
        entry: Dict[str, Any] = {
            "axis": str(record.get("axis") or "sidewalk"),
        }
        # Sidewalk/LLM records carry a REAL generator weight + basis. Merchant
        # records don't — emitting a hardcoded 0.0 here used to flow into
        # sidewalk_intent_weight and force every merchant prompt's opportunity
        # score to zero (the passthrough beats the heuristic classifier), so a
        # merchant record contributes only its axis/stamps and the heuristic
        # intent stays in charge downstream.
        if record.get("axis") == "sidewalk" or prompt_source in _LLM_PROMPT_SOURCES:
            entry.update({
                "attribute_basis": list(record.get("attribute_basis") or []),
                "evidence": list(record.get("evidence") or []),
                "intent_weight": float(record.get("intent_weight") or 0.0),
            })
        if is_stamped_prompt:
            entry["prompt_source"] = prompt_source
        if record.get("custom_scope"):
            # "sku" (custom_prompts_by_url) vs "brand" (the one-shot slots) —
            # lets the brand-level "Your prompts" panel exclude per-SKU rows.
            entry["custom_scope"] = str(record["custom_scope"])
        metadata[query] = entry
    return metadata


def _build_per_sku_audit_query_metadata(
    sku_ctx: Dict[str, Any],
    prompts_per_sku: int,
) -> Dict[str, Dict[str, Any]]:
    """Expose sidewalk evidence beside the legacy tuple prompt API.

    `_build_per_sku_audit_query_specs` must keep returning `(query, axis)`
    tuples for existing probe callers. This sibling is the rendering seam for
    later pieces that need query -> attribute_basis/evidence without changing
    that tuple contract.
    """
    return _query_metadata_from_records(
        _build_per_sku_audit_query_records(sku_ctx or {}, prompts_per_sku)
    )


def _build_per_sku_audit_query_specs(
    sku_ctx: Dict[str, Any],
    prompts_per_sku: int,
) -> List[Tuple[str, str]]:
    return [
        (str(record.get("query") or ""), str(record.get("axis") or "intent"))
        for record in _build_per_sku_audit_query_records(
            sku_ctx or {},
            prompts_per_sku,
        )
    ]


def _chunk_query_specs(
    specs: List[Tuple[str, str]],
    chunk_size: int = _PER_SKU_AUDIT_UPSTREAM_CHUNK_SIZE,
) -> List[List[Tuple[str, str]]]:
    size = max(1, int(chunk_size))
    return [specs[i:i + size] for i in range(0, len(specs), size)]


def _scan_mode_for_query_spec(query: str, axis: str) -> str:
    """Pick the scan mode that HONESTLY measures a (query, axis) spec.

    Branded/navigational/trust queries name the product, so findability
    (open_product_visibility_test) is the right test. Everything else —
    discovery category/problem/constraint queries and merchant custom prompts —
    must surface the brand on its own merit, so they run under organic category
    visibility (category_visibility_test). See _PER_SKU_BRANDED_SCAN_MODE."""
    if _intent_axis_for(query, axis) in _BRANDED_INTENTS:
        return _PER_SKU_BRANDED_SCAN_MODE
    return _PER_SKU_DISCOVERY_SCAN_MODE


def _partition_query_specs_by_scan_mode(
    specs: List[Tuple[str, str]],
) -> List[Tuple[str, List[Tuple[str, str]]]]:
    """Group query specs by scan mode (insertion-ordered) so each group is
    probed under the mode that measures it correctly. The fanout makes one
    upstream call per (provider, mode, chunk). Returns [(scan_mode, specs)...]."""
    groups: Dict[str, List[Tuple[str, str]]] = {}
    for query, axis in specs:
        groups.setdefault(
            _scan_mode_for_query_spec(query, axis), []
        ).append((query, axis))
    return list(groups.items())


def _per_sku_probe_context(
    sku_ctx: Dict[str, Any],
    query_specs: List[Tuple[str, str]],
) -> Dict[str, Any]:
    product = _get_product(sku_ctx or {})
    sku = _get_sku(sku_ctx or {})
    title = (
        sku.get("title")
        or product.get("title")
        or sku_ctx.get("sku_title")
        or sku_ctx.get("sku_key")
        or "SKU"
    )
    merchant_url = (
        product.get("canonical_url")
        or product.get("pivota_canonical_url")
        or sku_ctx.get("canonical_url")
        or sku_ctx.get("pivota_canonical_url")
        or ""
    )
    return {
        "queries": [query for query, _axis in query_specs],
        "product": {
            "title": str(title),
            "vendor": (
                product.get("brand")
                or product.get("vendor")
                or ""
            ),
            "product_type": (
                product.get("product_type")
                or product.get("category")
                or ""
            ),
        },
        "merchant_pdp_url": merchant_url,
        "product_entity_id": sku_ctx.get("sku_key"),
    }


def _normalize_per_sku_probe_payload(
    *,
    result: Dict[str, Any],
    requested_provider: str,
    sku_key: str,
    sku_ctx: Dict[str, Any],
    query_specs: List[Tuple[str, str]],
    probe_run_id: str,
    model_info: Mapping[str, Any],
    query_metadata: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    payload = dict(result or {})
    actual_provider = str(
        payload.get("provider") or requested_provider
    ).strip().lower()
    axis_by_query = {query.strip().lower(): axis for query, axis in query_specs}
    metadata_by_query = {
        str(query or "").strip().lower(): dict(meta or {})
        for query, meta in (query_metadata or {}).items()
    }
    product = _get_product(sku_ctx or {})
    canonical_url = product.get("canonical_url") or sku_ctx.get("canonical_url")
    pivota_url = (
        product.get("pivota_canonical_url")
        or sku_ctx.get("pivota_canonical_url")
    )
    normalized_runs: List[Dict[str, Any]] = []
    for run in payload.get("raw_runs") or []:
        if not isinstance(run, dict):
            continue
        row = dict(run)
        query = str(row.get("query") or "").strip()
        row["provider"] = actual_provider
        row["_provider"] = actual_provider
        row["_probe_run_id"] = probe_run_id
        meta = (
            dict(row.get("axis_metadata"))
            if isinstance(row.get("axis_metadata"), dict)
            else {}
        )
        meta.update({
            "sku_key": sku_key,
            "product_key": sku_ctx.get("product_key") or product.get("product_key"),
            "axis": axis_by_query.get(query.lower(), meta.get("axis") or "intent"),
            "source": "v3_per_sku_audit",
            "upstream_scan_mode": payload.get("scan_mode"),
        })
        sidewalk_meta = metadata_by_query.get(query.lower())
        if sidewalk_meta:
            # Only sidewalk/LLM entries carry a real generator weight — a
            # merchant-prompt entry has none, and stamping a synthetic 0.0
            # would zero its opportunity score (the passthrough in
            # sku_opportunity beats the heuristic intent classifier).
            if "intent_weight" in sidewalk_meta:
                meta.update({
                    "sidewalk_attribute_basis": list(
                        sidewalk_meta.get("attribute_basis") or []
                    ),
                    "sidewalk_evidence": list(sidewalk_meta.get("evidence") or []),
                    "sidewalk_intent_weight": float(
                        sidewalk_meta.get("intent_weight") or 0.0
                    ),
                })
            # Generator stamp (llm_winnable / llm_scenario / merchant_custom):
            # lets the lane classifier tell a SPECIFIC LLM discovery prompt (or
            # a deliberate merchant test) from a generic head term. Distinct
            # key from the pipeline `source` set just above.
            if sidewalk_meta.get("prompt_source"):
                meta["prompt_source"] = str(sidewalk_meta["prompt_source"])
            if sidewalk_meta.get("custom_scope"):
                meta["custom_scope"] = str(sidewalk_meta["custom_scope"])
        row["axis_metadata"] = meta
        if not isinstance(row.get("url_match"), dict):
            row["url_match"] = {
                "target_url": canonical_url or pivota_url,
                "in_grounding": _url_in_sources(row, [canonical_url, pivota_url]),
                "llm_self_report": {},
            }
        normalized_runs.append(row)

    return {
        "probe_run_id": probe_run_id,
        "scan_mode": "per_sku_audit",
        "upstream_scan_mode": payload.get("scan_mode"),
        "provider": actual_provider,
        "requested_provider": requested_provider,
        "sku_key": sku_key,
        "product_key": sku_ctx.get("product_key") or product.get("product_key"),
        "model": payload.get("model") or model_info.get("model"),
        "model_is_override": bool(
            payload.get("model_is_override")
            or model_info.get("model_is_override")
        ),
        **(
            {"default_model": model_info.get("default_model")}
            if model_info.get("default_model") else {}
        ),
        "runs_count": len(normalized_runs),
        "scores": payload.get("scores") or {},
        "findings": payload.get("findings") or [],
        "usage": payload.get("usage") or {},
        "raw_runs": normalized_runs,
    }


def _failed_per_sku_probe_payload(
    *,
    provider: str,
    sku_key: str,
    sku_ctx: Dict[str, Any],
    probe_run_id: str,
    error: str,
    model_info: Mapping[str, Any],
    scan_mode: str = _PER_SKU_AUDIT_PROBE_SCAN_MODE,
) -> Dict[str, Any]:
    product = _get_product(sku_ctx or {})
    return {
        "probe_run_id": probe_run_id,
        "scan_mode": "per_sku_audit",
        "upstream_scan_mode": scan_mode,
        "provider": provider,
        "requested_provider": provider,
        "sku_key": sku_key,
        "product_key": sku_ctx.get("product_key") or product.get("product_key"),
        "status": "probe_failed",
        "error": str(error or "")[:500],
        "model": model_info.get("model"),
        "model_is_override": bool(model_info.get("model_is_override")),
        **(
            {"default_model": model_info.get("default_model")}
            if model_info.get("default_model") else {}
        ),
        "runs_count": 0,
        "scores": {"visibility_score": 0},
        "findings": [],
        "usage": {},
        "raw_runs": [],
    }


async def _probe_per_sku_ctx(
    *,
    sku_ctx: Dict[str, Any],
    merchant_id: str,
    coverage: Mapping[str, Any],
    provider_model_metadata: Mapping[str, Any],
    prompts_per_sku: int,
    audit_run_id: Optional[str],
    custom_prompts: Optional[List[str]] = None,
    pinned_custom_prompts: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Run the normalized per-SKU audit probe loop for an already-built ctx.

    `custom_prompts` are brand-level merchant slots: probed this run, NOT
    pinned into the basis. `pinned_custom_prompts` are this SKU's own merchant
    prompts (custom_prompts_by_url): probed inside this SKU's context AND
    recorded into `_selected_specs_out`, so they enter the pinned measurement
    basis and gain week-over-week comparability.
    """
    safe_ctx = sku_ctx if isinstance(sku_ctx, dict) else {}
    sku_key = str(safe_ctx.get("sku_key") or "").strip() or "sku"
    target_prompts = max(1, int(prompts_per_sku or 0))
    # W2.1: when a prior run pinned the FULL selected set, reprobe EXACTLY those
    # queries — no spec rebuild, so the whole measurement basis (not just the
    # LLM lists W2 pins) is identical run-to-run. Confined to the probe path so
    # report-time metadata seams keep building fresh (they only render).
    pinned_specs = safe_ctx.get("_pinned_selected_specs")
    if isinstance(pinned_specs, list) and pinned_specs:
        query_records = [
            dict(r) for r in pinned_specs
            if isinstance(r, dict) and str(r.get("query") or "").strip()
        ]
        logger.info(
            "prompt-basis: reprobing %d pinned selected specs for sku=%s",
            len(query_records), sku_key,
        )
    else:
        query_records = _build_per_sku_audit_query_records(safe_ctx, target_prompts)
    existing = {str(r.get("query") or "").strip().lower() for r in query_records}
    existing.discard("")
    # This SKU's own merchant prompts (custom_prompts_by_url): probed INSIDE
    # the SKU's context so results join opportunity.per_prompt / win plan /
    # evidence chain, and recorded into the pinned basis below. The
    # source stamp keeps a deliberately-head merchant test out of the
    # broad-head evidence drop and lets the UI badge it.
    for prompt in pinned_custom_prompts or []:
        text = str(prompt or "").strip()
        if text and text.lower() not in existing:
            query_records.append({
                "query": text,
                "axis": "custom",
                "source": _MERCHANT_PROMPT_SOURCE,
                # Scope keeps per-SKU rows OUT of the brand-level "Your
                # prompts" panel (they live in the per-SKU surfaces).
                # Whitelisted in clean_selected_specs so it survives pinning.
                "custom_scope": "sku",
            })
            existing.add(text.lower())
    # Record the set to persist as the next run's pinned basis: the auto set
    # plus this SKU's merchant prompts (they join the comparable weekly set).
    # The brand-level slots appended below stay one-shot — NOT pinned.
    safe_ctx["_selected_specs_out"] = [dict(r) for r in query_records]
    # Append brand-level merchant-input prompt slots so they're actually probed
    # (deduped against the set). axis="custom" keeps them identifiable downstream.
    for prompt in custom_prompts or []:
        text = str(prompt or "").strip()
        if text and text.lower() not in existing:
            query_records.append({
                "query": text,
                "axis": "custom",
                "source": _MERCHANT_PROMPT_SOURCE,
                "custom_scope": "brand",
            })
            existing.add(text.lower())
    query_specs = [
        (str(record.get("query") or ""), str(record.get("axis") or "intent"))
        for record in query_records
    ]
    query_metadata = _query_metadata_from_records(query_records)
    provider_ids = list((coverage or {}).get("providers") or [])
    # Diagnose the "low prompts_per_sku -> 0 probes" anomaly: query-building
    # always yields >=1 spec for prompts_per_sku>=1 (proven by test), so a zero
    # probe count can only come from an empty provider set or an empty spec list.
    # Make either cause loud instead of silently persisting zero llm_probe_runs.
    if not query_specs or not provider_ids:
        logger.warning(
            "per-sku probe will run 0 times for sku_key=%s: prompts_per_sku=%s "
            "query_specs=%d providers=%d",
            sku_key, target_prompts, len(query_specs), len(provider_ids),
        )
    out: List[Dict[str, Any]] = []
    # Branded queries probe FINDABILITY; discovery/custom queries probe ORGANIC
    # category visibility — each under its own scan mode (one upstream call per
    # provider/mode/chunk). The probe context is identical for both modes (the
    # node uses product.title only for branded findability and the brand/vendor
    # for organic detection), so no per-mode context shaping is needed.
    mode_groups = _partition_query_specs_by_scan_mode(query_specs)
    for provider_id in provider_ids:
        model_info = provider_model_metadata.get(provider_id) or {}
        consecutive_failures = 0
        for scan_mode, mode_specs in mode_groups:
            mode_tag = (
                "branded" if scan_mode == _PER_SKU_BRANDED_SCAN_MODE else "discovery"
            )
            for chunk_idx, chunk in enumerate(_chunk_query_specs(mode_specs), start=1):
                probe_run_id = (
                    f"{audit_run_id or 'adhoc'}:{sku_key}:"
                    f"{provider_id}:per_sku:{mode_tag}:{chunk_idx}"
                )
                # Attempt the chunk with one retry on failure: a transient
                # timeout / rate-limit blip shouldn't silently drop this chunk's
                # prompts from the budget. Only a retried-then-still-failed chunk
                # is recorded failed (and counts toward the consecutive bail).
                result = None
                last_exc: Optional[Exception] = None
                for attempt in range(_PER_SKU_AUDIT_CHUNK_RETRIES + 1):
                    try:
                        result = await llm_client.probe(
                            scan_mode=scan_mode,
                            scan_target_id=probe_run_id,
                            merchant_id=str(merchant_id),
                            store_id=f"{merchant_id}_audit",
                            context=_per_sku_probe_context(safe_ctx, chunk),
                            provider=provider_id,
                            max_runs=len(chunk),
                            model=model_info.get("model"),
                            model_is_override=bool(
                                model_info.get("model_is_override")
                            ),
                        )
                        last_exc = None
                        break
                    except Exception as exc:  # noqa: BLE001 - isolate provider/chunk
                        last_exc = exc
                        if attempt < _PER_SKU_AUDIT_CHUNK_RETRIES:
                            logger.info(
                                "per-sku probe chunk failed; retrying sku=%s "
                                "provider=%s mode=%s chunk=%s attempt=%d/%d: %s",
                                sku_key, provider_id, mode_tag, chunk_idx,
                                attempt + 1, _PER_SKU_AUDIT_CHUNK_RETRIES, exc,
                            )
                            if _PER_SKU_AUDIT_CHUNK_RETRY_BACKOFF_S > 0:
                                await asyncio.sleep(
                                    _PER_SKU_AUDIT_CHUNK_RETRY_BACKOFF_S
                                )
                if last_exc is None:
                    out.append(
                        _normalize_per_sku_probe_payload(
                            result=result,
                            requested_provider=provider_id,
                            sku_key=sku_key,
                            sku_ctx=safe_ctx,
                            query_specs=chunk,
                            probe_run_id=probe_run_id,
                            model_info=model_info,
                            query_metadata=query_metadata,
                        )
                    )
                    consecutive_failures = 0
                else:
                    out.append(
                        _failed_per_sku_probe_payload(
                            provider=provider_id,
                            sku_key=sku_key,
                            sku_ctx=safe_ctx,
                            probe_run_id=probe_run_id,
                            error=str(last_exc),
                            model_info=model_info,
                            scan_mode=scan_mode,
                        )
                    )
                    consecutive_failures += 1
                    # Don't let one transient chunk timeout zero the SKU: keep
                    # probing later chunks. Only bail this (sku, provider) once
                    # failures are CONSECUTIVE (provider likely down), so we
                    # don't burn the full timeout on every remaining chunk.
                    if (
                        consecutive_failures
                        >= _PER_SKU_AUDIT_MAX_CONSECUTIVE_CHUNK_FAILURES
                    ):
                        break
            else:
                # This mode group finished without a consecutive-failure bail;
                # continue to the next mode group for this provider.
                continue
            # Inner chunk loop bailed (provider likely down) -> stop probing
            # remaining mode groups for this provider too.
            break
    return out


def _wedge_hero_index(hero_product: Mapping[str, Any]) -> int:
    for key in ("hero_index", "_wedge_hero_index"):
        try:
            return max(0, int(hero_product.get(key)))
        except (TypeError, ValueError):
            continue
    return 0


def _wedge_product_key(hero_product: Mapping[str, Any], sku_key: str) -> str:
    for key in ("product_key", "pdp_url", "canonical_url", "url", "title"):
        value = str(hero_product.get(key) or "").strip()
        if value:
            return value
    return sku_key


def _wedge_hero_sku_ctx(
    hero_product: Mapping[str, Any],
    *,
    merchant_id: str,
) -> Dict[str, Any]:
    hero_index = _wedge_hero_index(hero_product)
    sku_key = f"wedge:{hero_index}"
    title = str(hero_product.get("title") or hero_product.get("raw_title") or "Hero SKU").strip()
    vendor = str(hero_product.get("vendor") or hero_product.get("brand") or "").strip()
    pdp_url = str(
        hero_product.get("pdp_url")
        or hero_product.get("canonical_url")
        or hero_product.get("url")
        or ""
    ).strip()
    product_type = str(
        hero_product.get("product_type")
        or hero_product.get("category")
        or ""
    ).strip()
    attributes_raw = hero_product.get("attributes_raw")
    product = {
        "title": title,
        "raw_title": hero_product.get("raw_title") or title,
        "vendor": vendor,
        "brand": vendor,
        "product_type": product_type,
        "attributes_raw": attributes_raw if isinstance(attributes_raw, dict) else {},
        "canonical_url": pdp_url,
        "pivota_canonical_url": None,
    }
    return {
        "sku_key": sku_key,
        "merchant_id": str(merchant_id),
        "product_key": _wedge_product_key(hero_product, sku_key),
        "product": product,
        "sku": {"title": title, "sku_key": sku_key},
    }


def _strategic_brief_live_probe_enabled() -> bool:
    from config.settings import settings as app_settings
    from services.llm_synthesis import (
        LLMSynthesisError,
        configured_key_for_provider,
        normalize_provider,
    )

    if not getattr(app_settings, "strategic_brief_enabled", False):
        return False
    try:
        provider = normalize_provider(app_settings.strategic_brief_provider)
    except LLMSynthesisError:
        return False
    return bool(configured_key_for_provider(provider))


_WINNABLE_PROMPTS_SYSTEM = """You are a search-demand strategist for an AI-shopping-visibility audit.
Given ONE product's content, distill its KEY differentiating value propositions (specific ingredients,
target concern / use-case, audience, format, benefit) and write non-branded discovery search queries a real
shopper would type into an AI assistant AND that THIS product could realistically WIN — because they match
its specific differentiators, not the generic category.

RULES (this is a trust product — follow exactly):
- Use ONLY value propositions present in the PROVIDED CONTENT. NEVER invent ingredients, claims, benefits,
  certifications, audiences, or use-cases that are not in the content.
- Do NOT include the brand or product name — these are NON-BRANDED discovery queries.
- Do NOT output bare category heads ("best hair oil", "best serum"); those are unwinnable. EVERY query must
  anchor to a SPECIFIC differentiator (ingredient and/or concern and/or audience).
- Natural shopper phrasing, lower-case, 4-9 words, no punctuation gimmicks.
- Output ONLY a JSON array of 4-6 distinct query strings. No other text."""


def _parse_winnable_prompts(raw: Any) -> List[str]:
    # W3: the shared parser handles bare/fenced/embedded JSON arrays uniformly.
    from services.llm_io import parse_llm_str_array

    return parse_llm_str_array(raw, label="winnable_prompts")


def _resolve_prompt_gen_provider() -> Tuple[Optional[str], str]:
    """Resolve the stage-1 prompt-generation (provider, model) pair.

    Independently configurable so generators can be A/B compared in prod
    (PROMPT_GEN_PROVIDER/MODEL; default gemini — founder call 2026-07-03). The
    chain degrades by KEY availability — each candidate keeps its OWN model
    preference so a fallback provider never runs with another provider's model
    string. Returns (None, "") when no candidate has a configured key."""
    from config.settings import settings as app_settings
    from services.llm_synthesis import (
        LLMSynthesisError,
        configured_key_for_provider,
        default_model_for_provider,
        normalize_provider,
    )

    candidate_chain = (
        (
            str(getattr(app_settings, "prompt_gen_provider", "") or "").strip(),
            str(getattr(app_settings, "prompt_gen_model", "") or "").strip(),
        ),
        (
            str(getattr(app_settings, "strategic_brief_provider", "") or "").strip(),
            str(getattr(app_settings, "strategic_brief_model", "") or "").strip(),
        ),
        ("deepseek", ""),
    )
    seen_providers: set = set()
    for name, model_pref in candidate_chain:
        if not name:
            continue
        try:
            canonical = normalize_provider(name)
        except LLMSynthesisError:
            continue
        if canonical in seen_providers:
            continue
        seen_providers.add(canonical)
        if configured_key_for_provider(canonical):
            return canonical, (model_pref or default_model_for_provider(canonical))
        logger.warning(
            "prompt-gen: no API key for provider=%s — trying fallback",
            canonical,
        )
    return None, ""


async def extract_winnable_prompts(
    sku_ctx: Mapping[str, Any],
    *,
    max_prompts: int = 6,
) -> List[str]:
    """LLM value-prop extraction -> NON-branded, winnable, SPECIFIC
    discovery prompts grounded in the product's own content (title + description
    + tags). The audit then probes these instead of only generic category heads
    ("best hair oil") that no brand can realistically win. Best-effort: returns
    [] on disabled / no-key / parse / validation failure (caller falls back to
    the deterministic specs). Non-branded + content-grounded by construction;
    any brand-name leakage is filtered out."""
    from services.llm_synthesis import synthesize

    product = _get_product(sku_ctx or {})
    title = str(product.get("title") or "").strip()
    if not title:
        return []
    brand = str(product.get("brand") or product.get("vendor") or "").strip()
    attrs = product.get("attributes_raw")
    description, tags = "", []
    if isinstance(attrs, Mapping):
        description = str(attrs.get("description") or "")[:1500]
        if isinstance(attrs.get("tags"), list):
            tags = [str(t) for t in attrs["tags"]][:20]
    provider, model = _resolve_prompt_gen_provider()
    if not provider:
        logger.warning(
            "winnable-prompt extraction skipped: no configured provider key "
            "(chain exhausted)",
        )
        return []
    # Tier-1 retailer evidence (services/retailer_evidence.py): third-party
    # listing excerpts from prior runs. For thin own-page fetches this is often
    # the ONLY substantive product text — the extractor may use value props
    # stated there (they're still this product's published listing content, with
    # provenance), which keeps the no-invention rule intact.
    retailer_excerpts = [
        str(x or "").strip()
        for x in (product.get("_retailer_excerpts") or [])
        if str(x or "").strip()
    ][:6]
    content = {
        "title": title,
        "brand": brand or None,
        "product_type": product.get("product_type") or None,
        "description": description or None,
        "tags": tags or None,
        "retailer_listing_excerpts": retailer_excerpts or None,
    }
    user = "PRODUCT CONTENT:\n" + json.dumps(content, ensure_ascii=False, indent=2)
    try:
        result = await synthesize(
            system=_WINNABLE_PROMPTS_SYSTEM,
            user=user,
            provider=provider,
            model=model,
            max_tokens=1000,  # was 400 — too tight for a multi-item array (truncation-empty class)
        )
    except Exception:  # noqa: BLE001 - best-effort; fall back to deterministic specs
        logger.warning("winnable-prompt extraction failed for %r", title[:60], exc_info=True)
        return []
    brand_tokens = set(re.findall(r"[a-z0-9]+", brand.lower())) if brand else set()
    out: List[str] = []
    seen: set = set()
    for raw in _parse_winnable_prompts(result.get("text")):
        s = re.sub(r"\s+", " ", str(raw or "").strip().lower())
        if not s or len(s.split()) < 3:
            continue
        if brand_tokens & set(re.findall(r"[a-z0-9]+", s)):
            continue  # brand leaked in -> not a non-branded discovery query
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= max_prompts:
            break
    # Observability: this stage failed SILENTLY for a whole run once (ANUKO
    # 2026-07-02 — zero LLM prompts, no trace). Always log the outcome with the
    # provider/model so per-model prompt quality can be compared across runs
    # and an empty result is a visible signal, not a mystery.
    if out:
        logger.info(
            "winnable-prompts: provider=%s model=%s title=%r -> %d prompts: %s",
            provider, model, title[:60], len(out), out,
        )
    else:
        logger.warning(
            "winnable-prompts: provider=%s model=%s title=%r -> EMPTY "
            "(parse/validation dropped everything; raw len=%d finish_reason=%s)",
            provider, model, title[:60],
            len(str(result.get("text") or "")),
            result.get("finish_reason"),
        )
    return out


_SCENARIO_ELICIT_SYSTEM = """You are a shopping-demand researcher for AI assistants.
Given ONE product's category and verified attributes, enumerate scenario-framed shopper questions
that REAL users ask AI assistants where this CATEGORY could be the answer — occasions, travel,
activities, climate, life-stage, routines (packing for a beach trip, surviving a long flight,
humid-weather hair routine, wedding-day makeup that lasts).

RULES (this is a trust product — follow exactly):
- These are CATEGORY demand probes: the scenario must be plausible for the category. Do NOT assert
  product-specific claims, efficacy, or benefits the attributes don't support.
- No medical, pregnancy, or child-safety scenarios.
- Do NOT include any brand or product name — non-branded queries only.
- EVERY query must contain BOTH the category (or an obvious synonym) AND a concrete scenario.
- Natural shopper phrasing, lower-case, 5-10 words, no punctuation gimmicks.
- Output ONLY a JSON array of 4-6 distinct query strings. No other text."""


async def elicit_scenario_prompts(
    sku_ctx: Mapping[str, Any],
    *,
    category: str,
    max_prompts: int = 4,
) -> List[str]:
    """P4a scenario-demand mining: LLM-elicited scenario-framed shopper queries
    for the CATEGORY (not product claims). These run as exploratory probes —
    the existing demand loop (demand_signal -> open-lane / no-demand) is the
    validation gate before any lane is surfaced as a recommendation, per the
    demand-honesty principle. Best-effort: [] on any failure."""
    from services.llm_synthesis import synthesize

    category = str(category or "").strip()
    if not category:
        return []
    product = _get_product(sku_ctx or {})
    brand = str(product.get("brand") or product.get("vendor") or "").strip()
    provider, model = _resolve_prompt_gen_provider()
    if not provider:
        return []
    content = {
        "category": category,
        "product_type": product.get("product_type") or None,
        "title": str(product.get("title") or "")[:120] or None,
    }
    user = "PRODUCT CONTENT:\n" + json.dumps(content, ensure_ascii=False, indent=2)
    try:
        result = await synthesize(
            system=_SCENARIO_ELICIT_SYSTEM,
            user=user,
            provider=provider,
            model=model,
            max_tokens=1000,  # was 400 — too tight for a multi-item array (truncation-empty class)
        )
    except Exception:  # noqa: BLE001 - best-effort exploratory stage
        logger.warning("scenario-elicit failed for %r", category, exc_info=True)
        return []
    brand_tokens = set(re.findall(r"[a-z0-9]+", brand.lower())) if brand else set()
    category_tokens = set(re.findall(r"[a-z0-9가-힣]+", category.lower()))
    out: List[str] = []
    seen: set = set()
    for raw in _parse_winnable_prompts(result.get("text")):
        s = re.sub(r"\s+", " ", str(raw or "").strip().lower())
        tokens = re.findall(r"[a-z0-9가-힣]+", s)
        if not s or not (4 <= len(tokens) <= 12):
            continue
        if brand_tokens & set(tokens):
            continue  # brand leaked -> not a category demand probe
        if not (category_tokens & set(tokens)):
            continue  # must name the category, or the probe is unattributable
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= max_prompts:
            break
    if out:
        logger.info(
            "scenario-elicit: provider=%s model=%s category=%r -> %d prompts: %s",
            provider, model, category, len(out), out,
        )
    else:
        logger.warning(
            "scenario-elicit: provider=%s model=%s category=%r -> EMPTY "
            "(raw len=%d finish_reason=%s)",
            provider, model, category,
            len(str(result.get("text") or "")),
            result.get("finish_reason"),
        )
    return out


def _list_value(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


# Retailer / marketplace NAME tokens classify_host misses (its registry keys on
# hosts, and Coupang/Gmarket/etc. aren't in it). The "category winner" panel is
# about a competing PRODUCT, so a store must never be selected as the winner.
#
# MIGRATED to the beauty profile; this alias keeps the _competitor_is_brandlike
# call site byte-identical. (Phase 1 will add electronics retailers — bestbuy,
# bhphoto, crutchfield, newegg — to the electronics_audio profile and gate this
# lookup on the resolved vertical.)
_RETAILER_NAME_TOKENS = BEAUTY_PROFILE.retailer_tokens


def _competitor_is_brandlike(name: str, *, profile: VerticalProfile = BEAUTY_PROFILE) -> bool:
    """True when `name` is a plausible competing brand/product — not an
    ingredient/category type, a gray-market/secondhand marketplace, or a
    retailer/marketplace store. The 'category winner' panel probes 'what is
    {name} known for', which is nonsense for a store (Coupang/Bunjang) or a
    type name ('wireless earbuds').

    `profile` selects the vertical's type/retailer vocab (defaults to beauty =
    byte-identical); electronics drops 'wireless earbuds' as a type and
    'Best Buy'/'Newegg' as retailers."""
    cleaned = str(name or "").strip()
    if not cleaned or is_ingredient_or_category_type(
        cleaned,
        ingredient_tokens=profile.competitor_ingredient_tokens,
        form_tokens=profile.competitor_form_tokens,
    ):
        return False
    tokens = set(re.findall(r"[a-z0-9]+", cleaned.lower()))
    from services.retailer_evidence import _GRAY_MARKET_HOST_TOKENS

    if tokens & _GRAY_MARKET_HOST_TOKENS or tokens & profile.retailer_tokens:
        return False
    # classify_host catches registry-known retailers/marketplaces (Olive Young,
    # Amazon) even when passed as a display name.
    for form in {cleaned, cleaned.lower().replace(" ", ""),
                 cleaned.lower().replace(" ", "") + ".com"}:
        if classify_host(form).get("type") in {"retailer", "marketplace"}:
            return False
    return True


def _durable_competitor_for_brief(
    opportunity: Mapping[str, Any],
    *,
    profile: VerticalProfile = BEAUTY_PROFILE,
) -> Optional[str]:
    counts: Counter = Counter()
    opportunity_map = opportunity if isinstance(opportunity, Mapping) else {}
    for row in _list_value(opportunity_map.get("per_prompt")):
        if not isinstance(row, Mapping):
            continue
        competitors = row.get("competitors")
        if isinstance(competitors, str):
            competitor_values = [competitors]
        elif isinstance(competitors, list):
            competitor_values = competitors
        else:
            competitor_values = []
        for competitor in competitor_values:
            name = str(competitor or "").strip()
            # Only real competing brands.
            if name and _competitor_is_brandlike(name, profile=profile):
                counts[name] += 1
        # `repeated_owner` (the AI's most-repeated owner in this category answer)
        # is a real winner signal — but sku_opportunity falls back to the
        # most-cited SOURCE HOST when no competitor repeats, which put stores
        # (Coupang/Bunjang) here and made the panel probe "what is Coupang known
        # for" — nonsense (#1143). Fold it in ONLY when it's brand-like: keeps
        # the meaningful brand signal (breaks a competitor-count tie toward the
        # repeated owner) while never letting a retailer win.
        density = row.get("density")
        features = density.get("features") if isinstance(density, Mapping) else None
        owner = (
            str(features.get("repeated_owner") or "").strip()
            if isinstance(features, Mapping) else ""
        )
        if owner and _competitor_is_brandlike(owner, profile=profile):
            counts[owner] += 1
    if not counts:
        return None
    from services.sku_opportunity import _durable_competitor

    winner = _durable_competitor(counts)
    # Defense in depth: never return a store even if one out-counts the brands.
    if winner and not _competitor_is_brandlike(winner, profile=profile):
        return None
    return winner


def _competitor_attribute_query(competitor: str, category: str) -> str:
    category_phrase = category or "this product category"
    return (
        f"what attributes is {competitor} known for in {category_phrase}, "
        "including certifications, ingredients, format, use-case, or positioning"
    )


def _competitor_attribute_probe_context(
    *,
    competitor: str,
    category: str,
    query: str,
) -> Dict[str, Any]:
    return {
        "queries": [query],
        "product_title": competitor,
        "product_type": category,
        "merchant_brand": competitor,
        "merchant_pdp_url": "",
        "product": {
            "title": competitor,
            "vendor": competitor,
            "product_type": category,
        },
        "analysis_goal": (
            "Ground only competitor attribute PRESENCE. Do not infer what the "
            "competitor lacks."
        ),
    }


def _grounded_attribute_providers(coverage: Mapping[str, Any]) -> List[str]:
    providers: List[str] = []
    for provider in list((coverage or {}).get("providers") or []):
        provider_id = str(provider or "").strip().lower()
        if provider_id in _COMPETITOR_ATTRIBUTE_GROUNDED_PROVIDERS:
            providers.append(provider_id)
    return providers[:2]


_COMPETITOR_PROSE_KEYS = ("evidence_excerpt", "summary", "answer", "known_for")


def _salvage_competitor_prose(raw: Any) -> str:
    """Recover clean prose from a winner-profile LLM response when the upstream
    parse failed and left only the raw string.

    The failure that leaked into prod: the model wrapped its answer in a
    ```json { … "evidence_excerpt": "Rahua is …" } fence that was unterminated
    (or otherwise malformed), so `parsed` came back empty and the caller dumped
    the raw ```json envelope straight into merchant-facing copy. This salvages
    the prose field (fence-strip → JSON-parse → regex fallback for truncated
    envelopes). If it still can't extract prose, it returns "" so the section
    hides rather than leaking JSON — never surface a raw ```json string.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    looks_structured = "```" in text or bool(
        re.search(
            r'"(?:' + "|".join(_COMPETITOR_PROSE_KEYS)
            + r'|product_visible|competitors_listed)"\s*:',
            text,
        )
    )
    if not looks_structured:
        return text  # already clean prose — nothing to salvage
    # W3: the shared parser handles the well-formed-envelope case (bare / fenced
    # / brace-substring). If it parses, extract the prose field.
    from services.llm_io import parse_llm_object

    obj = parse_llm_object(text, label="competitor_prose")
    if isinstance(obj, Mapping):
        for key in _COMPETITOR_PROSE_KEYS:
            value = obj.get(key)
            if value:
                return str(value).strip()
        return ""  # valid JSON but no prose field — hide, don't leak
    # Parse failed (commonly a TRUNCATED/unterminated envelope, which no general
    # JSON parser can recover). Pull the prose field directly, tolerating a
    # missing closing quote/brace — the one thing this salvage adds over
    # parse_llm_object, and why it stays local.
    inner = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
    inner = re.sub(r"\s*```$", "", inner).strip()
    for key in _COMPETITOR_PROSE_KEYS:
        match = re.search(
            rf'"{key}"\s*:\s*"(.+?)(?:"\s*[,}}]|$)', inner, re.DOTALL
        )
        if match and match.group(1).strip():
            return match.group(1).strip().rstrip('"').strip()
    return ""


def _competitor_attribute_run_text(run: Mapping[str, Any]) -> str:
    parsed_parts: List[str] = []
    parsed = run.get("parsed")
    if isinstance(parsed, Mapping):
        for key in ("evidence_excerpt", "summary", "answer"):
            value = parsed.get(key)
            if value:
                parsed_parts.append(str(value))
    if parsed_parts:
        return "\n".join(parsed_parts)
    # `parsed` is empty — the upstream JSON parse failed. Salvage prose from the
    # raw string instead of dumping a raw ```json envelope into the report.
    return _salvage_competitor_prose(run.get("raw"))


def _normal_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _attribute_verbatim(text: str, aliases: Tuple[str, ...]) -> str:
    sentences = [
        segment.strip()
        for segment in re.split(r"(?<=[.!?])\s+|\n+", text or "")
        if segment.strip()
    ]
    for sentence in sentences:
        normalized = _normal_text(sentence)
        if any(_normal_text(alias) in normalized for alias in aliases):
            return sentence[:240]
    return (text or "").strip()[:240]


def _extract_competitor_attribute_evidence(
    run: Mapping[str, Any],
    *,
    provider: str,
) -> List[Dict[str, str]]:
    if not (_list_value(run.get("grounding_sources")) or _list_value(run.get("grounding_chunks"))):
        return []
    text = _competitor_attribute_run_text(run)
    normalized = _normal_text(text)
    if not normalized:
        return []
    out: List[Dict[str, str]] = []
    for canonical, aliases in _COMPETITOR_ATTRIBUTE_ALIASES:
        if not any(_normal_text(alias) in normalized for alias in aliases):
            continue
        out.append({
            "attribute": canonical,
            "provider": provider,
            "verbatim": _attribute_verbatim(text, aliases),
        })
    # Category-agnostic capture: the alias taxonomy above is collagen-specific,
    # so for other categories (hair, skincare, …) it matches nothing. Always
    # keep the grounded "what {competitor} is known for" answer verbatim so the
    # competitor diagnosis works for ANY category, not just supplements.
    out.append({
        "attribute": _COMPETITOR_KNOWN_FOR_KEY,
        "provider": provider,
        "verbatim": " ".join(text.split())[:280],
    })
    return out


def _merge_competitor_attribute_evidence(
    *,
    competitor: str,
    evidence_rows: List[Dict[str, str]],
) -> Any:
    by_attribute: Dict[str, Dict[str, str]] = {}
    known_for: Optional[str] = None
    for row in evidence_rows:
        attribute = str(row.get("attribute") or "").strip()
        provider = str(row.get("provider") or "").strip()
        verbatim = str(row.get("verbatim") or "").strip()
        if not attribute or not provider or not verbatim:
            continue
        if attribute == _COMPETITOR_KNOWN_FOR_KEY:
            # First grounded "known for" answer wins (category-agnostic summary).
            if not known_for:
                known_for = verbatim[:280]
            continue
        by_attribute.setdefault(attribute, {
            "attribute": attribute,
            "provider": provider,
            "verbatim": verbatim[:240],
        })
    if not by_attribute and not known_for:
        return "not_assessed"
    attributes = list(by_attribute)
    return {
        "status": "assessed",
        "competitor": competitor,
        # Typed attributes the AI named (alias taxonomy) — used by the brief's
        # grounded competitor-attribute words.
        "attributes_present": attributes[:8],
        # Verbatim, category-agnostic "what the AI says this competitor is known
        # for" — the surfaceable answer to "what did the winner do right".
        "known_for": known_for,
        "evidence": [by_attribute[attr] for attr in attributes[:8]],
        "note": "Grounded presence only - not a claim the competitor lacks anything else.",
    }


async def _probe_durable_competitor_attributes_for_brief(
    *,
    opportunity: Mapping[str, Any],
    product: Mapping[str, Any],
    merchant_id: str,
    run_id: str,
    coverage: Mapping[str, Any],
    provider_model_metadata: Mapping[str, Any],
) -> Any:
    if not _strategic_brief_live_probe_enabled():
        return "not_assessed"
    competitor = _durable_competitor_for_brief(
        opportunity, profile=_profile_for_sku_ctx(None, product)
    )
    if not competitor:
        return "not_assessed"
    category = str(
        product.get("product_type")
        or product.get("category")
        or ""
    ).strip()
    providers = _grounded_attribute_providers(coverage)
    if not providers:
        return "not_assessed"
    query = _competitor_attribute_query(competitor, category)
    context = _competitor_attribute_probe_context(
        competitor=competitor,
        category=category,
        query=query,
    )
    evidence_rows: List[Dict[str, str]] = []
    safe_competitor = re.sub(r"[^a-z0-9]+", "_", competitor.lower()).strip("_") or "competitor"
    for provider in providers:
        model_info = provider_model_metadata.get(provider) or {}
        try:
            result = await llm_client.probe(
                scan_mode=_PER_SKU_AUDIT_PROBE_SCAN_MODE,
                scan_target_id=(
                    f"{run_id or 'adhoc'}:competitor_attrs:{safe_competitor}:{provider}"
                ),
                merchant_id=str(merchant_id),
                store_id=f"{merchant_id}_audit",
                context=context,
                provider=provider,
                max_runs=1,
                model=model_info.get("model"),
                model_is_override=bool(model_info.get("model_is_override")),
            )
        except Exception:  # noqa: BLE001 - optional grounding must fail closed
            logger.info(
                "competitor attribute probe failed provider=%s competitor=%s",
                provider,
                competitor,
                exc_info=True,
            )
            continue
        actual_provider = str(result.get("provider") or provider).strip().lower()
        if not _classify_provider(actual_provider).get("is_real"):
            continue
        for run in _list_value(result.get("raw_runs")):
            if not isinstance(run, Mapping):
                continue
            evidence_rows.extend(
                _extract_competitor_attribute_evidence(
                    run,
                    provider=actual_provider,
                )
            )
    return _merge_competitor_attribute_evidence(
        competitor=competitor,
        evidence_rows=evidence_rows,
    )


def _sku_intelligence_ladder_layer(row: Mapping[str, Any]) -> Optional[str]:
    axis = str(row.get("axis") or "").lower()
    query_class = str(row.get("query_class") or "").lower()
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


def _who_owns_is_first_party(value: Any, merchant_host: Optional[str]) -> bool:
    """True when a who_owns value resolves to the merchant's own host. A
    competitor NAME ("Vital Proteins") never resolves to a host, so it is kept;
    only the merchant's own domain is dropped."""
    if not merchant_host:
        return False
    host = normalize_host(value) if isinstance(value, str) else None
    if not host:
        return False
    return host == merchant_host or host.endswith("." + merchant_host)


def _who_owns_prompt(row: Mapping[str, Any], merchant_host: Optional[str] = None) -> Optional[Any]:
    who = row.get("who_owns")
    if who and not _who_owns_is_first_party(who, merchant_host):
        return who
    controllers = stable_buyer_path_controller_hosts(row, exclude_hosts=merchant_host)
    if controllers:
        return controllers[0] if len(controllers) == 1 else controllers
    return None


def _prompt_sources(row: Mapping[str, Any], merchant_host: Optional[str] = None) -> List[Any]:
    return stable_buyer_path_controllers_for_row(row, exclude_hosts=merchant_host)[:3]


def _sku_intelligence_buyer_path_action(
    row: Mapping[str, Any], merchant_host: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not is_third_party_controlled_lane(row):
        return None
    if not has_lane_demand(row):
        return None
    query = str(row.get("query") or "").strip()
    sources = _prompt_sources(row, merchant_host)
    hosts = [
        str(source.get("host") or "").strip()
        for source in sources
        if isinstance(source, dict) and str(source.get("host") or "").strip()
    ]
    controllers = hosts[:3]
    controller_phrase = (
        ", ".join(controllers)
        if controllers
        else "fragmented sources with no single cited site"
    )
    lane = query or "this exposed lane"
    profile = build_controller_profile(
        {
            "host": str(source.get("host") or "").strip(),
            "role": row.get("source_route") or row.get("ownership_state"),
            "times_cited": source.get("times_cited"),
        }
        for source in sources
        if isinstance(source, dict) and str(source.get("host") or "").strip()
    )
    if is_canonical_source_vacuum(profile):
        move = (
            f"Read {controller_phrase} as a weak citation trail for {lane}, not proven "
            "lost buyer traffic. Make the official page citable first: exact SKU facts, "
            "structured product data, proof, stock, and authorized where-to-buy; then "
            "audit the reseller/source trail."
        )
        moves = [
            {
                "type": "canonical_source_authority",
                "operator_action": (
                    f"Make the official page the source AI can cite for {lane}: exact SKU "
                    "facts, structured product data, proof, stock, returns, and authorized where-to-buy."
                ),
            },
            {
                "type": "authorized_distribution_or_reseller_cleanup",
                "operator_action": (
                    "Audit the weak third-party trail for wrong titles, images, variants, "
                    "stock, authorization, and stale SKU facts; decide which real authorized "
                    "retail routes deserve attention and whether the citations are material."
                ),
            },
            {
                "type": "direct_buy_reason",
                "operator_action": (
                    "After the official page is source-ready, add first-order offer, starter + "
                    "replenishment bundle, subscription incentive, and why-buy-direct proof."
                ),
            },
        ]
    elif str(profile.get("strategy") or "") == "source_authority_gap":
        move = (
            f"Use the cited + buyable official page for {lane} as the source AI can cite "
            f"before pitching {controller_phrase}: official proof, availability, images, "
            "and source-consistent facts."
        )
        moves = [
            {
                "type": "canonical_source_authority",
                "operator_action": f"Make the official page the cited + buyable source for {lane}.",
            },
            {
                "type": "evidenced_source_outreach",
                "operator_action": f"Pitch {controller_phrase} with official SKU facts, proof assets, availability, and images.",
            },
            {
                "type": "direct_buy_reason",
                "operator_action": (
                    "Add first-order offer, starter + replenishment bundle, subscription "
                    "incentive, and why-buy-direct proof."
                ),
            },
        ]
    else:
        move = (
            f"Use the cited + buyable official page for {lane} to win the direct "
            f"buyer path against {controller_phrase}: first-order offer, starter + "
            "replenishment bundle, subscription incentive, and why-buy-direct proof."
        )
        moves = [
            {
                "type": "first_order_offer",
                "operator_action": f"Attach a first-order offer to the official page for {lane}.",
            },
            {
                "type": "starter_replenishment_bundle",
                "operator_action": f"Add a starter + replenishment bundle on the official page for {lane}.",
            },
            {
                "type": "subscription_or_why_buy_direct",
                "operator_action": (
                    "Add subscription incentive and why-buy-direct proof: guarantee, "
                    "samples, loyalty, returns, stock, and fresh product facts."
                ),
            },
        ]
    return {
        "prescription_class": "operational_efficiency",
        "lane": query,
        "controllers": controllers,
        "controller_strategy": profile.get("strategy"),
        "controller_strategy_label": profile.get("label"),
        "controller_profile": profile,
        "exposure_confidence": profile.get("exposure_confidence"),
        "exposure_read": profile.get("exposure_read"),
        "move": move,
        "canonical_page_play": {
            "lane": lane,
            "controllers": controllers,
            "controller_strategy": profile.get("strategy"),
            "controller_strategy_label": profile.get("label"),
            "controller_profile": profile,
            "exposure_confidence": profile.get("exposure_confidence"),
            "exposure_read": profile.get("exposure_read"),
            "page": "the official page",
            "economics_policy": (
                "Mechanics only: first-order offer, starter + replenishment bundle, "
                "subscription incentive, and why-buy-direct proof. Do not recommend "
                "exact discount depths, bundle prices, savings percentages, or margin "
                "claims without audited margin or promo evidence."
            ),
            "moves": moves,
            "checkout_readiness": (
                "Make the page cited, buyable, and agent-checkout ready after it is "
                "source-ready for this lane."
            ),
        },
    }


def _lane_priority_output_fields(row: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in (
        "lane_priority_score",
        "merchant_fit_score",
        "conversion_fit_score",
        "merchant_fit_reasons",
        "fit_penalties",
        "selection_reason",
    ):
        if key in row:
            out[key] = row.get(key)
    return out


def _demand_label(signal: Any) -> Optional[str]:
    """Coarse merchant-facing demand strength from the per-lane demand_signal
    (0-1). None when there's no signal, so the UI shows nothing rather than a
    fabricated level."""
    try:
        value = float(signal)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value >= 0.7:
        return "high"
    if value >= 0.4:
        return "moderate"
    return "low"


def _trim_sku_intelligence_prompt(
    row: Mapping[str, Any], merchant_host: Optional[str] = None,
) -> Dict[str, Any]:
    verdicts = row.get("provider_verdicts") if isinstance(row.get("provider_verdicts"), dict) else {}
    out = {
        "query": row.get("query"),
        "intent_ladder_layer": _sku_intelligence_ladder_layer(row),
        "gemini": verdicts.get("gemini", "absent"),
        "deepseek": verdicts.get("deepseek", "absent"),
        # Present only when ChatGPT ran (OPENAI_API_KEY live); mock/no-key runs
        # are filtered upstream so this stays "absent" until the key is set.
        "chatgpt": verdicts.get("chatgpt", "absent"),
        "ownership_state": row.get("ownership_state"),
        "who_owns": _who_owns_prompt(row, merchant_host),
        "sources": _prompt_sources(row, merchant_host),
        # Verbatim AI answer excerpt + who it named, so the merchant sees what
        # the AI actually said on the lanes it loses (not just the hostnames).
        "cited_evidence": row.get("cited_evidence"),
        "source_route": row.get("source_route"),
        "demand_signal": row.get("demand_signal"),
        # Coarse, merchant-facing demand strength so "AI shows demand" is
        # qualified per lane (the raw demand_signal stays for tooling).
        "demand_label": _demand_label(row.get("demand_signal")),
        "attribute_basis": row.get("attribute_basis"),
        "opportunity_score": row.get("opportunity_score"),
        **_lane_priority_output_fields(row),
    }
    buyer_path_action = _sku_intelligence_buyer_path_action(row, merchant_host)
    if buyer_path_action:
        out["buyer_path_action"] = buyer_path_action
    return out


_BUYER_PATH_THIRD_PARTY_OWNERSHIP = {
    "competitor-owned",
    "forum-owned",
    "marketplace-owned",
    "publisher-owned",
    "retailer-owned",
}
_BUYER_PATH_THIRD_PARTY_ROUTES = {
    "brand",
    "forum",
    "marketplace",
    "publisher",
    "retailer",
}


def _buyer_path_clean_str(value: Any) -> str:
    return str(value or "").strip()


def _buyer_path_unique(values: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        cleaned = _buyer_path_clean_str(value).lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _buyer_path_hosts_from_any(value: Any) -> List[str]:
    hosts: List[str] = []
    if isinstance(value, str):
        normalized = normalize_host(value) or value.strip().lower()
        if "." in normalized:
            hosts.append(normalized)
    elif isinstance(value, Mapping):
        if value.get("controllers"):
            hosts.extend(_buyer_path_hosts_from_any(value.get("controllers")))
        evidence = value.get("evidence")
        if isinstance(evidence, Mapping) and evidence.get("controllers"):
            hosts.extend(_buyer_path_hosts_from_any(evidence.get("controllers")))
        raw = value.get("host") or value.get("domain") or value.get("url")
        if raw:
            normalized = normalize_host(str(raw)) or str(raw).strip().lower()
            if "." in normalized:
                hosts.append(normalized)
    elif isinstance(value, list):
        for item in value:
            hosts.extend(_buyer_path_hosts_from_any(item))
    return _buyer_path_unique(hosts)


def _buyer_path_row_controllers(row: Mapping[str, Any]) -> List[str]:
    hosts: List[str] = []
    action = row.get("buyer_path_action")
    if isinstance(action, Mapping):
        hosts.extend(_buyer_path_hosts_from_any(action.get("controllers")))
    hosts.extend(_buyer_path_hosts_from_any(row.get("sources")))
    hosts.extend(_buyer_path_hosts_from_any(row.get("who_owns")))
    return _buyer_path_unique(hosts)[:3]


def _buyer_path_rows(sku_intelligence: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    matrix = sku_intelligence.get("prompt_matrix")
    if not isinstance(matrix, list):
        return []
    return [row for row in matrix if isinstance(row, Mapping)]


def _buyer_path_is_merchant_owned(row: Mapping[str, Any]) -> bool:
    return _buyer_path_clean_str(row.get("ownership_state")).lower() == "merchant-owned"


def _buyer_path_is_third_party(row: Mapping[str, Any]) -> bool:
    ownership = _buyer_path_clean_str(row.get("ownership_state")).lower()
    route = _buyer_path_clean_str(row.get("source_route")).lower()
    if ownership == "merchant-owned":
        return False
    return (
        ownership in _BUYER_PATH_THIRD_PARTY_OWNERSHIP
        or route in _BUYER_PATH_THIRD_PARTY_ROUTES
        or bool(_buyer_path_row_controllers(row))
    )


def _buyer_path_prompt_count(rows: List[Mapping[str, Any]]) -> int:
    count = 0
    for row in rows:
        ownership = _buyer_path_clean_str(row.get("ownership_state")).lower()
        if ownership and ownership != "no-demand":
            count += 1
    return count


def _buyer_path_selected_action_query(next_best_action: Mapping[str, Any]) -> str:
    evidence = next_best_action.get("evidence_used")
    if not isinstance(evidence, Mapping):
        return ""
    prompt = evidence.get("source_route_prompt")
    if not isinstance(prompt, Mapping):
        return ""
    return _buyer_path_clean_str(prompt.get("query")).lower()


def _buyer_path_primary_row(
    rows: List[Mapping[str, Any]],
    *,
    next_best_action: Optional[Mapping[str, Any]] = None,
) -> Optional[Mapping[str, Any]]:
    candidates = [
        row for row in rows
        if _buyer_path_is_third_party(row) and _buyer_path_clean_str(row.get("query"))
    ]
    if not candidates:
        return None
    selected_query = (
        _buyer_path_selected_action_query(next_best_action)
        if isinstance(next_best_action, Mapping) else ""
    )
    if selected_query:
        selected = next(
            (
                row for row in candidates
                if _buyer_path_clean_str(row.get("query")).lower() == selected_query
            ),
            None,
        )
        if selected:
            return selected
    candidates.sort(
        key=lambda row: (
            lane_priority_sort_key(row),
            -float(row.get("opportunity_score") or 0),
            _buyer_path_clean_str(row.get("query")).lower(),
        )
    )
    return candidates[0]


def _buyer_path_fallback_controllers(next_best_action: Mapping[str, Any]) -> List[str]:
    hosts: List[str] = []
    evidence = next_best_action.get("evidence_used")
    if isinstance(evidence, Mapping):
        prompt = evidence.get("source_route_prompt")
        if isinstance(prompt, Mapping):
            hosts.extend(_buyer_path_hosts_from_any(prompt.get("sources")))
    hosts.extend(_buyer_path_hosts_from_any(next_best_action.get("operator_moves")))
    return _buyer_path_unique(hosts)[:3]


def _buyer_path_controller_phrase(controllers: List[str]) -> str:
    cleaned = _buyer_path_unique(controllers)[:3]
    if not cleaned:
        return "the cited third-party sources"
    if len(cleaned) == 1:
        return cleaned[0]
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


def summarize_sku_buyer_path(sku_intelligence: Mapping[str, Any]) -> Dict[str, Any]:
    """Summarize who controls evidenced SKU-level buyer paths.

    This deliberately does not change the raw AI visibility / attribution
    verdict. It creates the missing merchant-facing dimension: whether the
    demand lanes are controlled by the merchant or by the cited route.
    """
    if not isinstance(sku_intelligence, Mapping):
        sku_intelligence = {}
    rows = _buyer_path_rows(sku_intelligence)
    prompt_count = _buyer_path_prompt_count(rows)
    if prompt_count <= 0:
        return {
            "state": "not_measured",
            "label_display": "Buyer path not measured",
            "explanation": "SKU-level buyer-path ownership was not measured for this report.",
            "prompt_count": 0,
            "merchant_owned_count": 0,
            "third_party_controlled_count": 0,
            "primary_lane": None,
            "top_controllers": [],
        }

    merchant_owned_count = sum(1 for row in rows if _buyer_path_is_merchant_owned(row))
    third_party_rows = [row for row in rows if _buyer_path_is_third_party(row)]
    third_party_controlled_count = len(third_party_rows)
    next_best_action = sku_intelligence.get("next_best_action")
    primary_row = _buyer_path_primary_row(
        rows,
        next_best_action=next_best_action if isinstance(next_best_action, Mapping) else None,
    )
    primary_lane = (
        _buyer_path_clean_str(primary_row.get("query")) if primary_row else None
    )
    controllers: List[str] = []
    if primary_row:
        controllers.extend(_buyer_path_row_controllers(primary_row))
    for row in third_party_rows:
        if primary_row is row:
            continue
        controllers.extend(_buyer_path_row_controllers(row))
    if not controllers and isinstance(next_best_action, Mapping):
        controllers.extend(_buyer_path_fallback_controllers(next_best_action))
    top_controllers = _buyer_path_unique(controllers)[:3]
    controller_phrase = _buyer_path_controller_phrase(top_controllers)

    if merchant_owned_count > prompt_count / 2:
        state = "merchant_controlled"
        label = "Merchant-owned buyer path"
        explanation = (
            f"{merchant_owned_count}/{prompt_count} evidenced prompt lanes are "
            "merchant-owned, so the owned path is the dominant route."
        )
    elif merchant_owned_count > 0 and third_party_controlled_count > 0:
        state = "mixed"
        label = "Mixed owned buyer path"
        lane_clause = (
            f"; the first exposed third-party lane is \"{primary_lane}\", "
            f"controlled by {controller_phrase}"
            if primary_lane else f"; third-party controllers include {controller_phrase}"
        )
        explanation = (
            f"{merchant_owned_count}/{prompt_count} evidenced prompt lanes are "
            f"merchant-owned, but {third_party_controlled_count} still route through "
            f"third-party sources{lane_clause}."
        )
    elif third_party_controlled_count > 0 and top_controllers:
        state = "third_party_controlled"
        label = "Weak owned buyer path"
        lane_clause = (
            f"; the first exposed lane is \"{primary_lane}\", controlled by "
            f"{controller_phrase}"
            if primary_lane else f"; controllers include {controller_phrase}"
        )
        explanation = (
            f"{merchant_owned_count}/{prompt_count} evidenced prompt lanes are "
            f"merchant-owned{lane_clause}."
        )
    elif third_party_controlled_count > 0:
        state = "fragmented_source_trail"
        label = "Fragmented buyer path"
        lane_clause = (
            f"; the first exposed lane is \"{primary_lane}\""
            if primary_lane else ""
        )
        explanation = (
            f"{third_party_controlled_count}/{prompt_count} evidenced prompt lanes "
            "show third-party exposure, but the cited hosts are fragmented with no "
            f"single site owning the buyer path{lane_clause}."
        )
    else:
        state = "not_measured"
        label = "Buyer path not measured"
        explanation = (
            "SKU-level prompts were present, but no controlled buyer path could be "
            "classified from the evidence."
        )

    return {
        "state": state,
        "label_display": label,
        "explanation": explanation,
        "prompt_count": prompt_count,
        "merchant_owned_count": merchant_owned_count,
        "third_party_controlled_count": third_party_controlled_count,
        "primary_lane": primary_lane,
        "top_controllers": top_controllers,
    }


def _ai_visibility_display(label: str, label_display: str) -> str:
    raw = _buyer_path_clean_str(label).upper()
    if raw == VERDICT_STRONG:
        return "Strong AI visibility"
    if raw == VERDICT_VIA_RETAILERS:
        return "Visible through third parties"
    if raw == VERDICT_MISATTRIBUTED:
        return "Visible but misattributed"
    if raw == VERDICT_PARTIAL:
        return "Partial AI visibility"
    if raw == VERDICT_INVISIBLE:
        return "Low AI visibility"
    return label_display or label or "AI visibility"


def _combined_buyer_path_label(raw_label: str, raw_display: str, buyer_path: Mapping[str, Any]) -> Optional[str]:
    state = _buyer_path_clean_str(buyer_path.get("state"))
    ai_label = _ai_visibility_display(raw_label, raw_display)
    if state == "third_party_controlled":
        return f"{ai_label}, weak owned buyer path"
    if state == "mixed":
        return f"{ai_label}, mixed owned buyer path"
    if state == "fragmented_source_trail":
        return f"{ai_label}, fragmented buyer path"
    return None


def _combined_buyer_path_explanation(raw_label: str, buyer_path: Mapping[str, Any]) -> Optional[str]:
    state = _buyer_path_clean_str(buyer_path.get("state"))
    if state not in {"third_party_controlled", "mixed", "fragmented_source_trail"}:
        return None
    prompt_count = int(buyer_path.get("prompt_count") or 0)
    merchant_owned_count = int(buyer_path.get("merchant_owned_count") or 0)
    third_party_count = int(buyer_path.get("third_party_controlled_count") or 0)
    lane = _buyer_path_clean_str(buyer_path.get("primary_lane"))
    controllers = _buyer_path_hosts_from_any(buyer_path.get("top_controllers"))
    controller_phrase = _buyer_path_controller_phrase(controllers)
    visibility_clause = (
        "AI answer visibility is strong"
        if _buyer_path_clean_str(raw_label).upper() == VERDICT_STRONG
        else "AI answer visibility exists"
    )
    if state == "mixed":
        lane_clause = (
            f"; the first exposed third-party lane is \"{lane}\", controlled by "
            f"{controller_phrase}"
            if lane else f"; third-party controllers include {controller_phrase}"
        )
        return (
            f"{visibility_clause}, but only {merchant_owned_count}/{prompt_count} "
            f"evidenced prompt lanes are merchant-owned; {third_party_count} still "
            f"route through third-party sources{lane_clause}. Read this as a buyer-path "
            "repair, not a finished owned-channel win."
        )
    if state == "fragmented_source_trail":
        lane_clause = f"; the first exposed lane is \"{lane}\"" if lane else ""
        return (
            f"{visibility_clause}, and {third_party_count}/{prompt_count} evidenced "
            "prompt lanes show third-party exposure, but the cited hosts are fragmented "
            f"with no single site owning the buyer path{lane_clause}. Read this as an "
            "official-source opening before naming a conversion opponent."
        )
    lane_clause = (
        f"; the first exposed lane is \"{lane}\", controlled by {controller_phrase}"
        if lane else f"; controllers include {controller_phrase}"
    )
    return (
        f"{visibility_clause}, but {merchant_owned_count}/{prompt_count} evidenced "
        f"prompt lanes are merchant-owned{lane_clause}. Read the existing exposure "
        "as demand to redirect, not as a finished owned-channel win."
    )


def _enrich_verdict_with_buyer_path(verdict: Dict[str, Any], buyer_path: Mapping[str, Any]) -> None:
    raw_label = _buyer_path_clean_str(verdict.get("label"))
    raw_display = _buyer_path_clean_str(verdict.get("label_display")) or _verdict_display_label(raw_label)
    raw_explanation = _buyer_path_clean_str(verdict.get("explanation"))
    verdict.setdefault("ai_attribution_label", raw_label)
    verdict.setdefault("ai_attribution_label_display", raw_display)
    verdict.setdefault("ai_attribution_explanation", raw_explanation)
    verdict["buyer_path_verdict"] = dict(buyer_path)
    combined_label = _combined_buyer_path_label(raw_label, raw_display, buyer_path)
    combined_explanation = _combined_buyer_path_explanation(raw_label, buyer_path)
    if combined_label and combined_explanation:
        verdict["label_display"] = combined_label
        verdict["explanation"] = combined_explanation


def apply_buyer_path_verdict_to_brand_report(
    brand_report: Dict[str, Any],
    sku_intelligence: Mapping[str, Any],
) -> Dict[str, Any]:
    """Attach owned-buyer-path verdicts without changing raw verdict enums."""
    if not isinstance(brand_report, dict):
        return brand_report
    buyer_path = summarize_sku_buyer_path(sku_intelligence)
    per_product = [
        product for product in (brand_report.get("per_product") or [])
        if isinstance(product, dict)
    ]
    if per_product and isinstance(per_product[0].get("verdict"), dict):
        _enrich_verdict_with_buyer_path(per_product[0]["verdict"], buyer_path)
        combined_display = per_product[0]["verdict"].get("label_display")
        combined_explanation = per_product[0]["verdict"].get("explanation")
        executive_summary = per_product[0].get("executive_summary")
        if (
            isinstance(executive_summary, dict)
            and buyer_path.get("state") in {"third_party_controlled", "mixed", "fragmented_source_trail"}
        ):
            executive_summary["verdict_pill_text"] = (
                combined_display
                or executive_summary.get("verdict_pill_text")
            )
        merchant_view = per_product[0].get("merchant_view")
        headline = (
            merchant_view.get("headline")
            if isinstance(merchant_view, dict) else None
        )
        if (
            isinstance(headline, dict)
            and buyer_path.get("state") in {"third_party_controlled", "mixed", "fragmented_source_trail"}
        ):
            headline["verdict_label_display"] = combined_display
            headline["one_liner"] = combined_explanation
            headline["plain_summary"] = combined_explanation

    aggregate = brand_report.get("aggregate")
    if isinstance(aggregate, dict):
        raw_label = _buyer_path_clean_str(aggregate.get("brand_verdict_label"))
        raw_explanation = _buyer_path_clean_str(aggregate.get("brand_verdict_explanation"))
        raw_display = _verdict_display_label(raw_label) if raw_label else raw_label
        aggregate.setdefault("ai_attribution_label", raw_label)
        aggregate.setdefault("ai_attribution_explanation", raw_explanation)
        aggregate["buyer_path_verdict"] = dict(buyer_path)
        combined_label = _combined_buyer_path_label(raw_label, raw_display, buyer_path)
        combined_explanation = _combined_buyer_path_explanation(raw_label, buyer_path)
        if combined_label and combined_explanation:
            aggregate["brand_verdict_label_display"] = combined_label
            aggregate["brand_verdict_explanation"] = combined_explanation
        elif raw_label and not aggregate.get("brand_verdict_label_display"):
            aggregate["brand_verdict_label_display"] = raw_display
    return brand_report


def _lost_head_category_for_money_shot(
    per_prompt: List[Dict[str, Any]],
    product_type: str,
) -> str:
    candidates = [
        row for row in per_prompt
        if row.get("axis") == "category"
        and row.get("ownership_state") != "merchant-owned"
    ]
    if not candidates:
        return f"the broad {product_type or 'product'} category"
    candidates.sort(
        key=lambda row: (
            float(row.get("opportunity_score") or 0),
            str(row.get("query") or "").lower(),
        )
    )
    return str(candidates[0].get("query") or "").strip() or f"the broad {product_type or 'product'} category"


def _headline_join(hosts: List[str]) -> str:
    hosts = [h for h in hosts if h][:3]
    if not hosts:
        return ""
    if len(hosts) == 1:
        return hosts[0]
    if len(hosts) == 2:
        return f"{hosts[0]} and {hosts[1]}"
    return f"{hosts[0]}, {hosts[1]} and {hosts[2]}"


def _top_exposed_lane(
    per_prompt: List[Mapping[str, Any]],
    *,
    merchant_host: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The highest-value lane where demand exists but a third-party source/retailer
    controls the buyer path (the de-inflated EXPOSURE — not an open lane).

    The lane (query) comes from the top-priority row, but the named controllers
    are aggregated across every third-party-controlled demand lane so the
    merchant-facing headline names the SKU's stable controllers instead of one
    noisy hero prompt's run-to-run cited sources."""
    rows = [
        row for row in per_prompt
        if is_third_party_controlled_lane(row)
        and has_lane_demand(row)
        and str(row.get("query") or "").strip()
    ]
    if not rows:
        return None
    rows.sort(
        key=lambda row: (
            lane_priority_sort_key(row),
            -float(row.get("demand_signal") or 0),
            str(row.get("query") or "").lower(),
        )
    )
    row = rows[0]
    groups = [
        chips
        for candidate in rows
        for chips in (stable_buyer_path_controllers_for_row(candidate),)
        if chips
    ]
    profile = aggregate_controller_profile(groups, exclude_hosts=[merchant_host] if merchant_host else None)
    controllers = [host for host in profile.get("controllers") or [] if host]
    if not controllers:
        controllers = stable_buyer_path_controller_hosts(row)
    return {"lane": str(row.get("query") or "").strip(), "controllers": controllers}


def _sku_intelligence_headline(
    *,
    opportunity: Mapping[str, Any],
    title: str,
    product_type: str,
    merchant_host: Optional[str] = None,
) -> str:
    top_open_lanes = opportunity.get("top_open_lanes") or []
    if not top_open_lanes:
        # Lead with the buyer-path EXPOSURE if the demand is
        # real but controlled by third-party sources/retailers (the de-inflated
        # story). "No open lane" is the wrong frame when the lanes are owned.
        exposed = _top_exposed_lane(
            list(opportunity.get("per_prompt") or []),
            merchant_host=merchant_host,
        )
        if exposed and exposed["lane"] and exposed["controllers"]:
            controllers = _headline_join(exposed["controllers"])
            return (
                f"AI shows demand for `{exposed['lane']}`; across tested buyer paths "
                f"for this SKU, cited routes point to {controllers} - not your site. "
                "Here's how to win the buyer path back."
            )
        if exposed and exposed["lane"]:
            return (
                f"AI recommends `{exposed['lane']}`, but the source trail is "
                "fragmented with no single site owning the lane yet. Make your "
                "official page the cited and buyable source first."
            )
        prompt_count = len(opportunity.get("per_prompt") or [])
        return (
            f"We tested {prompt_count} buyer prompts for {title}. The next move is "
            "product-evidence depth: keep the canonical page complete, buyable, "
            "and ready for product-specific demand."
        )
    top_open_lane = str((top_open_lanes[0] or {}).get("query") or "").strip()
    if not top_open_lane:
        prompt_count = len(opportunity.get("per_prompt") or [])
        return (
            f"We tested {prompt_count} buyer prompts for {title}. The next move is "
            "product-evidence depth: keep the canonical page complete, buyable, "
            "and ready for product-specific demand."
        )
    lost_head_category = _lost_head_category_for_money_shot(
        list(opportunity.get("per_prompt") or []),
        product_type,
    )
    if lost_head_category.startswith("the broad "):
        return (
            f"Nobody owns `{top_open_lane}` yet, and your product is exactly "
            "that — own it."
        )
    return (
        f"You lost `{lost_head_category}`, but nobody owns `{top_open_lane}` "
        "and your product is exactly that. Build this page and source trail now."
    )


def _display_sku_intelligence(
    *,
    sku_ctx: Dict[str, Any],
    opportunity: Mapping[str, Any],
) -> Dict[str, Any]:
    product = _get_product(sku_ctx or {})
    title = str(product.get("title") or sku_ctx.get("sku_key") or "this product")
    product_type = str(product.get("product_type") or product.get("category") or "product")
    merchant_host = normalize_host(product.get("canonical_url") or product.get("pdp_url"))
    per_prompt = [
        row for row in (opportunity.get("per_prompt") or [])
        if isinstance(row, dict)
    ]
    product_evidence = (
        opportunity.get("product_evidence")
        if isinstance(opportunity.get("product_evidence"), Mapping)
        else {}
    )
    prioritized_per_prompt = [
        enrich_lane_priority(row, product_evidence=product_evidence)
        if is_third_party_controlled_lane(row) and has_lane_demand(row)
        else dict(row)
        for row in per_prompt
    ]
    sidewalk_open_queries = {
        row.get("query")
        for row in prioritized_per_prompt
        if row.get("open_lane")
        and _sku_intelligence_ladder_layer(row) == "sidewalk_opportunity"
    }
    open_lanes = [
        lane for lane in (opportunity.get("top_open_lanes") or [])
        if isinstance(lane, dict)
        and lane.get("query") in sidewalk_open_queries
    ]
    lanes = [dict(lane) for lane in open_lanes[:3]]
    matrix_rows = [
        _trim_sku_intelligence_prompt(row, merchant_host)
        for row in prioritized_per_prompt
    ]
    matrix_rows.sort(
        key=lambda row: (
            0 if row.get("lane_priority_score") is not None else 1,
            -float(row.get("lane_priority_score") or 0),
            -float(row.get("merchant_fit_score") or 0),
            -float(row.get("conversion_fit_score") or 0),
            -float(row.get("opportunity_score") or 0),
            str(row.get("query") or "").lower(),
        )
    )
    has_exposure = any(
        is_third_party_controlled_lane(row)
        and has_lane_demand(row)
        and str(row.get("query") or "").strip()
        for row in prioritized_per_prompt
    )
    is_empty = len(lanes) == 0 and not has_exposure
    sideways_wedge = build_sideways_wedge(
        prioritized_per_prompt,
        product_evidence=product_evidence,
    )
    display_opportunity = dict(opportunity)
    display_opportunity["per_prompt"] = prioritized_per_prompt
    display_opportunity["top_open_lanes"] = lanes
    display_opportunity["sideways_wedge"] = sideways_wedge
    next_best_action = build_sku_next_best_action(
        opportunity=display_opportunity,
        identity={
            "name": title,
            "confidence": "medium" if title and title != "this product" else "low",
            "unresolved": not bool(title and title != "this product"),
        },
        sku_title=title,
        merchant_host=merchant_host,
    )
    return {
        "hero_sku": {
            "title": title,
            "pdp_url": product.get("canonical_url") or product.get("pdp_url"),
            "vendor": product.get("vendor") or product.get("brand"),
        },
        "headline": _sku_intelligence_headline(
            opportunity=display_opportunity,
            title=title,
            product_type=product_type,
            merchant_host=merchant_host,
        ),
        "intent_ladder": opportunity.get("intent_ladder") or {},
        "top_open_lanes": lanes,
        "substitution_alert": opportunity.get("substitution_alert") or {"present": False},
        "prompt_matrix": matrix_rows,
        "sideways_wedge": sideways_wedge,
        "demand_state_summary": opportunity.get("demand_state_summary"),
        "coverage": opportunity.get("confidence") or {},
        "next_best_action": next_best_action,
        "is_empty": is_empty,
    }


def _empty_sku_intelligence(
    sku_ctx: Optional[Dict[str, Any]] = None,
    *,
    note: Optional[str] = None,
    quality_gate: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Honest empty-state SKU intelligence: hero context + a clear reason, never
    a fabricated lane. Used when the per-SKU upstream is mock/unavailable."""
    product = _get_product(sku_ctx or {})
    title = product.get("title")
    next_best_action = build_sku_next_best_action(
        opportunity={
            "per_prompt": [],
            "top_open_lanes": [],
            "substitution_alert": {"present": False},
            "confidence": {"prompt_count": 0, "prompts_with_demand": 0},
        },
        identity={
            "name": title or "this product",
            "confidence": "medium" if title else "low",
            "unresolved": not bool(title),
        },
        sku_title=title,
    )
    out: Dict[str, Any] = {
        "hero_sku": {
            "title": product.get("title"),
            "pdp_url": product.get("canonical_url") or product.get("pdp_url"),
            "vendor": product.get("vendor") or product.get("brand"),
        },
        "headline": note or "Build the product evidence foundation before chasing AI demand.",
        "intent_ladder": {},
        "top_open_lanes": [],
        "substitution_alert": {"present": False},
        "prompt_matrix": [],
        "demand_state_summary": None,
        "coverage": {},
        "next_best_action": next_best_action,
        "is_empty": True,
    }
    if note:
        out["note"] = note
    if quality_gate:
        out["quality_gate"] = dict(quality_gate)
    return out


async def run_wedge_hero_sku_intelligence(
    *,
    hero_product: Dict[str, Any],
    merchant_id: str,
    run_id: str,
    coverage_profile: str,
    prompts_per_sku: int = 14,
) -> Dict[str, Any]:
    """Run the hero URL-fetched wedge product through per-SKU opportunity."""
    if not isinstance(hero_product, dict) or not hero_product:
        next_best_action = build_sku_next_best_action(
            opportunity={
                "per_prompt": [],
                "top_open_lanes": [],
                "substitution_alert": {"present": False},
                "confidence": {"prompt_count": 0, "prompts_with_demand": 0},
            },
            identity={"name": "this product", "confidence": "low", "unresolved": True},
            sku_title="this product",
        )
        return {
            "hero_sku": {"title": None, "pdp_url": None, "vendor": None},
            "headline": "Build the product evidence foundation before chasing AI demand.",
            "intent_ladder": {},
            "top_open_lanes": [],
            "substitution_alert": {"present": False},
            "prompt_matrix": [],
            "demand_state_summary": None,
            "coverage": {},
            "next_best_action": next_best_action,
            "is_empty": True,
        }
    sku_ctx = _wedge_hero_sku_ctx(hero_product, merchant_id=str(merchant_id))
    attribute_graph = build_sku_attribute_graph(_get_product(sku_ctx))
    coverage = resolve_coverage_profile(coverage_profile=coverage_profile)
    profile_providers = list(coverage.get("providers") or [])
    provider_model_metadata = resolve_provider_models(profile_providers)
    probe_runs = await _probe_per_sku_ctx(
        sku_ctx=sku_ctx,
        merchant_id=str(merchant_id),
        coverage=coverage,
        provider_model_metadata=provider_model_metadata,
        prompts_per_sku=prompts_per_sku,
        audit_run_id=run_id,
    )
    # Honesty parity with the brand-report mock guard (_detect_mock_per_product):
    # the per-SKU probes are a SEPARATE upstream call, so a transient fallback
    # here can produce synthetic runs even when the brand report was real. Drop
    # mock-provider runs; if no real signal remains, return the honest
    # empty-state rather than fabricate a money-shot on synthetic data.
    real_runs = [
        run for run in probe_runs
        if _classify_provider(str((run or {}).get("provider") or "")).get("is_real")
    ]
    if probe_runs and not real_runs:
        return _empty_sku_intelligence(
            sku_ctx,
            note="SKU intelligence is gated until live AI evidence is available.",
            quality_gate={
                "shareable": False,
                "reason": "live_sku_probe_not_real",
                "merchant_copy_allowed": False,
            },
        )

    from services.sku_opportunity import build_sku_opportunity

    opportunity = build_sku_opportunity(
        sku_ctx,
        {sku_ctx["sku_key"]: real_runs},
        attribute_graph=attribute_graph,
        vertical_profile=_profile_for_sku_ctx(sku_ctx, _get_product(sku_ctx)),
    )
    display = _display_sku_intelligence(
        sku_ctx=sku_ctx,
        opportunity=opportunity,
    )
    product = _get_product(sku_ctx)
    title = str(product.get("title") or sku_ctx.get("sku_key") or "this product")
    brief_opportunity = dict(opportunity)
    brief_opportunity["top_open_lanes"] = list(display.get("top_open_lanes") or [])
    competitor_attributes = await _probe_durable_competitor_attributes_for_brief(
        opportunity=opportunity,
        product=product,
        merchant_id=str(merchant_id),
        run_id=run_id,
        coverage=coverage,
        provider_model_metadata=provider_model_metadata,
    )
    strategic_brief_kwargs = {
        "opportunity": brief_opportunity,
        "attribute_graph": attribute_graph,
        "primary_gaps": [],
        "scores": {},
        "identity": {
            "name": title,
            "confidence": "medium" if title and title != "this product" else "low",
            "unresolved": not bool(title and title != "this product"),
            "anchors": {
                "brand": product.get("brand") or product.get("vendor"),
                "category": product.get("category") or product.get("product_type"),
            },
        },
        "sku_title": title,
        "merchant_host": normalize_host(product.get("canonical_url") or product.get("pdp_url")),
    }
    if competitor_attributes != "not_assessed":
        strategic_brief_kwargs["competitor_attributes"] = competitor_attributes
    display["next_best_action"] = await attach_sku_strategic_brief(
        display.get("next_best_action") or {},
        **strategic_brief_kwargs,
    )
    return display


async def run_per_sku_audit_probe_fanout(
    *,
    merchant_id: str,
    audit_run_id: Optional[str],
    products: List[Dict[str, Any]],
    coverage_profile: str,
    providers: Optional[List[str]] = None,
    model_overrides: Optional[Mapping[str, Any]] = None,
    prompts_per_sku: Optional[int] = None,
    custom_prompts: Optional[List[str]] = None,
    custom_prompts_by_sku: Optional[Mapping[str, List[str]]] = None,
    winnable_prompts: bool = False,
    refresh_prompt_basis: bool = False,
    audit_tier: str = "standard",
    declared_competitors: Optional[List[str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Run and shape v3 per-SKU citation probes before report assembly.

    PIVOTA-Agent caps one probe request at eight runs. The v3 contract is
    `prompts_per_sku` per provider, so this producer chunks the deterministic
    prompt set into upstream-safe batches and returns the normalized
    `per_sku_audit` payload that `load_per_sku_probe_runs` already reads.

    `custom_prompts` are merchant-input prompt slots (billed as prompt credits).
    They're brand-level, so they're probed ONCE (attached to the first SKU) to
    avoid an N-SKU × providers multiplier (see feedback_llm_call_multipliers),
    while still actually running — they were billed-but-never-probed before.

    `custom_prompts_by_sku` (keyed by sku_key) attaches merchant prompts to a
    SPECIFIC SKU: each list is probed inside that SKU's context — joining its
    per-prompt table / win plan / evidence chain — and pinned into that SKU's
    measurement basis for week-over-week comparability.
    """
    if not merchant_id or not str(merchant_id).strip():
        raise ValueError("merchant_id is required for per-SKU probe fan-out")
    coverage = resolve_coverage_profile(
        coverage_profile=coverage_profile,
        providers=providers,
    )
    profile_providers = list(coverage.get("providers") or [])
    provider_model_metadata = resolve_provider_models(
        profile_providers,
        model_overrides=model_overrides,
    )
    sku_keys = await _sku_keys_for_per_sku_mode(products, str(merchant_id))
    target_prompts = max(1, int(prompts_per_sku or 40))
    clean_custom = [
        str(p).strip() for p in (custom_prompts or []) if str(p or "").strip()
    ]
    clean_custom_by_sku: Dict[str, List[str]] = {}
    for _key, _prompts in (custom_prompts_by_sku or {}).items():
        _clean = [
            str(p).strip() for p in (_prompts or []) if str(p or "").strip()
        ]
        if _clean:
            clean_custom_by_sku[str(_key)] = _clean
    out: Dict[str, List[Dict[str, Any]]] = {}
    for idx, sku_key in enumerate(sku_keys):
        sku_ctx = await load_sku_context(sku_key, str(merchant_id))
        # Tier-1 retailer-evidence recycling: prior runs' third-party retailer
        # excerpts (provenance-tagged, gray-market excluded) feed the attribute
        # graph and the winnable/scenario prompt generation BELOW. (The LLM
        # attribute extractor also consumes these excerpts, but it runs earlier —
        # inside load_sku_context — and self-loads them there; when the extractor
        # ran, the product already carries them and the load below short-circuits.)
        # Rescues thin own-page fetches — long-tail brand sites often yield
        # title+brand only while the product's real story lives on Olive
        # Young/Coupang listings we already captured as grounded evidence.
        # Vocabulary/analysis input only; the verdict still reports the thin own
        # page honestly.
        _existing_product = sku_ctx.get("product") if isinstance(sku_ctx, dict) else None
        _has_retailer_excerpts = (
            isinstance(_existing_product, dict)
            and bool(_existing_product.get("_retailer_excerpts"))
        )
        if isinstance(sku_ctx, dict) and not _has_retailer_excerpts:
            try:
                from services.retailer_evidence import load_prior_retailer_evidence

                _re = await load_prior_retailer_evidence(
                    merchant_id=str(merchant_id), sku_key=str(sku_key),
                )
                if _re.get("excerpts"):
                    product_map = sku_ctx.get("product")
                    if isinstance(product_map, dict):
                        product_map["_retailer_excerpts"] = _re["excerpts"]
                        product_map["_retailer_excerpt_hosts"] = _re["hosts"]
            except Exception:  # noqa: BLE001 - enrichment must never block probing
                logger.warning("retailer-evidence stash skipped", exc_info=True)
        # Value-prop discovery prompts: extract from the product's content so the
        # audit probes winnable SPECIFIC demand, not just generic category heads.
        # Best-effort + opt-in (URL audits); stashed for the sync spec builder.
        #
        # W2 PINNED MEASUREMENT BASIS: the LLM-generated lists (winnable + P4a
        # scenario) are the stochastic part of the probe set — regenerating them
        # per run made the same SKU's scores non-comparable run-to-run. The
        # resolver reuses the basis a prior completed run stamped on its report
        # (same questions → comparable scores → honest re-audit delta, and zero
        # extra LLM spend); only a first audit — or an explicit refresh /
        # PROMPT_BASIS_VERSION bump — generates. Scenario elicitation still runs
        # AFTER the Tier-1 retailer-evidence stash so category resolution
        # benefits from retailer excerpts.
        if winnable_prompts and isinstance(sku_ctx, dict):
            from services.prompt_basis import resolve_prompt_basis

            async def _generate_winnable(_ctx: Dict[str, Any] = sku_ctx) -> List[str]:
                return await extract_winnable_prompts(_ctx)

            async def _generate_scenario(_ctx: Dict[str, Any] = sku_ctx) -> List[str]:
                _product_map = _get_product(_ctx)
                from services.sku_sidewalk import build_sku_attribute_graph

                _graph = build_sku_attribute_graph(_product_map or {})
                _elicit_category = _category_for_unbranded_prompts(
                    _product_map or {},
                    str((_product_map or {}).get("product_type") or ""),
                    _graph,
                )
                if not _elicit_category:
                    return []
                return await elicit_scenario_prompts(
                    _ctx, category=_elicit_category,
                )

            try:
                _basis = await resolve_prompt_basis(
                    merchant_id=str(merchant_id),
                    sku_key=str(sku_key),
                    generate_winnable=_generate_winnable,
                    generate_scenario=_generate_scenario,
                    # When set, regenerate the basis instead of pinning a prior
                    # run's — an explicit caller action (e.g. a re-audit that must
                    # reflect newly-grounded attributes in the probed query set,
                    # rather than reuse the frozen pre-change basis).
                    refresh=refresh_prompt_basis,
                    # Tier scopes pinning: only a same-tier prior basis is
                    # reused, and the tier's list cap decides how much of the
                    # LLM discovery lists survives (standard 12, deep 18).
                    audit_tier=audit_tier,
                )
                sku_ctx["_winnable_prompts"] = _basis["winnable"]
                sku_ctx["_scenario_elicited"] = _basis["scenario"]
                # W2.1: when the prior run stored the full probed set, hand it
                # to _probe_per_sku_ctx to reprobe verbatim (no spec rebuild).
                sku_ctx["_pinned_selected_specs"] = _basis.get("selected_specs") or []
                # Stamped on the per-SKU report (build_per_sku_report) so the
                # NEXT run can reload it and the delta can assert basis identity.
                sku_ctx["_prompt_basis_meta"] = _basis["meta"]
            except Exception:  # noqa: BLE001 - never block probing on the LLM step
                logger.warning("prompt-basis resolution skipped", exc_info=True)
        # Depth tier: stash the tier + competitor seeds for the spec builder
        # (Blocks A-F, services/deep_tier_prompts). The stamp is written for
        # EVERY tier, not just deep: URL-wedge synthetic contexts deliberately
        # survive reset_sku_context_cache, so a deep audit followed by a
        # standard audit of the same URL in one process would otherwise reuse
        # a ctx carrying the stale deep stamp and inject the deep blocks into
        # a standard run. Seeds load only when the spec set will actually be
        # rebuilt — a pinned selected set (W2.1) reprobes verbatim, so the
        # prior-run scan would be wasted DB work.
        from services.prompt_basis import AUDIT_TIER_DEEP as _TIER_DEEP

        if isinstance(sku_ctx, dict):
            _run_tier = str(audit_tier or "").strip().lower()
            sku_ctx["_audit_tier"] = _run_tier or "standard"
            if _run_tier != _TIER_DEEP:
                sku_ctx.pop("_deep_competitor_seeds", None)
                sku_ctx.pop("_deep_tier_queries", None)
            elif sku_ctx.get("_pinned_selected_specs"):
                # Pinned re-run: the prior selected set replays VERBATIM, so
                # declared competitors cannot take effect this run. Loud
                # breadcrumb — the route docs tell callers to pass
                # refresh_prompt_basis=true to re-anchor.
                if declared_competitors:
                    logger.info(
                        "deep-tier: declared_competitors ignored on pinned "
                        "re-run (pass refresh_prompt_basis=true to re-anchor) "
                        "merchant=%s sku=%s", merchant_id, sku_key,
                    )
            else:
                from services.deep_tier_prompts import (
                    load_prior_competitor_brands,
                    resolve_deep_anchor_seeds,
                    sanitize_declared_competitors,
                )

                _own_brand = str(
                    (_get_product(sku_ctx) or {}).get("brand")
                    or (_get_product(sku_ctx) or {}).get("vendor")
                    or ""
                )
                # Declared competitors LEAD the anchor list: the merchant/BD
                # knows who is gunning for the shelf — including AEO-active
                # brands the answer harvest structurally cannot see yet. The
                # evidence-ranked harvest fills the remaining slots.
                sku_ctx["_deep_competitor_seeds"] = resolve_deep_anchor_seeds(
                    sanitize_declared_competitors(
                        declared_competitors, own_brand=_own_brand,
                    ),
                    await load_prior_competitor_brands(
                        merchant_id=str(merchant_id),
                        sku_key=str(sku_key),
                        own_brand=_own_brand,
                    ),
                )
        out[sku_key] = await _probe_per_sku_ctx(
            sku_ctx=sku_ctx,
            merchant_id=str(merchant_id),
            coverage=coverage,
            provider_model_metadata=provider_model_metadata,
            prompts_per_sku=target_prompts,
            audit_run_id=audit_run_id,
            # Brand-level merchant prompts run once, on the first SKU only.
            custom_prompts=clean_custom if idx == 0 else None,
            # This SKU's own merchant prompts: probed in-context + pinned.
            pinned_custom_prompts=clean_custom_by_sku.get(str(sku_key)),
        )
        # W2: ride the basis meta on the PERSISTED probe payload. The report
        # phase resets the sku-context cache and reloads sku_ctx fresh, so a
        # ctx stash alone never reaches build_per_sku_report (prod pair
        # 2026-07-04: run #1 stamped prompt_basis=None, run #2 regenerated).
        # Probe runs are what report time durably reloads.
        if isinstance(sku_ctx, dict) and isinstance(
            sku_ctx.get("_prompt_basis_meta"), dict
        ):
            from services.prompt_basis import (
                attach_basis_meta_to_probe_runs,
                set_selected_specs_on_meta,
            )

            # W2.1: fold the queries ACTUALLY probed this run into the meta
            # BEFORE persisting — a fresh run establishes the selected set; a
            # pinned run re-records the same set (chain never breaks).
            final_meta = set_selected_specs_on_meta(
                sku_ctx["_prompt_basis_meta"],
                sku_ctx.get("_selected_specs_out"),
            )
            attach_basis_meta_to_probe_runs(out[sku_key], final_meta)
    return out


def _legacy_verdict_from_report(legacy_report: Dict[str, Any]) -> Optional[str]:
    aggregate = legacy_report.get("aggregate") or {}
    return aggregate.get("brand_verdict_label") or legacy_report.get("verdict_label")


async def _cost_summary_for_per_sku_audit(
    audit_run_id: Optional[str],
    probe_runs_by_sku: Dict[str, Any],
    provider_model_metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    provider_models = _copy_provider_model_metadata(provider_model_metadata)
    for probe_runs in (probe_runs_by_sku or {}).values():
        provider_models.update(_probe_run_provider_model_metadata(probe_runs))
    if audit_run_id:
        try:
            from db.llm_probe_runs import aggregate_cost_for_run
            recorded = await aggregate_cost_for_run(audit_run_id=audit_run_id)
        except Exception:
            recorded = None
        if recorded:
            return {
                "prompts": int(recorded.get("llm_calls") or 0),
                "providers": [
                    p.get("provider")
                    for p in recorded.get("providers") or []
                    if p.get("provider")
                ],
                **recorded,
                "provider_models": provider_models,
                "model_is_override": _any_model_override(provider_models),
            }

    providers = set()
    prompts = 0
    input_tokens = 0
    output_tokens = 0
    estimated_cost = 0.0
    for probe_runs in (probe_runs_by_sku or {}).values():
        for probe in _json_list(probe_runs):
            if not isinstance(probe, dict):
                continue
            if probe.get("provider"):
                providers.add(probe.get("provider"))
            prompts += len(probe.get("raw_runs") or [])
            usage = probe.get("usage") or {}
            input_tokens += int(usage.get("input_tokens") or usage.get("tokens_in") or 0)
            output_tokens += int(usage.get("output_tokens") or usage.get("tokens_out") or 0)
            estimated_cost += float(usage.get("estimated_cost_usd") or usage.get("cost_usd") or 0)
    return {
        "prompts": prompts,
        "providers": sorted(providers),
        "llm_calls": prompts,
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "estimated_cost_usd": round(estimated_cost, 6),
        "provider_models": provider_models,
        "model_is_override": _any_model_override(provider_models),
        "_telemetry_source": "per_sku_probe_payload",
    }


def _combine_scan_results_for_profile(
    scan_mode: str,
    provider_results: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    results = [
        payload
        for payload in provider_results.values()
        if isinstance(payload, dict)
    ]
    if not results:
        return None

    raw_runs: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []
    providers_actual: List[str] = []
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": 0.0,
    }
    score_values: List[int] = []

    for requested_provider, result in provider_results.items():
        actual_provider = str(
            result.get("provider") or requested_provider
        ).strip().lower()
        if actual_provider and actual_provider not in providers_actual:
            providers_actual.append(actual_provider)
        scores = result.get("scores") if isinstance(result.get("scores"), dict) else {}
        if scores.get("visibility_score") is not None:
            score_values.append(int(scores.get("visibility_score") or 0))
        for run in result.get("raw_runs") or []:
            if not isinstance(run, dict):
                continue
            row = dict(run)
            row.setdefault("_provider", actual_provider or requested_provider)
            raw_runs.append(row)
        for finding in result.get("findings") or []:
            if isinstance(finding, dict):
                copy = dict(finding)
                copy.setdefault("provider", actual_provider or requested_provider)
                findings.append(copy)
        result_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        usage["input_tokens"] += int(
            result_usage.get("input_tokens") or result_usage.get("tokens_in") or 0
        )
        usage["output_tokens"] += int(
            result_usage.get("output_tokens") or result_usage.get("tokens_out") or 0
        )
        usage["estimated_cost_usd"] += float(
            result_usage.get("estimated_cost_usd")
            or result_usage.get("cost_usd")
            or 0.0
        )

    return {
        "scan_mode": scan_mode,
        "provider": ",".join(providers_actual),
        "providers": providers_actual,
        "scores": {
            "visibility_score": max(score_values) if score_values else 0,
            "aggregation_rule": "any_profile_provider_max_score",
        },
        "findings": findings,
        "usage": {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "estimated_cost_usd": round(usage["estimated_cost_usd"], 6),
        },
        "raw_runs": raw_runs,
        "provider_results": provider_results,
    }


def _combine_bd_probes_for_profile(
    probes_by_provider: Dict[str, Dict[str, Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    scan_modes: List[str] = []
    for probes in probes_by_provider.values():
        for scan_mode in probes.keys():
            if scan_mode not in scan_modes:
                scan_modes.append(scan_mode)
    combined: Dict[str, Dict[str, Any]] = {}
    for scan_mode in scan_modes:
        per_provider = {
            provider: probes[scan_mode]
            for provider, probes in probes_by_provider.items()
            if isinstance(probes, dict) and isinstance(probes.get(scan_mode), dict)
        }
        merged = _combine_scan_results_for_profile(scan_mode, per_provider)
        if merged is not None:
            combined[scan_mode] = merged
    return combined


def _legacy_citation_by_provider(
    *,
    probes_by_provider: Dict[str, Dict[str, Dict[str, Any]]],
    merchant_host: Optional[str],
    merchant_brand: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for provider, probes in sorted(probes_by_provider.items()):
        attribution = probes.get("attribution") if isinstance(probes, dict) else None
        if not isinstance(attribution, dict):
            continue
        raw_runs = attribution.get("raw_runs") or []
        competitors, merchant_cited_runs, runs_with_any_citation = (
            extract_cited_hosts(
                raw_runs,
                merchant_host=merchant_host,
                merchant_brand=merchant_brand,
            )
        )
        out[provider] = {
            "runs": len(raw_runs),
            "merchant_cited_runs": merchant_cited_runs,
            "runs_with_any_citation": runs_with_any_citation,
            "attribution_score": (
                (attribution.get("scores") or {}).get("visibility_score", 0)
            ),
            "competitor_hosts": [
                {"host": host, "times_cited": count}
                for host, count in competitors.most_common(15)
            ],
        }
    return out


# ---------------------------------------------------------------------------
# Brand-level multi-product BD report (Phase 2b)
#
# Merchants pitch their brand, not a single SKU. The brand report runs
# the standard BD probe pipeline against up to 5 flagship products of
# one merchant, then aggregates: per-product structured reports, brand
# verdict from averaged scores, cross-product competitor host frequency.
#
# Hard cap of 5 products is the post-#280 cost guard equivalent at the
# brand level: 5 products × 3 scan modes × 3 runs = 45 grounded Gemini
# calls = ~1.1M tokens per brand report. Runs sequentially (each per-
# product call is already a 9-call probe burst); a future async fan-out
# is possible once worker-pool isolation lands.
# ---------------------------------------------------------------------------


# Spec §I — service-layer cap matches the route-layer cap. Lifted from
# 5 → 50 because credit pre-flight is now the authoritative cost gate.
# The per-product synchronous fan-out (~9-call probe burst per product
# in legacy mode, ~40 in per-SKU mode) is still bounded by the
# per-tenant concurrency gate in the probe engine.
_BRAND_REPORT_MAX_PRODUCTS = 50


async def run_brand_report(
    *,
    merchant_name: str,
    merchant_domain: Optional[str],
    products: List[Dict[str, Any]],
    provider: Optional[str] = None,
    coverage_profile: str = "us_shopper",
    providers: Optional[List[str]] = None,
    model_overrides: Optional[Mapping[str, Any]] = None,
    prompts_per_sku: Optional[int] = None,
    max_runs: int = 3,
    product_concurrency: int = 1,
    parallel_scan_modes: bool = False,
    include_category_visibility: bool = True,
    prior_runs: Optional[List[Dict[str, Any]]] = None,
    integration_state: Optional[Dict[str, Any]] = None,
    include_social_intelligence: bool = False,
    audit_mode: str = "legacy",
    merchant_id: Optional[str] = None,
    audit_run_id: Optional[str] = None,
    verify_providers: Optional[List[str]] = None,
    custom_prompts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run BD probes against up to 5 products of one merchant and
    aggregate into a brand-level report.

    `products` items: { title, vendor?, product_type?, pdp_url }

    Returns:
      {
        merchant_name, merchant_domain, timestamp, provider/providers,
        per_product: [<structured BD report>, ...],
        aggregate: {
          avg_visibility, avg_attribution, avg_category_visibility,
          brand_verdict_label, brand_verdict_explanation,
          products_count, products_succeeded, products_failed,
        },
        cross_product_competitors: [{host, times_cited}, ...],
        failed: [{pdp_url, error}, ...],
      }
    """
    if not merchant_name or not merchant_name.strip():
        raise ValueError("merchant_name is required")
    if not products:
        raise ValueError("products is required (at least 1)")
    if audit_mode not in {"legacy", "per_sku"}:
        raise ValueError("audit_mode must be 'legacy' or 'per_sku'")
    coverage = resolve_coverage_profile(
        coverage_profile=coverage_profile,
        provider=provider,
        providers=providers,
    )
    profile_providers = list(coverage.get("providers") or [])
    # Answer-quality verify providers. The explicit/single coverage paths (which
    # the merchant "Models to run" UI always triggers) return verify_providers=[];
    # default to the verify-supported set so verification isn't silently disabled.
    resolved_verify_providers = _resolve_audit_verify_providers(
        coverage, verify_providers
    )
    verify_sample = coverage.get("verify_sample") or {}
    provider_model_metadata = resolve_provider_models(
        profile_providers,
        model_overrides=model_overrides,
    )
    provider_label = (
        profile_providers[0]
        if len(profile_providers) == 1
        else ",".join(profile_providers)
    )

    if audit_mode == "per_sku":
        if not merchant_id or not str(merchant_id).strip():
            raise ValueError("merchant_id is required for audit_mode='per_sku'")
        reset_sku_context_cache()
        sku_keys = await _sku_keys_for_per_sku_mode(products, str(merchant_id))
        if not sku_keys:
            raise ValueError("per_sku audit requires products with sku_key or product_key")
        per_sku_reports: List[Dict[str, Any]] = []
        probe_runs_by_sku: Dict[str, Any] = {}
        for sku_key in sku_keys:
            probe_runs_by_sku[sku_key] = await load_per_sku_probe_runs(
                sku_key, str(merchant_id), audit_run_id,
            )
            sku_ctx = await load_sku_context(sku_key, str(merchant_id))
            verify_summary, verify_outputs = await _run_deepseek_verify_pass(
                sku_ctx=sku_ctx,
                probe_runs=probe_runs_by_sku[sku_key],
                merchant_id=str(merchant_id),
                audit_run_id=audit_run_id,
                verify_providers=resolved_verify_providers,
                verify_sample=verify_sample,
                prompts_per_sku=prompts_per_sku,
            )
            per_sku_reports.append(
                await build_per_sku_report(
                    sku_key,
                    str(merchant_id),
                    audit_run_id,
                    provider_model_metadata,
                    verify_outputs=verify_outputs,
                    verify_summary=verify_summary,
                )
            )
        brand_rollup = build_brand_rollup(per_sku_reports, str(merchant_id))
        # Phase 2: surface the niche-targeting the per-SKU opportunity already
        # computes as a first-class "where you can win" strategy.
        brand_rollup["where_you_can_win"] = build_where_you_can_win(per_sku_reports)
        # Win-the-specific-long-tail (Step 2): surface the engine's computed-but-
        # unprobed specific niches as suggested prompts the merchant can test (feeds
        # the guided custom-prompt UI). Disjoint from where_you_can_win.targets
        # (those are probed open lanes; these are un-probed candidates).
        brand_rollup["suggested_prompts"] = build_suggested_prompts(per_sku_reports)
        # Issue #902 item 1: roll the per-SKU Google indexing arcs up to the
        # brand, so a merchant reads zero category citations on freshly-minted
        # Pivota canonical PDPs as indexing latency, not a content gap.
        brand_rollup["indexing_arc"] = _brand_indexing_arc(per_sku_reports)
        # Phase 2 v2: record this audit's probed niche queries (compounds the
        # cross-merchant recurrence demand signal) and attach recurrence to the
        # winnable targets so the operator can rank by it. Best-effort.
        try:
            from services.niche_recurrence import record_niche_queries

            _probed_niche_queries = [
                row.get("normalized_query") or row.get("query")
                for _r in per_sku_reports
                for row in ((_r.get("opportunity") or {}).get("per_prompt") or [])
                if isinstance(row, dict)
            ]
            await record_niche_queries(
                queries=_probed_niche_queries, merchant_id=str(merchant_id)
            )
            await attach_niche_recurrence(brand_rollup["where_you_can_win"])
        except Exception:  # noqa: BLE001
            logger.warning("niche recurrence record/attach failed", exc_info=True)
        # Phase 4: record this audit's per-niche ownership, then attach re-audit
        # movement (won / holding / lost / still_open) to the targets. Record
        # FIRST so movement compares this audit (current) vs the prior. Best-effort.
        try:
            from services.niche_outcomes import record_niche_outcomes

            await record_niche_outcomes(
                per_sku_reports=per_sku_reports,
                merchant_id=str(merchant_id),
                audit_run_id=audit_run_id,
            )
            await attach_niche_movement(
                brand_rollup["where_you_can_win"], merchant_id=str(merchant_id)
            )
        except Exception:  # noqa: BLE001
            logger.warning("niche outcomes record/attach failed", exc_info=True)
        # Phase 4 v2: the transaction-outcomes moat (orders / refund / GMV), honest
        # + omitted until there are real transactions. Best-effort.
        try:
            _outcomes = await build_outcomes_summary(str(merchant_id))
            if _outcomes:
                brand_rollup["outcomes"] = _outcomes
        except Exception:  # noqa: BLE001
            logger.warning("outcomes summary failed", exc_info=True)
        # Issue #902 item 2: brand-level integration status + GSC-connect CTA,
        # ported from the legacy merchant_view (the per-SKU path never carried
        # them). Best-effort: a lookup failure must not sink the report.
        try:
            _integration = await _per_sku_integration_block(
                str(merchant_id), integration_state,
            )
            if _integration is not None:
                brand_rollup["integration"] = _integration
        except Exception:  # noqa: BLE001
            logger.warning("per-sku integration block failed", exc_info=True)
        _merchant_host = normalize_host(merchant_domain or "") or (
            (merchant_domain or "").strip() or None
        )
        # Durable account-level brand signal: a store_less signup IS a brand (no
        # retail storefront), so it must never re-derive as reseller from its
        # catalog vendor mix on every audit. Best-effort — a lookup failure leaves
        # the catalog-derived classification unchanged (byte-identical to before).
        _operating_mode: Optional[str] = None
        if merchant_id:
            try:
                from db.merchant_onboarding import get_merchant_onboarding

                _onboarding = await get_merchant_onboarding(str(merchant_id))
                if isinstance(_onboarding, dict):
                    _operating_mode = _onboarding.get("operating_mode")
            except Exception:  # noqa: BLE001
                logger.warning("operating_mode load failed", exc_info=True)
        _merchant_vendors, _merchant_is_reseller = _audit_merchant_vendors(
            merchant_name,
            _merchant_host,
            [p.get("vendor") for p in products if isinstance(p, dict)],
            operating_mode=_operating_mode,
        )
        # R2 signal: carry the derived merchant type so the report can frame
        # findability/endorsement honestly for a reseller (the brands it carries
        # vs the store itself).
        brand_rollup["merchant_type"] = "reseller" if _merchant_is_reseller else "brand"
        # Other domains Pivota already knows this merchant owns (onboarding +
        # catalog). Best-effort: a failure must not break the audit, and a
        # non-onboarded URL-wedge merchant simply returns none (the brand-alias
        # storefront-affix match in _host_is_first_party still catches
        # brand-named second domains like tryanuko.com).
        _merchant_owned_hosts: set = set()
        try:
            from services.brand_claim_service import merchant_owned_domains

            _merchant_owned_hosts = await merchant_owned_domains(str(merchant_id))
        except Exception:  # noqa: BLE001
            logger.warning("per-sku owned-domains load failed", exc_info=True)
        authority_map = build_authority_map(
            per_sku_reports,
            probe_runs_by_sku,
            merchant_host=_merchant_host,
            merchant_brand=merchant_name,
            merchant_vendors=_merchant_vendors,
            merchant_extra_hosts=_merchant_owned_hosts,
        )
        # R3 — store-as-destination (the retailer win metric): is the store the
        # AI-routed buy path, and who does AI route to instead. Reuses the
        # navigational citation rate + the authority hosts' buy-intent flags.
        brand_rollup["store_as_destination"] = _store_as_destination(
            brand_rollup.get("citation_by_intent"),
            authority_map.get("hosts"),
        )
        median_citation = (
            (brand_rollup.get("dimensions") or {})
            .get("citation", {})
            .get("median")
        )
        # Honest brand verdict — see _per_sku_brand_verdict. A blocked /
        # pre-index brand must NOT collapse to a false "invisible" verdict;
        # the get-indexed action becomes step 1 below. Sum the per-SKU
        # first-party citation counts so an INVISIBLE explanation reports the
        # real "cited in N of M queries" instead of falsely asserting the
        # merchant URL never appeared in any grounded source.
        _fp_cited = 0
        _fp_total = 0
        for _r in per_sku_reports:
            _fp = (
                (((_r.get("scores") or {}).get("citation") or {}).get("breakdown") or {})
                .get("first_party_rate")
                or {}
            )
            try:
                _fp_cited += int(_fp.get("numerator") or 0)
                _fp_total += int(_fp.get("denominator") or 0)
            except (TypeError, ValueError):
                continue
        # _fp_total > 0 holds whenever a scored verdict is reached: a non-None
        # median_citation means at least one SKU's compute_citation_score
        # returned non-None, which only happens when len(runs) > 0, and
        # first_party_rate.denominator == len(runs). The guard is belt-and-
        # suspenders (evidence with total=0 would render a nonsensical
        # "None of 0 queries"); _per_sku_brand_verdict routes the true
        # no-signal case to the blocked/insufficient branch before verdict_for.
        _brand_verdict_evidence = (
            {
                "attribution_runs_total": _fp_total,
                "merchant_cited_runs": _fp_cited,
            }
            if _fp_total > 0
            else None
        )
        brand_state, legacy_label, brand_verdict_explanation = _per_sku_brand_verdict(
            median_citation,
            len(per_sku_reports),
            len(brand_rollup.get("blocked_skus") or []),
            evidence=_brand_verdict_evidence,
        )
        brand_rollup["brand_state"] = brand_state
        brand_rollup["brand_verdict_label"] = legacy_label
        brand_rollup["brand_verdict_explanation"] = brand_verdict_explanation
        cost_summary = await _cost_summary_for_per_sku_audit(
            audit_run_id,
            probe_runs_by_sku,
            provider_model_metadata,
        )
        # Surface the merchant's custom ("Your Prompts") slots as an
        # open-vs-contested-lane table. They were probed (axis="custom") but the
        # per-SKU scorecard only scores the auto-generated queries, so without
        # this they ran + billed without ever being shown (the same silent-drop
        # family #820 closed at the probe layer).
        custom_prompt_results = build_custom_prompt_results(
            probe_runs_by_sku,
            custom_prompts,
            merchant_host=_merchant_host,
            merchant_brand=merchant_name,
            # Reuse the retailer-aware identity (resold brands not folded in).
            merchant_vendors=_merchant_vendors,
        )
        brand_verify_summary = _rollup_verify_summaries(per_sku_reports)
        # Fix 4 — per-SKU win-plan: for each losing category query, the
        # independent hosts AI grounds on (the targets), the competitor
        # benchmark, and the honest outreach path (incl. one-click pitch drafts
        # for emailable targets). Re-derives the per-query host linkage
        # authority_map aggregates away (joins each losing query's raw grounding
        # uri back to the resolved host rows). Built BEFORE the narrative so its
        # brand rollup can feed where_youre_losing.win_plan_summary. Best-effort:
        # never let it sink the report.
        try:
            win_plan = build_win_plan(
                per_sku_reports=per_sku_reports,
                authority_map=authority_map,
                merchant_name=merchant_name,
            )
        except Exception:  # noqa: BLE001
            logger.warning("win_plan build failed", exc_info=True)
            win_plan = None
        # Merchant/run-level vertical (dominant across the audit's SKUs) — the
        # run-level competitor panels below aggregate names across SKUs, so they
        # use this single profile rather than a per-SKU one. Beauty -> byte-identical.
        _merchant_profile = _merchant_profile_from_reports(per_sku_reports)
        # C3 — for a reseller, the winning competitor products AI names that the
        # merchant does NOT carry (a stocking / sourcing signal; the catalog-overlap
        # no DIY-with-a-frontier-model can produce). Best-effort + reseller-gated.
        if _merchant_is_reseller:
            try:
                brand_rollup["winning_products_not_carried"] = (
                    await _winning_products_not_carried(
                        str(merchant_id), win_plan, vertical_profile=_merchant_profile
                    )
                )
            except Exception:  # noqa: BLE001
                logger.warning("C3 winning_products_not_carried failed", exc_info=True)
        # Fix 3 — merchant-grade narrative assembled from the Fix 1 resolved
        # hosts + Fix 2 findability/endorsement split + verify rollup + the Fix 4
        # win-plan rollup. No fabrication: degrades to honest "not available"
        # when data is missing. Best-effort like the sibling enrichments above:
        # a malformed per-SKU report must never sink the whole brand report.
        try:
            merchant_narrative = build_merchant_narrative(
                merchant_name=merchant_name,
                per_sku_reports=per_sku_reports,
                brand_rollup=brand_rollup,
                authority_map=authority_map,
                verify_summary=brand_verify_summary,
                providers=profile_providers,
                verify_providers=resolved_verify_providers,
                pending_engine_support=coverage.get("pending_engine_support") or [],
                coverage_profile=coverage.get("profile"),
                win_plan=win_plan,
                vertical_profile=_merchant_profile,
            )
        except Exception:  # noqa: BLE001
            logger.warning("merchant_narrative build failed", exc_info=True)
            merchant_narrative = None
        # Step 3 (performance-over-time): run-level scores (so the finalize path
        # persists non-NULL columns instead of leaving the trend permanently empty)
        # + the run-over-run history the FE renders. Reuses _build_history_trend —
        # no new trend math, no per-SKU history table. Both ride on brand_rollup so
        # BrandRollupCover can read them. history is None on the first audit.
        # Best-effort like the win_plan / merchant_narrative siblings above: a
        # malformed prior_run must never sink the whole per_sku report.
        try:
            run_scores = _per_sku_run_aggregate(per_sku_reports)
            brand_rollup["run_scores"] = run_scores
            # Mode purity: compare only against prior per_sku runs (see
            # _per_sku_prior_runs) so the delta isn't a misleading legacy-vs-per_sku
            # comparison.
            history = _build_history_trend(
                _per_sku_prior_runs(prior_runs),
                current_scores={
                    "visibility": run_scores["avg_visibility"],
                    "attribution": run_scores["avg_attribution"],
                    "category_visibility": run_scores["avg_category_visibility"],
                    # Per-engine medians for this run → drives the per-model delta.
                    "by_model": _provider_medians(brand_rollup.get("citation_by_provider")),
                },
            )
            brand_rollup["tracking"] = {"history": history} if history else None
        except Exception:  # noqa: BLE001
            logger.warning("per_sku trend attach failed", exc_info=True)
            brand_rollup["tracking"] = None
        # W1 RunFacts phase 1 — compute the fact layer once per SKU from the
        # SAME probe runs the per-SKU scorecards read, stamped per report and
        # folded into a brand-level rollup on brand_rollup. Additive: no
        # rendered number reads from it yet (phase-2 cutover); W7 invariants
        # and parity logging do. Best-effort: a stamp failure must never sink
        # the report.
        try:
            _facts_by_sku = {
                _sku_key: compute_run_facts(
                    _flatten_probe_runs(_sku_probe_runs),
                    merchant_host=_merchant_host,
                    merchant_brand=merchant_name,
                    merchant_vendors=_merchant_vendors,
                ).to_dict()
                for _sku_key, _sku_probe_runs in probe_runs_by_sku.items()
            }
            for _r in per_sku_reports:
                if _r.get("sku_key") in _facts_by_sku:
                    _r["run_facts"] = _facts_by_sku[_r["sku_key"]]
            brand_rollup["run_facts"] = aggregate_run_facts(
                list(_facts_by_sku.values()),
                identity={"host": _merchant_host, "brand": merchant_name},
            )
            # W1 site 8 (CUTOVER): the rendered endorsement now IS the RunFacts T2
            # set (build_authority_map overlays it). The retired SKU-name-gated
            # value is stashed under `endorsement_hosts_legacy`; the tripwire keeps
            # measuring the legacy-vs-RunFacts gap so a regression in either surface
            # still shows up (grep RUNFACTS_PARITY_DRIFT).
            _legacy_endorse = sorted(
                str(h)
                for h in (
                    (authority_map.get("host_attribution_summary") or {}).get(
                        "endorsement_hosts_legacy"
                    )
                    or []
                )
                if h
            )
            _facts_endorse = sorted(
                brand_rollup["run_facts"].get("endorsement_hosts") or []
            )
            parity_measure(
                "bd_report._citation_signals.endorsement_hosts",
                _legacy_endorse[:10],
                _facts_endorse[:10],
                context={
                    "merchant": merchant_name,
                    "legacy_count": len(_legacy_endorse),
                    "run_facts_count": len(_facts_endorse),
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning("per_sku run_facts stamp failed", exc_info=True)
        brand_report = {
            "audit_run_id": audit_run_id,
            "merchant_id": str(merchant_id),
            "merchant_name": merchant_name,
            "merchant_domain": (merchant_domain or "").strip() or None,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "audit_mode": "per_sku",
            "coverage_profile": coverage.get("profile"),
            "providers": profile_providers,
            "verify_providers": resolved_verify_providers,
            "pending_engine_support": coverage.get("pending_engine_support") or [],
            "verify_sample": verify_sample,
            "provider_models": provider_model_metadata,
            "model_is_override": _any_model_override(provider_model_metadata),
            "per_sku_reports": per_sku_reports,
            "brand_rollup": brand_rollup,
            "custom_prompts": custom_prompt_results,
            "verify_summary": brand_verify_summary,
            "authority_map": authority_map,
            "merchant_narrative": merchant_narrative,
            # Honesty gate (Principle 4): a category with no grounded-evidence
            # binding (electronics/generic — evidence_bindings="none") DISCLOSES
            # it rather than implying full coverage. Beauty is INCI-grounded and
            # carries no disclosure, so the key is OMITTED -> beauty reports are
            # byte-identical. Uses the merchant/dominant-vertical profile.
            **(
                {"grounded_coverage_disclosure": _merchant_profile.grounded_coverage_disclosure}
                if getattr(_merchant_profile, "grounded_coverage_disclosure", None)
                else {}
            ),
            "win_plan": win_plan,
            "brand_state": brand_state,
            "brand_verdict_label": legacy_label,
            "brand_verdict_explanation": brand_verdict_explanation,
            "legacy_verdict": legacy_label,
            "cost_summary": cost_summary,
        }
        # Audit→action→outcome loop (per-SKU, the production mode): what
        # changed at the hosts the PRIOR run's win_plan / outreach moves told
        # the merchant to target. Attached TOP-LEVEL (beside win_plan /
        # merchant_narrative — the per-SKU report has no brand merchant_view).
        # Mode purity mirrors the trend above: only per_sku priors are
        # comparable. Best-effort like every sibling enrichment.
        await _attach_outreach_outcomes_per_sku(
            brand_report,
            merchant_id=str(merchant_id),
            prior_runs=_per_sku_prior_runs(prior_runs) if prior_runs is not None else None,
        )
        return brand_report

    if len(products) > _BRAND_REPORT_MAX_PRODUCTS:
        raise ValueError(
            f"products capped at {_BRAND_REPORT_MAX_PRODUCTS} per brand "
            f"report (received {len(products)}). Cost guard — see #280."
        )

    per_product: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    # Audit one product → ("ok", structured) | ("fail", failure). Extracted so
    # products can run either sequentially (default — the synced per-SKU audit
    # is unchanged) or with bounded concurrency (the wedge sets
    # product_concurrency to keep its ≤5-SKU free audit under the client
    # timeout). Each call only reads merchant-level immutable inputs and
    # returns its own result — no shared state — so concurrency is safe; the
    # caller's semaphore bounds LLM fan-out per the PR #278 safety rule.
    async def _audit_one_product(p):
        pdp_url = (p.get("pdp_url") or "").strip()
        title = (p.get("title") or "").strip()
        if not pdp_url or not title:
            return ("fail", {
                "pdp_url": pdp_url,
                "title": title,
                "error": "pdp_url and title are required for each product",
            })
        try:
            probes_by_provider: Dict[str, Dict[str, Dict[str, Any]]] = {}
            provider_failures: Dict[str, Dict[str, Any]] = {}
            for provider_id in profile_providers:
                model_info = provider_model_metadata.get(provider_id) or {}
                try:
                    probes_by_provider[provider_id] = await run_bd_probes(
                        merchant_name=merchant_name,
                        merchant_pdp_url=pdp_url,
                        product_title=title,
                        product_vendor=p.get("vendor"),
                        product_type=p.get("product_type"),
                        provider=provider_id,
                        max_runs=max_runs,
                        model=model_info.get("model"),
                        model_is_override=bool(model_info.get("model_is_override")),
                        include_category_visibility=include_category_visibility,
                        parallel_scan_modes=parallel_scan_modes,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate providers
                    provider_failures[provider_id] = {
                        "status": "probe_failed",
                        "error": str(exc)[:500],
                    }
                    logger.warning(
                        "run_brand_report provider probe failed "
                        "provider=%s product=%s: %s",
                        provider_id, title, str(exc)[:200],
                    )
            if not probes_by_provider:
                raise RuntimeError(
                    "all resolved providers failed for product "
                    f"{title!r}: {provider_failures}"
                )
            probes = _combine_bd_probes_for_profile(probes_by_provider)
            structured = build_structured_report(
                merchant_name=merchant_name,
                merchant_pdp_url=pdp_url,
                product_title=title,
                product_vendor=p.get("vendor"),
                product_type=p.get("product_type"),
                visibility_result=probes["visibility"],
                attribution_result=probes["attribution"],
                category_visibility_result=probes.get("category_visibility"),
                provider=provider_label,
                # Per-product url_source threads through from the
                # merchant audit route's 3-tier fallback chain so the
                # `merchant_view.headline.audited_via_pivota_canonical`
                # flag is per-product accurate, not just top-level.
                url_source=p.get("url_source"),
                # PR-C: prior_runs → trend in merchant_view.tracking.
                # Same merchant-level history is duplicated to each
                # per-product report so the frontend doesn't have to
                # join across products + audit history separately.
                # Mode purity (symmetric to per_sku): exclude per_sku priors so a
                # legacy delta isn't computed against per_sku score semantics.
                prior_runs=_legacy_prior_runs(prior_runs),
                # PR-D: per-product mint timestamp — when this row's
                # Pivota canonical sig was first created. Drives
                # merchant_view.diagnosis.indexing_arc_state's real
                # phase computation (replaces the static caveat).
                pivota_signature_minted_at=p.get("pivota_signature_minted_at"),
                # Phase 0: same merchant-level integration state on
                # every product report so the integration action
                # consistently fires (or stays absent) across products.
                integration_state=integration_state,
                # Cold-start is a BD prospect (no real merchant) AND a
                # synthetic-unintegrated shape. Combining both signals
                # flips exactly one case vs the old shape-only heuristic:
                # a REAL merchant who's onboarded nothing yet (non-prospect
                # id, unintegrated shape) is no longer misread as cold-
                # start, so the "Complete Pivota integration" CTA fires for
                # them. Every other case (prospect+synthetic → cold-start;
                # competitor/None with integration_state=None → not cold-
                # start; fully onboarded → not cold-start) is unchanged.
                is_cold_start=(
                    _merchant_id_is_prospect(merchant_id)
                    and _is_cold_start_audit(integration_state)
                ),
            )
            structured["coverage_profile"] = coverage.get("profile")
            structured["providers"] = profile_providers
            structured["verify_providers"] = resolved_verify_providers
            structured["requested_providers"] = (
                coverage.get("requested_providers") or profile_providers
            )
            structured["pending_engine_support"] = (
                coverage.get("pending_engine_support") or []
            )
            structured["verify_sample"] = verify_sample
            structured["provider_models"] = provider_model_metadata
            structured["model_is_override"] = _any_model_override(
                provider_model_metadata
            )
            structured["citation_aggregation_rule"] = (
                "any_profile_provider: headline attribution/citation "
                "presence uses the strongest provider result in the "
                "resolved profile; citation_by_provider preserves the "
                "drill-down."
            )
            citation_by_provider = _legacy_citation_by_provider(
                probes_by_provider=probes_by_provider,
                merchant_host=normalize_host(pdp_url),
                merchant_brand=(p.get("vendor") or merchant_name or "").strip() or None,
            )
            for failed_provider, failure in provider_failures.items():
                citation_by_provider[failed_provider] = dict(failure)
            structured["citation_by_provider"] = citation_by_provider
            if provider_failures:
                structured["provider_failures"] = provider_failures
            await _attach_reaudit_delta(
                structured,
                merchant_id=merchant_id,
                prior_runs=_legacy_prior_runs(prior_runs),
            )
            return ("ok", structured)
        except Exception as exc:  # noqa: BLE001 — per-product isolation
            return ("fail", {
                "pdp_url": pdp_url,
                "title": title,
                "error": str(exc),
            })

    # Sequential by default (synced audit, behavior-equivalent to the prior
    # loop); bounded-concurrent when the caller opts in. Order preserved.
    if product_concurrency and product_concurrency > 1:
        _sem = asyncio.Semaphore(
            min(int(product_concurrency), _BRAND_REPORT_MAX_PRODUCTS)
        )

        async def _bounded(p):
            async with _sem:
                return await _audit_one_product(p)

        _results = await asyncio.gather(*[_bounded(p) for p in products])
    else:
        _results = [await _audit_one_product(p) for p in products]
    for _status, _payload in _results:
        (per_product if _status == "ok" else failed).append(_payload)

    aggregate = _aggregate_brand_scores(per_product)
    aggregate["products_count"] = len(products)
    aggregate["products_succeeded"] = len(per_product)
    aggregate["products_failed"] = len(failed)

    cross_competitors = _aggregate_brand_competitors(
        per_product,
        # N5 PR-7: exclude merchant's own brand-derived domain from
        # the rollup. Required for external_seed audits where
        # merchant_host is the Pivota canonical URL, not the brand's
        # real D2C domain.
        merchant_brand=merchant_name,
        merchant_domain=merchant_domain,
    )

    # PR-8 (Option A step 3): wire bd_brand_signals.infer_social_intelligence
    # into the main audit so brand TikTok/Instagram presence + KOL
    # endorsements + competitive social comparison surface in every
    # merchant report — not just BD cold-start prospects.
    #
    # OPT-IN gate: the function adds 4-5 grounded Gemini calls per
    # audit (parallel via asyncio.gather, ~20s wall). Per the
    # LLM-multiplier-safety standing rule (PR #278 incident), default
    # is OFF until staging load-test confirms the per-tenant semaphore
    # bounds the additional load. Callers opt-in via
    # `include_social_intelligence=True`. The function ALSO short-
    # circuits to {available:false} when GEMINI_API_KEY is absent or
    # merchant_domain is empty, so a stray True flag in a misconfigured
    # env doesn't fire calls.
    #
    # Competitor brand input: the audit's `competitive_pressure.peers_named`
    # list across all products (deduped) is the natural feed for the
    # competitive comparison sub-call.
    social_intelligence: Optional[Dict[str, Any]] = None
    if include_social_intelligence and merchant_domain:
        peer_brand_names: List[str] = []
        seen_brand_names: set = set()
        for p in per_product:
            for peer in (p.get("competitive_pressure") or {}).get("peers_named") or []:
                name = (peer.get("name") or "").strip()
                if name and name.lower() not in seen_brand_names:
                    seen_brand_names.add(name.lower())
                    peer_brand_names.append(name)
        peer_brand_names = peer_brand_names[:10]
        try:
            from services.bd_brand_signals import infer_social_intelligence
            social_intelligence = await infer_social_intelligence(
                brand=merchant_name,
                domain=merchant_domain,
                detected_handles=None,
                competitor_brands=peer_brand_names or None,
            )
        except Exception:  # noqa: BLE001
            # Same pattern as bd_cold_start_service — never let social
            # inference failures block the audit return. Surface as
            # null; the merchant report renders the section only when
            # available=True, so a null here cleanly omits the
            # section without crashing the audit.
            social_intelligence = None

    # P2 (post-#525 codex review): reconcile the three competitor
    # surfaces (host rollup / category peers / social benchmark) into
    # one brand-keyed `competitor_entities` view. Additive — the raw
    # surfaces above are untouched; this is the coherent "what do we
    # know about competitor X" rollup a BD operator actually wants.
    competitor_entities = _reconcile_competitor_entities(
        cross_product_competitors=cross_competitors,
        per_product=per_product,
        social_intelligence=social_intelligence,
        merchant_brand=merchant_name,
    )

    # W1 RunFacts phase 1 — brand-level rollup folded from the per-product
    # stamps, so brand counters and per-product counters share one basis.
    # Additive; W7's _inv_counters_match_run_facts asserts the fold matches.
    brand_run_facts = aggregate_run_facts(
        [p.get("run_facts") for p in per_product],
        identity={
            "host": normalize_host(merchant_domain or "") or None,
            "brand": merchant_name,
        },
    )

    return {
        "merchant_name": merchant_name,
        "merchant_domain": (merchant_domain or "").strip() or None,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": provider_label,
        "coverage_profile": coverage.get("profile"),
        "providers": profile_providers,
        "verify_providers": resolved_verify_providers,
        "requested_providers": coverage.get("requested_providers") or profile_providers,
        "pending_engine_support": coverage.get("pending_engine_support") or [],
        "verify_sample": verify_sample,
        "provider_models": provider_model_metadata,
        "model_is_override": _any_model_override(provider_model_metadata),
        "per_product": per_product,
        "aggregate": aggregate,
        "run_facts": brand_run_facts,
        "cross_product_competitors": cross_competitors,
        "competitor_entities": competitor_entities,
        "social_intelligence": social_intelligence,
        "failed": failed,
    }


def _aggregate_brand_verdict_evidence(
    per_product: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a brand-level evidence dict for verdict_for() from
    successfully-probed per-product reports. Sums totals, unions top
    retailers across products (ranked by total times_cited), picks
    the highest-scoring product's competitive_pressure framing as
    representative, and computes a brand-wide failed_attribution_query
    sample.

    Used by `_aggregate_brand_scores` so the brand-level
    `verdict_for(...)` call gets evidence — without it the explanation
    falls back to the generic non-evidence prose, which reads
    template-y to merchants. This is the brand-level analogue of the
    per-product evidence built in `build_structured_report`.
    """
    if not per_product:
        return {}

    total_runs = 0
    total_cited = 0
    retailer_count: Counter = Counter()
    cited_host_meta: Dict[str, Dict[str, Any]] = {}
    failed_query_sample: List[str] = []
    category_scores: List[int] = []
    framing: Optional[str] = None
    framing_score = -1

    def _record_cited_host(entry: Dict[str, Any]) -> None:
        host = entry.get("host")
        count = int(entry.get("times_cited") or 0)
        if host and count:
            retailer_count[host] += count
            classification = classify_host(host)
            cited_host_meta[host] = {
                "host": host,
                "type": (
                    entry.get("type")
                    or classification.get("type")
                    or "unclassified"
                ),
                "confidence": (
                    entry.get("confidence")
                    or classification.get("confidence")
                    or "fallback"
                ),
            }

    for p in per_product:
        attr = p.get("attribution") or {}
        total_runs += int(attr.get("runs") or 0)
        total_cited += int(attr.get("merchant_cited_runs") or 0)

        # Walk both attribution.competitor_hosts (buyer-intent probe)
        # and category_visibility.retailer_hosts (category probe). The
        # union surfaces hosts that win across either probe type.
        for entry in attr.get("competitor_hosts") or []:
            _record_cited_host(entry)
        cat = p.get("category_visibility") or {}
        if cat:
            cs = cat.get("score")
            if isinstance(cs, (int, float)):
                category_scores.append(int(cs))
            for entry in cat.get("retailer_hosts") or []:
                _record_cited_host(entry)

        # Failed-query sample: pull `query` from attribution.queries
        # entries where merchant URL was missing (self_report_yes False).
        for q_row in attr.get("queries") or []:
            if not q_row.get("self_report_yes"):
                q = (q_row.get("query") or "").strip()
                if q and q not in failed_query_sample:
                    failed_query_sample.append(q)
                if len(failed_query_sample) >= 5:
                    break

        # Pick the competitive_pressure.framing from the highest-
        # scoring product (most-grounded data → least noise in framing).
        cp = p.get("competitive_pressure") or {}
        cp_framing = cp.get("framing")
        product_score = int((p.get("verdict") or {}).get("attribution_score") or 0)
        if cp_framing and product_score > framing_score:
            framing = cp_framing
            framing_score = product_score

    avg_category = (
        sum(category_scores) / len(category_scores)
        if category_scores else None
    )
    # gap_pct is per-tier semantics: how far category visibility leads
    # attribution. Computed at the brand level when we have both.
    gap_pct: Optional[int] = None
    if avg_category is not None and total_runs > 0:
        avg_attribution_pct = (
            int(round((total_cited / total_runs) * 100)) if total_runs else 0
        )
        gap_pct = max(0, int(avg_category) - avg_attribution_pct)

    top_cited_hosts: List[Dict[str, Any]] = []
    for host, count in retailer_count.most_common(5):
        entry = dict(cited_host_meta.get(host) or {"host": host})
        entry["times_cited"] = count
        if not _is_cdn_cited_host(entry):
            top_cited_hosts.append(entry)

    return {
        "attribution_runs_total": total_runs,
        "merchant_cited_runs": total_cited,
        "top_retailers": [h["host"] for h in _retail_cited_hosts(top_cited_hosts)],
        "top_cited_hosts": top_cited_hosts,
        "competitive_pressure_framing": framing,
        "category_score": int(avg_category) if avg_category is not None else None,
        "gap_pct": gap_pct,
        "failed_attribution_query_sample": failed_query_sample[:5],
    }


def _aggregate_brand_scores(per_product: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Average each score across successful per-product reports +
    derive a brand-level verdict. Returns empty-shape dict (with
    None values) when per_product is empty so the caller can render
    a clean "all failed" state."""
    if not per_product:
        return {
            "avg_visibility": None,
            "avg_attribution": None,
            "avg_category_visibility": None,
            "brand_verdict_label": None,
            "brand_verdict_explanation": (
                "No products were successfully probed; can't aggregate."
            ),
        }
    visibility_vals = [
        r["verdict"]["visibility_score"]
        for r in per_product
        if r.get("verdict", {}).get("visibility_score") is not None
    ]
    attribution_vals = [
        r["verdict"]["attribution_score"]
        for r in per_product
        if r.get("verdict", {}).get("attribution_score") is not None
    ]
    category_vals = [
        r["verdict"]["category_visibility_score"]
        for r in per_product
        if r.get("verdict", {}).get("category_visibility_score") is not None
    ]

    avg_visibility = (
        sum(visibility_vals) / len(visibility_vals) if visibility_vals else None
    )
    avg_attribution = (
        sum(attribution_vals) / len(attribution_vals) if attribution_vals else None
    )
    avg_category_visibility = (
        sum(category_vals) / len(category_vals) if category_vals else None
    )

    # Brand-level verdict uses the same thresholds as the per-product
    # verdict_for, but on AVERAGED scores so a brand with one strong
    # SKU + four weak ones gets correctly flagged as PARTIAL/MISATTRIBUTED.
    if avg_visibility is None or avg_attribution is None:
        brand_label = None
        brand_explanation = "Insufficient data to render a brand verdict."
    else:
        # PR-A regression fix: aggregate per-product evidence so the
        # brand-level explanation is data-bound (not the generic
        # fallback). Sums totals, unions top retailers across products
        # ranked by frequency, picks the highest-scoring product's
        # competitive_pressure framing as the representative one.
        brand_evidence = _aggregate_brand_verdict_evidence(per_product)
        brand_label, brand_explanation = verdict_for(
            int(avg_visibility),
            int(avg_attribution),
            category_visibility_score=(
                int(avg_category_visibility)
                if avg_category_visibility is not None else None
            ),
            evidence=brand_evidence,
        )

    return {
        "avg_visibility": round(avg_visibility, 1) if avg_visibility is not None else None,
        "avg_attribution": round(avg_attribution, 1) if avg_attribution is not None else None,
        "avg_category_visibility": (
            round(avg_category_visibility, 1) if avg_category_visibility is not None else None
        ),
        "brand_verdict_label": brand_label,
        "brand_verdict_explanation": brand_explanation,
    }


def _brand_to_candidate_hosts(brand: Optional[str]) -> List[str]:
    """Best-effort brand → likely-D2C-host mapping. "Beauty of Joseon"
    → ["beautyofjoseon.com", "beautyofjoseon.co"]. Used as a filter
    against the competitor rollup so the merchant's own D2C site
    doesn't show up as a "competitor" when the audit probed a
    different host shape (e.g. external_seed uses agent.pivota.cc
    as merchant_host, leaving the brand's real .com unprotected).

    Conservative — covers the most common shape (concatenated brand
    words + .com / .co / .co.uk). Doesn't try to enumerate every
    possible D2C domain shape (e.g. hyphens, prefixes like "get-",
    .net, .store). The cost of a false-positive exclude is small
    (one ranked entry dropped); the cost of a false-positive include
    is BD prose calling the merchant their own competitor.
    """
    if not brand:
        return []
    normalized = "".join(
        ch for ch in brand.strip().lower()
        if ch.isalnum()
    )
    if not normalized:
        return []
    return [
        f"{normalized}.com",
        f"{normalized}.co",
        f"{normalized}.co.uk",
        f"www.{normalized}.com",
    ]


def _own_host_set(
    merchant_brand: Optional[str],
    merchant_domain: Optional[str],
) -> set:
    """Build the set of hosts that are the merchant's own — to be
    excluded from the competitor rollup. Combines:
      - Explicit `merchant_domain` (when set by the brand-report
        caller; the strongest signal).
      - Brand-name-derived candidate hosts (see `_brand_to_candidate_hosts`).
    Both normalized lowercase, stripping leading 'www.'.
    """
    own: set = set()
    if merchant_domain:
        d = merchant_domain.strip().lower().lstrip(".")
        if d.startswith("www."):
            d = d[4:]
        if d:
            own.add(d)
    for candidate in _brand_to_candidate_hosts(merchant_brand):
        c = candidate.lstrip(".")
        if c.startswith("www."):
            c = c[4:]
        own.add(c)
    return own


def _aggregate_brand_competitors(
    per_product: List[Dict[str, Any]],
    *,
    merchant_brand: Optional[str] = None,
    merchant_domain: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Walk per-product attribution.competitor_hosts AND
    category_visibility.retailer_hosts, sum cross-probe + cross-product,
    return ranked top 15. The aggregate "who's stealing your AI traffic
    across the whole brand" view — pitch-relevant because BD wants to
    call out "Sephora captures 12 / 15 of your queries across these 5 SKUs".

    Q-P1-3: pre-fix this only walked attribution.competitor_hosts, which
    drops category-probe peer brands entirely. The Winona prod artifact
    surfaced "verywellfit.com" and "shape.com" as category-probe peers
    that never made it into the brand rollup because the buyer-intent
    probe (attribution.competitor_hosts) didn't separately capture them.

    N5 PR-7: external_seed audits use `agent.pivota.cc` as
    `merchant_host` (Pivota canonical PDP), which leaves the brand's
    actual D2C domain (e.g. `beautyofjoseon.com`) unprotected — it
    surfaced in the rollup as a "possible_peer_host" competitor. Fix:
    accept `merchant_brand` + `merchant_domain` and exclude any host
    matching the merchant's own brand-derived candidates or explicit
    domain. Internal merchants where `merchant_host` already matched
    the brand were never affected by this bug.

    Output shape per host:
      {
        host, times_cited,                     # back-compat
        buyer_intent_cited, category_cited,    # per-probe breakdown
        source,                                # "buyer_intent" | "category_only" | "both"
        confidence,                            # see below
      }

    Confidence tiering:
      - "verified_competitor": appears in BOTH probes. Strongest signal —
        named by a buyer-intent query AND captured category visibility.
      - "grounded_competitor": appears only in attribution.competitor_hosts.
        Direct buyer-intent capture, no category context.
      - "possible_peer_host": appears only in category_visibility.retailer_hosts.
        Category peer, but no direct buyer-intent capture — call out as
        peer/context, not verified competitor.

    Total `times_cited` sums across both probes for ranking.
    """
    own_hosts = _own_host_set(merchant_brand, merchant_domain)

    def _is_own_host(h: str) -> bool:
        if not h:
            return False
        normalized = h.strip().lower().lstrip(".")
        if normalized.startswith("www."):
            normalized = normalized[4:]
        return normalized in own_hosts

    buyer_intent_count: Counter = Counter()
    category_count: Counter = Counter()
    for product in per_product:
        attr = product.get("attribution") or {}
        for entry in attr.get("competitor_hosts") or []:
            host = (entry.get("host") or "").strip().lower()
            count = entry.get("times_cited") or 0
            if host and count and not _is_own_host(host):
                buyer_intent_count[host] += int(count)
        cat = product.get("category_visibility") or {}
        for entry in cat.get("retailer_hosts") or []:
            host = (entry.get("host") or "").strip().lower()
            count = entry.get("times_cited") or 0
            if host and count and not _is_own_host(host):
                category_count[host] += int(count)

    out: List[Dict[str, Any]] = []
    for host in set(buyer_intent_count) | set(category_count):
        bi = buyer_intent_count.get(host, 0)
        cv = category_count.get(host, 0)
        if bi and cv:
            confidence = "verified_competitor"
            source = "both"
        elif bi:
            confidence = "grounded_competitor"
            source = "buyer_intent"
        else:
            confidence = "possible_peer_host"
            source = "category_only"
        out.append({
            "host": host,
            "times_cited": bi + cv,
            "buyer_intent_cited": bi,
            "category_cited": cv,
            "source": source,
            "confidence": confidence,
        })

    # Rank by combined times_cited desc, then by host to stabilize ties.
    out.sort(key=lambda e: (-e["times_cited"], e["host"]))
    return out[:15]


def _normalize_competitor_name(name: str) -> str:
    """Canonical key for a competitor brand name — lowercased,
    alphanumeric-only. "Drunk Elephant" → "drunkelephant",
    "PEACH & LILY" → "peachlily". Used to join the three competitor
    surfaces (host rollup / category peers / social benchmark) which
    all key competitors differently."""
    return "".join(ch for ch in (name or "").strip().lower() if ch.isalnum())


def _reconcile_competitor_entities(
    *,
    cross_product_competitors: List[Dict[str, Any]],
    per_product: List[Dict[str, Any]],
    social_intelligence: Optional[Dict[str, Any]],
    merchant_brand: Optional[str],
) -> List[Dict[str, Any]]:
    """P2 (post-#525 codex review): the audit surfaces competitors in
    THREE places with no shared identity —

      1. `cross_product_competitors` — host-keyed (`sephora.com`),
         from `_aggregate_brand_competitors`.
      2. per-product `competitive_pressure.peers_named` — brand-name-
         keyed (`Sephora`), plus `peers_with_first_party_visibility`.
      3. `social_intelligence.competitor_presence` /
         `competitive_comparison` — brand-name-keyed social metrics.

    A BD operator reading the report gets the same competitor three
    times with no reconciliation. This builds an ADDITIVE derived
    view — `competitor_entities` — that joins all three by normalized
    brand name. It does NOT replace the raw surfaces (consumers that
    read them still work); it's the one coherent "what do we know
    about competitor X" rollup.

    Brand-centric: each entity is a named competitor brand (that's how
    BD thinks). Host-rollup entries that match no named brand stay in
    `cross_product_competitors` untouched — not duplicated here. The
    host↔brand join reuses `_brand_matches_host_segment` (the PR-7/N6
    matcher), so it inherits the tightened all-words + coverage rule
    and the merchant's-own-domain exclusion.
    """
    # 1. Seed entities from category peers (the canonical brand list),
    #    summing category mention counts across products.
    entities: Dict[str, Dict[str, Any]] = {}
    for product in per_product or []:
        cp = product.get("competitive_pressure") or {}
        for peer in cp.get("peers_named") or []:
            name = (peer.get("name") or "").strip()
            key = _normalize_competitor_name(name)
            if not key:
                continue
            ent = entities.setdefault(key, {
                "canonical_name": key,
                "display_name": name,
                "category_mentions": 0,
                "known_hosts": [],
                "first_party_visible": False,
                "social": None,
                "social_comparison": None,
                "seen_in": [],
            })
            ent["category_mentions"] += int(peer.get("times_cited") or 0)
            if "category_peers" not in ent["seen_in"]:
                ent["seen_in"].append("category_peers")
        # first-party visibility flag from the same block.
        for fp in cp.get("peers_with_first_party_visibility") or []:
            key = _normalize_competitor_name(fp.get("brand") or "")
            if key in entities:
                entities[key]["first_party_visible"] = True

    # 2. Join host-rollup entries: a host belongs to an entity when the
    #    entity's brand name matches the host's first segment.
    for host_entry in cross_product_competitors or []:
        host = (host_entry.get("host") or "").strip().lower()
        if not host:
            continue
        first_segment = host.split(".")[0]
        for key, ent in entities.items():
            if _brand_matches_host_segment(ent["display_name"], first_segment):
                ent["known_hosts"].append({
                    "host": host,
                    "confidence": host_entry.get("confidence"),
                    "times_cited": host_entry.get("times_cited"),
                    "source": host_entry.get("source"),
                })
                if "host_rollup" not in ent["seen_in"]:
                    ent["seen_in"].append("host_rollup")
                break  # one host → at most one entity

    # 3. Join social benchmark — competitor_presence + competitive_comparison.
    si = social_intelligence or {}
    comp_presence = si.get("competitor_presence") or {}
    for comp_name, presence in comp_presence.items():
        key = _normalize_competitor_name(comp_name)
        ent = entities.get(key)
        if ent is None:
            # Social named a competitor the category-peer list didn't —
            # surface it rather than drop it.
            ent = entities.setdefault(key or comp_name, {
                "canonical_name": key or comp_name,
                "display_name": comp_name,
                "category_mentions": 0,
                "known_hosts": [],
                "first_party_visible": False,
                "social": None,
                "social_comparison": None,
                "seen_in": [],
            })
        ent["social"] = presence
        if "social_benchmark" not in ent["seen_in"]:
            ent["seen_in"].append("social_benchmark")
    for comp in si.get("competitive_comparison") or []:
        if not isinstance(comp, dict):
            continue
        key = _normalize_competitor_name(comp.get("brand") or "")
        ent = entities.get(key)
        if ent is not None:
            ent["social_comparison"] = comp
            if "social_benchmark" not in ent["seen_in"]:
                ent["seen_in"].append("social_benchmark")

    # Rank: most-corroborated first (seen in the most surfaces), then
    # by category mention count.
    out = list(entities.values())
    out.sort(key=lambda e: (-len(e["seen_in"]), -e["category_mentions"], e["canonical_name"]))
    return out


# ---------------------------------------------------------------------------
# Industry context — hardcoded category facts surfaced in the BD report so a
# raw "your visibility is 33%" reads as "...in a channel that's 12% of D2C
# beauty traffic and growing 40% YoY". Keep these conservative — they're
# defensible BD numbers, not investor-deck flourishes. Update by hand
# (low-frequency edits, ~1×/quarter); the alternative of pulling from a CMS
# adds ops surface for no observable gain.
# ---------------------------------------------------------------------------

_INDUSTRY_CONTEXT_DEFAULT: Dict[str, Any] = {
    "category": "default",
    "ai_search_share_pct": None,
    "ai_search_growth_yoy_pct": None,
    "forward_projection": None,
    "blurb": (
        "AI shopping (ChatGPT / Gemini / Perplexity) is a fast-growing "
        "discovery channel for D2C brands. Merchants without AI-channel "
        "attribution today are losing share to competitors who do."
    ),
    # PR-7d defaults — null for unknown verticals
    "market_size_billions_usd": None,
    "market_size_year": None,
    "growth_horizon_years": None,
    "sub_category_trends": [],
    "comparison_to_other_verticals": None,
}


# PR-7d: industry vertical depth — market sizing, sub-category trends,
# and vertical comparisons. The polished Grüns report led with
# "wellness gummies category was ~$8B globally in 2024 and projected
# ~14% YoY through 2028" — that level of specificity needs structured
# data the system can surface, not boilerplate.
#
# Numbers are conservative third-party-defensible estimates from
# Statista / IBISWorld / Grand View Research / industry analyst
# reports. Update as those reports refresh (typically annual).
#
# `sub_category_trends` enables form-factor-aware narrative — e.g.,
# when the merchant's product is classified as a wellness gummy,
# the report can quote the gummy-specific growth rate, not just
# the parent category's.
_INDUSTRY_VERTICAL_DEPTH: Dict[str, Dict[str, Any]] = {
    "beauty": {
        "market_size_billions_usd": 580,
        "market_size_year": 2024,
        "growth_horizon_years": "2024-2028",
        "sub_category_trends": [
            {"sub": "Korean beauty (K-beauty)", "growth_pct": 9, "why": "AI-channel-amplified word of mouth + ingredient-deep editorial"},
            {"sub": "skinimalism / multi-use serums", "growth_pct": 12, "why": "ingredient simplicity reads well in AI comparison answers"},
            {"sub": "clean / fragrance-free", "growth_pct": 15, "why": "AI assistants surface ingredient-conscious editorial frames"},
        ],
        "comparison_to_other_verticals": (
            "Beauty AI-search penetration (~12%) trails consumer "
            "electronics (~14%) but leads fashion (~8%) and home (~7%)."
        ),
    },
    "fashion": {
        "market_size_billions_usd": 685,
        "market_size_year": 2024,
        "growth_horizon_years": "2024-2028",
        "sub_category_trends": [
            {"sub": "loungewear + sleepwear", "growth_pct": 11, "why": "AI editorial pickup of WFH adjacent categories"},
            {"sub": "athleisure", "growth_pct": 8, "why": "broad cross-vertical mention surface"},
        ],
        "comparison_to_other_verticals": (
            "Fashion AI-search penetration (~8%) lags behind beauty "
            "and consumer electronics; visual-style queries are the "
            "fastest-growing query class."
        ),
    },
    "fitness": {
        "market_size_billions_usd": 96,
        "market_size_year": 2024,
        "growth_horizon_years": "2024-2028",
        "sub_category_trends": [
            {"sub": "home gym equipment", "growth_pct": 7, "why": "Post-pandemic baseline normalization"},
            {"sub": "wearables / smart fitness", "growth_pct": 18, "why": "Crosses into electronics vertical for grounding"},
        ],
        "comparison_to_other_verticals": (
            "Fitness equipment AI-search penetration (~9%) outpaces "
            "fashion but lags wellness/supplements (~11%); "
            "spec-comparison queries skew toward grounded LLM use."
        ),
    },
    "wellness": {
        "market_size_billions_usd": 6_500,        # global wellness ($)
        "market_size_year": 2024,
        "growth_horizon_years": "2024-2028",
        "sub_category_trends": [
            {
                "sub": "wellness gummies",
                "growth_pct": 14,
                "why": (
                    "Form-factor preference shift away from powders + "
                    "capsules; AI assistants surface gummy-specific "
                    "category roundups (Forbes 'Best Green Gummies', "
                    "etc.) as a distinct subcategory."
                ),
            },
            {"sub": "daily greens powders", "growth_pct": 11, "why": "AG1-driven category education + value comparisons"},
            {"sub": "longevity / NAD+ / nootropics", "growth_pct": 22, "why": "AI assistants are well-suited to comparison queries"},
            {"sub": "magnesium + sleep stack", "growth_pct": 16, "why": "Sleep-focused AI editorial pickup"},
        ],
        "comparison_to_other_verticals": (
            "Wellness / supplements AI-search penetration (~11%) is "
            "among the fastest-growing in consumer verticals — only "
            "consumer electronics (~14%) moves faster. The category "
            "is uniquely well-suited to AI grounded retrieval because "
            "comparison and ingredient deep-dive queries dominate "
            "buyer research."
        ),
    },
    "food_bev": {
        "market_size_billions_usd": 410,
        "market_size_year": 2024,
        "growth_horizon_years": "2024-2028",
        "sub_category_trends": [
            {"sub": "specialty coffee", "growth_pct": 9, "why": "Strong editorial review surface (Wirecutter, Strategist)"},
            {"sub": "functional beverages (kombucha, adaptogens)", "growth_pct": 14, "why": "Wellness-adjacent crossover"},
        ],
        "comparison_to_other_verticals": (
            "Food/beverage AI-search penetration (~6%) is among the "
            "lower verticals — local + delivery context limits "
            "grounded-retrieval usage compared to shippable categories."
        ),
    },
    "home": {
        "market_size_billions_usd": 720,
        "market_size_year": 2024,
        "growth_horizon_years": "2024-2028",
        "sub_category_trends": [
            {"sub": "smart home + appliances", "growth_pct": 12, "why": "Crosses into electronics; benefits from spec comparison"},
            {"sub": "sustainable / non-toxic home goods", "growth_pct": 15, "why": "Editorial pickup of clean-living roundups"},
        ],
        "comparison_to_other_verticals": (
            "Home AI-search penetration (~7%) is mid-pack; "
            "higher-consideration purchases ($100+) skew more toward "
            "AI-assisted research than impulse categories."
        ),
    },
    "electronics": {
        "market_size_billions_usd": 1_350,
        "market_size_year": 2024,
        "growth_horizon_years": "2024-2028",
        "sub_category_trends": [
            {"sub": "audio (earbuds, headphones)", "growth_pct": 10, "why": "Mature AI-channel category; deep editorial"},
            {"sub": "wearables", "growth_pct": 18, "why": "Health + tracking spec comparisons drive AI usage"},
            {"sub": "smart-home hubs", "growth_pct": 14, "why": "Spec-heavy + ecosystem comparison queries"},
        ],
        "comparison_to_other_verticals": (
            "Consumer electronics AI-search penetration (~14%) is the "
            "highest among consumer verticals — spec-heavy products "
            "match AI assistants' summarization strength."
        ),
    },
}


def _enrich_industry_context_with_depth(
    base_context: Dict[str, Any],
) -> Dict[str, Any]:
    """PR-7d: merge market-sizing + sub-category + vertical-comparison
    fields into the base industry context. Returns a NEW dict so
    callers can mutate without affecting the registry."""
    out = dict(base_context)
    category = (base_context.get("category") or "").lower()
    depth = _INDUSTRY_VERTICAL_DEPTH.get(category)
    if depth:
        out["market_size_billions_usd"] = depth.get("market_size_billions_usd")
        out["market_size_year"] = depth.get("market_size_year")
        out["growth_horizon_years"] = depth.get("growth_horizon_years")
        out["sub_category_trends"] = list(depth.get("sub_category_trends") or [])
        out["comparison_to_other_verticals"] = depth.get(
            "comparison_to_other_verticals"
        )
    else:
        out.setdefault("market_size_billions_usd", None)
        out.setdefault("market_size_year", None)
        out.setdefault("growth_horizon_years", None)
        out.setdefault("sub_category_trends", [])
        out.setdefault("comparison_to_other_verticals", None)
    return out

# `forward_projection` is the per-category 24-month projection for AI-
# channel-native commerce share. Currently populated for beauty only —
# the V1 BD pitch focuses on K-beauty merchants and beauty has the most
# defensible secondary-source numbers. Other categories get None and the
# renderer skips the projection line; populate them as BD validates the
# framing on additional verticals.
_INDUSTRY_CONTEXT_BY_CATEGORY: Dict[str, Dict[str, Any]] = {
    "beauty": {
        "category": "beauty",
        "ai_search_share_pct": 12,
        "ai_search_growth_yoy_pct": 40,
        "forward_projection": (
            "By 2028, AI-channel-native commerce is projected to reach "
            "25-30% of D2C beauty discovery. Brands without an AI-native "
            "transaction surface (canonical PDP + in-chat checkout) will "
            "be retailer-dependent for that share — paying reseller margin "
            "on traffic that originated in the AI channel."
        ),
        "blurb": (
            "AI shopping is ~12% of new D2C beauty traffic and growing ~40% "
            "YoY (2025-2026). Beauty is one of the highest-AI-affinity "
            "categories — consumers ask AI assistants about ingredients, "
            "skin-type fit, and dupe alternatives — so brands without AI-"
            "channel attribution are losing the fastest in this segment."
        ),
    },
    "fashion": {
        "category": "fashion",
        "ai_search_share_pct": 8,
        "ai_search_growth_yoy_pct": 35,
        "forward_projection": None,
        "blurb": (
            "AI shopping is ~8% of D2C fashion traffic and growing ~35% YoY. "
            "Visual + style queries (\"summer dress under $80\") shift "
            "increasingly to AI assistants; merchants without grounded "
            "attribution lose discovery to retail aggregators."
        ),
    },
    "fitness": {
        "category": "fitness",
        "ai_search_share_pct": 9,
        "ai_search_growth_yoy_pct": 32,
        "forward_projection": None,
        "blurb": (
            "AI shopping is ~9% of D2C fitness equipment traffic and growing "
            "~32% YoY. Consumers research equipment + accessories through "
            "AI assistants before purchase; not appearing in those answers "
            "is invisible top-of-funnel."
        ),
    },
    "wellness": {
        "category": "wellness",
        "ai_search_share_pct": 11,
        "ai_search_growth_yoy_pct": 36,
        "forward_projection": None,
        "blurb": (
            "AI shopping is ~11% of D2C wellness / supplements traffic and "
            "growing ~36% YoY — among the fastest-growing verticals. "
            "Consumers ask AI assistants comparison questions (\"is it worth "
            "it\", \"best option under $X\", ingredient and efficacy "
            "deep-dives) before buying daily-use supplements; brands without "
            "grounded attribution lose the comparison-shopping funnel to "
            "retailer and editorial roundups."
        ),
    },
    "food_bev": {
        "category": "food_bev",
        "ai_search_share_pct": 6,
        "ai_search_growth_yoy_pct": 28,
        "forward_projection": None,
        "blurb": (
            "AI shopping is ~6% of D2C food/beverage traffic, growing ~28% "
            "YoY. Specialty / direct-from-maker brands gain disproportionately "
            "from grounded LLM citations."
        ),
    },
    "home": {
        "category": "home",
        "ai_search_share_pct": 7,
        "ai_search_growth_yoy_pct": 30,
        "forward_projection": None,
        "blurb": (
            "AI shopping is ~7% of D2C home/decor traffic and growing ~30% "
            "YoY. Higher-consideration purchases ($100+) skew toward AI-"
            "assisted research; missing the AI channel = missing pre-purchase "
            "evaluation."
        ),
    },
    "electronics": {
        "category": "electronics",
        "ai_search_share_pct": 14,
        "ai_search_growth_yoy_pct": 38,
        "forward_projection": None,
        "blurb": (
            "AI shopping is ~14% of D2C electronics traffic and growing "
            "~38% YoY — the highest among consumer verticals. Spec-heavy "
            "products are exactly what AI assistants are best at "
            "summarizing; merchants without grounded attribution lose the "
            "comparison-shopping funnel."
        ),
    },
}

# Crude classifier from product_type / vendor strings to a known category
# bucket. Conservative — when in doubt, fall through to default.
_CATEGORY_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("beauty", [
        "serum", "essence", "tonic", "ampoule", "cream", "moisturizer",
        "sunscreen", "spf", "blush", "lipstick", "lip oil", "lip balm",
        "mask", "patch", "fragrance", "perfume", "eau de", "foundation",
        "concealer", "brush", "skincare", "haircare", "shampoo",
        "conditioner",
    ]),
    ("fashion", [
        "shirt", "tee", "dress", "jacket", "coat", "pants", "jeans",
        "sneaker", "shoe", "bag", "handbag", "backpack", "scarf", "hat",
        # Sleepwear / loungewear / intimates — same vertical for the
        # purpose of industry context (D2C apparel, retailer-mediated
        # discovery), so they share the fashion blurb until BD validates
        # a sleepwear-specific projection.
        "sleepwear", "pajama", "pyjama", "robe", "loungewear",
        "nightgown", "lingerie", "intimates", "underwear", "bralette",
        "swimwear", "swimsuit", "bikini",
    ]),
    # Wellness BEFORE fitness so supplement/vitamin/greens keywords
    # route to the wellness blurb (not the equipment-focused fitness
    # one). Order matters: first match wins.
    ("wellness", [
        "supplement", "supplements", "protein", "vitamin", "vitamins",
        "creatine", "greens", "multivitamin", "probiotic", "prebiotic",
        "collagen", "adaptogen", "nootropic", "magnesium", "omega",
        "electrolyte", "electrolytes", "gummies", "gummy", "powder",
        "wellness", "nutrition", "nutraceutical",
    ]),
    ("fitness", [
        "yoga", "mat", "dumbbell", "treadmill", "fitness", "workout",
        "gym", "weights", "barbell", "kettlebell", "exercise bike",
        "pilates",
    ]),
    ("food_bev", [
        "coffee", "tea", "snack", "bar", "chocolate", "wine", "beer",
        "cocktail", "kombucha", "juice", "syrup", "honey", "olive oil",
    ]),
    ("home", [
        "candle", "rug", "throw", "pillow", "lamp", "vase", "bowl",
        "plate", "linen", "duvet", "decor",
    ]),
    ("electronics", [
        "phone", "laptop", "headphone", "earbud", "speaker", "camera",
        "tablet", "monitor", "router", "tv", "watch", "smartwatch",
        "console",
    ]),
]


def _industry_context_for(
    product_type: Optional[str],
    product_vendor: Optional[str] = None,
    product_title: Optional[str] = None,
) -> Dict[str, Any]:
    """Look up category context from product attributes. Inspects
    product_type first (most reliable), falls back to title / vendor
    keywords for products where product_type is missing or generic.

    PR-7d: returns the depth-enriched context (market size +
    sub-category trends + vertical comparison) so renderers can
    quote specific market sizing instead of generic share% only.
    """
    haystacks = [
        (product_type or "").lower(),
        (product_title or "").lower(),
        (product_vendor or "").lower(),
    ]
    haystack = " ".join(s for s in haystacks if s)
    if not haystack:
        return _enrich_industry_context_with_depth(_INDUSTRY_CONTEXT_DEFAULT)
    for category, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in haystack:
                return _enrich_industry_context_with_depth(
                    _INDUSTRY_CONTEXT_BY_CATEGORY[category]
                )
    return _enrich_industry_context_with_depth(_INDUSTRY_CONTEXT_DEFAULT)


# ---------------------------------------------------------------------------
# Action items — rule-based parser of the failed queries and competitor
# hosts to produce 3-5 specific, merchant-named actions instead of generic
# verdict prose. The intent is BD-pitch utility: every action references
# this merchant's actual data (a query they failed, a competitor that
# captured them) so the rep can read straight off the page. We deliberately
# avoid LLM-generated copywriting here — that would re-introduce
# hallucination + cost concerns the rest of the pipeline is fighting.
# ---------------------------------------------------------------------------


def _truncate_query(q: str, n: int = 60) -> str:
    s = (q or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# PR-8b — Recommendation engine v2 metadata enrichment
# ---------------------------------------------------------------------------
# Pre-PR action items shipped with severity + title + body + optional
# evidence only. Polished reports add execution metadata: who owns
# the action, what KPI tracks it, what outcome to expect, what phase
# the action belongs to. This metadata isn't generated per-action by
# hand (would require touching every items.append call across the
# 5 verdict-tier branches + playbook engine); instead, it's derived
# from action lever + title + severity by `_enrich_action_items_v2`
# at the END of the action-generation pipeline.
#
# `phase` (week_1_to_4 / week_4_to_12 / week_12_to_24) feeds into
# PR-8c (implementation roadmap generator) which groups actions
# into phases for the rendered roadmap table.
#
# `depends_on` is intentionally left null in v1 — auto-detecting
# action dependencies is hard and PR-8c's phased ordering already
# implies most dependencies. Renderer can wire dependency arrows
# when a future PR populates the field explicitly.

# Maps action `lever` (set by playbook engine for per-host actions) to
# the team that owns execution. Strategic actions (no lever set) fall
# through to the title/keyword-based heuristic below.
_OWNER_BY_LEVER: Dict[str, str] = {
    "editorial_outreach": "merchant_brand_team",
    "wholesale_onboarding": "merchant_growth_team",
    "creator_partnership": "merchant_brand_team",
    "marketplace_listing": "merchant_growth_team",
    "research": "joint",
}


def _v2_metadata_for_action(item: Dict[str, Any]) -> Dict[str, Any]:
    """Derive owner / kpi_to_track / expected_outcome / phase for a
    single action item. Pure function — keyed on existing fields
    (severity, title, lever, evidence). No LLM call.
    """
    severity = (item.get("severity") or "medium").lower()
    title = (item.get("title") or "").lower()
    lever = (item.get("lever") or "").lower()

    # Owner: prefer explicit lever mapping (per-host playbook actions
    # carry one), then keyword heuristic on the title.
    owner = _OWNER_BY_LEVER.get(lever)
    if owner is None:
        if any(s in title for s in (
            "index", "search console", "sitemap", "schema",
            "canonical", "url inspection",
        )):
            owner = "pivota_ops"
        elif any(s in title for s in (
            "pitch", "outreach", "editorial", "press",
            "content brief", "creator",
        )):
            owner = "merchant_brand_team"
        elif any(s in title for s in (
            "monitor", "drift", "rerun", "re-audit",
            "track", "watch",
        )):
            owner = "joint"
        elif any(s in title for s in (
            "wholesale", "marketplace", "listing", "retail",
        )):
            owner = "merchant_growth_team"
        elif any(s in title for s in ("investigate", "research")):
            owner = "joint"
        else:
            owner = "joint"

    # Phase: severity + lever drive the time-bucket assignment.
    # Critical actions land in week_1_to_4 (immediate); medium-severity
    # editorial pitches in week_4_to_12 (publication cycle latency);
    # low-severity monitoring in week_12_to_24 (ongoing cadence).
    if severity == "critical":
        phase = "week_1_to_4"
    elif severity == "high":
        phase = "week_1_to_4" if owner == "pivota_ops" else "week_4_to_12"
    elif severity == "medium":
        phase = "week_4_to_12"
    else:  # low
        phase = "week_12_to_24"

    # KPI + expected outcome: title-keyword-driven defaults. Keep
    # these short and concrete — renderer surfaces them as a single-
    # line "What to track" + "Expected outcome" pair.
    kpi_to_track: Optional[str] = None
    expected_outcome: Optional[str] = None
    if "index" in title or "search console" in title or "sitemap" in title:
        kpi_to_track = "Number of canonical PDPs indexed by Google"
        expected_outcome = (
            "First grounded citations of the merchant URL within "
            "30-60 days; full PDP citation share in 60-90 days."
        )
    elif "pitch" in title or "outreach" in title or "editorial" in title:
        kpi_to_track = (
            "Editorial inclusion confirmation + Google index date "
            "of the published page"
        )
        expected_outcome = (
            "New citation propagates to grounded LLM answers "
            "within 4-8 weeks of publication."
        )
    elif "content brief" in title:
        kpi_to_track = "Briefs delivered to merchant content team"
        expected_outcome = (
            "Brief becomes published content; new content indexed; "
            "reflected in next-cycle re-audit."
        )
    elif "wholesale" in title or "marketplace" in title:
        kpi_to_track = "Listing approval + first cited query"
        expected_outcome = (
            "Listing live within 6-12 weeks; AI grounded answers "
            "reflect new listing within an additional 4-8 weeks."
        )
    elif "monitor" in title or "drift" in title or "track" in title:
        kpi_to_track = (
            "Quarterly trend report on AI-channel citation share"
        )
        expected_outcome = (
            "Erosion detected within 30 days; citation drift "
            "surfaced for immediate action."
        )
    elif "investigate" in title:
        kpi_to_track = "Outreach decision (pursue / dismiss)"
        expected_outcome = (
            "Host added to registry or marked as noise; informs "
            "subsequent audits."
        )
    elif "reclaim" in title or "capture" in title:
        kpi_to_track = (
            "First-party citation rate in named-product queries"
        )
        expected_outcome = (
            "1+ of N named-product queries cites merchant URL "
            "directly within 90 days."
        )
    elif "schema" in title or "structured data" in title:
        kpi_to_track = "PDPs validated as schema-clean by Google's tester"
        expected_outcome = (
            "Cleaner schema improves grounded retrieval scoring; "
            "reflected in next-cycle re-audit."
        )
    # The families below previously fell through to None for both fields,
    # leaving the merchant a task with no "what success looks like" line.
    elif "category" in title:
        kpi_to_track = "First-party citation rate on category-level queries"
        expected_outcome = (
            "Your store starts being cited for category questions "
            "(not only branded ones) by the next re-audit."
        )
    elif "close the gap" in title or "inconsistent" in title or "missing" in title:
        kpi_to_track = "Share of buyer-intent queries that cite your URL"
        expected_outcome = (
            "The queries where you're cited inconsistently — or not at "
            "all — become reliable citations by the next re-audit."
        )
    elif "drain" in title or "competitor" in title:
        kpi_to_track = "Competitor citation share on your overlapping queries"
        expected_outcome = (
            "AI cites this competitor instead of you less often on the "
            "queries you both compete for."
        )
    elif "attribution" in title or "funnel" in title:
        kpi_to_track = "Runs where an AI answer cites your URL directly"
        expected_outcome = (
            "Earn your first direct AI-channel citations within "
            "60-90 days instead of losing the funnel to resellers."
        )
    elif "localize" in title or "market" in title:
        kpi_to_track = "Per-market AI visibility score"
        expected_outcome = (
            "Close the AI-visibility gap in the target market within "
            "a re-audit cycle."
        )

    # Fallback: never leave a task without a concrete success line. A
    # merchant-facing task with no expected outcome reads as busywork.
    if kpi_to_track is None or expected_outcome is None:
        kpi_to_track = kpi_to_track or (
            "AI citation rate on the targeted queries (re-audit)"
        )
        expected_outcome = expected_outcome or (
            "Improve how often AI shopping agents recommend your store "
            "for these queries, confirmed on the next re-audit."
        )

    return {
        "owner": owner,
        "phase": phase,
        "kpi_to_track": kpi_to_track,
        "expected_outcome": expected_outcome,
        # depends_on is null in v1; PR-8c roadmap may populate later.
        "depends_on": [],
    }


def _enrich_action_items_v2(
    action_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """PR-8b: enrich every action item with v2 execution metadata
    (owner / phase / kpi_to_track / expected_outcome / depends_on).

    Mutates the items in place + returns them so callers can chain.
    Defensive: any item that already has these fields is left
    unchanged (allows test fixtures + future code to pre-set them).
    """
    for item in action_items or []:
        meta = _v2_metadata_for_action(item)
        for key, value in meta.items():
            # Don't overwrite explicit values (e.g. test fixtures or
            # playbook engine that hand-crafts owner per-host).
            if key not in item or item.get(key) is None:
                item[key] = value
    return action_items


def _generate_action_items(
    *,
    verdict_label: str,
    visibility_runs: List[Dict[str, Any]],
    attribution_runs: List[Dict[str, Any]],
    competitor_hosts: List[Dict[str, Any]],
    merchant_cited_runs: int,
    runs_with_any_citation: int,
    visibility_score: int = 0,
    attribution_score: int = 0,
    category_visibility_score: Optional[int] = None,
    category_retailer_hosts: Optional[List[Dict[str, Any]]] = None,
    category_competitor_brands: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Return a list of 3-5 specific action items. Each item has
    `severity` (critical|high|medium|low), `title`, `body` (merchant-
    facing diagnostic prose, evidence-bound), and optional `evidence`
    (the failed query / cited competitor host that drives this action).

    Q-P1-6 PR-6: every emitted action's `severity` now routes through
    `compute_action_severity` from `services.audit_severity` and
    carries a `severity_reason` token. PR-3 wired the scorer into the
    playbook engine; this function is the brand-level / verdict-tier
    counterpart and was left out of that migration with a TODO. The
    canonical Winona regression case ("Specific queries where your
    URL was missing", base=medium with attribution=0/category=67) now
    upgrades to critical via Rule 2 instead of shipping at medium.

    Pitch-free: no "Pivota's agentic-commerce protocol", no "12% →
    25-30%" macros, no "complementary to existing retail distribution".
    Those live in `_build_what_pivota_changes` exclusively.
    """
    from services.audit_severity import compute_action_severity

    items: List[Dict[str, Any]] = []

    # Failures named in evidence text — shared with verdict_for via
    # module-level helpers so vocabulary stays consistent.
    failed_attribution_queries = _failed_attribution_queries(attribution_runs)
    failed_visibility_queries = _failed_visibility_queries(visibility_runs)
    top_competitor = competitor_hosts[0] if competitor_hosts else None
    top_retailer_hosts = _retail_cited_hosts(category_retailer_hosts)
    top_retailer_names = [
        r["host"] for r in top_retailer_hosts[:3] if r.get("host")
    ]
    top_cited_hosts = _copyworthy_cited_hosts(category_retailer_hosts)
    top_cited_names = [
        r["host"] for r in top_cited_hosts[:3] if r.get("host")
    ]
    retailers_phrase = ", ".join(top_retailer_names)
    cited_hosts_phrase = ", ".join(top_cited_names)
    cited_group_label = _cited_host_group_label(top_cited_hosts)
    attribution_runs_total = len(attribution_runs)

    # Q-P1-6 — shared inputs for compute_action_severity. Computed
    # once per call; each item below routes its hardcoded base
    # severity through the scorer with the per-site refinements
    # (has_failed_query_example flips True only inside the failed-
    # query branch, has_competitors_named varies by site, etc.).
    #
    # `score_gap_pct` is None when category_visibility_score wasn't
    # measured (Phase 2a probe disabled). The scorer treats None as
    # "no gap signal" and falls back to base-severity passthrough.
    _score_gap_pct: Optional[int]
    if category_visibility_score:
        _score_gap_pct = max(0, int(category_visibility_score) - int(attribution_score or 0))
    else:
        _score_gap_pct = None
    _has_failed_attribution_query = bool(failed_attribution_queries)
    # Brand-level actions don't target a specific host, so host_type
    # stays None — the scorer's Rules 1/4/5 (host-gated) won't fire.
    # That's correct: Rules 2/3/7 (gap × failed-query) are the relevant
    # lifters for brand-level remediations.
    _named_competitor_count = (
        (1 if top_competitor and top_competitor.get("host") else 0)
        + len(category_competitor_brands or [])
    )
    _has_named_competitors_any = _named_competitor_count > 0

    def _score(
        *,
        base: str,
        has_failed_query: bool = False,
        has_named_competitors: bool = False,
    ) -> Dict[str, str]:
        """Route a site's authored base severity through the central
        scorer. Returns a dict ready to **spread into items.append:
            {**_score(base="critical", has_failed_query=True), ...}
        Centralizes the boilerplate so each migration site stays one
        line of intent."""
        sev, reason = compute_action_severity(
            score_gap_pct=_score_gap_pct,
            host_type=None,  # brand-level, no specific target host
            has_failed_query_example=has_failed_query,
            has_competitors_named=has_named_competitors,
            base_severity=base,
        )
        return {"severity": sev, "severity_reason": reason}

    # Action 1: severity-stratified headline tied to this merchant's
    # specific failure pattern. All five tiers data-bind off the same
    # extracted variables so the language stays consistent.
    if verdict_label == VERDICT_INVISIBLE:
        body = (
            f"Across {attribution_runs_total} buyer-intent queries we "
            f"tested, AI agents returned zero grounded references to "
            f"your store"
        )
        if cited_hosts_phrase:
            body += (
                f". {cited_hosts_phrase} captured the citation slots "
                "that should have been yours"
            )
        body += (
            ". Grounded LLM citations are downstream of Google's index, "
            "so the typical root cause is that Google hasn't indexed "
            "your canonical PDPs yet. Submit your sitemap.xml to "
            "Search Console, request URL Inspection indexing for your "
            "top SKUs, and re-test in 72 hours."
        )
        items.append({
            **_score(
                base="critical",
                has_failed_query=_has_failed_attribution_query,
                has_named_competitors=_has_named_competitors_any,
            ),
            "title": "Index your canonical PDPs with Google Search Console",
            "body": body,
            "evidence": {
                "queries_tested": attribution_runs_total,
                "top_retailers": top_retailer_names[:5],
                "top_cited_hosts": top_cited_names[:5],
            } if top_cited_names else {"queries_tested": attribution_runs_total},
        })
    elif verdict_label == VERDICT_MISATTRIBUTED:
        if top_retailer_names:
            title = f"Reclaim attribution from {top_retailer_names[0]} and other resellers"
        elif top_cited_names:
            title = f"Reclaim attribution from {top_cited_names[0]} and other cited hosts"
        else:
            title = "Reclaim direct attribution from third-party sources"
        body = (
            f"AI agents recognize your product (visibility "
            f"{visibility_score}/100) but your URL appears in "
            f"{merchant_cited_runs} of {attribution_runs_total} buyer-"
            f"intent queries"
        )
        if cited_hosts_phrase:
            body += (
                f". The remaining {attribution_runs_total - merchant_cited_runs} "
                f"route through {cited_group_label} including {cited_hosts_phrase}"
            )
        body += ". Every cited URL that's not yours is lost organic traffic"
        if top_retailer_names:
            body += " — and a margin hit if the cited path is a reseller"
        body += ". The demand exists; it's just being captured by competitors."
        items.append({
            **_score(
                base="critical",
                has_failed_query=_has_failed_attribution_query,
                has_named_competitors=_has_named_competitors_any,
            ),
            "title": title,
            "body": body,
            "evidence": {
                "merchant_cited_runs": merchant_cited_runs,
                "queries_tested": attribution_runs_total,
                "top_retailers": top_retailer_names[:5],
                "top_cited_hosts": top_cited_names[:5],
            },
        })
    elif verdict_label == VERDICT_CATEGORY_MENTION_NO_FIRST_PARTY:
        body = (
            "Your brand surfaces in category-level AI answers, "
            f"but your own URL appears in {merchant_cited_runs} of "
            f"{attribution_runs_total} buyer-intent queries."
        )
        if cited_hosts_phrase:
            body += (
                f" The cited category sources are {cited_group_label} "
                f"including {cited_hosts_phrase}."
            )
        body += (
            " Convert those category mentions into first-party citation "
            "coverage before optimizing downstream conversion."
        )
        items.append({
            **_score(
                base="critical",
                has_failed_query=_has_failed_attribution_query,
                has_named_competitors=_has_named_competitors_any,
            ),
            "title": "Convert category mentions into first-party attribution",
            "body": body,
            "evidence": {
                "merchant_cited_runs": merchant_cited_runs,
                "queries_tested": attribution_runs_total,
                "top_cited_hosts": top_cited_names[:5],
            },
        })
    elif verdict_label == VERDICT_VIA_RETAILERS:
        # Conditional opener: brands like COSRX with cat=100/attr=33
        # already capture some first-party attribution; lead with the
        # gap, not "every grounded citation".
        if attribution_score == 0:
            opener = (
                "Your brand IS findable in AI-channel category queries "
                "— but every grounded citation routes consumers through "
                "third-party retailers instead of your own URL."
            )
        else:
            gap_pct = max(0, 100 - attribution_score)
            opener = (
                f"Your brand IS findable in AI-channel category queries "
                f"— and you capture {attribution_score}% of buyer-intent "
                f"queries to your own URL today. The remaining "
                f"{gap_pct}% routes through third-party retailers."
            )
        if retailers_phrase:
            opener += (
                f" Top retailers capturing the AI-channel funnel today: "
                f"{retailers_phrase}."
            )
        items.append({
            **_score(
                base="critical",
                has_failed_query=_has_failed_attribution_query,
                # VIA_RETAILERS by definition has top_retailer_hosts populated;
                # those ARE the named competitors capturing the funnel.
                has_named_competitors=(
                    _has_named_competitors_any or bool(top_retailer_hosts)
                ),
            ),
            "title": "Capture the AI-channel funnel that retailers are taking today",
            "body": opener,
            "evidence": (
                {"top_retailer_hosts": [r["host"] for r in top_retailer_hosts[:5] if r.get("host")]}
                if top_retailer_hosts
                else None
            ),
        })
    elif verdict_label == VERDICT_STRONG and (
        category_visibility_score is not None
        and visibility_score >= 50
        and (visibility_score - int(category_visibility_score)) >= 25
    ):
        # Branded buyer-intent is solved, but category discovery lags — the
        # primary lever is winning category (non-branded) queries, NOT just
        # maintenance. Keep this consistent with next_best_action's
        # category_discovery_gap so the action list doesn't say "coast" while
        # the headline prescription says "close the category gap".
        items.append({
            **_score(
                base="high",
                has_named_competitors=_has_named_competitors_any,
            ),
            "title": "Win category (non-branded) discovery, not just branded search",
            "body": (
                f"AI agents cite your URL in {merchant_cited_runs} of "
                f"{attribution_runs_total} branded buyer-intent queries "
                f"(visibility {visibility_score}/100, attribution "
                f"{attribution_score}/100) — branded discovery and attribution "
                f"are at goal state. But category visibility is "
                f"{int(category_visibility_score)}/100: shoppers who search the "
                f"category rather than your brand don't surface your product. "
                f"Add category + comparison content and earn cites in the "
                f"sources AI grounds those answers in."
            ),
            "evidence": {
                "merchant_cited_runs": merchant_cited_runs,
                "queries_tested": attribution_runs_total,
                "category_visibility_score": int(category_visibility_score),
            },
        })
    elif verdict_label == VERDICT_STRONG:
        items.append({
            **_score(base="low"),
            "title": "Maintain attribution with monitoring + drift detection",
            "body": (
                f"AI agents cite your URL in {merchant_cited_runs} of "
                f"{attribution_runs_total} buyer-intent queries "
                f"(visibility {visibility_score}/100, attribution "
                f"{attribution_score}/100). Both discovery and "
                f"attribution are at goal state. Watch for drift: "
                f"alert on attribution dropping below "
                f"{max(0, attribution_score - 15)}, schema regressions, "
                f"and new competitor cites that erode share."
            ),
            "evidence": {
                "merchant_cited_runs": merchant_cited_runs,
                "queries_tested": attribution_runs_total,
            },
        })
    else:  # PARTIAL
        losing = max(0, (attribution_runs_total or 0) - (merchant_cited_runs or 0))
        body = (
            f"Visibility {visibility_score}/100, attribution "
            f"{attribution_score}/100. Of {attribution_runs_total} "
            f"buyer-intent queries, {merchant_cited_runs} cited your "
            f"URL"
        )
        if losing > 0 and cited_hosts_phrase:
            body += (
                f"; the other {losing} grounded their answers in "
                f"{cited_group_label} including {cited_hosts_phrase}"
            )
        elif losing > 0:
            body += f"; the other {losing} did not cite a merchant URL"
        if losing > 0 and cited_hosts_phrase:
            body += (
                ". We did not verify whether those sources mention "
                "your brand or products."
            )
        body += (
            " The specific failing queries below are where the gaps "
            "are — close those first."
        )
        items.append({
            **_score(
                base="high",
                has_failed_query=_has_failed_attribution_query,
                has_named_competitors=_has_named_competitors_any,
            ),
            "title": "Close the gap on inconsistent queries",
            "body": body,
            "evidence": {
                "merchant_cited_runs": merchant_cited_runs,
                "queries_tested": attribution_runs_total,
                "top_retailers": top_retailer_names[:5],
                "top_cited_hosts": top_cited_names[:5],
            },
        })

    # Action 2: top competitor capture, named with frequency.
    if top_competitor and top_competitor.get("times_cited", 0) >= 2:
        items.append({
            # This site is guarded by `top_competitor` — we have a
            # named competitor by definition.
            **_score(
                base="high",
                has_failed_query=_has_failed_attribution_query,
                has_named_competitors=True,
            ),
            "title": f"Top citation drain: {top_competitor['host']}",
            "body": (
                f"`{top_competitor['host']}` was cited by Gemini in "
                f"{top_competitor['times_cited']} of the queries we "
                f"tested. They're capturing demand that should be "
                f"yours — every consumer arriving via that path is one "
                f"your direct site didn't get. If they're a reseller, "
                f"the margin loss is compounded; if they're a "
                f"marketplace, you're trading a first-party customer "
                f"relationship for a transaction."
            ),
            "evidence": {"competitor_host": top_competitor["host"]},
        })

    # Action 3: zero-citation case — more severe than just missing in
    # individual runs (the merchant's URL never showed in ANY grounded
    # source).
    if runs_with_any_citation > 0 and merchant_cited_runs == 0:
        items.append({
            # Zero-citation is critical evidence on its own; scorer
            # may pass it through, may upgrade if has_failed_query
            # adds the buyer-intent signal.
            **_score(
                base="critical",
                has_failed_query=_has_failed_attribution_query,
                has_named_competitors=_has_named_competitors_any,
            ),
            "title": "Zero direct AI-channel attribution today",
            "body": (
                f"Across {runs_with_any_citation} queries that returned "
                f"grounded sources, your verified URL appeared in zero "
                f"of them. Every grounded citation went to a third "
                f"party. First-party AI attribution is currently zero — "
                f"there is no organic AI-channel funnel."
            ),
        })

    # Action 4: specific failed-attribution query references (up to 2).
    if failed_attribution_queries:
        sample = ", ".join(
            f'"{_truncate_query(q)}"'
            for q in failed_attribution_queries[:2]
        )
        items.append({
            # THE canonical Winona regression case: base=medium with
            # gap=67 + has_failed_query → Rule 2/3/7 fires and upgrades.
            # Pre-PR-6 this shipped at medium regardless of evidence.
            **_score(
                base="medium",
                has_failed_query=True,
                has_named_competitors=_has_named_competitors_any,
            ),
            "title": "Specific queries where your URL was missing",
            "body": (
                f"Gemini's grounded answer to {sample} did not include "
                f"your verified PDP URL. These are buyer-intent queries "
                f"that should naturally route to your store; closing "
                f"them is the fastest path to attribution lift."
            ),
            "evidence": {"failed_queries": failed_attribution_queries[:5]},
        })

    # Action 5: visibility gap (open-product test failed grounding gate).
    # Suppressed for VIA_RETAILERS — "your PDP isn't indexed" reads
    # false for retail-strong brands whose PDPs ARE indexed; their
    # buyer-intent queries are just too long-tail. Suppressed for
    # STRONG too — discovery is solved.
    if (
        failed_visibility_queries
        and verdict_label != VERDICT_STRONG
        and verdict_label != VERDICT_VIA_RETAILERS
    ):
        items.append({
            # Visibility (not attribution) failures — we don't pass
            # has_failed_query here because the scorer's semantics
            # for that flag are buyer-intent failed-attribution
            # queries specifically (Rule 7's "exact buyer-intent
            # zero-attribution" case). Visibility failures are a
            # different evidence class, so pass through base=medium.
            **_score(base="medium"),
            "title": "Strengthen schema + sitemap inclusion for visibility",
            "body": (
                f"The product wasn't surfaced with grounded sources on "
                f"{len(failed_visibility_queries)} of "
                f"{len(visibility_runs)} visibility queries. Either "
                f"Gemini has no live-web knowledge of the product, or "
                f"your PDP isn't indexed deeply enough for grounded "
                f"retrieval. Schema.org Product + Breadcrumb markup "
                f"plus a Search-Console-submitted sitemap is the "
                f"foundation grounded LLMs need."
            ),
        })

    # Cap at 5 items so the report stays scannable.
    return items[:5]


# Pivota PDP self-baseline reference figures, used as the "after onboarding"
# benchmark in the "What Pivota changes" section. Source: aggregate of
# `scripts/agent_center_pivota_pdp_baseline.py` median_visibility +
# median_attribution across the 6 sig_* canonical seed PDPs. Refresh by hand
# (~1×/month or after material PDP/SEO changes); the alternative of running
# the baseline live at report-render time costs ~9 grounded Gemini calls per
# report and adds latency without a corresponding pitch benefit.
#
# TODO: when the baseline scheduler ships (deferred — Phase 3), wire these
# figures from the latest scheduled run instead of hardcoding.
PIVOTA_PDP_BASELINE_REFERENCE: Dict[str, Any] = {
    # Live numbers from `scripts/agent_center_pivota_pdp_baseline.py`
    # run on as_of_date. Refresh by re-running the script + updating
    # this dict; the BD report exposes these figures as the
    # "after-onboarding reference" anchor.
    #
    # Today (2026-05-06): Pivota canonical PDPs are in the indexing-up
    # phase — 0/5 surface in Gemini grounding for named-product
    # buyer-intent queries. Mechanics are shipped (canonical PDP +
    # Schema.org + sitemap); Google indexing latency is the rate-
    # limiting step. Expected to lift over the next 30-90 days as
    # Search Console URL Inspection submissions mature.
    "median_visibility": 0,
    "median_attribution": 0,
    "sample_size_pdps": 5,    # 1 PDP returned upstream 502 on this run
    "cited_count": 0,         # PDPs cited in grounding at least once
    "succeeded_count": 5,
    "as_of_date": "2026-05-06",
    "indexing_phase": "indexing-up",  # vs "steady-state"
    # Single source of truth for which probe modes the baseline runs.
    # Pulled from agent_center_pivota_pdp_baseline.py — keep in sync
    # if the script's mode list ever changes.
    "probe_modes_in_baseline": [
        "open_product_visibility_test",
        "merchant_store_attribution_test",
    ],
}


# Reference label for the internal Shopify test playground used by the
# onboarding sequence's order-side steps. Do not hardcode a real merchant
# ID here; production runtime must not depend on any specific test store.
TEST_MERCHANT_REFERENCE: Dict[str, str] = {
    "merchant_id": "internal_shopify_test_merchant",
    "shop_domain": "internal-shopify-test.example",
    # Discovery-side artifact: probes Pivota canonical sig_* PDPs,
    # NOT the test merchant's shop URL (which is unindexed by
    # design — Shopify dev domain has no public retrieval surface).
    "discovery_baseline_path": "reports/pivota-pdp-baseline.md",
    "audit_artifact_path": "reports/pivota-pdp-baseline.md",
}


# PR-10d: platform-specific copy for the last two chain steps + the
# outcome line of checkout_loop. Display labels mirror the
# capitalized brand spellings BD operators use in client conversations.
_PLATFORM_DISPLAY_NAMES: Dict[str, str] = {
    "shopify": "Shopify",
    "woocommerce": "WooCommerce",
    "bigcommerce": "BigCommerce",
    "wix": "Wix",
    "custom": "custom storefront",
    "headless_generic": "headless commerce backend",
}


def _build_platform_specific_chain_tail(
    merchant_platform: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    """Return platform-specific copy for checkout_loop's last two
    chain steps + the outcome line. Branches on the merchant's
    platform_capabilities.supports_platform_order_writeback flag
    so the report says what's TRUE for THIS merchant:

      - Shopify / WooCommerce / BigCommerce (writeback shipped):
        "Order forwarded to merchant's WooCommerce admin async"
        + outcome reads "Orders land in your WooCommerce admin..."
      - Wix / Custom / Headless (audit-only or custom integration):
        "Order routed to operations queue for manual fulfillment
        into your <platform>" + outcome reads "Until automated
        writeback ships, orders are routed via Pivota operations..."
      - Cold-start / unknown platform:
        Generic multi-platform copy that lists supported targets
        without claiming any specific integration is wired.

    Returns a dict with three keys: `step_5`, `step_6`, `outcome`.
    Each step value is the chain-entry shape expected by callers.
    """
    from services.platform_capabilities import get_store_platform_capabilities

    key = (merchant_platform or "").strip().lower()
    display = _PLATFORM_DISPLAY_NAMES.get(key)

    if key and display:
        # Known platform — branch on writeback support.
        capabilities = get_store_platform_capabilities(key)
        if capabilities.supports_platform_order_writeback:
            return {
                "step_5": {
                    "step": 5,
                    "label": (
                        f"Order forwarded to merchant's {display} admin "
                        f"async (background task)"
                    ),
                    "evidence": (
                        f"Live forwarding via {display} admin API"
                    ),
                    "shipped": True,
                },
                "step_6": {
                    "step": 6,
                    "label": (
                        f"Merchant sees the order in their {display} "
                        f"admin with first-party customer data (email, "
                        f"address, line items, attribution metadata)"
                    ),
                    "evidence": (
                        f"Verified end-to-end against a live {display} "
                        f"test merchant; covered by automated regression "
                        f"tests on each release"
                    ),
                    "shipped": True,
                },
                "outcome": (
                    f"Orders land in your {display} admin within seconds "
                    f"of in-chat completion. Customer email, shipping "
                    f"address, line items, and source-attribution "
                    f"metadata (`source = pivota_acp`, `agent = gemini`) "
                    f"are first-party data you own — Pivota does not "
                    f"intermediate the customer relationship."
                ),
            }
        # Audit-only / custom-integration platform.
        return {
            "step_5": {
                "step": 5,
                "label": (
                    f"Order routed to Pivota operations queue for "
                    f"manual fulfillment into your {display} admin "
                    f"(automated writeback adapter on the integration "
                    f"backlog)"
                ),
                "evidence": (
                    f"Manual routing today; automated {display} "
                    f"writeback scheduled for a follow-up integration "
                    f"sprint"
                ),
                "shipped": False,
            },
            "step_6": {
                "step": 6,
                "label": (
                    f"Pivota operations forwards the order into your "
                    f"{display} admin within one business day, with "
                    f"first-party customer data preserved (email, "
                    f"address, line items, attribution metadata)"
                ),
                "evidence": (
                    "Manual fulfillment SLA; automated writeback "
                    "ships when the platform adapter lands"
                ),
                "shipped": False,
            },
            "outcome": (
                f"Until the automated {display} writeback adapter "
                f"ships, orders are routed via Pivota operations with "
                f"a one-business-day SLA. First-party customer data "
                f"(`source = pivota_acp`, `agent = gemini`) is "
                f"preserved through the routing step — you own the "
                f"customer relationship regardless of fulfillment "
                f"path."
            ),
        }

    # No platform known (cold-start audit / pre-onboarding BD report).
    # Use multi-platform copy that lists the shipped targets without
    # claiming any specific integration is wired for THIS merchant.
    return {
        "step_5": {
            "step": 5,
            "label": (
                "Order forwarded to merchant's storefront admin async "
                "(background task; native adapter for Shopify, "
                "WooCommerce, BigCommerce — Wix + custom platforms "
                "via lightweight integration)"
            ),
            "evidence": (
                "Live forwarding via the platform-specific admin API "
                "once the merchant is onboarded"
            ),
            "shipped": True,
        },
            "step_6": {
                "step": 6,
                "label": (
                    "Merchant sees the order in their storefront admin "
                    "(Shopify / WooCommerce / BigCommerce native adapters) "
                    "with first-party customer data (email, address, "
                    "line items, attribution metadata)"
                ),
                "evidence": (
                    "Verified end-to-end on Shopify / WooCommerce / "
                    "BigCommerce using the internal test playground "
                    f"{TEST_MERCHANT_REFERENCE['merchant_id']}; equivalent "
                    "surfaces shipped for Wix + custom via integration sprint"
                ),
                "shipped": True,
            },
        "outcome": (
            "Orders land in the merchant's storefront admin within "
            "seconds of in-chat completion (Shopify, WooCommerce, "
            "BigCommerce — native adapter). Customer email, shipping "
            "address, line items, and source-attribution metadata "
            "(`source = pivota_acp`, `agent = gemini`) are first-"
            "party data the merchant owns — Pivota does not "
            "intermediate the customer relationship."
        ),
    }


def _build_what_pivota_changes(
    *,
    merchant_name: str,
    merchant_pdp_url: str,
    attribution_score: int,
    attribution_runs: int,
    merchant_cited_runs: int,
    category_retailer_hosts: List[Dict[str, Any]],
    category_visibility_score: Optional[int],
    merchant_platform: Optional[str] = None,
    providers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return the "What Pivota changes after onboarding" structured block.

    Two parts the merchant needs to believe before signing:

      1. **discovery_lift** — Why your AI-channel visibility will improve.
         Anchored on Pivota's 6 canonical seed PDPs (median visibility
         67/100, attribution 50/100) as the empirical "after" reference,
         plus the four mechanics that produce that surface (canonical
         AI-channel PDP / Schema.org structured data / sitemap submission
         / semantic categorization). The claim is comparative, not paired
         A/B — clearly disclosed in `methodology_note`.

      2. **checkout_loop** — How in-chat checkout closes the loop. The
         end-to-end 6-step chain from grounded Gemini citation to the
         merchant's admin, each step tagged shipped/roadmap with the
         verifying file or test reference. `merchant_platform` (when
         provided, from integration_state.store_platform_name) shapes
         the platform-specific language at steps 5-6 + outcome so a
         WooCommerce merchant reads "your WooCommerce admin", a Wix
         merchant reads "manual order routing today (Wix writeback on
         Q3 roadmap)", etc. When `merchant_platform` is None (cold-
         start audits where the platform is unknown), step 5-6 use
         multi-platform copy that lists all supported targets without
         claiming any specific one is wired.

    PR-10d: `merchant_platform` plumbed through from
    `integration_state.store_platform_name`. Accurate per-platform
    disclosure was previously buried in the platform_coverage block
    only; the visible checkout_loop chain still said "Shopify" even
    for Woo/BC/Wix merchants."""
    gap_pct = max(0, 100 - int(attribution_score))
    top_retailers = [
        r["host"] for r in _retail_cited_hosts(category_retailer_hosts)[:3]
        if r.get("host")
    ]
    top_cited_hosts = [
        r["host"] for r in _copyworthy_cited_hosts(category_retailer_hosts)[:3]
        if r.get("host")
    ]
    retailer_phrase = ", ".join(top_retailers)
    cited_host_phrase = (
        ", ".join(top_cited_hosts)
        if top_cited_hosts
        else "third-party sources"
    )
    captured_by_phrase = retailer_phrase or cited_host_phrase
    capture_subject = (
        f"Retailer pages ({captured_by_phrase})"
        if retailer_phrase
        else (
            f"Third-party sources ({captured_by_phrase})"
            if top_cited_hosts
            else "Third-party sources"
        )
    )
    cat_phrase = (
        f"category visibility {category_visibility_score}/100"
        if category_visibility_score is not None
        else "category visibility (not measured)"
    )
    today_summary = (
        f"{cat_phrase}; {merchant_cited_runs}/{attribution_runs} "
        f"buyer-intent queries reach the merchant URL today; the "
        f"remaining ~{gap_pct}% of the AI-channel funnel is captured by "
        f"{captured_by_phrase}."
    )

    # Honest model naming (mirrors _shape_url_audit_response on the URL-audit
    # surface): the Layer-1 grounded-citation copy must name the providers this
    # audit's `attribution_score` was actually measured on, not a hardcoded
    # "Gemini". `providers` is the resolved grounded-shopping profile
    # (profile_providers) threaded down from build_structured_report — NOT the
    # answer-quality verify set. On the default/verify-only profiles that's
    # Gemini (+ChatGPT), with DeepSeek confined to the separate verify pass. On
    # operator-selected profiles that put DeepSeek in `providers` (e.g.
    # gemini_deepseek), DeepSeek genuinely fed attribution_score and is named
    # here — which is honest, not a leak of the verify-only provider.
    _ran_label = _humanize_provider_list(providers or []) or "Gemini"
    _ran_keys = {str(p).strip().lower() for p in (providers or [])}
    # Roadmap engines shown as "maturing" — drop any already running today so a
    # provider never appears in both the "today" and the "as those mature" lists.
    _layer1_roadmap = [
        label
        for key, label in (
            ("chatgpt", "ChatGPT search"),
            ("perplexity", "Perplexity"),
            ("claude", "Claude"),
        )
        if key not in _ran_keys
    ]
    _layer1_subtitle = f"{_ran_label} today" + (
        f"; {' / '.join(_layer1_roadmap)} as those engines mature"
        if _layer1_roadmap
        else ""
    )

    discovery_lift = {
        "title": "Why your AI-channel discoverability will improve (multi-layer)",
        "current_state": (
            f"{cat_phrase}; {merchant_cited_runs}/{attribution_runs} "
            f"buyer-intent queries reach your URL today (this audit "
            f"measures Layer 1: grounded LLM citation). {capture_subject} "
            f"currently capture the rest of the "
            f"grounded surface."
        ),
        # The 3-layer agentic discovery surface. The merchant is buying
        # access to all three layers; today's audit only measures Layer 1.
        # Layer 2 is what's distinct about Pivota — direct API queries
        # from the proliferating agent ecosystem, independent of Google
        # indexing.
        "layers": [
            {
                "name": "Layer 1 — Grounded LLM citation",
                "subtitle": _layer1_subtitle,
                "what_it_is": (
                    "AI assistants that ground answers in live web search "
                    "(Google for Gemini, Bing for ChatGPT, etc.) cite "
                    "indexed canonical pages. Indexed PDPs surface as "
                    "buying paths. This is what `attribution_score` in "
                    "this report measures."
                ),
                "pivota_status": (
                    "**Indexing-up phase.** Pivota canonical PDPs are in "
                    "the typical 30-90 day post-publication arc working "
                    "through Search Console URL Inspection. In Pivota's own "
                    f"baseline ({TEST_MERCHANT_REFERENCE['discovery_baseline_path']}), "
                    f"{PIVOTA_PDP_BASELINE_REFERENCE['cited_count']} of "
                    f"{PIVOTA_PDP_BASELINE_REFERENCE['succeeded_count']} probed "
                    "canonical PDPs were cited in grounded answers so far. The "
                    "mechanics below are shipped; Google indexing latency is the "
                    "rate-limiting step before grounded-citation lift."
                ),
                "merchant_metric": "attribution_score",
                "mechanics": [
                    {
                        "label": "Canonical AI-channel PDP per SKU",
                        "evidence": "Live at agent.pivota.cc/products/sig_*",
                        "shipped": True,
                    },
                    {
                        "label": "Schema.org Product + Offer + BreadcrumbList structured data",
                        "evidence": "Embedded JSON-LD on every canonical PDP",
                        "shipped": True,
                    },
                    {
                        "label": "Sitemap submission + URL-Inspection indexing for grounded retrieval",
                        "evidence": "Sitemap published + Search Console URL Inspection submissions weekly",
                        "shipped": True,
                    },
                    {
                        "label": "Semantic categorization via canonical title patterns + breadcrumbs",
                        "evidence": "Category-aware metadata + JSON-LD breadcrumbs on every PDP",
                        "shipped": True,
                    },
                ],
            },
            {
                "name": "Layer 2 — Agent-direct API queries",
                "subtitle": "Apps-as-agents (Uber, Airbnb, Spotify, Reddit, X, Discord, Pinterest, news + dating apps) + personal agents (ChatGPT GPTs, Claude Projects, Perplexity Agents, custom corporate procurement)",
                "what_it_is": (
                    "Big consumer apps with massive existing audiences "
                    "but no shopping monetization today are transforming "
                    "into agents — Uber, Airbnb, Spotify, Reddit, X, "
                    "Discord, Pinterest, news apps, dating apps, fitness "
                    "trackers — every one of them needs a commerce layer "
                    "to monetize the user prompts they're already "
                    "fielding. Plus personal agents (ChatGPT GPTs, "
                    "Claude Projects, Perplexity Agents, custom "
                    "corporate procurement bots) querying directly. All "
                    "of these connect to Pivota via ACP + UCP API. When "
                    "a user prompts the agent (\"find me a snail mucin "
                    "mask under $20\" inside Reddit, or \"replenish my "
                    "eye-patch order\" inside Uber's voice agent), the "
                    "agent queries Pivota's catalog and recommends "
                    "matching merchant products + completes checkout. "
                    "**Independent of Google indexing** — agents resolve "
                    "canonical PDPs by their stable Pivota URL and "
                    "transact via the agentic-commerce protocol. This is "
                    "where Pivota wins the long game: every app that "
                    "transforms into an agent is a new commerce channel "
                    "for free, and onboarded merchants are queryable "
                    "across all of them without bilateral integration "
                    "work — Pivota is the catalog they all plug into."
                ),
                "pivota_status": (
                    "**Shipped + queryable today.** ACP + UCP + "
                    "agent_shop_gateway are all live; canonical PDPs are "
                    "addressable URLs any agent can reference. Onboarded "
                    "merchants are agent-queryable on day-1 of "
                    "onboarding, independent of Layer 1's indexing arc."
                ),
                "merchant_metric": None,  # binary by integration; no per-merchant probe
                "mechanics": [
                    {
                        "label": "ACP /orders/create — agents create + complete orders",
                        "evidence": "Live API endpoint; covered by automated end-to-end tests against a live Shopify test merchant",
                        "shipped": True,
                    },
                    {
                        "label": "UCP /ucp/v1/checkout-sessions — agent-callable checkout",
                        "evidence": "Live API endpoint",
                        "shipped": True,
                    },
                    {
                        "label": "Agent shop gateway (agent_shop_gateway) — unified agent operation surface (search → cart → checkout)",
                        "evidence": "Live API endpoint",
                        "shipped": True,
                    },
                    {
                        "label": "Canonical PDP URLs as stable agent-resolvable identifiers",
                        "evidence": "Live at agent.pivota.cc/products/sig_*; agents pass them through to checkout intents",
                        "shipped": True,
                    },
                ],
            },
        ],
        "prediction": (
            "Layer 1 (grounded LLM) follows the 30-90 day Google "
            "indexing arc; Pivota co-invests with onboarded merchants "
            "via Search Console URL Inspection submissions. Layer 2 "
            "(agent-direct API) is the day-1 value: any app or personal "
            "agent integrating with Pivota's ACP/UCP can recommend the "
            "merchant's products from the moment onboarding completes, "
            "independent of Google indexing. The compounding play sits "
            "in Layer 2 — as the agent ecosystem grows (every app "
            "becoming an agent, personal agents proliferating), Pivota-"
            "onboarded merchants are the agent-queryable catalog every "
            "new agent plugs into without bilateral integration work."
        ),
        "methodology_note": (
            "This audit's `attribution_score` measures Layer 1 only "
            f"(grounded LLM citation via {_ran_label}). The "
            f"{PIVOTA_PDP_BASELINE_REFERENCE['median_visibility']}/"
            f"{PIVOTA_PDP_BASELINE_REFERENCE['median_attribution']} "
            "Pivota baseline (probe modes "
            f"{', '.join(PIVOTA_PDP_BASELINE_REFERENCE['probe_modes_in_baseline'])}) "
            "is comparable only to the attribution_score. Layer 2's "
            "agent-direct surface has no per-merchant probe — it's binary "
            "by API integration: onboarded merchants are agent-queryable, "
            "non-onboarded are not. Pivota's own canonical PDPs are "
            "currently in the "
            f"{PIVOTA_PDP_BASELINE_REFERENCE['indexing_phase'].replace('_', '-')} "
            "phase for Layer 1; Layer 2 is shipped today."
        ),
    }

    # PR-10d: per-platform copy for the last two chain steps + the
    # outcome line. Falls back to multi-platform copy when the
    # platform is unknown (cold-start audits).
    _platform_chain = _build_platform_specific_chain_tail(merchant_platform)

    checkout_loop = {
        "title": "How in-chat checkout closes the loop",
        "chain": [
            {
                "step": 1,
                "label": "AI agent (Gemini / ChatGPT / shopping agent) cites the Pivota canonical PDP in a grounded answer",
                "evidence": "Live at agent.pivota.cc/products/sig_*",
                "shipped": True,
            },
            {
                "step": 2,
                "label": "Consumer (or their AI agent) triggers buy intent on the PDP",
                "evidence": "Buy CTA on every canonical PDP",
                "shipped": True,
            },
            {
                "step": 3,
                "label": "UCP (Universal Commerce Protocol) checkout session opens in-chat",
                "evidence": "Live API endpoint",
                "shipped": True,
            },
            {
                "step": 4,
                "label": "ACP (Agent Commerce Protocol) creates the order + processes payment",
                "evidence": "Live API endpoint",
                "shipped": True,
            },
            _platform_chain["step_5"],
            _platform_chain["step_6"],
        ],
        "platform_coverage": {
            # Multi-platform: end-to-end order writeback adapters for
            # Shopify, WooCommerce, and BigCommerce all live in
            # routes/order_routes.py; sync_order_to_connected_store
            # dispatches by platform. Wix is currently audit-ready
            # (read-only audit + manual order routing) pending the
            # Wix App OAuth + Stores API writeback integration. Custom
            # / headless storefronts are supported via a lightweight
            # engineering-scoped integration of the merchant's order
            # API (typical 1-2 weeks).
            "shipped": ["Shopify", "WooCommerce", "BigCommerce"],
            "audit_only": ["Wix"],
            "custom_integration": (
                "Custom-built and headless storefronts (Saleor, Medusa, "
                "Next.js + commerce backend, etc.) are supported via "
                "engineering-scoped integration of the merchant's "
                "order-creation API; typical scope 1-2 weeks."
            ),
            "note": (
                "Multi-platform: Shopify, WooCommerce, and BigCommerce "
                "are wired end-to-end for in-chat agent checkout → "
                "order forwarding into the merchant's existing admin. "
                "Wix supports the audit + agent-channel discovery path "
                "today; automated order writeback is on the Q3 roadmap. "
                "Any other platform — including custom-built and "
                "headless — is supported via lightweight integration."
            ),
        },
        "outcome": _platform_chain["outcome"],
    }

    onboarding_sequence = {
        "title": "Onboarding sequence — validated end-to-end on a live test merchant",
        "intro": (
            "Each step below is operated either as a Pivota agent or as "
            "a shipped pipeline (Shopify OAuth + ACP). Every step has "
            "been run end-to-end against a live Shopify test merchant "
            "we operate internally, so the sequence below is "
            "verifiable, not aspirational. Steps marked `manual_today` "
            "work end-to-end but don't yet have a one-click agent "
            "runner; operations runs them on the merchant's behalf "
            "during onboarding."
        ),
        "test_merchant": dict(TEST_MERCHANT_REFERENCE),
        "steps": [
            {
                "step": 1,
                "name": "Demand Test (this report)",
                "status": "shipped",
                "manual_today": False,
                "operates": "Pivota agent",
                "what": (
                    "Audits AI-channel discoverability + first-party "
                    "attribution against Gemini grounded search. This "
                    "document is its output."
                ),
                "addresses": "Establishes the pre-onboarding baseline.",
                "test_merchant_validation": (
                    "Same engine runs Pivota's own canonical-PDP "
                    "self-baseline monthly. Discovery side: the AI-"
                    "channel surface Pivota publishes for merchant SKUs."
                ),
            },
            {
                "step": 2,
                "name": "SKU Match",
                "status": "shipped",
                "manual_today": False,
                "operates": "Pivota agent",
                "what": (
                    "Data-quality preflight on the merchant's product "
                    "catalog: flags missing SKU IDs, missing/stale "
                    "prices, missing images, stale cache. These are the "
                    "gating issues that block canonical PDP indexing."
                ),
                "addresses": (
                    "Resolves catalog defects before they reach the "
                    "AI-channel surface."
                ),
                "test_merchant_validation": (
                    "Test merchant catalog runs through SKU Match on "
                    "each ops cycle; flag categories (missing/stale "
                    "price, image, cache) validated against a "
                    "known-good catalog."
                ),
            },
            {
                "step": 3,
                "name": "Merchant onboarding (KYB + Shopify OAuth)",
                "status": "shipped",
                "manual_today": False,
                "operates": "Pivota merchant portal pipeline",
                "what": (
                    "Standard onboarding: KYB verification + PSP setup "
                    "+ Shopify OAuth installs the access token Pivota "
                    "needs to forward orders."
                ),
                "addresses": (
                    "Wires the order-forwarding path described in the "
                    "Checkout Loop section above (step 5 of the chain)."
                ),
                "test_merchant_validation": (
                    "A live Shopify test merchant has been fully "
                    "onboarded via this pipeline — Shopify OAuth "
                    "completed, access token stored, store record "
                    "exists. That same record is what the order-"
                    "forwarding code reads at order time."
                ),
            },
            {
                "step": 4,
                "name": "End-to-end checkout verification",
                "status": "shipped",
                "manual_today": True,
                "operates": "Pivota ACP + manual ops walkthrough",
                "what": (
                    "Place a test order against one canonical PDP for a "
                    "representative SKU; confirm the 6-step chain "
                    "completes and the order lands in the merchant's "
                    "Shopify admin with first-party customer data."
                ),
                "addresses": (
                    "Confirms the Checkout Loop is functional for this "
                    "merchant's specific catalog/Shopify setup before "
                    "going live."
                ),
                "test_merchant_validation": (
                    "Verified by an automated end-to-end test against "
                    "the live Shopify test merchant on every release. "
                    "Hardening regression tests lock the auth-fallback "
                    "and retry behavior. The same verification script "
                    "is run against the prospective merchant during "
                    "onboarding QA."
                ),
            },
            {
                "step": 5,
                "name": "Audit re-run + attribution monitoring",
                "status": "shipped (audit) / roadmap (automated GMV agent)",
                "manual_today": True,
                "operates": "Demand Test (rerun) + manual GMV review of order metadata",
                "what": (
                    "Re-run this audit 30 days post-onboarding for "
                    "paired before/after lift on the same SKUs. Order "
                    "metadata (`source = pivota_acp`, `agent = gemini`) "
                    "supports manual GMV attribution today; an "
                    "automated GMV Attribution agent is on the Q3 "
                    "roadmap."
                ),
                "addresses": (
                    "Replaces today's comparative reference (vs Pivota "
                    "baseline) with merchant-specific paired data; "
                    "quantifies the lift."
                ),
                "test_merchant_validation": (
                    "Test merchant has a rolling history of monthly "
                    "Demand Test audits archived alongside its order "
                    "metadata; the diff between successive months is "
                    "what an automated GMV Attribution agent would "
                    "compute. Pattern is shipped — the wrapper agent is "
                    "the Q3 roadmap item."
                ),
            },
        ],
        "roadmap_note": (
            "RESERVED: Three Pivota agents (Offer Execution, Checkout "
            "Verification, GMV Attribution) are on the Q3 roadmap as "
            "one-click runners — not yet shipped as automated agents. "
            "Steps 4 and 5 above deliver their function manually today; "
            "the underlying pipelines they will wrap are already in "
            "production and validated against a live test merchant."
        ),
    }

    # Visibility Booster — directly answers the merchant's "what does
    # Pivota actually do to lift my Layer 1 visibility?" question, plus
    # corrects the common misconception that prompt repetition lifts
    # grounded retrieval. Honest separation of "what works" (mechanisms
    # that move grounded-retrieval signals) vs "what doesn't" (folk
    # remedies BD has heard from merchants).
    visibility_booster = {
        "title": "Merchant-side agent workflow — how Pivota actually lifts your Layer 1 visibility",
        "intro": (
            "A common merchant question: \"can you just send a lot of "
            "prompts that mention our product + URL until Gemini learns "
            "to cite us?\" — short answer, no. LLMs don't learn from "
            "prompt history; grounded retrieval reads from the live web "
            "index. Below is what actually moves the needle on Layer 1, "
            "and what Pivota's merchant-side agents operate on the "
            "merchant's behalf during onboarding + monthly thereafter."
        ),
        "mechanisms_that_work": [
            {
                "label": "Index reinforcement — Search Console URL Inspection",
                "what": (
                    "Pivota submits each onboarded SKU's canonical PDP "
                    "to Google Search Console URL Inspection so it gets "
                    "crawled + indexed faster than organic discovery "
                    "would deliver. Repeated weekly until the URL is "
                    "indexed."
                ),
                "status": "manual_today",
                "evidence": "ops runbook today; wrapped as 1-click agent on Q3 roadmap",
            },
            {
                "label": "Structured-data depth — Schema.org Product + Offer + BreadcrumbList",
                "what": (
                    "Pivota canonical PDPs ship with Schema.org "
                    "structured data that grounded LLM retrievers parse. "
                    "Richer structured data = higher chance of citation "
                    "when an AI agent looks for buying paths."
                ),
                "status": "shipped",
                "evidence": "Embedded JSON-LD on every canonical PDP",
            },
            {
                "label": "Stable canonical URL — citable, agent-resolvable",
                "what": (
                    "agent.pivota.cc/products/sig_* URLs are stable + "
                    "addressable. As 3rd-party content (reviews, "
                    "articles, comparisons) emerges over time, citations "
                    "land on the canonical URL and reinforce its "
                    "authority — compounding the longer the merchant "
                    "is on Pivota."
                ),
                "status": "shipped",
                "evidence": "Live at agent.pivota.cc/products/sig_*",
            },
            {
                "label": "Continuous diagnosis — Demand Test agent monthly rerun",
                "what": (
                    "Demand Test (this report's engine) reruns monthly "
                    "and identifies which queries fail to surface the "
                    "merchant's URL. Failures route to (a) catalog SKU "
                    "Match remediation, (b) ops Search Console "
                    "resubmission, (c) Q3-roadmap content-seeding agent."
                ),
                "status": "shipped",
                "evidence": "Monthly automated rerun for every onboarded merchant",
            },
            {
                "label": "Catalog quality preflight — SKU Match agent",
                "what": (
                    "SKU Match flags catalog defects (missing/stale "
                    "price, image, cache, SKU IDs) that gate canonical "
                    "PDP indexing. Resolves them before they reach the "
                    "AI-channel surface."
                ),
                "status": "shipped",
                "evidence": "Runs automatically on every catalog sync",
            },
        ],
        "what_doesnt_work": [
            (
                "**Prompt repetition / spam.** Sending many prompts to "
                "LLMs that mention the product + URL does NOT lift "
                "grounded retrieval. LLMs don't learn from prompt "
                "history; grounding reads from the web index, not "
                "conversation history. This is a popular folk remedy; "
                "it doesn't work."
            ),
            (
                "**Paid grounding / 'sponsored' citations.** No major "
                "grounded-LLM provider currently sells preferential "
                "placement in grounded answers. Even if they did, this "
                "audit measures organic surface; paid wouldn't change "
                "the score."
            ),
            (
                "**Hard-coding citations into the LLM.** Not exposed by "
                "any provider; LLMs don't have a \"manually add this "
                "URL to my retrieval set\" surface."
            ),
            (
                "**Buying ads on retailer pages that already cite you.** "
                "Increases retailer pageviews but does NOT lift Layer 1 "
                "merchant-URL attribution — retailers will still be "
                "the cited URL."
            ),
        ],
        "honest_position": (
            "Pivota's lever is index reinforcement + canonical-URL "
            "authority + Layer 2 (agent-direct API). We do NOT promise "
            "to make Gemini cite the merchant's URL tomorrow — Layer 1 "
            "is a 30-90 day arc bound by Google indexing latency. We DO "
            "promise day-1 Layer 2 surface for any agent integrating "
            "with our API, monthly diagnostic + ops remediation on "
            "Layer 1, and compounding canonical-URL authority over the "
            "life of the merchant's Pivota tenure."
        ),
    }

    return {
        "today_summary": today_summary,
        "discovery_lift": discovery_lift,
        "checkout_loop": checkout_loop,
        "onboarding_sequence": onboarding_sequence,
        "visibility_booster": visibility_booster,
    }


def _provider_medians(citation_by_provider: Optional[Mapping[str, Any]]) -> Optional[Dict[str, int]]:
    """{provider: median} from brand_rollup.citation_by_provider — the per-engine
    score for a single run's trend point (e.g. {"gemini": 18, "chatgpt": 21})."""
    if not isinstance(citation_by_provider, Mapping):
        return None
    out: Dict[str, int] = {}
    for provider, entry in citation_by_provider.items():
        median = entry.get("median") if isinstance(entry, Mapping) else None
        if median is None:
            continue
        try:
            out[str(provider)] = int(median)
        except (TypeError, ValueError):
            continue
    return out or None


def _by_model_delta(
    current_by_model: Optional[Mapping[str, Any]],
    prior_by_model: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, int]]:
    """Per-engine score change since the last audit. None for any engine either
    run didn't measure (so the UI can render '—' rather than a fake 0)."""
    if not isinstance(current_by_model, Mapping) or not isinstance(prior_by_model, Mapping):
        return None
    out: Dict[str, int] = {}
    for provider, current in current_by_model.items():
        prior = prior_by_model.get(provider)
        if current is None or prior is None:
            continue
        try:
            out[str(provider)] = int(current) - int(prior)
        except (TypeError, ValueError):
            continue
    return out or None


def _build_history_trend(
    prior_runs: Optional[List[Dict[str, Any]]],
    current_scores: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """PR-C: distill the merchant's last few audit runs into a trend
    summary the merchant_view.tracking block can render. None when
    no prior runs (first-ever audit on this merchant).

    Each prior_run entry comes from `db.merchant_audit_runs.recent_runs_for_merchant`.
    We use the most-recent succeeded run's scores as the comparison
    baseline — delta from THIS audit shows the merchant whether they're
    moving up, flat, or down since last time.

    PR-1a (APM): when `current_scores` is provided (the just-completed
    audit's verdict scores), compute `delta_from_most_recent` so the
    portal can render "+12 visibility, -3 attribution since last
    audit" badges. None when current_scores absent — caller can still
    render the sparkline from `series`.
    """
    succeeded = [
        r for r in (prior_runs or [])
        if r.get("status") == "succeeded"
        and r.get("visibility_score_avg") is not None
    ]
    if not succeeded:
        return None
    most_recent = succeeded[0]

    # Compute delta vs most-recent prior run when current scores
    # available. None for any score the current audit didn't measure
    # (e.g. category_visibility skipped because product_type missing
    # — in that case the delta is meaningless).
    delta_from_most_recent: Optional[Dict[str, Any]] = None
    if current_scores:
        def _delta(curr_key: str, prior_key: str) -> Optional[int]:
            curr = current_scores.get(curr_key)
            prior = most_recent.get(prior_key)
            if curr is None or prior is None:
                return None
            return int(curr) - int(prior)

        delta_from_most_recent = {
            "visibility": _delta("visibility", "visibility_score_avg"),
            "attribution": _delta("attribution", "attribution_score_avg"),
            "category_visibility": _delta(
                "category_visibility", "category_visibility_score_avg",
            ),
            # Per-engine delta (Gemini vs ChatGPT since last audit) when the
            # current run carried per-provider scores.
            "by_model": _by_model_delta(
                current_scores.get("by_model"), most_recent.get("provider_scores"),
            ),
            "days_since_last_audit": _days_between(
                most_recent.get("requested_at"),
            ),
        }

    return {
        "audits_in_history": len(succeeded),
        "most_recent_audit": {
            "run_id": most_recent.get("run_id"),
            "requested_at": most_recent.get("requested_at"),
            "visibility": most_recent.get("visibility_score_avg"),
            "attribution": most_recent.get("attribution_score_avg"),
            "category_visibility": most_recent.get("category_visibility_score_avg"),
            "by_model": most_recent.get("provider_scores"),
            "verdict_labels": most_recent.get("verdict_labels") or [],
        },
        "delta_from_most_recent": delta_from_most_recent,
        # The series for sparkline rendering (oldest → newest within
        # the history window). by_model = per-engine medians for that run
        # (None on legacy runs with no per-provider scores).
        "series": [
            {
                "requested_at": r.get("requested_at"),
                "visibility": r.get("visibility_score_avg"),
                "attribution": r.get("attribution_score_avg"),
                "category_visibility": r.get("category_visibility_score_avg"),
                "by_model": r.get("provider_scores"),
            }
            for r in reversed(succeeded)
        ],
    }


async def _attach_reaudit_delta(
    report: Dict[str, Any],
    *,
    merchant_id: Optional[str],
    prior_runs: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Attach the honest material-change layer, best-effort.

    ``prior_runs`` comes from recent_runs_for_merchant before the current run is
    marked succeeded. ``None`` means the caller did not provide history context,
    so omit the section. An empty list means a real first audit baseline.
    """
    if not isinstance(report, dict):
        return report
    merchant_view = report.get("merchant_view")
    if not isinstance(merchant_view, dict):
        return report
    if prior_runs is None:
        return report
    succeeded = [
        row for row in prior_runs
        if isinstance(row, dict) and row.get("status") == "succeeded"
    ]
    if not succeeded:
        merchant_view["reaudit_delta"] = build_reaudit_delta(
            current_report=report,
            prior_report=None,
            prior_row=None,
            days_since=None,
        )
        # Audit→action→outcome loop: first audit has no prior targets, so the
        # section degrades to an honest empty baseline (mirrors reaudit_delta's
        # is_first_audit contract).
        merchant_view["outreach_outcomes"] = build_outreach_outcomes(
            current_report=report,
            prior_report=None,
            measurement_basis=merchant_view["reaudit_delta"].get(
                "measurement_basis"
            ),
        )
        return report
    prior_row = succeeded[0]
    prior_run_id = str(prior_row.get("run_id") or "").strip()
    if not prior_run_id or not merchant_id:
        return report
    try:
        from db.merchant_audit_runs import fetch_audit_run_by_id

        prior_full = await fetch_audit_run_by_id(run_id=prior_run_id)
        prior_report = (
            prior_full.get("report_jsonb")
            if isinstance(prior_full, dict) else None
        )
        if not isinstance(prior_report, dict):
            return report
        merchant_view["reaudit_delta"] = build_reaudit_delta(
            current_report=report,
            prior_report=prior_report,
            prior_row=prior_row,
            days_since=_days_between(prior_row.get("requested_at")),
        )
        # Audit→action→outcome loop: what changed at the hosts the PRIOR
        # report told the merchant to target. Reuses the measurement basis
        # build_reaudit_delta just computed (query-level claims are only
        # licensed on the same pinned prompt set — never re-derived here).
        # The merchant's own done-marked tasks are surfaced as facts on
        # matching targets ("you marked this done N days before this run"),
        # never as causation.
        merchant_view["outreach_outcomes"] = build_outreach_outcomes(
            current_report=report,
            prior_report=prior_report,
            measurement_basis=merchant_view["reaudit_delta"].get(
                "measurement_basis"
            ),
            completed_actions=await _completed_outreach_actions(merchant_id),
        )
    except Exception as exc:  # noqa: BLE001 - audit must not fail on history
        logger.warning(
            "reaudit_delta attach failed merchant_id=%s prior_run_id=%s: %s",
            merchant_id, prior_run_id, str(exc)[:200],
        )
    return report


async def _completed_outreach_actions(
    merchant_id: Optional[str],
) -> Optional[List[Dict[str, Any]]]:
    """The merchant's done-marked task-queue rows that name a target host —
    passed to build_outreach_outcomes so a matching target carries the
    "you marked this done N days before this run" FACT (never a causal
    claim). Best-effort: a task-queue read failure must not sink the
    outcomes section, let alone the audit; None = lookup unavailable."""
    if not merchant_id:
        return None
    try:
        from db.merchant_tasks import list_tasks_for_merchant

        done_rows = await list_tasks_for_merchant(
            merchant_id=str(merchant_id),
            status_filter=["done"],
            limit=50,
        )
        return [
            {
                "host": (row.get("evidence_jsonb") or {}).get("target_host"),
                "title": row.get("title"),
                "completed_at": row.get("completed_at"),
            }
            for row in done_rows
            if isinstance(row.get("evidence_jsonb"), dict)
            and (row.get("evidence_jsonb") or {}).get("target_host")
        ]
    except Exception:  # noqa: BLE001
        logger.warning(
            "outreach_outcomes completed-task lookup failed merchant_id=%s",
            merchant_id, exc_info=True,
        )
        return None


async def _attach_outreach_outcomes_per_sku(
    report: Dict[str, Any],
    *,
    merchant_id: Optional[str],
    prior_runs: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Attach the audit→action→outcome section to a PER-SKU brand report,
    best-effort — the per-SKU sibling of _attach_reaudit_delta's outcomes
    attach (which only runs on the legacy per-product path).

    Attached TOP-LEVEL as report["outreach_outcomes"] (beside win_plan /
    merchant_narrative): the per-SKU brand report has no brand merchant_view,
    and its portal sections all read from the report root.

    ``prior_runs`` must already be mode-filtered by the caller
    (_per_sku_prior_runs) — only a per_sku prior carries the win_plan /
    authority_map shape the outcomes module compares against. ``None`` means
    no history context was provided, so the section is omitted; an empty list
    is a real first audit and attaches the honest baseline degrade.
    """
    if not isinstance(report, dict) or prior_runs is None:
        return report
    succeeded = [
        row for row in prior_runs
        if isinstance(row, dict) and row.get("status") == "succeeded"
    ]
    if not succeeded:
        # Best-effort like the success branch (review P2: this path was
        # unprotected — a raise here would sink the whole audit).
        try:
            report["outreach_outcomes"] = build_outreach_outcomes(
                current_report=report,
                prior_report=None,
                measurement_basis=measurement_basis_between(report, None),
            )
            # Wave-1 A1: first audit attaches the honest baseline shape.
            report["reaudit_delta"] = build_reaudit_delta(
                current_report=report,
                prior_report=None,
                prior_row=None,
                days_since=None,
            )
        except Exception:  # noqa: BLE001 - history must never sink the audit
            logger.warning("first-audit outcome/delta attach failed", exc_info=True)
        return report
    prior_row = succeeded[0]
    prior_run_id = str(prior_row.get("run_id") or "").strip()
    if not prior_run_id or not merchant_id:
        return report
    try:
        from db.merchant_audit_runs import fetch_audit_run_by_id

        prior_full = await fetch_audit_run_by_id(run_id=prior_run_id)
        prior_report = (
            prior_full.get("report_jsonb")
            if isinstance(prior_full, dict) else None
        )
        if not isinstance(prior_report, dict):
            return report
        report["outreach_outcomes"] = build_outreach_outcomes(
            current_report=report,
            prior_report=prior_report,
            # Same W2 basis verdict build_reaudit_delta computes — via
            # audit_delta's single source of truth, never re-derived here.
            measurement_basis=measurement_basis_between(report, prior_report),
            completed_actions=await _completed_outreach_actions(merchant_id),
        )
        # Wave-1 A1: per-SKU sibling of the legacy merchant_view attach —
        # TOP-LEVEL, same prior fetch, score movements + basis verdict.
        report["reaudit_delta"] = build_reaudit_delta(
            current_report=report,
            prior_report=prior_report,
            prior_row=prior_row,
            days_since=_days_between(
                str(prior_row.get("requested_at") or "") or None
            ),
        )
    except Exception as exc:  # noqa: BLE001 - audit must not fail on history
        logger.warning(
            "outreach_outcomes attach failed merchant_id=%s prior_run_id=%s: %s",
            merchant_id, prior_run_id, str(exc)[:200],
        )
    return report


def _days_between(iso_timestamp: Optional[str]) -> Optional[int]:
    """Return integer days from `iso_timestamp` to now (UTC). None when
    the input isn't parseable. Helper for trend delta rendering — the
    portal shows '+12 visibility (over 14 days)' not just '+12'."""
    if not iso_timestamp:
        return None
    try:
        # Handle both '...Z' and '...+00:00' shapes.
        s = iso_timestamp.replace("Z", "+00:00")
        prior = datetime.fromisoformat(s)
        if prior.tzinfo is None:
            prior = prior.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    delta = datetime.now(timezone.utc) - prior
    return max(0, int(delta.total_seconds() // 86400))


def _build_failed_queries_detailed(
    attribution_runs: List[Dict[str, Any]],
    *,
    merchant_brand: Optional[str],
    merchant_host: Optional[str],
    merchant_category: Optional[str],
    merchant_vendors: Optional[Tuple[str, ...]] = None,
    cap: int = 10,
) -> List[Dict[str, Any]]:
    """For each attribution-test query where the merchant's URL was
    NOT cited, surface what *did* win + which competitors Gemini
    named. Closes the gap between "your URL was missing on N queries"
    and "for THIS query, strategist.com cited Lunya / Eberjey / Hill
    House Home — your brand absent."

    Each entry:
      query              : verbatim query Gemini was asked
      top_cited_url      : winning URL (None when no grounded sources)
      top_cited_host     : hostname extracted from top_cited_url
      host_classification: classify_host output (type / coverage /
                           outreach hint), without the redundant `host`
                           field (it's already in `top_cited_host`)
      competitors_named  : up to 5 competitor brand names from
                           parsed.competitors_appearing, with the
                           merchant's own brand + aliases filtered out
                           (alias-aware via _strip_own_brand_competitors)

    Capped at `cap` entries (default 10) to keep response size bounded
    even on large probe runs.
    """
    out: List[Dict[str, Any]] = []
    brand_lower = (merchant_brand or "").strip().lower()
    host_lower = (merchant_host or "").strip().lower()
    # Alias-aware own-brand filter for competitors_appearing below — a de-spaced
    # echo ("bblab") or a vendor alias isn't a substring of the brand, so the
    # bare substring test used to leak the merchant into this merchant-facing
    # competitors_named field. Vendor aliases included when the caller has them.
    brand_aliases = derive_brand_aliases(
        merchant_brand or None,
        merchant_host,
        _clean_identity_tuple(merchant_vendors),
    )

    for run in attribution_runs or []:
        parsed = run.get("parsed") or {}
        if parsed.get("merchant_url_found"):
            continue  # query succeeded, not in the "failed" set

        # Resolve the cited host via _identify_run_sources so Vertex
        # redirector URIs render as their FINAL DESTINATION (e.g.
        # "whowhatwear.com — best lingerie roundup") instead of the
        # opaque vertexaisearch.cloud.google.com redirector. The
        # redirect-resolution + title-lookup logic is already there;
        # we just need to use it.
        sources = _identify_run_sources(run)
        chunks = run.get("grounding_chunks") or []
        if sources:
            top_label = sources[0].get("label") or ""
            top_url: Optional[str] = chunks[0] if chunks else None
            # Prefer the label as the host when it's already a hostname
            # (no spaces). Otherwise keep the raw URL host.
            top_host: Optional[str]
            if top_label and " " not in top_label and "." in top_label:
                top_host = top_label.lower()
            else:
                top_host = normalize_host(top_url) if top_url else None
        else:
            top_url = chunks[0] if chunks else None
            top_host = normalize_host(top_url) if top_url else None

        # If the "top cited host" actually IS the merchant (rare —
        # parsed.merchant_url_found should already catch this) skip
        # to avoid a confusing "you won this query but it's listed as
        # failed" output.
        if top_host and host_lower and host_lower in top_host.lower():
            continue

        competitors: List[str] = _strip_own_brand_competitors(
            parsed.get("competitors_appearing") or [],
            brand_lower,
            brand_aliases,
        )[:5]

        host_classification: Dict[str, Any]
        if top_host:
            full = classify_host(top_host, merchant_category=merchant_category)
            host_classification = {k: v for k, v in full.items() if k != "host"}
        else:
            host_classification = {
                "type": "unclassified",
                "subtype": None,
                "categories": [],
                "coverage_note": None,
                "outreach_hint": None,
                "applies_to_merchant_category": None,
            }

        out.append({
            "query": (run.get("query") or "").strip(),
            "top_cited_url": top_url,
            "top_cited_host": top_host,
            "host_classification": host_classification,
            "competitors_named": competitors,
        })
        if len(out) >= cap:
            break

    return out


def _category_recognition_phrase(
    score: Optional[int],
    *,
    has_title_match: bool,
    has_url_grounding: bool = False,
) -> str:
    """Score- and signal-conditional phrase for what category visibility
    actually means. Never overstates the evidence.

    Three category-evidence tiers, in descending strength:
      1. `has_url_grounding=True` — the merchant's URL was an actual
         grounding chunk. Strongest signal: AI grounded its category
         answer in the merchant's own site.
      2. `has_title_match=True` (URL did not appear) — Gemini named
         the brand in a grounded source title, but the source was
         someone else's URL. Still a real signal, but the merchant's
         URL was not the citation.
      3. NEITHER — score came purely from excerpt-only brand mention
         in answer prose, with no grounded source. Weakest signal:
         the brand was named, but no editorial source backs it up.

    Pre-fix bug (P0-Q1): this helper accepted only `has_title_match`.
    When `has_title_match=False` it ALWAYS said "your URL was used as
    a grounding source" — true for tier 1, FALSE for tier 3. The
    Winona audit (run 932d8261) hit tier 3 (excerpt match on a
    different product variant) but the merchant was told their URL
    was cited. Calling `has_url_grounding` explicitly closes that
    misclaim.

    Default `has_url_grounding=False` for back-compat with the
    handful of test fixtures that haven't migrated; the bare
    title-match call still gets the safer tier-3 narrative.
    """
    if score is None:
        return "we didn't measure category-level presence in this run"
    if has_url_grounding:
        # Tier 1: URL was an actual grounding chunk.
        if score >= 70:
            return (
                f"your URL was used as a grounding source on most "
                f"category-level queries (score {score}/100)"
            )
        return (
            f"your URL was used as a grounding source on some "
            f"category-level queries (score {score}/100)"
        )
    if has_title_match:
        # Tier 2: a grounded source titled the brand, but the URL
        # was not the cited URL.
        return (
            f"your brand was named in some category-level grounded "
            f"source titles (score {score}/100), but your URL itself "
            f"was not the cited source"
        )
    # Tier 3: excerpt-only brand mention, no grounded source.
    return (
        f"your brand was mentioned in category-level answer prose "
        f"(score {score}/100), but no grounded source named your "
        f"brand and your URL was not cited"
    )


def _build_visibility_plain_summary(
    *,
    verdict_label: str,
    visibility_score: int,
    attribution_score: int,
    category_visibility_score: Optional[int],
    category_match_details: Optional[List[Dict[str, Any]]] = None,
    attribution_runs_total: int,
    merchant_cited_runs: int,
    top_retailers: List[str],
) -> str:
    """Merchant-friendly translation of the score combination.

    Honesty rules:
      - Never claim a host is cited "instead of your URL" — only that
        third-party sources were used to ground answers when your
        URL didn't appear. We don't verify whether those sources
        editorially mention your brand or products.
      - Never claim "your brand is recognized" without title-match
        evidence. The brand-recognition phrase is gated on whether
        any category run had a grounded source title containing the
        brand name (`category_match_details[*].title_match`).
      - Never speculate about root causes (Google indexing, etc.) —
        describe what we observed; let the action items propose fixes.
    """
    # P0-Q1: category-evidence flags drive the recognition tier. Pre-fix
    # the helper only checked `has_title_match`, which conflated tier-1
    # (URL grounded) with tier-3 (excerpt-only mention) — the Winona
    # audit (run 932d8261) was told its URL was cited when in fact no
    # grounded source named the brand. Compute BOTH flags now.
    has_title_match = bool(
        category_match_details
        and any(d.get("title_match") for d in category_match_details)
    )
    has_url_grounding = bool(
        category_match_details
        and any(d.get("in_grounding") for d in category_match_details)
    )
    # `top_retailers` is the *category*-scope cited hosts; it does NOT
    # describe what grounded the buyer-intent (attribution) answers,
    # despite being used in buyer-intent prose pre-fix. We gate the
    # retailers_phrase to ONLY render when attribution actually had
    # cited hosts (merchant_cited_runs < attribution_runs_total AND
    # at least one cited host appeared in attribution scope). For now
    # we keep the parameter name as-is for back-compat; a follow-up
    # will plumb attribution-scope hosts through as a distinct kwarg.
    retailers_phrase = ", ".join(top_retailers[:3]) if top_retailers else ""
    not_cited = max(0, attribution_runs_total - merchant_cited_runs)

    if verdict_label == VERDICT_INVISIBLE:
        if attribution_runs_total > 0:
            base = (
                f"Across {attribution_runs_total} buyer-intent queries we "
                f"tested, your URL did not appear in any grounded source"
            )
            # P0-Q1: pre-fix this always said "Gemini grounded its
            # answers in third-party sources including <category
            # retailer hosts>". When attribution had 0 citations on
            # every run, the buyer-intent answers were NOT actually
            # grounded in those hosts — those hosts came from the
            # CATEGORY probe. Only mention third-party grounding when
            # there's at least ONE buyer-intent run that produced a
            # cited host (i.e., merchant_cited_runs > 0 OR we have
            # explicit evidence of buyer-intent third-party citations,
            # which we don't have at this call boundary today).
            # Conservative: when 0 of N attribution runs cited
            # anything, say so.
            if merchant_cited_runs == 0 and attribution_runs_total > 0:
                base += (
                    " and no grounded sources were returned for those "
                    "queries"
                )
            elif retailers_phrase:
                base += (
                    f". Gemini grounded its answers in third-party sources "
                    f"including {retailers_phrase} — we did not verify "
                    f"whether those sources mention your brand or products"
                )
            base += "."
            return base
        return (
            "Across the queries we tested, your URL did not appear in "
            "any grounded source. We didn't gather enough additional "
            "data to characterize what was cited instead."
        )

    if verdict_label == VERDICT_VIA_RETAILERS:
        recognition = _category_recognition_phrase(
            category_visibility_score,
            has_title_match=has_title_match,
            has_url_grounding=has_url_grounding,
        )
        base = (
            f"Mixed. {recognition[0].upper() + recognition[1:]}, but for "
            f"buyer-intent queries your URL appeared in only "
            f"{merchant_cited_runs} of {attribution_runs_total} runs"
        )
        # P0-Q1: only attribute buyer-intent grounding to third-party
        # sources when there was at least one buyer-intent run that
        # actually produced a citation. If 0/N, the answers were
        # ungrounded — saying "the other N grounded in third-party
        # sources" overstates the evidence.
        if not_cited > 0 and merchant_cited_runs > 0 and retailers_phrase:
            base += (
                f". The other {not_cited} grounded answers in third-party "
                f"sources including {retailers_phrase} — we did not verify "
                f"whether those sources mention your brand"
            )
        elif not_cited > 0 and merchant_cited_runs == 0:
            base += (
                f". None of the {attribution_runs_total} buyer-intent runs "
                f"returned a grounded source we could attribute"
            )
        base += "."
        return base

    if verdict_label == VERDICT_MISATTRIBUTED:
        base = (
            f"Partly. Your URL appeared in {merchant_cited_runs} of "
            f"{attribution_runs_total} buyer-intent queries"
        )
        if not_cited > 0 and retailers_phrase:
            base += (
                f". The other {not_cited} grounded answers in third-party "
                f"sources including {retailers_phrase} — we did not verify "
                f"whether those sources mention your brand"
            )
        base += "."
        return base

    if verdict_label == VERDICT_STRONG:
        # Category-aware: when branded buyer-intent is solved but category
        # discovery lags (mirrors classify_primary_gap + _explain_verdict),
        # don't claim a clean "at goal state" — name the open category gap so
        # the headline agrees with next_best_action.
        _cat = category_visibility_score
        if (
            _cat is not None
            and visibility_score >= 50
            and (visibility_score - int(_cat)) >= 25
        ):
            return (
                f"Mostly. AI agents reliably surface your product AND cite "
                f"your URL when shoppers search your brand "
                f"({merchant_cited_runs} of {attribution_runs_total} "
                f"buyer-intent queries) — branded discovery and attribution "
                f"are at goal state. But category visibility is {int(_cat)}/100: "
                f"shoppers who search the category, not your name, still don't "
                f"surface you. That's the open gap."
            )
        return (
            f"Yes. AI agents reliably surface your product AND cite "
            f"your URL as the buying path "
            f"({merchant_cited_runs} of {attribution_runs_total} "
            f"buyer-intent queries). Discovery and direct attribution "
            f"are at goal state."
        )

    # PARTIAL
    return (
        f"Mixed. AI agents sometimes find your product and sometimes "
        f"cite your URL, but neither is consistent — visibility "
        f"{visibility_score}/100, attribution {attribution_score}/100. "
        f"The action items below show the bigger gap to close first."
    )


def _build_competitive_table(
    competitive_pressure: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Flat per-brand rows merging `peers_named` (every competitor
    brand AI agents named) with `peers_with_first_party_visibility`
    (subset whose .com is cited). Frontend renders as a table directly.

    Schema per row:
      brand                    : str
      times_mentioned          : int  — how often the brand was named
      first_party_visible      : bool — does their .com appear in grounded sources?
      first_party_host         : Optional[str]
      host_citations           : int  — how many times their host was cited
    """
    peers_named = (competitive_pressure or {}).get("peers_named") or []
    peers_with_fp = (competitive_pressure or {}).get("peers_with_first_party_visibility") or []
    fp_by_brand: Dict[str, Dict[str, Any]] = {
        (p.get("brand") or "").lower(): p for p in peers_with_fp
    }

    rows: List[Dict[str, Any]] = []
    for entry in peers_named:
        brand = entry.get("name") or ""
        if not brand:
            continue
        fp = fp_by_brand.get(brand.lower())
        rows.append({
            "brand": brand,
            "times_mentioned": int(entry.get("times_cited") or 0),
            "first_party_visible": bool(fp),
            "first_party_host": (fp or {}).get("first_party_host"),
            "host_citations": int((fp or {}).get("host_citations") or 0),
        })
    return rows


def _merchant_id_is_prospect(merchant_id: Optional[str]) -> bool:
    """The honest cold-start discriminator: merchant identity, not the
    integration_state shape.

    A cold-start (BD-outreach) audit runs with NO onboarded merchant
    behind it — the route mints a synthetic ``prospect_<hex>`` id (see
    routes/agent_center_bd_routes.py::_prospect_merchant_id). A REAL
    merchant always has a non-prospect merchant_id, even one who has
    connected nothing yet.

    This matters because a fresh (or lookup-failed) real merchant's
    "totally unintegrated" integration_state — see
    services/merchant_integration_state.get_integration_state, which
    deliberately over-surfaces the integration CTA — is byte-identical
    to the synthetic cold-start shape (store+psp both missing). So
    `_is_cold_start_audit` (shape-only) can't tell them apart and
    over-triggers, wrongly suppressing the "Complete Pivota
    integration" CTA for real incomplete merchants. The merchant_id
    can tell them apart; the shape can't.
    """
    mid = str(merchant_id).strip() if merchant_id is not None else ""
    return (not mid) or mid.startswith("prospect_")


def _is_cold_start_audit(integration_state: Optional[Dict[str, Any]]) -> bool:
    """SHAPE-BASED cold-start fallback. Prefer an explicit `is_cold_start`
    threaded from the merchant identity (`_merchant_id_is_prospect`) —
    this heuristic is ambiguous: a real merchant who has connected
    nothing yet (or whose integration lookup failed) produces the exact
    same "store+psp both missing" shape as a synthetic cold-start
    target, so this returns a false positive for them. Retained only as
    the fallback when no explicit signal is supplied (e.g. direct
    callers that don't know the merchant identity).

    A cold-start audit's integration_state is the synthetic
    "totally unintegrated" shape minted by /bd/cold-start-audit:
    fully_integrated=False AND missing_pieces includes both
    store_platform AND psp. For these targets, history endpoints
    don't exist (no merchant_id) and Pivota's own indexing baseline
    is irrelevant context (the merchant hasn't onboarded yet).
    """
    if not integration_state:
        return False
    if integration_state.get("fully_integrated"):
        return False
    missing = integration_state.get("missing_pieces") or []
    return "store_platform" in missing and "psp" in missing


def _resolve_cold_start(
    is_cold_start: Optional[bool],
    integration_state: Optional[Dict[str, Any]],
) -> bool:
    """Prefer the explicit merchant-identity signal; fall back to the
    ambiguous shape heuristic only when no signal was threaded."""
    if is_cold_start is not None:
        return is_cold_start
    return _is_cold_start_audit(integration_state)


def _build_evidence_quotes(
    category_match_details: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """PR-7e: Extract verbatim Gemini-grounded evidence quotes that
    mention the merchant brand. Filtered to highest-confidence: only
    excerpt-corroborated runs (excerpt + LLM self-report + grounding
    source) qualify, so excerpt-only Gemini paraphrases (the no-name
    1688-case hallucination class) are excluded.

    Output is a list of `{query, excerpt_text, source_labels,
    attribution_path}` for the renderer to display as quote boxes.
    Excerpt text is preserved in original case.

    Truncation rule: keep all qualifying quotes. They're already
    filtered to corroborated matches (typically 0-3 per audit), and
    the renderer can decide to display only the top N if needed.
    """
    if not category_match_details:
        return []
    quotes: List[Dict[str, Any]] = []
    for d in category_match_details:
        excerpt_text = d.get("evidence_excerpt_text")
        if not excerpt_text:
            continue
        # Only surface excerpts where the brand was named AND
        # corroborated by LLM self-report + grounding source. Excerpt-
        # match alone falls through into the no-quote bucket — those
        # are likely Gemini paraphrasing, not actual editorial
        # citations.
        if not d.get("excerpt_corroborated_match"):
            continue
        # Truncate very long excerpts (>500 chars) to keep quote-box
        # rendering readable; preserve enough context for credibility.
        if len(excerpt_text) > 500:
            excerpt_text = excerpt_text[:497].rstrip() + "..."
        quotes.append({
            "query": d.get("query") or "",
            "excerpt_text": excerpt_text,
            "source_labels": d.get("source_labels") or [],
            "attribution_path": "merchant_named_in_grounded_excerpt",
        })
    return quotes


def _build_tracking_block(
    *,
    prior_runs: Optional[List[Dict[str, Any]]],
    integration_state: Optional[Dict[str, Any]],
    pivota_baseline: Dict[str, Any],
    your_gap_to_baseline: Dict[str, int],
    current_scores: Optional[Dict[str, Any]] = None,
    is_cold_start: Optional[bool] = None,
) -> Dict[str, Any]:
    """Tracking block for merchant_view.

    PR-11 (post-Grüns review): pivota_baseline_reference is now
    populated for BOTH cold-start and onboarded audits, but with
    `pitch_framing` context that lets the renderer present it as
    forward-looking pitch material rather than a damning "0/0
    baseline" headline. Prior behavior was to null this for cold-
    start to avoid anti-pitch framing — but that left renderers
    with nothing to show, and the surrounding `what_pivota_changes`
    narrative referenced a baseline number that never appeared in
    structured data.

    `your_gap_to_baseline` remains cold-start-null because the
    "your scores minus baseline" math has no meaning before the
    merchant has been audited as an onboarded merchant.

    Other fields:
      - history_link → cold targets have no merchant_id, no history
        endpoint access; remains gated.
      - history → built from prior_runs whenever those exist (even
        for cold-start prospect ids). PR-1a re-audit trend works
        cross-cold-start.
    """
    cold_start = _resolve_cold_start(is_cold_start, integration_state)
    block: Dict[str, Any] = {
        "next_audit_eligible_at": None,
        "history_link": None if cold_start else "/api/merchant-center/audit/history",
        "history": _build_history_trend(prior_runs, current_scores=current_scores),
        "your_gap_to_baseline": your_gap_to_baseline if not cold_start else None,
    }
    # Always populate baseline reference. The pitch_framing field
    # tells the renderer how to present it (positive forward-looking
    # for indexing-up phase vs comparable-to-merchant for steady-
    # state). Renderers should prefer pitch_framing over the raw
    # numeric baseline when it's available.
    indexing_phase = pivota_baseline.get("indexing_phase") or "steady-state"
    block["pivota_baseline_reference"] = {
        "visibility": pivota_baseline.get("median_visibility"),
        "attribution": pivota_baseline.get("median_attribution"),
        "as_of": pivota_baseline.get("as_of_date"),
        "indexing_phase": indexing_phase,
        "sample_size_pdps": pivota_baseline.get("sample_size_pdps"),
        "pitch_framing": _baseline_pitch_framing(
            indexing_phase=indexing_phase,
            cold_start=cold_start,
            baseline_visibility=pivota_baseline.get("median_visibility") or 0,
            baseline_attribution=pivota_baseline.get("median_attribution") or 0,
        ),
    }
    return block


def _baseline_pitch_framing(
    *,
    indexing_phase: str,
    cold_start: bool,
    baseline_visibility: int,
    baseline_attribution: int,
) -> Dict[str, str]:
    """Forward-looking framing for the Pivota canonical-PDP baseline.
    Renderer uses this in place of just showing the raw baseline
    number, so a 0/0 baseline reads as a 30-90 day Google indexing
    arc (correct) rather than as a Pivota failure (wrong)."""
    if indexing_phase == "indexing-up":
        if cold_start:
            return {
                "headline": (
                    "Pivota canonical PDPs are in the typical 30-90 day "
                    "Google indexing arc post-publication. Mechanics "
                    "(canonical PDP + Schema.org + sitemap + Search "
                    "Console URL Inspection cadence) are shipped; "
                    "Google's crawl latency is the rate-limiting step "
                    "before grounded-citation lift."
                ),
                "what_to_expect_post_onboarding": (
                    "After 30-90 days of co-investment with Pivota's "
                    "Search Console URL Inspection cadence, your "
                    "canonical PDPs progress through the indexing arc "
                    "and surface in grounded LLM answers for the "
                    "category queries you should own."
                ),
                "honest_caveat": (
                    "Pivota's own canonical PDPs currently surface "
                    f"{baseline_visibility}/{baseline_attribution} "
                    "in Gemini grounded retrieval — this is the "
                    "indexing-up phase reality, not steady-state. "
                    "Steady-state benchmarks will replace this anchor "
                    "as Pivota's seeded PDPs mature."
                ),
            }
        # Onboarded merchant in indexing-up phase
        return {
            "headline": (
                "Pivota canonical PDPs for your catalog are in the "
                "30-90 day Google indexing arc. Mechanics shipped; "
                "indexing latency is the rate-limiting step."
            ),
            "what_to_expect_post_onboarding": (
                "Re-audit at 30 and 90 days post-onboarding for "
                "paired before/after lift on the same SKUs."
            ),
            "honest_caveat": (
                f"Today's baseline: {baseline_visibility}/"
                f"{baseline_attribution}. Steady-state benchmarks "
                "will replace this as PDPs mature past the indexing "
                "arc."
            ),
        }
    # Steady-state phase
    return {
        "headline": (
            f"Pivota canonical PDPs (current baseline: "
            f"{baseline_visibility}/{baseline_attribution}) provide "
            "a comparable benchmark for AI-channel attribution at "
            "steady state."
        ),
        "what_to_expect_post_onboarding": (
            "Your scores after 30-90 days of Pivota onboarding can "
            "be compared directly against this steady-state baseline."
        ),
        "honest_caveat": "",
    }


def _build_merchant_view(
    *,
    verdict_label: str,
    verdict_explanation: str,
    visibility_score: int,
    attribution_score: int,
    category_visibility_score: Optional[int],
    category_match_details: Optional[List[Dict[str, Any]]] = None,
    industry_context: Dict[str, Any],
    action_items: List[Dict[str, Any]],
    competitive_pressure: Dict[str, Any],
    what_pivota_changes: Dict[str, Any],
    attribution_runs: List[Dict[str, Any]],
    merchant_cited_runs: int,
    competitor_hosts_list: List[Dict[str, Any]],
    category_retailer_hosts: List[Dict[str, Any]],
    category_competitor_brands: List[Dict[str, Any]],
    visibility_query_rows: List[Dict[str, Any]],
    attribution_query_rows: List[Dict[str, Any]],
    url_source: Optional[str],
    merchant_brand: Optional[str],
    merchant_host: Optional[str],
    merchant_storefront_name: Optional[str] = None,
    prior_runs: Optional[List[Dict[str, Any]]] = None,
    pivota_signature_minted_at: Optional[datetime] = None,
    integration_state: Optional[Dict[str, Any]] = None,
    is_cold_start: Optional[bool] = None,
) -> Dict[str, Any]:
    """Project the already-computed structured report into a 6-layer
    information architecture the merchant portal can render directly:
    headline → receipts → diagnosis → actions → tracking → pivota_value_prop.

    Pure projection — does not re-extract evidence or recompute scores.
    Every field references data the engine already produced. This is
    additive: existing top-level keys (`verdict`, `industry_context`,
    `action_items`, `competitive_pressure`, `what_pivota_changes`,
    `visibility`, `attribution`, `category_visibility`) remain
    untouched so the BD-side renderer keeps working.

    `url_source` flags whether THIS product's audit was probed against
    the merchant's own URL or the Pivota canonical PDP fallback (see
    `routes/merchant_audit_routes.py`'s 3-tier fallback chain). Used
    to render a "audited via Pivota canonical surface" indicator.
    Indexing-arc state computation lands in PR-D — for now the
    `diagnosis.indexing_arc_state` field is a static caveat.
    """
    # Cold-start is a property of merchant identity (BD prospect vs a
    # real onboarded merchant), NOT of the integration_state shape — a
    # real merchant who has connected nothing is byte-identical in
    # shape to a synthetic cold-start target. Prefer the explicit signal
    # threaded from the caller (which knows the merchant_id); fall back
    # to the ambiguous shape heuristic only when it's absent.
    cold_start = _resolve_cold_start(is_cold_start, integration_state)

    headline_one_liner = (verdict_explanation or "").split(".")[0].strip()
    if headline_one_liner and not headline_one_liner.endswith("."):
        headline_one_liner += "."

    # Top cited URLs across the attribution runs (collapses per-run dups
    # into a host frequency view sorted by count). Distinct from
    # `competitor_hosts` because it includes the merchant's own URL when
    # cited — so the receipts block honestly answers "who got cited?"
    # rather than only "who beat me?".
    top_cited_urls = []
    for row in attribution_query_rows:
        url = row.get("top_cited_url")
        if url:
            top_cited_urls.append(url)
    # Dedupe preserving order, cap at 5.
    seen: set = set()
    top_cited_urls_unique = []
    for u in top_cited_urls:
        if u in seen:
            continue
        seen.add(u)
        top_cited_urls_unique.append(u)
        if len(top_cited_urls_unique) >= 5:
            break

    pivota_baseline = dict(PIVOTA_PDP_BASELINE_REFERENCE)
    your_gap_to_baseline = {
        "visibility": visibility_score - int(pivota_baseline.get("median_visibility") or 0),
        "attribution": attribution_score - int(pivota_baseline.get("median_attribution") or 0),
    }

    audited_via_pivota_canonical = (url_source == "pivota_canonical_pdp")

    # Hoist the enriched receipt blocks so we can both (a) put them in
    # merchant_view.receipts and (b) feed them into the playbook engine
    # for action generation. Computed once per report.
    merchant_category = (industry_context or {}).get("category")
    failed_queries_detailed = _build_failed_queries_detailed(
        attribution_runs,
        merchant_brand=merchant_brand,
        merchant_host=merchant_host,
        merchant_category=merchant_category,
    )
    cited_hosts_detailed_full = [
        h for h in classify_cited_hosts(
            category_retailer_hosts or [],
            merchant_category=merchant_category,
        )
        if not _is_cdn_cited_host(h)
    ]

    # Phase C-4 PR-G: per-cited-host playbook actions. Strategic
    # actions from `_generate_action_items` (verdict-tier-based) lead;
    # per-host playbook actions come after, sorted by severity. Each
    # playbook action carries `playbook_step_id + target_host + lever
    # + expected_timeline_weeks` so the frontend can group/filter.
    # Phase A: also passes merchant_name + merchant_category so the
    # playbook engine can render `pitch_draft` (pre-filled email)
    # per editorial action.
    # Q-P1-6: thread the actual audit scores into the playbook engine
    # so the severity scorer can calibrate the playbook's authored
    # severity against this audit's evidence (score gap, named
    # competitors, failed-query examples). Pre-fix the playbook's
    # severity was a fixed value from data/playbooks.json; the
    # merchant saw "high" on pitch actions whose audit-evidence
    # didn't support a high tier (the Winona whowhatwear case).
    playbook_actions = select_playbooks(
        cited_hosts_detailed=cited_hosts_detailed_full,
        failed_queries_detailed=failed_queries_detailed,
        merchant_name=merchant_brand,  # use the friendly brand name
        merchant_category=merchant_category,
        attribution_score=attribution_score,
        category_score=category_visibility_score,
    )
    merged_actions = list(action_items or []) + list(playbook_actions or [])

    # Phase 0: when integration is incomplete, the audit's #1 action
    # is "Complete Pivota integration" — prepended ahead of all
    # strategic + playbook actions. When fully integrated, no
    # integration action is emitted; existing actions take over.
    #
    # Cold-start exception (BD employee portal): for cold targets
    # the BD operator isn't going to onboard the merchant from this
    # dashboard — the integration pitch belongs in pivota_value_prop
    # (rendered as a separate "How Pivota addresses these gaps"
    # panel), NOT as the #1 diagnostic action. Skip the prepend so
    # the action ladder stays purely diagnostic. The pitch content
    # is unchanged in pivota_value_prop further below.
    if integration_state is not None and not cold_start:
        from services.merchant_integration_state import build_integration_action
        integration_action = build_integration_action(integration_state)
        if integration_action is not None:
            merged_actions = [integration_action] + merged_actions
        else:
            # Phase D scaffolding: when Phase 0 is satisfied (store +
            # PSP done) but GSC isn't connected yet, surface the GSC
            # integration as a SECONDARY action high in the list. Not
            # critical-tier — it's the next step for a merchant who's
            # already onboarded.
            if not integration_state.get("gsc_integrated"):
                from services.gsc_integration import build_gsc_integration_action
                merged_actions = [build_gsc_integration_action()] + merged_actions

    # Phase E scaffolding: creator marketplace match. When the audit
    # surfaced named competitors (category_competitor_brands) AND
    # we have BD-curated creator candidates in the merchant's
    # category, emit a creator-partnership action. Returns None
    # silently when the database is empty — better to omit than to
    # fabricate candidate creators. This action slots in AFTER
    # integration actions (those are the highest leverage when
    # un-integrated) but BEFORE per-host playbook actions (it's a
    # category-wide play, not host-specific).
    if category_competitor_brands:
        from services.creator_matcher import (
            build_creator_partnership_action,
            match_creators,
        )
        creator_matches = match_creators(
            merchant_category=merchant_category,
            competitor_brands=[
                (b.get("name") or "")
                for b in category_competitor_brands
                if isinstance(b, dict)
            ],
        )
        creator_action = build_creator_partnership_action(
            matches=creator_matches,
            merchant_category=merchant_category,
        )
        if creator_action is not None:
            # Insert after any integration actions but before strategic
            # + playbook actions. Find first non-integration action
            # index and insert there.
            insertion_idx = 0
            for idx, existing in enumerate(merged_actions):
                if (existing.get("lever") or "") not in (
                    "pivota_integration", "gsc_integration"
                ):
                    insertion_idx = idx
                    break
                insertion_idx = idx + 1
            merged_actions = (
                merged_actions[:insertion_idx]
                + [creator_action]
                + merged_actions[insertion_idx:]
            )

    # Phase C scaffolding: when multi-market is enabled and the audit
    # has per-market results (set by upstream when wired up), surface
    # the localization action. Returns None when flag is off or
    # results are insufficient to draw a gap conclusion — better to
    # omit than to fabricate. Slots in alongside other category-wide
    # plays (Phase E creator_partnership), before strategic + playbook
    # actions.
    from services.multi_market_audit import (
        build_localization_action,
        build_markets_aggregate,
        empty_markets_aggregate,
    )
    markets_aggregate_for_actions = empty_markets_aggregate()
    localization_action = build_localization_action(
        markets_aggregate=markets_aggregate_for_actions,
    )
    if localization_action is not None:
        insertion_idx = 0
        for idx, existing in enumerate(merged_actions):
            if (existing.get("lever") or "") not in (
                "pivota_integration", "gsc_integration",
                "creator_partnership",
            ):
                insertion_idx = idx
                break
            insertion_idx = idx + 1
        merged_actions = (
            merged_actions[:insertion_idx]
            + [localization_action]
            + merged_actions[insertion_idx:]
        )

    # Stamp a 1-indexed `priority_order` on every action so the
    # frontend can render "Step 1, Step 2..." without re-deriving the
    # ordering. The integration action (if present) is at index 0,
    # strategic actions next, per-host playbook actions last.
    for i, a in enumerate(merged_actions, start=1):
        a["priority_order"] = i

    # PR-8b: enrich action items with v2 execution metadata (owner,
    # phase, kpi_to_track, expected_outcome, depends_on). Mutates in
    # place; renderer surfaces these alongside title/body/severity.
    # Runs LAST so any explicit values (test fixtures, future
    # per-action hand-tuning) take precedence — the enricher only
    # fills missing fields.
    _enrich_action_items_v2(merged_actions)

    plain_summary = _build_visibility_plain_summary(
        verdict_label=verdict_label,
        visibility_score=visibility_score,
        attribution_score=attribution_score,
        category_visibility_score=category_visibility_score,
        category_match_details=category_match_details,
        attribution_runs_total=len(attribution_runs or []),
        merchant_cited_runs=merchant_cited_runs,
        top_retailers=[
            h.get("host")
            for h in _retail_cited_hosts(category_retailer_hosts)[:3]
            if h.get("host")
        ],
    )
    competitive_table = _build_competitive_table(competitive_pressure or {})

    # Brand-vs-vendor disambiguation. For drop-shippers / wholesale
    # merchants whose product `vendor` is a sourcing-platform brand
    # (e.g. "guiruo" from a 1688-sourced PDP) NOT the storefront's
    # legal brand identity, "your brand" prose attributes signals to
    # the wrong entity from the merchant's perspective. Surface both
    # so the portal can clarify which brand the audit was probed
    # against.
    brand_audited_against = (merchant_brand or "").strip() or None
    storefront_name = (merchant_storefront_name or "").strip() or None
    brand_vendor_diverges = bool(
        brand_audited_against
        and storefront_name
        and brand_audited_against.lower() != storefront_name.lower()
    )

    merchant_view = {
        "headline": {
            "verdict_label": verdict_label,
            # Client-facing softer rendering of verdict_label. Used by
            # the report renderer in place of the bare technical label
            # ("INVISIBLE" reads as a damning summary verdict to a
            # famous brand even when the audit specifically measures
            # only Layer 1 grounded LLM citation, which is one channel
            # among many). The raw `verdict_label` is preserved for
            # downstream code that branches on the enum.
            "verdict_label_display": _verdict_display_label(
                verdict_label, cited_runs=merchant_cited_runs
            ),
            "one_liner": headline_one_liner or None,
            # Plain-language answer to "Am I visible to AI users or
            # not?" — translates the score combination into one short
            # paragraph the merchant can read without parsing the
            # vis/attr/category math. Distinct from `verdict.explanation`
            # (technical diagnostic) — see _build_visibility_plain_summary.
            "plain_summary": plain_summary,
            "scores": {
                "visibility": visibility_score,
                "attribution": attribution_score,
                "category_visibility": category_visibility_score,
            },
            "what_is_at_stake": industry_context.get("blurb"),
            "audited_via_pivota_canonical": audited_via_pivota_canonical,
            "url_source": url_source,
            "brand_disambiguation": (
                {
                    "brand_audited_against": brand_audited_against,
                    "storefront_name": storefront_name,
                    "note": (
                        "The audit probes were issued for the product's "
                        "vendor field — not the storefront name. If the "
                        "two represent different brand identities, treat "
                        "claims about 'your brand' as referring to the "
                        "vendor."
                    ),
                }
                if brand_vendor_diverges
                else None
            ),
        },
        "receipts": {
            "queries_tested": len(attribution_runs or []),
            "merchant_cited_in": merchant_cited_runs,
            "top_cited_urls": top_cited_urls_unique,
            # Phase C-4 PR-F: per-failed-query winner attribution.
            # Each entry: {query, top_cited_url, top_cited_host,
            # host_classification, competitors_named}. Closes the gap
            # between "your URL was missing on N queries" and "for THIS
            # query, this host won, with these competitor brands".
            "failed_queries_detailed": failed_queries_detailed,
            # Honest naming: these are "all hosts cited in grounded
            # sources except the merchant's own". Could be retailers
            # (nordstrom.com, sephora.com), competitor brand .coms
            # (serenaandlily.com), or editorial/review sites
            # (businessinsider.com, forbes.com).
            "top_cited_hosts": [
                h.get("host")
                for h in _copyworthy_cited_hosts(category_retailer_hosts)[:5]
                if h.get("host")
            ],
            # Phase C-4 PR-E: each cited host annotated with type +
            # coverage_note + outreach_hint pulled from the BD-curated
            # `data/cited_host_registry.json`. Unknown hosts get
            # `type: "unclassified"`. Frontend renders alongside
            # `top_cited_hosts` so merchants understand what each host
            # is and which lever applies. PR-G turns these annotations
            # into per-host playbook actions (in `actions` block).
            "cited_hosts_detailed": cited_hosts_detailed_full[:5],
            "top_competitor_brands": [
                b.get("name")
                for b in (category_competitor_brands or [])[:5]
                if b.get("name")
            ],
            # Flat per-brand rows merging peers_named + peers_with_
            # first_party_visibility — frontend renders as a table.
            # Each row: {brand, times_mentioned, first_party_visible,
            # first_party_host, host_citations}.
            "competitive_table": competitive_table,
            # Phase C scaffolding: per-market audit scores. Empty
            # aggregate when multi-market is disabled (default), so
            # portal can null-check `enabled`. Per-market dispatch
            # itself lands in a follow-up PR after staging load test.
            "markets": markets_aggregate_for_actions,
        },
        "diagnosis": {
            "primary": (competitive_pressure or {}).get("framing"),
            # PR-D: real arc phase computed from this product's
            # `pivota_signature_minted_at` (set by go-forward sync or
            # lazy-mint at first audit). Phases:
            #   fresh           — < 7 days since mint
            #   indexing        — 7-90 days (Google crawl + first-pass)
            #   expected_steady — > 90 days (if not cited by now, the
            #                     diagnostic shifts from "wait" to
            #                     "your URL doesn't win the queries")
            # Falls back to a generic caveat when minted_at is missing
            # (legacy rows the migration backfill couldn't reach).
            "indexing_arc_state": (
                compute_indexing_arc_state(pivota_signature_minted_at)
                if audited_via_pivota_canonical
                else None
            ),
        },
        "actions": merged_actions,
        "tracking": _build_tracking_block(
            prior_runs=prior_runs,
            integration_state=integration_state,
            pivota_baseline=pivota_baseline,
            your_gap_to_baseline=your_gap_to_baseline,
            current_scores={
                "visibility": visibility_score,
                "attribution": attribution_score,
                "category_visibility": category_visibility_score,
            },
            is_cold_start=cold_start,
        ),
        "pivota_value_prop": what_pivota_changes,
    }
    merchant_view["next_best_action"] = build_next_best_action(
        merchant_view=merchant_view,
        competitive_pressure=competitive_pressure,
        integration_state=integration_state,
        is_cold_start=cold_start,
    )
    return merchant_view


def build_structured_report(
    *,
    merchant_name: str,
    merchant_pdp_url: str,
    product_title: str,
    product_vendor: Optional[str],
    product_type: Optional[str],
    visibility_result: Dict[str, Any],
    attribution_result: Dict[str, Any],
    provider: str,
    timestamp: Optional[str] = None,
    category_visibility_result: Optional[Dict[str, Any]] = None,
    url_source: Optional[str] = None,
    prior_runs: Optional[List[Dict[str, Any]]] = None,
    pivota_signature_minted_at: Optional[datetime] = None,
    # Phase 0: when present + integration is incomplete, the audit's
    # #1 action becomes "Complete Pivota integration". Computed once
    # by the route handler and threaded down so per-product reports
    # share the same state.
    integration_state: Optional[Dict[str, Any]] = None,
    # PR-7a: brand_context dict from upstream infer_brand_context
    # call. When provided, the executive summary builder can quote
    # corporate intel ("Unilever-owned brand") in the opening
    # paragraph. Cold-start audits that didn't call infer_brand_context
    # pass None — narrative falls back to non-corporate framing.
    brand_context: Optional[Dict[str, Any]] = None,
    # Explicit cold-start signal derived from merchant identity by the
    # caller (run_brand_report computes it from merchant_id). When None,
    # we fall back to the ambiguous integration_state shape heuristic —
    # which false-positives for real merchants who've connected nothing
    # yet. Threading the truthful value keeps the "Complete Pivota
    # integration" CTA firing for real incomplete merchants while still
    # routing the pitch to pivota_value_prop for BD cold-start targets.
    is_cold_start: Optional[bool] = None,
) -> Dict[str, Any]:
    """Return a single JSON-serializable dict with everything the UI
    needs to render the BD report. Pure function.

    `category_visibility_result` is optional (Phase 2a) — when provided,
    the report exposes a `category_visibility` block with score + queries,
    and `verdict.category_visibility_score` for downstream consumers."""
    # Resolve cold-start once from the explicit merchant-identity signal
    # (falling back to the shape heuristic), then thread the SAME value
    # to every cold-start-sensitive builder so a report can't be
    # cold-start in one section and onboarded in another.
    _report_cold_start = _resolve_cold_start(is_cold_start, integration_state)
    visibility_score = (visibility_result.get("scores") or {}).get("visibility_score", 0)
    attribution_score = (attribution_result.get("scores") or {}).get("visibility_score", 0)
    visibility_runs = visibility_result.get("raw_runs") or []
    attribution_runs = attribution_result.get("raw_runs") or []

    # Category visibility (optional, Phase 2a)
    category_score: Optional[int] = None
    category_runs: List[Dict[str, Any]] = []
    category_match_details: List[Dict[str, Any]] = []
    category_competitor_brands: List[Dict[str, Any]] = []
    category_retailer_hosts: List[Dict[str, Any]] = []
    upstream_category_score = None
    if category_visibility_result:
        upstream_category_score = (
            category_visibility_result.get("scores") or {}
        ).get("visibility_score", 0)
        category_runs = category_visibility_result.get("raw_runs") or []

    merchant_host = normalize_host(merchant_pdp_url)
    # Prefer the explicit vendor; fall back to merchant_name. Brand-name
    # matching against grounding chunk titles ("Beauty of Joseon Official
    # Store" → matches brand "Beauty of Joseon") is what catches
    # attribution through Vertex AI's redirector wrapper.
    merchant_brand = (product_vendor or merchant_name or "").strip() or None
    merchant_identities = _merchant_identity_tuple(
        merchant_name,
        product_vendor,
        brand_context,
    )
    competitors, merchant_cited_runs, runs_with_any_citation = extract_cited_hosts(
        attribution_runs,
        merchant_host=merchant_host,
        merchant_brand=merchant_brand,
        merchant_vendors=merchant_identities,
    )
    # W1 RunFacts phase 1 — compute the fact layer ONCE for this report's
    # attribution runs and stamp it on the payload below. Every displayed
    # number still comes from the legacy implementations; the parity_check
    # calls are log-only drift probes (grep RUNFACTS_PARITY_DRIFT) that gate
    # the phase-2 cutover.
    run_facts = compute_run_facts(
        attribution_runs,
        merchant_host=merchant_host,
        merchant_brand=merchant_brand,
        merchant_vendors=merchant_identities,
    )
    parity_check(
        "bd_report.extract_cited_hosts.merchant_cited_runs",
        merchant_cited_runs,
        run_facts.brand_mentioned_runs,
        context={"merchant": merchant_name, "product": product_title},
    )
    parity_check(
        "bd_report.extract_cited_hosts.runs_with_any_citation",
        runs_with_any_citation,
        run_facts.runs_with_citations,
        context={"merchant": merchant_name, "product": product_title},
    )
    # W1 CUTOVER (verdict surface): the verdict + every downstream consumer now
    # reads the citedness counts from the single RunFacts fold — parity-proven ==
    # the legacy extract_cited_hosts values just checked (the parity_check calls
    # above stay as runtime tripwires; test_audit_facts.test_parity_with_legacy_
    # implementations is the hard net). This closes the contradiction class for
    # the verdict: it now shares ONE fact with build_channel_appearance (rewired
    # #1151), so "cite your URL N/M" can't disagree with "own site X/M".
    # extract_cited_hosts still supplies `competitors`.
    merchant_cited_runs = run_facts.brand_mentioned_runs
    runs_with_any_citation = run_facts.runs_with_citations

    # Re-score category from raw_runs so brand text-matches in
    # evidence excerpts and grounding source titles count as positive
    # signal. Also pull the rich competitor data Gemini returns on
    # category queries (was being dropped). Done here, post-probe, so
    # we don't need an upstream re-deploy.
    if category_runs:
        category_score, category_match_details = score_category_visibility(
            category_runs,
            merchant_host=merchant_host,
            merchant_brand=merchant_brand,
            merchant_vendors=merchant_identities,
        )
        # W1 site 4 (measure): the score's source-walk component (title_match)
        # vs T3 over the same category runs + identity. Quantifies the
        # word-boundary-vs-substring matcher gap; the in_grounding/excerpt
        # paths are context (they have no source-walk analogue by design).
        _cat_facts = compute_run_facts(
            category_runs,
            merchant_host=merchant_host,
            merchant_brand=merchant_brand,
            merchant_vendors=merchant_identities,
        )
        parity_measure(
            "bd_report.score_category_visibility.title_match_runs",
            sum(1 for d in category_match_details if d.get("title_match")),
            _cat_facts.brand_mentioned_runs,
            context={
                "merchant": merchant_name,
                "matched_total": sum(
                    1 for d in category_match_details if d.get("matched")
                ),
                "in_grounding": sum(
                    1 for d in category_match_details if d.get("in_grounding")
                ),
                "runs": len(category_runs),
            },
        )
        category_competitor_brands, category_retailer_hosts = (
            extract_category_competitors(
                category_runs,
                merchant_host=merchant_host,
                merchant_brand=merchant_brand,
                merchant_vendors=merchant_identities,
            )
        )
    elif category_visibility_result is not None:
        # Probe ran but returned no runs — keep score=0 for consistency.
        category_score = 0

    # Build competitive_pressure first — its `framing` string is folded
    # into the verdict explanation as the second sentence (when
    # available) so we don't repeat the analysis in two places.
    competitive_pressure = _build_competitive_pressure(
        category_competitor_brands=category_competitor_brands,
        category_retailer_hosts=category_retailer_hosts,
        merchant_brand=merchant_brand,
        merchant_host=merchant_host,
        merchant_attribution_score=attribution_score,
    )

    # Evidence dict for verdict_for — references THIS audit's actual
    # numbers (top retailers, failed query sample, gap pct, peer
    # framing) so the explanation paragraph names real things instead
    # of falling back to generic prose.
    top_cited_hosts = _copyworthy_cited_hosts(category_retailer_hosts)[:5]
    # `top_retailers` stays as a string-list alias for older evidence
    # consumers. It is now deliberately retail/marketplace-only; typed
    # `top_cited_hosts` is the source of truth for neutral host labels.
    top_retailer_hosts = [
        r["host"] for r in _retail_cited_hosts(top_cited_hosts) if r.get("host")
    ]
    gap_pct = (
        max(0, category_score - attribution_score)
        if category_score is not None
        else None
    )
    # Honest own-page citation count (own domain resolved as a cited source),
    # distinct from merchant_cited_runs (brand-mention OR listing). Gates the
    # STRONG "cite your URL / goal state" copy so it can't fire on mentions
    # alone — see _explain_verdict.
    # W1 CUTOVER: own-page citation count from the RunFacts fold (parity-proven
    # == legacy _own_url_cited_runs; the equivalence test is the net). One source.
    own_url_cited = run_facts.own_url_cited_runs
    verdict_evidence: Dict[str, Any] = {
        "attribution_runs_total": len(attribution_runs),
        "merchant_cited_runs": merchant_cited_runs,
        "own_url_cited_runs": own_url_cited,
        "top_retailers": top_retailer_hosts,
        "top_cited_hosts": top_cited_hosts,
        "competitive_pressure_framing": (competitive_pressure or {}).get("framing"),
        "category_score": category_score,
        "gap_pct": gap_pct,
        "failed_attribution_query_sample": _failed_attribution_queries(attribution_runs)[:3],
        # Per-run category match flags (in_grounding / title_match /
        # excerpt_match). VIA_RETAILERS prose uses these to gate the
        # "Your URL appears..." claim — only true when at least one
        # matched run had in_grounding=True. title_match-only signal
        # means brand was named in a source title but the URL itself
        # did not appear.
        "category_match_details": category_match_details,
        # Disambiguates "your URL" in the verdict text. When the audit
        # fell back to the Pivota canonical PDP, "your URL" reads as
        # "your store URL" to the merchant — but they don't HAVE one;
        # we tested the Pivota canonical sig_*. Verdict text now says
        # "Your Pivota canonical URL was cited..." for that case.
        "url_source": url_source,
    }
    verdict_label, verdict_explanation = verdict_for(
        visibility_score,
        attribution_score,
        category_visibility_score=category_score,
        evidence=verdict_evidence,
    )

    # Critical for credibility: surface what the upstream ACTUALLY used,
    # not just what was requested. A silent fallback to mock looks
    # identical to a real run in the UI without this.
    visibility_actual = (visibility_result.get("provider") or "").strip()
    attribution_actual = (attribution_result.get("provider") or "").strip()
    # Take the most-degraded of the two — if either fell back to mock, the
    # whole report is suspect.
    actual_provider_for_status = (
        visibility_actual
        if visibility_actual not in _REAL_PROVIDERS
        else attribution_actual
    )
    upstream_status = _classify_provider(actual_provider_for_status)
    upstream_status["requested_provider"] = provider
    upstream_status["visibility_provider"] = visibility_actual
    upstream_status["attribution_provider"] = attribution_actual

    def _per_query_rows(runs: List[Dict[str, Any]], judge_key: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for run in runs:
            parsed = run.get("parsed") or {}
            chunks = run.get("grounding_chunks") or []
            rows.append({
                "query": (run.get("query") or "").strip(),
                "self_report_yes": bool(parsed.get(judge_key)),
                "top_cited_url": (chunks[0] if chunks else None),
                "cited_urls_count": len(chunks),
            })
        return rows

    competitor_hosts_list = [
        {"host": h, "times_cited": c}
        for h, c in competitors.most_common(15)
    ]
    # PR-7e: extract verbatim Gemini-grounded evidence quotes that
    # mention the merchant brand by name with editorial-grade
    # corroboration. These power the renderer's "evidence quote box"
    # surface — the highest-leverage element in a polished audit
    # because they're verbatim what Gemini said about the brand.
    evidence_quotes = _build_evidence_quotes(category_match_details)
    action_items = _generate_action_items(
        verdict_label=verdict_label,
        visibility_runs=visibility_runs,
        attribution_runs=attribution_runs,
        competitor_hosts=competitor_hosts_list,
        merchant_cited_runs=merchant_cited_runs,
        runs_with_any_citation=runs_with_any_citation,
        visibility_score=visibility_score,
        attribution_score=attribution_score,
        # Q-P1-6 PR-6: scorer needs this to compute score_gap_pct.
        # `category_score` is Optional[int]; pass it through as-is so a
        # MEASURED 0 (brand absent from every category query, e.g. Ownist)
        # is distinguishable from "probe didn't run" (None). The score_gap_pct
        # logic treats both None and 0 as falsy (base passthrough), unchanged;
        # only the category-discovery action item needs the distinction.
        category_visibility_score=category_score,
        category_retailer_hosts=category_retailer_hosts,
        category_competitor_brands=category_competitor_brands,
    )
    industry_context = _industry_context_for(
        product_type=product_type,
        product_vendor=product_vendor,
        product_title=product_title,
    )

    # PR-7b: form factor + price band classification. Per-product
    # classification is keyword-deterministic (gummy/powder/capsule/
    # liquid/etc.); price band is bucketed from numeric price when
    # available. Cohort-level summary surfaces "merchant is only
    # gummy in cohort" insights via the form_factor_summary field.
    from services.product_form_factor_classifier import (
        build_cohort_form_factor_summary,
        classify_product,
    )
    merchant_product_classification = classify_product(
        product_title=product_title,
        product_type=product_type,
    )
    cohort_form_factor = build_cohort_form_factor_summary(
        merchant_brand=merchant_brand,
        merchant_form_factor=merchant_product_classification.get("form_factor"),
        competitor_brands=category_competitor_brands or [],
        cohort_audit_runs=None,  # cohort runs aren't in scope here;
                                  # cohort orchestrator passes them
                                  # separately via the cohort comparison
                                  # endpoint
    )
    # PR-10d: resolve merchant_platform once (used by both
    # _build_what_pivota_changes and build_pivota_commitments below)
    # so the checkout_loop chain + outcome read for THIS merchant's
    # platform instead of the legacy Shopify-only language.
    _merchant_platform = (
        (integration_state or {}).get("store_platform_name")
        if integration_state else None
    )
    # `provider` is the resolved grounded-shopping profile label
    # (provider_label = single id or comma-joined profile_providers). Parse it
    # back to a list so the Layer-1 methodology copy names the models actually
    # run instead of a hardcoded "Gemini".
    _resolved_providers = [
        part.strip().lower()
        for part in str(provider or "").split(",")
        if part and part.strip()
    ]
    what_pivota_changes = _build_what_pivota_changes(
        merchant_name=merchant_name,
        merchant_pdp_url=merchant_pdp_url,
        attribution_score=attribution_score,
        attribution_runs=len(attribution_runs),
        merchant_cited_runs=merchant_cited_runs,
        category_retailer_hosts=category_retailer_hosts,
        category_visibility_score=category_score,
        merchant_platform=_merchant_platform,
        providers=_resolved_providers,
    )

    visibility_query_rows = _per_query_rows(visibility_runs, "product_visible")
    attribution_query_rows = _per_query_rows(attribution_runs, "merchant_url_found")

    merchant_view = _build_merchant_view(
        verdict_label=verdict_label,
        verdict_explanation=verdict_explanation,
        visibility_score=visibility_score,
        attribution_score=attribution_score,
        category_visibility_score=category_score,
        category_match_details=category_match_details,
        industry_context=industry_context,
        action_items=action_items,
        competitive_pressure=competitive_pressure,
        what_pivota_changes=what_pivota_changes,
        attribution_runs=attribution_runs,
        merchant_cited_runs=merchant_cited_runs,
        competitor_hosts_list=competitor_hosts_list,
        category_retailer_hosts=category_retailer_hosts or [],
        category_competitor_brands=category_competitor_brands or [],
        visibility_query_rows=visibility_query_rows,
        attribution_query_rows=attribution_query_rows,
        url_source=url_source,
        merchant_brand=merchant_brand,
        merchant_host=merchant_host,
        merchant_storefront_name=merchant_name,
        prior_runs=prior_runs,
        pivota_signature_minted_at=pivota_signature_minted_at,
        integration_state=integration_state,
        is_cold_start=_report_cold_start,
    )

    # PR-8a (post-Grüns synthesis layer): build the strategic
    # executive summary — multi-paragraph narrative arc keyed off
    # detected score archetype. Surfaces as report.executive_summary
    # for renderers that want the narrative opening.
    from services.audit_narrative_builder import build_executive_summary
    top_cited_publishers_for_narrative = [
        h.get("host") for h in _copyworthy_cited_hosts(category_retailer_hosts)
        if _cited_host_type(h) == "editorial" and h.get("host")
    ][:3]
    executive_summary = build_executive_summary(
        merchant_name=merchant_name,
        visibility_score=visibility_score,
        attribution_score=attribution_score,
        category_visibility_score=category_score,
        evidence_quotes=evidence_quotes,
        cited_publishers=top_cited_publishers_for_narrative,
        competitor_brands=category_competitor_brands or [],
        industry_blurb=industry_context.get("blurb", ""),
        industry_share_pct=industry_context.get("ai_search_share_pct"),
        verdict_pill_text=_verdict_display_label(
            verdict_label, cited_runs=merchant_cited_runs
        ),
        # PR-7a: corporate intel from brand_context.corporate, when
        # available. Used by the editorial-archetype paragraph to
        # weave "{merchant_name}, a {parent}-owned brand" framing.
        corporate=(brand_context or {}).get("corporate"),
    )

    # PR-8c: implementation roadmap — groups PR-8b enriched action
    # items by their `phase` field into time-windowed buckets with
    # rolled-up owners + expected outcomes. Renderer surfaces this
    # as the "Implementation Roadmap" section between recommendations
    # and Pivota commitments.
    from services.audit_roadmap_builder import build_implementation_roadmap
    implementation_roadmap = build_implementation_roadmap(
        merchant_view.get("actions") or []
    )

    # PR-8d: Pivota commitments — explicit "what we deliver / what
    # we don't promise" block, platform-aware so disclosures are
    # accurate per merchant. Renderer surfaces between roadmap and
    # methodology / appendix.
    from services.audit_pivota_commitments_builder import (
        build_pivota_commitments,
    )
    _is_cold = _report_cold_start
    # _merchant_platform was hoisted earlier so the checkout_loop
    # chain in _build_what_pivota_changes can use the same value.
    pivota_commitments = build_pivota_commitments(
        merchant_platform=_merchant_platform,
        cold_start=_is_cold,
    )

    return {
        "merchant_name": merchant_name,
        "merchant_pdp_url": merchant_pdp_url,
        "merchant_host": merchant_host,
        "product": {
            "title": product_title,
            "vendor": product_vendor or None,
            "product_type": product_type or None,
            # PR-7b: form factor + price band classification (keyword-
            # deterministic). Renderers can show "Greens Gummies
            # (gummy, premium tier)" without re-deriving.
            "form_factor": merchant_product_classification.get("form_factor"),
            "price_band": merchant_product_classification.get("price_band"),
        },
        "provider": provider,
        "upstream_status": upstream_status,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # W1 RunFacts phase 1 — the compute-once fact layer for this report's
        # attribution runs. Additive: no rendered number reads from it yet
        # (that's the phase-2 cutover); W7 invariants + parity logging do.
        "run_facts": run_facts.to_dict(),
        "verdict": {
            "label": verdict_label,
            # Client-facing softer rendering. Renderers that show a
            # bare label string (e.g. the headline VerdictBanner)
            # should prefer this; downstream code that branches on
            # the verdict enum keeps using `label`.
            "label_display": _verdict_display_label(
                verdict_label, cited_runs=merchant_cited_runs
            ),
            "explanation": verdict_explanation,
            "visibility_score": visibility_score,
            "attribution_score": attribution_score,
            "category_visibility_score": category_score,  # null when category test wasn't run
        },
        "industry_context": industry_context,
        "action_items": action_items,
        "competitive_pressure": competitive_pressure,
        "what_pivota_changes": what_pivota_changes,
        # PR-7e: verbatim Gemini-grounded quotes that mention the
        # merchant brand by name with editorial-grade corroboration.
        # Renderers should surface these as quote boxes — highest-
        # leverage report element because they're verbatim what
        # Gemini said about the brand.
        "evidence_quotes": evidence_quotes,
        # PR-7b: cohort form-factor summary — surfaces "merchant is
        # the only gummy in the 15-competitor cohort" type insights.
        # When merchant_owns_unique_form_factor=True, renderer can
        # call out the structural moat. Empty when merchant
        # form_factor wasn't classifiable.
        "cohort_form_factor": cohort_form_factor,
        # PR-8a: strategic executive summary — multi-paragraph
        # narrative arc keyed off detected score archetype. The
        # opening of the polished report; renderers should surface
        # this BEFORE the verdict label / score table for the most
        # compelling read.
        "executive_summary": executive_summary,
        # PR-8c: implementation roadmap — phased rollup of action
        # items with rolled-up owners + expected outcomes per phase.
        # Renderer surfaces between recommendations and Pivota
        # commitments. Empty phases array when no recognized actions.
        "implementation_roadmap": implementation_roadmap,
        # PR-8d: Pivota commitments — explicit "what we deliver /
        # what we don't promise" block, platform-aware. Renderer
        # surfaces between roadmap and methodology.
        "pivota_commitments": pivota_commitments,
        "merchant_view": merchant_view,
        "visibility": {
            "score": visibility_score,
            "runs": len(visibility_runs),
            "queries": visibility_query_rows,
        },
        "attribution": {
            "score": attribution_score,
            "runs": len(attribution_runs),
            "merchant_cited_runs": merchant_cited_runs,
            "runs_with_any_citation": runs_with_any_citation,
            "queries": attribution_query_rows,
            "competitor_hosts": competitor_hosts_list,
        },
        "category_visibility": (
            {
                "score": category_score,
                "upstream_score": upstream_category_score,
                "runs": len(category_runs),
                "queries": _per_query_rows(category_runs, "brand_appears"),
                "match_details": category_match_details,
                "competitor_brands": category_competitor_brands,
                "top_cited_hosts": top_cited_hosts,
                "retailer_hosts": category_retailer_hosts,
            }
            if category_visibility_result is not None
            else None
        ),
        # Raw probe results for audit / debugging. UI can hide behind a
        # disclosure; CLI embeds in `<details>`.
        "raw": {
            "visibility": visibility_result,
            "attribution": attribution_result,
            "category_visibility": category_visibility_result,
        },
    }


def _markdown_chip_values(items: Any, *, key: str, limit: int = 4) -> List[str]:
    values: List[str] = []
    if not isinstance(items, list):
        return values
    for item in items:
        value = ""
        if isinstance(item, Mapping):
            value = str(item.get(key) or "").strip()
        else:
            value = str(item or "").strip()
        if value and value not in values:
            values.append(value)
        if len(values) >= limit:
            break
    return values


def _markdown_table_cell(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text.replace("|", "\\|") or "-"


def _markdown_http_link(label: Any, url: Any) -> str:
    href = str(url or "").strip()
    if not href.lower().startswith(("https://", "http://")):
        return "-"
    text = re.sub(r"\s+", " ", str(label or HANDOFF_LABEL_FALLBACK).strip())
    text = text.replace("[", "\\[").replace("]", "\\]").replace("|", "\\|")
    href = href.replace(")", "%29").replace(" ", "%20")
    return f"[{text or HANDOFF_LABEL_FALLBACK}]({href})"


HANDOFF_LABEL_FALLBACK = "Open buyable Pivota product page"


def _render_deliverability_markdown(report: Mapping[str, Any]) -> str:
    view = build_deliverability_render_view(report)
    if not view:
        return ""
    out: List[str] = ["## Servability and checkout\n"]
    out.append(str(view.get("headline") or "").strip() + "\n")
    definition = str(view.get("definition") or "").strip()
    if definition:
        out.append(f"_{definition}_\n")

    counts = [row for row in (view.get("counts") or []) if isinstance(row, Mapping)]
    if counts:
        out.append("\n| Status | SKUs |\n|---|---:|\n")
        for row in counts:
            out.append(
                f"| {_markdown_table_cell(row.get('label'))} "
                f"| {_markdown_table_cell(row.get('count'))} |\n"
            )

    transactable = [
        row for row in (view.get("transactable_rows") or [])
        if isinstance(row, Mapping)
    ]
    if transactable:
        out.append("\n**Confirmed transactable SKUs:**\n")
        has_handoff = any(
            str(row.get("handoff_url") or "").strip().lower().startswith(("https://", "http://"))
            for row in transactable
        )
        if has_handoff:
            out.append("| SKU | Checkout | Handoff | Read |\n|---|---|---|---|\n")
        else:
            out.append("| SKU | Checkout | Read |\n|---|---|---|\n")
        for row in transactable:
            if has_handoff:
                out.append(
                    f"| {_markdown_table_cell(row.get('sku_title'))} "
                    f"| {_markdown_table_cell(row.get('checkout_status'))} "
                    f"| {_markdown_http_link(row.get('handoff_label'), row.get('handoff_url'))} "
                    f"| {_markdown_table_cell(row.get('summary'))} |\n"
                )
            else:
                out.append(
                    f"| {_markdown_table_cell(row.get('sku_title'))} "
                    f"| {_markdown_table_cell(row.get('checkout_status'))} "
                    f"| {_markdown_table_cell(row.get('summary'))} |\n"
                )

    attention = [
        row for row in (view.get("attention_rows") or [])
        if isinstance(row, Mapping)
    ]
    if attention:
        out.append("\n**Needs attention before checkout:**\n")
        out.append("| SKU | State | Serving | Checkout | Read |\n|---|---|---|---|---|\n")
        for row in attention:
            out.append(
                f"| {_markdown_table_cell(row.get('sku_title'))} "
                f"| {_markdown_table_cell(row.get('status_label'))} "
                f"| {_markdown_table_cell(row.get('serving_status'))} "
                f"| {_markdown_table_cell(row.get('checkout_status'))} "
                f"| {_markdown_table_cell(row.get('summary'))} |\n"
            )
    out.append("\n")
    return "".join(out)


def _render_next_best_action_markdown(next_best_action: Optional[Mapping[str, Any]]) -> str:
    if not isinstance(next_best_action, Mapping):
        return ""
    headline = str(next_best_action.get("headline") or "").strip()
    why = str(next_best_action.get("why_this_first") or "").strip()
    first_move = str(next_best_action.get("first_move") or "").strip()
    evidence_summary = str(next_best_action.get("evidence_summary") or "").strip()
    evidence_chips = [
        str(chip).strip()
        for chip in (next_best_action.get("evidence_chips") or [])
        if str(chip).strip()
    ][:4]
    self_serve = [
        str(action).strip()
        for action in (
            next_best_action.get("self_serve")
            or next_best_action.get("self_serve_actions")
            or []
        )
        if str(action).strip()
    ][:2]
    pivota_items = [
        str(action).strip()
        for action in (
            next_best_action.get("pivota_assisted")
            or [next_best_action.get("pivota_path")]
        )
        if str(action).strip()
    ][:1]
    if not any([headline, why, first_move, self_serve, pivota_items]):
        return ""

    evidence = (
        next_best_action.get("evidence")
        if isinstance(next_best_action.get("evidence"), Mapping)
        else next_best_action.get("evidence_used")
    )
    evidence = evidence if isinstance(evidence, Mapping) else {}
    hosts = _markdown_chip_values(
        (
            evidence.get("retailer_hosts")
            or evidence.get("source_hosts")
            or evidence.get("cited_hosts")
        ),
        key="host",
    )
    competitors = _markdown_chip_values(evidence.get("competitors_named"), key="name")
    queries = _markdown_chip_values(
        evidence.get("failed_query_examples"),
        key="query",
        limit=3,
    )
    secondary_moves = [
        move for move in (next_best_action.get("secondary_moves") or [])
        if isinstance(move, Mapping) and str(move.get("title") or "").strip()
    ][:2]
    tracking_metrics = [
        str(metric).strip()
        for metric in (
            next_best_action.get("tracking_metrics")
            or next_best_action.get("how_to_track")
            or []
        )
        if str(metric).strip()
    ][:3]

    out: List[str] = ["## What should you do next?\n"]
    if headline:
        out.append(f"**{headline}**\n")
    if why:
        out.append(f"{why}\n")
    if first_move:
        out.append(f"**First move:** {first_move}\n")
    if evidence_summary:
        out.append(f"**Why this is the leak:** {evidence_summary}\n")
    if evidence_chips:
        out.append("**Gap read:** " + " · ".join(evidence_chips) + "\n")
    evidence_bits: List[str] = []
    if queries:
        evidence_bits.append("queries: " + ", ".join(f"`{query}`" for query in queries))
    if hosts:
        evidence_bits.append("cited hosts: " + ", ".join(f"`{host}`" for host in hosts))
    if competitors:
        evidence_bits.append("competitors: " + ", ".join(competitors))
    if evidence_bits:
        out.append("**Evidence:** " + " · ".join(evidence_bits) + "\n")
    if self_serve:
        out.append("\n**Do yourself this week:**\n")
        for action in self_serve:
            out.append(f"- {action}\n")
    if pivota_items:
        out.append("\n**Use Pivota for:**\n")
        for action in pivota_items:
            out.append(f"- {action}\n")
    if secondary_moves:
        out.append("\n**Secondary moves:**\n")
        for move in secondary_moves:
            reason = str(move.get("reason") or "").strip()
            out.append(f"- {move['title']}")
            if reason:
                out.append(f" — {reason}")
            out.append("\n")
    if tracking_metrics:
        out.append("\n**How to track:**\n")
        for metric in tracking_metrics:
            out.append(f"- {metric}\n")
    cta = next_best_action.get("cta")
    if isinstance(cta, Mapping):
        label = str(cta.get("label") or "").strip()
        trust_note = str(cta.get("trust_note") or "").strip()
        if label or trust_note:
            out.append("\n**CTA:** ")
            if label:
                out.append(label)
            if trust_note:
                out.append(f" — {trust_note}")
            out.append("\n")
    out.append("\n")
    return "".join(out)


def _render_reaudit_delta_markdown(delta: Optional[Mapping[str, Any]]) -> str:
    if not isinstance(delta, Mapping):
        return ""
    headline = str(delta.get("headline") or "").strip()
    if not headline:
        return ""
    out: List[str] = ["## Since your last audit\n"]
    out.append(headline + "\n")
    material = [
        movement for movement in (delta.get("movements") or [])
        if isinstance(movement, Mapping) and movement.get("is_material")
    ]
    if material:
        out.append("\n**Material movement:**\n")
        for movement in material:
            label = str(movement.get("label") or movement.get("signal") or "").strip()
            direction = str(movement.get("direction") or "changed").strip()
            before = movement.get("from")
            after = movement.get("to")
            out.append(f"- {label}: {before} → {after} ({direction})\n")
    tracked = [
        row for row in (delta.get("tracked_metric_results") or [])
        if isinstance(row, Mapping) and str(row.get("metric") or "").strip()
    ]
    if tracked:
        out.append("\n**Tracking read:**\n")
        for row in tracked:
            metric = str(row.get("metric") or "").strip()
            status = str(row.get("status") or "").strip() or "not_measurable"
            note = str(row.get("note") or "").strip()
            suffix = f" — {note}" if note else ""
            out.append(f"- {metric}: {status}{suffix}\n")
    out.append("\n")
    return "".join(out)


def _render_owned_buyer_path_play_markdown(next_best_action: Optional[Mapping[str, Any]]) -> str:
    if not isinstance(next_best_action, Mapping):
        return ""
    play = next_best_action.get("canonical_page_play")
    if not isinstance(play, Mapping):
        return ""
    lane = str(play.get("lane") or "").strip()
    moves = [
        move for move in (play.get("moves") or [])
        if isinstance(move, Mapping) and str(move.get("operator_action") or "").strip()
    ]
    if not lane and not moves:
        return ""

    strategy = str(
        play.get("controller_strategy_label")
        or play.get("controller_strategy")
        or "Buyer-path repair"
    ).strip()
    controllers = [
        str(controller).strip()
        for controller in (play.get("controllers") or [])
        if str(controller).strip()
    ][:3]
    profile = play.get("controller_profile") if isinstance(play.get("controller_profile"), Mapping) else {}
    focus = str(profile.get("operator_focus") or "").strip()
    exposure_read = str(
        play.get("exposure_read") or profile.get("exposure_read") or ""
    ).strip()
    out: List[str] = ["## Owned buyer path play\n"]
    out.append(f"**Strategy:** {strategy}\n")
    if lane:
        out.append(f"**Lane to win back:** `{lane}`\n")
    if controllers:
        out.append(
            "**Controllers evidenced:** "
            + ", ".join(f"`{host}`" for host in controllers)
            + "\n"
        )
    if focus:
        out.append(f"**Operator read:** {focus}\n")
    if exposure_read:
        out.append(f"**Exposure read:** {exposure_read}\n")
    wedge = next_best_action.get("sideways_wedge")
    if isinstance(wedge, Mapping):
        beachhead = wedge.get("recommended_beachhead_lane")
        beachhead_query = (
            str(beachhead.get("query") or "").strip()
            if isinstance(beachhead, Mapping) else ""
        )
        why_wedge = str(wedge.get("why_this_lane_not_the_head_prompt") or "").strip()
        do_not = [
            item for item in (wedge.get("do_not_chase_yet") or [])
            if isinstance(item, Mapping) and str(item.get("query") or "").strip()
        ][:3]
        if beachhead_query or why_wedge or do_not:
            out.append("\n**Sideways demand wedge:**\n")
            if beachhead_query:
                out.append(f"- Beachhead lane: `{beachhead_query}`\n")
            if why_wedge:
                out.append(f"- Why this first: {why_wedge}\n")
            if do_not:
                deferred = ", ".join(
                    f"`{str(item.get('query') or '').strip()}`"
                    for item in do_not
                )
                out.append(f"- Do not chase yet: {deferred}\n")
    if moves:
        out.append("\n**Operator checklist:**\n")
        for idx, move in enumerate(moves[:5], start=1):
            action = str(move.get("operator_action") or "").strip()
            why = str(move.get("why") or "").strip()
            move_type = str(move.get("type") or f"move_{idx}").replace("_", " ").title()
            out.append(f"{idx}. **{move_type}** — {action}\n")
            if why:
                out.append(f"   - Why: {why}\n")
    checkout = str(play.get("checkout_readiness") or "").strip()
    if checkout:
        out.append(f"\n**Agent-checkout readiness:** {checkout}\n")
    economics = str(play.get("economics_policy") or "").strip()
    if economics:
        out.append(f"**Economics guard:** {economics}\n")
    out.append("\n")
    return "".join(out)


def render_markdown_from_structured(report: Dict[str, Any]) -> str:
    """Convert the structured report into the BD-ready markdown output
    the CLI produces. Kept here so the script and any future markdown
    consumers (export-to-PDF, email, etc.) share the same shape."""
    sections: List[str] = []
    sections.append(f"# AI Commerce Readiness Report — {report['merchant_name']}\n")
    sections.append(
        f"_Generated {report['timestamp']} · Probe: pivota Demand Test Agent V1.5_\n"
    )
    sections.append(
        "This report measures two things: (1) is this brand findable when "
        "consumers ask AI shopping agents about products in this category "
        "today? (2) when AI shopping captures the projected 25-30% of D2C "
        "category traffic over the next 24 months, is the brand positioned "
        "to capture that funnel directly — or pay retailer markups for it? "
        "The second question is what Pivota addresses; the first establishes "
        "the starting point.\n"
    )

    upstream = report.get("upstream_status") or {}
    if upstream and not upstream.get("is_real"):
        sections.append(
            f"> ⚠️ **MOCK DATA — DO NOT SHARE WITH BD / MERCHANT**\n"
            f"> \n"
            f"> Requested provider: `{upstream.get('requested_provider', '?')}` · "
            f"Actual upstream: `{upstream.get('visibility_provider', '?')}` "
            f"(visibility), `{upstream.get('attribution_provider', '?')}` (attribution).\n"
            f"> \n"
            f"> {upstream.get('reason', 'Unknown mock fallback.')}\n"
        )
    else:
        sections.append(
            f"_Upstream: `{upstream.get('visibility_provider', report.get('provider'))}` "
            f"(real Gemini grounded search)._\n"
        )

    sections.append("## Subject\n")
    bullets = [
        f"- **Merchant:** {report['merchant_name']}",
        f"- **Verified URL:** {report['merchant_pdp_url']}",
        f"- **Product tested:** {report['product']['title']}",
    ]
    if report["product"].get("vendor"):
        bullets.append(f"- **Vendor / brand:** {report['product']['vendor']}")
    if report["product"].get("product_type"):
        bullets.append(f"- **Category:** {report['product']['product_type']}")
    sections.append("\n".join(bullets) + "\n")

    v = report["verdict"]
    verdict_display = v.get("label_display") or v.get("label")
    sections.append(f"## Verdict: **{verdict_display}**\n")
    sections.append(v["explanation"] + "\n")
    sections.append(
        f"- **AI visibility score:** **{v['visibility_score']}/100**  "
        f"(does Gemini surface this product when asked natural buyer queries?)\n"
        f"- **Direct attribution score:** **{v['attribution_score']}/100**  "
        f"(when it does surface the product, does Gemini cite the merchant's own URL?)\n"
    )

    # Industry context — qualitative business framing the BD rep can read
    # straight off the page. Keeps raw scores from feeling abstract.
    industry = report.get("industry_context") or {}
    if industry.get("blurb"):
        sections.append("## Industry context\n")
        sections.append(industry["blurb"] + "\n")
        if industry.get("ai_search_share_pct") is not None:
            sections.append(
                f"_Category baseline:_ AI shopping ≈ "
                f"**{industry['ai_search_share_pct']}%** of new D2C "
                f"{industry.get('category', 'category')} traffic, growing "
                f"~**{industry.get('ai_search_growth_yoy_pct', '?')}%** YoY.\n"
            )
        if industry.get("forward_projection"):
            sections.append(
                f"_24-month projection:_ {industry['forward_projection']}\n"
            )

    mv_for_play = report.get("merchant_view") if isinstance(report.get("merchant_view"), dict) else {}
    sections.append(
        _render_reaudit_delta_markdown(
            mv_for_play.get("reaudit_delta") if isinstance(mv_for_play, dict) else None
        )
    )
    sections.append(_render_deliverability_markdown(report))
    sections.append(
        _render_next_best_action_markdown(
            mv_for_play.get("next_best_action") if isinstance(mv_for_play, dict) else None
        )
    )

    # Recommended actions — derived from the merchant's actual failed
    # queries / cited competitors, not generic prose.
    actions = report.get("action_items") or []
    if actions:
        sections.append("## Recommended actions\n")
        for idx, action in enumerate(actions, start=1):
            sev = (action.get("severity") or "medium").upper()
            title = action.get("title") or "(untitled)"
            body = action.get("body") or ""
            sections.append(f"**{idx}. {title}** _(severity: {sev})_  \n{body}\n")

    sections.append(
        _render_owned_buyer_path_play_markdown(
            mv_for_play.get("next_best_action") if isinstance(mv_for_play, dict) else None
        )
    )

    # Competitive pressure — direct peer-vs-merchant first-party
    # visibility comparison. Sharpest BD framing: a merchant might
    # shrug off "your visibility is 0" but won't shrug off "your
    # competitor X has their own .com cited; you don't."
    cp = report.get("competitive_pressure") or {}
    if cp.get("peers_named"):
        sections.append(f"## {cp.get('title', 'Competitive pressure')}\n")
        if cp.get("intro"):
            sections.append(cp["intro"] + "\n")
        peers_named = cp.get("peers_named") or []
        if peers_named:
            sections.append(
                "**Direct competitor brands AI agents name in this category "
                "(top 10):**\n"
            )
            rows = ["| Brand | Times named in category queries |", "|---|---|"]
            for p in peers_named[:10]:
                brand = (p.get("brand") or p.get("name") or "").replace("|", "\\|")
                cnt = p.get("category_query_mentions") or p.get("times_cited") or 0
                rows.append(f"| {brand} | {cnt} |")
            sections.append("\n".join(rows) + "\n")
        peers_fp = cp.get("peers_with_first_party_visibility") or []
        if peers_fp:
            sections.append(
                "**Competitors whose own .com is cited first-party in "
                "Gemini grounding (this is the BD pressure):**\n"
            )
            rows = [
                "| Brand | First-party host | Citations | Category mentions |",
                "|---|---|---|---|",
            ]
            for p in peers_fp:
                brand = (p.get("brand") or "").replace("|", "\\|")
                host = (p.get("first_party_host") or "").replace("|", "\\|")
                citations = p.get("host_citations", 0)
                mentions = p.get("category_query_mentions", 0)
                rows.append(f"| {brand} | `{host}` | {citations} | {mentions} |")
            sections.append("\n".join(rows) + "\n")
        else:
            sections.append(
                "_None of the named competitor brands have their own .com "
                "cited in Gemini grounding for the same queries — the "
                "category is currently retailer-mediated end-to-end._\n"
            )
        if cp.get("framing"):
            sections.append(cp["framing"] + "\n")

    # What Pivota changes — the post-onboarding delta. Two parts the
    # merchant needs to believe: (1) why their AI-channel visibility
    # will improve (discovery_lift, with mechanics + Pivota PDP
    # reference); (2) how in-chat checkout closes the loop (6-step
    # chain from grounded answer → merchant Shopify admin, each with
    # verifying file/test reference).
    wpc = report.get("what_pivota_changes") or {}
    if wpc.get("discovery_lift") or wpc.get("checkout_loop"):
        sections.append("## What Pivota changes after onboarding\n")
        if wpc.get("today_summary"):
            sections.append(f"**Today:** {wpc['today_summary']}\n")

        dl = wpc.get("discovery_lift") or {}
        if dl:
            sections.append(f"### {dl.get('title', 'Discovery lift')}\n")
            if dl.get("current_state"):
                sections.append(f"**Current state.** {dl['current_state']}\n")
            layers = dl.get("layers") or []
            for layer in layers:
                name = layer.get("name", "")
                subtitle = layer.get("subtitle") or ""
                heading = f"#### {name}"
                if subtitle:
                    heading += f"\n_{subtitle}_"
                sections.append(heading + "\n")
                if layer.get("what_it_is"):
                    sections.append(f"{layer['what_it_is']}\n")
                if layer.get("pivota_status"):
                    sections.append(
                        f"**Pivota status.** {layer['pivota_status']}\n"
                    )
                metric = layer.get("merchant_metric")
                if metric:
                    sections.append(
                        f"**This layer's per-merchant signal in this audit:** "
                        f"`{metric}`\n"
                    )
                else:
                    sections.append(
                        "**This layer's per-merchant signal in this audit:** "
                        "_(not measured by this report; layer is binary by "
                        "Pivota integration)_\n"
                    )
                mechs = layer.get("mechanics") or []
                if mechs:
                    rows = [
                        "| Mechanic | Evidence | Status |",
                        "|---|---|---|",
                    ]
                    for m in mechs:
                        lab = (m.get("label") or "").replace("|", "\\|")
                        ev = (m.get("evidence") or "").replace("|", "\\|")
                        status = (
                            "✅ shipped" if m.get("shipped") else "🔄 in progress"
                        )
                        rows.append(f"| {lab} | `{ev}` | {status} |")
                    sections.append("\n".join(rows) + "\n")
            if dl.get("prediction"):
                sections.append(f"**Prediction.** {dl['prediction']}\n")
            if dl.get("methodology_note"):
                sections.append(f"_{dl['methodology_note']}_\n")

        cl = wpc.get("checkout_loop") or {}
        if cl:
            sections.append(f"### {cl.get('title', 'Checkout loop')}\n")
            chain = cl.get("chain") or []
            if chain:
                chain_rows = [
                    "| # | Step | Evidence | Status |",
                    "|---|---|---|---|",
                ]
                for s in chain:
                    n = s.get("step", "")
                    label = (s.get("label") or "").replace("|", "\\|")
                    ev = (s.get("evidence") or "").replace("|", "\\|")
                    status = "✅ shipped" if s.get("shipped") else "🔄 in progress"
                    chain_rows.append(f"| {n} | {label} | `{ev}` | {status} |")
                sections.append("\n".join(chain_rows) + "\n")
            pc = cl.get("platform_coverage") or {}
            if pc:
                # PR-codex-review-followup: platform_coverage shape
                # changed from `{shipped, roadmap}` to `{shipped,
                # audit_only, custom_integration, note}` in PR-10a +
                # PR-10c. The legacy `roadmap` key no longer exists,
                # so this section was rendering "Roadmap: (none)"
                # forever while the actually-useful audit_only +
                # custom_integration disclosures stayed invisible.
                shipped_list = ", ".join(pc.get("shipped") or [])
                audit_only_list = ", ".join(pc.get("audit_only") or [])
                sections.append(
                    f"**Platform coverage.** "
                    f"Shipped end-to-end: {shipped_list or '(none)'}. "
                    f"Audit + manual order routing: "
                    f"{audit_only_list or '(none)'}.\n"
                )
                if pc.get("custom_integration"):
                    sections.append(
                        f"_Custom / headless storefronts: "
                        f"{pc['custom_integration']}_\n"
                    )
                if pc.get("note"):
                    sections.append(f"**Roadmap.** {pc['note']}\n")
            if cl.get("outcome"):
                sections.append(f"**Outcome.** {cl['outcome']}\n")

        os = wpc.get("onboarding_sequence") or {}
        if os:
            sections.append(f"### {os.get('title', 'Onboarding sequence')}\n")
            if os.get("intro"):
                sections.append(os["intro"] + "\n")
            tm = os.get("test_merchant") or {}
            if tm.get("merchant_id"):
                sections.append(
                    f"_Test merchant playground: "
                    f"`{tm['merchant_id']}` @ `{tm.get('shop_domain', '?')}` "
                    f"(audit artifact: "
                    f"`{tm.get('audit_artifact_path', '?')}`)._\n"
                )
            steps = os.get("steps") or []
            if steps:
                rows = [
                    "| # | Step | Status | What | Test merchant validation |",
                    "|---|---|---|---|---|",
                ]
                for s in steps:
                    n = s.get("step", "")
                    name = (s.get("name") or "").replace("|", "\\|")
                    status = (s.get("status") or "").replace("|", "\\|")
                    if s.get("manual_today"):
                        status = f"🔧 manual today ({status})"
                    elif "shipped" in status.lower():
                        status = f"✅ {status}"
                    what = (s.get("what") or "").replace("|", "\\|").replace("\n", " ")
                    val = (
                        s.get("test_merchant_validation") or ""
                    ).replace("|", "\\|").replace("\n", " ")
                    rows.append(
                        f"| {n} | **{name}** | {status} | {what} | {val} |"
                    )
                sections.append("\n".join(rows) + "\n")
            if os.get("roadmap_note"):
                sections.append(f"**Roadmap.** {os['roadmap_note']}\n")
            if os.get("roadmap_note"):
                sections.append(f"_{os['roadmap_note']}_\n")

        vb = wpc.get("visibility_booster") or {}
        if vb:
            sections.append(f"### {vb.get('title', 'Visibility Booster')}\n")
            if vb.get("intro"):
                sections.append(vb["intro"] + "\n")
            mw = vb.get("mechanisms_that_work") or []
            if mw:
                sections.append("**Mechanisms that actually work:**\n")
                rows = [
                    "| Mechanism | What | Status | Evidence |",
                    "|---|---|---|---|",
                ]
                for m in mw:
                    label = (m.get("label") or "").replace("|", "\\|")
                    what_ = (m.get("what") or "").replace("|", "\\|").replace("\n", " ")
                    status = (m.get("status") or "").lower()
                    if status == "shipped":
                        status_md = "✅ shipped"
                    elif "manual" in status:
                        status_md = "🔧 manual today"
                    elif "roadmap" in status:
                        status_md = "📋 roadmap"
                    else:
                        status_md = m.get("status", "")
                    ev = (m.get("evidence") or "").replace("|", "\\|")
                    rows.append(f"| **{label}** | {what_} | {status_md} | `{ev}` |")
                sections.append("\n".join(rows) + "\n")
            wd = vb.get("what_doesnt_work") or []
            if wd:
                sections.append(
                    "**What does NOT work (folk remedies BD has heard from merchants):**\n"
                )
                for item in wd:
                    sections.append(f"- ❌ {item}\n")
            if vb.get("honest_position"):
                sections.append(
                    f"**Honest position.** {vb['honest_position']}\n"
                )

    sections.append("## 1. Open product visibility\n")
    sections.append(
        f"We fed Gemini {report['visibility']['runs']} buyer-style queries "
        f"(auto-generated from the product title + vendor + category). For each, "
        f"we asked: did Gemini surface the product as one of the answers?\n"
    )
    sections.append(_md_query_table(report["visibility"]["queries"]) + "\n")

    # Optional category visibility section (Phase 2a) — appears between
    # the open-product visibility table and the direct-attribution table
    # so the BD rep reads the report in escalating-credibility order:
    # product-named visibility → category-open visibility → direct
    # attribution.
    cat = report.get("category_visibility")
    if cat is not None:
        sections.append("## 1.5. Category-level discoverability\n")
        sections.append(
            f"We fed Gemini {cat['runs']} **category-open** queries (e.g. "
            f"\"best {{category}} 2026\") that DON'T name the product. "
            f"This is the honest discoverability test: does the merchant "
            f"brand surface in grounded sources for queries a typical "
            f"consumer asks without already knowing the brand?\n"
        )
        sections.append(_md_query_table(cat["queries"]) + "\n")
        cat_score = cat.get("score") or 0
        if cat_score == 0:
            sections.append(
                "_Score 0/100 — the merchant brand was absent from grounded "
                "sources on every category query. This is the harshest "
                "BD signal: consumers asking for products in this category "
                "without naming the brand never see the merchant._\n"
            )
        retailers = _copyworthy_cited_hosts(cat.get("retailer_hosts") or [])
        if retailers:
            sections.append(
                "**Where category traffic is being routed (cited hosts "
                "instead of merchant):**\n"
            )
            sections.append(_md_retailer_table(retailers) + "\n")
        comp_brands = cat.get("competitor_brands") or []
        if comp_brands:
            sections.append(
                "**Top direct competitors named by Gemini in category queries:**\n"
            )
            sections.append(_md_competitor_brand_table(comp_brands) + "\n")

    sections.append("## 2. Direct attribution\n")
    sections.append(
        f"We fed Gemini {report['attribution']['runs']} buyer-style queries that "
        f"should naturally cite the merchant's own store as a buying path. For "
        f"each, we asked: did Gemini cite the verified merchant URL "
        f"`{report['merchant_pdp_url']}` as a source (via Google Search grounding)?\n"
    )
    sections.append(_md_query_table(report["attribution"]["queries"]) + "\n")

    sections.append("### Where AI shopping traffic is going instead\n")
    runs_with_any_citation = report["attribution"]["runs_with_any_citation"]
    if runs_with_any_citation == 0:
        sections.append(
            "_(Gemini didn't return any cited URLs in its grounded answers. This "
            "usually means the product or product type is too long-tail for live "
            "web search to find anything — a stronger signal that the merchant is "
            "invisible to the AI-search channel than even a low attribution score.)_\n"
        )
    else:
        merchant_cited = report["attribution"]["merchant_cited_runs"]
        attr_runs = report["attribution"]["runs"]
        sections.append(
            f"Across {attr_runs} attribution queries, Gemini's grounded search "
            f"cited URLs from {runs_with_any_citation} runs. The merchant's own "
            f"URL appeared in {merchant_cited} of those.\n"
        )
        sections.append("**Top cited competitor / third-party hosts:**\n")
        sections.append(_md_competitor_table(report["attribution"]["competitor_hosts"]) + "\n")
        if merchant_cited == 0 and report["attribution"]["competitor_hosts"]:
            sections.append(
                "> Every grounded citation went to a third party. The merchant has "
                "_zero_ direct AI-channel attribution today.\n"
            )

    sections.append("## What this means for the merchant\n")
    sections.append(v["explanation"] + "\n")

    sections.append("## Methodology\n")
    sections.append(
        "- **Provider:** Gemini 2.5 Flash with Google Search grounding (live web, "
        "not training data).\n"
        "- **Queries:** auto-generated from product attributes — direct buying intent "
        "(`where can I buy X`, `shop X online`), comparative (`X reviews`, "
        "`X alternatives`), pricing (`best price for X`), vendor-anchored, and "
        "category-anchored. Operator-supplied queries override the generator if "
        "provided.\n"
        "- **Visibility scoring:** count of runs where Gemini's answer affirms the "
        "product is one of the buying paths.\n"
        "- **Attribution scoring:** count of runs where the verified merchant URL "
        "appears either in Gemini's cited sources (grounding metadata, gold-standard) "
        "or in the prose of the answer. The LLM's self-report is captured for "
        "transparency but does NOT drive the score (model frequently hallucinates "
        "self-attribution).\n"
        "- **Sample size:** 3 runs per scan_mode (conservative default; can be "
        "increased per probe call once worker-pool isolation lands upstream — see "
        "incident #280 for context).\n"
    )
    sections.append(
        "**Scope.** This report measures AI-channel discoverability and "
        "first-party attribution within Gemini grounded search. It does NOT "
        "measure D2C web traffic, retail sell-through, organic SEO health, or "
        "paid search performance — those channels are intentionally orthogonal "
        "to AI-native commerce and are well-served by existing tools (Search "
        "Console, GA4, retailer dashboards). Pivota addresses the gap none of "
        "those tools cover: the AI-channel transaction surface (canonical "
        "AI-channel PDP + in-chat checkout via the agentic-commerce protocol).\n"
    )

    sections.append("## Raw probe data\n")
    raw = report.get("raw") or {}
    import json as _json
    sections.append(
        "<details><summary>Click to expand</summary>\n\n```json\n"
        + _json.dumps(raw, indent=2, default=str)
        + "\n```\n</details>\n"
    )
    return "\n".join(sections)


def _md_query_table(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "_(no queries ran)_"
    out = ["| Query | Gemini said yes? | URL cited (top 1) |", "|---|---|---|"]
    for r in rows:
        symbol = "✅" if r["self_report_yes"] else "❌"
        url = r.get("top_cited_url") or "_(no grounded source)_"
        if isinstance(url, str) and len(url) > 70:
            url = url[:67] + "…"
        out.append(f"| {r['query']} | {symbol} | {url} |")
    return "\n".join(out)


def _md_competitor_table(competitors: List[Dict[str, Any]], top_n: int = 8) -> str:
    if not competitors:
        return "_(none — Gemini didn't cite any URLs in its answers)_"
    out = ["| Competitor host | Times cited |", "|---|---|"]
    for entry in competitors[:top_n]:
        out.append(f"| `{entry['host']}` | {entry['times_cited']} |")
    return "\n".join(out)


def _md_retailer_table(retailers: List[Dict[str, Any]], top_n: int = 8) -> str:
    if not retailers:
        return "_(none cited)_"
    out = ["| Cited host | Type | Category queries citing |", "|---|---|---|"]
    for entry in retailers[:top_n]:
        host_type = _cited_host_type_label(entry.get("type"))
        out.append(f"| `{entry['host']}` | {host_type} | {entry['times_cited']} |")
    return "\n".join(out)


def _md_competitor_brand_table(brands: List[Dict[str, Any]], top_n: int = 10) -> str:
    if not brands:
        return "_(none named)_"
    out = ["| Competitor brand | Category queries naming |", "|---|---|"]
    for entry in brands[:top_n]:
        out.append(f"| {entry['name']} | {entry['times_cited']} |")
    return "\n".join(out)


def render_brand_markdown(
    brand_report: Dict[str, Any],
    *,
    discovery: Optional[Dict[str, Any]] = None,
) -> str:
    """Render a multi-product brand audit into one downloadable markdown
    document. Wraps render_markdown_from_structured for each per-product
    report and prepends a brand-level header (merchant name, domain,
    discovery method, aggregate verdict, products audited count).

    Used by the cold-start export endpoint so BD operators can download
    the full audit as a single .md file (and from there to PDF via any
    markdown processor).
    """
    sections: List[str] = []
    merchant_name = brand_report.get("merchant_name") or "Unknown brand"
    merchant_domain = brand_report.get("merchant_domain") or ""
    timestamp = brand_report.get("timestamp") or ""
    aggregate = brand_report.get("aggregate") or {}
    per_product = brand_report.get("per_product") or []

    sections.append(f"# AI Commerce Readiness Report — {merchant_name}\n")
    if merchant_domain:
        sections.append(f"_Domain: `{merchant_domain}`_\n")
    if timestamp:
        sections.append(f"_Generated: {timestamp}_\n")

    if discovery:
        method = discovery.get("discovery_method") or "?"
        platform = discovery.get("platform") or "?"
        total = discovery.get("products_discovered_total") or 0
        audited = len(per_product)
        sections.append(
            f"_Discovery: {method} (platform: {platform}) · "
            f"{audited} of {total} products audited._\n"
        )
        enrichment = discovery.get("enrichment") or {}
        if enrichment.get("brand_category_inferred"):
            sections.append(
                f"_Brand category (inferred): "
                f"**{enrichment['brand_category_inferred']}**._\n"
            )

    if aggregate:
        sections.append("\n## Brand-level summary\n")
        verdict_label = (
            aggregate.get("brand_verdict_label_display")
            or aggregate.get("brand_verdict_label")
            or "(unknown)"
        )
        sections.append(f"**Aggregate verdict:** {verdict_label}\n")
        if aggregate.get("brand_verdict_explanation"):
            sections.append(aggregate["brand_verdict_explanation"] + "\n")
        cat_score = aggregate.get("avg_category_visibility")
        cat_line = (
            f"**{cat_score}/100**" if cat_score is not None else "_(not measured)_"
        )
        sections.append(
            f"- Average AI visibility: **{aggregate.get('avg_visibility', 0)}/100**\n"
            f"- Average direct attribution: **{aggregate.get('avg_attribution', 0)}/100**\n"
            f"- Average category discoverability: {cat_line}\n"
            f"- Products audited: {aggregate.get('products_count', len(per_product))}\n"
        )

    sections.append(_render_deliverability_markdown(brand_report))

    # P2 (post-#525 codex review): the reconciled competitor view —
    # one row per competitor brand joining what the host rollup,
    # category-peer list, and social benchmark each said. Rendered
    # ABOVE the raw per-surface sections below so the operator gets
    # the coherent picture first; the raw sections stay for detail.
    entities = brand_report.get("competitor_entities") or []
    if entities:
        sections.append("\n## Competitors — reconciled view\n")
        sections.append(
            "_One row per competitor brand, joining every surface the "
            "audit saw them in. `Seen in` shows which signals "
            "corroborate — a competitor in all three is the most "
            "certain._\n"
        )
        rows = [
            "| Competitor | Category mentions | Known host(s) | First-party visible | Social | Seen in |",
            "|---|---|---|---|---|---|",
        ]
        for ent in entities[:15]:
            hosts = ", ".join(
                f"`{h.get('host')}`" for h in (ent.get("known_hosts") or [])
                if h.get("host")
            ) or "—"
            social = ent.get("social") or {}
            social_bits = []
            for platform in ("tiktok", "instagram"):
                p = social.get(platform) or {}
                fv = p.get("follower_estimate") or p.get("follower_band")
                if fv:
                    social_bits.append(f"{platform[:2].upper()} {fv}")
            social_str = ", ".join(social_bits) or "—"
            seen = ", ".join(ent.get("seen_in") or []) or "—"
            rows.append(
                f"| {ent.get('display_name', '?')} "
                f"| {ent.get('category_mentions', 0)} "
                f"| {hosts} "
                f"| {'yes' if ent.get('first_party_visible') else 'no'} "
                f"| {social_str} "
                f"| {seen} |"
            )
        sections.append("\n".join(rows) + "\n")

    cross = brand_report.get("cross_product_competitors") or []
    if cross:
        sections.append("\n## Hosts capturing this brand's AI traffic\n")
        # Q-P1-3: split rendering into verified competitors vs possible
        # peer hosts so BD doesn't lead a pitch with "Sephora's stealing
        # your traffic" when the evidence is category-context only.
        verified = [e for e in cross if e.get("confidence") in {
            "verified_competitor", "grounded_competitor",
        }]
        peers = [e for e in cross if e.get("confidence") == "possible_peer_host"]
        if verified:
            rows = [
                "| Host | Times cited | Source | Confidence |",
                "|---|---|---|---|",
            ]
            for entry in verified[:15]:
                rows.append(
                    f"| `{entry.get('host', '?')}` "
                    f"| {entry.get('times_cited', 0)} "
                    f"| {entry.get('source', 'unknown')} "
                    f"| {entry.get('confidence', 'unknown')} |"
                )
            sections.append("\n".join(rows) + "\n")
        if peers:
            sections.append(
                "\n_Possible peer hosts (category context, no direct "
                "buyer-intent capture):_\n"
            )
            rows = ["| Host | Category cites |", "|---|---|"]
            for entry in peers[:15]:
                rows.append(
                    f"| `{entry.get('host', '?')}` "
                    f"| {entry.get('category_cited', entry.get('times_cited', 0))} |"
                )
            sections.append("\n".join(rows) + "\n")

    # PR-8 social intelligence section. Render only when available
    # (the bd_brand_signals function returns {available: false} when
    # GEMINI_API_KEY is unset or the caller passed include_social_
    # intelligence=False; in either case we skip the section so the
    # report doesn't carry empty "data not available" stubs).
    social = brand_report.get("social_intelligence") or {}
    if social.get("available"):
        sections.append("\n## Social channel intelligence\n")
        own = social.get("own_presence") or {}
        tt = own.get("tiktok") or {}
        ig = own.get("instagram") or {}

        # Failure-reason surfacing: map a sub-call's failure token to a
        # merchant-readable one-liner. When a sub-section is empty AND
        # a reason exists, the renderer shows this instead of silently
        # omitting — so the operator knows whether the evidence was verified.
        _failure_reasons = social.get("failure_reasons") or {}
        _FAILURE_TEXT = {
            "ungrounded": (
                "not verified in a live source; suppressed to avoid unverified numbers"
            ),
            "parse_error": "not verified from the returned source evidence",
            "rate_limited": "not verified because the source check was rate-limited",
            "transport_error": "not verified because the source check did not complete",
            "no_data": "no live source evidence found for this brand",
        }

        def _failure_note(label: str, reason: Optional[str]) -> Optional[str]:
            if not reason:
                return None
            text = _FAILURE_TEXT.get(reason, f"not verified ({reason})")
            return f"_{label}: {text}._\n"

        def _own_presence_line(platform_label: str, p: Dict[str, Any]) -> str:
            # PR-9: when the sub-call was ungrounded, every metric is
            # nulled — render the handle + an explicit "not verified"
            # note rather than a fabricated count. A grounded entry
            # renders the real numbers.
            handle = p.get("handle") or "?"
            if p.get("grounding") == "ungrounded":
                return (
                    f"- {platform_label}: `@{handle}` — follower / engagement "
                    "data not verified in live sources. Operator to confirm manually.\n"
                )
            followers = p.get("follower_estimate") or p.get("follower_band") or "?"
            focus = p.get("content_focus") or "mixed"
            return (
                f"- {platform_label}: `@{handle}` — {followers} followers; "
                f"focus: {focus}.\n"
            )

        # Own-presence block. Renders a line per platform that has
        # data; for a platform that came back empty WITH a failure
        # reason, render the explanation note instead.
        tt_note = _failure_note("TikTok presence", _failure_reasons.get("own_presence_tiktok"))
        ig_note = _failure_note("Instagram presence", _failure_reasons.get("own_presence_instagram"))
        if tt or ig or tt_note or ig_note:
            sections.append("**Your brand's own social presence:**\n")
            if tt:
                sections.append(_own_presence_line("TikTok", tt))
            elif tt_note:
                sections.append(f"- {tt_note}")
            if ig:
                sections.append(_own_presence_line("Instagram", ig))
            elif ig_note:
                sections.append(f"- {ig_note}")
        kol = social.get("kol_endorsements") or {}
        tt_kols = kol.get("tiktok") or []
        ig_kols = kol.get("instagram") or []
        kol_tt_note = _failure_note(
            "TikTok creator endorsements", _failure_reasons.get("kol_tiktok"),
        )
        kol_ig_note = _failure_note(
            "Instagram creator endorsements", _failure_reasons.get("kol_instagram"),
        )
        if tt_kols or ig_kols or kol_tt_note or kol_ig_note:
            sections.append(
                "\n**Creators who posted about your brand (last 12 months):**\n"
            )
            for k in (tt_kols or [])[:5]:
                sections.append(
                    f"- TikTok: {k.get('creator_name', '?')} "
                    f"({k.get('follower_band', '?')}) — "
                    f"{k.get('post_summary', 'post summary not available')}\n"
                )
            if not tt_kols and kol_tt_note:
                sections.append(f"- {kol_tt_note}")
            for k in (ig_kols or [])[:5]:
                sections.append(
                    f"- Instagram: {k.get('creator_name', '?')} "
                    f"({k.get('follower_band', '?')}) — "
                    f"{k.get('post_summary', 'post summary not available')}\n"
                )
            if not ig_kols and kol_ig_note:
                sections.append(f"- {kol_ig_note}")
        # PR-10: brand-vs-competitor benchmark table. This is the
        # PRIMARY benchmark — apples-to-apples follower counts because
        # the merchant AND each competitor were measured by the same
        # `_infer_own_presence` probe (PR-9's grounding gate applied
        # to each). A merchant's "662k TikTok" only means something
        # next to a peer's number.
        competitor_presence = social.get("competitor_presence") or {}
        if competitor_presence:
            sections.append(
                "\n**Brand vs. competitor social benchmark:** "
                "_(follower counts measured by the same grounded lookup "
                "for every brand — blank = not verified in live sources)_\n"
            )

            def _followers(p: Optional[Dict[str, Any]]) -> str:
                if not p:
                    return "—"
                # PR-9: an ungrounded probe has all metrics nulled.
                return str(
                    p.get("follower_estimate")
                    or p.get("follower_band")
                    or "not verified"
                )

            rows = ["| Brand | TikTok | Instagram |", "|---|---|---|"]
            # Merchant's own row first — the baseline being benchmarked.
            merchant_label = brand_report.get("merchant_name") or "Your brand"
            rows.append(
                f"| **{merchant_label}** (you) "
                f"| {_followers(tt)} | {_followers(ig)} |"
            )
            for comp_name, comp in competitor_presence.items():
                comp = comp or {}
                rows.append(
                    f"| {comp_name} "
                    f"| {_followers(comp.get('tiktok'))} "
                    f"| {_followers(comp.get('instagram'))} |"
                )
            sections.append("\n".join(rows) + "\n")

        # Secondary narrative layer — the single-call competitive
        # comparison's gap_summary prose. Kept below the benchmark
        # table because it's the less-reliable signal (one call
        # covering N brands; came back null in the PR-8 prod run).
        competitive = social.get("competitive_comparison") or []
        competitive_note = _failure_note(
            "Competitive social comparison",
            _failure_reasons.get("competitive_comparison"),
        )
        if competitive:
            sections.append("\n**Competitive social comparison:**\n")
            rows = ["| Brand | TikTok | Instagram | Gap |", "|---|---|---|---|"]
            for c in competitive[:8]:
                # P2 fix (codex review): _infer_competitive_social emits
                # `*_followers_estimate` + `gap_summary`; the renderer
                # was reading `tiktok_followers` / `instagram_followers`
                # / `notes` — fields that never exist — so every row
                # rendered blank. Read the actual field names.
                rows.append(
                    f"| {c.get('brand', '?')} "
                    f"| {c.get('tiktok_followers_estimate') or '—'} "
                    f"| {c.get('instagram_followers_estimate') or '—'} "
                    f"| {c.get('gap_summary') or ''} |"
                )
            sections.append("\n".join(rows) + "\n")
        elif competitive_note:
            # competitive_comparison came back null — explain why
            # rather than silently dropping the sub-section. (The
            # PR-10 per-competitor benchmark table above is the
            # primary signal; this note covers the secondary layer.)
            sections.append("\n" + competitive_note)

    failed = brand_report.get("failed") or []
    if failed:
        sections.append("\n## Products that failed to audit\n")
        for f in failed:
            sections.append(
                f"- `{f.get('product_title', '?')}` — {f.get('reason', 'unknown error')}\n"
            )

    sections.append("\n---\n")
    sections.append("\n## Per-product detail\n")

    for idx, product_report in enumerate(per_product, start=1):
        sections.append(f"\n---\n\n### Product {idx} of {len(per_product)}\n")
        sections.append(render_markdown_from_structured(product_report))

    return "\n".join(sections)
