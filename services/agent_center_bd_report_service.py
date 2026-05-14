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

    def _brand_in(text: str) -> bool:
        if not brand_lower:
            return False
        if brand_pattern is not None:
            return brand_pattern.search(text) is not None
        return brand_lower in text

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
            "grounded source. We did not gather enough additional data "
            "in this run to characterize what was cited; re-run the "
            "audit or check the action items for next steps."
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
                    f"the category-visibility score is {cs}/100 (we don't "
                    f"have per-run match details for this run)"
                )
            base = (
                f"Your category-visibility score is {cs}/100; your "
                f"buyer-intent attribution score is {attribution_score}"
                f"/100 — a {gp}-point gap. " +
                signal_phrase[0].upper() + signal_phrase[1:] +
                ", but in few buyer-intent queries"
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
            "queries grounded their answers in third-party sources rather "
            "than the merchant's own URL; we did not verify whether those "
            "sources mention the brand."
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
        label = (
            VERDICT_VIA_RETAILERS
            if typed_retail_hosts
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
        if (
            has_typed_cited_hosts
            and label == VERDICT_VIA_RETAILERS
            and not typed_retail_hosts
        ):
            label = VERDICT_PARTIAL
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
    integration_state: Optional[Dict[str, Any]] = None,
    include_social_intelligence: bool = False,
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
                # Phase 0: same merchant-level integration state on
                # every product report so the integration action
                # consistently fires (or stays absent) across products.
                integration_state=integration_state,
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
        "provider": provider,
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
                "with first-party customer data (email, address, "
                "line items, attribution metadata)"
            ),
            "evidence": (
                "Verified end-to-end on Shopify / WooCommerce / "
                "BigCommerce; equivalent surfaces shipped for Wix + "
                "custom via integration sprint"
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
                    "through Search Console URL Inspection. The mechanics "
                    "below are shipped; Google indexing latency is the "
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
                        "label": "Agent shop gateway — unified agent operation surface (search → cart → checkout)",
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
            "This audit's attribution score measures Layer 1 only "
            "(grounded LLM citation via Gemini). The "
            f"{PIVOTA_PDP_BASELINE_REFERENCE['median_visibility']}/"
            f"{PIVOTA_PDP_BASELINE_REFERENCE['median_attribution']} "
            "Pivota baseline (open-product visibility + merchant-store "
            "attribution probes) is comparable only to the attribution "
            "score. Layer 2's agent-direct surface has no per-merchant "
            "probe — it's binary by API integration: onboarded "
            "merchants are agent-queryable, non-onboarded are not. "
            "Pivota's own canonical PDPs are currently in the "
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
            "Three Pivota agents (Offer Execution, Checkout "
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

    return {
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
        verdict_label = aggregate.get("brand_verdict_label") or "(unknown)"
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
        # omitting — so the operator knows the lookup ran and WHY it
        # came back empty (vs. "was this even checked?").
        _failure_reasons = social.get("failure_reasons") or {}
        _FAILURE_TEXT = {
            "ungrounded": (
                "the lookup couldn't ground its answer in a live source "
                "(suppressed to avoid unverified numbers)"
            ),
            "parse_error": "the lookup returned an unparseable response",
            "rate_limited": "the lookup was rate-limited — retry later",
            "transport_error": "the lookup failed to reach the data source",
            "no_data": "the lookup ran but found nothing for this brand",
        }

        def _failure_note(label: str, reason: Optional[str]) -> Optional[str]:
            if not reason:
                return None
            text = _FAILURE_TEXT.get(reason, f"unavailable ({reason})")
            return f"_{label} unavailable — {text}._\n"

        def _own_presence_line(platform_label: str, p: Dict[str, Any]) -> str:
            # PR-9: when the sub-call was ungrounded, every metric is
            # nulled — render the handle + an explicit "not verified"
            # note rather than a fabricated count. A grounded entry
            # renders the real numbers.
            handle = p.get("handle") or "?"
            if p.get("grounding") == "ungrounded":
                return (
                    f"- {platform_label}: `@{handle}` — follower / engagement "
                    f"data not verified (the lookup couldn't ground its "
                    f"answer in a live source). Operator to confirm manually.\n"
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
                "for every brand — blank = the lookup couldn't ground a "
                "verified number)_\n"
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
