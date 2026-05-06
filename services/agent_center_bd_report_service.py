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

This module has no DB dependencies — all data comes from `llm_client.probe`
results passed in by the caller.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from services import agent_center_llm_client as llm_client


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
    include_category_visibility: bool = True,
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
        )

    visibility = await _one("open_product_visibility_test")
    attribution = await _one("merchant_store_attribution_test")
    out: Dict[str, Dict[str, Any]] = {
        "visibility": visibility,
        "attribution": attribution,
    }
    # Skip category if product_type is missing — buildCategoryQueries
    # upstream returns [] in that case and the probe falls back to
    # product_entity_id which makes the category test meaningless.
    can_run_category = bool(base_context["product"].get("product_type"))
    if include_category_visibility and can_run_category:
        out["category_visibility"] = await _one("category_visibility_test")
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
    return False


def extract_cited_hosts(
    raw_runs: List[Dict[str, Any]],
    *,
    merchant_host: Optional[str],
    merchant_brand: Optional[str] = None,
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
                src, merchant_host=merchant_host, merchant_brand=merchant_brand,
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
VERDICT_STRONG = "STRONG"
VERDICT_PARTIAL = "PARTIAL"


def score_category_visibility(
    runs: List[Dict[str, Any]],
    *,
    merchant_host: Optional[str],
    merchant_brand: Optional[str],
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
      - merchant brand appears in `evidence_excerpt` text (Gemini quoted
        the brand name in its grounded answer)

    The LLM's `brand_appears: true` self-report alone is NOT enough —
    Gemini frequently hallucinates self-attribution. We require textual
    confirmation in the grounded output.

    Returns (score 0–100, per-run match details for audit/UI)."""
    if not runs:
        return (0, [])
    brand_lower = (merchant_brand or "").strip().lower()
    host_lower = (merchant_host or "").strip().lower()
    details: List[Dict[str, Any]] = []
    matched = 0
    for run in runs:
        url_match = run.get("url_match") or {}
        in_grounding = bool(url_match.get("in_grounding"))
        sources = _identify_run_sources(run)
        title_match = False
        for src in sources:
            label = (src.get("label") or "").lower()
            if brand_lower and brand_lower in label:
                title_match = True
                break
            if host_lower and host_lower in label:
                title_match = True
                break
        parsed = run.get("parsed") or {}
        excerpt = (parsed.get("evidence_excerpt") or "").lower()
        excerpt_match = bool(brand_lower and brand_lower in excerpt)
        is_match = in_grounding or title_match or excerpt_match
        if is_match:
            matched += 1
        details.append({
            "query": run.get("query") or "",
            "in_grounding": in_grounding,
            "title_match": title_match,
            "excerpt_match": excerpt_match,
            "matched": is_match,
        })
    score = round((matched / len(runs)) * 100)
    return (score, details)


def extract_category_competitors(
    runs: List[Dict[str, Any]],
    *,
    merchant_host: Optional[str],
    merchant_brand: Optional[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Aggregate the rich competitor data Gemini returns on category
    queries — currently dropped on the floor by the BD report. Two
    distinct lists are returned:

      - `competitor_brands`: Counter of brand names from Gemini's
        `competitors_appearing` field (e.g. "Patchology", "Wander
        Beauty"). Direct competitors the merchant should know about.
      - `retailer_hosts`: Counter of grounding source titles that
        aren't the merchant's own brand/host (e.g. "sephora.com",
        "oliveyoung.com"). Where AI traffic gets routed instead of
        the merchant's site — the strongest pitch evidence for the
        "retailers are eating your AI search funnel" framing.

    Within-run dedup: cite Sephora 3× in one answer = 1 for Sephora.
    """
    brand_counter: Counter = Counter()
    retailer_counter: Counter = Counter()
    brand_lower = (merchant_brand or "").strip().lower()
    host_lower = (merchant_host or "").strip().lower()
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
            run_hosts.add(label)
        for h in run_hosts:
            retailer_counter[h] += 1

    competitor_brands = [
        {"name": n, "times_cited": c}
        for n, c in brand_counter.most_common(15)
    ]
    retailer_hosts = [
        {"host": h, "times_cited": c}
        for h, c in retailer_counter.most_common(15)
    ]
    return (competitor_brands, retailer_hosts)

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


def verdict_for(
    visibility_score: int,
    attribution_score: int,
    peer_thresholds: Optional[Dict[str, int]] = None,
    *,
    category_visibility_score: Optional[int] = None,
) -> Tuple[str, str]:
    """Categorize the (visibility, attribution) pair into one of four
    BD-friendly verdicts. Returns (label, explanation paragraph).

    `peer_thresholds` (Phase 2c, optional) overrides the V1.5 default
    cutoffs with empirical percentile-of-peers values. Schema:
      {
        invisible_max: int,         # both scores < this → INVISIBLE
        strong_min: int,            # both scores ≥ this → STRONG
        misattributed_attr_max: int,  # attribution < this AND visibility ≥ invisible_max → MISATTRIBUTED
      }

    When peer_thresholds is supplied, the explanation paragraph is
    augmented with the percentile context so BD reps can read e.g.
    "your visibility is bottom-quartile vs category peers (median 71%)"
    instead of an abstract score. Missing keys fall back to defaults
    individually — partial overrides are supported.

    Source-of-truth for empirical thresholds: aggregate the BD reports
    + Pivota PDP self-baseline runs into a peer cohort, compute P25/P50/
    P75. Phase 2c ships the call site only — calibration data flows
    through this kwarg from a future scheduled job."""
    t = dict(DEFAULT_VERDICT_THRESHOLDS)
    if peer_thresholds:
        for k, v in peer_thresholds.items():
            if k in t and isinstance(v, (int, float)) and v >= 0:
                t[k] = int(v)

    invisible_max = t["invisible_max"]
    strong_min = t["strong_min"]
    misattr_attr_max = t["misattributed_attr_max"]

    # When using calibrated thresholds, prefix the explanation with the
    # peer-distribution context so the BD rep has comparative framing.
    peer_prefix = ""
    if peer_thresholds:
        peer_prefix = (
            f"_(Calibrated thresholds: peer-cohort INVISIBLE < {invisible_max}/100, "
            f"STRONG ≥ {strong_min}/100. "
            f"Your visibility {visibility_score}/100, attribution {attribution_score}/100.)_  \n\n"
        )

    # VISIBLE VIA RETAILERS — sharpest BD framing. Triggers when the
    # category test surfaced the brand strongly but the merchant's
    # own first-party attribution lags far behind: the brand IS
    # findable in the AI channel, just mostly through retailer pages
    # (Sephora, Vogue Scandinavia, skinsort, etc.) rather than the
    # merchant's own URL.
    #
    # The check is gap-based, not absolute: it fires when (category -
    # attribution) >= invisible_max AND attribution < strong_min. This
    # catches both the BoJ-class case (cat=67, attr=0 → gap 67) and
    # the COSRX-class case (cat=100, attr=33 → gap 67) — both have
    # "retailers eating most of the AI funnel" as the right pitch
    # framing. A merchant with cat=80, attr=70 (gap=10) stays in
    # STRONG/PARTIAL because their own attribution is already
    # consistently captured.
    #
    # Checked BEFORE INVISIBLE / MISATTRIBUTED so cat-strong + attr-
    # weak cases land here even when raw attribution clears the
    # misattributed_attr_max floor.
    if (
        category_visibility_score is not None
        and category_visibility_score >= invisible_max
        and attribution_score < strong_min
        and (category_visibility_score - attribution_score) >= invisible_max
    ):
        return (
            VERDICT_VIA_RETAILERS,
            peer_prefix + (
                "AI shopping agents recognize this brand in category-level "
                "queries — but the funnel mostly routes consumers through "
                "third-party retailers (Sephora, Vogue Scandinavia, Ulta, "
                "Target, beauty marketplaces) rather than the merchant's own "
                "URL. The brand is findable in the AI channel; the merchant "
                "just isn't capturing the funnel consistently. Pivota's value "
                "here is two-part and complementary to existing retail "
                "distribution: (1) a canonical AI-channel PDP that captures "
                "direct first-party attribution as AI shopping grows from "
                "~12% to a projected 25-30% of D2C beauty traffic over the "
                "next 24 months — every retailer-cited query today is a "
                "deferred margin and a customer relationship the merchant "
                "doesn't own; (2) an in-chat transaction surface (Pivota's "
                "agentic-commerce protocol) so consumers asking Gemini / "
                "ChatGPT can complete checkout without leaving the AI "
                "assistant. This is not an SEO fix — the merchant's existing "
                "retail channels work fine. It's the AI-native transaction "
                "surface the merchant doesn't have today, sized for the "
                "channel that will dominate D2C beauty in 24 months."
            ),
        )
    if visibility_score < invisible_max and attribution_score < invisible_max:
        return (
            VERDICT_INVISIBLE,
            peer_prefix + (
                "AI shopping agents don't surface this product at all when "
                "consumers ask natural buyer queries. The merchant has effectively "
                "zero presence in this channel today. As consumer search continues "
                "to migrate from Google to ChatGPT / Gemini / Perplexity (~12% of "
                "D2C beauty traffic today, projected 25-30% by 2028), the merchant "
                "is losing access to a fast-growing acquisition surface they have "
                "no way to influence directly. Pivota's foundation step here is the "
                "canonical AI-channel PDP that gets the brand cited at all; once "
                "visibility clears the floor, in-chat checkout via Pivota's "
                "agentic-commerce protocol becomes the second leverage point — "
                "consumers complete the transaction inside Gemini / ChatGPT instead "
                "of being routed through retailer.com."
            ),
        )
    if attribution_score < misattr_attr_max and visibility_score >= invisible_max:
        return (
            VERDICT_MISATTRIBUTED,
            peer_prefix + (
                "AI agents recognize this product but consistently direct consumers "
                "to third-party retailers (marketplaces, beauty blogs, competitor "
                "stores) instead of the merchant's own site. Every cited URL that's "
                "not the merchant's is lost organic traffic — and a margin hit if "
                "the cited path is a third-party reseller. The demand exists; it's "
                "just being captured by competitors. Pivota's value here is two-part "
                "and complementary to existing retail distribution: (1) the canonical "
                "AI-channel PDP captures direct first-party attribution as AI shopping "
                "grows from ~12% to a projected 25-30% of D2C beauty discovery over "
                "the next 24 months; (2) in-chat checkout via Pivota's agentic-"
                "commerce protocol — consumers ask Gemini and complete purchase "
                "inside the assistant, no redirect, no retailer markup. This is "
                "AI-channel-native commerce, not an SEO fix."
            ),
        )
    if visibility_score >= strong_min and attribution_score >= strong_min:
        return (
            VERDICT_STRONG,
            peer_prefix + (
                "AI agents reliably surface this product AND cite the merchant's own "
                "canonical URL as the buying path. The discovery-and-attribution "
                "problem is solved at the audit level. The next AI-channel UX "
                "boundary is in-chat checkout: when a consumer asks Gemini / ChatGPT "
                "and decides to buy, completing the purchase inside the assistant "
                "(via Pivota's agentic-commerce protocol) rather than redirecting to "
                "the merchant's site is what AI-native commerce looks like at this "
                "stage. Today every consumer who clicks through is one cart-"
                "abandonment risk + one redirect away from the conversion they "
                "already committed to. Pivota's leverage at STRONG is highest here, "
                "not at the SEO/attribution layer the merchant has already won."
            ),
        )
    return (
        VERDICT_PARTIAL,
        peer_prefix + (
            "Mixed result — the product gets surfaced sometimes, and gets "
            "attributed to the merchant's own URL sometimes, but neither is "
            "consistent. The foundation is partly there. Pivota's two-part value "
            "prop applies cleanly: (1) tighten first-party attribution on the "
            "queries that currently route to retailers via the canonical "
            "AI-channel PDP; (2) for the queries that DO already reach the merchant, "
            "add in-chat checkout via Pivota's agentic-commerce protocol so "
            "consumers complete purchase inside Gemini / ChatGPT instead of being "
            "redirected out. The failing-query table below shows which gap is "
            "larger — that determines which lever to pull first in onboarding."
        ),
    )


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


_REAL_PROVIDERS = {"gemini"}


def _classify_provider(upstream_provider: str) -> Dict[str, Any]:
    """Categorize what the upstream actually used.

    Returns:
      - is_real: True if upstream ran a real LLM (gemini), False on any
        mock variant.
      - reason: a human-readable explanation surfaced in UI when a
        fallback happened. None when is_real.
    """
    p = (upstream_provider or "").strip()
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


_BRAND_REPORT_MAX_PRODUCTS = 5


async def run_brand_report(
    *,
    merchant_name: str,
    merchant_domain: Optional[str],
    products: List[Dict[str, Any]],
    provider: str = "gemini",
    max_runs: int = 3,
    include_category_visibility: bool = True,
) -> Dict[str, Any]:
    """Run BD probes against up to 5 products of one merchant and
    aggregate into a brand-level report.

    `products` items: { title, vendor?, product_type?, pdp_url }

    Returns:
      {
        merchant_name, merchant_domain, timestamp, provider,
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
    if len(products) > _BRAND_REPORT_MAX_PRODUCTS:
        raise ValueError(
            f"products capped at {_BRAND_REPORT_MAX_PRODUCTS} per brand "
            f"report (received {len(products)}). Cost guard — see #280."
        )

    per_product: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for idx, p in enumerate(products):
        pdp_url = (p.get("pdp_url") or "").strip()
        title = (p.get("title") or "").strip()
        if not pdp_url or not title:
            failed.append({
                "pdp_url": pdp_url,
                "title": title,
                "error": "pdp_url and title are required for each product",
            })
            continue
        try:
            probes = await run_bd_probes(
                merchant_name=merchant_name,
                merchant_pdp_url=pdp_url,
                product_title=title,
                product_vendor=p.get("vendor"),
                product_type=p.get("product_type"),
                provider=provider,
                max_runs=max_runs,
                include_category_visibility=include_category_visibility,
            )
            structured = build_structured_report(
                merchant_name=merchant_name,
                merchant_pdp_url=pdp_url,
                product_title=title,
                product_vendor=p.get("vendor"),
                product_type=p.get("product_type"),
                visibility_result=probes["visibility"],
                attribution_result=probes["attribution"],
                category_visibility_result=probes.get("category_visibility"),
                provider=provider,
            )
            per_product.append(structured)
        except Exception as exc:  # noqa: BLE001 — per-product isolation
            failed.append({
                "pdp_url": pdp_url,
                "title": title,
                "error": str(exc),
            })

    aggregate = _aggregate_brand_scores(per_product)
    aggregate["products_count"] = len(products)
    aggregate["products_succeeded"] = len(per_product)
    aggregate["products_failed"] = len(failed)

    cross_competitors = _aggregate_brand_competitors(per_product)

    return {
        "merchant_name": merchant_name,
        "merchant_domain": (merchant_domain or "").strip() or None,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": provider,
        "per_product": per_product,
        "aggregate": aggregate,
        "cross_product_competitors": cross_competitors,
        "failed": failed,
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
        brand_label, brand_explanation = verdict_for(
            int(avg_visibility), int(avg_attribution),
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


def _aggregate_brand_competitors(per_product: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Walk per-product attribution.competitor_hosts, sum across
    products, return ranked top 15. The aggregate "who's stealing
    your AI traffic across the whole brand" view — typically more
    pitch-relevant than per-product because BD wants to call out
    "Sephora captures 12 / 15 of your queries across these 5 SKUs"."""
    counter: Counter = Counter()
    for product in per_product:
        for entry in (product.get("attribution") or {}).get("competitor_hosts") or []:
            host = entry.get("host")
            count = entry.get("times_cited") or 0
            if host and count:
                counter[host] += int(count)
    return [
        {"host": h, "times_cited": c}
        for h, c in counter.most_common(15)
    ]


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
}

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
            "AI shopping is ~9% of D2C fitness/wellness traffic and growing "
            "~32% YoY. Consumers research equipment + supplements through "
            "AI assistants before purchase; not appearing in those answers "
            "is invisible top-of-funnel."
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
    ]),
    ("fitness", [
        "supplement", "protein", "vitamin", "creatine", "yoga", "mat",
        "dumbbell", "treadmill", "fitness", "workout",
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
    keywords for products where product_type is missing or generic."""
    haystacks = [
        (product_type or "").lower(),
        (product_title or "").lower(),
        (product_vendor or "").lower(),
    ]
    haystack = " ".join(s for s in haystacks if s)
    if not haystack:
        return dict(_INDUSTRY_CONTEXT_DEFAULT)
    for category, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in haystack:
                return dict(_INDUSTRY_CONTEXT_BY_CATEGORY[category])
    return dict(_INDUSTRY_CONTEXT_DEFAULT)


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


def _generate_action_items(
    *,
    verdict_label: str,
    visibility_runs: List[Dict[str, Any]],
    attribution_runs: List[Dict[str, Any]],
    competitor_hosts: List[Dict[str, Any]],
    merchant_cited_runs: int,
    runs_with_any_citation: int,
    attribution_score: int = 0,
    category_retailer_hosts: Optional[List[Dict[str, Any]]] = None,
    category_competitor_brands: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Return a list of 3-5 specific action items. Each item has
    `severity` (critical|high|medium|low), `title`, `body` (BD-rep
    facing prose), and optional `evidence` (the failed query / cited
    competitor host that drives this action)."""
    items: List[Dict[str, Any]] = []

    # Pull the failures we'll reference in evidence text.
    failed_attribution_queries = [
        run.get("query") or ""
        for run in attribution_runs
        if not (run.get("parsed") or {}).get("merchant_url_found")
    ]
    failed_visibility_queries = [
        run.get("query") or ""
        for run in visibility_runs
        if not ((run.get("parsed") or {}).get("product_visible") and (run.get("grounding_chunks") or []))
    ]
    top_competitor = competitor_hosts[0] if competitor_hosts else None

    # Action 1: severity-stratified headline that ties the verdict to
    # the merchant's specific failure pattern.
    if verdict_label == "INVISIBLE":
        items.append({
            "severity": "critical",
            "title": "Index your canonical PDPs with Search Console",
            "body": (
                "AI shopping agents return zero grounded references to your "
                "store across the queries we tested. The most likely root "
                "cause is that Google has not yet indexed your flagship PDPs "
                "(grounded LLM citations are downstream of Google's index). "
                "Submit your sitemap.xml to Search Console, request URL "
                "Inspection indexing for your top 5 SKUs, and re-test in "
                "72 hours."
            ),
        })
    elif verdict_label == "VISIBLE BUT MISATTRIBUTED":
        items.append({
            "severity": "critical",
            "title": "Reclaim direct attribution from third-party retailers",
            "body": (
                "AI agents recognize the product but consistently send "
                "consumers to third-party retailers (marketplaces, beauty "
                "blogs, competitor stores) instead of your own URL. Every "
                "cited URL that's not yours is lost organic traffic and a "
                "margin hit if the cited path is a reseller. This is the "
                "highest-impact failure mode — the demand exists; it's "
                "just being captured by competitors."
            ),
        })
    elif verdict_label == "VISIBLE VIA RETAILERS":
        # Build a top-retailer mention into the body when we have data.
        retailer_phrase = ""
        if category_retailer_hosts:
            top_retailers = [
                r["host"] for r in category_retailer_hosts[:3] if r.get("host")
            ]
            if top_retailers:
                retailer_phrase = (
                    f" Top retailers capturing the AI-channel funnel today: "
                    f"{', '.join(top_retailers)}."
                )
        # Conditional opener: when the merchant captures SOME first-party
        # attribution we shouldn't say "every grounded citation" — that
        # reads false to a brand like COSRX that already has cosrx.com
        # surfaced 1/3 of buyer-intent queries. Lead with the actual
        # gap percentage instead.
        attr_score = int(attribution_score)
        if attr_score == 0:
            opener = (
                "Your brand IS findable in AI-channel category queries — but "
                "every grounded citation routes consumers through third-party "
                "retailers instead of your own URL."
            )
        else:
            gap_pct = max(0, 100 - attr_score)
            opener = (
                f"Your brand IS findable in AI-channel category queries — and "
                f"you capture {attr_score}% of buyer-intent queries to your "
                f"own URL today. The remaining {gap_pct}% routes through "
                f"third-party retailers."
            )
        items.append({
            "severity": "critical",
            "title": "Capture the AI-channel funnel that retailers are taking today",
            "body": (
                opener + retailer_phrase + " "
                "Pivota's canonical PDP closes that gap two ways, "
                "complementary to existing retail distribution: (a) consistent "
                "first-party attribution as AI shopping grows from ~12% to a "
                "projected 25-30% of D2C beauty traffic over the next 24 months; "
                "(b) in-chat checkout via Pivota's agentic-commerce protocol — "
                "consumers ask Gemini / ChatGPT and buy from the brand directly "
                "without leaving the assistant or being routed through a "
                "retailer's checkout. This is AI-channel-native commerce, not "
                "an SEO fix."
            ),
            "evidence": (
                {"top_retailer_hosts": [r["host"] for r in category_retailer_hosts[:5] if r.get("host")]}
                if category_retailer_hosts
                else None
            ),
        })
    elif verdict_label == "STRONG":
        items.append({
            "severity": "low",
            "title": "Maintain attribution with monitoring + drift detection",
            "body": (
                "AI agents reliably surface your product AND cite your "
                "canonical URL as the buying path. Goal state. Pivota's "
                "role here is monitoring: alert on attribution drift, "
                "detect schema regressions, surface new competitor cites "
                "before they erode share."
            ),
        })
    else:  # PARTIAL
        items.append({
            "severity": "high",
            "title": "Close the gap on inconsistent queries",
            "body": (
                "Your product gets surfaced sometimes and gets attributed "
                "to your URL sometimes, but neither is consistent. The "
                "specific queries below are where the gaps are — close "
                "those before pitching for full Pivota onboarding."
            ),
        })

    # Action 2: top competitor capture, named with frequency.
    if top_competitor and top_competitor.get("times_cited", 0) >= 2:
        items.append({
            "severity": "high",
            "title": f"Top citation drain: {top_competitor['host']}",
            "body": (
                f"`{top_competitor['host']}` was cited by Gemini in "
                f"{top_competitor['times_cited']} of the queries we tested. "
                "They're capturing demand that should be yours — every "
                "consumer arriving via that path is one your direct site "
                "didn't get. If they're a reseller, the margin loss is "
                "compounded; if they're a marketplace, you're trading a "
                "first-party customer relationship for a transaction."
            ),
            "evidence": {"competitor_host": top_competitor["host"]},
        })

    # Action 3: zero-citation case (more severe than just missing in
    # individual runs — means the merchant's URL never showed in ANY
    # grounded source).
    if runs_with_any_citation > 0 and merchant_cited_runs == 0:
        items.append({
            "severity": "critical",
            "title": "Zero direct AI-channel attribution today",
            "body": (
                f"Across {runs_with_any_citation} queries that returned "
                "grounded sources, your verified URL appeared in zero of "
                "them. Every grounded citation went to a third party. "
                "First-party AI attribution is currently zero — there is "
                "no organic AI-channel funnel."
            ),
        })

    # Action 4: specific failed-attribution query references (up to 2).
    if failed_attribution_queries:
        sample = ", ".join(
            f'"{_truncate_query(q)}"'
            for q in failed_attribution_queries[:2]
        )
        items.append({
            "severity": "medium",
            "title": "Specific queries where your URL was missing",
            "body": (
                f"Gemini's grounded answer to {sample} did not include "
                "your verified PDP URL. These are buyer-intent queries "
                "that should naturally route to your store; closing them "
                "is the fastest path to attribution lift."
            ),
            "evidence": {"failed_queries": failed_attribution_queries[:5]},
        })

    # Action 5: visibility gap (open-product test failed grounding gate).
    # Suppressed for VIA_RETAILERS — the action's "your PDP isn't indexed"
    # framing is misleading for retail-strong brands where category
    # discoverability is high (their PDPs are demonstrably indexed; the
    # buyer-intent queries are just too long-tail). The right call for
    # VIA_RETAILERS is action #1's value-prop framing, not SEO hygiene.
    if (
        failed_visibility_queries
        and verdict_label != "STRONG"
        and verdict_label != "VISIBLE VIA RETAILERS"
    ):
        items.append({
            "severity": "medium",
            "title": "Strengthen schema + sitemap inclusion for visibility",
            "body": (
                "The product wasn't surfaced with grounded sources on at "
                "least one query — meaning Gemini either has no live-web "
                "knowledge of the product, or your PDP isn't indexed enough "
                "for grounded retrieval. Pivota's canonical PDP includes "
                "Schema.org Product + Breadcrumb + sitemap submission, "
                "which is the foundation grounded LLMs need to surface "
                "your product confidently."
            ),
        })

    # Cap at 5 items so the BD page stays scannable.
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
    "median_visibility": 67,
    "median_attribution": 50,
    "sample_size_pdps": 6,
    "as_of_date": "2026-05-06",
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
) -> Dict[str, Any]:
    """Return the "What Pivota changes after onboarding" structured block.

    Three levers, all derived from existing report data — no new probe
    calls. The merchant reads this section to see the post-onboarding
    delta, not just the pre-onboarding diagnosis.

      1. First-party AI attribution — converts retailer-routed category
         visibility into the merchant's own URL via the canonical
         AI-channel PDP.
      2. In-chat checkout — the agentic-commerce protocol lets consumers
         complete purchase inside Gemini / ChatGPT instead of clicking
         out to merchant.com or being redirected through retailer.com.
      3. Pivota PDP baseline reference — current median visibility +
         attribution figures across Pivota's 6 canonical seed PDPs, so
         the merchant has a concrete "this is what your AI-channel
         surface looks like after onboarding" anchor."""
    gap_pct = max(0, 100 - int(attribution_score))
    top_retailers = [
        r["host"] for r in (category_retailer_hosts or [])[:3] if r.get("host")
    ]
    retailer_phrase = (
        ", ".join(top_retailers) if top_retailers else "third-party retailers"
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
        f"{retailer_phrase}."
    )
    return {
        "today_summary": today_summary,
        "after_onboarding": [
            {
                "title": "First-party AI attribution",
                "today": (
                    f"~{gap_pct}% of the AI-channel funnel routes through "
                    f"{retailer_phrase} — every retailer-cited query is a "
                    f"margin hit (reseller markup) and a customer "
                    f"relationship the merchant doesn't own."
                ),
                "after": (
                    "Pivota canonical PDP captured in grounded Gemini answers "
                    "→ first-party attribution + direct customer relationship, "
                    "complementary to existing retail distribution."
                ),
            },
            {
                "title": "In-chat checkout (the AI-native lever)",
                "today": (
                    f"Consumers ask Gemini / ChatGPT → click out to "
                    f"{merchant_pdp_url} or get routed through retailer.com → "
                    f"cart-abandonment risk + redirect friction + retailer "
                    f"markup if the path is a reseller."
                ),
                "after": (
                    "Consumers complete checkout inside Gemini / ChatGPT via "
                    "Pivota's agentic-commerce protocol — no redirect, no "
                    "retailer margin loss, first-party transaction data. This "
                    "is the AI-channel UX boundary the merchant doesn't have "
                    "any path to today."
                ),
            },
            {
                "title": "Pivota PDP baseline reference",
                "value": (
                    f"Pivota's {PIVOTA_PDP_BASELINE_REFERENCE['sample_size_pdps']} "
                    f"canonical PDPs currently surface in Gemini grounding "
                    f"with median visibility "
                    f"{PIVOTA_PDP_BASELINE_REFERENCE['median_visibility']}/100 "
                    f"and median attribution "
                    f"{PIVOTA_PDP_BASELINE_REFERENCE['median_attribution']}/100 "
                    f"(internal baseline run, "
                    f"{PIVOTA_PDP_BASELINE_REFERENCE['as_of_date']}). Onboarded "
                    f"merchants inherit this AI-channel surface for their SKUs."
                ),
            },
        ],
    }


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
    competitors, merchant_cited_runs, runs_with_any_citation = extract_cited_hosts(
        attribution_runs,
        merchant_host=merchant_host,
        merchant_brand=merchant_brand,
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
        )
        category_competitor_brands, category_retailer_hosts = (
            extract_category_competitors(
                category_runs,
                merchant_host=merchant_host,
                merchant_brand=merchant_brand,
            )
        )
    elif category_visibility_result is not None:
        # Probe ran but returned no runs — keep score=0 for consistency.
        category_score = 0

    verdict_label, verdict_explanation = verdict_for(
        visibility_score,
        attribution_score,
        category_visibility_score=category_score,
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
    action_items = _generate_action_items(
        verdict_label=verdict_label,
        visibility_runs=visibility_runs,
        attribution_runs=attribution_runs,
        competitor_hosts=competitor_hosts_list,
        merchant_cited_runs=merchant_cited_runs,
        runs_with_any_citation=runs_with_any_citation,
        attribution_score=attribution_score,
        category_retailer_hosts=category_retailer_hosts,
        category_competitor_brands=category_competitor_brands,
    )
    industry_context = _industry_context_for(
        product_type=product_type,
        product_vendor=product_vendor,
        product_title=product_title,
    )
    what_pivota_changes = _build_what_pivota_changes(
        merchant_name=merchant_name,
        merchant_pdp_url=merchant_pdp_url,
        attribution_score=attribution_score,
        attribution_runs=len(attribution_runs),
        merchant_cited_runs=merchant_cited_runs,
        category_retailer_hosts=category_retailer_hosts,
        category_visibility_score=category_score,
    )

    return {
        "merchant_name": merchant_name,
        "merchant_pdp_url": merchant_pdp_url,
        "merchant_host": merchant_host,
        "product": {
            "title": product_title,
            "vendor": product_vendor or None,
            "product_type": product_type or None,
        },
        "provider": provider,
        "upstream_status": upstream_status,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": {
            "label": verdict_label,
            "explanation": verdict_explanation,
            "visibility_score": visibility_score,
            "attribution_score": attribution_score,
            "category_visibility_score": category_score,  # null when category test wasn't run
        },
        "industry_context": industry_context,
        "action_items": action_items,
        "what_pivota_changes": what_pivota_changes,
        "visibility": {
            "score": visibility_score,
            "runs": len(visibility_runs),
            "queries": _per_query_rows(visibility_runs, "product_visible"),
        },
        "attribution": {
            "score": attribution_score,
            "runs": len(attribution_runs),
            "merchant_cited_runs": merchant_cited_runs,
            "runs_with_any_citation": runs_with_any_citation,
            "queries": _per_query_rows(attribution_runs, "merchant_url_found"),
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
    sections.append(f"## Verdict: **{v['label']}**\n")
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

    # What Pivota changes — the post-onboarding delta. Renders between the
    # diagnostic sections (verdict / industry / actions) and the per-query
    # tables, so the merchant sees the value-prop framing before scrolling
    # into the raw data.
    wpc = report.get("what_pivota_changes") or {}
    if wpc.get("after_onboarding"):
        sections.append("## What Pivota changes after onboarding\n")
        if wpc.get("today_summary"):
            sections.append(f"**Today:** {wpc['today_summary']}\n")
        sections.append("**After Pivota onboarding (two-part value prop):**\n")
        levers = wpc.get("after_onboarding", [])
        comparison_levers = [lev for lev in levers if "today" in lev and "after" in lev]
        if comparison_levers:
            table_rows = ["| Lever | Today | After Pivota |", "|---|---|---|"]
            for lev in comparison_levers:
                t = (lev.get("title") or "").replace("|", "\\|")
                td = (lev.get("today") or "").replace("|", "\\|").replace("\n", " ")
                af = (lev.get("after") or "").replace("|", "\\|").replace("\n", " ")
                table_rows.append(f"| **{t}** | {td} | {af} |")
            sections.append("\n".join(table_rows) + "\n")
        for lev in levers:
            if "value" in lev:
                sections.append(
                    f"_Pivota PDP reference: {lev['value']}_\n"
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
        retailers = cat.get("retailer_hosts") or []
        if retailers:
            sections.append(
                "**Where category traffic is being routed (retailers cited "
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
    out = ["| Retailer / host | Category queries citing |", "|---|---|"]
    for entry in retailers[:top_n]:
        out.append(f"| `{entry['host']}` | {entry['times_cited']} |")
    return "\n".join(out)


def _md_competitor_brand_table(brands: List[Dict[str, Any]], top_n: int = 10) -> str:
    if not brands:
        return "_(none named)_"
    out = ["| Competitor brand | Category queries naming |", "|---|---|"]
    for entry in brands[:top_n]:
        out.append(f"| {entry['name']} | {entry['times_cited']} |")
    return "\n".join(out)
