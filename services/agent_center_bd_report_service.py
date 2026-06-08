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
import json
import logging
import math
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from services import agent_center_llm_client as llm_client
from services.audit_delta import build_reaudit_delta
from services.audit_playbook_engine import select_playbooks
from services.brand_alias import derive_brand_aliases, text_mentions_brand
from services.buyer_path_stable_controllers import (
    stable_buyer_path_controller_hosts,
    stable_buyer_path_controllers_for_row,
)
from services.buyer_path_controller_quality import (
    controller_profile as build_controller_profile,
    aggregate_controller_profile,
    is_canonical_source_vacuum,
)
from services.cited_host_classifier import classify_cited_hosts, classify_host
from services.coverage_profiles import (
    resolve_coverage_profile,
    resolve_provider_models,
)
from services.commerce_execution_policy import (
    SURFACE_PUBLIC_AGENT_PURCHASE,
    resolve_commerce_execution_policy,
)
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
from services.sku_sidewalk import (
    build_sku_attribute_graph,
    generate_sidewalk_query_specs,
)


_ANSWER_QUALITY_VERIFY_SCAN_MODE = "answer_quality_verify"
_ANSWER_QUALITY_VERIFY_PROVIDER = "deepseek"
_PER_SKU_AUDIT_PROBE_SCAN_MODE = "open_product_visibility_test"
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
_EXPLICIT_AVAILABLE_STATES = {"in_stock", "available"}
_COMPETITOR_ATTRIBUTE_GROUNDED_PROVIDERS = {"gemini", "chatgpt"}
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
    "Verified citation-positive prompts keep their deterministic "
    "answer_quality hit unless DeepSeek returns "
    "supports_recommendation=false or misstates_facts=true; flagged "
    "verified prompts contribute 0 to answer_quality_rate. "
    "Deterministic first_party, sku_mention, and authority buckets are "
    "unchanged."
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


def normalize_host(url: str) -> Optional[str]:
    """Strip www, lowercase. Returns None for unparseable URLs."""
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


# Vertex AI grounding wraps every cited URL in a redirector — the URI we
# get back is `vertexaisearch.cloud.google.com/grounding-api-redirect/...`
# which hides the actual destination domain. The structured chunk's
# `title` field contains the human-readable source name ("Sephora",
# "Olive Young Global", "Beauty of Joseon Official Store") — much more
# useful for BD competitor analysis than the redirector hostname.
_VERTEX_REDIRECTOR_HOSTS = {
    "vertexaisearch.cloud.google.com",
    "vertex-ai-search.cloud.google.com",
}


def _identify_run_sources(run: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return a list of `{key, label}` source identifiers for one run.

    Reads the new `grounding_sources` field (list of `{uri, title}`)
    when present (PIVOTA-Agent #1302+), falls back to the legacy
    `grounding_chunks` (URI strings only) for older payloads.

    `key` is what we use for de-dup + merchant matching.
    `label` is what we show in the competitor table — title preferred,
    URI host as fallback when title is missing.
    """
    sources_raw = run.get("grounding_sources")
    out: List[Dict[str, str]] = []
    seen_keys = set()
    if isinstance(sources_raw, list) and sources_raw:
        for s in sources_raw:
            if not isinstance(s, dict):
                continue
            uri = s.get("uri") or ""
            title = (s.get("title") or "").strip()
            host = normalize_host(uri) or ""
            # Prefer title for the label/key when the URI is a redirector
            # (which it almost always is with Vertex AI grounding).
            if host in _VERTEX_REDIRECTOR_HOSTS:
                if not title:
                    continue  # nothing meaningful to surface
                label = title
                key = title.lower()
            else:
                # Real (non-redirected) host — use the host for key and
                # title for label when we have it.
                label = title or host
                key = host or title.lower()
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            out.append({"key": key, "label": label})
        return out
    # Legacy fallback: only URI strings available.
    chunks = run.get("grounding_chunks") or []
    for url in chunks:
        host = normalize_host(url) if isinstance(url, str) else None
        if not host or host in _VERTEX_REDIRECTOR_HOSTS:
            continue
        if host in seen_keys:
            continue
        seen_keys.add(host)
        out.append({"key": host, "label": host})
    return out


def _source_matches_merchant(
    source: Dict[str, str],
    *,
    merchant_host: Optional[str],
    merchant_brand: Optional[str],
    merchant_vendors: Optional[Tuple[str, ...]] = None,
) -> bool:
    """A grounding source counts as merchant-attribution when:
      - host matches the verified merchant host (rare with redirectors), OR
      - title contains the merchant host (e.g. "beautyofjoseon.com" in
        "Beauty of Joseon Official Store" — only true for some titles), OR
      - title contains the merchant brand name.
    """
    label_lower = source.get("label", "").lower()
    if merchant_host and merchant_host in label_lower:
        return True
    if merchant_brand:
        brand_lower = merchant_brand.strip().lower()
        if brand_lower and brand_lower in label_lower:
            return True
        # Phase B: alias-aware match — the merchant is recorded as
        # "BB Lab Global" but the cited source title says "BB Lab". Only
        # ADDS matches over the literal compare above (never removes one).
        if text_mentions_brand(
            label_lower,
            derive_brand_aliases(
                merchant_brand,
                merchant_host,
                _clean_identity_tuple(merchant_vendors),
            ),
        ):
            return True
    return False


def _clean_identity_tuple(values: Optional[Tuple[str, ...]]) -> Tuple[str, ...]:
    out: List[str] = []
    seen = set()
    for value in values or ():
        cleaned = re.sub(r"\s+", " ", str(value or "").strip())
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return tuple(out)


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
      - Counter of {competitor_label: occurrences} — labels are
        Gemini's titles ("Sephora", "Olive Young Global") not the
        redirector host
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
        run_competitor_labels = set()
        for src in sources:
            if _source_matches_merchant(
                src,
                merchant_host=merchant_host,
                merchant_brand=merchant_brand,
                merchant_vendors=merchant_vendors,
            ):
                merchant_in_run = True
            else:
                run_competitor_labels.add(src["label"])
        if merchant_in_run:
            merchant_cited_runs += 1
        for label in run_competitor_labels:
            competitors[label] += 1
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


def _verdict_display_label(label: str) -> str:
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
            " This is real and immediate competitive pressure — every "
            "retailer-routed query is a customer a peer won and you "
            "didn't see."
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
                f"AI agents surface your product (visibility "
                f"{visibility_score}/100). {your_url_label} was cited "
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
        if has_evidence:
            return (
                f"AI agents cite your URL in {cited} of {runs_total} "
                f"buyer-intent queries (visibility {visibility_score}/100, "
                f"attribution {attribution_score}/100). Both discovery "
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
            f"Mixed result — visibility {visibility_score}/100, "
            f"attribution {attribution_score}/100. Of {runs_total} "
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


def verdict_for(
    visibility_score: int,
    attribution_score: int,
    peer_thresholds: Optional[Dict[str, int]] = None,
    *,
    category_visibility_score: Optional[int] = None,
    evidence: Optional[Dict[str, Any]] = None,
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
        label, visibility_score, attribution_score, evidence_dict
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


def reset_sku_context_cache() -> None:
    """Test hook and audit-run boundary helper."""
    _SKU_CONTEXT_CACHE.clear()


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


def _category_text(product: Dict[str, Any]) -> str:
    return " ".join(
        str(product.get(k) or "")
        for k in ("product_type", "category", "category_path")
    ).lower()


def _vertical_for(product: Dict[str, Any]) -> str:
    text = _category_text(product)
    if any(x in text for x in ("beauty", "skin", "cosmetic", "makeup", "wellness", "supplement", "vitamin")):
        return "beauty"
    if any(x in text for x in ("fashion", "apparel", "clothing", "sleepwear", "shirt", "dress", "shoe")):
        return "fashion"
    if any(x in text for x in ("electronics", "device", "laptop", "phone", "camera", "headphone", "speaker")):
        return "electronics"
    return "other"


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


def compute_content_richness_score(sku_ctx: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Spec A.2 content-richness score. Pure: reads normalized SKU context."""
    product = _get_product(sku_ctx or {})
    sku = _get_sku(sku_ctx or {})
    enrichment = _get_enrichment(sku_ctx or {})
    quality = _get_quality(sku_ctx or {})
    breakdown: Dict[str, Any] = {}
    missing: List[str] = []

    quality_value = quality.get("content_quality_score", sku_ctx.get("content_quality_score"))
    quality_points = _points_from_percent(quality_value, 25)
    _add_bucket(
        breakdown, missing, "product_quality_score",
        quality_points, 25,
        f"content quality normalized to {quality_points}/25" if quality_value is not None else "data unavailable",
        missing=None if quality_value is not None else ["product_quality_snapshot.content_quality_score"],
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
    _add_bucket(
        breakdown, missing, "vertical_structure",
        vertical_points, 20,
        f"{vertical} structure coverage" if vertical_points else "data unavailable",
        missing=vertical_missing or None,
        extra={"vertical": vertical},
    )

    readiness_value = quality.get("model_readiness_score", sku_ctx.get("model_readiness_score"))
    readiness_points = _points_from_percent(readiness_value, 15)
    _add_bucket(
        breakdown, missing, "model_readiness",
        readiness_points, 15,
        f"model readiness normalized to {readiness_points}/15" if readiness_value is not None else "data unavailable",
        missing=None if readiness_value is not None else ["product_quality_snapshot.model_readiness_score"],
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


def _is_first_party_host(host: Optional[str], sku_ctx: Dict[str, Any]) -> bool:
    if not host:
        return False
    product = _get_product(sku_ctx or {})
    first_party_hosts = {
        normalize_host(product.get("canonical_url") or ""),
        normalize_host(product.get("pivota_canonical_url") or ""),
        normalize_host(sku_ctx.get("canonical_url") or ""),
        normalize_host(sku_ctx.get("pivota_canonical_url") or ""),
    }
    first_party_hosts.discard(None)
    return host in first_party_hosts


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
    verdict = output.get("verdict") if isinstance(output.get("verdict"), Mapping) else {}
    return (
        verdict.get("supports_recommendation") is False
        or verdict.get("misstates_facts") is True
    )


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
    del sku_ctx  # reserved for future stricter candidate filters.
    candidates: List[Dict[str, Any]] = []
    seen = set()
    for run in _flatten_probe_runs(_any_provider_probe_runs(probe_runs)):
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
    observed_prompt_count = len(_flatten_probe_runs(_any_provider_probe_runs(probe_runs)))
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

    outputs: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for idx, run in enumerate(candidates[:sample_cap]):
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
            )
            verdict = _extract_verify_verdict(result)
            output = {
                **output_base,
                "verdict": verdict,
                "usage": result.get("usage") or {},
                "raw_runs": result.get("raw_runs") or [],
            }
            outputs.append(output)
            if verdict is None:
                errors.append({
                    "query": query,
                    "error": "unparseable_verify_verdict",
                })
        except Exception as exc:  # noqa: BLE001 - verifier must not fail audit
            errors.append({"query": query, "error": str(exc)[:200]})
            outputs.append({
                **output_base,
                "verdict": None,
                "error": str(exc)[:200],
            })

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
) -> Tuple[int, Dict[str, Any]]:
    """Spec A.4 citation score from Brief 1 per_sku_audit raw_runs."""
    product = _get_product(sku_ctx or {})
    sku = _get_sku(sku_ctx or {})
    runs = _flatten_probe_runs(per_sku_probe_runs)
    breakdown: Dict[str, Any] = {}
    missing: List[str] = []
    denominator = len(runs)
    if denominator <= 0:
        for name, max_points in (
            ("first_party_rate", 45),
            ("sku_mention_rate", 25),
            ("authority_near_variant_rate", 20),
            ("answer_quality_rate", 10),
        ):
            _add_bucket(
                breakdown, missing, name, 0, max_points,
                "data unavailable",
                missing=["per_sku_audit.raw_runs"],
                extra={"numerator": 0, "denominator": 0, "rate": 0.0},
            )
        return _finish_breakdown(breakdown, missing)

    canonical_url = product.get("canonical_url") or sku_ctx.get("canonical_url")
    pivota_url = product.get("pivota_canonical_url") or sku_ctx.get("pivota_canonical_url")
    title = product.get("title") or sku.get("title")
    sku_title = sku.get("title") or title
    variant_name = sku.get("sku") or sku.get("source_variant_id")

    first_party_hits = 0
    sku_mentions = 0
    authority_hits = 0
    quality_hits = 0
    adjusted_quality_hits = 0
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
        product_visible = parsed.get("product_visible")
        if product_visible is None:
            product_visible = run.get("product_visible")
        if product_visible is None:
            product_visible = llm_report.get("product_visible")
        negative_verdict = (
            product_visible is False
            or parsed.get("correct_sku") is False
            or llm_report.get("correct_sku") is False
        )

        # first_party: the merchant PDP must be a primary grounding source.
        # `url_match.in_grounding` can be true on branded prompts where the
        # brand is merely mentioned while publishers/retailers carry the
        # citations; that is visibility, not first-party control.
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
            else:
                adjusted_quality_hits += 1

    def _rate_bucket(name: str, numerator: int, max_points: int) -> None:
        rate = numerator / denominator if denominator else 0.0
        points = int(round(max_points * rate))
        _add_bucket(
            breakdown, missing, name, points, max_points,
            f"{numerator}/{denominator} prompts matched",
            extra={"numerator": numerator, "denominator": denominator, "rate": round(rate, 4)},
        )

    _rate_bucket("first_party_rate", first_party_hits, 45)
    _rate_bucket("sku_mention_rate", sku_mentions, 25)
    _rate_bucket("authority_near_variant_rate", authority_hits, 20)
    _rate_bucket("answer_quality_rate", adjusted_quality_hits, 10)
    breakdown["answer_quality_rate"]["deterministic_numerator"] = quality_hits
    breakdown["answer_quality_rate"]["verify_deweighted"] = verify_deweighted
    breakdown["answer_quality_rate"]["verify_rule"] = (
        _ANSWER_QUALITY_VERIFY_DEWEIGHT_RULE
    )
    total = int(round(
        45 * (first_party_hits / denominator)
        + 25 * (sku_mentions / denominator)
        + 20 * (authority_hits / denominator)
        + 10 * (adjusted_quality_hits / denominator)
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
    return merged


def _any_provider_probe_runs(per_sku_probe_runs: Any) -> List[Dict[str, Any]]:
    grouped_runs: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for run in _flatten_probe_runs(per_sku_probe_runs):
        grouped_runs[_citation_prompt_key(run)].append(run)
    merged_runs = [
        _merge_runs_for_any_provider(runs)
        for _key, runs in sorted(grouped_runs.items())
        if runs
    ]
    return [{
        "provider": "coverage_profile_any",
        "raw_runs": merged_runs,
    }] if merged_runs else []


def build_citation_by_provider(
    sku_ctx: Dict[str, Any],
    per_sku_probe_runs: Any,
    verify_outputs: Optional[Any] = None,
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for provider, probes in sorted(_group_probe_runs_by_provider(per_sku_probe_runs).items()):
        failed_probe = next(
            (
                probe for probe in _json_list(probes)
                if isinstance(probe, dict)
                and probe.get("status") == "probe_failed"
            ),
            None,
        )
        if failed_probe is not None:
            score, breakdown = compute_citation_score(sku_ctx, [], verify_outputs=verify_outputs)
            out[provider] = {
                "status": "probe_failed",
                "error": str(failed_probe.get("error") or "")[:500],
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


async def _fetch_one_dict(query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from db.database import database
    try:
        row = await database.fetch_one(query, values)
    except Exception:
        return None
    return _row_dict(row)


async def _fetch_all_dicts(query: str, values: Dict[str, Any]) -> List[Dict[str, Any]]:
    from db.database import database
    try:
        rows = await database.fetch_all(query, values)
    except Exception:
        return []
    return [d for d in (_row_dict(row) for row in rows or []) if d is not None]


async def load_sku_context(sku_key: str, merchant_id: str) -> Dict[str, Any]:
    """Read-only SKU-context loader for spec A.1-A.3 catalog signals."""
    cache_key = (str(sku_key or ""), str(merchant_id or ""))
    if cache_key in _SKU_CONTEXT_CACHE:
        return _SKU_CONTEXT_CACHE[cache_key]
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
    index_state = await _fetch_one_dict(
        """
        SELECT *
          FROM index_pipeline_state
         WHERE product_key = :product_key
            OR (merchant_id = :merchant_id AND product_group_id = :product_group_id)
         LIMIT 1
        """,
        {"product_key": product_key, "merchant_id": merchant_id, "product_group_id": group_id},
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
    merchant = await _fetch_one_dict(
        """
        SELECT *
          FROM merchants
         WHERE merchant_id = :merchant_id
         LIMIT 1
        """,
        {"merchant_id": merchant_id},
    ) or {}
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
    }
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
) -> List[Dict[str, Any]]:
    """Best-effort read of persisted per_sku_audit probe payloads."""
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
    return out


def _axis_coverage(probe_runs: Any) -> Dict[str, int]:
    counts: Counter = Counter()
    for run in _flatten_probe_runs(probe_runs):
        meta = run.get("axis_metadata") if isinstance(run.get("axis_metadata"), dict) else {}
        axis = str(meta.get("axis") or "unknown").strip() or "unknown"
        counts[axis] += 1
    return dict(counts)


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
        })
        if len(evidence) >= cap:
            break
    return evidence


def _failing_prompts(probe_runs: Any, cap: int = 20) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for run in _flatten_probe_runs(probe_runs):
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
        out.append({
            "query": run.get("query"),
            "axis": (run.get("axis_metadata") or {}).get("axis") if isinstance(run.get("axis_metadata"), dict) else None,
            "reason": "no first-party or correct-SKU grounded citation",
            "evidence_run_id": run.get("_probe_run_id"),
            "grounding_sources": run.get("grounding_sources") or [],
            "competitors_named": parsed.get("competitors_listed") or parsed.get("competitors_appearing") or run.get("competitors_listed") or [],
        })
        if len(out) >= cap:
            break
    return out


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
            gaps.append({
                "dimension": dimension,
                "bucket": bucket,
                "points": points,
                "max": max_points,
                "gap": gap,
                "reason": detail.get("reason"),
            })
    gaps.sort(key=lambda g: (-g["gap"], g["dimension"], g["bucket"]))
    return gaps[:cap]


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


def _impact_proxy_from_context(sku_ctx: Dict[str, Any]) -> float:
    offers = _get_offers(sku_ctx or {})
    prices = [
        _as_number(o.get("merchant_effective_price")) or _as_number(o.get("estimated_best_price")) or _as_number(o.get("list_price"))
        for o in offers
    ]
    prices = [p for p in prices if p is not None and p > 0]
    price = prices[0] if prices else 1.0
    return round(float(price) * math.log(1 + max(1, len(offers))), 4)


async def build_per_sku_report(
    sku_key: str,
    merchant_id: str,
    audit_run_id: Optional[str],
    provider_model_metadata: Optional[Mapping[str, Any]] = None,
    verify_outputs: Optional[List[Dict[str, Any]]] = None,
    verify_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sku_ctx = await load_sku_context(sku_key, merchant_id)
    probe_runs = await load_per_sku_probe_runs(sku_key, merchant_id, audit_run_id)
    product = _get_product(sku_ctx)
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
            _any_provider_probe_runs(probe_runs),
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
    primary_gaps = _primary_gaps(scores)
    failing_prompts = _failing_prompts(probe_runs)
    verify_summary_out = verify_summary or _verify_skipped_summary(
        reason="not_run",
        positives_count=len(_citation_positive_verify_candidates(sku_ctx, probe_runs)),
        verify_sample=None,
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
    )
    next_best_action = await attach_sku_strategic_brief(
        next_best_action,
        opportunity=opportunity,
        attribute_graph=attribute_graph,
        primary_gaps=primary_gaps,
        scores=scores,
        identity=identity,
        sku_title=(_get_sku(sku_ctx).get("title") or product.get("title")),
        merchant_host=normalize_host(product.get("canonical_url") or product.get("pdp_url")),
    )

    report = {
        "sku_key": sku_key,
        "product_key": sku_ctx.get("product_key") or product.get("product_key"),
        "content_key": sku_ctx.get("content_key") or product.get("content_key"),
        "sku_title": (_get_sku(sku_ctx).get("title") or product.get("title")),
        # Bad-name-tolerant resolved identity + confidence. When
        # identity.unresolved is True we only have a variant label / no
        # product-level name — downstream should treat low scores as
        # "enrich before trusting", not "invisible".
        "identity": identity,
        "scores": scores,
        "citation_by_provider": (
            citation_by_provider
            if not (sku_ctx.get("missing_inputs") and not product.get("product_key"))
            else {}
        ),
        "deliverability": build_sku_deliverability_prediction(sku_ctx, scores),
        "band": _sku_band(scores),
        "primary_gaps": primary_gaps,
        "verbatim_grounding_evidence": _grounding_evidence(probe_runs),
        "axis_coverage": _axis_coverage(probe_runs),
        "failing_prompts": failing_prompts,
        "impact_proxy": _impact_proxy_from_context(sku_ctx),
        "provider_models": provider_models,
        "model_is_override": _any_model_override(provider_models),
        "verify_summary": verify_summary_out,
        "verify_outputs": verify_outputs or [],
        "opportunity": opportunity,
        "next_best_action": next_best_action,
    }
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


def _dimension_distribution(per_sku_reports: List[Dict[str, Any]], dimension: str) -> Dict[str, Optional[int]]:
    values = [
        int((r.get("scores") or {}).get(dimension, {}).get("score"))
        for r in per_sku_reports
        if (r.get("scores") or {}).get(dimension, {}).get("score") is not None
    ]
    return {
        "median": _percentile(values, 0.5),
        "p25": _percentile(values, 0.25),
        "p75": _percentile(values, 0.75),
    }


def _overall_score(report: Dict[str, Any]) -> int:
    values = [
        int(payload.get("score"))
        for payload in (report.get("scores") or {}).values()
        if isinstance(payload, dict) and payload.get("score") is not None
    ]
    return min(values) if values else 0


def _fixability_for(dimension: str, bucket: Optional[str] = None) -> float:
    if dimension in {"identity", "content_richness", "routability"}:
        return 1.0
    if bucket == "authority_near_variant_rate":
        return 0.2
    if bucket in {"answer_quality_rate", "sku_mention_rate", "first_party_rate"}:
        return 0.5
    return 0.5


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
    priority_queue.sort(key=lambda row: row.get("priority_score") or 0, reverse=True)

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
    return "unclassified"


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
) -> Dict[str, Any]:
    sku_entries: List[Dict[str, Any]] = []
    host_matrix: Dict[str, Dict[str, Any]] = {}
    for report in per_sku_reports or []:
        sku_key = report.get("sku_key")
        probe_runs = probe_runs_by_sku.get(sku_key) if isinstance(probe_runs_by_sku, dict) else []
        host_rows: Dict[str, Dict[str, Any]] = {}
        reddit_threads: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for run in _flatten_probe_runs(probe_runs):
            provider = _run_provider(run)
            parsed = run.get("parsed") if isinstance(run.get("parsed"), dict) else {}
            url_match = run.get("url_match") if isinstance(run.get("url_match"), dict) else {}
            llm_report = url_match.get("llm_self_report") if isinstance(url_match.get("llm_self_report"), dict) else {}
            competitors = parsed.get("competitors_listed") or parsed.get("competitors_appearing") or run.get("competitors_listed") or []
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
                host = normalize_host(uri)
                if not host:
                    continue
                host_type = _classify_authority_host(host)
                row = host_rows.setdefault(host, {
                    "host": host,
                    "host_type": host_type,
                    "cites_exact_sku": False,
                    "cites_near_variant": False,
                    "cites_category_not_sku": False,
                    "prompts_cited_count": 0,
                    "providers": [],
                    "provider_counts": {},
                    "evidence_urls": [],
                    "evidence_excerpt": None,
                    "competitors_named": [],
                    "_queries": set(),
                })
                row["cites_exact_sku"] = bool(row["cites_exact_sku"] or exact)
                row["cites_near_variant"] = bool(row["cites_near_variant"] or near)
                row["cites_category_not_sku"] = bool(row["cites_category_not_sku"] or (not exact and not near))
                query = run.get("query") or ""
                if query not in row["_queries"]:
                    row["_queries"].add(query)
                    row["prompts_cited_count"] += 1
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
                    "skus": set(),
                    "prompts_cited_count": 0,
                    "providers": set(),
                    "provider_counts": defaultdict(int),
                })
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
            "reddit": {"subreddits": reddit_subreddits},
        })

    matrix_rows = []
    for row in host_matrix.values():
        matrix_rows.append({
            "host": row["host"],
            "host_type": row["host_type"],
            "skus": sorted(s for s in row["skus"] if s),
            "prompts_cited_count": row["prompts_cited_count"],
            "providers": sorted(row.get("providers") or []),
            "provider_counts": dict(sorted((row.get("provider_counts") or {}).items())),
        })
    matrix_rows.sort(key=lambda r: r["prompts_cited_count"], reverse=True)
    return {"skus": sku_entries, "hosts": matrix_rows}


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
    rows = await _fetch_all_dicts(
        f"""
        SELECT sku_key
          FROM catalog_skus
         WHERE merchant_id = :merchant_id
           AND product_key IN ({placeholders})
         ORDER BY product_key, sku_key
        """,
        values,
    )
    for row in rows:
        sku_key = row.get("sku_key")
        if sku_key and sku_key not in keys:
            keys.append(sku_key)
    return keys


def _dedupe_query_specs(specs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    seen = set()
    for query, axis in specs:
        q = str(query or "").strip()
        if not q:
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((q, str(axis or "intent").strip() or "intent"))
    return out


def _clean_prompt_term(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return text.strip(" \t\r\n,.;:/")


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


def _category_for_unbranded_prompts(
    product: Mapping[str, Any],
    product_type: str,
    graph: Mapping[str, Any],
) -> str:
    direct = _clean_prompt_term(
        product_type
        or product.get("product_type")
        or product.get("category")
    )
    if (
        direct
        and direct not in {"product", "products", "item", "items"}
        and not _noisy_prompt_category(direct)
    ):
        return direct
    for category in _graph_class_values(graph, "category"):
        if (
            category
            and category not in {"product", "products", "item", "items"}
            and not _noisy_prompt_category(category)
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
    if any(token in combined for token in ("collagen", "vitamin c", "niacin")):
        return "beauty supplement"
    if any(token in combined for token in ("supplement", "gummy", "gummies")):
        return "supplement"
    return ""


def _noisy_prompt_category(value: str) -> bool:
    cleaned = _clean_prompt_term(value)
    if not cleaned:
        return True
    tokens = set(re.findall(r"[a-z0-9]+", cleaned))
    if tokens & {"glow", "grape", "jelly", "orange", "shine"}:
        return True
    return False


def _unbranded_category_specs(
    *,
    category: str,
    graph: Mapping[str, Any],
    topics: List[str],
    bullets: List[str],
) -> List[Tuple[str, str]]:
    category = _clean_prompt_term(category)
    if not category or category in {"product", "products", "item", "items"}:
        return []
    specs: List[Tuple[str, str]] = [
        (f"best {category}", "category"),
        (f"what is the best {category}", "category"),
        (f"top {category}", "category"),
        (f"best {category} to buy online", "category"),
    ]
    audiences = _graph_class_values(graph, "audience")
    for audience in audiences[:3]:
        specs.extend([
            (f"best {category} for {audience}", "category"),
            (f"{category} for {audience}", "category"),
        ])

    attrs: List[str] = []
    for class_name in (
        "certification_constraint",
        "exclusion",
        "ingredient",
        "proof",
        "use_case",
    ):
        attrs.extend(_graph_class_values(graph, class_name))
    for attr in attrs[:6]:
        if category in attr:
            continue
        specs.append((f"best {attr} {category}", "attribute"))

    for topic in topics[:4]:
        cleaned = _clean_prompt_term(topic)
        if cleaned:
            specs.extend([
                (f"best {category} for {cleaned}", "category"),
                (f"{cleaned} {category}", "attribute"),
            ])
    for bullet in bullets[:4]:
        cleaned = _clean_prompt_term(bullet)
        if cleaned:
            specs.append((f"{cleaned} {category}", "attribute"))

    specs.extend([
        (f"recommended {category}", "category"),
        (f"best rated {category}", "category"),
        (f"{category} buying guide", "category"),
        (f"compare {category} options", "category"),
        (f"popular {category}", "category"),
        (f"what {category} should I buy", "category"),
    ])
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
    attribute_graph = build_sku_attribute_graph(product)
    unbranded_category = _category_for_unbranded_prompts(
        product,
        str(product_type or ""),
        attribute_graph,
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

    specs: List[Tuple[str, str]] = [
        (f"where can I buy {title}", "intent"),
        (f"shop {title} online", "intent"),
        (f"{title} for sale", "intent"),
    ]
    specs.extend(
        _unbranded_category_specs(
            category=unbranded_category,
            graph=attribute_graph,
            topics=topics,
            bullets=bullets,
        )
    )
    if variant_label and variant_label.lower() not in title.lower():
        # Use the human variant label (e.g. "14 Servings, 2-Week Routine") with
        # the full identity, not the opaque variant id.
        specs.extend([
            (f"{title} {variant_label}", "identity"),
            (f"buy {title} ({variant_label}) online", "identity"),
        ])

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
        if not query:
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
) -> List[Dict[str, Any]]:
    records = _dedupe_query_spec_records(records)
    target = max(1, int(target or 0))
    if len(records) >= target:
        return records[:target]

    axes = ("intent", "review", "comparison", "price", "category")
    idx = 1
    while len(records) < target:
        axis = axes[(idx - 1) % len(axes)]
        records.append({"query": f"{title} shopper question {idx}", "axis": axis})
        records = _dedupe_query_spec_records(records)
        idx += 1
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

    graph = build_sku_attribute_graph(product)
    target = 16 if int(prompts_per_sku or 0) > 16 else 6
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
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    _append_records(
        selected,
        _take_axis_records(
            base_records, {"intent"}, count=3, selected=selected,
        ),
        limit=target,
    )
    _append_records(
        selected,
        _take_axis_records(
            base_records, {"review", "comparison"}, count=3, selected=selected,
        ),
        limit=target,
    )
    _append_records(
        selected,
        _take_axis_records(
            base_records, {"category", "attribute"}, count=4, selected=selected,
        ),
        limit=target,
    )
    _append_records(
        selected,
        sidewalk_records[:_sidewalk_budget(target, len(sidewalk_records))],
        limit=target,
    )
    if target > 16:
        _append_records(
            selected,
            _take_axis_records(
                base_records, {"brand", "objection", "identity"},
                count=2,
                selected=selected,
            ),
            limit=target,
        )

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
    return _fill_per_sku_query_records(
        selected + remaining_base + remaining_sidewalk,
        target=target,
        title=title,
    )


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
    if not sidewalk_records:
        return _fill_per_sku_query_records(
            base_records,
            target=target,
            title=title,
        )
    if target <= 16:
        return _budgeted_wedge_query_records(
            base_records=base_records,
            sidewalk_records=sidewalk_records,
            target=target,
            title=title,
        )
    return _fill_per_sku_query_records(
        base_records + sidewalk_records,
        target=target,
        title=title,
    )


def _query_metadata_from_records(
    records: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    metadata: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if record.get("axis") != "sidewalk":
            continue
        query = str(record.get("query") or "").strip()
        if not query:
            continue
        metadata[query] = {
            "axis": "sidewalk",
            "attribute_basis": list(record.get("attribute_basis") or []),
            "evidence": list(record.get("evidence") or []),
            "intent_weight": float(record.get("intent_weight") or 0.0),
        }
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
            meta.update({
                "sidewalk_attribute_basis": list(
                    sidewalk_meta.get("attribute_basis") or []
                ),
                "sidewalk_evidence": list(sidewalk_meta.get("evidence") or []),
                "sidewalk_intent_weight": float(
                    sidewalk_meta.get("intent_weight") or 0.0
                ),
            })
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
) -> Dict[str, Any]:
    product = _get_product(sku_ctx or {})
    return {
        "probe_run_id": probe_run_id,
        "scan_mode": "per_sku_audit",
        "upstream_scan_mode": _PER_SKU_AUDIT_PROBE_SCAN_MODE,
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
) -> List[Dict[str, Any]]:
    """Run the normalized per-SKU audit probe loop for an already-built ctx."""
    safe_ctx = sku_ctx if isinstance(sku_ctx, dict) else {}
    sku_key = str(safe_ctx.get("sku_key") or "").strip() or "sku"
    target_prompts = max(1, int(prompts_per_sku or 0))
    query_records = _build_per_sku_audit_query_records(safe_ctx, target_prompts)
    query_specs = [
        (str(record.get("query") or ""), str(record.get("axis") or "intent"))
        for record in query_records
    ]
    query_metadata = _query_metadata_from_records(query_records)
    out: List[Dict[str, Any]] = []
    for provider_id in list((coverage or {}).get("providers") or []):
        model_info = provider_model_metadata.get(provider_id) or {}
        consecutive_failures = 0
        for chunk_idx, chunk in enumerate(_chunk_query_specs(query_specs), start=1):
            probe_run_id = (
                f"{audit_run_id or 'adhoc'}:{sku_key}:"
                f"{provider_id}:per_sku:{chunk_idx}"
            )
            try:
                result = await llm_client.probe(
                    scan_mode=_PER_SKU_AUDIT_PROBE_SCAN_MODE,
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
            except Exception as exc:  # noqa: BLE001 - isolate provider/chunk
                out.append(
                    _failed_per_sku_probe_payload(
                        provider=provider_id,
                        sku_key=sku_key,
                        sku_ctx=safe_ctx,
                        probe_run_id=probe_run_id,
                        error=str(exc),
                        model_info=model_info,
                    )
                )
                consecutive_failures += 1
                # Don't let one transient chunk timeout zero the SKU: keep
                # probing later chunks. Only bail this (sku, provider) once
                # failures are CONSECUTIVE (provider likely down), so we
                # don't burn the full timeout on every remaining chunk.
                if consecutive_failures >= _PER_SKU_AUDIT_MAX_CONSECUTIVE_CHUNK_FAILURES:
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


def _list_value(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _durable_competitor_for_brief(opportunity: Mapping[str, Any]) -> Optional[str]:
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
            if name:
                counts[name] += 1
        density = row.get("density") if isinstance(row.get("density"), Mapping) else {}
        features = (
            density.get("features")
            if isinstance(density.get("features"), Mapping)
            else {}
        )
        repeated_owner = str(features.get("repeated_owner") or "").strip()
        if repeated_owner and repeated_owner.lower() not in {"false", "none"}:
            counts[repeated_owner] += max(2, counts.get(repeated_owner, 0))
    if not counts:
        return None
    from services.sku_opportunity import _durable_competitor

    return _durable_competitor(counts)


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
    raw = run.get("raw")
    return str(raw or "")


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
    return out


def _merge_competitor_attribute_evidence(
    *,
    competitor: str,
    evidence_rows: List[Dict[str, str]],
) -> Any:
    by_attribute: Dict[str, Dict[str, str]] = {}
    for row in evidence_rows:
        attribute = str(row.get("attribute") or "").strip()
        provider = str(row.get("provider") or "").strip()
        verbatim = str(row.get("verbatim") or "").strip()
        if not attribute or not provider or not verbatim:
            continue
        by_attribute.setdefault(attribute, {
            "attribute": attribute,
            "provider": provider,
            "verbatim": verbatim[:240],
        })
    if not by_attribute:
        return "not_assessed"
    attributes = list(by_attribute)
    return {
        "status": "assessed",
        "competitor": competitor,
        "attributes_present": attributes[:8],
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
    competitor = _durable_competitor_for_brief(opportunity)
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


def _who_owns_prompt(row: Mapping[str, Any]) -> Optional[Any]:
    if row.get("who_owns"):
        return row.get("who_owns")
    controllers = stable_buyer_path_controller_hosts(row)
    if controllers:
        return controllers[0] if len(controllers) == 1 else controllers
    return None


def _prompt_sources(row: Mapping[str, Any]) -> List[Any]:
    return stable_buyer_path_controllers_for_row(row)[:3]


def _sku_intelligence_buyer_path_action(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if not is_third_party_controlled_lane(row):
        return None
    if not has_lane_demand(row):
        return None
    query = str(row.get("query") or "").strip()
    sources = _prompt_sources(row)
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


def _trim_sku_intelligence_prompt(row: Mapping[str, Any]) -> Dict[str, Any]:
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
        "who_owns": _who_owns_prompt(row),
        "sources": _prompt_sources(row),
        "source_route": row.get("source_route"),
        "demand_signal": row.get("demand_signal"),
        "attribute_basis": row.get("attribute_basis"),
        "opportunity_score": row.get("opportunity_score"),
        **_lane_priority_output_fields(row),
    }
    buyer_path_action = _sku_intelligence_buyer_path_action(row)
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
        _trim_sku_intelligence_prompt(row)
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
) -> Dict[str, List[Dict[str, Any]]]:
    """Run and shape v3 per-SKU citation probes before report assembly.

    PIVOTA-Agent caps one probe request at eight runs. The v3 contract is
    `prompts_per_sku` per provider, so this producer chunks the deterministic
    prompt set into upstream-safe batches and returns the normalized
    `per_sku_audit` payload that `load_per_sku_probe_runs` already reads.
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
    out: Dict[str, List[Dict[str, Any]]] = {}
    for sku_key in sku_keys:
        sku_ctx = await load_sku_context(sku_key, str(merchant_id))
        out[sku_key] = await _probe_per_sku_ctx(
            sku_ctx=sku_ctx,
            merchant_id=str(merchant_id),
            coverage=coverage,
            provider_model_metadata=provider_model_metadata,
            prompts_per_sku=target_prompts,
            audit_run_id=audit_run_id,
        )
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
    resolved_verify_providers = list(coverage.get("verify_providers") or [])
    if verify_providers is not None:
        resolved_verify_providers = [
            str(provider or "").strip().lower()
            for provider in verify_providers
            if str(provider or "").strip()
        ]
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
        authority_map = build_authority_map(per_sku_reports, probe_runs_by_sku)
        median_citation = (
            (brand_rollup.get("dimensions") or {})
            .get("citation", {})
            .get("median")
        )
        legacy_label, _legacy_explanation = verdict_for(
            int(median_citation or 0),
            int(median_citation or 0),
        )
        cost_summary = await _cost_summary_for_per_sku_audit(
            audit_run_id,
            probe_runs_by_sku,
            provider_model_metadata,
        )
        return {
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
            "verify_summary": _rollup_verify_summaries(per_sku_reports),
            "authority_map": authority_map,
            "legacy_verdict": legacy_label,
            "cost_summary": cost_summary,
        }

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
                prior_runs=prior_runs,
                # PR-D: per-product mint timestamp — when this row's
                # Pivota canonical sig was first created. Drives
                # merchant_view.diagnosis.indexing_arc_state's real
                # phase computation (replaces the static caveat).
                pivota_signature_minted_at=p.get("pivota_signature_minted_at"),
                # Phase 0: same merchant-level integration state on
                # every product report so the integration action
                # consistently fires (or stays absent) across products.
                integration_state=integration_state,
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
                prior_runs=prior_runs,
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
            "Consumers ask AI assistants comparison questions (\"vs AG1\", "
            "\"best greens powder under $50\", ingredient deep-dives) before "
            "buying daily-use supplements; brands without grounded "
            "attribution lose the comparison-shopping funnel to retailer "
            "and editorial roundups."
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
    category_visibility_score: int = 0,
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
                "subtitle": "Gemini today; ChatGPT search / Perplexity / Claude as those engines mature",
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
            "(grounded LLM citation via Gemini). The "
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
            "verdict_labels": most_recent.get("verdict_labels") or [],
        },
        "delta_from_most_recent": delta_from_most_recent,
        # The series for sparkline rendering (oldest → newest within
        # the history window). Capped at the # of prior_runs we got.
        "series": [
            {
                "requested_at": r.get("requested_at"),
                "visibility": r.get("visibility_score_avg"),
                "attribution": r.get("attribution_score_avg"),
                "category_visibility": r.get("category_visibility_score_avg"),
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
    except Exception as exc:  # noqa: BLE001 - audit must not fail on history
        logger.warning(
            "reaudit_delta attach failed merchant_id=%s prior_run_id=%s: %s",
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
                           merchant's own brand filtered out

    Capped at `cap` entries (default 10) to keep response size bounded
    even on large probe runs.
    """
    out: List[Dict[str, Any]] = []
    brand_lower = (merchant_brand or "").strip().lower()
    host_lower = (merchant_host or "").strip().lower()

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

        competitors: List[str] = []
        for raw in parsed.get("competitors_appearing") or []:
            if not isinstance(raw, str):
                continue
            name = raw.strip()
            if not name:
                continue
            name_lower = name.lower()
            if brand_lower and (brand_lower in name_lower or name_lower in brand_lower):
                continue
            competitors.append(name)
            if len(competitors) >= 5:
                break

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


def _is_cold_start_audit(integration_state: Optional[Dict[str, Any]]) -> bool:
    """A cold-start audit's integration_state is the synthetic
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
    cold_start = _is_cold_start_audit(integration_state)
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
    if integration_state is not None and not _is_cold_start_audit(integration_state):
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
            "verdict_label_display": _verdict_display_label(verdict_label),
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
        ),
        "pivota_value_prop": what_pivota_changes,
    }
    merchant_view["next_best_action"] = build_next_best_action(
        merchant_view=merchant_view,
        competitive_pressure=competitive_pressure,
        integration_state=integration_state,
        is_cold_start=_is_cold_start_audit(integration_state),
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
) -> Dict[str, Any]:
    """Return a single JSON-serializable dict with everything the UI
    needs to render the BD report. Pure function.

    `category_visibility_result` is optional (Phase 2a) — when provided,
    the report exposes a `category_visibility` block with score + queries,
    and `verdict.category_visibility_score` for downstream consumers."""
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
    verdict_evidence: Dict[str, Any] = {
        "attribution_runs_total": len(attribution_runs),
        "merchant_cited_runs": merchant_cited_runs,
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
        # `category_score` is Optional[int]; coerce to 0 when None
        # (probe didn't run) — the scorer treats 0 as "no measured
        # category visibility" and falls back to base passthrough.
        category_visibility_score=(category_score or 0),
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
    what_pivota_changes = _build_what_pivota_changes(
        merchant_name=merchant_name,
        merchant_pdp_url=merchant_pdp_url,
        attribution_score=attribution_score,
        attribution_runs=len(attribution_runs),
        merchant_cited_runs=merchant_cited_runs,
        category_retailer_hosts=category_retailer_hosts,
        category_visibility_score=category_score,
        merchant_platform=_merchant_platform,
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
        verdict_pill_text=_verdict_display_label(verdict_label),
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
    _is_cold = _is_cold_start_audit(integration_state)
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
        "verdict": {
            "label": verdict_label,
            # Client-facing softer rendering. Renderers that show a
            # bare label string (e.g. the headline VerdictBanner)
            # should prefer this; downstream code that branches on
            # the verdict enum keeps using `label`.
            "label_display": _verdict_display_label(verdict_label),
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
