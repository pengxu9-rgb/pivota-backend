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
from services.audit_playbook_engine import select_playbooks
from services.cited_host_classifier import classify_cited_hosts, classify_host
from services.pivota_indexing_arc import compute_indexing_arc_state


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

    def _brand_in(text: str) -> bool:
        if not brand_lower:
            return False
        if brand_pattern is not None:
            return brand_pattern.search(text) is not None
        return brand_lower in text

    details: List[Dict[str, Any]] = []
    matched = 0
    for run in runs:
        url_match = run.get("url_match") or {}
        in_grounding = bool(url_match.get("in_grounding"))
        sources = _identify_run_sources(run)
        title_match = False
        for src in sources:
            label = (src.get("label") or "").lower()
            if _brand_in(label):
                title_match = True
                break
            if host_lower and host_lower in label:
                title_match = True
                break
        parsed = run.get("parsed") or {}
        excerpt = (parsed.get("evidence_excerpt") or "").lower()
        excerpt_match = _brand_in(excerpt)
        # excerpt_match alone no longer credits a run. Gemini's
        # evidence excerpt is LLM-generated text and can mention the
        # brand even when no grounding source cites it (hallucination
        # / paraphrase). Require corroboration from url_match (the
        # merchant URL was in grounding chunks) or title_match (a
        # grounded source title contains the brand). Excerpt-only is
        # surfaced in `details` as a signal-quality flag, not a hit.
        is_match = in_grounding or title_match
        if is_match:
            matched += 1
        details.append({
            "query": run.get("query") or "",
            "in_grounding": in_grounding,
            "title_match": title_match,
            "excerpt_match": excerpt_match,
            "matched": is_match,
            "excerpt_only_signal": (excerpt_match and not is_match),
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


# ---------------------------------------------------------------------------
# Competitive pressure — the sharpest BD framing. A merchant might shrug
# off "your visibility is 0/3" if their products still sell through
# retailers. But "competitor X has their own .com cited 2/3 times in the
# same category queries you're invisible in" — that's an immediate
# competitive emergency the merchant can't ignore.
# ---------------------------------------------------------------------------


def _brand_discriminator(brand_name: str) -> Optional[str]:
    """Pick the longest >=4-char alphanumeric word from the brand name to
    use as a discriminator when matching against retailer hostnames.
    e.g. "Beauty of Joseon" → "joseon"; "The Ordinary" → "ordinary";
    "PEACH & LILY" → "peach" (passes through 'peachandlily' first segment)."""
    if not brand_name:
        return None
    import re as _re
    words = _re.findall(r"\w+", brand_name.lower())
    long_words = [w for w in words if len(w) >= 4]
    if not long_words:
        return None
    return max(long_words, key=len)


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
        Gemini grounding for those same category queries. Heuristic match
        — see _brand_discriminator. The presence of even ONE such peer
        is the BD pressure point.

    The framing string below tells the right story for both cases:
      (a) some peers are first-party visible — urgent: "every retailer-
          routed query is a customer they won and you didn't see"
      (b) no peers are first-party visible — first-mover opportunity:
          "the entire category is retailer-mediated; whoever onboards
          first owns the AI-channel surface"
    """
    peers_named = list(category_competitor_brands or [])
    retailer_hosts = list(category_retailer_hosts or [])

    peers_with_fp: List[Dict[str, Any]] = []
    for peer in peers_named:
        brand = peer.get("name") or ""
        disc = _brand_discriminator(brand)
        if not disc:
            continue
        for host_entry in retailer_hosts:
            host = (host_entry.get("host") or "").lower()
            first_segment = host.split(".")[0] if host else ""
            if disc in first_segment:
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
            f"**Competitive pressure: real and immediate.** Of the "
            f"{len(peers_named)} competitor brands AI agents name when "
            f"consumers ask about this category, "
            f"{len(peers_with_fp)} have their own .com cited in Gemini "
            f"grounding for the same queries — "
            + ", ".join(
                f"{p['brand']} ({p['first_party_host']})"
                for p in peers_with_fp[:3]
            )
            + (
                f". Your URL appears in {merchant_attribution_score}% of "
                f"buyer-intent queries; theirs appears multiple times. "
                f"Every retailer-routed query you have today is a customer "
                f"those competitors won and you didn't see."
                if not merchant_first_party_visible
                else ". You also appear first-party — but at a lower "
                "frequency than these peers."
            )
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
                f"the cited surface is split across third-party hosts "
                f"({top_named}) instead of any one brand's own .com"
                if top_named
                else "the cited surface is split across third-party hosts"
            )
        else:
            cited_phrase = (
                "the cited surface is split across third-party hosts"
            )
        framing = (
            f"**First-mover opportunity.** Of the {len(peers_named)} "
            f"competitor brands AI agents name in this category, NONE "
            f"appear to have their own .com cited in Gemini grounding "
            f"today (we may be under-counting — see follow-up note on "
            f"brand-from-grounding detection) — {cited_phrase}. "
            f"Whichever brand wins first-party attribution first + "
            f"completes the 30-90 day indexing arc owns the surface "
            f"before the rest of the category notices the channel exists."
        )

    return {
        "title": "Competitive pressure — your peers in the AI channel",
        "intro": (
            "Your products may still sell well through retailers today, "
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
                # The "1 of 6 cited; 5 went to..." case — phrase as
                # success/loss split so the merchant doesn't read
                # "1 cited" + "cited instead" as contradictory.
                base = (
                    f"{your_url_label} was cited in {cited} of "
                    f"{runs_total} buyer-intent queries"
                )
                if losing > 0 and retailers_phrase:
                    base += (
                        f". The other {losing} went to: {retailers_phrase}"
                    )
                elif losing > 0:
                    base += f". The other {losing} cited no merchant URL"
            else:
                # 0/N case — no contradictory phrasing needed.
                base = (
                    f"None of {runs_total} buyer-intent queries cited "
                    f"{your_url_label.lower()}"
                )
                if retailers_phrase:
                    base += f". They cited: {retailers_phrase} instead"
            base += (
                ". Grounded LLM citations are downstream of Google's "
                "index, so the typical root cause is that Google hasn't "
                "indexed your canonical PDPs yet — the AI agents have "
                "nothing to cite."
            )
            return base
        return (
            "AI agents return zero grounded references to your store "
            "across the queries we tested. Typical root cause: Google "
            "hasn't indexed your canonical PDPs, so grounded LLMs have "
            "nothing to cite."
        )

    if label == VERDICT_MISATTRIBUTED:
        if has_evidence:
            losing = max(0, (runs_total or 0) - (cited or 0))
            base = (
                f"AI agents recognize your product (visibility "
                f"{visibility_score}/100). {your_url_label} was cited "
                f"in {cited} of {runs_total} buyer-intent queries"
            )
            if losing > 0 and retailers_phrase:
                base += (
                    f"; the other {losing} went to {retailers_phrase}"
                )
            base += (
                ". The demand exists; resellers and marketplaces are "
                "capturing it instead of you."
            )
            if cp_framing:
                base += " " + cp_framing
            return base
        return (
            "AI agents recognize your product but consistently send "
            "consumers to third-party URLs instead of yours. The demand "
            "exists; competitors and resellers are capturing it."
        )

    if label == VERDICT_VIA_RETAILERS:
        if has_evidence:
            cs = cat_score if cat_score is not None else "?"
            gp = gap_pct if gap_pct is not None else "?"
            base = (
                f"Your brand surfaces in category-level AI queries "
                f"(category visibility {cs}/100), but your URL captures "
                f"only {attribution_score}/100 of buyer-intent queries — "
                f"a {gp}-point gap"
            )
            if retailers_phrase:
                base += f", routed through {retailers_phrase}"
            base += (
                ". The brand is findable in the AI channel; you're just "
                "not capturing the funnel."
            )
            if cp_framing:
                base += " " + cp_framing
            return base
        return (
            "AI agents recognize this brand in category-level queries — "
            "but the funnel mostly routes consumers through third-party "
            "retailers rather than the merchant's own URL. The brand is "
            "findable; the merchant isn't capturing it."
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
        base = (
            f"Mixed result — visibility {visibility_score}/100, "
            f"attribution {attribution_score}/100. Of {runs_total} "
            f"buyer-intent queries, {cited} cited your URL; the rest "
            f"routed elsewhere"
        )
        if failed_sample:
            sample = ", ".join(f'"{q[:50]}"' for q in failed_sample[:2])
            base += f". Failing queries include: {sample}"
        base += (
            ". The actions below show which gap is bigger; close that "
            "one first."
        )
        return base
    return (
        "Mixed result — the product gets surfaced sometimes, and gets "
        "attributed to the merchant's own URL sometimes, but neither is "
        "consistent. The action items below show which gap is bigger."
    )


def verdict_for(
    visibility_score: int,
    attribution_score: int,
    peer_thresholds: Optional[Dict[str, int]] = None,
    *,
    category_visibility_score: Optional[int] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Categorize the (visibility, attribution) pair into one of five
    verdicts and emit an evidence-bound diagnostic paragraph. Returns
    (label, explanation).

    `evidence` is a dict assembled by `build_structured_report` from
    already-extracted probe data:
      - attribution_runs_total: int
      - merchant_cited_runs: int
      - top_retailers: List[str]               (top hosts, len ≤ 3 used)
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

    label = _classify_verdict(
        visibility_score,
        attribution_score,
        category_visibility_score,
        invisible_max,
        strong_min,
        misattr_attr_max,
    )
    explanation = _explain_verdict(
        label, visibility_score, attribution_score, evidence or {}
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
    prior_runs: Optional[List[Dict[str, Any]]] = None,
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
    failed_query_sample: List[str] = []
    category_scores: List[int] = []
    framing: Optional[str] = None
    framing_score = -1
    for p in per_product:
        attr = p.get("attribution") or {}
        total_runs += int(attr.get("runs") or 0)
        total_cited += int(attr.get("merchant_cited_runs") or 0)

        # Walk both attribution.competitor_hosts (buyer-intent probe)
        # and category_visibility.retailer_hosts (category probe). The
        # union surfaces hosts that win across either probe type.
        for entry in attr.get("competitor_hosts") or []:
            host = entry.get("host")
            count = int(entry.get("times_cited") or 0)
            if host and count:
                retailer_count[host] += count
        cat = p.get("category_visibility") or {}
        if cat:
            cs = cat.get("score")
            if isinstance(cs, (int, float)):
                category_scores.append(int(cs))
            for entry in cat.get("retailer_hosts") or []:
                host = entry.get("host")
                count = int(entry.get("times_cited") or 0)
                if host and count:
                    retailer_count[host] += count

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

    return {
        "attribution_runs_total": total_runs,
        "merchant_cited_runs": total_cited,
        "top_retailers": [h for h, _ in retailer_count.most_common(5)],
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
        # Sleepwear / loungewear / intimates — same vertical for the
        # purpose of industry context (D2C apparel, retailer-mediated
        # discovery), so they share the fashion blurb until BD validates
        # a sleepwear-specific projection.
        "sleepwear", "pajama", "pyjama", "robe", "loungewear",
        "nightgown", "lingerie", "intimates", "underwear", "bralette",
        "swimwear", "swimsuit", "bikini",
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
    visibility_score: int = 0,
    attribution_score: int = 0,
    category_retailer_hosts: Optional[List[Dict[str, Any]]] = None,
    category_competitor_brands: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Return a list of 3-5 specific action items. Each item has
    `severity` (critical|high|medium|low), `title`, `body` (merchant-
    facing diagnostic prose, evidence-bound), and optional `evidence`
    (the failed query / cited competitor host that drives this action).

    Pitch-free: no "Pivota's agentic-commerce protocol", no "12% →
    25-30%" macros, no "complementary to existing retail distribution".
    Those live in `_build_what_pivota_changes` exclusively.
    """
    items: List[Dict[str, Any]] = []

    # Failures named in evidence text — shared with verdict_for via
    # module-level helpers so vocabulary stays consistent.
    failed_attribution_queries = _failed_attribution_queries(attribution_runs)
    failed_visibility_queries = _failed_visibility_queries(visibility_runs)
    top_competitor = competitor_hosts[0] if competitor_hosts else None
    top_retailer_names = [
        r["host"] for r in (category_retailer_hosts or [])[:3] if r.get("host")
    ]
    retailers_phrase = ", ".join(top_retailer_names)
    attribution_runs_total = len(attribution_runs)

    # Action 1: severity-stratified headline tied to this merchant's
    # specific failure pattern. All five tiers data-bind off the same
    # extracted variables so the language stays consistent.
    if verdict_label == VERDICT_INVISIBLE:
        body = (
            f"Across {attribution_runs_total} buyer-intent queries we "
            f"tested, AI agents returned zero grounded references to "
            f"your store"
        )
        if retailers_phrase:
            body += f". {retailers_phrase} captured the citation slots that should have been yours"
        body += (
            ". Grounded LLM citations are downstream of Google's index, "
            "so the typical root cause is that Google hasn't indexed "
            "your canonical PDPs yet. Submit your sitemap.xml to "
            "Search Console, request URL Inspection indexing for your "
            "top SKUs, and re-test in 72 hours."
        )
        items.append({
            "severity": "critical",
            "title": "Index your canonical PDPs with Google Search Console",
            "body": body,
            "evidence": {
                "queries_tested": attribution_runs_total,
                "top_retailers": top_retailer_names[:5],
            } if top_retailer_names else {"queries_tested": attribution_runs_total},
        })
    elif verdict_label == VERDICT_MISATTRIBUTED:
        if top_retailer_names:
            title = f"Reclaim attribution from {top_retailer_names[0]} and other resellers"
        else:
            title = "Reclaim direct attribution from third-party retailers"
        body = (
            f"AI agents recognize your product (visibility "
            f"{visibility_score}/100) but your URL appears in "
            f"{merchant_cited_runs} of {attribution_runs_total} buyer-"
            f"intent queries"
        )
        if retailers_phrase:
            body += f". The remaining {attribution_runs_total - merchant_cited_runs} route through {retailers_phrase}"
        body += (
            ". Every cited URL that's not yours is lost organic "
            "traffic — and a margin hit if the cited path is a "
            "reseller. The demand exists; it's just being captured by "
            "competitors."
        )
        items.append({
            "severity": "critical",
            "title": title,
            "body": body,
            "evidence": {
                "merchant_cited_runs": merchant_cited_runs,
                "queries_tested": attribution_runs_total,
                "top_retailers": top_retailer_names[:5],
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
            "severity": "critical",
            "title": "Capture the AI-channel funnel that retailers are taking today",
            "body": opener,
            "evidence": (
                {"top_retailer_hosts": [r["host"] for r in category_retailer_hosts[:5] if r.get("host")]}
                if category_retailer_hosts
                else None
            ),
        })
    elif verdict_label == VERDICT_STRONG:
        items.append({
            "severity": "low",
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
        body = (
            f"Visibility {visibility_score}/100, attribution "
            f"{attribution_score}/100. Of {attribution_runs_total} "
            f"buyer-intent queries, {merchant_cited_runs} cited your "
            f"URL; the rest routed elsewhere"
        )
        if retailers_phrase:
            body += f" (top: {retailers_phrase})"
        body += (
            ". The specific failing queries below are where the gaps "
            "are — close those first."
        )
        items.append({
            "severity": "high",
            "title": "Close the gap on inconsistent queries",
            "body": body,
            "evidence": {
                "merchant_cited_runs": merchant_cited_runs,
                "queries_tested": attribution_runs_total,
                "top_retailers": top_retailer_names[:5],
            },
        })

    # Action 2: top competitor capture, named with frequency.
    if top_competitor and top_competitor.get("times_cited", 0) >= 2:
        items.append({
            "severity": "high",
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
            "severity": "critical",
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
            "severity": "medium",
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
            "severity": "medium",
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


# Reference to the internal Shopify test merchant used as the live
# validation playground for the onboarding sequence's order-side steps
# (Outcome B: order-home). The discovery-side reference (Outcome A:
# discovery-lift) lives in `pivota-pdp-baseline.md`, produced by
# scripts/agent_center_pivota_pdp_baseline.py — that's the canonical
# AI-channel surface, NOT the test merchant's Shopify dev URL.
TEST_MERCHANT_REFERENCE: Dict[str, str] = {
    "merchant_id": "merch_38fa56d5118b9974",
    "shop_domain": "shop.myshopify.com",
    # Discovery-side artifact: probes Pivota canonical sig_* PDPs,
    # NOT the test merchant's Shopify URL (which is unindexed by
    # design — Shopify dev domain has no public retrieval surface).
    "discovery_baseline_path": "reports/pivota-pdp-baseline.md",
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

    Two parts the merchant needs to believe before signing:

      1. **discovery_lift** — Why your AI-channel visibility will improve.
         Anchored on Pivota's 6 canonical seed PDPs (median visibility
         67/100, attribution 50/100) as the empirical "after" reference,
         plus the four mechanics that produce that surface (canonical
         AI-channel PDP / Schema.org structured data / sitemap submission
         / semantic categorization). The claim is comparative, not paired
         A/B — clearly disclosed in `methodology_note`.

      2. **checkout_loop** — How in-chat checkout closes the loop. The
         end-to-end 6-step chain from grounded Gemini citation to
         merchant Shopify admin, each step tagged shipped/roadmap with
         the verifying file or test reference. Anchors on Shopify-only
         today (verified e2e with test merchant); other platforms
         (Wix / Woo / PrestaShop) have adapters but the order-completion
         dispatch isn't wired yet — disclosed honestly in
         `platform_coverage`."""
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

    discovery_lift = {
        "title": "Why your AI-channel discoverability will improve (multi-layer)",
        "current_state": (
            f"{cat_phrase}; {merchant_cited_runs}/{attribution_runs} "
            f"buyer-intent queries reach your URL today (this audit "
            f"measures Layer 1: grounded LLM citation). Retailer pages "
            f"({retailer_phrase}) currently capture the rest of the "
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
                    f"**Indexing-up phase.** "
                    f"{PIVOTA_PDP_BASELINE_REFERENCE['cited_count']}/"
                    f"{PIVOTA_PDP_BASELINE_REFERENCE['succeeded_count']} "
                    f"Pivota canonical PDPs cited in Gemini grounding "
                    f"as of "
                    f"{PIVOTA_PDP_BASELINE_REFERENCE['as_of_date']} — "
                    f"30-90 day arc post-publication, working through "
                    f"Search Console URL Inspection. See "
                    f"`reports/pivota-pdp-baseline.md` for live "
                    f"operational health."
                ),
                "merchant_metric": "attribution_score",
                "mechanics": [
                    {
                        "label": "Canonical AI-channel PDP per SKU",
                        "evidence": "agent.pivota.cc/products/sig_* (sitemap-seeds.ts)",
                        "shipped": True,
                    },
                    {
                        "label": "Schema.org Product + Offer + BreadcrumbList structured data",
                        "evidence": "pivota-agent-ui/src/app/products/[id]/productJsonLd.ts",
                        "shipped": True,
                    },
                    {
                        "label": "Sitemap submission + URL-Inspection indexing for grounded retrieval",
                        "evidence": "pivota-agent-ui/src/app/sitemap.xml + sitemap-products.xml",
                        "shipped": True,
                    },
                    {
                        "label": "Semantic categorization via canonical title patterns + breadcrumbs",
                        "evidence": "category-aware metadata + JSON-LD breadcrumbs",
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
                        "evidence": "pivota-acp/pivota_infra_main/routes/order_routes.py + tests/test_acp_shopify_order_forwarding.py",
                        "shipped": True,
                    },
                    {
                        "label": "UCP /ucp/v1/checkout-sessions — agent-callable checkout",
                        "evidence": "pivota-agent-ui/src/app/api/ucp/checkout-sessions/route.ts",
                        "shipped": True,
                    },
                    {
                        "label": "agent_shop_gateway /agent/shop/v1/invoke — unified agent operation surface",
                        "evidence": "PIVOTA-Agent/routes/agent_shop_gateway.py",
                        "shipped": True,
                    },
                    {
                        "label": "Canonical PDP URLs as stable agent-resolvable identifiers",
                        "evidence": "agent.pivota.cc/products/sig_* — passed to checkout intents by agents",
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
            "Pivota baseline (probe modes: `"
            + "` + `".join(PIVOTA_PDP_BASELINE_REFERENCE["probe_modes_in_baseline"])
            + "`) is comparable only to attribution_score. Layer 2's "
            "agent-direct surface has no per-merchant probe — it's "
            "binary by ACP/UCP integration (onboarded merchants are "
            "agent-queryable; non-onboarded are not). Pivota PDPs are "
            f"currently in the "
            f"`{PIVOTA_PDP_BASELINE_REFERENCE['indexing_phase']}` "
            "phase for Layer 1; Layer 2 is shipped today. Refresh via "
            "scripts/agent_center_pivota_pdp_baseline.py."
        ),
    }

    checkout_loop = {
        "title": "How in-chat checkout closes the loop",
        "chain": [
            {
                "step": 1,
                "label": "AI agent (Gemini / ChatGPT / shopping agent) cites the Pivota canonical PDP in a grounded answer",
                "evidence": "AI Commerce Readiness audit (this report) + agent.pivota.cc/products/sig_*",
                "shipped": True,
            },
            {
                "step": 2,
                "label": "Consumer (or their AI agent) triggers buy intent on the PDP",
                "evidence": "pivota-agent-ui/src/app/products/[id]/ProductDetailClient.tsx handleCheckout()",
                "shipped": True,
            },
            {
                "step": 3,
                "label": "UCP (Universal Commerce Protocol) checkout session opens in-chat",
                "evidence": "pivota-agent-ui/src/app/api/ucp/checkout-sessions/route.ts",
                "shipped": True,
            },
            {
                "step": 4,
                "label": "ACP (Agent Commerce Protocol) creates the order + processes payment",
                "evidence": "pivota_infra_main/routes/order_routes.py POST /orders/create",
                "shipped": True,
            },
            {
                "step": 5,
                "label": "Order forwarded to merchant Shopify admin async (background task)",
                "evidence": "pivota_infra_main/routes/order_routes.py create_shopify_order() → Shopify admin /2024-01/orders.json",
                "shipped": True,
            },
            {
                "step": 6,
                "label": "Merchant sees the order in their Shopify admin with first-party customer data (email, address, line items, attribution metadata)",
                "evidence": "Verified e2e with test merchant merch_38fa56d5118b9974 + tests/test_shopify_order_sync_hardening.py",
                "shipped": True,
            },
        ],
        "platform_coverage": {
            "shipped": ["Shopify"],
            "roadmap": ["WooCommerce", "Wix", "PrestaShop"],
            "note": (
                "Shopify path is wired end-to-end and verified with a test "
                "merchant. Adapters for Woo / Wix / PrestaShop exist in the "
                "codebase but the order-completion dispatch is hardcoded to "
                "Shopify today; multi-platform wiring is on the Q3 roadmap."
            ),
        },
        "outcome": (
            "Orders land in the merchant's Shopify admin within seconds of "
            "in-chat completion. Customer email, shipping address, line "
            "items, and source-attribution metadata "
            "(`source = pivota_acp`, `agent = gemini`) are first-party data "
            "the merchant owns — Pivota does not intermediate the customer "
            "relationship."
        ),
    }

    onboarding_sequence = {
        "title": "Onboarding sequence — validated end-to-end on the Pivota test merchant",
        "intro": (
            "Each step below is operated either as a Pivota agent or as a "
            "shipped pipeline (Shopify OAuth + ACP). Every step cites a "
            "concrete artifact from running the same sequence on Pivota's "
            "internal Shopify test merchant — verifiable evidence, not "
            "concepts. Steps marked `manual_today` work end-to-end but "
            "don't yet have a one-click agent runner; operations runs "
            "them on the merchant's behalf during onboarding."
        ),
        "test_merchant": {
            "merchant_id": TEST_MERCHANT_REFERENCE["merchant_id"],
            "shop_domain": TEST_MERCHANT_REFERENCE["shop_domain"],
            # Discovery side (Outcome A) anchored to canonical Pivota
            # PDPs; order side (Outcome B) anchored to test merchant
            # e2e tests. The test merchant proves order-home, not
            # discovery-lift.
            "discovery_baseline_path": TEST_MERCHANT_REFERENCE["discovery_baseline_path"],
        },
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
                    "Same engine runs the Pivota PDP self-baseline "
                    "(canonical sig_* URLs) monthly; latest output at "
                    f"`{TEST_MERCHANT_REFERENCE['discovery_baseline_path']}` "
                    "(produced by "
                    "`scripts/agent_center_pivota_pdp_baseline.py`). "
                    "Discovery side: the AI-channel surface Pivota "
                    "publishes for merchant SKUs."
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
                    "needs to forward orders. Stored in `merchant_stores"
                    "` table; retrieved at order-completion time via "
                    "`services/merchant_store_service.py:"
                    "get_primary_store`."
                ),
                "addresses": (
                    "Wires the order-forwarding path described in the "
                    "Checkout Loop section above (step 5 of the chain)."
                ),
                "test_merchant_validation": (
                    f"Test merchant `{TEST_MERCHANT_REFERENCE['merchant_id']}` "
                    "is fully onboarded via this pipeline — Shopify "
                    "OAuth completed, access_token stored, primary_store "
                    "row exists. The same row is what "
                    "`create_shopify_order()` reads at order time."
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
                    "Verified by the e2e shell test "
                    "`pivota-acp/test_epic5_shopify_order_poc.sh` "
                    "against test merchant on each release. Hardening "
                    "tests "
                    "`pivota-backend/tests/"
                    "test_shopify_order_sync_hardening.py` lock the "
                    "401-fallback + retry behavior. Same script will "
                    "run on the prospective merchant during onboarding "
                    "QA."
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
                    "roadmap (currently a RESERVED scan_mode "
                    "placeholder, not a shipped runner)."
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
            "Three Pivota agents (Offer Execution, Checkout "
            "Verification, GMV Attribution) exist as RESERVED scan_mode "
            "placeholders today and appear in our roadmap, not as "
            "shipped agents. Steps 4 and 5 above deliver their function "
            "manually until the agents ship — and the test merchant "
            "artifacts above show the underlying pipelines work today."
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
                "evidence": "pivota-agent-ui/src/app/products/[id]/productJsonLd.ts",
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
                "evidence": "agent.pivota.cc/products/sig_* (sitemap-seeds.ts)",
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
                "evidence": "scripts/agent_center_bd_external_merchant.py + monthly cron",
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
                "evidence": "services/agent_center_sku_match_service.py",
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
) -> Optional[Dict[str, Any]]:
    """PR-C: distill the merchant's last few audit runs into a trend
    summary the merchant_view.tracking block can render. None when
    no prior runs (first-ever audit on this merchant).

    Each prior_run entry comes from `db.merchant_audit_runs.recent_runs_for_merchant`.
    We use the most-recent succeeded run's scores as the comparison
    baseline — delta from THIS audit shows the merchant whether they're
    moving up, flat, or down since last time.
    """
    succeeded = [
        r for r in (prior_runs or [])
        if r.get("status") == "succeeded"
        and r.get("visibility_score_avg") is not None
    ]
    if not succeeded:
        return None
    most_recent = succeeded[0]
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


def _build_visibility_plain_summary(
    *,
    verdict_label: str,
    visibility_score: int,
    attribution_score: int,
    category_visibility_score: Optional[int],
    attribution_runs_total: int,
    merchant_cited_runs: int,
    top_retailers: List[str],
) -> str:
    """Merchant-friendly translation of the score combination.

    Merchants see two scores (`visibility` and `attribution`) plus a
    technical verdict explanation and ask the obvious question:
    "Am I visible to AI users or not?". This helper answers that
    question directly per tier, in one short paragraph, without
    re-stating the scores in math notation.

    Distinct from `verdict.explanation` (the per-tier diagnostic
    paragraph) — that lands the technical claim with named retailers
    + numbers; this one is the merchant-comprehension layer above it.
    """
    retailers_phrase = ", ".join(top_retailers[:3]) if top_retailers else "third-party hosts"

    if verdict_label == VERDICT_INVISIBLE:
        return (
            "No — AI agents don't surface your brand or your products "
            "today. The likely root cause is that your canonical PDPs "
            "aren't indexed by Google yet, so grounded LLMs have "
            "nothing to cite."
        )

    if verdict_label == VERDICT_VIA_RETAILERS:
        cs = category_visibility_score or 0
        gap_pct = max(0, cs - attribution_score)
        return (
            f"Yes and no. AI agents DO recognize your brand at the "
            f"category level — when consumers ask 'best X', your "
            f"brand is mentioned. But when they ask where to actually "
            f"buy your products, AI cites editorial / retailer pages "
            f"({retailers_phrase}) instead of your URL {gap_pct}% of "
            f"the time. You have brand recognition; you don't yet "
            f"have first-party traffic."
        )

    if verdict_label == VERDICT_MISATTRIBUTED:
        return (
            f"Partly. AI agents recognize your product, but when "
            f"buyers ask where to find it, your URL appears in "
            f"{merchant_cited_runs} of {attribution_runs_total} "
            f"buyer-intent queries — the rest route to "
            f"{retailers_phrase}. The product is recognized; the "
            f"buying funnel isn't yours."
        )

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


def _build_merchant_view(
    *,
    verdict_label: str,
    verdict_explanation: str,
    visibility_score: int,
    attribution_score: int,
    category_visibility_score: Optional[int],
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
    prior_runs: Optional[List[Dict[str, Any]]] = None,
    pivota_signature_minted_at: Optional[datetime] = None,
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
    cited_hosts_detailed_full = classify_cited_hosts(
        category_retailer_hosts or [],
        merchant_category=merchant_category,
    )

    # Phase C-4 PR-G: per-cited-host playbook actions. Strategic
    # actions from `_generate_action_items` (verdict-tier-based) lead;
    # per-host playbook actions come after, sorted by severity. Each
    # playbook action carries `playbook_step_id + target_host + lever
    # + expected_timeline_weeks` so the frontend can group/filter.
    # Phase A: also passes merchant_name + merchant_category so the
    # playbook engine can render `pitch_draft` (pre-filled email)
    # per editorial action.
    playbook_actions = select_playbooks(
        cited_hosts_detailed=cited_hosts_detailed_full,
        failed_queries_detailed=failed_queries_detailed,
        merchant_name=merchant_brand,  # use the friendly brand name
        merchant_category=merchant_category,
    )
    merged_actions = list(action_items or []) + list(playbook_actions or [])
    # Stamp a 1-indexed `priority_order` on every action so the
    # frontend can render "Step 1, Step 2..." without re-deriving the
    # ordering. Strategic actions from `_generate_action_items` come
    # first (lower numbers), per-host playbook actions follow.
    for i, a in enumerate(merged_actions, start=1):
        a["priority_order"] = i

    plain_summary = _build_visibility_plain_summary(
        verdict_label=verdict_label,
        visibility_score=visibility_score,
        attribution_score=attribution_score,
        category_visibility_score=category_visibility_score,
        attribution_runs_total=len(attribution_runs or []),
        merchant_cited_runs=merchant_cited_runs,
        top_retailers=[
            h.get("host")
            for h in (category_retailer_hosts or [])[:3]
            if h.get("host")
        ],
    )
    competitive_table = _build_competitive_table(competitive_pressure or {})

    return {
        "headline": {
            "verdict_label": verdict_label,
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
                for h in (category_retailer_hosts or [])[:5]
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
        "tracking": {
            # PR-C populates these from merchant_audit_runs history.
            "next_audit_eligible_at": None,
            "history_link": "/api/merchant-center/audit/history",
            "history": _build_history_trend(prior_runs),
            "pivota_baseline_reference": {
                "visibility": pivota_baseline.get("median_visibility"),
                "attribution": pivota_baseline.get("median_attribution"),
                "as_of": pivota_baseline.get("as_of_date"),
                "indexing_phase": pivota_baseline.get("indexing_phase"),
            },
            "your_gap_to_baseline": your_gap_to_baseline,
        },
        "pivota_value_prop": what_pivota_changes,
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
    url_source: Optional[str] = None,
    prior_runs: Optional[List[Dict[str, Any]]] = None,
    pivota_signature_minted_at: Optional[datetime] = None,
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
    top_retailer_hosts = [
        r["host"]
        for r in (category_retailer_hosts or [])[:5]
        if r.get("host")
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
        "competitive_pressure_framing": (competitive_pressure or {}).get("framing"),
        "category_score": category_score,
        "gap_pct": gap_pct,
        "failed_attribution_query_sample": _failed_attribution_queries(attribution_runs)[:3],
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
    action_items = _generate_action_items(
        verdict_label=verdict_label,
        visibility_runs=visibility_runs,
        attribution_runs=attribution_runs,
        competitor_hosts=competitor_hosts_list,
        merchant_cited_runs=merchant_cited_runs,
        runs_with_any_citation=runs_with_any_citation,
        visibility_score=visibility_score,
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

    visibility_query_rows = _per_query_rows(visibility_runs, "product_visible")
    attribution_query_rows = _per_query_rows(attribution_runs, "merchant_url_found")

    merchant_view = _build_merchant_view(
        verdict_label=verdict_label,
        verdict_explanation=verdict_explanation,
        visibility_score=visibility_score,
        attribution_score=attribution_score,
        category_visibility_score=category_score,
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
        prior_runs=prior_runs,
        pivota_signature_minted_at=pivota_signature_minted_at,
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
        "competitive_pressure": competitive_pressure,
        "what_pivota_changes": what_pivota_changes,
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
                shipped_list = ", ".join(pc.get("shipped") or [])
                roadmap_list = ", ".join(pc.get("roadmap") or [])
                sections.append(
                    f"**Platform coverage.** "
                    f"Today: {shipped_list or '(none)'}. "
                    f"Roadmap: {roadmap_list or '(none)'}.\n"
                )
                if pc.get("note"):
                    sections.append(f"_{pc['note']}_\n")
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
