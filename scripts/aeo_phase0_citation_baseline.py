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
   runs used the attribution mode and got 0 grounding sources on every
   query. That was a measurement artifact, not a finding: the attribution
   mode's prompt ("Given a query, return whether the verified Pivota PDP
   URL is mentioned") never instructs the model to search. The grounded
   discovery mode does, and returns real cited sources.

3. **Domain-level citation, computed here — not the probe's score.**
   `groundingContainsUrl()` in the probe counts a hit when a grounding
   chunk TITLE contains `merchantBrand`. That is correct for the merchant
   lane (the merchant's own store being cited is the thing measured) but is
   a false-positive generator when pointed inward: passing vendor="COSRX"
   would score a cosrx.com or Ulta citation as a Pivota citation. So we
   pass vendor="Pivota" AND independently recompute the hit from the raw
   `grounding_sources`, matching on our own hosts only.

WHO-IS-CITED-INSTEAD: Gemini returns opaque `vertexaisearch...redirect`
URIs, so the URI is useless for attribution — but the chunk `title` is the
bare domain ("target.com", "iherb.com"), which is what we harvest. OpenAI
returns real cited URLs, so we take the host. Only CITED sources are
counted, never the retrieved candidate pool.

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
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
# succeeded, 4-5 were reliably killed. Default small; raising this trades
# wall-clock for lost batches, and a lost batch is a hole in the baseline.
DEFAULT_BATCH_SIZE = 2

# The hosts that count as "Pivota was cited". Kept explicit (not a substring
# check on "pivota") so that e.g. a blog post ABOUT Pivota on someone else's
# domain is not miscounted as our surface being cited.
PIVOTA_HOSTS = ("agent.pivota.cc", "www.pivota.cc", "pivota.cc", "api.pivota.cc")

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


def _is_pivota_ref(source: Dict[str, Any]) -> bool:
    """True when a CITED grounding source points at a Pivota surface.

    Checks the host (OpenAI cites real URLs) and the title (Gemini wraps
    every URI in an opaque vertexaisearch redirect but sets `title` to the
    bare domain). Substring-matching the title is only safe because we test
    full hostnames from PIVOTA_HOSTS, not the bare token "pivota".
    """
    host = _host_of(str(source.get("uri") or ""))
    if host and any(host == h or host.endswith("." + h) for h in PIVOTA_HOSTS):
        return True
    title = str(source.get("title") or "").lower()
    return any(h in title for h in PIVOTA_HOSTS)


def _cited_domain(source: Dict[str, Any]) -> Optional[str]:
    """Best-effort publisher domain for a cited source.

    Gemini: the redirect URI carries no publisher, but `title` IS the domain.
    OpenAI: `uri` is the real cited URL and `title` is a page title.
    """
    host = _host_of(str(source.get("uri") or ""))
    if host and "vertexaisearch" not in host and "grounding-api" not in host:
        return host[4:] if host.startswith("www.") else host
    title = str(source.get("title") or "").strip().lower()
    # A Gemini chunk title is a bare domain like "target.com".
    if title and " " not in title and "." in title:
        return title[4:] if title.startswith("www.") else title
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


def post_probe(
    agent_url: str,
    internal_key: str,
    *,
    anchor: Dict[str, str],
    queries: List[str],
    provider: str,
    timeout: int,
    retries: int,
) -> Dict[str, Any]:
    """POST one probe request (<= PROBE_HARD_MAX_RUNS queries)."""
    pdp_url = f"https://agent.pivota.cc/products/{anchor['sig']}"
    body = {
        "scan_mode": PROBE_SCAN_MODE,
        "scan_target_id": anchor["sig"],
        # Required by validateRequest; this is Pivota's own seed surface.
        "merchant_id": "external_seed",
        "store_id": anchor["store_id"],
        "context": {
            "pivota_pdp_url": pdp_url,
            "product": {
                "title": anchor["title"],
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
            # 4xx is deterministic (bad body/auth) — retrying cannot help.
            detail = exc.read().decode(errors="replace")[:300]
            if 400 <= exc.code < 500:
                return {"_error": f"HTTP {exc.code}: {detail}"}
            last_err = f"HTTP {exc.code}: {detail}"
        except Exception as exc:  # noqa: BLE001 - transport churn is expected
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(min(4 * attempt, 15))
    return {"_error": last_err}


def run_baseline(args: argparse.Namespace, internal_key: str) -> Dict[str, Any]:
    prompts = [p for p in PORTFOLIO if not args.tiers or p["tier"] in args.tiers]
    if not prompts:
        raise SystemExit("no prompts selected — check --tiers")

    rows: List[Dict[str, Any]] = []
    request_errors: List[Dict[str, Any]] = []
    requests_made = 0

    for provider in args.providers:
        # Group by anchor so every query in a request shares one PDP target.
        by_anchor: Dict[str, List[Dict[str, str]]] = {}
        for prompt in prompts:
            by_anchor.setdefault(prompt["anchor"], []).append(prompt)

        for anchor_key, anchor_prompts in by_anchor.items():
            anchor = ANCHORS[anchor_key]
            for group in _chunks(anchor_prompts, args.batch_size):
                queries = [g["query"] for g in group]
                tier_of = {g["query"]: g["tier"] for g in group}
                print(
                    f"[{provider}] {anchor_key}: {len(queries)} query(ies)",
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
                reported = result.get("provider")
                for run in result.get("raw_runs") or []:
                    query = str(run.get("query") or "")
                    sources = [s for s in (run.get("grounding_sources") or []) if s]
                    url_match = run.get("url_match") or {}
                    pivota_sources = [s for s in sources if _is_pivota_ref(s)]
                    domains = sorted(
                        {d for d in (_cited_domain(s) for s in sources) if d}
                    )
                    row = {
                        "tier": tier_of.get(query, "unknown"),
                        "provider": provider,
                        "provider_reported": reported,
                        "anchor": anchor_key,
                        "anchor_brand": anchor["brand"],
                        "target_pdp": f"https://agent.pivota.cc/products/{anchor['sig']}",
                        "query": query,
                        # Domain-level: any Pivota surface cited. HEADLINE METRIC.
                        "pivota_cited": bool(pivota_sources),
                        # Stricter: the probe matched the exact target PDP.
                        "exact_pdp_in_grounding": bool(url_match.get("in_grounding")),
                        "pivota_cited_uris": [s.get("uri") for s in pivota_sources],
                        "grounding_source_count": len(sources),
                        "cited_domains": domains,
                        "run_error": run.get("error"),
                    }
                    rows.append(row)
                    flag = "CITED" if row["pivota_cited"] else "not cited"
                    print(
                        f"    [{row['tier']}] {query[:52]!r} -> {flag} "
                        f"({len(sources)} sources: {', '.join(domains[:4]) or 'none'})",
                        flush=True,
                    )
    return {
        "rows": rows,
        "request_errors": request_errors,
        "requests_made": requests_made,
        "prompts_selected": len(prompts),
    }


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    def rate(subset: List[Dict[str, Any]]) -> Dict[str, Any]:
        graded = [r for r in subset if not r.get("run_error")]
        cited = [r for r in graded if r["pivota_cited"]]
        exact = [r for r in graded if r["exact_pdp_in_grounding"]]
        zero_grounding = [r for r in graded if r["grounding_source_count"] == 0]
        return {
            "graded": len(graded),
            "errored": len(subset) - len(graded),
            "cited": len(cited),
            "citation_rate_pct": round(100.0 * len(cited) / len(graded), 1) if graded else None,
            "exact_pdp_cited": len(exact),
            "runs_with_zero_grounding": len(zero_grounding),
        }

    by_tier = {t: rate([r for r in rows if r["tier"] == t]) for t in TIER_ORDER
               if any(r["tier"] == t for r in rows)}
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
    return {
        "overall": rate(rows),
        "by_tier": by_tier,
        "by_provider": by_provider,
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
    out.append(f"**Probe requests:** {payload['requests_made']} "
               f"({len(payload['request_errors'])} failed)")
    out.append("")
    if meta.get("surface_note"):
        out.append("**Surface snapshot at capture time:** "
                   + meta["surface_note"])
        out.append("")

    ov = agg["overall"]
    out.append("## Headline")
    out.append("")
    out.append(f"- **Pivota citation rate: {ov['citation_rate_pct']}%** "
               f"({ov['cited']}/{ov['graded']} graded query-units)")
    out.append(f"- Exact target PDP in grounding: {ov['exact_pdp_cited']}/{ov['graded']}")
    out.append(f"- Query-units where the model returned NO cited source at all: "
               f"{ov['runs_with_zero_grounding']}/{ov['graded']}")
    out.append("")

    out.append("## Citation rate by intent tier")
    out.append("")
    out.append("| Tier | Cited | Graded | Rate | Exact PDP | Note |")
    out.append("|---|---|---|---|---|---|")
    for tier, stats in agg["by_tier"].items():
        out.append(
            f"| {tier} | {stats['cited']} | {stats['graded']} | "
            f"{stats['citation_rate_pct']}% | {stats['exact_pdp_cited']} | "
            f"{TIER_NOTES.get(tier, '')} |"
        )
    out.append("")

    out.append("## By provider")
    out.append("")
    out.append("| Provider | Cited | Graded | Rate | Zero-grounding runs |")
    out.append("|---|---|---|---|---|")
    for prov, stats in agg["by_provider"].items():
        out.append(
            f"| {prov} | {stats['cited']} | {stats['graded']} | "
            f"{stats['citation_rate_pct']}% | {stats['runs_with_zero_grounding']} |"
        )
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
                cited = "error"
            instead = ", ".join(row["cited_domains"][:6]) or "(no cited sources)"
            query = row["query"].replace("|", "\\|")
            out.append(
                f"| {tier} | {row['provider']} | {query} | {cited} | {instead} |"
            )
    out.append("")

    if payload["request_errors"]:
        out.append("## Failed probe requests")
        out.append("")
        for err in payload["request_errors"]:
            out.append(f"- `{err['provider']}` / {err['anchor']}: {err['error']}")
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
    ap.add_argument("--tiers", default="",
                    help="comma-separated subset of "
                         f"{','.join(TIER_ORDER)} (default: all)")
    ap.add_argument("--output", default="", help="markdown artifact path")
    ap.add_argument("--json-output", default="", help="raw per-run JSON path")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--retries", type=int, default=3)
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
    bad_prov = [p for p in args.providers if p not in ("gemini", "chatgpt", "claude", "mock")]
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

    agg = aggregate(payload["rows"])
    meta = {
        "captured_at": started.isoformat(),
        "providers": args.providers,
        "providers_unavailable": (
            ["claude (no ANTHROPIC_API_KEY on prod PIVOTA-Agent)"]
            if "claude" not in args.providers else []
        ),
        "surface_note": args.surface_note,
    }

    print("\n===== AEO PHASE 0 BASELINE =====")
    print(json.dumps({"overall": agg["overall"], "by_tier": agg["by_tier"],
                      "by_provider": agg["by_provider"]}, indent=2))
    print("top cited domains:", agg["top_cited_domains"][:12])

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
