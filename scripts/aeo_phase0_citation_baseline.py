#!/usr/bin/env python3
"""
AEO Phase 0 — inward-pointed citation baseline (the DEMAND-side metric).

Everything the visibility epic shipped through 2026-07-25 is SUPPLY side:
crawlable PDPs, Product JSON-LD, ISR caching, a 3.3k-URL sitemap, robots
explicitly allowing GPTBot/ClaudeBot. This script measures the only thing
that actually matters to the thesis: **when a US shopper asks an AI agent a
real K-beauty shopping question, does agent.pivota.cc get cited — and if
not, who does?**

It does NOT build a new prober. It points the existing client-facing
citation machinery (`/internal/agent-center/llm-probe` in PIVOTA-Agent, the
same endpoint the merchant audit + BD report lanes use) at Pivota's own
catalog. Sibling script: `agent_center_pivota_pdp_baseline.py` (6 hardcoded
seeds, BD-pitch framing). This one differs in three ways that matter:

1. **Intent tiers.** The portfolio is stratified the way the client-facing
   Visibility Momentum methodology stratifies a merchant portfolio:
   category/discovery, brand, sku, ingredient/concern, comparison. A single
   blended "citation rate" hides the only actionable signal — WHICH intents
   we could plausibly win. Head-term rows are recorded as BAROMETERS, never
   as targets (see METHODOLOGY note below).

2. **`search_grounded_product_discovery_test`, not
   `pivota_pdp_attribution_test`.** The 2026-07-23 and 2026-07-25 Phase 0
   runs used the attribution mode and got 0 grounding sources on EVERY
   query, which was read as "no first-party PDP is being surfaced for these
   queries at all". Under this mode the same portfolio returns grounding
   sources on 44 of 50 units, so that reading was an artifact of the mode.

   ⚠️ BE CAREFUL ABOUT THE MECHANISM. The search tool is attached
   UNCONDITIONALLY for every scan mode (`tools: [{googleSearch: {}}]` for
   Gemini; `web_search_preview` + `tool_choice:'auto'` for OpenAI), so the
   mode does NOT gate grounding — invoking the tool is the model's
   election, and the attribution mode's prompt (a yes/no about whether a
   URL is "mentioned") gives it no reason to search. The honest claim is
   "this mode elicits grounding, that one did not", NOT "that mode cannot
   ground". Note also that this mode's prompt ("list the URLs you would
   cite as buying paths") is the WEAKEST search instruction of the grounded
   modes; `open_product_visibility_test` actually says "Search the web for
   this query". If a future run needs a stronger elicitation, try that mode
   and A/B it against this one on the same portfolio.

3. **Domain-level citation, computed here — not the probe's score.**
   `groundingContainsUrl()` in the probe counts a hit when a grounding
   chunk TITLE merely CONTAINS `merchantBrand`. That is correct for the
   merchant lane (the merchant's own store being cited is the thing
   measured) but is a false-positive generator when pointed inward: passing
   vendor="COSRX" would score a cosrx.com or Ulta citation as a Pivota
   citation.

   Passing vendor="Pivota" does NOT remove that false positive, it only
   moves it — a chunk titled "Pivotal role of niacinamide | healthline.com"
   still satisfies the probe's substring test. So the probe's
   `url_match.in_grounding` is recorded as a DIAGNOSTIC ONLY
   (`probe_url_match_in_grounding`) and is never the reported metric, and
   it is deliberately not labelled "exact PDP": for Gemini the probe's
   path-comparison branch is unreachable (every chunk is a redirector, and
   the direct-match branch skips redirectors), so that field is a
   brand-substring signal for Gemini and a real path match for OpenAI —
   one field with two meanings.

   The headline `pivota_cited` is recomputed here from the raw
   `grounding_sources` against our own hostnames. When a source has a real
   (non-redirector) host, that host decides alone — so a page ABOUT Pivota
   on a third-party domain (e.g. a trustpilot.com review of pivota.cc) is
   correctly NOT a Pivota citation.

WHO-IS-CITED-INSTEAD: Gemini returns opaque `vertexaisearch...redirect`
URIs, so the URI is useless for attribution — the chunk `title` is the only
signal. Empirically (2026-07-25) those titles came back as bare domains
("target.com", "iherb.com"), but the prober's own docs describe them as
human-readable names ("Sephora", "Olive Young Global"). BOTH shapes occur;
a human-readable name is not resolvable to a domain, so it is counted as
`unattributable_source_count` and surfaced rather than silently dropped.
OpenAI returns real cited URLs, so we take the host.

⚠️ The two providers' `grounding_sources` are NOT the same population:
OpenAI's are answer-level `url_citation` annotations (true citations, the
retrieved pool deliberately excluded), while Gemini's are `groundingChunks`
(retrieved FOR grounding, not necessarily reflected in the answer). Gemini's
number is therefore the more generous of the two, which makes a Gemini
zero-citation result the stronger claim. Do not read cross-provider deltas
as behavioural differences.

Providers: gemini (Vertex) + chatgpt are configured on prod PIVOTA-Agent.
There is no ANTHROPIC_API_KEY on the service, so the `claude` lane cannot
run; it is reported as unmeasured rather than silently scored 0.

Cost: 1 real grounded LLM call per (prompt x provider). The default
portfolio is 25 prompts x 2 providers = 50 calls of production COGS.
Read-only: no DB writes, no feature flags, no merchant credit metering.

Usage:

  cd /Users/pengchydan/dev/PIVOTA-Agent
  export PROMOTIONS_ADMIN_KEY="$(railway variables --kv \
      | grep -m1 '^PROMOTIONS_ADMIN_KEY=' | cut -d= -f2-)"
  python3 /path/to/pivota-backend/scripts/aeo_phase0_citation_baseline.py \
      --output ~/dev/AEO_PHASE0_BASELINE_$(date +%F).md \
      --json-output /tmp/aeo_phase0_$(date +%F).json

  # cheap re-check of one tier / one provider:
  ... --providers gemini --tiers sku

Re-run cadence: crawlers must re-fetch and re-index before citations can
move, so re-run on a 1-2 WEEK cadence after an intervention, not daily.
Keep the portfolio frozen across runs — the whole value is comparability.

Exit codes:
  0 — baseline captured (a 0% citation rate is a valid result, not failure)
  1 — every probe request failed (network/upstream/auth) => no baseline
  2 — invalid arguments / missing config
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple  # Tuple: batch grouping key

DEFAULT_AGENT_URL = "https://pivota-agent-production.up.railway.app"
PROBE_PATH = "/internal/agent-center/llm-probe"

# `HARD_MAX_RUNS` in src/internal/agentCenterLlmProbe.js. Requests asking for
# more are silently truncated by the service, which would drop prompts from
# the portfolio without warning, so we never exceed it.
PROBE_HARD_MAX_RUNS = 8

# The probe runs a request's queries SEQUENTIALLY at ~25s per grounded call
# (DEFAULT_GEMINI_TIMEOUT_MS), so a big batch blows the edge proxy's response
# timeout and the connection is torn down mid-request (RemoteDisconnected)
# -- losing every query in the batch. Measured 2026-07-25: 3 queries/request
# even 2 failed intermittently, so the default is 1. Raising this trades
# wall-clock for lost batches, and a lost batch is a hole in the baseline.
DEFAULT_BATCH_SIZE = 1

# The hosts that count as "Pivota was cited". Kept explicit (not a substring
# check on "pivota") so that e.g. a blog post ABOUT Pivota on someone else's
# domain is not miscounted as our surface being cited.
PIVOTA_HOSTS = ("agent.pivota.cc", "www.pivota.cc", "pivota.cc")

# Mirror of `_VERTEX_REDIRECTOR_HOSTS` in agentCenterLlmProbe.js. Both hosts
# are real; a chunk on either carries no attributable publisher in its URI.
_VERTEX_REDIRECTOR_HOSTS = frozenset({
    "vertexaisearch.cloud.google.com",
    "vertex-ai-search.cloud.google.com",
})

# A provider that reports itself as `mock*` means the service had no API key
# and silently fell back (HTTP 200, empty raw_runs). Treating that as a
# measured 0% would be a fabricated finding.
MOCK_PROVIDER_PREFIX = "mock"

# Scoring subject. MUST NOT be a catalog brand: the probe's own
# `groundingContainsUrl` matches merchantBrand against grounding-chunk
# titles, so vendor="COSRX" would turn every cosrx.com/Ulta citation into a
# false "Pivota cited". See module docstring.
SCORING_VENDOR = "Pivota"

PROBE_SCAN_MODE = "search_grounded_product_discovery_test"

# ---------------------------------------------------------------------------
# ANCHORS — real rows on the live public serving surface, 2026-07-25.
#
# Every sig below was verified present in https://agent.pivota.cc/sitemap-products.xml
# AND trust serving_decision='public'. If an anchor is retired from the
# catalog, REPLACE it and note the swap in the artifact; do not silently drop
# it, or the tier denominators stop being comparable run-to-run.
# ---------------------------------------------------------------------------
ANCHORS: Dict[str, Dict[str, str]] = {
    "cosrx_snail96": {
        "sig": "sig_53d2fd31c24bd8a7a65525df466d5aef",
        "title": "COSRX Advanced Snail 96 Mucin Power Essence",
        "brand": "COSRX",
        "store_id": "cosrx",
        "product_type": "face essence",
    },
    "boj_relief_sun": {
        "sig": "sig_33941f213d6907294375b4cdf946615a",
        "title": "Beauty of Joseon Relief Sun : Rice + Probiotics (SPF50+ PA++++)",
        "brand": "Beauty of Joseon",
        "store_id": "beautyofjoseon",
        "product_type": "facial sunscreen",
    },
    "mixsoon_bean": {
        "sig": "sig_e44daa7308c832205f3295c8ace073fb",
        "title": "mixsoon Bean Essence 50ml",
        "brand": "Mixsoon",
        "store_id": "mixsoon",
        "product_type": "fermented essence",
    },
    "anua_heartleaf_toner": {
        "sig": "sig_605fa9a7413dd67552199962925912a5",
        "title": "Anua Heartleaf 77 Soothing Toner",
        "brand": "Anua",
        "store_id": "anua",
        "product_type": "facial toner",
    },
    "manyo_cleansing_oil": {
        "sig": "sig_8adc83db60e5bdda8ea1311afe39a938",
        "title": "Ma:nyo Pure Cleansing Oil Deep Clean",
        "brand": "Ma:nyo",
        "store_id": "manyo",
        "product_type": "cleansing oil",
    },
    "roundlab_dokdo_toner": {
        "sig": "sig_032244cef2017fed5f080ce6beffab28",
        "title": "Round Lab 1025 Dokdo Toner",
        "brand": "Round Lab",
        "store_id": "roundlab",
        "product_type": "facial toner",
    },
    "anua_niacinamide_txa": {
        "sig": "sig_201824f62bfeb8300b93abf2322050c6",
        "title": "Anua Niacinamide 10 TXA 4 Serum for Brightening and Dark Spots",
        "brand": "Anua",
        "store_id": "anua",
        "product_type": "brightening serum",
    },
    "cosrx_snail92_cream": {
        "sig": "sig_453f3739fcea5cd01428bfa77be782c8",
        "title": "COSRX Advanced Snail 92 All in One Cream",
        "brand": "COSRX",
        "store_id": "cosrx",
        "product_type": "face cream",
    },
}

# ---------------------------------------------------------------------------
# THE PHASE 0 PROMPT PORTFOLIO — 25 prompts x 5 intent tiers.
#
# METHODOLOGY (from the client-facing Visibility Momentum template):
# `category` tier rows are HEAD/NEAR-HEAD terms. They are BAROMETERS —
# measured every run, never chased. Generic category heads belong to the
# incumbent authority graph (retail aggregators + editorial), and a
# mid/long-tail challenger contesting them is a documented anti-pattern.
# The tiers we can actually act on are `ingredient_concern` (the K-beauty
# wedge: constraint-dense long tail) and `comparison` (substitution).
#
# `anchor` = which PDP is the attribution target for that prompt. For the
# open tiers it is the closest real product we serve; a citation of ANY
# pivota host still counts (domain-level metric), so the anchor choice
# affects only the stricter exact-PDP number.
# ---------------------------------------------------------------------------
PORTFOLIO: List[Dict[str, str]] = [
    # --- TIER 1: category / discovery (head terms — barometers) ----------
    {"tier": "category", "anchor": "mixsoon_bean",
     "query": "best korean essence for glass skin 2026"},
    {"tier": "category", "anchor": "boj_relief_sun",
     "query": "best korean sunscreen for face no white cast"},
    {"tier": "category", "anchor": "manyo_cleansing_oil",
     "query": "best korean cleansing oil for blackheads"},
    {"tier": "category", "anchor": "anua_heartleaf_toner",
     "query": "best korean toner for sensitive skin"},
    {"tier": "category", "anchor": "cosrx_snail96",
     "query": "best k-beauty snail mucin products"},

    # --- TIER 2: brand-specific -----------------------------------------
    {"tier": "brand", "anchor": "mixsoon_bean",
     "query": "mixsoon bean essence where to buy united states"},
    {"tier": "brand", "anchor": "cosrx_snail96",
     "query": "cosrx snail mucin essence official site price"},
    {"tier": "brand", "anchor": "anua_heartleaf_toner",
     "query": "anua heartleaf toner buy online usa"},
    {"tier": "brand", "anchor": "roundlab_dokdo_toner",
     "query": "round lab 1025 dokdo toner us retailer"},
    {"tier": "brand", "anchor": "manyo_cleansing_oil",
     "query": "manyo pure cleansing oil where to buy"},

    # --- TIER 3: sku-specific (exact product names) ----------------------
    {"tier": "sku", "anchor": "cosrx_snail96",
     "query": "COSRX Advanced Snail 96 Mucin Power Essence"},
    {"tier": "sku", "anchor": "boj_relief_sun",
     "query": "Beauty of Joseon Relief Sun Rice + Probiotics SPF50+ PA++++"},
    {"tier": "sku", "anchor": "mixsoon_bean",
     "query": "mixsoon Bean Essence 50ml"},
    {"tier": "sku", "anchor": "anua_heartleaf_toner",
     "query": "Anua Heartleaf 77 Soothing Toner"},
    {"tier": "sku", "anchor": "roundlab_dokdo_toner",
     "query": "Round Lab 1025 Dokdo Toner"},

    # --- TIER 4: ingredient / concern-led (THE WEDGE) --------------------
    {"tier": "ingredient_concern", "anchor": "anua_niacinamide_txa",
     "query": "korean serum with niacinamide and tranexamic acid for dark spots"},
    {"tier": "ingredient_concern", "anchor": "anua_heartleaf_toner",
     "query": "houttuynia cordata heartleaf toner for facial redness and irritation"},
    {"tier": "ingredient_concern", "anchor": "mixsoon_bean",
     "query": "fermented soybean essence for skin barrier repair"},
    {"tier": "ingredient_concern", "anchor": "boj_relief_sun",
     "query": "spf 50 sunscreen with rice extract and probiotics for dry skin"},
    {"tier": "ingredient_concern", "anchor": "manyo_cleansing_oil",
     "query": "korean cleansing oil safe for fungal acne malassezia"},

    # --- TIER 5: comparison / substitution -------------------------------
    {"tier": "comparison", "anchor": "cosrx_snail96",
     "query": "cosrx snail mucin essence vs beauty of joseon glow deep serum"},
    {"tier": "comparison", "anchor": "mixsoon_bean",
     "query": "mixsoon bean essence vs sk-ii facial treatment essence"},
    {"tier": "comparison", "anchor": "anua_heartleaf_toner",
     "query": "anua heartleaf toner vs round lab 1025 dokdo toner"},
    {"tier": "comparison", "anchor": "boj_relief_sun",
     "query": "beauty of joseon relief sun vs skin1004 centella sunscreen"},
    {"tier": "comparison", "anchor": "cosrx_snail92_cream",
     "query": "best alternative to cosrx snail 92 all in one cream"},
]

TIER_ORDER = ["category", "brand", "sku", "ingredient_concern", "comparison"]

TIER_NOTES = {
    "category": "head/near-head terms — BAROMETER only, never chased",
    "brand": "branded intent — should be the easiest win if we are indexed at all",
    "sku": "exact product name — the strongest possible attribution test",
    "ingredient_concern": "the K-beauty wedge: constraint-dense long tail",
    "comparison": "substitution / decision intent — where ADR-002 dossiers should win",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _host_of(uri: str) -> str:
    try:
        return (urllib.parse.urlsplit(uri).hostname or "").lower()
    except ValueError:
        return ""


def _is_redirector(host: str) -> bool:
    """Mirrors `_VERTEX_REDIRECTOR_HOSTS` in agentCenterLlmProbe.js.

    There are TWO hosts, not one. Matching only "vertexaisearch" credits
    `vertex-ai-search.cloud.google.com` to Google as a publisher.
    """
    return host in _VERTEX_REDIRECTOR_HOSTS


def _is_pivota_host(host: str) -> bool:
    return bool(host) and any(host == h or host.endswith("." + h) for h in PIVOTA_HOSTS)


def _is_pivota_ref(source: Dict[str, Any]) -> bool:
    """True when a CITED grounding source points at a Pivota surface.

    Two source shapes:
      * OpenAI cites a REAL url -> decide on the host alone. If the host is
        real and is not ours, the answer is NO regardless of the title. This
        is what stops `trustpilot.com/review/pivota.cc` (title
        "pivota.cc Reviews ...") from counting as our surface being cited --
        a page ABOUT Pivota on someone else's domain is not a Pivota citation.
      * Gemini wraps the URI in an opaque redirector, so the host is
        unusable and `title` is the only signal. ONLY then do we fall back to
        the title.

    The title fallback needs a full hostname from PIVOTA_HOSTS (never the
    bare token "pivota"), so "Pivotal role of niacinamide" does not match.
    """
    host = _host_of(str(source.get("uri") or ""))
    if host and not _is_redirector(host):
        # Real, attributable host: it decides, full stop.
        return _is_pivota_host(host)
    title = str(source.get("title") or "").lower()
    return any(h in title for h in PIVOTA_HOSTS)


def _cited_domain(source: Dict[str, Any]) -> Optional[str]:
    """Best-effort publisher domain for a CITED source, or None.

    OpenAI: `uri` is the real cited URL -> use its host.
    Gemini: `uri` is a redirector, so the publisher can only come from
    `title`. Empirically (2026-07-25) Gemini titles came back as bare
    domains ("target.com", "iherb.com"), but the prober's own docs describe
    them as human-readable source names ("Sephora", "Olive Young Global").
    BOTH shapes occur, and a human-readable name is NOT resolvable to a
    domain here -- so we return None and the caller counts it as
    unattributable rather than guessing. Callers MUST surface that count;
    silently dropping it understates the competitor table.
    """
    host = _host_of(str(source.get("uri") or ""))
    if host and not _is_redirector(host):
        return host[4:] if host.startswith("www.") else host
    title = str(source.get("title") or "").strip().lower()
    # Only a bare-domain-looking title is attributable.
    if title and " " not in title and "." in title and "/" not in title:
        return title[4:] if title.startswith("www.") else title
    return None


def _run_errored(run: Dict[str, Any]) -> Optional[str]:
    """Return an error reason when a probe run FAILED, else None.

    🚨 The prober does NOT emit an `error` key on a run. Both raw_runs
    emitters encode failure as `raw = "__error__:<reason>"` (see
    agentCenterLlmProbe.js `rawText = \\`__error__:${reason}\\``). Reading only
    `run["error"]` therefore grades every timeout as an honest "model cited
    nothing" -- a 0% citation that is really an infrastructure failure, which
    is exactly the dilution this script claims to avoid.

    Mirrors `services/audit_run_worker.py::_run_errored`, which checks both.
    """
    explicit = run.get("error")
    if explicit:
        return str(explicit)
    raw = run.get("raw")
    if isinstance(raw, str) and raw.startswith("__error__"):
        return raw[len("__error__:"):].strip() or "unknown"
    return None


def _chunks(batch: Iterable[Any], size: int) -> Iterable[List[Any]]:
    buf: List[Any] = []
    for item in batch:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


# Tiers whose prompts are deliberately BLIND -- the buyer query does not name
# a product, so telling the model the product name would make the test a
# tautology (the prober's own rationale for adding category_visibility_test:
# "the model sees the product in the query and answers yes").
#
# 🚨 `buildPromptForScanMode` for this scan mode ALWAYS injects
# `Product: <context.product.title>` into the prompt. There is no way to send a
# query without a Product line. So for blind tiers we inject the neutral
# CATEGORY descriptor (`product_type`) instead of the branded SKU title. The
# attribution target (pivota_pdp_url) is unchanged, and a citation of any
# Pivota host still counts, so the headline metric is unaffected.
BLIND_TIERS = frozenset({"category", "ingredient_concern"})


def post_probe(
    agent_url: str,
    internal_key: str,
    *,
    anchor: Dict[str, str],
    queries: List[str],
    provider: str,
    timeout: int,
    retries: int,
    blind: bool = False,
) -> Dict[str, Any]:
    """POST one probe request (<= PROBE_HARD_MAX_RUNS queries).

    `blind=True` withholds the branded SKU title from the prompt (see
    BLIND_TIERS) so an open category/concern query is not answered by
    handing the model the product name.
    """
    pdp_url = f"https://agent.pivota.cc/products/{anchor['sig']}"
    prompt_product = anchor["product_type"] if blind else anchor["title"]
    body = {
        "scan_mode": PROBE_SCAN_MODE,
        "scan_target_id": anchor["sig"],
        # Required by validateRequest; this is Pivota's own seed surface.
        "merchant_id": "external_seed",
        "store_id": anchor["store_id"],
        "context": {
            "pivota_pdp_url": pdp_url,
            "product": {
                # Blind tiers get the category descriptor, not the SKU name.
                "title": prompt_product,
                # NOT anchor["brand"] — see SCORING_VENDOR note.
                "vendor": SCORING_VENDOR,
                "product_type": anchor["product_type"],
            },
            "queries": queries,
        },
        "options": {"provider": provider, "max_runs": len(queries)},
    }
    req = urllib.request.Request(
        agent_url.rstrip("/") + PROBE_PATH,
        data=json.dumps(body).encode(),
        headers={
            "X-Pivota-Internal-Key": internal_key,
            "Content-Type": "application/json",
        },
    )
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            # 408/429 are transient despite being 4xx; the rest of the 4xx
            # range is deterministic (bad body / bad key) and retrying only
            # burns time.
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                return {"_error": f"HTTP {exc.code}: {detail}"}
            last_err = f"HTTP {exc.code}: {detail}"
        except Exception as exc:  # noqa: BLE001 - transport churn is expected
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            # ⚠️ COST + BLAST RADIUS: the prober runs a request's grounded
            # calls SEQUENTIALLY and returns only at the end, so a
            # RemoteDisconnected means the LLM spend ALREADY happened
            # server-side. Every retry re-bills the whole batch and adds
            # consecutive failures to the SHARED Gemini global circuit
            # breaker (5 consecutive failures opens it for live Aurora
            # traffic). Keep `retries` low and `batch_size` at 1.
            time.sleep(min(4 * attempt, 15))
    return {"_error": last_err}


def run_baseline(args: argparse.Namespace, internal_key: str) -> Dict[str, Any]:
    # An alternate prompt set never MUTATES the portfolio -- it replaces the
    # selection for this run only, so `run_aeo_probe.sh` (no args) keeps
    # producing a number comparable to every prior run. Validated eagerly:
    # an unknown anchor would otherwise KeyError deep in the request loop,
    # after real LLM spend.
    source = PORTFOLIO
    # getattr, not args.prompts_file: run_baseline is called PROGRAMMATICALLY by
    # tests (and could be by other tooling) with a hand-built args object that
    # predates this flag. A bare attribute access turns "you did not pass the new
    # optional flag" into an AttributeError for every existing caller.
    prompts_file = getattr(args, "prompts_file", "") or ""
    if prompts_file:
        import json as _json
        loaded = _json.loads(pathlib.Path(prompts_file).read_text())
        source = loaded["prompts"] if isinstance(loaded, dict) else loaded
        bad_anchor = sorted({p["anchor"] for p in source} - set(ANCHORS))
        if bad_anchor:
            raise SystemExit(f"unknown anchor(s) in {prompts_file}: {bad_anchor}")
        bad_tier = sorted({p["tier"] for p in source} - set(TIER_ORDER))
        if bad_tier:
            raise SystemExit(f"unknown tier(s) in {prompts_file}: {bad_tier}")
    prompts = [p for p in source if not args.tiers or p["tier"] in args.tiers]
    if not prompts:
        raise SystemExit("no prompts selected — check --tiers")

    rows: List[Dict[str, Any]] = []
    request_errors: List[Dict[str, Any]] = []
    requests_made = 0

    for provider in args.providers:
        # Group by (anchor, blind): every query in a request shares one PDP
        # target AND one prompt Product line, so blind and named tiers can
        # never share a batch.
        by_group: Dict[Tuple[str, bool], List[Dict[str, str]]] = {}
        for prompt in prompts:
            key = (prompt["anchor"], prompt["tier"] in BLIND_TIERS)
            by_group.setdefault(key, []).append(prompt)

        for (anchor_key, blind), anchor_prompts in by_group.items():
            anchor = ANCHORS[anchor_key]
            for group in _chunks(anchor_prompts, args.batch_size):
                queries = [g["query"] for g in group]
                tier_of = {g["query"]: g["tier"] for g in group}
                # POSITIONAL is primary, the string map is the fallback. Keying
                # tiers on the ECHOED query is fragile: any upstream
                # normalisation (a trailing space is enough) sends every row to
                # "unknown", and because render_markdown iterates TIER_ORDER those
                # rows vanish from BOTH the tier table and the per-prompt table
                # while still counting in `overall`. The report would then claim a
                # headline rate with every tier reading 0/0. Verified today that
                # the probe echoes verbatim, so this is belt-and-braces - but with
                # batch_size=1 (the default) position is exact and free.
                tier_by_index = [g["tier"] for g in group]
                print(
                    f"[{provider}] {anchor_key}"
                    f"{' (blind)' if blind else ''}: {len(queries)} query(ies)",
                    flush=True,
                )
                payload = post_probe(
                    args.agent_url,
                    internal_key,
                    anchor=anchor,
                    queries=queries,
                    provider=provider,
                    timeout=args.timeout,
                    retries=args.retries,
                    blind=blind,
                )
                requests_made += 1
                if payload.get("_error"):
                    print(f"    REQUEST FAILED: {payload['_error']}", flush=True)
                    request_errors.append(
                        {
                            "provider": provider,
                            "anchor": anchor_key,
                            "queries": queries,
                            "error": payload["_error"],
                        }
                    )
                    continue

                result = payload.get("result") or {}
                reported = str(result.get("provider") or "")
                # S6: a mock fallback returns HTTP 200 with empty raw_runs when
                # the service has no API key. Never let that read as a measured 0%.
                if reported.lower().startswith(MOCK_PROVIDER_PREFIX):
                    print(f"    PROVIDER FELL BACK TO MOCK: {reported!r} "
                          f"-- lane is UNMEASURED, not 0%", flush=True)
                    request_errors.append({
                        "provider": provider,
                        "anchor": anchor_key,
                        "queries": queries,
                        "error": f"provider_fallback:{reported}",
                    })
                    continue

                emitted = result.get("raw_runs") or []
                if not emitted:
                    request_errors.append({
                        "provider": provider,
                        "anchor": anchor_key,
                        "queries": queries,
                        "error": f"no_raw_runs (provider_reported={reported!r})",
                    })
                    print("    NO RUNS RETURNED -- recorded as a gap", flush=True)
                    continue

                for run_index, run in enumerate(emitted):
                    query = str(run.get("query") or "")
                    sources = [s for s in (run.get("grounding_sources") or []) if s]
                    url_match = run.get("url_match") or {}
                    pivota_sources = [s for s in sources if _is_pivota_ref(s)]
                    attributed = [_cited_domain(s) for s in sources]
                    domains = sorted({d for d in attributed if d})
                    unattributable = sum(1 for d in attributed if not d)
                    err = _run_errored(run)
                    row = {
                        "tier": (
                            tier_by_index[run_index]
                            if run_index < len(tier_by_index)
                            else (tier_of.get(query) or "unknown")
                        ),
                        "provider": provider,
                        "provider_reported": reported,
                        "anchor": anchor_key,
                        "anchor_brand": anchor["brand"],
                        "target_pdp": f"https://agent.pivota.cc/products/{anchor['sig']}",
                        "query": query,
                        # HEADLINE METRIC: any Pivota surface among cited sources.
                        "pivota_cited": bool(pivota_sources),
                        # NOT "exact PDP". The prober's url_match.in_grounding
                        # ORs a path match against a substring match of the
                        # scoring vendor in the chunk TITLE, and for Gemini the
                        # path branch is unreachable (every chunk is a
                        # redirector) -- so this is a brand-substring signal for
                        # Gemini and a path match for OpenAI. Kept for
                        # diagnostics, deliberately NOT reported as the strict
                        # metric.
                        "probe_url_match_in_grounding": bool(url_match.get("in_grounding")),
                        "pivota_cited_uris": [s.get("uri") for s in pivota_sources],
                        "grounding_source_count": len(sources),
                        "unattributable_source_count": unattributable,
                        "cited_domains": domains,
                        "run_error": err,
                    }
                    rows.append(row)
                    if err:
                        flag = f"ERROR({err[:40]})"
                    else:
                        flag = "CITED" if row["pivota_cited"] else "not cited"
                    print(
                        f"    [{row['tier']}] {query[:52]!r} -> {flag} "
                        f"({len(sources)} sources"
                        + (f", {unattributable} unattributable" if unattributable else "")
                        + f": {', '.join(domains[:4]) or 'none'})",
                        flush=True,
                    )
    expected_by_tier: Dict[str, int] = {}
    for prompt in prompts:
        expected_by_tier[prompt["tier"]] = (
            expected_by_tier.get(prompt["tier"], 0) + len(args.providers)
        )
    return {
        "rows": rows,
        "request_errors": request_errors,
        "requests_made": requests_made,
        "prompts_selected": len(prompts),
        "expected_units": len(prompts) * len(args.providers),
        "expected_by_tier": expected_by_tier,
    }


def aggregate(rows: List[Dict[str, Any]], expected: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Aggregate rows into rates.

    `expected` maps tier -> number of query-units the run INTENDED to grade
    (prompts x providers). Without it a run that lost a whole tier to
    transport failures renders as a complete baseline over the tiers that
    survived. Rates are reported as explicit numerator/denominator; a
    percentage from n=10 is not precise to 0.1%.
    """
    expected = expected or {}

    def rate(subset: List[Dict[str, Any]], expect: Optional[int] = None) -> Dict[str, Any]:
        graded = [r for r in subset if not r.get("run_error")]
        cited = [r for r in graded if r["pivota_cited"]]
        zero_grounding = [r for r in graded if r["grounding_source_count"] == 0]
        unattributable = sum(r.get("unattributable_source_count", 0) for r in graded)
        out = {
            "graded": len(graded),
            "errored": len(subset) - len(graded),
            "cited": len(cited),
            "citation_rate_pct": (
                round(100.0 * len(cited) / len(graded), 1) if graded else None
            ),
            "probe_url_match_hits": sum(
                1 for r in graded if r.get("probe_url_match_in_grounding")
            ),
            "runs_with_zero_grounding": len(zero_grounding),
            "unattributable_sources": unattributable,
        }
        if expect is not None:
            out["expected"] = expect
            out["missing"] = max(0, expect - len(subset))
        return out

    # Every SELECTED tier appears, even with zero surviving rows.
    tiers = [t for t in TIER_ORDER if any(r["tier"] == t for r in rows) or t in expected]
    by_tier = {
        t: rate([r for r in rows if r["tier"] == t], expected.get(t))
        for t in tiers
    }
    by_provider = {
        p: rate([r for r in rows if r["provider"] == p])
        for p in sorted({r["provider"] for r in rows})
    }
    domains: Counter = Counter()
    for row in rows:
        if row.get("run_error"):
            continue
        for dom in row["cited_domains"]:
            domains[dom] += 1
    total_expected = sum(expected.values()) if expected else None
    return {
        "overall": rate(rows, total_expected),
        "by_tier": by_tier,
        "by_provider": by_provider,
        "cited_domains": domains.most_common(),
        "top_cited_domains": domains.most_common(30),
    }


def render_markdown(payload: Dict[str, Any], agg: Dict[str, Any], meta: Dict[str, Any]) -> str:
    rows = payload["rows"]
    out: List[str] = []
    out.append("# AEO Phase 0 — Inward Citation Baseline")
    out.append("")
    out.append(f"**Captured:** {meta['captured_at']}  ")
    out.append(f"**Scan mode:** `{PROBE_SCAN_MODE}`  ")
    out.append(f"**Providers run:** {', '.join(meta['providers'])}  ")
    if meta.get("providers_unavailable"):
        out.append(f"**Providers UNMEASURED:** {', '.join(meta['providers_unavailable'])}  ")
    out.append(f"**Prompts:** {payload['prompts_selected']} across "
               f"{len(agg['by_tier'])} intent tiers  ")
    expected = payload.get("expected_units")
    graded = ov_graded = agg["overall"]["graded"]
    out.append(f"**Query-units:** {graded} graded of {expected} expected "
               f"(prompts x providers)  ")
    out.append(f"**Probe requests:** {payload['requests_made']} "
               f"({len(payload['request_errors'])} failed)")
    if expected and graded < expected:
        out.append("")
        out.append(f"> ⚠️ **INCOMPLETE RUN — {expected - graded} of {expected} "
                   f"query-units are missing.** Rates below are computed over "
                   f"what survived and are NOT a complete baseline. See "
                   f"'Failed probe requests'.")
    out.append("")
    if meta.get("surface_note"):
        out.append("**Surface snapshot at capture time:** "
                   + meta["surface_note"])
        out.append("")

    ov = agg["overall"]
    out.append("## Headline")
    out.append("")
    out.append(f"- **Pivota citation rate: {ov['cited']}/{ov['graded']} "
               f"graded query-units ({ov['citation_rate_pct']}%)**")
    out.append(f"- Runs excluded as probe errors: {ov['errored']}")
    out.append(f"- Query-units where the model cited nothing: "
               f"{ov['runs_with_zero_grounding']}/{ov['graded']}")
    if ov.get("unattributable_sources"):
        out.append(f"- Cited sources whose publisher could not be resolved "
                   f"(counted, not attributed): {ov['unattributable_sources']}")
    out.append(f"- Prober `url_match.in_grounding` hits (diagnostic only — NOT "
               f"an exact-PDP match; see caveat): {ov['probe_url_match_hits']}")
    out.append("")

    out.append("## Citation rate by intent tier")
    out.append("")
    out.append("| Tier | Cited / Graded | Rate | Errored | Missing | Note |")
    out.append("|---|---|---|---|---|---|")
    for tier, stats in agg["by_tier"].items():
        rate = ("n/a" if stats["citation_rate_pct"] is None
                else f"{stats['citation_rate_pct']}%")
        out.append(
            f"| {tier} | {stats['cited']} / {stats['graded']} | {rate} | "
            f"{stats['errored']} | {stats.get('missing', 0)} | "
            f"{TIER_NOTES.get(tier, '')} |"
        )
    out.append("")
    out.append("_Denominators are small (prompts x providers per tier). Treat "
               "per-tier rates as directional; they support a floor, not a "
               "ranking between tiers._")
    out.append("")

    out.append("## By provider")
    out.append("")
    out.append("| Provider | Cited / Graded | Rate | Errored | Cited nothing |")
    out.append("|---|---|---|---|---|")
    for prov, stats in agg["by_provider"].items():
        rate = ("n/a" if stats["citation_rate_pct"] is None
                else f"{stats['citation_rate_pct']}%")
        out.append(
            f"| {prov} | {stats['cited']} / {stats['graded']} | {rate} | "
            f"{stats['errored']} | {stats['runs_with_zero_grounding']} |"
        )
    out.append("")
    out.append("_⚠️ Provider lanes are not strictly comparable: OpenAI "
               "`grounding_sources` are answer-level `url_citation` "
               "annotations, while Gemini's are `groundingChunks` (retrieved "
               "for grounding). The pooled headline mixes both._")
    out.append("")

    out.append("## Who owns the citations instead")
    out.append("")
    out.append("Cited-source domains, by number of query-units citing them.")
    out.append("")
    out.append("| Domain | Query-units |")
    out.append("|---|---|")
    for dom, count in agg["top_cited_domains"]:
        out.append(f"| {dom} | {count} |")
    out.append("")

    out.append("## Per-prompt results")
    out.append("")
    out.append("| Tier | Provider | Query | Pivota cited? | Cited instead |")
    out.append("|---|---|---|---|---|")
    for tier in TIER_ORDER:
        for row in [r for r in rows if r["tier"] == tier]:
            cited = "**YES**" if row["pivota_cited"] else "no"
            if row.get("run_error"):
                cited = f"error ({str(row['run_error'])[:40]})"
            if row["cited_domains"]:
                instead = ", ".join(row["cited_domains"][:6])
            elif row["grounding_source_count"]:
                # S11: sources existed but no publisher was resolvable.
                instead = (f"({row['grounding_source_count']} cited sources, "
                           f"publisher unresolvable)")
            else:
                instead = "(model cited nothing)"
            query = row["query"].replace("|", "\\|")
            out.append(
                f"| {tier} | {row['provider']} | {query} | {cited} | {instead} |"
            )
    out.append("")

    if payload["request_errors"]:
        out.append("## Failed probe requests — these prompts are MISSING")
        out.append("")
        for err in payload["request_errors"]:
            qs = ", ".join(err.get("queries") or []) or "(unknown)"
            out.append(f"- `{err['provider']}` / {err['anchor']}: "
                       f"{err['error']}\n  - lost prompts: {qs}")
        out.append("")

    out.append("## Method (so this is re-runnable)")
    out.append("")
    out.append("```")
    out.append("cd /Users/pengchydan/dev/PIVOTA-Agent")
    out.append("export PROMOTIONS_ADMIN_KEY=\"$(railway variables --kv \\")
    out.append("    | grep -m1 '^PROMOTIONS_ADMIN_KEY=' | cut -d= -f2-)\"")
    out.append("python3 <pivota-backend>/scripts/aeo_phase0_citation_baseline.py \\")
    out.append("    --output ~/dev/AEO_PHASE0_BASELINE_$(date +%F).md \\")
    out.append("    --json-output /tmp/aeo_phase0_$(date +%F).json")
    out.append("```")
    out.append("")
    out.append("- Portfolio is FROZEN in the script. Do not edit prompts between "
               "runs or the comparison is void.")
    out.append("- `pivota_cited` is domain-level (any Pivota host in the CITED "
               "grounding sources), computed in this script rather than taken "
               "from the probe's `visibility_score`, because the probe's "
               "brand-title match would false-positive on catalog brands.")
    out.append("- Re-run 1-2 weeks after an intervention: crawlers must "
               "re-fetch and re-index before citations can move.")
    return "\n".join(out) + "\n"


def parse_args(argv: List[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="AEO Phase 0 inward citation baseline")
    ap.add_argument("--agent-url", default=os.environ.get(
        "PIVOTA_AGENT_INTERNAL_URL", DEFAULT_AGENT_URL))
    ap.add_argument("--providers", default="gemini,chatgpt",
                    help="comma-separated: gemini,chatgpt (claude needs an "
                         "ANTHROPIC_API_KEY on the agent service)")
    ap.add_argument("--prompts-file", default="",
                    help="JSON file with an ALTERNATE prompt set: {\"prompts\":[{tier,anchor,query}]}. "
                         "Omitted = the built-in PORTFOLIO, so the spine metric is unchanged by default. "
                         "Anchors must already exist in ANCHORS; tiers must be known tiers.")
    ap.add_argument("--tiers", default="",
                    help="comma-separated subset of "
                         f"{','.join(TIER_ORDER)} (default: all)")
    ap.add_argument("--output", default="", help="markdown artifact path")
    ap.add_argument("--json-output", default="", help="raw per-run JSON path")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--retries", type=int, default=2,
                    help="TOTAL attempts per request (2 = one retry). Each retry re-bills the whole batch server-side -- see post_probe.")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                    help=f"queries per probe request (default "
                         f"{DEFAULT_BATCH_SIZE}, max {PROBE_HARD_MAX_RUNS}); "
                         "larger batches get killed by the edge proxy")
    ap.add_argument("--surface-note", default="",
                    help="free-text surface snapshot recorded in the artifact")
    args = ap.parse_args(argv)
    args.providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    args.tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    bad_tiers = [t for t in args.tiers if t not in TIER_ORDER]
    if bad_tiers:
        ap.error(f"unknown tier(s): {', '.join(bad_tiers)}")
    bad_prov = [p for p in args.providers if p not in ("gemini", "chatgpt", "claude")]
    if bad_prov:
        ap.error(f"unsupported provider(s): {', '.join(bad_prov)}")
    if not 1 <= args.batch_size <= PROBE_HARD_MAX_RUNS:
        ap.error(f"--batch-size must be 1..{PROBE_HARD_MAX_RUNS}")
    return args


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    internal_key = os.environ.get("PROMOTIONS_ADMIN_KEY", "").strip()
    if not internal_key:
        print("PROMOTIONS_ADMIN_KEY is required (X-Pivota-Internal-Key).",
              file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc)
    payload = run_baseline(args, internal_key)
    if not payload["rows"]:
        print("no probe runs completed — baseline NOT captured", file=sys.stderr)
        for err in payload["request_errors"]:
            print(f"  {err['provider']}/{err['anchor']}: {err['error']}", file=sys.stderr)
        return 1

    agg = aggregate(payload["rows"], payload.get("expected_by_tier"))
    meta = {
        "captured_at": started.isoformat(),
        "providers": args.providers,
        # Lanes that produced NO graded rows are unmeasured, not 0%.
        "providers_unavailable": sorted(
            {p for p in args.providers
             if not any(r["provider"] == p and not r.get("run_error")
                        for r in payload["rows"])}
        ) + ([] if "claude" in args.providers
             else ["claude (not requested; needs ANTHROPIC_API_KEY on prod "
                   "PIVOTA-Agent, which is absent)"]),
        "surface_note": args.surface_note,
    }

    # WHICH CODE PRODUCED THESE NUMBERS. Review could not establish whether the
    # 2026-07-25 reference run predated the blind-tier fix (973cdd65), which
    # changed the request payload for 20 of 50 units - and nothing anywhere
    # recorded the SHA. That ambiguity cost the per-tier detail of two tiers.
    # Never again: stamp it into both artifacts.
    try:
        meta["script_git_sha"] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "unknown"
    except Exception:
        meta["script_git_sha"] = "unknown"

    print("\n===== AEO PHASE 0 BASELINE =====")
    print(json.dumps({"overall": agg["overall"], "by_tier": agg["by_tier"],
                      "by_provider": agg["by_provider"]}, indent=2))
    print("top cited domains:", agg["top_cited_domains"][:12])

    # ===== INSTRUMENT SUSPECT =====
    # THE ONE FAILURE MODE THAT LOOKS EXACTLY LIKE A REAL RESULT.
    #
    # Every other degradation is separated from "not cited" and excluded from the
    # graded denominator - auth failure, transport failure, per-run timeout,
    # provider outage, missing API key, empty raw_runs. This one is not: if the
    # gateway ever renames or empties `grounding_sources` while still returning a
    # well-formed 200, EVERY unit grades "not cited", the run exits 0, and the
    # artifact reports a clean 0.0% baseline. Simulated in review with all 25
    # queries genuinely citing us: headline 0/25, errored 0, exit 0.
    #
    # Zero grounding on EVERY graded unit is not a plausible measurement. The
    # 2026-07-25 reference run returned sources on 44 of 50. So say so loudly
    # rather than leaving the operator to remember that number.
    ov = agg["overall"]
    if ov.get("graded", 0) > 0 and ov.get("runs_with_zero_grounding", 0) == ov["graded"]:
        print("\n" + "=" * 62)
        print("INSTRUMENT SUSPECT - DO NOT REPORT THIS AS A BASELINE")
        print(f"  every one of {ov['graded']} graded units returned ZERO grounding")
        print("  sources. A real 0% citation run still elicits sources (07-25:")
        print("  44/50). This is the signature of a response-shape change -")
        print("  check that the probe still emits `grounding_sources` per run")
        print("  before treating the citation rate as a measurement.")
        print("=" * 62)

    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as fh:
            json.dump({"meta": meta, "aggregate": agg, **payload}, fh, indent=1)
        print(f"\nraw JSON -> {args.json_output}")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(payload, agg, meta))
        print(f"artifact -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
